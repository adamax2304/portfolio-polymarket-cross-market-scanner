from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import csv
import sqlite3
import time

from .models import ArbSignal, Market, PaperSettlement, PaperTrade


class ScannerStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS markets (
                key TEXT PRIMARY KEY,
                asset TEXT,
                duration_sec INTEGER,
                slug TEXT,
                question TEXT,
                condition_id TEXT,
                up_token_id TEXT,
                down_token_id TEXT,
                start_ts INTEGER,
                end_ts INTEGER,
                threshold_price REAL,
                threshold_source TEXT,
                seen_at TEXT,
                liquidity REAL DEFAULT 0,
                volume_24h REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS signals (
                signal_id TEXT PRIMARY KEY,
                detected_at TEXT,
                asset TEXT,
                end_ts INTEGER,
                lower_market_slug TEXT,
                higher_market_slug TEXT,
                lower_threshold REAL,
                higher_threshold REAL,
                long_up_slug TEXT,
                long_down_slug TEXT,
                up_token_id TEXT,
                down_token_id TEXT,
                up_ask REAL,
                down_ask REAL,
                top_cost REAL,
                top_edge REAL,
                avg_cost REAL,
                avg_edge REAL,
                executable_shares REAL,
                orientation_source TEXT,
                estimated_taker_fee_per_share REAL DEFAULT 0,
                net_avg_edge REAL DEFAULT 0,
                up_book_depth_shares_1pct REAL DEFAULT 0,
                down_book_depth_shares_1pct REAL DEFAULT 0,
                up_book_depth_usdc_1pct REAL DEFAULT 0,
                down_book_depth_usdc_1pct REAL DEFAULT 0,
                up_ask_levels_json TEXT DEFAULT '[]',
                down_ask_levels_json TEXT DEFAULT '[]',
                notes TEXT
            );
            CREATE TABLE IF NOT EXISTS paper_trades (
                signal_id TEXT PRIMARY KEY,
                opened_at TEXT,
                asset TEXT,
                shares REAL,
                cost REAL,
                worst_case_payout REAL,
                locked_profit_estimate REAL,
                status TEXT
            );
            CREATE TABLE IF NOT EXISTS paper_settlements (
                signal_id TEXT PRIMARY KEY,
                settled_at TEXT,
                asset TEXT,
                end_ts INTEGER,
                shares REAL,
                cost REAL,
                lower_market_slug TEXT,
                higher_market_slug TEXT,
                lower_threshold REAL,
                higher_threshold REAL,
                lower_final_price REAL,
                higher_final_price REAL,
                up_leg_won INTEGER,
                down_leg_won INTEGER,
                payout REAL,
                realized_profit REAL,
                locked_profit_estimate REAL,
                status TEXT,
                notes TEXT
            );
            """
        )
        self.conn.commit()
        self.ensure_column("markets", "liquidity", "REAL DEFAULT 0")
        self.ensure_column("markets", "volume_24h", "REAL DEFAULT 0")
        self.ensure_column("signals", "estimated_taker_fee_per_share", "REAL DEFAULT 0")
        self.ensure_column("signals", "lower_threshold", "REAL")
        self.ensure_column("signals", "higher_threshold", "REAL")
        self.ensure_column("signals", "up_token_id", "TEXT")
        self.ensure_column("signals", "down_token_id", "TEXT")
        self.ensure_column("signals", "up_ask", "REAL")
        self.ensure_column("signals", "down_ask", "REAL")
        self.ensure_column("signals", "top_cost", "REAL")
        self.ensure_column("signals", "avg_cost", "REAL")
        self.ensure_column("signals", "net_avg_edge", "REAL DEFAULT 0")
        self.ensure_column("signals", "up_book_depth_shares_1pct", "REAL DEFAULT 0")
        self.ensure_column("signals", "down_book_depth_shares_1pct", "REAL DEFAULT 0")
        self.ensure_column("signals", "up_book_depth_usdc_1pct", "REAL DEFAULT 0")
        self.ensure_column("signals", "down_book_depth_usdc_1pct", "REAL DEFAULT 0")
        self.ensure_column("signals", "up_ask_levels_json", "TEXT DEFAULT '[]'")
        self.ensure_column("signals", "down_ask_levels_json", "TEXT DEFAULT '[]'")

    def ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            self.conn.commit()

    def upsert_markets(self, markets: list[Market], seen_at: str) -> None:
        self.conn.executemany(
            """
            INSERT INTO markets(
                key, asset, duration_sec, slug, question, condition_id,
                up_token_id, down_token_id, start_ts, end_ts,
                threshold_price, threshold_source, seen_at, liquidity, volume_24h
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                threshold_price=excluded.threshold_price,
                threshold_source=excluded.threshold_source,
                seen_at=excluded.seen_at,
                liquidity=excluded.liquidity,
                volume_24h=excluded.volume_24h
            """,
            [
                (
                    market.key, market.asset, market.duration_sec, market.slug, market.question,
                    market.condition_id, market.tokens.up, market.tokens.down, market.start_ts,
                    market.end_ts, market.threshold_price, market.threshold_source, seen_at,
                    market.liquidity, market.volume_24h,
                )
                for market in markets
            ],
        )
        self.conn.commit()

    def insert_signals(self, signals: list[ArbSignal]) -> None:
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO signals(
                signal_id, detected_at, asset, end_ts, lower_market_slug,
                higher_market_slug, lower_threshold, higher_threshold,
                long_up_slug, long_down_slug, up_token_id, down_token_id,
                up_ask, down_ask, top_cost, top_edge, avg_cost,
                avg_edge, executable_shares, orientation_source,
                estimated_taker_fee_per_share, net_avg_edge,
                up_book_depth_shares_1pct, down_book_depth_shares_1pct,
                up_book_depth_usdc_1pct, down_book_depth_usdc_1pct,
                up_ask_levels_json, down_ask_levels_json, notes
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    s.signal_id, s.detected_at, s.asset, s.end_ts, s.lower_market_slug,
                    s.higher_market_slug, s.lower_threshold, s.higher_threshold,
                    s.long_up_slug, s.long_down_slug, s.up_token_id, s.down_token_id,
                    s.up_ask, s.down_ask, s.top_cost, s.top_edge, s.avg_cost,
                    s.avg_edge, s.executable_shares, s.orientation_source,
                    s.estimated_taker_fee_per_share, s.net_avg_edge,
                    s.up_book_depth_shares_1pct, s.down_book_depth_shares_1pct,
                    s.up_book_depth_usdc_1pct, s.down_book_depth_usdc_1pct,
                    s.up_ask_levels_json, s.down_ask_levels_json, s.notes,
                )
                for s in signals
            ],
        )
        self.conn.commit()

    def insert_paper_trades(self, trades: list[PaperTrade]) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO paper_trades VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    t.signal_id, t.opened_at, t.asset, t.shares, t.cost,
                    t.worst_case_payout, t.locked_profit_estimate, t.status,
                )
                for t in trades
            ],
        )
        self.conn.commit()

    def existing_paper_trade_ids(self) -> set[str]:
        return {str(row["signal_id"]) for row in self.conn.execute("SELECT signal_id FROM paper_trades")}

    def open_paper_trade_records(self, now_ts: int, grace_seconds: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT
                    t.signal_id, t.opened_at, t.asset, t.shares, t.cost,
                    t.worst_case_payout, t.locked_profit_estimate, t.status,
                    s.end_ts, s.lower_market_slug, s.higher_market_slug,
                    s.lower_threshold, s.higher_threshold,
                    s.orientation_source
                FROM paper_trades t
                JOIN signals s ON s.signal_id = t.signal_id
                LEFT JOIN paper_settlements ps ON ps.signal_id = t.signal_id
                WHERE (ps.signal_id IS NULL OR ps.status IN ('SETTLEMENT_PENDING', 'SETTLEMENT_ERROR'))
                  AND t.status IN ('OPEN', 'RESEARCH_OPEN', 'SETTLEMENT_PENDING', 'SETTLEMENT_ERROR')
                  AND s.end_ts IS NOT NULL
                  AND s.end_ts + ? <= ?
                """,
                (grace_seconds, now_ts),
            )
        )

    def all_paper_trade_records(self, *, limit: int | None = None) -> list[sqlite3.Row]:
        sql = """
            SELECT
                t.signal_id, t.opened_at, t.asset, t.shares, t.cost,
                t.worst_case_payout, t.locked_profit_estimate, t.status,
                s.end_ts, s.lower_market_slug, s.higher_market_slug,
                s.lower_threshold, s.higher_threshold,
                s.orientation_source
            FROM paper_trades t
            JOIN signals s ON s.signal_id = t.signal_id
            WHERE s.end_ts IS NOT NULL
            ORDER BY s.end_ts, t.signal_id
        """
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            return list(self.conn.execute(sql, (limit,)))
        return list(self.conn.execute(sql))

    def insert_paper_settlements(self, settlements: list[PaperSettlement]) -> None:
        if not settlements:
            return
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO paper_settlements(
                signal_id, settled_at, asset, end_ts, shares, cost,
                lower_market_slug, higher_market_slug, lower_threshold, higher_threshold,
                lower_final_price, higher_final_price, up_leg_won, down_leg_won,
                payout, realized_profit, locked_profit_estimate, status, notes
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    s.signal_id, s.settled_at, s.asset, s.end_ts, s.shares, s.cost,
                    s.lower_market_slug, s.higher_market_slug, s.lower_threshold, s.higher_threshold,
                    s.lower_final_price, s.higher_final_price,
                    int(s.up_leg_won) if s.up_leg_won is not None else None,
                    int(s.down_leg_won) if s.down_leg_won is not None else None,
                    s.payout, s.realized_profit, s.locked_profit_estimate, s.status, s.notes,
                )
                for s in settlements
            ],
        )
        self.conn.executemany(
            "UPDATE paper_trades SET status=? WHERE signal_id=?",
            [(s.status, s.signal_id) for s in settlements if s.status in {"SETTLED", "SETTLEMENT_PENDING", "SETTLEMENT_ERROR"}],
        )
        self.conn.commit()

    def all_paper_settlements(self) -> list[PaperSettlement]:
        rows = self.conn.execute("SELECT * FROM paper_settlements ORDER BY settled_at, signal_id").fetchall()
        return [
            PaperSettlement(
                signal_id=str(row["signal_id"]),
                settled_at=str(row["settled_at"]),
                asset=str(row["asset"]),
                end_ts=int(row["end_ts"]),
                shares=float(row["shares"]),
                cost=float(row["cost"]),
                lower_market_slug=str(row["lower_market_slug"]),
                higher_market_slug=str(row["higher_market_slug"]),
                lower_threshold=row["lower_threshold"],
                higher_threshold=row["higher_threshold"],
                lower_final_price=row["lower_final_price"],
                higher_final_price=row["higher_final_price"],
                up_leg_won=bool(row["up_leg_won"]) if row["up_leg_won"] is not None else None,
                down_leg_won=bool(row["down_leg_won"]) if row["down_leg_won"] is not None else None,
                payout=row["payout"],
                realized_profit=row["realized_profit"],
                locked_profit_estimate=float(row["locked_profit_estimate"]),
                status=str(row["status"]),
                notes=str(row["notes"] or ""),
            )
            for row in rows
        ]


def append_csv(path: Path, rows: list[object]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    dicts = [asdict(row) for row in rows]
    fieldnames = list(dicts[0].keys())
    exists = path.exists() and path.stat().st_size > 0
    if exists:
        with path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            existing_header = next(reader, [])
        if existing_header and existing_header != fieldnames:
            backup = path.with_name(f"{path.stem}.schema_mismatch_{int(time.time())}{path.suffix}.bak")
            path.replace(backup)
            exists = False
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerows(dicts)


def write_rows_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
