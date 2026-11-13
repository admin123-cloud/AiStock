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


def test_vendor_ad_detection_is_strict_to_tdx_web_layers():
    assert tdx._looks_like_vendor_ad("通达信信息", [])
    assert tdx._looks_like_vendor_ad("", ["AfxControl SubWebView"])
    assert not tdx._looks_like_vendor_ad("服务器设置", ["Static 设置"])


def test_main_menu_click_rejects_an_ad_or_unknown_window(monkeypatch):
    monkeypatch.setattr(tdx, "_bring_to_front", lambda _: None)
    monkeypatch.setattr(tdx, "_root", lambda hwnd: hwnd)
    calls = []
    monkeypatch.setattr(tdx, "_click_point", lambda x, y: calls.append((x, y)))
    for hit in (0, 20):
        monkeypatch.setattr(tdx, "_window_from_point", lambda x, y, hit=hit: hit)
        with pytest.raises(tdx.TdxAutomationError, match="covered"):
            tdx._click_main_point(10, 1, 2)
    assert not calls
    monkeypatch.setattr(tdx, "_window_from_point", lambda x, y: 10)
    tdx._click_main_point(10, 1, 2)
    assert calls == [(1, 2)]


def test_download_waits_for_completion_not_first_file_change(monkeypatch):
    class FakeUser32:
        def __init__(self):
            self.enabled = iter([0, 0, 1])
        def IsWindowEnabled(self, _):
            return next(self.enabled)

    snapshots = iter([
        {"files": 1, "bytes": 10, "max_mtime_ns": 1},
        {"files": 1, "bytes": 20, "max_mtime_ns": 2},
        {"files": 1, "bytes": 30, "max_mtime_ns": 3},
        {"files": 1, "bytes": 40, "max_mtime_ns": 4},
    ])
    monkeypatch.setattr(tdx, "USER32", FakeUser32())
    monkeypatch.setattr(tdx, "_find_button", lambda *_: 3)
    monkeypatch.setattr(tdx, "_click_window", lambda _: None)
    monkeypatch.setattr(tdx, "_is_visible", lambda _: True)
    monkeypatch.setattr(tdx, "_progress_position", lambda _: 1)
    monkeypatch.setattr(tdx, "_file_snapshot", lambda _: next(snapshots))
    monkeypatch.setattr(tdx.time, "sleep", lambda _: None)
    result = tdx._start_and_wait(1, Path("synthetic"), 30)
    assert result["after"]["bytes"] == 40
    assert result["evidence"] == "button_reenabled_after_disabled"
    assert result["coverage_verified"] is False


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
