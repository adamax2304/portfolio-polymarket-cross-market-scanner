from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
import csv
import logging

from .config import ScannerConfig
from .gamma import GammaClient
from .settlement import final_price, price_to_beat
from .storage import write_rows_csv
from .utils import iso_to_ts, safe_float, safe_int, utc_now

LOGGER = logging.getLogger(__name__)

ENTER_ACTIONS = {"WOULD_ENTER", "WOULD_ENTER_CLIPPED"}
SETTLED_STATUS = "SETTLED"
PENDING_STATUS = "SETTLEMENT_PENDING"
ERROR_STATUS = "SETTLEMENT_ERROR"


@dataclass
class ProbabilitySettlement:
    signal_id: str
    settled_at: str
    evaluated_at: str
    asset: str
    market_slug: str
    outcome: str
    token_id: str
    grade: str
    time_price_bucket: str
    price_bucket: str
    tte_bucket: str
    entry_seconds_to_expiry: int
    best_ask: float
    calibrated_probability: float
    calibrated_edge: float
    shares: float
    cost: float
    threshold_price: float | None
    final_price: float | None
    won: bool | None
    payout: float | None
    realized_profit: float | None
    roi_on_cost: float | None
    status: str
    notes: str = ""


@dataclass
class ProbabilityReportRow:
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
    avg_cost: float | None
    avg_price: float | None
    avg_calibrated_probability: float | None
    avg_calibrated_edge: float | None


class ProbabilitySettler:
    def __init__(self, cfg: ScannerConfig, gamma: GammaClient | None = None):
        self.cfg = cfg
        self.gamma = gamma or GammaClient(cfg)
        self._market_cache: dict[str, dict[str, Any] | None] = {}

    def settle_due(self, *, limit: int | None = None) -> list[ProbabilitySettlement]:
        decisions = load_enter_decisions(self.cfg.probability_risk_decisions_csv)
        existing = load_probability_settlements(self.cfg.probability_settlements_csv)
        now_ts = int(datetime.now(timezone.utc).timestamp())
        results = dict(existing)
        due_rows = []
        for row in decisions:
            signal_id = str(row.get("signal_id") or "")
            if not signal_id:
                continue
            prior = results.get(signal_id)
            if prior and prior.status == SETTLED_STATUS:
                continue
            if safe_int(row.get("expires_at_ts"), 0) > now_ts:
                continue
            due_rows.append(row)
            if limit and len(due_rows) >= limit:
                break

        for row in due_rows:
            settlement = self.settle_row(row)
            results[settlement.signal_id] = settlement

        all_rows = sorted(results.values(), key=lambda r: (r.evaluated_at, r.signal_id))
        write_rows_csv(self.cfg.probability_settlements_csv, [asdict(row) for row in all_rows])
        report = build_probability_report(all_rows)
        write_rows_csv(self.cfg.probability_report_csv, [asdict(row) for row in report])
        return [results[str(row.get("signal_id") or "")] for row in due_rows if str(row.get("signal_id") or "") in results]

    def settle_row(self, row: dict[str, str]) -> ProbabilitySettlement:
        try:
            return self._settle_row(row)
        except Exception as exc:
            LOGGER.warning("Probability settlement failed for %s: %s", row.get("signal_id"), exc)
            return settlement_from_row(row, status=ERROR_STATUS, notes=str(exc))

    def _settle_row(self, row: dict[str, str]) -> ProbabilitySettlement:
        market = self.market_by_slug(str(row.get("market_slug") or ""))
        threshold = price_to_beat(market)
        final = final_price(market)
        if threshold is None or final is None:
            market = self.market_by_slug(str(row.get("market_slug") or ""), refresh=True)
            threshold = price_to_beat(market)
            final = final_price(market)
        if threshold is None or final is None:
            return settlement_from_row(
                row,
                position_grace_seconds=self.cfg.probability_risk_position_grace_seconds,
                threshold_price=threshold,
                final=final,
                status=PENDING_STATUS,
                notes="Final price or price-to-beat is not available yet from Gamma metadata.",
            )

        outcome = str(row.get("outcome") or "")
        if outcome.lower() == "up":
            won = final >= threshold
        elif outcome.lower() == "down":
            won = final < threshold
        else:
            return settlement_from_row(
                row,
                position_grace_seconds=self.cfg.probability_risk_position_grace_seconds,
                threshold_price=threshold,
                final=final,
                status=ERROR_STATUS,
                notes=f"Unsupported outcome: {outcome}",
            )

        shares = safe_float(row.get("recommended_shares"), 0.0) or 0.0
        cost = safe_float(row.get("recommended_cost_usdc"), 0.0) or 0.0
        payout = shares if won else 0.0
        realized = payout - cost
        roi = realized / cost if cost > 0 else None
        return settlement_from_row(
            row,
            position_grace_seconds=self.cfg.probability_risk_position_grace_seconds,
            threshold_price=threshold,
            final=final,
            won=won,
            payout=payout,
            realized_profit=realized,
            roi_on_cost=roi,
            status=SETTLED_STATUS,
            notes="Actual paper payout computed from Gamma finalPrice and priceToBeat.",
        )

    def market_by_slug(self, slug: str, *, refresh: bool = False) -> dict[str, Any] | None:
        if refresh or slug not in self._market_cache:
            self._market_cache[slug] = self.gamma.get_market_by_slug(slug)
        return self._market_cache[slug]


