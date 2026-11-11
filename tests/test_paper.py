from polymarket_cross_scanner.models import ArbSignal
from polymarket_cross_scanner.paper import PaperLedger


def signal(source="threshold_known"):
    return ArbSignal(
        detected_at="now",
        asset="btc",
        end_ts=1,
        lower_market_slug="low",
        higher_market_slug="high",
        lower_threshold=1.0,
        higher_threshold=2.0,
        long_up_slug="low",
        long_down_slug="high",
        up_token_id="u",
        down_token_id="d",
        up_ask=0.4,
        down_ask=0.5,
        top_cost=0.9,
        top_edge=0.1,
        executable_shares=10,
        avg_cost=0.9,
        avg_edge=0.1,
        orientation_source=source,
    )


def test_price_inferred_paper_trade_is_marked_research_open():
    trade = PaperLedger().open_from_signals([signal("price_inferred")], max_shares=10)[0]
    assert trade.status == "RESEARCH_OPEN"


def test_threshold_known_paper_trade_is_open():
    trade = PaperLedger().open_from_signals([signal("threshold_known")], max_shares=10)[0]
    assert trade.status == "OPEN"
