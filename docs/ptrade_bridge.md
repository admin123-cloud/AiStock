# AiStock PTrade file bridge

This bridge lets AiStock create order intents locally while PTrade performs the
broker-authorized live order inside its own strategy runtime.

## Files

- AiStock pending orders: `data/runtime/ptrade_bridge/pending/*.json`
- Claimed orders being executed by PTrade: `data/runtime/ptrade_bridge/processing/*.json`
- PTrade acknowledgements: `data/runtime/ptrade_bridge/acks/*.json`
- PTrade heartbeat: `data/runtime/ptrade_bridge/status/latest.json`
- PTrade latest positions: `data/runtime/ptrade_bridge/positions/latest.json`
- PTrade latest broker orders: `data/runtime/ptrade_bridge/orders/latest.json`
- Async cancel requests: `data/runtime/ptrade_bridge/cancel_requests/*.json`
- PTrade cancel acknowledgements: `data/runtime/ptrade_bridge/cancel_acks/*.json`
- Errors: `data/runtime/ptrade_bridge/errors/*.json`
- PTrade strategy template: `scripts/ptrade_file_bridge_strategy.py`
- PTrade local API runner: `scripts/ptrade_file_bridge_api_runner.py`
- PTrade acceptance gate: `scripts/ptrade_bridge_acceptance.py`

## Safety defaults

- New orders default to `dry_run=true`.
- New orders default to `require_approval=true`.
- Live orders are capped by `max_order_value` in `config.json`; the default
  PTrade-side cap is 20,000 when no config value is present.
- The PTrade template has `ENABLE_LIVE_ORDER = False`.

For live trading, first confirm PTrade login, run one dry-run file, then switch
`ENABLE_LIVE_ORDER = True` and submit a very small test order with
`approved=true`.

## Recommended path: PTrade internal strategy

The current Xiangcai PTrade client can log in successfully, but the standalone
`PTradeQuantApi.get_portfolio()` probe may hang if PTrade has not started its
quant trading runtime. For live automation, prefer running
`scripts/ptrade_file_bridge_strategy.py` inside PTrade:

Build a local deployment package first:

```powershell
python F:\Stock\AiStock\scripts\ptrade_bridge_deploy_package.py
```

This writes `reports/ptrade_bridge_deploy_package` with the PTrade strategy
copy, a safe `config.json` sample, a local preflight result, the latest
read-only readiness audit, and a short README for the PTrade terminal operator.
Package generation is local only: it never submits orders and never calls
PTrade.

Before pasting the strategy into PTrade, run the local preflight explicitly when
you need to validate a target bridge path:

```powershell
python F:\Stock\AiStock\scripts\ptrade_bridge_preflight.py
```

The preflight creates missing bridge subdirectories, writes and removes one
canary file under `status`, validates `config.json` if present, verifies that
the strategy `BRIDGE_DIR` matches the target bridge directory, and confirms
`ENABLE_LIVE_ORDER = False`. It also embeds the read-only readiness audit, but
missing heartbeat or missing dry-run ack is not a local preflight failure before
the PTrade strategy has been started.

1. Open PTrade and log in.
2. Go to `量化 -> 交易`.
3. Create or upload a strategy using the packaged
   `ptrade_file_bridge_strategy.py`.
4. Create a stock trading task from that strategy.
5. Keep `ENABLE_LIVE_ORDER = False` for the first smoke test.
6. After a dry-run ack is visible in `data/runtime/ptrade_bridge/acks`, switch
   `ENABLE_LIVE_ORDER = True` only for a small approved test order.

The strategy consumes AiStock order-intent JSON files from
`data/runtime/ptrade_bridge/pending` and writes acknowledgements locally. Live
orders still require `dry_run=false`, and if `require_approval=true` then also
`approved=true`.

The PTrade internal strategy performs a second local safety check before any
live broker call: code/side/quantity/limit price must be valid, A-share buys
must use a 100-share multiple, and `quantity * price` must not exceed
`max_order_value`. Dry-run and waiting-approval acknowledgements are not blocked
by this cap because they do not send broker orders.

