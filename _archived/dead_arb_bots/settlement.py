from __future__ import annotations

from typing import Any
import logging
import sqlite3
import time

from .gamma import GammaClient
from .models import PaperSettlement
from .utils import safe_float, utc_now

LOGGER = logging.getLogger(__name__)


class PaperSettler:
    def __init__(self, gamma: GammaClient):
        self.gamma = gamma
        self._market_cache: dict[str, dict[str, Any] | None] = {}

    def settle_records(self, records: list[sqlite3.Row]) -> list[PaperSettlement]:
        settlements: list[PaperSettlement] = []
        for record in records:
            try:
                settlements.append(self.settle_record(record))
            except Exception as exc:
                LOGGER.warning("Paper settlement failed for %s: %s", record["signal_id"], exc)
                settlements.append(error_settlement(record, str(exc)))
        return settlements

    def settle_record(self, record: sqlite3.Row) -> PaperSettlement:
        lower = self.market_by_slug(record["lower_market_slug"])
        higher = self.market_by_slug(record["higher_market_slug"])
        lower_final = final_price(lower)
        higher_final = final_price(higher)
        lower_threshold = price_to_beat(lower) or safe_float(record["lower_threshold"])
        higher_threshold = price_to_beat(higher) or safe_float(record["higher_threshold"])
        if lower_final is None or lower_threshold is None:
            lower = self.market_by_slug(record["lower_market_slug"], refresh=True)
            lower_final = final_price(lower)
            lower_threshold = price_to_beat(lower) or safe_float(record["lower_threshold"])
        if higher_final is None or higher_threshold is None:
            higher = self.market_by_slug(record["higher_market_slug"], refresh=True)
            higher_final = final_price(higher)
            higher_threshold = price_to_beat(higher) or safe_float(record["higher_threshold"])
        shares = float(record["shares"])
        cost = float(record["cost"])
        locked = float(record["locked_profit_estimate"])
        if lower_final is None or higher_final is None or lower_threshold is None or higher_threshold is None:
            return PaperSettlement(
                signal_id=str(record["signal_id"]),
                settled_at=utc_now(),
                asset=str(record["asset"]),
                end_ts=int(record["end_ts"]),
                shares=shares,
                cost=cost,
                lower_market_slug=str(record["lower_market_slug"]),
                higher_market_slug=str(record["higher_market_slug"]),
                lower_threshold=lower_threshold,
                higher_threshold=higher_threshold,
                lower_final_price=lower_final,
                higher_final_price=higher_final,
                up_leg_won=None,
                down_leg_won=None,
                payout=None,
                realized_profit=None,
                locked_profit_estimate=locked,
                status="SETTLEMENT_PENDING",
                notes="Final price or threshold is not available yet from Gamma metadata.",
            )
        up_leg_won = lower_final >= lower_threshold
        down_leg_won = higher_final < higher_threshold
        payout = shares * int(up_leg_won) + shares * int(down_leg_won)
        realized = payout - cost
        return PaperSettlement(
            signal_id=str(record["signal_id"]),
            settled_at=utc_now(),
            asset=str(record["asset"]),
            end_ts=int(record["end_ts"]),
            shares=shares,
            cost=cost,
            lower_market_slug=str(record["lower_market_slug"]),
            higher_market_slug=str(record["higher_market_slug"]),
            lower_threshold=lower_threshold,
            higher_threshold=higher_threshold,
            lower_final_price=lower_final,
            higher_final_price=higher_final,
            up_leg_won=up_leg_won,
            down_leg_won=down_leg_won,
            payout=payout,
            realized_profit=realized,
            locked_profit_estimate=locked,
            status="SETTLED",
            notes="Actual paper payout computed from Gamma finalPrice and official priceToBeat when available.",
        )

    def market_by_slug(self, slug: str, *, refresh: bool = False) -> dict[str, Any] | None:
        if refresh or slug not in self._market_cache:
            market = None
            for attempt in range(4):
                market = self.gamma.get_market_by_slug(slug)
                if market:
                    break
                time.sleep(0.25 * (attempt + 1))
            self._market_cache[slug] = market
        return self._market_cache[slug]


def final_price(market: dict[str, Any] | None) -> float | None:
    return event_metadata_value(market, "finalPrice")


def price_to_beat(market: dict[str, Any] | None) -> float | None:
    return event_metadata_value(market, "priceToBeat")


def event_metadata_value(market: dict[str, Any] | None, key: str) -> float | None:
    if not market:
        return None
    metadata = market.get("eventMetadata")
    if isinstance(metadata, dict):
        value = safe_float(metadata.get(key))
        if value is not None:
            return value
    events = market.get("events")
    if isinstance(events, list) and events:
        event = events[0]
        if isinstance(event, dict):
            event_metadata = event.get("eventMetadata")
            if isinstance(event_metadata, dict):
                value = safe_float(event_metadata.get(key))
                if value is not None:
                    return value
    return None


def error_settlement(record: sqlite3.Row, message: str) -> PaperSettlement:
    return PaperSettlement(
        signal_id=str(record["signal_id"]),
        settled_at=utc_now(),
        asset=str(record["asset"]),
        end_ts=int(record["end_ts"] or 0),
        shares=float(record["shares"] or 0),
        cost=float(record["cost"] or 0),
        lower_market_slug=str(record["lower_market_slug"] or ""),
        higher_market_slug=str(record["higher_market_slug"] or ""),
        lower_threshold=safe_float(record["lower_threshold"]),
        higher_threshold=safe_float(record["higher_threshold"]),
        lower_final_price=None,
        higher_final_price=None,
        up_leg_won=None,
        down_leg_won=None,
        payout=None,
        realized_profit=None,
        locked_profit_estimate=float(record["locked_profit_estimate"] or 0),
        status="SETTLEMENT_ERROR",
        notes=message,
    )
