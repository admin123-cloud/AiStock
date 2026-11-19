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


def test_download_completes_when_button_text_restores_without_disabling(monkeypatch):
    class FakeUser32:
        def IsWindowEnabled(self, _):
            return 1

    labels = iter(["取消下载", "取消下载", "开始下载"])
    snapshots = iter([
        {"files": 1, "bytes": 10, "max_mtime_ns": 1},
        {"files": 1, "bytes": 20, "max_mtime_ns": 2},
        {"files": 2, "bytes": 40, "max_mtime_ns": 3},
        {"files": 2, "bytes": 40, "max_mtime_ns": 3},
    ])
    monkeypatch.setattr(tdx, "USER32", FakeUser32())
    monkeypatch.setattr(tdx, "_find_button", lambda *_: 3)
    monkeypatch.setattr(tdx, "_click_window", lambda _: None)
    monkeypatch.setattr(tdx, "_is_visible", lambda _: True)
    monkeypatch.setattr(tdx, "_safe_button_label", lambda _: next(labels))
    monkeypatch.setattr(tdx, "_progress_position", lambda _: 50)
    monkeypatch.setattr(tdx, "_file_snapshot", lambda _: next(snapshots))
    monkeypatch.setattr(tdx.time, "sleep", lambda _: None)

    result = tdx._start_and_wait(1, Path("synthetic"), 30)
    assert result["evidence"] == "button_text_restored_after_download"
    assert result["files_changed"] is True
    assert result["after"]["bytes"] == 40


def test_download_completes_after_quiet_period_when_button_never_disables(monkeypatch):
    class FakeUser32:
        def IsWindowEnabled(self, _):
            return 1

    # 模拟单调时钟：初始0s，轮询依次为 2s（有写入）、4s（写入完毕）、10s（静默6s>=5s）
    clock = iter([0.0, 2.0, 2.0, 4.0, 4.0, 10.0, 10.0])
    snapshots = iter([
        {"files": 1, "bytes": 10, "max_mtime_ns": 1},
        {"files": 2, "bytes": 30, "max_mtime_ns": 2},
        {"files": 3, "bytes": 60, "max_mtime_ns": 3},
        {"files": 3, "bytes": 60, "max_mtime_ns": 3},
    ])
    positions = iter([50, 100, 100])
    monkeypatch.setattr(tdx, "USER32", FakeUser32())
    monkeypatch.setattr(tdx, "_find_button", lambda *_: 3)
    monkeypatch.setattr(tdx, "_click_window", lambda _: None)
    monkeypatch.setattr(tdx, "_is_visible", lambda _: True)
    monkeypatch.setattr(tdx, "_safe_button_label", lambda _: "开始下载")
    monkeypatch.setattr(tdx, "_progress_position", lambda _: next(positions))
    monkeypatch.setattr(tdx, "_file_snapshot", lambda _: next(snapshots))
    monkeypatch.setattr(tdx.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(tdx.time, "sleep", lambda _: None)

    result = tdx._start_and_wait(1, Path("synthetic"), 60, quiet_seconds=5.0)
    assert result["evidence"] == "files_settled_after_progress"
    assert result["files_changed"] is True
    assert result["after"]["bytes"] == 60


def test_download_still_times_out_if_no_file_change_observed(monkeypatch):
    class FakeUser32:
        def IsWindowEnabled(self, _):
            return 1

    clock = iter([0.0, 2.0, 2.0, 10.0, 10.0, 35.0])
    fixed = {"files": 1, "bytes": 10, "max_mtime_ns": 1}
    monkeypatch.setattr(tdx, "USER32", FakeUser32())
    monkeypatch.setattr(tdx, "_find_button", lambda *_: 3)
    monkeypatch.setattr(tdx, "_click_window", lambda _: None)
    monkeypatch.setattr(tdx, "_is_visible", lambda _: True)
    monkeypatch.setattr(tdx, "_safe_button_label", lambda _: "开始下载")
    monkeypatch.setattr(tdx, "_progress_position", lambda _: 50)
    monkeypatch.setattr(tdx, "_file_snapshot", lambda _: dict(fixed))
    monkeypatch.setattr(tdx.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(tdx.time, "sleep", lambda _: None)

    with pytest.raises(tdx.TdxAutomationError, match="TDX download timeout"):
        tdx._start_and_wait(1, Path("synthetic"), 30, quiet_seconds=5.0)
