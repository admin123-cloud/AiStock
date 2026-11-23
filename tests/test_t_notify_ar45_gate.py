"""测试做T推送层与 L2 心跳层的 AR4.5-L3b 总闸门硬拦截与 30 分钟防刷屏冷却。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from services.trading import t_notify as tn

BJ_TZ = timezone(timedelta(hours=8))


def _write_draft(tracker_dir: Path, draft: dict) -> None:
    pp = tracker_dir / "pending_push"
    pp.mkdir(parents=True, exist_ok=True)
    (pp / f"{draft['id']}.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")


def test_defensive_gate_blocks_buy_t_and_allows_sell_t(tmp_path: Path):
    """AR4.5-L3b 防守/恐慌期：100% 拦截 buy-T 推送，正常放行 sell-T 高抛减仓信号。"""
    tracker_dir = tmp_path / "t_tracker"
    store_dir = tmp_path / "g3_desk_plans"
    store_dir.mkdir(parents=True, exist_ok=True)

    now_bj = datetime(2026, 11, 23, 10, 29, 0, tzinfo=BJ_TZ)
    _write_draft(
        tracker_dir,
        {
            "id": "L220261123_002015_buy_102831",
            "ts": "2026-11-23 10:28:31",
            "code": "002015",
            "name": "协鑫能科",
            "side": "buy-T",
            "qty": 1200,
            "price": 11.19,
            "signal": {"rsi15": 19.5, "rsi5": 21.0, "type": "oversold"},
        },
    )
    _write_draft(
        tracker_dir,
        {
            "id": "L220261123_002015_sell_102845",
            "ts": "2026-11-23 10:28:45",
            "code": "002015",
            "name": "协鑫能科",
            "side": "sell-T",
            "qty": 1200,
            "price": 12.10,
            "signal": {"rsi15": 82.1, "rsi5": 76.4, "type": "overheat"},
        },
    )

    sent_msgs: list[tuple[str, str]] = []

    def _fake_send(title: str, body: str, **kw):
        sent_msgs.append((title, body))
        return {"ok": True, "channel": "ilink_bridge", "detail": "ok"}

    with (
        patch.object(tn, "_store_dir", return_value=store_dir),
        patch.object(tn, "load_notify_config", return_value={"enabled": True, "notify_on_draft": True, "only_trading_hours": True, "channel": "ilink_bridge"}),
        patch.object(tn, "send_wechat_notification", side_effect=_fake_send),
    ):
        sent = tn.poll_and_push_new_drafts_once(
            now_bj=now_bj,
            tracker_dir_override=tracker_dir,
            gate_info_override={"allow_buy": False, "regime": "defensive", "regime_label": "极端防守/恐慌冰点"},
        )

    assert sent == 1
    assert len(sent_msgs) == 1
    assert "卖出T/高抛减仓信号" in sent_msgs[0][0]
    assert "RSI15m=`82.1`" in sent_msgs[0][1]
    assert "极端防守/恐慌冰点" in sent_msgs[0][1]


def test_same_side_30min_cooldown_prevents_30s_spam_and_drops_stale(tmp_path: Path):
    """验证 32 秒内不同 draft_id 同向信号仅推送 1 次，且超过 15 分钟滞留旧草案自动丢弃。"""
    tracker_dir = tmp_path / "t_tracker"
    store_dir = tmp_path / "g3_desk_plans"
    store_dir.mkdir(parents=True, exist_ok=True)

    now_bj = datetime(2026, 11, 23, 10, 30, 0, tzinfo=BJ_TZ)
    # 1) 两条间隔 32 秒的同向卖信号
    _write_draft(
        tracker_dir,
        {
            "id": "L220261123_002015_sell_102831",
            "ts": "2026-11-23 10:28:31",
            "code": "002015",
            "name": "协鑫能科",
            "side": "sell-T",
            "qty": 1200,
            "price": 12.10,
            "signal": {"rsi15": 82.0, "rsi5": 75.0, "type": "overheat"},
        },
    )
    _write_draft(
        tracker_dir,
        {
            "id": "L220261123_002015_sell_102903",
            "ts": "2026-11-23 10:29:03",
            "code": "002015",
            "name": "协鑫能科",
            "side": "sell-T",
            "qty": 1200,
            "price": 12.11,
            "signal": {"rsi15": 82.5, "rsi5": 76.0, "type": "overheat"},
        },
    )
    # 2) 一条 30 分钟前的滞留旧信号（应自动跳过不发）
    _write_draft(
        tracker_dir,
        {
            "id": "L220261123_300054_sell_095000",
            "ts": "2026-11-23 09:50:00",
            "code": "300054",
            "name": "鼎龙股份",
            "side": "sell-T",
            "qty": 100,
            "price": 56.0,
            "signal": {"rsi15": 81.0, "rsi5": 74.0, "type": "overheat"},
        },
    )

    sent_msgs: list[tuple[str, str]] = []

    def _fake_send(title: str, body: str, **kw):
        sent_msgs.append((title, body))
        return {"ok": True, "channel": "ilink_bridge", "detail": "ok"}

    with (
        patch.object(tn, "_store_dir", return_value=store_dir),
        patch.object(tn, "load_notify_config", return_value={"enabled": True, "notify_on_draft": True, "only_trading_hours": True, "channel": "ilink_bridge"}),
        patch.object(tn, "send_wechat_notification", side_effect=_fake_send),
    ):
        sent = tn.poll_and_push_new_drafts_once(
            now_bj=now_bj,
            tracker_dir_override=tracker_dir,
            gate_info_override={"allow_buy": False, "regime": "defensive", "regime_label": "极端防守/恐慌冰点"},
        )

    assert sent == 1
    assert len(sent_msgs) == 1
    assert "002015" in sent_msgs[0][0]


def test_get_ar45_buy_gate_status_imports_real_evaluator():
    """防止导入函数名错误退化为 unknown_fail_closed。"""
    with patch(
        "services.g3.market_regime_gate.evaluate_market_regime_gate",
        return_value={"regime": "defensive", "regime_label": "极端防守/恐慌冰点", "allow_new_mainwave": False},
    ):
        st = tn.get_ar45_buy_gate_status(force_refresh=True)
    assert st["allow_buy"] is False
    assert st["regime"] == "defensive"
    assert st["regime_label"] == "极端防守/恐慌冰点"

    with patch(
        "services.g3.market_regime_gate.evaluate_market_regime_gate",
        return_value={"regime": "offensive", "regime_label": "主升进攻期", "allow_new_mainwave": True},
    ):
        st2 = tn.get_ar45_buy_gate_status(force_refresh=True)
    assert st2["allow_buy"] is True
    assert st2["regime"] == "offensive"
