# -*- coding: utf-8 -*-
"""utils/market_sources/tdx_official_package.py 的测试（严格 TDD，先于实现编写）。

夹具来源：tests/fixtures/market_sources/（从 PoC-B 归档 CSV 抽样 + 已知值行），
已知值取自 F:/AiStock/data/data/runtime/daily_kline_coverage/tdx_official_package_poc_20261101/
下的 pocb_report_final.json（通达信盘后包 PoC）与 a_stock_data_tencent_kline_poc_report.json（腾讯 PoC）。
"""
import csv
import hashlib
import io
import json
import struct
import zipfile
from pathlib import Path

import pytest

from utils.market_sources import tdx_official_package as tdx

FIXTURES = Path(__file__).parent / "fixtures" / "market_sources"

# ── 测试内构造 .zip（.cod 150B/条 + .md1 512B/块，布局与 PoC 验证实现一致） ──
# 行元组: (code, name, prev_close, open, high, low, close, volume, amount)


def _cod_record(code, seq, name):
    record = bytearray(150)
    record[0:6] = code.encode("ascii")
    struct.pack_into("<H", record, 32, seq)
    encoded = name.encode("gbk")
    record[40:40 + len(encoded)] = encoded
    return bytes(record)


def _md1_block(prev_close, open_, high, low, close, volume, amount):
    block = bytearray(512)
    struct.pack_into("<d", block, 4, prev_close)
    struct.pack_into("<4d", block, 12, open_, high, low, close)
    struct.pack_into("<Q", block, 56, volume)
    struct.pack_into("<d", block, 72, amount)
    return bytes(block)


def _make_zip(ymd, markets):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for market, rows in markets.items():
            cod = b"".join(_cod_record(r[0], i, r[1]) for i, r in enumerate(rows))
            md1 = b"".join(
                _md1_block(*r[2:]) for r in rows)
            archive.writestr(f"{market}{ymd[2:]}.cod", cod)
            archive.writestr(f"{market}{ymd[2:]}.md1", md1)
    return buffer.getvalue()


# PoC 实测已知值（pocb_report_final.json samples + PoC-A 920982 独立解析对拍一致）
ROW_600000 = ("600000", "浦发银行", 9.61, 9.65, 9.66, 9.60, 9.62, 62529869, 602885524.0)
ROW_688981 = ("688981", "中芯国际", 101.98, 101.0, 101.44, 98.4, 100.09, 35790512, 3577411258.0)
ROW_000001 = ("000001", "平安银行", 11.41, 11.41, 11.42, 11.26, 11.30, 129764875, 1468424722.09)
ROW_920982 = ("920982", "锦波生物", 154.25, 154.26, 169.72, 152.22, 163.99, 3289703, 540594982.35)
ROW_920000 = ("920000", "安徽凤凰", 13.19, 13.21, 13.25, 13.18, 13.22, 277319, 3676293.66)

VALID_MARKETS = {"sh": [ROW_600000, ROW_688981], "sz": [ROW_000001], "bj": [ROW_920982, ROW_920000]}


def _valid_zip(ymd="20261022"):
    return _make_zip(ymd, VALID_MARKETS)


# ── parse_zip_bytes：正常解析与已知值 ─────────────────────────────────
def test_parse_zip_bytes_returns_rows_with_poc_known_values():
    rows = tdx.parse_zip_bytes(_valid_zip(), "20261022", min_priced={})

    assert len(rows) == 5
    by_code = {(r["market"], r["code"]): r for r in rows}
    pingan = by_code[("sz", "000001")]
    assert pingan["date"] == "2026-10-22"
    assert pingan["name"] == "平安银行"
    assert pingan["prev_close"] == pytest.approx(11.41)
    assert pingan["open"] == pytest.approx(11.41)
    assert pingan["high"] == pytest.approx(11.42)
    assert pingan["low"] == pytest.approx(11.26)
    assert pingan["close"] == pytest.approx(11.30)
    # PoC-B：000001.SZ 2026-10-22 原始量 129,764,875 股 / 额 1,468,424,722.09 元
    assert pingan["volume"] == 129764875
    assert pingan["amount"] == pytest.approx(1468424722.09)
    smic = by_code[("sh", "688981")]
    assert smic["volume"] == 35790512
    assert smic["amount"] == pytest.approx(3577411258.0)
    jinbo = by_code[("bj", "920982")]
    assert jinbo["volume"] == 3289703
    assert jinbo["amount"] == pytest.approx(540594982.35)
    assert jinbo["close"] == pytest.approx(163.99)


