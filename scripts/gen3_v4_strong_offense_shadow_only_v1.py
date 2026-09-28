from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path, runtime_path  # noqa: E402

CURRENT_SOURCE = runtime_path("gen3_v4_strong_offense_current_source", "latest_payload.csv")
FALLBACK_SOURCE = report_path("gen3_v4_strong_offense_candidate_v1", "g3_v4_strong_offense_closed_trades.csv")
SOURCE = CURRENT_SOURCE
SUMMARY_SOURCE = report_path("gen3_v4_strong_offense_candidate_v1", "summary.json")
OUT_DIR = report_path("gen3_v4_strong_offense_shadow_only_v1")
RUNTIME_DIR = runtime_path("gen3_v4_strong_offense_shadow")

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
]

FORBIDDEN_PATHS = [
    report_path("gen3_shadow_live_daily_update_v1"),
    report_path("gen3_live_payload_v1"),
    report_path("gen2_v2_complete_strategy"),
    report_path("gen2_v2_complete_strategy_2020"),
    runtime_path("v4_live_monitor"),
    runtime_path("gen3_guarded_shadow"),
    runtime_path("gen3_range_filtered_shadow"),
]

BASE_COLUMNS = [
    "shadow_schema_version",
    "strategy_id",
    "candidate_source",
    "source_is_research_package",
    "selected_variant",
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
    "strong_day_rank",
    "strong_rule_pass",
    "source_lineage_key",
    "live_visibility_status",
    "visibility_audit_level",
    "env_proxy_required",
    "ranking_proxy_required",
    "live_ready",
    "shadow_action",
    "auto_order_allowed",
    "formal_buy_signal",
    "order_path_enabled",
    "block_reason",
]

