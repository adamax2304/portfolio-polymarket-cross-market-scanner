from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import os
import subprocess
import threading
import uuid

from .envfile import load_env_file


class PersistentBridgeClient:
    def __init__(
        self,
        *,
        server_path: Path,
        bridge_path: Path | None = None,
        bridge_env_path: Path | None,
        python_cmd: str,
        cwd: Path | None = None,
    ):
        self.server_path = server_path
        self.bridge_path = bridge_path
        self.bridge_env_path = bridge_env_path
        self.python_cmd = python_cmd
        self.cwd = cwd or server_path.parent.parent
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None

    def request(self, command: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
        with self.lock:
            proc = self._ensure_proc()
            if not proc or not proc.stdin or not proc.stdout:
                return None
            req_id = uuid.uuid4().hex
            request = {"id": req_id, "command": command, **(payload or {})}
            try:
                proc.stdin.write(json.dumps(request) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
                if not line:
                    self.close()
                    return None
                response = json.loads(line)
                if str(response.get("id")) != req_id:
                    return None
                return response if isinstance(response, dict) else None
            except Exception:
                self.close()
                return None

    def start(self) -> bool:
        with self.lock:
            return self._ensure_proc() is not None

    def _ensure_proc(self) -> subprocess.Popen | None:
        if self.proc and self.proc.poll() is None:
            return self.proc
        if not self.server_path.exists():
            return None
        env = dict(os.environ)
        if self.bridge_env_path and self.bridge_env_path.exists():
            for key, value in load_env_file(self.bridge_env_path, override=False).items():
                env.setdefault(key, value)
        if self.bridge_path:
            env.setdefault("POLY_CLOB_BRIDGE_PATH", str(self.bridge_path))
        if self.bridge_env_path:
            env.setdefault("POLY_CLOB_ENV_PATH", str(self.bridge_env_path))
        try:
            self.proc = subprocess.Popen(
                [self.python_cmd, str(self.server_path)],
                cwd=str(self.cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                env=env,
            )
            ready = self.proc.stdout.readline() if self.proc.stdout else ""
            payload = json.loads(ready or "{}")
            if not payload.get("ready"):
                self.close()
                return None
            return self.proc
        except Exception:
            self.close()
            return None

    def close(self) -> None:
        proc = self.proc
        self.proc = None
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass


def bridge_call(
    *,
    bridge_path: Path,
    bridge_env_path: Path | None,
    python_cmd: str,
    command: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    if bridge_env_path:
        load_env_file(bridge_env_path, override=False)
    env = dict(os.environ)
    if bridge_env_path:
        for key, value in load_env_file(bridge_env_path, override=False).items():
            env.setdefault(key, value)
    proc = subprocess.run(
        [python_cmd, str(bridge_path), command],
        input=json.dumps(payload or {}),
        text=True,
        capture_output=True,
        timeout=timeout,
        cwd=str(bridge_path.parent.parent),
        env=env,
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        return {"ok": False, "error": proc.stderr.strip() or f"bridge exited {proc.returncode}"}
    try:
        parsed = json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": proc.stdout.strip() or proc.stderr.strip() or "bridge returned invalid JSON"}
    if not isinstance(parsed, dict):
        return {"ok": False, "error": "bridge returned non-object JSON"}
    return parsed


def bridge_call_fast(
    *,
    client: PersistentBridgeClient | None,
    bridge_path: Path,
    bridge_env_path: Path | None,
    python_cmd: str,
    command: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    if client:
        result = client.request(command, payload or {})
        if result is not None:
            result.setdefault("_bridge_mode", "persistent")
            return result
    result = bridge_call(
        bridge_path=bridge_path,
        bridge_env_path=bridge_env_path,
        python_cmd=python_cmd,
        command=command,
        payload=payload,
        timeout=timeout,
    )
    result.setdefault("_bridge_mode", "subprocess")
    return result


def get_collateral_balance(
    *,
    bridge_path: Path,
    bridge_env_path: Path | None,
    python_cmd: str,
    client: PersistentBridgeClient | None = None,
) -> dict[str, Any]:
    return bridge_call_fast(
        client=client,
        bridge_path=bridge_path,
        bridge_env_path=bridge_env_path,
        python_cmd=python_cmd,
        command="get_balance_allowance",
        payload={"asset_type": "COLLATERAL"},
        timeout=45.0,
    )


def place_limit(
    *,
    bridge_path: Path,
    bridge_env_path: Path | None,
    python_cmd: str,
    client: PersistentBridgeClient | None = None,
    token_id: str,
    side: str,
    price: float,
    size: float,
    order_type: str = "FOK",
    post_only: bool = False,
) -> dict[str, Any]:
    return bridge_call_fast(
        client=client,
        bridge_path=bridge_path,
        bridge_env_path=bridge_env_path,
        python_cmd=python_cmd,
        command="place_limit",
        payload={
            "token_id": str(token_id),
            "side": side,
            "price": float(price),
            "size": float(size),
            "order_type": order_type,
            "post_only": bool(post_only),
        },
        timeout=45.0,
    )


def place_post_only_gtc_limit(
    *,
    bridge_path: Path,
    bridge_env_path: Path | None,
    python_cmd: str,
    client: PersistentBridgeClient | None = None,
    token_id: str,
    side: str,
    price: float,
    size: float,
) -> dict[str, Any]:
    """Submit a POST_ONLY GTC limit order. Used for the cheap leg in maker-first mode.

    The order rests in the book; if it would cross the spread the venue rejects it instead of
    converting to a taker, which is exactly what we want — we only want maker fills."""

    return place_limit(
        bridge_path=bridge_path,
        bridge_env_path=bridge_env_path,
        python_cmd=python_cmd,
        client=client,
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        order_type="GTC",
        post_only=True,
    )


def cancel_order(
    *,
    bridge_path: Path,
    bridge_env_path: Path | None,
    python_cmd: str,
    client: PersistentBridgeClient | None = None,
    order_id: str,
) -> dict[str, Any]:
    return bridge_call_fast(
        client=client,
        bridge_path=bridge_path,
        bridge_env_path=bridge_env_path,
        python_cmd=python_cmd,
        command="cancel_order",
        payload={"order_id": str(order_id)},
        timeout=30.0,
    )


def place_fok_limit(
    *,
    bridge_path: Path,
    bridge_env_path: Path | None,
    python_cmd: str,
    client: PersistentBridgeClient | None = None,
    token_id: str,
    side: str,
    price: float,
    size: float,
) -> dict[str, Any]:
    return place_limit(
        bridge_path=bridge_path,
        bridge_env_path=bridge_env_path,
        python_cmd=python_cmd,
        client=client,
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        order_type="FOK",
    )


def place_fok_batch_limits(
    *,
    bridge_path: Path,
    bridge_env_path: Path | None,
    python_cmd: str,
    client: PersistentBridgeClient | None = None,
    orders: list[dict[str, Any]],
) -> dict[str, Any]:
    return bridge_call_fast(
        client=client,
        bridge_path=bridge_path,
        bridge_env_path=bridge_env_path,
        python_cmd=python_cmd,
        command="place_batch_limits",
        payload={"orders": orders},
        timeout=60.0,
    )


def get_order_status(
    *,
    bridge_path: Path,
    bridge_env_path: Path | None,
    python_cmd: str,
    client: PersistentBridgeClient | None = None,
    order_id: str,
) -> dict[str, Any]:
    return bridge_call_fast(
        client=client,
        bridge_path=bridge_path,
        bridge_env_path=bridge_env_path,
        python_cmd=python_cmd,
        command="get_order",
        payload={"order_id": str(order_id)},
        timeout=45.0,
    )


def get_order_book(
    *,
    bridge_path: Path,
    bridge_env_path: Path | None,
    python_cmd: str,
    client: PersistentBridgeClient | None = None,
    token_id: str,
) -> dict[str, Any]:
    return bridge_call_fast(
        client=client,
        bridge_path=bridge_path,
        bridge_env_path=bridge_env_path,
        python_cmd=python_cmd,
        command="get_order_book",
        payload={"token_id": str(token_id)},
        timeout=45.0,
    )


def get_positions(
    *,
    bridge_path: Path,
    bridge_env_path: Path | None,
    python_cmd: str,
    client: PersistentBridgeClient | None = None,
    size: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    return bridge_call_fast(
        client=client,
        bridge_path=bridge_path,
        bridge_env_path=bridge_env_path,
        python_cmd=python_cmd,
        command="get_positions",
        payload={"size": int(size), "offset": int(offset)},
        timeout=45.0,
    )


def redeem_condition(
    *,
    bridge_path: Path,
    bridge_env_path: Path | None,
    python_cmd: str,
    client: PersistentBridgeClient | None = None,
    condition_id: str,
    command: str,
) -> dict[str, Any]:
    return bridge_call_fast(
        client=client,
        bridge_path=bridge_path,
        bridge_env_path=bridge_env_path,
        python_cmd=python_cmd,
        command=command,
        payload={"condition_id": str(condition_id)},
        timeout=120.0,
    )
