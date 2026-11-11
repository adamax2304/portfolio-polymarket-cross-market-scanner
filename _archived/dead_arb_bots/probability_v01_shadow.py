from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import csv

from .config import ScannerConfig
from .models import OrderBook
from .probability_calibrated import CalibratedProbabilitySignal
from .probability_risk import ProbabilityRiskDecision, is_enter_action
from .probability_settlement import SETTLED_STATUS, ProbabilitySettlement
from .probability_v0_filters import TRADE_CANDIDATE, ProbabilityV0LiveLabel
from .storage import write_rows_csv
from .utils import safe_float


V01_VERSION = "probability_v03_shadow_2026_06_05"
V01_CORE_RULES = {"v03_eth_b_900s_core"}
V03_MIN_SIMULATED_FILL_COST = 1.0
V03_MIN_SIMULATED_FILL_SHARES = 5.0


@dataclass
class ProbabilityV01ShadowEntry:
    evaluated_at: str
    signal_id: str
    v01_version: str
    asset: str
    market_slug: str
    outcome: str
    token_id: str
    grade: str
    time_price_bucket: str
    best_ask: float
    calibrated_probability: float
    calibrated_edge: float
    v0_rule: str
    risk_action: str
    recommended_cost_usdc: float
    recommended_shares: float
    executable_shares: float
    ask_depth_shares_1pct: float
    ask_depth_usdc_1pct: float
    entry_limit_price: float
    simulated_fill_action: str
    simulated_fill_shares: float
    simulated_fill_cost: float
    simulated_avg_price: float | None
    simulated_slippage_per_share: float | None
    book_levels_used: int
    live_readiness: str
    live_readiness_reasons: str
    notes: str = ""


@dataclass
class ProbabilityV01ShadowLedgerRow:
    evaluated_at: str
    signal_id: str
    v01_version: str
    asset: str
    market_slug: str
    outcome: str
    grade: str
    time_price_bucket: str
    best_ask: float
    calibrated_probability: float
    calibrated_edge: float
    v0_rule: str
    risk_action: str
    recommended_cost_usdc: float
    recommended_shares: float
    simulated_fill_action: str
    simulated_fill_shares: float
    simulated_fill_cost: float
    simulated_avg_price: float | None
    simulated_slippage_per_share: float | None
    live_readiness: str
    live_readiness_reasons: str
    status: str
    won: bool | None
    payout: float | None
    realized_profit: float | None
    roi_on_cost: float | None
    cumulative_profit: float | None
    drawdown: float | None
    notes: str = ""


@dataclass
class ProbabilityV01ShadowReportRow:
    group_name: str
    group_value: str
    entries: int
    settled: int
    pending: int
    wins: int
    losses: int
    cost: float
    payout: float
    realized_profit: float
    roi_on_cost: float | None
    win_rate: float | None
    max_drawdown: float | None
    avg_price: float | None
    avg_calibrated_probability: float | None
    avg_calibrated_edge: float | None


def build_v01_shadow_entries(
    decisions: list[ProbabilityRiskDecision],
    labels: list[ProbabilityV0LiveLabel],
    signals: list[CalibratedProbabilitySignal],
    books: dict[str, OrderBook],
    *,
    entry_slippage: float,
) -> list[ProbabilityV01ShadowEntry]:
    labels_by_id = {label.signal_id: label for label in labels}
    signals_by_id = {signal.signal_id: signal for signal in signals}
    out: list[ProbabilityV01ShadowEntry] = []
    for decision in decisions:
        label = labels_by_id.get(decision.signal_id)
        signal = signals_by_id.get(decision.signal_id)
        if not label or not signal:
            continue
        if label.v0_action != TRADE_CANDIDATE:
            continue
        if not is_enter_action(decision.action) or decision.recommended_cost_usdc <= 0 or decision.recommended_shares <= 0:
            continue
        out.append(
            shadow_entry_from_decision(
                decision,
                label,
                signal,
                books.get(decision.token_id),
                entry_slippage=entry_slippage,
            )
        )
    return out


