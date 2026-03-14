"""
应用配置管理模块
支持环境变量和.env文件配置
"""
import os
from typing import Optional
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """应用全局设置"""
    
    # 数据库配置
    database_url: str = "mysql://root:root@127.0.0.1:3306/stockpy"
    
    # API密钥
    tencent_api_key: Optional[str] = None
    eastmoney_api_key: Optional[str] = None
    sina_api_key: Optional[str] = None
    
    # 定时任务配置
    update_schedule_time: str = "16:00"
    update_timezone: str = "Asia/Shanghai"
    
    # 应用配置
    debug: bool = True
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    
    # 数据源配置
    data_source_type: str = "local"  # local, api, web_scrape
    local_data_path: str = "./data"
    enable_tencent_api: bool = False
    enable_eastmoney_api: bool = False
    
    # WebSocket配置
    ws_enabled: bool = True
    ws_heartbeat_interval: int = 30  # 秒
    
    # 回测配置
    backtest_batch_size: int = 100
    backtest_slippage: float = 0.001  # 滑点
    
    # 技术指标配置
    ma_periods: list = [5, 10, 20, 50, 100, 200]  # 移动平均线周期
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    
    # 情绪指标权重配置
    sentiment_weights: dict = {
        "up_count": 0.2,
        "down_count": 0.2,
        "avg_change": 0.15,
        "turnover_rate": 0.15,
        "up_5_percent": 0.1,
        "down_5_percent": 0.1,
        "limit_up": 0.05,
        "limit_down": 0.05,
    }
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


# 全局配置实例
settings = Settings()
