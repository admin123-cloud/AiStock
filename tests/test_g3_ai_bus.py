# -*- coding: utf-8 -*-
"""AiStock <-> Antigravity 异步 AI 决策总线与双通道统一监听器单测。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from services.g3 import ai_bus

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host_bridges"))
import weixin_session_helper as helper  # noqa: E402


def test_ai_bus_submit_wait_respond_and_heartbeat(tmp_path, monkeypatch) -> None:
    bus_dir = tmp_path / "ai_bus"
    monkeypatch.setenv("AISTOCK_AI_BUS_DIR", str(bus_dir))

    # 1. 未写心跳时，同步等待应秒级降级（offline_fallback）并触发微信+邮箱双告警
    sent_alerts: list[tuple[str, str]] = []
    orig_notify = ai_bus.notify_ai_session_alert

    def _spy_notify(**kw):
        kw.setdefault("notify_fn", lambda ch, t, b: (sent_alerts.append((ch, t)), {"ok": True})[1])
        return orig_notify(**kw)

    monkeypatch.setattr(ai_bus, "notify_ai_session_alert", _spy_notify)

    off = ai_bus.submit_ai_job(kind="postopen_0940", prompt="早盘研判", wait_timeout_sec=5.0)
    assert off["ok"] is False
    assert off["status"] == "offline_fallback"
    assert [c for c, _ in sent_alerts] == ["ilink_bridge", "qq_email"]

    # 冷却期内再次离线巡检不刷屏
    chk_cool = ai_bus.check_session_and_alert(
        cooldown_sec=1800.0,
        notify_fn=lambda ch, t, b: (sent_alerts.append((ch, t)), {"ok": True})[1],
    )
    assert chk_cool["online"] is False
    assert chk_cool["alerted"] is False
    assert len(sent_alerts) == 2

    # 2. 刷新心跳后在线，自动复位离线冷却；提交异步任务落盘至 pending/
    ai_bus.touch_session_heartbeat()
    st0 = ai_bus.get_bus_status()
    assert st0["session_online"] is True

    sub = ai_bus.submit_ai_job(kind="desk_qa", prompt="当前大盘怎么看", context={"regime": "defensive"})
    assert sub["ok"] is True
    assert sub["status"] == "pending"
    jid = sub["job_id"]
    assert (bus_dir / "pending" / f"{jid}.json").is_file()

    # 3. 统一监听器 cmd_wait 检测到 ai_bus pending 后立即返回 0，并自动注入 live_strategy_constitution
    wx_dir = tmp_path / "weixin"
    monkeypatch.setattr(
        helper,
        "_fetch_live_strategy_constitution",
        lambda api_base="": {"available": True, "strategy_version": "AR4.6", "grounding_rule": "SSOT"},
    )
    rc_wait = helper.cmd_wait(wx_dir, bus_dir, interval=0.2, timeout=2.0, history_limit=5)
    assert rc_wait == 0

    # 4. 通过 respond-job 写回结果并自动归档至 done/
    rc_resp = helper.cmd_respond_job(
        bus_dir,
        jid,
        answer="当前大盘宽度 9.72% 处冰点，守红利低波并盯 10:30 30m 信号。",
        summary="守红利低波，盯 30m 冰点信号",
        decision="advisory",
        risks=["科创50承压"],
        actions=["不左侧抄底"],
    )
    assert rc_resp == 0
    assert not (bus_dir / "pending" / f"{jid}.json").exists()
    assert (bus_dir / "done" / f"{jid}.json").is_file()

    # 5. 查询任务结果返回 completed
    job_info = ai_bus.get_ai_job(jid)
    assert job_info["ok"] is True
    assert job_info["status"] == "completed"
    assert job_info["result"]["summary"] == "守红利低波，盯 30m 冰点信号"
    assert job_info["result"]["risk_flags"] == ["科创50承压"]
    assert job_info["result"]["actions"] == ["不左侧抄底"]
