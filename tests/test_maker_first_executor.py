from __future__ import annotations

import pytest

import polymarket_cross_scanner.maker_first.executor as maker_module
from polymarket_cross_scanner.config import ScannerConfig
from polymarket_cross_scanner.control_state import BotControlState, save_control_state
from polymarket_cross_scanner.maker_first.executor import MakerFirstLiveExecutor

# Reuse the helper fixtures from the live executor tests.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_cross_live_executor import signal, sim  # noqa: F401, E402


def _cfg(tmp_path, **overrides):
    base = dict(
        root=tmp_path,
        cross_live_enabled=True,
        cross_live_max_shares=5.0,
        cross_live_maker_first_enabled=True,
        cross_live_maker_first_min_edge=0.03,
        cross_live_maker_timeout_ms=5000,
        cross_live_maker_cheap_leg_below=0.50,
    )
    base.update(overrides)
    return ScannerConfig(**base)


def test_maker_first_skips_when_edge_below_min(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=True))
    executor = MakerFirstLiveExecutor(cfg)
    monkeypatch.setattr(executor, "guard_reason", lambda *_a, **_k: "")
    s = sim()

    sig = signal()
    sig.net_avg_edge = 0.005  # well below 0.03 threshold

    row = executor._execute(sig, s)

    assert row.live_result == "SKIPPED"
    assert "edge_below_min" in row.notes
    assert sig.signal_id not in executor.pending_makers


def test_maker_first_skips_when_cheap_leg_too_expensive(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, cross_live_maker_cheap_leg_below=0.40)
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=True))
    executor = MakerFirstLiveExecutor(cfg)
    monkeypatch.setattr(executor, "guard_reason", lambda *_a, **_k: "")
    sig = signal()
    sig.net_avg_edge = 0.05
    s = sim()
    # Both asks are 0.45/0.50 → cheap leg = 0.46 which is above 0.40 cutoff.

    row = executor._execute(sig, s)

    assert row.live_result == "SKIPPED"
    assert "cheap_leg_too_high" in row.notes


def test_maker_first_submits_post_only_and_tracks_pending(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=True))
    executor = MakerFirstLiveExecutor(cfg)
    monkeypatch.setattr(executor, "guard_reason", lambda *_a, **_k: "")
    sig = signal()
    sig.net_avg_edge = 0.06
    # make UP very cheap so it's clearly the maker leg
    sig.up_ask = 0.25  # 5*0.25 = $1.25, padding not needed
    sig.down_ask = 0.70
    s = sim()
    s.fresh_up_ask = 0.25
    s.fresh_down_ask = 0.70
    s.up_ask_levels_json = '[{"price":0.25,"size":50.0}]'
    s.down_ask_levels_json = '[{"price":0.70,"size":50.0}]'

    submitted = []

    def fake_post_only(**k):
        submitted.append(k)
        return {"ok": True, "result": {"success": True, "status": "open", "orderID": "M1", "size_matched": 0.0}}

    monkeypatch.setattr(maker_module, "place_post_only_gtc_limit", fake_post_only)

    row = executor._execute(sig, s)

    assert row.live_result == "MAKER_PENDING"
    assert sig.signal_id in executor.pending_makers
    pending = executor.pending_makers[sig.signal_id]
    assert pending.maker_leg == "UP"
    assert pending.maker_token_id == sig.up_token_id
    assert len(submitted) == 1
    # Maker price is set BELOW the ask (ask - 0.01) so POST_ONLY doesn't cross the book.
    assert submitted[0]["price"] == pytest.approx(0.24, rel=1e-6)


def test_maker_first_polls_pending_and_submits_fok_when_filled(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=True))
    executor = MakerFirstLiveExecutor(cfg)
    monkeypatch.setattr(executor, "guard_reason", lambda *_a, **_k: "")
    sig = signal()
    sig.net_avg_edge = 0.06
    sig.up_ask = 0.25  # 5*0.25 = $1.25, padding not needed
    sig.down_ask = 0.70
    s = sim()
    s.fresh_up_ask = 0.25
    s.fresh_down_ask = 0.70
    s.up_ask_levels_json = '[{"price":0.25,"size":50.0}]'
    s.down_ask_levels_json = '[{"price":0.70,"size":50.0}]'

    monkeypatch.setattr(
        maker_module,
        "place_post_only_gtc_limit",
        lambda **k: {"ok": True, "result": {"success": True, "status": "open", "orderID": "M1", "size_matched": 0.0}},
    )
    executor._execute(sig, s)
    assert sig.signal_id in executor.pending_makers

    # Now simulate the maker order filling: get_order_status returns matched size = full.
    monkeypatch.setattr(
        maker_module,
        "get_order_status",
        lambda **k: {"ok": True, "result": {"status": "matched", "orderID": "M1", "size_matched": 5.0}},
    )
    fok_payload = {"ok": True, "result": {"success": True, "status": "matched", "orderID": "F1", "size_matched": 5.0}}
    monkeypatch.setattr(maker_module, "place_fok_limit", lambda **k: fok_payload)

    finalized = executor.poll_pending_makers()

    assert len(finalized) == 1
    assert finalized[0].live_result == "MAKER_BOTH_FILLED"
    assert sig.signal_id not in executor.pending_makers


