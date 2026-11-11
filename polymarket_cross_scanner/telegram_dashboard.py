from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import csv
import json
import logging
import os
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import ScannerConfig
from .control_state import load_control_state, set_live_armed, set_trading_enabled
from .envfile import load_env_file
from .live_bridge import get_collateral_balance
from .utils import safe_float, safe_int, utc_now

LOGGER = logging.getLogger(__name__)


@dataclass
class TelegramDashboardConfig:
    root: Path
    token: str
    chat_id: str
    control_state_path: Path
    clob_bridge_path: Path | None
    clob_bridge_env_path: Path | None
    clob_bridge_python: str
    poll_timeout_seconds: int = 5


def config_from_env(root: Path, env_file: Path | None = None) -> TelegramDashboardConfig:
    if env_file:
        load_env_file(env_file, override=False)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    bridge_path = os.getenv("POLY_CLOB_BRIDGE_PATH", "").strip()
    bridge_env = os.getenv("POLY_CLOB_ENV_PATH", "").strip()
    return TelegramDashboardConfig(
        root=root,
        token=token,
        chat_id=chat_id,
        control_state_path=Path(os.getenv("CROSS_CONTROL_STATE_PATH", str(root / "data" / "control_state.json"))),
        clob_bridge_path=Path(bridge_path) if bridge_path else None,
        clob_bridge_env_path=Path(bridge_env) if bridge_env else None,
        clob_bridge_python=os.getenv("POLY_PYTHON_CMD", "python").strip() or "python",
    )