def test_parse_zip_bytes_orders_markets_sh_sz_bj():
    rows = tdx.parse_zip_bytes(_valid_zip(), "20261022", min_priced={})
    assert [r["market"] for r in rows] == ["sh", "sh", "sz", "bj", "bj"]


def test_parse_zip_bytes_skips_zero_close_blocks():
    zero = ("880001", "新标准券", 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.0)
    content = _make_zip("20261022", {"sh": [ROW_600000, zero], "sz": [ROW_000001], "bj": [ROW_920982]})
    rows = tdx.parse_zip_bytes(content, "20261022", min_priced={})
    assert [r["code"] for r in rows] == ["600000", "000001", "920982"]


def test_parse_zip_bytes_accepts_date_and_dash_forms_same_url_basis():
    # 两种日期写法解析同一包内容，行 date 字段一致
    rows = tdx.parse_zip_bytes(_valid_zip(), "2026-10-22".replace("-", ""), min_priced={})
    assert {r["date"] for r in rows} == {"2026-10-22"}


# ── parse_zip_bytes：fail-closed（恶意 / 损坏 / 格式变异一律抛错） ──
def test_parse_zip_bytes_rejects_non_zip_bytes():
    with pytest.raises(RuntimeError, match="zip"):
        tdx.parse_zip_bytes(b"<html>error page</html>", "20261022", min_priced={})


def test_parse_zip_bytes_rejects_corrupt_zip_after_pk_magic():
    with pytest.raises(RuntimeError):
        tdx.parse_zip_bytes(b"PK\x03\x04" + b"\x00" * 200, "20261022", min_priced={})


def test_parse_zip_bytes_rejects_missing_market_files():
    # 只有 sh 文件：20261022 晚于北交所首日，缺 sz/bj 按格式改变报错
    content = _make_zip("20261022", {"sh": [ROW_600000]})
    with pytest.raises(RuntimeError, match="sz"):
        tdx.parse_zip_bytes(content, "20261022", min_priced={})


def test_parse_zip_bytes_rejects_truncated_blocks():
    market = "sh"
    ymd = "20261022"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{market}{ymd[2:]}.cod", _cod_record("600000", 0, "浦发银行") * 2)
        archive.writestr(f"{market}{ymd[2:]}.md1", _md1_block(*ROW_600000[2:]) + b"\x01" * 511)
    with pytest.raises(RuntimeError, match="整块|截断"):
        tdx.parse_zip_bytes(buffer.getvalue(), ymd, min_priced={})


def test_parse_zip_bytes_rejects_cod_md1_count_mismatch():
    ymd = "20261022"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"sh{ymd[2:]}.cod", _cod_record("600000", 0, "浦发银行") * 2)
        archive.writestr(f"sh{ymd[2:]}.md1", _md1_block(*ROW_600000[2:]))
    with pytest.raises(RuntimeError, match="对不上"):
        tdx.parse_zip_bytes(buffer.getvalue(), ymd, min_priced={})


def test_parse_zip_bytes_rejects_out_of_range_seq():
    ymd = "20261022"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"sh{ymd[2:]}.cod", _cod_record("600000", 7, "浦发银行"))
        archive.writestr(f"sh{ymd[2:]}.md1", _md1_block(*ROW_600000[2:]))
    with pytest.raises(RuntimeError, match="越界|seq"):
        tdx.parse_zip_bytes(buffer.getvalue(), ymd, min_priced={})


