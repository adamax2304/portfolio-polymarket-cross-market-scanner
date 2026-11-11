"""Multi-outcome sum-to-1 arbitrage scanner.

Polymarket groups exclusive outcomes (e.g., "Who wins 2026 election?") under one
event. The YES prices of all candidate outcomes should sum to ~$1.

Arb signal:
  Σ YES > 1.05  → SHORT all outcomes (buy NO of each) for guaranteed profit
  Σ YES < 0.95  → LONG all outcomes (buy YES of each) for guaranteed profit

Outputs: one CSV row per anomalous event.
"""
from __future__ import annotations

import csv
import json
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
UA = {"User-Agent": "Mozilla/5.0 (IntraArbScanner/1.0)"}


@dataclass
class SumOutcomeFinding:
    event_slug: str
    event_title: str
    n_markets: int
    sum_yes: float
    arb_direction: str  # "BUY_ALL_YES" or "BUY_ALL_NO"
    gross_edge: float
    min_market_yes: float
    max_market_yes: float
    total_volume_24h: float
    market_questions: list[str]
    market_yes_prices: list[float]
    url: str


def _get(url: str) -> Optional[object]:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  ! fetch failed {url[:80]}: {e}")
        return None


def fetch_events(limit_pages: int = 6, page_size: int = 50) -> list[dict]:
    """Fetch active events from Polymarket gamma."""
    out: list[dict] = []
    for page in range(limit_pages):
        offset = page * page_size
        url = f"{GAMMA_EVENTS}?active=true&closed=false&limit={page_size}&offset={offset}&order=volume24hr&ascending=false"
        data = _get(url)
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        if len(data) < page_size:
            break
        time.sleep(0.2)
    return out


def scan_sum_outcome(
    *,
    min_markets: int = 3,
    min_gross_edge: float = 0.02,
    min_event_volume: float = 500.0,
    output_csv: Optional[str] = None,
) -> list[SumOutcomeFinding]:
    print(f"[sum-outcome] fetching events from Polymarket gamma...")
    events = fetch_events()
    print(f"  got {len(events)} events")

    findings: list[SumOutcomeFinding] = []
    for ev in events:
        markets = ev.get("markets") or []
        if len(markets) < min_markets:
            continue
        try:
            ev_vol = float(ev.get("volume24hr") or 0)
        except (TypeError, ValueError):
            ev_vol = 0
        if ev_vol < min_event_volume:
            continue

        # Only true mutually-exclusive outcomes — Polymarket flags these as negRisk.
        # Markets NOT in a negRisk event can co-occur and don't sum to 1.
        if not (ev.get("negRisk") or ev.get("enableNegRisk") or ev.get("negRiskAugmented")):
            continue
        # Each market should be a YES/NO contract.
        # For BUY-MERGE arb: we'd pay ASKS, redeem set for $1 → profit if Σasks < 1.
        # For SPLIT-SELL arb: we'd pay $1 for set, sell at BIDS → profit if Σbids > 1.
        asks = []
        bids = []
        questions = []
        for m in markets:
            try:
                bid = float(m.get("bestBid") or 0)
                ask = float(m.get("bestAsk") or 0)
                if bid <= 0 or ask <= 0 or bid >= ask:
                    continue
                asks.append(ask)
                bids.append(bid)
                questions.append(str(m.get("question") or m.get("groupItemTitle") or "")[:120])
            except (TypeError, ValueError):
                continue
        if len(asks) < min_markets:
            continue

        sum_asks = sum(asks)
        sum_bids = sum(bids)
        # Real arb: buy at asks, merge → profit = 1 - Σasks  (if Σasks < 1)
        # Real arb: split + sell at bids → profit = Σbids - 1  (if Σbids > 1)
        buy_edge = 1.0 - sum_asks
        sell_edge = sum_bids - 1.0
        if buy_edge < min_gross_edge and sell_edge < min_gross_edge:
            continue

        if sell_edge >= buy_edge:
            arb_dir = "SPLIT_AND_SELL"
            gross = sell_edge
            sum_yes = sum_bids
        else:
            arb_dir = "BUY_AND_MERGE"
            gross = buy_edge
            sum_yes = sum_asks
        # Skip outcomes flagged by Polymarket as having "Other" implicit option
        # (heuristic: events with too many low-priced outcomes likely have Other)
        if arb_dir == "BUY_AND_MERGE":
            cheap_count = sum(1 for p in asks if p < 0.05)
            if cheap_count / len(asks) > 0.7:
                continue  # too many trivially-cheap outcomes; likely "Other" not listed
        yes_prices = bids  # for display

        findings.append(SumOutcomeFinding(
            event_slug=str(ev.get("slug", "")),
            event_title=str(ev.get("title", ""))[:200],
            n_markets=len(yes_prices),
            sum_yes=sum_yes,
            arb_direction=arb_dir,
            gross_edge=gross,
            min_market_yes=min(yes_prices),
            max_market_yes=max(yes_prices),
            total_volume_24h=ev_vol,
            market_questions=questions,
            market_yes_prices=yes_prices,
            url=f"https://polymarket.com/event/{ev.get('slug', '')}",
        ))

    findings.sort(key=lambda f: f.gross_edge, reverse=True)
    print(f"\n[sum-outcome] {len(findings)} findings above edge {min_gross_edge:.2%}\n")
    for i, f in enumerate(findings[:20], 1):
        side_label = "Σasks" if f.arb_direction == "BUY_AND_MERGE" else "Σbids"
        print(f"#{i} edge=${f.gross_edge:.3f}  {side_label}={f.sum_yes:.3f}  n={f.n_markets}  vol24h=${f.total_volume_24h:,.0f}")
        print(f"   {f.event_title[:100]}")
        print(f"   {f.arb_direction}  url: {f.url}")
        # Show prices sorted descending
        top = sorted(f.market_yes_prices, reverse=True)[:5]
        print(f"   top yes prices: {[round(p, 3) for p in top]}")

    if output_csv:
        path = Path(output_csv)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ts", "rank", "event_slug", "event_title", "n_markets", "sum_yes", "arb_direction",
                        "gross_edge", "min_yes", "max_yes", "total_volume_24h", "questions", "yes_prices", "url"])
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for i, fnd in enumerate(findings, 1):
                w.writerow([ts, i, fnd.event_slug, fnd.event_title, fnd.n_markets, fnd.sum_yes,
                            fnd.arb_direction, fnd.gross_edge, fnd.min_market_yes, fnd.max_market_yes,
                            fnd.total_volume_24h,
                            " || ".join(fnd.market_questions),
                            " || ".join(f"{p:.4f}" for p in fnd.market_yes_prices),
                            fnd.url])
        print(f"\n[sum-outcome] csv → {path}")

    return findings
