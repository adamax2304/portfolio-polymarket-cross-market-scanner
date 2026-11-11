from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import csv
import re

from .config import ScannerConfig
from .execution_sim import LOCKED_ACTIONS
from .storage import write_rows_csv
from .utils import safe_float, safe_int


@dataclass
class CrossMarketLiveLikeLedgerRow:
    observed_at: str
    row_type: str
    engine: str
    attempt_id: str
    signal_id: str
    asset: str
    end_ts: int
    lower_market_slug: str
    higher_market_slug: str
    market_pair_type: str
    source_risk: str
    status: str
    final_action: str
    shares: float
    cost: float
    payout: float | None
    realized_profit: float | None
    modeled_profit: float | None
    latency_ms: int
    latency_haircut_cost: float
    latency_adjusted_profit: float | None
    initial_pair_cost: float | None
    final_pair_cost: float | None
    final_pair_edge: float | None
    locked_profit_estimate: float | None
    unwind_pnl: float | None
    unhedged_worst_case_loss: float
    first_leg: str
    second_leg: str
    first_filled: bool
    second_strict_filled: bool
    second_rescue_filled: bool
    unwind_filled: bool
    dry_run_path: str
    notes: str = ""


@dataclass
class CrossMarketLiveLikeReportRow:
    group_name: str
    group_value: str
    rows: int
    settled_or_modeled: int
    wins: int
    losses: int
    cost: float
    payout: float
    realized_or_modeled_profit: float
    latency_haircut_cost: float
    latency_adjusted_profit: float
    roi_on_cost: float | None
    latency_adjusted_roi_on_cost: float | None
    win_rate: float | None
    avg_latency_ms: float | None
    avg_final_pair_edge: float | None


def write_cross_livelike_outputs(cfg: ScannerConfig) -> tuple[list[CrossMarketLiveLikeLedgerRow], list[CrossMarketLiveLikeReportRow]]:
    ledger = build_cross_livelike_ledger(cfg)
    report = build_cross_livelike_report(ledger)
    write_rows_csv(cfg.cross_livelike_ledger_csv, [asdict(row) for row in ledger])
    write_rows_csv(cfg.cross_livelike_report_csv, [asdict(row) for row in report])
    cfg.cross_livelike_summary_md.write_text(summarize_cross_livelike(report), encoding="utf-8")
    return ledger, report


def build_cross_livelike_ledger(cfg: ScannerConfig) -> list[CrossMarketLiveLikeLedgerRow]:
    signals = load_signal_index(cfg.signals_csv)
    hft_events = load_signal_index(cfg.cross_hft_paper_events_csv)
    settlements = load_latest_by_id(cfg.paper_settlements_csv)
    rows: list[CrossMarketLiveLikeLedgerRow] = []

    for trade in load_csv_rows(cfg.paper_csv):
        signal_id = str(trade.get("signal_id") or "")
        settlement = settlements.get(signal_id)
        signal = signal_context(signals, signal_id)
        source = signal.get("orientation_source") or ""
        lower_slug = settlement.get("lower_market_slug") if settlement else signal.get("lower_market_slug")
        higher_slug = settlement.get("higher_market_slug") if settlement else signal.get("higher_market_slug")
        status = settlement.get("status") if settlement else normalized_trade_status(trade.get("status"))
        cost = safe_float(settlement.get("cost") if settlement else trade.get("cost"), 0.0) or 0.0
        shares = safe_float(settlement.get("shares") if settlement else trade.get("shares"), 0.0) or 0.0
        payout = safe_float(settlement.get("payout")) if settlement else None
        realized = safe_float(settlement.get("realized_profit")) if settlement else None
        latency_haircut = latency_haircut_cost(cfg, shares, cfg.cross_livelike_latency_floor_ms) if realized is not None else 0.0
        rows.append(
            CrossMarketLiveLikeLedgerRow(
                observed_at=str(settlement.get("settled_at") if settlement else trade.get("opened_at") or ""),
                row_type="PAPER_TRADE_STATUS",
                engine="paper",
                attempt_id=f"paper:{signal_id}",
                signal_id=signal_id,
                asset=str(trade.get("asset") or signal.get("asset") or ""),
                end_ts=safe_int(settlement.get("end_ts") if settlement else signal.get("end_ts"), 0),
                lower_market_slug=str(lower_slug or ""),
                higher_market_slug=str(higher_slug or ""),
                market_pair_type=market_pair_type(str(lower_slug or ""), str(higher_slug or "")),
                source_risk=source_risk_label(str(lower_slug or ""), str(higher_slug or ""), str(source)),
                status=str(status or "OPEN"),
                final_action="PAPER_SETTLED" if realized is not None else "PAPER_OPEN",
                shares=shares,
                cost=cost,
                payout=payout,
                realized_profit=realized,
                modeled_profit=realized,
                latency_ms=cfg.cross_livelike_latency_floor_ms if realized is not None else 0,
                latency_haircut_cost=latency_haircut,
                latency_adjusted_profit=(realized - latency_haircut) if realized is not None else None,
                initial_pair_cost=None,
                final_pair_cost=None,
                final_pair_edge=None,
                locked_profit_estimate=safe_float(settlement.get("locked_profit_estimate") if settlement else trade.get("locked_profit_estimate")),
                unwind_pnl=None,
                unhedged_worst_case_loss=0.0,
                first_leg="",
                second_leg="",
                first_filled=False,
                second_strict_filled=False,
                second_rescue_filled=False,
                unwind_filled=False,
                dry_run_path="paper_settlement_sync",
                notes="Status is synchronized from paper_settlements when available; raw paper_trades.csv remains append-only.",
            )
        )

    rows.extend(execution_rows(cfg, signals, cfg.cross_execution_sim_csv, "baseline"))
    rows.extend(execution_rows(cfg, hft_events | signals, cfg.cross_hft_paper_execution_csv, "cross_hft_paper"))
    return sorted(rows, key=lambda row: (row.observed_at, row.engine, row.signal_id, row.attempt_id))


