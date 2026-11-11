from pathlib import Path

from polymarket_cross_scanner.config import ScannerConfig
from polymarket_cross_scanner.probability_settlement import (
    SETTLED_STATUS,
    ProbabilitySettler,
    build_probability_report,
    load_probability_settlements,
)
from polymarket_cross_scanner.storage import write_rows_csv


class FakeGamma:
    def __init__(self, markets):
        self.markets = markets

    def get_market_by_slug(self, slug):
        return self.markets.get(slug)


def decision(**overrides):
    row = {
        "evaluated_at": "2026-06-03T00:00:00+00:00",
        "signal_id": "s1",
        "asset": "btc",
        "market_slug": "btc-updown-5m-1",
        "outcome": "Up",
        "token_id": "token",
        "grade": "A",
        "time_price_bucket": "tte<=300|px<=25c",
        "best_ask": "0.20",
        "calibrated_probability": "0.40",
        "calibrated_edge": "0.19",
        "recommended_cost_usdc": "10",
        "recommended_shares": "50",
        "action": "WOULD_ENTER",
        "expires_at_ts": "1780440100",
    }
    row.update(overrides)
    return row


def test_probability_settlement_computes_up_win_and_down_loss(tmp_path):
    cfg = ScannerConfig(root=tmp_path)
    gamma = FakeGamma(
        {
            "btc-updown-5m-1": {"events": [{"eventMetadata": {"priceToBeat": 100, "finalPrice": 101}}]},
        }
    )
    settler = ProbabilitySettler(cfg, gamma)

    up = settler.settle_row(decision(signal_id="up", outcome="Up"))
    down = settler.settle_row(decision(signal_id="down", outcome="Down"))

    assert up.status == SETTLED_STATUS
    assert up.won is True
    assert up.payout == 50
    assert up.realized_profit == 40
    assert up.roi_on_cost == 4

    assert down.status == SETTLED_STATUS
    assert down.won is False
    assert down.payout == 0
    assert down.realized_profit == -10


def test_probability_settle_due_writes_settlements_and_report(tmp_path):
    cfg = ScannerConfig(root=tmp_path)
    gamma = FakeGamma(
        {
            "btc-updown-5m-1": {"events": [{"eventMetadata": {"priceToBeat": 100, "finalPrice": 101}}]},
            "eth-updown-5m-1": {"events": [{"eventMetadata": {"priceToBeat": 200, "finalPrice": 190}}]},
        }
    )
    rows = [
        decision(signal_id="btc-up", asset="btc", market_slug="btc-updown-5m-1", outcome="Up"),
        decision(signal_id="eth-up", asset="eth", market_slug="eth-updown-5m-1", outcome="Up", best_ask="0.08"),
        decision(signal_id="blocked", action="WOULD_BLOCK", recommended_cost_usdc="10"),
    ]
    write_rows_csv(cfg.probability_risk_decisions_csv, rows)

    processed = ProbabilitySettler(cfg, gamma).settle_due()
    settlements = list(load_probability_settlements(cfg.probability_settlements_csv).values())
    report = build_probability_report(settlements)

    assert len(processed) == 2
    assert len(settlements) == 2
    assert {row.signal_id for row in settlements} == {"btc-up", "eth-up"}
    assert cfg.probability_report_csv.exists()

    total = next(row for row in report if row.group_name == "all")
    assert total.settled == 2
    assert total.wins == 1
    assert total.losses == 1
    assert total.realized_profit == 30

    btc = next(row for row in report if row.group_name == "asset" and row.group_value == "btc")
    eth = next(row for row in report if row.group_name == "asset" and row.group_value == "eth")
    assert btc.realized_profit == 40
    assert eth.realized_profit == -10
