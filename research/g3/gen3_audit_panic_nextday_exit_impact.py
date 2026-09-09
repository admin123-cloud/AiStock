from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_panic_final_candidate_v1 import _sql_literal
from utils.market_warehouse import clickhouse_query_df


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def _load_trades(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path)
    for col in ["entry_date", "policy_exit_date", "exit_date", "trigger_datetime", "exit_datetime", "confirm_datetime"]:
        if col in d.columns:
            d[col] = pd.to_datetime(d[col], errors="coerce")
    for col in ["entry_price_adjusted", "policy_net_ret", "trigger_close_ret", "exit_ret_net", "exit_price"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["entry_date", "code", "policy_net_ret"]).copy()


def _load_daily_open(trades: pd.DataFrame) -> dict[tuple[str, pd.Timestamp], float]:
    early = trades[trades["exit_datetime"].notna()].copy()
    if early.empty:
        return {}
    codes = sorted(early["code"].dropna().astype(str).unique().tolist())
    start = early["trigger_datetime"].min().strftime("%Y-%m-%d")
    end = (early["exit_datetime"].max() + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 500):
        quoted = ",".join(_sql_literal(c) for c in codes[i : i + 500])
        part = clickhouse_query_df(
            f"""
            SELECT code, trade_date, open
            FROM kline_daily
            WHERE code IN ({quoted})
              AND trade_date BETWEEN toDate({_sql_literal(start)}) AND toDate({_sql_literal(end)})
            ORDER BY code, trade_date
            """
        )
        if not part.empty:
            parts.append(part)
    d = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if d.empty:
        return {}
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.normalize()
    d["open"] = pd.to_numeric(d["open"], errors="coerce")
    d = d.dropna(subset=["code", "trade_date", "open"])
    return {(str(r.code), pd.Timestamp(r.trade_date).normalize()): float(r.open) for r in d.itertuples(index=False)}


def _classify_exit_timing(row: pd.Series) -> str:
    if pd.isna(row.get("trigger_datetime")) or pd.isna(row.get("exit_datetime")):
        return "no_early_exit"
    trigger = pd.Timestamp(row["trigger_datetime"])
    exit_dt = pd.Timestamp(row["exit_datetime"])
    if trigger.normalize() != exit_dt.normalize():
        return "next_trading_day_open"
    hour = trigger.hour + trigger.minute / 60.0
    if hour >= 14.5:
        return "late_day_same_day_next_bar"
    if hour >= 14.0:
        return "afternoon_late_same_day"
    return "same_day_intraday"


def _summarize(d: pd.DataFrame, group_col: str) -> pd.DataFrame:
    rows: list[dict] = []
    for key, g in d.groupby(group_col, dropna=False, sort=True):
        rows.append(
            {
                group_col: key,
                "trades": int(len(g)),
                "win_rate": float((g["policy_net_ret"] > 0).mean()),
                "mean_policy_ret": float(g["policy_net_ret"].mean()),
                "median_policy_ret": float(g["policy_net_ret"].median()),
                "worst_policy_ret": float(g["policy_net_ret"].min()),
                "mean_trigger_close_ret": float(g["trigger_close_ret"].mean()) if "trigger_close_ret" in g.columns else float("nan"),
                "mean_exit_ret_net": float(g["exit_ret_net"].mean()) if "exit_ret_net" in g.columns else float("nan"),
                "mean_nextday_open_gap_vs_exit": float(g["nextday_open_gap_vs_exit"].mean()) if "nextday_open_gap_vs_exit" in g.columns else float("nan"),
                "worst_nextday_open_gap_vs_exit": float(g["nextday_open_gap_vs_exit"].min()) if "nextday_open_gap_vs_exit" in g.columns else float("nan"),
            }
        )
    return pd.DataFrame(rows)


