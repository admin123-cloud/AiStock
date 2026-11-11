# -*- coding: utf-8 -*-
"""R50 L2 下单风控门（纯逻辑骨架，零 UI 零 formal 表写）。

任务书 task_briefs/20261111_l2_order_automation_r50.md 第 3 项：
入参（账号/代码/方向/价/量）→ 查 l2_risk_config（单笔上限/日亏熔断/
次数/时段/价格偏离/数据新鲜度）→ 全部通过才返回 fill 指令。

铁律：
  - 本模块绝不 import 任何 UI/自动化库（pywinauto/ctypes/send_keys 等），
    绝不发起任何窗口操作（单测 test_no_ui_imports 静态守卫）；
  - fail-closed：任何判定所需输入缺失/未知（账号未知、快照缺失、可卖
    未知、票池不可判……）一律拒绝，绝不放行"缺省即过"；
  - evaluate() 纯函数（无 I/O）；journal_decision() 显式落审计 jsonl
    （每笔判定全量 journal，l2_risk_config.global_breakers.audit）。

配置口径（runtime/t_tracker/l2_risk_config.json，schema l2_risk_config/1）：
  acct1_zeng_gf : 单笔 25000 元 + 底仓1/3取整百（min(25000/价, 底仓//3//100*100)）
  acct2_wang_gf : 单笔 300 股 / 6500 元，universe 仅 603881.SH
  global        : 价格偏离 0.5% / 同码每日 2 单 / 14:30 后禁开新仓 /
                  快照陈旧 120s 熔断 / 日亏熔断（账号级） / Owner kill_switch

Phase 2-4 才解锁填单/提交；本骨架输出 FillDirective 仅是"可以填"的判定，
不含任何执行动作。语义开放点已注明 PM_REVIEW，Phase 2 前过 PM。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

SCHEMA = "l2_risk_config/1"

# 交易时段（连续竞价；集合竞价 09:15-09:25 与尾盘不属自动单窗口，PM_REVIEW）
SESSIONS: tuple[tuple[dtime, dtime], ...] = (
    (dtime(9, 30, 0), dtime(11, 30, 0)),
    (dtime(13, 0, 0), dtime(15, 0, 0)),
)
NO_NEW_POSITION_AFTER = dtime(14, 30, 0)
FEED_STALE_HALT_MIN = 10          # 快照断流 >10min = abnormal_market_halt
SIDES = ("buy", "sell")


class GateConfigError(RuntimeError):
    """配置缺失/schema 不符——fail-closed，绝不在坏配置上放行任何单。"""


# ────────────────────────── 数据结构 ──────────────────────────

@dataclass(frozen=True)
class OrderRequest:
    account: str                 # 'acct1' / 'acct1_zeng_gf' / 'acct2' / 'acct2_wang_gf'
    code: str                    # '603881' 或 '603881.SH'（自动归一 6 位）
    side: str                    # 'buy' | 'sell'
    price: Any                   # Decimal|float|str → Decimal
    qty: int
    ts: datetime                 # 信号时刻（审计用；时段判定用 ctx.now）


@dataclass(frozen=True)
class MarketSnapshot:
    reference_price: Optional[Any] = None   # 偏离基准价（最新快照价）
    snapshot_ts: Optional[datetime] = None  # 快照时刻（新鲜度基准）
    halted: bool = False                    # 停牌
    limit_touched: bool = False             # 涨跌停触及
    feed_stale_min: Optional[int] = None    # 快照断流分钟数


@dataclass(frozen=True)
class DayState:
    day_pnl: Any = Decimal(0)          # 当日盈亏（熔断口径，负=亏，Decimal）
    orders_today: int = 0              # 本账户本码今日已下自动单数（双向合计）
    holdings_qty: int = 0              # 当前持仓股数（底仓 1/3 上限基数）
    sellable_qty: Optional[int] = None # 可卖股数（T+1 口径）；None=fail-closed 拒卖
    day_bought_qty: int = 0            # 今日已买入股数（T 平仓买判定）
    day_sold_qty: int = 0              # 今日已卖出股数


@dataclass(frozen=True)
class GateContext:
    now: datetime
    snapshot: Optional[MarketSnapshot] = None
    day: DayState = field(default_factory=DayState)
    kill_switch: bool = False          # Owner 微信"停止"指令已挂
    code_in_universe: Optional[bool] = None  # 票池判定（watchlist∪holdings）；None=不可判


@dataclass(frozen=True)
class Rejection:
    check: str
    reason: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FillDirective:
    action: str = "fill"               # 只声明"可填单"，不含任何执行语义
    account: str = ""
    code: str = ""
    side: str = ""
    price: str = ""
    qty: int = 0
    decided_at: str = ""
    checks_passed: tuple = ()
    config_schema: str = SCHEMA


@dataclass(frozen=True)
class GateDecision:
    approved: bool
    fill: Optional[FillDirective]
    rejections: tuple


# ────────────────────────── 配置加载 ──────────────────────────

def default_config_path() -> Path:
    try:
        from utils.paths import runtime_path
        return runtime_path("t_tracker") / "l2_risk_config.json"
    except Exception:  # pragma: no cover - 独立运行兜底
        return Path(r"F:\Aistock\data\data\runtime\t_tracker\l2_risk_config.json")


def load_config(path: Optional[Path] = None) -> dict:
    p = Path(path) if path else default_config_path()
    if not p.exists():
        raise GateConfigError(f"config_missing: {p}")
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateConfigError(f"config_unreadable: {exc}") from exc
    if cfg.get("schema") != SCHEMA:
        raise GateConfigError(
            f"config_schema_mismatch: {cfg.get('schema')!r} != {SCHEMA!r}")
    return cfg


def _account_aliases(cfg: dict) -> dict[str, str]:
    """'acct1' → 配置全键 'acct1_zeng_gf'（短名=全键首段 '_' 前）。"""
    aliases: dict[str, str] = {}
    for key in (cfg.get("accounts") or {}):
        aliases[str(key)] = str(key)
        aliases[str(key).split("_", 1)[0]] = str(key)
    return aliases


def _normalize_code(code: str) -> str:
    c = str(code or "").strip().upper()
    for suf in (".SH", ".SZ", ".BJ"):
        if c.endswith(suf):
            c = c[: -len(suf)]
    c = c.split(".")[0]
    if not (len(c) == 6 and c.isdigit()):
        raise ValueError(f"bad_code: {code!r}")
    return c


def _to_decimal(v: Any) -> Decimal:
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"bad_number: {v!r}") from exc


def _to_int(v: Any) -> int:
    """整数守卫：300.5/布尔等绝不静默截断。"""
    if isinstance(v, bool) or v is None:
        raise ValueError(f"bad_qty: {v!r}")
    if isinstance(v, int):
        return v
    d = _to_decimal(v)
    if d != d.to_integral_value():
        raise ValueError(f"bad_qty_not_integer: {v!r}")
    return int(d)


# ────────────────────────── 判定核心（纯函数） ──────────────────────────

def check_order(req: OrderRequest, ctx: GateContext, cfg: dict) -> GateDecision:
    """全量检查（不短路：收集全部拒因，审计需要完整画面）。"""
    if cfg.get("schema") != SCHEMA:
        return GateDecision(False, None, (Rejection(
            "config", f"config_schema_mismatch: {cfg.get('schema')!r}"),))

    gb = cfg.get("global_breakers") or {}
    rejections: list[Rejection] = []
    passed: list[str] = []

    def ok(name: str) -> None:
        passed.append(name)

    def rej(check: str, reason: str, **detail: Any) -> None:
        rejections.append(Rejection(check, reason, detail))

    # 1) kill_switch（Owner 微信"停止"→秒级停所有自动下单）
    if ctx.kill_switch:
        rej("kill_switch", "owner_kill_switch_engaged")
    else:
        ok("kill_switch")

    # 2) 账号已知 + 请求合法（fail-closed：解析失败直接拒绝）
    aliases = _account_aliases(cfg)
    acct_key = aliases.get(str(req.account or ""))
    side = str(req.side or "").lower()
    try:
        code = _normalize_code(req.code)
        price = _to_decimal(req.price)
        qty = _to_int(req.qty)
    except ValueError as exc:
        rej("request_valid", str(exc), account=req.account, side=req.side)
        acct_key = None
        code = price = qty = None  # type: ignore[assignment]
    request_ok = False
    if acct_key is None:
        rej("account_known", f"unknown_account: {req.account!r}")
    if side not in SIDES:
        rej("request_valid", f"bad_side: {req.side!r}")
    if code is not None:
        if qty is None or qty <= 0:
            rej("request_valid", f"bad_qty: {req.qty!r}")
        elif price is None or price <= 0:
            rej("request_valid", f"bad_price: {req.price!r}")
        elif side == "buy" and qty % 100 != 0:
            rej("request_valid", f"buy_qty_not_lot100: {qty}")
        elif side in SIDES:
            request_ok = True

    acct_cfg: dict = {}
    if acct_key is not None:
        acct_cfg = dict((cfg.get("accounts") or {}).get(acct_key) or {})
        ok("account_known")
    if code and side in SIDES and qty and qty > 0 and price and price > 0 \
            and not (side == "buy" and qty % 100):
        ok("request_valid")

    # 3) 票池 universe（配置内"仅 <6位码>"可判；其余账号由 ctx 代判，
    #    不可判 fail-closed 拒绝）
    if acct_cfg:
        universe = str(acct_cfg.get("universe") or "")
        config_verdict: Optional[bool] = None
        m = re.search(r"仅\s*[（(]?\s*(\d{6})", universe)
        if m:
            only_code = m.group(1)
            config_verdict = (code == only_code) if code else None
        verdict = ctx.code_in_universe
        if config_verdict is not None:
            verdict = config_verdict if verdict is None else (verdict and config_verdict)
        if verdict is True:
            ok("universe")
        else:
            rej("universe",
                "code_not_in_universe" if verdict is False
                else "universe_undeterminable_failclosed",
                code=code, universe=universe,
                ctx_code_in_universe=ctx.code_in_universe)

    # 4) 数据新鲜度 + 市场异常
    snap = ctx.snapshot
    if snap is None or snap.snapshot_ts is None:
        rej("data_freshness", "snapshot_missing_failclosed")
    else:
        age = (ctx.now - snap.snapshot_ts).total_seconds()
        limit = float(gb.get("stale_data_kill_sec", 120))
        if age < 0:
            rej("data_freshness", f"snapshot_from_future: {age:.0f}s")
        elif age > limit:
            rej("data_freshness", f"snapshot_stale: {age:.0f}s>{limit:.0f}s")
        else:
            ok("data_freshness")

    # 5) 价格偏离（|委托价-基准|/基准 > pct → 拒，双向防滑点外异常价）
    if snap is not None and snap.reference_price is not None:
        try:
            ref = _to_decimal(snap.reference_price)
        except ValueError:
            rej("price_deviation", "reference_price_invalid")
        else:
            if ref > 0 and price is not None and price > 0:
                dev = abs(price - ref) / ref
                pct = Decimal(str(gb.get("price_deviation_reject_pct", 0.5))) / 100
                if dev > pct:
                    rej("price_deviation",
                        f"price_deviation {float(dev * 100):.3f}%>"
                        f"{float(pct * 100):.1f}%",
                        price=str(price), reference=str(ref))
                else:
                    ok("price_deviation")
    else:
        rej("price_deviation", "reference_price_missing_failclosed")

    # 6) 市场异常（停牌/涨跌停触及/断流）
    halted_reasons = []
    if snap is not None:
        if snap.halted:
            halted_reasons.append("suspended")
        if snap.limit_touched:
            halted_reasons.append("limit_updown_touched")
        if snap.feed_stale_min is not None and snap.feed_stale_min > FEED_STALE_HALT_MIN:
            halted_reasons.append(f"feed_stale>{FEED_STALE_HALT_MIN}min")
    if halted_reasons:
        rej("market_halt", ",".join(halted_reasons))
    else:
        ok("market_halt")

    # 7) 单笔上限（股数上限 + 金额上限 + acct1 底仓 1/3 取整百规则）
    if acct_cfg and code and price is not None and qty:
        cap_share_cfg = acct_cfg.get("per_order_share_cap")
        cap_value = _to_decimal(acct_cfg.get("per_order_value_cap", 0) or 0)
        share_rule = str(acct_cfg.get("per_order_share_rule") or "")
        cap_share_dyn: Optional[int] = None
        if cap_share_cfg is not None:
            cap_share_dyn = int(cap_share_cfg)
        if "底仓1/3" in share_rule or "底仓 1/3" in share_rule:
            # min(金额上限/价, 底仓//3//100*100)，与 R47/R51 口径一致（4000→1300）
            by_value = int(cap_value // price) if price > 0 else 0
            by_base = (ctx.day.holdings_qty // 3 // 100) * 100
            rule_cap = min(by_value, by_base)
            cap_share_dyn = rule_cap if cap_share_dyn is None \
                else min(cap_share_dyn, rule_cap)
        notional = price * qty
        if cap_value > 0 and notional > cap_value:
            rej("per_order_cap", f"notional {notional} > {cap_value}",
                notional=str(notional), cap_value=str(cap_value))
        if cap_share_dyn is not None and qty > cap_share_dyn:
            rej("per_order_cap", f"qty {qty} > share_cap {cap_share_dyn}",
                share_cap=cap_share_dyn,
                base_holdings=ctx.day.holdings_qty if "底仓" in share_rule else None)
        if not any(r.check == "per_order_cap" for r in rejections):
            ok("per_order_cap")

    # 8) 可卖检查（T+1；sellable 未知 fail-closed）
    if side == "sell" and qty:
        sellable = ctx.day.sellable_qty
        if sellable is None:
            rej("sell_available", "sellable_unknown_failclosed")
        elif qty > sellable:
            rej("sell_available", f"qty {qty} > sellable {sellable}")
        else:
            ok("sell_available")
    else:
        ok("sell_available")

    # 9) 同码每日次数
    max_orders = int(gb.get("order_count_per_code_per_day", 2))
    if ctx.day.orders_today >= max_orders:
        rej("order_count",
            f"orders_today {ctx.day.orders_today} >= {max_orders}")
    else:
        ok("order_count")

    # 10) 日亏熔断（day_pnl <= -kill → 拒；等号=触发）
    kill = _to_decimal(acct_cfg.get("daily_loss_kill", 0) or 0) if acct_cfg else Decimal(0)
    if kill > 0:
        day_pnl = _to_decimal(ctx.day.day_pnl)
        if day_pnl <= -kill:
            rej("daily_loss_kill",
                f"day_pnl {day_pnl} <= -{kill}")
        else:
            ok("daily_loss_kill")
    else:
        rej("daily_loss_kill", "daily_loss_kill_not_configured_failclosed")

    # 11) 时段（连续竞价窗口）+ 14:30 后禁开新仓（T 平仓买豁免）
    t = ctx.now.time()
    in_session = any(lo <= t <= hi for lo, hi in SESSIONS)
    if not in_session:
        rej("time_window", f"outside_trading_session: {t}")
    else:
        ok("time_window")
    if side == "buy" and t > NO_NEW_POSITION_AFTER:
        t_close_capacity = ctx.day.day_sold_qty - ctx.day.day_bought_qty
        if qty is not None and qty > 0 and qty <= t_close_capacity:
            ok("no_new_position_after")   # 平今卖腿的 T 买，豁免
        else:
            rej("no_new_position_after",
                f"buy_after_1430_new_position "
                f"(t_close_capacity={t_close_capacity})")
    else:
        ok("no_new_position_after")

    if rejections:
        return GateDecision(False, None, tuple(rejections))
    fill = FillDirective(
        account=acct_key or str(req.account), code=code or "",
        side=side, price=str(price), qty=int(qty or 0),
        decided_at=ctx.now.isoformat(sep=" ", timespec="seconds"),
        checks_passed=tuple(passed))
    return GateDecision(True, fill, ())


# ────────────────────────── 审计 journal（显式调用才写） ──────────────────────────

def journal_decision(decision: GateDecision, req: OrderRequest, ctx: GateContext,
                     journal_path: Path) -> Path:
    """每笔判定全量 journal（通过/拒绝都记）。原子追加，返回落盘路径。"""
    journal_path = Path(journal_path)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": ctx.now.isoformat(sep=" ", timespec="milliseconds"),
        "schema": SCHEMA,
        "request": {"account": req.account, "code": req.code, "side": req.side,
                    "price": str(req.price), "qty": req.qty,
                    "signal_ts": req.ts.isoformat(sep=" ", timespec="seconds")},
        "kill_switch": ctx.kill_switch,
        "approved": decision.approved,
        "fill": decision.fill.__dict__ if decision.fill else None,
        "rejections": [r.__dict__ for r in decision.rejections],
    }
    with journal_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return journal_path
