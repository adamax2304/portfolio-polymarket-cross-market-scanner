import sqlite3

from polymarket_cross_scanner.settlement import PaperSettler


class FakeGamma:
    def __init__(self, markets):
        self.markets = markets

    def get_market_by_slug(self, slug):
        return self.markets[slug]


class RefreshingFakeGamma:
    def __init__(self):
        self.calls = {}

    def get_market_by_slug(self, slug):
        count = self.calls.get(slug, 0)
        self.calls[slug] = count + 1
        if count == 0:
            return {"events": [{"eventMetadata": {}}]}
        if slug == "low":
            return {"events": [{"eventMetadata": {"finalPrice": 100.5}}]}
        return {"events": [{"eventMetadata": {"finalPrice": 100.5}}]}


def row_from_dict(data):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE rows(
            signal_id TEXT, asset TEXT, end_ts INTEGER, shares REAL, cost REAL,
            locked_profit_estimate REAL, lower_market_slug TEXT, higher_market_slug TEXT,
            lower_threshold REAL, higher_threshold REAL
        )
        """
    )
    conn.execute(
        "INSERT INTO rows VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            data["signal_id"],
            data["asset"],
            data["end_ts"],
            data["shares"],
            data["cost"],
            data["locked_profit_estimate"],
            data["lower_market_slug"],
            data["higher_market_slug"],
            data["lower_threshold"],
            data["higher_threshold"],
        ),
    )
    return conn.execute("SELECT * FROM rows").fetchone()


def test_paper_settlement_computes_two_leg_payout():
    gamma = FakeGamma(
        {
            "low": {"events": [{"eventMetadata": {"finalPrice": 100.5}}]},
            "high": {"events": [{"eventMetadata": {"finalPrice": 100.5}}]},
        }
    )
    row = row_from_dict(
        {
            "signal_id": "s1",
            "asset": "btc",
            "end_ts": 123,
            "shares": 10,
            "cost": 8.6,
            "locked_profit_estimate": 1.4,
            "lower_market_slug": "low",
            "higher_market_slug": "high",
            "lower_threshold": 100,
            "higher_threshold": 101,
        }
    )
    settlement = PaperSettler(gamma).settle_record(row)
    assert settlement.status == "SETTLED"
    assert settlement.up_leg_won is True
    assert settlement.down_leg_won is True
    assert settlement.payout == 20
    assert round(settlement.realized_profit, 6) == 11.4


def test_paper_settlement_refreshes_stale_missing_final_price():
    row = row_from_dict(
        {
            "signal_id": "s1",
            "asset": "btc",
            "end_ts": 123,
            "shares": 10,
            "cost": 8.6,
            "locked_profit_estimate": 1.4,
            "lower_market_slug": "low",
            "higher_market_slug": "high",
            "lower_threshold": 100,
            "higher_threshold": 101,
        }
    )

    gamma = RefreshingFakeGamma()
    settlement = PaperSettler(gamma).settle_record(row)

    assert settlement.status == "SETTLED"
    assert gamma.calls["low"] == 2
    assert gamma.calls["high"] == 2


def test_paper_settlement_prefers_official_price_to_beat_over_captured_threshold():
    row = row_from_dict(
        {
            "signal_id": "s1",
            "asset": "btc",
            "end_ts": 123,
            "shares": 10,
            "cost": 9,
            "locked_profit_estimate": 1.0,
            "lower_market_slug": "lower",
            "higher_market_slug": "higher",
            "lower_threshold": 99.0,
            "higher_threshold": 101.0,
        }
    )
    gamma = FakeGamma(
        {
            "lower": {"events": [{"eventMetadata": {"priceToBeat": 100.0, "finalPrice": 99.5}}]},
            "higher": {"events": [{"eventMetadata": {"priceToBeat": 101.0, "finalPrice": 100.5}}]},
        }
    )

    settlement = PaperSettler(gamma).settle_record(row)

    assert settlement.lower_threshold == 100.0
    assert settlement.up_leg_won is False
    assert settlement.down_leg_won is True
    assert settlement.payout == 10
