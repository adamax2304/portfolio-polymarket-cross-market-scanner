from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import csv

from .probability import ProbabilityObservation
from .probability_calibration import edge_bucket, probability_decile_bucket, time_price_bucket
from .utils import safe_float


CALIBRATED_MODEL_VERSION = "calibrated_bucket_v1"
GRADE_RANK = {"REJECT": 0, "C": 1, "B": 2, "A": 3}


@dataclass(frozen=True)
class CalibrationStats:
    bucket: str
    rows: int
    avg_predicted_prob: float
    observed_win_rate: float
    calibration_error: float
    avg_market_price: float
    avg_net_edge: float
    pnl_per_share_sum: float
    roi_on_cost: float


@dataclass
class CalibratedProbabilitySignal:
    detected_at: str
    model_version: str
    raw_model_version: str
    asset: str
    duration_sec: int
    market_slug: str
    condition_id: str
    outcome: str
    token_id: str
    seconds_to_expiry: int
    threshold_price: float
    current_price: float
    best_ask: float
    raw_fair_prob: float
    raw_net_edge: float | None
    calibrated_probability: float
    calibrated_edge: float
    estimated_taker_fee_per_share: float
    executable_shares: float
    max_paper_cost: float
    ask_depth_shares_1pct: float
    ask_depth_usdc_1pct: float
    time_price_bucket: str
    time_price_rows: int
    time_price_observed_win_rate: float
    time_price_roi_on_cost: float
    edge_bucket: str
    edge_rows: int
    edge_roi_on_cost: float
    probability_bucket: str
    probability_rows: int
    probability_roi_on_cost: float
    grade: str
    reject_reason: str
    ask_levels_json: str
    notes: str = ""

    @property
    def signal_id(self) -> str:
        return f"{self.model_version}:{self.asset}:{self.market_slug}:{self.outcome}:{self.detected_at}"


