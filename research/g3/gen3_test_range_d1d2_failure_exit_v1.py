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

from scripts.gen3_test_range_30m_volume_acceptance_v1 import PROFILES, exit_dates, simulate, summarize, window_metrics  # noqa: E402
from scripts.gen3_test_range_box_first_panic_distill_second_accept_v1 import concentration, group_year  # noqa: E402


SOURCE_DIR = _report_path() / "gen3_range_first_panic_d1_accept_stability_v1"
OUT_DIR = _report_path() / "gen3_range_d1d2_failure_exit_v1"
SIGNAL_START = "2020-01-01"
SIGNAL_END = "2026-05-29"
CURVE_END = "2026-06-04"

SOURCES = [
    {
        "source": "balanced_77",
        "file": SOURCE_DIR / "d1_reclaim60_amt12_signals.csv",
        "desc": "77笔平衡候选：D1二次30m承接，日线修复>=60%，量能不过热(amount_ratio20<=1.2)",
    },
    {
        "source": "wide_182",
        "file": SOURCE_DIR / "d1_second_signals.csv",
        "desc": "182笔宽候选：只要求D1二次30m承接",
    },
]

POLICIES = [
    {
        "policy": "base_d3neg",
        "desc": "基准退出：D3仍亏损则D3退出，否则D5退出",
    },
    {
        "policy": "d1_weak_exit",
        "desc": "D1弱确认快速退出：D1收盘仍亏损则D1退出，否则回到D3/D5规则",
    },
    {
        "policy": "d2_weak_exit",
        "desc": "D2弱确认快速退出：D2收盘仍亏损则D2退出，否则回到D3/D5规则",
    },
    {
        "policy": "warn_d1_weak_exit",
        "desc": "入场质量警戒 + D1弱确认：只有入场日冲高回落/突破失败且D1仍亏损，才D1退出",
    },
    {
        "policy": "warn_d2_weak_exit",
        "desc": "入场质量警戒 + D2弱确认：只有入场日冲高回落/突破失败且D2仍亏损，才D2退出",
    },
    {
        "policy": "warn_d1d2_weak_exit",
        "desc": "入场质量警戒 + D1/D2递进确认：警戒票D1亏损则D1退，否则D2亏损则D2退，否则D3/D5",
    },
    {
        "policy": "warn_d1_half_exit",
        "desc": "入场质量警戒 + D1半仓退出近似：警戒票D1亏损时半仓D1退出，剩余半仓按D3/D5",
    },
]


