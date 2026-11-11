from datetime import datetime, timezone

from polymarket_cross_scanner.gamma import hourly_slug_candidates, slug_candidates


def test_hourly_slug_generation_uses_et_name():
    now = datetime(2026, 6, 1, 14, 41, tzinfo=timezone.utc)
    slugs = [x[0] for x in hourly_slug_candidates("btc", now, offsets=1)]
    assert "bitcoin-up-or-down-june-1-2026-10am-et" in slugs


def test_timestamp_slug_generation_for_5m_15m_4h():
    now = datetime(2026, 6, 1, 14, 41, tzinfo=timezone.utc)
    slugs = [x[0] for x in slug_candidates("btc", [300, 900, 14400], now, offsets=1)]
    assert "btc-updown-5m-1780324800" in slugs
    assert "btc-updown-15m-1780324200" in slugs
    assert "btc-updown-4h-1780315200" in slugs
