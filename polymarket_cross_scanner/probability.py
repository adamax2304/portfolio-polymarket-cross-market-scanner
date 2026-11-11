from __future__ import annotations

from dataclasses import dataclass
from math import erf, log, sqrt
import statistics

from .models import BookLevel, Market, OrderBook
from .rtds import PriceTick
from .strategy import depth_shares_within, depth_usdc_within, estimate_taker_fee_per_share, levels_json
from .utils import utc_now


MODEL_VERSION = "threshold_log_normal_v2"

DEFAULT_ANNUAL_VOL = {
    "btc": 0.55,
    "eth": 0.70,
    "sol": 0.95,
    "xrp": 1.05,
    "doge": 1.20,
    "bnb": 0.75,
}
SECONDS_PER_YEAR = 365 * 24 * 60 * 60


@dataclass(frozen=True)
class ProbabilityEstimate:
    fair_prob_up: float
    sigma_per_sqrt_second: float
    drift_per_second: float
    momentum_15s: float | None
    momentum_60s: float | None
    realized_vol_120s: float | None


@dataclass
class ProbabilityObservation:
    observed_at: str
    model_version: str
    asset: str
    duration_sec: int
    market_slug: str
    condition_id: str
    outcome: str
    token_id: str
    start_ts: int
    end_ts: int
    seconds_to_expiry: int
    threshold_price: float
    current_price: float
    distance_to_threshold: float
    best_ask: float | None
    best_bid: float | None
    ask_depth_shares_1pct: float
    ask_depth_usdc_1pct: float
    ask_levels_json: str
    fair_prob: float
    market_prob: float | None
    gross_edge: float | None
    estimated_taker_fee_per_share: float | None
    net_edge: float | None
    momentum_15s: float | None
    momentum_60s: float | None
    realized_vol_120s: float | None
    sigma_per_sqrt_second: float
    drift_per_second: float
    notes: str = ""

    @property
    def signal_id(self) -> str:
        return f"{self.model_version}:{self.asset}:{self.market_slug}:{self.outcome}:{self.observed_at}"


@dataclass
class ProbabilitySignal:
    detected_at: str
    model_version: str
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
    fair_prob: float
    gross_edge: float
    estimated_taker_fee_per_share: float
    net_edge: float
    executable_shares: float
    max_paper_cost: float
    ask_depth_shares_1pct: float
    ask_depth_usdc_1pct: float
    ask_levels_json: str
    notes: str = ""

    @property
    def signal_id(self) -> str:
        return f"{self.model_version}:{self.asset}:{self.market_slug}:{self.outcome}:{self.detected_at}"


def estimate_probability(
    *,
    asset: str,
    current_price: float,
    threshold_price: float,
    seconds_to_expiry: int,
    ticks: list[PriceTick] | None = None,
) -> ProbabilityEstimate:
    if current_price <= 0 or threshold_price <= 0:
        return ProbabilityEstimate(0.5, default_sigma(asset), 0.0, None, None, None)
    if seconds_to_expiry <= 0:
        return ProbabilityEstimate(1.0 if current_price >= threshold_price else 0.0, default_sigma(asset), 0.0, None, None, None)

    ticks = sorted(ticks or [], key=lambda t: t.ts_ms)
    momentum_15s = momentum(ticks, 15)
    momentum_60s = momentum(ticks, 60)
    realized = realized_volatility(ticks, 120)
    base_sigma = default_sigma(asset)
    sigma = realized if realized and realized > 0 else base_sigma
    sigma = clamp(sigma, base_sigma * 0.75, base_sigma * 4.0)
    drift = drift_from_momentum(momentum_15s, momentum_60s)

    log_distance = log(current_price / threshold_price)
    denom = sigma * sqrt(max(seconds_to_expiry, 1))
    if denom <= 0:
        fair_up = 1.0 if current_price >= threshold_price else 0.0
    else:
        fair_up = normal_cdf((log_distance + drift * seconds_to_expiry) / denom)
    return ProbabilityEstimate(
        fair_prob_up=clamp(fair_up, 0.001, 0.999),
        sigma_per_sqrt_second=sigma,
        drift_per_second=drift,
        momentum_15s=momentum_15s,
        momentum_60s=momentum_60s,
        realized_vol_120s=realized,
    )


