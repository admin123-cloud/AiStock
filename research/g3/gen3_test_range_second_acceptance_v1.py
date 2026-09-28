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

from scripts.gen3_test_range_30m_volume_acceptance_v1 import PROFILES, standardize, window_metrics  # noqa: E402
from scripts.gen3_test_range_box_stress_icepoint_climax_overlay_v1 import md_table, simulate_scaled, summarize  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


SOURCE = _report_path() / "gen3_range_ice_recent3_quality_quadrants_v1" / "ice_recent3_with_quality_flags.csv"
OUT_DIR = _report_path() / "gen3_range_second_acceptance_v1"

VARIANTS = [
    {
        "variant": "deep_and_reclaim",
        "desc": "箱体底部且日线修复",
        "mode": "base",
    },
    {
        "variant": "deep_and_reclaim_d0_second_accept",
        "desc": "箱体底部且日线修复，并要求入场日后半日再次放量承接",
        "mode": "d0_second",
    },
    {
        "variant": "deep_and_reclaim_d1_second_accept",
        "desc": "箱体底部且日线修复，并要求D1再次放量承接",
        "mode": "d1_second",
    },
    {
        "variant": "deep_and_reclaim_any_second_accept",
        "desc": "箱体底部且日线修复，并要求D0后半日或D1任一再次放量承接",
        "mode": "any_second",
    },
    {
        "variant": "neutral_second_acceptance",
        "desc": "中性横盘情绪下，要求D0后半日或D1二次放量承接",
        "mode": "neutral_any_second",
    },
]


def sql_list(values: list[str]) -> str:
    return ",".join("'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'" for v in values)


