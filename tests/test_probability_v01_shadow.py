from polymarket_cross_scanner.models import BookLevel, OrderBook
from polymarket_cross_scanner.probability_settlement import SETTLED_STATUS, ProbabilitySettlement
from polymarket_cross_scanner.probability_v01_shadow import (
    ProbabilityV01ShadowEntry,
    V01_CORE_RULES,
    build_v01_shadow_report,
    build_v01_shadow_ledger,
    is_v03_core_eligible,
    simulate_marketable_limit,
)
from polymarket_cross_scanner.utils import utc_now


def test_simulate_marketable_limit_full_and_partial_fill():
    book = OrderBook(
        token_id="token",
        bids=[],
        asks=[BookLevel(0.20, 10), BookLevel(0.21, 10), BookLevel(0.23, 10)],
        fetched_at=utc_now(),
    )

    full = simulate_marketable_limit(book, target_shares=15, limit_price=0.21)
    partial = simulate_marketable_limit(book, target_shares=25, limit_price=0.21)

    assert full["action"] == "FULL_FILL"
    assert round(full["cost"], 6) == 3.05
    assert partial["action"] == "PARTIAL_FILL"
    assert partial["shares"] == 20


def test_v01_shadow_ledger_uses_simulated_fill_cost_for_pnl():
    entry = ProbabilityV01ShadowEntry(
        evaluated_at="2026-06-04T12:00:00+00:00",
        signal_id="s1",
        v01_version="test",
        asset="btc",
        market_slug="btc-updown-5m-1",
        outcome="Up",
        token_id="token",
        grade="A",
        time_price_bucket="tte<=300|px<=25c",
        best_ask=0.20,
        calibrated_probability=0.30,
        calibrated_edge=0.08,
        v0_rule="v0_grade_a_cheap_near_expiry",
        risk_action="WOULD_ENTER",
        recommended_cost_usdc=25.0,
        recommended_shares=100.0,
        executable_shares=100.0,
        ask_depth_shares_1pct=100.0,
        ask_depth_usdc_1pct=20.0,
        entry_limit_price=0.21,
        simulated_fill_action="FULL_FILL",
        simulated_fill_shares=100.0,
        simulated_fill_cost=20.5,
        simulated_avg_price=0.205,
        simulated_slippage_per_share=0.005,
        book_levels_used=2,
        live_readiness="LIVE_READY_PAPER",
        live_readiness_reasons="",
    )
    settlement = ProbabilitySettlement(
        signal_id="s1",
        settled_at="2026-06-04T12:05:00+00:00",
        evaluated_at=entry.evaluated_at,
        asset="btc",
        market_slug=entry.market_slug,
        outcome="Up",
        token_id="token",
        grade="A",
        time_price_bucket=entry.time_price_bucket,
        price_bucket="px<=25c",
        tte_bucket="tte<=300",
        entry_seconds_to_expiry=240,
        best_ask=0.20,
        calibrated_probability=0.30,
        calibrated_edge=0.08,
        shares=100.0,
        cost=25.0,
        threshold_price=100.0,
        final_price=101.0,
        won=True,
        payout=100.0,
        realized_profit=75.0,
        roi_on_cost=3.0,
        status=SETTLED_STATUS,
    )

    ledger = build_v01_shadow_ledger([entry], {"s1": settlement})

    assert ledger[0].payout == 100.0
    assert ledger[0].realized_profit == 79.5
    assert round(ledger[0].roi_on_cost, 6) == round(79.5 / 20.5, 6)


def test_v03_core_rules_only_include_eth_b_900s_core():
    assert "v03_eth_b_900s_core" in V01_CORE_RULES
    assert "v02_eth_b_core" not in V01_CORE_RULES
    assert "v01_eth_b_observe_bucket" not in V01_CORE_RULES
    assert "v01_b_cheap_120s_probe" not in V01_CORE_RULES
    assert "v0_grade_a_cheap_near_expiry" not in V01_CORE_RULES


def test_v03_core_eligibility_requires_live_ready_full_fill_and_min_size():
    row = build_v01_shadow_ledger(
        [
            ProbabilityV01ShadowEntry(
                evaluated_at="2026-06-04T12:00:00+00:00",
                signal_id="s1",
                v01_version="test",
                asset="eth",
                market_slug="eth-updown-5m-1",
                outcome="Up",
                token_id="token",
                grade="B",
                time_price_bucket="tte<=900|px<=60c",
                best_ask=0.40,
                calibrated_probability=0.45,
                calibrated_edge=0.05,
                v0_rule="v03_eth_b_900s_core",
                risk_action="WOULD_ENTER",
                recommended_cost_usdc=25.0,
                recommended_shares=60.0,
                executable_shares=60.0,
                ask_depth_shares_1pct=60.0,
                ask_depth_usdc_1pct=24.0,
                entry_limit_price=0.41,
                simulated_fill_action="FULL_FILL",
                simulated_fill_shares=60.0,
                simulated_fill_cost=24.0,
                simulated_avg_price=0.40,
                simulated_slippage_per_share=0.0,
                book_levels_used=1,
                live_readiness="LIVE_READY_PAPER",
                live_readiness_reasons="",
            )
        ],
        {},
    )[0]

    assert is_v03_core_eligible(row)

    row.simulated_fill_cost = 0.99
    assert not is_v03_core_eligible(row)

    row.simulated_fill_cost = 24.0
    row.live_readiness = "PAPER_ONLY_REVIEW"
    assert not is_v03_core_eligible(row)


def test_shadow_report_includes_price_and_edge_research_buckets():
    row = build_v01_shadow_ledger(
        [
            ProbabilityV01ShadowEntry(
                evaluated_at="2026-06-04T12:00:00+00:00",
                signal_id="s1",
                v01_version="test",
                asset="eth",
                market_slug="eth-updown-15m-1",
                outcome="Up",
                token_id="token",
                grade="B",
                time_price_bucket="tte<=900|px<=60c",
                best_ask=0.42,
                calibrated_probability=0.50,
                calibrated_edge=0.061,
                v0_rule="v03_eth_b_900s_core",
                risk_action="WOULD_ENTER",
                recommended_cost_usdc=25.0,
                recommended_shares=50.0,
                executable_shares=50.0,
                ask_depth_shares_1pct=50.0,
                ask_depth_usdc_1pct=21.0,
                entry_limit_price=0.43,
                simulated_fill_action="FULL_FILL",
                simulated_fill_shares=50.0,
                simulated_fill_cost=21.0,
                simulated_avg_price=0.42,
                simulated_slippage_per_share=0.0,
                book_levels_used=1,
                live_readiness="LIVE_READY_PAPER",
                live_readiness_reasons="",
            )
        ],
        {},
    )

    report = build_v01_shadow_report(row)
    groups = {(item.group_name, item.group_value) for item in report}

    assert ("entry_price_bucket", "px<=50c") in groups
    assert ("edge_bucket", "edge 6-10%") in groups
    assert ("rule_time_price_bucket", "v03_eth_b_900s_core|tte<=900|px<=60c") in groups
    assert ("rule_edge_bucket", "v03_eth_b_900s_core|edge 6-10%") in groups
