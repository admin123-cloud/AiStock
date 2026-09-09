"""Make the recoverable boundary of the old institutional-mainwave pool explicit.

This audit does not claim to replay the formal 26 trades.  It separates
signal-date fields from later scheduler labels and records the only verified
hard gate (the canonical historical index_mom60 <= 5% condition).
"""
from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
import sys
from pathlib import Path

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path


# The backup lives outside reports; it is a read-only historical source.
POOL = Path(r"F:\Stock\AiStockResearchArchive\backups\g3_reset_20260620_003808\reports\gen3_pre_2024_10_state_alpha_closure_v5\institutional_mainwave_candidates.csv")
FORMAL = report_path("gen3_score120_formal_institutional_source_v1", "closed_trades.csv")
OUT = report_path("score120_old_pool_selector_boundary_v1")


def _key(frame: pd.DataFrame) -> pd.Series:
    return frame["entry_date"].astype(str) + "|" + frame["code"].astype(str)


def main() -> int:
    pool = pd.read_csv(POOL, encoding="utf-8-sig")
    formal = pd.read_csv(FORMAL, encoding="utf-8-sig")
    pool["formal_selected"] = _key(pool).isin(set(_key(formal)))
    pool["index_gate_pass"] = pd.to_numeric(pool["index_mom60"], errors="coerce").le(0.05)
    pool["boundary_bucket"] = "excluded_by_verified_index_gate"
    pool.loc[pool["index_gate_pass"], "boundary_bucket"] = "index_gate_pass_not_formal"
    pool.loc[pool["formal_selected"], "boundary_bucket"] = "formal_selected"

    safe_fields = [
        "code", "code_raw", "stock_name", "trade_date", "entry_date", "wave_style_score", "amount_rank", "index_close",
        "index_ma20", "index_ma60", "index_mom20", "index_mom60", "rank_key", "template_label", "selected_variant",
    ]
    historical_label_fields = [
        "selected_score", "selected_total", "selected_mean", "selected_win_rate", "selected_cooldown_sum_ret", "selected_cooldown_mean_ret",
        "scheduler", "net_ret", "gross_ret", "policy_exit_date", "exit_price",
    ]
    field_rows = []
    for field in safe_fields:
        if field in pool:
            field_rows.append({"field": field, "role": "signal_or_candidate_metadata", "eligible_for_new_research": True})
    for field in historical_label_fields:
        if field in pool:
            field_rows.append({"field": field, "role": "historical_label_or_downstream_scheduler", "eligible_for_new_research": False})

    OUT.mkdir(parents=True, exist_ok=True)
    pool.to_csv(OUT / "old_pool_with_verified_boundary.csv", index=False, encoding="utf-8-sig")
    pool[pool["formal_selected"]].to_csv(OUT / "formal26_recalled_from_old_pool.csv", index=False, encoding="utf-8-sig")
    pool[pool["index_gate_pass"] & ~pool["formal_selected"]].to_csv(OUT / "index_gate_pass_not_formal22.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(field_rows).to_csv(OUT / "field_eligibility.csv", index=False, encoding="utf-8-sig")

    counts = pool.groupby("boundary_bucket").size().to_dict()
    summary = {
        "status": "completed",
        "old_pool_rows": int(len(pool)),
        "formal_recall": int(pool["formal_selected"].sum()),
        "index_gate_pass": int(pool["index_gate_pass"].sum()),
        "boundary_counts": {str(k): int(v) for k, v in counts.items()},
        "verified_rule": "historical index_mom60 <= 0.05 recalls all formal26 but is not sufficient to reproduce formal selection",
        "prohibited_shortcut": "selected_score and related scheduler outcome fields are historical labels; source-lineage audit found zero direct scheduler trade-key overlap with formal26",
        "next_research_contract": "new candidate generator may use signal-date fields only and must report formal26 recall separately from out-of-sample performance",
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 旧机构主升强势池：可复原边界审计", "",
        "- 这不是旧正式 26 笔的重放器。", "- 它只记录可验证的旧候选池、指数门槛及字段使用边界。", "",
        f"- 旧强势池：`{len(pool)}` 条", f"- 正式 26 笔召回：`{int(pool['formal_selected'].sum())}/26`", f"- `index_mom60 <= 5%` 后：`{int(pool['index_gate_pass'].sum())}` 条", "",
        "## 结论", "", "- 指数门槛是已验证必要条件，不是充分条件。", "- `selected_score` 等调度结果字段不能作为新选股门槛。", "- 新研究只能从信号日字段重新生成候选，并用正式26笔做召回审计。",
    ]
    (OUT / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
