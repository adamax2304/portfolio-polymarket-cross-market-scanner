"""Real-time WebSocket-based intra-Polymarket arb scanner.

Subscribes to Polymarket CLOB WS for all negRisk event tokens,
maintains live per-event Σbid/Σask state, and emits alerts when
arb threshold is crossed. Paper-only — no execution.

Output: CSV with alert events, durability tracking, and paper PnL sim.
"""
from .runner import run_ws_arb_scanner

__all__ = ["run_ws_arb_scanner"]
