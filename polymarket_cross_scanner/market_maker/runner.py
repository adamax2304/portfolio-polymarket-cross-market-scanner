"""Entrypoint for market-maker runs (paper or live).

CLI integration: invoked via `python main.py market-maker-paper [args]`.
Live mode is intentionally NOT wired yet — it requires bridge wiring,
quote-tracking, cancel-on-disconnect, and per-market arming. Once paper
output shows positive expected PnL across many markets and long horizons,
we'll wire live mode.
"""
from __future__ import annotations

from .config import MarketMakerConfig
from .paper_sim import run_paper_sim
from .paper_sim_v2 import run_paper_sim_v2
from .paper_sim_v3 import run_paper_sim_v3


def run_paper_v3(args) -> None:
    cfg = MarketMakerConfig(
        min_volume_24hr_usd=getattr(args, "mm_min_volume_24h", 5000.0),
        min_liquidity_usd=getattr(args, "mm_min_liquidity", 2000.0),
        max_spread_at_top_cents=getattr(args, "mm_max_spread_cents", 0.05),
        min_spread_at_top_cents=getattr(args, "mm_min_spread_cents", 0.02),
        only_priced_below_cents=getattr(args, "mm_price_max", 0.85),
        only_priced_above_cents=getattr(args, "mm_price_min", 0.15),
        quote_size_shares=getattr(args, "mm_quote_size", 5.0),
        quote_improve_by_cents=getattr(args, "mm_improve_cents", 0.01),
        max_net_position_shares=getattr(args, "mm_max_position", 25.0),
        max_markets_concurrent=getattr(args, "mm_max_markets", 3),
        sim_horizon_seconds=getattr(args, "mm_duration_s", 600),
        paper_csv_path=getattr(args, "mm_paper_csv", "/root/polymarket/cross_scanner/data/market_maker_paper.csv"),
    )
    run_paper_sim_v3(cfg, duration_seconds=cfg.sim_horizon_seconds)


def run_paper_v2(args) -> None:
    cfg = MarketMakerConfig(
        min_volume_24hr_usd=getattr(args, "mm_min_volume_24h", 5000.0),
        min_liquidity_usd=getattr(args, "mm_min_liquidity", 2000.0),
        max_spread_at_top_cents=getattr(args, "mm_max_spread_cents", 0.05),
        min_spread_at_top_cents=getattr(args, "mm_min_spread_cents", 0.02),
        only_priced_below_cents=getattr(args, "mm_price_max", 0.85),
        only_priced_above_cents=getattr(args, "mm_price_min", 0.15),
        quote_size_shares=getattr(args, "mm_quote_size", 5.0),
        quote_improve_by_cents=getattr(args, "mm_improve_cents", 0.01),
        max_net_position_shares=getattr(args, "mm_max_position", 25.0),
        max_markets_concurrent=getattr(args, "mm_max_markets", 3),
        sim_horizon_seconds=getattr(args, "mm_duration_s", 600),
        paper_csv_path=getattr(args, "mm_paper_csv", "/root/polymarket/cross_scanner/data/market_maker_paper.csv"),
    )
    run_paper_sim_v2(cfg, duration_seconds=cfg.sim_horizon_seconds)


def run_paper(args) -> None:
    cfg = MarketMakerConfig(
        min_volume_24hr_usd=getattr(args, "mm_min_volume_24h", 5000.0),
        min_liquidity_usd=getattr(args, "mm_min_liquidity", 2000.0),
        max_spread_at_top_cents=getattr(args, "mm_max_spread_cents", 0.05),
        min_spread_at_top_cents=getattr(args, "mm_min_spread_cents", 0.02),
        only_priced_below_cents=getattr(args, "mm_price_max", 0.85),
        only_priced_above_cents=getattr(args, "mm_price_min", 0.15),
        quote_size_shares=getattr(args, "mm_quote_size", 5.0),
        quote_improve_by_cents=getattr(args, "mm_improve_cents", 0.01),
        max_net_position_shares=getattr(args, "mm_max_position", 25.0),
        max_markets_concurrent=getattr(args, "mm_max_markets", 3),
        sim_horizon_seconds=getattr(args, "mm_duration_s", 600),
        sim_fill_probability_per_second=getattr(args, "mm_sim_fill_p", 0.05),
        paper_csv_path=getattr(args, "mm_paper_csv", "/root/polymarket/cross_scanner/data/market_maker_paper.csv"),
    )
    run_paper_sim(cfg, duration_seconds=cfg.sim_horizon_seconds)
