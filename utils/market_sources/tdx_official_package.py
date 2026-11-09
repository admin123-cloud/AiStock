# -*- coding: utf-8 -*-
"""通达信官网每日盘后包适配层（a-stock-data SKILL.md §1.3，PoC-B 2026-11-01 验证）。

约定（与 pocb_report_final.json 一致）：
- 包内股票 volume 原始单位一律是「股」、amount 是「元」（两位小数）；normalize_to_contract
  统一换算为 AiStock 契约的 手 = 股 / 100，并附单位元数据与血缘。
- 官网约 16:10（北京时间）发布当日包；非交易日 / 未发布返回 404 → ValueError。
- 原始参考实现无重试；fetch_daily_package 自带指数退避重试。
- 解析对一切格式变异 fail-closed 抛错，绝不把部分/错数据当正常返回。
"""
import hashlib
import io
import math
import re
import struct
import time
import zipfile
import zlib
from datetime import date as _date_cls, datetime, timezone

TDX_PACKAGE_URL = "https://www.tdx.com.cn/products/data/data/g4day/{ymd}.zip"
# 盘后包从这一天起带北交所文件（2026-09-20 二分实测：20220505 无、20220506 有）
TDX_BJ_FIRST_DAY = "20220506"
# 每个市场有收盘价的最少行数（2026-09-20 实测 2021-08-16 ~ 2026-09-18 共 11 个包的最小值下取整）
TDX_MIN_PRICED = {"sh": 10000, "sz": 3000, "bj": 50}

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_STOCK_KEEP = {"sh": ("60", "68"), "sz": ("00", "30"), "bj": ("92",)}
# 非股票分类表：market -> (2 位前缀元组 -> 类别)；sz 39 段再按 3 位细分（399 指数 / 395 逆回购）
_CLASSIFY_2 = {
    "sh": {
        ("01", "02", "10", "11", "12", "13", "14", "15", "16", "17", "18", "19", "23", "24", "27"): "bond",
        ("20",): "reverse_repo",
        ("50", "51", "52", "53", "54", "55", "56", "57", "58", "59"): "fund",
        ("88",): "tdx_internal_index",
        ("90",): "b_share",
    },
    "sz": {
        ("10", "11", "12", "13", "14", "19", "52", "56"): "bond",
        ("15", "16", "18", "50"): "fund",
        ("20",): "b_share",
    },
    "bj": {
        ("82", "91"): "bond",
        ("43", "83", "87"): "bj_legacy_stock",   # 老号段，多已迁 920；按约定不保留但单独分类
    },
}
_SZ_39_3 = {"399": "index", "395": "reverse_repo"}
_BJ_899 = "index"
_SH_INDEX_2 = ("00", "99")


def _to_ymd(value):
    """'2026-10-22' / '20261022' / date → '20261022'；其他写法抛 ValueError。"""
    if isinstance(value, datetime):
        return value.date().strftime("%Y%m%d")
    if isinstance(value, _date_cls):
        return value.strftime("%Y%m%d")
    text = str(value).strip()
    fmt = "%Y%m%d" if re.fullmatch(r"[0-9]{8}", text) else "%Y-%m-%d"
    return datetime.strptime(text, fmt).strftime("%Y%m%d")


