from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
import asyncio
import logging
import time

from .clob import ClobClient
from .config import ScannerConfig
from .control_state import load_control_state
from .cross_livelike import write_cross_livelike_outputs
from .execution_sim import LOCKED_ACTIONS, CrossMarketExecutionSimulator
from .gamma import GammaClient
from .models import ArbSignal, CrossMarketMissedOpportunity, CrossMarketScanTiming, Market, OrderBook, PaperTrade
from .paper import PaperLedger
from .quote_stability import QuoteStabilityTracker
from .rtds import PriceState, run_rtds_feed
from .settlement import PaperSettler
from .storage import ScannerStore, append_csv, write_rows_csv
from .strategy import find_cross_window_arbs, grouped_by_asset_end, orient_markets
from .thresholds import ThresholdStore
from .utils import utc_now

LOGGER = logging.getLogger(__name__)


class CrossMarketScanner:
    def __init__(self, cfg: ScannerConfig):
        self.cfg = cfg
        self.gamma = GammaClient(cfg)
        self.clob = ClobClient(cfg)
        self.thresholds = ThresholdStore(cfg.threshold_path)
        self.store = ScannerStore(cfg.db_path)
        self.ledger = PaperLedger(self.store.existing_paper_trade_ids())
        self.settler = PaperSettler(self.gamma)
        self.execution_sim = CrossMarketExecutionSimulator(cfg, self.clob)
        self.quote_stability = QuoteStabilityTracker(
            window_seconds=cfg.cross_quote_stability_window_seconds,
            max_move=cfg.cross_quote_stability_max_move,
            min_samples=cfg.cross_quote_stability_min_samples,
        )
        self.markets_cache: list[Market] = []
        self.pair_token_ids_cache: set[str] = set()

    def close(self) -> None:
        self.clob.close()
        self.store.close()

    def scan_once(self, price_state: PriceState | None = None, paper: bool = True) -> tuple[list[ArbSignal], list[PaperTrade], list[Market]]:
        markets, timing = self.refresh_markets(price_state)
        return self.evaluate_markets(markets, price_state=price_state, paper=paper, mode="full", timing_preamble=timing, use_ws_only=False)

    def refresh_markets(self, price_state: PriceState | None = None) -> tuple[list[Market], dict[str, int]]:
        timing = {"gamma_ms": 0, "threshold_ms": 0, "store_ms": 0}
        markets_start = time.perf_counter()
        markets = self.gamma.discover_markets()
        timing["gamma_ms"] = elapsed_ms(markets_start)
        threshold_start = time.perf_counter()
        self.thresholds.reload()
        self.thresholds.apply(markets)
        if price_state:
            self.capture_thresholds(markets, price_state)
        self.capture_binance_thresholds(markets)
        self.thresholds.apply(markets)
        timing["threshold_ms"] = elapsed_ms(threshold_start)
        store_start = time.perf_counter()
        self.store.upsert_markets(markets, utc_now())
        timing["store_ms"] = elapsed_ms(store_start)
        self.clob.subscribe_many({m.tokens.up for m in markets} | {m.tokens.down for m in markets})
        self.markets_cache = markets
        self.pair_token_ids_cache = cross_pair_token_ids(markets, allow_price_inferred=self.cfg.allow_price_inferred_paper)
        if self.pair_token_ids_cache:
            self.clob.subscribe_many(self.pair_token_ids_cache)
        return markets, timing

    def evaluate_markets(
        self,
        markets: list[Market],
        *,
        price_state: PriceState | None = None,
        paper: bool = True,
        mode: str = "fast",
        timing_preamble: dict[str, int] | None = None,
        use_ws_only: bool = False,
    ) -> tuple[list[ArbSignal], list[PaperTrade], list[Market]]:
        timing_preamble = timing_preamble or {}
        total_start = time.perf_counter()
        book_start = time.perf_counter()
        books, book_stats = self.fetch_books_with_stats(markets, use_ws_only=use_ws_only)
        book_ms = elapsed_ms(book_start)
        self.quote_stability.update_many(books)
        unstable_token_ids = set()
        if self.cfg.cross_quote_stability_max_move > 0:
            unstable_token_ids = self.quote_stability.unstable_tokens()
        threshold_ms = timing_preamble.get("threshold_ms", 0)
        if price_state:
            threshold_start = time.perf_counter()
            self.capture_thresholds(markets, price_state)
            self.thresholds.apply(markets)
            threshold_ms += elapsed_ms(threshold_start)
        strategy_start = time.perf_counter()
        signals = find_cross_window_arbs(
            markets,
            books,
            min_edge=self.cfg.min_edge,
            max_shares=self.cfg.max_paper_shares,
            min_shares=self.cfg.min_shares,
            taker_fee_rate=self.cfg.taker_fee_rate,
            min_net_edge=self.cfg.cross_min_net_edge,
            min_seconds_to_expiry=self.cfg.min_seconds_to_expiry,
            max_orderbook_age_seconds=self.cfg.max_orderbook_age_seconds,
            allow_price_inferred=self.cfg.allow_price_inferred_paper,
            unstable_token_ids=unstable_token_ids,
        )
        strategy_ms = elapsed_ms(strategy_start)
        pair_coverage = pair_fresh_coverage(
            self.clob,
            markets,
            allow_price_inferred=self.cfg.allow_price_inferred_paper,
        )
        self.store.insert_signals(signals)
        append_csv(self.cfg.signals_csv, signals)
        execution_sims = []
        missed: list[CrossMarketMissedOpportunity] = []
        execution_ms = 0
        trading_enabled = load_control_state(self.cfg.control_state_path).trading_enabled
        if self.cfg.cross_execution_sim_enabled:
            execution_start = time.perf_counter()
            execution_sims = self.execution_sim.simulate_many(signals, books)
            execution_ms = elapsed_ms(execution_start)
            append_csv(self.cfg.cross_execution_sim_csv, execution_sims)
            missed = missed_opportunities(signals, execution_sims)
            append_csv(self.cfg.cross_missed_opportunities_csv, missed)
        trades: list[PaperTrade] = []
        settlements = []
        paper_ms = 0
        settlement_ms = 0
        if paper:
            paper_start = time.perf_counter()
            paper_signals = signals
            if not trading_enabled:
                paper_signals = []
            if self.cfg.cross_execution_sim_enabled and self.cfg.cross_execution_paper_requires_locked:
                locked_signal_ids = {row.signal_id for row in execution_sims if row.final_action in LOCKED_ACTIONS}
                paper_signals = [signal for signal in signals if signal.signal_id in locked_signal_ids]
                if not trading_enabled:
                    paper_signals = []
            trades = self.ledger.open_from_signals(paper_signals, self.cfg.max_paper_shares)
            self.store.insert_paper_trades(trades)
            append_csv(self.cfg.paper_csv, trades)
            paper_ms = elapsed_ms(paper_start)
            settlement_start = time.perf_counter()
            settlements = self.settle_due_paper()
            settlement_ms = elapsed_ms(settlement_start)
            if settlements:
                write_rows_csv(self.cfg.paper_settlements_csv, [asdict(row) for row in self.store.all_paper_settlements()])
        if self.cfg.cross_livelike_report_enabled and (execution_sims or trades or (paper and settlements)):
            write_cross_livelike_outputs(self.cfg)
        total_ms = elapsed_ms(total_start) + timing_preamble.get("gamma_ms", 0) + timing_preamble.get("store_ms", 0)
        timing_row = CrossMarketScanTiming(
            scanned_at=utc_now(),
            mode=mode,
            markets=len(markets),
            books=len(books),
            ws_books=book_stats.get("ws", 0),
            rest_books=book_stats.get("rest", 0),
            other_books=book_stats.get("bridge", 0) + book_stats.get("bridge_server", 0),
            missing_books=book_stats.get("missing", 0),
            signals=len(signals),
            execution_sims=len(execution_sims),
            missed_opportunities=len(missed),
            paper_trades=len(trades),
            gamma_ms=timing_preamble.get("gamma_ms", 0),
            threshold_ms=threshold_ms,
            store_ms=timing_preamble.get("store_ms", 0),
            book_ms=book_ms,
            strategy_ms=strategy_ms,
            execution_ms=execution_ms,
            paper_ms=paper_ms,
            settlement_ms=settlement_ms,
            total_ms=total_ms,
            pair_candidates=pair_coverage.get("pair_candidates", 0),
            pair_fresh=pair_coverage.get("pair_fresh", 0),
            pair_missing_up=pair_coverage.get("pair_missing_up", 0),
            pair_missing_down=pair_coverage.get("pair_missing_down", 0),
            pair_fresh_pct=pair_coverage.get("pair_fresh_pct", 0.0),
            notes="ws_only_books" if use_ws_only else "ws_with_fallback",
        )
        if not trading_enabled:
            timing_row.notes += "|trading_paused"
        append_csv(self.cfg.cross_scan_timing_csv, [timing_row])
        LOGGER.info(
            "Scan[%s]: %s markets, %s books (%s ws, %s rest, %s missing), %s signals, %s execution sims, %s missed, %s paper trades; timing gamma=%sms books=%sms strategy=%sms exec=%sms total=%sms",
            mode,
            len(markets),
            len(books),
            book_stats.get("ws", 0),
            book_stats.get("rest", 0),
            book_stats.get("missing", 0),
            len(signals),
            len(execution_sims),
            len(missed),
            len(trades),
            timing_row.gamma_ms,
            timing_row.book_ms,
            timing_row.strategy_ms,
            timing_row.execution_ms,
            timing_row.total_ms,
        )
        return signals, trades, markets

    def settle_due_paper(self):
        now_ts = int(datetime.now(timezone.utc).timestamp())
        records = self.store.open_paper_trade_records(now_ts, self.cfg.paper_settlement_grace_seconds)
        settlements = self.settler.settle_records(records)
        self.store.insert_paper_settlements(settlements)
        if settlements:
            settled = sum(1 for row in settlements if row.status == "SETTLED")
            LOGGER.info("Paper settlements: %s settled, %s pending/error", settled, len(settlements) - settled)
        return settlements

    def resettle_all_paper(self, *, limit: int | None = None):
        records = self.store.all_paper_trade_records(limit=limit)
        settlements = self.settler.settle_records(records)
        self.store.insert_paper_settlements(settlements)
        if settlements:
            write_rows_csv(self.cfg.paper_settlements_csv, [asdict(row) for row in self.store.all_paper_settlements()])
            if self.cfg.cross_livelike_report_enabled:
                write_cross_livelike_outputs(self.cfg)
            settled = sum(1 for row in settlements if row.status == "SETTLED")
            LOGGER.info("Paper resettlement: %s settled, %s pending/error", settled, len(settlements) - settled)
        return settlements

    def fetch_books(self, markets: list[Market]) -> dict[str, OrderBook]:
        books, _stats = self.fetch_books_with_stats(markets, use_ws_only=False)
        return books

    def fetch_books_with_stats(self, markets: list[Market], *, use_ws_only: bool) -> tuple[dict[str, OrderBook], Counter]:
        books: dict[str, OrderBook] = {}
        stats: Counter = Counter()
        token_ids = sorted({m.tokens.up for m in markets} | {m.tokens.down for m in markets})
        self.clob.subscribe_many(token_ids)
        workers = max(1, min(self.cfg.clob_book_workers, len(token_ids) or 1))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            fetch = self.clob.get_order_book_ws_only if use_ws_only else self.clob.get_order_book_with_source
            futures = {executor.submit(fetch, token_id): token_id for token_id in token_ids}
            for future in as_completed(futures):
                token_id = futures[future]
                try:
                    book, source = future.result()
                except Exception as exc:
                    LOGGER.debug("Book fetch failed for %s: %s", token_id[:16], exc)
                    stats["missing"] += 1
                    continue
                stats[source] += 1
                if book:
                    books[token_id] = book
        return books, stats

    def warm_pair_books(self, markets: list[Market] | None = None) -> Counter:
        if not self.cfg.cross_book_warm_enabled:
            return Counter()
        token_ids = self.pair_token_ids_cache
        if markets is not None:
            token_ids = cross_pair_token_ids(markets, allow_price_inferred=self.cfg.allow_price_inferred_paper)
            self.pair_token_ids_cache = token_ids
        return warm_stale_books(
            self.clob,
            token_ids,
            stale_after_seconds=self.cfg.cross_book_warm_stale_after_seconds,
            max_tokens=self.cfg.cross_book_warm_max_tokens_per_cycle,
            workers=self.cfg.clob_book_workers,
        )

    def capture_thresholds(self, markets: list[Market], price_state: PriceState) -> None:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        for market in markets:
            if market.threshold_price is not None:
                continue
            if not (market.start_ts <= now_ts <= market.start_ts + market.duration_sec):
                continue
            tick = price_state.snapshot_after_start(
                market.asset,
                market.start_ts,
                max_after_seconds=self.cfg.threshold_capture_max_after_seconds,
                max_before_seconds=self.cfg.threshold_capture_max_before_seconds,
            )
            if not tick:
                continue
            self.thresholds.set_threshold(market, tick.price, "rtds_live_start_window", tick_ts_ms=tick.ts_ms)
            market.threshold_price = tick.price
            market.threshold_source = "rtds_live_start_window"
            LOGGER.info("Captured threshold %s %s = %.4f", market.asset, market.slug, tick.price)

    def capture_binance_thresholds(self, markets: list[Market]) -> None:
        from .binance import hourly_open_at
        from .thresholds import is_hourly_slug

        for market in markets:
            if not is_hourly_slug(market.slug):
                continue
            if self.thresholds.has_binance_threshold(market):
                continue
            price = hourly_open_at(market.asset, market.start_ts)
            if price is None or price <= 0:
                continue
            self.thresholds.set_binance_threshold(market, price)
            LOGGER.info(
                "Captured Binance hourly threshold %s %s start_ts=%s = %.4f",
                market.asset,
                market.slug,
                market.start_ts,
                price,
            )


