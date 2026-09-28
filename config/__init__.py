"""
配置管理模块

提供项目配置管理功能，统一从 YAML 文件读取配置
"""

import yaml
import os
from pathlib import Path
from typing import Any, Dict, Optional


class Config:
    """配置管理类"""

    _instance = None
    _config: Dict[str, Any] = {}
    _data_sources: Dict[str, Any] = {}
    _config_dir: Path = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not self._config:
            self.load_config()

    def set_config_dir(self, config_dir: str):
        """设置配置目录"""
        self._config_dir = Path(config_dir)
        self.load_config()

    def load_config(self, config_file: str = "settings.yaml") -> Dict[str, Any]:
        """
        加载 YAML 配置文件

        Args:
            config_file: 配置文件名

        Returns:
            配置字典
        """
        if self._config_dir is None:
            # 默认使用当前文件所在目录
            self._config_dir = Path(__file__).parent

        # 加载主配置
        config_path = self._config_dir / config_file
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                self._config = yaml.safe_load(f) or {}

        # 加载数据源配置（包含密钥）
        datasources_path = self._config_dir / "datasources.yaml"
        if datasources_path.exists():
            with open(datasources_path, 'r', encoding='utf-8') as f:
                self._data_sources = yaml.safe_load(f) or {}

        return self._config

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置项，支持点号分隔的嵌套键

        Args:
            key: 配置键，如 "database.host"
            default: 默认值

        Returns:
            配置值
        """
        keys = key.split('.')
        value = self._config

        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default

        return value

    def get_data_source_config(self, source_name: str) -> Dict[str, Any]:
        """
        获取指定数据源的完整配置（包含密钥）

        Args:
            source_name: 数据源名称，如 "tushare"

        Returns:
            数据源配置字典
        """
        sources = self._data_sources.get('data_sources', {})
        return sources.get(source_name, {})

    def get_data_source_auth(self, source_name: str) -> Dict[str, Any]:
        """
        获取指定数据源的认证信息（密钥）

        Args:
            source_name: 数据源名称，如 "tushare"

        Returns:
            认证配置字典
        """
        config = self.get_data_source_config(source_name)
        return config.get('auth', {})

    def get_data_source_token(self, source_name: str) -> Optional[str]:
        """
        获取指定数据源的 API Token

        Args:
            source_name: 数据源名称

        Returns:
            API Token 字符串，如果不存在返回 None
        """
        auth = self.get_data_source_auth(source_name)
        return auth.get('token') or auth.get('api_key')

    def get_all_data_sources(self) -> Dict[str, Any]:
        """
        获取所有数据源配置

        Returns:
            所有数据源配置字典
        """
        return self._data_sources.get('data_sources', {})
    
    def get_data_sources_config(self) -> Dict[str, Any]:
        """
        获取数据源配置

        Returns:
            数据源配置字典
        """
        return self._data_sources.get('data_sources', {})

    def get_enabled_data_sources(self) -> Dict[str, Any]:
        """
        获取所有启用的数据源配置

        Returns:
            启用的数据源配置字典
        """
        all_sources = self.get_all_data_sources()
        return {name: config for name, config in all_sources.items() if config.get('enabled', False)}

    def get_service_config(self, service_name: str) -> Dict[str, Any]:
        """
        获取其他服务配置（邮件、通知等）

        Args:
            service_name: 服务名称，如 "email", "cloud", "notification"

        Returns:
            服务配置字典
        """
        return self._data_sources.get(service_name, {})

    @property
    def app(self) -> Dict[str, Any]:
        """应用配置"""
        return self._config.get('app', {})

    @property
    def api(self) -> Dict[str, Any]:
        """API 配置"""
        return self._config.get('api', {})

    @property
    def database(self) -> Dict[str, Any]:
        """数据库配置"""
        return self._config.get('database', {})

    @property
    def redis(self) -> Dict[str, Any]:
        """Redis 配置"""
        return self._config.get('redis', {})

    @property
    def logging(self) -> Dict[str, Any]:
        """日志配置"""
        return self._config.get('logging', {})

    @property
    def cache(self) -> Dict[str, Any]:
        """缓存配置"""
        return self._config.get('cache', {})

    @property
    def proxy(self) -> Dict[str, Any]:
        """代理配置"""
        return self._config.get('proxy', {})

    @property
    def data_sources(self) -> Dict[str, Any]:
        """数据源配置"""
        return self._data_sources.get('data_sources', {})

    @property
    def failover(self) -> Dict[str, Any]:
        """故障转移配置"""
        return self._data_sources.get('failover', {})

    @property
    def health_check(self) -> Dict[str, Any]:
        """健康检查配置"""
        return self._data_sources.get('health_check', {})

    @property
    def cloud(self) -> Dict[str, Any]:
        """云服务配置"""
        return self._data_sources.get('cloud', {})

    @property
    def email(self) -> Dict[str, Any]:
        """邮件服务配置"""
        return self._data_sources.get('email', {})

    @property
    def notification(self) -> Dict[str, Any]:
        """通知服务配置"""
        return self._data_sources.get('notification', {})

    @property
    def services(self) -> Dict[str, Any]:
        """其他第三方服务配置"""
        return self._data_sources.get('services', {})
    
    def get_data_sources_config(self) -> Dict[str, Any]:
        """获取数据源配置"""
        return self._data_sources.get('data_sources', {})

    @property
    def database_url(self) -> str:
        """数据库连接 URL"""
        db = self.database
        db_type = str(db.get('type', 'clickhouse')).strip().lower()
        if db_type != 'clickhouse':
            raise RuntimeError(f"Hard-cut mode: only ClickHouse is allowed, current database.type={db_type}")
        user = db.get('username', 'default')
        password = db.get('password', '')
        host = db.get('host', '127.0.0.1')
        port = db.get('port', 8123)
        database = db.get('database', 'stock')
        return f"clickhousedb://{user}:{password}@{host}:{port}/{database}"

    @property
    def redis_url(self) -> str:
        """Redis 连接 URL"""
        redis = self.redis
        host = redis.get('host', 'localhost')
        port = redis.get('port', 6379)
        db = redis.get('db', 0)
        password = redis.get('password', '')

        if password:
            return f"redis://:{password}@{host}:{port}/{db}"
        return f"redis://{host}:{port}/{db}"


# 创建全局配置实例
config = Config()


def get_config() -> Config:
    """获取配置实例"""
    return config


# 为了兼容性，保留 settings 变量名
settings = config


__all__ = [
    'Config',
    'config',
    'get_config',
    'settings',
]
