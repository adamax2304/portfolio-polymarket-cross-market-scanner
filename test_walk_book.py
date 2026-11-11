#!/usr/bin/env python3
"""Sanity tests for EventBookAggregator.walk_book_profit (walk-the-book sim).

Fee model: per-leg price-weighted, fee_per_share = fee_rate * price^2 * (1-price).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from polymarket_cross_scanner.ws_arb.book_state import EventBookAggregator


def make_agg(books_spec):
    agg = EventBookAggregator("test", [str(i) for i in range(len(books_spec))])
    for i, (bids, asks) in enumerate(books_spec):
        tb = agg.books[str(i)]
        tb.bids_full = dict(bids)
        tb.asks_full = dict(asks)
        tb._recompute_tops()
        tb.update_count = 1
    return agg


def approx(a, b, tol=1e-4):
    return abs(a - b) < tol


def leg_fee(p, r=0.06):
    return r * p * p * (1.0 - p) if 0 < p < 1 else 0.0


def test_split_simple_nofee():
    agg = make_agg([
        ({0.55: 100.0}, {0.60: 100.0}),
        ({0.50: 100.0}, {0.55: 100.0}),
    ])
    units, profit, cost = agg.walk_book_profit("SPLIT_AND_SELL", fee_rate=0.0, capital=1e9)
    assert approx(units, 100.0), units
    assert approx(profit, 5.0), profit
    assert approx(cost, 100.0), cost
    print(f"test_split_simple_nofee OK: units={units} profit={profit}")


def test_split_two_levels_nofee():
    agg = make_agg([
        ({0.60: 50.0, 0.52: 100.0}, {0.7: 10}),
        ({0.50: 50.0, 0.48: 100.0}, {0.7: 10}),
    ])
    units, profit, cost = agg.walk_book_profit("SPLIT_AND_SELL", fee_rate=0.0, capital=1e9)
    assert approx(units, 50.0), units
    assert approx(profit, 5.0), profit
    print(f"test_split_two_levels_nofee OK: units={units} profit={profit}")


def test_split_capital_bound():
    agg = make_agg([
        ({0.55: 1000.0}, {0.6: 10}),
        ({0.50: 1000.0}, {0.6: 10}),
    ])
    units, profit, cost = agg.walk_book_profit("SPLIT_AND_SELL", fee_rate=0.0, capital=30.0)
    assert approx(units, 30.0), units
    assert approx(profit, 1.5), profit
    assert approx(cost, 30.0), cost
    print(f"test_split_capital_bound OK: units={units} profit={profit}")


def test_no_arb():
    agg = make_agg([
        ({0.49: 100.0}, {0.55: 100}),
        ({0.49: 100.0}, {0.55: 100}),
    ])
    units, profit, _ = agg.walk_book_profit("SPLIT_AND_SELL", fee_rate=0.0, capital=1e9)
    assert approx(units, 0.0), units
    assert approx(profit, 0.0), profit
    print(f"test_no_arb OK: units={units} profit={profit}")


def test_buy_merge_nofee():
    agg = make_agg([
        ({0.40: 10}, {0.45: 100.0}),
        ({0.40: 10}, {0.48: 100.0}),
    ])
    units, profit, cost = agg.walk_book_profit("BUY_AND_MERGE", fee_rate=0.0, capital=1e9)
    assert approx(units, 100.0), units
    assert approx(profit, 7.0), profit
    assert approx(cost, 93.0), cost
    print(f"test_buy_merge_nofee OK: units={units} profit={profit} cost={cost}")


def test_fee_kills_thin_edge():
    # Σbid=1.005, price-weighted fee at ~0.5 is ~0.0075/leg -> ~0.015 total > 0.005 edge
    agg = make_agg([
        ({0.505: 100.0}, {0.6: 10}),
        ({0.500: 100.0}, {0.6: 10}),
    ])
    units, profit, _ = agg.walk_book_profit("SPLIT_AND_SELL", fee_rate=0.06, capital=1e9)
    assert approx(units, 0.0), (units, profit)
    print(f"test_fee_kills_thin_edge OK: units={units} profit={profit}")


def test_cheap_tokens_low_fee():
    # KEY REALISM TEST: 17 cheap (~$0.06) tokens, Σ=1.02. Price-weighted fee is
    # tiny on cheap legs, so a 2% gross edge survives to ~1.65% net.
    books = []
    for _ in range(17):
        books.append(({0.06: 20.0, 0.05: 500.0}, {0.08: 50}))
    agg = make_agg(books)
    units, profit, _ = agg.walk_book_profit("SPLIT_AND_SELL", fee_rate=0.06, capital=1e9)
    # seg1 (0..20): sum_p=1.02, fee=17*leg_fee(0.06)=17*0.000203=0.00345
    #   marg=1.02-0.00345-1=0.01655 ; *20 = 0.3310
    # seg2: sum_p=0.85 <1 -> stop
    expect_fee = 17 * leg_fee(0.06)
    expect_marg = 1.02 - expect_fee - 1.0
    assert approx(units, 20.0), units
    assert approx(profit, expect_marg * 20, tol=1e-3), (profit, expect_marg*20)
    # Sanity: net edge should be > 1.5% (fee barely dents it)
    assert profit / 20 > 0.015, profit/20
    print(f"test_cheap_tokens_low_fee OK: units={units} profit={profit:.4f} "
          f"net_edge/unit={profit/20:.4f} (fee/unit={expect_fee:.5f})")


def test_expensive_tokens_high_fee():
    # Contrast: 2 tokens at ~$0.50 each, Σbid=1.02. Fee at p=0.5 is heavy.
    agg = make_agg([
        ({0.51: 100.0}, {0.6: 10}),
        ({0.51: 100.0}, {0.6: 10}),
    ])
    units, profit, _ = agg.walk_book_profit("SPLIT_AND_SELL", fee_rate=0.06, capital=1e9)
    # sum_p=1.02, fee=2*leg_fee(0.51)=2*0.06*0.51^2*0.49=2*0.00765=0.0153
    # marg=1.02-0.0153-1=0.0047 ; *100 = 0.47
    fee = 2 * leg_fee(0.51)
    expect = (1.02 - fee - 1.0) * 100
    assert approx(profit, expect, tol=1e-2), (profit, expect)
    print(f"test_expensive_tokens_high_fee OK: profit={profit:.3f} "
          f"(fee/unit={fee:.5f} vs cheap 0.00345) -> expensive legs pay ~4x more")


def test_empty_book():
    agg = make_agg([
        ({0.60: 100.0}, {0.7: 10}),
        ({}, {0.7: 10}),
    ])
    units, _, _ = agg.walk_book_profit("SPLIT_AND_SELL", fee_rate=0.0, capital=1e9)
    assert approx(units, 0.0), units
    print(f"test_empty_book OK: units={units}")


if __name__ == "__main__":
    test_split_simple_nofee()
    test_split_two_levels_nofee()
    test_split_capital_bound()
    test_no_arb()
    test_buy_merge_nofee()
    test_fee_kills_thin_edge()
    test_cheap_tokens_low_fee()
    test_expensive_tokens_high_fee()
    test_empty_book()
    print("\nALL WALK-BOOK TESTS PASSED")
