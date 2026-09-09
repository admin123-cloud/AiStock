from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[1]))

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.market_warehouse import clickhouse_query_df  # noqa: E402


DEFAULT_CANDIDATES = REPO_ROOT / "reports" / "gen3_four_path_independent_candidates_sample800" / "g3_daily_candidates.parquet"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen3_four_path_independent_candidates_sample800" / "intraday_confirm_v1"
HORIZONS = (1, 2, 3, 5, 10, 20)
WINDOWS = {
    "train": ("2020-01-01", "2023-12-31"),
    "valid": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-12-31"),
    "full": ("1900-01-01", "2999-12-31"),
}


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if pd.isna(value):
        return None
    return str(value)


from research.common.reporting import percent_text as _pct


def _load_candidates(path: Path) -> pd.DataFrame:
    d = pd.read_parquet(path)
    for col in ["trade_date", "entry_date"]:
        d[col] = pd.to_datetime(d[col], errors="coerce").dt.strftime("%Y-%m-%d")
    return d.dropna(subset=["entry_date", "code", "g3_chain"]).copy()


def _load_minute_bars(candidates: pd.DataFrame, period: int) -> pd.DataFrame:
    table = {15: "kline_minute_15", 30: "kline_minute_30"}[int(period)]
    codes = sorted(candidates["code"].dropna().astype(str).unique().tolist())
    entry_dates = sorted(candidates["entry_date"].dropna().astype(str).unique().tolist())
    parts: list[pd.DataFrame] = []
    date_chunk_size = 80
    for di in range(0, len(entry_dates), date_chunk_size):
        date_chunk = entry_dates[di : di + date_chunk_size]
        date_list = ", ".join(f"toDate('{date}')" for date in date_chunk)
        for i in range(0, len(codes), 250):
            batch = codes[i : i + 250]
            quoted = ", ".join(f"'{code}'" for code in batch)
            part = clickhouse_query_df(
                f"""
                SELECT code, datetime, open, high, low, close, volume, amount
                FROM {table}
                WHERE code IN ({quoted})
                  AND toDate(datetime) IN ({date_list})
                ORDER BY code, datetime
                """
            )
            if not part.empty:
                parts.append(part)
    bars = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if bars.empty:
        return bars
    bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
    bars["entry_date"] = bars["datetime"].dt.strftime("%Y-%m-%d")
    bars["bar_time"] = bars["datetime"].dt.strftime("%H:%M:%S")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    bars = bars.dropna(subset=["code", "datetime", "entry_date", "open", "high", "low", "close"])
    bars = bars.sort_values(["code", "entry_date", "datetime"]).reset_index(drop=True)
    g = bars.groupby(["code", "entry_date"], sort=False)
    bars["prev_bar_high"] = g["high"].shift(1)
    bars["prev_bar_low"] = g["low"].shift(1)
    bars["intraday_high_so_far"] = g["high"].cummax()
    bars["intraday_low_so_far"] = g["low"].cummin()
    bars["open_range_high"] = g["high"].transform(lambda s: s.expanding().max()).where(bars["bar_time"] <= "10:30:00")
    bars["open_range_high"] = g["open_range_high"].ffill()
    bars["amount_ma3_prev"] = g["amount"].transform(lambda s: s.shift(1).rolling(3, min_periods=1).mean())
    bars["minute_day_close"] = g["close"].transform("last")
    bar_range = bars["high"] - bars["low"]
    bars["bar_close_pos"] = np.where(bar_range > 0, (bars["close"] - bars["low"]) / bar_range, np.nan)
    bars["bar_ret"] = bars["close"] / bars["open"] - 1.0
    bars["amount_ratio3"] = bars["amount"] / bars["amount_ma3_prev"]
    return bars.replace([np.inf, -np.inf], np.nan)


