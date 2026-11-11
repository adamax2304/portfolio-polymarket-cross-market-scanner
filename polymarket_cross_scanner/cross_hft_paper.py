from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import combinations
import asyncio
import logging
import time

from .clob import ClobClient
from .config import ScannerConfig
from .control_state import load_control_state
from .cross_livelike import write_cross_livelike_outputs
from .cross_live_executor import CrossMarketLiveExecutor
from .execution_sim import CrossMarketExecutionSimulator
from .gamma import GammaClient
from .models import ArbSignal, CrossHftPairEvaluation, CrossHftPaperEvent, CrossHftPaperHeartbeat, Market, OrderBook
from .oracle_divergence import OracleDivergenceTracker
from .rtds import PriceState, run_rtds_feed
from .scanner import elapsed_ms, missed_opportunities, next_warm_interval_seconds, warm_stale_books
from .storage import append_csv
from .strategy import (
    depth_shares_within,
    depth_usdc_within,
    executable_pair_size,
    find_cross_window_arbs,
    grouped_by_asset_end,
    is_book_fresh,
    levels_json,
    notes_for,
    oriented_market_pairs,
)
from .thresholds import ThresholdStore
from .utils import utc_now

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class HftPair:
    pair_id: str
    asset: str
    end_ts: int
    lower: Market
    higher: Market
    orientation_source: str
    up_token_id: str
    down_token_id: str


