"""Maintain per-token best_bid / best_ask state from WS book updates.

Polymarket CLOB WS messages:
  type=book (snapshot): full book replacement
  type=price_change: delta updates to one side

Each token in negRisk event tracked independently. Event-level Σbid/Σask
recomputed on every relevant token change.
"""
from __future__ import annotations

from dataclasses import dataclass
from time import time as now
from typing import Optional


@dataclass
class TokenBook:
    asset_id: str
    best_bid: float = 0.0  # 0 means no bid
    best_ask: float = 0.0  # 0 means no ask
    bids_full: dict[float, float] = None  # price -> size (full book)
    asks_full: dict[float, float] = None
    last_update_ts: float = 0.0
    update_count: int = 0

    def __post_init__(self):
        if self.bids_full is None:
            self.bids_full = {}
        if self.asks_full is None:
            self.asks_full = {}

    def apply_snapshot(self, msg: dict) -> None:
        """Apply a 'book' snapshot."""
        self.bids_full.clear()
        self.asks_full.clear()
        for b in msg.get("bids", []) or []:
            try:
                p = float(b.get("price", 0))
                s = float(b.get("size", 0))
                if p > 0 and s > 0:
                    self.bids_full[p] = s
            except (TypeError, ValueError):
                continue
        for a in msg.get("asks", []) or []:
            try:
                p = float(a.get("price", 0))
                s = float(a.get("size", 0))
                if p > 0 and s > 0:
                    self.asks_full[p] = s
            except (TypeError, ValueError):
                continue
        self._recompute_tops()
        self.last_update_ts = now()
        self.update_count += 1

    def apply_price_change(self, changes: list[dict]) -> None:
        """Apply incremental 'price_change' deltas.

        Each change has side, price, size. size=0 removes that level.
        """
        for c in changes:
            try:
                side = c.get("side", "")
                p = float(c.get("price", 0))
                s = float(c.get("size", 0))
                if p <= 0:
                    continue
                target = self.bids_full if side == "BUY" else self.asks_full
                if s == 0:
                    target.pop(p, None)
                else:
                    target[p] = s
            except (TypeError, ValueError):
                continue
        self._recompute_tops()
        self.last_update_ts = now()
        self.update_count += 1

    def _recompute_tops(self) -> None:
        self.best_bid = max(self.bids_full.keys()) if self.bids_full else 0.0
        self.best_ask = min(self.asks_full.keys()) if self.asks_full else 0.0

    def size_at_best_bid(self) -> float:
        return self.bids_full.get(self.best_bid, 0.0) if self.best_bid > 0 else 0.0

    def size_at_best_ask(self) -> float:
        return self.asks_full.get(self.best_ask, 0.0) if self.best_ask > 0 else 0.0


