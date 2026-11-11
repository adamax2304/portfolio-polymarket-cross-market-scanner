from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from .clob import ClobClient
from .config import ScannerConfig
from .diagnostics import write_all_diagnostics
from .gamma import GammaClient
from .models import Market, OrderBook
from .probability import ProbabilityObservation, ProbabilitySignal, observations_for_market, signals_from_observations
from .probability_calibrated import (
    CalibratedProbabilitySignal,
    ProbabilityCalibrationModel,
    calibrated_signals_from_observations,
)
from .probability_early import early_model_signals_from_raw
from .probability_risk import ProbabilityRiskDecision, ProbabilityRiskEngine
from .probability_settlement import ProbabilitySettler, load_probability_settlements
from .probability_v01_shadow import build_v01_shadow_entries, v01_rules_markdown, write_v01_shadow_outputs
from .probability_v0_filters import build_v0_live_labels, write_v0_filter_outputs, write_v0_forward_outputs
from .rtds import PriceState, run_rtds_feed
from .storage import ScannerStore, append_csv
from .thresholds import ThresholdStore
from .utils import utc_now

LOGGER = logging.getLogger(__name__)


class ProbabilityEdgeScanner:
    def __init__(self, cfg: ScannerConfig):
        self.cfg = cfg
        self.gamma = GammaClient(cfg)
        self.clob = ClobClient(cfg)
        self.thresholds = ThresholdStore(cfg.threshold_path)
        self.store = ScannerStore(cfg.db_path)
        self.calibration = ProbabilityCalibrationModel(cfg.probability_calibration_dir)
        self.risk = ProbabilityRiskEngine(cfg, soft_mode=True)
        if not self.calibration.available:
            LOGGER.warning("Probability calibration not available at %s", cfg.probability_calibration_dir)

    def close(self) -> None:
        self.clob.close()
        self.store.close()

    def scan_once(
        self,
        price_state: PriceState,
        *,
        min_net_edge: float,
        min_seconds_to_expiry: int,
        max_seconds_to_expiry: int,
        min_seconds_since_start: int,
        max_seconds_since_start: int,
        min_calibrated_edge: float,
        min_calibrated_grade: str,
        primary_source: str = "raw",
    ) -> tuple[
        list[ProbabilityObservation],
        list[ProbabilitySignal],
        list[CalibratedProbabilitySignal],
        list[ProbabilityRiskDecision],
        list[Market],
    ]:
        now_iso = utc_now()
        markets = self.gamma.discover_markets()
        self.thresholds.reload()
        self.thresholds.apply(markets)
        self.capture_thresholds(markets, price_state)
        self.thresholds.apply(markets)
        self.store.upsert_markets(markets, now_iso)
        books = self.fetch_books(markets)
        self.capture_thresholds(markets, price_state)
        self.thresholds.apply(markets)
        observations: list[ProbabilityObservation] = []
        for market in markets:
            if market.threshold_price is None:
                continue
            tick = price_state.latest(market.asset, max_age_seconds=10.0)
            if not tick:
                continue
            observations.extend(
                observations_for_market(
                    market,
                    tick,
                    price_state.recent(market.asset, max_age_seconds=300),
                    books,
                    taker_fee_rate=self.cfg.taker_fee_rate,
                )
            )
        signals = signals_from_observations(
            observations,
            min_net_edge=min_net_edge,
            min_shares=self.cfg.min_shares,
            max_paper_shares=self.cfg.max_paper_shares,
            min_seconds_to_expiry=min_seconds_to_expiry,
            max_seconds_to_expiry=max_seconds_to_expiry,
            min_seconds_since_start=min_seconds_since_start,
            max_seconds_since_start=max_seconds_since_start,
        )
        if primary_source == "calibrated":
            calibrated_signals = calibrated_signals_from_observations(
                observations,
                self.calibration,
                min_shares=self.cfg.min_shares,
                max_paper_shares=self.cfg.max_paper_shares,
                min_seconds_to_expiry=min_seconds_to_expiry,
                max_seconds_to_expiry=max_seconds_to_expiry,
                min_calibrated_edge=min_calibrated_edge,
                min_grade=min_calibrated_grade,
            )
        else:
            calibrated_signals = early_model_signals_from_raw(
                signals,
                min_seconds_since_start=min_seconds_since_start,
                max_seconds_since_start=max_seconds_since_start,
                min_raw_edge=min_calibrated_edge,
            )
        append_csv(self.cfg.probability_observations_csv, observations)
        append_csv(self.cfg.probability_signals_csv, signals)
        append_csv(self.cfg.probability_calibrated_signals_csv, calibrated_signals)
        risk_decisions = self.risk.evaluate_many(calibrated_signals)
        append_csv(self.cfg.probability_risk_decisions_csv, risk_decisions)
        v0_labels = build_v0_live_labels(risk_decisions)
        append_csv(self.cfg.probability_v0_live_labels_csv, v0_labels)
        append_csv(
            self.cfg.probability_v01_shadow_entries_csv,
            build_v01_shadow_entries(
                risk_decisions,
                v0_labels,
                calibrated_signals,
                books,
                entry_slippage=self.cfg.cross_execution_entry_slippage,
            ),
        )
        self.cfg.probability_v01_rules_md.write_text(v01_rules_markdown(), encoding="utf-8")
        LOGGER.info(
            "Probability scan[%s]: %s markets, %s books, %s observations, %s raw early signals, %s model signals, %s risk decisions",
            primary_source,
            len(markets),
            len(books),
            len(observations),
            len(signals),
            len(calibrated_signals),
            len(risk_decisions),
        )
        return observations, signals, calibrated_signals, risk_decisions, markets

    def fetch_books(self, markets: list[Market]) -> dict[str, OrderBook]:
        books: dict[str, OrderBook] = {}
        token_ids = sorted({m.tokens.up for m in markets} | {m.tokens.down for m in markets})
        workers = max(1, min(self.cfg.clob_book_workers, len(token_ids) or 1))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(self.clob.get_order_book, token_id): token_id for token_id in token_ids}
            for future in as_completed(futures):
                token_id = futures[future]
                try:
                    book = future.result()
                except Exception as exc:
                    LOGGER.debug("Book fetch failed for %s: %s", token_id[:16], exc)
                    continue
                if book:
                    books[token_id] = book
        return books

    def capture_thresholds(self, markets: list[Market], price_state: PriceState) -> None:
        for market in markets:
            if market.threshold_price is not None:
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
            LOGGER.info("Captured threshold %s %s = %.6f", market.asset, market.slug, tick.price)


