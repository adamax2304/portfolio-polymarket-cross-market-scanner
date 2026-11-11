import json

from polymarket_cross_scanner.rtds import parse_rtds_message


def test_parse_rtds_payload_data():
    msg = {
        "topic": "crypto_prices_chainlink",
        "type": "subscribe",
        "payload": {
            "symbol": "BTC/USD",
            "data": [{"timestamp": 1780324800000, "value": "100000.25"}],
        },
    }
    ticks = parse_rtds_message(json.dumps(msg))
    assert ticks[0].asset == "btc"
    assert ticks[0].price == 100000.25
    assert ticks[0].ts_ms == 1780324800000
