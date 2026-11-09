# -*- coding: utf-8 -*-
"""scripts/minute_agg_backfill_window.py 的管道测试（R19 15/30/60 从 5m 聚合；fake client，零真实 DB）。

id 黄金向量来自 2026-11-09 只读探针（存量 10-21~10-30 段、created_at=2026-11-01
13:16:30 规范终批逐 id 实测复现值），见 runtime 报告 r19_probe。
"""
from __future__ import annotations

import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import minute_backfill_1021_1030 as mb  # noqa: E402
import minute_agg_backfill_window as ma   # noqa: E402
from utils import tdx_lc5 as lc5          # noqa: E402

UTC = timezone.utc
CN = mb.CN_TZ
CH_1102_1109 = ("2026-11-02 00:00:00", "2026-11-11 00:00:00")  # R27b 北京钟字面量（与旧 UTC 形式同瞬间）
DAY = "2026-11-02"

# ── 存量反推黄金向量（真实 DB 实测；blake2b(batch|period|code|ts) big-endian）──
GOLDEN = [
    ("15m", "000698.SZ", datetime(2026, 10, 21, 9, 45), 4959001540538323151),
    ("30m", "000698.SZ", datetime(2026, 10, 21, 10, 0), 1187079812765804065),
    ("60m", "000698.SZ", datetime(2026, 10, 21, 10, 30), 10435530633528613241),
]


# ── 合成 5m 源行 ───────────────────────────────────────────────

def _synth_src_rows(code="000698.SZ", day=datetime(2026, 11, 2)):
    """合成一个股票日 48 根 5m 源行：(code, ts, o,h,l,c,v,a)，逐根值不同。"""
    from tests.test_tdx_lc5 import _full_day_bars
    bars = _full_day_bars(code, day)
    rows = []
    for i, b in enumerate(bars):
        rows.append((b.code, int(b.datetime.replace(tzinfo=CN).timestamp()),
                     float(i + 1), float(i + 2), float(i + 0.5), float(i + 1.5),
                     100 * (i + 1), 10.0 * (i + 1)))
    return rows


def _src_to_bars(rows):
    """源行 → LC5Record 分组（绕过 DB，供纯函数测试直接调用）。"""
    from collections import defaultdict
    by = defaultdict(list)
    for code, ts, o, h, l, c, v, a in rows:
        dt = datetime.fromtimestamp(ts, tz=UTC).astimezone(CN).replace(tzinfo=None)
        by[(code, dt.strftime("%Y-%m-%d"))].append(lc5.LC5Record(code, dt, o, h, l, c, int(v), float(a)))
    return {k: sorted(v, key=lambda b: b.datetime) for k, v in by.items()}


# ── FakeClient ─────────────────────────────────────────────────

class _Result:
    def __init__(self, names, rows):
        self.column_names = names
        self.result_rows = rows