async def run_probability_loop(
    cfg: ScannerConfig,
    *,
    seconds: int,
    poll_seconds: float,
    min_net_edge: float,
    min_seconds_to_expiry: int,
    max_seconds_to_expiry: int,
    min_seconds_since_start: int,
    max_seconds_since_start: int,
    min_calibrated_edge: float,
    min_calibrated_grade: str,
    primary_source: str = "raw",
    auto_settlement_batch_size: int = 50,
    auto_settlement_interval_seconds: int = 300,
) -> None:
    scanner = ProbabilityEdgeScanner(cfg)
    stop = asyncio.Event()
    price_state = PriceState()
    rtds_task = asyncio.create_task(run_rtds_feed(cfg, price_state, stop))
    await asyncio.sleep(1.0)
    deadline = asyncio.get_event_loop().time() + seconds if seconds > 0 else None
    next_settlement_at = asyncio.get_event_loop().time() + max(5, auto_settlement_interval_seconds)
    try:
        while True:
            await asyncio.to_thread(
                scanner.scan_once,
                price_state,
                min_net_edge=min_net_edge,
                min_seconds_to_expiry=min_seconds_to_expiry,
                max_seconds_to_expiry=max_seconds_to_expiry,
                min_seconds_since_start=min_seconds_since_start,
                max_seconds_since_start=max_seconds_since_start,
                min_calibrated_edge=min_calibrated_edge,
                min_calibrated_grade=min_calibrated_grade,
                primary_source=primary_source,
            )
            now_loop = asyncio.get_event_loop().time()
            if auto_settlement_batch_size > 0 and auto_settlement_interval_seconds > 0 and now_loop >= next_settlement_at:
                await asyncio.to_thread(settle_probability_batch, cfg, auto_settlement_batch_size)
                next_settlement_at = now_loop + auto_settlement_interval_seconds
            if deadline is not None and asyncio.get_event_loop().time() >= deadline:
                break
            await asyncio.sleep(poll_seconds)
    finally:
        stop.set()
        rtds_task.cancel()
        scanner.close()


def settle_probability_batch(cfg: ScannerConfig, batch_size: int) -> None:
    settler = ProbabilitySettler(cfg)
    processed = settler.settle_due(limit=batch_size)
    if not processed:
        return
    all_settlements = list(load_probability_settlements(cfg.probability_settlements_csv).values())
    write_v0_filter_outputs(cfg, all_settlements)
    write_v0_forward_outputs(cfg, all_settlements)
    write_v01_shadow_outputs(cfg, all_settlements)
    write_all_diagnostics(cfg)
    settled = sum(1 for row in processed if row.status == "SETTLED")
    LOGGER.info("Probability auto-settlement: %s processed, %s settled, %s pending/error", len(processed), settled, len(processed) - settled)


def summarize_probability(
    signals: list[ProbabilitySignal],
    observations: list[ProbabilityObservation],
    calibrated_signals: list[CalibratedProbabilitySignal] | None = None,
    risk_decisions: list[ProbabilityRiskDecision] | None = None,
) -> str:
    calibrated_signals = calibrated_signals or []
    risk_decisions = risk_decisions or []
    lines = [
        f"observations={len(observations)} raw_signals={len(signals)} "
        f"calibrated_signals={len(calibrated_signals)} risk_decisions={len(risk_decisions)}"
    ]
    for decision in risk_decisions[:10]:
        lines.append(
            f"RISK {decision.action} {decision.asset.upper()} {decision.outcome} {decision.market_slug} "
            f"cost={decision.recommended_cost_usdc:.2f} shares={decision.recommended_shares:.2f} "
            f"reasons={decision.risk_reasons or '-'}"
        )
    for signal in calibrated_signals[:10]:
        lines.append(
            f"CAL {signal.grade} {signal.asset.upper()} {signal.outcome} {signal.market_slug} ask={signal.best_ask:.3f} "
            f"p_cal={signal.calibrated_probability:.3f} cal_edge={signal.calibrated_edge:.3f} "
            f"bucket={signal.time_price_bucket} roi={signal.time_price_roi_on_cost:.3f} "
            f"shares={signal.executable_shares:.2f} tte={signal.seconds_to_expiry}s"
        )
    for signal in signals[:10]:
        lines.append(
            f"RAW {signal.asset.upper()} {signal.outcome} {signal.market_slug} ask={signal.best_ask:.3f} "
            f"fair={signal.fair_prob:.3f} net_edge={signal.net_edge:.3f} "
            f"shares={signal.executable_shares:.2f} tte={signal.seconds_to_expiry}s"
        )
    return "\n".join(lines)
