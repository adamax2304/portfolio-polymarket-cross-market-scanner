"""SAFE_ONLY paper shadow analyzer.

Tails ``cross_hft_paper_execution_sim.csv`` from the active scanner and applies the
SAFE_ONLY accounting rules. Writes:

  data/safe_only_paper_decisions.csv  — one row per signal with decision
  data/safe_only_paper_summary.csv    — rolling per-window summary
  data/safe_only_paper_heartbeat.csv  — health metric for ops

Decision rules:
  net_edge < min_edge                              -> SKIP_EDGE
  final_action == BOTH_FILL_STRICT|SIMULTANEOUS    -> SUCCESS_BOTH_FILL  (profit = locked_profit_estimate)
  final_action == HEDGE_RESCUE                     -> SKIP_RESCUE        (would not enter)
  final_action == UNWIND_LOSS                      -> HELD_ONE_LEG       (0 PnL on average; lottery)
  any partial fill action                          -> HELD_ONE_LEG       (0 PnL on average; lottery)
  NO_FIRST_LEG_FILL/NO_DYNAMIC_SIZE_FILL/...       -> SKIP_REJECT        (0 PnL)
"""

from __future__ import annotations

import asyncio
import csv
import logging
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import ScannerConfig
from ..storage import append_csv
from ..utils import utc_now


LOGGER = logging.getLogger(__name__)


SUCCESS_ACTIONS = {"BOTH_FILL_STRICT", "BOTH_FILL_SIMULTANEOUS"}
HELD_ONE_LEG_ACTIONS = {"HEDGE_RESCUE", "UNWIND_LOSS"}
SKIP_ACTIONS = {
    "NO_FIRST_LEG_FILL",
    "NO_DYNAMIC_SIZE_FILL",
    "ONE_LEG_STUCK",
    "STRICT_PAIR_COST_OR_DEPTH_FAILED",
    "FIRST_LEG_DEPTH_OR_LIMIT_FAILED",
}


@dataclass
class SafeOnlyDecision:
    observed_at: str
    simulated_at: str
    signal_id: str
    asset: str
    end_ts: int
    target_shares: float
    final_action: str
    net_edge: float
    locked_profit_estimate: float
    decision: str
    expected_pnl: float
    min_edge: float


@dataclass
class SafeOnlyHeartbeat:
    emitted_at: str
    sims_seen: int
    decisions_recorded: int
    successes: int
    holds: int
    skips: int
    expected_pnl_total: float


@dataclass
class SafeOnlySummaryRow:
    observed_at: str
    min_edge: float
    sims_seen_total: int
    success_total: int
    held_total: int
    skip_edge_total: int
    skip_reject_total: int
    skip_other_total: int
    expected_pnl_usd: float


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def classify(sim: dict[str, Any], *, min_edge: float) -> tuple[str, float]:
    """Return ``(decision, expected_pnl)`` for one sim row."""

    net_edge = _safe_float(sim.get("final_pair_edge")) or _safe_float(sim.get("locked_profit_estimate")) / max(
        _safe_float(sim.get("target_shares")) or 1.0, 1e-9
    )
    final_action = (sim.get("final_action") or "").strip()
    locked_profit = _safe_float(sim.get("locked_profit_estimate"))

    if net_edge < min_edge:
        return "SKIP_EDGE", 0.0
    if final_action in SUCCESS_ACTIONS:
        return "SUCCESS_BOTH_FILL", locked_profit
    if final_action in HELD_ONE_LEG_ACTIONS:
        return "HELD_ONE_LEG", 0.0
    if final_action in SKIP_ACTIONS:
        return "SKIP_REJECT", 0.0
    return "SKIP_OTHER", 0.0


def decide(sim: dict[str, Any], *, min_edge: float) -> SafeOnlyDecision:
    decision, expected_pnl = classify(sim, min_edge=min_edge)
    return SafeOnlyDecision(
        observed_at=utc_now(),
        simulated_at=(sim.get("simulated_at") or "")[:25],
        signal_id=(sim.get("signal_id") or "")[:120],
        asset=(sim.get("asset") or "")[:8],
        end_ts=_safe_int(sim.get("end_ts")),
        target_shares=_safe_float(sim.get("target_shares")),
        final_action=(sim.get("final_action") or "")[:32],
        net_edge=_safe_float(sim.get("final_pair_edge")),
        locked_profit_estimate=_safe_float(sim.get("locked_profit_estimate")),
        decision=decision,
        expected_pnl=expected_pnl,
        min_edge=min_edge,
    )


