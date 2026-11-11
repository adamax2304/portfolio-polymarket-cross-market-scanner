"""Polymarket CLOB WebSocket subscriber.

Manages connections (one per up to 500 tokens), dispatches book/price_change
messages to a callback. Auto-reconnects on disconnect.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Awaitable, Callable

import websockets


WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MAX_TOKENS_PER_CONNECTION = 400  # under 500 limit, with margin

DispatchCallback = Callable[[dict], Awaitable[None]]


async def _run_connection(token_ids: list[str], dispatch: DispatchCallback, conn_label: str) -> None:
    """Run one persistent WS connection for a chunk of tokens."""
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=30, max_size=None) as ws:
                # Subscribe
                await ws.send(json.dumps({"type": "market", "assets_ids": token_ids}))
                print(f"[ws-{conn_label}] subscribed to {len(token_ids)} tokens")
                backoff = 1.0
                async for raw in ws:
                    if not raw or raw.strip() in ("", "PONG"):
                        continue
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, list):
                        for item in data:
                            await dispatch(item)
                    elif isinstance(data, dict):
                        await dispatch(data)
        except Exception as e:
            print(f"[ws-{conn_label}] disconnected: {e}; reconnecting in {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


async def run_subscriber(
    token_ids: list[str],
    dispatch: DispatchCallback,
) -> None:
    """Spawn N parallel WS connections to cover all tokens."""
    chunks = [
        token_ids[i:i + MAX_TOKENS_PER_CONNECTION]
        for i in range(0, len(token_ids), MAX_TOKENS_PER_CONNECTION)
    ]
    print(f"[ws] {len(token_ids)} tokens → {len(chunks)} connections")
    tasks = [
        asyncio.create_task(_run_connection(chunk, dispatch, f"c{i}"))
        for i, chunk in enumerate(chunks)
    ]
    await asyncio.gather(*tasks)
