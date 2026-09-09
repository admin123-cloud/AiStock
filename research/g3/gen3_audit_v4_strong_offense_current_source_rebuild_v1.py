from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path  # noqa: E402

OUT_DIR = report_path("gen3_v4_strong_offense_current_source_rebuild_audit_v1")

V4_SOURCE = report_path("gen3_v4_strong_offense_candidate_v1", "g3_v4_strong_offense_closed_trades.csv")
PANIC_FINAL = report_path(
    "gen3_panic_v2_research",
    "final_candidate_v1",
    "m30_close5_full_nextopen_cost30_closed_trades.csv",
)
PANIC_CANDIDATES = report_path("gen3_panic_v2_research", "panic_v2_candidates.csv")
RANGE_NO_FUTURE = report_path("gen3_range_v3_gap_candidate_source_v1", "range_v3_gap_candidate_source_no_future.csv")
RANGE_H10_CLOSED = report_path("gen3_range_v3_gap_candidate_source_v1", "range_v3_weak_low_not_chasing_h10_closed_trades.csv")
STRONG_MAINUP_SOURCE = report_path("gen3_strong_v2_independent_source_v1", "strong_v2_main_up_only_hold5_closed_trades.csv")
STRONG_PLUSWEAK_SOURCE = report_path("gen3_strong_v2_independent_source_v1", "strong_v2_main_up_plus_weak04_hold5_closed_trades.csv")
STRONG_PLUSWEAK_NO_FORWARD = report_path("gen3_strong_v2_independent_source_v1", "strong_v2_main_up_plus_weak04_candidates_no_forward.csv")

FORBIDDEN_PATTERNS = [
    r"^fwd_ret",
    r"^outcome",
    r"^mfe_",
    r"^mae_",
    r"^gross_ret$",
    r"^net_ret$",
    r"^policy_net_ret$",
    r"^baseline_net_ret$",
    r"^exit_ret_net$",
    r"^realized_pnl$",
    r"^exit_value$",
    r"^stake$",
    r"^policy_exit_date$",
    r"^exit_date$",
    r"^exit_price$",
    r"^trigger_close_ret$",
]


def _read_csv(path: Path, label: str) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"{label}: {path}")
    return pd.read_csv(path, low_memory=False)