def observations_for_market(
    market: Market,
    current_tick: PriceTick,
    ticks: list[PriceTick],
    books: dict[str, OrderBook],
    *,
    taker_fee_rate: float,
) -> list[ProbabilityObservation]:
    if market.threshold_price is None:
        return []
    seconds_left = market.end_ts - int(current_tick.ts_ms / 1000)
    if seconds_left <= 0:
        return []
    estimate = estimate_probability(
        asset=market.asset,
        current_price=current_tick.price,
        threshold_price=market.threshold_price,
        seconds_to_expiry=seconds_left,
        ticks=ticks,
    )
    distance = (current_tick.price - market.threshold_price) / market.threshold_price
    rows: list[ProbabilityObservation] = []
    for outcome in ("Up", "Down"):
        token_id = market.tokens.token_for(outcome)
        book = books.get(token_id)
        best_ask = book.best_ask if book else None
        best_bid = book.best_bid if book else None
        fair = estimate.fair_prob_up if outcome == "Up" else 1 - estimate.fair_prob_up
        fee = estimate_single_taker_fee(best_ask, taker_fee_rate) if best_ask is not None else None
        gross_edge = fair - best_ask if best_ask is not None else None
        net_edge = gross_edge - fee if gross_edge is not None and fee is not None else None
        rows.append(
            ProbabilityObservation(
                observed_at=utc_now(),
                model_version=MODEL_VERSION,
                asset=market.asset,
                duration_sec=market.duration_sec,
                market_slug=market.slug,
                condition_id=market.condition_id,
                outcome=outcome,
                token_id=token_id,
                start_ts=market.start_ts,
                end_ts=market.end_ts,
                seconds_to_expiry=seconds_left,
                threshold_price=market.threshold_price,
                current_price=current_tick.price,
                distance_to_threshold=distance,
                best_ask=best_ask,
                best_bid=best_bid,
                ask_depth_shares_1pct=depth_shares_within(book.asks, best_ask + 0.01) if book and best_ask is not None else 0.0,
                ask_depth_usdc_1pct=depth_usdc_within(book.asks, best_ask + 0.01) if book and best_ask is not None else 0.0,
                ask_levels_json=levels_json(book.asks) if book else "[]",
                fair_prob=fair,
                market_prob=best_ask,
                gross_edge=gross_edge,
                estimated_taker_fee_per_share=fee,
                net_edge=net_edge,
                momentum_15s=estimate.momentum_15s,
                momentum_60s=estimate.momentum_60s,
                realized_vol_120s=estimate.realized_vol_120s,
                sigma_per_sqrt_second=estimate.sigma_per_sqrt_second,
                drift_per_second=estimate.drift_per_second,
            )
        )
    return rows


