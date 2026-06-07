from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
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


SRC_DIR = ROOT / "reports" / "gen3_v4_strong_position_scale_probe_v1"
OUT_DIR = ROOT / "reports" / "gen3_v4_strong_visible_d1_exit_v1"
BASE_VARIANT = "strong_second_score_ge_093"

VARIANTS = [
    {"variant": "base_093", "profit_lock": None, "fail_stop": None},
    {"variant": "d1_profit_lock10_d2open", "profit_lock": 0.10, "fail_stop": None},
    {"variant": "d1_fail_stop6_d2open", "profit_lock": None, "fail_stop": -0.06},
    {"variant": "d1_lock10_fail6_d2open", "profit_lock": 0.10, "fail_stop": -0.06},
]

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
    for col in ["score", "entry_price", "policy_net_ret", "position_scale", "route_priority", "strong_day_rank"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["position_scale"] = d.get("position_scale", 1.0).fillna(1.0)
    return d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])


def apply_d1_exit(candidates: pd.DataFrame, daily: pd.DataFrame, variant: dict[str, Any]) -> pd.DataFrame:
    d = candidates.copy()
    d["d1_exit_variant"] = str(variant["variant"])
    d["d1_exit_note"] = "base_policy"
    if variant["variant"] == "base_093":
        return d

    maps = _price_maps(daily)
    cal = _trade_calendar(d["entry_date"].min(), d["policy_exit_date"].max() + pd.Timedelta(days=10))
    next_map = _next_trade_date_map(cal)
    profit_lock = variant.get("profit_lock")
    fail_stop = variant.get("fail_stop")

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
        d1_close = maps["close"].get((code, d1))
        d2_open = maps["open"].get((code, d2))
        if d1_close is None or d2_open is None:
            continue
        d1_ret = float(d1_close) / entry_price - 1.0
        reason = None
        if profit_lock is not None and d1_ret >= float(profit_lock):
            reason = f"d1_profit_lock_{float(profit_lock):.2f}"
        if fail_stop is not None and d1_ret <= float(fail_stop):
            reason = f"d1_fail_stop_{float(fail_stop):.2f}"
        if reason is None:
            continue
        old_exit = pd.Timestamp(row["policy_exit_date"]).normalize()
        if d2 >= old_exit:
            continue
        ret = _ret_from_price(float(d2_open), entry_price, 30.0)
        if ret is None:
            continue
        d.at[idx, "policy_exit_date"] = d2
        d.at[idx, "policy_net_ret"] = float(ret)
        d.at[idx, "d1_exit_note"] = reason
        d.at[idx, "d1_close_ret"] = d1_ret

    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    return d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])


def note_summary(candidates: pd.DataFrame, variant: str) -> dict[str, Any]:
    strong = candidates[candidates["route"].astype(str).eq("strong_main")].copy()
    early = strong[strong["d1_exit_note"].astype(str).ne("base_policy")].copy()
    return {
        "variant": variant,
        "strong_rows": int(len(strong)),
        "early_rows": int(len(early)),
        "early_mean_ret": float(pd.to_numeric(early["policy_net_ret"], errors="coerce").mean()) if len(early) else None,
        "early_worst_ret": float(pd.to_numeric(early["policy_net_ret"], errors="coerce").min()) if len(early) else None,
        "note_counts": early["d1_exit_note"].astype(str).value_counts().to_dict() if len(early) else {},
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
        "early_mean_ret",
        "early_worst_ret",
    }
    lines = [
        "# G3 V4 strong_main D1 可见退出测试 v1",
        "",
        "## 边界",
        "",
        "- 固定样本为 `strong_second_score_ge_093`。",
        "- 只对 `strong_main` 应用 D1 可见退出；down/range 不动。",
        "- 规则固定：D1 收盘相对入场价 `>= +10%` 时 D2 开盘止盈；D1 收盘 `<= -6%` 时 D2 开盘失败止损。",
        "- 这是研究测试，不调阈值，不进入正式策略。",
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
        "## 判断",
        "",
        "- 如果 D1 止盈明显降低 nextopen/haircut2 脆弱性，但牺牲正常收益过多，则说明退出不能靠简单提早兑现。",
        "- 如果 D1 失败止损对回撤帮助有限，说明强势链路风险来自更广泛的路径回吐，而不是少数首日失败。",
        "",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_candidates()
    daily = _load_daily_prices(base, extra_days=20)
    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    annual_rows: list[dict[str, Any]] = []
    note_rows: list[dict[str, Any]] = []

    for variant in VARIANTS:
        vname = str(variant["variant"])
        candidates = apply_d1_exit(base, daily, variant)
        candidates.to_csv(OUT_DIR / f"{vname}_candidates.csv", index=False, encoding="utf-8-sig")
        note_rows.append(note_summary(candidates, vname))
        for profile in TEST_PROFILES:
            pname = str(profile["profile"])
            stressed = apply_execution_profile(candidates, daily, profile)
            run_dir = OUT_DIR / f"{vname}__{pname}"
            run_dir.mkdir(parents=True, exist_ok=True)
            stressed.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve, closed = simulate_scaled(stressed, float(profile["cost_bps"]), VARIANT_ROUTE_LIMITS.get(BASE_VARIANT))
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
    print(summary[summary["profile"].isin(["close_30bps", "nextopen_30bps", "nextopen_haircut2_30bps"])][["variant", "profile", "trade_count", "total_return", "max_drawdown", "strong_main_pnl"]].to_string(index=False))


if __name__ == "__main__":
    main()
