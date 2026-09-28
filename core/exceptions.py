"""
异常定义模块

定义系统中所有自定义异常类，便于错误处理和调试
"""


class AiStockException(Exception):
    """AiStock 基础异常类"""
    
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


class ConfigException(AiStockException):
    """配置相关异常"""
    pass


class DataSourceException(AiStockException):
    """数据源相关异常"""
    
    def __init__(self, source_name: str, message: str, details: dict = None):
        """
        初始化数据源异常
        
        Args:
            source_name: 数据源名称
            message: 异常消息
            details: 异常详细信息
        """
        self.source_name = source_name
        super().__init__(message, details)
    
    def __str__(self):
        if self.details:
            return f"[{self.source_name}] {self.message} | Details: {self.details}"
        return f"[{self.source_name}] {self.message}"


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


class DatabaseException(AiStockException):
    """数据库相关异常"""
    pass


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
