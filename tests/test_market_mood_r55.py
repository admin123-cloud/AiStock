# -*- coding: utf-8 -*-
"""R55（2026-11-11）：涨停生态聚合（情绪温度计）单元测试。

任务书：F:/Aistock/data/data/runtime/task_briefs/20261111_market_breadth_r55.md

零 DB（clickhouse 依赖 monkeypatch）+ 合成数据。契约：

  - 涨停价 = 昨收 × 板块规则（R20b 分类器口径：ST 5% / 北交所 30% /
    创业板·科创板 20% / 其余 10%），四舍五入到分（ROUND_HALF_UP，Decimal）；
  - 曾涨停 = 当日最高价触及过涨停价；现仍封板 = 现价≥涨停价；
    封板率 = 现仍封板 ÷ 曾涨停；炸板数 = 曾涨停 − 现仍封板；
  - 连板计数 = 收盘价封板的连续交易日数（盘中含当日实时封板 +1）；
  - 梯队表 = streak 分桶 + 数量 + 代表股（成交额排序）；
  - 盘中快照采集后追加计算 → runtime/market_mood/YYYYMMDD.json（realtime 键），
    盘后日线终算覆盖（final 键），原子写。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import services.market_mood as mm  # noqa: E402

BJ = ZoneInfo("Asia/Shanghai")


# ── 板块规则分类器（R20b 口径）─────────────────────────────────

class TestLimitRatio:
    def test_main_board_10pct(self):
        assert mm.limit_ratio("600519.SH", "贵州茅台") == 0.10
        assert mm.limit_ratio("002015.SZ", "协鑫能科") == 0.10

    def test_chinext_star_20pct(self):
        assert mm.limit_ratio("300054.SZ", "鼎龙股份") == 0.20
        assert mm.limit_ratio("301123.SZ", "某创业板") == 0.20
        assert mm.limit_ratio("688981.SH", "中芯国际") == 0.20

    def test_bse_30pct(self):
        assert mm.limit_ratio("920002.BJ", "万泰股份") == 0.30
        assert mm.limit_ratio("830799.BJ", "艾融软件") == 0.30

    def test_st_5pct(self):
        assert mm.limit_ratio("600000.SH", "ST浦发") == 0.05
        assert mm.limit_ratio("000005.SZ", "*ST星源") == 0.05

    def test_st_takes_precedence(self):
        # 与 R20b/_limit_up_threshold 一致：ST 判定优先于板块
        assert mm.limit_ratio("300999.SZ", "ST某某") == 0.05


class TestLimitPrice:
    def test_round_half_up(self):
        # 12.55 × 1.10 = 13.805 → 半分位必须进位（banker's rounding 会给 13.80）
        assert mm.limit_up_price(12.55, 0.10) == 13.81
        assert mm.limit_down_price(12.55, 0.10) == 11.30  # 11.295 → 11.30

    def test_plain_cases(self):
        assert mm.limit_up_price(10.00, 0.10) == 11.00
        assert mm.limit_up_price(100.00, 0.20) == 120.00
        assert mm.limit_up_price(13.64, 0.05) == 14.32  # 14.322 → 14.32
        assert mm.limit_down_price(10.00, 0.10) == 9.00

    def test_invalid_prev_close_raises(self):
        with pytest.raises(ValueError):
            mm.limit_up_price(0.0, 0.10)
        with pytest.raises(ValueError):
            mm.limit_up_price(-1.0, 0.10)


# ── 个股分类与封板率口径 ───────────────────────────────────────

def _rec(code="600001.SH", name="测试股", prev=10.0, last=11.0, high=11.0, low=10.2,
         amount=5e8):
    return {"code": code, "name": name, "prev_close": prev, "last_price": last,
            "day_high": high, "day_low": low, "amount": amount}


class TestClassify:
    def test_sealed_at_limit(self):
        # 主板 10 → 涨停价 11.00；现价=最高=11 → 现仍封 & 曾涨停
        c = mm.classify_stock(_rec())
        assert c["touched_limit_up"] and c["at_limit_up"]
        assert not c["at_limit_down"]

    def test_broke_board(self):
        # 触及 11.00 后回落 10.8 → 曾涨停、未封、炸板
        c = mm.classify_stock(_rec(last=10.8, high=11.0))
        assert c["touched_limit_up"] and not c["at_limit_up"]

    def test_never_touched(self):
        c = mm.classify_stock(_rec(last=10.5, high=10.6))
        assert not c["touched_limit_up"]

    def test_limit_down(self):
        c = mm.classify_stock(_rec(last=9.0, high=10.0, low=9.0))
        assert c["at_limit_down"]

    def test_float_tolerance_two_decimals(self):
        # last=10.999999（浮点噪声）也应判封板：按 2 位小数比较
        c = mm.classify_stock(_rec(last=10.999999, high=11.0))
        assert c["at_limit_up"]


class TestSealRate:
    def test_seal_rate_and_broke(self):
        stocks = [
            mm.classify_stock(_rec(code=f"60000{i}.SH", last=11.0, high=11.0))
            for i in range(8)
        ] + [
            mm.classify_stock(_rec(code=f"60001{i}.SH", last=10.8, high=11.0))
            for i in range(2)
        ]
        summary = mm.summarize_limit_stats(stocks)
        assert summary["limit_up_count"] == 8
        assert summary["ever_limit_up"] == 10
        assert summary["broke_count"] == 2
        assert summary["seal_rate"] == pytest.approx(0.8)

    def test_zero_ever_seal_rate_none(self):
        summary = mm.summarize_limit_stats([])
        assert summary["seal_rate"] is None
        assert summary["ever_limit_up"] == 0

    def test_limit_down_counted(self):
        stocks = [mm.classify_stock(_rec(code="600100.SH", last=9.0, high=10.0, low=9.0))]
        summary = mm.summarize_limit_stats(stocks)
        assert summary["limit_down_count"] == 1


# ── 连板计数 ───────────────────────────────────────────────────

class TestStreak:
    def _hist(self, closes, prevs, code="600001.SH", name="测试"):
        """closes/prevs: 由旧到新的日线序列（最后一根=最近交易日）。"""
        rows = []
        for c, p in zip(closes, prevs):
            rows.append({"code": code, "name": name, "trade_date": None,
                         "close": c, "prev_close": p})
        return rows

    def test_three_board_streak(self):
        # 10→11(封)→12.1(封)→13.31(封)
        hist = self._hist([11.0, 12.1, 13.31], [10.0, 11.0, 12.1])
        assert mm.compute_streak(hist) == 3

    def test_streak_broken_middle(self):
        # 10→11(封)→11.5(未封)→12.65(封) → streak=1
        hist = self._hist([11.0, 11.5, 12.65], [10.0, 11.0, 11.5])
        assert mm.compute_streak(hist) == 1

    def test_empty_history_zero(self):
        assert mm.compute_streak([]) == 0

    def test_intraday_streak_plus_one(self):
        hist = self._hist([11.0, 12.1], [10.0, 11.0])
        assert mm.compute_streak(hist) == 2
        assert mm.compute_streak(hist, today_at_limit=True) == 3

    def test_today_not_at_limit_no_plus(self):
        hist = self._hist([11.0, 12.1], [10.0, 11.0])
        assert mm.compute_streak(hist, today_at_limit=False) == 2


# ── 梯队表 ─────────────────────────────────────────────────────

class TestLadder:
    def test_buckets_and_reps(self):
        classified = []
        for i in range(3):
            classified.append((mm.classify_stock(
                _rec(code=f"60010{i}.SH", amount=1e9 - i * 1e8)), 2))
        classified.append((mm.classify_stock(_rec(code="600200.SH", amount=5e8)), 1))
        ladder = mm.build_ladder(classified, max_streak=10, top_n=2)
        rows = {row["streak"]: row for row in ladder}
        assert rows[2]["count"] == 3
        assert rows[1]["count"] == 1
        # 代表股按成交额降序、截断 top_n
        assert rows[2]["reps"][0]["code"] == "600100.SH"
        assert len(rows[2]["reps"]) == 2

    def test_max_streak(self):
        ladder_src = [(mm.classify_stock(_rec(code="600300.SH")), 5)]
        mood = mm.build_ladder(ladder_src, max_streak=10, top_n=5)
        assert mood is not None


# ── runtime/market_mood 落盘 ───────────────────────────────────

class TestMoodFileIo:
    def test_write_and_load_roundtrip(self, tmp_path):
        day = "20261111"
        realtime = {"mode": "intraday", "limit_up_count": 30, "as_of": "2026-11-11 14:35"}
        final = {"mode": "final", "limit_up_count": 32}
        mm.write_day_payload(day, realtime=realtime, out_root=tmp_path)
        loaded = mm.load_day_payload(day, out_root=tmp_path)
        assert loaded["realtime"] == realtime
        assert loaded.get("final") is None

        # 盘后终算覆盖：final 写入且保留 realtime 历史
        mm.write_day_payload(day, final=final, out_root=tmp_path)
        loaded2 = mm.load_day_payload(day, out_root=tmp_path)
        assert loaded2["final"] == final
        assert loaded2["realtime"] == realtime

    def test_load_missing_returns_none(self, tmp_path):
        assert mm.load_day_payload("19990101", out_root=tmp_path) is None

    def test_atomic_write_no_partial(self, tmp_path):
        mm.write_day_payload("20261111", realtime={"a": 1}, out_root=tmp_path)
        # 文件必须是合法 JSON（原子写不留半截）
        path = tmp_path / "market_mood" / "20261111.json"
        json.loads(path.read_text(encoding="utf-8"))


# ── API 侧模式选择 ─────────────────────────────────────────────

class TestLimitUpStatsSelection:
    def test_fresh_realtime_wins(self, tmp_path, monkeypatch):
        now = datetime(2026, 11, 11, 14, 36, tzinfo=BJ)
        realtime = {"mode": "intraday", "date": "2026-11-11",
                    "as_of": "2026-11-11 14:35", "limit_up_count": 30}
        mm.write_day_payload("20261111", realtime=realtime, out_root=tmp_path)
        payload = mm.limit_up_stats(now=now, out_root=tmp_path)
        assert payload["mode"] == "intraday"
        assert payload["limit_up_count"] == 30

    def test_stale_realtime_falls_to_final(self, tmp_path, monkeypatch):
        now = datetime(2026, 11, 11, 20, 0, tzinfo=BJ)
        realtime = {"mode": "intraday", "date": "2026-11-11",
                    "as_of": "2026-11-11 14:35", "limit_up_count": 30}
        final = {"mode": "final", "date": "2026-11-11", "limit_up_count": 32}
        mm.write_day_payload("20261111", realtime=realtime, out_root=tmp_path)
        mm.write_day_payload("20261111", final=final, out_root=tmp_path)
        payload = mm.limit_up_stats(now=now, out_root=tmp_path)
        assert payload["mode"] == "final"

    def test_missing_file_daily_fallback(self, tmp_path, monkeypatch):
        # JSON 缺失 → 按需日线终算（合成 fake）
        now = datetime(2026, 11, 11, 20, 0, tzinfo=BJ)
        monkeypatch.setattr(mm, "compute_final_mood",
                            lambda d: {"mode": "final", "date": d, "limit_up_count": 5})
        payload = mm.limit_up_stats(date="2026-11-11", now=now, out_root=tmp_path)
        assert payload["mode"] == "final"
        assert payload["source"] == "on_demand_daily"

    def test_everything_missing_fail_closed(self, tmp_path, monkeypatch):
        now = datetime(2026, 11, 11, 20, 0, tzinfo=BJ)
        monkeypatch.setattr(mm, "compute_final_mood", lambda d: None)
        payload = mm.limit_up_stats(date="2026-11-11", now=now, out_root=tmp_path)
        assert "error" in payload
