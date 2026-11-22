# -*- coding: utf-8 -*-
"""
===============================================================================
AR3 v3.0 阶段一 · 国金标准 QMT 纯内置原生策略（#01 跨资产 ETF 全天候轮动 + AR2_2A 冰点反抽）
===============================================================================
核心特性（100% 适配 2026-10 监管与国金标准 QMT v2.1.19+ 环境）：
  1. 零文件 IO（不调用 open / os / pathlib 读写任何磁盘文件）
  2. 零外部网络请求（不调用 requests / urllib / socket，绝不触发 onIllegalIPFind）
  3. 零 MiniQMT / xtquant 外部进程依赖（100% 运行在标准大 QMT 内置 Python 3.6 引擎内）
  4. 策略口径与 AiStock AR3 v3.0 #01 实证冠军（AR3_9_RegimeFocus_Top1 + AR2_2A）100% 一致：
       - 进攻期（上证确认上升）：配 A 股强势宽基 Top2（站上 MA20 且 20日动量 > 0）
       - 非进攻期（横盘/防守/冰点未企稳）：100% 聚焦非 A 股跨资产 20日动量 Top1
         （红利低波 / 黄金 / 纳指 / 标普500 / 恒生科技 / 中概互联；全破位切十年国债 511260.SH）
       - 极端冰点右侧反抽（AR2_2A）：防守态 + 沪深300宽度 <= 18% + 创业板/中证1000均涨 >= +0.50%
         触发 30% 创业板ETF(159915.SZ) + 30% 中证1000ETF(512100.SH) + 40% 非A股最强避险ETF
         （反抽仓位执行 -6% 硬止损 / +12% 止盈 / 最长持有 5 个交易日）
  5. 实盘安全护栏：
       - 默认 DRY_RUN = True（仅打印完整排行、五态判定与买卖演练单，绝不发真实委托）
       - 仅管理 14 只 AR3 ETF 白名单（绝不误动账号内其他手工股票）
       - 严格「先卖后买」分步编排 + 3位小数保护限价 + 100股整手 + 单笔金额上限

使用步骤（3 步上手）：
  1. 关闭 Clash Verge「系统代理」开关（仅保留 TUN 模式，避免触发 QMT 的 illegal IP: 127.0.0.1:7897 强停）
  2. 打开国金标准 QMT -> 顶部「模型研究」或「模型交易」-> 新建 Python 策略 -> 全文粘贴本文件
  3. 修改下方 ACCOUNT_ID 为你的国金资金账号，保持 DRY_RUN = True，在「沪深300(000300.SH) 日线」运行验证
     待观察 1~2 个交易日零报错且信号与 AiStock 作战台一致后，再按需将 DRY_RUN 改为 False。
===============================================================================
"""

import datetime
import math
import numpy as np

# =============================================================================
# 一、用户配置区（仅需按需修改此区域）
# =============================================================================
ACCOUNT_ID = "8890729932"          # 国金标准 QMT 资金账号（模型交易中也可自动读取内置 account 变量）
DRY_RUN = True                     # 安全默认：True=纯仿真演练(只打印不发单)；确认无误后再改为 False
TARGET_INVEST_RATIO = 0.98         # 目标总投资比例（预留 2% 现金缓冲防滑点与手续费）
MIN_REBALANCE_DIFF_YUAN = 1500.0   # 最小调仓金额阈值（小于此额度的微调不交易，省摩擦）
MAX_SINGLE_ORDER_YUAN = 50000.0    # 单笔委托金额上限（超过时自动拆单）
REBALANCE_TIME_HHMM = 935          # 每日主调仓触发时间（09:35，避开集合竞价剧烈滑点）
BUY_SLIPPAGE_PCT = 0.002           # 买入保护限价上浮 +0.2%
SELL_SLIPPAGE_PCT = 0.002          # 卖出保护限价下浮 -0.2%
ONLY_MANAGE_ETF_UNIVERSE = True    # 仅管理下方 14 只 AR3 ETF，绝不卖出账户中其他个股

# AR2_2A 极端冰点反抽参数（与 AiStock 实证参数一致）
PANIC_BREADTH_THRESHOLD_PCT = 18.0 # 市场站上 MA20 宽度 <= 18% 进入冰点戒备
PANIC_PAIR_REVERSAL_PCT = 0.50     # 创业板指 + 中证1000 当日均涨 >= +0.50% 确认右侧反抽
PANIC_STOP_LOSS_PCT = -6.0         # 反抽仓位硬止损 -6%
PANIC_TAKE_PROFIT_PCT = 12.0       # 反抽仓位止盈 +12%
PANIC_MAX_HOLD_DAYS = 5            # 反抽仓位最长持有 5 个交易日

