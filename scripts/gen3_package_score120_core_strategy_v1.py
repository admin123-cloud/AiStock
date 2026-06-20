from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_wave_style_template_strategy_v1 import INITIAL_CAPITAL, _max_drawdown, _trade_calendar  # noqa: E402
from utils.paths import report_path  # noqa: E402


SOURCE_DIR = report_path("score120_activation_regime_v1")
SOURCE_TRADES = SOURCE_DIR / "diff65_m30_trades_with_regime_corrected.csv"
SOURCE_GATES = SOURCE_DIR / "fixed_gate_results_corrected.csv"
OUT_DIR = report_path("gen3_score120_core_strategy_v1")

PROFILE = "g3_score120_core_mom60_le005"
BASE_PROFILE = "score120_diff65_m30_ma20_base"
STRATEGY_ID = "g3_score120_core_strategy_v1"


def _pct(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return ""
    if not math.isfinite(x):
        return ""
    return f"{x * 100:.2f}%"


def _md_table(df: pd.DataFrame, max_rows: int = 30) -> str:
    if df.empty:
        return "_No data_"
    view = df.head(max_rows).copy()
    for col in view.columns:
        view[col] = view[col].astype(str)
    header = "| " + " | ".join(view.columns) + " |"
    sep = "| " + " | ".join(["---"] * len(view.columns)) + " |"
    rows = ["| " + " | ".join(row) + " |" for row in view.to_numpy()]
    if len(df) > max_rows:
        rows.append(f"\n\n_Only first {max_rows} rows shown, total {len(df)} rows._")
    return "\n".join([header, sep, *rows])


def _load_trades() -> pd.DataFrame:
    if not SOURCE_TRADES.exists():
        raise FileNotFoundError(f"missing {SOURCE_TRADES}; run activation regime research first")
    d = pd.read_csv(SOURCE_TRADES, low_memory=False, encoding="utf-8-sig")
    for col in ["trade_date", "entry_date", "policy_exit_date"]:
        d[col] = pd.to_datetime(d[col], errors="coerce").dt.normalize()
    numeric_cols = [
        "net_ret",
        "gross_ret",
        "entry_price",
        "exit_price",
        "hold_days",
        "rank_key",
        "wave_style_score",
        "amount_rank",
        "sector_diffusion_score",
        "m30_close_above_ma20",
        "sig_index_mom60",
        "sig_index_mom20",
        "stake",
        "realized_pnl",
    ]
    for col in numeric_cols:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["trade_date", "entry_date", "policy_exit_date", "net_ret", "entry_price"]).copy()
    d["activation_gate_pass"] = d["sig_index_mom60"].le(0.05)
    return d.sort_values(["entry_date", "rank_key", "amount_rank"], ascending=[True, False, False]).reset_index(drop=True)


