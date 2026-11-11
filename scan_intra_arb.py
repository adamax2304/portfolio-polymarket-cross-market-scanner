#!/usr/bin/env python3
"""CLI for intra-Polymarket arbitrage scanners (paper / research only).

Usage:
    python scan_intra_arb.py [--sum] [--calendar] [--negation] [--all]
                              [--min-edge 0.02] [--min-vol 100]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from polymarket_cross_scanner.intra_arb import scan_sum_outcome, scan_calendar, scan_negation


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--sum", action="store_true", help="Run sum-to-1 multi-outcome scanner")
    p.add_argument("--calendar", action="store_true", help="Run calendar monotonicity scanner")
    p.add_argument("--negation", action="store_true", help="Run negation pair scanner")
    p.add_argument("--all", action="store_true", help="Run all scanners")
    p.add_argument("--min-edge", type=float, default=0.02)
    p.add_argument("--min-vol", type=float, default=100.0)
    p.add_argument("--output-dir", type=str, default="data/intra_arb")
    args = p.parse_args()

    run_all = args.all or not (args.sum or args.calendar or args.negation)
    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)

    if run_all or args.sum:
        print("\n" + "=" * 70 + "\n SUM-TO-1 MULTI-OUTCOME SCAN\n" + "=" * 70)
        scan_sum_outcome(
            min_gross_edge=args.min_edge,
            min_event_volume=args.min_vol,
            output_csv=str(outdir / "sum_outcome.csv"),
        )
    if run_all or args.calendar:
        print("\n" + "=" * 70 + "\n CALENDAR MONOTONICITY SCAN\n" + "=" * 70)
        scan_calendar(
            min_violation=args.min_edge,
            min_volume_24h=args.min_vol,
            output_csv=str(outdir / "calendar.csv"),
        )
    if run_all or args.negation:
        print("\n" + "=" * 70 + "\n NEGATION PAIR SCAN\n" + "=" * 70)
        scan_negation(
            min_gross_edge=args.min_edge,
            min_volume_24h=args.min_vol,
            output_csv=str(outdir / "negation.csv"),
        )


if __name__ == "__main__":
    main()