def shadow_entry_from_decision(
    decision: ProbabilityRiskDecision,
    label: ProbabilityV0LiveLabel,
    signal: CalibratedProbabilitySignal,
    book: OrderBook | None,
    *,
    entry_slippage: float,
) -> ProbabilityV01ShadowEntry:
    limit_price = min(0.999, decision.best_ask + max(0.0, entry_slippage))
    fill = simulate_marketable_limit(book, target_shares=decision.recommended_shares, limit_price=limit_price)
    reasons: list[str] = []
    if fill["action"] != "FULL_FILL":
        reasons.append(fill["action"].lower())
    if decision.recommended_cost_usdc <= 0:
        reasons.append("zero_recommended_cost")
    if signal.ask_depth_shares_1pct < decision.recommended_shares:
        reasons.append("one_pct_depth_below_size")
    if decision.best_ask <= 0.05:
        reasons.append("tiny_lottery_price")
    if fill["cost"] < V03_MIN_SIMULATED_FILL_COST:
        reasons.append("simulated_cost_below_1usdc")
    if fill["shares"] < V03_MIN_SIMULATED_FILL_SHARES:
        reasons.append("simulated_shares_below_5")
    readiness = "LIVE_READY_PAPER" if not reasons else "PAPER_ONLY_REVIEW"
    return ProbabilityV01ShadowEntry(
        evaluated_at=decision.evaluated_at,
        signal_id=decision.signal_id,
        v01_version=V01_VERSION,
        asset=decision.asset,
        market_slug=decision.market_slug,
        outcome=decision.outcome,
        token_id=decision.token_id,
        grade=decision.grade,
        time_price_bucket=decision.time_price_bucket,
        best_ask=decision.best_ask,
        calibrated_probability=decision.calibrated_probability,
        calibrated_edge=decision.calibrated_edge,
        v0_rule=label.v0_rule,
        risk_action=decision.action,
        recommended_cost_usdc=decision.recommended_cost_usdc,
        recommended_shares=decision.recommended_shares,
        executable_shares=decision.executable_shares,
        ask_depth_shares_1pct=signal.ask_depth_shares_1pct,
        ask_depth_usdc_1pct=signal.ask_depth_usdc_1pct,
        entry_limit_price=limit_price,
        simulated_fill_action=fill["action"],
        simulated_fill_shares=fill["shares"],
        simulated_fill_cost=fill["cost"],
        simulated_avg_price=fill["avg_price"],
        simulated_slippage_per_share=fill["slippage"],
        book_levels_used=fill["levels_used"],
        live_readiness=readiness,
        live_readiness_reasons="|".join(reasons),
        notes="V0.3 shadow entry. Does not block normal probability data collection.",
    )


def simulate_marketable_limit(book: OrderBook | None, *, target_shares: float, limit_price: float) -> dict:
    if not book or not book.asks or target_shares <= 0:
        return {"action": "NO_BOOK", "shares": 0.0, "cost": 0.0, "avg_price": None, "slippage": None, "levels_used": 0}
    shares = 0.0
    cost = 0.0
    levels_used = 0
    best_ask = book.asks[0].price
    for level in book.asks:
        if level.price > limit_price or shares >= target_shares:
            break
        take = min(level.size, target_shares - shares)
        if take <= 0:
            continue
        shares += take
        cost += take * level.price
        levels_used += 1
    avg = cost / shares if shares > 0 else None
    action = "FULL_FILL" if shares >= target_shares - 1e-9 else ("PARTIAL_FILL" if shares > 0 else "NO_LIQUIDITY_AT_LIMIT")
    return {
        "action": action,
        "shares": shares,
        "cost": cost,
        "avg_price": avg,
        "slippage": (avg - best_ask) if avg is not None else None,
        "levels_used": levels_used,
    }


def write_v01_shadow_outputs(cfg: ScannerConfig, settlements: list[ProbabilitySettlement]) -> tuple[list[ProbabilityV01ShadowLedgerRow], list[ProbabilityV01ShadowReportRow]]:
    entries = load_v01_shadow_entries(cfg.probability_v01_shadow_entries_csv)
    settlements_by_id = {row.signal_id: row for row in settlements}
    ledger = build_v01_shadow_ledger(entries, settlements_by_id)
    report = build_v01_shadow_report(ledger)
    write_rows_csv(cfg.probability_v01_shadow_ledger_csv, [asdict(row) for row in ledger])
    write_rows_csv(cfg.probability_v01_shadow_report_csv, [asdict(row) for row in report])
    core_report = build_v01_shadow_report([row for row in ledger if is_v03_core_eligible(row)])
    write_rows_csv(cfg.probability_v01_core_report_csv, [asdict(row) for row in core_report])
    cfg.probability_v01_shadow_summary_md.write_text(summarize_v01_shadow_report(report), encoding="utf-8")
    cfg.probability_v01_core_summary_md.write_text(summarize_v01_core_report(core_report), encoding="utf-8")
    cfg.probability_v01_rules_md.write_text(v01_rules_markdown(), encoding="utf-8")
    return ledger, report


