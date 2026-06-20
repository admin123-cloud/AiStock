from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_g3_five_strategies_from_scratch_v1 import _md_table, _pct  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402
from utils.paths import report_path  # noqa: E402


SOURCE_DIR = report_path("g3_five_strategies_native_bridge_v1")
OUT_DIR = report_path("g3_native_bridge_30m_integrity_v1")


def _safe_float(value: Any) -> float:
    try:
        x = float(value)
    except Exception:
        return math.nan
    return x if math.isfinite(x) else math.nan


def _read_candidates() -> pd.DataFrame:
    path = SOURCE_DIR / "all_strategy_candidates.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    df = pd.read_csv(path, low_memory=False)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.normalize()
    df = df[df["entry_date"].notna()].copy()
    df["code"] = df["code"].fillna("").astype(str).str.strip().str.upper()
    return df


def _query_30m_bars(code: str, start: str, end: str) -> pd.DataFrame:
    bars = clickhouse_query_df(
        """
        SELECT code, datetime, open, high, low, close, amount
        FROM kline_minute_30
        WHERE code = ?
          AND toDate(datetime) BETWEEN ? AND ?
        ORDER BY datetime
        """,
        [code, start, end],
    )
    if bars.empty:
        return bars
    bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
    bars["date"] = bars["datetime"].dt.normalize()
    for col in ["open", "high", "low", "close", "amount"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    return bars.dropna(subset=["datetime", "close"]).sort_values("datetime").reset_index(drop=True)


def _features_for_row(row: pd.Series, bars: pd.DataFrame) -> dict[str, Any]:
    entry_date = pd.Timestamp(row["entry_date"]).normalize()
    # Entry-date candidates are next-open style in several native sources; use both D and D-1
    # windows so the audit can tell whether confirmation was present before entry.
    signal_cutoff = entry_date - pd.Timedelta(hours=9)
    entry_cutoff = entry_date + pd.Timedelta(hours=15)
    hist_pre_entry = bars[bars["datetime"] <= entry_cutoff].tail(80).copy()
    hist_pre_signal = bars[bars["datetime"] <= signal_cutoff].tail(80).copy()
    day = bars[bars["date"].eq(entry_date)].copy()
    signal_day = bars[bars["date"].eq(entry_date - pd.Timedelta(days=1))].copy()

    base: dict[str, Any] = {
        "m30_data_ok": False,
        "m30_pre_entry_ok": False,
        "m30_pre_signal_ok": False,
        "m30_day_bar_count": int(len(day)),
        "m30_signal_day_bar_count": int(len(signal_day)),
        "m30_latest_time": None,
        "m30_close_above_ma20": math.nan,
        "m30_close_above_ma40": math.nan,
        "m30_mom6": math.nan,
        "m30_mom12": math.nan,
        "m30_amount_last2_ratio": math.nan,
        "m30_day_ret": math.nan,
        "m30_day_close_pos": math.nan,
        "m30_last_bar_ret": math.nan,
    }
    if len(hist_pre_entry) < 20:
        return base
    hist = hist_pre_entry
    close = pd.to_numeric(hist["close"], errors="coerce")
    amount = pd.to_numeric(hist["amount"], errors="coerce")
    last = float(close.iloc[-1])
    ma20 = float(close.tail(20).mean())
    ma40 = float(close.tail(40).mean()) if len(close) >= 40 else float(close.mean())
    prev6 = float(close.iloc[-7]) if len(close) >= 7 else math.nan
    prev12 = float(close.iloc[-13]) if len(close) >= 13 else math.nan
    amt_last2 = float(amount.tail(2).mean())
    amt_prev20 = float(amount.tail(22).head(20).mean()) if len(amount) >= 22 else float(amount.tail(20).mean())
    day_for_shape = day if not day.empty else hist.tail(min(8, len(hist))).copy()
    day_open = float(day_for_shape["open"].iloc[0])
    day_high = float(day_for_shape["high"].max())
    day_low = float(day_for_shape["low"].min())
    day_close = float(day_for_shape["close"].iloc[-1])
    day_range = max(day_high - day_low, 1e-9)
    last_open = float(day_for_shape["open"].iloc[-1])
    base.update(
        {
            "m30_data_ok": True,
            "m30_pre_entry_ok": len(hist_pre_entry) >= 20,
            "m30_pre_signal_ok": len(hist_pre_signal) >= 20,
            "m30_latest_time": hist["datetime"].iloc[-1].strftime("%Y-%m-%d %H:%M:%S"),
            "m30_close_above_ma20": last / ma20 - 1.0 if ma20 > 0 else math.nan,
            "m30_close_above_ma40": last / ma40 - 1.0 if ma40 > 0 else math.nan,
            "m30_mom6": last / prev6 - 1.0 if prev6 > 0 else math.nan,
            "m30_mom12": last / prev12 - 1.0 if prev12 > 0 else math.nan,
            "m30_amount_last2_ratio": amt_last2 / amt_prev20 if amt_prev20 > 0 else math.nan,
            "m30_day_ret": day_close / day_open - 1.0 if day_open > 0 else math.nan,
            "m30_day_close_pos": (day_close - day_low) / day_range,
            "m30_last_bar_ret": day_close / last_open - 1.0 if last_open > 0 else math.nan,
        }
    )
    return base


def _confirm_rule(row: pd.Series) -> tuple[bool, str]:
    strategy = str(row.get("trade_strategy") or "")
    if not bool(row.get("m30_data_ok")):
        return False, "missing_30m_data"
    close_ma20 = _safe_float(row.get("m30_close_above_ma20"))
    close_ma40 = _safe_float(row.get("m30_close_above_ma40"))
    mom6 = _safe_float(row.get("m30_mom6"))
    amount_ratio = _safe_float(row.get("m30_amount_last2_ratio"))
    close_pos = _safe_float(row.get("m30_day_close_pos"))
    day_ret = _safe_float(row.get("m30_day_ret"))
    last_bar_ret = _safe_float(row.get("m30_last_bar_ret"))

    if strategy == "institutional_score120_mainwave":
        ok = close_ma20 >= 0.0 and mom6 >= -0.03 and last_bar_ret <= 0.04
        return bool(ok), "close>=ma20 && mom6>=-3% && not_tail_chase"
    if strategy == "old_g3_strong_breakout":
        ok = close_ma20 >= -0.01 and mom6 >= -0.02 and close_pos >= 0.45
        return bool(ok), "close near/above ma20 && mom6>=-2% && close_pos>=45%"
    if strategy == "volume_runup_supplement":
        ok = close_ma40 >= -0.02 and amount_ratio >= 0.80 and close_pos >= 0.35
        return bool(ok), "close>=ma40-2% && amount_ratio>=0.8 && close_pos>=35%"
    if strategy == "panic_capitulation_repair":
        ok = close_pos >= 0.45 and day_ret >= -0.04 and amount_ratio >= 0.70
        return bool(ok), "close_pos>=45% && day_ret>=-4% && amount_ratio>=0.7"
    if strategy == "range_weak_repair":
        ok = close_pos >= 0.45 and day_ret >= -0.035 and close_ma20 >= -0.06
        return bool(ok), "close_pos>=45% && day_ret>=-3.5% && close>=ma20-6%"
    return False, "unknown_strategy"


def _compute_features(candidates: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for code, group in candidates.groupby("code", sort=False):
        start = (group["entry_date"].min() - pd.Timedelta(days=70)).strftime("%Y-%m-%d")
        end = group["entry_date"].max().strftime("%Y-%m-%d")
        bars = _query_30m_bars(str(code), start, end)
        for idx, row in group.iterrows():
            features = _features_for_row(row, bars) if not bars.empty else {"m30_data_ok": False}
            features["_idx"] = idx
            rows.append(features)
    feat = pd.DataFrame(rows).set_index("_idx")
    out = candidates.join(feat)
    confirm = out.apply(_confirm_rule, axis=1, result_type="expand")
    out["m30_confirmed_proxy"] = confirm[0].astype(bool)
    out["m30_confirm_rule"] = confirm[1]
    return out


def _group_summary(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for keys, part in df.groupby(cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        row = {col: key for col, key in zip(cols, keys)}
        row.update(
            {
                "candidate_count": int(len(part)),
                "m30_data_ok_rate": float(part["m30_data_ok"].mean()) if len(part) else 0.0,
                "m30_pre_signal_ok_rate": float(part["m30_pre_signal_ok"].mean()) if "m30_pre_signal_ok" in part.columns and len(part) else 0.0,
                "m30_confirmed_proxy_rate": float(part["m30_confirmed_proxy"].mean()) if len(part) else 0.0,
                "avg_m30_close_above_ma20": float(pd.to_numeric(part.get("m30_close_above_ma20"), errors="coerce").mean()),
                "avg_m30_day_close_pos": float(pd.to_numeric(part.get("m30_day_close_pos"), errors="coerce").mean()),
                "avg_m30_amount_last2_ratio": float(pd.to_numeric(part.get("m30_amount_last2_ratio"), errors="coerce").mean()),
            }
        )
        rows.append(row)
    out = pd.DataFrame(rows)
    return out.sort_values("candidate_count", ascending=False).reset_index(drop=True) if not out.empty else out


def _write_report(summary: dict[str, Any], by_strategy: pd.DataFrame, by_source: pd.DataFrame, failures: pd.DataFrame) -> None:
    text = f"""# G3 原生桥接候选 30m 完整性审计

## 结论

本审计检查 `g3_five_strategies_native_bridge_v1` 默认准入候选是否具备 30m 数据，以及按统一五策略给出的基础 30m 确认代理是否通过。它不改变回测收益，只用于判断“恢复出来的赚钱源”是否可以继续进入正式 30m 验收。

## 总体

- 候选数：{summary['candidate_count']}
- 30m 数据可用率：{_pct(summary['m30_data_ok_rate'])}
- 入场前一交易窗口可用率：{_pct(summary['m30_pre_signal_ok_rate'])}
- 30m 确认代理通过率：{_pct(summary['m30_confirmed_proxy_rate'])}

## 按交易策略

{_md_table(by_strategy, {'m30_data_ok_rate', 'm30_pre_signal_ok_rate', 'm30_confirmed_proxy_rate', 'avg_m30_close_above_ma20', 'avg_m30_day_close_pos'}, set())}

## 按原生来源

{_md_table(by_source, {'m30_data_ok_rate', 'm30_pre_signal_ok_rate', 'm30_confirmed_proxy_rate', 'avg_m30_close_above_ma20', 'avg_m30_day_close_pos'}, set())}

## 未通过样本 Top 30

{_md_table(failures.head(30), {'m30_close_above_ma20', 'm30_day_close_pos', 'm30_amount_last2_ratio'}, set())}

## 判断

1. 若 30m 数据可用率不足，问题是数据覆盖，不应误判为策略融合失败。
2. 若数据可用但确认代理通过率低，应回到各原生策略的真实 30m 确认规则，不应继续用日线代理替代。
3. 只有候选召回、原生源准入、30m确认、统一二槽路由同时通过，才能证明“减少策略没有跑丢原来赚钱的策略”。
"""
    (OUT_DIR / "REPORT_CN.md").write_text(text, encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    candidates = _read_candidates()
    audited = _compute_features(candidates)
    by_strategy = _group_summary(audited, ["trade_strategy", "trade_strategy_label"])
    by_source = _group_summary(audited, ["trade_strategy_label", "source_strategy_label"])
    failures = audited[~audited["m30_confirmed_proxy"].astype(bool)].copy()
    failure_cols = [
        "entry_date",
        "code",
        "name",
        "trade_strategy_label",
        "source_strategy_label",
        "m30_confirm_rule",
        "m30_data_ok",
        "m30_pre_signal_ok",
        "m30_day_bar_count",
        "m30_close_above_ma20",
        "m30_day_close_pos",
        "m30_amount_last2_ratio",
    ]
    failures = failures[[c for c in failure_cols if c in failures.columns]]
    summary = {
        "candidate_count": int(len(audited)),
        "m30_data_ok_rate": float(audited["m30_data_ok"].mean()) if len(audited) else 0.0,
        "m30_pre_signal_ok_rate": float(audited["m30_pre_signal_ok"].mean()) if "m30_pre_signal_ok" in audited.columns and len(audited) else 0.0,
        "m30_confirmed_proxy_rate": float(audited["m30_confirmed_proxy"].mean()) if len(audited) else 0.0,
        "output_dir": str(OUT_DIR),
    }
    audited.to_csv(OUT_DIR / "native_bridge_candidates_30m_audit.csv", index=False, encoding="utf-8-sig")
    by_strategy.to_csv(OUT_DIR / "m30_summary_by_strategy.csv", index=False, encoding="utf-8-sig")
    by_source.to_csv(OUT_DIR / "m30_summary_by_source.csv", index=False, encoding="utf-8-sig")
    failures.to_csv(OUT_DIR / "m30_unconfirmed_samples.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(summary, by_strategy, by_source, failures)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
