"""
数据源管理器

统一管理多个数据源，支持故障转移和负载均衡
"""

from typing import Any, Dict, List, Optional, Type
import pandas as pd

from .base_fetcher import BaseDataSource
from .sources import (
    TdxQuantDataSource,
)
from core.base import Singleton
from utils.exceptions import DataSourceException
from utils.logger import get_logger
from utils.config import ConfigManager

# 创建配置管理器实例
config = ConfigManager()

logger = get_logger("DataSourceManager")


class DataSourceManager(metaclass=Singleton):
    """
    数据源管理器
    
    管理多个数据源，按优先级选择可用数据源
    支持故障转移和自动恢复
    """
    
    # 数据源类注册表
    SOURCE_CLASSES: Dict[str, Type[BaseDataSource]] = {
        "tdxquant": TdxQuantDataSource,
    }
    
    def __init__(self):
        """初始化数据源管理器"""
        self._sources: Dict[str, BaseDataSource] = {}
        self._initialized = False
    
    def _ensure_initialized(self):
        """确保数据源已初始化"""
        if self._initialized:
            return

        # 从配置加载数据源
        sources_config = config.get_data_sources_config()

        for name, source_class in self.SOURCE_CLASSES.items():
            source_config = sources_config.get(name, {})
            if source_config.get("enabled", True):
                try:
                    self._sources[name] = source_class(name, source_config)
                    logger.info(f"已加载数据源: {name}")
                except Exception as e:
                    logger.error(f"加载数据源 {name} 失败: {e}")

        self._initialized = True
    
    def get_source(self, name: str) -> Optional[BaseDataSource]:
        """
        获取指定数据源
        
        Args:
            name: 数据源名称
        
        Returns:
            数据源实例
        """
        self._ensure_initialized()
        return self._sources.get(name)
    
    def get_available_sources(self) -> List[BaseDataSource]:
        """
        获取所有可用的数据源（按优先级排序）
        
        Returns:
            可用数据源列表
        """
        self._ensure_initialized()
        
        available = [
            source for source in self._sources.values()
            if source.is_available()
        ]
        
        # 按优先级排序（数字越小优先级越高）
        available.sort(key=lambda s: s.priority)
        return available
    
    def get_primary_source(self) -> Optional[BaseDataSource]:
        """
        获取主数据源（优先级最高的可用数据源）
        
        Returns:
            主数据源
        """
        sources = self.get_available_sources()
        return sources[0] if sources else None
    
    def call_with_failover(
        self,
        method_name: str,
        *args,
        source_name: Optional[str] = None,
        **kwargs
    ) -> Any:
        """
        调用数据源方法，支持故障转移
        
        Args:
            method_name: 方法名
            source_name: 指定数据源（可选）
            *args, **kwargs: 方法参数
        
        Returns:
            方法返回值
        
        Raises:
            DataSourceException: 所有数据源都失败时抛出
        """
        self._ensure_initialized()
        
        # 如果指定了数据源，只使用该数据源
        if source_name:
            source = self.get_source(source_name)
            if not source:
                raise DataSourceException(source_name, f"数据源 {source_name} 不存在")
            
            if not source.is_available():
                raise DataSourceException(source_name, f"数据源 {source_name} 不可用")
            
            method = getattr(source, method_name, None)
            if not method:
                raise DataSourceException(source_name, f"方法 {method_name} 不存在")
            
            return method(*args, **kwargs)
        
        # 否则按优先级尝试所有可用数据源
        errors = []
        available_sources = self.get_available_sources()
        
        # 根据方法类型选择合适的数据源
        method_category = self._get_method_category(method_name)
        
        for source in available_sources:
            # 检查数据源是否支持该方法
            if not hasattr(source, method_name):
                continue
            
            # 检查数据源是否支持该类型的数据
            if not source.supports(method_category):
                continue
            
            try:
                method = getattr(source, method_name)
                result = method(*args, **kwargs)
                logger.debug(f"数据源 {source.name} 调用 {method_name} 成功")
                return result
            
            except Exception as e:
                error_msg = f"数据源 {source.name} 调用 {method_name} 失败: {e}"
                logger.warning(error_msg)
                source.mark_failure(error_msg)
                errors.append((source.name, str(e)))
                continue
        
        # 所有数据源都失败
        error_details = "; ".join(f"{name}: {err}" for name, err in errors)
        raise DataSourceException("all", f"所有数据源都失败: {error_details}")
    
    def _get_method_category(self, method_name: str) -> str:
        """
        获取方法对应的数据源类型
        
        Args:
            method_name: 方法名
        
        Returns:
            数据源类型
        """
        category_map = {
            "get_stock_quote": "stock_quote",
            "get_realtime_quotes": "realtime_quote",
            "get_stock_history": "stock_history",
            "get_stock_info": "stock_info",
            "get_stock_list": "stock_list",
            "get_sector_data": "sector_data"
        }
        return category_map.get(method_name, "unknown")
    
    def get_available_sources(self) -> List[BaseDataSource]:
        """
        获取所有可用的数据源（按优先级排序）
        
        Returns:
            可用数据源列表
        """
        self._ensure_initialized()
        
        available = [
            source for source in self._sources.values()
            if source.is_available()
        ]
        
        # 按优先级排序（数字越小优先级越高）
        available.sort(key=lambda s: s.priority)
        return available
    
    # ========== 便捷方法 ==========
    
    def get_stock_list(
        self,
        market: str = "SH",
        stock_type: str = "stock",
        source_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        获取股票列表
        
        Args:
            market: 市场代码（SH/SZ/BJ）
            stock_type: 类型（stock/index）
            source_name: 指定数据源
        
        Returns:
            股票列表
        """
        result = self.call_with_failover(
            "get_stock_list",
            market=market,
            stock_type=stock_type,
            source_name=source_name
        )
        return result or []
    
    def get_all_stocks(self, source_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取所有股票

        Args:
            source_name: 指定数据源

        Returns:
            所有股票列表
        """
        # 使用tdxquant数据源获取所有股票
        source = self.get_source(source_name or "tdxquant")
        if source and hasattr(source, "get_all_stocks"):
            return source.get_all_stocks()

        # 备用方案：使用get_stock_list获取所有A股
        try:
            # 使用market_type=5表示所有A股
            stocks = self.call_with_failover(
                "get_stock_list",
                market_type=5,
                source_name=source_name
            )
            return stocks
        except Exception as e:
            logger.error(f"获取所有股票失败: {e}")
            return []

    def get_all_indices(self, source_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取所有指数

        Args:
            source_name: 指定数据源

        Returns:
            所有指数列表
        """
        # 使用tdxquant数据源获取所有指数
        source = self.get_source(source_name or "tdxquant")
        if source and hasattr(source, "get_all_indices"):
            return source.get_all_indices()

        # 备用方案：使用get_stock_list获取重点指数
        try:
            # 使用market_type=9表示重点指数
            indices = self.call_with_failover(
                "get_stock_list",
                market_type=9,
                source_name=source_name
            )
            return indices
        except Exception as e:
            logger.error(f"获取所有指数失败: {e}")
            return []
    
    def get_stock_quote(
        self,
        stock_code: str,
        source_name: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """获取股票实时行情"""
        from services.cache import cache_service
        # 先从缓存获取
        cached_quote = cache_service.get_stock_quote_cache(stock_code)
        if cached_quote:
            logger.debug(f"从缓存获取 {stock_code} 行情数据")
            return cached_quote
        
        # 从数据源获取
        quote = self.call_with_failover(
            "get_stock_quote",
            stock_code,
            source_name=source_name
        )
        
        # 存入缓存
        if quote:
            cache_service.set_stock_quote_cache(stock_code, quote)
        
        return quote
    
    def get_realtime_quotes(
        self,
        stock_codes: List[str],
        source_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """批量获取实时行情"""
        result = self.call_with_failover(
            "get_realtime_quotes",
            stock_codes,
            source_name=source_name
        )
        return result or []
    
    def get_stock_history(
        self,
        stock_code: str,
        start_date: str,
        end_date: str,
        period: str = "daily",
        source_name: Optional[str] = None
    ) -> Optional[pd.DataFrame]:
        """获取股票历史数据"""
        from services.cache import cache_service
        # 先从缓存获取
        cached_data = cache_service.get_stock_history_cache(stock_code, period, start_date, end_date)
        if cached_data is not None:
            logger.debug(f"从缓存获取 {stock_code} {period} 历史数据")
            return cached_data
        
        # 从数据源获取
        data = self.call_with_failover(
            "get_stock_history",
            stock_code,
            start_date,
            end_date,
            period=period,
            source_name=source_name
        )
        
        # 存入缓存
        if data is not None:
            cache_service.set_stock_history_cache(stock_code, period, start_date, end_date, data)
        
        return data
    
    def get_source_status(self) -> Dict[str, Dict[str, Any]]:
        """
        获取所有数据源状态
        
        Returns:
            数据源状态信息
        """
        self._ensure_initialized()
        
        return {
            name: source.get_status_info()
            for name, source in self._sources.items()
        }
    
    def health_check(self) -> Dict[str, bool]:
        """
        健康检查所有数据源
        
        Returns:
            各数据源健康状态
        """
        self._ensure_initialized()
        
        results = {}
        for name, source in self._sources.items():
            try:
                results[name] = source.health_check()
            except Exception as e:
                logger.error(f"数据源 {name} 健康检查失败: {e}")
                results[name] = False
        
        return results
