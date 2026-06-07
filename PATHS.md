# AiStock External Paths

`AiStock-core` is intentionally small. Runtime data and research artifacts live outside the core repository.

## Default Paths

- Data root: `F:\Stock\AiStockData\data`
- Reports root: `F:\Stock\AiStockResearchArchive\reports`
- Artifacts root: `F:\Stock\AiStockData\artifacts`
- Logs root: `F:\Stock\AiStockData\logs`

## Configuration

The path helper is `utils.paths`.

Environment variables override `config/settings.yaml`:

- `AISTOCK_DATA_ROOT`
- `AISTOCK_REPORTS_ROOT`
- `AISTOCK_ARTIFACTS_ROOT`
- `AISTOCK_LOGS_ROOT`

New code should use `utils.paths.data_path(...)`, `runtime_path(...)`, `cache_path(...)`, `warehouse_path(...)`, or `report_path(...)` instead of hard-coding `REPO_ROOT / "data"` or `REPO_ROOT / "reports"`.