def signals_from_observations(
    observations: list[ProbabilityObservation],
    *,
    min_net_edge: float,
    min_shares: float,
    max_paper_shares: float,
    min_seconds_to_expiry: int,
    max_seconds_to_expiry: int,
    min_seconds_since_start: int | None = None,
    max_seconds_since_start: int | None = None,
) -> list[ProbabilitySignal]:
    signals: list[ProbabilitySignal] = []
    for obs in observations:
        if obs.best_ask is None or obs.net_edge is None:
            continue
        if obs.seconds_to_expiry < min_seconds_to_expiry or obs.seconds_to_expiry > max_seconds_to_expiry:
            continue
        seconds_since_start = obs.end_ts - obs.start_ts - obs.seconds_to_expiry
        if min_seconds_since_start is not None and seconds_since_start < min_seconds_since_start:
            continue
        if max_seconds_since_start is not None and seconds_since_start > max_seconds_since_start:
            continue
        if obs.net_edge < min_net_edge:
            continue
        executable = min(obs.ask_depth_shares_1pct, max_paper_shares)
        if executable < min_shares:
            continue
        signals.append(
            ProbabilitySignal(
                detected_at=obs.observed_at,
                model_version=obs.model_version,
                asset=obs.asset,
                duration_sec=obs.duration_sec,
                market_slug=obs.market_slug,
                condition_id=obs.condition_id,
                outcome=obs.outcome,
                token_id=obs.token_id,
                seconds_to_expiry=obs.seconds_to_expiry,
                threshold_price=obs.threshold_price,
                current_price=obs.current_price,
                best_ask=obs.best_ask,
                fair_prob=obs.fair_prob,
                gross_edge=obs.gross_edge or 0.0,
                estimated_taker_fee_per_share=obs.estimated_taker_fee_per_share or 0.0,
                net_edge=obs.net_edge,
                executable_shares=executable,
                max_paper_cost=executable * obs.best_ask,
                ask_depth_shares_1pct=obs.ask_depth_shares_1pct,
                ask_depth_usdc_1pct=obs.ask_depth_usdc_1pct,
                ask_levels_json=obs.ask_levels_json,
                notes=(
                    f"Paper-only early probability edge signal. seconds_since_start={seconds_since_start}. "
                    "Uses raw fair_prob vs Polymarket ask as the primary hypothesis; v2 keeps a higher volatility floor to reduce overconfidence."
                ),
            )
        )
    return sorted(signals, key=lambda s: (s.net_edge, s.executable_shares), reverse=True)


def estimate_single_taker_fee(price: float | None, fee_rate: float) -> float:
    if price is None or fee_rate <= 0:
        return 0.0
    return estimate_taker_fee_per_share(price, 0.0, fee_rate)


def executable_single_size(levels: list[BookLevel], max_price: float, max_shares: float) -> tuple[float, float]:
    shares = 0.0
    cost = 0.0
    for level in levels:
        if level.price > max_price or shares >= max_shares:
            break
        take = min(level.size, max_shares - shares)
        shares += take
        cost += take * level.price
    return shares, cost


def default_sigma(asset: str) -> float:
    annual = DEFAULT_ANNUAL_VOL.get(asset, 0.8)
    return annual / sqrt(SECONDS_PER_YEAR)


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def momentum(ticks: list[PriceTick], seconds: int) -> float | None:
    if len(ticks) < 2:
        return None
    latest = ticks[-1]
    cutoff = latest.ts_ms - seconds * 1000
    prior = None
    for tick in reversed(ticks):
        if tick.ts_ms <= cutoff:
            prior = tick
            break
    if prior is None:
        prior = ticks[0]
    if prior.price <= 0 or latest.price <= 0 or prior.ts_ms == latest.ts_ms:
        return None
    return log(latest.price / prior.price)


def realized_volatility(ticks: list[PriceTick], seconds: int) -> float | None:
    if len(ticks) < 4:
        return None
    latest_ts = ticks[-1].ts_ms
    window = [tick for tick in ticks if tick.ts_ms >= latest_ts - seconds * 1000 and tick.price > 0]
    if len(window) < 4:
        return None
    returns: list[float] = []
    for prev, curr in zip(window, window[1:]):
        dt = (curr.ts_ms - prev.ts_ms) / 1000
        if dt <= 0:
            continue
        returns.append(log(curr.price / prev.price) / sqrt(dt))
    if len(returns) < 3:
        return None
    return statistics.pstdev(returns)


def drift_from_momentum(momentum_15s: float | None, momentum_60s: float | None) -> float:
    drift = 0.0
    if momentum_15s is not None:
        drift += 0.65 * momentum_15s / 15
    if momentum_60s is not None:
        drift += 0.35 * momentum_60s / 60
    return clamp(drift, -0.0002, 0.0002)
