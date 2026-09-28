from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "reports" / "gen2_breakout_buy_point_research"
PROBE = BASE / "breakout_family_intraday_strength_probe"
COMBO = PROBE / "combo_policy_probe"
DIAG = COMBO / "diagnosis_2026ytd"
OUT = COMBO / "manual_review_2026ytd"


def _read_csv(name: str) -> pd.DataFrame:
    return pd.read_csv(DIAG / name)


def _ret(row: Any) -> float:
    return float(getattr(row, "_8", np.nan))


def _build_tickets() -> pd.DataFrame:
    combo = _read_csv("combo_rtret60_or_breakbox25_lots.csv")
    missed = _read_csv("volume5_lots_not_bought_by_combo.csv")
    added = _read_csv("combo_lots_not_in_volume5.csv")
    rows: list[dict[str, Any]] = []

    stop_focus = combo[(combo["source_family"] == "volume5") & combo["had_stop"].astype(str).str.lower().eq("true")]
    for _, r in stop_focus.iterrows():
        rows.append(
            {
                "review_type": "combo_bought_stop",
                "date": r["buy_date"],
                "code": r["code"],
                "name": r["name"],
                "confirm_time": str(r["buy_datetime"])[11:16],
                "source_family": r["source_family"],
                "return": r["return"],
                "pnl": r["pnl"],
                "v4_rank": r["v4_rank"],
                "volume_ratio": r["volume_ratio"],
                "rt_return": r.get("rt_return_from_d1_close", np.nan),
                "breakbox": r.get("rt_breakout_vs_box_top", np.nan),
                "why_review": "bought by combo, then stop-loss",
                "manual_label": "",
                "visible_clue": "",
            }
        )

    for _, r in missed.iterrows():
        rows.append(
            {
                "review_type": "missed_volume5_winner",
                "date": r["buy_date"],
                "code": r["code"],
                "name": r["name"],
                "confirm_time": str(r["buy_datetime"])[11:16],
                "source_family": "volume5",
                "return": r["return"],
                "pnl": r["pnl"],
                "v4_rank": r["v4_rank"],
                "volume_ratio": r["volume_ratio"],
                "rt_return": np.nan,
                "breakbox": np.nan,
                "why_review": "same-day later volume5 winner not bought by combo",
                "manual_label": "",
                "visible_clue": "",
            }
        )

    for _, r in added[added["source_family"] == "big_bull"].iterrows():
        rows.append(
            {
                "review_type": "added_bigbull_winner",
                "date": r["buy_date"],
                "code": r["code"],
                "name": r["name"],
                "confirm_time": str(r["buy_datetime"])[11:16],
                "source_family": r["source_family"],
                "return": r["return"],
                "pnl": r["pnl"],
                "v4_rank": r["v4_rank"],
                "volume_ratio": r["volume_ratio"],
                "rt_return": r.get("rt_return_from_d1_close", np.nan),
                "breakbox": r.get("rt_breakout_vs_box_top", np.nan),
                "why_review": "new big-bull breakout contribution",
                "manual_label": "",
                "visible_clue": "",
            }
        )

    early_winners = combo[(combo["source_family"] == "volume5") & (combo["return"] > 0.13)]
    for _, r in early_winners.iterrows():
        rows.append(
            {
                "review_type": "early_volume5_winner_control",
                "date": r["buy_date"],
                "code": r["code"],
                "name": r["name"],
                "confirm_time": str(r["buy_datetime"])[11:16],
                "source_family": r["source_family"],
                "return": r["return"],
                "pnl": r["pnl"],
                "v4_rank": r["v4_rank"],
                "volume_ratio": r["volume_ratio"],
                "rt_return": r.get("rt_return_from_d1_close", np.nan),
                "breakbox": r.get("rt_breakout_vs_box_top", np.nan),
                "why_review": "early winner that waiting rules should not kill",
                "manual_label": "",
                "visible_clue": "",
            }
        )

    order = {
        "combo_bought_stop": 0,
        "missed_volume5_winner": 1,
        "added_bigbull_winner": 2,
        "early_volume5_winner_control": 3,
    }
    df = pd.DataFrame(rows)
    df["_order"] = df["review_type"].map(order).fillna(9)
    return df.sort_values(["date", "_order", "confirm_time", "code"]).drop(columns=["_order"]).reset_index(drop=True)


def _load_context(ticket_dates: list[str]) -> pd.DataFrame:
    combo_signals = pd.read_csv(COMBO / "segment_runs" / "rtret60_or_breakbox25" / "stop_cd3_skip" / "2026YTD" / "signals.csv")
    volume_signals = pd.read_csv(PROBE / "segment_runs" / "volume5_baseline" / "blind" / "signals.csv")
    for df in [combo_signals, volume_signals]:
        df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        df["confirm_datetime"] = pd.to_datetime(df["confirm_datetime"], errors="coerce")
        df["confirm_time"] = df["confirm_datetime"].dt.strftime("%H:%M")
        if "source_family" not in df.columns:
            df["source_family"] = "volume5"
    combo_signals["signal_set"] = "combo_or"
    volume_signals["signal_set"] = "volume5_baseline"
    sig = pd.concat([combo_signals, volume_signals], ignore_index=True, sort=False)
    sig = sig[sig["entry_date"].isin(ticket_dates)].copy()
    cols = [
        "entry_date",
        "signal_set",
        "confirm_time",
        "code",
        "name",
        "source_family",
        "source",
        "v4_rank",
        "v4_score",
        "entry_price",
        "volume_ratio",
        "rt_return_from_d1_close",
        "rt_breakout_vs_box_top",
    ]
    available = [c for c in cols if c in sig.columns]
    return sig[available].sort_values(
        ["entry_date", "signal_set", "confirm_time", "v4_rank", "v4_score"],
        ascending=[True, True, True, True, False],
    )


def _json_default(value: Any) -> Any:
    if pd.isna(value):
        return None
    return str(value)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tickets = _build_tickets()
    context = _load_context(sorted(tickets["date"].unique().tolist()))
    tickets_path = OUT / "2026ytd_review_tickets.csv"
    context_path = OUT / "2026ytd_same_day_candidates.csv"
    tickets.to_csv(tickets_path, index=False, encoding="utf-8-sig")
    context.to_csv(context_path, index=False, encoding="utf-8-sig")
    payload = {
        "tickets": str(tickets_path),
        "same_day_candidates": str(context_path),
        "ticket_count": int(len(tickets)),
        "context_rows": int(len(context)),
    }
    (OUT / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