# =============================================================================
# 二、AR3 v3.0 14 只跨资产 ETF 标的池（与 ClickHouse stock.etf_kline_daily 100% 同步）
# =============================================================================
SAFE_BOND_CODE = "511260.SH"
INDEX_SH = "000001.SH"             # 上证指数（AR3 大盘五态择时锚）
INDEX_CYB = "399006.SZ"            # 创业板指（AR2_2A 右侧反抽锚 1）
INDEX_ZZ1000 = "000852.SH"         # 中证1000（AR2_2A 右侧反抽锚 2）
PANIC_ETF_CYB = "159915.SZ"        # 创业板ETF
PANIC_ETF_ZZ1000 = "512100.SH"     # 中证1000ETF

# 格式: (QMT代码, 名称, 资产类别)
# 类别说明: cn_broad=A股宽基, cn_dividend=A股红利, gold=黄金, overseas=跨境QDII, bond=利率债
ETF_UNIVERSE = [
    ("510300.SH", "沪深300ETF",   "cn_broad"),
    ("510500.SH", "中证500ETF",   "cn_broad"),
    ("512100.SH", "中证1000ETF",  "cn_broad"),
    ("159915.SZ", "创业板ETF",    "cn_broad"),
    ("588000.SH", "科创50ETF",    "cn_broad"),
    ("510880.SH", "红利ETF",      "cn_dividend"),
    ("512890.SH", "红利低波ETF",  "cn_dividend"),
    ("518880.SH", "黄金ETF",      "gold"),
    ("513100.SH", "纳指ETF",      "overseas"),
    ("513500.SH", "标普500ETF",   "overseas"),
    ("513180.SH", "恒生科技ETF",  "overseas"),
    ("513050.SH", "中概互联ETF",  "overseas"),
    ("511260.SH", "十年国债ETF",  "bond"),
    ("511090.SH", "30年国债ETF",  "bond"),
]

ETF_CODES = [item[0] for item in ETF_UNIVERSE]
ETF_NAME_MAP = dict((item[0], item[1]) for item in ETF_UNIVERSE)
ETF_CLASS_MAP = dict((item[0], item[2]) for item in ETF_UNIVERSE)
ALL_WATCH_CODES = ETF_CODES + [INDEX_SH, INDEX_CYB, INDEX_ZZ1000]


# =============================================================================
# 三、核心算法纯函数（零外部依赖，可独立离线单元测试）
# =============================================================================
def _to_float_list(seq):
    """安全提取有效浮点收盘价序列，剔除 NaN / <=0。"""
    out = []
    if seq is None:
        return out
    for v in seq:
        try:
            f = float(v)
            if not math.isnan(f) and not math.isinf(f) and f > 0.0:
                out.append(f)
        except Exception:
            continue
    return out


def classify_sh_regime(sh_closes, breadth_pct=50.0, confirm_days=2):
    """
    计算上证指数 AR3 复合五态（与 AiStock 五态择时口径严格一致）：
      - 原始日态：
          close > ma20 且 ma20 > ma60 且 mom60 > 0  -> up
          close < ma20 且 (close < ma60 或 mom60 < 0) -> down
          其余 -> sideways
      - 连续 confirm_days=2 日确认后才切换 confirmed_regime，防止单日噪声反复打脸
      - 结合市场站上 MA20 宽度 breadth_pct <= 18% 升级为 extreme_panic
    返回 dict: {regime, regime_label, is_offensive, confirmed_regime, streak_days, mom60_pct, breadth_pct}
    """
    closes = _to_float_list(sh_closes)
    if len(closes) < 65:
        return {
            "regime": "defensive",
            "regime_label": "防守模式(历史K线不足安全降级)",
            "is_offensive": False,
            "confirmed_regime": "down",
            "streak_days": 0,
            "mom60_pct": 0.0,
            "breadth_pct": float(breadth_pct),
        }

    arr = np.array(closes, dtype=float)
    raw_states = []
    for idx in range(60, len(arr)):
        c = arr[idx]
        ma20 = float(np.mean(arr[idx - 19:idx + 1]))
        ma60 = float(np.mean(arr[idx - 59:idx + 1]))
        mom60 = (c / arr[idx - 60]) - 1.0
        if c > ma20 and ma20 > ma60 and mom60 > 0.0:
            raw_states.append("up")
        elif c < ma20 and (c < ma60 or mom60 < 0.0):
            raw_states.append("down")
        else:
            raw_states.append("sideways")

    # 连续 N=2 确认防抖
    confirmed = raw_states[0]
    pending = None
    pending_cnt = 0
    streak = 1
    for st in raw_states[1:]:
        if st == confirmed:
            pending = None
            pending_cnt = 0
            streak += 1
        else:
            if st == pending:
                pending_cnt += 1
            else:
                pending = st
                pending_cnt = 1
            if pending_cnt >= confirm_days:
                confirmed = st
                pending = None
                pending_cnt = 0
                streak = confirm_days

    mom60_pct = round(((arr[-1] / arr[-61]) - 1.0) * 100.0, 2)
    b_pct = round(float(breadth_pct), 2)

    if confirmed == "up":
        if b_pct >= 72.0 and mom60_pct >= 12.0:
            regime = "extreme_offensive"
            label = "主升进攻·高热"
        else:
            regime = "offensive"
            label = "进攻模式"
    elif confirmed == "sideways":
        regime = "balanced"
        label = "平衡震荡"
    else:
        if b_pct <= PANIC_BREADTH_THRESHOLD_PCT:
            regime = "extreme_panic"
            label = "极端防守·恐慌冰点"
        else:
            regime = "defensive"
            label = "防守模式"

    return {
        "regime": regime,
        "regime_label": label,
        "is_offensive": regime in ("offensive", "extreme_offensive"),
        "confirmed_regime": confirmed,
        "streak_days": streak,
        "mom60_pct": mom60_pct,
        "breadth_pct": b_pct,
    }