def build_v01_shadow_ledger(
    entries: list[ProbabilityV01ShadowEntry],
    settlements_by_id: dict[str, ProbabilitySettlement],
) -> list[ProbabilityV01ShadowLedgerRow]:
    rows: list[ProbabilityV01ShadowLedgerRow] = []
    cumulative = 0.0
    peak = 0.0
    for entry in sorted(dedupe_entries(entries), key=lambda row: (row.evaluated_at, row.signal_id)):
        settlement = settlements_by_id.get(entry.signal_id)
        status = settlement.status if settlement else "PENDING"
        won = settlement.won if settlement else None
        if settlement and settlement.status == SETTLED_STATUS and entry.simulated_fill_cost > 0:
            payout = entry.simulated_fill_shares if settlement.won else 0.0
            realized = payout - entry.simulated_fill_cost
            roi = realized / entry.simulated_fill_cost
            cumulative += realized
            peak = max(peak, cumulative)
            drawdown = peak - cumulative
        else:
            payout = None
            realized = None
            roi = None
            drawdown = None
        rows.append(
            ProbabilityV01ShadowLedgerRow(
                evaluated_at=entry.evaluated_at,
                signal_id=entry.signal_id,
                v01_version=entry.v01_version,
                asset=entry.asset,
                market_slug=entry.market_slug,
                outcome=entry.outcome,
                grade=entry.grade,
                time_price_bucket=entry.time_price_bucket,
                best_ask=entry.best_ask,
                calibrated_probability=entry.calibrated_probability,
                calibrated_edge=entry.calibrated_edge,
                v0_rule=entry.v0_rule,
                risk_action=entry.risk_action,
                recommended_cost_usdc=entry.recommended_cost_usdc,
                recommended_shares=entry.recommended_shares,
                simulated_fill_action=entry.simulated_fill_action,
                simulated_fill_shares=entry.simulated_fill_shares,
                simulated_fill_cost=entry.simulated_fill_cost,
                simulated_avg_price=entry.simulated_avg_price,
                simulated_slippage_per_share=entry.simulated_slippage_per_share,
                live_readiness=entry.live_readiness,
                live_readiness_reasons=entry.live_readiness_reasons,
                status=status,
                won=won,
                payout=payout,
                realized_profit=realized,
                roi_on_cost=roi,
                cumulative_profit=cumulative if realized is not None else None,
                drawdown=drawdown,
                notes="PnL uses V0.3 simulated fill cost/shares, not the generic risk decision cost.",
            )
        )
    return rows


def is_v03_core_eligible(row: ProbabilityV01ShadowLedgerRow) -> bool:
    return (
        row.v0_rule in V01_CORE_RULES
        and row.live_readiness == "LIVE_READY_PAPER"
        and row.simulated_fill_action == "FULL_FILL"
        and row.simulated_fill_cost >= V03_MIN_SIMULATED_FILL_COST
        and row.simulated_fill_shares >= V03_MIN_SIMULATED_FILL_SHARES
    )


def is_v02_core_eligible(row: ProbabilityV01ShadowLedgerRow) -> bool:
    return is_v03_core_eligible(row)


def build_v01_shadow_report(ledger: list[ProbabilityV01ShadowLedgerRow]) -> list[ProbabilityV01ShadowReportRow]:
    groups = [
        ("all", lambda r: "all"),
        ("asset", lambda r: r.asset),
        ("grade", lambda r: r.grade),
        ("rule", lambda r: r.v0_rule),
        ("time_price_bucket", lambda r: r.time_price_bucket),
        ("entry_price_bucket", lambda r: entry_price_bucket(r.best_ask)),
        ("edge_bucket", lambda r: edge_bucket(r.calibrated_edge)),
        ("rule_time_price_bucket", lambda r: f"{r.v0_rule}|{r.time_price_bucket}"),
        ("rule_edge_bucket", lambda r: f"{r.v0_rule}|{edge_bucket(r.calibrated_edge)}"),
        ("live_readiness", lambda r: r.live_readiness),
        ("live_readiness_reason", lambda r: readiness_reason_bucket(r.live_readiness_reasons)),
        ("day", lambda r: r.evaluated_at[:10]),
    ]
    report: list[ProbabilityV01ShadowReportRow] = []
    for group_name, key_fn in groups:
        buckets: dict[str, list[ProbabilityV01ShadowLedgerRow]] = {}
        for row in ledger:
            buckets.setdefault(key_fn(row), []).append(row)
        for group_value, rows in sorted(buckets.items()):
            report.append(shadow_report_row(group_name, group_value, rows))
    return report


