from __future__ import annotations

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo
from typing import Any
import logging
import requests
import threading

from .config import ASSET_DISPLAY, ASSET_NAMES, ASSET_SYMBOLS, DURATION_LABELS, ScannerConfig
from .models import Market, TokenPair
from .utils import iso_to_ts, parse_jsonish_list, safe_float

LOGGER = logging.getLogger(__name__)


class GammaClient:
    def __init__(self, cfg: ScannerConfig):
        self.cfg = cfg
        self._local = threading.local()
        self._market_cache: dict[str, Market] = {}

    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": "polymarket-cross-market-scanner/0.1"})
            self._local.session = session
        return session

    def get_market_by_slug(self, slug: str) -> dict[str, Any] | None:
        for params in ({"slug": slug, "limit": 1}, {"slug": slug, "closed": "true", "limit": 1}):
            try:
                resp = self.session().get(
                    f"{self.cfg.gamma_api_base.rstrip('/')}/markets",
                    params=params,
                    timeout=self.cfg.request_timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list) and data:
                    return data[0]
            except Exception as exc:
                LOGGER.debug("Gamma slug lookup failed for %s params=%s: %s", slug, params, exc)
        return None

    def discover_markets(self, now: datetime | None = None) -> list[Market]:
        now = now or datetime.now(timezone.utc)
        markets: dict[str, Market] = {}
        candidates = [
            (asset, slug, duration_sec, fallback_start)
            for asset in self.cfg.assets
            for slug, duration_sec, fallback_start in slug_candidates(asset, self.cfg.durations_sec, now, self.cfg.market_lookup_offsets)
        ]
        workers = max(1, min(self.cfg.gamma_market_workers, len(candidates) or 1))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self.get_market_by_slug, slug): (asset, slug, duration_sec, fallback_start)
                for asset, slug, duration_sec, fallback_start in candidates
            }
            for future in as_completed(futures):
                asset, _slug, duration_sec, fallback_start = futures[future]
                try:
                    raw = future.result()
                except Exception as exc:
                    LOGGER.debug("Gamma market fetch failed for %s: %s", _slug, exc)
                    continue
                if not raw:
                    continue
                market = parse_market(raw, asset, duration_sec, fallback_start)
                if market and market.liquidity >= self.cfg.min_liquidity and market.key not in markets:
                    markets[market.key] = market
        return self._with_market_cache(markets, now)

    def discover_markets_sequential(self, now: datetime | None = None) -> list[Market]:
        now = now or datetime.now(timezone.utc)
        markets: dict[str, Market] = {}
        for asset in self.cfg.assets:
            for slug, duration_sec, fallback_start in slug_candidates(asset, self.cfg.durations_sec, now, self.cfg.market_lookup_offsets):
                raw = self.get_market_by_slug(slug)
                if not raw:
                    continue
                market = parse_market(raw, asset, duration_sec, fallback_start)
                if market and market.liquidity >= self.cfg.min_liquidity and market.key not in markets:
                    markets[market.key] = market
        return self._with_market_cache(markets, now)

    def _with_market_cache(self, fresh: dict[str, Market], now: datetime) -> list[Market]:
        now_ts = int(now.timestamp())
        for key, market in fresh.items():
            self._market_cache[key] = market
        self._market_cache = {
            key: market
            for key, market in self._market_cache.items()
            if market.end_ts + 3600 >= now_ts
        }
        merged = dict(self._market_cache)
        merged.update(fresh)
        return sorted(merged.values(), key=lambda m: (m.asset, m.end_ts, m.duration_sec, m.slug))


