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

from scripts.probe_mainline_sector_strength_tdxquant import _fetch_histories, _fetch_sector_list  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


OUT_DIR = ROOT / "reports" / "mainline_sector_index_excess_backtest"
OUT_DIR.mkdir(parents=True, exist_ok=True)

BENCHMARKS = {
    "999999.SH": "上证指数",
    "000300.SH": "沪深300",
    "000852.SH": "中证1000",
}


def _safe_float(value: Any) -> Any:
    try:
        x = float(value)
    except Exception:
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, 6)


def _pct(value: Any) -> str:
    x = _safe_float(value)
    return "" if x is None else f"{x:.2%}"


def _quote_list(values: list[str]) -> str:
    return ",".join("'" + v.replace("'", "\\'") + "'" for v in values)


def _load_benchmarks(start_date: str, end_date: str) -> pd.DataFrame:
    codes = list(BENCHMARKS)
    sql = f"""
    SELECT code, trade_date, close
    FROM kline_daily
    WHERE code IN ({_quote_list(codes)})
      AND trade_date >= '{start_date}'
      AND trade_date <= '{end_date}'
    """
    df = clickhouse_query_df(sql)
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna(subset=["trade_date", "close"]).sort_values(["code", "trade_date"]).reset_index(drop=True)


def _close_on_or_before(df: pd.DataFrame, code: str, date: pd.Timestamp) -> float | None:
    g = df[(df["code"] == code) & (df["trade_date"] <= date)].sort_values("trade_date")
    if g.empty:
        return None
    value = g.iloc[-1]["close"]
    if pd.isna(value) or float(value) <= 0:
        return None
    return float(value)


def _sector_close_map(ydf: pd.DataFrame, date: pd.Timestamp) -> dict[str, float]:
    out: dict[str, float] = {}
    for code, g in ydf[ydf["date"] <= date].groupby("code"):
        use = g.sort_values("date")
        if use.empty:
            continue
        value = use.iloc[-1]["close"]
        if not pd.isna(value) and float(value) > 0:
            out[str(code)] = float(value)
    return out


def _year_backtest(
    hist: pd.DataFrame,
    bench: pd.DataFrame,
    year: int,
    level: int,
    top_pct: float,
) -> dict[str, Any] | None:
    ydf = hist[(hist["level"] == level) & (hist["date"].dt.year == year)].copy()
    if ydf.empty:
        return None
    dates = sorted(pd.Timestamp(d) for d in ydf["date"].dropna().unique())
    if not dates:
        return None
    first = dates[0]
    anchors = [d for d in dates if d <= pd.Timestamp(f"{year}-02-28")]
    if not anchors:
        return None
    anchor = anchors[-1]
    anchor_pos = max(i for i, d in enumerate(dates) if d <= anchor)
    h120 = dates[min(anchor_pos + 120, len(dates) - 1)]
    year_end = dates[-1]

    first_close = _sector_close_map(ydf, first)
    anchor_close = _sector_close_map(ydf, anchor)
    h120_close = _sector_close_map(ydf, h120)
    end_close = _sector_close_map(ydf, year_end)

    rows: list[dict[str, Any]] = []
    names = ydf.groupby("code")["name"].last().to_dict()
    for code, ac in anchor_close.items():
        fc = first_close.get(code)
        if not fc or fc <= 0:
            continue
        item = {
            "code": code,
            "name": str(names.get(code) or ""),
            "early_ret": ac / fc - 1.0,
        }
        for label, cmap in [("h120", h120_close), ("year_end", end_close)]:
            hc = cmap.get(code)
            item[f"ret_{label}"] = None if not hc or ac <= 0 else hc / ac - 1.0
        rows.append(item)
    sectors = pd.DataFrame(rows)
    if len(sectors) < 10:
        return None
    sectors["early_rank"] = sectors["early_ret"].rank(ascending=False, method="first")
    top_n = max(5, min(15, int(round(len(sectors) * top_pct))))
    top = sectors.nsmallest(top_n, "early_rank").copy()

    result: dict[str, Any] = {
        "year": year,
        "level": level,
        "sector_count": int(len(sectors)),
        "top_n": int(top_n),
        "first_date": str(first.date()),
        "anchor_date": str(anchor.date()),
        "h120_date": str(h120.date()),
        "year_end_date": str(year_end.date()),
        "top_sectors": [
            {
                "code": str(r.code),
                "name": str(r.name),
                "early_ret": _safe_float(r.early_ret),
                "ret_h120": _safe_float(r.ret_h120),
                "ret_year_end": _safe_float(r.ret_year_end),
            }
            for r in top.sort_values("early_rank").itertuples(index=False)
        ],
    }
    for horizon, end_date in [("h120", h120), ("year_end", year_end)]:
        col = f"ret_{horizon}"
        top_vals = pd.to_numeric(top[col], errors="coerce").dropna()
        all_vals = pd.to_numeric(sectors[col], errors="coerce").dropna()
        if top_vals.empty or all_vals.empty:
            continue
        top_ret = float(top_vals.mean())
        all_ret = float(all_vals.mean())
        result[f"top_ret_{horizon}"] = _safe_float(top_ret)
        result[f"all_sector_ret_{horizon}"] = _safe_float(all_ret)
        result[f"excess_vs_all_sector_{horizon}"] = _safe_float(top_ret - all_ret)
        for bcode, bname in BENCHMARKS.items():
            b0 = _close_on_or_before(bench, bcode, anchor)
            b1 = _close_on_or_before(bench, bcode, end_date)
            bret = None if not b0 or not b1 else b1 / b0 - 1.0
            result[f"benchmark_{bcode}_{horizon}"] = _safe_float(bret)
            result[f"excess_vs_{bcode}_{horizon}"] = None if bret is None else _safe_float(top_ret - bret)
    return result