def shadow_report_row(group_name: str, group_value: str, rows: list[ProbabilityV01ShadowLedgerRow]) -> ProbabilityV01ShadowReportRow:
    settled = [row for row in rows if row.status == SETTLED_STATUS and row.realized_profit is not None]
    pending = [row for row in rows if row.status != SETTLED_STATUS]
    wins = [row for row in settled if row.won is True]
    losses = [row for row in settled if row.won is False]
    cost = sum(row.simulated_fill_cost for row in settled)
    payout = sum(row.payout or 0.0 for row in settled)
    profit = sum(row.realized_profit or 0.0 for row in settled)
    return ProbabilityV01ShadowReportRow(
        group_name=group_name,
        group_value=group_value,
        entries=len(rows),
        settled=len(settled),
        pending=len(pending),
        wins=len(wins),
        losses=len(losses),
        cost=cost,
        payout=payout,
        realized_profit=profit,
        roi_on_cost=profit / cost if cost > 0 else None,
        win_rate=len(wins) / len(settled) if settled else None,
        max_drawdown=max((row.drawdown or 0.0 for row in rows), default=None),
        avg_price=average([row.simulated_avg_price for row in settled if row.simulated_avg_price is not None]),
        avg_calibrated_probability=average([row.calibrated_probability for row in settled]),
        avg_calibrated_edge=average([row.calibrated_edge for row in settled]),
    )


def load_v01_shadow_entries(path) -> list[ProbabilityV01ShadowEntry]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    rows: list[ProbabilityV01ShadowEntry] = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(
                ProbabilityV01ShadowEntry(
                    evaluated_at=str(row.get("evaluated_at") or ""),
                    signal_id=str(row.get("signal_id") or ""),
                    v01_version=str(row.get("v01_version") or V01_VERSION),
                    asset=str(row.get("asset") or ""),
                    market_slug=str(row.get("market_slug") or ""),
                    outcome=str(row.get("outcome") or ""),
                    token_id=str(row.get("token_id") or ""),
                    grade=str(row.get("grade") or ""),
                    time_price_bucket=str(row.get("time_price_bucket") or ""),
                    best_ask=safe_float(row.get("best_ask"), 0.0) or 0.0,
                    calibrated_probability=safe_float(row.get("calibrated_probability"), 0.0) or 0.0,
                    calibrated_edge=safe_float(row.get("calibrated_edge"), 0.0) or 0.0,
                    v0_rule=str(row.get("v0_rule") or ""),
                    risk_action=str(row.get("risk_action") or ""),
                    recommended_cost_usdc=safe_float(row.get("recommended_cost_usdc"), 0.0) or 0.0,
                    recommended_shares=safe_float(row.get("recommended_shares"), 0.0) or 0.0,
                    executable_shares=safe_float(row.get("executable_shares"), 0.0) or 0.0,
                    ask_depth_shares_1pct=safe_float(row.get("ask_depth_shares_1pct"), 0.0) or 0.0,
                    ask_depth_usdc_1pct=safe_float(row.get("ask_depth_usdc_1pct"), 0.0) or 0.0,
                    entry_limit_price=safe_float(row.get("entry_limit_price"), 0.0) or 0.0,
                    simulated_fill_action=str(row.get("simulated_fill_action") or ""),
                    simulated_fill_shares=safe_float(row.get("simulated_fill_shares"), 0.0) or 0.0,
                    simulated_fill_cost=safe_float(row.get("simulated_fill_cost"), 0.0) or 0.0,
                    simulated_avg_price=safe_float(row.get("simulated_avg_price")),
                    simulated_slippage_per_share=safe_float(row.get("simulated_slippage_per_share")),
                    book_levels_used=int(safe_float(row.get("book_levels_used"), 0) or 0),
                    live_readiness=str(row.get("live_readiness") or ""),
                    live_readiness_reasons=str(row.get("live_readiness_reasons") or ""),
                    notes=str(row.get("notes") or ""),
                )
            )
    return rows


