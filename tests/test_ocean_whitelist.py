from __future__ import annotations

import json
from pathlib import Path

import pytest

from utils.ocean_whitelist import (
    OceanWhitelistError,
    build_stock_whitelist_snapshot,
    write_stock_whitelist_snapshot,
)


def test_stock_whitelist_uses_master_type_and_strict_canonical_code() -> None:
    snapshot = build_stock_whitelist_snapshot([
        ("600000.SH", "stock"),
        {"code": "000001.sz", "type": "stock"},
    ], generated_at="2026-09-20T08:00:00+08:00")
    assert snapshot.codes == ("000001.SZ", "600000.SH")
    assert snapshot.source_rows == 2
    assert len(snapshot.policy_hash) == 64

    with pytest.raises(OceanWhitelistError, match="invalid_canonical"):
        build_stock_whitelist_snapshot([("600000", "stock")])
    with pytest.raises(OceanWhitelistError, match="master_row_type_not_stock"):
        build_stock_whitelist_snapshot([("600000.SH", "index")])


def test_whitelist_snapshot_is_timestamped_and_hash_anchored(tmp_path: Path) -> None:
    snapshot = build_stock_whitelist_snapshot(
        [("600000.SH", "stock")], generated_at="2026-09-20T08:00:00+08:00"
    )
    evidence = write_stock_whitelist_snapshot(tmp_path, snapshot)
    directory = Path(evidence["directory"])
    assert (directory / "accepted_stock_codes.txt").read_text(encoding="utf-8") == "600000.SH\n"
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["codes_sha256"] == evidence["codes_sha256"]
    assert manifest["accepted_stock_policy_hash"] == snapshot.policy_hash
    assert manifest["formal_authorized"] is False
    with pytest.raises(OceanWhitelistError, match="already_exists"):
        write_stock_whitelist_snapshot(tmp_path, snapshot)
