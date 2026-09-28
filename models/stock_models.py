"""
股票数据模型

定义股票监控系统的所有数据模型
"""

from datetime import datetime as dt_datetime, date
import threading
import time
from sqlalchemy import Column, Integer, BigInteger, String, Date, DateTime, Boolean, DECIMAL, Text, func
from sqlalchemy.ext.declarative import declarative_base
from utils.database import Base

# 使用函数引用
def get_current_time():
    return dt_datetime.now()


_id_lock = threading.Lock()
_id_counter = 0


def generate_int_id():
    global _id_counter
    with _id_lock:
        _id_counter = (_id_counter + 1) % 1000
        return (time.time_ns() // 1000) * 1000 + _id_counter


# ========================================
# 1. 基础信息模型
# ========================================

class Stock(Base):
    """股票基础信息模型"""
    __tablename__ = 'stocks'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), unique=True, nullable=False, comment='股票代码')
    name = Column(String(50), nullable=False, comment='股票名称')
    market = Column(String(20), nullable=False, comment='市场：sh/sz/bj')
    type = Column(String(20), nullable=False, comment='类型：stock/index/sector/theme')
    region = Column(String(50), comment='所属地区')
    industry = Column(String(50), comment='所属行业')
    industry_code = Column(String(10), comment='行业指数代码')
    st = Column(Boolean, default=False, comment='是否ST 0=否 1=是')
    quit = Column(Boolean, default=False, comment='是否退市 0=否 1=是')
    list_date = Column(Date, comment='上市日期')
    delist_date = Column(Date, comment='退市/摘牌日期')
    self_selected = Column(Boolean, default=False, comment='是否自选')
    holding = Column(Boolean, default=False, comment='是否持仓')
    float_share = Column(DECIMAL(18, 4), default=0.0000, comment='流通股本(万股)')
    total_share = Column(DECIMAL(18, 4), default=0.0000, comment='总股本(万股)')
    created_at = Column(DateTime, default=func.current_timestamp(), comment='创建时间')
    updated_at = Column(DateTime, default=func.current_timestamp(), onupdate=func.current_timestamp(), comment='更新时间')
    
    def __repr__(self):
        return f"<Stock(code={self.code}, name={self.name})>"


class Sector(Base):
    """板块信息模型"""
    __tablename__ = 'sectors'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), unique=True, nullable=False, comment='板块代码')
    name = Column(String(50), nullable=False, comment='板块名称')
    type = Column(String(20), nullable=False, comment='类型：industry/theme')
    parent_code = Column(String(10), comment='父板块代码')
    level = Column(Integer, default=1, comment='层级')
    stock_count = Column(Integer, default=0, comment='成分股数量')
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')
    
    def __repr__(self):
        return f"<Sector(code={self.code}, name={self.name})>"


class SectorStock(Base):
    """板块成分股关联模型"""
    __tablename__ = 'sector_stocks'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    sector_code = Column(String(10), nullable=False, comment='板块代码')
    stock_code = Column(String(10), nullable=False, comment='股票代码')
    weight = Column(DECIMAL(10, 4), comment='权重')
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')
    
    def __repr__(self):
        return f"<SectorStock(sector={self.sector_code}, stock={self.stock_code})>"


# ========================================
# 2. K线数据模型
# ========================================

class KlineDaily(Base):
    """日线数据模型"""
    __tablename__ = 'kline_daily'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, comment='股票代码')
    trade_date = Column(Date, nullable=False, comment='交易日期')
    open = Column(DECIMAL(10, 3), nullable=False, comment='开盘价')
    high = Column(DECIMAL(10, 3), nullable=False, comment='最高价')
    low = Column(DECIMAL(10, 3), nullable=False, comment='最低价')
    close = Column(DECIMAL(10, 3), nullable=False, comment='收盘价')
    volume = Column(BigInteger, nullable=False, default=0, comment='成交量(手)')
    amount = Column(DECIMAL(20, 2), nullable=False, default=0, comment='成交额(元)')
    amplitude = Column(DECIMAL(10, 2), comment='振幅(%)')
    change_pct = Column(DECIMAL(10, 2), comment='涨跌幅(%)')
    change_amount = Column(DECIMAL(10, 3), comment='涨跌额')
    turnover_rate = Column(DECIMAL(10, 2), comment='换手率(%)')
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')
    
    def __repr__(self):
        return f"<KlineDaily(code={self.code}, date={self.trade_date}, close={self.close})>"


