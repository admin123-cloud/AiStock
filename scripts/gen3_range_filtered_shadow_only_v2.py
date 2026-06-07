from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "runtime" / "gen3_range_filtered_live_safe" / "latest_payload.csv"
PACKAGE_SUMMARY = ROOT / "reports" / "gen3_range_filtered_candidate_package_v2" / "summary.json"
OUT_DIR = ROOT / "reports" / "gen3_range_filtered_shadow_only_v2"
RUNTIME_DIR = ROOT / "data" / "runtime" / "gen3_range_filtered_shadow"

FORBIDDEN_PATHS = [
    ROOT / "reports" / "gen3_shadow_live_daily_update_v1",
    ROOT / "reports" / "gen3_live_payload_v1",
    ROOT / "data" / "runtime" / "v4_live_monitor",
    ROOT / "reports" / "gen2_v2_complete_strategy",
    ROOT / "reports" / "gen2_v2_complete_strategy_2020",
    ROOT / "data" / "runtime" / "gen3_guarded_shadow",
]


def _path_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "mtime_ns": None,
            "size": None,
            "kind": "missing",
        }
    stat = path.stat()
    if path.is_dir():
        size = sum(1 for _ in path.rglob("*"))
        kind = "dir"
    else:
        size = stat.st_size
        kind = "file"
    return {
        "path": str(path),
        "exists": True,
        "mtime_ns": stat.st_mtime_ns,
        "size": size,
        "kind": kind,
    }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


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


def _build_payload(source: pd.DataFrame) -> pd.DataFrame:
    if source.empty:
        return pd.DataFrame()

    payload = source.copy()
    payload["shadow_schema_version"] = "g3_range_filtered_shadow_only_v2"
    payload["strategy_id"] = "g3_range_filtered_volume5_v2"
    payload["candidate_source"] = str(SOURCE)
    payload["source_is_live_safe_payload"] = True
    payload["shadow_action"] = "observe_only"
    payload["auto_order_allowed"] = False
    payload["formal_buy_signal"] = False
    payload["order_path_enabled"] = False
    payload["block_reason"] = "shadow_only_range_filtered_candidate_not_live"

    preferred = [
        "shadow_schema_version",
        "schema_version",
        "strategy_id",
        "candidate_source",
        "source_is_live_safe_payload",
        "signal_date",
        "entry_date",
        "decision_source_date",
        "confirm_datetime",
        "code",
        "name",
        "route",
        "route_source",
        "route_priority",
        "score",
        "entry_price",
        "range_filter_version",
        "live_visibility_status",
        "env_proxy_required",
        "ranking_proxy_required",
        "live_ready",
        "shadow_action",
        "auto_order_allowed",
        "formal_buy_signal",
        "order_path_enabled",
        "block_reason",
    ]
    trailing = [col for col in payload.columns if col not in preferred]
    cols = [col for col in preferred if col in payload.columns] + trailing

    if "entry_date" in payload.columns:
        payload["_entry_date_sort"] = pd.to_datetime(payload["entry_date"], errors="coerce")
    else:
        payload["_entry_date_sort"] = pd.NaT
    priority = pd.to_numeric(payload.get("route_priority", 999), errors="coerce").fillna(999)
    score = pd.to_numeric(payload.get("score", 0.0), errors="coerce").fillna(0.0)
    payload["_route_priority_sort"] = priority
    payload["_score_sort"] = score
    payload = payload.sort_values(
        ["_entry_date_sort", "_route_priority_sort", "_score_sort"],
        ascending=[True, True, False],
    )
    return payload[cols]


