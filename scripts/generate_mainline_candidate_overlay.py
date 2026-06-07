from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.research_main_wave_sector_score import _load_members
from utils.market_warehouse import clickhouse_query_df


OUT_DIR = ROOT / "reports" / "mainline_intraday_candidate_overlay"
MAINLINE_FACTOR = ROOT / "reports" / "mainline_intraday_diffusion_factor" / "mainline_intraday_diffusion_factor.csv"
CANDIDATE_FILES = [
    ("g2_v2_current", ROOT / "reports" / "gen2_v2_live_smoke_current" / "source.csv"),
    ("g3_guarded_live_safe", ROOT / "reports" / "gen3_guarded_live_safe_payload_v1" / "g3_guarded_live_safe_payload.csv"),
    ("g3_live_payload", ROOT / "reports" / "gen3_live_payload_v1" / "g3_live_payload.csv"),
]


def _code6(value: Any) -> str:
    text = str(value or "").strip().upper()
    if "." in text:
        text = text.split(".", 1)[0]
    return text.zfill(6)[-6:]


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, encoding="utf-8-sig")


def _load_mainline(target_date: str | None) -> tuple[str, pd.DataFrame]:
    factor = _read_csv(MAINLINE_FACTOR)
    if factor.empty:
        raise RuntimeError(f"mainline factor is empty or missing: {MAINLINE_FACTOR}")
    date_col = "date_text" if "date_text" in factor.columns else "trade_date"
    factor[date_col] = pd.to_datetime(factor[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
    if target_date is None:
        target_date = str(factor[date_col].dropna().max())
    picked = factor[factor[date_col].eq(target_date)].copy()
    if picked.empty:
        raise RuntimeError(f"no mainline factor rows for target_date={target_date}")
    return target_date, picked


def _load_latest_daily(target_date: str, codes6: list[str]) -> pd.DataFrame:
    if not codes6:
        return pd.DataFrame()
    placeholders = ",".join(["?"] * len(codes6))
    df = clickhouse_query_df(
        f"""
        SELECT
            code,
            trade_date,
            close,
            change_pct,
            amount
        FROM kline_daily
        WHERE trade_date = ?
          AND substring(code, 1, 6) IN ({placeholders})
        """,
        [target_date, *codes6],
    )
    if df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["code6"] = df["code"].map(_code6)
    for col in ["close", "change_pct", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _load_stock_names(codes6: list[str]) -> pd.DataFrame:
    if not codes6:
        return pd.DataFrame(columns=["code6", "stock_name"])
    placeholders = ",".join(["?"] * len(codes6))
    df = clickhouse_query_df(
        f"""
        SELECT code, name
        FROM stocks
        WHERE substring(code, 1, 6) IN ({placeholders})
        """,
        codes6,
    )
    if df.empty:
        return pd.DataFrame(columns=["code6", "stock_name"])
    df = df.copy()
    df["code6"] = df["code"].map(_code6)
    return df[["code6", "name"]].rename(columns={"name": "stock_name"}).drop_duplicates("code6")


def _candidate_overlay(mainline: pd.DataFrame, members: pd.DataFrame, target_date: str, lookback_days: int) -> pd.DataFrame:
    start = (pd.Timestamp(target_date) - pd.Timedelta(days=int(lookback_days))).strftime("%Y-%m-%d")
    frames: list[pd.DataFrame] = []
    main_cols = [
        "sector_code",
        "sector_name",
        "threshold_level",
        "mainline_intraday_score",
        "true_index_window_score",
        "intraday_diffusion_score",
    ]
    member_map = members.merge(mainline[main_cols], on=["sector_code", "sector_name"], how="inner")
    member_map = member_map[["stock_code6", *main_cols]].drop_duplicates(["stock_code6", "sector_code"])

    for source_name, path in CANDIDATE_FILES:
        df = _read_csv(path)
        if df.empty or "code" not in df.columns:
            continue
        date_col = "entry_date" if "entry_date" in df.columns else None
        if date_col is None:
            continue
        df = df.copy()
        df["entry_date_text"] = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
        df = df[(df["entry_date_text"] >= start) & (df["entry_date_text"] <= target_date)].copy()
        if df.empty:
            continue
        df["code6"] = df["code"].map(_code6)
        keep = [
            col
            for col in [
                "code",
                "code6",
                "name",
                "entry_date_text",
                "confirm_datetime",
                "route",
                "strategy_id",
                "score",
                "source_family",
                "signal_family",
                "g2_v2_buy_logic",
                "formal_buy_signal",
                "live_ready",
                "shadow_action",
                "block_reason",
                "l3_rt_strong3_ratio",
                "runup_from_60d_low",
            ]
            if col in df.columns
        ]
        out = df[keep].merge(member_map, left_on="code6", right_on="stock_code6", how="inner")
        if out.empty:
            continue
        out["candidate_source"] = source_name
        frames.append(out)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    sort_cols = [c for c in ["mainline_intraday_score", "score", "l3_rt_strong3_ratio"] if c in out.columns]
    if sort_cols:
        out = out.sort_values(sort_cols, ascending=False)
    return out


def _member_snapshot(mainline: pd.DataFrame, members: pd.DataFrame, target_date: str, top_per_sector: int) -> pd.DataFrame:
    main_cols = [
        "sector_code",
        "sector_name",
        "threshold_level",
        "mainline_intraday_score",
        "morning_rise_ratio",
        "morning_strong2_ratio",
        "morning_ret_avg",
    ]
    rows = members.merge(mainline[main_cols], on=["sector_code", "sector_name"], how="inner")
    codes6 = sorted(rows["stock_code6"].dropna().astype(str).unique().tolist())
    daily = _load_latest_daily(target_date, codes6)
    names = _load_stock_names(codes6)
    rows = rows.merge(daily[["code6", "close", "change_pct", "amount"]] if not daily.empty else pd.DataFrame(columns=["code6", "close", "change_pct", "amount"]), left_on="stock_code6", right_on="code6", how="left")
    rows = rows.merge(names, left_on="stock_code6", right_on="code6", how="left", suffixes=("", "_name"))
    rows["stock_name"] = rows["stock_name"].fillna(rows["stock_code_raw"])
    rows["amount"] = pd.to_numeric(rows["amount"], errors="coerce").fillna(0.0)
    rows["change_pct"] = pd.to_numeric(rows["change_pct"], errors="coerce")
    rows = rows.sort_values(["sector_code", "change_pct", "amount"], ascending=[True, False, False])
    rows["rank_in_sector_by_day"] = rows.groupby("sector_code").cumcount() + 1
    rows = rows[rows["rank_in_sector_by_day"] <= int(top_per_sector)].copy()
    keep = [
        "sector_name",
        "sector_code",
        "threshold_level",
        "mainline_intraday_score",
        "stock_code_raw",
        "stock_code6",
        "stock_name",
        "close",
        "change_pct",
        "amount",
        "rank_in_sector_by_day",
        "morning_rise_ratio",
        "morning_strong2_ratio",
        "morning_ret_avg",
    ]
    return rows[keep].sort_values(["mainline_intraday_score", "sector_code", "rank_in_sector_by_day"], ascending=[False, True, True])


def _write_report(target_date: str, mainline: pd.DataFrame, candidates: pd.DataFrame, snapshot: pd.DataFrame, summary: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    mainline_path = OUT_DIR / "mainline_sectors.csv"
    candidate_path = OUT_DIR / "mainline_candidate_overlay.csv"
    snapshot_path = OUT_DIR / "mainline_member_snapshot.csv"
    summary_path = OUT_DIR / "summary.json"
    report_path = OUT_DIR / "REPORT.md"

    mainline.to_csv(mainline_path, index=False, encoding="utf-8-sig")
    candidates.to_csv(candidate_path, index=False, encoding="utf-8-sig")
    snapshot.to_csv(snapshot_path, index=False, encoding="utf-8-sig")
    payload = {
        **summary,
        "mainline_sectors_csv": str(mainline_path),
        "candidate_overlay_csv": str(candidate_path),
        "member_snapshot_csv": str(snapshot_path),
        "report": str(report_path),
    }
    summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 主线板块候选联动雷达 research_only",
        "",
        f"- 目标日期：`{target_date}`",
        f"- 主线板块数：{len(mainline)}",
        f"- 撞上 G2/G3 候选数：{len(candidates)}",
        f"- 主线成分股快照数：{len(snapshot)}",
        "- 用途：辅助观察主线板块内部哪些股票已有策略语境或盘面承接，不是独立买点。",
        "- 风险：板块成分使用当前 `sector_stocks`，存在历史成员偏差；当前报告只用于当日研究观察。",
        "",
        "## 主线板块",
        "",
        "| 板块 | 阈值 | 主线分 | 上午上涨比例 | 上午强2比例 | 上午均涨 |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for row in mainline.sort_values("mainline_intraday_score", ascending=False).itertuples(index=False):
        lines.append(
            f"| {row.sector_name} `{row.sector_code}` | {row.threshold_level} | "
            f"{float(row.mainline_intraday_score):.2f} | {float(row.morning_rise_ratio):.2%} | "
            f"{float(row.morning_strong2_ratio):.2%} | {float(row.morning_ret_avg):.2%} |"
        )

    lines.extend(["", "## G2/G3 候选撞线", ""])
    if candidates.empty:
        lines.append("- 当前窗口内没有 G2/G3 候选落在这些主线板块里。")
    else:
        lines.extend(["| 来源 | 股票 | 板块 | 日期 | 分数 | 正式买点 | 阻断原因 |", "|---|---|---|---|---:|---|---|"])
        for row in candidates.head(30).itertuples(index=False):
            score = getattr(row, "score", "")
            formal = getattr(row, "formal_buy_signal", "")
            block = getattr(row, "block_reason", "")
            lines.append(
                f"| {row.candidate_source} | {getattr(row, 'name', '')} `{row.code}` | "
                f"{row.sector_name} | {row.entry_date_text} | {score} | {formal} | {block} |"
            )

    lines.extend(["", "## 主线成分股盘面快照 Top", ""])
    if snapshot.empty:
        lines.append("- 无成分股日线快照。")
    else:
        lines.extend(["| 板块 | 股票 | 当日涨幅 | 成交额 | 板块内排名 |", "|---|---|---:|---:|---:|"])
        for row in snapshot.head(40).itertuples(index=False):
            amount_yi = float(row.amount or 0) / 100000000.0
            change = "" if pd.isna(row.change_pct) else f"{float(row.change_pct):.2f}%"
            lines.append(
                f"| {row.sector_name} | {row.stock_name} `{row.stock_code_raw}` | "
                f"{change} | {amount_yi:.2f}亿 | {int(row.rank_in_sector_by_day)} |"
            )

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(str(report_path))
    print(str(candidate_path))
    print(str(snapshot_path))
    print(f"mainline_sector_count={len(mainline)}")
    print(f"candidate_overlay_count={len(candidates)}")
    print(f"member_snapshot_count={len(snapshot)}")
    if not candidates.empty:
        print(candidates.head(20).to_string(index=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Overlay current mainline sectors with G2/G3 candidates.")
    parser.add_argument("--target-date", default=None)
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--top-per-sector", type=int, default=8)
    args = parser.parse_args()

    target_date, mainline = _load_mainline(args.target_date)
    members = _load_members([2], 5)
    candidates = _candidate_overlay(mainline, members, target_date, args.lookback_days)
    snapshot = _member_snapshot(mainline, members, target_date, args.top_per_sector)
    summary = {
        "target_date": target_date,
        "lookback_days": int(args.lookback_days),
        "top_per_sector": int(args.top_per_sector),
        "research_only": True,
        "mainline_sector_count": int(len(mainline)),
        "candidate_overlay_count": int(len(candidates)),
        "member_snapshot_count": int(len(snapshot)),
    }
    _write_report(target_date, mainline, candidates, snapshot, summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
