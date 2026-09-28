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
from scripts.gen3_stress_v4_strong_second_093_execution_v1 import (  # noqa: E402
    annual_metrics,
    apply_execution_profile,
    window_metrics,
)
from scripts.gen3_test_v4_strong_position_scale_v1 import (  # noqa: E402
    VARIANT_ROUTE_LIMITS,
    md_table,
    simulate_scaled,
    summarize,
)


SRC_DIR = _report_path() / "gen3_v4_strong_position_scale_probe_v1"
OUT_DIR = _report_path() / "gen3_v4_strong_visible_d1_half_lock_v1"
BASE_VARIANT = "strong_second_score_ge_093"

TEST_PROFILES = [
    {"profile": "close_30bps", "mode": "close", "cost_bps": 30.0, "shock": 0.0},
    {"profile": "nextopen_30bps", "mode": "nextopen", "cost_bps": 30.0, "shock": 0.0},
    {"profile": "nextopen_haircut2_30bps", "mode": "nextopen", "cost_bps": 30.0, "shock": 0.02},
]


def load_candidates() -> pd.DataFrame:
    d = pd.read_csv(SRC_DIR / f"{BASE_VARIANT}_candidates.csv", low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in ["entry_price", "policy_net_ret", "position_scale", "route_priority"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["position_scale"] = d.get("position_scale", 1.0).fillna(1.0)
    return d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])


def apply_half_profit_lock(candidates: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    d = candidates.copy()
    d["half_lock_variant"] = "d1_profit_lock10_half_d2open"
    d["half_lock_note"] = "base_policy"
    d["d1_close_ret"] = pd.NA
    d["d2_open_ret_30bps"] = pd.NA
    d["original_policy_net_ret"] = d["policy_net_ret"]

    maps = _price_maps(daily)
    cal = _trade_calendar(d["entry_date"].min(), d["policy_exit_date"].max() + pd.Timedelta(days=10))
    next_map = _next_trade_date_map(cal)

    for idx, row in d.iterrows():
        if str(row.get("route")) != "strong_main":
            continue
        code = str(row["code"])
        entry_date = pd.Timestamp(row["entry_date"]).normalize()
        entry_price = float(row["entry_price"])
        d1 = next_map.get(entry_date)
        d2 = next_map.get(d1) if d1 is not None else None
        if d1 is None or d2 is None or entry_price <= 0:
            continue
        old_exit = pd.Timestamp(row["policy_exit_date"]).normalize()
        if d2 >= old_exit:
            continue
        d1_close = maps["close"].get((code, d1))
        d2_open = maps["open"].get((code, d2))
        if d1_close is None or d2_open is None:
            continue
        d1_ret = float(d1_close) / entry_price - 1.0
        if d1_ret < 0.10:
            continue
        d2_ret = _ret_from_price(float(d2_open), entry_price, 30.0)
        if d2_ret is None:
            continue
        d.at[idx, "half_lock_note"] = "d1_profit_lock10_half_d2open_proxy"
        d.at[idx, "d1_close_ret"] = d1_ret
        d.at[idx, "d2_open_ret_30bps"] = float(d2_ret)
        d.at[idx, "half_lock_proxy_ret_30bps"] = 0.5 * float(d2_ret) + 0.5 * float(row["policy_net_ret"])

    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    return d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])


