# -*- coding: utf-8 -*-
"""scripts/intraday_kline_upsert.py 单测（R58 腾讯真源改造）。

覆盖：当日已收盘 bar 过滤（周期结束边界口径）、估算额公式、确定性 id、
主源写入/降级链、TDX 对账计算。全部 mock，不发真实请求/不写真实库。
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import intraday_kline_upsert as up  # noqa: E402

BEIJING_TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 13, 14, 3, 0, tzinfo=BEIJING_TZ)   # 盘中 14:03
TODAY = "2026-11-13"


def _bar(dt_text, o=10.0, h=10.2, l=9.9, c=10.1, v=1200.0):
    return {"datetime": dt_text, "open": o, "high": h, "low": l, "close": c, "volume": v}


# ── 纯函数 ─────────────────────────────────────────────────────

class TestClosedTodayBars:
    def test_keeps_today_closed_and_drops_future_boundary(self):
        rows = [
            _bar("2026-11-12 15:00"),                 # 历史日：不写（TDX 权威）
            _bar("2026-11-13 09:35"),
            _bar("2026-11-13 14:00"),                 # 边界 14:00 <= 14:03 已收盘
            _bar("2026-11-13 14:05"),                 # 边界 14:05 > 14:03 未收盘 → 丢弃
        ]
        out = up.closed_today_bars(rows, TODAY, NOW)
        assert [r["datetime"] for r in out] == ["2026-11-13 09:35", "2026-11-13 14:00"]

    def test_boundary_equal_to_now_is_closed(self):
        rows = [_bar("2026-11-13 14:03")]
        assert len(up.closed_today_bars(rows, TODAY, NOW)) == 1

    def test_non_trade_day_returns_empty(self):
        assert up.closed_today_bars([_bar("2026-11-12 15:00")], TODAY, NOW) == []


class TestAmountAndId:
    def test_estimate_amount_close_times_lots_times_100(self):
        # 估算口径：close × volume(手) × 100（腾讯无成交额字段）
        assert up.estimate_amount(58.93, 2636) == pytest.approx(15533948.0)

    def test_bar_row_id_deterministic_and_distinct(self):
        a1 = up.bar_row_id("300054", "2026-11-13 09:35:00")
        a2 = up.bar_row_id("300054", "2026-11-13 09:35:00")
        b = up.bar_row_id("300054", "2026-11-13 09:40:00")
        c = up.bar_row_id("002015", "2026-11-13 09:35:00")
        assert a1 == a2 and a1 != b and a1 != c
        assert 0 <= a1 < 2 ** 64

    def test_build_insert_rows_full_shape(self):
        rows = up.build_insert_rows(
            [_bar("2026-11-13 09:35", o=10.0, h=10.2, l=9.9, c=10.1, v=1200.0)],
            "300054", created_at="2026-11-13 14:03:00")
        r = rows[0]
        assert r["code"] == "300054"                       # 裸码（盘中命名空间）
        assert r["datetime"] == "2026-11-13 09:35:00"      # 周期结束边界
        assert (r["open"], r["high"], r["low"], r["close"]) == (10.0, 10.2, 9.9, 10.1)
        assert r["volume"] == 1200                         # 手
        assert r["amount"] == pytest.approx(10.1 * 1200 * 100)
        assert r["created_at"] == "2026-11-13 14:03:00"
        assert r["id"] == up.bar_row_id("300054", "2026-11-13 09:35:00")


# ── 主链路（fetch/insert 全 mock） ─────────────────────────────

def _patched(monkeypatch, **names):
    for name, value in names.items():
        monkeypatch.setattr(up, name, value)


class TestRunUpsert:
    def test_primary_tencent_path_writes_today_closed(self, monkeypatch, tmp_path):
        inserted = []
        _patched(
            monkeypatch,
            fetch_tencent_m5=lambda code: {"rows": [
                _bar("2026-11-12 15:00"),                 # 历史日只在窗口里，不写
                _bar("2026-11-13 09:35"), _bar("2026-11-13 14:00"),
                _bar("2026-11-13 14:05"),                 # 未收盘
            ]},
            insert_rows=lambda rows: (len(rows), "ok") or inserted.extend(rows),
            reconcile_against_tdx=lambda fetched, now, out_dir=None: {
                "codes": {c: {"ok": True} for c in fetched}, "overall_ok": True,
                "report_path": str(tmp_path / "r.json")},
        )
        results = up.run_upsert(codes=["300054"], reconcile=True, now=NOW)
        assert results["300054"]["status"] == "ok"
        assert results["300054"]["rows"] == 2             # 只写当日已收盘 2 根
        assert results["300054"]["reconcile_ok"] is True
        assert results["_reconcile"]["overall_ok"] is True

    def test_fallback_qt_when_tencent_fails(self, monkeypatch):
        def _dead(code):
            raise RuntimeError("三个入口均不可用")
        _patched(
            monkeypatch,
            fetch_tencent_m5=_dead,
            fetch_qt_snapshot=lambda code: {"price": 10.5, "volume": 900, "amount": 950000.0},
            insert_rows=lambda rows: (len(rows), "ok"),
        )
        results = up.run_upsert(codes=["300054"], reconcile=False)
        assert results["300054"]["status"] == "fallback_qt"
        assert results["300054"]["rows"] == 1

    def test_fetch_failed_when_both_sources_dead(self, monkeypatch):
        def _dead_t(code):
            raise RuntimeError("tencent down")
        def _dead_q(code):
            return None
        _patched(monkeypatch, fetch_tencent_m5=_dead_t, fetch_qt_snapshot=_dead_q,
                 insert_rows=lambda rows: (0, "should_not_be_called"))
        results = up.run_upsert(codes=["300054"], reconcile=False)
        assert results["300054"]["status"] in ("no_data", "fetch_failed")

    def test_no_data_when_market_closed_all_day(self, monkeypatch):
        _patched(monkeypatch,
                 fetch_tencent_m5=lambda code: {"rows": [_bar("2026-11-12 15:00")]},
                 insert_rows=lambda rows: (0, "no_data"))
        results = up.run_upsert(codes=["300054"], reconcile=False)
        assert results["300054"]["status"] == "no_data"


# ── TDX 对账 ───────────────────────────────────────────────────

class TestReconcile:
    def test_violations_counted_and_report_written(self, monkeypatch, tmp_path):
        tdx = {
            "2026-11-13 09:35:00": {"open": 10.0, "high": 10.2, "low": 9.9,
                                    "close": 10.1, "volume": 1200.0},
            "2026-11-13 09:40:00": {"open": 10.1, "high": 10.5, "low": 10.0,
                                    "close": 10.4, "volume": 1300.0},
            "2026-11-13 09:45:00": {"open": 10.4, "high": 10.6, "low": 10.3,
                                    "close": 10.5, "volume": 2000.0},
        }
        monkeypatch.setattr(up, "load_tdx_bars", lambda code, dates: tdx)
        fetched = {"300054": [
            _bar("2026-11-13 09:35"),                            # 全等 → ok
            _bar("2026-11-13 09:40", h=10.5, l=10.0, c=10.4, v=1300.0),  # high 差 0→ok
            _bar("2026-11-13 09:45", h=10.6, c=10.4, v=2500.0),  # close差0.1/10.5≈0.95%>0.1%价违规; 量差25%违规
            _bar("2026-11-13 14:00"),                            # TDX 无此 bar → missing
        ]}
        report = up.reconcile_against_tdx(fetched, NOW, out_dir=tmp_path)
        day = report["codes"]["300054"]["dates"]["2026-11-13"]
        assert day["compared"] == 3
        assert day["price_violations"] >= 1
        assert day["volume_violations"] == 1
        assert day["missing_in_tdx"] == 1
        assert day["ok"] is False
        assert report["overall_ok"] is False
        # 报告文件落盘且可回读
        path = Path(report["report_path"])
        assert path.exists() and path.parent == tmp_path
        assert json.loads(path.read_text(encoding="utf-8"))["overall_ok"] is False

    def test_clean_match_ok(self, monkeypatch, tmp_path):
        tdx = {"2026-11-13 09:35:00": {"open": 10.0, "high": 10.2, "low": 9.9,
                                       "close": 10.1, "volume": 1200.0}}
        monkeypatch.setattr(up, "load_tdx_bars", lambda code, dates: tdx)
        report = up.reconcile_against_tdx({"300054": [_bar("2026-11-13 09:35")]},
                                          NOW, out_dir=tmp_path)
        assert report["overall_ok"] is True
        assert report["codes"]["300054"]["ok"] is True

    def test_volume_whole_lot_rounding_within_tolerance(self, monkeypatch, tmp_path):
        """腾讯最近一日整手 vs TDX 精确手：±1 手差异在 3% 容差内不违规。"""
        tdx = {"2026-11-13 09:35:00": {"open": 10.0, "high": 10.2, "low": 9.9,
                                       "close": 10.1, "volume": 1200.0}}
        monkeypatch.setattr(up, "load_tdx_bars", lambda code, dates: tdx)
        report = up.reconcile_against_tdx({"300054": [_bar("2026-11-13 09:35", v=1201.0)]},
                                          NOW, out_dir=tmp_path)
        assert report["overall_ok"] is True
