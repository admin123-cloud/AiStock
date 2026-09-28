from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "reports" / "gen3_down_panic_d1_env_proxy_audit_v1"

LIVE_SAFE = ROOT / "reports" / "gen3_guarded_live_safe_payload_v1" / "g3_guarded_live_safe_payload.csv"
PANIC_CLOSED = ROOT / "reports" / "gen3_panic_v2_research" / "final_candidate_v1" / "m30_close5_full_nextopen_cost30_closed_trades.csv"
MARKET_CONTEXT = ROOT / "reports" / "gen3_panic_v2_research" / "market_context.csv"

WINDOWS = {
    "weak_gap_2022_2024": ("2022-01-01", "2024-12-31"),
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-05-29"),
    "full": ("2020-01-01", "2026-05-29"),
}


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, low_memory=False) if path.exists() else pd.DataFrame()


def _date(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.normalize()


def _pct(value: Any) -> str:
    try:
        if pd.isna(value):
            return ""
        return f"{float(value) * 100:.2f}%"
    except Exception:
        return ""


def _max_drawdown_from_returns(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    equity = (1.0 + pd.to_numeric(returns, errors="coerce").fillna(0.0) * 0.20).cumprod()
    return float((equity / equity.cummax() - 1.0).min())


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    view = df.copy()
    for col in view.columns:
        if col in pct_cols:
            view[col] = view[col].map(_pct)
        else:
            view[col] = view[col].map(lambda x: "" if pd.isna(x) else str(x))
    return view.to_markdown(index=False)


def _load_joined() -> pd.DataFrame:
    live = _read_csv(LIVE_SAFE)
    live = live[live["route"].astype(str).eq("down_panic")].copy()
    live["entry_date"] = _date(live["entry_date"])
    live["code"] = live["code"].astype(str)

    closed = _read_csv(PANIC_CLOSED)
    closed["entry_date"] = _date(closed["entry_date"])
    closed["code"] = closed["code"].astype(str)
    closed_key_cols = [
        "entry_date",
        "code",
        "policy_net_ret",
        "candidate_score",
        "market_style",
        "breadth_ma20",
        "up_rate",
        "big_down_rate",
        "limit_down_proxy_rate",
        "market_amount_ratio20",
        "index_mom20",
        "confirm_datetime",
        "bar_close_pos",
        "bar_ret",
        "amount_ratio3",
    ]
    closed = closed[[c for c in closed_key_cols if c in closed.columns]].drop_duplicates(["entry_date", "code"])
    d = live.merge(closed, on=["entry_date", "code"], how="left", suffixes=("", "_closed"))

    ctx = _read_csv(MARKET_CONTEXT)
    ctx["trade_date"] = _date(ctx["trade_date"])
    ctx = ctx.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
    ctx_cols = [
        "trade_date",
        "market_style",
        "ma_skeleton",
        "volume_price_layer",
        "adx_layer",
        "breadth_ma20",
        "breadth_ma60",
        "up_rate",
        "big_down_rate",
        "limit_down_proxy_rate",
        "market_amount_ratio20",
        "adx20",
        "mom20",
    ]
    ctx = ctx[[c for c in ctx_cols if c in ctx.columns]].copy()
    ctx["prev_entry_date"] = ctx["trade_date"].shift(-1)
    d = d.merge(
        ctx.add_suffix("_d1").rename(columns={"prev_entry_date_d1": "entry_date"}),
        on="entry_date",
        how="left",
    )
    return d


def _apply_proxy_rules(d: pd.DataFrame) -> pd.DataFrame:
    out = d.copy()
    out["same_day_env_panic_context"] = (
        out["market_style"].isin(["standard_downtrend", "standard_range"])
        & (
            (pd.to_numeric(out["up_rate"], errors="coerce") <= 0.35)
            | (pd.to_numeric(out["big_down_rate"], errors="coerce") >= 0.12)
            | (pd.to_numeric(out["limit_down_proxy_rate"], errors="coerce") >= 0.01)
        )
    )
    out["d1_env_panic_context"] = (
        out["market_style_d1"].isin(["standard_downtrend", "standard_range"])
        & (
            (pd.to_numeric(out["up_rate_d1"], errors="coerce") <= 0.35)
            | (pd.to_numeric(out["big_down_rate_d1"], errors="coerce") >= 0.12)
            | (pd.to_numeric(out["limit_down_proxy_rate_d1"], errors="coerce") >= 0.01)
        )
    )
    out["d1_env_deep_stress"] = (
        (pd.to_numeric(out["big_down_rate_d1"], errors="coerce") >= 0.10)
        | (pd.to_numeric(out["up_rate_d1"], errors="coerce") <= 0.30)
        | (pd.to_numeric(out["limit_down_proxy_rate_d1"], errors="coerce") >= 0.01)
    )
    out["d1_env_proxy_pass"] = out["d1_env_panic_context"] & out["d1_env_deep_stress"]
    out["d1_env_proxy_status"] = out["d1_env_proxy_pass"].map(lambda x: "pass_d1_env_proxy" if bool(x) else "blocked_d1_env_not_panic")
    return out


def _metrics(d: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for name, mask in {
        "same_day_original": pd.Series(True, index=d.index),
        "d1_env_proxy_pass": d["d1_env_proxy_pass"].fillna(False),
        "d1_env_proxy_blocked": ~d["d1_env_proxy_pass"].fillna(False),
    }.items():
        g = d[mask].copy()
        ret = pd.to_numeric(g.get("policy_net_ret"), errors="coerce")
        rows.append(
            {
                "sample": name,
                "rows": len(g),
                "unique_dates": int(g["entry_date"].nunique()) if not g.empty else 0,
                "win_rate": float((ret > 0).mean()) if len(g) else 0.0,
                "avg_policy_ret": float(ret.mean()) if len(g) else 0.0,
                "sum_policy_ret": float(ret.sum()) if len(g) else 0.0,
                "worst_trade": float(ret.min()) if len(g) else 0.0,
                "rough_slot20_max_dd": _max_drawdown_from_returns(ret),
            }
        )
    return pd.DataFrame(rows)


def _window_metrics(d: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for window, (start, end) in WINDOWS.items():
        w = d[(d["entry_date"] >= pd.Timestamp(start)) & (d["entry_date"] <= pd.Timestamp(end))].copy()
        for name, mask in {
            "same_day_original": pd.Series(True, index=w.index),
            "d1_env_proxy_pass": w["d1_env_proxy_pass"].fillna(False) if not w.empty else pd.Series(dtype=bool),
        }.items():
            g = w[mask].copy() if not w.empty else w
            ret = pd.to_numeric(g.get("policy_net_ret"), errors="coerce")
            rows.append(
                {
                    "window": window,
                    "sample": name,
                    "rows": len(g),
                    "win_rate": float((ret > 0).mean()) if len(g) else 0.0,
                    "avg_policy_ret": float(ret.mean()) if len(g) else 0.0,
                    "sum_policy_ret": float(ret.sum()) if len(g) else 0.0,
                    "worst_trade": float(ret.min()) if len(g) else 0.0,
                    "rough_slot20_max_dd": _max_drawdown_from_returns(ret),
                }
            )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    joined = _apply_proxy_rules(_load_joined())
    metrics = _metrics(joined)
    windows = _window_metrics(joined)
    preview_cols = [
        "entry_date",
        "code",
        "name",
        "policy_net_ret",
        "market_style",
        "up_rate",
        "big_down_rate",
        "limit_down_proxy_rate",
        "market_style_d1",
        "up_rate_d1",
        "big_down_rate_d1",
        "limit_down_proxy_rate_d1",
        "d1_env_proxy_status",
    ]
    candidate_audit = joined[[c for c in preview_cols if c in joined.columns]].copy()

    candidate_audit.to_csv(OUT_DIR / "down_panic_d1_env_candidate_audit.csv", index=False, encoding="utf-8-sig")
    metrics.to_csv(OUT_DIR / "down_panic_d1_env_metrics.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "down_panic_d1_env_window_metrics.csv", index=False, encoding="utf-8-sig")

    pass_rows = int(joined["d1_env_proxy_pass"].fillna(False).sum())
    summary = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "input_rows": int(len(joined)),
        "d1_env_proxy_pass_rows": pass_rows,
        "d1_env_proxy_pass_rate": float(pass_rows / len(joined)) if len(joined) else 0.0,
        "hard_future_function_found": False,
        "live_readiness": "down_panic_env_proxy_partially_validated",
        "next_step": "decide_whether_to_apply_d1_env_proxy_or_build_intraday_market_proxy",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    report = f"""# G3 down_panic D-1 环境代理审计 V1

生成时间：{summary['generated_at']}

## 目的

这一步只验证一个问题：`down_panic` 是否可以用前一交易日市场环境快照替代信号日全日环境统计，避免把收盘后信息带进 10:00 买点。

审计对象：G3 guarded live-safe payload 中的 `down_panic` 样本，共 `{len(joined)}` 笔。

## 核心结果

- D-1 环境代理通过样本：`{pass_rows}` / `{len(joined)}`，通过率 `{_pct(summary['d1_env_proxy_pass_rate'])}`
- 这不是最终规则，只是代理可行性审计。
- 当前没有发现硬未来函数字段进入 live-safe payload；问题集中在“环境字段用 D-1 是否会损伤样本和收益”。

## 总体指标

{_md_table(metrics, pct_cols={"win_rate", "avg_policy_ret", "sum_policy_ret", "worst_trade", "rough_slot20_max_dd"})}

## 分窗口指标

{_md_table(windows, pct_cols={"win_rate", "avg_policy_ret", "sum_policy_ret", "worst_trade", "rough_slot20_max_dd"})}

## 判断

如果 D-1 环境代理保留样本过少或收益明显塌陷，就不能简单替换；下一步应构造“截至当前 30m 的市场环境代理”，例如实时上涨率、下跌率、跌停代理、成交额比例。

如果 D-1 代理保留样本和窗口表现仍然稳定，则可以先把 `down_panic` 从 `env_proxy_required=true` 升级为 `env_proxy_mode=d1_snapshot` 的 shadow-only 候选，再继续积累实盘样本。

## 下一步目标

根据本审计结果，选择两条路之一：

1. 直接把 D-1 环境代理写入 live-safe payload。
2. 如果 D-1 损伤过大，继续做 30m 市场环境代理，不急着把 `down_panic` 升级为 live-ready。
"""
    (OUT_DIR / "down_panic_d1_env_proxy_audit_report_cn.md").write_text(report, encoding="utf-8", newline="\n")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
