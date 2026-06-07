---
name: tdxquant-core
description: Work with TdxQuant in the AiStock project, including reading or updating TdxQuant integrations, using `tqcenter` APIs, mapping local PDF and markdown docs to code, troubleshooting client initialization, building strategy data flows, and handling formula, warning, backtest-feed, or trading interfaces. Use when requests mention TdxQuant, TongDaXin quant APIs, `tqcenter`, `tq.`, the TdxQuant PDF under `docs/`, or the project's TdxQuant datasource and integration code.
---

# TdxQuant Core

## Overview

Use this skill to turn the TdxQuant PDF and project code into reliable implementation guidance.
Start from the project's existing wrappers and docs, then map back to the broader API surface only when needed.

## Workflow

1. Read the local sources before making assumptions.
2. Confirm whether the task is about project integration, data access, sector metadata, formula calls, warnings, backtest feeds, or trading.
3. Prefer the project's established wrapper patterns over inventing direct raw `tqcenter` usage.
4. Treat any order-placement or account-query change as high risk. Preserve existing safety boundaries unless the user explicitly asks to change them.

## Read In This Order

1. Read [references/project-integration.md](references/project-integration.md) for how this repo actually initializes and calls TdxQuant.
2. Read [references/api-guide.md](references/api-guide.md) for the distilled API surface, required parameter shapes, and common pitfalls.
3. Read the source docs only if you need wording or a function not covered above:
   - the TdxQuant PDF under `F:\Stock\AiStock\docs\`
   - `F:\Stock\AiStock\docs\tdxquant_documentation.md`
   - `F:\Stock\AiStock\docs\TDXQUANT_SETUP.md`

## Project-First Rules

Use these files as the main implementation anchors:

- `F:\Stock\AiStock\data_fetcher\sources\tdxquant_pool.py`
- `F:\Stock\AiStock\tests\t1.py`
- `F:\Stock\AiStock\docs\tdxquant_documentation.md`
- `F:\Stock\AiStock\docs\TDXQUANT_SETUP.md`

When code and the PDF differ, prefer:

- current project behavior for local fixes
- the distilled API guide for new feature work
- the PDF only as source-of-truth backup when the other two are incomplete

## Operating Constraints

Always verify these assumptions before claiming something "just works":

- `tqcenter` is available from the TDX client plugin path, commonly `D:\TDX\PYPlugins\user`
- initialization happens before any data call
- the client is running and logged in for most operations
- some data requires client-side downloads or cache refresh before the API returns useful results
- returned structures are often dict-like field mappings or lists, not one flat DataFrame

## Safety Boundaries

Be careful with:

- `order_stock`, `cancel_order_stock`, account queries, and any live-trading flow
- changes that alter initialization path handling or singleton lifecycle in `tdxquant_pool.py`
- broad refactors that bypass the pool or scatter raw `tq` calls across the codebase

If the request touches trading behavior, preserve the existing default behavior unless the user explicitly asks to expand it.

## Output Expectations

When completing a TdxQuant task:

- explain which local file or wrapper owns the behavior
- mention any client or runtime prerequisite that could block success
- keep examples in the repo's existing code style
- prefer minimal, verifiable changes over speculative abstraction

## References

- [references/project-integration.md](references/project-integration.md)
- [references/api-guide.md](references/api-guide.md)
