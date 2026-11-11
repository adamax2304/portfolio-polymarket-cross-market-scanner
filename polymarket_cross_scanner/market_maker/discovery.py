"""Discover candidate markets for market making.

Pulls active markets from gamma-api, filters by liquidity/volume/spread, and
returns top N candidates ordered by an attractiveness score.
"""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from typing import Optional

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
UA_HEADER = {"User-Agent": "Mozilla/5.0 (compatible; PolyMM/1.0)"}


@dataclass
class MarketCandidate:
    slug: str
    question: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    best_bid: float
    best_ask: float
    spread: float
    volume_24hr: float
    liquidity: float
    end_date: str
    score: float = 0.0


def _gamma_get(params: str) -> list:
    url = f"{GAMMA_URL}?{params}"
    req = urllib.request.Request(url, headers=UA_HEADER)
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    return data if isinstance(data, list) else []


def discover_candidates(
    *,
    min_volume_24hr_usd: float,
    min_liquidity_usd: float,
    min_spread_cents: float,
    max_spread_cents: float,
    price_min: float,
    price_max: float,
    limit_per_page: int = 50,
    max_pages: int = 4,
) -> list[MarketCandidate]:
    """Pull active markets, filter to viable MM candidates."""
    results: list[MarketCandidate] = []
    offset = 0
    for _ in range(max_pages):
        try:
            page = _gamma_get(f"active=true&closed=false&limit={limit_per_page}&offset={offset}&order=volume24hr&ascending=false")
        except Exception:
            break
        if not page:
            break
        for m in page:
            if not m.get("enableOrderBook"):
                continue
            if not m.get("acceptingOrders"):
                continue
            try:
                vol = float(m.get("volume24hr") or 0)
                liq = float(m.get("liquidity") or 0)
                bid = float(m.get("bestBid") or 0)
                ask = float(m.get("bestAsk") or 0)
            except (TypeError, ValueError):
                continue
            if vol < min_volume_24hr_usd or liq < min_liquidity_usd:
                continue
            if bid <= 0 or ask <= 0 or bid >= ask:
                continue
            spread = ask - bid
            if spread < min_spread_cents or spread > max_spread_cents:
                continue
            if bid < price_min or ask > price_max:
                continue
            clob_ids = m.get("clobTokenIds")
            if isinstance(clob_ids, str):
                try:
                    clob_ids = json.loads(clob_ids)
                except Exception:
                    continue
            if not isinstance(clob_ids, list) or len(clob_ids) < 2:
                continue
            score = vol * spread  # higher volume + bigger spread = more attractive
            results.append(MarketCandidate(
                slug=str(m.get("slug", "")),
                question=str(m.get("question", ""))[:120],
                condition_id=str(m.get("conditionId", "")),
                yes_token_id=str(clob_ids[0]),
                no_token_id=str(clob_ids[1]),
                best_bid=bid,
                best_ask=ask,
                spread=spread,
                volume_24hr=vol,
                liquidity=liq,
                end_date=str(m.get("endDate", "")),
                score=score,
            ))
        offset += limit_per_page
        time.sleep(0.1)
    results.sort(key=lambda c: c.score, reverse=True)
    return results
