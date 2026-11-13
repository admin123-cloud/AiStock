# -*- coding: utf-8 -*-
"""R28（2026-11-10）：东财全市场实时快照采集器（运营层，盘中每 5min）。

任务书：F:/Aistock/data/data/runtime/task_briefs/20261110_intraday_eastmoney_r28.md

数据源（2026-11-10 盘中调研定案，探针归档 runtime/intraday_snapshot_r28/）：
  - 个股全市场：push2.eastmoney.com /api/qt/clist/get
      fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048（沪深京全 A 股）
      pz 服务端恒定上限 100，total≈5928（SH2468/SZ3095/BJ365，含停牌约 355 行
      价格 '-' 如实跳过）；fields f2价/f3涨跌幅%/f5量(手)/f6额(元)/f12码/
      f13市场(1=SH,0=SZ|BJ)/f14名称/f18昨收。
      分页间隔 ≥3s（任务书限频边界），单页失败重试 3 次（3s 退避）仍败 →
      整轮 fail-closed 中止（零部分写入）。
  - 指数：push2 /api/qt/ulist.np/get，冻结 10 大盘指数（R18 FROZEN_INDEX_CODES），
      带 f18 昨收（2026-11-10 对拍：上证昨收 3660.79 与 kline_daily 11-09 收盘
      逐位一致；紧对拍东财 vs 腾讯 3 轮价差 0.00、昨收 7/7 全等）。

写入目标（仅此两表，**绝不写 kline_* 正式表**；16:15 盘后管道职责不变）：
  - stock.intraday_market_snapshot  个股明细
  - stock.intraday_index_snapshot   指数快照
  两表 datetime 一律 DateTime('Asia/Shanghai')（R27 统一语义），按日分区，
  TTL 30 日；id=blake2b 确定性域（审计/回滚用）；写入持 DbWriteLock
  scope=intraday_snapshot_r28（锁表方案，与盘后 daily 锁不同 scope 不同窗）。

治理：读回行数不符 → 按精确 snapshot_ts DELETE 回滚该轮（运营表允许），
status=failure；退出码 0=ok / 3=源不可达 / 1=其他失败。
"""
from __future__ import annotations

__version__ = "1.0.0"

import argparse
import hashlib
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.logger import get_logger  # noqa: E402

logger = get_logger("intraday_snapshot_collect")

BEIJING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

DATABASE = "stock"
STOCK_TABLE = "intraday_market_snapshot"
INDEX_TABLE = "intraday_index_snapshot"
STOCK_TABLE_FQN = f"{DATABASE}.{STOCK_TABLE}"
INDEX_TABLE_FQN = f"{DATABASE}.{INDEX_TABLE}"

LOCK_SCOPE = "intraday_snapshot_r28"
LOCK_OWNER = "aistock-engineer"

# ── 东财端点参数（调研定案，勿扩大）─────────────────────────────
# 2026-11-13：https 端点被 WAF 掐（Remote end closed），http 可通——全链路降级 http
CLIST_URL = "http://push2.eastmoney.com/api/qt/clist/get"
ULIST_URL = "http://push2.eastmoney.com/api/qt/ulist.np/get"
CLIST_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
CLIST_FIELDS = "f2,f3,f5,f6,f12,f13,f14,f18"
ULIST_FIELDS = "f2,f3,f4,f5,f6,f12,f13,f14,f18"
PAGE_SIZE = 100          # 服务端上限（pz>100 无效，探针实证）
PAGE_INTERVAL_SEC = 3.0  # 任务书限频边界：分页 ≥3s
PAGE_TIMEOUT_SEC = 15.0
PAGE_RETRY_MAX = 3
PAGE_RETRY_BACKOFF_SEC = 3.0
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# R18 FROZEN_INDEX_CODES（scripts/daily_kline_pipeline.py，11→10 码，禁扩大）；
# secid 前缀：SH=1. SZ=0.（仅两市指数，无 BJ）。
INDEX_SECIDS: Tuple[Tuple[str, str], ...] = (
    ("1.000001", "000001.SH"),
    ("0.399001", "399001.SZ"),
    ("0.399106", "399106.SZ"),
    ("1.000300", "000300.SH"),
    ("1.000016", "000016.SH"),
    ("1.000905", "000905.SH"),
    ("1.000852", "000852.SH"),
    ("0.399006", "399006.SZ"),
    ("1.000680", "000680.SH"),
    ("1.000688", "000688.SH"),
)

