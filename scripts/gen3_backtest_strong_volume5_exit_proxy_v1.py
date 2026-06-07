from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import (
    INITIAL_CAPITAL,
    OUT_DIR as SLOT_DIR,
    SOURCE,
    WINDOWS,
    _classify_market_style,
    _exit_date_map,
    _max_drawdown,
    _md_table,
    _pct,
    _trade_calendar,
)


OUT_DIR = ROOT / "reports" / "gen3_strong_volume5_exit_proxy_v1"


def _prepare_base(cost_bps: float) -> pd.DataFrame:
    df = pd.read_parquet(SOURCE)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.normalize()
    df = df[df["source_family"].eq("volume5")].copy()
    df["gross_ret_h5"] = pd.to_numeric(df["outcome_fwd_ret_5d"], errors="coerce").where(
        pd.to_numeric(df["outcome_fwd_ret_5d"], errors="coerce").notna(),
        pd.to_numeric(df["fwd_ret_5d"], errors="coerce"),
    )
    df = df.dropna(subset=["entry_date", "gross_ret_h5", "code"]).copy()
    df["v4_rank"] = pd.to_numeric(df.get("v4_rank"), errors="coerce").fillna(1e9)
    df["v4_score"] = pd.to_numeric(df.get("v4_score"), errors="coerce").fillna(-1e9)
    df["stop5_touch_30m"] = df.get("stop5_touch_30m", False).fillna(False).astype(bool)
    df["stop5_touch_datetime"] = pd.to_datetime(df.get("stop5_touch_datetime"), errors="coerce")
    df["g3_market_style"] = df.apply(_classify_market_style, axis=1)
    df = df[df["g3_market_style"].isin(["main_up", "weak_recovery"])].copy()

    calendar = _trade_calendar(df["entry_date"].min(), df["entry_date"].max() + pd.Timedelta(days=20))
    exit_map = _exit_date_map(calendar, 5)
    df["fixed_exit_date"] = df["entry_date"].map(exit_map)
    df = df.dropna(subset=["fixed_exit_date"]).copy()
    df["fixed_exit_date"] = pd.to_datetime(df["fixed_exit_date"]).dt.normalize()
    df["fixed_net_ret"] = df["gross_ret_h5"] - cost_bps / 10000.0
    return df.sort_values(["entry_date", "v4_rank", "v4_score", "confirm_datetime", "code"], ascending=[True, True, False, True, True])


def _apply_exit_policy(base: pd.DataFrame, policy: str, cost_bps: float) -> pd.DataFrame:
    d = base.copy()
    d["exit_reason_proxy"] = "fixed_h5"
    d["policy_exit_date"] = d["fixed_exit_date"]
    d["net_ret"] = d["fixed_net_ret"]
    if policy in {"stop5_full", "stop5_cd3_skip"}:
        stop = d["stop5_touch_30m"].astype(bool)
        stop_date = pd.to_datetime(d["stop5_touch_datetime"], errors="coerce").dt.normalize()
        stop_date = stop_date.where(stop_date.notna(), d["entry_date"])
        d.loc[stop, "policy_exit_date"] = stop_date[stop]
        d.loc[stop, "net_ret"] = -0.05 - cost_bps / 10000.0
        d.loc[stop, "exit_reason_proxy"] = "stop5_30m_full"
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"]).dt.normalize()
    return d.sort_values(["entry_date", "v4_rank", "v4_score", "confirm_datetime", "code"], ascending=[True, True, False, True, True])


