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
from scripts.gen3_test_v4_strong_position_scale_v1 import md_table, simulate_scaled, summarize  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


SOURCE = _report_path() / "gen3_v4_ice_down_panic_30m_confirm_overlay_v1" / "base_candidates_with_ice_30m_confirm.csv"
OUT_DIR = _report_path() / "gen3_v4_down_panic_neutral_entry_structure_v1"
BASE_COST_BPS = 30.0

PROFILES = [
    {"profile": "cost30", "cost_bps": 30.0, "all_shock": 0.0},
    {"profile": "cost100", "cost_bps": 100.0, "all_shock": 0.0},
    {"profile": "cost30_all_shock2", "cost_bps": 30.0, "all_shock": 0.02},
]

VARIANTS = [
    {"variant": "base", "filter": "none"},
    {"variant": "neutral_deep_dd30", "filter": "deep_dd30"},
    {"variant": "neutral_prev10_down8", "filter": "prev10_down8"},
    {"variant": "neutral_deep_or_prev10_down8", "filter": "deep_or_prev10_down8"},
    {"variant": "neutral_deep_and_amount1", "filter": "deep_and_amount1"},
]


def load_candidates() -> pd.DataFrame:
    d = pd.read_csv(SOURCE, low_memory=False, encoding="utf-8-sig")
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in ["entry_price", "policy_net_ret", "position_scale", "score", "route_priority"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["position_scale"] = d.get("position_scale", 1.0).fillna(1.0)
    return d.dropna(subset=["entry_date", "policy_exit_date", "code", "entry_price", "policy_net_ret"]).copy()


def load_daily(candidates: pd.DataFrame) -> pd.DataFrame:
    target = candidates[candidates["route"].astype(str).eq("down_panic") & candidates["emotion_signal"].astype(str).eq("neutral")].copy()
    codes = sorted(target["code"].dropna().astype(str).unique().tolist())
    if not codes:
        return pd.DataFrame()
    start = target["entry_date"].min() - pd.Timedelta(days=100)
    end = target["entry_date"].max() + pd.Timedelta(days=3)
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


def enrich_neutral(candidates: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    daily_by_code = {code: g.sort_values("trade_date").reset_index(drop=True) for code, g in daily.groupby("code")}
    target = candidates[candidates["route"].astype(str).eq("down_panic") & candidates["emotion_signal"].astype(str).eq("neutral")].copy()
    for _, row in target.iterrows():
        code = str(row["code"])
        entry = pd.Timestamp(row["entry_date"]).normalize()
        g = daily_by_code.get(code, pd.DataFrame())
        entry_bar = g[g["trade_date"].eq(entry)].head(1)
        prev_window = g[g["trade_date"].lt(entry)].tail(10)
        item = {"entry_date": entry, "code": code}
        if not entry_bar.empty:
            eb = entry_bar.iloc[0]
            item["entry_amount_ratio20"] = eb.get("amount_ratio20")
            item["entry_runup_from_60d_low"] = eb.get("runup_from_60d_low")
            item["entry_drawdown_from_60d_high"] = eb.get("drawdown_from_60d_high")
            item["entry_close_ret_vs_prev"] = eb["close"] / eb["prev_close"] - 1.0 if pd.notna(eb.get("prev_close")) and eb.get("prev_close", 0) > 0 else pd.NA
        if not prev_window.empty:
            item["prev10_max_down_day"] = (prev_window["close"] / prev_window["prev_close"] - 1.0).min()
            item["prev10_min_close_ret"] = prev_window["close"].min() / prev_window.iloc[-1]["close"] - 1.0 if prev_window.iloc[-1]["close"] > 0 else pd.NA
        rows.append(item)
    feat = pd.DataFrame(rows)
    drop_cols = [
        "entry_amount_ratio20",
        "entry_runup_from_60d_low",
        "entry_drawdown_from_60d_high",
        "entry_close_ret_vs_prev",
        "prev10_max_down_day",
        "prev10_min_close_ret",
    ]
    base = candidates.drop(columns=[col for col in drop_cols if col in candidates.columns]).copy()
    out = base.merge(feat, on=["entry_date", "code"], how="left")
    return out


def neutral_pass(d: pd.DataFrame, filter_name: str) -> pd.Series:
    if filter_name == "none":
        return pd.Series(True, index=d.index)
    is_neutral = d["route"].astype(str).eq("down_panic") & d["emotion_signal"].astype(str).eq("neutral")
    def num_col(name: str) -> pd.Series:
        if name not in d.columns:
            return pd.Series(pd.NA, index=d.index)
        return pd.to_numeric(d[name], errors="coerce")

    deep_dd30 = num_col("entry_drawdown_from_60d_high").le(-0.30)
    prev10_down8 = num_col("prev10_max_down_day").le(-0.08)
    amount1 = num_col("entry_amount_ratio20").ge(1.0)
    keep_neutral = {
        "deep_dd30": deep_dd30,
        "prev10_down8": prev10_down8,
        "deep_or_prev10_down8": deep_dd30 | prev10_down8,
        "deep_and_amount1": deep_dd30 & amount1,
    }[filter_name]
    return (~is_neutral) | keep_neutral.fillna(False)


def apply_variant(candidates: pd.DataFrame, variant: dict[str, str]) -> pd.DataFrame:
    d = candidates.copy()
    d["neutral_entry_structure_variant"] = variant["variant"]
    mask = neutral_pass(d, variant["filter"])
    d = d[mask].copy()
    return d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])


