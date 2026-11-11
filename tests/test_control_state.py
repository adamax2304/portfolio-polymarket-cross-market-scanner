from polymarket_cross_scanner.control_state import (
    load_control_state,
    set_live_armed,
    set_trading_enabled,
)


def test_control_state_defaults_to_trading_enabled(tmp_path):
    state = load_control_state(tmp_path / "missing.json")

    assert state.trading_enabled is True
    assert state.live_armed is False


def test_control_state_persists_pause_and_live_guard(tmp_path):
    path = tmp_path / "control.json"

    paused = set_trading_enabled(path, False, updated_by="test", reason="pause")
    armed = set_live_armed(path, True, updated_by="test", reason="arm")
    loaded = load_control_state(path)

    assert paused.trading_enabled is False
    assert armed.live_armed is True
    assert loaded.trading_enabled is False
    assert loaded.live_armed is True
