"""Cross-venue arbitrage scanner orchestration.

Reads markets from Polymarket / Manifold / Kalshi / PredictIt, matches by content,
ranks by spread, and writes a CSV + console report.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .matcher import CrossMatch, find_matches
from .sources import (
    fetch_kalshi,
    fetch_manifold,
    fetch_polymarket,
    fetch_predictit,
)


def scan_cross_venues(
    *,
    min_spread: float = 0.03,
    min_similarity: float = 0.4,
    min_volume_24h: float = 100.0,
    output_csv: Optional[str] = None,
    venues: Optional[list[str]] = None,
) -> list[CrossMatch]:
    if venues is None:
        venues = ["polymarket", "manifold", "kalshi", "predictit"]
    print(f"\n[scan] venues={venues} min_spread={min_spread} min_similarity={min_similarity}")

    markets_by_venue: dict[str, list] = {}
    fetchers = {
        "polymarket": fetch_polymarket,
        "manifold": fetch_manifold,
        "kalshi": fetch_kalshi,
        "predictit": fetch_predictit,
    }
    for v in venues:
        if v not in fetchers:
            continue
        print(f"  fetching {v}...")
        mkts = fetchers[v]()
        # filter by min volume (PredictIt = 0 vol exposed, skip filter)
        if v != "predictit":
            mkts = [m for m in mkts if m.volume_24h_usd >= min_volume_24h]
        markets_by_venue[v] = mkts
        print(f"    got {len(mkts)} markets")

    if sum(len(m) for m in markets_by_venue.values()) == 0:
        print("[scan] no markets fetched")
        return []

    matches = find_matches(markets_by_venue, min_similarity=min_similarity, require_shared_anchor=True)
    # Filter by spread
    matches = [m for m in matches if m.abs_spread >= min_spread]
    print(f"\n[scan] {len(matches)} matches above spread {min_spread:.2%}")

    if matches:
        print("\nTop 20 by absolute spread:")
        print("=" * 100)
        for i, m in enumerate(matches[:20], 1):
            print(f"\n#{i} spread={m.abs_spread:.3f} similarity={m.similarity:.2f} anchors={sorted(m.shared_anchors)}")
            print(f"  [{m.venue_a:10s}] {m.market_a.question[:90]}")
            print(f"            YES={m.market_a.yes_price:.3f} vol24h=${m.market_a.volume_24h_usd:,.0f}")
            print(f"            url: {m.market_a.url}")
            print(f"  [{m.venue_b:10s}] {m.market_b.question[:90]}")
            print(f"            YES={m.market_b.yes_price:.3f} vol24h=${m.market_b.volume_24h_usd:,.0f}")
            print(f"            url: {m.market_b.url}")
            # Arb direction
            if m.yes_spread > 0:
                cheap_venue, cheap_p = m.venue_b, m.market_b.yes_price
                expensive_venue, expensive_p = m.venue_a, m.market_a.yes_price
            else:
                cheap_venue, cheap_p = m.venue_a, m.market_a.yes_price
                expensive_venue, expensive_p = m.venue_b, m.market_b.yes_price
            print(f"  => Buy YES on {cheap_venue} @ ${cheap_p:.3f}, Buy NO (= sell YES) on {expensive_venue} @ ${1-expensive_p:.3f}")
            print(f"    Combined cost per pair: ${cheap_p + (1 - expensive_p):.3f}, gross edge: ${1 - (cheap_p + (1 - expensive_p)):.3f}")

    if output_csv:
        path = Path(output_csv)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "ts", "rank", "venue_a", "question_a", "yes_price_a", "vol24h_a", "url_a",
                "venue_b", "question_b", "yes_price_b", "vol24h_b", "url_b",
                "similarity", "shared_anchors", "abs_spread", "yes_spread",
                "buy_yes_venue", "buy_yes_price", "sell_yes_venue", "sell_yes_price",
                "gross_edge",
            ])
            for i, m in enumerate(matches, 1):
                if m.yes_spread > 0:
                    buy_v, buy_p, sell_v, sell_p = m.venue_b, m.market_b.yes_price, m.venue_a, m.market_a.yes_price
                else:
                    buy_v, buy_p, sell_v, sell_p = m.venue_a, m.market_a.yes_price, m.venue_b, m.market_b.yes_price
                gross = 1 - (buy_p + (1 - sell_p))
                w.writerow([
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    i,
                    m.venue_a, m.market_a.question, m.market_a.yes_price, m.market_a.volume_24h_usd, m.market_a.url,
                    m.venue_b, m.market_b.question, m.market_b.yes_price, m.market_b.volume_24h_usd, m.market_b.url,
                    m.similarity, "|".join(sorted(m.shared_anchors)), m.abs_spread, m.yes_spread,
                    buy_v, buy_p, sell_v, sell_p, gross,
                ])
        print(f"\n[scan] csv → {path}")

    return matches
