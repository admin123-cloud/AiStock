from __future__ import annotations

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

from utils.paths import report_path, runtime_path

OUT_DIR = report_path("gen3_guarded_live_safe_payload_v1")
RUNTIME_DIR = runtime_path("gen3_guarded_live_safe")

SELECTED_PATH = report_path("gen3_guarded_candidate_package_v1", "g3_guarded_candidate_closed_trades.csv")
PANIC_SOURCE = report_path("gen3_panic_v2_research", "final_candidate_v1", "m30_close5_full_nextopen_cost30_closed_trades.csv")
PANIC_CONTEXT_SOURCE = report_path("gen3_panic_v2_research", "panic_v2_candidates.csv")
RANGE_SOURCE = report_path("gen3_range_v3_mtm_pressure_v1", "range_v3_weak_low_not_chasing_h5_cost30_closed_trades.csv")
STRONG_SOURCE = report_path("gen3_strong_v2_independent_source_v1", "strong_v2_main_up_only_hold5_closed_trades.csv")

FORBIDDEN_PATTERNS = [
    r"^fwd_ret",
    r"^outcome",
    r"^mfe_",
    r"^mae_",
    r"^exit_close_",
    r"^gross_ret$",
    r"^net_ret$",
    r"^baseline_net_ret$",
    r"^policy_net_ret$",
    r"^fixed_net_ret$",
    r"^exit_ret_net$",
    r"^realized_pnl$",
    r"^exit_value$",
    r"^stake$",
    r"^policy_exit_date$",
    r"^exit_date$",
    r"^exit_price$",
    r"^trigger_close_ret$",
]

BASE_COLUMNS = [
    "schema_version",
    "strategy_id",
    "mode",
    "route",
    "route_source",
    "route_priority",
    "code",
    "name",
    "signal_date",
    "entry_date",
    "decision_source_date",
    "confirm_datetime",
    "entry_price",
    "score",
    "source_lineage_key",
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

ROUTE_EXTRA_COLUMNS = [
    "trade_date",
    "factor_date",
    "bar_time",
    "confirm_rule",
    "bar_close_pos",
    "bar_ret",
    "amount_ratio3",
    "candidate_score",
    "entry_price_adjusted",
    "entry_open",
    "range_v3_score",
    "range_v3_variant",
    "range_v3_family",
    "env_proxy_mode",
    "d1_env_proxy_status",
    "ranking_proxy_mode",
    "ranking_proxy_status",
    "panic_variant",
    "rank_in_day",
    "pattern",
    "trigger_type",
    "fractal_datetime",
    "intraday_normal_datetime",
    "rt_return_from_d1_close",
    "rt_confirm_vs_ma5",
    "rt_confirm_vs_ma10",
    "rt_30m_amount_ratio",
    "score_volume5",
    "g3_strong_score",
    "g3_market_style",
    "market_style",
    "ma_skeleton",
    "volume_price_layer",
    "adx_layer",
    "breadth_ma20",
    "breadth_ma60",
    "up_rate",
    "big_down_rate",
    "limit_down_proxy_rate",
    "market_amount_ratio20",
    "index_mom20",
    "range_pos60",
    "drawdown20",
    "amount_ratio20",
    "lower_shadow_ratio",
    "close_position",
    "gap_open",
    "runup_from_60d_low",
    "cap_pressure_amount_share",
    "l3_rt_strong3_ratio",
]


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, low_memory=False) if path.exists() else pd.DataFrame()


