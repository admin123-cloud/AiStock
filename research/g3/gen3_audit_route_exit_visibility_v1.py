from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path

OUT_DIR = report_path("gen3_route_exit_visibility_audit_v1")
CANDIDATES = report_path("gen3_combo_range_filter_v1", "range_conservative_combo_b_candidates.csv")
PANIC_SOURCE = report_path("gen3_panic_v2_research", "final_candidate_v1", "m30_close5_full_nextopen_cost30_closed_trades.csv")
RANGE_SOURCE = report_path("gen3_range_v3_mtm_pressure_v1", "range_v3_weak_low_not_chasing_h5_cost30_closed_trades.csv")


def _date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.normalize()


def _key(df: pd.DataFrame) -> pd.Series:
    return _date(df["entry_date"]).dt.strftime("%Y-%m-%d") + "|" + df["code"].astype(str)


def _md_table(df: pd.DataFrame, max_rows: int = 20) -> str:
    if df.empty:
        return "_无数据_"
    view = df.head(max_rows).copy()
    for col in view.columns:
        view[col] = view[col].astype(str)
    header = "| " + " | ".join(view.columns) + " |"
    sep = "| " + " | ".join(["---"] * len(view.columns)) + " |"
    rows = ["| " + " | ".join(row) + " |" for row in view.to_numpy()]
    suffix = [f"\n\n_仅展示前 {max_rows} 行，共 {len(df)} 行。_"] if len(df) > max_rows else []
    return "\n".join([header, sep, *rows, *suffix])


