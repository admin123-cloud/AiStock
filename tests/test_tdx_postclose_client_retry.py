"""Synthetic, isolated tests for TDX download retry safety.

These tests are intentionally limited to pure policy and monkeypatched Win32
seams.  They do not start TDX, touch runtime files, use the network, or access a
数据库.
"""

from types import SimpleNamespace

import pytest

from scripts import tdx_postclose_client as client


def test_activity_without_button_disable_blocks_retry_as_uncertain():
    result = client._classify_download_failure(
        dialog_visible=True,
        saw_disabled=False,
        saw_progress=True,
        saw_file_change=True,
    )

    assert result["state"] == "uncertain_in_progress"
    assert result["retry_safe"] is False
    assert result["retry_blocked_reason"] == "activity_seen_without_completion_evidence"


def test_start_click_retries_once_only_after_proven_no_click(monkeypatch):
    fake_user32 = SimpleNamespace(IsWindowEnabled=lambda hwnd: True)
    monkeypatch.setattr(client, "IS_WINDOWS", True)
    monkeypatch.setattr(client, "USER32", fake_user32)
    monkeypatch.setattr(client, "_is_visible", lambda hwnd: True)
    monkeypatch.setattr(client, "_find_button", lambda *args, **kwargs: 11)
    monkeypatch.setattr(client, "_unexpected_download_modal", lambda dialog: None)

    calls = []

    def click_once_then_succeed(hwnd):
        calls.append(hwnd)
        if len(calls) == 1:
            raise client.TdxClickError("covered before click", click_sent=False)

    monkeypatch.setattr(client, "_click_window", click_once_then_succeed)
    monkeypatch.setattr(client.time, "sleep", lambda seconds: None)

    assert client._click_download_start(7) == 2
    assert calls == [11, 11]


def test_start_click_stops_on_unknown_modal(monkeypatch):
    fake_user32 = SimpleNamespace(IsWindowEnabled=lambda hwnd: True)
    monkeypatch.setattr(client, "IS_WINDOWS", True)
    monkeypatch.setattr(client, "USER32", fake_user32)
    monkeypatch.setattr(client, "_is_visible", lambda hwnd: True)
    monkeypatch.setattr(client, "_find_button", lambda *args, **kwargs: 11)
    monkeypatch.setattr(client, "_unexpected_download_modal", lambda dialog: 99)
    calls = []

    def covered(hwnd):
        calls.append(hwnd)
        raise client.TdxClickError("covered before click", click_sent=False)

    monkeypatch.setattr(client, "_click_window", covered)

    with pytest.raises(client.TdxDownloadError) as caught:
        client._click_download_start(7)

    assert calls == [11]
    assert caught.value.state == "failed"
    assert caught.value.retry_safe is False
    assert caught.value.retry_blocked_reason == "unexpected_modal_visible:99"


def test_download_error_report_preserves_uncertainty_and_evidence():
    error = client.TdxDownloadError(
        "background activity remains possible",
        state="uncertain_in_progress",
        retry_safe=False,
        retry_blocked_reason="activity_seen_without_completion_evidence",
        evidence={"progress_seen": True, "files_changed": True},
    )

    assert error.report_fields() == {
        "download_state": "uncertain_in_progress",
        "retry_safe": False,
        "retry_blocked_reason": "activity_seen_without_completion_evidence",
        "download_evidence": {"progress_seen": True, "files_changed": True},
    }
