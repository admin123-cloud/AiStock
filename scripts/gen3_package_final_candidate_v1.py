from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Iterable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_query_df  # noqa: E402


DEFAULT_OUT = ROOT / "reports" / "gen3_final_candidate_package_v1"
BASE_CURVE = ROOT / "reports" / "gen3_combo_panic_strong_execution_stress_v1" / "base_30bps_curve.csv"
BASE_TRADES = ROOT / "reports" / "gen3_combo_panic_strong_execution_stress_v1" / "base_30bps_closed_trades.csv"
STRESS_SUMMARY = ROOT / "reports" / "gen3_combo_panic_strong_execution_stress_v1" / "combo_execution_stress_summary_raw.csv"
STRESS_ANNUAL = ROOT / "reports" / "gen3_combo_panic_strong_execution_stress_v1" / "combo_execution_stress_annual_raw.csv"

BENCHMARKS = {
    "999999.SH": "上证指数",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
}

BENCHMARK_PLOT_LABELS = {
    "999999.SH": "SSE Composite",
    "000300.SH": "CSI 300",
    "000905.SH": "CSI 500",
    "000852.SH": "CSI 1000",
}


def _fmt_pct(x: float | int | None) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return ""
    return f"{x * 100:.2f}%"


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return float("nan")
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(dd.min())


def _metrics(curve: pd.DataFrame, name: str) -> dict:
    d = curve.sort_values("date").copy()
    d["daily_ret"] = d["equity"].pct_change().fillna(0.0)
    total = float(d["equity"].iloc[-1] / d["equity"].iloc[0] - 1.0)
    days = max((d["date"].iloc[-1] - d["date"].iloc[0]).days, 1)
    years = days / 365.25
    ann = (1.0 + total) ** (1.0 / years) - 1.0 if total > -1 else float("nan")
    dd = _max_drawdown(d["equity"])
    vol = float(d["daily_ret"].std(ddof=0) * math.sqrt(252))
    sharpe = float(d["daily_ret"].mean() / d["daily_ret"].std(ddof=0) * math.sqrt(252)) if d["daily_ret"].std(ddof=0) > 0 else float("nan")
    calmar = ann / abs(dd) if dd < 0 else float("nan")
    exposure_days = float((d["open_positions"] > 0).mean()) if "open_positions" in d else float("nan")
    max_pos = float(d["open_positions"].max()) if "open_positions" in d else float("nan")
    avg_pos = float(d["open_positions"].mean()) if "open_positions" in d else float("nan")
    active_avg_pos = float(d.loc[d["open_positions"] > 0, "open_positions"].mean()) if "open_positions" in d and (d["open_positions"] > 0).any() else 0.0
    return {
        "name": name,
        "start": d["date"].iloc[0].date().isoformat(),
        "end": d["date"].iloc[-1].date().isoformat(),
        "trading_days": len(d),
        "calendar_years": years,
        "total_return": total,
        "annual_return": ann,
        "max_drawdown": dd,
        "daily_vol_ann": vol,
        "sharpe_daily": sharpe,
        "calmar": calmar,
        "exposure_day_ratio": exposure_days,
        "avg_open_positions": avg_pos,
        "active_avg_open_positions": active_avg_pos,
        "max_open_positions": max_pos,
        "worst_open_mtm_ret": float(d["worst_open_mtm_ret"].min()) if "worst_open_mtm_ret" in d else float("nan"),
    }


def _window_return(curve: pd.DataFrame, start: str, end: str) -> dict:
    d = curve[(curve["date"] >= pd.Timestamp(start)) & (curve["date"] <= pd.Timestamp(end))].sort_values("date")
    if d.empty:
        return {"start": start, "end": end, "return": float("nan"), "max_drawdown": float("nan"), "days": 0}
    return {
        "start": d["date"].iloc[0].date().isoformat(),
        "end": d["date"].iloc[-1].date().isoformat(),
        "return": float(d["equity"].iloc[-1] / d["equity"].iloc[0] - 1.0),
        "max_drawdown": _max_drawdown(d["equity"]),
        "days": len(d),
    }


