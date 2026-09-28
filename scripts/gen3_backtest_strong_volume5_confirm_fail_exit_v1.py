from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_exit_proxy_v1 import _simulate
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import (
    INITIAL_CAPITAL,
    SOURCE,
    WINDOWS,
    _classify_market_style,
    _exit_date_map,
    _max_drawdown,
    _md_table,
    _trade_calendar,
)


OUT_DIR = ROOT / "reports" / "gen3_strong_volume5_confirm_fail_exit_v1"


def _prepare_base(cost_bps: float) -> pd.DataFrame:
    df = pd.read_parquet(SOURCE)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.normalize()
    df = df[df["source_family"].eq("volume5")].copy()
    df["h5"] = pd.to_numeric(df["outcome_fwd_ret_5d"], errors="coerce").where(
        pd.to_numeric(df["outcome_fwd_ret_5d"], errors="coerce").notna(),
        pd.to_numeric(df["fwd_ret_5d"], errors="coerce"),
    )
    for col in ["fwd_ret_1d", "fwd_ret_2d", "fwd_ret_3d"]:
        df[col] = pd.to_numeric(df.get(col), errors="coerce")
    df = df.dropna(subset=["entry_date", "h5", "code"]).copy()
    df["v4_rank"] = pd.to_numeric(df.get("v4_rank"), errors="coerce").fillna(1e9)
    df["v4_score"] = pd.to_numeric(df.get("v4_score"), errors="coerce").fillna(-1e9)
    df["stop5_touch_30m"] = df.get("stop5_touch_30m", False).fillna(False).astype(bool)
    df["stop5_touch_datetime"] = pd.to_datetime(df.get("stop5_touch_datetime"), errors="coerce")
    df["g3_market_style"] = df.apply(_classify_market_style, axis=1)
    df = df[df["g3_market_style"].isin(["main_up", "weak_recovery"])].copy()

    calendar = _trade_calendar(df["entry_date"].min(), df["entry_date"].max() + pd.Timedelta(days=30))
    for hold_days in [1, 2, 3, 5]:
        exit_map = _exit_date_map(calendar, hold_days)
        df[f"exit_date_h{hold_days}"] = df["entry_date"].map(exit_map)
        df[f"exit_date_h{hold_days}"] = pd.to_datetime(df[f"exit_date_h{hold_days}"], errors="coerce").dt.normalize()
    df = df.dropna(subset=["exit_date_h5"]).copy()
    df["fixed_net_ret"] = df["h5"] - cost_bps / 10000.0
    return df.sort_values(["entry_date", "v4_rank", "v4_score", "confirm_datetime", "code"], ascending=[True, True, False, True, True])


