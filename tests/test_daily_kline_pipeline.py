# -*- coding: utf-8 -*-
"""scripts/daily_kline_pipeline.py 的测试（严格 TDD，先于实现编写）。

离线原则：只使用合成盘后包字节 / 假 fetcher / 假 client 注入，
绝不访问网络或数据库；冻结夹具在 tests/fixtures/daily_pipeline/。
"""
import hashlib
import io
import json
import struct
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts import daily_kline_pipeline as pipeline

FIXTURES = Path(__file__).parent / "fixtures" / "daily_pipeline"
BEIJING = ZoneInfo("Asia/Shanghai")


# ── 合成盘后包（.cod 150B/条 + .md1 512B/块，布局与 PoC 验证实现一致） ──
# 行元组: (code, name, prev_close, open, high, low, close, volume[股], amount[元])
ROW_600000 = ("600000", "浦发银行", 9.61, 9.65, 9.66, 9.60, 9.62, 62529869, 602885524.0)
ROW_688981 = ("688981", "中芯国际", 101.98, 101.0, 101.44, 98.4, 100.09, 35790512, 3577411258.0)
ROW_000001 = ("000001", "平安银行", 11.41, 11.41, 11.42, 11.26, 11.30, 129764875, 1468424722.09)
ROW_300001 = ("300001", "特锐德", 22.10, 22.15, 22.40, 22.05, 22.32, 8123400, 180987654.32)
ROW_920982 = ("920982", "锦波生物", 154.25, 154.26, 169.72, 152.22, 163.99, 3289703, 540594982.35)
ROW_920000 = ("920000", "安徽凤凰", 13.19, 13.21, 13.25, 13.18, 13.22, 277319, 3676293.66)

# 非股票 / 零量行（parse 保留、filter 剔除并分类计数）
ROW_999999 = ("999999", "上证指数", 3600.0, 3605.0, 3660.0, 3595.0, 3599.0, 497973940, 764227190864.5)
ROW_510300 = ("510300", "沪深300ETF", 4.10, 4.11, 4.13, 4.09, 4.12, 30000000, 123600000.0)
ROW_399001 = ("399001", "深证成指", 11800.0, 11810.0, 11880.0, 11790.0, 11850.0, 300000000, 500000000000.0)
ROW_601111_VOL0 = ("601111", "中国国航", 8.50, 8.51, 8.52, 8.49, 8.50, 0, 0.0)  # 停牌股：有价无量

# R17（2026-11-09）：冻结 11 码大盘指数纳入写入的合成行。
# 指数 volume 包内原始即「手」（2026-10-21/11-09 实测：与 kline_daily QMT 存量、
# 腾讯日线手数逐码一致，比例 1.0000），语义上不 ÷100。
ROW_000300 = ("000300", "沪深300", 4150.0, 4160.0, 4188.0, 4155.0, 4180.0, 144000000, 387900000000.0)
ROW_399005 = ("399005", "中小100", 6500.0, 6510.0, 6550.0, 6490.0, 6530.0, 80000000, 210000000000.0)
ROW_880001 = ("880001", "通达信板块", 100.0, 101.0, 102.0, 99.0, 100.5, 500000, 80000000.0)

# 任务书 20261109_daily_index_bars_r17.md 冻结 11 码；R18（20261109_index_000985_removal）
# 移除 000985.SH 中证全指 → 10 码（独立字面量，防实现侧手误）
FROZEN_TEN = frozenset({
    "000001.SH", "399001.SZ", "399106.SZ", "000300.SH", "000016.SH", "000905.SH",
    "000852.SH", "399006.SZ", "000680.SH", "000688.SH",
})

SYNTHETIC_MARKETS = {
    "sh": [ROW_600000, ROW_688981, ROW_999999, ROW_510300, ROW_601111_VOL0],
    "sz": [ROW_000001, ROW_300001, ROW_399001],
    "bj": [ROW_920982, ROW_920000],
}
SYNTHETIC_KEPT = {"sh": 2, "sz": 2, "bj": 2}
TRADE_DATE = "2026-11-02"
TRADE_DATE_COMPACT = "20261102"


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


def make_zip(ymd, markets):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for market, rows in markets.items():
            cod = b"".join(_cod_record(r[0], i, r[1]) for i, r in enumerate(rows))
            md1 = b"".join(_md1_block(*r[2:]) for r in rows)
            archive.writestr(f"{market}{ymd[2:]}.cod", cod)
            archive.writestr(f"{market}{ymd[2:]}.md1", md1)
    return buffer.getvalue()


def synthetic_zip():
    return make_zip(TRADE_DATE_COMPACT, SYNTHETIC_MARKETS)


class FakeResponse:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content


class FakeFetcher:
    """按脚本依次返回响应或抛异常；记录每次请求的 url。"""

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


def _fetch_pkg(content=None):
    """注入用盘后包下载器：返回 200 + 合成包，与真实 fetcher 同签名。"""
    return lambda url, timeout: FakeResponse(200, synthetic_zip() if content is None else content)


def _forbidden_client_factory():
    def factory():
        raise AssertionError("默认模式绝不允许建立数据库连接")
    return factory


def _base_options(**overrides):
    options = {
        "trade_date": TRADE_DATE,
        "source": "pkg",
        "crosscheck_sample": 0,
        "stage_write": False,
        "output": None,
        "fetch_package": _fetch_pkg(),
        "min_priced": {},                      # 合成小包放宽解析下限
        "client_factory": _forbidden_client_factory(),
        "crosscheck_fetch": None,
        "now": datetime(2026, 11, 2, 17, 0, tzinfo=BEIJING),
        "report_root": None,                   # 测试里显式给 output，避免写默认盘
    }
    options.update(overrides)
    return options


# ── 常量与白名单 ─────────────────────────────────────────────────────
def test_stage_table_whitelist_is_hardcoded_single_name():
    assert pipeline.STAGE_TABLE_WHITELIST == ("stock.kline_daily_pkg_stage_20261101",)
    assert pipeline.STAGE_TABLE == "stock.kline_daily_pkg_stage_20261101"
    assert pipeline.KLINE_DAILY_TABLE == "stock.kline_daily"


def test_assert_stage_table_allowed_accepts_whitelist_and_rejects_everything_else():
    pipeline.assert_stage_table_allowed("stock.kline_daily_pkg_stage_20261101")
    for bad in ("stock.kline_daily", "stock.kline_daily_pkg_stage_20261231",
                "stock.kline_daily_pkg_stage_20261101_copy", "", "other.kline_daily_pkg_stage_20261101"):
        with pytest.raises(ValueError, match="白名单|whitelist"):
            pipeline.assert_stage_table_allowed(bad)


# ── 默认模式：显式 --trade-date、无 --stage-write → 零 DB 访问 ──────
def test_offline_happy_path_zero_db_access(tmp_path):
    output = tmp_path / "report.json"
    result = pipeline.run_pipeline(_base_options(output=output))

    assert result["exit_code"] == 0
    report = result["report"]
    assert report["status"] == "passed"
    assert report["db_access"] is False            # 全程未触碰数据库
    assert output.is_file()
    on_disk = json.loads(output.read_text(encoding="utf-8"))
    assert on_disk == report                       # 落盘内容与返回值一致


def test_offline_report_lineage_and_row_counts(tmp_path):
    content = synthetic_zip()
    result = pipeline.run_pipeline(_base_options(output=tmp_path / "r.json", fetch_package=_fetch_pkg(content)))
    report = result["report"]

    lineage = report["lineage"]
    assert lineage["url"] == f"https://www.tdx.com.cn/products/data/data/g4day/{TRADE_DATE_COMPACT}.zip"
    assert lineage["zip_sha256"] == hashlib.sha256(content).hexdigest()
    assert lineage["fetched_at"].endswith("+00:00")
    assert lineage["size_bytes"] == len(content)
    assert lineage["row_count"] == 7          # R17：6 股票 + 399001.SZ（清单内指数纳入）

    counts = report["row_counts"]
    assert counts["total_rows"] == 10
    assert counts["kept"] == 6
    assert counts["kept_by_market"] == SYNTHETIC_KEPT
    assert counts["stock_zero_volume"] == 1
    assert counts["excluded_by_category"] == {"fund": 1, "index": 2}
    # R17 说明：excluded_by_category 是适配层股票池过滤的原始普查口径（399001.SZ
    # 也计入其中）；冻结指数的实际纳入结果以 index_inclusion 为准（对账透明）。
    inclusion = counts["index_inclusion"]                              # R17：冻结指数纳入报告
    assert inclusion["kept"] == 1
    assert inclusion["kept_codes"] == ["399001.SZ"]
    assert inclusion["zero_volume"] == 0
    assert inclusion["missing_codes"] == sorted(FROZEN_TEN - {"399001.SZ"})
    assert counts["unknown"] == []
    assert report["units"] == {"volume": "lots", "amount": "CNY"}


def test_offline_report_gate_statuses(tmp_path):
    result = pipeline.run_pipeline(_base_options(output=tmp_path / "r.json"))
    gates = result["report"]["gates"]

    for name in ("ohlc_envelope", "non_negative_volume_amount", "neeq_policy", "duplicate_keys"):
        assert gates[name]["status"] == "pass", name
    # 默认模式零 DB：依赖正式表的两个门禁显式跳过并给原因
    assert gates["row_count_vs_prev_day"]["status"] == "skipped"
    assert "db" in gates["row_count_vs_prev_day"]["reason"].lower() or "DB" in gates["row_count_vs_prev_day"]["reason"]
    assert gates["formal_overlap_consistency"]["status"] == "skipped"
    assert gates["tencent_crosscheck"]["status"] == "skipped"      # crosscheck_sample=0
    assert result["report"]["stage_write"]["performed"] is False


