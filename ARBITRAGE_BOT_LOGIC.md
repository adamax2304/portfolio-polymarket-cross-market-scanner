# Cross-Market Arbitrage Bot Logic

This document is the detailed working spec for the cross-market arbitrage paper bots.

## Strategy

The bot looks for deterministic crypto Up/Down arbitrage across Polymarket markets that share:

```text
same asset
same resolution timestamp
different price-to-beat thresholds
```

For two markets:

```text
lower threshold market  -> buy Up
higher threshold market -> buy Down
```

If thresholds are correctly ordered, the pair has minimum payout of 1 share:

```text
final below both thresholds      -> Down on higher threshold wins
final between thresholds         -> both legs win
final above both thresholds      -> Up on lower threshold wins
```

The bot only uses `threshold_known` orientation by default. `price_inferred` remains research-only and is not enabled in autostart.

## Market Discovery

Gamma discovery runs every 60 seconds by default.

The bot watches:

```text
assets: btc, eth, sol, xrp, doge, bnb
durations: 5m, 15m, 30m, 1h, 4h
```

Discovery finds nearby active markets, applies cached/captured thresholds, and builds candidate pairs grouped by:

```text
(asset, end_ts)
```

Important live behavior:

```text
pair_candidates can be 0 outside aligned-expiry windows.
This is normal for strict deterministic arbitrage.

Example:
  at 12:35 UTC the active 5m market may resolve at 12:40
  while the active 15m market resolves at 12:45
  so there is no deterministic cross-market pair yet.

  at 12:40 UTC a new 5m market starts and resolves at 12:45
  matching the active 15m market, so one pair per asset can appear.
```

The bot should therefore be judged by:

```text
when pair_candidates > 0:
  pair_fresh_pct should be near 1.0
  subscribed_tokens should match 2 * pairs in HFT-paper
  updates_drained / pairs_evaluated should move from WS and warm snapshots
```

## Threshold Capture

Thresholds come from:

```text
Gamma event metadata priceToBeat when available
RTDS Chainlink tick around market start
data/thresholds.json cache
```

Priority rule:

```text
Gamma eventMetadata.priceToBeat is authoritative when available.
data/thresholds.json must not overwrite Gamma metadata.
Paper settlement also prefers official Gamma priceToBeat/finalPrice over recorded/captured thresholds.
```

RTDS threshold capture prefers the earliest tick after market start, with a small before-start fallback. Captured thresholds are persisted and reused.

## Order Book Data Path

The bot uses a hybrid book path:

```text
WS cache is the hot path
REST/bridge warm-up keeps quiet books fresh
execution simulator can refetch live books before deciding fill quality
```

Fast evaluation still reads from the WS cache only. REST is not called inside every 2-second strategy evaluation.

## Book Freshness

Problem solved:

```text
CLOB WS can stay silent for passive legs.
If a passive leg has no recent WS update, a pure WS-only scanner may mark it missing even though there is standing liquidity.
```

Current solution:

```text
build the exact pair-leg token set
warm only those tokens via REST/bridge
write warm snapshots back into the WS cache
mark warm snapshots as update events for HFT-paper
```

Default warm-up settings:

```text
cross_book_warm_interval_seconds = 15
cross_book_warm_stale_after_seconds = 20
cross_book_warm_max_tokens_per_cycle = 160
cross_book_warm_near_expiry_seconds = 120
cross_book_warm_near_interval_seconds = 5
```

Near expiry dynamic warm-up:

```text
if any active watched market has <=120 seconds to expiry:
  warm every 5 seconds
else:
  warm every 15 seconds
```

## WS Sharding

The CLOB WS cache supports multiple subscription shards.

Default:

```text
clob_ws_max_assets_per_conn = 500
```

If subscribed token count exceeds the per-connection limit:

```text
token IDs are sorted and split into chunks
one WS connection is opened per chunk
each shard reconnects if chunk membership changes
```

This avoids silently watching only the first 500 tokens.

## Baseline Cross Scanner