class FakeClient:
    """镜像 R16 FakeClient，扩展：5m 源行按窗口过滤、id 探针、栅格校准。"""

    def __init__(self, missing=None, src_5m=None, existing_keys=(),
                 trading_days=None, calibration_day_rows=None):
        self.queries = []
        self.commands = []
        self.inserts = []
        self.stored = {}
        self.lock_tables = set()
        self.lock_markers = {}
        self.missing = dict(missing or {})          # {table: [(day, code), ...]}
        self.src_5m = list(src_5m or [])
        self.existing_keys = list(existing_keys)    # [(day, code), ...]（三表通用）
        self.trading_days = list(trading_days or [DAY])
        self.calibration_day_rows = calibration_day_rows or {}

    # -- 查询窗口里的两个 toDateTime 字面量（R27b 起带 'Asia/Shanghai' 参数）--
    @staticmethod
    def _win(sql):
        return re.findall(r"toDateTime\('(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})'(?:, '[^']+')?\)", sql)[:2]

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
            table = re.search(r"FROM (stock\.kline_minute_\w+)", sql).group(1)
            return _Result(["td", "code"], self.missing.get(table, []))
        if "is_trading" in sql:
            asked = re.findall(r"'(\d{4}-\d{2}-\d{2})'", sql)
            return _Result(["d", "it"], [(d, 1) for d in asked if d in self.trading_days])
        if "created_at >= toDateTime('2026-11-01 13:00:00')" in sql:
            # id_domain_probe：返回黄金向量行（与公式一致）
            table = re.search(r"FROM (stock\.kline_minute_\w+)", sql).group(1)
            period = {"kline_minute_15": "15m", "kline_minute_30": "30m",
                      "kline_minute_60": "60m"}[table.split(".")[-1]]
            rows = []
            for p, code, dt_bj, gid in GOLDEN:
                if p == period:
                    rows.append((code, int(dt_bj.replace(tzinfo=CN).timestamp()), gid))
            return _Result(["code", "ts", "id"], rows)
        if "2026-10-30 00:00:00" in sql or "2026-10-31 00:00:00" in sql:
            # grid_calibration_probe：10-30 存量行（或其 5m 源）
            table = re.search(r"FROM (stock\.kline_minute_\w+)", sql).group(1)
            key = table.split(".")[-1]
            if key in self.calibration_day_rows:
                lo, hi = self._win(sql)
                lo_ts = int(datetime.strptime(lo, "%Y-%m-%d %H:%M:%S")
                            .replace(tzinfo=CN).timestamp())
                hi_ts = int(datetime.strptime(hi, "%Y-%m-%d %H:%M:%S")
                            .replace(tzinfo=CN).timestamp())
                rows = [r for r in self.calibration_day_rows[key] if lo_ts <= r[1] < hi_ts]
                return _Result(["code", "ts", "open", "high", "low", "close",
                                "volume", "amount"], rows)
            return _Result(["code", "ts", "open", "high", "low", "close",
                            "volume", "amount"], [])
        if "FROM stock.kline_minute_5" in sql and "toUnixTimestamp" in sql:
            lo, hi = self._win(sql)
            lo_ts = int(datetime.strptime(lo, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN).timestamp())
            hi_ts = int(datetime.strptime(hi, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN).timestamp())
            rows = [r for r in self.src_5m if lo_ts <= r[1] < hi_ts]
            return _Result(["code", "ts", "open", "high", "low", "close",
                            "volume", "amount"], rows)
        if "count() AS n" in sql and "id IN" in sql:
            return _Result(["n"], [0])  # 回滚核验：按 id 已删净
        if "count() AS n" in sql and "GROUP BY d, code" in sql:
            table = re.search(r"FROM (stock\.kline_minute_\w+)", sql).group(1).split(" FINAL")[0]
            agg = {}
            for r in self.stored.get(table.split(".")[-1], []):
                day = r[1].astimezone(CN).strftime("%Y-%m-%d")
                n, uid, v, a, ids = agg.get((day, r[0]), (0, 0, 0.0, 0.0, set()))
                amount = r[7]
                # R22b：镜像真实表 Nullable 语义——任一行 NULL → 组聚合额 NULL（绝不部分求和）
                new_a = None if (a is None or amount is None) else a + amount
                agg[(day, r[0])] = (n + 1, uid + 1, v + r[6], new_a, ids | {r[9]})
            return _Result(["d", "code", "n", "uid", "v", "a"],
                           [(d, c, n, uid, v, a) for (d, c), (n, uid, v, a, _)
                            in sorted(agg.items())])
        if "toUnixTimestamp(datetime) AS ts, id" in sql:
            table = re.search(r"FROM (stock\.kline_minute_\w+)", sql).group(1).split(".")[-1]
            lo, hi = self._win(sql)
            lo_ts = int(datetime.strptime(lo, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN).timestamp())
            hi_ts = int(datetime.strptime(hi, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN).timestamp())
            rows = [(r[0], int(r[1].timestamp()), r[9])
                    for r in self.stored.get(table, []) if lo_ts <= r[1].timestamp() < hi_ts]
            return _Result(["code", "ts", "id"], rows)
        if "toString(toDateTime(datetime, 'Asia/Shanghai'))" in sql:
            table = re.search(r"FROM (stock\.kline_minute_\w+)", sql).group(1).split(".")[-1]
            code = sql.split("code = '")[1].split("'")[0]
            rows = [(r[1].astimezone(CN).strftime("%Y-%m-%d %H:%M"),
                     r[2], r[3], r[4], r[5], r[6], r[7])
                    for r in self.stored.get(table, []) if r[0] == code]
            return _Result(["dt", "open", "high", "low", "close", "volume", "amount"], rows)
        if "DISTINCT code" in sql:
            return _Result(["code", "d"], [(c, d) for d, c in self.existing_keys])
        raise AssertionError(f"unexpected query: {sql}")

    def command(self, sql):
        self.commands.append(sql)
        if sql.startswith("CREATE TABLE stock.write_lock_"):
            name = sql.split()[2].split(".")[-1]
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


