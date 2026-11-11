from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import csv
import time

from .config import ScannerConfig
from .gamma import GammaClient
from .settlement import final_price, price_to_beat
from .storage import write_rows_csv
from .utils import safe_float, utc_now


@dataclass
class CrossFamilyResolutionAuditRow:
    audited_at: str
    signal_id: str
    asset: str
    end_ts: int
    lower_market_slug: str
    higher_market_slug: str
    source_pair: str
    source_risk: str
    lower_resolution_source: str
    higher_resolution_source: str
    lower_recorded_threshold: float | None
    higher_recorded_threshold: float | None
    lower_official_threshold: float | None
    higher_official_threshold: float | None
    lower_recorded_final: float | None
    higher_recorded_final: float | None
    lower_official_final: float | None
    higher_official_final: float | None
    official_threshold_gap: float | None
    official_close_basis: float | None
    recorded_up_won: bool | None
    recorded_down_won: bool | None
    official_up_won: bool | None
    official_down_won: bool | None
    recorded_payout: float | None
    official_payout: float | None
    cost: float
    recorded_profit: float | None
    official_profit: float | None
    profit_delta: float | None
    threshold_mismatch: bool
    outcome_mismatch: bool
    impossible_under_single_source: bool
    status: str
    notes: str


def build_cross_family_resolution_audit(cfg: ScannerConfig, *, sleep_seconds: float = 0.2) -> list[CrossFamilyResolutionAuditRow]:
    gamma = GammaClient(cfg)
    rows = load_csv_rows(cfg.paper_settlements_csv)
    audited_at = utc_now()
    out: list[CrossFamilyResolutionAuditRow] = []
    cache: dict[str, dict[str, Any] | None] = {}
    for row in rows:
        lower_slug = str(row.get("lower_market_slug") or "")
        higher_slug = str(row.get("higher_market_slug") or "")
        if not is_cross_family(lower_slug, higher_slug):
            continue
        lower_market = cached_market(gamma, cache, lower_slug, sleep_seconds)
        higher_market = cached_market(gamma, cache, higher_slug, sleep_seconds)
        out.append(audit_row(audited_at, row, lower_market, higher_market))
    write_rows_csv(cfg.cross_family_resolution_audit_csv, [asdict(row) for row in out])
    cfg.cross_family_resolution_audit_summary_md.write_text(summarize_cross_family_audit(out), encoding="utf-8")
    return out


def cached_market(gamma: GammaClient, cache: dict[str, dict[str, Any] | None], slug: str, sleep_seconds: float) -> dict[str, Any] | None:
    if slug not in cache:
        market = None
        for attempt in range(4):
            market = gamma.get_market_by_slug(slug)
            if market:
                break
            time.sleep(max(sleep_seconds, 0.25) * (attempt + 1))
        cache[slug] = market
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return cache[slug]


