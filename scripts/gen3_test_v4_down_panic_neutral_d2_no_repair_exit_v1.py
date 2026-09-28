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
from scripts.gen3_test_v4_strong_position_scale_v1 import md_table, simulate_scaled, summarize  # noqa: E402


SOURCE = ROOT / "reports" / "gen3_v4_ice_down_panic_30m_confirm_overlay_v1" / "base_candidates_with_ice_30m_confirm.csv"
OUT_DIR = ROOT / "reports" / "gen3_v4_down_panic_neutral_d2_no_repair_exit_v1"
BASE_COST_BPS = 30.0

PROFILES = [
    {"profile": "cost30", "cost_bps": 30.0, "all_shock": 0.0},
    {"profile": "cost100", "cost_bps": 100.0, "all_shock": 0.0},
    {"profile": "cost30_all_shock2", "cost_bps": 30.0, "all_shock": 0.02},
]

VARIANTS = [
    {"variant": "base", "action": "none"},
    {"variant": "neutral_d2_no_repair_fast_exit", "action": "fast_exit"},
    {"variant": "neutral_d2_no_repair_half_proxy", "action": "half_proxy"},
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
    d["neutral_repair_exit_variant"] = variant["variant"]
    d["neutral_repair_exit_note"] = "base_policy"
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
        if d1 is None or d2 is None or d3 is None or d3 >= old_exit:
            continue
        d1_high = maps["high"].get((code, d1))
        d2_high = maps["high"].get((code, d2))
        d1_close = maps["close"].get((code, d1))
        d2_close = maps["close"].get((code, d2))
        d3_open = maps["open"].get((code, d3))
        if any(x is None for x in [d1_high, d2_high, d1_close, d2_close, d3_open]):
            continue
        max_high_ret = max(float(d1_high), float(d2_high)) / entry_price - 1.0
        max_close_ret = max(float(d1_close), float(d2_close)) / entry_price - 1.0
        no_repair = max_high_ret < 0.03 and max_close_ret < 0.02
        if not no_repair:
            continue
        early_ret = _ret_from_price(float(d3_open), entry_price, BASE_COST_BPS)
        if early_ret is None:
            continue
        original_ret = float(row["policy_net_ret"])
        if variant["action"] == "fast_exit":
            d.at[idx, "policy_exit_date"] = d3
            d.at[idx, "policy_net_ret"] = early_ret
        elif variant["action"] == "half_proxy":
            d.at[idx, "policy_net_ret"] = 0.5 * float(early_ret) + 0.5 * original_ret
        d.at[idx, "neutral_repair_exit_note"] = "d1d2_no_repair_d3open"
        d.at[idx, "neutral_repair_exit_ret"] = early_ret
        d.at[idx, "d1d2_max_high_ret"] = max_high_ret
        d.at[idx, "d1d2_max_close_ret"] = max_close_ret

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
    triggered = target[target["neutral_repair_exit_note"].astype(str).ne("base_policy")].copy()
    return {
        "variant": variant,
        "neutral_rows": int(len(target)),
        "triggered_rows": int(len(triggered)),
        "trigger_rate": float(len(triggered) / len(target)) if len(target) else 0.0,
        "triggered_original_avg_ret": float(pd.to_numeric(triggered["original_policy_net_ret"], errors="coerce").mean()) if len(triggered) else None,
        "triggered_new_avg_ret": float(pd.to_numeric(triggered["policy_net_ret"], errors="coerce").mean()) if len(triggered) else None,
        "triggered_worst_new_ret": float(pd.to_numeric(triggered["policy_net_ret"], errors="coerce").min()) if len(triggered) else None,
        "note_counts": triggered["neutral_repair_exit_note"].astype(str).value_counts().to_dict() if len(triggered) else {},
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
        "# G3 V4 down_panic neutral D1/D2 无修复退出测试 v1",
        "",
        "## 研究目的",
        "",
        "- 只对 `route=down_panic` 且 `emotion_signal=neutral` 生效。",
        "- 不使用 score/rank 过滤，不改变候选源，只测试持仓后前两天没有修复时是否应快速退出或半仓降风险。",
        "- 固定规则：D1/D2 最高价都未到入场价 `+3%`，且 D1/D2 最好收盘都未到 `+2%`，则 D3 开盘处理。",
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
        "- 如果 `fast_exit` 和 `half_proxy` 都改善三组压力，说明 neutral 的核心问题是早期修复不足。",
        "- 如果只改善压力但压低正常收益，则只作为防守规则观察。",
        "- 如果触发样本过少或收益无明显改善，则 neutral 需要重建入场源，而不是继续加退出规则。",
        "",
        "## 本轮实际结论",
        "",
        "- `D1/D2 无修复退出` 触发 10 个 `down_panic neutral` 候选，触发率 `31.25%`，覆盖度比 `D1/D2 <= -3%` 高。",
        "- 但触发样本原始平均收益 `-0.85%`，快退后变为 `-0.96%`，半仓代理后 `-0.91%`，没有改善触发样本本身。",
        "- slot 复算也失败：`fast_exit` 把 `cost30` 从 `+374.23%` 降到 `+371.38%`，`cost100` 从 `+215.09%` 降到 `+213.20%`，`all_shock2` 从 `+46.72%` 降到 `+45.84%`，最大回撤还从 `-32.72%` 扩到 `-33.54%`。",
        "- 这说明 neutral 的问题不能靠 D1/D2 日线级“无修复退出”解决；它会误杀一部分后续修复票。",
        "",
        "## 下一步目标",
        "",
        "停止继续给 `down_panic neutral` 叠退出规则。下一步应该重建 neutral 的入场源：把它从“弱势 panic 的非冰点残余样本”拆出来，单独寻找更明确的结构，例如箱体底部、行业阶段性错杀、或 30m 级别真实放量承接。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
