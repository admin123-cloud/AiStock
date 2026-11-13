# -*- coding: utf-8 -*-
"""utils/market_sources/tencent_kline.py 的测试（mock 响应，不发真实请求）。

夹具：tests/fixtures/market_sources/tencent_mkline_m5_sz300054.json ——
2026-11-13 盘后实测抓取的 kline/mkline 原始载荷（proxy.finance.qq.com 通道，
320 根 m5，2026-11-05 10:55 ~ 2026-11-13 15:00，周期结束边界）。
单位口径：volume 原始「手」，科创板 688/689 原始「股」÷100；无成交额字段。
"""
import json
from pathlib import Path

import pytest

from utils.market_sources import tencent_kline as tk

FIXTURES = Path(__file__).parent / "fixtures" / "market_sources"


def _load_payload(filename):
    with open(FIXTURES / filename, encoding="utf-8") as handle:
        return json.load(handle)


class _FakeResponse:
    def __init__(self, text):
        self.text = text


def _payload_response(payload):
    return _FakeResponse(json.dumps(payload))


@pytest.fixture(autouse=True)
def _clear_cooldown():
    tk.host_down_until.clear()
    yield
    tk.host_down_until.clear()


# ── parse_mkline_payload：离线解析与单位换算 ──────────────────────────

def test_parse_real_fixture_rows_and_known_values():
    rows = tk.parse_mkline_payload(_load_payload("tencent_mkline_m5_sz300054.json"),
                                   "sz300054", "m5")
    assert len(rows) == 320
    # 实测锚（2026-11-13 收盘 bar）：15:00 边界，真实 OHLC
    last = rows[-1]
    assert last["datetime"] == "2026-11-13 15:00"
    assert last["open"] == pytest.approx(58.85)
    assert last["high"] == pytest.approx(58.93)
    assert last["low"] == pytest.approx(58.83)
    assert last["close"] == pytest.approx(58.93)
    assert last["volume"] == pytest.approx(2636.0)
    assert last["volume_raw_unit"] == "lots"
    assert last["volume_divisor"] == 1
    # 升序 + 无重复
    stamps = [r["datetime"] for r in rows]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)


def test_parse_amount_explicitly_none_with_marker():
    rows = tk.parse_mkline_payload(_load_payload("tencent_mkline_m5_sz300054.json"),
                                   "sz300054", "m5")
    for row in rows:
        assert row["amount"] is None                    # 腾讯分钟线无成交额字段
        assert row["amount_available"] is False
        assert row["amount_unit"] == "CNY"
        assert row["amount_missing_reason"]


def test_parse_turnover_bp_divided_by_100():
    rows = tk.parse_mkline_payload(_load_payload("tencent_mkline_m5_sz300054.json"),
                                   "sz300054", "m5")
    last = rows[-1]
    # 基点 3.54 → 0.0354%（单根 5min bar 的换手率）
    assert last["turnover_rate_pct"] == pytest.approx(0.0354, abs=1e-6)


def _mk_payload(symbol="sz300054", period="m5", items=None):
    return {"code": 0, "msg": "", "data": {symbol: {period: items if items is not None else []}}}


def test_parse_synthetic_basic_row_and_sci_board_shares_divided_by_100():
    items = [["202611130935", "10.00", "10.50", "10.60", "9.90", "12300"]]
    rows = tk.parse_mkline_payload(_mk_payload("sz300054", items=items), "sz300054", "m5")
    row = rows[0]
    assert row["datetime"] == "2026-11-13 09:35"
    assert row["volume"] == pytest.approx(12300.0)
    assert row["volume_raw_unit"] == "lots"
    # 科创板 688：原始股 ÷100 → 手
    rows688 = tk.parse_mkline_payload(_mk_payload("sh688981", items=items), "sh688981", "m5")
    assert rows688[0]["volume"] == pytest.approx(123.0)
    assert rows688[0]["volume_raw_unit"] == "shares"
    assert rows688[0]["volume_divisor"] == 100
    # 689 同口径
    rows689 = tk.parse_mkline_payload(_mk_payload("sh689009", items=items), "sh689009", "m5")
    assert rows689[0]["volume"] == pytest.approx(123.0)