Autostart command uses:

```text
main.py run
cross_discovery_interval_seconds = 60
cross_eval_interval_seconds = 2
clob_ws_enabled = true
cross_fast_ws_only_books = true
cross_execution_refetch_delay_ms = 0
cross_execution_entry_slippage = 0.01
cross_execution_rescue_extra_cost = 0.05
cross_min_net_edge = 0.01
```

Per cycle it writes:

```text
data/signals.csv
data/cross_market_execution_sim.csv
data/cross_market_missed_opportunities.csv
data/paper_trades.csv
data/paper_settlements.csv
data/cross_market_scan_timing.csv
```

`cross_market_scan_timing.csv` includes pair coverage:

```text
pair_candidates
pair_fresh
pair_missing_up
pair_missing_down
pair_fresh_pct
```

Interpretation:

```text
pair_fresh_pct is the main health metric for "are we able to evaluate all candidate arbitrage pairs?"
For entry detection a book is considered usable when ask depth exists.
Bid depth is useful for unwind simulation but is not required to detect a buyable arbitrage pair.
```

## HFT-Paper Scanner

`cross-hft-paper` is a paper-only event-driven collector. It does not write normal `paper_trades.csv`.

It evaluates opportunities from three trigger sources:

```text
ws_update       real CLOB WS update
warm_snapshot   REST/bridge warm-up refreshed a stale pair-leg book
periodic_sweep  safety sweep evaluated all active pairs
```

The event source is written in:

```text
data/cross_hft_paper_events.csv -> trigger_source
```

The HFT loop:

```text
drain WS/warm update events
map token IDs to impacted pair IDs
evaluate impacted pairs
periodically sweep all pairs every 2 seconds
write opportunities and execution sims in batches
write heartbeat every 10 seconds
```

HFT heartbeat includes:

```text
updates_drained
pairs_evaluated
opportunities
execution_sims
missed_opportunities
pair_fresh
pair_missing_up
pair_missing_down
pair_fresh_pct
```

## Signal Filters

For both baseline and HFT-paper:

```text
min_edge = 0.01 gross
cross_min_net_edge = 0.01 after estimated taker fees
min_shares = 5
max_paper_shares = 25
max_orderbook_age_seconds = 120
```

The strategy walks both ask books together and requires equal shares on both legs.

## Execution Simulation

For each signal, the execution simulator:

```text
refetches both books
tries simultaneous strict two-leg fill
falls back to first-leg/second-leg logic only if needed
uses dynamic sizing
uses rescue budget if first leg fills and second leg needs a worse hedge
tries profitable unwind before rescue if possible
```

Normal locked actions:

```text
BOTH_FILL_SIMULTANEOUS
BOTH_FILL_STRICT
```

Rescue/damage-control actions:

```text
HEDGE_RESCUE
UNWIND_PROFIT
UNWIND_LOSS
NO_FIRST_LEG_FILL
NO_DYNAMIC_SIZE_FILL
ONE_LEG_STUCK
```

Current paper trade opening only counts normal locked entries by default.

## What Still Can Miss Opportunities

Known residual risks:

```text
Gamma discovery may miss a newly created market until next 60s refresh
threshold may be unknown until RTDS/Gamma captures it
liquidity can disappear between paper observation and real order submission
Polymarket API/WS latency can delay book updates
queue position is not modeled
fees/rebates are estimated, not reconciled from fills
```

## Next Live-Readiness Steps

Before live:

```text
add order-intent simulator with exact live API order types
record submit/cancel/fill latency from the real CLOB bridge
add kill switch and hard loss limits
add per-market max exposure
add balance/allowance checks
add order reconciliation and settlement/redeem tracking
keep threshold_known-only gate
```

## Live-Like Paper Realism Layer

Added on 2026-06-05 for unrestricted paper research across all aligned-expiry cross-market pairs.

Paper mode intentionally does not restrict market duration combinations while data is being collected. Any pair can be tested when it has the same asset and the same `end_ts`, including:

