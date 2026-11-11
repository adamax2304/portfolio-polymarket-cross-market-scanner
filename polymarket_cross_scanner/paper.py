from __future__ import annotations

from .models import ArbSignal, PaperTrade


class PaperLedger:
    def __init__(self, seen_signal_ids: set[str] | None = None):
        self.seen_signal_ids: set[str] = set(seen_signal_ids or set())

    def open_from_signals(self, signals: list[ArbSignal], max_shares: float) -> list[PaperTrade]:
        trades: list[PaperTrade] = []
        for signal in signals:
            if signal.signal_id in self.seen_signal_ids:
                continue
            shares = min(signal.executable_shares, max_shares)
            if shares <= 0:
                continue
            cost = shares * signal.avg_cost
            trade = PaperTrade(
                opened_at=signal.detected_at,
                signal_id=signal.signal_id,
                asset=signal.asset,
                shares=shares,
                cost=cost,
                worst_case_payout=shares,
                locked_profit_estimate=shares - cost,
                status="RESEARCH_OPEN" if signal.orientation_source == "price_inferred" else "OPEN",
            )
            trades.append(trade)
            self.seen_signal_ids.add(signal.signal_id)
        return trades
