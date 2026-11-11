from polymarket_cross_scanner.models import Market, TokenPair
from polymarket_cross_scanner.thresholds import ThresholdStore


def market(threshold_price=None, threshold_source="unknown"):
    return Market(
        asset="btc",
        duration_sec=300,
        slug="btc-updown-5m-1",
        question="Bitcoin Up or Down",
        condition_id="condition",
        tokens=TokenPair(up="up", down="down"),
        start_ts=1,
        end_ts=301,
        threshold_price=threshold_price,
        threshold_source=threshold_source,
    )


def test_threshold_store_does_not_override_gamma_event_metadata(tmp_path):
    store = ThresholdStore(tmp_path / "thresholds.json")
    stale = market()
    store.set_threshold(stale, 99.0, "rtds_live_start_window")
    official = market(threshold_price=100.0, threshold_source="gamma_event_metadata")

    store.apply([official])

    assert official.threshold_price == 100.0
    assert official.threshold_source == "gamma_event_metadata"
