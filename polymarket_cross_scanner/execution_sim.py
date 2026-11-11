from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
import time

from .config import ScannerConfig
from .models import ArbSignal, BookLevel, CrossMarketExecutionSim, OrderBook
from .strategy import levels_json
from .utils import utc_now


LOCKED_ACTIONS = {"BOTH_FILL_STRICT", "BOTH_FILL_SIMULTANEOUS"}


@dataclass(frozen=True)
class FillResult:
    filled: bool
    shares: float
    cost_or_proceeds: float
    avg_price: float | None


class CrossMarketExecutionSimulator:
    def __init__(self, cfg: ScannerConfig, clob):
        self.cfg = cfg
        self.clob = clob

    def simulate_many(
        self,
        signals: list[ArbSignal],
        initial_books: dict[str, OrderBook],
    ) -> list[CrossMarketExecutionSim]:
        return [self.simulate(signal, initial_books) for signal in signals]

    def simulate(self, signal: ArbSignal, initial_books: dict[str, OrderBook]) -> CrossMarketExecutionSim:
        max_target_shares = min(signal.executable_shares, self.cfg.max_paper_shares)
        strict_max_pair_cost = 1.0 - self.cfg.min_edge
        rescue_max_pair_cost = strict_max_pair_cost + self.cfg.cross_execution_rescue_extra_cost
        up_initial = initial_books.get(signal.up_token_id)
        down_initial = initial_books.get(signal.down_token_id)
        leg_order = choose_first_leg(signal)

        started = time.perf_counter()
        if self.cfg.cross_execution_refetch_delay_ms > 0:
            time.sleep(self.cfg.cross_execution_refetch_delay_ms / 1000)
        fresh_up, fresh_down = self._refetch_pair(signal.up_token_id, signal.down_token_id)
        fresh_up = fresh_up or up_initial
        fresh_down = fresh_down or down_initial
        latency_ms = int((time.perf_counter() - started) * 1000)

        books_by_leg = {"UP": fresh_up, "DOWN": fresh_down}
        asks_by_leg = {
            "UP": fresh_up.asks if fresh_up else [],
            "DOWN": fresh_down.asks if fresh_down else [],
        }
        bids_by_leg = {
            "UP": fresh_up.bids if fresh_up else [],
            "DOWN": fresh_down.bids if fresh_down else [],
        }
        initial_ask_by_leg = {"UP": signal.up_ask, "DOWN": signal.down_ask}

        target_shares = max_target_shares
        if self.cfg.cross_execution_dynamic_sizing_enabled:
            target_shares = choose_dynamic_shares(
                max_target_shares,
                min_shares=self.cfg.min_shares,
                step=5.0,
                up_asks=asks_by_leg["UP"],
                down_asks=asks_by_leg["DOWN"],
                max_pair_cost=strict_max_pair_cost,
            )
            if target_shares <= 0:
                return self._row(
                    signal,
                    min(max_target_shares, self.cfg.min_shares),
                    "UP",
                    "DOWN",
                    strict_max_pair_cost,
                    rescue_max_pair_cost,
                    fresh_up,
                    fresh_down,
                    FillResult(False, 0, 0, None),
                    FillResult(False, 0, 0, None),
                    FillResult(False, 0, 0, None),
                    FillResult(False, 0, 0, None),
                    final_action="NO_DYNAMIC_SIZE_FILL",
                    refetch_latency_ms=latency_ms,
                    reason="no_candidate_size_passed_strict_pair_cost",
                )

        if self.cfg.cross_execution_simultaneous_enabled:
            simultaneous = simulate_pair_buy(asks_by_leg["UP"], asks_by_leg["DOWN"], target_shares, strict_max_pair_cost)
            if simultaneous.filled:
                up_fill = simulate_fok_buy(asks_by_leg["UP"], target_shares, max_price=1.0)
                down_fill = simulate_fok_buy(asks_by_leg["DOWN"], target_shares, max_price=1.0)
                pair_cost = up_fill.cost_or_proceeds + down_fill.cost_or_proceeds
                return self._row(
                    signal,
                    target_shares,
                    "UP",
                    "DOWN",
                    strict_max_pair_cost,
                    rescue_max_pair_cost,
                    fresh_up,
                    fresh_down,
                    up_fill,
                    down_fill,
                    FillResult(False, 0, 0, None),
                    FillResult(False, 0, 0, None),
                    final_action="BOTH_FILL_SIMULTANEOUS",
                    refetch_latency_ms=latency_ms,
                    reason=f"simultaneous_pair_cost={pair_cost / target_shares:.4f}",
                    final_pair_cost=pair_cost,
                )

        first_leg = leg_order
        second_leg = "DOWN" if first_leg == "UP" else "UP"
        first_limit = initial_ask_by_leg[first_leg] + self.cfg.cross_execution_entry_slippage
        first = simulate_fok_buy(asks_by_leg[first_leg], target_shares, first_limit)
        if not first.filled:
            return self._row(
                signal,
                target_shares,
                first_leg,
                second_leg,
                strict_max_pair_cost,
                rescue_max_pair_cost,
                fresh_up,
                fresh_down,
                first,
                FillResult(False, 0, 0, None),
                FillResult(False, 0, 0, None),
                FillResult(False, 0, 0, None),
                final_action="NO_FIRST_LEG_FILL",
                refetch_latency_ms=latency_ms,
                reason=f"first_leg_fok_failed limit={first_limit:.4f}",
            )

        second_strict_limit = max(0.001, strict_max_pair_cost - (first.avg_price or 0.0))
        second_strict = simulate_fok_buy(asks_by_leg[second_leg], target_shares, second_strict_limit)
        if second_strict.filled:
            pair_cost = first.cost_or_proceeds + second_strict.cost_or_proceeds
            return self._row(
                signal,
                target_shares,
                first_leg,
                second_leg,
                strict_max_pair_cost,
                rescue_max_pair_cost,
                fresh_up,
                fresh_down,
                first,
                second_strict,
                FillResult(False, 0, 0, None),
                FillResult(False, 0, 0, None),
                final_action="BOTH_FILL_STRICT",
                refetch_latency_ms=latency_ms,
                reason=f"strict_second_limit={second_strict_limit:.4f}",
                final_pair_cost=pair_cost,
            )

        unwind = simulate_fok_sell(bids_by_leg[first_leg], target_shares)
        unwind_pnl = unwind.cost_or_proceeds - first.cost_or_proceeds if unwind.filled else None
        if unwind.filled and unwind_pnl is not None and unwind_pnl >= 0:
            return self._row(
                signal,
                target_shares,
                first_leg,
                second_leg,
                strict_max_pair_cost,
                rescue_max_pair_cost,
                fresh_up,
                fresh_down,
                first,
                second_strict,
                FillResult(False, 0, 0, None),
                unwind,
                final_action="UNWIND_PROFIT",
                refetch_latency_ms=latency_ms,
                reason=f"second_strict_failed; unwind_profitable pnl={unwind_pnl:.4f}",
            )

        rescue_limit = max(0.001, rescue_max_pair_cost - (first.avg_price or 0.0))
        second_rescue = simulate_fok_buy(asks_by_leg[second_leg], target_shares, rescue_limit)
        if second_rescue.filled:
            pair_cost = first.cost_or_proceeds + second_rescue.cost_or_proceeds
            return self._row(
                signal,
                target_shares,
                first_leg,
                second_leg,
                strict_max_pair_cost,
                rescue_max_pair_cost,
                fresh_up,
                fresh_down,
                first,
                second_strict,
                second_rescue,
                unwind,
                final_action="HEDGE_RESCUE",
                refetch_latency_ms=latency_ms,
                reason=f"second_strict_failed; rescue_limit={rescue_limit:.4f}",
                final_pair_cost=pair_cost,
            )

        if unwind.filled:
            return self._row(
                signal,
                target_shares,
                first_leg,
                second_leg,
                strict_max_pair_cost,
                rescue_max_pair_cost,
                fresh_up,
                fresh_down,
                first,
                second_strict,
                second_rescue,
                unwind,
                final_action="UNWIND_LOSS",
                refetch_latency_ms=latency_ms,
                reason=f"second_strict_and_rescue_failed; unwind_loss={unwind_pnl:.4f}",
            )

        return self._row(
            signal,
            target_shares,
            first_leg,
            second_leg,
            strict_max_pair_cost,
            rescue_max_pair_cost,
            fresh_up,
            fresh_down,
            first,
            second_strict,
            second_rescue,
            unwind,
            final_action="ONE_LEG_STUCK",
            refetch_latency_ms=latency_ms,
            reason="second_strict_failed; rescue_failed; unwind_failed",
        )

    def _refetch(self, token_id: str) -> OrderBook | None:
        try:
            return self.clob.get_order_book(token_id)
        except Exception:
            return None

    def _refetch_pair(self, up_token_id: str, down_token_id: str) -> tuple[OrderBook | None, OrderBook | None]:
        with ThreadPoolExecutor(max_workers=2) as executor:
            up_future = executor.submit(self._refetch, up_token_id)
            down_future = executor.submit(self._refetch, down_token_id)
            return up_future.result(), down_future.result()

    def _row(
        self,
        signal: ArbSignal,
        target_shares: float,
        first_leg: str,
        second_leg: str,
        strict_max_pair_cost: float,
        rescue_max_pair_cost: float,
        fresh_up: OrderBook | None,
        fresh_down: OrderBook | None,
        first: FillResult,
        second_strict: FillResult,
        second_rescue: FillResult,
        unwind: FillResult,
        *,
        final_action: str,
        refetch_latency_ms: int,
        reason: str,
        final_pair_cost: float | None = None,
    ) -> CrossMarketExecutionSim:
        final_pair_edge = None
        locked_profit = None
        if final_pair_cost is not None and target_shares > 0:
            final_pair_cost_per_share = final_pair_cost / target_shares
            final_pair_edge = 1.0 - final_pair_cost_per_share
            locked_profit = target_shares - final_pair_cost
        unwind_pnl = unwind.cost_or_proceeds - first.cost_or_proceeds if unwind.filled and first.filled else None
        return CrossMarketExecutionSim(
            simulated_at=utc_now(),
            signal_id=signal.signal_id,
            asset=signal.asset,
            end_ts=signal.end_ts,
            target_shares=target_shares,
            leg_order=f"{first_leg}_FIRST",
            initial_up_ask=signal.up_ask,
            initial_down_ask=signal.down_ask,
            initial_pair_cost=signal.top_cost,
            strict_max_pair_cost=strict_max_pair_cost,
            rescue_max_pair_cost=rescue_max_pair_cost,
            fresh_up_ask=fresh_up.best_ask if fresh_up else None,
            fresh_down_ask=fresh_down.best_ask if fresh_down else None,
            fresh_up_bid=fresh_up.best_bid if fresh_up else None,
            fresh_down_bid=fresh_down.best_bid if fresh_down else None,
            first_leg=first_leg,
            first_filled=first.filled,
            first_avg_price=first.avg_price,
            first_cost=first.cost_or_proceeds if first.filled else 0.0,
            second_leg=second_leg,
            second_strict_filled=second_strict.filled,
            second_strict_avg_price=second_strict.avg_price,
            second_strict_cost=second_strict.cost_or_proceeds if second_strict.filled else 0.0,
            second_rescue_filled=second_rescue.filled,
            second_rescue_avg_price=second_rescue.avg_price,
            second_rescue_cost=second_rescue.cost_or_proceeds if second_rescue.filled else 0.0,
            unwind_filled=unwind.filled,
            unwind_avg_price=unwind.avg_price,
            unwind_proceeds=unwind.cost_or_proceeds if unwind.filled else 0.0,
            unwind_pnl=unwind_pnl,
            final_action=final_action,
            final_pair_cost=final_pair_cost,
            final_pair_edge=final_pair_edge,
            locked_profit_estimate=locked_profit,
            unhedged_worst_case_loss=first.cost_or_proceeds if first.filled else 0.0,
            refetch_latency_ms=refetch_latency_ms,
            decision_reason=reason,
            up_ask_levels_json=levels_json(fresh_up.asks) if fresh_up else "[]",
            down_ask_levels_json=levels_json(fresh_down.asks) if fresh_down else "[]",
            up_bid_levels_json=levels_json(fresh_up.bids) if fresh_up else "[]",
            down_bid_levels_json=levels_json(fresh_down.bids) if fresh_down else "[]",
        )


