"""l2_order_filler 与 order_prefill 关键修复回归单测（纯隔离，零触碰同花顺窗口）。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_bridges import l2_order_filler as filler  # noqa: E402
from services.g3.order_prefill import evaluate_and_build_prefill_draft  # noqa: E402

BJ_TZ = timezone(timedelta(hours=8))


def test_find_window_never_passes_timeout_kwarg(monkeypatch):
    calls: list[dict] = []

    def fake_find_windows(**kwargs):
        assert "timeout" not in kwargs
        calls.append(kwargs)
        return [123456]

    class FakeApp:
        def __init__(self, backend=None):
            self.backend = backend

        def connect(self, handle=None):
            self.handle = handle
            return self

    monkeypatch.setattr(filler, "_PYWINSUCCESS", True)
    monkeypatch.setattr(filler, "Application", FakeApp, raising=False)
    monkeypatch.setattr(
        filler,
        "pywinauto",
        SimpleNamespace(findwindows=SimpleNamespace(find_windows=fake_find_windows)),
        raising=False,
    )

    app, hwnd, err = filler._find_window(wait_sec=0.1)
    assert err is None
    assert hwnd == 123456
    assert len(calls) >= 1


def test_identify_edits_prefers_ths_fixed_aids():
    def _mk(aid: str, name: str = ""):
        return SimpleNamespace(element_info=SimpleNamespace(automation_id=aid, name=name))

    edits = [_mk("9999", "杂项"), _mk("1034", ""), _mk("1032", ""), _mk("1033", "")]
    labeled = filler._identify_edits(edits)
    assert labeled["code"].element_info.automation_id == "1032"
    assert labeled["price"].element_info.automation_id == "1033"
    assert labeled["qty"].element_info.automation_id == "1034"


def test_verify_filled_values_strict():
    ok, errs = filler._verify_filled_values({"code": "512890", "price": "1.264", "qty": "2900"}, "512890", 1.264, 2900)
    assert ok and errs == []

    ok2, errs2 = filler._verify_filled_values({"code": "512890", "price": "1.300", "qty": "2900"}, "512890", 1.264, 2900)
    assert not ok2 and any("价格不符" in e for e in errs2)


def test_order_prefill_allows_1440_etf_and_panic_stock(tmp_path):
    dt_1440 = datetime(2026, 11, 26, 14, 40, 0, tzinfo=BJ_TZ)
    r_etf = evaluate_and_build_prefill_draft(
        code="512890",
        name="红利低波ETF",
        side="BUY",
        price=1.26,
        shares=2000,
        order_type="base_rebalance",
        account_id="acct1",
        live_price=1.26,
        available_cash=10000.0,
        market_gate={"allow_new_mainwave": False, "regime_label": "防守退潮"},
        now_bj=dt_1440,
        draft_dir=tmp_path,
        persist=False,
    )
    assert r_etf["approved"] is True

    r_panic = evaluate_and_build_prefill_draft(
        code="002015",
        name="协鑫能科",
        side="BUY",
        price=10.0,
        shares=500,
        order_type="base_rebalance",
        account_id="acct1",
        live_price=10.0,
        available_cash=10000.0,
        market_gate={"allow_new_mainwave": False, "is_panic_ab_active": True, "regime_label": "冰点触发"},
        now_bj=dt_1440,
        draft_dir=tmp_path,
        persist=False,
    )
    assert r_panic["approved"] is True


def test_match_account_supports_both_label_and_masked_holder_id():
    assert filler._match_account_from_text("广发证券-曾*") == "acct1"
    assert filler._match_account_from_text("广发证券－王*美") == "acct2"
    assert filler._match_account_from_text("A5****1851") == "acct1"
    assert filler._match_account_from_text("沪A A6****5646") == "acct2"
    assert filler._match_account_from_text("编辑账户") is None


def test_score_panel_window_prefers_yuanhang_and_identified_account(monkeypatch):
    monkeypatch.setattr(
        filler,
        "_win32_process_exe",
        lambda h: r"D:\同花顺远航\同花顺远航版\transaction\xiadan.exe" if h == 200 else r"D:\同花顺\xiadan.exe",
    )
    monkeypatch.setattr(filler, "_win32_rect", lambda h: (0, 0, 1200, 800))
    monkeypatch.setattr(
        filler,
        "_read_live_account_win32",
        lambda h: {"account": "acct1"} if h == 200 else {"account": None},
    )
    s_old, _ = filler._score_panel_window(100)
    s_yh, _ = filler._score_panel_window(200)
    assert s_yh > s_old


def test_sanitize_ocr_numeric_strips_cursor_noise():
    assert filler._sanitize_ocr_numeric("512890|", is_code=True) == "512890"
    assert filler._sanitize_ocr_numeric("1.261", is_code=False) == "1.261"
    assert filler._sanitize_ocr_numeric("100l", is_code=False) == "100"
    assert filler._sanitize_ocr_numeric(" 2,900I ", is_code=False) == "2900"


def test_account_1004_overrides_cosmetic_2322_mismatch():
    fake_info = {
        "account": "acct2",
        "account_1004": "acct2",
        "account_2322": "acct1",
    }
    assert filler._is_account_truly_active(fake_info, "acct1") is False
    assert filler._is_account_truly_active(fake_info, "acct2") is True



