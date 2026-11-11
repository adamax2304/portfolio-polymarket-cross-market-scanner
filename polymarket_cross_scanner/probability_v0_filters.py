from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import csv

from .config import ScannerConfig
from .probability_settlement import SETTLED_STATUS, ProbabilitySettlement
from .probability_risk import ProbabilityRiskDecision
from .storage import write_rows_csv

TRADE_CANDIDATE = "TRADE_CANDIDATE"
OBSERVE = "OBSERVE"
AVOID = "AVOID"
OBSERVE_COOLING = "OBSERVE_COOLING"


@dataclass
class ProbabilityV0FilterDecision:
    signal_id: str
    evaluated_at: str
    asset: str
    market_slug: str
    outcome: str
    grade: str
    time_price_bucket: str
    price_bucket: str
    tte_bucket: str
    best_ask: float
    calibrated_probability: float
    calibrated_edge: float
    v0_action: str
    v0_rule: str
    v0_reasons: str
    status: str
    won: bool | None
    cost: float
    payout: float | None
    realized_profit: float | None
    roi_on_cost: float | None


@dataclass
class ProbabilityV0LiveLabel:
    evaluated_at: str
    signal_id: str
    asset: str
    market_slug: str
    outcome: str
    grade: str
    time_price_bucket: str
    best_ask: float
    calibrated_probability: float
    calibrated_edge: float
    risk_action: str
    recommended_cost_usdc: float
    recommended_shares: float
    v0_action: str
    v0_rule: str
    v0_reasons: str
    notes: str = ""


@dataclass
class ProbabilityV0FilterReportRow:
    group_name: str
    group_value: str
    trades: int
    settled: int
    pending: int
    wins: int
    losses: int
    cost: float
    payout: float
    realized_profit: float
    roi_on_cost: float | None
    win_rate: float | None
    avg_price: float | None
    avg_calibrated_probability: float | None
    avg_calibrated_edge: float | None


def classify_v0(settlement: ProbabilitySettlement) -> tuple[str, str, list[str]]:
    return classify_v0_values(
        asset=settlement.asset,
        grade=settlement.grade,
        price=settlement.best_ask,
        seconds_to_expiry=settlement.entry_seconds_to_expiry,
        calibrated_edge=settlement.calibrated_edge,
        time_price_bucket=settlement.time_price_bucket,
    )


def classify_v0_risk_decision(decision: ProbabilityRiskDecision) -> tuple[str, str, list[str]]:
    return classify_v0_values(
        asset=decision.asset,
        grade=decision.grade,
        price=decision.best_ask,
        seconds_to_expiry=time_price_bucket_tte_upper(decision.time_price_bucket),
        calibrated_edge=decision.calibrated_edge,
        time_price_bucket=decision.time_price_bucket,
    )


def classify_v0_values(
    *,
    asset: str,
    grade: str,
    price: float,
    seconds_to_expiry: int,
    calibrated_edge: float,
    time_price_bucket: str | None = None,
) -> tuple[str, str, list[str]]:
    asset = asset.lower()
    grade = grade.upper()
    tte = seconds_to_expiry
    edge = calibrated_edge
    bucket = time_price_bucket or derived_time_price_bucket(tte, price)
    reasons: list[str] = []

    if price <= 0.05:
        return AVOID, "avoid_tiny_lottery_price", ["px<=5c has been a persistent loss bucket"]
    if asset == "bnb":
        return AVOID, "avoid_bnb_until_recovery", ["bnb is negative across broad settled sample"]
    if asset == "doge" and grade != "A":
        return AVOID, "avoid_doge_non_a", ["doge broad/grade-B flow is negative"]

    if grade == "A" and 0.05 < price <= 0.25 and tte <= 300:
        return (
            OBSERVE_COOLING,
            "observe_grade_a_cheap_near_expiry_cooling",
            ["grade-A cheap near-expiry was unstable in forward shadow and is not V0.3 core"],
        )

    if grade == "A":
        return AVOID, "avoid_grade_a_outside_core_bucket", ["grade A outside the cheap near-expiry bucket is negative in current forward data"]

    if grade == "B" and bucket == "tte<=120|px<=25c":
        return (
            OBSERVE_COOLING,
            "observe_b_cheap_120s_probe_cooling",
            ["B cheap <=120s probe turned negative in forward shadow and is not V0.3 core"],
        )

    if asset == "eth" and grade == "B" and bucket == "tte<=900|px<=60c":
        return (
            TRADE_CANDIDATE,
            "v03_eth_b_900s_core",
            ["ETH grade-B 900s <=60c is the only V0.3 core probability candidate"],
        )

    if asset == "eth" and grade == "B" and bucket == "tte<=300|px<=60c":
        return (
            OBSERVE_COOLING,
            "observe_eth_b_300s_cooling",
            ["ETH grade-B 300s <=60c was negative in V0.2 forward shadow and is not V0.3 core"],
        )

    old_midprice_trap = asset in {"xrp", "sol"} and grade == "B" and 0.25 < price <= 0.50 and tte <= 300 and edge >= 0.03
    if old_midprice_trap:
        return (
            OBSERVE_COOLING,
            "observe_old_b_midprice_candidate_cooling",
            ["the old xrp/sol B midprice candidate cooled off in forward testing and is not V0.3 core"],
        )

    if asset in {"xrp", "eth", "sol"}:
        return OBSERVE, "observe_positive_asset_outside_core_bucket", ["asset has positive broad sample, but this exact bucket is not core V0 yet"]
    return OBSERVE, "observe_unproven_bucket", ["keep collecting because future regimes can change bucket performance"]


