from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_legacy_bridge(path: Path):
    spec = importlib.util.spec_from_file_location("legacy_polymarket_clob_bridge", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot load bridge module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install_cached_clients(module: Any) -> None:
    original_build_client = module._build_client
    cache: dict[tuple[bool, bool], Any] = {}

    def cached_build_client(require_l2: bool, require_signer: bool = True):
        key = (bool(require_l2), bool(require_signer))
        client = cache.get(key)
        if client is not None:
            return client
        client = original_build_client(require_l2=require_l2, require_signer=require_signer)
        cache[key] = client
        return client

    module._build_client = cached_build_client


def plain(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def main() -> int:
    bridge_path = Path(os.getenv("POLY_CLOB_BRIDGE_PATH", "")).expanduser()
    env_path = Path(os.getenv("POLY_CLOB_ENV_PATH", "")).expanduser()
    if env_path:
        load_env_file(env_path)
    if not bridge_path.exists():
        print(json.dumps({"ready": False, "error": f"POLY_CLOB_BRIDGE_PATH missing: {bridge_path}"}), flush=True)
        return 2

    try:
        module = load_legacy_bridge(bridge_path)
        install_cached_clients(module)
        handlers = {
            "ensure_creds": module._ensure_creds,
            "place_limit": module._place_limit,
            "place_market": module._place_market,
            "place_batch_limits": module._place_batch_limits,
            "cancel_order": module._cancel,
            "get_order": module._get_order,
            "get_open_orders": module._get_open_orders,
            "get_trades": module._get_trades,
            "get_order_book": module._get_order_book,
            "get_midpoint": module._get_midpoint,
            "get_positions": module._get_positions,
            "get_balance_allowance": module._get_balance_allowance,
            "redeem": module._redeem,
            "redeem_direct": module._redeem_direct,
            "redeem_eoa_direct": module._redeem_eoa_direct,
            "sweep_eoa_to_safe": module._sweep_eoa_to_safe,
        }
    except Exception as exc:
        print(json.dumps({"ready": False, "error": str(exc)}), flush=True)
        return 1

    print(json.dumps({"ready": True, "server": "fast_clob_bridge_server"}), flush=True)
    for line in sys.stdin:
        req_id = ""
        try:
            request = json.loads(line)
            req_id = str(request.get("id") or "")
            command = str(request.get("command") or "").strip().lower()
            payload = {k: v for k, v in request.items() if k not in {"id", "command"}}
            handler = handlers.get(command)
            if not handler:
                raise RuntimeError(f"Unknown command: {command}")
            result = handler(payload)
            response = {"id": req_id, "ok": True, "result": plain(result)}
        except Exception as exc:
            response = {"id": req_id, "ok": False, "error": str(exc)}
        print(json.dumps(response), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
