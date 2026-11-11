from polymarket_cross_scanner.config import ScannerConfig
from polymarket_cross_scanner.execution_sim import (
    LOCKED_ACTIONS,
    CrossMarketExecutionSimulator,
    choose_dynamic_shares,
    simulate_fok_buy,
    simulate_fok_sell,
)
from polymarket_cross_scanner.models import ArbSignal, BookLevel, OrderBook


class FakeClob:
    def __init__(self, books):
        self.books = books

    def get_order_book(self, token_id):
        return self.books[token_id]


def book(token, asks, bids=None):
    return OrderBook(
        token_id=token,
        asks=[BookLevel(price, size) for price, size in asks],
        bids=[BookLevel(price, size) for price, size in (bids or [])],
        fetched_at="2026-06-02T00:00:00+00:00",
    )


def signal():
    return ArbSignal(
        detected_at="2026-06-02T00:00:00+00:00",
        asset="btc",
        end_ts=123,
        lower_market_slug="low",
        higher_market_slug="high",
        lower_threshold=100,
        higher_threshold=101,
        long_up_slug="low",
        long_down_slug="high",
        up_token_id="up",
        down_token_id="down",
        up_ask=0.45,
        down_ask=0.50,
        top_cost=0.95,
        top_edge=0.05,
        executable_shares=10,
        avg_cost=0.95,
        avg_edge=0.05,
        orientation_source="threshold_known",
        up_book_depth_shares_1pct=10,
        down_book_depth_shares_1pct=100,
    )


def test_fok_buy_requires_full_size_within_limit():
    assert simulate_fok_buy([BookLevel(0.45, 5)], 10, 0.46).filled is False
    fill = simulate_fok_buy([BookLevel(0.45, 5), BookLevel(0.46, 5)], 10, 0.46)
    assert fill.filled is True
    assert round(fill.avg_price, 6) == 0.455


def test_fok_sell_uses_bid_ladder():
    fill = simulate_fok_sell([BookLevel(0.47, 5), BookLevel(0.46, 5)], 10)
    assert fill.filled is True
    assert round(fill.avg_price, 6) == 0.465


def test_execution_sim_strict_both_fill(tmp_path):
    cfg = ScannerConfig(root=tmp_path, cross_execution_refetch_delay_ms=0)
    books = {
        "up": book("up", asks=[(0.45, 10)], bids=[(0.44, 10)]),
        "down": book("down", asks=[(0.50, 10)], bids=[(0.49, 10)]),
    }
    sim = CrossMarketExecutionSimulator(cfg, FakeClob(books)).simulate(signal(), books)
    assert sim.final_action == "BOTH_FILL_SIMULTANEOUS"
    assert round(sim.final_pair_edge, 6) == 0.05


def test_execution_sim_unwinds_profitable_first_leg_before_rescue(tmp_path):
    cfg = ScannerConfig(
        root=tmp_path,
        cross_execution_refetch_delay_ms=0,
        cross_execution_simultaneous_enabled=False,
        cross_execution_dynamic_sizing_enabled=False,
    )
    books = {
        "up": book("up", asks=[(0.45, 10)], bids=[(0.47, 10)]),
        "down": book("down", asks=[(0.99, 10)], bids=[(0.40, 10)]),
    }
    sim = CrossMarketExecutionSimulator(cfg, FakeClob(books)).simulate(signal(), books)
    assert sim.final_action == "UNWIND_PROFIT"
    assert sim.unwind_pnl > 0


def test_execution_sim_uses_rescue_when_unwind_not_profitable(tmp_path):
    cfg = ScannerConfig(
        root=tmp_path,
        cross_execution_refetch_delay_ms=0,
        cross_execution_rescue_extra_cost=0.05,
        cross_execution_simultaneous_enabled=False,
        cross_execution_dynamic_sizing_enabled=False,
    )
    books = {
        "up": book("up", asks=[(0.45, 10)], bids=[(0.43, 10)]),
        "down": book("down", asks=[(0.56, 10)], bids=[(0.40, 10)]),
    }
    sim = CrossMarketExecutionSimulator(cfg, FakeClob(books)).simulate(signal(), books)
    assert sim.final_action == "HEDGE_RESCUE"
    assert round(sim.final_pair_edge, 6) == -0.01
    assert "HEDGE_RESCUE" not in LOCKED_ACTIONS


def test_dynamic_sizing_picks_smaller_fillable_pair():
    up = [BookLevel(0.45, 10), BookLevel(0.80, 20)]
    down = [BookLevel(0.50, 25)]
    assert choose_dynamic_shares(25, min_shares=5, step=5, up_asks=up, down_asks=down, max_pair_cost=0.99) == 10