def execution_rows(
    cfg: ScannerConfig,
    signals: dict[str, dict[str, str]],
    path,
    engine: str,
) -> list[CrossMarketLiveLikeLedgerRow]:
    rows: list[CrossMarketLiveLikeLedgerRow] = []
    for idx, sim in enumerate(load_csv_rows(path)):
        signal_id = str(sim.get("signal_id") or "")
        signal = signal_context(signals, signal_id)
        lower_slug = str(signal.get("lower_market_slug") or "")
        higher_slug = str(signal.get("higher_market_slug") or "")
        source = str(signal.get("orientation_source") or "")
        final_action = str(sim.get("final_action") or "")
        shares = safe_float(sim.get("target_shares"), 0.0) or 0.0
        cost, payout, modeled = modeled_execution_pnl(sim, shares)
        latency_ms = max(
            safe_int(sim.get("refetch_latency_ms"), 0),
            cfg.cross_livelike_latency_floor_ms if final_action in LOCKED_ACTIONS or final_action == "HEDGE_RESCUE" else 0,
        )
        haircut = latency_haircut_cost(cfg, shares, latency_ms) if modeled is not None else 0.0
        rows.append(
            CrossMarketLiveLikeLedgerRow(
                observed_at=str(sim.get("simulated_at") or ""),
                row_type="EXECUTION_ATTEMPT",
                engine=engine,
                attempt_id=f"{engine}:{idx}:{signal_id}",
                signal_id=signal_id,
                asset=str(sim.get("asset") or signal.get("asset") or ""),
                end_ts=safe_int(sim.get("end_ts") or signal.get("end_ts"), 0),
                lower_market_slug=lower_slug,
                higher_market_slug=higher_slug,
                market_pair_type=market_pair_type(lower_slug, higher_slug),
                source_risk=source_risk_label(lower_slug, higher_slug, source),
                status=execution_status(final_action),
                final_action=final_action,
                shares=shares,
                cost=cost,
                payout=payout,
                realized_profit=None,
                modeled_profit=modeled,
                latency_ms=latency_ms,
                latency_haircut_cost=haircut,
                latency_adjusted_profit=(modeled - haircut) if modeled is not None else None,
                initial_pair_cost=safe_float(sim.get("initial_pair_cost")),
                final_pair_cost=safe_float(sim.get("final_pair_cost")),
                final_pair_edge=safe_float(sim.get("final_pair_edge")),
                locked_profit_estimate=safe_float(sim.get("locked_profit_estimate")),
                unwind_pnl=safe_float(sim.get("unwind_pnl")),
                unhedged_worst_case_loss=safe_float(sim.get("unhedged_worst_case_loss"), 0.0) or 0.0,
                first_leg=str(sim.get("first_leg") or ""),
                second_leg=str(sim.get("second_leg") or ""),
                first_filled=truthy(sim.get("first_filled")),
                second_strict_filled=truthy(sim.get("second_strict_filled")),
                second_rescue_filled=truthy(sim.get("second_rescue_filled")),
                unwind_filled=truthy(sim.get("unwind_filled")),
                dry_run_path="clob_dry_run_payload_not_submitted",
                notes=str(sim.get("decision_reason") or ""),
            )
        )
    return rows