def test_offline_rows_are_normalized_to_lots_and_cny(tmp_path):
    result = pipeline.run_pipeline(_base_options(output=tmp_path / "r.json"))
    rows = {row["code"]: row for row in result["rows"]}
    assert rows["600000.SH"]["volume"] == pytest.approx(625298.69)
    assert rows["600000.SH"]["volume_unit"] == "lots"
    assert rows["000001.SZ"]["volume"] == pytest.approx(1297648.75)
    assert rows["000001.SZ"]["amount"] == pytest.approx(1468424722.09)
    assert rows["920982.BJ"]["volume"] == pytest.approx(32897.03)
    assert rows["600000.SH"]["close"] == pytest.approx(9.62)
    # R17：清单内指数 399001.SZ 纳入；volume 保持包内「手」（指数量纲与个股不同，不 ÷100）
    assert rows["399001.SZ"]["volume"] == pytest.approx(300000000.0)
    assert rows["399001.SZ"]["volume_unit"] == "lots"
    assert rows["399001.SZ"]["amount"] == pytest.approx(500000000000.0)
    assert set(rows) == {"600000.SH", "688981.SH", "000001.SZ", "300001.SZ", "920982.BJ", "920000.BJ",
                         "399001.SZ"}


def test_offline_failure_exit_nonzero_and_report_written(tmp_path):
    # low > high 的坏行：解析/过滤都放行（格式合法），由 OHLC 包络门禁拦下
    bad = ("600519", "贵州茅台", 1500.0, 1502.0, 1510.0, 1520.0, 1505.0, 123400, 186000000.0)
    markets = {"sh": [ROW_600000, bad], "sz": [ROW_000001], "bj": [ROW_920982]}
    output = tmp_path / "failed.json"
    result = pipeline.run_pipeline(_base_options(
        output=output, fetch_package=_fetch_pkg(make_zip(TRADE_DATE_COMPACT, markets))))

    assert result["exit_code"] != 0
    report = result["report"]
    assert report["status"] == "failed"
    assert report["gates"]["ohlc_envelope"]["status"] == "fail"
    assert "600519.SH" in str(report["gates"]["ohlc_envelope"]["violations"])
    assert output.is_file()
    assert report["stage_write"]["performed"] is False


def test_fetch_failure_is_error_status_with_report(tmp_path):
    # 官网 404（非交易日/未发布）→ ValueError → status=error、退出码 2、报告仍落盘
    output = tmp_path / "err.json"
    result = pipeline.run_pipeline(_base_options(
        output=output, fetch_package=FakeFetcher([FakeResponse(404)])))

    assert result["exit_code"] == 2
    assert result["report"]["status"] == "error"
    assert "404" in result["report"]["error"]
    assert output.is_file()


# ── R17（2026-11-09）：冻结大盘指数纳入写入（TDX 官网包同源）；R18 同日移除 000985.SH ──
# 语义（任务书 task_briefs/20261109_daily_index_bars_r17.md；R18=20261109_index_000985_removal）：
# - index 类中 code ∈ 冻结清单 → 纳入写入，与个股同门禁/id/only-missing-keys/
#   读回验证/回滚 marker；其余 index（含 597 清单内行业指数）与 tdx_internal_index
#   继续排除并计数；包内缺码如实报告，绝不硬造。
# - 指数 volume 包内原始即「手」，不做 ÷100（个股才是 股÷100→手）。
# - R18：000985.SH 中证全指出清单（11→10 码），回归排除类（官网包从不含该码，
#   历史 K 线停在 10-21，Owner 裁决下线）。
def test_frozen_index_codes_constant_is_exactly_the_frozen_ten():
    assert pipeline.FROZEN_INDEX_CODES == FROZEN_TEN
    assert len(FROZEN_TEN) == 10
    assert "000985.SH" not in pipeline.FROZEN_INDEX_CODES   # R18：中证全指出清单


def test_r18_removed_000985_row_stays_excluded(tmp_path):
    # R18：000985.SH 中证全指已移出冻结清单，包内出现该行也只进排除计数，不写入。
    row_000985 = ("000985", "中证全指", 5000.0, 5010.0, 5020.0, 5000.0, 5015.0,
                  90000000, 260000000000.0)
    markets = {"sh": [ROW_600000, ROW_000300, row_000985], "sz": [ROW_000001], "bj": [ROW_920982]}
    result = pipeline.run_pipeline(_base_options(
        output=tmp_path / "r.json", fetch_package=_fetch_pkg(make_zip(TRADE_DATE_COMPACT, markets))))

    assert result["exit_code"] == 0
    codes = {r["code"] for r in result["rows"]}
    assert "000985.SH" not in codes                        # 出清单 → 不写
    assert "000300.SH" in codes                            # 清单内 000300 照常纳入
    inclusion = result["report"]["row_counts"]["index_inclusion"]
    assert inclusion["kept_codes"] == ["000300.SH"]
    assert "000985.SH" not in inclusion["missing_codes"]   # 不在清单内，缺码报告无它
    assert result["report"]["row_counts"]["excluded_by_category"]["index"] == 2  # 原始普查：000300（纳入仍计）+000985（维持排除）


def test_frozen_index_rows_included_others_still_excluded(tmp_path):
    markets = {"sh": [ROW_600000, ROW_000300, ROW_999999, ROW_880001, ROW_510300, ROW_601111_VOL0],
               "sz": [ROW_000001, ROW_300001, ROW_399001, ROW_399005],
               "bj": [ROW_920982]}
    result = pipeline.run_pipeline(_base_options(
        output=tmp_path / "r.json", fetch_package=_fetch_pkg(make_zip(TRADE_DATE_COMPACT, markets))))

    assert result["exit_code"] == 0
    rows = {row["code"]: row for row in result["rows"]}
    assert "000300.SH" in rows and "399001.SZ" in rows          # 清单内指数纳入
    assert "999999.SH" not in rows                              # 清单外指数仍排除
    assert "399005.SZ" not in rows
    assert "880001.SH" not in rows                              # tdx_internal_index 仍排除
    counts = result["report"]["row_counts"]
    assert counts["index_inclusion"]["kept"] == 2
    assert counts["index_inclusion"]["kept_codes"] == ["000300.SH", "399001.SZ"]
    assert counts["index_inclusion"]["zero_volume"] == 0
    assert counts["excluded_by_category"]["index"] == 4         # 原始普查：999999+399005+000300+399001
    assert counts["excluded_by_category"]["tdx_internal_index"] == 1
    # 最终去向：000300/399001 重新纳入（index_inclusion），999999/399005 维持排除
    # 与个股同门禁：指数行进入同一 rows 流水，离线四门禁全过
    gates = result["report"]["gates"]
    for name in ("ohlc_envelope", "non_negative_volume_amount", "neeq_policy", "duplicate_keys"):
        assert gates[name]["status"] == "pass", name


def test_frozen_index_volume_kept_in_lots_not_divided_by_100(tmp_path):
    result = pipeline.run_pipeline(_base_options(output=tmp_path / "r.json"))
    row = {r["code"]: r for r in result["rows"]}["399001.SZ"]
    assert row["volume"] == pytest.approx(300000000.0)          # 手：包内原始值，不 ÷100
    assert row["volume_unit"] == "lots"
    assert row["volume_raw_unit"] == "lots"
    assert row["volume_divisor"] == 1
    assert row["amount"] == pytest.approx(500000000000.0)       # 元
    assert row["market"] == "sz"
    assert row["prev_close"] == pytest.approx(11800.0)


def test_frozen_index_absent_codes_reported_not_fabricated(tmp_path):
    markets = {"sh": [ROW_600000, ROW_999999], "sz": [ROW_000001, ROW_399005], "bj": [ROW_920982]}
    result = pipeline.run_pipeline(_base_options(
        output=tmp_path / "r.json", fetch_package=_fetch_pkg(make_zip(TRADE_DATE_COMPACT, markets))))

    assert result["exit_code"] == 0                              # 缺码不是错误：如实报告不硬造
    inclusion = result["report"]["row_counts"]["index_inclusion"]
    assert inclusion["kept"] == 0
    assert inclusion["kept_codes"] == []
    assert inclusion["missing_codes"] == sorted(FROZEN_TEN)   # 全部 10 码缺席（官网包实况：R18 前从不含 000985.SH）


def test_frozen_index_zero_volume_row_not_included(tmp_path):
    zero_vol = ("000300", "沪深300", 4150.0, 4160.0, 4188.0, 4155.0, 4180.0, 0, 0.0)
    markets = {"sh": [ROW_600000, zero_vol], "sz": [ROW_000001], "bj": [ROW_920982]}
    result = pipeline.run_pipeline(_base_options(
        output=tmp_path / "r.json", fetch_package=_fetch_pkg(make_zip(TRADE_DATE_COMPACT, markets))))

    assert result["exit_code"] == 0
    assert "000300.SH" not in {r["code"] for r in result["rows"]}
    inclusion = result["report"]["row_counts"]["index_inclusion"]
    assert inclusion["kept"] == 0
    assert inclusion["zero_volume"] == 1                         # 防御量纲破坏：无量指数不写
    assert inclusion["missing_codes"] == sorted(FROZEN_TEN)


def test_frozen_index_contract_fail_closed_on_non_frozen_row():
    raw = {"date": TRADE_DATE, "market": "sz", "code": "399005", "name": "中小100",
           "prev_close": 6500.0, "open": 6510.0, "high": 6550.0, "low": 6490.0,
           "close": 6530.0, "volume": 80000000, "amount": 210000000000.0}
    with pytest.raises(RuntimeError, match="冻结|frozen"):
        pipeline.frozen_index_rows_to_contract([raw])


def test_select_crosscheck_codes_excludes_frozen_indexes():
    # 腾讯指数量口径与官网包存在系统性差异（399006.SZ 实测 raw/tencent=1.0439，
    # 而 QMT 存量与官网包逐码 1.0000），指数不入腾讯交叉抽样，个股抽样不变。
    rows = _selectable_rows() + [_contract_row("000300.SH", close=4180.0, volume=144000000.0)]
    assert pipeline.select_crosscheck_codes(rows, 99, TRADE_DATE) == sorted(PKG_CLOSE)


