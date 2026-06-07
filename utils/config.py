"""
配置管理模块

统一管理项目配置，支持 YAML 格式配置文件
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional
import yaml
from utils.exceptions import ConfigException


class ConfigManager:
    """
    配置管理器
    
    单例模式，统一管理所有配置
    """
    
    _instance = None
    _lock = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """初始化配置管理器"""
        if self._initialized:
            return
        
        self._initialized = True
        self._config_cache = {}
        
        # 自动查找配置目录
        self._config_dir = self._find_config_dir()
        self._env_file = self._config_dir.parent / ".env"
        
        # 加载环境变量
        self._load_env()
    
    def _find_config_dir(self) -> Path:
        """
        自动查找配置目录
        
        从当前文件所在目录开始向上查找，直到找到包含 config 目录的位置
        
        Returns:
            配置目录路径
        """
        # 获取当前文件所在目录
        current_file = Path(__file__).resolve()
        current_dir = current_file.parent
        
        # 向上查找，最多查找5层
        for _ in range(5):
            # 检查当前目录下是否有 config 目录
            config_dir = current_dir / "config"
            if config_dir.exists() and config_dir.is_dir():
                return config_dir
            
            # 检查 backend 子目录下是否有 config 目录
            backend_config_dir = current_dir / "backend" / "config"
            if backend_config_dir.exists() and backend_config_dir.is_dir():
                return backend_config_dir
            
            # 向上一级目录
            parent_dir = current_dir.parent
            if parent_dir == current_dir:  # 到达根目录
                break
            current_dir = parent_dir
        
        # 如果找不到，使用默认路径（相对于当前工作目录）
        return Path("config")
    
    def _load_env(self):
        """加载环境变量"""
        try:
            from dotenv import load_dotenv
            load_dotenv(self._env_file)
        except ImportError:
            pass  # 如果没有安装 python-dotenv，跳过
    
    def load_yaml(self, filename: str, use_cache: bool = True) -> Dict[str, Any]:
        """
        加载 YAML 配置文件
        
        Args:
            filename: 配置文件名（不含路径）
            use_cache: 是否使用缓存
        
        Returns:
            配置字典
        """
        # 检查缓存
        if use_cache and filename in self._config_cache:
            return self._config_cache[filename]
        
        # 构建配置文件路径
        config_path = self._config_dir / filename
        
        if not config_path.exists():
            raise ConfigException(
                f"Config file not found: {config_path}",
                {"path": str(config_path)}
            )
        
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
                
                # 处理环境变量替换
                config = self._process_env_vars(config)
                
                # 缓存配置
                if use_cache:
                    self._config_cache[filename] = config
                
                return config
        except yaml.YAMLError as e:
            raise ConfigException(
                f"Failed to parse config file: {filename}",
                {"error": str(e)}
            )
    
    def _process_env_vars(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        处理配置中的环境变量
        
        支持 ${ENV_VAR} 和 ${ENV_VAR:default} 格式
        """
        if isinstance(config, dict):
            return {k: self._process_env_vars(v) for k, v in config.items()}
        elif isinstance(config, list):
            return [self._process_env_vars(item) for item in config]
        elif isinstance(config, str):
            # 匹配 ${VAR} 或 ${VAR:default}
            import re
            pattern = r'\$\{([^}:]+)(?::([^}]*))?\}'
            matches = re.findall(pattern, config)
            
            for var_name, default_value in matches:
                env_value = os.getenv(var_name, default_value)
                if env_value:
                    config = config.replace(f'${{{var_name}:{default_value}}}', env_value)
                    config = config.replace(f'${{{var_name}}}', env_value)
            
            return config
        else:
            return config
    
    def get(self, key: str, default: Any = None, config_file: str = "settings.yaml") -> Any:
        """
        获取配置值（支持点分隔的键名）
        
        Args:
            key: 配置键名（如 "database.path"）
            default: 默认值
            config_file: 配置文件名
        
        Returns:
            配置值
        """
        config = self.load_yaml(config_file)
        
        # 分割键名
        keys = key.split('.')
        value = config
        
        try:
            for k in keys:
                value = value[k]
            return value
        except (KeyError, TypeError):
            return default
    
    def get_database_config(self) -> Dict[str, Any]:
        """获取数据库配置"""
        return self.load_yaml("settings.yaml").get("database", {})
    
    def get_data_sources_config(self) -> Dict[str, Any]:
        """获取数据源配置"""
        return self.load_yaml("datasources.yaml").get("data_sources", {})
    
    def get_failover_config(self) -> Dict[str, Any]:
        """获取故障转移配置"""
        return self.load_yaml("datasources.yaml").get("failover", {})
    
    def reload(self, filename: Optional[str] = None):
        """
        重新加载配置
        
        Args:
            filename: 指定配置文件名，None 表示重新加载所有配置
        """
        if filename:
            if filename in self._config_cache:
                del self._config_cache[filename]
        else:
            self._config_cache.clear()
    
    def set_config_dir(self, path: str):
        """
        设置配置目录
        
        Args:
            path: 配置目录路径
        """
        self._config_dir = Path(path)
        self._config_cache.clear()


# 全局配置管理器实例
config = ConfigManager()
