from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_confirm_d3_execution_stress_v1 import _sql_literal  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


TRADES = _report_path() / "gen3_v4_bigbull_final_execution_stress_v1" / "cost30" / "closed_trades.csv"
CONTEXT = _report_path() / "gen3_v4_bigbull_structural_context_v1" / "bigbull_context_enriched.csv"
OUT_DIR = _report_path() / "gen3_v4_bigbull_30m_delay_capacity_v1"
COST_BPS = 30.0
BIGBULL_ROUTE = "strong_bigbull_struct_gate"


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "无数据"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
        for col, value in row.items():
            if col in pct_cols:
                item[col] = pct(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = value
        rows.append(item)
    view = pd.DataFrame(rows)
    return view.to_markdown(index=False)


def load_bigbull_trades() -> pd.DataFrame:
    trades = pd.read_csv(TRADES, low_memory=False, encoding="utf-8-sig")
    trades = trades[trades["route"].astype(str).eq(BIGBULL_ROUTE)].copy()
    trades["entry_date"] = pd.to_datetime(trades["entry_date"], errors="coerce").dt.normalize()
    trades["code"] = trades["code"].astype(str)
    for col in ["entry_price", "policy_net_ret", "stake", "realized_pnl"]:
        trades[col] = pd.to_numeric(trades[col], errors="coerce")
    return trades.dropna(subset=["entry_date", "code", "entry_price", "policy_net_ret", "stake"]).copy()


def load_context(trades: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "entry_date",
        "code",
        "confirm_datetime",
        "fractal_datetime",
        "confirm_amount",
        "confirm_amount_ma5_prev",
        "rt_30m_amount_ratio",
        "box_top",
        "rt_breakout_vs_box_top",
        "calc_ret_5d",
        "calc_ret_10d",
    ]
    ctx = pd.read_csv(CONTEXT, usecols=lambda c: c in cols, low_memory=False, encoding="utf-8-sig")
    ctx["entry_date"] = pd.to_datetime(ctx["entry_date"], errors="coerce").dt.normalize()
    ctx["code"] = ctx["code"].astype(str)
    ctx["confirm_datetime"] = pd.to_datetime(ctx["confirm_datetime"], errors="coerce")
    for col in [c for c in cols if c not in {"entry_date", "code", "confirm_datetime", "fractal_datetime"}]:
        if col in ctx.columns:
            ctx[col] = pd.to_numeric(ctx[col], errors="coerce")
    keys = trades[["entry_date", "code"]].drop_duplicates()
    return keys.merge(ctx, on=["entry_date", "code"], how="left")


def load_30m_bars(trades: pd.DataFrame) -> pd.DataFrame:
    codes = sorted(trades["code"].dropna().astype(str).unique().tolist())
    start = trades["entry_date"].min().strftime("%Y-%m-%d")
    end = (trades["entry_date"].max() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 200):
        quoted = ",".join(_sql_literal(code) for code in codes[i : i + 200])
        sql = f"""
        SELECT code, datetime, open, high, low, close, amount
        FROM kline_minute_30
        WHERE code IN ({quoted})
          AND datetime >= toDateTime({_sql_literal(start + " 09:00:00")})
          AND datetime <= toDateTime({_sql_literal(end + " 15:30:00")})
        ORDER BY code, datetime
        """
        part = clickhouse_query_df(sql)
        if not part.empty:
            parts.append(part)
    bars = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if bars.empty:
        return bars
    bars["code"] = bars["code"].astype(str)
    bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
    bars["trade_date"] = bars["datetime"].dt.normalize()
    for col in ["open", "high", "low", "close", "amount"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    return bars.dropna(subset=["code", "datetime", "open", "close"]).sort_values(["code", "datetime"]).copy()


def attach_delay_prices(trades: pd.DataFrame, ctx: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    out = trades.merge(ctx, on=["entry_date", "code"], how="left")
    records: list[dict[str, Any]] = []
    grouped = {code: g.sort_values("datetime").reset_index(drop=True) for code, g in bars.groupby("code")}
    for _, row in out.iterrows():
        code = str(row["code"])
        confirm_dt = row.get("confirm_datetime")
        day = row["entry_date"]
        g = grouped.get(code, pd.DataFrame())
        same_day = g[g["trade_date"].eq(day)].copy() if not g.empty else pd.DataFrame()
        confirm_bar = pd.Series(dtype=object)
        next_bar = pd.Series(dtype=object)
        if not same_day.empty and pd.notna(confirm_dt):
            exact = same_day[same_day["datetime"].eq(confirm_dt)]
            if exact.empty:
                exact = same_day[same_day["datetime"].le(confirm_dt)].tail(1)
            if not exact.empty:
                confirm_bar = exact.iloc[0]
                later = same_day[same_day["datetime"].gt(confirm_bar["datetime"])]
                if not later.empty:
                    next_bar = later.iloc[0]
        if next_bar.empty and not same_day.empty:
            after_entry = same_day[same_day["datetime"].gt(confirm_dt)] if pd.notna(confirm_dt) else same_day
            if not after_entry.empty:
                next_bar = after_entry.iloc[0]
        records.append(
            {
                "code": code,
                "entry_date": day,
                "matched_confirm_datetime": confirm_bar.get("datetime", pd.NaT),
                "confirm_bar_close": confirm_bar.get("close", float("nan")),
                "confirm_bar_amount": confirm_bar.get("amount", float("nan")),
                "next_30m_datetime": next_bar.get("datetime", pd.NaT),
                "next_30m_open": next_bar.get("open", float("nan")),
                "next_30m_close": next_bar.get("close", float("nan")),
                "next_30m_high": next_bar.get("high", float("nan")),
                "next_30m_low": next_bar.get("low", float("nan")),
                "next_30m_amount": next_bar.get("amount", float("nan")),
            }
        )
    px = pd.DataFrame(records)
    out = out.merge(px, on=["entry_date", "code"], how="left")
    gross_ratio = 1.0 + out["policy_net_ret"] + COST_BPS / 10000.0
    for label, price_col in [("next_open", "next_30m_open"), ("next_close", "next_30m_close")]:
        price = pd.to_numeric(out[price_col], errors="coerce")
        out[f"delay_{label}_net_ret"] = gross_ratio * (out["entry_price"] / price) - 1.0 - COST_BPS / 10000.0
        out.loc[price.le(0) | price.isna(), f"delay_{label}_net_ret"] = pd.NA
        out[f"delay_{label}_ret_delta"] = out[f"delay_{label}_net_ret"] - out["policy_net_ret"]
        out[f"delay_{label}_pnl_delta"] = out["stake"] * out[f"delay_{label}_ret_delta"]
    for col in ["confirm_bar_amount", "next_30m_amount", "confirm_amount"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["stake_to_confirm_bar_amount"] = out["stake"] / out["confirm_bar_amount"]
    out["stake_to_next_30m_amount"] = out["stake"] / out["next_30m_amount"]
    out["stake_to_context_confirm_amount"] = out["stake"] / out["confirm_amount"]
    for col in ["stake_to_confirm_bar_amount", "stake_to_next_30m_amount", "stake_to_context_confirm_amount"]:
        out.loc[~out[col].replace([float("inf"), -float("inf")], pd.NA).notna(), col] = pd.NA
    out["next_bar_missing"] = out["next_30m_open"].isna()
    return out


def summarize_delay(d: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for label in ["policy", "delay_next_open", "delay_next_close"]:
        ret_col = "policy_net_ret" if label == "policy" else f"{label}_net_ret"
        ret = pd.to_numeric(d[ret_col], errors="coerce")
        valid = ret.dropna()
        rows.append(
            {
                "口径": label,
                "样本数": int(valid.size),
                "平均收益": float(valid.mean()),
                "胜率": float((valid > 0).mean()),
                "最差单笔": float(valid.min()),
                "收益合计近似": float((d.loc[valid.index, "stake"] * valid).sum()),
            }
        )
    return pd.DataFrame(rows)


def paired_delay_summary(d: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for label in ["next_open", "next_close"]:
        ret_col = f"delay_{label}_net_ret"
        delta_col = f"delay_{label}_ret_delta"
        pnl_col = f"delay_{label}_pnl_delta"
        g = d[d[ret_col].notna()].copy()
        base = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        delayed = pd.to_numeric(g[ret_col], errors="coerce")
        rows.append(
            {
                "配对口径": label,
                "样本数": int(len(g)),
                "基准平均收益": float(base.mean()),
                "延迟平均收益": float(delayed.mean()),
                "基准胜率": float((base > 0).mean()),
                "延迟胜率": float((delayed > 0).mean()),
                "平均收益差": float(pd.to_numeric(g[delta_col], errors="coerce").mean()),
                "PnL差额": float(pd.to_numeric(g[pnl_col], errors="coerce").sum()),
            }
        )
    return pd.DataFrame(rows)


def grouped_summary(d: pd.DataFrame, group_col: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, g in d.groupby(group_col, dropna=False):
        row = {group_col: key, "交易数": int(len(g))}
        for ret_col in ["policy_net_ret", "delay_next_open_net_ret", "delay_next_close_net_ret"]:
            ret = pd.to_numeric(g[ret_col], errors="coerce")
            row[f"{ret_col}_avg"] = float(ret.mean())
            row[f"{ret_col}_win"] = float((ret > 0).mean())
        row["next_open_delta_pnl"] = float(pd.to_numeric(g["delay_next_open_pnl_delta"], errors="coerce").sum())
        row["next_close_delta_pnl"] = float(pd.to_numeric(g["delay_next_close_pnl_delta"], errors="coerce").sum())
        rows.append(row)
    return pd.DataFrame(rows)


def capacity_summary(d: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for col in ["stake_to_confirm_bar_amount", "stake_to_next_30m_amount", "stake_to_context_confirm_amount"]:
        s = pd.to_numeric(d[col], errors="coerce")
        rows.append(
            {
                "容量口径": col,
                "可计算样本": int(s.notna().sum()),
                "中位占比": float(s.median()),
                "最大占比": float(s.max()),
                ">1%": int((s > 0.01).sum()),
                ">3%": int((s > 0.03).sum()),
                ">5%": int((s > 0.05).sum()),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = load_bigbull_trades()
    ctx = load_context(trades)
    bars = load_30m_bars(trades)
    audit = attach_delay_prices(trades, ctx, bars)
    audit["entry_year"] = audit["entry_date"].dt.year

    delay = summarize_delay(audit)
    paired = paired_delay_summary(audit)
    by_year = grouped_summary(audit, "entry_year")
    by_style = grouped_summary(audit, "market_style_d1")
    capacity = capacity_summary(audit)
    missing = audit[audit["next_bar_missing"]].copy()

    audit.to_csv(OUT_DIR / "bigbull_30m_delay_capacity_trades.csv", index=False, encoding="utf-8-sig")
    delay.to_csv(OUT_DIR / "delay_summary.csv", index=False, encoding="utf-8-sig")
    paired.to_csv(OUT_DIR / "paired_delay_summary.csv", index=False, encoding="utf-8-sig")
    by_year.to_csv(OUT_DIR / "delay_by_year.csv", index=False, encoding="utf-8-sig")
    by_style.to_csv(OUT_DIR / "delay_by_market_style.csv", index=False, encoding="utf-8-sig")
    capacity.to_csv(OUT_DIR / "capacity_summary.csv", index=False, encoding="utf-8-sig")
    missing.to_csv(OUT_DIR / "missing_next_30m_trades.csv", index=False, encoding="utf-8-sig")

    lines = [
        "# G3 V4 bigbull 真实30m延迟成交与容量审计",
        "",
        "## 口径",
        "",
        "- 对象：最终候选 `uptrend_or_weak_rebound + quarter + D1弱确认全退出` 中实际成交的 bigbull 增强链路。",
        "- 基准：沿用 cost30 回测里的确认价买入与原退出逻辑。",
        "- 延迟成交：同一退出逻辑不变，只把买入价替换为确认 bar 后下一根 30m 的 `open` 或 `close`。",
        "- 容量：用单笔 stake 除以确认 bar、下一根 30m bar、原上下文确认金额，检查是否依赖小成交量。",
        "",
        "## 延迟成交汇总",
        "",
        md_table(delay, {"平均收益", "胜率", "最差单笔"}),
        "",
        "## 配对样本延迟影响",
        "",
        md_table(paired, {"基准平均收益", "延迟平均收益", "基准胜率", "延迟胜率", "平均收益差"}),
        "",
        "## 容量汇总",
        "",
        md_table(capacity, {"中位占比", "最大占比"}),
        "",
        "## 下一根30m缺失样本",
        "",
        md_table(missing[["entry_date", "code", "name", "confirm_datetime", "policy_net_ret", "market_style_d1"]], {"policy_net_ret"})
        if not missing.empty
        else "无缺失",
        "",
        "## 年度分组",
        "",
        md_table(by_year, {c for c in by_year.columns if c.endswith("_avg") or c.endswith("_win")}),
        "",
        "## 市场状态分组",
        "",
        md_table(by_style, {c for c in by_style.columns if c.endswith("_avg") or c.endswith("_win")}),
        "",
        "## 关键结论",
        "",
    ]
    open_delta = float(pd.to_numeric(audit["delay_next_open_pnl_delta"], errors="coerce").sum())
    close_delta = float(pd.to_numeric(audit["delay_next_close_pnl_delta"], errors="coerce").sum())
    lines.extend(
        [
            f"- 下一根 30m 缺失样本：{int(audit['next_bar_missing'].sum())}/{len(audit)}。",
            f"- 在可配对样本中，下一根 open 延迟成交对 bigbull 链路的近似 PnL 影响：{open_delta:.2f}。",
            f"- 在可配对样本中，下一根 close 延迟成交对 bigbull 链路的近似 PnL 影响：{close_delta:.2f}。",
            "- 若下一根 close 口径仍明显为正，说明突破确认后的可执行性尚可；若利润大幅消失，则说明这条链路更像吃理想确认价。",
            "- 容量占比目前不能按字面百分比直接下结论，因为 ClickHouse 30m `amount` 与上下文确认金额疑似不是元口径；本报告先保留三种金额字段，下一步应单独做金额单位校准。",
            "",
            "## 下一步目标",
            "",
            "如果延迟成交通过，下一步应继续做强势链路的“突破失败/冲高回落”入场质量剔除审计，而不是扩大 score/rank 过滤。",
        ]
    )
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