def _apply_policy(base: pd.DataFrame, policy: str, cost_bps: float) -> pd.DataFrame:
    d = base.copy()
    d["exit_reason_proxy"] = "fixed_h5"
    d["policy_exit_date"] = d["exit_date_h5"]
    d["net_ret"] = d["fixed_net_ret"]
    stop = d["stop5_touch_30m"].astype(bool)

    if policy == "fixed_h5":
        pass
    elif policy == "stop5_full":
        d.loc[stop, "policy_exit_date"] = pd.to_datetime(d.loc[stop, "stop5_touch_datetime"], errors="coerce").dt.normalize().where(
            pd.to_datetime(d.loc[stop, "stop5_touch_datetime"], errors="coerce").notna(),
            d.loc[stop, "entry_date"],
        )
        d.loc[stop, "net_ret"] = -0.05 - cost_bps / 10000.0
        d.loc[stop, "exit_reason_proxy"] = "stop5_30m_full"
    elif policy == "confirm_d1_le_m5":
        fail = stop & d["fwd_ret_1d"].le(-0.05)
        d.loc[fail, "policy_exit_date"] = d.loc[fail, "exit_date_h1"]
        d.loc[fail, "net_ret"] = d.loc[fail, "fwd_ret_1d"] - cost_bps / 10000.0
        d.loc[fail, "exit_reason_proxy"] = "confirm_fail_d1_le_m5"
    elif policy == "confirm_d2_le_m5":
        fail = stop & d["fwd_ret_2d"].le(-0.05)
        d.loc[fail, "policy_exit_date"] = d.loc[fail, "exit_date_h2"]
        d.loc[fail, "net_ret"] = d.loc[fail, "fwd_ret_2d"] - cost_bps / 10000.0
        d.loc[fail, "exit_reason_proxy"] = "confirm_fail_d2_le_m5"
    elif policy == "confirm_d1_m3_d2_le0":
        fail = stop & d["fwd_ret_1d"].le(-0.03) & d["fwd_ret_2d"].le(0.0)
        d.loc[fail, "policy_exit_date"] = d.loc[fail, "exit_date_h2"]
        d.loc[fail, "net_ret"] = d.loc[fail, "fwd_ret_2d"] - cost_bps / 10000.0
        d.loc[fail, "exit_reason_proxy"] = "confirm_fail_d1_m3_d2_le0"
    elif policy == "confirm_d3_le0":
        fail = stop & d["fwd_ret_3d"].le(0.0)
        d.loc[fail, "policy_exit_date"] = d.loc[fail, "exit_date_h3"]
        d.loc[fail, "net_ret"] = d.loc[fail, "fwd_ret_3d"] - cost_bps / 10000.0
        d.loc[fail, "exit_reason_proxy"] = "confirm_fail_d3_le0"
    else:
        raise ValueError(policy)

    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d = d.dropna(subset=["policy_exit_date", "net_ret"]).copy()
    return d.sort_values(["entry_date", "v4_rank", "v4_score", "confirm_datetime", "code"], ascending=[True, True, False, True, True])


def _metrics(closed: pd.DataFrame, curve: pd.DataFrame, window: str, policy: str, book: str) -> dict:
    start, end = WINDOWS[window]
    cw = curve[(curve["date"] >= pd.Timestamp(start)) & (curve["date"] <= pd.Timestamp(end))].copy()
    tw = closed[(closed["entry_date"] >= pd.Timestamp(start)) & (closed["entry_date"] <= pd.Timestamp(end))].copy() if not closed.empty else pd.DataFrame()
    if cw.empty:
        return {"window": window, "policy": policy, "book": book, "closed": 0}
    start_equity = float(cw["equity"].iloc[0])
    end_equity = float(cw["equity"].iloc[-1])
    local = cw["equity"] / start_equity
    dd = local / local.cummax() - 1.0
    return {
        "window": window,
        "policy": policy,
        "book": book,
        "closed": int(len(tw)),
        "early_exits": int((tw.get("exit_reason_proxy", pd.Series(dtype=str)).astype(str) != "fixed_h5").sum()) if not tw.empty else 0,
        "start_equity": start_equity,
        "end_equity": end_equity,
        "total_ret": end_equity / start_equity - 1.0,
        "max_drawdown": float(dd.min()),
        "win_rate": float((tw["net_ret"] > 0).mean()) if len(tw) else 0.0,
        "mean_trade_ret": float(tw["net_ret"].mean()) if len(tw) else 0.0,
        "worst_trade": float(tw["net_ret"].min()) if len(tw) else 0.0,
        "max_open_positions": int(cw["open_positions"].max()),
    }


