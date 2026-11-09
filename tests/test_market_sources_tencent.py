# -*- coding: utf-8 -*-
"""utils/market_sources/tencent_daily.py 的测试（严格 TDD，先于实现编写）。

夹具：tests/fixtures/market_sources/tencent_*.json —— 由 PoC-A 报告
（a_stock_data_tencent_kline_poc_report.json 的 samples_raw）的实测样本响应构造的
fqkline/get 原始载荷（data.{symbol}.day / .qfqday，元素 [日期, 开, 收, 高, 低, 量]）。
单位口径（PoC-A 实测）：腾讯日线量对 688/689 科创板是「股」，其余是「手」；无成交额字段。
"""
import json
from pathlib import Path

import pytest

from utils.market_sources import tencent_daily as tc

FIXTURES = Path(__file__).parent / "fixtures" / "market_sources"


def _load_payload(filename):
    with open(FIXTURES / filename, encoding="utf-8") as handle:
        return json.load(handle)


# ── parse_daily_payload：离线解析与单位换算 ──────────────────────────
def test_parse_sz_main_board_volume_already_lots_with_poc_known_value():
    rows = tc.parse_daily_payload(_load_payload("tencent_day_sz000001.json"), "sz000001")

    assert len(rows) == 9
    by_date = {r["date"]: r for r in rows}
    # PoC-A：000001.SZ 2026-10-22 腾讯手值 1,297,649 = 正式表手值原样，不换算
    row = by_date["2026-10-22"]
    assert row["open"] == pytest.approx(11.41)
    assert row["close"] == pytest.approx(11.30)
    assert row["high"] == pytest.approx(11.42)
    assert row["low"] == pytest.approx(11.26)
    assert row["volume"] == pytest.approx(1297649.0)
    assert row["volume_raw"] == pytest.approx(1297649.0)
    assert row["volume_raw_unit"] == "lots"
    assert row["volume_unit"] == "lots"
    assert row["volume_divisor"] == 1
    row30 = by_date["2026-10-30"]
    assert row30["volume"] == pytest.approx(1864245.0)


def test_parse_sci_tech_board_shares_divided_by_100_with_poc_known_value():
    rows = tc.parse_daily_payload(_load_payload("tencent_day_sh688981.json"), "sh688981")

    by_date = {r["date"]: r for r in rows}
    # PoC-A：688981.SH 2026-10-22 腾讯股值 35,790,512 ÷100 = 357,905.12 手（表值 357,905）
    row = by_date["2026-10-22"]
    assert row["volume_raw"] == pytest.approx(35790512.0)
    assert row["volume_raw_unit"] == "shares"
    assert row["volume"] == pytest.approx(357905.12)
    assert round(row["volume"]) == 357905
    assert row["volume_divisor"] == 100
    assert row["close"] == pytest.approx(100.09)
    row30 = by_date["2026-10-30"]
    assert row30["volume"] == pytest.approx(299989.89)
    assert row30["close"] == pytest.approx(96.79)
    # 尾行带额外字段（真实响应常见第 7 列换手信息）不影响解析
    assert len(rows) == 9


def test_parse_689_prefix_also_treated_as_shares():
    payload = {"code": 0, "msg": "",
               "data": {"sh689009": {"day": [["2026-10-22", "10.00", "10.50", "10.60", "9.90", "123456"]]}}}
    rows = tc.parse_daily_payload(payload, "sh689009")
    assert rows[0]["volume_raw_unit"] == "shares"
    assert rows[0]["volume"] == pytest.approx(1234.56)


def test_parse_amount_explicitly_none_with_marker():
    rows = tc.parse_daily_payload(_load_payload("tencent_day_sz000001.json"), "sz000001")
    for row in rows:
        assert row["amount"] is None                    # 腾讯 K 线无成交额字段
        assert row["amount_available"] is False
        assert row["amount_unit"] == "CNY"
        assert row["amount_missing_reason"]


def test_parse_reads_qfqday_key_when_present():
    rows = tc.parse_daily_payload(_load_payload("tencent_qfqday_sz000001.json"), "sz000001",
                                  adjust="qfq")
    assert len(rows) == 9
    assert rows[-1]["date"] == "2026-10-30"
    assert rows[-1]["close"] == pytest.approx(11.64)
    assert all(r["adjust"] == "qfq" for r in rows)


