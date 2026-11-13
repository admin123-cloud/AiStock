"""Pure contract checks for the Windows-only TDX post-close automation."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import tdx_postclose_client as tdx  # noqa: E402


def test_normal_label_removes_accelerator_and_menu_hint():
    assert tdx._normal_label("数据导出\t34") == "数据导出 34"
    assert tdx._normal_label("&登录") == "登录"
    assert tdx._normal_label("  盘后数据下载  ") == "盘后数据下载"


def test_file_snapshot_uses_only_lc5_metadata(tmp_path):
    path = tmp_path / "vipdoc" / "sh" / "fzline" / "sh000001.lc5"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x" * 32)
    snap = tdx._file_snapshot(tmp_path)
    assert snap["files"] == 1
    assert snap["bytes"] == 32
    assert snap["max_mtime_ns"] > 0


def test_automation_refuses_non_windows(monkeypatch):
    monkeypatch.setattr(tdx, "IS_WINDOWS", False)
    monkeypatch.setattr(tdx, "USER32", None)
    with pytest.raises(tdx.TdxAutomationError, match="requires Windows"):
        tdx._require_windows()


def test_source_never_handles_credentials_or_wm_settext():
    source = Path(tdx.__file__).read_text(encoding="utf-8")
    assert "WM_SETTEXT" not in source
    assert "credentials_touched\": False" in source
