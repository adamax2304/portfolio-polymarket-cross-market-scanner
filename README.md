# polymarket_cross_market_scanner

Paper-mode scanner for Polymarket crypto Up/Down cross-window arbitrage.

Read `PROJECT_LOGIC.md` before changing strategy logic. If strategy rules, filters, market discovery, paper execution, or risk assumptions change, update `PROJECT_LOGIC.md` in the same change.

The core deterministic setup is:

```text
same asset + same end time + different start thresholds
buy Up on the lower-threshold market
buy Down on the higher-threshold market
if depth-adjusted cost < 1.00 - buffer
```

The project never places live orders. It only reads public Gamma/CLOB data, optionally listens to Polymarket RTDS Chainlink prices to capture price-to-beat thresholds, logs signals, and opens simulated paper trades.

## Setup

```powershell
cd C:\Users\adama\Documents\polymarket_cross_market_scanner
pip install -r requirements.txt
```

## Commands

One scan:

```powershell
python main.py scan-once --assets btc,eth,sol,xrp,doge,bnb --min-edge 0.01 --min-liquidity 500
```

Run paper scanner for 10 minutes with RTDS threshold capture:

```powershell
python main.py run --seconds 600 --poll-seconds 5 --assets btc,eth,sol,xrp,doge,bnb --min-edge 0.01 --min-liquidity 500
```

Default mode only logs deterministic threshold-known signals. To also log research-only inferred signals:

```powershell
python main.py run --seconds 600 --allow-price-inferred
```

## Outputs

```text
data/scanner.sqlite3
data/thresholds.json
data/signals.csv
data/paper_trades.csv
```

## Implementation Notes

- Uses Gamma `/markets?slug=...` for market discovery.
- Uses CLOB `/book?token_id=...` for book snapshots.
- Supports timestamp slugs for `5m`, `15m`, attempted `30m`, and `4h`.
- Supports hourly human slugs like `bitcoin-up-or-down-june-1-2026-10am-et`.
- Polymarket currently may not list every requested duration for every asset; missing `30m` markets are skipped.
- `threshold_known` signals use captured RTDS price-to-beat thresholds.
- `price_inferred` signals use Up price hints to infer threshold ordering; they are useful for research only and are disabled by default.

## Tests

```powershell
python -m compileall .
pytest -q
```
