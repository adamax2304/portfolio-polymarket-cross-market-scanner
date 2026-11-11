"""Calendar monotonicity scanner.

Detect markets like "BTC > $100K by Date X" with multiple X values.
P(by later date) must be ≥ P(by earlier date). If reversed, arb.

Arb structure (when P_earlier > P_later):
  Buy YES of later (cheap)  ← we win if event happens before later date
  Buy NO of earlier (cheap because P(YES_earlier) is high)
  Gross edge = P_earlier - P_later  (per pair)

Heuristic: cluster markets by their question text stripped of dates/numbers,
within each cluster sort by end_ts and check yes_prices monotonicity.
"""
from __future__ import annotations

import csv
import json
import re
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"
UA = {"User-Agent": "Mozilla/5.0 (CalArb/1.0)"}


@dataclass
class CalendarMarket:
    slug: str
    question: str
    yes_price: float
    end_ts: int
    volume_24h: float
    url: str


@dataclass
class CalendarFinding:
    cluster_key: str
    market_earlier: CalendarMarket
    market_later: CalendarMarket
    violation: float  # earlier_yes - later_yes  (positive = arb opportunity)


def _get(url: str) -> Optional[object]:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  ! fetch failed: {e}")
        return None


def fetch_markets(limit_pages: int = 6, page_size: int = 100) -> list[dict]:
    out: list[dict] = []
    for page in range(limit_pages):
        offset = page * page_size
        url = f"{GAMMA_MARKETS}?active=true&closed=false&limit={page_size}&offset={offset}&order=volume24hr&ascending=false"
        data = _get(url)
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        if len(data) < page_size:
            break
        time.sleep(0.15)
    return out


DATE_PATTERN = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec|"
    r"q[1234]|h[12]|eoy|end of year|"
    r"\d{1,2}(st|nd|rd|th)?(\s+\d{4})?)\b",
    re.IGNORECASE,
)
# Match prices like $54,000 / $66k / 100K
PRICE_PATTERN = re.compile(r"(\$[\d,]+(?:\.\d+)?[kKmM]?|\b\d{2,}[kKmM]\b)")
# Numeric ranges like "200-219" or "80-99" or "500+"
RANGE_PATTERN = re.compile(r"\b(\d+\s*[-–]\s*\d+|\d+\+)\b")
# Year tokens we DON'T want to strip (keep distinct thresholds)
YEAR_PATTERN = re.compile(r"\b(20[2-3]\d)\b")


def cluster_key(question: str) -> str:
    """Strip dates, but KEEP price thresholds + numeric ranges as part of the key.

    Two markets share a cluster only if they ask the same question with different DATES.
    Different price thresholds OR different count ranges → different clusters.
    """
    q = question.lower()
    # Extract thresholds and ranges, KEEP them as part of the key
    thresholds = PRICE_PATTERN.findall(q)
    ranges = RANGE_PATTERN.findall(q)
    # Strip dates and years
    q = DATE_PATTERN.sub("", q)
    q = YEAR_PATTERN.sub("", q)
    q = re.sub(r"[^a-z0-9\s$.,]", " ", q)
    tokens = [t for t in q.split() if len(t) > 2 and not t.isdigit()]
    # Combine threshold + range info into cluster key
    thresholds_norm = sorted({t.replace(",", "").replace("$", "").lower() for t in thresholds})
    ranges_norm = sorted({r.replace(" ", "").replace("–", "-") for r in ranges})
    base = " ".join(sorted(set(tokens)))
    suffix_parts = []
    if thresholds_norm: suffix_parts.append("p:" + ",".join(thresholds_norm))
    if ranges_norm: suffix_parts.append("r:" + ",".join(ranges_norm))
    suffix = " :: ".join(suffix_parts)
    return f"{base} :: {suffix}" if suffix else base


def scan_calendar(
    *,
    min_violation: float = 0.02,
    min_volume_24h: float = 100.0,
    output_csv: Optional[str] = None,
) -> list[CalendarFinding]:
    print(f"[calendar] fetching markets...")
    raw = fetch_markets()
    print(f"  got {len(raw)} markets")

    # Normalize
    normalized: list[CalendarMarket] = []
    for m in raw:
        try:
            bid = float(m.get("bestBid") or 0)
            ask = float(m.get("bestAsk") or 0)
            if bid <= 0 or ask <= 0 or bid >= ask:
                continue
            vol = float(m.get("volume24hr") or 0)
            if vol < min_volume_24h:
                continue
            end_date = m.get("endDate")
            if not end_date:
                continue
            end_ts = int(datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp())
            normalized.append(CalendarMarket(
                slug=str(m.get("slug", "")),
                question=str(m.get("question", ""))[:200],
                yes_price=(bid + ask) / 2,
                end_ts=end_ts,
                volume_24h=vol,
                url=f"https://polymarket.com/event/{m.get('slug', '')}",
            ))
        except (TypeError, ValueError):
            continue
    print(f"  normalized {len(normalized)} usable")

    # Cluster
    clusters: dict[str, list[CalendarMarket]] = {}
    for m in normalized:
        key = cluster_key(m.question)
        if len(key.split()) < 3:
            continue  # too generic
        clusters.setdefault(key, []).append(m)

    findings: list[CalendarFinding] = []
    for key, items in clusters.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda m: m.end_ts)
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a = items[i]  # earlier
                b = items[j]  # later
                if b.end_ts - a.end_ts < 86400:
                    continue  # less than 1 day apart, skip
                violation = a.yes_price - b.yes_price
                if violation < min_violation:
                    continue
                findings.append(CalendarFinding(
                    cluster_key=key,
                    market_earlier=a,
                    market_later=b,
                    violation=violation,
                ))

    findings.sort(key=lambda f: f.violation, reverse=True)
    print(f"\n[calendar] {len(findings)} monotonicity violations above {min_violation:.2%}\n")
    for i, f in enumerate(findings[:15], 1):
        print(f"#{i} violation={f.violation:.3f}")
        print(f"   EARLIER: {f.market_earlier.question[:90]}")
        print(f"            YES={f.market_earlier.yes_price:.3f} ends={datetime.fromtimestamp(f.market_earlier.end_ts, tz=timezone.utc).isoformat()[:10]}")
        print(f"   LATER:   {f.market_later.question[:90]}")
        print(f"            YES={f.market_later.yes_price:.3f} ends={datetime.fromtimestamp(f.market_later.end_ts, tz=timezone.utc).isoformat()[:10]}")
        print(f"   url: {f.market_earlier.url}  vs  {f.market_later.url}")

    if output_csv:
        path = Path(output_csv)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ts", "rank", "cluster_key", "violation",
                        "earlier_question", "earlier_yes", "earlier_end", "earlier_url",
                        "later_question", "later_yes", "later_end", "later_url"])
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for i, fnd in enumerate(findings, 1):
                w.writerow([ts, i, fnd.cluster_key, fnd.violation,
                            fnd.market_earlier.question, fnd.market_earlier.yes_price,
                            datetime.fromtimestamp(fnd.market_earlier.end_ts, tz=timezone.utc).isoformat(),
                            fnd.market_earlier.url,
                            fnd.market_later.question, fnd.market_later.yes_price,
                            datetime.fromtimestamp(fnd.market_later.end_ts, tz=timezone.utc).isoformat(),
                            fnd.market_later.url])
        print(f"\n[calendar] csv → {path}")

    return findings