class KlineWeekly(Base):
    """周线数据模型"""
    __tablename__ = 'kline_weekly'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, comment='股票代码')
    week_start_date = Column(Date, nullable=False, comment='周开始日期')
    week_end_date = Column(Date, nullable=True, comment='周结束日期')
    open = Column(DECIMAL(10, 3), nullable=False, comment='开盘价')
    high = Column(DECIMAL(10, 3), nullable=False, comment='最高价')
    low = Column(DECIMAL(10, 3), nullable=False, comment='最低价')
    close = Column(DECIMAL(10, 3), comment='收盘价')
    volume = Column(BigInteger, nullable=False, default=0, comment='成交量(手)')
    amount = Column(DECIMAL(20, 2), nullable=False, default=0, comment='成交额(元)')
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')
    
    def __repr__(self):
        return f"<KlineWeekly(code={self.code}, week={self.week_start_date})>"


class KlineMonthly(Base):
    """月线数据模型"""
    __tablename__ = 'kline_monthly'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, comment='股票代码')
    month_start_date = Column(Date, nullable=False, comment='月开始日期')
    month_end_date = Column(Date, nullable=False, comment='月结束日期')
    open = Column(DECIMAL(10, 3), nullable=False, comment='开盘价')
    high = Column(DECIMAL(10, 3), nullable=False, comment='最高价')
    low = Column(DECIMAL(10, 3), nullable=False, comment='最低价')
    close = Column(DECIMAL(10, 3), comment='收盘价')
    volume = Column(BigInteger, nullable=False, default=0, comment='成交量(手)')
    amount = Column(DECIMAL(20, 2), nullable=False, default=0, comment='成交额(元)')
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')
    
    def __repr__(self):
        return f"<KlineMonthly(code={self.code}, month={self.month_start_date})>"


class KlineQuarterly(Base):
    """季度线数据模型"""
    __tablename__ = 'kline_quarterly'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, comment='股票代码')
    quarter = Column(Integer, nullable=False, comment='季度(1-4)')
    year = Column(Integer, nullable=False, comment='年份')
    open = Column(DECIMAL(10, 3), nullable=False, comment='开盘价')
    high = Column(DECIMAL(10, 3), nullable=False, comment='最高价')
    low = Column(DECIMAL(10, 3), nullable=False, comment='最低价')
    close = Column(DECIMAL(10, 3), comment='收盘价')
    volume = Column(BigInteger, nullable=False, default=0, comment='成交量(手)')
    amount = Column(DECIMAL(20, 2), nullable=False, default=0, comment='成交额(元)')
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')
    
    def __repr__(self):
        return f"<KlineQuarterly(code={self.code}, quarter=Q{self.quarter}{self.year})>"


class KlineYearly(Base):
    """年线数据模型"""
    __tablename__ = 'kline_yearly'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, comment='股票代码')
    year = Column(Integer, nullable=False, comment='年份')
    open = Column(DECIMAL(10, 3), nullable=False, comment='开盘价')
    high = Column(DECIMAL(10, 3), nullable=False, comment='最高价')
    low = Column(DECIMAL(10, 3), nullable=False, comment='最低价')
    close = Column(DECIMAL(10, 3), comment='收盘价')
    volume = Column(BigInteger, nullable=False, default=0, comment='成交量(手)')
    amount = Column(DECIMAL(20, 2), nullable=False, default=0, comment='成交额(元)')
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')
    
    def __repr__(self):
        return f"<KlineYearly(code={self.code}, year={self.year})>"


class KlineMinute1(Base):
    """1分钟K线数据模型"""
    __tablename__ = 'kline_minute_1'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, comment='股票代码')
    datetime = Column(DateTime, nullable=False, comment='日期时间')
    open = Column(DECIMAL(10, 3), nullable=False, comment='开盘价')
    high = Column(DECIMAL(10, 3), nullable=False, comment='最高价')
    low = Column(DECIMAL(10, 3), nullable=False, comment='最低价')
    close = Column(DECIMAL(10, 3), nullable=False, comment='收盘价')
    volume = Column(BigInteger, nullable=False, default=0, comment='成交量(手)')
    amount = Column(DECIMAL(20, 2), nullable=False, default=0, comment='成交额(元)')
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')
    
    def __repr__(self):
        return f"<KlineMinute1(code={self.code}, datetime={self.datetime})>"


