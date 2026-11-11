from __future__ import annotations

from pathlib import Path
from typing import Any
import json

from .models import Market
from .utils import utc_now


class ThresholdStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def save(self) -> None:
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")

    def reload(self) -> None:
        self._data = self._load()

    def get(self, market: Market) -> float | None:
        row = self._data.get(market.key) or self._data.get(market.slug)
        if not row:
            return None
        # Hourly markets resolve from Binance USDT 1H close. Prefer that threshold when
        # it has been captured; fall back to the Chainlink RTDS capture if not (so the
        # bot remains usable while Binance capture warms up).
        if is_hourly_slug(market.slug):
            binance_value = row.get("binance_threshold_price")
            if binance_value is not None:
                try:
                    return float(binance_value)
                except (TypeError, ValueError):
                    pass
        value = row.get("threshold_price")
        return float(value) if value is not None else None

    def source(self, market: Market) -> str:
        row = self._data.get(market.key) or self._data.get(market.slug) or {}
        if is_hourly_slug(market.slug) and row.get("binance_threshold_price") is not None:
            return str(row.get("binance_source") or "binance_usdt_hourly_open")
        return str(row.get("source") or "unknown")

    def has_binance_threshold(self, market: Market) -> bool:
        row = self._data.get(market.key) or self._data.get(market.slug) or {}
        return row.get("binance_threshold_price") is not None

    def set_threshold(self, market: Market, threshold_price: float, source: str, *, tick_ts_ms: int | None = None) -> None:
        delta_ms = tick_ts_ms - market.start_ts * 1000 if tick_ts_ms is not None else None
        existing = dict(self._data.get(market.key) or self._data.get(market.slug) or {})
        existing.update(
            {
                "asset": market.asset,
                "slug": market.slug,
                "condition_id": market.condition_id,
                "start_ts": market.start_ts,
                "end_ts": market.end_ts,
                "duration_sec": market.duration_sec,
                "threshold_price": float(threshold_price),
                "source": source,
                "tick_ts_ms": tick_ts_ms,
                "delta_ms": delta_ms,
                "captured_at": utc_now(),
            }
        )
        self._data[market.key] = existing
        self._data[market.slug] = existing
        self.save()

    def set_binance_threshold(self, market: Market, threshold_price: float, *, source: str = "binance_usdt_hourly_open") -> None:
        existing = dict(self._data.get(market.key) or self._data.get(market.slug) or {})
        existing.update(
            {
                "asset": market.asset,
                "slug": market.slug,
                "condition_id": market.condition_id,
                "start_ts": market.start_ts,
                "end_ts": market.end_ts,
                "duration_sec": market.duration_sec,
                "binance_threshold_price": float(threshold_price),
                "binance_source": source,
                "binance_captured_at": utc_now(),
            }
        )
        self._data[market.key] = existing
        self._data[market.slug] = existing
        self.save()

    def apply(self, markets: list[Market]) -> None:
        for market in markets:
            if market.threshold_price is not None and market.threshold_source == "gamma_event_metadata":
                continue
            threshold = self.get(market)
            if threshold is not None:
                market.threshold_price = threshold
                market.threshold_source = self.source(market)


def is_hourly_slug(slug: str) -> bool:
    """Hourly markets follow the human-readable `*-up-or-down-*` pattern; rolling
    updown markets use the `*-updown-*` pattern. The hourly bucket is the one that
    resolves from Binance USDT 1H candles (the rolling bucket resolves from Chainlink)."""

    if not slug:
        return False
    return "-up-or-down-" in slug