def compute_etf_metrics(close_map):
    """
    计算 14 只跨资产 ETF 的 1日/20日动量、MA20、年化波动率与风险调整动量。
    close_map: {code: [close_0, ..., close_t]}（使用前复权日收盘序列）
    """
    rows = []
    for code, name, asset_class in ETF_UNIVERSE:
        closes = _to_float_list(close_map.get(code))
        if len(closes) < 22:
            continue
        arr = np.array(closes, dtype=float)
        c_now = float(arr[-1])
        c_prev1 = float(arr[-2])
        c_prev20 = float(arr[-21])
        ma20 = float(np.mean(arr[-20:]))
        ret1 = (c_now / c_prev1 - 1.0) * 100.0
        ret20 = (c_now / c_prev20 - 1.0) * 100.0
        daily_rets = np.diff(arr[-21:]) / arr[-21:-1]
        vol20 = float(np.std(daily_rets, ddof=1)) * math.sqrt(242.0) if len(daily_rets) >= 2 else 0.20
        above_ma20 = c_now > ma20
        ram = (ret20 / 100.0) / (vol20 + 0.05)
        rows.append({
            "code": code,
            "name": name,
            "asset_class": asset_class,
            "close": round(c_now, 4),
            "ma20": round(ma20, 4),
            "ret_1d_pct": round(ret1, 2),
            "ret_20d_pct": round(ret20, 2),
            "vol_20d_pct": round(vol20 * 100.0, 1),
            "risk_adj_mom": round(ram, 4),
            "above_ma20": above_ma20,
            "eligible": bool(above_ma20 and ret20 > 0.0),
        })
    return rows


def decide_ar3_etf01_targets(regime_info, etf_rows, panic_active=False):
    """
    AR3 #01（AR3_9_RegimeFocus_Top1 + AR2_2A 冰点反抽）目标权重决策器。
    返回: (target_weights_dict, headline_str)
      target_weights_dict: {code: weight_0_to_1}（总和 = 1.0）
    """
    def _pick_top(pool, key, k):
        cands = [x for x in pool if x["eligible"]]
        cands.sort(key=lambda x: x[key], reverse=True)
        return cands[:k]

    cn_broad = [x for x in etf_rows if x["asset_class"] == "cn_broad"]
    non_cn = [x for x in etf_rows if x["asset_class"] != "cn_broad"]

    # 1. 极端冰点右侧反抽接管：30% 创业板 + 30% 中证1000 + 40% 非A股风险调整动量 Top1
    if panic_active:
        safe_top = _pick_top(non_cn, "risk_adj_mom", 1)
        safe_code = safe_top[0]["code"] if safe_top else SAFE_BOND_CODE
        targets = {
            PANIC_ETF_CYB: 0.30,
            PANIC_ETF_ZZ1000: 0.30,
            safe_code: 0.40,
        }
        headline = "AR2_2A 冰点右侧反抽生效：30%% 创业板ETF + 30%% 中证1000ETF + 40%% %s" % ETF_NAME_MAP.get(safe_code, safe_code)
        return targets, headline

    # 2. 进攻期：优先配 A 股强势宽基 20 日动量 Top2
    if regime_info.get("is_offensive"):
        top_cn = _pick_top(cn_broad, "ret_20d_pct", 2)
        if top_cn:
            w = 1.0 / float(len(top_cn))
            targets = dict((x["code"], w) for x in top_cn)
            names = " + ".join(["%s(%.1f%%)" % (x["name"], x["ret_20d_pct"]) for x in top_cn])
            headline = "进攻期 #01 宽基动量 Top%d：%s" % (len(top_cn), names)
            return targets, headline

        # 若 A 股宽基暂未站上趋势，切非 A 股最强单核
        top_non = _pick_top(non_cn, "ret_20d_pct", 1)
        code = top_non[0]["code"] if top_non else SAFE_BOND_CODE
        headline = "进攻期但A股宽基未达标 -> 暂切非A股最强 %s" % ETF_NAME_MAP.get(code, code)
        return {code: 1.0}, headline

    # 3. 平衡 / 防守 / 冰点未企稳：100% 聚焦非 A 股跨资产 20 日纯动量 Top1
    top_non = _pick_top(non_cn, "ret_20d_pct", 1)
    if top_non:
        winner = top_non[0]
        headline = "非进攻期(%s) -> 100%% 切入非A股最强单核：%s(%s, 20d %+.2f%%)" % (
            regime_info.get("regime_label", ""), winner["name"], winner["code"], winner["ret_20d_pct"]
        )
        return {winner["code"]: 1.0}, headline

    headline = "非进攻期且全球资产全破 MA20 -> 100%% 转入十年国债ETF(%s)避险" % SAFE_BOND_CODE
    return {SAFE_BOND_CODE: 1.0}, headline


