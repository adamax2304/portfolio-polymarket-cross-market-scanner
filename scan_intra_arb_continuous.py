#!/usr/bin/env python3
"""Continuous intra-arb scanner.

Runs all three scanners and APPENDS findings to historical CSV with timestamps.
Designed to be invoked every N minutes by systemd timer or cron.

Output layout:
    data/intra_arb_history/
        sum_outcome_history.csv   — all sum-to-1 findings ever
        calendar_history.csv       — all calendar violations ever
        negation_history.csv       — all negation pair findings ever
        alerts.csv                 — high-edge findings worth attention
        scan_log.csv               — one row per scan (counts + timing)

Strategy:
 - On each invocation, scan all three categories
 - For each finding, write a row with ts, scan_id, and all original fields
 - Findings with edge > alert_threshold also written to alerts.csv
 - This builds up a time series of how edges evolve over hours/days
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from polymarket_cross_scanner.intra_arb import scan_sum_outcome, scan_calendar, scan_negation


def append_csv(path: Path, header: list[str], rows: list[list]) -> None:
    new_file = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(header)
        for r in rows:
            w.writerow(r)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--output-dir", type=str, default="/root/polymarket/cross_scanner/data/intra_arb_history")
    p.add_argument("--min-edge", type=float, default=0.005, help="Min edge to log (any record)")
    p.add_argument("--alert-edge", type=float, default=0.02, help="Min edge to mark as alert")
    p.add_argument("--min-vol", type=float, default=500.0)
    args = p.parse_args()

    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    scan_id = uuid.uuid4().hex[:10]
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    start = time.time()
    print(f"\n[continuous] scan_id={scan_id} ts={ts}")

    # === SUM OUTCOME ===
    print("\n--- sum_outcome ---")
    sum_findings = scan_sum_outcome(
        min_gross_edge=args.min_edge,
        min_event_volume=args.min_vol,
        output_csv=None,
    )
    sum_rows = []
    for f in sum_findings:
        sum_rows.append([
            ts, scan_id, f.event_slug, f.event_title, f.n_markets,
            round(f.sum_yes, 4), f.arb_direction, round(f.gross_edge, 4),
            round(f.min_market_yes, 4), round(f.max_market_yes, 4),
            round(f.total_volume_24h, 0),
            f.url,
        ])
    append_csv(outdir / "sum_outcome_history.csv",
               ["ts", "scan_id", "event_slug", "event_title", "n_markets",
                "sum_yes", "arb_direction", "gross_edge",
                "min_yes", "max_yes", "vol24h", "url"],
               sum_rows)

    # === CALENDAR ===
    print("\n--- calendar ---")
    cal_findings = scan_calendar(
        min_violation=args.min_edge,
        min_volume_24h=args.min_vol,
        output_csv=None,
    )
    cal_rows = []
    for f in cal_findings:
        cal_rows.append([
            ts, scan_id, round(f.violation, 4),
            f.market_earlier.question, round(f.market_earlier.yes_price, 4),
            datetime.fromtimestamp(f.market_earlier.end_ts, tz=timezone.utc).isoformat(timespec="seconds"),
            f.market_earlier.url,
            f.market_later.question, round(f.market_later.yes_price, 4),
            datetime.fromtimestamp(f.market_later.end_ts, tz=timezone.utc).isoformat(timespec="seconds"),
            f.market_later.url,
        ])
    append_csv(outdir / "calendar_history.csv",
               ["ts", "scan_id", "violation",
                "earlier_q", "earlier_yes", "earlier_end", "earlier_url",
                "later_q", "later_yes", "later_end", "later_url"],
               cal_rows)

    # === NEGATION ===
    print("\n--- negation ---")
    neg_findings = scan_negation(
        min_gross_edge=args.min_edge,
        min_volume_24h=args.min_vol,
        output_csv=None,
    )
    neg_rows = []
    for f in neg_findings:
        neg_rows.append([
            ts, scan_id, round(f.sum_yes, 4), round(f.gross_edge, 4), f.direction,
            f.market_a.question, round(f.market_a.yes_price, 4), f.market_a.url,
            f.market_b.question, round(f.market_b.yes_price, 4), f.market_b.url,
        ])
    append_csv(outdir / "negation_history.csv",
               ["ts", "scan_id", "sum_yes", "gross_edge", "direction",
                "a_q", "a_yes", "a_url",
                "b_q", "b_yes", "b_url"],
               neg_rows)

    # === ALERTS ===
    alerts = []
    for f in sum_findings:
        if f.gross_edge >= args.alert_edge:
            alerts.append([ts, scan_id, "sum_outcome", f.gross_edge, f.event_title, f.url])
    for f in cal_findings:
        if f.violation >= args.alert_edge:
            alerts.append([ts, scan_id, "calendar", f.violation,
                           f.market_earlier.question[:80] + " ↔ " + f.market_later.question[:80],
                           f.market_earlier.url])
    for f in neg_findings:
        if f.gross_edge >= args.alert_edge:
            alerts.append([ts, scan_id, "negation", f.gross_edge,
                           f.market_a.question[:80] + " ↔ " + f.market_b.question[:80],
                           f.market_a.url])
    if alerts:
        append_csv(outdir / "alerts.csv",
                   ["ts", "scan_id", "category", "edge", "description", "url"],
                   alerts)

    elapsed = time.time() - start
    append_csv(outdir / "scan_log.csv",
               ["ts", "scan_id", "sum_findings", "calendar_findings", "negation_findings", "alerts", "elapsed_s"],
               [[ts, scan_id, len(sum_findings), len(cal_findings), len(neg_findings), len(alerts), round(elapsed, 1)]])

    print(f"\n[continuous] done in {elapsed:.1f}s | sum={len(sum_findings)} cal={len(cal_findings)} neg={len(neg_findings)} alerts={len(alerts)}")


if __name__ == "__main__":
    main()
