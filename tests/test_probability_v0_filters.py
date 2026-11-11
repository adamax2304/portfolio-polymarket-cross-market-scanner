from polymarket_cross_scanner.probability_settlement import SETTLED_STATUS, ProbabilitySettlement
from polymarket_cross_scanner.probability_risk import ProbabilityRiskDecision
from polymarket_cross_scanner.probability_v0_filters import (
    AVOID,
    OBSERVE,
    OBSERVE_COOLING,
    TRADE_CANDIDATE,
    ProbabilityV0LiveLabel,
    build_v0_forward_decisions,
    build_v0_live_labels,
    build_v0_filter_decisions,
    build_v0_filter_report,
    classify_v0,
)


def settlement(**overrides):
    row = {
        "signal_id": "s1",
        "settled_at": "2026-06-03T00:05:00+00:00",
        "evaluated_at": "2026-06-03T00:00:00+00:00",
        "asset": "xrp",
        "market_slug": "xrp-updown-5m-1",
        "outcome": "Up",
        "token_id": "token",
        "grade": "A",
        "time_price_bucket": "tte<=300|px<=25c",
        "price_bucket": "px<=25c",
        "tte_bucket": "tte<=300",
        "entry_seconds_to_expiry": 240,
        "best_ask": 0.20,
        "calibrated_probability": 0.30,
        "calibrated_edge": 0.09,
        "shares": 50.0,
        "cost": 10.0,
        "threshold_price": 100.0,
        "final_price": 101.0,
        "won": True,
        "payout": 50.0,
        "realized_profit": 40.0,
        "roi_on_cost": 4.0,
        "status": SETTLED_STATUS,
        "notes": "",
    }
    row.update(overrides)
    return ProbabilitySettlement(**row)


def test_v02_cools_grade_a_cheap_near_expiry_bucket():
    action, rule, reasons = classify_v0(settlement(asset="btc", grade="A", best_ask=0.12, entry_seconds_to_expiry=180))

    assert action == OBSERVE_COOLING
    assert rule == "observe_grade_a_cheap_near_expiry_cooling"
    assert reasons


def test_v0_avoids_obvious_negative_buckets_but_keeps_other_flow_observable():
    assert classify_v0(settlement(best_ask=0.03))[0] == AVOID
    assert classify_v0(settlement(asset="bnb", best_ask=0.20))[0] == AVOID
    assert classify_v0(settlement(asset="doge", grade="B", best_ask=0.20))[0] == AVOID
    assert classify_v0(settlement(asset="btc", grade="B", best_ask=0.35, entry_seconds_to_expiry=600))[0] == OBSERVE


def test_v03_promotes_only_eth_b_900s_core_and_cools_failed_buckets():
    eth_action, eth_rule, _reasons = classify_v0(
        settlement(
            asset="eth",
            grade="B",
            best_ask=0.62,
            entry_seconds_to_expiry=500,
            time_price_bucket="tte<=900|px<=60c",
            calibrated_edge=0.02,
        )
    )
    eth_300_action, eth_300_rule, _reasons = classify_v0(
        settlement(
            asset="eth",
            grade="B",
            best_ask=0.40,
            entry_seconds_to_expiry=200,
            time_price_bucket="tte<=300|px<=60c",
            calibrated_edge=0.05,
        )
    )
    cheap_action, cheap_rule, _reasons = classify_v0(
        settlement(
            asset="btc",
            grade="B",
            best_ask=0.20,
            entry_seconds_to_expiry=100,
            time_price_bucket="tte<=120|px<=25c",
            calibrated_edge=0.02,
        )
    )

    assert (eth_action, eth_rule) == (TRADE_CANDIDATE, "v03_eth_b_900s_core")
    assert (eth_300_action, eth_300_rule) == (OBSERVE_COOLING, "observe_eth_b_300s_cooling")
    assert (cheap_action, cheap_rule) == (OBSERVE_COOLING, "observe_b_cheap_120s_probe_cooling")


def test_v01_demotes_old_b_midprice_candidate_after_forward_cooloff():
    action, rule, reasons = classify_v0(
        settlement(
            asset="xrp",
            grade="B",
            best_ask=0.40,
            entry_seconds_to_expiry=240,
            time_price_bucket="tte<=300|px<=60c",
            calibrated_edge=0.04,
        )
    )

    assert action == OBSERVE_COOLING
    assert rule == "observe_old_b_midprice_candidate_cooling"
    assert reasons