def test_parse_rows_sorted_by_date_and_marked_none_adjust():
    rows = tc.parse_daily_payload(_load_payload("tencent_day_sh600000.json"), "sh600000")
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)
    assert all(r["adjust"] == "none" for r in rows)
    # PoC-A：600000.SH 2026-10-30 手值 648,865（腾讯原始即手，不换算）
    assert rows[-1]["volume"] == pytest.approx(648865.0)
    assert rows[-1]["close"] == pytest.approx(10.02)


def test_parse_param_error_payload_raises_value_error():
    with pytest.raises(ValueError, match="param"):
        tc.parse_daily_payload(_load_payload("tencent_param_error.json"), "sz000001")


def test_parse_rejects_duplicate_dates_fail_closed():
    payload = {"code": 0, "msg": "",
               "data": {"sz000001": {"day": [
                   ["2026-10-22", "11.41", "11.30", "11.42", "11.26", "1297649"],
                   ["2026-10-22", "11.40", "11.31", "11.42", "11.26", "1200000"]]}}}
    with pytest.raises(RuntimeError, match="两次|重复"):
        tc.parse_daily_payload(payload, "sz000001")


def test_parse_rejects_missing_symbol_node_fail_closed():
    payload = {"code": 0, "msg": "", "data": {"sh600000": {"day": []}}}
    with pytest.raises(RuntimeError, match="sz000001"):
        tc.parse_daily_payload(payload, "sz000001")


def test_parse_rejects_missing_kline_keys_fail_closed():
    payload = {"code": 0, "msg": "", "data": {"sz000001": {"qt": {"x": 1}}}}
    with pytest.raises(RuntimeError, match="day|qfqday|格式"):
        tc.parse_daily_payload(payload, "sz000001")


def test_parse_rejects_empty_adjusted_key_with_raw_data():
    payload = {"code": 0, "msg": "", "data": {"sz000001": {
        "qfqday": [],
        "day": [["2026-10-22", "11.41", "11.30", "11.42", "11.26", "1297649"]]}}}
    with pytest.raises(RuntimeError, match="qfq|原始价"):
        tc.parse_daily_payload(payload, "sz000001", adjust="qfq")


def test_parse_rejects_malformed_rows_fail_closed():
    base = {"code": 0, "msg": "", "data": {"sz000001": {"day": [
        ["2026-10-22", "11.41", "11.30", "11.42", "11.26", "abc"]]}}}
    with pytest.raises(RuntimeError, match="数值|volume"):
        tc.parse_daily_payload(base, "sz000001")
    short = {"code": 0, "msg": "", "data": {"sz000001": {"day": [["2026-10-22", "11.41", "11.3"]]}}}
    with pytest.raises(RuntimeError, match="格式"):
        tc.parse_daily_payload(short, "sz000001")
    not_list = {"code": 0, "msg": "", "data": {"sz000001": {"day": {"oops": 1}}}}
    with pytest.raises(RuntimeError, match="格式"):
        tc.parse_daily_payload(not_list, "sz000001")


def test_parse_rejects_non_dict_payload_fail_closed():
    with pytest.raises(RuntimeError, match="JSON|对象"):
        tc.parse_daily_payload([1, 2, 3], "sz000001")


# ── fetch_daily_kline：三入口轮换 / 冷却 / 北交所拒绝（全部离线） ──
class _FakeTransport:
    """按 host 关键字决定行为：脚本里 host 前缀 → 响应文本或异常；记录调用顺序。"""

    def __init__(self, script):
        self.script, self.calls = dict(script), []

    def __call__(self, url, params, timeout):
        self.calls.append(url.split("/")[2] if "://" in url else url)
        for key, step in self.script.items():
            if key in url:
                if isinstance(step, Exception):
                    raise step
                return _FakeHTTPResponse(step)


class _FakeHTTPResponse:
    def __init__(self, text):
        self.text = text


@pytest.fixture(autouse=True)
def _reset_cooldown():
    tc.host_down_until.clear()
    yield
    tc.host_down_until.clear()


def _ok_payload():
    return json.dumps({"code": 0, "msg": "",
                       "data": {"sz000001": {"day": [
                           ["2026-10-22", "11.41", "11.30", "11.42", "11.26", "1297649"],
                           ["2026-10-23", "11.27", "11.20", "11.35", "11.17", "1448806"]]}}})


