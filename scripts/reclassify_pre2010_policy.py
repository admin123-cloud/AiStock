from __future__ import annotations

"""Read-only reclassification of pre-2010 candidate/formal evidence.

This report deliberately does not promote rows.  It applies the user-approved
policy (price/OHLC 0.5%, volume 0.1%, amount 1%, amount in yuan) to the
existing independent reports and keeps coverage/lifecycle differences in
review.  Where an upstream report does not decompose value differences by
field, the result says so instead of inferring an amount-only explanation.
"""

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


RUNTIME = Path(r"F:\AiStockData\data\runtime\quality_plan")
OUT = RUNTIME / "pre2010_policy_reclassification_20260921"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def add_bucket(out: dict, year: int, layer: str, period: str, *, candidate: int,
               missing: int = 0, extra: int = 0, duplicates: int = 0,
               incomplete: int = 0, value_diff: int = 0,
               ohlc: int | None = None, volume: int | None = None,
               amount: int | None = None, within: int | None = None,
               source_status: str = "checked", note: str = "") -> None:
    # A value difference is beyond the period comparator's configured policy
    # (0.5% price, 0.1% volume, 1% amount).  It is therefore not accepted.
    # Missing keys are review unless the evidence proves no lifecycle/suffix
    # explanation; the current pre-2010 evidence does not prove that.
    accepted = max(candidate - value_diff - incomplete, 0) if source_status == "checked" else 0
    rejected = value_diff
    review = missing + extra + duplicates + incomplete
    status = "accepted" if source_status == "checked" and rejected == 0 and review == 0 else (
        "not_checked" if source_status != "checked" else "review")
    row = {
        "year": year, "layer": layer, "period": period,
        "candidate_rows": candidate, "formal_missing": missing,
        "formal_extra": extra, "formal_duplicate_groups": duplicates,
        "candidate_incomplete": incomplete, "value_diff_over_policy": value_diff,
        "accepted_quality_rows": accepted, "review_rows": review,
        "rejected_rows": rejected, "status": status,
        "source_status": source_status, "note": note,
    }
    if ohlc is not None:
        row["ohlc_conflict_over_policy"] = ohlc
    if volume is not None:
        row["volume_conflict_over_policy"] = volume
    if amount is not None:
        row["amount_conflict_over_policy"] = amount
    if within is not None:
        row["within_policy_value_rows"] = within
    out["rows"].append(row)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": "pre2010-policy-reclassification-v1",
        "scope": [2000, 2009],
        "read_only": True,
        "formal_clickhouse_written": False,
        "formal_mutation": False,
        "policy": {
            "price_ohlc_relative_tolerance": 0.005,
            "volume_relative_tolerance": 0.001,
            "amount_relative_tolerance": 0.01,
            "amount_unit": "yuan",
            "lifecycle_or_suffix_difference": "review",
            "formal_extra": "review_only",
            "unobserved_field_decomposition": "review",
        },
        "rows": [],
        "sources": [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # 2000--2002: parse the complete new-candidate reports.
    for year in (2000, 2001, 2002):
        p = RUNTIME / "ocean_formal_new_2000_2002" / str(year) / "report.json"
        d = load(p); report["sources"].append({"path": str(p), "sha256": sha(p)})
        for r in d["rows"]:
            add_bucket(
                report, year, "5m", "5m", candidate=int(r["candidate_rows"]),
                missing=int(r["missing_exact"]), extra=int(r["formal_extra_exact"]),
                duplicates=int(r["formal_duplicate_groups"]),
                value_diff=int(r["value_conflict_rows"]),
                ohlc=int(r["ohlc_conflict_rows"]), volume=int(r["volume_conflict_rows"]),
                amount=int(r["amount_conflict_rows"]), within=int(r["within_tolerance_rows"]),
                note="amount-only <=1% is within policy but upstream report does not split it from other within-tolerance fields",
            )
        p = RUNTIME / "ocean_derived_formal_new_2000_2002" / str(year) / "minute_report.json"
        d = load(p); report["sources"].append({"path": str(p), "sha256": sha(p)})
        for r in d["rows"]:
            add_bucket(report, year, "derived", r["period"], candidate=int(r["candidate_rows"]),
                        missing=int(r["missing_formal_rows"]), extra=int(r["formal_extra_rows"]),
                        duplicates=int(r["formal_duplicate_groups"]),
                        incomplete=int(r["candidate_incomplete_buckets"]),
                        value_diff=int(r["value_difference_rows"]),
                        note="formal extra/lifecycle differences remain review")
        p = RUNTIME / "ocean_derived_formal_new_2000_2002" / str(year) / "daily_report.json"
        d = load(p); report["sources"].append({"path": str(p), "sha256": sha(p)})
        for r in d["rows"]:
            add_bucket(report, year, "derived", "1d", candidate=int(r["candidate_rows"]),
                        missing=int(r["missing_formal_rows"]), extra=int(r["formal_extra_rows"]),
                        duplicates=int(r["formal_duplicate_groups"]),
                        incomplete=int(r["candidate_incomplete_buckets"]),
                        value_diff=int(r["value_difference_rows"]),
                        note="daily is auxiliary; 5m is not rejected by daily-only differences")

    # 2003--2005: the independent closeout report has complete aggregate
    # counts but no per-field decomposition for value differences.
    derived_0305 = {
        2003: {"15": (3484704, 4784, 511568, 1887008, 68142), "30": (1742352, 2392, 255784, 1909842, 35006), "60": (871176, 1196, 127892, 997872, 18317), "1d": (217794, 0, 75375, 0, 119)},
        2004: {"15": (3927664, 3696, 443920, 0, 1171640), "30": (1963832, 1848, 221960, 0, 546478), "60": (981916, 924, 110980, 0, 233735), "1d": (245479, 0, 70408, 0, 252)},
        2005: {"15": (4016144, 3712, 419600, 0, 1259886), "30": (2008072, 1856, 209800, 0, 565600), "60": (1004036, 928, 104900, 0, 224026), "1d": (251009, 0, 69398, 0, 302)},
    }
    p0305 = Path(r"F:\AiDevelop\ai-workspace\projects\AiStock\changes\2026-09-21-ocean-2003-2005-detail-materialization.md")
    report["sources"].append({"path": str(p0305), "sha256": sha(p0305)})
    for year, periods in derived_0305.items():
        for period, (candidate, missing, extra, dup, diff) in periods.items():
            add_bucket(report, year, "derived", period, candidate=candidate, missing=missing,
                        extra=extra, duplicates=dup, value_diff=diff,
                        note="missing keys and code suffix/lifecycle findings are review; upstream does not decompose value_diff into OHLC/volume/amount")

    # 2006--2009: new-candidate derived audit counts. Missing keys are kept
    # review because they are formal coverage differences; value differences
    # are rejected only as an aggregate comparator result, not attributed to
    # a specific field without raw row evidence.
    derived_0609 = {
        2006: {"15": (3862032, 11296, 229728, 1474173), "30": (1931016, 5648, 114864, 625617), "60": (965508, 2824, 57432, 248567), "1d": (241377, 0, 53705, 2304)},
        2007: {"15": (4394528, 3408, 266640, 1797725), "30": (2197264, 1704, 133320, 764648), "60": (1098632, 852, 66660, 287171), "1d": (274658, 0, 56715, 1861)},
        2008: {"15": (4838288, 3744, 388912, 2630186), "30": (2419144, 1872, 194456, 1106353), "60": (1209572, 936, 97228, 399349), "1d": (302393, 0, 66550, 3940)},
        2009: {"15": (5257456, 2464, 212336, 3746646), "30": (2628728, 1232, 106168, 1618376), "60": (1314364, 616, 53084, 603982), "1d": (328591, 0, 55946, 1453)},
    }
    p0609 = Path(r"F:\AiDevelop\ai-workspace\projects\AiStock\changes\2026-09-21-pre2010-new-candidate-derived-audit-2006-2009.md")
    report["sources"].append({"path": str(p0609), "sha256": sha(p0609)})
    for year, periods in derived_0609.items():
        for period, (candidate, missing, extra, diff) in periods.items():
            add_bucket(report, year, "derived", period, candidate=candidate, missing=missing,
                        extra=extra, value_diff=diff,
                        note="new-candidate internal buckets pass; formal missing/extra are coverage review; value_diff field decomposition unavailable")

    # Explicit materialization status for 5m 2003--2009. New 5m candidate
    # structure passed, but promotion classification still requires formal
    # exact-key evidence; do not turn that absence into accepted.
    for year in range(2003, 2010):
        add_bucket(report, year, "5m_promotion", "5m", candidate=0,
                    source_status="not_checked",
                    note="5m detail materialized and internal 48-slot gate passed; candidate-to-formal promotion evidence is not a clean accepted set")

    # Summaries by year/layer/period and global promotion decision.
    summary = defaultdict(lambda: {"accepted": 0, "review": 0, "rejected": 0, "not_checked": 0})
    for row in report["rows"]:
        key = f"{row['year']}/{row['layer']}/{row['period']}"
        bucket = summary[key]
        bucket["accepted"] += row["accepted_quality_rows"]
        bucket["review"] += row["review_rows"]
        bucket["rejected"] += row["rejected_rows"]
        bucket["not_checked"] += int(row["status"] == "not_checked")
    report["summary_by_scope"] = dict(summary)
    report["promotion"] = {
        "accepted_rows": 0,
        "review_rows": sum(r["review_rows"] for r in report["rows"]),
        "rejected_rows": sum(r["rejected_rows"] for r in report["rows"]),
        "formal_insert_rows": 0,
        "reason": "no candidate row meets missing-key + no-conflict + no-lifecycle-ambiguity proof",
    }
    out_json = OUT / "manifest.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(out_json), "rows": len(report["rows"]), "accepted": 0,
                      "review": report["promotion"]["review_rows"],
                      "rejected": report["promotion"]["rejected_rows"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
