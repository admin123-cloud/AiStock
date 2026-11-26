# -*- coding: utf-8 -*-
"""G3 Phase 2A：AI 候选股排雷与催化过滤引擎（AI Stock Screener）。

职责：
1. 维护结构化排雷与催化契约（落盘 runtime/g3_ai_screening/YYYYMMDD.json 与 latest.json），
   支持由现有 Agent / 定时工作流注入深度公告/财报/减持/新闻穿透结果（source="agent_enriched"）；
2. 在 Agent 尚未注入当日深度结果前，提供确定性排雷底线（source="rule_baseline"）：
   - ST/*ST/退市风险红灯一票否决；
   - 数据阻断标黄；
   - 基于主升分与同业共振生成基础催化分与观察逻辑；
3. 将排雷与催化结果无缝融合进 /api/g3/mainwave，输出绿灯 Top 5 精选池并与 Phase 1A 大盘闸门联动。
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from utils.common.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))
VALID_LIGHTS = ("green", "yellow", "red")
LIGHT_RANK = {"green": 0, "yellow": 1, "red": 2}
STRICT_YELLOW_BLOCK_MAX_CATALYST = 2  # 严格版：黄灯且催化分 <= 2 等同红灯禁买
SCREENING_STALE_CALENDAR_DAYS = 3  # 排雷档案超 3 个自然日视为滞后过期

_RECENT_ST_CACHE: dict[str, Any] = {"expires_at": 0.0, "data": None}
_RECENT_ST_LOCK = threading.Lock()


def load_recent_st_alias_map(
    *,
    lookback_days: int = 365,
    force_refresh: bool = False,
) -> dict[str, dict[str, str]]:
    """从 ClickHouse stock.source_instrument_alias 识别近 12 个月曾带 ST/*ST 的刚摘帽股票（带 1h 内存缓存）。"""
    now_mono = time.monotonic()
    if (
        not force_refresh
        and _RECENT_ST_CACHE["data"] is not None
        and now_mono < float(_RECENT_ST_CACHE["expires_at"])
    ):
        return _RECENT_ST_CACHE["data"]

    with _RECENT_ST_LOCK:
        now_mono = time.monotonic()
        if (
            not force_refresh
            and _RECENT_ST_CACHE["data"] is not None
            and now_mono < float(_RECENT_ST_CACHE["expires_at"])
        ):
            return _RECENT_ST_CACHE["data"]
        result: dict[str, dict[str, str]] = {}
        try:
            from utils.kline.market_warehouse import clickhouse_client

            days = max(30, int(lookback_days))
            with clickhouse_client() as ch:
                res = ch.query(
                    f"""
                    SELECT
                        substring(canonical_code, 1, 6) AS code6,
                        any(if(positionCaseInsensitive(source_name, 'ST') > 0, source_name, canonical_name)) AS st_name,
                        toString(max(updated_at)) AS last_st_at
                    FROM stock.source_instrument_alias
                    WHERE (
                        positionCaseInsensitive(source_name, 'ST') > 0
                        OR positionCaseInsensitive(canonical_name, 'ST') > 0
                    )
                      AND updated_at >= now() - INTERVAL {days} DAY
                    GROUP BY code6
                    """
                )
                for row in (res.result_rows or []):
                    c6 = _norm_code6(row[0])
                    if c6 and c6 != "000000":
                        result[c6] = {
                            "old_name": str(row[1] or "ST"),
                            "last_st_at": str(row[2] or "")[:10],
                        }
        except Exception:
            # ClickHouse 不可达时静默降级，绝不阻断主流程
            result = {}
        _RECENT_ST_CACHE["data"] = result
        _RECENT_ST_CACHE["expires_at"] = time.monotonic() + (3600.0 if result else 120.0)
        return result


def get_screening_freshness(
    saved_doc: Optional[Mapping[str, Any]],
    *,
    now_bj: Optional[datetime] = None,
) -> dict[str, Any]:
    """计算当前 AI 排雷档案的时效状态（供页面顶栏与下单警告显式提示是否滞后）。"""
    now = now_bj or datetime.now(BJ_TZ)
    if not isinstance(saved_doc, Mapping) or not saved_doc.get("decision_date"):
        return {
            "decision_date": None,
            "generated_at": None,
            "items_count": 0,
            "age_days": None,
            "is_stale": True,
            "stale_notice": "尚未生成 AI 排雷档案，当前仅由确定性底线规则兜底。",
        }
    d_str = _norm_date(saved_doc.get("decision_date"))
    try:
        d_date = datetime.strptime(d_str, "%Y-%m-%d").date()
        age_days = max(0, (now.date() - d_date).days)
    except ValueError:
        age_days = None
    is_stale = age_days is not None and age_days >= SCREENING_STALE_CALENDAR_DAYS
    items_cnt = len(saved_doc.get("items_by_code") or {})
    stale_notice = (
        f"AI 排雷档案停留于 {d_str}（已滞后 {age_days} 天），新增候选股暂由底线规则兜底，请执行盘后深度排雷。"
        if is_stale
        else f"AI 排雷档案最新日期 {d_str}（覆盖 {items_cnt} 只标的）"
    )
    return {
        "decision_date": d_str,
        "generated_at": saved_doc.get("generated_at"),
        "items_count": items_cnt,
        "age_days": age_days,
        "is_stale": is_stale,
        "stale_notice": stale_notice,
    }


def evaluate_buy_risk_gate(
    code: str,
    *,
    name: str = "",
    is_etf: bool = False,
    saved_doc: Optional[Mapping[str, Any]] = None,
    recent_st_map: Optional[Mapping[str, Mapping[str, str]]] = None,
    base_dir: Optional[Path] = None,
    now_bj: Optional[datetime] = None,
) -> dict[str, Any]:
    """AR4.6 全链路唯一权威「买前 AI 排雷严格版风控门禁」。

    严格版规则：
    - ETF 豁免个股 AI 排雷；
    - 现役 ST/*ST/退市警示：红灯一票否决（hard_blocked=True）；
    - AI 排雷红灯（red）：硬禁买（hard_blocked=True）；
    - AI 排雷黄灯（yellow）且催化分 <= 2：等同红灯硬禁买（hard_blocked=True）；
    - 近 12 个月曾 ST 刚摘帽（source_instrument_alias 命中）且未经 AI 明确洗白为绿灯：
      自动按黄灯低催化（<=2）硬禁买（hard_blocked=True）；
    - AI 排雷黄灯（yellow）且催化分 >= 3：不硬阻断，但降权排在绿灯之后并强制风险警告（demote_warn）；
    - AI 排雷绿灯（green）：放行（allow）；
    - 暂无 AI 深度排雷档案（unknown）：放行但提示人工核对公告（unscreened_warn）。
    """
    code6 = _norm_code6(code)
    if is_etf or code6.startswith(("51", "56", "58", "159", "16")):
        return {
            "code6": code6,
            "risk_light": "green",
            "catalyst_score": None,
            "risk_flags": [],
            "one_line_thesis": "",
            "decision_date": None,
            "is_stale": False,
            "source": "etf_exempt",
            "buy_action": "allow",
            "hard_blocked": False,
            "reason": "ETF 标的豁免个股排雷",
        }

    doc = saved_doc if saved_doc is not None else load_saved_screening(base_dir=base_dir)
    fresh = get_screening_freshness(doc, now_bj=now_bj)
    sentinel = lookup_stock_ai_sentinel(code6, saved_doc=doc, base_dir=base_dir)
    st_map = recent_st_map if recent_st_map is not None else load_recent_st_alias_map()
    st_hit = st_map.get(code6) if isinstance(st_map, Mapping) else None

    flags: list[str] = []
    name_upper = str(name or "").upper()
    if "ST" in name_upper or "退" in str(name or ""):
        return {
            "code6": code6,
            "risk_light": "red",
            "catalyst_score": 1,
            "risk_flags": ["ST/退市风险股（红灯一票否决）"],
            "one_line_thesis": "触发确定性底线红灯：ST/退市风险股禁止买入。",
            "decision_date": fresh["decision_date"],
            "is_stale": fresh["is_stale"],
            "source": "rule_baseline",
            "buy_action": "block",
            "hard_blocked": True,
            "reason": "ST/退市风险股（红灯一票否决）",
        }

    if st_hit:
        old_nm = str(st_hit.get("old_name") or "ST")
        last_at = str(st_hit.get("last_st_at") or "")
        flags.append(f"近12个月曾ST刚摘帽（曾用名 {old_nm}{f'，{last_at}' if last_at else ''}）")

    if isinstance(sentinel, Mapping):
        light = str(sentinel.get("risk_light") or "yellow").lower()
        cat_score = int(_safe_float(sentinel.get("catalyst_score")) or 3)
        merged_flags = list(dict.fromkeys([*flags, *(sentinel.get("risk_flags") or [])]))
        thesis = str(sentinel.get("one_line_thesis") or "")
        src = str(sentinel.get("source") or "agent_enriched")
        first_flag = merged_flags[0] if merged_flags else thesis[:60]

        if light == "red":
            return {
                "code6": code6,
                "risk_light": "red",
                "catalyst_score": cat_score,
                "risk_flags": merged_flags,
                "one_line_thesis": thesis,
                "decision_date": sentinel.get("decision_date") or fresh["decision_date"],
                "is_stale": fresh["is_stale"],
                "source": src,
                "buy_action": "block",
                "hard_blocked": True,
                "reason": f"AI排雷红灯一票否决（{first_flag or '重大风险'}）",
            }
        if light == "yellow":
            if cat_score <= STRICT_YELLOW_BLOCK_MAX_CATALYST or st_hit:
                return {
                    "code6": code6,
                    "risk_light": "yellow",
                    "catalyst_score": min(cat_score, STRICT_YELLOW_BLOCK_MAX_CATALYST),
                    "risk_flags": merged_flags,
                    "one_line_thesis": thesis,
                    "decision_date": sentinel.get("decision_date") or fresh["decision_date"],
                    "is_stale": fresh["is_stale"],
                    "source": src,
                    "buy_action": "block",
                    "hard_blocked": True,
                    "reason": f"AI排雷黄灯低催化({cat_score}/5，严格版禁买：{first_flag or '高危隐患'})",
                }
            return {
                "code6": code6,
                "risk_light": "yellow",
                "catalyst_score": cat_score,
                "risk_flags": merged_flags,
                "one_line_thesis": thesis,
                "decision_date": sentinel.get("decision_date") or fresh["decision_date"],
                "is_stale": fresh["is_stale"],
                "source": src,
                "buy_action": "demote_warn",
                "hard_blocked": False,
                "reason": f"AI排雷黄灯警示({cat_score}/5，已降权：{first_flag or '需核对公告'})",
            }
        # green：若同时命中近12个月刚摘帽但 AI 已明确深度核查判绿灯，保留提示但不硬拦
        return {
            "code6": code6,
            "risk_light": "green",
            "catalyst_score": cat_score,
            "risk_flags": merged_flags,
            "one_line_thesis": thesis,
            "decision_date": sentinel.get("decision_date") or fresh["decision_date"],
            "is_stale": fresh["is_stale"],
            "source": src,
            "buy_action": "allow",
            "hard_blocked": False,
            "reason": f"AI排雷绿灯通过（催化 {cat_score}/5）",
        }

    # 无 AI 深度排雷记录，但命中近 12 个月刚摘帽底线 -> 严格版硬拦截！
    if st_hit:
        old_nm = str(st_hit.get("old_name") or "ST")
        return {
            "code6": code6,
            "risk_light": "yellow",
            "catalyst_score": 2,
            "risk_flags": flags,
            "one_line_thesis": f"底线规则命中：近12个月曾带ST（{old_nm}），未经AI深度排雷洗白前严禁自动买入。",
            "decision_date": fresh["decision_date"],
            "is_stale": fresh["is_stale"],
            "source": "rule_baseline_recent_st",
            "buy_action": "block",
            "hard_blocked": True,
            "reason": f"近12个月曾ST刚摘帽（{old_nm}，严格版禁买）",
        }

    return {
        "code6": code6,
        "risk_light": "unknown",
        "catalyst_score": None,
        "risk_flags": [],
        "one_line_thesis": "",
        "decision_date": fresh["decision_date"],
        "is_stale": fresh["is_stale"],
        "source": "unscreened",
        "buy_action": "unscreened_warn",
        "hard_blocked": False,
        "reason": (
            f"暂无该股AI深度排雷档案（最新档案 {fresh['decision_date'] or '缺失'}），请人工核对近期公告与减持"
        ),
    }


def _screening_dir(base_dir: Optional[Path] = None) -> Path:
    p = base_dir or runtime_path("g3_ai_screening")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _norm_code6(code: Any) -> str:
    return str(code or "").split(".")[0].strip().zfill(6)


def _norm_date(raw: Any) -> str:
    s = str(raw or "").strip()
    m = re.search(r"(20\d{2})[-/]?(\d{2})[-/]?(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return datetime.now(BJ_TZ).strftime("%Y-%m-%d")


def _safe_float(v: Any) -> Optional[float]:
    if v in (None, "", "-"):
        return None
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _read_json_safe(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 1. 确定性底线排雷（无 Agent 注入时的诚实兜底）
# ---------------------------------------------------------------------------

def evaluate_rule_baseline_item(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """对单只主升候选执行确定性基础排雷与催化初评。"""
    code = str(candidate.get("code") or "")
    code6 = _norm_code6(code)
    name = str(candidate.get("name") or code)
    industry = str(candidate.get("industry") or "未分类")
    stage = str(candidate.get("stage") or "")
    score = _safe_float(candidate.get("score")) or 0.0
    resonance = int(_safe_float(candidate.get("resonance")) or 0)

    risk_flags: list[str] = []
    risk_light = "green"

    name_upper = name.upper()
    if "ST" in name_upper or "退" in name:
        risk_light = "red"
        risk_flags.append("ST/退市风险股（红灯一票否决）")

    if stage == "data_blocked":
        if risk_light != "red":
            risk_light = "yellow"
        risk_flags.append("30m/日线数据阻断，暂不可确认买点")

    if code6.startswith(("8", "4", "92")):
        if risk_light == "green":
            risk_light = "yellow"
        risk_flags.append("北交所高弹性标的，流动性与波动风险偏高")

    if resonance < 2:
        if risk_light == "green":
            risk_light = "yellow"
        risk_flags.append("同业共振偏弱（<2只）")

    # 基础催化评分（1~4分；5分保留给 Agent 确认业绩/政策/订单强催化）
    if risk_light == "red":
        catalyst_score = 1
        catalyst_type = "风险排除"
        thesis = f"触发红灯排雷：{'；'.join(risk_flags)}，禁止纳入开仓池。"
    else:
        if score >= 95 and resonance >= 4:
            catalyst_score = 4
            catalyst_type = "板块强共振"
        elif score >= 90 and resonance >= 3:
            catalyst_score = 3
            catalyst_type = "板块共振"
        else:
            catalyst_score = 2
            catalyst_type = "量价主升"
        if risk_flags:
            thesis = f"{industry}主升分 {score:.1f}（共振 {resonance} 只）；注意：{'；'.join(risk_flags)}。待 Agent 补充公告/财报穿透。"
        else:
            thesis = f"{industry}主升分 {score:.1f}（同业共振 {resonance} 只），基础量价形态健康；待 Agent 补充公告排雷与业绩催化穿透。"

    return {
        "code": code or code6,
        "code6": code6,
        "name": name,
        "industry": industry,
        "risk_light": risk_light,
        "risk_flags": risk_flags,
        "catalyst_score": catalyst_score,
        "catalyst_type": catalyst_type,
        "one_line_thesis": thesis,
        "source": "rule_baseline",
        "updated_at": datetime.now(BJ_TZ).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# 2. Agent 深度排雷催化落盘与读取
# ---------------------------------------------------------------------------

def save_agent_screening(
    *,
    decision_date: str,
    items: Sequence[Mapping[str, Any]],
    summary_note: Optional[str] = None,
    agent_name: str = "antigravity_agent",
    merge_existing: bool = True,
    base_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """校验并落盘 Agent 产出的结构化排雷与催化评分结果（默认同日增量合并，不覆盖已有标的）。"""
    d_str = _norm_date(decision_date)
    ymd = d_str.replace("-", "")
    now_iso = datetime.now(BJ_TZ).isoformat(timespec="seconds")

    store_dir = _screening_dir(base_dir)
    dated_path = store_dir / f"{ymd}.json"
    latest_path = store_dir / "latest.json"

    normalized_items: dict[str, dict[str, Any]] = {}
    existing_note: Optional[str] = None
    if merge_existing:
        existing = _read_json_safe(dated_path) or _read_json_safe(latest_path)
        if existing and str(existing.get("decision_date") or "") == d_str:
            existing_map = existing.get("items_by_code")
            if isinstance(existing_map, Mapping):
                for k, v in existing_map.items():
                    if isinstance(v, Mapping):
                        normalized_items[str(k)] = dict(v)
            existing_note = existing.get("summary_note")
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        code_raw = str(raw.get("code") or "").strip()
        code6 = _norm_code6(code_raw)
        if not code6 or code6 == "000000":
            continue
        light = str(raw.get("risk_light") or "yellow").lower().strip()
        if light not in VALID_LIGHTS:
            light = "yellow"
        c_score = int(_safe_float(raw.get("catalyst_score")) or 3)
        c_score = max(1, min(5, c_score))
        flags = [str(x).strip() for x in (raw.get("risk_flags") or []) if str(x).strip()]
        normalized_items[code6] = {
            "code": code_raw or code6,
            "code6": code6,
            "name": str(raw.get("name") or ""),
            "industry": str(raw.get("industry") or ""),
            "risk_light": light,
            "risk_flags": flags,
            "catalyst_score": c_score,
            "catalyst_type": str(raw.get("catalyst_type") or "逻辑催化").strip(),
            "one_line_thesis": str(raw.get("one_line_thesis") or "").strip() or "Agent 已完成公告与催化复核。",
            "source": str(raw.get("source") or "agent_enriched"),
            "updated_at": now_iso,
        }

    doc = {
        "schema": "g3_ai_screening/1",
        "decision_date": d_str,
        "generated_at": now_iso,
        "agent_name": agent_name,
        "summary_note": summary_note or existing_note or f"Agent 已完成 {len(normalized_items)} 只标的公告排雷与催化逻辑评估",
        "items_by_code": normalized_items,
    }

    text = json.dumps(doc, ensure_ascii=False, indent=2)
    dated_path.write_text(text, encoding="utf-8")
    latest_path.write_text(text, encoding="utf-8")
    return doc


def lookup_stock_ai_sentinel(
    code: str,
    *,
    saved_doc: Optional[Mapping[str, Any]] = None,
    base_dir: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """供 /g3/desk 持仓股卡片查询最新 AI 公告/事件哨兵状态（共用同一排雷库，绝不孤立）。"""
    code6 = _norm_code6(code)
    doc = saved_doc if saved_doc is not None else load_saved_screening(base_dir=base_dir)
    if not isinstance(doc, Mapping):
        return None
    items_map = doc.get("items_by_code")
    if not isinstance(items_map, Mapping):
        return None
    item = items_map.get(code6)
    if not isinstance(item, Mapping):
        return None
    light = str(item.get("risk_light") or "yellow").lower()
    return {
        "code6": code6,
        "risk_light": light,
        "forbid_forward_t": light == "red",
        "catalyst_score": int(_safe_float(item.get("catalyst_score")) or 3),
        "catalyst_type": str(item.get("catalyst_type") or ""),
        "risk_flags": list(item.get("risk_flags") or []),
        "one_line_thesis": str(item.get("one_line_thesis") or ""),
        "source": str(item.get("source") or "agent_enriched"),
        "decision_date": str(doc.get("decision_date") or ""),
        "updated_at": item.get("updated_at") or doc.get("generated_at"),
    }


def load_saved_screening(
    decision_date: Optional[str] = None,
    *,
    base_dir: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """优先按 decision_date 精确读取，未指定时回退 latest.json。"""
    store_dir = base_dir or runtime_path("g3_ai_screening")
    if not store_dir.exists():
        return None
    if decision_date:
        ymd = _norm_date(decision_date).replace("-", "")
        exact = _read_json_safe(store_dir / f"{ymd}.json")
        if exact:
            return exact
    return _read_json_safe(store_dir / "latest.json")


# ---------------------------------------------------------------------------
# 3. 融合进 /api/g3/mainwave 输出
# ---------------------------------------------------------------------------

def merge_screening_into_mainwave(
    mainwave_payload: Mapping[str, Any],
    *,
    market_gate: Optional[Mapping[str, Any]] = None,
    base_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """将 AI 排雷与催化结果融合进主升浪扫描输出，生成绿灯精选池并联动大盘总闸门。"""
    result = dict(mainwave_payload)
    decision_date = str(result.get("decision_date") or "")
    saved = load_saved_screening(decision_date or None, base_dir=base_dir)
    saved_same_day = bool(saved and decision_date and str(saved.get("decision_date") or "") == decision_date)
    saved_map = (saved.get("items_by_code") or {}) if saved_same_day else {}

    enriched_candidates: list[dict[str, Any]] = []
    counts = {"green": 0, "yellow": 0, "red": 0}
    agent_enriched_count = 0

    for raw_cand in result.get("candidates") or []:
        cand = dict(raw_cand)
        code6 = _norm_code6(cand.get("code"))
        baseline = evaluate_rule_baseline_item(cand)
        agent_item = saved_map.get(code6)
        if isinstance(agent_item, Mapping):
            # 保留红灯一票否决（若底线已触发 ST/退市红灯，Agent 不得洗白为绿灯）
            final_light = "red" if baseline["risk_light"] == "red" else str(agent_item.get("risk_light") or "yellow")
            merged_flags = list(dict.fromkeys([*baseline["risk_flags"], *(agent_item.get("risk_flags") or [])]))
            ai_info = {
                **baseline,
                **agent_item,
                "risk_light": final_light,
                "risk_flags": merged_flags,
                "source": str(agent_item.get("source") or "agent_enriched"),
            }
            if ai_info["source"] == "agent_enriched":
                agent_enriched_count += 1
        else:
            ai_info = baseline

        counts[ai_info["risk_light"]] = counts.get(ai_info["risk_light"], 0) + 1
        cand["ai_screening"] = ai_info
        enriched_candidates.append(cand)

    # 排序：绿灯优先 -> 催化分降序 -> 主升分降序
    top_picks_sorted = sorted(
        [c for c in enriched_candidates if (c.get("ai_screening") or {}).get("risk_light") == "green"],
        key=lambda x: (
            -int((x.get("ai_screening") or {}).get("catalyst_score") or 0),
            -(_safe_float(x.get("score")) or 0.0),
        ),
    )
    top_picks = [
        {
            "code": c.get("code"),
            "name": c.get("name"),
            "industry": c.get("industry"),
            "score": c.get("score"),
            "resonance": c.get("resonance"),
            "stage": c.get("stage"),
            "risk_light": c["ai_screening"]["risk_light"],
            "catalyst_score": c["ai_screening"]["catalyst_score"],
            "catalyst_type": c["ai_screening"]["catalyst_type"],
            "one_line_thesis": c["ai_screening"]["one_line_thesis"],
            "source": c["ai_screening"]["source"],
        }
        for c in top_picks_sorted[:5]
    ]

    allow_new_mainwave = True
    gate_regime = None
    gate_label = None
    if isinstance(market_gate, Mapping):
        allow_new_mainwave = bool(market_gate.get("allow_new_mainwave", True))
        gate_regime = market_gate.get("regime")
        gate_label = market_gate.get("regime_label")

    mode = "agent_enriched" if agent_enriched_count > 0 else "rule_baseline"
    gate_notice = (
        f"当前大盘处于【{gate_label or '防守模式'}】，严禁开新主升仓；以下绿灯精选仅作跟踪观察池，待大盘转暖再出手。"
        if not allow_new_mainwave
        else f"当前大盘【{gate_label or '允许开仓'}】，可优先从绿灯高催化池中结合 10:00 首根 30m 确认择优介入。"
    )

    result["candidates"] = enriched_candidates
    result["ai_screening_summary"] = {
        "schema": "g3_ai_screening_summary/1",
        "decision_date": decision_date,
        "mode": mode,
        "agent_enriched_count": agent_enriched_count,
        "total_candidates": len(enriched_candidates),
        "counts": counts,
        "allow_new_mainwave": allow_new_mainwave,
        "market_gate_regime": gate_regime,
        "gate_notice": gate_notice,
        "agent_summary_note": saved.get("summary_note") if saved_same_day else None,
        "generated_at": (saved.get("generated_at") if saved_same_day else datetime.now(BJ_TZ).isoformat(timespec="seconds")),
        "top_picks": top_picks,
    }
    return result