class TelegramDashboard:
    def __init__(self, cfg: TelegramDashboardConfig):
        self.cfg = cfg
        self.api_base = f"https://api.telegram.org/bot{cfg.token}"
        self.offset = 0
        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=2,
            status=3,
            backoff_factor=0.4,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def run_forever(self) -> None:
        if not self.cfg.token or not self.cfg.chat_id:
            raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
        try:
            self.send_dashboard("Dashboard started")
        except Exception as exc:
            LOGGER.warning("initial telegram dashboard send failed: %s", exc)
        while True:
            try:
                for update in self.get_updates():
                    try:
                        self.handle_update(update)
                    except Exception as exc:
                        LOGGER.warning("telegram update handling failed: %s", exc)
            except Exception as exc:
                LOGGER.warning("telegram dashboard loop error: %s", exc)
                time.sleep(3)

    def get_updates(self) -> list[dict[str, Any]]:
        resp = self.session.get(
            f"{self.api_base}/getUpdates",
            params={"offset": self.offset, "timeout": self.cfg.poll_timeout_seconds},
            timeout=(5, self.cfg.poll_timeout_seconds + 5),
        )
        resp.raise_for_status()
        payload = resp.json()
        updates = payload.get("result") if payload.get("ok") else []
        out = updates if isinstance(updates, list) else []
        for item in out:
            self.offset = max(self.offset, int(item.get("update_id", 0)) + 1)
        return out

    def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        callback = update.get("callback_query") or {}
        chat_id = str((message.get("chat") or callback.get("message", {}).get("chat") or {}).get("id") or "")
        if chat_id != str(self.cfg.chat_id):
            if callback.get("id"):
                self.answer_callback(str(callback["id"]), "Unauthorized")
            return
        if callback:
            self.handle_callback(callback)
            return
        text = str(message.get("text") or "").strip().lower()
        if text in {"/start", "/dashboard", "/status", "status"}:
            self.send_dashboard()

    def handle_callback(self, callback: dict[str, Any]) -> None:
        data = str(callback.get("data") or "")
        callback_id = str(callback.get("id") or "")
        msg = callback.get("message") or {}
        message_id = safe_int(msg.get("message_id"), 0)
        if callback_id:
            self.answer_callback(callback_id)
        if data == "refresh":
            self.edit_dashboard(message_id)
        elif data == "stats":
            self.edit_message(message_id, self.stats_text(), self.back_keyboard())
        elif data == "trades":
            self.edit_message(message_id, self.last_trades_text(15), self.back_keyboard())
        elif data == "balance":
            self.edit_message(message_id, self.balance_text(), self.back_keyboard())
        elif data == "health":
            self.edit_message(message_id, self.health_text(), self.back_keyboard())
        elif data == "pause":
            set_trading_enabled(self.cfg.control_state_path, False, updated_by="telegram", reason="manual_pause")
            self.edit_dashboard(message_id, prefix="Trading paused")
        elif data == "resume":
            set_trading_enabled(self.cfg.control_state_path, True, updated_by="telegram", reason="manual_resume")
            self.edit_dashboard(message_id, prefix="Trading enabled")
        elif data == "live_guard":
            self.edit_message(message_id, self.live_guard_text(), self.live_guard_keyboard())
        elif data == "live_arm_confirm":
            set_live_armed(self.cfg.control_state_path, True, updated_by="telegram", reason="manual_live_arm")
            self.edit_dashboard(message_id, prefix="Live armed")
        elif data == "live_disarm":
            set_live_armed(self.cfg.control_state_path, False, updated_by="telegram", reason="manual_live_disarm")
            self.edit_dashboard(message_id, prefix="Live disarmed")
        elif data == "back":
            self.edit_dashboard(message_id)

    def send_dashboard(self, prefix: str = "") -> None:
        self.send_message(self.dashboard_text(prefix), self.dashboard_keyboard())

    def edit_dashboard(self, message_id: int, prefix: str = "") -> None:
        self.edit_message(message_id, self.dashboard_text(prefix), self.dashboard_keyboard())

    def dashboard_text(self, prefix: str = "") -> str:
        state = load_control_state(self.cfg.control_state_path)
        health = read_health_metrics(self.cfg.root / "data" / "bot_health_dashboard.md")
        cross_age = health.get("cross_baseline_last_scan_age_min", "n/a")
        hft_age = health.get("cross_hft_last_heartbeat_age_min", "n/a")
        prob_age = health.get("probability_last_decision_age_min", "n/a")
        parts = []
        if prefix:
            parts.append(prefix)
            parts.append("")
        parts.extend(
            [
                "Cross-market dashboard",
                f"Updated: {utc_now()}",
                "",
                f"Trading: {'ENABLED' if state.trading_enabled else 'PAUSED'}",
                f"Live: {'ARMED' if state.live_armed else 'DISARMED'}",
                "Mode now: paper/data collection, live guarded",
                "",
                f"Min order size: 5 shares",
                f"Cross scan age: {cross_age} min",
                f"HFT heartbeat age: {hft_age} min",
                f"Probability age: {prob_age} min",
            ]
        )
        return "\n".join(parts)

    def dashboard_keyboard(self) -> dict[str, Any]:
        state = load_control_state(self.cfg.control_state_path)
        toggle = {"text": "Pause bot", "callback_data": "pause"} if state.trading_enabled else {"text": "Enable bot", "callback_data": "resume"}
        return {
            "inline_keyboard": [
                [{"text": "Stats", "callback_data": "stats"}, {"text": "Last 15 trades", "callback_data": "trades"}],
                [{"text": "Balance", "callback_data": "balance"}, {"text": "Health", "callback_data": "health"}],
                [toggle, {"text": "Live guard", "callback_data": "live_guard"}],
                [{"text": "Refresh", "callback_data": "refresh"}],
            ]
        }

    def live_guard_keyboard(self) -> dict[str, Any]:
        state = load_control_state(self.cfg.control_state_path)
        rows = []
        if state.live_armed:
            rows.append([{"text": "Disarm live", "callback_data": "live_disarm"}])
        else:
            rows.append([{"text": "Confirm ARM LIVE", "callback_data": "live_arm_confirm"}])
        rows.append([{"text": "Back", "callback_data": "back"}])
        return {"inline_keyboard": rows}

    def back_keyboard(self) -> dict[str, Any]:
        return {"inline_keyboard": [[{"text": "Back", "callback_data": "back"}, {"text": "Refresh", "callback_data": "refresh"}]]}

    def stats_text(self) -> str:
        report = load_csv(self.cfg.root / "data" / "cross_market_livelike_report.csv")
        source_rows = [r for r in report if r.get("group_name") == "source_risk"]
        all_row = next((r for r in report if r.get("group_name") == "all"), {})
        lines = [
            "Cross-market stats",
            f"Rows: {all_row.get('rows', 'n/a')} settled/modeled: {all_row.get('settled_or_modeled', 'n/a')}",
            f"PnL: {fmt_num(all_row.get('realized_or_modeled_profit'))} ROI: {fmt_pct(all_row.get('roi_on_cost'))}",
            f"Latency-adjusted ROI: {fmt_pct(all_row.get('latency_adjusted_roi_on_cost'))}",
            "",
            "By source risk:",
        ]
        for row in source_rows:
            lines.append(
                f"{row.get('group_value')}: n={row.get('settled_or_modeled')} "
                f"pnl={fmt_num(row.get('realized_or_modeled_profit'))} roi={fmt_pct(row.get('roi_on_cost'))}"
            )
        lines.extend(["", "Min live/order guard: 5 shares. price_inferred should stay OFF for live."])
        return "\n".join(lines)

    def last_trades_text(self, limit: int) -> str:
        live_orders = load_csv(self.cfg.root / "data" / "cross_market_live_orders.csv")
        live_candidates = load_csv(self.cfg.root / "data" / "cross_market_live_candidates.csv")
        rows = []
        for order in live_orders:
            rows.append((order.get("attempted_at") or "", "order", order))
        for candidate in live_candidates:
            reason = candidate.get("reason") or ""
            if reason and reason not in {"BOTH_FILLED_BATCH", "BOTH_FILLED", "BOTH_FILLED_RESCUE"}:
                rows.append((candidate.get("observed_at") or "", "candidate", candidate))
        rows.sort(key=lambda item: item[0], reverse=True)
        lines = ["Last 15 live attempts/candidates"]
        for idx, (seen_at, row_type, row) in enumerate(rows[:limit], start=1):
            asset = (row.get("asset") or "").upper()
            end_ts = row.get("end_ts") or ""
            if row_type == "order":
                result = row.get("live_result") or ""
                cost = row.get("estimated_cost")
                notes = row.get("notes") or ""
                lines.append(f"{idx}. {seen_at} {asset} {result} cost={fmt_num(cost)} end={end_ts} {shorten(notes, 54)}")
            else:
                reason = row.get("reason") or ""
                up_ask = row.get("up_ask")
                down_ask = row.get("down_ask")
                lines.append(f"{idx}. {seen_at} {asset} CANDIDATE {shorten(reason, 48)} asks={fmt_num(up_ask)}+{fmt_num(down_ask)} end={end_ts}")
        return "\n".join(lines)

    def balance_text(self) -> str:
        if not self.cfg.clob_bridge_path or not self.cfg.clob_bridge_path.exists():
            return "Balance unavailable: POLY_CLOB_BRIDGE_PATH is not configured."
        result = get_collateral_balance(
            bridge_path=self.cfg.clob_bridge_path,
            bridge_env_path=self.cfg.clob_bridge_env_path,
            python_cmd=self.cfg.clob_bridge_python,
        )
        if not result.get("ok"):
            return f"Balance error: {result.get('error') or 'unknown error'}"
        data = result.get("result") or {}
        balance = amount_6(data.get("balance"))
        effective = amount_6(data.get("effective_balance") or data.get("balance"))
        allowances = data.get("allowances") if isinstance(data.get("allowances"), dict) else {}
        allowance_values = [safe_float(value, 0.0) or 0.0 for value in allowances.values()]
        allowance_status = "approved" if any(value > 1_000_000_000 for value in allowance_values) else "limited/unknown"
        return "\n".join(
            [
                "Polymarket collateral",
                f"Balance: {balance:.4f}",
                f"Effective: {effective:.4f}",
                f"Allowance: {allowance_status}",
                "",
                "Source: live CLOB bridge get_balance_allowance(COLLATERAL)",
            ]
        )

    def health_text(self) -> str:
        health = read_health_metrics(self.cfg.root / "data" / "bot_health_dashboard.md")
        lines = ["Health"]
        for key in [
            "cross_baseline_last_scan_age_min",
            "cross_hft_last_heartbeat_age_min",
            "probability_last_decision_age_min",
            "recent_log_errors",
            "cross_windows_recent",
            "cross_windows_with_opportunity_recent",
        ]:
            lines.append(f"{key}: {health.get(key, 'n/a')}")
        return "\n".join(lines)

    def live_guard_text(self) -> str:
        state = load_control_state(self.cfg.control_state_path)
        return "\n".join(
            [
                "Live guard",
                f"Trading: {'ENABLED' if state.trading_enabled else 'PAUSED'}",
                f"Live armed: {'YES' if state.live_armed else 'NO'}",
                "",
                "Live launch should require:",
                "official priceToBeat on both legs",
                "min size >= 5 shares",
                "price_inferred OFF",
                "same-updown first, CrossFamily tiny cap",
                "rescue/kill switch enabled",
            ]
        )

    def send_message(self, text: str, markup: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": self.cfg.chat_id, "text": text}
        if markup:
            payload["reply_markup"] = json.dumps(markup)
        resp = self.session.post(f"{self.api_base}/sendMessage", json=payload, timeout=(5, 10))
        if not resp.ok:
            LOGGER.warning("telegram sendMessage failed: %s %s", resp.status_code, resp.text[:300])
            resp.raise_for_status()

    def edit_message(self, message_id: int, text: str, markup: dict[str, Any] | None = None) -> None:
        if not message_id:
            self.send_message(text, markup)
            return
        payload: dict[str, Any] = {"chat_id": self.cfg.chat_id, "message_id": message_id, "text": text}
        if markup:
            payload["reply_markup"] = json.dumps(markup)
        resp = self.session.post(f"{self.api_base}/editMessageText", json=payload, timeout=(5, 10))
        if resp.status_code == 400 and "message is not modified" in resp.text.lower():
            return
        if not resp.ok:
            LOGGER.warning("telegram editMessageText failed: %s %s", resp.status_code, resp.text[:300])
        resp.raise_for_status()

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        try:
            self.session.post(f"{self.api_base}/answerCallbackQuery", json=payload, timeout=(5, 5))
        except Exception as exc:
            LOGGER.warning("telegram answerCallbackQuery failed: %s", exc)


def load_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_health_metrics(path: Path) -> dict[str, str]:
    metrics: dict[str, str] = {}
    if not path.exists():
        return metrics
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.startswith("|") or line.startswith("| ---") or "Metric" in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) >= 2:
            metrics[cells[0]] = cells[1]
    return metrics


def amount_6(value: Any) -> float:
    raw = safe_float(value, 0.0) or 0.0
    return raw / 1_000_000 if raw > 10_000 else raw


def fmt_num(value: Any) -> str:
    number = safe_float(value)
    return "n/a" if number is None else f"{number:.4f}"


def fmt_pct(value: Any) -> str:
    number = safe_float(value)
    return "n/a" if number is None else f"{number * 100:.2f}%"


def shorten(value: str, max_len: int) -> str:
    text = (value or "").replace("\n", " ")
    return text if len(text) <= max_len else text[: max(0, max_len - 3)] + "..."


def run_dashboard(root: Path, env_file: Path | None = None) -> None:
    cfg = config_from_env(root, env_file)
    dashboard = TelegramDashboard(cfg)
    dashboard.run_forever()
