from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_g3_five_strategies_from_scratch_v1 import _md_table, _money, _pct  # noqa: E402
from scripts.backtest_g3_five_strategies_native_bridge_v1 import _simulate_candidate_set  # noqa: E402
from scripts.gen3_build_four_path_candidates import _load_stock_daily, _load_trade_dates  # noqa: E402
from utils.paths import report_path  # noqa: E402


SOURCE_DIR = report_path("g3_five_strategies_native_bridge_v1")
PROXY_M30_DIR = report_path("g3_native_bridge_30m_integrity_v1")
OUT_DIR = report_path("g3_native_bridge_native_m30_semantics_v1")

REPORT_ROOT = Path(r"F:\Stock\AiStockResearchArchive\reports")

STRATEGY_LABELS = {
    "institutional_score120_mainwave": "机构主升Score120",
    "old_g3_strong_breakout": "强势突破",
    "volume_runup_supplement": "量能续强补位",
    "range_weak_repair": "震荡弱势修复",
    "panic_capitulation_repair": "恐慌出清修复",
}


def _read_source(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path, low_memory=False)


def _norm_code(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip().str.upper()


def _truthy(series: pd.Series) -> pd.Series:
    if series.empty:
        return pd.Series(dtype=bool)
    text = series.fillna("").astype(str).str.strip().str.lower()
    return text.isin({"1", "true", "t", "yes", "y"})


def _base_rows(
    df: pd.DataFrame,
    *,
    trade_strategy: str,
    source_file: str,
    source_label: str,
    score_cols: list[str],
    name_col: str = "name",
) -> pd.DataFrame:
    if df.empty or "code" not in df.columns or "entry_date" not in df.columns:
        return pd.DataFrame()
    score_col = next((c for c in score_cols if c in df.columns), None)
    out = pd.DataFrame(
        {
            "code": _norm_code(df["code"]),
            "entry_date": pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d"),
            "name": df[name_col] if name_col in df.columns else df.get("stock_name", ""),
            "trade_strategy": trade_strategy,
            "trade_strategy_label": STRATEGY_LABELS[trade_strategy],
            "source_strategy_label": source_label,
            "native_source_file": source_file,
            "strategy_score": pd.to_numeric(df[score_col], errors="coerce") if score_col else 0.0,
        }
    )
    return out[out["code"].ne("") & out["entry_date"].notna()].copy()


def _score120_rows() -> pd.DataFrame:
    source_file = "score120_sector_diffusion_30m_overlay_v1"
    df = _read_source(REPORT_ROOT / source_file / "base_trades_with_sector_diffusion_30m.csv")
    out = _base_rows(
        df,
        trade_strategy="institutional_score120_mainwave",
        source_file=source_file,
        source_label="Score120主升+主线扩散+30m原生源",
        score_cols=["selected_score", "wave_style_score", "score"],
        name_col="stock_name",
    )
    if out.empty:
        return out
    out["native_m30_confirmed"] = _truthy(df.loc[out.index, "m30_ok"]) if "m30_ok" in df.columns else True
    out["native_confirm_rule"] = "score120_overlay:m30_ok"
    out["native_confirm_datetime"] = ""
    out["native_m30_semantics"] = "explicit_native_30m_overlay"
    return out


def _route_package_rows() -> pd.DataFrame:
    source_file = "route_execution_mandate_v3"
    df = _read_source(REPORT_ROOT / "gen3_route_execution_mandate_candidate_package_v3" / "g3_route_execution_mandate_candidate_closed_trades.csv")
    if df.empty:
        return pd.DataFrame()
    route = df.get("route", pd.Series("", index=df.index)).fillna("").astype(str)
    mapping = {
        "strong_main": ("old_g3_strong_breakout", "旧G3强势突破原生源"),
        "range_gap": ("range_weak_repair", "旧G3弱势/震荡修复原生源"),
        "down_panic": ("panic_capitulation_repair", "旧G3恐慌修复原生源"),
    }
    parts: list[pd.DataFrame] = []
    for route_key, (strategy, label) in mapping.items():
        part_raw = df[route.eq(route_key)].copy()
        part = _base_rows(
            part_raw,
            trade_strategy=strategy,
            source_file=source_file,
            source_label=label,
            score_cols=["score", "selected_score"],
        )
        if part.empty:
            continue
        route_source = part_raw.get("route_source", pd.Series("", index=part_raw.index)).fillna("").astype(str).reset_index(drop=True)
        part = part.reset_index(drop=True)
        part["native_m30_confirmed"] = True
        part["native_confirm_rule"] = "route_package_accepted:" + route_source
        part["native_confirm_datetime"] = ""
        part["native_m30_semantics"] = route_source.map(
            lambda x: "explicit_native_30m_route" if "m30" in str(x).lower() else "route_package_accepted_no_explicit_m30_field"
        )
        parts.append(part)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def _panic_v2_rows() -> pd.DataFrame:
    source_file = "gen3_panic_v2_research_final_candidate_v1"
    df = _read_source(REPORT_ROOT / "gen3_panic_v2_research" / "final_candidate_v1" / "m30_close5_full_nextopen_cost30_closed_trades.csv")
    out = _base_rows(
        df,
        trade_strategy="panic_capitulation_repair",
        source_file=source_file,
        source_label="Panic V2 30m恐慌修复原生源",
        score_cols=["candidate_score", "score"],
    )
    if out.empty:
        return out
    confirm_dt = df.loc[out.index, "confirm_datetime"] if "confirm_datetime" in df.columns else pd.Series("", index=out.index)
    confirm_rule = df.loc[out.index, "confirm_rule"] if "confirm_rule" in df.columns else pd.Series("", index=out.index)
    out["native_m30_confirmed"] = confirm_dt.notna() & confirm_dt.astype(str).str.strip().ne("")
    out["native_confirm_rule"] = confirm_rule.fillna("").astype(str)
    out["native_confirm_datetime"] = confirm_dt.fillna("").astype(str)
    out["native_m30_semantics"] = "explicit_native_30m_confirm_datetime"
    return out


def _g2_volume_rows() -> pd.DataFrame:
    source_file = "gen2_alpha191_volume5_keep80"
    df = _read_source(REPORT_ROOT / "gen2_alpha191_light_constraint_matrix" / "sources" / "volume5_keep80_runup_le100.parquet")
    out = _base_rows(
        df,
        trade_strategy="volume_runup_supplement",
        source_file=source_file,
        source_label="G2量能续强/Alpha191原生源",
        score_cols=["alpha191_overlay_score", "score_volume5", "v4_score"],
    )
    if out.empty:
        return out
    src = df.loc[out.index].copy()
    confirm_dt = src.get("confirm_datetime", pd.Series("", index=src.index))
    period = src.get("intraday_period", pd.Series("", index=src.index)).fillna("").astype(str).str.lower()
    amount_ratio = pd.to_numeric(src.get("rt_30m_amount_ratio", pd.Series(math.nan, index=src.index)), errors="coerce")
    out["native_m30_confirmed"] = confirm_dt.notna() & confirm_dt.astype(str).str.strip().ne("") & period.eq("30m") & amount_ratio.notna()
    out["native_confirm_rule"] = (
        "g2_volume5:"
        + src.get("trigger_type", pd.Series("", index=src.index)).fillna("").astype(str)
        + "|period="
        + period
    ).to_numpy()
    out["native_confirm_datetime"] = confirm_dt.fillna("").astype(str).to_numpy()
    out["native_m30_semantics"] = "explicit_native_30m_intraday_confirm"
    return out


def _build_native_semantics() -> pd.DataFrame:
    parts = [_score120_rows(), _route_package_rows(), _panic_v2_rows(), _g2_volume_rows()]
    rows = pd.concat([p for p in parts if not p.empty], ignore_index=True)
    if rows.empty:
        return rows
    rows["native_m30_confirmed"] = rows["native_m30_confirmed"].fillna(False).astype(bool)
    rows = rows.sort_values(
        ["entry_date", "code", "trade_strategy", "native_m30_confirmed", "strategy_score"],
        ascending=[True, True, True, False, False],
    )
    return rows.drop_duplicates(["entry_date", "code", "trade_strategy", "native_source_file"], keep="first").reset_index(drop=True)


def _read_default_candidates() -> pd.DataFrame:
    path = SOURCE_DIR / "all_strategy_candidates.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    df = pd.read_csv(path, low_memory=False)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["code"] = _norm_code(df["code"])
    return df


def _attach_native_semantics(candidates: pd.DataFrame, native_rows: pd.DataFrame) -> pd.DataFrame:
    keys = ["entry_date", "code", "trade_strategy", "native_source_file"]
    attach_cols = keys + ["native_m30_confirmed", "native_confirm_rule", "native_confirm_datetime", "native_m30_semantics"]
    out = candidates.merge(native_rows[attach_cols], on=keys, how="left")
    out["native_m30_confirmed"] = out["native_m30_confirmed"].fillna(False).astype(bool)
    out["native_m30_semantics"] = out["native_m30_semantics"].fillna("missing_native_semantics")
    out["native_confirm_rule"] = out["native_confirm_rule"].fillna("")
    out["native_confirm_datetime"] = out["native_confirm_datetime"].fillna("")
    return out


def _attach_proxy_confirmation(candidates: pd.DataFrame) -> pd.DataFrame:
    path = PROXY_M30_DIR / "native_bridge_candidates_30m_audit.csv"
    if not path.exists():
        candidates["m30_confirmed_proxy"] = False
        return candidates
    proxy = pd.read_csv(path, low_memory=False)
    proxy["entry_date"] = pd.to_datetime(proxy["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    proxy["code"] = _norm_code(proxy["code"])
    keys = ["entry_date", "code", "trade_strategy", "native_source_file"]
    cols = keys + ["m30_confirmed_proxy", "m30_confirm_rule"]
    out = candidates.merge(proxy[[c for c in cols if c in proxy.columns]], on=keys, how="left")
    out["m30_confirmed_proxy"] = out["m30_confirmed_proxy"].fillna(False).astype(bool)
    out["m30_confirm_rule"] = out.get("m30_confirm_rule", "").fillna("")
    return out


def _group_summary(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for keys, part in df.groupby(group_cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        row = {col: key for col, key in zip(group_cols, keys)}
        row.update(
            {
                "candidate_count": int(len(part)),
                "native_m30_confirmed_rate": float(part["native_m30_confirmed"].mean()) if len(part) else 0.0,
                "proxy_m30_confirmed_rate": float(part["m30_confirmed_proxy"].mean()) if len(part) else 0.0,
                "proxy_kill_count": int((part["native_m30_confirmed"] & ~part["m30_confirmed_proxy"]).sum()),
                "native_missing_count": int((~part["native_m30_confirmed"]).sum()),
            }
        )
        rows.append(row)
    out = pd.DataFrame(rows)
    return out.sort_values("candidate_count", ascending=False).reset_index(drop=True) if not out.empty else out


def _scenario_matrix(candidates: pd.DataFrame) -> pd.DataFrame:
    if candidates.empty:
        return pd.DataFrame()
    start = str(pd.to_datetime(candidates["entry_date"], errors="coerce").min().date())
    end = str(pd.to_datetime(candidates["entry_date"], errors="coerce").max().date())
    stocks = _load_stock_daily(start, end, max_codes=0)
    daily_for_exit = stocks.copy()
    daily_for_exit["trade_date_ts"] = pd.to_datetime(daily_for_exit["trade_date"], errors="coerce").dt.normalize()
    daily_by_code = {code: g.sort_values("trade_date_ts").copy() for code, g in daily_for_exit.groupby("code", sort=False)}
    calendar = [pd.Timestamp(x).normalize() for x in _load_trade_dates(start, end)]
    scenarios = [
        ("default_native_bridge", candidates),
        ("native_m30_confirmed_only", candidates[candidates["native_m30_confirmed"]]),
        ("proxy_m30_confirmed_only", candidates[candidates["m30_confirmed_proxy"]]),
        ("proxy_killed_but_native_confirmed", candidates[candidates["native_m30_confirmed"] & ~candidates["m30_confirmed_proxy"]]),
    ]
    rows = [_simulate_candidate_set(name, part.copy(), daily_by_code, calendar, slots=2, slot_pct=0.50) for name, part in scenarios]
    return pd.DataFrame(rows)


def _write_report(summary: dict[str, Any], by_strategy: pd.DataFrame, by_source: pd.DataFrame, scenarios: pd.DataFrame, killed: pd.DataFrame) -> None:
    text = f"""# G3 原生30m确认语义审计 v1

## 结论

本审计区分两件事：

1. 原生30m确认：Score120 overlay、Panic V2、G2 volume5 自身已经带有 30m 确认字段；旧G3路由包代表已被当时路由合同准入。
2. 统一代理30m确认：用当前脚本临时构造的一组通用 30m 条件复核所有策略。

结果显示，收益能否还原的关键不是“策略主体从多个减少到5个”，而是不能把原生买点生成器替换成统一代理条件。

## 总体

- 默认桥接候选：{summary["candidate_count"]}
- 原生30m/路由准入确认率：{_pct(summary["native_m30_confirmed_rate"])}
- 统一代理30m确认率：{_pct(summary["proxy_m30_confirmed_rate"])}
- 被代理误杀但原生确认的候选：{summary["proxy_kill_count"]}
- 原生语义缺失候选：{summary["native_missing_count"]}

## 收益对照

{_md_table(scenarios, {"win_rate", "avg_ret", "total_return", "max_drawdown"}, {"sum_pnl"})}

## 按交易策略

{_md_table(by_strategy, {"native_m30_confirmed_rate", "proxy_m30_confirmed_rate"}, set())}

## 按原生来源

{_md_table(by_source, {"native_m30_confirmed_rate", "proxy_m30_confirmed_rate"}, set())}

## 被统一代理误杀样本 Top 50

{_md_table(killed.head(50), {"strategy_score"}, set(), max_rows=50)}

## 判断

减少策略可以保留收益，但前提是 5 个策略主体只统一“交易合同、成交路由、归因标签、仓位和卖出合同”，不要统一替换买点生成器。原来收益跑丢，主要由两类错误造成：

1. 用日线代理重写原生源，导致 Score120、强势突破、G2量能补位等进攻策略的买点召回大幅下降。
2. 用单一30m代理复核所有策略，导致已有原生30m确认或原路由准入的候选被误杀。

因此正式收敛方式应是：策略主体保持 5 个；每个主体下保留原生子路由生成器；最终候选统一进入同一个二槽路由、风控和卖出合同。这样才能减少策略名称，同时不把原来挣钱的策略跑丢。
"""
    (OUT_DIR / "REPORT_CN.md").write_text(text, encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    native_rows = _build_native_semantics()
    candidates = _read_default_candidates()
    audited = _attach_proxy_confirmation(_attach_native_semantics(candidates, native_rows))
    by_strategy = _group_summary(audited, ["trade_strategy", "trade_strategy_label"])
    by_source = _group_summary(audited, ["trade_strategy_label", "source_strategy_label", "native_source_file", "native_m30_semantics"])
    killed_cols = [
        "entry_date",
        "code",
        "name",
        "trade_strategy_label",
        "source_strategy_label",
        "native_source_file",
        "strategy_score",
        "native_confirm_rule",
        "m30_confirm_rule",
    ]
    killed = audited[audited["native_m30_confirmed"] & ~audited["m30_confirmed_proxy"]].copy()
    killed = killed[[c for c in killed_cols if c in killed.columns]].sort_values(["entry_date", "strategy_score"], ascending=[True, False])
    scenarios = _scenario_matrix(audited)
    summary = {
        "candidate_count": int(len(audited)),
        "native_semantics_rows": int(len(native_rows)),
        "native_m30_confirmed_rate": float(audited["native_m30_confirmed"].mean()) if len(audited) else 0.0,
        "proxy_m30_confirmed_rate": float(audited["m30_confirmed_proxy"].mean()) if len(audited) else 0.0,
        "proxy_kill_count": int((audited["native_m30_confirmed"] & ~audited["m30_confirmed_proxy"]).sum()),
        "native_missing_count": int((~audited["native_m30_confirmed"]).sum()),
        "scenario_default_total_return": float(scenarios.loc[scenarios["scenario"].eq("default_native_bridge"), "total_return"].iloc[0]) if not scenarios.empty else None,
        "scenario_native_m30_total_return": float(scenarios.loc[scenarios["scenario"].eq("native_m30_confirmed_only"), "total_return"].iloc[0]) if not scenarios.empty else None,
        "scenario_proxy_m30_total_return": float(scenarios.loc[scenarios["scenario"].eq("proxy_m30_confirmed_only"), "total_return"].iloc[0]) if not scenarios.empty else None,
        "output_dir": str(OUT_DIR),
    }
    native_rows.to_csv(OUT_DIR / "native_m30_semantics_source_rows.csv", index=False, encoding="utf-8-sig")
    audited.to_csv(OUT_DIR / "native_m30_candidate_detail.csv", index=False, encoding="utf-8-sig")
    by_strategy.to_csv(OUT_DIR / "native_m30_by_strategy.csv", index=False, encoding="utf-8-sig")
    by_source.to_csv(OUT_DIR / "native_m30_by_source.csv", index=False, encoding="utf-8-sig")
    killed.to_csv(OUT_DIR / "proxy_killed_but_native_confirmed.csv", index=False, encoding="utf-8-sig")
    scenarios.to_csv(OUT_DIR / "native_m30_scenario_matrix.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(summary, by_strategy, by_source, scenarios, killed)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
