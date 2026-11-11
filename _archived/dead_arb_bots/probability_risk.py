from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import csv

from .config import ScannerConfig
from .probability_calibrated import CalibratedProbabilitySignal
from .utils import iso_to_ts, safe_float, safe_int


GRADE_SIZE_MULTIPLIER = {"A": 1.0, "B": 0.6, "C": 0.25}


@dataclass
class ProbabilityRiskDecision:
    evaluated_at: str
    signal_id: str
    asset: str
    market_slug: str
    outcome: str
    token_id: str
    grade: str
    time_price_bucket: str
    best_ask: float
    calibrated_probability: float
    calibrated_edge: float
    executable_shares: float
    executable_cost: float
    kelly_fraction_raw: float
    kelly_fraction_used: float
    grade_size_multiplier: float
    desired_cost_usdc: float
    desired_shares: float
    recommended_cost_usdc: float
    recommended_shares: float
    action: str
    risk_reasons: str
    daily_worst_case_loss_used_usdc: float
    daily_stop_loss_usdc: float
    daily_cost_used_usdc: float
    max_daily_cost_usdc: float
    open_cost_usdc: float
    max_open_cost_usdc: float
    open_positions: int
    max_open_positions: int
    asset_open_cost_usdc: float
    max_asset_cost_usdc: float
    bucket_open_cost_usdc: float
    max_bucket_cost_usdc: float
    expires_at_ts: int
    soft_mode: bool
    notes: str = ""


@dataclass
class RiskPosition:
    key: str
    asset: str
    bucket: str
    cost: float
    expires_at_ts: int