def _date(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.normalize()


def _forbidden_fields(columns: list[str]) -> list[str]:
    return [col for col in columns if any(re.search(pattern, col) for pattern in FORBIDDEN_PATTERNS)]


def _source_key(df: pd.DataFrame, route: str) -> pd.Series:
    date = _date(df["entry_date"]).dt.strftime("%Y-%m-%d")
    return df["code"].astype(str) + "|" + date + "|" + route


def _selected_keys() -> pd.DataFrame:
    selected = _read_csv(SELECTED_PATH)
    selected["entry_date"] = _date(selected["entry_date"])
    selected["code"] = selected["code"].astype(str)
    selected["_source_key"] = _source_key(selected, selected["route"].astype(str))
    keep = ["_source_key", "route", "route_source", "route_priority"]
    return selected[keep].drop_duplicates("_source_key", keep="first")


def _base_payload(df: pd.DataFrame, route: str, selected: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=BASE_COLUMNS + ROUTE_EXTRA_COLUMNS)
    d = df.copy()
    d["entry_date"] = _date(d["entry_date"])
    d["code"] = d["code"].astype(str)
    d["_source_key"] = _source_key(d, route)
    d = d.merge(selected[selected["route"] == route], on="_source_key", how="inner", suffixes=("", "_selected"))
    if d.empty:
        return pd.DataFrame(columns=BASE_COLUMNS + ROUTE_EXTRA_COLUMNS)

    out = pd.DataFrame(index=d.index)
    out["schema_version"] = "g3_guarded_live_safe_payload_v1"
    out["strategy_id"] = "g3_guarded_volume5_v1"
    out["mode"] = "shadow_only_observe"
    out["route"] = route
    out["route_source"] = d.get("route_source_selected", d.get("route_source", ""))
    out["route_priority"] = pd.to_numeric(d.get("route_priority_selected", d.get("route_priority", 0)), errors="coerce").fillna(0).astype(int)
    out["code"] = d["code"].astype(str)
    out["name"] = d.get("name", "")
    out["entry_date"] = d["entry_date"].dt.strftime("%Y-%m-%d")
    out["confirm_datetime"] = d.get("confirm_datetime", "")
    out["source_lineage_key"] = d["_source_key"]
    out["shadow_action"] = "observe_only"
    out["auto_order_allowed"] = False
    out["formal_buy_signal"] = False
    out["order_path_enabled"] = False

    for col in ROUTE_EXTRA_COLUMNS:
        if col in d.columns:
            out[col] = d[col]
    return out


def _attach_panic_decision_context(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or not PANIC_CONTEXT_SOURCE.exists():
        return df
    usecols = [
        "trade_date",
        "entry_date",
        "code",
        "panic_variant",
    ]
    ctx = pd.read_csv(PANIC_CONTEXT_SOURCE, usecols=lambda c: c in usecols, low_memory=False)
    if ctx.empty or not {"trade_date", "entry_date", "code"}.issubset(ctx.columns):
        return df

    out = df.copy()
    out["entry_date"] = _date(out["entry_date"])
    out["code"] = out["code"].astype(str)
    out["_panic_variant_key"] = out.get("g3_chain", "").astype(str)

    ctx["entry_date"] = _date(ctx["entry_date"])
    ctx["trade_date"] = _date(ctx["trade_date"])
    ctx["code"] = ctx["code"].astype(str)
    if "panic_variant" in ctx.columns:
        ctx["_panic_variant_key"] = ctx["panic_variant"].astype(str)
    else:
        ctx["_panic_variant_key"] = ""

    exact = ctx.drop_duplicates(["entry_date", "code", "_panic_variant_key"], keep="first")
    out = out.merge(
        exact[["entry_date", "code", "_panic_variant_key", "trade_date", "panic_variant"]],
        on=["entry_date", "code", "_panic_variant_key"],
        how="left",
        suffixes=("", "_ctx"),
    )

    missing = out["trade_date"].isna()
    if missing.any():
        fallback = ctx.drop_duplicates(["entry_date", "code"], keep="first")
        out = out.merge(
            fallback[["entry_date", "code", "trade_date"]].rename(columns={"trade_date": "trade_date_fallback"}),
            on=["entry_date", "code"],
            how="left",
        )
        out.loc[missing, "trade_date"] = out.loc[missing, "trade_date_fallback"]
        out = out.drop(columns=["trade_date_fallback"])

    return out.drop(columns=["_panic_variant_key"])


def _panic_payload(selected: pd.DataFrame) -> pd.DataFrame:
    d = _attach_panic_decision_context(_read_csv(PANIC_SOURCE))
    out = _base_payload(d, "down_panic", selected)
    if out.empty:
        return out
    trade_date = _date(out["trade_date"]) if "trade_date" in out.columns else pd.Series(pd.NaT, index=out.index)
    entry_date = _date(out["entry_date"])
    confirm = pd.to_datetime(out.get("confirm_datetime"), errors="coerce")
    d1_visible = (trade_date < entry_date) & (confirm.dt.normalize() == entry_date)
    out["signal_date"] = trade_date.dt.strftime("%Y-%m-%d").fillna(out["entry_date"])
    out["decision_source_date"] = out["signal_date"]
    price_source = out["entry_price_adjusted"] if "entry_price_adjusted" in out.columns else out["entry_price"]
    out["entry_price"] = pd.to_numeric(price_source, errors="coerce")
    out["score"] = pd.to_numeric(out.get("candidate_score", 0.0), errors="coerce")
    out["env_proxy_mode"] = "d1_snapshot"
    out["d1_env_proxy_status"] = d1_visible.map({True: "pass_d1_env_snapshot", False: "fail_d1_env_snapshot"})
    out["env_proxy_required"] = ~d1_visible
    out["ranking_proxy_required"] = False
    out["live_ready"] = d1_visible
    out["live_visibility_status"] = d1_visible.map({
        True: "pass_d1_env_snapshot_and_30m_confirm_shadow_only",
        False: "fail_d1_env_snapshot_shadow_only",
    })
    out["block_reason"] = d1_visible.map({
        True: "shadow_only_not_auto_ordered",
        False: "down_panic_d1_env_snapshot_failed_shadow_only",
    })
    return out


def _range_payload(selected: pd.DataFrame) -> pd.DataFrame:
    d = _read_csv(RANGE_SOURCE)
    out = _base_payload(d, "range_gap", selected)
    if out.empty:
        return out
    trade_date = _date(out["trade_date"]) if "trade_date" in out.columns else pd.Series(pd.NaT, index=out.index)
    entry_date = _date(out["entry_date"])
    out["signal_date"] = trade_date.dt.strftime("%Y-%m-%d")
    out["decision_source_date"] = out["signal_date"]
    out["entry_price"] = pd.to_numeric(out.get("entry_open", pd.Series(pd.NA, index=out.index)), errors="coerce")
    out["score"] = pd.to_numeric(out.get("range_v3_score", out.get("candidate_score", 0.0)), errors="coerce")
    pass_rule = trade_date < entry_date
    out["env_proxy_required"] = False
    out["ranking_proxy_required"] = False
    out["live_ready"] = pass_rule.fillna(False)
    out["live_visibility_status"] = pass_rule.map(lambda x: "pass_d1_open" if bool(x) else "blocked_trade_date_not_before_entry")
    out["block_reason"] = pass_rule.map(lambda x: "shadow_only_research_candidate_not_live" if bool(x) else "range_trade_date_rule_failed")
    return out


def _strong_payload(selected: pd.DataFrame) -> pd.DataFrame:
    d = _read_csv(STRONG_SOURCE)
    out = _base_payload(d, "strong_main", selected)
    if out.empty:
        return out
    trade_date = _date(out["trade_date"]) if "trade_date" in out.columns else pd.Series(pd.NaT, index=out.index)
    factor_date = _date(out["factor_date"]) if "factor_date" in out.columns else pd.Series(pd.NaT, index=out.index)
    entry_date = _date(out["entry_date"])
    confirm = pd.to_datetime(out["confirm_datetime"], errors="coerce") if "confirm_datetime" in out.columns else pd.Series(pd.NaT, index=out.index)
    pass_rule = (trade_date < entry_date) & (factor_date < entry_date) & (confirm.dt.normalize() == entry_date)
    out["signal_date"] = trade_date.dt.strftime("%Y-%m-%d")
    out["decision_source_date"] = factor_date.dt.strftime("%Y-%m-%d")
    out["entry_price"] = pd.to_numeric(out.get("entry_price", pd.Series(pd.NA, index=out.index)), errors="coerce")
    out["score"] = pd.to_numeric(out.get("g3_strong_score", out.get("score_volume5", 0.0)), errors="coerce")
    out["ranking_proxy_mode"] = "d1_volume5_plus_intraday_confirm"
    out["ranking_proxy_status"] = pass_rule.map(lambda x: "pass_ranking_proxy_visible" if bool(x) else "fail_ranking_proxy_visible")
    out["env_proxy_required"] = False
    out["ranking_proxy_required"] = ~pass_rule
    out["live_ready"] = pass_rule.fillna(False)
    out["live_visibility_status"] = pass_rule.map(lambda x: "pass_d1_volume5_30m_confirm_shadow_only" if bool(x) else "blocked_date_visibility_rule_failed")
    out["block_reason"] = pass_rule.map(lambda x: "shadow_only_not_auto_ordered" if bool(x) else "strong_date_visibility_rule_failed")
    return out


def _finalize(parts: list[pd.DataFrame]) -> pd.DataFrame:
    payload = pd.concat([p for p in parts if not p.empty], ignore_index=True) if parts else pd.DataFrame()
    if payload.empty:
        return payload
    for col in BASE_COLUMNS + ROUTE_EXTRA_COLUMNS:
        if col not in payload.columns:
            payload[col] = pd.NA
    payload = payload[BASE_COLUMNS + ROUTE_EXTRA_COLUMNS]
    forbidden = _forbidden_fields(list(payload.columns))
    if forbidden:
        raise RuntimeError(f"forbidden fields leaked into live-safe payload: {forbidden}")
    payload["entry_date_sort"] = pd.to_datetime(payload["entry_date"], errors="coerce")
    payload["route_priority"] = pd.to_numeric(payload["route_priority"], errors="coerce").fillna(0).astype(int)
    payload["score"] = pd.to_numeric(payload["score"], errors="coerce").fillna(0.0)
    payload = payload.sort_values(["entry_date_sort", "route_priority", "score"], ascending=[True, False, False]).drop(columns=["entry_date_sort"])
    return payload


def _summary(payload: pd.DataFrame) -> pd.DataFrame:
    if payload.empty:
        return pd.DataFrame()
    rows = []
    for route, g in payload.groupby("route", dropna=False):
        rows.append(
            {
                "route": route,
                "rows": len(g),
                "live_ready_rows": int(g["live_ready"].fillna(False).sum()),
                "env_proxy_required_rows": int(g["env_proxy_required"].fillna(False).sum()),
                "ranking_proxy_required_rows": int(g["ranking_proxy_required"].fillna(False).sum()),
                "auto_order_allowed_rows": int(g["auto_order_allowed"].fillna(False).sum()),
                "formal_buy_signal_rows": int(g["formal_buy_signal"].fillna(False).sum()),
                "order_path_enabled_rows": int(g["order_path_enabled"].fillna(False).sum()),
                "latest_entry_date": str(pd.to_datetime(g["entry_date"], errors="coerce").max().date()),
            }
        )
    return pd.DataFrame(rows)


def _md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_无数据_"
    return df.to_markdown(index=False)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    selected = _selected_keys()
    payload = _finalize([
        _panic_payload(selected),
        _range_payload(selected),
        _strong_payload(selected),
    ])
    summary = _summary(payload)
    forbidden_audit = pd.DataFrame(
        [
            {
                "scope": "payload_columns",
                "forbidden_field_count": len(_forbidden_fields(list(payload.columns))),
                "forbidden_fields": ",".join(_forbidden_fields(list(payload.columns))),
                "verdict": "PASS" if not _forbidden_fields(list(payload.columns)) else "FAIL",
            }
        ]
    )

    payload.to_csv(OUT_DIR / "g3_guarded_live_safe_payload.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "g3_guarded_live_safe_summary.csv", index=False, encoding="utf-8-sig")
    forbidden_audit.to_csv(OUT_DIR / "g3_guarded_live_safe_forbidden_field_audit.csv", index=False, encoding="utf-8-sig")
    payload.to_csv(RUNTIME_DIR / "latest_payload.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(RUNTIME_DIR / "latest_summary.csv", index=False, encoding="utf-8-sig")

    meta = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "payload_rows": int(len(payload)),
        "routes": summary.to_dict(orient="records"),
        "forbidden_field_count": int(forbidden_audit.iloc[0]["forbidden_field_count"]),
        "auto_order_allowed_rows": int(payload["auto_order_allowed"].fillna(False).sum()) if not payload.empty else 0,
        "formal_buy_signal_rows": int(payload["formal_buy_signal"].fillna(False).sum()) if not payload.empty else 0,
        "order_path_enabled_rows": int(payload["order_path_enabled"].fillna(False).sum()) if not payload.empty else 0,
        "live_readiness": "shadow_only_payload_ready",
        "next_step": "keep_shadow_only_observation_and_audit_full_combo_after_live_visibility_upgrade",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    (RUNTIME_DIR / "latest_summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    report = f"""# G3 guarded live-safe payload V1

生成时间：{meta['generated_at']}

## 定位

这一步把 G3 guarded 从“研究 closed trades 明细”推进为“可被页面/影子盘读取的 live-safe 候选 payload”。

本 payload 仍然是 shadow-only：

- `auto_order_allowed=false`
- `formal_buy_signal=false`
- `order_path_enabled=false`

它剥离了未来收益、成交后结果、退出结果字段，不从 closed trades 直接接正式交易。

## 汇总

{_md_table(summary)}

## 禁止字段审计

{_md_table(forbidden_audit)}

## 三条路由规则

- `range_gap`：要求 `trade_date < entry_date`，当前可作为 D+1 开盘观察候选，但仍不自动下单。
- `strong_main`：要求 `trade_date/factor_date < entry_date` 且 `confirm_datetime` 为入场日 30m 确认；当前使用 `ranking_proxy_mode=d1_volume5_plus_intraday_confirm`，仍只允许观察。
- `down_panic`：使用上游 `panic_v2_candidates` 的 D-1 `trade_date` 作为 `signal_date/decision_source_date`，买入日只保留 30m 确认；当前 `env_proxy_required=false`，但仍不自动下单。

## 阶段判断

当前状态：

`live-safe payload 已生成；shadow-only 可读；自动交易未通过。`

## 下一步目标

下一步在 live-visible 口径下重做组合层审计，确认三条链路同时可见后，收益、回撤、年份稳定性与执行压力没有被日期语义修正破坏；同时继续保持 shadow-only。
"""
    (OUT_DIR / "g3_guarded_live_safe_payload_report_cn.md").write_text(report, encoding="utf-8", newline="\n")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
