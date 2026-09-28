from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--audit", type=Path, required=True)
    p.add_argument("--derived", type=Path)
    p.add_argument("--formal-diff", type=Path)
    p.add_argument("--formal-diff-1d", type=Path)
    args = p.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    manifest["detail_audit"] = str(args.audit)
    manifest["detail_audit_sha256"] = sha256(args.audit)
    manifest["detail_audit_status"] = audit.get("status")
    manifest["detail_audit_stats"] = audit.get("stats", {})
    manifest["detail_audit_review_reasons"] = audit.get("review_reasons", {})
    manifest["status"] = "complete" if audit.get("status") == "pass" else "review"
    if args.derived and args.derived.is_file():
        derived = json.loads(args.derived.read_text(encoding="utf-8"))
        manifest["derived_manifest"] = str(args.derived)
        manifest["derived_manifest_sha256"] = sha256(args.derived)
        manifest["derived_status"] = derived.get("status")
        manifest["derived_rows"] = derived.get("derived_rows", {})
    if args.formal_diff and args.formal_diff.is_file():
        diff = json.loads(args.formal_diff.read_text(encoding="utf-8"))
        manifest["formal_diff"] = str(args.formal_diff)
        manifest["formal_diff_sha256"] = sha256(args.formal_diff)
        manifest["formal_diff_status"] = diff.get("status")
        manifest["formal_diff_rows"] = len(diff.get("rows", []))
        if diff.get("status") != "pass":
            manifest["status"] = "review"
    if args.formal_diff_1d and args.formal_diff_1d.is_file():
        diff = json.loads(args.formal_diff_1d.read_text(encoding="utf-8"))
        manifest["formal_diff_1d"] = str(args.formal_diff_1d)
        manifest["formal_diff_1d_sha256"] = sha256(args.formal_diff_1d)
        manifest["formal_diff_1d_status"] = diff.get("status")
        manifest["formal_diff_1d_rows"] = len(diff.get("rows", []))
        if diff.get("status") != "pass":
            manifest["status"] = "review"
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
