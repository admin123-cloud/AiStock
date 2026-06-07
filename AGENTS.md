# AiStock-core Agent Rules

## Repository Shape

This repository is the lightweight core codebase. Keep it small and friendly to Codex, Git, and TortoiseGit.

External layers:

- Data/runtime: `F:\Stock\AiStockData`
- Research archive: `F:\Stock\AiStockResearchArchive`
- Original legacy tree: `F:\Stock\AiStock`

## Path Rules

- Use `utils.paths` for runtime/data/report/log paths.
- Prefer `data_path(...)`, `runtime_path(...)`, `cache_path(...)`, `warehouse_path(...)`, and `report_path(...)`.
- Do not add root-level `data/`, `reports/`, `artifacts/`, `logs/`, `Libs/`, `node_modules/`, or generated output directories to this repo.
- Do not create junctions or symlinks from this repo to the external heavy directories unless the user explicitly requests it.

## Secrets

- Do not commit `.env`, `.env.local`, or `config/settings.local.yaml`.
- Keep `config/settings.yaml` safe for version control: no real SMTP passwords, tokens, API keys, or account credentials.

## Migration Notes

- API/runtime modules should be migrated to external paths first.
- Research scripts can be migrated incrementally when they are actively used.
- If a script still assumes `REPO_ROOT / "reports"` or `REPO_ROOT / "data"`, update that script to use `utils.paths` instead of bringing heavy directories back into the core repo.
