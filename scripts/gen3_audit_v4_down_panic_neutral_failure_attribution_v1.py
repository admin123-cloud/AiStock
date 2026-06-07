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
LOCK_TAGS = ROOT / "reports" / "gen3_v4_down_panic_all_lock5_v1" / "down_panic_lock_tags.csv"
OUT_DIR = ROOT / "reports" / "gen3_v4_down_panic_neutral_failure_attribution_v1"


def load_trades() -> pd.DataFrame:
    d = pd.read_csv(SOURCE, low_memory=False, encoding="utf-8-sig")
    d = d[d["route"].astype(str).eq("down_panic") & d["emotion_signal"].astype(str).eq("neutral")].copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in ["entry_price", "policy_net_ret", "stake", "realized_pnl", "score"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["entry_date", "policy_exit_date", "code", "entry_price", "policy_net_ret"]).copy()


def load_lock_tags() -> pd.DataFrame:
    if not LOCK_TAGS.exists():
        return pd.DataFrame()
    d = pd.read_csv(LOCK_TAGS, low_memory=False, encoding="utf-8-sig")
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    keep = ["entry_date", "code", "lock3_triggered", "lock3_net_ret", "lock5_triggered", "lock5_net_ret", "lock7_triggered", "lock7_net_ret"]
    for col in keep:
        if col.startswith("lock") and col.endswith("_net_ret"):
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d[keep].copy()


def load_daily(trades: pd.DataFrame) -> pd.DataFrame:
    codes = sorted(trades["code"].dropna().astype(str).unique().tolist())
    if not codes:
        return pd.DataFrame()
    start = trades["entry_date"].min() - pd.Timedelta(days=80)
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
    d["low60"] = d.groupby("code")["low"].transform(lambda s: s.shift(1).rolling(60, min_periods=20).min())
    d["high60"] = d.groupby("code")["high"].transform(lambda s: s.shift(1).rolling(60, min_periods=20).max())
    d["runup_from_60d_low"] = d["close"] / d["low60"] - 1.0
    d["drawdown_from_60d_high"] = d["close"] / d["high60"] - 1.0
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
    if d.empty:
        return d
    d["code"] = d["code"].astype(str)
    return d