def test_formal_write_inserts_missing_frozen_index_keys(tmp_path):
    overlap = [  # 该日 6 只个股键均已存在且值与包一致 → 只缺 399001.SZ 指数键
        {"code": "600000.SH", "open": 9.65, "high": 9.66, "low": 9.60, "close": 9.62,
         "volume": 625298.69, "amount": 602885524.0},
        {"code": "688981.SH", "open": 101.0, "high": 101.44, "low": 98.4, "close": 100.09,
         "volume": 357905.12, "amount": 3577411258.0},
        {"code": "000001.SZ", "open": 11.41, "high": 11.42, "low": 11.26, "close": 11.30,
         "volume": 1297648.75, "amount": 1468424722.09},
        {"code": "300001.SZ", "open": 22.15, "high": 22.40, "low": 22.05, "close": 22.32,
         "volume": 81234.0, "amount": 180987654.32},
        {"code": "920982.BJ", "open": 154.26, "high": 169.72, "low": 152.22, "close": 163.99,
         "volume": 32897.03, "amount": 540594982.35},
        {"code": "920000.BJ", "open": 13.21, "high": 13.25, "low": 13.18, "close": 13.22,
         "volume": 2773.19, "amount": 3676293.66},
    ]
    options, client = _formal_options(tmp_path, overlap_rows=overlap)
    result = pipeline.run_pipeline(options)

    assert result["exit_code"] == 0
    formal = result["report"]["formal_write"]
    assert formal["performed"] is True
    assert formal["inserted_rows"] == 1                          # only-missing-keys：只插指数键
    assert formal["missing_key_count"] == 1
    assert formal["readback"]["row_count"] == 7                  # 6 已有 + 1 新插
    assert formal["readback"]["row_count_ok"] is True
    assert formal["readback"]["digest_match"] is True
    assert formal["readback"]["bad_id_rows"] == 0
    table, values, columns = client.inserts[0]
    assert table == FORMAL_TABLE
    assert len(values) == 1
    item = dict(zip(columns, values[0]))
    assert item["code"] == "399001.SZ"
    assert item["volume"] == pytest.approx(300000000.0)          # 手：原始值直写正式表
    assert item["amount"] == pytest.approx(500000000000.0)
    assert item["id"] == pipeline.formal_row_id("399001.SZ", TRADE_DATE)


# ── CLI 参数 ─────────────────────────────────────────────────────────
def test_parse_args_defaults():
    args = pipeline.parse_args([])
    assert args.trade_date is None
    assert args.source == "pkg"
    assert args.crosscheck_sample == 20
    assert args.stage_write is False
    assert args.formal_write is False
    assert args.output is None


def test_parse_args_rejects_unknown_source_and_negative_sample():
    with pytest.raises(SystemExit):
        pipeline.parse_args(["--source", "tencent"])
    with pytest.raises(SystemExit):
        pipeline.parse_args(["--crosscheck-sample", "-1"])


def test_default_output_path_uses_runtime_daily_pipeline_dir():
    path = pipeline.default_output_path(TRADE_DATE, data_root=Path("F:/AiStock/data/data"))
    assert path == Path("F:/AiStock/data/data/runtime/daily_kline_coverage/daily_pipeline/"
                        "2026-11-02/report.json")


# ── 门禁函数：OHLC 包络 ──────────────────────────────────────────────
def _contract_row(code, **overrides):
    row = {"code": code, "date": TRADE_DATE, "open": 10.0, "high": 10.5, "low": 9.8,
           "close": 10.2, "volume": 1234.5, "amount": 1234567.8}
    row.update(overrides)
    return row


def test_gate_ohlc_envelope_passes_on_valid_rows():
    gate = pipeline.check_ohlc_envelope([_contract_row("600000.SH"), _contract_row("000001.SZ")])
    assert gate["status"] == "pass"
    assert gate["checked"] == 2
    assert gate["violations"] == []


def test_gate_ohlc_envelope_flags_low_above_open_close_and_high_below():
    low_above = _contract_row("600001.SH", low=10.6)                  # low > max(open, close)
    high_below = _contract_row("600002.SH", high=9.9)                 # high < min(open, close)
    negative = _contract_row("600003.SH", open=-1.0)                  # 非负要求
    nan_low = _contract_row("600004.SH", low=float("nan"))            # 有限数要求
    gate = pipeline.check_ohlc_envelope([low_above, high_below, negative, nan_low, _contract_row("600005.SH")])
    assert gate["status"] == "fail"
    assert gate["violation_count"] == 4
    codes = {v["code"] for v in gate["violations"]}
    assert codes == {"600001.SH", "600002.SH", "600003.SH", "600004.SH"}


# ── 门禁函数：量额非负 ───────────────────────────────────────────────
def test_gate_non_negative_volume_amount():
    ok = pipeline.check_non_negative_volume_amount(
        [_contract_row("600000.SH", volume=0.0, amount=0.0), _contract_row("000001.SZ")])
    assert ok["status"] == "pass"                      # 0 是合法下限（停牌日）
    bad = pipeline.check_non_negative_volume_amount(
        [_contract_row("600001.SH", volume=-5.0), _contract_row("600002.SH", amount=-1.5)])
    assert bad["status"] == "fail"
    assert bad["violation_count"] == 2


# ── 门禁函数：NEEQ 安全码策略拒收 ────────────────────────────────────
def test_gate_neeq_policy_blocks_legacy_and_neeq_but_exempts_bj_and_tdx_index():
    gate = pipeline.check_neeq_policy([
        _contract_row("430047.SZ"),      # 老三板/NEEQ 43 段 → 拒收
        _contract_row("400001.SZ"),      # 400 段 → 拒收
        _contract_row("920982.BJ"),      # 北交所 920 豁免
        _contract_row("880001.SH"),      # 通达信自编指数豁免（但本就不会进股票池）
        _contract_row("600000.SH"),
    ])
    assert gate["status"] == "fail"
    assert gate["blocked_codes"] == ["400001.SZ", "430047.SZ"]
    clean = pipeline.check_neeq_policy([_contract_row("600000.SH"), _contract_row("920982.BJ")])
    assert clean["status"] == "pass"
    assert clean["blocked_codes"] == []


# ── 门禁函数：重复键零容忍 ───────────────────────────────────────────
def test_gate_duplicate_keys_zero_tolerance():
    rows = [_contract_row("600000.SH"), _contract_row("600000.SH"),
            _contract_row("000001.SZ"), _contract_row("000001.SZ"), _contract_row("000001.SZ"),
            _contract_row("920982.BJ")]
    gate = pipeline.check_duplicate_keys(rows)
    assert gate["status"] == "fail"
    by_code = {d["code"]: d["count"] for d in gate["duplicates"]}
    assert by_code == {"600000.SH": 2, "000001.SZ": 3}
    assert pipeline.check_duplicate_keys([_contract_row("600000.SH"), _contract_row("000001.SZ")])["status"] == "pass"


def test_pipeline_rejections_aggregate_failed_gate_items(tmp_path):
    bad = ("600519", "贵州茅台", 1500.0, 1502.0, 1510.0, 1520.0, 1505.0, 123400, 186000000.0)
    markets = {"sh": [ROW_600000, bad], "sz": [ROW_000001], "bj": [ROW_920982]}
    result = pipeline.run_pipeline(_base_options(
        output=tmp_path / "r.json", fetch_package=_fetch_pkg(make_zip(TRADE_DATE_COMPACT, markets))))
    rejections = result["report"]["rejections"]
    assert rejections and all(r["gate"] == "ohlc_envelope" for r in rejections)
    assert any("600519.SH" in str(r["item"]) for r in rejections)
    assert result["report"]["failed_gates"] == ["ohlc_envelope"]


# ── 假 ClickHouse client：只读应答 + 写操作记录 ──────────────────────
class FakeResult:
    def __init__(self, rows, column_names):
        self.result_rows = rows
        self.column_names = list(column_names)


