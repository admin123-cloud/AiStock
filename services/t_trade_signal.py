# -*- coding: utf-8 -*-
"""R37 L1 半自动做T信号引擎（零自动下单）。

任务书：F:/Aistock/data/data/runtime/task_briefs/20261110_l1_semi_auto_ttrade_r37.md

闭环（R32 调研定案的 L1）：
  09:25 周期判定（R34 量化式，T-1 日线）→ 盘中每 5min 信号节拍（价带+RSI 触发
  + fail-closed 预检门）→ 委托草案落盘 drafts/ + pending_push/（PM watcher 推送
  Owner，THS 手动按键）→ 15:10 当日成交对账（bridge 导出/OCR/文件源）→ 台账
  ledger/ + 日终 summary。

零交易动作、零 formal 表写（全部 runtime/t_tracker 运营层文件）；THS 仅查询/导出。

周期判定与触发参数逐条对应 R34 手册（F:/Aistock/research/R34-做T方法论-三周期手册-2026-11-10.md
§3.0-§3.5）；两票校准带以 R34 §3.5 为锚（R34_CALIB），其余组合按手册通用式派生。
RSI 口径 = Wilder RSI14（镜像 runtime/t_tracker/monitor_rsi.py 权威实现）。

R46 双速制（2026-11-11，任务书 20261111_fast_lane_r46.md）：
  - 快车道 run_fast_tick（1 分钟）：东财 ulist 实时价 + 5m 序列末根（形成中 bar）
    以实时价刷新重估 intrabar RSI5；价带类触发（买带/卖带/目标带/止损线）即产
    fast 草案（trigger_kind 前缀 fast_，bar_status=open，intrabar_rsi5 实值）；
    预检门/布防/冷却与慢车道共用同一 SignalState（完全一致，30min 冷却照旧）；
  - 慢车道 run_beat（5 分钟收线，语义不动）：bar 收线后复核 open 的 fast 草案
    ——成立 → 升级 confirmed_bar；瓦解 → expired_unconfirmed 如实留档；同波
    (code,side,band) 去重并入（不另发）；日终对账汇总 fast 命中率；
  - T 票池 = broker_state 持仓 ∪ holding_t watchlist(manual+ths)（R46）；
  - 东财限频：ulist 票池一次请求（≤10 码/次）；分时K同码同周期 ≤1 次/分钟
    （cached_east_klines TTL 缓存）。
"""
from __future__ import annotations

__version__ = "1.3.0"

import json
import logging
import os
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
import urllib.request

# Asia/Shanghai 固定 +08:00（与 pipeline_scheduler/scheduler.config 一致）
BEIJING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

SCHEMA_REGIME = "t_tracker_regime/1"
SCHEMA_BEAT = "t_tracker_beat/1"
SCHEMA_LEDGER = "t_tracker_ledger/1"

# ── 预检门常量（fail-closed）────────────────────────────────────
DAILY_T_CAP = 2                # 单票单日 T 开仓类草案上限
T_FRACTION = 3                 # T 仓 ≤ 底仓 1/3
LOT = 100                      # A 股整百
TIME_WINDOWS = ((dt_time(9, 35), dt_time(11, 30)), (dt_time(13, 0), dt_time(14, 55)))
FRESHNESS_MAX_SEC = 600        # 分时K最后 bar 距现在 >10min 拒发
COOLDOWN_SEC = 30 * 60         # 同向信号 30min 去重
FALLBACK_EXIT_AFTER = dt_time(14, 50)  # 14:50 仍未达目标 → 兜底离场草案

SIDE_BUY = "buy-T"
SIDE_SELL = "sell-T"
SIDE_REDUCE = "reduce-hint"

# ── R46 快车道常量 ──────────────────────────────────────────────
FAST_LANE = "fast"
BAR_STATUS_OPEN = "open"
BAR_STATUS_CONFIRMED = "confirmed_bar"
BAR_STATUS_EXPIRED = "expired_unconfirmed"
# 价带类触发（1 分钟粒度判定）：买带 / 横盘上沿卖带 / 在途T目标带 / 止损线；
# exit_rsi（RSI 类）与 fallback_eod（时间类）留在慢车道。
FAST_TRIGGER_KINDS = frozenset({"buy_band", "sell_band", "target_band", "stop_break"})
MERGE_WAVE_SEC = 390            # 同波判定窗：fast→slow ≤6.5min（一个 5m bar 波段）
KLT_CACHE_TTL_SEC = 60.0        # 东财分时K限频：同码同周期 ≤1 次/分钟
ULIST_CHUNK = 10                # 东财 ulist 单请求码数上限
SCHEMA_FAST_BEAT = "t_tracker_fast_beat/1"
SCHEMA_L2_DRAFT = "t_tracker_l2_draft/1"

# ── L2 策略常量（2026-11-12，任务书 20261112_l2_integration.md）────
# 15min RSI(14) 为主导 + 5min RSI(14) 为辅助确认。
L2_RSI_PERIOD = 14
L2_COOLDOWN_SEC = 300           # L2 同票同向 5min 去重（30s 心跳快，需缩短）
# 任务书 §7 策略升级：
#   15min RSI>=80 + 5min RSI>=70 → 卖出T（减仓 1/4母仓） → sell-T
#   15min RSI<=20 + 5min RSI<=30 → 买入T（加仓 1/4母仓） → buy-T
#   卖带 75/80 分批 / 买带不高于 35
L2_SELL_RSI15_MIN = 80.0
L2_SELL_RSI5_MIN = 70.0
L2_BUY_RSI15_MAX = 20.0
L2_BUY_RSI5_MAX = 30.0
L2_FRACTION = 4                 # T 仓 ≤ 母仓 1/4（与 L1 的 1/3 不同）
L2_HEAD_SHOULDER_PLACEHOLDER = "STRUCTURE_PLACEHOLDER"

# ── R34 校准参数（2026-11-10，来源 R34 §3.5；作为回归锚）──────────
# R54（2026-11-11 Owner 裁决"卖晚一点、买早一点"）只改上升周期两档：
#   卖侧 65/70 → 阶梯 75 卖半（exit_rsi5_trim_min）→ 80 清余（exit_rsi5_min）；
#   买侧 ≤30 → ≤35 常规（buy_rsi5_max），≤20 确定性档（conviction_rsi5_max，标注用）。
# 横盘/下降条目零改动。
R34_CALIB: dict[str, dict[str, dict[str, Any]]] = {
    "002015": {
        "sideways": {
            "buy_band": [13.00, 13.20], "buy_rsi5_max": 35.0,
            "sell_band": [13.70, 13.90], "sell_rsi5_min": 70.0,
            "exit_rsi5_min": 70.0, "stop": 12.90,
        },
        "up": {"buy_rsi5_max": 35.0, "conviction_rsi5_max": 20.0,
               "vwap_dev_buy_max": -0.005,
               "exit_rsi5_trim_min": 75.0, "exit_rsi5_min": 80.0},
    },
    "300054": {
        "up": {
            "buy_band": [59.90, 60.70], "buy_rsi5_max": 35.0,
            "conviction_rsi5_max": 20.0,
            "sell_band": [63.20, 64.50], "sell_rsi5_min": 80.0,
            "exit_rsi5_trim_min": 75.0, "exit_rsi5_min": 80.0, "stop": 59.30,
        },
        # sideways 不做校准：R34 §3.5 只校准了上升期；横盘按 §3.2 从 20 日区间派生
    },
}
CALIB_SOURCE = "R54-calib-20261111"
DERIVED_SOURCE = "R54-derived"

# R34 锚值（回归对拍用）：交易日 2026-11-10 的 T-1(=11-09 收盘) 判定。
# 002015 取自 R34 报告 §1.4 表 11-10 行；300054 取自 §3.5（多头排列已完成）。
R34_ANCHORS: dict[str, dict[str, Any]] = {
    "002015": {"for_date": "2026-11-10", "regime": "sideways",
               "ma5": 12.99, "ma10": 12.64, "ma20": 13.05, "pos20": 0.67},
    "300054": {"for_date": "2026-11-10", "regime": "up",
               "ma5": 61.48, "ma10": 58.83, "ma20": 58.29, "pos20": 0.75},
}
ANCHOR_MA_TOL = 0.05     # MA 相对容差 ±5%（宽松：R34 300054 含当日晨数据）
ANCHOR_POS_TOL = 0.20    # pos20 绝对容差

# ── 对账常量 ────────────────────────────────────────────────────
MATCH_TIME_WINDOW_MIN = 40      # 草案↔成交 时间窗 ±40min
MATCH_PRICE_TOL_PCT = 0.015     # 价带容差 ±1.5%
# 费率（R34 §0 台账实测推导；bridge 成交行无费用列时估算用）
FEE_BUY_PCT = 0.000186
FEE_SELL_PCT = 0.000666
FEE_MIN_COMMISSION = 5.0

logger = logging.getLogger("t_trade_signal")


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def tracker_dir(out_dir: Optional[Path] = None) -> Path:
    if out_dir is not None:
        return Path(out_dir)
    root = os.getenv("AISTOCK_T_TRACKER_DIR")
    if root:
        return Path(root)
    import sys
    proj = Path(__file__).resolve().parents[1]
    if str(proj) not in sys.path:
        sys.path.insert(0, str(proj))
    from utils.paths import runtime_path
    return runtime_path("t_tracker")


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _load_json(path: Path) -> Optional[dict]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.is_file():
        return rows
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def regime_file_path(root: Path, day: date) -> Path:
    return root / f"regime_{day.strftime('%Y%m%d')}.json"


def drafts_path(root: Path, day: date) -> Path:
    return root / "drafts" / f"{day.strftime('%Y%m%d')}.jsonl"


def state_path(root: Path, day: date) -> Path:
    return root / f"state_{day.strftime('%Y%m%d')}.json"


# ---------------------------------------------------------------------------
# 基础计算（纯函数）
# ---------------------------------------------------------------------------

def rsi_wilder(closes: Sequence[float], period: int = 14) -> Optional[float]:
    """Wilder RSI（镜像 monitor_rsi.py 权威实现；样本不足返回 None）。"""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    ag, al = sum(gains[:period]) / period, sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def compute_regime(bars: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """R34 §3.0 周期判定（纯函数）。

    bars：升序日线 [{date, close, high, low}]，最后一根 = T-1。需 ≥20 根。
    上升 = MA5>MA10>MA20 且 T-1 收盘 ≥ MA5；下降 = MA5<MA10<MA20 且 收盘 ≤ MA5；
    横盘 = 其余一切（含金叉/死叉过渡态）。
    pos20 =（收盘 − lo20）/（hi20 − lo20）。
    """
    if len(bars) < 20:
        raise ValueError(f"regime needs >=20 daily bars, got {len(bars)}")
    closes = [float(b["close"]) for b in bars]
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20
    last = closes[-1]
    window = bars[-20:]
    hi20 = max(float(b["high"]) for b in window)
    lo20 = min(float(b["low"]) for b in window)
    pos20 = (last - lo20) / (hi20 - lo20) if hi20 > lo20 else 0.5
    if ma5 > ma10 > ma20 and last >= ma5:
        regime = "up"
    elif ma5 < ma10 < ma20 and last <= ma5:
        regime = "down"
    else:
        regime = "sideways"
    chg5d = None
    if len(closes) >= 6:
        prev = closes[-6]
        chg5d = (last - prev) / prev * 100 if prev else None
    return {
        "regime": regime, "ma5": round(ma5, 3), "ma10": round(ma10, 3),
        "ma20": round(ma20, 3), "pos20": round(pos20, 4), "hi20": round(hi20, 3),
        "lo20": round(lo20, 3), "close": last, "chg5d_pct": round(chg5d, 2) if chg5d is not None else None,
        "t1_date": str(bars[-1]["date"]),
    }


def check_against_r34_anchor(code: str, info: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """与 R34 锚值回归对拍（仅两票 for_date 匹配时；偏差只标记不拦截）。"""
    anchor = R34_ANCHORS.get(code)
    if not anchor:
        return None
    diffs, ok = {}, True
    for key in ("ma5", "ma10", "ma20"):
        want, got = float(anchor[key]), float(info[key])
        rel = abs(got - want) / want if want else 0.0
        diffs[key] = {"anchor": want, "computed": got, "rel_diff": round(rel, 4)}
        if rel > ANCHOR_MA_TOL:
            ok = False
    pos_diff = abs(float(info["pos20"]) - float(anchor["pos20"]))
    diffs["pos20"] = {"anchor": anchor["pos20"], "computed": info["pos20"], "abs_diff": round(pos_diff, 4)}
    if pos_diff > ANCHOR_POS_TOL:
        ok = False
    regime_match = info["regime"] == anchor["regime"]
    return {
        "anchor_for_date": anchor["for_date"], "regime_anchor": anchor["regime"],
        "regime_computed": info["regime"], "regime_match": regime_match,
        "within_tolerance": ok and regime_match, "details": diffs,
    }


def resolve_params(code: str, regime: str, info: Mapping[str, Any]) -> dict[str, Any]:
    """按 (票, 周期) 解析触发参数：R34 校准带优先，其余按手册通用式派生。"""
    calib = R34_CALIB.get(code, {}).get(regime)
    if calib:
        out = dict(calib)
        out["source"] = CALIB_SOURCE
        return out
    out: dict[str, Any] = {"source": DERIVED_SOURCE}
    if regime == "up":
        # R54（Owner 裁决 2026-11-11，仅上升周期）：
        #   买侧 RSI5m ≤35 常规买；≤20 确定性档（buy_tier=conviction，仅标注+优先级，
        #   同 T 仓上限；R34 实证 ≤20 全年仅 6-14% 交易日出现，稀缺）；
        #   卖侧阶梯 RSI5m ≥75 卖 1/2（trim）→ ≥80 清余仓；VWAP 乖离 ≤ −0.5% 不变。
        out.update({"buy_rsi5_max": 35.0, "conviction_rsi5_max": 20.0,
                    "vwap_dev_buy_max": -0.005,
                    "exit_rsi5_trim_min": 75.0, "exit_rsi5_min": 80.0})
    elif regime == "sideways":
        # R34 §3.2：下沿 [lo20, lo20+0.15rng] + RSI5≤30 + RSI15<40；上沿 [hi20−0.15rng, hi20] + RSI5≥70
        hi20, lo20 = float(info["hi20"]), float(info["lo20"])
        rng = hi20 - lo20
        out.update({
            "buy_band": [round(lo20, 2), round(lo20 + 0.15 * rng, 2)],
            "buy_rsi5_max": 30.0, "buy_rsi15_max": 40.0,
            "sell_band": [round(hi20 - 0.15 * rng, 2), round(hi20, 2)],
            "sell_rsi5_min": 70.0, "exit_rsi5_min": 70.0,
            "stop": round(lo20 * 0.99, 2),
        })
    else:  # down：只发减仓提示（R34 §3.3：RSI5 65-70 + VWAP 乖离 ≥+1.5%）
        out.update({"reduce_rsi5_min": 65.0, "reduce_vwap_dev_min": 0.015})
    return out


def code_with_suffix(code: str) -> str:
    """6 位码 → 带后缀库码（kline_daily/snapshot 口径）。"""
    code = code.strip().split(".")[0]
    if code.startswith(("92", "43", "83")):
        return f"{code}.BJ"
    if code.startswith(("6", "9", "5")):
        return f"{code}.SH"
    return f"{code}.SZ"


def east_secid(code: str) -> str:
    code6 = code.strip().split(".")[0]
    if code6.startswith(("6", "9", "5")):
        return f"1.{code6}"
    return f"0.{code6}"


def closed_bars(bars: Sequence[Mapping[str, Any]], now: datetime) -> list[dict]:
    """只保留已收盘 bar（东财 bar 时间戳 = 周期结束时刻；未来时间戳即未收）。"""
    out = []
    for b in bars:
        ts = parse_ts(str(b["ts"]))
        if ts is not None and ts <= now:
            out.append(dict(b))
    return out


def parse_ts(text: str) -> Optional[datetime]:
    text = text.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19 if ":" in text else 10], fmt)
        except ValueError:
            continue
    return None