def load_base() -> pd.DataFrame:
    d = pd.read_csv(SOURCE, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in [
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "candidate_score",
        "rank_key",
        "fwd_ret_confirm_to_close_5d",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["is_deep_floor"] = d["range_pos60"].le(0.08)
    d["is_daily_reclaim"] = d["close_position"].ge(0.60)
    return d.dropna(subset=["entry_date", "code", "entry_price_adjusted", "fwd_ret_confirm_to_close_5d"]).copy()


def load_following_30m(signals: pd.DataFrame) -> pd.DataFrame:
    if signals.empty:
        return pd.DataFrame()
    codes = sorted(signals["code"].astype(str).unique())
    start = signals["entry_date"].min().strftime("%Y-%m-%d")
    end = (signals["entry_date"].max() + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    sql = f"""
    SELECT code, datetime, open, high, low, close, volume, amount
    FROM kline_minute_30
    WHERE code IN ({sql_list(codes)})
      AND datetime >= toDateTime('{start} 09:30:00')
      AND datetime <= toDateTime('{end} 15:00:00')
    ORDER BY code, datetime
    """
    bars = clickhouse_query_df(sql)
    if bars.empty:
        return bars
    bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
    bars["bar_date"] = bars["datetime"].dt.normalize()
    bars["bar_time"] = bars["datetime"].dt.strftime("%H:%M:%S")
    for col in ["open", "high", "low", "close", "amount"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    bars["amount_ma3_prev"] = bars.groupby("code")["amount"].transform(lambda s: s.rolling(3, min_periods=3).mean().shift(1))
    bars["amount_ratio3_follow"] = bars["amount"] / bars["amount_ma3_prev"].replace(0, pd.NA)
    rng = (bars["high"] - bars["low"]).replace(0, pd.NA)
    bars["bar_close_pos_follow"] = (bars["close"] - bars["low"]) / rng
    bars["bar_ret_follow"] = bars["close"] / bars["open"].replace(0, pd.NA) - 1.0
    return bars


def attach_second_acceptance(signals: pd.DataFrame) -> pd.DataFrame:
    bars = load_following_30m(signals)
    out = signals.copy()
    out["d0_second_accept"] = False
    out["d1_second_accept"] = False
    out["d0_second_accept_datetime"] = pd.NA
    out["d1_second_accept_datetime"] = pd.NA
    if bars.empty:
        return out

    follow_rows = []
    for row in out.itertuples(index=False):
        code = str(getattr(row, "code"))
        entry_date = pd.Timestamp(getattr(row, "entry_date")).normalize()
        confirm_dt = pd.to_datetime(getattr(row, "confirm_datetime"), errors="coerce")
        b = bars[bars["code"].eq(code)].copy()
        if b.empty:
            follow_rows.append((False, False, pd.NaT, pd.NaT))
            continue
        d0 = b[(b["bar_date"].eq(entry_date)) & (b["datetime"] > confirm_dt)].copy()
        d1_dates = sorted(b[b["bar_date"].gt(entry_date)]["bar_date"].dropna().unique())
        d1_date = pd.Timestamp(d1_dates[0]).normalize() if d1_dates else pd.NaT
        d1 = b[b["bar_date"].eq(d1_date)].copy() if pd.notna(d1_date) else pd.DataFrame()

        def accept(df: pd.DataFrame) -> pd.Timestamp | pd.NaT:
            if df.empty:
                return pd.NaT
            m = (
                df["bar_ret_follow"].ge(0.0).fillna(False)
                & df["bar_close_pos_follow"].ge(0.70).fillna(False)
                & df["amount_ratio3_follow"].ge(1.30).fillna(False)
            )
            hit = df[m].sort_values("datetime").head(1)
            return pd.NaT if hit.empty else pd.Timestamp(hit["datetime"].iloc[0])

        d0_hit = accept(d0)
        d1_hit = accept(d1)
        follow_rows.append((pd.notna(d0_hit), pd.notna(d1_hit), d0_hit, d1_hit))

    vals = pd.DataFrame(follow_rows, columns=["d0_second_accept", "d1_second_accept", "d0_second_accept_datetime", "d1_second_accept_datetime"])
    for col in vals.columns:
        out[col] = vals[col].values
    return out


def select_variant(base: pd.DataFrame, spec: dict[str, str]) -> pd.DataFrame:
    deep_and_reclaim = base["is_deep_floor"] & base["is_daily_reclaim"]
    neutral = base.get("emotion_signal", pd.Series("", index=base.index)).astype(str).eq("neutral")
    mode = spec["mode"]
    if mode == "base":
        mask = deep_and_reclaim
    elif mode == "d0_second":
        mask = deep_and_reclaim & base["d0_second_accept"]
    elif mode == "d1_second":
        mask = deep_and_reclaim & base["d1_second_accept"]
    elif mode == "any_second":
        mask = deep_and_reclaim & (base["d0_second_accept"] | base["d1_second_accept"])
    elif mode == "neutral_any_second":
        mask = neutral & (base["d0_second_accept"] | base["d1_second_accept"])
    else:
        raise ValueError(mode)
    selected = base[mask].copy()
    selected["variant"] = spec["variant"]
    selected["desc"] = spec["desc"]
    selected["family"] = "range_second_acceptance"
    selected["hold_days"] = 5
    selected["rank_key"] = pd.to_numeric(selected.get("rank_key", selected.get("candidate_score", 0.0)), errors="coerce").fillna(0.0)
    selected["rank_in_day"] = selected.groupby("entry_date")["rank_key"].rank(method="first", ascending=False)
    return selected


def coverage(base: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for spec in VARIANTS:
        d = select_variant(base, spec)
        ret = pd.to_numeric(d.get("fwd_ret_confirm_to_close_5d", pd.Series(dtype=float)), errors="coerce")
        rows.append(
            {
                "variant": spec["variant"],
                "desc": spec["desc"],
                "signal_count": int(len(d)),
                "signal_days": int(d["entry_date"].nunique()) if len(d) else 0,
                "raw_avg_5d": float(ret.mean()) if len(ret) else 0.0,
                "raw_win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = attach_second_acceptance(load_base())
    base.to_csv(OUT_DIR / "base_with_second_acceptance_flags.csv", index=False, encoding="utf-8-sig")
    cov = coverage(base)
    cov.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8-sig")

    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    for spec in VARIANTS:
        selected = select_variant(base, spec)
        selected.to_csv(OUT_DIR / f"{spec['variant']}_signals.csv", index=False, encoding="utf-8-sig")
        for profile in PROFILES:
            candidates = standardize(selected, profile)
            curve, closed = simulate_scaled(candidates)
            run_dir = OUT_DIR / f"{spec['variant']}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(summarize(curve, closed, str(spec["variant"]), str(profile["profile"]), str(spec["desc"])))
            window_rows.extend(window_metrics(curve, closed, str(spec["variant"]), str(profile["profile"])))

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    pct_cols = {"raw_avg_5d", "raw_win_rate", "total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "return"}
    report = "\n".join(
        [
            "# G3 横盘/冰点二次30m承接复验 v1",
            "",
            "## 回测范围",
            "- 候选信号/入场窗口：2020-01-01 至 2026-05-29。",
            "- 实际信号来自 `ice_recent3_with_quality_flags.csv`，入场日覆盖以各子策略实际成交为准。",
            "- 本轮是独立候选源研究，不接实盘，不使用新的 score/rank 过滤。",
            "",
            "## 策略名解释",
            "- `deep_and_reclaim`：箱体底部且日线修复。要求个股贴近60日箱体底部，并且日线收盘修复位置较高。",
            "- `deep_and_reclaim_d0_second_accept`：在 `deep_and_reclaim` 基础上，入场日确认后半日还要再出现一次30m放量承接。",
            "- `deep_and_reclaim_d1_second_accept`：在 `deep_and_reclaim` 基础上，D1还要再出现一次30m放量承接。",
            "- `deep_and_reclaim_any_second_accept`：在 `deep_and_reclaim` 基础上，D0后半日或D1任一出现二次承接即可。",
            "- `neutral_second_acceptance`：中性横盘情绪下，不要求冰点，只要求D0后半日或D1出现二次放量承接。",
            "- `cost30`：30bps成本；`cost100`：100bps高摩擦；`shock2_cost30`：30bps成本再扣2%冲击。",
            "",
            "## 二次承接定义",
            "- 使用 ClickHouse `kline_minute_30`，不使用事后收益。",
            "- 固定条件：确认后或D1的30m K线为非阴线、收盘位置 >= 70%、成交额相对前三根30m均量 >= 1.30。",
            "",
            "## 覆盖率",
            md_table(cov, pct_cols=pct_cols),
            "",
            "## slot复算结果",
            md_table(summary, pct_cols=pct_cols),
            "",
            "## 分窗口结果",
            md_table(windows, pct_cols=pct_cols),
            "",
            "## 判断口径",
            "- 如果二次承接显著改善 `cost100` 和 `shock2_cost30`，说明横盘源的质量来自持续承接。",
            "- 如果只减少交易次数但压力口径没有改善，说明二次承接不是根因，应转向候选源重建。",
        ]
    )
    (OUT_DIR / "REPORT.md").write_text(report + "\n", encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
