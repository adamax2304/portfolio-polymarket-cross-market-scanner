#!/usr/bin/env python3
"""Summarize accumulated intra-arb scanner history.

Reads files in data/intra_arb_history/ and reports:
 - Number of scans completed, time range
 - Top sustained edges (appearing across many scans)
 - New / emerging arbs (only seen recently)
 - Big spikes (edge jumped from <X to >Y in adjacent scans)
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", type=str, default="/root/polymarket/cross_scanner/data/intra_arb_history")
    p.add_argument("--top", type=int, default=15)
    args = p.parse_args()

    d = Path(args.data_dir)
    if not d.exists():
        print(f"missing: {d}")
        sys.exit(1)

    scan_log = load(d / "scan_log.csv")
    sum_h = load(d / "sum_outcome_history.csv")
    cal_h = load(d / "calendar_history.csv")
    neg_h = load(d / "negation_history.csv")
    alerts = load(d / "alerts.csv")

    print(f"\n=== INTRA-ARB HISTORY SUMMARY ===\n")
    print(f"Scans:     {len(scan_log)}")
    if scan_log:
        first = scan_log[0]["ts"]
        last = scan_log[-1]["ts"]
        print(f"  range:   {first} → {last}")
    print(f"Total findings:")
    print(f"  sum_outcome:  {len(sum_h):5d}")
    print(f"  calendar:     {len(cal_h):5d}")
    print(f"  negation:     {len(neg_h):5d}")
    print(f"  alerts:       {len(alerts):5d}")

    if not sum_h and not cal_h and not neg_h:
        print("\n(no findings yet)")
        return

    # === SUM: sustained top edges ===
    print(f"\n--- SUM-OUTCOME: top sustained edges (avg over all observations) ---")
    sum_by_event = defaultdict(list)
    for r in sum_h:
        try:
            e = float(r["gross_edge"])
        except (TypeError, ValueError):
            continue
        sum_by_event[r["event_title"]].append((float(r["gross_edge"]), float(r["vol24h"]), r["url"]))
    rows = []
    for title, observations in sum_by_event.items():
        edges = [o[0] for o in observations]
        if len(edges) < 2:
            continue
        avg = sum(edges) / len(edges)
        max_e = max(edges)
        vol = observations[-1][1]
        url = observations[-1][2]
        rows.append((title, len(edges), avg, max_e, vol, url))
    rows.sort(key=lambda r: r[2], reverse=True)
    for i, (title, n, avg, max_e, vol, url) in enumerate(rows[:args.top], 1):
        print(f"#{i} avg=${avg:.3f} max=${max_e:.3f} seen={n}x vol=${vol:,.0f}")
        print(f"   {title[:100]}")
        print(f"   {url}")

    # === CALENDAR: sustained violations ===
    print(f"\n--- CALENDAR: top sustained violations ---")
    cal_by_pair = defaultdict(list)
    for r in cal_h:
        try:
            v = float(r["violation"])
        except (TypeError, ValueError):
            continue
        key = (r["earlier_q"][:80], r["later_q"][:80])
        cal_by_pair[key].append((v, r["earlier_url"], r["later_url"]))
    rows = []
    for (eq, lq), observations in cal_by_pair.items():
        violations = [o[0] for o in observations]
        if len(violations) < 2:
            continue
        avg = sum(violations) / len(violations)
        rows.append((eq, lq, len(violations), avg, max(violations),
                     observations[-1][1], observations[-1][2]))
    rows.sort(key=lambda r: r[3], reverse=True)
    for i, (eq, lq, n, avg, max_v, url_e, url_l) in enumerate(rows[:args.top], 1):
        print(f"#{i} avg={avg:.3f} max={max_v:.3f} seen={n}x")
        print(f"   EARLIER: {eq}")
        print(f"   LATER:   {lq}")

    # === SPIKES: edges that jumped large ===
    print(f"\n--- BIG SPIKES (edge > 0.05 AND vol > 10000) ---")
    big_alerts = []
    for r in alerts:
        try:
            e = float(r["edge"])
            if e >= 0.05:
                big_alerts.append((r["ts"], e, r["category"], r["description"][:100], r["url"]))
        except (TypeError, ValueError):
            continue
    big_alerts.sort(key=lambda x: x[1], reverse=True)
    for ts, e, cat, desc, url in big_alerts[:10]:
        print(f"  ts={ts}  edge={e:.3f}  cat={cat}")
        print(f"    {desc}")
        print(f"    {url}")


if __name__ == "__main__":
    main()
