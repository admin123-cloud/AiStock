# -*- coding: utf-8 -*-
"""G3 Phase 2B：盘前 09:15 AI 市场主线简报引擎（Pre-Market Brief）。

职责：
1. 聚合 Phase 1A（大盘择时总闸门 × R51 仓位水位）+ Phase 2A（主升浪候选行业共振与 AI 排雷精选池）
   + 可选的 AI Agent 隔夜政策/行业新闻研判；
2. 生成结构化盘前简报（落盘 runtime/g3_premarket_brief/YYYYMMDD.json 与 latest.json）与一页式 Markdown；
3. 支持一键通过现有微信通道（services/trading/t_notify.py）推送盘前简报。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from utils.common.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))


def _brief_dir(base_dir: Optional[Path] = None) -> Path:
    p = base_dir or runtime_path("g3_premarket_brief")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _norm_date(raw: Any) -> str:
    s = str(raw or "").strip()
    m = re.search(r"(20\d{2})[-/]?(\d{2})[-/]?(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return datetime.now(BJ_TZ).strftime("%Y-%m-%d")


def _read_json_safe(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _extract_core_themes(candidates: Sequence[Mapping[str, Any]], top_n: int = 3) -> list[dict[str, Any]]:
    """从主升浪候选池按行业聚合出当前最强的 1~3 条板块主线。"""
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for c in candidates:
        ind = str(c.get("industry") or "").strip()
        if not ind or ind in ("未分类", "未知"):
            continue
        ai = c.get("ai_screening") if isinstance(c.get("ai_screening"), Mapping) else {}
        if ai.get("risk_light") == "red":
            continue
        groups.setdefault(ind, []).append(c)

    themes: list[dict[str, Any]] = []
    for ind, rows in groups.items():
        scores = [float(r.get("score") or 0) for r in rows]
        green_cnt = sum(1 for r in rows if (r.get("ai_screening") or {}).get("risk_light") == "green")
        avg_score = round(sum(scores) / len(scores), 1) if scores else 0.0
        reps = [f"{r.get('name') or r.get('code')}({str(r.get('code') or '').split('.')[0]})" for r in rows[:3]]
        themes.append({
            "industry": ind,
            "candidate_count": len(rows),
            "green_count": green_cnt,
            "avg_score": avg_score,
            "representatives": reps,
            "strength_score": round(len(rows) * 10 + green_cnt * 4 + avg_score * 0.2, 2),
        })

    themes.sort(key=lambda x: x["strength_score"], reverse=True)
    return themes[:top_n]


def generate_premarket_brief(
    *,
    market_gate: Mapping[str, Any],
    mainwave_payload: Optional[Mapping[str, Any]] = None,
    strategy_matrix: Optional[Mapping[str, Any]] = None,
    strategy_router: Optional[Mapping[str, Any]] = None,
    ar3_plan: Optional[Mapping[str, Any]] = None,
    overnight_notes: Optional[Sequence[str]] = None,
    agent_thesis: Optional[str] = None,
    source: str = "auto_baseline",
    trade_date: Optional[str] = None,
    base_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """生成并落盘当日 09:10 盘前 AI 晨会简报（100% 统一为 AR3 v3.0 终极双核口径，彻底停推旧主升票）。"""
    now_bj = datetime.now(BJ_TZ)
    d_str = _norm_date(trade_date or market_gate.get("ref_date") or now_bj.strftime("%Y-%m-%d"))
    ymd = d_str.replace("-", "")

    wl = market_gate.get("r51_waterline") if isinstance(market_gate.get("r51_waterline"), Mapping) else {}
    sources = market_gate.get("sources") if isinstance(market_gate.get("sources"), Mapping) else {}
    mood = sources.get("market_mood") if isinstance(sources.get("market_mood"), Mapping) else {}
    idx = sources.get("index_r51") if isinstance(sources.get("index_r51"), Mapping) else {}
    tf = sources.get("turnover_forecast") if isinstance(sources.get("turnover_forecast"), Mapping) else {}

    regime = str((ar3_plan or {}).get("regime") or market_gate.get("regime") or "defensive")
    regime_label = str((ar3_plan or {}).get("regime_label") or market_gate.get("regime_label") or "防守模式")
    is_offensive = regime in ("offensive", "extreme_offensive")
    allow_new = is_offensive

    # 若传入 AR3 v3.0 调仓计划，彻底以 AR3 v3.0（ct1曾单账号 + 50% AB混合 + #01 跨资产ETF）为唯一真值
    if isinstance(ar3_plan, Mapping) and ar3_plan.get("account"):
        acct = ar3_plan.get("account") or {}
        cf = ar3_plan.get("cash_flow") or {}
        cfg = ar3_plan.get("config") or {}
        sells = [o for o in (ar3_plan.get("sell_orders") or []) if int(o.get("suggested_shares") or 0) > 0]
        buys = list(ar3_plan.get("buy_orders") or [])
        me = ar3_plan.get("money_effect_20d") or {}
        macro_dash = ar3_plan.get("macro_dashboard") or {}
        macro_dec = macro_dash.get("decision") if isinstance(macro_dash, Mapping) else {}
        raw_eff_cap = cfg.get("effective_stock_budget_cap_pct")
        stock_cap = int(raw_eff_cap if raw_eff_cap is not None else (cfg.get("stock_budget_cap_pct") or 50))
        stock_mode = str(cfg.get("active_stock_mode") or "ab_blend")
        etf_mode = str(cfg.get("etf_core_mode") or "regime_focus")
        active_strat_name = f"AR4.5-L3b 终极双核（{stock_cap}% {stock_mode} + 风险调整Top2逆波ETF）"
        actual_pct = acct.get("position_pct")
        cap_pct = 100.0  # AR4.5-L3b 物理单账户零杠杆：进攻期按国家队分位动态调节股仓(0/35/50/65%)，非进攻期 0%股+100%非A股Top2逆波ETF

        alloc_line = (
            f"AR3-F基本面个股 {stock_cap}%({stock_mode}) + AR4.5跨资产ETF {100 - stock_cap}%(含闲置现金)"
            if is_offensive and stock_cap > 0
            else (
                "ERP极贵逃顶阀(<5%)触发 -> AR3-F个股 0% + AR4.5避险ETF 100%"
                if is_offensive and stock_cap <= 0
                else "AR3-F个股 0%(锁仓禁开) + AR4.5非A股风险调整Top2逆波ETF 100%(含冰点反抽)"
            )
        )
        macro_line = None
        if isinstance(macro_dash, Mapping) and macro_dash.get("available"):
            inflow5 = float(macro_dash.get("broad6_net_inflow_5d_yi") or 0.0)
            inflow_sign = "+" if inflow5 >= 0 else ""
            sb20 = macro_dash.get("sb_net_20d_yi")
            sb_part = f"｜南向20日净买 {float(sb20):+,.0f}亿" if sb20 is not None else ""
            macro_line = (
                f"国家队250日分位 {macro_dash.get('nt_ratio_pctile_250d')}%（{(macro_dec or {}).get('phase_label') or '—'}）"
                f"｜6大宽基5日净申购 {inflow_sign}{inflow5:,.0f}亿{sb_part}"
                f"｜沪深300 ERP {macro_dash.get('erp_pct')}%｜进攻股仓生效上限 {stock_cap}%"
            )

        checklist: list[str] = [
            f"【AR3 v3.0 总控】大盘={regime_label}｜主力账号 {acct.get('label')}（总资产 ¥{acct.get('total_assets', 0):,.0f}｜可用 ¥{acct.get('available_cash', 0):,.0f}｜现仓位 {actual_pct}%）",
            f"【目标配额】{alloc_line}",
        ]
        if sells:
            sell_txt = "；".join(
                f"卖 {o.get('name', '')}({o.get('code', '')}) {int(o.get('suggested_shares') or o.get('shares') or 0)}股@≤¥{o.get('limit_price', o.get('ref_price', '—'))}(约¥{float(o.get('est_amount') or o.get('est_yuan') or 0):,.0f})"
                for o in sells
            )
            checklist.append(f"【第一步·先卖清单】{sell_txt}（预计回款 ¥{float(cf.get('est_sell_proceeds') or 0):,.0f}）")
        else:
            checklist.append("【第一步·先卖清单】当前无须卖出调仓。")

        if buys:
            buy_txt = "；".join(
                f"买 {o.get('name', '')}({o.get('code', '')}) 现可买{int(o.get('immediate_shares') or 0)}股/卖后总买{int(o.get('post_sell_shares') or o.get('shares') or 0)}股@≥¥{o.get('limit_price', o.get('ref_price', '—'))}(止损¥{o.get('stop_loss_price', '—')})"
                for o in buys
            )
            checklist.append(f"【第二步·后买清单】{buy_txt}")
        else:
            checklist.append("【第二步·后买清单】当前无须买入新仓。")

        if me.get("headline"):
            checklist.append(f"【近20日双线赚钱效应】{me['headline']}")

        turnover_str = f"{tf['forecast_trillion']}万亿" if tf.get("forecast_trillion") is not None else "—"
        lines = [
            f"⚡ 【AR4.5-L3b 盘前终极战报 & 买卖单 · {d_str}】",
            f"■ 主力账号：{acct.get('label') or acct.get('display_name') or acct.get('account_id', 'acct1')}｜总资产 ¥{float(acct.get('total_assets') or 0):,.0f}｜可用 ¥{float(acct.get('available_cash') or 0):,.0f}｜现仓位 {actual_pct}%",
            f"■ 大盘五态：{regime_label}｜上证AR3趋势={idx.get('confirmed_regime') or '—'}（连{idx.get('streak_days') or 0}日）｜涨停{mood.get('limit_up_count', '—')}/跌停{mood.get('limit_down_count', '—')}｜成交额={turnover_str}",
            f"■ AR3 配额：{alloc_line}",
        ]
        if macro_line:
            lines.append(f"■ 国家队与ERP：{macro_line}")
        if agent_thesis:
            lines.append(f"■ AI 隔夜研判：{agent_thesis.strip()}")
        if overnight_notes:
            lines.append("■ 隔夜要点：" + "；".join(str(x).strip() for x in overnight_notes if str(x).strip()))
        lines.append("■ 今日最终执行清单：")
        for idx_i, item in enumerate(checklist, 1):
            lines.append(f"  {idx_i}. {item}")
        lines.append("👉 打开作战台 /g3/desk 点「一键顺序预填至同花顺」，核对后按确认即可执行。")

        markdown_text = "\n".join(lines)
        brief = {
            "schema": "g3_premarket_brief/2_ar3",
            "date": d_str,
            "generated_at": now_bj.isoformat(timespec="seconds"),
            "source": source,
            "market_stance": {
                "regime": regime,
                "regime_label": regime_label,
                "tone": market_gate.get("tone") or ("emerald" if is_offensive else "red"),
                "allow_new_mainwave": allow_new,
                "target_cap_pct": cap_pct,
                "actual_pct": actual_pct,
                "waterline_status": "ar3_dual_core",
                "waterline_advice": alloc_line,
            },
            "core_themes": [],
            "themes_title": "AR3 v3.0 终极双核目标配置",
            "active_strategy_name": active_strat_name,
            "top_picks": [],
            "ar3_summary": {
                "account_label": acct.get("label"),
                "sell_count": len(sells),
                "buy_count": len(buys),
                "sell_orders": sells,
                "buy_orders": buys,
                "money_effect_20d": me,
            },
            "overnight_notes": [str(x).strip() for x in (overnight_notes or []) if str(x).strip()],
            "agent_thesis": (agent_thesis or "").strip() or None,
            "action_checklist": checklist,
            "markdown_text": markdown_text,
        }
        store_dir = _brief_dir(base_dir)
        text = json.dumps(brief, ensure_ascii=False, indent=2)
        (store_dir / f"{ymd}.json").write_text(text, encoding="utf-8")
        (store_dir / "latest.json").write_text(text, encoding="utf-8")
        return brief

    # 兼容单测或未传入 ar3_plan 时的回退分支
    mw = dict(mainwave_payload or {})
    candidates = mw.get("candidates") or []
    ai_sum = mw.get("ai_screening_summary") if isinstance(mw.get("ai_screening_summary"), Mapping) else {}
    top_picks = list(ai_sum.get("top_picks") or [])[:5]
    core_themes = _extract_core_themes(candidates, top_n=3)

    cap_pct = float(market_gate.get("target_position_cap_pct") or 25.0)
    actual_pct = wl.get("actual_pct")
    is_defensive = regime in ("defensive", "extreme_panic") or not allow_new

    active_strat_name = (
        str((strategy_router or {}).get("active_strategy_name") or "")
        or ("现金防守 / 严禁开新仓" if is_defensive else "周期生效策略")
    )
    if is_defensive:
        themes_title = "抗跌/避险资金流向（仅观察·严禁开新仓）"
    elif regime == "balanced":
        themes_title = "强主线轮动板块（按轮动补涨策略观察）"
    else:
        themes_title = "主升进攻核心板块（按主升冠军策略出击）"

    # 生成今日行动清单（与五态大盘总闸门 + 周期策略路由 100% 统一口径）
    checklist: list[str] = []
    if is_defensive:
        checklist.append(
            f"【总开关与生效策略】今日为{regime_label}（上限 ≤{cap_pct:.0f}%），策略路由已锁死「{active_strat_name}」，严禁开新主升仓。"
        )
    else:
        checklist.append(
            f"【总开关与生效策略】今日为{regime_label}（上限 ≤{cap_pct:.0f}%），已激活「{active_strat_name}」，允许按规则择优执行。"
        )

    if wl.get("advice"):
        checklist.append(f"【AR1水位】{wl['advice']}")

    if core_themes:
        theme_str = "、".join(f"{t['industry']}({t['candidate_count']}只)" for t in core_themes)
        if is_defensive:
            checklist.append(f"【防守风向】相对抗跌板块：{theme_str}（仅作盘面风向观察，不作为开新仓依据）。")
        else:
            checklist.append(f"【板块主线】强势共振行业：{theme_str}。")
    else:
        checklist.append("【板块观察】当前无明显主线共振，耐心等待右侧信号。")

    router_picks = list((strategy_router or {}).get("picks") or [])
    if router_picks and not is_defensive:
        picks_str = "、".join(f"{p.get('name')}({p.get('code')})" for p in router_picks[:3])
        checklist.append(f"【生效策略命中】{active_strat_name} 今日命中前排：{picks_str}。")
    elif top_picks and not is_defensive:
        picks_str = "、".join(f"{p.get('name')}({str(p.get('code') or '').split('.')[0]})" for p in top_picks[:3])
        checklist.append(f"【精选关注】绿灯高催化池前排：{picks_str}。")
    elif is_defensive:
        checklist.append("【开仓禁令】防守/冰点期严禁追买任何主升候选股，已有持仓优先逢反弹降水位或反T降本。")

    # 融合 Phase 4 三腿配额到作战清单与 Markdown 简报
    alloc = (strategy_matrix or {}).get("allocation") if isinstance(strategy_matrix, Mapping) else None
    alloc_line = None
    if isinstance(alloc, Mapping):
        alloc_line = (
            f"进攻腿(主升) {alloc.get('offense_pct', 0):.0f}%｜"
            f"稳健腿(ETF) {alloc.get('defense_etf_pct', 0):.0f}%｜"
            f"增强腿(做T) {alloc.get('t_enhance_pct', 0):.0f}%｜"
            f"现金留存 {alloc.get('cash_pct', 0):.0f}%"
        )
        checklist.append(f"【三腿配额】{alloc_line}")

    # 生成一页式 Markdown 简报（适合前端展示与微信推送）
    turnover_str = f"{tf['forecast_trillion']}万亿" if tf.get("forecast_trillion") is not None else "—"
    actual_str = f"{actual_pct:.1f}%" if actual_pct is not None else "待同步"
    lines = [
        f"【AiStock 盘前主线简报 · {d_str}】",
        f"■ 市场定位：{regime_label}｜仓位上限 ≤{cap_pct:.0f}%｜{'允许开新主升' if allow_new else '严禁开新主升'}",
        f"■ 当前水位：{actual_str}（{wl.get('status') or 'unknown'}）",
        f"■ 核心盘面：上证AR1趋势={idx.get('confirmed_regime') or '—'}（连续{idx.get('streak_days') or 0}日）｜情绪={mood.get('mood_label') or '—'}（涨停{mood.get('limit_up_count', '—')}/跌停{mood.get('limit_down_count', '—')}）｜成交额={turnover_str}",
    ]
    if alloc_line:
        lines.append(f"■ 三腿配额：{alloc_line}")
    if agent_thesis:
        lines.append(f"■ AI 主线研判：{agent_thesis.strip()}")
    if overnight_notes:
        lines.append("■ 隔夜要点：" + "；".join(str(x).strip() for x in overnight_notes if str(x).strip()))
    lines.append("■ 今日作战清单：")
    for idx_i, item in enumerate(checklist, 1):
        lines.append(f"  {idx_i}. {item}")

    markdown_text = "\n".join(lines)

    brief = {
        "schema": "g3_premarket_brief/1",
        "date": d_str,
        "generated_at": now_bj.isoformat(timespec="seconds"),
        "source": source,
        "market_stance": {
            "regime": regime,
            "regime_label": regime_label,
            "tone": market_gate.get("tone") or "red",
            "allow_new_mainwave": allow_new,
            "target_cap_pct": cap_pct,
            "actual_pct": actual_pct,
            "waterline_status": wl.get("status"),
            "waterline_advice": wl.get("advice"),
        },
        "core_themes": core_themes,
        "themes_title": themes_title,
        "active_strategy_name": active_strat_name,
        "top_picks": top_picks,
        "overnight_notes": [str(x).strip() for x in (overnight_notes or []) if str(x).strip()],
        "agent_thesis": (agent_thesis or "").strip() or None,
        "action_checklist": checklist,
        "markdown_text": markdown_text,
    }

    store_dir = _brief_dir(base_dir)
    text = json.dumps(brief, ensure_ascii=False, indent=2)
    (store_dir / f"{ymd}.json").write_text(text, encoding="utf-8")
    (store_dir / "latest.json").write_text(text, encoding="utf-8")
    return brief


def load_latest_premarket_brief(
    trade_date: Optional[str] = None,
    *,
    base_dir: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    store_dir = base_dir or runtime_path("g3_premarket_brief")
    if not store_dir.exists():
        return None
    if trade_date:
        ymd = _norm_date(trade_date).replace("-", "")
        exact = _read_json_safe(store_dir / f"{ymd}.json")
        if exact:
            return exact
    return _read_json_safe(store_dir / "latest.json")


def push_premarket_brief_wechat(brief: Mapping[str, Any]) -> dict[str, Any]:
    """复用 t_notify 现有微信通道推送盘前简报。"""
    from services.trading.t_notify import send_wechat_notification

    title = f"AiStock 盘前主线简报 ({brief.get('date') or ''})"
    content = str(brief.get("markdown_text") or "")
    ok, channel, detail = send_wechat_notification(title=title, content=content)
    return {"ok": ok, "channel": channel, "detail": detail}
