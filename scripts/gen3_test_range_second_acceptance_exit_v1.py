from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar  # noqa: E402
from scripts.gen3_test_range_30m_volume_acceptance_v1 import (  # noqa: E402
    PROFILES,
    exit_dates,
    standardize,
    window_metrics,
)
from scripts.gen3_test_range_box_stress_icepoint_climax_overlay_v1 import md_table, simulate_scaled, summarize  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


SOURCE = ROOT / "reports" / "gen3_range_second_acceptance_v1" / "deep_and_reclaim_any_second_accept_signals.csv"
OUT_DIR = ROOT / "reports" / "gen3_range_second_acceptance_exit_v1"

VARIANTS = [
    {
        "variant": "baseline_hold5",
        "desc": "二次承接后固定持有5日",
        "mode": "baseline",
    },
    {
        "variant": "d1_nonpositive_exit_d2open",
        "desc": "D1收盘无正反馈则D2开盘退出",
        "mode": "d1_nonpositive",
    },
    {
        "variant": "d2_no_follow_exit_d3open",
        "desc": "D2仍无正反馈则D3开盘退出",
        "mode": "d2_nonpositive",
    },
    {
        "variant": "d1_or_d2_no_follow_exit",
        "desc": "D1无正反馈先退，否则D2仍无正反馈再退",
        "mode": "d1_or_d2_nonpositive",
    },
]


