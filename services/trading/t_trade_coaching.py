# -*- coding: utf-8 -*-
"""R48 每日教练式复盘（reconcile 升级，纯函数模块）。

任务书 20261111_holdings_poller_r48.md §2：每笔成交（含 inferred 即时版 /
盘后精确版）标注信号对照与规则违反，日终产出 coaching 摘要字段
{纪律评分, 跟单率, 最差一笔归因, 明日提醒一句话}——写入 ledger summary
`coaching` 键，PM watcher 15:12 原样推送。

设计约束：
  * 零 tts 反向依赖（tts.run_reconcile 函数内延迟 import 本模块，避免环）；
  * 全部输入可注入（fills/草案/周期/契约/日内高低点/周内接飞刀先验），
    单测零网络零 THS；
  * fail-honest：数据面缺失（fills unavailable / day_low 拿不到）→ 如实
    标注 not_scored / day_low_unavailable，绝不编造。

信号对照口径（每笔成交）：
  跟单   —— 同向草案命中且草案时间 ≤ 成交时间 +120s（草案先于动手；
            Owner 常见"先动手后出草案"，时间上不可能跟单）；
  带外   —— 无同向草案覆盖（草案未覆盖时的自发交易；含"草案后至同向"）；
  计划外 —— 无同向草案但有反向草案在窗（±40min，与引擎匹配窗一致）仍动手。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

SCHEMA_COACHING = "t_tracker_coaching/1"
MATCH_WINDOW_MIN = 10      # inferred ↔ fills 覆盖容差（任务书：同窗±10min）
DRAFT_MATCH_WINDOW_MIN = 40  # 与引擎 match_fill_to_draft 一致
FOLLOW_SKEW_S = 120        # 草案晚于成交 >2min → 不算跟单（先动手后出草案）
LATE_ENTRY_AFTER = "14:30"

PENALTIES = {
    "knife_catch": 15,
    "contra_plan": 20,
    "off_plan": 10,
    "oversize": 5,
    "late_entry": 5,
    "contract_ignored": 25,
    "contract_trim_unexecuted": 10,
}


# --------------------------------------------------------------------------- #
# 15:05 fills 精确覆盖 inferred（±10min 同码同向同窗）
# --------------------------------------------------------------------------- #

def _parse_ts(text: str) -> Optional[datetime]:
    try:
        return datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _norm_fill_row(row: Mapping[str, Any], default_account: str = "acct1") \
        -> Optional[dict[str, Any]]:
    ts = _parse_ts(str(row.get("trade_time") or row.get("ts") or ""))
    code = str(row.get("code") or "").split(".")[0]
    side = str(row.get("side") or "").upper()
    shares = int(row.get("shares") or row.get("delta_qty") or 0)
    price = float(row.get("price") or 0.0)
    if not ts or not code or side not in ("BUY", "SELL") or shares <= 0:
        return None
    return {
        "ts": ts, "code": code, "side": side, "shares": shares,
        "price": price,
        "amount": float(row.get("amount") or round(price * shares, 2)),
        "account": str(row.get("account") or default_account),
    }


def override_inferred_with_fills(inferred_rows: Sequence[Mapping[str, Any]],
                                 fills_rows: Sequence[Mapping[str, Any]],
                                 window_min: int = MATCH_WINDOW_MIN,
                                 default_account: str = "acct1") -> list[dict]:
    """以真实成交价/量覆盖 inferred 行（返回更新后的行列表；幂等：已
    matched 的行不再改写）。同窗多笔（拆单 100+1900）按窗口内合计与
    加权价覆盖，全部成交引用留痕。"""
    fills = [f for f in (_norm_fill_row(r, default_account) for r in fills_rows)
             if f is not None]
    out: list[dict] = []
    for row in inferred_rows:
        row = dict(row)
        prev = row.get("fill_override")
        if isinstance(prev, Mapping) and prev.get("status") == "matched":
            out.append(row)
            continue
        ts = _parse_ts(str(row.get("ts") or ""))
        want = "BUY" if str(row.get("side")) == "buy" else "SELL"
        account = str(row.get("account") or default_account)
        delta = abs(int(row.get("delta_qty") or 0))
        if ts is None or delta == 0:
            row["fill_override"] = {"status": "invalid_row"}
            out.append(row)
            continue
        in_win = [f for f in fills
                  if f["account"] == account and f["code"] == str(row.get("code"))
                  and f["side"] == want
                  and abs((f["ts"] - ts).total_seconds()) <= window_min * 60]
        if not in_win:
            row["fill_override"] = {"status": "unmatched"}
            out.append(row)
            continue
        in_win.sort(key=lambda f: abs((f["ts"] - ts).total_seconds()))
        group = [f for f in in_win if f["shares"] <= delta] or in_win[:1]
        # 拆单合并：窗口内全部同向成交若合计恰等于 |delta| 则整组吸收
        if sum(f["shares"] for f in group) != delta \
                and sum(f["shares"] for f in in_win) == delta:
            group = in_win
        qty = sum(f["shares"] for f in group)
        amount = sum(f["price"] * f["shares"] for f in group)
        weighted = round(amount / max(qty, 1), 3)
        price_ref = row.get("price_ref")
        row["fill_override"] = {
            "status": "matched",
            "fills": [{"ts": f["ts"].strftime("%Y-%m-%d %H:%M:%S"),
                       "price": f["price"], "shares": f["shares"],
                       "amount": f["amount"]} for f in group],
            "sum_shares": qty, "price_weighted": weighted,
            "ts_nearest": group[0]["ts"].strftime("%Y-%m-%d %H:%M:%S"),
            "price_ref_vs_fill": (round(weighted - float(price_ref), 3)
                                  if isinstance(price_ref, (int, float))
                                  else None),
            "also_in_window": [
                {"ts": f["ts"].strftime("%Y-%m-%d %H:%M:%S"),
                 "shares": f["shares"]} for f in in_win if f not in group],
        }
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# 每笔标注 + 日终 coaching
# --------------------------------------------------------------------------- #

def _regime_of(regime_doc: Mapping[str, Any], code: str) -> str:
    return str(((regime_doc or {}).get("codes") or {}).get(code, {}) \
               .get("regime") or "")


def _draft_window_hits(drafts: Sequence[Mapping[str, Any]], *, code: str,
                       side: str, ts: datetime,
                       window_min: int = DRAFT_MATCH_WINDOW_MIN) -> list[dict]:
    out = []
    for d in drafts:
        if str(d.get("code")) != code or str(d.get("side")) != side:
            continue
        d_ts = _parse_ts(str(d.get("ts") or ""))
        if d_ts is None:
            continue
        if abs((ts - d_ts).total_seconds()) <= window_min * 60:
            out.append(dict(d))
    return out


def annotate_fill(fill: Mapping[str, Any], *, drafts: Sequence[Mapping[str, Any]],
                  matched_draft: Optional[Mapping[str, Any]],
                  regime: str, knife_week_no: Optional[int]) -> dict[str, Any]:
    """单笔成交标注：信号对照 / 规则违反 / 执行质量（滑点）。"""
    ts = fill["ts"]
    side = fill["side"]
    code = fill["code"]
    violations: list[dict[str, Any]] = []
    slippage = None
    vs = None

    same_side = "buy-T" if side == "BUY" else "sell-T"
    opp_side = "sell-T" if side == "BUY" else "buy-T"
    same_hits = _draft_window_hits(drafts, code=code, side=same_side, ts=ts)
    opp_hits = _draft_window_hits(drafts, code=code, side=opp_side, ts=ts)

    follow = None
    if matched_draft is not None:
        d_ts = _parse_ts(str(matched_draft.get("ts") or ""))
        if d_ts is not None and (ts - d_ts).total_seconds() >= -FOLLOW_SKEW_S:
            follow = True
    if follow:
        signal = "跟单"
        draft_id = matched_draft.get("id")
        px = float(matched_draft.get("px") or 0.0)
        if px > 0:
            slippage = round(float(fill["price"]) - px, 3)  # 买:正=更差 卖:负=更差
            vs = px
    elif opp_hits:
        signal = "计划外"
        draft_id = None
        violations.append({"type": "contra_plan",
                           "label": "计划外（有反向草案仍动手）",
                           "opp_draft_ids": [d.get("id") for d in opp_hits]})
    else:
        signal = "带外"
        draft_id = (matched_draft or {}).get("id") if matched_draft else None
        note = "草案后至同向（先动手后出草案，时间上非跟单）" \
            if matched_draft is not None else None
        violations.append({"type": "off_plan", "label": "带外（草案未覆盖）",
                           "note": note})
        if note:
            violations[0]["draft_id"] = draft_id

    if regime == "down" and side == "BUY":
        item = {"type": "knife_catch", "label": "接飞刀（下降态买入）"}
        if knife_week_no is not None:
            item["week_no"] = knife_week_no
            item["label"] = f"接飞刀（下降态买入，本周第{knife_week_no}次）"
        violations.append(item)

    if matched_draft is not None:
        suggested = int(matched_draft.get("qty") or 0)
        if suggested > 0 and int(fill["shares"]) > suggested * 2:
            violations.append({
                "type": "oversize", "label": "超量（>草案建议2倍）",
                "suggested": suggested, "actual": int(fill["shares"]),
                "draft_id": matched_draft.get("id")})

    if side == "BUY" and ts.time() >= datetime.strptime(
            LATE_ENTRY_AFTER, "%H:%M").time():
        violations.append({"type": "late_entry",
                           "label": "14:30后开新仓",
                           "ts": ts.strftime("%H:%M:%S")})

    return {
        "ts": ts.strftime("%Y-%m-%d %H:%M:%S"), "code": code, "side": side,
        "shares": fill["shares"], "price": fill["price"],
        "signal": signal, "draft_id": draft_id,
        "slippage_vs_draft_px": slippage, "draft_px": vs,
        "violations": violations,
    }


def contract_violations(*, contract_doc: Mapping[str, Any],
                        fills_today: Sequence[Mapping[str, Any]],
                        day_hl: Optional[Mapping[str, Mapping[str, Any]]] = None,
                        ) -> list[dict[str, Any]]:
    """止损契约（contract_stop.json）执行核对：跌破 hard_stop 当日无卖出
    = 契约触发未执行；rally_trim 价位到达当日无卖出 = 减仓波未执行。
    day_hl={code: {"low","high"}}；码缺/值 None → 如实标 unavailable，绝不猜。"""
    out: list[dict[str, Any]] = []
    for rule in (contract_doc or {}).get("rules") or []:
        code = str(rule.get("code") or "")
        sold_today = any(f["code"] == code and f["side"] == "SELL"
                         for f in fills_today)
        hl = (day_hl or {}).get(code) or {}
        day_low, day_high = hl.get("low"), hl.get("high")
        hard_stop = rule.get("hard_stop")
        if hard_stop is not None:
            if day_low is None:
                out.append({"type": "contract_ignored", "code": code,
                            "hard_stop": hard_stop, "day_low": None,
                            "label": "契约核对缺日内最低价（day_low_unavailable）",
                            "status": "not_scored"})
            elif day_low < float(hard_stop) and not sold_today:
                out.append({"type": "contract_ignored", "code": code,
                            "hard_stop": hard_stop, "day_low": day_low,
                            "label": (f"止损契约触发未执行（{code} 跌破 "
                                      f"{hard_stop}，日内低 {day_low}，当日无卖出）")})
        trim = rule.get("rally_trim") or {}
        trim_px = trim.get("price")
        if trim_px is not None:
            if day_high is None:
                out.append({"type": "contract_trim_unexecuted", "code": code,
                            "trim_price": trim_px, "day_high": None,
                            "label": "契约减仓波核对缺日内最高价（unavailable）",
                            "status": "not_scored"})
            elif day_high >= float(trim_px) and not sold_today:
                out.append({"type": "contract_trim_unexecuted", "code": code,
                            "trim_price": trim_px, "day_high": day_high,
                            "label": (f"反弹减仓波未执行（{code} 触及 "
                                      f"{trim_px}，日内高 {day_high}，当日无卖出）")})
    return out


def weekly_knife_count(root: Path, for_date: date) -> int:
    """本周（周一起）至今日之前的接飞刀次数（读既有 summary coaching 字段）。"""
    monday = for_date - timedelta(days=for_date.weekday())
    total = 0
    d = monday
    while d < for_date:
        path = root / "ledger" / f"{d.strftime('%Y%m%d')}_summary.json"
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                coaching = payload.get("coaching") or {}
                total += int(coaching.get("knife_catch_count") or 0)
            except (ValueError, OSError):
                pass
        d += timedelta(days=1)
    return total


def _worst_round(trips: Sequence[Mapping[str, Any]],
                 annotations: Mapping[str, Mapping[str, Any]]) \
        -> Optional[dict[str, Any]]:
    if not trips:
        return None
    worst = min(trips, key=lambda r: (r.get("net_pnl") or 0.0))
    legs_ts = []
    for leg_key in ("buy", "sell"):
        leg = worst.get(leg_key)
        if isinstance(leg, Mapping) and leg.get("ts"):
            legs_ts.append(str(leg["ts"]))
    reasons = []
    for key, ann in annotations.items():
        if key in legs_ts and ann.get("violations"):
            reasons.extend(v.get("label") for v in ann["violations"])
    attribution = "；".join(dict.fromkeys(reasons)) if reasons \
        else "无规则违反（行情不利/正常T损耗）"
    return {
        "code": worst.get("code"),
        "net_pnl": worst.get("net_pnl"),
        "sell_ts": (worst.get("sell") or {}).get("ts"),
        "归因": attribution,
    }


def _reminder(violation_types: Sequence[str], knife_week: int) -> str:
    counts: dict[str, int] = {}
    for v in violation_types:
        counts[v] = counts.get(v, 0) + 1
    if "contract_ignored" in counts:
        return "止损契约触发未执行——明日跌破硬止损线请第一时间执行，忽略需主动留言留痕"
    if "knife_catch" in counts:
        return (f"下降态接飞刀本周已第{knife_week}次——明日下降态只出不进，"
                f"反弹减仓不抄底")
    if "contra_plan" in counts:
        return "今日有反向草案仍动手——明日下单前先比对草案方向"
    if "off_plan" in counts:
        return (f"今日{counts['off_plan']}笔带外交易——明日每笔动手前先看是否"
                f"有草案覆盖")
    if "oversize" in counts:
        return "今日超量执行——明日单笔不超草案建议的2倍"
    if "contract_trim_unexecuted" in counts:
        return "反弹减仓波未执行——明日触及契约减仓价请按波执行"
    if "late_entry" in counts:
        return "尾盘开新仓——明日14:30后不再开新仓"
    return "全日零违反——保持节奏，按草案纪律执行"


def build_coaching(*, for_date: date,
                   fills_today: Sequence[Mapping[str, Any]],
                   matches: Mapping[int, Optional[Mapping[str, Any]]],
                   drafts: Sequence[Mapping[str, Any]],
                   rounds_today: Sequence[Mapping[str, Any]],
                   regime_doc: Mapping[str, Any],
                   contract_doc: Mapping[str, Any],
                   knife_week_prior: int = 0,
                   day_hl: Optional[Mapping[str, Mapping[str, Any]]] = None,
                   inferred_rows: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """日终教练式复盘。fills_today 为**当日**该账号归一化成交（ts 为
    datetime）；matches 与引擎 run_reconcile 的 {idx: draft|None} 同构。"""
    knife_seq = 0
    trades: list[dict[str, Any]] = []
    for idx, fill in enumerate(fills_today):
        knife_week_no = None
        if _regime_of(regime_doc, fill["code"]) == "down" and fill["side"] == "BUY":
            knife_seq += 1
            knife_week_no = knife_week_prior + knife_seq
        trades.append(annotate_fill(
            fill, drafts=drafts, matched_draft=matches.get(idx),
            regime=_regime_of(regime_doc, fill["code"]),
            knife_week_no=knife_week_no))

    contract_items = contract_violations(contract_doc=contract_doc,
                                         fills_today=fills_today,
                                         day_hl=day_hl)
    scored_contract = [v for v in contract_items if v.get("status") != "not_scored"]

    all_violation_types = [v["type"] for t in trades for v in t["violations"]] \
        + [v["type"] for v in scored_contract]
    knife_total = sum(1 for v in all_violation_types if v == "knife_catch")
    penalty = sum(PENALTIES.get(v, 0) for v in all_violation_types)
    score = max(0, 100 - penalty) if fills_today or scored_contract else 100
    followed = sum(1 for t in trades if t["signal"] == "跟单")
    follow_rate = round(followed / len(fills_today), 4) if fills_today else None
    trips = [r for r in rounds_today if r.get("kind") == "round_trip"]
    annotations_by_ts = {t["ts"]: t for t in trades}

    inferred_unmatched = [
        {"ts": r.get("ts"), "code": r.get("code"), "side": r.get("side"),
         "delta_qty": r.get("delta_qty"), "price_ref": r.get("price_ref")}
        for r in inferred_rows
        if (r.get("fill_override") or {}).get("status") != "matched"]

    daily = {
        "纪律评分": score,
        "跟单率": follow_rate,
        "最差一笔归因": _worst_round(trips, annotations_by_ts),
        "明日提醒": _reminder(all_violation_types,
                              knife_week_prior + knife_total),
    }
    return {
        "schema": SCHEMA_COACHING, "date": for_date.isoformat(),
        "fills_scored": len(fills_today), "followed": followed,
        "knife_catch_count": knife_total,
        "knife_week_total": knife_week_prior + knife_total,
        "violations_total": len(all_violation_types),
        "daily": daily,
        "trades": trades,
        "contract": contract_items,
        "inferred_unmatched_by_fills": inferred_unmatched,
        "day_hl_used": dict(day_hl or {}),
        "fills_missing_note": None if fills_today else
        "当日无该账号 fills（零成交或 fills 源 unavailable）",
    }


def attach_to_summary(summary: dict[str, Any], coaching: dict[str, Any]) -> None:
    """PM watcher 15:12 直推面：中文四字段原样 + 机器字段供周内聚合。"""
    summary["coaching"] = {**coaching["daily"],
                           "fills_scored": coaching["fills_scored"],
                           "followed": coaching["followed"],
                           "knife_catch_count": coaching["knife_catch_count"],
                           "knife_week_total": coaching["knife_week_total"],
                           "violations_total": coaching["violations_total"],
                           "trades": coaching["trades"],
                           "contract": coaching["contract"],
                           "inferred_unmatched_by_fills":
                               coaching["inferred_unmatched_by_fills"]}