async def run_scanner_loop(cfg: ScannerConfig, *, seconds: int, poll_seconds: float, paper: bool, use_rtds: bool) -> None:
    scanner = CrossMarketScanner(cfg)
    stop = asyncio.Event()
    price_state = PriceState()
    rtds_task = None
    if use_rtds:
        rtds_task = asyncio.create_task(run_rtds_feed(cfg, price_state, stop))
        await asyncio.sleep(1.0)
    deadline = asyncio.get_event_loop().time() + seconds if seconds > 0 else None
    try:
        if cfg.cross_fast_loop_enabled:
            next_discovery_at = 0.0
            next_warm_at = 0.0
            while True:
                now_loop = asyncio.get_event_loop().time()
                discovery_timing = None
                did_discovery = False
                if not scanner.markets_cache or now_loop >= next_discovery_at:
                    markets, discovery_timing = await asyncio.to_thread(scanner.refresh_markets, price_state if use_rtds else None)
                    did_discovery = True
                    next_discovery_at = now_loop + cfg.cross_discovery_interval_seconds
                    next_warm_at = 0.0
                else:
                    markets = scanner.markets_cache
                if cfg.cross_book_warm_enabled and now_loop >= next_warm_at:
                    warm_stats = await asyncio.to_thread(scanner.warm_pair_books, markets)
                    if warm_stats:
                        LOGGER.info(
                            "Book warm: targets=%s refreshed=%s rest=%s bridge=%s missing=%s skipped_fresh=%s interval=%ss",
                            warm_stats.get("targets", 0),
                            warm_stats.get("refreshed", 0),
                            warm_stats.get("rest", 0),
                            warm_stats.get("bridge", 0) + warm_stats.get("bridge_server", 0),
                            warm_stats.get("missing", 0),
                            warm_stats.get("skipped_fresh", 0),
                            next_warm_interval_seconds(cfg, markets),
                        )
                    next_warm_at = now_loop + next_warm_interval_seconds(cfg, markets)
                await asyncio.to_thread(
                    scanner.evaluate_markets,
                    markets,
                    price_state=price_state if use_rtds else None,
                    paper=paper,
                    mode="discovery+eval" if did_discovery else "fast",
                    timing_preamble=discovery_timing,
                    use_ws_only=bool(cfg.cross_fast_ws_only_books),
                )
                if deadline is not None and asyncio.get_event_loop().time() >= deadline:
                    break
                await asyncio.sleep(cfg.cross_eval_interval_seconds)
            return
        while True:
            await asyncio.to_thread(scanner.scan_once, price_state=price_state if use_rtds else None, paper=paper)
            if deadline is not None and asyncio.get_event_loop().time() >= deadline:
                break
            await asyncio.sleep(poll_seconds)
    finally:
        stop.set()
        if rtds_task:
            rtds_task.cancel()
        scanner.close()


