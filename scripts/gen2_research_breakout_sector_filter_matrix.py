from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[1]))

import argparse
import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_backtest_risk_cool_dynamic_circuit import _run_dynamic  # noqa: E402
from scripts.gen2_runtime_dates import add_end_date_argument, resolve_end_date, resolve_window_ends  # noqa: E402
from utils.market_warehouse import clickhouse_client  # noqa: E402
from utils.paths import report_path  # noqa: E402


COMBO = report_path(
    "gen2_breakout_buy_point_research",
    "breakout_family_intraday_strength_probe",
    "combo_policy_probe",
)
SOURCE = COMBO / "sources" / "rtret60_or_breakbox25.parquet"
OUT = COMBO / "sector_context_probe" / "sector_filter_matrix"
_MEMBERS_CACHE = pd.DataFrame(columns=["stock_code", "stock_code6", "sector_code", "sector_name", "level", "stock_count"])
from research.common.reporting import timestamp_json_default as _json_default


def _code6(code: Any) -> str:
    return str(code).split(".")[0].zfill(6)


@lru_cache(maxsize=1)
def _ch():
    return clickhouse_client()


def _chunks(items: list[str], size: int = 500) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


@lru_cache(maxsize=4096)
def _members(code6_key: tuple[str, ...] | None = None) -> pd.DataFrame:
    if code6_key is None:
        code6_key = tuple()
    code6_list = list(dict.fromkeys(code6_key))
    if not code6_list:
        return pd.DataFrame(columns=["stock_code", "stock_code6", "sector_code", "sector_name", "level", "stock_count"])
    frames: list[pd.DataFrame] = []
    for chunk in _chunks(code6_list):
        quoted = ", ".join([f"'{code}'" for code in chunk])
        q = f"""
            SELECT ss.stock_code, s.code AS sector_code, s.name AS sector_name, toUInt8(s.level) AS level, s.stock_count
            FROM sector_stocks ss
            INNER JOIN sectors s ON ss.sector_code = s.code
            WHERE s.type = 'industry'
              AND substring(toString(ss.stock_code), 1, 6) IN ({quoted})
        """
        df = _ch().query_df(q)
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["stock_code", "stock_code6", "sector_code", "sector_name", "level", "stock_count"])
    df = pd.concat(frames, ignore_index=True)
    raw_code = df["stock_code"].astype(str)
    df["stock_code6"] = raw_code.map(lambda v: v.split(".")[0].zfill(6))
    df["stock_code"] = raw_code
    df = df.drop_duplicates(["stock_code6", "sector_code", "level"]).reset_index(drop=True)
    df["level"] = pd.to_numeric(df["level"], errors="coerce").astype("Int64")
    return df


@lru_cache(maxsize=4096)
def _sector_intraday(sector_code: str, trade_date: str, confirm_time: str) -> dict[str, Any]:
    members = _MEMBERS_CACHE
    if members.empty:
        return {}
    codes = members.loc[members["sector_code"] == sector_code, "stock_code"].dropna().astype(str).unique().tolist()
    if not codes:
        return {}
    quoted = ", ".join(f"'{code}'" for code in codes)
    dt = f"{trade_date} {confirm_time}:00"
    q = f"""
    SELECT m.code, m.close AS intraday_close, d.close AS prev_close
    FROM
    (
        SELECT code, close
        FROM kline_minute_30
        WHERE code IN ({quoted})
          AND datetime = toDateTime('{dt}')
    ) m
    INNER JOIN
    (
        SELECT code, close
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date = (
              SELECT max(trade_date)
              FROM kline_daily
              WHERE code IN ({quoted}) AND trade_date < toDate('{trade_date}')
          )
    ) d ON m.code = d.code
    """
    df = _ch().query_df(q)
    if df.empty:
        return {}
    df["intraday_close"] = pd.to_numeric(df["intraday_close"], errors="coerce")
    df["prev_close"] = pd.to_numeric(df["prev_close"], errors="coerce")
    df = df[(df["prev_close"] > 0) & df["intraday_close"].notna()].copy()
    if df.empty:
        return {}
    df["rt_ret"] = df["intraday_close"] / df["prev_close"] - 1.0
    return {
        "member_bars": int(len(df)),
        "avg_return": float(df["rt_ret"].mean()),
        "rise_ratio": float((df["rt_ret"] > 0).mean()),
        "strong3_ratio": float((df["rt_ret"] >= 0.03).mean()),
        "strong5_ratio": float((df["rt_ret"] >= 0.05).mean()),
        "top_return": float(df["rt_ret"].max()),
    }


