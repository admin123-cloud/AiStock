from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import numpy as np

PROJECT_ROOT = _PROJECT_ROOT
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.market_warehouse import clickhouse_query_df
from utils.paths import report_path


TRADE_FILE = Path("\u0044:/\u540c\u82b1\u987a/1\u5e74.txt")
OUT_DIR = report_path("live_trade_15m_rsi_t_overlay")
BOX_OUT_DIR = report_path("live_trade_15m_rsi_t_box_overlay")


@dataclass
class Lot:
    shares: int
    price: float
    dt: datetime


def _safe_float(value: Any) -> float:
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return 0.0
        text = str(value).strip().replace(",", "")
        return float(text) if text else 0.0
    except Exception:
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(round(_safe_float(value)))
    except Exception:
        return 0


def load_trades(path: Path) -> pd.DataFrame:
    rows: list[list[str]] = []
    header: list[str] | None = None
    raw = path.read_bytes()
    text = None
    for encoding in ("utf-8-sig", "gb18030", "gbk"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    source_order = 0
    for line in text.splitlines():
        if "\t" not in line:
            continue
        parts = line.rstrip("\t").split("\t")
        if parts and parts[0] == "成交日期":
            header = parts
            continue
        if header and parts and re.fullmatch(r"\d{8}", parts[0] or ""):
            if len(parts) < len(header):
                parts += [""] * (len(header) - len(parts))
            rows.append(parts[: len(header)] + [source_order])
            source_order += 1
    if not header:
        raise RuntimeError(f"未找到成交表头: {path}")
    df = pd.DataFrame(rows, columns=header + ["source_order"])
    df["date"] = pd.to_datetime(df["成交日期"], format="%Y%m%d", errors="coerce")
    df["code"] = df["证券代码"].astype(str).str.extract(r"(\d{6})", expand=False).fillna("")
    df["name"] = df["证券名称"].astype(str).str.strip()
    df["op"] = df["操作"].astype(str).str.strip()
    df["qty"] = df["成交数量"].map(_safe_int).abs()
    df["signed_qty"] = df.apply(
        lambda r: r["qty"] if r["op"] == "证券买入" else (-r["qty"] if r["op"] == "证券卖出" else 0),
        axis=1,
    )
    for col in ["成交均价", "成交金额", "发生金额", "印花税", "佣金", "过户费", "经手费", "证管费"]:
        df[col] = df[col].map(_safe_float)
    df = df[df["date"].notna()].copy()
    # The broker export is newest-first. Reverse original order to preserve same-day
    # sequencing because no intraday fill time is exported.
    df = df.sort_values("source_order", ascending=False, kind="stable").reset_index(drop=True)
    return df


def build_cycles(df: pd.DataFrame) -> pd.DataFrame:
    trades = df[(df["code"] != "") & (df["op"].isin(["证券买入", "证券卖出"]))].copy()
    cycles: list[dict[str, Any]] = []
    for code, g in trades.groupby("code", sort=False):
        pos = 0
        cycle_rows: list[int] = []
        seq = 0
        tainted = False
        for idx, row in g.iterrows():
            if pos == 0:
                cycle_rows = []
                seq += 1
                if row["op"] == "证券买入":
                    tainted = _safe_int(row.get("余额", 0)) > int(row["qty"])
                else:
                    tainted = True
            cycle_rows.append(idx)
            pos += int(row["signed_qty"])
            if pos <= 0 and cycle_rows:
                c = trades.loc[cycle_rows].copy()
                buys = c[c["op"] == "证券买入"]
                sells = c[c["op"] == "证券卖出"]
                if not buys.empty and not sells.empty:
                    gross_buy = float(buys["成交金额"].sum())
                    gross_sell = float(sells["成交金额"].sum())
                    actual_cash = float(c["发生金额"].sum())
                    cycles.append(
                        {
                            "cycle_id": f"{code}-{seq}",
                            "code": code,
                            "name": c["name"].replace("", pd.NA).dropna().iloc[-1] if c["name"].replace("", pd.NA).dropna().size else "",
                            "start_date": c["date"].min().date().isoformat(),
                            "end_date": c["date"].max().date().isoformat(),
                            "holding_days": int((c["date"].max().date() - c["date"].min().date()).days),
                            "buy_count": int(len(buys)),
                            "sell_count": int(len(sells)),
                            "buy_shares": int(buys["qty"].sum()),
                            "sell_shares": int(sells["qty"].sum()),
                            "gross_buy": gross_buy,
                            "gross_sell": gross_sell,
                            "actual_cash_pnl": actual_cash,
                            "actual_gross_pnl": gross_sell - gross_buy,
                            "actual_return": actual_cash / gross_buy if gross_buy else 0.0,
                            "tainted_opening_position": bool(tainted),
                            "row_indices": list(map(int, cycle_rows)),
                        }
                    )
                cycle_rows = []
                pos = 0
                tainted = False
        if cycle_rows:
            c = trades.loc[cycle_rows].copy()
            buys = c[c["op"] == "证券买入"]
            if not buys.empty:
                cycles.append(
                    {
                        "cycle_id": f"{code}-{seq}-open",
                        "code": code,
                        "name": c["name"].replace("", pd.NA).dropna().iloc[-1] if c["name"].replace("", pd.NA).dropna().size else "",
                        "start_date": c["date"].min().date().isoformat(),
                        "end_date": c["date"].max().date().isoformat(),
                        "holding_days": int((c["date"].max().date() - c["date"].min().date()).days),
                        "buy_count": int(len(buys)),
                        "sell_count": int((c["op"] == "证券卖出").sum()),
                        "buy_shares": int(buys["qty"].sum()),
                        "sell_shares": int((c[c["op"] == "证券卖出"]["qty"]).sum()),
                        "gross_buy": float(buys["成交金额"].sum()),
                        "gross_sell": float(c[c["op"] == "证券卖出"]["成交金额"].sum()),
                        "actual_cash_pnl": float(c["发生金额"].sum()),
                        "actual_gross_pnl": float(c[c["op"] == "证券卖出"]["成交金额"].sum() - buys["成交金额"].sum()),
                        "actual_return": 0.0,
                        "tainted_opening_position": bool(tainted),
                        "row_indices": list(map(int, cycle_rows)),
                    }
                )
    return pd.DataFrame(cycles)


def tdx_rsi(close: pd.Series, n: int = 6) -> pd.Series:
    diff = close.diff()
    up = diff.clip(lower=0)
    absdiff = diff.abs()
    # TDX SMA(X,N,1): Y=(X + (N-1)*Y')/N, equivalent to ewm alpha=1/N adjust=False.
    ma_up = up.ewm(alpha=1 / n, adjust=False).mean()
    ma_abs = absdiff.ewm(alpha=1 / n, adjust=False).mean()
    return (ma_up / ma_abs.replace(0, np.nan) * 100).astype(float)


def add_rsi_signals(k: pd.DataFrame) -> pd.DataFrame:
    k = k.sort_values("datetime").copy()
    k["rsi6"] = tdx_rsi(k["close"], 6)
    k["cross_down_80"] = (k["rsi6"].shift(1) > 80) & (k["rsi6"] <= 80)
    k["cross_up_20"] = (k["rsi6"].shift(1) < 20) & (k["rsi6"] >= 20)
    k["was_below_20"] = k["rsi6"].shift(1) < 20
    k["pivot_high"] = (
        (k["high"] > k["high"].shift(1))
        & (k["high"] > k["high"].shift(2))
        & (k["high"] >= k["high"].shift(-1))
        & (k["high"] >= k["high"].shift(-2))
    )
    piv = k[k["pivot_high"] & k["rsi6"].notna()].copy()
    div_idx: set[int] = set()
    prev: pd.Series | None = None
    for idx, row in piv.iterrows():
        if prev is not None and row["high"] > prev["high"] and row["rsi6"] < prev["rsi6"] and row["rsi6"] >= 65:
            div_idx.add(int(idx))
        prev = row
    k["bearish_divergence"] = [i in div_idx for i in k.index]
    box_window = 32
    prior_high = k["high"].shift(1).rolling(box_window, min_periods=16).max()
    prior_low = k["low"].shift(1).rolling(box_window, min_periods=16).min()
    box_mid = (prior_high + prior_low) / 2
    box_width = (prior_high - prior_low) / box_mid.replace(0, np.nan)
    slope = (k["close"] / k["close"].shift(box_window) - 1).abs()
    k["box_upper"] = prior_high
    k["box_lower"] = prior_low
    k["box_width_pct"] = box_width
    k["box_slope_abs"] = slope
    k["in_box_regime"] = (
        prior_high.notna()
        & prior_low.notna()
        & box_width.between(0.025, 0.18)
        & (slope <= 0.10)
        & (k["close"] <= prior_high * 1.005)
        & (k["close"] >= prior_low * 0.995)
    )
    k["box_breakout"] = (
        prior_high.notna()
        & prior_low.notna()
        & ((k["close"] > prior_high * 1.005) | (k["close"] < prior_low * 0.995))
    )
    return k


def fetch_15m(codes: list[str], start: date, end: date) -> pd.DataFrame:
    def market_code(code: str) -> str:
        if code.startswith(("0", "3")):
            return f"{code}.SZ"
        if code.startswith("6"):
            return f"{code}.SH"
        return f"{code}.BJ"

    parts: list[pd.DataFrame] = []
    for code in codes:
        q_code = market_code(code)
        sql = """
        SELECT code, datetime, open, high, low, close, volume, amount
        FROM kline_minute_15
        WHERE code = {code:String}
          AND datetime >= {start:DateTime}
          AND datetime <= {end:DateTime}
        ORDER BY datetime
        """
        df = clickhouse_query_df(
            sql,
            {
                "code": q_code,
                "start": datetime.combine(start - timedelta(days=20), datetime.min.time()),
                "end": datetime.combine(end + timedelta(days=2), datetime.max.time().replace(microsecond=0)),
            },
        )
        if not df.empty:
            df["code"] = code
            parts.append(df)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def _sell_lots(lots: list[Lot], shares: int, price: float, dt: datetime) -> tuple[float, int]:
    remaining = shares
    pnl = 0.0
    sold = 0
    while remaining > 0 and lots:
        lot = lots[0]
        take = min(lot.shares, remaining)
        pnl += (price - lot.price) * take
        lot.shares -= take
        remaining -= take
        sold += take
        if lot.shares == 0:
            lots.pop(0)
    return pnl, sold


def simulate_cycle(cycle: pd.Series, trade_rows: pd.DataFrame, k: pd.DataFrame, *, box_only: bool = False) -> dict[str, Any]:
    code = cycle["code"]
    rows = trade_rows.loc[cycle["row_indices"]].sort_values("date").copy()
    start_dt = pd.Timestamp(cycle["start_date"])
    end_dt = pd.Timestamp(cycle["end_date"]) + pd.Timedelta(hours=15)
    bars = k[(k["code"] == code) & (k["datetime"] >= start_dt) & (k["datetime"] <= end_dt)].copy()
    if bars.empty:
        return {"valid": False, "reason": "missing_15m"}
    bars = add_rsi_signals(bars).reset_index(drop=True)

    lots: list[Lot] = []
    reserve_lots: list[Lot] = []
    pnl = 0.0
    events: list[dict[str, Any]] = []
    below20_seen = False
    box_disabled = False
    box_signal_blocked = 0
    box_breakout_seen = False
    actual_by_date = {pd.Timestamp(d).date(): g for d, g in rows.groupby(rows["date"].dt.date)}

    for bar in bars.itertuples(index=False):
        bdt = pd.Timestamp(bar.datetime).to_pydatetime()
        bdate = pd.Timestamp(bar.datetime).date()
        if bdate in actual_by_date and bdt.hour == 9 and bdt.minute == 45:
            for _, r in actual_by_date[bdate].iterrows():
                if r["op"] == "证券买入":
                    lots.append(Lot(int(r["qty"]), float(r["成交均价"]), bdt))
                elif r["op"] == "证券卖出":
                    sell_shares = min(int(r["qty"]), sum(x.shares for x in lots))
                    if sell_shares > 0:
                        leg_pnl, sold = _sell_lots(lots, sell_shares, float(r["成交均价"]), bdt)
                        pnl += leg_pnl
                        events.append({"datetime": bdt.isoformat(" "), "action": "actual_sell", "shares": sold, "price": float(r["成交均价"])})

        pos = sum(x.shares for x in lots)
        if pos <= 0:
            continue

        if box_only and bool(bar.box_breakout):
            box_disabled = True
            box_breakout_seen = True
            events.append(
                {
                    "datetime": bdt.isoformat(" "),
                    "action": "box_breakout_disable_t",
                    "shares": 0,
                    "price": float(bar.close),
                    "box_upper": float(bar.box_upper) if pd.notna(bar.box_upper) else None,
                    "box_lower": float(bar.box_lower) if pd.notna(bar.box_lower) else None,
                }
            )

        allow_t = True
        if box_only:
            allow_t = (not box_disabled) and bool(bar.in_box_regime)

        if bool(bar.bearish_divergence):
            if not allow_t:
                box_signal_blocked += 1
                continue
            target = max(100, int((pos * 0.5) // 100 * 100))
            target = min(target, pos)
            leg_pnl, sold = _sell_lots(lots, target, float(bar.close), bdt)
            pnl += leg_pnl
            if sold:
                reserve_lots.append(Lot(sold, float(bar.close), bdt))
                events.append({"datetime": bdt.isoformat(" "), "action": "rsi_div_sell_half", "shares": sold, "price": float(bar.close), "rsi": float(bar.rsi6)})
            continue

        if bool(bar.cross_down_80):
            if not allow_t:
                box_signal_blocked += 1
                continue
            target = max(100, int((pos / 3) // 100 * 100))
            target = min(target, pos)
            leg_pnl, sold = _sell_lots(lots, target, float(bar.close), bdt)
            pnl += leg_pnl
            if sold:
                reserve_lots.append(Lot(sold, float(bar.close), bdt))
                events.append({"datetime": bdt.isoformat(" "), "action": "rsi80_cross_sell_part", "shares": sold, "price": float(bar.close), "rsi": float(bar.rsi6)})

        if bool(bar.was_below_20):
            below20_seen = True
        if below20_seen and bool(bar.cross_up_20) and reserve_lots:
            if box_only and (box_disabled or not bool(bar.in_box_regime)):
                box_signal_blocked += 1
                continue
            buy_back = sum(x.shares for x in reserve_lots)
            lots.append(Lot(buy_back, float(bar.close), bdt))
            events.append({"datetime": bdt.isoformat(" "), "action": "rsi20_cross_buyback_t", "shares": buy_back, "price": float(bar.close), "rsi": float(bar.rsi6)})
            reserve_lots = []
            below20_seen = False

    last_price = float(bars.iloc[-1]["close"])
    forced_shares = sum(x.shares for x in lots)
    if forced_shares:
        leg_pnl, sold = _sell_lots(lots, forced_shares, last_price, pd.Timestamp(bars.iloc[-1]["datetime"]).to_pydatetime())
        pnl += leg_pnl
        events.append({"datetime": str(bars.iloc[-1]["datetime"]), "action": "cycle_end_mark_sell", "shares": sold, "price": last_price})

    gross_buy = float(cycle["gross_buy"])
    return {
        "valid": True,
        "overlay_gross_pnl": pnl,
        "overlay_return": pnl / gross_buy if gross_buy else 0.0,
        "event_count": len([e for e in events if e["action"].startswith("rsi")]),
        "sell_signal_count": len([e for e in events if "sell" in e["action"] and e["action"].startswith("rsi")]),
        "buyback_signal_count": len([e for e in events if "buyback" in e["action"]]),
        "box_signal_blocked": int(box_signal_blocked),
        "box_breakout_seen": bool(box_breakout_seen),
        "events": events,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-file", type=Path, default=TRADE_FILE)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--box-only", action="store_true", help="只在箱体震荡内做T，跳出箱体后停止RSI做T")
    args = parser.parse_args()
    if args.out_dir is None:
        args.out_dir = BOX_OUT_DIR if args.box_only else OUT_DIR

    args.out_dir.mkdir(parents=True, exist_ok=True)
    df = load_trades(args.trade_file)
    cycles = build_cycles(df)
    closed = cycles[~cycles["cycle_id"].astype(str).str.endswith("-open")].copy()
    codes = sorted(closed["code"].unique().tolist())
    start = pd.to_datetime(closed["start_date"]).min().date()
    end = pd.to_datetime(closed["end_date"]).max().date()
    k = fetch_15m(codes, start, end)

    results: list[dict[str, Any]] = []
    all_events: list[dict[str, Any]] = []
    for _, cycle in closed.iterrows():
        sim = simulate_cycle(cycle, df, k, box_only=args.box_only)
        row = cycle.drop(labels=["row_indices"]).to_dict()
        row.update({k2: v for k2, v in sim.items() if k2 != "events"})
        if sim.get("valid"):
            if int(row.get("sell_signal_count", 0) or 0) + int(row.get("buyback_signal_count", 0) or 0) == 0:
                row["overlay_gross_pnl"] = float(row["actual_gross_pnl"])
                row["overlay_return"] = float(row["actual_return"])
            row["delta_gross_pnl"] = float(row["overlay_gross_pnl"]) - float(row["actual_gross_pnl"])
            row["delta_return"] = float(row["overlay_return"]) - float(row["actual_return"])
            for e in sim.get("events", []):
                e = dict(e)
                e["cycle_id"] = cycle["cycle_id"]
                e["code"] = cycle["code"]
                e["name"] = cycle["name"]
                all_events.append(e)
        results.append(row)

    out = pd.DataFrame(results)
    valid = out[out["valid"] == True].copy()  # noqa: E712
    clean_valid = valid[valid["tainted_opening_position"] == False].copy() if not valid.empty else valid

    def metrics(frame: pd.DataFrame) -> dict[str, Any]:
        if frame.empty:
            return {
                "cycles": 0,
                "actual_gross_pnl": 0.0,
                "overlay_gross_pnl": 0.0,
                "delta_gross_pnl": 0.0,
                "gross_buy": 0.0,
                "actual_return_on_buy": 0.0,
                "overlay_return_on_buy": 0.0,
                "cycles_improved": 0,
                "cycles_worse": 0,
            }
        gross_buy = float(frame["gross_buy"].sum())
        actual = float(frame["actual_gross_pnl"].sum())
        overlay = float(frame["overlay_gross_pnl"].sum())
        return {
            "cycles": int(len(frame)),
            "actual_gross_pnl": actual,
            "overlay_gross_pnl": overlay,
            "delta_gross_pnl": overlay - actual,
            "actual_cash_pnl_reference": float(frame["actual_cash_pnl"].sum()),
            "gross_buy": gross_buy,
            "actual_return_on_buy": actual / gross_buy if gross_buy else 0.0,
            "overlay_return_on_buy": overlay / gross_buy if gross_buy else 0.0,
            "cycles_improved": int((frame["delta_gross_pnl"] > 0).sum()),
            "cycles_worse": int((frame["delta_gross_pnl"] < 0).sum()),
            "median_delta_per_cycle": float(frame["delta_gross_pnl"].median()),
            "avg_delta_per_cycle": float(frame["delta_gross_pnl"].mean()),
            "rsi_sell_signals": int(frame["sell_signal_count"].sum()),
            "rsi_buyback_signals": int(frame["buyback_signal_count"].sum()),
        }

    summary = {
        "trade_file": str(args.trade_file),
        "box_only": bool(args.box_only),
        "cycles_total": int(len(out)),
        "valid_cycles": int(len(valid)),
        "clean_valid_cycles": int(len(clean_valid)),
        "tainted_valid_cycles": int((valid["tainted_opening_position"] == True).sum()) if not valid.empty else 0,  # noqa: E712
        "coverage": float(len(valid) / len(out)) if len(out) else 0.0,
        "all_valid_metrics": metrics(valid),
        "clean_valid_metrics": metrics(clean_valid),
    }
    if not valid.empty:
        summary["top_improvement"] = valid.sort_values("delta_gross_pnl", ascending=False).head(10)[
            ["cycle_id", "code", "name", "start_date", "end_date", "actual_gross_pnl", "overlay_gross_pnl", "delta_gross_pnl", "sell_signal_count", "buyback_signal_count"]
        ].to_dict(orient="records")
        summary["top_worse"] = valid.sort_values("delta_gross_pnl", ascending=True).head(10)[
            ["cycle_id", "code", "name", "start_date", "end_date", "actual_gross_pnl", "overlay_gross_pnl", "delta_gross_pnl", "sell_signal_count", "buyback_signal_count"]
        ].to_dict(orient="records")

    out.to_csv(args.out_dir / "cycle_comparison.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(all_events).to_csv(args.out_dir / "rsi_overlay_events.csv", index=False, encoding="utf-8-sig")
    (args.out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
