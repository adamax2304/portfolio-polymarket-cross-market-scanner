#!/usr/bin/env python3
"""CLI entry for WS arb scanner."""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from polymarket_cross_scanner.ws_arb import run_ws_arb_scanner


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--min-edge", type=float, default=0.02, help="Min edge to alert")
    p.add_argument("--min-volume", type=float, default=500.0, help="Min event vol24h to subscribe")
    p.add_argument("--max-events", type=int, default=120)
    p.add_argument("--alert-cooldown-s", type=float, default=15.0)
    p.add_argument("--alert-csv", type=str, default="/root/polymarket/cross_scanner/data/ws_arb_alerts.csv")
    args = p.parse_args()
    asyncio.run(run_ws_arb_scanner(
        min_edge=args.min_edge,
        min_volume_24h=args.min_volume,
        max_events=args.max_events,
        alert_cooldown_seconds=args.alert_cooldown_s,
        alert_csv_path=args.alert_csv,
    ))


if __name__ == "__main__":
    main()
