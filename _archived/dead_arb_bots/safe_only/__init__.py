"""Variant A — SAFE_ONLY paper-mode shadow analyzer.

Reads ongoing HFT paper execution sims and applies SAFE_ONLY rules:
  - filter signals by min edge (default 0.03)
  - count only BOTH_FILL_STRICT / BOTH_FILL_SIMULTANEOUS as successful arb
  - partial fills counted as held-to-resolution lottery (avg ~$0)
  - rescue/unwind explicitly disabled

Outputs per-decision rows + rolling summary so we can compare against actual
live execution and see what SAFE_ONLY *would* have done.
"""

from .paper import run_safe_only_paper_loop  # noqa: F401