def build_v0_live_labels(decisions: list[ProbabilityRiskDecision]) -> list[ProbabilityV0LiveLabel]:
    labels: list[ProbabilityV0LiveLabel] = []
    for decision in decisions:
        action, rule, reasons = classify_v0_risk_decision(decision)
        labels.append(
            ProbabilityV0LiveLabel(
                evaluated_at=decision.evaluated_at,
                signal_id=decision.signal_id,
                asset=decision.asset,
                market_slug=decision.market_slug,
                outcome=decision.outcome,
                grade=decision.grade,
                time_price_bucket=decision.time_price_bucket,
                best_ask=decision.best_ask,
                calibrated_probability=decision.calibrated_probability,
                calibrated_edge=decision.calibrated_edge,
                risk_action=decision.action,
                recommended_cost_usdc=decision.recommended_cost_usdc,
                recommended_shares=decision.recommended_shares,
                v0_action=action,
                v0_rule=rule,
                v0_reasons="|".join(reasons),
                notes="V0 research label only; does not block probability data collection.",
            )
        )
    return labels


def build_v0_filter_decisions(settlements: list[ProbabilitySettlement]) -> list[ProbabilityV0FilterDecision]:
    decisions: list[ProbabilityV0FilterDecision] = []
    for row in settlements:
        action, rule, reasons = classify_v0(row)
        decisions.append(
            ProbabilityV0FilterDecision(
                signal_id=row.signal_id,
                evaluated_at=row.evaluated_at,
                asset=row.asset,
                market_slug=row.market_slug,
                outcome=row.outcome,
                grade=row.grade,
                time_price_bucket=row.time_price_bucket,
                price_bucket=row.price_bucket,
                tte_bucket=row.tte_bucket,
                best_ask=row.best_ask,
                calibrated_probability=row.calibrated_probability,
                calibrated_edge=row.calibrated_edge,
                v0_action=action,
                v0_rule=rule,
                v0_reasons="|".join(reasons),
                status=row.status,
                won=row.won,
                cost=row.cost,
                payout=row.payout,
                realized_profit=row.realized_profit,
                roi_on_cost=row.roi_on_cost,
            )
        )
    return decisions


def build_v0_filter_report(decisions: list[ProbabilityV0FilterDecision]) -> list[ProbabilityV0FilterReportRow]:
    groups = [
        ("v0_action", lambda r: r.v0_action),
        ("v0_rule", lambda r: r.v0_rule),
        ("v0_action_asset", lambda r: f"{r.v0_action}|{r.asset}"),
        ("v0_action_grade", lambda r: f"{r.v0_action}|{r.grade}"),
        ("v0_action_time_price", lambda r: f"{r.v0_action}|{r.time_price_bucket}"),
    ]
    report: list[ProbabilityV0FilterReportRow] = []
    for group_name, key_fn in groups:
        buckets: dict[str, list[ProbabilityV0FilterDecision]] = {}
        for decision in decisions:
            buckets.setdefault(key_fn(decision), []).append(decision)
        for group_value, rows in sorted(buckets.items()):
            report.append(v0_report_row(group_name, group_value, rows))
    return report