def summarize_signals(signals: list[ArbSignal], trades: list[PaperTrade]) -> str:
    lines = [
        f"signals={len(signals)} paper_trades={len(trades)}",
    ]
    for signal in signals[:10]:
        lines.append(
            f"{signal.asset.upper()} edge={signal.avg_edge:.4f} shares={signal.executable_shares:.2f} "
            f"{signal.long_up_slug} UP + {signal.long_down_slug} DOWN source={signal.orientation_source}"
        )
    return "\n".join(lines)


def cross_pair_token_ids(markets: list[Market], *, allow_price_inferred: bool) -> set[str]:
    token_ids: set[str] = set()
    for lower_up, higher_down in cross_pair_token_pairs(markets, allow_price_inferred=allow_price_inferred):
        token_ids.add(lower_up)
        token_ids.add(higher_down)
    return token_ids


def cross_pair_token_pairs(markets: list[Market], *, allow_price_inferred: bool) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for group in grouped_by_asset_end(markets).values():
        if len(group) < 2:
            continue
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                lower, higher, source = orient_markets(a, b)
                if source == "unknown":
                    continue
                if source == "price_inferred" and not allow_price_inferred:
                    continue
                pairs.append((lower.tokens.up, higher.tokens.down))
    return pairs


def pair_fresh_coverage(clob: ClobClient, markets: list[Market], *, allow_price_inferred: bool) -> Counter:
    stats: Counter = Counter()
    pairs = cross_pair_token_pairs(markets, allow_price_inferred=allow_price_inferred)
    stats["pair_candidates"] = len(pairs)
    for up_token_id, down_token_id in pairs:
        up_book = clob.get_order_book_from_ws(up_token_id)
        down_book = clob.get_order_book_from_ws(down_token_id)
        if up_book and down_book:
            stats["pair_fresh"] += 1
        else:
            if not up_book:
                stats["pair_missing_up"] += 1
            if not down_book:
                stats["pair_missing_down"] += 1
    stats["pair_fresh_pct"] = (stats["pair_fresh"] / stats["pair_candidates"]) if stats["pair_candidates"] else 0.0
    return stats


