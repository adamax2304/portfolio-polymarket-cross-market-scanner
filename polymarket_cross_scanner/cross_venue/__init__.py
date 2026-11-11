"""Cross-venue prediction-market arbitrage scanner.

Reads markets from Polymarket, Manifold, Kalshi → matches by topic → reports
implied-probability spreads > threshold.

No execution — research only.
"""
from .scanner import scan_cross_venues

__all__ = ["scan_cross_venues"]