class FakeCHClient:
    """按 SQL 形状应答的假 client；query() 只接受 SELECT，写操作走 command()/insert()。

    正式表模拟：overlap_rows 为该日预置（门禁视图 + 锁内反连接视图共用），
    formal_rows 记录 --formal-write 插入的行；anti_join_inject 在锁内反连接那一次
    读取时并入额外行（模拟门禁与锁之间并发写入）。锁模拟：write_lock DDL 标记表。
    """

    def __init__(self, calendar_sessions=(), formal_latest=None, prev_day_codes=(),
                 overlap_rows=(), stocks_rows=(), stage_corrupt=False,
                 formal_corrupt=False, formal_id_wrong=False, lock_held=False,
                 probe_answers=None, anti_join_inject=None):
        self.calendar_sessions = [str(d) for d in calendar_sessions]
        self.formal_latest = str(formal_latest) if formal_latest else None
        self.prev_day_codes = list(prev_day_codes)
        self.overlap_rows = list(overlap_rows)
        self.stocks_rows = list(stocks_rows)
        self.stage_corrupt = stage_corrupt
        self.formal_corrupt = formal_corrupt
        self.formal_id_wrong = formal_id_wrong
        self.lock_held = lock_held
        self.probe_answers = dict(probe_answers or {})
        self.anti_join_inject = list(anti_join_inject or [])
        self.stage_rows = {}                     # {(trade_date, code): row dict}
        self.formal_rows = {}                    # {(trade_date, code): row dict}
        self.lock_tables = set()
        self.lock_marker_rows = []               # 标记表内的 metadata 行
        self.day_row_reads = 0                   # 该日 7 列读取次数（门禁 1 次/反连接 2 次）
        self.event_log = []                      # 时序：("query"|"command", sql)
        self.queries, self.commands, self.inserts = [], [], []
        self.non_select_queries, self.non_readonly_queries = [], []

    _STAGE_COLS = ["code", "trade_date", "open", "high", "low", "close", "volume",
                   "amount", "prev_close", "name", "market", "source_url", "zip_sha256",
                   "fetched_at", "pipeline_version"]

    _FORMAL_DAY_COLS = ["code", "open", "high", "low", "close", "volume", "amount"]

    _FORMAL_READBACK_COLS = ["code", "open", "high", "low", "close", "volume", "amount",
                             "amplitude", "change_pct", "change_amount", "turnover_rate", "id"]

    _LOCK_METADATA_COLS = ["scope", "owner", "purpose", "acquired_at", "host", "pid"]

    def stage_count_for(self, trade_date):
        return sum(1 for (day, _code) in self.stage_rows if day == str(trade_date))

    def _held_holder_row(self):
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return ("kline_daily_daily_append", "other_writer@elsewhere:1",
                "competing daily append", now_text, "elsewhere", 4242)

    def _formal_day_view(self, day):
        """该日 7 列视图：预置 overlap + 已插入行（+ 可选读回期破坏）。"""
        rows = {str(row["code"]): {c: row.get(c) for c in self._FORMAL_DAY_COLS}
                for row in self.overlap_rows}
        for (d, code), row in self.formal_rows.items():
            if d == day:
                rows[code] = {c: row.get(c) for c in self._FORMAL_DAY_COLS}
        out = [rows[code] for code in sorted(rows)]
        if self.formal_corrupt and self.formal_rows and out:
            out[0] = {**out[0], "volume": float(out[0]["volume"]) + 999.0}
        return out

    def query(self, sql, settings=None, **_kwargs):
        self.queries.append(sql)
        self.event_log.append(("query", sql))
        if not sql.lstrip().upper().startswith("SELECT"):
            self.non_select_queries.append(sql)
            raise AssertionError(f"query() 只允许 SELECT: {sql}")
        if not (settings or {}).get("readonly"):
            self.non_readonly_queries.append(sql)
        if "write_lock_" in sql:
            if "acquired_at" in sql:                       # 标记表 metadata 读取
                if self.lock_held and not self.lock_marker_rows:
                    row = self._held_holder_row()
                elif self.lock_marker_rows:
                    row = self.lock_marker_rows[-1]
                else:
                    row = None
                rows = [row] if row else []
                return FakeResult(rows, self._LOCK_METADATA_COLS)
            if "system.tables" in sql or "table_count" in sql:   # system.tables 存在性
                import re as _re4
                name = _re4.search(r"name='([^']+)'", sql)
                exists = name is not None and (
                    name.group(1) in self.lock_tables
                    or (self.lock_held and name.group(1).startswith("write_lock_")))
                return FakeResult([(1 if exists else 0,)], ["table_count"])
            raise AssertionError(f"假 client 不认识的锁查询: {sql}")
        if "system.tables" in sql:
            import re as _re4
            name = _re4.search(r"name='([^']+)'", sql)
            exists = name is not None and (
                name.group(1) in self.lock_tables
                or (self.lock_held and name.group(1).startswith("write_lock_")))
            return FakeResult([(1 if exists else 0,)], ["table_count"])
        if "cityHash64(" in sql and " FROM " not in sql.upper():   # id 探针
            import re as _re5
            match = _re5.search(r"cityHash64\('([^']+)', toDate\('(\d{4}-\d{2}-\d{2})'\)\)", sql)
            code, day = match.group(1), match.group(2)
            value = self.probe_answers.get(code, pipeline.formal_row_id(code, day))
            return FakeResult([(value,)], ["probe_id"])
        if "kline_daily_pkg_stage" in sql:
            import re as _re2
            match = _re2.search(r"toDate\('(\d{4}-\d{2}-\d{2})'\)", sql)
            day = match.group(1)
            if "count()" in sql:
                return FakeResult([(self.stage_count_for(day),)], ["row_count"])
            rows = [dict(value) for (d, _c), value in self.stage_rows.items() if d == day]
            rows.sort(key=lambda row: row["code"])
            if self.stage_corrupt and rows:
                rows[0] = {**rows[0], "volume": rows[0]["volume"] + 999.0}
            cols = self._STAGE_COLS
            return FakeResult([tuple(row.get(c) for c in cols) for row in rows], cols)
        if "max(trade_date)" in sql:
            return FakeResult([(self.formal_latest,)], ["latest"])
        if "trade_calendar" in sql:
            import re as _re
            match = _re.search(r"toDate\('(\d{4}-\d{2}-\d{2})'\)", sql)
            candidate = match.group(1)
            sessions = [d for d in self.calendar_sessions if d <= candidate][-5:]
            return FakeResult([(d,) for d in reversed(sessions)], ["trade_date"])
        if "FROM stock.stocks" in sql:
            cols = ["code", "list_date", "delist_date", "quit", "listing_status"]
            return FakeResult([tuple(row.get(c) for c in cols) for row in self.stocks_rows], cols)
        if "AS market" in sql:
            return FakeResult([(code, self._market_of(code)) for code in self.prev_day_codes],
                              ["code", "market"])
        if "stock.kline_daily" in sql:
            import re as _re6
            match = _re6.search(r"toDate\('(\d{4}-\d{2}-\d{2})'\)", sql)
            day = match.group(1)
            if "count()" in sql and "id != cityHash64" in sql:      # id 服务端一致性
                bad = 0
                stamp_match = _re6.search(r"created_at = toDateTime\('([^']+)'\)", sql)
                stamp = stamp_match.group(1) if stamp_match else None
                for (d, code), row in self.formal_rows.items():
                    if d != day or (stamp and str(row.get("created_at"))[:19] != stamp):
                        continue
                    if self.formal_id_wrong or row.get("id") != pipeline.formal_row_id(code, d):
                        bad += 1
                return FakeResult([(bad,)], ["bad_id_rows"])
            if "count()" in sql:
                if "id IN (" in sql:                                   # 回滚后按 id 列表计数剩余行
                    ids = {int(value) for value in
                           _re6.search(r"id IN \(([^)]+)\)", sql).group(1).split(",")}
                    return FakeResult(
                        [(sum(1 for (d, _c), row in self.formal_rows.items()
                              if d == day and row.get("id") in ids),)],
                        ["row_count"])
                if "created_at = toDateTime" in sql:                # 回滚 marker 剩余行
                    stamp = _re6.search(r"created_at = toDateTime\('([^']+)'\)", sql).group(1)
                    return FakeResult(
                        [(sum(1 for (d, _c), row in self.formal_rows.items()
                              if d == day and str(row.get("created_at"))[:19] == stamp),)],
                        ["row_count"])
                return FakeResult([(len(self._formal_day_view(day)),)], ["row_count"])
            if "created_at = toDateTime" in sql:                    # 本批次抽样读回
                stamp = _re6.search(r"created_at = toDateTime\('([^']+)'\)", sql).group(1)
                rows = [dict(row) for (d, _c), row in self.formal_rows.items()
                        if d == day and str(row.get("created_at"))[:19] == stamp]
                rows.sort(key=lambda row: str(row["code"]))
                cols = self._FORMAL_READBACK_COLS
                return FakeResult([tuple(row.get(c) for c in cols) for row in rows], cols)
            if "ORDER BY code" in sql:                              # 该日 7 列视图（门禁/反连接/digest）
                self.day_row_reads += 1
                rows = self._formal_day_view(day)
                if self.day_row_reads == 2 and self.anti_join_inject:
                    rows = sorted(rows + [dict(r) for r in self.anti_join_inject],
                                  key=lambda row: str(row["code"]))
                cols = self._FORMAL_DAY_COLS
                return FakeResult([tuple(row.get(c) for c in cols) for row in rows], cols)
            raise AssertionError(f"假 client 不认识的正式表查询: {sql}")
        raise AssertionError(f"假 client 不认识的查询: {sql}")

    @staticmethod
    def _market_of(code):
        return {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(code.split(".")[-1], "other")

    def command(self, sql, settings=None, **_kwargs):
        self.commands.append((sql, settings or {}))
        self.event_log.append(("command", sql))
        upper = sql.lstrip().upper()
        import re as _re3
        if upper.startswith("CREATE") and "write_lock_" in sql:
            table = _re3.search(r"CREATE TABLE (\S+)", sql).group(1)
            if self.lock_held or table in self.lock_tables:
                raise RuntimeError(f"Table {table} already exists")
            self.lock_tables.add(table)
            return 1
        if upper.startswith("INSERT") and "write_lock_" in sql:
            quoted = _re3.findall(r"'([^']*)'", sql)
            values = quoted[:5] + [int(_re3.search(r", (\d+)\)$", sql).group(1))]
            self.lock_marker_rows.append(tuple(values))
            return 1
        if upper.startswith("DROP") and "write_lock_" in sql:
            table = _re3.search(r"DROP TABLE (\S+)", sql).group(1)
            self.lock_tables.discard(table)
            return 1
        if upper.startswith("ALTER") and "DELETE" in upper and "stock.kline_daily" in sql \
                and "pkg_stage" not in sql:
            day = _re3.search(r"toDate\('(\d{4}-\d{2}-\d{2})'\)", sql).group(1)
            ids = {int(value) for value in
                   _re3.search(r"id IN \(([^)]+)\)", sql).group(1).split(",")}
            for key in [k for k in self.formal_rows
                        if k[0] == day and self.formal_rows[k].get("id") in ids]:
                del self.formal_rows[key]
            return 1
        if upper.startswith("ALTER") and "DELETE" in upper and "kline_daily_pkg_stage" in sql:
            day = _re3.search(r"toDate\('(\d{4}-\d{2}-\d{2})'\)", sql).group(1)
            for key in [k for k in self.stage_rows if k[0] == day]:
                del self.stage_rows[key]
        return 1

    def insert(self, table, values, column_names=None, **_kwargs):
        self.inserts.append((table, list(values), list(column_names or [])))
        self.event_log.append(("insert", table))
        columns = list(column_names or [])
        for row in values:
            item = dict(zip(columns, row))
            key = (str(item.get("trade_date")), item["code"])
            if table == "stock.kline_daily":
                self.formal_rows[key] = item
            else:
                self.stage_rows[key] = item
        return None

    def close(self):
        return None


PREV_DAY = "2026-10-30"
KEPT_CODES = ["600000.SH", "688981.SH", "000001.SZ", "300001.SZ", "920982.BJ", "920000.BJ"]


def _db_options(tmp_path, **client_overrides):
    kwargs = dict(
        calendar_sessions=["2026-10-29", PREV_DAY, TRADE_DATE],
        formal_latest=PREV_DAY,
        prev_day_codes=KEPT_CODES,
    )
    kwargs.update(client_overrides)
    client = FakeCHClient(**kwargs)
    options = _base_options(output=tmp_path / "r.json")
    options["trade_date"] = None                       # 触发只读解析 + DB 门禁
    options["client_factory"] = lambda: client
    return options, client


# ── 交易日解析：正式表最近有行日期 + 日历判断 + 16:10 截止 ──────────
def test_resolve_trade_date_same_day_after_1610():
    client = FakeCHClient(calendar_sessions=[PREV_DAY, TRADE_DATE], formal_latest=PREV_DAY)
    now = datetime(2026, 11, 2, 17, 0, tzinfo=BEIJING)
    resolution = pipeline.resolve_latest_completed_trade_date(client, now=now)
    assert resolution["trade_date"] == TRADE_DATE
    assert resolution["formal_latest"] == PREV_DAY
    assert resolution["calendar_recent"][-1] == TRADE_DATE


def test_resolve_trade_date_before_1610_falls_back_to_previous_session():
    client = FakeCHClient(calendar_sessions=[PREV_DAY, TRADE_DATE], formal_latest=PREV_DAY)
    now = datetime(2026, 11, 2, 15, 0, tzinfo=BEIJING)          # 当日包未发布
    resolution = pipeline.resolve_latest_completed_trade_date(client, now=now)
    assert resolution["trade_date"] == PREV_DAY


def test_resolve_trade_date_weekend_jumps_to_friday():
    client = FakeCHClient(calendar_sessions=["2026-10-29", PREV_DAY], formal_latest=PREV_DAY)
    now = datetime(2026, 11, 1, 18, 0, tzinfo=BEIJING)          # 周日
    resolution = pipeline.resolve_latest_completed_trade_date(client, now=now)
    assert resolution["trade_date"] == PREV_DAY


def test_resolve_trade_date_fails_when_calendar_empty():
    client = FakeCHClient(calendar_sessions=[], formal_latest=PREV_DAY)
    with pytest.raises(RuntimeError, match="trade-date|交易日"):
        pipeline.resolve_latest_completed_trade_date(client, now=datetime(2026, 11, 2, 17, 0, tzinfo=BEIJING))


def test_resolve_trade_date_fails_when_formal_ahead_of_calendar():
    client = FakeCHClient(calendar_sessions=[PREV_DAY], formal_latest=TRADE_DATE)
    with pytest.raises(RuntimeError, match="正式表|formal"):
        pipeline.resolve_latest_completed_trade_date(client, now=datetime(2026, 11, 2, 17, 0, tzinfo=BEIJING))


def test_resolve_queries_are_readonly_selects():
    client = FakeCHClient(calendar_sessions=[PREV_DAY, TRADE_DATE], formal_latest=PREV_DAY)
    pipeline.resolve_latest_completed_trade_date(client, now=datetime(2026, 11, 2, 17, 0, tzinfo=BEIJING))
    assert client.queries
    assert not client.non_select_queries
    assert not client.non_readonly_queries


# ── 门禁：各板块行数与前一交易日对比 ─────────────────────────────────
def test_gate_row_count_vs_prev_day_pass_and_lists_new_disappeared():
    rows = [_contract_row(code) for code in KEPT_CODES[:4]] + \
           [_contract_row("601899.SH"), _contract_row("920099.BJ")]   # 新增两个
    gate = pipeline.check_row_count_vs_prev_day(
        rows, prev_codes=KEPT_CODES, prev_date=PREV_DAY, target_date=TRADE_DATE,
        stocks_by_code={})
    assert gate["status"] == "pass"
    assert gate["counts"]["sh"] == 3 and gate["counts"]["bj"] == 1
    assert gate["prev_counts"]["sh"] == 2 and gate["prev_counts"]["bj"] == 2
    assert gate["new_codes"] == [{"code": "601899.SH", "explanation": "unknown"},
                                 {"code": "920099.BJ", "explanation": "unknown"}]
    assert gate["disappeared_codes"] == [{"code": "920000.BJ", "explanation": "unknown"},
                                         {"code": "920982.BJ", "explanation": "unknown"}]


def test_gate_row_count_explains_new_listing_and_delisting_from_stocks_master():
    rows = [_contract_row(code) for code in KEPT_CODES] + [_contract_row("301999.SZ")]
    stocks = {"301999.SZ": {"list_date": TRADE_DATE, "delist_date": None, "quit": 0},
              "920982.BJ": {"list_date": "2024-01-01", "delist_date": TRADE_DATE, "quit": 1},
              "920000.BJ": {"list_date": "2024-01-01", "delist_date": None, "quit": 0}}
    gate = pipeline.check_row_count_vs_prev_day(
        rows, prev_codes=KEPT_CODES + ["919999.BJ"], prev_date=PREV_DAY, target_date=TRADE_DATE,
        stocks_by_code=stocks)
    assert gate["status"] == "pass"
    assert gate["new_codes"] == [{"code": "301999.SZ", "explanation": "new_listing"}]
    disappeared = {item["code"]: item["explanation"] for item in gate["disappeared_codes"]}
    assert disappeared == {"919999.BJ": "unknown"}      # 主表查不到 → unknown


def test_gate_row_count_fails_on_threshold_breach():
    rows = [_contract_row(code) for code in KEPT_CODES]           # sh 2 个
    prev_codes = [f"60{i:04d}.SH" for i in range(600)]            # sh 600 个 → 远超阈值
    gate = pipeline.check_row_count_vs_prev_day(
        rows, prev_codes=prev_codes, prev_date=PREV_DAY, target_date=TRADE_DATE, stocks_by_code={})
    assert gate["status"] == "fail"
    assert any(v["market"] == "sh" for v in gate["violations"])


def test_gate_row_count_fails_when_prev_day_formal_empty():
    gate = pipeline.check_row_count_vs_prev_day(
        [_contract_row("600000.SH")], prev_codes=[], prev_date=PREV_DAY,
        target_date=TRADE_DATE, stocks_by_code={})
    assert gate["status"] == "fail"
    assert "前一交易日" in gate["reason"]


# ── 门禁：与正式表已有重叠键值一致性（价 1% / 量额 3%） ─────────────
def test_gate_formal_overlap_pass_within_tolerances():
    rows = [_contract_row("600000.SH", close=10.2, volume=1000.0, amount=100000.0),
            _contract_row("000001.SZ")]
    formal = [{"code": "600000.SH", "open": 10.01, "high": 10.5, "low": 9.85, "close": 10.3,
               "volume": 1010.0, "amount": 102900.0}]           # close 1% 内 / 量额 3% 内
    gate = pipeline.check_formal_overlap(rows, formal_rows=formal)
    assert gate["status"] == "pass"
    assert gate["overlap_count"] == 1
    assert gate["mismatches"] == []


def test_gate_formal_overlap_price_and_quantity_mismatch_fail():
    rows = [_contract_row("600000.SH", close=10.2, volume=1000.0, amount=100000.0),
            _contract_row("688981.SH", open=100.0, high=101.0, low=99.0, close=100.5,
                          volume=5000.0, amount=50000000.0)]
    formal = [
        {"code": "600000.SH", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
         "volume": 1500.0, "amount": 100000.0},                # volume 超 3%
        {"code": "688981.SH", "open": 100.0, "high": 101.0, "low": 99.0, "close": 95.0,
         "volume": 5000.0, "amount": 50000000.0},              # close 超 1%
    ]
    gate = pipeline.check_formal_overlap(rows, formal_rows=formal)
    assert gate["status"] == "fail"
    fields = {(m["code"], m["field"]) for m in gate["mismatches"]}
    assert ("600000.SH", "volume") in fields
    assert ("688981.SH", "close") in fields
    assert gate["overlap_count"] == 2


def test_gate_formal_overlap_empty_is_pass():
    gate = pipeline.check_formal_overlap([_contract_row("600000.SH")], formal_rows=[])
    assert gate["status"] == "pass"
    assert gate["overlap_count"] == 0


# ── 管道级：省略 --trade-date → 只读解析 + DB 门禁执行 ───────────────
def test_run_with_omitted_trade_date_engages_readonly_db_gates(tmp_path):
    options, client = _db_options(tmp_path)
    result = pipeline.run_pipeline(options)

    assert result["exit_code"] == 0
    report = result["report"]
    assert report["db_access"] is True
    assert report["trade_date"] == TRADE_DATE
    assert report["trade_date_resolution"]["formal_latest"] == PREV_DAY
    assert report["gates"]["row_count_vs_prev_day"]["status"] == "pass"
    assert report["gates"]["formal_overlap_consistency"]["status"] == "pass"
    assert not client.non_select_queries
    assert not client.non_readonly_queries
    assert not client.commands and not client.inserts        # 只读模式无任何写


def test_run_with_prev_day_count_breach_fails_without_stage(tmp_path):
    prev_codes = KEPT_CODES + [f"60{i:04d}.SH" for i in range(700)]
    options, client = _db_options(tmp_path, prev_day_codes=prev_codes)
    result = pipeline.run_pipeline(options)
    assert result["exit_code"] == 1
    assert result["report"]["gates"]["row_count_vs_prev_day"]["status"] == "fail"
    assert not client.commands and not client.inserts


def test_run_with_overlap_mismatch_fails(tmp_path):
    overlap = [{"code": "600000.SH", "open": 10.0, "high": 10.5, "low": 9.8, "close": 8.0,
                "volume": 1234.5, "amount": 1234567.8}]        # close 与包内 9.62 差 ~17%
    options, client = _db_options(tmp_path, overlap_rows=overlap)
    result = pipeline.run_pipeline(options)
    assert result["exit_code"] == 1
    assert result["report"]["gates"]["formal_overlap_consistency"]["status"] == "fail"


# ── 腾讯交叉样本验证 ─────────────────────────────────────────────────
PKG_CLOSE = {"600000.SH": 9.62, "688981.SH": 100.09, "000001.SZ": 11.30, "300001.SZ": 22.32}
PKG_VOLUME = {"600000.SH": 625298.69, "688981.SH": 357905.12,
              "000001.SZ": 1297648.75, "300001.SZ": 81234.0}


def _matching_crosscheck_fetch(code, trade_date):
    return {"code": code, "date": trade_date, "close": PKG_CLOSE[code],
            "volume": PKG_VOLUME[code], "volume_unit": "lots"}


def _selectable_rows():
    return [_contract_row(code, close=PKG_CLOSE[code], volume=PKG_VOLUME[code]) for code in PKG_CLOSE]


def test_select_crosscheck_codes_excludes_bj_and_is_deterministic():
    rows = _selectable_rows() + [_contract_row("920982.BJ"), _contract_row("920000.BJ")]
    first = pipeline.select_crosscheck_codes(rows, 3, TRADE_DATE)
    second = pipeline.select_crosscheck_codes(rows, 3, TRADE_DATE)
    assert first == second
    assert len(first) == 3
    assert set(first) <= set(PKG_CLOSE)                 # 北交所绝不入样
    assert pipeline.select_crosscheck_codes(rows, 99, TRADE_DATE) == sorted(PKG_CLOSE)


def test_select_crosscheck_codes_empty_when_no_sh_sz():
    rows = [_contract_row("920982.BJ")]
    assert pipeline.select_crosscheck_codes(rows, 5, TRADE_DATE) == []


def test_gate_tencent_crosscheck_match_pass():
    gate = pipeline.check_tencent_crosscheck(
        _selectable_rows(), ["600000.SH", "000001.SZ"], _matching_crosscheck_fetch, TRADE_DATE)
    assert gate["status"] == "pass"
    assert gate["sample_size"] == 2
    assert gate["mismatches"] == []
    assert all(r["status"] == "match" for r in gate["results"])


def test_gate_tencent_crosscheck_mismatch_and_no_row_fail():
    def fetch(code, trade_date):
        if code == "600000.SH":
            return {"close": PKG_CLOSE[code] * 1.10, "volume": PKG_VOLUME[code]}   # close 超 1%
        if code == "688981.SH":
            return {"close": PKG_CLOSE[code], "volume": PKG_VOLUME[code] * 1.50}   # volume 超 3%
        return None                                                                # 当日无行
    gate = pipeline.check_tencent_crosscheck(
        _selectable_rows(), ["600000.SH", "688981.SH", "000001.SZ"], fetch, TRADE_DATE)
    assert gate["status"] == "fail"
    fields = {(m["code"], m["field"]) for m in gate["mismatches"]}
    assert ("600000.SH", "close") in fields
    assert ("688981.SH", "volume") in fields
    statuses = {r["code"]: r["status"] for r in gate["results"]}
    assert statuses["000001.SZ"] == "no_row"


def test_gate_tencent_crosscheck_all_errors_fail():
    def fetch(code, trade_date):
        raise RuntimeError("三个入口均不可用")
    gate = pipeline.check_tencent_crosscheck(
        _selectable_rows(), ["600000.SH", "000001.SZ"], fetch, TRADE_DATE)
    assert gate["status"] == "fail"
    assert gate["error_count"] == 2


def test_pipeline_crosscheck_in_default_zero_db_mode(tmp_path):
    options = _base_options(output=tmp_path / "r.json")
    options["crosscheck_sample"] = 2
    options["crosscheck_fetch"] = _matching_crosscheck_fetch
    result = pipeline.run_pipeline(options)
    assert result["exit_code"] == 0
    report = result["report"]
    assert report["db_access"] is False                  # 交叉验证走网络，不碰 DB
    assert report["gates"]["tencent_crosscheck"]["status"] == "pass"
    assert report["crosscheck"]["sample_size"] == 2


def test_pipeline_crosscheck_mismatch_fails_pipeline(tmp_path):
    def bad_fetch(code, trade_date):
        return {"close": PKG_CLOSE[code] * 1.20, "volume": PKG_VOLUME[code]}
    options = _base_options(output=tmp_path / "r.json")
    options["crosscheck_sample"] = 2
    options["crosscheck_fetch"] = bad_fetch
    result = pipeline.run_pipeline(options)
    assert result["exit_code"] == 1
    assert result["report"]["gates"]["tencent_crosscheck"]["status"] == "fail"
    assert any(r["gate"] == "tencent_crosscheck" for r in result["report"]["rejections"])


# ── --stage-write：白名单表 + 读回验证 + 回滚 ────────────────────────
def _stage_options(tmp_path, **client_overrides):
    kwargs = dict(
        calendar_sessions=["2026-10-29", PREV_DAY, TRADE_DATE],
        formal_latest=PREV_DAY,
        prev_day_codes=KEPT_CODES,
    )
    kwargs.update(client_overrides)
    client = FakeCHClient(**kwargs)
    options = _base_options(output=tmp_path / "r.json")   # 显式 trade-date；stage_write 接入 DB
    options["stage_write"] = True
    options["client_factory"] = lambda: client
    return options, client


def test_stage_rows_for_insert_carries_lineage_columns():
    row = {"code": "600000.SH", "market": "sh", "date": TRADE_DATE, "name": "浦发银行",
           "prev_close": 9.61, "open": 9.65, "high": 9.66, "low": 9.60, "close": 9.62,
           "volume": 625298.69, "amount": 602885524.0}
    lineage = {"url": "https://example/20261102.zip", "zip_sha256": "ab" * 32,
               "fetched_at": "2026-11-02T08:30:00+00:00"}
    stage_rows = pipeline._stage_rows_for_insert([row], TRADE_DATE, lineage)
    assert len(stage_rows) == 1
    values = dict(zip(pipeline.STAGE_COLUMNS, stage_rows[0]))
    assert values["code"] == "600000.SH"
    assert values["source_url"] == lineage["url"]
    assert values["zip_sha256"] == lineage["zip_sha256"]
    assert values["pipeline_version"] == pipeline.PIPELINE_VERSION
    assert values["volume"] == 625298.69                    # 手
    assert values["amount"] == 602885524.0                  # 元
    assert values["market"] == "sh"


def test_stage_write_happy_path_creates_whitelist_table_inserts_and_verifies(tmp_path):
    options, client = _stage_options(tmp_path)
    result = pipeline.run_pipeline(options)

    assert result["exit_code"] == 0
    report = result["report"]
    assert report["status"] == "passed"
    assert report["db_access"] is True
    stage = report["stage_write"]
    assert stage["performed"] is True
    assert stage["table"] == "stock.kline_daily_pkg_stage_20261101"
    assert stage["inserted_rows"] == 7                          # R17：6 股票 + 399001.SZ
    assert stage["readback"]["row_count"] == 7
    assert stage["readback"]["digest_match"] is True

    create_sqls = [sql for sql, _ in client.commands if sql.lstrip().upper().startswith("CREATE")]
    assert len(create_sqls) == 1
    assert "stock.kline_daily_pkg_stage_20261101" in create_sqls[0]
    assert "IF NOT EXISTS" in create_sqls[0].upper()
    for column in ("source_url", "zip_sha256", "fetched_at", "pipeline_version"):
        assert column in create_sqls[0]

    assert len(client.inserts) == 1
    table, values, columns = client.inserts[0]
    assert table == "stock.kline_daily_pkg_stage_20261101"
    assert tuple(columns) == tuple(pipeline.STAGE_COLUMNS)
    assert len(values) == 7                                      # R17：含清单内指数 399001.SZ
    # 一切写操作只指向白名单表；正式表绝无写入
    written_tables = {sql.split()[-1].rstrip(" (") for sql, _ in client.commands
                      if not sql.lstrip().upper().startswith("CREATE")} | {table}
    assert written_tables == {"stock.kline_daily_pkg_stage_20261101"}


def test_stage_write_rolls_back_on_readback_mismatch(tmp_path):
    options, client = _stage_options(tmp_path, stage_corrupt=True)
    result = pipeline.run_pipeline(options)

    assert result["exit_code"] == 1
    report = result["report"]
    assert report["status"] == "failed"
    stage = report["stage_write"]
    assert stage["performed"] is False
    assert stage["rolled_back"] is True
    assert stage["readback"]["digest_match"] is False
    # 回滚：ALTER ... DELETE 只针对白名单表该日行，且删除后 0 行
    delete_sqls = [sql for sql, _ in client.commands
                   if sql.lstrip().upper().startswith("ALTER")]
    assert len(delete_sqls) == 1
    assert "stock.kline_daily_pkg_stage_20261101" in delete_sqls[0]
    assert TRADE_DATE in delete_sqls[0]
    assert client.stage_count_for(TRADE_DATE) == 0


def test_stage_write_blocked_when_any_gate_failed(tmp_path):
    bad = ("600519", "贵州茅台", 1500.0, 1502.0, 1510.0, 1520.0, 1505.0, 123400, 186000000.0)
    markets = {"sh": [ROW_600000, bad], "sz": [ROW_000001], "bj": [ROW_920982]}
    options, client = _stage_options(tmp_path)
    options["fetch_package"] = _fetch_pkg(make_zip(TRADE_DATE_COMPACT, markets))
    result = pipeline.run_pipeline(options)

    assert result["exit_code"] == 1
    assert result["report"]["gates"]["ohlc_envelope"]["status"] == "fail"
    stage = result["report"]["stage_write"]
    assert stage["performed"] is False
    assert not client.commands and not client.inserts       # 门禁未过：连建表都不允许


def test_stage_write_fail_closed_when_date_already_staged(tmp_path):
    options, client = _stage_options(tmp_path)
    client.stage_rows[("2026-11-02", "600000.SH")] = {          # 预置同日残留行
        "code": "600000.SH", "trade_date": TRADE_DATE, "open": 1.0, "high": 1.0,
        "low": 1.0, "close": 1.0, "volume": 1.0, "amount": 1.0, "prev_close": 1.0,
        "name": "x", "market": "sh", "source_url": "u", "zip_sha256": "z",
        "fetched_at": "2026-11-02 08:00:00", "pipeline_version": "old"}
    result = pipeline.run_pipeline(options)

    assert result["exit_code"] != 0
    stage = result["report"]["stage_write"]
    assert stage["performed"] is False
    assert client.inserts == []                                # 不覆盖已有 stage 行
    assert client.stage_count_for(TRADE_DATE) == 1             # 原有行不受影响


# ── main() / CLI 集成 ────────────────────────────────────────────────
def test_main_offline_happy_path_with_injections(tmp_path, capsys):
    output = tmp_path / "main_report.json"
    code = pipeline.main(["--trade-date", TRADE_DATE, "--crosscheck-sample", "0",
                          "--output", str(output)],
                         injections={"fetch_package": _fetch_pkg(), "min_priced": {},
                                     "client_factory": _forbidden_client_factory(),
                                     "now": datetime(2026, 11, 2, 17, 0, tzinfo=BEIJING)})
    assert code == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "passed"
    assert report["db_access"] is False
    assert report["stage_write"]["performed"] is False
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "passed"


def test_main_requires_explicit_trade_date_without_db(tmp_path):
    output = tmp_path / "err.json"

    def no_db():
        raise RuntimeError("数据库不可达")

    code = pipeline.main(["--output", str(output)], injections={"client_factory": no_db})
    assert code == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "error"
    assert "数据库不可达" in report["error"]


def test_main_rejects_malformed_trade_date(tmp_path):
    code = pipeline.main(["--trade-date", "2026/11/02", "--output", str(tmp_path / "e.json")],
                         injections={"fetch_package": _fetch_pkg(), "min_priced": {}})
    assert code == 2
    report = json.loads((tmp_path / "e.json").read_text(encoding="utf-8"))
    assert report["status"] == "error"
    assert "trade-date" in report["error"] or "交易日" in report["error"]


def test_parse_args_accepts_compact_date_and_stage_write_flag():
    args = pipeline.parse_args(["--trade-date", "20261102", "--stage-write",
                                "--crosscheck-sample", "0"])
    assert args.trade_date == "20261102"
    assert args.stage_write is True
    assert args.crosscheck_sample == 0


# ── 冻结合成包夹具（tests/fixtures/daily_pipeline/） ─────────────────
def test_frozen_fixture_package_golden_summary(tmp_path):
    content = (FIXTURES / "tdx_pkg_synthetic_20261102.zip").read_bytes()
    output = tmp_path / "fixture_report.json"
    result = pipeline.run_pipeline(_base_options(output=output, fetch_package=_fetch_pkg(content)))

    assert result["exit_code"] == 0
    report = result["report"]
    summary = json.loads((FIXTURES / "tdx_pkg_synthetic_20261102_expected.json")
                         .read_text(encoding="utf-8"))
    assert report["lineage"]["zip_sha256"] == summary["zip_sha256"] == hashlib.sha256(content).hexdigest()
    assert report["row_counts"]["total_rows"] == summary["total_rows"]
    assert report["row_counts"]["kept"] == summary["kept"]
    assert report["row_counts"]["kept_by_market"] == summary["kept_by_market"]
    assert report["row_counts"]["excluded_by_category"] == summary["excluded_by_category"]
    assert report["row_counts"]["stock_zero_volume"] == summary["stock_zero_volume"]
    # R17：冻结指数纳入报告（夹具含 399001.SZ；999999.SH 仍排除在 excluded 计数里）
    inclusion = report["row_counts"]["index_inclusion"]
    assert inclusion["kept"] == 1
    assert inclusion["kept_codes"] == ["399001.SZ"]
    assert inclusion["missing_codes"] == sorted(FROZEN_TEN - {"399001.SZ"})


# ══════════════════════════════════════════════════════════════════════
# --formal-write：正式表 stock.kline_daily 追加写路径（严格 TDD，先于实现）
# ══════════════════════════════════════════════════════════════════════
FORMAL_TABLE = "stock.kline_daily"
LOCK_SCOPE = "kline_daily_daily_append"
LOCK_TABLE = f"stock.write_lock_{LOCK_SCOPE}"

# ClickHouse 文档哈希函数页的金样本：users 表 groupBitXor(cityHash64(*)) 期望值
CLICKHOUSE_DOCS_GROUP_XOR = 11639977218258521182


def _formal_options(tmp_path, **client_overrides):
    kwargs = dict(
        calendar_sessions=["2026-10-29", PREV_DAY, TRADE_DATE],
        formal_latest=PREV_DAY,
        prev_day_codes=KEPT_CODES,
    )
    kwargs.update(client_overrides)
    client = FakeCHClient(**kwargs)
    options = _base_options(output=tmp_path / "r.json")   # 显式 trade-date；formal_write 接入 DB
    options["formal_write"] = True
    options["client_factory"] = lambda: client
    return options, client


# ── cityHash64 纯 Python 实现（与 ClickHouse 24.8 语义一致） ─────────
def test_cityhash64_python_port_matches_clickhouse_docs_golden_vector():
    # 金样本：CREATE TABLE users(id UInt32, name String, age UInt8, city String);
    # 三行 (1,'Alice',25,'New York'),(2,'Bob',30,'London'),(3,'Charlie',35,'Tokyo');
    # SELECT groupBitXor(cityHash64(*)) FROM users = 11639977218258521182
    rows = [(1, "Alice", 25, "New York"), (2, "Bob", 30, "London"), (3, "Charlie", 35, "Tokyo")]
    acc = 0
    for uid, name, age, city in rows:
        running = pipeline.clickhouse_int_hash64(uid)                    # 首参：POD → intHash64
        running = pipeline.hash128to64(running, pipeline.city_hash64(name.encode("utf-8")))
        running = pipeline.hash128to64(running, pipeline.clickhouse_int_hash64(age))
        running = pipeline.hash128to64(running, pipeline.city_hash64(city.encode("utf-8")))
        acc ^= running
    assert acc == CLICKHOUSE_DOCS_GROUP_XOR


def test_formal_row_id_deterministic_and_pinned_vector():
    first = pipeline.formal_row_id("600000.SH", TRADE_DATE)
    assert first == pipeline.formal_row_id("600000.SH", "20261102")      # 确定性
    assert isinstance(first, int) and 0 <= first < 2 ** 64
    assert first != pipeline.formal_row_id("600000.SH", PREV_DAY)        # 不同日期不同 id
    assert first != pipeline.formal_row_id("000001.SZ", TRADE_DATE)      # 不同代码不同 id


def test_formal_columns_are_contract_columns_without_lineage():
    assert pipeline.FORMAL_COLUMNS == (
        "code", "trade_date", "open", "high", "low", "close", "volume", "amount",
        "amplitude", "change_pct", "change_amount", "turnover_rate", "created_at", "id")
    for lineage_col in ("source_url", "zip_sha256", "fetched_at", "pipeline_version",
                        "prev_close", "name", "market"):
        assert lineage_col not in pipeline.FORMAL_COLUMNS


def test_assert_formal_table_allowed_whitelist_is_kline_daily_only():
    pipeline.assert_formal_table_allowed("stock.kline_daily")
    for bad in ("stock.kline_daily_pkg_stage_20261101", "stock.kline_daily__migration_candidate",
                "other.kline_daily", ""):
        with pytest.raises(ValueError, match="白名单|whitelist|正式表"):
            pipeline.assert_formal_table_allowed(bad)


# ── CLI：--formal-write 与 --stage-write 互斥 ────────────────────────
def test_parse_args_accepts_formal_write_flag():
    args = pipeline.parse_args(["--trade-date", "20261102", "--formal-write",
                                "--crosscheck-sample", "0"])
    assert args.formal_write is True
    assert args.stage_write is False


def test_parse_args_rejects_formal_write_with_stage_write(capsys):
    with pytest.raises(SystemExit) as excinfo:
        pipeline.parse_args(["--trade-date", "20261102", "--stage-write", "--formal-write"])
    assert excinfo.value.code == 2
    assert "互斥" in capsys.readouterr().err


# ── 插入行构造：id / created_at / 派生指标 ───────────────────────────
def test_formal_rows_for_insert_id_created_at_and_metrics():
    rows = [
        {"code": "600000.SH", "date": TRADE_DATE, "open": 9.65, "high": 9.66, "low": 9.60,
         "close": 9.62, "volume": 625298.69, "amount": 602885524.0, "prev_close": 9.61},
        {"code": "000001.SZ", "date": TRADE_DATE, "open": 11.41, "high": 11.42, "low": 11.26,
         "close": 11.30, "volume": 1297648.75, "amount": 1468424722.09, "prev_close": 0.0},
    ]
    batch_ts = datetime(2026, 11, 2, 17, 5, 0, tzinfo=timezone.utc)
    assert batch_ts.utcoffset() == timedelta(0)                    # marker 必须 UTC-aware
    tuples_ = pipeline._formal_rows_for_insert(rows, TRADE_DATE, batch_ts)
    assert len(tuples_) == 2
    as_dicts = [dict(zip(pipeline.FORMAL_COLUMNS, item)) for item in tuples_]
    for item in as_dicts:
        assert item["created_at"] == batch_ts                       # 同批同行同值（回滚 marker）
        assert item["id"] == pipeline.formal_row_id(item["code"], TRADE_DATE)
    first = as_dicts[0]
    assert first["amplitude"] == pytest.approx((9.66 - 9.60) / 9.61 * 100.0)
    assert first["change_pct"] == pytest.approx((9.62 - 9.61) / 9.61 * 100.0)
    assert first["change_amount"] == pytest.approx(9.62 - 9.61)
    assert first["turnover_rate"] == 0.0                            # 盘后包无流通股本 → 0
    second = as_dicts[1]
    assert second["amplitude"] == 0.0 and second["change_pct"] == 0.0    # 无 prev_close → 0
    assert second["change_amount"] == 0.0


# ── journal 路径 ─────────────────────────────────────────────────────
def test_default_formal_journal_path_uses_runtime_daily_pipeline_dir():
    path = pipeline.default_formal_journal_path(TRADE_DATE, data_root=Path("F:/AiStock/data/data"))
    assert path == Path("F:/AiStock/data/data/runtime/daily_kline_coverage/daily_pipeline/"
                        "2026-11-02/formal_write_journal.jsonl")


# ── 主流程：反连接只插缺失键 + 锁 + 读回验证 ────────────────────────
def test_formal_write_happy_path_inserts_only_missing_keys(tmp_path):
    overlap = [{"code": "600000.SH", "open": 9.65, "high": 9.66, "low": 9.60, "close": 9.62,
                "volume": 625298.69, "amount": 602885524.0}]          # 该日已有 1 个重叠键
    options, client = _formal_options(tmp_path, overlap_rows=overlap)
    result = pipeline.run_pipeline(options)

    assert result["exit_code"] == 0
    report = result["report"]
    assert report["status"] == "passed"
    formal = report["formal_write"]
    assert formal["performed"] is True
    assert formal["table"] == FORMAL_TABLE
    assert formal["inserted_rows"] == 6                               # R17：7 键 - 1 重叠
    assert formal["overlap_key_count"] == 1
    assert formal["readback"]["row_count"] == 7                       # 该日行数 = 缺失 6 + 已有 1
    assert formal["readback"]["expected_row_count"] == 7
    assert formal["readback"]["row_count_ok"] is True
    assert formal["readback"]["digest_match"] is True
    assert formal["readback"]["bad_id_rows"] == 0

    # 插入元组：正式契约列、无血缘列、缺失键不含重叠键
    assert len(client.inserts) == 1
    table, values, columns = client.inserts[0]
    assert table == FORMAL_TABLE
    assert tuple(columns) == tuple(pipeline.FORMAL_COLUMNS)
    assert len(values) == 6
    inserted_codes = {dict(zip(columns, v))["code"] for v in values}
    assert "600000.SH" not in inserted_codes
    assert inserted_codes == (set(KEPT_CODES) | {"399001.SZ"}) - {"600000.SH"}   # R17：含清单内指数

    # 写操作只指向 白名单正式表 + 锁标记表；读回后读侧 id/created_at 语义见下述专项
    written_tables = {sql.split()[2] for sql, _ in client.commands
                      if sql.lstrip().upper().startswith(("CREATE", "INSERT", "DROP"))} \
        | {table}
    assert written_tables <= {FORMAL_TABLE, LOCK_TABLE}


def test_formal_write_insert_rows_carry_batch_created_at_and_ids(tmp_path):
    options, client = _formal_options(tmp_path)
    result = pipeline.run_pipeline(options)
    assert result["exit_code"] == 0
    formal = result["report"]["formal_write"]
    batch_ts = formal["batch_created_at"]
    _table, values, columns = client.inserts[0]
    stamps, ids = set(), set()
    for row in values:
        item = dict(zip(columns, row))
        assert item["created_at"].utcoffset() == timedelta(0)       # 防 naive 时区漂移
        stamps.add(str(item["created_at"])[:19])
        ids.add(item["id"] == pipeline.formal_row_id(item["code"], TRADE_DATE))
    assert len(stamps) == 1 and list(stamps)[0] == batch_ts           # 同批同行同值
    assert ids == {True}                                              # id = cityHash64(code, trade_date)


def test_formal_write_lock_acquired_before_anti_join_and_released(tmp_path):
    overlap = [{"code": "600000.SH", "open": 9.65, "high": 9.66, "low": 9.60, "close": 9.62,
                "volume": 625298.69, "amount": 602885524.0}]
    options, client = _formal_options(tmp_path, overlap_rows=overlap)
    result = pipeline.run_pipeline(options)
    assert result["exit_code"] == 0

    kinds = [(kind, text) for kind, text in client.event_log]
    lock_create = next(i for i, (k, t) in enumerate(kinds)
                       if k == "command" and t.upper().startswith("CREATE") and "write_lock_" in t)
    anti_join_reads = [i for i, (k, t) in enumerate(kinds)
                       if k == "query" and "ORDER BY code" in t and "stock.kline_daily" in t]
    assert len(anti_join_reads) >= 3                                 # 门禁 1 + 反连接 1 + digest 1
    assert anti_join_reads[1] > lock_create                          # 锁内重新执行反连接
    probe_reads = [i for i, (k, t) in enumerate(kinds)
                   if k == "query" and "cityHash64(" in t]
    assert probe_reads and all(i > lock_create for i in probe_reads) # 探针也在锁内
    lock_drop = [i for i, (k, t) in enumerate(kinds)
                 if k == "command" and t.upper().startswith("DROP") and "write_lock_" in t]
    assert lock_drop and lock_drop[0] > anti_join_reads[-1]          # 读完才释放锁
    assert client.lock_tables == set()                               # 已释放


def test_formal_write_lineage_reported_not_written(tmp_path):
    options, client = _formal_options(tmp_path)
    result = pipeline.run_pipeline(options)
    formal = result["report"]["formal_write"]
    assert formal["lineage"]["source_url"] == result["report"]["lineage"]["url"]
    assert formal["lineage"]["zip_sha256"] == result["report"]["lineage"]["zip_sha256"]
    _table, _values, columns = client.inserts[0]
    assert "source_url" not in columns and "zip_sha256" not in columns


def test_formal_write_journal_is_atomic_jsonl(tmp_path):
    options, client = _formal_options(tmp_path)
    result = pipeline.run_pipeline(options)
    assert result["exit_code"] == 0
    journal_path = tmp_path / "formal_write_journal.jsonl"
    assert journal_path.is_file()
    lines = journal_path.read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line) for line in lines]
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)   # seq 递增
    assert len({e["seq"] for e in events}) == len(events)
    names = [e["event"] for e in events]
    for expected in ("formal_write_start", "formal_write_lock_acquired",
                     "formal_anti_join", "formal_insert", "formal_readback_ok",
                     "formal_write_lock_released"):
        assert expected in names, expected
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())      # 段文件已清理


