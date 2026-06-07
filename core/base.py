"""
基础类定义模块
"""

from threading import Lock
from typing import Any


class Singleton(type):
    """
    单例模式元类
    
    使用方式：
        class MyClass(metaclass=Singleton):
            pass
    """
    
    _instances = {}
    _lock = Lock()
    
    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            with cls._lock:
                # 双重检查锁定
                if cls not in cls._instances:
                    cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


class BaseEntity:
    """实体基类"""
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            key: value for key, value in self.__dict__.items()
            if not key.startswith('_')
        }
    
    def __repr__(self) -> str:
        class_name = self.__class__.__name__
        attrs = ', '.join(f'{k}={v}' for k, v in self.to_dict().items())
        return f"{class_name}({attrs})"


class DataSourceStatus:
    """数据源状态枚举"""
    
    AVAILABLE = "available"       # 可用
    UNAVAILABLE = "unavailable"   # 不可用
    DEGRADED = "degraded"         # 降级
    DISABLED = "disabled"         # 已禁用
    
    def __init__(self, status: str):
        self.status = status
    
    def __str__(self):
        return self.status
    
    def __eq__(self, other):
        if isinstance(other, DataSourceStatus):
            return self.status == other.status
        if isinstance(other, str):
            return self.status == other
        return False
