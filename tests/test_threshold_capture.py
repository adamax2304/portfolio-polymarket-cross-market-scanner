from pathlib import Path
from datetime import datetime, timezone

from polymarket_cross_scanner.config import ScannerConfig
from polymarket_cross_scanner.models import Market, TokenPair
from polymarket_cross_scanner.rtds import PriceState, PriceTick
from polymarket_cross_scanner.scanner import CrossMarketScanner


def test_capture_threshold_uses_earliest_tick_after_start(tmp_path):
    cfg = ScannerConfig(root=tmp_path)
    scanner = CrossMarketScanner(cfg)
    state = PriceState()
    try:
        start = int(datetime.now(timezone.utc).timestamp()) - 5
        market = Market(
            asset="btc",
            duration_sec=300,
            slug=f"btc-updown-5m-{start}",
            question="Bitcoin Up or Down",
            condition_id="c1",
            tokens=TokenPair(up="u", down="d"),
            start_ts=start,
            end_ts=start + 300,
        )
        state.update(PriceTick(asset="btc", price=99.0, ts_ms=(start - 1) * 1000, received_monotonic=0))
        state.update(PriceTick(asset="btc", price=100.0, ts_ms=(start + 2) * 1000, received_monotonic=0))
        state.update(PriceTick(asset="btc", price=101.0, ts_ms=(start + 5) * 1000, received_monotonic=0))
        scanner.capture_thresholds([market], state)
        assert market.threshold_price == 100.0
        assert market.threshold_source == "rtds_live_start_window"
    finally:
        scanner.close()


def test_capture_threshold_rejects_late_first_tick(tmp_path):
    cfg = ScannerConfig(root=tmp_path, threshold_capture_max_after_seconds=45)
    scanner = CrossMarketScanner(cfg)
    state = PriceState()
    try:
        start = int(datetime.now(timezone.utc).timestamp()) - 120
        market = Market(
            asset="btc",
            duration_sec=300,
            slug=f"btc-updown-5m-{start}",
            question="Bitcoin Up or Down",
            condition_id="c1",
            tokens=TokenPair(up="u", down="d"),
            start_ts=start,
            end_ts=start + 300,
        )
        state.update(PriceTick(asset="btc", price=100.0, ts_ms=(start + 90) * 1000, received_monotonic=0))
        scanner.capture_thresholds([market], state)
        assert market.threshold_price is None
    finally:
        scanner.close()
