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

from utils.market_warehouse import clickhouse_query_df


WINDOWS = {
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-12-31"),
    "full": ("2020-01-01", "2026-12-31"),
}

THRESHOLDS = [-0.05, -0.08, -0.10]


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    value = pd.to_numeric(v, errors="coerce")
    if pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _load_paths(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path)
    for col in ["entry_date", "exit_date", "confirm_datetime"]:
        if col in d.columns:
            d[col] = pd.to_datetime(d[col], errors="coerce")
    for col in ["entry_price_adjusted", "net_ret", "min_low_ret", "min_close_ret"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["code", "entry_date", "exit_date", "confirm_datetime", "entry_price_adjusted"]).copy()


def _load_daily_close(codes: list[str], start: str, end: str) -> pd.DataFrame:
    code_list = ",".join(_sql_literal(code) for code in codes)
    sql = f"""
    SELECT code, trade_date, close AS daily_close
    FROM kline_daily
    WHERE code IN ({code_list})
      AND trade_date BETWEEN toDate({_sql_literal(start)}) AND toDate({_sql_literal(end)})
    ORDER BY code, trade_date
    """
    d = clickhouse_query_df(sql)
    if d.empty:
        return d
    d["trade_date"] = pd.to_datetime(d["trade_date"]).dt.normalize()
    d["daily_close"] = pd.to_numeric(d["daily_close"], errors="coerce")
    return d.dropna(subset=["code", "trade_date", "daily_close"])


