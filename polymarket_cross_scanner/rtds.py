from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict, deque
from typing import Any
import asyncio
import json
import logging
import time
import threading

import websockets

from .config import ScannerConfig

LOGGER = logging.getLogger(__name__)


@dataclass
class PriceTick:
    asset: str
    price: float
    ts_ms: int
    received_monotonic: float


class PriceState:
    def __init__(self):
        self._prices: dict[str, PriceTick] = {}
        self._history: dict[str, deque[PriceTick]] = defaultdict(lambda: deque(maxlen=20_000))
        self._lock = threading.RLock()

    def update(self, tick: PriceTick) -> None:
        with self._lock:
            self._prices[tick.asset] = tick
            self._history[tick.asset].append(tick)

    def latest(self, asset: str, max_age_seconds: float = 10.0) -> PriceTick | None:
        with self._lock:
            tick = self._prices.get(asset)
        if not tick:
            return None
        if time.monotonic() - tick.received_monotonic > max_age_seconds:
            return None
        return tick

    def recent(self, asset: str, max_age_seconds: float = 300.0) -> list[PriceTick]:
        now = time.monotonic()
        with self._lock:
            return [
                tick
                for tick in self._history.get(asset, [])
                if now - tick.received_monotonic <= max_age_seconds
            ]

    def snapshot_after_start(self, asset: str, start_ts: int, *, max_after_seconds: int = 45, max_before_seconds: int = 10) -> PriceTick | None:
        start_ms = start_ts * 1000
        best_after: PriceTick | None = None
        best_after_delta: int | None = None
        best_before: PriceTick | None = None
        best_before_delta: int | None = None
        with self._lock:
            history = list(self._history.get(asset, []))
        for tick in history:
            delta = tick.ts_ms - start_ms
            if 0 <= delta <= max_after_seconds * 1000:
                if best_after_delta is None or delta < best_after_delta:
                    best_after = tick
                    best_after_delta = delta
            elif -max_before_seconds * 1000 <= delta < 0:
                abs_delta = abs(delta)
                if best_before_delta is None or abs_delta < best_before_delta:
                    best_before = tick
                    best_before_delta = abs_delta
        return best_after or best_before


async def run_rtds_feed(cfg: ScannerConfig, state: PriceState, stop: asyncio.Event) -> None:
    subscribe = {
        "action": "subscribe",
        "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*"}],
    }
    stale_reconnect_seconds = 30.0
    while not stop.is_set():
        try:
            async with websockets.connect(cfg.rtds_ws, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps(subscribe))
                LOGGER.info("RTDS connected")
                last_tick_monotonic = time.monotonic()
                while not stop.is_set():
                    try:
                        text = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        if time.monotonic() - last_tick_monotonic > stale_reconnect_seconds:
                            LOGGER.warning("RTDS stale for %.0fs; reconnecting", time.monotonic() - last_tick_monotonic)
                            break
                        continue
                    ticks = parse_rtds_message(text)
                    if ticks:
                        last_tick_monotonic = time.monotonic()
                    for tick in ticks:
                        if tick.asset in cfg.assets:
                            state.update(tick)
        except Exception as exc:
            LOGGER.warning("RTDS feed error: %s", exc)
            await asyncio.sleep(3)


def parse_rtds_message(text: str | bytes) -> list[PriceTick]:
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="ignore")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return []
    candidates: list[Any] = []
    payload = value.get("payload") if isinstance(value, dict) else None
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            candidates.extend(data)
        candidates.append(payload)
    if isinstance(value, dict):
        candidates.append(value)
    ticks: list[PriceTick] = []
    fallback_symbol = payload.get("symbol") if isinstance(payload, dict) else None
    for item in candidates:
        parsed = parse_price_payload(item, fallback_symbol)
        if parsed:
            ticks.append(parsed)
    return ticks


def parse_price_payload(payload: Any, fallback_symbol: str | None = None) -> PriceTick | None:
    if not isinstance(payload, dict):
        return None
    symbol = payload.get("symbol") or payload.get("asset") or payload.get("asset_id") or fallback_symbol
    asset = normalize_asset(str(symbol or ""))
    if not asset:
        return None
    price = parse_value(payload)
    ts_ms = parse_ts_ms(payload)
    if price is None or ts_ms is None:
        return None
    return PriceTick(asset=asset, price=price, ts_ms=ts_ms, received_monotonic=time.monotonic())


def normalize_asset(symbol: str) -> str | None:
    s = "".join(c for c in symbol.lower() if c.isalnum())
    if s.startswith("btc") or s == "xbtusd":
        return "btc"
    if s.startswith("eth"):
        return "eth"
    if s.startswith("sol"):
        return "sol"
    if s.startswith("xrp"):
        return "xrp"
    if s.startswith("doge"):
        return "doge"
    if s.startswith("bnb"):
        return "bnb"
    return None


def parse_value(payload: dict[str, Any]) -> float | None:
    for key in ("value", "price", "answer", "mid", "close"):
        if key in payload:
            try:
                return float(payload[key])
            except (TypeError, ValueError):
                pass
    return None


def parse_ts_ms(payload: dict[str, Any]) -> int | None:
    raw = None
    for key in ("timestamp", "ts", "time", "updated_at"):
        if key in payload:
            raw = payload[key]
            break
    if raw is None:
        return None
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return None
    if value >= 1_000_000_000_000_000_000:
        return value // 1_000_000
    if value >= 1_000_000_000_000_000:
        return value // 1_000
    if value >= 1_000_000_000_000:
        return value
    if value >= 1_000_000_000:
        return value * 1000
    return None
