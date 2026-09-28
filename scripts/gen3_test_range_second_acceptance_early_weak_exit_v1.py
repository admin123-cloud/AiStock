from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = PROJECT_ROOT / "reports" / "gen3_range_second_acceptance_structure_veto_v1"
VARIANT = "deep_and_reclaim_any_second_accept__not_floor_not_strong_unless_extreme_volume"
PROFILE = "cost30"
RUN_DIR = SOURCE_DIR / f"{VARIANT}__{PROFILE}"
OUT_DIR = PROJECT_ROOT / "reports" / "gen3_range_second_acceptance_early_weak_exit_v1"
INITIAL_CAPITAL = 150000.0
SLOT_STAKE = 30000.0
COST = 0.003


def pct(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "--"
    return f"{value * 100:+.2f}%"


def numeric(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for col in cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def max_drawdown_from_trade_curve(rets: pd.Series) -> tuple[float, float]:
    equity = INITIAL_CAPITAL
    peak = equity
    max_dd = 0.0
    for ret in rets.fillna(0.0):
        equity += SLOT_STAKE * ret
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1.0)
    return equity / INITIAL_CAPITAL - 1.0, max_dd


def policy_return(row: pd.Series, policy: str) -> tuple[float, str]:
    base = row["fwd_ret_confirm_to_close_5d"] - COST
    d1 = row["fwd_ret_confirm_to_close_1d"]
    d2 = row["fwd_ret_confirm_to_close_2d"]
    d3 = row["fwd_ret_confirm_to_close_3d"]

    if policy == "base_hold5":
        return base, "持有到D5"
    if policy == "d1_nonpositive_exit_d1":
        if pd.notna(d1) and d1 <= 0:
            return d1 - COST, "D1不强，D1收盘退出"
        return base, "持有到D5"
    if policy == "d2_nonpositive_exit_d2":
        if pd.notna(d2) and d2 <= 0:
            return d2 - COST, "D2仍不强，D2收盘退出"
        return base, "持有到D5"
    if policy == "d1_d2_both_weak_exit_d2":
        if pd.notna(d1) and pd.notna(d2) and d1 <= 0 and d2 <= 0:
            return d2 - COST, "D1/D2都弱，D2收盘退出"
        return base, "持有到D5"
    if policy == "d2_fade_exit_d2":
        if pd.notna(d1) and pd.notna(d2) and d2 < d1 and d2 <= 0.01:
            return d2 - COST, "D2低于D1且修复不足，D2收盘退出"
        return base, "持有到D5"
    if policy == "d3_nonpositive_exit_d3":
        if pd.notna(d3) and d3 <= 0:
            return d3 - COST, "D3仍不强，D3收盘退出"
        return base, "持有到D5"
    raise ValueError(f"unknown policy: {policy}")


def summarize(df: pd.DataFrame, ret_col: str) -> dict[str, float | int | None]:
    rets = df[ret_col].dropna()
    total_return, max_drawdown = max_drawdown_from_trade_curve(df[ret_col])
    return {
        "trade_count": int(len(df)),
        "total_return": total_return,
        "max_drawdown": max_drawdown,
        "win_rate": float((rets > 0).mean()) if len(rets) else None,
        "avg_trade_return": float(rets.mean()) if len(rets) else None,
        "worst_trade": float(rets.min()) if len(rets) else None,
        "changed_count": int((df["exit_reason"] != "持有到D5").sum()),
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = pd.read_csv(RUN_DIR / "closed_trades.csv", low_memory=False)
    trades = numeric(
        trades,
        [
            "policy_net_ret",
            "realized_pnl",
            "fwd_ret_confirm_to_close_1d",
            "fwd_ret_confirm_to_close_2d",
            "fwd_ret_confirm_to_close_3d",
            "fwd_ret_confirm_to_close_5d",
            "range_pos60",
            "close_position",
            "amount_ratio3",
            "gap_open",
            "index_mom20",
        ],
    )
    trades["entry_dt"] = pd.to_datetime(trades["entry_date"], errors="coerce")
    trades = trades.sort_values(["entry_dt", "code"]).reset_index(drop=True)

    policies = [
        "base_hold5",
        "d1_nonpositive_exit_d1",
        "d2_nonpositive_exit_d2",
        "d1_d2_both_weak_exit_d2",
        "d2_fade_exit_d2",
        "d3_nonpositive_exit_d3",
    ]
    summaries = []
    detail_frames = []
    for policy in policies:
        d = trades.copy()
        returns = d.apply(lambda row: policy_return(row, policy), axis=1)
        d["policy"] = policy
        d["alt_net_ret"] = [item[0] for item in returns]
        d["exit_reason"] = [item[1] for item in returns]
        d["ret_delta_vs_base"] = d["alt_net_ret"] - d["policy_net_ret"]
        summary = summarize(d, "alt_net_ret")
        summary["policy"] = policy
        summaries.append(summary)
        detail_frames.append(d)

    summary_df = pd.DataFrame(summaries)
    details = pd.concat(detail_frames, ignore_index=True)
    keep_cols = [
        "policy",
        "entry_date",
        "policy_exit_date",
        "code",
        "name",
        "policy_net_ret",
        "alt_net_ret",
        "ret_delta_vs_base",
        "exit_reason",
        "emotion_signal",
        "source_desc",
        "fwd_ret_confirm_to_close_1d",
        "fwd_ret_confirm_to_close_2d",
        "fwd_ret_confirm_to_close_3d",
        "fwd_ret_confirm_to_close_5d",
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "gap_open",
        "index_mom20",
    ]
    keep_cols = [col for col in keep_cols if col in details.columns]

    summary_df.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    details[keep_cols].to_csv(OUT_DIR / "policy_trade_details.csv", index=False, encoding="utf-8-sig")

    best = summary_df.sort_values(["total_return", "max_drawdown"], ascending=[False, False]).iloc[0].to_dict()
    base = summary_df[summary_df["policy"] == "base_hold5"].iloc[0].to_dict()
    result = {
        "variant": VARIANT,
        "variant_cn": "横盘/冰点二次承接结构否决版",
        "profile": PROFILE,
        "profile_cn": "30bps交易成本口径",
        "signal_backtest_start": "2020-01-01",
        "signal_backtest_end": "2026-05-29",
        "trade_count": int(len(trades)),
        "base": base,
        "best_by_return": best,
    }
    (OUT_DIR / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# G3 横盘/冰点二次承接 D1/D2 弱确认退出审计",
        "",
        "## 口径说明",
        f"- 英文名：`{VARIANT}`。",
        "- 中文含义：横盘/冰点环境下，箱体底部且日线修复，D0 后半日或 D1 任一出现 30m 放量承接，并加入结构否决。",
        "- 本次只改退出，不改入场，不使用 score/rank 再过滤。",
        "- 信号回测窗口：2020-01-01 至 2026-05-29；交易数 17 笔；成本口径 cost30，即 30bps。",
        "",
        "## 策略对照",
    ]
    for _, row in summary_df.iterrows():
        lines.append(
            f"- `{row['policy']}`：{int(row['trade_count'])} 笔，收益 {pct(row['total_return'])}，回撤 {pct(row['max_drawdown'])}，胜率 {pct(row['win_rate'])}，均笔 {pct(row['avg_trade_return'])}，最差 {pct(row['worst_trade'])}，触发提前退出 {int(row['changed_count'])} 笔。"
        )
    lines.extend(
        [
            "",
            "## 初步结论",
            f"- 基准 `base_hold5` 是固定持有到 D5：收益 {pct(base['total_return'])}，回撤 {pct(base['max_drawdown'])}。",
            f"- 单看收益最高的是 `{best['policy']}`：收益 {pct(best['total_return'])}，回撤 {pct(best['max_drawdown'])}，但样本只有 17 笔，不能据此直接升正式规则。",
            "- 如果 D1/D2 弱确认退出不能同时提升收益和压低最差单笔，就说明失败不是简单持有期问题，而要继续回到入场质量和真实承接强度。",
        ]
    )
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
