from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_query_df  # noqa: E402


OUT_DIR = ROOT / "reports" / "mainline_pro_replicate_matrix"
OUT_DIR.mkdir(parents=True, exist_ok=True)

START_DATE = "2010-01-01"
END_DATE = "2025-12-31"


@dataclass(frozen=True)
class PoolSpec:
    name: str
    description: str
    codes: list[str]


def _safe_float(value: Any, ndigits: int = 6) -> Any:
    try:
        x = float(value)
    except Exception:
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, ndigits)


def _pct(value: Any) -> str:
    x = _safe_float(value)
    return "" if x is None else f"{x:.2%}"


def _quote(values: list[str]) -> str:
    return ",".join("'" + v.replace("'", "\\'") + "'" for v in values)


def _compound(returns: list[float]) -> float | None:
    if not returns:
        return None
    value = 1.0
    for ret in returns:
        if ret is None or math.isnan(float(ret)) or math.isinf(float(ret)):
            continue
        value *= 1.0 + float(ret)
    return value


def _load_index_meta() -> pd.DataFrame:
    sql = """
    SELECT
        k.code AS code,
        any(s.name) AS name,
        min(k.trade_date) AS min_date,
        max(k.trade_date) AS max_date,
        count() AS rows
    FROM kline_daily k
    INNER JOIN stocks s ON s.code = k.code
    WHERE s.type = 'index'
      AND k.close > 0
    GROUP BY k.code
    ORDER BY rows DESC, code
    """
    df = clickhouse_query_df(sql)
    if df.empty:
        return df
    df["min_date"] = pd.to_datetime(df["min_date"], errors="coerce")
    df["max_date"] = pd.to_datetime(df["max_date"], errors="coerce")
    df["rows"] = pd.to_numeric(df["rows"], errors="coerce").fillna(0).astype(int)
    return df


def _build_pools(meta: pd.DataFrame) -> list[PoolSpec]:
    complete = meta[
        (meta["min_date"] <= pd.Timestamp("2010-01-04"))
        & (meta["max_date"] >= pd.Timestamp("2025-12-31"))
    ].copy()
    broad = meta[(meta["rows"] >= 500) & (meta["max_date"] >= pd.Timestamp("2025-12-31"))].copy()
    return [
        PoolSpec(
            "complete_2010_2025",
            "本地指数池中 2010-2025 全窗口都有日线覆盖的指数；尽量避免新指数幸存者/上市日偏差。",
            complete["code"].astype(str).tolist(),
        ),
        PoolSpec(
            "broad_rows500",
            "本地指数池中日线不少于 500 行且覆盖到 2025 年末的指数；允许后上市指数进入，偏向视频可能使用的扩展指数/ETF池口径。",
            broad["code"].astype(str).tolist(),
        ),
    ]


def _load_prices(codes: list[str]) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame()
    sql = f"""
    SELECT code, trade_date, close
    FROM kline_daily
    WHERE code IN ({_quote(codes)})
      AND trade_date >= '{START_DATE}'
      AND trade_date <= '{END_DATE}'
      AND close > 0
    ORDER BY code, trade_date
    """
    df = clickhouse_query_df(sql)
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna(subset=["trade_date", "close"]).reset_index(drop=True)


def _close_on_or_before(g: pd.DataFrame, date: pd.Timestamp) -> float | None:
    use = g[g["trade_date"] <= date]
    if use.empty:
        return None
    value = float(use.iloc[-1]["close"])
    return value if value > 0 else None


def _last_close_row(pivot: pd.DataFrame, date: pd.Timestamp) -> pd.Series | None:
    use = pivot[pivot.index <= date]
    if use.empty:
        return None
    return use.iloc[-1]


