from __future__ import annotations

import argparse
import json
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

POLICIES = ["fixed_hold", "m30_close5_full_nextopen", "m30_close8_full_nextopen"]


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


def _trade_calendar(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    sql = f"""
    SELECT DISTINCT trade_date
    FROM kline_daily
    WHERE trade_date BETWEEN toDate('{start.strftime("%Y-%m-%d")}')
      AND toDate('{end.strftime("%Y-%m-%d")}')
    ORDER BY trade_date
    """
    d = clickhouse_query_df(sql)
    if d.empty:
        return []
    return pd.to_datetime(d["trade_date"]).dt.normalize().tolist()


def _prepare_policy_candidates(source: Path, policy: str, cost_bps: float) -> pd.DataFrame:
    paths = _load_paths(source)
    bars = _load_30m(paths)
    bar_map = {code: g.sort_values("datetime").reset_index(drop=True).copy() for code, g in bars.groupby("code")}
    d = pd.DataFrame([_apply_policy(row, bar_map, policy, cost_bps) for _, row in paths.iterrows()])
    d["entry_date"] = pd.to_datetime(d["entry_date"]).dt.normalize()
    d["exit_date"] = pd.to_datetime(d["exit_date"]).dt.normalize()
    if "exit_datetime" not in d.columns:
        d["exit_datetime"] = pd.NaT
    d["policy_exit_date"] = d["exit_date"]
    early = d["executable"].astype(bool) & d["exit_datetime"].notna()
    d.loc[early, "policy_exit_date"] = pd.to_datetime(d.loc[early, "exit_datetime"]).dt.normalize()
    d["candidate_score"] = pd.to_numeric(d.get("candidate_score", 0.0), errors="coerce").fillna(-1e9)
    d["chain_rank"] = pd.to_numeric(d.get("chain_rank", 1e9), errors="coerce").fillna(1e9)
    return d.sort_values(["entry_date", "candidate_score", "chain_rank"], ascending=[True, False, True])


def _simulate_slots(candidates: pd.DataFrame, initial_capital: float, slots: int, slot_pct: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    if candidates.empty:
        return pd.DataFrame(), pd.DataFrame()
    calendar = _trade_calendar(candidates["entry_date"].min(), candidates["policy_exit_date"].max())
    by_entry = {d: g.copy() for d, g in candidates.groupby("entry_date")}
    cash = float(initial_capital)
    open_pos: list[dict] = []
    closed: list[dict] = []
    curve_rows: list[dict] = []

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

        equity = cash + sum(float(p["stake"]) for p in open_pos)
        curve_rows.append(
            {
                "date": day,
                "cash": cash,
                "reserved_principal": sum(float(p["stake"]) for p in open_pos),
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "skipped": skipped,
                "realized_pnl": realized_pnl,
            }
        )

    closed_df = pd.DataFrame(closed)
    curve = pd.DataFrame(curve_rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / initial_capital - 1.0
    return closed_df, curve


def _metrics(closed: pd.DataFrame, curve: pd.DataFrame, window: str, policy: str, book: str) -> dict:
    if curve.empty:
        return {"window": window, "policy": policy, "book": book, "closed": 0}
    start, end = WINDOWS[window]
    cw = curve[(curve["date"] >= pd.Timestamp(start)) & (curve["date"] <= pd.Timestamp(end))].copy()
    tw = closed[(closed["entry_date"] >= pd.Timestamp(start)) & (closed["entry_date"] <= pd.Timestamp(end))].copy() if not closed.empty else pd.DataFrame()
    if cw.empty:
        return {"window": window, "policy": policy, "book": book, "closed": 0}
    start_equity = float(cw["equity"].iloc[0])
    end_equity = float(cw["equity"].iloc[-1])
    local = cw["equity"] / start_equity
    dd = local / local.cummax() - 1.0
    return {
        "window": window,
        "policy": policy,
        "book": book,
        "closed": int(len(tw)),
        "triggered": int(tw["triggered"].sum()) if "triggered" in tw.columns and not tw.empty else 0,
        "executable": int(tw["executable"].sum()) if "executable" in tw.columns and not tw.empty else 0,
        "unique_codes": int(tw["code"].nunique()) if not tw.empty else 0,
        "start_equity": start_equity,
        "end_equity": end_equity,
        "total_ret": end_equity / start_equity - 1.0,
        "max_drawdown": float(dd.min()),
        "win_rate": float((tw["policy_net_ret"] > 0).mean()) if len(tw) else 0.0,
        "mean_trade_ret": float(tw["policy_net_ret"].mean()) if len(tw) else 0.0,
        "worst_trade": float(tw["policy_net_ret"].min()) if len(tw) else 0.0,
        "max_open_positions": int(cw["open_positions"].max()),
        "skipped": int(cw["skipped"].sum()),
    }


def _display(raw: pd.DataFrame) -> pd.DataFrame:
    out = raw.copy()
    for col in ["total_ret", "max_drawdown", "win_rate", "mean_trade_ret", "worst_trade"]:
        if col in out.columns:
            out[col] = out[col].map(_pct)
    for col in ["start_equity", "end_equity"]:
        if col in out.columns:
            out[col] = out[col].map(_money)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Full slot resimulation for G3 panic 30m full exits.")
    parser.add_argument("--input", default="reports/gen3_panic_v2_research/systemic_pause_audit_v1/pause_weak_no_capitulation_trades.csv")
    parser.add_argument("--output-dir", default="reports/gen3_panic_v2_research/failure_exit_30m_slot_resim_v1")
    parser.add_argument("--initial-capital", type=float, default=150000.0)
    parser.add_argument("--cost-bps", type=float, default=30.0)
    args = parser.parse_args()

    source = Path(args.input)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    books = {"slot5_20pct": (5, 0.20), "slot10_10pct": (10, 0.10)}

    rows: list[dict] = []
    for policy in POLICIES:
        candidates = _prepare_policy_candidates(source, policy, args.cost_bps)
        candidates.to_csv(out_dir / f"{policy}_policy_candidates.csv", index=False, encoding="utf-8-sig")
        for book_name, (slots, slot_pct) in books.items():
            closed, curve = _simulate_slots(candidates, args.initial_capital, slots, slot_pct)
            closed.to_csv(out_dir / f"{policy}_{book_name}_closed_trades.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(out_dir / f"{policy}_{book_name}_equity_curve.csv", index=False, encoding="utf-8-sig")
            for window in WINDOWS:
                rows.append(_metrics(closed, curve, window, policy, book_name))

    raw = pd.DataFrame(rows)
    raw.to_csv(out_dir / "failure_exit_30m_slot_resim_summary_raw.csv", index=False, encoding="utf-8-sig")
    display = _display(raw)
    display.to_csv(out_dir / "failure_exit_30m_slot_resim_summary_display.csv", index=False, encoding="utf-8-sig")

    full = display[display["window"].eq("full")]
    slot5 = display[display["book"].eq("slot5_20pct")]
    lines = [
        "# G3 Panic 30m 全退出完整 slot 复算 V1",
        "",
        "## 口径",
        "",
        f"- 输入文件：`{source}`",
        f"- 输出目录：`{out_dir}`",
        f"- 初始资金：`{args.initial_capital:.2f}`",
        "- 使用 `pause_weak_no_capitulation` 全部 97 条候选交易重新跑 slot；早退释放的现金和持仓槽可参与后续开仓。",
        "- 本版只复算全退出：`m30_close5_full_nextopen`、`m30_close8_full_nextopen`；半退出需要另行定义持仓槽占用。",
        "- 资金曲线仍是退出日确认盈亏，不做持仓期 MTM；用于回答早退释放资金后收益是否被补回。",
        "",
        "## full 对照",
        "",
        full[
            [
                "policy",
                "book",
                "closed",
                "triggered",
                "executable",
                "skipped",
                "total_ret",
                "max_drawdown",
                "win_rate",
                "mean_trade_ret",
                "worst_trade",
                "max_open_positions",
            ]
        ].to_markdown(index=False),
        "",
        "## slot5 分段",
        "",
        slot5[
            [
                "window",
                "policy",
                "closed",
                "triggered",
                "executable",
                "skipped",
                "total_ret",
                "max_drawdown",
                "win_rate",
                "mean_trade_ret",
                "worst_trade",
            ]
        ].to_markdown(index=False),
        "",
        "## 判断",
        "",
        "1. 如果完整 slot 复算后收益仍明显低于固定持有，说明早退释放资金不能补偿被打掉的修复弹性。",
        "2. 如果回撤改善稳定但 valid 收益牺牲大，30m 全退出更适合作为高风险熔断，而不是默认规则。",
        "3. 下一步若继续，需要把全退出规则和系统性暂停合并到逐日 MTM + slot 复算，形成最终风控候选。",
    ]
    (out_dir / "failure_exit_30m_slot_resim_report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "input": str(source),
        "output_dir": str(out_dir),
        "policies": POLICIES,
        "initial_capital": args.initial_capital,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