```text
5m vs 15m
5m vs 30m
5m vs 1h/hourly
5m vs 4h
15m vs 30m
15m vs 1h/hourly
15m vs 4h
30m vs 1h/hourly
30m vs 4h
1h/hourly vs 4h
```

The new live-like layer writes:

```text
data/cross_market_livelike_ledger.csv
data/cross_market_livelike_report.csv
data/cross_market_livelike_summary.md
```

It combines three sources:

```text
paper_trades.csv + paper_settlements.csv
cross_market_execution_sim.csv
cross_hft_paper_execution_sim.csv
```

`paper_trades.csv` remains append-only. The live-like ledger synchronizes paper trade status from `paper_settlements.csv` so settled/open trade state can be audited without mutating raw history.

Every execution attempt is counted, not only successful paper trades:

```text
BOTH_FILL_SIMULTANEOUS
BOTH_FILL_STRICT
HEDGE_RESCUE
UNWIND_PROFIT
UNWIND_LOSS
NO_FIRST_LEG_FILL
NO_DYNAMIC_SIZE_FILL
ONE_LEG_STUCK
```

Modeled PnL rules:

```text
BOTH_FILL_*: payout = shares, profit = shares - final_pair_cost
HEDGE_RESCUE: payout = shares, profit = shares - final_pair_cost, can be negative
UNWIND_*: profit = unwind_proceeds - first_leg_cost
NO_FIRST_LEG_FILL / NO_DYNAMIC_SIZE_FILL: profit = 0
ONE_LEG_STUCK: conservative profit = -unhedged_worst_case_loss
```

Source-risk is labeled, not filtered:

```text
LOW_SAME_UPDOWN_FAMILY
RESEARCH_CROSS_FAMILY
MEDIUM_SAME_DURATION_CROSS_FAMILY
HIGH_PRICE_INFERRED
UNKNOWN_SOURCE
```

CrossFamily resolution audit, 2026-06-05:

```text
audit command:
  python main.py audit-cross-family --sleep-seconds 0.5

audit files:
  data/cross_family_resolution_audit.csv
  data/cross_family_resolution_audit_summary.md

result after resettling paper trades through official Gamma metadata:
  CrossFamily rows audited = 24
  threshold mismatches = 0
  outcome mismatches = 0
  official CrossFamily PnL = 80.5544
```

Important interpretation:

```text
RESEARCH_CROSS_FAMILY is not a guaranteed same-oracle arbitrage bucket.
Hourly *-up-or-down-* crypto markets can resolve from Binance USDT 1H candles.
Short/rolling *-updown-* markets can resolve from Chainlink USD data streams.

The strategy math is deterministic only when both legs settle from the same effective final price source.
With mixed Binance/Chainlink sources, both legs can theoretically lose if the Chainlink final is below the lower threshold while the Binance final is above/equal to the higher threshold.
The current audited sample did not show both-leg-loss cases, but that is empirical, not a guarantee.
```

Live rule:

```text
LOW_SAME_UPDOWN_FAMILY is the first live-candidate bucket.
RESEARCH_CROSS_FAMILY requires explicit opt-in, tiny caps, official priceToBeat on both legs, source-pair labeling, and a source-basis safety buffer.
```

## Telegram Dashboard And Live Guard

Added before live launch preparation.

Command:

```text
python main.py telegram-dashboard --env-file .env
```

The dashboard uses Telegram inline buttons, not a reply keyboard. It supports:

```text
Stats
Last 15 trades
Balance via live CLOB bridge
Health
Pause bot
Enable bot
Live guard
Refresh
```

Control state file:

```text
data/control_state.json
```

Fields:

```text
trading_enabled = global paper/live entry switch
live_armed = explicit live guard flag
updated_at / updated_by / reason = audit trail
```

When `trading_enabled=false`, scanners continue collecting market/book data, but they do not open new paper trades or HFT-paper opportunity rows. Future live order submission must check both:

```text
trading_enabled == true
live_armed == true
```

