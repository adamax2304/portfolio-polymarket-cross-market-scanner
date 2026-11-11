"""Discover active negRisk events + their token IDs."""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from typing import Optional


GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
UA = {"User-Agent": "Mozilla/5.0 (WSArb/1.0)"}


@dataclass
class TrackedEvent:
    slug: str
    title: str
    condition_id: str  # one of the markets' condition_id (used for /trades)
    yes_token_ids: list[str]  # token IDs we'll subscribe to
    market_questions: list[str]  # parallel to yes_token_ids
    volume_24h: float
    url: str


def _get(url: str) -> Optional[object]:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  ! gamma fetch fail: {e}")
        return None


def discover_negrisk_events(
    min_volume_24h: float = 1000.0,
    max_events: int = 100,
    min_markets: int = 3,
) -> list[TrackedEvent]:
    """Pull active negRisk events sorted by volume."""
    out: list[TrackedEvent] = []
    for page in range(6):
        offset = page * 50
        url = f"{GAMMA_EVENTS}?active=true&closed=false&limit=50&offset={offset}&order=volume24hr&ascending=false"
        data = _get(url)
        if not isinstance(data, list) or not data:
            break
        for ev in data:
            try:
                if not (ev.get("negRisk") or ev.get("enableNegRisk") or ev.get("negRiskAugmented")):
                    continue
                vol = float(ev.get("volume24hr") or 0)
                if vol < min_volume_24h:
                    continue
                markets = ev.get("markets") or []
                if len(markets) < min_markets:
                    continue
                token_ids = []
                questions = []
                first_condition = None
                for m in markets:
                    if m.get("closed"):
                        continue
                    clob_ids = m.get("clobTokenIds")
                    if isinstance(clob_ids, str):
                        try:
                            clob_ids = json.loads(clob_ids)
                        except Exception:
                            continue
                    if not isinstance(clob_ids, list) or len(clob_ids) < 1:
                        continue
                    # YES token is clob_ids[0]
                    token_ids.append(str(clob_ids[0]))
                    questions.append(str(m.get("question") or m.get("groupItemTitle") or "")[:120])
                    if not first_condition:
                        first_condition = str(m.get("conditionId") or "")
                if len(token_ids) < min_markets:
                    continue
                out.append(TrackedEvent(
                    slug=str(ev.get("slug", "")),
                    title=str(ev.get("title", ""))[:200],
                    condition_id=first_condition or "",
                    yes_token_ids=token_ids,
                    market_questions=questions,
                    volume_24h=vol,
                    url=f"https://polymarket.com/event/{ev.get('slug', '')}",
                ))
                if len(out) >= max_events:
                    break
            except (TypeError, ValueError):
                continue
        if len(out) >= max_events:
            break
        if len(data) < 50:
            break
        time.sleep(0.15)
    return out
