from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "reports" / "gen3_guarded_live_safe_payload_v1" / "g3_guarded_live_safe_payload.csv"
SUMMARY_SOURCE = ROOT / "reports" / "gen3_guarded_candidate_package_v1" / "summary.json"
OUT_DIR = ROOT / "reports" / "gen3_guarded_shadow_only_v1"
RUNTIME_DIR = ROOT / "data" / "runtime" / "gen3_guarded_shadow"

FORBIDDEN_PATHS = [
    ROOT / "reports" / "gen3_shadow_live_daily_update_v1",
    ROOT / "reports" / "gen3_live_payload_v1",
    ROOT / "data" / "runtime" / "v4_live_monitor",
    ROOT / "reports" / "gen2_v2_complete_strategy",
    ROOT / "data" / "runtime" / "gen3_panic_shadow",
    ROOT / "data" / "runtime" / "gen3_range_shadow",
    ROOT / "data" / "runtime" / "gen3_strong_shadow",
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


def _fmt_pct(value: Any) -> str:
    try:
        if pd.isna(value):
            return ""
        return f"{float(value) * 100:.2f}%"
    except Exception:
        return ""


def _md_table(df: pd.DataFrame, max_rows: int = 20) -> str:
    if df.empty:
        return "_无数据_"
    view = df.head(max_rows).copy()
    for col in view.columns:
        view[col] = view[col].astype(str)
    header = "| " + " | ".join(view.columns) + " |"
    sep = "| " + " | ".join(["---"] * len(view.columns)) + " |"
    rows = ["| " + " | ".join(row) + " |" for row in view.to_numpy()]
    suffix = []
    if len(df) > max_rows:
        suffix.append(f"\n\n_仅展示前 {max_rows} 行，共 {len(df)} 行。_")
    return "\n".join([header, sep, *rows, *suffix])


def _build_payload(source: pd.DataFrame) -> pd.DataFrame:
    if source.empty:
        return pd.DataFrame()

    payload = source.copy()
    payload["shadow_schema_version"] = "g3_guarded_shadow_only_v1"
    payload["strategy_id"] = "g3_guarded_volume5_v1"
    payload["candidate_source"] = str(SOURCE)
    payload["source_is_live_safe_payload"] = True
    payload["shadow_action"] = "observe_only"
    payload["auto_order_allowed"] = False
    payload["formal_buy_signal"] = False
    payload["order_path_enabled"] = False
    if "block_reason" not in payload.columns:
        payload["block_reason"] = "shadow_only_research_candidate_not_live"

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
    return payload[cols].sort_values(["entry_date", "route_priority", "score"], ascending=[True, True, False])


def _diagnose(as_of_date: str, payload: pd.DataFrame) -> tuple[str, str, str]:
    if payload.empty:
        return (
            "SOURCE_EMPTY",
            "G3 guarded 候选源为空或不可读，不能生成影子观察清单。",
            "",
        )

    dates = pd.to_datetime(payload["entry_date"], errors="coerce")
    latest = dates.max()
    latest_text = "" if pd.isna(latest) else latest.strftime("%Y-%m-%d")
    today_count = int((dates.dt.strftime("%Y-%m-%d") == as_of_date).sum())
    if today_count > 0:
        return (
            "HAS_TODAY_SHADOW_CANDIDATES",
            f"{as_of_date} 有 {today_count} 条 G3 guarded live-safe 观察候选，但本链路固定 observe-only，不能下单。",
            latest_text,
        )
    return (
        "NO_G3_GUARDED_SIGNAL_FOR_DATE",
        f"{as_of_date} 没有 G3 guarded live-safe 观察候选；候选源最新 entry_date={latest_text}。",
        latest_text,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build isolated G3 guarded shadow-only observation outputs.")
    parser.add_argument("--as-of", default=None, help="Decision datetime, e.g. 2026-06-01 15:00:00")
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
    summary_source = _read_json(SUMMARY_SOURCE)
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

    summary_rows = [
        {
            "as_of": as_of.strftime("%Y-%m-%d %H:%M:%S"),
            "as_of_date": as_of_date,
            "strategy_id": "g3_guarded_volume5_v1",
            "mode": "shadow_only_observe",
            "diagnosis_code": diagnosis_code,
            "diagnosis": diagnosis,
            "source_path": str(SOURCE),
            "source_type": "g3_guarded_live_safe_payload",
            "source_rows": len(source),
            "payload_rows": len(payload),
            "today_shadow_candidates": len(today),
            "recent_rows": len(recent),
            "latest_entry_date": latest_entry_date,
            "auto_order_allowed_rows": 0,
            "formal_buy_signal_rows": 0,
            "order_path_enabled_rows": 0,
            "main_30bps_return": summary_source.get("main_30bps", {}).get("total_return"),
            "main_30bps_max_drawdown": summary_source.get("main_30bps", {}).get("max_drawdown"),
            "close_100bps_return": summary_source.get("close_100bps", {}).get("total_return"),
            "nextopen_limitdown_30bps_return": summary_source.get("nextopen_limitdown_30bps", {}).get("total_return"),
            "severe_nextopen_haircut2_return": summary_source.get("severe_nextopen_haircut2", {}).get("total_return"),
        }
    ]
    summary = pd.DataFrame(summary_rows)

    payload.to_csv(out / "g3_guarded_shadow_payload.csv", index=False, encoding="utf-8-sig")
    today.to_csv(out / "g3_guarded_shadow_today_candidates.csv", index=False, encoding="utf-8-sig")
    recent.to_csv(out / "g3_guarded_shadow_recent_candidates.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(out / "g3_guarded_shadow_summary.csv", index=False, encoding="utf-8-sig")

    latest_summary = summary_rows[0].copy()
    latest_summary["written_outputs"] = [
        str(out / "g3_guarded_shadow_payload.csv"),
        str(out / "g3_guarded_shadow_today_candidates.csv"),
        str(out / "g3_guarded_shadow_recent_candidates.csv"),
        str(out / "g3_guarded_shadow_summary.csv"),
        str(out / "g3_guarded_shadow_only_report_cn.md"),
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
    isolation.to_csv(out / "g3_guarded_shadow_isolation_manifest.csv", index=False, encoding="utf-8-sig")

    report = f"""# G3 guarded shadow-only 观察链路 V1

生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## 定位

这是 G3 第三代 guarded 候选的独立影子观察链路，只用于每日观察和复盘。

- 固定 `shadow_action = observe_only`
- 固定 `auto_order_allowed = false`
- 固定 `formal_buy_signal = false`
- 固定 `order_path_enabled = false`
- 固定 `block_reason = shadow_only_research_candidate_not_live`

它不调用旧 G3 live payload 脚本，不写 G2 实盘目录，不写 `data/runtime/v4_live_monitor`。

## 今日诊断

{_md_table(summary)}

## 今日影子候选

{_md_table(today)}

## 最近 {args.recent_days} 天研究候选

{_md_table(recent)}

## 隔离校验

{_md_table(isolation)}

## 当前判断

当前这一步完成的是“可观察但不可交易”的落地：G3 guarded 候选来自 live-safe payload，可以每天产出诊断和候选清单，但仍然不能进入正式买点、页面可买校验或自动下单。

如果 `diagnosis_code = NO_G3_GUARDED_SIGNAL_FOR_DATE`，含义不是系统故障，而是该日期没有新的 G3 guarded 候选。当前候选源最新日期为 `{latest_entry_date}`。

## 下一步目标

下一步应该做只读可视化或 API 接口：把本目录的 `latest_summary.json` 和 `latest_candidates.csv` 以观察卡片展示出来，同时继续保持不可交易状态。之后再做真实可见性审计：检查这些候选在盘中是否能被同一时刻的数据生成，而不是历史回看才知道。
"""
    (out / "g3_guarded_shadow_only_report_cn.md").write_text(report, encoding="utf-8", newline="\n")

    print(
        json.dumps(
            {
                "out_dir": str(out),
                "runtime_dir": str(runtime),
                "diagnosis_code": diagnosis_code,
                "today_shadow_candidates": len(today),
                "latest_entry_date": latest_entry_date,
                "isolation_unchanged": bool(isolation["unchanged"].all()),
                "auto_order_allowed_rows": 0,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