# BJ 码前缀（f13=0 且码以这些前缀开头 → .BJ，其余 f13=0 → .SZ）
BJ_CODE_PREFIXES = ("43", "83", "87", "92")

STOCK_COLUMNS = (
    "code", "name", "price", "change_pct", "volume", "amount", "prev_close",
    "snapshot_ts", "created_at", "id",
)
INDEX_COLUMNS = (
    "code", "name", "price", "change_pct", "change_amount", "prev_close",
    "volume", "amount", "snapshot_ts", "created_at", "id",
)

ID_DOMAIN = "r28-intraday-snapshot-v1"

STOCK_DDL = f"""
CREATE TABLE IF NOT EXISTS {DATABASE}.{STOCK_TABLE}
(
    code LowCardinality(String) COMMENT '带后缀证券码，如 600519.SH / 920002.BJ',
    name String COMMENT '东财名称（含 ST/退市前缀，涨跌停阈值分类用）',
    price Nullable(Float64) COMMENT '最新价（元）',
    change_pct Nullable(Float64) COMMENT '涨跌幅 %（相对昨收）',
    volume Nullable(Float64) COMMENT '成交量（东财口径，手）',
    amount Nullable(Float64) COMMENT '成交额（元）',
    prev_close Nullable(Float64) COMMENT '昨收（元，f18 对拍锚）',
    snapshot_ts DateTime('Asia/Shanghai') COMMENT '本轮快照统一时间（采集开始北京钟）',
    created_at DateTime('Asia/Shanghai') COMMENT '写入时间（ReplacingMergeTree 版本列）',
    id UInt64 COMMENT 'blake2b 确定性域 {ID_DOMAIN}'
)
ENGINE = ReplacingMergeTree(created_at)
PARTITION BY toDate(snapshot_ts)
ORDER BY (snapshot_ts, code)
TTL toDateTime(snapshot_ts) + INTERVAL 30 DAY DELETE
"""

INDEX_DDL = f"""
CREATE TABLE IF NOT EXISTS {DATABASE}.{INDEX_TABLE}
(
    code LowCardinality(String) COMMENT '冻结 10 大盘指数码（R18）',
    name String,
    price Nullable(Float64),
    change_pct Nullable(Float64),
    change_amount Nullable(Float64) COMMENT '涨跌额（元）',
    prev_close Nullable(Float64) COMMENT '昨收（f18）',
    volume Nullable(Float64) COMMENT '成交量（东财口径）',
    amount Nullable(Float64) COMMENT '成交额（元）',
    snapshot_ts DateTime('Asia/Shanghai'),
    created_at DateTime('Asia/Shanghai'),
    id UInt64
)
ENGINE = ReplacingMergeTree(created_at)
PARTITION BY toDate(snapshot_ts)
ORDER BY (snapshot_ts, code)
TTL toDateTime(snapshot_ts) + INTERVAL 30 DAY DELETE
"""


# ── 纯函数（单测友好）──────────────────────────────────────────

