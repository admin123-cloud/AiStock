from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
import threading
import time
from datetime import date, datetime
from enum import Enum
from pathlib import Path
import re
from typing import Any, Optional

import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from data_fetcher.sources.tdxquant_pool import tdxquant_pool


app = FastAPI(title="AiStock TDX Gateway", version="1.0.0")
PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_MARKET_DATA_RESPONSE_FIELDS = ("Open", "High", "Low", "Close", "Volume", "Amount")
GATEWAY_TASK_NAME = "AiStock TDX Gateway"


class MarketDataRequest(BaseModel):
    field_list: list[str] = Field(default_factory=list)
    stock_list: list[str] = Field(default_factory=list)
    period: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    count: int = -1
    dividend_type: str = "none"
    fill_data: bool = True


class QuotesRequest(BaseModel):
    stock_codes: list[str]


class StockListRequest(BaseModel):
    market: str = "ALL"
    stock_type: str = "stock"
    list_type: int = 1


class RefreshCacheRequest(BaseModel):
    force: bool = False
    market: str = ""


class RefreshKlineRequest(BaseModel):
    stock_list: list[str]
    period: str


class GbInfoRequest(BaseModel):
    stock_code: str
    date_list: list[str] = Field(default_factory=list)
    count: int = -1


class GbInfoByDateRequest(BaseModel):
    stock_code: str
    start_date: str
    end_date: str


def _jsonable(value: Any) -> Any:
    if isinstance(value, pd.DataFrame):
        frame = value.copy()
        frame.index = frame.index.map(str)
        return {
            "__type__": "dataframe",
            "index": [str(item) for item in frame.index.tolist()],
            "columns": [str(item) for item in frame.columns.tolist()],
            "data": frame.where(pd.notnull(frame), None).values.tolist(),
        }
    if isinstance(value, pd.Series):
        series = value.copy()
        series.index = series.index.map(str)
        return {
            "__type__": "series",
            "index": [str(item) for item in series.index.tolist()],
            "data": series.where(pd.notnull(series), None).tolist(),
        }
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": _jsonable(data)}


def _run_command(args: list[str], timeout: int = 10) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "").strip()[-4000:],
            "stderr": (proc.stderr or "").strip()[-4000:],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _gateway_process_snapshot() -> dict[str, Any]:
    if os.name != "nt":
        return {"ok": False, "reason": "not_windows", "pid": os.getpid()}
    script = rf"""
$pidValue = {os.getpid()}
$proc = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
$tcp = Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue | Select-Object LocalAddress,LocalPort,State,OwningProcess
$task = Get-ScheduledTask -TaskName '{GATEWAY_TASK_NAME}' -ErrorAction SilentlyContinue
$taskInfo = Get-ScheduledTask -TaskName '{GATEWAY_TASK_NAME}' -ErrorAction SilentlyContinue | Get-ScheduledTaskInfo -ErrorAction SilentlyContinue
[pscustomobject]@{{
  pid = $pidValue
  processName = $proc.ProcessName
  path = $proc.Path
  startTime = if ($proc.StartTime) {{ $proc.StartTime.ToString('s') }} else {{ $null }}
  privateMemoryMb = if ($proc) {{ [math]::Round($proc.PrivateMemorySize64 / 1MB, 2) }} else {{ $null }}
  cpu = if ($proc) {{ [math]::Round($proc.CPU, 2) }} else {{ $null }}
  tcp = $tcp
  taskState = if ($task) {{ $task.State.ToString() }} else {{ $null }}
  lastRunTime = if ($taskInfo.LastRunTime) {{ $taskInfo.LastRunTime.ToString('s') }} else {{ $null }}
  lastTaskResult = if ($taskInfo) {{ $taskInfo.LastTaskResult }} else {{ $null }}
}} | ConvertTo-Json -Depth 6
"""
    result = _run_command(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], timeout=10)
    if result.get("ok") and result.get("stdout"):
        try:
            return {"ok": True, "data": json.loads(result["stdout"])}
        except Exception:
            pass
    return result


