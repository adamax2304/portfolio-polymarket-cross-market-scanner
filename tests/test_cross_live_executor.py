from __future__ import annotations

import pytest

import polymarket_cross_scanner.cross_live_executor as live_executor_module
from polymarket_cross_scanner.config import ScannerConfig
from polymarket_cross_scanner.control_state import BotControlState, save_control_state
from polymarket_cross_scanner.cross_live_executor import CrossMarketLiveExecutor, load_live_rows
from polymarket_cross_scanner.models import ArbSignal, CrossMarketExecutionSim, CrossMarketLiveOrder, CrossMarketLiveRedemption
from polymarket_cross_scanner.storage import append_csv
from polymarket_cross_scanner.utils import utc_now


def test_live_executor_skips_when_not_armed(tmp_path):
    cfg = ScannerConfig(root=tmp_path, cross_live_enabled=True, cross_live_max_shares=5.0)
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=False))
    executor = CrossMarketLiveExecutor(cfg)

    row = executor.maybe_execute(signal(), sim())

    assert row is not None
    assert row.live_result == "SKIPPED"
    assert "live_not_armed" in row.notes
    rows = load_live_rows(cfg.cross_live_orders_csv)
    assert rows == []
    candidates = load_live_rows(cfg.cross_live_candidates_csv)
    assert len(candidates) == 1
    assert candidates[0]["reason"] == "live_not_armed"


def test_live_executor_keeps_guard_skips_out_of_live_orders(tmp_path):
    cfg = ScannerConfig(root=tmp_path, cross_live_enabled=True, cross_live_max_shares=5.0)
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=False))
    executor = CrossMarketLiveExecutor(cfg)

    first = executor.maybe_execute(signal(), sim())
    second = executor.maybe_execute(signal(), sim())

    assert first is not None
    assert second is not None
    assert len(load_live_rows(cfg.cross_live_orders_csv)) == 0
    assert len(load_live_rows(cfg.cross_live_candidates_csv)) == 2


def test_live_executor_retries_rejected_signal_after_cooldown(tmp_path, monkeypatch):
    cfg = ScannerConfig(
        root=tmp_path,
        cross_live_enabled=True,
        cross_live_max_shares=5.0,
        cross_live_retry_cooldown_ms=0,
        cross_live_max_attempts_per_signal=2,
    )
    executor = CrossMarketLiveExecutor(cfg)
    monkeypatch.setattr(executor, "guard_reason", lambda *_args, **_kwargs: "")

    def reject_batch(**_kwargs):
        return {
            "ok": True,
            "result": [
                {"success": False, "errorMsg": "first rejected", "orderID": "first"},
                {"success": False, "errorMsg": "second rejected", "orderID": "second"},
            ],
        }

    monkeypatch.setattr(live_executor_module, "place_fok_batch_limits", reject_batch)

    first = executor.maybe_execute(signal(), sim())
    second = executor.maybe_execute(signal(), sim())
    third = executor.maybe_execute(signal(), sim())

    assert first is not None
    assert second is not None
    assert third is None
    assert first.live_result == "BATCH_BOTH_REJECTED"
    assert second.live_result == "BATCH_BOTH_REJECTED"
    assert len(load_live_rows(cfg.cross_live_orders_csv)) == 2


def test_live_executor_does_not_retry_terminal_filled_signal(tmp_path, monkeypatch):
    cfg = ScannerConfig(root=tmp_path, cross_live_enabled=True, cross_live_max_shares=5.0)
    executor = CrossMarketLiveExecutor(cfg)
    monkeypatch.setattr(executor, "guard_reason", lambda *_args, **_kwargs: "")

    def fill_batch(**_kwargs):
        return {
            "ok": True,
            "result": [
                {"success": True, "status": "matched", "orderID": "first", "size_matched": 5.0},
                {"success": True, "status": "matched", "orderID": "second", "size_matched": 5.0},
            ],
        }

    monkeypatch.setattr(live_executor_module, "place_fok_batch_limits", fill_batch)

    first = executor.maybe_execute(signal(), sim())
    second = executor.maybe_execute(signal(), sim())

    assert first is not None
    assert first.live_result == "BOTH_FILLED_BATCH"
    assert second is None
    assert len(load_live_rows(cfg.cross_live_orders_csv)) == 1


