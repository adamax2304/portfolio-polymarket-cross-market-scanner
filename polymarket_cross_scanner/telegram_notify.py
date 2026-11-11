from __future__ import annotations

from pathlib import Path
import logging
import os

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import ScannerConfig
from .envfile import load_env_file
from .models import CrossMarketLiveOrder


LOGGER = logging.getLogger(__name__)


def _telegram_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=2,
        status=3,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"POST"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=2, pool_maxsize=4)
    session.mount("https://", adapter)
    return session


def notify_live_order(cfg: ScannerConfig, row: CrossMarketLiveOrder) -> None:
    root_env = cfg.root / ".env"
    if root_env.exists():
        load_env_file(root_env, override=False)
    bridge_env = Path(cfg.clob_bridge_env_path) if cfg.clob_bridge_env_path else None
    if bridge_env and bridge_env.exists():
        load_env_file(bridge_env, override=False)

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return

    text = live_order_message(row)
    try:
        resp = _telegram_session().post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=(5, 10),
        )
        if resp.status_code >= 300:
            LOGGER.warning("telegram live notification failed: %s %s", resp.status_code, resp.text[:300])
    except Exception as exc:
        LOGGER.warning("telegram live notification error: %s", exc)


def live_order_message(row: CrossMarketLiveOrder) -> str:
    notes = (row.notes or "").replace("\n", " ")
    if len(notes) > 650:
        notes = notes[:647] + "..."
    return "\n".join(
        [
            f"Cross live: {row.live_result}",
            f"asset={row.asset.upper()} source={row.source_risk}",
            f"cost={row.estimated_cost:.4f} base_shares={row.shares:.4f} latency_ms={row.bridge_latency_ms}",
            f"first={row.first_leg or '-'}:{row.first_status or '-'} second={row.second_status or '-'} unwind={row.unwind_status or '-'}",
            f"markets={row.lower_market_slug} | {row.higher_market_slug}",
            f"notes={notes}",
        ]
    )
