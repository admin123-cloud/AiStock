"""单元测试：通达信标准板块解析、NEEQ 过滤与 .day 历史日线构建。"""

from __future__ import annotations

import struct
from datetime import date
from pathlib import Path

from pipelines.jobs.tdx_sector_sync import (
    _normalize_stock_code,
    parse_sector_day_file,
    parse_tdx_sectors,
)


def test_normalize_stock_code_filters_neeq_and_non_a_shares():
    assert _normalize_stock_code("0", "000001") == "000001.SZ"
    assert _normalize_stock_code("1", "600519") == "600519.SH"
    assert _normalize_stock_code("2", "920489") == "920489.BJ"
    # NEEQ 老三板 / 非 920 北交所代码必须拦截
    assert _normalize_stock_code("2", "830799") is None
    assert _normalize_stock_code("2", "430047") is None
    assert _normalize_stock_code("0", "400001") is None
    # 非股票代码（指数/债券等）拦截
    assert _normalize_stock_code("1", "880301") is None


def test_parse_tdx_sectors_and_day_file(tmp_path: Path):
    hq = tmp_path / "T0002" / "hq_cache"
    hq.mkdir(parents=True)

    (hq / "tdxzs3.cfg").write_text(
        "\n".join([
            "煤炭|880301|2|1|1|T0101",
            "煤炭开采|880302|2|1|1|T010101",
            "大基金持股|880551|4|1|1|大基金持股",
            "高分红股|880526|5|1|1|高分红股",
            "黑龙江|880201|3|1|1|1",
            "研究煤炭|881001|12|1|1|X10",  # type=12 不纳入标准行业体系
        ]),
        encoding="gbk",
    )
    (hq / "tdxhy.cfg").write_text(
        "\n".join([
            "1|601088|T010101|||X100101",
            "0|000983|T010101|||X100102",
            "2|920489|T0101|||X10",
            "2|830799|T010101|||X100101",  # NEEQ 应被过滤
        ]),
        encoding="gbk",
    )
    (hq / "infoharbor_block.dat").write_text(
        "\n".join([
            "#GN_大基金持股,2,880551,20240613,20261119,,",
            "1#688981,0#002371",
            "#FG_高分红股,1,880526,20200101,20261119,,",
            "1#601088",
        ]),
        encoding="gbk",
    )

    sectors, memberships = parse_tdx_sectors(tmp_path)
    by_code = {s["code"]: s for s in sectors}
    assert set(by_code.keys()) == {"880301.SH", "880302.SH", "880551.SH", "880526.SH", "880201.SH"}
    assert by_code["880301.SH"]["level"] == 1
    assert by_code["880301.SH"]["stock_count"] == 3  # 含下属二级行业与自身 3 只（排除 830799）
    assert by_code["880302.SH"]["level"] == 2
    assert by_code["880302.SH"]["parent_code"] == "880301.SH"
    assert by_code["880302.SH"]["stock_count"] == 2
    assert by_code["880551.SH"]["type"] == "concept"
    assert by_code["880551.SH"]["stock_count"] == 2
    assert ("880551.SH", "688981.SH") in memberships

    # 构造 2 根 32 字节通达信日线 .day 文件
    day_file = tmp_path / "sh880301.day"
    bar1 = struct.pack("<IIIIIfII", 20261118, 100000, 102000, 99000, 100000, 1.5e9, 120000, 0)
    bar2 = struct.pack("<IIIIIfII", 20261119, 101000, 105000, 100500, 105000, 2.0e9, 180000, 0)
    day_file.write_bytes(bar1 + bar2)

    rows = parse_sector_day_file(day_file, "880301.SH", 3)
    assert len(rows) == 2
    assert rows[0][1] == date(2026, 11, 18)
    assert rows[0][6] is None
    assert rows[1][1] == date(2026, 11, 19)
    assert rows[1][5] == 1050.0
    assert rows[1][6] == 5.0
