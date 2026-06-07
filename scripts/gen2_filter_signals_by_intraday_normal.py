from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from api.gen2_strategy import GEN2_OPEN_RULE_V1  # noqa: E402
from scripts.gen2_backtest_open_v1_portfolio import _json_default  # noqa: E402
from scripts.gen2_runtime_dates import add_end_date_argument, resolve_end_date  # noqa: E402
from utils.market_warehouse import clickhouse_client  # noqa: E402

DEFAULT_INPUT = REPO_ROOT / "reports" / "gen2_30m_fractal_restart_realistic_d1_w2" / "fractal_triggers.parquet"
DEFAULT_STATE_DAILY = REPO_ROOT / "reports" / "gen2_open_state_research_full" / "g2_open_state_daily.csv"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_intraday_normal_signal_filters"

INDEX_CODES = {
    "999999.SH": "shanghai",
    "000905.SH": "csi500",
    "000852.SH": "csi1000",
}


def _load_trading_dates_from_calendar(start_date: str, end_date: str) -> list[str]:
    ch = clickhouse_client()
    rows = ch.query_df(
        f"""
        SELECT trade_date
        FROM trade_calendar
        WHERE is_trading = 1
          AND market = 'SH'
          AND trade_date >= '{start_date}'
          AND trade_date <= '{end_date}'
        ORDER BY trade_date
        """
    )
    if rows.empty:
        return []
    rows["trade_date"] = pd.to_datetime(rows["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return rows["trade_date"].dropna().unique().tolist()


def _load_candidate_signals(path: Path, start_date: str, end_date: str) -> pd.DataFrame:
    df = pd.read_parquet(path)
    rule = GEN2_OPEN_RULE_V1
    d = df[
        (df["pattern"] == rule["pattern"])
        & (df["trigger_type"] == rule["trigger_type"])
    ].copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    d = d[(d["entry_date"] >= start_date) & (d["entry_date"] <= end_date)].copy()
    d = d.dropna(subset=["entry_date", "trade_date", "confirm_datetime", "code"]).copy()
    d["source_g2_open_state"] = d["g2_open_state"].astype(str)
    return d.reset_index(drop=True)


def _load_prev_daily_state(path: Path, start_date: str, end_date: str) -> pd.DataFrame:
    states = pd.read_csv(path) if path.exists() else pd.DataFrame()
    if states.empty:
        base = pd.DataFrame()
    else:
        states["trade_date"] = pd.to_datetime(states["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        states = states.dropna(subset=["trade_date"]).sort_values("trade_date").reset_index(drop=True)
        keep = [
            "trade_date",
            "g2_open_state",
            "breadth_ma20",
            "csi1000_ma20",
            "csi1000_ma20_slope5",
            "csi500_ma20",
            "shanghai_ma20",
            "shanghai_ma20_slope5",
        ]
        d = states[[col for col in keep if col in states.columns]].copy()
        for col in d.columns:
            if col != "trade_date" and col != "g2_open_state":
                d[col] = pd.to_numeric(d[col], errors="coerce")
        d["entry_date"] = d["trade_date"].shift(-1)
        d = d.dropna(subset=["entry_date"]).copy()
        base = d[(d["entry_date"] >= start_date) & (d["entry_date"] <= end_date)].copy()

    trading_dates = _load_trading_dates_from_calendar(start_date, end_date)
    if trading_dates:
        needed = set(trading_dates)
    else:
        needed = set(pd.date_range(start_date, end_date, freq="B").strftime("%Y-%m-%d"))
    present = set(base.get("entry_date", pd.Series(dtype=str)).dropna().astype(str).tolist())
    missing = sorted(needed - present)
    if missing:
        fallback = _load_prev_daily_state_from_clickhouse(start_date, end_date)
        if not fallback.empty:
            base = pd.concat([base, fallback], ignore_index=True)
            base = base.drop_duplicates(subset=["entry_date"], keep="first")

    if base.empty:
        return base
    base = base[(base["entry_date"] >= start_date) & (base["entry_date"] <= end_date)].copy()
    return base.rename(columns={col: f"prev_{col}" for col in base.columns if col != "entry_date"})


def _load_prev_daily_state_from_clickhouse(start_date: str, end_date: str) -> pd.DataFrame:
    ch = clickhouse_client()
    calendar = ch.query_df(
        f"""
        SELECT trade_date
        FROM trade_calendar
        WHERE is_trading = 1
          AND market = 'SH'
          AND trade_date <= '{end_date}'
        ORDER BY trade_date
        """
    )
    if calendar.empty:
        return pd.DataFrame()
    calendar["trade_date"] = pd.to_datetime(calendar["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    calendar = calendar.dropna(subset=["trade_date"]).drop_duplicates().reset_index(drop=True)
    calendar["entry_date"] = calendar["trade_date"].shift(-1)
    pairs = calendar[(calendar["entry_date"] >= start_date) & (calendar["entry_date"] <= end_date)].copy()
    if pairs.empty:
        return pd.DataFrame()
    prev_dates = sorted(pairs["trade_date"].astype(str).unique().tolist())
    history_start = (datetime.strptime(prev_dates[0], "%Y-%m-%d") - timedelta(days=220)).strftime("%Y-%m-%d")
    history_end = prev_dates[-1]

    index_codes = ", ".join([f"'{code}'" for code in ["999999.SH", "000905.SH", "000852.SH"]])
    index_df = ch.query_df(
        f"""
        SELECT code, trade_date, any(close) AS close
        FROM (SELECT * FROM kline_daily FINAL)
        WHERE code IN ({index_codes})
          AND trade_date BETWEEN '{history_start}' AND '{history_end}'
        GROUP BY code, trade_date
        ORDER BY code, trade_date
        """
    )
    if index_df.empty:
        return pd.DataFrame()
    index_df["trade_date"] = pd.to_datetime(index_df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    index_df["close"] = pd.to_numeric(index_df["close"], errors="coerce")

    feature_parts: list[pd.DataFrame] = []
    prefix_map = {"999999.SH": "shanghai", "000905.SH": "csi500", "000852.SH": "csi1000"}
    for code, group in index_df.groupby("code", sort=False):
        part = group.sort_values("trade_date").copy()
        prefix = prefix_map.get(str(code))
        if not prefix:
            continue
        part[f"{prefix}_ma20"] = part["close"].rolling(20, min_periods=20).mean()
        part[f"{prefix}_ma20_slope5"] = part[f"{prefix}_ma20"] / part[f"{prefix}_ma20"].shift(5) - 1.0
        part[f"{prefix}_above_ma20"] = part["close"] > part[f"{prefix}_ma20"]
        keep_cols = ["trade_date", f"{prefix}_ma20", f"{prefix}_ma20_slope5", f"{prefix}_above_ma20"]
        feature_parts.append(part[keep_cols].copy())
    if not feature_parts:
        return pd.DataFrame()
    features = feature_parts[0]
    for part in feature_parts[1:]:
        features = features.merge(part, on="trade_date", how="outer")

    prev_dates_sql = ", ".join([f"'{date}'" for date in prev_dates])
    breadth = ch.query_df(
        f"""
        WITH daily AS
        (
            SELECT k.code, k.trade_date, any(k.close) AS close
            FROM (SELECT * FROM kline_daily FINAL) AS k
            JOIN (SELECT * FROM stocks FINAL) AS s ON s.code = k.code
            WHERE s.type = 'stock'
              AND coalesce(s.st, 0) = 0
              AND coalesce(s.quit, 0) = 0
              AND k.trade_date BETWEEN '{history_start}' AND '{history_end}'
            GROUP BY k.code, k.trade_date
        ),
        featured AS
        (
            SELECT
                code,
                trade_date,
                close,
                avg(close) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20,
                count() OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS n20
            FROM daily
        )
        SELECT
            trade_date,
            countIf(n20 >= 20) AS stock_count,
            avgIf(close > ma20, n20 >= 20) AS breadth_ma20
        FROM featured
        WHERE trade_date IN ({prev_dates_sql})
        GROUP BY trade_date
        ORDER BY trade_date
        """
    )
    if breadth.empty:
        breadth = pd.DataFrame(columns=["trade_date", "breadth_ma20"])
    breadth["trade_date"] = pd.to_datetime(breadth["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    breadth["breadth_ma20"] = pd.to_numeric(breadth.get("breadth_ma20"), errors="coerce")

    d = features.merge(breadth[["trade_date", "breadth_ma20"]], on="trade_date", how="left")
    d = pairs[["trade_date", "entry_date"]].merge(d, on="trade_date", how="left")
    active_above = d["csi1000_above_ma20"].fillna(False).astype(bool)
    active_slope_up = pd.to_numeric(d["csi1000_ma20_slope5"], errors="coerce").fillna(0.0) > 0
    confirm_above = d["csi500_above_ma20"].fillna(False).astype(bool)
    risk_above = d["shanghai_above_ma20"].fillna(False).astype(bool)
    risk_slope_up = pd.to_numeric(d["shanghai_ma20_slope5"], errors="coerce").fillna(0.0) > 0
    breadth_value = pd.to_numeric(d["breadth_ma20"], errors="coerce")
    breadth_improve3 = breadth_value.diff().gt(0).rolling(3, min_periods=3).sum().eq(3)
    risk_ok = risk_above | risk_slope_up | breadth_value.ge(0.55)
    off = ((~active_above) & breadth_value.lt(0.40)) | ((~active_above) & (~risk_above))
    normal = active_above & (active_slope_up | confirm_above) & breadth_value.ge(0.45) & risk_ok
    probe = (
        (active_above & breadth_value.ge(0.30) & breadth_value.lt(0.45))
        | (active_above & (~active_slope_up))
        | ((~active_above) & confirm_above & breadth_improve3.fillna(False))
    )
    d["g2_open_state"] = np.select([off, normal, probe], ["OFF", "NORMAL", "PROBE"], default="OFF")
    keep = [
        "trade_date",
        "g2_open_state",
        "breadth_ma20",
        "csi1000_ma20",
        "csi1000_ma20_slope5",
        "csi500_ma20",
        "shanghai_ma20",
        "shanghai_ma20_slope5",
        "entry_date",
    ]
    return d[[col for col in keep if col in d.columns]].copy()


def _load_index_minutes(period: int, dates: list[str]) -> pd.DataFrame:
    if not dates:
        return pd.DataFrame()
    table = {15: "kline_minute_15", 30: "kline_minute_30"}[int(period)]
    quoted_codes = ", ".join([f"'{code}'" for code in INDEX_CODES])
    quoted_dates = ", ".join([f"'{date}'" for date in sorted(set(dates))])
    ch = clickhouse_client()
    df = ch.query_df(
        f"""
        SELECT code, datetime, close
        FROM {table}
        WHERE code IN ({quoted_codes})
          AND toDate(datetime) IN ({quoted_dates})
        ORDER BY datetime, code
        """
    )
    if df.empty:
        try:
            from scripts.collect_intraday_snapshots import load_snapshot_minute_bars

            snap = load_snapshot_minute_bars(
                codes=list(INDEX_CODES.keys()),
                start_date=min(dates),
                end_date=max(dates),
                period_minutes=int(period),
                asset_type="index",
            )
            if snap is not None and not snap.empty:
                df = snap[["code", "datetime", "close"]].copy()
        except Exception:
            pass
    if df.empty:
        return df
    df["datetime"] = pd.to_datetime(df["datetime"])
    df["entry_date"] = df["datetime"].dt.strftime("%Y-%m-%d")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    wide = df.pivot_table(index=["entry_date", "datetime"], columns="code", values="close", aggfunc="last").reset_index()
    return wide.rename(columns={code: f"{prefix}_intraday_close" for code, prefix in INDEX_CODES.items()})


def _build_intraday_state(signals: pd.DataFrame, state_daily: Path, period: int) -> pd.DataFrame:
    start_date = str(signals["entry_date"].min())
    end_date = str(signals["entry_date"].max())
    prev_state = _load_prev_daily_state(state_daily, start_date, end_date)
    minutes = _load_index_minutes(period, signals["entry_date"].dropna().astype(str).unique().tolist())
    if minutes.empty:
        return pd.DataFrame()
    d = minutes.merge(prev_state, on="entry_date", how="left")
    active_above = pd.to_numeric(d["csi1000_intraday_close"], errors="coerce") > pd.to_numeric(d["prev_csi1000_ma20"], errors="coerce")
    active_slope_up = pd.to_numeric(d["prev_csi1000_ma20_slope5"], errors="coerce").fillna(0.0) > 0
    confirm_above = pd.to_numeric(d["csi500_intraday_close"], errors="coerce") > pd.to_numeric(d["prev_csi500_ma20"], errors="coerce")
    risk_above = pd.to_numeric(d["shanghai_intraday_close"], errors="coerce") > pd.to_numeric(d["prev_shanghai_ma20"], errors="coerce")
    risk_slope_up = pd.to_numeric(d["prev_shanghai_ma20_slope5"], errors="coerce").fillna(0.0) > 0
    breadth = pd.to_numeric(d["prev_breadth_ma20"], errors="coerce")
    risk_ok = risk_above | risk_slope_up | breadth.ge(0.55)
    d["intraday_normal"] = active_above & (active_slope_up | confirm_above) & breadth.ge(0.45) & risk_ok
    d["intraday_period"] = f"{int(period)}m"
    d["intraday_state_note"] = "uses intraday index closes with previous-day MA/slope/breadth"
    return d


def _filter_with_state(signals: pd.DataFrame, intraday_state: pd.DataFrame, mode: str) -> pd.DataFrame:
    normal = intraday_state[intraday_state["intraday_normal"]].copy()
    if normal.empty:
        out = signals.iloc[0:0].copy()
        out["intraday_normal_mode"] = mode
        return out
    if mode == "any_day":
        first_normal = normal.sort_values("datetime").groupby("entry_date", as_index=False).first()
        allowed = signals.merge(
            first_normal[["entry_date", "datetime", "intraday_period", "intraday_state_note"]].rename(columns={"datetime": "intraday_normal_datetime"}),
            on="entry_date",
            how="inner",
        )
    elif mode == "before_confirm":
        state_cols = ["entry_date", "datetime", "intraday_period", "intraday_state_note"]
        rows: list[pd.DataFrame] = []
        for date, sig_group in signals.groupby("entry_date", sort=False):
            day_state = normal[normal["entry_date"] == date][state_cols].sort_values("datetime")
            if day_state.empty:
                continue
            for _, sig in sig_group.iterrows():
                seen = day_state[day_state["datetime"] <= sig["confirm_datetime"]]
                if seen.empty:
                    continue
                item = sig.to_frame().T
                first_seen = seen.iloc[0]
                item["intraday_normal_datetime"] = first_seen["datetime"]
                item["intraday_period"] = first_seen["intraday_period"]
                item["intraday_state_note"] = first_seen["intraday_state_note"]
                rows.append(item)
        allowed = pd.concat(rows, ignore_index=True) if rows else signals.iloc[0:0].copy()
    else:
        raise ValueError(f"Unsupported mode: {mode}")

    allowed = allowed.copy()
    allowed["intraday_normal_mode"] = mode
    allowed["g2_open_state"] = "NORMAL"
    return allowed


def _write_output(df: pd.DataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(output_path, index=False)
    df.to_csv(output_path.with_suffix(".csv"), index=False, encoding="utf-8-sig")


def run(input_path: Path, state_daily: Path, output_dir: Path, start_date: str, end_date: str) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    signals = _load_candidate_signals(input_path, start_date, end_date)
    rows: list[dict[str, Any]] = []
    outputs: dict[str, str] = {}
    for period in (15, 30):
        intraday_state = _build_intraday_state(signals, state_daily, period)
        state_path = output_dir / f"intraday_state_{period}m.csv"
        intraday_state.to_csv(state_path, index=False, encoding="utf-8-sig")
        for mode in ("before_confirm", "any_day"):
            filtered = _filter_with_state(signals, intraday_state, mode)
            output_path = output_dir / f"signals_intraday_normal_{period}m_{mode}.parquet"
            _write_output(filtered, output_path)
            key = f"{period}m_{mode}"
            outputs[key] = str(output_path.relative_to(output_dir))
            rows.append(
                {
                    "key": key,
                    "period": f"{period}m",
                    "mode": mode,
                    "signal_count": int(len(filtered)),
                    "unique_codes": int(filtered["code"].nunique()) if not filtered.empty else 0,
                    "source_state_counts": filtered["source_g2_open_state"].value_counts().to_dict() if not filtered.empty else {},
                    "output": str(output_path),
                }
            )
    payload = {
        "schema_version": 1,
        "input": str(input_path),
        "state_daily": str(state_daily),
        "start_date": start_date,
        "end_date": end_date,
        "base_signal_count": int(len(signals)),
        "base_unique_codes": int(signals["code"].nunique()) if not signals.empty else 0,
        "rows": rows,
        "outputs": outputs,
        "notes": [
            "Base signals use D-1 daily candidate context and D intraday trigger.",
            "before_confirm only allows entries after intraday NORMAL has appeared at or before the entry confirmation bar.",
            "any_day allows entries if intraday NORMAL appears at any time during the signal day; this is a loose diagnostic and may still be optimistic.",
            "Intraday NORMAL uses intraday index closes with previous-day MA/slope/breadth to avoid daily close lookahead.",
        ],
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Filter D-1-context G2 signals by signal-day 15m/30m intraday NORMAL.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--state-daily", default=str(DEFAULT_STATE_DAILY))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    add_end_date_argument(parser)
    args = parser.parse_args()
    payload = run(
        input_path=Path(args.input),
        state_daily=Path(args.state_daily),
        output_dir=Path(args.output_dir),
        start_date=str(args.start_date),
        end_date=resolve_end_date(args.end_date),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
