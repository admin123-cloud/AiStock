# Project Integration

## Purpose

Use this file when the user wants to modify or debug TdxQuant usage inside AiStock rather than discuss the API in the abstract.

## Primary Code Paths

- `F:\Stock\AiStock\data_fetcher\sources\tdxquant_pool.py`
- `F:\Stock\AiStock\tests\t1.py`
- `F:\Stock\AiStock\docs\TDXQUANT_SETUP.md`
- `F:\Stock\AiStock\docs\tdxquant_documentation.md`

## Observed Integration Pattern

The repo uses a singleton-style connection pool in `tdxquant_pool.py`.

Key traits:

- Adds the project root to `sys.path`.
- Adds the TDX plugin path `D:\TDX\PYPlugins\user` to `sys.path`.
- Imports `from tqcenter import tq`.
- Initializes once and keeps the connection warm with a background keepalive thread.
- Verifies connectivity with lightweight list queries before marking the datasource available.

## Practical Implications

- Do not scatter ad-hoc `sys.path` and `tq.initialize(...)` logic in many places if the pool already owns that concern.
- If a bug is local to data fetching, fix the wrapper or caller rather than bypassing the wrapper.
- Preserve thread-safety and retry logic when editing the pool.
- Assume the TDX client may be unavailable at startup and code for retries or degraded status.

## Initialization Notes

The project docs and tests imply these runtime expectations:

- The TDX client/plugin environment must already be installed.
- `tqcenter` is imported from the client plugin directory, not from PyPI.
- Initialization is mandatory before any data call.
- Client-side data preparation may still be required for some datasets.

## Typical Repo Tasks

### Add a new TdxQuant data method

1. Check whether the raw API already exists in `docs/tdxquant_documentation.md`.
2. Decide whether it belongs in `tdxquant_pool.py` or a higher-level datasource wrapper.
3. Match the pool's error handling, status updates, and return conventions.
4. Add a focused test or at least a local example mirroring `tests/t1.py`.

### Diagnose import or init failures

Check in this order:

1. Whether the TDX client path exists on disk.
2. Whether `sys.path` includes the plugin path before import.
3. Whether the client is running and logged in.
4. Whether `tq.initialize(...)` is called with the expected path or script context.

### Diagnose empty or partial data

Check in this order:

1. Whether code, market suffix, and date format match the API expectation.
2. Whether a cache refresh or K-line refresh is required.
3. Whether the requested period is supported.
4. Whether the user is expecting a flat DataFrame when the API actually returns field-to-table mappings.

## High-Risk Areas

Treat these changes cautiously:

- order placement and cancellation
- account and position queries
- any change that modifies the default client path
- refactors that remove the singleton or keepalive behavior
