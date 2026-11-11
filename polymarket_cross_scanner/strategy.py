from __future__ import annotations

from itertools import combinations
from typing import Literal
from datetime import datetime, timezone
import json

from .models import ArbSignal, Market, OrderBook
from .utils import utc_now


OrientationSource = Literal["threshold_known", "same_threshold", "price_inferred", "unknown"]


def find_cross_window_arbs(
    markets: list[Market],
    books: dict[str, OrderBook],
    *,
    min_edge: float,
    max_shares: float,
    min_shares: float,
    taker_fee_rate: float,
    min_net_edge: float,
    min_seconds_to_expiry: int,
    max_orderbook_age_seconds: int,
    allow_price_inferred: bool,
    unstable_token_ids: set[str] | None = None,
) -> list[ArbSignal]:
    signals: list[ArbSignal] = []
    unstable_token_ids = unstable_token_ids or set()
    for group in grouped_by_asset_end(markets).values():
        if len(group) < 2:
            continue
        for a, b in combinations(group, 2):
            if min(a.seconds_to_expiry, b.seconds_to_expiry) < min_seconds_to_expiry:
                continue
            for lower, higher, source in oriented_market_pairs(a, b, allow_price_inferred=allow_price_inferred):
                if min(lower.seconds_to_expiry, higher.seconds_to_expiry) < min_seconds_to_expiry:
                    continue
                up_book = books.get(lower.tokens.up)
                down_book = books.get(higher.tokens.down)
                if not up_book or not down_book or up_book.best_ask is None or down_book.best_ask is None:
                    continue
                if lower.tokens.up in unstable_token_ids or higher.tokens.down in unstable_token_ids:
                    continue
                if not is_book_fresh(up_book, max_orderbook_age_seconds) or not is_book_fresh(down_book, max_orderbook_age_seconds):
                    continue
                top_cost = up_book.best_ask + down_book.best_ask
                shares, total_cost, total_fee = executable_pair_size(
                    up_book.asks,
                    down_book.asks,
                    max_cost_per_share=1 - min_edge,
                    max_shares=max_shares,
                    taker_fee_rate=taker_fee_rate,
                )
                if shares < min_shares:
                    continue
                avg_cost = total_cost / shares
                fee_per_share = total_fee / shares
                net_avg_edge = 1 - avg_cost - fee_per_share
                if net_avg_edge < min_net_edge:
                    continue
                signals.append(
                    ArbSignal(
                        detected_at=utc_now(),
                        asset=lower.asset,
                        end_ts=lower.end_ts,
                        lower_market_slug=lower.slug,
                        higher_market_slug=higher.slug,
                        lower_threshold=lower.threshold_price,
                        higher_threshold=higher.threshold_price,
                        long_up_slug=lower.slug,
                        long_down_slug=higher.slug,
                        up_token_id=lower.tokens.up,
                        down_token_id=higher.tokens.down,
                        up_ask=up_book.best_ask,
                        down_ask=down_book.best_ask,
                        top_cost=top_cost,
                        top_edge=1 - top_cost,
                        executable_shares=shares,
                        avg_cost=avg_cost,
                        avg_edge=1 - avg_cost,
                        orientation_source=source,
                        estimated_taker_fee_per_share=fee_per_share,
                        net_avg_edge=net_avg_edge,
                        up_book_depth_shares_1pct=depth_shares_within(up_book.asks, up_book.best_ask + 0.01),
                        down_book_depth_shares_1pct=depth_shares_within(down_book.asks, down_book.best_ask + 0.01),
                        up_book_depth_usdc_1pct=depth_usdc_within(up_book.asks, up_book.best_ask + 0.01),
                        down_book_depth_usdc_1pct=depth_usdc_within(down_book.asks, down_book.best_ask + 0.01),
                        up_ask_levels_json=levels_json(up_book.asks),
                        down_ask_levels_json=levels_json(down_book.asks),
                        notes=notes_for(source),
                    )
                )
    return sorted(signals, key=lambda s: (s.avg_edge, s.executable_shares), reverse=True)


