from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_audit_panic_30m_failure_visibility import _load_30m
from scripts.gen3_backtest_panic_final_candidate_v1 import _sql_literal
from utils.market_warehouse import clickhouse_query_df


WINDOWS = {
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-12-31"),
    "full": ("2020-01-01", "2026-12-31"),
}


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def _load_trades(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path)
    for col in ["entry_date", "exit_date", "policy_exit_date", "confirm_datetime", "exit_datetime", "trigger_datetime"]:
        if col in d.columns:
            d[col] = pd.to_datetime(d[col], errors="coerce")
    numeric_cols = [
        "entry_price_adjusted",
        "policy_net_ret",
        "baseline_net_ret",
        "exit_price",
        "exit_ret_net",
        "candidate_score",
        "chain_rank",
    ]
    for col in numeric_cols:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["entry_date"] = d["entry_date"].dt.normalize()
    d["exit_date"] = d["exit_date"].dt.normalize()
    d["policy_exit_date"] = d["policy_exit_date"].dt.normalize()
    return d.dropna(subset=["code", "entry_date", "exit_date", "policy_exit_date", "confirm_datetime"]).copy()


def _limit_pct(code: str, name: str, trade_date: pd.Timestamp) -> float:
    text = f"{name or ''}".upper()
    if "ST" in text:
        return 0.05
    code_text = str(code)
    if code_text.startswith(("688", "689")):
        return 0.20
    if code_text.startswith(("300", "301")) and trade_date >= pd.Timestamp("2020-08-24"):
        return 0.20
    if code_text.startswith(("8", "4", "920")):
        return 0.30
    return 0.10


