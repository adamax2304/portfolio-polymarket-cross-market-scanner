"""Variant B — MAKER_FIRST live execution.

Replaces FOK-on-both-legs with a maker-first flow:
  1. POST_ONLY GTC on the cheap leg
  2. Wait up to ``cross_live_maker_timeout_ms``
  3. If maker fills, FOK BUY the expensive leg immediately
  4. If maker times out, cancel and skip the signal
  5. NO rescue, NO unwind. One-leg fills are held to resolution.

This package wraps the existing cross-hft scanner discovery + signal flow but swaps the
executor implementation.
"""

from .executor import MakerFirstLiveExecutor  # noqa: F401
from .runner import run_maker_first_live  # noqa: F401
