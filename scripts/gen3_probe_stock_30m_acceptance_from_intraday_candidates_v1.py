from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_validate_intraday_confirm import _label_signals, _load_minute_bars  # noqa: E402
from utils.paths import report_path  # noqa: E402


DEFAULT_SOURCE = report_path("gen3_mainline_icepoint_intraday_diffusion_validation_v1") / "candidate_with_intraday.csv"
OUT_DIR = report_path("gen3_mainline_icepoint_stock_30m_acceptance_probe_v1")
HORIZONS = [1, 2, 3, 5, 10, 20]


def _safe_float(value: Any, digits: int = 6) -> Any:
    try:
        x = float(value)
    except Exception:
        return None
    if pd.isna(x) or np.isinf(x):
        return None
    return round(x, digits)


def _load_candidates(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path, encoding="utf-8-sig")
    d["entry_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d["code"] = d["code_raw"].astype(str)
    d["name"] = d.get("stock_name", "").astype(str)
    d["g3_chain"] = "mainline_icepoint_repair_pullback"
    d["candidate_score"] = pd.to_numeric(d.get("mainline_window_score", 0.0), errors="coerce").fillna(0.0)
    d["entry_open"] = pd.to_numeric(d.get("close", np.nan), errors="coerce")
    return d.dropna(subset=["entry_date", "code"]).drop_duplicates(["entry_date", "code"]).copy()


def _build_acceptance(candidates: pd.DataFrame, period: int) -> pd.DataFrame:
    bars = _load_minute_bars(candidates, period=period)
    if bars.empty:
        return pd.DataFrame()
    ctx_cols = [
        "entry_date",
        "code",
        "name",
        "g3_chain",
        "candidate_score",
        "sector_name",
        "close_up_rate",
        "mainline_window_score",
        "pullback_from_high20",
        "days_since_high20",
    ]
    ctx = candidates[[c for c in ctx_cols if c in candidates.columns]].copy()
    joined = bars.merge(ctx, on=["entry_date", "code"], how="inner")
    joined = joined[joined["bar_time"].ge("10:00:00")].copy()
    if joined.empty:
        return joined

    prior_low = joined.groupby(["code", "entry_date"])["intraday_low_so_far"].shift(1)
    no_new_low = joined["low"] >= prior_low.fillna(joined["low"])
    break_prev = joined["close"] > joined["prev_bar_high"]
    break_open_range = joined["close"] > joined["open_range_high"]
    reclaim = (
        (joined["close"] > joined["open"])
        & joined["bar_close_pos"].ge(0.65)
        & joined["amount_ratio3"].fillna(0.0).ge(1.20)
        & (break_prev | break_open_range | no_new_low)
    )
    out = joined[reclaim].copy()
    if out.empty:
        return out
    out = out.sort_values(["entry_date", "code", "datetime"]).groupby(["entry_date", "code"], as_index=False).first()
    out = out.rename(
        columns={
            "datetime": "confirm_datetime",
            "close": "entry_price",
            "open": "confirm_open",
            "high": "confirm_high",
            "low": "confirm_low",
            "amount": "confirm_amount",
        }
    )
    out["confirm_rule"] = f"{period}m_volume_reclaim"
    return _label_signals(out)


def _summarize(label: str, sample: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for h in HORIZONS:
        col = f"fwd_ret_confirm_to_close_{h}d"
        vals = pd.to_numeric(sample.get(col), errors="coerce").dropna() if col in sample.columns else pd.Series(dtype=float)
        rows.append(
            {
                "group": label,
                "horizon": h,
                "count": int(len(vals)),
                "avg": _safe_float(vals.mean()),
                "median": _safe_float(vals.median()),
                "win_rate": _safe_float((vals > 0).mean()),
                "best": _safe_float(vals.max()),
                "worst": _safe_float(vals.min()),
            }
        )
    return rows


def run(args: argparse.Namespace) -> dict[str, Any]:
    source = Path(args.source)
    candidates = _load_candidates(source)
    accepted = _build_acceptance(candidates, int(args.period))
    summary = pd.DataFrame(_summarize("stock_30m_accept", accepted))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    accepted.to_csv(OUT_DIR / "accepted_signals.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")

    result = {
        "status": "completed",
        "source": str(source),
        "candidate_rows": int(len(candidates)),
        "accepted_rows": int(len(accepted)),
        "confirm_rate": _safe_float(len(accepted) / len(candidates)) if len(candidates) else None,
        "out_dir": str(OUT_DIR),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# G3 主线冰点修复候选的个股 30m 承接探针 v1",
        "",
        f"- 候选来源：{source}",
        f"- 日线候选数：{len(candidates)}",
        f"- 30m 承接确认数：{len(accepted)}",
        f"- 确认率：{result['confirm_rate']}",
        "",
        summary.to_markdown(index=False),
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe stock-level 30m acceptance from G3 mainline icepoint candidates.")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--period", type=int, default=30)
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
