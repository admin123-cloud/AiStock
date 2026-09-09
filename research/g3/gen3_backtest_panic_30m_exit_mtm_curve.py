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

from utils.market_warehouse import clickhouse_query_df


WINDOWS = {
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-12-31"),
    "full": ("2020-01-01", "2026-12-31"),
}

POLICIES = [
    "fixed_hold",
    "m30_close5_full_nextopen",
    "m30_close5_half_nextopen",
    "m30_close8_half_nextopen",
]


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    value = pd.to_numeric(v, errors="coerce")
    if pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _money(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v):.2f}"


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _load_policy_trades(input_dir: Path, policy: str) -> pd.DataFrame:
    d = pd.read_csv(input_dir / f"{policy}_trades.csv")
    for col in ["entry_date", "exit_date", "confirm_datetime", "exit_datetime", "trigger_datetime"]:
        if col in d.columns:
            d[col] = pd.to_datetime(d[col], errors="coerce")
    for col in ["stake", "entry_price_adjusted", "baseline_net_ret", "policy_net_ret", "exit_ret_net", "early_exit_fraction"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["triggered"] = d.get("triggered", False).astype(str).str.lower().eq("true")
    d["executable"] = d.get("executable", False).astype(str).str.lower().eq("true")
    return d.dropna(subset=["code", "entry_date", "exit_date", "stake", "entry_price_adjusted", "baseline_net_ret", "policy_net_ret"])


def _trade_calendar(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    sql = f"""
    SELECT DISTINCT trade_date
    FROM kline_daily
    WHERE trade_date BETWEEN toDate({_sql_literal(start.strftime('%Y-%m-%d'))})
      AND toDate({_sql_literal(end.strftime('%Y-%m-%d'))})
    ORDER BY trade_date
    """
    d = clickhouse_query_df(sql)
    if d.empty:
        return []
    return pd.to_datetime(d["trade_date"]).dt.normalize().tolist()


def _load_daily_close(legs: pd.DataFrame) -> dict[tuple[str, pd.Timestamp], float]:
    codes = sorted({str(c) for c in legs["code"].dropna().tolist() if re.fullmatch(r"[0-9A-Z.]+", str(c))})
    if not codes:
        return {}
    start = legs["entry_date"].min().strftime("%Y-%m-%d")
    end = legs["leg_exit_date"].max().strftime("%Y-%m-%d")
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
        return {}
    d["trade_date"] = pd.to_datetime(d["trade_date"]).dt.normalize()
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    d = d.dropna(subset=["code", "trade_date", "close"])
    return {(str(r.code), pd.Timestamp(r.trade_date).normalize()): float(r.close) for r in d.itertuples(index=False)}


def _legs_from_trades(trades: pd.DataFrame, policy: str) -> pd.DataFrame:
    rows: list[dict] = []
    for row in trades.itertuples(index=False):
        r = row._asdict()
        entry_date = pd.Timestamp(r["entry_date"]).normalize()
        fixed_exit_date = pd.Timestamp(r["exit_date"]).normalize()
        stake = float(r["stake"])
        baseline_ret = float(r["baseline_net_ret"])
        if policy == "fixed_hold" or not bool(r.get("executable")):
            leg = r.copy()
            leg.update(
                {
                    "leg_id": f"{r['code']}_{entry_date.date()}_fixed",
                    "leg_fraction": 1.0,
                    "leg_stake": stake,
                    "leg_exit_date": fixed_exit_date,
                    "leg_net_ret": baseline_ret,
                    "leg_source": "fixed_hold",
                }
            )
            rows.append(leg)
            continue

        early_fraction = float(r.get("early_exit_fraction") or 0.0)
        early_ret = float(r.get("exit_ret_net"))
        early_exit_dt = pd.Timestamp(r.get("exit_datetime"))
        early_exit_date = early_exit_dt.normalize()
        if early_fraction >= 1.0:
            leg = r.copy()
            leg.update(
                {
                    "leg_id": f"{r['code']}_{entry_date.date()}_early_full",
                    "leg_fraction": 1.0,
                    "leg_stake": stake,
                    "leg_exit_date": early_exit_date,
                    "leg_net_ret": early_ret,
                    "leg_source": "early_full",
                }
            )
            rows.append(leg)
            continue

        if early_fraction > 0.0:
            early_leg = r.copy()
            early_leg.update(
                {
                    "leg_id": f"{r['code']}_{entry_date.date()}_early_half",
                    "leg_fraction": early_fraction,
                    "leg_stake": stake * early_fraction,
                    "leg_exit_date": early_exit_date,
                    "leg_net_ret": early_ret,
                    "leg_source": "early_partial",
                }
            )
            rows.append(early_leg)

        rest_fraction = 1.0 - early_fraction
        if rest_fraction > 0.0:
            rest_leg = r.copy()
            rest_leg.update(
                {
                    "leg_id": f"{r['code']}_{entry_date.date()}_rest_hold",
                    "leg_fraction": rest_fraction,
                    "leg_stake": stake * rest_fraction,
                    "leg_exit_date": fixed_exit_date,
                    "leg_net_ret": baseline_ret,
                    "leg_source": "rest_fixed_hold",
                }
            )
            rows.append(rest_leg)

    out = pd.DataFrame(rows)
    out["entry_date"] = pd.to_datetime(out["entry_date"]).dt.normalize()
    out["leg_exit_date"] = pd.to_datetime(out["leg_exit_date"]).dt.normalize()
    return out


def _simulate(legs: pd.DataFrame, initial_capital: float, cost_bps: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    if legs.empty:
        return pd.DataFrame(), pd.DataFrame()
    calendar = _trade_calendar(legs["entry_date"].min(), legs["leg_exit_date"].max())
    close_map = _load_daily_close(legs)
    cash = float(initial_capital)
    open_legs: list[dict] = []
    closed: list[dict] = []
    curve_rows: list[dict] = []
    by_entry = {d: g.copy() for d, g in legs.groupby("entry_date")}
    mtm_cost = cost_bps / 10000.0

    for day in calendar:
        realized_pnl = 0.0
        still_open: list[dict] = []
        for leg in open_legs:
            if leg["leg_exit_date"] <= day:
                exit_value = float(leg["leg_stake"]) * (1.0 + float(leg["leg_net_ret"]))
                cash += exit_value
                realized_pnl += exit_value - float(leg["leg_stake"])
                out = leg.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = exit_value - float(leg["leg_stake"])
                closed.append(out)
            else:
                still_open.append(leg)
        open_legs = still_open

        todays = by_entry.get(day)
        opened = 0
        if todays is not None:
            for leg in todays.itertuples(index=False):
                r = leg._asdict()
                cash -= float(r["leg_stake"])
                open_legs.append(r)
                opened += 1

        mtm_value = 0.0
        worst_open_ret = 0.0
        for leg in open_legs:
            close = close_map.get((str(leg["code"]), day))
            if close is None or float(leg["entry_price_adjusted"]) <= 0:
                mtm_value += float(leg["leg_stake"])
                continue
            mtm_ret = close / float(leg["entry_price_adjusted"]) - 1.0 - mtm_cost
            worst_open_ret = min(worst_open_ret, mtm_ret)
            mtm_value += float(leg["leg_stake"]) * (1.0 + mtm_ret)

        equity = cash + mtm_value
        curve_rows.append(
            {
                "date": day,
                "cash": cash,
                "mtm_value": mtm_value,
                "equity": equity,
                "open_legs": len(open_legs),
                "opened_legs": opened,
                "realized_pnl": realized_pnl,
                "worst_open_mtm_ret": worst_open_ret,
            }
        )

    curve = pd.DataFrame(curve_rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / initial_capital - 1.0
    return pd.DataFrame(closed), curve


def _metrics(curve: pd.DataFrame, closed: pd.DataFrame, window: str, policy: str) -> dict:
    if curve.empty:
        return {"window": window, "policy": policy, "closed_legs": 0}
    start, end = WINDOWS[window]
    cw = curve[(curve["date"] >= pd.Timestamp(start)) & (curve["date"] <= pd.Timestamp(end))].copy()
    tw = closed[(closed["entry_date"] >= pd.Timestamp(start)) & (closed["entry_date"] <= pd.Timestamp(end))].copy() if not closed.empty else pd.DataFrame()
    if cw.empty:
        return {"window": window, "policy": policy, "closed_legs": 0}
    start_equity = float(cw["equity"].iloc[0])
    end_equity = float(cw["equity"].iloc[-1])
    local = cw["equity"] / start_equity
    dd = local / local.cummax() - 1.0
    return {
        "window": window,
        "policy": policy,
        "closed_legs": int(len(tw)),
        "unique_trades": int(tw[["code", "entry_date"]].drop_duplicates().shape[0]) if not tw.empty else 0,
        "start_equity": start_equity,
        "end_equity": end_equity,
        "total_ret": end_equity / start_equity - 1.0,
        "max_drawdown": float(dd.min()),
        "worst_open_mtm_ret": float(cw["worst_open_mtm_ret"].min()),
        "max_open_legs": int(cw["open_legs"].max()),
    }


def _display(raw: pd.DataFrame) -> pd.DataFrame:
    out = raw.copy()
    for col in ["total_ret", "max_drawdown", "worst_open_mtm_ret"]:
        if col in out.columns:
            out[col] = out[col].map(_pct)
    for col in ["start_equity", "end_equity"]:
        if col in out.columns:
            out[col] = out[col].map(_money)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="MTM curve for G3 panic 30m failure exits.")
    parser.add_argument("--input-dir", default="reports/gen3_panic_v2_research/failure_exit_30m_v1")
    parser.add_argument("--output-dir", default="reports/gen3_panic_v2_research/failure_exit_30m_mtm_curve_v1")
    parser.add_argument("--initial-capital", type=float, default=150000.0)
    parser.add_argument("--mtm-cost-bps", type=float, default=30.0)
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for policy in POLICIES:
        trades = _load_policy_trades(input_dir, policy)
        legs = _legs_from_trades(trades, policy)
        legs.to_csv(out_dir / f"{policy}_legs.csv", index=False, encoding="utf-8-sig")
        closed, curve = _simulate(legs, args.initial_capital, args.mtm_cost_bps)
        closed.to_csv(out_dir / f"{policy}_closed_legs.csv", index=False, encoding="utf-8-sig")
        curve.to_csv(out_dir / f"{policy}_mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
        for window in WINDOWS:
            rows.append(_metrics(curve, closed, window, policy))

    raw = pd.DataFrame(rows)
    raw.to_csv(out_dir / "failure_exit_30m_mtm_summary_raw.csv", index=False, encoding="utf-8-sig")
    display = _display(raw)
    display.to_csv(out_dir / "failure_exit_30m_mtm_summary_display.csv", index=False, encoding="utf-8-sig")

    full = display[display["window"].eq("full")]
    segmented = display[display["policy"].isin(["fixed_hold", "m30_close5_full_nextopen", "m30_close5_half_nextopen"])]
    lines = [
        "# G3 Panic 30m 失败退出逐日盯市资金曲线 V1",
        "",
        "## 口径",
        "",
        f"- 输入目录：`{input_dir}`",
        f"- 输出目录：`{out_dir}`",
        f"- 初始资金：`{args.initial_capital:.2f}`",
        "- 使用同一批已经成交的 `pause_weak_no_capitulation + slot5_20pct` 交易，不重新释放资金寻找新开仓，因此这是保守对照。",
        "- 全退出策略在 30m 失败后的下一根开盘全部退出；半退出策略拆成早退腿和原持有腿。",
        "- 持仓期间仍用日线收盘逐日盯市，未模拟跌停不可卖和真实排队。",
        "",
        "## full 对照",
        "",
        full[
            [
                "policy",
                "closed_legs",
                "unique_trades",
                "total_ret",
                "max_drawdown",
                "worst_open_mtm_ret",
                "max_open_legs",
            ]
        ].to_markdown(index=False),
        "",
        "## 重点策略分段",
        "",
        segmented[
            [
                "window",
                "policy",
                "unique_trades",
                "total_ret",
                "max_drawdown",
                "worst_open_mtm_ret",
                "max_open_legs",
            ]
        ].to_markdown(index=False),
        "",
        "## 判断",
        "",
        "1. 若半退出在收益损失较小的情况下继续压低盯市回撤，它比全退出更适合作为下一轮候选。",
        "2. 若全退出明显压回撤但牺牲 valid 收益，应只作为高风险环境下的应急风控，而不是默认退出。",
        "3. 下一步如果继续，应加入真实 slot 复算，让早退释放的现金可以参与后续开仓。",
    ]
    (out_dir / "failure_exit_30m_mtm_report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "input_dir": str(input_dir),
        "output_dir": str(out_dir),
        "policies": POLICIES,
        "initial_capital": args.initial_capital,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