def audit_row(
    audited_at: str,
    row: dict[str, str],
    lower_market: dict[str, Any] | None,
    higher_market: dict[str, Any] | None,
) -> CrossFamilyResolutionAuditRow:
    lower_recorded_threshold = safe_float(row.get("lower_threshold"))
    higher_recorded_threshold = safe_float(row.get("higher_threshold"))
    lower_recorded_final = safe_float(row.get("lower_final_price"))
    higher_recorded_final = safe_float(row.get("higher_final_price"))
    lower_official_threshold = price_to_beat(lower_market)
    higher_official_threshold = price_to_beat(higher_market)
    lower_official_final = final_price(lower_market)
    higher_official_final = final_price(higher_market)
    shares = safe_float(row.get("shares"), 0.0) or 0.0
    cost = safe_float(row.get("cost"), 0.0) or 0.0
    recorded_up_won = parse_bool(row.get("up_leg_won"))
    recorded_down_won = parse_bool(row.get("down_leg_won"))
    official_up_won = (
        lower_official_final >= lower_official_threshold
        if lower_official_final is not None and lower_official_threshold is not None
        else None
    )
    official_down_won = (
        higher_official_final < higher_official_threshold
        if higher_official_final is not None and higher_official_threshold is not None
        else None
    )
    recorded_payout = safe_float(row.get("payout"))
    official_payout = None
    if official_up_won is not None and official_down_won is not None:
        official_payout = shares * int(official_up_won) + shares * int(official_down_won)
    recorded_profit = safe_float(row.get("realized_profit"))
    official_profit = official_payout - cost if official_payout is not None else None
    lower_source = source_family(lower_market)
    higher_source = source_family(higher_market)
    source_pair = f"{lower_source}/{higher_source}"
    threshold_mismatch = comparable_changed(lower_recorded_threshold, lower_official_threshold) or comparable_changed(
        higher_recorded_threshold, higher_official_threshold
    )
    outcome_mismatch = comparable_changed(recorded_up_won, official_up_won) or comparable_changed(recorded_down_won, official_down_won)
    official_threshold_gap = (
        higher_official_threshold - lower_official_threshold
        if higher_official_threshold is not None and lower_official_threshold is not None
        else None
    )
    official_close_basis = (
        higher_official_final - lower_official_final
        if higher_official_final is not None and lower_official_final is not None
        else None
    )
    status = str(row.get("status") or "")
    notes: list[str] = []
    if lower_source != higher_source:
        notes.append("Mixed oracle/source: not a guaranteed same-price arbitrage.")
    if threshold_mismatch:
        notes.append("Recorded settlement threshold differs from official Gamma priceToBeat.")
    if official_threshold_gap is not None and official_threshold_gap <= 0:
        notes.append("Official threshold order does not match lower<higher orientation.")
    if official_up_won is False and official_down_won is False:
        notes.append("Both legs lose under official mixed-source settlement.")
    return CrossFamilyResolutionAuditRow(
        audited_at=audited_at,
        signal_id=str(row.get("signal_id") or ""),
        asset=str(row.get("asset") or ""),
        end_ts=int(safe_float(row.get("end_ts"), 0) or 0),
        lower_market_slug=str(row.get("lower_market_slug") or ""),
        higher_market_slug=str(row.get("higher_market_slug") or ""),
        source_pair=source_pair,
        source_risk=source_risk(lower_source, higher_source),
        lower_resolution_source=resolution_source(lower_market),
        higher_resolution_source=resolution_source(higher_market),
        lower_recorded_threshold=lower_recorded_threshold,
        higher_recorded_threshold=higher_recorded_threshold,
        lower_official_threshold=lower_official_threshold,
        higher_official_threshold=higher_official_threshold,
        lower_recorded_final=lower_recorded_final,
        higher_recorded_final=higher_recorded_final,
        lower_official_final=lower_official_final,
        higher_official_final=higher_official_final,
        official_threshold_gap=official_threshold_gap,
        official_close_basis=official_close_basis,
        recorded_up_won=recorded_up_won,
        recorded_down_won=recorded_down_won,
        official_up_won=official_up_won,
        official_down_won=official_down_won,
        recorded_payout=recorded_payout,
        official_payout=official_payout,
        cost=cost,
        recorded_profit=recorded_profit,
        official_profit=official_profit,
        profit_delta=(official_profit - recorded_profit) if official_profit is not None and recorded_profit is not None else None,
        threshold_mismatch=threshold_mismatch,
        outcome_mismatch=outcome_mismatch,
        impossible_under_single_source=bool(official_up_won is False and official_down_won is False),
        status=status,
        notes=" ".join(notes) if notes else "Official Gamma metadata is consistent with recorded settlement.",
    )


