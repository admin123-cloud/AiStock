"""acct1 AR4.5-L3b 盘中智能买卖提醒引擎单元测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from services.g3 import acct1_intraday_advisor as adv

BJ_TZ = timezone(timedelta(hours=8))


def _mock_overview(avail_cash: float = 520.14, gcl_qty: int = 4800) -> dict:
    return {
        "accounts": [
            {
                "account_id": "acct1",
                "label": "广发证券-曾*",
                "generated_at": "2026-11-23T10:12:36+08:00",
                "funds": {
                    "total_assets": round(avail_cash + gcl_qty * 11.18 + 21500 * 1.266 + 6200 * 3.402, 2),
                    "available": avail_cash,
                    "market_value": round(gcl_qty * 11.18 + 21500 * 1.266 + 6200 * 3.402, 2),
                },
                "holdings": [
                    {
                        "code": "002015",
                        "name": "协鑫能科",
                        "quantity": gcl_qty,
                        "available_quantity": gcl_qty,
                        "cost_price": 17.10,
                        "current_price": 11.18,
                        "market_value": round(gcl_qty * 11.18, 2),
                    },
                    {
                        "code": "512890",
                        "name": "红利低波ETF",
                        "quantity": 21500,
                        "available_quantity": 0,
                        "cost_price": 1.263,
                        "current_price": 1.266,
                        "market_value": round(21500 * 1.266, 2),
                    },
                    {
                        "code": "510880",
                        "name": "红利ETF",
                        "quantity": 6200,
                        "available_quantity": 6200,
                        "cost_price": 3.404,
                        "current_price": 3.402,
                        "market_value": round(6200 * 3.402, 2),
                    },
                ],
            }
        ]
    }


def _mock_radar(is_offensive: bool = False) -> dict:
    return {
        "regime": "offensive" if is_offensive else "defensive",
        "is_offensive": is_offensive,
        "etf_radar": {
            "available": True,
            "targets": [
                {"code": "512890", "name": "红利低波ETF", "close": 1.266, "ma20": 1.250, "basket_weight_pct": 56.2, "account_weight_pct": 56.2, "vol_20d_pct": 11.4, "reason": "AR4.5-L3b 风险调整Top2"},
                {"code": "510880", "name": "红利ETF", "close": 3.402, "ma20": 3.395, "basket_weight_pct": 43.8, "account_weight_pct": 43.8, "vol_20d_pct": 14.6, "reason": "AR4.5-L3b 风险调整Top2"},
            ],
            "universe": [
                {"code": "512890", "name": "红利低波ETF", "close": 1.266, "ma20": 1.250},
                {"code": "510880", "name": "红利ETF", "close": 3.402, "ma20": 3.395},
            ],
        },
        "stock_radar": {
            "available": True,
            "is_offensive": is_offensive,
            "exec_queue": [],
        },
    }


def test_defensive_intraday_pushes_sell_skips_tiny_buy_and_cooldowns(tmp_path: Path):
    """防守期现金仅 ¥520 时：推送 002015 卖出指令，跳过不足 ¥2500 的碎单买入；10 分钟后复扫命中冷却不刷屏。"""
    sent_msgs: list[tuple[str, str]] = []

    def _fake_send(title: str, body: str, **kw):
        sent_msgs.append((title, body))
        return {"ok": True, "channel": "ilink_bridge", "detail": "ok"}

    now1 = datetime(2026, 11, 23, 10, 45, 0, tzinfo=BJ_TZ)
    quotes = {
        "002015": {"price": 11.18, "change_pct": -2.1},
        "512890": {"price": 1.266, "change_pct": 0.3},
        "510880": {"price": 3.402, "change_pct": 0.2},
    }
    with patch.object(adv, "send_wechat_notification", side_effect=_fake_send):
        r1 = adv.run_acct1_advisor(
            mode="intraday",
            dry_run=False,
            now_bj=now1,
            base_dir=tmp_path,
            overview_override=_mock_overview(avail_cash=520.14),
            radar_override=_mock_radar(False),
            quotes_override=quotes,
        )
        # 10 分钟后持仓未变，再次巡检应触发 2 小时冷却不重复刷屏
        r2 = adv.run_acct1_advisor(
            mode="intraday",
            dry_run=False,
            now_bj=now1 + timedelta(minutes=10),
            base_dir=tmp_path,
            overview_override=_mock_overview(avail_cash=520.14),
            radar_override=_mock_radar(False),
            quotes_override=quotes,
        )

    assert r1["pushed_count"] == 1
    assert r1["pushed"][0]["code"] == "002015"
    assert r1["pushed"][0]["side"] == "SELL"
    assert "acct1·AR4.5-L3b 卖出" in sent_msgs[0][0]
    assert r2["pushed_count"] == 0


def test_manual_sell_fill_overlay_immediately_unlocks_ar45_etf_buy(tmp_path: Path):
    """登记 acct1 手工卖出 002015 后，自动叠加回款现金并立刻衔接推送站稳 MA20 的 AR4.5-L3b ETF 买单。"""
    sent_msgs: list[tuple[str, str]] = []

    def _fake_send(title: str, body: str, **kw):
        sent_msgs.append((title, body))
        return {"ok": True, "channel": "ilink_bridge", "detail": "ok"}

    now_fill = datetime(2026, 11, 23, 10, 50, 0, tzinfo=BJ_TZ)
    adv.record_acct1_manual_fill(
        code="002015",
        name="协鑫能科",
        side="SELL",
        shares=2700,
        price=11.20,
        now_bj=now_fill,
        base_dir=tmp_path,
    )

    quotes = {
        "002015": {"price": 11.20, "change_pct": -1.9},
        "512890": {"price": 1.266, "change_pct": 0.3},  # >= ma20 1.250，放行买入
        "510880": {"price": 3.350, "change_pct": -1.5}, # < ma20*0.995 (3.378)，盘中破位暂缓买入！
    }
    with patch.object(adv, "send_wechat_notification", side_effect=_fake_send):
        r = adv.run_acct1_advisor(
            mode="intraday",
            dry_run=False,
            now_bj=now_fill + timedelta(minutes=1),
            base_dir=tmp_path,
            overview_override=_mock_overview(avail_cash=520.14),
            radar_override=_mock_radar(False),
            quotes_override=quotes,
        )

    assert r["applied_manual_fills"] == 1
    assert r["available_cash"] > 30000.0
    buys = [x for x in r["pushed"] if x["side"] == "BUY"]
    assert len(buys) == 1
    assert buys[0]["code"] == "512890"
    assert "acct1·AR4.5-L3b 买入" in buys[0]["title"]
    # 510880 盘中跌破 MA20 被拦截跳过
    assert any(s["code"] == "510880" and "etf_below_ma20" in s["reason"] for s in r["skipped"])


def test_buy_blocked_after_1445_cutoff(tmp_path: Path):
    """14:46 后允许卖出但禁止推送新开买仓指令。"""
    now_late = datetime(2026, 11, 23, 14, 46, 0, tzinfo=BJ_TZ)
    quotes = {
        "002015": {"price": 11.18, "change_pct": -2.0},
        "512890": {"price": 1.266, "change_pct": 0.3},
        "510880": {"price": 3.402, "change_pct": 0.2},
    }
    r = adv.run_acct1_advisor(
        mode="intraday",
        dry_run=True,
        now_bj=now_late,
        base_dir=tmp_path,
        overview_override=_mock_overview(avail_cash=40000.0),
        radar_override=_mock_radar(False),
        quotes_override=quotes,
    )
    assert all(x["side"] != "BUY" for x in r["pushed"])
    assert any(s["side"] == "BUY" and s["reason"] == "after_1445_buy_cutoff" for s in r["skipped"])


def test_panic_zone_blocks_early_floor_sell_and_allows_rebound_or_tail(tmp_path: Path):
    """2026-11-23 补丁：极端冰点(宽度<=18%)早盘急跌<=-2.5%禁止地板割肉；盘中反抽或14:35尾盘才放行。"""
    radar_panic = {**_mock_radar(False), "market_breadth_pct": 7.71}
    # 1) 10:55 急杀 -4.21% -> 必须被拦截，不得地板割肉！
    r_floor = adv.run_acct1_advisor(
        mode="intraday",
        dry_run=True,
        now_bj=datetime(2026, 11, 23, 10, 55, 0, tzinfo=BJ_TZ),
        base_dir=tmp_path,
        overview_override=_mock_overview(avail_cash=520.14),
        radar_override=radar_panic,
        quotes_override={
            "002015": {"price": 11.16, "change_pct": -4.21, "vwap": 11.35},
            "512890": {"price": 1.261, "change_pct": -0.5},
            "510880": {"price": 3.392, "change_pct": -1.0},
            "159915": {"price": 2.21, "change_pct": -1.2},
            "512100": {"price": 2.26, "change_pct": -1.5},
        },
    )
    assert all(x["code"] != "002015" for x in r_floor["pushed"])
    assert any(s["code"] == "002015" and "panic_zone_no_floor_sell" in s["reason"] for s in r_floor["skipped"])

    # 2) 下午反抽至 0.0%（同时 159915/512100 大涨触发 AR2_2A 冰点右侧拐点）-> 放行反抽卖出 + 播报拐点
    r_rebound = adv.run_acct1_advisor(
        mode="intraday",
        dry_run=True,
        now_bj=datetime(2026, 11, 23, 14, 15, 0, tzinfo=BJ_TZ),
        base_dir=tmp_path,
        overview_override=_mock_overview(avail_cash=520.14),
        radar_override=radar_panic,
        quotes_override={
            "002015": {"price": 11.65, "change_pct": 0.0, "vwap": 11.40},
            "512890": {"price": 1.258, "change_pct": -1.0},
            "510880": {"price": 3.394, "change_pct": -1.0},
            "159915": {"price": 2.292, "change_pct": 2.78},
            "512100": {"price": 2.360, "change_pct": 2.61},
        },
    )
    assert any(x["code"] == "002015" and x["side"] == "SELL" for x in r_rebound["pushed"])
    assert any(x["code"] == "AR2_2A" and x["side"] == "ALERT" for x in r_rebound["pushed"])


def test_smooth_step_daily_done_stops_second_sell_same_day(tmp_path: Path):
    """2026-11-23 补丁：当日已核销卖出 002015 达总资产 25%+，不再同日二次催卖剩余 2100 股。"""
    now_fill = datetime(2026, 11, 23, 11, 3, 0, tzinfo=BJ_TZ)
    adv.record_acct1_manual_fill(
        code="002015",
        name="协鑫能科",
        side="SELL",
        shares=2700,
        price=11.25,
        now_bj=now_fill,
        base_dir=tmp_path,
    )
    r = adv.run_acct1_advisor(
        mode="intraday",
        dry_run=True,
        now_bj=now_fill + timedelta(minutes=5),
        base_dir=tmp_path,
        overview_override=_mock_overview(avail_cash=520.14),
        radar_override=_mock_radar(False),
        quotes_override={
            "002015": {"price": 11.65, "change_pct": 0.0},
            "512890": {"price": 1.261, "change_pct": -0.5},
            "510880": {"price": 3.394, "change_pct": -1.0},
        },
    )
    assert all(x["code"] != "002015" for x in r["pushed"])
    assert any(s["code"] == "002015" and "smooth_step_daily_done" in s["reason"] for s in r["skipped"])


def test_panic_rebound_uses_live_etf_intraday_over_stale_ch_close():
    """2026-11-23 补丁：盘中 CH 仍是 T-1 日线时，优先采用 159915/512100 实时涨跌幅实时触发右侧信号。"""
    from services.mining import panic_rebound_monitor as prm

    stale_ch = {
        "399006.SZ": {"date": "2026-11-20", "close": 2200.0, "change_pct": -3.54},
        "000852.SH": {"date": "2026-11-20", "close": 5800.0, "change_pct": -4.89},
    }
    live_etfs = {
        "159915": {"price": 2.292, "change_pct": 2.78},
        "512100": {"price": 2.360, "change_pct": 2.61},
    }
    with patch.object(prm, "_fetch_latest_pair_index_chg_from_ch", return_value=stale_ch):
        res = prm.evaluate_panic_rebound_signal(
            market_gate={"sources": {"breadth": {"above_ma20_pct": 7.71}}},
            etf_quote_fetcher=lambda _codes: live_etfs,
        )
    assert res["triggered"] is True
    assert abs(res["metrics"]["pair_avg_change_pct"] - 2.695) < 0.01
    assert res["metrics"]["cyb_change_pct"] == 2.78
    assert res["metrics"]["zz1000_change_pct"] == 2.61


def test_four_regime_contract_matrix_zero_rule_conflict(tmp_path: Path):
    """全 4 态契约矩阵回归测试：防止策略大脑与执行层规则打架（事后才发现问题）。

    1) defensive 常规防守：禁止买 A 股，且防守 ETF 严格要求现价 >= MA20；
    2) defensive + panic_triggered 冰点反抽：159915/512100 虽在 MA20 下方，但标记为 panic_rebound_etf 必须正常放行；
    3) offensive 主升进攻：允许买策略目标 A 股与 ETF。
    """
    now_am = datetime(2026, 11, 24, 9, 45, 0, tzinfo=BJ_TZ)

    # 场景 1：常规防守期（510880 跌破 MA20 被拦截，512890 站稳 MA20 放行）
    r_def = adv.run_acct1_advisor(
        mode="intraday",
        dry_run=True,
        now_bj=now_am,
        base_dir=tmp_path / "s1",
        overview_override=_mock_overview(avail_cash=35000.0, gcl_qty=0),
        radar_override=_mock_radar(False),
        quotes_override={
            "512890": {"price": 1.260, "change_pct": 0.2},  # >= 1.250 放行
            "510880": {"price": 3.380, "change_pct": -0.5}, # < 3.395 拦截
        },
    )
    assert any(p["code"] == "512890" and p["side"] == "BUY" for p in r_def["pushed"])
    assert any(s["code"] == "510880" and "etf_below_ma20" in s["reason"] for s in r_def["skipped"])

    # 场景 2：冰点反抽期（panic_triggered=True，159915/512100 在 MA20 下方必须放行，绝不能被常规 MA20 误杀！）
    radar_panic = {
        "regime": "defensive",
        "is_offensive": False,
        "etf_radar": {
            "available": True,
            "panic_triggered": True,
            "targets": [
                {"code": "159915", "name": "创业板ETF", "close": 2.295, "ma20": 2.451, "basket_weight_pct": 30.0, "reason": "冰点企稳右侧反抽"},
                {"code": "512100", "name": "中证1000ETF", "close": 2.362, "ma20": 2.565, "basket_weight_pct": 30.0, "reason": "冰点企稳右侧反抽"},
                {"code": "512890", "name": "红利低波ETF", "close": 1.256, "ma20": 1.243, "basket_weight_pct": 40.0, "reason": "冰点期避险压舱石"},
            ],
            "universe": [
                {"code": "159915", "name": "创业板ETF", "close": 2.295, "ma20": 2.451},
                {"code": "512100", "name": "中证1000ETF", "close": 2.362, "ma20": 2.565},
                {"code": "512890", "name": "红利低波ETF", "close": 1.256, "ma20": 1.243},
            ],
        },
        "stock_radar": {"available": True, "is_offensive": False, "exec_queue": []},
    }
    r_panic = adv.run_acct1_advisor(
        mode="intraday",
        dry_run=True,
        now_bj=now_am,
        base_dir=tmp_path / "s2",
        overview_override=_mock_overview(avail_cash=35000.0, gcl_qty=0),
        radar_override=radar_panic,
        quotes_override={
            "159915": {"price": 2.300, "change_pct": 0.2},  # 低于 MA20 2.451，但属于 panic_rebound_etf 必须放行！
            "512100": {"price": 2.365, "change_pct": 0.1},  # 低于 MA20 2.565，但属于 panic_rebound_etf 必须放行！
            "512890": {"price": 1.256, "change_pct": 0.0},
        },
    )
    pushed_codes = {p["code"] for p in r_panic["pushed"] if p["side"] == "BUY"}
    assert "159915" in pushed_codes
    assert "512100" in pushed_codes

    # 场景 3：主升进攻期（允许买入 AR4.5-L3b 目标个股）
    radar_off = {
        "regime": "offensive",
        "is_offensive": True,
        "etf_radar": {
            "available": True,
            "panic_triggered": False,
            "targets": [
                {"code": "510300", "name": "沪深300ETF", "close": 4.200, "ma20": 4.050, "basket_weight_pct": 100.0, "reason": "进攻期宽基"},
            ],
            "universe": [{"code": "510300", "name": "沪深300ETF", "close": 4.200, "ma20": 4.050}],
        },
        "stock_radar": {
            "available": True,
            "is_offensive": True,
            "stock_budget_active_pct": 50.0,
            "slot_weight_pct": 12.5,
            "max_slots": 4,
            "exec_queue": [
                {"code": "002415", "name": "海康威视测试", "industry": "电子", "source_label": "F3A", "close": 25.0, "stop_loss_price": 22.5, "notice_date": "2026-10-30", "net_profit_yoy_pct": 35.0},
            ],
        },
    }
    r_off = adv.run_acct1_advisor(
        mode="intraday",
        dry_run=True,
        now_bj=now_am,
        base_dir=tmp_path / "s3",
        overview_override=_mock_overview(avail_cash=35000.0, gcl_qty=0),
        radar_override=radar_off,
        quotes_override={"002415": {"price": 25.10, "change_pct": 0.4}, "510300": {"price": 4.210, "change_pct": 0.2}},
    )
    assert any(p["code"] == "002415" and p["side"] == "BUY" for p in r_off["pushed"])
