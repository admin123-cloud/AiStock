from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_query_df  # noqa: E402


AUDIT = ROOT / "reports" / "gen3_weak_rebound_g2v4_quality_v1" / "g2_v4_candidates_entry_quality.csv"
OUT_DIR = ROOT / "reports" / "gen3_weak_rebound_g2v4_entry_30m_accept_v1"


def sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def pct(x: Any) -> str:
    if x is None or pd.isna(x):
        return "--"
    return f"{float(x) * 100:+.2f}%"


def num(x: Any) -> str:
    if x is None or pd.isna(x):
        return "--"
    if isinstance(x, float):
        return f"{x:.4f}"
    return str(x)


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
        for col in df.columns:
            value = row[col]
            item[col] = pct(value) if col in pct_cols else num(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def load_candidates() -> pd.DataFrame:
    d = pd.read_csv(AUDIT, low_memory=False)
    d["entry_date_ts"] = pd.to_datetime(d["entry_date_ts"], errors="coerce").dt.normalize()
    d["buy_datetime"] = pd.to_datetime(d["buy_datetime"], errors="coerce")
    for col in [
        "buy_price",
        "close",
        "weighted_return",
        "d1_close_ret",
        "d2_close_ret",
        "entry_upper_shadow",
        "entry_intraday_ret",
        "entry_break_high20",
    ]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    for col in ["entry_warning", "early_weak_confirm", "bad5_trade", "loss_trade"]:
        d[col] = d[col].map({"True": True, "False": False, True: True, False: False}).fillna(False)
    w = d[d["market_style"].eq("weak_rebound")].copy()
    return w.sort_values("entry_date_ts")


def load_30m_bars(candidates: pd.DataFrame) -> pd.DataFrame:
    if candidates.empty:
        return pd.DataFrame()
    codes = sorted(candidates["code"].dropna().astype(str).unique().tolist())
    start = candidates["entry_date_ts"].min().strftime("%Y-%m-%d")
    end = candidates["entry_date_ts"].max().strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 200):
        quoted = ",".join(sql_literal(c) for c in codes[i : i + 200])
        sql = f"""
        SELECT code, datetime, open, high, low, close, amount
        FROM kline_minute_30
        WHERE code IN ({quoted})
          AND datetime >= toDateTime({sql_literal(start + " 09:00:00")})
          AND datetime <= toDateTime({sql_literal(end + " 15:30:00")})
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
    bars["entry_date_ts"] = bars["datetime"].dt.normalize()
    bars["bar_time"] = bars["datetime"].dt.strftime("%H:%M:%S")
    for col in ["open", "high", "low", "close", "amount"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    bars = bars.dropna(subset=["code", "datetime", "open", "high", "low", "close"]).sort_values(["code", "entry_date_ts", "datetime"])
    g = bars.groupby(["code", "entry_date_ts"], sort=False)
    bars["amount_ma3_prev"] = g["amount"].transform(lambda s: s.shift(1).rolling(3, min_periods=1).mean())
    bars["prev_bar_high"] = g["high"].shift(1)
    bar_range = bars["high"] - bars["low"]
    bars["bar_close_pos"] = np.where(bar_range > 0, (bars["close"] - bars["low"]) / bar_range, np.nan)
    bars["bar_ret"] = bars["close"] / bars["open"] - 1.0
    bars["amount_ratio3"] = bars["amount"] / bars["amount_ma3_prev"]
    return bars.replace([np.inf, -np.inf], np.nan)


def audit_candidate(row: pd.Series, bars: pd.DataFrame) -> dict[str, Any]:
    key = str(row["candidate_key"])
    buy_dt = pd.Timestamp(row["buy_datetime"])
    day_bars = bars[(bars["code"].eq(str(row["code"]))) & (bars["entry_date_ts"].eq(pd.Timestamp(row["entry_date_ts"])))]
    post = day_bars[day_bars["datetime"].ge(buy_dt)].copy()
    buy_price = float(row["buy_price"])
    if post.empty:
        return {
            "candidate_key": key,
            "bar_count_after_buy": 0,
            "basic_accept": False,
            "strong_accept": False,
            "fail_after_buy": False,
        }
    basic = (
        post["close"].gt(post["open"])
        & post["bar_close_pos"].ge(0.60)
        & post["amount_ratio3"].fillna(0.0).ge(1.00)
        & post["close"].ge(buy_price * 0.99)
    )
    strong = (
        post["close"].gt(post["open"])
        & post["bar_close_pos"].ge(0.70)
        & post["amount_ratio3"].fillna(0.0).ge(1.20)
        & post["close"].ge(buy_price)
        & post["close"].gt(post["prev_bar_high"].fillna(-np.inf))
    )
    fail = post["close"].le(buy_price * 0.97) | (post["close"].lt(post["open"]) & post["bar_close_pos"].le(0.25))
    first_basic = post[basic].head(1)
    first_strong = post[strong].head(1)
    worst_close_ret = float((post["close"] / buy_price - 1.0).min())
    best_close_ret = float((post["close"] / buy_price - 1.0).max())
    eod_ret = float(post["close"].iloc[-1] / buy_price - 1.0 - 0.003)
    return {
        "candidate_key": key,
        "bar_count_after_buy": int(len(post)),
        "basic_accept": bool(basic.any()),
        "strong_accept": bool(strong.any()),
        "fail_after_buy": bool(fail.any()),
        "basic_accept_count": int(basic.sum()),
        "strong_accept_count": int(strong.sum()),
        "first_basic_time": first_basic["bar_time"].iloc[0] if not first_basic.empty else "",
        "first_strong_time": first_strong["bar_time"].iloc[0] if not first_strong.empty else "",
        "best_post_close_ret": best_close_ret,
        "worst_post_close_ret": worst_close_ret,
        "eod_exit_ret_cost30": eod_ret,
        "post_amount_ratio3_max": float(post["amount_ratio3"].max()) if post["amount_ratio3"].notna().any() else np.nan,
        "post_close_pos_max": float(post["bar_close_pos"].max()) if post["bar_close_pos"].notna().any() else np.nan,
    }


def summarize(d: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    if d.empty:
        return pd.DataFrame()
    return (
        d.groupby(keys, dropna=False)
        .agg(
            positions=("candidate_key", "nunique"),
            legs=("leg_count", "sum"),
            mean_return=("weighted_return", "mean"),
            median_return=("weighted_return", "median"),
            sum_return=("weighted_return", "sum"),
            win_rate=("weighted_return", lambda s: float((pd.to_numeric(s, errors="coerce") > 0).mean())),
            bad5_rate=("weighted_return", lambda s: float((pd.to_numeric(s, errors="coerce") <= -0.05).mean())),
            early_weak_rate=("early_weak_confirm", "mean"),
            entry_warning_rate=("entry_warning", "mean"),
            fail_rate=("fail_after_buy", "mean"),
            eod_exit_mean=("eod_exit_ret_cost30", "mean"),
        )
        .reset_index()
    )


def policy_probe(d: pd.DataFrame) -> pd.DataFrame:
    rows = []
    policies = {
        "base_original": pd.Series(d["weighted_return"].values, index=d.index),
        "no_basic_accept_full_eod_exit": d["weighted_return"].where(d["basic_accept"], d["eod_exit_ret_cost30"]),
        "no_basic_accept_half_eod_exit": d["weighted_return"].where(d["basic_accept"], 0.5 * d["eod_exit_ret_cost30"] + 0.5 * d["weighted_return"]),
        "no_strong_accept_full_eod_exit": d["weighted_return"].where(d["strong_accept"], d["eod_exit_ret_cost30"]),
        "no_strong_accept_half_eod_exit": d["weighted_return"].where(d["strong_accept"], 0.5 * d["eod_exit_ret_cost30"] + 0.5 * d["weighted_return"]),
    }
    for name, ret in policies.items():
        x = d.copy()
        x["probe_return"] = pd.to_numeric(ret, errors="coerce")
        for source, g in x.groupby("source_family", dropna=False):
            rows.append(
                {
                    "policy": name,
                    "source_family": source,
                    "positions": int(g["candidate_key"].nunique()),
                    "mean_return": float(g["probe_return"].mean()),
                    "median_return": float(g["probe_return"].median()),
                    "sum_return": float(g["probe_return"].sum()),
                    "win_rate": float((g["probe_return"] > 0).mean()),
                    "bad5_rate": float((g["probe_return"] <= -0.05).mean()),
                }
            )
        rows.append(
            {
                "policy": name,
                "source_family": "all",
                "positions": int(x["candidate_key"].nunique()),
                "mean_return": float(x["probe_return"].mean()),
                "median_return": float(x["probe_return"].median()),
                "sum_return": float(x["probe_return"].sum()),
                "win_rate": float((x["probe_return"] > 0).mean()),
                "bad5_rate": float((x["probe_return"] <= -0.05).mean()),
            }
        )
    return pd.DataFrame(rows)


def write_report(enriched: pd.DataFrame, by_accept: pd.DataFrame, by_source: pd.DataFrame, policies: pd.DataFrame, samples: pd.DataFrame) -> None:
    pct_cols = {
        "mean_return",
        "median_return",
        "sum_return",
        "win_rate",
        "bad5_rate",
        "early_weak_rate",
        "entry_warning_rate",
        "fail_rate",
        "eod_exit_mean",
        "weighted_return",
        "eod_exit_ret_cost30",
        "best_post_close_ret",
        "worst_post_close_ret",
        "entry_upper_shadow",
        "d1_close_ret",
        "d2_close_ret",
    }
    lines = [
        "# G3 weak_rebound G2 v4 入场日 30m 承接审计 v1",
        "",
        "## 回测时间与口径",
        "- 审计对象：G2 v4 实际成交窗口 2024-09-26 至 2026-05-20 中，被四状态标记为 `weak_rebound` 的 12 笔持仓 / 21 条交易腿。",
        "- 使用数据：ClickHouse `kline_minute_30`，只看买入当日、买入时间之后的 30m K 线。",
        "- 本轮是候选级审计和策略探针，不是最终逐日 MTM；如果候选级有效，下一轮再接入现金流复算。",
        "- 避免过拟合：只测试两档朴素承接定义，没有做阈值网格搜索。",
        "",
        "## 英文名解释",
        "- `basic_accept`：基础承接，买入后 30m K 线收阳、收盘位置在本 K 线 60% 以上、成交额不低于前 3 根均值、收盘不低于买入价 -1%。",
        "- `strong_accept`：强承接，在基础上要求收盘位置 70% 以上、成交额至少前 3 根 1.2 倍、收盘高于买入价并突破前一根 30m 高点。",
        "- `fail_after_buy`：买入后失败，30m 收盘跌破买入价 -3%，或阴线且收盘位置低于 25%。",
        "- `no_basic_accept_full_eod_exit`：如果买入当天没有基础承接，则按当天最后一根 30m 收盘估算全退出，扣 30bps。",
        "- `no_basic_accept_half_eod_exit`：如果没有基础承接，则当天先退出一半，另一半保留原 G2 退出。",
        "",
        "## 承接分组结果",
        md_table(by_accept, pct_cols=pct_cols),
        "",
        "## 来源分组结果",
        md_table(by_source, pct_cols=pct_cols),
        "",
        "## 策略探针",
        md_table(policies, pct_cols=pct_cols),
        "",
        "## 样本明细",
        md_table(samples, pct_cols=pct_cols),
        "",
        "## 阶段判断",
        "- 如果 `basic_accept=False` 的样本收益明显差，且 EOD 退出探针改善坏样本，则 30m 承接可进入逐日 MTM。",
        "- 如果承接标签不能区分收益，说明问题不是买入后承接，而应回到入场日突破失败/冲高回落结构。",
        "- 这一步不作为实盘规则，只作为 G3 weak_rebound 是否吸收 G2 的质量确认研究。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    candidates = load_candidates()
    bars = load_30m_bars(candidates)
    audit_rows = [audit_candidate(row, bars) for _, row in candidates.iterrows()]
    tags = pd.DataFrame(audit_rows)
    enriched = candidates.merge(tags, on="candidate_key", how="left")
    by_accept = summarize(enriched, ["basic_accept", "strong_accept", "fail_after_buy"])
    by_source = summarize(enriched, ["source_family", "basic_accept"])
    policies = policy_probe(enriched)
    sample_cols = [
        "entry_date_ts",
        "code",
        "name",
        "source_family",
        "buy_datetime",
        "weighted_return",
        "basic_accept",
        "strong_accept",
        "fail_after_buy",
        "first_basic_time",
        "first_strong_time",
        "eod_exit_ret_cost30",
        "best_post_close_ret",
        "worst_post_close_ret",
        "entry_warning",
        "early_weak_confirm",
        "d1_close_ret",
        "d2_close_ret",
        "exit_reasons",
    ]
    samples = enriched[[c for c in sample_cols if c in enriched.columns]].sort_values("entry_date_ts")

    bars.to_csv(OUT_DIR / "entry_day_30m_bars.csv", index=False, encoding="utf-8-sig")
    enriched.to_csv(OUT_DIR / "weak_rebound_30m_accept_audit.csv", index=False, encoding="utf-8-sig")
    by_accept.to_csv(OUT_DIR / "by_acceptance.csv", index=False, encoding="utf-8-sig")
    by_source.to_csv(OUT_DIR / "by_source_acceptance.csv", index=False, encoding="utf-8-sig")
    policies.to_csv(OUT_DIR / "policy_probe.csv", index=False, encoding="utf-8-sig")
    samples.to_csv(OUT_DIR / "sample_details.csv", index=False, encoding="utf-8-sig")
    write_report(enriched, by_accept, by_source, policies, samples)
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