# ── fail-closed：门禁失败 / 重叠不一致 / 读回不符回滚 / 锁占用 / 探针 ──
def test_formal_write_blocked_when_any_gate_failed(tmp_path):
    bad = ("600519", "贵州茅台", 1500.0, 1502.0, 1510.0, 1520.0, 1505.0, 123400, 186000000.0)
    markets = {"sh": [ROW_600000, bad], "sz": [ROW_000001], "bj": [ROW_920982]}
    options, client = _formal_options(tmp_path)
    options["fetch_package"] = _fetch_pkg(make_zip(TRADE_DATE_COMPACT, markets))
    result = pipeline.run_pipeline(options)

    assert result["exit_code"] == 1
    formal = result["report"]["formal_write"]
    assert formal["performed"] is False
    assert "门禁" in formal["reason"]
    assert client.inserts == []                                     # 连锁都不拿
    assert client.lock_tables == set()
    assert not any("write_lock_" in sql for sql, _ in client.commands)


def test_formal_write_fail_closed_on_inlock_overlap_mismatch(tmp_path):
    # 门禁时无重叠 → 通过；锁内反连接时并发写入了一个 close 严重不符的重叠键 → fail-closed
    options, client = _formal_options(tmp_path, anti_join_inject=[
        {"code": "688981.SH", "open": 101.0, "high": 101.44, "low": 98.4,
         "close": 66.66, "volume": 357905.12, "amount": 3577411258.0}])
    result = pipeline.run_pipeline(options)

    assert result["exit_code"] == 1
    formal = result["report"]["formal_write"]
    assert formal["performed"] is False
    assert formal["rolled_back"] is False
    assert "重叠" in formal["reason"] or "不一致" in formal["reason"]
    assert formal["overlap_check"]["status"] == "fail"
    assert client.inserts == []                                     # 报错不写
    assert client.lock_tables == set()                              # 锁照常释放


