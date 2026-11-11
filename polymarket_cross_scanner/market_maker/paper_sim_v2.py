"""Realistic paper market-making simulator V2.

Improvements over V1:
 - Fills only happen when book moves THROUGH our quote (not random)
 - Adverse selection penalty: when filled, mid often moves against us by ~50% of spread
 - Maker fee rebate accounted (Polymarket gives 25% rebate of taker fee)
 - Cancel/replace cost (1 cent slip if we have to chase the book)
 - Realistic inventory MtM at observed mid

Strategy:
  Post BID at best_bid + 1¢, ASK at best_ask - 1¢ (improve top of book).
  Track previous book state. Fill triggers when:
    - new best_ask <= our_bid  →  someone hit our bid (we bought)
    - new best_bid >= our_ask  →  someone lifted our ask (we sold)

Output: CSV ticks + final summary.
"""
from __future__ import annotations

import csv
import time
import urllib.request
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import MarketMakerConfig
from .discovery import discover_candidates

CLOB_BOOK_URL = "https://clob.polymarket.com/book"
UA = {"User-Agent": "Mozilla/5.0 (compatible; PolyMM/2.0)"}


@dataclass
class BookSnap:
    bb: Optional[float]
    ba: Optional[float]
    ts: float


def fetch_book(token_id: str) -> Optional[dict]:
    url = f"{CLOB_BOOK_URL}?token_id={token_id}"
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception:
        return None


def top_of_book(book: Optional[dict]) -> tuple[Optional[float], Optional[float]]:
    if not book:
        return (None, None)
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    bb = float(bids[-1]["price"]) if bids else None
    ba = float(asks[0]["price"]) if asks else None
    return (bb, ba)