def day_vwap(bars5_today: Sequence[Mapping[str, Any]]) -> Optional[float]:
    """当日 VWAP = Σ成交额 / (Σ量(手)×100)（东财 5m：amount 元 / volume 手）。"""
    amt = sum(float(b.get("amount") or 0.0) for b in bars5_today)
    vol = sum(float(b.get("volume") or 0.0) for b in bars5_today)
    if vol <= 0 or amt <= 0:
        return None
    return amt / (vol * 100.0)


# ---------------------------------------------------------------------------
# 数据适配器（默认实现：东财分时K / CH 只读 / broker_state；全部可注入）
# ---------------------------------------------------------------------------

EAST_K_URL = ("http://push2his.eastmoney.com/api/qt/stock/kline/get"
              "?secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f56,f57"
              "&klt={klt}&fqt=1&end=20500101&lmt={lmt}")

def fetch_east_klines(code: str, klt: int, lmt: int = 120) -> list[dict]:
    """东财 push2his 前复权分时K（含当日）：[{ts, open, close, volume(手), amount(元)}]。

    R40 起含 open（f52，影子做T虚拟成交次开价用）；既有消费方按 key 取值不受影响。
    """
    url = EAST_K_URL.format(secid=east_secid(code), klt=klt, lmt=lmt)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    klines = (data.get("data") or {}).get("klines") or []
    out = []
    for row in klines:
        parts = row.split(",")
        if len(parts) < 5:
            continue
        out.append({
            "ts": parts[0],
            "open": float(parts[1]) if parts[1] else float(parts[2]),
            "close": float(parts[2]) if parts[2] else 0.0,
            "volume": float(parts[3]) if parts[3] else 0.0,
            "amount": float(parts[4]) if parts[4] else 0.0,
        })
    return out


# ── R46：东财 ulist 实时价（票池一次请求）+ 分时K TTL 限频缓存 ─────────

EAST_ULIST_URL = ("http://push2.eastmoney.com/api/qt/ulist.np/get"
                  "?secids={secids}&fields=f2,f12,f13,f124&fltt=2")