def v0_report_row(group_name: str, group_value: str, rows: list[ProbabilityV0FilterDecision]) -> ProbabilityV0FilterReportRow:
    settled = [row for row in rows if row.status == SETTLED_STATUS]
    pending = [row for row in rows if row.status != SETTLED_STATUS]
    wins = [row for row in settled if row.won is True]
    losses = [row for row in settled if row.won is False]
    cost = sum(row.cost for row in settled)
    payout = sum(row.payout or 0.0 for row in settled)
    profit = sum(row.realized_profit or 0.0 for row in settled)
    return ProbabilityV0FilterReportRow(
        group_name=group_name,
        group_value=group_value,
        trades=len(rows),
        settled=len(settled),
        pending=len(pending),
        wins=len(wins),
        losses=len(losses),
        cost=cost,
        payout=payout,
        realized_profit=profit,
        roi_on_cost=profit / cost if cost > 0 else None,
        win_rate=len(wins) / len(settled) if settled else None,
        avg_price=average([row.best_ask for row in settled]),
        avg_calibrated_probability=average([row.calibrated_probability for row in settled]),
        avg_calibrated_edge=average([row.calibrated_edge for row in settled]),
    )


def write_v0_filter_outputs(cfg: ScannerConfig, settlements: list[ProbabilitySettlement]) -> tuple[list[ProbabilityV0FilterDecision], list[ProbabilityV0FilterReportRow]]:
    decisions = build_v0_filter_decisions(settlements)
    report = build_v0_filter_report(decisions)
    write_rows_csv(cfg.probability_v0_filter_decisions_csv, [asdict(row) for row in decisions])
    write_rows_csv(cfg.probability_v0_filter_report_csv, [asdict(row) for row in report])
    cfg.probability_v0_filter_summary_md.write_text(summarize_v0_filter_report(report), encoding="utf-8")
    return decisions, report


def write_v0_forward_outputs(cfg: ScannerConfig, settlements: list[ProbabilitySettlement]) -> tuple[list[ProbabilityV0FilterDecision], list[ProbabilityV0FilterReportRow]]:
    labels_by_signal_id = load_v0_live_labels(cfg.probability_v0_live_labels_csv)
    decisions = build_v0_forward_decisions(settlements, labels_by_signal_id)
    report = build_v0_filter_report(decisions)
    write_rows_csv(cfg.probability_v0_forward_decisions_csv, [asdict(row) for row in decisions])
    write_rows_csv(cfg.probability_v0_forward_report_csv, [asdict(row) for row in report])
    cfg.probability_v0_forward_summary_md.write_text(summarize_v0_forward_report(report), encoding="utf-8")
    return decisions, report


def build_v0_forward_decisions(
    settlements: list[ProbabilitySettlement],
    labels_by_signal_id: dict[str, ProbabilityV0LiveLabel],
) -> list[ProbabilityV0FilterDecision]:
    decisions: list[ProbabilityV0FilterDecision] = []
    for row in settlements:
        label = labels_by_signal_id.get(row.signal_id)
        if not label:
            continue
        decisions.append(
            ProbabilityV0FilterDecision(
                signal_id=row.signal_id,
                evaluated_at=row.evaluated_at,
                asset=row.asset,
                market_slug=row.market_slug,
                outcome=row.outcome,
                grade=row.grade,
                time_price_bucket=row.time_price_bucket,
                price_bucket=row.price_bucket,
                tte_bucket=row.tte_bucket,
                best_ask=row.best_ask,
                calibrated_probability=row.calibrated_probability,
                calibrated_edge=row.calibrated_edge,
                v0_action=label.v0_action,
                v0_rule=label.v0_rule,
                v0_reasons=label.v0_reasons,
                status=row.status,
                won=row.won,
                cost=row.cost,
                payout=row.payout,
                realized_profit=row.realized_profit,
                roi_on_cost=row.roi_on_cost,
            )
        )
    return decisions