def test_parse_param_error_raises_value_error():
    with pytest.raises(ValueError, match="param"):
        tk.parse_mkline_payload({"code": 0, "msg": "param error", "data": {}}, "sz300054", "m5")


def test_parse_rejects_duplicate_timestamps_fail_closed():
    items = [["202611130935", "1", "1", "1", "1", "100"],
             ["202611130935", "1", "1", "1", "1", "100"]]
    with pytest.raises(RuntimeError, match="两次"):
        tk.parse_mkline_payload(_mk_payload(items=items), "sz300054", "m5")


def test_parse_rejects_missing_symbol_node_fail_closed():
    payload = _mk_payload(items=[["202611130935", "1", "1", "1", "1", "100"]])
    with pytest.raises(RuntimeError, match="没有 sz000001"):
        tk.parse_mkline_payload(payload, "sz000001", "m5")


def test_parse_rejects_missing_period_list_fail_closed():
    payload = {"code": 0, "msg": "", "data": {"sz300054": {"m1": []}}}
    with pytest.raises(RuntimeError, match="没有 m5 列表"):
        tk.parse_mkline_payload(payload, "sz300054", "m5")


def test_parse_rejects_malformed_rows_fail_closed():
    for bad in (["202611130935", "1", "1"],          # 行变短
                "202611130935,1,1,1,1,100",          # 行不是数组
                ["notadate", "1", "1", "1", "1", "100"],  # 时间戳不可识别
                ["202611130935", "1", "1", "1", "1", ""], # 必填量为空
                ["202611130935", "1", "1", "1", "1", True]):  # 布尔量
        with pytest.raises(RuntimeError):
            tk.parse_mkline_payload(_mk_payload(items=[bad]), "sz300054", "m5")


# ── fetch_minute_kline：入口轮换 / 冷却 / fail-closed ────────────────

def test_fetch_prefers_proxy_channel_first():
    calls = []

    def transport(url, params, timeout):
        calls.append(url)
        return _payload_response(_mk_payload(items=[["202611130935", "1", "1", "1", "1", "100"]]))

    res = tk.fetch_minute_kline("300054", "m5", 10, transport=transport)
    assert calls == [tk.TENCENT_KLINE_HOSTS[0] + tk.MKLINE_PATH]
    assert res["host"].startswith("https://proxy.finance.qq.com")
    assert res["row_count"] == 1
    assert res["units"] == {"volume": "lots", "amount": None}
    assert res["lineage"]["source"] == "tencent_mkline"
    assert res["lineage"]["hosts_tried"] == [tk.TENCENT_KLINE_HOSTS[0]]
    assert res["rows"][0]["code"] == "sz300054"


def test_fetch_rotates_to_next_host_when_first_fails():
    calls = []

    def transport(url, params, timeout):
        calls.append(url)
        if "proxy.finance.qq.com" in url:
            raise RuntimeError("connection reset (WAF)")
        return _payload_response(_mk_payload(items=[["202611130935", "1", "1", "1", "1", "100"]]))

    res = tk.fetch_minute_kline("300054", "m5", 10, transport=transport)
    assert len(calls) == 2
    assert calls[1] == tk.TENCENT_KLINE_HOSTS[1] + tk.MKLINE_PATH
    assert res["host"] == tk.TENCENT_KLINE_HOSTS[1]
    # 失败入口进入冷却
    assert tk.host_down_until[tk.TENCENT_KLINE_HOSTS[0]] > 0


