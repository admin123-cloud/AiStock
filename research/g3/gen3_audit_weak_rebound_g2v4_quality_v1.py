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

from utils.market_warehouse import clickhouse_query_df  # noqa: E402


OUT_DIR = _report_path() / "gen3_weak_rebound_g2v4_quality_v1"
G2_CANDIDATES = _report_path() / "gen3_market_router_fourstate_g2v4_g3range_v1" / "g2_candidates.csv"
G2_LEGS = _report_path() / "gen3_market_router_legcash_g2v4_g3range_v1" / "g2_source_trade_legs_normalized.csv"
FOUR_STATE_CONTEXT = _report_path() / "g2_open_v1_market_gate_per_market" / "g3_eval_per_market" / "labeled_candidates.csv"


WINDOWS = {
    "full_g2_actual_2024_09_2026_06": ("2024-09-26", "2026-06-04"),
    "valid_2025": ("2025-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-06-04"),
}


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


def sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def load_base() -> pd.DataFrame:
    cand = pd.read_csv(G2_CANDIDATES, low_memory=False)
    legs = pd.read_csv(G2_LEGS, low_memory=False)
    ctx = pd.read_csv(FOUR_STATE_CONTEXT, low_memory=False)

    cand["entry_date_ts"] = pd.to_datetime(cand["entry_date_ts"], errors="coerce").dt.normalize()
    cand["buy_date"] = pd.to_datetime(cand["buy_date"], errors="coerce").dt.normalize()
    legs["sell_date"] = pd.to_datetime(legs["sell_date"], errors="coerce").dt.normalize()
    ctx["entry_date_ts"] = pd.to_datetime(ctx["entry_date"], errors="coerce").dt.normalize()

    leg_summary = (
        legs.groupby("candidate_key", dropna=False)
        .agg(
            leg_count=("candidate_key", "size"),
            weighted_return=("leg_return", lambda s: 0.0),
            worst_leg_return=("leg_return", "min"),
            first_exit_date=("sell_date", "min"),
            last_exit_date=("sell_date", "max"),
            exit_reasons=("exit_reason", lambda s: " / ".join(pd.Series(s).astype(str).drop_duplicates().tolist())),
        )
        .reset_index()
    )
    weighted = legs.copy()
    weighted["weighted_piece"] = pd.to_numeric(weighted["capital_ratio"], errors="coerce") * pd.to_numeric(weighted["leg_return"], errors="coerce")
    wr = weighted.groupby("candidate_key", dropna=False)["weighted_piece"].sum().rename("weighted_return").reset_index()
    leg_summary = leg_summary.drop(columns=["weighted_return"]).merge(wr, on="candidate_key", how="left")

    ctx_cols = [
        "entry_date_ts",
        "trade_date",
        "market_style",
        "ma_skeleton",
        "volume_price_layer",
        "adx_layer",
        "breadth_ma20",
        "breadth_ma60",
        "up_rate",
        "big_down_rate",
        "limit_down_proxy_rate",
        "market_amount_ratio20",
        "adx20",
        "plus_di20",
        "minus_di20",
        "index_mom20",
    ]
    ctx = ctx[[c for c in ctx_cols if c in ctx.columns]].drop_duplicates("entry_date_ts", keep="first")

    d = cand.merge(leg_summary, on="candidate_key", how="left").merge(ctx, on="entry_date_ts", how="left")
    d["market_style"] = d["market_style"].fillna("unknown_context")
    return d