def test_live_executor_does_not_round_size_up(tmp_path):
    cfg = ScannerConfig(root=tmp_path, cross_live_enabled=True, cross_live_max_shares=5.0, clob_min_order_size_shares=5.0)
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=False))
    executor = CrossMarketLiveExecutor(cfg)

    row = executor.maybe_execute(signal(executable_shares=4.0), sim(target_shares=4.0))

    assert row is not None
    assert row.shares == pytest.approx(4.0)
    assert row.live_result == "SKIPPED"
    assert load_live_rows(cfg.cross_live_orders_csv) == []


def test_live_executor_blocks_above_two_open_arbs(tmp_path):
    bridge_path = tmp_path / "bridge.py"
    bridge_path.write_text("", encoding="utf-8")
    cfg = ScannerConfig(root=tmp_path, cross_live_enabled=True, cross_live_max_open_arbs=2, clob_bridge_path=str(bridge_path))
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=True))
    append_csv(cfg.cross_live_orders_csv, [live_order("one"), live_order("two")])
    executor = CrossMarketLiveExecutor(cfg)

    reason = executor.guard_reason(signal(), sim(), "LOW_SAME_UPDOWN_FAMILY", 5.0, 4.75)

    assert reason == "max_open_live_arbs:2/2"


def test_redeemed_live_arbs_do_not_count_as_open(tmp_path):
    cfg = ScannerConfig(root=tmp_path, cross_live_enabled=True, cross_live_max_open_arbs=2)
    append_csv(cfg.cross_live_orders_csv, [live_order("one")])
    append_csv(cfg.cross_live_redemptions_csv, [
        redemption("one", "lower"),
        redemption("one", "higher"),
    ])
    executor = CrossMarketLiveExecutor(cfg)

    assert executor.open_live_arbs() == 0


def test_live_executor_rescue_hedges_second_leg_before_unwind(tmp_path, monkeypatch):
    cfg = ScannerConfig(root=tmp_path, cross_live_enabled=True, cross_live_max_shares=5.0, cross_live_execution_mode="sequential")
    executor = CrossMarketLiveExecutor(cfg)
    monkeypatch.setattr(executor, "guard_reason", lambda *_args, **_kwargs: "")
    calls = []
    payloads = [
        {"ok": True, "result": {"status": "matched", "order_id": "first", "size_matched": 5.0}},
        {"ok": False, "error": "strict second rejected"},
        {"ok": True, "result": {"status": "matched", "order_id": "second-rescue", "size_matched": 5.0}},
    ]

    def fake_place_fok_limit(**kwargs):
        calls.append(kwargs)
        return payloads.pop(0)

    monkeypatch.setattr(live_executor_module, "place_fok_limit", fake_place_fok_limit)

    row = executor._execute(signal(), sim())

    assert row.live_result == "BOTH_FILLED_RESCUE"
    assert row.final_action == "HEDGE_RESCUE"
    assert row.second_order_id == "second-rescue"
    assert row.estimated_cost == pytest.approx(5.0 * (0.46 + 0.58))
    assert [call["side"] for call in calls] == ["BUY", "BUY", "BUY"]
    assert calls[1]["price"] == pytest.approx(0.51)
    assert calls[2]["price"] == pytest.approx(0.58)


def test_live_executor_uses_unwind_ladder_after_second_and_rescue_fail(tmp_path, monkeypatch):
    cfg = ScannerConfig(root=tmp_path, cross_live_enabled=True, cross_live_max_shares=5.0, cross_live_execution_mode="sequential")
    executor = CrossMarketLiveExecutor(cfg)
    monkeypatch.setattr(executor, "guard_reason", lambda *_args, **_kwargs: "")
    calls = []
    payloads = [
        {"ok": True, "result": {"status": "matched", "order_id": "first", "size_matched": 5.0}},
        {"ok": False, "error": "strict second rejected"},
        {"ok": False, "error": "rescue second rejected"},
        {"ok": False, "error": "unwind top rejected"},
        {"ok": True, "result": {"status": "matched", "order_id": "unwind", "size_matched": 5.0}},
    ]

    def fake_place_fok_limit(**kwargs):
        calls.append(kwargs)
        return payloads.pop(0)

    monkeypatch.setattr(live_executor_module, "place_fok_limit", fake_place_fok_limit)

    row = executor._execute(signal(), sim())

    assert row.live_result == "SECOND_REJECTED_UNWIND_FILLED"
    assert row.unwind_order_id == "unwind"
    sell_calls = [call for call in calls if call["side"] == "SELL"]
    assert [call["price"] for call in sell_calls] == pytest.approx([0.44, 0.43])
    assert "unwind_ladder=0.4400:error:not_filled|0.4300:matched:filled" in row.notes


