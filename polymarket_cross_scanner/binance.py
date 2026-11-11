"""Minimal Binance REST client for hourly threshold capture and live spot price.

Polymarket "Up or Down" hourly markets (`*-up-or-down-*`) resolve from Binance
USDT 1H candles. The threshold is the open price of the candle containing the
market's start_ts. Our existing RTDS feed gives Chainlink USD prices instead, so
cross-family pairs (hourly vs rolling updown) were using the wrong oracle.

This module exposes:
  - ``ASSET_TO_SYMBOL`` — asset code → Binance symbol mapping
  - ``hourly_open_at`` — fetch the 1H open for a given asset and hour boundary
  - ``spot_price`` — fetch the current spot price for a symbol
  - ``BinanceClient`` — small client with timeout and caching

All calls are best-effort and return ``None`` on any failure rather than raising.
The caller decides what to do without the data (skip pair, etc).
"""

from __future__ import annotations

import os
import time
from typing import Any
import urllib.request
import urllib.parse
import json


ASSET_TO_SYMBOL: dict[str, str] = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
    "doge": "DOGEUSDT",
    "bnb": "BNBUSDT",
}


def symbol_for(asset: str) -> str | None:
    return ASSET_TO_SYMBOL.get(asset.lower())


class BinanceClient:
    def __init__(
        self,
        *,
        api_base: str | None = None,
        timeout: float = 5.0,
        spot_cache_seconds: float = 1.5,
    ) -> None:
        self.api_base = (api_base or os.getenv("BINANCE_API_BASE") or "https://api.binance.com").rstrip("/")
        self.timeout = timeout
        self.spot_cache_seconds = spot_cache_seconds
        self._spot_cache: dict[str, tuple[float, float]] = {}

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        url = f"{self.api_base}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "polymarket-cross-scanner/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
            return json.loads(payload.decode("utf-8"))
        except Exception:
            return None

    def hourly_open_at(self, symbol: str, hour_boundary_ts: int) -> float | None:
        """Return the OPEN price of the 1H candle whose open matches ``hour_boundary_ts``.

        ``hour_boundary_ts`` is a unix timestamp in seconds at an exact hour boundary.
        Binance treats kline timestamps as UTC milliseconds.
        """

        start_ms = int(hour_boundary_ts) * 1000
        data = self._get(
            "/api/v3/klines",
            {
                "symbol": symbol,
                "interval": "1h",
                "startTime": start_ms,
                "limit": 1,
            },
        )
        if not isinstance(data, list) or not data:
            return None
        candle = data[0]
        if not isinstance(candle, list) or len(candle) < 2:
            return None
        try:
            return float(candle[1])
        except (TypeError, ValueError):
            return None

    def spot_price(self, symbol: str) -> float | None:
        cached = self._spot_cache.get(symbol)
        now = time.monotonic()
        if cached and now - cached[0] <= self.spot_cache_seconds:
            return cached[1]
        data = self._get("/api/v3/ticker/price", {"symbol": symbol})
        if not isinstance(data, dict):
            return None
        try:
            price = float(data.get("price"))
        except (TypeError, ValueError):
            return None
        self._spot_cache[symbol] = (now, price)
        return price


_DEFAULT_CLIENT: BinanceClient | None = None


def get_default_client() -> BinanceClient:
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = BinanceClient()
    return _DEFAULT_CLIENT


def hourly_open_at(asset: str, hour_boundary_ts: int, *, client: BinanceClient | None = None) -> float | None:
    sym = symbol_for(asset)
    if not sym:
        return None
    client = client or get_default_client()
    return client.hourly_open_at(sym, hour_boundary_ts)


def spot_price(asset: str, *, client: BinanceClient | None = None) -> float | None:
    sym = symbol_for(asset)
    if not sym:
        return None
    client = client or get_default_client()
    return client.spot_price(sym)