def _date(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.normalize()


def _key(df: pd.DataFrame, date_col: str = "entry_date") -> pd.Series:
    return _date(df[date_col]).dt.strftime("%Y-%m-%d") + "|" + df["code"].astype(str)


def _forbidden_fields(columns: list[str]) -> list[str]:
    return [col for col in columns if any(re.search(pattern, col) for pattern in FORBIDDEN_PATTERNS)]


def _route_source_map() -> dict[str, pd.DataFrame]:
    panic_final = _read_csv(PANIC_FINAL, "panic_final")
    panic_candidates = _read_csv(PANIC_CANDIDATES, "panic_candidates")
    range_no_future = _read_csv(RANGE_NO_FUTURE, "range_no_future")
    range_h10 = _read_csv(RANGE_H10_CLOSED, "range_h10")
    strong_mainup = _read_csv(STRONG_MAINUP_SOURCE, "strong_mainup_source")
    strong_plusweak = _read_csv(STRONG_PLUSWEAK_SOURCE, "strong_plusweak_source")
    strong_plusweak_no_forward = _read_csv(STRONG_PLUSWEAK_NO_FORWARD, "strong_plusweak_no_forward")

    for df in [panic_final, panic_candidates, range_no_future, range_h10, strong_mainup, strong_plusweak, strong_plusweak_no_forward]:
        df["code"] = df["code"].astype(str)
        df["_key"] = _key(df)

    panic_final["_source_layer"] = "panic_final_intraday_confirm"
    panic_candidates["_source_layer"] = "panic_daily_candidate_pool"
    range_no_future["_source_layer"] = "range_no_future_candidate_pool"
    range_h10["_source_layer"] = "range_h10_closed_trade_source"
    strong_mainup["_source_layer"] = "strong_v2_mainup_intraday_confirm_source"
    strong_plusweak["_source_layer"] = "strong_v2_plusweak_intraday_confirm_source"
    strong_plusweak_no_forward["_source_layer"] = "strong_v2_plusweak_no_forward_pool"

    return {
        "down_panic_final": panic_final,
        "down_panic_pool": panic_candidates,
        "range_gap_pool": range_no_future,
        "range_gap_h10": range_h10,
        "strong_mainup": strong_mainup,
        "strong_plusweak": strong_plusweak,
        "strong_plusweak_no_forward": strong_plusweak_no_forward,
    }


def _attach_route_audit(v4: pd.DataFrame, sources: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for row in v4.itertuples(index=False):
        route = str(getattr(row, "route"))
        entry_date = pd.Timestamp(getattr(row, "entry_date")).strftime("%Y-%m-%d")
        code = str(getattr(row, "code"))
        key = f"{entry_date}|{code}"
        score = float(getattr(row, "score")) if pd.notna(getattr(row, "score")) else None

        if route == "down_panic":
            final = sources["down_panic_final"]
            pool = sources["down_panic_pool"]
            final_hit = final[final["_key"].eq(key)]
            pool_hit = pool[pool["_key"].eq(key)]
            hit = not final_hit.empty or not pool_hit.empty
            source_layer = "panic_final" if not final_hit.empty else ("panic_pool" if not pool_hit.empty else "")
            trade_date = ""
            confirm = ""
            if not final_hit.empty:
                trade_date = ""
                confirm = str(final_hit.iloc[0].get("confirm_datetime", ""))
            if not pool_hit.empty:
                trade_date = pd.Timestamp(pool_hit.iloc[0].get("trade_date")).strftime("%Y-%m-%d")
            visibility_status = "rebuildable_from_daily_pool_and_intraday_final" if hit else "missing_panic_source_key"
            blocker = "" if hit else "panic source key not found"
        elif route == "range_gap":
            pool = sources["range_gap_pool"]
            h10 = sources["range_gap_h10"]
            pool_hit = pool[pool["_key"].eq(key) & pool.get("range_v3_variant", pd.Series("", index=pool.index)).astype(str).eq("range_v3_weak_low_not_chasing_h10")]
            h10_hit = h10[h10["_key"].eq(key)]
            hit = not pool_hit.empty or not h10_hit.empty
            source_layer = "range_no_future" if not pool_hit.empty else ("range_h10_closed" if not h10_hit.empty else "")
            trade_date = ""
            confirm = ""
            if not pool_hit.empty:
                trade_date = pd.Timestamp(pool_hit.iloc[0].get("trade_date")).strftime("%Y-%m-%d")
            elif not h10_hit.empty:
                trade_date = pd.Timestamp(h10_hit.iloc[0].get("trade_date")).strftime("%Y-%m-%d")
            visibility_status = "rebuildable_from_range_no_future_pool" if not pool_hit.empty else ("fallback_closed_source_only" if hit else "missing_range_source_key")
            blocker = "" if not pool_hit.empty else ("range no-future exact variant missing" if hit else "range source key not found")
        elif route == "strong_main":
            strong_plusweak = sources["strong_plusweak"]
            strong_mainup = sources["strong_mainup"]
            strong_pool = sources["strong_plusweak_no_forward"]
            source_hit = strong_plusweak[strong_plusweak["_key"].eq(key)]
            mainup_hit = strong_mainup[strong_mainup["_key"].eq(key)]
            pool_hit = strong_pool[strong_pool["_key"].eq(key)]
            hit = not source_hit.empty or not mainup_hit.empty or not pool_hit.empty
            source_layer = (
                "strong_v2_plusweak_source"
                if not source_hit.empty
                else ("strong_v2_mainup_source" if not mainup_hit.empty else ("strong_v2_plusweak_no_forward_pool" if not pool_hit.empty else ""))
            )
            trade_date = ""
            confirm = ""
            source_row = source_hit if not source_hit.empty else (mainup_hit if not mainup_hit.empty else pool_hit)
            if hit:
                trade_date = pd.Timestamp(source_row.iloc[0].get("trade_date")).strftime("%Y-%m-%d")
                confirm = str(source_row.iloc[0].get("confirm_datetime", ""))
            visibility_status = "rebuildable_from_strong_v2_plusweak_source" if not source_hit.empty else ("fallback_strong_source_or_pool" if hit else "missing_strong_source_key")
            blocker = "" if hit else "strong source key not found"
        else:
            hit = False
            source_layer = ""
            trade_date = ""
            confirm = ""
            visibility_status = "unknown_route"
            blocker = f"unknown route {route}"

        rows.append(
            {
                "entry_date": entry_date,
                "code": code,
                "name": getattr(row, "name", ""),
                "route": route,
                "score": score,
                "source_rebuild_hit": bool(hit),
                "source_layer": source_layer,
                "source_trade_date": trade_date,
                "confirm_datetime": confirm,
                "visibility_status": visibility_status,
                "blocker": blocker,
            }
        )
    return pd.DataFrame(rows)


def _summary(audit: pd.DataFrame, v4: pd.DataFrame, sources: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for route, group in audit.groupby("route", dropna=False):
        rows.append(
            {
                "route": route,
                "candidate_rows": int(len(group)),
                "source_rebuild_hits": int(group["source_rebuild_hit"].sum()),
                "source_rebuild_hit_rate": float(group["source_rebuild_hit"].mean()) if len(group) else 0.0,
                "missing_rows": int((~group["source_rebuild_hit"]).sum()),
                "latest_entry_date": str(pd.to_datetime(group["entry_date"], errors="coerce").max().date()),
            }
        )
    rows.append(
        {
            "route": "__total__",
            "candidate_rows": int(len(audit)),
            "source_rebuild_hits": int(audit["source_rebuild_hit"].sum()),
            "source_rebuild_hit_rate": float(audit["source_rebuild_hit"].mean()) if len(audit) else 0.0,
            "missing_rows": int((~audit["source_rebuild_hit"]).sum()),
            "latest_entry_date": str(pd.to_datetime(audit["entry_date"], errors="coerce").max().date()) if not audit.empty else "",
        }
    )
    return pd.DataFrame(rows)


def _source_inventory(sources: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for name, df in sources.items():
        rows.append(
            {
                "source": name,
                "rows": int(len(df)),
                "entry_date_min": str(_date(df["entry_date"]).min().date()) if not df.empty else "",
                "entry_date_max": str(_date(df["entry_date"]).max().date()) if not df.empty else "",
                "forbidden_field_count": len(_forbidden_fields(list(df.columns))),
                "forbidden_fields": ",".join(_forbidden_fields(list(df.columns))[:20]),
            }
        )
    return pd.DataFrame(rows)


def _md_table(df: pd.DataFrame, max_rows: int = 30) -> str:
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


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    v4 = _read_csv(V4_SOURCE, "v4_strong_offense")
    v4["entry_date"] = _date(v4["entry_date"])
    v4["code"] = v4["code"].astype(str)
    sources = _route_source_map()

    audit = _attach_route_audit(v4, sources)
    summary = _summary(audit, v4, sources)
    inventory = _source_inventory(sources)
    missing = audit[~audit["source_rebuild_hit"]].copy()
    blockers = (
        audit.groupby(["route", "visibility_status", "blocker"], dropna=False)
        .size()
        .reset_index(name="rows")
        .sort_values(["route", "rows"], ascending=[True, False])
    )

    audit.to_csv(OUT_DIR / "g3_v4_strong_offense_current_source_row_audit.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "g3_v4_strong_offense_current_source_summary.csv", index=False, encoding="utf-8-sig")
    inventory.to_csv(OUT_DIR / "g3_v4_strong_offense_source_inventory.csv", index=False, encoding="utf-8-sig")
    missing.to_csv(OUT_DIR / "g3_v4_strong_offense_current_source_missing.csv", index=False, encoding="utf-8-sig")
    blockers.to_csv(OUT_DIR / "g3_v4_strong_offense_current_source_blockers.csv", index=False, encoding="utf-8-sig")

    exact_hits = int(audit["source_rebuild_hit"].sum())
    meta = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "candidate": "g3_v4_strong_offense_candidate_v1",
        "audit": "current_source_rebuild_audit_v1",
        "candidate_rows": int(len(audit)),
        "source_rebuild_hits": exact_hits,
        "source_rebuild_hit_rate": float(exact_hits / len(audit)) if len(audit) else 0.0,
        "research_only": True,
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "verdict": "PASS_SOURCE_KEY_REBUILD_AUDIT" if exact_hits == len(audit) else "PARTIAL_SOURCE_KEY_REBUILD_AUDIT",
        "next_step": "build sanitized current-source payload from matched upstream rows, then compare it with closed-trade mapped payload.",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report = f"""# G3 V4 强进攻 current-source rebuild 审计 V1

生成时间：{meta["generated_at"]}

## 定位

本审计只回答一个问题：`g3_v4_strong_offense_candidate_v1` 的 111 条研究候选，能不能回连到当时可见或近似可重建的上游候选源。

它不生成正式买点，不打开下单路径，也不改策略参数。

## 总览

{_md_table(summary)}

## 源清单

{_md_table(inventory)}

## 阻断摘要

{_md_table(blockers)}

## 缺失明细

{_md_table(missing)}

## 判断

- 如果 `source_rebuild_hit_rate=100%`，说明历史强进攻候选至少可以按 `entry_date+code+route` 回连到上游源。
- 这仍不等于实盘可用，因为上游源自身还可能包含研究期回放、盘中确认补写、或候选日历延迟。
- 下一步要从命中的上游源重新生成一个 sanitized payload，剥离收益/退出字段，并与当前 closed-trade 映射 payload 做逐行一致性对比。
"""
    (OUT_DIR / "g3_v4_strong_offense_current_source_rebuild_audit_report_cn.md").write_text(report, encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
