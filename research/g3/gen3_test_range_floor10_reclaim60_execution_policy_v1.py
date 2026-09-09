from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_test_range_30m_volume_acceptance_v1 import INITIAL_CAPITAL, PROFILES, max_drawdown  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


SOURCE = _report_path() / "gen3_range_ice_recent3_threshold_stability_v1" / "floor10_reclaim60__cost30" / "closed_trades.csv"
AUDIT_SOURCE = _report_path() / "gen3_range_floor10_reclaim60_execution_audit_v1" / "audited_trades.csv"
OUT_DIR = _report_path() / "gen3_range_floor10_reclaim60_execution_policy_v1"
SLOTS = 5
SLOT_PCT = 0.20
DAILY_OPEN_LIMIT = 1
WINDOWS = {
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-12-31"),
    "full": ("2020-01-01", "2026-12-31"),
}


POLICIES = [
    {
        "variant": "baseline_hold5",
        "desc": "原始持有5日",
        "skip_late": False,
        "skip_ultra_late": False,
        "ultra_late_d1open_entry": False,
        "d0_weak_d1open_exit": False,
        "d1_gapdown_d1open_exit": False,
        "d1_weak_d2open_exit": False,
        "d2_weak_d3open_exit": False,
    },
    {
        "variant": "d1_weak_d2open_exit",
        "desc": "D1收盘弱确认后，D2开盘退出",
        "skip_late": False,
        "skip_ultra_late": False,
        "ultra_late_d1open_entry": False,
        "d0_weak_d1open_exit": False,
        "d1_gapdown_d1open_exit": False,
        "d1_weak_d2open_exit": True,
        "d2_weak_d3open_exit": False,
    },
    {
        "variant": "d2_weak_d3open_exit",
        "desc": "D2仍弱后，D3开盘退出",
        "skip_late": False,
        "skip_ultra_late": False,
        "ultra_late_d1open_entry": False,
        "d0_weak_d1open_exit": False,
        "d1_gapdown_d1open_exit": False,
        "d1_weak_d2open_exit": False,
        "d2_weak_d3open_exit": True,
    },
    {
        "variant": "d1_or_d2_weak_exit",
        "desc": "D1弱先D2开盘退出，否则D2仍弱D3开盘退出",
        "skip_late": False,
        "skip_ultra_late": False,
        "ultra_late_d1open_entry": False,
        "d0_weak_d1open_exit": False,
        "d1_gapdown_d1open_exit": False,
        "d1_weak_d2open_exit": True,
        "d2_weak_d3open_exit": True,
    },
    {
        "variant": "d1_gapdown_d1open_exit",
        "desc": "D1开盘低开超3%时，D1开盘退出",
        "skip_late": False,
        "skip_ultra_late": False,
        "ultra_late_d1open_entry": False,
        "d0_weak_d1open_exit": False,
        "d1_gapdown_d1open_exit": True,
        "d1_weak_d2open_exit": False,
        "d2_weak_d3open_exit": False,
    },
    {
        "variant": "d0_weak_d1open_exit",
        "desc": "D0收盘弱确认后，D1开盘退出",
        "skip_late": False,
        "skip_ultra_late": False,
        "ultra_late_d1open_entry": False,
        "d0_weak_d1open_exit": True,
        "d1_gapdown_d1open_exit": False,
        "d1_weak_d2open_exit": False,
        "d2_weak_d3open_exit": False,
    },
    {
        "variant": "skip_1500_confirm",
        "desc": "跳过15:00确认信号",
        "skip_late": False,
        "skip_ultra_late": True,
        "ultra_late_d1open_entry": False,
        "d0_weak_d1open_exit": False,
        "d1_gapdown_d1open_exit": False,
        "d1_weak_d2open_exit": False,
        "d2_weak_d3open_exit": False,
    },
    {
        "variant": "skip_1430plus_confirm",
        "desc": "跳过14:30及之后确认信号",
        "skip_late": True,
        "skip_ultra_late": False,
        "ultra_late_d1open_entry": False,
        "d0_weak_d1open_exit": False,
        "d1_gapdown_d1open_exit": False,
        "d1_weak_d2open_exit": False,
        "d2_weak_d3open_exit": False,
    },
    {
        "variant": "confirm1500_to_d1open_entry",
        "desc": "15:00确认按下一交易日开盘买入",
        "skip_late": False,
        "skip_ultra_late": False,
        "ultra_late_d1open_entry": True,
        "d0_weak_d1open_exit": False,
        "d1_gapdown_d1open_exit": False,
        "d1_weak_d2open_exit": False,
        "d2_weak_d3open_exit": False,
    },
]


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows: list[dict[str, Any]] = []
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
    d = pd.read_csv(SOURCE)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    for col in [
        "entry_price_adjusted",
        "fwd_ret_confirm_to_close_1d",
        "fwd_ret_confirm_to_close_2d",
        "fwd_ret_confirm_to_close_3d",
        "fwd_ret_confirm_to_close_5d",
        "candidate_score",
        "amount_ratio3",
        "rank_key",
        "range_pos60",
        "close_position",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    if "rank_key" not in d.columns:
        d["rank_key"] = d["candidate_score"].fillna(0.0) + d["amount_ratio3"].fillna(0.0) * 0.02
    return d.dropna(subset=["code", "entry_date", "confirm_datetime", "entry_price_adjusted", "fwd_ret_confirm_to_close_5d"]).copy()


def load_daily_ohlc(trades: pd.DataFrame) -> pd.DataFrame:
    codes = sorted(trades["code"].astype(str).unique().tolist())
    start = pd.to_datetime(trades["entry_date"]).min().strftime("%Y-%m-%d")
    end = (pd.to_datetime(trades["entry_date"]).max() + pd.Timedelta(days=20)).strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 500):
        batch = codes[i : i + 500]
        quoted = ", ".join(f"'{code}'" for code in batch)
        part = clickhouse_query_df(
            f"""
            SELECT code, trade_date, open, high, close
            FROM kline_daily
            WHERE code IN ({quoted})
              AND trade_date BETWEEN toDate(%(start)s) AND toDate(%(end)s)
            ORDER BY code, trade_date
            """,
            {"start": start, "end": end},
        )
        if not part.empty:
            parts.append(part)
    daily = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if daily.empty:
        return daily
    daily["trade_date"] = pd.to_datetime(daily["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "close"]:
        daily[col] = pd.to_numeric(daily[col], errors="coerce")
    daily = daily.dropna(subset=["code", "trade_date", "open", "high", "close"]).sort_values(["code", "trade_date"]).reset_index(drop=True)
    g = daily.groupby("code", sort=False)
    for offset in [1, 2, 3, 5]:
        daily[f"d{offset}_date"] = g["trade_date"].shift(-offset)
        daily[f"d{offset}_open"] = g["open"].shift(-offset)
        daily[f"d{offset}_close"] = g["close"].shift(-offset)
    return daily.rename(columns={"trade_date": "entry_date"})


def enrich(base: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "code",
        "entry_date",
        "open",
        "high",
        "close",
        "d1_date",
        "d1_open",
        "d1_close",
        "d2_date",
        "d2_open",
        "d2_close",
        "d3_date",
        "d3_open",
        "d3_close",
        "d5_date",
        "d5_close",
    ]
    d = base.merge(daily[cols], on=["code", "entry_date"], how="left")
    d["confirm_time"] = d["confirm_datetime"].dt.strftime("%H:%M")
    d["entry_date_ts"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["d5_date"], errors="coerce")
    d["entry_price_used"] = d["entry_price_adjusted"]
    d["late_confirm"] = d["confirm_datetime"].dt.time >= pd.Timestamp("14:30").time()
    d["ultra_late_confirm"] = d["confirm_datetime"].dt.time >= pd.Timestamp("15:00").time()
    d["d0_confirm_to_close"] = d["close"] / d["entry_price_adjusted"] - 1.0
    d["d1_open_ret"] = d["d1_open"] / d["entry_price_adjusted"] - 1.0
    d["d1_close_ret"] = d["d1_close"] / d["entry_price_adjusted"] - 1.0
    d["d2_open_ret"] = d["d2_open"] / d["entry_price_adjusted"] - 1.0
    d["d2_close_ret"] = d["d2_close"] / d["entry_price_adjusted"] - 1.0
    d["d3_open_ret"] = d["d3_open"] / d["entry_price_adjusted"] - 1.0
    d["d0_weak_close"] = d["d0_confirm_to_close"].le(-0.02)
    d["d1_gapdown_3p"] = d["d1_open_ret"].le(-0.03)
    d["d1_weak_confirm"] = d["d1_close_ret"].le(-0.03)
    d["d2_still_weak"] = d["d2_close_ret"].le(-0.03)
    return d.dropna(subset=["entry_date_ts", "policy_exit_date", "entry_price_used"]).copy()


def load_enriched_base() -> pd.DataFrame:
    if AUDIT_SOURCE.exists():
        d = pd.read_csv(AUDIT_SOURCE)
        d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
        d["entry_date_ts"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
        for col in [
            "entry_price_adjusted",
            "fwd_ret_confirm_to_close_1d",
            "fwd_ret_confirm_to_close_2d",
            "fwd_ret_confirm_to_close_3d",
            "fwd_ret_confirm_to_close_5d",
            "candidate_score",
            "amount_ratio3",
            "rank_key",
            "range_pos60",
            "close_position",
            "open",
            "high",
            "close",
            "d1_open",
            "d1_close",
            "d2_open",
            "d2_close",
            "d3_open",
            "d3_close",
            "d0_confirm_to_close",
            "d1_open_ret",
            "d1_close_ret",
            "d2_open_ret",
            "d2_close_ret",
            "d3_open_ret",
        ]:
            if col in d.columns:
                d[col] = pd.to_numeric(d[col], errors="coerce")
        for col in ["d1_date", "d2_date", "d3_date"]:
            if col in d.columns:
                d[col] = pd.to_datetime(d[col], errors="coerce")
        if "rank_key" not in d.columns:
            d["rank_key"] = d["candidate_score"].fillna(0.0) + d["amount_ratio3"].fillna(0.0) * 0.02
        d["entry_price_used"] = d["entry_price_adjusted"]
        d["d5_close"] = d["entry_price_adjusted"] * (1.0 + d["fwd_ret_confirm_to_close_5d"])
        d["policy_exit_date"] = d["entry_date_ts"] + pd.Timedelta(days=8)
        d["late_confirm"] = d["confirm_datetime"].dt.time >= pd.Timestamp("14:30").time()
        d["ultra_late_confirm"] = d["confirm_datetime"].dt.time >= pd.Timestamp("15:00").time()
        d["d0_weak_close"] = d["d0_confirm_to_close"].le(-0.02)
        d["d1_gapdown_3p"] = d["d1_open_ret"].le(-0.03)
        d["d1_weak_confirm"] = d["d1_close_ret"].le(-0.03)
        d["d2_still_weak"] = d["d2_close_ret"].le(-0.03)
        d["local_calendar_fallback"] = True
        return d.dropna(subset=["entry_date_ts", "policy_exit_date", "entry_price_used", "fwd_ret_confirm_to_close_5d"]).copy()
    base = load_base()
    out = enrich(base, load_daily_ohlc(base))
    out["local_calendar_fallback"] = False
    return out


def local_calendar(start: pd.Timestamp, end: pd.Timestamp, extra: pd.Series) -> list[pd.Timestamp]:
    days = set(pd.bdate_range(pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()).tolist())
    for day in pd.to_datetime(extra, errors="coerce").dropna():
        days.add(pd.Timestamp(day).normalize())
    return sorted(days)


def local_simulate(candidates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if candidates.empty:
        return pd.DataFrame(), pd.DataFrame()
    d = candidates.sort_values(["entry_date_ts", "rank_key"], ascending=[True, False]).copy()
    cal = local_calendar(d["entry_date_ts"].min(), d["policy_exit_date"].max(), pd.concat([d["entry_date_ts"], d["policy_exit_date"]]))
    by_day = {pd.Timestamp(day).normalize(): g.copy() for day, g in d.groupby("entry_date_ts")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []
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
        todays = by_day.get(day)
        opened = 0
        if todays is not None:
            for row in todays.itertuples(index=False):
                if opened >= DAILY_OPEN_LIMIT or len(open_pos) >= SLOTS:
                    continue
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                stake = equity_before * SLOT_PCT
                if stake <= 0 or cash < stake:
                    continue
                pos = row._asdict()
                pos["stake"] = stake
                cash -= stake
                open_pos.append(pos)
                opened += 1
        equity = cash + sum(float(p["stake"]) for p in open_pos)
        curve_rows.append(
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
    curve = pd.DataFrame(curve_rows)
    closed_df = pd.DataFrame(closed)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, closed_df


def local_window_metrics(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, profile: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, (start, end) in WINDOWS.items():
        part = curve[pd.to_datetime(curve["date"]).between(pd.Timestamp(start), pd.Timestamp(end))].copy() if not curve.empty else pd.DataFrame()
        c = closed[pd.to_datetime(closed["entry_date_ts"]).between(pd.Timestamp(start), pd.Timestamp(end))].copy() if not closed.empty else pd.DataFrame()
        net = pd.to_numeric(c.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
        rows.append(
            {
                "variant": variant,
                "profile": profile,
                "window": name,
                "return": float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0) if len(part) else 0.0,
                "max_drawdown": max_drawdown(part["equity"]) if len(part) else 0.0,
                "trade_count": int(len(c)),
                "win_rate": float((net > 0).mean()) if len(net) else 0.0,
            }
        )
    return rows


def apply_policy(base: pd.DataFrame, policy: dict[str, Any], profile: dict[str, Any]) -> pd.DataFrame:
    d = base.copy()
    if bool(policy["skip_late"]):
        d = d[~d["late_confirm"]].copy()
    if bool(policy["skip_ultra_late"]):
        d = d[~d["ultra_late_confirm"]].copy()

    raw = pd.to_numeric(d["fwd_ret_confirm_to_close_5d"], errors="coerce").copy()
    d["exit_reason"] = "hold5"
    d["entry_adjust_reason"] = "confirm_price"

    if bool(policy["ultra_late_d1open_entry"]):
        mask = d["ultra_late_confirm"] & d["d1_open"].notna() & d["d5_close"].notna()
        raw.loc[mask] = d.loc[mask, "d5_close"] / d.loc[mask, "d1_open"] - 1.0
        d.loc[mask, "entry_price_used"] = d.loc[mask, "d1_open"]
        d.loc[mask, "entry_adjust_reason"] = "ultra_late_d1open_entry"

    if bool(policy["d1_gapdown_d1open_exit"]):
        mask = d["d1_gapdown_3p"] & d["d1_open"].notna()
        raw.loc[mask] = d.loc[mask, "d1_open_ret"]
        d.loc[mask, "policy_exit_date"] = pd.to_datetime(d.loc[mask, "d1_date"], errors="coerce")
        d.loc[mask, "exit_reason"] = "d1_gapdown_d1open"

    if bool(policy["d0_weak_d1open_exit"]):
        mask = d["exit_reason"].eq("hold5") & d["d0_weak_close"] & d["d1_open"].notna()
        raw.loc[mask] = d.loc[mask, "d1_open_ret"]
        d.loc[mask, "policy_exit_date"] = pd.to_datetime(d.loc[mask, "d1_date"], errors="coerce")
        d.loc[mask, "exit_reason"] = "d0_weak_d1open"

    if bool(policy["d1_weak_d2open_exit"]):
        mask = d["exit_reason"].eq("hold5") & d["d1_weak_confirm"] & d["d2_open"].notna()
        raw.loc[mask] = d.loc[mask, "d2_open_ret"]
        d.loc[mask, "policy_exit_date"] = pd.to_datetime(d.loc[mask, "d2_date"], errors="coerce")
        d.loc[mask, "exit_reason"] = "d1_weak_d2open"

    if bool(policy["d2_weak_d3open_exit"]):
        mask = d["exit_reason"].eq("hold5") & d["d2_still_weak"] & d["d3_open"].notna()
        raw.loc[mask] = d.loc[mask, "d3_open_ret"]
        d.loc[mask, "policy_exit_date"] = pd.to_datetime(d.loc[mask, "d3_date"], errors="coerce")
        d.loc[mask, "exit_reason"] = "d2_weak_d3open"

    cost = float(profile["cost_bps"]) / 10000.0
    shock = float(profile["shock"])
    d["policy_net_ret"] = raw - cost - shock
    d["variant"] = policy["variant"]
    d["desc"] = policy["desc"]
    return d.dropna(subset=["policy_exit_date", "policy_net_ret", "entry_price_used"]).copy()


def summarize(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, profile: str, desc: str) -> dict[str, Any]:
    net = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    exit_counts = closed.get("exit_reason", pd.Series(dtype=str)).value_counts().to_dict() if not closed.empty else {}
    return {
        "variant": variant,
        "desc": desc,
        "profile": profile,
        "trade_count": int(len(closed)),
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0) if not curve.empty else 0.0,
        "max_drawdown": max_drawdown(curve["equity"]) if not curve.empty else 0.0,
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_trade_return": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "d1open_exit_count": int(exit_counts.get("d1_gapdown_d1open", 0) + exit_counts.get("d0_weak_d1open", 0)),
        "d2open_exit_count": int(exit_counts.get("d1_weak_d2open", 0)),
        "d3open_exit_count": int(exit_counts.get("d2_weak_d3open", 0)),
        "d1open_entry_count": int(closed.get("entry_adjust_reason", pd.Series(dtype=str)).eq("ultra_late_d1open_entry").sum()) if not closed.empty else 0,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_enriched_base()
    base.to_csv(OUT_DIR / "base_enriched.csv", index=False, encoding="utf-8-sig")
    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    for policy in POLICIES:
        for profile in PROFILES:
            candidates = apply_policy(base, policy, profile)
            curve, closed = local_simulate(candidates)
            run_dir = OUT_DIR / f"{policy['variant']}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(summarize(curve, closed, str(policy["variant"]), str(profile["profile"]), str(policy["desc"])))
            window_rows.extend(local_window_metrics(curve, closed, str(policy["variant"]), str(profile["profile"])))

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")

    cost30 = summary[summary["profile"].eq("cost30")].copy()
    cost100 = summary[summary["profile"].eq("cost100")].copy()
    shock = summary[summary["profile"].eq("shock2_cost30")].copy()
    rank = (
        cost30[["variant", "desc", "trade_count", "total_return", "max_drawdown", "win_rate", "worst_trade"]]
        .rename(columns={"total_return": "cost30_return", "max_drawdown": "cost30_max_dd", "win_rate": "cost30_win_rate", "worst_trade": "cost30_worst_trade"})
        .merge(cost100[["variant", "total_return", "max_drawdown"]].rename(columns={"total_return": "cost100_return", "max_drawdown": "cost100_max_dd"}), on="variant")
        .merge(shock[["variant", "total_return", "max_drawdown"]].rename(columns={"total_return": "shock2_return", "max_drawdown": "shock2_max_dd"}), on="variant")
        .sort_values(["shock2_return", "cost100_return", "cost30_return"], ascending=[False, False, False])
    )
    rank.to_csv(OUT_DIR / "policy_rank.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "return",
        "cost30_return",
        "cost30_max_dd",
        "cost30_win_rate",
        "cost30_worst_trade",
        "cost100_return",
        "cost100_max_dd",
        "shock2_return",
        "shock2_max_dd",
    }
    lines = [
        "# G3 floor10_reclaim60 执行政策复验 v1",
        "",
        "## 策略名解释",
        "",
        "- `floor10_reclaim60`：冰点后3日窗口里，箱体位置不高于10%，且日线收盘修复不低于60%的横盘箱体底部30m承接买法。",
        "- `baseline_hold5`：原始持有5个交易日。",
        "- `d1_weak_d2open_exit`：D1收盘已经跌破确认价3%后，D2开盘退出。",
        "- `d2_weak_d3open_exit`：D2收盘仍跌破确认价3%后，D3开盘退出。",
        "- `d1_or_d2_weak_exit`：D1弱先退；D1没触发但D2仍弱，再D3开盘退。",
        "- `d1_gapdown_d1open_exit`：D1开盘已经低开超3%，D1开盘直接退出。",
        "- `d0_weak_d1open_exit`：D0收盘相对确认价跌超2%，D1开盘退出。",
        "- `skip_1500_confirm`：跳过15:00才确认的信号。",
        "- `skip_1430plus_confirm`：跳过14:30及之后才确认的信号。",
        "- `confirm1500_to_d1open_entry`：15:00确认不按当日成交，改按下一交易日开盘买入。",
        "",
        "## 政策排名",
        "",
        md_table(rank, pct_cols=pct_cols),
        "",
        "## 完整结果",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 分窗口结果",
        "",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 判断口径",
        "",
        "- 如果早弱退出提高了压力口径，同时没有明显牺牲 2024-2025 验证窗，才说明值得进入候选策略。",
        "- 如果跳过尾盘确认显著降低收益，说明尾盘信号是收益来源之一，后续应做“15:00转次日开盘成交”压力，而不是粗暴删除。",
        "- 如果 `confirm1500_to_d1open_entry` 仍为正，说明该源对尾盘成交假设不敏感；如果转负，则不能按当前口径接入实盘。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
