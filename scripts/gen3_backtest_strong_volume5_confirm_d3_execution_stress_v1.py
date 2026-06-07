from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_confirm_fail_exit_v1 import _apply_policy, _prepare_base
from scripts.gen3_backtest_strong_volume5_exit_proxy_v1 import _simulate
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import WINDOWS, _max_drawdown, _md_table, _trade_calendar
from utils.market_warehouse import clickhouse_query_df


OUT_DIR = ROOT / "reports" / "gen3_strong_volume5_confirm_d3_execution_stress_v1"


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _load_daily_prices(candidates: pd.DataFrame, extra_days: int = 10) -> pd.DataFrame:
    codes = sorted(candidates["code"].dropna().astype(str).unique().tolist())
    start = pd.Timestamp(candidates["entry_date"].min()).strftime("%Y-%m-%d")
    end = (pd.Timestamp(candidates["policy_exit_date"].max()) + pd.Timedelta(days=extra_days * 3)).strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 300):
        quoted = ",".join(_sql_literal(c) for c in codes[i : i + 300])
        sql = f"""
        SELECT code, trade_date, open, high, low, close
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN toDate({_sql_literal(start)}) AND toDate({_sql_literal(end)})
        ORDER BY code, trade_date
        """
        part = clickhouse_query_df(sql)
        if not part.empty:
            parts.append(part)
    d = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if d.empty:
        return d
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.normalize()
    for col in ["open", "high", "low", "close"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["code", "trade_date", "open", "high", "low", "close"]).sort_values(["code", "trade_date"])
    d["prev_close"] = d.groupby("code")["close"].shift(1)
    d["approx_limit_down"] = (d["prev_close"] > 0) & (d["high"] <= d["prev_close"] * 0.905)
    return d.reset_index(drop=True)


def _next_trade_date_map(calendar: list[pd.Timestamp]) -> dict[pd.Timestamp, pd.Timestamp]:
    return {calendar[i]: calendar[i + 1] for i in range(len(calendar) - 1)}


def _price_maps(daily: pd.DataFrame) -> dict[str, dict[tuple[str, pd.Timestamp], float | bool]]:
    out: dict[str, dict[tuple[str, pd.Timestamp], float | bool]] = {k: {} for k in ["open", "close", "high", "low", "limit_down"]}
    for r in daily.itertuples(index=False):
        key = (str(r.code), pd.Timestamp(r.trade_date).normalize())
        out["open"][key] = float(r.open)
        out["close"][key] = float(r.close)
        out["high"][key] = float(r.high)
        out["low"][key] = float(r.low)
        out["limit_down"][key] = bool(r.approx_limit_down)
    return out


def _ret_from_price(price: float | None, entry_price: float, cost_bps: float) -> float | None:
    if price is None or entry_price <= 0:
        return None
    return float(price) / float(entry_price) - 1.0 - cost_bps / 10000.0


def _apply_execution_variant(candidates: pd.DataFrame, daily: pd.DataFrame, variant: str, cost_bps: float) -> pd.DataFrame:
    d = candidates.copy()
    d["execution_variant"] = variant
    d["execution_note"] = "base"
    maps = _price_maps(daily)
    calendar = _trade_calendar(d["entry_date"].min(), d["policy_exit_date"].max() + pd.Timedelta(days=20))
    next_map = _next_trade_date_map(calendar)

    if variant == "same_close":
        return d

    for idx, row in d.iterrows():
        code = str(row["code"])
        entry_price = float(row.get("entry_price") or 0.0)
        exit_date = pd.Timestamp(row["policy_exit_date"]).normalize()
        target_date = exit_date
        target_field = "close"
        note = variant

        if variant == "next_open_all":
            target_date = next_map.get(exit_date, exit_date)
            target_field = "open"
        elif variant == "limit_down_delay_next_open":
            if bool(maps["limit_down"].get((code, exit_date), False)):
                target_date = next_map.get(exit_date, exit_date)
                target_field = "open"
                note = "limit_down_delayed"
            else:
                target_field = "close"
                note = "no_limit_down"
        else:
            raise ValueError(variant)

        price = maps[target_field].get((code, target_date))
        ret = _ret_from_price(price, entry_price, cost_bps)
        if ret is None:
            d.at[idx, "execution_note"] = "missing_price_keep_base"
            continue
        d.at[idx, "policy_exit_date"] = target_date
        d.at[idx, "net_ret"] = ret
        d.at[idx, "execution_note"] = note

    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["net_ret"] = pd.to_numeric(d["net_ret"], errors="coerce")
    return d.dropna(subset=["policy_exit_date", "net_ret"]).sort_values(
        ["entry_date", "v4_rank", "v4_score", "confirm_datetime", "code"],
        ascending=[True, True, False, True, True],
    )