def test_live_executor_batch_fok_fills_both_legs(tmp_path, monkeypatch):
    cfg = ScannerConfig(root=tmp_path, cross_live_enabled=True, cross_live_max_shares=5.0, cross_live_execution_mode="batch_fok")
    executor = CrossMarketLiveExecutor(cfg)
    monkeypatch.setattr(executor, "guard_reason", lambda *_args, **_kwargs: "")
    calls = []

    def fake_batch(**kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "result": [
                {"success": True, "status": "matched", "orderID": "first", "size_matched": 5.0},
                {"success": True, "status": "matched", "orderID": "second", "size_matched": 5.0},
            ],
        }

    monkeypatch.setattr(live_executor_module, "place_fok_batch_limits", fake_batch)

    row = executor._execute(signal(), sim())

    assert row.live_result == "BOTH_FILLED_BATCH"
    assert row.first_order_id == "first"
    assert row.second_order_id == "second"
    assert len(calls) == 1
    assert [order["side"] for order in calls[0]["orders"]] == ["BUY", "BUY"]
    assert [order["order_type"] for order in calls[0]["orders"]] == ["FOK", "FOK"]


def test_live_executor_batch_one_fill_uses_rescue_missing_leg(tmp_path, monkeypatch):
    cfg = ScannerConfig(root=tmp_path, cross_live_enabled=True, cross_live_max_shares=5.0, cross_live_execution_mode="batch_fok")
    executor = CrossMarketLiveExecutor(cfg)
    monkeypatch.setattr(executor, "guard_reason", lambda *_args, **_kwargs: "")
    rescue_calls = []

    def fake_batch(**_kwargs):
        return {
            "ok": True,
            "result": [
                {"success": True, "status": "matched", "orderID": "first", "size_matched": 5.0},
                {"success": False, "errorMsg": "second rejected", "orderID": "second"},
            ],
        }

    def fake_place_fok_limit(**kwargs):
        rescue_calls.append(kwargs)
        return {"ok": True, "result": {"status": "matched", "order_id": "rescue", "size_matched": 5.0}}

    monkeypatch.setattr(live_executor_module, "place_fok_batch_limits", fake_batch)
    monkeypatch.setattr(live_executor_module, "place_fok_limit", fake_place_fok_limit)

    row = executor._execute(signal(), sim())

    assert row.live_result == "BATCH_ONE_FILLED_RESCUE"
    assert row.first_order_id == "first"
    assert row.second_order_id == "rescue"
    assert rescue_calls[0]["price"] == pytest.approx(0.58)
    assert "batch_one_filled" in row.notes


def test_order_result_treats_success_true_error_msg_as_rejected():
    result = live_executor_module.order_result(
        {
            "ok": True,
            "result": {
                "success": True,
                "status": "",
                "orderID": "killed",
                "errorMsg": "order couldn't be fully filled. FOK orders are fully filled or killed.",
            },
        },
        expected_size=5.0,
    )

    assert result["filled"] is False
    assert result["status"] == "error"
    assert result["order_id"] == "killed"
    assert "FOK orders are fully filled or killed" in result["notes"]


def test_live_executor_pads_low_price_buy_leg_to_min_notional(tmp_path, monkeypatch):
    cfg = ScannerConfig(root=tmp_path, cross_live_enabled=True, cross_live_max_shares=5.0, cross_live_execution_mode="batch_fok")
    executor = CrossMarketLiveExecutor(cfg)
    monkeypatch.setattr(executor, "guard_reason", lambda *_args, **_kwargs: "")
    calls = []

    def fake_batch(**kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "result": [
                {"success": True, "status": "matched", "orderID": "first", "size_matched": 25.0},
                {"success": True, "status": "matched", "orderID": "second", "size_matched": 5.0},
            ],
        }

    monkeypatch.setattr(live_executor_module, "place_fok_batch_limits", fake_batch)

    row = executor._execute(signal(), sim_low_price())

    assert row.live_result == "BOTH_FILLED_BATCH"
    assert row.estimated_cost == pytest.approx(25.5 * 0.04 + 5.0 * 0.95)
    assert [order["size"] for order in calls[0]["orders"]] == pytest.approx([25.5, 5.0])
    assert "min_notional_padding" in row.notes