def _annual(curve: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for year, g in curve.groupby(curve["date"].dt.year):
        g = g.sort_values("date")
        rows.append(
            {
                "year": int(year),
                "return": float(g["equity"].iloc[-1] / g["equity"].iloc[0] - 1.0),
                "max_drawdown": _max_drawdown(g["equity"]),
                "worst_open_mtm_ret": float(g["worst_open_mtm_ret"].min()) if "worst_open_mtm_ret" in g else float("nan"),
                "avg_open_positions": float(g["open_positions"].mean()) if "open_positions" in g else float("nan"),
                "exposure_day_ratio": float((g["open_positions"] > 0).mean()) if "open_positions" in g else float("nan"),
            }
        )
    return pd.DataFrame(rows)


def _monthly(curve: pd.DataFrame) -> pd.DataFrame:
    d = curve.copy()
    d["month"] = d["date"].dt.to_period("M").astype(str)
    rows = []
    for month, g in d.groupby("month"):
        g = g.sort_values("date")
        rows.append(
            {
                "month": month,
                "return": float(g["equity"].iloc[-1] / g["equity"].iloc[0] - 1.0),
                "max_drawdown": _max_drawdown(g["equity"]),
                "avg_open_positions": float(g["open_positions"].mean()) if "open_positions" in g else float("nan"),
            }
        )
    return pd.DataFrame(rows)


def _load_benchmarks(start: str, end: str, codes: Iterable[str]) -> pd.DataFrame:
    code_list = ", ".join(f"'{c}'" for c in codes)
    sql = f"""
        SELECT code, trade_date, close
        FROM kline_daily
        WHERE code IN ({code_list})
          AND trade_date >= toDate('{start}')
          AND trade_date <= toDate('{end}')
        ORDER BY code, trade_date
    """
    df = clickhouse_query_df(sql)
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])
    return df