def next_warm_interval_seconds(cfg: ScannerConfig, markets: list[Market]) -> float:
    near = any(
        0 < market.seconds_to_expiry <= cfg.cross_book_warm_near_expiry_seconds
        for market in markets
        if market.is_active_now
    )
    if near:
        return cfg.cross_book_warm_near_interval_seconds
    return cfg.cross_book_warm_interval_seconds


def warm_stale_books(
    clob: ClobClient,
    token_ids: set[str] | list[str],
    *,
    stale_after_seconds: float,
    max_tokens: int,
    workers: int,
    force_refresh: bool = False,
) -> Counter:
    stats: Counter = Counter()
    unique = sorted({str(token_id) for token_id in token_ids if token_id})
    stats["targets"] = len(unique)
    stale: list[str] = []
    for token_id in unique:
        age = clob.ws_book_age_seconds(token_id)
        book = clob.get_order_book_from_ws(token_id)
        if force_refresh or book is None or age is None or age >= stale_after_seconds:
            stale.append(token_id)
        else:
            stats["skipped_fresh"] += 1
    if max_tokens > 0:
        stale = stale[:max_tokens]
    if not stale:
        return stats
    stats["refresh_candidates"] = len(stale)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(stale)))) as executor:
        futures = {executor.submit(clob.refresh_order_book_cache, token_id): token_id for token_id in stale}
        for future in as_completed(futures):
            try:
                book, source = future.result()
            except Exception:
                stats["missing"] += 1
                continue
            stats[source] += 1
            if book:
                stats["refreshed"] += 1
    return stats


