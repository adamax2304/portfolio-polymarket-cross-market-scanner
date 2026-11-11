from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class TokenPair:
    up: str
    down: str

    def token_for(self, outcome: str) -> str:
        normalized = outcome.lower()
        if normalized == "up":
            return self.up
        if normalized == "down":
            return self.down
        raise ValueError(f"Unsupported outcome: {outcome}")


@dataclass
class Market:
    asset: str
    duration_sec: int
    slug: str
    question: str
    condition_id: str
    tokens: TokenPair
    start_ts: int
    end_ts: int
    tick_size: float = 0.01
    neg_risk: bool = False
    liquidity: float = 0.0
    volume_24h: float = 0.0
    up_price_hint: float | None = None
    down_price_hint: float | None = None
    threshold_price: float | None = None
    threshold_source: str = "unknown"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return self.condition_id or self.slug

    @property
    def is_active_now(self) -> bool:
        now = int(datetime.now(timezone.utc).timestamp())
        return self.start_ts <= now < self.end_ts

    @property
    def seconds_to_expiry(self) -> int:
        now = int(datetime.now(timezone.utc).timestamp())
        return self.end_ts - now


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: list[BookLevel]
    asks: list[BookLevel]
    fetched_at: str

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None


@dataclass
class ArbSignal:
    detected_at: str
    asset: str
    end_ts: int
    lower_market_slug: str
    higher_market_slug: str
    lower_threshold: float | None
    higher_threshold: float | None
    long_up_slug: str
    long_down_slug: str
    up_token_id: str
    down_token_id: str
    up_ask: float
    down_ask: float
    top_cost: float
    top_edge: float
    executable_shares: float
    avg_cost: float
    avg_edge: float
    orientation_source: str
    estimated_taker_fee_per_share: float = 0.0
    net_avg_edge: float = 0.0
    up_book_depth_shares_1pct: float = 0.0
    down_book_depth_shares_1pct: float = 0.0
    up_book_depth_usdc_1pct: float = 0.0
    down_book_depth_usdc_1pct: float = 0.0
    up_ask_levels_json: str = "[]"
    down_ask_levels_json: str = "[]"
    notes: str = ""

    @property
    def signal_id(self) -> str:
        return f"{self.asset}:{self.end_ts}:{self.long_up_slug}:{self.long_down_slug}:{self.orientation_source}"


@dataclass
class PaperTrade:
    opened_at: str
    signal_id: str
    asset: str
    shares: float
    cost: float
    worst_case_payout: float
    locked_profit_estimate: float
    status: str = "OPEN"


@dataclass
class PaperSettlement:
    signal_id: str
    settled_at: str
    asset: str
    end_ts: int
    shares: float
    cost: float
    lower_market_slug: str
    higher_market_slug: str
    lower_threshold: float | None
    higher_threshold: float | None
    lower_final_price: float | None
    higher_final_price: float | None
    up_leg_won: bool | None
    down_leg_won: bool | None
    payout: float | None
    realized_profit: float | None
    locked_profit_estimate: float
    status: str
    notes: str = ""


@dataclass
class CrossMarketExecutionSim:
    simulated_at: str
    signal_id: str
    asset: str
    end_ts: int
    target_shares: float
    leg_order: str
    initial_up_ask: float
    initial_down_ask: float
    initial_pair_cost: float
    strict_max_pair_cost: float
    rescue_max_pair_cost: float
    fresh_up_ask: float | None
    fresh_down_ask: float | None
    fresh_up_bid: float | None
    fresh_down_bid: float | None
    first_leg: str
    first_filled: bool
    first_avg_price: float | None
    first_cost: float
    second_leg: str
    second_strict_filled: bool
    second_strict_avg_price: float | None
    second_strict_cost: float
    second_rescue_filled: bool
    second_rescue_avg_price: float | None
    second_rescue_cost: float
    unwind_filled: bool
    unwind_avg_price: float | None
    unwind_proceeds: float
    unwind_pnl: float | None
    final_action: str
    final_pair_cost: float | None
    final_pair_edge: float | None
    locked_profit_estimate: float | None
    unhedged_worst_case_loss: float
    refetch_latency_ms: int
    decision_reason: str
    up_ask_levels_json: str = "[]"
    down_ask_levels_json: str = "[]"
    up_bid_levels_json: str = "[]"
    down_bid_levels_json: str = "[]"


@dataclass
class CrossMarketMissedOpportunity:
    detected_at: str
    simulated_at: str
    signal_id: str
    asset: str
    end_ts: int
    final_action: str
    miss_category: str
    decision_reason: str
    initial_pair_cost: float
    fresh_pair_cost: float | None
    pair_cost_move: float | None
    top_edge: float
    avg_edge: float
    net_avg_edge: float
    executable_shares: float
    target_shares: float
    refetch_latency_ms: int
    up_ask: float
    down_ask: float
    fresh_up_ask: float | None
    fresh_down_ask: float | None
    notes: str = ""