def load_enter_decisions(path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("action") not in ENTER_ACTIONS:
                continue
            if (safe_float(row.get("recommended_cost_usdc"), 0.0) or 0.0) <= 0:
                continue
            signal_id = str(row.get("signal_id") or "")
            if not signal_id or signal_id in seen:
                continue
            seen.add(signal_id)
            rows.append(row)
    return rows


def load_probability_settlements(path) -> dict[str, ProbabilitySettlement]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    settlements: dict[str, ProbabilitySettlement] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            signal_id = str(row.get("signal_id") or "")
            if not signal_id:
                continue
            settlements[signal_id] = ProbabilitySettlement(
                signal_id=signal_id,
                settled_at=str(row.get("settled_at") or ""),
                evaluated_at=str(row.get("evaluated_at") or ""),
                asset=str(row.get("asset") or ""),
                market_slug=str(row.get("market_slug") or ""),
                outcome=str(row.get("outcome") or ""),
                token_id=str(row.get("token_id") or ""),
                grade=str(row.get("grade") or ""),
                time_price_bucket=str(row.get("time_price_bucket") or ""),
                price_bucket=str(row.get("price_bucket") or ""),
                tte_bucket=str(row.get("tte_bucket") or ""),
                entry_seconds_to_expiry=safe_int(row.get("entry_seconds_to_expiry"), 0),
                best_ask=safe_float(row.get("best_ask"), 0.0) or 0.0,
                calibrated_probability=safe_float(row.get("calibrated_probability"), 0.0) or 0.0,
                calibrated_edge=safe_float(row.get("calibrated_edge"), 0.0) or 0.0,
                shares=safe_float(row.get("shares"), 0.0) or 0.0,
                cost=safe_float(row.get("cost"), 0.0) or 0.0,
                threshold_price=safe_float(row.get("threshold_price")),
                final_price=safe_float(row.get("final_price")),
                won=parse_optional_bool(row.get("won")),
                payout=safe_float(row.get("payout")),
                realized_profit=safe_float(row.get("realized_profit")),
                roi_on_cost=safe_float(row.get("roi_on_cost")),
                status=str(row.get("status") or ""),
                notes=str(row.get("notes") or ""),
            )
    return settlements


def settlement_from_row(
    row: dict[str, str],
    *,
    position_grace_seconds: int = 90,
    threshold_price: float | None = None,
    final: float | None = None,
    won: bool | None = None,
    payout: float | None = None,
    realized_profit: float | None = None,
    roi_on_cost: float | None = None,
    status: str,
    notes: str = "",
) -> ProbabilitySettlement:
    evaluated_at = str(row.get("evaluated_at") or "")
    entry_seconds_to_expiry = max(0, safe_int(row.get("expires_at_ts"), 0) - iso_to_ts(evaluated_at) - position_grace_seconds)
    best_ask = safe_float(row.get("best_ask"), 0.0) or 0.0
    return ProbabilitySettlement(
        signal_id=str(row.get("signal_id") or ""),
        settled_at=utc_now(),
        evaluated_at=evaluated_at,
        asset=str(row.get("asset") or ""),
        market_slug=str(row.get("market_slug") or ""),
        outcome=str(row.get("outcome") or ""),
        token_id=str(row.get("token_id") or ""),
        grade=str(row.get("grade") or ""),
        time_price_bucket=str(row.get("time_price_bucket") or ""),
        price_bucket=price_bucket(best_ask),
        tte_bucket=tte_bucket(entry_seconds_to_expiry),
        entry_seconds_to_expiry=entry_seconds_to_expiry,
        best_ask=best_ask,
        calibrated_probability=safe_float(row.get("calibrated_probability"), 0.0) or 0.0,
        calibrated_edge=safe_float(row.get("calibrated_edge"), 0.0) or 0.0,
        shares=safe_float(row.get("recommended_shares"), 0.0) or 0.0,
        cost=safe_float(row.get("recommended_cost_usdc"), 0.0) or 0.0,
        threshold_price=threshold_price,
        final_price=final,
        won=won,
        payout=payout,
        realized_profit=realized_profit,
        roi_on_cost=roi_on_cost,
        status=status,
        notes=notes,
    )


def build_probability_report(settlements: list[ProbabilitySettlement]) -> list[ProbabilityReportRow]:
    rows: list[ProbabilityReportRow] = []
    groups = [
        ("all", lambda r: "all"),
        ("asset", lambda r: r.asset),
        ("grade", lambda r: r.grade),
        ("time_price_bucket", lambda r: r.time_price_bucket),
        ("price_bucket", lambda r: r.price_bucket),
        ("tte_bucket", lambda r: r.tte_bucket),
        ("asset_grade", lambda r: f"{r.asset}|{r.grade}"),
        ("asset_time_price_bucket", lambda r: f"{r.asset}|{r.time_price_bucket}"),
    ]
    for group_name, key_fn in groups:
        buckets: dict[str, list[ProbabilitySettlement]] = {}
        for settlement in settlements:
            buckets.setdefault(key_fn(settlement), []).append(settlement)
        for group_value, bucket_rows in sorted(buckets.items()):
            rows.append(report_row(group_name, group_value, bucket_rows))
    return rows


def report_row(group_name: str, group_value: str, rows: list[ProbabilitySettlement]) -> ProbabilityReportRow:
    settled = [r for r in rows if r.status == SETTLED_STATUS]
    pending = [r for r in rows if r.status != SETTLED_STATUS]
    wins = [r for r in settled if r.won is True]
    losses = [r for r in settled if r.won is False]
    cost = sum(r.cost for r in settled)
    payout = sum(r.payout or 0.0 for r in settled)
    profit = sum(r.realized_profit or 0.0 for r in settled)
    return ProbabilityReportRow(
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
        avg_cost=cost / len(settled) if settled else None,
        avg_price=average([r.best_ask for r in settled]),
        avg_calibrated_probability=average([r.calibrated_probability for r in settled]),
        avg_calibrated_edge=average([r.calibrated_edge for r in settled]),
    )


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def price_bucket(price: float) -> str:
    if price <= 0.05:
        return "px<=5c"
    if price <= 0.10:
        return "px<=10c"
    if price <= 0.25:
        return "px<=25c"
    if price <= 0.50:
        return "px<=50c"
    return "px>50c"


def tte_bucket(seconds: int) -> str:
    if seconds <= 60:
        return "tte<=60"
    if seconds <= 300:
        return "tte<=300"
    if seconds <= 900:
        return "tte<=900"
    if seconds <= 1800:
        return "tte<=1800"
    return "tte>1800"


def parse_optional_bool(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    return str(value).strip().lower() in {"1", "true", "yes"}


def summarize_probability_settlements(settlements: list[ProbabilitySettlement], report: list[ProbabilityReportRow]) -> str:
    settled = [row for row in settlements if row.status == SETTLED_STATUS]
    pending = [row for row in settlements if row.status != SETTLED_STATUS]
    total = next((row for row in report if row.group_name == "all" and row.group_value == "all"), None)
    lines = [f"processed={len(settlements)} settled={len(settled)} pending_or_error={len(pending)}"]
    if total:
        roi = total.roi_on_cost * 100 if total.roi_on_cost is not None else None
        win_rate = total.win_rate * 100 if total.win_rate is not None else None
        lines.append(
            f"all settled={total.settled} cost={total.cost:.2f} pnl={total.realized_profit:.2f} "
            f"roi={roi:.2f}% win_rate={win_rate:.2f}%" if roi is not None and win_rate is not None else "all no settled rows yet"
        )
    for row in report:
        if row.group_name == "asset" and row.settled:
            roi = row.roi_on_cost * 100 if row.roi_on_cost is not None else 0.0
            win_rate = row.win_rate * 100 if row.win_rate is not None else 0.0
            lines.append(f"asset {row.group_value}: n={row.settled} pnl={row.realized_profit:.2f} roi={roi:.2f}% win={win_rate:.1f}%")
    return "\n".join(lines)
