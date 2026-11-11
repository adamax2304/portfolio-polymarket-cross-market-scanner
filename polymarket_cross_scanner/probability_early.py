from __future__ import annotations

from .probability import ProbabilitySignal
from .probability_calibrated import CalibratedProbabilitySignal


EARLY_MODEL_VERSION = "early_raw_probability_v1"
GRADE_RANK = {"C": 1, "B": 2, "A": 3}


def early_model_signals_from_raw(
    signals: list[ProbabilitySignal],
    *,
    min_seconds_since_start: int,
    max_seconds_since_start: int,
    min_raw_edge: float,
) -> list[CalibratedProbabilitySignal]:
    rows: list[CalibratedProbabilitySignal] = []
    for signal in signals:
        seconds_since_start = signal.duration_sec - signal.seconds_to_expiry
        if seconds_since_start < min_seconds_since_start or seconds_since_start > max_seconds_since_start:
            continue
        if signal.net_edge < min_raw_edge:
            continue
        grade = grade_raw_signal(signal.net_edge, signal.ask_depth_shares_1pct)
        rows.append(
            CalibratedProbabilitySignal(
                detected_at=signal.detected_at,
                model_version=EARLY_MODEL_VERSION,
                raw_model_version=signal.model_version,
                asset=signal.asset,
                duration_sec=signal.duration_sec,
                market_slug=signal.market_slug,
                condition_id=signal.condition_id,
                outcome=signal.outcome,
                token_id=signal.token_id,
                seconds_to_expiry=signal.seconds_to_expiry,
                threshold_price=signal.threshold_price,
                current_price=signal.current_price,
                best_ask=signal.best_ask,
                raw_fair_prob=signal.fair_prob,
                raw_net_edge=signal.net_edge,
                calibrated_probability=signal.fair_prob,
                calibrated_edge=signal.net_edge,
                estimated_taker_fee_per_share=signal.estimated_taker_fee_per_share,
                executable_shares=signal.executable_shares,
                max_paper_cost=signal.max_paper_cost,
                ask_depth_shares_1pct=signal.ask_depth_shares_1pct,
                ask_depth_usdc_1pct=signal.ask_depth_usdc_1pct,
                time_price_bucket=early_bucket(seconds_since_start, signal.best_ask),
                time_price_rows=0,
                time_price_observed_win_rate=0.0,
                time_price_roi_on_cost=0.0,
                edge_bucket=raw_edge_bucket(signal.net_edge),
                edge_rows=0,
                edge_roi_on_cost=0.0,
                probability_bucket=probability_bucket(signal.fair_prob),
                probability_rows=0,
                probability_roi_on_cost=0.0,
                grade=grade,
                reject_reason="",
                ask_levels_json=signal.ask_levels_json,
                notes=(
                    "Early-entry raw probability model. Primary probability is raw fair_prob from price/threshold/"
                    "volatility/momentum, not historical bucket win-rate."
                ),
            )
        )
    return sorted(rows, key=lambda row: (GRADE_RANK.get(row.grade, 0), row.calibrated_edge, row.executable_shares), reverse=True)


def grade_raw_signal(net_edge: float, depth_shares_1pct: float) -> str:
    if net_edge >= 0.08 and depth_shares_1pct >= 10:
        return "A"
    if net_edge >= 0.03:
        return "B"
    return "C"


def early_bucket(seconds_since_start: int, price: float) -> str:
    if seconds_since_start <= 30:
        start_bucket = "start<=30"
    elif seconds_since_start <= 60:
        start_bucket = "start<=60"
    elif seconds_since_start <= 120:
        start_bucket = "start<=120"
    elif seconds_since_start <= 180:
        start_bucket = "start<=180"
    else:
        start_bucket = "start>180"
    return f"{start_bucket}|{price_bucket(price)}"


def price_bucket(price: float) -> str:
    if price <= 0.05:
        return "px<=5c"
    if price <= 0.25:
        return "px<=25c"
    if price <= 0.60:
        return "px<=60c"
    if price < 0.95:
        return "px<95c"
    return "px>=95c"


def raw_edge_bucket(edge: float) -> str:
    if edge < 0.01:
        return "edge<1%"
    if edge < 0.03:
        return "edge 1-3%"
    if edge < 0.05:
        return "edge 3-5%"
    if edge < 0.08:
        return "edge 5-8%"
    return "edge >=8%"


def probability_bucket(probability: float) -> str:
    p = max(0.0, min(0.999999, probability))
    low = int(p * 10) / 10
    high = low + 0.1
    return f"p{low:.1f}-{high:.1f}"
