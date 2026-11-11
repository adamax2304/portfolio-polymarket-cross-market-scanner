"""Market maker configuration."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class MarketMakerConfig:
    # Discovery: select markets matching these constraints
    min_volume_24hr_usd: float = 5000.0           # only quote markets with real traffic
    min_liquidity_usd: float = 2000.0             # require depth
    max_spread_at_top_cents: float = 0.05         # only post if spread <= 5¢ (room to capture)
    min_spread_at_top_cents: float = 0.02         # don't post if spread already < 2¢ (no profit)
    only_priced_below_cents: float = 0.85         # avoid extreme tails (binary collapse risk)
    only_priced_above_cents: float = 0.15
    market_ttl_min_seconds: int = 3600            # market must live ≥1h (we need time to quote)

    # Quote behavior
    quote_size_shares: float = 5.0
    quote_improve_by_cents: float = 0.01          # post 1¢ inside best bid/ask (top of book)
    requote_threshold_cents: float = 0.01         # reprice if best moves by ≥ 1¢
    cancel_if_no_top_seconds: float = 30.0        # cancel if our quote falls off top of book

    # Inventory & risk
    max_net_position_shares: float = 25.0         # +25 long → stop quoting BIDs; -25 short → stop ASKs
    max_gross_quotes_per_market: int = 2          # 1 bid + 1 ask
    max_markets_concurrent: int = 3
    max_capital_per_market_usd: float = 25.0      # max committed in open orders + inventory

    # Fees / slippage
    maker_fee_rate: float = 0.0                   # Polymarket maker fee (varies)
    take_loss_on_inventory_cents: float = 0.01    # max acceptable inventory liquidation cost

    # Sim only
    sim_fill_probability_per_second: float = 0.05  # rough heuristic for paper sim
    sim_horizon_seconds: int = 600                # 10 min simulation window per market

    # Control gate (live only — paper ignores)
    live_armed: bool = False
    paper_only: bool = True

    # Output paths
    paper_csv_path: str = "/root/polymarket/cross_scanner/data/market_maker_paper.csv"
    live_csv_path: str = "/root/polymarket/cross_scanner/data/market_maker_live.csv"
