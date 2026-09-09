from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[1]))

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_backtest_risk_cool_dynamic_circuit import _run_dynamic  # noqa: E402


BASE = ROOT / "reports" / "g2_volume5_sector_layer_probe"
ENRICHED = BASE / "volume5_sector_enriched.parquet"
OUT = ROOT / "reports" / "g2_volume5_sector_sort_bonus_probe"
WINDOWS = {
    "full": ("2024-07-09", "2026-05-26"),
    "train": ("2024-07-09", "2025-03-31"),
    "valid": ("2025-04-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-05-26"),
}


from research.common.reporting import timestamp_json_default as _json_default


from research.common.reporting import percent_text as _pct


def _drop_aux(df: pd.DataFrame) -> pd.DataFrame:
    return df.drop(columns=[c for c in ["confirm_time", "code6"] if c in df.columns]).copy()


def _write_source(name: str, df: pd.DataFrame) -> Path:
    source_dir = OUT / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / f"{name}.parquet"
    out = _drop_aux(df)
    out.to_parquet(path, index=False)
    out.to_csv(path.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    return path


def _prepare_base() -> pd.DataFrame:
    df = pd.read_parquet(ENRICHED).copy()
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["confirm_datetime"] = pd.to_datetime(df["confirm_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    df["v4_rank_orig"] = pd.to_numeric(df["v4_rank"], errors="coerce").fillna(999).astype(int)
    df["v4_score_orig"] = pd.to_numeric(df["v4_score"], errors="coerce").fillna(0.0)
    df["l3_s3"] = pd.to_numeric(df["l3_s3"], errors="coerce").fillna(-1.0)
    df["sector_strong"] = df["l3_s3"] >= 0.05
    return df


def _rank_bonus(df: pd.DataFrame, bonus: int) -> pd.DataFrame:
    out = df.copy()
    adj = out["v4_rank_orig"] - np.where(out["sector_strong"], bonus, 0)
    out["v4_rank"] = np.maximum(1, adj).astype(int)
    out["v4_score"] = out["v4_score_orig"]
    return out


def _score_bonus(df: pd.DataFrame, bonus: float) -> pd.DataFrame:
    out = df.copy()
    out["v4_rank"] = out["v4_rank_orig"]
    out["v4_score"] = out["v4_score_orig"] + np.where(out["sector_strong"], bonus, 0.0)
    return out


def _score_scaled(df: pd.DataFrame, scale: float) -> pd.DataFrame:
    out = df.copy()
    out["v4_rank"] = out["v4_rank_orig"]
    out["v4_score"] = out["v4_score_orig"] + np.maximum(out["l3_s3"], 0.0) * scale
    return out


def _combo_bonus(df: pd.DataFrame, rank_bonus: int, score_bonus: float) -> pd.DataFrame:
    out = _rank_bonus(df, rank_bonus)
    out["v4_score"] = out["v4_score_orig"] + np.where(out["sector_strong"], score_bonus, 0.0)
    return out


def _variants(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {
        "base": df,
        "rank_b1": _rank_bonus(df, 1),
        "rank_b2": _rank_bonus(df, 2),
        "score_b005": _score_bonus(df, 0.05),
        "score_b010": _score_bonus(df, 0.10),
        "score_b020": _score_bonus(df, 0.20),
        "score_s010": _score_scaled(df, 0.10),
        "score_s020": _score_scaled(df, 0.20),
        "combo_r1_s005": _combo_bonus(df, 1, 0.05),
        "combo_r1_s010": _combo_bonus(df, 1, 0.10),
    }


def _trade_lot_summary(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "trades.csv"
    if not path.exists():
        return {}
    trades = pd.read_csv(path)
    if trades.empty:
        return {"lot_count": 0}
    lot = trades.groupby(["buy_date", "code", "name"], dropna=False).agg(pnl=("pnl", "sum"))
    return {
        "lot_count": int(len(lot)),
        "lot_win_rate": float((lot["pnl"] > 0).mean()),
        "lot_avg_pnl": float(lot["pnl"].mean()),
    }


def _run_or_read(source_path: Path, run_dir: Path, start: str, end: str, sort_mode: str) -> dict[str, Any]:
    summary_path = run_dir / "summary.json"
    if summary_path.exists():
        return json.loads(summary_path.read_text(encoding="utf-8"))
    return _run_dynamic(source_path, run_dir, "stop_cd3_skip", start, end, sort_mode=sort_mode)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    base = _prepare_base()
    rows: list[dict[str, Any]] = []
    for variant, source_df in _variants(base).items():
        source_path = _write_source(variant, source_df)
        counts = {
            "signals": int(len(source_df)),
            "sector_strong_signals": int(source_df["sector_strong"].sum()),
        }
        for sort_mode in ["rank", "score"]:
            for window, (start, end) in WINDOWS.items():
                run_dir = OUT / "runs" / variant / sort_mode / window
                summary = _run_or_read(source_path, run_dir, start, end, sort_mode)
                rows.append(
                    {
                        "variant": variant,
                        "sort_mode": sort_mode,
                        "window": window,
                        **counts,
                        "trades": summary["trade_count"],
                        "total_return": summary["total_return"],
                        "excess_return": summary["excess_return"],
                        "max_drawdown": summary["max_drawdown"],
                        "win_rate": summary["win_rate"],
                        "avg_trade_return": summary["avg_trade_return"],
                        **_trade_lot_summary(run_dir),
                    }
                )
    result = pd.DataFrame(rows)
    result.to_csv(OUT / "volume5_sector_sort_bonus_summary.csv", index=False, encoding="utf-8-sig")
    (OUT / "volume5_sector_sort_bonus_summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    lines = [
        "# Volume5 Sector Sort Bonus Probe",
        "",
        "| variant | sort | window | trades | lots | total | excess | max_dd | win | lot_win | avg_trade |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['variant']} | {row['sort_mode']} | {row['window']} | {row['trades']} | "
            f"{row.get('lot_count', '')} | {_pct(row['total_return'])} | {_pct(row['excess_return'])} | "
            f"{_pct(row['max_drawdown'])} | {_pct(row['win_rate'])} | {_pct(row.get('lot_win_rate'))} | "
            f"{_pct(row['avg_trade_return'])} |"
        )
    (OUT / "volume5_sector_sort_bonus_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(OUT), "rows": len(rows)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