def choose_first_leg(signal: ArbSignal) -> str:
    if signal.up_book_depth_shares_1pct <= signal.down_book_depth_shares_1pct:
        return "UP"
    return "DOWN"


def simulate_fok_buy(levels: list[BookLevel], shares: float, max_price: float) -> FillResult:
    return simulate_fok(levels, shares, max_price=max_price, side="BUY")


def simulate_fok_sell(levels: list[BookLevel], shares: float) -> FillResult:
    return simulate_fok(levels, shares, max_price=None, side="SELL")


def choose_dynamic_shares(
    max_shares: float,
    *,
    min_shares: float,
    step: float,
    up_asks: list[BookLevel],
    down_asks: list[BookLevel],
    max_pair_cost: float,
) -> float:
    if max_shares < min_shares:
        return 0.0
    candidate = step * int(max_shares // step)
    if candidate < min_shares:
        candidate = min_shares
    while candidate >= min_shares:
        if simulate_pair_buy(up_asks, down_asks, candidate, max_pair_cost).filled:
            return candidate
        candidate -= step
    return 0.0


def simulate_pair_buy(up_asks: list[BookLevel], down_asks: list[BookLevel], shares: float, max_pair_cost: float) -> FillResult:
    up = simulate_fok_buy(up_asks, shares, max_price=1.0)
    down = simulate_fok_buy(down_asks, shares, max_price=1.0)
    if not up.filled or not down.filled:
        return FillResult(False, min(up.shares, down.shares), up.cost_or_proceeds + down.cost_or_proceeds, None)
    pair_cost = up.cost_or_proceeds + down.cost_or_proceeds
    avg_pair_cost = pair_cost / shares if shares > 0 else None
    if avg_pair_cost is None or avg_pair_cost > max_pair_cost:
        return FillResult(False, shares, pair_cost, avg_pair_cost)
    return FillResult(True, shares, pair_cost, avg_pair_cost)


def simulate_fok(levels: list[BookLevel], shares: float, *, max_price: float | None, side: str) -> FillResult:
    if shares <= 0:
        return FillResult(False, 0.0, 0.0, None)
    remaining = shares
    notional = 0.0
    filled = 0.0
    for level in levels:
        if side == "BUY" and max_price is not None and level.price > max_price:
            break
        take = min(level.size, remaining)
        filled += take
        notional += take * level.price
        remaining -= take
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        return FillResult(False, filled, notional, (notional / filled if filled > 0 else None))
    return FillResult(True, filled, notional, notional / filled)
