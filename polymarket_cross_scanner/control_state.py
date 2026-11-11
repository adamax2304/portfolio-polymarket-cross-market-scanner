from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import json

from .utils import utc_now


@dataclass
class BotControlState:
    trading_enabled: bool = True
    live_armed: bool = False
    updated_at: str = ""
    updated_by: str = "system"
    reason: str = "default"


def default_control_state() -> BotControlState:
    return BotControlState(updated_at=utc_now())


def load_control_state(path: Path) -> BotControlState:
    if not path.exists() or path.stat().st_size == 0:
        return default_control_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default_control_state()
    if not isinstance(data, dict):
        return default_control_state()
    return BotControlState(
        trading_enabled=bool(data.get("trading_enabled", True)),
        live_armed=bool(data.get("live_armed", False)),
        updated_at=str(data.get("updated_at") or utc_now()),
        updated_by=str(data.get("updated_by") or "system"),
        reason=str(data.get("reason") or ""),
    )


def save_control_state(path: Path, state: BotControlState) -> BotControlState:
    path.parent.mkdir(parents=True, exist_ok=True)
    state.updated_at = utc_now()
    path.write_text(json.dumps(asdict(state), indent=2, sort_keys=True), encoding="utf-8")
    return state


def set_trading_enabled(path: Path, enabled: bool, *, updated_by: str, reason: str) -> BotControlState:
    state = load_control_state(path)
    state.trading_enabled = enabled
    state.updated_by = updated_by
    state.reason = reason
    return save_control_state(path, state)


def set_live_armed(path: Path, armed: bool, *, updated_by: str, reason: str) -> BotControlState:
    state = load_control_state(path)
    state.live_armed = armed
    state.updated_by = updated_by
    state.reason = reason
    return save_control_state(path, state)
