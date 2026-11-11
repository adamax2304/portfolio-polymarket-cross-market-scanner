"""Fuzzy match markets across venues by question content.

Strategy:
 1. Normalize question text (lowercase, strip punctuation, remove stopwords)
 2. Extract entity tokens (proper nouns, numbers, dates)
 3. For each pair of markets from DIFFERENT venues, compute similarity:
    - Jaccard on key tokens
    - Required: must share at least one "anchor" token (name, year, number)
 4. Return matches above threshold
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .sources import NormalizedMarket


STOPWORDS = set("a an the of in on at to for and or but is are was were be been being "
                "have has had do does did will would should could may might must can "
                "this that these those by with from as it its their there here when "
                "where what who which how why if then else than that's there's".split())

# Words that indicate an entity (kept even if generic)
ANCHOR_HINT = set("trump biden harris obama putin xi modi musk tesla bitcoin btc eth ethereum "
                  "solana sol fed cpi gdp election president senate house republican democrat "
                  "nfl nba nfc afc mvp superbowl world cup olympic".split())


def normalize_question(q: str) -> set[str]:
    """Lowercase, strip punctuation, drop stopwords. Return token set."""
    q = q.lower()
    q = re.sub(r"[^a-z0-9\s]", " ", q)
    tokens = [t for t in q.split() if t and t not in STOPWORDS and len(t) > 1]
    return set(tokens)


def extract_anchors(tokens: set[str]) -> set[str]:
    """Tokens that should match exactly: numbers, year-like, known entities."""
    anchors = set()
    for t in tokens:
        if t in ANCHOR_HINT:
            anchors.add(t)
        elif t.isdigit() and (1900 < int(t) < 2100 or len(t) >= 3):
            # year or large number
            anchors.add(t)
        elif re.match(r"^\d+k?$", t):
            anchors.add(t)
    return anchors


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# Words that disqualify a match if they differ between two markets
# (these denote different specific subjects within the same theme)
DISCRIMINATING_TOKENS = set("""
trump biden harris obama putin xi musk tesla bezos zuckerberg pelosi mcconnell newsom desantis
algeria argentina australia austria belgium brazil canada china colombia croatia czechia denmark
ecuador egypt england france germany ghana iran israel italy japan mexico morocco netherlands
nigeria norway paraguay peru poland portugal russia scotland senegal serbia spain sweden switzerland
turkey ukraine uruguay usa wales
chiefs ravens packers cowboys lakers celtics warriors knicks bulls eagles bills steelers
btc bitcoin eth ethereum sol solana xrp doge dogecoin ada bnb shib pepe bonk avax matic
january february march april may june july august september october november december
yes no over under more less above below greater fewer goals points runs games matches
democrat republican gop dnc senator congressman governor mayor
""".split())

# Quantitative comparisons - if one side has these, both must
QUANT_MARKERS = set("more less over under above below greater fewer than".split())


def has_distinguishing_conflict(a_tokens: set, b_tokens: set) -> bool:
    """Conflict types that disqualify a match.

    1. Different discriminating tokens (country/person/team).
    2. One side has a quantitative comparison ("more than 150") and the other doesn't.
    3. Both have quantitative comparisons but the numbers differ.
    """
    a_disc = a_tokens & DISCRIMINATING_TOKENS
    b_disc = b_tokens & DISCRIMINATING_TOKENS
    # Only flag conflict if BOTH have disc tokens AND they differ
    if a_disc and b_disc and a_disc != b_disc and not (a_disc & b_disc):
        return True

    # Quantitative comparison mismatch
    a_quant = a_tokens & QUANT_MARKERS
    b_quant = b_tokens & QUANT_MARKERS
    if bool(a_quant) != bool(b_quant):
        return True
    # If both quant, check numbers differ
    if a_quant and b_quant:
        a_nums = {t for t in a_tokens if t.isdigit() and int(t) > 1 and int(t) < 1900}
        b_nums = {t for t in b_tokens if t.isdigit() and int(t) > 1 and int(t) < 1900}
        if a_nums and b_nums and a_nums != b_nums:
            return True
    return False


@dataclass
class CrossMatch:
    venue_a: str
    venue_b: str
    market_a: NormalizedMarket
    market_b: NormalizedMarket
    similarity: float
    shared_anchors: set
    yes_spread: float  # market_a.yes - market_b.yes (signed)
    abs_spread: float


def find_matches(
    markets_by_venue: dict[str, list[NormalizedMarket]],
    *,
    min_similarity: float = 0.4,
    require_shared_anchor: bool = True,
) -> list[CrossMatch]:
    """Cross-product all venue pairs, return matches above similarity threshold."""
    # Pre-tokenize
    tokenized: dict[str, list[tuple[NormalizedMarket, set[str], set[str]]]] = {}
    for venue, mkts in markets_by_venue.items():
        tokenized[venue] = []
        for m in mkts:
            toks = normalize_question(m.question)
            anchors = extract_anchors(toks)
            tokenized[venue].append((m, toks, anchors))

    venues = sorted(tokenized.keys())
    matches: list[CrossMatch] = []
    for i in range(len(venues)):
        for j in range(i + 1, len(venues)):
            va, vb = venues[i], venues[j]
            for (ma, ta, aa) in tokenized[va]:
                for (mb, tb, ab) in tokenized[vb]:
                    if require_shared_anchor and not (aa & ab):
                        continue
                    if has_distinguishing_conflict(ta, tb):
                        continue
                    sim = jaccard(ta, tb)
                    if sim < min_similarity:
                        continue
                    spread = ma.yes_price - mb.yes_price
                    matches.append(CrossMatch(
                        venue_a=va, venue_b=vb,
                        market_a=ma, market_b=mb,
                        similarity=sim,
                        shared_anchors=aa & ab,
                        yes_spread=spread,
                        abs_spread=abs(spread),
                    ))
    matches.sort(key=lambda m: m.abs_spread, reverse=True)
    return matches