def _make_calibration_rows():
    """10-30 栅格校准数据：合成 5m + 用参考实现 lc5.aggregate_5m 生成的存量 15/30/60 行。"""
    src = _synth_src_rows(day=datetime(2026, 10, 30))
    bars = _src_to_bars(src)[("000698.SZ", "2026-10-30")]
    out = {"kline_minute_5": src}
    for period, minutes in (("15m", 15), ("30m", 30), ("60m", 60)):
        rows = []
        for b in lc5.aggregate_5m(bars, minutes):
            ts = int(b.datetime.replace(tzinfo=CN).timestamp())
            rows.append(("000698.SZ", ts, b.open, b.high, b.low, b.close,
                         int(b.volume), float(b.amount)))
        out[f"kline_minute_{minutes}"] = rows
    return out


# ── id 反推（黄金向量逐 id 复现）───────────────────────────────

@pytest.mark.parametrize("period,code,dt_bj,gid", GOLDEN)
def test_blake2b_row_id_golden_vectors(period, code, dt_bj, gid):
    ts = int(dt_bj.replace(tzinfo=CN).timestamp())
    assert ma.blake2b_row_id(ma.ID_BATCH, period, code, ts) == gid


def test_blake2b_row_id_period_tag_changes_id():
    ts = int(datetime(2026, 11, 2, 9, 45).replace(tzinfo=CN).timestamp())
    ids = {ma.blake2b_row_id(ma.ID_BATCH, p, "000698.SZ", ts) for p in ma.TARGET_PERIODS}
    assert len(ids) == 3  # 周期标签参与哈希，跨周期不撞 id


def test_blake2b_row_id_unsigned_64():
    ts = int(datetime(2026, 11, 9, 15, 0).replace(tzinfo=CN).timestamp())
    for p in ma.TARGET_PERIODS:
        assert 0 <= ma.blake2b_row_id(ma.ID_BATCH, p, "920002.BJ", ts) < 2 ** 64


# ── 时区（UTC 存储 + epoch 换算）───────────────────────────────

def test_epoch_roundtrip_beijing_utc():
    bj = datetime(2026, 11, 2, 15, 0)
    ts = ma.naive_bj_to_epoch(bj)
    assert ma.epoch_to_naive_bj(ts) == bj
    assert ma.epoch_to_utc(ts) == datetime(2026, 11, 2, 7, 0, tzinfo=UTC)  # 北京 15:00 = UTC 07:00


def test_ch_window_reused_from_r16():
    import minute_backfill_window as mw
    assert ma.ch_window(datetime(2026, 11, 2).date(), datetime(2026, 11, 9).date()) \
        == tuple(CH_1102_1109) == tuple(mw.ch_window(datetime(2026, 11, 2).date(),
                                                     datetime(2026, 11, 9).date()))


def test_day_window_is_exactly_one_beijing_day():
    # 精确单北京日：[d 00:00, d+1 00:00) 北京钟（R27b 与旧 UTC 形式同瞬间）。
    # 回归锚：R16 ch_window(d,d) 上界是 end+2 日 00:00 北京（多覆盖一整个
    # 北京日）——单日取数误用会拿到 96/48 根（2026-11-09 formal-write 实测
    # 回滚后修正）。
    assert ma.day_window("2026-11-03") == ("2026-11-03 00:00:00", "2026-11-04 00:00:00")
    lo, hi = ma.day_window("2026-11-03")
    lo_ts = int(datetime.strptime(lo, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN).timestamp())
    hi_ts = int(datetime.strptime(hi, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN).timestamp())
    assert hi_ts - lo_ts == 86400  # 恰 24h
    assert ma.ch_window(datetime(2026, 11, 3).date(), datetime(2026, 11, 3).date())[1] \
        == "2026-11-05 00:00:00"  # ch_window 单日上界确实多一日（防再误用的显式锚）


# ── 合成行聚合 + 栅格对齐 ─────────────────────────────────────