def _restart_gateway_after_response(delay_seconds: float = 1.0) -> None:
    time.sleep(delay_seconds)
    if os.name != "nt":
        return
    pid = os.getpid()
    start_script = PROJECT_ROOT / "scripts" / "start_tdx_gateway.bat"
    command = (
        f"Start-Sleep -Seconds 2; "
        f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue; "
        f"Start-Sleep -Seconds 2; "
        f"Start-Process -FilePath '{start_script}' -WindowStyle Hidden"
    )
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", command],
        cwd=str(PROJECT_ROOT),
        close_fds=True,
    )


def _limit_market_data_response(data: Any, field_list: list[str]) -> Any:
    if not isinstance(data, dict):
        return data
    requested = [str(item).strip() for item in field_list if str(item).strip()]
    allowed = requested or list(DEFAULT_MARKET_DATA_RESPONSE_FIELDS)
    allowed_keys = {item.lower() for item in allowed}
    out: dict[str, Any] = {}
    for key, value in data.items():
        if str(key).lower() in allowed_keys:
            out[str(key)] = value
    return out


def _normalize_tdx_date(value: Optional[str], field_name: str) -> Optional[str]:
    """TdxQuant accepts compact date strings; reject formats that can hang SDK calls."""
    if value is None or value == "":
        return value
    text = str(value).strip()
    if re.fullmatch(r"\d{8}(\d{6})?", text):
        return text
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text.replace("-", "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text):
        return text.replace("-", "").replace(":", "").replace(" ", "")
    raise HTTPException(
        status_code=422,
        detail=f"{field_name} must be YYYYMMDD, YYYYMMDDHHMMSS, YYYY-MM-DD, or YYYY-MM-DD HH:MM:SS",
    )


@app.get("/health")
def health() -> dict[str, Any]:
    status = tdxquant_pool.get_status()
    ready = str(_jsonable(status)).lower() == "available"
    return {
        "ok": True,
        "ready": ready,
        "status": _jsonable(status),
        "last_activity": _jsonable(tdxquant_pool.get_last_activity()),
        "last_error": getattr(tdxquant_pool, "_last_init_error", None),
    }


@app.get("/admin/diagnostics")
def admin_diagnostics(run_probe: bool = False) -> dict[str, Any]:
    status = tdxquant_pool.get_status()
    probe: Optional[dict[str, Any]] = None
    if run_probe:
        try:
            result = tdxquant_pool.get_market_data(
                field_list=[],
                stock_list=["999999.SH"],
                period="1d",
                count=1,
                dividend_type="none",
                fill_data=False,
            )
            probe = {
                "ok": isinstance(result, dict) and bool(result),
                "type": type(result).__name__,
                "fields": list(result.keys())[:12] if isinstance(result, dict) else [],
            }
        except Exception as exc:
            probe = {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "data": {
            "gateway": {
                "pid": os.getpid(),
                "url": "http://127.0.0.1:8765",
                "status": _jsonable(status),
                "ready": str(_jsonable(status)).lower() == "available",
                "last_activity": _jsonable(tdxquant_pool.get_last_activity()),
                "last_error": getattr(tdxquant_pool, "_last_init_error", None),
            },
            "process": _gateway_process_snapshot(),
            "market_data_probe": probe,
            "checked_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        },
    }


@app.post("/admin/restart")
def admin_restart() -> dict[str, Any]:
    if os.name != "nt":
        raise HTTPException(status_code=501, detail="Gateway restart is only available on the Windows host process")
    snapshot = _gateway_process_snapshot()
    threading.Thread(target=_restart_gateway_after_response, daemon=True).start()
    return {
        "ok": True,
        "data": {
            "message": "TDX Gateway restart has been scheduled through the Windows task entry.",
            "task_name": GATEWAY_TASK_NAME,
            "before": snapshot,
            "requested_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        },
    }


@app.get("/account/ths/health")
def ths_account_health() -> dict[str, Any]:
    try:
        import pywinauto  # type: ignore

        pywinauto_available = True
        pywinauto_version = getattr(pywinauto, "__version__", None)
    except Exception as exc:
        pywinauto_available = False
        pywinauto_version = None
        pywinauto_error = str(exc)
    else:
        pywinauto_error = None

    return {
        "ok": True,
        "ready": bool(pywinauto_available),
        "pywinauto_available": pywinauto_available,
        "pywinauto_version": pywinauto_version,
        "pywinauto_error": pywinauto_error,
        "window_check": "skipped",
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }


@app.post("/account/ths/capital-holdings")
def ths_account_capital_holdings() -> dict[str, Any]:
    code = r'''
import json
from datetime import datetime

try:
    from services.ths_account_bridge import read_ths_capital_holdings
    result = read_ths_capital_holdings()
except Exception as exc:
    result = {
        "ok": False,
        "source": "tdx_gateway_ths_bridge_subprocess",
        "message": f"读取同花顺资金持仓失败: {exc}",
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }
if isinstance(result, dict):
    result.setdefault("source", "tdx_gateway_ths_bridge_subprocess")
    result.setdefault("updated_at", datetime.now().isoformat(sep=" ", timespec="seconds"))
else:
    result = {
        "ok": False,
        "source": "tdx_gateway_ths_bridge_subprocess",
        "message": "读取同花顺资金持仓返回了非字典结果",
        "raw_type": type(result).__name__,
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }
print("AISTOCK_THS_RESULT=" + json.dumps(result, ensure_ascii=False, default=str))
'''
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=70,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "ready": False,
            "source": "tdx_gateway_ths_bridge",
            "message": "读取同花顺资金持仓超时，已终止本次子进程读取",
            "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        }
    marker = "AISTOCK_THS_RESULT="
    for line in reversed(str(proc.stdout or "").splitlines()):
        if line.startswith(marker):
            result = json.loads(line[len(marker):])
            if isinstance(result, dict):
                result.setdefault("source", "tdx_gateway_ths_bridge")
                result.setdefault("subprocess_returncode", proc.returncode)
                return result
    return {
        "ok": False,
        "ready": False,
        "source": "tdx_gateway_ths_bridge",
        "message": "读取同花顺资金持仓子进程没有返回有效结果",
        "subprocess_returncode": proc.returncode,
        "stderr": str(proc.stderr or "")[-1000:],
        "stdout": str(proc.stdout or "")[-1000:],
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }


@app.post("/account/ths/trades")
def ths_account_trades() -> dict[str, Any]:
    code = r'''
import json
from datetime import datetime

try:
    from services.ths_account_bridge import read_ths_trade_history
    result = read_ths_trade_history()
except Exception as exc:
    result = {
        "ok": False,
        "source": "tdx_gateway_ths_bridge_subprocess",
        "message": f"读取同花顺历史成交失败: {exc}",
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }
if isinstance(result, dict):
    result.setdefault("source", "tdx_gateway_ths_bridge_subprocess")
    result.setdefault("updated_at", datetime.now().isoformat(sep=" ", timespec="seconds"))
else:
    result = {
        "ok": False,
        "source": "tdx_gateway_ths_bridge_subprocess",
        "message": "读取同花顺历史成交返回了非字典结果",
        "raw_type": type(result).__name__,
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }
print("AISTOCK_THS_RESULT=" + json.dumps(result, ensure_ascii=False, default=str))
'''
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "ready": False,
            "source": "tdx_gateway_ths_bridge",
            "message": "读取同花顺历史成交超时，已终止本次子进程读取",
            "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        }
    marker = "AISTOCK_THS_RESULT="
    for line in reversed(str(proc.stdout or "").splitlines()):
        if line.startswith(marker):
            result = json.loads(line[len(marker):])
            if isinstance(result, dict):
                result.setdefault("source", "tdx_gateway_ths_bridge")
                result.setdefault("subprocess_returncode", proc.returncode)
                return result
    return {
        "ok": False,
        "ready": False,
        "source": "tdx_gateway_ths_bridge",
        "message": "读取同花顺历史成交子进程没有返回有效结果",
        "subprocess_returncode": proc.returncode,
        "stderr": str(proc.stderr or "")[-1000:],
        "stdout": str(proc.stdout or "")[-1000:],
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }


@app.post("/initialize")
def initialize() -> dict[str, Any]:
    initialized = bool(tdxquant_pool._initialize(force=True))
    status = tdxquant_pool.get_status()
    return {
        "ok": initialized,
        "ready": initialized,
        "status": _jsonable(status),
        "last_activity": _jsonable(tdxquant_pool.get_last_activity()),
        "last_error": getattr(tdxquant_pool, "_last_init_error", None),
    }


@app.post("/stock-list")
def stock_list(request: StockListRequest) -> dict[str, Any]:
    return _ok(
        tdxquant_pool.get_stock_list(
            market=request.market,
            stock_type=request.stock_type,
            list_type=request.list_type,
        )
    )


@app.get("/stock-info/{stock_code}")
def stock_info(stock_code: str) -> dict[str, Any]:
    return _ok(tdxquant_pool.get_stock_info(stock_code))


@app.post("/realtime-quotes")
def realtime_quotes(request: QuotesRequest) -> dict[str, Any]:
    return _ok(tdxquant_pool.get_realtime_quotes(request.stock_codes))


@app.post("/market-data")
def market_data(request: MarketDataRequest) -> dict[str, Any]:
    start_time = _normalize_tdx_date(request.start_time, "start_time")
    end_time = _normalize_tdx_date(request.end_time, "end_time")
    result = tdxquant_pool.get_market_data(
        field_list=[],
        stock_list=request.stock_list,
        period=request.period,
        start_time=start_time,
        end_time=end_time,
        count=request.count,
        dividend_type=request.dividend_type,
        fill_data=request.fill_data,
    )
    if result is None:
        raise HTTPException(status_code=503, detail="TdxQuant market data is unavailable")
    result = _limit_market_data_response(result, request.field_list)
    gc.collect()
    return _ok(result)


@app.post("/refresh-cache")
def refresh_cache(request: RefreshCacheRequest) -> dict[str, Any]:
    return _ok(tdxquant_pool.refresh_cache(force=request.force, market=request.market))


@app.post("/refresh-kline")
def refresh_kline(request: RefreshKlineRequest) -> dict[str, Any]:
    return _ok(tdxquant_pool.refresh_kline(request.stock_list, request.period))


@app.post("/gb-info")
def gb_info(request: GbInfoRequest) -> dict[str, Any]:
    return _ok(tdxquant_pool.get_gb_info(request.stock_code, request.date_list, request.count))


@app.post("/gb-info-by-date")
def gb_info_by_date(request: GbInfoByDateRequest) -> dict[str, Any]:
    return _ok(tdxquant_pool.get_gb_info_by_date(request.stock_code, request.start_date, request.end_date))


@app.get("/financial-data/{stock_code}")
def financial_data(stock_code: str, report_type: int = 1, report_period: Optional[str] = None) -> dict[str, Any]:
    return _ok(tdxquant_pool.get_financial_data(stock_code, report_type, report_period))


@app.get("/sector-data")
def sector_data(sector_code: Optional[str] = None) -> dict[str, Any]:
    return _ok(tdxquant_pool.get_sector_data(sector_code))


@app.get("/sector-list")
def sector_list() -> dict[str, Any]:
    return _ok(tdxquant_pool.get_sector_list())


@app.get("/sector/{sector_code}/stocks")
def sector_stocks(sector_code: str) -> dict[str, Any]:
    return _ok(tdxquant_pool.get_stock_list_in_sector(sector_code))
