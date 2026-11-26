# -*- coding: utf-8 -*-
"""acct2 独立半托管交易引擎（1 周 Dry-run 试运行版）。

设计原则：
  1. 完全隔离：零 UI 导入、零同花顺窗口操作、零 ClickHouse 写入、零触碰 acct1。
  2. 严格遵守 A 股规则：T+1 可卖校验、100 股整手、涨跌停拒单、午休闸、14:30 后禁开新仓、
     真实佣金（万 2.5 最低 5 元）与印花税（个股卖出万 5，ETF 免印花税）仿真。
  3. 硬风控红线：基于历史高点净值（High-Water Mark）的三级动态回撤熔断
     （-6% 黄线降档 / -10% 橙线禁开新仓 / -12% 红线自动 kill_switch 硬熔断），
     以及单日亏损 -3% 熔断、单票仓位上限、高价股/ST 过滤。
  4. 策略框架：市场宽度水位控制 + 存量重仓风控重构 + 核心 ETF 动量/防御轮动。
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = "acct2_managed/1"

# 连续竞价交易时段
MORNING_SESSION = (dtime(9, 30, 0), dtime(11, 30, 0))
AFTERNOON_SESSION = (dtime(13, 0, 0), dtime(15, 0, 0))
NO_NEW_BUY_AFTER = dtime(14, 30, 0)

# 默认高流动性 ETF 池（代码 -> 元信息）
DEFAULT_ETF_UNIVERSE: dict[str, dict[str, str]] = {
    "510300.SH": {"name": "沪深300ETF", "bucket": "broad", "asset_type": "etf"},
    "510500.SH": {"name": "中证500ETF", "bucket": "broad", "asset_type": "etf"},
    "588000.SH": {"name": "科创50ETF", "bucket": "growth", "asset_type": "etf"},
    "159915.SZ": {"name": "创业板ETF", "bucket": "growth", "asset_type": "etf"},
    "510880.SH": {"name": "红利ETF", "bucket": "defensive", "asset_type": "etf"},
    "518880.SH": {"name": "黄金ETF", "bucket": "defensive", "asset_type": "etf"},
    "511010.SH": {"name": "国债ETF", "bucket": "defensive", "asset_type": "etf"},
    "512480.SH": {"name": "半导体ETF", "bucket": "sector", "asset_type": "etf"},
    "512880.SH": {"name": "证券ETF", "bucket": "sector", "asset_type": "etf"},
    "512010.SH": {"name": "医药ETF", "bucket": "sector", "asset_type": "etf"},
}


def normalize_code(code: str) -> str:
    """将 6 位代码统一规范为带后缀格式（如 603881 -> 603881.SH）。"""
    raw = str(code or "").strip().upper()
    if "." in raw:
        return raw
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) != 6:
        return raw
    if digits.startswith(("5", "6", "9")):
        return f"{digits}.SH"
    if digits.startswith(("0", "1", "2", "3")):
        return f"{digits}.SZ"
    return f"{digits}.BJ"


def infer_asset_type(code: str) -> str:
    """根据代码前缀粗判资产类型（etf / stock）。"""
    norm = normalize_code(code)
    prefix = norm.split(".")[0]
    if prefix.startswith(("51", "56", "58", "15", "16")):
        return "etf"
    return "stock"


def price_limit_ratio(code: str) -> float:
    """返回涨跌停幅度（科创/创业板 20%，主板/常规 ETF 10%）。"""
    norm = normalize_code(code)
    digits = norm.split(".")[0]
    if digits.startswith(("300", "301", "688", "588", "159915")):
        return 0.20
    if norm.endswith(".BJ"):
        return 0.30
    return 0.10


def round_price(price: float, asset_type: str) -> float:
    """ETF 按 3 位小数、股票按 2 位小数舍入报价。"""
    digits = 3 if asset_type == "etf" else 2
    return round(float(price), digits)


def calc_trade_fees(
    side: str,
    amount: float,
    asset_type: str,
    commission_rate: float = 0.00025,
    min_commission: float = 5.0,
    stamp_tax_rate: float = 0.0005,
) -> dict[str, float]:
    """计算 A 股交易费用：佣金万 2.5（最低 5 元），股票卖出收万 5 印花税，ETF 免印花税。"""
    amt = max(0.0, float(amount))
    commission = max(min_commission, round(amt * commission_rate, 2)) if amt > 0 else 0.0
    stamp_tax = 0.0
    if side == "sell" and asset_type != "etf" and amt > 0:
        stamp_tax = round(amt * stamp_tax_rate, 2)
    total = round(commission + stamp_tax, 2)
    return {
        "commission": round(commission, 2),
        "stamp_tax": round(stamp_tax, 2),
        "total_fee": total,
    }


def is_in_trading_session(now_time: dtime) -> bool:
    """判断是否处于 A 股连续竞价时段（排除午休 11:30-13:00 及非交易时间）。"""
    in_am = MORNING_SESSION[0] <= now_time <= MORNING_SESSION[1]
    in_pm = AFTERNOON_SESSION[0] <= now_time <= AFTERNOON_SESSION[1]
    return in_am or in_pm


# ────────────────────────── 数据模型 ──────────────────────────


@dataclass
class Acct2ManagedConfig:
    schema: str = SCHEMA_VERSION
    account: str = "acct2"
    dry_run: bool = True
    dd_yellow_pct: float = 0.06
    dd_orange_pct: float = 0.10
    dd_red_pct: float = 0.12
    daily_loss_kill_pct: float = 0.03
    max_positions: int = 3
    max_single_etf_weight: float = 0.50
    max_single_stock_weight: float = 0.35
    min_order_value: float = 2500.0
    max_stock_price: float = 40.0
    stock_stop_loss_pct: float = 0.06
    etf_stop_loss_pct: float = 0.05
    slippage_bps: float = 10.0  # 默认双边滑点仿真 0.10%
    commission_rate: float = 0.00025
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.0005
    # 接管存量持仓的专项硬防守参数（按代码覆盖）
    legacy_overrides: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {
            "603881.SH": {
                "hard_defense_price": 17.50,
                "reduce_lot_shares": 300,
                "rebound_gain_pct": 2.0,
                "rebound_vwap_dev_pct": 1.5,
                "target_max_weight": 0.30,
            }
        }
    )
    etf_universe: dict[str, dict[str, str]] = field(
        default_factory=lambda: dict(DEFAULT_ETF_UNIVERSE)
    )


@dataclass
class Position:
    code: str
    name: str
    asset_type: str  # "etf" | "stock"
    qty: int
    available_qty: int
    avg_cost: float  # 接管口径成本价（用于接管后止损）
    last_price: float
    legacy_cost_price: Optional[float] = None  # 券商原始历史成本（仅记录）
    entry_date: str = ""

    @property
    def market_value(self) -> float:
        return round(self.qty * self.last_price, 2)


@dataclass
class MarketBar:
    date: str
    open: float
    close: float
    high: float
    low: float
    volume: float


@dataclass
class LiveQuote:
    code: str
    name: str
    price: float
    prev_close: float
    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    vwap: Optional[float] = None
    ts: str = ""
    halted: bool = False

    @property
    def pct_change(self) -> float:
        if self.prev_close <= 0:
            return 0.0
        return (self.price - self.prev_close) / self.prev_close * 100.0

    def is_limit_up(self) -> bool:
        if self.prev_close <= 0:
            return False
        limit = round(self.prev_close * (1.0 + price_limit_ratio(self.code)), 3)
        return self.price >= limit - 0.002

    def is_limit_down(self) -> bool:
        if self.prev_close <= 0:
            return False
        limit = round(self.prev_close * (1.0 - price_limit_ratio(self.code)), 3)
        return self.price <= limit + 0.002


@dataclass
class MarketBreadth:
    date: str
    above_ma20_pct: float
    level: str = "unknown"  # weak / neutral / strong
    stale: bool = False


@dataclass
class OrderIntent:
    code: str
    name: str
    asset_type: str
    side: str  # "buy" | "sell"
    qty: int
    limit_price: float
    reason: str
    priority: int = 10  # 数字越小优先级越高（风控减仓优先于新开仓）


@dataclass
class GateResult:
    approved: bool
    reason: str
    risk_tier: str  # normal / yellow / orange / red / kill_switch
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Acct2State:
    schema: str = SCHEMA_VERSION
    account: str = "acct2"
    initialized_at: str = ""
    base_date: str = ""
    base_assets: float = 0.0
    hwm_assets: float = 0.0
    prev_eod_assets: float = 0.0
    cash: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    kill_switch: bool = False
    kill_switch_reason: str = ""
    orders_today: dict[str, int] = field(default_factory=dict)
    last_trade_date: str = ""
    # 对比基准：接管日原样不动（buy & hold）
    benchmark_cash: float = 0.0
    benchmark_holdings: dict[str, int] = field(default_factory=dict)
    benchmark_hs300_base: Optional[float] = None
    total_fees_paid: float = 0.0

    @property
    def market_value(self) -> float:
        return round(sum(p.market_value for p in self.positions.values()), 2)

    @property
    def total_assets(self) -> float:
        return round(self.cash + self.market_value, 2)

    @property
    def nav(self) -> float:
        if self.base_assets <= 0:
            return 1.0
        return round(self.total_assets / self.base_assets, 6)

    @property
    def drawdown_from_hwm(self) -> float:
        """返回相对历史最高净值的回撤比例（0 或正数，如 0.06 表示回撤 6%）。"""
        peak = max(self.hwm_assets, self.base_assets)
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - self.total_assets) / peak)

    @property
    def daily_pnl_pct(self) -> float:
        base = self.prev_eod_assets if self.prev_eod_assets > 0 else self.base_assets
        if base <= 0:
            return 0.0
        return (self.total_assets - base) / base

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["total_assets"] = self.total_assets
        d["market_value"] = self.market_value
        d["nav"] = self.nav
        d["drawdown_from_hwm"] = round(self.drawdown_from_hwm, 6)
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Acct2State":
        pos_map: dict[str, Position] = {}
        for code, p in (raw.get("positions") or {}).items():
            pos_map[normalize_code(code)] = Position(
                code=normalize_code(p["code"]),
                name=str(p.get("name") or ""),
                asset_type=str(p.get("asset_type") or infer_asset_type(p["code"])),
                qty=int(p["qty"]),
                available_qty=int(p["available_qty"]),
                avg_cost=float(p["avg_cost"]),
                last_price=float(p["last_price"]),
                legacy_cost_price=p.get("legacy_cost_price"),
                entry_date=str(p.get("entry_date") or ""),
            )
        return cls(
            schema=str(raw.get("schema") or SCHEMA_VERSION),
            account=str(raw.get("account") or "acct2"),
            initialized_at=str(raw.get("initialized_at") or ""),
            base_date=str(raw.get("base_date") or ""),
            base_assets=float(raw.get("base_assets") or 0.0),
            hwm_assets=float(raw.get("hwm_assets") or 0.0),
            prev_eod_assets=float(raw.get("prev_eod_assets") or 0.0),
            cash=float(raw.get("cash") or 0.0),
            positions=pos_map,
            kill_switch=bool(raw.get("kill_switch") or False),
            kill_switch_reason=str(raw.get("kill_switch_reason") or ""),
            orders_today={str(k): int(v) for k, v in (raw.get("orders_today") or {}).items()},
            last_trade_date=str(raw.get("last_trade_date") or ""),
            benchmark_cash=float(raw.get("benchmark_cash") or 0.0),
            benchmark_holdings={
                normalize_code(k): int(v)
                for k, v in (raw.get("benchmark_holdings") or {}).items()
            },
            benchmark_hs300_base=raw.get("benchmark_hs300_base"),
            total_fees_paid=float(raw.get("total_fees_paid") or 0.0),
        )


# ────────────────────────── 风控门与水位计算 ──────────────────────────


def evaluate_risk_tier(state: Acct2State, cfg: Acct2ManagedConfig) -> tuple[str, float]:
    """根据 HWM 动态回撤判定风控档位及对应的总仓位上限覆盖值。"""
    if state.kill_switch:
        return "kill_switch", 0.0
    dd = state.drawdown_from_hwm
    if dd >= cfg.dd_red_pct:
        return "red", 0.0
    if dd >= cfg.dd_orange_pct:
        return "orange", 0.0
    if dd >= cfg.dd_yellow_pct:
        return "yellow", 0.50
    return "normal", 1.00


def target_position_cap(
    breadth: MarketBreadth, state: Acct2State, cfg: Acct2ManagedConfig
) -> tuple[float, str]:
    """结合市场宽度水位 + 三级回撤档位，返回最终允许的总仓位上限。"""
    tier, tier_cap = evaluate_risk_tier(state, cfg)
    if tier in ("kill_switch", "red", "orange"):
        return 0.0, tier

    if breadth.above_ma20_pct < 25.0:
        breadth_cap = 0.30
    elif breadth.above_ma20_pct < 55.0:
        breadth_cap = 0.60
    else:
        breadth_cap = 0.85

    return min(breadth_cap, tier_cap), tier


def check_order_gate(
    intent: OrderIntent,
    quote: Optional[LiveQuote],
    state: Acct2State,
    cfg: Acct2ManagedConfig,
    now_dt: datetime,
    bypass_session_check: bool = False,
) -> GateResult:
    """独立风控门（fail-closed）：所有买卖指令必须通过才允许进入纸面/预填单。"""
    tier, _ = evaluate_risk_tier(state, cfg)

    # 1. 红线或人工 kill_switch：彻底停机
    if tier in ("kill_switch", "red"):
        return GateResult(
            approved=False,
            reason="hard_drawdown_or_kill_switch",
            risk_tier=tier,
            detail={"drawdown": round(state.drawdown_from_hwm, 4)},
        )

    # 2. 交易时段与午休闸
    if not bypass_session_check:
        t = now_dt.time()
        if not is_in_trading_session(t):
            return GateResult(False, "outside_trading_session", tier, {"time": t.isoformat()})
        if intent.side == "buy" and t >= NO_NEW_BUY_AFTER:
            return GateResult(False, "no_new_buy_after_1430", tier, {"time": t.isoformat()})

    # 3. 实时行情与涨跌停/停牌校验
    if quote is None or quote.price <= 0:
        return GateResult(False, "missing_quote", tier)
    if quote.halted:
        return GateResult(False, "security_halted", tier)
    if intent.side == "buy" and quote.is_limit_up():
        return GateResult(False, "limit_up_block_buy", tier)
    if intent.side == "sell" and quote.is_limit_down():
        return GateResult(False, "limit_down_block_sell", tier)

    # 4. 价格偏离校验（> 1.5% 拒绝异常报价）
    dev = abs(intent.limit_price - quote.price) / quote.price
    if dev > 0.015:
        return GateResult(False, "price_deviation_too_large", tier, {"dev": round(dev, 4)})

    # 5. 同码每日次数上限（2 笔）
    norm_code = normalize_code(intent.code)
    if state.orders_today.get(norm_code, 0) >= 2:
        return GateResult(False, "daily_order_count_exceeded", tier)

    # 6. 买入专属门禁
    if intent.side == "buy":
        if tier == "orange":
            return GateResult(False, "orange_drawdown_no_new_buy", tier)
        if tier == "yellow" and intent.asset_type == "stock":
            return GateResult(False, "yellow_drawdown_block_stock_buy", tier)
        if state.daily_pnl_pct <= -cfg.daily_loss_kill_pct:
            return GateResult(
                False,
                "daily_loss_kill_no_new_buy",
                tier,
                {"daily_pnl_pct": round(state.daily_pnl_pct, 4)},
            )
        if intent.qty <= 0 or intent.qty % 100 != 0:
            return GateResult(False, "buy_qty_must_be_lot_100", tier, {"qty": intent.qty})
        if intent.asset_type == "stock" and intent.limit_price > cfg.max_stock_price:
            return GateResult(False, "stock_price_above_cap", tier)
        if "ST" in intent.name.upper():
            return GateResult(False, "st_stock_forbidden", tier)

        order_val = intent.qty * intent.limit_price
        fees = calc_trade_fees("buy", order_val, intent.asset_type, cfg.commission_rate, cfg.min_commission, cfg.stamp_tax_rate)
        if order_val < cfg.min_order_value:
            return GateResult(False, "below_min_order_value", tier, {"order_val": round(order_val, 2)})
        if order_val + fees["total_fee"] > state.cash + 1e-6:
            return GateResult(False, "insufficient_cash", tier, {"cash": state.cash, "need": round(order_val + fees["total_fee"], 2)})

        # 持股数上限
        if norm_code not in state.positions and len(state.positions) >= cfg.max_positions:
            return GateResult(False, "max_positions_reached", tier)

        # 单票仓位上限
        total_assets = max(state.total_assets, 1.0)
        cur_mv = state.positions[norm_code].market_value if norm_code in state.positions else 0.0
        new_weight = (cur_mv + order_val) / total_assets
        max_w = cfg.max_single_etf_weight if intent.asset_type == "etf" else cfg.max_single_stock_weight
        if new_weight > max_w + 1e-6:
            return GateResult(
                False,
                "single_position_weight_exceeded",
                tier,
                {"new_weight": round(new_weight, 4), "max_weight": max_w},
            )

    # 7. 卖出专属门禁（T+1 严格校验）
    elif intent.side == "sell":
        pos = state.positions.get(norm_code)
        if pos is None or pos.qty <= 0:
            return GateResult(False, "no_position_to_sell", tier)
        if intent.qty <= 0 or intent.qty > pos.available_qty:
            return GateResult(
                False,
                "insufficient_t1_available_qty",
                tier,
                {"req": intent.qty, "available": pos.available_qty},
            )
        # 卖出需为 100 股整手，除非是清仓剩余零股
        if intent.qty % 100 != 0 and intent.qty != pos.available_qty:
            return GateResult(False, "sell_qty_must_be_lot_100_or_full_odd_lot", tier)
    else:
        return GateResult(False, "invalid_side", tier)

    return GateResult(True, "approved", tier)


# ────────────────────────── 策略决策引擎 ──────────────────────────


def calc_etf_metrics(bars: list[MarketBar]) -> Optional[dict[str, float]]:
    """基于前复权日线计算 MA5/MA20、20日动量与距 MA5 偏离。"""
    if len(bars) < 21:
        return None
    closes = [b.close for b in bars]
    last_close = closes[-1]
    ma5 = sum(closes[-5:]) / 5.0
    ma20 = sum(closes[-20:]) / 20.0
    mom20 = (last_close / closes[-21] - 1.0) * 100.0 if closes[-21] > 0 else 0.0
    mom5 = (last_close / closes[-6] - 1.0) * 100.0 if closes[-6] > 0 else 0.0
    dev_ma5 = (last_close / ma5 - 1.0) * 100.0 if ma5 > 0 else 0.0
    return {
        "close": round(last_close, 4),
        "ma5": round(ma5, 4),
        "ma20": round(ma20, 4),
        "mom20_pct": round(mom20, 2),
        "mom5_pct": round(mom5, 2),
        "dev_ma5_pct": round(dev_ma5, 2),
    }


def generate_strategy_intents(
    state: Acct2State,
    cfg: Acct2ManagedConfig,
    breadth: MarketBreadth,
    quotes: dict[str, LiveQuote],
    kline_map: dict[str, list[MarketBar]],
) -> list[OrderIntent]:
    """生成当日/盘中调仓指令列表（先卖后买，按优先级排序）。"""
    intents: list[OrderIntent] = []
    pos_cap, tier = target_position_cap(breadth, state, cfg)

    # 红线熔断直接返回空（交由外部置位 kill_switch）
    if tier in ("kill_switch", "red"):
        return intents

    total_assets = max(state.total_assets, 1.0)

    # ── 第一步：持仓风控与存量重构（卖出优先） ──
    for code, pos in list(state.positions.items()):
        q = quotes.get(code)
        if q is None or q.price <= 0 or pos.available_qty <= 0:
            continue
        cur_price = q.price
        weight = (pos.qty * cur_price) / total_assets
        override = cfg.legacy_overrides.get(code)

        # 1A. 存量特判（如 603881.SH）：破位硬防守 or 反抽分批降仓
        if override:
            hard_defense = float(override.get("hard_defense_price", 0.0))
            reduce_lot = min(int(override.get("reduce_lot_shares", 300)), pos.available_qty)
            reduce_lot = (reduce_lot // 100) * 100
            target_max_w = float(override.get("target_max_weight", cfg.max_single_stock_weight))

            if hard_defense > 0 and cur_price <= hard_defense and reduce_lot >= 100:
                intents.append(
                    OrderIntent(
                        code=code,
                        name=pos.name,
                        asset_type=pos.asset_type,
                        side="sell",
                        qty=reduce_lot,
                        limit_price=round_price(cur_price, pos.asset_type),
                        reason=f"破位硬防守减仓（现价 {cur_price:.2f} <= {hard_defense:.2f}）",
                        priority=1,
                    )
                )
                continue

            # 超重单票在反抽窗口分批降暴露
            if weight > target_max_w and reduce_lot >= 100:
                bars = kline_map.get(code) or []
                ma5 = (sum(b.close for b in bars[-5:]) / 5.0) if len(bars) >= 5 else 0.0
                rebound_gain = float(override.get("rebound_gain_pct", 2.0))
                rebound_vwap_dev = float(override.get("rebound_vwap_dev_pct", 1.5))
                vwap_dev_pct = (
                    (cur_price - q.vwap) / q.vwap * 100.0 if (q.vwap and q.vwap > 0) else 0.0
                )
                hit_ma5 = ma5 > 0 and cur_price >= ma5 * 0.995
                if q.pct_change >= rebound_gain or vwap_dev_pct >= rebound_vwap_dev or hit_ma5:
                    intents.append(
                        OrderIntent(
                            code=code,
                            name=pos.name,
                            asset_type=pos.asset_type,
                            side="sell",
                            qty=reduce_lot,
                            limit_price=round_price(cur_price, pos.asset_type),
                            reason=(
                                f"单票超重({weight*100:.1f}%>{target_max_w*100:.0f}%)逢反抽降仓"
                                f"（涨幅{q.pct_change:+.2f}% / MA5={ma5:.2f}）"
                            ),
                            priority=2,
                        )
                    )
                    continue

        # 1B. 常规刚性止损（相对接管/建仓成本）
        stop_pct = cfg.etf_stop_loss_pct if pos.asset_type == "etf" else cfg.stock_stop_loss_pct
        if pos.avg_cost > 0 and (cur_price - pos.avg_cost) / pos.avg_cost <= -stop_pct:
            sell_qty = pos.available_qty
            intents.append(
                OrderIntent(
                    code=code,
                    name=pos.name,
                    asset_type=pos.asset_type,
                    side="sell",
                    qty=sell_qty,
                    limit_price=round_price(cur_price, pos.asset_type),
                    reason=f"触及刚性止损线（现价{cur_price:.3f} vs 成本{pos.avg_cost:.3f}）",
                    priority=1,
                )
            )
            continue

        # 1C. ETF 趋势跌破 MA20 退出
        if pos.asset_type == "etf":
            metrics = calc_etf_metrics(kline_map.get(code) or [])
            if metrics and cur_price < metrics["ma20"] * 0.992:
                intents.append(
                    OrderIntent(
                        code=code,
                        name=pos.name,
                        asset_type="etf",
                        side="sell",
                        qty=pos.available_qty,
                        limit_price=round_price(cur_price, "etf"),
                        reason=f"ETF跌破MA20趋势离场（现价{cur_price:.3f} < MA20 {metrics['ma20']:.3f}）",
                        priority=3,
                    )
                )

    # ── 第二步：核心 ETF 轮动开仓（仅当水位有余量且非橙/红档） ──
    if tier in ("orange", "red", "kill_switch"):
        return sorted(intents, key=lambda x: x.priority)

    current_mv = sum(
        p.qty * (quotes[c].price if c in quotes and quotes[c].price > 0 else p.last_price)
        for c, p in state.positions.items()
    )
    current_weight = current_mv / total_assets
    if current_weight >= pos_cap - 0.05:
        return sorted(intents, key=lambda x: x.priority)

    # 冰点弱市只允许防御/避险桶，避免抄底弱势权益
    allowed_buckets = (
        {"defensive"}
        if breadth.above_ma20_pct < 25.0
        else {"defensive", "broad", "growth", "sector"}
    )

    candidates: list[tuple[float, str, dict[str, str], dict[str, float], LiveQuote]] = []
    for code, meta in cfg.etf_universe.items():
        norm_code = normalize_code(code)
        if meta.get("bucket") not in allowed_buckets:
            continue
        if norm_code in state.positions:
            continue
        q = quotes.get(norm_code)
        bars = kline_map.get(norm_code) or []
        if q is None or q.price <= 0 or q.halted:
            continue
        metrics = calc_etf_metrics(bars)
        if not metrics:
            continue
        # 趋势过滤：现价站上 MA20、20日动量为正、不追高（距 MA5 <= 3%）
        if q.price > metrics["ma20"] and metrics["mom20_pct"] > 0.0 and metrics["dev_ma5_pct"] <= 3.0:
            score = metrics["mom20_pct"] * 0.7 + metrics["mom5_pct"] * 0.3
            candidates.append((score, norm_code, meta, metrics, q))

    candidates.sort(key=lambda item: item[0], reverse=True)

    available_budget = min(
        state.cash * 0.98,
        max(0.0, (pos_cap - current_weight) * total_assets),
    )
    slots_left = max(0, cfg.max_positions - len(state.positions))

    for score, code, meta, metrics, q in candidates:
        if slots_left <= 0 or available_budget < cfg.min_order_value:
            break
        per_target = min(available_budget, total_assets * cfg.max_single_etf_weight * 0.85)
        lot_cost = q.price * 100.0
        if lot_cost <= 0:
            continue
        qty = int(per_target // lot_cost) * 100
        if qty < 100 or qty * q.price < cfg.min_order_value:
            continue
        intents.append(
            OrderIntent(
                code=code,
                name=meta["name"],
                asset_type="etf",
                side="buy",
                qty=qty,
                limit_price=round_price(q.price, "etf"),
                reason=(
                    f"ETF趋势动量入选（桶={meta['bucket']}，20日动量{metrics['mom20_pct']:+.2f}%，"
                    f"现价{q.price:.3f}>MA20 {metrics['ma20']:.3f}，评分{score:.2f}）"
                ),
                priority=10,
            )
        )
        available_budget -= qty * q.price
        slots_left -= 1

    return sorted(intents, key=lambda x: x.priority)


# ────────────────────────── 纸面撮合与状态演进 ──────────────────────────


def mark_to_market(state: Acct2State, quotes: dict[str, LiveQuote]) -> None:
    """按最新报价刷新持仓市价与历史高点净值（HWM）。"""
    for code, pos in state.positions.items():
        q = quotes.get(code)
        if q and q.price > 0:
            pos.last_price = q.price
    if state.total_assets > state.hwm_assets:
        state.hwm_assets = state.total_assets


def execute_paper_order(
    intent: OrderIntent,
    quote: LiveQuote,
    state: Acct2State,
    cfg: Acct2ManagedConfig,
    trade_date: str,
    now_dt: datetime,
) -> dict[str, Any]:
    """在纸面账本中执行一笔已过风控门的模拟单（含滑点、真实佣金与印花税、T+1 冻结）。"""
    slip = cfg.slippage_bps / 10000.0
    raw_px = quote.price * (1.0 + slip) if intent.side == "buy" else quote.price * (1.0 - slip)
    fill_price = round_price(raw_px, intent.asset_type)
    amount = round(fill_price * intent.qty, 2)
    fees = calc_trade_fees(
        intent.side,
        amount,
        intent.asset_type,
        cfg.commission_rate,
        cfg.min_commission,
        cfg.stamp_tax_rate,
    )
    norm_code = normalize_code(intent.code)

    if intent.side == "buy":
        total_cost = round(amount + fees["total_fee"], 2)
        state.cash = round(state.cash - total_cost, 2)
        if norm_code in state.positions:
            pos = state.positions[norm_code]
            new_qty = pos.qty + intent.qty
            pos.avg_cost = round((pos.avg_cost * pos.qty + total_cost) / new_qty, 4)
            pos.qty = new_qty
            pos.last_price = quote.price
            # T+1：当日买入不增加 available_qty
        else:
            state.positions[norm_code] = Position(
                code=norm_code,
                name=intent.name,
                asset_type=intent.asset_type,
                qty=intent.qty,
                available_qty=0,  # T+1 冻结
                avg_cost=round(total_cost / intent.qty, 4),
                last_price=quote.price,
                entry_date=trade_date,
            )
    else:
        pos = state.positions[norm_code]
        net_proceeds = round(amount - fees["total_fee"], 2)
        state.cash = round(state.cash + net_proceeds, 2)
        pos.qty -= intent.qty
        pos.available_qty -= intent.qty
        pos.last_price = quote.price
        if pos.qty <= 0:
            del state.positions[norm_code]

    state.total_fees_paid = round(state.total_fees_paid + fees["total_fee"], 2)
    state.orders_today[norm_code] = state.orders_today.get(norm_code, 0) + 1
    state.last_trade_date = trade_date

    return {
        "ts": now_dt.isoformat(timespec="seconds"),
        "trade_date": trade_date,
        "account": state.account,
        "dry_run": cfg.dry_run,
        "code": norm_code,
        "name": intent.name,
        "asset_type": intent.asset_type,
        "side": intent.side,
        "qty": intent.qty,
        "quote_price": quote.price,
        "fill_price": fill_price,
        "amount": amount,
        "fees": fees,
        "reason": intent.reason,
        "cash_after": state.cash,
        "total_assets_after": state.total_assets,
    }


def apply_eod_settlement(
    state: Acct2State,
    cfg: Acct2ManagedConfig,
    quotes: dict[str, LiveQuote],
    trade_date: str,
) -> dict[str, Any]:
    """盘后结算：盯市、更新 HWM、检查 -12% 硬熔断、T+1 解冻、计算对比基准。"""
    mark_to_market(state, quotes)

    # 红线回撤检查：触及 -12% 自动锁死 kill_switch
    if state.drawdown_from_hwm >= cfg.dd_red_pct and not state.kill_switch:
        state.kill_switch = True
        state.kill_switch_reason = (
            f"EOD 触发 -{cfg.dd_red_pct*100:.0f}% 硬回撤熔断"
            f"（当前回撤 -{state.drawdown_from_hwm*100:.2f}%，总资产 {state.total_assets:.2f}）"
        )

    # 计算躺平对比基准（原样持有接管日仓位不动）
    bm_mv = 0.0
    for code, qty in state.benchmark_holdings.items():
        px = quotes[code].price if code in quotes and quotes[code].price > 0 else 0.0
        if px <= 0 and code in state.positions:
            px = state.positions[code].last_price
        bm_mv += qty * px
    bm_assets = round(state.benchmark_cash + bm_mv, 2)
    bm_nav = round(bm_assets / state.base_assets, 6) if state.base_assets > 0 else 1.0

    # 沪深300基准（510300.SH）
    hs300_q = quotes.get("510300.SH")
    hs300_nav = None
    if hs300_q and hs300_q.price > 0:
        if not state.benchmark_hs300_base:
            state.benchmark_hs300_base = hs300_q.price
        hs300_nav = round(hs300_q.price / state.benchmark_hs300_base, 6)

    tier, _ = evaluate_risk_tier(state, cfg)
    record = {
        "trade_date": trade_date,
        "account": state.account,
        "dry_run": cfg.dry_run,
        "total_assets": state.total_assets,
        "cash": state.cash,
        "market_value": state.market_value,
        "position_weight": round(state.market_value / max(state.total_assets, 1.0), 4),
        "nav": state.nav,
        "hwm_assets": state.hwm_assets,
        "drawdown_pct": round(-state.drawdown_from_hwm * 100.0, 2),
        "daily_pnl_pct": round(state.daily_pnl_pct * 100.0, 2),
        "risk_tier": tier,
        "kill_switch": state.kill_switch,
        "total_fees_paid": state.total_fees_paid,
        "benchmark_hold_assets": bm_assets,
        "benchmark_hold_nav": bm_nav,
        "excess_vs_hold_pct": round((state.nav - bm_nav) * 100.0, 2),
        "benchmark_hs300_nav": hs300_nav,
        "positions": [
            {
                "code": p.code,
                "name": p.name,
                "qty": p.qty,
                "last_price": p.last_price,
                "market_value": p.market_value,
            }
            for p in state.positions.values()
        ],
    }

    # T+1 解冻与次日状态复位
    for pos in state.positions.values():
        pos.available_qty = pos.qty
    state.prev_eod_assets = state.total_assets
    state.orders_today = {}
    state.last_trade_date = trade_date
    return record


# ────────────────────────── 文件存储工具 ──────────────────────────


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
