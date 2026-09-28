from __future__ import annotations

import json
from pathlib import Path


BASE = Path(r"F:\AiStock\data\data\runtime\quality_plan\pre2010_policy_reclassification_20260921\manifest.json")
DETAIL = Path(r"F:\AiStock\data\data\runtime\quality_plan\pre2010_amount_only_subset_20260921\manifest.json")


def main() -> int:
    base = json.loads(BASE.read_text(encoding="utf-8"))
    detail = json.loads(DETAIL.read_text(encoding="utf-8"))
    by_year = {}
    for year in (2000, 2001, 2002):
        rows = [r for r in detail["rows"] if r["year"] == year]
        keys = {
            "exact", "within_policy_nonzero", "amount_only_over_1pct",
            "amount_only_within_1pct", "ohlc_only_within_0_5pct",
            "volume_only_within_0_1pct",
            "ohlc_only_over_0_5pct", "volume_only_over_0_1pct",
            "volume_and_amount_over_policy", "ohlc_and_amount_over_policy",
            "ohlc_and_volume_over_policy", "mixed_ohlc_volume_amount_over_policy",
            "candidate_rows", "formal_rows", "missing_exact", "formal_extra_exact",
        }
        agg = {k: sum(int(r.get(k, 0)) for r in rows) for k in keys}
        by_year[str(year)] = agg
    base["field_level_subset"] = {
        "source_manifest": str(DETAIL),
        "scope": [2000, 2002],
        "status": "checked",
        "amount_only_over_1pct_is_not_accepted": True,
        "summary_by_year": by_year,
        "interpretation": {
            "exact_or_within_policy": "accepted_quality_only; formal existing keys are not promotion additions",
            "amount_only_within_1pct": "accepted_quality_only under pre-2010 policy; not a promotion addition when formal key already exists",
            "amount_only_over_1pct": "review_or_rejected_amount_conflict; not automatically accepted",
            "ohlc_or_volume_over_policy": "rejected_value_conflict",
            "missing_exact": "0 in 2000-2002; no new-key promotion subset",
            "formal_extra": "review_coverage_only",
        },
    }
    BASE.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(BASE), "field_level_subset": base["field_level_subset"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

