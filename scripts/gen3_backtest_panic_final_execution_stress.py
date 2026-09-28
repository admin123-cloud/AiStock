from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_audit_panic_30m_failure_visibility import _load_30m
from scripts.gen3_backtest_panic_final_candidate_v1 import (
    _display,
    _metrics,
    _prepare_paths,
    _prepare_policy_candidates,
    _simulate_slot_mtm,
    _sql_literal,
    _trade_calendar,
)
from utils.market_warehouse import clickhouse_query_df


POLICY = "m30_close5_full_nextopen"
DELAY_DAYS = [0, 1, 2]


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    value = pd.to_numeric(v, errors="coerce")
    if pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _load_daily_close_frame(candidates: pd.DataFrame, extra_days: int = 10) -> pd.DataFrame:
    codes = sorted({str(c) for c in candidates["code"].dropna().tolist() if re.fullmatch(r"[0-9A-Z.]+", str(c))})
    if not codes:
        return pd.DataFrame()
    start = candidates["entry_date"].min().strftime("%Y-%m-%d")
    end = (candidates["exit_date"].max() + pd.Timedelta(days=extra_days)).strftime("%Y-%m-%d")
    code_list = ",".join(_sql_literal(code) for code in codes)
    sql = f"""
    SELECT code, trade_date, close
    FROM kline_daily
    WHERE code IN ({code_list})
      AND trade_date BETWEEN toDate({_sql_literal(start)}) AND toDate({_sql_literal(end)})
    ORDER BY code, trade_date
    """
    d = clickhouse_query_df(sql)
    if d.empty:
        return d
    d["trade_date"] = pd.to_datetime(d["trade_date"]).dt.normalize()
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    return d.dropna(subset=["code", "trade_date", "close"])


def _delay_exit_candidates(candidates: pd.DataFrame, delay_days: int, cost_bps: float) -> pd.DataFrame:
    d = candidates.copy()
    d["execution_delay_days"] = delay_days
    if delay_days <= 0:
        return d

    daily = _load_daily_close_frame(d)
    if daily.empty:
        return d
    calendar = _trade_calendar(d["entry_date"].min(), d["exit_date"].max() + pd.Timedelta(days=10))
    idx_map = {day: i for i, day in enumerate(calendar)}
    close_map = {(str(r.code), pd.Timestamp(r.trade_date).normalize()): float(r.close) for r in daily.itertuples(index=False)}

    early = d["executable"].astype(bool) & d["exit_datetime"].notna()
    for idx, row in d[early].iterrows():
        base_exit_date = pd.Timestamp(row["policy_exit_date"]).normalize()
        base_i = idx_map.get(base_exit_date)
        if base_i is None:
            continue
        delayed_i = min(base_i + delay_days, len(calendar) - 1)
        delayed_date = calendar[delayed_i]
        close = close_map.get((str(row["code"]), delayed_date))
        if close is None or float(row["entry_price_adjusted"]) <= 0:
            continue
        delayed_ret = close / float(row["entry_price_adjusted"]) - 1.0 - cost_bps / 10000.0
        d.at[idx, "policy_exit_date"] = delayed_date
        d.at[idx, "policy_net_ret"] = delayed_ret
        d.at[idx, "exit_source"] = f"m30_failure_delayed_{delay_days}d_close"
    return d


def main() -> None:
    parser = argparse.ArgumentParser(description="Execution-delay stress for G3 panic final candidate.")
    parser.add_argument("--input", default="reports/gen3_panic_v2_research/systemic_pause_audit_v1/pause_weak_no_capitulation_trades.csv")
    parser.add_argument("--output-dir", default="reports/gen3_panic_v2_research/final_candidate_execution_stress_v1")
    parser.add_argument("--initial-capital", type=float, default=150000.0)
    parser.add_argument("--cost-bps", type=float, default=30.0)
    args = parser.parse_args()

    source = Path(args.input)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    paths = _prepare_paths(source, args.cost_bps)
    bars = _load_30m(paths)
    bar_map = {code: g.sort_values("datetime").reset_index(drop=True).copy() for code, g in bars.groupby("code")}
    base_candidates = _prepare_policy_candidates(paths, bar_map, POLICY, args.cost_bps)

    rows: list[dict] = []
    for delay_days in DELAY_DAYS:
        candidates = _delay_exit_candidates(base_candidates, delay_days, args.cost_bps)
        tag = f"{POLICY}_delay{delay_days}d"
        candidates.to_csv(out_dir / f"{tag}_policy_candidates.csv", index=False, encoding="utf-8-sig")
        closed, curve = _simulate_slot_mtm(
            candidates=candidates,
            initial_capital=args.initial_capital,
            slots=5,
            slot_pct=0.20,
            mtm_cost_bps=args.cost_bps,
        )
        closed.to_csv(out_dir / f"{tag}_closed_trades.csv", index=False, encoding="utf-8-sig")
        curve.to_csv(out_dir / f"{tag}_mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
        for window in ["train_2020_2023", "valid_2024_2025", "blind_2026ytd", "full"]:
            m = _metrics(closed, curve, window, f"{POLICY}_delay{delay_days}d", args.cost_bps)
            m["execution_delay_days"] = delay_days
            rows.append(m)

    raw = pd.DataFrame(rows)
    raw.to_csv(out_dir / "execution_stress_summary_raw.csv", index=False, encoding="utf-8-sig")
    display = _display(raw)
    display.to_csv(out_dir / "execution_stress_summary_display.csv", index=False, encoding="utf-8-sig")

    full = display[display["window"].eq("full")]
    lines = [
        "# G3 Panic 最终候选 V1 延迟成交压力测试",
        "",
        "## 口径",
        "",
        f"- 输入文件：`{source}`",
        f"- 输出目录：`{out_dir}`",
        f"- 成本：`{args.cost_bps:.1f}bps`",
        "- 候选：`slot5_20pct + 30m 收盘 -5% 下一根开盘全退`。",
        "- 压力：若触发退出后无法立即成交，统一延迟 0/1/2 个交易日到日线收盘价退出。",
        "- 这是保守压力，不等同真实跌停队列，但能观察退出延迟对收益和回撤的破坏程度。",
        "",
        "## full 对照",
        "",
        full[
            [
                "policy",
                "execution_delay_days",
                "closed",
                "triggered",
                "executable",
                "skipped",
                "total_ret",
                "max_drawdown",
                "worst_open_mtm_ret",
                "win_rate",
                "mean_trade_ret",
                "worst_trade",
            ]
        ].to_markdown(index=False),
        "",
        "## 全部分段",
        "",
        display[
            [
                "window",
                "execution_delay_days",
                "closed",
                "triggered",
                "executable",
                "skipped",
                "total_ret",
                "max_drawdown",
                "worst_open_mtm_ret",
                "mean_trade_ret",
                "worst_trade",
            ]
        ].to_markdown(index=False),
        "",
        "## 判断",
        "",
        "1. 如果延迟 1-2 日后回撤和收益明显恶化，说明该退出候选高度依赖执行及时性。",
        "2. 如果延迟后仍优于固定持有的尾部风险，可以继续推进到跌停不可卖的更细压力测试。",
    ]
    (out_dir / "execution_stress_report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "input": str(source),
        "output_dir": str(out_dir),
        "policy": POLICY,
        "delay_days": DELAY_DAYS,
        "cost_bps": args.cost_bps,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
