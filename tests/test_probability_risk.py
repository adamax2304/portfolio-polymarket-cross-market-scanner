from dataclasses import asdict
from datetime import datetime, timezone

from polymarket_cross_scanner.config import ScannerConfig
from polymarket_cross_scanner.probability_calibrated import CalibratedProbabilitySignal
from polymarket_cross_scanner.probability_risk import ProbabilityRiskEngine, binary_kelly_cost_fraction
from polymarket_cross_scanner.storage import write_rows_csv


def signal(slug="btc-updown-5m-1", ask=0.02, probability=0.035, edge=0.014, grade="A", bucket="tte<=300|px<=5c"):
    return CalibratedProbabilitySignal(
        detected_at="2026-06-02T00:00:00+00:00",
        model_version="calibrated",
        raw_model_version="raw",
        asset="btc",
        duration_sec=300,
        market_slug=slug,
        condition_id="c",
        outcome="Up",
        token_id="t",
        seconds_to_expiry=100,
        threshold_price=100.0,
        current_price=100.1,
        best_ask=ask,
        raw_fair_prob=0.9,
        raw_net_edge=0.8,
        calibrated_probability=probability,
        calibrated_edge=edge,
        estimated_taker_fee_per_share=0.001,
        executable_shares=1000.0,
        max_paper_cost=1000.0 * ask,
        ask_depth_shares_1pct=1000.0,
        ask_depth_usdc_1pct=20.0,
        time_price_bucket=bucket,
        time_price_rows=152,
        time_price_observed_win_rate=probability,
        time_price_roi_on_cost=1.1,
        edge_bucket="edge >=10%",
        edge_rows=500,
        edge_roi_on_cost=0.08,
        probability_bucket="p0.9-1.0",
        probability_rows=500,
        probability_roi_on_cost=0.07,
        grade=grade,
        reject_reason="",
        ask_levels_json="[]",
    )


def today_signal(slug="btc-updown-5m-1"):
    row = signal(slug=slug)
    row.detected_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return row


def test_binary_kelly_cost_fraction_uses_edge_after_fee():
    assert binary_kelly_cost_fraction(0.035, 0.02, 0.001) > 0
    assert binary_kelly_cost_fraction(0.02, 0.02, 0.001) == 0


def test_risk_engine_sizes_and_caps_trade_cost(tmp_path):
    cfg = ScannerConfig(
        root=tmp_path,
        probability_risk_bankroll_usdc=1000,
        probability_risk_kelly_fraction=1.0,
        probability_risk_max_trade_cost_usdc=3.0,
        probability_risk_min_trade_cost_usdc=0.01,
    )
    decision = ProbabilityRiskEngine(cfg).evaluate(signal())
    assert decision.action == "WOULD_ENTER_CLIPPED"
    assert decision.recommended_cost_usdc == 3.0
    assert "max_trade_cost" in decision.risk_reasons


def test_risk_engine_blocks_duplicate_market_outcome(tmp_path):
    cfg = ScannerConfig(root=tmp_path, probability_risk_min_trade_cost_usdc=0.01)
    engine = ProbabilityRiskEngine(cfg)
    first = engine.evaluate(signal())
    second = engine.evaluate(signal())
    assert first.recommended_cost_usdc > 0
    assert second.action == "WOULD_BLOCK"
    assert "duplicate_market_outcome" in second.risk_reasons


def test_risk_engine_soft_daily_stop_blocks_without_stopping_collection(tmp_path):
    cfg = ScannerConfig(
        root=tmp_path,
        probability_risk_min_trade_cost_usdc=0.01,
        probability_risk_daily_stop_loss_usdc=0.5,
        probability_risk_max_daily_cost_usdc=100,
    )
    engine = ProbabilityRiskEngine(cfg)
    first = engine.evaluate(signal(slug="s1"))
    second = engine.evaluate(signal(slug="s2"))
    assert first.recommended_cost_usdc > 0
    assert second.action == "WOULD_BLOCK"
    assert "daily_stop_loss" in second.risk_reasons
    assert second.soft_mode is True


def test_risk_engine_restores_today_state_from_csv(tmp_path):
    cfg = ScannerConfig(root=tmp_path, probability_risk_min_trade_cost_usdc=0.01)
    first_engine = ProbabilityRiskEngine(cfg)
    first = first_engine.evaluate(today_signal(slug="restore"))
    write_rows_csv(cfg.probability_risk_decisions_csv, [asdict(first)])

    restored = ProbabilityRiskEngine(cfg)
    assert restored.daily_cost_used == first.recommended_cost_usdc
    assert restored.daily_worst_case_loss_used == first.recommended_cost_usdc
    assert "restore:Up" in restored.entered_keys

    duplicate = restored.evaluate(today_signal(slug="restore"))
    assert duplicate.action == "WOULD_BLOCK"
    assert "duplicate_market_outcome" in duplicate.risk_reasons