def _simulate(
    candidates: pd.DataFrame,
    policy: str,
    slots: int,
    slot_pct: float,
    daily_open_limit: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    calendar = _trade_calendar(candidates["entry_date"].min(), candidates["policy_exit_date"].max())
    cal_index = {day: idx for idx, day in enumerate(calendar)}
    by_entry = {day: g.copy() for day, g in candidates.groupby("entry_date")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict] = []
    closed: list[dict] = []
    curve_rows: list[dict] = []
    cooldown_until_idx = -1

    for idx, day in enumerate(calendar):
        realized_pnl = 0.0
        stop_events_today = 0
        still_open: list[dict] = []
        for pos in open_pos:
            if pos["policy_exit_date"] <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["net_ret"]))
                cash += exit_value
                realized_pnl += exit_value - float(pos["stake"])
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = exit_value - float(pos["stake"])
                closed.append(out)
                if str(pos.get("exit_reason_proxy")) == "stop5_30m_full":
                    stop_events_today += 1
            else:
                still_open.append(pos)
        open_pos = still_open

        if policy == "stop5_cd3_skip" and stop_events_today > 0:
            cooldown_until_idx = max(cooldown_until_idx, idx + 3)
        cooldown_active = policy == "stop5_cd3_skip" and idx <= cooldown_until_idx

        opened = 0
        skipped_slots = 0
        skipped_daily_limit = 0
        skipped_cooldown = 0
        todays = by_entry.get(day)
        if todays is not None:
            for row in todays.itertuples(index=False):
                if cooldown_active:
                    skipped_cooldown += 1
                    continue
                if opened >= daily_open_limit:
                    skipped_daily_limit += 1
                    continue
                if len(open_pos) >= slots:
                    skipped_slots += 1
                    continue
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                stake = equity_before * slot_pct
                if stake <= 0 or cash < stake:
                    skipped_slots += 1
                    continue
                pos = row._asdict()
                pos["stake"] = stake
                cash -= stake
                # If a stop is labelled on entry day, close it immediately at the proxy stop return.
                if cal_index.get(pd.Timestamp(pos["policy_exit_date"]).normalize(), idx) <= idx:
                    exit_value = stake * (1.0 + float(pos["net_ret"]))
                    cash += exit_value
                    realized_pnl += exit_value - stake
                    pos["exit_value"] = exit_value
                    pos["realized_pnl"] = exit_value - stake
                    closed.append(pos)
                    if str(pos.get("exit_reason_proxy")) == "stop5_30m_full":
                        stop_events_today += 1
                        if policy == "stop5_cd3_skip":
                            cooldown_until_idx = max(cooldown_until_idx, idx + 3)
                else:
                    open_pos.append(pos)
                opened += 1

        reserved_principal = sum(float(p["stake"]) for p in open_pos)
        equity = cash + reserved_principal
        curve_rows.append(
            {
                "date": day,
                "cash": cash,
                "reserved_principal": reserved_principal,
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "skipped_slots": skipped_slots,
                "skipped_daily_limit": skipped_daily_limit,
                "skipped_cooldown": skipped_cooldown,
                "stop_events_today": stop_events_today,
                "cooldown_active": cooldown_active,
                "realized_pnl": realized_pnl,
            }
        )

    closed_df = pd.DataFrame(closed)
    curve = pd.DataFrame(curve_rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return closed_df, curve


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
        "stop_exits": int((tw.get("exit_reason_proxy", pd.Series(dtype=str)).astype(str) == "stop5_30m_full").sum()) if not tw.empty else 0,
        "start_equity": start_equity,
        "end_equity": end_equity,
        "total_ret": end_equity / start_equity - 1.0,
        "max_drawdown": float(dd.min()),
        "win_rate": float((tw["net_ret"] > 0).mean()) if len(tw) else 0.0,
        "mean_trade_ret": float(tw["net_ret"].mean()) if len(tw) else 0.0,
        "worst_trade": float(tw["net_ret"].min()) if len(tw) else 0.0,
        "max_open_positions": int(cw["open_positions"].max()),
        "skipped_cooldown": int(cw.get("skipped_cooldown", pd.Series(dtype=float)).sum()),
        "cooldown_days": int(cw.get("cooldown_active", pd.Series(dtype=bool)).astype(bool).sum()),
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
                "stop_exits": int((tw.get("exit_reason_proxy", pd.Series(dtype=str)).astype(str) == "stop5_30m_full").sum()) if not tw.empty else 0,
                "win_rate": float((tw["net_ret"] > 0).mean()) if len(tw) else 0.0,
                "mean_trade_ret": float(tw["net_ret"].mean()) if len(tw) else 0.0,
                "worst_trade": float(tw["net_ret"].min()) if len(tw) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cost_bps = 30.0
    base = _prepare_base(cost_bps)
    policies = ["fixed_h5", "stop5_full", "stop5_cd3_skip"]
    books = [
        ("slot2_50pct_daily1", 2, 0.50, 1),
        ("slot5_20pct_daily2", 5, 0.20, 2),
    ]
    summaries = []
    annual_parts = []
    for policy in policies:
        candidates = _apply_exit_policy(base, policy, cost_bps)
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
    summary.to_csv(OUT_DIR / "exit_proxy_summary_raw.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "exit_proxy_annual_raw.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"total_ret", "max_drawdown", "win_rate", "mean_trade_ret", "worst_trade", "return"}
    full = summary[summary["window"].eq("full")].sort_values(["book", "policy"])
    slot5_annual = annual[annual["book"].eq("slot5_20pct_daily2")].sort_values(["policy", "year"])
    report = [
        "# G3 强势链路 Volume5 退出冷却代理 V1",
        "",
        "## 口径",
        "",
        "- 只研究 `core_recovery_volume5`：`main_up + weak_recovery`。",
        "- 基础账本沿用第13步：`slot2_50pct_daily1` 与 `slot5_20pct_daily2`。",
        "- `fixed_h5` 是固定 5 日持有；`stop5_full` 是 30m 触及 -5% 后全退；`stop5_cd3_skip` 是 stop 后 3 个交易日不开新仓。",
        "- 这里使用既有源中的 `stop5_touch_30m`/`stop5_touch_datetime` 做历史执行代理；后续正式 G3 必须改成从 30m 明细实时重放。",
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
        "- 30m -5% 全退显著压低单笔尾部亏损，但会牺牲部分进攻收益。",
        "- 冷却规则是否值得保留，要看它是否降低 2021/2022/2024 的坏月份，而不是看 full 收益是否最高。",
        "- 若该代理有效，下一步应做真正的 30m 明细重放，包括触发当根/次根成交、跌停不可卖、滑点和尾盘次日开盘成交。",
        "",
    ]
    (OUT_DIR / "exit_proxy_report_cn.md").write_text("\n".join(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "out_dir": str(OUT_DIR),
                "source_core_rows": int(len(base)),
                "full": full[["policy", "book", "closed", "stop_exits", "total_ret", "max_drawdown", "win_rate", "mean_trade_ret", "worst_trade", "skipped_cooldown", "cooldown_days"]].to_dict(orient="records"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