class KlineMinute5(Base):
    """5分钟K线数据模型"""
    __tablename__ = 'kline_minute_5'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, comment='股票代码')
    datetime = Column(DateTime, nullable=False, comment='日期时间')
    open = Column(DECIMAL(10, 3), nullable=False, comment='开盘价')
    high = Column(DECIMAL(10, 3), nullable=False, comment='最高价')
    low = Column(DECIMAL(10, 3), nullable=False, comment='最低价')
    close = Column(DECIMAL(10, 3), nullable=False, comment='收盘价')
    volume = Column(BigInteger, nullable=False, default=0, comment='成交量(手)')
    amount = Column(DECIMAL(20, 2), nullable=False, default=0, comment='成交额(元)')
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')
    
    def __repr__(self):
        return f"<KlineMinute5(code={self.code}, datetime={self.datetime})>"


class KlineMinute15(Base):
    """15分钟K线数据模型"""
    __tablename__ = 'kline_minute_15'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, comment='股票代码')
    datetime = Column(DateTime, nullable=False, comment='日期时间')
    open = Column(DECIMAL(10, 3), nullable=False, comment='开盘价')
    high = Column(DECIMAL(10, 3), nullable=False, comment='最高价')
    low = Column(DECIMAL(10, 3), nullable=False, comment='最低价')
    close = Column(DECIMAL(10, 3), nullable=False, comment='收盘价')
    volume = Column(BigInteger, nullable=False, default=0, comment='成交量(手)')
    amount = Column(DECIMAL(20, 2), nullable=False, default=0, comment='成交额(元)')
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')
    
    def __repr__(self):
        return f"<KlineMinute15(code={self.code}, datetime={self.datetime})>"


class KlineMinute30(Base):
    """30分钟K线数据模型"""
    __tablename__ = 'kline_minute_30'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, comment='股票代码')
    datetime = Column(DateTime, nullable=False, comment='日期时间')
    open = Column(DECIMAL(10, 3), nullable=False, comment='开盘价')
    high = Column(DECIMAL(10, 3), nullable=False, comment='最高价')
    low = Column(DECIMAL(10, 3), nullable=False, comment='最低价')
    close = Column(DECIMAL(10, 3), nullable=False, comment='收盘价')
    volume = Column(BigInteger, nullable=False, default=0, comment='成交量(手)')
    amount = Column(DECIMAL(20, 2), nullable=False, default=0, comment='成交额(元)')
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')
    
    def __repr__(self):
        return f"<KlineMinute30(code={self.code}, datetime={self.datetime})>"


class KlineMinute60(Base):
    """60分钟K线数据模型"""
    __tablename__ = 'kline_minute_60'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, comment='股票代码')
    datetime = Column(DateTime, nullable=False, comment='日期时间')
    open = Column(DECIMAL(10, 3), nullable=False, comment='开盘价')
    high = Column(DECIMAL(10, 3), nullable=False, comment='最高价')
    low = Column(DECIMAL(10, 3), nullable=False, comment='最低价')
    close = Column(DECIMAL(10, 3), nullable=False, comment='收盘价')
    volume = Column(BigInteger, nullable=False, default=0, comment='成交量(手)')
    amount = Column(DECIMAL(20, 2), nullable=False, default=0, comment='成交额(元)')
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')
    
    def __repr__(self):
        return f"<KlineMinute60(code={self.code}, datetime={self.datetime})>"





# ========================================
# 3. 技术指标模型
# ========================================