def _diagnose(as_of_date: str, payload: pd.DataFrame) -> tuple[str, str, str]:
    if payload.empty or "entry_date" not in payload.columns:
        return (
            "SOURCE_EMPTY",
            "G3 range-filtered V2 候选源为空或不可读，不能生成影子观察清单。",
            "",
        )

    dates = pd.to_datetime(payload["entry_date"], errors="coerce")
    latest = dates.max()
    latest_text = "" if pd.isna(latest) else latest.strftime("%Y-%m-%d")
    today_count = int((dates.dt.strftime("%Y-%m-%d") == as_of_date).sum())
    if today_count > 0:
        return (
            "HAS_TODAY_RANGE_FILTERED_SHADOW_CANDIDATES",
            f"{as_of_date} 有 {today_count} 条 G3 V2 range-filtered 影子候选，但本链路固定 observe-only，不能下单。",
            latest_text,
        )
    return (
        "NO_G3_RANGE_FILTERED_SIGNAL_FOR_DATE",
        f"{as_of_date} 没有 G3 V2 range-filtered 影子候选；候选源最新 entry_date={latest_text}。",
        latest_text,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build isolated G3 range-filtered V2 shadow-only observation outputs.")
    parser.add_argument("--as-of", default=None, help="Decision datetime, e.g. 2026-06-02 15:00:00")
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    parser.add_argument("--runtime-dir", default=str(RUNTIME_DIR))
    parser.add_argument("--recent-days", type=int, default=10)
    args = parser.parse_args()

    as_of = pd.Timestamp(args.as_of) if args.as_of else pd.Timestamp(datetime.now())
    as_of_date = as_of.strftime("%Y-%m-%d")
    out = Path(args.out_dir)
    runtime = Path(args.runtime_dir)

    pre_status = [_path_status(path) for path in FORBIDDEN_PATHS]

    out.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)

    source = pd.read_csv(SOURCE, low_memory=False) if SOURCE.exists() else pd.DataFrame()
    package_summary = _read_json(PACKAGE_SUMMARY)
    payload = _build_payload(source)
    diagnosis_code, diagnosis, latest_entry_date = _diagnose(as_of_date, payload)

    if not payload.empty:
        entry_dates = pd.to_datetime(payload["entry_date"], errors="coerce")
        today = payload.loc[entry_dates.dt.strftime("%Y-%m-%d") == as_of_date].copy()
        recent_cutoff = pd.Timestamp(as_of_date) - pd.Timedelta(days=args.recent_days)
        recent = payload.loc[(entry_dates <= pd.Timestamp(as_of_date)) & (entry_dates >= recent_cutoff)].copy()
        recent = recent.sort_values(["entry_date", "route_priority", "score"], ascending=[False, True, False])
    else:
        today = pd.DataFrame()
        recent = pd.DataFrame()

    route_summary = []
    if not payload.empty and "route" in payload.columns:
        for route, group in payload.groupby("route", dropna=False):
            route_summary.append(
                {
                    "route": route,
                    "payload_rows": int(len(group)),
                    "live_ready_rows": int(group.get("live_ready", pd.Series(False, index=group.index)).fillna(False).sum()),
                    "today_shadow_candidates": int(len(today[today["route"].eq(route)])) if "route" in today.columns else 0,
                    "latest_entry_date": str(pd.to_datetime(group["entry_date"], errors="coerce").max().date()),
                }
            )
    route_df = pd.DataFrame(route_summary)

    summary_rows = [
        {
            "as_of": as_of.strftime("%Y-%m-%d %H:%M:%S"),
            "as_of_date": as_of_date,
            "strategy_id": "g3_range_filtered_volume5_v2",
            "mode": "shadow_only_observe",
            "diagnosis_code": diagnosis_code,
            "diagnosis": diagnosis,
            "source_path": str(SOURCE),
            "source_type": "g3_range_filtered_live_safe_payload_v2",
            "source_rows": int(len(source)),
            "payload_rows": int(len(payload)),
            "today_shadow_candidates": int(len(today)),
            "recent_rows": int(len(recent)),
            "latest_entry_date": latest_entry_date,
            "auto_order_allowed_rows": 0,
            "formal_buy_signal_rows": 0,
            "order_path_enabled_rows": 0,
            "stress_close_30bps_return": package_summary.get("stress_close_30bps", {}).get("total_return"),
            "stress_close_30bps_max_drawdown": package_summary.get("stress_close_30bps", {}).get("max_drawdown"),
            "stress_close_100bps_return": package_summary.get("stress_close_100bps", {}).get("total_return"),
            "stress_close_100bps_max_drawdown": package_summary.get("stress_close_100bps", {}).get("max_drawdown"),
            "nextopen_limitdown_30bps_return": package_summary.get("nextopen_limitdown_30bps", {}).get("total_return"),
            "severe_nextopen_haircut2_return": package_summary.get("severe_nextopen_haircut2", {}).get("total_return"),
            "severe_nextopen_haircut2_max_drawdown": package_summary.get("severe_nextopen_haircut2", {}).get("max_drawdown"),
        }
    ]
    summary = pd.DataFrame(summary_rows)

    payload.to_csv(out / "g3_range_filtered_shadow_payload.csv", index=False, encoding="utf-8-sig")
    today.to_csv(out / "g3_range_filtered_shadow_today_candidates.csv", index=False, encoding="utf-8-sig")
    recent.to_csv(out / "g3_range_filtered_shadow_recent_candidates.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(out / "g3_range_filtered_shadow_summary.csv", index=False, encoding="utf-8-sig")
    route_df.to_csv(out / "g3_range_filtered_shadow_route_summary.csv", index=False, encoding="utf-8-sig")

    latest_summary = summary_rows[0].copy()
    latest_summary["routes"] = route_summary
    latest_summary["written_outputs"] = [
        str(out / "g3_range_filtered_shadow_payload.csv"),
        str(out / "g3_range_filtered_shadow_today_candidates.csv"),
        str(out / "g3_range_filtered_shadow_recent_candidates.csv"),
        str(out / "g3_range_filtered_shadow_summary.csv"),
        str(out / "g3_range_filtered_shadow_route_summary.csv"),
        str(runtime / "latest_summary.json"),
        str(runtime / "latest_candidates.csv"),
    ]
    (runtime / "latest_summary.json").write_text(
        json.dumps(latest_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    today.to_csv(runtime / "latest_candidates.csv", index=False, encoding="utf-8-sig")

    post_status = [_path_status(path) for path in FORBIDDEN_PATHS]
    isolation_rows = []
    for pre, post in zip(pre_status, post_status):
        unchanged = (
            pre["exists"] == post["exists"]
            and pre["mtime_ns"] == post["mtime_ns"]
            and pre["size"] == post["size"]
        )
        isolation_rows.append(
            {
                "path": pre["path"],
                "exists_before": pre["exists"],
                "exists_after": post["exists"],
                "mtime_ns_before": pre["mtime_ns"],
                "mtime_ns_after": post["mtime_ns"],
                "size_before": pre["size"],
                "size_after": post["size"],
                "unchanged": unchanged,
            }
        )
    isolation = pd.DataFrame(isolation_rows)
    isolation.to_csv(out / "g3_range_filtered_shadow_isolation_manifest.csv", index=False, encoding="utf-8-sig")

    report = f"""# G3 range-filtered shadow-only 观察链路 V2

生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## 定位

这是 `g3_range_filtered_volume5_v2` 的独立影子观察链路，只读取 V2 live-safe payload，不写入 G2，不进入正式买点，也不打开下单路径。

- 固定 `shadow_action = observe_only`
- 固定 `auto_order_allowed = false`
- 固定 `formal_buy_signal = false`
- 固定 `order_path_enabled = false`
- 固定 `block_reason = shadow_only_range_filtered_candidate_not_live`

## 今日诊断

{_md_table(summary)}

## 路由摘要

{_md_table(route_df)}

## 今日影子候选

{_md_table(today)}

## 最近 {args.recent_days} 天研究候选

{_md_table(recent)}

## 隔离校验

{_md_table(isolation)}

## 当前判断

V2 已经可以作为页面/API 的只读观察源。它比 V1 少了噪音 range_gap 样本，但仍然不是实盘策略：严重次日低开 haircut2 压力仍为负，需要继续研究执行侧和横盘候选源质量。

## 下一步目标

把 `latest_summary.json` 和 `latest_candidates.csv` 接入 `/api/gen3-shadow/range-filtered`，并在实盘页面上与旧 V1 guarded 版本并排展示。"""
    (out / "g3_range_filtered_shadow_only_report_cn.md").write_text(report, encoding="utf-8", newline="\n")

    print(
        json.dumps(
            {
                "out_dir": str(out),
                "runtime_dir": str(runtime),
                "diagnosis_code": diagnosis_code,
                "today_shadow_candidates": int(len(today)),
                "payload_rows": int(len(payload)),
                "latest_entry_date": latest_entry_date,
                "isolation_unchanged": bool(isolation["unchanged"].all()),
                "auto_order_allowed_rows": 0,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