def test_parse_zip_bytes_rejects_duplicate_code_and_duplicate_seq():
    for bad_second in (("600000", "浦发银行"), ("600519", "贵州茅台")):
        ymd = "20261022"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(f"sh{ymd[2:]}.cod",
                             _cod_record("600000", 0, "浦发银行") + _cod_record(bad_second[0], 0, bad_second[1]))
            archive.writestr(f"sh{ymd[2:]}.md1",
                             _md1_block(*ROW_600000[2:]) + _md1_block(*ROW_600000[2:]))
        with pytest.raises(RuntimeError, match="重复"):
            tdx.parse_zip_bytes(buffer.getvalue(), ymd, min_priced={})


def test_parse_zip_bytes_rejects_non_numeric_code():
    ymd = "20261022"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"sh{ymd[2:]}.cod", _cod_record("12AB34", 0, "坏代码"))
        archive.writestr(f"sh{ymd[2:]}.md1", _md1_block(*ROW_600000[2:]))
    with pytest.raises(RuntimeError, match="6 位数字|代码"):
        tdx.parse_zip_bytes(buffer.getvalue(), ymd, min_priced={})


def test_parse_zip_bytes_rejects_non_finite_price():
    bad = ("600000", "浦发银行", 9.61, 9.65, 9.66, 9.60, float("nan"), 100, 1000.0)
    content = _make_zip("20261022", {"sh": [bad], "sz": [ROW_000001], "bj": [ROW_920982]})
    with pytest.raises(RuntimeError, match="有限|损坏"):
        tdx.parse_zip_bytes(content, "20261022", min_priced={})


def test_parse_zip_bytes_rejects_non_gbk_name():
    ymd = "20261022"
    record = bytearray(150)
    record[0:6] = b"600000"
    struct.pack_into("<H", record, 32, 0)
    record[40:44] = b"\xff\xfe\x81\xff"  # 不是合法 GBK
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"sh{ymd[2:]}.cod", bytes(record))
        archive.writestr(f"sh{ymd[2:]}.md1", _md1_block(*ROW_600000[2:]))
    with pytest.raises(RuntimeError, match="GBK|名称"):
        tdx.parse_zip_bytes(buffer.getvalue(), ymd, min_priced={})


def test_parse_zip_bytes_rejects_empty_name_with_price():
    bad = ("600000", "", 9.61, 9.65, 9.66, 9.60, 9.62, 100, 1000.0)
    content = _make_zip("20261022", {"sh": [bad], "sz": [ROW_000001], "bj": [ROW_920982]})
    with pytest.raises(RuntimeError, match="名称"):
        tdx.parse_zip_bytes(content, "20261022", min_priced={})


def test_parse_zip_bytes_allows_missing_bj_before_20220506():
    content = _make_zip("20211231", {"sh": [ROW_600000], "sz": [ROW_000001]})
    rows = tdx.parse_zip_bytes(content, "20211231", min_priced={})
    assert len(rows) == 2  # 这天之前的包没有北交所文件，不算错


def test_parse_zip_bytes_rejects_missing_bj_on_or_after_20220506():
    content = _make_zip("20220506", {"sh": [ROW_600000], "sz": [ROW_000001]})
    with pytest.raises(RuntimeError, match="bj"):
        tdx.parse_zip_bytes(content, "20220506", min_priced={})


def test_parse_zip_bytes_enforces_min_priced_thresholds():
    # 小样本包按生产默认下限（sh 10000）应报残缺；显式放宽后通过 —— 下限检查可测
    content = _valid_zip()
    with pytest.raises(RuntimeError, match="残缺|下限"):
        tdx.parse_zip_bytes(content, "20261022")  # 默认 min_priced=TDX_MIN_PRICED
    rows = tdx.parse_zip_bytes(content, "20261022", min_priced={"sh": 1, "sz": 1, "bj": 1})
    assert len(rows) == 5


# ── 夹具加载 ─────────────────────────────────────────────────────────
_NUMERIC = ("prev_close", "open", "high", "low", "close", "amount")


