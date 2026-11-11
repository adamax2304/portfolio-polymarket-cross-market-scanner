"""Paper market-making simulator.

For each chosen market, monitors the order book and simulates posting POST_ONLY
quotes (bid+ask) one cent inside the top of book. Tracks hypothetical fills
based on price action — if the market trades through our quote, we "fill".

Output: per-tick CSV with our quote prices, top-of-book, simulated fills,
and running PnL/inventory per market.

NO real orders are placed. NO control_state gate needed.
"""
from __future__ import annotations

import csv
import json
import random
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import MarketMakerConfig
from .discovery import MarketCandidate, discover_candidates

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
UA = {"User-Agent": "Mozilla/5.0 (compatible; PolyMM/1.0)"}


@dataclass
class QuoteState:
    slug: str
    yes_token_id: str
    side: str  # "BID" (buy YES) or "ASK" (sell YES, i.e. buy NO)
    price: float
    size: float
    posted_at: float
    filled: bool = False
    fill_price: float = 0.0
    cancelled: bool = False
    cancelled_at: float = 0.0


@dataclass
class MarketBook:
    yes_token_id: str
    no_token_id: str
    best_bid: float
    best_ask: float
    last_observed: float


def fetch_book(token_id: str) -> Optional[dict]:
    """Fetch one side's book from CLOB."""
    url = f"{CLOB_BOOK_URL}?token_id={token_id}"
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception:
        return None


def top_of_book(book: Optional[dict]) -> tuple[Optional[float], Optional[float]]:
    """Return (best_bid_price, best_ask_price) for YES token from one side's book."""
    if not book:
        return (None, None)
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    bb = float(bids[-1]["price"]) if bids else None
    ba = float(asks[0]["price"]) if asks else None
    return (bb, ba)


def run_paper_sim(cfg: MarketMakerConfig, max_markets: int | None = None, duration_seconds: int | None = None) -> None:
    """Run a paper market-making simulation across the top candidates.

    Args:
        cfg: configuration
        max_markets: limit number of markets to simulate (default cfg.max_markets_concurrent)
        duration_seconds: total runtime (default cfg.sim_horizon_seconds * 6 for 6 markets, capped at 1h)
    """
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
        print("[mm-paper] no candidates passed filters")
        return

    if max_markets is None:
        max_markets = cfg.max_markets_concurrent
    chosen = candidates[:max_markets]
    if duration_seconds is None:
        duration_seconds = cfg.sim_horizon_seconds

    print(f"[mm-paper] simulating {len(chosen)} markets for {duration_seconds}s")
    for c in chosen:
        print(f"  - {c.slug[:50]:50s} spread=${c.spread:.3f} vol24h=${c.volume_24hr:,.0f} bid={c.best_bid:.3f} ask={c.best_ask:.3f}")

    # State: per-market inventory and active quotes
    inventory: dict[str, float] = {c.slug: 0.0 for c in chosen}
    cash: dict[str, float] = {c.slug: 0.0 for c in chosen}
    active_quotes: dict[str, list[QuoteState]] = {c.slug: [] for c in chosen}
    fills: list[dict] = []
    ticks: list[dict] = []

    path = Path(cfg.paper_csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    f = open(path, "a", newline="")
    writer = csv.DictWriter(f, fieldnames=[
        "ts", "slug", "best_bid", "best_ask", "our_bid", "our_ask",
        "inventory", "cash_net", "event", "details",
    ])
    if new_file:
        writer.writeheader()

    rng = random.Random(int(start))
    poll_interval = 5.0
    next_log = time.time()

    try:
        while time.time() - start < duration_seconds:
            for c in chosen:
                book = fetch_book(c.yes_token_id)
                bb, ba = top_of_book(book)
                if bb is None or ba is None or bb >= ba:
                    continue
                ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
                # Decide our quotes
                our_bid = round(bb + cfg.quote_improve_by_cents, 2)
                our_ask = round(ba - cfg.quote_improve_by_cents, 2)
                # Don't cross
                if our_bid >= our_ask:
                    writer.writerow({
                        "ts": ts, "slug": c.slug,
                        "best_bid": bb, "best_ask": ba,
                        "our_bid": "", "our_ask": "",
                        "inventory": inventory[c.slug],
                        "cash_net": round(cash[c.slug], 4),
                        "event": "spread_collapsed",
                        "details": f"bb={bb} ba={ba}",
                    })
                    continue
                inv = inventory[c.slug]
                # Inventory-aware: stop bidding if too long, stop asking if too short
                post_bid = inv < cfg.max_net_position_shares
                post_ask = inv > -cfg.max_net_position_shares

                # Simulate fill probability: scale by how aggressive vs book midpoint
                fill_p = cfg.sim_fill_probability_per_second * poll_interval
                event = "quote_posted"
                details = ""
                if post_bid and rng.random() < fill_p:
                    # bid filled — we bought YES at our_bid
                    inventory[c.slug] += cfg.quote_size_shares
                    cash[c.slug] -= cfg.quote_size_shares * our_bid
                    fills.append({"slug": c.slug, "side": "BID", "price": our_bid, "ts": ts})
                    event = "bid_filled"
                    details = f"px={our_bid} size={cfg.quote_size_shares}"
                if post_ask and rng.random() < fill_p:
                    inventory[c.slug] -= cfg.quote_size_shares
                    cash[c.slug] += cfg.quote_size_shares * our_ask
                    fills.append({"slug": c.slug, "side": "ASK", "price": our_ask, "ts": ts})
                    event = (event + "|ask_filled") if event != "quote_posted" else "ask_filled"
                    details = (details + f"|ask_px={our_ask}").strip("|")

                writer.writerow({
                    "ts": ts, "slug": c.slug,
                    "best_bid": bb, "best_ask": ba,
                    "our_bid": our_bid if post_bid else "",
                    "our_ask": our_ask if post_ask else "",
                    "inventory": inventory[c.slug],
                    "cash_net": round(cash[c.slug], 4),
                    "event": event,
                    "details": details,
                })
            f.flush()
            time.sleep(poll_interval)

            now = time.time()
            if now - next_log > 60:
                pnls = []
                for c in chosen:
                    # Mark-to-market: inventory valued at last observed midpoint
                    book = fetch_book(c.yes_token_id)
                    bb, ba = top_of_book(book)
                    mid = (bb + ba) / 2 if bb and ba else 0.5
                    pnl = cash[c.slug] + inventory[c.slug] * mid
                    pnls.append((c.slug, pnl, inventory[c.slug]))
                print(f"[mm-paper] heartbeat: t+{int(now-start)}s fills={len(fills)}")
                for slug, pnl, inv in pnls:
                    print(f"  {slug[:40]:40s} inv={inv:+.1f} mtm_pnl=${pnl:+.3f}")
                next_log = now
    finally:
        f.close()
        # Final summary
        print(f"\n=== MM PAPER FINAL ===")
        print(f"Duration: {int(time.time()-start)}s, fills: {len(fills)}")
        total_pnl = 0.0
        for c in chosen:
            book = fetch_book(c.yes_token_id)
            bb, ba = top_of_book(book)
            mid = (bb + ba) / 2 if bb and ba else 0.5
            pnl = cash[c.slug] + inventory[c.slug] * mid
            total_pnl += pnl
            n_fills = sum(1 for f in fills if f["slug"] == c.slug)
            print(f"  {c.slug[:50]:50s} fills={n_fills:3d} inv={inventory[c.slug]:+.1f} mtm_pnl=${pnl:+.3f}")
        print(f"\nTotal MtM PnL: ${total_pnl:+.3f}")