GRIDS = {
    "15m": ["09:45", "10:00", "10:15", "10:30", "10:45", "11:00", "11:15", "11:30",
            "13:15", "13:30", "13:45", "14:00", "14:15", "14:30", "14:45", "15:00"],
    "30m": ["10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00"],
    "60m": ["10:30", "11:30", "14:00", "15:00"],
}


@pytest.mark.parametrize("period", ma.TARGET_PERIODS)
def test_build_agg_rows_grid_alignment(period):
    rows = _synth_src_rows()
    bars = _src_to_bars(rows)
    created = datetime(2026, 11, 9, tzinfo=UTC)
    out = ma.build_agg_rows(bars, created)[period]
    assert len(out) == ma.BARS_PER_CD[period]
    got = [r[1].astimezone(CN).strftime("%H:%M") for r in out]
    assert got == GRIDS[period]  # 与存量 10 月栅格逐一相同


@pytest.mark.parametrize("period", ma.TARGET_PERIODS)
def test_agg_values_ohlc_and_sums(period):
    rows = _synth_src_rows()
    bars = _src_to_bars(rows)[("000698.SZ", DAY)]
    minutes = ma.AGG_MINUTES[period]
    ref = lc5.aggregate_5m(bars, minutes)   # 参考实现（1021_1030 已验证口径）
    created = datetime(2026, 11, 9, tzinfo=UTC)
    out = ma.build_agg_rows(_src_to_bars(rows), created)[period]
    assert len(out) == len(ref)
    for got, want in zip(out, ref):
        code, dt_utc, o, h, l, c, v, a, ca, rid = got
        assert code == want.code
        assert dt_utc == ma.epoch_to_utc(int(want.datetime.replace(tzinfo=CN).timestamp()))
        assert o == want.open and h == want.high and l == want.low and c == want.close
        assert v == want.volume and a == pytest.approx(want.amount, abs=1e-4)
        assert ca == created
        assert rid == ma.blake2b_row_id(ma.ID_BATCH, period, code,
                                        int(want.datetime.replace(tzinfo=CN).timestamp()))


def test_build_agg_rows_respects_allowed_keys():
    bars = _src_to_bars(_synth_src_rows())
    created = datetime(2026, 11, 9, tzinfo=UTC)
    out = ma.build_agg_rows(bars, created, allowed_keys=set())  # 只写缺失键：空集 → 零行
    assert all(v == [] for v in out.values())


# ── 源门禁（48 槽）────────────────────────────────────────────

def test_fetch_5m_bars_groups_and_validates():
    client = FakeClient(src_5m=_synth_src_rows())
    bars_by_key, rejects = ma.fetch_5m_bars(client, ["000698.SZ"], CH_1102_1109)
    assert rejects == []
    assert set(bars_by_key) == {("000698.SZ", DAY)}
    assert len(bars_by_key[("000698.SZ", DAY)]) == 48


def test_fetch_5m_bars_rejects_incomplete_day():
    src = _synth_src_rows()[:-3]  # 45 根：不足 48 → fail-closed 拒绝
    client = FakeClient(src_5m=src)
    bars_by_key, rejects = ma.fetch_5m_bars(client, ["000698.SZ"], CH_1102_1109)
    assert bars_by_key == {} and len(rejects) == 1
    assert rejects[0]["code"] == "000698.SZ" and rejects[0]["day"] == DAY


# ── 计划精确性（码数×16/8/4）──────────────────────────────────

def test_plan_report_exact_bars_per_cd():
    bars = _src_to_bars(_synth_src_rows())
    created = datetime(2026, 11, 9, tzinfo=UTC)
    candidates = ma.build_agg_rows(bars, created)
    plan = ma.plan_report(candidates)
    for period in ma.TARGET_PERIODS:
        assert plan[period]["rows"] == ma.BARS_PER_CD[period]
        assert plan[period]["code_days"] == 1
        assert plan[period]["exact_bars_per_cd"] is True


def test_plan_report_flags_inexact():
    created = datetime(2026, 11, 9, tzinfo=UTC)
    ts = ma.naive_bj_to_epoch(datetime(2026, 11, 2, 9, 45))
    row = ("000698.SZ", ma.epoch_to_utc(ts), 1.0, 1.0, 1.0, 1.0, 1, 1.0, created, 1)
    plan = ma.plan_report({"15m": [row] * 17, "30m": [], "60m": []})  # 17 根 ≠ 1 CD × 16
    assert plan["15m"]["exact_bars_per_cd"] is False


