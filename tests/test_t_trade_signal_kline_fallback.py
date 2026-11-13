# -*- coding: utf-8 -*-
"""t_trade_signal.fetch_east_klines 502 回退链单测（2026-11-13 腾讯真源改造）。

链序（东财 WAF 事故后定序）：
  ① vendored 腾讯分钟线（真 bar，_tencent_fallback_klines）
  ② CH 本地 5min 聚合（_ch_fallback_klines，降为第三级）
  ③ 腾讯 qt 快照伪 bar（_qt_snapshot_klines，末级）
全部纯 mock，不发真实请求。
"""
from __future__ import annotations

import io
import sys
import urllib.error
from datetime import datetime
from email.message import Message
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import t_trade_signal as tts  # noqa: E402


def _http_502():
    msg = Message()
    msg["Content-Type"] = "text/html"
    return urllib.error.HTTPError(
        "http://push2his.eastmoney.com/api/qt/stock/kline/get",
        502, "Bad Gateway", msg, io.BytesIO(b"<html>502</html>"))


@pytest.fixture()
def east_502(monkeypatch):
    """让东财 push2his 恒抛 502。"""
    def _urlopen(req, timeout=None):
        raise _http_502()
    monkeypatch.setattr(tts.urllib.request, "urlopen", _urlopen)


def test_tencent_fallback_maps_period_and_marks_no_amount(monkeypatch):
    """①级：vendored 腾讯分钟线 → [{ts, open, close, volume, amount=0}]（真 bar）。"""
    import utils.market_sources.tencent_kline as tk_mod
    monkeypatch.setattr(
        tk_mod, "fetch_minute_kline",
        lambda code, period, count: {
            "rows": [
                {"datetime": "2026-11-13 14:45", "open": 10.0, "high": 10.2,
                 "low": 9.9, "close": 10.1, "volume": 1200.0},
                {"datetime": "2026-11-13 15:00", "open": 10.1, "high": 10.3,
                 "low": 10.0, "close": 10.2, "volume": 800.0},
            ]})
    rows = tts._tencent_fallback_klines("300054", 15, 120)
    assert rows == [
        {"ts": "2026-11-13 14:45:00", "open": 10.0, "close": 10.1,
         "volume": 1200.0, "amount": 0.0},
        {"ts": "2026-11-13 15:00:00", "open": 10.1, "close": 10.2,
         "volume": 800.0, "amount": 0.0},
    ]
    # 腾讯无成交额 → amount=0 如实置零（day_vwap 返回 None，不做假 VWAP）
    assert tts.day_vwap(rows) is None


def test_tencent_fallback_unknown_klt_returns_empty():
    assert tts._tencent_fallback_klines("300054", 3, 120) == []


def test_qt_snapshot_klines_builds_single_pseudo_bar(monkeypatch):
    """③级：qt 快照 → 一根 O=H=L=C=现价伪 bar。"""
    parts = [f"f{i}" for i in range(50)]
    parts[1] = "测试股"
    parts[3] = "12.34"
    parts[4] = "12.00"
    parts[6] = "5600"      # 手
    parts[31] = "0.34"     # 涨跌额
    parts[32] = "2.83"     # 涨跌%
    parts[37] = "700.5"    # 额（万）

    class _Resp:
        def __init__(self):
            self._text = 'v_sz300054="' + "~".join(parts) + '";'

        def read(self):
            return self._text.encode("gbk")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(tts.urllib.request, "urlopen", lambda req, timeout=None: _Resp())
    rows = tts._qt_snapshot_klines("300054", 15)
    assert len(rows) == 1
    bar = rows[0]
    assert bar["open"] == bar["close"] == 12.34
    assert bar["volume"] == 5600.0
    assert bar["amount"] == pytest.approx(7005000.0)     # 万 × 10000
    assert bar["ts"].endswith(":00") and len(bar["ts"]) == 19   # YYYY-MM-DD HH:MM:SS