def _annual(policy: str, book: str, closed: pd.DataFrame, curve: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for year, part in curve.groupby(curve["date"].dt.year):
        part = part.sort_values("date")
        tw = closed[closed["entry_date"].dt.year.eq(year)] if not closed.empty else pd.DataFrame()
        rows.append(
            {
                "policy": policy,
                "book": book,
                "year": int(year),
                "return": float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0),
                "max_drawdown": _max_drawdown(part["equity"]),
                "closed": int(len(tw)),
                "early_exits": int((tw.get("exit_reason_proxy", pd.Series(dtype=str)).astype(str) != "fixed_h5").sum()) if not tw.empty else 0,
                "win_rate": float((tw["net_ret"] > 0).mean()) if len(tw) else 0.0,
                "mean_trade_ret": float(tw["net_ret"].mean()) if len(tw) else 0.0,
                "worst_trade": float(tw["net_ret"].min()) if len(tw) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _write_report(summary: pd.DataFrame, annual: pd.DataFrame) -> None:
    pct_cols = {"total_ret", "max_drawdown", "win_rate", "mean_trade_ret", "worst_trade", "return"}
    full = summary[summary["window"].eq("full")].sort_values(["book", "policy"])
    slot5_annual = annual[annual["book"].eq("slot5_20pct_daily2")].sort_values(["policy", "year"])
    lines = [
        "# G3 强势链路 Volume5 确认失败退出代理 V1",
        "",
        "## 口径",
        "",
        "- 样本固定为 `core_recovery_volume5 = main_up + weak_recovery`。",
        "- 只测试少数预注册代理，不做参数搜索：`fixed_h5`、`stop5_full`、`confirm_d1_le_m5`、`confirm_d2_le_m5`、`confirm_d1_m3_d2_le0`、`confirm_d3_le0`。",
        "- `confirm_*` 使用 1/2/3 日前瞻收盘收益作为研究代理，目的是验证“触发 stop 后等待修复确认”是否值得继续做 30m 明细重放；它本身不是可实盘规则。",
        "- 成本固定 30bps，账本固定 `slot2_50pct_daily1` 与 `slot5_20pct_daily2`。",
        "",
        "## Full 窗口",
        "",
        _md_table(full, pct_cols=pct_cols),
        "",
        "## Slot5 年度",
        "",
        _md_table(slot5_annual, pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 若确认失败代理优于裸 stop，同时不显著恶化回撤，说明强势链路退出应走“失败确认”而不是“触发即卖”。",
        "- 若确认代理仍无法改善，则下一步应回到入场质量：寻找不触发 stop 的强势股共同特征，而不是继续打磨卖点。",
        "",
    ]
    (OUT_DIR / "confirm_fail_exit_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cost_bps = 30.0
    policies = [
        "fixed_h5",
        "stop5_full",
        "confirm_d1_le_m5",
        "confirm_d2_le_m5",
        "confirm_d1_m3_d2_le0",
        "confirm_d3_le0",
    ]
    books = [
        ("slot2_50pct_daily1", 2, 0.50, 1),
        ("slot5_20pct_daily2", 5, 0.20, 2),
    ]
    base = _prepare_base(cost_bps)
    summaries = []
    annual_parts = []
    for policy in policies:
        candidates = _apply_policy(base, policy, cost_bps)
        for book, slots, slot_pct, daily_open_limit in books:
            closed, curve = _simulate(candidates, policy, slots, slot_pct, daily_open_limit)
            stem = f"core_volume5_{policy}_{book}"
            closed.to_csv(OUT_DIR / f"{stem}_closed_trades.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(OUT_DIR / f"{stem}_curve.csv", index=False, encoding="utf-8-sig")
            annual_parts.append(_annual(policy, book, closed, curve))
            for window in WINDOWS:
                summaries.append(_metrics(closed, curve, window, policy, book))

    summary = pd.DataFrame(summaries)
    annual = pd.concat(annual_parts, ignore_index=True) if annual_parts else pd.DataFrame()
    summary.to_csv(OUT_DIR / "confirm_fail_exit_summary_raw.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "confirm_fail_exit_annual_raw.csv", index=False, encoding="utf-8-sig")
    _write_report(summary, annual)
    full = summary[summary["window"].eq("full")].sort_values(["book", "policy"])
    print(
        json.dumps(
            {
                "out_dir": str(OUT_DIR),
                "source_rows": int(len(base)),
                "full": full[["policy", "book", "closed", "early_exits", "total_ret", "max_drawdown", "win_rate", "mean_trade_ret", "worst_trade"]].to_dict(orient="records"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
