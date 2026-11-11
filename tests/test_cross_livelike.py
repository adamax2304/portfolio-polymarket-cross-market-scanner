from polymarket_cross_scanner.config import ScannerConfig
from polymarket_cross_scanner.cross_livelike import build_cross_livelike_ledger, build_cross_livelike_report
from polymarket_cross_scanner.storage import write_rows_csv


def execution_row(**overrides):
    row = {
        "simulated_at": "2026-06-05T00:00:00+00:00",
        "signal_id": "btc:123:btc-updown-5m-100:btc-updown-15m-0:threshold_known",
        "asset": "btc",
        "end_ts": "123",
        "target_shares": "10",
        "leg_order": "UP_FIRST",
        "initial_up_ask": "0.45",
        "initial_down_ask": "0.50",
        "initial_pair_cost": "0.95",
        "strict_max_pair_cost": "0.99",
        "rescue_max_pair_cost": "1.04",
        "fresh_up_ask": "0.45",
        "fresh_down_ask": "0.50",
        "fresh_up_bid": "0.44",
        "fresh_down_bid": "0.49",
        "first_leg": "UP",
        "first_filled": "True",
        "first_avg_price": "0.45",
        "first_cost": "4.5",
        "second_leg": "DOWN",
        "second_strict_filled": "True",
        "second_strict_avg_price": "0.50",
        "second_strict_cost": "5.0",
        "second_rescue_filled": "False",
        "second_rescue_avg_price": "",
        "second_rescue_cost": "0.0",
        "unwind_filled": "False",
        "unwind_avg_price": "",
        "unwind_proceeds": "0.0",
        "unwind_pnl": "",
        "final_action": "BOTH_FILL_SIMULTANEOUS",
        "final_pair_cost": "9.5",
        "final_pair_edge": "0.05",
        "locked_profit_estimate": "0.5",
        "unhedged_worst_case_loss": "4.5",
        "refetch_latency_ms": "12",
        "decision_reason": "test",
        "up_ask_levels_json": "[]",
        "down_ask_levels_json": "[]",
        "up_bid_levels_json": "[]",
        "down_bid_levels_json": "[]",
    }
    row.update(overrides)
    return row


def test_livelike_recovers_hft_pair_metadata_from_signal_id(tmp_path):
    cfg = ScannerConfig(root=tmp_path)
    write_rows_csv(cfg.cross_hft_paper_execution_csv, [execution_row()])

    ledger = build_cross_livelike_ledger(cfg)

    assert len(ledger) == 1
    assert ledger[0].engine == "cross_hft_paper"
    assert ledger[0].market_pair_type == "5m vs 15m"
    assert ledger[0].source_risk == "LOW_SAME_UPDOWN_FAMILY"
    assert ledger[0].latency_ms == cfg.cross_livelike_latency_floor_ms
    assert ledger[0].latency_adjusted_profit < ledger[0].modeled_profit


def test_livelike_report_counts_rescue_and_unwind_losses(tmp_path):
    cfg = ScannerConfig(root=tmp_path)
    write_rows_csv(
        cfg.cross_execution_sim_csv,
        [
            execution_row(
                final_action="HEDGE_RESCUE",
                second_strict_filled="False",
                second_rescue_filled="True",
                second_rescue_avg_price="0.57",
                second_rescue_cost="5.7",
                final_pair_cost="10.2",
                final_pair_edge="-0.02",
                locked_profit_estimate="-0.2",
            ),
            execution_row(
                final_action="UNWIND_LOSS",
                second_strict_filled="False",
                unwind_filled="True",
                unwind_avg_price="0.43",
                unwind_proceeds="4.3",
                unwind_pnl="-0.2",
                final_pair_cost="",
                final_pair_edge="",
                locked_profit_estimate="",
            ),
        ],
    )

    report = build_cross_livelike_report(build_cross_livelike_ledger(cfg))
    rescue = next(row for row in report if row.group_name == "engine_action" and row.group_value == "baseline|HEDGE_RESCUE")
    unwind = next(row for row in report if row.group_name == "engine_action" and row.group_value == "baseline|UNWIND_LOSS")

    assert rescue.realized_or_modeled_profit < 0
    assert unwind.realized_or_modeled_profit < 0