# ── id 存量反推探针 ────────────────────────────────────────────

def test_id_domain_probe_matches_canonical_batch():
    client = FakeClient()
    probe = ma.id_domain_probe(client, per_table=3)
    for period in ma.TARGET_PERIODS:
        assert probe[period]["sampled"] == 1 and probe[period]["match"] == 1


def test_id_domain_probe_fail_closed_on_mismatch():
    client = FakeClient()
    # 篡改：让 15m 探针返回与公式不符的 id
    orig = client.query

    def query(sql, settings=None):
        res = orig(sql, settings)
        if "created_at >= toDateTime('2026-11-01 13:00:00')" in sql and "kline_minute_15" in sql:
            rows = [(c, ts, 1234567890123456789) for c, ts, _id in res.result_rows]
            return _Result(res.column_names, rows)
        return res

    client.query = query
    with pytest.raises(SystemExit):
        ma.id_domain_probe(client, per_table=3)


# ── 栅格/口径校准探针（对存量 10-30 逐字段复算）──────────────

def test_grid_calibration_probe_against_reference_rows():
    client = FakeClient(calibration_day_rows=_make_calibration_rows())
    out = ma.grid_calibration_probe(client, codes=["000698.SZ"], day="2026-10-30")
    for period in ma.TARGET_PERIODS:
        assert out[period]["rows"] == ma.BARS_PER_CD[period]
        assert out[period]["problems"] == []


def test_grid_calibration_probe_fail_closed_on_value_mismatch():
    cal = _make_calibration_rows()
    bad = list(cal["kline_minute_15"])
    code, ts, o, h, l, c, v, a = bad[0]
    cal["kline_minute_15"] = [(code, ts, o + 5.0, h, l, c, v, a)] + bad[1:]  # 篡改 open
    client = FakeClient(calibration_day_rows=cal)
    with pytest.raises(RuntimeError):
        ma.grid_calibration_probe(client, codes=["000698.SZ"], day="2026-10-30")


# ── formal_write（fake client，三表）──────────────────────────

def _formal_setup():
    src = _synth_src_rows()
    bars, rejects = ma.fetch_5m_bars(FakeClient(src_5m=src), ["000698.SZ"], CH_1102_1109)
    assert rejects == []
    created = datetime(2026, 11, 9, tzinfo=UTC)
    candidates = ma.build_agg_rows(bars, created)
    return candidates


def test_formal_write_success_three_tables_in_order():
    candidates = _formal_setup()
    client = FakeClient(src_5m=_synth_src_rows())
    events = []
    report = ma.formal_write(lambda: client, candidates, CH_1102_1109,
                             lambda e: events.append(e))
    assert report["status"] == "ok"
    assert [t for t, _ in client.inserts] == ["kline_minute_15", "kline_minute_30", "kline_minute_60"]
    for (table, rows), period in zip(client.inserts, ma.TARGET_PERIODS):
        assert len(rows) == ma.BARS_PER_CD[period]
    ev = [e.get("event") for e in events]
    assert ev[0] == "lock_acquired" and ev[-1] == "lock_released"
    assert "anti_join_clean" in ev and "rollback_start" not in ev
    assert client.lock_tables == set()  # 锁干净释放


def test_formal_write_stale_existing_keys_aborts_without_insert():
    candidates = _formal_setup()
    client = FakeClient(existing_keys=[(DAY, "000698.SZ")])
    events = []
    with pytest.raises(RuntimeError, match="反连接发现存量键"):
        ma.formal_write(lambda: client, candidates, CH_1102_1109,
                        lambda e: events.append(e))
    assert client.inserts == []  # fail-closed：未插入任何行
    assert events[-1]["event"] == "lock_released"