KEEP_EXTRAS = [
    "current_source_layer",
    "trade_date",
    "factor_date",
    "g3_chain",
    "range_v3_variant",
    "range_v3_family",
    "rank_in_day",
    "market_breadth",
    "g3_strong_score",
    "score_volume5",
    "runup_from_60d_low",
    "l3_s3",
    "l2_s3",
    "l2_rt_rise_ratio",
    "v4_rank",
    "v4_score",
    "source_family",
    "l2_sector_name",
    "l3_sector_name",
    "scale_variant",
    "position_scale",
    "scale_note",
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


def _forbidden_fields(columns: list[str]) -> list[str]:
    return [col for col in columns if any(re.search(pattern, col) for pattern in FORBIDDEN_PATTERNS)]


def _date(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.normalize()


def _fmt_date(series: pd.Series) -> pd.Series:
    return _date(series).dt.strftime("%Y-%m-%d")


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


def _build_payload(source: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if source.empty:
        return pd.DataFrame(columns=BASE_COLUMNS), pd.DataFrame()

    forbidden_in_source = _forbidden_fields(list(source.columns))
    d = source.drop(columns=forbidden_in_source, errors="ignore").copy()
    is_current_source = "current_source_layer" in d.columns or str(d.get("schema_version", pd.Series([""])).iloc[0]).startswith(
        "g3_v4_strong_offense_current_source_payload"
    )
    d["entry_date"] = _date(d["entry_date"])
    d["code"] = d["code"].astype(str)
    d["route"] = d["route"].astype(str)
    d["score"] = pd.to_numeric(d.get("score"), errors="coerce")
    d["route_priority"] = pd.to_numeric(d.get("route_priority"), errors="coerce").fillna(0).astype(int)
    d["source_lineage_key"] = d["route"] + "|" + d["entry_date"].dt.strftime("%Y-%m-%d") + "|" + d["code"]

    payload = pd.DataFrame(index=d.index)
    payload["shadow_schema_version"] = "g3_v4_strong_offense_shadow_only_v1"
    payload["strategy_id"] = "g3_v4_strong_offense_candidate_v1"
    payload["candidate_source"] = str(SOURCE)
    payload["source_is_research_package"] = not is_current_source
    payload["selected_variant"] = "second093_score_lt105"
    if is_current_source and "signal_date" in d.columns:
        payload["signal_date"] = d["signal_date"]
    else:
        payload["signal_date"] = d["entry_date"].dt.strftime("%Y-%m-%d")
    payload["entry_date"] = d["entry_date"].dt.strftime("%Y-%m-%d")
    payload["decision_source_date"] = d.get("decision_source_date", payload["signal_date"])
    payload["confirm_datetime"] = d.get("confirm_datetime", pd.Series(pd.NA, index=d.index))
    payload["code"] = d["code"]
    payload["name"] = d.get("name", "")
    payload["route"] = d["route"]
    payload["route_source"] = d.get("route_source", "")
    payload["route_priority"] = d["route_priority"]
    payload["score"] = d["score"]
    payload["entry_price"] = pd.to_numeric(d.get("entry_price"), errors="coerce")
    payload["strong_day_rank"] = pd.to_numeric(d.get("strong_day_rank"), errors="coerce")
    strong = payload["route"].eq("strong_main")
    first_strong = payload["strong_day_rank"].lt(2) | payload["strong_day_rank"].isna()
    second_strong_pass = payload["strong_day_rank"].ge(2) & payload["score"].ge(0.93)
    score_cap_pass = payload["score"].lt(1.05)
    payload["strong_rule_pass"] = (~strong) | ((first_strong | second_strong_pass) & score_cap_pass)
    payload["source_lineage_key"] = d["source_lineage_key"]
    payload["visibility_audit_level"] = (
        "current_source_payload_sanitized" if is_current_source else "research_closed_trade_mapping_only"
    )
    payload["live_visibility_status"] = d.get(
        "live_visibility_status",
        pd.Series("pending_current_source_rebuild", index=d.index),
    )
    payload["env_proxy_required"] = d.get("env_proxy_required", pd.Series(True, index=d.index)).fillna(True).astype(bool)
    payload["ranking_proxy_required"] = d.get("ranking_proxy_required", pd.Series(True, index=d.index)).fillna(True).astype(bool)
    payload["live_ready"] = d.get("live_ready", pd.Series(False, index=d.index)).fillna(False).astype(bool)
    payload["shadow_action"] = "observe_only"
    payload["auto_order_allowed"] = False
    payload["formal_buy_signal"] = False
    payload["order_path_enabled"] = False
    payload["block_reason"] = d.get(
        "block_reason",
        pd.Series("shadow_only_research_candidate_not_live", index=d.index),
    )

    has_confirm = pd.to_datetime(payload["confirm_datetime"], errors="coerce").dt.normalize().eq(d["entry_date"])
    strong_visible = strong & has_confirm & payload["strong_rule_pass"]
    if not is_current_source:
        payload.loc[strong_visible, "visibility_audit_level"] = "strong_intraday_confirm_timestamp_present"
        payload.loc[strong_visible, "live_visibility_status"] = "pass_strong_rule_timestamp_mapping_shadow_only"
        payload.loc[strong_visible, "env_proxy_required"] = False
        payload.loc[strong_visible, "ranking_proxy_required"] = False
        payload.loc[strong_visible, "live_ready"] = True
        payload.loc[strong_visible, "block_reason"] = "shadow_only_not_auto_ordered"

    for col in KEEP_EXTRAS:
        if col in d.columns:
            payload[col] = d[col]

    forbidden_out = _forbidden_fields(list(payload.columns))
    if forbidden_out:
        raise RuntimeError(f"forbidden fields leaked into shadow payload: {forbidden_out}")

    sort_score = pd.to_numeric(payload["score"], errors="coerce").fillna(0.0)
    payload = payload.assign(_entry_date_sort=d["entry_date"], _score_sort=sort_score)
    payload = payload.sort_values(["_entry_date_sort", "route_priority", "_score_sort"], ascending=[True, False, False])
    payload = payload.drop(columns=["_entry_date_sort", "_score_sort"])

    audit = pd.DataFrame(
        [
            {
                "scope": "source_columns",
                "forbidden_field_count": len(forbidden_in_source),
                "forbidden_fields": ",".join(forbidden_in_source),
                "verdict": "PASS_STRIPPED" if forbidden_in_source else "PASS",
            },
            {
                "scope": "payload_columns",
                "forbidden_field_count": len(_forbidden_fields(list(payload.columns))),
                "forbidden_fields": ",".join(_forbidden_fields(list(payload.columns))),
                "verdict": "PASS" if not _forbidden_fields(list(payload.columns)) else "FAIL",
            },
        ]
    )
    return payload[BASE_COLUMNS + [c for c in KEEP_EXTRAS if c in payload.columns]], audit


def _diagnose(as_of_date: str, payload: pd.DataFrame) -> tuple[str, str, str]:
    if payload.empty or "entry_date" not in payload.columns:
        return (
            "SOURCE_EMPTY",
            "G3 V4 强进攻候选源为空或不可读，不能生成影子观察清单。",
            "",
        )
    dates = pd.to_datetime(payload["entry_date"], errors="coerce")
    latest = dates.max()
    latest_text = "" if pd.isna(latest) else latest.strftime("%Y-%m-%d")
    today_count = int((dates.dt.strftime("%Y-%m-%d") == as_of_date).sum())
    if today_count > 0:
        return (
            "HAS_TODAY_STRONG_OFFENSE_SHADOW_CANDIDATES",
            f"{as_of_date} 有 {today_count} 条 G3 V4 强进攻影子候选，但来源仍是研究映射，固定 observe-only。",
            latest_text,
        )
    return (
        "NO_G3_V4_STRONG_OFFENSE_SIGNAL_FOR_DATE",
        f"{as_of_date} 没有 G3 V4 强进攻影子候选；候选源最新 entry_date={latest_text}。",
        latest_text,
    )


def main() -> None:
    global SOURCE
    parser = argparse.ArgumentParser(description="Build G3 V4 strong offense shadow-only observation outputs.")
    parser.add_argument("--as-of", default=None, help="Decision datetime, e.g. 2026-05-20 15:00:00")
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    parser.add_argument("--runtime-dir", default=str(RUNTIME_DIR))
    parser.add_argument("--recent-days", type=int, default=30)
    args = parser.parse_args()

    as_of = pd.Timestamp(args.as_of) if args.as_of else pd.Timestamp(datetime.now())
    as_of_date = as_of.strftime("%Y-%m-%d")
    out = Path(args.out_dir)
    runtime = Path(args.runtime_dir)

    pre_status = [_path_status(path) for path in FORBIDDEN_PATHS]
    out.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)

    SOURCE = CURRENT_SOURCE if CURRENT_SOURCE.exists() else FALLBACK_SOURCE
    source = pd.read_csv(SOURCE, low_memory=False) if SOURCE.exists() else pd.DataFrame()
    candidate_summary = _read_json(SUMMARY_SOURCE)
    payload, forbidden_audit = _build_payload(source)
    diagnosis_code, diagnosis, latest_entry_date = _diagnose(as_of_date, payload)

    if not payload.empty:
        entry_dates = pd.to_datetime(payload["entry_date"], errors="coerce")
        today = payload.loc[entry_dates.dt.strftime("%Y-%m-%d") == as_of_date].copy()
        recent_cutoff = pd.Timestamp(as_of_date) - pd.Timedelta(days=args.recent_days)
        recent = payload.loc[(entry_dates <= pd.Timestamp(as_of_date)) & (entry_dates >= recent_cutoff)].copy()
        recent = recent.sort_values(["entry_date", "route_priority", "score"], ascending=[False, False, False])
    else:
        today = pd.DataFrame()
        recent = pd.DataFrame()

    route_summary = []
    if not payload.empty:
        for route, group in payload.groupby("route", dropna=False):
            route_summary.append(
                {
                    "route": route,
                    "payload_rows": int(len(group)),
                    "live_ready_rows": int(group["live_ready"].fillna(False).sum()),
                    "today_shadow_candidates": int(len(today[today["route"].eq(route)])) if "route" in today.columns else 0,
                    "latest_entry_date": str(pd.to_datetime(group["entry_date"], errors="coerce").max().date()),
                }
            )
    route_df = pd.DataFrame(route_summary)

    selected_metrics = candidate_summary.get("selected_metrics", {})
    source_type = "current_source_payload" if SOURCE == CURRENT_SOURCE else "research_closed_trade_mapping"
    summary_rows = [
        {
            "as_of": as_of.strftime("%Y-%m-%d %H:%M:%S"),
            "as_of_date": as_of_date,
            "strategy_id": "g3_v4_strong_offense_candidate_v1",
            "mode": "shadow_only_observe",
            "diagnosis_code": diagnosis_code,
            "diagnosis": diagnosis,
            "source_path": str(SOURCE),
            "source_type": source_type,
            "source_rows": int(len(source)),
            "payload_rows": int(len(payload)),
            "today_shadow_candidates": int(len(today)),
            "recent_rows": int(len(recent)),
            "latest_entry_date": latest_entry_date,
            "auto_order_allowed_rows": 0,
            "formal_buy_signal_rows": 0,
            "order_path_enabled_rows": 0,
            "live_ready_rows": int(payload["live_ready"].fillna(False).sum()) if not payload.empty else 0,
            "selected_total_return": selected_metrics.get("total_return"),
            "selected_max_drawdown": selected_metrics.get("max_drawdown"),
            "selected_trade_count": selected_metrics.get("trade_count"),
            "selected_strong_main_trades": selected_metrics.get("strong_main_trades"),
        }
    ]
    summary = pd.DataFrame(summary_rows)

    payload.to_csv(out / "g3_v4_strong_offense_shadow_payload.csv", index=False, encoding="utf-8-sig")
    today.to_csv(out / "g3_v4_strong_offense_shadow_today_candidates.csv", index=False, encoding="utf-8-sig")
    recent.to_csv(out / "g3_v4_strong_offense_shadow_recent_candidates.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(out / "g3_v4_strong_offense_shadow_summary.csv", index=False, encoding="utf-8-sig")
    route_df.to_csv(out / "g3_v4_strong_offense_shadow_route_summary.csv", index=False, encoding="utf-8-sig")
    forbidden_audit.to_csv(out / "g3_v4_strong_offense_shadow_forbidden_field_audit.csv", index=False, encoding="utf-8-sig")

    latest_summary = summary_rows[0].copy()
    latest_summary["routes"] = route_summary
    latest_summary["guardrail_note"] = "当前只是研究候选到影子观察的映射，不是正式 live-safe payload。"
    latest_summary["written_outputs"] = [
        str(out / "g3_v4_strong_offense_shadow_payload.csv"),
        str(out / "g3_v4_strong_offense_shadow_today_candidates.csv"),
        str(out / "g3_v4_strong_offense_shadow_recent_candidates.csv"),
        str(out / "g3_v4_strong_offense_shadow_summary.csv"),
        str(out / "g3_v4_strong_offense_shadow_route_summary.csv"),
        str(out / "g3_v4_strong_offense_shadow_forbidden_field_audit.csv"),
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
    isolation.to_csv(out / "g3_v4_strong_offense_shadow_isolation_manifest.csv", index=False, encoding="utf-8-sig")

    report = f"""# G3 V4 强进攻 shadow-only 观察链路 V1

生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## 定位

这是 `g3_v4_strong_offense_candidate_v1` 的影子观察映射，只用于继续调研 G3 当前进攻性。

- 固定 `shadow_action = observe_only`
- 固定 `auto_order_allowed = false`
- 固定 `formal_buy_signal = false`
- 固定 `order_path_enabled = false`
- 源头仍是研究 closed-trade 包，尚不是正式 live-safe payload

## 今日诊断

{_md_table(summary)}

## 路由摘要

{_md_table(route_df)}

## 禁止字段审计

{_md_table(forbidden_audit)}

## 今日影子候选

{_md_table(today)}

## 最近 {args.recent_days} 天影子候选

{_md_table(recent)}

## 隔离校验

{_md_table(isolation)}

## 当前判断

这一步把强进攻候选推进到了“可被页面/API 读取的影子观察产物”，但还没有证明它能由当天盘中链路实时生成。尤其是 `down_panic` 和 `range_gap` 在当前 V4 标准候选里缺少完整可见性上下文；`strong_main` 只能对有 `confirm_datetime` 的样本做时间戳映射。因此当前结论是：进攻候选值得继续跟踪，但不能升级成正式买点。

## 下一步

下一步应补一个真正的 current-source rebuild：从当天候选源重建 down/range/strong 三条路线，并让 V4 强进攻规则在该源上过滤，而不是从历史 closed trades 反推出候选。"""
    (out / "g3_v4_strong_offense_shadow_only_report_cn.md").write_text(report, encoding="utf-8", newline="\n")

    print(
        json.dumps(
            {
                "out_dir": str(out),
                "runtime_dir": str(runtime),
                "diagnosis_code": diagnosis_code,
                "today_shadow_candidates": int(len(today)),
                "payload_rows": int(len(payload)),
                "latest_entry_date": latest_entry_date,
                "live_ready_rows": int(payload["live_ready"].fillna(False).sum()) if not payload.empty else 0,
                "isolation_unchanged": bool(isolation["unchanged"].all()),
                "auto_order_allowed_rows": 0,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
