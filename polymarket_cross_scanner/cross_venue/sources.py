"""Data source adapters for prediction markets.

Each source returns a list of `NormalizedMarket` records with:
  venue, slug/id, question, yes_price, no_price, volume_24h, end_ts, url
"""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Optional


UA = {"User-Agent": "Mozilla/5.0 (CrossVenueScanner/1.0)"}


@dataclass
class NormalizedMarket:
    venue: str           # "polymarket" / "manifold" / "kalshi" / "predictit"
    market_id: str       # venue-specific id (used for URL + dedupe)
    question: str        # canonical question text
    yes_price: float     # 0..1 implied prob of YES
    no_price: float      # 0..1 implied prob of NO  (often = 1 - yes)
    volume_24h_usd: float
    liquidity_usd: float
    end_ts: Optional[int] = None
    url: Optional[str] = None
    raw: dict = field(default_factory=dict)


def _get(url: str, timeout: float = 15.0) -> Optional[object]:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  ! fetch failed {url[:80]}: {e}")
        return None


# ---------- Polymarket ----------

def fetch_polymarket(limit_pages: int = 4, page_size: int = 100) -> list[NormalizedMarket]:
    out: list[NormalizedMarket] = []
    for page in range(limit_pages):
        offset = page * page_size
        url = f"https://gamma-api.polymarket.com/markets?active=true&closed=false&limit={page_size}&offset={offset}&order=volume24hr&ascending=false"
        data = _get(url)
        if not data or not isinstance(data, list):
            break
        for m in data:
            try:
                yes = float(m.get("bestBid") or 0)
                ask = float(m.get("bestAsk") or 0)
                if yes <= 0 or ask <= 0 or yes >= ask:
                    continue
                mid_yes = (yes + ask) / 2
                vol = float(m.get("volume24hr") or 0)
                liq = float(m.get("liquidity") or 0)
                if vol < 100:  # skip dead markets
                    continue
                out.append(NormalizedMarket(
                    venue="polymarket",
                    market_id=str(m.get("slug", "")),
                    question=str(m.get("question", ""))[:200],
                    yes_price=mid_yes,
                    no_price=1.0 - mid_yes,
                    volume_24h_usd=vol,
                    liquidity_usd=liq,
                    end_ts=None,
                    url=f"https://polymarket.com/event/{m.get('slug', '')}",
                    raw=m,
                ))
            except (TypeError, ValueError):
                continue
        if len(data) < page_size:
            break
        time.sleep(0.2)
    return out


# ---------- Manifold ----------

def fetch_manifold(limit: int = 500) -> list[NormalizedMarket]:
    """Manifold uses cursor pagination via beforeTime. Top markets by volume."""
    out: list[NormalizedMarket] = []
    # Get sorted by liquidity to focus on markets with real bookings
    url = f"https://api.manifold.markets/v0/markets?limit={min(limit, 1000)}"
    data = _get(url)
    if not data or not isinstance(data, list):
        return out
    for m in data:
        try:
            if m.get("outcomeType") != "BINARY":
                continue
            if m.get("isResolved"):
                continue
            prob = float(m.get("probability") or m.get("p", 0.5))
            vol = float(m.get("volume24Hours") or m.get("volume", 0))
            liq = float(m.get("totalLiquidity") or 0)
            if vol < 50:
                continue
            out.append(NormalizedMarket(
                venue="manifold",
                market_id=str(m.get("id", "")),
                question=str(m.get("question", ""))[:200],
                yes_price=prob,
                no_price=1.0 - prob,
                volume_24h_usd=vol,
                liquidity_usd=liq,
                end_ts=int(m.get("closeTime", 0) / 1000) if m.get("closeTime") else None,
                url=str(m.get("url", "")),
                raw=m,
            ))
        except (TypeError, ValueError):
            continue
    return out


# ---------- Kalshi ----------

def fetch_kalshi(limit_pages: int = 4) -> list[NormalizedMarket]:
    """Kalshi public read endpoint."""
    out: list[NormalizedMarket] = []
    cursor = ""
    for _ in range(limit_pages):
        url = f"https://api.elections.kalshi.com/trade-api/v2/markets?status=open&limit=200"
        if cursor:
            url += f"&cursor={cursor}"
        data = _get(url)
        if not isinstance(data, dict):
            break
        markets = data.get("markets", []) or []
        for m in markets:
            try:
                # Kalshi yes_bid/yes_ask in cents (0..100)
                yes_bid = float(m.get("yes_bid", 0)) / 100
                yes_ask = float(m.get("yes_ask", 0)) / 100
                if yes_bid <= 0 or yes_ask <= 0 or yes_bid >= yes_ask:
                    continue
                mid = (yes_bid + yes_ask) / 2
                vol = float(m.get("volume_24h", 0))
                liq = float(m.get("liquidity", 0))
                if vol < 100:
                    continue
                ticker = str(m.get("ticker", ""))
                title = str(m.get("title", "") or m.get("subtitle", ""))
                out.append(NormalizedMarket(
                    venue="kalshi",
                    market_id=ticker,
                    question=title[:200],
                    yes_price=mid,
                    no_price=1.0 - mid,
                    volume_24h_usd=vol,
                    liquidity_usd=liq,
                    end_ts=None,
                    url=f"https://kalshi.com/markets/{ticker.split('-')[0].lower()}/{ticker.lower()}",
                    raw=m,
                ))
            except (TypeError, ValueError):
                continue
        cursor = data.get("cursor", "") or ""
        if not cursor or not markets:
            break
        time.sleep(0.2)
    return out


# ---------- PredictIt ----------

def fetch_predictit() -> list[NormalizedMarket]:
    """PredictIt public data endpoint. Each 'market' has multiple contracts."""
    out: list[NormalizedMarket] = []
    data = _get("https://www.predictit.org/api/marketdata/all/")
    if not isinstance(data, dict):
        return out
    for mkt in data.get("markets", []) or []:
        title = str(mkt.get("name", ""))[:200]
        for c in mkt.get("contracts", []) or []:
            try:
                if c.get("status") != "Open":
                    continue
                yes = float(c.get("bestBuyYesCost", 0) or 0)
                no = float(c.get("bestBuyNoCost", 0) or 0)
                if yes <= 0 or no <= 0:
                    continue
                # PredictIt: yes_cost + no_cost ~= 1 (with some friction)
                mid = yes  # implied YES probability
                # combine market name + contract name as question
                contract_name = str(c.get("name", ""))
                full_q = f"{title} :: {contract_name}" if contract_name else title
                out.append(NormalizedMarket(
                    venue="predictit",
                    market_id=str(c.get("id", "")),
                    question=full_q[:200],
                    yes_price=mid,
                    no_price=1.0 - mid,
                    volume_24h_usd=0.0,  # not exposed in this endpoint
                    liquidity_usd=0.0,
                    end_ts=None,
                    url=str(mkt.get("url", "") or f"https://www.predictit.org/markets/detail/{mkt.get('id')}"),
                    raw=c,
                ))
            except (TypeError, ValueError):
                continue
    return out
