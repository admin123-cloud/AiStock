from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from api.gen2_strategy import GEN2_OPEN_RULE_V1, build_gen2_open_signals  # noqa: E402

DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_open_v1_signals"


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if pd.isna(value):
        return None
    return str(value)


def _write_findings(output_dir: Path, payload: dict[str, Any]) -> None:
    rule = payload.get("rule") or {}
    rows = payload.get("recent_rows") or []
    lines = [
        "# G2 Open V1 Signals",
        "",
        f"Rule: `{rule.get('rule_id', '')}`",
        f"Selected date: {payload.get('selected_date', '')}",
        f"Latest signal date: {payload.get('latest_signal_date', '')}",
        f"Exact count: {payload.get('exact_count', 0)}",
        f"Recent count: {payload.get('recent_count', 0)}",
        "",
        "## Rule",
        "",
        f"- Pattern: `{rule.get('pattern', '')}`",
        f"- State: `{rule.get('g2_open_state', '')}`",
        f"- Trigger: `{rule.get('trigger_type', '')}`",
        f"- Expected hold days: `{rule.get('hold_days', '')}`",
        "",
        "## Recent Signals",
        "",
        "| date | code | name | rank | score | entry | 3d_ret | 3d_excess |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows[:30]:
        score = row.get("v4_score")
        entry_price = row.get("entry_price")
        lines.append(
            "| {date} | {code} | {name} | {rank} | {score} | {entry} | {ret} | {excess} |".format(
                date=row.get("signal_date", ""),
                code=row.get("code", ""),
                name=row.get("name", ""),
                rank=row.get("v4_rank", ""),
                score="" if score is None else f"{float(score):.4f}",
                entry="" if entry_price is None else f"{float(entry_price):.2f}",
                ret=row.get("historical_ret_3d_text") or "",
                excess=row.get("historical_excess_3d_text") or "",
            )
        )
    (output_dir / "findings.md").write_text("\n".join(lines), encoding="utf-8")


def run(output_dir: Path, signal_date: str, limit: int) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = build_gen2_open_signals(signal_date or None, limit)
    rows = payload.get("recent_rows") or []
    exact_rows = payload.get("exact_rows") or []

    pd.DataFrame(rows).to_csv(output_dir / "recent_signals.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(exact_rows).to_csv(output_dir / "exact_signals.csv", index=False, encoding="utf-8-sig")
    (output_dir / "rule.json").write_text(json.dumps(GEN2_OPEN_RULE_V1, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_findings(output_dir, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Build G2 Open V1 signal artifacts.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--signal-date", default="")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    payload = run(Path(args.output_dir), str(args.signal_date or ""), int(args.limit))
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
