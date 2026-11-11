"""Variant C — STRICT_FOK live executor (paper-faithful).

Mirrors the SAFE_ONLY accounting rules from Variant A but executes for real:
  * Batch FOK on BOTH legs at strict pair-cost prices.
  * NO rescue, NO unwind. If one leg fills and the other rejects we *hold* the
    filled leg to expiry (auto-redeem picks up the payout if it wins).
  * Lighter edge filter (default 1.5%) — we don't choke on signals, the strict
    FOK math protects us from negative-edge trades.

Runs alongside the Variant B (maker-first) executor on the same wallet. Both bots
share ``cross_market_live_orders.csv`` and coordinate via the NO_RETRY_RESULTS set
in :mod:`polymarket_cross_scanner.cross_live_executor`, so neither will re-attempt
a signal the other already executed (or has open exposure on).
"""

from .executor import StrictFokLiveExecutor  # noqa: F401
from .runner import run_strict_fok_live  # noqa: F401