def test_formal_write_rolls_back_on_readback_mismatch(tmp_path):
    options, client = _formal_options(tmp_path, formal_corrupt=True)
    result = pipeline.run_pipeline(options)

    assert result["exit_code"] == 1
    report = result["report"]
    formal = report["formal_write"]
    assert formal["performed"] is False
    assert formal["rolled_back"] is True
    assert formal["readback"]["digest_match"] is False
    assert "formal_write" in report["failed_gates"]

    rollback_sqls = [sql for sql, _settings in client.commands
                     if sql.lstrip().upper().startswith("ALTER") and "stock.kline_daily" in sql]
    assert len(rollback_sqls) == 1
    sql = rollback_sqls[0]
    settings = next(st for s, st in client.commands if s == sql)
    assert f"toDate('{TRADE_DATE}')" in sql                          # 只删该日
    assert "id IN (" in sql                                          # 按本批次精确 id 删除
    assert settings.get("mutations_sync") == 2                       # 同步等待 mutation
    assert client.formal_rows == {}                                  # 删净校验通过
    assert formal["rollback"]["remaining_rows"] == 0


def test_formal_write_rejected_when_lock_held(tmp_path):
    options, client = _formal_options(tmp_path, lock_held=True)
    result = pipeline.run_pipeline(options)

    assert result["exit_code"] == 1
    formal = result["report"]["formal_write"]
    assert formal["performed"] is False
    assert formal["rolled_back"] is False
    assert "锁" in formal["reason"]
    assert formal["lock"]["held"] is True
    assert client.inserts == []                                     # 不写
    assert not any(sql.lstrip().upper().startswith("DROP") for sql, _ in client.commands)
    assert not any(sql.lstrip().upper().startswith("ALTER") for sql, _ in client.commands)


