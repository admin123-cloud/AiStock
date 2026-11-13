# -*- coding: utf-8 -*-
"""intraday_snapshot_collect 腾讯 qt 批量 fallback 单测（2026-11-13 WAF 事故后转正）。

覆盖：字段映射（parts[3]/[32]/[6]/[37]×10000/[4]/[1]/[31]）、A 股号段白名单、
clist/ulist mimic 分页、run() 东财不可达自动切 qt（同表/同锁/同幂等 id）。
全部 mock，不发真实请求。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import intraday_snapshot_collect as isc  # noqa: E402

BEIJING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
SNAP_TS = datetime(2026, 11, 13, 10, 35, 0, tzinfo=BEIJING_TZ)


def _qt_line(key, price="10.50", chg_pct="1.23", vol="12345", amt_wan="600.5",
             pre_close="10.37", name="测试股", chg_amt="0.13"):
    parts = [f"x{i}" for i in range(50)]
    parts[1] = name
    parts[3] = price
    parts[4] = pre_close
    parts[6] = vol
    parts[31] = chg_amt
    parts[32] = chg_pct
    parts[37] = amt_wan
    return f'v_{key}="' + "~".join(parts) + '";'


# ── 纯映射函数 ─────────────────────────────────────────────────

class TestQtMapping:
    def test_market_prefix(self):
        assert isc.qt_market_prefix("600000") == "sh"
        assert isc.qt_market_prefix("688981") == "sh"
        assert isc.qt_market_prefix("000001") == "sz"
        assert isc.qt_market_prefix("300054") == "sz"
        assert isc.qt_market_prefix("920982") == "bj"

    def test_parse_qt_payload_skips_bad_lines(self):
        text = _qt_line("sz300054") + "garbage;" + _qt_line("sh600000") + ";"
        out = isc.parse_qt_payload(text)
        assert set(out) == {"sz300054", "sh600000"}
        assert len(out["sz300054"]) == 50

    def test_clist_item_field_mapping(self):
        out = isc.parse_qt_payload(_qt_line("sz300054"))
        item = isc.qt_row_to_clist_item(out["sz300054"], "300054")
        assert item == {
            "f2": 10.5,            # parts[3] 现价
            "f3": 1.23,            # parts[32] 涨跌%
            "f5": 12345.0,         # parts[6] 量（手）
            "f6": 6005000.0,       # parts[37] 额万 ×10000
            "f12": "300054",
            "f13": 0,              # 深市
            "f14": "测试股",       # parts[1] 名称
            "f18": 10.37,          # parts[4] 昨收
        }

    def test_clist_item_sh_flag_and_bj(self):
        out = isc.parse_qt_payload(_qt_line("sh600000") + _qt_line("bj920982"))
        assert isc.qt_row_to_clist_item(out["sh600000"], "600000")["f13"] == 1
        bj = isc.qt_row_to_clist_item(out["bj920982"], "920982")
        assert bj["f13"] == 0       # 经 stock_code_with_suffix → 920 前缀 → .BJ
        assert isc.stock_code_with_suffix(bj["f12"], bj["f13"]) == "920982.BJ"

    def test_no_price_row_skipped(self):
        out = isc.parse_qt_payload(_qt_line("sz300054", price="0.00"))
        assert isc.qt_row_to_clist_item(out["sz300054"], "300054") is None

    def test_ulist_item_field_mapping(self):
        out = isc.parse_qt_payload(_qt_line("sh000001"))
        item = isc.qt_row_to_ulist_item(out["sh000001"], "000001")
        assert item["f4"] == pytest.approx(0.13)     # parts[31] 涨跌额
        assert item["f2"] == 10.5
        assert item["f6"] == pytest.approx(6005000.0)
        assert item["f12"] == "000001"

    def test_extract_a_share_plain_codes_whitelist_and_dedupe(self):
        rows = ["600000.SH", "600000.SH", "000001.SZ", "300054.SZ", "920982.BJ",
                "000001.SH",          # 冻结指数（R18）剔除
                "900901.SH",          # B 股剔除
                "510300.SH",          # ETF 剔除
                "204001.SH",          # 逆回购剔除
                "", None]
        assert isc.extract_a_share_plain_codes(rows) == ["000001", "300054", "600000", "920982"]


# ── build_qt_fetcher：clist 分页 / ulist mimic ────────────────────

class FakeQueryClient:
    """带 query()（qt 代码表）的 FakeClient 扩展。"""

    def __init__(self, codes):
        self.codes = codes
        self.commands = []
        self.inserts = []
        self.readback_stocks = None
        self.readback_indices = None

    class _Res:
        def __init__(self, rows):
            self.result_rows = rows

    def query(self, sql):
        assert "kline_daily" in sql
        return self._Res([(c,) for c in self.codes])

    def command(self, sql):
        self.commands.append(sql)
        if "SELECT count()" in sql:
            if isc.STOCK_TABLE in sql:
                return self.readback_stocks
            return self.readback_indices
        return None

    def insert(self, table, rows, column_names=None):
        self.inserts.append((table, list(rows), list(column_names or [])))


def _fake_url_open(codes_by_key):
    calls = []

    class _Resp:
        def __init__(self, text):
            self._text = text

        def read(self):
            return self._text.encode("gbk")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def open_(req, timeout=None):
        url = req.full_url
        assert url.startswith(isc.QT_URL)
        keys = url[len(isc.QT_URL):].split(",")
        calls.append(keys)
        assert len(keys) <= isc.QT_BATCH_SIZE, "批量不得超过 80 码/请求"
        text = ";".join(_qt_line(k) for k in keys)
        return _Resp(text)

    open_.calls = calls
    return open_


class TestQtFetcher:
    def test_clist_paging_mimics_east_shape(self):
        codes = ["300054", "600000", "920982"]
        url_open = _fake_url_open(None)
        fetch = isc.build_qt_fetcher(
            client_factory=lambda: FakeQueryClient(codes),
            url_open=url_open, sleep_fn=lambda _s: None)
        payload = fetch(isc.build_clist_url(1))
        assert payload["data"]["total"] == 3
        diff = payload["data"]["diff"]
        assert [d["f12"] for d in diff] == ["300054", "600000", "920982"]
        # 分页切片语义与东财一致
        p2 = fetch(isc.build_clist_url(2))          # pn=2, pz=100 → 空
        assert p2["data"]["diff"] == []
        # 一次拉全后代码表缓存，第二次调用不再打 CH
        fetch(isc.build_clist_url(1))

    def test_batch_size_80_allows_market_sweep(self):
        codes = [f"6{i:05d}" for i in range(200)]   # 200 只 → 3 批
        url_open = _fake_url_open(None)
        isc.build_qt_fetcher(
            client_factory=lambda: FakeQueryClient(codes),
            url_open=url_open, sleep_fn=lambda _s: None)(isc.build_clist_url(1))
        assert [len(b) for b in url_open.calls] == [80, 80, 40]

    def test_ulist_returns_frozen_ten_indices(self):
        url_open = _fake_url_open(None)
        fetch = isc.build_qt_fetcher(
            client_factory=lambda: FakeQueryClient([]),
            url_open=url_open, sleep_fn=lambda _s: None)
        payload = fetch(isc.build_ulist_url())
        plains = [d["f12"] for d in payload["data"]["diff"]]
        expected = sorted({sfx.split(".")[0] for _, sfx in isc.INDEX_SECIDS})
        assert sorted(plains) == expected
        # qt 键市场前缀正确（sh000680 不落成 sz000680）
        keys = url_open.calls[0]
        assert "sh000680" in keys and "sz399001" in keys

    def test_unknown_url_rejected(self):
        fetch = isc.build_qt_fetcher(client_factory=lambda: FakeQueryClient([]),
                                     sleep_fn=lambda _s: None)
        with pytest.raises(RuntimeError, match="未知 url"):
            fetch("https://example.com/other")


# ── run()：东财不可达自动切 qt ───────────────────────────────────

class TestRunQtFallback:
    def test_auto_fallback_same_tables_same_lock_same_ids(self, tmp_path, monkeypatch):
        """东财失败 → qt fallback：成功写两张运营表、锁 scope 不变、id 与东财链同域。"""
        codes = [f"6{i:05d}" for i in range(1200)]          # ≥1000 过行数门
        client = FakeQueryClient(codes)
        client.readback_stocks = 1200
        client.readback_indices = 10

        def _east_dead(url):
            raise RuntimeError("Remote end closed connection without response")
        monkeypatch.setattr(isc, "_default_fetch_json", _east_dead)

        url_open = _fake_url_open(None)
        monkeypatch.setattr(isc, "fetch_qt_batch",
                            lambda prefixed, url_open=None: isc.parse_qt_payload(
                                ";".join(_qt_line(k) for k in prefixed)))

        class FakeLock:
            def __init__(self):
                self.acquired = []
                self.released = []

            def acquire(self, scope, owner, purpose=""):
                self.acquired.append(scope)
                return {"scope": scope}

            def release(self, scope, owner):
                self.released.append(scope)

        lock = FakeLock()
        import utils.db_write_lock as lock_mod
        monkeypatch.setattr(lock_mod, "DbWriteLock", lambda _c: lock)
        import utils.paths as paths_mod
        monkeypatch.setattr(paths_mod, "data_root", lambda: tmp_path)

        report = isc.run(client_factory=lambda: client,
                         out_dir=tmp_path / "snap", sleep_fn=lambda _s: None,
                         now_fn=lambda: SNAP_TS)
        assert report["status"] == "success"
        assert report["source"] == "tencent_qt_batch"
        assert report["valid_stock_rows"] == 1200
        # 同两张运营表、同锁 scope、同幂等 id 域（与东财链一致）
        tables = {t for t, _r, _c in client.inserts}
        assert tables == {isc.STOCK_TABLE, isc.INDEX_TABLE}
        assert lock.acquired == [isc.LOCK_SCOPE] and lock.released == [isc.LOCK_SCOPE]
        stock_rows = next(r for t, r, _c in client.inserts if t == isc.STOCK_TABLE)
        assert stock_rows[0][isc.STOCK_COLUMNS.index("id")] == \
            isc.snapshot_id(isc.STOCK_TABLE, "600000.SH", SNAP_TS)
        assert isc.exit_code(report) == 0

    def test_both_sources_dead_fail_closed_rc3(self, tmp_path, monkeypatch):
        """东财 + qt 都死 → failure/source_unreachable，rc=3，零写入。"""
        client = FakeQueryClient([])
        client.readback_stocks = 0

        def _east_dead(url):
            raise RuntimeError("east WAF")
        monkeypatch.setattr(isc, "_default_fetch_json", _east_dead)

        def _qt_dead(prefixed, url_open=None):
            raise RuntimeError("qt dead")
        monkeypatch.setattr(isc, "fetch_qt_batch", _qt_dead)

        import utils.db_write_lock as lock_mod
        monkeypatch.setattr(lock_mod, "DbWriteLock",
                            lambda _c: type("L", (), {
                                "acquire": lambda s, o, purpose="": {},
                                "release": lambda s, o: None})())
        import utils.paths as paths_mod
        monkeypatch.setattr(paths_mod, "data_root", lambda: tmp_path)

        report = isc.run(client_factory=lambda: client,
                         out_dir=tmp_path / "snap", sleep_fn=lambda _s: None,
                         now_fn=lambda: SNAP_TS)
        assert report["status"] == "failure"
        assert report["reason"] == "source_unreachable"
        assert "east:" in report["error"] and "qt_fallback:" in report["error"]
        assert isc.exit_code(report) == 3
        assert client.inserts == []

    def test_explicit_fetcher_failure_no_implicit_fallback(self, tmp_path, monkeypatch):
        """显式注入的 fetcher 失败 → 不隐式切 qt（注入语义保持）。"""
        called = []
        monkeypatch.setattr(isc, "fetch_qt_batch",
                            lambda prefixed, url_open=None: called.append(1) or {})
        client = FakeQueryClient([])
        report, _ = _run_explicit_dead_fetcher(client, tmp_path)
        assert report["status"] == "failure"
        assert report["reason"] == "source_unreachable"
        assert called == []                     # qt 从未被触发


def _run_explicit_dead_fetcher(client, tmp_path):
    import utils.db_write_lock as lock_mod
    import utils.paths as paths_mod
    lock = type("L", (), {"acquire": staticmethod(lambda s, o, purpose="": {}),
                          "release": staticmethod(lambda s, o: None)})()
    mp = pytest.MonkeyPatch()
    mp.setattr(lock_mod, "DbWriteLock", lambda _c: lock)
    mp.setattr(paths_mod, "data_root", lambda: tmp_path)
    try:
        def dead_fetcher(url):
            raise RuntimeError("injected dead fetcher")
        report = isc.run(fetcher=dead_fetcher, client_factory=lambda: client,
                         out_dir=tmp_path / "snap", sleep_fn=lambda _s: None,
                         now_fn=lambda: SNAP_TS)
    finally:
        mp.undo()
    return report, lock
