from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar  # noqa: E402
from scripts.gen3_test_v4_strong_position_scale_v1 import md_table, simulate_scaled, summarize  # noqa: E402


G3_BASE = ROOT / "reports" / "gen3_v4_strong_entry_warning_early_exit_v1" / "base_093_candidates.csv"
BIGBULL_SOURCE = ROOT / "reports" / "gen3_v4_strong_missing_g2_bigbull_v1" / "g2_source_with_g3_overlap.csv"
OUT_DIR = ROOT / "reports" / "gen3_v4_add_g2_bigbull_route_v1"

COST_BPS = 30.0


def pct(v: Any) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def load_g3_base() -> pd.DataFrame:
    d = pd.read_csv(G3_BASE, low_memory=False, encoding="utf-8-sig")
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in ["entry_price", "policy_net_ret", "score", "route_priority", "position_scale"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["position_scale"] = d.get("position_scale", 1.0).fillna(1.0)
    return d.dropna(subset=["entry_date", "policy_exit_date", "code", "entry_price", "policy_net_ret"]).copy()


def exit_date_map(start: pd.Timestamp, end: pd.Timestamp, hold_days: int = 5) -> dict[pd.Timestamp, pd.Timestamp]:
    cal = _trade_calendar(start, end)
    out: dict[pd.Timestamp, pd.Timestamp] = {}
    for i, day in enumerate(cal):
        idx = i + hold_days - 1
        if idx < len(cal):
            out[day] = cal[idx]
    return out


def load_bigbull_candidates() -> pd.DataFrame:
    d = pd.read_csv(BIGBULL_SOURCE, low_memory=False, encoding="utf-8-sig")
    d = d[d["source_family"].astype(str).eq("big_bull")].copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in ["entry_price", "calc_ret_5d", "v4_score", "v4_rank", "rt_return_from_d1_close", "l3_rt_strong3_ratio"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["entry_date", "code", "entry_price", "calc_ret_5d"]).copy()
    exits = exit_date_map(d["entry_date"].min(), d["entry_date"].max() + pd.Timedelta(days=45), 5)
    out = pd.DataFrame(
        {
            "entry_date": d["entry_date"],
            "policy_exit_date": d["entry_date"].map(exits),
            "code": d["code"],
            "name": d["name"],
            "route": "strong_bigbull_rebreak",
            "route_source": "g2_big_bull_rebreak_2_5d_intraday_sector",
            "route_priority": 2,
            "score": d["v4_score"].fillna(0.0),
            "entry_price": d["entry_price"],
            "policy_net_ret": d["calc_ret_5d"] - COST_BPS / 10000.0,
            "position_scale": 1.0,
            "scale_note": "full",
            "g2_v4_rank": d.get("v4_rank"),
            "g2_rt_return_from_d1_close": d.get("rt_return_from_d1_close"),
            "g2_l3_rt_strong3_ratio": d.get("l3_rt_strong3_ratio"),
        }
    )
    return out.dropna(subset=["policy_exit_date", "policy_net_ret"]).copy()


def scale_bigbull(big: pd.DataFrame, scale: float) -> pd.DataFrame:
    d = big.copy()
    d["position_scale"] = float(scale)
    d["scale_note"] = f"bigbull_scale_{scale:.2f}"
    return d


def apply_profile(candidates: pd.DataFrame, name: str) -> pd.DataFrame:
    d = candidates.copy()
    d["stress_profile"] = name
    if name == "30bps":
        return d
    if name == "30bps_haircut2":
        d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce") - 0.02
        return d
    raise ValueError(name)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    g3 = load_g3_base()
    big = load_bigbull_candidates()
    variants = {
        "g3_base": g3,
        "g3_plus_bigbull_full": pd.concat([g3, big], ignore_index=True, sort=False),
        "g3_plus_bigbull_half": pd.concat([g3, scale_bigbull(big, 0.50)], ignore_index=True, sort=False),
        "g3_plus_bigbull_quarter": pd.concat([g3, scale_bigbull(big, 0.25)], ignore_index=True, sort=False),
    }
    summaries: list[dict[str, Any]] = []
    for variant, candidates in variants.items():
        candidates.to_csv(OUT_DIR / f"{variant}_candidates.csv", index=False, encoding="utf-8-sig")
        for profile in ["30bps", "30bps_haircut2"]:
            prof = apply_profile(candidates, profile)
            curve, closed = simulate_scaled(prof, COST_BPS)
            run_dir = OUT_DIR / f"{variant}__{profile}"
            run_dir.mkdir(parents=True, exist_ok=True)
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summaries.append(summarize(curve, closed, variant, profile))
    summary = pd.DataFrame(summaries)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    pct_cols = {
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "worst_open_mtm_ret",
        "strong_main_avg_ret",
        "strong_bigbull_rebreak_avg_ret",
    }
    lines = [
        "# G3 V4 增加 G2 big_bull 独立强势 route 轻量复算 v1",
        "",
        "## 边界",
        "",
        "- 当前 G3 base 使用 `base_093_candidates`，不改已有 volume5 strong_main、range_gap、down_panic。",
        "- 新增 route：`strong_bigbull_rebreak`，来源为 G2 source 中的 `big_bull_rebreak_2_5d + intraday_strength + sector_strong`。",
        "- 退出口径先用 5 个交易日 close 前向收益近似，并扣 30bps；不是完整 G2 30m 止盈止损，也不是正式 G3 策略。",
        "- 本轮目标只是判断 big_bull 作为独立候选源是否值得继续做完整 slot/执行压力测试。",
        "",
        "## 汇总",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 如果 `g3_plus_bigbull` 在正常口径和 haircut2 下都改善，下一步才进入完整 30m 执行复算。",
        "- 如果收益改善但回撤/最差持仓恶化，big_bull 应只作为强势市场小权重补位，不应直接等权加入。",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"done: {OUT_DIR}")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
