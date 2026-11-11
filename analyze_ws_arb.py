#!/usr/bin/env python3
"""Summarize WS arb scanner alerts.csv (v3 schema: walk-the-book execution).

Three PnL models, increasingly realistic:
  1. NAIVE          — assume infinite depth, capture edge_500ms - fee on full capital
  2. DEPTH-BOUNDED  — capture limited by single-best-level bottleneck size
  3. WALK-THE-BOOK  — realistic: consume multiple book levels (VWAP), profit-optimal
                      size bounded by depth + capital, executed at 500ms latency,
                      net of per-cycle gas. THIS is the headline number.

A spike only "executes" in the walk-the-book model if, at the chosen execution
latency, the book is fresh AND walking it yields positive profit above gas.
exec_profit==0 => the order would not fill profitably => skipped (realistic).

Backward compatible: pre-v3 rows simply lack exec_* columns and are skipped by
the walk-the-book model (reported as coverage).
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


TAKER_FEE = 0.007


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _f(row: dict, key: str):
    v = row.get(key, "")
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def per_unit_profit(direction: str, sum_value: float) -> float:
    if direction == "SPLIT_AND_SELL":
        return sum_value * (1 - TAKER_FEE) - 1.0
    return 1.0 - sum_value * (1 + TAKER_FEE)


def cost_per_unit(direction: str, sum_value: float) -> float:
    if direction == "SPLIT_AND_SELL":
        return 1.0
    return sum_value * (1 + TAKER_FEE)


def span_hours(rows: list[dict]) -> float:
    try:
        t0 = datetime.fromisoformat(rows[0]["detected_at"].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(rows[-1]["detected_at"].replace("Z", "+00:00"))
        return (t1 - t0).total_seconds() / 3600
    except Exception:
        return 0.0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", type=str, default="/root/polymarket/cross_scanner/data/ws_arb_alerts.csv")
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--capital", type=float, default=100.0)
    p.add_argument("--max-stale-ms", type=float, default=500.0)
    p.add_argument("--min-edge-net", type=float, default=0.003)
    p.add_argument("--gas", type=float, default=0.05,
                   help="Per-cycle on-chain gas/cost subtracted in walk-the-book model")
    p.add_argument("--race-haircut", type=float, default=0.0,
                   help="Fraction [0..1] of walk-the-book profit assumed lost to "
                        "competing bots / partial fills (0=none, 0.5=half)")
    p.add_argument("--exec-latency-ms", type=int, default=2000, choices=[500, 2000],
                   help="Execution-latency basis for walk-the-book. On-chain "
                        "split+sells take ~2-3s, so 2000ms is the honest default; "
                        "500ms is an optimistic best-case that includes sub-second "
                        "flash spikes we cannot realistically capture.")
    args = p.parse_args()
    exec_col = f"exec_profit_{args.exec_latency_ms}ms"

    alerts = load(Path(args.csv))
    if not alerts:
        print(f"no alerts at {args.csv}")
        sys.exit(0)

    n = len(alerts)
    print(f"\n=== WS ARB ALERTS SUMMARY (v3) ===\n")
    print(f"Total alerts:  {n}")

    span_h = span_hours(alerts)
    if span_h > 0:
        t0 = alerts[0]["detected_at"]
        t1 = alerts[-1]["detected_at"]
        print(f"Time range:    {t0} -> {t1}")
        print(f"Span:          {span_h:.2f}h  |  rate: {n/max(span_h,0.01):.1f} alerts/h")

    # Durability
    durs = sorted(int(a["durability_ms"]) for a in alerts
                  if a.get("durability_ms", "").isdigit() and int(a["durability_ms"]) > 0)
    if durs:
        print(f"\n--- Durability (ms) ---")
        print(f"  count={len(durs)}  median={statistics.median(durs):.0f}  "
              f"p25={durs[len(durs)//4]:.0f}  p75={durs[3*len(durs)//4]:.0f}  max={max(durs)}")

    # Edge decay
    ed = []
    for a in alerts:
        g = _f(a, "gross_edge")
        if g is not None:
            ed.append((g, _f(a, "edge_500ms"), _f(a, "edge_2000ms"), _f(a, "edge_5000ms")))
    if ed:
        print(f"\n--- Edge decay ---")
        print(f"  initial:  median={statistics.median([d[0] for d in ed]):.4f}  max={max(d[0] for d in ed):.4f}")
        for name, idx in (("500ms", 1), ("2s", 2), ("5s", 3)):
            s = [d[idx] for d in ed if d[idx] is not None]
            if s:
                print(f"  {name:6s}:  median={statistics.median(s):.4f}  surviving={len(s)}/{n}")

    # Staleness
    stale_rows = [a for a in alerts if _f(a, "oldest_book_age_ms_at_detect") is not None]
    if stale_rows:
        stale = sorted(_f(a, "oldest_book_age_ms_at_detect") for a in stale_rows)
        n_fresh = sum(1 for s in stale if s <= args.max_stale_ms)
        print(f"\n--- Book staleness at detect (ms) ---")
        print(f"  median={statistics.median(stale):.0f}  p75={stale[3*len(stale)//4]:.0f}  "
              f"max={max(stale):.0f}  fresh(<={args.max_stale_ms:.0f}ms)={n_fresh}/{len(stale)} "
              f"({100*n_fresh/len(stale):.0f}%)")

    # Top events
    by_event = Counter(a["event_slug"] for a in alerts)
    print(f"\n--- Top events ---")
    for slug, c in by_event.most_common(args.top):
        ex = next(a for a in alerts if a["event_slug"] == slug)
        print(f"  {c:3d}x  {slug[:60]:60s}  vol=${(_f(ex,'vol_24h') or 0):,.0f}")

    # === 1. NAIVE ===
    print(f"\n--- 1. NAIVE PnL (cap=${args.capital}, fee={TAKER_FEE:.2%}, infinite depth) ---")
    tot = 0.0
    ne = 0
    for a in alerts:
        e = _f(a, "edge_500ms")
        if e is None or e <= 0:
            continue
        net = e - TAKER_FEE
        if net <= 0:
            continue
        ne += 1
        tot += args.capital * net
    print(f"  executed: {ne}/{n}   total=${tot:.2f}", end="")
    if span_h > 0:
        print(f"   per_day=${tot*24/span_h:.2f}")
    else:
        print()

    # === 2. DEPTH-BOUNDED (single best level) ===
    print(f"\n--- 2. DEPTH-BOUNDED PnL (single best level, max_stale={args.max_stale_ms:.0f}ms) ---")
    tot2 = 0.0
    ne2 = 0
    for a in alerts:
        size500 = _f(a, "bottleneck_size_500ms")
        stale = _f(a, "oldest_book_age_ms_at_detect")
        e = _f(a, "edge_500ms")
        if size500 is None or stale is None or e is None:
            continue
        if stale > args.max_stale_ms or e <= 0 or size500 <= 0:
            continue
        direction = a["direction"]
        sum500 = 1.0 + e if direction == "SPLIT_AND_SELL" else 1.0 - e
        npu = per_unit_profit(direction, sum500)
        if npu < args.min_edge_net:
            continue
        cpu = cost_per_unit(direction, sum500)
        units = min(args.capital / cpu, size500)
        tot2 += units * npu
        ne2 += 1
    print(f"  executed: {ne2}/{n}   total=${tot2:.2f}", end="")
    if span_h > 0:
        print(f"   per_day=${tot2*24/span_h:.2f}")
    else:
        print()

    # === 3. WALK-THE-BOOK (headline) ===
    print(f"\n--- 3. WALK-THE-BOOK PnL [HEADLINE] "
          f"(cap=${args.capital}, gas=${args.gas}/cycle, "
          f"max_stale={args.max_stale_ms:.0f}ms, exec_latency={args.exec_latency_ms}ms, "
          f"race_haircut={args.race_haircut:.0%}) ---")
    v3_rows = [a for a in alerts if _f(a, "exec_profit_detect") is not None]
    print(f"  v3 coverage: {len(v3_rows)}/{n}")

    tot3 = 0.0
    ne3 = 0
    sk_stale = sk_nofill = sk_gas = sk_nov3 = 0
    per_event_pnl = defaultdict(float)
    winners = []
    detect_upper = 0.0  # zero-latency upper bound
    for a in alerts:
        ep_detect = _f(a, "exec_profit_detect")
        if ep_detect is None:
            sk_nov3 += 1
            continue
        stale = _f(a, "oldest_book_age_ms_at_detect")
        if stale is None or stale > args.max_stale_ms:
            sk_stale += 1
            continue
        if ep_detect > 0:
            detect_upper += ep_detect
        # realistic capture = profit walking the book at the chosen execution
        # latency. Spike that died before that checkpoint => column empty => no fill.
        ep = _f(a, exec_col)
        if ep is None:
            sk_nofill += 1
            continue
        ep *= (1.0 - args.race_haircut)
        net = ep - args.gas
        if ep <= 0:
            sk_nofill += 1
            continue
        if net <= 0:
            sk_gas += 1
            continue
        tot3 += net
        ne3 += 1
        per_event_pnl[a["event_slug"]] += net
        winners.append((net, a["event_slug"], _f(a, "exec_units_detect") or 0))

    print(f"  executed:              {ne3}/{n}")
    print(f"  skipped no v3 fields:  {sk_nov3}")
    print(f"  skipped stale book:    {sk_stale}")
    print(f"  skipped no-fill (<=0): {sk_nofill}")
    print(f"  skipped gas>profit:    {sk_gas}")
    print(f"  ---")
    print(f"  zero-latency upper bound (detect): ${detect_upper:.2f}")
    print(f"  REALISTIC total PnL:               ${tot3:.2f}")
    if ne3:
        print(f"  avg/executed spike:                ${tot3/ne3:.3f}")
    # Per-day over the span covered by v3 rows
    if v3_rows:
        sv3 = span_hours(v3_rows)
        if sv3 > 0.01:
            print(f"  REALISTIC per day:                 ${tot3*24/sv3:.2f}  (over {sv3:.1f}h v3 data)")

    if per_event_pnl:
        print(f"\n  --- realistic PnL by event ---")
        for slug, pnl in sorted(per_event_pnl.items(), key=lambda kv: -kv[1])[:10]:
            print(f"    ${pnl:7.2f}  {slug[:55]}")

    if winners:
        winners.sort(reverse=True)
        print(f"\n  --- top 5 single spikes ---")
        for net, slug, units in winners[:5]:
            print(f"    ${net:6.3f}  units={units:7.1f}  {slug[:50]}")

    # === 4. SHADOW-FILL RACE TEST (no capital) — the decisive question ===
    fo_rows = [a for a in alerts if a.get("fill_outcome", "")]
    print(f"\n--- 4. SHADOW-FILL RACE TEST [latency-gated, zero capital] ---")
    print(f"  classified alerts: {len(fo_rows)}/{n}")
    if fo_rows:
        oc = Counter(a["fill_outcome"] for a in fo_rows)
        tot = len(fo_rows)
        won = oc.get("WON", 0)
        lost = oc.get("LOST_RACE", 0)
        decayed = oc.get("DECAYED", 0)
        print(f"  WON       (fill at our latency):  {won:4d}  ({100*won/tot:.0f}%)")
        print(f"  LOST_RACE (competitor traded 1st):{lost:4d}  ({100*lost/tot:.0f}%)")
        print(f"  DECAYED   (quote pulled, no trade):{decayed:4d}  ({100*decayed/tot:.0f}%)")
        # trade-print intensity
        tw = [int(a["trades_within_latency"]) for a in fo_rows
              if a.get("trades_within_latency", "").lstrip("-").isdigit()]
        if tw:
            with_trade = sum(1 for x in tw if x > 0)
            print(f"  spikes with >=1 competing trade within latency: "
                  f"{with_trade}/{len(tw)} ({100*with_trade/len(tw):.0f}%)")
        # Verdict
        print(f"\n  --- SPEED VERDICT ---")
        beatable = won + decayed  # WON now + DECAYED (a faster bot beats the pull)
        if tot > 0:
            print(f"  Beatable-by-speed (WON+DECAYED): {beatable}/{tot} "
                  f"({100*beatable/tot:.0f}%)")
            print(f"  Hard-blocked (LOST_RACE):        {lost}/{tot} "
                  f"({100*lost/tot:.0f}%)")
            if lost > beatable:
                print(f"  => Mostly LOST_RACE: co-located bots beat us. A faster bot "
                      f"from Stockholm likely WON'T help materially.")
            elif decayed > won:
                print(f"  => Mostly DECAYED: spikes evaporate (quote pulls), few "
                      f"competing trades. A faster executor SHOULD capture more — "
                      f"the bottleneck is our reaction speed, not competition.")
            else:
                print(f"  => Mixed/WON-heavy: opportunities are reachable at our "
                      f"latency; building a real executor is justified.")

        # Fresh-only breakdown: exclude spikes detected on a stale book (a token
        # that hadn't updated), which can produce phantom "WON" verdicts. This is
        # the honest race view — real, actionable opportunities only.
        fresh = [a for a in fo_rows
                 if (_f(a, "oldest_book_age_ms_at_detect") or 1e9) <= args.max_stale_ms]
        print(f"\n  --- FRESH-ONLY (book <= {args.max_stale_ms:.0f}ms at detect, "
              f"excludes stale phantoms): {len(fresh)}/{len(fo_rows)} ---")
        if fresh:
            foc = Counter(a["fill_outcome"] for a in fresh)
            ft = len(fresh)
            fw, fl, fd = foc.get("WON", 0), foc.get("LOST_RACE", 0), foc.get("DECAYED", 0)
            print(f"  WON       {fw:4d}  ({100*fw/ft:.0f}%)")
            print(f"  LOST_RACE {fl:4d}  ({100*fl/ft:.0f}%)")
            print(f"  DECAYED   {fd:4d}  ({100*fd/ft:.0f}%)")
            if fl > (fw + fd):
                print(f"  => FRESH verdict: co-located bots win the race. Speed won't help.")
            elif fl >= fw:
                print(f"  => FRESH verdict: CONTESTED — LOST_RACE ({100*fl/ft:.0f}%) >= WON "
                      f"({100*fw/ft:.0f}%). Faster bots take a large share; a fast executor "
                      f"helps on the rest but co-location is a real wall. Marginal.")
            elif fd >= fw:
                print(f"  => FRESH verdict: spikes evaporate (pulls) — a fast executor "
                      f"beats the pull. Bottleneck is OUR reaction speed.")
            else:
                print(f"  => FRESH verdict: reachable at our latency — fast executor justified.")


if __name__ == "__main__":
    main()
