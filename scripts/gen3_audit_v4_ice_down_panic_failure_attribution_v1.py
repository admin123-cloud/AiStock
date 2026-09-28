from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_confirm_d3_execution_stress_v1 import _sql_literal  # noqa: E402
from scripts.gen3_test_v4_strong_position_scale_v1 import md_table  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


SOURCE = ROOT / "reports" / "gen3_v4_ice_down_panic_30m_confirm_overlay_v1" / "base__cost30" / "closed_trades.csv"
OUT_DIR = ROOT / "reports" / "gen3_v4_ice_down_panic_failure_attribution_v1"


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def load_trades() -> pd.DataFrame:
    d = pd.read_csv(SOURCE, low_memory=False, encoding="utf-8-sig")
    d = d[d["route"].astype(str).eq("down_panic") & d["emotion_signal"].astype(str).eq("icepoint")].copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in ["entry_price", "policy_net_ret", "stake", "realized_pnl", "score"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["entry_date", "policy_exit_date", "code", "entry_price", "policy_net_ret"]).copy()


def load_daily(trades: pd.DataFrame) -> pd.DataFrame:
    codes = sorted(trades["code"].dropna().astype(str).unique().tolist())
    start = trades["entry_date"].min() - pd.Timedelta(days=35)
    end = trades["policy_exit_date"].max() + pd.Timedelta(days=5)
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 250):
        quoted = ",".join(_sql_literal(code) for code in codes[i : i + 250])
        sql = f"""
        SELECT code, trade_date, open, high, low, close, amount, turnover_rate
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN toDate({_sql_literal(start.strftime('%Y-%m-%d'))})
                             AND toDate({_sql_literal(end.strftime('%Y-%m-%d'))})
        ORDER BY code, trade_date
        """
        part = clickhouse_query_df(sql)
        if not part.empty:
            parts.append(part)
    d = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if d.empty:
        return d
    d["code"] = d["code"].astype(str)
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.normalize()
    for col in ["open", "high", "low", "close", "amount", "turnover_rate"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["code", "trade_date", "open", "high", "low", "close"]).sort_values(["code", "trade_date"])
    d["prev_close"] = d.groupby("code")["close"].shift(1)
    d["amount20"] = d.groupby("code")["amount"].transform(lambda s: s.shift(1).rolling(20, min_periods=5).mean())
    d["amount_ratio20"] = d["amount"] / d["amount20"]
    return d.replace([float("inf"), -float("inf")], pd.NA)


def load_stock_meta(trades: pd.DataFrame) -> pd.DataFrame:
    codes = sorted(trades["code"].dropna().astype(str).unique().tolist())
    if not codes:
        return pd.DataFrame()
    quoted = ",".join(_sql_literal(code) for code in codes)
    sql = f"""
    SELECT code, name AS stock_name, industry
    FROM stocks
    WHERE code IN ({quoted})
    """
    d = clickhouse_query_df(sql)
    d["code"] = d["code"].astype(str)
    return d