def _num(value: Any) -> Optional[float]:
    """东财数值字段：'-'（停牌/无值）与非法值 → None；数值 → float。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def snapshot_id(table: str, code: str, snapshot_ts: datetime) -> int:
    """确定性 id：blake2b(domain|table|code|ts) 前 8 字节 → UInt64。"""
    ts_text = snapshot_ts.strftime("%Y-%m-%d %H:%M:%S")
    digest = hashlib.blake2b(
        f"{ID_DOMAIN}|{table}|{code}|{ts_text}".encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big")


def stock_code_with_suffix(raw_code: Any, market_flag: Any) -> Optional[str]:
    """f12+f13 → 带后缀码。f13=1→.SH；f13=0 且 BJ 前缀→.BJ；其余→.SZ。"""
    code = str(raw_code or "").strip()
    if not code or not code.isdigit():
        return None
    flag = str(market_flag)
    if flag == "1":
        return f"{code}.SH"
    if flag == "0":
        if code.startswith(BJ_CODE_PREFIXES):
            return f"{code}.BJ"
        return f"{code}.SZ"
    return None


def normalize_stock_rows(
    raw_rows: Sequence[Mapping[str, Any]], snapshot_ts: datetime, created_at: datetime
) -> Tuple[List[tuple], int]:
    """clist diff 行 → 明细行元组。停牌（价 '-'）如实跳过，返回 (rows, skipped)。"""
    rows: List[tuple] = []
    skipped = 0
    for item in raw_rows:
        code = stock_code_with_suffix(item.get("f12"), item.get("f13"))
        price = _num(item.get("f2"))
        if code is None or price is None or price <= 0:
            skipped += 1
            continue
        name = str(item.get("f14") or "").strip()
        rows.append((
            code,
            name,
            price,
            _num(item.get("f3")),
            _num(item.get("f5")),
            _num(item.get("f6")),
            _num(item.get("f18")),
            snapshot_ts,
            created_at,
            snapshot_id(STOCK_TABLE, code, snapshot_ts),
        ))
    return rows, skipped


def normalize_index_rows(
    raw_rows: Sequence[Mapping[str, Any]], snapshot_ts: datetime, created_at: datetime
) -> List[tuple]:
    """ulist diff 行（f12 纯码，10 指数码空间内唯一）→ 指数行元组。无价行跳过。"""
    # ulist 只回 f12（纯码）；冻结 10 码内无两市撞码，直接映射
    suffix_by_plain = {suffix.split(".")[0]: suffix for _, suffix in INDEX_SECIDS}
    rows: List[tuple] = []
    for item in raw_rows:
        plain = str(item.get("f12") or "").strip()
        code = suffix_by_plain.get(plain)
        price = _num(item.get("f2"))
        if code is None or price is None or price <= 0:
            continue
        rows.append((
            code,
            str(item.get("f14") or "").strip(),
            price,
            _num(item.get("f3")),
            _num(item.get("f4")),
            _num(item.get("f18")),
            _num(item.get("f5")),
            _num(item.get("f6")),
            snapshot_ts,
            created_at,
            snapshot_id(INDEX_TABLE, code, snapshot_ts),
        ))
    return rows


def build_clist_url(page: int) -> str:
    return (
        f"{CLIST_URL}?pn={page}&pz={PAGE_SIZE}&po=1&np=1&fltt=2&invt=2"
        f"&fid=f12&fs={CLIST_FS}&fields={CLIST_FIELDS}"
    )


def build_ulist_url() -> str:
    secids = ",".join(secid for secid, _ in INDEX_SECIDS)
    return f"{ULIST_URL}?fltt=2&invt=2&secids={secids}&fields={ULIST_FIELDS}"


# ── HTTP（可注入 fetcher）──────────────────────────────────────

def _default_fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=PAGE_TIMEOUT_SEC) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_page_with_retry(
    url: str,
    fetcher: Callable[[str], dict],
    *,
    retry_max: int = PAGE_RETRY_MAX,
    backoff_sec: float = PAGE_RETRY_BACKOFF_SEC,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict:
    """单请求重试：全部失败抛 RuntimeError（源不可达由上层转 rc=3）。"""
    last_exc: Optional[Exception] = None
    for attempt in range(1, retry_max + 1):
        try:
            return fetcher(url)
        except Exception as exc:  # noqa: BLE001 - 网络/解析异常统一重试
            last_exc = exc
            if attempt < retry_max:
                sleep_fn(backoff_sec)
    raise RuntimeError(f"fetch failed after {retry_max} attempts: {url} ({last_exc})")


def collect_market_pages(
    fetcher: Callable[[str], dict],
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
    interval_sec: float = PAGE_INTERVAL_SEC,
) -> Tuple[List[dict], int]:
    """全市场分页拉取（纯数据，不落库）。任一页重试耗尽 → RuntimeError。"""
    all_rows: List[dict] = []
    page = 1
    total = None
    while True:
        payload = fetch_page_with_retry(build_clist_url(page), fetcher, sleep_fn=sleep_fn)
        data = (payload or {}).get("data") or {}
        diff = data.get("diff") or []
        if total is None:
            total = int(data.get("total") or 0)
        if not diff:
            break
        all_rows.extend(diff)
        if total and len(all_rows) >= total:
            break
        page += 1
        sleep_fn(interval_sec)
    if total and len(all_rows) < total:
        raise RuntimeError(
            f"clist pagination incomplete: got {len(all_rows)} < total {total}"
        )
    return all_rows, int(total or 0)


def collect_index_rows(fetcher: Callable[[str], dict]) -> List[dict]:
    payload = fetch_page_with_retry(build_ulist_url(), fetcher)
    data = (payload or {}).get("data") or {}
    return list(data.get("diff") or [])


# ── 腾讯 qt 批量快照 fallback（2026-11-13 东财 WAF 事故后由热修临时脚本转正）──
# 通道：qt.gtimg.cn 批量（80 码/请求；a-stock-data SKILL.md §1.1 实测全市场
# 5574 只 ~70 请求 ≈11s，含北交所）。字段映射沿用事故日热修脚本
# （tencent_intraday_snapshot.py）实战口径：
#   parts[3]=现价(f2) [32]=涨跌%(f3) [6]=量手(f5) [37]=额万(f6=×10000)
#   [4]=昨收(f18) [1]=名称(f14) [31]=涨跌额(f4，仅指数)
# fallback 与东财主源走同一 run()：同表、同 DbWriteLock(scope=intraday_snapshot_r28)、
# 同 blake2b 幂等 id 域——只是数据通道不同，治理语义零变化。
QT_URL = "http://qt.gtimg.cn/q="
QT_BATCH_SIZE = 80
QT_BATCH_SLEEP_SEC = 0.12      # 批间冷却（实测 ~70 批全市场 ≈11s 的节奏）
QT_HTTP_TIMEOUT_SEC = 15.0
QT_PAGE_INTERVAL_SEC = 0.05    # fetcher 内部已批量拉全，分页出缓存不需 3s 限频
QT_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# A 股号段白名单（2026-11-13 实测全集）：沪 600-689（主板+科创板）、深 000-003/300/301、
# 北 920（43/83/87 老码已迁移且被 kline 表约束拒绝）。非 A 股（B 股/ETF/债券）剔除。
A_SHARE_PLAIN_PREFIXES = tuple(f"{i:03d}" for i in range(600, 690)) + (
    "000", "001", "002", "003", "300", "301", "920")


def qt_market_prefix(code6: str) -> str:
    """裸 6 位码 → qt 前缀（sh/sz/bj）。

    北交所 92x 必须先于 9x→沪 判断（9 开头歧义段）；2026-11-13 实测 qt 对
    bj920982 正常返回、sh920982 返回 pv_none_match（事故日热修脚本按 9→sh
    请求导致北交所被静默丢弃，此处修正）。
    """
    if code6.startswith(("92", "43", "83", "87")):
        return "bj"
    if code6.startswith(("6", "9", "5")):
        return "sh"
    if code6.startswith(("8", "4")):
        return "bj"
    return "sz"


def parse_qt_payload(text: str) -> dict:
    """qt 批量响应（gbk 文本）→ {prefixed_key: parts 列表}。坏行跳过。"""
    out: dict[str, List[str]] = {}
    for line in text.strip().split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        parts = val.strip('"').split("~")
        if len(parts) > 37:
            out[key.replace("v_", "")] = parts
    return out


def _qt_float(parts: List[str], idx: int) -> float:
    try:
        return float(parts[idx])
    except (ValueError, IndexError):
        return 0.0


def qt_row_to_clist_item(parts: List[str], code6: str) -> Optional[dict]:
    """qt 行 → 东财 clist diff 行形状（f2/f3/f5/f6/f12/f13/f14/f18）。无价跳过。"""
    price = _qt_float(parts, 3)
    if price <= 0:
        return None                                # 停牌/无价如实跳过（与东财口径一致）
    return {
        "f2": price,                               # 现价
        "f3": _qt_float(parts, 32),                # 涨跌%
        "f5": _qt_float(parts, 6),                 # 量（手；688 量纲为股的 caveat 同 §1.1）
        "f6": _qt_float(parts, 37) * 10000.0,      # 额（万 → 元）
        "f12": code6,
        "f13": 1 if qt_market_prefix(code6) == "sh" else 0,
        "f14": parts[1],                           # 名称
        "f18": _qt_float(parts, 4),                # 昨收
    }


def qt_row_to_ulist_item(parts: List[str], plain: str) -> Optional[dict]:
    """qt 指数行 → 东财 ulist diff 行形状（多 f4=涨跌额）。无价跳过。"""
    price = _qt_float(parts, 3)
    if price <= 0:
        return None
    return {
        "f2": price,
        "f3": _qt_float(parts, 32),
        "f4": _qt_float(parts, 31),                # 涨跌额
        "f5": _qt_float(parts, 6),
        "f6": _qt_float(parts, 37) * 10000.0,
        "f12": plain,
        "f14": parts[1],
        "f18": _qt_float(parts, 4),
    }


def extract_a_share_plain_codes(rows: Sequence[str]) -> List[str]:
    """kline_daily 后缀码行 → 去重升序裸码列表（剔冻结指数、剔非 A 股号段）。"""
    frozen = {suffix for _, suffix in INDEX_SECIDS}
    seen: set = set()
    codes: List[str] = []
    for row in rows:
        code = str(row or "").strip()
        if not code or code in frozen:
            continue
        plain = code.split(".")[0]
        if (len(plain) == 6 and plain.isdigit() and plain[:3] in A_SHARE_PLAIN_PREFIXES
                and plain not in seen):
            seen.add(plain)
            codes.append(plain)
    return sorted(codes)


def load_qt_universe(client: Any) -> List[str]:
    """代码表：kline_daily 最新交易日的 distinct A 股码（裸码）。"""
    res = client.query(
        "SELECT DISTINCT code FROM stock.kline_daily "
        "WHERE trade_date = (SELECT max(trade_date) FROM stock.kline_daily)"
    )
    return extract_a_share_plain_codes([r[0] for r in res.result_rows])


def fetch_qt_batch(prefixed_codes: Sequence[str],
                   url_open: Optional[Callable] = None) -> dict:
    """qt 批量拉一批（≤80 码）→ {prefixed_key: parts}。"""
    open_ = url_open or urllib.request.urlopen
    url = QT_URL + ",".join(prefixed_codes)
    req = urllib.request.Request(url, headers=QT_HTTP_HEADERS)
    with open_(req, timeout=QT_HTTP_TIMEOUT_SEC) as resp:
        text = resp.read().decode("gbk", errors="replace")
    return parse_qt_payload(text)


def build_qt_fetcher(client_factory: Optional[Callable[[], Any]] = None,
                     url_open: Optional[Callable] = None,
                     sleep_fn: Callable[[float], None] = time.sleep) -> Callable[[str], dict]:
    """构造 mimicking fetcher：把东财 clist/ulist 请求喂成 qt 批量数据。

    - 代码表懒加载（首次 clist 请求时查 CH kline_daily 最新交易日，进程内缓存）；
    - clist 分页语义与东财一致（data.total + diff 切片），直接复用
      collect_market_pages / normalize_stock_rows / snapshot_id 幂等链路；
    - ulist：冻结 10 指数一次 qt 批量（f12=纯码，normalize_index_rows 负责映射）。
    """
    cache: dict = {"items": None}

    def _stock_items() -> List[dict]:
        if cache["items"] is not None:
            return cache["items"]
        factory = client_factory or _default_client_factory
        codes = load_qt_universe(factory())
        logger.info(f"qt fallback universe: {len(codes)} codes from kline_daily")
        items: List[dict] = []
        for i in range(0, len(codes), QT_BATCH_SIZE):
            batch = codes[i:i + QT_BATCH_SIZE]
            data = fetch_qt_batch([qt_market_prefix(c) + c for c in batch], url_open)
            for code in batch:
                parts = data.get(qt_market_prefix(code) + code)
                if not parts:
                    continue
                item = qt_row_to_clist_item(parts, code)
                if item is not None:
                    items.append(item)
            if i + QT_BATCH_SIZE < len(codes):
                sleep_fn(QT_BATCH_SLEEP_SEC)
        cache["items"] = items
        return items

    def fetch(url: str) -> dict:
        if "/api/qt/clist/get" in url:
            items = _stock_items()
            parsed = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed.query)
            pn = int(params.get("pn", ["1"])[0])
            pz = int(params.get("pz", ["100"])[0])
            start = (pn - 1) * pz
            return {"data": {"total": len(items), "diff": items[start:start + pz]}}
        if "/api/qt/ulist.np/get" in url:
            qt_keys = []
            plain_by_key = {}
            for _, suffix in INDEX_SECIDS:
                plain = suffix.split(".")[0]
                key = ("sh" if suffix.endswith(".SH") else "sz") + plain
                qt_keys.append(key)
                plain_by_key[key] = plain
            data = fetch_qt_batch(qt_keys, url_open)
            items = []
            for key in qt_keys:
                parts = data.get(key)
                if not parts:
                    continue
                item = qt_row_to_ulist_item(parts, plain_by_key[key])
                if item is not None:
                    items.append(item)
            return {"data": {"diff": items}}
        raise RuntimeError(f"qt fallback fetcher 收到未知 url: {url}")

    return fetch


# ── 落库（客户端可注入）────────────────────────────────────────

def _default_client_factory() -> Any:
    from utils.market_warehouse import clickhouse_client

    return clickhouse_client()


def ensure_tables(client: Any) -> None:
    client.command(STOCK_DDL)
    client.command(INDEX_DDL)


def _rollback_snapshot(client: Any, table: str, snapshot_ts: datetime) -> None:
    ts_text = snapshot_ts.strftime("%Y-%m-%d %H:%M:%S")
    client.command(
        f"ALTER TABLE {DATABASE}.{table} DELETE "
        f"WHERE snapshot_ts = toDateTime('{ts_text}', 'Asia/Shanghai')"
    )


def run(
    *,
    fetcher: Optional[Callable[[str], dict]] = None,
    client_factory: Optional[Callable[[], Any]] = None,
    out_dir: Optional[Path] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> dict:
    """单轮采集。返回报告 dict（同时写 JSON 报告文件）。"""
    from utils.db_write_lock import DbWriteLock
    from utils.paths import data_root

    fetch = fetcher or _default_fetch_json
    client_factory = client_factory or _default_client_factory
    now_fn = now_fn or (lambda: datetime.now(BEIJING_TZ))
    snapshot_ts = now_fn()
    if snapshot_ts.tzinfo is None:
        snapshot_ts = snapshot_ts.replace(tzinfo=BEIJING_TZ)
    created_at = snapshot_ts
    stamp = snapshot_ts.strftime("%Y%m%d_%H%M%S")
    if out_dir is None:
        out_dir = data_root() / "runtime" / "intraday_snapshot"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"report_{stamp}.json"

    report: dict[str, Any] = {
        "schema": "intraday_snapshot_collect/1",
        "version": __version__,
        "snapshot_ts": snapshot_ts.isoformat(),
        "tables": [STOCK_TABLE_FQN, INDEX_TABLE_FQN],
        "started_at": datetime.now(BEIJING_TZ).isoformat(),
    }

    # -- 采集（fail-closed：任一环节失败即中止，零部分写入）--
    # 默认链：东财 clist/ulist 主源；源不可达 → 自动切腾讯 qt 批量快照 fallback
    # （同表/同锁/同幂等 id，仅换数据通道）。显式注入 fetcher 时不做隐式回退。
    t0 = time.monotonic()
    source_name = "eastmoney_push2"
    try:
        raw_stocks, total_hint = collect_market_pages(fetch, sleep_fn=sleep_fn)
        raw_indices = collect_index_rows(fetch)
    except RuntimeError as exc:
        if fetcher is not None:
            report.update({
                "status": "failure",
                "reason": "source_unreachable",
                "error": str(exc),
            })
            _write_report(report_path, report)
            logger.error(f"intraday snapshot source unreachable: {exc}")
            return report
        logger.warning(f"east push2 unreachable, falling back to tencent qt batch: {exc}")
        try:
            qt_fetch = build_qt_fetcher(client_factory=client_factory, sleep_fn=sleep_fn)
            raw_stocks, total_hint = collect_market_pages(
                qt_fetch, sleep_fn=sleep_fn, interval_sec=QT_PAGE_INTERVAL_SEC)
            raw_indices = collect_index_rows(qt_fetch)
            source_name = "tencent_qt_batch"
        except RuntimeError as exc2:
            report.update({
                "status": "failure",
                "reason": "source_unreachable",
                "error": f"east: {exc}; qt_fallback: {exc2}",
            })
            _write_report(report_path, report)
            logger.error(f"intraday snapshot sources unreachable (east+qt): {exc2}")
            return report
    fetch_elapsed = round(time.monotonic() - t0, 1)

    stock_rows, skipped = normalize_stock_rows(raw_stocks, snapshot_ts, created_at)
    index_rows = normalize_index_rows(raw_indices, snapshot_ts, created_at)
    if len(stock_rows) < 1000:
        report.update({
            "status": "failure",
            "reason": "insufficient_rows",
            "raw_total": total_hint,
            "valid_rows": len(stock_rows),
        })
        _write_report(report_path, report)
        logger.error(f"intraday snapshot rows insufficient: {len(stock_rows)}")
        return report
    if not index_rows:
        report.update({
            "status": "failure",
            "reason": "index_rows_empty",
        })
        _write_report(report_path, report)
        return report

    # -- 建表 + 锁内写入 + 读回 --
    lock_client = client_factory()
    lock = DbWriteLock(lock_client)
    acquired = lock.acquire(LOCK_SCOPE, LOCK_OWNER, purpose="盘中东财快照采集（R28）")
    wrote_stocks = False
    wrote_indices = False
    try:
        client = client_factory()
        ensure_tables(client)
        client.insert(STOCK_TABLE, [tuple(r) for r in stock_rows], column_names=list(STOCK_COLUMNS))
        wrote_stocks = True
        client.insert(INDEX_TABLE, [tuple(r) for r in index_rows], column_names=list(INDEX_COLUMNS))
        wrote_indices = True

        ts_text = snapshot_ts.strftime("%Y-%m-%d %H:%M:%S")
        stock_count = int(client.command(
            f"SELECT count() FROM {DATABASE}.{STOCK_TABLE} "
            f"WHERE snapshot_ts = toDateTime('{ts_text}', 'Asia/Shanghai')"
        ))
        index_count = int(client.command(
            f"SELECT count() FROM {DATABASE}.{INDEX_TABLE} "
            f"WHERE snapshot_ts = toDateTime('{ts_text}', 'Asia/Shanghai')"
        ))
        if stock_count != len(stock_rows) or index_count != len(index_rows):
            # 读回不符 → 回滚本轮（运营表按精确 snapshot_ts 删除）
            _rollback_snapshot(client, STOCK_TABLE, snapshot_ts)
            _rollback_snapshot(client, INDEX_TABLE, snapshot_ts)
            report.update({
                "status": "failure",
                "reason": "readback_mismatch",
                "inserted_stocks": len(stock_rows),
                "readback_stocks": stock_count,
                "inserted_indices": len(index_rows),
                "readback_indices": index_count,
            })
            _write_report(report_path, report)
            return report
        report.update({
            "status": "success",
            "source": source_name,
            "raw_total": total_hint,
            "valid_stock_rows": len(stock_rows),
            "skipped_no_price": skipped,
            "index_rows": len(index_rows),
            "readback_stocks": stock_count,
            "readback_indices": index_count,
            "fetch_elapsed_sec": fetch_elapsed,
            "total_elapsed_sec": round(time.monotonic() - t0, 1),
        })
        _write_report(report_path, report)
        logger.info(
            f"intraday snapshot ok: stocks={len(stock_rows)} (skipped={skipped}) "
            f"indices={len(index_rows)} elapsed={fetch_elapsed:.1f}s"
        )
        return report
    except Exception as exc:  # noqa: BLE001 - 写入异常：已写的表逐个回滚，再记失败
        try:
            rollback_client = client_factory()
            if wrote_stocks:
                _rollback_snapshot(rollback_client, STOCK_TABLE, snapshot_ts)
            if wrote_indices:
                _rollback_snapshot(rollback_client, INDEX_TABLE, snapshot_ts)
        except Exception as rb_exc:  # noqa: BLE001
            logger.error(f"intraday snapshot rollback failed: {rb_exc}")
        report.update({"status": "failure", "reason": "write_error", "error": str(exc)})
        _write_report(report_path, report)
        return report
    finally:
        try:
            lock.release(LOCK_SCOPE, LOCK_OWNER)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"intraday snapshot lock release failed: {exc}")


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    payload = dict(report)
    payload["finished_at"] = datetime.now(BEIJING_TZ).isoformat()
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def exit_code(report: Mapping[str, Any]) -> int:
    status = report.get("status")
    if status == "success":
        return 0
    if report.get("reason") == "source_unreachable":
        return 3
    return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="AiStock 盘中东财实时快照采集器（R28）：写 intraday_market_snapshot/"
                    "intraday_index_snapshot 两张运营层表，不触碰 kline_* 正式表。"
    )
    parser.add_argument(
        "--out-dir", default=None,
        help="报告输出目录（默认 AISTOCK_DATA_ROOT/runtime/intraday_snapshot）",
    )
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir) if args.out_dir else None
    report = run(out_dir=out_dir)
    print(json.dumps({k: report.get(k) for k in (
        "status", "reason", "valid_stock_rows", "index_rows", "snapshot_ts",
    )}, ensure_ascii=False))
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
