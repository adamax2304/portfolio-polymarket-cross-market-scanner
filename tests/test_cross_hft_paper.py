from __future__ import annotations

import json

import pytest

from polymarket_cross_scanner.clob import ClobClient, ClobWsBookCache
from polymarket_cross_scanner.config import ScannerConfig
from polymarket_cross_scanner.cross_hft_paper import CrossHftPaperScanner, build_hft_pairs
from polymarket_cross_scanner.models import BookLevel, Market, OrderBook, TokenPair
from polymarket_cross_scanner.scanner import warm_stale_books
from polymarket_cross_scanner.utils import utc_now


def test_clob_ws_cache_drains_updated_tokens(tmp_path):
    cfg = ScannerConfig(root=tmp_path)
    cache = ClobWsBookCache(cfg)
    payload = {
        "event_type": "book",
        "asset_id": "token-a",
        "bids": [{"price": "0.40", "size": "10"}],
        "asks": [{"price": "0.41", "size": "10"}],
    }

    cache._handle_payload(json.dumps(payload))

    assert cache.drain_updated_tokens() == ["token-a"]
    assert cache.drain_updated_tokens() == []


def test_clob_ws_book_is_usable_with_asks_even_without_bids(tmp_path):
    cfg = ScannerConfig(root=tmp_path, clob_ws_enabled=True)
    cache = ClobWsBookCache(cfg)
    payload = {
        "event_type": "book",
        "asset_id": "token-a",
        "bids": [],
        "asks": [{"price": "0.41", "size": "10"}],
    }
    cache._handle_payload(json.dumps(payload))
    client = ClobClient(ScannerConfig(root=tmp_path, clob_ws_enabled=False))
    client._ws_cache = cache

    assert cache.get_order_book("token-a", max_age_seconds=30).best_ask == 0.41
    assert client.get_order_book_from_ws("token-a").best_ask == 0.41


def test_warm_stale_books_refreshes_fresh_but_unusable_books():
    clob = FakeWarmClob(
        ages={"missing-asks": 1.0, "usable": 1.0},
        books={"usable": book("usable", ask=0.42, size=10)},
    )

    stats = warm_stale_books(
        clob,
        {"missing-asks", "usable"},
        stale_after_seconds=20,
        max_tokens=10,
        workers=1,
    )

    assert clob.refreshed == ["missing-asks"]
    assert stats["refresh_candidates"] == 1
    assert stats["skipped_fresh"] == 1


def test_hft_pair_evaluation_uses_ws_books(tmp_path):
    lower = market("btc", "lower", 100.0, "up-lower", "down-lower")
    higher = market("btc", "higher", 101.0, "up-higher", "down-higher")
    pairs = build_hft_pairs([lower, higher], allow_price_inferred=False)
    cfg = ScannerConfig(root=tmp_path, min_edge=0.01, cross_min_net_edge=0.005, min_shares=5, max_paper_shares=10)
    scanner = CrossHftPaperScanner(cfg)
    scanner.clob = FakeClob(
        {
            "up-lower": book("up-lower", ask=0.45, size=10),
            "down-higher": book("down-higher", ask=0.48, size=10),
        }
    )

    evaluated = scanner.evaluate_pair(pairs[0], trigger_token_id="up-lower")

    assert evaluated is not None
    event, signal, books = evaluated
    assert event.top_cost == pytest.approx(0.93)
    assert event.net_avg_edge > 0.005
    assert signal.up_token_id == "up-lower"
    assert set(books) == {"up-lower", "down-higher"}


def test_hft_pairs_include_equal_threshold_duplicates():
    a = market("btc", "a", 100.0, "a-up", "a-down")
    b = market("btc", "b", 100.0, "b-up", "b-down")

    pairs = build_hft_pairs([a, b], allow_price_inferred=False)

    assert [(pair.lower.slug, pair.higher.slug, pair.orientation_source) for pair in pairs] == [
        ("a", "b", "same_threshold"),
        ("b", "a", "same_threshold"),
    ]


class FakeClob:
    def __init__(self, books):
        self.books = books

    def get_order_book_from_ws(self, token_id):
        return self.books.get(token_id)


class FakeWarmClob:
    def __init__(self, *, ages, books):
        self.ages = ages
        self.books = books
        self.refreshed = []

    def ws_book_age_seconds(self, token_id):
        return self.ages.get(token_id)

    def get_order_book_from_ws(self, token_id):
        return self.books.get(token_id)

    def refresh_order_book_cache(self, token_id):
        self.refreshed.append(token_id)
        return self.books.get(token_id), "rest"


def market(asset: str, slug: str, threshold: float, up: str, down: str) -> Market:
    return Market(
        asset=asset,
        duration_sec=300,
        slug=slug,
        question=f"{asset} Up or Down",
        condition_id=slug,
        tokens=TokenPair(up=up, down=down),
        start_ts=0,
        end_ts=4_102_444_800,
        threshold_price=threshold,
        threshold_source="test",
    )


def book(token_id: str, *, ask: float, size: float) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        bids=[BookLevel(price=max(0.01, ask - 0.01), size=size)],
        asks=[BookLevel(price=ask, size=size)],
        fetched_at=utc_now(),
    )
