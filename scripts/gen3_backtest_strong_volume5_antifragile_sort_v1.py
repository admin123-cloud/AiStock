from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_confirm_fail_exit_v1 import _apply_policy
from scripts.gen3_backtest_strong_volume5_exit_proxy_v1 import _simulate
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import (
    SOURCE,
    WINDOWS,
    _classify_market_style,
    _exit_date_map,
    _max_drawdown,
    _md_table,
    _trade_calendar,
)


OUT_DIR = ROOT / "reports" / "gen3_strong_volume5_antifragile_sort_v1"


def _clip01(s: pd.Series) -> pd.Series:
    return s.clip(lower=0.0, upper=1.0)


def _num(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series(default, index=df.index, dtype=float)
    return pd.to_numeric(df[col], errors="coerce").fillna(default)


def _prepare_base(cost_bps: float) -> pd.DataFrame:
    df = pd.read_parquet(SOURCE)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.normalize()
    df = df[df["source_family"].eq("volume5")].copy()
    df["h5"] = pd.to_numeric(df["outcome_fwd_ret_5d"], errors="coerce").where(
        pd.to_numeric(df["outcome_fwd_ret_5d"], errors="coerce").notna(),
        pd.to_numeric(df["fwd_ret_5d"], errors="coerce"),
    )
    for col in ["fwd_ret_1d", "fwd_ret_2d", "fwd_ret_3d"]:
        df[col] = pd.to_numeric(df.get(col), errors="coerce")
    df = df.dropna(subset=["entry_date", "h5", "code"]).copy()
    df["v4_rank"] = pd.to_numeric(df.get("v4_rank"), errors="coerce").fillna(1e9)
    df["v4_score"] = pd.to_numeric(df.get("v4_score"), errors="coerce").fillna(-1e9)
    df["stop5_touch_30m"] = df.get("stop5_touch_30m", False).fillna(False).astype(bool)
    df["stop5_touch_datetime"] = pd.to_datetime(df.get("stop5_touch_datetime"), errors="coerce")
    df["g3_market_style"] = df.apply(_classify_market_style, axis=1)
    df = df[df["g3_market_style"].isin(["main_up", "weak_recovery"])].copy()

    calendar = _trade_calendar(df["entry_date"].min(), df["entry_date"].max() + pd.Timedelta(days=30))
    for hold_days in [1, 2, 3, 5]:
        exit_map = _exit_date_map(calendar, hold_days)
        df[f"exit_date_h{hold_days}"] = df["entry_date"].map(exit_map)
        df[f"exit_date_h{hold_days}"] = pd.to_datetime(df[f"exit_date_h{hold_days}"], errors="coerce").dt.normalize()
    df = df.dropna(subset=["exit_date_h5"]).copy()
    df["fixed_net_ret"] = df["h5"] - cost_bps / 10000.0
    _add_antifragile_scores(df)
    return df


def _add_antifragile_scores(df: pd.DataFrame) -> None:
    v4_quality = _clip01((_num(df, "v4_score") - 0.50) / 0.40)
    amount_quality = _clip01((_num(df, "rt_30m_amount_ratio") - 2.0) / 3.0)
    mom10_quality = _clip01((_num(df, "mom10") - 0.08) / 0.15)
    mom20_quality = _clip01((_num(df, "mom20") - 0.08) / 0.20)
    pressure_quality = 1.0 - _clip01(_num(df, "cap_pressure_amount_share") / 0.35)
    overhead_quality = 1.0 - _clip01(_num(df, "overhead_pressure_amount_share") / 0.35)
    runup_balance = 1.0 - (_num(df, "runup_from_60d_low") - 0.45).abs().div(0.55).clip(lower=0.0, upper=1.0)

    df["g3_antifragile_score"] = (
        0.30 * v4_quality
        + 0.20 * amount_quality
        + 0.15 * mom10_quality
        + 0.10 * mom20_quality
        + 0.10 * pressure_quality
        + 0.10 * overhead_quality
        + 0.05 * runup_balance
    )
    df["g3_antifragile_rank"] = df.groupby("entry_date")["g3_antifragile_score"].rank(method="first", ascending=False)


def _sort_candidates(d: pd.DataFrame, sort_mode: str) -> pd.DataFrame:
    if sort_mode == "v4_rank":
        cols = ["entry_date", "v4_rank", "v4_score", "confirm_datetime", "code"]
        asc = [True, True, False, True, True]
    elif sort_mode == "antifragile_score":
        cols = ["entry_date", "g3_antifragile_score", "v4_rank", "v4_score", "confirm_datetime", "code"]
        asc = [True, False, True, False, True, True]
    elif sort_mode == "antifragile_then_v4":
        cols = ["entry_date", "g3_antifragile_rank", "v4_rank", "v4_score", "confirm_datetime", "code"]
        asc = [True, True, True, False, True, True]
    else:
        raise ValueError(sort_mode)
    existing = [c for c in cols if c in d.columns]
    return d.sort_values(existing, ascending=asc[: len(existing)]).copy()


def _metrics(closed: pd.DataFrame, curve: pd.DataFrame, window: str, policy: str, sort_mode: str, book: str) -> dict:
    start, end = WINDOWS[window]
    cw = curve[(curve["date"] >= pd.Timestamp(start)) & (curve["date"] <= pd.Timestamp(end))].copy()
    tw = closed[(closed["entry_date"] >= pd.Timestamp(start)) & (closed["entry_date"] <= pd.Timestamp(end))].copy() if not closed.empty else pd.DataFrame()
    if cw.empty:
        return {"window": window, "policy": policy, "sort_mode": sort_mode, "book": book, "closed": 0}
    start_equity = float(cw["equity"].iloc[0])
    end_equity = float(cw["equity"].iloc[-1])
    local = cw["equity"] / start_equity
    dd = local / local.cummax() - 1.0
    stop_rate = float(tw["stop5_touch_30m"].astype(bool).mean()) if "stop5_touch_30m" in tw.columns and len(tw) else 0.0
    bad10 = float((tw["net_ret"] <= -0.10).mean()) if len(tw) else 0.0
    return {
        "window": window,
        "policy": policy,
        "sort_mode": sort_mode,
        "book": book,
        "closed": int(len(tw)),
        "early_exits": int((tw.get("exit_reason_proxy", pd.Series(dtype=str)).astype(str) != "fixed_h5").sum()) if not tw.empty else 0,
        "start_equity": start_equity,
        "end_equity": end_equity,
        "total_ret": end_equity / start_equity - 1.0,
        "max_drawdown": float(dd.min()),
        "win_rate": float((tw["net_ret"] > 0).mean()) if len(tw) else 0.0,
        "mean_trade_ret": float(tw["net_ret"].mean()) if len(tw) else 0.0,
        "worst_trade": float(tw["net_ret"].min()) if len(tw) else 0.0,
        "stop_touch_rate": stop_rate,
        "bad10_rate": bad10,
        "max_open_positions": int(cw["open_positions"].max()),
    }


def _annual(policy: str, sort_mode: str, book: str, closed: pd.DataFrame, curve: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for year, part in curve.groupby(curve["date"].dt.year):
        part = part.sort_values("date")
        tw = closed[closed["entry_date"].dt.year.eq(year)] if not closed.empty else pd.DataFrame()
        rows.append(
            {
                "policy": policy,
                "sort_mode": sort_mode,
                "book": book,
                "year": int(year),
                "return": float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0),
                "max_drawdown": _max_drawdown(part["equity"]),
                "closed": int(len(tw)),
                "early_exits": int((tw.get("exit_reason_proxy", pd.Series(dtype=str)).astype(str) != "fixed_h5").sum()) if not tw.empty else 0,
                "win_rate": float((tw["net_ret"] > 0).mean()) if len(tw) else 0.0,
                "mean_trade_ret": float(tw["net_ret"].mean()) if len(tw) else 0.0,
                "worst_trade": float(tw["net_ret"].min()) if len(tw) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _write_report(summary: pd.DataFrame, annual: pd.DataFrame) -> None:
    pct_cols = {
        "total_ret",
        "max_drawdown",
        "win_rate",
        "mean_trade_ret",
        "worst_trade",
        "stop_touch_rate",
        "bad10_rate",
        "return",
    }
    full = summary[summary["window"].eq("full")].sort_values(["book", "policy", "sort_mode"])
    focus = annual[
        annual["book"].eq("slot5_20pct_daily2")
        & annual["policy"].isin(["fixed_h5", "confirm_d3_le0"])
    ].sort_values(["policy", "sort_mode", "year"])
    lines = [
        "# G3 强势链路 Volume5 抗跌质量排序 V1",
        "",
        "## 口径",
        "",
        "- 样本：`core_recovery_volume5 = main_up + weak_recovery`。",
        "- 只改变同一天候选排序，不删除候选，不扩大信号源。",
        "- 抗跌质量分使用入场可见字段：`v4_score`、`rt_30m_amount_ratio`、`mom10/mom20`、筹码/上方压力、60日涨幅平衡。",
        "- 这是影子排序验证，不是正式选股过滤；不按结果搜索权重。",
        "",
        "## Full 窗口",
        "",
        _md_table(full, pct_cols=pct_cols),
        "",
        "## Slot5 年度焦点",
        "",
        _md_table(focus, pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 若抗跌排序改善 `stop_touch_rate`、`bad10_rate` 或回撤，同时收益不塌陷，后续可进入 30m 明细重放。",
        "- 若仅提高收益但不改善尾部风险，说明它更像进攻排序，不应包装成风控。",
        "- 若收益/回撤都恶化，应放弃这版影子评分，回到更底层的实时可见特征构造。",
        "",
    ]
    (OUT_DIR / "antifragile_sort_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cost_bps = 30.0
    base = _prepare_base(cost_bps)
    policies = ["fixed_h5", "confirm_d3_le0"]
    sort_modes = ["v4_rank", "antifragile_score", "antifragile_then_v4"]
    books = [
        ("slot2_50pct_daily1", 2, 0.50, 1),
        ("slot5_20pct_daily2", 5, 0.20, 2),
    ]
    summaries = []
    annual_parts = []
    for policy in policies:
        policy_candidates = _apply_policy(base, policy, cost_bps)
        for sort_mode in sort_modes:
            candidates = _sort_candidates(policy_candidates, sort_mode)
            for book, slots, slot_pct, daily_open_limit in books:
                closed, curve = _simulate(candidates, policy, slots, slot_pct, daily_open_limit)
                stem = f"core_volume5_{policy}_{sort_mode}_{book}"
                closed.to_csv(OUT_DIR / f"{stem}_closed_trades.csv", index=False, encoding="utf-8-sig")
                curve.to_csv(OUT_DIR / f"{stem}_curve.csv", index=False, encoding="utf-8-sig")
                annual_parts.append(_annual(policy, sort_mode, book, closed, curve))
                for window in WINDOWS:
                    summaries.append(_metrics(closed, curve, window, policy, sort_mode, book))

    summary = pd.DataFrame(summaries)
    annual = pd.concat(annual_parts, ignore_index=True) if annual_parts else pd.DataFrame()
    summary.to_csv(OUT_DIR / "antifragile_sort_summary_raw.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "antifragile_sort_annual_raw.csv", index=False, encoding="utf-8-sig")
    base.to_csv(OUT_DIR / "antifragile_scored_source.csv", index=False, encoding="utf-8-sig")
    _write_report(summary, annual)
    full = summary[summary["window"].eq("full") & summary["book"].eq("slot5_20pct_daily2")].sort_values(["policy", "sort_mode"])
    print(
        json.dumps(
            {
                "out_dir": str(OUT_DIR),
                "source_rows": int(len(base)),
                "slot5_full": full[
                    [
                        "policy",
                        "sort_mode",
                        "closed",
                        "early_exits",
                        "total_ret",
                        "max_drawdown",
                        "win_rate",
                        "mean_trade_ret",
                        "worst_trade",
                        "stop_touch_rate",
                        "bad10_rate",
                    ]
                ].to_dict(orient="records"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