def test_fetch_rotates_to_next_host_when_first_fails():
    transport = _FakeTransport({
        "web.ifzq.gtimg.cn": OSError("rate limited"),
        "proxy.finance.qq.com": _ok_payload(),
        "ifzq.gtimg.cn": _ok_payload(),
    })
    result = tc.fetch_daily_kline("sz000001", count=10, transport=transport)
    assert transport.calls == ["web.ifzq.gtimg.cn", "proxy.finance.qq.com"]
    assert result["host"] == "https://proxy.finance.qq.com/ifzqgtimg"
    assert len(result["rows"]) == 2
    assert result["rows"][0]["volume"] == pytest.approx(1297649.0)   # 深市原始即手
    assert result["row_count"] == 2
    assert result["units"]["volume"] == "lots"
    assert result["lineage"]["source"] == "tencent_kline"


def test_fetch_cooldown_skips_failed_host_on_next_call():
    transport = _FakeTransport({
        "web.ifzq.gtimg.cn": OSError("rate limited"),
        "proxy.finance.qq.com": _ok_payload(),
        "ifzq.gtimg.cn": _ok_payload(),
    })
    clock = {"now": 1_800_000_000.0}
    tc.fetch_daily_kline("sz000001", count=10, transport=transport, now=lambda: clock["now"])
    transport.calls.clear()
    tc.fetch_daily_kline("sz000001", count=10, transport=transport, now=lambda: clock["now"])
    # 冷却期内第一个入口被跳过，直接从第二个入口开始
    assert transport.calls[0] == "proxy.finance.qq.com"
    # 冷却过期（+121s）后第一个入口重新可用
    transport.calls.clear()
    clock["now"] += 121
    tc.fetch_daily_kline("sz000001", count=10, transport=transport, now=lambda: clock["now"])
    assert transport.calls[0] == "web.ifzq.gtimg.cn"


def test_fetch_raises_when_all_hosts_fail():
    transport = _FakeTransport({
        "web.ifzq.gtimg.cn": OSError("down"),
        "proxy.finance.qq.com": OSError("down"),
        "ifzq.gtimg.cn": OSError("down"),
    })
    with pytest.raises(RuntimeError, match="三个入口|均不可用"):
        tc.fetch_daily_kline("sz000001", count=10, transport=transport)
    assert len(transport.calls) == 3


def test_fetch_treats_empty_json_as_host_failure_and_rotates():
    # SKILL.md #52：被限流的入口返回空 JSON —— 视为该入口失败，换下一个
    transport = _FakeTransport({
        "web.ifzq.gtimg.cn": "",
        "proxy.finance.qq.com": _ok_payload(),
        "ifzq.gtimg.cn": _ok_payload(),
    })
    result = tc.fetch_daily_kline("sz000001", count=10, transport=transport)
    assert result["host"] == "https://proxy.finance.qq.com/ifzqgtimg"


def test_fetch_param_error_raises_value_error_not_rotated():
    transport = _FakeTransport({
        "web.ifzq.gtimg.cn": json.dumps({"code": 0, "msg": "param error", "data": {}}),
    })
    with pytest.raises(ValueError, match="param"):
        tc.fetch_daily_kline("sz000001", count=10, transport=transport)
    assert len(transport.calls) == 1


def test_fetch_rejects_beijing_exchange_before_any_request():
    transport = _FakeTransport({})
    with pytest.raises(ValueError, match="北交所"):
        tc.fetch_daily_kline("bj920982", transport=transport)
    with pytest.raises(ValueError, match="北交所"):
        tc.fetch_daily_kline("920982", transport=transport)
    assert transport.calls == []


def test_normalize_symbol_resolves_market_ambiguity():
    assert tc.normalize_symbol("sz000001") == "sz000001"
    assert tc.normalize_symbol("000001") == "sz000001"        # 裸码按号段：000 → 深
    assert tc.normalize_symbol("688981") == "sh688981"
    assert tc.normalize_symbol("688981.SH") == "sh688981"
    assert tc.normalize_symbol("600000.XSHG".lower()) == "sh600000"
    with pytest.raises(ValueError):
        tc.normalize_symbol("XX12345")
