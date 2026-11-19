from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_ROOT = Path(r"F:\Aistock\runtime")
DEFAULT_STORAGE_ROOT = Path(r"F:\Aistock\storage")
DEFAULT_DATA_ROOT = DEFAULT_STORAGE_ROOT
DEFAULT_REPORTS_ROOT = DEFAULT_STORAGE_ROOT / "reports"
DEFAULT_ARTIFACTS_ROOT = DEFAULT_STORAGE_ROOT / "artifacts"
DEFAULT_LOGS_ROOT = DEFAULT_STORAGE_ROOT / "logs"


def _load_paths_config() -> dict[str, Any]:
    config_path = PROJECT_ROOT / "config" / "settings.local.yaml"
    if not config_path.exists():
        config_path = PROJECT_ROOT / "config" / "settings.yaml"
    try:
        with config_path.open("r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except Exception:
        return {}
    paths = config.get("paths", {})
    return paths if isinstance(paths, dict) else {}


def _path_from_config(name: str, env_name: str, default: Path) -> Path:
    env_value = os.getenv(env_name)
    if env_value:
        return Path(env_value)
    value = _load_paths_config().get(name)
    if value and not str(value).startswith("${"):
        return Path(value)
    return default


def data_root() -> Path:
    return _path_from_config("data_root", "AISTOCK_DATA_ROOT", DEFAULT_DATA_ROOT)


def runtime_root() -> Path:
    env_runtime = os.getenv("AISTOCK_RUNTIME_ROOT")
    if env_runtime:
        return Path(env_runtime)
    # 若显式通过 AISTOCK_DATA_ROOT 覆盖（如 Docker 容器内 /aistock/data 或测试隔离 tmp_path），
    # 沿用 <AISTOCK_DATA_ROOT>/runtime 保持 100% 向后兼容。
    env_data = os.getenv("AISTOCK_DATA_ROOT")
    if env_data:
        return Path(env_data) / "runtime"
    cfg_runtime = _load_paths_config().get("runtime_root")
    if cfg_runtime and not str(cfg_runtime).startswith("${"):
        return Path(cfg_runtime)
    return DEFAULT_RUNTIME_ROOT


def reports_root() -> Path:
    return _path_from_config("reports_root", "AISTOCK_REPORTS_ROOT", DEFAULT_REPORTS_ROOT)


def artifacts_root() -> Path:
    return _path_from_config("artifacts_root", "AISTOCK_ARTIFACTS_ROOT", DEFAULT_ARTIFACTS_ROOT)


def logs_root() -> Path:
    return _path_from_config("logs_root", "AISTOCK_LOGS_ROOT", DEFAULT_LOGS_ROOT)


def data_path(*parts: str) -> Path:
    if parts and parts[0] == "runtime":
        return runtime_path(*parts[1:])
    return data_root().joinpath(*parts)


def runtime_path(*parts: str) -> Path:
    return runtime_root().joinpath(*parts)


def cache_path(*parts: str) -> Path:
    return data_path("cache", *parts)


def warehouse_path(*parts: str) -> Path:
    return data_path("warehouse", *parts)


def report_path(*parts: str) -> Path:
    return reports_root().joinpath(*parts)


DATA_ROOT = data_root()
RUNTIME_ROOT = runtime_root()
REPORTS_ROOT = reports_root()
ARTIFACTS_ROOT = artifacts_root()
LOGS_ROOT = logs_root()