def test_formal_write_readback_failure_triggers_rollback_per_table():
    candidates = _formal_setup()
    client = FakeClient(src_5m=_synth_src_rows())
    calls = {"n": 0}
    orig = client.query

    def query(sql, settings=None):
        if "count() AS n" in sql and "GROUP BY d, code" in sql and "FINAL" not in sql:
            calls["n"] += 1
            if calls["n"] == 1:  # 15m raw 读回空 → 读回缺失
                return _Result(["d", "code", "n", "uid", "v", "a"], [])
        return orig(sql, settings)

    client.query = query
    events = []
    with pytest.raises(RuntimeError, match="读回验证失败"):
        ma.formal_write(lambda: client, candidates, CH_1102_1109,
                        lambda e: events.append(e))
    ev = [e.get("event") for e in events]
    assert "rollback_start" in ev and "rollback_delete" in ev
    # 实现镜像 1021_1030：三表全部插入后才读回 → 失败回滚必须覆盖三表已插 id
    del_tables = {c.split("stock.")[1].split()[0] for c in client.commands
                  if "DELETE WHERE id IN" in c}
    assert del_tables == {"kline_minute_15", "kline_minute_30", "kline_minute_60"}


def test_formal_write_never_touches_5m_table():
    candidates = _formal_setup()
    client = FakeClient(src_5m=_synth_src_rows())
    ma.formal_write(lambda: client, candidates, CH_1102_1109, lambda e: None)
    assert all(t != "kline_minute_5" for t, _ in client.inserts)


def test_formal_write_sample_check_excludes_next_day_source():
    """回归（2026-11-09 实测）：5m 源含 11-02 与 11-03 两日时，单日复算必须
    只取当日 48 根——错用 ch_window(d,d) 会取到 96 根触发误回滚。"""
    two_days = (_synth_src_rows(day=datetime(2026, 11, 2))
                + _synth_src_rows(day=datetime(2026, 11, 3)))
    src_client = FakeClient(src_5m=two_days)
    bars_by_key, rejects = ma.fetch_5m_bars(src_client, ["000698.SZ"], CH_1102_1109)
    assert rejects == [] and len(bars_by_key) == 2
    created = datetime(2026, 11, 9, tzinfo=UTC)
    candidates = ma.build_agg_rows(bars_by_key, created,
                                   allowed_keys={("2026-11-02", "000698.SZ")})
    client = FakeClient(src_5m=two_days)
    report = ma.formal_write(lambda: client, candidates, CH_1102_1109, lambda e: None)
    assert report["status"] == "ok"  # 次日源在场不再触发 96/48 误判
    for table, rows in client.inserts:
        assert len(rows) == 16 or len(rows) == 8 or len(rows) == 4  # 仅 11-02 单日


# ── CLI（默认零库访问 / dry-run e2e）──────────────────────────

def test_main_invalid_window_never_touches_db(monkeypatch):
    def boom():
        raise AssertionError("窗口校验失败前发生了库访问")

    monkeypatch.setattr(ma, "_default_client_factory", boom)
    with pytest.raises(SystemExit):
        ma.main(["--mode", "audit", "--start-date", "2026-11-02", "--end-date", "2026-11-20"])
    with pytest.raises(SystemExit):
        ma.main(["--mode", "audit", "--start-date", "not-a-date", "--end-date", "2026-11-09"])