def test_formal_write_probe_mismatch_blocks_insert(tmp_path):
    # ClickHouse 侧 cityHash64 探针值与本实现不符 → fail-closed 不写
    options, client = _formal_options(tmp_path, probe_answers={"600000.SH": 12345})
    result = pipeline.run_pipeline(options)

    assert result["exit_code"] == 1
    formal = result["report"]["formal_write"]
    assert formal["performed"] is False
    assert "cityHash64" in formal["reason"]
    assert client.inserts == []
    assert client.lock_tables == set()                               # 锁释放


def test_formal_write_id_readback_mismatch_triggers_rollback(tmp_path):
    options, client = _formal_options(tmp_path, formal_id_wrong=True)
    result = pipeline.run_pipeline(options)

    assert result["exit_code"] == 1
    formal = result["report"]["formal_write"]
    assert formal["performed"] is False
    assert formal["rolled_back"] is True
    assert formal["readback"]["bad_id_rows"] > 0
    assert client.formal_rows == {}


def test_main_passes_formal_write_flag_and_writes_report(tmp_path, capsys):
    client_holder = {}

    def factory():
        client_holder["client"] = FakeCHClient(
            calendar_sessions=["2026-10-29", PREV_DAY, TRADE_DATE],
            formal_latest=PREV_DAY, prev_day_codes=KEPT_CODES)
        return client_holder["client"]

    output = tmp_path / "main_formal.json"
    code = pipeline.main(["--trade-date", TRADE_DATE, "--formal-write",
                          "--crosscheck-sample", "0", "--output", str(output)],
                         injections={"fetch_package": _fetch_pkg(), "min_priced": {},
                                     "client_factory": factory,
                                     "now": datetime(2026, 11, 2, 17, 0, tzinfo=BEIJING)})
    assert code == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["formal_write"]["performed"] is True
    assert json.loads(capsys.readouterr().out)["formal_write"]["performed"] is True