def enrich_trades(trades: pd.DataFrame, daily: pd.DataFrame, meta: pd.DataFrame, locks: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    daily_by_code = {code: g.sort_values("trade_date").reset_index(drop=True) for code, g in daily.groupby("code")}
    for _, row in trades.iterrows():
        code = str(row["code"])
        entry = row["entry_date"]
        exit_date = row["policy_exit_date"]
        g = daily_by_code.get(code, pd.DataFrame())
        entry_bar = g[g["trade_date"].eq(entry)].head(1)
        prev_window = g[g["trade_date"].lt(entry)].tail(10)
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
            item["entry_amount_ratio20"] = eb.get("amount_ratio20")
            item["entry_turnover_rate"] = eb.get("turnover_rate")
            item["entry_runup_from_60d_low"] = eb.get("runup_from_60d_low")
            item["entry_drawdown_from_60d_high"] = eb.get("drawdown_from_60d_high")
        if not prev_window.empty:
            last_close = prev_window.iloc[-1]["close"]
            item["prev10_min_close_ret"] = prev_window["close"].min() / last_close - 1.0 if last_close > 0 else pd.NA
            item["prev10_max_down_day"] = (prev_window["close"] / prev_window["prev_close"] - 1.0).min()
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
    if not locks.empty:
        out = out.merge(locks, on=["entry_date", "code"], how="left")
    out["is_loss"] = pd.to_numeric(out["policy_net_ret"], errors="coerce").lt(0)
    out["failure_tag"] = "profitable"
    out.loc[out["is_loss"], "failure_tag"] = "residual_loss"
    loss = out["is_loss"]
    entry_close = pd.to_numeric(out.get("entry_close_ret_from_entry"), errors="coerce")
    d1_close = pd.to_numeric(out.get("d1_close_ret_from_entry"), errors="coerce")
    mfe = pd.to_numeric(out.get("mfe_high_ret"), errors="coerce")
    mae = pd.to_numeric(out.get("mae_low_ret"), errors="coerce")
    prev_down = pd.to_numeric(out.get("prev10_max_down_day"), errors="coerce")
    drawdown60 = pd.to_numeric(out.get("entry_drawdown_from_60d_high"), errors="coerce")
    out.loc[loss & prev_down.gt(-0.06) & drawdown60.gt(-0.18), "failure_tag"] = "not_real_capitulation"
    out.loc[loss & entry_close.lt(-0.02), "failure_tag"] = "entry_day_no_bid"
    out.loc[loss & d1_close.lt(-0.03), "failure_tag"] = "d1_weak_follow"
    out.loc[loss & mfe.gt(0.03), "failure_tag"] = "gave_back_profit_exit_slow"
    out.loc[loss & mae.lt(-0.08), "failure_tag"] = "deep_adverse_move"
    return out


def grouped(df: pd.DataFrame, col: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, g in df.groupby(col, dropna=False):
        ret = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        pnl = pd.to_numeric(g.get("realized_pnl"), errors="coerce")
        rows.append(
            {
                col: key,
                "trade_count": int(len(g)),
                "loss_count": int((ret < 0).sum()),
                "avg_ret": float(ret.mean()),
                "win_rate": float((ret > 0).mean()),
                "pnl": float(pnl.sum()) if not pnl.empty else 0.0,
                "avg_mfe": float(pd.to_numeric(g.get("mfe_high_ret"), errors="coerce").mean()),
                "avg_mae": float(pd.to_numeric(g.get("mae_low_ret"), errors="coerce").mean()),
                "avg_prev10_down": float(pd.to_numeric(g.get("prev10_max_down_day"), errors="coerce").mean()),
                "avg_drawdown60": float(pd.to_numeric(g.get("entry_drawdown_from_60d_high"), errors="coerce").mean()),
            }
        )
    return pd.DataFrame(rows).sort_values(["pnl", "avg_ret"], ascending=[True, True])


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = load_trades()
    daily = load_daily(trades)
    locks = load_lock_tags()
    meta = load_stock_meta(trades)
    enriched = enrich_trades(trades, daily, meta, locks)
    enriched["year"] = pd.to_datetime(enriched["entry_date"], errors="coerce").dt.year
    enriched.to_csv(OUT_DIR / "down_panic_neutral_trades_enriched.csv", index=False, encoding="utf-8-sig")

    by_failure = grouped(enriched, "failure_tag")
    by_year = grouped(enriched, "year")
    by_industry = grouped(enriched, "industry")
    by_lock5 = grouped(enriched, "lock5_triggered")
    losers = enriched[enriched["is_loss"]].sort_values("policy_net_ret")
    winners = enriched[~enriched["is_loss"]].sort_values("policy_net_ret", ascending=False)
    for name, df in [
        ("by_failure_tag.csv", by_failure),
        ("by_year.csv", by_year),
        ("by_industry.csv", by_industry),
        ("by_lock5.csv", by_lock5),
        ("losing_trades.csv", losers),
        ("winning_trades.csv", winners),
    ]:
        df.to_csv(OUT_DIR / name, index=False, encoding="utf-8-sig")

    pct_cols = {"avg_ret", "win_rate", "avg_mfe", "avg_mae", "avg_prev10_down", "avg_drawdown60", "policy_net_ret", "entry_close_ret_from_entry", "d1_close_ret_from_entry", "mfe_high_ret", "mae_low_ret", "prev10_max_down_day", "entry_drawdown_from_60d_high"}
    lines = [
        "# G3 V4 down_panic neutral 失败归因 v1",
        "",
        "## 对象",
        "",
        "- 使用 base cost30 实际成交的 `route=down_panic` 且 `emotion_signal=neutral` 交易。",
        f"- 实际成交笔数：`{len(enriched)}`；亏损笔数：`{int(enriched['is_loss'].sum())}`。",
        "- 本轮不引入 score/rank 过滤，只解释为什么 neutral 弱势 panic 不能直接套冰点打法。",
        "",
        "## 失败类型",
        "",
        md_table(by_failure, pct_cols=pct_cols),
        "",
        "## 年度归因",
        "",
        md_table(by_year, pct_cols=pct_cols),
        "",
        "## lock5 触发归因",
        "",
        md_table(by_lock5, pct_cols=pct_cols),
        "",
        "## 行业归因",
        "",
        md_table(by_industry.head(20), pct_cols=pct_cols),
        "",
        "## 最大亏损样本",
        "",
        md_table(losers[["entry_date", "policy_exit_date", "code", "name", "industry", "policy_net_ret", "failure_tag", "entry_close_ret_from_entry", "d1_close_ret_from_entry", "mfe_high_ret", "mae_low_ret", "prev10_max_down_day", "entry_drawdown_from_60d_high"]].head(20), pct_cols=pct_cols),
        "",
        "## 最大盈利样本",
        "",
        md_table(winners[["entry_date", "policy_exit_date", "code", "name", "industry", "policy_net_ret", "entry_close_ret_from_entry", "d1_close_ret_from_entry", "mfe_high_ret", "mae_low_ret", "prev10_max_down_day", "entry_drawdown_from_60d_high"]].head(15), pct_cols=pct_cols),
        "",
        "## 下一步判断口径",
        "",
        "- 如果亏损集中在 `not_real_capitulation`，说明 neutral 问题是入场源不够恐慌，应该重新定义弱势反弹触发源。",
        "- 如果亏损集中在 `gave_back_profit_exit_slow`，说明 neutral 仍有交易价值，但要独立退出纪律。",
        "- 如果亏损集中在个别年份/行业，才考虑风格暂停；否则不要加过窄过滤。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
