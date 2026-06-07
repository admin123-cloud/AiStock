# V5 Standard Backtest Protocol

## Goal

This protocol defines the only acceptable backtest path for `V5` before any live-trading consideration.

The target is not to maximize a short-term Sharpe ratio by changing replay logic. The target is to build a single, reproducible, auditable test standard and then evaluate whether `Sharpe >= 2.0` is real or illusory.

## Scope

This protocol applies to:

- V5 stock selection
- V5 market timing
- V5 position sizing
- V5 buy/sell execution
- A/B comparison of `14:50 tail buy` vs `next-day open buy`

This protocol explicitly forbids:

- reuse of old `V4` or `V4.2` trade artifacts as signal inputs
- using only previously traded stocks as the test universe
- mixing strict intraday replay with silent `daily_eod_fallback` and then reporting one combined Sharpe
- comparing variants with different sell rules, different costs, or different position caps

## Standard Universe

The daily candidate universe must start from the full A-share stock universe in `stocks` where:

- `type = 'stock'`
- `quit = 0`

Additional filters may be applied only as strategy rules, not as preselection shortcuts.

Allowed strategy-level stock filters:

- `20-day average turnover >= 2e8`
- `mom5 > 0`
- `close > ma10`
- `vol10` within strategy range
- ST or clearly non-tradable names excluded

The universe must not be replaced by:

- historical holdings only
- historical winners only
- watchlist-only stocks

## Required Data

### Minimum required for strict V5 A/B

- `1d` daily data for the full stock universe
- `15m` minute data for the full stock universe
- `30m` minute data for the full stock universe

### Optional

- `60m` minute data is optional for diagnostics only

Current V5 logic uses:

- daily factors for ranking and breadth
- `15m` bars for final buy/sell confirmation
- `30m` bars for trend confirmation

So a strict V5 replay does **not** require `5m` or `60m`.

## Current Data Reality (2026-04-27 Audit)

### Daily

- stock daily range: `2019-12-31 ~ 2026-04-27`
- latest stock daily coverage: `5510 / 5517`

Daily history is already sufficient for a 5+ year daily-level backtest.

### 15m

- stock range: `2026-01-05 09:45 ~ 2026-04-24 15:00`
- latest stock coverage: `5511 / 5517`

### 30m

- stock range: `2026-01-05 10:00 ~ 2026-04-24 15:00`
- latest stock coverage: `5511 / 5517`

### 60m

- stock range: `2021-04-12 ~ 2026-04-24 15:00`
- but 2021 stock coverage is only `76` codes

Conclusion:

- full 5+ year daily backtest is possible now
- full 5+ year strict intraday V5 backtest is **not** possible now
- `60m` is not a substitute for missing `15m/30m`

## Data Source Strategy

### Daily data

Daily long history can continue to use:

- current local database
- Tushare daily backfill
- AkShare daily backfill as secondary support

### Minute data

Strict V5 replay needs long-history `15m/30m`.

Current external-source reality:

- current `Tushare` token can fetch daily data
- `Tushare stk_mins` limit is too strict for full-universe 5-year backfill
- `AkShare stock_zh_a_minute` is recent-window oriented
- `AkShare stock_zh_a_hist_min_em` and `index_zh_a_hist_min_em` are not yet validated as a stable bulk backfill source in the current environment

### Therefore

Before claiming a strict 5-year intraday V5 result, we must first satisfy one of the following:

1. complete 5-year full-universe `15m/30m` backfill from a stable source
2. or officially downgrade the strict intraday A/B window to the longest complete minute-coverage window

## Standard Signal Definition

### Signal day

Signal generation time for V5 is:

- stock-pool tracking during the session
- final signal snapshot at `14:45`

### Buy execution

Variant A:

- buy at `14:50` on the signal day

Variant B:

- buy at the next trading day open

### Sell execution

Sell rules must be identical across A/B variants.

Allowed sell triggers:

- stop loss
- take profit
- trailing stop
- score drop
- trend break
- portfolio cut

Intraday sell confirmation must use the same minute periods and the same cutoff rules for both A and B.

## Cost Model

All official V5 results must use the same strict friction model:

- buy slippage: `0.20%`
- sell slippage: `0.20%`
- buy commission: `0.03%`
- sell commission: `0.03%`
- sell stamp tax: `0.10%`

This yields roughly `0.36%` round-trip friction before any extra liquidity penalty.

Additional execution constraints:

- limit-up stocks cannot be bought
- limit-down stocks cannot be sold
- if the instrument is suspended or missing required bars, the trade must be marked invalid rather than silently approximated

## Position and Risk Rules

Standard V5 position rules:

- maximum holdings: `3`
- hard portfolio warning threshold: `-8%`
- if hard threshold is hit, disable new buying immediately
- freeze for `1` trading day
- after freeze, resume only under the same standard rules

Position logic must not be changed between A/B variants.

## Allowed vs Forbidden Fallback

### Allowed in exploratory diagnostics

- daily-only fallback to understand factor behavior
- partial minute-replay windows for narrow-sample tests

### Forbidden in official V5 Sharpe reporting

- mixing minute-available dates and minute-missing dates into one "strict intraday Sharpe"
- using end-of-day completed daily bars as if they were known at `14:45`
- reusing V4/V4.2 trade outputs as candidate inputs

If minute data is unavailable for a date, that date must be:

- excluded from strict intraday A/B statistics
- and reported explicitly in coverage metrics

## A/B Test Design

To test whether tail trading improves Sharpe, we must run:

### Shared components

- same universe
- same daily factors
- same signal snapshot time (`14:45`)
- same ranking
- same position rules
- same sell rules
- same cost model
- same limit-up/limit-down constraints

### Variant A

- entry time: `14:50` on signal day

### Variant B

- entry time: next trading day open

### Required outputs

- total return
- annualized return
- Sharpe
- max drawdown
- turnover
- win rate
- average holding days
- exposure ratio
- valid-trade coverage ratio
- excluded-day ratio due to missing minute data

## Trust Rules

### Results we can trust

Only results that satisfy all conditions below:

1. full-universe scan
2. strategy filters applied after universe scan
3. no old artifact reuse
4. explicit cost model
5. explicit price-limit constraints
6. fixed buy/sell timing rules
7. clearly reported minute-coverage ratio
8. backtest period at least 5 years for daily-level claims
9. intraday A/B only reported on dates with full required minute data

### Results that must be discarded

Any result with one or more of the following:

1. old V4/V4.2 artifacts reused as signal inputs
2. holdings-only or traded-stock-only universe
3. mixed strict replay and `daily_eod_fallback` reported as one Sharpe
4. short-window Sharpe reported as a long-term conclusion
5. no slippage / no limit-up / no limit-down constraints
6. different sell logic across A/B variants

## Working Conclusion

As of `2026-04-27`:

- a 5+ year daily-level V5 backtest is possible
- a 5+ year strict intraday V5 A/B is not yet data-complete
- the current valid path is:
  1. standardize the V5 protocol now
  2. keep daily-level 5+ year results separate from intraday A/B results
  3. run strict intraday A/B on the longest complete minute window first
  4. expand only after long-history `15m/30m` becomes truly available
