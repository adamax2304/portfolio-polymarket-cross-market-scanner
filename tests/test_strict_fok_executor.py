from __future__ import annotations

import polymarket_cross_scanner.strict_fok.executor as strict_module
from polymarket_cross_scanner.config import ScannerConfig
from polymarket_cross_scanner.control_state import BotControlState, save_control_state
from polymarket_cross_scanner.strict_fok.executor import StrictFokLiveExecutor

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_cross_live_executor import signal, sim  # noqa: E402, F401


def _cfg(tmp_path, **overrides):
    base = dict(
        root=tmp_path,
        cross_live_enabled=True,
        cross_live_max_shares=5.0,
        cross_live_strict_fok_enabled=True,
        cross_live_strict_fok_min_edge=0.015,
    )
    base.update(overrides)
    return ScannerConfig(**base)


def test_strict_skips_when_edge_below_min(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=True))
    executor = StrictFokLiveExecutor(cfg)
    monkeypatch.setattr(executor, "guard_reason", lambda *_a, **_k: "")
    sig = signal()
    sig.net_avg_edge = 0.005

    row = executor._execute(sig, sim())

    assert row.live_result == "SKIPPED"
    assert "strict_edge_below_min" in row.notes


def test_strict_both_filled_records_strict_both_filled(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=True))
    executor = StrictFokLiveExecutor(cfg)
    monkeypatch.setattr(executor, "guard_reason", lambda *_a, **_k: "")
    sig = signal()
    sig.net_avg_edge = 0.05
    s = sim()
    s.up_ask_levels_json = '[{"price":0.45,"size":50.0}]'
    s.down_ask_levels_json = '[{"price":0.50,"size":50.0}]'

    monkeypatch.setattr(
        strict_module,
        "place_fok_batch_limits",
        lambda **k: {
            "ok": True,
            "result": [
                {"success": True, "status": "matched", "orderID": "F1", "size_matched": 5.0},
                {"success": True, "status": "matched", "orderID": "F2", "size_matched": 5.0},
            ],
        },
    )

    row = executor._execute(sig, s)

    assert row.live_result == "STRICT_BOTH_FILLED"


def test_strict_both_rejected_records_skip_no_exposure(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=True))
    executor = StrictFokLiveExecutor(cfg)
    monkeypatch.setattr(executor, "guard_reason", lambda *_a, **_k: "")
    sig = signal()
    sig.net_avg_edge = 0.05
    s = sim()
    s.up_ask_levels_json = '[{"price":0.45,"size":50.0}]'
    s.down_ask_levels_json = '[{"price":0.50,"size":50.0}]'
    monkeypatch.setattr(
        strict_module,
        "place_fok_batch_limits",
        lambda **k: {
            "ok": True,
            "result": [
                {"success": False, "status": "rejected", "errorMsg": "no fill"},
                {"success": False, "status": "rejected", "errorMsg": "no fill"},
            ],
        },
    )

    row = executor._execute(sig, s)

    assert row.live_result == "STRICT_BOTH_REJECTED"


def test_strict_one_leg_filled_records_strict_one_leg_held_no_rescue(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=True))
    executor = StrictFokLiveExecutor(cfg)
    monkeypatch.setattr(executor, "guard_reason", lambda *_a, **_k: "")
    sig = signal()
    sig.net_avg_edge = 0.05
    s = sim()
    s.up_ask_levels_json = '[{"price":0.45,"size":50.0}]'
    s.down_ask_levels_json = '[{"price":0.50,"size":50.0}]'
    # Track that rescue/unwind helpers are NOT called
    rescue_called = []
    unwind_called = []
    monkeypatch.setattr(executor, "rescue_missing_leg", lambda **k: rescue_called.append(k) or {"filled": False, "order_id": "", "status": "", "notes": "should_not_call", "price": 0})
    monkeypatch.setattr(executor, "unwind_first_leg", lambda **k: unwind_called.append(k) or {"filled": False, "order_id": "", "status": "", "notes": "should_not_call"})

    monkeypatch.setattr(
        strict_module,
        "place_fok_batch_limits",
        lambda **k: {
            "ok": True,
            "result": [
                {"success": True, "status": "matched", "orderID": "F1", "size_matched": 5.0},
                {"success": False, "status": "rejected", "errorMsg": "no fill"},
            ],
        },
    )

    row = executor._execute(sig, s)

    assert row.live_result == "STRICT_ONE_LEG_HELD"
    assert "one_leg_filled" in row.notes
    assert not rescue_called
    assert not unwind_called