class TechnicalIndicator(Base):
    """技术指标模型"""
    __tablename__ = 'technical_indicators'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, comment='股票代码')
    trade_date = Column(Date, nullable=False, comment='交易日期')
    period_type = Column(String(10), nullable=False, comment='周期类型')
    
    # 均线指标
    ma5 = Column(DECIMAL(10, 3), comment='5日均线')
    ma10 = Column(DECIMAL(10, 3), comment='10日均线')
    ma20 = Column(DECIMAL(10, 3), comment='20日均线')
    ma60 = Column(DECIMAL(10, 3), comment='60日均线')
    
    # MACD
    macd = Column(DECIMAL(10, 3), comment='MACD')
    macd_signal = Column(DECIMAL(10, 3), comment='MACD信号线')
    macd_hist = Column(DECIMAL(10, 3), comment='MACD柱')
    
    # KDJ
    k_value = Column(DECIMAL(10, 3), comment='K值')
    d_value = Column(DECIMAL(10, 3), comment='D值')
    j_value = Column(DECIMAL(10, 3), comment='J值')
    
    # RSI
    rsi6 = Column(DECIMAL(10, 3), comment='RSI6')
    rsi12 = Column(DECIMAL(10, 3), comment='RSI12')
    rsi24 = Column(DECIMAL(10, 3), comment='RSI24')
    
    # 布林带
    boll_upper = Column(DECIMAL(10, 3), comment='布林上轨')
    boll_middle = Column(DECIMAL(10, 3), comment='布林中轨')
    boll_lower = Column(DECIMAL(10, 3), comment='布林下轨')
    
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')
    
    def __repr__(self):
        return f"<TechnicalIndicator(code={self.code}, date={self.trade_date})>"


# ========================================
# 4. 资金流向模型
# ========================================

class MoneyFlow(Base):
    """资金流向模型"""
    __tablename__ = 'money_flow'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, comment='股票代码')
    trade_date = Column(Date, nullable=False, comment='交易日期')
    
    # 主力资金
    main_net_inflow = Column(DECIMAL(20, 2), comment='主力净流入(元)')
    main_net_inflow_pct = Column(DECIMAL(10, 2), comment='主力净流入占比(%)')
    
    # 超大单
    super_large_net_inflow = Column(DECIMAL(20, 2), comment='超大单净流入')
    super_large_net_inflow_pct = Column(DECIMAL(10, 2), comment='超大单净流入占比(%)')
    
    # 大单
    large_net_inflow = Column(DECIMAL(20, 2), comment='大单净流入')
    large_net_inflow_pct = Column(DECIMAL(10, 2), comment='大单净流入占比(%)')
    
    # 中单
    medium_net_inflow = Column(DECIMAL(20, 2), comment='中单净流入')
    medium_net_inflow_pct = Column(DECIMAL(10, 2), comment='中单净流入占比(%)')
    
    # 小单
    small_net_inflow = Column(DECIMAL(20, 2), comment='小单净流入')
    small_net_inflow_pct = Column(DECIMAL(10, 2), comment='小单净流入占比(%)')
    
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')
    
    def __repr__(self):
        return f"<MoneyFlow(code={self.code}, date={self.trade_date})>"


# ========================================
# 5. 板块数据模型
# ========================================

class SectorKlineDaily(Base):
    """板块日线数据模型"""
    __tablename__ = 'sector_kline_daily'

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment='主键ID')
    code = Column(String(10), nullable=False, comment='板块代码')
    trade_date = Column(Date, nullable=False, comment='交易日期')
    change_pct = Column(DECIMAL(10, 2), comment='涨跌幅(%)')
    stock_count = Column(Integer, comment='成分股数量')
    rise_count = Column(Integer, comment='上涨股票数')
    fall_count = Column(Integer, comment='下跌股票数')
    flat_count = Column(Integer, comment='平盘股票数')
    limit_up_count = Column(Integer, comment='涨停股票数')
    limit_down_count = Column(Integer, comment='跌停股票数')
    open = Column(DECIMAL(10, 2), comment='开盘价')
    high = Column(DECIMAL(10, 2), comment='最高价')
    low = Column(DECIMAL(10, 2), comment='最低价')
    close = Column(DECIMAL(10, 2), comment='收盘价')
    total_amount = Column(DECIMAL(20, 2), comment='总成交额')
    total_volume = Column(BigInteger, comment='总成交量')
    created_at = Column(DateTime, comment='创建时间')

    def __repr__(self):
        return f"<SectorKlineDaily(code={self.code}, date={self.trade_date})>"


# ========================================
# 6. 系统配置模型
# ========================================

