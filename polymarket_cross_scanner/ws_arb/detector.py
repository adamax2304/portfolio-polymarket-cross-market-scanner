"""Arb spike detector.

Watches Σbid and Σask state per event, emits alerts when an arb threshold
is crossed, tracks how long the spike survives.

Alert fires:
  Σbid > 1 + min_edge   → SPLIT_AND_SELL opportunity
  Σask < 1 - min_edge   → BUY_AND_MERGE opportunity

Each alert is logged with:
  ts, event_slug, direction, Σ_at_detect, theoretical_edge, vol24h,
  durability_ms (how long the edge persisted),
  realized_edge_after_500ms / after_2000ms (simulated execution slip)
"""
from __future__ import annotations

import csv
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .book_state import EventBookAggregator
from .discovery import TrackedEvent


@dataclass
class Alert:
    detected_at_ts: float
    event_slug: str
    direction: str  # "SPLIT_AND_SELL" or "BUY_AND_MERGE"
    sum_value: float  # Σbids or Σasks
    gross_edge: float
    n_markets: int
    vol_24h: float
    # Durability tracking
    still_active: bool = True
    ended_at_ts: float = 0.0
    edge_after_500ms: Optional[float] = None
    edge_after_2000ms: Optional[float] = None
    edge_after_5000ms: Optional[float] = None
    max_edge_during: float = 0.0
    # Realism tracking (v2)
    bottleneck_size_at_detect: float = 0.0  # shares — min size at top of book across tokens
    oldest_book_age_ms_at_detect: float = 0.0  # ms — staleness of stalest book
    bottleneck_size_after_500ms: Optional[float] = None
    bottleneck_size_after_2000ms: Optional[float] = None
    bottleneck_size_after_5000ms: Optional[float] = None
    # Walk-the-book execution sim (v3) — realistic profit consuming multiple
    # book levels, bounded by depth + capital. exec_profit==0 means no-fill.
    exec_units_detect: float = 0.0
    exec_profit_detect: float = 0.0
    exec_profit_after_500ms: Optional[float] = None
    exec_profit_after_2000ms: Optional[float] = None
    # Shadow-fill race test (v4) — would WE fill, at our measured latency, with
    # zero capital? Distinguishes losing the race (a real trade printed first)
    # from quote-decay (maker pulled — a faster bot would catch it).
    exec_profit_at_latency: Optional[float] = None  # walk profit at detect+latency
    trades_within_latency: int = 0    # competing trades on legs in [detect, +latency]
    trades_total_during: int = 0      # competing trades on legs over whole spike
    fill_outcome: str = ""            # WON | LOST_RACE | DECAYED


