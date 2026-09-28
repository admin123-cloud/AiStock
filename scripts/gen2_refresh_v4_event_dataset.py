from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.gen2_build_v4_event_dataset import build_dataset  # noqa: E402

DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_event_study_full"


def _write_parquet_atomic(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        df.to_parquet(tmp_path, index=False)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _date_text(value: Any) -> str:
    return str(pd.Timestamp(value).date())


def _load_existing_summary(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def refresh_dataset(args: argparse.Namespace) -> Dict[str, Any]:
    output_dir = Path(args.output_dir)
    parquet_path = output_dir / "v4_event_dataset.parquet"
    summary_path = output_dir / "summary.json"
    output_dir.mkdir(parents=True, exist_ok=True)

    start_date = _date_text(args.start_date)
    end_date = _date_text(args.end_date)
    tmp_root = Path(tempfile.mkdtemp(prefix="gen2_v4_refresh_"))
    try:
        window_dir = tmp_root / "window"
        window_summary = build_dataset(
            SimpleNamespace(
                start_date=start_date,
                end_date=end_date,
                max_rank=int(args.max_rank),
                output_dir=str(window_dir),
                skip_csv=True,
            )
        )
        new_df = pd.read_parquet(window_dir / "v4_event_dataset.parquet")
        existing_summary = _load_existing_summary(summary_path)

        if parquet_path.exists():
            old_df = pd.read_parquet(parquet_path)
            if not old_df.empty and "trade_date" in old_df.columns:
                old_df = old_df.copy()
                old_df["trade_date"] = pd.to_datetime(old_df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
                old_df = old_df[(old_df["trade_date"] < start_date) | (old_df["trade_date"] > end_date)].copy()
            merged = pd.concat([old_df, new_df], ignore_index=True, sort=False)
        else:
            merged = new_df

        if not merged.empty:
            merged["trade_date"] = pd.to_datetime(merged["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
            merged = merged.dropna(subset=["trade_date", "code"]).sort_values(["trade_date", "v4_rank", "code"]).reset_index(drop=True)

        backup_path = None
        if parquet_path.exists() and bool(args.backup):
            backup_path = parquet_path.with_suffix(f".{pd.Timestamp.now().strftime('%Y%m%d%H%M%S')}.bak.parquet")
            shutil.copy2(parquet_path, backup_path)

        _write_parquet_atomic(merged, parquet_path)
        if bool(args.write_csv):
            merged.to_csv(output_dir / "v4_event_dataset.csv", index=False, encoding="utf-8-sig")

        summary = {
            **existing_summary,
            "schema_version": int(existing_summary.get("schema_version") or 1),
            "start_date": str(merged["trade_date"].min()) if not merged.empty else start_date,
            "end_date": str(merged["trade_date"].max()) if not merged.empty else end_date,
            "max_rank": int(args.max_rank),
            "rank_scope": "full_score_pool" if int(args.max_rank) <= 0 else f"top_{int(args.max_rank)}",
            "rows": int(len(merged)),
            "signal_days": int(merged["trade_date"].nunique()) if not merged.empty else 0,
            "unique_stocks": int(merged["code"].nunique()) if not merged.empty else 0,
            "last_refresh": {
                "start_date": start_date,
                "end_date": end_date,
                "window_rows": int(len(new_df)),
                "window_signal_days": int(new_df["trade_date"].nunique()) if not new_df.empty else 0,
                "backup_path": str(backup_path) if backup_path else "",
                "window_summary": window_summary,
            },
            "outputs": {
                "dataset_parquet": "v4_event_dataset.parquet",
                "dataset_csv": "v4_event_dataset.csv" if bool(args.write_csv) else None,
                "summary_json": "summary.json",
            },
        }
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return summary
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh and merge recent G2 V4 event dataset rows.")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--max-rank", type=int, default=0)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--write-csv", action="store_true")
    parser.add_argument("--no-backup", dest="backup", action="store_false")
    parser.set_defaults(backup=True)
    return parser.parse_args()


def main() -> int:
    summary = refresh_dataset(parse_args())
    print(json.dumps(summary.get("last_refresh", summary), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
