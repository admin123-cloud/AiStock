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

from scripts.gen3_backtest_strong_volume5_confirm_d3_execution_stress_v1 import (  # noqa: E402
    _load_daily_prices,
    _next_trade_date_map,
    _price_maps,
    _ret_from_price,
)
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar  # noqa: E402
from scripts.gen3_stress_v4_strong_second_093_execution_v1 import annual_metrics, window_metrics  # noqa: E402
from scripts.gen3_test_v4_strong_position_scale_v1 import (  # noqa: E402
    VARIANT_ROUTE_LIMITS,
    md_table,
    simulate_scaled,
    summarize,
)


SRC_DIR = _report_path() / "gen3_v4_strong_position_scale_probe_v1"
QUALITY_PATH = _report_path() / "gen3_v4_strong_entry_quality_audit_v1" / "entry_quality_enriched.csv"
OUT_DIR = _report_path() / "gen3_v4_strong_entry_warning_early_exit_v1"
BASE_VARIANT = "strong_second_score_ge_093"

VARIANTS = [
    {"variant": "base_093", "warning": "none", "action": "none"},
    {"variant": "warn_or_d1d2_fast_exit", "warning": "or", "action": "fast_exit"},
    {"variant": "warn_or_d1d2_half_proxy", "warning": "or", "action": "half_proxy"},
    {"variant": "warn_strict_d1d2_fast_exit", "warning": "strict", "action": "fast_exit"},
    {"variant": "warn_strict_d1d2_half_proxy", "warning": "strict", "action": "half_proxy"},
]

PROFILES = [
    {"profile": "policy_30bps", "shock": 0.0},
    {"profile": "policy_haircut2_30bps", "shock": 0.02},
]


def load_candidates() -> pd.DataFrame:
    d = pd.read_csv(SRC_DIR / f"{BASE_VARIANT}_candidates.csv", low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in ["score", "entry_price", "policy_net_ret", "position_scale", "route_priority", "strong_day_rank"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["position_scale"] = d.get("position_scale", 1.0).fillna(1.0)
    return d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])


def load_quality() -> pd.DataFrame:
    q = pd.read_csv(QUALITY_PATH, low_memory=False, encoding="utf-8-sig")
    q["entry_date"] = pd.to_datetime(q["entry_date"], errors="coerce").dt.normalize()
    q["code"] = q["code"].astype(str)
    keep = [
        "entry_date",
        "code",
        "entry_upper_shadow",
        "entry_break_high20",
        "entry_amount_ratio20",
        "runup_from_60d_low",
        "d1_close_ret",
        "d2_close_ret",
        "early_fail",
        "failure_tag",
    ]
    q = q[[col for col in keep if col in q.columns]].copy()
    for col in ["entry_upper_shadow", "entry_break_high20", "entry_amount_ratio20", "runup_from_60d_low", "d1_close_ret", "d2_close_ret"]:
        if col in q.columns:
            q[col] = pd.to_numeric(q[col], errors="coerce")
    return q


def attach_quality(base: pd.DataFrame) -> pd.DataFrame:
    q = load_quality()
    d = base.merge(q, on=["entry_date", "code"], how="left")
    d["entry_warning_or"] = (
        pd.to_numeric(d.get("entry_upper_shadow"), errors="coerce").ge(0.50)
        | pd.to_numeric(d.get("entry_break_high20"), errors="coerce").le(-0.02)
    )
    d["entry_warning_strict"] = (
        pd.to_numeric(d.get("entry_upper_shadow"), errors="coerce").ge(0.50)
        & pd.to_numeric(d.get("entry_break_high20"), errors="coerce").le(-0.02)
    )
    return d


def warning_mask(d: pd.DataFrame, warning: str) -> pd.Series:
    if warning == "or":
        return d["entry_warning_or"].fillna(False)
    if warning == "strict":
        return d["entry_warning_strict"].fillna(False)
    return pd.Series(False, index=d.index)


def apply_early_action(candidates: pd.DataFrame, daily: pd.DataFrame, variant: dict[str, str]) -> pd.DataFrame:
    d = candidates.copy()
    d["early_exit_variant"] = variant["variant"]
    d["early_exit_note"] = "base_policy"
    d["original_policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    if variant["action"] == "none":
        return d

    maps = _price_maps(daily)
    cal = _trade_calendar(d["entry_date"].min(), d["policy_exit_date"].max() + pd.Timedelta(days=10))
    next_map = _next_trade_date_map(cal)
    warn = warning_mask(d, variant["warning"])

    for idx, row in d[warn & d["route"].astype(str).eq("strong_main")].iterrows():
        entry_date = pd.Timestamp(row["entry_date"]).normalize()
        entry_price = float(row["entry_price"])
        old_exit = pd.Timestamp(row["policy_exit_date"]).normalize()
        code = str(row["code"])
        d1 = next_map.get(entry_date)
        d2 = next_map.get(d1) if d1 is not None else None
        d3 = next_map.get(d2) if d2 is not None else None
        if d1 is None or d2 is None or d3 is None or entry_price <= 0:
            continue

        d1_close = maps["close"].get((code, d1))
        d2_close = maps["close"].get((code, d2))
        d2_open = maps["open"].get((code, d2))
        d3_open = maps["open"].get((code, d3))
        exit_date = None
        exit_open = None
        reason = None
        if d1_close is not None and float(d1_close) / entry_price - 1.0 <= -0.03:
            exit_date = d2
            exit_open = d2_open
            reason = "warning_d1_weak3_d2open"
        elif d2_close is not None and float(d2_close) / entry_price - 1.0 <= -0.03:
            exit_date = d3
            exit_open = d3_open
            reason = "warning_d2_weak3_d3open"
        if reason is None or exit_date is None or exit_open is None or exit_date >= old_exit:
            continue
        early_ret = _ret_from_price(float(exit_open), entry_price, 30.0)
        if early_ret is None:
            continue
        original_ret = float(row["policy_net_ret"])
        if variant["action"] == "fast_exit":
            d.at[idx, "policy_exit_date"] = exit_date
            d.at[idx, "policy_net_ret"] = early_ret
        elif variant["action"] == "half_proxy":
            d.at[idx, "policy_net_ret"] = 0.5 * float(early_ret) + 0.5 * original_ret
        d.at[idx, "early_exit_note"] = reason
        d.at[idx, "early_open_ret_30bps"] = early_ret

    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    return d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])