Polymarket CLOB minimum order size:

```text
clob_min_order_size_shares = 5
min_shares is clamped to at least 5
```

This matches Polymarket CLOB market info `mos` and avoids known CLOB rejects for orders below 5 shares.

The report also applies a non-blocking latency haircut:

```text
cross_livelike_latency_floor_ms = 250
cross_livelike_latency_edge_haircut_per_100ms = 0.001
```

This means the bot still records all paper opportunities, but the report shows whether the edge survives a conservative live-like latency cost.

## Cross HFT Live Mode

Added on 2026-06-05 for tiny-size live launch.

Command:

```text
python main.py cross-hft-live --seconds 0 --assets btc,eth,sol,xrp,doge,bnb --min-edge 0.01 --cross-min-net-edge 0.01 --cross-live-max-shares 5 --cross-live-max-daily-cost-usdc 22 --cross-live-max-open-arbs 2 --cross-live-redeem-interval-seconds 300
```

Live mode reuses the websocket-triggered HFT scanner. It still writes HFT paper opportunities, execution simulations, missed opportunities, and live-like reports, but it can also submit real CLOB FOK limit orders when every guard passes.

Live guards:

```text
cross_live_enabled = true only in cross-hft-live command
trading_enabled must be true
live_armed must be true
execution simulator final_action must be BOTH_FILL_STRICT or BOTH_FILL_SIMULTANEOUS
price_inferred is disabled unless explicitly allowed
research cross-family is allowed by default for current tiny-size test
shares are capped by cross_live_max_shares and floored only downward to CLOB min-size steps
daily estimated live cost is capped by cross_live_max_daily_cost_usdc
simultaneous open arbitrage positions are capped by cross_live_max_open_arbs
bridge path must exist and collateral balance must cover estimated cost
each live BUY leg must satisfy CLOB min notional, currently 1 USDC
if a 5-share cheap leg is below 1 USDC, live mode can pad only that leg's size to the CLOB minimum
min-notional padding is capped by clob_min_notional_max_extra_usdc, currently 1.25 USDC extra exposure
```

Execution flow:

```text
1. Signal is detected from fresh WS/warmed books.
2. Live-like simulator refetches/checks both legs.
3. Default live mode is `batch_fok`.
4. If simulator locks both legs, both FOK BUY legs are posted through one CLOB `post_orders` batch.
5. Before submission, each leg gets its own live size. Base hedge size stays 5 shares, but a low-price leg can be increased just enough to reach 1 USDC notional.
6. If both batch legs fill, result is `BOTH_FILLED_BATCH`.
7. If neither batch leg fills, no position is opened.
8. If exactly one batch leg fills, bot attempts rescue FOK BUY on the missing leg up to `rescue_max_pair_cost`, using that missing leg's live size.
9. If rescue hedge also fails, bot attempts aggressive FOK SELL unwind ladder on the filled leg, using the actually filled leg size.
10. Every attempt is written to data/cross_market_live_orders.csv.
```

Min-notional padding is intentionally a tiny-balance live workaround. Example: with base 5 shares and a 0.04 cheap leg, CLOB would reject `0.04 * 5 = 0.20 USDC`. The bot can instead send 25 shares on that cheap leg and 5 shares on the other leg. The first 5 shares remain the hedged arbitrage pair; the extra cheap shares are small optional exposure. If the padded pair would exceed balance, daily cap, or the extra-notional cap, the bot skips instead of forcing the trade.

Order confirmation is two-step. The bot checks the immediate CLOB response and then calls `get_order` when the status is not clearly filled. This avoids treating an ambiguous FOK response as a confirmed hedge.

Live Telegram notifications are sent after every row written to `data/cross_market_live_orders.csv`, including fills, rejects, rescue/unwind outcomes, and skipped live attempts. Dashboard control remains separate from these push alerts.

Important precision: CLOB `post_orders` is a batch submission endpoint, not a documented atomic cross-order arbitrage primitive. It reduces request delay by sending both signed FOK orders together, but the bot still treats the response as per-order and keeps rescue/unwind protection for the one-filled case.