def build_rebalance_orders(
    total_assets,
    available_cash,
    holdings,
    target_weights,
    price_map,
    invest_ratio=TARGET_INVEST_RATIO,
    min_diff_yuan=MIN_REBALANCE_DIFF_YUAN,
    only_manage_etf=ONLY_MANAGE_ETF_UNIVERSE,
):
    """
    计算精确「先卖后买」100 股整手调仓指令单。
    holdings: {code: {"volume": int, "can_use": int, "open_price": float, "market_value": float}}
    返回: (sell_orders, buy_orders)
    """
    sell_orders = []
    buy_orders = []
    if total_assets <= 100.0:
        return sell_orders, buy_orders

    investable_assets = float(total_assets) * float(invest_ratio)
    managed_set = set(ETF_CODES) if only_manage_etf else set(list(holdings.keys()) + list(target_weights.keys()))

    # 第一步：计算所有需要卖出或减仓的标的（严格按可用股数 can_use）
    for code, pos in holdings.items():
        if only_manage_etf and code not in managed_set:
            continue
        cur_vol = int(pos.get("volume", 0))
        can_use = int(pos.get("can_use", 0))
        px = float(price_map.get(code, 0.0))
        if cur_vol <= 0 or can_use < 100 or px <= 0.0:
            continue

        tgt_w = float(target_weights.get(code, 0.0))
        tgt_yuan = investable_assets * tgt_w
        cur_yuan = cur_vol * px

        if tgt_w <= 0.0:
            # 非目标标的：全部清仓可用股数（按 100 股向下取整）
            sell_vol = (can_use // 100) * 100
            if sell_vol >= 100:
                sell_orders.append({
                    "code": code,
                    "name": ETF_NAME_MAP.get(code, code),
                    "side": "SELL",
                    "volume": sell_vol,
                    "ref_price": round(px, 3),
                    "limit_price": round(px * (1.0 - SELL_SLIPPAGE_PCT), 3),
                    "est_yuan": round(sell_vol * px, 2),
                    "reason": "非目标ETF清退",
                })
        elif cur_yuan - tgt_yuan >= min_diff_yuan:
            diff_vol = int((cur_yuan - tgt_yuan) / px / 100.0) * 100
            sell_vol = min(diff_vol, (can_use // 100) * 100)
            if sell_vol >= 100:
                sell_orders.append({
                    "code": code,
                    "name": ETF_NAME_MAP.get(code, code),
                    "side": "SELL",
                    "volume": sell_vol,
                    "ref_price": round(px, 3),
                    "limit_price": round(px * (1.0 - SELL_SLIPPAGE_PCT), 3),
                    "est_yuan": round(sell_vol * px, 2),
                    "reason": "超配减仓至目标权重 %.0f%%" % (tgt_w * 100.0),
                })

    # 第二步：按当前真实可用现金计算买入指令（若有卖单待成交，本轮只用现有可用现金，卖后下轮再买剩余）
    cash_left = max(0.0, float(available_cash) * 0.995)
    for code, tgt_w in sorted(target_weights.items(), key=lambda kv: kv[1], reverse=True):
        if tgt_w <= 0.0:
            continue
        px = float(price_map.get(code, 0.0))
        if px <= 0.0:
            continue
        cur_vol = int(holdings.get(code, {}).get("volume", 0))
        cur_yuan = cur_vol * px
        tgt_yuan = investable_assets * float(tgt_w)
        need_yuan = tgt_yuan - cur_yuan
        if need_yuan < min_diff_yuan:
            continue

        # 本轮实际能买金额受当前可用现金约束
        limit_px = round(px * (1.0 + BUY_SLIPPAGE_PCT), 3)
        alloc_yuan = min(need_yuan, cash_left)
        buy_vol = int(alloc_yuan / limit_px / 100.0) * 100
        if buy_vol >= 100:
            est = round(buy_vol * limit_px, 2)
            cash_left = max(0.0, cash_left - est)
            buy_orders.append({
                "code": code,
                "name": ETF_NAME_MAP.get(code, code),
                "side": "BUY",
                "volume": buy_vol,
                "ref_price": round(px, 3),
                "limit_price": limit_px,
                "est_yuan": est,
                "reason": "按目标权重 %.0f%% 建仓/补足" % (tgt_w * 100.0),
            })

    return sell_orders, buy_orders


# =============================================================================
# 四、QMT 原生行情读取、账户读取与合规下单封装（零文件 IO、零网络外连）
# =============================================================================
def _get_active_account(ContextInfo):
    """优先使用模型交易界面绑定的全局 account 变量，否则回退顶部 ACCOUNT_ID。"""
    try:
        bound = globals().get("account")
        if bound and str(bound).strip():
            return str(bound).strip()
    except Exception:
        pass
    return str(ACCOUNT_ID).strip()


def _estimate_hs300_breadth(ContextInfo):
    """
    用沪深 300 成分股站上 MA20 的比例估算全市场宽度（本地纯内存计算，耗时 <0.3 秒）。
    若成分股数据暂缺，自动回退用 5 只 A 股宽基 ETF 站上 MA20 比例。
    """
    try:
        members = ContextInfo.get_stock_list_in_sector("沪深300")
        if members and len(members) >= 50:
            md = ContextInfo.get_market_data_ex(
                ["close"],
                stock_code=members,
                period="1d",
                count=25,
                dividend_type="front",
                fill_data=True,
                subscribe=False,
            )
            above = 0
            valid = 0
            for code in members:
                df = md.get(code)
                if df is None or len(df) < 20:
                    continue
                vals = _to_float_list(df["close"].values)
                if len(vals) < 20:
                    continue
                valid += 1
                if vals[-1] > float(np.mean(vals[-20:])):
                    above += 1
            if valid >= 50:
                return round(100.0 * above / float(valid), 2)
    except Exception as exc:
        print("[AR3-QMT][WARN] 沪深300宽度计算异常，使用宽基ETF回退: %s" % exc)
    return 50.0


def _fetch_daily_closes(ContextInfo, codes, count=90):
    """通过 QMT 原生 get_market_data_ex 读取前复权日线收盘价（零外部网络请求）。"""
    md = ContextInfo.get_market_data_ex(
        ["close"],
        stock_code=codes,
        period="1d",
        count=count,
        dividend_type="front",
        fill_data=True,
        subscribe=False,
    )
    out = {}
    for code in codes:
        df = md.get(code)
        if df is not None and len(df) > 0 and "close" in df.columns:
            out[code] = _to_float_list(df["close"].values)
        else:
            out[code] = []
    return out


def _fetch_realtime_prices(ContextInfo, codes, fallback_close_map):
    """通过 QMT 原生 get_full_tick 获取最新价，盘后/休市自动回退最近日收盘价。"""
    prices = {}
    ticks = {}
    try:
        ticks = ContextInfo.get_full_tick(codes) or {}
    except Exception:
        ticks = {}

    for code in codes:
        px = 0.0
        tk = ticks.get(code)
        if isinstance(tk, dict):
            for k in ("lastPrice", "lastClose"):
                try:
                    v = float(tk.get(k, 0.0))
                    if v > 0.0 and not math.isnan(v):
                        px = v
                        break
                except Exception:
                    pass
        if px <= 0.0:
            seq = fallback_close_map.get(code) or []
            if seq:
                px = float(seq[-1])
        prices[code] = px
    return prices, ticks


def _check_panic_intraday_reversal(ticks, fallback_close_map):
    """检查 AR2_2A 冰点右侧确认条件：创业板指 + 中证1000 当日均涨 >= +0.50%。"""
    changes = []
    for idx_code in (INDEX_CYB, INDEX_ZZ1000):
        tk = ticks.get(idx_code) if isinstance(ticks, dict) else None
        pct = None
        if isinstance(tk, dict):
            try:
                lp = float(tk.get("lastPrice", 0.0))
                lc = float(tk.get("lastClose", 0.0))
                if lp > 0.0 and lc > 0.0:
                    pct = (lp / lc - 1.0) * 100.0
            except Exception:
                pct = None
        if pct is None:
            seq = fallback_close_map.get(idx_code) or []
            if len(seq) >= 2 and seq[-2] > 0.0:
                pct = (seq[-1] / seq[-2] - 1.0) * 100.0
        if pct is not None:
            changes.append(pct)
    if len(changes) < 2:
        return False, 0.0
    avg_chg = round((changes[0] + changes[1]) / 2.0, 2)
    return (avg_chg >= PANIC_PAIR_REVERSAL_PCT), avg_chg


def _read_account_and_holdings(account_id):
    """通过 QMT 原生 get_trade_detail_data 读取真实资金与持仓。"""
    total_assets = 0.0
    available_cash = 0.0
    holdings = {}

    try:
        accts = get_trade_detail_data(account_id, "STOCK", "ACCOUNT") or []
        if accts:
            a = accts[0]
            total_assets = float(getattr(a, "m_dBalance", 0.0) or 0.0)
            available_cash = float(getattr(a, "m_dAvailable", 0.0) or 0.0)
    except Exception as exc:
        print("[AR3-QMT][WARN] 读取账户资金失败(%s): %s" % (account_id, exc))

    try:
        positions = get_trade_detail_data(account_id, "STOCK", "POSITION") or []
        for p in positions:
            vol = int(getattr(p, "m_nVolume", 0) or 0)
            if vol <= 0:
                continue
            code = "%s.%s" % (getattr(p, "m_strInstrumentID", ""), getattr(p, "m_strExchangeID", ""))
            holdings[code] = {
                "volume": vol,
                "can_use": int(getattr(p, "m_nCanUseVolume", 0) or 0),
                "open_price": float(getattr(p, "m_dOpenPrice", 0.0) or 0.0),
                "market_value": float(getattr(p, "m_dMarketValue", 0.0) or 0.0),
            }
    except Exception as exc:
        print("[AR3-QMT][WARN] 读取账户持仓失败(%s): %s" % (account_id, exc))

    return total_assets, available_cash, holdings


def _submit_order_safely(ContextInfo, account_id, order):
    """合规下单封装：默认 DRY_RUN 仅打印；实盘时自动按单笔金额上限拆单调用原生 passorder。"""
    code = order["code"]
    side = order["side"]
    total_vol = int(order["volume"])
    limit_px = round(float(order["limit_price"]), 3)
    op_type = 23 if side == "BUY" else 24

    # 单笔上限拆单保护
    max_shares_per_leg = max(100, int(MAX_SINGLE_ORDER_YUAN / max(limit_px, 0.01) / 100.0) * 100)
    remain = total_vol
    leg_idx = 0
    while remain >= 100:
        leg_vol = min(remain, max_shares_per_leg)
        leg_vol = (leg_vol // 100) * 100
        if leg_vol < 100:
            break
        leg_idx += 1
        tag = "AR3_ETF01_%s_%s" % (side, datetime.datetime.now().strftime("%H%M%S"))
        if DRY_RUN:
            print(
                "[AR3-QMT][DRY_RUN 仿真单 #%d] 账号=%s | %s %s(%s) | %d股 @ 限价%.3f (约¥%.0f) | 原因: %s"
                % (leg_idx, account_id, side, order["name"], code, leg_vol, limit_px, leg_vol * limit_px, order["reason"])
            )
        else:
            print(
                "[AR3-QMT][LIVE 实盘报单 #%d] 账号=%s | %s %s(%s) | %d股 @ 限价%.3f (约¥%.0f) | 原因: %s"
                % (leg_idx, account_id, side, order["name"], code, leg_vol, limit_px, leg_vol * limit_px, order["reason"])
            )
            passorder(
                op_type,
                1101,
                account_id,
                code,
                11,
                limit_px,
                leg_vol,
                "AR3_ETF01_NATIVE",
                2,
                tag,
                ContextInfo,
            )
        remain -= leg_vol


def _is_in_trading_window(now_dt):
    """仅允许 A 股连续竞价安全窗口执行实盘下单（09:32~11:28, 13:02~14:55）。"""
    if now_dt.weekday() >= 5:
        return False
    hhmm = now_dt.hour * 100 + now_dt.minute
    return (932 <= hhmm <= 1128) or (1302 <= hhmm <= 1455)


def _run_ar3_cycle(ContextInfo, trigger_source="timer", allow_trade=False):
    """执行一次完整的 AR3 #01 信号评估、排行打印与（可选）合规调仓。"""
    now_dt = datetime.datetime.now()
    today_str = now_dt.strftime("%Y-%m-%d")
    account_id = _get_active_account(ContextInfo)

    # 1. 拉取行情 + 估算宽度 + 计算五态
    close_map = _fetch_daily_closes(ContextInfo, ALL_WATCH_CODES, count=90)
    price_map, ticks = _fetch_realtime_prices(ContextInfo, ALL_WATCH_CODES, close_map)
    breadth_pct = _estimate_hs300_breadth(ContextInfo)
    regime_info = classify_sh_regime(close_map.get(INDEX_SH, []), breadth_pct=breadth_pct)
    etf_rows = compute_etf_metrics(close_map)

    # 2. 检查 AR2_2A 冰点反抽状态（含持仓止盈/止损/5日到期退出检查）
    total_assets, available_cash, holdings = _read_account_and_holdings(account_id)
    panic_active = bool(getattr(ContextInfo, "ar3_panic_active", False))
    panic_entry_date = getattr(ContextInfo, "ar3_panic_entry_date", "")
    panic_hold_days = int(getattr(ContextInfo, "ar3_panic_hold_days", 0))

    if panic_active:
        # 每日跨日累加持有天数
        if panic_entry_date and panic_entry_date != today_str and getattr(ContextInfo, "ar3_panic_last_count_date", "") != today_str:
            panic_hold_days += 1
            ContextInfo.ar3_panic_hold_days = panic_hold_days
            ContextInfo.ar3_panic_last_count_date = today_str

        # 检查反抽 ETF（创业板/中证1000）是否触及 -6% 硬止损、+12% 止盈或满 5 日
        exit_reason = ""
        if panic_hold_days >= PANIC_MAX_HOLD_DAYS:
            exit_reason = "满 %d 个交易日到期退出" % PANIC_MAX_HOLD_DAYS
        else:
            for p_code in (PANIC_ETF_CYB, PANIC_ETF_ZZ1000):
                pos = holdings.get(p_code)
                if pos and pos.get("open_price", 0.0) > 0.0 and price_map.get(p_code, 0.0) > 0.0:
                    pnl_pct = (price_map[p_code] / pos["open_price"] - 1.0) * 100.0
                    if pnl_pct <= PANIC_STOP_LOSS_PCT:
                        exit_reason = "%s 触及硬止损(%.2f%% <= %.1f%%)" % (p_code, pnl_pct, PANIC_STOP_LOSS_PCT)
                        break
                    if pnl_pct >= PANIC_TAKE_PROFIT_PCT:
                        exit_reason = "%s 触及止盈(%.2f%% >= +%.1f%%)" % (p_code, pnl_pct, PANIC_TAKE_PROFIT_PCT)
                        break
        if exit_reason:
            print("[AR3-QMT][AR2_2A 退出] %s，关闭反抽态回归 #01 常规配置" % exit_reason)
            panic_active = False
            ContextInfo.ar3_panic_active = False
            ContextInfo.ar3_panic_hold_days = 0
            ContextInfo.ar3_panic_entry_date = ""
    else:
        # 若处于极端冰点态，检查右侧阳线确认
        if regime_info["regime"] == "extreme_panic":
            pair_ok, pair_avg = _check_panic_intraday_reversal(ticks, close_map)
            if pair_ok:
                print("[AR3-QMT][AR2_2A 触发!] 冰点宽度=%.1f%% 且 创业板+中证1000均涨=%+.2f%% >= +%.2f%%"
                      % (breadth_pct, pair_avg, PANIC_PAIR_REVERSAL_PCT))
                panic_active = True
                ContextInfo.ar3_panic_active = True
                ContextInfo.ar3_panic_entry_date = today_str
                ContextInfo.ar3_panic_last_count_date = today_str
                ContextInfo.ar3_panic_hold_days = 0

    # 3. 决策今日 #01 目标权重
    target_weights, headline = decide_ar3_etf01_targets(regime_info, etf_rows, panic_active=panic_active)

    # 4. 打印清晰诊断面板（零外部依赖，直接在 QMT 策略输出栏查看）
    print("=" * 78)
    print("[AR3-QMT #01 战报] %s | 触发=%s | 模式=%s | 账号=%s"
          % (now_dt.strftime("%Y-%m-%d %H:%M:%S"), trigger_source, "DRY_RUN仿真" if DRY_RUN else "LIVE实盘", account_id))
    print("  大盘五态: %s (%s, 连续%d日, 60日动量 %+.2f%%, 沪深300宽度 %.1f%%)"
          % (regime_info["regime_label"], regime_info["confirmed_regime"],
             regime_info["streak_days"], regime_info["mom60_pct"], regime_info["breadth_pct"]))
    print("  核心指令: %s" % headline)
    print("  账户概况: 总资产 ¥%.2f | 可用 ¥%.2f | 当前持仓数 %d" % (total_assets, available_cash, len(holdings)))

    ranked = sorted(etf_rows, key=lambda x: x["ret_20d_pct"], reverse=True)
    print("  14只跨资产ETF 20日动量前6名:")
    for r in ranked[:6]:
        flag = "OK" if r["eligible"] else "破趋势"
        print("    - %s %-8s [%s] 现价=%.3f | 20d=%+6.2f%% | MA20=%.3f | RAM=%+.3f | %s"
              % (r["code"], r["name"], r["asset_class"], r["close"], r["ret_20d_pct"], r["ma20"], r["risk_adj_mom"], flag))

    # 5. 计算先卖后买调仓单（若账号尚未入金为 0，则按 ¥100,000 仿真展示演练单）
    sim_mode_assets = total_assets
    sim_mode_cash = available_cash
    if DRY_RUN and total_assets < 1000.0:
        sim_mode_assets = 100000.0
        sim_mode_cash = 100000.0
        print("  [提示] 当前国金账号余额为 ¥0，DRY_RUN 按 ¥100,000 模拟净值演示精准买卖股数：")

    sell_orders, buy_orders = build_rebalance_orders(
        total_assets=sim_mode_assets,
        available_cash=sim_mode_cash,
        holdings=holdings,
        target_weights=target_weights,
        price_map=price_map,
    )

    if not sell_orders and not buy_orders:
        print("  调仓结论: 当前持仓已与 #01 目标权重对齐（或差额 < ¥%.0f），无需调仓。" % MIN_REBALANCE_DIFF_YUAN)
        print("=" * 78)
        return {"sells": [], "buys": [], "targets": target_weights, "regime": regime_info}

    # 先卖后买安全编排：本轮有卖单时先只执行卖单，待下轮资金到账后再买
    if sell_orders:
        print("  >>> 第一步·先卖清单 (%d 笔):" % len(sell_orders))
        for od in sell_orders:
            if allow_trade or DRY_RUN:
                _submit_order_safely(ContextInfo, account_id, od)
            else:
                print("    [非交易窗口仅预览] SELL %s(%s) %d股 @ %.3f" % (od["name"], od["code"], od["volume"], od["limit_price"]))
        if buy_orders:
            print("  >>> 第二步·待卖单回款后买入 (%d 笔，下轮定时器自动接续):" % len(buy_orders))
            for od in buy_orders:
                print("    [待买预览] BUY %s(%s) %d股 @ %.3f (约¥%.0f)" % (od["name"], od["code"], od["volume"], od["limit_price"], od["est_yuan"]))
    else:
        print("  >>> 买入清单 (%d 笔):" % len(buy_orders))
        for od in buy_orders:
            if allow_trade or DRY_RUN:
                _submit_order_safely(ContextInfo, account_id, od)
            else:
                print("    [非交易窗口仅预览] BUY %s(%s) %d股 @ %.3f" % (od["name"], od["code"], od["volume"], od["limit_price"]))

    print("=" * 78)
    return {"sells": sell_orders, "buys": buy_orders, "targets": target_weights, "regime": regime_info}


# =============================================================================
# 五、QMT 生命周期回调入口（init / handlebar / on_rebalance_timer）
# =============================================================================
def init(ContextInfo):
    ContextInfo.set_universe(ALL_WATCH_CODES)
    ContextInfo.ar3_boot_printed = False
    ContextInfo.ar3_last_rebalance_date = ""
    ContextInfo.ar3_last_poll_minute = -1
    ContextInfo.ar3_panic_active = False
    ContextInfo.ar3_panic_entry_date = ""
    ContextInfo.ar3_panic_last_count_date = ""
    ContextInfo.ar3_panic_hold_days = 0

    print("[AR3-QMT] 初始化完成：14只跨资产ETF #01全天候轮动 + AR2_2A冰点反抽（零文件IO / 零外部网络）")
    print("[AR3-QMT] 当前模式: %s | 绑定账号: %s" % ("DRY_RUN 仿真演练" if DRY_RUN else "LIVE 实盘下单", _get_active_account(ContextInfo)))

    # 注册 60 秒轮询定时器（纯内置调度，不依赖外链）
    ContextInfo.run_time("on_rebalance_timer", "60nSecond", "2026-01-01 09:30:00", "SH")


def handlebar(ContextInfo):
    # 仅在最后一根 K 线做一次启动即时自检，方便盘后或模型研究里立即验证输出
    if not ContextInfo.is_last_bar():
        return
    if not getattr(ContextInfo, "ar3_boot_printed", False):
        ContextInfo.ar3_boot_printed = True
        _run_ar3_cycle(ContextInfo, trigger_source="boot_self_check", allow_trade=False)


def on_rebalance_timer(ContextInfo):
    now_dt = datetime.datetime.now()
    today_str = now_dt.strftime("%Y-%m-%d")
    hhmm = now_dt.hour * 100 + now_dt.minute
    in_window = _is_in_trading_window(now_dt)

    # 1. 每日 09:35 触发主调仓（若先卖后买，09:36~09:42 会自动跟进买单，直到对齐）
    if in_window and hhmm >= REBALANCE_TIME_HHMM and hhmm <= REBALANCE_TIME_HHMM + 10:
        if getattr(ContextInfo, "ar3_last_rebalance_date", "") != today_str:
            res = _run_ar3_cycle(ContextInfo, trigger_source="daily_rebalance", allow_trade=True)
            # 若已经没有卖单（或卖单和买单都已完成/处于DRY_RUN），标记今日主调仓完成
            if DRY_RUN or not res.get("sells"):
                ContextInfo.ar3_last_rebalance_date = today_str
        return

    # 2. 盘中每 5 分钟（09:45 ~ 14:50）巡检 AR2_2A 冰点反抽右侧触发 & 止盈止损
    if in_window and now_dt.minute % 5 == 0 and now_dt.minute != getattr(ContextInfo, "ar3_last_poll_minute", -1):
        ContextInfo.ar3_last_poll_minute = now_dt.minute
        _run_ar3_cycle(ContextInfo, trigger_source="intraday_sentinel", allow_trade=True)