def enrich_trades(trades: pd.DataFrame, daily: pd.DataFrame, meta: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    daily_by_code = {code: g.sort_values("trade_date").reset_index(drop=True) for code, g in daily.groupby("code")}
    for _, row in trades.iterrows():
        code = str(row["code"])
        entry = row["entry_date"]
        exit_date = row["policy_exit_date"]
        g = daily_by_code.get(code, pd.DataFrame())
        entry_bar = g[g["trade_date"].eq(entry)].head(1)
        holding = g[(g["trade_date"].ge(entry)) & (g["trade_date"].le(exit_date))].copy()
        d1 = g[g["trade_date"].gt(entry)].head(1)
        d2 = g[g["trade_date"].gt(entry)].head(2).tail(1)
        entry_price = float(row["entry_price"])
        item = row.to_dict()
        if not entry_bar.empty:
            eb = entry_bar.iloc[0]
            item["entry_open_ret_vs_prev"] = eb["open"] / eb["prev_close"] - 1.0 if pd.notna(eb.get("prev_close")) and eb.get("prev_close", 0) > 0 else pd.NA
            item["entry_close_ret_from_entry"] = eb["close"] / entry_price - 1.0
            item["entry_low_ret_from_entry"] = eb["low"] / entry_price - 1.0
            item["entry_high_ret_from_entry"] = eb["high"] / entry_price - 1.0
            item["entry_amount"] = eb.get("amount")
            item["entry_amount_ratio20"] = eb.get("amount_ratio20")
            item["entry_turnover_rate"] = eb.get("turnover_rate")
        if not d1.empty:
            item["d1_close_ret_from_entry"] = d1.iloc[0]["close"] / entry_price - 1.0
            item["d1_low_ret_from_entry"] = d1.iloc[0]["low"] / entry_price - 1.0
        if not d2.empty:
            item["d2_close_ret_from_entry"] = d2.iloc[0]["close"] / entry_price - 1.0
            item["d2_low_ret_from_entry"] = d2.iloc[0]["low"] / entry_price - 1.0
        if not holding.empty:
            item["holding_days"] = int(len(holding))
            item["mfe_high_ret"] = holding["high"].max() / entry_price - 1.0
            item["mae_low_ret"] = holding["low"].min() / entry_price - 1.0
            item["best_close_ret"] = holding["close"].max() / entry_price - 1.0
            item["worst_close_ret"] = holding["close"].min() / entry_price - 1.0
            item["exit_close_ret_from_entry"] = holding.iloc[-1]["close"] / entry_price - 1.0
        rows.append(item)
    out = pd.DataFrame(rows)
    if not meta.empty:
        out = out.merge(meta[["code", "industry"]], on="code", how="left")
    out["is_loss"] = pd.to_numeric(out["policy_net_ret"], errors="coerce").lt(0)
    out["failure_tag"] = "winner"
    loss = out["is_loss"]
    out.loc[loss & pd.to_numeric(out.get("entry_close_ret_from_entry"), errors="coerce").lt(-0.02), "failure_tag"] = "entry_day_no_bid"
    out.loc[loss & pd.to_numeric(out.get("d1_close_ret_from_entry"), errors="coerce").lt(-0.03), "failure_tag"] = "d1_weak_follow"
    out.loc[loss & pd.to_numeric(out.get("mfe_high_ret"), errors="coerce").gt(0.03), "failure_tag"] = "gave_back_profit_exit_slow"
    out.loc[loss & pd.to_numeric(out.get("mae_low_ret"), errors="coerce").lt(-0.08), "failure_tag"] = "deep_adverse_move"
    return out


def grouped(df: pd.DataFrame, col: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, g in df.groupby(col, dropna=False):
        ret = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        rows.append(
            {
                col: key,
                "trade_count": int(len(g)),
                "loss_count": int((ret < 0).sum()),
                "avg_ret": float(ret.mean()),
                "win_rate": float((ret > 0).mean()),
                "pnl": float(pd.to_numeric(g["realized_pnl"], errors="coerce").sum()),
                "avg_mfe": float(pd.to_numeric(g.get("mfe_high_ret"), errors="coerce").mean()),
                "avg_mae": float(pd.to_numeric(g.get("mae_low_ret"), errors="coerce").mean()),
            }
        )
    return pd.DataFrame(rows).sort_values(["pnl", "avg_ret"], ascending=[True, True])


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = load_trades()
    daily = load_daily(trades)
    meta = load_stock_meta(trades)
    enriched = enrich_trades(trades, daily, meta)
    enriched["year"] = pd.to_datetime(enriched["entry_date"], errors="coerce").dt.year
    enriched.to_csv(OUT_DIR / "ice_down_panic_trades_enriched.csv", index=False, encoding="utf-8-sig")

    by_failure = grouped(enriched, "failure_tag")
    by_year = grouped(enriched, "year")
    by_industry = grouped(enriched, "industry")
    by_strict = grouped(enriched, "strict_1030_confirmed")
    losers = enriched[enriched["is_loss"]].sort_values("policy_net_ret")
    losers.to_csv(OUT_DIR / "losing_trades.csv", index=False, encoding="utf-8-sig")
    by_failure.to_csv(OUT_DIR / "by_failure_tag.csv", index=False, encoding="utf-8-sig")
    by_year.to_csv(OUT_DIR / "by_year.csv", index=False, encoding="utf-8-sig")
    by_industry.to_csv(OUT_DIR / "by_industry.csv", index=False, encoding="utf-8-sig")
    by_strict.to_csv(OUT_DIR / "by_strict1030.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"avg_ret", "win_rate", "avg_mfe", "avg_mae"}
    lines = [
        "# G3 V4 icepoint + down_panic 失败归因 v1",
        "",
        "## 对象",
        "",
        "- 使用 base cost30 实际成交的 `emotion_signal=icepoint` 且 `route=down_panic` 交易。",
        f"- 实际成交笔数：`{len(enriched)}`；亏损笔数：`{int(enriched['is_loss'].sum())}`。",
        "",
        "## 失败类型",
        "",
        md_table(by_failure, pct_cols=pct_cols),
        "",
        "## 年度归因",
        "",
        md_table(by_year, pct_cols=pct_cols),
        "",
        "## 行业归因",
        "",
        md_table(by_industry.head(20), pct_cols=pct_cols),
        "",
        "## strict1030 分组",
        "",
        md_table(by_strict, pct_cols=pct_cols),
        "",
        "## 最大亏损样本",
        "",
        md_table(losers[["entry_date", "policy_exit_date", "code", "name", "industry", "policy_net_ret", "failure_tag", "entry_close_ret_from_entry", "d1_close_ret_from_entry", "mfe_high_ret", "mae_low_ret"]].head(20), pct_cols={"policy_net_ret", "entry_close_ret_from_entry", "d1_close_ret_from_entry", "mfe_high_ret", "mae_low_ret"}),
        "",
        "## 下一步判断",
        "",
        "- 如果主要失败是 `gave_back_profit_exit_slow`，优先研究退出，而不是入场过滤。",
        "- 如果主要失败是 `entry_day_no_bid` / `d1_weak_follow`，优先研究开仓后早弱减仓或快速退出。",
        "- 如果行业集中贡献亏损，再考虑行业/风格层面的暂停标签。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
