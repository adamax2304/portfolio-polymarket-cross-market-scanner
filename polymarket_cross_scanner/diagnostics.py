from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import csv
import re

from .config import ScannerConfig
from .storage import write_rows_csv
from .utils import safe_float, safe_int, utc_now


@dataclass
class CrossWindowReportRow:
    window_start: str
    window_end: str
    scans: int
    markets_max: int
    pair_candidates_max: int
    avg_pair_fresh_pct: float | None
    full_fresh_scans: int
    partial_or_missing_fresh_scans: int
    signals: int
    execution_sims: int
    missed_opportunities: int
    paper_trades: int
    hft_heartbeats: int
    hft_pairs_max: int
    hft_pair_fresh_pct_avg: float | None
    hft_updates: int
    hft_pairs_evaluated: int
    hft_opportunities: int
    likely_no_trade_reason: str
    notes: str = ""


@dataclass
class HealthMetricRow:
    name: str
    value: str
    status: str
    notes: str = ""


def write_all_diagnostics(cfg: ScannerConfig) -> None:
    window_rows = build_cross_window_report(cfg)
    write_rows_csv(cfg.cross_window_report_csv, [asdict(row) for row in window_rows])
    cfg.cross_window_summary_md.write_text(summarize_cross_window_report(window_rows), encoding="utf-8")
    cfg.health_dashboard_md.write_text(build_health_dashboard(cfg, window_rows), encoding="utf-8")


def build_cross_window_report(cfg: ScannerConfig) -> list[CrossWindowReportRow]:
    scan_rows = read_csv_rows(cfg.cross_scan_timing_csv)
    hft_rows = read_csv_rows(cfg.cross_hft_paper_heartbeat_csv)
    active = [row for row in scan_rows if safe_int(row.get("pair_candidates"), 0) > 0]
    windows: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    last_ts = None
    for row in active:
        ts = parse_ts(row.get("scanned_at"))
        if ts is None:
            continue
        if current and last_ts is not None and (ts - last_ts).total_seconds() > 90:
            windows.append(current)
            current = []
        current.append(row)
        last_ts = ts
    if current:
        windows.append(current)
    return [window_report_row(rows, hft_rows) for rows in windows[-100:]]


def window_report_row(rows: list[dict[str, str]], hft_rows: list[dict[str, str]]) -> CrossWindowReportRow:
    start = parse_ts(rows[0].get("scanned_at"))
    end = parse_ts(rows[-1].get("scanned_at"))
    start_iso = iso_or_empty(start)
    end_iso = iso_or_empty(end)
    hft_in_window = []
    for row in hft_rows:
        ts = parse_ts(row.get("ts"))
        if ts and start and end and start <= ts <= end:
            hft_in_window.append(row)
    fresh_values = [safe_float(row.get("pair_fresh_pct")) for row in rows if safe_float(row.get("pair_fresh_pct")) is not None]
    hft_fresh_values = [safe_float(row.get("pair_fresh_pct")) for row in hft_in_window if safe_float(row.get("pair_fresh_pct")) is not None]
    signals = sum(safe_int(row.get("signals"), 0) for row in rows)
    execution_sims = sum(safe_int(row.get("execution_sims"), 0) for row in rows)
    missed = sum(safe_int(row.get("missed_opportunities"), 0) for row in rows)
    paper_trades = sum(safe_int(row.get("paper_trades"), 0) for row in rows)
    hft_opps = sum(safe_int(row.get("opportunities"), 0) for row in hft_in_window)
    full_fresh = sum(1 for row in rows if safe_float(row.get("pair_fresh_pct"), 0.0) == 1.0)
    partial = sum(1 for row in rows if safe_float(row.get("pair_fresh_pct"), 0.0) < 1.0)
    return CrossWindowReportRow(
        window_start=start_iso,
        window_end=end_iso,
        scans=len(rows),
        markets_max=max((safe_int(row.get("markets"), 0) for row in rows), default=0),
        pair_candidates_max=max((safe_int(row.get("pair_candidates"), 0) for row in rows), default=0),
        avg_pair_fresh_pct=average([x for x in fresh_values if x is not None]),
        full_fresh_scans=full_fresh,
        partial_or_missing_fresh_scans=partial,
        signals=signals,
        execution_sims=execution_sims,
        missed_opportunities=missed,
        paper_trades=paper_trades,
        hft_heartbeats=len(hft_in_window),
        hft_pairs_max=max((safe_int(row.get("pairs"), 0) for row in hft_in_window), default=0),
        hft_pair_fresh_pct_avg=average([x for x in hft_fresh_values if x is not None]),
        hft_updates=sum(safe_int(row.get("updates_drained"), 0) for row in hft_in_window),
        hft_pairs_evaluated=sum(safe_int(row.get("pairs_evaluated"), 0) for row in hft_in_window),
        hft_opportunities=hft_opps,
        likely_no_trade_reason=likely_window_reason(signals, hft_opps, full_fresh, partial),
        notes="Aligned-expiry window detected from baseline pair_candidates>0.",
    )


def likely_window_reason(signals: int, hft_opps: int, full_fresh: int, partial: int) -> str:
    if signals > 0 or hft_opps > 0:
        return "trade_or_opportunity_seen"
    if full_fresh <= 0 and partial > 0:
        return "missing_or_unbuyable_pair_leg"
    if full_fresh > 0:
        return "no_edge_or_min_size"
    return "insufficient_pair_freshness"


