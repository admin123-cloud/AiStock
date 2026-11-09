# -*- coding: utf-8 -*-
"""腾讯 K 线日线适配层（a-stock-data SKILL.md §1.2，PoC-A 2026-11-01 验证）。

单位口径（PoC-A 实测）：
- 日线 volume 对科创板 688/689 是「股」，需 ÷100 换算为 AiStock 契约的「手」；
  其余板块（沪主板/深主板/创业板/B股/ETF/指数）原始即「手」，不换算。
- 本接口**没有成交额**：amount 显式置 None 并附 amount_available=False 与原因标记。
- 不支持北交所（腾讯只返回最新 1 根日线，区间/分钟为空）→ 直接 ValueError。
- 三个入口同一后端、限流独立（SKILL.md #52：单入口约 600 次后返回空 JSON），
  失败入口冷却 120 秒轮换；解析离线可测（parse_daily_payload 不发请求）。
"""
import json
import re
import time
from datetime import datetime, timezone

TENCENT_KLINE_HOSTS = [
    "https://web.ifzq.gtimg.cn",
    "https://proxy.finance.qq.com/ifzqgtimg",
    "https://ifzq.gtimg.cn",
]
KLINE_PATH = "/appstock/app/fqkline/get"
HOST_COOLDOWN_SECONDS = 120          # 某入口失败后暂停使用的秒数
# 模块级入口冷却状态（同进程内所有调用共享）
host_down_until = {}

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA, "Referer": "https://gu.qq.com/"}

# 科创板 688/689：腾讯原始量是股；其余是手
_SHARES_PREFIXES = ("688", "689")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _num(value, what):
    """响应里的数值字段 → float；空值/布尔/非数值一律 RuntimeError（fail-closed）。"""
    if value is None or isinstance(value, bool):
        raise RuntimeError(f"腾讯 K 线 {what} 字段为空或不是数值: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").strip()
    try:
        return float(text)
    except ValueError as exc:
        raise RuntimeError(f"腾讯 K 线 {what} 字段无法识别为数值: {value!r}") from exc


def _src_date(value):
    text = str(value).strip()
    if not _DATE_RE.match(text):
        raise RuntimeError(f"腾讯 K 线返回了无法识别的日期 {value!r}，格式可能已变")
    return text


def normalize_symbol(code):
    """任意写法（sh688981 / 688981 / 688981.SH / 000001）→ 'sz000001' 式前缀符号。

    显式前缀/后缀透传消歧；裸 6 位按号段归属（688/689/6/5/9→sh、0/3→sz、92/4/8→bj）。
    认不出抛 ValueError，绝不猜。
    """
    raw = str(code).strip().lower()
    if raw.startswith(("sh", "sz", "bj")) and re.fullmatch(r"(sh|sz|bj)\d{6}", raw):
        return raw
    match = re.fullmatch(r"(\d{6})(?:\.(sh|sz|bj|xshg|xshe))?", raw)
    if not match:
        raise ValueError(f"无法把 {code!r} 解析为 6 位代码（支持 sh688981 / 688981 / 688981.SH / 600000.XSHG）")
    digits, suffix = match.group(1), match.group(2)
    if suffix == "xshg":
        suffix = "sh"
    elif suffix == "xshe":
        suffix = "sz"
    if suffix:
        return suffix + digits
    if digits.startswith("92"):        # 北交所 92x 必须先于 9x→沪 判断（9 开头歧义段）
        return "bj" + digits
    if digits[0] in ("5", "6", "9"):
        return "sh" + digits
    if digits[0] in ("0", "3"):
        return "sz" + digits
    if digits[0] in ("4", "8"):
        return "bj" + digits
    raise ValueError(f"无法判断 {code!r} 的归属市场，请用显式前缀（如 sh{digits}）")


def parse_daily_payload(payload, symbol, adjust=""):
    """离线解析 fqkline/get 的 JSON 载荷 → 契约行列表（不发任何请求）。

    symbol 形如 'sh688981'（决定量的单位口径）。行元素 [日期, 开, 收, 高, 低, 量, (额外列…)]，
    额外列忽略。输出每行：date/open/high/low/close/volume(手)/volume_raw/volume_raw_unit/
    volume_divisor/amount=None + amount_available=False + amount_missing_reason/adjust。
    格式变异（非对象载荷、缺 symbol 节点、缺 day/qfqday、qfq 空而原始有数据、
    行变短/非列表、日期重复或不可识别、数值不可识别、msg=param error）一律抛错。
    """
    if not isinstance(payload, dict):
        raise RuntimeError(f"腾讯 K 线载荷应为 JSON 对象，实际是 {type(payload).__name__}")
    if payload.get("msg") == "param error":
        raise ValueError("腾讯 K 线 param error：参数错误（区间过长或代码不存在）")
    data = payload.get("data")
    if not isinstance(data, dict) or symbol not in data:
        raise RuntimeError(f"腾讯 K 线返回里没有 {symbol} 的数据节点，格式可能已变")
    node = data[symbol]
    if not isinstance(node, dict):
        raise RuntimeError(f"腾讯 K 线 {symbol} 节点应为对象，实际是 {type(node).__name__}")
    key = (adjust or "") + "day" if adjust else "day"
    items = node.get(key) if key in node else node.get("day")
    if not isinstance(items, list):
        raise RuntimeError(f"腾讯 K 线 {symbol} 的返回里没有可用的 {key}/day 列表，格式可能已变")
    if key != "day" and key in node and not items and node.get("day"):
        raise RuntimeError(f"腾讯 K 线 {symbol} 的 {key} 为空、day 却有数据，"
                           f"不能拿原始价冒充复权价")
    raw_unit = "shares" if symbol.startswith("sh") and symbol[2:5] in _SHARES_PREFIXES else "lots"
    divisor = 100 if raw_unit == "shares" else 1
    rows, seen = [], set()
    for item in items:
        try:
            if not isinstance(item, list) or len(item) < 6:
                raise RuntimeError(f"行格式改变（不是 ≥6 元素的数组）: {item!r}")
            day = _src_date(item[0])
            if day in seen:
                raise RuntimeError(f"日期 {day} 出现两次，结果不可信")
            seen.add(day)
            rows.append({
                "code": symbol,
                "date": day,
                "open": _num(item[1], "open"),
                "close": _num(item[2], "close"),
                "high": _num(item[3], "high"),
                "low": _num(item[4], "low"),
                "volume_raw": _num(item[5], "volume"),
                "volume_raw_unit": raw_unit,
                "volume_divisor": divisor,
                "volume": round(_num(item[5], "volume") / divisor, 2),
                "volume_unit": "lots",
                # 腾讯 K 线无成交额字段：显式置 None + 标记（成交额用通达信盘后包）
                "amount": None,
                "amount_available": False,
                "amount_missing_reason": "tencent_kline_has_no_amount_field",
                "amount_unit": "CNY",
                "adjust": adjust or "none",
            })
        except RuntimeError:
            raise
        except (TypeError, IndexError, KeyError) as exc:
            raise RuntimeError(f"腾讯 K 线 {symbol} 行格式改变: {item!r} ({exc})") from exc
    rows.sort(key=lambda row: row["date"])
    return rows


