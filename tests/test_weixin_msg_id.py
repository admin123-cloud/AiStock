"""微信短消息编号（[idxxxx]）自动标记与引用反查单测。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_bridges import weixin_ilink_bridge as bridge_mod  # noqa: E402


def _make_bridge(tmp_path: Path) -> bridge_mod.ILinkBridge:
    (tmp_path / "acct_test.json").write_text(
        json.dumps({"token": "tok_test", "user_id": "owner_1", "base_url": "https://example.test"}),
        encoding="utf-8",
    )
    return bridge_mod.ILinkBridge(tmp_path, "http://127.0.0.1:3000/api")


def test_parse_ref_msg_id_variants():
    assert bridge_mod.parse_ref_msg_id("id0001:选2") == ("id0001", "选2")
    assert bridge_mod.parse_ref_msg_id("ID12：帮我查原因") == ("id0012", "帮我查原因")
    assert bridge_mod.parse_ref_msg_id("[id0005] 同意提交") == ("id0005", "同意提交")
    assert bridge_mod.parse_ref_msg_id("id7 确认执行") == ("id0007", "确认执行")
    assert bridge_mod.parse_ref_msg_id("普通提问内容") == (None, "普通提问内容")


def test_send_text_assigns_monotonic_ids_and_records_index(tmp_path, monkeypatch):
    sent_texts: list[str] = []

    def fake_post(base_url, endpoint, token, payload, timeout=20.0):
        sent_texts.append(payload["msg"]["item_list"][0]["text_item"]["text"])
        return {"ret": 0, "errcode": 0, "message_id": len(sent_texts)}

    monkeypatch.setattr(bridge_mod, "_ilink_post", fake_post)
    b = _make_bridge(tmp_path)

    r1 = b.send_text("第一条告警", source="tdx_postclose_client")
    r2 = b.send_text("第二条回复", source="session_reply")
    r3 = b.send_text("收到回执", source="inbox_ack", assign_id=False)

    assert r1["ok"] and r1["short_id"] == "id0001"
    assert r2["ok"] and r2["short_id"] == "id0002"
    assert r3["ok"] and "short_id" not in r3
    assert sent_texts[0].startswith("[id0001]\n第一条告警")
    assert sent_texts[1].startswith("[id0002]\n第二条回复")
    assert sent_texts[2] == "收到回执"

    # 重启 bridge 后编号必须继续递增且不影响账号凭证加载
    b2 = _make_bridge(tmp_path)
    assert b2.account_id == "acct_test"
    rec = b2.lookup_outbound_message("id0001")
    assert rec is not None and rec["text"] == "第一条告警"
    r4 = b2.send_text("重启后第一条", source="http_send")
    assert r4["short_id"] == "id0003"


def test_enqueue_question_attaches_referenced_outbound_message(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_mod, "_ilink_post", lambda *a, **kw: {"ret": 0, "errcode": 0})
    b = _make_bridge(tmp_path)
    res = b.send_text("通达信下载失败，回复1补跑，回复2加固", source="session_reply")
    sid = res["short_id"]

    ref_id, clean_text = bridge_mod.parse_ref_msg_id(f"{sid}:选2")
    item = b._enqueue_session_question("owner_1", f"{sid}:选2", ref_msg_id=ref_id, clean_text=clean_text)

    assert item["ref_msg_id"] == sid
    assert item["clean_text"] == "选2"
    assert item["ref_found"] is True
    assert "通达信下载失败" in item["referenced_message"]["text"]
