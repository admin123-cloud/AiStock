from __future__ import annotations

"""Research-only comparison of MA exits for the runner after Score120 takes half profit."""

import json
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_query_df, clickhouse_table_exists
from utils.paths import report_path


SOURCE = report_path("gen3_score120_formal_institutional_source_v1", "closed_trades.csv")
OUT_DIR = report_path("formal26_profit_protection_ma_exits_v1")
TP = 0.12


def _query(table: str, codes: list[str], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if not clickhouse_table_exists(table):
        return pd.DataFrame()
    parts: list[pd.DataFrame] = []
    for offset in range(0, len(codes), 80):
        quoted = ",".join("'" + code.replace("'", "''") + "'" for code in codes[offset : offset + 80])
        time_col = "trade_date" if table == "kline_daily" else "datetime"
        sql = f"""
        SELECT code, {time_col}, open, high, low, close
        FROM {table}
        WHERE code IN ({quoted})
          AND {time_col} >= {'toDate' if table == 'kline_daily' else 'toDateTime'}('{start:%Y-%m-%d} 00:00:00')
          AND {time_col} <= {'toDate' if table == 'kline_daily' else 'toDateTime'}('{end:%Y-%m-%d} 23:59:59')
        ORDER BY code, {time_col}
        """
        part = clickhouse_query_df(sql)
        if not part.empty:
            parts.append(part)
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if out.empty:
        return out
    stamp = "trade_date" if table == "kline_daily" else "datetime"
    out[stamp] = pd.to_datetime(out[stamp], errors="coerce")
    for col in ["open", "high", "low", "close"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.dropna(subset=["code", stamp, "open", "high", "low", "close"]).sort_values(["code", stamp])


def _load() -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    trades = pd.read_csv(SOURCE, encoding="utf-8-sig", low_memory=False)
    trades = trades[trades["mode"].fillna("").eq("institutional_mainwave")].copy()
    for col in ["entry_datetime", "exit_datetime", "entry_date", "policy_exit_date"]:
        trades[col] = pd.to_datetime(trades[col], errors="coerce")
    trades["entry_price"] = pd.to_numeric(trades["entry_price"], errors="coerce")
    trades["net_ret"] = pd.to_numeric(trades["net_ret"], errors="coerce")
    trades = trades.dropna(subset=["code", "entry_datetime", "exit_datetime", "entry_price", "net_ret"]).copy()
    codes = sorted(trades["code"].astype(str).unique())
    start = trades["entry_datetime"].min().normalize() - pd.Timedelta(days=30)
    end = trades["exit_datetime"].max().normalize() + pd.Timedelta(days=3)
    bars = {
        "d1": _query("kline_daily", codes, start, end),
        "m30": _query("kline_minute_30", codes, start, end),
        "m15": _query("kline_minute_15", codes, start, end),
    }
    return trades, bars


def _runner_start(row: pd.Series, bars30: pd.DataFrame) -> pd.Timestamp | None:
    if "take_profit_partial_30m" not in str(row["exit_reason"]):
        return None
    candidate = bars30[(bars30["datetime"] >= row["entry_datetime"]) & (bars30["datetime"] < row["exit_datetime"])].copy()
    hit = candidate[candidate["high"] >= float(row["entry_price"]) * (1.0 + TP)]
    return pd.Timestamp(hit.iloc[0]["datetime"]) if not hit.empty else None


def _ma_exit(row: pd.Series, bars: pd.DataFrame, kind: str, runner_start: pd.Timestamp | None) -> tuple[pd.Timestamp | None, float | None, str]:
    if runner_start is None or bars.empty:
        return None, None, "not_armed_or_missing_bars"
    stamp = "trade_date" if kind == "d1" else "datetime"
    work = bars[(bars[stamp] >= row["entry_datetime"].normalize()) & (bars[stamp] < row["exit_datetime"])].copy()
    if work.empty:
        return None, None, "no_bars_before_original_exit"
    work["ma"] = work["close"].rolling(5 if kind == "d1" else 20, min_periods=5 if kind == "d1" else 20).mean()
    armed = work[work[stamp] > runner_start].copy()
    if kind == "d1":
        hit_index = armed.index[(armed["close"] < armed["ma"]) & armed["ma"].notna()]
    else:
        below = (armed["close"] < armed["ma"]) & armed["ma"].notna()
        hit_index = armed.index[below & below.shift(1, fill_value=False)]
    if len(hit_index) == 0:
        return None, None, "no_ma_break_before_original_exit"
    pos = work.index.get_loc(hit_index[0])
    if isinstance(pos, slice) or pos >= len(work) - 1:
        return None, None, "no_next_bar_to_execute"
    execution = work.iloc[int(pos) + 1]
    exit_ts = pd.Timestamp(execution[stamp])
    if exit_ts >= row["exit_datetime"]:
        return None, None, "execution_not_earlier_than_original_exit"
    return exit_ts, float(execution["open"]), f"{kind}_ma_break"


def _metrics(frame: pd.DataFrame) -> dict[str, Any]:
    ret = pd.to_numeric(frame["variant_net_ret"], errors="coerce")
    win, loss = ret[ret > 0], ret[ret < 0]
    return {
        "trades": int(len(frame)),
        "changed": int(frame["changed"].sum()),
        "win_rate": float((ret > 0).mean()),
        "mean_ret": float(ret.mean()),
        "payoff_ratio": float(win.mean() / abs(loss.mean())) if len(win) and len(loss) else None,
        "worst_trade": float(ret.min()),
        "mean_delta_vs_original": float((ret - pd.to_numeric(frame["net_ret"], errors="coerce")).mean()),
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    source, raw_bars = _load()
    groups = {kind: {str(code): g.reset_index(drop=True) for code, g in bars.groupby("code")} for kind, bars in raw_bars.items()}
    all_rows: list[dict[str, Any]] = []
    for variant, kind in [("base_original", "base"), ("daily_ma5_runner", "d1"), ("m30_ma20_runner", "m30"), ("m15_ma20_runner", "m15")]:
        for _, row in source.iterrows():
            result = row.to_dict()
            result["variant"] = variant
            result["variant_net_ret"] = float(row["net_ret"])
            result["changed"] = False
            result["ma_exit_time"] = pd.NaT
            result["ma_exit_price"] = math.nan
            result["ma_exit_reason"] = "base_original"
            if kind != "base":
                runner_start = _runner_start(row, groups["m30"].get(str(row["code"]), pd.DataFrame()))
                exit_ts, price, reason = _ma_exit(row, groups[kind].get(str(row["code"]), pd.DataFrame()), kind, runner_start)
                result["ma_exit_reason"] = reason
                if exit_ts is not None and price is not None:
                    runner_ret = price / float(row["entry_price"]) - 1.0
                    result["variant_net_ret"] = 0.5 * TP + 0.5 * runner_ret
                    result["changed"] = True
                    result["ma_exit_time"] = exit_ts
                    result["ma_exit_price"] = price
            all_rows.append(result)
    audit = pd.DataFrame(all_rows)
    rows: list[dict[str, Any]] = []
    windows = {"full": ("2020-01-01", "2026-06-30"), "valid_2025": ("2025-01-01", "2025-12-31"), "blind_2026h1": ("2026-01-01", "2026-06-30")}
    for variant, group in audit.groupby("variant"):
        for name, (start, end) in windows.items():
            sample = group[group["entry_date"].between(pd.Timestamp(start), pd.Timestamp(end))]
            if not sample.empty:
                rows.append({"variant": variant, "window": name, **_metrics(sample)})
    summary = pd.DataFrame(rows)
    audit.to_csv(OUT_DIR / "formal26_trade_level_ma_exit_audit.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "window_summary.csv", index=False, encoding="utf-8-sig")
    coverage = {kind: int(len(bars)) for kind, bars in raw_bars.items()}
    payload = {
        "status": "completed",
        "research_only": True,
        "source_trades": int(len(source)),
        "take_profit_partial_trades": int(source["exit_reason"].astype(str).str.contains("take_profit_partial_30m").sum()),
        "bar_rows": coverage,
        "semantics": "Only replace the remaining half after an observed +12% partial take-profit. A MA signal must be complete and execution uses the next bar open before the original exit.",
        "limitations": "This is a trade-level incremental-exit audit, not a re-selection or full mark-to-market portfolio replay. No future bars are used for a trigger.",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = ["# 正式26笔主浪：盈利余仓均线退出验证", "", "- 仅研究，不改策略合同或交易开关。", "- 仅对已触发 +12% 半仓止盈的交易测试余仓退出；信号完成后以下一根 K 线开盘执行。", "", "## 分窗口结果", "", summary.to_markdown(index=False, floatfmt=".4f"), "", "## 边界", "", payload["limitations"]]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