def _summary(rows: list[dict[str, Any]], level: int, horizon: str) -> dict[str, Any]:
    use = [r for r in rows if r["level"] == level and r["year"] < 2026 and f"top_ret_{horizon}" in r]
    out: dict[str, Any] = {
        "level": level,
        "horizon": horizon,
        "years": [r["year"] for r in use],
        "tested_years": len(use),
    }
    if not use:
        return out
    fields = [f"top_ret_{horizon}", f"all_sector_ret_{horizon}", f"excess_vs_all_sector_{horizon}"]
    fields += [f"benchmark_{b}_{horizon}" for b in BENCHMARKS]
    fields += [f"excess_vs_{b}_{horizon}" for b in BENCHMARKS]
    for field in fields:
        vals = pd.Series([r.get(field) for r in use], dtype="float64").dropna()
        out[f"avg_{field}"] = _safe_float(vals.mean()) if not vals.empty else None
        if field.startswith("excess_"):
            out[f"positive_{field}_years"] = int((vals > 0).sum()) if not vals.empty else 0
    out[f"compound_top_ret_{horizon}"] = _safe_float(math.prod(1 + float(r[f"top_ret_{horizon}"]) for r in use) - 1)
    for bcode in BENCHMARKS:
        bvals = [r.get(f"benchmark_{bcode}_{horizon}") for r in use]
        if all(v is not None for v in bvals):
            out[f"compound_benchmark_{bcode}_{horizon}"] = _safe_float(math.prod(1 + float(v) for v in bvals) - 1)
            out[f"compound_excess_vs_{bcode}_{horizon}"] = _safe_float(
                out[f"compound_top_ret_{horizon}"] - out[f"compound_benchmark_{bcode}_{horizon}"]
            )
    return out


