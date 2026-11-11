#!/usr/bin/env python3
"""Measure our true round-trip latency to the Polymarket CLOB — no orders, no money.

We can't time the POST /order path without placing an order, but the dominant,
competitor-relative variable is network RTT to the CLOB host (TLS + request +
first byte). We time read-only GET /price round-trips, which traverse the exact
same host/TLS path an order submission would. The operator-side matching adds a
fixed component on top, identical for everyone, so the RTT we measure is the part
that actually determines whether we win or lose the race vs a co-located bot.

Reports p50/p90/p99 over N samples. Use the p90 as the shadow-fill latency gate.
"""
from __future__ import annotations

import argparse
import statistics
import time
import urllib.request


CLOB = "https://clob.polymarket.com"
UA = {"User-Agent": "Mozilla/5.0 (LatencyProbe/1.0)"}


def one_sample(token_id: str, timeout: float) -> float | None:
    # /price is a tiny read-only endpoint on the order-matching host.
    url = f"{CLOB}/price?token_id={token_id}&side=buy"
    req = urllib.request.Request(url, headers=UA)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
    except Exception:
        return None
    return (time.perf_counter() - t0) * 1000.0  # ms


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--token", type=str,
                   help="A token_id to ping. If omitted, discovers one.")
    p.add_argument("--n", type=int, default=60)
    p.add_argument("--timeout", type=float, default=5.0)
    args = p.parse_args()

    token = args.token
    if not token:
        from polymarket_cross_scanner.ws_arb.discovery import discover_negrisk_events
        evs = discover_negrisk_events(min_volume_24h=500, max_events=5)
        token = next(t for e in evs for t in e.yes_token_ids)
        print(f"using discovered token {token[:20]}...")

    # warm up TLS/connection
    one_sample(token, args.timeout)

    samples = []
    for i in range(args.n):
        ms = one_sample(token, args.timeout)
        if ms is not None:
            samples.append(ms)
        time.sleep(0.1)

    if not samples:
        print("no successful samples")
        return
    samples.sort()
    n = len(samples)
    p50 = statistics.median(samples)
    p90 = samples[min(n - 1, int(n * 0.90))]
    p99 = samples[min(n - 1, int(n * 0.99))]
    print(f"\n=== CLOB REST RTT from this host (n={n}/{args.n}) ===")
    print(f"  min={min(samples):.0f}ms  p50={p50:.0f}ms  p90={p90:.0f}ms  "
          f"p99={p99:.0f}ms  max={max(samples):.0f}ms")
    print(f"\n  -> suggested shadow-fill latency gate (p90): {p90:.0f}ms")
    print(f"  (order submit adds a fixed operator-side component on top, equal for all)")


if __name__ == "__main__":
    main()