def is_book_fresh(book: OrderBook, max_age_seconds: int) -> bool:
    if max_age_seconds <= 0:
        return True
    try:
        fetched = datetime.fromisoformat(book.fetched_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    age = datetime.now(timezone.utc) - fetched
    return age.total_seconds() <= max_age_seconds


def grouped_by_asset_end(markets: list[Market]) -> dict[tuple[str, int], list[Market]]:
    groups: dict[tuple[str, int], list[Market]] = {}
    for market in markets:
        if not market.is_active_now:
            continue
        groups.setdefault((market.asset, market.end_ts), []).append(market)
    return groups


def orient_markets(a: Market, b: Market) -> tuple[Market, Market, OrientationSource]:
    """Return (lower-threshold market, higher-threshold market, source)."""
    if a.threshold_price is not None and b.threshold_price is not None:
        if a.threshold_price == b.threshold_price:
            return a, b, "unknown"
        return (a, b, "threshold_known") if a.threshold_price < b.threshold_price else (b, a, "threshold_known")
    if a.up_price_hint is not None and b.up_price_hint is not None and a.up_price_hint != b.up_price_hint:
        return (a, b, "price_inferred") if a.up_price_hint > b.up_price_hint else (b, a, "price_inferred")
    return a, b, "unknown"


def oriented_market_pairs(a: Market, b: Market, *, allow_price_inferred: bool) -> list[tuple[Market, Market, OrientationSource]]:
    if a.threshold_price is not None and b.threshold_price is not None and a.threshold_price == b.threshold_price:
        if a.tokens.up == b.tokens.up or a.tokens.down == b.tokens.down:
            return []
        return [(a, b, "same_threshold"), (b, a, "same_threshold")]
    lower, higher, source = orient_markets(a, b)
    if source == "unknown":
        return []
    if source == "price_inferred" and not allow_price_inferred:
        return []
    return [(lower, higher, source)]


def executable_pair_size(asks_a, asks_b, *, max_cost_per_share: float, max_shares: float, taker_fee_rate: float = 0.0) -> tuple[float, float, float]:
    i = j = 0
    shares = 0.0
    total_cost = 0.0
    total_fee = 0.0
    rem_a = asks_a[0].size if asks_a else 0.0
    rem_b = asks_b[0].size if asks_b else 0.0
    while i < len(asks_a) and j < len(asks_b) and shares < max_shares:
        marginal_cost = asks_a[i].price + asks_b[j].price
        if marginal_cost > max_cost_per_share:
            break
        take = min(rem_a, rem_b, max_shares - shares)
        if take <= 0:
            break
        shares += take
        total_cost += take * marginal_cost
        total_fee += take * estimate_taker_fee_per_share(asks_a[i].price, asks_b[j].price, taker_fee_rate)
        rem_a -= take
        rem_b -= take
        if rem_a <= 1e-12:
            i += 1
            if i < len(asks_a):
                rem_a = asks_a[i].size
        if rem_b <= 1e-12:
            j += 1
            if j < len(asks_b):
                rem_b = asks_b[j].size
    return shares, total_cost, total_fee


def estimate_taker_fee_per_share(price_a: float, price_b: float, fee_rate: float) -> float:
    if fee_rate <= 0:
        return 0.0
    return fee_rate * (price_a * (1 - price_a) + price_b * (1 - price_b))


def depth_shares_within(levels, max_price: float) -> float:
    return sum(level.size for level in levels if level.price <= max_price)


def depth_usdc_within(levels, max_price: float) -> float:
    return sum(level.size * level.price for level in levels if level.price <= max_price)


def levels_json(levels, max_levels: int = 10) -> str:
    return json.dumps(
        [{"price": level.price, "size": level.size} for level in levels[:max_levels]],
        separators=(",", ":"),
    )


def notes_for(source: str) -> str:
    if source == "threshold_known":
        return "Deterministic orientation from captured price-to-beat thresholds."
    if source == "same_threshold":
        return "Same price-to-beat duplicate orientation; checks both UP/DOWN directions across aligned markets."
    if source == "price_inferred":
        return "Orientation inferred from market Up prices; good for paper logging, not enough for live execution."
    return ""