def test_main_dry_run_end_to_end_with_fake_client(tmp_path, monkeypatch):
    client = FakeClient(
        missing={f"stock.kline_minute_{m}": [(DAY, "000698.SZ")] for m in (15, 30, 60)},
        src_5m=_synth_src_rows(),
        calibration_day_rows=_make_calibration_rows())
    monkeypatch.setattr(ma, "_default_client_factory", lambda: client)
    out = tmp_path / "out"
    rc = ma.main(["--mode", "dry-run", "--start-date", DAY, "--end-date", DAY,
                  "--out-dir", str(out)])
    assert rc == 0
    report = json.loads((out / "dry_run_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "dry_run_ok"
    for period in ma.TARGET_PERIODS:
        assert report["plan"][period]["rows"] == ma.BARS_PER_CD[period]
        assert report["plan"][period]["exact_bars_per_cd"] is True
    assert client.inserts == []  # dry-run 零写入
    assert report["id_probe"]["15m"]["match"] == 1
    assert report["skipped_no_5m"] == 0  # 5m 有源的键不得误报跳过（键序回归）


def test_main_audit_writes_missing_tsv(tmp_path, monkeypatch):
    client = FakeClient(
        missing={f"stock.kline_minute_{m}": [(DAY, "000698.SZ")] for m in (15, 30, 60)})
    monkeypatch.setattr(ma, "_default_client_factory", lambda: client)
    out = tmp_path / "out"
    rc = ma.main(["--mode", "audit", "--start-date", DAY, "--end-date", DAY,
                  "--out-dir", str(out)])
    assert rc == 0
    for period in ma.TARGET_PERIODS:
        tsv = out / f"missing_keys_{period}.tsv"
        assert tsv.exists() and tsv.read_text(encoding="utf-8").strip() == f"{DAY}\t000698.SZ"
    audit = json.loads((out / "audit.json").read_text(encoding="utf-8"))
    assert audit["15m"]["missing_code_days"] == 1


def test_main_dry_run_fails_closed_when_5m_source_missing_for_key(tmp_path, monkeypatch):
    # 缺失键在 5m 无数据（4 个 fail-closed code-day 场景）：跳过并报告，不造 bar
    client = FakeClient(
        missing={f"stock.kline_minute_{m}": [(DAY, "000698.SZ")] for m in (15, 30, 60)},
        src_5m=[], calibration_day_rows=_make_calibration_rows())
    monkeypatch.setattr(ma, "_default_client_factory", lambda: client)
    out = tmp_path / "out"
    rc = ma.main(["--mode", "dry-run", "--start-date", DAY, "--end-date", DAY,
                  "--out-dir", str(out)])
    assert rc == 0
    report = json.loads((out / "dry_run_report.json").read_text(encoding="utf-8"))
    assert report["plan"]["15m"]["rows"] == 0
    assert report["skipped_no_5m"] == 1
    assert client.inserts == []


# ── R22b：amount NULL 口径（成分任一 NULL → 该聚合行 NULL，绝不部分求和）──

def _null_amount_rows(rows, null_indices):
    """把合成源行指定序号的 amount 置 NULL（模拟 R21/R22 B 源 amount=NULL 行）。"""
    nulls = set(null_indices)
    return [r[:-1] + (None,) if i in nulls else r for i, r in enumerate(rows)]


def _ref_agg(period, minutes):
    """参考实现：对不带 NULL 的合成源聚合（OHLC/量/非 NULL 额的基准）。"""
    return lc5.aggregate_5m(_src_to_bars(_synth_src_rows())[("000698.SZ", DAY)],
                            ma.AGG_MINUTES[period])


def test_amount_matches_null_aware_unit():
    f = ma._amount_matches
    assert f(None, None, 1e-6) is True          # 双 NULL 一致
    assert f(math.nan, None, 1e-6) is True      # NaN 哨兵视同 NULL
    assert f(None, 1.0, 1e-6) is False          # 单边 NULL 不一致
    assert f(1.0, None, 1e-6) is False
    assert f(100.0, 100.0, 1e-6) is True
    assert f(100.0, 150.0, 1e-6) is False


def test_fetch_5m_bars_tolerates_null_amount():
    # 崩点回归：R22 实测 L159 float(None) TypeError；NULL 行必须照常过 48 槽门禁
    src = _null_amount_rows(_synth_src_rows(), range(48))
    client = FakeClient(src_5m=src)
    bars_by_key, rejects = ma.fetch_5m_bars(client, ["000698.SZ"], CH_1102_1109)
    assert rejects == []
    assert set(bars_by_key) == {("000698.SZ", DAY)}
    assert len(bars_by_key[("000698.SZ", DAY)]) == 48


@pytest.mark.parametrize("period", ma.TARGET_PERIODS)
def test_build_agg_rows_all_null_amount_rows_are_null(period):
    # 全 NULL：16/8/4 根全部 amount=None；OHLC/量与参考完全一致（不受额影响）
    src = _null_amount_rows(_synth_src_rows(), range(48))
    bars_by_key, _ = ma.fetch_5m_bars(FakeClient(src_5m=src), ["000698.SZ"], CH_1102_1109)
    created = datetime(2026, 11, 9, tzinfo=UTC)
    out = ma.build_agg_rows(bars_by_key, created)[period]
    ref = _ref_agg(period, ma.AGG_MINUTES[period])
    assert len(out) == len(ref) == ma.BARS_PER_CD[period]
    for got, want in zip(out, ref):
        assert got[7] is None
        assert got[2:7] == (want.open, want.high, want.low, want.close, want.volume)


def test_build_agg_rows_mixed_null_propagates_per_bucket():
    # 混合：仅 09:35/09:40 两根 NULL → 仅含它们的桶额=None，其余桶照常求和；
    # 绝不出现部分求和（09:45 桶若只加 09:45 一根会得 30.0）
    src = _null_amount_rows(_synth_src_rows(), {0, 1})
    bars_by_key, _ = ma.fetch_5m_bars(FakeClient(src_5m=src), ["000698.SZ"], CH_1102_1109)
    created = datetime(2026, 11, 9, tzinfo=UTC)
    built = ma.build_agg_rows(bars_by_key, created)

    a15 = {r[1].astimezone(CN).strftime("%H:%M"): r[7] for r in built["15m"]}
    assert a15["09:45"] is None
    assert a15["10:00"] == pytest.approx(150.0)      # 40+50+60
    assert 30.0 not in a15.values()                  # 部分求和禁令
    a30 = {r[1].astimezone(CN).strftime("%H:%M"): r[7] for r in built["30m"]}
    assert a30["10:00"] is None and a30["10:30"] == pytest.approx(570.0)
    a60 = {r[1].astimezone(CN).strftime("%H:%M"): r[7] for r in built["60m"]}
    assert a60["10:30"] is None and a60["11:30"] == pytest.approx(2220.0)

    ref = _ref_agg("15m", 15)
    for got, want in zip(built["15m"], ref):
        assert got[2:7] == (want.open, want.high, want.low, want.close, want.volume)
    assert built["15m"][0][6] == 600                 # 量照算：100+200+300


def test_readback_aggregate_null_aware_sql_and_values():
    created = datetime(2026, 11, 9, tzinfo=UTC)
    rows = [("000698.SZ", ma.epoch_to_utc(ma.naive_bj_to_epoch(datetime(2026, 11, 2, 9, 45))),
             1.0, 2.0, 0.5, 1.5, 100, None, created, 7),
            ("000698.SZ", ma.epoch_to_utc(ma.naive_bj_to_epoch(datetime(2026, 11, 2, 10, 0))),
             1.0, 2.0, 0.5, 1.5, 100, 3.0, created, 8)]
    client = FakeClient()
    client.stored["kline_minute_15"] = rows
    out = ma._readback_aggregate(client, "stock.kline_minute_15",
                                 [(DAY, "000698.SZ")], CH_1102_1109)
    assert out[(DAY, "000698.SZ")] == (2, 2, 200.0, None)   # 任一行 NULL → 组聚合额 None
    sql = client.queries[-1]
    assert "countIf(amount IS NULL)" in sql                  # SQL 必须 NULL 感知
    assert "sum(amount) AS a" not in sql                     # 禁止裸 sum 部分求和


def test_formal_write_all_null_amount_end_to_end():
    # B 源 48 根全 NULL（R22 两键实况）：写入、读回、id 复现、抽样复算全链不崩
    src = _null_amount_rows(_synth_src_rows(), range(48))
    bars_by_key, _ = ma.fetch_5m_bars(FakeClient(src_5m=src), ["000698.SZ"], CH_1102_1109)
    candidates = ma.build_agg_rows(bars_by_key, datetime(2026, 11, 9, tzinfo=UTC))
    client = FakeClient(src_5m=src)
    events = []
    report = ma.formal_write(lambda: client, candidates, CH_1102_1109,
                             lambda e: events.append(e))
    assert report["status"] == "ok"
    for table, rows in client.inserts:
        assert rows and all(r[7] is None for r in rows)
    ev = [e.get("event") for e in events]
    assert "readback_ok" in ev and "rollback_start" not in ev
    assert client.lock_tables == set()


def test_formal_write_mixed_null_amount_no_partial_sum():
    src = _null_amount_rows(_synth_src_rows(), {0, 1})
    bars_by_key, _ = ma.fetch_5m_bars(FakeClient(src_5m=src), ["000698.SZ"], CH_1102_1109)
    candidates = ma.build_agg_rows(bars_by_key, datetime(2026, 11, 9, tzinfo=UTC))
    client = FakeClient(src_5m=src)
    report = ma.formal_write(lambda: client, candidates, CH_1102_1109, lambda e: None)
    assert report["status"] == "ok"
    inserts = dict(client.inserts)
    a15 = {r[1].astimezone(CN).strftime("%H:%M"): r[7]
           for r in inserts["kline_minute_15"]}
    assert a15["09:45"] is None            # 含 NULL 成分的桶 → NULL
    assert a15["10:00"] == pytest.approx(150.0)
    assert 30.0 not in a15.values()        # 部分求和禁令贯穿写-读回全链
