# -*- coding: utf-8 -*-
"""G3 Phase 3C：安全半自动风控预检与预填草案生成器（Human-in-the-Loop）。

职责：
1. 在用户手工/信号准备下单前，执行逐项确定性风控预检（复用 runtime/t_tracker/l2_risk_config.json 与 Phase 1A 大盘总闸门）：
   - 整百股数与正价格校验；
   - 大盘防守期禁开新主升仓校验（对 base_rebalance 买入强拦截）；
   - 单笔金额/股数上限校验（含底仓 1/3 上限对 t_trade 生效）；
   - T+1 可卖股数校验（卖出不得超过可用股数）；
   - 现价偏离 0.5% 双向校验；
   - 交易时段与 14:30 禁开新仓校验（盘后预演模式显式标注为 rehearsal，不伪装盘中放行）。
2. 全项通过（或仅盘后预演提示）后，生成带唯一 draft_id 的合规预填单落盘到
   runtime/g3_prefill_drafts/YYYYMMDD.jsonl，供人工一键核对后在同花顺确认执行。
3. 铁律：本模块纯逻辑无 UI 自动化，绝不自动点击下单！
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from utils.common.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))
DEFAULT_MAX_DEVIATION_PCT = 0.5
DEFAULT_MAX_ORDER_AMOUNT = 25000.0
NO_NEW_POS_AFTER = dtime(14, 30, 0)


def _draft_store_dir(base_dir: Optional[Path] = None) -> Path:
    p = base_dir or runtime_path("g3_prefill_drafts")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _load_l2_risk_config(tracker_dir: Optional[Path] = None) -> dict[str, Any]:
    p = (tracker_dir or runtime_path("t_tracker")) / "l2_risk_config.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _is_in_trading_session(t: dtime) -> bool:
    return (dtime(9, 30, 0) <= t <= dtime(11, 30, 0)) or (dtime(13, 0, 0) <= t <= dtime(15, 0, 0))


def evaluate_and_build_prefill_draft(
    *,
    code: str,
    name: str,
    side: str,
    price: float,
    shares: int,
    order_type: str = "t_trade",
    account_id: str = "acct1",
    live_price: Optional[float] = None,
    total_shares: int = 0,
    available_shares: int = 0,
    available_cash: Optional[float] = None,
    market_gate: Optional[Mapping[str, Any]] = None,
    now_bj: Optional[datetime] = None,
    tracker_dir: Optional[Path] = None,
    draft_dir: Optional[Path] = None,
    persist: bool = True,
) -> dict[str, Any]:
    """执行 L2 风控预检并生成半自动合规预填单草案（零自动下单）。"""
    now = now_bj or datetime.now(BJ_TZ)
    code6 = str(code or "").split(".")[0].strip().zfill(6)
    side_u = str(side or "").upper().strip()
    otype = str(order_type or "t_trade").strip().lower()
    if otype not in ("t_trade", "base_rebalance"):
        otype = "t_trade"

    cfg = _load_l2_risk_config(tracker_dir)
    gb = cfg.get("global_breakers") if isinstance(cfg.get("global_breakers"), Mapping) else {}
    max_dev_pct = float(gb.get("price_deviation_pct") or DEFAULT_MAX_DEVIATION_PCT)
    if max_dev_pct < 0.1:
        # 兼容小数口径如 0.005 -> 0.5%
        max_dev_pct = round(max_dev_pct * 100.0, 3)

    # 解析账号级单笔金额上限
    max_order_amt = DEFAULT_MAX_ORDER_AMOUNT
    accts = cfg.get("accounts") if isinstance(cfg.get("accounts"), Mapping) else {}
    for k, v in accts.items():
        if isinstance(v, Mapping) and (k == account_id or k.startswith(account_id)):
            if v.get("max_order_amount"):
                max_order_amt = float(v["max_order_amount"])
            break

    order_amount = round(float(price) * int(shares), 2) if (price > 0 and shares > 0) else 0.0
    checks: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []

    def _add(name_key: str, ok: bool, detail: str, *, hard: bool = True) -> None:
        checks.append({"check": name_key, "ok": ok, "hard": hard, "detail": detail})
        if not ok:
            if hard:
                blockers.append(detail)
            else:
                warnings.append(detail)

    # 1. 基础合法性
    _add(
        "basic_params",
        side_u in ("BUY", "SELL") and price > 0 and shares >= 100 and shares % 100 == 0,
        "委托方向、正价格与整百股数校验通过"
        if (side_u in ("BUY", "SELL") and price > 0 and shares >= 100 and shares % 100 == 0)
        else "参数无效：需为 BUY/SELL、价格 > 0 且股数为 100 整数倍",
    )

    # 2. 大盘总闸门联动（防守期严禁 base_rebalance 买入/开新仓）
    if isinstance(market_gate, Mapping):
        allow_new = bool(market_gate.get("allow_new_mainwave", True))
        regime_label = str(market_gate.get("regime_label") or market_gate.get("regime") or "")
        if side_u == "BUY" and otype == "base_rebalance" and not allow_new:
            _add(
                "market_regime_gate",
                False,
                f"大盘总闸门拦截：当前【{regime_label}】严禁开新仓或加底仓（base_rebalance BUY）",
                hard=True,
            )
        elif side_u == "BUY" and otype == "t_trade" and not allow_new:
            _add(
                "market_regime_gate",
                False,
                f"大盘【{regime_label}】提示：防守期正T低吸须严格确认强支撑与硬止损未破",
                hard=False,
            )
        else:
            _add("market_regime_gate", True, f"大盘总闸门通过（当前：{regime_label or '正常'}）")

    # 3. 单笔金额与底仓 1/3 上限
    _add(
        "max_order_amount",
        order_amount <= max_order_amt,
        f"单笔金额 ¥{order_amount:,.2f} ≤ 上限 ¥{max_order_amt:,.0f}"
        if order_amount <= max_order_amt
        else f"超出单笔金额上限：¥{order_amount:,.2f} > ¥{max_order_amt:,.0f}",
    )
    if otype == "t_trade" and total_shares > 0:
        t_cap = (total_shares // 3 // 100) * 100
        if t_cap >= 100:
            _add(
                "t_lot_one_third",
                shares <= t_cap,
                f"做T股数 {shares} ≤ 底仓1/3上限 {t_cap} 股"
                if shares <= t_cap
                else f"做T超量：{shares} 股 > 底仓1/3上限 {t_cap} 股（如需调底仓请改选「水位调仓单」）",
            )

    # 4. 资金 / T+1 可卖股数
    if side_u == "SELL":
        _add(
            "t_plus_1_sellable",
            available_shares >= shares,
            f"T+1 可卖校验通过（可用 {available_shares} 股 ≥ 委托 {shares} 股）"
            if available_shares >= shares
            else f"T+1 可卖不足：可用 {available_shares} 股 < 委托 {shares} 股",
        )
    elif side_u == "BUY" and available_cash is not None:
        _add(
            "cash_sufficiency",
            available_cash >= order_amount,
            f"可用现金校验通过（¥{available_cash:,.2f} ≥ ¥{order_amount:,.2f}）"
            if available_cash >= order_amount
            else f"可用现金不足：¥{available_cash:,.2f} < 预计金额 ¥{order_amount:,.2f}",
        )

    # 5. 现价偏离校验（0.5%）
    if live_price is not None and live_price > 0 and price > 0:
        dev_pct = abs(price - live_price) / live_price * 100.0
        _add(
            "price_deviation",
            dev_pct <= max_dev_pct,
            f"委托价偏离现价 {dev_pct:.2f}% ≤ {max_dev_pct:.2f}%"
            if dev_pct <= max_dev_pct
            else f"价格偏离超限：委托 {price:.2f} vs 现价 {live_price:.2f}（偏离 {dev_pct:.2f}% > {max_dev_pct:.2f}%）",
        )
    else:
        _add("price_deviation", False, "缺少实时参考现价，无法校验 0.5% 价格偏离（人工核对盘口）", hard=False)

    # 6. 交易时段与 14:30 禁开新仓
    cur_t = now.time()
    in_session = _is_in_trading_session(cur_t)
    if not in_session:
        _add("trading_session", False, f"当前非连续竞价时段（{now.strftime('%H:%M')}），本次为盘前/盘后预演校验", hard=False)
    elif side_u == "BUY" and cur_t >= NO_NEW_POS_AFTER:
        _add("no_new_after_1430", False, "14:30 后禁开新仓/新买腿（L2 硬风控拦截）", hard=True)
    else:
        _add("trading_session", True, f"处于连续竞价合规时段（{now.strftime('%H:%M')}）")

    approved = len(blockers) == 0
    draft_id = f"PF{now.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
    result = {
        "schema": "g3_l2_prefill_draft/1",
        "draft_id": draft_id,
        "created_at": now.isoformat(timespec="seconds"),
        "approved": approved,
        "execution_mode": "human_confirm_only",
        "in_trading_session": in_session,
        "order": {
            "account_id": account_id,
            "code": code6,
            "name": name or code6,
            "side": side_u,
            "order_type": otype,
            "order_type_label": "水位调仓单" if otype == "base_rebalance" else "做T滚动单",
            "price": round(float(price), 3),
            "shares": int(shares),
            "amount": order_amount,
        },
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "summary": (
            f"L2 风控预检通过（放行码 {draft_id}），请人工核对后在同花顺确认下单"
            if approved
            else f"L2 风控拦截（{len(blockers)} 项硬阻断）：{blockers[0]}"
        ),
    }

    if persist:
        p = _draft_store_dir(draft_dir) / f"{now.strftime('%Y%m%d')}.jsonl"
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result, ensure_ascii=False) + "\n")

    return result