def _benchmark_summary(curve: pd.DataFrame, bench: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = []
    bench_curves = []
    strategy_total = float(curve["equity"].iloc[-1] / curve["equity"].iloc[0] - 1.0)
    for code, name in BENCHMARKS.items():
        b = bench[bench["code"] == code].sort_values("trade_date")
        if b.empty:
            rows.append({"code": code, "name": name, "rows": 0, "start": "", "end": "", "return": float("nan"), "max_drawdown": float("nan"), "strategy_excess": float("nan")})
            continue
        b = b.copy()
        b["equity"] = b["close"] / b["close"].iloc[0]
        ret = float(b["equity"].iloc[-1] - 1.0)
        rows.append(
            {
                "code": code,
                "name": name,
                "rows": len(b),
                "start": b["trade_date"].iloc[0].date().isoformat(),
                "end": b["trade_date"].iloc[-1].date().isoformat(),
                "return": ret,
                "max_drawdown": _max_drawdown(b["equity"]),
                "strategy_excess": strategy_total - ret,
            }
        )
        bench_curves.append(b.rename(columns={"trade_date": "date"})[["date", "code", "equity"]])
    curves = pd.concat(bench_curves, ignore_index=True) if bench_curves else pd.DataFrame()
    return pd.DataFrame(rows), curves


def _window_benchmark_excess(curve: pd.DataFrame, bench: pd.DataFrame) -> pd.DataFrame:
    windows = {
        "train_2020_2023": ("2020-01-03", "2023-12-29"),
        "valid_2024_2025": ("2024-01-02", "2025-12-31"),
        "blind_2026ytd": ("2026-01-01", "2026-05-27"),
        "old_window_2020_2024": ("2020-01-03", "2024-12-31"),
        "recent_2025_2026": ("2025-01-01", "2026-05-27"),
    }
    rows = []
    for w, (start, end) in windows.items():
        s = _window_return(curve, start, end)
        for code, name in BENCHMARKS.items():
            b = bench[(bench["code"] == code) & (bench["trade_date"] >= pd.Timestamp(start)) & (bench["trade_date"] <= pd.Timestamp(end))].sort_values("trade_date")
            if b.empty:
                bret = float("nan")
                bdd = float("nan")
                bstart = ""
                bend = ""
            else:
                eq = b["close"] / b["close"].iloc[0]
                bret = float(eq.iloc[-1] - 1.0)
                bdd = _max_drawdown(eq)
                bstart = b["trade_date"].iloc[0].date().isoformat()
                bend = b["trade_date"].iloc[-1].date().isoformat()
            rows.append(
                {
                    "window": w,
                    "strategy_start": s["start"],
                    "strategy_end": s["end"],
                    "benchmark_start": bstart,
                    "benchmark_end": bend,
                    "benchmark_code": code,
                    "benchmark_name": name,
                    "strategy_return": s["return"],
                    "benchmark_return": bret,
                    "strategy_excess": s["return"] - bret if not math.isnan(bret) and not math.isnan(s["return"]) else float("nan"),
                    "strategy_drawdown": s["max_drawdown"],
                    "benchmark_drawdown": bdd,
                }
            )
    return pd.DataFrame(rows)


def _plot(curve: pd.DataFrame, bench_curves: pd.DataFrame, out: Path) -> None:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(curve["date"], curve["equity"], label="G3 Panic+Strong base_30bps", linewidth=2.2)
    if not bench_curves.empty:
        for code, g in bench_curves.groupby("code"):
            ax.plot(g["date"], g["equity"], label=f"{BENCHMARK_PLOT_LABELS.get(code, code)}", linewidth=1.1, alpha=0.85)
    ax.set_title("G3 Final Candidate V1 Equity vs Benchmarks")
    ax.set_xlabel("Date")
    ax.set_ylabel("Equity")
    ax.grid(True, alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "g3_final_candidate_equity_vs_benchmark.png", dpi=160)
    plt.close(fig)


def _md_table(df: pd.DataFrame, columns: list[str]) -> str:
    if df.empty:
        return ""
    return df[columns].to_markdown(index=False)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    curve = pd.read_csv(BASE_CURVE)
    curve["date"] = pd.to_datetime(curve["date"])
    curve = curve.sort_values("date")

    trades = pd.read_csv(BASE_TRADES, low_memory=False)
    stress_summary = pd.read_csv(STRESS_SUMMARY)
    stress_annual = pd.read_csv(STRESS_ANNUAL)

    start = curve["date"].iloc[0].date().isoformat()
    end = curve["date"].iloc[-1].date().isoformat()
    bench = _load_benchmarks(start, end, BENCHMARKS.keys())

    metrics = pd.DataFrame([_metrics(curve, "g3_final_panic_strong_base_30bps")])
    annual = _annual(curve)
    monthly = _monthly(curve)
    bench_summary, bench_curves = _benchmark_summary(curve, bench)
    window_excess = _window_benchmark_excess(curve, bench)

    chain_rows = []
    if "g3_chain" in trades.columns:
        for chain, g in trades.groupby("g3_chain"):
            chain_rows.append(
                {
                    "chain": chain,
                    "closed": len(g),
                    "win_rate": float((pd.to_numeric(g["net_ret"], errors="coerce") > 0).mean()) if "net_ret" in g else float("nan"),
                    "mean_net_ret": float(pd.to_numeric(g["net_ret"], errors="coerce").mean()) if "net_ret" in g else float("nan"),
                    "median_net_ret": float(pd.to_numeric(g["net_ret"], errors="coerce").median()) if "net_ret" in g else float("nan"),
                    "worst_net_ret": float(pd.to_numeric(g["net_ret"], errors="coerce").min()) if "net_ret" in g else float("nan"),
                }
            )
    chain_summary = pd.DataFrame(chain_rows)

    metrics.to_csv(out / "final_candidate_metrics.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(out / "final_candidate_annual.csv", index=False, encoding="utf-8-sig")
    monthly.to_csv(out / "final_candidate_monthly.csv", index=False, encoding="utf-8-sig")
    bench_summary.to_csv(out / "benchmark_summary.csv", index=False, encoding="utf-8-sig")
    window_excess.to_csv(out / "benchmark_window_excess.csv", index=False, encoding="utf-8-sig")
    chain_summary.to_csv(out / "chain_trade_summary.csv", index=False, encoding="utf-8-sig")
    curve.to_csv(out / "final_candidate_equity_curve.csv", index=False, encoding="utf-8-sig")
    _plot(curve, bench_curves, out)

    m = metrics.iloc[0].to_dict()
    stress_keep = stress_summary[
        stress_summary["profile"].isin(
            [
                "base_30bps",
                "slippage_50bps",
                "slippage_100bps",
                "panic_delay2_strong_limitdown30",
            ]
        )
    ].copy()
    for col in ["total_ret", "max_drawdown", "worst_open_mtm_ret", "win_rate", "mean_trade_ret", "worst_trade", "bad10_rate"]:
        if col in stress_keep:
            stress_keep[col] = pd.to_numeric(stress_keep[col], errors="coerce").map(_fmt_pct)

    annual_show = annual.copy()
    for col in ["return", "max_drawdown", "worst_open_mtm_ret", "exposure_day_ratio"]:
        annual_show[col] = annual_show[col].map(_fmt_pct)
    annual_show["avg_open_positions"] = annual_show["avg_open_positions"].map(lambda x: f"{x:.2f}")

    bench_show = bench_summary.copy()
    for col in ["return", "max_drawdown", "strategy_excess"]:
        bench_show[col] = bench_show[col].map(_fmt_pct)

    window_show = window_excess.copy()
    for col in ["strategy_return", "benchmark_return", "strategy_excess", "strategy_drawdown", "benchmark_drawdown"]:
        window_show[col] = window_show[col].map(_fmt_pct)

    chain_show = chain_summary.copy()
    if not chain_show.empty:
        for col in ["win_rate", "mean_net_ret", "median_net_ret", "worst_net_ret"]:
            chain_show[col] = chain_show[col].map(_fmt_pct)

    report = f"""# G3 正式候选包 V1：Panic + Strong

生成日期：2026-06-01

## 版本定义

- 候选版本：`g3_final_panic_strong_base_30bps`
- 组合结构：`Panic final 50% + Strong volume5 50%`
- 核心曲线：`base_30bps`
- 区间：`{m['start']}` 至 `{m['end']}`
- 输入：只读取既有 G3 研究产物和 ClickHouse 指数日线；未触发 G2 重建，未改动 G2 运行链路。

## 总体指标

| 指标 | 数值 |
|---|---:|
| 总收益 | {_fmt_pct(m['total_return'])} |
| 年化收益 | {_fmt_pct(m['annual_return'])} |
| 最大回撤 | {_fmt_pct(m['max_drawdown'])} |
| 日频年化波动 | {_fmt_pct(m['daily_vol_ann'])} |
| 日频 Sharpe | {m['sharpe_daily']:.2f} |
| Calmar | {m['calmar']:.2f} |
| 交易日数量 | {int(m['trading_days'])} |
| 日历年数 | {m['calendar_years']:.2f} |
| 有持仓交易日占比 | {_fmt_pct(m['exposure_day_ratio'])} |
| 平均打开持仓数 | {m['avg_open_positions']:.2f} |
| 有持仓日平均持仓数 | {m['active_avg_open_positions']:.2f} |
| 最大打开持仓数 | {m['max_open_positions']:.0f} |
| 最差开仓 MTM | {_fmt_pct(m['worst_open_mtm_ret'])} |

## 年度结果

{_md_table(annual_show, ['year', 'return', 'max_drawdown', 'worst_open_mtm_ret', 'avg_open_positions', 'exposure_day_ratio'])}

## 基准对比

{_md_table(bench_show, ['code', 'name', 'rows', 'start', 'end', 'return', 'max_drawdown', 'strategy_excess'])}

## 分段超额

{_md_table(window_show, ['window', 'benchmark_name', 'strategy_return', 'benchmark_return', 'strategy_excess', 'strategy_drawdown', 'benchmark_drawdown'])}

## 链路交易质量

{_md_table(chain_show, ['chain', 'closed', 'win_rate', 'mean_net_ret', 'median_net_ret', 'worst_net_ret']) if not chain_show.empty else '无链路字段。'}

## 执行压力摘要

{_md_table(stress_keep, ['profile', 'start', 'end', 'total_ret', 'max_drawdown', 'worst_open_mtm_ret', 'closed', 'win_rate', 'mean_trade_ret', 'worst_trade', 'bad10_rate'])}

## 关键判断

1. 这版正式候选比单独看 2025/2026 更健康，但收益仍明显受强势年份驱动。
2. 2023 和 2024 仍是薄弱年份，说明 `range/weak` 新源重建仍有必要。
3. 100bps 压力下仍为正收益，但收益降到低位，后续不能忽略真实成交摩擦。
4. 有持仓交易日占比和平均持仓数已经单独列出，后续比较新版本时必须同时看收益和资金暴露，不能只看总收益。
5. 基准对比使用 ClickHouse `kline_daily` 指数日线；若某指数覆盖缺口，表中会保留实际 rows、start、end。

## 文件清单

- `final_candidate_equity_curve.csv`：正式候选逐日权益曲线。
- `final_candidate_metrics.csv`：总体指标。
- `final_candidate_annual.csv`：年度指标。
- `final_candidate_monthly.csv`：月度指标。
- `benchmark_summary.csv`：全区间基准对比。
- `benchmark_window_excess.csv`：分段基准超额。
- `chain_trade_summary.csv`：panic/strong 链路交易质量。
- `g3_final_candidate_equity_vs_benchmark.png`：收益曲线与基准图。

## 下一步

下一步应做未来函数和实盘可见性审计：逐项确认 `panic` 的 30m 恐慌确认、`strong` 的 volume5/二次确认、卖出触发、跌停延迟和次日开盘成交是否都能在真实时间点获得。
"""
    (out / "g3_final_candidate_package_report_cn.md").write_text(report, encoding="utf-8", newline="\n")
    print(f"[OK] wrote package to {out}")


if __name__ == "__main__":
    main()