def dedupe_entries(entries: list[ProbabilityV01ShadowEntry]) -> list[ProbabilityV01ShadowEntry]:
    by_id: dict[str, ProbabilityV01ShadowEntry] = {}
    for entry in entries:
        if entry.signal_id and entry.signal_id not in by_id:
            by_id[entry.signal_id] = entry
    return list(by_id.values())


def summarize_v01_shadow_report(report: list[ProbabilityV01ShadowReportRow]) -> str:
    lines = [
        "# Probability V0.3 Shadow Report",
        "",
        "This report measures only V0.3 TRADE_CANDIDATE entries logged after live labeling. Normal probability data collection remains unrestricted.",
        "",
        "| Group | Value | Entries | Settled | ROI | PnL | Win Rate | Max DD |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    priority = {"all", "rule", "asset", "time_price_bucket", "edge_bucket", "entry_price_bucket", "live_readiness", "day"}
    for row in report:
        if row.group_name not in priority:
            continue
        roi = "" if row.roi_on_cost is None else f"{row.roi_on_cost * 100:.2f}%"
        win_rate = "" if row.win_rate is None else f"{row.win_rate * 100:.2f}%"
        dd = "" if row.max_drawdown is None else f"{row.max_drawdown:.2f}"
        lines.append(
            f"| {row.group_name} | {row.group_value} | {row.entries} | {row.settled} | {roi} | {row.realized_profit:.2f} | {win_rate} | {dd} |"
        )
    return "\n".join(lines) + "\n"


def summarize_v01_core_report(report: list[ProbabilityV01ShadowReportRow]) -> str:
    summary = summarize_v01_shadow_report(report)
    return summary.replace("# Probability V0.3 Shadow Report", "# Probability V0.3 Core-Only Report", 1).replace(
        "This report measures only V0.3 TRADE_CANDIDATE entries logged after live labeling. Normal probability data collection remains unrestricted.",
        "Core-only report includes only V0.3 core entries: v03_eth_b_900s_core, LIVE_READY_PAPER, full fill, simulated cost >= 1 USDC, simulated shares >= 5. Cooling/experimental buckets are still collected but excluded here.",
        1,
    )


def v01_rules_markdown() -> str:
    return """# Probability Bot V0.3 Rules

These rules are research/shadow-trading rules. They do not limit raw observations, raw signals, calibrated signals, or AVOID/OBSERVE logging.

## TRADE_CANDIDATE

- ETH grade B core bucket: `tte<=900|px<=60c`.
- No coin-specific core rules except ETH.

## AVOID

- `price <= 5c`: tiny lottery bucket remains structurally negative.
- `bnb`: avoided until forward report shows recovery.
- `doge` non-A: avoided until grade-B/grade-C flow recovers.
- Grade A outside cheap near-expiry core bucket.

## OBSERVE / OBSERVE_COOLING

- Grade A cheap near-expiry is OBSERVE_COOLING.
- B cheap <=120s probe is OBSERVE_COOLING.
- ETH grade B `tte<=300|px<=60c` is OBSERVE_COOLING after negative V0.2 forward results.
- Everything else remains logged for regime-change detection and future rule updates.

## Live-Readiness Simulation

For each V0.3 candidate, the bot simulates a marketable limit entry at `best_ask + entry_slippage`.
The shadow ledger uses simulated fill shares/cost for PnL, not generic risk cost, so the report is closer to live execution.

V0.3 core-only report includes only LIVE_READY_PAPER, FULL_FILL, simulated cost >= 1 USDC, and simulated shares >= 5.
"""


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def entry_price_bucket(price: float) -> str:
    if price <= 0.05:
        return "px<=5c"
    if price <= 0.25:
        return "px<=25c"
    if price <= 0.40:
        return "px<=40c"
    if price <= 0.50:
        return "px<=50c"
    if price <= 0.60:
        return "px<=60c"
    return "px>60c"


def edge_bucket(edge: float) -> str:
    if edge < 0.01:
        return "edge<1%"
    if edge < 0.03:
        return "edge 1-3%"
    if edge < 0.06:
        return "edge 3-6%"
    if edge < 0.10:
        return "edge 6-10%"
    return "edge>=10%"


def readiness_reason_bucket(reasons: str) -> str:
    if not reasons:
        return "live_ready"
    return reasons.split("|", 1)[0] or "unknown"
