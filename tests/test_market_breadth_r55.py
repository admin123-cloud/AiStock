# -*- coding: utf-8 -*-
"""R55（2026-11-11）：市场宽度 + 涨跌分布直方图 + regime breadth 接入 单元测试。

任务书：F:/Aistock/data/data/runtime/task_briefs/20261111_market_breadth_r55.md

零 DB：services.market_breadth 的 clickhouse 依赖全 monkeypatch（镜像
tests/test_intraday_market_snapshot_read.py 的 R28 模式）。契约：

  - 宽度：kline_daily 一条聚合 SQL（raw+argMax 去重、60 交易日窗、窗口函数
    编号），MA5/20/60 站上占比 + 60日新高/新低 + 高低比；历史任一日可查；
    非交易日/无数据 fail-closed 返回 error payload；
  - 直方图：13 分桶（<-10 / -10~-8 / ... / 0 / ... / 8~10 / >10）边界
    左闭右开、0 单列、外侧桶含∞；平均/中位；盘中（快照新鲜）/盘后（日线）
    双模式 fail-closed；
  - regime 接入：run_regime 增 breadth 键（水位确认因子，观察期），只标注
    divergence 不改判定式；breadth 不可用 → status=unavailable 不抛异常。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import services.market_breadth as mb  # noqa: E402

BJ = ZoneInfo("Asia/Shanghai")


# ── 13 分桶边界 ─────────────────────────────────────────────────

class TestBucketEdges:
    def test_thirteen_buckets_with_labels(self):
        assert len(mb.BUCKET_LABELS) == 13
        assert mb.BUCKET_LABELS[0] == "<-10"
        assert mb.BUCKET_LABELS[6] == "0"
        assert mb.BUCKET_LABELS[12] == ">10"

    def test_partition_covers_all_reals(self):
        # 每个实数恰好落入一个桶：用分界值逐一验证
        cases = {
            -20.0: 0, -10.0001: 0,
            -10.0: 1, -8.0: 2, -6.0: 3, -4.0: 4, -2.0: 5,
            0.0: 6,
            0.5: 7, 2.0: 8, 4.0: 9, 6.0: 10, 8.0: 11, 10.0: 12,
            19.9: 12, 29.9: 12,
        }
        for pct, idx in cases.items():
            assert mb.bucket_index(pct) == idx, f"pct={pct}"

    def test_inside_bucket_samples(self):
        assert mb.bucket_index(-9.99) == 1
        assert mb.bucket_index(-0.01) == 5
        assert mb.bucket_index(0.01) == 7
        assert mb.bucket_index(1.99) == 7
        assert mb.bucket_index(9.99) == 11

    def test_none_and_nan_fail_closed(self):
        with pytest.raises(ValueError):
            mb.bucket_index(None)
        with pytest.raises(ValueError):
            mb.bucket_index(float("nan"))

    def test_bucket_counts_sum(self):
        pcts = [-20, -10, -9.9, -8, -6, -4, -2, -1, 0, 0, 1, 2, 4, 6, 8, 10, 11]
        counts = mb.bucket_counts(pcts)
        assert len(counts) == 13
        assert sum(counts) == len(pcts)
        assert counts[0] == 1 and counts[1] == 2 and counts[12] == 2
        assert counts[5] == 2 and counts[6] == 2  # -2~-0 与 0 单列


# ── 宽度：SQL 形状守卫（R20b token 风格）────────────────────────

class TestBreadthSqlShape:
    def test_sql_uses_raw_argmax_dedup(self):
        sql = mb.build_breadth_sql()
        assert "argMax" in sql, "raw+argMax 去重（R44 惯例）"
        assert "FINAL" not in sql.replace("FINALIZE", ""), "不得走 FINAL 视图"
        for token in ("quality_read", "precise"):
            assert token not in sql, f"禁用 {token} 视图"

    def test_sql_window_is_60_trade_dates_with_rank(self):
        sql = mb.build_breadth_sql()
        assert "LIMIT 60" in sql
        assert "row_number()" in sql, "窗口日期编号（rn=0 为查询日）"
        assert "type = 'stock'" in sql

    def test_sql_ma_and_extremes_columns(self):
        sql = mb.build_breadth_sql()
        for token in ("above_ma5", "above_ma20", "above_ma60",
                      "new_high_60d", "new_low_60d"):
            assert token in sql, f"缺少 {token}"


def _breadth_row(total=5580, b5=5500, a5=3300, b20=5400, a20=2700,
                 b60=5300, a60=2650, hi=120, lo=60):
    return {
        "total": total, "ma5_base": b5, "above_ma5": a5,
        "ma20_base": b20, "above_ma20": a20, "ma60_base": b60,
        "above_ma60": a60, "new_high_60d": hi, "new_low_60d": lo,
    }


class TestParseBreadthRow:
    def test_pct_math(self):
        payload = mb.parse_breadth_row(_breadth_row(), "2026-11-11")
        assert payload["date"] == "2026-11-11"
        assert payload["total"] == 5580
        assert payload["above_ma5_pct"] == pytest.approx(60.0, abs=0.05)
        assert payload["above_ma20_pct"] == pytest.approx(50.0, abs=0.05)
        assert payload["above_ma60_pct"] == pytest.approx(50.0, abs=0.05)
        assert payload["new_high_60d"] == 120
        assert payload["new_low_60d"] == 60
        assert payload["hl_ratio"] == pytest.approx(2.0, abs=0.01)
        assert payload["base_counts"] == {"ma5": 5500, "ma20": 5400, "ma60": 5300}

    def test_zero_new_low_ratio_none(self):
        row = _breadth_row(hi=80, lo=0)
        payload = mb.parse_breadth_row(row, "2026-11-11")
        assert payload["hl_ratio"] is None
        assert payload["new_low_60d"] == 0

    def test_zero_base_pct_none(self):
        row = _breadth_row(b20=0, a20=0)
        payload = mb.parse_breadth_row(row, "2026-11-11")
        assert payload["above_ma20_pct"] is None


class TestMarketBreadthOrchestrator:
    def _patch(self, monkeypatch, pre_row, agg_df, queries):
        pre_df = pd.DataFrame([pre_row])

        def fake_query_df(sql, params=None, *, raw=False):
            queries.append(sql)
            if "cnt" in sql and "latest" in sql:
                return pre_df
            return agg_df

        monkeypatch.setattr(mb, "clickhouse_available", lambda: True)
        monkeypatch.setattr(mb, "clickhouse_query_df", fake_query_df)

    def test_success_and_caching(self, monkeypatch):
        queries = []
        agg = pd.DataFrame([_breadth_row()])
        self._patch(monkeypatch, {"cnt": 5580, "latest": "2026-11-11"}, agg, queries)
        payload = mb.market_breadth("2026-11-11")
        assert payload["above_ma5_pct"] == pytest.approx(60.0, abs=0.05)
        assert payload["source"] == "clickhouse:kline_daily"
        assert queries, "至少发出预检+聚合两查询"

    def test_non_trade_date_fail_closed(self, monkeypatch):
        queries = []
        self._patch(monkeypatch, {"cnt": 0, "latest": "2026-11-10"},
                    pd.DataFrame([_breadth_row()]), queries)
        payload = mb.market_breadth("2026-11-16")
        assert "error" in payload
        assert "2026-11-10" in str(payload), "返回最近可用日期提示"

    def test_default_date_uses_latest(self, monkeypatch):
        queries = []
        self._patch(monkeypatch, {"cnt": 5580, "latest": "2026-11-11"},
                    pd.DataFrame([_breadth_row()]), queries)
        payload = mb.market_breadth()
        assert payload.get("date") == "2026-11-11"

    def test_clickhouse_down_error(self, monkeypatch):
        monkeypatch.setattr(mb, "clickhouse_available", lambda: False)
        payload = mb.market_breadth("2026-11-11")
        assert "error" in payload


# ── 涨跌分布直方图 ─────────────────────────────────────────────

class TestDistributionDualMode:
    def _snapshot_row(self, ts):
        row = {f"bucket_{i}": 0 for i in range(13)}
        for i, n in zip((0, 1, 6, 12), (50, 100, 300, 40)):
            row[f"bucket_{i}"] = n
        row.update({
            "latest_ts": ts, "covered": 5570, "avg_pct": 0.35,
            "median_pct": 0.12, "up_count": 3000, "down_count": 2200,
            "flat_count": 370,
        })
        return pd.DataFrame([row])

    def test_intraday_mode_when_snapshot_fresh(self, monkeypatch):
        ts = datetime.now(BJ).replace(second=0, microsecond=0) - __import__("datetime").timedelta(seconds=60)
        df = self._snapshot_row(ts)
        queries = []

        def fake_query(sql, params=None, *, raw=False):
            queries.append(sql)
            return df if "snapshot_ts" in sql else pd.DataFrame()

        monkeypatch.setattr(mb, "clickhouse_available", lambda: True)
        monkeypatch.setattr(mb, "clickhouse_query_df", fake_query)
        payload = mb.advance_decline_distribution(now=datetime.now(BJ))
        assert payload["mode"] == "intraday"
        assert payload["as_of"].startswith(ts.strftime("%Y-%m-%d"))
        assert len(payload["buckets"]) == 13
        assert payload["buckets"][1]["count"] == 100
        assert payload["avg_change_pct"] == pytest.approx(0.35)
        assert payload["median_change_pct"] == pytest.approx(0.12)
        assert payload["covered"] == 5570

    def test_daily_mode_when_snapshot_stale(self, monkeypatch):
        stale = datetime.now(BJ) - __import__("datetime").timedelta(minutes=30)
        snap = self._snapshot_row(stale)
        daily = pd.DataFrame([{
            **{f"bucket_{i}": 0 for i in range(13)},
            "avg_pct": -0.4, "median_pct": -0.2, "covered": 5584,
        }])
        daily.loc[0, "bucket_5"] = 900

        def fake_query(sql, params=None, *, raw=False):
            return snap if "snapshot_ts" in sql else daily

        monkeypatch.setattr(mb, "clickhouse_available", lambda: True)
        monkeypatch.setattr(mb, "clickhouse_query_df", fake_query)
        payload = mb.advance_decline_distribution(
            trade_date="2026-11-11", now=datetime.now(BJ))
        assert payload["mode"] == "daily"
        assert payload["date"] == "2026-11-11"
        assert payload["buckets"][5]["count"] == 900
        assert payload["avg_change_pct"] == pytest.approx(-0.4)

    def test_distribution_sql_boundaries_match_pure_fn(self):
        sql = mb.build_distribution_sql()
        # 每个分界值出现在 SQL 里（与 bucket_index 同一来源）
        for edge in (-10, -8, -6, -4, -2, 0, 2, 4, 6, 8, 10):
            assert str(edge) in sql, f"SQL 缺少分界 {edge}"
        assert "quantileExact(0.5)" in sql, "中位数用精确分位"


# ── regime 接入（breadth 水位确认因子）────────────────────────

class TestBreadthWaterAnnotation:
    def test_level_thresholds(self):
        strong = mb.breadth_level({"above_ma20_pct": 60.0})
        weak = mb.breadth_level({"above_ma20_pct": 40.0})
        neutral = mb.breadth_level({"above_ma20_pct": 50.0})
        assert (strong, weak, neutral) == ("strong", "weak", "neutral")

    def test_divergence_up_regime_with_weak_breadth(self):
        breadth = {"date": "2026-11-10", "above_ma20_pct": 35.0}
        note = mb.breadth_divergence("up", breadth)
        assert note == "breadth_weak_vs_up"

    def test_divergence_down_regime_with_strong_breadth(self):
        breadth = {"date": "2026-11-10", "above_ma20_pct": 66.0}
        assert mb.breadth_divergence("down", breadth) == "breadth_strong_vs_down"

    def test_no_divergence_cases(self):
        b = {"above_ma20_pct": 65.0}
        assert mb.breadth_divergence("up", b) is None
        assert mb.breadth_divergence("sideways", b) is None
        assert mb.breadth_divergence("sideways", {"above_ma20_pct": 30.0}) is None

    def test_annotation_marks_observation_only(self):
        breadth = {"date": "2026-11-10", "above_ma20_pct": 35.0,
                   "above_ma5_pct": 40.0, "above_ma60_pct": 30.0,
                   "new_high_60d": 20, "new_low_60d": 90, "hl_ratio": 0.22}
        regimes = {"002015": "up", "300054": "down", "603881": "sideways"}
        ann = mb.breadth_water_annotation(breadth, regimes)
        assert ann["level"] == "weak"
        assert ann["divergence"] == {"002015": "breadth_weak_vs_up"}
        assert "观察期" in ann["note"]
        assert "不改判定" in ann["note"]


class TestRegimeIntegration:
    """run_regime 增 breadth 键：fail-safe、判定字段零变化。"""

    def _run_regime(self, monkeypatch, breadth_payload, tmp_path=None):
        import services.t_trade_signal as tts

        bars = [
            {"date": f"2026-10-{d:02d}", "close": 10.0 + d * 0.05,
             "high": 10.5 + d * 0.05, "low": 9.5 + d * 0.05, "volume": 1000.0}
            for d in range(1, 31)
        ]

        def fake_bars(code, for_date):
            # 30 根合成日线
            rows = [
                {"trade_date": f"2026-10-{d:02d}", "close": 10.0 + d * 0.05}
                for d in range(1, 31)
            ]
            return rows

        monkeypatch.setattr(
            tts, "load_t_pool",
            lambda: {"pool_source": "test", "updated_at": None, "holding_source": None,
                     "path": None,
                     "holdings": [{"code": "002015.SZ", "name": "协鑫能科", "qty": 4000}]})

        captured = {}

        def fake_compute(bars):
            captured["bars"] = bars
            return {"regime": "sideways", "ma5": 10.0, "ma10": 10.0, "ma20": 10.0,
                    "pos20": 0.5, "hi20": 11.0, "lo20": 9.0, "close": 10.5,
                    "chg5d_pct": 1.0, "t1_date": "2026-11-10"}

        monkeypatch.setattr(tts, "compute_regime", fake_compute)
        monkeypatch.setattr(tts, "ch_daily_bars", fake_bars)
        monkeypatch.setattr(tts, "resolve_params",
                            lambda code, regime, info: {"source": "test"})
        breadth_loader = lambda: breadth_payload  # noqa: E731
        return tts.run_regime(
            for_date=__import__("datetime").date(2026, 11, 11),
            now=datetime(2026, 11, 11, 9, 25, tzinfo=BJ),
            out_dir=tmp_path,
            breadth_loader=breadth_loader,
        )

    def test_breadth_key_present_with_annotation(self, monkeypatch, tmp_path):
        breadth = {"date": "2026-11-10", "above_ma20_pct": 35.0}
        payload = self._run_regime(monkeypatch, breadth, tmp_path)
        assert payload["breadth"]["date"] == "2026-11-10"
        assert payload["breadth"]["level"] == "weak"
        assert payload["breadth"]["divergence"] == {}

    def test_breadth_unavailable_fail_safe(self, monkeypatch, tmp_path):
        payload = self._run_regime(monkeypatch, None, tmp_path)
        assert payload["breadth"]["status"] == "unavailable"
        assert "002015" in payload["codes"], "regime 判定不受 breadth 影响"

    def test_breadth_loader_exception_fail_safe(self, monkeypatch, tmp_path):
        import services.t_trade_signal as tts

        def boom():
            raise RuntimeError("ch down")

        monkeypatch.setattr(tts, "load_t_pool",
                            lambda: {"pool_source": "test", "updated_at": None,
                                     "holding_source": None, "path": None,
                                     "holdings": [{"code": "002015.SZ", "name": "x", "qty": 1}]})
        monkeypatch.setattr(tts, "ch_daily_bars", lambda code, d: [])
        payload = tts.run_regime(
            for_date=__import__("datetime").date(2026, 11, 11),
            now=datetime(2026, 11, 11, 9, 25, tzinfo=BJ),
            out_dir=tmp_path,
            breadth_loader=boom,
        )
        assert payload["breadth"]["status"] == "unavailable"

    def test_judgment_fields_unchanged(self, monkeypatch, tmp_path):
        with_b = self._run_regime(monkeypatch, {"date": "2026-11-10", "above_ma20_pct": 35.0}, tmp_path)
        without_b = self._run_regime(monkeypatch, None, tmp_path)
        assert with_b["codes"] == without_b["codes"], "判定字段必须逐字节一致（不改判定式）"
