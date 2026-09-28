from __future__ import annotations

"""Fixed-sequence stop sensitivity for the accepted 10:30 breakout mode."""

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_breakout_1030_execution_proxy_v1 import _load_inputs, _metrics, _simulate
from utils.paths import report_path


OUT_DIR = report_path("breakout_1030_stop_sensitivity_v1")
STOP_PCTS = [0.03, 0.04, 0.05]
RECENT_START, RECENT_END = pd.Timestamp("2025-05-16"), pd.Timestamp("2026-05-15")


def _realised_two_slot_curve(closed: pd.DataFrame) -> pd.DataFrame:
    """A conservative realised-PnL two-slot proxy; it does not mark open bars."""
    if closed.empty:
        return pd.DataFrame()
    d = closed.copy(); d["entry_date"] = pd.to_datetime(d["entry_date"]).dt.normalize(); d["exit_date"] = pd.to_datetime(d["exit_date"]).dt.normalize()
    days = pd.date_range(d["entry_date"].min(), d["exit_date"].max(), freq="D")
    cash, open_pos, rows = 1_000_000.0, [], []
    entries = {k: v.to_dict("records") for k, v in d.groupby("entry_date")}
    for day in days:
        still = []
        for pos in open_pos:
            if pos["exit_date"] <= day:
                cash += pos["stake"] * (1.0 + float(pos["net_ret"]))
            else:
                still.append(pos)
        open_pos = still
        for item in entries.get(day, []):
            if len(open_pos) >= 2:
                continue
            equity = cash + sum(x["stake"] for x in open_pos)
            stake = equity * .50
            if cash >= stake:
                cash -= stake
                open_pos.append({"exit_date": item["exit_date"], "net_ret": item["net_ret"], "stake": stake})
        equity = cash + sum(x["stake"] for x in open_pos)
        rows.append({"date": day, "equity_realised_proxy": equity, "open_positions": len(open_pos)})
    curve = pd.DataFrame(rows)
    curve["peak"] = curve["equity_realised_proxy"].cummax(); curve["drawdown"] = curve["equity_realised_proxy"] / curve["peak"] - 1.0
    return curve


def _row(closed: pd.DataFrame, scope: str, stop_pct: float) -> dict[str, object]:
    metric = _metrics(closed)
    curve = _realised_two_slot_curve(closed)
    initial = float(curve.iloc[0]["equity_realised_proxy"]) if not curve.empty else np.nan
    final = float(curve.iloc[-1]["equity_realised_proxy"]) if not curve.empty else np.nan
    metric.update({"scope": scope, "stop_pct": stop_pct, "portfolio_return_realised_proxy": final / initial - 1.0 if initial else np.nan, "max_drawdown_realised_proxy": float(curve["drawdown"].min()) if not curve.empty else np.nan, "portfolio_final_equity": final})
    return metric


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    entries, mpaths, paths = _load_inputs()
    all_closed, rows, curves = [], [], []
    for stop_pct in STOP_PCTS:
        closed = pd.DataFrame([z for _, r in entries.iterrows() if (z := _simulate(r, mpaths.get(str(r["code"]), pd.DataFrame()), paths.get(str(r["code6"]), pd.DataFrame()), stop_pct=stop_pct)) is not None])
        closed["stop_pct"] = stop_pct
        all_closed.append(closed)
        recent = closed[closed["entry_date"].between(RECENT_START, RECENT_END)].copy()
        for scope, sample in [("recent_12m", recent), ("full", closed)]:
            rows.append(_row(sample, scope, stop_pct))
            curve = _realised_two_slot_curve(sample); curve["scope"] = scope; curve["stop_pct"] = stop_pct; curves.append(curve)
    summary = pd.DataFrame(rows)
    pd.concat(all_closed, ignore_index=True).to_csv(OUT_DIR / "closed_trades.csv", index=False, encoding="utf-8-sig")
    pd.concat(curves, ignore_index=True).to_csv(OUT_DIR / "realised_two_slot_curves.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "stop_sensitivity_summary.csv", index=False, encoding="utf-8-sig")
    meta = {"status": "completed", "generated_at": datetime.now().isoformat(timespec="seconds"), "live_eligible": False, "recent_window": "2025-05-16 to 2026-05-15 by entry date", "comparison": "fixed accepted-entry sequence; only stop percentage varies; +10% half take-profit and runner rule unchanged", "drawdown": "two-slot 50% realised-PnL proxy; open positions are carried at principal, so this is not intraday mark-to-market drawdown"}
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    report = "# 前高突破10:30模式：止损敏感性 v1\n\n只比较 3%、4%、5% 止损，其他规则不变。近一年按信号入场日为 2025-05-16 至 2026-05-15。组合回撤为两槽50%的**已实现净值代理**，不把未平仓浮动损益伪装成准确盘中回撤。\n\n" + summary.to_markdown(index=False) + "\n\n不因单一近一年窗口的较高收益而改变合同；需同时看全样本与近期窗口。\n"
    (OUT_DIR / "REPORT_CN.md").write_text(report, encoding="utf-8")
    print(json.dumps({**meta, "accepted_entries": len(entries)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
