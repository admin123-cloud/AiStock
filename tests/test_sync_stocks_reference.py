# -*- coding: utf-8 -*-
"""证券主数据 stock.stocks 增量同步纯函数与安全门禁单测。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import sync_stocks_reference as ssr  # noqa: E402


def test_normalize_a_share_code_filters_neeq_and_maps_markets():
    assert ssr.normalize_a_share_code("001381") == "001381.SZ"
    assert ssr.normalize_a_share_code("301718") == "301718.SZ"
    assert ssr.normalize_a_share_code("600519") == "600519.SH"
    assert ssr.normalize_a_share_code("688827") == "688827.SH"
    assert ssr.normalize_a_share_code("920067") == "920067.BJ"
    # 严禁三板/旧北交所代码进入 stock.stocks
    assert ssr.normalize_a_share_code("400001") is None
    assert ssr.normalize_a_share_code("430047") is None
    assert ssr.normalize_a_share_code("830799") is None
    assert ssr.normalize_a_share_code("870204") is None
    assert ssr.normalize_a_share_code("510880") is None


def test_plan_rejects_partial_catalog_fail_closed():
    with pytest.raises(RuntimeError, match="fail-closed"):
        ssr.plan_stock_reference_updates(
            live_catalog={"000001.SZ": "平安银行"},
            existing_stocks={},
            kline_date_bounds={},
            delist_cutoff_date="2026-11-19",
            now_str="2026-11-25 19:10:00",
            min_live_stocks=5000,
        )


def test_plan_covers_add_promote_rename_st_delist_and_preserves_fields():
    existing = {
        "000002.SZ": {
            "code": "000002.SZ",
            "name": "万 科Ａ",
            "market": "SZ",
            "type": "stock",
            "listing_status": "active",
            "industry": "房地产",
            "region": "广东",
            "list_date": "1991-01-29",
            "delist_date": None,
            "quit": 0,
            "st": 0,
            "industry_code": "880482",
            "self_selected": 1,
            "holding": 0,
            "float_share": 100.0,
            "total_share": 110.0,
            "created_at": "2020-01-01 00:00:00",
            "id": 0,
        },
        "000892.SZ": {
            "code": "000892.SZ",
            "name": "欢瑞世纪",
            "market": "SZ",
            "type": "stock",
            "listing_status": "active",
            "industry": "影视",
            "region": "重庆",
            "list_date": "1999-01-15",
            "delist_date": None,
            "quit": 0,
            "st": 0,
            "industry_code": "880418",
            "self_selected": 0,
            "holding": 1,
            "float_share": 50.0,
            "total_share": 90.0,
            "created_at": "2020-01-01 00:00:00",
            "id": 0,
        },
        "301569.SZ": {
            "code": "301569.SZ",
            "name": "联亚药业",
            "market": "SZ",
            "type": "stock",
            "listing_status": "pending_listing",
            "industry": "",
            "region": None,
            "list_date": None,
            "delist_date": None,
            "quit": 0,
            "st": 0,
            "industry_code": None,
            "self_selected": 0,
            "holding": 0,
            "float_share": None,
            "total_share": None,
            "created_at": "2026-10-07 05:01:17",
            "id": 0,
        },
        "601059.SH": {
            "code": "601059.SH",
            "name": "信达证券",
            "market": "SH",
            "type": "stock",
            "listing_status": "active",
            "industry": "证券",
            "region": "北京",
            "list_date": "2023-02-01",
            "delist_date": None,
            "quit": 0,
            "st": 0,
            "industry_code": "880472",
            "self_selected": 0,
            "holding": 0,
            "float_share": 80.0,
            "total_share": 324.0,
            "created_at": "2023-02-01 00:00:00",
            "id": 0,
        },
        "002999.SZ": {
            "code": "002999.SZ",
            "name": "临时停牌测试",
            "market": "SZ",
            "type": "stock",
            "listing_status": "active",
            "industry": "电子",
            "region": "江苏",
            "list_date": "2020-05-01",
            "delist_date": None,
            "quit": 0,
            "st": 0,
            "industry_code": "880400",
            "self_selected": 0,
            "holding": 0,
            "float_share": 10.0,
            "total_share": 20.0,
            "created_at": "2020-05-01 00:00:00",
            "id": 0,
        },
    }
    live = {
        "000002.SZ": "万  科Ａ",      # 纯空格变化 -> 应忽略
        "000892.SZ": "ST欢瑞",        # 戴帽 -> 更新 name 与 st=1，继承 industry/holding
        "301569.SZ": "联亚药业",      # pending -> active，补 list_date
        "001381.SZ": "皇冠新材",      # 新股 -> 新增
    }
    bounds = {
        "001381.SZ": ("2026-10-28", "2026-11-25"),
        "301569.SZ": ("2026-10-15", "2026-11-25"),
        "601059.SH": ("2023-02-01", "2026-09-14"),  # 远早于截止日 -> 标 quit=1
        "002999.SZ": ("2020-05-01", "2026-11-24"),  # 近 5 日仍有成交 -> 绝不误标退市
    }

    upserts, summary = ssr.plan_stock_reference_updates(
        live_catalog=live,
        existing_stocks=existing,
        kline_date_bounds=bounds,
        delist_cutoff_date="2026-11-19",
        now_str="2026-11-25 19:10:00",
        min_live_stocks=4,
    )
    by_code = {r["code"]: r for r in upserts}

    assert "000002.SZ" not in by_code
    assert "002999.SZ" not in by_code
    assert summary["skipped_suspended_count"] == 1

    assert by_code["001381.SZ"]["list_date"] == "2026-10-28"
    assert by_code["001381.SZ"]["listing_status"] == "active"

    assert by_code["301569.SZ"]["listing_status"] == "active"
    assert by_code["301569.SZ"]["list_date"] == "2026-10-15"

    assert by_code["000892.SZ"]["name"] == "ST欢瑞"
    assert by_code["000892.SZ"]["st"] == 1
    assert by_code["000892.SZ"]["industry"] == "影视"
    assert by_code["000892.SZ"]["holding"] == 1

    assert by_code["601059.SH"]["quit"] == 1
    assert by_code["601059.SH"]["delist_date"] == "2026-09-14"