def modeled_execution_pnl(sim: dict[str, str], shares: float) -> tuple[float, float | None, float | None]:
    action = str(sim.get("final_action") or "")
    if action in LOCKED_ACTIONS or action == "HEDGE_RESCUE":
        cost = safe_float(sim.get("final_pair_cost"), 0.0) or 0.0
        payout = shares
        return cost, payout, payout - cost
    if action in {"UNWIND_PROFIT", "UNWIND_LOSS"}:
        first_cost = safe_float(sim.get("first_cost"), 0.0) or 0.0
        proceeds = safe_float(sim.get("unwind_proceeds"), 0.0) or 0.0
        return first_cost, proceeds, safe_float(sim.get("unwind_pnl"), proceeds - first_cost)
    if action == "ONE_LEG_STUCK":
        loss = safe_float(sim.get("unhedged_worst_case_loss"), 0.0) or 0.0
        return loss, 0.0, -loss
    return 0.0, 0.0, 0.0


def build_cross_livelike_report(ledger: list[CrossMarketLiveLikeLedgerRow]) -> list[CrossMarketLiveLikeReportRow]:
    groups = [
        ("all", lambda r: "all"),
        ("engine", lambda r: r.engine),
        ("row_type", lambda r: r.row_type),
        ("asset", lambda r: r.asset),
        ("market_pair_type", lambda r: r.market_pair_type),
        ("source_risk", lambda r: r.source_risk),
        ("engine_action", lambda r: f"{r.engine}|{r.final_action}"),
        ("engine_pair_type", lambda r: f"{r.engine}|{r.market_pair_type}"),
        ("day", lambda r: r.observed_at[:10]),
    ]
    report: list[CrossMarketLiveLikeReportRow] = []
    for group_name, key_fn in groups:
        buckets: dict[str, list[CrossMarketLiveLikeLedgerRow]] = {}
        for row in ledger:
            buckets.setdefault(key_fn(row), []).append(row)
        for group_value, rows in sorted(buckets.items()):
            report.append(report_row(group_name, group_value, rows))
    return report


def report_row(group_name: str, group_value: str, rows: list[CrossMarketLiveLikeLedgerRow]) -> CrossMarketLiveLikeReportRow:
    modeled = [row for row in rows if row.modeled_profit is not None]
    wins = [row for row in modeled if (row.modeled_profit or 0.0) > 0]
    losses = [row for row in modeled if (row.modeled_profit or 0.0) < 0]
    cost = sum(row.cost for row in modeled)
    payout = sum(row.payout or 0.0 for row in modeled)
    profit = sum(row.modeled_profit or 0.0 for row in modeled)
    haircut = sum(row.latency_haircut_cost for row in modeled)
    adjusted = sum(row.latency_adjusted_profit or 0.0 for row in modeled)
    return CrossMarketLiveLikeReportRow(
        group_name=group_name,
        group_value=group_value,
        rows=len(rows),
        settled_or_modeled=len(modeled),
        wins=len(wins),
        losses=len(losses),
        cost=cost,
        payout=payout,
        realized_or_modeled_profit=profit,
        latency_haircut_cost=haircut,
        latency_adjusted_profit=adjusted,
        roi_on_cost=profit / cost if cost > 0 else None,
        latency_adjusted_roi_on_cost=adjusted / cost if cost > 0 else None,
        win_rate=len(wins) / len(modeled) if modeled else None,
        avg_latency_ms=average([row.latency_ms for row in modeled]),
        avg_final_pair_edge=average([row.final_pair_edge for row in modeled if row.final_pair_edge is not None]),
    )


def summarize_cross_livelike(report: list[CrossMarketLiveLikeReportRow]) -> str:
    total = next((row for row in report if row.group_name == "all" and row.group_value == "all"), None)
    lines = [
        "# Cross Market Live-Like Report",
        "",
        "Paper mode remains unrestricted across all aligned-expiry pairs: 5m, 15m, 30m, 1h/hourly, 4h, and every available combination between them.",
        "This report does not filter source-risk buckets. It labels them so live readiness can be decided from data instead of hiding samples.",
        "",
    ]
    if total:
        roi = pct(total.roi_on_cost)
        adj_roi = pct(total.latency_adjusted_roi_on_cost)
        lines.append(
            f"Total rows={total.rows}, modeled/settled={total.settled_or_modeled}, cost={total.cost:.4f}, profit={total.realized_or_modeled_profit:.4f}, ROI={roi}, latency-adjusted ROI={adj_roi}."
        )
        lines.append("")
    lines.append("## Engine")
    for row in report:
        if row.group_name == "engine":
            lines.append(summary_line(row))
    lines.append("")
    lines.append("## Pair Types")
    for row in report:
        if row.group_name == "market_pair_type":
            lines.append(summary_line(row))
    lines.append("")
    lines.append("## Source Risk")
    for row in report:
        if row.group_name == "source_risk":
            lines.append(summary_line(row))
    lines.append("")
    lines.append("## Execution Actions")
    for row in report:
        if row.group_name == "engine_action":
            lines.append(summary_line(row))
    lines.append("")
    lines.append("Live-readiness rule of thumb: trust paper only after positive edge survives EXECUTION_ATTEMPT rows, source-risk buckets, latency haircut, and rescue/unwind/no-fill attempts.")
    return "\n".join(lines) + "\n"


