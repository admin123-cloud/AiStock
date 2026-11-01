# -*- coding: utf-8 -*-
"""scripts/minute_backfill_1021_1030.py 的管道测试（fake client，零真实 DB）。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import minute_backfill_1021_1030 as mb  # noqa: E402
from utils import tdx_lc5 as lc5  # noqa: E402

CN = mb.CN_TZ
UTC = timezone.utc


# ── 测试夹具 ───────────────────────────────────────────────────

class FakeClient:
    def __init__(self):
        self.queries = []
        self.commands = []
        self.inserts = []          # (table, rows)
        self.stored = {}           # table -> rows（读回聚合用）
        self.lock_tables = set()
        self.lock_markers = {}     # table -> metadata tuple

    def query(self, sql, settings=None):
        self.queries.append(sql)
        if "ORDER BY acquired_at DESC LIMIT 1" in sql:
            table = sql.split("FROM stock.")[1].split()[0].split(".")[-1]
            row = self.lock_markers.get(table)
            if row is None:
                return _Result(["scope", "owner", "purpose", "acquired_at", "host", "pid"], [])
            return _Result(["scope", "owner", "purpose", "acquired_at", "host", "pid"], [row])
        if "system.tables" in sql:
            name = sql.split("name='")[1].split("'")[0]
            return _Result(["table_count"], [1 if name in self.lock_tables else 0])
        if "LEFT ANTI JOIN" in sql:
            # 审计反连接：返回固定缺失集（day, code）
            return _Result(["td", "code"], [("2026-10-21", "000698.SZ")])
        if "is_trading" in sql:
            return _Result(["d", "it"], [(d, 1) for d in mb.WINDOW_DAYS])
        if "FROM stock.kline_daily WHERE trade_date IN" in sql:
            return _Result(["d", "code", "open", "high", "low", "close", "volume", "amount"],
                           [("2026-10-21", "000698.SZ", 10.0, 10.5, 9.8, 10.2, 4800.0, 4_800_000.0)])
        if "cityHash64" in sql:
            return _Result(["sid"], [8365171336205711208])
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
            return _Result(["code", "d"], [])
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


def _make_candidates(tmp_path):
    """构造一个合规 (code, day) 的合成 lc5 文件并跑 parse_and_gate。"""
    from tests.test_tdx_lc5 import _full_day_bars, pack_record  # noqa: PLC0415
    p = tmp_path / "vipdoc" / "sz" / "fzline"
    p.mkdir(parents=True)
    bars = _full_day_bars("000698.SZ", datetime(2026, 10, 21))
    recs = [pack_record(b.datetime, 10.0, 10.5, 9.8, 10.2, 100_000.0, 10_000)
            for b in bars]
    (p / "sz000698.lc5").write_bytes(b"".join(recs))
    daily_ref = {("2026-10-21", "000698.SZ"): (10.0, 10.5, 9.8, 10.2, 4800.0, 4_800_000.0)}
    keys = [("2026-10-21", "000698.SZ")]
    return mb.parse_and_gate(keys, daily_ref, tmp_path / "vipdoc", progress_every=0)


# ── 门禁 ───────────────────────────────────────────────────────

def test_minute_row_id_deterministic_and_period_distinct():
    dt = datetime(2026, 10, 21, 9, 35)
    a = mb.minute_row_id("000698.SZ", dt, "5m")
    assert a == mb.minute_row_id("000698.SZ", dt, "5m")
    assert a != mb.minute_row_id("000698.SZ", dt, "15m")
    assert a != mb.minute_row_id("600000.SH", dt, "5m")


def test_build_row_shape_and_tz():
    bar = lc5.LC5Record("000698.SZ", datetime(2026, 10, 21, 9, 35), 1, 2, 0.5, 1.5, 300, 1000.0)
    created = datetime(2026, 11, 1, tzinfo=UTC)
    row = mb.build_row(bar, "5m", created)
    assert row[0] == "000698.SZ" and row[1].tzinfo is not None
    assert row[1].astimezone(CN).hour == 9
    assert row[6] == 300 and row[7] == 1000.0 and row[8] is created
    assert isinstance(row[9], int) and 0 <= row[9] < 2 ** 64


def test_envelope_gate_pass_exact():
    bars = [lc5.LC5Record("X", datetime(2026, 10, 21, 9, 35), 10.0, 10.5, 9.8, 10.2, 4800, 4_800_000.0)]
    assert mb.envelope_gate(bars, (10.0, 10.5, 9.8, 10.2, 4800.0, 4_800_000.0)) is None


def test_envelope_gate_price_break():
    bars = [lc5.LC5Record("X", datetime(2026, 10, 21, 9, 35), 10.0, 10.5, 9.8, 10.2, 4800, 4_800_000.0)]
    assert mb.envelope_gate(bars, (9.0, 10.5, 9.8, 10.2, 4800.0, 4_800_000.0)) is not None


def test_envelope_gate_volume_break():
    bars = [lc5.LC5Record("X", datetime(2026, 10, 21, 9, 35), 10.0, 10.5, 9.8, 10.2, 4800, 4_800_000.0)]
    assert mb.envelope_gate(bars, (10.0, 10.5, 9.8, 10.2, 4000.0, 4_800_000.0)) is not None  # 20% 偏差


def test_envelope_gate_zero_daily_volume_strict():
    bars = [lc5.LC5Record("X", datetime(2026, 10, 21, 9, 35), 10.0, 10.5, 9.8, 10.2, 100, 1000.0)]
    assert mb.envelope_gate(bars, (10.0, 10.5, 9.8, 10.2, 0.0, 1000.0)) is not None


# ── parse_and_gate ────────────────────────────────────────────

def test_parse_and_gate_full_day_four_periods(tmp_path):
    candidates, rejects = _make_candidates(tmp_path)
    assert rejects == []
    assert len(candidates["5m"]) == 48
    assert len(candidates["15m"]) == 16
    assert len(candidates["30m"]) == 8
    assert len(candidates["60m"]) == 4
    ids = [r[9] for r in candidates["5m"]]
    assert len(set(ids)) == 48


def test_parse_and_gate_missing_file_rejects_all_days(tmp_path):
    daily_ref = {("2026-10-21", "000698.SZ"): (10.0, 10.5, 9.8, 10.2, 4800.0, 4_800_000.0)}
    candidates, rejects = mb.parse_and_gate(
        [("2026-10-21", "000698.SZ")], daily_ref, tmp_path, progress_every=0)
    assert candidates["5m"] == []
    assert len(rejects) == 1 and "不存在" in rejects[0]["reason"]


def test_parse_and_gate_envelope_reject(tmp_path):
    from tests.test_tdx_lc5 import _full_day_bars, pack_record  # noqa: PLC0415
    p = tmp_path / "vipdoc" / "sz" / "fzline"
    p.mkdir(parents=True)
    bars = _full_day_bars("000698.SZ", datetime(2026, 10, 21))
    (p / "sz000698.lc5").write_bytes(b"".join(
        pack_record(b.datetime, 20.0, 21.0, 19.5, 20.2, 100_000.0, 100_000) for b in bars))
    daily_ref = {("2026-10-21", "000698.SZ"): (10.0, 10.5, 9.8, 10.2, 4800.0, 4_800_000.0)}
    candidates, rejects = mb.parse_and_gate(
        [("2026-10-21", "000698.SZ")], daily_ref, tmp_path / "vipdoc", progress_every=0)
    assert candidates["5m"] == []
    assert "包络门禁" in rejects[0]["reason"]


def test_parse_and_gate_neeq_code_rejected(tmp_path):
    daily_ref = {("2026-10-21", "430047.BJ"): (10.0, 10.5, 9.8, 10.2, 4800.0, 4_800_000.0)}
    candidates, rejects = mb.parse_and_gate(
        [("2026-10-21", "430047.BJ")], daily_ref, tmp_path, progress_every=0)
    assert candidates["5m"] == [] and "NEEQ" in rejects[0]["reason"]


# ── 腾讯交叉验证 ──────────────────────────────────────────────

def test_crosscheck_sample_sh_per_bar_sz_daily_volume():
    def fetcher(symbol):
        # sh600000: 09:35 完全一致；sz000698: 逐根量故意错但日合计对（深市口径）
        bars_sh = [["202610300935", "10.00", "10.02", "10.03", "9.99", "1000.00"],
                   ["202610300940", "10.02", "10.04", "10.05", "10.01", "1100.00"]]
        bars_sz = [["202610300935", "3.42", "3.41", "3.42", "3.41", "200.00"],
                   ["202610300940", "3.41", "3.41", "3.42", "3.41", "300.00"]]
        key = "sh600000" if symbol == "sh600000" else "sz000698"
        val = bars_sh if symbol == "sh600000" else bars_sz
        return {"data": {key: {"m5": val}}}

    def make_candidates(code, bars, created):
        rows = []
        for hm, o, c, h, l, v in bars:
            dt = datetime.strptime("20261030" + hm, "%Y%m%d%H%M")
            bar = lc5.LC5Record(code, dt, o, h, l, c, v, v * 1000.0)
            rows.append(mb.build_row(bar, "5m", created))
        return rows

    created = datetime(2026, 11, 1, tzinfo=UTC)
    candidates = {"5m": (make_candidates("600000.SH",
                                         [("0935", 10.00, 10.02, 10.03, 9.99, 1000.0),
                                          ("0940", 10.02, 10.04, 10.05, 10.01, 1100.0)], created)
                         + make_candidates("000698.SZ",
                                           [("0935", 3.42, 3.41, 3.42, 3.41, 100.0),
                                            ("0940", 3.41, 3.41, 3.42, 3.41, 400.0)], created))}
    missing = [("2026-10-30", "600000.SH"), ("2026-10-30", "000698.SZ")]
    result = mb.crosscheck_sample(candidates, missing, ["600000.SH", "000698.SZ"], fetcher=fetcher)
    assert result["pass_days"] == 2 and result["fail_days"] == 0


def test_crosscheck_sample_price_mismatch_flagged():
    def fetcher(symbol):
        return {"data": {symbol: {"m5": [["202610300935", "9.00", "9.01", "9.02", "8.99", "100.00"]]}}}

    created = datetime(2026, 11, 1, tzinfo=UTC)
    bar = lc5.LC5Record("600000.SH", datetime(2026, 10, 30, 9, 35), 10.0, 10.05, 9.99, 10.02, 100, 1000.0)
    candidates = {"5m": [mb.build_row(bar, "5m", created)]}
    result = mb.crosscheck_sample(candidates, [("2026-10-30", "600000.SH")],
                                  ["600000.SH"], fetcher=fetcher)
    assert result["fail_days"] == 1
    assert result["detail"][0]["status"] == "mismatch"


def test_crosscheck_tencent_kcb_volume_converted():
    bars = mb.tencent_m5_fetch("688119.SH", fetcher=lambda s: {"data": {s: {"m5": [
        ["202610300935", "3.35", "3.34", "3.36", "3.34", "165686.00"]]}}})
    assert bars[0]["volume"] == pytest.approx(1656.86)


def test_pick_sample_excludes_1021_and_bj():
    keys = [("2026-10-21", "600000.SH"), ("2026-10-30", "600000.SH"),
            ("2026-10-22", "000001.SZ"), ("2026-10-30", "920002.BJ")]
    sample = mb.pick_sample(keys, n_sh=1, n_sz=1)
    assert "600000.SH" in sample and "000001.SZ" in sample
    assert "920002.BJ" not in sample


# ── formal_write（fake client）─────────────────────────────────

def test_formal_write_success_flow(tmp_path):
    candidates, rejects = _make_candidates(tmp_path)
    assert rejects == []
    client = FakeClient()
    events = []

    def journal(evt):
        events.append(evt)

    report = mb.formal_write(lambda: client, candidates, journal)
    assert report["status"] == "ok"
    tables = [t for t, _ in client.inserts]
    assert sorted(set(tables)) == sorted(["kline_minute_5", "kline_minute_15", "kline_minute_30", "kline_minute_60"])
    ev_types = [e.get("event") for e in events]
    assert ev_types[0] == "lock_acquired" and ev_types[-1] == "lock_released"
    assert "anti_join_clean" in ev_types and "rollback_start" not in ev_types


def test_formal_write_stale_existing_keys_aborts_without_insert(tmp_path):
    candidates, _ = _make_candidates(tmp_path)
    client = FakeClient()

    def query(sql, settings=None):
        if "DISTINCT code" in sql:
            return _Result(["code", "d"], [("000698.SZ", "2026-10-21")])  # 存量键！
        return FakeClient.query(client, sql, settings)

    client.query = query
    events = []

    with pytest.raises(RuntimeError, match="反连接发现存量键"):
        mb.formal_write(lambda: client, candidates, lambda e: events.append(e))
    assert client.inserts == []  # fail-closed：未插入任何行
    assert events[-1]["event"] == "lock_released"


def test_formal_write_readback_failure_triggers_rollback(tmp_path):
    candidates, _ = _make_candidates(tmp_path)
    client = FakeClient()
    call = {"n": 0}

    def query(sql, settings=None):
        if "count() AS n" in sql and "GROUP BY d, code" in sql:
            call["n"] += 1
            if call["n"] == 1:  # raw 读回返回空 → 缺失
                return _Result(["d", "code", "n", "v", "a"], [])
        return FakeClient.query(client, sql, settings)

    client.query = query
    events = []
    with pytest.raises(RuntimeError, match="读回验证失败"):
        mb.formal_write(lambda: client, candidates, lambda e: events.append(e))
    ev_types = [e.get("event") for e in events]
    assert "rollback_start" in ev_types and "rollback_delete" in ev_types
    delete_cmds = [c for c in client.commands if "DELETE WHERE id IN" in c]
    assert delete_cmds, "回滚必须按精确 id 列表删除"