def slug_candidates(asset: str, durations_sec: list[int], now: datetime, offsets: int) -> list[tuple[str, int, int]]:
    out: list[tuple[str, int, int]] = []
    now_ts = int(now.timestamp())
    for duration_sec in durations_sec:
        label = DURATION_LABELS.get(duration_sec)
        if not label:
            continue
        if duration_sec in {300, 900, 1800, 14400}:
            bucket = (now_ts // duration_sec) * duration_sec
            for i in range(-1, offsets + 1):
                start = bucket + i * duration_sec
                out.append((f"{asset}-updown-{label}-{start}", duration_sec, start))
        if duration_sec == 3600:
            out.extend(hourly_slug_candidates(asset, now, offsets))
    return out


def hourly_slug_candidates(asset: str, now: datetime, offsets: int) -> list[tuple[str, int, int]]:
    et = ZoneInfo("America/New_York")
    base = now.astimezone(et).replace(minute=0, second=0, microsecond=0)
    asset_name = ASSET_NAMES[asset]
    out: list[tuple[str, int, int]] = []
    for i in range(-1, offsets + 1):
        start_et = base + timedelta(hours=i)
        month = start_et.strftime("%B").lower()
        hour12 = start_et.hour % 12 or 12
        ampm = "am" if start_et.hour < 12 else "pm"
        slug = f"{asset_name}-up-or-down-{month}-{start_et.day}-{start_et.year}-{hour12}{ampm}-et"
        out.append((slug, 3600, int(start_et.astimezone(timezone.utc).timestamp())))
    return out


def parse_market(raw: dict[str, Any], asset: str, duration_sec: int, fallback_start: int) -> Market | None:
    outcomes = [str(x) for x in parse_jsonish_list(raw.get("outcomes"))]
    token_ids = [str(x) for x in parse_jsonish_list(raw.get("clobTokenIds") or raw.get("tokenIds"))]
    if len(outcomes) < 2 or len(token_ids) < 2:
        return None
    token_by_outcome = {outcome.lower(): token_id for outcome, token_id in zip(outcomes, token_ids)}
    up = token_by_outcome.get("up") or token_by_outcome.get("yes") or token_ids[0]
    down = token_by_outcome.get("down") or token_by_outcome.get("no") or token_ids[1]
    start_ts = iso_to_ts(raw.get("eventStartTime")) or fallback_start
    end_ts = iso_to_ts(raw.get("endDate")) or start_ts + duration_sec
    if end_ts <= start_ts:
        end_ts = start_ts + duration_sec
    prices = parse_jsonish_list(raw.get("outcomePrices"))
    up_hint = safe_float(prices[0]) if prices else None
    down_hint = safe_float(prices[1]) if len(prices) > 1 else None
    event_metadata = extract_event_metadata(raw)
    threshold = safe_float(event_metadata.get("priceToBeat"))
    question = raw.get("question") or raw.get("title") or raw.get("slug") or ""
    if ASSET_DISPLAY.get(asset, "").lower() not in question.lower() and ASSET_SYMBOLS.get(asset, "").lower() not in question.lower():
        LOGGER.debug("Skipping %s because title does not match asset %s", raw.get("slug"), asset)
        return None
    market = Market(
        asset=asset,
        duration_sec=duration_sec,
        slug=str(raw.get("slug") or ""),
        question=question,
        condition_id=str(raw.get("conditionId") or raw.get("condition_id") or ""),
        tokens=TokenPair(up=str(up), down=str(down)),
        start_ts=start_ts,
        end_ts=end_ts,
        tick_size=float(raw.get("orderPriceMinTickSize") or raw.get("minimum_tick_size") or 0.01),
        neg_risk=bool(raw.get("negRisk") or raw.get("neg_risk") or False),
        liquidity=float(raw.get("liquidityNum") or raw.get("liquidity") or raw.get("liquidityClob") or 0),
        volume_24h=float(raw.get("volume24hrClob") or raw.get("volume24hr") or 0),
        up_price_hint=up_hint,
        down_price_hint=down_hint,
        raw=raw,
    )
    if threshold is not None:
        market.threshold_price = threshold
        market.threshold_source = "gamma_event_metadata"
    return market


def extract_event_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    metadata = raw.get("eventMetadata")
    if isinstance(metadata, dict):
        return metadata
    events = raw.get("events")
    if isinstance(events, list) and events:
        event_metadata = events[0].get("eventMetadata") if isinstance(events[0], dict) else None
        if isinstance(event_metadata, dict):
            return event_metadata
    return {}
