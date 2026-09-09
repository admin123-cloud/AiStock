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
from scripts.gen3_test_v4_strong_position_scale_v1 import md_table, simulate_scaled, summarize  # noqa: E402


SOURCE = _report_path() / "gen3_v4_ice_down_panic_30m_confirm_overlay_v1" / "base_candidates_with_ice_30m_confirm.csv"
OUT_DIR = _report_path() / "gen3_v4_down_panic_neutral_d1d2_weak_exit_v1"
BASE_COST_BPS = 30.0

PROFILES = [
    {"profile": "cost30", "cost_bps": 30.0, "all_shock": 0.0},
    {"profile": "cost100", "cost_bps": 100.0, "all_shock": 0.0},
    {"profile": "cost30_all_shock2", "cost_bps": 30.0, "all_shock": 0.02},
]

VARIANTS = [
    {"variant": "base", "action": "none"},
    {"variant": "neutral_d1d2_weak3_fast_exit", "action": "fast_exit"},
    {"variant": "neutral_d1d2_weak3_half_proxy", "action": "half_proxy"},
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


def apply_variant(candidates: pd.DataFrame, daily: pd.DataFrame, variant: dict[str, str]) -> pd.DataFrame:
    d = candidates.copy()
    d["neutral_weak_exit_variant"] = variant["variant"]
    d["neutral_weak_exit_note"] = "base_policy"
    d["original_policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    if variant["action"] == "none":
        return d

    maps = _price_maps(daily)
    cal = _trade_calendar(d["entry_date"].min(), d["policy_exit_date"].max() + pd.Timedelta(days=10))
    next_map = _next_trade_date_map(cal)

    target = d["route"].astype(str).eq("down_panic") & d["emotion_signal"].astype(str).eq("neutral")
    for idx, row in d[target].iterrows():
        entry_date = pd.Timestamp(row["entry_date"]).normalize()
        old_exit = pd.Timestamp(row["policy_exit_date"]).normalize()
        entry_price = float(row["entry_price"])
        code = str(row["code"])
        if entry_price <= 0:
            continue
        d1 = next_map.get(entry_date)
        d2 = next_map.get(d1) if d1 is not None else None
        d3 = next_map.get(d2) if d2 is not None else None
        if d1 is None or d2 is None or d3 is None:
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
            reason = "d1_close_le_minus3_d2open"
        elif d2_close is not None and float(d2_close) / entry_price - 1.0 <= -0.03:
            exit_date = d3
            exit_open = d3_open
            reason = "d2_close_le_minus3_d3open"
        if reason is None or exit_date is None or exit_open is None or exit_date >= old_exit:
            continue
        early_ret = _ret_from_price(float(exit_open), entry_price, BASE_COST_BPS)
        if early_ret is None:
            continue
        original_ret = float(row["policy_net_ret"])
        if variant["action"] == "fast_exit":
            d.at[idx, "policy_exit_date"] = exit_date
            d.at[idx, "policy_net_ret"] = early_ret
        elif variant["action"] == "half_proxy":
            d.at[idx, "policy_net_ret"] = 0.5 * float(early_ret) + 0.5 * original_ret
        d.at[idx, "neutral_weak_exit_note"] = reason
        d.at[idx, "neutral_weak_exit_ret"] = early_ret

    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    return d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])


