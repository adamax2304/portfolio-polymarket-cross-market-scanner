"""Strict-FOK live runner. Mirrors the maker-first runner structure."""

from __future__ import annotations

import asyncio
import logging

from ..config import ScannerConfig
from ..cross_hft_paper import CrossHftPaperScanner, run_cross_hft_paper_loop
from .executor import StrictFokLiveExecutor


LOGGER = logging.getLogger(__name__)


async def run_strict_fok_live(
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
    cfg.cross_live_strict_fok_enabled = True

    import polymarket_cross_scanner.cross_hft_paper as paper_module

    original_executor_cls = paper_module.CrossMarketLiveExecutor
    paper_module.CrossMarketLiveExecutor = StrictFokLiveExecutor
    try:
        await run_cross_hft_paper_loop(
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
        paper_module.CrossMarketLiveExecutor = original_executor_cls