def _display(raw: pd.DataFrame) -> pd.DataFrame:
    out = raw.copy()
    for col in [
        "win_rate",
        "mean_policy_ret",
        "median_policy_ret",
        "worst_policy_ret",
        "mean_trigger_close_ret",
        "mean_exit_ret_net",
        "mean_nextday_open_gap_vs_exit",
        "worst_nextday_open_gap_vs_exit",
    ]:
        if col in out.columns:
            out[col] = out[col].map(_pct)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit next-day open impact for G3 panic 30m exits.")
    parser.add_argument(
        "--input",
        default="reports/gen3_panic_v2_research/final_candidate_v1/m30_close5_full_nextopen_cost30_closed_trades.csv",
    )
    parser.add_argument("--output-dir", default="reports/gen3_panic_v2_research/nextday_exit_impact_v1")
    args = parser.parse_args()

    source = Path(args.input)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    trades = _load_trades(source)
    trades["exit_timing_bucket"] = trades.apply(_classify_exit_timing, axis=1)
    open_map = _load_daily_open(trades)

    def _nextday_gap(row: pd.Series) -> float:
        if pd.isna(row.get("exit_datetime")) or pd.isna(row.get("exit_price")):
            return float("nan")
        exit_dt = pd.Timestamp(row["exit_datetime"])
        if pd.Timestamp(row["trigger_datetime"]).normalize() == exit_dt.normalize():
            return float("nan")
        open_price = open_map.get((str(row["code"]), exit_dt.normalize()))
        if open_price is None or float(row["exit_price"]) <= 0:
            return float("nan")
        return open_price / float(row["exit_price"]) - 1.0

    trades["nextday_open_gap_vs_exit"] = trades.apply(_nextday_gap, axis=1)
    trades.to_csv(out_dir / "nextday_exit_impact_trades.csv", index=False, encoding="utf-8-sig")
    raw = _summarize(trades, "exit_timing_bucket")
    raw.to_csv(out_dir / "nextday_exit_impact_summary_raw.csv", index=False, encoding="utf-8-sig")
    display = _display(raw)
    display.to_csv(out_dir / "nextday_exit_impact_summary_display.csv", index=False, encoding="utf-8-sig")

    early = trades[trades["exit_timing_bucket"].ne("no_early_exit")].copy()
    early_show_cols = [
        "entry_date",
        "code",
        "name",
        "policy_net_ret",
        "trigger_datetime",
        "trigger_close_ret",
        "exit_datetime",
        "exit_price",
        "exit_timing_bucket",
        "nextday_open_gap_vs_exit",
    ]
    early_show = early[[c for c in early_show_cols if c in early.columns]].copy()
    for col in ["policy_net_ret", "trigger_close_ret", "nextday_open_gap_vs_exit"]:
        if col in early_show.columns:
            early_show[col] = early_show[col].map(_pct)

    lines = [
        "# G3 Panic 次日开盘退出冲击审计 V1",
        "",
        "## 口径",
        "",
        f"- 输入：`{source}`",
        "- 仅统计 30m -5% 失败退出触发后，成交落在同日下一根还是下一交易日开盘。",
        "- 本审计不改变回测收益，只把尾盘/隔日执行风险单独暴露出来。",
        "",
        "## 分桶摘要",
        "",
        display.to_markdown(index=False),
        "",
        "## 早退明细",
        "",
        early_show.to_markdown(index=False) if not early_show.empty else "无 30m 早退交易。",
        "",
        "## 判断",
        "",
        "1. 如果 `next_trading_day_open` 样本很多，说明 30m 退出高度依赖隔夜流动性，需要单独加隔夜冲击压力。",
        "2. 如果样本很少，则当前主要风险仍是同日下一根 bar 的成交质量，而不是尾盘隔夜退出。",
    ]
    (out_dir / "nextday_exit_impact_report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary = {
        "input": str(source),
        "output_dir": str(out_dir),
        "trades": int(len(trades)),
        "early_exits": int(len(early)),
        "nextday_open_exits": int(trades["exit_timing_bucket"].eq("next_trading_day_open").sum()),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