def test_live_executor_pads_to_clob_amount_precision(tmp_path):
    cfg = ScannerConfig(root=tmp_path, cross_live_enabled=True)
    executor = CrossMarketLiveExecutor(cfg)

    size = executor.size_for_min_notional(0.14, 5.0)

    assert size == pytest.approx(7.5)
    assert size * 0.14 == pytest.approx(1.05)


def test_live_executor_skips_min_notional_padding_above_extra_cap(tmp_path, monkeypatch):
    cfg = ScannerConfig(
        root=tmp_path,
        cross_live_enabled=True,
        cross_live_max_shares=5.0,
        cross_live_execution_mode="batch_fok",
        clob_min_notional_max_extra_usdc=0.5,
    )
    executor = CrossMarketLiveExecutor(cfg)
    monkeypatch.setattr(executor, "guard_reason", lambda *_args, **_kwargs: "")

    def fail_batch(**_kwargs):
        raise AssertionError("batch should not be submitted when padding extra cap is exceeded")

    monkeypatch.setattr(live_executor_module, "place_fok_batch_limits", fail_batch)

    row = executor._execute(signal(), sim_low_price())

    assert row.live_result == "SKIPPED"
    assert "min_notional_padding_extra_cap:0.8200>0.5000" in row.notes


def signal(*, executable_shares: float = 5.0) -> ArbSignal:
    return ArbSignal(
        detected_at=utc_now(),
        asset="btc",
        end_ts=4_102_444_800,
        lower_market_slug="bitcoin-up-or-down-5m-lower",
        higher_market_slug="bitcoin-up-or-down-15m-higher",
        lower_threshold=100.0,
        higher_threshold=101.0,
        long_up_slug="bitcoin-up-or-down-5m-lower",
        long_down_slug="bitcoin-up-or-down-15m-higher",
        up_token_id="up-token",
        down_token_id="down-token",
        up_ask=0.45,
        down_ask=0.50,
        top_cost=0.95,
        top_edge=0.05,
        executable_shares=executable_shares,
        avg_cost=0.95,
        avg_edge=0.05,
        orientation_source="threshold",
        net_avg_edge=0.05,
    )


def sim(*, target_shares: float = 5.0) -> CrossMarketExecutionSim:
    return CrossMarketExecutionSim(
        simulated_at=utc_now(),
        signal_id=signal().signal_id,
        asset="btc",
        end_ts=4_102_444_800,
        target_shares=target_shares,
        leg_order="UP_FIRST",
        initial_up_ask=0.45,
        initial_down_ask=0.50,
        initial_pair_cost=0.95,
        strict_max_pair_cost=0.99,
        rescue_max_pair_cost=1.04,
        fresh_up_ask=0.45,
        fresh_down_ask=0.50,
        fresh_up_bid=0.44,
        fresh_down_bid=0.49,
        first_leg="UP",
        first_filled=True,
        first_avg_price=0.45,
        first_cost=2.25,
        second_leg="DOWN",
        second_strict_filled=True,
        second_strict_avg_price=0.50,
        second_strict_cost=2.50,
        second_rescue_filled=False,
        second_rescue_avg_price=None,
        second_rescue_cost=0.0,
        unwind_filled=False,
        unwind_avg_price=None,
        unwind_proceeds=0.0,
        unwind_pnl=None,
        final_action="BOTH_FILL_STRICT",
        final_pair_cost=0.95,
        final_pair_edge=0.05,
        locked_profit_estimate=0.25,
        unhedged_worst_case_loss=0.0,
        refetch_latency_ms=1,
        decision_reason="test",
    )


def sim_low_price() -> CrossMarketExecutionSim:
    base = sim()
    base.initial_up_ask = 0.03
    base.initial_down_ask = 0.96
    base.initial_pair_cost = 0.99
    base.fresh_up_ask = 0.03
    base.fresh_down_ask = 0.96
    base.fresh_up_bid = 0.02
    base.fresh_down_bid = 0.95
    base.first_avg_price = 0.03
    base.first_cost = 0.15
    base.second_strict_avg_price = 0.96
    base.second_strict_cost = 4.8
    base.final_pair_cost = 0.99
    base.final_pair_edge = 0.01
    return base


