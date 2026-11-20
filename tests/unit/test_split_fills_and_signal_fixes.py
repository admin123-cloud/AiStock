# -*- coding: utf-8 -*-
"""回归测试：拆单成交不丢股（2000股全量入账+幂等）与做T信号引擎风控/结算修复。"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from host_bridges.ths_desktop_bridge import _sync_fills_to_l2_closure
from host_bridges.ths_read_account_task import _merge_today_fills_into_read_report
from services.trading import t_trade_signal as tts


def _sample_six_split_fills():
    """镜像 2026-11-20 09:47:45 002015 实际 6 笔拆单（200+200+100+100+100+1300=2000 股），均带截断 fill_no='010300.'。"""
    specs = [(200, 12.04), (200, 12.04), (100, 12.04), (100, 12.04), (100, 12.04), (1300, 12.05)]
    return [
        {
            "code": "002015",
            "name": "协鑫能科",
            "date": "20261120",
            "time": "09:47:45",
            "side": "buy",
            "quantity": float(q),
            "price": px,
            "amount": round(q * px, 2),
            "contract_no": "13998",
            "fill_no": "010300.",
            "source": "today_fills",
        }
        for q, px in specs
    ]


def test_merge_today_fills_preserves_all_split_executions():
    today_rows = _sample_six_split_fills()
    read_rep = {
        "fills": {
            "attempted": True,
            "rows": [
                {"code": "002015", "date": "20261116", "time": "09:35:55", "side": "buy", "quantity": 1000.0, "price": 12.71, "fill_no": "010300."},
            ],
            "status": "partial",
            "row_count": 1,
        }
    }
    _merge_today_fills_into_read_report(read_rep, {"status": "ok", "rows": today_rows})
    merged = read_rep["fills"]["rows"]
    today_merged = [r for r in merged if r["date"] == "20261120"]
    assert len(today_merged) == 6
    assert sum(int(r["quantity"]) for r in today_merged) == 2000
    assert len(merged) == 7  # 6 笔当日 + 1 笔 11-16 历史


def test_sync_multi_fills_sums_all_2000_shares_and_is_idempotent(tmp_path: Path):
    today_rows = _sample_six_split_fills()
    multi_report = {
        "accounts": {
            "acct1": {
                "read_account": {
                    "today_fills": {"status": "ok", "rows": today_rows},
                    "fills": {"status": "partial", "rows": today_rows},
                }
            }
        }
    }
    # 首次同步：6 笔拆单全部入账，open_t.qty == 2000
    s1 = _sync_fills_to_l2_closure(multi_report, for_date=date(2026, 11, 20), t_tracker_root=tmp_path)
    assert s1["acct1"]["status"] == "synced"
    assert s1["acct1"]["applied_buys"] == 6

    st = tts.load_state(tmp_path, date(2026, 11, 20))
    ot = st.open_t("002015")
    assert ot is not None
    assert ot["qty"] == 2000
    assert round(ot["entry_px"], 4) == 12.0465

    # 二次重复采集：100% 幂等，绝不重复加仓
    s2 = _sync_fills_to_l2_closure(multi_report, for_date=date(2026, 11, 20), t_tracker_root=tmp_path)
    assert s2["acct1"]["applied_buys"] == 0
    assert s2["acct1"]["idempotent_skipped"] == 6
    st2 = tts.load_state(tmp_path, date(2026, 11, 20))
    assert st2.open_t("002015")["qty"] == 2000


def test_fast_settle_supports_l2_rsi5_rollup():
    ok, reason = tts._fast_settle_holds(
        {"kind": "l2_rsi5_rollup", "band": [13.0, 13.2], "rsi5_max": 35.0},
        px_c=11.92,
        rsi5=26.9,
        rsi15=24.0,
    )
    assert ok is True
    assert reason == ""


def test_weak_market_blocks_forward_buy_entry():
    now = datetime(2026, 11, 20, 10, 5, 0)
    # 构造 26 根 5m K 线，使 RSI5m 进入超卖区（~20）且 px=13.05 落入买入价带 [13.0, 13.2]
    closes = [14.0]
    for i in range(25):
        closes.append(round(closes[-1] + (0.02 if i % 4 == 0 else -0.05), 3))
    closes[-1] = 13.05
    bars5 = [
        {"ts": f"2026-11-19 13:{10 + i:02d}:00", "open": c, "high": c + 0.02, "low": c - 0.02, "close": c, "volume": 100, "amount": c * 10000}
        for i, c in enumerate(closes)
    ]
    bars5[-1]["ts"] = "2026-11-20 10:05:00"
    bars15 = list(bars5)
    state = tts.SignalState()
    res = tts.evaluate_code(
        code="002015",
        name="协鑫能科",
        regime_info={"regime": "sideways", "ma5": 13.1},
        params={"buy_band": [13.0, 13.2], "buy_rsi5_max": 35.0, "sell_band": [13.7, 13.9], "stop": 12.9},
        bars5=bars5,
        bars15=bars15,
        px=13.05,
        px_source="east_ulist",
        now=now,
        state=state,
        drafts_today=[],
        holding={"code": "002015", "shares": 5100, "account": "acct1"},
        market_breadth={"level": "weak", "above_ma20_pct": 11.01},
    )
    assert res.get("draft") is None
    sup = res.get("suppressed")
    assert isinstance(sup, dict) and sup.get("reason") == "weak_market_block_forward_buy"
