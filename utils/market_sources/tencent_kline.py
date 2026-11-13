# -*- coding: utf-8 -*-
"""腾讯 K 线分钟线适配层（vendored，整块移植）。

来源：github.com/simonlin1212/a-stock-data（工具包 V3.10.3）SKILL.md §1.2
``v39-tencent-kline`` 分钟段 + 前置 helper（会话/重试/数值校验），按工具包接入
规范整块 vendored 进宿主代码库并注明版本，**不做运行时依赖**。接入前交叉验证
见 runtime/intraday_kline_upsert/（2026-11-13 对 TDX 盘后 m5 逐 bar 对账）。

通道（与 tencent_daily 同一后端、限流各自独立，单入口约 600 次后返回空 JSON）：
  1. https://proxy.finance.qq.com/ifzqgtimg —— 2026-11-13 实测唯一可用通道
     （web.ifzq/ifzq 直连 302/被 WAF），故排在首位；
  2/3. web.ifzq.gtimg.cn / ifzq.gtimg.cn —— 保留轮换（入口恢复后自动可用）。
失败入口冷却 120 秒；三入口均不可用 → RuntimeError（fail-closed，不造数）。

单位与口径（工具包 SKILL.md §1.2 实测，与 tencent_daily 日线一致）：
- volume 原始「手」；科创板 688/689 原始「股」，÷100 统一为手（volume_divisor 暴露）；
- 最近一个交易日的分钟量为整手，下一交易日才回填精确股数（对账容差须覆盖）；
- 本接口**没有成交额**：amount 显式 None + amount_available=False（调用方估算须标注口径）；
- 不含北交所（腾讯对北交所分钟线为空）→ 直接 ValueError；
- ``datetime`` 为**周期结束边界**（如 '09:35' 是 09:30-09:35 的 bar），因此最后一根
  可能是未收盘 bar——按边界时刻与当前北京时间比较丢弃或标记，由调用方决定；
- ``turnover_rate_pct``：源给的是换手率基点（÷100 才是百分数）。

测试离线可测：parse_mkline_payload 不发请求；fetch 的 transport 可注入。
"""
import json
import math
import time
from datetime import datetime, timezone

# vendored 自 v39-helpers：三入口顺序按 2026-11-13 实测调整（proxy 通道优先）
TENCENT_KLINE_HOSTS = [
    "https://proxy.finance.qq.com/ifzqgtimg",
    "https://web.ifzq.gtimg.cn",
    "https://ifzq.gtimg.cn",
]
MKLINE_PATH = "/appstock/app/kline/mkline"
HOST_COOLDOWN_SECONDS = 120          # 某入口失败后暂停使用的秒数
# 模块级入口冷却状态（同进程内所有调用共享）
host_down_until = {}

TENCENT_MINUTE_PERIODS = ("m1", "m5", "m15", "m30", "m60")
MINUTE_COUNT_MAX = 320               # 工具包实测：分钟线单次 ≤320 根

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA, "Referer": "https://gu.qq.com/"}

# 科创板 688/689：腾讯原始量是股；其余是手（工具包 SKILL.md 关键规则）
_SHARES_PREFIXES = ("688", "689")