def live_order(signal_id: str) -> CrossMarketLiveOrder:
    return CrossMarketLiveOrder(
        attempted_at=utc_now(),
        signal_id=signal_id,
        asset="btc",
        end_ts=4_102_444_800,
        lower_market_slug="lower",
        higher_market_slug="higher",
        source_risk="LOW_SAME_UPDOWN_FAMILY",
        shares=5.0,
        up_token_id="up",
        down_token_id="down",
        up_limit_price=0.45,
        down_limit_price=0.50,
        first_leg="UP",
        first_order_id="one",
        first_status="matched",
        second_order_id="two",
        second_status="matched",
        unwind_order_id="",
        unwind_status="",
        estimated_cost=4.75,
        final_action="BOTH_FILL_STRICT",
        live_result="BOTH_FILLED",
        bridge_latency_ms=10,
    )


def redemption(signal_id: str, slug: str) -> CrossMarketLiveRedemption:
    return CrossMarketLiveRedemption(
        attempted_at=utc_now(),
        signal_id=signal_id,
        market_slug=slug,
        condition_id="0xabc",
        command="redeem_eoa_direct",
        redeem_result="REDEEMED",
        tx_hash="0xhash",
    )


def test_live_executor_ws_preflight_skips_when_ws_depth_insufficient(tmp_path, monkeypatch):
    cfg = ScannerConfig(
        root=tmp_path,
        cross_live_enabled=True,
        cross_live_max_shares=5.0,
        cross_live_execution_mode="batch_fok",
        cross_live_preflight_enabled=True,
        cross_live_preflight_use_ws_levels=True,
    )
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=True))
    executor = CrossMarketLiveExecutor(cfg)
    monkeypatch.setattr(executor, "guard_reason", lambda *_a, **_k: "")
    s = sim()
    # WS book shows only 1 share available — preflight should fail before any submit
    s.up_ask_levels_json = '[{"price":0.45,"size":1.0},{"price":0.80,"size":10.0}]'
    s.down_ask_levels_json = '[{"price":0.50,"size":100.0}]'
    batch_called = []
    monkeypatch.setattr(live_executor_module, "place_fok_batch_limits", lambda **k: batch_called.append(k) or {"ok": True, "result": []})

    row = executor._execute(signal(), s)

    assert row.live_result == "SKIPPED"
    assert "ws_insufficient_depth" in row.notes
    assert batch_called == []  # never sent to CLOB


def test_live_executor_ws_preflight_passes_when_ws_depth_sufficient(tmp_path, monkeypatch):
    cfg = ScannerConfig(
        root=tmp_path,
        cross_live_enabled=True,
        cross_live_max_shares=5.0,
        cross_live_execution_mode="batch_fok",
        cross_live_preflight_enabled=True,
        cross_live_preflight_use_ws_levels=True,
    )
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=True))
    executor = CrossMarketLiveExecutor(cfg)
    monkeypatch.setattr(executor, "guard_reason", lambda *_a, **_k: "")
    s = sim()
    s.up_ask_levels_json = '[{"price":0.45,"size":50.0}]'
    s.down_ask_levels_json = '[{"price":0.50,"size":50.0}]'

    def fake_batch(**k):
        return {
            "ok": True,
            "result": [
                {"success": True, "status": "matched", "orderID": "f", "size_matched": 5.0},
                {"success": True, "status": "matched", "orderID": "s", "size_matched": 5.0},
            ],
        }

    monkeypatch.setattr(live_executor_module, "place_fok_batch_limits", fake_batch)

    row = executor._execute(signal(), s)

    assert row.live_result == "BOTH_FILLED_BATCH"


def test_live_executor_stale_sim_is_skipped(tmp_path, monkeypatch):
    cfg = ScannerConfig(
        root=tmp_path,
        cross_live_enabled=True,
        cross_live_max_shares=5.0,
        cross_live_execution_mode="batch_fok",
        cross_live_max_sim_age_ms=500,
    )
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=True))
    executor = CrossMarketLiveExecutor(cfg)
    monkeypatch.setattr(executor, "guard_reason", lambda *_a, **_k: "")
    s = sim()
    # Force a deliberately stale simulated_at (5 seconds ago)
    s.simulated_at = "2026-01-01T00:00:00+00:00"
    batch_called = []
    monkeypatch.setattr(live_executor_module, "place_fok_batch_limits", lambda **k: batch_called.append(k) or {"ok": True, "result": []})

    row = executor._execute(signal(), s)

    assert row.live_result == "SKIPPED"
    assert "stale_sim" in row.notes
    assert batch_called == []


