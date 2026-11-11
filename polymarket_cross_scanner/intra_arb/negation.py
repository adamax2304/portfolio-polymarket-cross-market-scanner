"""Negation pair arbitrage scanner.

Detect Polymarket market pairs that are negations of each other:
  "Will X happen by Y?"  vs  "Will X NOT happen by Y?"

YES_a + YES_b should equal 1.0 (one of them must be true).
If sum > 1.05 → arb opportunity (buy NO of both).
If sum < 0.95 → arb opportunity (buy YES of both).

Heuristic: question text differs only by the addition of 'not'/'no'/'fail to'.
"""
from __future__ import annotations

import csv
import json
import re
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"
UA = {"User-Agent": "Mozilla/5.0 (NegArb/1.0)"}


@dataclass
class NegationMarket:
    slug: str
    question: str
    yes_price: float
    end_ts: Optional[int]
    volume_24h: float
    url: str


@dataclass
class NegationFinding:
    market_a: NegationMarket
    market_b: NegationMarket
    sum_yes: float
    gross_edge: float
    direction: str  # "BUY_BOTH_NO" if sum>1, "BUY_BOTH_YES" if sum<1


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


# Strip negation markers; return (stripped, was_negated)
NEG_WORDS = re.compile(r"\b(not|no|fail to|never|none|won['']?t|wouldn['']?t|doesn['']?t|aren['']?t|isn['']?t)\b", re.IGNORECASE)


def strip_negation(q: str) -> tuple[str, bool]:
    found = NEG_WORDS.search(q)
    if not found:
        return q.lower(), False
    stripped = NEG_WORDS.sub("", q).lower()
    # Normalize whitespace + drop common stopwords
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return stripped, True


def normalize(q: str) -> str:
    q = q.lower()
    q = re.sub(r"[^a-z0-9\s]", " ", q)
    return " ".join(sorted(set(t for t in q.split() if len(t) > 2)))


def scan_negation(
    *,
    min_gross_edge: float = 0.02,
    min_volume_24h: float = 100.0,
    output_csv: Optional[str] = None,
) -> list[NegationFinding]:
    print(f"[negation] fetching markets...")
    raw = fetch_markets()
    print(f"  got {len(raw)} markets")

    normalized: list[NegationMarket] = []
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
            end_ts = None
            if end_date:
                try:
                    end_ts = int(datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp())
                except Exception:
                    pass
            normalized.append(NegationMarket(
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

    # Strategy: bucket by normalized-no-negation question. Pair within each bucket.
    # Strict: require token sets to be near-identical except for negation.
    buckets: dict[str, list[tuple[NegationMarket, bool]]] = {}
    for m in normalized:
        stripped, was_neg = strip_negation(m.question)
        key = normalize(stripped)
        if len(key.split()) < 4:
            continue
        buckets.setdefault(key, []).append((m, was_neg))

    def jaccard_strict(a: str, b: str) -> float:
        ta = set(a.split())
        tb = set(b.split())
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / len(ta | tb)

    findings: list[NegationFinding] = []
    for key, items in buckets.items():
        if len(items) < 2:
            continue
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                ma, na = items[i]
                mb, nb = items[j]
                # Require one negated, one not
                if na == nb:
                    continue
                # Same end_ts within 1 day
                if ma.end_ts and mb.end_ts and abs(ma.end_ts - mb.end_ts) > 86400:
                    continue
                # Require strong overlap of non-negation tokens (≥0.7 Jaccard on raw text)
                qa = re.sub(r"[^a-z0-9\s]", " ", ma.question.lower())
                qb = re.sub(r"[^a-z0-9\s]", " ", mb.question.lower())
                # Remove number tokens since 10/9/11 in "10 Fed cuts" shouldn't all bucket together
                qa = re.sub(r"\b\d+\b", "", qa)
                qb = re.sub(r"\b\d+\b", "", qb)
                # Drop negation tokens from BOTH for similarity comparison
                qa_clean = NEG_WORDS.sub("", qa)
                qb_clean = NEG_WORDS.sub("", qb)
                if jaccard_strict(qa_clean, qb_clean) < 0.7:
                    continue
                s = ma.yes_price + mb.yes_price
                dev = s - 1.0
                if abs(dev) < min_gross_edge:
                    continue
                direction = "BUY_BOTH_NO" if dev > 0 else "BUY_BOTH_YES"
                findings.append(NegationFinding(
                    market_a=ma, market_b=mb, sum_yes=s,
                    gross_edge=abs(dev), direction=direction,
                ))

    findings.sort(key=lambda f: f.gross_edge, reverse=True)
    print(f"\n[negation] {len(findings)} findings above edge {min_gross_edge:.2%}\n")
    for i, f in enumerate(findings[:15], 1):
        print(f"#{i} edge={f.gross_edge:.3f} sum={f.sum_yes:.3f} direction={f.direction}")
        print(f"   A: {f.market_a.question[:90]}  YES={f.market_a.yes_price:.3f}")
        print(f"   B: {f.market_b.question[:90]}  YES={f.market_b.yes_price:.3f}")

    if output_csv:
        path = Path(output_csv)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ts", "rank", "sum_yes", "gross_edge", "direction",
                        "a_question", "a_yes", "a_url",
                        "b_question", "b_yes", "b_url"])
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for i, fnd in enumerate(findings, 1):
                w.writerow([ts, i, fnd.sum_yes, fnd.gross_edge, fnd.direction,
                            fnd.market_a.question, fnd.market_a.yes_price, fnd.market_a.url,
                            fnd.market_b.question, fnd.market_b.yes_price, fnd.market_b.url])
        print(f"\n[negation] csv → {path}")

    return findings