def _default_transport(url, params, timeout):
    """默认传输：requests GET + 浏览器 UA / Referer。"""
    import requests
    return requests.get(url, params=params, headers=_HEADERS, timeout=timeout)


def fetch_daily_kline(code, start=None, end=None, count=320, adjust="",
                      transport=None, timeout=(8, 20), now=time.time):
    """腾讯日线（fqkline/get）：三入口轮换 + 120s 失败冷却，解析走 parse_daily_payload。

    code 支持任意写法（normalize_symbol）；北交所直接 ValueError（腾讯只给最新 1 根，
    区间/分钟为空，PoC-A 920982 实测）。被限流入口的典型表现（异常 / 空 JSON /
    顶层非对象）→ 标记冷却并换下一入口；param error 是参数问题 → ValueError 不轮换；
    三个入口都不可用 → RuntimeError。adjust：''（不复权，对账/落库用）/ 'qfq' / 'hfq'。
    transport / now 可注入以便离线测试。返回 rows + host + units + lineage。
    """
    symbol = normalize_symbol(code)
    if symbol.startswith("bj"):
        raise ValueError("腾讯 K 线不支持北交所（只返回最新 1 根日线、区间/分钟为空，PoC-A 920982 实测）；"
                         "北交所日线请用 tdx_official_package 按交易日取")
    if adjust not in ("", "qfq", "hfq"):
        raise ValueError("adjust 只能是 '' / 'qfq' / 'hfq'")
    if not 1 <= int(count) <= 640:
        raise ValueError("日线 count 范围 1–640")
    if end and not start:
        raise ValueError("只给 end 时请同时给 start")
    param = f"{symbol},day,{start or ''},{end or ''},{int(count)},{adjust}"
    caller = transport or _default_transport
    errors, tried = [], []
    for host in TENCENT_KLINE_HOSTS:
        if host_down_until.get(host, 0) > now():
            continue
        url = host + KLINE_PATH
        tried.append(host)
        try:
            response = caller(url, {"param": param}, timeout)
            text = (response.text or "").strip()
            payload = json.loads(text) if text else {}
            if not isinstance(payload, dict):
                raise RuntimeError(f"顶层是 {type(payload).__name__}，不是对象")
        except ValueError as exc:                      # json 解析错误属于格式问题，换入口
            errors.append(f"{host}: 响应不是 JSON ({exc})")
            host_down_until[host] = now() + HOST_COOLDOWN_SECONDS
            continue
        except Exception as exc:                       # 网络层异常，换入口
            errors.append(f"{host}: {type(exc).__name__}: {exc}")
            host_down_until[host] = now() + HOST_COOLDOWN_SECONDS
            continue
        if payload.get("msg") == "param error":
            raise ValueError(f"腾讯 K 线 param error：参数错误（区间过长或代码不存在）: {param}")
        data = payload.get("data")
        if isinstance(data, dict) and data:
            rows = parse_daily_payload(payload, symbol, adjust=adjust)
            if rows:
                return {
                    "symbol": symbol,
                    "rows": rows,
                    "row_count": len(rows),
                    "host": host,
                    "url": url,
                    "param": param,
                    "units": {"volume": "lots", "amount": None},
                    "lineage": {
                        "source": "tencent_kline",
                        "url": url,
                        "param": param,
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                        "row_count": len(rows),
                        "hosts_tried": tried,
                        "note": "腾讯 K 线无成交额；volume 已统一为手（688/689 原始为股 ÷100）",
                    },
                }
            errors.append(f"{host}: data 为空（未上市/停牌区间/代码有误）")
        else:
            errors.append(f"{host}: 空响应")
        host_down_until[host] = now() + HOST_COOLDOWN_SECONDS
    raise RuntimeError(f"腾讯 K 线三个入口均不可用（可能被限流，稍后重试）: " + "; ".join(errors))