def parse_zip_bytes(content, ymd, min_priced=TDX_MIN_PRICED):
    """离线解析盘后包字节：每市场一对 {market}{yyMMdd}.cod（150B/条代码表）+ .md1（512B/块行情）。

    与下载分离，可离线测试。任何格式变异（非 zip、缺文件、长度不整块、条数对不上、
    代码非 6 位数字或重复、行情块序号重复/越界、价格非有限数、名称非 GBK/为空）
    一律 RuntimeError（fail-closed）。close<=0 的通达信自编板块条目按无价剔除（非错误）。
    min_priced: {market: 最少有价行数} 下限核对，None/空 dict 跳过（小样本/离线测试用），
    默认生产实测下限 TDX_MIN_PRICED。
    """
    if not isinstance(content, (bytes, bytearray)):
        raise RuntimeError(f"盘后包内容应为 bytes，实际是 {type(content).__name__}")
    ymd = _to_ymd(ymd)
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, EOFError) as exc:
        raise RuntimeError(f"通达信盘后包不是可解压的 zip 文件: {type(exc).__name__}: {exc}") from exc
    names = set(archive.namelist())
    rows = []
    for market in ("sh", "sz", "bj"):
        cod_name, md1_name = f"{market}{ymd[2:]}.cod", f"{market}{ymd[2:]}.md1"
        if (market == "bj" and ymd < TDX_BJ_FIRST_DAY
                and cod_name not in names and md1_name not in names):
            continue            # 这天之前的包还没有北交所文件；之后缺文件按格式改变报错
        if cod_name not in names or md1_name not in names:
            raise RuntimeError(f"通达信盘后包缺少 {cod_name}/{md1_name}，格式可能已变")
        try:
            cod, md1 = archive.read(cod_name), archive.read(md1_name)
        except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError) as exc:
            raise RuntimeError(f"通达信盘后包 {cod_name}/{md1_name} 无法解压: "
                               f"{type(exc).__name__}: {exc}") from exc
        if len(cod) % 150 or len(md1) % 512:
            raise RuntimeError(f"{market} 代码表或行情块长度不是整块，文件可能被截断")
        if len(cod) // 150 != len(md1) // 512:
            raise RuntimeError(f"{market} 代码表 {len(cod) // 150} 条、行情块 {len(md1) // 512} 块，对不上")
        before, codes, seqs = len(rows), set(), set()
        for offset in range(0, len(cod), 150):
            record = cod[offset:offset + 150]
            code = record[0:6].rstrip(b"\x00 ").decode("ascii", "replace")
            seq = struct.unpack("<H", record[32:34])[0]
            if not re.fullmatch(r"[0-9]{6}", code):
                raise RuntimeError(f"通达信盘后包 {market} 代码表出现非 6 位数字代码 {code!r}，格式可能已变")
            if code in codes or seq in seqs:
                raise RuntimeError(f"通达信盘后包 {market} 代码表有重复的代码 / 行情块序号（{code!r}, seq={seq}）")
            codes.add(code)
            seqs.add(seq)
            block = md1[seq * 512:(seq + 1) * 512]
            if len(block) != 512:
                raise RuntimeError(f"{market}{code} 行情块越界（seq={seq}）")
            prev_close = struct.unpack("<d", block[4:12])[0]
            open_, high, low, close = struct.unpack("<4d", block[12:44])
            amount = struct.unpack("<d", block[72:80])[0]
            if not all(math.isfinite(v) for v in (prev_close, open_, high, low, close, amount)):
                raise RuntimeError(f"通达信盘后包 {market}{code} 行情块出现非有限数值，文件可能已损坏")
            if close <= 0:
                continue        # 880/881 等通达信自编板块当日无价，整块为 0，不是证券
            volume = struct.unpack("<Q", block[56:64])[0]
            raw_name = record[40:72].split(b"\x00")[0]
            try:
                name = raw_name.decode("gbk").strip()
            except UnicodeDecodeError as exc:
                raise RuntimeError(f"通达信盘后包 {market}{code} 的名称不是 GBK，文件可能已损坏") from exc
            if not name:
                raise RuntimeError(f"通达信盘后包 {market}{code} 有价格却没有名称，文件可能已损坏")
            rows.append({"date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}", "market": market,
                         "code": code, "name": name,
                         "prev_close": round(prev_close, 4), "open": round(open_, 4),
                         "high": round(high, 4), "low": round(low, 4),
                         "close": round(close, 4), "volume": volume,
                         "amount": round(amount, 2)})
        floor = (min_priced or {}).get(market)
        if floor is not None and len(rows) - before < floor:
            raise RuntimeError(f"通达信盘后包 {market} 市场只有 {len(rows) - before} 条有价记录"
                               f"（下限 {floor}），文件可能残缺或格式已变")
    return rows