def test_fetch_cooldown_skips_failed_host_on_next_call():
    def transport(url, params, timeout):
        if "proxy.finance.qq.com" in url:
            raise RuntimeError("down")
        return _payload_response(_mk_payload(items=[["202611130935", "1", "1", "1", "1", "100"]]))

    res = tk.fetch_minute_kline("300054", "m5", 10, transport=transport)
    assert res["host"] == tk.TENCENT_KLINE_HOSTS[1]
    assert tk.host_down_until.get(tk.TENCENT_KLINE_HOSTS[0], 0) > 0   # 失败入口进入冷却

    calls = []

    def transport2(url, params, timeout):
        calls.append(url)
        return _payload_response(_mk_payload(items=[["202611130935", "1", "1", "1", "1", "100"]]))

    tk.fetch_minute_kline("300054", "m5", 10, transport=transport2)
    assert calls and "proxy.finance.qq.com" not in calls[0]           # 冷却入口不被请求


def test_fetch_treats_empty_json_and_non_dict_as_host_failure():
    def transport(url, params, timeout):
        if "proxy.finance.qq.com" in url:
            return _FakeResponse("")                    # 空 JSON（限流典型表现）
        if "web.ifzq" in url:
            return _FakeResponse("[1,2,3]")             # 顶层非对象
        raise RuntimeError("conn refused")
    with pytest.raises(RuntimeError, match="均不可用"):
        tk.fetch_minute_kline("300054", "m5", 10, transport=transport)
    assert set(tk.host_down_until) == set(tk.TENCENT_KLINE_HOSTS)


def test_fetch_raises_when_all_hosts_fail():
    def transport(url, params, timeout):
        raise RuntimeError("timeout")
    with pytest.raises(RuntimeError, match="均不可用"):
        tk.fetch_minute_kline("300054", "m5", 10, transport=transport)


def test_fetch_param_error_raises_value_error_not_rotated():
    calls = []

    def transport(url, params, timeout):
        calls.append(url)
        return _payload_response({"code": 0, "msg": "param error", "data": {}})

    with pytest.raises(ValueError, match="param"):
        tk.fetch_minute_kline("300054", "m5", 10, transport=transport)
    assert len(calls) == 1                              # 参数错不轮换


def test_fetch_rejects_beijing_exchange_before_any_request():
    def transport(url, params, timeout):                # noqa: ARG001 - 不应被调用
        raise AssertionError("北交所应在发请求前拒绝")
    with pytest.raises(ValueError, match="北交所"):
        tk.fetch_minute_kline("920982", "m5", 10, transport=transport)
    with pytest.raises(ValueError, match="北交所"):
        tk.fetch_minute_kline("bj920982", "m5", 10, transport=transport)


def test_fetch_validates_period_and_count():
    def transport(url, params, timeout):                # noqa: ARG001
        return _payload_response(_mk_payload(items=[["202611130935", "1", "1", "1", "1", "100"]]))

    with pytest.raises(ValueError, match="period"):
        tk.fetch_minute_kline("300054", "day", 10, transport=transport)
    with pytest.raises(ValueError, match="count"):
        tk.fetch_minute_kline("300054", "m5", 0, transport=transport)
    with pytest.raises(ValueError, match="count"):
        tk.fetch_minute_kline("300054", "m5", 321, transport=transport)


def test_fetch_normalizes_symbol_and_passes_param():
    seen = {}

    def transport(url, params, timeout):
        seen.update(params)
        return _payload_response(_mk_payload("sz300054", "m15",
                                             [["202611130945", "1", "1", "1", "1", "100"]]))

    tk.fetch_minute_kline("300054.SZ", "m15", 20, transport=transport)
    assert seen["param"] == "sz300054,m15,,20"


def test_fetch_empty_data_rows_fail_closed_no_fake_data():
    def transport(url, params, timeout):
        return _payload_response({"code": 0, "msg": "", "data": {"sz300054": {"m5": []}}})

    with pytest.raises(RuntimeError, match="均不可用"):
        tk.fetch_minute_kline("300054", "m5", 10, transport=transport)