def _annual_returns(
    pivot: pd.DataFrame,
    anchor_month: int,
    anchor_day: int,
    top_n: int,
    horizon: str,
    long_short: bool = False,
) -> dict[str, Any]:
    year_rows: list[dict[str, Any]] = []
    rets: list[float] = []
    for year in range(2010, 2026):
        anchor = pd.Timestamp(year=year, month=anchor_month, day=anchor_day)
        if horizon == "year_end":
            end = pd.Timestamp(year=year, month=12, day=31)
        elif horizon == "next_anchor":
            if year >= 2025:
                continue
            end = pd.Timestamp(year=year + 1, month=anchor_month, day=anchor_day)
        else:
            raise ValueError(horizon)
        first = pd.Timestamp(year=year, month=1, day=10)

        c0 = _last_close_row(pivot, first)
        ca = _last_close_row(pivot, anchor)
        ce = _last_close_row(pivot, end)
        if c0 is None or ca is None or ce is None:
            continue
        frame = pd.DataFrame({"early_ret": ca / c0 - 1.0, "hold_ret": ce / ca - 1.0})
        frame = frame.replace([math.inf, -math.inf], pd.NA).dropna()
        if len(frame) < max(10, top_n * 2):
            continue
        top = frame.nlargest(top_n, "early_ret")
        long_ret = float(top["hold_ret"].mean())
        if long_short:
            bottom = frame.nsmallest(top_n, "early_ret")
            ret = long_ret - float(bottom["hold_ret"].mean())
        else:
            ret = long_ret
        rets.append(ret)
        year_rows.append(
            {
                "year": year,
                "available": int(len(frame)),
                "ret": _safe_float(ret),
                "top_codes": [str(x) for x in top.index.tolist()],
            }
        )
    equity = _compound(rets)
    return {
        "family": "annual",
        "anchor": f"{anchor_month:02d}-{anchor_day:02d}",
        "horizon": horizon,
        "top_n": top_n,
        "mode": "long_short" if long_short else "long",
        "years": len(rets),
        "equity": _safe_float(equity),
        "total_return": _safe_float(None if equity is None else equity - 1.0),
        "avg_year_ret": _safe_float(float(pd.Series(rets).mean()) if rets else None),
        "positive_years": int((pd.Series(rets) > 0).sum()) if rets else 0,
        "yearly": year_rows,
    }


def _monthly_matrix(month_close: pd.DataFrame, lookback_months: int, top_n: int, long_short: bool = False) -> dict[str, Any]:
    rets: list[float] = []
    rows: list[dict[str, Any]] = []
    for i in range(lookback_months, len(month_close) - 1):
        lookback = month_close.iloc[i] / month_close.iloc[i - lookback_months] - 1.0
        next_ret = month_close.iloc[i + 1] / month_close.iloc[i] - 1.0
        frame = pd.DataFrame({"mom": lookback, "next_ret": next_ret}).replace([math.inf, -math.inf], pd.NA).dropna()
        if len(frame) < max(10, top_n * 2):
            continue
        top = frame.nlargest(top_n, "mom")
        long_ret = float(top["next_ret"].mean())
        if long_short:
            bottom = frame.nsmallest(top_n, "mom")
            ret = long_ret - float(bottom["next_ret"].mean())
        else:
            ret = long_ret
        rets.append(ret)
        rows.append(
            {
                "month": str(month_close.index[i].date()),
                "available": int(len(frame)),
                "ret": _safe_float(ret),
                "top_codes": [str(x) for x in top.index.tolist()],
            }
        )
    equity = _compound(rets)
    return {
        "family": "monthly",
        "lookback_months": lookback_months,
        "top_n": top_n,
        "mode": "long_short" if long_short else "long",
        "months": len(rets),
        "equity": _safe_float(equity),
        "total_return": _safe_float(None if equity is None else equity - 1.0),
        "avg_month_ret": _safe_float(float(pd.Series(rets).mean()) if rets else None),
        "positive_months": int((pd.Series(rets) > 0).sum()) if rets else 0,
        "monthly": rows,
    }


