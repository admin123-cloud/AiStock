from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


OUT = ROOT / "reports" / "g2_mainline_sector_overlay_backtest_v1"
G2_SOURCE = ROOT / "reports" / "gen2_v2_complete_strategy" / "sources" / "g2_v2_complete.parquet"
G2_TRADES = ROOT / "reports" / "gen2_v2_complete_strategy" / "runs" / "official" / "full" / "trades.csv"
MAINLINE_CONFIRMED = ROOT / "reports" / "true_sector_index_intraday_diffusion_v1" / "confirmed_intraday_diffusion_sectors.csv"

WINDOWS = {
    "train": ("2024-07-09", "2025-03-31"),
    "valid": ("2025-04-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-05-29"),
    "full": ("2024-07-09", "2026-05-29"),
}

ACTIVE_DAYS = [20, 60, 120, 180, 240, 360, 720]


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    return str(obj)


def _window_of(date: pd.Timestamp) -> str | None:
    for name, (start, end) in WINDOWS.items():
        if name == "full":
            continue
        if pd.Timestamp(start) <= date <= pd.Timestamp(end):
            return name
    return None


def _load_g2_lots() -> pd.DataFrame:
    source = pd.read_parquet(G2_SOURCE).copy()
    trades = pd.read_csv(G2_TRADES, encoding="utf-8-sig").copy()
    source["entry_date_text"] = pd.to_datetime(source["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    trades["buy_date_text"] = pd.to_datetime(trades["buy_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    trades["sell_date_text"] = pd.to_datetime(trades["sell_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    trades["return"] = pd.to_numeric(trades["return"], errors="coerce")
    trades["pnl"] = pd.to_numeric(trades["pnl"], errors="coerce")
    trades["capital"] = pd.to_numeric(trades["capital"], errors="coerce")

    lots = (
        trades.groupby(["buy_date_text", "code", "name"], dropna=False)
        .agg(
            sell_date=("sell_date_text", "max"),
            lot_return=("return", "sum"),
            pnl=("pnl", "sum"),
            capital=("capital", "first"),
            exit_rows=("return", "count"),
            min_exit_return=("return", "min"),
            max_exit_return=("return", "max"),
        )
        .reset_index()
        .rename(columns={"buy_date_text": "buy_date"})
    )

    context_cols = [
        "entry_date_text",
        "code",
        "signal_family",
        "source_family",
        "g2_v2_buy_logic",
        "l1_sector_code",
        "l1_sector_name",
        "l2_sector_code",
        "l2_sector_name",
        "l3_sector_code",
        "l3_sector_name",
        "l3_s3",
        "l2_s3",
        "l3_rise",
        "sector_strong",
        "v4_rank",
        "v4_score",
        "confirm_datetime",
    ]
    context = source[[c for c in context_cols if c in source.columns]].copy()
    lots = lots.merge(context, left_on=["buy_date", "code"], right_on=["entry_date_text", "code"], how="left")
    lots["buy_ts"] = pd.to_datetime(lots["buy_date"], errors="coerce")
    lots["sell_ts"] = pd.to_datetime(lots["sell_date"], errors="coerce")
    lots["window"] = lots["buy_ts"].map(_window_of)
    lots = lots.dropna(subset=["buy_ts"])
    return lots


def _load_mainline() -> pd.DataFrame:
    df = pd.read_csv(MAINLINE_CONFIRMED, encoding="utf-8-sig").copy()
    df["anchor_ts"] = pd.to_datetime(df["anchor_date"], errors="coerce")
    df["sector_code"] = df["sector_code"].astype(str)
    df["intraday_diffusion_score"] = pd.to_numeric(df["intraday_diffusion_score"], errors="coerce")
    return df.dropna(subset=["anchor_ts", "sector_code"])


def _add_mainline_flags(lots: pd.DataFrame, mainline: pd.DataFrame) -> pd.DataFrame:
    out = lots.copy()
    for days in ACTIVE_DAYS:
        out[f"mainline_l2_active_{days}d"] = False
        out[f"mainline_l3_parent_active_{days}d"] = False
        out[f"mainline_score_{days}d"] = np.nan
        out[f"mainline_anchor_{days}d"] = ""

    main_by_sector = {code: part.sort_values("anchor_ts") for code, part in mainline.groupby("sector_code")}
    for idx, row in out.iterrows():
        buy_ts = row["buy_ts"]
        l2_code = str(row.get("l2_sector_code") or "")
        l1_code = str(row.get("l1_sector_code") or "")
        for days in ACTIVE_DAYS:
            best = None
            for code, flag_col in [
                (l2_code, f"mainline_l2_active_{days}d"),
                (l1_code, f"mainline_l3_parent_active_{days}d"),
            ]:
                if not code or code == "nan" or code not in main_by_sector:
                    continue
                part = main_by_sector[code]
                hit = part[(part["anchor_ts"] <= buy_ts) & (part["anchor_ts"] >= buy_ts - pd.Timedelta(days=days))]
                if hit.empty:
                    continue
                latest = hit.sort_values(["anchor_ts", "intraday_diffusion_score"], ascending=[False, False]).iloc[0]
                out.at[idx, flag_col] = True
                if best is None or float(latest["intraday_diffusion_score"]) > float(best["intraday_diffusion_score"]):
                    best = latest
            if best is not None:
                out.at[idx, f"mainline_score_{days}d"] = float(best["intraday_diffusion_score"])
                out.at[idx, f"mainline_anchor_{days}d"] = pd.Timestamp(best["anchor_ts"]).strftime("%Y-%m-%d")
    return out


def _subset_for_variant(df: pd.DataFrame, variant: str) -> pd.Series:
    if variant == "all_g2":
        return pd.Series(True, index=df.index)
    if variant.startswith("l2_active_"):
        days = variant.split("_")[-1]
        return df[f"mainline_l2_active_{days}"].astype(bool)
    if variant.startswith("l2_or_l1_active_"):
        days = variant.split("_")[-1]
        return df[f"mainline_l2_active_{days}"].astype(bool) | df[f"mainline_l3_parent_active_{days}"].astype(bool)
    raise ValueError(variant)


def _max_drawdown_from_returns(returns: pd.Series) -> float:
    if returns.empty:
        return np.nan
    equity = (1.0 + returns.fillna(0.0)).cumprod()
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(dd.min())


def _stats(part: pd.DataFrame) -> dict[str, Any]:
    returns = pd.to_numeric(part["lot_return"], errors="coerce").dropna()
    if returns.empty:
        return {
            "lots": 0,
            "total_return_compound": np.nan,
            "sum_pnl": 0.0,
            "win_rate": np.nan,
            "avg_lot_return": np.nan,
            "median_lot_return": np.nan,
            "max_drawdown_lot_curve": np.nan,
            "best_lot": np.nan,
            "worst_lot": np.nan,
        }
    ordered = part.sort_values(["buy_ts", "code"]).copy()
    ordered_returns = pd.to_numeric(ordered["lot_return"], errors="coerce").fillna(0.0)
    return {
        "lots": int(len(returns)),
        "total_return_compound": float((1.0 + ordered_returns).prod() - 1.0),
        "sum_pnl": float(pd.to_numeric(part["pnl"], errors="coerce").fillna(0.0).sum()),
        "win_rate": float((returns > 0).mean()),
        "avg_lot_return": float(returns.mean()),
        "median_lot_return": float(returns.median()),
        "max_drawdown_lot_curve": _max_drawdown_from_returns(ordered_returns),
        "best_lot": float(returns.max()),
        "worst_lot": float(returns.min()),
    }


def _build_summary(lots: pd.DataFrame) -> pd.DataFrame:
    variants = ["all_g2"]
    for days in ACTIVE_DAYS:
        variants.append(f"l2_active_{days}d")
        variants.append(f"l2_or_l1_active_{days}d")

    rows = []
    for variant in variants:
        mask = _subset_for_variant(lots, variant)
        for window, (start, end) in WINDOWS.items():
            wmask = (lots["buy_ts"] >= pd.Timestamp(start)) & (lots["buy_ts"] <= pd.Timestamp(end))
            part = lots[mask & wmask].copy()
            row = {
                "variant": variant,
                "window": window,
                "start_date": start,
                "end_date": end,
            }
            row.update(_stats(part))
            rows.append(row)
    return pd.DataFrame(rows)


def _write_report(summary: pd.DataFrame, lots: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    lots_path = OUT / "g2_lots_with_mainline_flags.csv"
    summary_path = OUT / "summary.csv"
    json_path = OUT / "summary.json"
    report_path = OUT / "REPORT.md"
    lots.to_csv(lots_path, index=False, encoding="utf-8-sig")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")

    payload = {
        "report": str(report_path),
        "summary_csv": str(summary_path),
        "lots_csv": str(lots_path),
        "g2_lots": int(len(lots)),
        "mainline_confirmed_file": str(MAINLINE_CONFIRMED),
        "note": "Lot-level approximation. Partial exits are grouped by buy_date+code; lot_return sums exit returns, matching prior G2 lot attribution convention.",
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")

    def fmt_pct(x: Any) -> str:
        if pd.isna(x):
            return ""
        return f"{float(x):.2%}"

    lines = [
        "# G2 主线板块选股叠加回测 V1",
        "",
        "## 回测口径",
        "",
        "- 基础策略：`g2_v2_complete` 正式 full 交易记录。",
        "- 回测窗口：train `2024-07-09~2025-03-31`，valid `2025-04-01~2025-12-31`，blind_2026ytd `2026-01-01~2026-05-29`，full `2024-07-09~2026-05-29`。",
        "- 主线定义：复用 `true_sector_index_intraday_diffusion_v1` 的历史确认板块，即真实 L2 板块指数窗口 + 上午盘中扩散确认。",
        "- 选股叠加：G2 买入日落在主线确认后的 20/60/120 个自然日内，且个股所属 L2 板块命中主线板块，则视为 `l2_active_xd`。",
        "- 诊断扩展：额外输出 180/240/360/720 天窗口，用于识别锚点过稀导致的样本不足问题；这些长窗口不建议直接作为实盘规则。",
        "- 宽松叠加：`l2_or_l1_active_xd` 同时允许 L1 父级板块命中，用于观察主线方向泛化，但正式更应优先看 L2。",
        "- 风险提示：这是逐笔 lot 级近似，不是完整资金排队重放；半仓/剩余仓退出已按 `buy_date+code` 合并，`lot_return` 沿用前序 G2 lot 归因的退出收益求和口径。",
        "",
        "## 核心结果",
        "",
        "| variant | window | 日期 | 笔数 | 复合收益 | 最大回撤 | 胜率 | 平均单笔 | 中位单笔 | PnL |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary.itertuples(index=False):
        lines.append(
            f"| {row.variant} | {row.window} | {row.start_date}~{row.end_date} | {int(row.lots)} | "
            f"{fmt_pct(row.total_return_compound)} | {fmt_pct(row.max_drawdown_lot_curve)} | "
            f"{fmt_pct(row.win_rate)} | {fmt_pct(row.avg_lot_return)} | {fmt_pct(row.median_lot_return)} | "
            f"{float(row.sum_pnl):.0f} |"
        )

    full = summary[summary["window"].eq("full")].copy()
    base = full[full["variant"].eq("all_g2")].iloc[0]
    lines.extend(["", "## 结论", ""])
    for variant in ["l2_active_20d", "l2_active_60d", "l2_active_120d", "l2_or_l1_active_60d", "l2_or_l1_active_120d"]:
        row = full[full["variant"].eq(variant)]
        if row.empty:
            continue
        r = row.iloc[0]
        lines.append(
            f"- `{variant}`：full 窗口 {r['start_date']}~{r['end_date']}，"
            f"{int(r['lots'])} 笔，复合收益 {fmt_pct(r['total_return_compound'])}，"
            f"胜率 {fmt_pct(r['win_rate'])}，平均单笔 {fmt_pct(r['avg_lot_return'])}；"
            f"相对全量 G2 的平均单笔变化 {fmt_pct(r['avg_lot_return'] - base['avg_lot_return'])}。"
        )
    lines.extend(
        [
            "",
            "## 使用建议",
            "",
            "- 如果 `l2_active_20d/60d/120d` 样本过少，说明当前主线确认文件只是季度/锚点研究产物，不能直接当作 G2 日频过滤器。",
            "- 若要提升 G2 单笔质量，当前更可靠的是使用 G2 信号确认时刻自身的 `l3_s3/l2_s3` 板块扩散强度，而不是这份稀疏锚点主线文件。",
            "- 如果目标是控制回撤，主线板块更适合作为排序加分和仓位倾斜，而不是硬过滤；硬过滤会减少交易笔数，可能错过非主线但有效的 G2 交易。",
            "- 这份回测仍需下一轮做完整资金曲线重放，加入成交滑点、涨跌停排队、容量和确认 bar 可见性审计后，才能进入正式策略。",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(str(report_path))
    print(str(summary_path))
    print(str(lots_path))
    print(summary.to_string(index=False))


def main() -> int:
    lots = _load_g2_lots()
    mainline = _load_mainline()
    lots = _add_mainline_flags(lots, mainline)
    summary = _build_summary(lots)
    _write_report(summary, lots)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
