# G2 Alpha191 T-1 keep80 stop_cd3

## Version

- Strategy code: `g2_alpha191_t1_keep80_stop_cd3_skip`
- Status: shadow live candidate
- Base pool: G2 V3 User V2 risk_cool candidates
- Execution policy: `stop_cd3_skip`
- Candidate role: offensive main-strategy candidate

## Frozen Factor Gate

- Factor timing: T-1 confirmed daily values only
- Factors:
  - `Alpha150`: high is better
  - `Alpha095`: low is better
  - `Alpha144`: high is better
- Score: average of train-distribution percentiles
- Train window: `2024-07-09` to `2025-12-31`
- Gate variant: `keep80`
- Threshold quantile: `0.20`
- Current threshold: `0.3919270833333333`

## Historical Baseline

Full sample `2024-07-09` to `2026-05-21`:

- Signals: `252`
- Trades: `80`
- Total return: `107.20%`
- Max drawdown: `-13.19%`
- Win rate: `63.75%`
- Average trade return: `5.71%`
- Daily Sharpe: `1.87`

Walk-forward OOS combined:

- OOS compounded return: `154.26%`
- Daily Sharpe: `3.28`
- Worst segment drawdown: `-9.29%`
- Signals: `179`
- Trades: `68`

## Promotion Rules

Do not promote this strategy into the main live strategy only because of one backtest. Promote only after shadow/live validation meets all conditions:

- At least `20` shadow or small-capital live trades.
- Return beats the current G2 main comparison line.
- Sharpe beats the current G2 main comparison line.
- Max drawdown is no worse than `1.2x` the comparison line.
- Filtered losers contribute positively.
- Missed strong winners are explainable and acceptable.
- No obvious factor timing leak, same-day daily OHLC leak, or train-percentile leak.

## Live Trial Sizing

- Observation phase: max single position `20%` to `25%`.
- Upgrade phase: max single position `33%` after the first stable sample set.
- Full phase: restore existing G2 sizing only after promotion rules pass.

## Required Daily Audit Fields

Each live or shadow signal should keep:

- `alpha191_factor_date`
- `alpha191_gate_variant`
- `alpha191_gate_score`
- `alpha191_gate_threshold`
- `alpha191_gate_pass`
- `alpha191_alpha150_value`
- `Alpha150_train_pct`
- `alpha191_alpha095_value`
- `Alpha095_train_pct`
- `alpha191_alpha144_value`
- `Alpha144_train_pct`
- G2 candidate reason and risk-cool filter reason