def summarize_cross_family_audit(rows: list[CrossFamilyResolutionAuditRow]) -> str:
    settled = [row for row in rows if row.official_profit is not None]
    threshold_mismatch = [row for row in rows if row.threshold_mismatch]
    outcome_mismatch = [row for row in rows if row.outcome_mismatch]
    both_lose = [row for row in rows if row.impossible_under_single_source]
    recorded_profit = sum(row.recorded_profit or 0.0 for row in settled)
    official_profit = sum(row.official_profit or 0.0 for row in settled)
    source_counts: dict[str, int] = {}
    source_profit: dict[str, float] = {}
    for row in settled:
        source_counts[row.source_pair] = source_counts.get(row.source_pair, 0) + 1
        source_profit[row.source_pair] = source_profit.get(row.source_pair, 0.0) + (row.official_profit or 0.0)
    lines = [
        "# CrossFamily Resolution Audit",
        "",
        "Scope: settled paper rows where one leg is an hourly `up-or-down` market and the other leg is an `updown` market.",
        "The audit recomputes outcomes from official Gamma `eventMetadata.priceToBeat` and `eventMetadata.finalPrice`; recorded/captured thresholds are used only for comparison.",
        "",
        f"Rows audited: {len(rows)}",
        f"Rows with official settlement data: {len(settled)}",
        f"Recorded PnL on audited rows: {recorded_profit:.4f}",
        f"Official recomputed PnL: {official_profit:.4f}",
        f"PnL delta official-recorded: {official_profit - recorded_profit:.4f}",
        f"Threshold mismatches: {len(threshold_mismatch)}",
        f"Outcome mismatches: {len(outcome_mismatch)}",
        f"Both-leg-loss mixed-source cases: {len(both_lose)}",
        "",
        "## Source Pairs",
    ]
    for source_pair, count in sorted(source_counts.items()):
        lines.append(f"- {source_pair}: rows={count}, official_pnl={source_profit[source_pair]:.4f}")
    lines.extend(
        [
            "",
            "## Live Readiness Conclusion",
            "CrossFamily is a research/basis bucket, not a guaranteed same-oracle arbitrage bucket. Hourly `up-or-down` crypto markets can resolve from Binance USDT 1H candles while `updown` markets can resolve from Chainlink USD data streams.",
            "For live, CrossFamily should require explicit opt-in, official `priceToBeat` on both legs, source-pair labeling, a source-basis safety buffer, and tiny caps until the mixed-source audit remains stable.",
            "The lower-risk live candidate is `LOW_SAME_UPDOWN_FAMILY`, where both legs belong to the `updown` family and resolve from the same Chainlink-style source family.",
            "",
        ]
    )
    if outcome_mismatch:
        lines.append("## Mismatches")
        for row in outcome_mismatch[:20]:
            lines.append(
                f"- {row.signal_id}: recorded=({row.recorded_up_won},{row.recorded_down_won}) "
                f"official=({row.official_up_won},{row.official_down_won}) delta={row.profit_delta}"
            )
        lines.append("")
    return "\n".join(lines)


def load_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def is_cross_family(lower_slug: str, higher_slug: str) -> bool:
    combined = f"{lower_slug} {higher_slug}"
    return "-up-or-down-" in combined and "-updown-" in combined


def parse_bool(value: str | None) -> bool | None:
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes"}:
        return True
    if text in {"0", "false", "no"}:
        return False
    return None


def float_changed(recorded: float | None, official: float | None, *, tolerance: float = 1e-9) -> bool:
    if recorded is None or official is None:
        return recorded != official
    return abs(recorded - official) > tolerance


def comparable_changed(recorded, official) -> bool:
    if official is None:
        return False
    if isinstance(recorded, float | int | type(None)) and isinstance(official, float | int | type(None)):
        return float_changed(recorded, official)
    return recorded != official


def source_family(market: dict[str, Any] | None) -> str:
    text = f"{resolution_source(market)} {description(market)}".lower()
    if "binance" in text or "usdt" in text:
        return "BINANCE_USDT"
    if "chainlink" in text or "data.chain.link" in text:
        return "CHAINLINK_USD"
    return "UNKNOWN"


def source_risk(lower_source: str, higher_source: str) -> str:
    if lower_source == "UNKNOWN" or higher_source == "UNKNOWN":
        return "UNKNOWN_SOURCE"
    if lower_source == higher_source:
        return "LOW_SAME_SOURCE_FAMILY"
    return "MIXED_ORACLE_RESEARCH"


def resolution_source(market: dict[str, Any] | None) -> str:
    if not market:
        return ""
    source = market.get("resolutionSource")
    if source:
        return str(source)
    events = market.get("events")
    if isinstance(events, list) and events:
        source = events[0].get("resolutionSource") if isinstance(events[0], dict) else None
        if source:
            return str(source)
    return ""


def description(market: dict[str, Any] | None) -> str:
    if not market:
        return ""
    desc = market.get("description")
    if desc:
        return str(desc)
    events = market.get("events")
    if isinstance(events, list) and events:
        desc = events[0].get("description") if isinstance(events[0], dict) else None
        if desc:
            return str(desc)
    return ""
