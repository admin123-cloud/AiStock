# -*- coding: utf-8 -*-
"""scripts/minute_backfill_window.py 的管道测试（R16 窗口泛化；fake client，零真实 DB）。"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import minute_backfill_1021_1030 as mb  # noqa: E402
import minute_backfill_window as mw     # noqa: E402
from utils import tdx_lc5 as lc5        # noqa: E402

UTC = timezone.utc
CN = mb.CN_TZ
CH_1102_1105 = ("2026-11-02 00:00:00", "2026-11-07 00:00:00")  # R27b 北京钟字面量（与旧 UTC 形式同瞬间）


# ── 测试夹具 ───────────────────────────────────────────────────

class FakeClient:
    """镜像 tests/test_minute_backfill_1021_1030.py 的 FakeClient（按本管道查询形态裁剪）。"""

    def __init__(self, missing=(("2026-11-02", "000698.SZ"),), trading_days=None,
                 existing_keys=(), probe_date=datetime(2026, 11, 2, 9, 35)):
        self.queries = []
        self.commands = []
        self.inserts = []
        self.stored = {}
        self.lock_tables = set()
        self.lock_markers = {}
        self.missing = list(missing)
        self.trading_days = list(trading_days or ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"])
        self.existing_keys = list(existing_keys)
        self.probe_sid = mb.minute_row_id("000698.SZ", probe_date, "5m")

    def query(self, sql, settings=None):
        self.queries.append(sql)
        if "ORDER BY acquired_at DESC LIMIT 1" in sql:
            table = sql.split("FROM stock.")[1].split()[0].split(".")[-1]
            row = self.lock_markers.get(table)
            cols = ["scope", "owner", "purpose", "acquired_at", "host", "pid"]
            return _Result(cols, [] if row is None else [row])
        if "system.tables" in sql:
            name = sql.split("name='")[1].split("'")[0]
            return _Result(["table_count"], [1 if name in self.lock_tables else 0])
        if "LEFT ANTI JOIN" in sql:
            return _Result(["td", "code"], self.missing)
        if "is_trading" in sql:
            asked = re.findall(r"'(\d{4}-\d{2}-\d{2})'", sql)
            return _Result(["d", "it"], [(d, 1) for d in asked if d in self.trading_days])
        if "FROM stock.kline_daily WHERE trade_date IN" in sql:
            return _Result(["d", "code", "open", "high", "low", "close", "volume", "amount"],
                           [("2026-11-02", "000698.SZ", 10.0, 10.5, 9.8, 10.2, 4800.0, 4_800_000.0)])
        if "cityHash64" in sql:
            return _Result(["sid"], [(self.probe_sid,)])
        if "count() AS n" in sql and "id IN" in sql:
            return _Result(["n"], [0])  # 回滚核验：按 id 已删净
        if "count() AS n" in sql and "GROUP BY d, code" in sql:
            table = sql.split("FROM stock.")[1].split()[0].split(" FINAL")[0]
            agg = {}
            for r in self.stored.get(table, []):
                day = r[1].astimezone(mb.CN_TZ).strftime("%Y-%m-%d")
                n, v, a = agg.get((day, r[0]), (0, 0.0, 0.0))
                agg[(day, r[0])] = (n + 1, v + r[6], a + r[7])
            return _Result(["d", "code", "n", "v", "a"],
                           [(d, c, n, v, a) for (d, c), (n, v, a) in sorted(agg.items())])
        if "DISTINCT code" in sql:
            # existing_keys 以 (day, code) 传入；列名顺序为 (code, d)
            return _Result(["code", "d"], [(c, d) for d, c in self.existing_keys])
        if "toString(toDateTime(datetime, 'Asia/Shanghai'))" in sql:
            table = sql.split("FROM stock.")[1].split()[0]
            code = sql.split("code = '")[1].split("'")[0]
            rows = [(r[1].astimezone(mb.CN_TZ).strftime("%Y-%m-%d %H:%M"),
                     r[2], r[3], r[4], r[5], r[6], r[7])
                    for r in self.stored.get(table, []) if r[0] == code]
            return _Result(["dt", "open", "high", "low", "close", "volume", "amount"], rows)
        raise AssertionError(f"unexpected query: {sql}")

    def command(self, sql):
        self.commands.append(sql)
        if sql.startswith("CREATE TABLE stock.write_lock_"):
            name = sql.split()[2]
            if name in self.lock_tables:
                raise RuntimeError("table exists")
            self.lock_tables.add(name)
            return ""
        if sql.startswith("INSERT INTO stock.write_lock_"):
            name = sql.split()[2].split(".")[-1]
            values = sql.split("VALUES (")[1].rstrip(")").split(", ")
            self.lock_markers[name] = tuple(v.strip("'") for v in values)
            return ""
        if sql.startswith("DROP TABLE stock.write_lock_"):
            name = sql.split()[2].split(".")[-1]
            self.lock_tables.discard(name)
            self.lock_markers.pop(name, None)
            return ""
        return ""

    def insert(self, table, rows, column_names=None):
        self.inserts.append((table, list(rows)))
        self.stored.setdefault(table, []).extend(rows)
        return


class _Result:
    def __init__(self, names, rows):
        self.column_names = names
        self.result_rows = rows


def _write_synthetic_vipdoc(tmp_path, code="000698.SZ", file_days=(datetime(2026, 11, 2),),
                            ohlc=(10.0, 10.5, 9.8, 10.2), amount=100_000.0, shares=10_000):
    """合成 lc5：每个 file_day 各 48 根合规 bar。"""
    from tests.test_tdx_lc5 import _full_day_bars, pack_record  # noqa: PLC0415
    num, suffix = code.split(".")
    p = tmp_path / "vipdoc" / suffix.lower() / "fzline"
    p.mkdir(parents=True, exist_ok=True)
    recs = []
    for day in file_days:
        bars = _full_day_bars(code, day)
        recs.extend(pack_record(b.datetime, *ohlc, amount, shares) for b in bars)
    (p / f"{suffix.lower()}{num}.lc5").write_bytes(b"".join(recs))
    return tmp_path / "vipdoc"


def _daily_ref(days=("2026-11-02",), code="000698.SZ"):
    return {(d, code): (10.0, 10.5, 9.8, 10.2, 4800.0, 4_800_000.0) for d in days}


def _make_rows_5m(tmp_path, days=("2026-11-02",), file_days=None):
    """合成 vipdoc → parse_and_gate → 只取 5m 候选行。"""
    root = _write_synthetic_vipdoc(
        tmp_path, file_days=[datetime.fromisoformat(d) if isinstance(d, str) else d
                              for d in (file_days if file_days is not None else days)])
    keys = [(d, "000698.SZ") for d in days]
    candidates, rejects = mb.parse_and_gate(keys, _daily_ref(days), root, progress_every=0)
    return candidates["5m"], rejects


# ── 窗口参数解析 ───────────────────────────────────────────────

def test_ch_window_parity_with_1021_1030():
    # 同一公式必须复现原模块硬编码窗口（10-21~10-30）→ 证明镜像无漂移
    from datetime import date
    assert mw.ch_window(date(2026, 10, 21), date(2026, 10, 30)) == tuple(mb.CH_WINDOW)
    assert mw.ch_window(date(2026, 11, 2), date(2026, 11, 5)) == CH_1102_1105


def test_resolve_window_valid_1102_1105():
    win = mw.resolve_window("2026-11-02", "2026-11-05")
    assert win["days"] == ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]
    assert win["span_days"] == 4
    assert tuple(win["ch_window"]) == CH_1102_1105


@pytest.mark.parametrize("bad", ["2026/11/02", "11-02", "2026-13-01", "2026-11-32", "", "20261102"])
def test_resolve_window_rejects_bad_format(bad):
    with pytest.raises(SystemExit):
        mw.resolve_window(bad, "2026-11-05")


def test_resolve_window_rejects_inverted_range():
    with pytest.raises(SystemExit):
        mw.resolve_window("2026-11-05", "2026-11-02")


def test_resolve_window_rejects_span_over_default_16():
    with pytest.raises(SystemExit):
        mw.resolve_window("2026-11-01", "2026-11-17")  # 17 天 > 默认 16
    ok = mw.resolve_window("2026-11-01", "2026-11-16")  # 恰 16 天
    assert ok["span_days"] == 16
    lifted = mw.resolve_window("2026-11-01", "2026-11-17", max_window_days=20)
    assert lifted["span_days"] == 17


# ── 窗口与 lc5 数据的裁剪 ──────────────────────────────────────

def test_fetch_missing_keys_sql_uses_parameterized_window():
    client = FakeClient(missing=(("2026-11-03", "600000.SH"),))
    keys = mw.fetch_missing_keys(client, "stock.kline_minute_5",
                                 ["2026-11-02", "2026-11-05"], CH_1102_1105)
    assert keys == [("2026-11-03", "600000.SH")]
    sql = client.queries[0]
    assert "toDateTime('2026-11-02 00:00:00', 'Asia/Shanghai')" in sql
    assert "toDateTime('2026-11-07 00:00:00', 'Asia/Shanghai')" in sql
    assert "'2026-11-02'" in sql and "'2026-11-05'" in sql
    assert mb.CH_WINDOW[0] not in sql and mb.CH_WINDOW[1] not in sql  # 非 1021_1030 硬编码


def test_lc5_pruned_to_requested_window_days(tmp_path):
    # 文件含 11-02 与 11-06 两天，但窗口只请求 11-02 → 只产出 11-02 的 48 根
    rows, rejects = _make_rows_5m(tmp_path, days=("2026-11-02",),
                                  file_days=("2026-11-02", "2026-11-06"))
    assert rejects == []
    assert len(rows) == 48
    assert {r[1].astimezone(CN).strftime("%Y-%m-%d") for r in rows} == {"2026-11-02"}


def test_trading_days_gate_filters_non_trading_and_fails_on_empty():
    client = FakeClient(trading_days=["2026-11-02"])
    assert mw.trading_days_gate(client, ["2026-10-31", "2026-11-01", "2026-11-02"]) == ["2026-11-02"]
    with pytest.raises(SystemExit):
        mw.trading_days_gate(FakeClient(trading_days=[]), ["2026-10-31", "2026-11-01"])


def test_daily_reference_uses_requested_days_not_default_window():
    # 防回归：fetch_daily_reference 的 days 默认值是 1021_1030 硬编码窗口，必须显式传窗口
    client = FakeClient()
    mw.fetch_daily_reference_checked(client, ["000698.SZ"], ["2026-11-02", "2026-11-05"])
    sql = client.queries[-1]
    assert "'2026-11-02'" in sql and "'2026-11-05'" in sql
    assert mb.WINDOW_DAYS[0] not in sql  # 未回落到默认 10-21 窗口


# ── 默认零库访问 ───────────────────────────────────────────────

def test_main_invalid_window_never_touches_db(monkeypatch):
    def boom():
        raise AssertionError("窗口校验失败前发生了库访问")
    monkeypatch.setattr(mw, "_default_client_factory", boom)
    with pytest.raises(SystemExit):
        mw.main(["--mode", "audit", "--start-date", "2026-11-02", "--end-date", "2026-11-20"])
    with pytest.raises(SystemExit):
        mw.main(["--mode", "audit", "--start-date", "not-a-date", "--end-date", "2026-11-05"])


def test_main_dry_run_end_to_end_with_fake_client(tmp_path, monkeypatch):
    _make_rows_5m(tmp_path, days=("2026-11-02",))
    client = FakeClient(trading_days=["2026-11-02"])
    monkeypatch.setattr(mw, "_default_client_factory", lambda: client)
    out = tmp_path / "out"
    rc = mw.main(["--mode", "dry-run", "--start-date", "2026-11-02", "--end-date", "2026-11-02",
                  "--no-crosscheck", "--tdx-root", str(tmp_path / "vipdoc"), "--out-dir", str(out)])
    assert rc == 0
    report = json.loads((out / "dry_run_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "dry_run_ok"
    assert report["plan"]["rows"] == 48 and report["plan"]["code_days"] == 1
    assert report["id_probe"]["python"] == report["id_probe"]["server"]
    assert client.inserts == []  # dry-run 零写入


# ── formal_write（fake client，仅 5m 单表）─────────────────────

def test_formal_write_success_only_5m_table(tmp_path):
    rows, rejects = _make_rows_5m(tmp_path)
    assert rejects == [] and len(rows) == 48
    client = FakeClient()
    events = []
    report = mw.formal_write(lambda: client, rows, CH_1102_1105, lambda e: events.append(e))
    assert report["status"] == "ok"
    assert [t for t, _ in client.inserts] == ["kline_minute_5"]  # 只写 5m，绝不写 15/30/60
    ev = [e.get("event") for e in events]
    assert ev[0] == "lock_acquired" and ev[-1] == "lock_released"
    assert "anti_join_clean" in ev and "rollback_start" not in ev
    assert report["tables"]["5m"]["rows"] == 48 and report["tables"]["5m"]["code_days"] == 1


def test_formal_write_stale_existing_keys_aborts_without_insert(tmp_path):
    rows, _ = _make_rows_5m(tmp_path)
    client = FakeClient(existing_keys=[("2026-11-02", "000698.SZ")])
    events = []
    with pytest.raises(RuntimeError, match="反连接发现存量键"):
        mw.formal_write(lambda: client, rows, CH_1102_1105, lambda e: events.append(e))
    assert client.inserts == []  # fail-closed：未插入任何行
    assert events[-1]["event"] == "lock_released"


def test_formal_write_readback_failure_triggers_rollback(tmp_path):
    rows, _ = _make_rows_5m(tmp_path)
    client = FakeClient()
    calls = {"n": 0}
    orig = client.query

    def query(sql, settings=None):
        if "count() AS n" in sql and "GROUP BY d, code" in sql:
            calls["n"] += 1
            if calls["n"] == 1:  # raw 读回返回空 → 读回缺失
                return _Result(["d", "code", "n", "v", "a"], [])
        return orig(sql, settings)

    client.query = query
    events = []
    with pytest.raises(RuntimeError, match="读回验证失败"):
        mw.formal_write(lambda: client, rows, CH_1102_1105, lambda e: events.append(e))
    ev = [e.get("event") for e in events]
    assert "rollback_start" in ev and "rollback_delete" in ev
    assert any("DELETE WHERE id IN" in c for c in client.commands), "回滚必须按精确 id 列表删除"


# ── 腾讯交叉验证（无 10-21 硬编码过滤）────────────────────────

def test_crosscheck_sample_covers_window_days():
    def fetcher(symbol):
        return {"data": {symbol: {"m5": [["202611020935", "10.00", "10.02", "10.03", "9.99", "1000.00"],
                                          ["202611020940", "10.02", "10.04", "10.05", "10.01", "1100.00"]]}}}

    created = datetime(2026, 11, 9, tzinfo=UTC)
    bars = [lc5.LC5Record("600000.SH", datetime(2026, 11, 2, 9, 35), 10.00, 10.03, 9.99, 10.02, 1000, 1_000_000.0),
            lc5.LC5Record("600000.SH", datetime(2026, 11, 2, 9, 40), 10.02, 10.05, 10.01, 10.04, 1100, 1_100_000.0)]
    candidates = {"5m": [mb.build_row(b, "5m", created) for b in bars]}
    result = mw.crosscheck_sample(candidates, [("2026-11-02", "600000.SH")],
                                  ["600000.SH"], fetcher=fetcher)
    assert result["pass_days"] == 1 and result["fail_days"] == 0


def test_crosscheck_no_tencent_coverage_reported():
    def fetcher(symbol):  # 腾讯只覆盖 10-30，窗口日 11-02 无覆盖
        return {"data": {symbol: {"m5": [["202610300935", "10.00", "10.02", "10.03", "9.99", "1000.00"]]}}}

    created = datetime(2026, 11, 9, tzinfo=UTC)
    bar = lc5.LC5Record("600000.SH", datetime(2026, 11, 2, 9, 35), 10.0, 10.03, 9.99, 10.02, 1000, 1_000_000.0)
    result = mw.crosscheck_sample({"5m": [mb.build_row(bar, "5m", created)]},
                                  [("2026-11-02", "600000.SH")], ["600000.SH"], fetcher=fetcher)
    assert result["detail"][0]["status"] == "no_tencent_coverage"


def test_crosscheck_price_mismatch_flagged():
    def fetcher(symbol):
        return {"data": {symbol: {"m5": [["202611020935", "9.00", "9.01", "9.02", "8.99", "100.00"]]}}}

    created = datetime(2026, 11, 9, tzinfo=UTC)
    bar = lc5.LC5Record("600000.SH", datetime(2026, 11, 2, 9, 35), 10.0, 10.05, 9.99, 10.02, 100, 1000.0)
    result = mw.crosscheck_sample({"5m": [mb.build_row(bar, "5m", created)]},
                                  [("2026-11-02", "600000.SH")], ["600000.SH"], fetcher=fetcher)
    assert result["fail_days"] == 1 and result["detail"][0]["status"] == "mismatch"


def test_pick_sample_uses_all_window_days_excludes_bj():
    keys = [("2026-11-02", "600000.SH"), ("2026-11-03", "000001.SZ"), ("2026-11-02", "920002.BJ")]
    sample = mw.pick_sample(keys, n_sh=1, n_sz=1)
    assert "600000.SH" in sample and "000001.SZ" in sample
    assert "920002.BJ" not in sample