def missed_opportunities(signals: list[ArbSignal], execution_sims) -> list[CrossMarketMissedOpportunity]:
    by_id = {signal.signal_id: signal for signal in signals}
    rows: list[CrossMarketMissedOpportunity] = []
    for sim in execution_sims:
        if sim.final_action in LOCKED_ACTIONS:
            continue
        signal = by_id.get(sim.signal_id)
        if not signal:
            continue
        fresh_pair = None
        pair_move = None
        if sim.fresh_up_ask is not None and sim.fresh_down_ask is not None:
            fresh_pair = sim.fresh_up_ask + sim.fresh_down_ask
            pair_move = fresh_pair - sim.initial_pair_cost
        rows.append(
            CrossMarketMissedOpportunity(
                detected_at=signal.detected_at,
                simulated_at=sim.simulated_at,
                signal_id=sim.signal_id,
                asset=signal.asset,
                end_ts=signal.end_ts,
                final_action=sim.final_action,
                miss_category=miss_category(sim.final_action, pair_move, fresh_pair),
                decision_reason=sim.decision_reason,
                initial_pair_cost=sim.initial_pair_cost,
                fresh_pair_cost=fresh_pair,
                pair_cost_move=pair_move,
                top_edge=signal.top_edge,
                avg_edge=signal.avg_edge,
                net_avg_edge=signal.net_avg_edge,
                executable_shares=signal.executable_shares,
                target_shares=sim.target_shares,
                refetch_latency_ms=sim.refetch_latency_ms,
                up_ask=signal.up_ask,
                down_ask=signal.down_ask,
                fresh_up_ask=sim.fresh_up_ask,
                fresh_down_ask=sim.fresh_down_ask,
                notes=miss_notes(sim.final_action, pair_move, fresh_pair),
            )
        )
    return rows


