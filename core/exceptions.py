"""
自定义异常模块

定义项目中使用的自定义异常类
"""


# ========================================
# 基础异常类
# ========================================

class AiStockException(Exception):
    """AiStock基础异常类"""
    
    def __init__(self, message: str, details: dict = None):
        """
        初始化异常
        
        Args:
            message: 异常消息
            details: 异常详细信息
        """
        self.message = message
        self.details = details or {}
        super().__init__(self.message)
    
    def __str__(self):
        if self.details:
            return f"{self.message} | Details: {self.details}"
        return self.message


# ========================================
# 配置相关异常
# ========================================

class ConfigException(AiStockException):
    """配置相关异常"""
    pass


class ConfigError(AiStockException):
    """配置错误"""
    pass


# ========================================
# 数据源相关异常
# ========================================

class DataSourceException(AiStockException):
    """数据源相关异常（兼容旧代码）"""
    
    def __init__(self, source_name: str = None, message: str = None, details: dict = None):
        """
        初始化数据源异常
        
        Args:
            source_name: 数据源名称
            message: 异常消息
            details: 异常详细信息
        """
        self.source_name = source_name
        # 兼容两种调用方式
        if message is None and source_name is not None:
            message = source_name
            self.source_name = None
        super().__init__(message or "数据源异常", details)
    
    def __str__(self):
        if self.source_name:
            return f"[{self.source_name}] {super().__str__()}"
        return super().__str__()


class DataSourceError(AiStockException):
    """数据源异常"""
    pass


class DataFetchError(DataSourceError):
    """数据获取异常"""
    pass


class DataParseError(DataSourceError):
    """数据解析异常"""
    pass


class DataValidationError(DataSourceError):
    """数据验证异常"""
    pass


# ========================================
# 数据库相关异常
# ========================================

class DatabaseException(AiStockException):
    """数据库相关异常（兼容旧代码）"""
    pass


class DatabaseError(AiStockException):
    """数据库异常"""
    pass


class ConnectionError(DatabaseError):
    """连接异常"""
    pass


class QueryError(DatabaseError):
    """查询异常"""
    pass


class TransactionError(DatabaseError):
    """事务异常"""
    pass


# ========================================
# 模型相关异常
# ========================================

class ModelException(AiStockException):
    """模型相关异常（兼容旧代码）"""
    pass


class ModelError(AiStockException):
    """模型异常"""
    pass


class ModelTrainingError(ModelError):
    """模型训练异常"""
    pass


class ModelPredictionError(ModelError):
    """模型预测异常"""
    pass


# ========================================
# 股票相关异常
# ========================================

class StockNotFoundError(AiStockException):
    """股票不存在异常"""
    pass


class InvalidStockCodeError(AiStockException):
    """无效股票代码异常"""
    pass


# ========================================
# 策略相关异常
# ========================================

class StrategyError(AiStockException):
    """策略异常"""
    pass


class BacktestError(AiStockException):
    """回测异常"""
    pass


# ========================================
# 执行相关异常
# ========================================

class ExecutionError(AiStockException):
    """执行异常"""
    pass


class OrderError(ExecutionError):
    """订单异常"""
    pass


class RiskControlError(ExecutionError):
    """风控异常"""
    pass


class DataSourceNotAvailableException(DataSourceException):
    """数据源不可用异常"""
    pass

class DataNotFoundException(AiStockException):
    """数据未找到异常"""
    
    def __init__(self, data_type: str, query: str, details: dict = None):
        """
        初始化数据未找到异常
        
        Args:
            data_type: 数据类型
            query: 查询条件
            details: 异常详细信息
        """
        self.data_type = data_type
        self.query = query
        super().__init__(
            f"Data not found: {data_type} with query '{query}'",
            details
        )

class ValidationException(AiStockException):
    """数据验证异常"""
    pass

class RateLimitException(AiStockException):
    """速率限制异常"""
    
    def __init__(self, source_name: str, retry_after: float = None):
        """
        初始化速率限制异常
        
        Args:
            source_name: 数据源名称
            retry_after: 重试等待时间（秒）
        """
        self.source_name = source_name
        self.retry_after = retry_after
        message = f"Rate limit exceeded for {source_name}"
        if retry_after:
            message += f", retry after {retry_after} seconds"
        super().__init__(message)
