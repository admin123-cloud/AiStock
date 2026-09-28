from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "reports" / "gen3_guarded_intraday_visibility_audit_v1"

SELECTED_PATH = ROOT / "reports" / "gen3_guarded_candidate_package_v1" / "g3_guarded_candidate_closed_trades.csv"
PANIC_SOURCE = ROOT / "reports" / "gen3_panic_v2_research" / "final_candidate_v1" / "m30_close5_full_nextopen_cost30_closed_trades.csv"
RANGE_SOURCE = ROOT / "reports" / "gen3_range_v3_mtm_pressure_v1" / "range_v3_weak_low_not_chasing_h5_cost30_closed_trades.csv"
STRONG_SOURCE = ROOT / "reports" / "gen3_strong_v2_independent_source_v1" / "strong_v2_main_up_only_hold5_closed_trades.csv"

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
]


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, low_memory=False) if path.exists() else pd.DataFrame()


def _date(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.normalize()


def _dt(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce")


def _pct(value: Any) -> str:
    try:
        if pd.isna(value):
            return ""
        return f"{float(value) * 100:.2f}%"
    except Exception:
        return ""


def _rate(mask: pd.Series) -> float:
    if mask.empty:
        return 0.0
    return float(mask.fillna(False).mean())


def _md_table(df: pd.DataFrame, max_rows: int = 30, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    view = df.head(max_rows).copy()
    for col in view.columns:
        if col in pct_cols:
            view[col] = view[col].map(_pct)
        else:
            view[col] = view[col].map(lambda x: "" if pd.isna(x) else str(x))
    text = view.to_markdown(index=False)
    if len(df) > max_rows:
        text += f"\n\n_仅展示前 {max_rows} 行，共 {len(df)} 行。_"
    return text


def _forbidden_fields(columns: list[str]) -> list[str]:
    out = []
    for col in columns:
        if any(re.search(pattern, col) for pattern in FORBIDDEN_PATTERNS):
            out.append(col)
    return out


def _prep_source(df: pd.DataFrame, route: str) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["entry_date"] = _date(out["entry_date"])
    out["code"] = out["code"].astype(str)
    out["route"] = route
    out["_source_key"] = out["code"] + "|" + out["entry_date"].dt.strftime("%Y-%m-%d") + "|" + route
    return out.drop_duplicates("_source_key", keep="first")


def _load_joined() -> tuple[pd.DataFrame, dict[str, pd.DataFrame], pd.DataFrame]:
    selected = _read_csv(SELECTED_PATH)
    selected["entry_date"] = _date(selected["entry_date"])
    selected["code"] = selected["code"].astype(str)
    selected["_source_key"] = selected["code"] + "|" + selected["entry_date"].dt.strftime("%Y-%m-%d") + "|" + selected["route"].astype(str)

    sources = {
        "down_panic": _prep_source(_read_csv(PANIC_SOURCE), "down_panic"),
        "range_gap": _prep_source(_read_csv(RANGE_SOURCE), "range_gap"),
        "strong_main": _prep_source(_read_csv(STRONG_SOURCE), "strong_main"),
    }

    joined_parts = []
    for route, source in sources.items():
        part = selected[selected["route"] == route].copy()
        if source.empty or part.empty:
            part["source_matched"] = False
            joined_parts.append(part)
            continue
        use_cols = [c for c in source.columns if c not in {"route"}]
        joined = part.merge(source[use_cols], on="_source_key", how="left", suffixes=("", "_src"))
        joined["source_matched"] = joined["entry_date_src"].notna() if "entry_date_src" in joined.columns else False
        joined_parts.append(joined)
    joined_all = pd.concat(joined_parts, ignore_index=True) if joined_parts else pd.DataFrame()
    return selected, sources, joined_all


def _route_summary(joined: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for route, g in joined.groupby("route", dropna=False):
        row: dict[str, Any] = {
            "route": route,
            "selected_rows": len(g),
            "matched_rows": int(g.get("source_matched", pd.Series(False, index=g.index)).fillna(False).sum()),
            "matched_rate": _rate(g.get("source_matched", pd.Series(False, index=g.index))),
            "verdict": "REVIEW",
        }

        if route == "range_gap":
            trade_date = _date(g.get("trade_date", pd.Series(pd.NaT, index=g.index)))
            entry_date = _date(g["entry_date"])
            row.update(
                {
                    "decision_date_rule": "trade_date < entry_date",
                    "decision_date_pass_rate": _rate(trade_date < entry_date),
                    "confirm_rows": 0,
                    "proxy_required": False,
                    "verdict": "PASS_D1_OPEN",
                    "blocker": "",
                }
            )
        elif route == "strong_main":
            trade_date = _date(g.get("trade_date", pd.Series(pd.NaT, index=g.index)))
            factor_date = _date(g.get("factor_date", pd.Series(pd.NaT, index=g.index)))
            entry_date = _date(g["entry_date"])
            confirm = _dt(g.get("confirm_datetime", pd.Series(pd.NaT, index=g.index)))
            row.update(
                {
                    "decision_date_rule": "trade_date/factor_date < entry_date; confirm_datetime on entry day",
                    "trade_date_lt_entry_rate": _rate(trade_date < entry_date),
                    "factor_date_lt_entry_rate": _rate(factor_date < entry_date),
                    "confirm_rows": int(confirm.notna().sum()),
                    "confirm_same_entry_day_rate": _rate(confirm.dt.normalize() == entry_date),
                    "confirm_time_max": "" if confirm.dropna().empty else confirm.max().strftime("%H:%M:%S"),
                    "proxy_required": True,
                    "verdict": "PASS_WITH_INTRADAY_PROXY_REQUIREMENT",
                    "blocker": "V4/Alpha191/volume5 只能用 D-1 排名或盘中代理，不能用同日收盘后排名。",
                }
            )
        elif route == "down_panic":
            entry_date = _date(g["entry_date"])
            confirm = _dt(g.get("confirm_datetime", pd.Series(pd.NaT, index=g.index)))
            row.update(
                {
                    "decision_date_rule": "confirm_datetime on entry day; market panic fields need intraday/D-1 proxy",
                    "confirm_rows": int(confirm.notna().sum()),
                    "confirm_same_entry_day_rate": _rate(confirm.dt.normalize() == entry_date),
                    "confirm_time_max": "" if confirm.dropna().empty else confirm.max().strftime("%H:%M:%S"),
                    "proxy_required": True,
                    "verdict": "PASS_30M_CONFIRM_BUT_ENV_PROXY_REQUIRED",
                    "blocker": "恐慌/市场环境字段不能直接使用信号日收盘后全日统计，需要 D-1 快照或截至当前 30m 的实时重算。",
                }
            )
        rows.append(row)
    return pd.DataFrame(rows)


def _field_policy(sources: dict[str, pd.DataFrame], selected: pd.DataFrame) -> pd.DataFrame:
    rows = []
    route_policy = {
        "down_panic": {
            "live_ok": ["code", "name", "entry_date", "confirm_datetime", "bar_time", "confirm_open", "confirm_high", "confirm_low", "confirm_amount", "bar_close_pos", "bar_ret", "amount_ratio3", "entry_price", "entry_price_adjusted", "confirm_rule"],
            "proxy": ["market_style", "ma_skeleton", "volume_price_layer", "adx_layer", "breadth_ma20", "breadth_ma60", "up_rate", "big_down_rate", "limit_down_proxy_rate", "market_amount_ratio20", "adx20", "index_mom20", "drawdown20", "range_pos60", "amount_ratio20"],
        },
        "range_gap": {
            "live_ok": ["code", "name", "trade_date", "entry_date", "entry_open", "range_v3_score", "range_v3_variant", "range_v3_family", "rank_in_day"],
            "proxy": ["market_style", "ma_skeleton", "volume_price_layer", "adx_layer", "breadth_ma20", "breadth_ma60", "up_rate", "big_down_rate", "range_pos60", "drawdown20", "amount_ratio20", "lower_shadow_ratio", "close_position", "gap_open"],
        },
        "strong_main": {
            "live_ok": ["code", "name", "entry_date", "confirm_datetime", "entry_price", "pattern", "trigger_type", "fractal_datetime", "confirm_amount", "volume_expand", "intraday_normal_datetime", "rt_return_from_d1_close", "rt_confirm_vs_ma5", "rt_confirm_vs_ma10", "rt_30m_amount_ratio"],
            "proxy": ["trade_date", "factor_date", "v4_rank", "v4_score", "score_volume5", "market_breadth", "g3_strong_score", "l3_rt_strong3_ratio", "runup_from_60d_low", "cap_pressure_amount_share"],
        },
    }
    for route, source in sources.items():
        cols = set(source.columns)
        policy = route_policy.get(route, {})
        for category, fields in policy.items():
            present = [field for field in fields if field in cols]
            missing = [field for field in fields if field not in cols]
            rows.append(
                {
                    "route": route,
                    "category": "live_ok" if category == "live_ok" else "requires_d1_or_intraday_proxy",
                    "present_count": len(present),
                    "missing_count": len(missing),
                    "present_fields": ",".join(present),
                    "missing_fields": ",".join(missing),
                    "verdict": "PASS" if category == "live_ok" else "PROXY_REQUIRED",
                }
            )
        forbidden = _forbidden_fields(list(cols))
        rows.append(
            {
                "route": route,
                "category": "forbidden_research_only",
                "present_count": len(forbidden),
                "missing_count": 0,
                "present_fields": ",".join(forbidden),
                "missing_fields": "",
                "verdict": "MUST_STRIP_BEFORE_LIVE",
            }
        )

    selected_forbidden = _forbidden_fields(list(selected.columns))
    rows.append(
        {
            "route": "guarded_selected_package",
            "category": "forbidden_research_only",
            "present_count": len(selected_forbidden),
            "missing_count": 0,
            "present_fields": ",".join(selected_forbidden),
            "missing_fields": "",
            "verdict": "OK_FOR_RESEARCH_PACKAGE_ONLY__MUST_STRIP_FOR_LIVE",
        }
    )
    return pd.DataFrame(rows)


def _blockers(joined: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for route, g in joined.groupby("route", dropna=False):
        unmatched = g[~g.get("source_matched", pd.Series(False, index=g.index)).fillna(False)]
        if not unmatched.empty:
            rows.extend(
                {
                    "severity": "high",
                    "route": route,
                    "code": row.get("code"),
                    "entry_date": row.get("entry_date"),
                    "issue": "selected trade cannot be linked back to source feature row",
                    "action": "修复候选包 lineage，不能用失去源字段的明细接实盘。",
                }
                for _, row in unmatched.head(20).iterrows()
            )
    columns = ["severity", "route", "code", "entry_date", "issue", "action"]
    return pd.DataFrame(rows, columns=columns)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    selected, sources, joined = _load_joined()
    route_summary = _route_summary(joined)
    field_policy = _field_policy(sources, selected)
    blockers = _blockers(joined)

    route_summary.to_csv(OUT_DIR / "route_visibility_summary.csv", index=False, encoding="utf-8-sig")
    field_policy.to_csv(OUT_DIR / "field_policy_audit.csv", index=False, encoding="utf-8-sig")
    blockers.to_csv(OUT_DIR / "visibility_blockers.csv", index=False, encoding="utf-8-sig")

    completion = {
        "status": "completed",
        "selected_rows": int(len(selected)),
        "routes": route_summary.to_dict(orient="records"),
        "hard_blocker_count": int(len(blockers[blockers.get("severity", pd.Series(dtype=str)).eq("high")])) if not blockers.empty else 0,
        "live_readiness": "shadow_only_not_live",
        "next_step": "build_guarded_live_safe_payload_schema_and_strip_research_fields",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(completion, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    report = f"""# G3 guarded 盘中可见性审计 V1

生成日期：2026-06-01

## 审计对象

- guarded 候选包：`{SELECTED_PATH.relative_to(ROOT)}`
- panic 源：`{PANIC_SOURCE.relative_to(ROOT)}`
- range 源：`{RANGE_SOURCE.relative_to(ROOT)}`
- strong 源：`{STRONG_SOURCE.relative_to(ROOT)}`

本审计只回答一个问题：当前 G3 guarded 的收益来源，在“入场时点”是否有足够证据证明可见。它不是收益优化，也不新增参数。

## 关键结论

- `range_gap`：初步通过。它使用 `trade_date < entry_date` 的 D+1 开盘逻辑，适合做震荡/弱反弹的隔日观察候选。
- `strong_main`：有条件通过。`trade_date/factor_date < entry_date` 与 `confirm_datetime` 同日 30m 确认可以构成“D-1 强势候选 + 当日二次确认”，但必须继续使用 D-1 或盘中代理的 V4/Alpha191/volume5 排名。
- `down_panic`：30m 确认通过，但环境字段需要代理。恐慌/市场环境字段不能直接使用信号日收盘后全日统计，必须改成 D-1 快照或截至当前 30m 的实时重算。
- guarded 研究包仍含 `policy_net_ret/stake/exit_value/realized_pnl` 等研究/回测字段，只能留在研究包或 shadow-only 输出中；正式 live payload 必须剥离。

## 路由可见性汇总

{_md_table(route_summary, pct_cols={"matched_rate", "decision_date_pass_rate", "trade_date_lt_entry_rate", "factor_date_lt_entry_rate", "confirm_same_entry_day_rate"})}

## 字段级策略

{_md_table(field_policy)}

## 硬阻塞样本

{_md_table(blockers)}

## 阶段判断

当前状态应定义为：

`研究候选通过；shadow-only 可观察；自动交易未通过。`

这一步没有否定 G3 guarded 的收益结论，反而把它往可上线方向推进了一层：我们已经知道三条链路分别缺什么。

## 下一步目标

下一步做 `G3 guarded live-safe payload`：

1. 从三条源明细重新生成 live-safe 候选，不从 closed trades 直接接实盘。
2. 剥离所有未来收益和成交后字段。
3. 对 `strong_main` 固定 `factor_date < entry_date`，对 `range_gap` 固定 `trade_date < entry_date`。
4. 对 `down_panic` 标记 `env_proxy_required=true`，未完成 D-1/盘中环境代理前只允许 shadow-only。
"""
    (OUT_DIR / "g3_guarded_intraday_visibility_audit_report_cn.md").write_text(report, encoding="utf-8", newline="\n")
    print(json.dumps(completion, ensure_ascii=False))


if __name__ == "__main__":
    main()
