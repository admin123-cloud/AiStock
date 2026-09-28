from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import argparse
import json
from pathlib import Path

import pandas as pd


WINDOWS = {
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-12-31"),
    "full": ("2020-01-01", "2026-12-31"),
}


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{v * 100:.2f}%"


def _load_trades(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path)
    d["entry_date"] = pd.to_datetime(d["entry_date"]).dt.normalize()
    d["exit_date"] = pd.to_datetime(d["exit_date"]).dt.normalize()
    d["net_ret"] = pd.to_numeric(d["net_ret"], errors="coerce")
    d["candidate_score"] = pd.to_numeric(d.get("candidate_score", 0.0), errors="coerce").fillna(-1e9)
    d["chain_rank"] = pd.to_numeric(d.get("chain_rank", 1e9), errors="coerce").fillna(1e9)
    return d.dropna(subset=["entry_date", "exit_date", "net_ret"]).sort_values(
        ["entry_date", "candidate_score", "chain_rank"], ascending=[True, False, True]
    )


def _simulate(trades: pd.DataFrame, initial_capital: float, slots: int, slot_pct: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    cash = float(initial_capital)
    open_positions: list[dict] = []
    trade_rows: list[dict] = []
    curve_rows: list[dict] = []

    all_dates = sorted(set(trades["entry_date"].tolist()) | set(trades["exit_date"].tolist()))
    for current_date in all_dates:
        realized_pnl = 0.0
        still_open: list[dict] = []
        for pos in open_positions:
            if pos["exit_date"] <= current_date:
                exit_value = pos["stake"] * (1.0 + pos["net_ret"])
                pnl = exit_value - pos["stake"]
                cash += exit_value
                realized_pnl += pnl
                pos = pos.copy()
                pos["status"] = "closed"
                pos["realized_pnl"] = pnl
                pos["exit_value"] = exit_value
                trade_rows.append(pos)
            else:
                still_open.append(pos)
        open_positions = still_open

        todays = trades[trades["entry_date"].eq(current_date)].copy()
        opened = 0
        skipped = 0
        for row in todays.itertuples(index=False):
            if len(open_positions) >= slots:
                skipped += 1
                continue
            equity_before_open = cash + sum(p["stake"] for p in open_positions)
            stake = equity_before_open * slot_pct
            if stake <= 0 or cash < stake:
                skipped += 1
                continue
            cash -= stake
            pos = row._asdict()
            pos["stake"] = stake
            pos["status"] = "open"
            pos["realized_pnl"] = 0.0
            pos["exit_value"] = 0.0
            open_positions.append(pos)
            opened += 1

        equity = cash + sum(p["stake"] for p in open_positions)
        curve_rows.append(
            {
                "date": current_date,
                "cash": cash,
                "reserved_principal": sum(p["stake"] for p in open_positions),
                "equity": equity,
                "open_positions": len(open_positions),
                "opened": opened,
                "skipped": skipped,
                "realized_pnl": realized_pnl,
            }
        )

    for pos in open_positions:
        pos = pos.copy()
        pos["status"] = "still_open"
        trade_rows.append(pos)

    curve = pd.DataFrame(curve_rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / initial_capital - 1.0
    executed = pd.DataFrame(trade_rows)
    return executed, curve


def _metrics(executed: pd.DataFrame, curve: pd.DataFrame, window_name: str, policy: str, book: str, initial_capital: float) -> dict:
    if curve.empty:
        return {"window": window_name, "policy": policy, "book": book, "executed": 0}
    start, end = WINDOWS[window_name]
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    cw = curve[(curve["date"] >= start_ts) & (curve["date"] <= end_ts)].copy()
    tw = executed[(executed["entry_date"] >= start_ts) & (executed["entry_date"] <= end_ts)].copy() if not executed.empty else pd.DataFrame()
    if cw.empty:
        return {"window": window_name, "policy": policy, "book": book, "executed": 0}
    start_equity = float(cw["equity"].iloc[0])
    end_equity = float(cw["equity"].iloc[-1])
    local_equity = cw["equity"] / start_equity
    local_dd = local_equity / local_equity.cummax() - 1.0
    closed = tw[tw["status"].eq("closed")] if not tw.empty and "status" in tw.columns else pd.DataFrame()
    return {
        "window": window_name,
        "policy": policy,
        "book": book,
        "executed": int(len(tw)),
        "closed": int(len(closed)),
        "unique_codes": int(tw["code"].nunique()) if not tw.empty and "code" in tw.columns else 0,
        "start_equity": start_equity,
        "end_equity": end_equity,
        "total_ret": end_equity / start_equity - 1.0,
        "max_drawdown": float(local_dd.min()),
        "win_rate": float((closed["net_ret"] > 0).mean()) if len(closed) else 0.0,
        "mean_trade_ret": float(closed["net_ret"].mean()) if len(closed) else 0.0,
        "worst_trade": float(closed["net_ret"].min()) if len(closed) else 0.0,
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
            out[col] = out[col].map(lambda v: f"{float(v):.2f}" if pd.notna(v) else "")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate G3 panic module capital curve.")
    parser.add_argument("--input-dir", default="reports/gen3_panic_v2_research/systemic_pause_audit_v1")
    parser.add_argument("--output-dir", default="reports/gen3_panic_v2_research/capital_curve_v1")
    parser.add_argument("--initial-capital", type=float, default=150000.0)
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    policies = ["base", "pause_weak_no_capitulation"]
    books = {
        "slot5_20pct": (5, 0.20),
        "slot10_10pct": (10, 0.10),
    }

    rows: list[dict] = []
    for policy in policies:
        source = input_dir / f"{policy}_trades.csv"
        trades = _load_trades(source)
        for book_name, (slots, slot_pct) in books.items():
            executed, curve = _simulate(trades, args.initial_capital, slots, slot_pct)
            executed.to_csv(out_dir / f"{policy}_{book_name}_executed_trades.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(out_dir / f"{policy}_{book_name}_equity_curve.csv", index=False, encoding="utf-8-sig")
            for window in WINDOWS:
                rows.append(_metrics(executed, curve, window, policy, book_name, args.initial_capital))

    raw = pd.DataFrame(rows)
    raw.to_csv(out_dir / "capital_curve_summary_raw.csv", index=False, encoding="utf-8-sig")
    display = _display(raw)
    display.to_csv(out_dir / "capital_curve_summary_display.csv", index=False, encoding="utf-8-sig")

    full = display[display["window"].eq("full")]
    segmented = display[(display["policy"].eq("pause_weak_no_capitulation")) & (display["book"].eq("slot5_20pct"))]
    lines = [
        "# G3 Panic 资金曲线审计 V1",
        "",
        "## 口径",
        "",
        f"- 输入目录：`{input_dir}`",
        f"- 输出目录：`{out_dir}`",
        f"- 初始资金：`{args.initial_capital:.2f}`。",
        "- 两个固定资金簿：`slot5_20pct`、`slot10_10pct`。",
        "- 每天先处理退出释放资金，再按信号排名开仓；资金或槽位不足则跳过。",
        "- 未做盘中市值波动标记，只在退出日确认盈亏；这是研究用资金占用曲线，不是最终实盘曲线。",
        "",
        "## full 对照",
        "",
        full[
            [
                "policy",
                "book",
                "executed",
                "closed",
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
        "## 主暂停口径分段",
        "",
        segmented[
            [
                "window",
                "executed",
                "closed",
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
        "## 判断",
        "",
        "1. 如果系统性暂停在资金占用曲线里仍改善回撤，才说明它不只是事件曲线上的统计幻觉。",
        "2. 这一步仍未处理盘中止损、涨跌停成交、真实滑点和持仓市值波动，不能作为正式实盘结论。",
    ]
    (out_dir / "capital_curve_report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "input_dir": str(input_dir),
        "output_dir": str(out_dir),
        "initial_capital": args.initial_capital,
        "summary_rows": int(len(raw)),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