def apply_profile(candidates: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    d = candidates.copy()
    d["execution_profile"] = profile["profile"]
    shock = float(profile.get("shock", 0.0))
    if shock:
        d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce") - shock
        d["execution_note"] = "policy_extra_haircut2"
    else:
        d["execution_note"] = "policy_30bps"
    return d.dropna(subset=["policy_net_ret"])


def note_summary(candidates: pd.DataFrame, variant: str) -> dict[str, Any]:
    strong = candidates[candidates["route"].astype(str).eq("strong_main")].copy()
    triggered = strong[strong["early_exit_note"].astype(str).ne("base_policy")].copy()
    return {
        "variant": variant,
        "strong_rows": int(len(strong)),
        "triggered_rows": int(len(triggered)),
        "triggered_mean_original_ret": float(pd.to_numeric(triggered["original_policy_net_ret"], errors="coerce").mean()) if len(triggered) else None,
        "triggered_mean_new_ret": float(pd.to_numeric(triggered["policy_net_ret"], errors="coerce").mean()) if len(triggered) else None,
        "triggered_worst_new_ret": float(pd.to_numeric(triggered["policy_net_ret"], errors="coerce").min()) if len(triggered) else None,
        "note_counts": triggered["early_exit_note"].astype(str).value_counts().to_dict() if len(triggered) else {},
    }


def write_report(summary: pd.DataFrame, windows: pd.DataFrame, annual: pd.DataFrame, notes: pd.DataFrame) -> None:
    pct_cols = {
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "worst_open_mtm_ret",
        "strong_main_avg_ret",
        "return",
        "triggered_mean_original_ret",
        "triggered_mean_new_ret",
        "triggered_worst_new_ret",
    }
    lines = [
        "# G3 V4 strong_main 入场警戒 + D1/D2 早期确认退出测试 v1",
        "",
        "## 边界",
        "",
        "- 固定样本：`strong_second_score_ge_093`。",
        "- 不使用 score/rank 做新增过滤。",
        "- 入场警戒只来自入场日可见形态：`上影 >= 50%` 或 `收盘低于20日高点2%以上`；严格警戒为两者同时成立。",
        "- 早期弱确认为持仓后可见信号：D1 收盘相对入场 `<= -3%` 时 D2 开盘处理；否则 D2 收盘 `<= -3%` 时 D3 开盘处理。",
        "- `fast_exit` 是快速退出；`half_proxy` 是半仓降风险收益代理，不是完整现金流复算。",
        "- 本轮只验证结构方向，不做阈值搜索，不升级正式策略。",
        "",
        "## Full 汇总",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 触发诊断",
        "",
        md_table(notes, pct_cols=pct_cols),
        "",
        "## 窗口复核",
        "",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 年度复核",
        "",
        md_table(annual, pct_cols=pct_cols),
        "",
        "## 初步判断口径",
        "",
        "- 若 fast_exit 明显改善回撤但牺牲正常收益过大，应转向半仓/降风险而不是全平。",
        "- 若 half_proxy 能减少压力口径损伤且不压低核心窗口收益，才值得进入完整 slot/cash 复算。",
        "- 若宽警戒和严格警戒都无效，说明入场形态警戒不足，需要补板块退潮或 30m 可见强度。",
        "",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = attach_quality(load_candidates())
    daily = _load_daily_prices(base, extra_days=20)
    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    annual_rows: list[dict[str, Any]] = []
    note_rows: list[dict[str, Any]] = []

    for variant in VARIANTS:
        vname = variant["variant"]
        candidates = apply_early_action(base, daily, variant)
        candidates.to_csv(OUT_DIR / f"{vname}_candidates.csv", index=False, encoding="utf-8-sig")
        note_rows.append(note_summary(candidates, vname))
        for profile in PROFILES:
            pname = profile["profile"]
            stressed = apply_profile(candidates, profile)
            run_dir = OUT_DIR / f"{vname}__{pname}"
            run_dir.mkdir(parents=True, exist_ok=True)
            stressed.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve, closed = simulate_scaled(stressed, 30.0, VARIANT_ROUTE_LIMITS.get(BASE_VARIANT))
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(summarize(curve, closed, vname, pname))
            window_rows.extend([{"variant": vname, **x} for x in window_metrics(curve, closed, pname)])
            annual_rows.extend([{"variant": vname, **x} for x in annual_metrics(curve, closed, pname)])

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    annual = pd.DataFrame(annual_rows)
    notes = pd.DataFrame(note_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual_metrics.csv", index=False, encoding="utf-8-sig")
    notes.to_csv(OUT_DIR / "note_summary.csv", index=False, encoding="utf-8-sig")
    write_report(summary, windows, annual, notes)
    print(f"wrote {OUT_DIR}")
    print(summary[["variant", "profile", "trade_count", "total_return", "max_drawdown", "strong_main_pnl"]].to_string(index=False))
    print(notes.to_string(index=False))


if __name__ == "__main__":
    main()