def summary_line(row: CrossMarketLiveLikeReportRow) -> str:
    return (
        f"- {row.group_value}: rows={row.rows}, n={row.settled_or_modeled}, cost={row.cost:.4f}, "
        f"profit={row.realized_or_modeled_profit:.4f}, ROI={pct(row.roi_on_cost)}, "
        f"latency_adj_profit={row.latency_adjusted_profit:.4f}, latency_adj_ROI={pct(row.latency_adjusted_roi_on_cost)}"
    )


def market_pair_type(lower_slug: str, higher_slug: str) -> str:
    return f"{market_duration(lower_slug)} vs {market_duration(higher_slug)}"


def market_duration(slug: str) -> str:
    normalized = slug.lower()
    for label in ("5m", "15m", "30m", "4h"):
        if f"updown-{label}" in normalized or f"up-or-down-{label}" in normalized:
            return label
    if "-up-or-down-" in normalized:
        return "1h"
    match = re.search(r"updown-(\d+h)-", normalized)
    if match:
        return match.group(1)
    return "unknown"


def source_risk_label(lower_slug: str, higher_slug: str, orientation_source: str) -> str:
    if orientation_source == "price_inferred":
        return "HIGH_PRICE_INFERRED"
    lower_type = market_duration(lower_slug)
    higher_type = market_duration(higher_slug)
    if lower_type == "unknown" or higher_type == "unknown":
        return "UNKNOWN_SOURCE"
    if "-updown-" in lower_slug and "-updown-" in higher_slug:
        return "LOW_SAME_UPDOWN_FAMILY"
    if lower_type == higher_type:
        return "MEDIUM_SAME_DURATION_CROSS_FAMILY"
    return "RESEARCH_CROSS_FAMILY"


def latency_haircut_cost(cfg: ScannerConfig, shares: float, latency_ms: int) -> float:
    if shares <= 0 or latency_ms <= 0:
        return 0.0
    units = latency_ms / 100.0
    return shares * units * max(0.0, cfg.cross_livelike_latency_edge_haircut_per_100ms)


def execution_status(final_action: str) -> str:
    if final_action in LOCKED_ACTIONS:
        return "LIVE_LIKE_LOCKED"
    if final_action == "HEDGE_RESCUE":
        return "LIVE_LIKE_RESCUED"
    if final_action.startswith("UNWIND"):
        return "LIVE_LIKE_UNWOUND"
    if final_action in {"NO_FIRST_LEG_FILL", "NO_DYNAMIC_SIZE_FILL"}:
        return "LIVE_LIKE_NO_POSITION"
    if final_action == "ONE_LEG_STUCK":
        return "LIVE_LIKE_STUCK_RISK"
    return "LIVE_LIKE_REVIEW"


def normalized_trade_status(value) -> str:
    text = str(value or "")
    return text if text else "OPEN"


def load_signal_index(path) -> dict[str, dict[str, str]]:
    return load_latest_by_id(path)


def signal_context(signals: dict[str, dict[str, str]], signal_id: str) -> dict[str, str]:
    row = dict(signals.get(signal_id, {}))
    if row.get("lower_market_slug") and row.get("higher_market_slug"):
        return row
    parts = signal_id.split(":")
    if len(parts) >= 5:
        row.setdefault("asset", parts[0])
        row.setdefault("end_ts", parts[1])
        row.setdefault("lower_market_slug", parts[2])
        row.setdefault("higher_market_slug", parts[3])
        row.setdefault("orientation_source", parts[4])
    return row


def load_latest_by_id(path) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for row in load_csv_rows(path):
        signal_id = str(row.get("signal_id") or "")
        if signal_id:
            out[signal_id] = row
    return out


def load_csv_rows(path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def average(values: list[float | int]) -> float | None:
    return sum(values) / len(values) if values else None


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.2f}%"
