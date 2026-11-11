"""Intra-Polymarket arbitrage scanners (paper / research only).

Three categories:
 1. sum_outcome — multi-outcome events where YES prices should sum to ~$1
 2. calendar     — same question, different end dates; later should be ≥ earlier
 3. negation     — YES/NO duals (paired markets) where prices should sum to $1

NO live execution. Output CSV for human review.
"""
from .sum_outcome import scan_sum_outcome
from .calendar import scan_calendar
from .negation import scan_negation

__all__ = ["scan_sum_outcome", "scan_calendar", "scan_negation"]