class DataUpdateLog(Base):
    """数据更新记录模型"""
    __tablename__ = 'data_update_log'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    data_type = Column(String(50), nullable=False, comment='数据类型')
    code = Column(String(10), comment='股票代码')
    update_time = Column(DateTime, default=get_current_time, comment='更新时间')
    record_count = Column(Integer, comment='更新记录数')
    status = Column(String(20), comment='状态')
    error_message = Column(Text, comment='错误信息')
    
    def __repr__(self):
        return f"<DataUpdateLog(type={self.data_type}, time={self.update_time})>"


class UserStock(Base):
    """自选股配置模型"""
    __tablename__ = 'user_stocks'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, comment='用户ID')
    code = Column(String(10), nullable=False, comment='股票代码')
    stock_name = Column(String(50), comment='股票名称')
    group_name = Column(String(50), default='default', comment='分组名称')
    is_holding = Column(Boolean, default=False, comment='是否持仓')
    position_shares = Column(Integer, comment='持仓股数')
    position_cost = Column(DECIMAL(10, 3), comment='持仓成本')
    notes = Column(Text, comment='备注')
    sort_order = Column(Integer, default=0, comment='排序')
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')
    updated_at = Column(DateTime, default=get_current_time, onupdate=get_current_time, comment='更新时间')
    
    def __repr__(self):
        return f"<UserStock(user={self.user_id}, code={self.code})>"


# ========================================
# 7. 市场情绪数据模型
# ========================================

class StrategyPoolRun(Base):
    __tablename__ = 'strategy_pool_runs'

    id = Column(BigInteger, primary_key=True, autoincrement=True, default=generate_int_id)
    strategy_name = Column(String(100), nullable=False, default='main_force_accumulation', comment='策略名称')
    trade_date = Column(Date, nullable=False, comment='信号交易日')
    run_type = Column(String(20), nullable=False, default='official', comment='运行类型')
    param_version = Column(String(50), nullable=False, default='v1', comment='参数版本')
    total_scanned = Column(Integer, default=0, comment='扫描总数')
    total_candidates = Column(Integer, default=0, comment='入池数量')
    notes = Column(Text, comment='备注')
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')


class StrategyCandidate(Base):
    __tablename__ = 'strategy_candidates'

    id = Column(BigInteger, primary_key=True, autoincrement=True, default=generate_int_id)
    strategy_name = Column(String(100), nullable=False, default='main_force_accumulation', comment='策略名称')
    code = Column(String(10), nullable=False, comment='股票代码')
    stock_name = Column(String(50), comment='股票名称')
    first_detected_date = Column(Date, nullable=False, comment='首次入池日期')
    last_detected_date = Column(Date, nullable=False, comment='最近识别日期')
    current_trade_date = Column(Date, nullable=False, comment='当前状态交易日')
    current_state = Column(String(60), nullable=False, comment='当前状态')
    alert_level = Column(String(20), comment='提醒级别')
    is_active = Column(Boolean, default=True, comment='是否活跃')
    box_top = Column(DECIMAL(10, 3), comment='箱体上沿')
    box_bottom = Column(DECIMAL(10, 3), comment='箱体下沿')
    last_close = Column(DECIMAL(10, 3), comment='最新收盘价')
    trigger_price = Column(DECIMAL(10, 3), comment='触发价')
    trigger_reason = Column(Text, comment='触发原因')
    param_version = Column(String(50), nullable=False, default='v1', comment='参数版本')
    notes = Column(Text, comment='备注')
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')
    updated_at = Column(DateTime, default=get_current_time, onupdate=get_current_time, comment='更新时间')


class StrategyStateHistory(Base):
    __tablename__ = 'strategy_state_history'

    id = Column(BigInteger, primary_key=True, autoincrement=True, default=generate_int_id)
    strategy_name = Column(String(100), nullable=False, default='main_force_accumulation', comment='策略名称')
    candidate_id = Column(BigInteger, comment='候选池ID')
    code = Column(String(10), nullable=False, comment='股票代码')
    stock_name = Column(String(50), comment='股票名称')
    trade_date = Column(Date, nullable=False, comment='交易日')
    state = Column(String(60), nullable=False, comment='状态')
    alert_level = Column(String(20), comment='提醒级别')
    is_confirmed = Column(Boolean, default=False, comment='是否确认')
    close_price = Column(DECIMAL(10, 3), comment='收盘价')
    trigger_price = Column(DECIMAL(10, 3), comment='触发价')
    box_top = Column(DECIMAL(10, 3), comment='箱体上沿')
    box_bottom = Column(DECIMAL(10, 3), comment='箱体下沿')
    trigger_reason = Column(Text, comment='触发原因')
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')