def summarize_cross_window_report(rows: list[CrossWindowReportRow]) -> str:
    total_windows = len(rows)
    windows_with_trade = sum(1 for row in rows if row.signals > 0 or row.hft_opportunities > 0 or row.paper_trades > 0)
    lines = [
        "# Cross-Market Window Report",
        "",
        "Aligned-expiry windows are periods where deterministic cross-market pairs existed (`pair_candidates > 0`).",
        "",
        f"- windows: {total_windows}",
        f"- windows with signal/opportunity/trade: {windows_with_trade}",
        f"- latest generated at: {utc_now()}",
        "",
        "| Start | End | Pairs Max | Avg Fresh | Signals | HFT Opps | Paper Trades | Reason |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows[-30:]:
        fresh = "" if row.avg_pair_fresh_pct is None else f"{row.avg_pair_fresh_pct * 100:.1f}%"
        lines.append(
            f"| {row.window_start} | {row.window_end} | {row.pair_candidates_max} | {fresh} | {row.signals} | {row.hft_opportunities} | {row.paper_trades} | {row.likely_no_trade_reason} |"
        )
    return "\n".join(lines) + "\n"


def build_health_dashboard(cfg: ScannerConfig, window_rows: list[CrossWindowReportRow] | None = None) -> str:
    window_rows = window_rows if window_rows is not None else build_cross_window_report(cfg)
    metrics = build_health_metrics(cfg, window_rows)
    lines = [
        "# Bot Health Dashboard",
        "",
        f"Generated at: {utc_now()}",
        "",
        "| Metric | Value | Status | Notes |",
        "| --- | ---: | --- | --- |",
    ]
    for metric in metrics:
        lines.append(f"| {metric.name} | {metric.value} | {metric.status} | {metric.notes} |")
    lines.extend(
        [
            "",
            "## Key Files",
            "",
            f"- Probability V0.3 shadow: `{cfg.probability_v01_shadow_summary_md}`",
            f"- Probability V0.3 core-only: `{cfg.probability_v01_core_summary_md}`",
            f"- Cross window report: `{cfg.cross_window_summary_md}`",
        ]
    )
    return "\n".join(lines) + "\n"


def build_health_metrics(cfg: ScannerConfig, window_rows: list[CrossWindowReportRow]) -> list[HealthMetricRow]:
    prob_log = cfg.root / "logs" / "probability_risk_stderr.log"
    cross_log = cfg.root / "logs" / "cross_hft_paper_stderr.log"
    scanner_log = cfg.root / "logs" / "scanner_livelike_stderr.log"
    shadow_entries = read_csv_rows(cfg.probability_v01_shadow_entries_csv)
    shadow_ledger = read_csv_rows(cfg.probability_v01_shadow_ledger_csv)
    shadow_report = read_csv_rows(cfg.probability_v01_shadow_report_csv)
    all_shadow = next((row for row in shadow_report if row.get("group_name") == "all"), {})
    latest_prob = latest_ts_from_csv(cfg.probability_risk_decisions_csv, "evaluated_at")
    latest_cross = latest_ts_from_csv(cfg.cross_scan_timing_csv, "scanned_at")
    latest_hft = latest_ts_from_csv(cfg.cross_hft_paper_heartbeat_csv, "ts")
    error_count = sum(recent_error_count(path) for path in [prob_log, cross_log, scanner_log])
    last_windows = window_rows[-20:]
    return [
        freshness_metric("probability_last_decision_age_min", latest_prob, max_ok_minutes=10),
        freshness_metric("cross_baseline_last_scan_age_min", latest_cross, max_ok_minutes=5),
        freshness_metric("cross_hft_last_heartbeat_age_min", latest_hft, max_ok_minutes=5),
        HealthMetricRow("recent_log_errors", str(error_count), "OK" if error_count == 0 else "WARN", "Scans current stderr logs for error keywords."),
        HealthMetricRow("v01_shadow_entries", str(len(shadow_entries)), "OK", "Forward entries after V0.3 deployment."),
        HealthMetricRow("v01_shadow_settled", str(all_shadow.get("settled") or 0), "OK", "Settled V0.3 shadow entries."),
        HealthMetricRow("v01_shadow_roi", str(all_shadow.get("roi_on_cost") or ""), "OK", "ROI uses simulated fill cost/shares."),
        HealthMetricRow("cross_windows_recent", str(len(last_windows)), "OK", "Last 20 aligned-expiry windows retained in summary."),
        HealthMetricRow(
            "cross_windows_with_opportunity_recent",
            str(sum(1 for row in last_windows if row.signals > 0 or row.hft_opportunities > 0)),
            "OK",
            "Recent aligned windows with baseline signals or HFT opportunities.",
        ),
        HealthMetricRow("v01_shadow_pending", str(sum(1 for row in shadow_ledger if row.get("status") != "SETTLED")), "OK", "Pending entries should settle as Gamma finalPrice becomes available."),
    ]


def freshness_metric(name: str, ts: datetime | None, *, max_ok_minutes: float) -> HealthMetricRow:
    if not ts:
        return HealthMetricRow(name, "", "WARN", "No timestamp found.")
    age = (datetime.now(timezone.utc) - ts).total_seconds() / 60
    return HealthMetricRow(name, f"{age:.1f}", "OK" if age <= max_ok_minutes else "WARN", "Lower is better.")


def latest_ts_from_csv(path: Path, field: str) -> datetime | None:
    rows = read_csv_rows(path)
    for row in reversed(rows):
        ts = parse_ts(row.get(field))
        if ts:
            return ts
    return None


def recent_error_count(path: Path, *, max_lines: int = 2000) -> int:
    if not path.exists():
        return 0
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-max_lines:]
    except OSError:
        return 0
    pattern = re.compile(r"Traceback| CRITICAL | Exception | ERROR ")
    return sum(1 for line in lines if pattern.search(line))


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_or_empty(value: datetime | None) -> str:
    return "" if value is None else value.replace(microsecond=0).isoformat()


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None