The internal strategy writes `status/latest.json` every poll. Position/order
snapshots are intentionally disabled by default (`ENABLE_POSITION_SNAPSHOT =
False`, `ENABLE_ORDER_SNAPSHOT = False`) because account queries can be slower
or unstable in some PTrade runtimes. After dry-run order consumption is stable,
you can enable them at a low frequency such as 300 seconds. Order consumption
and ack writing must remain the priority.

The heartbeat is also the primary operations signal. In addition to
`updated_at`, it includes queue telemetry such as `pending_count`,
`processing_count`, `cancel_request_count`, `oldest_pending_age_seconds`,
`oldest_processing_age_seconds`, `total_order_processed`,
`total_cancel_processed`, `total_bridge_errors`, `last_order_poll`, and
`last_cancel_poll`. These fields prove whether the PTrade strategy is actively
consuming the file queue without needing to query broker positions or orders on
every page refresh.

## Non-blocking live-order model

AiStock order submission must stay a local filesystem operation: the backend
validates the G2 candidate and writes one JSON file into `pending`, then returns
to the caller. It must not wait for quote snapshots, ClickHouse inserts, PTrade
account queries, or PTrade order acknowledgements.

Both `/trading/gen2/paper-order` and `/trading/ptrade/bridge/orders` return
`submit_elapsed_seconds`. This value measures only the synchronous local submit
path. Dry-run probes and acceptance gates may wait for PTrade acknowledgements,
but the normal order-submit APIs must not.

The PTrade side atomically moves each order from `pending` to `processing`
before execution. After dry-run, waiting-approval, submitted, or error handling,
the file is removed from `processing` and the result is written to `acks` or
`errors`. This prevents a slow PTrade terminal or a repeated polling cycle from
blocking normal order discovery/submission or repeatedly processing the same
pending order.

Cancel follows the same non-blocking principle. If an order is still in
`pending`, AiStock removes it locally and records it in `canceled`. If it has
already left `pending`, AiStock writes `cancel_requests/{order_id}.json` and
returns immediately. The PTrade strategy consumes that request asynchronously and
writes `cancel_acks/{order_id}.json`. Missing broker order ids are reported as
`cancel_unavailable` instead of blocking the API request.

If a PTrade terminal exits after claiming a file but before writing an ack, the
order remains in `processing`. AiStock exposes `processing_count`,
`processing_stale_count`, and `last_processing_age_seconds` in bridge status so
the live page can show the condition. Stale processing files must not be
automatically replayed into `pending`, because the broker terminal may already
have accepted the order. After checking PTrade manually, move stale files to
`errors` with:

```powershell
POST /api/trading/ptrade/bridge/processing/recover-stale
{"max_age_seconds": 300, "reason": "manual PTrade reconciliation done"}
```

For the current G2 simulated-live workflow:

- Keep G2 buy-signal discovery on the normal trading-hours monitor cadence.
- It is acceptable to slow broad latest-quote snapshots and ClickHouse snapshot
  inserts when the machine is under pressure.
- The live page uses split refresh cadences instead of one heavy auto-refresh
  loop: PTrade bridge state and holding quotes refresh around every 30 seconds,
  market gate around every 60 seconds, and G2 shadow/workflow status around
  every 180 seconds. Manual refresh buttons can still force an update.
- Do not put broad quote refresh, database writes, or PTrade account probes in
  the synchronous `/trading/gen2/paper-order` or `/trading/ptrade/bridge/orders`
  request path.
- The live page sends the displayed G2 candidate row as a request snapshot when
  calling `/trading/gen2/paper-order`. The backend requires this snapshot and
  rejects the order quickly if it is missing or mismatched; it must not rebuild
  the whole selection pool before writing `pending`.
- The live page also sends the already displayed market-gate snapshot. The
  backend requires `available/can_open` to be explicit in this request snapshot
  and rejects the order quickly if the snapshot is missing or mismatched; it
  must not read daily index bars, intraday index snapshots, or ClickHouse inside
  the order request.
- To live-submit, create a fresh order with `dry_run=false` and `approved=true`.
  A `waiting_approval` acknowledgement is treated as a completed bridge attempt,
  not as a file that should be edited in place.
- Backend API requests that would live-submit to PTrade (`dry_run=false` and
  either `approved=true` or `require_approval=false`) are refused until local
  bridge readiness passes. Dry-run orders and waiting-approval orders are still
  accepted without this live-submit readiness gate.