def _load_daily(trades: pd.DataFrame) -> pd.DataFrame:
    codes = sorted({str(c) for c in trades["code"].dropna().tolist() if re.fullmatch(r"[0-9A-Z.]+", str(c))})
    if not codes:
        return pd.DataFrame()
    start = (trades["entry_date"].min() - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    end = (trades["policy_exit_date"].max() + pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    code_list = ",".join(_sql_literal(code) for code in codes)
    sql = f"""
    SELECT code, trade_date, open, high, low, close, amount
    FROM kline_daily
    WHERE code IN ({code_list})
      AND trade_date BETWEEN toDate({_sql_literal(start)}) AND toDate({_sql_literal(end)})
    ORDER BY code, trade_date
    """
    d = clickhouse_query_df(sql)
    if d.empty:
        return d
    d["trade_date"] = pd.to_datetime(d["trade_date"]).dt.normalize()
    for col in ["open", "high", "low", "close", "amount"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["code", "trade_date", "open", "high", "low", "close"]).copy()
    d["prev_close"] = d.groupby("code")["close"].shift(1)
    return d


def _near_down(value: float | None, down_limit: float | None, tolerance: float) -> bool:
    if value is None or down_limit is None or pd.isna(value) or pd.isna(down_limit) or down_limit <= 0:
        return False
    return float(value) <= float(down_limit) * (1.0 + tolerance)


def _bar_at_or_after(bars: pd.DataFrame, dt: pd.Timestamp) -> pd.Series | None:
    if bars.empty or pd.isna(dt):
        return None
    hit = bars[bars["datetime"] >= dt]
    if hit.empty:
        return None
    return hit.iloc[0]


def _bar_exact(bars: pd.DataFrame, dt: pd.Timestamp) -> pd.Series | None:
    if bars.empty or pd.isna(dt):
        return None
    hit = bars[bars["datetime"] == dt]
    if hit.empty:
        return _bar_at_or_after(bars, dt)
    return hit.iloc[0]


def _audit_one(
    row: pd.Series,
    daily_map: dict[tuple[str, pd.Timestamp], pd.Series],
    bar_map: dict[str, pd.DataFrame],
    tolerance: float,
) -> dict:
    code = str(row["code"])
    name = str(row.get("name", ""))
    entry_day = pd.Timestamp(row["entry_date"]).normalize()
    exit_day = pd.Timestamp(row["policy_exit_date"]).normalize()
    out = row.to_dict()

    bars = bar_map.get(code, pd.DataFrame())
    entry_bar = _bar_exact(bars, pd.Timestamp(row["confirm_datetime"]))
    exit_dt = pd.Timestamp(row["exit_datetime"]) if "exit_datetime" in row and pd.notna(row["exit_datetime"]) else pd.NaT
    exit_bar = _bar_exact(bars, exit_dt) if pd.notna(exit_dt) else None

    entry_daily = daily_map.get((code, entry_day))
    exit_daily = daily_map.get((code, exit_day))

    for prefix, day, daily, bar in [
        ("entry", entry_day, entry_daily, entry_bar),
        ("exit", exit_day, exit_daily, exit_bar),
    ]:
        limit_pct = _limit_pct(code, name, day)
        prev_close = float(daily["prev_close"]) if daily is not None and pd.notna(daily.get("prev_close")) else None
        down_limit = prev_close * (1.0 - limit_pct) if prev_close else None
        out[f"{prefix}_limit_pct"] = limit_pct
        out[f"{prefix}_prev_close"] = prev_close
        out[f"{prefix}_down_limit_est"] = down_limit
        out[f"{prefix}_daily_missing"] = daily is None
        out[f"{prefix}_bar_missing"] = bar is None
        if daily is not None:
            out[f"{prefix}_daily_open_near_down_limit"] = _near_down(float(daily["open"]), down_limit, tolerance)
            out[f"{prefix}_daily_low_near_down_limit"] = _near_down(float(daily["low"]), down_limit, tolerance)
            out[f"{prefix}_daily_close_near_down_limit"] = _near_down(float(daily["close"]), down_limit, tolerance)
            out[f"{prefix}_daily_locked_down_proxy"] = (
                _near_down(float(daily["open"]), down_limit, tolerance)
                and _near_down(float(daily["high"]), down_limit, tolerance)
                and _near_down(float(daily["close"]), down_limit, tolerance)
            )
            out[f"{prefix}_daily_amount"] = float(daily.get("amount", float("nan")))
        if bar is not None:
            out[f"{prefix}_bar_datetime"] = bar["datetime"]
            out[f"{prefix}_bar_open_near_down_limit"] = _near_down(float(bar["adj_open"]), down_limit, tolerance)
            out[f"{prefix}_bar_low_near_down_limit"] = _near_down(float(bar["adj_low"]), down_limit, tolerance)
            out[f"{prefix}_bar_close_near_down_limit"] = _near_down(float(bar["adj_close"]), down_limit, tolerance)
            out[f"{prefix}_bar_locked_down_proxy"] = (
                _near_down(float(bar["adj_open"]), down_limit, tolerance)
                and _near_down(float(bar["adj_high"]), down_limit, tolerance)
                and _near_down(float(bar["adj_close"]), down_limit, tolerance)
            )
            out[f"{prefix}_bar_amount"] = float(bar.get("amount", float("nan")))

    out["entry_execution_risk_proxy"] = bool(
        out.get("entry_bar_locked_down_proxy", False)
        or out.get("entry_daily_locked_down_proxy", False)
        or out.get("entry_bar_missing", False)
    )
    early_exit = bool(row.get("triggered", False)) and bool(row.get("executable", False))
    out["exit_is_early_m30"] = early_exit
    out["exit_execution_risk_proxy"] = bool(
        out.get("exit_daily_locked_down_proxy", False)
        or (early_exit and out.get("exit_bar_open_near_down_limit", False))
        or (early_exit and out.get("exit_bar_locked_down_proxy", False))
        or out.get("exit_daily_missing", False)
        or (early_exit and out.get("exit_bar_missing", False))
    )
    out["any_execution_risk_proxy"] = bool(out["entry_execution_risk_proxy"] or out["exit_execution_risk_proxy"])
    return out


def _summary(audit: pd.DataFrame, window: str) -> dict:
    start, end = WINDOWS[window]
    w = audit[(audit["entry_date"] >= pd.Timestamp(start)) & (audit["entry_date"] <= pd.Timestamp(end))].copy()
    if w.empty:
        return {"window": window, "trades": 0}
    return {
        "window": window,
        "trades": int(len(w)),
        "early_m30_exits": int(w["exit_is_early_m30"].sum()),
        "entry_risk_count": int(w["entry_execution_risk_proxy"].sum()),
        "exit_risk_count": int(w["exit_execution_risk_proxy"].sum()),
        "any_risk_count": int(w["any_execution_risk_proxy"].sum()),
        "entry_risk_rate": float(w["entry_execution_risk_proxy"].mean()),
        "exit_risk_rate": float(w["exit_execution_risk_proxy"].mean()),
        "any_risk_rate": float(w["any_execution_risk_proxy"].mean()),
        "missing_daily_count": int(w["entry_daily_missing"].sum() + w["exit_daily_missing"].sum()),
        "missing_exit_bar_count": int(w["exit_is_early_m30"].astype(bool).mul(w["exit_bar_missing"].astype(bool)).sum()),
        "mean_policy_net_ret": float(w["policy_net_ret"].mean()),
        "worst_policy_net_ret": float(w["policy_net_ret"].min()),
    }


def _display(raw: pd.DataFrame) -> pd.DataFrame:
    out = raw.copy()
    for col in ["entry_risk_rate", "exit_risk_rate", "any_risk_rate", "mean_policy_net_ret", "worst_policy_net_ret"]:
        if col in out.columns:
            out[col] = out[col].map(_pct)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit execution feasibility for G3 panic final candidate.")
    parser.add_argument(
        "--input",
        default="reports/gen3_panic_v2_research/final_candidate_v1/m30_close5_full_nextopen_cost30_closed_trades.csv",
    )
    parser.add_argument("--output-dir", default="reports/gen3_panic_v2_research/execution_feasibility_v1")
    parser.add_argument("--tolerance-bps", type=float, default=30.0)
    args = parser.parse_args()

    source = Path(args.input)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tolerance = args.tolerance_bps / 10000.0

    trades = _load_trades(source)
    daily = _load_daily(trades)
    daily_map = {(str(r.code), pd.Timestamp(r.trade_date).normalize()): pd.Series(r._asdict()) for r in daily.itertuples(index=False)}
    bars = _load_30m(trades)
    bar_map = {code: g.sort_values("datetime").reset_index(drop=True).copy() for code, g in bars.groupby("code")}

    audit = pd.DataFrame([_audit_one(row, daily_map, bar_map, tolerance) for _, row in trades.iterrows()])
    audit.to_csv(out_dir / "execution_feasibility_trades.csv", index=False, encoding="utf-8-sig")

    raw = pd.DataFrame([_summary(audit, window) for window in WINDOWS])
    raw.to_csv(out_dir / "execution_feasibility_summary_raw.csv", index=False, encoding="utf-8-sig")
    display = _display(raw)
    display.to_csv(out_dir / "execution_feasibility_summary_display.csv", index=False, encoding="utf-8-sig")

    risky = audit[audit["any_execution_risk_proxy"].astype(bool)].copy()
    risky_cols = [
        "entry_date",
        "code",
        "name",
        "policy_exit_date",
        "policy_net_ret",
        "exit_source",
        "entry_execution_risk_proxy",
        "exit_execution_risk_proxy",
        "entry_daily_locked_down_proxy",
        "entry_bar_locked_down_proxy",
        "exit_daily_locked_down_proxy",
        "exit_bar_open_near_down_limit",
        "exit_bar_locked_down_proxy",
        "entry_daily_missing",
        "exit_daily_missing",
        "exit_bar_missing",
    ]
    risky[[c for c in risky_cols if c in risky.columns]].to_csv(out_dir / "execution_feasibility_risky_trades.csv", index=False, encoding="utf-8-sig")

    lines = [
        "# G3 Panic 最终候选 V1 可成交性审计",
        "",
        "## 口径",
        "",
        f"- 输入：`{source}`",
        f"- 输出目录：`{out_dir}`",
        f"- 跌停接近容忍：`{args.tolerance_bps:.1f}bps`",
        "- 涨跌停价用前一交易日收盘估算：普通 10%，创业板/科创板 20%，北交所 30%，名称含 ST 按 5%。",
        "- 本审计只标记“可能有成交偏差”的交易，不改变收益曲线，不做参数优化。",
        "",
        "## 分段摘要",
        "",
        display.to_markdown(index=False),
        "",
        "## 风险交易",
        "",
    ]
    if risky.empty:
        lines.append("未发现按当前口径标记为买入/退出不可成交风险的交易。")
    else:
        show = risky[[c for c in risky_cols if c in risky.columns]].copy()
        for col in ["policy_net_ret"]:
            if col in show.columns:
                show[col] = show[col].map(_pct)
        lines.append(show.to_markdown(index=False))
    lines += [
        "",
        "## 判断",
        "",
        "1. 若风险交易集中在 30m 早退退出端，说明风控规则需要增加“跌停无法卖出”的延迟队列模拟。",
        "2. 若风险交易主要在买入端，说明恐慌买点可能存在回测可买、实盘排队的问题，需要下调成交假设。",
        "3. 若风险交易很少，G3 V1 可以进入影子盘验证，但仍不能直接实盘下单。",
    ]
    (out_dir / "execution_feasibility_report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "input": str(source),
        "output_dir": str(out_dir),
        "tolerance_bps": args.tolerance_bps,
        "trades": int(len(audit)),
        "risk_trades": int(audit["any_execution_risk_proxy"].sum()) if not audit.empty else 0,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