def test_maker_first_timeout_cancels_and_logs_skip(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, cross_live_maker_timeout_ms=500)
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=True))
    executor = MakerFirstLiveExecutor(cfg)
    monkeypatch.setattr(executor, "guard_reason", lambda *_a, **_k: "")
    sig = signal()
    sig.net_avg_edge = 0.06
    sig.up_ask = 0.25  # 5*0.25 = $1.25, padding not needed
    sig.down_ask = 0.70
    s = sim()
    s.fresh_up_ask = 0.25
    s.fresh_down_ask = 0.70
    s.up_ask_levels_json = '[{"price":0.25,"size":50.0}]'
    s.down_ask_levels_json = '[{"price":0.70,"size":50.0}]'
    monkeypatch.setattr(
        maker_module,
        "place_post_only_gtc_limit",
        lambda **k: {"ok": True, "result": {"success": True, "status": "open", "orderID": "M1", "size_matched": 0.0}},
    )
    executor._execute(sig, s)
    # Force the pending maker's timeout into the past.
    executor.pending_makers[sig.signal_id].timeout_at_ms = 1
    monkeypatch.setattr(
        maker_module,
        "get_order_status",
        lambda **k: {"ok": True, "result": {"status": "open", "orderID": "M1", "size_matched": 0.0}},
    )
    cancel_called = []
    monkeypatch.setattr(maker_module, "cancel_order", lambda **k: cancel_called.append(k) or {"ok": True, "result": {}})

    finalized = executor.poll_pending_makers()

    assert len(finalized) == 1
    assert finalized[0].live_result == "MAKER_TIMEOUT_CANCELLED"
    assert cancel_called and cancel_called[0]["order_id"] == "M1"


# ----- Guard worker tests (🔴 + 🟡 mitigations) ----------------------------------

class _FakeBook:
    def __init__(self, ask=None, bid=None):
        self.best_ask = ask
        self.best_bid = bid


def _arm_pending(executor, monkeypatch, sig, s):
    monkeypatch.setattr(executor, "guard_reason", lambda *_a, **_k: "")
    monkeypatch.setattr(
        maker_module,
        "place_post_only_gtc_limit",
        lambda **k: {"ok": True, "result": {"success": True, "status": "open", "orderID": "M1", "size_matched": 0.0}},
    )
    executor._execute(sig, s)
    monkeypatch.setattr(
        maker_module,
        "get_order_status",
        lambda **k: {"ok": True, "result": {"status": "open", "orderID": "M1", "size_matched": 0.0}},
    )


def _setup_signal_sim():
    sig = signal()
    sig.net_avg_edge = 0.06
    sig.up_ask = 0.25
    sig.down_ask = 0.70
    s = sim()
    s.fresh_up_ask = 0.25
    s.fresh_down_ask = 0.70
    s.up_ask_levels_json = '[{"price":0.25,"size":50.0}]'
    s.down_ask_levels_json = '[{"price":0.70,"size":50.0}]'
    return sig, s


def test_guard_captures_initial_snapshots(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=True))
    executor = MakerFirstLiveExecutor(cfg)
    sig, s = _setup_signal_sim()
    _arm_pending(executor, monkeypatch, sig, s)
    pending = executor.pending_makers[sig.signal_id]
    assert pending.initial_cheap_ask == pytest.approx(0.25)
    assert pending.initial_expensive_ask == pytest.approx(0.70)