def _enrich_source() -> pd.DataFrame:
    source = pd.read_parquet(SOURCE).copy()
    source["entry_date"] = pd.to_datetime(source["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    source["trade_date"] = pd.to_datetime(source["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    source["confirm_datetime"] = pd.to_datetime(source["confirm_datetime"], errors="coerce")
    source["confirm_time"] = source["confirm_datetime"].dt.strftime("%H:%M")
    source["code6"] = source["code"].map(_code6)

    members = _members(tuple(sorted(set(source["code6"].dropna().astype(str).str.zfill(6))))).sort_values(
        ["stock_code6", "level", "stock_count"], ascending=[True, True, True]
    )
    global _MEMBERS_CACHE
    _MEMBERS_CACHE = members
    sector_map = {
        (r.stock_code6, int(r.level)): r
        for r in members.itertuples(index=False)
        if pd.notna(r.level)
    }

    rows: list[dict[str, Any]] = []
    for row in source.itertuples(index=False):
        out = row._asdict()
        for level in [1, 2, 3]:
            sec = sector_map.get((out["code6"], level))
            prefix = f"l{level}"
            if sec is None:
                out[f"{prefix}_sector_code"] = None
                out[f"{prefix}_sector_name"] = None
                continue
            out[f"{prefix}_sector_code"] = sec.sector_code
            out[f"{prefix}_sector_name"] = sec.sector_name
            out[f"{prefix}_sector_stock_count"] = int(sec.stock_count)
            stats = _sector_intraday(str(sec.sector_code), out["entry_date"], out["confirm_time"])
            for key, value in stats.items():
                out[f"{prefix}_rt_{key}"] = value
        rows.append(out)

    enriched = pd.DataFrame(rows)
    enriched["confirm_datetime"] = pd.to_datetime(enriched["confirm_datetime"], errors="coerce").dt.strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return enriched


def _write_source(name: str, df: pd.DataFrame) -> Path:
    source_dir = OUT / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / f"{name}.parquet"
    drop_cols = ["confirm_time", "code6"]
    out = df.drop(columns=[c for c in drop_cols if c in df.columns]).copy()
    out.to_parquet(path, index=False)
    out.to_csv(path.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    return path


def _empty_summary() -> dict[str, Any]:
    return {
        "trade_count": 0,
        "total_return": 0.0,
        "excess_return": 0.0,
        "max_drawdown": 0.0,
        "win_rate": 0.0,
        "avg_trade_return": 0.0,
    }


def _source_counts(df: pd.DataFrame) -> dict[str, Any]:
    source_family = df["source_family"] if "source_family" in df.columns else pd.Series(dtype=str)
    return {
        "signals": int(len(df)),
        "volume5_signals": int((source_family == "volume5").sum()),
        "bigbull_signals": int((source_family == "big_bull").sum()),
    }


from research.common.reporting import percent_text as _pct


def _num_col(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(-1.0, index=df.index)
    return pd.to_numeric(df[column], errors="coerce").fillna(-1.0)


def _filters(df: pd.DataFrame) -> dict[str, pd.Series]:
    l2_rise = _num_col(df, "l2_rt_rise_ratio")
    l2_s3 = _num_col(df, "l2_rt_strong3_ratio")
    l3_rise = _num_col(df, "l3_rt_rise_ratio")
    l3_s3 = _num_col(df, "l3_rt_strong3_ratio")
    l3_s5 = _num_col(df, "l3_rt_strong5_ratio")
    return {
        "base_combo_or": pd.Series(True, index=df.index),
        "l3_rise_ge_50": l3_rise >= 0.50,
        "l3_rise_ge_60": l3_rise >= 0.60,
        "l3_s3_ge_05": l3_s3 >= 0.05,
        "l3_s3_ge_10": l3_s3 >= 0.10,
        "l3_rise55_s3_ge_05": (l3_rise >= 0.55) & (l3_s3 >= 0.05),
        "l2_rise_ge_60": l2_rise >= 0.60,
        "l2_s3_ge_08": l2_s3 >= 0.08,
        "l2_rise60_s3_ge_08": (l2_rise >= 0.60) & (l2_s3 >= 0.08),
        "l2_or_l3_rise_ge_60": (l2_rise >= 0.60) | (l3_rise >= 0.60),
        "l2_or_l3_s3_ge_10": (l2_s3 >= 0.10) | (l3_s3 >= 0.10),
        "l3_s5_ge_05": l3_s5 >= 0.05,
    }


def run(end_date: str) -> dict[str, Any]:
    OUT.mkdir(parents=True, exist_ok=True)
    enriched = _enrich_source()
    enriched_path = OUT / "sector_enriched_combo_or.parquet"
    enriched.to_parquet(enriched_path, index=False)
    enriched.to_csv(enriched_path.with_suffix(".csv"), index=False, encoding="utf-8-sig")

    rows: list[dict[str, Any]] = []
    windows = resolve_window_ends(end_date)
    for variant, mask in _filters(enriched).items():
        source_df = enriched[mask].copy()
        source_path = _write_source(variant, source_df)
        counts = _source_counts(source_df)
        for window, (start, end) in windows.items():
            run_dir = OUT / "runs" / variant / window
            summary = _empty_summary() if source_df.empty else _run_dynamic(
                source_path, run_dir, "stop_cd3_skip", start, end, sort_mode="trigger_time"
            )
            rows.append(
                {
                    "variant": variant,
                    "window": window,
                    **counts,
                    "trades": summary["trade_count"],
                    "total_return": summary["total_return"],
                    "excess_return": summary["excess_return"],
                    "max_drawdown": summary["max_drawdown"],
                    "win_rate": summary["win_rate"],
                    "avg_trade_return": summary["avg_trade_return"],
                }
            )

    result = pd.DataFrame(rows)
    result.to_csv(OUT / "sector_filter_summary.csv", index=False, encoding="utf-8-sig")
    (OUT / "sector_filter_summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )

    lines = [
        "# Breakout Sector Filter Matrix",
        "",
        "All filters use 30m sector breadth visible at each signal confirmation bar.",
        "",
        "| variant | window | signals | bigbull | volume5 | trades | total | excess | max_dd | win | avg_trade |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['variant']} | {row['window']} | {row['signals']} | {row['bigbull_signals']} | "
            f"{row['volume5_signals']} | {row['trades']} | {_pct(row['total_return'])} | "
            f"{_pct(row['excess_return'])} | {_pct(row['max_drawdown'])} | {_pct(row['win_rate'])} | "
            f"{_pct(row['avg_trade_return'])} |"
        )
    (OUT / "sector_filter_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    payload = {"out": str(OUT), "end_date": end_date, "rows": len(rows), "windows": windows}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh breakout sector filter matrix to the latest trade date.")
    add_end_date_argument(parser)
    args = parser.parse_args()
    run(resolve_end_date(args.end_date))


if __name__ == "__main__":
    main()
