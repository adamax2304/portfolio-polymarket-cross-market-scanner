"""Live oracle divergence tracker.

Polymarket cross-family pairs combine markets that resolve from different oracles:
  - Hourly `*-up-or-down-*` markets resolve from Binance USDT 1H candles.
  - Rolling `*-updown-*` markets resolve from Chainlink USD data streams.

When Binance and Chainlink prices for the same asset diverge materially, a
cross-family pair stops behaving like a true arb: each leg's resolution is read
from a different price source, and the gap between those sources can flip
which side wins independent of the price movement we're betting on.

This tracker exposes a synchronous ``divergence_bps(asset)`` API that returns the
current absolute relative gap between Binance spot and the latest Chainlink tick
(in basis points). It uses a short cache (default 1.5s) so the executor can call
it on every signal without hammering the Binance REST endpoint.

If either side is missing, ``divergence_bps`` returns ``None`` — the executor
treats that as "unknown" and (by config) either skips or proceeds. We default to
proceeding so the bot remains usable while warmup is in progress.
"""

from __future__ import annotations

import time

from .binance import BinanceClient, get_default_client, symbol_for


class OracleDivergenceTracker:
    def __init__(
        self,
        *,
        chainlink_provider,
        binance_client: BinanceClient | None = None,
        cache_seconds: float = 1.5,
    ) -> None:
        # chainlink_provider must expose ``latest_price(asset: str) -> float | None``.
        # In production this is the PriceState used by the scanner; in tests this can
        # be a small adapter.
        self.chainlink = chainlink_provider
        self.binance = binance_client or get_default_client()
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, float | None]] = {}

    def chainlink_price(self, asset: str) -> float | None:
        try:
            # Adapter: PriceState exposes .latest(asset) returning a PriceTick with .price
            getter = getattr(self.chainlink, "latest_price", None)
            if callable(getter):
                return getter(asset)
            getter = getattr(self.chainlink, "latest", None)
            if callable(getter):
                tick = getter(asset)
                if tick is None:
                    return None
                price = getattr(tick, "price", None)
                return float(price) if price is not None else None
        except Exception:
            return None
        return None

    def binance_spot(self, asset: str) -> float | None:
        sym = symbol_for(asset)
        if not sym:
            return None
        cached = self._cache.get(sym)
        now = time.monotonic()
        if cached and now - cached[0] <= self.cache_seconds:
            return cached[1]
        price = self.binance.spot_price(sym)
        self._cache[sym] = (now, price)
        return price

    def divergence_bps(self, asset: str) -> int | None:
        cl = self.chainlink_price(asset)
        bn = self.binance_spot(asset)
        if cl is None or bn is None or cl <= 0 or bn <= 0:
            return None
        gap = abs(bn - cl) / cl
        return int(round(gap * 10_000))

    def snapshot(self, asset: str) -> dict[str, float | int | None]:
        cl = self.chainlink_price(asset)
        bn = self.binance_spot(asset)
        if cl is None or bn is None or cl <= 0 or bn <= 0:
            return {"chainlink": cl, "binance": bn, "divergence_bps": None}
        gap_bps = int(round(abs(bn - cl) / cl * 10_000))
        return {"chainlink": cl, "binance": bn, "divergence_bps": gap_bps}