def test_guard_cancels_on_adverse_cheap_drop(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, cross_live_maker_guard_adverse_cheap_drop=0.02)
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=True))
    executor = MakerFirstLiveExecutor(cfg)
    sig, s = _setup_signal_sim()
    _arm_pending(executor, monkeypatch, sig, s)
    cancel_called = []
    monkeypatch.setattr(maker_module, "cancel_order", lambda **k: cancel_called.append(k) or {"ok": True, "result": {}})

    # cheap ask dropped from 0.25 to 0.22 (drop of 0.03 > 0.02 threshold)
    def lookup(tok):
        if tok == sig.up_token_id:
            return _FakeBook(ask=0.22)
        return _FakeBook(ask=0.70)

    finalized = executor.poll_pending_makers(book_lookup=lookup)
    assert len(finalized) == 1
    assert finalized[0].live_result == "MAKER_CANCELLED_ADVERSE"
    assert cancel_called  # cancel was invoked


def test_guard_cancels_on_expensive_ask_rise(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, cross_live_maker_guard_expensive_rise=0.02)
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=True))
    executor = MakerFirstLiveExecutor(cfg)
    sig, s = _setup_signal_sim()
    _arm_pending(executor, monkeypatch, sig, s)
    cancel_called = []
    monkeypatch.setattr(maker_module, "cancel_order", lambda **k: cancel_called.append(k) or {"ok": True, "result": {}})

    # expensive ask rose from 0.70 to 0.75 (rise of 0.05 > 0.02 threshold)
    def lookup(tok):
        if tok == sig.up_token_id:
            return _FakeBook(ask=0.25)
        return _FakeBook(ask=0.75)

    finalized = executor.poll_pending_makers(book_lookup=lookup)
    assert len(finalized) == 1
    assert finalized[0].live_result == "MAKER_CANCELLED_EXPENSIVE_MOVED"
    assert cancel_called


def test_guard_aborts_fok_when_edge_gone_on_fill(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, cross_live_maker_guard_min_refill_edge=0.0)
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=True))
    executor = MakerFirstLiveExecutor(cfg)
    sig, s = _setup_signal_sim()
    _arm_pending(executor, monkeypatch, sig, s)

    # Maker filled
    monkeypatch.setattr(
        maker_module,
        "get_order_status",
        lambda **k: {"ok": True, "result": {"status": "matched", "orderID": "M1", "size_matched": 5.0}},
    )
    fok_called = []
    monkeypatch.setattr(maker_module, "place_fok_limit", lambda **k: fok_called.append(k) or {"ok": True, "result": {"success": True, "status": "matched", "orderID": "F1", "size_matched": 5.0}})

    # expensive ask runaway: 0.25 (maker fill) + 0.80 (expensive) = 1.05 > 1.0 → edge negative
    def lookup(tok):
        return _FakeBook(ask=0.80) if tok == sig.down_token_id else _FakeBook(ask=0.24)

    finalized = executor.poll_pending_makers(book_lookup=lookup)
    assert len(finalized) == 1
    assert finalized[0].live_result == "MAKER_EDGE_GONE"
    assert not fok_called, "FOK on expensive leg must NOT fire when edge is gone"


def test_guard_disabled_falls_back_to_original_behavior(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, cross_live_maker_guard_enabled=False)
    save_control_state(cfg.control_state_path, BotControlState(trading_enabled=True, live_armed=True))
    executor = MakerFirstLiveExecutor(cfg)
    sig, s = _setup_signal_sim()
    _arm_pending(executor, monkeypatch, sig, s)
    # Massive adverse move — would normally fire guard, but guard is disabled so it should NOT cancel.
    cancel_called = []
    monkeypatch.setattr(maker_module, "cancel_order", lambda **k: cancel_called.append(k) or {"ok": True, "result": {}})

    def lookup(tok):
        return _FakeBook(ask=0.10) if tok == sig.up_token_id else _FakeBook(ask=0.95)

    finalized = executor.poll_pending_makers(book_lookup=lookup)
    # Nothing should happen — not filled, not timed out, guard disabled.
    assert finalized == []
    assert not cancel_called


def test_guard_codes_in_no_retry_set():
    from polymarket_cross_scanner.cross_live_executor import NO_RETRY_RESULTS, OPEN_LIVE_RESULTS
    assert "MAKER_EDGE_GONE" in OPEN_LIVE_RESULTS  # has cheap-leg exposure
    assert "MAKER_CANCELLED_ADVERSE" in NO_RETRY_RESULTS
    assert "MAKER_CANCELLED_EXPENSIVE_MOVED" in NO_RETRY_RESULTS
    assert "MAKER_CANCELLED_ADVERSE" not in OPEN_LIVE_RESULTS  # no exposure
    assert "MAKER_CANCELLED_EXPENSIVE_MOVED" not in OPEN_LIVE_RESULTS  # no exposure