def test_strict_open_results_in_no_retry_set():
    from polymarket_cross_scanner.cross_live_executor import NO_RETRY_RESULTS, OPEN_LIVE_RESULTS
    assert "STRICT_BOTH_FILLED" in OPEN_LIVE_RESULTS
    assert "STRICT_ONE_LEG_HELD" in OPEN_LIVE_RESULTS
    assert "STRICT_ONE_FILLED_RESCUE" in OPEN_LIVE_RESULTS
    assert "STRICT_ONE_FILLED_UNWIND_FAILED" in OPEN_LIVE_RESULTS
    assert "STRICT_BOTH_FILLED" in NO_RETRY_RESULTS
    assert "STRICT_ONE_LEG_HELD" in NO_RETRY_RESULTS
    assert "STRICT_ONE_FILLED_UNWIND_FILLED" in NO_RETRY_RESULTS
    # B's variants also coordinated
    assert "MAKER_BOTH_FILLED" in NO_RETRY_RESULTS
    assert "MAKER_PENDING" in NO_RETRY_RESULTS


def test_strict_one_leg_filled_with_rescue_enabled_calls_rescue(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, cross_live_strict_fok_rescue_enabled=True)
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=True))
    executor = StrictFokLiveExecutor(cfg)
    monkeypatch.setattr(executor, "guard_reason", lambda *_a, **_k: "")
    sig = signal()
    sig.net_avg_edge = 0.05
    s = sim()
    s.up_ask_levels_json = '[{"price":0.45,"size":50.0}]'
    s.down_ask_levels_json = '[{"price":0.50,"size":50.0}]'
    rescue_called = []
    monkeypatch.setattr(executor, "rescue_missing_leg", lambda **k: rescue_called.append(k) or {"filled": True, "order_id": "R1", "status": "matched", "notes": "rescued", "price": 0.52})
    monkeypatch.setattr(executor, "unwind_first_leg", lambda **k: {"filled": False, "order_id": "", "status": "", "notes": "should_not_call"})
    monkeypatch.setattr(
        strict_module,
        "place_fok_batch_limits",
        lambda **k: {
            "ok": True,
            "result": [
                {"success": True, "status": "matched", "orderID": "F1", "size_matched": 5.0},
                {"success": False, "status": "rejected", "errorMsg": "no fill"},
            ],
        },
    )

    row = executor._execute(sig, s)

    assert row.live_result == "STRICT_ONE_FILLED_RESCUE"
    assert rescue_called, "rescue_missing_leg should be invoked when flag enabled"


def test_strict_one_leg_filled_with_rescue_enabled_falls_back_to_unwind(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, cross_live_strict_fok_rescue_enabled=True)
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=True))
    executor = StrictFokLiveExecutor(cfg)
    monkeypatch.setattr(executor, "guard_reason", lambda *_a, **_k: "")
    sig = signal()
    sig.net_avg_edge = 0.05
    s = sim()
    s.up_ask_levels_json = '[{"price":0.45,"size":50.0}]'
    s.down_ask_levels_json = '[{"price":0.50,"size":50.0}]'
    monkeypatch.setattr(executor, "rescue_missing_leg", lambda **k: {"filled": False, "order_id": "", "status": "rejected", "notes": "rescue_failed", "price": 0})
    monkeypatch.setattr(executor, "unwind_first_leg", lambda **k: {"filled": True, "order_id": "U1", "status": "matched", "notes": "unwound"})
    monkeypatch.setattr(
        strict_module,
        "place_fok_batch_limits",
        lambda **k: {
            "ok": True,
            "result": [
                {"success": True, "status": "matched", "orderID": "F1", "size_matched": 5.0},
                {"success": False, "status": "rejected", "errorMsg": "no fill"},
            ],
        },
    )

    row = executor._execute(sig, s)

    assert row.live_result == "STRICT_ONE_FILLED_UNWIND_FILLED"