class FakeOracleTracker:
    def __init__(self, bps: int | None, *, snapshot: dict | None = None):
        self.bps = bps
        self._snapshot = snapshot or {"binance": None, "chainlink": None, "divergence_bps": bps}

    def divergence_bps(self, asset):
        return self.bps

    def snapshot(self, asset):
        return dict(self._snapshot)


def test_oracle_divergence_guard_skips_high_divergence(tmp_path):
    cfg = ScannerConfig(
        root=tmp_path,
        cross_live_enabled=True,
        cross_live_research_oracle_divergence_guard=True,
        cross_live_research_max_oracle_divergence_bps=10,
        cross_live_research_require_binance_threshold=False,
    )
    executor = CrossMarketLiveExecutor(cfg)
    # Cross-family signal: one hourly leg, one rolling updown leg
    sig = signal()
    sig.lower_market_slug = "btc-updown-15m-1780868700"
    sig.higher_market_slug = "bitcoin-up-or-down-june-7-2026-11pm-et"
    executor.attach_oracle_tracker(FakeOracleTracker(bps=25))

    reason = executor.oracle_divergence_reason(sig)

    assert reason.startswith("oracle_divergence_high:")
    assert "25bps>10bps" in reason


def test_oracle_divergence_guard_allows_low_divergence(tmp_path):
    cfg = ScannerConfig(
        root=tmp_path,
        cross_live_enabled=True,
        cross_live_research_oracle_divergence_guard=True,
        cross_live_research_max_oracle_divergence_bps=10,
        cross_live_research_require_binance_threshold=False,
    )
    executor = CrossMarketLiveExecutor(cfg)
    executor.attach_oracle_tracker(FakeOracleTracker(bps=5))

    reason = executor.oracle_divergence_reason(signal())

    assert reason == ""


def test_oracle_divergence_guard_requires_binance_threshold(tmp_path):
    from polymarket_cross_scanner.thresholds import ThresholdStore

    cfg = ScannerConfig(
        root=tmp_path,
        cross_live_enabled=True,
        cross_live_research_oracle_divergence_guard=True,
        cross_live_research_max_oracle_divergence_bps=10,
        cross_live_research_require_binance_threshold=True,
    )
    executor = CrossMarketLiveExecutor(cfg)
    store = ThresholdStore(cfg.threshold_path)
    executor.attach_oracle_tracker(FakeOracleTracker(bps=5), store)

    sig = signal()
    sig.lower_market_slug = "btc-updown-15m-1780868700"
    sig.higher_market_slug = "bitcoin-up-or-down-june-7-2026-11pm-et"

    reason = executor.oracle_divergence_reason(sig)

    assert reason.startswith("missing_binance_threshold:")


def test_live_executor_dynamic_unwind_uses_ws_bids(tmp_path, monkeypatch):
    cfg = ScannerConfig(
        root=tmp_path,
        cross_live_enabled=True,
        cross_live_max_shares=5.0,
        cross_live_execution_mode="batch_fok",
        cross_live_unwind_dynamic_bids=True,
    )
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=True))
    executor = CrossMarketLiveExecutor(cfg)
    s = sim()
    s.up_bid_levels_json = '[{"price":0.41,"size":3.0},{"price":0.40,"size":10.0}]'
    sell_attempts = []

    def fake_place(**k):
        sell_attempts.append((k.get("side"), k.get("price")))
        # Reject the first try (0.41), accept 0.40
        if k.get("price") and abs(k["price"] - 0.40) < 1e-9:
            return {"ok": True, "result": {"success": True, "status": "matched", "orderID": "u1", "size_matched": 5.0}}
        return {"ok": True, "result": {"success": False, "status": "rejected", "errorMsg": "no fill"}}

    monkeypatch.setattr(live_executor_module, "place_fok_limit", fake_place)

    result = executor.unwind_first_leg(
        token_id="up",
        first_leg="UP",
        first_price=0.45,
        sim=s,
        shares=5.0,
        signal=signal(),
    )

    sell_prices = [p for side, p in sell_attempts if side == "SELL"]
    # Dynamic bids 0.41, 0.40 should appear at the top of the candidate list
    assert sell_prices[0] == 0.41
    assert sell_prices[1] == 0.40
    assert result["filled"] is True
    assert "final_price=0.4000" in result["notes"]
