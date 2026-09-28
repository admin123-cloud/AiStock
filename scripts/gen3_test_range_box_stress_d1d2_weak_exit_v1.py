from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_test_range_30m_volume_acceptance_v1 import (  # noqa: E402
    INITIAL_CAPITAL,
    PROFILES,
    max_drawdown,
    simulate,
    window_metrics,
)
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


SOURCE = ROOT / "reports" / "gen3_range_30m_volume_acceptance_v1" / "box_stress_accept_h5_signals.csv"
OUT_DIR = ROOT / "reports" / "gen3_range_box_stress_d1d2_weak_exit_v1"
WINDOWS = {
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-12-31"),
    "full": ("2020-01-01", "2026-12-31"),
}

VARIANTS = [
    {
        "variant": "baseline_h5",
        "desc": "原始横盘箱体底部放量承接，持有5日",
        "d1_exit": False,
        "d2_exit": False,
    },
    {
        "variant": "d1_weak_exit_d2open",
        "desc": "D1弱确认后，D2开盘快速退出",
        "d1_exit": True,
        "d2_exit": False,
    },
    {
        "variant": "d2_still_weak_exit_d3open",
        "desc": "D2仍弱后，D3开盘快速退出",
        "d1_exit": False,
        "d2_exit": True,
    },
    {
        "variant": "d1_or_d2_weak_exit",
        "desc": "D1弱或D2仍弱，下一交易日开盘退出",
        "d1_exit": True,
        "d2_exit": True,
    },
]


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item: dict[str, Any] = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = pct(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def load_daily_opens(signals: pd.DataFrame) -> pd.DataFrame:
    codes = sorted(signals["code"].dropna().astype(str).unique().tolist())
    start = pd.to_datetime(signals["entry_date"]).min().strftime("%Y-%m-%d")
    end = (pd.to_datetime(signals["entry_date"]).max() + pd.Timedelta(days=15)).strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 500):
        batch = codes[i : i + 500]
        quoted = ", ".join(f"'{code}'" for code in batch)
        part = clickhouse_query_df(
            f"""
            SELECT code, trade_date, open
            FROM kline_daily
            WHERE code IN ({quoted})
              AND trade_date BETWEEN toDate(%(start)s) AND toDate(%(end)s)
            ORDER BY code, trade_date
            """,
            {"start": start, "end": end},
        )
        if not part.empty:
            parts.append(part)
    daily = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if daily.empty:
        return daily
    daily["trade_date"] = pd.to_datetime(daily["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    daily["open"] = pd.to_numeric(daily["open"], errors="coerce")
    daily = daily.dropna(subset=["code", "trade_date", "open"]).sort_values(["code", "trade_date"]).reset_index(drop=True)
    g = daily.groupby("code", sort=False)
    daily["d2_open"] = g["open"].shift(-2)
    daily["d3_open"] = g["open"].shift(-3)
    return daily.rename(columns={"trade_date": "entry_date"})[["code", "entry_date", "d2_open", "d3_open"]]


def prepare_base() -> pd.DataFrame:
    d = pd.read_csv(SOURCE)
    opens = load_daily_opens(d)
    d = d.merge(opens, on=["code", "entry_date"], how="left")
    d["entry_date_ts"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["entry_date_ts"], errors="coerce") + pd.Timedelta(days=8)
    for col in [
        "entry_price_adjusted",
        "fwd_ret_confirm_to_close_2d",
        "fwd_ret_confirm_to_close_3d",
        "fwd_ret_confirm_to_close_5d",
        "candidate_score",
        "amount_ratio3",
        "d2_open",
        "d3_open",
    ]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d["rank_key"] = d["candidate_score"].fillna(0.0) + d["amount_ratio3"].fillna(0.0) * 0.02
    d["entry_price_used"] = d["entry_price_adjusted"]
    return d.dropna(subset=["entry_date_ts", "entry_price_adjusted", "fwd_ret_confirm_to_close_5d"]).copy()


def apply_exit_policy(base: pd.DataFrame, spec: dict[str, Any], profile: dict[str, Any]) -> pd.DataFrame:
    d = base.copy()
    cost = float(profile["cost_bps"]) / 10000.0
    shock = float(profile["shock"])
    d["d1_weak_trigger"] = d["fwd_ret_confirm_to_close_2d"].le(-0.03)
    d["d2_weak_trigger"] = d["fwd_ret_confirm_to_close_3d"].le(-0.03)
    d["exit_reason"] = "hold5"
    raw_ret = d["fwd_ret_confirm_to_close_5d"].copy()

    if bool(spec["d1_exit"]):
        mask = d["d1_weak_trigger"] & d["d2_open"].notna()
        raw_ret.loc[mask] = d.loc[mask, "d2_open"] / d.loc[mask, "entry_price_adjusted"] - 1.0
        d.loc[mask, "exit_reason"] = "d1_weak_d2open"

    if bool(spec["d2_exit"]):
        mask = d["exit_reason"].eq("hold5") & d["d2_weak_trigger"] & d["d3_open"].notna()
        raw_ret.loc[mask] = d.loc[mask, "d3_open"] / d.loc[mask, "entry_price_adjusted"] - 1.0
        d.loc[mask, "exit_reason"] = "d2_weak_d3open"

    d["policy_net_ret"] = raw_ret - cost - shock
    d["variant"] = spec["variant"]
    d["desc"] = spec["desc"]
    return d


def summarize(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, profile: str, desc: str) -> dict[str, Any]:
    net = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    return {
        "variant": variant,
        "desc": desc,
        "profile": profile,
        "trade_count": int(len(closed)),
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0) if not curve.empty else 0.0,
        "max_drawdown": max_drawdown(curve["equity"]) if not curve.empty else 0.0,
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_trade_return": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "d1_exit_count": int(closed.get("exit_reason", pd.Series(dtype=str)).eq("d1_weak_d2open").sum()) if not closed.empty else 0,
        "d2_exit_count": int(closed.get("exit_reason", pd.Series(dtype=str)).eq("d2_weak_d3open").sum()) if not closed.empty else 0,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = prepare_base()
    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    for spec in VARIANTS:
        for profile in PROFILES:
            candidates = apply_exit_policy(base, spec, profile)
            curve, closed = simulate(candidates)
            run_dir = OUT_DIR / f"{spec['variant']}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(summarize(curve, closed, str(spec["variant"]), str(profile["profile"]), str(spec["desc"])))
            window_rows.extend(window_metrics(curve, closed, str(spec["variant"]), str(profile["profile"])))

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "return"}
    focus = summary[summary["profile"].isin(["cost30", "cost100", "shock2_cost30"])].copy()
    lines = [
        "# G3 box_stress_accept_h5 D1/D2早期弱确认退出复验 v1",
        "",
        "## 策略名解释",
        "",
        "- `box_stress_accept_h5`：中文是“横盘箱体底部放量承接，持有5日”。先找箱体底部附近的日线候选，再用30m放量承接确认买入。",
        "- `d1_weak_exit_d2open`：D1收盘相对确认买入价跌超过3%，下一交易日开盘快速退出。",
        "- `d2_still_weak_exit_d3open`：D2收盘相对确认买入价仍跌超过3%，下一交易日开盘快速退出。",
        "- `d1_or_d2_weak_exit`：D1弱先退；若D1未触发但D2仍弱，再下一交易日开盘退出。",
        "",
        "## 复验结果",
        "",
        md_table(focus, pct_cols=pct_cols),
        "",
        "## 分窗口结果",
        "",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 该脚本用于验证早期弱确认是否比继续调止盈止损更接近问题根部。",
        "- 若收益和回撤同时改善，下一步再做跌停不可卖、滑点和次日低开压力。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
