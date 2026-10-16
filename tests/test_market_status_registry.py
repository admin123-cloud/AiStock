import hashlib
import json
from datetime import date
from pathlib import Path
import tempfile
import unittest

from scripts.maintain_market_status import is_exempt, plan_insert, prepare_exact


class MarketStatusRegistryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "notice.pdf"
        path.write_bytes(b"reviewed primary notice fixture")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.proof = dict(code="002717.SZ", date="2026-10-13", status="suspended",
                          review_state="verified_for_this_exact_date", kline_fabricated=False,
                          evidence_url="https://static.cninfo.com.cn/finalpage/2026-10-12/1225602315.PDF",
                          source_path=str(path), source_sha256=digest,
                          full_text_path=str(path), full_text_sha256=digest,
                          supporting_interval_start="2026-10-13", supporting_interval_end="2026-10-15",
                          reason="已人工核验的公告证据")

    def prepare(self, **changes):
        return prepare_exact([dict(self.proof, **changes)], today=date(2026, 10, 16))

    def test_exact_date_never_expands_support_interval(self):
        row, = self.prepare()
        self.assertEqual((row["start_date"], row["end_date"]), ("2026-10-13", "2026-10-13"))
        self.assertEqual(json.loads(row["evidence_json"])["source_sha256"], self.proof["source_sha256"])
        self.assertTrue(is_exempt(row))

    def test_unknown_candidate_and_revoked_never_exempt(self):
        row, = self.prepare()
        for state in ("unconfirmed", "pending_exchange_confirmation", "revoked", "superseded"):
            self.assertFalse(is_exempt(dict(row, review_state=state)))
        self.assertFalse(is_exempt(dict(row, evidence_url="")))
        self.assertFalse(is_exempt(dict(row, status="source_absent")))

    def test_tampered_or_unreviewed_notice_rejected(self):
        for change in (dict(source_sha256="0" * 64), dict(review_state="unconfirmed"),
                       dict(evidence_url="https://news.example/notice"), dict(date="2026-10-12"),
                       dict(date="2026-10-17"), dict(kline_fabricated=True)):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.prepare(**change)

    def test_idempotent_and_conflicting_existing_key(self):
        rows = self.prepare()
        self.assertEqual(plan_insert(rows, [dict(rows[0], observed_at="2026-10-16 00:00:00")])[0], [])
        for change in (dict(review_state="revoked"), dict(reason="different evidence")):
            with self.assertRaises(ValueError):
                plan_insert(rows, [dict(rows[0], **change)])
        # An old open interval or unresolved source-absence is not replaced.
        for change in (dict(end_date="2099-12-31"), dict(status="source_absent")):
            self.assertEqual(plan_insert(rows, [dict(rows[0], **change)])[0], rows)


if __name__ == "__main__":
    unittest.main()
