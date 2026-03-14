"""
数据库模型定义
使用SQLAlchemy ORM定义数据表结构
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, Date, Enum, Index, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base
import enum


class Stock(Base):
    """股票基础信息表"""
    __tablename__ = "stock"
    
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(10), unique=True, index=True, comment="股票代码 如600000")
    name = Column(String(50), index=True, comment="股票名称")
    market = Column(String(2), comment="所属市场：SH(上海)、SZ(深圳)")
    industry = Column(String(20), comment="所属行业")
    is_active = Column(Integer, default=1, comment="是否在交易中：1激活，0已停牌")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # 关系
    klines = relationship("KlineData", back_populates="stock", cascade="all, delete-orphan")
    
    __table_args__ = (
        Index('ix_stock_code_market', 'code', 'market'),
    )


class KlineData(Base):
    """K线数据表"""
    __tablename__ = "kline_data"
    
    id = Column(Integer, primary_key=True, index=True)
    stock_id = Column(Integer, ForeignKey('stock.id', ondelete='CASCADE'), index=True)
    code = Column(String(10), index=True, comment="股票代码")
    date = Column(Date, index=True, comment="交易日期")
    period = Column(String(2), comment="周期：D(日)、W(周)、M(月)")
    open_price = Column(Float, comment="开盘价")
    high_price = Column(Float, comment="最高价")
    low_price = Column(Float, comment="最低价")
    close_price = Column(Float, comment="收盘价")
    volume = Column(Integer, comment="成交量（手）")
    amount = Column(Float, comment="成交额（元）")
    change_percent = Column(Float, comment="涨跌幅（%）")
    turnover_rate = Column(Float, comment="成交量比（%）", nullable=True)
    
    # 主要技术指标（在K线表中缓存）
    ma5 = Column(Float, comment="5日均线", nullable=True)
    ma10 = Column(Float, comment="10日均线", nullable=True)
    ma20 = Column(Float, comment="20日均线", nullable=True)
    ma50 = Column(Float, comment="50日均线", nullable=True)
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # 关系
    stock = relationship("Stock", back_populates="klines")
    
    __table_args__ = (
        Index('ix_kline_code_date_period', 'code', 'date', 'period'),
        Index('ix_kline_date', 'date'),
    )


class SentimentData(Base):
    """大盘情绪数据表"""
    __tablename__ = "sentiment_data"
    
    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, unique=True, index=True, comment="交易日期")
    
    # 基础情绪指标
    up_count = Column(Integer, comment="上涨家数")
    down_count = Column(Integer, comment="下跌家数")
    unchanged_count = Column(Integer, comment="平盘家数", nullable=True)
    
    # 涨跌幅指标
    avg_change_percent = Column(Float, comment="平均涨跌幅（%）")
    total_turnover_amount = Column(Float, comment="沪深两市总成交额（亿元）")
    daily_turnover_change = Column(Float, comment="日成交额环比变化（%）", nullable=True)
    
    # 特殊涨跌统计
    up_5_percent_count = Column(Integer, comment="涨幅5%以上家数")
    down_5_percent_count = Column(Integer, comment="跌幅5%以上家数")
    limit_up_count = Column(Integer, comment="涨停家数")
    limit_down_count = Column(Integer, comment="跌停家数")
    
    # 综合情绪评分（0-100）
    sentiment_score = Column(Float, comment="综合情绪指数（0-100）", nullable=True)
    sentiment_level = Column(String(10), comment="情绪等级：极度悲观/悲观/中性/乐观/极度乐观", nullable=True)
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    __table_args__ = (
        Index('ix_sentiment_date', 'date'),
    )


class BacktestResult(Base):
    """回测结果表"""
    __tablename__ = "backtest_result"
    
    id = Column(Integer, primary_key=True, index=True)
    strategy_name = Column(String(50), index=True, comment="策略名称")
    start_date = Column(Date, comment="回测开始日期")
    end_date = Column(Date, comment="回测结束日期")
    
    # 交易统计
    total_trades = Column(Integer, comment="总交易次数")
    winning_trades = Column(Integer, comment="盈利交易数")
    losing_trades = Column(Integer, comment="亏损交易数")
    
    # 收益指标
    total_return = Column(Float, comment="总收益率（%）")
    annual_return = Column(Float, comment="年化收益率（%）")
    max_drawdown = Column(Float, comment="最大回撤（%）")
    sharpe_ratio = Column(Float, comment="夏普比率")
    
    # 风险指标
    win_rate = Column(Float, comment="胜率（%）")
    profit_factor = Column(Float, comment="利润因子")
    
    # 详细信息
    initial_capital = Column(Float, comment="初始资金（元）")
    final_capital = Column(Float, comment="最终资金（元）")
    max_capital = Column(Float, comment="最大账户净值（元）")
    
    created_at = Column(DateTime, default=datetime.now)
    
    __table_args__ = (
        Index('ix_backtest_strategy_date', 'strategy_name', 'start_date'),
    )


class TechnicalIndicator(Base):
    """技术指标表（缓存计算结果）"""
    __tablename__ = "technical_indicator"
    
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(10), index=True, comment="股票代码")
    date = Column(Date, index=True, comment="交易日期")
    period = Column(String(2), comment="周期")
    
    # MACD
    macd_dif = Column(Float, comment="MACD DIF", nullable=True)
    macd_dea = Column(Float, comment="MACD DEA", nullable=True)
    macd_histogram = Column(Float, comment="MACD直方图", nullable=True)
    
    # RSI
    rsi_6 = Column(Float, comment="RSI(6)", nullable=True)
    rsi_12 = Column(Float, comment="RSI(12)", nullable=True)
    rsi_24 = Column(Float, comment="RSI(24)", nullable=True)
    
    # Bollinger Bands
    bb_upper = Column(Float, comment="布林带上轨", nullable=True)
    bb_middle = Column(Float, comment="布林带中轨", nullable=True)
    bb_lower = Column(Float, comment="布林带下轨", nullable=True)
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    __table_args__ = (
        Index('ix_indicator_code_date_period', 'code', 'date', 'period'),
    )
