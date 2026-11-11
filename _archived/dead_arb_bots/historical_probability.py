from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from math import log, sqrt
from pathlib import Path
from typing import Any
import csv
import json
import logging
import statistics
import time

import requests

from .config import ASSET_NAMES
from .probability import MODEL_VERSION, estimate_probability, estimate_single_taker_fee
from .rtds import PriceTick
from .utils import parse_jsonish_list, safe_float, safe_int, utc_now

LOGGER = logging.getLogger(__name__)

BINANCE_SYMBOLS = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
    "doge": "DOGEUSDT",
    "bnb": "BNBUSDT",
}
BINANCE_BASES = ("https://api.binance.com", "https://data-api.binance.vision")


@dataclass(frozen=True)
class HistoricalMarket:
    asset: str
    duration_sec: int
    slug: str
    title: str
    condition_id: str
    up_token_id: str
    down_token_id: str
    start_ts: int
    end_ts: int
    price_to_beat: float
    final_price: float | None
    winning_outcome: str | None


@dataclass
class HistoricalSnapshot:
    observed_ts: int
    observed_utc: str
    model_version: str
    asset: str
    duration_sec: int
    market_slug: str
    condition_id: str
    outcome: str
    token_id: str
    seconds_to_expiry: int
    price_to_beat: float
    final_price: float | None
    winning_outcome: str | None
    polymarket_price: float
    actual_win: bool | None
    pnl_per_share: float | None
    binance_price: float | None
    distance_to_threshold: float | None
    momentum_1m: float | None
    momentum_3m: float | None
    realized_vol_5m: float | None
    fair_prob: float | None
    gross_edge: float | None
    estimated_taker_fee_per_share: float | None
    net_edge: float | None
    source: str


@dataclass
class HistoricalBucketSummary:
    bucket: str
    rows: int
    wins: int
    avg_price: float
    avg_fair_prob: float | None
    pnl_per_share: float
    roi_on_cost: float
    avg_seconds_to_expiry: float


