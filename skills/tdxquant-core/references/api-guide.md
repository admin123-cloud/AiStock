# API Guide

## Purpose

Use this file when a task needs the distilled TdxQuant API surface without loading the full PDF.

## Runtime Prerequisites

- Import `tq` from `tqcenter`.
- Call `tq.initialize(...)` before any other API use.
- Run against an active TDX client environment that supports TQ strategy features.
- Expect some datasets to require prior client downloads, cache refresh, or K-line refresh.

## Data Shapes And Conventions

- Stock codes usually use six digits plus market suffix, such as `600519.SH` or `000001.SZ`.
- Dates commonly use `YYYYMMDD`.
- Datetimes commonly use `YYYYMMDDHHMMSS`.
- K-line periods commonly include `1d`, `1w`, `1m`, `5m`, `15m`, `30m`, `60m`.
- Dividend types commonly include `none`, `front`, and `back`.

## Core Function Groups

### Initialization and cache

- `tq.initialize(__file__ or path)`
- `tq.refresh_cache(force=False, market='')`
- `tq.refresh_kline(stock_list, period)`

Use these before blaming downstream data calls. Empty or stale results often come from missing client-side preparation rather than bad business logic.

### Market data

- `tq.get_market_data(...)`
- `tq.get_market_snapshot(stock_list, field_list=[])`

Use `get_market_data` for historical K-line style access.
Expect the return value to be organized by field, with per-stock time series underneath.

Watch for:

- count limits on a single call
- unsupported periods in some cache helpers
- differences between adjusted and unadjusted prices

### Stock lists, sectors, and relationships

- `tq.get_stock_list(market_type=0, list_type=0)`
- `tq.get_sector_list(block_type=1, list_type=0)`
- `tq.get_stock_list_in_sector(sector_code, block_type=1, list_type=0)`
- `tq.get_relation(stock_code)`

Use these for symbol universes, sector constituents, and classification lookups.

### Stock info and financial data

- `tq.get_stock_info(stock_code, field_list=[])`
- `tq.get_financial_data_by_date(stock_list, field_list, year, mmdd)`
- `tq.get_more_info(stock_list, field_list=[])`
- `tq.get_gb_info(stock_code, start_date, end_date)`
- `tq.get_kzz_info(stock_list)`
- `tq.get_trackzs_etf_info()`

Use `get_financial_data_by_date` carefully. It depends on field codes such as `Fn193` and may require financial packages already downloaded in the client.

### Formula integration

- `tq.formula_format_data(...)`
- `tq.formula_set_data(...)`
- `tq.formula_set_data_info(...)`
- `tq.formula_get_data(...)`
- `tq.formula_zb(...)`
- `tq.formula_xg(...)`
- `tq.formula_exp(...)`
- `tq.formula_process_mul_xg(...)`
- `tq.formula_process_mul_zb(...)`

Use these when the task bridges Python output and TDX formulas or screeners.
Keep parameter ordering and expected result shape explicit in code because formula APIs are easy to misuse silently.

### Trading, warning, and backtest feed

- `tq.stock_account()`
- `tq.query_stock_asset()`
- `tq.query_stock_orders()`
- `tq.query_stock_positions()`
- `tq.order_stock(...)`
- `tq.cancel_order_stock(order_id)`
- `tq.send_warn(...)`
- `tq.send_bt_data(...)`

Treat trading calls as high risk.
For warning and backtest feed methods, validate list lengths and timestamp formatting before sending data.

### Other bridge functions

- `tq.send_file(file_path)`
- `tq.download_file(file_type, date)`
- `tq.send_user_block(block_name, stock_list)`
- `tq.exec_to_tdx(func_name, params)`

Use these for client-side interoperability tasks that do not fit the core market-data or trading categories.

## Common Failure Modes

### `ImportError: No module named 'tqcenter'`

- The plugin path is missing from `sys.path`.
- The TDX client/plugin files are not installed where the project expects them.

### Initialization fails

- The client is not running.
- The account/session is not ready.
- The path passed to initialization is inconsistent with the installed environment.

### Data is empty or incomplete

- The market suffix is wrong.
- The period or date format is wrong.
- The client has not refreshed required data.
- The caller misread the nested return structure.

## Source Material

The distilled guidance in this file is based on:

- `F:\Stock\AiStock\docs\TdxQuant接口说明文档.pdf`
- `F:\Stock\AiStock\docs\tdxquant_documentation.md`
- `F:\Stock\AiStock\docs\TDXQUANT_SETUP.md`
