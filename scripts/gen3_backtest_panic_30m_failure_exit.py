from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_audit_panic_30m_failure_visibility import _load_30m, _load_paths


WINDOWS = {
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-12-31"),
    "full": ("2020-01-01", "2026-12-31"),
}

POLICIES = {
    "fixed_hold": {"threshold": None, "exit_fraction": 0.0},
    "m30_close5_full_nextopen": {"threshold": -0.05, "exit_fraction": 1.0},
    "m30_close8_full_nextopen": {"threshold": -0.08, "exit_fraction": 1.0},
    "m30_close5_half_nextopen": {"threshold": -0.05, "exit_fraction": 0.5},
    "m30_close8_half_nextopen": {"threshold": -0.08, "exit_fraction": 0.5},
}


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    value = pd.to_numeric(v, errors="coerce")
    if pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _prepare_bar_map(paths: pd.DataFrame) -> dict[str, pd.DataFrame]:
    bars = _load_30m(paths)
    if bars.empty:
        return {}
    return {code: g.sort_values("datetime").reset_index(drop=True).copy() for code, g in bars.groupby("code")}


def _exit_for_threshold(row: pd.Series, bar_map: dict[str, pd.DataFrame], threshold: float, cost_bps: float) -> dict:
    code = str(row["code"])
    entry_price = float(row["entry_price_adjusted"])
    confirm_dt = pd.Timestamp(row["confirm_datetime"])
    exit_dt = pd.Timestamp(row["exit_date"]) + pd.Timedelta(hours=15)
    bars = bar_map.get(code, pd.DataFrame())
    if bars.empty or entry_price <= 0:
        return {"triggered": False}

    path = bars[(bars["datetime"] > confirm_dt) & (bars["datetime"] <= exit_dt)].copy()
    if path.empty:
        return {"triggered": False}
    path["close_ret"] = path["adj_close"] / entry_price - 1.0
    path["next_open"] = path["adj_open"].shift(-1)
    path["next_datetime"] = path["datetime"].shift(-1)
    hit = path[path["close_ret"] <= threshold]
    if hit.empty:
        return {"triggered": False}

    first = hit.iloc[0]
    next_open = first.get("next_open")
    next_dt = first.get("next_datetime")
    if pd.isna(next_open) or pd.isna(next_dt):
        return {
            "triggered": True,
            "executable": False,
            "trigger_datetime": first["datetime"],
            "trigger_close_ret": float(first["close_ret"]),
        }

    gross_ret = float(next_open) / entry_price - 1.0
    return {
        "triggered": True,
        "executable": True,
        "trigger_datetime": first["datetime"],
        "trigger_close_ret": float(first["close_ret"]),
        "exit_datetime": next_dt,
        "exit_price": float(next_open),
        "exit_ret_net": gross_ret - cost_bps / 10000.0,
    }


def _apply_policy(row: pd.Series, bar_map: dict[str, pd.DataFrame], policy: str, cost_bps: float) -> dict:
    cfg = POLICIES[policy]
    out = row.to_dict()
    out["policy"] = policy
    out["baseline_net_ret"] = float(row["net_ret"])
    out["policy_net_ret"] = float(row["net_ret"])
    out["triggered"] = False
    out["executable"] = False
    out["exit_source"] = "fixed_hold"
    out["early_exit_fraction"] = 0.0
    threshold = cfg["threshold"]
    if threshold is None:
        return out

    decision = _exit_for_threshold(row, bar_map, float(threshold), cost_bps)
    out.update(decision)
    if not decision.get("triggered") or not decision.get("executable"):
        return out

    early_ret = float(decision["exit_ret_net"])
    fraction = float(cfg["exit_fraction"])
    out["early_exit_fraction"] = fraction
    out["exit_source"] = "m30_failure_next_open"
    out["policy_net_ret"] = fraction * early_ret + (1.0 - fraction) * float(row["net_ret"])
    return out


def _metrics(trades: pd.DataFrame, window: str, policy: str) -> dict:
    start, end = WINDOWS[window]
    d = trades[
        (trades["entry_date"] >= pd.Timestamp(start))
        & (trades["entry_date"] <= pd.Timestamp(end))
    ].copy()
    if d.empty:
        return {"window": window, "policy": policy, "trades": 0}
    ret = d["policy_net_ret"].astype(float)
    base = d["baseline_net_ret"].astype(float)
    equity = (1.0 + ret).cumprod()
    dd = equity / equity.cummax() - 1.0
    return {
        "window": window,
        "policy": policy,
        "trades": int(len(d)),
        "triggered": int(d["triggered"].sum()),
        "executable": int(d["executable"].sum()),
        "trigger_rate": float(d["triggered"].mean()),
        "executable_rate": float(d["executable"].mean()),
        "win_rate": float((ret > 0).mean()),
        "mean_ret": float(ret.mean()),
        "median_ret": float(ret.median()),
        "worst_ret": float(ret.min()),
        "total_compound_ret": float(equity.iloc[-1] - 1.0),
        "max_closed_trade_drawdown": float(dd.min()),
        "baseline_mean_ret": float(base.mean()),
        "baseline_worst_ret": float(base.min()),
        "improvement_mean_ret": float(ret.mean() - base.mean()),
        "improvement_worst_ret": float(ret.min() - base.min()),
    }