def _metrics(closed: pd.DataFrame, curve: pd.DataFrame, window: str, profile: str, cost_bps: float) -> dict:
    start, end = WINDOWS[window]
    cw = curve[(curve["date"] >= pd.Timestamp(start)) & (curve["date"] <= pd.Timestamp(end))].copy()
    tw = closed[(closed["entry_date"] >= pd.Timestamp(start)) & (closed["entry_date"] <= pd.Timestamp(end))].copy() if not closed.empty else pd.DataFrame()
    if cw.empty:
        return {"window": window, "profile": profile, "cost_bps": cost_bps, "closed": 0}
    start_equity = float(cw["equity"].iloc[0])
    end_equity = float(cw["equity"].iloc[-1])
    local = cw["equity"] / start_equity
    dd = local / local.cummax() - 1.0
    return {
        "window": window,
        "profile": profile,
        "cost_bps": cost_bps,
        "closed": int(len(tw)),
        "early_exits": int((tw.get("exit_reason_proxy", pd.Series(dtype=str)).astype(str) != "fixed_h5").sum()) if not tw.empty else 0,
        "start_equity": start_equity,
        "end_equity": end_equity,
        "total_ret": end_equity / start_equity - 1.0,
        "max_drawdown": float(dd.min()),
        "win_rate": float((tw["net_ret"] > 0).mean()) if len(tw) else 0.0,
        "mean_trade_ret": float(tw["net_ret"].mean()) if len(tw) else 0.0,
        "worst_trade": float(tw["net_ret"].min()) if len(tw) else 0.0,
        "bad10_rate": float((tw["net_ret"] <= -0.10).mean()) if len(tw) else 0.0,
        "max_open_positions": int(cw["open_positions"].max()),
    }


