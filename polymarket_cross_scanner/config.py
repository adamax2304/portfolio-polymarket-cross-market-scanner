from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os


ASSET_NAMES = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "xrp": "xrp",
    "doge": "dogecoin",
    "bnb": "bnb",
}

ASSET_DISPLAY = {
    "btc": "Bitcoin",
    "eth": "Ethereum",
    "sol": "Solana",
    "xrp": "XRP",
    "doge": "Dogecoin",
    "bnb": "BNB",
}

ASSET_SYMBOLS = {
    "btc": "BTC",
    "eth": "ETH",
    "sol": "SOL",
    "xrp": "XRP",
    "doge": "DOGE",
    "bnb": "BNB",
}

DURATION_LABELS = {
    300: "5m",
    900: "15m",
    1800: "30m",
    3600: "1h",
    14400: "4h",
}


@dataclass
class ScannerConfig:
    root: Path = Path.cwd()
    gamma_api_base: str = os.getenv("POLYMARKET_GAMMA_API_BASE", "https://gamma-api.polymarket.com")
    clob_api_base: str = os.getenv("POLYMARKET_CLOB_API_BASE", "https://clob.polymarket.com")
    rtds_ws: str = os.getenv("POLYMARKET_RTDS_WS", "wss://ws-live-data.polymarket.com")
    assets: list[str] = field(default_factory=lambda: ["btc", "eth", "sol", "xrp", "doge", "bnb"])
    durations_sec: list[int] = field(default_factory=lambda: [300, 900, 1800, 3600, 14400])
    request_timeout: float = 15.0
    min_edge: float = 0.01
    min_shares: float = 5.0
    min_liquidity: float = 0.0
    max_paper_shares: float = 25.0
    clob_min_order_size_shares: float = 5.0
    clob_min_order_notional_usdc: float = 1.0
    clob_min_order_notional_buffer_usdc: float = 0.02
    clob_min_notional_padding_enabled: bool = True
    clob_min_notional_max_extra_usdc: float = 1.25
    taker_fee_rate: float = 0.07
    rescue_enabled: bool = True
    rescue_max_extra_cost: float = 0.05
    min_seconds_to_expiry: int = 20
    max_orderbook_age_seconds: int = 120
    cross_min_net_edge: float = 0.01
    cross_quote_stability_window_seconds: float = 30.0
    cross_quote_stability_max_move: float = 0.0
    cross_quote_stability_min_samples: int = 2
    paper_settlement_grace_seconds: int = 45
    threshold_capture_max_after_seconds: int = 45
    threshold_capture_max_before_seconds: int = 10
    allow_price_inferred_paper: bool = False
    market_lookup_offsets: int = 3
    stale_threshold_seconds: int = 2 * 86400
    clob_bridge_path: str = os.getenv("POLY_CLOB_BRIDGE_PATH", "")
    clob_bridge_server_path: str = os.getenv("POLY_CLOB_SERVER_PATH", "")
    clob_bridge_cwd: str = os.getenv("POLY_CLOB_BRIDGE_CWD", "")
    clob_bridge_env_path: str = os.getenv("POLY_CLOB_ENV_PATH", "")
    clob_bridge_python: str = os.getenv("POLY_PYTHON_CMD", "python")
    control_state_file: str = os.getenv("CROSS_CONTROL_STATE_PATH", "")
    clob_book_workers: int = int(os.getenv("POLY_CLOB_BOOK_WORKERS", "16"))
    gamma_market_workers: int = int(os.getenv("POLY_GAMMA_MARKET_WORKERS", "16"))
    clob_ws_enabled: bool = os.getenv("POLY_CLOB_WS_ENABLED", "0").lower() in {"1", "true", "yes"}
    clob_ws_url: str = os.getenv("POLY_CLOB_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market")
    clob_ws_max_book_age_seconds: float = float(os.getenv("POLY_CLOB_WS_MAX_BOOK_AGE_SECONDS", "30.0"))
    clob_ws_max_assets_per_conn: int = int(os.getenv("POLY_CLOB_WS_MAX_ASSETS_PER_CONN", "500"))
    cross_execution_sim_enabled: bool = True
    cross_execution_refetch_delay_ms: int = 50
    cross_execution_entry_slippage: float = 0.01
    cross_execution_rescue_extra_cost: float = 0.05
    cross_execution_simultaneous_enabled: bool = True
    cross_execution_dynamic_sizing_enabled: bool = True
    cross_execution_paper_requires_locked: bool = True
    cross_livelike_report_enabled: bool = True
    cross_livelike_latency_floor_ms: int = 250
    cross_livelike_latency_edge_haircut_per_100ms: float = 0.001
    cross_fast_loop_enabled: bool = True
    cross_discovery_interval_seconds: float = 60.0
    cross_eval_interval_seconds: float = 2.0
    cross_fast_ws_only_books: bool = True
    cross_book_warm_enabled: bool = True
    cross_book_warm_interval_seconds: float = 15.0
    cross_book_warm_stale_after_seconds: float = 20.0
    cross_book_warm_max_tokens_per_cycle: int = 160
    cross_book_warm_near_expiry_seconds: int = 120
    cross_book_warm_near_interval_seconds: float = 5.0
    cross_live_enabled: bool = False
    cross_live_max_shares: float = 5.0
    cross_live_max_daily_cost_usdc: float = 0.0
    cross_live_max_open_arbs: int = 2
    cross_live_allow_research_cross_family: bool = True
    cross_live_allow_price_inferred: bool = False
    # Hard filter: when True, ONLY trade signals with orientation_source == "same_threshold"
    # (true cross-threshold arb where the inequality structure guarantees min 1 winner).
    # Blocks LOW_SAME_UPDOWN_FAMILY, MEDIUM_SAME_DURATION_CROSS_FAMILY, RESEARCH_CROSS_FAMILY,
    # HIGH_PRICE_INFERRED, UNKNOWN_SOURCE — these are cross-WINDOW pairs, not arb.
    cross_live_safe_signals_only: bool = False
    # When safe-only is on, also require both legs share an EXACT end_ts (oracle resolves them
    # at the same moment). Different end_ts = different oracle calls = potential 2x loser.
    cross_live_require_same_end_ts: bool = True
    cross_live_preflight_enabled: bool = True
    cross_live_preflight_use_ws_levels: bool = True
    cross_live_preflight_rest_verify: bool = False
    cross_live_max_sim_age_ms: int = 2000
    cross_live_research_min_depth_multiplier: float = 1.25
    cross_live_research_min_net_edge: float = 0.01
    cross_live_redeem_enabled: bool = True
    cross_live_redeem_interval_seconds: int = 300
    cross_live_execution_mode: str = "batch_fok"
    cross_live_retry_cooldown_ms: int = 1500
    cross_live_max_attempts_per_signal: int = 5
    cross_live_guard_cache_seconds: float = 1.5
    cross_live_confirm_unknown_only: bool = True
    cross_live_unwind_dynamic_bids: bool = True
    cross_live_research_oracle_divergence_guard: bool = True
    cross_live_research_max_oracle_divergence_bps: int = 15
    cross_live_research_require_binance_threshold: bool = True
    cross_live_maker_first_enabled: bool = False
    cross_live_maker_first_min_edge: float = 0.03
    # Guard worker (Variant B): cancel maker if cheap mid drops, expensive ask rises, or
    # re-evaluated edge would be negative when the maker fills.
    cross_live_maker_guard_enabled: bool = True
    cross_live_maker_guard_adverse_cheap_drop: float = 0.02
    cross_live_maker_guard_expensive_rise: float = 0.02
    cross_live_maker_guard_min_refill_edge: float = 0.0
    cross_live_maker_timeout_ms: int = 5000
    cross_live_maker_poll_interval_ms: int = 200
    cross_live_maker_cheap_leg_below: float = 0.50
    cross_live_strict_fok_enabled: bool = False
    cross_live_strict_fok_min_edge: float = 0.015
    cross_live_strict_fok_rescue_enabled: bool = False
    probability_risk_bankroll_usdc: float = 1000.0
    probability_risk_kelly_fraction: float = 0.15
    probability_risk_max_kelly_fraction: float = 0.08
    probability_risk_min_trade_cost_usdc: float = 0.0
    probability_risk_max_trade_cost_usdc: float = 25.0
    probability_risk_daily_stop_loss_usdc: float = 1_000_000.0
    probability_risk_max_daily_cost_usdc: float = 1_000_000.0
    probability_risk_max_open_cost_usdc: float = 1_000_000.0
    probability_risk_max_asset_cost_usdc: float = 1_000_000.0
    probability_risk_max_bucket_cost_usdc: float = 1_000_000.0
    probability_risk_max_open_positions: int = 100_000
    probability_risk_position_grace_seconds: int = 90
    probability_auto_settlement_batch_size: int = 50
    probability_auto_settlement_interval_seconds: int = 300

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "scanner.sqlite3"

    @property
    def threshold_path(self) -> Path:
        return self.data_dir / "thresholds.json"

    @property
    def control_state_path(self) -> Path:
        return Path(self.control_state_file) if self.control_state_file else self.data_dir / "control_state.json"

    @property
    def signals_csv(self) -> Path:
        return self.data_dir / "signals.csv"

    @property
    def paper_csv(self) -> Path:
        return self.data_dir / "paper_trades.csv"

    @property
    def paper_settlements_csv(self) -> Path:
        return self.data_dir / "paper_settlements.csv"

    @property
    def cross_execution_sim_csv(self) -> Path:
        return self.data_dir / "cross_market_execution_sim.csv"

    @property
    def cross_missed_opportunities_csv(self) -> Path:
        return self.data_dir / "cross_market_missed_opportunities.csv"

    @property
    def cross_scan_timing_csv(self) -> Path:
        return self.data_dir / "cross_market_scan_timing.csv"

    @property
    def cross_hft_paper_events_csv(self) -> Path:
        return self.data_dir / "cross_hft_paper_events.csv"

    @property
    def cross_hft_paper_execution_csv(self) -> Path:
        return self.data_dir / "cross_hft_paper_execution_sim.csv"

    @property
    def cross_hft_paper_missed_csv(self) -> Path:
        return self.data_dir / "cross_hft_paper_missed_opportunities.csv"

    @property
    def cross_hft_paper_heartbeat_csv(self) -> Path:
        return self.data_dir / "cross_hft_paper_heartbeat.csv"

    @property
    def cross_hft_pair_evaluations_csv(self) -> Path:
        return self.data_dir / "cross_hft_pair_evaluations.csv"

    @property
    def cross_livelike_ledger_csv(self) -> Path:
        return self.data_dir / "cross_market_livelike_ledger.csv"

    @property
    def cross_livelike_report_csv(self) -> Path:
        return self.data_dir / "cross_market_livelike_report.csv"

    @property
    def cross_livelike_summary_md(self) -> Path:
        return self.data_dir / "cross_market_livelike_summary.md"

    @property
    def cross_live_orders_csv(self) -> Path:
        return self.data_dir / "cross_market_live_orders.csv"

    @property
    def cross_live_candidates_csv(self) -> Path:
        return self.data_dir / "cross_market_live_candidates.csv"

    @property
    def cross_live_latency_csv(self) -> Path:
        return self.data_dir / "cross_market_live_latency.csv"

    @property
    def cross_live_redemptions_csv(self) -> Path:
        return self.data_dir / "cross_market_live_redemptions.csv"

    @property
    def cross_family_oracle_decisions_csv(self) -> Path:
        return self.data_dir / "cross_family_oracle_decisions.csv"

    @property
    def cross_family_resolution_audit_csv(self) -> Path:
        return self.data_dir / "cross_family_resolution_audit.csv"

    @property
    def cross_family_resolution_audit_summary_md(self) -> Path:
        return self.data_dir / "cross_family_resolution_audit_summary.md"

    @property
    def probability_observations_csv(self) -> Path:
        return self.data_dir / "probability_observations.csv"

    @property
    def probability_signals_csv(self) -> Path:
        return self.data_dir / "probability_signals.csv"

    @property
    def probability_calibrated_signals_csv(self) -> Path:
        return self.data_dir / "probability_calibrated_signals.csv"

    @property
    def probability_risk_decisions_csv(self) -> Path:
        return self.data_dir / "probability_risk_decisions.csv"

    @property
    def probability_settlements_csv(self) -> Path:
        return self.data_dir / "probability_settlements.csv"

    @property
    def probability_report_csv(self) -> Path:
        return self.data_dir / "probability_report.csv"

    @property
    def probability_v0_filter_decisions_csv(self) -> Path:
        return self.data_dir / "probability_v0_filter_decisions.csv"

    @property
    def probability_v0_live_labels_csv(self) -> Path:
        return self.data_dir / "probability_v0_live_labels.csv"

    @property
    def probability_v0_filter_report_csv(self) -> Path:
        return self.data_dir / "probability_v0_filter_report.csv"

    @property
    def probability_v0_filter_summary_md(self) -> Path:
        return self.data_dir / "probability_v0_filter_summary.md"

    @property
    def probability_v0_forward_decisions_csv(self) -> Path:
        return self.data_dir / "probability_v0_forward_decisions.csv"

    @property
    def probability_v0_forward_report_csv(self) -> Path:
        return self.data_dir / "probability_v0_forward_report.csv"

    @property
    def probability_v0_forward_summary_md(self) -> Path:
        return self.data_dir / "probability_v0_forward_summary.md"

    @property
    def probability_v01_rules_md(self) -> Path:
        return self.data_dir / "probability_v01_rules.md"

    @property
    def probability_v01_shadow_entries_csv(self) -> Path:
        return self.data_dir / "probability_v01_shadow_entries.csv"

    @property
    def probability_v01_shadow_ledger_csv(self) -> Path:
        return self.data_dir / "probability_v01_shadow_ledger.csv"

    @property
    def probability_v01_shadow_report_csv(self) -> Path:
        return self.data_dir / "probability_v01_shadow_report.csv"

    @property
    def probability_v01_shadow_summary_md(self) -> Path:
        return self.data_dir / "probability_v01_shadow_summary.md"

    @property
    def probability_v01_core_report_csv(self) -> Path:
        return self.data_dir / "probability_v01_core_report.csv"

    @property
    def probability_v01_core_summary_md(self) -> Path:
        return self.data_dir / "probability_v01_core_summary.md"

    @property
    def cross_window_report_csv(self) -> Path:
        return self.data_dir / "cross_market_window_report.csv"

    @property
    def cross_window_summary_md(self) -> Path:
        return self.data_dir / "cross_market_window_summary.md"

    @property
    def health_dashboard_md(self) -> Path:
        return self.data_dir / "bot_health_dashboard.md"

    @property
    def probability_research_dir(self) -> Path:
        return self.data_dir / "probability_research"

    @property
    def probability_calibration_dir(self) -> Path:
        return self.probability_research_dir / "0x50f7_full" / "calibration"