class ProbabilityCalibrationModel:
    def __init__(self, calibration_dir: Path):
        self.calibration_dir = calibration_dir
        self.time_price = self._load("calibration_time_price_buckets.csv")
        self.edge = self._load("calibration_edge_buckets.csv")
        self.probability = self._load("calibration_probability_deciles.csv")

    @property
    def available(self) -> bool:
        return bool(self.time_price)

    def evaluate(
        self,
        obs: ProbabilityObservation,
        *,
        min_shares: float,
        max_paper_shares: float,
        min_seconds_to_expiry: int,
        max_seconds_to_expiry: int,
        min_calibrated_edge: float,
        min_grade: str,
    ) -> CalibratedProbabilitySignal | None:
        if obs.best_ask is None:
            return None
        if obs.seconds_to_expiry < min_seconds_to_expiry or obs.seconds_to_expiry > max_seconds_to_expiry:
            return None

        executable = min(obs.ask_depth_shares_1pct, max_paper_shares)
        if executable < min_shares:
            return None

        tp_key = time_price_bucket(obs.seconds_to_expiry, obs.best_ask)
        edge_key = edge_bucket(obs.net_edge)
        prob_key = probability_decile_bucket(obs.fair_prob)
        tp = self.time_price.get(tp_key)
        edge_stats = self.edge.get(edge_key)
        prob_stats = self.probability.get(prob_key)

        if not tp:
            return self._row(
                obs,
                executable=executable,
                calibrated_probability=0.0,
                calibrated_edge=-1.0,
                tp_key=tp_key,
                tp=None,
                edge_key=edge_key,
                edge_stats=edge_stats,
                prob_key=prob_key,
                prob_stats=prob_stats,
                grade="REJECT",
                reject_reason="missing_time_price_bucket",
            )

        fee = obs.estimated_taker_fee_per_share or 0.0
        calibrated_probability = tp.observed_win_rate
        calibrated_edge = calibrated_probability - obs.best_ask - fee
        grade, reject_reason = grade_calibrated_bucket(
            rows=tp.rows,
            roi_on_cost=tp.roi_on_cost,
            calibrated_edge=calibrated_edge,
            min_calibrated_edge=min_calibrated_edge,
        )
        if GRADE_RANK[grade] < GRADE_RANK.get(min_grade.upper(), 2):
            return None
        return self._row(
            obs,
            executable=executable,
            calibrated_probability=calibrated_probability,
            calibrated_edge=calibrated_edge,
            tp_key=tp_key,
            tp=tp,
            edge_key=edge_key,
            edge_stats=edge_stats,
            prob_key=prob_key,
            prob_stats=prob_stats,
            grade=grade,
            reject_reason=reject_reason,
        )

    def _row(
        self,
        obs: ProbabilityObservation,
        *,
        executable: float,
        calibrated_probability: float,
        calibrated_edge: float,
        tp_key: str,
        tp: CalibrationStats | None,
        edge_key: str,
        edge_stats: CalibrationStats | None,
        prob_key: str,
        prob_stats: CalibrationStats | None,
        grade: str,
        reject_reason: str,
    ) -> CalibratedProbabilitySignal:
        return CalibratedProbabilitySignal(
            detected_at=obs.observed_at,
            model_version=CALIBRATED_MODEL_VERSION,
            raw_model_version=obs.model_version,
            asset=obs.asset,
            duration_sec=obs.duration_sec,
            market_slug=obs.market_slug,
            condition_id=obs.condition_id,
            outcome=obs.outcome,
            token_id=obs.token_id,
            seconds_to_expiry=obs.seconds_to_expiry,
            threshold_price=obs.threshold_price,
            current_price=obs.current_price,
            best_ask=obs.best_ask or 0.0,
            raw_fair_prob=obs.fair_prob,
            raw_net_edge=obs.net_edge,
            calibrated_probability=calibrated_probability,
            calibrated_edge=calibrated_edge,
            estimated_taker_fee_per_share=obs.estimated_taker_fee_per_share or 0.0,
            executable_shares=executable,
            max_paper_cost=executable * (obs.best_ask or 0.0),
            ask_depth_shares_1pct=obs.ask_depth_shares_1pct,
            ask_depth_usdc_1pct=obs.ask_depth_usdc_1pct,
            time_price_bucket=tp_key,
            time_price_rows=tp.rows if tp else 0,
            time_price_observed_win_rate=tp.observed_win_rate if tp else 0.0,
            time_price_roi_on_cost=tp.roi_on_cost if tp else 0.0,
            edge_bucket=edge_key,
            edge_rows=edge_stats.rows if edge_stats else 0,
            edge_roi_on_cost=edge_stats.roi_on_cost if edge_stats else 0.0,
            probability_bucket=prob_key,
            probability_rows=prob_stats.rows if prob_stats else 0,
            probability_roi_on_cost=prob_stats.roi_on_cost if prob_stats else 0.0,
            grade=grade,
            reject_reason=reject_reason,
            ask_levels_json=obs.ask_levels_json,
            notes="Paper-only calibrated probability signal. Uses historical bucket win-rate, not raw fair_prob, as execution probability.",
        )

    def _load(self, filename: str) -> dict[str, CalibrationStats]:
        path = self.calibration_dir / filename
        if not path.exists():
            return {}
        with path.open(newline="", encoding="utf-8") as f:
            rows = {}
            for row in csv.DictReader(f):
                bucket = str(row.get("bucket") or "")
                if not bucket:
                    continue
                rows[bucket] = CalibrationStats(
                    bucket=bucket,
                    rows=int(safe_float(row.get("rows"), 0) or 0),
                    avg_predicted_prob=safe_float(row.get("avg_predicted_prob"), 0.0) or 0.0,
                    observed_win_rate=safe_float(row.get("observed_win_rate"), 0.0) or 0.0,
                    calibration_error=safe_float(row.get("calibration_error"), 0.0) or 0.0,
                    avg_market_price=safe_float(row.get("avg_market_price"), 0.0) or 0.0,
                    avg_net_edge=safe_float(row.get("avg_net_edge"), 0.0) or 0.0,
                    pnl_per_share_sum=safe_float(row.get("pnl_per_share_sum"), 0.0) or 0.0,
                    roi_on_cost=safe_float(row.get("roi_on_cost"), 0.0) or 0.0,
                )
            return rows


def calibrated_signals_from_observations(
    observations: list[ProbabilityObservation],
    calibration: ProbabilityCalibrationModel,
    *,
    min_shares: float,
    max_paper_shares: float,
    min_seconds_to_expiry: int,
    max_seconds_to_expiry: int,
    min_calibrated_edge: float,
    min_grade: str,
) -> list[CalibratedProbabilitySignal]:
    if not calibration.available:
        return []
    rows = [
        signal
        for obs in observations
        if (
            signal := calibration.evaluate(
                obs,
                min_shares=min_shares,
                max_paper_shares=max_paper_shares,
                min_seconds_to_expiry=min_seconds_to_expiry,
                max_seconds_to_expiry=max_seconds_to_expiry,
                min_calibrated_edge=min_calibrated_edge,
                min_grade=min_grade,
            )
        )
    ]
    return sorted(rows, key=lambda s: (GRADE_RANK[s.grade], s.calibrated_edge, s.executable_shares), reverse=True)


def grade_calibrated_bucket(*, rows: int, roi_on_cost: float, calibrated_edge: float, min_calibrated_edge: float) -> tuple[str, str]:
    if rows < 50:
        return "REJECT", "too_few_historical_rows"
    if roi_on_cost <= 0:
        return "REJECT", "historical_bucket_roi_not_positive"
    if calibrated_edge < min_calibrated_edge:
        return "REJECT", "calibrated_edge_below_min"
    if rows >= 150 and roi_on_cost >= 0.10 and calibrated_edge >= 0.005:
        return "A", ""
    if rows >= 100 and roi_on_cost >= 0.02 and calibrated_edge >= min_calibrated_edge:
        return "B", ""
    return "C", ""