def _annual(profile: str, cost_bps: float, closed: pd.DataFrame, curve: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for year, part in curve.groupby(curve["date"].dt.year):
        part = part.sort_values("date")
        tw = closed[closed["entry_date"].dt.year.eq(year)] if not closed.empty else pd.DataFrame()
        rows.append(
            {
                "profile": profile,
                "cost_bps": cost_bps,
                "year": int(year),
                "return": float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0),
                "max_drawdown": _max_drawdown(part["equity"]),
                "closed": int(len(tw)),
                "early_exits": int((tw.get("exit_reason_proxy", pd.Series(dtype=str)).astype(str) != "fixed_h5").sum()) if not tw.empty else 0,
                "win_rate": float((tw["net_ret"] > 0).mean()) if len(tw) else 0.0,
                "mean_trade_ret": float(tw["net_ret"].mean()) if len(tw) else 0.0,
                "worst_trade": float(tw["net_ret"].min()) if len(tw) else 0.0,
                "bad10_rate": float((tw["net_ret"] <= -0.10).mean()) if len(tw) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _diagnostics(candidates: pd.DataFrame, profile: str, cost_bps: float) -> dict:
    return {
        "profile": profile,
        "cost_bps": cost_bps,
        "rows": int(len(candidates)),
        "execution_notes": {str(k): int(v) for k, v in candidates.get("execution_note", pd.Series(dtype=str)).astype(str).value_counts(dropna=False).to_dict().items()},
        "mean_ret": float(candidates["net_ret"].mean()),
        "worst_ret": float(candidates["net_ret"].min()),
        "bad10_rate": float((candidates["net_ret"] <= -0.10).mean()),
    }


def _write_report(summary: pd.DataFrame, annual: pd.DataFrame, diagnostics: pd.DataFrame) -> None:
    pct_cols = {"total_ret", "max_drawdown", "win_rate", "mean_trade_ret", "worst_trade", "bad10_rate", "return", "mean_ret", "worst_ret"}
    full = summary[summary["window"].eq("full")].sort_values(["profile", "cost_bps"])
    annual_focus = annual[annual["profile"].isin(["same_close", "next_open_all", "limit_down_delay_next_open"])].sort_values(["profile", "cost_bps", "year"])
    lines = [
        "# G3 强势链路 Confirm D3 执行压力测试 V1",
        "",
        "## 口径",
        "",
        "- 样本：`core_recovery_volume5 + confirm_d3_le0 + slot5_20pct_daily2`。",
        "- 成本压力：30bps、50bps、100bps。",
        "- 执行压力：`same_close`、全部延迟到次日开盘 `next_open_all`、近似跌停不可卖延迟 `limit_down_delay_next_open`。",
        "- 跌停近似：若退出日 `daily high <= prev_close * 0.905`，认为当日不可卖，延迟到下一交易日开盘。",
        "- 当前仍是日线执行近似，未模拟排队和逐笔流动性。",
        "",
        "## Full 窗口",
        "",
        _md_table(full, pct_cols=pct_cols),
        "",
        "## 年度焦点",
        "",
        _md_table(annual_focus, pct_cols=pct_cols),
        "",
        "## 诊断",
        "",
        _md_table(diagnostics, pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 若次日开盘压力仍能保持正收益和可控回撤，D3 退出具备继续正式化价值。",
        "- 若压力后收益主要仍来自 2025/2026，强势链路只能作为 shadow，不进入正式组合。",
        "- 后续还需要加入等待期减仓，而不是直接全仓持有到 D3。",
        "",
    ]
    (OUT_DIR / "confirm_d3_execution_stress_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    profiles = ["same_close", "next_open_all", "limit_down_delay_next_open"]
    cost_list = [30.0, 50.0, 100.0]
    summaries: list[dict] = []
    annual_parts: list[pd.DataFrame] = []
    diag_rows: list[dict] = []
    for cost_bps in cost_list:
        base = _prepare_base(cost_bps)
        confirm = _apply_policy(base, "confirm_d3_le0", cost_bps)
        daily = _load_daily_prices(confirm)
        for profile in profiles:
            candidates = _apply_execution_variant(confirm, daily, profile, cost_bps)
            candidates.to_csv(OUT_DIR / f"{profile}_{int(cost_bps)}bps_candidates.csv", index=False, encoding="utf-8-sig")
            diag_rows.append(_diagnostics(candidates, profile, cost_bps))
            closed, curve = _simulate(candidates, f"confirm_d3_{profile}", slots=5, slot_pct=0.20, daily_open_limit=2)
            closed.to_csv(OUT_DIR / f"{profile}_{int(cost_bps)}bps_closed_trades.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(OUT_DIR / f"{profile}_{int(cost_bps)}bps_curve.csv", index=False, encoding="utf-8-sig")
            annual_parts.append(_annual(profile, cost_bps, closed, curve))
            for window in ["train_2020_2023", "valid_2024_2025", "blind_2026ytd", "full"]:
                summaries.append(_metrics(closed, curve, window, profile, cost_bps))

    summary = pd.DataFrame(summaries)
    annual = pd.concat(annual_parts, ignore_index=True) if annual_parts else pd.DataFrame()
    diagnostics = pd.DataFrame(diag_rows)
    summary.to_csv(OUT_DIR / "confirm_d3_execution_stress_summary_raw.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "confirm_d3_execution_stress_annual_raw.csv", index=False, encoding="utf-8-sig")
    diagnostics.to_csv(OUT_DIR / "confirm_d3_execution_stress_diagnostics.csv", index=False, encoding="utf-8-sig")
    _write_report(summary, annual, diagnostics)
    full = summary[summary["window"].eq("full")].sort_values(["profile", "cost_bps"])
    print(
        json.dumps(
            {
                "out_dir": str(OUT_DIR),
                "full": full[["profile", "cost_bps", "closed", "early_exits", "total_ret", "max_drawdown", "win_rate", "mean_trade_ret", "worst_trade", "bad10_rate"]].to_dict(orient="records"),
                "diagnostics": diag_rows,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
