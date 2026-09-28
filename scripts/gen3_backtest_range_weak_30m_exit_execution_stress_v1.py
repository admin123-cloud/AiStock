from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_range_weak_30m_exit_mtm_v1 import (
    INITIAL_CAPITAL,
    _annual,
    _combine_50_50,
    _load_source,
    _md_table,
    _metrics,
    _simulate_mtm,
    _sql_literal,
)
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar
from utils.market_warehouse import clickhouse_query_df


OUT_DIR = ROOT / "reports" / "gen3_range_weak_30m_exit_execution_stress_v1"
BASE_POLICIES = {
    "range_stress_top1": "m30_close_m8_exit",
    "weak_low_top1": "m30_close_m5_exit",
}
STRESS_SPECS = [
    {"combo": "fixed_30bps", "mode": "fixed", "cost_bps": 30.0, "tail_nextopen": False, "limit_delay": False},
    {"combo": "base_barclose_30bps", "mode": "m30", "cost_bps": 30.0, "tail_nextopen": False, "limit_delay": False},
    {"combo": "barclose_50bps", "mode": "m30", "cost_bps": 50.0, "tail_nextopen": False, "limit_delay": False},
    {"combo": "barclose_100bps", "mode": "m30", "cost_bps": 100.0, "tail_nextopen": False, "limit_delay": False},
    {"combo": "tail_nextopen_30bps", "mode": "m30", "cost_bps": 30.0, "tail_nextopen": True, "limit_delay": False},
    {"combo": "limitdown_delay_30bps", "mode": "m30", "cost_bps": 30.0, "tail_nextopen": False, "limit_delay": True},
    {"combo": "tail_nextopen_limitdown_30bps", "mode": "m30", "cost_bps": 30.0, "tail_nextopen": True, "limit_delay": True},
]


