# -*- coding: utf-8 -*-
"""R37b host 侧 THS 当日成交导出器：解析/合并/幂等/不可达 fail-closed 单测。

fixture 全部纯合成；R32 当日成交 OCR 样本取自
runtime/ths_holdings_r32/evidence/r32_taskA_round5_today_trades.json 的 preview_lines
（2026-11-10 hexin 2026.9.22.14 实拍，两笔 002015 卖单）。零 THS 依赖、零真实路径写。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from scripts.ths_client import (
    build_payload,
    merge_fills,
    parse_today_trades_lines,
    run_export,
    should_skip_existing,
)

# --- R32 实拍证据：当日成交视图 OCR preview_lines（2026-11-10，含左侧栏噪声） ---
R32_TODAY_TRADES_LINES = [
    "资产：115796.88可用：82239.88当日：+1107.00+0.97%\t撤\t帮助\t反馈\t设置\t旧版\t口",
    "买\t卖\t撤\t查\t广发证券-曾*\t|添加\t退出\t登录",
    "买买入[F1]\t汇总",
    "卖卖出[F2]\t成交时间\t证券代码\t证券名称\t操作\t成交数量\t成交均价\t成交金额\t合同编号\t成交编号",
    "撤单[F3]\t09:38:58\t002015\t协盘能科\t证券卖出\t1900\t13.720\t26068.000\t12370\t0103000...",
    "自选股\t09:38:52\t002015\t协盘能科\t证券卖出\t100\t13.720\t1372.000\t12370\t0103000..",
    "双向委托",
    "查询[F4]",
    "资金股票",
    "当日成交",
    "当日委托",
    "历史成交",
    "历史委托",
]

FOR_DATE = date(2026, 11, 10)

# R32 报告：历史成交 8 笔（11-03~11-09 全量，bridge `_parse_trades_from_ocr_items` 行形状）
R32_HISTORY_ROWS = [
    {"trade_time": "2026-11-09 14:55:17", "side": "BUY", "code": "002015", "name": "协鑫能科",
     "shares": 2000, "price": 13.44, "amount": 26880.0, "source": "ths_trade_ocr_snapshot"},
    {"trade_time": "2026-11-06 13:00:55", "side": "SELL", "code": "002015", "name": "协鑫能科",
     "shares": 2200, "price": 13.67, "amount": 30074.0, "source": "ths_trade_ocr_snapshot"},
    {"trade_time": "2026-11-06 09:31:11", "side": "BUY", "code": "002015", "name": "协鑫能科",
     "shares": 2000, "price": 13.16, "amount": 26320.0, "source": "ths_trade_ocr_snapshot"},
    {"trade_time": "2026-11-05 13:07:21", "side": "SELL", "code": "002015", "name": "协鑫能科",
     "shares": 2100, "price": 13.12, "amount": 27552.0, "source": "ths_trade_ocr_snapshot"},
    {"trade_time": "2026-11-04 13:46:20", "side": "SELL", "code": "002015", "name": "协鑫能科",
     "shares": 2000, "price": 12.49, "amount": 24980.0, "source": "ths_trade_ocr_snapshot"},
    {"trade_time": "2026-11-04 13:05:49", "side": "SELL", "code": "002015", "name": "协鑫能科",
     "shares": 2000, "price": 12.25, "amount": 24500.0, "source": "ths_trade_ocr_snapshot"},
    {"trade_time": "2026-11-03 13:09:29", "side": "BUY", "code": "002015", "name": "协鑫能科",
     "shares": 2000, "price": 12.12, "amount": 24240.0, "source": "ths_trade_ocr_snapshot"},
    {"trade_time": "2026-11-03 10:16:57", "side": "SELL", "code": "002015", "name": "协鑫能科",
     "shares": 2000, "price": 12.31, "amount": 24620.0, "source": "ths_trade_ocr_snapshot"},
]


# ---------------------------------------------------------------------------
# parse_today_trades_lines
# ---------------------------------------------------------------------------

class TestParseTodayTrades:
    def test_r32_evidence_two_fills_exact(self):
        res = parse_today_trades_lines(R32_TODAY_TRADES_LINES, FOR_DATE)
        assert res["header_seen"] is True
        assert res["parsed"] == 2 and res["skipped_count"] == 0
        rows = res["rows"]
        assert rows[0] == {
            "trade_time": "2026-11-10 09:38:52", "side": "SELL", "code": "002015",
            "name": "协盘能科", "shares": 100, "price": 13.72, "amount": 1372.0,
            "source": "ths_today_trades_ocr",
        }
        assert rows[1]["trade_time"] == "2026-11-10 09:38:58"
        assert rows[1]["shares"] == 1900 and rows[1]["amount"] == 26068.0

    def test_buy_side_row(self):
        lines = [
            "卖卖出[F2]\t成交时间\t证券代码\t证券名称\t操作\t成交数量\t成交均价\t成交金额",
            "撤单[F3]\t09:31:08\t300054\t鼎龙股份\t证券买入\t100\t61.770\t6177.000",
        ]
        res = parse_today_trades_lines(lines, FOR_DATE)
        assert res["parsed"] == 1
        assert res["rows"][0]["side"] == "BUY" and res["rows"][0]["code"] == "300054"

    def test_header_only_zero_trades_is_clean_zero(self):
        lines = [
            "卖卖出[F2]\t成交时间\t证券代码\t证券名称\t操作\t成交数量\t成交均价\t成交金额",
            "查询[F4]\t资金股票\t当日成交\t历史成交",
        ]
        res = parse_today_trades_lines(lines, FOR_DATE)
        assert res["header_seen"] is True
        assert res["rows"] == [] and res["parsed"] == 0 and res["skipped_count"] == 0

    def test_no_header_marks_view_unconfirmed(self):
        res = parse_today_trades_lines(["买\t卖\t撤\t查\t广发证券-曾*"], FOR_DATE)
        assert res["header_seen"] is False and res["rows"] == []

    def test_duplicate_rows_deduped(self):
        lines = [
            "卖卖出[F2]\t成交时间\t证券代码\t证券名称\t操作\t成交数量\t成交均价\t成交金额",
            "撤单[F3]\t09:38:58\t002015\t协盘能科\t证券卖出\t1900\t13.720\t26068.000\t12370",
            "自选股\t09:38:58\t002015\t协盘能科\t证券卖出\t1900\t13.720\t26068.000\t12370",
        ]
        res = parse_today_trades_lines(lines, FOR_DATE)
        assert res["parsed"] == 1

    def test_missing_amount_uses_price_times_shares(self):
        lines = [
            "卖卖出[F2]\t成交时间\t证券代码\t证券名称\t操作\t成交数量\t成交均价\t成交金额",
            # 成交金额列 OCR 丢失，合同编号 12370（整数形态）滑入 → 不得当金额
            "撤单[F3]\t10:01:00\t002015\t协鑫能科\t证券卖出\t2000\t13.500\t12370",
        ]
        res = parse_today_trades_lines(lines, FOR_DATE)
        assert res["parsed"] == 1 and res["skipped_count"] == 0
        row = res["rows"][0]
        assert row["shares"] == 2000 and row["price"] == 13.5
        assert row["amount"] == pytest.approx(27000.0)

    def test_broken_price_row_counted_skipped(self):
        lines = [
            "卖卖出[F2]\t成交时间\t证券代码\t证券名称\t操作\t成交数量\t成交均价\t成交金额",
            "撤单[F3]\t10:01:00\t002015\t协鑫能科\t证券卖出\t12370",
        ]
        res = parse_today_trades_lines(lines, FOR_DATE)
        assert res["parsed"] == 0 and res["skipped_count"] == 1
        assert res["skipped"] and "price_missing" in res["skipped"][0]

    def test_sidebar_line_with_amount_but_no_time_not_a_row(self):
        # 资金持仓视图等其它视图行（有时间无代码/无时间）不得混入
        lines = [
            "查询[F4]\t操作\t证券代码▼证券名称\t股票余额\t可用余额\t市值",
            "资金股票\t三\t002015\t协盘能科\t2000\t2000\t27280.000\t22.377\t13.640",
        ]
        res = parse_today_trades_lines(lines, FOR_DATE)
        assert res["rows"] == [] and res["skipped_count"] == 0

    def test_name_cell_ocr_misread_kept_verbatim(self):
        # R32 实拍「协盘能科」（协鑫能科 OCR 误读）原样保留，不纠错不造数
        res = parse_today_trades_lines(R32_TODAY_TRADES_LINES, FOR_DATE)
        assert all(r["name"] == "协盘能科" for r in res["rows"])


# ---------------------------------------------------------------------------
# merge_fills
# ---------------------------------------------------------------------------

class TestMergeFills:
    def test_history_filtered_to_pairing_window_and_sorted(self):
        today = parse_today_trades_lines(R32_TODAY_TRADES_LINES, FOR_DATE)["rows"]
        merged = merge_fills(today, R32_HISTORY_ROWS, FOR_DATE, window_days=10)
        assert [r["trade_time"] for r in merged] == sorted(r["trade_time"] for r in merged)
        dates = {r["trade_time"][:10] for r in merged}
        assert FOR_DATE.isoformat() in dates
        assert all(d < FOR_DATE.isoformat() for d in dates - {FOR_DATE.isoformat()})

    def test_history_older_than_window_dropped(self):
        today = parse_today_trades_lines(R32_TODAY_TRADES_LINES, FOR_DATE)["rows"]
        stale = [dict(R32_HISTORY_ROWS[0], trade_time="2026-10-09 14:55:17")]
        merged = merge_fills(today, stale, FOR_DATE, window_days=10)
        assert len(merged) == 2  # 只剩当日两笔

    def test_history_future_rows_dropped(self):
        today = parse_today_trades_lines(R32_TODAY_TRADES_LINES, FOR_DATE)["rows"]
        future = [dict(R32_HISTORY_ROWS[0], trade_time="2026-11-11 09:31:00")]
        merged = merge_fills(today, future, FOR_DATE, window_days=10)
        assert len(merged) == 2

    def test_protocol_rows_accepted_by_r37_normalize_fill(self):
        from services.t_trade_signal import normalize_fill

        today = parse_today_trades_lines(R32_TODAY_TRADES_LINES, FOR_DATE)["rows"]
        merged = merge_fills(today, R32_HISTORY_ROWS, FOR_DATE, window_days=10)
        assert len(merged) == 10  # 2 当日 + 8 历史
        assert all(normalize_fill(r) is not None for r in merged)


# ---------------------------------------------------------------------------
# payload / 幂等
# ---------------------------------------------------------------------------

class TestPayloadAndIdempotency:
    def test_payload_shape(self):
        rows = parse_today_trades_lines(R32_TODAY_TRADES_LINES, FOR_DATE)["rows"]
        payload = build_payload(rows, status="ok", trade_date=FOR_DATE,
                                captured_at="2026-11-10 21:00:00", history_rows=4)
        assert payload["schema"] == "ths_fills_export/1"
        assert payload["status"] == "ok" and payload["fills"] is rows
        assert payload["history_rows_merged"] == 4

    def test_skip_when_postclose_ok_file_exists(self):
        existing = {"status": "ok", "trade_date": "2026-11-10",
                    "captured_at": "2026-11-10 15:05:12"}
        assert should_skip_existing(existing, FOR_DATE, datetime(2026, 11, 10, 21, 0)) is True

    def test_no_skip_before_close_allows_refresh(self):
        existing = {"status": "ok", "trade_date": "2026-11-10",
                    "captured_at": "2026-11-10 12:00:00"}
        assert should_skip_existing(existing, FOR_DATE, datetime(2026, 11, 10, 15, 5)) is False

    def test_no_skip_other_date_or_unavailable_status(self):
        other_day = {"status": "ok", "trade_date": "2026-11-09",
                     "captured_at": "2026-11-09 15:30:00"}
        assert should_skip_existing(other_day, FOR_DATE, datetime(2026, 11, 10, 21, 0)) is False
        assert should_skip_existing(None, FOR_DATE, datetime(2026, 11, 10, 21, 0)) is False


# ---------------------------------------------------------------------------
# run_export（注入 capture，不触真机）
# ---------------------------------------------------------------------------

def _capture_ok(_evidence_dir=None):
    return {
        "today_lines": R32_TODAY_TRADES_LINES,
        "history_rows": [R32_HISTORY_ROWS[0]],  # 仅 11-09 买（跨日 T3 配对）
        "history_header_seen": True,
        "window_state": {"minimized_before": False},
    }


class TestRunExport:
    def test_write_fills_file_and_skip_on_second_run(self, tmp_path: Path):
        out = tmp_path / "t_tracker"
        now = datetime(2026, 11, 10, 21, 0, 0)
        first = run_export(for_date=FOR_DATE, now=now, out_root=out,
                           force=False, capture=_capture_ok)
        assert first["action"] == "write" and first["status"] == "ok"
        fills_file = out / "fills" / "20261110.json"
        payload = json.loads(fills_file.read_text(encoding="utf-8"))
        assert len(payload["fills"]) == 3  # 2 当日卖 + 1 前日买
        # 幂等：盘后 ok 文件存在 → 第二次跳过
        second = run_export(for_date=FOR_DATE, now=now, out_root=out,
                            force=False, capture=_capture_ok)
        assert second["action"] == "skip"

    def test_force_reruns(self, tmp_path: Path):
        out = tmp_path / "t_tracker"
        now = datetime(2026, 11, 10, 21, 0, 0)
        run_export(for_date=FOR_DATE, now=now, out_root=out, force=False, capture=_capture_ok)
        again = run_export(for_date=FOR_DATE, now=now, out_root=out,
                           force=True, capture=_capture_ok)
        assert again["action"] == "write"

    def test_ths_unreachable_writes_marker_not_fills(self, tmp_path: Path):
        def capture_fail(_evidence_dir=None):
            raise RuntimeError("window not found: xiadan")

        res = run_export(for_date=FOR_DATE, now=datetime(2026, 11, 10, 21, 0),
                         out_root=tmp_path, force=False, capture=capture_fail)
        assert res["action"] == "unavailable"
        marker = tmp_path / "fills" / "20261110.unavailable.json"
        body = json.loads(marker.read_text(encoding="utf-8"))
        assert body["status"] == "unavailable" and "window not found" in body["reason"]
        assert not (tmp_path / "fills" / "20261110.json").exists()

    def test_today_view_not_confirmed_writes_marker(self, tmp_path: Path):
        def capture_wrong_view(_evidence_dir=None):
            return {"today_lines": ["资金股票\t三\t002015\t协盘能科\t2000"],
                    "history_rows": [], "history_header_seen": True,
                    "window_state": {}}

        res = run_export(for_date=FOR_DATE, now=datetime(2026, 11, 10, 21, 0),
                         out_root=tmp_path, force=False, capture=capture_wrong_view)
        assert res["action"] == "unavailable"
        assert "header" in res["reason"]

    def test_partial_ocr_rows_fail_closed(self, tmp_path: Path):
        def capture_partial(_evidence_dir=None):
            lines = list(R32_TODAY_TRADES_LINES) + [
                "撤单[F3]\t14:50:00\t002015\t协鑫能科\t证券卖出\t12370",
            ]
            return {"today_lines": lines, "history_rows": [],
                    "history_header_seen": True, "window_state": {}}

        res = run_export(for_date=FOR_DATE, now=datetime(2026, 11, 10, 21, 0),
                         out_root=tmp_path, force=False, capture=capture_partial)
        assert res["action"] == "unavailable"
        assert "partial" in res["reason"]

    def test_zero_trades_day_writes_empty_ok_file(self, tmp_path: Path):
        def capture_zero(_evidence_dir=None):
            return {"today_lines": [
                "卖卖出[F2]\t成交时间\t证券代码\t证券名称\t操作\t成交数量\t成交均价\t成交金额",
                "查询[F4]\t资金股票\t当日成交\t历史成交",
            ], "history_rows": [R32_HISTORY_ROWS[0]], "history_header_seen": True,
                "window_state": {}}

        res = run_export(for_date=FOR_DATE, now=datetime(2026, 11, 10, 21, 0),
                         out_root=tmp_path, force=False, capture=capture_zero)
        assert res["action"] == "write" and res["status"] == "zero_trades"
        payload = json.loads((tmp_path / "fills" / "20261110.json").read_text(encoding="utf-8"))
        # 当日零成交=合法零（空当日行），fills 仅保留前日配对行；≠unavailable，不造数
        assert all(r["trade_time"].startswith("2026-11-09") for r in payload["fills"])
        assert res["fills_today"] == 0

    def test_history_view_unreadable_fail_closed(self, tmp_path: Path):
        def capture_history_broken(_evidence_dir=None):
            return {"today_lines": R32_TODAY_TRADES_LINES, "history_rows": [],
                    "history_header_seen": False, "window_state": {}}

        res = run_export(for_date=FOR_DATE, now=datetime(2026, 11, 10, 21, 0),
                         out_root=tmp_path, force=False, capture=capture_history_broken)
        assert res["action"] == "unavailable"

    def test_end_to_end_reconcile_reproduces_r37_baseline(self, tmp_path: Path):
        """导出的 fills 经 R37 reconcile：复现 2026-11-10 跨日 T3 基线 +536.73。"""
        from services import t_trade_signal as tts

        out = tmp_path / "t_tracker"
        run_export(for_date=FOR_DATE, now=datetime(2026, 11, 10, 21, 0),
                   out_root=out, force=False, capture=_capture_ok)
        payload = json.loads((out / "fills" / "20261110.json").read_text(encoding="utf-8"))

        # replay 草案（T20261110_002015_sell_01）原样注入对账
        drafts = [{
            "schema": "t_tracker_beat/1", "id": "T20261110_002015_sell_01",
            "ts": "2026-11-10 09:41:00", "code": "002015", "name": "协鑫能科",
            "side": "sell-T", "subtype": "zone", "regime": "sideways",
            "px": 13.84, "trigger": {"kind": "sell_band", "band": [13.7, 13.9]},
            "qty": 600, "target_band": [13.0, 13.2],
        }]
        summary = tts.run_reconcile(
            for_date=FOR_DATE, now=datetime(2026, 11, 10, 21, 0), out_dir=out,
            fills_loader=lambda _d: (payload["fills"], "file:20261110.json"),
            drafts_rows=drafts)
        assert summary["fills_source"] == "file:20261110.json"
        assert summary["fills_today"] == 2
        assert summary["t_round_trips"] == 2
        assert summary["net_pnl"] == 536.73
        assert summary["matched_draft_ids"] == ["T20261110_002015_sell_01"]

    def test_end_to_end_full_history_same_today_numbers(self, tmp_path: Path):
        """注入全部 R32 历史（11-03 起）也不改变当日净利 +536.73（FIFO 消耗序不变）。"""
        from services import t_trade_signal as tts

        out = tmp_path / "t_tracker"

        def capture_full_history(_evidence_dir=None):
            return {"today_lines": R32_TODAY_TRADES_LINES,
                    "history_rows": R32_HISTORY_ROWS,
                    "history_header_seen": True, "window_state": {}}

        run_export(for_date=FOR_DATE, now=datetime(2026, 11, 10, 21, 0),
                   out_root=out, force=False, capture=capture_full_history)
        payload = json.loads((out / "fills" / "20261110.json").read_text(encoding="utf-8"))
        assert len(payload["fills"]) == 10
        summary = tts.run_reconcile(
            for_date=FOR_DATE, now=datetime(2026, 11, 10, 21, 0), out_dir=out,
            fills_loader=lambda _d: (payload["fills"], "file:20261110.json"),
            drafts_rows=[])
        assert summary["t_round_trips"] == 2
        assert summary["net_pnl"] == 536.73