def classify_code(market, code):
    """(market, 6 位代码) → 分类标签。股票池为 stock（沪 60/68、深 00/30、北 92），
    其余为排除类别（index/bond/fund/reverse_repo/tdx_internal_index/b_share/bj_legacy_stock）；
    认不出的返回 "unknown" —— 交给调用方计数，绝不静默丢弃。
    分类依据 PoC-B 三日全包实测前缀分布（pocb_report_final.json + 归档 CSV 枚举）。
    """
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
        return "unknown"
    prefix2, prefix3 = code[:2], code[:3]
    if market in _STOCK_KEEP:
        if prefix2 in _STOCK_KEEP[market]:
            return "stock"
    else:
        return "unknown"
    if market == "sh":
        if prefix2 in _SH_INDEX_2:
            return "index"
    elif market == "sz":
        if prefix3 in _SZ_39_3:
            return _SZ_39_3[prefix3]
    elif market == "bj":
        if prefix3 == "899":
            return _BJ_899
    for prefixes, category in _CLASSIFY_2[market].items():
        if prefix2 in prefixes:
            return category
    return "unknown"


def filter_stock_universe(rows):
    """从 parse_zip_bytes 输出的全证券行里只保留股票（沪 60/68、深 00/30、北 92）且 volume>0。

    返回 (kept_rows, report)：report 含逐类排除计数、每类示例、volume=0 的股票数、
    未知前缀明细（market/2 位前缀/条数/示例代码）与总行数；
    kept + excluded + unknown + zero_volume == total_rows 由总账保证，任何一行都不会被静默丢弃。
    """
    kept, excluded_counts, excluded_examples, unknown_counts, unknown_examples = [], {}, [], {}, {}
    zero_volume = 0
    for row in rows:
        market, code = row.get("market"), row.get("code")
        category = classify_code(market, code)
        if category == "stock":
            volume = row["volume"]
            if not isinstance(volume, (int, float)) or isinstance(volume, bool):
                raise RuntimeError(f"股票行 {market}{code} 的 volume 不是数值: {volume!r}")
            if volume > 0:
                kept.append(row)
            else:
                zero_volume += 1
            continue
        if category == "unknown":
            key = (market, code[:2] if isinstance(code, str) and code else str(code)[:2])
            unknown_counts[key] = unknown_counts.get(key, 0) + 1
            unknown_examples.setdefault(key, []).append(code)
            continue
        excluded_counts[category] = excluded_counts.get(category, 0) + 1
        if len(excluded_examples) < 50 or category not in {e["category"] for e in excluded_examples}:
            excluded_examples.append({"market": market, "code": code,
                                      "name": row.get("name"), "category": category})
    report = {
        "total_rows": len(rows),
        "kept": len(kept),
        "kept_by_market": {m: sum(1 for r in kept if r["market"] == m) for m in ("sh", "sz", "bj")},
        "excluded_by_category": dict(sorted(excluded_counts.items())),
        "excluded_examples": excluded_examples,
        "stock_zero_volume": zero_volume,
        "unknown": [{"market": m, "prefix": p, "count": unknown_counts[(m, p)],
                     "examples": unknown_examples[(m, p)][:5]}
                    for m, p in sorted(unknown_counts)],
    }
    accounted = len(kept) + sum(excluded_counts.values()) + sum(unknown_counts.values()) + zero_volume
    if accounted != len(rows):
        raise RuntimeError(f"股票池过滤总账不平：{accounted} != {len(rows)}，有行被静默丢弃")
    return kept, report


_VOLUME_DIVISOR = 100