class StrategyAlert(Base):
    __tablename__ = 'strategy_alerts'

    id = Column(BigInteger, primary_key=True, autoincrement=True, default=generate_int_id)
    strategy_name = Column(String(100), nullable=False, default='main_force_accumulation', comment='策略名称')
    candidate_id = Column(BigInteger, comment='候选池ID')
    code = Column(String(10), nullable=False, comment='股票代码')
    stock_name = Column(String(50), comment='股票名称')
    trade_date = Column(Date, nullable=False, comment='交易日')
    state = Column(String(60), nullable=False, comment='状态')
    alert_level = Column(String(20), nullable=False, comment='提醒级别')
    channel = Column(String(20), nullable=False, default='system', comment='渠道')
    title = Column(String(200), comment='标题')
    message = Column(Text, comment='内容')
    trigger_price = Column(DECIMAL(10, 3), comment='触发价')
    box_top = Column(DECIMAL(10, 3), comment='箱体上沿')
    box_bottom = Column(DECIMAL(10, 3), comment='箱体下沿')
    is_sent = Column(Boolean, default=False, comment='是否已发送')
    sent_at = Column(DateTime, comment='发送时间')
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')


class StrategyBacktestRun(Base):
    __tablename__ = 'strategy_backtest_runs'

    id = Column(BigInteger, primary_key=True, autoincrement=True, default=generate_int_id)
    strategy_name = Column(String(100), nullable=False, default='main_force_accumulation', comment='策略名称')
    task_id = Column(String(100), nullable=False, comment='任务ID')
    start_date = Column(Date, comment='开始日期')
    end_date = Column(Date, comment='结束日期')
    status = Column(String(20), nullable=False, default='running', comment='状态')
    summary_json = Column(Text, comment='摘要JSON')
    error_message = Column(Text, comment='错误信息')
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')
    completed_at = Column(DateTime, comment='完成时间')


class SelectorRun(Base):
    __tablename__ = 'selector_runs'

    id = Column(BigInteger, primary_key=True, autoincrement=True, default=generate_int_id)
    selector_name = Column(String(100), nullable=False, default='main_rise_build_up', comment='选择器名称')
    run_type = Column(String(30), nullable=False, comment='Run type: realtime/history')
    status = Column(String(20), nullable=False, default='completed', comment='状态')
    params_json = Column(Text, comment='参数JSON')
    summary_json = Column(Text, comment='Summary JSON')
    total_items = Column(Integer, default=0, comment='主表条数')
    total_signals = Column(Integer, default=0, comment='信号条数')
    notes = Column(Text, comment='澶囨敞')
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')
    updated_at = Column(DateTime, default=get_current_time, onupdate=get_current_time, comment='更新时间')


class SelectorRunRecord(Base):
    __tablename__ = 'selector_run_records'

    id = Column(BigInteger, primary_key=True, autoincrement=True, default=generate_int_id)
    run_id = Column(BigInteger, nullable=False, comment='selector_runs.id')
    record_type = Column(String(30), nullable=False, comment='realtime_item/history_item/signal_event')
    code = Column(String(10), comment='股票代码')
    stock_name = Column(String(50), comment='股票名称')
    signal_date = Column(Date, comment='信号日期')
    payload_json = Column(Text, comment='璁板綍JSON')
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')


