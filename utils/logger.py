"""
日志工具模块

使用 loguru 提供统一的日志记录功能
"""

import sys
from pathlib import Path
from typing import Optional
from loguru import logger

from utils.paths import logs_root


def setup_logger(
    log_dir: str = "logs",
    log_level: str = "DEBUG",
    rotation: str = "100 MB",
    retention: str = "7 days",
    compression: str = "zip",
    console_output: bool = True
):
    """
    配置日志系统
    
    Args:
        log_dir: 日志目录（相对于项目根目录）
        log_level: 日志级别
        rotation: 日志轮转大小
        retention: 日志保留时间
        compression: 日志压缩格式
        console_output: 是否输出到控制台
    """
    # 移除默认处理器
    logger.remove()
    
    # 创建日志目录（相对于项目根目录）
    log_path = Path(log_dir)
    if not log_path.is_absolute():
        log_path = logs_root() if log_dir == "logs" else Path(__file__).parent.parent / log_path
    log_path.mkdir(parents=True, exist_ok=True)
    
    # 日志格式
    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    )
    
    # 简洁的控制台格式
    console_format = (
        "<green>{time:HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<level>{message}</level>"
    )
    
    # 控制台输出
    if console_output:
        # 定义控制台过滤器，过滤掉mysql相关的日志
        def console_filter(record):
            """控制台过滤器，过滤掉mysql相关的日志"""
            return record.get("extra", {}).get("name") != "mysql"
        
        logger.add(
            sys.stdout,
            format=console_format,
            level=log_level,
            colorize=True,
            enqueue=False,  # 改为False，确保实时输出
            filter=console_filter
        )
    
    # 所有日志文件
    logger.add(
        log_path / "aistock.log",
        format=log_format,
        level=log_level,
        rotation=rotation,
        retention=retention,
        compression=compression,
        encoding="utf-8",
        enqueue=True
    )
    
    # 错误日志文件（单独记录）
    logger.add(
        log_path / "error.log",
        format=log_format,
        level="ERROR",
        rotation=rotation,
        retention=retention,
        compression=compression,
        encoding="utf-8",
        enqueue=True
    )
    
    logger.info("Logger initialized successfully")


def get_logger(name: str = "AiStock"):
    """
    获取日志记录器
    
    Args:
        name: 日志记录器名称
    
    Returns:
        logger 实例
    """
    return logger.bind(name=name)
