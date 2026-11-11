from polymarket_cross_scanner.probability import ProbabilityObservation
from polymarket_cross_scanner.probability_calibrated import (
    ProbabilityCalibrationModel,
    calibrated_signals_from_observations,
    grade_calibrated_bucket,
)
from polymarket_cross_scanner.storage import write_rows_csv


def calibration_row(bucket, rows=150, observed=0.03, price=0.02, edge=0.25, roi=0.5):
    return {
        "bucket": bucket,
        "rows": rows,
        "avg_predicted_prob": 0.4,
        "observed_win_rate": observed,
        "calibration_error": observed - 0.4,
        "avg_market_price": price,
        "avg_net_edge": edge,
        "pnl_per_share_sum": 1.0,
        "roi_on_cost": roi,
    }


def observation(best_ask=0.02, seconds_to_expiry=100):
    return ProbabilityObservation(
        observed_at="2026-06-02T00:00:00+00:00",
        model_version="raw",
        asset="btc",
        duration_sec=300,
        market_slug="btc-updown-5m-1",
        condition_id="c",
        outcome="Up",
        token_id="t",
        start_ts=1,
        end_ts=301,
        seconds_to_expiry=seconds_to_expiry,
        threshold_price=100.0,
        current_price=100.1,
        distance_to_threshold=0.001,
        best_ask=best_ask,
        best_bid=best_ask - 0.01,
        ask_depth_shares_1pct=25.0,
        ask_depth_usdc_1pct=0.5,
        ask_levels_json="[]",
        fair_prob=0.9,
        market_prob=best_ask,
        gross_edge=0.9 - best_ask,
        estimated_taker_fee_per_share=0.001,
        net_edge=0.9 - best_ask - 0.001,
        momentum_15s=None,
        momentum_60s=None,
        realized_vol_120s=None,
        sigma_per_sqrt_second=0.0001,
        drift_per_second=0.0,
    )


def write_calibration(tmp_path, time_price_rows):
    write_rows_csv(tmp_path / "calibration_time_price_buckets.csv", time_price_rows)
    write_rows_csv(tmp_path / "calibration_edge_buckets.csv", [calibration_row("edge >=10%", rows=500, observed=0.5, roi=0.08)])
    write_rows_csv(tmp_path / "calibration_probability_deciles.csv", [calibration_row("p0.9-1.0", rows=500, observed=0.56, roi=0.07)])


def test_grade_calibrated_bucket_requires_positive_history_and_edge():
    assert grade_calibrated_bucket(rows=152, roi_on_cost=1.1, calibrated_edge=0.01, min_calibrated_edge=0.0)[0] == "A"
    assert grade_calibrated_bucket(rows=30, roi_on_cost=1.1, calibrated_edge=0.01, min_calibrated_edge=0.0)[0] == "REJECT"
    assert grade_calibrated_bucket(rows=152, roi_on_cost=-0.1, calibrated_edge=0.01, min_calibrated_edge=0.0)[0] == "REJECT"
    assert grade_calibrated_bucket(rows=152, roi_on_cost=1.1, calibrated_edge=-0.01, min_calibrated_edge=0.0)[0] == "REJECT"


def test_calibrated_signal_uses_time_price_observed_winrate(tmp_path):
    write_calibration(tmp_path, [calibration_row("tte<=120|px<=5c", rows=152, observed=0.035, price=0.015, roi=1.1)])
    model = ProbabilityCalibrationModel(tmp_path)
    signals = calibrated_signals_from_observations(
        [observation(best_ask=0.02, seconds_to_expiry=100)],
        model,
        min_shares=5,
        max_paper_shares=25,
        min_seconds_to_expiry=45,
        max_seconds_to_expiry=900,
        min_calibrated_edge=0.0,
        min_grade="B",
    )
    assert len(signals) == 1
    assert signals[0].grade == "A"
    assert signals[0].calibrated_probability == 0.035
    assert round(signals[0].calibrated_edge, 6) == 0.014


def test_negative_history_bucket_is_not_signaled(tmp_path):
    write_calibration(tmp_path, [calibration_row("tte<=300|px<95c", rows=453, observed=0.68, price=0.75, roi=-0.09)])
    model = ProbabilityCalibrationModel(tmp_path)
    signals = calibrated_signals_from_observations(
        [observation(best_ask=0.75, seconds_to_expiry=250)],
        model,
        min_shares=5,
        max_paper_shares=25,
        min_seconds_to_expiry=45,
        max_seconds_to_expiry=900,
        min_calibrated_edge=0.0,
        min_grade="C",
    )
    assert signals == []