class EmotionCycle(Base):
    """情绪周期模型"""
    __tablename__ = 'emotion_cycle'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, comment='交易日期')
    close_up_rate = Column(DECIMAL(10, 2), comment='收盘上涨率（%）')
    intraday_up_rate = Column(DECIMAL(10, 2), comment='盘中上涨率（%）')
    sh_up_rate = Column(DECIMAL(10, 2), comment='上证上涨率（%）')
    sz_up_rate = Column(DECIMAL(10, 2), comment='深证上涨率（%）')
    gem_up_rate = Column(DECIMAL(10, 2), comment='创业板上涨率（%）')
    cyb_up_rate = Column(DECIMAL(10, 2), comment='创业板上涨率（%）')
    strong_up_rate = Column(DECIMAL(10, 2), comment='强势上涨率（%）')
    limit_up_follow_rate = Column(DECIMAL(10, 2), comment='涨停溢价率（%）')
    weak_up_rate = Column(DECIMAL(10, 2), comment='弱势上涨率（%）')
    yesterday_monster_up_rate = Column(DECIMAL(10, 2), comment='昨日妖股上涨率（%）')
    yesterday_strong_up_rate = Column(DECIMAL(10, 2), comment='昨日强势股上涨率（%）')
    yesterday_weak_up_rate = Column(DECIMAL(10, 2), comment='昨日弱势股上涨率（%）')
    total_stocks = Column(Integer, comment='统计股票数')
    is_confirmed = Column(Integer, default=0, comment='数据是否已确认：0未确认，1已确认')
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')
    updated_at = Column(DateTime, default=get_current_time, onupdate=get_current_time, comment='更新时间')
    
    def __repr__(self):
        return f"<EmotionCycle(date={self.date}, close_up={self.close_up_rate}%)>"


class SentimentData(Base):
    """市场情绪数据模型"""
    __tablename__ = 'sentiment_data'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, comment='交易日期')
    up_count = Column(Integer, comment='上涨家数')
    down_count = Column(Integer, comment='下跌家数')
    unchanged_count = Column(Integer, comment='平盘家数')
    avg_change_percent = Column(DECIMAL(10, 2), comment='平均涨跌幅（%）')
    total_turnover_amount = Column(DECIMAL(20, 2), comment='沪深两市总成交额（亿元）')
    daily_turnover_change = Column(DECIMAL(10, 2), comment='日成交额环比变化（%）')
    up_5_percent_count = Column(Integer, comment='涨幅5%以上家数')
    down_5_percent_count = Column(Integer, comment='跌幅5%以上家数')
    limit_up_count = Column(Integer, comment='涨停家数')
    limit_down_count = Column(Integer, comment='跌停家数')
    sentiment_score = Column(DECIMAL(10, 2), comment='综合情绪指数（0-100）')
    sentiment_level = Column(String(10), comment='情绪等级')
    consecutive_2_limit = Column(Integer, default=0, comment='2连板家数')
    consecutive_3_limit = Column(Integer, default=0, comment='3连板家数')
    consecutive_4_limit = Column(Integer, default=0, comment='4连板家数')
    consecutive_5_plus_limit = Column(Integer, default=0, comment='5连板及以上家数')
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')
    updated_at = Column(DateTime, default=get_current_time, onupdate=get_current_time, comment='更新时间')
    
    def __repr__(self):
        return f"<SentimentData(date={self.date}, score={self.sentiment_score}, level={self.sentiment_level})>"


# ========================================
# 8. 数据巡检结果模型
# ========================================

class KlineCheckResult(Base):
    """K线完整性检查结果模型"""
    __tablename__ = 'kline_check_results'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    check_batch_id = Column(String(32), nullable=False, comment='检查批次ID')
    check_time = Column(DateTime, default=get_current_time, comment='检查时间')
    data_type = Column(String(20), nullable=False, comment='数据类型：stock/index/sector')
    period = Column(String(10), nullable=False, comment='K线周期：1d/60min/30min/15min/5min/1min')
    time_mode = Column(String(20), nullable=False, comment='时间模式：full/recent')
    
    # 统计信息
    total_checked = Column(Integer, default=0, comment='检查总数')
    complete_count = Column(Integer, default=0, comment='完整数量')
    incomplete_count = Column(Integer, default=0, comment='不完整数量')
    error_count = Column(Integer, default=0, comment='错误数量')
    
    # 详细结果（JSON格式存储）
    details = Column(Text, comment='详细检查结果JSON')
    
    # 状态
    status = Column(String(20), default='completed', comment='检查状态：running/completed/failed')
    error_message = Column(Text, comment='错误信息')
    
    created_at = Column(DateTime, default=get_current_time, comment='创建时间')
    
    def __repr__(self):
        return f"<KlineCheckResult(batch={self.check_batch_id}, type={self.data_type}, period={self.period})>"