def _write_report(result: dict[str, Any]) -> None:
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 主线板块指数超额收益回测",
        "",
        "## 方法",
        "",
        "- 数据源：TDXQuant 板块指数日线；宽基指数来自 ClickHouse `kline_daily`。",
        "- 规则：每年首个交易日至 2 月最后交易日计算板块指数早期涨幅，选 Top10% 强板块指数等权；Top 数量下限 5、上限 15。",
        "- 买入：2 月最后交易日收盘后假设持有板块指数组合；评估 `h120` 和年末。",
        "- 对比：全体同层级板块均值、上证指数、沪深300、中证1000。",
        "- 2026 仅截至 2026-06-02，列分年结果，但不纳入汇总均值。",
        "",
        "## 汇总",
        "",
        "| 层级 | 周期 | 年份 | Top组合 | 全体板块 | 超额全体 | 超额上证 | 超额沪深300 | 超额中证1000 | 复合Top | 复合超额中证1000 |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in result["summary"]:
        horizon = r["horizon"]
        lines.append(
            f"| L{r['level']} | {horizon} | {','.join(map(str, r['years']))} | "
            f"{_pct(r.get(f'avg_top_ret_{horizon}'))} | {_pct(r.get(f'avg_all_sector_ret_{horizon}'))} | "
            f"{_pct(r.get(f'avg_excess_vs_all_sector_{horizon}'))} | "
            f"{_pct(r.get(f'avg_excess_vs_999999.SH_{horizon}'))} | "
            f"{_pct(r.get(f'avg_excess_vs_000300.SH_{horizon}'))} | "
            f"{_pct(r.get(f'avg_excess_vs_000852.SH_{horizon}'))} | "
            f"{_pct(r.get(f'compound_top_ret_{horizon}'))} | "
            f"{_pct(r.get(f'compound_excess_vs_000852.SH_{horizon}'))} |"
        )
    lines.extend(["", "## 分年结果", ""])
    lines.append("| 年份 | 层级 | 周期 | Top组合 | 全体板块 | 超额全体 | 上证 | 超额上证 | 沪深300 | 超额沪深300 | 中证1000 | 超额中证1000 | Top板块 |")
    lines.append("|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for r in result["yearly"]:
        top_names = "、".join(x["name"] for x in r["top_sectors"][:5])
        for horizon in ["h120", "year_end"]:
            if f"top_ret_{horizon}" not in r:
                continue
            lines.append(
                f"| {r['year']} | L{r['level']} | {horizon} | {_pct(r.get(f'top_ret_{horizon}'))} | "
                f"{_pct(r.get(f'all_sector_ret_{horizon}'))} | {_pct(r.get(f'excess_vs_all_sector_{horizon}'))} | "
                f"{_pct(r.get(f'benchmark_999999.SH_{horizon}'))} | {_pct(r.get(f'excess_vs_999999.SH_{horizon}'))} | "
                f"{_pct(r.get(f'benchmark_000300.SH_{horizon}'))} | {_pct(r.get(f'excess_vs_000300.SH_{horizon}'))} | "
                f"{_pct(r.get(f'benchmark_000852.SH_{horizon}'))} | {_pct(r.get(f'excess_vs_000852.SH_{horizon}'))} | "
                f"{top_names} |"
            )
    (OUT_DIR / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    sectors = _fetch_sector_list()
    histories, failures = _fetch_histories(sectors, "2020-01-01", "2026-06-02")
    bench = _load_benchmarks("2020-01-01", "2026-06-02")
    yearly: list[dict[str, Any]] = []
    for level in [1, 2, 3]:
        for year in range(2020, 2027):
            item = _year_backtest(histories, bench, year, level, top_pct=0.10)
            if item:
                yearly.append(item)
    summary = [_summary(yearly, level, horizon) for level in [1, 2, 3] for horizon in ["h120", "year_end"]]
    result = {
        "method": "year_start_to_feb_end_top_sector_index_equal_weight_excess",
        "sector_counts": sectors.groupby("level")["code"].nunique().to_dict() if not sectors.empty else {},
        "history_rows": int(len(histories)),
        "benchmark_rows": int(len(bench)),
        "failures": failures,
        "summary": summary,
        "yearly": yearly,
    }
    _write_report(result)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(str(OUT_DIR / "summary.md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
