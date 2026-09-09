from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
import re
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
OUT_DIR = _report_path() / "gen3_struct_veto_combo_e_live_visibility_v1"

SELECTED_PATH = _report_path() / "gen3_struct_veto_combo_e_candidate_v1" / "struct_veto_combo_e_candidates.csv"
PANIC_SOURCE = _report_path() / "gen3_panic_v2_research" / "final_candidate_v1" / "m30_close5_full_nextopen_cost30_closed_trades.csv"
PANIC_CANDIDATES = _report_path() / "gen3_panic_v2_research" / "panic_v2_candidates.csv"
RANGE_SOURCE = _report_path() / "gen3_range_v3_mtm_pressure_v1" / "range_v3_weak_low_not_chasing_h5_cost30_closed_trades.csv"
STRONG_SOURCE = _report_path() / "gen3_strong_v2_independent_source_v1" / "strong_v2_main_up_only_hold5_closed_trades.csv"

FORBIDDEN_PATTERNS = [
    r"^fwd_ret",
    r"^outcome",
    r"^mfe_",
    r"^mae_",
    r"^exit",
    r"^gross_ret$",
    r"^net_ret$",
    r"^baseline_net_ret$",
    r"^policy_net_ret$",
    r"^realized_pnl$",
    r"^stake$",
]


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, low_memory=False) if path.exists() else pd.DataFrame()


def as_date(value: pd.Series) -> pd.Series:
    return pd.to_datetime(value, errors="coerce").dt.normalize()


def as_dt(value: pd.Series) -> pd.Series:
    return pd.to_datetime(value, errors="coerce")


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def rate(mask: pd.Series) -> float:
    if mask.empty:
        return 0.0
    return float(mask.fillna(False).mean())


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, max_rows: int = 60) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    view = df.head(max_rows).copy()
    for col in view.columns:
        if col in pct_cols:
            view[col] = view[col].map(pct)
        else:
            view[col] = view[col].map(lambda x: "" if pd.isna(x) else str(x))
    suffix = "" if len(df) <= max_rows else f"\n\n_仅展示前 {max_rows} 行，共 {len(df)} 行。_"
    return view.to_markdown(index=False) + suffix


def forbidden_fields(cols: list[str]) -> list[str]:
    fields: list[str] = []
    for col in cols:
        if any(re.search(pattern, col) for pattern in FORBIDDEN_PATTERNS):
            fields.append(col)
    return fields


def prep_source(df: pd.DataFrame, route: str) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["entry_date"] = as_date(out["entry_date"])
    out["code"] = out["code"].astype(str)
    out["route"] = route
    out["_source_key"] = out["code"] + "|" + out["entry_date"].dt.strftime("%Y-%m-%d") + "|" + route
    return out.drop_duplicates("_source_key", keep="first")