Local non-blocking smoke test, without touching the real bridge queue:

```powershell
python F:\Stock\AiStock\scripts\ptrade_bridge_smoke.py --include-real-status
```

This writes an isolated dry-run order, consumes it through the local runner,
checks waiting-approval cleanup, checks stale `processing` recovery, and writes
`reports/ptrade_bridge_smoke/latest.json`. It never sends a live order and never
imports `PTradeQuantApi`.

G2 order-path fast-path probe, also without touching the real bridge queue:

```powershell
python F:\Stock\AiStock\scripts\ptrade_order_path_probe.py
```

This uses an isolated bridge directory and deliberately blocks selection-pool
rebuilds, live market-gate lookup, and ClickHouse calls. It proves the
`/trading/gen2/paper-order` fast path can use request snapshots and write
`pending` without waiting for broad quote snapshots or database reads.

When the PTrade internal strategy is running, the normal bridge status should
show a recent `ptrade_heartbeat_age_seconds`. A stale or missing heartbeat means
AiStock can still write local pending orders, but PTrade is not proven to be
consuming them.

Bridge status mirrors the heartbeat telemetry under `ptrade_strategy_queue`.
When investigating a stuck order, check `ptrade_heartbeat_age_seconds`,
`ptrade_strategy_queue.oldest_pending_age_seconds`,
`ptrade_strategy_queue.oldest_processing_age_seconds`, and
`ptrade_strategy_queue.last_bridge_error` before touching any queue files.

Bridge status also includes a local readiness block:

- `readiness.local_submit_ready`: AiStock can write `pending` locally.
- `readiness.ptrade_heartbeat_recent`: the PTrade internal strategy heartbeat is
  recent.
- `readiness.dry_run_probe_ack_recent`: a recent `ptrade_bridge_live_probe`
  dry-run ack is present.
- `readiness.ptrade_live_order_enabled`: the PTrade strategy heartbeat reports
  `enable_live_order=true`.
- `readiness.ready_for_live_order`: heartbeat, dry-run probe ack,
  `enable_live_order=true`, and no stale `processing` are all true.

This readiness check is intentionally local-file-only. It does not call PTrade,
query quotes, or read ClickHouse, so it can be displayed on the live page without
blocking discovery or order submission.

The same readiness check is also used as the API guard for true live-submit
requests through `/trading/gen2/paper-order` and
`/trading/ptrade/bridge/orders`. This protects against accidentally sending an
approved broker order before PTrade has produced both a recent heartbeat and a
recent `ptrade_bridge_live_probe` dry-run acknowledgement.

Read-only readiness audit:

```powershell
python F:\Stock\AiStock\scripts\ptrade_bridge_readiness_audit.py
```

This writes `reports/ptrade_bridge_readiness_audit/latest.json` and never
submits orders or calls PTrade. It reports three gates:

- `local_submit_ready`: AiStock can write local `pending` files.
- `dry_run_probe_ready`: heartbeat is recent and no stale `processing` exists.
- `live_submit_ready`: dry-run probe ack is recent, PTrade heartbeat reports
  `enable_live_order=true`, no stale processing exists, the queue is empty for
  a small test, and `max_order_value` is positive.

The same audit is exposed through:

```text
GET /api/trading/ptrade/bridge/readiness-audit
```

Read-only operator checklist and evidence summary:

```powershell
python F:\Stock\AiStock\scripts\ptrade_bridge_evidence_report.py
```

This writes:

- `reports/ptrade_bridge_operator_checklist/latest.md`
- `reports/ptrade_bridge_operator_checklist/latest.json`

It never writes the real bridge `data/runtime/ptrade_bridge/pending`, never
calls PTrade, and never queries ClickHouse. By default it refreshes the
isolated `ptrade_order_path_probe` proof first, then summarizes the current
queue, heartbeat, readiness gates, latest probe reports, the latest isolated
order-path nonblocking proof, and the exact remaining actions before a true
cloud-simulation live-submit can be approved. Add
`--skip-order-path-probe-refresh` if you only want to read existing report
files.

Real bridge live probe, read-only by default:

