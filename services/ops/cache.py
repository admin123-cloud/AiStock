"""
缓存服务

提供本地缓存功能，支持内存缓存和文件缓存
"""

import json
import pickle
import time
import threading
from typing import Dict, Any, Optional, Union, List
from datetime import datetime, timedelta
import os
from pathlib import Path

from utils.logger import get_logger
from utils.config import ConfigManager
from utils.paths import cache_path

# 创建配置管理器实例
config = ConfigManager()

logger = get_logger("CacheService")


class CacheService:
    """
    缓存服务
    """
    
    def __init__(self):
        """初始化缓存服务"""
        self.memory_cache = {}
        self.memory_cache_lock = threading.Lock()
        self.cache_dir = cache_path()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.default_ttl = 3600  # 默认缓存时间1小时
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        获取缓存值        
        Args:
            key: 缓存键            default: 默认值        
        Returns:
            缓存值或默认值        """
        # 先从内存缓存获取
        with self.memory_cache_lock:
            if key in self.memory_cache:
                cache_data = self.memory_cache[key]
                if not self._is_expired(cache_data):
                    return cache_data["value"]
                else:
                    del self.memory_cache[key]
        
        # 再从文件缓存获取
        file_path = self._get_cache_file_path(key)
        if file_path.exists():
            try:
                with open(file_path, 'rb') as f:
                    cache_data = pickle.load(f)
                if not self._is_expired(cache_data):
                    # 加载到内存缓存
                    with self.memory_cache_lock:
                        self.memory_cache[key] = cache_data
                    return cache_data["value"]
                else:
                    # 删除过期的文件缓存
                    file_path.unlink()
            except Exception as e:
                logger.warning(f"读取文件缓存失败: {e}")
        
        return default
    
    def set(self, key: str, value: Any, ttl: int = None) -> bool:
        """
        设置缓存值        
        Args:
            key: 缓存键            value: 缓存值            ttl: 缓存时间（秒）        
        Returns:
            是否设置成功
        """
        try:
            ttl = ttl or self.default_ttl
            cache_data = {
                "value": value,
                "expire_time": time.time() + ttl
            }
            
            # 存入内存缓存
            with self.memory_cache_lock:
                self.memory_cache[key] = cache_data
            
            # 存入文件缓存
            file_path = self._get_cache_file_path(key)
            try:
                with open(file_path, 'wb') as f:
                    pickle.dump(cache_data, f)
            except Exception as e:
                logger.warning(f"写入文件缓存失败: {e}")
            
            return True
        except Exception as e:
            logger.error(f"设置缓存失败: {e}")
            return False
    
    def delete(self, key: str) -> bool:
        """
        删除缓存
        
        Args:
            key: 缓存键        
        Returns:
            是否删除成功
        """
        try:
            # 从内存缓存删除
            with self.memory_cache_lock:
                if key in self.memory_cache:
                    del self.memory_cache[key]
            
            # 从文件缓存删除
            file_path = self._get_cache_file_path(key)
            if file_path.exists():
                file_path.unlink()
            
            return True
        except Exception as e:
            logger.error(f"删除缓存失败: {e}")
            return False
    
    def clear(self) -> bool:
        """
        清空所有缓存        
        Returns:
            是否清空成功
        """
        try:
            # 清空内存缓存
            with self.memory_cache_lock:
                self.memory_cache.clear()
            
            # 清空文件缓存
            for file_path in self.cache_dir.glob("*.cache"):
                try:
                    file_path.unlink()
                except Exception as e:
                    logger.warning(f"删除缓存文件失败: {e}")
            
            return True
        except Exception as e:
            logger.error(f"清空缓存失败: {e}")
            return False
    
    def get_cache_size(self) -> Dict[str, int]:
        """
        获取缓存大小
        
        Returns:
            缓存大小信息
        """
        with self.memory_cache_lock:
            memory_size = len(self.memory_cache)
        
        file_size = len(list(self.cache_dir.glob("*.cache")))
        
        return {
            "memory": memory_size,
            "file": file_size
        }
    
    def _is_expired(self, cache_data: Dict) -> bool:
        """
        检查缓存是否过期        
        Args:
            cache_data: 缓存数据
        
        Returns:
            是否过期
        """
        expire_time = cache_data.get("expire_time", 0)
        return time.time() > expire_time
    
    def _get_cache_file_path(self, key: str) -> Path:
        """
        获取缓存文件路径
        
        Args:
            key: 缓存键        
        Returns:
            缓存文件路径
        """
        # 生成安全的文件名
        safe_key = key.replace("/", "_").replace("\\", "_").replace(":", "_")
        return self.cache_dir / f"{safe_key}.cache"
    
    def get_stock_quote_cache(self, stock_code: str) -> Optional[Dict]:
        """
        获取股票行情缓存
        
        Args:
            stock_code: 股票代码
        
        Returns:
            行情数据或None
        """
        key = f"stock_quote:{stock_code}"
        return self.get(key)
    
    def set_stock_quote_cache(self, stock_code: str, quote: Dict, ttl: int = 60) -> bool:
        """
        设置股票行情缓存
        
        Args:
            stock_code: 股票代码
            quote: 行情数据
            ttl: 缓存时间（秒），默认60秒        
        Returns:
            是否设置成功
        """
        key = f"stock_quote:{stock_code}"
        return self.set(key, quote, ttl)
    
    def get_stock_history_cache(self, stock_code: str, period: str, start_date: str, end_date: str) -> Optional[Any]:
        """
        获取股票历史数据缓存
        
        Args:
            stock_code: 股票代码
            period: 周期
            start_date: 开始日期
            end_date: 结束日期
        
        Returns:
            历史数据或None
        """
        key = f"stock_history:{stock_code}:{period}:{start_date}:{end_date}"
        return self.get(key)
    
    def set_stock_history_cache(self, stock_code: str, period: str, start_date: str, end_date: str, data: Any, ttl: int = 3600) -> bool:
        """
        设置股票历史数据缓存
        
        Args:
            stock_code: 股票代码
            period: 周期
            start_date: 开始日期
            end_date: 结束日期
            data: 历史数据
            ttl: 缓存时间（秒），默认1小时
        
        Returns:
            是否设置成功
        """
        key = f"stock_history:{stock_code}:{period}:{start_date}:{end_date}"
        return self.set(key, data, ttl)
    
    def get_stock_list_cache(self, market: str = "SH", stock_type: str = "stock") -> Optional[List[Dict]]:
        """
        获取股票列表缓存
        
        Args:
            market: 市场
            stock_type: 股票类型
        
        Returns:
            股票列表或None
        """
        key = f"stock_list:{market}:{stock_type}"
        return self.get(key)
    
    def set_stock_list_cache(self, market: str, stock_type: str, stocks: List[Dict], ttl: int = 86400) -> bool:
        """
        设置股票列表缓存
        
        Args:
            market: 市场
            stock_type: 股票类型
            stocks: 股票列表
            ttl: 缓存时间（秒），默认1天        
        Returns:
            是否设置成功
        """
        key = f"stock_list:{market}:{stock_type}"
        return self.set(key, stocks, ttl)


# 全局缓存服务实例
cache_service = CacheService()
