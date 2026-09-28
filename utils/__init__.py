"""
工具模块

提供项目中的通用工具函数和类，包括：
- 日志管理
- 配置管理
- 数据库工具
- 通用助手函数
- 装饰器
- 自定义异常
"""

from .logger import setup_logger, get_logger
from .config import config
from .database import DatabaseManager
from .helpers import (
    format_number,
    validate_stock_code,
    is_trading_day,
    calculate_change,
    format_stock_code,
    detect_market,
    parse_market_code,
    is_trading_time,
)
from .decorators import (
    retry,
    timer,
    log_execution,
    validate_args,
    cache_result,
    rate_limit,
    async_retry,
    async_timer,
    singleton,
    deprecated,
    catch_exceptions,
)
from .exceptions import (
    AiStockException,
    DataSourceException,
    DataSourceError,
    DataFetchError,
    DataParseError,
    DatabaseException,
    DatabaseError,
    ConnectionError,
    QueryError,
    ModelException,
    ModelError,
    ModelTrainingError,
    ModelPredictionError,
    StrategyError,
    BacktestError,
    ExecutionError,
    OrderError,
    RiskControlError,
    ConfigException,
    ConfigError,
    StockNotFoundError,
    InvalidStockCodeError,
    DataValidationError,
    TransactionError,
)

__all__ = [
    # Logger
    'setup_logger',
    'get_logger',
    
    # Config
    'config',
    
    # Database
    'DatabaseManager',
    
    # Helpers
    'format_number',
    'validate_stock_code',
    'is_trading_day',
    'calculate_change',
    'format_stock_code',
    'detect_market',
    'parse_market_code',
    'is_trading_time',
    
    # Decorators
    'retry',
    'timer',
    'log_execution',
    'validate_args',
    'cache_result',
    'rate_limit',
    'async_retry',
    'async_timer',
    'singleton',
    'deprecated',
    'catch_exceptions',
    
    # Exceptions
    'AiStockException',
    'DataSourceException',
    'DataSourceError',
    'DataFetchError',
    'DataParseError',
    'DatabaseException',
    'DatabaseError',
    'ConnectionError',
    'QueryError',
    'ModelException',
    'ModelError',
    'ModelTrainingError',
    'ModelPredictionError',
    'StrategyError',
    'BacktestError',
    'ExecutionError',
    'OrderError',
    'RiskControlError',
    'ConfigException',
    'ConfigError',
    'StockNotFoundError',
    'InvalidStockCodeError',
    'DataValidationError',
    'TransactionError',
]