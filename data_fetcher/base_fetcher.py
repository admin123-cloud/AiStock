"""
数据源基类

定义所有数据源必须实现的接口
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional
import pandas as pd
from core.base import DataSourceStatus
from utils.exceptions import DataSourceException
from utils.logger import get_logger

logger = get_logger("BaseDataSource")


class BaseDataSource(ABC):
    """
    数据源基类
    
    所有数据源必须继承此类并实现所有抽象方法
    """
    
    def __init__(self, name: str, config: Dict[str, Any]):
        """
        初始化数据源
        
        Args:
            name: 数据源名称
            config: 数据源配置
        """
        self.name = name
        self.config = config
        self.enabled = config.get("enabled", True)
        self.priority = config.get("priority", 999)
        self.timeout = config.get("timeout", 10)
        self.max_retries = config.get("max_retries", 3)
        self.retry_delay = config.get("retry_delay", 1)
        self.headers = config.get("headers", {})
        self.supported_types = config.get("supported_types", [])
        
        # 状态管理
        self.status = DataSourceStatus.AVAILABLE
        self.failure_count = 0
        self.last_success_time = None
        self.last_failure_time = None
        self.last_error_message = None
    
    @abstractmethod
    def get_stock_quote(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """
        获取股票实时行情
        
        Args:
            stock_code: 股票代码
        
        Returns:
            行情数据字典，包含：
            - code: 股票代码
            - name: 股票名称
            - price: 当前价格
            - open: 开盘价
            - high: 最高价
            - low: 最低价
            - pre_close: 昨收价
            - volume: 成交量
            - amount: 成交额
            - change: 涨跌额
            - change_pct: 涨跌幅
            - time: 更新时间
        """
        pass
    
    @abstractmethod
    def get_stock_history(
        self,
        stock_code: str,
        start_date: str,
        end_date: str,
        period: str = "daily"
    ) -> Optional[pd.DataFrame]:
        """
        获取股票历史数据
        
        Args:
            stock_code: 股票代码
            start_date: 开始日期（YYYY-MM-DD）
            end_date: 结束日期（YYYY-MM-DD）
            period: 周期（daily, weekly, monthly, 1min, 5min, 15min, 30min, 60min）
        
        Returns:
            历史数据 DataFrame，包含列：
            - date: 日期
            - open: 开盘价
            - high: 最高价
            - low: 最低价
            - close: 收盘价
            - volume: 成交量
            - amount: 成交额
        """
        pass
    
    @abstractmethod
    def get_stock_info(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """
        获取股票基本信息
        
        Args:
            stock_code: 股票代码
        
        Returns:
            股票信息字典，包含：
            - code: 股票代码
            - name: 股票名称
            - industry: 所属行业
            - sector: 所属板块
            - list_date: 上市日期
            - total_share: 总股本
            - float_share: 流通股本
        """
        pass
    
    @abstractmethod
    def get_stock_list(
        self,
        market: str = "SH",
        stock_type: str = "stock"
    ) -> Optional[List[Dict[str, Any]]]:
        """
        批量获取股票列表（基本信息）
        
        Args:
            market: 市场代码（SH/SZ/BJ）
            stock_type: 股票类型（stock/index/fund）
        
        Returns:
            股票列表，每个元素包含：
            - code: 股票代码
            - name: 股票名称
            - market: 市场
            - type: 类型
            - industry: 所属行业
            - sector: 所属板块
        """
        pass
    
    @abstractmethod
    def get_realtime_quotes(self, stock_codes: List[str]) -> Optional[List[Dict[str, Any]]]:
        """
        批量获取实时行情
        
        Args:
            stock_codes: 股票代码列表
        
        Returns:
            行情数据列表
        """
        pass
    
    @abstractmethod
    def get_sector_data(self, sector_code: str = None) -> Optional[Any]:
        """
        获取板块数据

        Args:
            sector_code: 板块代码（可选）

        Returns:
            板块数据字典或列表
        """
        pass
    
    @abstractmethod
    def health_check(self) -> bool:
        """
        健康检查
        
        Returns:
            数据源是否健康
        """
        pass
    
    def supports(self, data_type: str) -> bool:
        """
        检查是否支持某种数据类型
        
        Args:
            data_type: 数据类型
        
        Returns:
            是否支持
        """
        return data_type in self.supported_types
    
    def mark_success(self):
        """标记成功"""
        self.failure_count = 0
        self.last_success_time = datetime.now()
        self.status = DataSourceStatus.AVAILABLE
        self.last_error_message = None
        logger.debug(f"数据源 {self.name} 操作成功")
    
    def mark_failure(self, error_message: str):
        """
        标记失败
        
        Args:
            error_message: 错误消息
        """
        self.failure_count += 1
        self.last_failure_time = datetime.now()
        self.last_error_message = error_message
        
        # 检查是否需要禁用
        failover_config = self.config.get("failover", {})
        max_failures = failover_config.get("max_failures", 3)
        
        if self.failure_count >= max_failures:
            self.status = DataSourceStatus.UNAVAILABLE
            logger.warning(f"数据源 {self.name} 连续失败 {self.failure_count} 次，已禁用")
    
    def is_available(self) -> bool:
        """
        检查数据源是否可用
        
        Returns:
            是否可用
        """
        if not self.enabled:
            return False
        
        if self.status == DataSourceStatus.DISABLED:
            return False
        
        if self.status == DataSourceStatus.UNAVAILABLE:
            # 检查冷却时间
            failover_config = self.config.get("failover", {})
            cooldown_time = failover_config.get("cooldown_time", 300)
            
            if self.last_failure_time:
                elapsed = (datetime.now() - self.last_failure_time).total_seconds()
                if elapsed < cooldown_time:
                    return False
                else:
                    # 冷却时间结束，尝试恢复
                    self.status = DataSourceStatus.DEGRADED
                    logger.info(f"数据源 {self.name} 冷却时间结束，尝试恢复")
                    return True
        
        return True
    
    def get_health_score(self) -> int:
        """
        获取数据源健康评分
        
        Returns:
            健康评分（0-100）
        """
        score = 100
        
        # 失败次数扣分
        if self.failure_count > 0:
            score -= min(self.failure_count * 10, 50)
        
        # 最近失败扣分
        if self.last_failure_time:
            elapsed = (datetime.now() - self.last_failure_time).total_seconds()
            if elapsed < 3600:  # 1小时内
                score -= min(int((3600 - elapsed) / 360), 30)
        
        # 状态扣分
        if self.status == DataSourceStatus.DEGRADED:
            score -= 20
        elif self.status == DataSourceStatus.UNAVAILABLE:
            score = 0
        
        return max(score, 0)
    
    def get_status_info(self) -> Dict[str, Any]:
        """
        获取数据源状态信息
        
        Returns:
            状态信息字典
        """
        return {
            "name": self.name,
            "enabled": self.enabled,
            "priority": self.priority,
            "status": str(self.status),
            "failure_count": self.failure_count,
            "last_success_time": self.last_success_time.isoformat() if self.last_success_time else None,
            "last_failure_time": self.last_failure_time.isoformat() if self.last_failure_time else None,
            "last_error_message": self.last_error_message,
            "supported_types": self.supported_types,
        }
    
    def __str__(self):
        return f"{self.__class__.__name__}(name={self.name}, priority={self.priority}, status={self.status})"
    
    def __repr__(self):
        return self.__str__()
