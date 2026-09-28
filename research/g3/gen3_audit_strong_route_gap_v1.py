from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
OUT_DIR = _report_path() / "gen3_strong_route_gap_audit_v1"

G2_ONLY = _report_path() / "gen3_g2_full_gap_attribution_v1" / "g2_only_trades.csv"
G2_SOURCE = _report_path() / "gen2_v2_complete_strategy_2020" / "sources" / "g2_v2_complete.parquet"
G3_STRONG_DIR = _report_path() / "gen3_strong_v2_independent_source_v1"
G3_ROUTE_TRADES = (
    _report_path()
    / "gen3_route_execution_mandate_candidate_package_v3"
    / "g3_route_execution_mandate_candidate_closed_trades.csv"
)

MAINUP_CANDIDATES = G3_STRONG_DIR / "strong_v2_main_up_only_candidates_no_forward.csv"
PLUSWEAK_CANDIDATES = G3_STRONG_DIR / "strong_v2_main_up_plus_weak04_candidates_no_forward.csv"
MAINUP_HOLD5 = G3_STRONG_DIR / "strong_v2_main_up_only_hold5_closed_trades.csv"
PLUSWEAK_HOLD5 = G3_STRONG_DIR / "strong_v2_main_up_plus_weak04_hold5_closed_trades.csv"


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path, low_memory=False)


def _key(df: pd.DataFrame, date_col: str = "entry_date") -> pd.Series:
    date = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
    return df["code"].astype(str) + "|" + date.astype(str)


def _to_num(s: pd.Series | Any) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def classify_g3_market_style(row: pd.Series) -> str:
    close = row.get("index_close")
    ma20 = row.get("index_ma20")
    ma60 = row.get("index_ma60")
    mom20 = row.get("index_mom20")
    breadth = row.get("market_breadth")
    if pd.notna(close) and pd.notna(ma20) and pd.notna(ma60) and close >= ma20 >= ma60:
        return "main_up"
    if (
        pd.notna(close)
        and pd.notna(ma20)
        and pd.notna(ma60)
        and pd.notna(mom20)
        and pd.notna(breadth)
        and ma20 < ma60
        and close >= ma60
        and mom20 > 0
        and breadth >= 0.5
    ):
        return "weak_recovery"
    if pd.notna(close) and pd.notna(ma20) and close < ma20:
        return "defense_or_failed"
    return "neutral"


def _load_g2_source() -> pd.DataFrame:
    src = pd.read_parquet(G2_SOURCE).copy()
    src["entry_date"] = pd.to_datetime(src["entry_date"], errors="coerce").dt.normalize()
    src = src.dropna(subset=["entry_date", "code"]).copy()
    src["key"] = _key(src)
    src["g3_market_style_rebuilt"] = src.apply(classify_g3_market_style, axis=1)
    for col in [
        "runup_from_60d_low",
        "score_volume5",
        "v4_rank",
        "v4_score",
        "sector_score_bonus",
        "l3_s3",
        "l2_s3",
        "index_close",
        "index_ma20",
        "index_ma60",
        "index_mom20",
        "market_breadth",
    ]:
        if col in src.columns:
            src[col] = _to_num(src[col])
    src["g3_strong_score_rebuilt"] = (
        src.get("v4_score", 0).fillna(0.0)
        + src.get("sector_score_bonus", 0).fillna(0.0)
        + src.get("l3_s3", 0).fillna(0.0) * 0.03
        + src.get("l2_s3", 0).fillna(0.0) * 0.02
    )
    keep = [
        "key",
        "entry_date",
        "code",
        "name",
        "source_family",
        "signal_family",
        "g2_v2_buy_logic",
        "trigger_type",
        "g3_market_style_rebuilt",
        "g3_strong_score_rebuilt",
        "v4_rank",
        "v4_score",
        "score_volume5",
        "runup_from_60d_low",
        "sector_strong",
        "sector_score_bonus",
        "l3_s3",
        "l2_s3",
        "index_close",
        "index_ma20",
        "index_ma60",
        "index_mom20",
        "market_breadth",
    ]
    return src[[c for c in keep if c in src.columns]].drop_duplicates("key")


def _load_key_set(path: Path) -> set[str]:
    df = _read_csv(path)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.normalize()
    df = df.dropna(subset=["entry_date", "code"]).copy()
    return set(_key(df))