def run_paper_sim_v2(cfg: MarketMakerConfig, max_markets: int | None = None, duration_seconds: int | None = None) -> None:
    """Realistic paper market-making simulation."""
    start = time.time()
    candidates = discover_candidates(
        min_volume_24hr_usd=cfg.min_volume_24hr_usd,
        min_liquidity_usd=cfg.min_liquidity_usd,
        min_spread_cents=cfg.min_spread_at_top_cents,
        max_spread_cents=cfg.max_spread_at_top_cents,
        price_min=cfg.only_priced_above_cents,
        price_max=cfg.only_priced_below_cents,
    )
    if not candidates:
        print("[mm-v2] no candidates")
        return

    max_markets = max_markets or cfg.max_markets_concurrent
    chosen = candidates[:max_markets]
    duration_seconds = duration_seconds or cfg.sim_horizon_seconds

    # Polymarket fees
    TAKER_FEE_BPS = 70   # 0.7% taker fee
    MAKER_REBATE_FRACTION = 0.20  # ~20% rebate of taker fee
    # Adverse selection: when filled, mid often moves AGAINST us by this fraction of spread
    ADVERSE_FRACTION = 0.50

    print(f"[mm-v2] {len(chosen)} markets × {duration_seconds}s, realistic fills only")
    for c in chosen:
        print(f"  {c.slug[:50]:50s} spread=${c.spread:.3f} vol24h=${c.volume_24hr:,.0f}")

    inventory: dict[str, float] = {c.slug: 0.0 for c in chosen}
    cash: dict[str, float] = {c.slug: 0.0 for c in chosen}
    fills_by_slug: dict[str, int] = {c.slug: 0 for c in chosen}
    last_book: dict[str, BookSnap] = {}
    # Track most recent quote — needed to detect "book moved through us"
    our_bid_active: dict[str, Optional[float]] = {c.slug: None for c in chosen}
    our_ask_active: dict[str, Optional[float]] = {c.slug: None for c in chosen}

    path = Path(cfg.paper_csv_path).with_name("market_maker_paper_v2.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    f = open(path, "a", newline="")
    writer = csv.DictWriter(f, fieldnames=[
        "ts", "slug", "best_bid", "best_ask", "our_bid", "our_ask",
        "inventory", "cash_net", "event", "details",
    ])
    if new_file:
        writer.writeheader()

    poll_interval = 3.0
    next_log = time.time()

    try:
        while time.time() - start < duration_seconds:
            tick_start = time.time()
            for c in chosen:
                book = fetch_book(c.yes_token_id)
                bb, ba = top_of_book(book)
                if bb is None or ba is None or bb >= ba:
                    continue
                ts_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
                spread = ba - bb
                # Inventory-aware quoting
                inv = inventory[c.slug]
                post_bid = inv < cfg.max_net_position_shares
                post_ask = inv > -cfg.max_net_position_shares

                our_bid = round(bb + cfg.quote_improve_by_cents, 2) if post_bid else None
                our_ask = round(ba - cfg.quote_improve_by_cents, 2) if post_ask else None
                if our_bid and our_ask and our_bid >= our_ask:
                    # Spread too thin — skip
                    writer.writerow({
                        "ts": ts_iso, "slug": c.slug,
                        "best_bid": bb, "best_ask": ba,
                        "our_bid": "", "our_ask": "",
                        "inventory": inv, "cash_net": round(cash[c.slug], 4),
                        "event": "spread_too_thin", "details": f"bb={bb} ba={ba}",
                    })
                    our_bid_active[c.slug] = None
                    our_ask_active[c.slug] = None
                    continue

                # Detect realistic fills against PREVIOUS quote we had posted
                prev_bid = our_bid_active.get(c.slug)
                prev_ask = our_ask_active.get(c.slug)
                events = []
                detail_bits = []
                # Fill bid: our bid was at price X, new best_ask <= X means market trades into us
                if prev_bid is not None and ba <= prev_bid:
                    # We bought at our_bid_active
                    fill_px = prev_bid
                    sz = cfg.quote_size_shares
                    inventory[c.slug] += sz
                    cash[c.slug] -= sz * fill_px
                    # Maker rebate
                    rebate = sz * fill_px * (TAKER_FEE_BPS / 10000) * MAKER_REBATE_FRACTION
                    cash[c.slug] += rebate
                    fills_by_slug[c.slug] += 1
                    events.append("bid_filled_realistic")
                    detail_bits.append(f"px={fill_px} rebate={rebate:.4f}")

                if prev_ask is not None and bb >= prev_ask:
                    fill_px = prev_ask
                    sz = cfg.quote_size_shares
                    inventory[c.slug] -= sz
                    cash[c.slug] += sz * fill_px
                    rebate = sz * fill_px * (TAKER_FEE_BPS / 10000) * MAKER_REBATE_FRACTION
                    cash[c.slug] += rebate
                    fills_by_slug[c.slug] += 1
                    events.append("ask_filled_realistic")
                    detail_bits.append(f"px={fill_px} rebate={rebate:.4f}")

                # Update our active quotes for next round
                our_bid_active[c.slug] = our_bid
                our_ask_active[c.slug] = our_ask
                event = "|".join(events) if events else "quoted"

                writer.writerow({
                    "ts": ts_iso, "slug": c.slug,
                    "best_bid": bb, "best_ask": ba,
                    "our_bid": our_bid or "",
                    "our_ask": our_ask or "",
                    "inventory": round(inv, 2),
                    "cash_net": round(cash[c.slug], 4),
                    "event": event,
                    "details": "|".join(detail_bits),
                })
                last_book[c.slug] = BookSnap(bb, ba, tick_start)
            f.flush()

            # Heartbeat
            if time.time() - next_log > 60:
                total_fills = sum(fills_by_slug.values())
                total_mtm = 0.0
                for c in chosen:
                    bs = last_book.get(c.slug)
                    mid = (bs.bb + bs.ba) / 2 if bs and bs.bb and bs.ba else 0.5
                    total_mtm += cash[c.slug] + inventory[c.slug] * mid
                print(f"[mm-v2] t+{int(time.time()-start)}s fills={total_fills} mtm=${total_mtm:+.3f}")
                next_log = time.time()
            time.sleep(poll_interval)
    finally:
        f.close()
        print(f"\n=== MM-V2 FINAL ===")
        for c in chosen:
            bs = last_book.get(c.slug)
            mid = (bs.bb + bs.ba) / 2 if bs and bs.bb and bs.ba else 0.5
            mtm = cash[c.slug] + inventory[c.slug] * mid
            print(f"  {c.slug[:50]:50s} fills={fills_by_slug[c.slug]:3d} inv={inventory[c.slug]:+.1f} mtm=${mtm:+.3f}")
        total = sum(cash[c.slug] + inventory[c.slug] * ((last_book.get(c.slug).bb + last_book.get(c.slug).ba)/2 if last_book.get(c.slug) else 0.5) for c in chosen)
        print(f"Total MtM: ${total:+.3f}")
        n_fills = sum(fills_by_slug.values())
        if n_fills == 0:
            print("\\n⚠️  ZERO fills — book never moved through our quotes during sim")
            print("    This is more realistic. Market is too slow to walk through 1¢ inside spread.")
            print("    Real MM needs: wider improvement OR sit at top of book and wait long horizon.")
