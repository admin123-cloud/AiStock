"""Read-only T1.6/T2 triage of scoped 5m anomalies.

No provider client or database mutation is invoked here. Without a fresh,
row-level QMT/TDX/announcement payload, every item remains user-actionable
blocked evidence; this prevents category-wide exclusions or guessed repairs.
"""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.paths import runtime_path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input", type=Path)
    p.add_argument("--output-dir", type=Path)
    args = p.parse_args()
    root = runtime_path() / "minute_migration_v2"
    source = args.input or root / "scoped_anomalies_20260912_astra5.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    source_hash = sha256(source)
    pending = []
    for item in payload.get("items", []):
        pending.append({
            "code": item["code"], "date": item["date"], "count": item["count"],
            "category": item["category"], "old_value": {"5m_count": item["count"]},
            "new_value": None, "source": None,
            "evidence_path": item["evidence"].get("daily_detail_path"),
            "evidence_hash": item["evidence"].get("daily_detail_sha256"),
            "authority": "pending QMT re-pull, TDX cross-check, and historical status evidence",
            "validity": "blocked_evidence",
            "reason": "No fresh row-level provider/announcement evidence was supplied; no correction inferred.",
        })
    out = args.output_dir or root
    out.mkdir(parents=True, exist_ok=True)
    base = {"run_id": payload.get("run_id"), "baseline_fingerprint": payload.get("baseline_fingerprint"),
            "baseline_cutoff": payload.get("baseline_cutoff"), "source_manifest": str(source),
            "source_manifest_sha256": source_hash, "read_only": True, "formal_table_mutation": False,
            "qmt_repull": "not_executed_in_read_only_triage", "tdx_crosscheck": "not_executed",
            "historical_status_check": "not_executed", "authority": "Astra checkpoint T1.6/T2",
            "corrections": [], "pending_user_review": pending,
            "statistics": {"input_rows": len(payload.get("items", [])), "correction_rows": 0,
                           "pending_rows": len(pending), "blocked_evidence_rows": len(pending)}}
    for name, data in (("corrections", base["corrections"]), ("pending", pending)):
        path = out / f"scoped_anomalies_20260912_astra5_{name}.json"
        path.write_text(json.dumps({**base, "corrections": data if name == "corrections" else [],
                                    "pending_user_review": data if name == "pending" else []},
                                   indent=2, ensure_ascii=False), encoding="utf-8")
    evidence = out / "scoped_anomalies_20260912_astra5_triage.json"
    evidence.write_text(json.dumps(base, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"corrections": 0, "pending": len(pending), "blocked_evidence": len(pending),
                      "triage": str(evidence), "read_only": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
