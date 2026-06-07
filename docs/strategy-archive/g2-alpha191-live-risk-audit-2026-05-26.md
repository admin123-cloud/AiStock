# G2 Alpha191 Live Risk Audit - 2026-05-26

## Scope

- Main execution candidate: `g2_alpha191_volume5_keep80_runup`
- Live gate: `volume5_keep80_runup`
- Core logic: D-1 V4 context, D intraday 30m bottom-fractal confirmation, Alpha191 T-1 `volume5` rerank, keep80 weak filter, `runup_from_60d_low <= 100%`.

## Future-Function Check

No hard future-function issue was found in the main Alpha191 overlay path after this audit.

- V4 context for live candidates uses the previous trading day only. In `scripts/gen2_update_live_shadow.py`, `_build_live_contexts` maps `signal_date` to `prev_date = trade_dates[idx - 1]` and uses `events[trade_date == prev_date]`.
- Alpha191 live factors use T-1 daily data. `_load_live_alpha191_values` maps `signal_date` to `factor_date = trade_dates[idx - 1]` and computes the five factors on that factor date.
- Intraday market-state filtering uses index minute closes only up to `confirm_datetime`, with previous-day MA, slope, and breadth. `_filter_with_state(..., "before_confirm")` keeps state rows where `datetime <= confirm_datetime`.
- Preference filters for overhead pressure and 60-day runup are based on history before the entry date, so they are not using future price movement.

## Fix Applied

- Changed `scripts/gen2_update_live_shadow.py` default `--alpha191-train-end` from `2025-12-31` to `2025-03-31`.
- Reason: the selected strategy was evaluated with a train / validation / blind discipline. Letting the live default train the Alpha191 threshold through `2025-12-31` would absorb the validation period into the threshold distribution and weaken the anti-overfitting boundary.

## Live Result Risks Still Not Fully Modeled

These are not classic future functions, but they can make real execution worse than the backtest:

- 30m trigger confirmation: the bottom-fractal logic uses the next 30m bar to confirm. This is valid only if the live monitor waits until the confirming 30m bar has fully closed. Running on a partially formed 30m bar can create false early signals.
- Entry fill: the backtest buys at the confirming 30m close plus 5 bps. In real trading the signal is known at or after bar close, so actual fills may be the next tick or next bar, with extra slippage.
- Limit-up, suspension, and liquidity: the current backtest charges commission, stamp tax, and 5 bps slippage, but it does not fully reject orders that are limit-up, suspended, or too illiquid at the exact execution moment.
- Same-day minute freshness: live signals depend on current-day 30m bars and index minute bars being available in ClickHouse before the monitor run. If data is delayed until after close, email signals will be delayed; if unfinished bars are stored early, signals can be optimistic.
- Share-cap cache: market-cap bucket filters depend on local share-cap history. Missing or stale share-cap data can alter the overhead-pressure bucket, though this is metadata availability risk rather than price lookahead.
- Exit path realism: stop loss and take-profit fills use 30m high/low threshold hits and fill at the threshold with 5 bps slippage. This is a standard approximation, but real gap-through and fast-move execution can be worse.

## Required Live Guardrails

- Restart the backend process after code changes, otherwise the running monitor may still use stale `alpha191_gate` settings.
- Keep the scheduled monitor aligned to completed 30m bars, or add an explicit cutoff that drops bars whose close time is not finalized.
- Add live tradability checks before sending buy emails: not suspended, not ST if excluded by policy, not at limit-up, current quote available, and minimum amount/liquidity satisfied.
- Record every email candidate with `signal_date`, `confirm_datetime`, factor date, Alpha191 score, threshold, data max minute timestamp, and whether the bar was finalized.