def _csv_tail_dict(path: Path, *, start_byte: int) -> tuple[list[dict[str, Any]], int]:
    """Read new rows from a CSV starting at byte ``start_byte``. Returns ``(rows, new_byte)``."""

    if not path.exists() or path.stat().st_size <= start_byte:
        return [], start_byte
    with open(path, "r", encoding="utf-8", newline="") as f:
        header_line = f.readline()
        if not header_line:
            return [], path.stat().st_size
        headers = next(csv.reader([header_line]))
        # If start_byte is 0 we are reading the file for the first time; skip the header but
        # treat all data as new. Otherwise seek to the previous tail position.
        if start_byte > len(header_line):
            f.seek(start_byte)
        rows: list[dict[str, Any]] = []
        reader = csv.reader(f)
        for raw in reader:
            if not raw or len(raw) < len(headers):
                continue
            rows.append(dict(zip(headers, raw)))
        return rows, path.stat().st_size


async def run_safe_only_paper_loop(
    cfg: ScannerConfig,
    *,
    seconds: int,
    min_edge: float,
    poll_seconds: float = 5.0,
    heartbeat_interval_seconds: float = 30.0,
) -> None:
    decisions_csv = cfg.data_dir / "safe_only_paper_decisions.csv"
    summary_csv = cfg.data_dir / "safe_only_paper_summary.csv"
    heartbeat_csv = cfg.data_dir / "safe_only_paper_heartbeat.csv"
    sim_csv = cfg.cross_hft_paper_execution_csv

    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    # Resume from end-of-file so we don't double-count historical rows on every restart.
    cursor = sim_csv.stat().st_size if sim_csv.exists() else 0
    LOGGER.info("safe_only paper starting; sim_csv=%s cursor=%s min_edge=%s", sim_csv, cursor, min_edge)

    stats = Counter()
    deadline = time.monotonic() + seconds if seconds > 0 else None
    last_heartbeat = 0.0

    while True:
        if deadline is not None and time.monotonic() > deadline:
            break

        rows, new_cursor = _csv_tail_dict(sim_csv, start_byte=cursor)
        cursor = new_cursor

        decisions: list[SafeOnlyDecision] = []
        for row in rows:
            d = decide(row, min_edge=min_edge)
            decisions.append(d)
            stats[d.decision] += 1
            stats["expected_pnl_cents"] += int(round(d.expected_pnl * 100))
            stats["sims_seen"] += 1

        if decisions:
            append_csv(decisions_csv, decisions)
            # Aggregated rolling summary line: easy to grep / chart.
            append_csv(
                summary_csv,
                [
                    SafeOnlySummaryRow(
                        observed_at=utc_now(),
                        min_edge=min_edge,
                        sims_seen_total=stats["sims_seen"],
                        success_total=stats["SUCCESS_BOTH_FILL"],
                        held_total=stats["HELD_ONE_LEG"],
                        skip_edge_total=stats["SKIP_EDGE"],
                        skip_reject_total=stats["SKIP_REJECT"],
                        skip_other_total=stats["SKIP_OTHER"],
                        expected_pnl_usd=stats["expected_pnl_cents"] / 100.0,
                    )
                ],
            )

        now = time.monotonic()
        if now - last_heartbeat >= heartbeat_interval_seconds:
            hb = SafeOnlyHeartbeat(
                emitted_at=utc_now(),
                sims_seen=stats["sims_seen"],
                decisions_recorded=stats["sims_seen"],
                successes=stats["SUCCESS_BOTH_FILL"],
                holds=stats["HELD_ONE_LEG"],
                skips=stats["SKIP_EDGE"] + stats["SKIP_REJECT"] + stats["SKIP_OTHER"],
                expected_pnl_total=stats["expected_pnl_cents"] / 100.0,
            )
            append_csv(heartbeat_csv, [hb])
            LOGGER.info(
                "safe_only paper heartbeat: sims=%s success=%s held=%s skip_edge=%s skip_reject=%s pnl=%s",
                hb.sims_seen,
                hb.successes,
                hb.holds,
                stats["SKIP_EDGE"],
                stats["SKIP_REJECT"],
                f"${hb.expected_pnl_total:.2f}",
            )
            last_heartbeat = now

        await asyncio.sleep(poll_seconds)

    LOGGER.info("safe_only paper loop exited; final stats=%s", dict(stats))