def sql_list(values: list[str]) -> str:
    return ",".join("'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'" for v in values)


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def load_signals() -> pd.DataFrame:
    d = pd.read_csv(SOURCE, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    d["hold_days"] = 5
    for col in [
        "entry_price_adjusted",
        "fwd_ret_confirm_to_close_1d",
        "fwd_ret_confirm_to_close_2d",
        "fwd_ret_confirm_to_close_5d",
        "candidate_score",
        "amount_ratio3",
        "rank_key",
    ]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d["rank_key"] = d["rank_key"].fillna(d["candidate_score"].fillna(0.0) + d["amount_ratio3"].fillna(0.0) * 0.02)
    return d.dropna(subset=["entry_date", "code", "entry_price_adjusted", "fwd_ret_confirm_to_close_5d"]).copy()


def load_daily_opens(signals: pd.DataFrame) -> pd.DataFrame:
    codes = sorted(signals["code"].dropna().astype(str).unique().tolist())
    start = signals["entry_date"].min().strftime("%Y-%m-%d")
    end = (signals["entry_date"].max() + pd.Timedelta(days=15)).strftime("%Y-%m-%d")
    sql = f"""
    SELECT code, trade_date, open
    FROM kline_daily
    WHERE code IN ({sql_list(codes)})
      AND trade_date BETWEEN toDate('{start}') AND toDate('{end}')
    ORDER BY code, trade_date
    """
    daily = clickhouse_query_df(sql)
    if daily.empty:
        return daily
    daily["trade_date"] = pd.to_datetime(daily["trade_date"], errors="coerce").dt.normalize()
    daily["code"] = daily["code"].astype(str)
    daily["open"] = pd.to_numeric(daily["open"], errors="coerce")
    daily = daily.dropna(subset=["code", "trade_date", "open"]).sort_values(["code", "trade_date"]).reset_index(drop=True)
    g = daily.groupby("code", sort=False)
    daily["d2_open"] = g["open"].shift(-2)
    daily["d3_open"] = g["open"].shift(-3)
    daily["d2_date"] = g["trade_date"].shift(-2)
    daily["d3_date"] = g["trade_date"].shift(-3)
    return daily.rename(columns={"trade_date": "entry_date"})[["code", "entry_date", "d2_open", "d3_open", "d2_date", "d3_date"]]


def apply_exit_policy(base: pd.DataFrame, spec: dict[str, str], profile: dict[str, Any]) -> pd.DataFrame:
    d = base.copy()
    cost = float(profile["cost_bps"]) / 10000.0
    shock = float(profile["shock"])
    raw_ret = d["fwd_ret_confirm_to_close_5d"].copy()
    d["exit_reason"] = "hold5"
    d["policy_exit_date"] = d["entry_date"].map(exit_dates(d["entry_date"], 5))

    d1_weak = d["fwd_ret_confirm_to_close_1d"].le(0.0)
    d2_weak = d["fwd_ret_confirm_to_close_2d"].le(0.0)
    mode = spec["mode"]
    if mode in {"d1_nonpositive", "d1_or_d2_nonpositive"}:
        mask = d1_weak & d["d2_open"].notna()
        raw_ret.loc[mask] = d.loc[mask, "d2_open"] / d.loc[mask, "entry_price_adjusted"] - 1.0
        d.loc[mask, "policy_exit_date"] = d.loc[mask, "d2_date"]
        d.loc[mask, "exit_reason"] = "d1_nonpositive_d2open"
    if mode in {"d2_nonpositive", "d1_or_d2_nonpositive"}:
        mask = d["exit_reason"].eq("hold5") & d2_weak & d["d3_open"].notna()
        raw_ret.loc[mask] = d.loc[mask, "d3_open"] / d.loc[mask, "entry_price_adjusted"] - 1.0
        d.loc[mask, "policy_exit_date"] = d.loc[mask, "d3_date"]
        d.loc[mask, "exit_reason"] = "d2_nonpositive_d3open"

    d["policy_net_ret"] = raw_ret - cost - shock
    d["entry_date_ts"] = d["entry_date"]
    d["entry_price_used"] = d["entry_price_adjusted"]
    d["variant"] = spec["variant"]
    d["desc"] = spec["desc"]
    d["position_scale"] = 1.0
    return d.dropna(subset=["policy_exit_date", "policy_net_ret", "entry_price_used"]).copy()


def summarize_exit(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, profile: str, desc: str) -> dict[str, Any]:
    row = summarize(curve, closed, variant, profile, desc)
    exit_reason = closed.get("exit_reason", pd.Series(dtype=str)).astype(str) if not closed.empty else pd.Series(dtype=str)
    row["d1_exit_count"] = int(exit_reason.eq("d1_nonpositive_d2open").sum())
    row["d2_exit_count"] = int(exit_reason.eq("d2_nonpositive_d3open").sum())
    return row


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    raw = load_signals()
    opens = load_daily_opens(raw)
    base = raw.merge(opens, on=["code", "entry_date"], how="left")
    base.to_csv(OUT_DIR / "base_signals_with_exit_opens.csv", index=False, encoding="utf-8-sig")

    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    for spec in VARIANTS:
        for profile in PROFILES:
            candidates = apply_exit_policy(base, spec, profile)
            curve, closed = simulate_scaled(candidates)
            run_dir = OUT_DIR / f"{spec['variant']}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(summarize_exit(curve, closed, str(spec["variant"]), str(profile["profile"]), str(spec["desc"])))
            window_rows.extend(window_metrics(curve, closed, str(spec["variant"]), str(profile["profile"])))

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "return"}
    report = "\n".join(
        [
            "# G3 横盘二次承接 D1/D2 延续退出复验 v1",
            "",
            "## 回测范围",
            "- 策略源：`deep_and_reclaim_any_second_accept`，中文是“箱体底部且日线修复，并要求D0后半日或D1任一再次放量承接”。",
            "- 候选信号/入场窗口：2020-01-01 至 2026-05-29。",
            "- 样本数：22 笔。本轮只验证退出，不新增入场 score/rank 过滤。",
            "",
            "## 策略名解释",
            "- `baseline_hold5`：二次承接后固定持有5日。",
            "- `d1_nonpositive_exit_d2open`：D1收盘没有正收益，则D2开盘退出。",
            "- `d2_no_follow_exit_d3open`：D2收盘仍没有正收益，则D3开盘退出。",
            "- `d1_or_d2_no_follow_exit`：D1无正反馈先退；若D1未触发但D2仍无正反馈，则D3开盘退。",
            "",
            "## 复验结果",
            md_table(summary, pct_cols=pct_cols),
            "",
            "## 分窗口结果",
            md_table(windows, pct_cols=pct_cols),
            "",
            "## 判断口径",
            "- 如果退出只降低收益、没有改善压力口径，则不应加入。",
            "- 如果 D2 无延续退出改善 2026 但伤害 2020-2023 过大，只能作为弱势/中性环境的后续研究，不可直接并入正式组合。",
        ]
    )
    (OUT_DIR / "REPORT.md").write_text(report + "\n", encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
