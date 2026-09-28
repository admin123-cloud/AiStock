from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_audit_guarded_execution_stress_v1 import (  # noqa: E402
    load_daily_ohlc as formal_load_daily_ohlc,
    prepare_daily_maps as formal_prepare_daily_maps,
    route_attribution as formal_route_attribution,
    simulate as formal_simulate,
    summarize as formal_summarize,
    summarize_windows as formal_summarize_windows,
)
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar  # noqa: E402
from scripts.gen3_test_range_30m_volume_acceptance_v1 import PROFILES, standardize, window_metrics  # noqa: E402
from scripts.gen3_test_range_box_stress_icepoint_climax_overlay_v1 import md_table, simulate_scaled, summarize  # noqa: E402


RANGE_SOURCE = ROOT / "reports" / "gen3_range_second_acceptance_v1" / "deep_and_reclaim_d1_second_accept_signals.csv"
COMBO_SOURCE = ROOT / "reports" / "gen3_struct_veto_combo_e_candidate_v1" / "close_30bps_stressed_candidates.csv"
OUT_DIR = ROOT / "reports" / "gen3_range_icepoint_d1_second_accept_overlay_v1"

BACKTEST_START = "2020-01-01"
BACKTEST_END = "2026-05-29"
CURVE_END = "2026-06-04"


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def load_ice_d1_signals() -> pd.DataFrame:
    d = pd.read_csv(RANGE_SOURCE, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    d["emotion_signal"] = d.get("emotion_signal", "").astype(str)
    d["d1_second_accept"] = d.get("d1_second_accept", False).astype(str).str.lower().isin(["true", "1"])
    d = d[d["emotion_signal"].eq("icepoint") & d["d1_second_accept"]].copy()
    d["variant"] = "icepoint_d1_second_accept"
    d["desc"] = "冰点环境下，箱体底部日线修复后，D1再次出现30m放量承接"
    d["route"] = "range_ice_d1_second_accept"
    d["route_source"] = "icepoint_box_floor_reclaim_d1_30m_accept"
    d["route_priority"] = 3
    d["hold_days"] = 5
    d["rank_key"] = pd.to_numeric(d.get("rank_key", d.get("candidate_score", 0.0)), errors="coerce").fillna(0.0)
    return d.dropna(subset=["entry_date", "code", "entry_price_adjusted", "fwd_ret_confirm_to_close_5d"]).copy()


def load_combo_close30() -> pd.DataFrame:
    d = pd.read_csv(COMBO_SOURCE, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    d["variant"] = "struct_veto_combo_e"
    d["desc"] = "G3结构化否决组合E，30bps收盘成交压力口径"
    d["entry_date_ts"] = d["entry_date"]
    d["entry_price_used"] = pd.to_numeric(d["entry_price"], errors="coerce")
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    d["rank_key"] = pd.to_numeric(d.get("score", 0.0), errors="coerce").fillna(0.0)
    d["position_scale"] = 1.0
    return d.dropna(subset=["entry_date_ts", "policy_exit_date", "code", "entry_price_used", "policy_net_ret"]).copy()


def standardize_ice(signals: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    d = standardize(signals, profile)
    d["route"] = "range_ice_d1_second_accept"
    d["route_source"] = "icepoint_box_floor_reclaim_d1_30m_accept"
    d["route_priority"] = 3
    d["position_scale"] = 1.0
    d["rank_key"] = pd.to_numeric(d["rank_key"], errors="coerce").fillna(0.0)
    return d


def formalize_for_combo(d: pd.DataFrame, source_group: str) -> pd.DataFrame:
    out = d.copy()
    out["entry_date"] = pd.to_datetime(out["entry_date"], errors="coerce").dt.normalize()
    out["policy_exit_date"] = pd.to_datetime(out["policy_exit_date"], errors="coerce").dt.normalize()
    out["entry_price"] = pd.to_numeric(out.get("entry_price_used", out.get("entry_price")), errors="coerce")
    out["policy_net_ret"] = pd.to_numeric(out["policy_net_ret"], errors="coerce")
    out["score"] = pd.to_numeric(out.get("score", out.get("rank_key", 0.0)), errors="coerce").fillna(0.0)
    out["route_priority"] = pd.to_numeric(out.get("route_priority", 0), errors="coerce").fillna(0).astype(int)
    out["route"] = out.get("route", source_group).astype(str)
    out["source_group"] = source_group
    if "limitdown_delayed" not in out.columns:
        out["limitdown_delayed"] = False
    else:
        out["limitdown_delayed"] = out["limitdown_delayed"].fillna(False).astype(bool)
    if "execution_delay_days" not in out.columns:
        out["execution_delay_days"] = 0
    else:
        out["execution_delay_days"] = pd.to_numeric(out["execution_delay_days"], errors="coerce").fillna(0)
    return out.dropna(subset=["entry_date", "policy_exit_date", "code", "entry_price", "policy_net_ret"]).copy()


def prepare_combo_plus(combo: pd.DataFrame, ice: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    base_keys = set(zip(combo["entry_date"].dt.strftime("%Y-%m-%d"), combo["code"].astype(str)))
    overlay = ice.copy()
    overlay["dedupe_key"] = list(zip(overlay["entry_date"].dt.strftime("%Y-%m-%d"), overlay["code"].astype(str)))
    overlay["overlap_with_combo_same_day_code"] = overlay["dedupe_key"].isin(base_keys)
    unique_overlay = overlay[~overlay["overlap_with_combo_same_day_code"]].drop(columns=["dedupe_key"]).copy()

    base = formalize_for_combo(combo, "combo_e")
    unique_overlay = formalize_for_combo(unique_overlay, "icepoint_d1_overlay")
    combined = pd.concat([base, unique_overlay], ignore_index=True, sort=False)
    combined["policy_exit_date"] = pd.to_datetime(combined["policy_exit_date"], errors="coerce").dt.normalize()
    return combined, overlay


def route_attribution(closed: pd.DataFrame, label: str) -> pd.DataFrame:
    rows = []
    if closed.empty:
        return pd.DataFrame()
    for source_group, g in closed.groupby("source_group", dropna=False):
        net = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        rows.append(
            {
                "variant": label,
                "source_group": source_group,
                "trade_count": int(len(g)),
                "win_rate": float((net > 0).mean()) if len(net) else 0.0,
                "avg_trade_return": float(net.mean()) if len(net) else 0.0,
                "pnl": float(pd.to_numeric(g["realized_pnl"], errors="coerce").sum()),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ice_signals = load_ice_d1_signals()
    combo = load_combo_close30()
    ice_signals.to_csv(OUT_DIR / "icepoint_d1_second_accept_signals.csv", index=False, encoding="utf-8-sig")

    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    route_rows: list[pd.DataFrame] = []

    formal_combo = formalize_for_combo(combo, "combo_e")
    formal_daily = formal_load_daily_ohlc(formal_combo)
    formal_calendar = _trade_calendar(formal_combo["entry_date"].min(), formal_combo["policy_exit_date"].max() + pd.Timedelta(days=20))
    formal_close_map = {(str(r.code), pd.Timestamp(r.trade_date).normalize()): float(r.close) for r in formal_daily.itertuples(index=False)}
    combo_curve, combo_closed = formal_simulate(formal_combo, formal_calendar, formal_close_map)
    combo_curve.to_csv(OUT_DIR / "combo_e_close30_formal_resim_mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
    combo_closed.to_csv(OUT_DIR / "combo_e_close30_formal_resim_closed_trades.csv", index=False, encoding="utf-8-sig")
    combo_row = formal_summarize(combo_curve, combo_closed, "close_30bps_formal_resim")
    combo_row["variant"] = "combo_e_close30_formal_resim"
    combo_row["desc"] = "combo_e 30bps正式模拟器复算基线"
    summary_rows.append(combo_row)
    for w in formal_summarize_windows(combo_curve, combo_closed, "close_30bps_formal_resim"):
        w["variant"] = "combo_e_close30_formal_resim"
        window_rows.append(w)

    for profile in PROFILES:
        ice_candidates = standardize_ice(ice_signals, profile)
        ice_curve, ice_closed = simulate_scaled(ice_candidates)
        run_dir = OUT_DIR / f"icepoint_d1_second_accept__{profile['profile']}"
        run_dir.mkdir(parents=True, exist_ok=True)
        ice_candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
        ice_curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
        ice_closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        summary_rows.append(summarize(ice_curve, ice_closed, "icepoint_d1_second_accept", str(profile["profile"]), "冰点+D1二次承接独立候选源"))
        window_rows.extend(window_metrics(ice_curve, ice_closed, "icepoint_d1_second_accept", str(profile["profile"])))

        if profile["profile"] == "cost30":
            combined, overlay_audit = prepare_combo_plus(combo, ice_candidates)
            formal_daily_plus = formal_load_daily_ohlc(combined)
            formal_calendar_plus = _trade_calendar(combined["entry_date"].min(), combined["policy_exit_date"].max() + pd.Timedelta(days=20))
            formal_close_map_plus = {
                (str(r.code), pd.Timestamp(r.trade_date).normalize()): float(r.close) for r in formal_daily_plus.itertuples(index=False)
            }
            combo_plus_curve, combo_plus_closed = formal_simulate(combined, formal_calendar_plus, formal_close_map_plus)
            combined.to_csv(OUT_DIR / "combo_e_plus_icepoint_d1_candidates.csv", index=False, encoding="utf-8-sig")
            overlay_audit.to_csv(OUT_DIR / "icepoint_d1_overlay_overlap_audit.csv", index=False, encoding="utf-8-sig")
            combo_plus_curve.to_csv(OUT_DIR / "combo_e_plus_icepoint_d1_mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            combo_plus_closed.to_csv(OUT_DIR / "combo_e_plus_icepoint_d1_closed_trades.csv", index=False, encoding="utf-8-sig")
            plus_row = formal_summarize(combo_plus_curve, combo_plus_closed, "close_30bps_plus_cost30_formal")
            plus_row["variant"] = "combo_e_plus_icepoint_d1"
            plus_row["desc"] = "combo_e叠加冰点D1二次承接，去除同日同股重复"
            summary_rows.append(plus_row)
            for w in formal_summarize_windows(combo_plus_curve, combo_plus_closed, "close_30bps_plus_cost30_formal"):
                w["variant"] = "combo_e_plus_icepoint_d1"
                window_rows.append(w)
            route_rows.append(route_attribution(combo_plus_closed, "combo_e_plus_icepoint_d1"))
            formal_route = formal_route_attribution(combo_plus_closed, "close_30bps_plus_cost30_formal")
            formal_route.insert(0, "variant", "combo_e_plus_icepoint_d1")
            formal_route.to_csv(OUT_DIR / "formal_route_attribution.csv", index=False, encoding="utf-8-sig")

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    routes = pd.concat(route_rows, ignore_index=True) if route_rows else pd.DataFrame()

    summary.insert(0, "backtest_start", BACKTEST_START)
    summary.insert(1, "backtest_end", BACKTEST_END)
    summary.insert(2, "curve_end", CURVE_END)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    routes.to_csv(OUT_DIR / "route_attribution.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "return", "pnl"}
    report = "\n".join(
        [
            "# G3 冰点+D1二次承接独立候选源叠加复验 v1",
            "",
            "## 回测范围",
            f"- 候选信号/入场窗口：{BACKTEST_START} 至 {BACKTEST_END}。",
            f"- 资金曲线结算至：{CURVE_END}。",
            "- 复验对象：`icepoint_d1_second_accept`，中文意思是“冰点环境下，箱体底部日线修复后，D1再次出现30m放量承接”。",
            "- 叠加方式：和 `struct_veto_combo_e` 去除同日同股重复后合并，仍用5槽、每日最多开1笔的完整 slot 复算。",
            "",
            "## 策略名解释",
            "- `icepoint_d1_second_accept`：冰点+D1二次承接。只在情绪冰点窗口内，买箱体底部修复后第二天继续出现30m放量承接的票。",
            "- `combo_e_close30_resim`：G3结构化否决组合E的30bps收盘成交基线复算。",
            "- `combo_e_plus_icepoint_d1`：在 combo_e 基础上叠加冰点+D1二次承接，观察是否提供新增收益。",
            "- `cost30`：单笔按30bps交易成本扣减。",
            "- `cost100`：单笔按100bps高摩擦成本扣减。",
            "- `shock2_cost30`：30bps成本外，再额外扣2%冲击，用来模拟低开、滑点或执行偏差压力。",
            "",
            "## 汇总结果",
            md_table(summary, pct_cols=pct_cols),
            "",
            "## 分窗口结果",
            md_table(windows, pct_cols=pct_cols),
            "",
            "## 叠加后来源贡献",
            md_table(routes, pct_cols=pct_cols),
            "",
            "## 初步判断口径",
            "- 如果独立候选源在高摩擦/冲击下仍稳定，但叠加 combo_e 后新增交易很少，说明它更适合作为候选标签而不是主策略。",
            "- 如果叠加后回撤下降或收益明显增加，并且增量不是集中在少数股票，再进入下一轮成交集中度审计。",
        ]
    )
    (OUT_DIR / "REPORT.md").write_text(report + "\n", encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