def _pct(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _is_tail_datetime(value: object) -> bool:
    if pd.isna(value):
        return False
    dt = pd.Timestamp(value)
    return (dt.hour, dt.minute) >= (14, 30)


def _limit_pct(code: str) -> float:
    code = str(code)
    if code.startswith(("300", "301", "688", "689", "8", "4", "920")):
        return 0.20
    return 0.10


def _load_daily_ohlc(candidates: pd.DataFrame, extra_days: int = 15) -> pd.DataFrame:
    codes = sorted({str(c) for c in candidates["code"].dropna().tolist() if re.fullmatch(r"[0-9A-Z.]+", str(c))})
    if not codes:
        return pd.DataFrame()
    start = pd.Timestamp(candidates["entry_date"].min()).strftime("%Y-%m-%d")
    end = (pd.Timestamp(candidates["policy_exit_date"].max()) + pd.Timedelta(days=extra_days)).strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 500):
        quoted = ",".join(_sql_literal(c) for c in codes[i : i + 500])
        sql = f"""
        SELECT code, trade_date, open, close
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
    for col in ["open", "close"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["code", "trade_date", "open", "close"]).sort_values(["code", "trade_date"])
    d["pre_close"] = d.groupby("code")["close"].shift(1)
    d["limit_pct"] = d["code"].map(_limit_pct)
    d["limit_down_price"] = d["pre_close"] * (1.0 - d["limit_pct"])
    d["close_limitdown_proxy"] = d["pre_close"].notna() & (d["close"] <= d["limit_down_price"] * 1.003)
    d["open_limitdown_proxy"] = d["pre_close"].notna() & (d["open"] <= d["limit_down_price"] * 1.003)
    d["open_tradable_proxy"] = ~d["open_limitdown_proxy"]
    return d


def _first_calendar_after(calendar: list[pd.Timestamp], day: pd.Timestamp) -> pd.Timestamp | None:
    day = pd.Timestamp(day).normalize()
    for item in calendar:
        if item > day:
            return item
    return None


def _first_tradable_open_after(
    code: str,
    start_day: pd.Timestamp,
    calendar: list[pd.Timestamp],
    daily_map: dict[tuple[str, pd.Timestamp], dict],
) -> tuple[pd.Timestamp | None, float | None, int, bool]:
    start_day = pd.Timestamp(start_day).normalize()
    start_seen = False
    delay = 0
    saw_limit = False
    for day in calendar:
        if day < start_day:
            continue
        if not start_seen:
            start_seen = True
        else:
            delay += 1
        row = daily_map.get((str(code), day))
        if row is None:
            continue
        if bool(row.get("open_tradable_proxy", True)):
            return day, float(row["open"]), delay, saw_limit
        saw_limit = True
    return None, None, delay, saw_limit


def _prepare_candidates(source: pd.DataFrame, spec: dict, daily: pd.DataFrame, calendar: list[pd.Timestamp]) -> pd.DataFrame:
    combo = str(spec["combo"])
    cost_bps = float(spec["cost_bps"])
    daily_map = {
        (str(r.code), pd.Timestamp(r.trade_date).normalize()): r._asdict()
        for r in daily.itertuples(index=False)
    }
    parts = []
    for book, policy in BASE_POLICIES.items():
        d = source[source["book"].eq(book)].copy()
        d["combo"] = combo
        d["exec_policy"] = "fixed" if spec["mode"] == "fixed" else policy
        d["fixed_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
        d["fixed_net_ret_30bps"] = pd.to_numeric(d["net_ret"], errors="coerce")
        d["fixed_gross_ret"] = d["fixed_net_ret_30bps"] + 30.0 / 10000.0
        d["exec_hit"] = False
        d["stress_exit_source"] = "fixed"
        d["execution_delay_days"] = 0
        d["limitdown_delayed"] = False
        d["tail_nextopen_applied"] = False
        if spec["mode"] == "fixed":
            d["policy_exit_date"] = d["fixed_exit_date"]
            d["net_ret"] = d["fixed_gross_ret"] - cost_bps / 10000.0
            parts.append(d)
            continue

        hit_col = f"{policy}_hit"
        dt_col = f"{policy}_datetime"
        ret_col = f"{policy}_net_ret"
        hit = d[hit_col].astype(str).str.lower().eq("true")
        d["exec_hit"] = hit
        d.loc[~hit, "policy_exit_date"] = d.loc[~hit, "fixed_exit_date"]
        d.loc[~hit, "net_ret"] = d.loc[~hit, "fixed_gross_ret"] - cost_bps / 10000.0
        d.loc[hit, "policy_exit_date"] = pd.to_datetime(d.loc[hit, dt_col], errors="coerce").dt.normalize()
        d.loc[hit, "net_ret"] = pd.to_numeric(d.loc[hit, ret_col], errors="coerce") + 30.0 / 10000.0 - cost_bps / 10000.0
        d.loc[hit, "stress_exit_source"] = "trigger_bar_close"

        for idx, row in d[hit].iterrows():
            trigger_dt = pd.Timestamp(row[dt_col])
            trigger_day = trigger_dt.normalize()
            code = str(row["code"])
            use_next_open = bool(spec["tail_nextopen"]) and _is_tail_datetime(trigger_dt)
            trigger_daily = daily_map.get((code, trigger_day), {})
            must_delay = bool(spec["limit_delay"]) and bool(trigger_daily.get("close_limitdown_proxy", False))
            if use_next_open:
                next_day = _first_calendar_after(calendar, trigger_day)
                if next_day is None:
                    continue
                exit_day, exit_open, delay, saw_limit = _first_tradable_open_after(code, next_day, calendar, daily_map)
                if exit_day is None or exit_open is None:
                    continue
                d.at[idx, "policy_exit_date"] = exit_day
                d.at[idx, "net_ret"] = exit_open / float(row["entry_price"]) - 1.0 - cost_bps / 10000.0
                d.at[idx, "stress_exit_source"] = "tail_next_trade_open"
                d.at[idx, "execution_delay_days"] = delay + 1
                d.at[idx, "tail_nextopen_applied"] = True
                d.at[idx, "limitdown_delayed"] = saw_limit
                continue
            if must_delay:
                next_day = _first_calendar_after(calendar, trigger_day)
                if next_day is None:
                    continue
                exit_day, exit_open, delay, saw_limit = _first_tradable_open_after(code, next_day, calendar, daily_map)
                if exit_day is None or exit_open is None:
                    continue
                d.at[idx, "policy_exit_date"] = exit_day
                d.at[idx, "net_ret"] = exit_open / float(row["entry_price"]) - 1.0 - cost_bps / 10000.0
                d.at[idx, "stress_exit_source"] = "limitdown_delayed_open"
                d.at[idx, "execution_delay_days"] = delay + 1
                d.at[idx, "limitdown_delayed"] = True or saw_limit
        d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
        d["net_ret"] = pd.to_numeric(d["net_ret"], errors="coerce")
        parts.append(d.dropna(subset=["policy_exit_date", "net_ret"]))
    return pd.concat(parts, ignore_index=True).sort_values(["entry_date", "book", "candidate_score"], ascending=[True, True, False])


def _write_report(summary: pd.DataFrame, annual: pd.DataFrame, diagnostics: pd.DataFrame) -> None:
    pct_cols = {
        "total_ret",
        "max_drawdown",
        "worst_open_mtm_ret",
        "early_exit_rate",
        "win_rate",
        "mean_trade_ret",
        "median_trade_ret",
        "worst_trade",
        "bad10_rate",
        "return",
        "tail_nextopen_rate",
        "limitdown_delay_rate",
    }
    full = summary[summary["book"].eq("range_weak_50_50")].sort_values("combo")
    annual_combo = annual[annual["book"].eq("range_weak_50_50")].sort_values(["combo", "year"])
    lines = [
        "# G3 Range/Weak 30m 执行压力测试 V1",
        "",
        "## 口径",
        "",
        "- 基准规则：`range_stress_top1` 使用 `30m close <= -8%`，`weak_low_top1` 使用 `30m close <= -5%`。",
        "- 压力项：30/50/100bps 成本，尾盘触发改为次一交易日开盘，触发日跌停不可卖则延迟到下一次可开盘成交。",
        "- 跌停不可卖为日线代理：按代码板块估算 10%/20% 跌停，若触发日收盘贴近跌停则不按当根 30m 收盘成交。",
        "- 该步骤仍为 shadow 执行审计，不进入正式 G3。",
        "",
        "## 50/50 Full 对照",
        "",
        _md_table(full, pct_cols=pct_cols),
        "",
        "## 执行触发诊断",
        "",
        _md_table(diagnostics.sort_values(["combo", "book"]), pct_cols=pct_cols),
        "",
        "## 50/50 年度",
        "",
        _md_table(annual_combo, pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 如果 `barclose_100bps` 仍接近基准，说明这套 30m 保护不是靠极低摩擦才成立。",
        "- 如果 `tail_nextopen` 明显恶化，实盘必须把尾盘触发单独标记，不能默认按当根收盘可成交。",
        "- 如果 `limitdown_delay` 与 base 接近，说明主要尾部并非来自连续跌停不可卖；若明显恶化，则 range/weak 仍只能影子观察。",
        "",
    ]
    (OUT_DIR / "range_weak_30m_execution_stress_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    source = _load_source()
    source["policy_exit_date"] = pd.to_datetime(source["policy_exit_date"], errors="coerce").dt.normalize()
    calendar = _trade_calendar(source["entry_date"].min(), source["policy_exit_date"].max() + pd.Timedelta(days=20))
    daily_seed = source.copy()
    daily_seed["policy_exit_date"] = daily_seed["policy_exit_date"] + pd.Timedelta(days=20)
    daily = _load_daily_ohlc(daily_seed)
    summary_rows = []
    annual_parts = []
    diag_rows = []
    for spec in STRESS_SPECS:
        combo = str(spec["combo"])
        candidates = _prepare_candidates(source, spec, daily, calendar)
        candidates.to_csv(OUT_DIR / f"{combo}_candidates.csv", index=False, encoding="utf-8-sig")
        curves = {}
        closed_parts = []
        for book, part in candidates.groupby("book"):
            closed, curve = _simulate_mtm(part)
            stem = f"{combo}_{book}"
            closed.to_csv(OUT_DIR / f"{stem}_closed_trades.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(OUT_DIR / f"{stem}_mtm_curve.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(_metrics(combo, str(book), curve, closed))
            annual_parts.append(_annual(combo, str(book), curve, closed))
            curves[str(book)] = curve
            closed_parts.append(closed.assign(source_book=book) if not closed.empty else closed)
            diag_rows.append(
                {
                    "combo": combo,
                    "book": str(book),
                    "rows": int(len(part)),
                    "exec_hits": int(part["exec_hit"].astype(bool).sum()),
                    "tail_nextopen": int(part["tail_nextopen_applied"].astype(bool).sum()),
                    "tail_nextopen_rate": float(part["tail_nextopen_applied"].astype(bool).mean()),
                    "limitdown_delay": int(part["limitdown_delayed"].astype(bool).sum()),
                    "limitdown_delay_rate": float(part["limitdown_delayed"].astype(bool).mean()),
                    "mean_delay_days": float(pd.to_numeric(part["execution_delay_days"], errors="coerce").mean()),
                    "max_delay_days": int(pd.to_numeric(part["execution_delay_days"], errors="coerce").max()),
                }
            )
        combo_curve = _combine_50_50(curves, combo)
        combo_closed = pd.concat(closed_parts, ignore_index=True) if closed_parts else pd.DataFrame()
        combo_curve.to_csv(OUT_DIR / f"{combo}_range_weak_50_50_mtm_curve.csv", index=False, encoding="utf-8-sig")
        combo_closed.to_csv(OUT_DIR / f"{combo}_range_weak_50_50_closed_trades.csv", index=False, encoding="utf-8-sig")
        summary_rows.append(_metrics(combo, "range_weak_50_50", combo_curve, combo_closed))
        annual_parts.append(_annual(combo, "range_weak_50_50", combo_curve, combo_closed))

    summary = pd.DataFrame(summary_rows)
    annual = pd.concat(annual_parts, ignore_index=True)
    diagnostics = pd.DataFrame(diag_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual.csv", index=False, encoding="utf-8-sig")
    diagnostics.to_csv(OUT_DIR / "diagnostics.csv", index=False, encoding="utf-8-sig")
    _write_report(summary, annual, diagnostics)
    print(
        json.dumps(
            {
                "out_dir": str(OUT_DIR),
                "summary": summary[summary["book"].eq("range_weak_50_50")].to_dict(orient="records"),
                "diagnostics": diagnostics.to_dict(orient="records"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
