"""Mark only successfully verified wstock tail restorations as reviewed.

The operation is idempotent per review key.  Without --yes this is a read-only
preview, so a staged file can be inspected safely before calling the API.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_client


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tail", required=True)
    ap.add_argument("--staged", type=Path, required=True)
    ap.add_argument("--applied", type=Path, required=True)
    ap.add_argument("--queue", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--api", default="http://127.0.0.1:3000/api/operations/data-review-actions")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()

    staged = json.loads(a.staged.read_text(encoding="utf-8"))
    applied = json.loads(a.applied.read_text(encoding="utf-8"))
    if applied.get("readback_fail"):
        raise SystemExit("refuse ledger update: formal readback failures present")
    accepted = {(x["code"], x["trade_date"][:10]) for x in staged.get("accepted", [])}
    client = clickhouse_client()
    bad = []
    for code, day in sorted(accepted):
        count = client.query(
            "SELECT count() FROM stock.kline_minute_5 "
            "WHERE code={code:String} AND toDate(datetime)={day:Date}",
            parameters={"code": code, "day": day},
            settings={"max_threads": 1},
        ).result_rows[0][0]
        if count != 48:
            bad.append({"code": code, "trade_date": day, "formal_rows": count})
    if bad:
        print(json.dumps({"refused": "formal_not_48", "bad": bad}, ensure_ascii=False))
        raise SystemExit(1)
    queue = json.loads(a.queue.read_text(encoding="utf-8"))
    targets = [x for x in queue if (x["code"], x["trade_date"][:10]) in accepted]
    reason = (
        "该代码日已通过 wstock 官方 wdz 转换源补全并写入正式表；"
        "stage 通过完整 48 根门禁，正式写入后逐键读回验证通过。"
    )
    batch = f"wstock-rotation-tail{a.tail}-20260919"

    def evidence_hash(refs: list[str]) -> str:
        raw = json.dumps({"reason": reason, "evidence_refs": refs},
                         ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    print(json.dumps({"tail": a.tail, "accepted": len(accepted),
                      "queue_targets": len(targets), "apply": applied},
                     ensure_ascii=False))
    if not a.yes:
        return

    ok = 0
    for item in targets:
        code = item["code"]
        market = code.split(".", 1)[1].upper()
        number = code.split(".", 1)[0]
        refs = [
            f"research:{a.staged}",
            f"research:{a.applied}",
            f"research:{a.out_dir / market / (market + number + '.csv')}",
        ]
        body = {
            "action": "mark_reviewed",
            "review_key": item["key"],
            "idempotency_key": "wstock-rotation-" + hashlib.sha256(item["key"].encode()).hexdigest()[:24],
            "reason": reason,
            "evidence_refs": refs,
            "evidence_hash": evidence_hash(refs),
            "confirm_token": "auto",
            "payload": {"batch": batch, "tail": a.tail},
        }
        req = urllib.request.Request(a.api, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30):
            pass
        ok += 1
    print(json.dumps({"marked_reviewed": ok, "failed": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
