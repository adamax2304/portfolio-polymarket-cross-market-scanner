from __future__ import annotations

import asyncio
from collections import deque
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

import requests
import websockets

from .config import ScannerConfig
from .models import BookLevel, OrderBook
from .utils import safe_float, utc_now


class ClobClient:
    def __init__(self, cfg: ScannerConfig):
        self.cfg = cfg
        self._local = threading.local()
        if not cfg.clob_bridge_server_path and cfg.clob_bridge_path:
            default_server = cfg.root / "polymarket_cross_scanner" / "fast_clob_bridge_server.py"
            if default_server.exists():
                cfg.clob_bridge_server_path = str(default_server)
        self._bridge_server = JsonLinesBridgeClient(cfg) if cfg.clob_bridge_server_path else None
        self._ws_cache = ClobWsBookCache(cfg) if cfg.clob_ws_enabled else None
        if self._ws_cache:
            self._ws_cache.start()

    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": "polymarket-cross-market-scanner/0.1"})
            self._local.session = session
        return session

    def get_order_book(self, token_id: str) -> OrderBook | None:
        book, _source = self.get_order_book_with_source(token_id)
        return book

    def get_order_book_with_source(self, token_id: str) -> tuple[OrderBook | None, str]:
        ws_book = self.get_order_book_from_ws(token_id)
        if ws_book:
            return ws_book, "ws"
        bridge_server_book = self.get_order_book_from_bridge_server(token_id)
        if bridge_server_book:
            self.cache_ws_book(bridge_server_book)
            return bridge_server_book, "bridge_server"
        bridge_book = self.get_order_book_from_bridge(token_id)
        if bridge_book:
            self.cache_ws_book(bridge_book)
            return bridge_book, "bridge"
        return self.get_order_book_rest(token_id)

    def get_order_book_rest(self, token_id: str, *, mark_ws_updated: bool = False) -> tuple[OrderBook | None, str]:
        resp = self.session().get(
            f"{self.cfg.clob_api_base.rstrip('/')}/book",
            params={"token_id": token_id},
            timeout=self.cfg.request_timeout,
        )
        if resp.status_code == 404:
            return None, "missing"
        resp.raise_for_status()
        data = resp.json()
        bids = sorted(parse_levels(data.get("bids", [])), key=lambda x: x.price, reverse=True)
        asks = sorted(parse_levels(data.get("asks", [])), key=lambda x: x.price)
        book = OrderBook(token_id=token_id, bids=bids, asks=asks, fetched_at=utc_now())
        self.cache_ws_book(book, mark_updated=mark_ws_updated, update_source="warm_rest" if mark_ws_updated else "rest")
        return book, "rest"

    def refresh_order_book_cache(self, token_id: str) -> tuple[OrderBook | None, str]:
        bridge_server_book = self.get_order_book_from_bridge_server(token_id)
        if bridge_server_book:
            self.cache_ws_book(bridge_server_book, mark_updated=True, update_source="warm_bridge_server")
            return bridge_server_book, "bridge_server"
        bridge_book = self.get_order_book_from_bridge(token_id)
        if bridge_book:
            self.cache_ws_book(bridge_book, mark_updated=True, update_source="warm_bridge")
            return bridge_book, "bridge"
        return self.get_order_book_rest(token_id, mark_ws_updated=True)

    def get_order_book_ws_only(self, token_id: str) -> tuple[OrderBook | None, str]:
        book = self.get_order_book_from_ws(token_id)
        return (book, "ws") if book else (None, "missing")

    def drain_ws_updates(self, max_items: int = 1000) -> list[str]:
        if not self._ws_cache:
            return []
        return self._ws_cache.drain_updated_tokens(max_items)

    def drain_ws_update_events(self, max_items: int = 1000) -> list[tuple[str, str]]:
        if not self._ws_cache:
            return []
        return self._ws_cache.drain_updated_events(max_items)

    def subscribe_many(self, token_ids: list[str] | set[str]) -> None:
        if self._ws_cache:
            self._ws_cache.subscribe_many(token_ids)

    def cache_ws_book(self, book: OrderBook, *, mark_updated: bool = False, update_source: str = "cache") -> None:
        if self._ws_cache:
            self._ws_cache.put_order_book(book, mark_updated=mark_updated, update_source=update_source)

    def ws_book_age_seconds(self, token_id: str) -> float | None:
        if not self._ws_cache:
            return None
        return self._ws_cache.book_age_seconds(token_id)

    def get_order_book_from_ws(self, token_id: str) -> OrderBook | None:
        if not self._ws_cache:
            return None
        book = self._ws_cache.get_order_book(token_id, self.cfg.clob_ws_max_book_age_seconds)
        if not book or not book.asks:
            return None
        return book

    def get_order_book_from_bridge(self, token_id: str) -> OrderBook | None:
        if not self.cfg.clob_bridge_path:
            return None
        bridge = Path(self.cfg.clob_bridge_path)
        if not bridge.exists():
            return None
        try:
            proc = subprocess.run(
                ["python", str(bridge), "get_order_book"],
                input=json.dumps({"token_id": token_id}),
                text=True,
                capture_output=True,
                timeout=self.cfg.request_timeout,
                cwd=str(bridge.parent.parent),
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                return None
            payload = json.loads(proc.stdout)
            if not payload.get("ok"):
                return None
            data = payload.get("result") or {}
            bids = sorted(parse_levels(data.get("bids", [])), key=lambda x: x.price, reverse=True)
            asks = sorted(parse_levels(data.get("asks", [])), key=lambda x: x.price)
            return OrderBook(token_id=token_id, bids=bids, asks=asks, fetched_at=utc_now())
        except Exception:
            return None

    def get_order_book_from_bridge_server(self, token_id: str) -> OrderBook | None:
        if not self._bridge_server:
            return None
        payload = self._bridge_server.request("get_order_book", {"token_id": token_id})
        if not payload or not payload.get("ok"):
            return None
        data = payload.get("result") or {}
        bids = sorted(parse_levels(data.get("bids", [])), key=lambda x: x.price, reverse=True)
        asks = sorted(parse_levels(data.get("asks", [])), key=lambda x: x.price)
        return OrderBook(token_id=token_id, bids=bids, asks=asks, fetched_at=utc_now())

    def close(self) -> None:
        if self._ws_cache:
            self._ws_cache.close()
        if self._bridge_server:
            self._bridge_server.close()


def parse_levels(rows: list[dict]) -> list[BookLevel]:
    out: list[BookLevel] = []
    for row in rows or []:
        price = safe_float(row.get("price"))
        size = safe_float(row.get("size"))
        if price is not None and size is not None and price > 0 and size > 0:
            out.append(BookLevel(price=price, size=size))
    return out


class JsonLinesBridgeClient:
    def __init__(self, cfg: ScannerConfig):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None

    def request(self, command: str, payload: dict) -> dict | None:
        with self.lock:
            proc = self._ensure_proc()
            if not proc or not proc.stdin or not proc.stdout:
                return None
            req_id = uuid.uuid4().hex
            request = {"id": req_id, "command": command, **payload}
            try:
                proc.stdin.write(json.dumps(request) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
                if not line:
                    self.close()
                    return None
                response = json.loads(line)
                if str(response.get("id")) != req_id:
                    return None
                return response
            except Exception:
                self.close()
                return None

    def _ensure_proc(self) -> subprocess.Popen | None:
        if self.proc and self.proc.poll() is None:
            return self.proc
        server = Path(self.cfg.clob_bridge_server_path)
        if not server.exists():
            return None
        cwd = Path(self.cfg.clob_bridge_cwd) if self.cfg.clob_bridge_cwd else server.parent.parent
        env = build_bridge_env(self.cfg)
        try:
            self.proc = subprocess.Popen(
                [self.cfg.clob_bridge_python, str(server)],
                cwd=str(cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                env=env,
            )
            ready = self.proc.stdout.readline() if self.proc.stdout else ""
            if not ready:
                self.close()
                return None
            payload = json.loads(ready)
            if not payload.get("ready"):
                self.close()
                return None
            return self.proc
        except Exception:
            self.close()
            return None

    def close(self) -> None:
        proc = self.proc
        self.proc = None
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass


def build_bridge_env(cfg: ScannerConfig) -> dict[str, str]:
    env = dict(os.environ)
    env_path = Path(cfg.clob_bridge_env_path) if cfg.clob_bridge_env_path else None
    if env_path and env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            env.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return env


class ClobWsBookCache:
    def __init__(self, cfg: ScannerConfig):
        self.cfg = cfg
        self.assets: set[str] = set()
        self.books: dict[str, tuple[OrderBook, float]] = {}
        self.updated_tokens: deque[tuple[str, str]] = deque(maxlen=20000)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._thread_main, name="clob-ws-book-cache", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stop_event.set()

    def subscribe_many(self, token_ids: list[str] | set[str]) -> None:
        clean = {str(token_id) for token_id in token_ids if token_id}
        if not clean:
            return
        with self.lock:
            self.assets.update(clean)

    def drain_updated_tokens(self, max_items: int = 1000) -> list[str]:
        return [token_id for token_id, _source in self.drain_updated_events(max_items)]

    def drain_updated_events(self, max_items: int = 1000) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        with self.lock:
            while self.updated_tokens and len(out) < max_items:
                out.append(self.updated_tokens.popleft())
        return out

    def get_order_book(self, token_id: str, max_age_seconds: float) -> OrderBook | None:
        with self.lock:
            item = self.books.get(str(token_id))
        if not item:
            return None
        book, updated_at = item
        if time.time() - updated_at > max_age_seconds:
            return None
        return book

    def book_age_seconds(self, token_id: str) -> float | None:
        with self.lock:
            item = self.books.get(str(token_id))
        if not item:
            return None
        return time.time() - item[1]

    def put_order_book(self, book: OrderBook, *, mark_updated: bool = False, update_source: str = "cache") -> None:
        now = time.time()
        with self.lock:
            self.books[str(book.token_id)] = (book, now)
            if mark_updated:
                self.updated_tokens.append((str(book.token_id), update_source))

    def _thread_main(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        while not self.stop_event.is_set():
            chunks = self._asset_chunks()
            if not chunks:
                await asyncio.sleep(0.2)
                continue
            tasks = [asyncio.create_task(self._run_shard(i)) for i in range(len(chunks))]
            try:
                while not self.stop_event.is_set():
                    await asyncio.sleep(2.0)
                    if len(self._asset_chunks()) != len(tasks):
                        break
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    def _asset_chunks(self) -> list[list[str]]:
        with self.lock:
            assets = sorted(self.assets)
        size = max(1, self.cfg.clob_ws_max_assets_per_conn)
        return [assets[i:i + size] for i in range(0, len(assets), size)]

    async def _run_shard(self, shard_index: int) -> None:
        while not self.stop_event.is_set():
            chunks = self._asset_chunks()
            if shard_index >= len(chunks):
                await asyncio.sleep(0.2)
                continue
            assets = chunks[shard_index]
            if not assets:
                await asyncio.sleep(0.2)
                continue
            try:
                async with websockets.connect(
                    self.cfg.clob_ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=2,
                    max_queue=2048,
                ) as ws:
                    await ws.send(json.dumps({"assets_ids": assets, "type": "market", "custom_feature_enabled": True}))
                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        while not self.stop_event.is_set():
                            chunks = self._asset_chunks()
                            wanted = chunks[shard_index] if shard_index < len(chunks) else []
                            if wanted != assets:
                                break
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                            except asyncio.TimeoutError:
                                continue
                            if isinstance(raw, bytes):
                                raw = raw.decode("utf-8", errors="ignore")
                            if raw == "PONG":
                                continue
                            self._handle_payload(raw)
                    finally:
                        ping_task.cancel()
            except Exception:
                await asyncio.sleep(1.0)

    async def _ping_loop(self, ws) -> None:
        while not self.stop_event.is_set():
            await asyncio.sleep(10)
            try:
                await ws.send("PING")
            except Exception:
                return

    def _handle_payload(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except Exception:
            return
        items = payload if isinstance(payload, list) else [payload]
        now = time.time()
        for item in items:
            if not isinstance(item, dict):
                continue
            event_type = str(item.get("event_type") or item.get("type") or "").lower()
            token_id = str(item.get("asset_id") or item.get("assetId") or "")
            if not token_id:
                continue
            if event_type == "book":
                book = OrderBook(
                    token_id=token_id,
                    bids=sorted(parse_levels(item.get("bids", [])), key=lambda x: x.price, reverse=True),
                    asks=sorted(parse_levels(item.get("asks", [])), key=lambda x: x.price),
                    fetched_at=utc_now(),
                )
                with self.lock:
                    self.books[token_id] = (book, now)
                    self.updated_tokens.append((token_id, "ws_book"))
            elif event_type == "best_bid_ask":
                with self.lock:
                    old = self.books.get(token_id)
                    if not old:
                        continue
                    old_book = old[0]
                    ask = safe_float(item.get("best_ask") or item.get("bestAsk") or item.get("ask"))
                    bid = safe_float(item.get("best_bid") or item.get("bestBid") or item.get("bid"))
                    asks = old_book.asks
                    bids = old_book.bids
                    if ask is not None:
                        size = asks[0].size if asks else 0.0
                        asks = [BookLevel(ask, size)] + [level for level in asks[1:] if level.price != ask]
                    if bid is not None:
                        size = bids[0].size if bids else 0.0
                        bids = [BookLevel(bid, size)] + [level for level in bids[1:] if level.price != bid]
                    self.books[token_id] = (
                        OrderBook(
                            token_id=token_id,
                            bids=sorted(bids, key=lambda x: x.price, reverse=True),
                            asks=sorted(asks, key=lambda x: x.price),
                            fetched_at=utc_now(),
                        ),
                        now,
                    )
                    self.updated_tokens.append((token_id, "ws_best_bid_ask"))
