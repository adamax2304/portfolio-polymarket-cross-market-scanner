"""Pure market-making module: post both bid and ask on a single liquid market, capture spread.

Two execution paths:
 - paper_sim:  simulate quote updates + fill events, log expected PnL. No real orders.
 - live:      post real POST_ONLY orders, manage inventory + cancellation. Gated by control_state.

Strategy parameters live in MarketMakerConfig.
"""
from .config import MarketMakerConfig
from .paper_sim import run_paper_sim

__all__ = ["MarketMakerConfig", "run_paper_sim"]