class Test502Chain:
    def test_level1_tencent_wins(self, east_502, monkeypatch):
        called = []
        monkeypatch.setattr(tts, "_tencent_fallback_klines",
                            lambda c, k, l: called.append("tencent") or [
                                {"ts": "2026-11-13 15:00:00", "open": 1, "close": 1,
                                 "volume": 1, "amount": 0}])
        monkeypatch.setattr(tts, "_ch_fallback_klines",
                            lambda c, k, l=120: called.append("ch") or [])
        monkeypatch.setattr(tts, "_qt_snapshot_klines",
                            lambda c, k, l=120: called.append("qt") or [])
        rows = tts.fetch_east_klines("300054", 15)
        assert called == ["tencent"]
        assert rows and rows[0]["ts"] == "2026-11-13 15:00:00"

    def test_level2_ch_when_tencent_raises_or_empty(self, east_502, monkeypatch):
        called = []
        monkeypatch.setattr(tts, "_tencent_fallback_klines",
                            lambda c, k, l: called.append("tencent") or [])
        monkeypatch.setattr(tts, "_ch_fallback_klines",
                            lambda c, k, l=120: called.append("ch") or [
                                {"ts": "2026-11-13 14:45:00", "open": 2, "close": 2,
                                 "volume": 2, "amount": 2}])
        monkeypatch.setattr(tts, "_qt_snapshot_klines",
                            lambda c, k, l=120: called.append("qt") or [])
        rows = tts.fetch_east_klines("300054", 15)
        assert called == ["tencent", "ch"]
        assert rows[0]["close"] == 2

        # tencent 抛异常同样降级到 CH（不中断链）
        called.clear()
        def _boom(c, k, l):
            called.append("tencent")
            raise RuntimeError("三个入口均不可用")
        monkeypatch.setattr(tts, "_tencent_fallback_klines", _boom)
        rows = tts.fetch_east_klines("300054", 15)
        assert called == ["tencent", "ch"]
        assert rows[0]["close"] == 2

    def test_level3_qt_last_resort(self, east_502, monkeypatch):
        called = []
        monkeypatch.setattr(tts, "_tencent_fallback_klines",
                            lambda c, k, l: called.append("tencent") or [])
        monkeypatch.setattr(tts, "_ch_fallback_klines",
                            lambda c, k, l=120: called.append("ch") or [])
        monkeypatch.setattr(tts, "_qt_snapshot_klines",
                            lambda c, k, l=120: called.append("qt") or [
                                {"ts": "2026-11-13 15:00:00", "open": 3, "close": 3,
                                 "volume": 3, "amount": 3}])
        rows = tts.fetch_east_klines("300054", 15)
        assert called == ["tencent", "ch", "qt"]
        assert rows[0]["close"] == 3

    def test_all_levels_empty_reraises_502(self, east_502, monkeypatch):
        monkeypatch.setattr(tts, "_tencent_fallback_klines", lambda c, k, l: [])
        monkeypatch.setattr(tts, "_ch_fallback_klines", lambda c, k, l=120: [])
        monkeypatch.setattr(tts, "_qt_snapshot_klines", lambda c, k, l=120: [])
        with pytest.raises(urllib.error.HTTPError):
            tts.fetch_east_klines("300054", 15)

    def test_qt_failure_reraises_502(self, east_502, monkeypatch):
        monkeypatch.setattr(tts, "_tencent_fallback_klines", lambda c, k, l: [])
        monkeypatch.setattr(tts, "_ch_fallback_klines", lambda c, k, l=120: [])

        def _qt_boom(c, k, l=120):
            raise RuntimeError("qt down")
        monkeypatch.setattr(tts, "_qt_snapshot_klines", _qt_boom)
        with pytest.raises(urllib.error.HTTPError):
            tts.fetch_east_klines("300054", 15)

    def test_non_502_http_error_no_fallback(self, monkeypatch):
        """非 502（如 403）不走回退链，原样上抛。"""
        def _urlopen(req, timeout=None):
            raise urllib.error.HTTPError("u", 403, "Forbidden", Message(), io.BytesIO(b""))
        monkeypatch.setattr(tts.urllib.request, "urlopen", _urlopen)
        monkeypatch.setattr(tts, "_tencent_fallback_klines",
                            lambda *a: pytest.fail("不应触发回退"))
        with pytest.raises(urllib.error.HTTPError):
            tts.fetch_east_klines("300054", 15)