def _load_30m(paths: pd.DataFrame) -> pd.DataFrame:
    codes = sorted({str(c) for c in paths["code"].dropna().tolist() if re.fullmatch(r"[0-9A-Z.]+", str(c))})
    if not codes:
        return pd.DataFrame()
    min_date = paths["entry_date"].min().strftime("%Y-%m-%d")
    max_date = paths["exit_date"].max().strftime("%Y-%m-%d")
    daily = _load_daily_close(codes, min_date, max_date)
    parts: list[pd.DataFrame] = []
    date_values = pd.date_range(min_date, max_date, freq="D")
    valid_dates = sorted({d.strftime("%Y-%m-%d") for d in date_values})
    for di in range(0, len(valid_dates), 90):
        date_chunk = valid_dates[di : di + 90]
        date_list = ", ".join(f"toDate({_sql_literal(date)})" for date in date_chunk)
        for ci in range(0, len(codes), 200):
            batch = codes[ci : ci + 200]
            quoted = ", ".join(_sql_literal(code) for code in batch)
            part = clickhouse_query_df(
                f"""
                SELECT code, datetime, open, high, low, close, amount, volume
                FROM kline_minute_30
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
    bars["trade_date"] = bars["datetime"].dt.normalize()
    for col in ["open", "high", "low", "close", "amount", "volume"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    bars = bars.dropna(subset=["code", "datetime", "open", "high", "low", "close"])
    bars["minute_day_close"] = bars.groupby(["code", "trade_date"])["close"].transform("last")
    bars = bars.merge(daily, on=["code", "trade_date"], how="left")
    bars["price_scale_daily_to_minute"] = bars["daily_close"] / bars["minute_day_close"]
    bars["adj_open"] = bars["open"] * bars["price_scale_daily_to_minute"]
    bars["adj_high"] = bars["high"] * bars["price_scale_daily_to_minute"]
    bars["adj_low"] = bars["low"] * bars["price_scale_daily_to_minute"]
    bars["adj_close"] = bars["close"] * bars["price_scale_daily_to_minute"]
    return bars.dropna(subset=["adj_low", "adj_close"]).sort_values(["code", "datetime"])


def _first_hit(path: pd.DataFrame, col: str, threshold: float) -> tuple[bool, pd.Timestamp | pd.NaT]:
    hit = path[path[col] <= threshold]
    if hit.empty:
        return False, pd.NaT
    return True, pd.Timestamp(hit.iloc[0]["datetime"])


def _analyze_visibility(row: pd.Series, bar_map: dict[str, pd.DataFrame]) -> dict:
    code = str(row["code"])
    entry_price = float(row["entry_price_adjusted"])
    confirm_dt = pd.Timestamp(row["confirm_datetime"])
    exit_dt = pd.Timestamp(row["exit_date"]) + pd.Timedelta(hours=15)
    bars = bar_map.get(code, pd.DataFrame())
    out = row.to_dict()
    if bars.empty or entry_price <= 0:
        out["bar_rows_after_confirm"] = 0
        return out
    path = bars[(bars["datetime"] > confirm_dt) & (bars["datetime"] <= exit_dt)].copy()
    if path.empty:
        out["bar_rows_after_confirm"] = 0
        return out
    path["low_ret_30m"] = path["adj_low"] / entry_price - 1.0
    path["close_ret_30m"] = path["adj_close"] / entry_price - 1.0
    low_idx = path["low_ret_30m"].idxmin()
    close_idx = path["close_ret_30m"].idxmin()
    out.update(
        {
            "bar_rows_after_confirm": int(len(path)),
            "min_30m_low_ret": float(path.loc[low_idx, "low_ret_30m"]),
            "min_30m_low_datetime": path.loc[low_idx, "datetime"],
            "min_30m_close_ret": float(path.loc[close_idx, "close_ret_30m"]),
            "min_30m_close_datetime": path.loc[close_idx, "datetime"],
        }
    )
    for threshold in THRESHOLDS:
        p = abs(int(threshold * 100))
        low_hit, low_dt = _first_hit(path, "low_ret_30m", threshold)
        close_hit, close_dt = _first_hit(path, "close_ret_30m", threshold)
        out[f"m30_low_le_{p}pct"] = low_hit
        out[f"m30_low_le_{p}pct_datetime"] = low_dt
        out[f"m30_close_le_{p}pct"] = close_hit
        out[f"m30_close_le_{p}pct_datetime"] = close_dt
    return out


def _summary(d: pd.DataFrame, window: str) -> dict:
    start, end = WINDOWS[window]
    w = d[(d["entry_date"] >= pd.Timestamp(start)) & (d["entry_date"] <= pd.Timestamp(end))].copy()
    if w.empty:
        return {"window": window, "trades": 0}
    out = {
        "window": window,
        "trades": int(len(w)),
        "win_rate": float((w["net_ret"] > 0).mean()),
        "mean_net_ret": float(w["net_ret"].mean()),
        "mean_30m_low_ret": float(w["min_30m_low_ret"].mean()),
        "p10_30m_low_ret": float(w["min_30m_low_ret"].quantile(0.10)),
        "worst_30m_low_ret": float(w["min_30m_low_ret"].min()),
        "mean_30m_close_ret": float(w["min_30m_close_ret"].mean()),
    }
    for threshold in THRESHOLDS:
        p = abs(int(threshold * 100))
        out[f"m30_low_le_{p}pct_rate"] = float(w[f"m30_low_le_{p}pct"].mean())
        out[f"m30_close_le_{p}pct_rate"] = float(w[f"m30_close_le_{p}pct"].mean())
    if "low_le_8pct" in w.columns and "m30_close_le_5pct" in w.columns:
        deep = w[w["low_le_8pct"].astype(bool)]
        out["deep_daily_low_count"] = int(len(deep))
        out["deep_daily_low_m30_close5_visible_rate"] = float(deep["m30_close_le_5pct"].mean()) if len(deep) else 0.0
        out["deep_daily_low_m30_close8_visible_rate"] = float(deep["m30_close_le_8pct"].mean()) if len(deep) else 0.0
    return out


def _display(raw: pd.DataFrame) -> pd.DataFrame:
    out = raw.copy()
    pct_cols = [
        "win_rate",
        "mean_net_ret",
        "mean_30m_low_ret",
        "p10_30m_low_ret",
        "worst_30m_low_ret",
        "mean_30m_close_ret",
    ]
    pct_cols += [c for c in out.columns if c.endswith("_rate") and c not in pct_cols]
    for col in pct_cols:
        if col in out.columns:
            out[col] = out[col].map(_pct)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit 30m visibility of G3 panic failures.")
    parser.add_argument("--input", default="reports/gen3_panic_v2_research/trade_risk_path_v1/pause_weak_no_capitulation_slot5_20pct_trade_risk_paths.csv")
    parser.add_argument("--output-dir", default="reports/gen3_panic_v2_research/failure_visibility_30m_v1")
    args = parser.parse_args()

    source = Path(args.input)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    paths = _load_paths(source)
    bars = _load_30m(paths)
    bar_map = {code: g.sort_values("datetime").copy() for code, g in bars.groupby("code")}
    visible = pd.DataFrame([_analyze_visibility(row, bar_map) for _, row in paths.iterrows()])
    visible.to_csv(out_dir / "pause_slot5_30m_visibility.csv", index=False, encoding="utf-8-sig")

    raw = pd.DataFrame([_summary(visible, window) for window in WINDOWS])
    raw.to_csv(out_dir / "failure_visibility_30m_summary_raw.csv", index=False, encoding="utf-8-sig")
    display = _display(raw)
    display.to_csv(out_dir / "failure_visibility_30m_summary_display.csv", index=False, encoding="utf-8-sig")

    worst = visible.sort_values("min_30m_low_ret").head(15).copy()
    worst.to_csv(out_dir / "worst_30m_low_trades.csv", index=False, encoding="utf-8-sig")
    worst_view = worst.copy()
    if not worst_view.empty:
        for col in ["net_ret", "min_low_ret", "min_30m_low_ret", "min_30m_close_ret"]:
            if col in worst_view.columns:
                worst_view[col] = worst_view[col].map(_pct)

    lines = [
        "# G3 Panic 30m 失败可见性审计 V1",
        "",
        "## 口径",
        "",
        f"- 输入文件：`{source}`",
        f"- 输出目录：`{out_dir}`",
        "- 样本为 `pause_weak_no_capitulation + slot5_20pct` 已成交交易。",
        "- 30m 价格先按当日 `daily_close / minute_day_close` 校准到日线价格尺度，再计算相对 `entry_price_adjusted` 的低点和收盘回撤。",
        "- 只使用确认时间之后到退出日 15:00 之前的 30m bar，用于判断风险是否在持仓过程中可见。",
        "",
        "## 分段汇总",
        "",
        display[
            [
                "window",
                "trades",
                "win_rate",
                "mean_net_ret",
                "mean_30m_low_ret",
                "p10_30m_low_ret",
                "worst_30m_low_ret",
                "m30_low_le_8pct_rate",
                "m30_close_le_5pct_rate",
                "m30_close_le_8pct_rate",
                "deep_daily_low_count",
                "deep_daily_low_m30_close5_visible_rate",
                "deep_daily_low_m30_close8_visible_rate",
            ]
        ].to_markdown(index=False),
        "",
        "## 最深 30m 低点样本",
        "",
        worst_view[
            [
                "entry_date",
                "code",
                "name",
                "net_ret",
                "min_low_ret",
                "min_30m_low_ret",
                "min_30m_low_datetime",
                "min_30m_close_ret",
                "min_30m_close_datetime",
                "g3_repair_env_label",
                "market_style",
            ]
        ].to_markdown(index=False)
        if not worst_view.empty
        else "无样本。",
        "",
        "## 判断",
        "",
        "1. 若深低点样本中 30m 收盘 -5% 可见率较高，可以继续研究“失败确认后减仓/退出”，但不能直接按最优阈值调参。",
        "2. 若 30m 低点可见而收盘不可见，说明风险更多是盘中刺穿，实盘处理会涉及成交、滑点和纪律，不适合用日线回测替代。",
        "3. 这一版是可见性审计，不是正式退出策略回测。",
    ]
    (out_dir / "failure_visibility_30m_report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "input": str(source),
        "output_dir": str(out_dir),
        "trades": int(len(visible)),
        "bars": int(len(bars)),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
