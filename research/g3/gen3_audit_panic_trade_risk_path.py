from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_query_df


WINDOWS = {
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-12-31"),
    "full": ("2020-01-01", "2026-12-31"),
}

BOOKS = {
    "base": ["slot5_20pct", "slot10_10pct"],
    "pause_weak_no_capitulation": ["slot5_20pct", "slot10_10pct"],
}

THRESHOLDS = [-0.05, -0.08, -0.10, -0.15]


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    value = pd.to_numeric(v, errors="coerce")
    if pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _load_trades(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path)
    d["entry_date"] = pd.to_datetime(d["entry_date"]).dt.normalize()
    d["exit_date"] = pd.to_datetime(d["exit_date"]).dt.normalize()
    for col in ["entry_price_adjusted", "net_ret", "stake", "candidate_score", "chain_rank"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d[d.get("status", "closed").eq("closed")].copy()
    return d.dropna(subset=["code", "entry_date", "exit_date", "entry_price_adjusted", "net_ret"])


def _load_daily_ohlc(trades: pd.DataFrame) -> pd.DataFrame:
    codes = sorted({str(c) for c in trades["code"].dropna().tolist() if re.fullmatch(r"[0-9A-Z.]+", str(c))})
    if not codes:
        return pd.DataFrame()
    start = trades["entry_date"].min().strftime("%Y-%m-%d")
    end = trades["exit_date"].max().strftime("%Y-%m-%d")
    code_list = ",".join(_sql_literal(code) for code in codes)
    sql = f"""
    SELECT code, trade_date, open, high, low, close, amount, volume
    FROM kline_daily
    WHERE code IN ({code_list})
      AND trade_date BETWEEN toDate({_sql_literal(start)}) AND toDate({_sql_literal(end)})
    ORDER BY code, trade_date
    """
    d = clickhouse_query_df(sql)
    if d.empty:
        return d
    d["trade_date"] = pd.to_datetime(d["trade_date"]).dt.normalize()
    for col in ["open", "high", "low", "close", "amount", "volume"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["code", "trade_date", "open", "high", "low", "close"])


def _days_from_entry(entry_date: pd.Timestamp, trade_date: pd.Timestamp, trade_days: list[pd.Timestamp]) -> int | None:
    days = [d for d in trade_days if entry_date <= d <= trade_date]
    if not days:
        return None
    return len(days) - 1


def _analyze_trade(row: pd.Series, ohlc_map: dict[str, pd.DataFrame]) -> dict:
    code = str(row["code"])
    entry_date = pd.Timestamp(row["entry_date"]).normalize()
    exit_date = pd.Timestamp(row["exit_date"]).normalize()
    entry_price = float(row["entry_price_adjusted"])
    d = ohlc_map.get(code, pd.DataFrame())
    if d.empty or entry_price <= 0:
        base = row.to_dict()
        base.update({"path_rows": 0})
        return base
    path = d[(d["trade_date"] >= entry_date) & (d["trade_date"] <= exit_date)].copy()
    if path.empty:
        base = row.to_dict()
        base.update({"path_rows": 0})
        return base
    path["low_ret"] = path["low"] / entry_price - 1.0
    path["close_ret"] = path["close"] / entry_price - 1.0
    path["open_ret"] = path["open"] / entry_price - 1.0
    trade_days = path["trade_date"].tolist()

    low_idx = path["low_ret"].idxmin()
    close_idx = path["close_ret"].idxmin()
    out = row.to_dict()
    out.update(
        {
            "path_rows": int(len(path)),
            "min_low_ret": float(path.loc[low_idx, "low_ret"]),
            "min_low_date": path.loc[low_idx, "trade_date"],
            "min_low_day_index": _days_from_entry(entry_date, path.loc[low_idx, "trade_date"], trade_days),
            "min_close_ret": float(path.loc[close_idx, "close_ret"]),
            "min_close_date": path.loc[close_idx, "trade_date"],
            "min_close_day_index": _days_from_entry(entry_date, path.loc[close_idx, "trade_date"], trade_days),
            "max_close_ret": float(path["close_ret"].max()),
            "entry_day_close_ret": float(path.iloc[0]["close_ret"]),
            "exit_day_open_ret": float(path.iloc[-1]["open_ret"]),
        }
    )
    for threshold in THRESHOLDS:
        key = f"low_le_{abs(int(threshold * 100))}pct"
        hit = path[path["low_ret"] <= threshold]
        out[key] = bool(not hit.empty)
        out[f"{key}_date"] = hit.iloc[0]["trade_date"] if not hit.empty else pd.NaT
        out[f"{key}_day_index"] = _days_from_entry(entry_date, hit.iloc[0]["trade_date"], trade_days) if not hit.empty else None

        close_key = f"close_le_{abs(int(threshold * 100))}pct"
        close_hit = path[path["close_ret"] <= threshold]
        out[close_key] = bool(not close_hit.empty)
        out[f"{close_key}_date"] = close_hit.iloc[0]["trade_date"] if not close_hit.empty else pd.NaT
        out[f"{close_key}_day_index"] = _days_from_entry(entry_date, close_hit.iloc[0]["trade_date"], trade_days) if not close_hit.empty else None
    return out


def _risk_summary(paths: pd.DataFrame, window: str, policy: str, book: str) -> dict:
    start, end = WINDOWS[window]
    d = paths[
        (paths["entry_date"] >= pd.Timestamp(start))
        & (paths["entry_date"] <= pd.Timestamp(end))
    ].copy()
    if d.empty:
        return {"window": window, "policy": policy, "book": book, "trades": 0}
    out = {
        "window": window,
        "policy": policy,
        "book": book,
        "trades": int(len(d)),
        "win_rate": float((d["net_ret"] > 0).mean()),
        "mean_net_ret": float(d["net_ret"].mean()),
        "worst_net_ret": float(d["net_ret"].min()),
        "mean_min_low_ret": float(d["min_low_ret"].mean()),
        "p10_min_low_ret": float(d["min_low_ret"].quantile(0.10)),
        "worst_min_low_ret": float(d["min_low_ret"].min()),
        "mean_min_close_ret": float(d["min_close_ret"].mean()),
        "worst_min_close_ret": float(d["min_close_ret"].min()),
    }
    for threshold in THRESHOLDS:
        key = f"low_le_{abs(int(threshold * 100))}pct"
        close_key = f"close_le_{abs(int(threshold * 100))}pct"
        out[f"{key}_rate"] = float(d[key].mean()) if key in d else 0.0
        out[f"{close_key}_rate"] = float(d[close_key].mean()) if close_key in d else 0.0
    return out


def _display_summary(raw: pd.DataFrame) -> pd.DataFrame:
    out = raw.copy()
    pct_cols = [
        "win_rate",
        "mean_net_ret",
        "worst_net_ret",
        "mean_min_low_ret",
        "p10_min_low_ret",
        "worst_min_low_ret",
        "mean_min_close_ret",
        "worst_min_close_ret",
    ]
    pct_cols += [c for c in out.columns if c.endswith("_rate") and c not in pct_cols]
    for col in pct_cols:
        if col in out.columns:
            out[col] = out[col].map(_pct)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit G3 panic trade risk path using daily OHLC.")
    parser.add_argument("--input-dir", default="reports/gen3_panic_v2_research/capital_curve_v1")
    parser.add_argument("--output-dir", default="reports/gen3_panic_v2_research/trade_risk_path_v1")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    summaries: list[dict] = []
    all_paths: list[pd.DataFrame] = []
    for policy, books in BOOKS.items():
        for book in books:
            source = input_dir / f"{policy}_{book}_executed_trades.csv"
            trades = _load_trades(source)
            ohlc = _load_daily_ohlc(trades)
            ohlc_map = {code: g.sort_values("trade_date").copy() for code, g in ohlc.groupby("code")}
            paths = pd.DataFrame([_analyze_trade(row, ohlc_map) for _, row in trades.iterrows()])
            paths["policy"] = policy
            paths["book"] = book
            paths.to_csv(out_dir / f"{policy}_{book}_trade_risk_paths.csv", index=False, encoding="utf-8-sig")
            all_paths.append(paths)
            for window in WINDOWS:
                summaries.append(_risk_summary(paths, window, policy, book))

    raw = pd.DataFrame(summaries)
    raw.to_csv(out_dir / "trade_risk_path_summary_raw.csv", index=False, encoding="utf-8-sig")
    display = _display_summary(raw)
    display.to_csv(out_dir / "trade_risk_path_summary_display.csv", index=False, encoding="utf-8-sig")

    all_path_df = pd.concat(all_paths, ignore_index=True) if all_paths else pd.DataFrame()
    worst_low = all_path_df.sort_values("min_low_ret").head(20).copy() if not all_path_df.empty else pd.DataFrame()
    worst_low.to_csv(out_dir / "worst_min_low_trades.csv", index=False, encoding="utf-8-sig")

    full = display[display["window"].eq("full")]
    pause_slot5 = display[
        display["policy"].eq("pause_weak_no_capitulation")
        & display["book"].eq("slot5_20pct")
    ]
    worst_view = worst_low.head(12).copy()
    if not worst_view.empty:
        for col in ["net_ret", "min_low_ret", "min_close_ret", "max_close_ret"]:
            worst_view[col] = worst_view[col].map(_pct)

    lines = [
        "# G3 Panic 单笔风险路径审计 V1",
        "",
        "## 口径",
        "",
        f"- 输入目录：`{input_dir}`",
        f"- 输出目录：`{out_dir}`",
        "- 读取上一版资金占用模拟中的已成交交易，再用 `kline_daily` 的 open/high/low/close 还原持仓期日线风险路径。",
        "- 本报告只做风险体检：统计持仓期最低价回撤、最低收盘回撤、以及是否触及 -5%/-8%/-10%/-15%。不把这些阈值直接当作新策略参数。",
        "",
        "## full 对照",
        "",
        full[
            [
                "policy",
                "book",
                "trades",
                "win_rate",
                "mean_net_ret",
                "worst_net_ret",
                "mean_min_low_ret",
                "p10_min_low_ret",
                "worst_min_low_ret",
                "low_le_8pct_rate",
                "close_le_8pct_rate",
                "low_le_15pct_rate",
            ]
        ].to_markdown(index=False),
        "",
        "## 主暂停口径分段",
        "",
        pause_slot5[
            [
                "window",
                "trades",
                "win_rate",
                "mean_net_ret",
                "mean_min_low_ret",
                "p10_min_low_ret",
                "worst_min_low_ret",
                "low_le_8pct_rate",
                "close_le_8pct_rate",
            ]
        ].to_markdown(index=False),
        "",
        "## 最深低点样本",
        "",
        worst_view[
            [
                "policy",
                "book",
                "entry_date",
                "code",
                "name",
                "net_ret",
                "min_low_ret",
                "min_low_date",
                "min_low_day_index",
                "min_close_ret",
                "g3_repair_env_label",
                "market_style",
            ]
        ].to_markdown(index=False)
        if not worst_view.empty
        else "无样本。",
        "",
        "## 判断",
        "",
        "1. 如果低点击穿率显著高于收盘击穿率，说明单纯日收盘止损会漏掉大量盘中失效，需要转向分钟级失败确认。",
        "2. 如果最深低点集中在少数单票或少数系统性日期，优先用仓位和系统暂停解决，而不是继续细切入场阈值。",
        "3. 这一步仍未模拟跌停不可卖、盘中成交排队和真实滑点，因此只能作为下一步风控设计依据。",
    ]
    (out_dir / "trade_risk_path_report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "input_dir": str(input_dir),
        "output_dir": str(out_dir),
        "summary_rows": int(len(raw)),
        "path_rows": int(len(all_path_df)),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
