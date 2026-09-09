from __future__ import annotations

"""Tight-stop test for native Score120 mainwave signals, entry logic fixed."""

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.audit_score120_exit_payoff_contracts_v1 as base
from utils.paths import report_path


OUT_DIR = report_path("score120_tight_stop_payoff_v1")
SCOPES = [("recent_12m", "2025-05-16", "2026-05-15"), ("full", "2020-01-01", "2026-05-15")]
SPECS = [
    {"contract": f"hs{n}_tp10_half_be", "hard_stop": n / 100, "take_profit": .10, "sell_ratio": .50, "after_tp_stop": 0.00}
    for n in (3, 4, 5, 8, 10, 12)
]


def _curve(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    d = trades.copy(); d["entry_date"] = pd.to_datetime(d["entry_date"]).dt.normalize(); d["exit_date"] = pd.to_datetime(d["final_exit_date"]).dt.normalize()
    rows, cash, open_pos = [], 1_000_000.0, []
    by_entry = {k: v.sort_values(["rank_key", "amount_rank"], ascending=False).to_dict("records") for k, v in d.groupby("entry_date")}
    for day in pd.date_range(d["entry_date"].min(), d["exit_date"].max(), freq="D"):
        remain = []
        for pos in open_pos:
            if pos["exit_date"] <= day:
                cash += pos["stake"] * (1.0 + float(pos["contract_net_ret"]))
            else:
                remain.append(pos)
        open_pos = remain
        opened = 0
        for item in by_entry.get(day, []):
            if opened >= 2 or len(open_pos) >= 2:
                break
            equity = cash + sum(p["stake"] for p in open_pos); stake = equity * .5
            if cash >= stake:
                cash -= stake; open_pos.append({"stake": stake, "exit_date": item["exit_date"], "contract_net_ret": item["contract_net_ret"]}); opened += 1
        equity = cash + sum(p["stake"] for p in open_pos)
        rows.append({"date": day, "equity_realised_proxy": equity, "open_positions": len(open_pos)})
    out = pd.DataFrame(rows); out["peak"] = out["equity_realised_proxy"].cummax(); out["drawdown"] = out["equity_realised_proxy"] / out["peak"] - 1.0
    return out


def _metrics(trades: pd.DataFrame, scope: str, contract: str) -> dict[str, object]:
    r = pd.to_numeric(trades["contract_net_ret"], errors="coerce").dropna(); w, l = r[r > 0], r[r <= 0]
    curve = _curve(trades)
    initial, final = float(curve.iloc[0]["equity_realised_proxy"]), float(curve.iloc[-1]["equity_realised_proxy"])
    return {"scope": scope, "contract": contract, "trades": len(r), "win_rate": (r > 0).mean(), "expectation": r.mean(), "avg_win": w.mean() if len(w) else np.nan, "avg_loss": l.mean() if len(l) else np.nan, "payoff_ratio": w.mean() / abs(l.mean()) if len(w) and len(l) else np.nan, "worst_trade": r.min(), "stop_rate": trades["exit_reason"].astype(str).eq("hard_stop").mean(), "tp_rate": trades["touched_take_profit"].mean(), "portfolio_return_realised_proxy": final / initial - 1.0, "max_drawdown_realised_proxy": curve["drawdown"].min()}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_trades, rows, curves = [], [], []
    for scope, start, end in SCOPES:
        signals = base._load_signals(start, end); daily = base._load_daily(signals); by_code = {str(k): v for k, v in daily.groupby("code")}
        for spec in SPECS:
            result = []
            for _, row in signals.iterrows():
                bars = by_code.get(str(row["code_raw"]), pd.DataFrame())
                if not bars.empty:
                    bars = bars[(bars["trade_date"] >= row["entry_date"]) & (bars["trade_date"] <= row["policy_exit_date"])]
                result.append(base._simulate_trade(row, bars, spec))
            trades = pd.DataFrame(result); trades["scope"] = scope; all_trades.append(trades)
            rows.append(_metrics(trades, scope, spec["contract"]))
            curve = _curve(trades); curve["scope"] = scope; curve["contract"] = spec["contract"]; curves.append(curve)
    summary = pd.DataFrame(rows)
    pd.concat(all_trades, ignore_index=True).to_csv(OUT_DIR / "contract_trades.csv", index=False, encoding="utf-8-sig")
    pd.concat(curves, ignore_index=True).to_csv(OUT_DIR / "realised_two_slot_curves.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "tight_stop_summary.csv", index=False, encoding="utf-8-sig")
    meta = {"status": "completed", "generated_at": datetime.now().isoformat(timespec="seconds"), "live_eligible": False, "entry": "native Score120 + sector diffusion + 30m MA20 source; entry unchanged", "comparison": "only hard stop 3/4/5/8/10/12 varies; +10% sell half then the runner is protected at breakeven", "drawdown": "two-slot 50% realised-PnL proxy, not mark-to-market", "limitation": "native source has 46 full-history signals and only a small recent sample; daily OHLC stop-first proxy; breakeven is a lower bound, not yet the native 30m previous-low execution replay"}
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "REPORT_CN.md").write_text("# 机构主升：小亏大赚止损审计 v1\n\n固定主升原生入场，只收紧硬止损；+10%减半，余仓抬到成本线保护。\n\n" + summary.to_markdown(index=False) + "\n", encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
