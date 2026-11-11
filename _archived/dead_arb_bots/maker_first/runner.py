"""maker-first live runner.

We reuse the existing ``cross_hft_paper`` scanner — it already wires up the WS book
cache, market discovery, threshold capture (incl. Binance hourly), pair-eval loop and
oracle divergence tracker. The only difference is the executor class and a polling
hook so deferred maker orders make progress between WS-driven iterations.
"""

from __future__ import annotations

import asyncio
import logging

from ..config import ScannerConfig
from ..cross_hft_paper import CrossHftPaperScanner, run_cross_hft_paper_loop
from .executor import MakerFirstLiveExecutor


LOGGER = logging.getLogger(__name__)


async def run_maker_first_live(
    cfg: ScannerConfig,
    *,
    seconds: int,
    discovery_interval_seconds: float,
    idle_sleep_ms: int,
    flush_interval_seconds: float,
    heartbeat_interval_seconds: float,
    signal_cooldown_ms: int,
    full_sweep_interval_seconds: float,
    use_rtds: bool = True,
) -> None:
    cfg.cross_live_enabled = True
    cfg.cross_live_maker_first_enabled = True

    # Monkey-patch the constructor that ``run_cross_hft_paper_loop`` calls so that the
    # scanner uses the maker-first executor instead of the default ``CrossMarketLiveExecutor``.
    import polymarket_cross_scanner.cross_hft_paper as paper_module

    original_executor_cls = paper_module.CrossMarketLiveExecutor
    paper_module.CrossMarketLiveExecutor = MakerFirstLiveExecutor

    # The scanner only polls makers when ``process_pair_ids`` fires; if pairs are very
    # quiet we still want to make timely progress on pending maker orders. Wrap the
    # scanner loop with a small periodic poller.
    original_run = run_cross_hft_paper_loop

    async def _wrapped_run() -> None:
        scanner_ref: dict[str, CrossHftPaperScanner] = {}

        # Capture the scanner instance the moment it's constructed.
        original_init = CrossHftPaperScanner.__init__

        def _capture_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            original_init(self, *args, **kwargs)
            scanner_ref["scanner"] = self

        CrossHftPaperScanner.__init__ = _capture_init  # type: ignore[assignment]
        try:
            poller_task = asyncio.create_task(_poll_loop(scanner_ref))
            try:
                await original_run(
                    cfg,
                    seconds=seconds,
                    discovery_interval_seconds=discovery_interval_seconds,
                    idle_sleep_ms=idle_sleep_ms,
                    flush_interval_seconds=flush_interval_seconds,
                    heartbeat_interval_seconds=heartbeat_interval_seconds,
                    signal_cooldown_ms=signal_cooldown_ms,
                    full_sweep_interval_seconds=full_sweep_interval_seconds,
                    use_rtds=use_rtds,
                )
            finally:
                poller_task.cancel()
                try:
                    await poller_task
                except asyncio.CancelledError:
                    pass
        finally:
            CrossHftPaperScanner.__init__ = original_init  # type: ignore[assignment]
            paper_module.CrossMarketLiveExecutor = original_executor_cls

    async def _poll_loop(ref: dict[str, CrossHftPaperScanner]) -> None:
        interval = max(0.05, cfg.cross_live_maker_poll_interval_ms / 1000.0)
        while True:
            try:
                scanner = ref.get("scanner")
                executor = getattr(scanner, "live_executor", None) if scanner else None
                if isinstance(executor, MakerFirstLiveExecutor):
                    book_lookup = getattr(getattr(scanner, "clob", None), "get_order_book_from_ws", None)
                    finalized = executor.poll_pending_makers(book_lookup=book_lookup)
                    for row in finalized:
                        LOGGER.info(
                            "Maker-first finalize: signal=%s result=%s notes=%s",
                            row.signal_id,
                            row.live_result,
                            row.notes[:120],
                        )
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover — defensive
                LOGGER.exception("maker-first poll loop error: %s", exc)
                await asyncio.sleep(1.0)

    await _wrapped_run()