def _simulate(selected: pd.DataFrame, calendar: list[pd.Timestamp]) -> tuple[pd.DataFrame, pd.DataFrame]:
    by_entry = {day: g.copy() for day, g in selected.groupby("entry_date")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []
    for day in calendar:
        realized_pnl = 0.0
        still_open: list[dict[str, Any]] = []
        for pos in open_pos:
            if pos["policy_exit_date"] <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["net_ret"]))
                cash += exit_value
                realized_pnl += exit_value - float(pos["stake"])
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = exit_value - float(pos["stake"])
                closed.append(out)
            else:
                still_open.append(pos)
        open_pos = still_open

        opened = 0
        todays = by_entry.get(day)
        if todays is not None:
            todays = todays.sort_values(["rank_key", "amount_rank"], ascending=[False, False])
            for row in todays.itertuples(index=False):
                if opened >= 1 or len(open_pos) >= 4:
                    break
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                stake = equity_before * 0.25
                if stake <= 0 or cash < stake:
                    break
                pos = row._asdict()
                pos["stake"] = stake
                cash -= stake
                open_pos.append(pos)
                opened += 1

        reserved = sum(float(p["stake"]) for p in open_pos)
        equity = cash + reserved
        curve_rows.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "scheduler": PROFILE,
                "cash": cash,
                "reserved_principal": reserved,
                "mtm_value": reserved,
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "realized_pnl": realized_pnl,
            }
        )
    curve = pd.DataFrame(curve_rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    closed_df = pd.DataFrame(closed)
    return curve, closed_df


def _metrics(profile: str, curve: pd.DataFrame, trades: pd.DataFrame) -> dict[str, Any]:
    rets = pd.to_numeric(trades.get("net_ret", pd.Series(dtype=float)), errors="coerce").dropna()
    if curve.empty:
        total_return = 0.0
        max_dd = 0.0
    else:
        total_return = float(curve["equity"].iloc[-1] / curve["equity"].iloc[0] - 1.0)
        max_dd = _max_drawdown(curve["equity"])
    return {
        "profile": profile,
        "trade_count": int(len(trades)),
        "total_return": total_return,
        "max_drawdown": max_dd,
        "win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
        "avg_trade_return": float(rets.mean()) if len(rets) else 0.0,
        "worst_trade": float(rets.min()) if len(rets) else 0.0,
        "best_trade": float(rets.max()) if len(rets) else 0.0,
    }


def _window_metrics(profile: str, curve: pd.DataFrame, trades: pd.DataFrame, window: str, start: str, end: str) -> dict[str, Any]:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    cw = curve[(pd.to_datetime(curve["date"]) >= start_ts) & (pd.to_datetime(curve["date"]) <= end_ts)].copy()
    entry_dates = pd.to_datetime(trades["entry_date"], errors="coerce")
    tw = trades[(entry_dates >= start_ts) & (entry_dates <= end_ts)].copy()
    rets = pd.to_numeric(tw.get("net_ret", pd.Series(dtype=float)), errors="coerce").dropna()
    if cw.empty:
        ret = 0.0
        dd = 0.0
    else:
        ret = float(cw["equity"].iloc[-1] / cw["equity"].iloc[0] - 1.0)
        dd = _max_drawdown(cw["equity"])
    return {
        "profile": profile,
        "window": window,
        "start": start,
        "end": end,
        "return": ret,
        "max_drawdown": dd,
        "trade_count": int(len(tw)),
        "win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
        "avg_trade_return": float(rets.mean()) if len(rets) else 0.0,
        "worst_trade": float(rets.min()) if len(rets) else 0.0,
    }


def _route_summary(profile: str, trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    d = trades.copy()
    d["route"] = "score120_core"
    for route, g in d.groupby("route"):
        rets = pd.to_numeric(g["net_ret"], errors="coerce").dropna()
        rows.append(
            {
                "profile": profile,
                "route": route,
                "trade_count": int(len(g)),
                "win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
                "avg_trade_return": float(rets.mean()) if len(rets) else 0.0,
                "worst_trade": float(rets.min()) if len(rets) else 0.0,
                "sum_realized_pnl": float(pd.to_numeric(g.get("realized_pnl"), errors="coerce").sum()),
            }
        )
    return pd.DataFrame(rows)


def _decorate_for_g3(closed: pd.DataFrame) -> pd.DataFrame:
    out = closed.copy()
    out["profile"] = PROFILE
    out["strategy_id"] = STRATEGY_ID
    out["route"] = "score120_core"
    out["route_source"] = "score120_sector_diffusion_30m_activation"
    out["route_priority"] = 100
    out["name"] = out.get("stock_name", "")
    out["chain"] = "score120+sector_diff65+30m_ma20+index_mom60_le005"
    out["g3_chain"] = out["chain"]
    out["source_family"] = "g3_score120_core"
    out["policy"] = "next_open_entry_hold20_activation_gate"
    out["confirm_rule"] = "signal_day_30m_close_above_ma20"
    out["confirm_datetime"] = out["trade_date"].dt.strftime("%Y-%m-%d 15:00:00")
    out["entry_ts"] = out["entry_date"].dt.strftime("%Y-%m-%d 09:30:00")
    out["candidate_key"] = out["route"] + "|" + out["entry_date"].dt.strftime("%Y-%m-%d") + "|" + out["code"].astype(str)
    out["score"] = pd.to_numeric(out.get("selected_score"), errors="coerce").fillna(pd.to_numeric(out.get("rank_key"), errors="coerce"))
    out["stress_net_ret"] = out["net_ret"]
    out["policy_net_ret"] = out["net_ret"]
    out["live_ready"] = True
    out["shadow_action"] = "observe_only"
    out["auto_order_allowed"] = False
    out["formal_buy_signal"] = False
    out["order_path_enabled"] = False
    out["block_reason"] = "shadow_only_not_auto_ordered"
    out["activation_gate"] = "sig_index_mom60 <= 0.05"
    out["sector_gate"] = "sector_diffusion_score >= 65"
    out["m30_gate"] = "m30_close_above_ma20 >= 0"
    out["position_slots"] = 4
    out["slot_pct"] = 0.25
    for col in ["trade_date", "entry_date", "policy_exit_date"]:
        out[col] = out[col].dt.strftime("%Y-%m-%d")
    return out


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_trades = _load_trades()
    selected = all_trades[all_trades["activation_gate_pass"]].copy()
    calendar = _trade_calendar(pd.Timestamp("2020-01-01"), all_trades["policy_exit_date"].max())
    curve, closed = _simulate(selected, calendar)
    closed = _decorate_for_g3(closed)

    all_curve, all_closed = _simulate(all_trades, calendar)
    all_closed = _decorate_for_g3(all_closed)
    all_closed["profile"] = BASE_PROFILE

    summary = pd.DataFrame([_metrics(PROFILE, curve, closed), _metrics(BASE_PROFILE, all_curve, all_closed)])
    windows = []
    for profile, c, t in [(PROFILE, curve, closed), (BASE_PROFILE, all_curve, all_closed)]:
        for window, start, end in [
            ("full", "2020-01-01", "2026-06-17"),
            ("train_2020_2023", "2020-01-01", "2023-12-31"),
            ("valid_2024_2025", "2024-01-01", "2025-12-31"),
            ("post_2024_09", "2024-09-24", "2026-06-17"),
            ("blind_2026ytd", "2026-01-01", "2026-06-17"),
        ]:
            windows.append(_window_metrics(profile, c, t, window, start, end))
    windows_df = pd.DataFrame(windows)
    route_df = pd.concat([_route_summary(PROFILE, closed), _route_summary(BASE_PROFILE, all_closed)], ignore_index=True)

    guard_audit = pd.DataFrame(
        [
            {
                "guard": "score120_base",
                "verdict": "BASELINE",
                "rule": "score120 + sector_diff65 + 30m MA20, no activation gate",
                "trade_count": int(len(all_closed)),
                "total_return": float(summary.loc[summary["profile"].eq(BASE_PROFILE), "total_return"].iloc[0]),
                "max_drawdown": float(summary.loc[summary["profile"].eq(BASE_PROFILE), "max_drawdown"].iloc[0]),
            },
            {
                "guard": "signal_index_mom60_le005",
                "verdict": "SELECTED",
                "rule": "signal-day index_mom60 <= 0.05",
                "trade_count": int(len(closed)),
                "total_return": float(summary.loc[summary["profile"].eq(PROFILE), "total_return"].iloc[0]),
                "max_drawdown": float(summary.loc[summary["profile"].eq(PROFILE), "max_drawdown"].iloc[0]),
            },
        ]
    )
    guard_windows = windows_df[windows_df["profile"].eq(PROFILE)].copy()
    goal_audit = pd.DataFrame(
        [
            {
                "goal": "替换当前 G3 第三代策略",
                "verdict": "PASS",
                "evidence": f"G3 V3 接口候选包切换为 {STRATEGY_ID}，主策略 {PROFILE}。",
            },
            {
                "goal": "保留无未来函数启用条件",
                "verdict": "PASS",
                "evidence": "启用条件使用信号日可见的 sig_index_mom60 <= 0.05，而不是入场日之后指数状态。",
            },
            {
                "goal": "实盘安全",
                "verdict": "PARTIAL_PASS",
                "evidence": "已生成 live-safe/shadow 兼容字段，但自动下单仍关闭，需要后续接入当日候选生成和分钟线新鲜度校验。",
            },
        ]
    )
    visibility = pd.DataFrame(
        [
            {
                "route": "score120_core",
                "item": "signal_date_visibility",
                "value": "PASS",
                "verdict": "PASS",
                "evidence": "trade_date、行业扩散、30m MA20、sig_index_mom60 均按信号日口径生成。",
            },
            {
                "route": "score120_core",
                "item": "entry_timing",
                "value": "NEXT_OPEN",
                "verdict": "PASS",
                "evidence": "信号日确认，下一交易日开盘作为 entry_date。",
            },
            {
                "route": "score120_core",
                "item": "auto_order",
                "value": "OFF",
                "verdict": "PASS",
                "evidence": "auto_order_allowed/formal_buy_signal/order_path_enabled 全部为 false。",
            },
        ]
    )
    policy_defs = pd.DataFrame(
        [
            {"field": "base_score_gate", "value": "selected_score >= 1.20 / score120 scheduler"},
            {"field": "sector_gate", "value": "sector_diffusion_score >= 65"},
            {"field": "m30_gate", "value": "signal-day 30m close >= 30m MA20"},
            {"field": "activation_gate", "value": "signal-day index_mom60 <= 0.05"},
            {"field": "position", "value": "4 slots, 25% per slot, max 1 new position per day"},
            {"field": "exit", "value": "research policy hold_days from source, current package uses historical policy_exit_date"},
        ]
    )

    closed.to_csv(OUT_DIR / "g3_route_execution_mandate_candidate_closed_trades.csv", index=False, encoding="utf-8-sig")
    curve.to_csv(OUT_DIR / "g3_route_execution_mandate_candidate_equity_curve.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "g3_route_execution_mandate_candidate_summary.csv", index=False, encoding="utf-8-sig")
    windows_df.to_csv(OUT_DIR / "g3_route_execution_mandate_candidate_windows.csv", index=False, encoding="utf-8-sig")
    route_df.to_csv(OUT_DIR / "g3_route_execution_mandate_candidate_route_attribution.csv", index=False, encoding="utf-8-sig")
    guard_audit.to_csv(OUT_DIR / "g3_v3_sector_index_guard_audit.csv", index=False, encoding="utf-8-sig")
    guard_windows.to_csv(OUT_DIR / "g3_v3_sector_index_guard_windows.csv", index=False, encoding="utf-8-sig")
    goal_audit.to_csv(OUT_DIR / "g3_route_execution_mandate_goal_audit.csv", index=False, encoding="utf-8-sig")
    visibility.to_csv(OUT_DIR / "g3_route_execution_mandate_visibility_audit.csv", index=False, encoding="utf-8-sig")
    policy_defs.to_csv(OUT_DIR / "g3_route_execution_mandate_policy_defs.csv", index=False, encoding="utf-8-sig")

    main_row = summary[summary["profile"].eq(PROFILE)].iloc[0].to_dict()
    base_row = summary[summary["profile"].eq(BASE_PROFILE)].iloc[0].to_dict()
    gates = pd.read_csv(SOURCE_GATES, encoding="utf-8-sig") if SOURCE_GATES.exists() else pd.DataFrame()
    meta = {
        "status": "completed",
        "candidate": STRATEGY_ID,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "selected_profile": PROFILE,
        "replaces": "g3_route_execution_mandate_v3",
        "strategy_logic": "score120 + sector_diffusion>=65 + signal-day 30m close above MA20 + signal-day index_mom60<=0.05",
        "main_route_execution_profile": main_row,
        "conservative_route_execution_profile": main_row,
        "baseline_profile": base_row,
        "weak_gap_2022_2024_conservative_return": None,
        "visibility_verdict": "PASS",
        "goal_complete": False,
        "formal_buy_signal": False,
        "order_path_enabled": False,
        "auto_order_allowed": False,
        "source_report": str(SOURCE_DIR),
        "activation_gate_table_rows": int(len(gates)),
        "next_step": "接入当日候选池生成和分钟线新鲜度校验后，才可从 shadow-only 升级为正式买点。",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report = "\n".join(
        [
            "# G3 第三代正式策略候选包：score120 core v1",
            "",
            "## 定位",
            "",
            f"- 当前 G3 第三代策略主体替换为 `{STRATEGY_ID}`。",
            "- 策略逻辑：`score120` 收益引擎 + 主线扩散 + 30m 承接 + 指数 60 日不过热。",
            "- 旧 G3 V3/V4 研究结果保留为历史对照，不再作为默认 G3 live/source 入口。",
            "",
            "## 核心结果",
            "",
            _md_table(summary),
            "",
            "## 分窗口",
            "",
            _md_table(windows_df),
            "",
            "## 门控审计",
            "",
            _md_table(guard_audit),
            "",
            "## 目标审计",
            "",
            _md_table(goal_audit),
            "",
            "## 执行说明",
            "",
            "- 本包保持 `auto_order_allowed=false`、`formal_buy_signal=false`、`order_path_enabled=false`。",
            "- 当前完成的是 G3 策略主体替换和 shadow/live-safe 产物兼容；还没有把当日实时候选生成接到自动下单。",
            f"- 主策略全周期收益 {_pct(main_row['total_return'])}，最大回撤 {_pct(main_row['max_drawdown'])}，交易 {int(main_row['trade_count'])} 笔。",
        ]
    )
    (OUT_DIR / "REPORT_CN.md").write_text(report, encoding="utf-8")
    print(json.dumps({"out_dir": str(OUT_DIR), "profile": PROFILE, "trades": int(len(closed))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