def apply_half_lock_after_profile(stressed: pd.DataFrame, daily: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    d = stressed.copy()
    if "half_lock_note" not in d.columns:
        return d
    mask = d["half_lock_note"].astype(str).eq("d1_profit_lock10_half_d2open_proxy")
    if not bool(mask.any()):
        return d

    maps = _price_maps(daily)
    cal = _trade_calendar(d["entry_date"].min(), d["policy_exit_date"].max() + pd.Timedelta(days=10))
    next_map = _next_trade_date_map(cal)
    cost_bps = float(profile["cost_bps"])
    shock = float(profile.get("shock", 0.0))

    for idx, row in d[mask].iterrows():
        code = str(row["code"])
        entry_date = pd.Timestamp(row["entry_date"]).normalize()
        entry_price = float(row["entry_price"])
        d1 = next_map.get(entry_date)
        d2 = next_map.get(d1) if d1 is not None else None
        if d2 is None or entry_price <= 0:
            continue
        d2_open = maps["open"].get((code, d2))
        d2_ret = _ret_from_price(float(d2_open), entry_price, cost_bps) if d2_open is not None else None
        if d2_ret is None:
            continue
        original_profile_ret = float(row["policy_net_ret"])
        early_half_ret = float(d2_ret) - shock
        d.at[idx, "original_profile_net_ret"] = original_profile_ret
        d.at[idx, "early_half_profile_ret"] = early_half_ret
        d.at[idx, "policy_net_ret"] = 0.5 * early_half_ret + 0.5 * original_profile_ret
        d.at[idx, "execution_note"] = f"{row.get('execution_note', '')}|half_lock_proxy"
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    return d.dropna(subset=["policy_net_ret"])


def note_summary(candidates: pd.DataFrame, variant: str) -> dict[str, Any]:
    strong = candidates[candidates["route"].astype(str).eq("strong_main")].copy()
    early = strong[strong["half_lock_note"].astype(str).ne("base_policy")].copy()
    return {
        "variant": variant,
        "strong_rows": int(len(strong)),
        "half_lock_rows": int(len(early)),
        "half_lock_mean_original_ret": float(pd.to_numeric(early["original_policy_net_ret"], errors="coerce").mean()) if len(early) else None,
        "half_lock_mean_proxy_ret_30bps": float(pd.to_numeric(early["half_lock_proxy_ret_30bps"], errors="coerce").mean()) if len(early) else None,
        "half_lock_worst_proxy_ret_30bps": float(pd.to_numeric(early["half_lock_proxy_ret_30bps"], errors="coerce").min()) if len(early) else None,
        "note_counts": early["half_lock_note"].astype(str).value_counts().to_dict() if len(early) else {},
    }


def run_variant(candidates: pd.DataFrame, daily: pd.DataFrame, variant: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    annual_rows: list[dict[str, Any]] = []
    candidates.to_csv(OUT_DIR / f"{variant}_candidates.csv", index=False, encoding="utf-8-sig")
    for profile in TEST_PROFILES:
        pname = str(profile["profile"])
        stressed = apply_execution_profile(candidates, daily, profile)
        if variant == "d1_profit_lock10_half_d2open":
            stressed = apply_half_lock_after_profile(stressed, daily, profile)
        run_dir = OUT_DIR / f"{variant}__{pname}"
        run_dir.mkdir(parents=True, exist_ok=True)
        stressed.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
        curve, closed = simulate_scaled(stressed, float(profile["cost_bps"]), VARIANT_ROUTE_LIMITS.get(BASE_VARIANT))
        curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        summary_rows.append(summarize(curve, closed, variant, pname))
        window_rows.extend([{"variant": variant, **x} for x in window_metrics(curve, closed, pname)])
        annual_rows.extend([{"variant": variant, **x} for x in annual_metrics(curve, closed, pname)])
    return summary_rows, window_rows, annual_rows


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
        "half_lock_mean_original_ret",
        "half_lock_mean_proxy_ret_30bps",
        "half_lock_worst_proxy_ret_30bps",
    }
    lines = [
        "# G3 V4 strong_main D1 半仓利润保护测试 v1",
        "",
        "## 边界",
        "",
        "- 固定样本：`strong_second_score_ge_093`。",
        "- 只对 `strong_main` 做研究代理；down/range 链路不动。",
        "- 固定规则：D1 收盘相对入场价 `>= +10%` 时，D2 开盘视为减半仓，剩余半仓继续走原策略退出。",
        "- 这是收益率层面的半仓代理，不是完整现金流复算；只能判断方向价值，不能直接升级正式规则。",
        "- 没有调参搜索，目的是避免把上一轮 D1 满仓退出的坏结果继续过拟合。",
        "",
        "## Full 汇总",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 窗口复核",
        "",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 年度复核",
        "",
        md_table(annual, pct_cols=pct_cols),
        "",
        "## 触发诊断",
        "",
        md_table(notes, pct_cols=pct_cols),
        "",
        "## 初步判断",
        "",
        "- 如果半仓保护只小幅改善 nextopen/haircut2，但明显牺牲 close 收益，则说明强势退出不应该用简单 D1 固定止盈。",
        "- 如果半仓保护能在压力口径改善回撤，同时不明显牺牲 close 收益，才值得进入完整 slot/cash 复算。",
        "",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_candidates()
    daily = _load_daily_prices(base, extra_days=20)
    half = apply_half_profit_lock(base, daily)

    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    annual_rows: list[dict[str, Any]] = []
    note_rows = [
        note_summary(base.assign(half_lock_note="base_policy", original_policy_net_ret=base["policy_net_ret"]), "base_093"),
        note_summary(half, "d1_profit_lock10_half_d2open"),
    ]

    for variant, candidates in [("base_093", base), ("d1_profit_lock10_half_d2open", half)]:
        s, w, a = run_variant(candidates, daily, variant)
        summary_rows.extend(s)
        window_rows.extend(w)
        annual_rows.extend(a)

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
    cols = ["variant", "profile", "trade_count", "total_return", "max_drawdown", "strong_main_pnl"]
    print(summary[cols].to_string(index=False))
    print(notes.to_string(index=False))


if __name__ == "__main__":
    main()