def build_historical_dataset(
    *,
    wallet_export_dir: Path,
    out_dir: Path,
    max_markets: int | None,
    fidelity_minutes: int,
    padding_seconds: int,
    include_binance: bool,
    taker_fee_rate: float,
    sleep_seconds: float,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    markets = load_markets_from_wallet_export(wallet_export_dir / "markets.csv")
    if max_markets:
        markets = markets[:max_markets]
    market_rows_path = out_dir / "historical_markets.csv"
    snapshots_path = out_dir / "historical_snapshots.csv"
    summary_path = out_dir / "historical_summary.csv"
    report_path = out_dir / "historical_report.md"

    write_dict_csv(market_rows_path, [asdict(market) for market in markets])
    binance_cache = fetch_binance_cache(markets) if include_binance else {}

    session = requests.Session()
    session.headers.update({"User-Agent": "polymarket-probability-research/0.1"})
    snapshots: list[HistoricalSnapshot] = []
    errors: list[str] = []
    for index, market in enumerate(markets, start=1):
        for outcome, token_id in (("Up", market.up_token_id), ("Down", market.down_token_id)):
            try:
                history = fetch_polymarket_price_history(
                    session,
                    token_id,
                    start_ts=market.start_ts - padding_seconds,
                    end_ts=market.end_ts + padding_seconds,
                    fidelity_minutes=fidelity_minutes,
                )
            except Exception as exc:
                errors.append(f"{market.slug} {outcome}: {exc}")
                continue
            for point in history:
                snapshot = build_snapshot(market, outcome, token_id, point, binance_cache.get(market.asset, {}), taker_fee_rate)
                if snapshot:
                    snapshots.append(snapshot)
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        if index % 25 == 0:
            LOGGER.info("Historical build: %s/%s markets, %s snapshots", index, len(markets), len(snapshots))

    write_dict_csv(snapshots_path, [asdict(row) for row in snapshots])
    summaries = summarize_snapshots(snapshots)
    write_dict_csv(summary_path, [asdict(row) for row in summaries])
    write_report(report_path, markets, snapshots, summaries, errors)
    meta = {
        "built_at": utc_now(),
        "wallet_export_dir": str(wallet_export_dir),
        "out_dir": str(out_dir),
        "markets": len(markets),
        "snapshots": len(snapshots),
        "summaries": len(summaries),
        "errors": errors[:100],
        "paths": {
            "markets": str(market_rows_path),
            "snapshots": str(snapshots_path),
            "summary": str(summary_path),
            "report": str(report_path),
        },
    }
    (out_dir / "historical_dataset_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def load_markets_from_wallet_export(path: Path) -> list[HistoricalMarket]:
    markets: list[HistoricalMarket] = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw = json.loads(row.get("raw_json") or "{}")
            events = raw.get("events") or []
            event = events[0] if events else {}
            meta = event.get("eventMetadata") or raw.get("eventMetadata") or {}
            price_to_beat = safe_float(meta.get("priceToBeat"))
            if price_to_beat is None:
                continue
            final_price = safe_float(meta.get("finalPrice"))
            start_ts = parse_ts(raw.get("eventStartTime") or event.get("startTime"))
            end_ts = parse_ts(raw.get("endDate") or event.get("endDate") or row.get("end_date"))
            if not start_ts or not end_ts:
                continue
            token_ids = parse_jsonish_list(raw.get("clobTokenIds") or row.get("token_ids"))
            outcomes = [str(x) for x in parse_jsonish_list(raw.get("outcomes") or row.get("outcomes"))]
            if len(token_ids) < 2 or len(outcomes) < 2:
                continue
            token_by_outcome = {outcome.lower(): str(token_id) for outcome, token_id in zip(outcomes, token_ids)}
            asset = infer_asset(str(row.get("slug") or raw.get("slug") or ""))
            if not asset:
                continue
            winning = None
            if final_price is not None:
                winning = "Up" if final_price >= price_to_beat else "Down"
            markets.append(
                HistoricalMarket(
                    asset=asset,
                    duration_sec=end_ts - start_ts,
                    slug=str(row.get("slug") or raw.get("slug") or ""),
                    title=str(row.get("market_title") or raw.get("question") or raw.get("title") or ""),
                    condition_id=str(row.get("condition_id") or raw.get("conditionId") or ""),
                    up_token_id=token_by_outcome.get("up") or str(token_ids[0]),
                    down_token_id=token_by_outcome.get("down") or str(token_ids[1]),
                    start_ts=start_ts,
                    end_ts=end_ts,
                    price_to_beat=price_to_beat,
                    final_price=final_price,
                    winning_outcome=winning,
                )
            )
    return sorted(markets, key=lambda m: (m.start_ts, m.asset, m.duration_sec, m.slug))


def fetch_polymarket_price_history(
    session: requests.Session,
    token_id: str,
    *,
    start_ts: int,
    end_ts: int,
    fidelity_minutes: int,
) -> list[dict[str, Any]]:
    resp = session.get(
        "https://clob.polymarket.com/prices-history",
        params={"market": token_id, "startTs": start_ts, "endTs": end_ts, "fidelity": fidelity_minutes},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    history = data.get("history") if isinstance(data, dict) else []
    return history if isinstance(history, list) else []


def fetch_binance_cache(markets: list[HistoricalMarket]) -> dict[str, dict[int, float]]:
    by_asset: dict[str, tuple[int, int]] = {}
    for market in markets:
        if market.asset not in BINANCE_SYMBOLS:
            continue
        current = by_asset.get(market.asset)
        start = market.start_ts - 900
        end = market.end_ts + 900
        if current:
            by_asset[market.asset] = (min(current[0], start), max(current[1], end))
        else:
            by_asset[market.asset] = (start, end)
    cache: dict[str, dict[int, float]] = {}
    for asset, (start, end) in by_asset.items():
        try:
            cache[asset] = fetch_binance_klines(BINANCE_SYMBOLS[asset], start, end)
        except Exception as exc:
            LOGGER.warning("Binance history unavailable for %s: %s", asset, exc)
            cache[asset] = {}
    return cache


def fetch_binance_klines(symbol: str, start_ts: int, end_ts: int) -> dict[int, float]:
    out: dict[int, float] = {}
    start_ms = start_ts * 1000
    end_ms = end_ts * 1000
    for base in BINANCE_BASES:
        cursor = start_ms
        out.clear()
        try:
            while cursor < end_ms:
                resp = requests.get(
                    f"{base}/api/v3/klines",
                    params={"symbol": symbol, "interval": "1m", "startTime": cursor, "endTime": end_ms, "limit": 1000},
                    timeout=20,
                )
                resp.raise_for_status()
                rows = resp.json()
                if not rows:
                    break
                for row in rows:
                    ts = int(row[0] / 1000)
                    out[ts] = float(row[4])
                cursor = int(rows[-1][0]) + 60_000
                if len(rows) < 1000:
                    break
            if out:
                return dict(sorted(out.items()))
        except Exception:
            continue
    return {}


def build_snapshot(
    market: HistoricalMarket,
    outcome: str,
    token_id: str,
    point: dict[str, Any],
    binance_prices: dict[int, float],
    taker_fee_rate: float,
) -> HistoricalSnapshot | None:
    ts = safe_int(point.get("t"))
    price = safe_float(point.get("p"))
    if not ts or price is None or price <= 0:
        return None
    seconds_to_expiry = market.end_ts - ts
    if seconds_to_expiry <= 0:
        return None
    actual_win = outcome == market.winning_outcome if market.winning_outcome else None
    pnl = (1 - price) if actual_win else (-price if actual_win is False else None)
    external_price = nearest_minute_price(binance_prices, ts)
    distance = (external_price - market.price_to_beat) / market.price_to_beat if external_price else None
    momentum_1m = log_return(binance_prices, ts, 60)
    momentum_3m = log_return(binance_prices, ts, 180)
    vol_5m = realized_vol(binance_prices, ts, 300)
    fair_prob = None
    gross_edge = None
    net_edge = None
    fee = estimate_single_taker_fee(price, taker_fee_rate)
    source = "polymarket_price_history"
    if external_price:
        ticks = price_ticks_from_binance(binance_prices, ts, 300, market.asset)
        estimate = estimate_probability(
            asset=market.asset,
            current_price=external_price,
            threshold_price=market.price_to_beat,
            seconds_to_expiry=seconds_to_expiry,
            ticks=ticks,
        )
        fair_up = estimate.fair_prob_up
        fair_prob = fair_up if outcome == "Up" else 1 - fair_up
        gross_edge = fair_prob - price
        net_edge = gross_edge - fee
        source = "polymarket_price_history+binance_1m"
    return HistoricalSnapshot(
        observed_ts=ts,
        observed_utc=datetime.fromtimestamp(ts, timezone.utc).isoformat(),
        model_version=MODEL_VERSION,
        asset=market.asset,
        duration_sec=market.duration_sec,
        market_slug=market.slug,
        condition_id=market.condition_id,
        outcome=outcome,
        token_id=token_id,
        seconds_to_expiry=seconds_to_expiry,
        price_to_beat=market.price_to_beat,
        final_price=market.final_price,
        winning_outcome=market.winning_outcome,
        polymarket_price=price,
        actual_win=actual_win,
        pnl_per_share=pnl,
        binance_price=external_price,
        distance_to_threshold=distance,
        momentum_1m=momentum_1m,
        momentum_3m=momentum_3m,
        realized_vol_5m=vol_5m,
        fair_prob=fair_prob,
        gross_edge=gross_edge,
        estimated_taker_fee_per_share=fee,
        net_edge=net_edge,
        source=source,
    )


def summarize_snapshots(snapshots: list[HistoricalSnapshot]) -> list[HistoricalBucketSummary]:
    groups: dict[str, list[HistoricalSnapshot]] = {}
    for row in snapshots:
        groups.setdefault(f"{price_bucket(row.polymarket_price)}|{time_bucket(row.seconds_to_expiry)}", []).append(row)
        if row.net_edge is not None:
            groups.setdefault(f"model_edge:{edge_bucket(row.net_edge)}", []).append(row)
    summaries: list[HistoricalBucketSummary] = []
    for bucket, rows in sorted(groups.items()):
        cost = sum(r.polymarket_price for r in rows)
        pnl = sum(r.pnl_per_share or 0.0 for r in rows)
        fair_values = [r.fair_prob for r in rows if r.fair_prob is not None]
        summaries.append(
            HistoricalBucketSummary(
                bucket=bucket,
                rows=len(rows),
                wins=sum(1 for r in rows if r.actual_win),
                avg_price=sum(r.polymarket_price for r in rows) / len(rows),
                avg_fair_prob=sum(fair_values) / len(fair_values) if fair_values else None,
                pnl_per_share=pnl,
                roi_on_cost=pnl / cost if cost > 0 else 0.0,
                avg_seconds_to_expiry=sum(r.seconds_to_expiry for r in rows) / len(rows),
            )
        )
    return summaries


def write_report(path: Path, markets: list[HistoricalMarket], snapshots: list[HistoricalSnapshot], summaries: list[HistoricalBucketSummary], errors: list[str]) -> None:
    top = sorted(summaries, key=lambda x: (x.roi_on_cost, x.rows), reverse=True)[:20]
    signal_rows = [r for r in snapshots if r.net_edge is not None and r.net_edge >= 0.02]
    lines = [
        "# Historical Probability Research",
        "",
        f"Built at: {utc_now()}",
        f"Markets: {len(markets)}",
        f"Snapshots: {len(snapshots)}",
        f"Model edge rows >= 2%: {len(signal_rows)}",
        f"Errors: {len(errors)}",
        "",
        "## Best Buckets",
        "",
        "| bucket | rows | wins | avg_price | avg_fair_prob | roi_on_cost | avg_tte |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in top:
        fair = "" if row.avg_fair_prob is None else f"{row.avg_fair_prob:.4f}"
        lines.append(
            f"| {row.bucket} | {row.rows} | {row.wins} | {row.avg_price:.4f} | {fair} | {row.roi_on_cost:.4f} | {row.avg_seconds_to_expiry:.1f} |"
        )
    lines.extend(
        [
            "",
            "## Caveats",
            "",
            "- Polymarket `/prices-history` is historical token price, not full bid/ask/depth replay.",
            "- Binance 1m data is only a research proxy; live settlement uses Chainlink/RTDS where available.",
            "- Positive historical buckets are hypotheses for paper testing, not live execution approval.",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_dict_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def parse_ts(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return safe_int(value)


def infer_asset(slug: str) -> str | None:
    text = slug.lower()
    for asset, name in ASSET_NAMES.items():
        if text.startswith(f"{asset}-") or text.startswith(f"{name}-"):
            return asset
    if text.startswith("bitcoin-"):
        return "btc"
    if text.startswith("ethereum-"):
        return "eth"
    if text.startswith("solana-"):
        return "sol"
    if text.startswith("dogecoin-"):
        return "doge"
    return None


def nearest_minute_price(prices: dict[int, float], ts: int) -> float | None:
    if not prices:
        return None
    minute = (ts // 60) * 60
    if minute in prices:
        return prices[minute]
    candidates = [t for t in (minute - 60, minute + 60) if t in prices]
    if not candidates:
        return None
    return prices[min(candidates, key=lambda t: abs(t - ts))]


def log_return(prices: dict[int, float], ts: int, lookback_seconds: int) -> float | None:
    current = nearest_minute_price(prices, ts)
    prior = nearest_minute_price(prices, ts - lookback_seconds)
    if not current or not prior or current <= 0 or prior <= 0:
        return None
    return log(current / prior)


def realized_vol(prices: dict[int, float], ts: int, lookback_seconds: int) -> float | None:
    values: list[float] = []
    start = ((ts - lookback_seconds) // 60) * 60
    end = (ts // 60) * 60
    for minute in range(start, end + 1, 60):
        if minute in prices:
            values.append(prices[minute])
    if len(values) < 4:
        return None
    returns = [log(curr / prev) / sqrt(60) for prev, curr in zip(values, values[1:]) if prev > 0 and curr > 0]
    if len(returns) < 3:
        return None
    return statistics.pstdev(returns)


def price_ticks_from_binance(prices: dict[int, float], ts: int, lookback_seconds: int, asset: str) -> list[PriceTick]:
    ticks: list[PriceTick] = []
    start = ((ts - lookback_seconds) // 60) * 60
    end = (ts // 60) * 60
    for minute in range(start, end + 1, 60):
        price = prices.get(minute)
        if price:
            ticks.append(PriceTick(asset=asset, price=price, ts_ms=minute * 1000, received_monotonic=0.0))
    return ticks


def price_bucket(price: float) -> str:
    if price <= 0.01:
        return "px<=0.01"
    if price <= 0.02:
        return "px<=0.02"
    if price <= 0.05:
        return "px<=0.05"
    if price <= 0.10:
        return "px<=0.10"
    if price <= 0.25:
        return "px<=0.25"
    if price <= 0.60:
        return "px<=0.60"
    if price < 0.95:
        return "px<0.95"
    return "px>=0.95"


def time_bucket(seconds_to_expiry: int) -> str:
    if seconds_to_expiry <= 30:
        return "tte<=30s"
    if seconds_to_expiry <= 60:
        return "tte<=60s"
    if seconds_to_expiry <= 120:
        return "tte<=120s"
    if seconds_to_expiry <= 300:
        return "tte<=300s"
    if seconds_to_expiry <= 900:
        return "tte<=900s"
    return "tte>900s"


def edge_bucket(edge: float) -> str:
    if edge < 0:
        return "edge<0"
    if edge < 0.01:
        return "edge<1%"
    if edge < 0.02:
        return "edge<2%"
    if edge < 0.05:
        return "edge<5%"
    if edge < 0.10:
        return "edge<10%"
    return "edge>=10%"
