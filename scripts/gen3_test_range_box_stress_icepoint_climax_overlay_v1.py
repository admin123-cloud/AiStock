from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar  # noqa: E402
from scripts.gen3_test_range_30m_volume_acceptance_v1 import (  # noqa: E402
    INITIAL_CAPITAL,
    PROFILES,
    SLOT_PCT,
    SLOTS,
    max_drawdown,
    standardize,
    window_metrics,
)


SOURCE = ROOT / "reports" / "gen3_range_30m_volume_acceptance_v1" / "box_stress_accept_h5_signals.csv"
EVENTS = ROOT / "reports" / "gen3_v4_icepoint_climax_overlay_v1" / "emotion_events_d1_visible.csv"
OUT_DIR = ROOT / "reports" / "gen3_range_box_stress_icepoint_climax_overlay_v1"

VARIANTS = [
    {"variant": "base", "desc": "原始横盘箱体底部放量承接", "mode": "base"},
    {"variant": "ice_only", "desc": "只在冰点后买入", "mode": "ice_only"},
    {"variant": "neutral_only", "desc": "只在普通情绪日买入", "mode": "neutral_only"},
    {"variant": "climax_pause", "desc": "高潮后暂停买入", "mode": "climax_pause"},
    {"variant": "climax_half", "desc": "高潮后半仓买入", "mode": "climax_half"},
    {"variant": "ice125", "desc": "冰点后加仓到1.25倍", "mode": "ice125"},
    {"variant": "ice150", "desc": "冰点后加仓到1.50倍", "mode": "ice150"},
    {"variant": "ice125_climax_half", "desc": "冰点1.25倍，高潮半仓", "mode": "ice125_climax_half"},
    {"variant": "ice125_climax_pause", "desc": "冰点1.25倍，高潮暂停", "mode": "ice125_climax_pause"},
]


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item: dict[str, Any] = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = pct(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def load_base() -> pd.DataFrame:
    signals = pd.read_csv(SOURCE)
    signals["entry_date"] = pd.to_datetime(signals["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    signals["hold_days"] = 5
    events = pd.read_csv(EVENTS)
    events["entry_date"] = pd.to_datetime(events["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    keep = [
        "signal_date",
        "entry_date",
        "limit_up_count",
        "limit_down_count",
        "ice_score",
        "climax_score",
        "emotion_score",
        "emotion_signal",
    ]
    d = signals.merge(events[[c for c in keep if c in events.columns]], on="entry_date", how="left")
    d["emotion_signal"] = d["emotion_signal"].fillna("neutral")
    for col in ["ice_score", "climax_score", "emotion_score", "limit_up_count", "limit_down_count"]:
        d[col] = pd.to_numeric(d[col], errors="coerce").fillna(0.0)
    d["rank_key"] = pd.to_numeric(d["rank_key"], errors="coerce").fillna(0.0)
    return d


def apply_overlay(candidates: pd.DataFrame, spec: dict[str, Any]) -> pd.DataFrame:
    d = candidates.copy()
    mode = str(spec["mode"])
    d["overlay_variant"] = spec["variant"]
    d["overlay_desc"] = spec["desc"]
    d["position_scale"] = 1.0
    d["overlay_note"] = "base"
    ice = d["emotion_signal"].astype(str).eq("icepoint")
    neutral = d["emotion_signal"].astype(str).eq("neutral")
    climax = d["emotion_signal"].astype(str).eq("climax")
    if mode == "base":
        return d
    if mode == "ice_only":
        return d[ice].copy()
    if mode == "neutral_only":
        return d[neutral].copy()
    if mode == "climax_pause":
        out = d[~climax].copy()
        out["overlay_note"] = "climax_pause"
        return out
    if mode == "climax_half":
        d.loc[climax, "position_scale"] = 0.5
        d.loc[climax, "overlay_note"] = "climax_half"
        return d
    if mode == "ice125":
        d.loc[ice, "position_scale"] = 1.25
        d.loc[ice, "overlay_note"] = "ice125"
        return d
    if mode == "ice150":
        d.loc[ice, "position_scale"] = 1.50
        d.loc[ice, "overlay_note"] = "ice150"
        return d
    if mode == "ice125_climax_half":
        d.loc[ice, "position_scale"] = 1.25
        d.loc[climax, "position_scale"] = 0.5
        d.loc[ice, "overlay_note"] = "ice125"
        d.loc[climax, "overlay_note"] = "climax_half"
        return d
    if mode == "ice125_climax_pause":
        d.loc[ice, "position_scale"] = 1.25
        d.loc[ice, "overlay_note"] = "ice125"
        return d[~climax].copy()
    raise ValueError(mode)


def simulate_scaled(candidates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if candidates.empty:
        return pd.DataFrame(), pd.DataFrame()
    d = candidates.sort_values(["entry_date_ts", "rank_key"], ascending=[True, False]).copy()
    cal = _trade_calendar(d["entry_date_ts"].min(), d["policy_exit_date"].max())
    by_day = {day: g.copy() for day, g in d.groupby("entry_date_ts")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for day in cal:
        day = pd.Timestamp(day).normalize()
        still = []
        realized = 0.0
        for pos in open_pos:
            if pd.Timestamp(pos["policy_exit_date"]).normalize() <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["policy_net_ret"]))
                cash += exit_value
                pnl = exit_value - float(pos["stake"])
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = pnl
                closed.append(out)
                realized += pnl
            else:
                still.append(pos)
        open_pos = still
        opened = 0
        todays = by_day.get(day)
        if todays is not None:
            for row in todays.itertuples(index=False):
                if opened >= 1 or len(open_pos) >= SLOTS:
                    continue
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                scale = float(getattr(row, "position_scale", 1.0) or 1.0)
                stake = equity_before * SLOT_PCT * scale
                if stake <= 0 or cash < stake:
                    continue
                pos = row._asdict()
                pos["stake"] = stake
                cash -= stake
                open_pos.append(pos)
                opened += 1
        equity = cash + sum(float(p["stake"]) for p in open_pos)
        rows.append(
            {
                "date": day,
                "cash": cash,
                "reserved_principal": sum(float(p["stake"]) for p in open_pos),
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "realized_pnl": realized,
            }
        )
    curve = pd.DataFrame(rows)
    closed_df = pd.DataFrame(closed)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, closed_df


def summarize(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, profile: str, desc: str) -> dict[str, Any]:
    net = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    return {
        "variant": variant,
        "desc": desc,
        "profile": profile,
        "trade_count": int(len(closed)),
        "ice_trades": int(closed.get("emotion_signal", pd.Series(dtype=str)).astype(str).eq("icepoint").sum()) if not closed.empty else 0,
        "climax_trades": int(closed.get("emotion_signal", pd.Series(dtype=str)).astype(str).eq("climax").sum()) if not closed.empty else 0,
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0) if not curve.empty else 0.0,
        "max_drawdown": max_drawdown(curve["equity"]) if not curve.empty else 0.0,
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_trade_return": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "avg_position_scale": float(pd.to_numeric(closed.get("position_scale", pd.Series(dtype=float)), errors="coerce").mean()) if not closed.empty else 0.0,
    }


def emotion_summary(closed: pd.DataFrame, variant: str, profile: str) -> pd.DataFrame:
    rows = []
    if closed.empty:
        return pd.DataFrame()
    for signal, g in closed.groupby("emotion_signal", dropna=False):
        net = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        rows.append(
            {
                "variant": variant,
                "profile": profile,
                "emotion_signal": signal,
                "trade_count": int(len(g)),
                "avg_ret": float(net.mean()),
                "win_rate": float((net > 0).mean()),
                "pnl": float(pd.to_numeric(g["realized_pnl"], errors="coerce").sum()),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base_raw = load_base()
    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    emotion_rows: list[pd.DataFrame] = []
    base_raw.to_csv(OUT_DIR / "box_stress_signals_with_emotion.csv", index=False, encoding="utf-8-sig")

    for spec in VARIANTS:
        overlaid = apply_overlay(base_raw, spec)
        overlaid.to_csv(OUT_DIR / f"{spec['variant']}_signals.csv", index=False, encoding="utf-8-sig")
        for profile in PROFILES:
            candidates = standardize(overlaid, profile)
            curve, closed = simulate_scaled(candidates)
            run_dir = OUT_DIR / f"{spec['variant']}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(summarize(curve, closed, str(spec["variant"]), str(profile["profile"]), str(spec["desc"])))
            window_rows.extend(window_metrics(curve, closed, str(spec["variant"]), str(profile["profile"])))
            es = emotion_summary(closed, str(spec["variant"]), str(profile["profile"]))
            if not es.empty:
                emotion_rows.append(es)

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    emotions = pd.concat(emotion_rows, ignore_index=True) if emotion_rows else pd.DataFrame()
    counts = base_raw.groupby("emotion_signal").size().reset_index(name="signal_count")
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    emotions.to_csv(OUT_DIR / "emotion_summary.csv", index=False, encoding="utf-8-sig")
    counts.to_csv(OUT_DIR / "emotion_signal_counts.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "return", "avg_ret"}
    focus = summary[summary["profile"].isin(["cost30", "cost100", "shock2_cost30"])].copy()
    lines = [
        "# G3 box_stress_accept_h5 冰点/高潮择时 overlay v1",
        "",
        "## 策略名解释",
        "",
        "- `box_stress_accept_h5`：中文是“横盘箱体底部放量承接，持有5日”。先找横盘箱体底部附近的日线候选，再用30m真实放量承接确认买入。",
        "- `icepoint`：中文是“冰点”。用前一交易日全市场跌停家数的滚动高分位识别极端恐慌，映射到下一交易日买入，属于 D-1 可见口径。",
        "- `climax`：中文是“高潮”。用前一交易日全市场涨停家数的滚动高分位识别情绪高潮，映射到下一交易日减仓或暂停买入。",
        "- `ice_only`：只在冰点后买入。",
        "- `climax_pause`：高潮后不买。",
        "- `climax_half`：高潮后只用半仓买。",
        "- `ice125/ice150`：冰点后把开仓资金放大到1.25倍/1.50倍。",
        "",
        "## 情绪标签覆盖",
        "",
        md_table(counts),
        "",
        "## slot复算结果",
        "",
        md_table(focus, pct_cols=pct_cols),
        "",
        "## 分窗口结果",
        "",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 情绪分组成交",
        "",
        md_table(emotions, pct_cols={"avg_ret", "win_rate"}),
        "",
        "## 判断口径",
        "",
        "- 如果 `ice_only` 样本太少，即使收益好也只能作为观察标签，不能单独成为主线。",
        "- 如果 `climax_pause` 或 `climax_half` 明显改善 2024-2025 和压力口径，说明高潮适合做横盘源的减仓/暂停规则。",
        "- 如果冰点加仓只提高30bps但恶化压力口径，说明它仍然有过拟合或执行冲击风险。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