def load_source(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["confirm_datetime"] = pd.to_datetime(d.get("confirm_datetime"), errors="coerce")
    for col in [
        "confirm_high",
        "prev_bar_high",
        "daily_entry_close",
        "entry_price_adjusted",
        "price_scale_daily_to_minute",
        "close_position",
        "bar_close_pos",
        "fwd_ret_confirm_to_close_1d",
        "fwd_ret_confirm_to_close_2d",
        "fwd_ret_confirm_to_close_3d",
        "fwd_ret_confirm_to_close_5d",
        "amount_ratio20",
        "big_down_rate",
        "rank_key",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    if "rank_key" not in d.columns:
        d["rank_key"] = 0.0
    d["rank_key"] = d["rank_key"].fillna(0.0)
    d["entry_date_ts"] = d["entry_date"]
    d["entry_price_used"] = pd.to_numeric(d["entry_price_adjusted"], errors="coerce")
    return d.dropna(
        subset=[
            "entry_date",
            "code",
            "entry_price_used",
            "fwd_ret_confirm_to_close_1d",
            "fwd_ret_confirm_to_close_2d",
            "fwd_ret_confirm_to_close_3d",
            "fwd_ret_confirm_to_close_5d",
        ]
    ).copy()


def add_quality_flags(d: pd.DataFrame) -> pd.DataFrame:
    out = d.copy()
    scale = pd.to_numeric(out.get("price_scale_daily_to_minute", 1.0), errors="coerce").fillna(1.0)
    adj_high = pd.to_numeric(out.get("confirm_high"), errors="coerce") * scale
    adj_prev_high = pd.to_numeric(out.get("prev_bar_high"), errors="coerce") * scale
    close = pd.to_numeric(out.get("daily_entry_close"), errors="coerce")
    entry = pd.to_numeric(out.get("entry_price_used"), errors="coerce")
    out["entry_to_close_ret"] = close / entry - 1.0
    out["giveback_from_confirm_high"] = close / adj_high - 1.0
    out["break_fail"] = close.lt(adj_prev_high) | close.lt(entry * 0.995)
    out["fade_warn"] = out["entry_to_close_ret"].le(-0.005) | out["giveback_from_confirm_high"].le(-0.025) | out["close_position"].lt(0.70)
    out["quality_warn"] = out["fade_warn"] | out["break_fail"]
    out["d1_weak"] = pd.to_numeric(out["fwd_ret_confirm_to_close_1d"], errors="coerce").lt(0.0)
    out["d2_weak"] = pd.to_numeric(out["fwd_ret_confirm_to_close_2d"], errors="coerce").lt(0.0)
    return out


def _policy_gross_ret(d: pd.DataFrame, policy: str) -> tuple[pd.Series, pd.Series, pd.Series]:
    ret1 = pd.to_numeric(d["fwd_ret_confirm_to_close_1d"], errors="coerce")
    ret2 = pd.to_numeric(d["fwd_ret_confirm_to_close_2d"], errors="coerce")
    ret3 = pd.to_numeric(d["fwd_ret_confirm_to_close_3d"], errors="coerce")
    ret5 = pd.to_numeric(d["fwd_ret_confirm_to_close_5d"], errors="coerce")
    base_early = ret3.lt(0.0)
    base_ret = ret5.where(~base_early, ret3)
    base_hold = pd.Series(5, index=d.index).where(~base_early, 3)
    base_reason = pd.Series("hold_d5", index=d.index).where(~base_early, "d3_negative_exit")
    warn = d["quality_warn"].astype(bool)

    if policy == "base_d3neg":
        return base_ret, base_hold, base_reason
    if policy == "d1_weak_exit":
        early = ret1.lt(0.0)
        return ret1.where(early, base_ret), pd.Series(1, index=d.index).where(early, base_hold), base_reason.where(~early, "d1_weak_exit")
    if policy == "d2_weak_exit":
        early = ret2.lt(0.0)
        return ret2.where(early, base_ret), pd.Series(2, index=d.index).where(early, base_hold), base_reason.where(~early, "d2_weak_exit")
    if policy == "warn_d1_weak_exit":
        early = warn & ret1.lt(0.0)
        return ret1.where(early, base_ret), pd.Series(1, index=d.index).where(early, base_hold), base_reason.where(~early, "warn_d1_weak_exit")
    if policy == "warn_d2_weak_exit":
        early = warn & ret2.lt(0.0)
        return ret2.where(early, base_ret), pd.Series(2, index=d.index).where(early, base_hold), base_reason.where(~early, "warn_d2_weak_exit")
    if policy == "warn_d1d2_weak_exit":
        early1 = warn & ret1.lt(0.0)
        early2 = warn & ~early1 & ret2.lt(0.0)
        gross = base_ret.where(~early2, ret2).where(~early1, ret1)
        hold = base_hold.where(~early2, 2).where(~early1, 1)
        reason = base_reason.where(~early2, "warn_d2_weak_exit").where(~early1, "warn_d1_weak_exit")
        return gross, hold, reason
    if policy == "warn_d1_half_exit":
        early = warn & ret1.lt(0.0)
        gross = base_ret.where(~early, 0.5 * ret1 + 0.5 * base_ret)
        return gross, base_hold, base_reason.where(~early, "warn_d1_half_exit_approx")
    raise ValueError(policy)


def standardize_policy(signals: pd.DataFrame, profile: dict[str, Any], source: str, policy: dict[str, str]) -> pd.DataFrame:
    d = signals.copy()
    gross, hold, reason = _policy_gross_ret(d, policy["policy"])
    cost = float(profile["cost_bps"]) / 10000.0
    shock = float(profile["shock"])
    d["policy_net_ret"] = gross - cost - shock
    d["policy_hold_days"] = hold.astype(int)
    d["exit_reason"] = reason
    maps = {n: exit_dates(d["entry_date_ts"], n) for n in [1, 2, 3, 5]}
    d["policy_exit_date"] = [maps[int(h)].get(pd.Timestamp(day).normalize()) for day, h in zip(d["entry_date_ts"], d["policy_hold_days"])]
    d["variant"] = f"{source}__{policy['policy']}"
    d["desc"] = policy["desc"]
    return d.dropna(subset=["entry_date_ts", "policy_exit_date", "policy_net_ret", "entry_price_used"]).copy()


def coverage_rows(source: str, desc: str, d: pd.DataFrame) -> dict[str, Any]:
    return {
        "source": source,
        "desc": desc,
        "signal_count": int(len(d)),
        "signal_days": int(d["entry_date"].nunique()),
        "quality_warn_count": int(d["quality_warn"].sum()),
        "quality_warn_ratio": float(d["quality_warn"].mean()),
        "break_fail_count": int(d["break_fail"].sum()),
        "fade_warn_count": int(d["fade_warn"].sum()),
        "d1_weak_count": int(d["d1_weak"].sum()),
        "d2_weak_count": int(d["d2_weak"].sum()),
        "warn_d1_weak_count": int((d["quality_warn"] & d["d1_weak"]).sum()),
        "warn_d2_weak_count": int((d["quality_warn"] & d["d2_weak"]).sum()),
        "warn_avg_5d_gross": float(pd.to_numeric(d.loc[d["quality_warn"], "fwd_ret_confirm_to_close_5d"], errors="coerce").mean()),
        "clean_avg_5d_gross": float(pd.to_numeric(d.loc[~d["quality_warn"], "fwd_ret_confirm_to_close_5d"], errors="coerce").mean()),
    }


def write_report(cov: pd.DataFrame, summary: pd.DataFrame, windows: pd.DataFrame, best_variant: str, yearly: pd.DataFrame, conc: pd.DataFrame) -> None:
    def p(x: Any) -> str:
        if pd.isna(x):
            return "--"
        return f"{float(x) * 100:+.2f}%"

    lines = [
        "# G3 v4 入场失败质量警戒与 D1/D2 早期退出复验",
        "",
        f"- 信号入场窗口：{SIGNAL_START} 至 {SIGNAL_END}。",
        f"- 曲线结算窗口：{SIGNAL_START} 至 {CURVE_END}。",
        "- 资金与仓位：初始资金 150,000，5槽位，同日最多开1笔。",
        "- 本轮只改退出/减仓逻辑，不新增 score/rank 过滤。",
        "",
        "## 英文策略名解释",
        "- `balanced_77`：77笔平衡候选，要求 D1 二次30m承接、日线修复>=60%、量能不过热。",
        "- `wide_182`：182笔宽候选，只要求 D1 二次30m承接。",
        "- `quality_warn`：入场日冲高回落或突破失败警戒。",
        "- `d1_weak/d2_weak`：买入后第1/第2个交易日收盘相对入场仍亏损。",
        "- `warn_d1d2_weak_exit`：只有警戒票才触发 D1/D2 快速退出。",
        "- `warn_d1_half_exit`：警戒票 D1 亏损时半仓退出的近似测算。",
        "",
        "## 入场质量覆盖",
        cov.to_markdown(index=False),
        "",
        "## slot 复算汇总",
        summary.to_markdown(index=False),
        "",
        f"## 相对最好方案：`{best_variant}`",
        windows[windows["variant"].eq(best_variant)].to_markdown(index=False),
        "",
        "## 最好方案年度拆分",
        yearly.to_markdown(index=False),
        "",
        "## 最好方案集中度",
        conc.to_markdown(index=False),
        "",
        "## 判断",
        "- 如果快速退出只降低收益、不改善冲击口径，说明问题不在退出阈值，而在候选源质量。",
        "- 如果宽候选经 D1/D2 退出后仍无法穿越 2% 冲击，说明不能靠卖法修复宽源。",
        "- 半仓退出为近似测算，若有效，后续需要写真实半仓 slot 复算。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    coverage = []
    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    closed_map: dict[tuple[str, str], pd.DataFrame] = {}
    for source_spec in SOURCES:
        base = add_quality_flags(load_source(source_spec["file"]))
        coverage.append(coverage_rows(source_spec["source"], source_spec["desc"], base))
        base.to_csv(OUT_DIR / f"{source_spec['source']}_signals_with_quality_flags.csv", index=False, encoding="utf-8-sig")
        for policy in POLICIES:
            for profile in PROFILES:
                candidates = standardize_policy(base, profile, source_spec["source"], policy)
                curve, closed = simulate(candidates)
                variant = f"{source_spec['source']}__{policy['policy']}"
                run_dir = OUT_DIR / f"{variant}__{profile['profile']}"
                run_dir.mkdir(parents=True, exist_ok=True)
                candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
                curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
                closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
                closed_map[(variant, str(profile["profile"]))] = closed
                summary_rows.append(summarize(curve, closed, variant, str(profile["profile"]), policy["desc"]))
                window_rows.extend(window_metrics(curve, closed, variant, str(profile["profile"])))
    cov = pd.DataFrame(coverage)
    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    cov.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")

    rank = summary[summary["profile"].eq("shock2_cost30")].copy()
    rank = rank.merge(
        summary[summary["profile"].eq("cost30")][["variant", "total_return", "max_drawdown"]].rename(
            columns={"total_return": "cost30_return", "max_drawdown": "cost30_dd"}
        ),
        on="variant",
        how="left",
    )
    rank = rank.sort_values(["total_return", "cost30_return"], ascending=False)
    best_variant = str(rank.iloc[0]["variant"])
    best_closed = closed_map[(best_variant, "cost30")]
    yearly = group_year(best_closed)
    conc = concentration(best_closed)
    yearly.to_csv(OUT_DIR / "best_yearly_cost30.csv", index=False, encoding="utf-8-sig")
    conc.to_csv(OUT_DIR / "best_concentration_cost30.csv", index=False, encoding="utf-8-sig")
    keep_cols = [
        "entry_date",
        "policy_exit_date",
        "code",
        "name",
        "exit_reason",
        "policy_net_ret",
        "realized_pnl",
        "quality_warn",
        "break_fail",
        "fade_warn",
        "d1_weak",
        "d2_weak",
        "entry_to_close_ret",
        "giveback_from_confirm_high",
        "fwd_ret_confirm_to_close_1d",
        "fwd_ret_confirm_to_close_2d",
        "fwd_ret_confirm_to_close_3d",
        "fwd_ret_confirm_to_close_5d",
    ]
    keep_cols = [c for c in keep_cols if c in best_closed.columns]
    best_closed.sort_values("realized_pnl", ascending=False).head(20)[keep_cols].to_csv(
        OUT_DIR / "best_top_trades_cost30.csv", index=False, encoding="utf-8-sig"
    )
    best_closed.sort_values("policy_net_ret").head(20)[keep_cols].to_csv(
        OUT_DIR / "best_worst_trades_cost30.csv", index=False, encoding="utf-8-sig"
    )
    write_report(cov, summary, windows, best_variant, yearly, conc)
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