def _daily_rank(path: Path, score_col: str = "g3_strong_score") -> pd.DataFrame:
    df = _read_csv(path)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.normalize()
    df = df.dropna(subset=["entry_date", "code"]).copy()
    df["key"] = _key(df)
    df[score_col] = _to_num(df[score_col]) if score_col in df.columns else 0.0
    df["v4_rank"] = _to_num(df["v4_rank"]) if "v4_rank" in df.columns else 999999
    df = df.sort_values(["entry_date", score_col, "v4_rank", "code"], ascending=[True, False, True, True])
    df["candidate_rank_in_day"] = df.groupby("entry_date").cumcount() + 1
    df["candidate_count_in_day"] = df.groupby("entry_date")["key"].transform("size")
    return df[["key", "candidate_rank_in_day", "candidate_count_in_day"]].drop_duplicates("key")


def _reason(row: pd.Series) -> str:
    if not bool(row.get("in_g2_source", False)):
        return "00_source_missing"
    if str(row.get("source_family_src", "")) != "volume5":
        return "10_not_volume5_source"
    if pd.isna(row.get("score_volume5_src")):
        return "20_score_volume5_missing"
    if pd.notna(row.get("runup_from_60d_low_src")) and float(row["runup_from_60d_low_src"]) > 1.0:
        return "30_runup_gt_100pct_filter"
    style = str(row.get("g3_market_style_rebuilt_src", ""))
    if style not in {"main_up", "weak_recovery"}:
        return "40_market_style_filtered"
    if not bool(row.get("in_plusweak_candidate", False)):
        return "50_plusweak_candidate_missing_other"
    if bool(row.get("in_plusweak_candidate", False)) and not bool(row.get("in_mainup_candidate", False)):
        return "60_mainup_only_narrowed_style"
    if bool(row.get("in_mainup_candidate", False)) and not bool(row.get("in_mainup_hold5", False)):
        return "70_daily_slot_or_hold5_sim_not_selected"
    if bool(row.get("in_mainup_hold5", False)) and not bool(row.get("in_g3_route_exec", False)):
        return "80_router_priority_or_route_limit"
    return "90_present_but_execution_gap"