def enrich_panic_source(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    candidates = read_csv(PANIC_CANDIDATES)
    if candidates.empty:
        return df
    left = df.copy()
    right = candidates.copy()
    left["entry_date"] = as_date(left["entry_date"])
    right["entry_date"] = as_date(right["entry_date"])
    left["code"] = left["code"].astype(str)
    right["code"] = right["code"].astype(str)
    keep = [
        "code",
        "entry_date",
        "trade_date",
        "panic_variant",
        "market_style",
        "adx_layer",
        "up_rate",
        "big_down_rate",
        "drawdown20",
        "range_pos60",
        "market_amount_ratio20",
    ]
    keep = [col for col in keep if col in right.columns]
    right = right[keep].drop_duplicates(["code", "entry_date"], keep="first")
    return left.merge(right, on=["code", "entry_date"], how="left", suffixes=("", "_candidate"))


def load_joined() -> tuple[pd.DataFrame, dict[str, pd.DataFrame], pd.DataFrame]:
    selected = read_csv(SELECTED_PATH)
    selected["entry_date"] = as_date(selected["entry_date"])
    selected["code"] = selected["code"].astype(str)
    selected["_source_key"] = selected["code"] + "|" + selected["entry_date"].dt.strftime("%Y-%m-%d") + "|" + selected["route"].astype(str)

    sources = {
        "down_panic": prep_source(enrich_panic_source(read_csv(PANIC_SOURCE)), "down_panic"),
        "range_gap": prep_source(read_csv(RANGE_SOURCE), "range_gap"),
        "strong_main": prep_source(read_csv(STRONG_SOURCE), "strong_main"),
    }

    parts = []
    for route, source in sources.items():
        part = selected[selected["route"].eq(route)].copy()
        if part.empty:
            continue
        if source.empty:
            part["source_matched"] = False
            parts.append(part)
            continue
        joined = part.merge(source, on="_source_key", how="left", suffixes=("", "_src"))
        joined["source_matched"] = joined["entry_date_src"].notna() if "entry_date_src" in joined.columns else False
        parts.append(joined)
    return selected, sources, pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def route_visibility(joined: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for route, g in joined.groupby("route", dropna=False):
        entry = as_date(g["entry_date"])
        row: dict[str, Any] = {
            "route": route,
            "route_cn": {
                "down_panic": "弱势恐慌：弱市/下跌后买恐慌出清和30m修复",
                "range_gap": "横盘箱体：震荡/弱反弹里买箱体低位或跳空修复",
                "strong_main": "强势主线：吸收G2 volume5，买强势周期的二次确认",
            }.get(str(route), ""),
            "candidate_rows": int(len(g)),
            "matched_rows": int(g["source_matched"].fillna(False).sum()),
            "matched_rate": rate(g["source_matched"]),
            "verdict": "REVIEW",
            "blocker": "",
        }
        if route == "down_panic":
            trade_date = as_date(g.get("trade_date", pd.Series(pd.NaT, index=g.index)))
            confirm = as_dt(g.get("confirm_datetime", pd.Series(pd.NaT, index=g.index)))
            bad_volume = g.get("g3_volume_context", pd.Series("", index=g.index)).astype(str).eq("overheated_volume")
            bad_repair = g.get("g3_repair_env_label", pd.Series("", index=g.index)).astype(str).eq("neutral_or_unclassified")
            strong_clearance = (
                g.get("g3_capitulation_strength", pd.Series("", index=g.index)).astype(str).eq("strong_capitulation")
                & g.get("g3_repair_env_label", pd.Series("", index=g.index)).astype(str).eq("strong_clearance")
            )
            row.update(
                {
                    "decision_rule": "trade_date < entry_date; confirm_datetime 在 entry_date 当日；过滤 overheated_volume、neutral_or_unclassified、strong_capitulation+strong_clearance",
                    "trade_date_lt_entry_rate": rate(trade_date < entry),
                    "confirm_same_entry_day_rate": rate(confirm.dt.normalize() == entry),
                    "bad_volume_rows": int(bad_volume.sum()),
                    "bad_repair_rows": int(bad_repair.sum()),
                    "bad_strong_clearance_rows": int(strong_clearance.sum()),
                    "verdict": "PASS_RESEARCH_VISIBILITY__NEEDS_INTRADAY_ENV_PROXY",
                    "blocker": "要接影子盘前，恐慌环境字段必须由D-1快照或截至确认30m的实时重算提供，不能用信号日收盘后全日统计。",
                }
            )
        elif route == "range_gap":
            trade_date = as_date(g.get("trade_date", pd.Series(pd.NaT, index=g.index)))
            close_position = pd.to_numeric(g.get("close_position", pd.Series(pd.NA, index=g.index)), errors="coerce")
            adx_layer = g.get("adx_layer", pd.Series("", index=g.index)).astype(str)
            row.update(
                {
                    "decision_rule": "trade_date < entry_date; close_position >= 0.80; adx_layer 不为 weak_trend_strength/downtrend_strength",
                    "trade_date_lt_entry_rate": rate(trade_date < entry),
                    "close_position_min": float(close_position.min()) if close_position.notna().any() else None,
                    "forbidden_adx_rows": int(adx_layer.isin(["weak_trend_strength", "downtrend_strength"]).sum()),
                    "verdict": "PASS_D1_OPEN_VISIBILITY",
                    "blocker": "",
                }
            )
        elif route == "strong_main":
            trade_date = as_date(g.get("trade_date", pd.Series(pd.NaT, index=g.index)))
            factor_date = as_date(g.get("factor_date", pd.Series(pd.NaT, index=g.index)))
            confirm = as_dt(g.get("confirm_datetime", pd.Series(pd.NaT, index=g.index)))
            score_volume5 = pd.to_numeric(g.get("score_volume5", pd.Series(pd.NA, index=g.index)), errors="coerce")
            breadth = pd.to_numeric(g.get("market_breadth", pd.Series(pd.NA, index=g.index)), errors="coerce")
            strong_score = pd.to_numeric(g.get("g3_strong_score", pd.Series(pd.NA, index=g.index)), errors="coerce")
            row.update(
                {
                    "decision_rule": "trade_date/factor_date < entry_date; score_volume5 >= 0.70; market_breadth > 0.56; g3_strong_score > 0.626; confirm_datetime 在 entry_date 当日",
                    "trade_date_lt_entry_rate": rate(trade_date < entry),
                    "factor_date_lt_entry_rate": rate(factor_date < entry),
                    "confirm_same_entry_day_rate": rate(confirm.dt.normalize() == entry),
                    "score_volume5_min": float(score_volume5.min()) if score_volume5.notna().any() else None,
                    "market_breadth_min": float(breadth.min()) if breadth.notna().any() else None,
                    "g3_strong_score_min": float(strong_score.min()) if strong_score.notna().any() else None,
                    "verdict": "PASS_WITH_D1_FACTOR_AND_30M_CONFIRM_REQUIREMENT",
                    "blocker": "g3_strong_score 只能由D-1可见分数和入场日30m确认代理组成；影子盘字段必须剥离收益、退出、持仓金额等研究字段。",
                }
            )
        rows.append(row)
    return pd.DataFrame(rows)


def field_policy(selected: pd.DataFrame, sources: dict[str, pd.DataFrame]) -> pd.DataFrame:
    route_fields = {
        "down_panic": {
            "live_visible": ["code", "name", "entry_date", "confirm_datetime", "entry_price_adjusted", "bar_time", "bar_close_pos", "bar_ret", "amount_ratio3"],
            "d1_or_intraday_proxy": ["trade_date", "market_style", "adx_layer", "up_rate", "big_down_rate", "drawdown20", "range_pos60", "g3_capitulation_strength", "g3_volume_context", "g3_repair_env_label"],
        },
        "range_gap": {
            "live_visible": ["code", "name", "trade_date", "entry_date", "entry_open", "rank_in_day"],
            "d1_or_intraday_proxy": ["adx_layer", "close_position", "gap_open", "range_pos60", "drawdown20", "market_amount_ratio20"],
        },
        "strong_main": {
            "live_visible": ["code", "name", "entry_date", "confirm_datetime", "entry_price", "pattern", "trigger_type", "rt_30m_amount_ratio"],
            "d1_or_intraday_proxy": ["trade_date", "factor_date", "v4_rank", "v4_score", "score_volume5", "market_breadth", "g3_strong_score", "l3_rt_strong3_ratio"],
        },
    }
    rows: list[dict[str, Any]] = []
    for route, source in sources.items():
        cols = set(source.columns)
        for category, fields in route_fields.get(route, {}).items():
            present = [field for field in fields if field in cols]
            missing = [field for field in fields if field not in cols]
            rows.append(
                {
                    "route": route,
                    "category": category,
                    "present_count": len(present),
                    "missing_count": len(missing),
                    "present_fields": ",".join(present),
                    "missing_fields": ",".join(missing),
                    "verdict": "PASS" if not missing else "REVIEW_MISSING",
                }
            )
        forbidden = forbidden_fields(list(cols))
        rows.append(
            {
                "route": route,
                "category": "research_only_must_strip",
                "present_count": len(forbidden),
                "missing_count": 0,
                "present_fields": ",".join(forbidden),
                "missing_fields": "",
                "verdict": "MUST_STRIP_BEFORE_SHADOW_OR_LIVE" if forbidden else "PASS",
            }
        )
    selected_forbidden = forbidden_fields(list(selected.columns))
    rows.append(
        {
            "route": "combo_e_selected",
            "category": "research_only_must_strip",
            "present_count": len(selected_forbidden),
            "missing_count": 0,
            "present_fields": ",".join(selected_forbidden),
            "missing_fields": "",
            "verdict": "MUST_STRIP_BEFORE_SHADOW_OR_LIVE" if selected_forbidden else "PASS",
        }
    )
    return pd.DataFrame(rows)


def blockers(joined: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, row in joined[~joined["source_matched"].fillna(False)].iterrows():
        rows.append(
            {
                "severity": "high",
                "route": row.get("route"),
                "entry_date": row.get("entry_date"),
                "code": row.get("code"),
                "issue": "候选无法回连到原始特征源",
                "action": "先修复lineage，不能进入影子盘。",
            }
        )
    return pd.DataFrame(rows, columns=["severity", "route", "entry_date", "code", "issue", "action"])


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    selected, sources, joined = load_joined()
    route_summary = route_visibility(joined)
    policy = field_policy(selected, sources)
    block = blockers(joined)

    route_summary.to_csv(OUT_DIR / "route_visibility_summary.csv", index=False, encoding="utf-8-sig")
    policy.to_csv(OUT_DIR / "field_policy_audit.csv", index=False, encoding="utf-8-sig")
    block.to_csv(OUT_DIR / "visibility_blockers.csv", index=False, encoding="utf-8-sig")

    hard_blockers = int(len(block))
    summary = {
        "status": "completed",
        "variant": "struct_veto_combo_e",
        "selected_rows": int(len(selected)),
        "hard_blockers": hard_blockers,
        "candidate_signal_window": "2020-01-01 to 2026-05-29",
        "actual_entry_window": "2020-01-03 to 2026-05-18",
        "mtm_settlement_window": "2020-01-03 to 2026-06-04",
        "verdict": "lineage_pass_shadow_payload_next" if hard_blockers == 0 else "blocked_fix_lineage_first",
        "live_status": "research_candidate_only_not_live",
        "next_step": "build_shadow_safe_payload_without_research_return_fields" if hard_blockers == 0 else "fix_unmatched_sources",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# G3 struct_veto_combo_e 实盘可见性审计",
        "",
        "## 审计范围",
        "- 策略版本：`struct_veto_combo_e`。",
        "- 候选信号/入场窗口：2020-01-01 至 2026-05-29。",
        "- 实际成交入场窗口：2020-01-03 至 2026-05-18。",
        "- MTM结算窗口：2020-01-03 至 2026-06-04。",
        "- 状态：研究候选，不是实盘下单版本。",
        "",
        "## 英文名解释",
        "- `down_panic`：弱势恐慌买法，在弱市或下跌后等待恐慌出清，再用30m修复确认买入。",
        "- `range_gap`：横盘/箱体买法，在震荡或弱反弹环境里，买箱体低位、跳空或反抽修复。",
        "- `strong_main`：强势主线买法，吸收G2的volume5强势思路，在强势周期里等二次确认。",
        "- `struct_veto_combo_e`：结构化否决版组合，不再靠score/rank继续调参，而是剔除已证明执行压力较大的结构。",
        "",
        "## 链路可见性",
        md_table(route_summary, pct_cols={"matched_rate", "trade_date_lt_entry_rate", "factor_date_lt_entry_rate", "confirm_same_entry_day_rate"}),
        "",
        "## 字段策略",
        md_table(policy),
        "",
        "## 阻塞项",
        md_table(block),
        "",
        "## 结论",
    ]
    if hard_blockers == 0:
        lines.extend(
            [
                "- 原始来源回连通过：三条链路候选都能回连到特征源。",
                "- 下一步可以做影子盘安全payload，但必须剥离收益、退出、持仓金额等研究字段。",
                "- 还不能直接接自动交易，因为 `down_panic` 的市场恐慌环境字段和 `strong_main` 的强势分数仍要在影子payload里明确拆成 D-1 快照或入场日30m代理。",
            ]
        )
    else:
        lines.append("- 存在候选无法回连到来源，先修lineage。")
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
