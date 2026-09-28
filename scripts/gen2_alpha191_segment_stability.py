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

from scripts.gen2_batch_alpha191_factor_tests import run_batch  # noqa: E402


DEFAULT_OUTPUT_DIR = ROOT / "reports" / "gen2_alpha191_segment_stability"

SEGMENTS = [
    ("author_2010_2017", "2010-01-04", "2017-04-28"),
    ("post_style_2018_2021", "2018-01-01", "2021-12-31"),
    ("weak_market_2022_2023", "2022-01-01", "2023-12-31"),
    ("recent_g2_2024_2026", "2024-01-01", "2026-05-25"),
]


def _read_best(segment_dir: Path, segment: str) -> pd.DataFrame:
    path = segment_dir / "alpha191_recent_2y_best_by_factor.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    df["segment"] = segment
    return df


def _safe_float(value: Any) -> float | None:
    try:
        value = float(value)
    except Exception:
        return None
    return value if pd.notna(value) else None


def _rating_score(value: Any) -> int:
    return {
        "strong": 3,
        "medium": 2,
        "watch": 1,
        "insufficient": 0,
        "needs_data_source": 0,
    }.get(str(value), 0)


def _summarize(all_best: pd.DataFrame, output_dir: Path) -> dict[str, Any]:
    if all_best.empty:
        payload = {"schema_version": 1, "message": "no segment outputs found"}
        (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    all_best.to_csv(output_dir / "alpha191_segment_best_by_factor.csv", index=False, encoding="utf-8-sig")

    rows: list[dict[str, Any]] = []
    for factor_id, g in all_best.groupby("factor_id"):
        valid = g[g["available"] == True].copy()  # noqa: E712
        ratings = {str(row["segment"]): str(row.get("rating")) for _, row in g.iterrows()}
        directions = {str(row["segment"]): str(row.get("direction")) for _, row in valid.iterrows()}
        edges = [_safe_float(x) for x in valid.get("edge_mean", pd.Series(dtype=float))]
        edges = [x for x in edges if x is not None]
        ic_values = [_safe_float(x) for x in valid.get("mean_ic", pd.Series(dtype=float))]
        ic_values = [x for x in ic_values if x is not None]
        strong_count = sum(1 for x in ratings.values() if x == "strong")
        medium_plus_count = sum(1 for x in ratings.values() if x in {"strong", "medium"})
        direction_set = {x for x in directions.values() if x and x != "nan"}
        rows.append(
            {
                "factor_id": factor_id,
                "theme": valid["theme"].mode().iloc[0] if not valid.empty and "theme" in valid else None,
                "segments_tested": int(g["segment"].nunique()),
                "segments_available": int(valid["segment"].nunique()),
                "strong_count": int(strong_count),
                "medium_plus_count": int(medium_plus_count),
                "direction_consistent": len(direction_set) <= 1,
                "dominant_direction": sorted(direction_set)[0] if len(direction_set) == 1 else ",".join(sorted(direction_set)),
                "avg_edge": float(pd.Series(edges).mean()) if edges else None,
                "min_edge": float(pd.Series(edges).min()) if edges else None,
                "avg_abs_ic": float(pd.Series([abs(x) for x in ic_values]).mean()) if ic_values else None,
                "rating_score_sum": int(sum(_rating_score(x) for x in ratings.values())),
                "ratings_json": json.dumps(ratings, ensure_ascii=False, sort_keys=True),
                "directions_json": json.dumps(directions, ensure_ascii=False, sort_keys=True),
            }
        )
    stability = pd.DataFrame(rows)
    stability = stability.sort_values(
        ["medium_plus_count", "strong_count", "direction_consistent", "avg_edge", "avg_abs_ic"],
        ascending=[False, False, False, False, False],
    )
    stability.to_csv(output_dir / "alpha191_cross_segment_stability.csv", index=False, encoding="utf-8-sig")

    segment_summary = (
        all_best.groupby(["segment", "rating"], dropna=False)
        .size()
        .reset_index(name="count")
        .sort_values(["segment", "rating"])
    )
    segment_summary.to_csv(output_dir / "alpha191_segment_rating_summary.csv", index=False, encoding="utf-8-sig")

    top_rows = stability.head(30).where(pd.notna(stability.head(30)), None).to_dict("records")
    payload = {
        "schema_version": 1,
        "segments": [{"name": name, "start": start, "end": end} for name, start, end in SEGMENTS],
        "outputs": {
            "all_best": str(output_dir / "alpha191_segment_best_by_factor.csv"),
            "stability": str(output_dir / "alpha191_cross_segment_stability.csv"),
            "rating_summary": str(output_dir / "alpha191_segment_rating_summary.csv"),
        },
        "top_stable_factors": top_rows,
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def run(output_dir: Path, horizons: list[int], min_symbols: int, factor_pattern: str | None, no_resume: bool) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    frames: list[pd.DataFrame] = []
    for segment, start_date, end_date in SEGMENTS:
        segment_dir = output_dir / segment
        print(f"[segment] {segment} {start_date}..{end_date}", flush=True)
        run_batch(
            start_date=start_date,
            end_date=end_date,
            horizons=horizons,
            min_symbols=min_symbols,
            output_dir=segment_dir,
            factor_pattern=factor_pattern,
            resume=not no_resume,
        )
        best = _read_best(segment_dir, segment)
        if not best.empty:
            frames.append(best)
    all_best = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return _summarize(all_best, output_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run segmented GTJA Alpha191 stability tests.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--horizons", default="1,3,5")
    parser.add_argument("--min-symbols", type=int, default=80)
    parser.add_argument("--factor-pattern", default=None)
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()
    horizons = [int(x.strip()) for x in str(args.horizons).split(",") if x.strip()]
    payload = run(Path(args.output_dir), horizons, args.min_symbols, args.factor_pattern, args.no_resume)
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
