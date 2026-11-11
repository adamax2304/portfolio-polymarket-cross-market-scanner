#!/usr/bin/env python3
"""CLI entrypoint for cross-venue arbitrage scanner.

Usage:
    python scan_cross_venues.py [--min-spread 0.03] [--min-similarity 0.4] \
        [--output data/cross_venue_arb.csv] [--venues polymarket,manifold,kalshi,predictit]
"""
import argparse
import sys
from pathlib import Path

# Make local package importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

from polymarket_cross_scanner.cross_venue import scan_cross_venues


def main() -> None:
    p = argparse.ArgumentParser(description="Cross-venue prediction market arb scanner")
    p.add_argument("--min-spread", type=float, default=0.03, help="Min absolute YES-price spread to report")
    p.add_argument("--min-similarity", type=float, default=0.4, help="Min Jaccard similarity for fuzzy match")
    p.add_argument("--min-volume-24h", type=float, default=100.0, help="Min 24h volume USD")
    p.add_argument("--output", type=str, default="data/cross_venue_arb.csv")
    p.add_argument("--venues", type=str, default="polymarket,manifold,kalshi,predictit")
    args = p.parse_args()
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    matches = scan_cross_venues(
        min_spread=args.min_spread,
        min_similarity=args.min_similarity,
        min_volume_24h=args.min_volume_24h,
        output_csv=args.output,
        venues=venues,
    )
    print(f"\n=== Done. {len(matches)} matches above threshold. ===")


if __name__ == "__main__":
    main()
