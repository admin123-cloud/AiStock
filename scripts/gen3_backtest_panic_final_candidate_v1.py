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

from scripts.gen3_audit_panic_30m_failure_visibility import _load_30m, _load_paths
from scripts.gen3_backtest_panic_30m_failure_exit import _apply_policy
from utils.market_warehouse import clickhouse_query_df


WINDOWS = {
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-12-31"),
    "full": ("2020-01-01", "2026-12-31"),
}

POLICIES = ["fixed_hold", "m30_close5_full_nextopen"]
COST_BPS_LIST = [30.0, 50.0, 100.0]


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


def _load_daily_close(candidates: pd.DataFrame) -> dict[tuple[str, pd.Timestamp], float]:
    codes = sorted({str(c) for c in candidates["code"].dropna().tolist() if re.fullmatch(r"[0-9A-Z.]+", str(c))})
    if not codes:
        return {}
    start = candidates["entry_date"].min().strftime("%Y-%m-%d")
    end = candidates["policy_exit_date"].max().strftime("%Y-%m-%d")
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


def _prepare_paths(source: Path, cost_bps: float) -> pd.DataFrame:
    paths = _load_paths(source)
    if "gross_ret" in paths.columns:
        paths["gross_ret"] = pd.to_numeric(paths["gross_ret"], errors="coerce")
        paths["net_ret"] = paths["gross_ret"] - cost_bps / 10000.0
    return paths


def _prepare_policy_candidates(paths: pd.DataFrame, bar_map: dict[str, pd.DataFrame], policy: str, cost_bps: float) -> pd.DataFrame:
    d = pd.DataFrame([_apply_policy(row, bar_map, policy, cost_bps) for _, row in paths.iterrows()])
    d["entry_date"] = pd.to_datetime(d["entry_date"]).dt.normalize()
    d["exit_date"] = pd.to_datetime(d["exit_date"]).dt.normalize()
    if "exit_datetime" not in d.columns:
        d["exit_datetime"] = pd.NaT
    d["policy_exit_date"] = d["exit_date"]
    early = d["executable"].astype(bool) & d["exit_datetime"].notna()
    d.loc[early, "policy_exit_date"] = pd.to_datetime(d.loc[early, "exit_datetime"]).dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"]).dt.normalize()
    d["candidate_score"] = pd.to_numeric(d.get("candidate_score", 0.0), errors="coerce").fillna(-1e9)
    d["chain_rank"] = pd.to_numeric(d.get("chain_rank", 1e9), errors="coerce").fillna(1e9)
    return d.sort_values(["entry_date", "candidate_score", "chain_rank"], ascending=[True, False, True])