class CrossHftPaperScanner:
    def __init__(self, cfg: ScannerConfig, *, signal_cooldown_ms: int = 250):
        self.cfg = cfg
        self.gamma = GammaClient(cfg)
        self.clob = ClobClient(cfg)
        self.thresholds = ThresholdStore(cfg.threshold_path)
        self.execution_sim = CrossMarketExecutionSimulator(cfg, self.clob)
        self.live_executor = CrossMarketLiveExecutor(cfg) if cfg.cross_live_enabled else None
        self.oracle_tracker: OracleDivergenceTracker | None = None
        self.signal_cooldown_ms = max(0, signal_cooldown_ms)
        self.markets: list[Market] = []
        self.pairs_by_id: dict[str, HftPair] = {}
        self.token_to_pair_ids: dict[str, set[str]] = {}
        self.last_signal_ms_by_pair: dict[str, int] = {}
        self.pending_events: list[CrossHftPaperEvent] = []
        self.pending_pair_evals: list[CrossHftPairEvaluation] = []
        self.pending_sims = []
        self.pending_missed = []
        self.counters = {
            "updates_drained": 0,
            "unique_tokens_updated": 0,
            "pairs_evaluated": 0,
            "opportunities": 0,
            "execution_sims": 0,
            "missed_opportunities": 0,
            "suppressed_by_cooldown": 0,
            "redemptions": 0,
            "reject_expired": 0,
            "reject_missing_book": 0,
            "reject_stale_book": 0,
            "reject_min_shares": 0,
            "reject_min_net_edge": 0,
        }
        self.last_gamma_ms = 0

    def close(self) -> None:
        self.flush()
        self.clob.close()

    def refresh_markets(self, price_state: PriceState | None = None) -> None:
        started = time.perf_counter()
        markets = self.gamma.discover_markets()
        self.last_gamma_ms = elapsed_ms(started)
        self.thresholds.reload()
        self.thresholds.apply(markets)
        if price_state:
            self.capture_thresholds(markets, price_state)
        self.capture_binance_thresholds(markets)
        self.thresholds.apply(markets)

        pairs = build_hft_pairs(markets, allow_price_inferred=self.cfg.allow_price_inferred_paper)
        token_to_pair_ids: dict[str, set[str]] = {}
        for pair in pairs:
            token_to_pair_ids.setdefault(pair.up_token_id, set()).add(pair.pair_id)
            token_to_pair_ids.setdefault(pair.down_token_id, set()).add(pair.pair_id)

        self.markets = markets
        self.pairs_by_id = {pair.pair_id: pair for pair in pairs}
        self.token_to_pair_ids = token_to_pair_ids
        self.clob.subscribe_many(set(token_to_pair_ids))
        LOGGER.info(
            "HFT paper discovery: markets=%s pairs=%s subscribed_tokens=%s gamma=%sms",
            len(markets),
            len(pairs),
            len(token_to_pair_ids),
            self.last_gamma_ms,
        )

    def process_updates(self, update_events: list[tuple[str, str]]) -> None:
        if not update_events:
            return
        unique_tokens = {token_id for token_id, _source in update_events}
        sources = {source for _token_id, source in update_events}
        pair_ids: set[str] = set()
        for token_id in unique_tokens:
            pair_ids.update(self.token_to_pair_ids.get(token_id, set()))
        self.counters["updates_drained"] += len(update_events)
        self.counters["unique_tokens_updated"] += len(unique_tokens)
        if not pair_ids:
            return

        trigger_token_id = sorted(unique_tokens)[0]
        trigger_source = "warm_snapshot" if any(source.startswith("warm_") for source in sources) else "ws_update"
        self.process_pair_ids(pair_ids, trigger_token_id=trigger_token_id, trigger_source=trigger_source)

    def process_pair_ids(self, pair_ids: set[str], *, trigger_token_id: str, trigger_source: str) -> None:
        if not load_control_state(self.cfg.control_state_path).trading_enabled:
            return
        for pair_id in sorted(pair_ids):
            pair = self.pairs_by_id.get(pair_id)
            if not pair:
                continue
            self.counters["pairs_evaluated"] += 1
            evaluated = self.evaluate_pair(pair, trigger_token_id=trigger_token_id, trigger_source=trigger_source)
            if not evaluated:
                continue
            event, signal, books = evaluated
            now_ms = int(time.time() * 1000)
            last_ms = self.last_signal_ms_by_pair.get(pair.pair_id, 0)
            if self.signal_cooldown_ms and now_ms - last_ms < self.signal_cooldown_ms:
                self.counters["suppressed_by_cooldown"] += 1
                continue
            self.last_signal_ms_by_pair[pair.pair_id] = now_ms
            self.pending_events.append(event)
            self.counters["opportunities"] += 1
            if self.cfg.cross_execution_sim_enabled:
                sim = self.execution_sim.simulate(signal, books)
                self.pending_sims.append(sim)
                self.counters["execution_sims"] += 1
                if self.live_executor:
                    live_order = self.live_executor.maybe_execute(signal, sim)
                    if live_order:
                        LOGGER.info(
                            "Cross live order: result=%s asset=%s shares=%s action=%s notes=%s",
                            live_order.live_result,
                            live_order.asset,
                            live_order.shares,
                            live_order.final_action,
                            live_order.notes,
                        )
                missed = missed_opportunities([signal], [sim])
                self.pending_missed.extend(missed)
                self.counters["missed_opportunities"] += len(missed)

    def auto_redeem_due(self) -> None:
        if not self.live_executor:
            return
        rows = self.live_executor.auto_redeem_due()
        self.counters["redemptions"] += len(rows)
        for row in rows:
            LOGGER.info(
                "Cross live redemption: result=%s command=%s slug=%s tx=%s notes=%s",
                row.redeem_result,
                row.command,
                row.market_slug,
                row.tx_hash,
                row.notes,
            )

    def sweep_all_pairs(self) -> None:
        self.process_pair_ids(set(self.pairs_by_id), trigger_token_id="periodic_sweep", trigger_source="periodic_sweep")

    def warm_pair_books(self) -> dict:
        stats = warm_stale_books(
            self.clob,
            set(self.token_to_pair_ids),
            stale_after_seconds=self.cfg.cross_book_warm_stale_after_seconds,
            max_tokens=self.cfg.cross_book_warm_max_tokens_per_cycle,
            workers=self.cfg.clob_book_workers,
            force_refresh=self.cfg.cross_live_enabled,
        )
        return dict(stats)

    def evaluate_pair(self, pair: HftPair, *, trigger_token_id: str, trigger_source: str = "manual") -> tuple[CrossHftPaperEvent, ArbSignal, dict[str, OrderBook]] | None:
        started = time.perf_counter()
        if pair.lower.seconds_to_expiry < self.cfg.min_seconds_to_expiry:
            self.record_pair_eval(pair, trigger_token_id, trigger_source, started, "expired")
            return None
        up_book = self.clob.get_order_book_from_ws(pair.up_token_id)
        down_book = self.clob.get_order_book_from_ws(pair.down_token_id)
        if not up_book or not down_book or up_book.best_ask is None or down_book.best_ask is None:
            self.record_pair_eval(pair, trigger_token_id, trigger_source, started, "missing_book", up_book=up_book, down_book=down_book)
            return None
        if not is_book_fresh(up_book, self.cfg.max_orderbook_age_seconds) or not is_book_fresh(down_book, self.cfg.max_orderbook_age_seconds):
            self.record_pair_eval(pair, trigger_token_id, trigger_source, started, "stale_book", up_book=up_book, down_book=down_book)
            return None

        top_cost = up_book.best_ask + down_book.best_ask
        books = {pair.up_token_id: up_book, pair.down_token_id: down_book}
        signals = find_cross_window_arbs(
            [pair.lower, pair.higher],
            books,
            min_edge=self.cfg.min_edge,
            max_shares=self.cfg.max_paper_shares,
            min_shares=self.cfg.min_shares,
            taker_fee_rate=self.cfg.taker_fee_rate,
            min_net_edge=self.cfg.cross_min_net_edge,
            min_seconds_to_expiry=self.cfg.min_seconds_to_expiry,
            max_orderbook_age_seconds=self.cfg.max_orderbook_age_seconds,
            allow_price_inferred=self.cfg.allow_price_inferred_paper,
        )
        signal = next(
            (
                candidate
                for candidate in signals
                if candidate.up_token_id == pair.up_token_id
                and candidate.down_token_id == pair.down_token_id
                and candidate.lower_market_slug == pair.lower.slug
                and candidate.higher_market_slug == pair.higher.slug
            ),
            None,
        )
        if not signal:
            self.record_rejected_priced_pair(pair, trigger_token_id, trigger_source, started, up_book, down_book, top_cost)
            return None

        detected_at = utc_now()
        signal.detected_at = detected_at
        event = CrossHftPaperEvent(
            detected_at=detected_at,
            trigger_source=trigger_source,
            trigger_token_id=trigger_token_id,
            pair_id=pair.pair_id,
            signal_id=signal.signal_id,
            asset=pair.asset,
            end_ts=pair.end_ts,
            seconds_to_expiry=pair.lower.seconds_to_expiry,
            lower_market_slug=pair.lower.slug,
            higher_market_slug=pair.higher.slug,
            lower_threshold=pair.lower.threshold_price,
            higher_threshold=pair.higher.threshold_price,
            orientation_source=pair.orientation_source,
            up_token_id=pair.up_token_id,
            down_token_id=pair.down_token_id,
            up_book_age_ms=book_age_ms(up_book),
            down_book_age_ms=book_age_ms(down_book),
            up_ask=up_book.best_ask,
            down_ask=down_book.best_ask,
            top_cost=signal.top_cost,
            top_edge=signal.top_edge,
            executable_shares=signal.executable_shares,
            avg_cost=signal.avg_cost,
            avg_edge=signal.avg_edge,
            estimated_taker_fee_per_share=signal.estimated_taker_fee_per_share,
            net_avg_edge=signal.net_avg_edge,
            up_book_depth_shares_1pct=signal.up_book_depth_shares_1pct,
            down_book_depth_shares_1pct=signal.down_book_depth_shares_1pct,
            up_book_depth_usdc_1pct=signal.up_book_depth_usdc_1pct,
            down_book_depth_usdc_1pct=signal.down_book_depth_usdc_1pct,
            eval_latency_ms=elapsed_ms(started),
            up_ask_levels_json=signal.up_ask_levels_json,
            down_ask_levels_json=signal.down_ask_levels_json,
            notes="ws_update_triggered",
        )
        self.record_pair_eval(
            pair,
            trigger_token_id,
            trigger_source,
            started,
            "accepted",
            up_book=up_book,
            down_book=down_book,
            top_cost=signal.top_cost,
            executable_shares=signal.executable_shares,
            avg_cost=signal.avg_cost,
            fee_per_share=signal.estimated_taker_fee_per_share,
            net_avg_edge=signal.net_avg_edge,
            force=True,
        )
        return event, signal, books

    def record_rejected_priced_pair(
        self,
        pair: HftPair,
        trigger_token_id: str,
        trigger_source: str,
        started: float,
        up_book: OrderBook,
        down_book: OrderBook,
        top_cost: float,
    ) -> None:
        shares, total_cost, total_fee = executable_pair_size(
            up_book.asks,
            down_book.asks,
            max_cost_per_share=1 - self.cfg.min_edge,
            max_shares=self.cfg.max_paper_shares,
            taker_fee_rate=self.cfg.taker_fee_rate,
        )
        avg_cost = total_cost / shares if shares else None
        fee_per_share = total_fee / shares if shares else None
        net_avg_edge = 1 - avg_cost - (fee_per_share or 0.0) if avg_cost is not None else None
        if shares < self.cfg.min_shares:
            reason = "min_shares"
        elif net_avg_edge is not None and net_avg_edge < self.cfg.cross_min_net_edge:
            reason = "min_net_edge"
        else:
            reason = "strategy_rejected"
        self.record_pair_eval(
            pair,
            trigger_token_id,
            trigger_source,
            started,
            reason,
            up_book=up_book,
            down_book=down_book,
            top_cost=top_cost,
            executable_shares=shares,
            avg_cost=avg_cost,
            fee_per_share=fee_per_share,
            net_avg_edge=net_avg_edge,
        )

    def record_pair_eval(
        self,
        pair: HftPair,
        trigger_token_id: str,
        trigger_source: str,
        started: float,
        reject_reason: str,
        *,
        up_book: OrderBook | None = None,
        down_book: OrderBook | None = None,
        top_cost: float | None = None,
        executable_shares: float = 0.0,
        avg_cost: float | None = None,
        fee_per_share: float | None = None,
        net_avg_edge: float | None = None,
        force: bool = False,
    ) -> None:
        counter_key = f"reject_{reject_reason}"
        if counter_key in self.counters:
            self.counters[counter_key] += 1
        interesting = force or reject_reason != "min_net_edge" or (
            top_cost is not None and top_cost <= 1.03
        )
        if not interesting:
            return
        self.pending_pair_evals.append(
            CrossHftPairEvaluation(
                evaluated_at=utc_now(),
                trigger_source=trigger_source,
                trigger_token_id=trigger_token_id,
                pair_id=pair.pair_id,
                asset=pair.asset,
                end_ts=pair.end_ts,
                lower_market_slug=pair.lower.slug,
                higher_market_slug=pair.higher.slug,
                orientation_source=pair.orientation_source,
                up_token_id=pair.up_token_id,
                down_token_id=pair.down_token_id,
                up_book_age_ms=book_age_ms(up_book) if up_book else -1,
                down_book_age_ms=book_age_ms(down_book) if down_book else -1,
                up_ask=up_book.best_ask if up_book else None,
                down_ask=down_book.best_ask if down_book else None,
                top_cost=top_cost,
                executable_shares=executable_shares,
                avg_cost=avg_cost,
                estimated_taker_fee_per_share=fee_per_share,
                net_avg_edge=net_avg_edge,
                reject_reason=reject_reason,
                eval_latency_ms=elapsed_ms(started),
                notes="shared_strategy_eval",
            )
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
        """Fetch Binance USDT 1H open for each active hourly market (`*-up-or-down-*`).
        These markets resolve from Binance USDT 1H candles, so this is the *correct*
        threshold for orientation and edge calculation. Best-effort, non-blocking-ish."""

        from .binance import hourly_open_at
        from .thresholds import is_hourly_slug

        for market in markets:
            if not is_hourly_slug(market.slug):
                continue
            if self.thresholds.has_binance_threshold(market):
                continue
            # Hourly markets always start on an exact hour boundary.
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

    def flush(self) -> int:
        started = time.perf_counter()
        append_csv(self.cfg.cross_hft_paper_events_csv, self.pending_events)
        append_csv(self.cfg.cross_hft_pair_evaluations_csv, self.pending_pair_evals)
        append_csv(self.cfg.cross_hft_paper_execution_csv, self.pending_sims)
        append_csv(self.cfg.cross_hft_paper_missed_csv, self.pending_missed)
        wrote = len(self.pending_events) + len(self.pending_pair_evals) + len(self.pending_sims) + len(self.pending_missed)
        self.pending_events = []
        self.pending_pair_evals = []
        self.pending_sims = []
        self.pending_missed = []
        if wrote and self.cfg.cross_livelike_report_enabled:
            write_cross_livelike_outputs(self.cfg)
        return elapsed_ms(started) if wrote else 0

    def heartbeat(self, *, loop_ms: int, flush_ms: int) -> CrossHftPaperHeartbeat:
        coverage = self.pair_fresh_coverage()
        row = CrossHftPaperHeartbeat(
            ts=utc_now(),
            mode="cross-hft-live" if self.cfg.cross_live_enabled else "cross-hft-paper",
            markets=len(self.markets),
            pairs=len(self.pairs_by_id),
            subscribed_tokens=len(self.token_to_pair_ids),
            updates_drained=self.counters["updates_drained"],
            unique_tokens_updated=self.counters["unique_tokens_updated"],
            pairs_evaluated=self.counters["pairs_evaluated"],
            opportunities=self.counters["opportunities"],
            execution_sims=self.counters["execution_sims"],
            missed_opportunities=self.counters["missed_opportunities"],
            suppressed_by_cooldown=self.counters["suppressed_by_cooldown"],
            pair_fresh=coverage["pair_fresh"],
            pair_missing_up=coverage["pair_missing_up"],
            pair_missing_down=coverage["pair_missing_down"],
            pair_fresh_pct=coverage["pair_fresh_pct"],
            gamma_ms=self.last_gamma_ms,
            loop_ms=loop_ms,
            flush_ms=flush_ms,
            notes=(
                f"ws_event_driven redemptions={self.counters.get('redemptions', 0)} "
                f"rejects=expired:{self.counters.get('reject_expired', 0)} "
                f"missing:{self.counters.get('reject_missing_book', 0)} "
                f"stale:{self.counters.get('reject_stale_book', 0)} "
                f"min_shares:{self.counters.get('reject_min_shares', 0)} "
                f"min_net:{self.counters.get('reject_min_net_edge', 0)}"
            ),
        )
        self.counters = {key: 0 for key in self.counters}
        append_csv(self.cfg.cross_hft_paper_heartbeat_csv, [row])
        LOGGER.info(
            "HFT paper heartbeat: markets=%s pairs=%s updates=%s evals=%s opps=%s sims=%s missed=%s loop=%sms flush=%sms",
            row.markets,
            row.pairs,
            row.updates_drained,
            row.pairs_evaluated,
            row.opportunities,
            row.execution_sims,
            row.missed_opportunities,
            row.loop_ms,
            row.flush_ms,
        )
        return row

    def pair_fresh_coverage(self) -> dict[str, int | float]:
        stats: dict[str, int | float] = {
            "pair_fresh": 0,
            "pair_missing_up": 0,
            "pair_missing_down": 0,
            "pair_fresh_pct": 0.0,
        }
        for pair in self.pairs_by_id.values():
            up_book = self.clob.get_order_book_from_ws(pair.up_token_id)
            down_book = self.clob.get_order_book_from_ws(pair.down_token_id)
            if up_book and down_book:
                stats["pair_fresh"] += 1
            else:
                if not up_book:
                    stats["pair_missing_up"] += 1
                if not down_book:
                    stats["pair_missing_down"] += 1
        stats["pair_fresh_pct"] = stats["pair_fresh"] / len(self.pairs_by_id) if self.pairs_by_id else 0.0
        return stats


def build_hft_pairs(markets: list[Market], *, allow_price_inferred: bool) -> list[HftPair]:
    out: list[HftPair] = []
    for group in grouped_by_asset_end(markets).values():
        if len(group) < 2:
            continue
        for a, b in combinations(group, 2):
            for lower, higher, source in oriented_market_pairs(a, b, allow_price_inferred=allow_price_inferred):
                pair_id = f"{lower.asset}:{lower.end_ts}:{lower.slug}:{higher.slug}:{source}"
                out.append(
                    HftPair(
                        pair_id=pair_id,
                        asset=lower.asset,
                        end_ts=lower.end_ts,
                        lower=lower,
                        higher=higher,
                        orientation_source=source,
                        up_token_id=lower.tokens.up,
                        down_token_id=higher.tokens.down,
                    )
                )
    return out


def book_age_ms(book: OrderBook) -> int:
    try:
        fetched = datetime.fromisoformat(book.fetched_at.replace("Z", "+00:00"))
    except ValueError:
        return -1
    return int((datetime.now(timezone.utc) - fetched).total_seconds() * 1000)


async def run_cross_hft_paper_loop(
    cfg: ScannerConfig,
    *,
    seconds: int,
    discovery_interval_seconds: float,
    idle_sleep_ms: int,
    flush_interval_seconds: float,
    heartbeat_interval_seconds: float,
    signal_cooldown_ms: int,
    full_sweep_interval_seconds: float,
    use_rtds: bool,
) -> None:
    cfg.clob_ws_enabled = True
    scanner = CrossHftPaperScanner(cfg, signal_cooldown_ms=signal_cooldown_ms)
    stop = asyncio.Event()
    price_state = PriceState()
    rtds_task = None
    if use_rtds:
        rtds_task = asyncio.create_task(run_rtds_feed(cfg, price_state, stop))
        await asyncio.sleep(1.0)
    # Wire the oracle divergence tracker into the live executor so cross-family
    # signals can be vetoed when Binance vs Chainlink prices disagree at submit time.
    if scanner.live_executor is not None:
        scanner.oracle_tracker = OracleDivergenceTracker(chainlink_provider=price_state)
        scanner.live_executor.attach_oracle_tracker(scanner.oracle_tracker, scanner.thresholds)
    deadline = asyncio.get_event_loop().time() + seconds if seconds > 0 else None
    next_discovery_at = 0.0
    next_flush_at = asyncio.get_event_loop().time() + flush_interval_seconds
    next_heartbeat_at = 0.0
    next_warm_at = 0.0
    next_sweep_at = 0.0
    next_redeem_at = 0.0
    redeem_task: asyncio.Task[None] | None = None
    last_loop_ms = 0
    last_flush_ms = 0
    try:
        while True:
            loop_started = time.perf_counter()
            now_loop = asyncio.get_event_loop().time()
            if redeem_task and redeem_task.done():
                try:
                    redeem_task.result()
                except Exception as exc:
                    LOGGER.warning("HFT live redeem task failed: %s", exc)
                redeem_task = None
            if deadline and now_loop >= deadline:
                break
            if now_loop >= next_discovery_at:
                await asyncio.to_thread(scanner.refresh_markets, price_state if use_rtds else None)
                next_discovery_at = now_loop + discovery_interval_seconds
                next_warm_at = 0.0
                next_sweep_at = 0.0
            if cfg.cross_book_warm_enabled and now_loop >= next_warm_at:
                warm_stats = await asyncio.to_thread(scanner.warm_pair_books)
                if warm_stats:
                    LOGGER.info(
                        "HFT book warm: targets=%s refreshed=%s rest=%s bridge=%s missing=%s skipped_fresh=%s interval=%ss",
                        warm_stats.get("targets", 0),
                        warm_stats.get("refreshed", 0),
                        warm_stats.get("rest", 0),
                        warm_stats.get("bridge", 0) + warm_stats.get("bridge_server", 0),
                        warm_stats.get("missing", 0),
                        warm_stats.get("skipped_fresh", 0),
                        next_warm_interval_seconds(cfg, scanner.markets),
                    )
                next_warm_at = now_loop + next_warm_interval_seconds(cfg, scanner.markets)
            updates = scanner.clob.drain_ws_update_events(max_items=5000)
            if updates:
                await asyncio.to_thread(scanner.process_updates, updates)
            if full_sweep_interval_seconds > 0 and now_loop >= next_sweep_at:
                await asyncio.to_thread(scanner.sweep_all_pairs)
                next_sweep_at = now_loop + full_sweep_interval_seconds
            if scanner.live_executor and cfg.cross_live_redeem_enabled and now_loop >= next_redeem_at:
                if redeem_task is None:
                    redeem_task = asyncio.create_task(asyncio.to_thread(scanner.auto_redeem_due))
                else:
                    LOGGER.info("HFT live redeem skipped: previous redeem task still running")
                next_redeem_at = now_loop + max(30, cfg.cross_live_redeem_interval_seconds)
            now_loop = asyncio.get_event_loop().time()
            if now_loop >= next_flush_at:
                last_flush_ms = await asyncio.to_thread(scanner.flush)
                next_flush_at = now_loop + flush_interval_seconds
            last_loop_ms = elapsed_ms(loop_started)
            if now_loop >= next_heartbeat_at:
                scanner.heartbeat(loop_ms=last_loop_ms, flush_ms=last_flush_ms)
                next_heartbeat_at = now_loop + heartbeat_interval_seconds
                last_flush_ms = 0
            await asyncio.sleep(max(0.001, idle_sleep_ms / 1000))
    finally:
        stop.set()
        if rtds_task:
            rtds_task.cancel()
        await asyncio.to_thread(scanner.flush)
        scanner.close()