def miss_category(final_action: str, pair_move: float | None, fresh_pair: float | None) -> str:
    if final_action == "NO_DYNAMIC_SIZE_FILL":
        if fresh_pair is None:
            return "missing_fresh_book"
        return "strict_pair_cost_or_depth_failed"
    if final_action == "NO_FIRST_LEG_FILL":
        if pair_move is not None and pair_move >= 0.03:
            return "stale_quote_pair_moved"
        return "first_leg_depth_or_limit_failed"
    if final_action == "UNWIND_LOSS":
        return "second_leg_failed_unwind_would_lose"
    if final_action == "HEDGE_RESCUE":
        return "rescue_required_not_normal_entry"
    if final_action == "UNWIND_PROFIT":
        return "single_leg_unwind_profit_not_locked"
    if final_action == "ONE_LEG_STUCK":
        return "one_leg_stuck"
    return "not_locked"


def miss_notes(final_action: str, pair_move: float | None, fresh_pair: float | None) -> str:
    if fresh_pair is None:
        return "Fresh pair cost unavailable, usually because one book was missing on refetch."
    if pair_move is not None and pair_move >= 0.03:
        return "Fresh pair cost moved materially higher before simulated execution."
    if final_action == "UNWIND_LOSS":
        return "The first leg could fill, but hedge/rescue failed and immediate unwind was negative."
    return "Execution simulator did not produce a normal locked two-leg entry."


def elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