Live order ledger:

```text
data/cross_market_live_orders.csv
```

Open arb limit:

```text
live_result BOTH_FILLED_BATCH, BOTH_FILLED, BOTH_FILLED_RESCUE, BATCH_ONE_FILLED_RESCUE, SECOND_REJECTED_UNWIND_FAILED, and BATCH_ONE_FILLED_UNWIND_FAILED count as open
fully redeemed signals no longer count as open
max simultaneous open arbitrage positions = 2
```

With 5 shares, this allows up to two arbitrage opportunities at once: four filled legs total.

Auto-redeem:

```text
cross_live_redeem_enabled = true
cross_live_redeem_interval_seconds = 300
redemption ledger = data/cross_market_live_redemptions.csv
```

After a live position passes expiry plus settlement grace, the bot checks Gamma finalPrice. If the market appears settled, it attempts redeem fallback:

```text
redeem_eoa_direct -> redeem_direct -> redeem
```

The live redeemer also checks CLOB `get_positions` every redeem cycle. Any actual position with `redeemable=true` is redeemed once even if it is missing from `cross_market_live_orders.csv`. These orphan-position attempts use `signal_id=position:<condition_id>` in the redemption CSV, so balance is less likely to stay stuck because a live fill was not represented in the bot ledger.

Redeem runs as a background task in HFT live mode. Slow RPC, `get_positions`, or on-chain redeem calls must not block heartbeat, scanning, or trading; if a previous redeem task is still running, the next redeem tick is skipped and the scanner keeps going.

The fallback is intentional because old bridge notes show CTF positions may appear under different wallet/funder paths depending on account setup and market vintage.

Discovery fix added after live audit on 2026-06-05:

```text
ThresholdStore is reloaded from data/thresholds.json on every discovery refresh.
```

This matters because baseline, HFT, and probability processes all write or use the same threshold file. A long-running live-HFT process must see thresholds captured by the other processes after it started.

Equal price-to-beat duplicate markets are now handled as:

```text
orientation_source = same_threshold
check A UP + B DOWN
check B UP + A DOWN
```

Before this fix, equal-threshold aligned markets were treated as `unknown` and could make HFT discovery show `pairs=0` even when duplicate-market spread capture was possible.

Important result values:

```text
SKIPPED = guard blocked before any live order
FIRST_REJECTED = first FOK was not confirmed filled
BOTH_FILLED = both legs confirmed
BOTH_FILLED_BATCH = both FOK legs filled from one batch submission
BOTH_FILLED_RESCUE = first leg filled, strict second rejected, worse rescue hedge filled
BATCH_BOTH_REJECTED = both FOK legs rejected from one batch submission
BATCH_ONE_FILLED_RESCUE = one batch leg filled, missing leg filled through rescue
BATCH_ONE_FILLED_UNWIND_FILLED = one batch leg filled, rescue failed, filled leg unwound
BATCH_ONE_FILLED_UNWIND_FAILED = one batch leg filled, rescue failed, unwind failed
SECOND_REJECTED_UNWIND_FILLED = first filled, second rejected, unwind filled
SECOND_REJECTED_UNWIND_FAILED = first filled, second rejected, unwind failed
```

Live rescue hardening added after the 2026-06-06 BNB live audit:

```text
first leg filled
strict second leg rejected
try rescue second leg at rescue_max_pair_cost budget
if rescue second leg rejected, sell first leg through an unwind ladder
unwind ladder starts near current bid and steps down to emergency prices
```

This is intentionally more aggressive than the original single FOK SELL at current bid. The goal is to prefer a controlled small realized loss over holding a naked single leg into resolution.

Live min-notional guard added after the 2026-06-06 batch audit:

```text
Polymarket rejects marketable BUY orders below about $1 notional.
With 5 shares, any BUY leg priced below 0.20 can be invalid even if share size is 5.
The live executor now skips such signals before submitting to CLOB.
```

