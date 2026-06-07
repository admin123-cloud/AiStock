"""
装饰器模块

提供常用的装饰器，包括重试、计时、缓存等
"""

import time
import functools
from typing import Callable, Any, Optional
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


def retry(max_retries: int = 3, delay: float = 1.0, exceptions: tuple = (Exception,)):
    """
    重试装饰器
    
    Args:
        max_retries: 最大重试次数
        delay: 重试延迟（秒）
        exceptions: 需要重试的异常类型
    
    Returns:
        装饰器函数
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        logger.warning(f"Attempt {attempt + 1} failed for {func.__name__}: {e}. Retrying in {delay}s...")
                        time.sleep(delay)
                    else:
                        logger.error(f"All {max_retries} attempts failed for {func.__name__}")
            
            raise last_exception
        return wrapper
    return decorator


def timer(func: Callable = None, name: Optional[str] = None) -> Callable:
    """
    计时装饰器
    
    Args:
        func: 被装饰的函数
        name: 自定义名称
    
    Returns:
        装饰器函数或包装函数
    """
    def decorator(f: Callable) -> Callable:
        @functools.wraps(f)
        def wrapper(*args, **kwargs) -> Any:
            start_time = time.time()
            result = f(*args, **kwargs)
            end_time = time.time()
            elapsed_time = end_time - start_time
            func_name = name or f.__name__
            logger.info(f"{func_name} executed in {elapsed_time:.4f} seconds")
            return result
        return wrapper
    
    if func is not None:
        return decorator(func)
    return decorator


def log_execution(func: Callable = None, level: str = "info") -> Callable:
    """
    执行日志装饰器
    
    Args:
        func: 被装饰的函数
        level: 日志级别
    
    Returns:
        装饰器函数或包装函数
    """
    def decorator(f: Callable) -> Callable:
        @functools.wraps(f)
        def wrapper(*args, **kwargs) -> Any:
            log_func = getattr(logger, level.lower(), logger.info)
            log_func(f"Executing {f.__name__} with args={args}, kwargs={kwargs}")
            try:
                result = f(*args, **kwargs)
                log_func(f"{f.__name__} completed successfully")
                return result
            except Exception as e:
                logger.error(f"{f.__name__} failed with error: {e}")
                raise
        return wrapper
    
    if func is not None:
        return decorator(func)
    return decorator


def validate_args(*validators: Callable):
    """
    参数验证装饰器
    
    Args:
        validators: 验证函数列表
    
    Returns:
        装饰器函数
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            for validator in validators:
                validator(*args, **kwargs)
            return func(*args, **kwargs)
        return wrapper
    return decorator


def cache_result(ttl: int = 3600):
    """
    结果缓存装饰器
    
    Args:
        ttl: 缓存时间（秒）
    
    Returns:
        装饰器函数
    """
    def decorator(func: Callable) -> Callable:
        cache = {}
        
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            cache_key = (args, frozenset(kwargs.items()))
            current_time = time.time()
            
            if cache_key in cache:
                result, timestamp = cache[cache_key]
                if current_time - timestamp < ttl:
                    logger.debug(f"Cache hit for {func.__name__}")
                    return result
            
            result = func(*args, **kwargs)
            cache[cache_key] = (result, current_time)
            logger.debug(f"Cache miss for {func.__name__}, result cached")
            return result
        
        return wrapper
    return decorator


def rate_limit(max_calls: int = 10, period: float = 60.0):
    """
    速率限制装饰器
    
    Args:
        max_calls: 最大调用次数
        period: 时间周期（秒）
    
    Returns:
        装饰器函数
    """
    def decorator(func: Callable) -> Callable:
        calls = []
        
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            current_time = time.time()
            
            # 清理过期的调用记录
            calls[:] = [t for t in calls if current_time - t < period]
            
            if len(calls) >= max_calls:
                logger.warning(f"Rate limit exceeded for {func.__name__}")
                raise Exception(f"Rate limit exceeded: {max_calls} calls per {period} seconds")
            
            calls.append(current_time)
            return func(*args, **kwargs)
        
        return wrapper
    return decorator


def async_retry(max_retries: int = 3, delay: float = 1.0, exceptions: tuple = (Exception,)):
    """
    异步重试装饰器
    
    Args:
        max_retries: 最大重试次数
        delay: 重试延迟（秒）
        exceptions: 需要重试的异常类型
    
    Returns:
        装饰器函数
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        logger.warning(f"Async attempt {attempt + 1} failed for {func.__name__}: {e}. Retrying in {delay}s...")
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"All {max_retries} async attempts failed for {func.__name__}")
            
            raise last_exception
        return wrapper
    return decorator


def async_timer(func: Callable = None, name: Optional[str] = None) -> Callable:
    """
    异步计时装饰器
    
    Args:
        func: 被装饰的函数
        name: 自定义名称
    
    Returns:
        装饰器函数或包装函数
    """
    def decorator(f: Callable) -> Callable:
        @functools.wraps(f)
        async def wrapper(*args, **kwargs) -> Any:
            start_time = time.time()
            result = await f(*args, **kwargs)
            end_time = time.time()
            elapsed_time = end_time - start_time
            func_name = name or f.__name__
            logger.info(f"Async {func_name} executed in {elapsed_time:.4f} seconds")
            return result
        return wrapper
    
    if func is not None:
        return decorator(func)
    return decorator


def singleton(cls):
    """
    单例装饰器
    
    Args:
        cls: 被装饰的类
    
    Returns:
        单例类
    """
    instances = {}
    
    @functools.wraps(cls)
    def get_instance(*args, **kwargs):
        if cls not in instances:
            instances[cls] = cls(*args, **kwargs)
        return instances[cls]
    
    return get_instance


def deprecated(func: Callable = None, message: str = "This function is deprecated") -> Callable:
    """
    弃用装饰器
    
    Args:
        func: 被装饰的函数
        message: 弃用消息
    
    Returns:
        装饰器函数或包装函数
    """
    def decorator(f: Callable) -> Callable:
        @functools.wraps(f)
        def wrapper(*args, **kwargs) -> Any:
            logger.warning(f"DEPRECATED: {f.__name__} - {message}")
            return f(*args, **kwargs)
        return wrapper
    
    if func is not None:
        return decorator(func)
    return decorator


def catch_exceptions(default_return: Any = None, exceptions: tuple = (Exception,)):
    """
    异常捕获装饰器
    
    Args:
        default_return: 异常时的默认返回值
        exceptions: 需要捕获的异常类型
    
    Returns:
        装饰器函数
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            try:
                return func(*args, **kwargs)
            except exceptions as e:
                logger.error(f"Exception caught in {func.__name__}: {e}")
                return default_return
        return wrapper
    return decorator