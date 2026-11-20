# -*- coding: utf-8 -*-
"""G3 做T单向微信通知模块单元测试。"""
from __future__ import annotations

import services.trading.t_notify as tn


def test_format_draft_message():
    title, body = tn.format_draft_message({
        "id": "T20261120_002015_buy_01",
        "ts": "2026-11-20 10:02:23",
        "code": "002015",
        "name": "协鑫能科",
        "side": "buy",
        "px": 11.94,
        "qty": 1300,
        "trigger": {"kind": "fast_l2_rsi5_rollup", "rsi5": 28.7, "rsi15": 23.6, "vwap": 12.052},
    })
    assert "买入T信号" in title
    assert "协鑫能科" in title
    assert "11.94" in title
    assert "1300" in body
    assert "RSI5m=`28.7`" in body


def test_mask_secret():
    assert tn._mask_secret("") == ""
    assert tn._mask_secret("123456") == "******"
    assert tn._mask_secret("abcd12345678wxyz") == "abcd****wxyz"


def test_send_wechat_missing_credentials():
    res = tn.send_wechat_notification("t", "b", config={"channel": "pushplus", "pushplus_token": ""})
    assert res["ok"] is False
    assert "Token" in res["detail"]

    res2 = tn.send_wechat_notification("t", "b", config={"channel": "wecom_webhook", "wecom_webhook": ""})
    assert res2["ok"] is False
    assert "Webhook" in res2["detail"]