class EventBookAggregator:
    """Tracks all token books for one negRisk event and computes Σ stats."""

    def __init__(self, event_slug: str, token_ids: list[str]):
        self.event_slug = event_slug
        self.token_ids = list(token_ids)
        self.books: dict[str, TokenBook] = {t: TokenBook(asset_id=t) for t in token_ids}

    def get_or_create(self, asset_id: str) -> TokenBook:
        if asset_id not in self.books:
            self.books[asset_id] = TokenBook(asset_id=asset_id)
        return self.books[asset_id]

    def sum_bids(self) -> tuple[float, int, int]:
        """Returns (Σ best_bid, count with bid, total tokens)."""
        s = 0.0
        n_with = 0
        for b in self.books.values():
            if b.best_bid > 0:
                s += b.best_bid
                n_with += 1
        return s, n_with, len(self.books)

    def sum_asks(self) -> tuple[float, int, int]:
        s = 0.0
        n_with = 0
        for b in self.books.values():
            if b.best_ask > 0:
                s += b.best_ask
                n_with += 1
        return s, n_with, len(self.books)

    def all_tokens_ready(self) -> bool:
        return all(b.update_count > 0 for b in self.books.values())

    def bottleneck_bid_size(self) -> float:
        """Min size_at_best_bid across all tokens.

        For SPLIT_AND_SELL: we must sell ONE token from each market simultaneously,
        so notional executable = min(size_at_best_bid) shares × $1 redemption.
        """
        sizes = []
        for b in self.books.values():
            if b.best_bid <= 0:
                return 0.0
            sizes.append(b.size_at_best_bid())
        return min(sizes) if sizes else 0.0

    def bottleneck_ask_size(self) -> float:
        """Min size_at_best_ask across all tokens.

        For BUY_AND_MERGE: we buy ONE share from each token simultaneously,
        so executable size = min(size_at_best_ask).
        """
        sizes = []
        for b in self.books.values():
            if b.best_ask <= 0:
                return 0.0
            sizes.append(b.size_at_best_ask())
        return min(sizes) if sizes else 0.0

    def freshness(self) -> tuple[float, float]:
        """Returns (oldest update age in seconds, newest update age)."""
        ts = [b.last_update_ts for b in self.books.values() if b.last_update_ts > 0]
        if not ts:
            return (float("inf"), float("inf"))
        cur = now()
        return (cur - min(ts), cur - max(ts))

    @staticmethod
    def _leg_fee(price: float, fee_rate: float) -> float:
        """Polymarket per-trade taker fee for 1 share at `price`.

        Polymarket switched to per-category, price-weighted taker fees on
        2026-03-23 (previously zero). Public formula:
            fee = shares · price · feeRate · (price·(1-price))^exponent
        i.e. per-share fee = feeRate · price² · (1-price)   (exponent=1).

        The (price·(1-price)) factor makes the fee SMALL for extreme-priced
        tokens — exactly the cheap (~$0.06) legs that dominate negRisk
        exact-score events. fee_rate defaults to the sports-anchored 0.06
        (≈0.75¢/share max at p=0.5); economics≈0.10, crypto≈0.144,
        geopolitical=0. Approximate from public sources — wire the live SDK
        fee before real execution.
        """
        if price <= 0 or price >= 1:
            return 0.0
        return fee_rate * price * price * (1.0 - price)

    def walk_book_profit(
        self,
        direction: str,
        fee_rate: float = 0.06,
        capital: float = 100.0,
    ) -> tuple[float, float, float]:
        """Realistic execution simulator: walk DOWN/UP the full book level by level.

        This is the core realism upgrade over bottleneck_*_size, which only looked
        at the single best level. Here we model executing an arb by consuming
        liquidity across multiple price levels (the deeper we go, the worse the
        fill price), finding the profit-maximizing trade size bounded by BOTH
        available book depth AND deployable capital. Fees are charged per leg with
        Polymarket's price-weighted formula (see _leg_fee).

        SPLIT_AND_SELL: split $1 -> 1 share of each token, sell each share into its
          bid book. Marginal profit of the x-th unit
            = Σ_i [bid_i(x) - leg_fee(bid_i(x))] - 1
          We keep adding units while marginal profit > 0.

        BUY_AND_MERGE: buy 1 share of each token up its ask book, then merge -> $1.
          Marginal profit of the x-th unit
            = 1 - Σ_i [ask_i(x) + leg_fee(ask_i(x))]

        Returns (units, gross_profit, cost):
          units        — profit-maximizing executable size (shares of each token)
          gross_profit — total profit at that size (net of fees, before gas)
          cost         — USDC capital consumed (to flag capital-binding)

        units==0 means the order would NOT fill profitably at current book — a
        realistic "no-fill" outcome. Complexity O(total levels), called only at
        sample checkpoints, never in the per-message hot path.
        """
        is_split = direction == "SPLIT_AND_SELL"
        # Build per-token level ladders, best price first.
        ladders: list[list[tuple[float, float]]] = []
        for b in self.books.values():
            if is_split:
                levels = sorted(b.bids_full.items(), key=lambda kv: -kv[0])
            else:
                levels = sorted(b.asks_full.items(), key=lambda kv: kv[0])
            # Drop non-positive sizes defensively
            levels = [(p, s) for p, s in levels if p > 0 and s > 0]
            if not levels:
                # One token has an empty book on the needed side -> arb impossible
                return (0.0, 0.0, 0.0)
            ladders.append(levels)

        n = len(ladders)
        idx = [0] * n
        rem = [ladders[i][0][1] for i in range(n)]
        units = 0.0
        profit = 0.0
        cost = 0.0
        EPS = 1e-9
        # Hard iteration cap = total level count + n (each loop exhausts >=1 level
        # or breaks). Guards against any pathological float loop.
        max_iter = sum(len(l) for l in ladders) + n + 5

        for _ in range(max_iter):
            # Advance any token that has exhausted its current level.
            exhausted = False
            for i in range(n):
                while rem[i] <= EPS:
                    if idx[i] + 1 < len(ladders[i]):
                        idx[i] += 1
                        rem[i] = ladders[i][idx[i]][1]
                    else:
                        exhausted = True
                        break
                if exhausted:
                    break
            if exhausted:
                break  # a token's book fully consumed -> cannot execute more units

            prices = [ladders[i][idx[i]][0] for i in range(n)]
            sum_p = sum(prices)
            fee_total = sum(self._leg_fee(p, fee_rate) for p in prices)
            if is_split:
                # split costs $1, sell each leg for its bid, pay per-leg fee
                marg = sum_p - fee_total - 1.0
                unit_cost = 1.0
            else:
                # buy each leg at its ask + fee, merge yields $1
                marg = 1.0 - sum_p - fee_total
                unit_cost = sum_p + fee_total

            if marg <= 0:
                break  # no more profitable depth

            seg = min(rem)  # units available before some token changes level
            if unit_cost > 0:
                cap_left = (capital - cost) / unit_cost
                if cap_left <= EPS:
                    break  # capital exhausted
                seg = min(seg, cap_left)
            if seg <= EPS:
                break

            profit += marg * seg
            cost += unit_cost * seg
            units += seg
            for i in range(n):
                rem[i] -= seg

        return (units, profit, cost)