def _summary(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for reason, g in df.groupby("miss_reason", dropna=False):
        pnl = _to_num(g["pnl"]).fillna(0.0)
        ret = _to_num(g["net_return"]).dropna()
        rows.append(
            {
                "miss_reason": reason,
                "trade_count": int(len(g)),
                "pnl_sum": float(pnl.sum()),
                "pnl_share": None,
                "avg_g2_return": float(ret.mean()) if not ret.empty else None,
                "best_g2_return": float(ret.max()) if not ret.empty else None,
                "median_candidate_rank": float(_to_num(g["mainup_candidate_rank"]).median())
                if "mainup_candidate_rank" in g
                else None,
            }
        )
    out = pd.DataFrame(rows).sort_values("pnl_sum", ascending=False)
    total = float(out["pnl_sum"].sum()) if not out.empty else 0.0
    if total:
        out["pnl_share"] = out["pnl_sum"] / total
    return out


from research.common.reporting import percent_text as _pct


def _num(value: Any, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value):.{digits}f}"


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = _pct(value)
            elif isinstance(value, float):
                item[col] = _num(value, 4)
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def _write_report(audit: pd.DataFrame, reason_summary: pd.DataFrame, top: pd.DataFrame, summary: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "# G3 强势链路缺口审计 v1",
        "",
        "## 结论",
        "",
        "- 这份审计只解释 G2 full 近两年 G2-only 强势交易为什么没有被当前 G3 V3 吃到，不用于直接调参。",
        "- 当前 G3 的主要问题不是防守模块太弱，而是强势周期的进攻引擎被压窄：候选源、路由额度和退出执行都弱于 G2 full。",
        "- 后续不应该拿少数大赢家反向拟合参数，而应该重建一条固定规则的强势路由，再做 train/valid/blind 分段验证。",
        "",
        "## 总览",
        "",
        f"- 审计交易数：{summary['trade_count']}",
        f"- G2-only 合计 PnL：{_num(summary['pnl_sum'], 2)}",
        f"- 已进入 main_up_only 候选比例：{_pct(summary['mainup_candidate_rate'])}",
        f"- 已进入 plus_weak 候选比例：{_pct(summary['plusweak_candidate_rate'])}",
        f"- 已被 main_up_only hold5 模拟选中比例：{_pct(summary['mainup_hold5_rate'])}",
        f"- 已被 G3 V3 路由执行比例：{_pct(summary['route_exec_rate'])}",
        "",
        "## 缺失原因归因",
        "",
        _md_table(
            reason_summary,
            pct_cols={"pnl_share", "avg_g2_return", "best_g2_return"},
        ),
        "",
        "## 错失利润最大的 G2-only 交易",
        "",
        _md_table(
            top[
                [
                    "entry_date",
                    "code",
                    "name",
                    "pnl",
                    "net_return",
                    "miss_reason",
                    "g3_market_style_rebuilt_src",
                    "v4_rank_src",
                    "mainup_candidate_rank",
                    "plusweak_candidate_rank",
                    "exit_reason",
                ]
            ],
            pct_cols={"net_return"},
        ),
        "",
        "## 下一步",
        "",
        "1. 不再把 strong_main 绑定为 `main_up_only_hold5`，先测试 `main_up + weak_recovery` 的强势候选是否能恢复收益覆盖。",
        "2. 把 G2 full 的 30m 分批止盈 + 前日低点移动退出迁移成 G3 strong route 的专属执行器，和弱势/range 执行器分开。",
        "3. 用 2020-2023 / 2024-2025 / 2026YTD 三段固定验证，任何提升必须同时报告收益、回撤、交易数和错失大赢家覆盖率。",
        "",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    g2_only = _read_csv(G2_ONLY)
    g2_only["entry_date"] = pd.to_datetime(g2_only["entry_date"], errors="coerce").dt.normalize()
    g2_only = g2_only.dropna(subset=["entry_date", "code"]).copy()
    g2_only["key"] = _key(g2_only)
    g2_only = g2_only[g2_only["entry_date"].ge(pd.Timestamp("2024-06-01"))].copy()

    src = _load_g2_source()
    mainup_candidates = _load_key_set(MAINUP_CANDIDATES)
    plusweak_candidates = _load_key_set(PLUSWEAK_CANDIDATES)
    mainup_hold5 = _load_key_set(MAINUP_HOLD5)
    plusweak_hold5 = _load_key_set(PLUSWEAK_HOLD5)

    route = _read_csv(G3_ROUTE_TRADES)
    route["entry_date"] = pd.to_datetime(route["entry_date"], errors="coerce").dt.normalize()
    route = route.dropna(subset=["entry_date", "code"]).copy()
    route["key"] = _key(route)
    route_exec = set(route["key"])

    mainup_rank = _daily_rank(MAINUP_CANDIDATES).rename(
        columns={
            "candidate_rank_in_day": "mainup_candidate_rank",
            "candidate_count_in_day": "mainup_candidate_count",
        }
    )
    plusweak_rank = _daily_rank(PLUSWEAK_CANDIDATES).rename(
        columns={
            "candidate_rank_in_day": "plusweak_candidate_rank",
            "candidate_count_in_day": "plusweak_candidate_count",
        }
    )

    audit = g2_only.merge(src.add_suffix("_src"), left_on="key", right_on="key_src", how="left")
    audit["in_g2_source"] = audit["key_src"].notna()
    audit["in_mainup_candidate"] = audit["key"].isin(mainup_candidates)
    audit["in_plusweak_candidate"] = audit["key"].isin(plusweak_candidates)
    audit["in_mainup_hold5"] = audit["key"].isin(mainup_hold5)
    audit["in_plusweak_hold5"] = audit["key"].isin(plusweak_hold5)
    audit["in_g3_route_exec"] = audit["key"].isin(route_exec)
    audit = audit.merge(mainup_rank, on="key", how="left").merge(plusweak_rank, on="key", how="left")
    audit["miss_reason"] = audit.apply(_reason, axis=1)

    reason_summary = _summary(audit)
    top = audit.sort_values("pnl", ascending=False).head(20)
    summary = {
        "trade_count": int(len(audit)),
        "pnl_sum": float(_to_num(audit["pnl"]).fillna(0.0).sum()),
        "mainup_candidate_rate": float(audit["in_mainup_candidate"].mean()) if len(audit) else None,
        "plusweak_candidate_rate": float(audit["in_plusweak_candidate"].mean()) if len(audit) else None,
        "mainup_hold5_rate": float(audit["in_mainup_hold5"].mean()) if len(audit) else None,
        "plusweak_hold5_rate": float(audit["in_plusweak_hold5"].mean()) if len(audit) else None,
        "route_exec_rate": float(audit["in_g3_route_exec"].mean()) if len(audit) else None,
        "reason_count": reason_summary.to_dict(orient="records"),
    }

    audit.to_csv(OUT_DIR / "g2_only_filter_reasons.csv", index=False, encoding="utf-8-sig")
    reason_summary.to_csv(OUT_DIR / "filter_reason_summary.csv", index=False, encoding="utf-8-sig")
    top.to_csv(OUT_DIR / "top_missed_winners.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(audit, reason_summary, top, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