def apply_stress(candidates: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    d = candidates.copy()
    extra_cost = (float(profile["cost_bps"]) - BASE_COST_BPS) / 10000.0
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce") - extra_cost - float(profile.get("all_shock", 0.0))
    d["stress_profile"] = profile["profile"]
    return d.dropna(subset=["policy_net_ret"])


def filter_summary(base: pd.DataFrame, variant: dict[str, str]) -> dict[str, Any]:
    target = base[base["route"].astype(str).eq("down_panic") & base["emotion_signal"].astype(str).eq("neutral")].copy()
    passed = target[neutral_pass(target, variant["filter"])].copy()
    ret = pd.to_numeric(passed["policy_net_ret"], errors="coerce")
    all_ret = pd.to_numeric(target["policy_net_ret"], errors="coerce")
    return {
        "variant": variant["variant"],
        "neutral_candidates": int(len(target)),
        "kept": int(len(passed)),
        "removed": int(len(target) - len(passed)),
        "keep_rate": float(len(passed) / len(target)) if len(target) else 0.0,
        "all_avg_ret": float(all_ret.mean()) if len(target) else None,
        "kept_avg_ret": float(ret.mean()) if len(passed) else None,
        "kept_win_rate": float((ret > 0).mean()) if len(passed) else None,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_candidates()
    daily = load_daily(base)
    enriched = enrich_neutral(base, daily)
    enriched.to_csv(OUT_DIR / "base_candidates_neutral_features.csv", index=False, encoding="utf-8-sig")

    summaries: list[dict[str, Any]] = []
    filters: list[dict[str, Any]] = []
    for variant in VARIANTS:
        vname = variant["variant"]
        filters.append(filter_summary(enriched, variant))
        v = apply_variant(enriched, variant)
        v.to_csv(OUT_DIR / f"{vname}_candidates.csv", index=False, encoding="utf-8-sig")
        for profile in PROFILES:
            stressed = apply_stress(v, profile)
            curve, closed = simulate_scaled(stressed, float(profile["cost_bps"]))
            run_dir = OUT_DIR / f"{vname}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summaries.append(summarize(curve, closed, vname, str(profile["profile"])))
    summary = pd.DataFrame(summaries)
    filter_df = pd.DataFrame(filters)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    filter_df.to_csv(OUT_DIR / "filter_summary.csv", index=False, encoding="utf-8-sig")
    pct_cols = {
        "keep_rate",
        "all_avg_ret",
        "kept_avg_ret",
        "kept_win_rate",
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "worst_open_mtm_ret",
        "down_panic_avg_ret",
    }
    lines = [
        "# G3 V4 down_panic neutral 入场结构复验 v1",
        "",
        "## 研究目的",
        "",
        "- 从退出规则切回入场源重建。",
        "- 只对 `route=down_panic` 且 `emotion_signal=neutral` 做入场前/入场日可见结构筛选。",
        "- 不使用 score/rank，不使用 D1/D2 未来持仓结果做入场过滤。",
        "",
        "## 固定结构",
        "",
        "- `neutral_deep_dd30`：入场日收盘相对 60 日高点回撤 `<= -30%`。",
        "- `neutral_prev10_down8`：入场前 10 日最大单日下跌 `<= -8%`。",
        "- `neutral_deep_or_prev10_down8`：二者满足其一。",
        "- `neutral_deep_and_amount1`：60 日深回撤且入场日成交额不低于 20 日均量。",
        "",
        "## 候选保留诊断",
        "",
        md_table(filter_df, pct_cols=pct_cols),
        "",
        "## slot 复算结果",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 判断口径",
        "",
        "- 如果过滤后提升三组压力，且没有大幅减少交易到不可用，才说明 neutral 可重建。",
        "- 如果只提高样本均值但 slot 收益下降，说明过滤破坏组合补位，不应接入。",
        "- 如果所有结构都失败，neutral 应暂时暂停，等待新思路。",
        "",
        "## 本轮实际结论",
        "",
        "- `neutral_deep_dd30` 失败：保留 21/32 个 neutral 候选，但候选均值从 `1.20%` 降到 `1.05%`，slot 三组口径都弱于 base。",
        "- `neutral_prev10_down8` 有信息量但偏防守：保留 13/32 个 neutral 候选，候选均值升到 `2.47%`；`cost100` 从 `+215.09%` 小升到 `+215.21%`，`all_shock2` 从 `+46.72%` 提升到 `+53.33%`，最大回撤从 `-32.72%` 收窄到 `-30.44%`；但正常 `cost30` 从 `+374.23%` 降到 `+363.45%`。",
        "- `neutral_deep_or_prev10_down8` 失败：保留过宽，收益和压力都不如 base。",
        "- `neutral_deep_and_amount1` 不理想：保留 10/32 个，压力口径改善到 `+50.49%`，但正常 `cost30` 降到 `+351.05%`，候选均值也只有 `0.43%`。",
        "- 因此，当前 neutral 不能作为进攻型独立买点加入；`prev10_down8` 只能作为弱势防守模式下的候选源观察，不适合作为提升总收益的正式规则。",
        "",
        "## 下一步目标",
        "",
        "暂停对 `down_panic neutral` 的退出层和简单日线结构继续加规则。下一步应把研究重心转向新的横盘/箱体底部独立候选源，或为 `prev10_down8` 单独做防守型小仓位 shadow，而不是把它混入主策略。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