def fetch_east_ulist_quotes(
    codes: Sequence[str],
    *,
    timeout: float = 10.0,
    url_open: Optional[Callable] = None,
) -> dict[str, dict[str, Any]]:
    """东财 ulist 实时价：{code6: {price, ts(北京时间)}}。

    单次请求 ≤ULIST_CHUNK 码（R46 限频：票池一次请求）；f2 停牌/'-' 跳过；
    f124 = 行情更新 unix 秒 → 北京时间字符串。全部失败由调用方降级链兜底。
    """
    out: dict[str, dict[str, Any]] = {}
    codes6 = [str(c).split(".")[0] for c in codes if c]
    if not codes6:
        return out
    open_ = url_open or urllib.request.urlopen
    for i in range(0, len(codes6), ULIST_CHUNK):
        chunk = codes6[i:i + ULIST_CHUNK]
        secids = ",".join(east_secid(c) for c in chunk)
        req = urllib.request.Request(
            EAST_ULIST_URL.format(secids=secids),
            headers={"User-Agent": "Mozilla/5.0"})
        with open_(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for it in (data.get("data") or {}).get("diff") or []:
            code6 = str(it.get("f12") or "")
            raw_px = it.get("f2")
            if not code6 or raw_px in (None, "", "-"):
                continue
            ts = None
            if it.get("f124"):
                try:
                    ts = fmt_ts(datetime.fromtimestamp(
                        int(it["f124"]), BEIJING_TZ).replace(tzinfo=None))
                except (ValueError, OSError, OverflowError):
                    ts = None
            out[code6] = {"price": float(raw_px), "ts": ts}
    return out


_klt_cache: dict[tuple[str, int], tuple[float, list[dict]]] = {}


def cached_east_klines(
    code: str,
    klt: int,
    *,
    now_ts: Optional[float] = None,
    ttl: float = KLT_CACHE_TTL_SEC,
    fetch: Optional[Callable[[str, int], list[dict]]] = None,
) -> list[dict]:
    """东财分时K TTL 缓存（R46 限频：同码同周期 ≤1 次/分钟）。

    缓存命中返回缓存值（不再请求）；未命中拉取并写缓存。拉取失败异常上抛
    （不回传陈旧缓存——fail-closed，由调用方按票记录失败）。
    """
    key = (str(code), int(klt))
    now_ts = time.time() if now_ts is None else float(now_ts)
    hit = _klt_cache.get(key)
    if hit is not None and now_ts - hit[0] < ttl:
        return hit[1]
    bars = (fetch or fetch_east_klines)(str(code), int(klt))
    _klt_cache[key] = (now_ts, bars)
    return bars


def reset_klt_cache() -> None:
    """清空分时K TTL 缓存（测试用）。"""
    _klt_cache.clear()


EAST_TRENDS_URL = ("http://push2his.eastmoney.com/api/qt/stock/trends2/get"
                   "?secid={secid}&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
                   "&fields2=f51,f52,f53,f54,f55,f56,f57,f58&ndays={ndays}&iscr=0")


def fetch_east_trends_1m(code: str, ndays: int = 2, *,
                         url_open: Optional[Callable] = None,
                         timeout: float = 15.0) -> list[dict]:
    """东财 trends2 分时（1m，近 ndays 日）：[{ts, open, close, volume(手), amount(元)}]。

    R46 回放用——kline/get klt=1 实证只回当日（beg/end/lmt 均不生效），跨日
    1m 须走 trends2；量额口径与 fetch_east_klines 一致（手/元）。
    """
    open_ = url_open or urllib.request.urlopen
    url = EAST_TRENDS_URL.format(secid=east_secid(code), ndays=ndays)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with open_(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out: list[dict] = []
    for row in (data.get("data") or {}).get("trends") or []:
        parts = row.split(",")
        if len(parts) < 7:
            continue
        try:
            open_px = float(parts[1]) if parts[1] and float(parts[1]) > 0 else float(parts[2])
            out.append({
                "ts": parts[0],
                "open": open_px,
                "close": float(parts[2]) if parts[2] else 0.0,
                "volume": float(parts[5]) if parts[5] else 0.0,
                "amount": float(parts[6]) if parts[6] else 0.0,
            })
        except ValueError:
            continue
    return out


def _ch_client():
    import sys
    proj = Path(__file__).resolve().parents[1]
    if str(proj) not in sys.path:
        sys.path.insert(0, str(proj))
    from utils.market_warehouse import clickhouse_client
    return clickhouse_client()


def ch_daily_bars(code: str, as_of: date, limit: int = 30) -> list[dict]:
    """kline_daily 只读：升序 [{date, close, high, low}]。

    严格 trade_date < as_of（T-1 语义）：即使 as_of 当日 bar 已被 16:15 盘后
    管道写入（回放/补跑场景），也不把当日当 T-1 用。
    """
    client = _ch_client()
    res = client.query(
        "SELECT trade_date, close, high, low FROM stock.kline_daily "
        "WHERE code = {c:String} AND trade_date < {d:String} "
        "ORDER BY trade_date DESC LIMIT {n:UInt32}",
        parameters={"c": code_with_suffix(code), "d": as_of.isoformat(), "n": limit},
    )
    rows = [{"date": str(r[0]), "close": float(r[1]), "high": float(r[2]), "low": float(r[3])}
            for r in res.result_rows]
    rows.reverse()
    return rows


def ch_snapshot_price(code: str) -> Optional[dict]:
    """R28 快照表最新价（只读）：{price, snapshot_ts} 或 None。"""
    client = _ch_client()
    res = client.query(
        "SELECT price, snapshot_ts FROM stock.intraday_market_snapshot "
        "WHERE code = {c:String} ORDER BY snapshot_ts DESC LIMIT 1",
        parameters={"c": code_with_suffix(code)},
    )
    if not res.result_rows:
        return None
    price, ts = res.result_rows[0]
    if price is None:
        return None
    return {"price": float(price), "snapshot_ts": str(ts).replace("T", " ")[:16]}


def _res_key(code: str, holding: Mapping[str, Any]) -> str:
    """beat 报告键：acct1/空=legacy 纯码键；其他账号=码:账号（同码双账号不互相覆盖）。"""
    account = str((holding or {}).get("account") or "acct1")
    return code if account in ("", "acct1") else f"{code}:{account}"


def load_broker_holdings() -> dict[str, Any]:
    """票池 = broker_state（THS 持仓，holding_source=ths_account_bridge）。

    R47 多账号：accounts.<acctN> 在场时按账号展开——同码两账号=两行
    （shares=该账号份额、account=acctN），1/3 底仓限额天然分账号。
    单账号 legacy 形态=原行透传+account="acct1"（数量口径零变化）。
    返回 {holdings: [...], updated_at, holding_source, path}；
    文件缺失/不可解析 → 抛异常（fail-closed，不猜持仓）。
    """
    import sys
    proj = Path(__file__).resolve().parents[1]
    if str(proj) not in sys.path:
        sys.path.insert(0, str(proj))
    from utils.paths import runtime_path
    path = runtime_path("gen3_state_alpha", "broker_state.json")
    data = _load_json(Path(path))
    if not data or not isinstance(data.get("holdings"), list):
        raise RuntimeError(f"broker_state unreadable: {path}")
    from services.broker_state_autosync import union_holdings

    rows: list[dict[str, Any]] = []
    for row in union_holdings(data):
        if not row.get("code") or int(row.get("shares") or 0) <= 0:
            continue
        detail = row.get("account_detail") if isinstance(row.get("account_detail"), dict) else {}
        if detail:
            for acct in sorted(detail):
                if int(detail[acct] or 0) > 0:
                    rows.append({**row, "shares": int(detail[acct]), "account": acct})
        else:
            rows.append({**row, "account": "acct1"})
    if not rows:
        raise RuntimeError(f"broker_state holdings empty: {path}")
    return {
        "holdings": rows,
        "updated_at": str(data.get("updated_at") or ""),
        "holding_source": str(data.get("holding_source") or ""),
        "path": str(path),
    }


def _default_watchlist_paths() -> tuple[Path, Path]:
    import sys
    proj = Path(__file__).resolve().parents[1]
    if str(proj) not in sys.path:
        sys.path.insert(0, str(proj))
    from utils.paths import runtime_path
    store = runtime_path("gen3_state_alpha", "holding_t_watchlist",
                         "holding_tick_watchlist.json")
    legacy = runtime_path("gen3_state_alpha", "holding_tick_watchlist.json")
    return Path(store), Path(legacy)


def load_t_pool(*, watchlist_store: Optional[Path] = None) -> dict[str, Any]:
    """T 票池（R46）= broker_state 持仓 ∪ holding_t watchlist(manual+ths)。

    broker_state 不可读 → 抛异常（fail-closed 同现行：不猜持仓）；watchlist
    缺件/不可读 → 降级仅持仓（辅助源，不阻断）。watchlist 独有码 shares=qty
    （未填=0：观察票——照常参与判定与预检门，底仓 0 → t_lot_below_min 拒发
    草案，绝不猜底仓）。持仓已有的码以 broker_state 为准（watchlist ths 同步
    项与其重复时去重）。
    """
    pool = load_broker_holdings()
    try:
        store, legacy = (watchlist_store, None) if watchlist_store is not None \
            else _default_watchlist_paths()
        items: list[dict[str, Any]] = []
        if store.exists():
            import sys
            proj = Path(__file__).resolve().parents[1]
            if str(proj) not in sys.path:
                sys.path.insert(0, str(proj))
            from services.holding_t_watchlist import read_watchlist
            items = list(read_watchlist(store, legacy_path=legacy).get("items") or [])
    except Exception as exc:  # noqa: BLE001 - watchlist 为辅助源：降级仅持仓
        logger.warning("[pool] watchlist 读取失败，降级仅持仓：%s", exc)
        items = []
    if not items:
        return pool
    held = {str(h.get("code") or "").split(".")[0]: h for h in pool["holdings"]}
    merged = list(pool["holdings"])
    added: list[str] = []
    for it in items:
        code6 = str(it.get("code") or "").split(".")[0]
        if not code6 or code6 in held:
            continue
        try:
            shares = int(it.get("qty") or 0)
        except (TypeError, ValueError):
            shares = 0
        merged.append({"code": code6, "name": str(it.get("name") or ""),
                       "shares": shares,
                       "source": f"watchlist:{it.get('source') or 'manual'}"})
        added.append(code6)
    if not added:
        return pool
    out = dict(pool)
    out["holdings"] = merged
    out["pool_source"] = "broker_state+watchlist"
    out["watchlist_added"] = added
    return out


# ---------------------------------------------------------------------------
# 09:25 周期判定任务
# ---------------------------------------------------------------------------

def _regime_breadth_annotation(
    codes: dict,
    breadth_loader: Optional[Callable[[], Optional[dict]]],
    log: logging.Logger,
) -> dict:
    """R55（2026-11-11）：regime json 增 breadth 水位确认因子（观察期）。

    只标注 divergence 提示，不改判定式；宽度不可用/计算失败 → status=
    unavailable（fail-closed，绝不阻断周期判定本身）。
    """
    unavailable = {"status": "unavailable",
                   "note": "宽度不可用（fail-closed），未标注；观察期，不改判定式"}
    try:
        from services.market_breadth import breadth_water_annotation, market_breadth
    except Exception as exc:  # noqa: BLE001
        log.warning("[regime] breadth 模块不可用：%s", exc)
        return unavailable
    loader = breadth_loader or market_breadth
    try:
        breadth = loader() or None
    except Exception as exc:  # noqa: BLE001
        log.warning("[regime] breadth 计算失败：%s", exc)
        breadth = None
    regimes = {code: str(info.get("regime") or "") for code, info in codes.items()}
    return breadth_water_annotation(breadth, regimes)


def run_regime(
    *,
    for_date: date,
    now: datetime,
    out_dir: Optional[Path] = None,
    holdings_loader: Optional[Callable[[], dict]] = None,
    daily_bars_loader: Optional[Callable[[str, date], list[dict]]] = None,
    breadth_loader: Optional[Callable[[], Optional[dict]]] = None,
    log: Optional[logging.Logger] = None,
) -> dict:
    """盘前周期判定：T-1 日线 → regime_<date>.json（含解析后的触发参数）。"""
    log = log or logger
    root = tracker_dir(out_dir)
    load_holdings = holdings_loader or load_t_pool   # R46：T 票池并集
    load_bars = daily_bars_loader or ch_daily_bars

    pool = load_holdings()
    codes: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for holding in pool["holdings"]:
        code = str(holding["code"]).split(".")[0]
        try:
            bars = load_bars(code, for_date)
            info = compute_regime(bars)
            info.update({
                "code": code, "name": holding.get("name") or "",
                "data_rows": len(bars),
                "params": resolve_params(code, info["regime"], info),
            })
            anchor = check_against_r34_anchor(code, info)
            if anchor is not None and anchor["anchor_for_date"] == for_date.isoformat():
                info["anchor_check"] = anchor
            codes[code] = info
        except Exception as exc:  # noqa: BLE001 - 单票失败如实记录
            errors[code] = f"{type(exc).__name__}: {exc}"
            log.error("[regime] %s 判定失败：%s", code, errors[code])
    if not codes and errors:
        return {"schema": SCHEMA_REGIME, "date": for_date.isoformat(),
                "status": "failure", "errors": errors,
                # R55：失败路径同样带 breadth 键（schema 稳定；fail-safe）
                "breadth": _regime_breadth_annotation({}, breadth_loader, log)}
    payload = {
        "schema": SCHEMA_REGIME, "version": __version__,
        "date": for_date.isoformat(), "generated_at": fmt_ts(now),
        "status": "ok" if not errors else "partial",
        "pool": {"source": pool.get("pool_source") or "broker_state",
                 "updated_at": pool.get("updated_at"),
                 "holding_source": pool.get("holding_source"), "path": pool.get("path")},
        "codes": codes, "errors": errors,
    }
    # R55：breadth 水位确认因子（观察期，只标注 divergence 不改判定式；fail-safe）
    payload["breadth"] = _regime_breadth_annotation(codes, breadth_loader, log)
    _atomic_write_json(regime_file_path(root, for_date), payload)
    log.info("[regime] %s 完成：%s", for_date,
             {c: v["regime"] for c, v in codes.items()})
    return payload


def fmt_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 信号节拍（盘中）
# ---------------------------------------------------------------------------

def in_time_window(now: datetime) -> bool:
    t = now.time()
    return any(start <= t <= end for start, end in TIME_WINDOWS)


def t_lot_cap(base_shares: int) -> int:
    """T 仓上限 = 底仓 1/3 向下取整百（2000→600；<300 → 0=不可做T）。"""
    return (int(base_shares) // T_FRACTION // LOT) * LOT


class SignalState:
    """布防/冷却/在途T状态（每日一档；fire 后解除布防，条件离开重布防）。"""

    def __init__(self, data: Optional[dict] = None):
        self.data: dict[str, Any] = data or {}

    def code_state(self, code: str) -> dict:
        return self.data.setdefault(code, {})

    def side_state(self, code: str, group: str) -> dict:
        st = self.code_state(code).setdefault("sides", {}).setdefault(group, {})
        st.setdefault("armed", True)
        st.setdefault("last_fire", None)
        return st

    def open_t(self, code: str) -> Optional[dict]:
        return self.code_state(code).get("open_t")

    def set_open_t(self, code: str, value: Optional[dict]) -> None:
        if value is None:
            self.code_state(code).pop("open_t", None)
        else:
            self.code_state(code)["open_t"] = value


def load_state(root: Path, day: date) -> SignalState:
    data = _load_json(state_path(root, day))
    return SignalState(data or {})


def save_state(root: Path, day: date, state: SignalState) -> None:
    _atomic_write_json(state_path(root, day), state.data)


def cooldown_ok(side_state: Mapping[str, Any], now: datetime) -> bool:
    last = side_state.get("last_fire")
    if not last:
        return True
    ts = parse_ts(str(last))
    if ts is None:
        return True
    return (now - ts).total_seconds() >= COOLDOWN_SEC


def _regime_side_gate(regime: str, side: str, subtype: str) -> tuple[bool, str]:
    """周期×方向门：下降禁买侧草案；上升禁新的卖T开仓（正T离场/75 档减半除外）。"""
    if regime == "down" and side in (SIDE_BUY,):
        return False, "down_regime_no_buy"
    if regime == "up" and side == SIDE_SELL and subtype not in ("exit", "trim"):
        return False, "up_regime_no_new_sellT"
    return True, ""


def evaluate_code(
    *,
    code: str,
    name: str,
    regime_info: Mapping[str, Any],
    params: Mapping[str, Any],
    bars5: Sequence[Mapping[str, Any]],
    bars15: Sequence[Mapping[str, Any]],
    px: float,
    px_source: str,
    now: datetime,
    state: SignalState,
    drafts_today: Sequence[Mapping[str, Any]],
    holding: Mapping[str, Any],
    holdings_updated_at: str = "",
    allowed_trigger_kinds: Optional[frozenset] = None,
) -> dict[str, Any]:
    """单票一拍：触发判定 + 预检门 → 草案（或抑制原因）。纯逻辑、零 IO。

    allowed_trigger_kinds（R46 快车道用）：非 None 时只保留指定 trigger_kind 的
    候选（价带类）；None = 不过滤（慢车道现行语义，不动）。
    """
    regime = str(regime_info["regime"])
    closes5 = [float(b["close"]) for b in bars5]
    closes15 = [float(b["close"]) for b in bars15]
    rsi5 = rsi_wilder(closes5)
    rsi15 = rsi_wilder(closes15)
    today_str = now.strftime("%Y-%m-%d")
    bars5_today = [b for b in bars5 if str(b["ts"]).startswith(today_str)]
    vwap = day_vwap(bars5_today)
    vwap_dev = (px - vwap) / vwap if (vwap and vwap > 0) else None
    last5_ts = parse_ts(str(bars5[-1]["ts"])) if bars5 else None
    freshness_sec = (now - last5_ts).total_seconds() if last5_ts else None

    result: dict[str, Any] = {
        "code": code, "name": name, "ts": fmt_ts(now), "regime": regime,
        "px": px, "px_source": px_source, "rsi5": round(rsi5, 1) if rsi5 is not None else None,
        "rsi15": round(rsi15, 1) if rsi15 is not None else None,
        "vwap": round(vwap, 3) if vwap else None,
        "vwap_dev_pct": round(vwap_dev * 100, 2) if vwap_dev is not None else None,
        "last5m_ts": str(bars5[-1]["ts"]) if bars5 else None,
        "freshness_sec": freshness_sec,
        "draft": None, "suppressed": None,
    }

    def suppress(reason: str, detail: Optional[dict] = None) -> None:
        result["suppressed"] = {"reason": reason, **(detail or {})}

    if rsi5 is None or rsi15 is None:
        suppress("rsi_insufficient_data")
        return result

    # ── 触发条件（与 R34 手册逐条对应；价带+RSI 阈为硬条件，量能/乖离仅记录）──
    open_t = state.open_t(code)
    candidates: list[dict[str, Any]] = []
    if open_t:
        target = open_t.get("target_band")
        hit_target = bool(target and float(target[0]) <= px <= float(target[1]))
        hit_exit_rsi = rsi5 >= float(params.get("exit_rsi5_min") or 1e9)
        # R54 阶梯第一档：≥trim_min 且尚未减半 → 卖 1/2（elif 链保证 ≥exit_min 时
        # 全清优先，绝不出现"该清仓却只卖半"）
        trim_min = params.get("exit_rsi5_trim_min")
        hit_trim_rsi = bool(
            trim_min and not open_t.get("trimmed")
            and rsi5 >= float(trim_min))
        stop_px = open_t.get("stop")
        hit_stop = bool(stop_px and px <= float(stop_px))
        fallback = now.time() >= FALLBACK_EXIT_AFTER
        if hit_stop:
            candidates.append({"side": SIDE_SELL, "subtype": "stop", "priority": 0,
                               "trigger_kind": "stop_break"})
        elif hit_target or hit_exit_rsi:
            candidates.append({"side": SIDE_SELL, "subtype": "exit", "priority": 1,
                               "trigger_kind": "target_band" if hit_target else "exit_rsi"})
        elif hit_trim_rsi:
            candidates.append({"side": SIDE_SELL, "subtype": "trim", "priority": 2,
                               "trigger_kind": "exit_rsi"})
        elif fallback:
            candidates.append({"side": SIDE_SELL, "subtype": "fallback_eod", "priority": 2,
                               "trigger_kind": "fallback_eod"})
    else:
        if regime in ("up", "sideways"):
            buy_band = params.get("buy_band")
            in_buy_band = bool(buy_band and float(buy_band[0]) <= px <= float(buy_band[1]))
            vwap_ok = True
            if not buy_band and params.get("vwap_dev_buy_max") is not None:
                vwap_ok = vwap_dev is not None and vwap_dev <= float(params["vwap_dev_buy_max"])
            rsi15_ok = True
            if params.get("buy_rsi15_max") is not None:
                rsi15_ok = rsi15 < float(params["buy_rsi15_max"])
            if (in_buy_band or (not buy_band and vwap_ok)) and rsi5 <= float(params["buy_rsi5_max"]) and rsi15_ok:
                # R54 买侧分层：≤conviction_rsi5_max → 确定性档（仅标注+优先提示，
                # 硬条件与 1x T 仓上限不变）；其余 ≤buy_rsi5_max → 常规档
                conv_max = params.get("conviction_rsi5_max")
                tier = None
                if conv_max is not None:
                    tier = "conviction" if rsi5 <= float(conv_max) else "normal"
                candidates.append({"side": SIDE_BUY, "subtype": "entry", "priority": 3,
                                   "trigger_kind": "buy_band" if buy_band else "vwap_dev",
                                   "buy_tier": tier})
        if regime == "sideways":
            sell_band = params.get("sell_band")
            if sell_band and float(sell_band[0]) <= px <= float(sell_band[1]) and rsi5 >= float(params.get("sell_rsi5_min") or 1e9):
                candidates.append({"side": SIDE_SELL, "subtype": "zone", "priority": 4,
                                   "trigger_kind": "sell_band"})
        if regime == "down":
            r = params.get("reduce_rsi5_min")
            v = params.get("reduce_vwap_dev_min")
            if r and rsi5 >= float(r) and vwap_dev is not None and vwap_dev >= float(v):
                candidates.append({"side": SIDE_REDUCE, "subtype": "hint", "priority": 5,
                                   "trigger_kind": "rebound_reduce"})
    candidates.sort(key=lambda c: c["priority"])
    # R46 快车道：只保留价带类候选（None = 慢车道不过滤，语义不动）
    if allowed_trigger_kinds is not None:
        candidates = [c for c in candidates
                      if c["trigger_kind"] in allowed_trigger_kinds]

    # 条件离开 → 重布防（镜像 PM 监控离场重布防语义）：
    # 各布防组（open/exit/hint）的触发条件当前不再成立时恢复 armed。
    still_hot = _groups_still_hot(code, regime, params, px, rsi5, rsi15, vwap_dev, open_t, now)
    for group, hot in still_hot.items():
        st = state.side_state(code, group)
        if not hot:
            st["armed"] = True

    if not candidates:
        # L2: 15min RSI 警戒 → 5min RSI 跌破/升破 触发
        l2_cands = _l2_rsi_candidates(rsi5, rsi15, state, code, now, bars5=bars5)
        if l2_cands:
            candidates.extend(l2_cands)
    if not candidates:
        return result

    base_shares = int(holding.get("shares") or 0)
    lot = t_lot_cap(base_shares)
    count_today = len([d for d in drafts_today
                       if d.get("code") == code and d.get("subtype") in ("entry", "zone", "hint")])

    for cand in candidates:
        side, subtype = cand["side"], cand["subtype"]
        gate_ok, gate_reason = _regime_side_gate(regime, side, subtype)
        if not gate_ok:
            suppress(gate_reason, {"side": side, "subtype": subtype})
            continue
        # 布防组镜像 PM 监控的独立布防语义：买带/上沿卖带/离场/减仓提示各自独立；
        # R54 trim 独立成组——75 档减半的去布防/冷却绝不能拖住 80 档清仓线
        if subtype in ("exit", "stop", "fallback_eod"):
            group = "exit"
        elif subtype == "trim":
            group = "trim"
        elif side == SIDE_REDUCE:
            group = "hint"
        elif subtype == "zone":
            group = "sell_zone"
        else:
            group = "buy"
        st = state.side_state(code, group)
        if not st["armed"]:
            suppress("disarmed_wait_rearm", {"side": side, "subtype": subtype})
            continue
        if not cooldown_ok(st, now):
            suppress("cooldown_30min", {"side": side, "subtype": subtype})
            continue
        # ── 预检门（fail-closed；任一不过即拒发草案）──
        if code != str(holding.get("code", code)).split(".")[0] or base_shares <= 0:
            suppress("not_in_pool", {"side": side})
            continue
        if subtype in ("entry", "zone", "hint") and count_today >= DAILY_T_CAP:
            suppress("daily_t_cap", {"side": side, "count_today": count_today, "cap": DAILY_T_CAP})
            continue
        if lot < LOT:
            suppress("t_lot_below_min", {"base_shares": base_shares, "lot": lot})
            continue
        if not in_time_window(now):
            suppress("outside_time_window", {"windows": [[fmt_t(a), fmt_t(b)] for a, b in TIME_WINDOWS]})
            continue
        if freshness_sec is None or freshness_sec > FRESHNESS_MAX_SEC:
            suppress("stale_minute_data", {"freshness_sec": freshness_sec,
                                           "max_sec": FRESHNESS_MAX_SEC})
            continue

        qty = lot
        target_band = None
        stop = params.get("stop")
        if subtype in ("exit", "stop", "fallback_eod") and open_t:
            qty = int(open_t.get("qty") or lot)
            target_band = open_t.get("target_band")
            stop = open_t.get("stop")
        elif subtype == "trim" and open_t:
            # R54：卖在途 T 的 1/2，向下取整百；不足一手 → fail-closed 拒发
            full_qty = int(open_t.get("qty") or 0)
            qty = full_qty // 2 // LOT * LOT
            if qty < LOT:
                suppress("trim_lot_below_min",
                         {"open_qty": full_qty, "half_lot": qty, "lot": LOT})
                continue
            target_band = open_t.get("target_band")
            stop = open_t.get("stop")
        elif side == SIDE_BUY:
            target_band = params.get("sell_band")
        elif subtype == "zone":
            target_band = params.get("buy_band")

        draft_id = make_draft_id(now, code, side, drafts_today)
        draft = {
            "schema": SCHEMA_BEAT, "id": draft_id, "ts": fmt_ts(now),
            "code": code, "name": name, "side": side, "subtype": subtype,
            "account": str(holding.get("account") or "acct1"),  # R47：同码分账号草案
            "regime": regime, "params_source": params.get("source"),
            "px": px, "px_source": px_source,
            "trigger": {
                "kind": cand["trigger_kind"],
                "band": params.get("buy_band") if side == SIDE_BUY else (
                    open_t.get("target_band") if subtype in ("exit", "fallback_eod") else params.get("sell_band")),
                "rsi5": round(rsi5, 1), "rsi15": round(rsi15, 1),
                "vwap": round(vwap, 3) if vwap else None,
                "vwap_dev_pct": round(vwap_dev * 100, 2) if vwap_dev is not None else None,
            },
            "qty": qty, "target_band": target_band, "stop": stop,
            "open_t_ref": open_t["draft_id"] if (open_t and subtype in ("exit", "stop", "fallback_eod", "trim")) else None,
            "precheck": {
                "pool": {"shares": base_shares, "lot_cap": lot,
                         "account": str(holding.get("account") or "acct1"),
                         "holdings_updated_at": holdings_updated_at},
                "daily_count_before": count_today, "cap": DAILY_T_CAP,
                "time_window": fmt_t(now.time()), "freshness_sec": round(freshness_sec or 0, 1),
                "cooldown_group": group,
            },
        }
        # R54 买侧分层标注（仅上升周期带 conviction_rsi5_max 参数的草案）
        tier = cand.get("buy_tier")
        if subtype == "entry" and tier:
            draft["buy_tier"] = tier
            if tier == "conviction":
                draft["conviction"] = True
                draft["buy_tier_note"] = (
                    "conviction_buy：RSI5≤20 确定性档（优先级最高，同 1x T 仓上限）；"
                    "R34 实证 ≤20 全年仅 6-14% 交易日出现，稀缺")
        st["armed"] = False
        st["last_fire"] = fmt_ts(now)
        if subtype == "entry":
            state.set_open_t(code, {
                "draft_id": draft_id, "entry_px": px, "qty": qty,
                "target_band": params.get("sell_band"), "stop": params.get("stop"),
                "ts": fmt_ts(now),
            })
        elif subtype == "trim":
            # R54 阶梯第一档成交入草案：在途 T 减半续存（trimmed 防二次减半）
            remaining = int(open_t["qty"]) - qty
            state.set_open_t(code, {**open_t, "qty": remaining, "trimmed": True,
                                    "trim_ts": fmt_ts(now), "trim_ref": draft_id})
        elif subtype in ("exit", "stop", "fallback_eod"):
            state.set_open_t(code, None)
        result["draft"] = draft
        return result
    return result


def fmt_t(t: dt_time) -> str:
    return t.strftime("%H:%M")


def make_draft_id(now: datetime, code: str, side: str, drafts_today: Sequence[Mapping[str, Any]]) -> str:
    side_short = {"buy-T": "buy", "sell-T": "sell", "reduce-hint": "red"}[side]
    existing = {d.get("id") for d in drafts_today}
    seq = len([d for d in drafts_today if d.get("code") == code]) + 1
    draft_id = f"T{now.strftime('%Y%m%d')}_{code}_{side_short}_{seq:02d}"
    while draft_id in existing:
        seq += 1
        draft_id = f"T{now.strftime('%Y%m%d')}_{code}_{side_short}_{seq:02d}"
    return draft_id


def _groups_still_hot(code, regime, params, px, rsi5, rsi15, vwap_dev, open_t, now) -> dict[str, bool]:
    """各布防组的触发条件当前是否仍成立（不成立 → 允许重布防）。"""
    hot: dict[str, bool] = {}
    buy_band = params.get("buy_band")
    buy_hot = False
    if buy_band:
        buy_hot = float(buy_band[0]) <= px <= float(buy_band[1]) and rsi5 <= float(params.get("buy_rsi5_max") or 1e9)
    elif params.get("vwap_dev_buy_max") is not None:
        buy_hot = vwap_dev is not None and vwap_dev <= float(params["vwap_dev_buy_max"]) and rsi5 <= float(params.get("buy_rsi5_max") or 1e9)
    sell_band = params.get("sell_band")
    sell_hot = bool(
        regime == "sideways" and sell_band
        and float(sell_band[0]) <= px <= float(sell_band[1])
        and rsi5 >= float(params.get("sell_rsi5_min") or 1e9))
    # 布防组镜像 PM 监控：买带/上沿卖带各自独立"条件离开→重布防"
    hot["buy"] = buy_hot
    hot["sell_zone"] = sell_hot
    hot["exit"] = bool(open_t) or bool(
        regime == "sideways" and sell_band
        and float(sell_band[0]) <= px <= float(sell_band[1])
        and rsi5 >= float(params.get("sell_rsi5_min") or 1e9))
    # R54 trim 组独立布防：在途 T 未减半且 RSI 仍 ≥75 档 = 条件成立（防重布防重复减半；
    # trimmed 标志是第二道闸）
    trim_min = params.get("exit_rsi5_trim_min")
    hot["trim"] = bool(
        open_t and not open_t.get("trimmed") and trim_min
        and rsi5 >= float(trim_min))
    hot["hint"] = bool(
        regime == "down" and params.get("reduce_rsi5_min")
        and rsi5 >= float(params["reduce_rsi5_min"]))
    # L2: 15min RSI 警戒 —— ≥80时 warm，跌破80→冷（可触发卖出）
    hot["rsi_sell"] = bool(rsi15 is not None and rsi15 >= 65.0)
    # L2: 15min RSI ≤30 时 cold_warn，升破30→暖（可触发买入）
    hot["rsi_buy"] = bool(rsi15 is not None and rsi15 <= 30.0)
    return hot


def _l2_rsi_candidates(
    rsi5: Optional[float],
    rsi15: Optional[float],
    state: 'BeatState',
    code: str,
    now: datetime,
    bars5: Optional[list[dict]] = None,
) -> list[dict]:
    """L2 15min RSI 警戒触发：进入警戒区后，以下拐点信号任一先到即触发:
    - 5min RSI 跌破 80 / 升破 20
    - 5min 3K 见顶形态（长上影/阴包阳/黄昏星）
    - 5min 3K 见底形态（长下影/阳包阴/启明星）
    - 15min RSI 跌破 80 / 升破 20
    """
    cands: list[dict] = []
    sell_st = state.side_state(code, "rsi_sell")
    buy_st = state.side_state(code, "rsi_buy")

    # ── 5min MA5 计算（取最近5根close均线） ──
    ma5 = None
    if bars5 and len(bars5) >= 5:
        closes5 = [float(b.get("close", 0)) for b in bars5[-5:]]
        ma5 = sum(closes5) / 5

    # ── 卖出拐点（15min RSI≥65 后） ──
    is_rsi15_warn = rsi15 is not None and rsi15 >= 65.0
    sold = False
    if sell_st.get("armed"):
        # ① 5min RSI 从警戒后峰值回落 ≥ 10
        rsi5_peak = sell_st.get("rsi5_peak") or 0.0
        if rsi5 is not None:
            if rsi5 > rsi5_peak:
                rsi5_peak = rsi5
        if not sold and rsi5 is not None and rsi5_peak >= 70.0 and rsi5_peak - rsi5 >= 10.0:
            cands.append({"side": "sell", "subtype": "exit", "priority": 2,
                          "trigger_kind": "l2_rsi5_rollback"})
            sold = True
        # ② 5min 3K 见顶形态
        if not sold and bars5 and len(bars5) >= 3:
            if _three_k_top(bars5[-3:]):
                cands.append({"side": "sell", "subtype": "exit", "priority": 2,
                              "trigger_kind": "l2_3k_top"})
                sold = True
        # ③ 5min 收盘价跌破 MA5
        if not sold and ma5 is not None and bars5:
            close5 = float(bars5[-1].get("close", 0))
            if close5 < ma5:
                cands.append({"side": "sell", "subtype": "exit", "priority": 2,
                              "trigger_kind": "l2_price_break_ma5"})
                sold = True
    # 持续追踪峰值
    if rsi5 is not None and sell_st.get("armed") and not sold:
        old_peak = sell_st.get("rsi5_peak") or 0.0
        if rsi5 > old_peak:
            sell_st["rsi5_peak"] = rsi5

    # ── 买入拐点（15min RSI≤30 后） ──
    is_rsi15_cold = rsi15 is not None and rsi15 <= 30.0
    bought = False
    if buy_st.get("armed"):
        # ① 5min RSI 从警戒后谷底回升 ≥ 10
        rsi5_trough = buy_st.get("rsi5_trough") or 999.0
        if rsi5 is not None:
            if rsi5 < rsi5_trough:
                rsi5_trough = rsi5
        if not bought and rsi5 is not None and rsi5_trough <= 30.0 and rsi5 - rsi5_trough >= 10.0:
            cands.append({"side": "buy", "subtype": "entry", "priority": 2,
                          "trigger_kind": "l2_rsi5_rollup"})
            bought = True
        # ② 5min 3K 见底形态
        if not bought and bars5 and len(bars5) >= 3:
            if _three_k_bottom(bars5[-3:]):
                cands.append({"side": "buy", "subtype": "entry", "priority": 2,
                              "trigger_kind": "l2_3k_bottom"})
                bought = True
        # ③ 5min 收盘价升破 MA5
        if not bought and ma5 is not None and bars5:
            close5 = float(bars5[-1].get("close", 0))
            if close5 > ma5:
                cands.append({"side": "buy", "subtype": "entry", "priority": 2,
                              "trigger_kind": "l2_price_rise_ma5"})
                bought = True
    # 持续追踪谷底
    if rsi5 is not None and buy_st.get("armed") and not bought:
        old_trough = buy_st.get("rsi5_trough") or 999.0
        if rsi5 < old_trough:
            buy_st["rsi5_trough"] = rsi5

    return cands


def _three_k_top(bars3: list[dict]) -> bool:
    """5min 3K 见顶形态（任一成立）：
    A. 上影线见顶：连续3根，每根上影线≥实体2倍，且高点依次降低
    B. 阴包阳：阳线后阴线完全覆盖阳线实体
    C. 黄昏星：长阳→小实体→长阴，阴线实体≥阳线实体50%
    """
    if len(bars3) < 3:
        return False
    def body(bar): return abs(bar.get('close', 0) - bar.get('open', 0))
    def upper(bar): return bar.get('high', 0) - max(bar.get('close', 0), bar.get('open', 0))
    def lower(bar): return min(bar.get('close', 0), bar.get('open', 0)) - bar.get('low', 0)
    def is_up(bar): return bar.get('close', 0) > bar.get('open', 0)
    def is_down(bar): return bar.get('close', 0) < bar.get('open', 0)
    b0, b1, b2 = bars3[0], bars3[1], bars3[2]
    bo0, bo1, bo2 = body(b0), body(b1), body(b2)
    avg_body = max((bo0 + bo1 + bo2) / 3, 0.01)
    # A. 上影线见顶
    if all(upper(b) >= 2 * body(b) for b in bars3)\
       and bars3[0].get('high', 0) > bars3[1].get('high', 0) > bars3[2].get('high', 0):
        return True
    # B. 阴包阳
    if is_up(b1) and is_down(b2) and bo2 > bo1 and b2.get('close', 0) < b1.get('open', 0):
        return True
    # C. 黄昏星
    if is_up(b0) and bo0 >= 1.5 * avg_body \
       and bo1 <= 0.3 * avg_body \
       and is_down(b2) and bo2 >= 0.5 * bo0:
        return True
    return False


def _three_k_bottom(bars3: list[dict]) -> bool:
    """5min 3K 见底形态（任一成立）：
    A. 下影线见底：连续3根，每根下影线≥实体2倍，且低点依次抬高
    B. 阳包阴：阴线后阳线完全覆盖阴线实体
    C. 启明星：长阴→小实体→长阳，阳线实体≥阴线实体50%
    """
    if len(bars3) < 3:
        return False
    def body(bar): return abs(bar.get('close', 0) - bar.get('open', 0))
    def upper(bar): return bar.get('high', 0) - max(bar.get('close', 0), bar.get('open', 0))
    def lower(bar): return min(bar.get('close', 0), bar.get('open', 0)) - bar.get('low', 0)
    def is_up(bar): return bar.get('close', 0) > bar.get('open', 0)
    def is_down(bar): return bar.get('close', 0) < bar.get('open', 0)
    b0, b1, b2 = bars3[0], bars3[1], bars3[2]
    bo0, bo1, bo2 = body(b0), body(b1), body(b2)
    avg_body = max((bo0 + bo1 + bo2) / 3, 0.01)
    # A. 下影线见底
    if all(lower(b) >= 2 * body(b) for b in bars3)\
       and bars3[0].get('low', 1e9) < bars3[1].get('low', 1e9) < bars3[2].get('low', 1e9):
        return True
    # B. 阳包阴
    if is_down(b1) and is_up(b2) and bo2 > bo1 and b2.get('close', 0) > b1.get('open', 0):
        return True
    # C. 启明星
    if is_down(b0) and bo0 >= 1.5 * avg_body \
       and bo1 <= 0.3 * avg_body \
       and is_up(b2) and bo2 >= 0.5 * bo0:
        return True
    return False


def run_beat(
    *,
    now: datetime,
    out_dir: Optional[Path] = None,
    holdings_loader: Optional[Callable[[], dict]] = None,
    regime_loader: Optional[Callable[[date], Optional[dict]]] = None,
    bars5_loader: Optional[Callable[[str], list[dict]]] = None,
    bars15_loader: Optional[Callable[[str], list[dict]]] = None,
    snapshot_loader: Optional[Callable[[str], Optional[dict]]] = None,
    write_pending_push: bool = True,
    log: Optional[logging.Logger] = None,
) -> dict:
    """盘中一拍：加载 regime/持仓/行情 → 逐票评估 → 草案落盘 + 节拍记录。

    R46 增量（慢车道判定语义不动）：票池默认并集（broker_state ∪ watchlist）；
    开拍先结算 open 的 fast 草案（收线复核）；慢车道新草案与近窗 open 的
    fast 草案同 (code,side,band) 时并入升级（同波去重，不另发）。
    """
    log = log or logger
    root = tracker_dir(out_dir)
    day = now.date()
    load_holdings = holdings_loader or load_t_pool
    _fetch5 = bars5_loader or (lambda code: fetch_east_klines(code, 5))
    _fetch15 = bars15_loader or (lambda code: fetch_east_klines(code, 15))
    # 请求内 memo：收线结算与逐票评估共用一次装载（纯缓存，不改判定语义）
    memo5: dict[str, list[dict]] = {}
    memo15: dict[str, list[dict]] = {}

    def load_bars5(code: str) -> list[dict]:
        if code not in memo5:
            memo5[code] = _fetch5(code)
        return memo5[code]

    def load_bars15(code: str) -> list[dict]:
        if code not in memo15:
            memo15[code] = _fetch15(code)
        return memo15[code]

    load_snapshot = snapshot_loader or ch_snapshot_price

    def _default_regime_loader(d: date) -> Optional[dict]:
        return _load_json(regime_file_path(root, d))

    load_regime = regime_loader or _default_regime_loader
    base = {"schema": SCHEMA_BEAT, "version": __version__, "ts": fmt_ts(now)}

    try:
        holdings = load_holdings()
    except Exception as exc:  # noqa: BLE001 - 持仓不可读 → 全票 fail-closed
        log.error("[beat] 持仓加载失败：%s", exc)
        return {**base, "status": "failure", "reason": f"holdings_unreadable: {exc}", "codes": {}}

    regime = load_regime(day)
    if not regime or not isinstance(regime.get("codes"), dict):
        log.error("[beat] %s 周期文件缺失/不可读，全票 fail-closed", day)
        return {**base, "status": "failure", "reason": "regime_file_missing", "codes": {}}

    state = load_state(root, day)
    drafts_file = drafts_path(root, day)
    drafts_today = read_jsonl(drafts_file)
    # R46 收线复核：bar_status=open 的 fast 草案，其形成中 bar 已收 → 结算
    settled_fast = settle_open_fast_drafts(now=now, drafts=drafts_today,
                                           load_bars5=load_bars5,
                                           load_bars15=load_bars15, log=log)
    merged_fast: list[dict] = []
    results: dict[str, Any] = {}
    new_drafts: list[dict] = []

    for holding in holdings["holdings"]:
        code = str(holding["code"]).split(".")[0]
        res_key = _res_key(code, holding)
        info = regime["codes"].get(code)
        if not info:
            results[res_key] = {"code": code, "suppressed": {"reason": "no_regime_info"}}
            continue
        try:
            bars5 = closed_bars(load_bars5(code), now)
            bars15 = closed_bars(load_bars15(code), now)
            if not bars5 or not bars15:
                results[res_key] = {"code": code, "suppressed": {"reason": "empty_minute_data"}}
                continue
            snap = None
            try:
                snap = load_snapshot(code)
            except Exception:  # noqa: BLE001 - 快照价仅辅助，失败降级 5m 收价
                snap = None
            px = float(bars5[-1]["close"])
            px_source = "m5_close"
            if snap and snap.get("price"):
                snap_ts = parse_ts(snap["snapshot_ts"])
                if snap_ts is not None and (now - snap_ts).total_seconds() <= 600:
                    px = float(snap["price"])
                    px_source = "intraday_snapshot"
            res = evaluate_code(
                code=code, name=info.get("name") or holding.get("name") or "",
                regime_info=info, params=info.get("params") or {},
                bars5=bars5, bars15=bars15, px=px, px_source=px_source, now=now,
                state=state, drafts_today=drafts_today + new_drafts, holding=holding,
                holdings_updated_at=str(holdings.get("updated_at") or ""),
            )
            results[res_key] = res
            if res.get("draft"):
                # R46 同波去重：慢车道草案与近窗 open 的 fast 草案同
                # (code,side,band) → 并入升级该 fast 草案，不另发。
                fast = _mergeable_fast_draft(res["draft"], drafts_today, now)
                if fast is not None:
                    fast["bar_status"] = BAR_STATUS_CONFIRMED
                    fast["confirmed_at"] = fmt_ts(now)
                    fast["confirm_source"] = "slow_beat_merge"
                    fast["closed_rsi5"] = res["draft"]["trigger"]["rsi5"]
                    if res["draft"].get("subtype") == "entry":
                        state.set_open_t(code, {
                            "draft_id": fast["id"], "entry_px": fast.get("px"),
                            "qty": int(fast.get("qty") or 0),
                            "target_band": fast.get("target_band"),
                            "stop": fast.get("stop"), "ts": fmt_ts(now)})
                    res["draft"] = None
                    res["merged_into"] = fast["id"]
                    merged_fast.append(fast)
                else:
                    new_drafts.append(res["draft"])
        except Exception as exc:  # noqa: BLE001 - 单票数据失败 fail-closed 该票
            results[res_key] = {"code": code, "suppressed": {"reason": f"evaluate_error: {type(exc).__name__}: {exc}"}}
            log.error("[beat] %s 评估失败：%s", code, exc)

    if settled_fast or merged_fast:
        # 结算/并入原位重写：草案 jsonl 保持既有行序、id 不变；pending_push
        # 中尚未消费的同名文件同步重写（升级标注直达 PM watcher）。
        _rewrite_jsonl(drafts_file, drafts_today)
        for d in settled_fast + merged_fast:
            pp = root / "pending_push" / f"{d['id']}.json"
            if write_pending_push and pp.is_file():
                _atomic_write_json(pp, d)
    for draft in new_drafts:
        _append_jsonl(drafts_file, draft)
        if write_pending_push:
            _atomic_write_json(root / "pending_push" / f"{draft['id']}.json", draft)
    save_state(root, day, state)
    beat_record = {**base, "status": "ok", "codes": results,
                   "new_draft_ids": [d["id"] for d in new_drafts],
                   "fast_settled": [d["id"] for d in settled_fast],
                   "fast_merged": [d["id"] for d in merged_fast]}
    _append_jsonl(root / "beats" / f"{day.strftime('%Y%m%d')}.jsonl", beat_record)
    if new_drafts:
        log.info("[beat] %s 草案 %s", fmt_ts(now), [d["id"] for d in new_drafts])
    return beat_record


# ---------------------------------------------------------------------------
# R46 快车道（1 分钟）：实时价 + intrabar 重估 + fast 草案；慢车道收线结算
# ---------------------------------------------------------------------------

def _rewrite_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """整体原位重写 jsonl（结算/并入用；行序与行数不变，原子替换）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows),
        encoding="utf-8")
    os.replace(tmp, path)


def build_intrabar_bars5(raw_bars5: Sequence[Mapping[str, Any]], px: float,
                         now: datetime) -> tuple[list[dict], Optional[str]]:
    """5m 序列末根（形成中 bar）以实时价刷新（R46 intrabar 口径）。

    返回 (bars5_fast, forming_ts)。形成中 bar = 第一根 ts > now 的 5m bar
    （东财口径 ts=周期结束时刻）；其 close 以实时价覆盖，volume/amount 维持
    东财 partial 值（VWAP 口径不变）。无形成中 bar（收线瞬间/午休边界）→
    仅已收序列（intrabar 退化为收线值）。
    """
    bars = closed_bars(raw_bars5, now)
    forming_ts: Optional[str] = None
    for b in raw_bars5:
        ts = parse_ts(str(b["ts"]))
        if ts is not None and ts > now:
            fb = dict(b)
            fb["close"] = float(px)
            forming_ts = str(b["ts"])
            bars.append(fb)
            break
    return bars, forming_ts


def synthetic_fast_view(bars5_full: Sequence[Mapping[str, Any]],
                        bars1_full: Sequence[Mapping[str, Any]],
                        now: datetime) -> tuple[list[dict], Optional[str], Optional[dict]]:
    """回放用合成视图：全日 5m+1m 缓存 → (bars5_raw, forming_ts, quote)。

    quote = 已收最后一根 1m bar 的收盘（分钟级实时价代理，ts=该 bar 时刻）；
    形成中 5m bar = 缓存中 start(F) < now < end(F) 的那根，volume/amount 用
    区间内已收 1m bar 聚合（close 将被 run_fast_tick 以 quote 实时价覆盖）。
    """
    closed5 = closed_bars(bars5_full, now)
    m1 = closed_bars(bars1_full, now)
    quote: Optional[dict] = None
    if m1:
        quote = {"price": float(m1[-1]["close"]), "ts": str(m1[-1]["ts"])}
    forming: Optional[dict] = None
    for b in bars5_full:
        ts = parse_ts(str(b["ts"]))
        if ts is None:
            continue
        start = ts - timedelta(minutes=5)
        if start < now < ts:
            inner = [x for x in m1
                     if (parse_ts(str(x["ts"])) or now) > start]
            forming = {
                "ts": str(b["ts"]),
                "open": float(b.get("open") or 0.0),
                "close": float(b["close"]),
                "volume": round(sum(float(x.get("volume") or 0.0) for x in inner), 2),
                "amount": round(sum(float(x.get("amount") or 0.0) for x in inner), 2),
            }
            break
    bars = list(closed5)
    forming_ts = None
    if forming is not None:
        forming_ts = forming["ts"]
        if quote is not None:
            forming["close"] = quote["price"]
        bars.append(forming)
    return bars, forming_ts, quote


def _fast_settle_spec(draft: Mapping[str, Any],
                      params: Mapping[str, Any]) -> dict[str, Any]:
    """从 fast 草案反推收线结算判据（fire 时未显式存亦可确定性重建）。"""
    kind = str((draft.get("trigger") or {}).get("kind") or "")
    base = kind[5:] if kind.startswith("fast_") else kind
    subtype = draft.get("subtype")
    spec: dict[str, Any] = {"kind": base}
    if subtype == "entry":
        spec["band"] = params.get("buy_band")
        spec["rsi5_max"] = params.get("buy_rsi5_max")
        spec["rsi15_max"] = params.get("buy_rsi15_max")
    elif subtype == "zone":
        spec["band"] = params.get("sell_band")
        spec["rsi5_min"] = params.get("sell_rsi5_min")
    elif subtype == "exit":
        spec["band"] = (draft.get("trigger") or {}).get("band") or draft.get("target_band")
    elif subtype == "stop":
        spec["stop"] = draft.get("stop")
    return spec


def _fast_settle_holds(spec: Mapping[str, Any], px_c: float,
                       rsi5: Optional[float], rsi15: Optional[float],
                       ) -> tuple[bool, str]:
    kind = spec.get("kind")
    if kind == "buy_band":
        band = spec.get("band")
        if not band or not (float(band[0]) <= px_c <= float(band[1])):
            return False, "px_out_of_band"
        if spec.get("rsi5_max") is not None and not (
                rsi5 is not None and rsi5 <= float(spec["rsi5_max"])):
            return False, "rsi5_above_max"
        if spec.get("rsi15_max") is not None and not (
                rsi15 is not None and rsi15 < float(spec["rsi15_max"])):
            return False, "rsi15_above_max"
        return True, ""
    if kind == "sell_band":
        band = spec.get("band")
        if not band or not (float(band[0]) <= px_c <= float(band[1])):
            return False, "px_out_of_band"
        if spec.get("rsi5_min") is not None and not (
                rsi5 is not None and rsi5 >= float(spec["rsi5_min"])):
            return False, "rsi5_below_min"
        return True, ""
    if kind == "target_band":
        band = spec.get("band")
        if band and float(band[0]) <= px_c <= float(band[1]):
            return True, ""
        return False, "px_out_of_target_band"
    if kind == "stop_break":
        stop = spec.get("stop")
        if stop is not None and px_c <= float(stop):
            return True, ""
        return False, "px_above_stop"
    return False, "unknown_kind"


def settle_open_fast_drafts(
    *,
    now: datetime,
    drafts: list[dict],
    load_bars5: Callable[[str], list[dict]],
    load_bars15: Callable[[str], list[dict]],
    params_by_code: Optional[Callable[[str], dict]] = None,
    log: Optional[logging.Logger] = None,
) -> list[dict]:
    """慢车道收线复核（R46）：bar_status=open 的 fast 草案 → 结算。

    原 forming_bar_ts 的 bar 已出现在已收序列 → 以该 bar 收线值复核价带条件：
    成立 → confirmed_bar；瓦解 → expired_unconfirmed（留档不删除）。
    bar 尚未收/数据未到 → 保持 open 等下一拍。返回本拍结算的草案（已原地改写）。
    """
    log = log or logger
    settled: list[dict] = []
    for d in drafts:
        if d.get("lane") != FAST_LANE or d.get("bar_status") != BAR_STATUS_OPEN:
            continue
        code = str(d.get("code") or "")
        forming_ts = parse_ts(str(d.get("forming_bar_ts") or ""))
        if not code or forming_ts is None or now < forming_ts:
            continue
        try:
            bars5_closed = closed_bars(load_bars5(code), now)
            bars15_closed = closed_bars(load_bars15(code), now)
        except Exception as exc:  # noqa: BLE001 - 行情失败保留 open 等下一拍
            log.error("[fast-settle] %s 行情加载失败，保留 open：%s", d.get("id"), exc)
            continue
        settled_bar = next((b for b in bars5_closed
                            if (parse_ts(str(b["ts"])) or now) >= forming_ts), None)
        if settled_bar is None:
            continue  # 原 forming bar 尚未进入已收序列（数据延迟），等下一拍
        px_c = float(settled_bar["close"])
        closes = [float(b["close"]) for b in bars5_closed
                  if (parse_ts(str(b["ts"])) or now) <= parse_ts(str(settled_bar["ts"]))]
        rsi5 = rsi_wilder(closes)
        rsi15 = rsi_wilder([float(b["close"]) for b in bars15_closed]) if bars15_closed else None
        params = (params_by_code or (lambda _c: {}))(code)
        spec = d.get("fast_settle_check") or _fast_settle_spec(d, params)
        holds, reason = _fast_settle_holds(spec, px_c, rsi5, rsi15)
        detail = {"bar_ts": str(settled_bar["ts"]), "close": px_c,
                  "rsi5": round(rsi5, 1) if rsi5 is not None else None}
        if holds:
            d["bar_status"] = BAR_STATUS_CONFIRMED
            d["confirmed_at"] = fmt_ts(now)
            d["confirm_source"] = "slow_beat_settle"
            d.update({f"confirm_{k}": v for k, v in detail.items()})
        else:
            d["bar_status"] = BAR_STATUS_EXPIRED
            d["expired_at"] = fmt_ts(now)
            d["expire_reason"] = reason
            d.update({f"expire_{k}": v for k, v in detail.items()})
        settled.append(d)
        log.info("[fast-settle] %s → %s（%s）", d.get("id"), d["bar_status"],
                 detail["bar_ts"])
    return settled


def _mergeable_fast_draft(new_draft: Mapping[str, Any],
                          drafts_today: Sequence[Mapping[str, Any]],
                          now: datetime) -> Optional[dict]:
    """同波可并入的 open fast 草案：同 (code,side,band) 且 ≤MERGE_WAVE_SEC。"""
    band_new = (new_draft.get("trigger") or {}).get("band")
    if band_new is None:
        return None
    for d in drafts_today:
        if d.get("lane") != FAST_LANE or d.get("bar_status") != BAR_STATUS_OPEN:
            continue
        if d.get("code") != new_draft.get("code") or d.get("side") != new_draft.get("side"):
            continue
        if (d.get("trigger") or {}).get("band") != band_new:
            continue
        d_ts = parse_ts(str(d.get("ts") or ""))
        if d_ts is None or (now - d_ts).total_seconds() > MERGE_WAVE_SEC:
            continue
        return d
    return None


def run_fast_tick(
    *,
    now: datetime,
    out_dir: Optional[Path] = None,
    holdings_loader: Optional[Callable[[], dict]] = None,
    regime_loader: Optional[Callable[[date], Optional[dict]]] = None,
    bars5_loader: Optional[Callable[[str], list[dict]]] = None,
    bars15_loader: Optional[Callable[[str], list[dict]]] = None,
    quotes_loader: Optional[Callable[[Sequence[str]], dict]] = None,
    snapshot_loader: Optional[Callable[[str], Optional[dict]]] = None,
    write_pending_push: bool = True,
    log: Optional[logging.Logger] = None,
) -> dict:
    """快车道一拍（R46，1 分钟粒度）：实时价重估 intrabar → fast 草案。

    预检门/布防/冷却与慢车道完全一致（共用 SignalState 与草案台账）；
    价带类触发即产 fast 草案（trigger_kind 前缀 fast_、bar_status=open、
    intrabar_rsi5 实值），落 drafts + pending_push（协议不变）；
    收线确认/瓦解由慢车道 run_beat 结算（settle_open_fast_drafts）。
    """
    log = log or logger
    root = tracker_dir(out_dir)
    day = now.date()
    load_pool = holdings_loader or load_t_pool
    load_bars5 = bars5_loader or (lambda code: cached_east_klines(code, 5))
    load_bars15 = bars15_loader or (lambda code: cached_east_klines(code, 15))
    load_quotes = quotes_loader or fetch_east_ulist_quotes
    load_snapshot = snapshot_loader or ch_snapshot_price

    def _default_regime_loader(d: date) -> Optional[dict]:
        return _load_json(regime_file_path(root, d))

    load_regime = regime_loader or _default_regime_loader
    base = {"schema": SCHEMA_FAST_BEAT, "version": __version__, "ts": fmt_ts(now)}

    try:
        holdings = load_pool()
    except Exception as exc:  # noqa: BLE001 - 持仓不可读 → 全票 fail-closed
        log.error("[fast] 持仓加载失败：%s", exc)
        return {**base, "status": "failure", "reason": f"holdings_unreadable: {exc}", "codes": {}}
    regime = load_regime(day)
    if not regime or not isinstance(regime.get("codes"), dict):
        log.error("[fast] %s 周期文件缺失/不可读，全票 fail-closed", day)
        return {**base, "status": "failure", "reason": "regime_file_missing", "codes": {}}

    state = load_state(root, day)
    drafts_file = drafts_path(root, day)
    drafts_today = read_jsonl(drafts_file)

    pool_codes = [str(h.get("code") or "").split(".")[0] for h in holdings["holdings"]]
    try:
        quotes = load_quotes(pool_codes) or {}
    except Exception as exc:  # noqa: BLE001 - ulist 失败降级逐票快照/5m 收价
        log.warning("[fast] ulist 实时价失败，降级：%s", exc)
        quotes = {}

    results: dict[str, Any] = {}
    new_drafts: list[dict] = []
    for holding in holdings["holdings"]:
        code = str(holding["code"]).split(".")[0]
        res_key = _res_key(code, holding)
        info = regime["codes"].get(code)
        if not info:
            results[res_key] = {"code": code, "lane": FAST_LANE,
                             "suppressed": {"reason": "no_regime_info"}}
            continue
        try:
            bars15 = closed_bars(load_bars15(code), now)
            raw5 = load_bars5(code)
            bars5_closed = closed_bars(raw5, now)
            if not raw5 or not bars15 or not bars5_closed:
                results[res_key] = {"code": code, "lane": FAST_LANE,
                                 "suppressed": {"reason": "empty_minute_data"}}
                continue
            # 实时价链：ulist → CH 快照（≤10min）→ 5m 收价（fail-soft，标注来源）
            px: Optional[float] = None
            px_source, quote_ts = "east_ulist", None
            q = quotes.get(code)
            if q and q.get("price"):
                px, quote_ts = float(q["price"]), q.get("ts")
            else:
                try:
                    snap = load_snapshot(code)
                except Exception:  # noqa: BLE001
                    snap = None
                if snap and snap.get("price"):
                    snap_ts = parse_ts(snap["snapshot_ts"])
                    if snap_ts is not None and (now - snap_ts).total_seconds() <= 600:
                        px = float(snap["price"])
                        px_source = "intraday_snapshot"
            if px is None:
                px = float(bars5_closed[-1]["close"])
                px_source = "m5_close"
            bars5_fast, forming_ts = build_intrabar_bars5(raw5, px, now)
            res = evaluate_code(
                code=code, name=info.get("name") or holding.get("name") or "",
                regime_info=info, params=info.get("params") or {},
                bars5=bars5_fast, bars15=bars15, px=px, px_source=px_source, now=now,
                state=state, drafts_today=drafts_today + new_drafts, holding=holding,
                holdings_updated_at=str(holdings.get("updated_at") or ""),
                allowed_trigger_kinds=FAST_TRIGGER_KINDS,
            )
            res["lane"] = FAST_LANE
            # 结算锚 = 触发时的末根 bar（形成中 bar 优先；无形成中 bar 的退化
            # 路径回退到触发已收 bar——否则草案永不结算滞留 open）
            settle_bar_ts = str(bars5_fast[-1]["ts"]) if bars5_fast else None
            res["forming_bar_ts"] = settle_bar_ts
            res["quote_ts"] = quote_ts
            if res.get("draft"):
                d = res["draft"]
                kind = str(d["trigger"]["kind"])
                d["trigger"]["kind"] = f"fast_{kind}"
                d["lane"] = FAST_LANE
                d["bar_status"] = BAR_STATUS_OPEN
                d["intrabar_rsi5"] = d["trigger"]["rsi5"]
                d["forming_bar_ts"] = settle_bar_ts
                d["quote_ts"] = quote_ts
                d["fast_settle_check"] = _fast_settle_spec(
                    {**d, "trigger": {"kind": kind, "band": d["trigger"].get("band")}},
                    info.get("params") or {})
                new_drafts.append(d)
            results[res_key] = res
        except Exception as exc:  # noqa: BLE001 - 单票数据失败 fail-closed 该票
            results[res_key] = {"code": code, "lane": FAST_LANE,
                             "suppressed": {"reason": f"evaluate_error: {type(exc).__name__}: {exc}"}}
            log.error("[fast] %s 评估失败：%s", code, exc)

    for draft in new_drafts:
        _append_jsonl(drafts_file, draft)
        if write_pending_push:
            _atomic_write_json(root / "pending_push" / f"{draft['id']}.json", draft)
    save_state(root, day, state)
    beat_record = {**base, "status": "ok",
                   "pool": {"codes": pool_codes,
                            "source": holdings.get("pool_source") or "broker_state",
                            "watchlist_added": holdings.get("watchlist_added") or []},
                   "codes": results,
                   "new_draft_ids": [d["id"] for d in new_drafts]}
    _append_jsonl(root / "beats" / f"fast_{day.strftime('%Y%m%d')}.jsonl", beat_record)
    if new_drafts:
        log.info("[fast] %s fast 草案 %s", fmt_ts(now), [d["id"] for d in new_drafts])
    return beat_record


# ---------------------------------------------------------------------------
# 15:10 对账
# ---------------------------------------------------------------------------

def _fee_estimate(side: str, amount: float) -> float:
    rate = FEE_BUY_PCT if side == "BUY" else FEE_SELL_PCT
    fee = amount * rate
    if side == "BUY" and fee < FEE_MIN_COMMISSION:
        fee = FEE_MIN_COMMISSION
    return round(fee, 2)


def normalize_fill(row: Mapping[str, Any]) -> Optional[dict]:
    """bridge 成交行（导出文件/OCR 同构）→ {ts, code, side, price, shares, amount, source}。"""
    ts = parse_ts(str(row.get("trade_time") or ""))
    code = str(row.get("code") or "").split(".")[0]
    side = str(row.get("side") or "").upper()
    shares = int(row.get("shares") or 0)
    price = float(row.get("price") or 0.0)
    if not ts or not code or side not in ("BUY", "SELL") or shares <= 0 or price <= 0:
        return None
    amount = row.get("amount")
    return {
        "ts": fmt_ts(ts), "ts_dt": ts, "code": code, "side": side,
        "price": price, "shares": shares,
        "amount": float(amount) if amount else round(price * shares, 2),
        "source": str(row.get("source") or "fills_file"),
    }


def match_fill_to_draft(fill: Mapping[str, Any], drafts: Sequence[Mapping[str, Any]],
                        used: set[str]) -> Optional[dict]:
    """匹配：code + 方向 + 时间窗 ±40min + 价带容差 ±1.5%（取时间最近的草案）。

    一份草案可吸收多笔成交（Owner 常见拆单执行，如 100+1900 同分钟两笔）；
    used 只用于汇总 matched/unmatched 草案清单，不做独占消费。
    """
    want_side = SIDE_BUY if fill["side"] == "BUY" else SIDE_SELL
    candidates: list[tuple[float, dict]] = []
    for d in drafts:
        if d.get("code") != fill["code"]:
            continue
        if str(d.get("side")) != want_side:
            continue
        d_ts = parse_ts(str(d.get("ts") or ""))
        if d_ts is None:
            continue
        dt_min = abs((fill["ts_dt"] - d_ts).total_seconds()) / 60.0
        if dt_min > MATCH_TIME_WINDOW_MIN:
            continue
        band = d.get("trigger", {}).get("band") or d.get("target_band")
        px = float(fill["price"])
        tol = px * MATCH_PRICE_TOL_PCT
        if band and len(band) == 2:
            lo, hi = float(band[0]) - tol, float(band[1]) + tol
        else:
            lo, hi = float(d.get("px") or px) - tol, float(d.get("px") or px) + tol
        if not (lo <= px <= hi):
            continue
        candidates.append((dt_min, d))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    best = candidates[0][1]
    used.add(best["id"])
    return best


def build_rounds(fills: Sequence[dict], matches: dict[int, Optional[dict]]) -> list[dict]:
    """按码时间序 FIFO 配对：BUY 开 lot、每笔 SELL 消耗 lot 形成一个回合（多 lot
    加权均价，买入费用按消耗量分摊）；单边如实留痕。"""
    by_code: dict[str, list[tuple[int, dict]]] = {}
    for idx, fill in enumerate(fills):
        by_code.setdefault(fill["code"], []).append((idx, fill))
    rounds: list[dict] = []
    for code in sorted(by_code):
        open_lots: list[list[Any]] = []  # [fill, draft, remaining_shares, lot_fee_total]
        for idx, fill in by_code[code]:
            draft = matches.get(idx)
            if fill["side"] == "BUY":
                lot_fee = _fee_estimate("BUY", fill["amount"])
                open_lots.append([fill, draft, fill["shares"], lot_fee])
                continue
            need = fill["shares"]
            consumed: list[tuple[dict, Optional[dict], int, float]] = []  # (fill,draft,qty,fee_share)
            while need > 0 and open_lots:
                lot = open_lots[0]
                take = min(need, lot[2])
                fee_share = round(lot[3] * take / max(lot[0]["shares"], 1), 2)
                consumed.append((lot[0], lot[1], take, fee_share))
                lot[2] -= take
                need -= take
                if lot[2] <= 0:
                    open_lots.pop(0)
            if consumed:
                qty = sum(c[2] for c in consumed)
                buy_amount = sum(c[0]["price"] * c[2] for c in consumed)
                avg_buy = buy_amount / qty
                buy_fees = round(sum(c[3] for c in consumed), 2)
                sell_fee = _fee_estimate("SELL", fill["price"] * qty)
                gross = round((fill["price"] - avg_buy) * qty, 2)
                all_disciplined = bool(draft) and all(
                    c[1] is not None and _qty_disciplined(c[1], c[0]) for c in consumed
                ) and _qty_disciplined(draft, fill)
                rounds.append({
                    "schema": SCHEMA_LEDGER, "code": code,
                    "kind": "round_trip",
                    "buy": {"ts": consumed[0][0]["ts"], "avg_price": round(avg_buy, 3),
                            "qty": qty,
                            "lots": [_fill_ref(c[0], c[1]) for c in consumed]},
                    "sell": _fill_ref(fill, draft),
                    "qty": qty, "gross_pnl": gross,
                    "fees": round(buy_fees + sell_fee, 2),
                    "net_pnl": round(gross - buy_fees - sell_fee, 2),
                    "disciplined": all_disciplined,
                    "matched_drafts": sorted({d["id"] for c in consumed if c[1] for d in (c[1],)}
                                             | ({draft["id"]} if draft else set())),
                })
            if need > 0:
                rounds.append(_single_sided(fill, draft, need, "sell_no_buy"))
        for lot_fill, lot_draft, remaining, _fee in open_lots:
            if remaining > 0:
                rounds.append(_single_sided(lot_fill, lot_draft, remaining, "buy_unclosed"))
    return rounds


def _qty_disciplined(draft: Mapping[str, Any], fill: Mapping[str, Any]) -> bool:
    return int(fill["shares"]) <= int(draft.get("qty") or 0) * 2  # 容忍 2×建议量


def _fill_ref(fill: dict, draft: Optional[dict]) -> dict:
    return {"ts": fill["ts"], "side": fill["side"], "price": fill["price"],
            "shares": fill["shares"], "amount": fill["amount"],
            "fee_est": _fee_estimate(fill["side"], fill["amount"]),
            "source": fill["source"], "draft_id": draft["id"] if draft else None}


def _single_sided(fill: dict, draft: Optional[dict], qty: int, kind: str) -> dict:
    return {
        "schema": SCHEMA_LEDGER, "code": fill["code"], "kind": kind,
        "fill": _fill_ref(fill, draft), "qty": qty,
        "gross_pnl": None, "fees": _fee_estimate(fill["side"], fill["amount"] * qty / max(fill["shares"], 1)),
        "net_pnl": None, "disciplined": bool(draft),
        "matched_drafts": [draft["id"]] if draft else [],
        "note": "off_plan" if not draft else "single_sided",
    }


# ── R48：教练式复盘 + trades_inferred fills 精确覆盖 ─────────────────────

EAST_K_DAY_HL_URL = ("http://push2his.eastmoney.com/api/qt/stock/kline/get"
                     "?secid={secid}&fields1=f1,f2,f3&fields2=f51,f54,f55"
                     "&klt=101&fqt=1&end=20500101&lmt={lmt}")


def _parse_day_hl_klines(klines: Sequence[str],
                         want_date: str) -> Optional[dict[str, float]]:
    """日 K 行（fields2=f51,f54,f55）= 日期,高,低 → 当日 {high, low}。

    注意：EAST_K_URL 的 fields2=f51,f52,f53,f56,f57（日期,开,收,量,额）不含
    高低价——R48a 修复：曾误用该 URL 把量/额当高低价（473702519.98 量级
    即暴露），契约核对绝不能吃这种脏值。
    """
    for row in klines:
        parts = str(row).split(",")
        if len(parts) >= 3 and parts[0].startswith(want_date):
            try:
                return {"high": float(parts[1]), "low": float(parts[2])}
            except ValueError:
                return None
    return None


def fetch_east_day_hl(code: str, for_date: date) -> Optional[dict[str, float]]:
    """当日日线高低价（klt=101；fields2=f51,f54,f55=日期,高,低）。

    只取日期恰为 for_date 的那根；无当日根（未开盘/停牌/源失败）→ None。
    """
    url = EAST_K_DAY_HL_URL.format(secid=east_secid(code), lmt=3)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    klines = (data.get("data") or {}).get("klines") or []
    hl = _parse_day_hl_klines(klines, for_date.isoformat())
    if hl is None:
        return None
    # 价格域 sanity（A 股高低价 0<p<10000）：越界=源列漂移，按不可用处理
    if not (0 < hl["high"] < 10000 and 0 < hl["low"] <= hl["high"]):
        return None
    return hl


def _coaching_for_reconcile(*, root: Path, for_date: date, fills: list[dict],
                            matches: dict[int, Optional[dict]], drafts: list[dict],
                            today_rounds: list[dict],
                            account: str) -> dict[str, Any]:
    """R48 reconcile 内嵌链：① trades_inferred 本账号行以真实 fills 覆盖
    （±10min 同码同向，文件原子回写，幂等）；② 教练式复盘（含契约核对，
    日内高低价东财日线；缺源如实 not_scored）。"""
    from services import t_trade_coaching as coach
    from services.ths_holdings_poll import inferred_path, read_jsonl as read_inferred

    inf_file = inferred_path(root, for_date)
    rows_all = read_inferred(inf_file)
    mine = [r for r in rows_all
            if str(r.get("account") or "acct1") == account]
    mine_over = coach.override_inferred_with_fills(mine, fills,
                                                   default_account=account)
    def _key(r: dict) -> tuple:
        return (str(r.get("account") or "acct1"), str(r.get("code")),
                str(r.get("ts")))
    over_by_key = {_key(r): r for r in mine_over}
    rows_new = [over_by_key.get(_key(r), r) for r in rows_all]
    if rows_new != rows_all:
        inf_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = inf_file.with_suffix(inf_file.suffix + ".tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False, default=str)
                               + "\n" for r in rows_new), encoding="utf-8")
        os.replace(tmp, inf_file)

    day_prefix = for_date.isoformat()
    today_idx = [i for i, f in enumerate(fills)
                 if str(f.get("ts", "")).startswith(day_prefix)]
    fills_today_c = [{"ts": fills[i]["ts_dt"], "code": fills[i]["code"],
                      "side": fills[i]["side"], "shares": fills[i]["shares"],
                      "price": fills[i]["price"]} for i in today_idx]
    matches_today = {n: matches[i] for n, i in enumerate(today_idx)}

    regime_doc = _load_json(regime_file_path(root, for_date)) or {}
    contract_doc = _load_json(root / "contract_stop.json") or {}
    knife_prior = coach.weekly_knife_count(root, for_date)

    day_hl: dict[str, Any] = {}
    for rule in contract_doc.get("rules") or []:
        code = str(rule.get("code") or "")
        if not code:
            continue
        try:
            hl = fetch_east_day_hl(code, for_date)
        except Exception:  # noqa: BLE001 - 行情缺 → 契约核对 not_scored，不猜
            hl = None
        day_hl[code] = hl or {"low": None, "high": None}

    return coach.build_coaching(
        for_date=for_date, fills_today=fills_today_c, matches=matches_today,
        drafts=drafts, rounds_today=today_rounds, regime_doc=regime_doc,
        contract_doc=contract_doc, knife_week_prior=knife_prior,
        day_hl=day_hl, inferred_rows=mine_over)


def run_reconcile(
    *,
    for_date: date,
    now: datetime,
    out_dir: Optional[Path] = None,
    fills_loader: Optional[Callable[[date], tuple[list[dict], str]]] = None,
    drafts_rows: Optional[list[dict]] = None,
    account: str = "acct1",
    log: Optional[logging.Logger] = None,
) -> dict:
    """15:10 对账：成交 ↔ 草案匹配 → ledger/YYYYMMDD.jsonl + summary。

    fills_loader 返回 (fills, source_label)；默认链：--fills-file 语义由 CLI 注入 →
    runtime/t_tracker/fills/<date>.json → bridge 导出文件解析。无源 → fills_source=unavailable。
    R48：account 标注本次对账账号；coaching 复盘 + trades_inferred fills 精确覆盖
    （±10min 同码同向）随对账落盘，summary 增 `coaching` 字段（PM 15:12 直推面）。
    """
    log = log or logger
    root = tracker_dir(out_dir)
    stamp = for_date.strftime("%Y%m%d")
    ledger_dir = root / "ledger"
    drafts = drafts_rows if drafts_rows is not None else read_jsonl(drafts_path(root, for_date))

    if fills_loader is None:
        # R48：root 感知（out_dir 注入时读该根的 fills；生产行为不变）
        fills_loader = lambda d: _default_fills_loader(d, root=root)  # noqa: E731
    try:
        fills_raw, source_label = fills_loader(for_date)
    except Exception as exc:  # noqa: BLE001 - 成交源失败如实记录，不造数
        log.error("[reconcile] 成交源失败：%s", exc)
        fills_raw, source_label = [], f"error:{type(exc).__name__}"

    fills = [f for f in (normalize_fill(r) for r in fills_raw) if f is not None]
    # 成交按 (码, 时间) 全序排列：FIFO 配对与时间窗匹配都依赖时间序
    # （bridge/OCR 源常为倒序快照，如 09:38:58 在 09:38:52 之前）。
    fills.sort(key=lambda f: (f["code"], f["ts_dt"]))
    day_prefix = for_date.isoformat()
    fills_today = [f for f in fills if f["ts"].startswith(day_prefix)]
    prior_fills = [f for f in fills if not f["ts"].startswith(day_prefix)]

    used: set[str] = set()
    matches: dict[int, Optional[dict]] = {}
    for idx, fill in enumerate(fills):
        # 时间窗 ±40min 天然排除隔日草案误配（跨日 T 的前日买入不占今日草案）
        matches[idx] = match_fill_to_draft(fill, drafts, used)

    rounds = build_rounds(fills, matches)
    today_rounds = [r for r in rounds if _round_day(r) == day_prefix]
    trips = [r for r in today_rounds if r["kind"] == "round_trip"]
    off_plan = [r for r in today_rounds if r.get("note") == "off_plan"]
    disciplined = [r for r in trips if r["disciplined"]]
    net = round(sum(r["net_pnl"] or 0.0 for r in trips), 2)
    gross = round(sum(r["gross_pnl"] or 0.0 for r in trips), 2)
    fees = round(sum(r["fees"] or 0.0 for r in today_rounds), 2)

    # R46 fast 命中率统计（草案台账口径：confirmed / expired / open 未决）
    fast_all = [d for d in drafts if d.get("lane") == FAST_LANE]
    fast_confirmed = [d for d in fast_all if d.get("bar_status") == BAR_STATUS_CONFIRMED]
    fast_expired = [d for d in fast_all if d.get("bar_status") == BAR_STATUS_EXPIRED]
    fast_open = [d for d in fast_all if d.get("bar_status") == BAR_STATUS_OPEN]
    fast_settled_n = len(fast_confirmed) + len(fast_expired)
    fast_lane_stats = {
        "drafts": len(fast_all),
        "confirmed_bar": len(fast_confirmed),
        "expired_unconfirmed": len(fast_expired),
        "open_unresolved": len(fast_open),
        "merged_from_slow": sum(1 for d in fast_confirmed
                                if d.get("confirm_source") == "slow_beat_merge"),
        "hit_rate": round(len(fast_confirmed) / fast_settled_n, 4) if fast_settled_n else None,
    }

    summary = {
        "schema": "t_tracker_summary/1", "version": __version__, "date": day_prefix,
        "generated_at": fmt_ts(now), "fills_source": source_label,
        "fills_total": len(fills), "fills_today": len(fills_today),
        "prior_day_fills_for_pairing": len(prior_fills),
        "t_round_trips": len(trips), "net_pnl": net, "gross_pnl": gross, "fees": fees,
        "discipline_rate": round(len(disciplined) / len(trips), 4) if trips else None,
        "off_plan_count": len(off_plan),
        "matched_draft_ids": sorted(used),
        "unmatched_draft_ids": sorted({d.get("id") for d in drafts} - used),
        "rounds": len(today_rounds),
        "fast_lane": fast_lane_stats,
        "account": account,
    }

    # R48 教练式复盘 + trades_inferred 精确覆盖（±10min 同码同向同窗）
    try:
        from services import t_trade_coaching as _coach  # 延迟 import 防环
        coaching = _coaching_for_reconcile(
            root=root, for_date=for_date, fills=fills, matches=matches,
            drafts=drafts, today_rounds=today_rounds, account=account)
        _coach.attach_to_summary(summary, coaching)
        ledger_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(ledger_dir / f"{stamp}_coaching.json", coaching)
        log.info("[reconcile] coaching：评分=%s 跟单率=%s 违反=%d",
                 coaching["daily"]["纪律评分"], coaching["daily"]["跟单率"],
                 coaching["violations_total"])
    except Exception as exc:  # noqa: BLE001 - 复盘失败不影响台账主体，如实标注
        log.error("[reconcile] coaching 失败：%s", exc)
        summary["coaching_error"] = f"{type(exc).__name__}: {exc}"

    ledger_file = ledger_dir / f"{stamp}.jsonl"
    ledger_file.parent.mkdir(parents=True, exist_ok=True)
    ledger_file.write_text(
        "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rounds),
        encoding="utf-8")
    _atomic_write_json(ledger_dir / f"{stamp}_summary.json", summary)
    log.info("[reconcile] %s：fills=%d(today %d) trips=%d net=%.2f off_plan=%d source=%s",
             for_date, len(fills), len(fills_today), len(trips), net, len(off_plan), source_label)
    return summary


def _round_day(round_: Mapping[str, Any]) -> str:
    for key in ("sell", "buy", "fill"):
        ref = round_.get(key)
        if isinstance(ref, Mapping) and ref.get("ts"):
            return str(ref["ts"])[:10]
    return ""


def _default_fills_loader(for_date: date,
                          root: Optional[Path] = None) -> tuple[list[dict], str]:
    """成交源链：runtime/t_tracker/fills/<date>.json → bridge 历史成交导出文件。

    R48：root 参数让注入 out_dir 的调用（测试/分账号）读对应根；缺省=生产根。
    """
    root = root if root is not None else tracker_dir()
    fills_file = root / "fills" / f"{for_date.strftime('%Y%m%d')}.json"
    if fills_file.is_file():
        data = _load_json(fills_file) or {}
        rows = data.get("fills")
        if isinstance(rows, list):
            return rows, f"file:{fills_file.name}"
    try:
        import sys
        proj = Path(__file__).resolve().parents[1]
        if str(proj) not in sys.path:
            sys.path.insert(0, str(proj))
        from services.ths_account_bridge import read_ths_trade_history
        parsed = read_ths_trade_history()
        rows = parsed.get("rows") if isinstance(parsed, dict) else None
        if isinstance(rows, list) and rows:
            return rows, "bridge:ths_export_file"
    except Exception:  # noqa: BLE001 - 导出文件不可用 → 下一档
        pass
    return [], "unavailable"


# ---------------------------------------------------------------------------
# L2 策略引擎（2026-11-12，任务书 20261112_l2_integration.md §7）
# ---------------------------------------------------------------------------

L2Side = str  # "buy-T" | "sell-T"

L2_SIDES = ("buy-T", "sell-T")


def evaluate_l2_signal(
    *,
    code: str,
    name: str,
    price: float,
    price_source: str,
    shares: int,           # 母仓股数
    rsi15: Optional[float],
    rsi5: Optional[float],
    now: datetime,
    last_l2_time: Optional[datetime],  # 本票本向上次 L2 信号时间（冷却用）
    cooldown_sec: int = L2_COOLDOWN_SEC,
) -> dict[str, Any]:
    """L2 策略：15min RSI(14) + 5min RSI(14) 双档判定。

    任务书 §7 策略升级：
      15min RSI>=80 + 5min RSI>=70 → 卖出T（减仓 1/4母仓）
      15min RSI<=20 + 5min RSI<=30 → 买入T（加仓 1/4母仓）
      先到先触发；
      卖带 75/80 分批（卖出信号标注 tier=trim/full）
      买带不高于 35（买入信号标注 buy_tier=conviction/normal）

    头肩形态识别 → STRUCTURE_PLACEHOLDER（后续做，不阻塞）。

    返回 {signal, draft, suppressed}。
    """
    result: dict[str, Any] = {
        "code": code, "name": name, "ts": tts_now_str(now),
        "price": price, "price_source": price_source,
        "rsi15": round(rsi15, 1) if rsi15 is not None else None,
        "rsi5": round(rsi5, 1) if rsi5 is not None else None,
        "shares": shares,
        "signal": None,
        "draft": None,
        "suppressed": None,
        "head_shoulder": L2_HEAD_SHOULDER_PLACEHOLDER,
    }

    def suppress(reason: str, **kw: Any) -> None:
        result["suppressed"] = {"reason": reason, **kw}

    if rsi15 is None or rsi5 is None:
        suppress("rsi_data_insufficient",
                 rsi15=rsi15, rsi5=rsi5)
        return result

    # ── 冷却检查 ──
    if last_l2_time is not None:
        elapsed = (now - last_l2_time).total_seconds()
        if elapsed < cooldown_sec:
            suppress("cooldown",
                     last_l2_time=tts_now_str(last_l2_time),
                     elapsed_sec=round(elapsed, 1),
                     cooldown_sec=cooldown_sec)
            # 不阻塞——仅为draft标注，signal仍可返回
            result["cooldown_active"] = True

    # ── 仓位下限检查 ──
    lot_size = (shares // L2_FRACTION // 100) * 100
    if lot_size < 100:
        suppress("position_too_small",
                 shares=shares, fraction=L2_FRACTION, lot_size=lot_size)
        return result

    # ── 卖出信号（15min RSI 过热） ──
    sell_signal = False
    sell_tier = None
    buy_signal = False
    buy_tier = None
    position_pct = 1.0 / L2_FRACTION  # 1/4 = 0.25

    if rsi15 >= L2_SELL_RSI15_MIN and rsi5 >= L2_SELL_RSI5_MIN:
        sell_signal = True
        # 分批标注：>=80 全清，75-80 减半
        if rsi15 >= 80.0:
            sell_tier = "full"
            position_pct = 1.0 / L2_FRACTION
        else:
            sell_tier = "trim"
            position_pct = 1.0 / (L2_FRACTION * 2)  # 1/8
        # 头肩形态占位（后续做——5min 头肩→做空确认）
        _ = L2_HEAD_SHOULDER_PLACEHOLDER

    # ── 买入信号（15min RSI 超卖） ──
    if rsi15 <= L2_BUY_RSI15_MAX and rsi5 <= L2_BUY_RSI5_MAX:
        buy_signal = True
        # 买带分层：≤20 确定性档，≤35 常规（买带不高于35）
        if rsi15 <= 15.0:
            buy_tier = "conviction"
        else:
            buy_tier = "normal"
        # 15min RSI<=20 + 5min RSI<=30 → 加仓 1/4母仓
        # 买带不高于35 → 即使 RSI5>30 也以 35 为上限做常态标注
        if rsi5 > L2_BUY_RSI5_MAX and rsi5 <= 35.0:
            buy_tier = "above30_watch"

    # ── 先到先触发（优先卖出） ──
    if sell_signal:
        qty = int(lot_size * 100) if sell_tier == "trim" else lot_size
        draft_id = _l2_draft_id(now, code, "sell-T")
        draft = {
            "schema": SCHEMA_L2_DRAFT,
            "id": draft_id,
            "ts": tts_now_str(now),
            "code": code, "name": name,
            "side": "sell-T",
            "tier": sell_tier,
            "qty": qty,
            "price": price,
            "price_source": price_source,
            "signal": {
                "rsi15": round(rsi15, 1),
                "rsi5": round(rsi5, 1),
                "type": "overheat",
                "position_pct": round(position_pct, 4),
            },
        }
        result["signal"] = "sell-T"
        result["draft"] = draft
        result["draft_id"] = draft_id
        result["lot_qty"] = qty
        result["position_pct"] = position_pct

    elif buy_signal:
        qty = max(lot_size, 100)
        draft_id = _l2_draft_id(now, code, "buy-T")
        draft = {
            "schema": SCHEMA_L2_DRAFT,
            "id": draft_id,
            "ts": tts_now_str(now),
            "code": code, "name": name,
            "side": "buy-T",
            "tier": buy_tier,
            "qty": qty,
            "price": price,
            "price_source": price_source,
            "signal": {
                "rsi15": round(rsi15, 1),
                "rsi5": round(rsi5, 1),
                "type": "oversold",
                "position_pct": round(position_pct, 4),
            },
        }
        result["signal"] = "buy-T"
        result["draft"] = draft
        result["draft_id"] = draft_id
        result["lot_qty"] = qty
        result["position_pct"] = position_pct

    return result


def _l2_draft_id(now: datetime, code: str, side: str) -> str:
    short = {"buy-T": "buy", "sell-T": "sell"}.get(side, side)
    return f"L2{now.strftime('%Y%m%d')}_{code}_{short}_{now.strftime('%H%M%S')}"


def tts_now_str(dt: Optional[datetime] = None) -> str:
    return (dt or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def run_l2_beat(
    *,
    code: str,
    name: str,
    price: float,
    price_source: str,
    shares: int,
    rsi15: Optional[float],
    rsi5: Optional[float],
    now: datetime,
    last_l2_time: Optional[datetime] = None,
    out_dir: Optional[Path] = None,
    write_draft: bool = True,
) -> dict[str, Any]:
    """L2 策略单票一拍的执行入口：同步 evaluate + 草案落盘。

    供 pipeline_scheduler 的 t_trade_monitor job 调用。
    """
    root = tracker_dir(out_dir)
    result = evaluate_l2_signal(
        code=code, name=name,
        price=price, price_source=price_source,
        shares=shares,
        rsi15=rsi15, rsi5=rsi5,
        now=now, last_l2_time=last_l2_time,
    )
    if result.get("draft") and write_draft:
        draft = result["draft"]
        # 草案落盘（drafts jsonl + pending_push）
        day = now.date()
        _append_jsonl(
            root / "drafts" / f"{day.strftime('%Y%m%d')}.jsonl",
            draft,
        )
        _atomic_write_json(
            root / "pending_push" / f"{draft['id']}.json",
            draft,
        )
        result["written"] = True
    return result