@dataclass
class CrossMarketScanTiming:
    scanned_at: str
    mode: str
    markets: int
    books: int
    ws_books: int
    rest_books: int
    other_books: int
    missing_books: int
    signals: int
    execution_sims: int
    missed_opportunities: int
    paper_trades: int
    gamma_ms: int
    threshold_ms: int
    store_ms: int
    book_ms: int
    strategy_ms: int
    execution_ms: int
    paper_ms: int
    settlement_ms: int
    total_ms: int
    pair_candidates: int = 0
    pair_fresh: int = 0
    pair_missing_up: int = 0
    pair_missing_down: int = 0
    pair_fresh_pct: float = 0.0
    notes: str = ""


@dataclass
class CrossHftPaperEvent:
    detected_at: str
    trigger_source: str
    trigger_token_id: str
    pair_id: str
    signal_id: str
    asset: str
    end_ts: int
    seconds_to_expiry: int
    lower_market_slug: str
    higher_market_slug: str
    lower_threshold: float | None
    higher_threshold: float | None
    orientation_source: str
    up_token_id: str
    down_token_id: str
    up_book_age_ms: int
    down_book_age_ms: int
    up_ask: float
    down_ask: float
    top_cost: float
    top_edge: float
    executable_shares: float
    avg_cost: float
    avg_edge: float
    estimated_taker_fee_per_share: float
    net_avg_edge: float
    up_book_depth_shares_1pct: float
    down_book_depth_shares_1pct: float
    up_book_depth_usdc_1pct: float
    down_book_depth_usdc_1pct: float
    eval_latency_ms: int
    up_ask_levels_json: str = "[]"
    down_ask_levels_json: str = "[]"
    notes: str = ""


@dataclass
class CrossHftPaperHeartbeat:
    ts: str
    mode: str
    markets: int
    pairs: int
    subscribed_tokens: int
    updates_drained: int
    unique_tokens_updated: int
    pairs_evaluated: int
    opportunities: int
    execution_sims: int
    missed_opportunities: int
    suppressed_by_cooldown: int
    pair_fresh: int
    pair_missing_up: int
    pair_missing_down: int
    pair_fresh_pct: float
    gamma_ms: int
    loop_ms: int
    flush_ms: int
    notes: str = ""


@dataclass
class CrossHftPairEvaluation:
    evaluated_at: str
    trigger_source: str
    trigger_token_id: str
    pair_id: str
    asset: str
    end_ts: int
    lower_market_slug: str
    higher_market_slug: str
    orientation_source: str
    up_token_id: str
    down_token_id: str
    up_book_age_ms: int
    down_book_age_ms: int
    up_ask: float | None
    down_ask: float | None
    top_cost: float | None
    executable_shares: float
    avg_cost: float | None
    estimated_taker_fee_per_share: float | None
    net_avg_edge: float | None
    reject_reason: str
    eval_latency_ms: int
    notes: str = ""


@dataclass
class CrossMarketLiveOrder:
    attempted_at: str
    signal_id: str
    asset: str
    end_ts: int
    lower_market_slug: str
    higher_market_slug: str
    source_risk: str
    shares: float
    up_token_id: str
    down_token_id: str
    up_limit_price: float | None
    down_limit_price: float | None
    first_leg: str
    first_order_id: str
    first_status: str
    second_order_id: str
    second_status: str
    unwind_order_id: str
    unwind_status: str
    estimated_cost: float
    final_action: str
    live_result: str
    bridge_latency_ms: int
    notes: str = ""


@dataclass
class CrossMarketLiveCandidate:
    observed_at: str
    signal_id: str
    asset: str
    end_ts: int
    lower_market_slug: str
    higher_market_slug: str
    source_risk: str
    sim_final_action: str
    retry_attempt: int
    live_decision: str
    retryable: bool
    next_retry_after_ms: int
    estimated_cost: float
    up_ask: float | None
    down_ask: float | None
    fresh_up_ask: float | None
    fresh_down_ask: float | None
    reason: str = ""


@dataclass
class CrossMarketLiveLatency:
    measured_at: str
    signal_id: str
    live_result: str
    action: str
    submit_ms: int = 0
    confirm_ms: int = 0
    rescue_ms: int = 0
    unwind_ms: int = 0
    balance_ms: int = 0
    total_ms: int = 0
    bridge_mode: str = ""
    notes: str = ""


@dataclass
class CrossMarketLiveRedemption:
    attempted_at: str
    signal_id: str
    market_slug: str
    condition_id: str
    command: str
    redeem_result: str
    tx_hash: str
    notes: str = ""
