from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_mainline_pro_etf_vs_index import (  # noqa: E402
    _load_csindex_prices,
    _load_local_index_prices,
    _load_true_etf_prices,
    _safe_float,
)


OUT_DIR = _report_path() / "mainline_rotation_after_pullback"
START = "2007-01-01"
END = "2026-06-02"


@dataclass(frozen=True)
class Rule:
    name: str
    rank_months: int
    pullback: float
    rebound: float
    min_wait_days: int
    max_wait_days: int
    top_n: int = 3


RULES = [
    Rule("rank1m_pb8_reb3_wait5_20", rank_months=1, pullback=0.08, rebound=0.03, min_wait_days=5, max_wait_days=20),
    Rule("rank2m_pb8_reb3_wait5_20", rank_months=2, pullback=0.08, rebound=0.03, min_wait_days=5, max_wait_days=20),
    Rule("rank3m_pb8_reb3_wait5_20", rank_months=3, pullback=0.08, rebound=0.03, min_wait_days=5, max_wait_days=20),
    Rule("rank1m_pb12_reb3_wait5_20", rank_months=1, pullback=0.12, rebound=0.03, min_wait_days=5, max_wait_days=20),
    Rule("rank2m_pb12_reb3_wait5_20", rank_months=2, pullback=0.12, rebound=0.03, min_wait_days=5, max_wait_days=20),
    Rule("rank3m_pb12_reb3_wait5_20", rank_months=3, pullback=0.12, rebound=0.03, min_wait_days=5, max_wait_days=20),
]