def test_v01_avoids_grade_a_outside_core_bucket():
    action, rule, _reasons = classify_v0(
        settlement(asset="btc", grade="A", best_ask=0.40, entry_seconds_to_expiry=600, time_price_bucket="tte<=900|px<=60c")
    )

    assert action == AVOID
    assert rule == "avoid_grade_a_outside_core_bucket"


def test_v0_report_summarizes_candidate_roi_separately():
    rows = [
        settlement(
            signal_id="candidate-win",
            asset="eth",
            grade="B",
            best_ask=0.40,
            time_price_bucket="tte<=900|px<=60c",
            cost=10,
            payout=50,
            realized_profit=40,
            won=True,
        ),
        settlement(signal_id="avoid-loss", asset="bnb", grade="B", best_ask=0.20, cost=10, payout=0, realized_profit=-10, won=False),
    ]

    decisions = build_v0_filter_decisions(rows)
    report = build_v0_filter_report(decisions)

    candidate = next(row for row in report if row.group_name == "v0_action" and row.group_value == TRADE_CANDIDATE)
    avoid = next(row for row in report if row.group_name == "v0_action" and row.group_value == AVOID)

    assert candidate.settled == 1
    assert candidate.realized_profit == 40
    assert avoid.settled == 1
    assert avoid.realized_profit == -10


def test_v0_live_labels_do_not_change_risk_action():
    decision = ProbabilityRiskDecision(
        evaluated_at="2026-06-03T00:00:00+00:00",
        signal_id="s1",
        asset="bnb",
        market_slug="bnb-updown-5m-1",
        outcome="Up",
        token_id="token",
        grade="B",
        time_price_bucket="tte<=300|px<=60c",
        best_ask=0.40,
        calibrated_probability=0.50,
        calibrated_edge=0.09,
        executable_shares=10,
        executable_cost=4,
        kelly_fraction_raw=0.1,
        kelly_fraction_used=0.08,
        grade_size_multiplier=0.6,
        desired_cost_usdc=4,
        desired_shares=10,
        recommended_cost_usdc=4,
        recommended_shares=10,
        action="WOULD_ENTER",
        risk_reasons="",
        daily_worst_case_loss_used_usdc=0,
        daily_stop_loss_usdc=1000,
        daily_cost_used_usdc=0,
        max_daily_cost_usdc=1000,
        open_cost_usdc=0,
        max_open_cost_usdc=1000,
        open_positions=0,
        max_open_positions=100,
        asset_open_cost_usdc=0,
        max_asset_cost_usdc=1000,
        bucket_open_cost_usdc=0,
        max_bucket_cost_usdc=1000,
        expires_at_ts=1780440100,
        soft_mode=True,
    )

    label = build_v0_live_labels([decision])[0]

    assert label.risk_action == "WOULD_ENTER"
    assert label.v0_action == AVOID
    assert label.v0_rule == "avoid_bnb_until_recovery"


def test_v0_forward_decisions_only_include_live_labeled_signals():
    labeled = settlement(signal_id="labeled", asset="xrp", grade="A", best_ask=0.20)
    old_backfill = settlement(signal_id="old", asset="bnb", grade="B", best_ask=0.20)
    label = ProbabilityV0LiveLabel(
        evaluated_at=labeled.evaluated_at,
        signal_id="labeled",
        asset="xrp",
        market_slug=labeled.market_slug,
        outcome=labeled.outcome,
        grade="A",
        time_price_bucket=labeled.time_price_bucket,
        best_ask=0.20,
        calibrated_probability=0.30,
        calibrated_edge=0.09,
        risk_action="WOULD_ENTER",
        recommended_cost_usdc=10,
        recommended_shares=50,
        v0_action=TRADE_CANDIDATE,
        v0_rule="v0_grade_a_cheap_near_expiry",
        v0_reasons="forward label",
    )

    decisions = build_v0_forward_decisions([labeled, old_backfill], {"labeled": label})

    assert [row.signal_id for row in decisions] == ["labeled"]
    assert decisions[0].v0_action == TRADE_CANDIDATE
