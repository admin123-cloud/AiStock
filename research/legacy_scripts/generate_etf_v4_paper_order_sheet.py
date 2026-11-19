"""Generate a paper target-share sheet for v4; never sends orders."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _latest_price(symbol: str) -> tuple[str, float]:
    d = yf.download(symbol, period="10d", auto_adjust=False, progress=False, threads=False)
    if d.empty:
        raise RuntimeError(f"no latest raw price for {symbol}")
    d.columns = [c[0] if isinstance(c, tuple) else c for c in d.columns]
    return str(d.index[-1].date()), float(d["Close"].iloc[-1])


def run(args: argparse.Namespace) -> dict:
    history = json.loads(Path(args.ledger).read_text(encoding="utf-8"))
    if not history:
        raise ValueError("paper ledger is empty")
    latest = history[-1]
    rows = []
    symbols = latest["symbols"]
    for symbol in symbols:
        obs = latest["observations"][symbol]["v4_ma40_250_entry_fixed"]
        price_date, price = _latest_price(symbol)
        scale = float(obs.get("held_position_scale", 0.0)) if obs.get("liquidity_gate", False) else 0.0
        target_value = args.capital * scale / len(symbols)
        shares = int(max(target_value, 0.0) // (price * args.lot)) * args.lot
        rows.append({
            "symbol": symbol,
            "price_date": price_date,
            "reference_price": price,
            "signal_action": obs.get("action"),
            "liquidity_gate": bool(obs.get("liquidity_gate", False)),
            "held_position_scale": scale,
            "target_value": target_value,
            "paper_target_shares": shares,
            "paper_target_value": shares * price,
        })
    invested = sum(r["paper_target_value"] for r in rows)
    result = {
        "research_only": True,
        "paper_only": True,
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "as_of": latest["as_of"],
        "capital": args.capital,
        "lot": args.lot,
        "cash_estimate": args.capital - invested,
        "rows": rows,
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--ledger", required=True)
    p.add_argument("--capital", type=float, default=500000)
    p.add_argument("--lot", type=int, default=100)
    p.add_argument("--output", required=True)
    print(json.dumps(run(p.parse_args()), ensure_ascii=False, indent=2))