def _simulate_slot_mtm(
    candidates: pd.DataFrame,
    initial_capital: float,
    slots: int,
    slot_pct: float,
    mtm_cost_bps: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if candidates.empty:
        return pd.DataFrame(), pd.DataFrame()
    calendar = _trade_calendar(candidates["entry_date"].min(), candidates["policy_exit_date"].max())
    close_map = _load_daily_close(candidates)
    by_entry = {d: g.copy() for d, g in candidates.groupby("entry_date")}
    cash = float(initial_capital)
    open_pos: list[dict] = []
    closed: list[dict] = []
    curve_rows: list[dict] = []
    mtm_cost = mtm_cost_bps / 10000.0

    for day in calendar:
        realized_pnl = 0.0
        still_open: list[dict] = []
        for pos in open_pos:
            if pos["policy_exit_date"] <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["policy_net_ret"]))
                cash += exit_value
                realized_pnl += exit_value - float(pos["stake"])
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = exit_value - float(pos["stake"])
                out["status"] = "closed"
                closed.append(out)
            else:
                still_open.append(pos)
        open_pos = still_open

        opened = 0
        skipped = 0
        todays = by_entry.get(day)
        if todays is not None:
            for row in todays.itertuples(index=False):
                if len(open_pos) >= slots:
                    skipped += 1
                    continue
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                stake = equity_before * slot_pct
                if stake <= 0 or cash < stake:
                    skipped += 1
                    continue
                r = row._asdict()
                r["stake"] = stake
                cash -= stake
                if pd.Timestamp(r["policy_exit_date"]).normalize() <= day:
                    exit_value = stake * (1.0 + float(r["policy_net_ret"]))
                    cash += exit_value
                    realized_pnl += exit_value - stake
                    r["exit_value"] = exit_value
                    r["realized_pnl"] = exit_value - stake
                    r["status"] = "same_day_closed"
                    closed.append(r)
                else:
                    open_pos.append(r)
                opened += 1

        mtm_value = 0.0
        worst_open_mtm_ret = 0.0
        missing_close = 0
        for pos in open_pos:
            close = close_map.get((str(pos["code"]), day))
            if close is None or float(pos["entry_price_adjusted"]) <= 0:
                mtm_value += float(pos["stake"])
                missing_close += 1
                continue
            mtm_ret = close / float(pos["entry_price_adjusted"]) - 1.0 - mtm_cost
            worst_open_mtm_ret = min(worst_open_mtm_ret, mtm_ret)
            mtm_value += float(pos["stake"]) * (1.0 + mtm_ret)

        equity = cash + mtm_value
        curve_rows.append(
            {
                "date": day,
                "cash": cash,
                "reserved_principal": sum(float(p["stake"]) for p in open_pos),
                "mtm_value": mtm_value,
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "skipped": skipped,
                "realized_pnl": realized_pnl,
                "worst_open_mtm_ret": worst_open_mtm_ret,
                "missing_close_positions": missing_close,
            }
        )

    closed_df = pd.DataFrame(closed)
    curve = pd.DataFrame(curve_rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / initial_capital - 1.0
    return closed_df, curve


def _metrics(closed: pd.DataFrame, curve: pd.DataFrame, window: str, policy: str, cost_bps: float) -> dict:
    if curve.empty:
        return {"window": window, "policy": policy, "cost_bps": cost_bps, "closed": 0}
    start, end = WINDOWS[window]
    cw = curve[(curve["date"] >= pd.Timestamp(start)) & (curve["date"] <= pd.Timestamp(end))].copy()
    tw = closed[(closed["entry_date"] >= pd.Timestamp(start)) & (closed["entry_date"] <= pd.Timestamp(end))].copy() if not closed.empty else pd.DataFrame()
    if cw.empty:
        return {"window": window, "policy": policy, "cost_bps": cost_bps, "closed": 0}
    start_equity = float(cw["equity"].iloc[0])
    end_equity = float(cw["equity"].iloc[-1])
    local = cw["equity"] / start_equity
    dd = local / local.cummax() - 1.0
    return {
        "window": window,
        "policy": policy,
        "cost_bps": cost_bps,
        "closed": int(len(tw)),
        "triggered": int(tw["triggered"].sum()) if "triggered" in tw.columns and not tw.empty else 0,
        "executable": int(tw["executable"].sum()) if "executable" in tw.columns and not tw.empty else 0,
        "skipped": int(cw["skipped"].sum()),
        "unique_codes": int(tw["code"].nunique()) if not tw.empty else 0,
        "start_equity": start_equity,
        "end_equity": end_equity,
        "total_ret": end_equity / start_equity - 1.0,
        "max_drawdown": float(dd.min()),
        "worst_open_mtm_ret": float(cw["worst_open_mtm_ret"].min()),
        "win_rate": float((tw["policy_net_ret"] > 0).mean()) if len(tw) else 0.0,
        "mean_trade_ret": float(tw["policy_net_ret"].mean()) if len(tw) else 0.0,
        "worst_trade": float(tw["policy_net_ret"].min()) if len(tw) else 0.0,
        "max_open_positions": int(cw["open_positions"].max()),
        "missing_close_positions": int(cw["missing_close_positions"].sum()),
    }


def _display(raw: pd.DataFrame) -> pd.DataFrame:
    out = raw.copy()
    for col in ["total_ret", "max_drawdown", "worst_open_mtm_ret", "win_rate", "mean_trade_ret", "worst_trade"]:
        if col in out.columns:
            out[col] = out[col].map(_pct)
    for col in ["start_equity", "end_equity"]:
        if col in out.columns:
            out[col] = out[col].map(_money)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="G3 panic final candidate V1 stress test.")
    parser.add_argument("--input", default="reports/gen3_panic_v2_research/systemic_pause_audit_v1/pause_weak_no_capitulation_trades.csv")
    parser.add_argument("--output-dir", default="reports/gen3_panic_v2_research/final_candidate_v1")
    parser.add_argument("--initial-capital", type=float, default=150000.0)
    args = parser.parse_args()

    source = Path(args.input)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for cost_bps in COST_BPS_LIST:
        paths = _prepare_paths(source, cost_bps)
        bars = _load_30m(paths)
        bar_map = {code: g.sort_values("datetime").reset_index(drop=True).copy() for code, g in bars.groupby("code")}
        for policy in POLICIES:
            candidates = _prepare_policy_candidates(paths, bar_map, policy, cost_bps)
            tag = f"{policy}_cost{int(cost_bps)}"
            candidates.to_csv(out_dir / f"{tag}_policy_candidates.csv", index=False, encoding="utf-8-sig")
            closed, curve = _simulate_slot_mtm(
                candidates=candidates,
                initial_capital=args.initial_capital,
                slots=5,
                slot_pct=0.20,
                mtm_cost_bps=cost_bps,
            )
            closed.to_csv(out_dir / f"{tag}_closed_trades.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(out_dir / f"{tag}_mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            for window in WINDOWS:
                rows.append(_metrics(closed, curve, window, policy, cost_bps))

    raw = pd.DataFrame(rows)
    raw.to_csv(out_dir / "final_candidate_summary_raw.csv", index=False, encoding="utf-8-sig")
    display = _display(raw)
    display.to_csv(out_dir / "final_candidate_summary_display.csv", index=False, encoding="utf-8-sig")

    full = display[display["window"].eq("full")]
    c30 = display[display["cost_bps"].astype(str).eq("30.0")]
    lines = [
        "# G3 Panic 最终候选 V1 压力测试",
        "",
        "## 口径",
        "",
        f"- 输入文件：`{source}`",
        f"- 输出目录：`{out_dir}`",
        f"- 初始资金：`{args.initial_capital:.2f}`",
        "- 候选源：`panic_v2_deep_wash_repair + low_safety_margin + avoid_midrisk + pause_weak_no_capitulation`。",
        "- 组合：`slot5_20pct`，最多 5 个持仓槽，每槽 20% 权益。",
        "- 风控候选：`30m 收盘跌破 -5%，下一根 30m 开盘全退`。",
        "- 压力成本：30/50/100bps；持仓期用日线收盘逐日盯市。",
        "- 仍未模拟跌停不可卖、真实排队成交和盘口冲击，因此还不是上线结论。",
        "",
        "## full 压力对照",
        "",
        full[
            [
                "policy",
                "cost_bps",
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
        "## 30bps 分段",
        "",
        c30[
            [
                "window",
                "policy",
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
        "## 判断",
        "",
        "1. 如果 30m -5% 全退在 50/100bps 成本下仍保持收益和回撤优势，才说明它不是成本敏感的幻觉。",
        "2. 如果 valid 段收益继续明显低于固定持有，需要把全退降级为高风险环境熔断，而不是默认规则。",
        "3. 下一步必须做跌停不可卖和延迟成交压力测试，否则不能进入影子实盘。",
    ]
    (out_dir / "final_candidate_report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "input": str(source),
        "output_dir": str(out_dir),
        "policies": POLICIES,
        "cost_bps_list": COST_BPS_LIST,
        "initial_capital": args.initial_capital,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