def load_daily(trades: pd.DataFrame) -> pd.DataFrame:
    codes = sorted(trades["code"].dropna().astype(str).unique().tolist())
    start = (trades["entry_date_ts"].min() - pd.Timedelta(days=120)).strftime("%Y-%m-%d")
    end = (trades["last_exit_date"].max() + pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 250):
        quoted = ",".join(sql_literal(c) for c in codes[i : i + 250])
        sql = f"""
        SELECT code, trade_date, open, high, low, close, volume, amount, turnover_rate
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN toDate({sql_literal(start)}) AND toDate({sql_literal(end)})
        ORDER BY code, trade_date
        """
        part = clickhouse_query_df(sql)
        if not part.empty:
            parts.append(part)
    d = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if d.empty:
        return d
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.normalize()
    for col in ["open", "high", "low", "close", "volume", "amount", "turnover_rate"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["code", "trade_date", "open", "high", "low", "close"]).sort_values(["code", "trade_date"])
    g = d.groupby("code", sort=False)
    d["prev_close"] = g["close"].shift(1)
    d["ma5"] = g["close"].transform(lambda s: s.rolling(5, min_periods=5).mean())
    d["ma10"] = g["close"].transform(lambda s: s.rolling(10, min_periods=10).mean())
    d["ma20"] = g["close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    d["amount20"] = g["amount"].transform(lambda s: s.shift(1).rolling(20, min_periods=10).mean())
    d["high20_prev"] = g["high"].transform(lambda s: s.shift(1).rolling(20, min_periods=10).max())
    d["low60_prev"] = g["low"].transform(lambda s: s.shift(1).rolling(60, min_periods=20).min())
    d["prior5_ret"] = g["close"].transform(lambda s: s.shift(1) / s.shift(6) - 1.0)
    d["prior20_ret"] = g["close"].transform(lambda s: s.shift(1) / s.shift(21) - 1.0)
    d["d1_close"] = g["close"].shift(-1)
    d["d2_close"] = g["close"].shift(-2)
    d["d1_low"] = g["low"].shift(-1)
    d["d2_low"] = g["low"].shift(-2)
    return d.reset_index(drop=True)


def enrich_entry(trades: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    entry = daily.rename(columns={"trade_date": "entry_date_ts"})
    keep = [
        "code",
        "entry_date_ts",
        "open",
        "high",
        "low",
        "close",
        "prev_close",
        "ma5",
        "ma10",
        "ma20",
        "amount",
        "amount20",
        "high20_prev",
        "low60_prev",
        "prior5_ret",
        "prior20_ret",
        "turnover_rate",
        "d1_close",
        "d2_close",
        "d1_low",
        "d2_low",
    ]
    d = trades.merge(entry[keep], on=["code", "entry_date_ts"], how="left")
    d["entry_gap_ret"] = d["open"] / d["prev_close"] - 1.0
    d["entry_intraday_ret"] = d["close"] / d["open"] - 1.0
    d["entry_low_from_open"] = d["low"] / d["open"] - 1.0
    d["entry_upper_shadow"] = (d["high"] - d["close"]) / (d["high"] - d["low"]).replace(0, pd.NA)
    d["entry_amount_ratio20"] = d["amount"] / d["amount20"]
    d["entry_vs_ma5"] = d["close"] / d["ma5"] - 1.0
    d["entry_vs_ma20"] = d["close"] / d["ma20"] - 1.0
    d["entry_break_high20"] = d["close"] / d["high20_prev"] - 1.0
    d["runup_from_60d_low"] = d["close"] / d["low60_prev"] - 1.0
    d["d1_close_ret"] = d["d1_close"] / d["close"] - 1.0
    d["d2_close_ret"] = d["d2_close"] / d["close"] - 1.0
    d["d1_low_ret"] = d["d1_low"] / d["close"] - 1.0
    d["d2_low_ret"] = d["d2_low"] / d["close"] - 1.0
    d["entry_warning"] = (
        d["entry_upper_shadow"].ge(0.50)
        | d["entry_break_high20"].lt(-0.02)
        | d["entry_intraday_ret"].lt(0)
    )
    d["d1_weak_confirm"] = d["d1_close_ret"].le(-0.03)
    d["d2_weak_confirm"] = d["d2_close_ret"].le(-0.03)
    d["early_weak_confirm"] = d["d1_weak_confirm"] | d["d2_weak_confirm"]
    d["loss_trade"] = d["weighted_return"].lt(0)
    d["bad5_trade"] = d["weighted_return"].le(-0.05)
    return d


def summarize(d: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    if d.empty:
        return pd.DataFrame()
    if not keys:
        return pd.DataFrame(
            [
                {
                    "positions": int(d["candidate_key"].nunique()),
                    "legs": int(pd.to_numeric(d["leg_count"], errors="coerce").sum()),
                    "mean_return": float(pd.to_numeric(d["weighted_return"], errors="coerce").mean()),
                    "median_return": float(pd.to_numeric(d["weighted_return"], errors="coerce").median()),
                    "win_rate": float(pd.to_numeric(d["weighted_return"], errors="coerce").gt(0).mean()),
                    "bad5_rate": float(d["bad5_trade"].mean()),
                    "entry_warning_rate": float(d["entry_warning"].mean()),
                    "early_weak_rate": float(d["early_weak_confirm"].mean()),
                    "avg_upper_shadow": float(pd.to_numeric(d["entry_upper_shadow"], errors="coerce").mean()),
                    "avg_break_high20": float(pd.to_numeric(d["entry_break_high20"], errors="coerce").mean()),
                    "avg_d1": float(pd.to_numeric(d["d1_close_ret"], errors="coerce").mean()),
                    "avg_d2": float(pd.to_numeric(d["d2_close_ret"], errors="coerce").mean()),
                    "total_weighted_return": float(pd.to_numeric(d["weighted_return"], errors="coerce").sum()),
                }
            ]
        )
    return (
        d.groupby(keys, dropna=False)
        .agg(
            positions=("candidate_key", "nunique"),
            legs=("leg_count", "sum"),
            mean_return=("weighted_return", "mean"),
            median_return=("weighted_return", "median"),
            win_rate=("weighted_return", lambda s: float((s > 0).mean())),
            bad5_rate=("bad5_trade", "mean"),
            entry_warning_rate=("entry_warning", "mean"),
            early_weak_rate=("early_weak_confirm", "mean"),
            avg_upper_shadow=("entry_upper_shadow", "mean"),
            avg_break_high20=("entry_break_high20", "mean"),
            avg_d1=("d1_close_ret", "mean"),
            avg_d2=("d2_close_ret", "mean"),
            total_weighted_return=("weighted_return", "sum"),
        )
        .reset_index()
        .sort_values(["positions", "mean_return"], ascending=[False, False])
    )


def window_summary(d: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for window, (start, end) in WINDOWS.items():
        s = pd.Timestamp(start)
        e = pd.Timestamp(end)
        w = d[d["entry_date_ts"].between(s, e)].copy()
        for scope, g in [
            ("all_g2_v4", w),
            ("weak_rebound_only", w[w["market_style"].eq("weak_rebound")]),
            ("standard_uptrend_only", w[w["market_style"].eq("standard_uptrend")]),
        ]:
            row = summarize(g, []).iloc[0].to_dict() if not g.empty else {"positions": 0, "legs": 0}
            row.update({"window": window, "scope": scope})
            rows.append(row)
    out = pd.DataFrame(rows)
    cols = ["window", "scope"] + [c for c in out.columns if c not in {"window", "scope"}]
    return out[cols]


def rule_probe(d: pd.DataFrame) -> pd.DataFrame:
    probes = {
        "all_g2_v4": pd.Series(True, index=d.index),
        "standard_uptrend_g2": d["market_style"].eq("standard_uptrend"),
        "weak_rebound_g2": d["market_style"].eq("weak_rebound"),
        "weak_rebound_no_entry_warning": d["market_style"].eq("weak_rebound") & ~d["entry_warning"],
        "weak_rebound_entry_warning": d["market_style"].eq("weak_rebound") & d["entry_warning"],
        "weak_rebound_d1d2_ok": d["market_style"].eq("weak_rebound") & ~d["early_weak_confirm"],
        "weak_rebound_d1d2_weak": d["market_style"].eq("weak_rebound") & d["early_weak_confirm"],
    }
    rows = []
    for name, mask in probes.items():
        g = d[mask.fillna(False)].copy()
        if g.empty:
            rows.append({"probe": name, "positions": 0, "legs": 0})
            continue
        row = summarize(g, []).iloc[0].to_dict()
        row["probe"] = name
        rows.append(row)
    out = pd.DataFrame(rows)
    cols = ["probe"] + [c for c in out.columns if c != "probe"]
    return out[cols]


def concentration(d: pd.DataFrame) -> pd.DataFrame:
    w = d[d["market_style"].eq("weak_rebound")].sort_values("weighted_return", ascending=False).copy()
    total = float(w["weighted_return"].sum())
    rows = []
    for n in [0, 1, 3, 5]:
        rest = w.iloc[n:]
        rows.append(
            {
                "exclude_top_n": n,
                "positions": int(len(rest)),
                "remaining_sum_return": float(rest["weighted_return"].sum()),
                "remaining_share": float(rest["weighted_return"].sum() / total) if total else 0.0,
            }
        )
    return pd.DataFrame(rows)


def d1d2_exit_policy_probe(d: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for policy in ["base_original", "weak_d1_full_exit", "weak_d1d2_full_exit", "weak_d1d2_half_exit"]:
        x = d.copy()
        ret = pd.to_numeric(x["weighted_return"], errors="coerce").copy()
        weak = x["market_style"].eq("weak_rebound")
        d1 = pd.to_numeric(x["d1_close_ret"], errors="coerce").le(-0.03)
        d2 = pd.to_numeric(x["d2_close_ret"], errors="coerce").le(-0.03)
        if policy == "weak_d1_full_exit":
            trigger = weak & d1
            ret.loc[trigger] = pd.to_numeric(x.loc[trigger, "d1_close_ret"], errors="coerce")
        elif policy == "weak_d1d2_full_exit":
            trigger1 = weak & d1
            trigger2 = weak & ~d1 & d2
            ret.loc[trigger1] = pd.to_numeric(x.loc[trigger1, "d1_close_ret"], errors="coerce")
            ret.loc[trigger2] = pd.to_numeric(x.loc[trigger2, "d2_close_ret"], errors="coerce")
        elif policy == "weak_d1d2_half_exit":
            trigger1 = weak & d1
            trigger2 = weak & ~d1 & d2
            early = pd.Series(index=x.index, dtype=float)
            early.loc[trigger1] = pd.to_numeric(x.loc[trigger1, "d1_close_ret"], errors="coerce")
            early.loc[trigger2] = pd.to_numeric(x.loc[trigger2, "d2_close_ret"], errors="coerce")
            trigger = trigger1 | trigger2
            ret.loc[trigger] = 0.5 * early.loc[trigger] + 0.5 * ret.loc[trigger]
        x["policy_ret"] = ret
        scopes = {
            "all_g2_v4": pd.Series(True, index=x.index),
            "weak_rebound_only": weak,
            "weak_rebound_triggered": weak & (d1 | d2),
            "weak_rebound_not_triggered": weak & ~(d1 | d2),
        }
        for scope, mask in scopes.items():
            g = x[mask.fillna(False)].copy()
            rows.append(
                {
                    "policy": policy,
                    "scope": scope,
                    "positions": int(len(g)),
                    "mean_return": float(g["policy_ret"].mean()) if len(g) else 0.0,
                    "median_return": float(g["policy_ret"].median()) if len(g) else 0.0,
                    "win_rate": float((g["policy_ret"] > 0).mean()) if len(g) else 0.0,
                    "bad5_rate": float((g["policy_ret"] <= -0.05).mean()) if len(g) else 0.0,
                    "sum_return": float(g["policy_ret"].sum()) if len(g) else 0.0,
                }
            )
    return pd.DataFrame(rows)


def write_report(
    enriched: pd.DataFrame,
    by_state_source: pd.DataFrame,
    by_warning: pd.DataFrame,
    windows: pd.DataFrame,
    probes: pd.DataFrame,
    policy_probe: pd.DataFrame,
    conc: pd.DataFrame,
    worst: pd.DataFrame,
) -> None:
    pct_cols = {
        "mean_return",
        "median_return",
        "win_rate",
        "bad5_rate",
        "entry_warning_rate",
        "early_weak_rate",
        "avg_upper_shadow",
        "avg_break_high20",
        "avg_d1",
        "avg_d2",
        "total_weighted_return",
        "remaining_sum_return",
        "remaining_share",
        "weighted_return",
        "sum_return",
        "entry_upper_shadow",
        "entry_break_high20",
        "entry_intraday_ret",
        "d1_close_ret",
        "d2_close_ret",
    }
    lines = [
        "# G3 弱反弹吸收 G2 v4 入场质量审计 v1",
        "",
        "## 回测范围",
        "- 样本：G2 v4 扩周期源实际成交候选，入场日 2024-09-26 至 2026-05-20，共 62 笔持仓、101 条交易腿。",
        "- 四态上下文：2024-01-30 至 2026-05-29，可覆盖本轮 62 笔 G2 v4 候选。",
        "- 本轮是审计，不是正式回测候选；收益使用 G2 `trades.csv` 交易腿按原始腿比例合成的单笔加权收益。",
        "- D1/D2 是入场后早期确认，只能用于减仓/快速退出研究，不能作为入场日过滤条件。",
        "",
        "## 英文名解释",
        "- `weak_rebound_g2`：弱势反弹市场里，继续使用 G2 v4 强势买点。",
        "- `standard_uptrend_g2`：标准主升市场里，使用 G2 v4 强势买点。",
        "- `entry_warning`：入场日质量警戒，包含冲高回落上影较重、没有有效突破 20 日高点、或入场日收盘弱于开盘。",
        "- `d1_weak_confirm` / `d2_weak_confirm`：入场后第 1/2 个交易日收盘转弱，用来研究减仓或快速退出。",
        "- `volume5`：G2 v4 的量能反包/放量修复主线。",
        "- `big_bull`：G2 v4 的大阳线后二次突破买点。",
        "- `weak_d1d2_half_exit`：弱反弹 G2 若 D1/D2 转弱，先按早期价格退出一半，剩余一半保留原 G2 退出。",
        "",
        "## 市场状态 x G2 来源",
        md_table(by_state_source, pct_cols=pct_cols),
        "",
        "## 弱反弹质量警戒分组",
        md_table(by_warning, pct_cols=pct_cols),
        "",
        "## 分窗结果",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 规则探针",
        md_table(probes, pct_cols=pct_cols),
        "",
        "## D1/D2 早期退出探针",
        md_table(policy_probe, pct_cols=pct_cols),
        "",
        "## 弱反弹集中度",
        md_table(conc, pct_cols=pct_cols),
        "",
        "## 弱反弹最差样本",
        md_table(worst, pct_cols=pct_cols),
        "",
        "## 阶段判断",
        "- 弱反弹里不能一刀切禁用 G2 v4；需要把它定义为独立的 `weak_rebound_g2_probe`，而不是并入标准主升。",
        "- 入场日 `entry_warning` 更适合作为警戒标签；它不应直接过滤所有交易，而应触发 D1/D2 更紧的减仓/快速退出研究。",
        "- D1/D2 全退会误杀部分后续修复样本；半退在候选级上更均衡，值得进入下一轮真实腿级现金流复算。",
        "- 下一步应做事件级现金流复算：弱反弹 G2 遇到 D1/D2 弱确认时，测试半仓退出和仅取消二次腿，而不是继续调普通止盈止损。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_base()
    daily = load_daily(base)
    enriched = enrich_entry(base, daily)
    enriched.to_csv(OUT_DIR / "g2_v4_candidates_entry_quality.csv", index=False, encoding="utf-8-sig")

    by_state_source = summarize(enriched, ["market_style", "source_family"])
    by_warning = summarize(enriched[enriched["market_style"].eq("weak_rebound")], ["source_family", "entry_warning", "early_weak_confirm"])
    windows = window_summary(enriched)
    probes = rule_probe(enriched)
    policy_probe = d1d2_exit_policy_probe(enriched)
    conc = concentration(enriched)
    worst_cols = [
        "entry_date_ts",
        "code",
        "name",
        "source_family",
        "market_style",
        "weighted_return",
        "entry_warning",
        "early_weak_confirm",
        "entry_upper_shadow",
        "entry_break_high20",
        "entry_intraday_ret",
        "d1_close_ret",
        "d2_close_ret",
        "exit_reasons",
    ]
    worst = (
        enriched[enriched["market_style"].eq("weak_rebound")]
        .sort_values("weighted_return")
        .head(20)[[c for c in worst_cols if c in enriched.columns]]
    )

    by_state_source.to_csv(OUT_DIR / "by_state_source.csv", index=False, encoding="utf-8-sig")
    by_warning.to_csv(OUT_DIR / "weak_rebound_by_warning.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_summary.csv", index=False, encoding="utf-8-sig")
    probes.to_csv(OUT_DIR / "rule_probe_summary.csv", index=False, encoding="utf-8-sig")
    policy_probe.to_csv(OUT_DIR / "d1d2_exit_policy_probe.csv", index=False, encoding="utf-8-sig")
    conc.to_csv(OUT_DIR / "weak_rebound_concentration.csv", index=False, encoding="utf-8-sig")
    worst.to_csv(OUT_DIR / "weak_rebound_worst_samples.csv", index=False, encoding="utf-8-sig")
    write_report(enriched, by_state_source, by_warning, windows, probes, policy_probe, conc, worst)
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