def _pct(value: Any) -> str:
    x = _safe_float(value)
    return "" if x is None else f"{x:.2%}"


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        item: dict[str, Any] = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = _pct(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else value
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def _close_on_or_before(pivot: pd.DataFrame, date: pd.Timestamp) -> pd.Series | None:
    use = pivot[pivot.index <= date]
    if use.empty:
        return None
    return use.iloc[-1]


def _select_top(pivot: pd.DataFrame, names: dict[str, str], date: pd.Timestamp, rank_months: int, top_n: int) -> list[str]:
    now = _close_on_or_before(pivot, date)
    start = _close_on_or_before(pivot, date - pd.DateOffset(months=rank_months))
    if now is None or start is None:
        return []
    frame = pd.DataFrame({"rank_ret": now / start - 1.0}).replace([math.inf, -math.inf], pd.NA).dropna()
    frame = frame[frame["rank_ret"].notna()]
    if len(frame) < top_n:
        return []
    top = frame.nlargest(top_n, "rank_ret")
    return [str(code) for code in top.index if str(code) in names]


def _basket_close(row: pd.Series, codes: list[str]) -> float | None:
    vals = pd.to_numeric(row.reindex(codes), errors="coerce").dropna()
    if vals.empty:
        return None
    return float(vals.mean())


def _simulate_one_year(
    pivot: pd.DataFrame,
    names: dict[str, str],
    year: int,
    rule: Rule,
) -> dict[str, Any] | None:
    anchor = pd.Timestamp(year=year, month=3, day=31)
    start_idx = pivot.index.searchsorted(anchor, side="left")
    if start_idx >= len(pivot):
        return None
    year_end = pd.Timestamp(year=year, month=12, day=31)
    end_idx = pivot.index.searchsorted(year_end, side="right") - 1
    if end_idx <= start_idx:
        return None

    current = _select_top(pivot, names, pivot.index[start_idx], rule.rank_months, rule.top_n)
    if len(current) < rule.top_n:
        return None

    cash = 1.0
    segments: list[dict[str, Any]] = []
    i = start_idx
    segment_start_idx = i
    entry_value = _basket_close(pivot.iloc[i], current)
    if entry_value is None:
        return None
    high_value = entry_value
    in_wait = False
    wait_start_idx = -1
    wait_low = entry_value

    while i <= end_idx:
        row = pivot.iloc[i]
        value = _basket_close(row, current)
        if value is None:
            i += 1
            continue

        if not in_wait:
            high_value = max(high_value, value)
            if value / high_value - 1.0 <= -rule.pullback:
                seg_ret = value / entry_value - 1.0
                cash *= 1.0 + seg_ret
                segments.append(
                    {
                        "start": str(pivot.index[segment_start_idx].date()),
                        "end": str(pivot.index[i].date()),
                        "codes": current,
                        "names": [names.get(c, "") for c in current],
                        "return": _safe_float(seg_ret),
                        "exit": "pullback",
                    }
                )
                in_wait = True
                wait_start_idx = i
                wait_low = value
        else:
            wait_low = min(wait_low, value)
            waited = i - wait_start_idx
            recovered = value / wait_low - 1.0 >= rule.rebound
            timed = waited >= rule.max_wait_days
            if waited >= rule.min_wait_days and (recovered or timed):
                new_top = _select_top(pivot, names, pivot.index[i], rule.rank_months, rule.top_n)
                if len(new_top) >= rule.top_n:
                    current = new_top
                    segment_start_idx = i
                    entry_value = _basket_close(pivot.iloc[i], current)
                    if entry_value is None:
                        return None
                    high_value = entry_value
                    in_wait = False
        i += 1

    if not in_wait:
        final_value = _basket_close(pivot.iloc[end_idx], current)
        if final_value is None:
            return None
        seg_ret = final_value / entry_value - 1.0
        cash *= 1.0 + seg_ret
        segments.append(
            {
                "start": str(pivot.index[segment_start_idx].date()),
                "end": str(pivot.index[end_idx].date()),
                "codes": current,
                "names": [names.get(c, "") for c in current],
                "return": _safe_float(seg_ret),
                "exit": "year_end",
            }
        )
    return {
        "year": year,
        "ret": _safe_float(cash - 1.0),
        "segments": segments,
        "rotations": max(0, len(segments) - 1),
        "initial": segments[0]["names"] if segments else [],
        "final": segments[-1]["names"] if segments else [],
    }


def _simulate(prices: pd.DataFrame, pool: str, rule: Rule) -> dict[str, Any]:
    if prices.empty:
        return {"pool": pool, "rule": rule.name, "error": "empty"}
    px = prices.copy()
    px["date"] = pd.to_datetime(px["date"], errors="coerce")
    px["close"] = pd.to_numeric(px["close"], errors="coerce")
    px = px.dropna(subset=["date", "code", "close"]).query("close > 0").copy()
    px = px[(px["date"] >= START) & (px["date"] <= END)].copy()
    pivot = px.pivot_table(index="date", columns="code", values="close", aggfunc="last").sort_index()
    names = px.groupby("code")["name"].last().astype(str).to_dict()
    yearly: list[dict[str, Any]] = []
    for year in range(2007, 2026):
        item = _simulate_one_year(pivot, names, year, rule)
        if item is not None:
            yearly.append(item)
    rets = [float(x["ret"]) for x in yearly if x.get("ret") is not None]
    equity = math.prod(1.0 + r for r in rets) if rets else None
    return {
        "pool": pool,
        "rule": rule.name,
        "rank_months": rule.rank_months,
        "pullback": rule.pullback,
        "rebound": rule.rebound,
        "years": len(rets),
        "equity": _safe_float(equity),
        "total_return": _safe_float(None if equity is None else equity - 1.0),
        "avg_year_ret": _safe_float(pd.Series(rets).mean() if rets else None),
        "median_year_ret": _safe_float(pd.Series(rets).median() if rets else None),
        "max_year_loss": _safe_float(pd.Series(rets).min() if rets else None),
        "positive_years": int((pd.Series(rets) > 0).sum()) if rets else 0,
        "avg_rotations": _safe_float(pd.Series([x["rotations"] for x in yearly]).mean() if yearly else None),
        "yearly": yearly,
    }


def _baseline_next_anchor(prices: pd.DataFrame, pool: str, rank_months: int = 2) -> dict[str, Any]:
    if prices.empty:
        return {"pool": pool, "rule": f"baseline_rank{rank_months}m_no_rotation", "error": "empty"}
    px = prices.copy()
    px["date"] = pd.to_datetime(px["date"], errors="coerce")
    px["close"] = pd.to_numeric(px["close"], errors="coerce")
    px = px.dropna(subset=["date", "code", "close"]).query("close > 0").copy()
    pivot = px.pivot_table(index="date", columns="code", values="close", aggfunc="last").sort_index()
    names = px.groupby("code")["name"].last().astype(str).to_dict()
    yearly: list[dict[str, Any]] = []
    for year in range(2007, 2026):
        anchor = pd.Timestamp(year=year, month=3, day=31)
        end = pd.Timestamp(year=year + 1, month=3, day=31)
        ca = _close_on_or_before(pivot, anchor)
        ce = _close_on_or_before(pivot, end)
        c0 = _close_on_or_before(pivot, anchor - pd.DateOffset(months=rank_months))
        if ca is None or ce is None or c0 is None:
            continue
        frame = pd.DataFrame({"rank_ret": ca / c0 - 1.0, "hold": ce / ca - 1.0}).replace(
            [math.inf, -math.inf], pd.NA
        ).dropna()
        if len(frame) < 3:
            continue
        top = frame.nlargest(3, "rank_ret")
        ret = float(top["hold"].mean())
        yearly.append(
            {
                "year": year,
                "ret": _safe_float(ret),
                "initial": [names.get(str(code), "") for code in top.index],
            }
        )
    rets = [float(x["ret"]) for x in yearly]
    equity = math.prod(1.0 + r for r in rets) if rets else None
    return {
        "pool": pool,
        "rule": f"baseline_rank{rank_months}m_no_rotation_next_anchor",
        "rank_months": rank_months,
        "years": len(rets),
        "equity": _safe_float(equity),
        "total_return": _safe_float(None if equity is None else equity - 1.0),
        "avg_year_ret": _safe_float(pd.Series(rets).mean() if rets else None),
        "median_year_ret": _safe_float(pd.Series(rets).median() if rets else None),
        "max_year_loss": _safe_float(pd.Series(rets).min() if rets else None),
        "positive_years": int((pd.Series(rets) > 0).sum()) if rets else 0,
        "avg_rotations": 0.0,
        "yearly": yearly,
    }


def _write_report(rows: list[dict[str, Any]], detail: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary = pd.DataFrame([{k: v for k, v in r.items() if k != "yearly"} for r in rows])
    summary.to_csv(OUT_DIR / "rotation_summary.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "rotation_summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "rotation_detail.json").write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")

    display = summary.sort_values(["pool", "equity"], ascending=[True, False]).copy()
    top = display.groupby("pool", group_keys=False).head(8)
    pct_cols = {"total_return", "avg_year_ret", "median_year_ret", "max_year_loss", "pullback", "rebound"}
    lines = [
        "# 3月底主线板块 + 调整后再选强势板块回测",
        "",
        "## 规则口径",
        "",
        "- 初始选择：每年 3 月 31 日，用此前 1/2/3 个月涨幅排名，选 Top3 板块/ETF/指数等权。",
        "- 调整识别：持有组合从阶段高点回撤达到 8% 或 12%，视为一轮调整开始，先退出该段持有。",
        "- 再入场：调整后至少等待 5 个交易日；若组合从等待低点反弹 3%，或等待满 20 个交易日，则按最近 1/2/3 个月强度重新选 Top3。",
        "- 年度终止：每年 12 月 31 日结束，不跨年持仓；另列 `no_rotation_next_anchor` 作为 3 月底选后持有到下一年 3 月底的基准。",
        "- 这是板块/ETF 层测试，不是个股级 G2/G3 撮合；未加入滑点、冲击成本、ETF 买卖价差和停牌流动性问题。",
        "",
        "## 每个池的较优规则",
        "",
        _md_table(top, pct_cols=pct_cols),
        "",
        "## 初步结论",
        "",
        "- “3月底选强势板块”本身有动量信号；但加入“回撤后退出、再选下一轮强势”的状态机，并没有稳定碾压简单持有到下一锚点。",
        "- 8% 回撤规则通常切换更频繁，能减少部分年度大亏，但也容易在强趋势中被震出；12% 回撤更接近趋势持有。",
        "- 这个逻辑更适合作为 G2/G3 的强势环境/主线候选池刷新机制，而不是单独做 ETF 年度轮动策略。",
        "",
    ]
    (OUT_DIR / "rotation_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def run() -> dict[str, Any]:
    true_etf, true_meta = _load_true_etf_prices()
    local_idx, local_meta = _load_local_index_prices()
    cs_idx, cs_meta = _load_csindex_prices(52)
    pools = {
        "true_etf_listed_only": true_etf,
        "local_theme_index": local_idx,
        "csindex_backfilled_theme_index": cs_idx,
    }
    rows: list[dict[str, Any]] = []
    for pool, prices in pools.items():
        for rank_months in [1, 2, 3]:
            rows.append(_baseline_next_anchor(prices, pool, rank_months))
        for rule in RULES:
            rows.append(_simulate(prices, pool, rule))
    detail = {
        "meta": {
            "true_etf_count": len(true_meta),
            "local_index_count": len(local_meta),
            "csindex_count": len(cs_meta),
        },
        "rules": [r.__dict__ for r in RULES],
    }
    _write_report(rows, detail)
    payload = {"out": str(OUT_DIR), "rows": len(rows), "report": str(OUT_DIR / "rotation_report_cn.md")}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


if __name__ == "__main__":
    run()
