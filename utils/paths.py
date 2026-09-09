from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = Path(r"F:\Stock\AiStockData\data")
DEFAULT_REPORTS_ROOT = Path(r"F:\Stock\AiStockResearchArchive\reports")
DEFAULT_ARTIFACTS_ROOT = Path(r"F:\Stock\AiStockData\artifacts")
DEFAULT_LOGS_ROOT = Path(r"F:\Stock\AiStockData\logs")


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
    if isinstance(value, str):
        placeholder = re.fullmatch(r"\$\{([^:}]+)(?::(.*))?\}", value)
        if placeholder:
            value = os.getenv(placeholder.group(1), placeholder.group(2))
    return Path(value) if value else default


def data_root() -> Path:
    return _path_from_config("data_root", "AISTOCK_DATA_ROOT", DEFAULT_DATA_ROOT)


def reports_root() -> Path:
    return _path_from_config("reports_root", "AISTOCK_REPORTS_ROOT", DEFAULT_REPORTS_ROOT)


def artifacts_root() -> Path:
    return _path_from_config("artifacts_root", "AISTOCK_ARTIFACTS_ROOT", DEFAULT_ARTIFACTS_ROOT)


def logs_root() -> Path:
    return _path_from_config("logs_root", "AISTOCK_LOGS_ROOT", DEFAULT_LOGS_ROOT)


def data_path(*parts: str) -> Path:
    return data_root().joinpath(*parts)


def runtime_path(*parts: str) -> Path:
    return data_path("runtime", *parts)


def cache_path(*parts: str) -> Path:
    return data_path("cache", *parts)


def warehouse_path(*parts: str) -> Path:
    return data_path("warehouse", *parts)


def report_path(*parts: str) -> Path:
    return reports_root().joinpath(*parts)


DATA_ROOT = data_root()
REPORTS_ROOT = reports_root()
ARTIFACTS_ROOT = artifacts_root()
LOGS_ROOT = logs_root()