def _to_record(rows: list[dict[str, Any]], route: str, item: str, value: Any, verdict: str, evidence: str) -> None:
    rows.append({"route": route, "item": item, "value": value, "verdict": verdict, "evidence": evidence})


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    candidates = pd.read_csv(CANDIDATES, low_memory=False)
    candidates["entry_date"] = _date(candidates["entry_date"])
    candidates["policy_exit_date"] = _date(candidates["policy_exit_date"])
    candidates["code"] = candidates["code"].astype(str)
    candidates["_key"] = _key(candidates)
    down = candidates[candidates["route"].eq("down_panic")].copy()
    range_gap = candidates[candidates["route"].eq("range_gap")].copy()

    audit_rows: list[dict[str, Any]] = []

    panic = pd.read_csv(PANIC_SOURCE, low_memory=False)
    panic["entry_date"] = _date(panic["entry_date"])
    panic["exit_date"] = _date(panic["exit_date"])
    panic["exit_datetime"] = pd.to_datetime(panic.get("exit_datetime"), errors="coerce")
    panic["trigger_datetime"] = pd.to_datetime(panic.get("trigger_datetime"), errors="coerce")
    panic["code"] = panic["code"].astype(str)
    panic["_key"] = _key(panic)
    panic_keep = [
        "_key",
        "exit_date",
        "policy",
        "triggered",
        "executable",
        "exit_source",
        "trigger_datetime",
        "exit_datetime",
        "early_exit_fraction",
    ]
    down_join = down.merge(panic[panic_keep].drop_duplicates("_key", keep="first"), on="_key", how="left")
    fixed_hold = down_join["exit_source"].astype(str).eq("fixed_hold")
    executable_30m = down_join["executable"].fillna(False).astype(bool) & down_join["exit_datetime"].notna()
    down_visible = fixed_hold | executable_30m

    _to_record(
        audit_rows,
        "down_panic",
        "source_match",
        f"{int(down_join['exit_source'].notna().sum())}/{len(down_join)}",
        "PASS" if int(down_join["exit_source"].notna().sum()) == len(down_join) else "FAIL",
        "down_panic 候选可回溯到 30m 失败退出源文件。",
    )
    _to_record(
        audit_rows,
        "down_panic",
        "same_day_exit_visibility",
        f"{int(down_visible.sum())}/{len(down_join)}",
        "PASS" if bool(down_visible.all()) else "FAIL",
        "fixed_hold 是入场时可预设的退出日；executable 30m 退出有 trigger/exit datetime。",
    )
    _to_record(
        audit_rows,
        "down_panic",
        "executable_30m_rows",
        int(executable_30m.sum()),
        "INFO",
        "30m 触发退出样本数；其余为固定持有到预设退出日。",
    )

    range_src = pd.read_csv(RANGE_SOURCE, low_memory=False)
    range_src["entry_date"] = _date(range_src["entry_date"])
    range_src["trade_date"] = _date(range_src["trade_date"])
    range_src["policy_exit_date"] = _date(range_src["policy_exit_date"])
    range_src["code"] = range_src["code"].astype(str)
    range_src["_key"] = _key(range_src)
    range_keep = ["_key", "trade_date", "hold_days", "range_v3_variant", "range_v3_family", "policy_exit_date"]
    range_join = range_gap.merge(range_src[range_keep].drop_duplicates("_key", keep="first"), on="_key", how="left", suffixes=("", "_source"))
    hold_known = pd.to_numeric(range_join["hold_days"], errors="coerce").notna()
    signal_before_entry = range_join["trade_date"].notna() & (range_join["trade_date"] < range_join["entry_date"])
    exit_match = range_join["policy_exit_date_source"].isna() | (range_join["policy_exit_date_source"] == range_join["policy_exit_date"])
    range_visible = hold_known & signal_before_entry & exit_match

    _to_record(
        audit_rows,
        "range_gap",
        "source_match",
        f"{int(range_join['hold_days'].notna().sum())}/{len(range_join)}",
        "PASS" if int(range_join["hold_days"].notna().sum()) == len(range_join) else "FAIL",
        "range_gap 候选可回溯到 range_v3 源文件。",
    )
    _to_record(
        audit_rows,
        "range_gap",
        "scheduled_exit_visibility",
        f"{int(range_visible.sum())}/{len(range_join)}",
        "PASS" if bool(range_visible.all()) else "FAIL",
        "trade_date 在 entry_date 前，hold_days 固定且可在入场前确定退出日。",
    )
    _to_record(
        audit_rows,
        "range_gap",
        "hold_days_values",
        ",".join(str(int(x)) for x in sorted(pd.to_numeric(range_join["hold_days"], errors="coerce").dropna().unique())),
        "INFO",
        "range_gap 当前是固定持有短打，不依赖退出日收盘后才知道的条件。",
    )
    _to_record(
        audit_rows,
        "strong_main",
        "same_day_mandate",
        "not_applied",
        "INFO",
        "strong_main 在本轮测试中仍承受 nextopen+haircut2，不使用同日退出修正。",
    )

    audit = pd.DataFrame(audit_rows)
    failures = audit[audit["verdict"].eq("FAIL")].copy()
    audit.to_csv(OUT_DIR / "route_exit_visibility_audit.csv", index=False, encoding="utf-8-sig")
    down_join.to_csv(OUT_DIR / "down_panic_exit_visibility_join.csv", index=False, encoding="utf-8-sig")
    range_join.to_csv(OUT_DIR / "range_gap_exit_visibility_join.csv", index=False, encoding="utf-8-sig")

    meta = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "candidate": "range_conservative_combo_b",
        "down_panic_rows": int(len(down_join)),
        "down_panic_visible_rows": int(down_visible.sum()),
        "range_gap_rows": int(len(range_join)),
        "range_gap_visible_rows": int(range_visible.sum()),
        "failure_count": int(len(failures)),
        "verdict": "PASS" if failures.empty else "FAIL",
        "next_step": "package_v3_route_execution_mandate_if_no_visibility_failures",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report = f"""# G3 路由级退出可见性审计 V1

生成时间：{meta["generated_at"]}

## 目的

上一轮发现 extreme haircut2 的主要问题不是强势链路，而是把 `down_panic/range_gap` 的退出强行拖到次日开盘。此处审计“down/range 同日退出约束”是否会引入未来函数。

## 审计结论

{_md_table(audit)}

## 失败项

{_md_table(failures)}

## 判断

- `down_panic`：固定持有退出日是入场时可预设；30m 失败退出样本必须有 `trigger_datetime/exit_datetime` 才算可见。
- `range_gap`：当前源为固定 `hold_days` 短打，且 `trade_date < entry_date`，退出日可以在入场前确定。
- `strong_main`：本轮没有使用同日退出修正，仍保留 extreme nextopen haircut2 压力。

因此，本审计若为 PASS，只能证明“同日退出约束本身不依赖未来条件”；它还不能证明真实成交价一定等于收盘价。下一步应把该约束包装为 V3 执行口径，并继续用 50/100bps 与跌停延迟压力约束它。"""
    (OUT_DIR / "route_exit_visibility_audit_report_cn.md").write_text(report, encoding="utf-8", newline="\n")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