def apply_stress(candidates: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    d = candidates.copy()
    extra_cost = (float(profile["cost_bps"]) - BASE_COST_BPS) / 10000.0
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce") - extra_cost - float(profile.get("all_shock", 0.0))
    d["stress_profile"] = profile["profile"]
    return d.dropna(subset=["policy_net_ret"])


def note_summary(candidates: pd.DataFrame, variant: str) -> dict[str, Any]:
    target = candidates[candidates["route"].astype(str).eq("down_panic") & candidates["emotion_signal"].astype(str).eq("neutral")].copy()
    triggered = target[target["neutral_weak_exit_note"].astype(str).ne("base_policy")].copy()
    return {
        "variant": variant,
        "neutral_rows": int(len(target)),
        "triggered_rows": int(len(triggered)),
        "trigger_rate": float(len(triggered) / len(target)) if len(target) else 0.0,
        "triggered_original_avg_ret": float(pd.to_numeric(triggered["original_policy_net_ret"], errors="coerce").mean()) if len(triggered) else None,
        "triggered_new_avg_ret": float(pd.to_numeric(triggered["policy_net_ret"], errors="coerce").mean()) if len(triggered) else None,
        "triggered_worst_new_ret": float(pd.to_numeric(triggered["policy_net_ret"], errors="coerce").min()) if len(triggered) else None,
        "note_counts": triggered["neutral_weak_exit_note"].astype(str).value_counts().to_dict() if len(triggered) else {},
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_candidates()
    daily = _load_daily_prices(base, extra_days=20)
    summaries: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    for variant in VARIANTS:
        vname = variant["variant"]
        v = apply_variant(base, daily, variant)
        v.to_csv(OUT_DIR / f"{vname}_candidates.csv", index=False, encoding="utf-8-sig")
        notes.append(note_summary(v, vname))
        for profile in PROFILES:
            stressed = apply_stress(v, profile)
            curve, closed = simulate_scaled(stressed, float(profile["cost_bps"]))
            run_dir = OUT_DIR / f"{vname}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summaries.append(summarize(curve, closed, vname, str(profile["profile"])))
    summary = pd.DataFrame(summaries)
    note_df = pd.DataFrame(notes)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    note_df.to_csv(OUT_DIR / "note_summary.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "worst_open_mtm_ret",
        "down_panic_avg_ret",
        "trigger_rate",
        "triggered_original_avg_ret",
        "triggered_new_avg_ret",
        "triggered_worst_new_ret",
    }
    lines = [
        "# G3 V4 down_panic neutral D1/D2 早弱退出测试 v1",
        "",
        "## 研究目的",
        "",
        "- 只对 `route=down_panic` 且 `emotion_signal=neutral` 生效。",
        "- 不使用 score/rank 过滤，不改变候选源，只测试持仓后 D1/D2 早期弱确认是否应该快速退出或减仓。",
        "- 规则固定：D1 收盘相对入场价 `<= -3%`，D2 开盘处理；否则 D2 收盘 `<= -3%`，D3 开盘处理。",
        "",
        "## 触发诊断",
        "",
        md_table(note_df, pct_cols=pct_cols),
        "",
        "## slot 复算结果",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 判断口径",
        "",
        "- 如果 `fast_exit` 明显提升压力口径但压低正常收益，优先考虑 `half_proxy`。",
        "- 如果二者都无效，说明 neutral 的问题不是早弱退出，而是入场源本身要重建。",
        "- 如果只改善 2025/2026 类局部年份，不能直接升为正式规则，需要再做年度稳定性拆分。",
        "",
        "## 本轮实际结论",
        "",
        "- `D1/D2 <= -3%` 早弱退出触发太少：32 个 `down_panic neutral` 候选只触发 1 个。",
        "- `fast_exit` 只把该样本从 `-5.55%` 修到 `-4.11%`，全局 `cost30` 从 `+374.23%` 到 `+375.31%`，`all_shock2` 从 `+46.72%` 到 `+47.03%`，改善幅度不足以形成规则。",
        "- 这说明 neutral 的问题不是“已经明显跌破后的止损太慢”，而是 D1/D2 早期没有形成足够修复力度时，仍继续持有。",
        "",
        "## 下一步目标",
        "",
        "继续测试 `D1/D2 无修复退出`：如果持仓后前两天没有出现足够的反弹幅度或收盘修复，就在 D3 开盘快速退出或半仓降风险。这个方向比继续调止损阈值更接近 neutral 失败根部。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