def normalize_to_contract(rows, lineage):
    """已过滤的股票行 → AiStock 日线契约：volume 一律 股÷100→手（保留 2 位小数精确值），
    amount 元（2 位小数），逐行附单位元数据，结果附 units 与血缘（url / fetched_at /
    zip_sha256 / 行数，来自 fetch_daily_package 的 lineage 参数）。

    fail-closed：入参里出现非股票行（指数/债券等 volume 量纲不同，÷100 即错数据）或
    lineage 缺 url / fetched_at / zip_sha256 时直接抛错，不产出半可信结果。
    """
    missing = [key for key in ("url", "fetched_at", "zip_sha256")
               if not isinstance(lineage, dict) or not lineage.get(key)]
    if missing:
        raise ValueError(f"normalize_to_contract 需要 lineage 提供血缘字段 {missing}（url/fetched_at/zip_sha256）")

    out_rows = []
    for row in rows:
        if classify_code(row.get("market"), row.get("code")) != "stock":
            raise RuntimeError(f"normalize_to_contract 收到非股票行 {row.get('market')}{row.get('code')}，"
                               f"其 volume 量纲不是股，不能 ÷100 —— 请先过 filter_stock_universe")
        raw_volume = row["volume"]
        if not isinstance(raw_volume, (int, float)) or isinstance(raw_volume, bool):
            raise RuntimeError(f"股票行 {row.get('market')}{row.get('code')} 的 volume 不是数值: {raw_volume!r}")
        volume = round(raw_volume / _VOLUME_DIVISOR, 2)
        out_rows.append({
            "code": f'{row["code"]}.{row["market"].upper()}',
            "market": row["market"],
            "date": row["date"],
            "name": row["name"],
            "prev_close": row["prev_close"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": volume,
            "volume_unit": "lots",
            "volume_raw": raw_volume,
            "volume_raw_unit": "shares",
            "volume_divisor": _VOLUME_DIVISOR,
            "amount": round(float(row["amount"]), 2),
            "amount_unit": "CNY",
            "amount_available": True,
        })
    return {
        "source": "tdx_official_package",
        "units": {"volume": "lots", "amount": "CNY"},
        "rows": out_rows,
        "lineage": {"url": lineage["url"], "fetched_at": lineage["fetched_at"],
                    "zip_sha256": lineage["zip_sha256"],
                    "row_count": len(out_rows), "raw_row_count": len(rows)},
    }


_TRANSIENT_STATUS = (429, 500, 502, 503, 504)


def _default_fetcher(url, timeout):
    """默认下载器：requests GET + 浏览器 UA。网络异常 / 非 2xx 由调用方统一处理。"""
    import requests
    return requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)


def fetch_daily_package(trade_date, retries=3, timeout=60, fetcher=None,
                        sleep=time.sleep, backoff_base=1.0):
    """下载某交易日的盘后包（约 2.7MB），与解析分离：返回 content + 血缘，不解析。

    原参考实现（SKILL.md §1.3）无重试；这里对瞬态失败（网络异常 / 429 / 5xx）做
    指数退避重试：retries=3 → 最多 1+3=4 次尝试，退避 backoff_base×{1,2,4} 秒。
    404 是终态（非交易日 / 当日包未发布，官网约 16:10 北京时间后才有）→ 立即 ValueError，
    不重试；200 但非 zip（错误页）→ 立即 RuntimeError。重试耗尽抛 RuntimeError。
    fetcher / sleep 可注入以便离线测试。
    """
    ymd = _to_ymd(trade_date)          # 写错日期 → ValueError，不发起请求
    url = TDX_PACKAGE_URL.format(ymd=ymd)
    downloader = fetcher or _default_fetcher
    attempts, last_error = 0, None
    for attempt in range(retries + 1):
        if attempt:
            sleep(backoff_base * (2 ** (attempt - 1)))
        attempts += 1
        try:
            response = downloader(url, timeout)
        except Exception as exc:       # 网络层异常：可重试
            last_error = RuntimeError(f"请求 {url} 失败（第 {attempts} 次）: "
                                      f"{type(exc).__name__}: {exc}")
            continue
        status = getattr(response, "status_code", None)
        if status == 404:
            raise ValueError(f"{trade_date} 没有通达信盘后包（404）：非交易日、当日包尚未发布"
                             f"（约 16:10 北京时间），或早于官网保留范围")
        if status != 200:
            if status in _TRANSIENT_STATUS and attempt < retries:
                last_error = RuntimeError(f"{url} 返回 HTTP {status}（第 {attempts} 次），将重试")
                continue
            raise RuntimeError(f"{url} 返回 HTTP {status}，不是可下载的盘后包")
        content = response.content
        if not content.startswith(b"PK"):
            raise RuntimeError(f"{url} 返回 200 但内容不是 zip 文件，可能是错误页"
                               f"（前 16 字节: {content[:16]!r}）")
        return {"url": url, "content": content, "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "zip_sha256": hashlib.sha256(content).hexdigest(),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "attempts": attempts}
    raise RuntimeError(f"下载 {url} 在 {attempts} 次尝试后仍失败: {last_error}")
