# V5 Data Audit and Trust Boundary

Date: `2026-04-27`

## 1. What is already sufficient

### Daily history

- stock daily data exists from `2019-12-31` to `2026-04-27`
- latest stock daily coverage is `5510 / 5517`

This is enough to support:

- 5+ year daily-factor replay
- 5+ year market-breadth replay
- 5+ year stock-pool filtering audit

## 2. What is still missing

### 15m

- full-universe stock minute data only starts from `2026-01-05`

### 30m

- full-universe stock minute data only starts from `2026-01-05`

### 60m

- minute history is longer
- but 2021 coverage is only `76` stocks
- not enough for a full-universe intraday replay

## 3. What V5 actually needs

Strict V5 intraday replay needs:

- `1d` for factor ranking
- `15m` for tail buy and risk sell confirmation
- `30m` for trend confirmation

It does **not** fundamentally require:

- `5m`
- `60m`

## 4. External-source feasibility

### Tushare

Verified:

- daily interface works
- minute interface is heavily rate-limited for the current token

Conclusion:

- Tushare is acceptable for daily backfill
- Tushare is not acceptable as the main source for 5-year full-universe `15m/30m`

### AkShare

Verified:

- package is installed
- `stock_zh_a_minute` can return recent-minute data
- historical minute interfaces are not yet validated as stable bulk backfill sources in this environment

Conclusion:

- AkShare may help for recent/partial repair
- AkShare is not yet a confirmed 5-year full-universe minute backfill solution

## 5. What can be trusted today

### Trustable

1. daily-level 5+ year factor behavior
2. daily-level breadth behavior
3. daily-level universe filtering behavior
4. short-window strict intraday replay on the complete minute window

### Not trustable

1. any 5+ year strict intraday Sharpe claim
2. any result mixing full minute-replay days with fallback end-of-day approximation
3. any result derived from previous strategy artifacts rather than raw market data

## 6. Audit conclusion

### Can be kept

- daily-level 5+ year replay
- intraday A/B tests on the complete minute window

### Must be discarded

- old high-Sharpe V5 results built from non-standard inputs
- any "strict intraday" Sharpe that does not have full `15m/30m` support

## 7. Practical next step

Recommended sequence:

1. lock the standard protocol
2. run a strict A/B test only on the complete minute window
3. separately maintain a 5+ year daily-level audit
4. only after long-history `15m/30m` is solved, upgrade the intraday A/B horizon
