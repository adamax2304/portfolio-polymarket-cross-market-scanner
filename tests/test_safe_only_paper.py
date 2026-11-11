from __future__ import annotations

from polymarket_cross_scanner.safe_only.paper import classify, decide


def _sim(**overrides):
    base = {
        "final_pair_edge": "0.05",
        "final_action": "BOTH_FILL_STRICT",
        "locked_profit_estimate": "0.30",
        "target_shares": "5",
        "simulated_at": "2026-06-08T13:00:00+00:00",
        "signal_id": "test:1",
        "asset": "eth",
        "end_ts": "1780900000",
    }
    base.update(overrides)
    return base


def test_classify_success_when_edge_above_min_and_strict_fill():
    decision, pnl = classify(_sim(), min_edge=0.03)
    assert decision == "SUCCESS_BOTH_FILL"
    assert pnl == 0.30


def test_classify_success_simultaneous_action():
    decision, pnl = classify(_sim(final_action="BOTH_FILL_SIMULTANEOUS"), min_edge=0.03)
    assert decision == "SUCCESS_BOTH_FILL"


def test_classify_edge_below_min_is_skip_edge():
    decision, pnl = classify(_sim(final_pair_edge="0.01"), min_edge=0.03)
    assert decision == "SKIP_EDGE"
    assert pnl == 0.0


def test_classify_rescue_action_is_held():
    decision, pnl = classify(_sim(final_action="HEDGE_RESCUE"), min_edge=0.03)
    assert decision == "HELD_ONE_LEG"
    assert pnl == 0.0


def test_classify_no_first_leg_fill_is_skip_reject():
    decision, _ = classify(_sim(final_action="NO_FIRST_LEG_FILL"), min_edge=0.03)
    assert decision == "SKIP_REJECT"


def test_classify_unknown_action_is_skip_other():
    decision, _ = classify(_sim(final_action="UNRECOGNIZED_THING"), min_edge=0.03)
    assert decision == "SKIP_OTHER"


def test_decide_returns_dataclass_with_pnl():
    d = decide(_sim(), min_edge=0.03)
    assert d.decision == "SUCCESS_BOTH_FILL"
    assert d.expected_pnl == 0.30
    assert d.asset == "eth"
    assert d.min_edge == 0.03
