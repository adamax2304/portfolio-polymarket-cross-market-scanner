"""V3 paper market-making simulator — uses real /trades endpoint.

Method:
 - Poll /trades for each selected market every poll_interval seconds
 - For each new trade since last poll:
   * If trade.side == SELL and our_bid >= trade.price: we filled the bid (taker sold into us)
   * If trade.side == BUY  and our_ask <= trade.price: we filled the ask (taker bought from us)
 - Conservative: we assume queue priority = first to fill if our quote matches/beats trade price
 - Cap fills per trade by remaining trade size (1 trade can fill multiple makers; we take min(trade.size, our_size))
"""
from __future__ import annotations

import csv
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import MarketMakerConfig
from .discovery import discover_candidates

CLOB_BOOK_URL = "https://clob.polymarket.com/book"
TRADES_URL = "https://data-api.polymarket.com/trades"
UA = {"User-Agent": "Mozilla/5.0 (compatible; PolyMM/3.0)"}

TAKER_FEE_BPS = 70
MAKER_REBATE_FRACTION = 0.20


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


def fetch_trades(condition_id: str, limit: int = 100) -> list[dict]:
    url = f"{TRADES_URL}?market={condition_id}&limit={limit}"
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def run_paper_sim_v3(cfg: MarketMakerConfig, max_markets: int | None = None, duration_seconds: int | None = None) -> None:
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
        print("[mm-v3] no candidates")
        return
    max_markets = max_markets or cfg.max_markets_concurrent
    chosen = candidates[:max_markets]
    duration_seconds = duration_seconds or cfg.sim_horizon_seconds

    print(f"[mm-v3] {len(chosen)} markets × {duration_seconds}s, using /trades endpoint")
    for c in chosen:
        print(f"  {c.slug[:50]:50s} cond={c.condition_id[:10]}... spread=${c.spread:.3f}")

    inventory = {c.slug: 0.0 for c in chosen}
    cash = {c.slug: 0.0 for c in chosen}
    fills_by_slug = {c.slug: 0 for c in chosen}
    last_seen_ts = {c.slug: int(time.time())  for c in chosen}  # only trades after this
    our_bid_active = {c.slug: None for c in chosen}
    our_ask_active = {c.slug: None for c in chosen}
    last_book = {}

    path = Path(cfg.paper_csv_path).with_name("market_maker_paper_v3.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    f = open(path, "a", newline="")
    writer = csv.DictWriter(f, fieldnames=[
        "ts", "slug", "best_bid", "best_ask", "our_bid", "our_ask",
        "inventory", "cash_net", "trade_count", "event", "details",
    ])
    if new_file:
        writer.writeheader()

    poll_interval = 10.0
    next_log = time.time()

    try:
        while time.time() - start < duration_seconds:
            for c in chosen:
                book = fetch_book(c.yes_token_id)
                bb, ba = top_of_book(book)
                if bb is None or ba is None or bb >= ba:
                    continue
                ts_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
                inv = inventory[c.slug]
                post_bid = inv < cfg.max_net_position_shares
                post_ask = inv > -cfg.max_net_position_shares
                our_bid = round(bb + cfg.quote_improve_by_cents, 2) if post_bid else None
                our_ask = round(ba - cfg.quote_improve_by_cents, 2) if post_ask else None
                if our_bid and our_ask and our_bid >= our_ask:
                    continue

                # Fetch recent trades for this market
                trades = fetch_trades(c.condition_id, limit=50)
                new_trades = [t for t in trades if t.get("timestamp", 0) > last_seen_ts[c.slug]]
                # Sort old→new
                new_trades.sort(key=lambda t: t.get("timestamp", 0))

                events = []
                detail_bits = []
                trade_count_this_round = 0
                for t in new_trades:
                    try:
                        tp = float(t.get("price", 0))
                        tsz = float(t.get("size", 0))
                    except (TypeError, ValueError):
                        continue
                    tside = t.get("side", "")
                    trade_count_this_round += 1
                    # Our_bid: we'd fill if taker SELL crossed our bid (sold at price <= our_bid)
                    if tside == "SELL" and our_bid_active.get(c.slug) and tp <= our_bid_active[c.slug]:
                        # Fill our bid, capped by remaining quote size and trade size
                        sz = min(cfg.quote_size_shares, tsz)
                        if sz > 0 and inv < cfg.max_net_position_shares:
                            fill_px = our_bid_active[c.slug]
                            inventory[c.slug] += sz
                            cash[c.slug] -= sz * fill_px
                            rebate = sz * fill_px * (TAKER_FEE_BPS / 10000) * MAKER_REBATE_FRACTION
                            cash[c.slug] += rebate
                            fills_by_slug[c.slug] += 1
                            events.append("bid_filled_real")
                            detail_bits.append(f"bid_px={fill_px} sz={sz} trade_px={tp}")
                            inv = inventory[c.slug]
                    if tside == "BUY" and our_ask_active.get(c.slug) and tp >= our_ask_active[c.slug]:
                        sz = min(cfg.quote_size_shares, tsz)
                        if sz > 0 and inv > -cfg.max_net_position_shares:
                            fill_px = our_ask_active[c.slug]
                            inventory[c.slug] -= sz
                            cash[c.slug] += sz * fill_px
                            rebate = sz * fill_px * (TAKER_FEE_BPS / 10000) * MAKER_REBATE_FRACTION
                            cash[c.slug] += rebate
                            fills_by_slug[c.slug] += 1
                            events.append("ask_filled_real")
                            detail_bits.append(f"ask_px={fill_px} sz={sz} trade_px={tp}")
                            inv = inventory[c.slug]
                    last_seen_ts[c.slug] = max(last_seen_ts[c.slug], int(t.get("timestamp", 0)))

                # Update our active quotes
                our_bid_active[c.slug] = our_bid
                our_ask_active[c.slug] = our_ask
                last_book[c.slug] = (bb, ba)

                event = "|".join(events) if events else "quoted"
                writer.writerow({
                    "ts": ts_iso, "slug": c.slug,
                    "best_bid": bb, "best_ask": ba,
                    "our_bid": our_bid or "",
                    "our_ask": our_ask or "",
                    "inventory": round(inv, 2),
                    "cash_net": round(cash[c.slug], 4),
                    "trade_count": trade_count_this_round,
                    "event": event,
                    "details": "|".join(detail_bits),
                })
            f.flush()

            if time.time() - next_log > 60:
                tot_fills = sum(fills_by_slug.values())
                tot_mtm = 0.0
                for c in chosen:
                    bb_ba = last_book.get(c.slug)
                    mid = (bb_ba[0] + bb_ba[1]) / 2 if bb_ba else 0.5
                    tot_mtm += cash[c.slug] + inventory[c.slug] * mid
                print(f"[mm-v3] t+{int(time.time()-start)}s fills={tot_fills} mtm=${tot_mtm:+.3f}")
                next_log = time.time()
            time.sleep(poll_interval)
    finally:
        f.close()
        print(f"\n=== MM-V3 FINAL ===")
        for c in chosen:
            bb_ba = last_book.get(c.slug)
            mid = (bb_ba[0] + bb_ba[1]) / 2 if bb_ba else 0.5
            mtm = cash[c.slug] + inventory[c.slug] * mid
            print(f"  {c.slug[:50]:50s} fills={fills_by_slug[c.slug]:3d} inv={inventory[c.slug]:+.1f} mtm=${mtm:+.3f}")