def load_v0_live_labels(path: Path) -> dict[str, ProbabilityV0LiveLabel]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    labels: dict[str, ProbabilityV0LiveLabel] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            signal_id = str(row.get("signal_id") or "")
            if not signal_id or signal_id in labels:
                continue
            labels[signal_id] = ProbabilityV0LiveLabel(
                evaluated_at=str(row.get("evaluated_at") or ""),
                signal_id=signal_id,
                asset=str(row.get("asset") or ""),
                market_slug=str(row.get("market_slug") or ""),
                outcome=str(row.get("outcome") or ""),
                grade=str(row.get("grade") or ""),
                time_price_bucket=str(row.get("time_price_bucket") or ""),
                best_ask=float(row.get("best_ask") or 0.0),
                calibrated_probability=float(row.get("calibrated_probability") or 0.0),
                calibrated_edge=float(row.get("calibrated_edge") or 0.0),
                risk_action=str(row.get("risk_action") or ""),
                recommended_cost_usdc=float(row.get("recommended_cost_usdc") or 0.0),
                recommended_shares=float(row.get("recommended_shares") or 0.0),
                v0_action=str(row.get("v0_action") or ""),
                v0_rule=str(row.get("v0_rule") or ""),
                v0_reasons=str(row.get("v0_reasons") or ""),
                notes=str(row.get("notes") or ""),
            )
    return labels


def summarize_v0_filter_report(report: list[ProbabilityV0FilterReportRow]) -> str:
    lines = [
        "# Probability V0 Filter Report",
        "",
        "V0 filters are research labels, not hard data-collection limits. The bot should keep logging all observations/signals while we forward-test the candidate subset.",
        "",
        "| Group | Value | Settled | ROI | PnL | Win Rate |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    priority_groups = {"v0_action", "v0_rule", "v0_action_asset"}
    for row in report:
        if row.group_name not in priority_groups:
            continue
        roi = "" if row.roi_on_cost is None else f"{row.roi_on_cost * 100:.2f}%"
        win_rate = "" if row.win_rate is None else f"{row.win_rate * 100:.2f}%"
        lines.append(f"| {row.group_name} | {row.group_value} | {row.settled} | {roi} | {row.realized_profit:.2f} | {win_rate} |")
    lines.extend(
        [
            "",
            "Current V0 intent:",
            "",
            "- TRADE_CANDIDATE: subset worth forward paper-testing as the first possible trading system.",
            "- OBSERVE / OBSERVE_COOLING: keep collecting; do not trade as core V0 yet.",
            "- AVOID: obvious current loss buckets; still collect data to detect recovery/regime change.",
        ]
    )
    return "\n".join(lines) + "\n"


def summarize_v0_forward_report(report: list[ProbabilityV0FilterReportRow]) -> str:
    summary = summarize_v0_filter_report(report)
    return summary.replace("# Probability V0 Filter Report", "# Probability V0 Forward Report", 1).replace(
        "V0 filters are research labels, not hard data-collection limits. The bot should keep logging all observations/signals while we forward-test the candidate subset.",
        "Forward report includes only settlements that have a live V0 label, so it measures the post-deployment V0 strategy instead of the backfilled sample.",
        1,
    )


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def time_price_bucket_tte_upper(bucket: str) -> int:
    first = str(bucket or "").split("|", 1)[0]
    if first.startswith("tte<="):
        try:
            return int(first.replace("tte<=", ""))
        except ValueError:
            return 10**9
    return 10**9


def derived_time_price_bucket(tte: int, price: float) -> str:
    if tte <= 60:
        time_bucket = "tte<=60"
    elif tte <= 120:
        time_bucket = "tte<=120"
    elif tte <= 300:
        time_bucket = "tte<=300"
    elif tte <= 900:
        time_bucket = "tte<=900"
    else:
        time_bucket = "tte>900"
    if price <= 0.05:
        price_bucket = "px<=5c"
    elif price <= 0.25:
        price_bucket = "px<=25c"
    elif price <= 0.60:
        price_bucket = "px<=60c"
    elif price < 0.95:
        price_bucket = "px<95c"
    else:
        price_bucket = "px>=95c"
    return f"{time_bucket}|{price_bucket}"
