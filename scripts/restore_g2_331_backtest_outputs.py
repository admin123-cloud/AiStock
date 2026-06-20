from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path


RECOVERED_RUN_DIR = report_path(
    "gen2_v2_complete_331_recovery",
    "recovered_from_git",
    "rerun_probe_expand_2020_2026",
)
CANONICAL_RUN_DIR = report_path(
    "gen2_v2_complete_strategy",
    "runs",
    "sort_probe",
    "g2_v2",
    "full",
)
CANONICAL_SOURCE_DIR = report_path("gen2_v2_complete_strategy", "sources")
RECOVERED_SOURCE = report_path(
    "gen2_v2_complete_331_recovery",
    "recovered_from_git",
    "g2_v2_complete.parquet",
)

RUN_FILES = [
    "summary.json",
    "equity_curve.csv",
    "trades.csv",
    "signals.csv",
]


def _copy_if_exists(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def restore_g2_331_backtest_outputs() -> dict[str, Any]:
    missing = [name for name in RUN_FILES if not (RECOVERED_RUN_DIR / name).exists()]
    if missing:
        raise FileNotFoundError(f"Recovered G2 331% run is incomplete: {missing}")

    backup_dir = ""
    if CANONICAL_RUN_DIR.exists():
        backup = CANONICAL_RUN_DIR.parent / f"full_backup_before_331_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        backup.mkdir(parents=True, exist_ok=True)
        for name in RUN_FILES + ["segment_summary.csv", "decision_ledger.csv"]:
            _copy_if_exists(CANONICAL_RUN_DIR / name, backup / name)
        backup_dir = str(backup)

    CANONICAL_RUN_DIR.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in RUN_FILES:
        shutil.copy2(RECOVERED_RUN_DIR / name, CANONICAL_RUN_DIR / name)
        copied.append(name)

    source_copied = False
    if RECOVERED_SOURCE.exists():
        source_copied = _copy_if_exists(RECOVERED_SOURCE, CANONICAL_SOURCE_DIR / "g2_v2_complete.recovered_331.parquet")

    summary = json.loads((CANONICAL_RUN_DIR / "summary.json").read_text(encoding="utf-8"))
    manifest = {
        "restored_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "g2_331_recovered_from_git",
        "source_dir": str(RECOVERED_RUN_DIR),
        "target_dir": str(CANONICAL_RUN_DIR),
        "backup_dir": backup_dir,
        "copied_files": copied,
        "source_copied": source_copied,
        "summary": {
            "total_return": summary.get("total_return"),
            "signal_count": summary.get("signal_count"),
            "trade_count": summary.get("trade_count"),
            "max_drawdown": summary.get("max_drawdown"),
            "win_rate": summary.get("win_rate"),
        },
    }
    (CANONICAL_RUN_DIR / "restore_331_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    print(json.dumps(restore_g2_331_backtest_outputs(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
