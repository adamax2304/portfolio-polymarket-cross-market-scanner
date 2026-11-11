from polymarket_cross_scanner.models import BookLevel, Market, OrderBook, TokenPair
from polymarket_cross_scanner.strategy import executable_pair_size, find_cross_window_arbs, orient_markets, oriented_market_pairs


def market(slug, threshold, up_hint=None):
    return Market(
        asset="btc",
        duration_sec=300,
        slug=slug,
        question=slug,
        condition_id=slug,
        tokens=TokenPair(up=slug + "-up", down=slug + "-down"),
        start_ts=1,
        end_ts=4_102_444_800,
        threshold_price=threshold,
        threshold_source="test" if threshold is not None else "unknown",
        up_price_hint=up_hint,
    )


def book(token, ask):
    return OrderBook(token_id=token, bids=[], asks=[BookLevel(price=ask, size=100)], fetched_at="now")


def test_orient_markets_by_threshold():
    low = market("low", 100.0)
    high = market("high", 101.0)
    assert orient_markets(high, low) == (low, high, "threshold_known")


def test_orient_markets_by_up_price_hint():
    likely_low = market("likely-low", None, up_hint=0.65)
    likely_high = market("likely-high", None, up_hint=0.35)
    assert orient_markets(likely_high, likely_low) == (likely_low, likely_high, "price_inferred")


def test_oriented_market_pairs_equal_threshold_checks_both_directions():
    a = market("a", 100.0)
    b = market("b", 100.0)

    pairs = oriented_market_pairs(a, b, allow_price_inferred=False)

    assert [(low.slug, high.slug, source) for low, high, source in pairs] == [
        ("a", "b", "same_threshold"),
        ("b", "a", "same_threshold"),
    ]


def test_executable_pair_size_walks_depth():
    asks_a = [BookLevel(0.45, 10), BookLevel(0.48, 10)]
    asks_b = [BookLevel(0.50, 15), BookLevel(0.55, 10)]
    shares, cost, fee = executable_pair_size(asks_a, asks_b, max_cost_per_share=0.98, max_shares=25)
    assert shares == 15
    assert round(cost, 6) == 14.4
    assert fee == 0


def test_find_cross_window_arb_threshold_known():
    low = market("low", 100.0)
    high = market("high", 101.0)
    books = {
        low.tokens.up: book(low.tokens.up, 0.45),
        high.tokens.down: book(high.tokens.down, 0.50),
    }
    signals = find_cross_window_arbs(
        [low, high],
        books,
        min_edge=0.02,
        max_shares=25,
        min_shares=5,
        taker_fee_rate=0,
        min_net_edge=0,
        min_seconds_to_expiry=0,
        max_orderbook_age_seconds=0,
        allow_price_inferred=True,
    )
    assert len(signals) == 1
    assert round(signals[0].avg_edge, 6) == 0.05
    assert signals[0].long_up_slug == "low"
    assert signals[0].long_down_slug == "high"
    assert signals[0].up_book_depth_shares_1pct == 100
    assert '"price":0.45' in signals[0].up_ask_levels_json


def test_find_cross_window_arb_same_threshold_duplicate_both_directions():
    a = market("a", 100.0)
    b = market("b", 100.0)
    books = {
        a.tokens.up: book(a.tokens.up, 0.40),
        b.tokens.down: book(b.tokens.down, 0.55),
        b.tokens.up: book(b.tokens.up, 0.30),
        a.tokens.down: book(a.tokens.down, 0.60),
    }

    signals = find_cross_window_arbs(
        [a, b],
        books,
        min_edge=0.02,
        max_shares=25,
        min_shares=5,
        taker_fee_rate=0,
        min_net_edge=0,
        min_seconds_to_expiry=0,
        max_orderbook_age_seconds=0,
        allow_price_inferred=False,
    )

    assert len(signals) == 2
    assert {signal.orientation_source for signal in signals} == {"same_threshold"}
    assert {signal.signal_id for signal in signals} == {
        "btc:4102444800:a:b:same_threshold",
        "btc:4102444800:b:a:same_threshold",
    }


def test_fee_estimate_reduces_net_edge():
    low = market("low", 100.0)
    high = market("high", 101.0)
    books = {
        low.tokens.up: book(low.tokens.up, 0.45),
        high.tokens.down: book(high.tokens.down, 0.50),
    }
    signals = find_cross_window_arbs(
        [low, high],
        books,
        min_edge=0.01,
        max_shares=25,
        min_shares=5,
        taker_fee_rate=0.07,
        min_net_edge=0,
        min_seconds_to_expiry=0,
        max_orderbook_age_seconds=0,
        allow_price_inferred=True,
    )
    assert signals[0].estimated_taker_fee_per_share > 0
    assert signals[0].net_avg_edge < signals[0].avg_edge


def test_find_cross_window_filters_low_net_edge():
    low = market("low", 100.0)
    high = market("high", 101.0)
    books = {
        low.tokens.up: book(low.tokens.up, 0.45),
        high.tokens.down: book(high.tokens.down, 0.50),
    }
    signals = find_cross_window_arbs(
        [low, high],
        books,
        min_edge=0.01,
        max_shares=25,
        min_shares=5,
        taker_fee_rate=0.07,
        min_net_edge=0.10,
        min_seconds_to_expiry=0,
        max_orderbook_age_seconds=0,
        allow_price_inferred=True,
    )
    assert signals == []


def test_find_cross_window_filters_unstable_tokens():
    low = market("low", 100.0)
    high = market("high", 101.0)
    books = {
        low.tokens.up: book(low.tokens.up, 0.45),
        high.tokens.down: book(high.tokens.down, 0.50),
    }
    signals = find_cross_window_arbs(
        [low, high],
        books,
        min_edge=0.01,
        max_shares=25,
        min_shares=5,
        taker_fee_rate=0,
        min_net_edge=0,
        min_seconds_to_expiry=0,
        max_orderbook_age_seconds=0,
        allow_price_inferred=True,
        unstable_token_ids={low.tokens.up},
    )
    assert signals == []