def _load_fixture_rows(filename):
    with open(FIXTURES / filename, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        for key in _NUMERIC:
            row[key] = float(row[key])
        row["volume"] = int(row["volume"])
    return rows


# ── filter_stock_universe：股票池过滤 + 分类计数 ────────────────────
def test_filter_keeps_only_stock_prefixes_with_positive_volume_golden_counts():
    rows = _load_fixture_rows("tdx_pkg_rows_20261030_sample.csv")
    kept, report = tdx.filter_stock_universe(rows)

    # 夹具生成脚本（独立分类逻辑）冻结的黄金计数
    assert len(rows) == 621
    assert len(kept) == 505
    assert report["kept_by_market"] == {"sh": 68, "sz": 83, "bj": 354}
    assert report["excluded_by_category"] == {
        "bond": 78, "fund": 22, "index": 8, "tdx_internal_index": 2,
        "b_share": 4, "reverse_repo": 2,
    }
    assert report["stock_zero_volume"] == 0
    assert report["unknown"] == []           # 归档 CSV 无未知前缀


def test_filter_accounting_is_complete_no_row_silently_dropped():
    for filename in ("tdx_pkg_rows_20261030_sample.csv", "tdx_pkg_rows_20261022_known.csv"):
        rows = _load_fixture_rows(filename)
        kept, report = tdx.filter_stock_universe(rows)
        excluded_total = sum(report["excluded_by_category"].values())
        unknown_total = sum(entry["count"] for entry in report["unknown"])
        assert len(kept) + excluded_total + unknown_total + report["stock_zero_volume"] == len(rows)
        assert report["total_rows"] == len(rows)


def test_filter_keeps_known_value_rows_from_poc():
    rows = _load_fixture_rows("tdx_pkg_rows_20261022_known.csv")
    kept, report = tdx.filter_stock_universe(rows)
    codes = {f'{r["code"]}.{r["market"].upper()}' for r in kept}
    assert codes == {"600000.SH", "688981.SH", "000001.SZ", "920982.BJ", "920000.BJ"}
    assert report["kept_by_market"] == {"sh": 2, "sz": 1, "bj": 2}


def test_filter_excludes_index_bond_fund_examples_by_category():
    rows = _load_fixture_rows("tdx_pkg_rows_20261030_sample.csv")
    kept, report = tdx.filter_stock_universe(rows)
    kept_codes = {r["code"] for r in kept}
    assert "999999" not in kept_codes          # sh 指数（上证指数）
    assert "920000" in kept_codes              # bj 股票
    excluded = report["excluded_examples"]
    assert any(example["code"] == "999999" and example["category"] == "index" for example in excluded)
    # 归档样本含 sh 债券/基金/逆回购/自编指数/沪 B 股分类
    assert report["excluded_by_category"]["bond"] == 78
    assert report["excluded_by_category"]["fund"] == 22


def test_filter_drops_zero_volume_stock_and_counts_it():
    rows = _load_fixture_rows("tdx_pkg_rows_20261022_known.csv")
    suspended = dict(rows[0])
    suspended["volume"] = 0                    # 停牌股：有价无量 → 不保留，单独计数
    kept, report = tdx.filter_stock_universe(rows + [suspended])
    assert len(kept) == 5                      # 5 行原有股票全保留，新增的 0 量副本被剔除
    assert report["stock_zero_volume"] == 1


def test_filter_counts_unknown_prefixes_instead_of_dropping_silently():
    rows = _load_fixture_rows("tdx_pkg_rows_20261022_known.csv")
    alien = {"date": "2026-10-22", "market": "sh", "code": "777777", "name": "未知品种",
             "prev_close": 1.0, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
             "volume": 500, "amount": 500.0}
    kept, report = tdx.filter_stock_universe(rows + [alien])
    assert len(kept) == 5                       # 未知前缀绝不混入股票池
    assert report["unknown"] == [
        {"market": "sh", "prefix": "77", "count": 1, "examples": ["777777"]}]
    # 总账仍对得上：5 保留 + 1 未知 = 6
    assert sum(report["excluded_by_category"].values()) == 0
    assert report["total_rows"] == 6


def test_classify_covers_poc_prefix_taxonomy():
    # 沪：60/68 股、000/999 指数、204 逆回购、5x 基金、88 自编、900 B 股、01/02/1x/23/24/27 债
    assert tdx.classify_code("sh", "600000") == "stock"
    assert tdx.classify_code("sh", "688981") == "stock"
    assert tdx.classify_code("sh", "000001") == "index"
    assert tdx.classify_code("sh", "999999") == "index"
    assert tdx.classify_code("sh", "204001") == "reverse_repo"
    assert tdx.classify_code("sh", "510300") == "fund"
    assert tdx.classify_code("sh", "880001") == "tdx_internal_index"
    assert tdx.classify_code("sh", "900901") == "b_share"
    assert tdx.classify_code("sh", "019742") == "bond"
    assert tdx.classify_code("sh", "113050") == "bond"
    assert tdx.classify_code("sh", "240001") == "bond"
    # 深：00/30 股、399 指数、395 逆回购、200 B 股、10~14/19/52/56 债、15/16/18/50 基金
    assert tdx.classify_code("sz", "000001") == "stock"
    assert tdx.classify_code("sz", "301569") == "stock"
    assert tdx.classify_code("sz", "399001") == "index"
    assert tdx.classify_code("sz", "395001") == "reverse_repo"
    assert tdx.classify_code("sz", "200002") == "b_share"
    assert tdx.classify_code("sz", "101006") == "bond"
    assert tdx.classify_code("sz", "159915") == "fund"
    assert tdx.classify_code("sz", "180101") == "fund"
    # 北：92 股、82/91 债、899 指数、43/83/87 老号段
    assert tdx.classify_code("bj", "920982") == "stock"
    assert tdx.classify_code("bj", "820001") == "bond"
    assert tdx.classify_code("bj", "910001") == "bond"
    assert tdx.classify_code("bj", "899050") == "index"
    assert tdx.classify_code("bj", "430047") == "bj_legacy_stock"
    # 认不出的 → unknown（不静默丢弃）
    assert tdx.classify_code("sh", "777777") == "unknown"
    assert tdx.classify_code("xx", "600000") == "unknown"


# ── normalize_to_contract：股÷100→手、元、单位元数据、血缘 ──────────
def _lineage():
    return {"url": "https://www.tdx.com.cn/products/data/data/g4day/20261022.zip",
            "fetched_at": "2026-11-01T09:54:40.538581+00:00",
            "zip_sha256": hashlib.sha256(b"fake-zip").hexdigest()}


def test_normalize_converts_shares_to_lots_with_poc_known_values():
    rows, _ = tdx.filter_stock_universe(_load_fixture_rows("tdx_pkg_rows_20261022_known.csv"))
    contract = tdx.normalize_to_contract(rows, lineage=_lineage())

    out = {r["code"]: r for r in contract["rows"]}
    pingan = out["000001.SZ"]
    # PoC-B：000001.SZ 2026-10-22 包原始量 129,764,875 股 ÷100 = 1,297,648.75 手；
    # 正式表 / 候选 CSV 手值 1,297,649（四舍五入一致，PoC 实测 0.0% 差）
    assert pingan["volume"] == pytest.approx(1297648.75)
    assert round(pingan["volume"]) == 1297649
    assert pingan["amount"] == pytest.approx(1468424722.09)
    assert pingan["code"] == "000001.SZ"
    # 688981.SH：35,790,512 股 ÷100 = 357,905.12 手（表值 357,905）
    smic = out["688981.SH"]
    assert smic["volume"] == pytest.approx(357905.12)
    assert round(smic["volume"]) == 357905
    assert smic["code"] == "688981.SH"
    assert smic["amount"] == pytest.approx(3577411258.0)
    # 920982.BJ（PoC-A 独立解析对拍一致）：3,289,703 股 ÷100 = 32,897.03 手
    jinbo = out["920982.BJ"]
    assert jinbo["volume"] == pytest.approx(32897.03)
    assert jinbo["code"] == "920982.BJ"
    assert jinbo["amount"] == pytest.approx(540594982.35)
    # 价格字段原样保留
    assert jinbo["close"] == pytest.approx(163.99)


def test_normalize_volume_lots_times_100_equals_raw_shares_exactly():
    rows, _ = tdx.filter_stock_universe(_load_fixture_rows("tdx_pkg_rows_20261022_known.csv"))
    contract = tdx.normalize_to_contract(rows, lineage=_lineage())
    for row in contract["rows"]:
        assert row["volume"] * 100 == pytest.approx(row["volume_raw"], abs=1e-6)
        assert row["volume_raw_unit"] == "shares"
        assert row["volume_unit"] == "lots"
        assert row["volume_divisor"] == 100
        assert row["amount_available"] is True
        assert row["amount_unit"] == "CNY"


def test_normalize_attaches_units_and_lineage_metadata():
    rows, _ = tdx.filter_stock_universe(_load_fixture_rows("tdx_pkg_rows_20261022_known.csv"))
    lineage = _lineage()
    contract = tdx.normalize_to_contract(rows, lineage=lineage)

    assert contract["source"] == "tdx_official_package"
    assert contract["units"] == {"volume": "lots", "amount": "CNY"}
    assert contract["lineage"]["url"] == lineage["url"]
    assert contract["lineage"]["fetched_at"] == lineage["fetched_at"]
    assert contract["lineage"]["zip_sha256"] == lineage["zip_sha256"]
    assert contract["lineage"]["row_count"] == 5
    assert contract["lineage"]["raw_row_count"] == 5
    assert len(contract["rows"]) == 5


def test_normalize_rejects_non_stock_rows_fail_closed():
    rows, _ = tdx.filter_stock_universe(_load_fixture_rows("tdx_pkg_rows_20261022_known.csv"))
    index_row = {"date": "2026-10-22", "market": "sh", "code": "999999", "name": "上证指数",
                 "prev_close": 3600.0, "open": 3605.0, "high": 3660.0, "low": 3595.0,
                 "close": 3599.0, "volume": 497973940, "amount": 764227190864.5}
    with pytest.raises(RuntimeError, match="999999|股票"):
        # 指数 volume 不是股，÷100 会错数据 —— 必须 fail-closed 拒绝
        tdx.normalize_to_contract(rows + [index_row], lineage=_lineage())


def test_normalize_requires_lineage_keys():
    rows, _ = tdx.filter_stock_universe(_load_fixture_rows("tdx_pkg_rows_20261022_known.csv"))
    with pytest.raises(ValueError, match="lineage"):
        tdx.normalize_to_contract(rows, lineage={"url": "x"})  # 缺 fetched_at / zip_sha256


# ── fetch_daily_package：下载与解析分离，带退避重试（原函数无重试） ──
class _FakeResponse:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content


class _FakeFetcher:
    """按脚本依次返回响应或抛网络异常，记录每次请求的 url 与 sleep 序列由外部注入。"""

    def __init__(self, script):
        self.script, self.calls = list(script), []

    def __call__(self, url, timeout):
        self.calls.append(url)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _no_sleep(_seconds):
    pass


def test_fetch_builds_url_from_date_forms_and_returns_lineage():
    import requests  # noqa: F401  只证明依赖在位；本测试不联网
    fetcher = _FakeFetcher([_FakeResponse(200, _valid_zip())])
    result = tdx.fetch_daily_package("2026-10-22", fetcher=fetcher, sleep=_no_sleep)

    assert fetcher.calls == ["https://www.tdx.com.cn/products/data/data/g4day/20261022.zip"]
    assert result["url"] == fetcher.calls[0]
    assert result["size"] == len(_valid_zip())
    assert result["sha256"] == hashlib.sha256(_valid_zip()).hexdigest()
    assert result["zip_sha256"] == result["sha256"]
    assert result["fetched_at"].endswith("+00:00")       # UTC ISO 时间戳
    assert result["attempts"] == 1
    # 下载与解析分离：content 原样返回，可离线喂给 parse_zip_bytes
    rows = tdx.parse_zip_bytes(result["content"], "20261022", min_priced={})
    assert len(rows) == 5


def test_fetch_accepts_compact_date_form_too():
    fetcher = _FakeFetcher([_FakeResponse(200, _valid_zip())])
    tdx.fetch_daily_package("20261022", fetcher=fetcher, sleep=_no_sleep)
    assert fetcher.calls[0].endswith("/20261022.zip")


def test_fetch_rejects_malformed_date_without_calling_network():
    fetcher = _FakeFetcher([])
    with pytest.raises(ValueError):
        tdx.fetch_daily_package("2026/10/22", fetcher=fetcher, sleep=_no_sleep)
    assert fetcher.calls == []


def test_fetch_404_raises_value_error_immediately_without_retry():
    # 404 = 非交易日 / 当日包未发布（约 16:10 北京时间后才有）：终态，不重试
    fetcher = _FakeFetcher([_FakeResponse(404)] * 5)
    sleeps = []
    with pytest.raises(ValueError, match="盘后包|404"):
        tdx.fetch_daily_package("2026-11-01", fetcher=fetcher, retries=3,
                                sleep=sleeps.append, timeout=60)
    assert len(fetcher.calls) == 1
    assert sleeps == []


def test_fetch_retries_transient_5xx_with_backoff_then_succeeds():
    script = [_FakeResponse(502), _FakeResponse(503), _FakeResponse(200, _valid_zip())]
    fetcher, sleeps = _FakeFetcher(script), []
    result = tdx.fetch_daily_package("2026-10-22", fetcher=fetcher, retries=3,
                                     sleep=sleeps.append)
    assert result["attempts"] == 3
    assert len(fetcher.calls) == 3
    assert sleeps == [1.0, 2.0]                          # 指数退避 1s, 2s


def test_fetch_retries_network_errors_with_exponential_backoff():
    script = [OSError("conn reset"), OSError("timeout"), OSError("refused"),
              _FakeResponse(200, _valid_zip())]
    fetcher, sleeps = _FakeFetcher(script), []
    result = tdx.fetch_daily_package("2026-10-22", fetcher=fetcher, retries=3,
                                     sleep=sleeps.append)
    assert result["attempts"] == 4                       # 首次 + 3 次重试
    assert sleeps == [1.0, 2.0, 4.0]


def test_fetch_raises_after_exhausting_retries():
    fetcher = _FakeFetcher([OSError("down")] * 4)
    sleeps = []
    with pytest.raises(RuntimeError, match="20261022.zip"):
        tdx.fetch_daily_package("2026-10-22", fetcher=fetcher, retries=3,
                                sleep=sleeps.append)
    assert len(fetcher.calls) == 4                       # retries=3 → 最多 4 次
    assert sleeps == [1.0, 2.0, 4.0]


def test_fetch_rejects_200_non_zip_body_immediately():
    fetcher = _FakeFetcher([_FakeResponse(200, b"<html>WAF error page</html>")])
    with pytest.raises(RuntimeError, match="zip"):
        tdx.fetch_daily_package("2026-10-22", fetcher=fetcher, sleep=_no_sleep)
    assert len(fetcher.calls) == 1                       # 错误页不是可重试的瞬态


def test_fetch_parse_filter_normalize_end_to_end_offline():
    # 全链路（下载→解析→过滤→契约化）离线打通，已知值逐字段核对
    fetcher = _FakeFetcher([_FakeResponse(200, _valid_zip())])
    result = tdx.fetch_daily_package("2026-10-22", fetcher=fetcher, sleep=_no_sleep)
    raw_rows = tdx.parse_zip_bytes(result["content"], "20261022", min_priced={})
    stocks, report = tdx.filter_stock_universe(raw_rows)
    contract = tdx.normalize_to_contract(
        stocks, lineage={"url": result["url"], "fetched_at": result["fetched_at"],
                         "zip_sha256": result["sha256"]})
    by_code = {r["code"]: r for r in contract["rows"]}
    assert report["kept"] == 5
    assert by_code["920982.BJ"]["volume"] == pytest.approx(32897.03)
    assert by_code["000001.SZ"]["volume"] == pytest.approx(1297648.75)
    assert round(by_code["688981.SH"]["volume"]) == 357905
    assert contract["lineage"]["zip_sha256"] == result["sha256"]