```powershell
python F:\Stock\AiStock\scripts\ptrade_bridge_live_probe.py
```

This only reads `data/runtime/ptrade_bridge`, writes
`reports/ptrade_bridge_live_probe/latest.json`, and reports pending,
processing, stale processing, and heartbeat state.

After the PTrade internal strategy is visible in the terminal and heartbeat is
recent, run one dry-run consumption probe:

```powershell
python F:\Stock\AiStock\scripts\ptrade_bridge_acceptance.py --heartbeat-timeout-seconds 120 --dry-run-timeout-seconds 30
```

The acceptance gate first runs local preflight, then waits for
`status/latest.json` heartbeat, then writes exactly one dry-run probe order and
waits for its ack, then runs the read-only readiness audit. It exits non-zero
until `live_submit_ready=true`, and it never submits a live order.

If you want to start the local wait before operating PTrade, use the bounded
watcher:

```powershell
python F:\Stock\AiStock\scripts\ptrade_bridge_watch_acceptance.py --watch-timeout-seconds 600 --dry-run-timeout-seconds 30
```

It waits for `status/latest.json` heartbeat, then runs the same dry-run
acceptance gate and writes
`reports/ptrade_bridge_watch_acceptance/latest.json`. It exits on timeout and
never submits live orders.

After `live_submit_ready=true` is visible and the broker/account screen has
been manually confirmed, the final cloud-simulation proof is one explicitly
approved small live-submit test:

```powershell
python F:\Stock\AiStock\scripts\ptrade_bridge_live_submit_test.py --approve-live-submit --code 600000 --price 10.5 --quantity 100
```

This script refuses to write any live order unless `--approve-live-submit` is
present, the readiness audit reports `live_submit_ready=true`, the bridge queue
is empty, the order value is within both the CLI cap and PTrade config cap, and
the buy quantity is a 100-share multiple. It writes
`reports/ptrade_bridge_live_submit_test/latest.json` and measures the local
`submit_elapsed_seconds`; waiting for the acknowledgement is part of this
manual evidence script only, not part of the normal order-submit APIs.

The same guarded test is exposed on the live trading page as the
`live-submit` small acceptance button. The page starts it through a background
task so the browser request returns immediately:

```text
POST /api/trading/ptrade/bridge/live-submit-test/start
GET /api/trading/ptrade/bridge/live-submit-test-task/{task_id}
```

For manual step-by-step probing, run:

```powershell
python F:\Stock\AiStock\scripts\ptrade_bridge_live_probe.py --submit-dry-run --timeout-seconds 30
```

This writes a single `dry_run=true` probe order and waits for `dry_run` or
`waiting_approval` ack. It never sends a live order. If you want the command to
fail when the internal strategy heartbeat is missing or stale, add
`--require-heartbeat`.

The same probe is exposed to the live trading page through:

```text
POST /api/trading/ptrade/bridge/live-probe
```

The page buttons follow the same safety model:

- `刷新桥接状态`: read-only status/orders/fills/positions query.
- `只读探针`: read-only probe; no pending order is written.
- `dry-run 探针`: writes exactly one `dry_run=true` probe order and waits for
  PTrade ack. This is for proving the PTrade internal strategy is consuming the
  queue before any approved live-submit test.

## Optional standalone local API runner

Dry-run once:

```powershell
D:\PTrade\ptrade\Libs\Python\Libs\Python\python3\python.exe F:\Stock\AiStock\scripts\ptrade_file_bridge_api_runner.py --once
```

Probe PTrade API connection without printing account details:

```powershell
D:\PTrade\ptrade\Libs\Python\Libs\Python\python3\python.exe F:\Stock\AiStock\scripts\ptrade_file_bridge_api_runner.py --probe
```

Live loop, only after small-order approval:

```powershell
D:\PTrade\ptrade\Libs\Python\Libs\Python\python3\python.exe F:\Stock\AiStock\scripts\ptrade_file_bridge_api_runner.py --loop --enable-live-order
```

The standalone runner follows the same file-bridge rules as the PTrade internal
strategy. It also writes `status/latest.json` each processing pass, so bridge
readiness can be verified without account-query snapshots.

If `--probe` hangs, stop the probe process and use the internal strategy path
above.