Live HFT retry/freshness changes added on 2026-06-06 after paper/live divergence audit:

```text
cross-hft-live is the live-aligned HFT process.
It writes HFT paper-style events/sims from the same WS orderbook cache used for live decisions.
Standalone cross-hft-paper is optional through POLY_START_STANDALONE_HFT_PAPER=1 and should not be used as the main live comparison source.
```

Live attempts are no longer one-shot for every `signal_id`:

```text
BOTH_FILLED_BATCH, BOTH_FILLED_RESCUE, BATCH_ONE_FILLED_RESCUE, and unhedged/open-risk results are terminal.
BATCH_BOTH_REJECTED, FIRST_REJECTED, and SKIPPED are retryable.
Retryable signals can be attempted again after cross_live_retry_cooldown_ms.
Each signal is capped by cross_live_max_attempts_per_signal actual non-SKIPPED order submissions.
```

This matters because a FOK reject often means "not fillable at this instant", not "never trade this aligned-expiry opportunity again". If the same HFT signal remains visible and edge survives, live may retry instead of losing the full window after one reject.

Live candidate diagnostics are written to:

```text
data/cross_market_live_candidates.csv
```

Every live-visible candidate logs:

```text
ATTEMPTED
RETRY_COOLDOWN
MAX_ATTEMPTS
TERMINAL_ALREADY_FILLED
```

The candidate row includes source risk, sim action, retry attempt, retryability, estimated cost, visible asks, fresh asks, and the reason. Use it to explain why paper saw an opportunity but live did not submit a new order.

Live guard checks now cache balance/daily_cost/open_arbs briefly:

```text
cross_live_guard_cache_seconds = 1.5
```

This reduces repeated bridge calls during event bursts while keeping the cache short enough for the tiny live bankroll and max-open-arbs guard.

Live execution latency hardening added on 2026-06-06:

```text
cross-hft-live starts polymarket_cross_scanner/fast_clob_bridge_server.py as a persistent child process.
The persistent server loads the legacy live clob_bridge.py once and caches py-clob clients in memory.
Live order submit, rescue, unwind, balance, get_order, get_positions, and redeem calls prefer the persistent bridge.
If the persistent bridge is unavailable, live falls back to the old subprocess bridge.
```

The goal is to remove the 2-3 second Python/SDK cold-start from every hot-path call. Direct balance-call tests after warmup were around 100-250ms instead of about 2.5s.

FOK confirmation policy:

```text
cross_live_confirm_unknown_only = true
```

If a FOK result is clearly filled or clearly rejected/error, the executor does not spend another synchronous get_order roundtrip. It only confirms ambiguous `unknown` order states.

Latency diagnostics are written to:

```text
data/cross_market_live_latency.csv
```

The file breaks live latency into:

```text
submit_ms
confirm_ms
rescue_ms
unwind_ms
balance_ms
total_ms
bridge_mode
```

Use this file instead of only `cross_market_live_orders.bridge_latency_ms` when diagnosing live speed.

Current live deployment starts `cross-hft-live` from `start_bots.ps1`, but actual trading is still controlled by `data/control_state.json`:

```text
live_armed=true
trading_enabled=true
```

If either flag is false, the process can run and collect data but must not submit live orders.

The old dry-run bridge field is:

```text
clob_dry_run_payload_not_submitted
```

This explicitly means no live CLOB order is submitted in paper. Before live, replace this with a real signed-order dry run or tiny-size live rehearsal that records submit latency, rejects, fills, cancels, and order reconciliation.

## Probability Bot Reset

On 2026-06-05 the probability bot was reset to the original early-entry hypothesis:

```text
our fair probability > Polymarket ask + fees + edge buffer
entry window = 10..180 seconds after market start
assets = btc,eth,sol,xrp,doge,bnb
durations are not restricted by the old <=900s expiry window
primary_source = raw
```

Historical bucket calibration is now analysis context only, not the main entry signal.