class ProbabilityRiskEngine:
    def __init__(self, cfg: ScannerConfig, *, soft_mode: bool = True):
        self.cfg = cfg
        self.soft_mode = soft_mode
        self.current_day = ""
        self.daily_worst_case_loss_used = 0.0
        self.daily_cost_used = 0.0
        self.positions: list[RiskPosition] = []
        self.entered_keys: set[str] = set()
        self.restore_from_csv()

    def evaluate_many(self, signals: list[CalibratedProbabilitySignal]) -> list[ProbabilityRiskDecision]:
        decisions: list[ProbabilityRiskDecision] = []
        for signal in signals:
            decisions.append(self.evaluate(signal))
        return decisions

    def evaluate(self, signal: CalibratedProbabilitySignal) -> ProbabilityRiskDecision:
        now_ts = iso_to_ts(signal.detected_at)
        day = signal.detected_at[:10]
        if day != self.current_day:
            self.current_day = day
            self.daily_worst_case_loss_used = 0.0
            self.daily_cost_used = 0.0
            self.entered_keys.clear()
        self._expire_positions(now_ts)

        key = f"{signal.market_slug}:{signal.outcome}"
        executable_cost = max(0.0, signal.max_paper_cost)
        kelly_raw = binary_kelly_cost_fraction(signal.calibrated_probability, signal.best_ask, signal.estimated_taker_fee_per_share)
        kelly_used = min(kelly_raw, self.cfg.probability_risk_max_kelly_fraction)
        grade_multiplier = GRADE_SIZE_MULTIPLIER.get(signal.grade, 0.0)
        desired_cost = (
            self.cfg.probability_risk_bankroll_usdc
            * self.cfg.probability_risk_kelly_fraction
            * kelly_used
            * grade_multiplier
        )
        desired_cost = min(desired_cost, executable_cost)

        reasons: list[str] = []
        capped_cost = desired_cost
        capped_cost = cap_cost(capped_cost, self.cfg.probability_risk_max_trade_cost_usdc, reasons, "max_trade_cost")
        capped_cost = cap_cost(capped_cost, self._remaining_daily_stop(), reasons, "daily_stop_loss")
        capped_cost = cap_cost(capped_cost, self._remaining_daily_cost(), reasons, "max_daily_cost")
        capped_cost = cap_cost(capped_cost, self._remaining_open_cost(), reasons, "max_open_cost")
        capped_cost = cap_cost(capped_cost, self._remaining_asset_cost(signal.asset), reasons, "max_asset_cost")
        capped_cost = cap_cost(capped_cost, self._remaining_bucket_cost(signal.time_price_bucket), reasons, "max_bucket_cost")

        if len(self.positions) >= self.cfg.probability_risk_max_open_positions:
            capped_cost = 0.0
            reasons.append("max_open_positions")
        if key in self.entered_keys:
            capped_cost = 0.0
            reasons.append("duplicate_market_outcome")
        if capped_cost < self.cfg.probability_risk_min_trade_cost_usdc:
            capped_cost = 0.0
            if "min_trade_cost" not in reasons:
                reasons.append("min_trade_cost")

        recommended_shares = capped_cost / signal.best_ask if signal.best_ask > 0 else 0.0
        desired_shares = desired_cost / signal.best_ask if signal.best_ask > 0 else 0.0
        action = risk_action(desired_cost, capped_cost, reasons)
        expires_at_ts = now_ts + signal.seconds_to_expiry + self.cfg.probability_risk_position_grace_seconds

        decision = ProbabilityRiskDecision(
            evaluated_at=signal.detected_at,
            signal_id=signal.signal_id,
            asset=signal.asset,
            market_slug=signal.market_slug,
            outcome=signal.outcome,
            token_id=signal.token_id,
            grade=signal.grade,
            time_price_bucket=signal.time_price_bucket,
            best_ask=signal.best_ask,
            calibrated_probability=signal.calibrated_probability,
            calibrated_edge=signal.calibrated_edge,
            executable_shares=signal.executable_shares,
            executable_cost=executable_cost,
            kelly_fraction_raw=kelly_raw,
            kelly_fraction_used=kelly_used,
            grade_size_multiplier=grade_multiplier,
            desired_cost_usdc=desired_cost,
            desired_shares=desired_shares,
            recommended_cost_usdc=capped_cost,
            recommended_shares=recommended_shares,
            action=action,
            risk_reasons="|".join(sorted(set(reasons))),
            daily_worst_case_loss_used_usdc=self.daily_worst_case_loss_used,
            daily_stop_loss_usdc=self.cfg.probability_risk_daily_stop_loss_usdc,
            daily_cost_used_usdc=self.daily_cost_used,
            max_daily_cost_usdc=self.cfg.probability_risk_max_daily_cost_usdc,
            open_cost_usdc=self.open_cost,
            max_open_cost_usdc=self.cfg.probability_risk_max_open_cost_usdc,
            open_positions=len(self.positions),
            max_open_positions=self.cfg.probability_risk_max_open_positions,
            asset_open_cost_usdc=self.asset_open_cost(signal.asset),
            max_asset_cost_usdc=self.cfg.probability_risk_max_asset_cost_usdc,
            bucket_open_cost_usdc=self.bucket_open_cost(signal.time_price_bucket),
            max_bucket_cost_usdc=self.cfg.probability_risk_max_bucket_cost_usdc,
            expires_at_ts=expires_at_ts,
            soft_mode=self.soft_mode,
            notes="Soft paper risk decision. Observations and calibrated signals continue even when action is WOULD_BLOCK.",
        )

        if capped_cost > 0:
            self.daily_worst_case_loss_used += capped_cost
            self.daily_cost_used += capped_cost
            self.positions.append(RiskPosition(key, signal.asset, signal.time_price_bucket, capped_cost, expires_at_ts))
            self.entered_keys.add(key)
        return decision

    @property
    def open_cost(self) -> float:
        return sum(position.cost for position in self.positions)

    def asset_open_cost(self, asset: str) -> float:
        return sum(position.cost for position in self.positions if position.asset == asset)

    def bucket_open_cost(self, bucket: str) -> float:
        return sum(position.cost for position in self.positions if position.bucket == bucket)

    def _expire_positions(self, now_ts: int) -> None:
        self.positions = [position for position in self.positions if position.expires_at_ts > now_ts]

    def _remaining_daily_stop(self) -> float:
        return max(0.0, self.cfg.probability_risk_daily_stop_loss_usdc - self.daily_worst_case_loss_used)

    def _remaining_daily_cost(self) -> float:
        return max(0.0, self.cfg.probability_risk_max_daily_cost_usdc - self.daily_cost_used)

    def _remaining_open_cost(self) -> float:
        return max(0.0, self.cfg.probability_risk_max_open_cost_usdc - self.open_cost)

    def _remaining_asset_cost(self, asset: str) -> float:
        return max(0.0, self.cfg.probability_risk_max_asset_cost_usdc - self.asset_open_cost(asset))

    def _remaining_bucket_cost(self, bucket: str) -> float:
        return max(0.0, self.cfg.probability_risk_max_bucket_cost_usdc - self.bucket_open_cost(bucket))

    def restore_from_csv(self) -> None:
        path = self.cfg.probability_risk_decisions_csv
        if not path.exists():
            return
        now_ts = int(datetime.now(timezone.utc).timestamp())
        today = datetime.now(timezone.utc).date().isoformat()
        daily_cost = 0.0
        positions: list[RiskPosition] = []
        entered_keys: set[str] = set()
        try:
            with path.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if not is_enter_action(row.get("action", "")):
                        continue
                    evaluated_at = str(row.get("evaluated_at") or "")
                    if evaluated_at[:10] != today:
                        continue
                    cost = safe_float(row.get("recommended_cost_usdc"), 0.0) or 0.0
                    if cost <= 0:
                        continue
                    market_slug = str(row.get("market_slug") or "")
                    outcome = str(row.get("outcome") or "")
                    key = f"{market_slug}:{outcome}"
                    entered_keys.add(key)
                    daily_cost += cost
                    expires_at_ts = safe_int(row.get("expires_at_ts"), 0)
                    if expires_at_ts > now_ts:
                        positions.append(
                            RiskPosition(
                                key=key,
                                asset=str(row.get("asset") or ""),
                                bucket=str(row.get("time_price_bucket") or ""),
                                cost=cost,
                                expires_at_ts=expires_at_ts,
                            )
                        )
        except Exception:
            return
        self.current_day = today
        self.daily_worst_case_loss_used = daily_cost
        self.daily_cost_used = daily_cost
        self.positions = positions
        self.entered_keys = entered_keys


def binary_kelly_cost_fraction(probability: float, price: float, fee: float = 0.0) -> float:
    if probability <= 0 or price <= 0 or price >= 1:
        return 0.0
    edge_after_fee = probability - price - max(fee, 0.0)
    if edge_after_fee <= 0:
        return 0.0
    return max(0.0, edge_after_fee / max(1.0 - price, 0.01))


def cap_cost(cost: float, limit: float, reasons: list[str], reason: str) -> float:
    if limit <= 0:
        reasons.append(reason)
        return 0.0
    if cost > limit:
        reasons.append(reason)
        return limit
    return cost


def risk_action(desired_cost: float, capped_cost: float, reasons: list[str]) -> str:
    if capped_cost <= 0:
        return "WOULD_BLOCK"
    if reasons or capped_cost < desired_cost:
        return "WOULD_ENTER_CLIPPED"
    return "WOULD_ENTER"


def is_enter_action(action: str) -> bool:
    return action in {"WOULD_ENTER", "WOULD_ENTER_CLIPPED"}
