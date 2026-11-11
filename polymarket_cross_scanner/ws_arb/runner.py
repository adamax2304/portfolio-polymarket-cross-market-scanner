"""WS arb scanner orchestrator.

Outer loop pattern:
 1. Discover negRisk events
 2. Build detector with all token IDs
 3. Spawn WS subscribers + heartbeat
 4. Run for `rediscovery_interval_s` (default 30 min)
 5. Tear down → loop back to step 1 with fresh discovery

This solves three problems at once:
 - Stale event list (FIFA matches resolve, scanner never sees new ones)
 - Memory growth (TokenBooks for resolved markets accumulate)
 - WS connection drift (forced reconnect every interval)

The teardown gap is ~1-2 seconds per cycle.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from .detector import ArbDetector
from .discovery import TrackedEvent, discover_negrisk_events
from .subscriber import run_subscriber


async def _run_one_cycle(
    *,
    min_edge: float,
    min_volume_24h: float,
    max_events: int,
    alert_cooldown_seconds: float,
    alert_csv_path: str,
    cycle_duration_s: float,
    heartbeat_interval_s: float,
    cycle_idx: int,
) -> None:
    """Run one discovery cycle. Returns when cycle_duration_s elapses or all tasks die."""
    print(f"[ws-arb] === cycle #{cycle_idx} discovering events ===")
    events: list[TrackedEvent] = discover_negrisk_events(
        min_volume_24h=min_volume_24h,
        max_events=max_events,
    )
    print(f"[ws-arb] {len(events)} events selected")
    if not events:
        print("[ws-arb] no events; sleeping 60s before retry")
        await asyncio.sleep(60)
        return
    n_tokens = sum(len(e.yes_token_ids) for e in events)
    print(f"[ws-arb] total tokens to subscribe: {n_tokens}")
    for e in events[:10]:
        print(f"  - {e.title[:60]:60s} markets={len(e.yes_token_ids)} vol=${e.volume_24h:,.0f}")

    detector = ArbDetector(
        events=events,
        min_edge=min_edge,
        alert_cooldown_seconds=alert_cooldown_seconds,
        alert_csv_path=Path(alert_csv_path),
    )

    async def dispatch(msg: dict) -> None:
        await detector.dispatch(msg)

    all_tokens = [t for e in events for t in e.yes_token_ids]

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(heartbeat_interval_s)
            print(f"[ws-arb hb cycle{cycle_idx}] {detector.heartbeat()}")

    sub_task = asyncio.create_task(run_subscriber(all_tokens, dispatch))
    hb_task = asyncio.create_task(heartbeat())
    timer_task = asyncio.create_task(asyncio.sleep(cycle_duration_s))

    try:
        # Whichever completes first wins. Normally it's the timer (planned rediscovery).
        # If subscriber dies catastrophically we also exit and restart fresh.
        done, pending = await asyncio.wait(
            {sub_task, hb_task, timer_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        # Drain cancellations
        await asyncio.gather(*pending, return_exceptions=True)
        # Surface any non-cancel exception from completed tasks (for logs)
        for t in done:
            if t is timer_task:
                continue
            try:
                exc = t.exception()
                if exc:
                    print(f"[ws-arb] task ended with: {exc!r}")
            except asyncio.CancelledError:
                pass
    finally:
        detector.close()
        print(f"[ws-arb] === cycle #{cycle_idx} torn down ===")


async def run_ws_arb_scanner(
    *,
    min_edge: float = 0.02,
    min_volume_24h: float = 500.0,
    max_events: int = 120,
    alert_cooldown_seconds: float = 15.0,
    alert_csv_path: str = "/root/polymarket/cross_scanner/data/ws_arb_alerts.csv",
    rediscovery_interval_s: float = 1800.0,  # 30 min
    heartbeat_interval_s: float = 30.0,
) -> None:
    cycle_idx = 0
    while True:
        cycle_idx += 1
        try:
            await _run_one_cycle(
                min_edge=min_edge,
                min_volume_24h=min_volume_24h,
                max_events=max_events,
                alert_cooldown_seconds=alert_cooldown_seconds,
                alert_csv_path=alert_csv_path,
                cycle_duration_s=rediscovery_interval_s,
                heartbeat_interval_s=heartbeat_interval_s,
                cycle_idx=cycle_idx,
            )
        except Exception as e:
            print(f"[ws-arb] cycle #{cycle_idx} crashed: {e!r}; restarting in 15s")
            await asyncio.sleep(15)