def _display(raw: pd.DataFrame) -> pd.DataFrame:
    out = raw.copy()
    pct_cols = [
        "trigger_rate",
        "executable_rate",
        "win_rate",
        "mean_ret",
        "median_ret",
        "worst_ret",
        "total_compound_ret",
        "max_closed_trade_drawdown",
        "baseline_mean_ret",
        "baseline_worst_ret",
        "improvement_mean_ret",
        "improvement_worst_ret",
    ]
    for col in pct_cols:
        if col in out.columns:
            out[col] = out[col].map(_pct)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest coarse 30m failure exits for G3 panic module.")
    parser.add_argument("--input", default="reports/gen3_panic_v2_research/trade_risk_path_v1/pause_weak_no_capitulation_slot5_20pct_trade_risk_paths.csv")
    parser.add_argument("--output-dir", default="reports/gen3_panic_v2_research/failure_exit_30m_v1")
    parser.add_argument("--cost-bps", type=float, default=30.0)
    args = parser.parse_args()

    source = Path(args.input)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    paths = _load_paths(source)
    bar_map = _prepare_bar_map(paths)
    all_trades: list[pd.DataFrame] = []
    rows: list[dict] = []

    for policy in POLICIES:
        trades = pd.DataFrame([_apply_policy(row, bar_map, policy, args.cost_bps) for _, row in paths.iterrows()])
        trades = trades.sort_values(["entry_date", "candidate_score", "chain_rank"], ascending=[True, False, True])
        trades.to_csv(out_dir / f"{policy}_trades.csv", index=False, encoding="utf-8-sig")
        all_trades.append(trades)
        for window in WINDOWS:
            rows.append(_metrics(trades, window, policy))

    raw = pd.DataFrame(rows)
    raw.to_csv(out_dir / "failure_exit_30m_summary_raw.csv", index=False, encoding="utf-8-sig")
    display = _display(raw)
    display.to_csv(out_dir / "failure_exit_30m_summary_display.csv", index=False, encoding="utf-8-sig")

    full = display[display["window"].eq("full")]
    segmented = display[display["policy"].isin(["fixed_hold", "m30_close5_full_nextopen", "m30_close5_half_nextopen"])]

    worst = pd.concat(all_trades, ignore_index=True)
    worst = worst.sort_values("policy_net_ret").head(15).copy()
    for col in ["baseline_net_ret", "policy_net_ret", "trigger_close_ret", "exit_ret_net"]:
        if col in worst.columns:
            worst[col] = worst[col].map(_pct)
    worst.to_csv(out_dir / "worst_policy_trades_display.csv", index=False, encoding="utf-8-sig")

    lines = [
        "# G3 Panic 30m 失败退出验证 V1",
        "",
        "## 口径",
        "",
        f"- 输入文件：`{source}`",
        f"- 输出目录：`{out_dir}`",
        f"- 交易成本：`{args.cost_bps:.1f}bps`，与前序研究保持一致。",
        "- 样本固定为 `pause_weak_no_capitulation + slot5_20pct` 的 83 笔已成交交易。",
        "- 失败信号只用确认买入之后的 30m bar；触发条件为 30m 收盘跌破固定阈值。",
        "- 执行价不使用触发 bar 收盘，而是假设下一根 30m 开盘成交；若没有下一根 bar，则回退为固定持有退出。",
        "- 这是粗粒度风控验证，不是正式实盘规则。",
        "",
        "## full 对照",
        "",
        full[
            [
                "policy",
                "trades",
                "triggered",
                "executable",
                "win_rate",
                "mean_ret",
                "worst_ret",
                "total_compound_ret",
                "max_closed_trade_drawdown",
                "improvement_mean_ret",
                "improvement_worst_ret",
            ]
        ].to_markdown(index=False),
        "",
        "## 重点策略分段",
        "",
        segmented[
            [
                "window",
                "policy",
                "trades",
                "triggered",
                "executable",
                "win_rate",
                "mean_ret",
                "worst_ret",
                "total_compound_ret",
                "max_closed_trade_drawdown",
            ]
        ].to_markdown(index=False),
        "",
        "## 最差交易样本",
        "",
        worst[
            [
                "policy",
                "entry_date",
                "code",
                "name",
                "baseline_net_ret",
                "policy_net_ret",
                "triggered",
                "executable",
                "trigger_datetime",
                "trigger_close_ret",
                "exit_datetime",
                "exit_ret_net",
                "g3_repair_env_label",
                "market_style",
            ]
        ].to_markdown(index=False),
        "",
        "## 判断",
        "",
        "1. 若 full 改善但 train/valid 不一致，只能作为风险观察，不能进入策略。",
        "2. 全退出若显著改善最差收益但牺牲均值，需要优先考虑半仓退出或只用于更高风险环境。",
        "3. 下一轮需要把退出政策放回资金曲线逐日盯市里，确认组合回撤是否真正改善。",
    ]
    (out_dir / "failure_exit_30m_report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "input": str(source),
        "output_dir": str(out_dir),
        "trades": int(len(paths)),
        "policies": list(POLICIES),
        "cost_bps": args.cost_bps,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
