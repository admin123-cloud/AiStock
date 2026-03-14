"""
数据验证模型（Pydantic）
用于API请求/响应验证
"""
from pydantic import BaseModel
from datetime import datetime, date
from typing import Optional, List


# ======================== Stock Models ========================

class StockBase(BaseModel):
    code: str
    name: str
    market: str
    industry: Optional[str] = None


class StockCreate(StockBase):
    pass


class StockResponse(StockBase):
    id: int
    is_active: int
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


# ======================== KlineData Models ========================

class KlineDataBase(BaseModel):
    code: str
    date: date
    period: str
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: int
    amount: float
    change_percent: float


class KlineDataCreate(KlineDataBase):
    stock_id: Optional[int] = None


class KlineDataResponse(KlineDataBase):
    id: int
    stock: Optional[StockResponse] = None
    ma5: Optional[float] = None
    ma10: Optional[float] = None
    ma20: Optional[float] = None
    ma50: Optional[float] = None
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


# ======================== SentimentData Models ========================

class SentimentDataBase(BaseModel):
    date: date
    up_count: int
    down_count: int
    avg_change_percent: float
    total_turnover_amount: float
    up_5_percent_count: int
    down_5_percent_count: int
    limit_up_count: int
    limit_down_count: int


class SentimentDataCreate(SentimentDataBase):
    unchanged_count: Optional[int] = None
    daily_turnover_change: Optional[float] = None
    sentiment_score: Optional[float] = None
    sentiment_level: Optional[str] = None


class SentimentDataResponse(SentimentDataBase):
    id: int
    sentiment_score: Optional[float] = None
    sentiment_level: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


# ======================== BacktestResult Models ========================

class BacktestResultBase(BaseModel):
    strategy_name: str
    start_date: date
    end_date: date
    total_trades: int
    winning_trades: int
    losing_trades: int
    total_return: float
    annual_return: float
    max_drawdown: float


class BacktestResultCreate(BacktestResultBase):
    sharpe_ratio: float
    win_rate: float
    profit_factor: float
    initial_capital: float
    final_capital: float
    max_capital: float


class BacktestResultResponse(BacktestResultBase):
    id: int
    sharpe_ratio: float
    win_rate: float
    profit_factor: float
    initial_capital: float
    final_capital: float
    max_capital: float
    created_at: datetime
    
    class Config:
        from_attributes = True


# ======================== TechnicalIndicator Models ========================

class TechnicalIndicatorBase(BaseModel):
    code: str
    date: date
    period: str


class TechnicalIndicatorCreate(TechnicalIndicatorBase):
    macd_dif: Optional[float] = None
    macd_dea: Optional[float] = None
    macd_histogram: Optional[float] = None
    rsi_6: Optional[float] = None
    rsi_12: Optional[float] = None
    rsi_24: Optional[float] = None
    bb_upper: Optional[float] = None
    bb_middle: Optional[float] = None
    bb_lower: Optional[float] = None


class TechnicalIndicatorResponse(TechnicalIndicatorCreate):
    id: int
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


# ======================== Batch Models ========================

class KlineDataBatch(BaseModel):
    """批量K线数据"""
    klines: List[KlineDataCreate]


class SentimentBatch(BaseModel):
    """批量情绪数据"""
    sentiments: List[SentimentDataCreate]


# ======================== Query Models ========================

class KlineQuery(BaseModel):
    """K线查询参数"""
    code: str
    period: str = "D"
    limit: int = 100
    start_date: Optional[date] = None
    end_date: Optional[date] = None


class SentimentQuery(BaseModel):
    """情绪数据查询参数"""
    days: int = 30
    start_date: Optional[date] = None
    end_date: Optional[date] = None