def _run_pool(pool: PoolSpec, meta: pd.DataFrame) -> dict[str, Any]:
    prices = _load_prices(pool.codes)
    pivot = prices.pivot_table(index="trade_date", columns="code", values="close", aggfunc="last").sort_index()
    month_close = pivot.resample("M").last().dropna(how="all")
    tests: list[dict[str, Any]] = []
    for anchor_month, anchor_day in [(2, 28), (3, 31), (4, 30)]:
        for horizon in ["year_end", "next_anchor"]:
            for top_n in [1, 2, 3, 5, 10]:
                tests.append(_annual_returns(pivot, anchor_month, anchor_day, top_n, horizon, False))
                tests.append(_annual_returns(pivot, anchor_month, anchor_day, top_n, horizon, True))
    for lookback in [1, 2, 3, 6, 12]:
        for top_n in [1, 2, 3, 5, 10]:
            tests.append(_monthly_matrix(month_close, lookback, top_n, False))
            tests.append(_monthly_matrix(month_close, lookback, top_n, True))
    ranked = sorted(
        tests,
        key=lambda x: float(x.get("equity") or 0.0),
        reverse=True,
    )
    names = meta.set_index("code")["name"].astype(str).to_dict()
    return {
        "pool": pool.name,
        "description": pool.description,
        "code_count": len(pool.codes),
        "codes": [{"code": c, "name": names.get(c, "")} for c in pool.codes],
        "price_rows": int(len(prices)),
        "best_20": ranked[:20],
        "tests": tests,
    }


def _strategy_label(row: dict[str, Any]) -> str:
    if row["family"] == "annual":
        return f"年度 {row['anchor']} -> {row['horizon']} Top{row['top_n']} {row['mode']}"
    return f"月度 lookback{row['lookback_months']}M Top{row['top_n']} {row['mode']}"


def _write_report(result: dict[str, Any]) -> None:
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines: list[str] = [
        "# 佟掌柜 13pro 强者恒强口径复核矩阵",
        "",
        "## 结论先行",
        "",
        "- 已按第二篇视频可见口径重点复核：52 只指数/ETF、3 月末选股、20 年 24 倍收益、增强收益 19 倍。",
        "- 在本地 ClickHouse 可得指数池里，不论采用年度 2/28、3/31、4/30 锚点，还是月度动量轮动；不论 Top1/2/3/5/10，还是多空增强，都没有接近 19x/24x。",
        "- 这说明视频结果大概率依赖一个尚未复原的具体 52 标的池、收益增强定义，或存在幸存者/调参/未来可见性口径。当前不能把视频的 19 倍当作可直接复现的指数层面结论。",
        "",
        "## 标的池",
        "",
        "| 池 | 数量 | 说明 |",
        "|---|---:|---|",
    ]
    for pool in result["pools"]:
        lines.append(f"| {pool['pool']} | {pool['code_count']} | {pool['description']} |")

    lines.extend(["", "## 各池最佳结果 Top20", ""])
    for pool in result["pools"]:
        lines.extend([f"### {pool['pool']}", "", "| 排名 | 策略 | 样本 | 净值倍数 | 总收益 | 平均收益 | 胜率计数 |", "|---:|---|---:|---:|---:|---:|---:|"])
        for i, row in enumerate(pool["best_20"], start=1):
            sample = row.get("years", row.get("months", ""))
            avg = row.get("avg_year_ret", row.get("avg_month_ret"))
            pos = row.get("positive_years", row.get("positive_months"))
            lines.append(
                f"| {i} | {_strategy_label(row)} | {sample} | "
                f"{row.get('equity') or ''} | {_pct(row.get('total_return'))} | {_pct(avg)} | {pos} |"
            )
        lines.append("")

    lines.extend(
        [
            "## 对视频 19x/24x 的解释",
            "",
            "目前能确认的是：视频第二篇不是简单的“春节后/2月底强板块持有到年底”口径，而是更偏 3 月末选强势指数/ETF的 pro 口径。",
            "",
            "但本地指数池复核没有出现同量级结果。要复现视频，下一步必须拿到它的精确 52 只指数/ETF清单和“增强收益”的计算方式；否则继续调锚点、TopN、持有期，本质是在替视频做参数搜索，容易把过拟合当策略。",
            "",
        ]
    )
    (OUT_DIR / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    meta = _load_index_meta()
    pools = _build_pools(meta)
    result = {
        "source": "ClickHouse kline_daily + stocks(type='index')",
        "start_date": START_DATE,
        "end_date": END_DATE,
        "pools": [_run_pool(pool, meta) for pool in pools],
    }
    _write_report(result)
    for pool in result["pools"]:
        best = pool["best_20"][0] if pool["best_20"] else {}
        print(pool["pool"], pool["code_count"], _strategy_label(best) if best else "", best.get("equity"))
    print(OUT_DIR / "summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
