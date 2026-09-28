# AiStock unified app Agent Rules

## Repository Shape

This repository is the unified AiStock application source under F:\AiStock\app. Keep runtime data and research artifacts in the sibling directories managed by this root.

External layers:

- Data/runtime: `F:\AiStock\data`
- Research archive: `F:\AiStock\research`
- Unified root: `F:\AiStock`

## Path Rules

- Use `utils.paths` for runtime/data/report/log paths.
- Prefer `data_path(...)`, `runtime_path(...)`, `cache_path(...)`, `warehouse_path(...)`, and `report_path(...)`.
- Runtime data is stored in `F:\AiStock\data`; research reports are stored in `F:\AiStock\research`. Keep generated heavy outputs outside the app source when possible.
- Do not create junctions or symlinks from this repo to the external heavy directories unless the user explicitly requests it.

## Secrets

- Do not commit `.env`, `.env.local`, or `config/settings.local.yaml`.
- Keep `config/settings.yaml` safe for version control: no real SMTP passwords, tokens, API keys, or account credentials.

## Migration Notes

- API/runtime modules should be migrated to external paths first.
- Research scripts can be migrated incrementally when they are actively used.
- If a script still assumes `REPO_ROOT / "reports"` or `REPO_ROOT / "data"`, update that script to use `utils.paths` instead of bringing heavy directories back into the core repo.

## AiStock Project Knowledge — mandatory read/write lifecycle

The canonical project knowledge base is `F:\AiDevelop\ai-workspace\projects\AiStock`.
Before any AiStock startup, investigation, new requirement, fix or refactor, read its `README.md`, `AGENTS.md`, `CURRENT.md`, `LEARNINGS.md`, then the relevant design/requirements/operations pages. Verify the actual checkout and deployment against that record.
At completion update the knowledge base's current state, affected topic, change record and reusable lessons, linking the code commit and validation. Do not declare the task complete before that writeback. If unavailable, report it and continue only independent read-only work.
Keep code, tests, executable configuration and this short pointer here. Do not add project design notes, incident experiences or scattered docs to the code repository. Historical documents are preserved in the knowledge base archives. Machine strategy contracts remain in code; knowledge changes do not automatically change runtime behavior.
