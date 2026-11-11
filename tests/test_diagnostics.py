from pathlib import Path

from polymarket_cross_scanner.config import ScannerConfig
from polymarket_cross_scanner.diagnostics import build_cross_window_report
from polymarket_cross_scanner.storage import write_rows_csv


def test_cross_window_report_groups_aligned_windows_and_reasons(tmp_path):
    cfg = ScannerConfig(root=tmp_path)
    write_rows_csv(
        cfg.cross_scan_timing_csv,
        [
            scan_row("2026-06-04T12:00:00+00:00", pair_candidates=6, pair_fresh_pct=0.5, signals=0),
            scan_row("2026-06-04T12:00:02+00:00", pair_candidates=6, pair_fresh_pct=0.5, signals=0),
            scan_row("2026-06-04T12:05:00+00:00", pair_candidates=6, pair_fresh_pct=1.0, signals=0),
            scan_row("2026-06-04T12:05:02+00:00", pair_candidates=6, pair_fresh_pct=1.0, signals=1),
        ],
    )
    write_rows_csv(
        cfg.cross_hft_paper_heartbeat_csv,
        [
            {
                "ts": "2026-06-04T12:05:01+00:00",
                "pairs": 6,
                "pair_fresh_pct": 1.0,
                "updates_drained": 10,
                "pairs_evaluated": 6,
                "opportunities": 1,
            }
        ],
    )

    report = build_cross_window_report(cfg)

    assert len(report) == 2
    assert report[0].likely_no_trade_reason == "missing_or_unbuyable_pair_leg"
    assert report[1].likely_no_trade_reason == "trade_or_opportunity_seen"
    assert report[1].hft_opportunities == 1


def scan_row(ts: str, *, pair_candidates: int, pair_fresh_pct: float, signals: int) -> dict:
    return {
        "scanned_at": ts,
        "mode": "fast",
        "markets": 100,
        "books": 10,
        "ws_books": 10,
        "rest_books": 0,
        "other_books": 0,
        "missing_books": 0,
        "signals": signals,
        "execution_sims": signals,
        "missed_opportunities": 0,
        "paper_trades": signals,
        "gamma_ms": 0,
        "threshold_ms": 0,
        "store_ms": 0,
        "book_ms": 1,
        "strategy_ms": 1,
        "execution_ms": 0,
        "paper_ms": 0,
        "settlement_ms": 0,
        "total_ms": 2,
        "pair_candidates": pair_candidates,
        "pair_fresh": int(pair_candidates * pair_fresh_pct),
        "pair_missing_up": 0 if pair_fresh_pct == 1.0 else 3,
        "pair_missing_down": 0,
        "pair_fresh_pct": pair_fresh_pct,
        "notes": "",
    }
