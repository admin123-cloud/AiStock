from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "reports" / "gen2_breakout_buy_point_research" / "breakout_family_intraday_strength_probe"
COMBO = PROBE / "combo_policy_probe"
OUT = COMBO / "diagnosis_2026ytd"


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    return str(obj)


def _pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _read_trade_lots(run_dir: Path) -> pd.DataFrame:
    trades = pd.read_csv(run_dir / "trades.csv")
    signals = pd.read_csv(run_dir / "signals.csv")
    if trades.empty:
        return pd.DataFrame()
    for col in ["buy_date", "sell_date"]:
        if col in trades.columns:
            trades[col] = pd.to_datetime(trades[col], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["entry_date", "trade_date"]:
        if col in signals.columns:
            signals[col] = pd.to_datetime(signals[col], errors="coerce").dt.strftime("%Y-%m-%d")
    trades["buy_datetime"] = pd.to_datetime(trades["buy_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    signals["confirm_datetime"] = pd.to_datetime(signals["confirm_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    source_cols = [
        "entry_date",
        "code",
        "confirm_datetime",
        "source_family",
        "source",
        "v4_rank",
        "v4_score",
        "rt_return_from_d1_close",
        "rt_breakout_vs_box_top",
        "volume_ratio",
    ]
    available = [c for c in source_cols if c in signals.columns]
    signal_meta = signals[available].drop_duplicates(["entry_date", "code", "confirm_datetime"], keep="first")
    merged = trades.merge(
        signal_meta,
        left_on=["buy_date", "code", "buy_datetime"],
        right_on=["entry_date", "code", "confirm_datetime"],
        how="left",
        suffixes=("", "_signal"),
    )
    if "source_family" not in merged.columns:
        merged["source_family"] = "volume5"
    merged["source_family"] = merged["source_family"].fillna("volume5")
    group_cols = ["buy_date", "code", "name", "buy_datetime", "source_family"]
    rows = []
    for key, g in merged.groupby(group_cols, dropna=False):
        first = g.iloc[0]
        capital = float(first["capital"]) if "capital" in g.columns and pd.notna(first["capital"]) else np.nan
        pnl = float(g["pnl"].sum())
        rows.append(
            {
                "buy_date": key[0],
                "code": key[1],
                "name": key[2],
                "buy_datetime": key[3],
                "source_family": key[4],
                "pnl": pnl,
                "return": pnl / capital if capital and np.isfinite(capital) else np.nan,
                "capital": capital,
                "exit_rows": int(len(g)),
                "had_stop": bool((g["exit_reason"].astype(str) == "stop_loss_30m").any()) if "exit_reason" in g.columns else False,
                "v4_rank": int(first["v4_rank"]) if "v4_rank" in g.columns and pd.notna(first["v4_rank"]) else None,
                "v4_score": float(first["v4_score"]) if "v4_score" in g.columns and pd.notna(first["v4_score"]) else None,
                "rt_return_from_d1_close": float(first["rt_return_from_d1_close"])
                if "rt_return_from_d1_close" in g.columns and pd.notna(first["rt_return_from_d1_close"])
                else None,
                "rt_breakout_vs_box_top": float(first["rt_breakout_vs_box_top"])
                if "rt_breakout_vs_box_top" in g.columns and pd.notna(first["rt_breakout_vs_box_top"])
                else None,
                "volume_ratio": float(first["volume_ratio"]) if "volume_ratio" in g.columns and pd.notna(first["volume_ratio"]) else None,
            }
        )
    return pd.DataFrame(rows)


def _summary(df: pd.DataFrame, group_col: str | None = None) -> list[dict[str, Any]]:
    if df.empty:
        return []
    groups = [(None, df)] if group_col is None else list(df.groupby(group_col, dropna=False, observed=False))
    rows = []
    for name, g in groups:
        rows.append(
            {
                "group": "all" if group_col is None else str(name),
                "lots": int(len(g)),
                "pnl": float(g["pnl"].sum()),
                "avg_return": float(g["return"].mean()),
                "win_rate": float((g["return"] > 0).mean()),
                "stop_ratio": float(g["had_stop"].mean()) if "had_stop" in g.columns else None,
                "median_rank": float(g["v4_rank"].median()) if "v4_rank" in g.columns else None,
            }
        )
    return rows


def _bin_summary(df: pd.DataFrame, col: str, bins: list[float], labels: list[str]) -> list[dict[str, Any]]:
    if df.empty or col not in df.columns:
        return []
    d = df.dropna(subset=[col]).copy()
    if d.empty:
        return []
    d["bin"] = pd.cut(d[col], bins=bins, labels=labels, include_lowest=True)
    return _summary(d, "bin")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    combo_dir = COMBO / "segment_runs" / "rtret60_or_breakbox25" / "stop_cd3_skip" / "2026YTD"
    strict_dir = COMBO / "segment_runs" / "rtret70_and_breakbox35" / "stop_cd3_skip" / "2026YTD"
    volume_dir = PROBE / "segment_runs" / "volume5_baseline" / "blind"

    combo = _read_trade_lots(combo_dir)
    strict = _read_trade_lots(strict_dir)
    volume = _read_trade_lots(volume_dir)
    combo.to_csv(OUT / "combo_rtret60_or_breakbox25_lots.csv", index=False, encoding="utf-8-sig")
    strict.to_csv(OUT / "strict_rtret70_and_breakbox35_lots.csv", index=False, encoding="utf-8-sig")
    volume.to_csv(OUT / "volume5_baseline_lots.csv", index=False, encoding="utf-8-sig")

    combo_keys = set(zip(combo["buy_date"], combo["code"])) if not combo.empty else set()
    volume_keys = set(zip(volume["buy_date"], volume["code"])) if not volume.empty else set()
    displaced_volume5 = volume[[key not in combo_keys for key in zip(volume["buy_date"], volume["code"])]].copy()
    added_combo = combo[[key not in volume_keys for key in zip(combo["buy_date"], combo["code"])]].copy()
    displaced_volume5.to_csv(OUT / "volume5_lots_not_bought_by_combo.csv", index=False, encoding="utf-8-sig")
    added_combo.to_csv(OUT / "combo_lots_not_in_volume5.csv", index=False, encoding="utf-8-sig")

    payload = {
        "combo_summary": _summary(combo),
        "combo_by_source": _summary(combo, "source_family"),
        "strict_summary": _summary(strict),
        "strict_by_source": _summary(strict, "source_family"),
        "volume5_summary": _summary(volume),
        "added_combo_summary": _summary(added_combo, "source_family"),
        "displaced_volume5_summary": _summary(displaced_volume5),
        "combo_confirm_hour": _summary(combo.assign(confirm_hour=pd.to_datetime(combo["buy_datetime"]).dt.hour), "confirm_hour")
        if not combo.empty
        else [],
        "combo_bigbull_rtret_bins": _bin_summary(
            combo[combo["source_family"] == "big_bull"].copy(),
            "rt_return_from_d1_close",
            [-np.inf, 0.06, 0.07, 0.08, np.inf],
            ["<=6%", "6-7%", "7-8%", ">8%"],
        ),
        "combo_bigbull_breakbox_bins": _bin_summary(
            combo[combo["source_family"] == "big_bull"].copy(),
            "rt_breakout_vs_box_top",
            [-np.inf, 0.025, 0.04, 0.06, np.inf],
            ["<=2.5%", "2.5-4%", "4-6%", ">6%"],
        ),
        "top_combo_losers": combo.sort_values("pnl").head(8).to_dict("records") if not combo.empty else [],
        "top_volume5_winners_missed": displaced_volume5.sort_values("pnl", ascending=False).head(8).to_dict("records")
        if not displaced_volume5.empty
        else [],
    }
    (OUT / "diagnosis_2026ytd.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )

    lines = [
        "# 2026YTD Intraday Combo Diagnosis",
        "",
        "Lot-level summaries aggregate partial exits back to the original buy lot.",
        "",
        "## Main Comparison",
        "",
        "| group | lots | pnl | avg_return | win | stop | median_rank |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for title, rows in [
        ("combo_all", payload["combo_summary"]),
        ("combo_by_source", payload["combo_by_source"]),
        ("strict_all", payload["strict_summary"]),
        ("strict_by_source", payload["strict_by_source"]),
        ("volume5_all", payload["volume5_summary"]),
        ("combo_added", payload["added_combo_summary"]),
        ("volume5_displaced", payload["displaced_volume5_summary"]),
    ]:
        for row in rows:
            lines.append(
                f"| {title}:{row['group']} | {row['lots']} | {row['pnl']:.0f} | {_pct(row['avg_return'])} | "
                f"{_pct(row['win_rate'])} | {_pct(row['stop_ratio'])} | {row['median_rank'] if row['median_rank'] is not None else ''} |"
            )
    lines.extend(["", "## Combo Confirm Hour", "", "| hour | lots | pnl | avg_return | win | stop |", "| --- | ---: | ---: | ---: | ---: | ---: |"])
    for row in payload["combo_confirm_hour"]:
        lines.append(
            f"| {row['group']} | {row['lots']} | {row['pnl']:.0f} | {_pct(row['avg_return'])} | {_pct(row['win_rate'])} | {_pct(row['stop_ratio'])} |"
        )
    lines.extend(["", "## Big-Bull Intraday Bins", "", "| factor | bin | lots | pnl | avg_return | win | stop |", "| --- | --- | ---: | ---: | ---: | ---: | ---: |"])
    for factor, rows in [("rtret", payload["combo_bigbull_rtret_bins"]), ("breakbox", payload["combo_bigbull_breakbox_bins"])]:
        for row in rows:
            lines.append(
                f"| {factor} | {row['group']} | {row['lots']} | {row['pnl']:.0f} | {_pct(row['avg_return'])} | {_pct(row['win_rate'])} | {_pct(row['stop_ratio'])} |"
            )
    (OUT / "diagnosis_2026ytd.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
