from polymarket_cross_scanner.probability import ProbabilityObservation, signals_from_observations
from polymarket_cross_scanner.probability_early import EARLY_MODEL_VERSION, early_model_signals_from_raw


def observation(*, seconds_to_expiry=170, best_ask=0.40, fair_prob=0.50):
    return ProbabilityObservation(
        observed_at="2026-06-05T00:00:00+00:00",
        model_version="threshold_log_normal_v1",
        asset="btc",
        duration_sec=300,
        market_slug="btc-updown-5m-1",
        condition_id="c",
        outcome="Up",
        token_id="t",
        start_ts=1000,
        end_ts=1300,
        seconds_to_expiry=seconds_to_expiry,
        threshold_price=100.0,
        current_price=100.2,
        distance_to_threshold=0.002,
        best_ask=best_ask,
        best_bid=best_ask - 0.01,
        ask_depth_shares_1pct=25.0,
        ask_depth_usdc_1pct=10.0,
        ask_levels_json="[]",
        fair_prob=fair_prob,
        market_prob=best_ask,
        gross_edge=fair_prob - best_ask,
        estimated_taker_fee_per_share=0.001,
        net_edge=fair_prob - best_ask - 0.001,
        momentum_15s=0.001,
        momentum_60s=0.002,
        realized_vol_120s=0.0001,
        sigma_per_sqrt_second=0.0001,
        drift_per_second=0.0,
    )


def test_raw_probability_signals_are_limited_by_seconds_since_start():
    early = observation(seconds_to_expiry=170)  # 130s after start
    late = observation(seconds_to_expiry=90)  # 210s after start

    signals = signals_from_observations(
        [early, late],
        min_net_edge=0.01,
        min_shares=5,
        max_paper_shares=25,
        min_seconds_to_expiry=45,
        max_seconds_to_expiry=14400,
        min_seconds_since_start=10,
        max_seconds_since_start=180,
    )

    assert len(signals) == 1
    assert signals[0].seconds_to_expiry == 170
    assert "seconds_since_start=130" in signals[0].notes


def test_early_model_uses_raw_fair_probability_as_primary_probability():
    raw = signals_from_observations(
        [observation(best_ask=0.42, fair_prob=0.53)],
        min_net_edge=0.01,
        min_shares=5,
        max_paper_shares=25,
        min_seconds_to_expiry=45,
        max_seconds_to_expiry=14400,
        min_seconds_since_start=10,
        max_seconds_since_start=180,
    )

    model_signals = early_model_signals_from_raw(
        raw,
        min_seconds_since_start=10,
        max_seconds_since_start=180,
        min_raw_edge=0.0,
    )

    assert len(model_signals) == 1
    assert model_signals[0].model_version == EARLY_MODEL_VERSION
    assert model_signals[0].calibrated_probability == model_signals[0].raw_fair_prob
    assert round(model_signals[0].calibrated_edge, 6) == round(model_signals[0].raw_net_edge, 6)
    assert model_signals[0].time_price_bucket.startswith("start<=")