# vendored 自 v39-helpers V39_RETRY_SESSION：复用连接 + 连接失败/握手/等响应头超时/
# 429/5xx 指数退避重试（读响应体中途超时不重试，也不按 Retry-After 等待）
_RETRY_SESSION = None
try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    _retry_adapter = HTTPAdapter(max_retries=Retry(
        total=3, connect=3, backoff_factor=0.6, respect_retry_after_header=False,
        status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"]))
    _RETRY_SESSION = requests.Session()
    _RETRY_SESSION.mount("https://", _retry_adapter)
    _RETRY_SESSION.mount("http://", _retry_adapter)
except Exception:                    # 老版本 urllib3 缺 allowed_methods：退化为不重试
    _RETRY_SESSION = None


def normalize_symbol(code):
    """复用 tencent_daily.normalize_symbol（任意写法 → 'sz000001' 式前缀符号）。"""
    from utils.market_sources.tencent_daily import normalize_symbol as _normalize
    return _normalize(code)


def _num(value):
    """可选数值：'1,234.50'→1234.5；空串/'-'/'--'/None→None；布尔/非法→RuntimeError。"""
    if value is None:
        return None
    if isinstance(value, bool):
        raise RuntimeError(f"腾讯分钟线在数值字段给了布尔值 {value!r}")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return float(value)
    text = str(value).replace(",", "").strip()
    if text in ("", "-", "--", "None", "null"):
        return None
    try:
        number = float(text)
    except ValueError as exc:
        raise RuntimeError(f"腾讯分钟线返回了无法识别的数值 {value!r}") from exc
    return number if math.isfinite(number) else None


def _req_num(value, what):
    """必填数值（价格/成交量）：空值/NaN/inf 一律 RuntimeError（fail-closed）。"""
    number = _num(value)
    if number is None:
        raise RuntimeError(f"腾讯分钟线的 {what} 为空或不是有限数值: {value!r}")
    return number


def parse_mkline_payload(payload, symbol, period):
    """离线解析 kline/mkline JSON 载荷 → 契约行列表（不发任何请求）。

    行元素 [时间戳YYYYMMDDHHMM, 开, 收, 高, 低, 量, (额外列…), 换手基点]，
    额外列忽略。输出每行：code/datetime('YYYY-MM-DD HH:MM' 周期结束边界)/
    open/high/low/close/volume(手)/volume_raw/volume_raw_unit/volume_divisor/
    turnover_rate_pct(百分数)/amount=None + amount_available=False + 原因标记。
    格式变异（非对象载荷、缺 symbol 节点、缺 period 列表、行变短/非列表、
    时间戳重复或不可识别、数值不可识别、msg=param error）一律抛错。
    """
    if not isinstance(payload, dict):
        raise RuntimeError(f"腾讯分钟线载荷应为 JSON 对象，实际是 {type(payload).__name__}")
    if payload.get("msg") == "param error":
        raise ValueError("腾讯分钟线 param error：参数错误（代码不存在或 count 越界）")
    data = payload.get("data")
    if not isinstance(data, dict) or symbol not in data:
        raise RuntimeError(f"腾讯分钟线返回里没有 {symbol} 的数据节点，格式可能已变")
    node = data[symbol]
    if not isinstance(node, dict) or not isinstance(node.get(period), list):
        raise RuntimeError(f"腾讯分钟线 {symbol} 的返回里没有 {period} 列表，格式可能已变")
    raw_unit = "shares" if symbol.startswith("sh") and symbol[2:5] in _SHARES_PREFIXES else "lots"
    divisor = 100 if raw_unit == "shares" else 1
    rows, seen = [], set()
    for item in node[period]:
        if not isinstance(item, list) or len(item) < 6:
            raise RuntimeError(f"腾讯分钟线行格式改变（不是 ≥6 元素的数组）: {item!r}")
        try:
            stamp = datetime.strptime(str(item[0]), "%Y%m%d%H%M")
        except ValueError as exc:
            raise RuntimeError(f"腾讯分钟线 {symbol} 时间戳无法识别: {item[0]!r}") from exc
        if stamp in seen:
            raise RuntimeError(f"腾讯分钟线 {symbol} 同一时刻 {stamp} 出现两次，结果不可信")
        seen.add(stamp)
        turnover = _num(item[7]) if len(item) > 7 else None
        rows.append({
            "code": symbol,
            "datetime": stamp.strftime("%Y-%m-%d %H:%M"),
            "open": _req_num(item[1], "open"),
            "high": _req_num(item[3], "high"),
            "low": _req_num(item[4], "low"),
            "close": _req_num(item[2], "close"),
            "volume_raw": _req_num(item[5], "volume"),
            "volume_raw_unit": raw_unit,
            "volume_divisor": divisor,
            "volume": round(_req_num(item[5], "volume") / divisor, 2),
            "volume_unit": "lots",
            # 第 8 个字段是换手率「基点」，÷100 才是百分数；它不是成交额
            "turnover_rate_pct": (round(turnover / 100.0, 6)) if turnover is not None else None,
            # 腾讯分钟线无成交额字段：显式置 None + 标记（调用方估算须另注口径）
            "amount": None,
            "amount_available": False,
            "amount_missing_reason": "tencent_mkline_has_no_amount_field",
            "amount_unit": "CNY",
        })
    rows.sort(key=lambda row: row["datetime"])
    return rows


def _default_transport(url, params, timeout):
    """默认传输：重试会话 GET + 浏览器 UA / Referer（vendored v39 _v39_http 语义）。"""
    if _RETRY_SESSION is not None:
        return _RETRY_SESSION.get(url, params=params, headers=_HEADERS, timeout=timeout)
    import requests
    return requests.get(url, params=params, headers=_HEADERS, timeout=timeout)


def fetch_minute_kline(code, period="m5", count=320,
                       transport=None, timeout=(8, 20), now=time.time):
    """腾讯分钟线（kline/mkline）：proxy 通道优先 + 三入口轮换 + 120s 失败冷却。

    period: m1/m5/m15/m30/m60（不复权；adjust 不适用）；count 1–320（回看窗口
    m5≈6.7 天）；北交所直接 ValueError（腾讯分钟线为空）。被限流入口的典型表现
    （异常/空 JSON/顶层非对象/空 data）→ 标记冷却换下一入口；param error 是参数
    问题 → ValueError 不轮换；三入口均不可用 → RuntimeError。
    transport/now 可注入以便离线测试。返回 dict：symbol/rows/row_count/host/url/
    param/units/lineage（含 fetched_at，UTC-aware；bar 时间按北京时间解释）。
    """
    period = str(period).lower()
    if period not in TENCENT_MINUTE_PERIODS:
        raise ValueError(f"period 只能是 {'/'.join(TENCENT_MINUTE_PERIODS)}")
    if not 1 <= int(count) <= MINUTE_COUNT_MAX:
        raise ValueError(f"分钟线 count 范围 1–{MINUTE_COUNT_MAX}")
    symbol = normalize_symbol(code)
    if symbol.startswith("bj"):
        raise ValueError("腾讯分钟线不支持北交所（腾讯对北交所分钟线为空）；"
                         "北交所分钟数据走 TDX lc5 盘后管道")
    param = f"{symbol},{period},,{int(count)}"
    caller = transport or _default_transport
    errors, tried = [], []
    for host in TENCENT_KLINE_HOSTS:
        if host_down_until.get(host, 0) > now():
            continue
        url = host + MKLINE_PATH
        tried.append(host)
        try:
            response = caller(url, {"param": param}, timeout)
            text = (response.text or "").strip()
            payload = json.loads(text) if text else {}
            if not isinstance(payload, dict):
                raise RuntimeError(f"顶层是 {type(payload).__name__}，不是对象")
        except ValueError as exc:                      # JSON 解析错误属于该入口坏了，换入口
            errors.append(f"{host}: 响应不是 JSON ({exc})")
            host_down_until[host] = now() + HOST_COOLDOWN_SECONDS
            continue
        except Exception as exc:                       # 网络层异常（重试耗尽），换入口
            errors.append(f"{host}: {type(exc).__name__}: {exc}")
            host_down_until[host] = now() + HOST_COOLDOWN_SECONDS
            continue
        if payload.get("msg") == "param error":
            raise ValueError(f"腾讯分钟线 param error：参数错误: {param}")
        data = payload.get("data")
        if isinstance(data, dict) and data:
            rows = parse_mkline_payload(payload, symbol, period)
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
                        "source": "tencent_mkline",
                        "url": url,
                        "param": param,
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                        "row_count": len(rows),
                        "hosts_tried": tried,
                        "note": "腾讯分钟线无成交额；volume 统一为手（688/689 原始为股 ÷100）；"
                                "datetime=周期结束边界，最后一根可能是未收盘 bar",
                    },
                }
            errors.append(f"{host}: data 为空（未上市/停牌/代码有误）")
        else:
            errors.append(f"{host}: 空响应")
        host_down_until[host] = now() + HOST_COOLDOWN_SECONDS
    raise RuntimeError("腾讯分钟线三个入口均不可用（可能被限流，稍后重试）: "
                       + "; ".join(errors))
