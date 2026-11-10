# -*- coding: utf-8 -*-
"""scripts/intraday_snapshot_collect.py 纯单元测试（R28 盘中东财快照采集器）。

零网络/零 DB/零 subprocess：fetcher 与 client 工厂全部注入 fake。
契约（任务书 20261110_intraday_eastmoney_r28）：
  - clist/ulist 解析（后缀映射、停牌 '-' 跳过、昨收保留）；
  - 分页拉取终止条件、分页间隔 ≥3s、单页重试耗尽 → 整轮 fail-closed；
  - id 确定性（blake2b 域）；DDL 北京钟 + 按日分区 + TTL 30 日 + ReplacingMergeTree；
  - 只写 intraday_market_snapshot / intraday_index_snapshot 两表（绝不 kline_*）；
  - 读回不符 → 按精确 snapshot_ts 回滚并报 failure；退出码语义 0/3/1。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import intraday_snapshot_collect as isc  # noqa: E402

BJ = timezone(timedelta(hours=8), "Asia/Shanghai")
SNAP_TS = datetime(2026, 11, 10, 10, 35, 0, tzinfo=BJ)


def _stock_row(code="600519", f13=1, price=1436.2, pct=-0.61, name="贵州茅台"):
    return {"f2": price, "f3": pct, "f5": 21000, "f6": 3.0e9, "f12": code,
            "f13": f13, "f14": name, "f18": 1444.99}


def _index_row(code="000001", price=3671.35, pct=0.29):
    return {"f2": price, "f3": pct, "f4": 10.56, "f5": 1.7e8, "f6": 3.0e11,
            "f12": code, "f13": 1, "f14": "上证指数", "f18": 3660.79}


# ── 解析 ──────────────────────────────────────────────────────

class TestNormalize:
    def test_stock_suffix_mapping_sh_sz_bj(self):
        rows, skipped = isc.normalize_stock_rows(
            [_stock_row("600519", 1), _stock_row("000001", 0), _stock_row("920002", 0)],
            SNAP_TS, SNAP_TS,
        )
        codes = [r[0] for r in rows]
        assert codes == ["600519.SH", "000001.SZ", "920002.BJ"]
        assert skipped == 0

    def test_bj_prefixes_covered(self):
        rows, _ = isc.normalize_stock_rows(
            [_stock_row(p, 0) for p in ("430047", "830799", "873169", "920118")],
            SNAP_TS, SNAP_TS,
        )
        assert {r[0] for r in rows} == {"430047.BJ", "830799.BJ", "873169.BJ", "920118.BJ"}

    def test_suspended_dash_price_skipped(self):
        rows, skipped = isc.normalize_stock_rows(
            [_stock_row("920680", 0, price="-"), _stock_row("600519", 1)],
            SNAP_TS, SNAP_TS,
        )
        assert len(rows) == 1 and skipped == 1

    def test_fields_and_id_determinism(self):
        rows, _ = isc.normalize_stock_rows([_stock_row()], SNAP_TS, SNAP_TS)
        row = rows[0]
        assert row[1] == "贵州茅台"
        assert row[2] == 1436.2 and row[3] == -0.61 and row[6] == 1444.99
        assert row[7] == SNAP_TS and row[8] == SNAP_TS
        again, _ = isc.normalize_stock_rows([_stock_row()], SNAP_TS, SNAP_TS)
        assert row[9] == again[0][9]
        assert row[9] != isc.snapshot_id(isc.STOCK_TABLE, "600519.SH", SNAP_TS + timedelta(minutes=5))

    def test_index_rows_frozen_ten(self):
        rows = isc.normalize_index_rows([_index_row(c) for c in (
            "000001", "399001", "399106", "000300", "000016",
            "000905", "000852", "399006", "000680", "000688")], SNAP_TS, SNAP_TS)
        assert len(rows) == 10
        assert rows[0][0] == "000001.SH" and rows[1][0] == "399001.SZ"
        assert rows[0][5] == 3660.79  # prev_close f18
        # 非冻结码被拒
        rows2 = isc.normalize_index_rows([_index_row("880003")], SNAP_TS, SNAP_TS)
        assert rows2 == []

    def test_index_no_price_skipped(self):
        rows = isc.normalize_index_rows([_index_row(price="-")], SNAP_TS, SNAP_TS)
        assert rows == []


# ── 分页与重试 ────────────────────────────────────────────────

class FakeFetcher:
    INDEX_CODES = ("000001", "399001", "399106", "000300", "000016",
                   "000905", "000852", "399006", "000680", "000688")

    def __init__(self, pages, index_payload=None, fail_pages=()):
        self.pages = {i + 1: p for i, p in enumerate(pages)}
        if index_payload is None:
            index_payload = {"data": {"diff": [
                {"f2": 3671.35, "f3": 0.29, "f4": 10.56, "f5": 1.7e8, "f6": 3.0e11,
                 "f12": code, "f13": 1, "f14": f"指数{code}", "f18": 3660.79}
                for code in self.INDEX_CODES
            ]}}
        self.index_payload = index_payload
        self.fail_pages = set(fail_pages)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if "ulist" in url:
            import json
            return self.index_payload
        if "pn=" in url:
            pn = int(url.split("pn=")[1].split("&")[0])
            if pn in self.fail_pages:
                raise OSError("boom")
            if pn in self.pages:
                import json
                return self.pages[pn]
        raise AssertionError(f"unexpected url {url}")


def _page(pn, total, rows):
    return {"data": {"total": total, "diff": rows if rows else None}}


class TestPagination:
    def test_multi_page_and_interval(self):
        sleeps = []
        fetcher = FakeFetcher([
            _page(1, 250, [_stock_row("600519", 1)] * 100),
            _page(2, 250, [_stock_row("000001", 0)] * 100),
            _page(3, 250, [_stock_row("920002", 0)] * 50),
        ])
        rows, total = isc.collect_market_pages(fetcher, sleep_fn=sleeps.append)
        assert total == 250 and len(rows) == 250
        assert sleeps == [3.0, 3.0]  # 页间 ≥3s（任务书限频边界）

    def test_url_contract(self):
        fetcher = FakeFetcher([_page(1, 1, [_stock_row()])])
        isc.collect_market_pages(fetcher, sleep_fn=lambda _s: None)
        url = fetcher.urls[0]
        assert "pz=100" in url and "fltt=2" in url and isc.CLIST_FS in url
        assert isc.CLIST_FIELDS in url

    def test_incomplete_pagination_fail_closed(self):
        # total=300 但第 3 页提前空 → RuntimeError（不允许部分快照）
        fetcher = FakeFetcher([
            _page(1, 300, [_stock_row()] * 100),
            _page(2, 300, [_stock_row()] * 100),
            _page(3, 300, []),
        ])
        with pytest.raises(RuntimeError):
            isc.collect_market_pages(fetcher, sleep_fn=lambda _s: None)

    def test_page_retry_exhausted_raises(self):
        fetcher = FakeFetcher([_page(1, 1, [_stock_row()])], fail_pages={1})
        sleeps = []
        with pytest.raises(RuntimeError):
            isc.fetch_page_with_retry(isc.build_clist_url(1), fetcher,
                                      backoff_sec=3.0, sleep_fn=sleeps.append)
        assert sleeps == [3.0, 3.0]  # 3 次尝试、2 次退避


# ── run()：写库目标与回滚 ─────────────────────────────────────

class FakeClient:
    def __init__(self, readback_stocks=None, readback_indices=None,
                 fail_on_insert=False, fail_table=None):
        self.commands = []
        self.inserts = []
        self.readback_stocks = readback_stocks
        self.readback_indices = readback_indices
        self.fail_on_insert = fail_on_insert
        self.fail_table = fail_table

    def command(self, sql):
        self.commands.append(sql)
        if "SELECT count()" in sql:
            if isc.STOCK_TABLE in sql:
                return self.readback_stocks
            return self.readback_indices
        return None

    def insert(self, table, rows, column_names=None):
        if self.fail_on_insert and (self.fail_table is None or self.fail_table == table):
            raise RuntimeError("insert boom")
        self.inserts.append((table, list(rows), list(column_names or [])))


class FakeLock:
    def __init__(self):
        self.acquired = []
        self.released = []

    def acquire(self, scope, owner, purpose=""):
        self.acquired.append((scope, owner, purpose))
        return {"scope": scope, "owner": owner}

    def release(self, scope, owner):
        self.released.append((scope, owner))


def _market(n=1200):
    return [_stock_row(str(600000 + i), 1) for i in range(n)]


def _run_with(fetcher, client, tmp_path, now_fn=lambda: SNAP_TS):
    lock = FakeLock()
    monkey_lock = pytest.MonkeyPatch()
    import utils.db_write_lock as lock_mod
    monkey_lock.setattr(lock_mod, "DbWriteLock", lambda _c: lock)
    import utils.paths as paths_mod
    monkey_lock.setattr(paths_mod, "data_root", lambda: tmp_path)
    try:
        report = isc.run(
            fetcher=fetcher,
            client_factory=lambda: client,
            out_dir=tmp_path / "intraday_snapshot",
            sleep_fn=lambda _s: None,
            now_fn=now_fn,
        )
    finally:
        monkey_lock.undo()
    return report, lock


class TestRun:
    def test_success_writes_only_two_tables_never_kline(self, tmp_path):
        fetcher = FakeFetcher([
            _page(1, 1200, _market(1200)),
            _page(2, 1200, []),
        ])
        client = FakeClient(readback_stocks=1200, readback_indices=10)
        report, lock = _run_with(fetcher, client, tmp_path)
        assert report["status"] == "success"
        assert isc.exit_code(report) == 0
        # 只写两张运营表（fail-closed：绝不 kline_*）
        tables = {t for t, _rows, _c in client.inserts}
        assert tables == {isc.STOCK_TABLE, isc.INDEX_TABLE}
        assert not any("kline" in t for t, _r, _c in client.inserts)
        # 锁 scope 独立于盘后 daily 锁
        assert lock.acquired[0][0] == isc.LOCK_SCOPE
        assert lock.released == [(isc.LOCK_SCOPE, isc.LOCK_OWNER)]
        # 建表 DDL 已执行
        ddl_text = "\n".join(client.commands)
        assert "intraday_market_snapshot" in ddl_text and "intraday_index_snapshot" in ddl_text

    def test_source_unreachable_rc3_zero_partial_write(self, tmp_path):
        fetcher = FakeFetcher([_page(1, 1, [])], fail_pages={1})
        client = FakeClient()
        report, _ = _run_with(fetcher, client, tmp_path)
        assert report["status"] == "failure" and report["reason"] == "source_unreachable"
        assert isc.exit_code(report) == 3
        assert client.inserts == []  # 零部分写入

    def test_readback_mismatch_rollback_and_fail(self, tmp_path):
        fetcher = FakeFetcher([_page(1, 1200, _market(1200)), _page(2, 1200, [])])
        client = FakeClient(readback_stocks=1199, readback_indices=10)
        report, _ = _run_with(fetcher, client, tmp_path)
        assert report["status"] == "failure" and report["reason"] == "readback_mismatch"
        assert isc.exit_code(report) == 1
        rollback_sql = "\n".join(client.commands)
        assert rollback_sql.count("ALTER TABLE") == 2
        assert "WHERE snapshot_ts = toDateTime('2026-11-10 10:35:00', 'Asia/Shanghai')" in rollback_sql

    def test_insert_exception_rolls_back_written_tables(self, tmp_path):
        fetcher = FakeFetcher([_page(1, 1200, _market(1200)), _page(2, 1200, [])])
        # 明细已写入、指数 insert 失败 → 仅回滚已写入的明细表
        client = FakeClient(readback_stocks=1200, readback_indices=10,
                            fail_on_insert=True, fail_table=isc.INDEX_TABLE)
        report, _ = _run_with(fetcher, client, tmp_path)
        assert report["status"] == "failure" and report["reason"] == "write_error"
        alter_sqls = [c for c in client.commands if "ALTER TABLE" in c]
        assert len(alter_sqls) == 1 and isc.STOCK_TABLE in alter_sqls[0]

    def test_first_insert_fail_nothing_written_no_rollback(self, tmp_path):
        fetcher = FakeFetcher([_page(1, 1200, _market(1200)), _page(2, 1200, [])])
        client = FakeClient(fail_on_insert=True, fail_table=isc.STOCK_TABLE)
        report, _ = _run_with(fetcher, client, tmp_path)
        assert report["status"] == "failure" and report["reason"] == "write_error"
        assert not any("ALTER TABLE" in c for c in client.commands)

    def test_insufficient_rows_fail_closed(self, tmp_path):
        fetcher = FakeFetcher([_page(1, 900, _market(900)), _page(2, 900, [])])
        client = FakeClient()
        report, _ = _run_with(fetcher, client, tmp_path)
        assert report["status"] == "failure" and report["reason"] == "insufficient_rows"
        assert client.inserts == []

    def test_report_file_written(self, tmp_path):
        fetcher = FakeFetcher([_page(1, 1200, _market(1200)), _page(2, 1200, [])])
        client = FakeClient(readback_stocks=1200, readback_indices=10)
        _run_with(fetcher, client, tmp_path)
        reports = list((tmp_path / "intraday_snapshot").glob("report_*.json"))
        assert len(reports) == 1


# ── DDL 契约（北京钟 / 分区 / TTL）─────────────────────────────

class TestDDL:
    @pytest.mark.parametrize("ddl,table", [(isc.STOCK_DDL, isc.STOCK_TABLE),
                                           (isc.INDEX_DDL, isc.INDEX_TABLE)])
    def test_beijing_clock_partition_ttl(self, ddl, table):
        assert "DateTime('Asia/Shanghai')" in ddl
        assert "PARTITION BY toDate(snapshot_ts)" in ddl
        assert "TTL toDateTime(snapshot_ts) + INTERVAL 30 DAY DELETE" in ddl
        assert "ReplacingMergeTree(created_at)" in ddl
        assert "CREATE TABLE IF NOT EXISTS" in ddl and table in ddl

    def test_columns_contract(self):
        assert isc.STOCK_COLUMNS == (
            "code", "name", "price", "change_pct", "volume", "amount", "prev_close",
            "snapshot_ts", "created_at", "id")
        assert "change_amount" in isc.INDEX_COLUMNS

    def test_index_secids_frozen_ten_only(self):
        assert len(isc.INDEX_SECIDS) == 10
        assert {s for _, s in isc.INDEX_SECIDS} == {
            "000001.SH", "399001.SZ", "399106.SZ", "000300.SH", "000016.SH",
            "000905.SH", "000852.SH", "399006.SZ", "000680.SH", "000688.SH"}

    def test_rate_limit_boundary(self):
        assert isc.PAGE_INTERVAL_SEC >= 3.0
        assert isc.PAGE_SIZE == 100