def _confirm_signals(candidates: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    context_cols = [
        "code",
        "entry_date",
        "g3_chain",
        "chain_rank",
        "candidate_score",
        "entry_weight_hint",
        "range_pos60",
        "drawdown10",
        "drawdown20",
        "up_rate",
        "breadth_ma20",
    ]
    ctx = candidates[[c for c in context_cols if c in candidates.columns]].copy()
    joined = bars.merge(ctx, on=["code", "entry_date"], how="inner")
    if joined.empty:
        return joined
    joined = joined[joined["bar_time"] >= "10:00:00"].copy()

    common_reversal = (
        (joined["close"] > joined["open"])
        & (joined["bar_close_pos"] >= 0.58)
        & (joined["amount_ratio3"].fillna(1.0) >= 0.85)
    )
    break_prev = joined["close"] > joined["prev_bar_high"]
    break_open_range = joined["close"] > joined["open_range_high"]
    no_new_low = joined["low"] >= joined["intraday_low_so_far"].groupby([joined["code"], joined["entry_date"]]).shift(1).fillna(joined["low"])
    lower_reclaim = (joined["bar_close_pos"] >= 0.70) & (joined["bar_ret"] > -0.005)

    masks = {
        "weak_rebound_repair": common_reversal & (break_prev | break_open_range),
        "range_box_bottom": common_reversal & (break_prev | no_new_low),
        "downtrend_panic_capitulation": (lower_reclaim | break_prev) & (joined["amount_ratio3"].fillna(1.0) >= 1.0),
        "strong_trend_breakout": break_open_range & (joined["amount_ratio3"].fillna(1.0) >= 1.1),
    }
    parts: list[pd.DataFrame] = []
    for chain, mask in masks.items():
        d = joined[(joined["g3_chain"] == chain) & mask].copy()
        if d.empty:
            continue
        d = d.sort_values(["entry_date", "code", "datetime"]).groupby(["entry_date", "code", "g3_chain"], as_index=False).first()
        if chain == "weak_rebound_repair":
            d["confirm_rule"] = "30m_rebound_break_prev_or_open_range"
        elif chain == "range_box_bottom":
            d["confirm_rule"] = "30m_box_bottom_reversal"
        elif chain == "downtrend_panic_capitulation":
            d["confirm_rule"] = "30m_panic_reclaim"
        else:
            d["confirm_rule"] = "30m_open_range_breakout"
        parts.append(d)
    panic_mask = (
        joined["g3_chain"].astype(str).str.startswith("panic_")
        & ((lower_reclaim | break_prev | common_reversal) & (joined["amount_ratio3"].fillna(1.0) >= 0.85))
    )
    d = joined[panic_mask].copy()
    if not d.empty:
        d = d.sort_values(["entry_date", "code", "datetime"]).groupby(["entry_date", "code", "g3_chain"], as_index=False).first()
        d["confirm_rule"] = "30m_panic_variant_reclaim"
        parts.append(d)
    if not parts:
        return pd.DataFrame(
            columns=[
                "entry_date",
                "code",
                "g3_chain",
                "chain_rank",
                "candidate_score",
                "entry_weight_hint",
                "confirm_datetime",
                "entry_price",
                "confirm_rule",
            ]
        )
    out = pd.concat(parts, ignore_index=True)
    out = out.rename(
        columns={
            "datetime": "confirm_datetime",
            "close": "entry_price",
            "open": "confirm_open",
            "high": "confirm_high",
            "low": "confirm_low",
            "amount": "confirm_amount",
        }
    )
    keep = [
        "entry_date",
        "code",
        "g3_chain",
        "chain_rank",
        "candidate_score",
        "entry_weight_hint",
        "confirm_datetime",
        "entry_price",
        "confirm_open",
        "confirm_high",
        "confirm_low",
        "confirm_amount",
        "bar_time",
        "bar_close_pos",
        "bar_ret",
        "amount_ratio3",
        "minute_day_close",
        "confirm_rule",
    ]
    return out[[c for c in keep if c in out.columns]].sort_values(["entry_date", "g3_chain", "confirm_datetime", "chain_rank"]).reset_index(drop=True)


def _load_daily_exits(signals: pd.DataFrame) -> pd.DataFrame:
    if signals.empty or "code" not in signals.columns:
        return pd.DataFrame()
    codes = sorted(signals["code"].dropna().astype(str).unique().tolist())
    if not codes:
        return pd.DataFrame()
    start_date = str(signals["entry_date"].min())
    end_date = (pd.Timestamp(signals["entry_date"].max()) + pd.Timedelta(days=max(HORIZONS) * 3 + 15)).strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 500):
        batch = codes[i : i + 500]
        quoted = ", ".join(f"'{code}'" for code in batch)
        part = clickhouse_query_df(
            f"""
            SELECT code, trade_date, close
            FROM kline_daily
            WHERE code IN ({quoted})
              AND trade_date BETWEEN toDate(%(start_date)s) AND toDate(%(end_date)s)
            ORDER BY code, trade_date
            """,
            {"start_date": start_date, "end_date": end_date},
        )
        parts.append(part)
    daily = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if daily.empty:
        return daily
    daily["trade_date"] = pd.to_datetime(daily["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    daily["close"] = pd.to_numeric(daily["close"], errors="coerce")
    daily = daily.dropna(subset=["code", "trade_date", "close"]).sort_values(["code", "trade_date"]).reset_index(drop=True)
    g = daily.groupby("code", sort=False)
    for h in HORIZONS:
        daily[f"exit_close_{h}d"] = g["close"].shift(-h + 1)
    return daily


def _label_signals(signals: pd.DataFrame) -> pd.DataFrame:
    if signals.empty:
        return signals.copy()
    daily = _load_daily_exits(signals)
    if daily.empty or signals.empty:
        return signals
    cols = ["code", "trade_date", "close"] + [f"exit_close_{h}d" for h in HORIZONS]
    out = signals.merge(daily[cols].rename(columns={"trade_date": "entry_date"}), on=["code", "entry_date"], how="left")
    out = out.rename(columns={"close": "daily_entry_close"})
    scale = pd.to_numeric(out["daily_entry_close"], errors="coerce") / pd.to_numeric(out["minute_day_close"], errors="coerce")
    out["price_scale_daily_to_minute"] = scale.replace([np.inf, -np.inf], np.nan)
    out["entry_price_adjusted"] = out["entry_price"] * out["price_scale_daily_to_minute"]
    for h in HORIZONS:
        out[f"fwd_ret_confirm_to_close_{h}d"] = out[f"exit_close_{h}d"] / out["entry_price_adjusted"] - 1.0
    return out


def _summarize_group(group: pd.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {
        "signals": int(len(group)),
        "days": int(group["entry_date"].nunique()) if "entry_date" in group else 0,
        "unique_codes": int(group["code"].nunique()) if "code" in group else 0,
    }
    for h in HORIZONS:
        col = f"fwd_ret_confirm_to_close_{h}d"
        s = pd.to_numeric(group[col], errors="coerce").dropna() if col in group else pd.Series(dtype=float)
        out[f"n_{h}d"] = int(len(s))
        out[f"mean_{h}d"] = float(s.mean()) if len(s) else None
        out[f"median_{h}d"] = float(s.median()) if len(s) else None
        out[f"win_{h}d"] = float((s > 0).mean()) if len(s) else None
    return out


def _build_summary(labeled: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for window, (start, end) in WINDOWS.items():
        w = labeled[(labeled["entry_date"] >= start) & (labeled["entry_date"] <= end)].copy()
        if w.empty:
            continue
        for chain, group in w.groupby("g3_chain", sort=True):
            item = {"window": window, "g3_chain": chain}
            item.update(_summarize_group(group))
            rows.append(item)
        item = {"window": window, "g3_chain": "ALL"}
        item.update(_summarize_group(w))
        rows.append(item)
    return pd.DataFrame(rows)


def _format_summary(raw: pd.DataFrame) -> pd.DataFrame:
    d = raw.copy()
    for h in HORIZONS:
        for key in ["mean", "median", "win"]:
            col = f"{key}_{h}d"
            if col in d:
                d[col] = d[col].map(_pct)
    return d


def _write_report(output_dir: Path, candidates_path: Path, candidates: pd.DataFrame, labeled: pd.DataFrame, raw: pd.DataFrame, display: pd.DataFrame) -> None:
    total_candidates = len(candidates)
    total_signals = len(labeled)
    confirm_rate = total_signals / total_candidates if total_candidates else 0.0
    lines: list[str] = [
        "# G3 30m 确认层验证 V1",
        "",
        "## 口径",
        "",
        f"- 候选源：`{candidates_path}`",
        "- 日线候选只负责生成候选；30m 确认负责决定是否入场。",
        "- 入场价：确认 30m bar 收盘价；退出标签：持有 1/2/3/5/10/20 个交易日后的日线收盘价。",
        "- 由于本地分钟线和日线可能存在复权口径差异，入场价先按 `买入日日线收盘 / 买入日最后一根分钟收盘` 校准到日线口径。",
        "- 当前仍是标签验证，不含手续费、滑点、涨跌停无法成交、仓位冲突和盘中止损。",
        "",
        "## 确认覆盖",
        "",
        f"- 日线候选：`{total_candidates}`",
        f"- 30m 确认信号：`{total_signals}`",
        f"- 确认率：`{_pct(confirm_rate)}`",
        "",
        "## 汇总表",
        "",
    ]
    if display.empty:
        lines.append("无确认信号。")
    else:
        cols = [
            "window",
            "g3_chain",
            "signals",
            "days",
            "unique_codes",
            "mean_2d",
            "median_2d",
            "win_2d",
            "mean_3d",
            "median_3d",
            "win_3d",
            "mean_5d",
            "median_5d",
            "win_5d",
            "mean_10d",
            "median_10d",
            "win_10d",
        ]
        lines.append(display[[c for c in cols if c in display.columns]].to_markdown(index=False))
    lines.extend(
        [
            "",
            "## 初步观察",
            "",
            "- 如果 30m 确认后短周期收益改善，说明原思路更适合“等待反抽确认后短打”。",
            "- 如果确认后样本大幅减少但收益没有改善，说明日线候选定义本身还需要重做，而不是继续加确认条件。",
            "",
        ]
    )
    output_dir.joinpath("intraday_confirm_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    candidates_path = Path(args.candidates)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates = _load_candidates(candidates_path)
    if args.exclude_strong:
        candidates = candidates[candidates["g3_chain"] != "strong_trend_breakout"].copy()
    if args.chains:
        wanted = {item.strip() for item in str(args.chains).split(",") if item.strip()}
        candidates = candidates[candidates["g3_chain"].isin(wanted)].copy()
    bars = _load_minute_bars(candidates, period=int(args.period))
    confirmed = _confirm_signals(candidates, bars)
    labeled = _label_signals(confirmed)
    raw = _build_summary(labeled)
    display = _format_summary(raw)

    confirmed.to_parquet(output_dir / "confirmed_signals.parquet", index=False)
    confirmed.to_csv(output_dir / "confirmed_signals.csv", index=False, encoding="utf-8-sig")
    labeled.to_parquet(output_dir / "labeled_confirmed_signals.parquet", index=False)
    labeled.to_csv(output_dir / "labeled_confirmed_signals.csv", index=False, encoding="utf-8-sig")
    raw.to_csv(output_dir / "forward_summary_raw.csv", index=False, encoding="utf-8-sig")
    display.to_csv(output_dir / "forward_summary_display.csv", index=False, encoding="utf-8-sig")
    result = {
        "candidates_path": str(candidates_path),
        "output_dir": str(output_dir),
        "period": int(args.period),
        "exclude_strong": bool(args.exclude_strong),
        "chains": str(args.chains or ""),
        "candidate_rows": int(len(candidates)),
        "confirmed_rows": int(len(confirmed)),
        "confirm_rate": float(len(confirmed) / len(candidates)) if len(candidates) else None,
        "summary_rows": int(len(raw)),
    }
    (output_dir / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_report(output_dir, candidates_path, candidates, labeled, raw, display)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate G3 candidates with intraday confirmation.")
    parser.add_argument("--candidates", default=str(DEFAULT_CANDIDATES))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--period", type=int, choices=[15, 30], default=30)
    parser.add_argument("--exclude-strong", action="store_true", help="Focus on weak/range/panic chains.")
    parser.add_argument("--chains", default="", help="Comma separated g3_chain values to include.")
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
