from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import time

from .models import OrderBook


@dataclass(frozen=True)
class QuoteSample:
    ts: float
    ask: float | None
    bid: float | None


class QuoteStabilityTracker:
    def __init__(self, *, window_seconds: float, max_move: float, min_samples: int):
        self.window_seconds = window_seconds
        self.max_move = max_move
        self.min_samples = min_samples
        self.samples: dict[str, deque[QuoteSample]] = {}

    def update_many(self, books: dict[str, OrderBook]) -> None:
        now = time.time()
        for token_id, book in books.items():
            queue = self.samples.setdefault(token_id, deque())
            queue.append(QuoteSample(now, book.best_ask, book.best_bid))
            while queue and now - queue[0].ts > self.window_seconds:
                queue.popleft()

    def unstable_tokens(self) -> set[str]:
        out: set[str] = set()
        for token_id, queue in self.samples.items():
            if len(queue) < self.min_samples:
                continue
            asks = [sample.ask for sample in queue if sample.ask is not None]
            bids = [sample.bid for sample in queue if sample.bid is not None]
            ask_move = max(asks) - min(asks) if len(asks) >= self.min_samples else 0.0
            bid_move = max(bids) - min(bids) if len(bids) >= self.min_samples else 0.0
            if max(ask_move, bid_move) > self.max_move:
                out.add(token_id)
        return out