class ArbDetector:
    def __init__(
        self,
        events: list[TrackedEvent],
        *,
        min_edge: float = 0.02,
        alert_cooldown_seconds: float = 30.0,
        alert_csv_path: Path,
        exec_capital: float = 100.0,
        fee_rate: float = 0.06,
        shadow_latency_ms: float = 150.0,
    ):
        self.events = {ev.slug: ev for ev in events}
        self.aggregators: dict[str, EventBookAggregator] = {
            ev.slug: EventBookAggregator(ev.slug, ev.yes_token_ids) for ev in events
        }
        # Reverse lookup: asset_id → event_slug
        self.asset_to_event: dict[str, str] = {}
        for ev in events:
            for tid in ev.yes_token_ids:
                self.asset_to_event[tid] = ev.slug

        self.min_edge = min_edge
        self.alert_cooldown_seconds = alert_cooldown_seconds
        self.exec_capital = exec_capital
        self.fee_rate = fee_rate
        self.shadow_latency_ms = shadow_latency_ms
        self.alert_csv_path = alert_csv_path
        self.alert_csv_path.parent.mkdir(parents=True, exist_ok=True)
        self._csv_writer = None
        self._csv_file = None
        self._open_csv()

        # Active alerts (still being tracked)
        self.active_alerts: dict[str, Alert] = {}  # by event_slug
        self.last_alert_ts: dict[str, float] = {}  # cooldown tracker
        # Stats
        self.total_alerts = 0
        self.total_messages_seen = 0
        self.start_ts = time.time()

    CSV_HEADER = [
        "detected_at", "event_slug", "direction", "sum_value", "gross_edge",
        "n_markets", "vol_24h",
        "ended_at", "durability_ms", "max_edge_during",
        "edge_500ms", "edge_2000ms", "edge_5000ms",
        # v2 additions
        "bottleneck_size_at_detect", "oldest_book_age_ms_at_detect",
        "bottleneck_size_500ms", "bottleneck_size_2000ms", "bottleneck_size_5000ms",
        # v3 additions — walk-the-book realistic execution
        "exec_units_detect", "exec_profit_detect",
        "exec_profit_500ms", "exec_profit_2000ms",
        # v4 additions — shadow-fill race test (no capital)
        "exec_profit_at_latency", "trades_within_latency",
        "trades_total_during", "fill_outcome",
    ]

    def _open_csv(self) -> None:
        # If an existing CSV has a different (older-schema) header, archive it to
        # a non-clobbering .archiveN.csv before starting a fresh file. This makes
        # schema migrations safe and idempotent across versions.
        if self.alert_csv_path.exists():
            try:
                with open(self.alert_csv_path, encoding="utf-8") as f:
                    first_line = f.readline().strip()
                expected_first = ",".join(self.CSV_HEADER)
                if first_line and first_line != expected_first:
                    archived = False
                    for k in range(1, 100):
                        archive = self.alert_csv_path.with_suffix(f".archive{k}.csv")
                        if not archive.exists():
                            self.alert_csv_path.rename(archive)
                            print(f"[ws-arb] archived old-schema CSV to {archive.name}")
                            archived = True
                            break
                    if not archived:
                        self.alert_csv_path.unlink()
                        print("[ws-arb] removed old-schema CSV (archive slots full)")
            except Exception as e:
                print(f"[ws-arb] CSV archive check failed: {e}")

        new = not self.alert_csv_path.exists()
        self._csv_file = open(self.alert_csv_path, "a", newline="", encoding="utf-8")
        self._csv_writer = csv.writer(self._csv_file)
        if new:
            self._csv_writer.writerow(self.CSV_HEADER)
            self._csv_file.flush()

    async def dispatch(self, msg: dict) -> None:
        """Handle one WS message."""
        self.total_messages_seen += 1
        event_type = msg.get("event_type", "")
        if event_type == "book":
            asset = str(msg.get("asset_id", ""))
            slug = self.asset_to_event.get(asset)
            if not slug:
                return
            agg = self.aggregators[slug]
            book = agg.get_or_create(asset)
            book.apply_snapshot(msg)
            self._check_event(slug)
        elif event_type == "price_change":
            changes = msg.get("price_changes") or msg.get("changes") or []
            # Group changes by asset
            by_asset: dict[str, list[dict]] = {}
            for c in changes:
                a = str(c.get("asset_id", ""))
                if a:
                    by_asset.setdefault(a, []).append(c)
            affected_events = set()
            for asset, asset_changes in by_asset.items():
                slug = self.asset_to_event.get(asset)
                if not slug:
                    continue
                agg = self.aggregators[slug]
                book = agg.get_or_create(asset)
                book.apply_price_change(asset_changes)
                affected_events.add(slug)
            for slug in affected_events:
                self._check_event(slug)
        elif event_type == "last_trade_price":
            # A real trade executed on a token. If an arb alert is currently
            # active on that token's event, this trade is a competitor consuming
            # liquidity during our spike window — the core race signal.
            asset = str(msg.get("asset_id", ""))
            slug = self.asset_to_event.get(asset)
            if not slug:
                return
            active = self.active_alerts.get(slug)
            if active is not None:
                active.trades_total_during += 1
                since_ms = (time.time() - active.detected_at_ts) * 1000
                if since_ms <= self.shadow_latency_ms:
                    active.trades_within_latency += 1

    def _check_event(self, event_slug: str) -> None:
        agg = self.aggregators[event_slug]
        if not agg.all_tokens_ready():
            return
        sum_bids, n_bids, total = agg.sum_bids()
        sum_asks, n_asks, _ = agg.sum_asks()
        # Skip if any token has no bid or no ask
        if n_bids < total or n_asks < total:
            return
        sell_edge = sum_bids - 1.0  # SPLIT_AND_SELL
        buy_edge = 1.0 - sum_asks  # BUY_AND_MERGE

        now_ts = time.time()
        ev = self.events[event_slug]

        # Update active alert if any
        active = self.active_alerts.get(event_slug)
        if active is not None:
            current_edge = sell_edge if active.direction == "SPLIT_AND_SELL" else buy_edge
            active.max_edge_during = max(active.max_edge_during, current_edge)
            # Record edge + bottleneck size at sample windows
            since_ms = (now_ts - active.detected_at_ts) * 1000
            current_size = (agg.bottleneck_bid_size() if active.direction == "SPLIT_AND_SELL"
                            else agg.bottleneck_ask_size())
            # Shadow-fill latency gate: would the arb still be executable at the
            # instant our order actually reaches the matching engine?
            if active.exec_profit_at_latency is None and since_ms >= self.shadow_latency_ms:
                _, ep_lat, _ = agg.walk_book_profit(active.direction, self.fee_rate, self.exec_capital)
                active.exec_profit_at_latency = round(ep_lat, 4)
            if active.edge_after_500ms is None and since_ms >= 500:
                active.edge_after_500ms = round(current_edge, 5)
                active.bottleneck_size_after_500ms = round(current_size, 2)
                _, ep, _ = agg.walk_book_profit(active.direction, self.fee_rate, self.exec_capital)
                active.exec_profit_after_500ms = round(ep, 4)
            if active.edge_after_2000ms is None and since_ms >= 2000:
                active.edge_after_2000ms = round(current_edge, 5)
                active.bottleneck_size_after_2000ms = round(current_size, 2)
                _, ep, _ = agg.walk_book_profit(active.direction, self.fee_rate, self.exec_capital)
                active.exec_profit_after_2000ms = round(ep, 4)
            if active.edge_after_5000ms is None and since_ms >= 5000:
                active.edge_after_5000ms = round(current_edge, 5)
                active.bottleneck_size_after_5000ms = round(current_size, 2)
            # If edge dropped below threshold, close the alert
            if current_edge < self.min_edge:
                active.still_active = False
                active.ended_at_ts = now_ts
                durability_ms = round((active.ended_at_ts - active.detected_at_ts) * 1000)
                self._classify_fill(active)
                self._write_alert(active, durability_ms)
                self.active_alerts.pop(event_slug, None)
            return

        # No active alert — check for new spike
        if sell_edge >= self.min_edge or buy_edge >= self.min_edge:
            cool = self.last_alert_ts.get(event_slug, 0)
            if now_ts - cool < self.alert_cooldown_seconds:
                return
            if sell_edge >= buy_edge:
                direction = "SPLIT_AND_SELL"
                sum_val = sum_bids
                gross = sell_edge
                bottleneck = agg.bottleneck_bid_size()
            else:
                direction = "BUY_AND_MERGE"
                sum_val = sum_asks
                gross = buy_edge
                bottleneck = agg.bottleneck_ask_size()
            oldest_age_s, _ = agg.freshness()
            oldest_age_ms = round(oldest_age_s * 1000) if oldest_age_s != float("inf") else 0
            exec_units, exec_profit, _ = agg.walk_book_profit(
                direction, self.fee_rate, self.exec_capital)
            alert = Alert(
                detected_at_ts=now_ts,
                event_slug=event_slug,
                direction=direction,
                sum_value=round(sum_val, 5),
                gross_edge=round(gross, 5),
                n_markets=total,
                vol_24h=ev.volume_24h,
                max_edge_during=gross,
                bottleneck_size_at_detect=round(bottleneck, 2),
                oldest_book_age_ms_at_detect=oldest_age_ms,
                exec_units_detect=round(exec_units, 2),
                exec_profit_detect=round(exec_profit, 4),
            )
            self.active_alerts[event_slug] = alert
            self.last_alert_ts[event_slug] = now_ts
            self.total_alerts += 1
            ts_iso = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(timespec="milliseconds")
            print(f"[ALERT] {ts_iso} {direction} edge={gross:.4f} Σ={sum_val:.4f} "
                  f"depth={bottleneck:.0f} stale={oldest_age_ms}ms "
                  f"exec=${exec_profit:.2f}/{exec_units:.0f}u "
                  f"event={event_slug[:46]} vol={ev.volume_24h:,.0f}")

    def _classify_fill(self, a: Alert) -> None:
        """Shadow-fill verdict — no capital used.

        WON       — arb still executable at our measured latency: we'd have filled.
        LOST_RACE — gone by our latency AND a real trade printed on the legs first:
                    a faster (likely co-located) bot took it. Speed from here loses.
        DECAYED   — gone by our latency but NO trade printed: the maker just pulled
                    the quote. A faster bot WOULD beat the pull — worth building.
        """
        ep = a.exec_profit_at_latency
        if ep is not None and ep > 0:
            a.fill_outcome = "WON"
        elif a.trades_within_latency > 0:
            a.fill_outcome = "LOST_RACE"
        else:
            a.fill_outcome = "DECAYED"

    def _write_alert(self, a: Alert, durability_ms: int) -> None:
        end_iso = datetime.fromtimestamp(a.ended_at_ts, tz=timezone.utc).isoformat(timespec="milliseconds")
        det_iso = datetime.fromtimestamp(a.detected_at_ts, tz=timezone.utc).isoformat(timespec="milliseconds")
        self._csv_writer.writerow([
            det_iso, a.event_slug, a.direction, a.sum_value, a.gross_edge,
            a.n_markets, a.vol_24h,
            end_iso, durability_ms, round(a.max_edge_during, 5),
            a.edge_after_500ms if a.edge_after_500ms is not None else "",
            a.edge_after_2000ms if a.edge_after_2000ms is not None else "",
            a.edge_after_5000ms if a.edge_after_5000ms is not None else "",
            round(a.bottleneck_size_at_detect, 2), round(a.oldest_book_age_ms_at_detect),
            a.bottleneck_size_after_500ms if a.bottleneck_size_after_500ms is not None else "",
            a.bottleneck_size_after_2000ms if a.bottleneck_size_after_2000ms is not None else "",
            a.bottleneck_size_after_5000ms if a.bottleneck_size_after_5000ms is not None else "",
            round(a.exec_units_detect, 2), round(a.exec_profit_detect, 4),
            a.exec_profit_after_500ms if a.exec_profit_after_500ms is not None else "",
            a.exec_profit_after_2000ms if a.exec_profit_after_2000ms is not None else "",
            a.exec_profit_at_latency if a.exec_profit_at_latency is not None else "",
            a.trades_within_latency, a.trades_total_during, a.fill_outcome,
        ])
        self._csv_file.flush()
        print(f"[CLOSED] {a.event_slug[:46]} dur={durability_ms}ms "
              f"exec_detect=${a.exec_profit_detect:.2f} "
              f"fill={a.fill_outcome} trades@lat={a.trades_within_latency}")

    def heartbeat(self) -> str:
        elapsed = time.time() - self.start_ts
        rate = self.total_messages_seen / elapsed if elapsed > 0 else 0
        return f"events={len(self.events)} msgs={self.total_messages_seen} ({rate:.1f}/s) alerts={self.total_alerts} active={len(self.active_alerts)}"

    def close(self) -> None:
        # Close any active alerts
        now_ts = time.time()
        for a in list(self.active_alerts.values()):
            a.ended_at_ts = now_ts
            durability_ms = round((now_ts - a.detected_at_ts) * 1000)
            self._classify_fill(a)
            self._write_alert(a, durability_ms)
        if self._csv_file:
            self._csv_file.close()
