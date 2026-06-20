"""
V4 trading and backtest APIs.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import smtplib
import subprocess
import sys
import threading
import warnings
from datetime import date, datetime, time, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import numpy as np
import pandas as pd
from fastapi import APIRouter, Body, Query
from sqlalchemy import text
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger

from api.gen2_strategy import (
    GEN2_OPEN_RULE_V1,
    build_gen2_backtest_history,
    build_gen2_data_freshness,
    build_gen2_open_signals,
    build_gen2_strategy_lab,
    build_gen2_timing,
    update_gen2_trade_classification,
)
from api.gen2_factor import build_gen2_factor_registry, run_gen2_factor_test
from utils.database import db
from utils.logger import get_logger
from utils.market_warehouse import clean_minute_bars_df, clickhouse_available, clickhouse_table_exists, clickhouse_query_df
from utils.config import config as app_config
from utils.paths import report_path, runtime_path
from scheduler.trading_calendar import TradingCalendar
from execution.ptrade_bridge import PTradeFileBridge
from scripts.ptrade_bridge_readiness_audit import run_audit as run_ptrade_bridge_readiness_audit
from scripts.ptrade_bridge_live_probe import run_probe as run_ptrade_bridge_live_probe
from scripts.ptrade_bridge_acceptance import run_acceptance as run_ptrade_bridge_acceptance
from scripts.ptrade_bridge_live_submit_test import run_live_submit_test as run_ptrade_bridge_live_submit_test
from scripts.ptrade_bridge_watch_acceptance import run_watch_acceptance as run_ptrade_bridge_watch_acceptance
from scripts.ptrade_bridge_evidence_report import build_evidence as build_ptrade_bridge_evidence_report

warnings.filterwarnings(
    "ignore",
    message=r"32-bit application should be automated using 32-bit Python.*",
    category=UserWarning,
)

router = APIRouter(prefix="/trading", tags=["trading"])
logger = get_logger("trading")
REPO_ROOT = Path(__file__).resolve().parents[1]
PTRADE_BRIDGE = PTradeFileBridge()
GEN2_SHADOW_UPDATE_TASKS: Dict[str, Dict[str, Any]] = {}
GEN2_SHADOW_UPDATE_TASK_LOCK = threading.Lock()
GEN2_BACKTEST_UPDATE_TASKS: Dict[str, Dict[str, Any]] = {}
GEN2_BACKTEST_UPDATE_TASK_LOCK = threading.Lock()
GEN2_OFFICIAL_REBUILD_TASKS: Dict[str, Dict[str, Any]] = {}
GEN2_OFFICIAL_REBUILD_TASK_LOCK = threading.Lock()
GEN2_MAINLINE_UPDATE_TASKS: Dict[str, Dict[str, Any]] = {}
GEN2_MAINLINE_UPDATE_TASK_LOCK = threading.Lock()
PTRADE_ACCEPTANCE_TASKS: Dict[str, Dict[str, Any]] = {}
PTRADE_ACCEPTANCE_TASK_LOCK = threading.Lock()
PTRADE_LIVE_SUBMIT_TEST_TASKS: Dict[str, Dict[str, Any]] = {}
PTRADE_LIVE_SUBMIT_TEST_TASK_LOCK = threading.Lock()
PTRADE_WATCH_ACCEPTANCE_TASKS: Dict[str, Dict[str, Any]] = {}
PTRADE_WATCH_ACCEPTANCE_TASK_LOCK = threading.Lock()

DEFAULT_V4_OUTPUT_DIR = r"C:\Users\Administrator\Documents\Codex\2026-04-23-aistock-100000-a-3-a-t\output_v4"
DEFAULT_V42_OUTPUT_DIR = r"C:\Users\Administrator\Documents\Codex\2026-04-23-aistock-100000-a-3-a-t\output_v42"
DEFAULT_BACKTEST_STRATEGY_VERSION = os.getenv("AISTOCK_V4_BACKTEST_STRATEGY_VERSION", "v5")
DEFAULT_LIVE_STRATEGY_VERSION = os.getenv("AISTOCK_V4_LIVE_STRATEGY_VERSION", "v5")
DEFAULT_SIGNAL_EXECUTION_MODE = os.getenv("AISTOCK_V4_SIGNAL_EXECUTION_MODE", "same_day").strip().lower()
DEFAULT_LIVE_HARD_DD = -0.10
LIVE_HARD_DD_ENV_KEY = "AISTOCK_V4_LIVE_HARD_DD"
DEFAULT_LIVE_FREEZE_DAYS = 1
LIVE_FREEZE_DAYS_ENV_KEY = "AISTOCK_V4_LIVE_FREEZE_DAYS"
DEFAULT_LIVE_RECOVERY_DAYS = 3
LIVE_RECOVERY_DAYS_ENV_KEY = "AISTOCK_V4_LIVE_RECOVERY_DAYS"
DEFAULT_LIVE_RECOVERY_MAX_HOLDINGS = 1
LIVE_RECOVERY_MAX_HOLDINGS_ENV_KEY = "AISTOCK_V4_LIVE_RECOVERY_MAX_HOLDINGS"

TRADING_ARTIFACT_TABLE = "trading_strategy_artifacts"
TRADING_SELECTION_SCORE_TABLE = "trading_selection_scores"
TRADING_INTRADAY_SIGNAL_ARTIFACT_KEY = "intraday_buy_points"
GEN2_RISK_COOL_SHADOW_LEDGER_PATH = report_path("gen2_risk_cool_shadow_ledger", "shadow_ledger.csv")
GEN2_RISK_COOL_SHADOW_SUMMARY_PATH = report_path("gen2_risk_cool_shadow_ledger", "summary.csv")
GEN2_RISK_COOL_SHADOW_LIVE_DIR = report_path("gen2_risk_cool_shadow_ledger", "live_updates")
GEN2_V4_EVENT_DATASET_PATH = report_path("gen2_event_study_full", "v4_event_dataset.parquet")
GEN2_OPEN_SIGNAL_SOURCE_PATH = report_path("gen2_30m_fractal_restart_realistic_d1_w2", "fractal_triggers.parquet")
GEN2_VALID_SIGNAL_TARGET_PATH = (
    report_path("gen2_intraday_normal_signal_filters_tday_context", "signals_intraday_normal_30m_before_confirm.parquet")
)
GEN2_RISK_COOL_BASE_PATH = report_path("gen2_risk_cool_dynamic_circuit_user_v2_cap_v2", "sources", "risk_cool_base.csv")
GEN2_V2_COMPLETE_SOURCE_PATH = report_path("gen2_v2_complete_strategy", "sources", "g2_v2_complete.parquet")
GEN2_V2_COMPLETE_SUMMARY_PATH = report_path("gen2_v2_complete_strategy", "summary.json")
GEN2_ALPHA191_TRAIN_SOURCE_PATH = report_path("gen2_alpha191_overlay_candidate_train_dirs", "sources", "risk_cool_base.parquet")
GEN2_ALPHA191_TRAIN_VALUES_PATH = report_path("gen2_alpha191_candidate_core10_t1", "alpha191_core10_t1_signal_values.parquet")
GEN2_OPEN_STATE_DAILY_PATH = report_path("gen2_open_state_research_full", "g2_open_state_daily.csv")
GEN2_DAILY_TICKET_DIR = runtime_path("gen2_daily_trade_tickets")
GEN2_DAILY_TICKET_LATEST_PATH = GEN2_DAILY_TICKET_DIR / "latest.json"
GEN2_DAILY_EXECUTION_LEDGER_PATH = GEN2_DAILY_TICKET_DIR / "execution_ledger.json"
GEN2_OPEN_STATE_TIMING_SCRIPT_PATH = REPO_ROOT / "scripts" / "gen2_timing_regime_research.py"
GEN2_OPEN_STATE_RESEARCH_SCRIPT_PATH = REPO_ROOT / "scripts" / "gen2_build_open_state_research.py"
GEN2_OPEN_STATE_TIMING_DIR = report_path("gen2_timing_research")
GEN2_OPEN_STATE_RESEARCH_DIR = report_path("gen2_open_state_research_full")
GEN2_MAINLINE_INTRADAY_FACTOR_DIR = report_path("mainline_intraday_diffusion_factor")
GEN2_MAINLINE_INTRADAY_OVERLAY_DIR = report_path("mainline_intraday_candidate_overlay")
GEN2_MAINLINE_THEME_POOL_DIR = report_path("mainline_theme_observation_pool_v1")
GEN2_MAINLINE_THEME_OVERLAY_DIR = report_path("mainline_theme_strategy_overlay_v1")
GEN2_MAINLINE_SECTOR_WATCHLIST_DIR = report_path("mainline_sector_watchlist")
GEN2_OPEN_STATE_REBUILD_LOCK = threading.Lock()
GEN2_OPEN_STATE_REBUILD_MAX_COOLDOWN_SECONDS = 300
GEN2_OPEN_STATE_REBUILD_TIMEOUT_SECONDS = 1800
GEN2_OPEN_STATE_TIMING_START_DATE = "2010-01-01"
GEN2_ALPHA191_ACTIVE_GATES = {"off", "volume5_keep80_runup", "g2_v2_complete"}

STRATEGY_VERSION_LABELS = {
    "v4": "V4",
    "v4.2_15m": "V4.2 15m",
    "v4.2_30m": "V4.2 30m",
    "v5": "V5",
    "v4_audit_raw": "V4 Audit Raw",
}
STRATEGY_VERSION_ORDER = {
    "v4": 10,
    "v4.1": 20,
    "v4.2_15m": 30,
    "v4.2_30m": 40,
    "v5": 50,
    "v4_audit_raw": 60,
}

HOLDING_PATTERN = re.compile(r"^(?P<code>\d{6}):(?P<shares>\d+)@(?P<price>[0-9.]+)$")

SCORE_MIN_TURNOVER20 = 2e8
SCORE_MIN_VOL10 = 0.008
SCORE_MAX_VOL10 = 0.09
MODE_MAX_HOLDINGS = {"on": 3, "neutral": 2, "off": 1}
DEFAULT_ENTRY_MIN_SCORE = 0.75
DEFAULT_KEEP_RANK_MULT = 2
DEFAULT_INTRADAY_CUTOFF_TIME = "14:30"
DEFAULT_INTRADAY_SELL_CUTOFF_TIME = "10:00"
DEFAULT_INTRADAY_BREAKOUT_BUFFER = 0.995
DEFAULT_INTRADAY_VOLUME_RATIO_FLOOR = 0.75
DEFAULT_INTRADAY_SESSION_RETURN_CAP = 0.08

_SIGNAL_DAY_CACHE: Dict[str, Dict[str, Any]] = {}
_ARTIFACT_TABLE_READY = False
_SELECTION_SCORE_TABLE_READY = False
_GEN2_OPEN_STATE_REBUILD_STATE: Dict[str, Any] = {
    "running": False,
    "target_date": None,
    "started_at": None,
    "attempted_at": None,
    "success_at": None,
    "last_error": None,
    "last_error_at": None,
}

V4_MONITOR_DIR = runtime_path("v4_live_monitor")
V4_MONITOR_STATE_PATH = V4_MONITOR_DIR / "monitor_state.json"
V4_MONITOR_SIGNAL_PATH = V4_MONITOR_DIR / "signal_snapshot.json"
GEN2_SHADOW_MONITOR_STATE_PATH = V4_MONITOR_DIR / "gen2_shadow_buy_monitor_state.json"
GEN2_STRATEGY_REFRESH_STATE_PATH = V4_MONITOR_DIR / "gen2_strategy_refresh_state.json"
GEN2_SHADOW_VERIFICATION_PATH = V4_MONITOR_DIR / "gen2_shadow_signal_verifications.json"
THS_CAPITAL_HOLDINGS_CACHE_PATH = V4_MONITOR_DIR / "ths_capital_holdings_cache.json"
_v4_monitor_scheduler: Optional[BackgroundScheduler] = None
_v4_monitor_scheduler_lock = threading.Lock()
_v4_monitor_job_id = "v4_manual_holdings_monitor"
_gen2_shadow_monitor_job_id = "gen2_shadow_buy_monitor"
_gen2_strategy_refresh_job_id = "gen2_strategy_refresh_30m"
_gen2_strategy_refresh_lock = threading.Lock()
GEN2_30M_BAR_CLOSE_TIMES = (
    time(10, 0),
    time(10, 30),
    time(11, 0),
    time(11, 30),
    time(13, 30),
    time(14, 0),
    time(14, 30),
    time(15, 0),
)


def _ensure_v4_monitor_dir() -> None:
    V4_MONITOR_DIR.mkdir(parents=True, exist_ok=True)


def _load_json_file(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _detect_ths_table_view(raw_text: str) -> str:
    text = str(raw_text or "")
    if all(token in text for token in ["交易日期", "交易时间", "股票代码", "操作"]):
        return "recent_trades"
    if all(token in text for token in ["股票代码", "股票名称", "证券数量", "持仓数"]):
        return "holdings"
    return "unknown"


def _read_ths_delivery_file_text() -> Dict[str, Any]:
    candidates = [
        Path(r"D:\THS_交割单_1.txt"),
        Path(r"D:\THS_交割单_持有明细.txt"),
        Path(r"D:\THS_交割单_历史持仓.txt"),
    ]
    target = next((path for path in candidates if path.exists()), None)
    if target is None:
        return {"ok": False, "message": "未找到可用交割单文件", "searched_paths": [str(p) for p in candidates]}
    encodings = ["utf-8", "gbk", "gb18030", "utf-16"]
    raw_text = ""
    last_error: Optional[Exception] = None
    for enc in encodings:
        try:
            raw_text = target.read_text(encoding=enc)
            if str(raw_text).strip():
                break
        except Exception as exc:
            last_error = exc
            raw_text = ""
    if not str(raw_text).strip():
        err = str(last_error) if last_error is not None else "EMPTY_FILE"
        return {"ok": False, "message": f"读取交割单文件失败: {err}", "file_path": str(target)}
    lines = [line.rstrip("\r") for line in str(raw_text or "").splitlines() if line.strip()]
    normalized = "\n".join(lines)
    return {
        "ok": True,
        "raw_text": normalized,
        "view_type": "delivery_file",
        "view_type_desc": "交割单文本",
        "recognized_recent_trade": True,
        "preview_lines": lines[:12],
        "file_path": str(target),
        "file_mtime": datetime.fromtimestamp(target.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    }

def _get_ths_trade_window():
    from pywinauto import Application
    try:
        app = Application(backend="uia").connect(path="xiadan.exe", timeout=3)
    except Exception as exc:
        raise RuntimeError(str(exc))
    candidates = []
    for win in app.windows():
        try:
            title = str(win.window_text() or "").strip()
        except Exception:
            title = ""
        if not title:
            continue
        candidates.append((title, win.handle))
    for title, win in candidates:
        if "网上股票交易系统" in title:
            return app.window(handle=win)
    for title, win in candidates:
        if "交易系统" in title or "xiadan" in title.lower():
            return app.window(handle=win)
    raise RuntimeError("NO_XIADAN_WINDOW")


def _read_ths_current_table_text() -> Dict[str, Any]:
    try:
        from pywinauto.keyboard import send_keys
        import time as time_mod

        win = _get_ths_trade_window()
        win.set_focus()
        time_mod.sleep(0.5)

        history_item = win.child_window(title="历史成交", control_type="TreeItem")
        if not history_item.exists(timeout=3):
            return {"ok": False, "message": "未找到同花顺“历史成交”菜单"}
        history_item.click_input()
        time_mod.sleep(1.0)

        try:
            recent_week_btn = win.child_window(title="最近一周", control_type="Button")
            if recent_week_btn.exists(timeout=1):
                recent_week_btn.click_input()
                time_mod.sleep(0.8)
        except Exception:
            pass

        try:
            grid_pane = win.child_window(title="Custom1", auto_id="1047", control_type="Pane")
            if grid_pane.exists(timeout=1):
                grid_pane.click_input(coords=(200, 30))
                time_mod.sleep(0.4)
        except Exception:
            pass

        send_keys("^a")
        time_mod.sleep(0.2)
        send_keys("^c")
        time_mod.sleep(0.8)

        clip_proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-STA",
                "-Command",
                "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Clipboard]::GetText()",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
        raw_text = str(clip_proc.stdout or "").strip()
        if clip_proc.returncode != 0 or not raw_text:
            err = str(clip_proc.stderr or "").strip() or "EMPTY_CLIPBOARD_TEXT"
            raise RuntimeError(err)
    except Exception as exc:
        logger.warning(f"read ths current table failed: {exc}")
        return {"ok": False, "message": f"读取同花顺窗口失? {exc}"}

    lines = [line.rstrip("\r") for line in str(raw_text or "").splitlines() if line.strip()]
    raw_text = "\n".join(lines)
    view_type = _detect_ths_table_view(raw_text)
    view_desc = {
        "recent_trades": "历史成交",
        "holdings": "持仓",
        "unknown": "鏈煡琛ㄦ牸",
    }.get(view_type, "鏈煡琛ㄦ牸")
    return {
        "ok": True,
        "raw_text": raw_text,
        "view_type": view_type,
        "view_type_desc": view_desc,
        "recognized_recent_trade": view_type == "recent_trades",
        "preview_lines": lines[:12],
    }


def _split_ths_row(line: str) -> List[str]:
    raw_text = str(line or "").rstrip("\r\n")
    if not raw_text.strip():
        return []
    if "\t" in raw_text:
        parts = [part.strip() for part in raw_text.split("\t")]
        while parts and parts[-1] == "":
            parts.pop()
        return parts
    text = raw_text.strip()
    return [part.strip() for part in re.split(r"\s{2,}", text) if str(part).strip()]


def _parse_ths_numeric(value: Any) -> Optional[float]:
    text = str(value or "").replace(",", "").strip()
    if not text:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def _extract_numeric_after_labels(raw_text: str, labels: List[str]) -> Optional[float]:
    text = str(raw_text or "")
    for label in labels:
        pattern = rf"{re.escape(label)}[^\d\-]{{0,16}}(-?\d[\d,]*(?:\.\d+)?)"
        m = re.search(pattern, text)
        if m:
            value = _parse_ths_numeric(m.group(1))
            if value is not None:
                return value
    tokens = [tok.strip() for tok in re.split(r"[\t\r\n]+", text) if str(tok).strip()]
    for idx, tok in enumerate(tokens):
        for label in labels:
            if label in tok and idx + 1 < len(tokens):
                value = _parse_ths_numeric(tokens[idx + 1])
                if value is not None:
                    return value
    return None


def _is_valid_ths_holding_name(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return re.fullmatch(r"[\d.\-]+", text) is None


def _validate_ths_capital_holdings_result(holdings: List[Dict[str, Any]]) -> bool:
    if not holdings:
        return False
    for item in holdings:
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or "").strip()
        shares = int(item.get("shares") or 0)
        cost_price = _parse_ths_numeric(item.get("cost_price"))
        current_price = _parse_ths_numeric(item.get("current_price"))
        market_value = _parse_ths_numeric(item.get("market_value"))
        if not re.fullmatch(r"\d{6}", code):
            return False
        if not _is_valid_ths_holding_name(name):
            return False
        if shares <= 0:
            return False
        if market_value is not None and market_value <= 0:
            return False
        if cost_price is not None and cost_price <= 0:
            return False
        if current_price is not None and current_price <= 0:
            return False
    return True


def _parse_ths_capital_holdings(raw_text: str) -> Dict[str, Any]:
    lines = [line.rstrip("\r") for line in str(raw_text or "").splitlines() if str(line).strip()]
    header_idx = -1
    headers: List[str] = []
    for idx, line in enumerate(lines):
        cols = _split_ths_row(line)
        if any("股票代码" in col for col in cols) and any("股票名称" in col for col in cols):
            header_idx = idx
            headers = cols
            break

    def _find_header_index(candidates: List[str]) -> int:
        for i, header in enumerate(headers):
            normalized = str(header or "").replace(" ", "")
            if any(candidate in normalized for candidate in candidates):
                return i
        return -1

    code_idx = _find_header_index(["股票代码"])
    name_idx = _find_header_index(["股票名称"])
    shares_idx = _find_header_index(["证券数量", "当前持仓", "持仓数量", "股票余额"])
    cost_idx = _find_header_index(["成本价", "摊薄成本", "保本价"])
    current_idx = _find_header_index(["市价", "最新价", "现价"])
    market_idx = _find_header_index(["参考市值", "市值", "最新市值"])
    pnl_idx = _find_header_index(["盈亏比", "盈亏比例"])

    holdings: List[Dict[str, Any]] = []
    if header_idx >= 0:
        for line in lines[header_idx + 1 :]:
            cols = _split_ths_row(line)
            if not cols:
                continue
            code = ""
            if 0 <= code_idx < len(cols):
                code = str(cols[code_idx]).strip()
            if not re.fullmatch(r"\d{6}", code):
                hit = next((str(col).strip() for col in cols if re.fullmatch(r"\d{6}", str(col).strip())), "")
                code = hit
            if not code:
                continue
            name = str(cols[name_idx]).strip() if 0 <= name_idx < len(cols) else ""
            shares = _parse_ths_numeric(cols[shares_idx]) if 0 <= shares_idx < len(cols) else None
            cost_price = _parse_ths_numeric(cols[cost_idx]) if 0 <= cost_idx < len(cols) else None
            current_price = _parse_ths_numeric(cols[current_idx]) if 0 <= current_idx < len(cols) else None
            market_value = _parse_ths_numeric(cols[market_idx]) if 0 <= market_idx < len(cols) else None
            pnl_ratio = _parse_ths_numeric(cols[pnl_idx]) if 0 <= pnl_idx < len(cols) else None
            shares_int = int(shares or 0)
            if shares_int <= 0:
                continue
            if (current_price is None or current_price <= 0) and market_value is not None and shares_int > 0:
                current_price = market_value / shares_int
            holdings.append(
                {
                    "code": code,
                    "name": name,
                    "shares": shares_int,
                    "cost_price": cost_price,
                    "current_price": current_price,
                    "market_value": market_value,
                    "pnl_ratio": pnl_ratio,
                }
            )

    holdings_market_value = None
    if holdings:
        holdings_market_value = sum((_parse_ths_numeric(item.get("market_value")) or 0.0) for item in holdings)
    market_value = _extract_numeric_after_labels(raw_text, ["参考市值", "市值", "最新市值", "总市值"])
    available_cash = _extract_numeric_after_labels(raw_text, ["可用资金", "可用现金", "可用金额", "现金可用"])
    total_capital = _extract_numeric_after_labels(raw_text, ["总资产", "总可用", "总资金"])
    cost_value = None
    if holdings:
        cost_value = 0.0
        for item in holdings:
            shares = int(item.get("shares") or 0)
            cost_price = _parse_ths_numeric(item.get("cost_price"))
            if cost_price is not None and shares > 0:
                cost_value += cost_price * shares
    if holdings_market_value is not None and holdings_market_value > 0:
        if market_value is None or market_value <= 0 or market_value < holdings_market_value * 0.5:
            market_value = holdings_market_value
    if cost_value is None and holdings:
        cost_value = sum(((_parse_ths_numeric(item.get("cost_price")) or 0.0) * int(item.get("shares") or 0)) for item in holdings)
    if total_capital is None and available_cash is not None and market_value is not None:
        total_capital = available_cash + market_value
    return {
        "capital": {
            "total_capital": total_capital,
            "available_cash": available_cash,
            "market_value": market_value,
            "cost_value": cost_value,
        },
        "holdings": holdings,
    }

def _get_ths_trade_window_win32():
    from pywinauto import Application

    uia_win = _get_ths_trade_window()
    handle = int(uia_win.handle)
    app = Application(backend="win32").connect(handle=handle, timeout=3)
    return app.window(handle=handle)


def _dismiss_ths_copy_verification_dialog() -> None:
    try:
        win = _get_ths_trade_window()
        prompt = win.child_window(title="确认撤单提示", control_type="Text")
        cancel = win.child_window(title="取消", control_type="Button")
        if prompt.exists(timeout=1) and cancel.exists(timeout=1):
            cancel.click_input()
    except Exception:
        pass


def _read_ths_capital_summary_from_controls(win32_win) -> Dict[str, Optional[float]]:
    statics = []
    for child in win32_win.children():
        try:
            if child.friendly_class_name() != "Static":
                continue
            text = str(child.window_text() or "").strip()
            rect = child.rectangle()
            statics.append(
                {
                    "text": text,
                    "left": rect.left,
                    "top": rect.top,
                    "right": rect.right,
                    "bottom": rect.bottom,
                }
            )
        except Exception:
            continue

    def _find_value(labels: List[str], min_left: int, max_left: int, min_top: int, max_top: int) -> Optional[float]:
        candidates = [
            item
            for item in statics
            if item["text"] in labels and min_left <= item["left"] <= max_left and min_top <= item["top"] <= max_top
        ]
        if not candidates:
            candidates = [item for item in statics if item["text"] in labels]
        candidates = sorted(candidates, key=lambda item: (item["top"], item["left"]))
        for label in candidates:
            center_y = (label["top"] + label["bottom"]) / 2
            numeric_items = []
            for item in statics:
                if item["left"] <= label["right"]:
                    continue
                if abs(((item["top"] + item["bottom"]) / 2) - center_y) > 10:
                    continue
                value = _parse_ths_numeric(item["text"])
                if value is None:
                    continue
                numeric_items.append((abs(item["left"] - label["right"]), item["left"], value))
            if numeric_items:
                numeric_items.sort(key=lambda pair: (pair[0], pair[1]))
                return numeric_items[0][2]
        return None

    return {
        "available_cash": _find_value(["可用现金", "可用资金"], 200, 360, 150, 210),
        "market_value": _find_value(["参考市值", "市值"], 380, 500, 135, 180),
        "total_capital": _find_value(["总资产", "总资金"], 380, 500, 160, 205),
    }


def _read_ths_holdings_from_grid_ocr(win32_win) -> List[Dict[str, Any]]:
    from PIL import ImageGrab
    from rapidocr_onnxruntime import RapidOCR

    grid = None
    for child in win32_win.children():
        try:
            if child.friendly_class_name() == "CVirtualGridCtrl" and str(child.window_text() or "").strip() == "Custom1":
                grid = child
                break
        except Exception:
            continue
    if grid is None:
        raise RuntimeError("NO_THS_HOLDINGS_GRID")

    rect = grid.rectangle()
    image = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))
    tmp_path = REPO_ROOT / "tmp_ths_holdings_grid_ocr.png"
    image.save(tmp_path)

    ocr = RapidOCR()
    result, _ = ocr(str(tmp_path))
    if not result:
        raise RuntimeError("EMPTY_THS_HOLDINGS_OCR")

    lines: Dict[float, List[Tuple[float, str, float]]] = {}
    for box, text, score in result:
        xs = [point[0] for point in box]
        ys = [point[1] for point in box]
        x = min(xs)
        y = min(ys)
        group_key = None
        for existing in lines.keys():
            if abs(existing - y) <= 10:
                group_key = existing
                break
        if group_key is None:
            group_key = y
            lines[group_key] = []
        lines[group_key].append((x, str(text or "").strip(), float(score or 0)))

    holdings: List[Dict[str, Any]] = []
    for y in sorted(lines.keys()):
        texts = [text for _, text, _ in sorted(lines[y], key=lambda item: item[0])]
        code_idx = next((idx for idx, text in enumerate(texts) if re.fullmatch(r"\d{6}", text)), -1)
        if code_idx < 0:
            continue
        row = texts[code_idx:]
        if len(row) < 9:
            continue
        shares = int(_parse_ths_numeric(row[2]) or 0)
        if shares <= 0:
            continue
        holdings.append(
            {
                "code": row[0],
                "name": row[1],
                "shares": shares,
                "market_value": _parse_ths_numeric(row[4]),
                "cost_price": _parse_ths_numeric(row[5]),
                "current_price": _parse_ths_numeric(row[6]),
                "pnl_ratio": _parse_ths_numeric(row[8]),
            }
        )
    return holdings


def _read_ths_capital_holdings_without_copy() -> Dict[str, Any]:
    import time as time_mod

    _dismiss_ths_copy_verification_dialog()
    win32_win = _get_ths_trade_window_win32()
    win32_win.set_focus()
    time_mod.sleep(0.6)

    summary = _read_ths_capital_summary_from_controls(win32_win)
    holdings = _read_ths_holdings_from_grid_ocr(win32_win)
    if not _validate_ths_capital_holdings_result(holdings):
        raise RuntimeError("INVALID_THS_CAPITAL_HOLDINGS_OCR_PARSE")

    cost_value = sum(((_parse_ths_numeric(item.get("cost_price")) or 0.0) * int(item.get("shares") or 0)) for item in holdings)
    market_value = sum((_parse_ths_numeric(item.get("market_value")) or 0.0) for item in holdings)
    capital = {
        "total_capital": summary.get("total_capital"),
        "available_cash": summary.get("available_cash"),
        "market_value": summary.get("market_value") if summary.get("market_value") is not None else market_value,
        "cost_value": cost_value,
    }
    return {
        "ok": True,
        "view_type": "capital_holdings",
        "view_type_desc": "资金持仓",
        "capital": capital,
        "holdings": holdings,
        "preview_lines": [f"{item['code']} {item['name']} {item['shares']}" for item in holdings[:8]],
        "synced_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "fallback_cache": False,
        "source": "ths_window_ocr",
    }


def _read_ths_capital_holdings_text() -> Dict[str, Any]:
    try:
        result = _read_ths_capital_holdings_without_copy()
        _save_ths_capital_holdings_cache(result)
        return result
    except Exception as exc:
        logger.warning(f"read ths capital holdings without copy failed: {exc}")
        cached = _load_ths_capital_holdings_cache()
        if cached:
            return _sanitize(
                {
                    **cached,
                    "ok": True,
                    "fallback_cache": True,
                    "message": f"读取同花顺持仓信息失败，已返回缓存数据: {exc}",
                }
            )
        return {"ok": False, "message": f"读取同花顺持仓数据失败: {exc}"}

    try:
        from pywinauto.keyboard import send_keys
        import time as time_mod

        win = _get_ths_trade_window()
        win.set_focus()
        time_mod.sleep(0.5)

        holdings_item = win.child_window(title="资金持仓", control_type="TreeItem")
        if not holdings_item.exists(timeout=3):
            return {"ok": False, "message": "未找到同花顺“资金持仓”菜单"}
        holdings_item.click_input()
        time_mod.sleep(1.0)

        try:
            grid_pane = win.child_window(title="Custom1", auto_id="1047", control_type="Pane")
            if grid_pane.exists(timeout=1):
                grid_pane.click_input(coords=(220, 36))
                time_mod.sleep(0.4)
        except Exception:
            pass

        send_keys("^a")
        time_mod.sleep(0.2)
        send_keys("^c")
        time_mod.sleep(0.8)

        clip_proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-STA",
                "-Command",
                "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Clipboard]::GetText()",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
        raw_text = str(clip_proc.stdout or "").strip()
        if clip_proc.returncode != 0 or not raw_text:
            err = str(clip_proc.stderr or "").strip() or "EMPTY_CLIPBOARD_TEXT"
            raise RuntimeError(err)
    except Exception as exc:
        logger.warning(f"read ths capital holdings failed: {exc}")
        cached = _load_ths_capital_holdings_cache()
        if cached:
            return _sanitize(
                {
                    **cached,
                    "ok": True,
                    "fallback_cache": True,
                    "message": f"实时读取失败，已回到最近一次成功同步的资金持股快照: {exc}",
                }
            )
        return {"ok": False, "message": f"读取同花顺资金股票失? {exc}"}

    lines = [line.rstrip("\r") for line in str(raw_text or "").splitlines() if line.strip()]
    normalized = "\n".join(lines)
    parsed = _parse_ths_capital_holdings(normalized)
    if not _validate_ths_capital_holdings_result(parsed.get("holdings") or []):
        raise RuntimeError("INVALID_THS_CAPITAL_HOLDINGS_PARSE")
    result = {
        "ok": True,
        "raw_text": normalized,
        "view_type": "capital_holdings",
        "view_type_desc": "资金股票",
        "preview_lines": lines[:16],
        "capital": parsed.get("capital") or {},
        "holdings": parsed.get("holdings") or [],
        "synced_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "fallback_cache": False,
    }
    _save_ths_capital_holdings_cache(result)
    return result


def _read_ths_capital_holdings_text() -> Dict[str, Any]:
    first_error: Exception | None = None
    try:
        result = _read_ths_capital_holdings_without_copy()
        _save_ths_capital_holdings_cache(result)
        return result
    except Exception as exc:
        first_error = exc
        logger.warning(f"read ths capital holdings without copy failed: {exc}")

    try:
        from pywinauto.keyboard import send_keys
        import time as time_mod

        win = _get_ths_trade_window()
        win.set_focus()
        time_mod.sleep(0.5)

        holdings_item = win.child_window(title="资金持仓", control_type="TreeItem")
        if not holdings_item.exists(timeout=3):
            raise RuntimeError("NO_THS_CAPITAL_HOLDINGS_MENU")
        holdings_item.click_input()
        time_mod.sleep(1.0)

        try:
            grid_pane = win.child_window(title="Custom1", auto_id="1047", control_type="Pane")
            if grid_pane.exists(timeout=1):
                grid_pane.click_input(coords=(220, 36))
                time_mod.sleep(0.4)
        except Exception:
            pass

        send_keys("^a")
        time_mod.sleep(0.2)
        send_keys("^c")
        time_mod.sleep(0.8)

        clip_proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-STA",
                "-Command",
                "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Clipboard]::GetText()",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
        raw_text = str(clip_proc.stdout or "").strip()
        if clip_proc.returncode != 0 or not raw_text:
            err = str(clip_proc.stderr or "").strip() or "EMPTY_CLIPBOARD_TEXT"
            raise RuntimeError(err)
    except Exception as exc:
        logger.warning(f"read ths capital holdings clipboard fallback failed: {exc}")
        combined = RuntimeError(f"ocr={first_error}; clipboard={exc}") if first_error else exc
        return _ths_capital_holdings_cache_response(combined, "实时读取同花顺资金持仓失败")

    lines = [line.rstrip("\r") for line in str(raw_text or "").splitlines() if line.strip()]
    normalized = "\n".join(lines)
    parsed = _parse_ths_capital_holdings(normalized)
    if not _validate_ths_capital_holdings_result(parsed.get("holdings") or []):
        return _ths_capital_holdings_cache_response(
            RuntimeError("INVALID_THS_CAPITAL_HOLDINGS_PARSE"),
            "实时解析同花顺资金持仓失败",
        )
    result = {
        "ok": True,
        "raw_text": normalized,
        "view_type": "capital_holdings",
        "view_type_desc": "资金持仓",
        "preview_lines": lines[:16],
        "capital": parsed.get("capital") or {},
        "holdings": parsed.get("holdings") or [],
        "synced_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "fallback_cache": False,
        "source": "ths_clipboard",
    }
    _save_ths_capital_holdings_cache(result)
    return result


def _save_json_file(path: Path, payload: Any) -> None:
    _ensure_v4_monitor_dir()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _default_monitor_recipient_email() -> str:
    email_cfg = app_config.get("email", default=None, config_file="settings.yaml") or {}
    to_emails = email_cfg.get("to_emails") or []
    if isinstance(to_emails, str):
        to_emails = [to_emails]
    for item in to_emails:
        value = str(item or "").strip()
        if value:
            return value
    return str(email_cfg.get("from_email") or email_cfg.get("smtp_user") or "").strip()


def _default_v4_monitor_state() -> Dict[str, Any]:
    recipient_email = _default_monitor_recipient_email()
    return {
        "enabled": False,
        "interval_seconds": 60,
        "recipient_email": recipient_email,
        "holdings": [],
        "trading_hours_only": True,
        "quiet_minutes": 15,
        "repeat_reminder_minutes": 60,
        "reminder_active": False,
        "reminder_activated_at": None,
        "last_alert_by_code": {},
        "last_run_at": None,
        "last_email_sent_at": None,
        "last_error": None,
    }


def _load_v4_monitor_state() -> Dict[str, Any]:
    state = _load_json_file(V4_MONITOR_STATE_PATH, _default_v4_monitor_state())
    if not isinstance(state, dict):
        state = _default_v4_monitor_state()
    base = _default_v4_monitor_state()
    base.update(state)
    holdings = base.get("holdings")
    base["holdings"] = holdings if isinstance(holdings, list) else []
    base["interval_seconds"] = max(30, _to_int(base.get("interval_seconds"), 60))
    base["quiet_minutes"] = max(0, _to_int(base.get("quiet_minutes"), 15))
    base["repeat_reminder_minutes"] = max(60, _to_int(base.get("repeat_reminder_minutes"), 60))
    base["trading_hours_only"] = bool(base.get("trading_hours_only"))
    base["reminder_active"] = bool(base.get("reminder_active"))
    base["reminder_activated_at"] = str(base.get("reminder_activated_at") or "").strip() or None
    last_alert = base.get("last_alert_by_code")
    base["last_alert_by_code"] = last_alert if isinstance(last_alert, dict) else {}
    base["enabled"] = bool(base.get("enabled"))
    base["recipient_email"] = str(base.get("recipient_email") or "").strip()
    return base


def _save_v4_monitor_state(state: Dict[str, Any]) -> None:
    _save_json_file(V4_MONITOR_STATE_PATH, _sanitize(state))


def _default_gen2_shadow_monitor_state() -> Dict[str, Any]:
    recipient_email = _default_monitor_recipient_email()
    return {
        "enabled": True,
        "interval_seconds": 120,
        "pool_rank": 200,
        "alpha191_gate": "g2_v2_complete",
        "recipient_email": recipient_email,
        "trading_hours_only": True,
        "heartbeat_enabled": True,
        "heartbeat_email_minutes": 30,
        "official_rebuild_enabled": True,
        "official_rebuild_include_breakout": True,
        "official_rebuild_timeout_seconds": 10800,
        "official_rebuild_retry_minutes": 10,
        "last_official_rebuild_date": None,
        "last_official_rebuild_at": None,
        "last_official_rebuild_ok": None,
        "last_official_rebuild_attempt_at": None,
        "last_official_rebuild_failed_at": None,
        "last_official_rebuild_task_id": None,
        "last_official_rebuild_task_status": None,
        "last_official_rebuild_result": None,
        "last_alert_keys": {},
        "last_run_at": None,
        "last_email_sent_at": None,
        "last_heartbeat_sent_at": None,
        "last_blocker_alert_at": None,
        "last_error": None,
        "last_result": None,
    }


def _load_gen2_shadow_monitor_state() -> Dict[str, Any]:
    state = _load_json_file(GEN2_SHADOW_MONITOR_STATE_PATH, _default_gen2_shadow_monitor_state())
    if not isinstance(state, dict):
        state = _default_gen2_shadow_monitor_state()
    base = _default_gen2_shadow_monitor_state()
    base.update(state)
    base["enabled"] = bool(base.get("enabled"))
    base["interval_seconds"] = max(120, _to_int(base.get("interval_seconds"), 120))
    base["pool_rank"] = min(500, max(50, _to_int(base.get("pool_rank"), 200)))
    alpha191_gate = str(base.get("alpha191_gate") or "off").strip().lower()
    base["alpha191_gate"] = alpha191_gate if alpha191_gate in GEN2_ALPHA191_ACTIVE_GATES else "off"
    base["recipient_email"] = str(base.get("recipient_email") or "").strip()
    base["trading_hours_only"] = bool(base.get("trading_hours_only"))
    base["heartbeat_enabled"] = bool(base.get("heartbeat_enabled", True))
    base["heartbeat_email_minutes"] = max(30, _to_int(base.get("heartbeat_email_minutes"), 30))
    base["official_rebuild_enabled"] = bool(base.get("official_rebuild_enabled", True))
    base["official_rebuild_legacy_330"] = bool(base.get("official_rebuild_legacy_330", True))
    base["official_rebuild_include_breakout"] = bool(base.get("official_rebuild_include_breakout", True))
    base["official_rebuild_timeout_seconds"] = max(300, _to_int(base.get("official_rebuild_timeout_seconds"), 10800))
    base["official_rebuild_retry_minutes"] = max(1, _to_int(base.get("official_rebuild_retry_minutes"), 10))
    keys = base.get("last_alert_keys")
    base["last_alert_keys"] = keys if isinstance(keys, dict) else {}
    return base


def _save_gen2_shadow_monitor_state(state: Dict[str, Any]) -> None:
    _save_json_file(GEN2_SHADOW_MONITOR_STATE_PATH, _sanitize(state))


def _default_gen2_strategy_refresh_state() -> Dict[str, Any]:
    return {
        "enabled": True,
        "trading_hours_only": True,
        "data_delay_minutes": 2,
        "run_shadow_monitor": True,
        "run_mainline_hotspots": True,
        "mainline_mode": "sector",
        "mainline_limit": 30,
        "force_each_bar_once": True,
        "last_bar_slot": None,
        "last_run_at": None,
        "last_success_at": None,
        "last_skip_at": None,
        "last_error": None,
        "last_result": None,
        "last_mainline_task_id": None,
    }


def _load_gen2_strategy_refresh_state() -> Dict[str, Any]:
    state = _load_json_file(GEN2_STRATEGY_REFRESH_STATE_PATH, _default_gen2_strategy_refresh_state())
    if not isinstance(state, dict):
        state = _default_gen2_strategy_refresh_state()
    base = _default_gen2_strategy_refresh_state()
    base.update(state)
    base["enabled"] = bool(base.get("enabled"))
    base["trading_hours_only"] = bool(base.get("trading_hours_only"))
    base["data_delay_minutes"] = min(20, max(0, _to_int(base.get("data_delay_minutes"), 2)))
    base["run_shadow_monitor"] = bool(base.get("run_shadow_monitor", True))
    base["run_mainline_hotspots"] = bool(base.get("run_mainline_hotspots", True))
    mode = str(base.get("mainline_mode") or "sector").strip().lower()
    base["mainline_mode"] = mode if mode in {"all", "sector", "theme"} else "sector"
    base["mainline_limit"] = min(120, max(5, _to_int(base.get("mainline_limit"), 30)))
    base["force_each_bar_once"] = bool(base.get("force_each_bar_once", True))
    return base


def _save_gen2_strategy_refresh_state(state: Dict[str, Any]) -> None:
    _save_json_file(GEN2_STRATEGY_REFRESH_STATE_PATH, _sanitize(state))


def _gen2_30m_bar_slot(now: datetime, data_delay_minutes: int = 2) -> Optional[str]:
    if not TradingCalendar.is_trading_day(now):
        return None
    delay = timedelta(minutes=min(20, max(0, int(data_delay_minutes or 0))))
    current = now.time()
    selected: Optional[time] = None
    for close_time in GEN2_30M_BAR_CLOSE_TIMES:
        ready_at = (datetime.combine(now.date(), close_time) + delay).time()
        if current >= ready_at:
            selected = close_time
    if selected is None:
        return None
    return f"{now.strftime('%Y-%m-%d')} {selected.strftime('%H:%M')}"


def _gen2_strategy_refresh_triggers(data_delay_minutes: int) -> OrTrigger:
    delay = timedelta(minutes=min(20, max(0, int(data_delay_minutes or 0))))
    triggers = []
    base_date = date(2000, 1, 1)
    for close_time in GEN2_30M_BAR_CLOSE_TIMES:
        run_at = datetime.combine(base_date, close_time) + delay
        triggers.append(CronTrigger(hour=str(run_at.hour), minute=str(run_at.minute), timezone="Asia/Shanghai"))
    return OrTrigger(triggers)


def _run_gen2_official_latest_rebuild(
    signal_date: str,
    include_breakout: bool = False,
    breakout_only: bool = False,
    legacy_330: bool = False,
    timeout_seconds: int = 10800,
) -> Dict[str, Any]:
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "gen2_rebuild_latest_official_pipeline.py"),
        "--end-date",
        signal_date,
    ]
    if breakout_only:
        cmd.append("--breakout-only")
    elif not include_breakout:
        cmd.append("--skip-breakout")
    if legacy_330:
        cmd.append("--legacy-330")
    started_at = datetime.now()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=max(300, int(timeout_seconds or 10800)),
        )
    except subprocess.TimeoutExpired as exc:
        finished_at = datetime.now()
        return {
            "ok": False,
            "signal_date": signal_date,
            "include_breakout": include_breakout,
            "breakout_only": breakout_only,
            "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": finished_at.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
            "timeout_seconds": max(300, int(timeout_seconds or 10800)),
            "error": "timeout",
            "message": f"G2 latest-driven正式主链重建超时?{max(300, int(timeout_seconds or 5400))}秒）",
            "stdout_tail": "\n".join((exc.stdout or "").splitlines()[-20:]),
            "stderr_tail": "\n".join((exc.stderr or "").splitlines()[-20:]),
            "cmd": cmd,
        }

    finished_at = datetime.now()
    stdout_text = proc.stdout or ""
    stderr_text = proc.stderr or ""
    payload: Dict[str, Any] | None = None
    try:
        parsed = json.loads(stdout_text) if stdout_text.strip() else {}
        if isinstance(parsed, dict):
            payload = parsed
    except Exception:
        payload = None
    payload_ok: Optional[bool] = None
    if isinstance(payload, dict) and "ok" in payload:
        payload_ok = bool(payload.get("ok"))
    result = {
        "ok": proc.returncode == 0 and payload_ok is not False,
        "signal_date": signal_date,
        "include_breakout": include_breakout,
        "breakout_only": breakout_only,
        "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": finished_at.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
        "returncode": proc.returncode,
        "stdout_tail": "\n".join(stdout_text.splitlines()[-20:]),
        "stderr_tail": "\n".join(stderr_text.splitlines()[-20:]),
        "cmd": cmd,
    }
    if payload is not None:
        result["payload"] = payload
        result["completed_steps"] = payload.get("completed_steps")
        if payload_ok is False:
            result["payload_ok"] = False
            result["failed_steps"] = [
                str(item.get("title") or item.get("name") or "")
                for item in payload.get("results", [])
                if isinstance(item, dict) and not bool(item.get("ok"))
            ]
    if not result["ok"]:
        reason = "payload ok=false" if payload_ok is False else f"returncode={proc.returncode}"
        result["message"] = f"G2 latest-driven official rebuild failed: {reason}"
    return result


def _set_gen2_official_rebuild_task(task_id: str, payload: Dict[str, Any]) -> None:
    with GEN2_OFFICIAL_REBUILD_TASK_LOCK:
        task = GEN2_OFFICIAL_REBUILD_TASKS.setdefault(task_id, {})
        task.update(payload)
        task["updated_at"] = datetime.now().isoformat(timespec="seconds")


def _parse_task_datetime(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    normalized = text.replace("T", " ")
    candidates = [normalized, normalized.replace("Z", ""), normalized]
    for fmt in ("%Y-%m-%d %H:%M:%S",):
        for item in candidates:
            try:
                return datetime.strptime(item, fmt)
            except Exception:
                continue
    try:
        return datetime.fromisoformat(normalized.replace("Z", ""))
    except Exception:
        return None


def _mark_gen2_official_rebuild_task_failed(task: Dict[str, Any], reason: str) -> Dict[str, Any]:
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        return {}
    now = datetime.now()
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    start_time = _parse_task_datetime(task.get("started_at") or task.get("created_at")) or now
    timeout_seconds = int(task.get("timeout_seconds") or 10800)
    duration_seconds = round((now - start_time).total_seconds(), 1)
    failed_result = {
        "ok": False,
        "signal_date": str(task.get("signal_date") or ""),
        "include_breakout": bool(task.get("include_breakout")),
        "breakout_only": bool(task.get("breakout_only")),
        "started_at": start_time.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": now_text,
        "duration_seconds": duration_seconds,
        "timeout_seconds": timeout_seconds,
        "error": "stale_or_failed",
        "message": reason,
        "stdout_tail": "",
        "stderr_tail": "",
        "cmd": [],
    }
    _set_gen2_official_rebuild_task(
        task_id,
        {
            "status": "failed",
            "progress": 100,
            "error": reason,
            "message": reason,
            "result": failed_result,
            "completed_at": now_text,
            "duration_seconds": duration_seconds,
        },
    )
    state = _load_gen2_shadow_monitor_state()
    state["last_official_rebuild_at"] = now_text
    state["last_official_rebuild_ok"] = False
    state["last_official_rebuild_task_id"] = task_id
    state["last_official_rebuild_task_status"] = "failed"
    state["last_official_rebuild_result"] = failed_result
    state["last_official_rebuild_failed_at"] = now_text
    _save_gen2_shadow_monitor_state(state)
    return failed_result


def _get_gen2_official_rebuild_task(task_id: str) -> Optional[Dict[str, Any]]:
    with GEN2_OFFICIAL_REBUILD_TASK_LOCK:
        task = GEN2_OFFICIAL_REBUILD_TASKS.get(task_id)
        return dict(task) if task else None


def _find_active_gen2_official_rebuild_task() -> Optional[Dict[str, Any]]:
    with GEN2_OFFICIAL_REBUILD_TASK_LOCK:
        for task in GEN2_OFFICIAL_REBUILD_TASKS.values():
            status = str(task.get("status") or "").strip().lower()
            if status not in {"queued", "running"}:
                continue
            start_time = _parse_task_datetime(task.get("started_at") or task.get("created_at"))
            timeout_seconds = max(300, _to_int(task.get("timeout_seconds"), 10800))
            if start_time and (datetime.now() - start_time).total_seconds() >= timeout_seconds + 120:
                _mark_gen2_official_rebuild_task_failed(task, f"official rebuild task exceeded timeout guard: {timeout_seconds}s")
                continue
            return dict(task)
    return None


def _reconcile_official_rebuild_state_if_needed(
    state: Dict[str, Any], now: Optional[datetime] = None
) -> Optional[Dict[str, Any]]:
    if not isinstance(state, dict):
        return None
    now = now or datetime.now()
    state_status = str(state.get("last_official_rebuild_task_status") or "").strip().lower()
    if state_status not in {"queued", "running"}:
        result = state.get("last_official_rebuild_result")
        return result if isinstance(result, dict) else None

    task_id = str(state.get("last_official_rebuild_task_id") or "").strip()
    if task_id:
        task_payload = _get_gen2_official_rebuild_task(task_id)
        if isinstance(task_payload, dict):
            task_status = str(task_payload.get("status") or "").strip().lower()
            if task_status not in {"queued", "running"}:
                task_result = task_payload.get("result")
                result = task_result if isinstance(task_result, dict) else None
                if not isinstance(result, dict):
                    result = {
                        "ok": False,
                        "signal_date": str(task_payload.get("signal_date") or state.get("last_official_rebuild_date") or ""),
                        "started_at": str(task_payload.get("started_at") or ""),
                        "finished_at": str(task_payload.get("completed_at") or now.strftime("%Y-%m-%d %H:%M:%S")),
                        "duration_seconds": task_payload.get("duration_seconds"),
                        "timeout_seconds": _to_int(task_payload.get("timeout_seconds"), 10800),
                        "error": task_payload.get("error") or "official rebuild task exited",
                        "message": task_payload.get("error") or "official rebuild task exited",
                        "status": task_status or "failed",
                        "task_id": task_id,
                    }

                state["last_official_rebuild_at"] = str(result.get("finished_at") or now.strftime("%Y-%m-%d %H:%M:%S"))
                state["last_official_rebuild_ok"] = bool(result.get("ok"))
                state["last_official_rebuild_task_status"] = task_status or ("completed" if result.get("ok") else "failed")
                state["last_official_rebuild_result"] = result
                if state["last_official_rebuild_task_status"] == "completed":
                    if result.get("signal_date"):
                        state["last_official_rebuild_date"] = str(result.get("signal_date"))
                    state["last_official_rebuild_failed_at"] = None
                else:
                    state["last_official_rebuild_failed_at"] = state["last_official_rebuild_at"]
                _save_gen2_shadow_monitor_state(state)
                return result

    timeout_seconds = max(300, _to_int(state.get("official_rebuild_timeout_seconds"), 10800))
    state_result = state.get("last_official_rebuild_result")
    state_result = state_result if isinstance(state_result, dict) else {}
    started_text = str(
        state_result.get("started_at")
        or state_result.get("created_at")
        or state.get("last_official_rebuild_attempt_at")
        or ""
    ).strip()
    started_at = _parse_task_datetime(started_text)
    if started_at is None:
        return state_result if state_result else None

    if (now - started_at).total_seconds() < timeout_seconds + 120:
        return state_result if state_result else {
            "status": state_status,
            "started_at": started_text,
            "signal_date": str(state.get("last_official_rebuild_date") or ""),
            "ok": None,
        }

    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    failed_result = {
        "ok": False,
        "signal_date": str(state.get("last_official_rebuild_date") or ""),
        "include_breakout": bool(state.get("official_rebuild_include_breakout", True)),
        "breakout_only": False,
        "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": now_text,
        "duration_seconds": round((now - started_at).total_seconds(), 1),
        "timeout_seconds": timeout_seconds,
        "error": "timeout",
        "message": f"official rebuild task exceeded timeout guard: {timeout_seconds}s",
        "stdout_tail": "",
        "stderr_tail": "",
        "cmd": list(state_result.get("cmd", [])) if isinstance(state_result, dict) else [],
        "status": "failed",
        "task_id": str(state.get("last_official_rebuild_task_id") or ""),
    }
    state["last_official_rebuild_at"] = now_text
    state["last_official_rebuild_ok"] = False
    state["last_official_rebuild_task_status"] = "failed"
    state["last_official_rebuild_result"] = failed_result
    state["last_official_rebuild_failed_at"] = now_text
    _save_gen2_shadow_monitor_state(state)
    return failed_result


def _run_gen2_official_rebuild_task(
    task_id: str,
    signal_date: str,
    include_breakout: bool,
    breakout_only: bool,
    legacy_330: bool,
    timeout_seconds: int,
) -> None:
    started_at = datetime.now()
    _set_gen2_official_rebuild_task(
        task_id,
        {
            "task_id": task_id,
            "status": "running",
            "progress": 10,
            "signal_date": signal_date,
            "include_breakout": bool(include_breakout),
            "breakout_only": bool(breakout_only),
            "legacy_330": bool(legacy_330),
            "timeout_seconds": int(timeout_seconds),
            "started_at": started_at.isoformat(timespec="seconds"),
        },
    )
    state = _load_gen2_shadow_monitor_state()
    state["last_official_rebuild_attempt_at"] = started_at.strftime("%Y-%m-%d %H:%M:%S")
    state["last_official_rebuild_task_id"] = task_id
    state["last_official_rebuild_task_status"] = "running"
    _save_gen2_shadow_monitor_state(state)

    result: Dict[str, Any]
    finished_at = datetime.now()
    try:
        result = _run_gen2_official_latest_rebuild(
            signal_date,
            include_breakout=include_breakout,
            breakout_only=breakout_only,
            legacy_330=legacy_330,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        logger.exception("G2 official rebuild task unexpected error")
        finished_at = datetime.now()
        result = {
            "ok": False,
            "signal_date": signal_date,
            "include_breakout": bool(include_breakout),
            "breakout_only": bool(breakout_only),
            "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": finished_at.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
            "timeout_seconds": int(timeout_seconds),
            "error": "exception",
            "message": str(exc),
            "stdout_tail": "",
            "stderr_tail": "",
            "cmd": [],
        }
    finished_at = datetime.now()
    task_payload = {
        "status": "completed" if result.get("ok") else "failed",
        "progress": 100,
        "result": result,
        "completed_at": finished_at.isoformat(timespec="seconds"),
        "duration_seconds": result.get("duration_seconds"),
    }
    if not result.get("ok"):
        task_payload["error"] = result.get("message") or result.get("error") or "official rebuild failed"
    _set_gen2_official_rebuild_task(task_id, task_payload)

    state = _load_gen2_shadow_monitor_state()
    state["last_official_rebuild_at"] = result.get("finished_at") or finished_at.strftime("%Y-%m-%d %H:%M:%S")
    state["last_official_rebuild_ok"] = bool(result.get("ok"))
    state["last_official_rebuild_task_id"] = task_id
    state["last_official_rebuild_task_status"] = "completed" if result.get("ok") else "failed"
    state["last_official_rebuild_result"] = result
    if result.get("ok"):
        state["last_official_rebuild_date"] = signal_date
        state["last_official_rebuild_failed_at"] = None
    else:
        state["last_official_rebuild_failed_at"] = finished_at.strftime("%Y-%m-%d %H:%M:%S")
    _save_gen2_shadow_monitor_state(state)


def _start_gen2_official_rebuild_task(
    signal_date: str,
    include_breakout: bool,
    timeout_seconds: int,
    breakout_only: bool = False,
    legacy_330: bool = False,
) -> Dict[str, Any]:
    active = _find_active_gen2_official_rebuild_task()
    if active:
        return active
    task_id = f"gen2_official_rebuild_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"
    _set_gen2_official_rebuild_task(
        task_id,
        {
            "task_id": task_id,
            "status": "queued",
            "progress": 0,
            "signal_date": signal_date,
            "include_breakout": bool(include_breakout),
            "breakout_only": bool(breakout_only),
            "legacy_330": bool(legacy_330),
            "timeout_seconds": int(timeout_seconds),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    state = _load_gen2_shadow_monitor_state()
    state["last_official_rebuild_attempt_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["last_official_rebuild_task_id"] = task_id
    state["last_official_rebuild_task_status"] = "queued"
    _save_gen2_shadow_monitor_state(state)
    thread = threading.Thread(
        target=_run_gen2_official_rebuild_task,
        args=(task_id, signal_date, bool(include_breakout), bool(breakout_only), bool(legacy_330), int(timeout_seconds)),
        name=f"gen2-official-rebuild-{task_id}",
        daemon=True,
    )
    thread.start()
    return _get_gen2_official_rebuild_task(task_id) or {"task_id": task_id, "status": "queued", "progress": 0}


def _gen2_official_rebuild_summary_text(result: Optional[Dict[str, Any]]) -> str:
    if not isinstance(result, dict) or not result:
        return "官方回补尚未就绪"
    mode_label = "只做breakout" if bool(result.get("breakout_only")) else ("包含breakout" if bool(result.get("include_breakout")) else "主流程")
    task_status = str(result.get("status") or "").strip().lower()
    if task_status in {"queued", "running"}:
        signal_date = str(result.get("signal_date") or "--")
        started_at = str(result.get("started_at") or result.get("created_at") or "").strip()
        phase = "排队中" if task_status == "queued" else "执行中"
        suffix = f"锛屽紑濮嬩簬{started_at}" if started_at else ""
        return f"{mode_label}{phase}，开始时间：{started_at}" if started_at else f"{mode_label}{phase}，信号日：{signal_date}"
    status = "成功" if bool(result.get("ok")) else "失败"
    signal_date = str(result.get("signal_date") or "--")
    duration = result.get("duration_seconds")
    duration_text = f"{duration}秒" if duration is not None else "--"
    completed_steps = result.get("completed_steps")
    if completed_steps is None and isinstance(result.get("payload"), dict):
        completed_steps = result["payload"].get("completed_steps")
    step_text = f"{completed_steps}步" if completed_steps is not None else "--"
    if result.get("error") == "timeout":
        return f"{mode_label}{status}，信号日{signal_date}，在{duration_text}超时"
    return f"{mode_label}{status}，信号日{signal_date}，已完成{step_text}，耗时{duration_text}"


GEN2_SHADOW_VERIFICATION_STATUSES = {
    "": {"label": "未设置", "type": "info"},
    "watch": {"label": "观察", "type": "warning"},
    "paper": {"label": "模拟盘", "type": "primary"},
    "small_buy": {"label": "小单", "type": "success"},
    "skip": {"label": "跳过", "type": "info"},
    "reject": {"label": "驳回", "type": "danger"},
}


def _gen2_shadow_signal_key(item: Dict[str, Any]) -> str:
    entry_date = str(item.get("entry_date") or "").strip()
    code6 = _normalize_stock_code6(item.get("code6") or item.get("code"))
    confirm_datetime = str(item.get("confirm_datetime") or "").strip()
    raw_variant = item.get("alpha191_gate_variant")
    try:
        variant = "" if pd.isna(raw_variant) else str(raw_variant or "").strip()
    except Exception:
        variant = str(raw_variant or "").strip()
    return "|".join([entry_date, code6, confirm_datetime, variant])


def _load_gen2_shadow_verifications() -> Dict[str, Dict[str, Any]]:
    data = _load_json_file(GEN2_SHADOW_VERIFICATION_PATH, {})
    if not isinstance(data, dict):
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            result[str(key)] = value
    return result


def _save_gen2_shadow_verifications(data: Dict[str, Dict[str, Any]]) -> None:
    _save_json_file(GEN2_SHADOW_VERIFICATION_PATH, _sanitize(data))


def _gen2_shadow_verification_meta(status: Any) -> Dict[str, str]:
    status_text = str(status or "").strip()
    meta = GEN2_SHADOW_VERIFICATION_STATUSES.get(status_text, GEN2_SHADOW_VERIFICATION_STATUSES[""])
    return {"status": status_text if status_text in GEN2_SHADOW_VERIFICATION_STATUSES else "", **meta}


def _gen2_shadow_verification_options() -> List[Dict[str, str]]:
    return [
        {"value": key, "label": value["label"], "type": value["type"]}
        for key, value in GEN2_SHADOW_VERIFICATION_STATUSES.items()
    ]


def _apply_gen2_shadow_verifications(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    verifications = _load_gen2_shadow_verifications()
    for row in rows:
        key = _gen2_shadow_signal_key(row)
        item = verifications.get(key, {})
        meta = _gen2_shadow_verification_meta(item.get("status") if isinstance(item, dict) else "")
        row["verification_key"] = key
        row["verification_status"] = meta["status"]
        row["verification_label"] = meta["label"]
        row["verification_type"] = meta["type"]
        row["verification_note"] = str(item.get("note") or "") if isinstance(item, dict) else ""
        row["verification_position_pct"] = _to_float(item.get("position_pct")) if isinstance(item, dict) else None
        row["verification_fill_price"] = _to_float(item.get("fill_price")) if isinstance(item, dict) else None
        row["verification_updated_at"] = str(item.get("updated_at") or "") if isinstance(item, dict) else ""
    return rows


def _avg_numeric(rows: List[Dict[str, Any]], key: str) -> Optional[float]:
    values = [_to_float(row.get(key)) for row in rows]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return float(sum(values) / len(values))


def _build_gen2_shadow_verification_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    marked_rows = [row for row in rows if str(row.get("verification_status") or "").strip()]
    action_rows = [row for row in marked_rows if str(row.get("verification_status") or "") == "small_buy"]
    status_counts: Dict[str, int] = {}
    for row in rows:
        status = str(row.get("verification_status") or "").strip()
        status_counts[status] = int(status_counts.get(status, 0)) + 1

    rejected_rows = [
        row
        for row in marked_rows
        if str(row.get("verification_status") or "").strip() in {"skip", "reject"}
    ]
    positive_5d = sum(1 for row in marked_rows if (_to_float(row.get("fwd5_pct")) or 0) > 0)
    avg_fwd5 = _avg_numeric(marked_rows, "fwd5_pct")
    avg_fwd10 = _avg_numeric(marked_rows, "fwd10_pct")
    avg_fwd20 = _avg_numeric(marked_rows, "fwd20_pct")
    stop_count = sum(1 for row in marked_rows if bool(row.get("stop5_touch_30m")))
    positive_5d_ratio = (positive_5d / len(marked_rows)) if marked_rows else None
    stop5_touch_ratio = (stop_count / len(marked_rows)) if marked_rows else None
    promotion_status = "collecting"
    promotion_type = "info"
    promotion_label = "信号待观察"
    promotion_message = "首次采样中，默认仅观察是否具备小单转大单/结构改善特征，先不要盲目实盘。"

    if len(marked_rows) >= 10:
        avg5_ok = avg_fwd5 is not None and avg_fwd5 > 1.0
        avg10_ok = avg_fwd10 is not None and avg_fwd10 > 1.5
        positive_ok = positive_5d_ratio is not None and positive_5d_ratio >= 0.55
        stop_ok = stop5_touch_ratio is not None and stop5_touch_ratio <= 0.30
        if len(marked_rows) >= 20 and avg10_ok and positive_ok and stop_ok:
            promotion_status = "promote_candidate"
            promotion_type = "success"
            promotion_label = "晋级为正式"
            promotion_message = "样本数量与强度满足标准，建议纳入正式策略观察列。"
        elif avg5_ok and stop_ok:
            promotion_status = "small_live"
            promotion_type = "warning"
            promotion_label = "小单观察"
            promotion_message = "信号较强但放量偏弱，建议先做小仓位实验，不直接加码。"
        elif (avg_fwd5 is not None and avg_fwd5 < 0) or (stop5_touch_ratio is not None and stop5_touch_ratio > 0.40):
            promotion_status = "hold"
            promotion_type = "danger"
            promotion_label = "观望"
            promotion_message = "信号强度偏弱或结构不稳，建议先观望，不建议加仓。"
        else:
            promotion_status = "watch"
            promotion_type = "primary"
            promotion_label = "持续观察"
            promotion_message = "信号样本可持续跟踪，但尚未达到正式转化条件。"

    return _sanitize(
        {
            "total": len(rows),
            "marked": len(marked_rows),
            "unmarked": max(0, len(rows) - len(marked_rows)),
            "action_count": len(action_rows),
            "rejected_count": len(rejected_rows),
            "status_counts": status_counts,
            "avg_marked_fwd5_pct": avg_fwd5,
            "avg_marked_fwd10_pct": avg_fwd10,
            "avg_marked_fwd20_pct": avg_fwd20,
            "positive_5d_count": positive_5d,
            "positive_5d_ratio": positive_5d_ratio,
            "stop5_touch_count": stop_count,
            "stop5_touch_ratio": stop5_touch_ratio,
            "promotion_status": promotion_status,
            "promotion_type": promotion_type,
            "promotion_label": promotion_label,
            "promotion_message": promotion_message,
        }
    )

def _build_gen2_shadow_global_verification_summary(ledger_df: pd.DataFrame) -> Dict[str, Any]:
    if ledger_df.empty:
        return _build_gen2_shadow_verification_summary([])
    rows: List[Dict[str, Any]] = []
    for _, item in ledger_df.iterrows():
        code = str(item.get("code") or "").strip()
        code6 = _normalize_stock_code6(code)
        rows.append(
            {
                "code": _to_exchange_stock_code(code6) if code6 else code,
                "code6": code6,
                "entry_date": str(item.get("entry_date") or ""),
                "confirm_datetime": str(item.get("confirm_datetime") or ""),
                "alpha191_gate_variant": "" if pd.isna(item.get("alpha191_gate_variant")) else str(item.get("alpha191_gate_variant") or ""),
                "fwd5_pct": _pct_value(item.get("outcome_fwd_ret_5d")),
                "fwd10_pct": _pct_value(item.get("outcome_fwd_ret_10d")),
                "fwd20_pct": _pct_value(item.get("outcome_fwd_ret_20d")),
                "stop5_touch_30m": bool(item.get("stop5_touch_30m")),
            }
        )
    return _build_gen2_shadow_verification_summary(_apply_gen2_shadow_verifications(rows))


def _build_gen2_shadow_verification_queue(ledger_df: pd.DataFrame, limit: int = 12) -> List[Dict[str, Any]]:
    if ledger_df.empty:
        return []
    rows: List[Dict[str, Any]] = []
    for _, item in ledger_df.iterrows():
        code = str(item.get("code") or "").strip()
        code6 = _normalize_stock_code6(code)
        fwd5 = _pct_value(item.get("outcome_fwd_ret_5d"))
        fwd10 = _pct_value(item.get("outcome_fwd_ret_10d"))
        fwd20 = _pct_value(item.get("outcome_fwd_ret_20d"))
        if fwd5 is None and fwd10 is None and fwd20 is None:
            continue
        status = str(item.get("shadow_status") or "").strip()
        if status not in {"observable", "executed"}:
            continue
        rows.append(
            {
                "code": _to_exchange_stock_code(code6) if code6 else code,
                "code6": code6,
                "name": str(item.get("name") or ""),
                "entry_date": str(item.get("entry_date") or ""),
                "confirm_datetime": str(item.get("confirm_datetime") or ""),
                "shadow_status": status,
                "status_label": _gen2_shadow_status_label(status),
                "can_observe": True,
                "is_suspended": False,
                "day_signal_rank": _to_int(item.get("day_signal_rank"), default=0) or None,
                "entry_price": _to_float(item.get("entry_price")),
                "v4_rank": _to_int(item.get("v4_rank"), default=0) or None,
                "alpha191_gate_variant": "" if pd.isna(item.get("alpha191_gate_variant")) else str(item.get("alpha191_gate_variant") or ""),
                "alpha191_gate_score": _to_float(item.get("alpha191_gate_score")),
                "fwd5_pct": fwd5,
                "fwd10_pct": fwd10,
                "fwd20_pct": fwd20,
                "stop5_touch_30m": bool(item.get("stop5_touch_30m")),
                "reason_text": str(item.get("execution_note") or "").strip(),
            }
        )
    rows = _apply_gen2_shadow_verifications(rows)
    rows = [row for row in rows if not str(row.get("verification_status") or "").strip()]
    rows.sort(
        key=lambda row: (
            str(row.get("entry_date") or ""),
            str(row.get("confirm_datetime") or ""),
            -(_to_int(row.get("day_signal_rank"), default=999) or 999),
        ),
        reverse=True,
    )
    return _sanitize(rows[: max(1, int(limit))])


def save_gen2_shadow_verification(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}
    key = str(payload.get("verification_key") or "").strip()
    if not key:
        key = _gen2_shadow_signal_key(payload)
    if not key or key.count("|") < 3:
        return {"ok": False, "error": "missing verification_key"}

    status = str(payload.get("status") or "").strip()
    if status not in GEN2_SHADOW_VERIFICATION_STATUSES:
        return {"ok": False, "error": "unsupported verification status"}

    verifications = _load_gen2_shadow_verifications()
    if not status and not str(payload.get("note") or "").strip() and _to_float(payload.get("position_pct")) is None and _to_float(payload.get("fill_price")) is None:
        verifications.pop(key, None)
        _save_gen2_shadow_verifications(verifications)
        meta = _gen2_shadow_verification_meta("")
        return {"ok": True, "verification_key": key, **meta}

    item = {
        "status": status,
        "note": str(payload.get("note") or "").strip(),
        "position_pct": _to_float(payload.get("position_pct")),
        "fill_price": _to_float(payload.get("fill_price")),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    verifications[key] = item
    _save_gen2_shadow_verifications(verifications)
    meta = _gen2_shadow_verification_meta(status)
    return _sanitize({"ok": True, "verification_key": key, **meta, **item})


def _load_v4_signal_snapshot() -> Dict[str, Any]:
    data = _load_json_file(V4_MONITOR_SIGNAL_PATH, {})
    return data if isinstance(data, dict) else {}


def _save_v4_signal_snapshot(data: Dict[str, Any]) -> None:
    _save_json_file(V4_MONITOR_SIGNAL_PATH, _sanitize(data))


def _load_ths_capital_holdings_cache() -> Dict[str, Any]:
    data = _load_json_file(THS_CAPITAL_HOLDINGS_CACHE_PATH, {})
    return data if isinstance(data, dict) else {}


def _is_ths_capital_holdings_cache_fresh(data: Dict[str, Any], max_age_minutes: int = 30) -> bool:
    synced_at = str((data or {}).get("synced_at") or "").strip()
    if not synced_at:
        return False
    try:
        ts = datetime.fromisoformat(synced_at.replace("/", "-"))
    except Exception:
        try:
            ts = datetime.strptime(synced_at[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return False
    return (datetime.now() - ts).total_seconds() <= max_age_minutes * 60


def _ths_capital_holdings_cache_response(exc: Exception, message_prefix: str) -> Dict[str, Any]:
    cached = _load_ths_capital_holdings_cache()
    if not cached:
        return {"ok": False, "message": f"{message_prefix}: {exc}"}
    fresh = _is_ths_capital_holdings_cache_fresh(cached)
    payload = {
        **cached,
        "ok": bool(fresh),
        "fallback_cache": True,
        "stale_cache": not fresh,
        "message": f"{message_prefix}，{'已返回最近缓存' if fresh else '缓存已过期，禁止作为真实账户同步结果'}: {exc}",
    }
    return _sanitize(payload)


def _save_ths_capital_holdings_cache(data: Dict[str, Any]) -> None:
    _save_json_file(THS_CAPITAL_HOLDINGS_CACHE_PATH, _sanitize(data))


def _ensure_v4_monitor_scheduler() -> BackgroundScheduler:
    global _v4_monitor_scheduler
    if _v4_monitor_scheduler is not None:
        return _v4_monitor_scheduler
    with _v4_monitor_scheduler_lock:
        if _v4_monitor_scheduler is not None:
            return _v4_monitor_scheduler
        sched = BackgroundScheduler(timezone="Asia/Shanghai")
        sched.start()
        _v4_monitor_scheduler = sched
    return _v4_monitor_scheduler


def _monitor_state_for_row(row: Dict[str, Any]) -> str:
    pnl = _to_float(row.get("pnl_ratio"))
    discipline = "正常"
    if pnl is not None and pnl <= -6:
        discipline = "严控"
    elif pnl is not None and pnl <= -4:
        discipline = "警惕"
    s15 = bool(((row.get("rsi15_signal") or {}).get("detected")))
    s30 = bool(((row.get("rsi30_signal") or {}).get("detected")))
    risk = str(row.get("risk_level") or "")
    suggestion = str(row.get("suggestion") or "")
    return f"{discipline}|{int(s15)}|{int(s30)}|{risk}|{suggestion}"


def _load_smtp_config_for_monitor(recipient_override: Optional[str] = None) -> Dict[str, Any]:
    email_cfg = app_config.get("email", default=None, config_file="settings.yaml") or {}
    if email_cfg.get("enabled", True) is False:
        raise RuntimeError("email.enabled 未启用，无法发送邮件")
    smtp_server = str(email_cfg.get("smtp_server") or "").strip()
    smtp_port = _to_int(email_cfg.get("smtp_port"), 0)
    smtp_user = str(email_cfg.get("smtp_user") or "").strip()
    smtp_password = str(email_cfg.get("smtp_password") or "").strip()
    from_email = str(email_cfg.get("from_email") or "").strip()
    to_emails = email_cfg.get("to_emails") or []
    if isinstance(to_emails, str):
        to_emails = [to_emails]
    to_emails = [str(x).strip() for x in to_emails if str(x).strip()]
    if recipient_override and "@" in recipient_override:
        to_emails = [recipient_override]
    if not all([smtp_server, smtp_port, smtp_user, smtp_password, from_email, to_emails]):
        raise RuntimeError("SMTP 配置不完整，请先补全 settings.yaml 邮件配置")
    return {
        "server": smtp_server,
        "port": smtp_port,
        "username": smtp_user,
        "password": smtp_password,
        "from_email": from_email,
        "to_emails": to_emails,
        "use_ssl": bool(email_cfg.get("use_ssl")) or smtp_port == 465,
    }


def _smtp_attempt_plans(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    plans: List[Dict[str, Any]] = []
    port = _to_int(cfg.get("port"), 0)
    use_ssl = bool(cfg.get("use_ssl"))
    if use_ssl:
        plans.append({"mode": "ssl", "port": port})
        if port == 465:
            plans.append({"mode": "starttls", "port": 587})
    else:
        if port == 587:
            plans.append({"mode": "starttls", "port": port})
        plans.append({"mode": "plain", "port": port})
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for item in plans:
        key = (item.get("mode"), item.get("port"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _smtp_login_probe(cfg: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    for plan in _smtp_attempt_plans(cfg):
        server = None
        try:
            mode = str(plan.get("mode") or "")
            port = _to_int(plan.get("port"), 0)
            if mode == "ssl":
                server = smtplib.SMTP_SSL(cfg["server"], port, timeout=30)
            else:
                server = smtplib.SMTP(cfg["server"], port, timeout=30)
                server.ehlo()
                if mode == "starttls":
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
            server.login(cfg["username"], cfg["password"])
            return {"ok": True, "mode": mode, "port": port, "errors": errors}
        except Exception as exc:
            errors.append(f"{plan.get('mode')}:{plan.get('port')} -> {exc}")
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    try:
                        server.close()
                    except Exception:
                        pass
    return {"ok": False, "mode": "", "port": None, "errors": errors}


def _open_monitor_smtp_server(cfg: Dict[str, Any]) -> tuple[Any, Dict[str, Any]]:
    errors: List[str] = []
    for plan in _smtp_attempt_plans(cfg):
        server = None
        try:
            mode = str(plan.get("mode") or "")
            port = _to_int(plan.get("port"), 0)
            if mode == "ssl":
                server = smtplib.SMTP_SSL(cfg["server"], port, timeout=30)
            else:
                server = smtplib.SMTP(cfg["server"], port, timeout=30)
                server.ehlo()
                if mode == "starttls":
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
            server.login(cfg["username"], cfg["password"])
            return server, {"mode": mode, "port": port}
        except Exception as exc:
            errors.append(f"{plan.get('mode')}:{plan.get('port')} -> {exc}")
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    try:
                        server.close()
                    except Exception:
                        pass
    raise RuntimeError("SMTP send failed: " + " | ".join(errors))


def _send_monitor_email(subject: str, body: str, recipient_override: Optional[str] = None) -> None:
    cfg = _load_smtp_config_for_monitor(recipient_override)
    msg = MIMEMultipart()
    msg["From"] = cfg["from_email"]
    msg["To"] = ",".join(cfg["to_emails"])
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    server, _send_meta = _open_monitor_smtp_server(cfg)
    try:
        server.sendmail(cfg["from_email"], cfg["to_emails"], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass


def _run_v4_manual_holdings_monitor(force_send: bool = False, recipient_override: Optional[str] = None) -> Dict[str, Any]:
    state = _load_v4_monitor_state()
    now = datetime.now()
    if state.get("trading_hours_only"):
        if not TradingCalendar.is_trading_day(now):
            state["last_run_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
            _save_v4_monitor_state(state)
            return {"ok": True, "message": "休市时段，先跳过执行", "email_sent": False, "changes": []}
        current_t = now.time()
        in_morning = time(9, 30) <= current_t <= time(11, 30)
        in_afternoon = time(13, 0) <= current_t <= time(15, 0)
        if not (in_morning or in_afternoon):
            state["last_run_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
            _save_v4_monitor_state(state)
            return {"ok": True, "message": "交易时间外，先缓存等待", "email_sent": False, "changes": []}
    holdings = state.get("holdings") or []
    if not holdings:
        state["last_run_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        _save_v4_monitor_state(state)
        return {"ok": False, "message": "当前无持仓，跳过巡检", "email_sent": False, "changes": []}

    resp = refresh_v4_manual_holdings_all({"holdings": holdings})
    rows = resp.get("rows") or []
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    prev = _load_v4_signal_snapshot()
    prev_states = prev.get("states") if isinstance(prev, dict) else {}
    prev_states = prev_states if isinstance(prev_states, dict) else {}
    next_states: Dict[str, str] = {}
    changes: List[Dict[str, Any]] = []
    alert_rows: List[Dict[str, Any]] = []
    quiet_minutes = max(0, _to_int(state.get("quiet_minutes"), 15))
    quiet_seconds = quiet_minutes * 60
    last_alert_by_code = state.get("last_alert_by_code") if isinstance(state.get("last_alert_by_code"), dict) else {}
    for row in rows:
        code = str(row.get("code") or "").strip()[:6]
        if not code:
            continue
        status_key = _monitor_state_for_row(row)
        next_states[code] = status_key
        before = str(prev_states.get(code) or "")
        has_alert = ("|1|" in status_key) or status_key.startswith("绂佸姞浠搢") or status_key.startswith("搴斿鐞唡")
        if has_alert:
            alert_rows.append(
                {
                    "code": code,
                    "name": str(row.get("name") or ""),
                    "pnl_ratio": row.get("pnl_ratio"),
                    "risk_level": row.get("risk_level"),
                    "rsi15": bool(((row.get("rsi15_signal") or {}).get("detected"))),
                    "rsi30": bool(((row.get("rsi30_signal") or {}).get("detected"))),
                    "suggestion": str(row.get("suggestion") or ""),
                }
            )
        should_alert = bool((force_send or before != status_key) and has_alert)
        if should_alert and not force_send and quiet_seconds > 0:
            last_alert_at = str(last_alert_by_code.get(code) or "").strip()
            try:
                if last_alert_at:
                    last_dt = datetime.strptime(last_alert_at, "%Y-%m-%d %H:%M:%S")
                    if (now - last_dt).total_seconds() < quiet_seconds:
                        should_alert = False
            except Exception:
                pass
        if should_alert:
            changes.append(
                {
                    "code": code,
                    "name": str(row.get("name") or ""),
                    "pnl_ratio": row.get("pnl_ratio"),
                    "risk_level": row.get("risk_level"),
                    "rsi15": bool(((row.get("rsi15_signal") or {}).get("detected"))),
                    "rsi30": bool(((row.get("rsi30_signal") or {}).get("detected"))),
                    "suggestion": str(row.get("suggestion") or ""),
                }
            )

    _save_v4_signal_snapshot({"updated_at": now_text, "states": next_states})
    email_sent = False
    repeat_minutes = max(60, _to_int(state.get("repeat_reminder_minutes"), 60))
    repeat_seconds = repeat_minutes * 60
    reminder_active = bool(state.get("reminder_active"))
    if changes:
        subject = f"实盘持仓监控信号提醒 {now_text[:16]}"
        lines = [f"更新时间:{now_text}", f"通知数量:{len(changes)}", ""]
        for idx, item in enumerate(changes, start=1):
            pnl_text = "--" if _to_float(item.get("pnl_ratio")) is None else f"{float(item.get('pnl_ratio')):.2f}%"
            lines.append(
                f"{idx}. {item.get('code')} {item.get('name')} | 盈亏 {pnl_text} | 风险 {item.get('risk_level')} | 15m背离={item.get('rsi15')} 30m背离={item.get('rsi30')}"
            )
            lines.append(f"   建议：{item.get('suggestion')}")
        _send_monitor_email(subject, "\n".join(lines).strip() + "\n", recipient_override=recipient_override or state.get("recipient_email"))
        email_sent = True
        for item in changes:
            code = str(item.get("code") or "").strip()[:6]
            if code:
                last_alert_by_code[code] = now_text
        state["reminder_active"] = True
        if not state.get("reminder_activated_at"):
            state["reminder_activated_at"] = now_text

    if (not changes) and alert_rows and reminder_active:
        repeat_due = False
        last_email_text = str(state.get("last_email_sent_at") or "").strip()
        if last_email_text:
            try:
                last_email_dt = datetime.strptime(last_email_text, "%Y-%m-%d %H:%M:%S")
                repeat_due = (now - last_email_dt).total_seconds() >= repeat_seconds
            except Exception:
                repeat_due = True
        else:
            repeat_due = True
        if repeat_due:
            subject = f"实盘持仓监控持续提醒(每{repeat_minutes}分钟) {now_text[:16]}"
            lines = [
                f"更新时间:{now_text}",
                "提醒类型：持续提醒（条件未解除，霢手动处理持仓列表后停止）",
                f"通知数量:{len(alert_rows)}",
                "",
            ]
            for idx, item in enumerate(alert_rows, start=1):
                pnl_text = "--" if _to_float(item.get("pnl_ratio")) is None else f"{float(item.get('pnl_ratio')):.2f}%"
                lines.append(
                    f"{idx}. {item.get('code')} {item.get('name')} | 盈亏 {pnl_text} | 风险 {item.get('risk_level')} | 15m背离={item.get('rsi15')} 30m背离={item.get('rsi30')}"
                )
                lines.append(f"   建议：{item.get('suggestion')}")
            _send_monitor_email(
                subject,
                "\n".join(lines).strip() + "\n",
                recipient_override=recipient_override or state.get("recipient_email"),
            )
            email_sent = True

    if not alert_rows:
        state["reminder_active"] = False
        state["reminder_activated_at"] = None

    state["last_run_at"] = now_text
    if email_sent:
        state["last_email_sent_at"] = now_text
    state["last_error"] = None
    state["last_alert_by_code"] = last_alert_by_code
    _save_v4_monitor_state(state)
    return {"ok": True, "message": "监控执行完成", "email_sent": email_sent, "changes": changes}


def _monitor_job_wrapper() -> None:
    state = _load_v4_monitor_state()
    if not state.get("enabled"):
        return
    try:
        _run_v4_manual_holdings_monitor(force_send=False)
    except Exception as exc:
        state["last_run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["last_error"] = str(exc)
        _save_v4_monitor_state(state)
        logger.warning(f"v4 monitor job failed: {exc}")


def _configure_v4_monitor_scheduler() -> Dict[str, Any]:
    scheduler = _ensure_v4_monitor_scheduler()
    state = _load_v4_monitor_state()
    try:
        scheduler.remove_job(_v4_monitor_job_id)
    except Exception:
        pass
    if state.get("enabled"):
        interval_seconds = max(30, _to_int(state.get("interval_seconds"), 60))
        scheduler.add_job(
            _monitor_job_wrapper,
            trigger=IntervalTrigger(seconds=interval_seconds, timezone="Asia/Shanghai"),
            id=_v4_monitor_job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=20,
        )
    job = scheduler.get_job(_v4_monitor_job_id)
    return {
        "enabled": bool(state.get("enabled")),
        "interval_seconds": int(state.get("interval_seconds") or 60),
        "trading_hours_only": bool(state.get("trading_hours_only")),
        "quiet_minutes": int(state.get("quiet_minutes") or 0),
        "repeat_reminder_minutes": int(state.get("repeat_reminder_minutes") or 60),
        "reminder_active": bool(state.get("reminder_active")),
        "reminder_activated_at": state.get("reminder_activated_at"),
        "recipient_email": str(state.get("recipient_email") or ""),
        "next_run_time": str(job.next_run_time) if job and job.next_run_time else None,
        "last_run_at": state.get("last_run_at"),
        "last_email_sent_at": state.get("last_email_sent_at"),
        "last_error": state.get("last_error"),
    }


def init_v4_manual_holdings_monitor_scheduler_from_config() -> Dict[str, Any]:
    return _configure_v4_monitor_scheduler()


def _is_trading_session_now(now: datetime) -> bool:
    if not TradingCalendar.is_trading_day(now):
        return False
    current_t = now.time()
    in_morning = time(9, 30) <= current_t <= time(11, 30)
    in_afternoon = time(13, 0) <= current_t <= time(15, 0)
    return bool(in_morning or in_afternoon)


def _run_gen2_live_shadow_update_sync(signal_date: str, pool_rank: int, alpha191_gate: str = "off") -> Dict[str, Any]:
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "gen2_update_live_shadow.py"),
        "--signal-date",
        signal_date,
        "--pool-rank",
        str(pool_rank),
        "--alpha191-gate",
        str(alpha191_gate or "off"),
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"returncode={proc.returncode}")[-1200:]
        raise RuntimeError(f"G2 shadow update failed: {detail}")
    try:
        payload = json.loads(proc.stdout or "{}")
    except Exception:
        payload = {}
    payload["returncode"] = proc.returncode
    return payload


def _read_csv_max_trade_date(path: Path, date_col: str) -> str:
    if not path.exists():
        return ""
    try:
        df = pd.read_csv(path, usecols=[date_col])
    except Exception:
        return ""
    if df.empty:
        return ""
    dates = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
    return str(dates.max()) if len(dates) else ""


def _resolve_gen2_signal_date(signal_date: str, allow_fallback: bool = True) -> Dict[str, Any]:
    requested = _normalize_date_str(signal_date)
    if not requested:
        return {
            "ok": False,
            "requested": None,
            "effective": None,
            "using_fallback": False,
            "reason": "invalid signal date",
        }

    status = _source_csv_date_rows_status(
        GEN2_OPEN_STATE_DAILY_PATH,
        "trade_date",
        requested,
        "timing_regime",
        "g2_open_state_daily",
        min_rows=1,
    )
    if bool(status.get("ok")):
        return {
            "ok": True,
            "requested": requested,
            "effective": requested,
            "using_fallback": False,
            "status": status,
        }

    latest = _read_csv_max_trade_date(GEN2_OPEN_STATE_DAILY_PATH, "trade_date")
    if not latest:
        return {
            "ok": False,
            "requested": requested,
            "effective": None,
            "using_fallback": False,
            "status": status,
            "reason": "g2_open_state_daily has no usable data yet",
        }

    if allow_fallback and latest < requested:
        fallback_status = _source_csv_date_rows_status(
            GEN2_OPEN_STATE_DAILY_PATH,
            "trade_date",
            latest,
            "timing_regime",
            "g2_open_state_daily",
            min_rows=1,
        )
        if bool(fallback_status.get("ok")):
            return {
                "ok": True,
                "requested": requested,
                "effective": latest,
                "using_fallback": True,
                "status": fallback_status,
                "reason": f"auto fallback to latest available open-state date: {latest}",
            }

    return {
        "ok": False,
        "requested": requested,
        "effective": None,
        "using_fallback": False,
        "status": status,
        "reason": "g2_open_state_daily requested date still unavailable after status check",
    }


def _run_subprocess_checked(cmd: List[str], label: str, timeout_seconds: int) -> Dict[str, Any]:
    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
    )
    stdout_text = (proc.stdout or "").strip()
    stderr_text = (proc.stderr or "").strip()
    if proc.returncode != 0:
        detail = (stderr_text or stdout_text or f"returncode={proc.returncode}")[-2500:]
        raise RuntimeError(f"{label} failed ({proc.returncode}): {detail}")
    return {"returncode": proc.returncode, "stdout": stdout_text, "stderr": stderr_text}


def _ensure_gen2_open_state_daily_upto_date(signal_date: str) -> Dict[str, Any]:
    return _ensure_gen2_open_state_daily_upto_date_impl(signal_date, allow_fallback=True)


def _ensure_gen2_open_state_daily_upto_date_impl(signal_date: str, allow_fallback: bool = True) -> Dict[str, Any]:
    normalized = _normalize_date_str(signal_date)
    if not normalized:
        return {"ok": False, "attempted": False, "message": "Invalid signal date for auto rebuild", "status": None}

    status = _source_csv_date_rows_status(
        GEN2_OPEN_STATE_DAILY_PATH,
        "trade_date",
        normalized,
        "timing_regime",
        "g2_open_state_daily",
        min_rows=1,
    )
    if bool(status.get("ok")):
        return {
            "ok": True,
            "attempted": False,
            "message": "g2_open_state_daily already has required rows",
            "status": status,
            "details": {},
        }

    now = datetime.now()
    timing_file = GEN2_OPEN_STATE_TIMING_DIR / "index_features.csv"
    timing_file_status = (
        f"timing_max_date={_read_csv_max_trade_date(timing_file, 'trade_date')}" if timing_file.exists() else "timing_file_missing"
    )

    with GEN2_OPEN_STATE_REBUILD_LOCK:
        if _GEN2_OPEN_STATE_REBUILD_STATE.get("running"):
            return {
                "ok": False,
                "attempted": False,
                "running": True,
                "message": "G2 open-state rebuild already running",
                "status": status,
                "details": {"timing_file_status": timing_file_status},
            }
        last_error_at = _GEN2_OPEN_STATE_REBUILD_STATE.get("last_error_at")
        if last_error_at is not None:
            try:
                if (now - last_error_at).total_seconds() < GEN2_OPEN_STATE_REBUILD_MAX_COOLDOWN_SECONDS:
                    return {
                        "ok": False,
                        "attempted": False,
                        "message": "G2 open-state rebuild skipped due recent failure cooldown",
                        "status": status,
                        "details": {"cooldown_seconds": GEN2_OPEN_STATE_REBUILD_MAX_COOLDOWN_SECONDS, "timing_file_status": timing_file_status},
                    }
            except Exception:
                logger.exception("Failed evaluating G2 open-state rebuild cooldown window")
        _GEN2_OPEN_STATE_REBUILD_STATE["running"] = True
        _GEN2_OPEN_STATE_REBUILD_STATE["target_date"] = normalized
        _GEN2_OPEN_STATE_REBUILD_STATE["started_at"] = now

    timing_cmd = [
        sys.executable,
        str(GEN2_OPEN_STATE_TIMING_SCRIPT_PATH),
        "--start-date",
        GEN2_OPEN_STATE_TIMING_START_DATE,
        "--end-date",
        normalized,
        "--output-dir",
        str(GEN2_OPEN_STATE_TIMING_DIR),
    ]
    build_cmd = [
        sys.executable,
        str(GEN2_OPEN_STATE_RESEARCH_SCRIPT_PATH),
        "--timing-dir",
        str(GEN2_OPEN_STATE_TIMING_DIR),
        "--event-dataset",
        str(GEN2_V4_EVENT_DATASET_PATH),
        "--output-dir",
        str(GEN2_OPEN_STATE_RESEARCH_DIR),
        "--factor-mode",
        "proxy",
    ]

    details = {
        "target_date": normalized,
        "timing_file_status": timing_file_status,
        "timing_command": " ".join(timing_cmd),
        "build_command": " ".join(build_cmd),
    }
    try:
        _run_subprocess_checked(timing_cmd, "g2_timing_research", GEN2_OPEN_STATE_REBUILD_TIMEOUT_SECONDS // 2)
        build_result = _run_subprocess_checked(build_cmd, "g2_open_state_research", GEN2_OPEN_STATE_REBUILD_TIMEOUT_SECONDS // 2)
        details["build_stdout_tail"] = str(build_result.get("stdout", ""))[-1000:]
        details["build_stderr_tail"] = str(build_result.get("stderr", ""))[-1000:]
        updated_status = _source_csv_date_rows_status(
            GEN2_OPEN_STATE_DAILY_PATH,
            "trade_date",
            normalized,
            "timing_regime",
            "g2_open_state_daily",
            min_rows=1,
        )
        ok = bool(updated_status.get("ok"))
        with GEN2_OPEN_STATE_REBUILD_LOCK:
            _GEN2_OPEN_STATE_REBUILD_STATE["running"] = False
            _GEN2_OPEN_STATE_REBUILD_STATE["attempted_at"] = datetime.now()
            _GEN2_OPEN_STATE_REBUILD_STATE["last_error"] = None
            _GEN2_OPEN_STATE_REBUILD_STATE["last_error_at"] = None
            if ok:
                _GEN2_OPEN_STATE_REBUILD_STATE["success_at"] = datetime.now()
                _GEN2_OPEN_STATE_REBUILD_STATE["target_date"] = normalized
        if ok:
            return {
                "ok": True,
                "attempted": True,
                "message": "G2 open-state rebuild completed",
                "status": updated_status,
                "effective": normalized,
                "details": details,
            }
        fallback_date = _read_csv_max_trade_date(GEN2_OPEN_STATE_DAILY_PATH, "trade_date")
        if allow_fallback and fallback_date and fallback_date < normalized:
            fallback_status = _source_csv_date_rows_status(
                GEN2_OPEN_STATE_DAILY_PATH,
                "trade_date",
                fallback_date,
                "timing_regime",
                "g2_open_state_daily",
                min_rows=1,
            )
            if bool(fallback_status.get("ok")):
                return {
                    "ok": True,
                    "attempted": True,
                "message": f"G2 open-state fallback to latest available date {fallback_date}",
                "status": fallback_status,
                "effective": fallback_date,
                "details": {**details, "fallback_reason": "target_date_not_ready_after_rebuild", "fallback_date": fallback_date},
            }
        return {
            "ok": False,
            "attempted": True,
            "message": "G2 open-state rebuild finished but target date still missing",
            "status": updated_status,
            "effective": None,
            "details": details,
        }
    except Exception as exc:
        logger.exception("G2 open-state auto rebuild failed")
        with GEN2_OPEN_STATE_REBUILD_LOCK:
            _GEN2_OPEN_STATE_REBUILD_STATE["running"] = False
            _GEN2_OPEN_STATE_REBUILD_STATE["attempted_at"] = datetime.now()
            _GEN2_OPEN_STATE_REBUILD_STATE["last_error"] = str(exc)
            _GEN2_OPEN_STATE_REBUILD_STATE["last_error_at"] = datetime.now()
        return {
            "ok": False,
            "attempted": True,
            "message": f"G2 open-state auto rebuild failed: {exc}",
            "status": status,
            "effective": None,
            "details": details,
        }


def _collect_gen2_promotion_snapshots(signal_date: str, pool_rank: int) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "ok": True,
        "signal_date": signal_date,
        "target_count": 0,
        "rows": 0,
        "source": "",
        "index_target_codes": ["999999.SH"],
        "index_rows": 0,
        "index_source": "",
        "index_snapshot_time": "",
        "error": "",
    }
    try:
        from scripts.collect_intraday_snapshots import collect_global_index_snapshot, collect_global_stock_snapshot
        from scripts.gen2_update_live_shadow import build_g2_promotion_snapshot_codes

        targets = build_g2_promotion_snapshot_codes(signal_date, pool_rank=pool_rank, limit=300)
        codes = [str(item.get("code") or "").strip() for item in targets if item.get("code")]
        codes = sorted({code for code in codes if code})
        result["target_count"] = len(codes)
        stock_snapshot: Dict[str, Any] = {}
        if codes:
            stock_snapshot = collect_global_stock_snapshot(target_codes=codes) or {}
            result.update(
                {
                    "rows": _to_int(stock_snapshot.get("rows"), 0),
                    "source": str(stock_snapshot.get("source") or ""),
                    "snapshot_time": str(stock_snapshot.get("snapshot_time") or ""),
                    "elapsed_sec": stock_snapshot.get("elapsed_sec"),
                    "errors": stock_snapshot.get("errors") or [],
                }
            )
        index_snapshot = collect_global_index_snapshot(target_codes=result["index_target_codes"]) or {}
        result.update(
            {
                "index_rows": _to_int(index_snapshot.get("rows"), 0),
                "index_source": str(index_snapshot.get("source") or ""),
                "index_snapshot_time": str(index_snapshot.get("snapshot_time") or ""),
                "index_elapsed_sec": index_snapshot.get("elapsed_sec"),
            }
        )
        result["ok"] = (not codes or result["rows"] > 0) and result["index_rows"] > 0
        if not result["ok"]:
            problems = []
            if codes and result["rows"] <= 0:
                problems.append("promotion stock snapshot returned zero rows")
            if result["index_rows"] <= 0:
                problems.append("index snapshot returned zero rows")
            result["error"] = "; ".join(problems)
    except Exception as exc:
        logger.warning(f"collect gen2 promotion snapshots failed: {exc}")
        result.update({"ok": False, "error": str(exc)})
    return result


def _load_gen2_shadow_monitor_candidates(signal_date: str) -> List[Dict[str, Any]]:
    ledger_df = _load_gen2_risk_cool_shadow_ledger()
    requested_date = _normalize_date_str(signal_date) if signal_date else None
    if not requested_date:
        return []

    candidate_source = "shadow_ledger"
    day_df = pd.DataFrame()
    if not ledger_df.empty and "entry_date" in ledger_df.columns:
        day_df = ledger_df[ledger_df["entry_date"] == requested_date].copy()
    if day_df.empty:
        day_df = _load_gen2_live_update_frame(requested_date, "filtered_signals")
        candidate_source = "live_filtered_signals"
        if not day_df.empty:
            day_df["shadow_status"] = "observable"
            if "day_signal_rank" not in day_df.columns:
                if "alpha191_volume5_rank_in_day" in day_df.columns:
                    day_df["day_signal_rank"] = pd.to_numeric(day_df["alpha191_volume5_rank_in_day"], errors="coerce")
                elif "v4_rank" in day_df.columns:
                    day_df["day_signal_rank"] = pd.to_numeric(day_df["v4_rank"], errors="coerce")
            if "execution_note" not in day_df.columns:
                day_df["execution_note"] = day_df.get("signal_family", "live filtered G2 signal")

    if day_df.empty:
        return []
    if "shadow_status" in day_df.columns:
        day_df["shadow_status"] = day_df["shadow_status"].map(lambda x: str(x or "").strip())
        day_df = day_df[day_df["shadow_status"].isin(["observable", "executed"])]
    else:
        return []
    if day_df.empty:
        return []
    if "day_signal_rank" not in day_df.columns:
        day_df["day_signal_rank"] = np.nan
    if "confirm_datetime" not in day_df.columns:
        day_df["confirm_datetime"] = ""
    if "code" not in day_df.columns and "code6" in day_df.columns:
        day_df["code"] = day_df["code6"]
    day_df = day_df.sort_values(["shadow_status", "day_signal_rank", "confirm_datetime"], na_position="last")
    rows: List[Dict[str, Any]] = []
    for _, item in day_df.iterrows():
        code = _normalize_stock_code6(item.get("code"))
        if not code:
            continue
        status = str(item.get("shadow_status") or "").strip()
        rows.append(
            {
                "alert_key": "|".join(
                    [
                        signal_date,
                        code,
                        str(item.get("confirm_datetime") or ""),
                        status,
                    ]
                ),
                "code": _to_exchange_stock_code(code),
                "code6": code,
                "name": str(item.get("name") or ""),
                "confirm_datetime": str(item.get("confirm_datetime") or ""),
                "shadow_status": status,
                "status_label": _gen2_shadow_status_label(status),
                "entry_price": _to_float(item.get("entry_price")),
                "v4_rank": _to_int(item.get("v4_rank"), default=0) or None,
                "v4_score": _to_float(item.get("v4_score")),
                "rt_return_pct": _pct_value(item.get("rt_return_from_d1_close")),
                "amount_ratio": _to_float(item.get("rt_30m_amount_ratio")),
                "reason_text": str(item.get("execution_note") or "").strip(),
                "candidate_source": candidate_source,
            }
        )
    return rows


def _load_gen2_pending_shadow_alerts_for_latest_trade_date(state: Dict[str, Any]) -> Dict[str, Any]:
    last_alert_keys = state.get("last_alert_keys") if isinstance(state.get("last_alert_keys"), dict) else {}
    latest_date = _normalize_date_str(_resolve_latest_stock_trade_date())
    if not latest_date:
        return {"signal_date": "", "rows": [], "new_rows": [], "last_alert_keys": last_alert_keys, "update_result": {}}

    dates: List[str] = []
    if GEN2_OPEN_STATE_DAILY_PATH.exists():
        try:
            state_df = pd.read_csv(GEN2_OPEN_STATE_DAILY_PATH, usecols=["trade_date"])
            state_dates = pd.to_datetime(state_df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
            dates = sorted([str(value) for value in state_dates.dropna().unique() if str(value) <= latest_date])[-7:]
        except Exception as exc:
            logger.warning(f"load gen2 open state dates for shadow alert backfill failed: {exc}")
            dates = []
    if not dates:
        live_dates = []
        for date_text in _gen2_live_update_dates():
            try:
                date_obj = datetime.strptime(date_text, "%Y-%m-%d")
            except Exception:
                continue
            if date_text <= latest_date and TradingCalendar.is_trading_day(date_obj):
                live_dates.append(date_text)
        dates = sorted(set(live_dates + [latest_date]))[-7:]

    rows: List[Dict[str, Any]] = []
    update_result = {
        "raw_candidates": 0,
        "filtered_signals": 0,
        "shadow_rows_for_date": 0,
        "backfill_dates": dates,
    }
    for date_text in dates:
        day_rows = _load_gen2_shadow_monitor_candidates(date_text)
        rows.extend(day_rows)
        summary = _load_gen2_live_update_summary(date_text)
        if summary:
            update_result["raw_candidates"] += _to_int(summary.get("raw_candidates"), default=0) or 0
            update_result["filtered_signals"] += _to_int(summary.get("filtered_signals"), default=len(day_rows)) or 0
            update_result["shadow_rows_for_date"] += _to_int(summary.get("shadow_rows_for_date"), default=len(day_rows)) or 0
        else:
            update_result["filtered_signals"] += len(day_rows)
            update_result["shadow_rows_for_date"] += len(day_rows)

    new_rows = [row for row in rows if row.get("alert_key") not in last_alert_keys]
    if not dates:
        signal_date = latest_date
    elif len(dates) == 1:
        signal_date = dates[0]
    else:
        signal_date = f"{dates[0]}..{dates[-1]}"
    return {
        "signal_date": signal_date,
        "rows": rows,
        "new_rows": new_rows,
        "last_alert_keys": last_alert_keys,
        "update_result": update_result,
    }


def _format_optional_number(value: Any, digits: int = 2, suffix: str = "") -> str:
    val = _to_float(value)
    if val is None:
        return "--"
    return f"{val:.{digits}f}{suffix}"


def _send_gen2_shadow_buy_email(
    now_text: str,
    signal_date: str,
    update_result: Dict[str, Any],
    rows: List[Dict[str, Any]],
    recipient_override: Optional[str] = None,
) -> None:
    subject = f"G2 shadow buy signals {signal_date} {now_text[11:16]}"
    lines = [
        f"Generated at: {now_text}",
        f"Signal date: {signal_date}",
        (
            "Candidates: "
            f"{update_result.get('raw_candidates', '--')}, "
            f"Filtered: {update_result.get('filtered_signals', '--')}, "
            f"Shadow rows: {update_result.get('shadow_rows_for_date', '--')}"
        ),
        f"Signals built: {len(rows)}",
        "",
    ]
    for idx, item in enumerate(rows, start=1):
        lines.append(
            (
                f"{idx}. {item.get('code')} {item.get('name')} | "
                f"{item.get('status_label') or item.get('shadow_status')} | "
                f"Confirm {item.get('confirm_datetime') or '--'} | "
                f"Entry {_format_optional_number(item.get('entry_price'), 3)} | "
                f"V4 rank {item.get('v4_rank') or '--'} | "
                f"RT return {_format_optional_number(item.get('rt_return_pct'), 2, '%')} | "
                f"30m amount ratio {_format_optional_number(item.get('amount_ratio'), 2)}"
            )
        )
        reason = str(item.get("reason_text") or "").strip()
        if reason:
            lines.append(f"   Reason: {reason}")
    _send_monitor_email(subject, "\n".join(lines).strip() + "\n", recipient_override=recipient_override)


def _gen2_monitor_market_gate_summary(signal_date: Optional[str] = None) -> Dict[str, Any]:
    try:
        gate = get_v4_market_gate(signal_date)
        if not isinstance(gate, dict):
            return {"available": False, "can_open": False, "text": "Market gate data invalid"}
        source = str(gate.get("source") or "daily_close")
        source_label = "Intraday snapshot" if source == "intraday_quote_snapshot" else "Signal-date daily close"
        close = _to_float(gate.get("close"))
        ma20 = _to_float(gate.get("ma20"))
        date_text = str(gate.get("trade_date") or "--")
        snapshot_time = str(gate.get("snapshot_time") or "").strip()
        prefix = "允许交易" if gate.get("can_open") else "停止交易"
        detail = (
            f"{date_text} {source_label} close={_format_optional_number(close, 2)} / "
            f"MA20={_format_optional_number(ma20, 2)}"
        )
        if snapshot_time:
            detail += f" / sample_time={snapshot_time}"
        return {
            "available": bool(gate.get("available")),
            "can_open": bool(gate.get("can_open")),
            "text": f"{prefix}: {detail}",
            "raw": gate,
        }
    except Exception as exc:
        logger.warning(f"build gen2 monitor market gate summary failed: {exc}")
        return {"available": False, "can_open": False, "text": f"Market gate failed: {exc}"}


def _gen2_monitor_row_line(row: Dict[str, Any], idx: int) -> str:
    code = str(row.get("code") or row.get("code6") or "--")
    name = str(row.get("name") or "")
    tags: List[str] = []
    if row.get("buyable") or row.get("pass_official_v2_live"):
        tags.append("可交易")
    if row.get("pass_mainline1") or str(row.get("source_family") or "") == "volume5":
        tags.append("volume5")
    if row.get("pass_mainline2") or row.get("pass_breakout_stage") or str(row.get("source_family") or "") == "big_bull":
        stage = row.get("breakout_stage_no")
        if stage:
            tags.append(f"阶段{stage}确认")
    if row.get("pass_v4_g2_trigger"):
        tags.append("触发")
    if row.get("pass_volume5_quality") and "volume5" not in tags:
        tags.append("volume5质量")
    tag_text = "/".join(tags) if tags else str(row.get("stage_label") or row.get("quality_family_label") or "观察")
    parts = [f"{idx}. {code} {name}".strip(), f"[{tag_text}]"]
    v4_rank = row.get("v4_rank")
    if v4_rank:
        parts.append(f"V4 {v4_rank}")
    vol_rank = row.get("alpha191_volume5_rank_in_day")
    if vol_rank:
        parts.append(f"volume5排名 {vol_rank}")
    rt = row.get("rt_return_pct")
    if rt is not None:
        parts.append(f"实时 {_format_optional_number(rt, 2, '%')}")
    amount_ratio = row.get("amount_ratio")
    if amount_ratio is not None:
        parts.append(f"30m增幅 {_format_optional_number(amount_ratio, 2)}")
    l3 = row.get("l3_rt_strong3_ratio")
    if l3 is not None:
        parts.append(f"L3占比 {_format_optional_number(float(l3) * 100, 2, '%')}")
    price = row.get("entry_price")
    if price is not None:
        parts.append(f"入场价{_format_optional_number(price, 3)}")
    reason = str(row.get("reason_text") or row.get("g2_v2_buy_logic") or "").strip()
    if reason:
        parts.append(f"说明:{reason[:80]}")
    return " | ".join(parts)

def _gen2_monitor_selection_digest(signal_date: str, limit: int = 8) -> Dict[str, Any]:
    try:
        pool = _build_gen2_selection_pool(signal_date, max(30, limit * 3))
        if not isinstance(pool, dict) or not pool.get("available"):
            return {"available": False, "rows": [], "text": str((pool or {}).get("message") or "策略选股池不可用")}
        rows: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for section in ["complete_rows", "trigger_rows", "quality_rows", "rows"]:
            for row in pool.get(section) or []:
                if not isinstance(row, dict):
                    continue
                code_key = str(row.get("code6") or _normalize_stock_code6(row.get("code")) or row.get("key") or "")
                if not code_key or code_key in seen:
                    continue
                if section == "quality_rows" and not (
                    row.get("pass_quality") or row.get("pass_breakout_stage") or row.get("quality_family") == "breakout"
                ):
                    continue
                seen.add(code_key)
                rows.append(row)
                if len(rows) >= limit:
                    break
            if len(rows) >= limit:
                break
        summary = {
            "row_count": pool.get("row_count"),
            "trigger_count": pool.get("trigger_count"),
            "complete_count": pool.get("complete_count"),
            "quality_pass_count": pool.get("quality_pass_count"),
            "breakout_stage3_count": pool.get("breakout_quality_stage3_count"),
            "breakout_stage4_count": pool.get("breakout_quality_stage4_count"),
        }
        return {
            "available": True,
            "rows": rows,
            "summary": summary,
            "branch_freshness": pool.get("branch_freshness") or {},
            "raw": pool,
        }
    except Exception as exc:
        logger.warning(f"build gen2 monitor selection digest failed: {exc}")
        return {"available": False, "rows": [], "text": f"策略候读取失败：{exc}"}


def _send_gen2_shadow_heartbeat_email(
    now_text: str,
    signal_date: str,
    update_result: Dict[str, Any],
    rows: List[Dict[str, Any]],
    blocker_result: Dict[str, Any],
    recipient_override: Optional[str] = None,
) -> None:
    market_gate = _gen2_monitor_market_gate_summary(signal_date)
    selection_digest = _gen2_monitor_selection_digest(signal_date, limit=8)
    branch_freshness = selection_digest.get("branch_freshness") if isinstance(selection_digest, dict) else {}
    official_rebuild = blocker_result.get("official_rebuild") if isinstance(blocker_result, dict) else None

    can_open = bool(market_gate.get("available") and market_gate.get("can_open"))
    open_text = "允许交易" if can_open else "停止交易"
    subject = f"G2实盘监控预警 {signal_date} {now_text[11:16]} {open_text}"

    stage_summary = blocker_result.get("pipeline_stages") or []
    failed_stages = [item for item in stage_summary if isinstance(item, dict) and not bool(item.get("ok", True))]
    stage_text = ", ".join(
        [f"{item.get('stage')}: {item.get('passed', 0)}/{item.get('total', 0)}" for item in stage_summary if isinstance(item, dict)]
    )

    blockers = blocker_result.get("blockers") or []
    wait_only_rebuild = _is_gen2_official_rebuild_wait_only(blocker_result)
    display_blockers = [] if wait_only_rebuild else blockers
    promotion_snapshot = blocker_result.get("promotion_snapshot") or {}
    official_text = _gen2_official_rebuild_summary_text(official_rebuild)

    lines = [
        f"当前时间: {now_text}",
        f"交易日期: {signal_date}",
        "",
        f"市场状态: {market_gate.get('text')}",
        f"交易时间可见度: {'正常' if can_open else '休市或异常'}",
        f"选股数据: 原始={update_result.get('raw_candidates', '--')}，筛选={update_result.get('filtered_signals', '--')}，监控={update_result.get('shadow_rows_for_date', '--')}，已选={len(rows)}",
        f"阻断项: {len(display_blockers)}" + ("（官方重建等待中，不按阻断处理）" if wait_only_rebuild else ""),
        f"阶段通过: {stage_text or '--'}",
        f"官方重建: {official_text}",
        f"分支新鲜度: {(branch_freshness or {}).get('text') or '--'}",
        "",
    ]

    formal_rows = rows[:5]
    lines.extend(["=== 候选池(前5) ==="])
    if formal_rows:
        for idx, item in enumerate(formal_rows, start=1):
            lines.append(_gen2_monitor_row_line(item, idx))
    else:
        lines.append("- 无可交易行数")

    lines.extend(["", "=== 分支预览 ==="])
    near_rows = selection_digest.get("rows") or []
    if near_rows:
        for idx, item in enumerate(near_rows, start=1):
            lines.append(_gen2_monitor_row_line(item, idx))
    else:
        lines.append(f"- {selection_digest.get('text') if isinstance(selection_digest, dict) else '无更多分支预览'}")

    if failed_stages:
        lines.extend(["", "=== 异常阶段 ==="])
        for idx, item in enumerate(failed_stages[:5], start=1):
            lines.append(f"{idx}. {item.get('stage')}: {item.get('passed', 0)}/{item.get('total', 0)}")

    lines.append("")
    lines.append("说明: 这是自动生成的心跳邮件，便于观察交易信号与数据有效性。")
    _send_monitor_email(subject, "\n".join(lines).strip() + "\n", recipient_override=recipient_override)

def _should_send_gen2_heartbeat(state: Dict[str, Any], now: datetime, force_send: bool = False) -> bool:
    if force_send:
        return True
    if not state.get("heartbeat_enabled", True):
        return False
    last_text = str(state.get("last_heartbeat_sent_at") or "").strip()
    if not last_text:
        return True
    try:
        last_dt = datetime.strptime(last_text, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return True
    if last_dt.date() != now.date():
        return True
    interval_minutes = max(30, _to_int(state.get("heartbeat_email_minutes"), 30))
    return (now - last_dt).total_seconds() >= interval_minutes * 60


def _latest_required_bar_time(now: datetime, period_minutes: int) -> Optional[str]:
    if not _is_trading_session_now(now):
        return None
    if period_minutes == 30:
        checkpoints = ["10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00"]
    elif period_minutes == 15:
        checkpoints = [
            "09:45",
            "10:00",
            "10:15",
            "10:30",
            "10:45",
            "11:00",
            "11:15",
            "11:30",
            "13:15",
            "13:30",
            "13:45",
            "14:00",
            "14:15",
            "14:30",
            "14:45",
            "15:00",
        ]
    else:
        return None
    ready = []
    for item in checkpoints:
        bar_dt = datetime.strptime(f"{now.strftime('%Y-%m-%d')} {item}:00", "%Y-%m-%d %H:%M:%S")
        if now >= bar_dt + timedelta(minutes=4):
            ready.append(item)
    return ready[-1] if ready else None


def _snapshot_bucket_end(ts: datetime, period_minutes: int) -> datetime:
    anchor = ts.replace(hour=9, minute=30, second=0, microsecond=0)
    if ts <= anchor:
        return anchor
    period = max(1, int(period_minutes))
    elapsed_minutes = (ts - anchor).total_seconds() / 60.0
    slots = int((elapsed_minutes + period - 1e-9) // period)
    return anchor + timedelta(minutes=slots * period)


def _gen2_intraday_snapshot_status(signal_date: str, required_dt: datetime, period_minutes: int, min_codes: int = 3000) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "table": "intraday_quote_snapshot",
        "ok": False,
        "reason": "",
        "code_count": 0,
        "row_count": 0,
        "max_datetime": "",
        "max_bar_datetime": "",
        "min_codes": int(min_codes),
    }
    if not clickhouse_table_exists("intraday_quote_snapshot"):
        result["reason"] = "intraday_quote_snapshot table missing"
        return result
    try:
        df = clickhouse_query_df(
            """
            SELECT
                count() AS row_count,
                uniqExact(code) AS code_count,
                max(snapshot_time) AS max_datetime
            FROM intraday_quote_snapshot
            WHERE snapshot_date = ?::DATE
              AND asset_type = 'stock'
            """,
            [signal_date],
        )
    except Exception as exc:
        result["reason"] = f"read intraday_quote_snapshot failed: {exc}"
        return result
    if df is None or df.empty:
        result["reason"] = "intraday_quote_snapshot has no rows"
        return result
    row = df.iloc[0]
    row_count = _to_int(row.get("row_count"), 0)
    code_count = _to_int(row.get("code_count"), 0)
    max_dt = pd.to_datetime(row.get("max_datetime"), errors="coerce")
    result["row_count"] = row_count
    result["code_count"] = code_count
    result["max_datetime"] = "" if pd.isna(max_dt) else max_dt.strftime("%Y-%m-%d %H:%M:%S")
    if pd.isna(max_dt):
        result["reason"] = "intraday_quote_snapshot max_datetime is null"
    else:
        max_bar_dt = _snapshot_bucket_end(max_dt.to_pydatetime(), int(period_minutes))
        result["max_bar_datetime"] = max_bar_dt.strftime("%Y-%m-%d %H:%M:%S")

    if not result["reason"] and result.get("max_bar_datetime") and datetime.strptime(str(result["max_bar_datetime"]), "%Y-%m-%d %H:%M:%S") < required_dt:
        result["reason"] = (
            f"intraday_quote_snapshot reached {result['max_datetime']}, but required bar is {required_dt.strftime('%Y-%m-%d %H:%M:%S')}"
        )
    elif not result["reason"] and code_count < int(min_codes):
        result["reason"] = f"intraday_quote_snapshot snapshot coverage insufficient, code_count={code_count}, min_codes={int(min_codes)}"
    else:
        result["ok"] = True
    return result


def _gen2_minute_table_status(signal_date: str, period_minutes: int, now: datetime) -> Dict[str, Any]:
    table_name = f"kline_minute_{period_minutes}"
    required_time = _latest_required_bar_time(now, period_minutes)
    result: Dict[str, Any] = {
        "table": table_name,
        "period_minutes": period_minutes,
        "required_time": required_time,
        "ok": True,
        "reason": "",
        "code_count": 0,
        "row_count": 0,
        "max_datetime": "",
    }
    if required_time is None:
        return result
    if not clickhouse_available():
        result.update({"ok": False, "reason": "ClickHouse unavailable"})
        return result
    if not clickhouse_table_exists(table_name):
        result.update({"ok": False, "reason": f"{table_name} does not exist"})
        return result
    try:
        df = clickhouse_query_df(
            f"""
            SELECT
                count() AS row_count,
                uniqExact(code) AS code_count,
                max(datetime) AS max_datetime
            FROM {table_name}
            WHERE toDate(datetime) = ?::DATE
            """,
            [signal_date],
        )
    except Exception as exc:
        result.update({"ok": False, "reason": f"read {table_name} failed: {exc}"})
        return result
    if df is None or df.empty:
        result.update({"ok": False, "reason": f"{table_name} has no rows"})
        return result
    row = df.iloc[0]
    row_count = _to_int(row.get("row_count"), 0)
    code_count = _to_int(row.get("code_count"), 0)
    max_dt = pd.to_datetime(row.get("max_datetime"), errors="coerce")
    result["row_count"] = row_count
    result["code_count"] = code_count
    result["max_datetime"] = "" if pd.isna(max_dt) else max_dt.strftime("%Y-%m-%d %H:%M:%S")
    required_dt = datetime.strptime(f"{signal_date} {required_time}:00", "%Y-%m-%d %H:%M:%S")
    table_reason = ""
    if pd.isna(max_dt):
        table_reason = f"{table_name} max_datetime is null"
    elif max_dt.to_pydatetime() < required_dt:
        table_reason = f"{table_name} max_datetime={result['max_datetime']} behind required {required_dt.strftime('%Y-%m-%d %H:%M:%S')}"
    elif code_count < 3000:
        table_reason = f"{table_name} code coverage insufficient, code_count={code_count}"
    if table_reason:
        snapshot_status = _gen2_intraday_snapshot_status(signal_date, required_dt, period_minutes, min_codes=3000)
        result["snapshot_status"] = snapshot_status
        if snapshot_status.get("ok"):
            result.update(
                {
                    "ok": True,
                    "reason": "",
                    "source": "intraday_quote_snapshot",
                    "snapshot_max_datetime": snapshot_status.get("max_datetime"),
                    "snapshot_code_count": snapshot_status.get("code_count"),
                    "snapshot_row_count": snapshot_status.get("row_count"),
                }
            )
        else:
            snapshot_reason = str(snapshot_status.get("reason") or "").strip()
            result.update({"ok": False, "reason": f"{table_reason}, fallback {snapshot_reason}"})
    return result

def _previous_stock_trade_date(signal_date: str) -> Optional[str]:
    normalized = _normalize_date_str(signal_date)
    if not normalized:
        return None
    sql = text(
        """
        SELECT MAX(k.trade_date) AS prev_date
        FROM kline_daily k
        JOIN stocks s ON s.code = k.code
        WHERE k.trade_date < :signal_date
          AND s.type = 'stock'
          AND s.quit = 0
          AND (s.st = 0 OR s.st IS NULL)
        """
    )
    try:
        with db.engine.connect() as conn:
            prev = conn.execute(sql, {"signal_date": normalized}).scalar()
        return _normalize_date_str(prev)
    except Exception as exc:
        logger.warning(f"resolve previous stock trade date failed: signal_date={signal_date}, error={exc}")
        return None


def _v4_event_dataset_status(trade_date: str, required_role: str, min_rows: int = 50) -> Dict[str, Any]:
    normalized = _normalize_date_str(trade_date)
    path = GEN2_V4_EVENT_DATASET_PATH
    result: Dict[str, Any] = {
        "type": "v4_event_dataset",
        "role": required_role,
        "trade_date": normalized,
        "path": str(path),
        "ok": True,
        "message": "",
        "row_count": 0,
        "min_rows": int(min_rows),
    }
    if not normalized:
        result.update({"ok": False, "message": f"{required_role} V4 event date is empty"})
        return result
    if not path.exists():
        result.update({"ok": False, "message": f"{required_role} V4 event dataset missing: {path}"})
        return result
    try:
        df = pd.read_parquet(path, columns=["trade_date", "code"])
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        row_count = int(df["trade_date"].eq(normalized).sum())
        result["row_count"] = row_count
        if row_count < int(min_rows):
            result.update({"ok": False, "message": f"{required_role} V4 event rows too low: {normalized}, rows={row_count}, min={min_rows}"})
    except Exception as exc:
        result.update({"ok": False, "message": f"{required_role} V4 event dataset read failed: {normalized}, {exc}"})
    return result


def _pipeline_check(stage: str, name: str, ok: bool, message: str = "", **extra: Any) -> Dict[str, Any]:
    return {"stage": stage, "name": name, "ok": bool(ok), "message": str(message or ""), **extra}


def _file_pipeline_status(stage: str, name: str, path: Path, min_bytes: int = 1) -> Dict[str, Any]:
    exists = path.exists()
    size = path.stat().st_size if exists and path.is_file() else 0
    ok = bool(exists and (not path.is_file() or size >= int(min_bytes)))
    msg = "" if ok else f"{name} missing or too small: {path}"
    return _pipeline_check(stage, name, ok, msg, path=str(path), size=size)


def _directory_writable_status(stage: str, name: str, path: Path) -> Dict[str, Any]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        ok = os.access(path, os.W_OK)
        return _pipeline_check(stage, name, ok, "" if ok else f"{name} is not writable: {path}", path=str(path))
    except Exception as exc:
        return _pipeline_check(stage, name, False, f"{name} writable check failed: {exc}", path=str(path))


def _clickhouse_source_status(signal_date: str, prev_trade_date: Optional[str]) -> List[Dict[str, Any]]:
    stage = "data_source"
    checks: List[Dict[str, Any]] = []
    available = clickhouse_available()
    checks.append(_pipeline_check(stage, "clickhouse_available", available, "" if available else "ClickHouse unavailable"))
    if not available:
        return checks
    for table in ["kline_daily", "kline_minute_15", "kline_minute_30"]:
        exists = clickhouse_table_exists(table)
        checks.append(_pipeline_check(stage, f"{table}_exists", exists, "" if exists else f"{table} does not exist", table=table))
    if prev_trade_date:
        checks.append(_daily_stock_coverage_status(prev_trade_date, "D-1 daily source"))
    checks.append(_daily_stock_coverage_status(signal_date, "D-day daily source"))
    return checks


def _daily_stock_coverage_status(trade_date: str, name: str, min_codes: int = 3000) -> Dict[str, Any]:
    stage = "data_source"
    normalized = _normalize_date_str(trade_date)
    if not normalized:
        return _pipeline_check(stage, name, False, f"{name} trade_date empty")
    try:
        df = clickhouse_query_df(
            """
            SELECT count() AS row_count, uniqExact(code) AS code_count
            FROM kline_daily
            WHERE trade_date = ?::DATE
            """,
            [normalized],
        )
        row = df.iloc[0] if df is not None and not df.empty else {}
        row_count = _to_int(row.get("row_count"), 0)
        code_count = _to_int(row.get("code_count"), 0)
        ok = code_count >= int(min_codes)
        return _pipeline_check(
            stage,
            name,
            ok,
            "" if ok else f"{name} coverage too low: {normalized}, codes={code_count}, min={min_codes}",
            trade_date=normalized,
            row_count=row_count,
            code_count=code_count,
            min_codes=int(min_codes),
        )
    except Exception as exc:
        return _pipeline_check(stage, name, False, f"{name} coverage read failed: {exc}", trade_date=normalized)


def _sector_mapping_status() -> Dict[str, Any]:
    stage = "selection_context"
    if not clickhouse_available():
        return _pipeline_check(stage, "sector_mapping", False, "ClickHouse unavailable for sector mapping")
    try:
        df = clickhouse_query_df(
            """
            SELECT count() AS rows, uniqExact(stock_code) AS stock_count, uniqExact(sector_code) AS sector_count
            FROM sector_stocks
            """
        )
        row = df.iloc[0] if df is not None and not df.empty else {}
        rows = _to_int(row.get("rows"), 0)
        stock_count = _to_int(row.get("stock_count"), 0)
        sector_count = _to_int(row.get("sector_count"), 0)
        ok = rows > 0 and stock_count >= 3000 and sector_count > 0
        return _pipeline_check(
            stage,
            "sector_mapping",
            ok,
            "" if ok else f"sector mapping abnormal: rows={rows}, stocks={stock_count}, sectors={sector_count}",
            rows=rows,
            stock_count=stock_count,
            sector_count=sector_count,
        )
    except Exception as exc:
        return _pipeline_check(stage, "sector_mapping", False, f"sector mapping read failed: {exc}")


def _source_date_rows_status(path: Path, date_col: str, trade_date: str, stage: str, name: str, min_rows: int = 1) -> Dict[str, Any]:
    normalized = _normalize_date_str(trade_date)
    if not path.exists():
        return _pipeline_check(stage, name, False, f"{name} missing: {path}", path=str(path), trade_date=normalized)
    try:
        df = pd.read_parquet(path, columns=[date_col])
        dates = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
        row_count = int(dates.eq(normalized).sum()) if normalized else 0
        max_date = str(dates.max()) if len(dates) else ""
        ok = row_count >= int(min_rows)
        return _pipeline_check(
            stage,
            name,
            ok,
            "" if ok else f"{name} rows too low for {normalized}: rows={row_count}, min={min_rows}, max_date={max_date}",
            path=str(path),
            trade_date=normalized,
            row_count=row_count,
            min_rows=int(min_rows),
            max_date=max_date,
        )
    except Exception as exc:
        return _pipeline_check(stage, name, False, f"{name} read failed: {exc}", path=str(path), trade_date=normalized)


def _source_csv_date_rows_status(path: Path, date_col: str, trade_date: str, stage: str, name: str, min_rows: int = 1) -> Dict[str, Any]:
    normalized = _normalize_date_str(trade_date)
    if not path.exists():
        return _pipeline_check(stage, name, False, f"{name} missing: {path}", path=str(path), trade_date=normalized)
    try:
        df = pd.read_csv(path, usecols=[date_col])
        dates = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
        row_count = int(dates.eq(normalized).sum()) if normalized else 0
        max_date = str(dates.max()) if len(dates) else ""
        ok = row_count >= int(min_rows)
        return _pipeline_check(
            stage,
            name,
            ok,
            "" if ok else f"{name} rows too low for {normalized}: rows={row_count}, min={min_rows}, max_date={max_date}",
            path=str(path),
            trade_date=normalized,
            row_count=row_count,
            min_rows=int(min_rows),
            max_date=max_date,
        )
    except Exception as exc:
        return _pipeline_check(stage, name, False, f"{name} read failed: {exc}", path=str(path), trade_date=normalized)


def _alpha191_artifact_status(signal_date: str, prev_trade_date: Optional[str], alpha191_gate: str) -> List[Dict[str, Any]]:
    stage = "scoring"
    gate = str(alpha191_gate or "off").strip().lower()
    checks = [_pipeline_check(stage, "alpha191_gate", gate in GEN2_ALPHA191_ACTIVE_GATES, "" if gate in GEN2_ALPHA191_ACTIVE_GATES else f"unsupported gate: {gate}", gate=gate)]
    if gate in {"volume5_keep80_runup", "g2_v2_complete"}:
        checks.append(_file_pipeline_status(stage, "alpha191_train_source", GEN2_ALPHA191_TRAIN_SOURCE_PATH, min_bytes=1024))
        checks.append(_file_pipeline_status(stage, "alpha191_train_values", GEN2_ALPHA191_TRAIN_VALUES_PATH, min_bytes=1024))
    return checks


def _notification_config_status(recipient_override: Optional[str] = None) -> Dict[str, Any]:
    stage = "notification"
    try:
        cfg = _load_smtp_config_for_monitor(recipient_override)
        probe = _smtp_login_probe(cfg)
        if not probe.get("ok"):
            return _pipeline_check(
                stage,
                "smtp_config",
                False,
                "SMTP login probe failed: " + " | ".join(probe.get("errors") or []),
                to_emails=cfg.get("to_emails"),
                server=cfg.get("server"),
                port=cfg.get("port"),
            )
        return _pipeline_check(
            stage,
            "smtp_config",
            True,
            "",
            to_emails=cfg.get("to_emails"),
            server=cfg.get("server"),
            port=cfg.get("port"),
            send_mode=probe.get("mode"),
            effective_port=probe.get("port"),
        )
    except Exception as exc:
        return _pipeline_check(stage, "smtp_config", False, f"SMTP config unavailable: {exc}")


def _stage_summary(checks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in checks:
        grouped.setdefault(str(item.get("stage") or "unknown"), []).append(item)
    return [
        {
            "stage": stage,
            "ok": all(bool(item.get("ok")) for item in items),
            "failed": [item for item in items if not item.get("ok")],
            "checks": items,
        }
        for stage, items in grouped.items()
    ]


def _freshness_pipeline_checks(signal_date: str, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    freshness = build_gen2_data_freshness(signal_date, signal_date)
    sources = freshness.get("sources") or {}
    checks: List[Dict[str, Any]] = []
    normalized_signal_date = _normalize_date_str(signal_date) or signal_date
    freshness_required_date = normalized_signal_date
    if now is not None and normalized_signal_date == _normalize_date_str(_resolve_latest_stock_trade_date()) and _is_trading_session_now(now):
        freshness_required_date = _previous_stock_trade_date(normalized_signal_date) or normalized_signal_date

    def _max_date(key: str) -> str:
        return str((sources.get(key) or {}).get("max_date") or "")

    latest_daily = _max_date("v4_event_dataset")
    latest_trigger = _max_date("g2_30m_triggers")
    latest_valid = _max_date("valid_signal_target")
    latest_risk_cool = _max_date("risk_cool_base")
    latest_shadow = _max_date("shadow_ledger")

    def _g2_30m_trigger_fresh_check() -> Dict[str, Any]:
        trigger_is_fresh = bool(latest_trigger and latest_trigger >= freshness_required_date)
        source_status: Optional[Dict[str, Any]] = None
        source_is_fresh = False
        if not trigger_is_fresh and now is not None:
            source_status = _gen2_minute_table_status(normalized_signal_date, 30, now)
            source_is_fresh = bool(source_status.get("ok"))
        ok = bool(trigger_is_fresh or source_is_fresh)
        if trigger_is_fresh:
            message = ""
        elif source_is_fresh:
            message = ""
        else:
            source_reason = ""
            if source_status:
                source_reason = str(source_status.get("reason") or "").strip()
            message = (
                f"G2 30m trigger source stale: latest={latest_trigger or '--'}, "
                f"required>={freshness_required_date}"
                + (f"; 30m source unavailable: {source_reason}" if source_reason else "")
            )
        return _pipeline_check(
            "strategy",
            "g2_30m_trigger_fresh",
            ok,
            message,
            latest_date=latest_trigger,
            required_date=freshness_required_date,
            requested_date=normalized_signal_date,
            source_fallback_used=bool((not trigger_is_fresh) and source_is_fresh),
            source_status=source_status or {},
        )

    def _has_new_official_trigger_rows(after_date: str) -> bool:
        path = report_path("gen2_30m_fractal_restart_realistic_d1_w2", "fractal_triggers.parquet")
        if not path.exists():
            return False
        try:
            df = pd.read_parquet(path, columns=["entry_date", "pattern", "trigger_type"])
            df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
            return bool(
                (
                    df["entry_date"].gt(str(after_date or ""))
                    & df["pattern"].eq(str(GEN2_OPEN_RULE_V1.get("pattern") or ""))
                    & df["trigger_type"].eq(str(GEN2_OPEN_RULE_V1.get("trigger_type") or ""))
                ).any()
            )
        except Exception:
            return False

    def _has_new_rows(path: Path, after_date: str, is_csv: bool = False) -> bool:
        if not path.exists():
            return False
        try:
            if is_csv:
                df = pd.read_csv(path, usecols=["entry_date"])
            else:
                df = pd.read_parquet(path, columns=["entry_date"])
            dates = pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
            return bool(dates.gt(str(after_date or "")).any())
        except Exception:
            return False

    def _rebuilt_after(upstream: Path, downstream: Path) -> bool:
        try:
            if not upstream.exists() or not downstream.exists():
                return False
            return downstream.stat().st_mtime >= upstream.stat().st_mtime
        except Exception:
            return False

    checks.append(
        _pipeline_check(
            "selection",
            "v4_event_dataset_fresh",
            bool(latest_daily and latest_daily >= signal_date),
            "" if latest_daily and latest_daily >= signal_date else f"V4 event dataset stale: latest={latest_daily or '--'}, required>={signal_date}",
            latest_date=latest_daily,
            required_date=signal_date,
        )
    )
    checks.append(_g2_30m_trigger_fresh_check())
    checks.append(
        _pipeline_check(
            "strategy",
            "valid_signal_target_fresh",
            bool(
                (latest_valid and latest_valid >= freshness_required_date)
                or not _has_new_official_trigger_rows(latest_valid)
                or _rebuilt_after(GEN2_OPEN_SIGNAL_SOURCE_PATH, GEN2_VALID_SIGNAL_TARGET_PATH)
            ),
            ""
            if (latest_valid and latest_valid >= freshness_required_date)
            or not _has_new_official_trigger_rows(latest_valid)
            or _rebuilt_after(GEN2_OPEN_SIGNAL_SOURCE_PATH, GEN2_VALID_SIGNAL_TARGET_PATH)
            else f"G2 valid-signal target stale: latest={latest_valid or '--'}, required propagation after new official trigger rows",
            latest_date=latest_valid,
            required_date=freshness_required_date,
            requested_date=normalized_signal_date,
        )
    )
    checks.append(
        _pipeline_check(
            "strategy",
            "risk_cool_base_fresh",
            bool(
                (latest_risk_cool and latest_risk_cool >= freshness_required_date)
                or not _has_new_rows(GEN2_VALID_SIGNAL_TARGET_PATH, latest_risk_cool)
                or _rebuilt_after(GEN2_VALID_SIGNAL_TARGET_PATH, GEN2_RISK_COOL_BASE_PATH)
            ),
            ""
            if (latest_risk_cool and latest_risk_cool >= freshness_required_date)
            or not _has_new_rows(GEN2_VALID_SIGNAL_TARGET_PATH, latest_risk_cool)
            or _rebuilt_after(GEN2_VALID_SIGNAL_TARGET_PATH, GEN2_RISK_COOL_BASE_PATH)
            else f"G2 risk_cool_base stale: latest={latest_risk_cool or '--'}, valid-signal target has newer rows",
            latest_date=latest_risk_cool,
            required_date=freshness_required_date,
            requested_date=normalized_signal_date,
        )
    )
    checks.append(
        _pipeline_check(
            "strategy",
            "shadow_ledger_fresh",
            bool(
                (latest_shadow and latest_shadow >= freshness_required_date)
                or not _has_new_rows(GEN2_RISK_COOL_BASE_PATH, latest_shadow, is_csv=True)
                or _rebuilt_after(GEN2_RISK_COOL_BASE_PATH, GEN2_RISK_COOL_SHADOW_LEDGER_PATH)
            ),
            ""
            if (latest_shadow and latest_shadow >= freshness_required_date)
            or not _has_new_rows(GEN2_RISK_COOL_BASE_PATH, latest_shadow, is_csv=True)
            or _rebuilt_after(GEN2_RISK_COOL_BASE_PATH, GEN2_RISK_COOL_SHADOW_LEDGER_PATH)
            else f"G2 shadow ledger stale: latest={latest_shadow or '--'}, risk_cool_base has newer rows",
            latest_date=latest_shadow,
            required_date=freshness_required_date,
            requested_date=normalized_signal_date,
        )
    )
    return checks


def _artifact_file_status(path: Path) -> Dict[str, Any]:
    exists = path.exists()
    is_file = exists and path.is_file()
    return {
        "exists": bool(exists),
        "size": int(path.stat().st_size) if is_file else 0,
        "modified_at": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S") if exists else "",
    }


def _parquet_date_summary(path: Path, date_col: str, target_date: Optional[str] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {"row_count": 0, "target_rows": None, "min_date": "", "max_date": "", "read_ok": False}
    if not path.exists():
        result["message"] = f"missing: {path}"
        return result
    try:
        df = pd.read_parquet(path, columns=[date_col])
        dates = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
        valid_dates = dates.dropna()
        result.update(
            {
                "row_count": int(len(df)),
                "min_date": str(valid_dates.min()) if len(valid_dates) else "",
                "max_date": str(valid_dates.max()) if len(valid_dates) else "",
                "read_ok": True,
            }
        )
        normalized = _normalize_date_str(target_date) if target_date else ""
        if normalized:
            result["target_rows"] = int(dates.eq(normalized).sum())
            result["target_date"] = normalized
    except Exception as exc:
        result["message"] = f"read failed: {exc}"
    return result


def _csv_date_summary(path: Path, date_col: str, target_date: Optional[str] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {"row_count": 0, "target_rows": None, "min_date": "", "max_date": "", "read_ok": False}
    if not path.exists():
        result["message"] = f"missing: {path}"
        return result
    try:
        df = pd.read_csv(path, usecols=[date_col])
        dates = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
        valid_dates = dates.dropna()
        result.update(
            {
                "row_count": int(len(df)),
                "min_date": str(valid_dates.min()) if len(valid_dates) else "",
                "max_date": str(valid_dates.max()) if len(valid_dates) else "",
                "read_ok": True,
            }
        )
        normalized = _normalize_date_str(target_date) if target_date else ""
        if normalized:
            result["target_rows"] = int(dates.eq(normalized).sum())
            result["target_date"] = normalized
    except Exception as exc:
        result["message"] = f"read failed: {exc}"
    return result


def _clickhouse_artifact_summary(table: str, date_col: str, target_date: Optional[str] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {"table_exists": False, "target_rows": None, "target_codes": None, "read_ok": False}
    if not clickhouse_available():
        result["message"] = "ClickHouse unavailable"
        return result
    if not clickhouse_table_exists(table):
        result["message"] = f"{table} does not exist"
        return result
    result["table_exists"] = True
    normalized = _normalize_date_str(target_date) if target_date else ""
    if not normalized:
        result["read_ok"] = True
        return result
    try:
        df = clickhouse_query_df(
            f"""
            SELECT count() AS row_count, uniqExact(code) AS code_count
            FROM {table}
            WHERE toDate({date_col}) = ?::DATE
            """,
            [normalized],
        )
        row = df.iloc[0] if df is not None and not df.empty else {}
        result.update(
            {
                "target_date": normalized,
                "target_rows": _to_int(row.get("row_count"), 0),
                "target_codes": _to_int(row.get("code_count"), 0),
                "read_ok": True,
            }
        )
    except Exception as exc:
        result["message"] = f"read failed: {exc}"
    return result


def _gen2_workflow_lineage(signal_date: str, prev_trade_date: Optional[str]) -> List[Dict[str, Any]]:
    d1_date = prev_trade_date or ""
    live_summary_path = GEN2_RISK_COOL_SHADOW_SUMMARY_PATH
    items: List[Dict[str, Any]] = [
        {
            "stage": "data_source",
            "name": "daily_bar_source",
            "label": "日线行情",
            "source_type": "raw",
            "storage": "clickhouse:kline_daily",
            "official_reader": "_daily_stock_coverage_status / _load_gen2_v4_pool_context",
            "write_owner": "daily market data maintenance",
            "read_policy": "依赖每日行情表；若异常需检查行情采集链路。",
            "status": _clickhouse_artifact_summary("kline_daily", "trade_date", d1_date or signal_date),
        },
        {
            "stage": "data_source",
            "name": "minute_15_source",
            "label": "15m 行情",
            "source_type": "raw",
            "storage": "clickhouse:kline_minute_15",
            "official_reader": "_gen2_minute_table_status",
            "write_owner": "minute kline maintenance",
            "read_policy": "以分钟线聚合结果为准；不足时回退 intraday_quote_snapshot。",
            "status": _clickhouse_artifact_summary("kline_minute_15", "datetime", signal_date),
        },
        {
            "stage": "data_source",
            "name": "minute_30_source",
            "label": "30m 行情",
            "source_type": "raw",
            "storage": "clickhouse:kline_minute_30",
            "official_reader": "_gen2_minute_table_status",
            "write_owner": "minute kline maintenance",
            "read_policy": "以 30m 线为主；不完整时回退 intraday_quote_snapshot。",
            "status": _clickhouse_artifact_summary("kline_minute_30", "datetime", signal_date),
        },
        {
            "stage": "timing_regime",
            "name": "g2_open_state_daily",
            "label": "G2 开仓窗口",
            "source_type": "truth",
            "storage": str(GEN2_OPEN_STATE_DAILY_PATH),
            "official_reader": "scripts/gen2_build_open_state_research.py / _load_g2_open_states",
            "write_owner": "scripts/gen2_build_open_state_research.py",
            "read_policy": "按交易日补齐开仓状态，缺失会影响候选生成。",
            "file": _artifact_file_status(GEN2_OPEN_STATE_DAILY_PATH),
            "status": _csv_date_summary(GEN2_OPEN_STATE_DAILY_PATH, "trade_date", signal_date),
        },
        {
            "stage": "selection",
            "name": "v4_event_dataset",
            "label": "V4 事件样本",
            "source_type": "truth",
            "storage": str(GEN2_V4_EVENT_DATASET_PATH),
            "official_reader": "_v4_event_dataset_status / _load_gen2_v4_pool_context",
            "write_owner": "scripts/gen2_refresh_v4_event_dataset.py",
            "read_policy": "依赖 V4 事件 parquet 的 D-1 和当日 rank/entry_pass 字段。",
            "file": _artifact_file_status(GEN2_V4_EVENT_DATASET_PATH),
            "status": _parquet_date_summary(GEN2_V4_EVENT_DATASET_PATH, "trade_date", d1_date or signal_date),
        },
        {
            "stage": "selection_context",
            "name": "sector_mapping",
            "label": "行业映射",
            "source_type": "truth",
            "storage": "clickhouse:sector_stocks",
            "official_reader": "_sector_mapping_status",
            "write_owner": "sector data maintenance",
            "read_policy": "行业映射缺失会影响板块限制与筛选结果。",
            "status": _clickhouse_artifact_summary("sector_stocks", "updated_at", None),
        },
        {
            "stage": "scoring",
            "name": "alpha191_train_source",
            "label": "Alpha191 训练源",
            "source_type": "derived",
            "storage": str(GEN2_ALPHA191_TRAIN_SOURCE_PATH),
            "official_reader": "_alpha191_artifact_status",
            "write_owner": "Alpha191 research build scripts",
            "read_policy": "历史训练源用于得分补全，缺失会回退到可用字段。",
            "file": _artifact_file_status(GEN2_ALPHA191_TRAIN_SOURCE_PATH),
            "status": _parquet_date_summary(GEN2_ALPHA191_TRAIN_SOURCE_PATH, "entry_date", d1_date or signal_date),
        },
        {
            "stage": "strategy",
            "name": "g2_v2_complete_source",
            "label": "G2 核心策略源",
            "source_type": "truth",
            "storage": str(GEN2_V2_COMPLETE_SOURCE_PATH),
            "official_reader": "api/gen2_strategy.py backtest source readers",
            "write_owner": "scripts/gen2_build_v2_complete_strategy.py",
            "read_policy": "核心策略源不可缺失，缺失会导致主池为空。",
            "file": _artifact_file_status(GEN2_V2_COMPLETE_SOURCE_PATH),
            "status": _parquet_date_summary(GEN2_V2_COMPLETE_SOURCE_PATH, "entry_date", signal_date),
        },
        {
            "stage": "strategy",
            "name": "shadow_ledger",
            "label": "实盘风控台账",
            "source_type": "truth",
            "storage": str(GEN2_RISK_COOL_SHADOW_LEDGER_PATH),
            "official_reader": "_load_gen2_risk_cool_shadow_ledger",
            "write_owner": "scripts/gen2_update_live_shadow.py",
            "read_policy": "写入失败会导致风控和告警链路失效。",
            "file": _artifact_file_status(GEN2_RISK_COOL_SHADOW_LEDGER_PATH),
            "status": _csv_date_summary(GEN2_RISK_COOL_SHADOW_LEDGER_PATH, "entry_date", signal_date),
        },
        {
            "stage": "strategy",
            "name": "live_update_summary",
            "label": "当日更新摘要",
            "source_type": "audit",
            "storage": str(live_summary_path),
            "official_reader": "_load_gen2_live_update_summary",
            "write_owner": "scripts/gen2_update_live_shadow.py",
            "read_policy": "用于审计和运行历史追踪，不存在时系统会尝试重建。",
            "file": _artifact_file_status(live_summary_path),
            "status": _artifact_file_status(live_summary_path),
        },
    ]
    return items

def _check_gen2_shadow_blockers_legacy(signal_date: str, now: datetime) -> Dict[str, Any]:
    blockers: List[Dict[str, Any]] = []
    file_checks = [
        ("event_dataset", GEN2_V4_EVENT_DATASET_PATH),
        ("shadow_ledger", GEN2_RISK_COOL_SHADOW_LEDGER_PATH),
        ("v2_complete_source", GEN2_V2_COMPLETE_SOURCE_PATH),
    ]
    for key, path in file_checks:
        if not path.exists():
            blockers.append({"type": "file_missing", "key": key, "path": str(path), "message": f"{key} 文件不存在"})
    prev_trade_date = _previous_stock_trade_date(signal_date)
    v4_status: List[Dict[str, Any]] = []
    if prev_trade_date:
        v4_status.extend(
            [
                _v4_event_dataset_status(prev_trade_date, "D-1 input"),
            ]
        )
    else:
        v4_status.append(
            {
                "type": "v4_trade_calendar",
                "ok": False,
                "trade_date": signal_date,
                "message": f"Cannot resolve previous trading day for {signal_date}",
            }
        )
    if now.time() >= time(14, 35):
        v4_status.extend(
            [
                _v4_event_dataset_status(signal_date, "D-day generated"),
            ]
        )
    for item in v4_status:
        if not item.get("ok"):
            blockers.append({**item, "message": item.get("message") or item.get("reason") or item.get("type")})
    minute_status = [
        _gen2_minute_table_status(signal_date, 15, now),
        _gen2_minute_table_status(signal_date, 30, now),
    ]
    for item in minute_status:
        if not item.get("ok"):
            blockers.append({"type": "minute_data", **item, "message": item.get("reason")})
    return {
        "ok": not blockers,
        "checked_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "signal_date": signal_date,
        "prev_trade_date": prev_trade_date,
        "blockers": blockers,
        "v4_status": v4_status,
        "minute_status": minute_status,
    }

def _check_gen2_shadow_blockers(
    signal_date: str,
    now: datetime,
    alpha191_gate: str = "g2_v2_complete",
    recipient_override: Optional[str] = None,
) -> Dict[str, Any]:
    prev_trade_date = _previous_stock_trade_date(signal_date)
    pipeline_checks: List[Dict[str, Any]] = []
    pipeline_checks.extend(_clickhouse_source_status(signal_date, prev_trade_date))
    pipeline_checks.append(
        _source_csv_date_rows_status(
            GEN2_OPEN_STATE_DAILY_PATH,
            "trade_date",
            signal_date,
            "timing_regime",
            "g2_open_state_daily",
            min_rows=1,
        )
    )
    pipeline_checks.append(_file_pipeline_status("selection", "v4_event_dataset_file", GEN2_V4_EVENT_DATASET_PATH, min_bytes=1024))
    pipeline_checks.append(_sector_mapping_status())
    pipeline_checks.extend(_alpha191_artifact_status(signal_date, prev_trade_date, alpha191_gate))
    pipeline_checks.append(_file_pipeline_status("strategy", "shadow_ledger_file", GEN2_RISK_COOL_SHADOW_LEDGER_PATH, min_bytes=1))
    pipeline_checks.append(_file_pipeline_status("strategy", "v2_complete_source_file", GEN2_V2_COMPLETE_SOURCE_PATH, min_bytes=1024))
    pipeline_checks.append(_directory_writable_status("strategy", "shadow_live_output_dir", GEN2_RISK_COOL_SHADOW_LIVE_DIR))
    pipeline_checks.append(_notification_config_status(recipient_override))
    pipeline_checks.extend(_freshness_pipeline_checks(signal_date, now))

    v4_status: List[Dict[str, Any]] = []
    if prev_trade_date:
        v4_status.extend(
            [
                _v4_event_dataset_status(prev_trade_date, "D-1 input"),
            ]
        )
    else:
        v4_status.append(
            {
                "stage": "selection",
                "type": "v4_trade_calendar",
                "ok": False,
                "trade_date": signal_date,
                "message": f"Cannot resolve previous trading day for {signal_date}",
            }
        )
    if now.time() >= time(14, 35):
        v4_status.extend(
            [
                _v4_event_dataset_status(signal_date, "D-day generated"),
            ]
        )
    for item in v4_status:
        item.setdefault("stage", "selection")
    pipeline_checks.extend(v4_status)

    minute_status = [
        _gen2_minute_table_status(signal_date, 15, now),
        _gen2_minute_table_status(signal_date, 30, now),
    ]
    for item in minute_status:
        pipeline_checks.append(
            _pipeline_check(
                "intraday_data",
                str(item.get("table") or f"minute_{item.get('period_minutes')}"),
                bool(item.get("ok")),
                str(item.get("reason") or ""),
                **{k: v for k, v in item.items() if k not in {"ok", "reason"}},
            )
        )

    blockers = [
        {"type": item.get("stage"), **item, "message": item.get("message") or item.get("reason") or item.get("name")}
        for item in pipeline_checks
        if not item.get("ok")
    ]
    return {
        "ok": not blockers,
        "checked_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "signal_date": signal_date,
        "prev_trade_date": prev_trade_date,
        "blockers": blockers,
        "pipeline_stages": _stage_summary(pipeline_checks),
        "pipeline_checks": pipeline_checks,
        "v4_status": v4_status,
        "minute_status": minute_status,
    }


def _send_gen2_blocker_email(now_text: str, signal_date: str, blocker_result: Dict[str, Any], recipient_override: Optional[str] = None) -> None:
    blockers = blocker_result.get("blockers") or []
    if not blockers:
        return
    subject = f"G2 blocker alert {signal_date} {now_text[11:16]}"
    official_rebuild = blocker_result.get("official_rebuild") if isinstance(blocker_result, dict) else None
    lines = [
        f"Generated at: {now_text}",
        f"Signal date: {signal_date}",
        f"Official rebuild: {_gen2_official_rebuild_summary_text(official_rebuild)}",
        f"Blocker count: {len(blockers)}",
        "",
    ]
    for idx, item in enumerate(blockers, start=1):
        lines.append(f"{idx}. {item.get('message') or item.get('reason') or item.get('type')}")
        if item.get("path"):
            lines.append(f"   file: {item.get('path')}")
        if item.get("table"):
            lines.append(
                (
                    "   details: "
                    f"table={item.get('table')} | "
                    f"required={item.get('required_time') or '--'} | "
                    f"latest={item.get('max_datetime') or '--'} | "
                    f"codes={item.get('code_count') or 0}"
                )
            )
    lines.append("")
    lines.append("当前状态：G2 关键链路存在阻塞，本轮 live update 已暂停。")
    _send_monitor_email(subject, "\n".join(lines).strip() + "\n", recipient_override=recipient_override)


def _is_gen2_official_rebuild_wait_only(blocker_result: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(blocker_result, dict):
        return False
    blockers = blocker_result.get("blockers") or []
    if len(blockers) != 1:
        return False
    item = blockers[0] if isinstance(blockers[0], dict) else {}
    if str(item.get("type") or "").strip().lower() != "official_rebuild":
        return False
    official_rebuild = blocker_result.get("official_rebuild") if isinstance(blocker_result, dict) else None
    status = str((official_rebuild or {}).get("status") or "").strip().lower()
    return status in {"queued", "running"}


def _run_gen2_shadow_buy_monitor(force_send: bool = False, recipient_override: Optional[str] = None) -> Dict[str, Any]:
    state = _load_gen2_shadow_monitor_state()
    now = datetime.now()
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    state["last_error"] = None
    if state.get("trading_hours_only") and not force_send and not _is_trading_session_now(now):
        pending_alerts = _load_gen2_pending_shadow_alerts_for_latest_trade_date(state)
        pending_signal_date = str(pending_alerts.get("signal_date") or "")
        rows = pending_alerts.get("rows") if isinstance(pending_alerts.get("rows"), list) else []
        new_rows = pending_alerts.get("new_rows") if isinstance(pending_alerts.get("new_rows"), list) else []
        last_alert_keys = pending_alerts.get("last_alert_keys") if isinstance(pending_alerts.get("last_alert_keys"), dict) else {}
        if pending_signal_date and new_rows:
            update_result = pending_alerts.get("update_result") if isinstance(pending_alerts.get("update_result"), dict) else {}
            if not update_result:
                update_result = {
                    "raw_candidates": None,
                    "filtered_signals": len(rows),
                    "shadow_rows_for_date": len(rows),
                }
            _send_gen2_shadow_buy_email(
                now_text,
                pending_signal_date,
                update_result,
                new_rows,
                recipient_override=recipient_override or state.get("recipient_email"),
            )
            for row in new_rows:
                key = str(row.get("alert_key") or "")
                if key:
                    last_alert_keys[key] = now_text
            if len(last_alert_keys) > 1000:
                last_alert_keys = dict(list(last_alert_keys.items())[-1000:])
            state["last_alert_keys"] = last_alert_keys
            state["last_run_at"] = now_text
            state["last_email_sent_at"] = now_text
            state["last_error"] = None
            state["last_result"] = {
                "skipped": False,
                "reason": "after_hours_pending_shadow_alert",
                "after_hours_pending_check": True,
                "signal_date": pending_signal_date,
                "candidate_count": len(rows),
                "new_count": len(new_rows),
                "email_sent": True,
            }
            _save_gen2_shadow_monitor_state(state)
            return {
                "ok": True,
                "message": "非交易时段，已发送影子候选补报",
                "email_sent": True,
                "new_rows": new_rows,
                "candidate_count": len(rows),
                "signal_date": pending_signal_date,
            }
        state["last_run_at"] = now_text
        state["last_error"] = None
        state["last_result"] = {"skipped": True, "reason": "非交易日或非交易时段"}
        _save_gen2_shadow_monitor_state(state)
        return {"ok": True, "message": "非交易日或非交易时段，已跳过", "email_sent": False, "new_rows": []}

    requested_signal_date = _normalize_date_str(_resolve_latest_stock_trade_date())
    if not requested_signal_date:
        raise RuntimeError("无法解析朢新交易日")
    date_resolution = _resolve_gen2_signal_date(requested_signal_date, allow_fallback=False)
    date_resolution_reason = str(date_resolution.get("reason") or "").strip() or None
    date_resolution_status = date_resolution.get("status") if isinstance(date_resolution.get("status"), dict) else None
    pipeline_signal_date = requested_signal_date
    if bool(date_resolution.get("ok")) and date_resolution.get("effective"):
        pipeline_signal_date = _normalize_date_str(date_resolution.get("effective")) or requested_signal_date
    if pipeline_signal_date != requested_signal_date:
        logger.info(
            "G2 shadow monitor requested date auto-adjusted",
            extra={"requested_signal_date": requested_signal_date, "effective_signal_date": pipeline_signal_date, "reason": date_resolution_reason},
        )

    official_rebuild_result = state.get("last_official_rebuild_result") if isinstance(state.get("last_official_rebuild_result"), dict) else None
    blocker_result: Optional[Dict[str, Any]] = None
    if state.get("official_rebuild_enabled", True):
        reconciled_rebuild_result = _reconcile_official_rebuild_state_if_needed(state)
        if reconciled_rebuild_result is not None:
            official_rebuild_result = reconciled_rebuild_result
        last_rebuild_date = _normalize_date_str(state.get("last_official_rebuild_date"))
        rebuild_retry_minutes = max(1, _to_int(state.get("official_rebuild_retry_minutes"), 10))
        active_rebuild_task = _find_active_gen2_official_rebuild_task()
        if active_rebuild_task:
            official_rebuild_result = active_rebuild_task
        rebuild_ready = last_rebuild_date == pipeline_signal_date and bool(state.get("last_official_rebuild_ok"))
        if not rebuild_ready and not active_rebuild_task:
            last_failed_text = str(state.get("last_official_rebuild_failed_at") or "").strip()
            can_retry = True
            if not force_send and last_failed_text:
                try:
                    last_failed_dt = datetime.strptime(last_failed_text, "%Y-%m-%d %H:%M:%S")
                    can_retry = (now - last_failed_dt).total_seconds() >= rebuild_retry_minutes * 60
                except Exception:
                    can_retry = True
            if can_retry:
                active_rebuild_task = _start_gen2_official_rebuild_task(
                    pipeline_signal_date,
                    include_breakout=bool(state.get("official_rebuild_include_breakout")),
                    legacy_330=bool(state.get("official_rebuild_legacy_330")),
                    timeout_seconds=int(state.get("official_rebuild_timeout_seconds") or 10800),
                )
                official_rebuild_result = active_rebuild_task
            else:
                official_rebuild_result = state.get("last_official_rebuild_result") if isinstance(state.get("last_official_rebuild_result"), dict) else official_rebuild_result
        if active_rebuild_task:
            task_time = str(
                active_rebuild_task.get("started_at")
                or active_rebuild_task.get("created_at")
                or now_text
            ).replace("T", " ")
            state["last_official_rebuild_attempt_at"] = task_time[:19]
            state["last_official_rebuild_task_id"] = active_rebuild_task.get("task_id")
            state["last_official_rebuild_task_status"] = active_rebuild_task.get("status") or "running"
        if last_rebuild_date != pipeline_signal_date or not bool(state.get("last_official_rebuild_ok")):
            task_status = str((official_rebuild_result or {}).get("status") or state.get("last_official_rebuild_task_status") or "").strip().lower()
            if task_status in {"queued", "running"}:
                rebuild_message = "G2 latest-driven正式主链正在重建，当前轮询先等待结果"
            elif official_rebuild_result and official_rebuild_result.get("ok") is False:
                rebuild_message = official_rebuild_result.get("message") or "G2 latest-driven 触发官方补建失败"
            else:
                rebuild_message = "G2 latest-driven正式主链尚未刷新到最新交易日"
            blocker_result = {
                "ok": False,
                "checked_at": now_text,
                "signal_date": pipeline_signal_date,
                "prev_trade_date": None,
                "official_rebuild": official_rebuild_result,
                "promotion_snapshot": {},
                "pipeline_stages": [],
                "pipeline_checks": [],
                "blockers": [
                    {
                        "stage": "strategy",
                        "type": "official_rebuild",
                        "name": "official_rebuild",
                        "ok": False,
                        "message": rebuild_message,
                    }
                ],
            }
        if blocker_result is not None:
            blocker_email_sent = False
            heartbeat_email_sent = False
            if _is_gen2_official_rebuild_wait_only(blocker_result):
                if _should_send_gen2_heartbeat(state, now, force_send=force_send):
                    _send_gen2_shadow_heartbeat_email(
                        now_text,
                        pipeline_signal_date,
                        {},
                        [],
                        blocker_result,
                        recipient_override=recipient_override or state.get("recipient_email"),
                    )
                    heartbeat_email_sent = True
                    state["last_heartbeat_sent_at"] = now_text
                state["last_run_at"] = now_text
                state["last_error"] = None
            state["last_result"] = {
                    "signal_date": pipeline_signal_date,
                    "requested_signal_date": requested_signal_date,
                    "date_resolution_reason": date_resolution_reason,
                    "date_resolution_status": date_resolution_status,
                    "blocked": True,
                    "blocker_count": 1,
                    "blocker_check": blocker_result,
                    "promotion_snapshot": {},
                    "official_rebuild": official_rebuild_result,
                    "blocker_email_sent": blocker_email_sent,
                    "heartbeat_email_sent": heartbeat_email_sent,
                }
            _save_gen2_shadow_monitor_state(state)
            return {
                    "ok": False,
                    "message": rebuild_message,
                    "email_sent": False,
                    "blocker_email_sent": blocker_email_sent,
                    "heartbeat_email_sent": heartbeat_email_sent,
                    "blocker_check": blocker_result,
                    "promotion_snapshot": {},
                    "official_rebuild": official_rebuild_result,
                    "signal_date": pipeline_signal_date,
                    "requested_signal_date": requested_signal_date,
                    "date_resolution_reason": date_resolution_reason,
                    "date_resolution_status": date_resolution_status,
                "new_rows": [],
            }

    open_state_rebuild = _ensure_gen2_open_state_daily_upto_date_impl(pipeline_signal_date, allow_fallback=False)
    if open_state_rebuild.get("ok"):
        effective_open_state_date = _normalize_date_str(open_state_rebuild.get("effective"))
        if effective_open_state_date:
            if effective_open_state_date != pipeline_signal_date:
                logger.info(
                    "G2 shadow monitor open-state rebuild effective date changed",
                    extra={"requested_signal_date": pipeline_signal_date, "effective_signal_date": effective_open_state_date},
                )
            pipeline_signal_date = effective_open_state_date
    if not open_state_rebuild.get("ok"):
        status = open_state_rebuild.get("status") or {}
        blocker = {
            "stage": "timing_regime",
            "type": "g2_open_state_rebuild",
            "name": "g2_open_state_daily",
            "ok": False,
            "path": str(GEN2_OPEN_STATE_DAILY_PATH),
            "message": str(open_state_rebuild.get("message") or status.get("message") or status.get("reason") or "g2_open_state_daily unavailable"),
            **{k: v for k, v in status.items() if k not in {"ok", "reason", "message", "stage", "name"}},
            "rebuild": open_state_rebuild.get("details") or {},
            "rebuild_running": bool(open_state_rebuild.get("running")),
            "attempted": bool(open_state_rebuild.get("attempted")),
            "rebuild_status": open_state_rebuild.get("status"),
        }
        blocker_result = {
            "ok": False,
            "checked_at": now_text,
            "signal_date": pipeline_signal_date,
            "prev_trade_date": None,
            "promotion_snapshot": {},
            "official_rebuild": official_rebuild_result,
            "pipeline_stages": _stage_summary([blocker]),
            "pipeline_checks": [blocker],
            "blockers": [blocker],
            "v4_status": [],
            "minute_status": [],
        }
        blocker_email_sent = False
        last_blocker_at = str(state.get("last_blocker_alert_at") or "").strip()
        send_blocker = bool(force_send)
        if not send_blocker:
            try:
                last_dt = datetime.strptime(last_blocker_at, "%Y-%m-%d %H:%M:%S") if last_blocker_at else None
                send_blocker = last_dt is None or (now - last_dt).total_seconds() >= 15 * 60
            except Exception:
                send_blocker = True
        if send_blocker:
            _send_gen2_blocker_email(
                now_text,
                pipeline_signal_date,
                blocker_result,
                recipient_override=recipient_override or state.get("recipient_email"),
            )
            blocker_email_sent = True
            state["last_blocker_alert_at"] = now_text
        state["last_run_at"] = now_text
        state["last_error"] = "gen2_open_state_daily_pending"
        state["last_result"] = {
            "signal_date": pipeline_signal_date,
            "requested_signal_date": requested_signal_date,
            "date_resolution_reason": date_resolution_reason,
            "date_resolution_status": date_resolution_status,
            "blocked": True,
            "blocker_count": 1,
            "blocker_check": blocker_result,
            "promotion_snapshot": {},
            "official_rebuild": official_rebuild_result,
            "open_state_rebuild": open_state_rebuild,
            "blocker_email_sent": blocker_email_sent,
        }
        _save_gen2_shadow_monitor_state(state)
        return {
            "ok": False,
            "message": "G2 open state data unavailable for signal date; auto rebuild handled",
            "email_sent": False,
            "blocker_email_sent": blocker_email_sent,
            "blocker_check": blocker_result,
            "promotion_snapshot": {},
            "official_rebuild": official_rebuild_result,
            "signal_date": pipeline_signal_date,
            "requested_signal_date": requested_signal_date,
            "date_resolution_reason": date_resolution_reason,
            "date_resolution_status": date_resolution_status,
            "new_rows": [],
        }

    promotion_snapshot_result = _collect_gen2_promotion_snapshots(
        pipeline_signal_date,
        int(state.get("pool_rank") or 200),
    )

    blocker_result = _check_gen2_shadow_blockers(
        pipeline_signal_date,
        now,
        alpha191_gate=str(state.get("alpha191_gate") or "off"),
        recipient_override=recipient_override or state.get("recipient_email"),
    )
    blocker_result["promotion_snapshot"] = promotion_snapshot_result
    blocker_result["official_rebuild"] = official_rebuild_result
    blocker_email_sent = False
    if blocker_result.get("blockers"):
        last_blocker_at = str(state.get("last_blocker_alert_at") or "").strip()
        send_blocker = bool(force_send)
        if not send_blocker:
            try:
                last_dt = datetime.strptime(last_blocker_at, "%Y-%m-%d %H:%M:%S") if last_blocker_at else None
                send_blocker = last_dt is None or (now - last_dt).total_seconds() >= 15 * 60
            except Exception:
                send_blocker = True
        if send_blocker:
            _send_gen2_blocker_email(
                now_text,
                pipeline_signal_date,
                blocker_result,
                recipient_override=recipient_override or state.get("recipient_email"),
            )
            blocker_email_sent = True
            state["last_blocker_alert_at"] = now_text
        state["last_run_at"] = now_text
        state["last_error"] = "gen2_shadow_blocked"
        state["last_result"] = {
            "signal_date": pipeline_signal_date,
            "requested_signal_date": requested_signal_date,
            "date_resolution_reason": date_resolution_reason,
            "date_resolution_status": date_resolution_status,
            "blocked": True,
            "blocker_count": len(blocker_result.get("blockers") or []),
            "blocker_check": blocker_result,
            "promotion_snapshot": promotion_snapshot_result,
            "official_rebuild": official_rebuild_result,
            "blocker_email_sent": blocker_email_sent,
        }
        _save_gen2_shadow_monitor_state(state)
        return {
            "ok": False,
            "message": "G2 shadow monitor blocked; live update skipped",
            "email_sent": False,
            "blocker_email_sent": blocker_email_sent,
            "blocker_check": blocker_result,
            "promotion_snapshot": promotion_snapshot_result,
            "official_rebuild": official_rebuild_result,
            "signal_date": pipeline_signal_date,
            "requested_signal_date": requested_signal_date,
            "date_resolution_reason": date_resolution_reason,
            "date_resolution_status": date_resolution_status,
            "new_rows": [],
        }

    update_result = _run_gen2_live_shadow_update_sync(
        pipeline_signal_date,
        int(state.get("pool_rank") or 200),
        str(state.get("alpha191_gate") or "off"),
    )
    rows = _load_gen2_shadow_monitor_candidates(pipeline_signal_date)
    last_alert_keys = state.get("last_alert_keys") if isinstance(state.get("last_alert_keys"), dict) else {}
    new_rows = [row for row in rows if force_send or row.get("alert_key") not in last_alert_keys]
    email_sent = False
    heartbeat_email_sent = False
    if new_rows:
        _send_gen2_shadow_buy_email(
            now_text,
            pipeline_signal_date,
            update_result,
            new_rows,
            recipient_override=recipient_override or state.get("recipient_email"),
        )
        email_sent = True
        for row in new_rows:
            key = str(row.get("alert_key") or "")
            if key:
                last_alert_keys[key] = now_text
    elif _should_send_gen2_heartbeat(state, now, force_send=force_send):
        _send_gen2_shadow_heartbeat_email(
            now_text,
            pipeline_signal_date,
            update_result,
            rows,
            blocker_result,
            recipient_override=recipient_override or state.get("recipient_email"),
        )
        heartbeat_email_sent = True

    if len(last_alert_keys) > 1000:
        last_alert_keys = dict(list(last_alert_keys.items())[-1000:])
    state["last_alert_keys"] = last_alert_keys
    state["last_run_at"] = now_text
    if email_sent:
        state["last_email_sent_at"] = now_text
    if heartbeat_email_sent:
        state["last_heartbeat_sent_at"] = now_text
    state["last_error"] = None
    state["last_result"] = {
        "signal_date": pipeline_signal_date,
        "requested_signal_date": requested_signal_date,
        "date_resolution_reason": date_resolution_reason,
        "date_resolution_status": date_resolution_status,
        "raw_candidates": update_result.get("raw_candidates"),
        "filtered_signals": update_result.get("filtered_signals"),
        "shadow_rows_for_date": update_result.get("shadow_rows_for_date"),
        "candidate_count": len(rows),
        "new_count": len(new_rows),
        "email_sent": email_sent,
        "heartbeat_email_sent": heartbeat_email_sent,
        "promotion_snapshot": promotion_snapshot_result,
        "official_rebuild": official_rebuild_result,
        "blocker_check": blocker_result,
    }
    _save_gen2_shadow_monitor_state(state)
    return {
        "ok": True,
        "message": "G2影子买点探测完成",
        "email_sent": email_sent,
        "heartbeat_email_sent": heartbeat_email_sent,
        "blocker_email_sent": blocker_email_sent,
        "blocker_check": blocker_result,
        "promotion_snapshot": promotion_snapshot_result,
        "official_rebuild": official_rebuild_result,
        "signal_date": pipeline_signal_date,
        "requested_signal_date": requested_signal_date,
        "date_resolution_reason": date_resolution_reason,
        "date_resolution_status": date_resolution_status,
        "update_result": update_result,
        "candidate_count": len(rows),
        "new_count": len(new_rows),
        "new_rows": new_rows,
    }


def _gen2_shadow_monitor_job_wrapper() -> None:
    state = _load_gen2_shadow_monitor_state()
    if not state.get("enabled"):
        return
    try:
        _run_gen2_shadow_buy_monitor(force_send=False)
    except Exception as exc:
        state["last_run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["last_error"] = str(exc)
        _save_gen2_shadow_monitor_state(state)
        logger.warning(f"gen2 shadow buy monitor job failed: {exc}")


def _run_gen2_strategy_refresh_30m(force: bool = False, source: str = "scheduler") -> Dict[str, Any]:
    state = _load_gen2_strategy_refresh_state()
    now = datetime.now()
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    bar_slot = _gen2_30m_bar_slot(now, int(state.get("data_delay_minutes") or 2))

    if not force and not state.get("enabled"):
        state["last_skip_at"] = now_text
        state["last_result"] = {"ok": True, "skipped": True, "reason": "disabled", "source": source}
        _save_gen2_strategy_refresh_state(state)
        return state["last_result"]
    if not force and state.get("trading_hours_only") and not bar_slot:
        state["last_skip_at"] = now_text
        state["last_result"] = {
            "ok": True,
            "skipped": True,
            "reason": "waiting_for_30m_bar_or_non_trading_day",
            "source": source,
        }
        _save_gen2_strategy_refresh_state(state)
        return state["last_result"]
    if not force and state.get("force_each_bar_once") and bar_slot and state.get("last_bar_slot") == bar_slot:
        state["last_skip_at"] = now_text
        state["last_result"] = {"ok": True, "skipped": True, "reason": "bar_slot_already_refreshed", "bar_slot": bar_slot, "source": source}
        _save_gen2_strategy_refresh_state(state)
        return state["last_result"]
    if not _gen2_strategy_refresh_lock.acquire(blocking=False):
        state["last_skip_at"] = now_text
        state["last_result"] = {"ok": True, "skipped": True, "reason": "previous_refresh_still_running", "bar_slot": bar_slot, "source": source}
        _save_gen2_strategy_refresh_state(state)
        return state["last_result"]

    started_at = datetime.now()
    selected_date = ""
    mainline_task_id = None
    try:
        requested_signal_date = _normalize_date_str(_resolve_latest_stock_trade_date())
        date_resolution = _resolve_gen2_signal_date(requested_signal_date, allow_fallback=False) if requested_signal_date else {}
        selected_date = _normalize_date_str(date_resolution.get("effective")) or requested_signal_date or ""
        steps: List[Dict[str, Any]] = []

        if state.get("run_mainline_hotspots"):
            active_mainline_task = _get_active_gen2_mainline_update_task()
            if active_mainline_task:
                steps.append(
                    {
                        "name": "mainline_hotspots",
                        "skipped": True,
                        "reason": "mainline_update_already_running",
                        "active_task": active_mainline_task,
                    }
                )
            else:
                mainline_task_id = f"gen2_strategy_refresh_mainline_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"
                _run_gen2_mainline_update_task(
                    mainline_task_id,
                    selected_date or None,
                    int(state.get("mainline_limit") or 30),
                    str(state.get("mainline_mode") or "sector"),
                )
                steps.append({"name": "mainline_hotspots", "task_id": mainline_task_id, "task": _get_gen2_mainline_update_task(mainline_task_id)})

        shadow_result: Dict[str, Any] = {}
        if state.get("run_shadow_monitor"):
            shadow_result = _run_gen2_shadow_buy_monitor(force_send=False)
            steps.append(
                {
                    "name": "shadow_monitor",
                    "ok": bool(shadow_result.get("ok", True)),
                    "signal_date": shadow_result.get("signal_date"),
                    "candidate_count": shadow_result.get("candidate_count"),
                    "new_count": shadow_result.get("new_count"),
                    "official_rebuild": shadow_result.get("official_rebuild"),
                    "blocker_check": shadow_result.get("blocker_check"),
                }
            )

        finished_at = datetime.now()
        failed_steps = []
        for step in steps:
            if step.get("skipped"):
                continue
            if step.get("name") == "mainline_hotspots":
                task_status = str((step.get("task") or {}).get("status") or "").strip().lower()
                if task_status in {"failed", "error", "missing"}:
                    failed_steps.append({"name": "mainline_hotspots", "reason": (step.get("task") or {}).get("error") or (step.get("task") or {}).get("message")})
            elif step.get("ok") is False:
                failed_steps.append({"name": step.get("name"), "reason": step.get("blocker_check") or step.get("official_rebuild") or "step returned ok=false"})
        overall_ok = not failed_steps
        result = {
            "ok": overall_ok,
            "skipped": False,
            "source": source,
            "bar_slot": bar_slot,
            "signal_date": selected_date,
            "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": finished_at.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
            "steps": steps,
            "failed_steps": failed_steps,
        }
        state = _load_gen2_strategy_refresh_state()
        state["last_run_at"] = result["finished_at"]
        if overall_ok:
            state["last_success_at"] = result["finished_at"]
            state["last_error"] = None
        else:
            state["last_error"] = "; ".join(
                str(item.get("reason") or item.get("name") or "unknown")[:500]
                for item in failed_steps[:3]
            ) or "G2 strategy refresh failed"
        state["last_result"] = result
        state["last_mainline_task_id"] = mainline_task_id
        if bar_slot:
            state["last_bar_slot"] = bar_slot
        _save_gen2_strategy_refresh_state(state)
        return result
    except Exception as exc:
        logger.exception("G2 30m strategy refresh failed")
        finished_at = datetime.now()
        result = {
            "ok": False,
            "skipped": False,
            "source": source,
            "bar_slot": bar_slot,
            "signal_date": selected_date,
            "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": finished_at.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
            "error": str(exc),
        }
        state = _load_gen2_strategy_refresh_state()
        state["last_run_at"] = result["finished_at"]
        state["last_error"] = str(exc)
        state["last_result"] = result
        _save_gen2_strategy_refresh_state(state)
        return result
    finally:
        _gen2_strategy_refresh_lock.release()


def _gen2_strategy_refresh_job_wrapper() -> None:
    try:
        _run_gen2_strategy_refresh_30m(force=False, source="scheduler")
    except Exception as exc:
        state = _load_gen2_strategy_refresh_state()
        state["last_run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["last_error"] = str(exc)
        _save_gen2_strategy_refresh_state(state)
        logger.warning(f"gen2 30m strategy refresh job failed: {exc}")


def _configure_gen2_strategy_refresh_scheduler() -> Dict[str, Any]:
    scheduler = _ensure_v4_monitor_scheduler()
    state = _load_gen2_strategy_refresh_state()
    try:
        scheduler.remove_job(_gen2_strategy_refresh_job_id)
    except Exception:
        pass
    if state.get("enabled"):
        scheduler.add_job(
            _gen2_strategy_refresh_job_wrapper,
            trigger=_gen2_strategy_refresh_triggers(int(state.get("data_delay_minutes") or 2)),
            id=_gen2_strategy_refresh_job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=180,
        )
    job = scheduler.get_job(_gen2_strategy_refresh_job_id)
    next_run_time = str(job.next_run_time) if job and job.next_run_time else None
    current_bar_slot = _gen2_30m_bar_slot(datetime.now(), int(state.get("data_delay_minutes") or 2))
    return {
        "enabled": bool(state.get("enabled")),
        "trading_hours_only": bool(state.get("trading_hours_only")),
        "data_delay_minutes": int(state.get("data_delay_minutes") or 2),
        "run_shadow_monitor": bool(state.get("run_shadow_monitor")),
        "run_mainline_hotspots": bool(state.get("run_mainline_hotspots")),
        "mainline_mode": str(state.get("mainline_mode") or "sector"),
        "mainline_limit": int(state.get("mainline_limit") or 30),
        "force_each_bar_once": bool(state.get("force_each_bar_once", True)),
        "current_bar_slot": current_bar_slot,
        "next_run_time": next_run_time,
        "last_bar_slot": state.get("last_bar_slot"),
        "last_run_at": state.get("last_run_at"),
        "last_success_at": state.get("last_success_at"),
        "last_skip_at": state.get("last_skip_at"),
        "last_error": state.get("last_error"),
        "last_result": state.get("last_result"),
        "last_mainline_task_id": state.get("last_mainline_task_id"),
        "state_path": str(GEN2_STRATEGY_REFRESH_STATE_PATH),
    }


def _configure_gen2_shadow_buy_monitor_scheduler() -> Dict[str, Any]:
    scheduler = _ensure_v4_monitor_scheduler()
    state = _load_gen2_shadow_monitor_state()
    try:
        scheduler.remove_job(_gen2_shadow_monitor_job_id)
    except Exception:
        pass
    if state.get("enabled"):
        interval_seconds = max(120, _to_int(state.get("interval_seconds"), 120))
        if state.get("trading_hours_only"):
            interval_minutes = max(1, int((interval_seconds + 59) // 60))
            minute_all = "*" if interval_minutes <= 1 else f"*/{interval_minutes}"
            minute_30_59 = "30-59" if interval_minutes <= 1 else f"30-59/{interval_minutes}"
            minute_0_30 = "0-30" if interval_minutes <= 1 else f"0-30/{interval_minutes}"
            trigger = OrTrigger(
                [
                    CronTrigger(hour="9", minute=minute_30_59, timezone="Asia/Shanghai"),
                    CronTrigger(hour="10", minute=minute_all, timezone="Asia/Shanghai"),
                    CronTrigger(hour="11", minute=minute_0_30, timezone="Asia/Shanghai"),
                    CronTrigger(hour="13-14", minute=minute_all, timezone="Asia/Shanghai"),
                    CronTrigger(hour="15", minute="0", timezone="Asia/Shanghai"),
                ]
            )
        else:
            trigger = IntervalTrigger(seconds=interval_seconds, timezone="Asia/Shanghai")
        scheduler.add_job(
            _gen2_shadow_monitor_job_wrapper,
            trigger=trigger,
            id=_gen2_shadow_monitor_job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )
    job = scheduler.get_job(_gen2_shadow_monitor_job_id)
    return {
        "enabled": bool(state.get("enabled")),
        "interval_seconds": int(state.get("interval_seconds") or 120),
        "pool_rank": int(state.get("pool_rank") or 200),
        "alpha191_gate": str(state.get("alpha191_gate") or "off"),
        "trading_hours_only": bool(state.get("trading_hours_only")),
        "heartbeat_enabled": bool(state.get("heartbeat_enabled", True)),
        "heartbeat_email_minutes": int(state.get("heartbeat_email_minutes") or 30),
        "official_rebuild_enabled": bool(state.get("official_rebuild_enabled", True)),
        "official_rebuild_legacy_330": bool(state.get("official_rebuild_legacy_330", True)),
        "official_rebuild_include_breakout": bool(state.get("official_rebuild_include_breakout", True)),
        "official_rebuild_timeout_seconds": int(state.get("official_rebuild_timeout_seconds") or 10800),
        "official_rebuild_retry_minutes": int(state.get("official_rebuild_retry_minutes") or 10),
        "recipient_email": str(state.get("recipient_email") or ""),
        "next_run_time": str(job.next_run_time) if job and job.next_run_time else None,
        "last_official_rebuild_date": state.get("last_official_rebuild_date"),
        "last_official_rebuild_at": state.get("last_official_rebuild_at"),
        "last_official_rebuild_ok": state.get("last_official_rebuild_ok"),
        "last_official_rebuild_attempt_at": state.get("last_official_rebuild_attempt_at"),
        "last_official_rebuild_failed_at": state.get("last_official_rebuild_failed_at"),
        "last_official_rebuild_task_id": state.get("last_official_rebuild_task_id"),
        "last_official_rebuild_task_status": state.get("last_official_rebuild_task_status"),
        "last_official_rebuild_result": state.get("last_official_rebuild_result"),
        "last_run_at": state.get("last_run_at"),
        "last_email_sent_at": state.get("last_email_sent_at"),
        "last_heartbeat_sent_at": state.get("last_heartbeat_sent_at"),
        "last_error": state.get("last_error"),
        "last_result": state.get("last_result"),
    }


def init_gen2_shadow_buy_monitor_scheduler_from_config() -> Dict[str, Any]:
    return {
        "shadow_monitor": _configure_gen2_shadow_buy_monitor_scheduler(),
        "strategy_refresh_30m": _configure_gen2_strategy_refresh_scheduler(),
    }


def init_gen2_strategy_refresh_scheduler_from_config() -> Dict[str, Any]:
    return _configure_gen2_strategy_refresh_scheduler()


def _calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    close = pd.to_numeric(series, errors="coerce")
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.bfill().ffill()


def _detect_bearish_divergence(df: pd.DataFrame) -> Dict[str, Any]:
    if df.empty or len(df) < 30:
        return {"detected": False, "reason": "30m K线数据不足"}
    work = df.copy()
    work["close"] = pd.to_numeric(work["close"], errors="coerce")
    work["rsi14"] = _calc_rsi(work["close"], 14)
    work = work.dropna(subset=["close", "rsi14"]).reset_index(drop=True)
    if len(work) < 30:
        return {"detected": False, "reason": "RSI鏈夋晥鏍锋湰涓嶈冻"}

    highs: List[int] = []
    for i in range(2, len(work) - 2):
        c = float(work.at[i, "close"])
        if (
            c > float(work.at[i - 1, "close"])
            and c > float(work.at[i - 2, "close"])
            and c >= float(work.at[i + 1, "close"])
            and c >= float(work.at[i + 2, "close"])
        ):
            highs.append(i)
    if len(highs) < 2:
        return {
            "detected": False,
            "reason": "价格未形成有效双峰，无法确认下跌背离",
            "rsi_latest": round(float(work["rsi14"].iloc[-1]), 2),
        }

    h1, h2 = highs[-2], highs[-1]
    price_higher_high = float(work.at[h2, "close"]) > float(work.at[h1, "close"])
    rsi_lower_high = float(work.at[h2, "rsi14"]) < float(work.at[h1, "rsi14"])
    near_overbought = float(work.at[h2, "rsi14"]) >= 60.0 or float(work["rsi14"].iloc[-1]) >= 65.0
    detected = bool(price_higher_high and rsi_lower_high and near_overbought)
    return {
        "detected": detected,
        "reason": "价格出现新高但 RSI 没有同步创新高" if detected else "价格未形成可靠背离形态",
        "last_peak_time": str(work.at[h2, "datetime"]),
        "prev_peak_time": str(work.at[h1, "datetime"]),
        "last_peak_price": round(float(work.at[h2, "close"]), 3),
        "prev_peak_price": round(float(work.at[h1, "close"]), 3),
        "last_peak_rsi": round(float(work.at[h2, "rsi14"]), 2),
        "prev_peak_rsi": round(float(work.at[h1, "rsi14"]), 2),
        "rsi_latest": round(float(work["rsi14"].iloc[-1]), 2),
    }


def _detect_rsi_box_t_signal(df: pd.DataFrame) -> Dict[str, Any]:
    if df.empty or len(df) < 40:
        return {
            "enabled": False,
            "status": "data_missing",
            "action": "observe",
            "recommendation": "15m数据不足，暂不评估箱体做T。",
        }
    work = df.copy().sort_values("datetime").reset_index(drop=True)
    for col in ["close", "high", "low"]:
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["close", "high", "low"]).reset_index(drop=True)
    if len(work) < 40:
        return {
            "enabled": False,
            "status": "data_missing",
            "action": "observe",
            "recommendation": "15m有效K线不足，暂不评估箱体做T。",
        }

    work["rsi6"] = _calc_rsi(work["close"], 6)
    box_window = 32
    latest = work.iloc[-1]
    prev = work.iloc[-2] if len(work) >= 2 else latest
    prior = work.iloc[-(box_window + 1):-1].copy()
    if len(prior) < 16:
        return {
            "enabled": False,
            "status": "data_missing",
            "action": "observe",
            "recommendation": "箱体样本不足，暂不做T。",
        }

    upper = float(prior["high"].max())
    lower = float(prior["low"].min())
    close = float(latest["close"])
    rsi = float(latest["rsi6"]) if pd.notna(latest.get("rsi6")) else None
    prev_rsi = float(prev["rsi6"]) if pd.notna(prev.get("rsi6")) else None
    mid = (upper + lower) / 2.0 if upper > 0 and lower > 0 else 0.0
    width_pct = (upper - lower) / mid if mid > 0 else None
    base_close = float(work.iloc[-(box_window + 1)]["close"]) if len(work) >= box_window + 1 else None
    slope_abs = abs(close / base_close - 1.0) if base_close and base_close > 0 else None
    breakout_up = close > upper * 1.005 if upper > 0 else False
    breakdown_down = close < lower * 0.995 if lower > 0 else False
    in_box = (
        width_pct is not None
        and 0.025 <= width_pct <= 0.18
        and (slope_abs is None or slope_abs <= 0.10)
        and close <= upper * 1.005
        and close >= lower * 0.995
    )

    bearish_div = _detect_bearish_divergence(work[["datetime", "close", "high", "low"]].copy())
    cross_down_80 = bool(prev_rsi is not None and rsi is not None and prev_rsi > 80 and rsi <= 80)
    cross_up_20 = bool(prev_rsi is not None and rsi is not None and prev_rsi < 20 and rsi >= 20)

    if breakout_up:
        status = "breakout_up"
        action = "hold_trend_no_t"
        recommendation = "已向上跳出箱体，停止做T，按趋势持股观察。"
    elif breakdown_down:
        status = "breakdown_down"
        action = "risk_control_no_t"
        recommendation = "已跌破箱体下沿，停止做T，优先按风控处理。"
    elif in_box and bearish_div.get("detected"):
        status = "in_box_bearish_divergence"
        action = "sell_half"
        recommendation = "箱体震荡内出现15m RSI背离，建议至少减半仓做T。"
    elif in_box and cross_down_80:
        status = "in_box_rsi80_cross_down"
        action = "sell_part"
        recommendation = "箱体震荡内 RSI6 跌破80，建议卖出一部分做T。"
    elif in_box and cross_up_20:
        status = "in_box_rsi20_cross_up"
        action = "buyback_t"
        recommendation = "箱体震荡内 RSI6 从20下方回到20上方，可考虑买回做T仓位。"
    elif in_box and rsi is not None and rsi < 20:
        status = "in_box_oversold_wait"
        action = "wait_buyback"
        recommendation = "箱体震荡内 RSI6 低于20，等待重新上穿20再买回。"
    elif in_box:
        status = "in_box_wait"
        action = "observe"
        recommendation = "仍在箱体震荡内，等待 RSI 做T触发。"
    else:
        status = "not_box"
        action = "observe"
        recommendation = "当前不满足箱体震荡条件，不启用 RSI 做T。"

    return {
        "enabled": bool(in_box and not breakout_up and not breakdown_down),
        "status": status,
        "action": action,
        "recommendation": recommendation,
        "datetime": str(latest.get("datetime")),
        "close": round(close, 3),
        "box_upper": round(upper, 3),
        "box_lower": round(lower, 3),
        "box_width_pct": round(float(width_pct) * 100, 2) if width_pct is not None else None,
        "box_slope_abs_pct": round(float(slope_abs) * 100, 2) if slope_abs is not None else None,
        "rsi6": round(float(rsi), 2) if rsi is not None else None,
        "prev_rsi6": round(float(prev_rsi), 2) if prev_rsi is not None else None,
        "in_box": bool(in_box),
        "breakout_up": bool(breakout_up),
        "breakdown_down": bool(breakdown_down),
        "bearish_divergence": bool(bearish_div.get("detected")),
        "rsi80_cross_down": bool(cross_down_80),
        "rsi20_cross_up": bool(cross_up_20),
    }


def _load_minute_bars_for_signal(code: str, period_minutes: int, limit: int = 240) -> pd.DataFrame:
    short_code = str(code or "").strip()[:6]
    if not short_code:
        return pd.DataFrame()
    suffix = str(code or "").strip()[6:].upper()
    if suffix in {".SH", ".SZ", ".BJ"}:
        code_candidates = [f"{short_code}{suffix}"]
    elif short_code.startswith(("6", "9")):
        code_candidates = [f"{short_code}.SH"]
    elif short_code.startswith(("0", "2", "3")):
        code_candidates = [f"{short_code}.SZ"]
    elif short_code.startswith(("4", "8")):
        code_candidates = [f"{short_code}.BJ"]
    else:
        code_candidates = [f"{short_code}.SH", f"{short_code}.SZ", f"{short_code}.BJ"]
    table_name = f"kline_minute_{period_minutes}"
    if not clickhouse_available():
        return pd.DataFrame()
    try:
        if not clickhouse_table_exists(table_name):
            return pd.DataFrame()
        raw_limit = max(int(limit), 1) * 4
        df = clickhouse_query_df(
            f"""
            SELECT code, datetime, close, high, low
            FROM {table_name}
            WHERE code IN ({",".join(["?"] * len(code_candidates))})
            ORDER BY datetime DESC
            LIMIT ?
            """,
            [*code_candidates, raw_limit],
        )
    except Exception as exc:
        logger.warning(f"load minute bars from ClickHouse failed: code={short_code}, period={period_minutes}, error={exc}")
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    df = clean_minute_bars_df(df, period_minutes, datetime_col="datetime", code_col="code")
    if df.empty:
        return pd.DataFrame()
    if int(limit) > 0 and len(df) > int(limit):
        df = df.tail(int(limit)).reset_index(drop=True)
    return df


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _resolve_live_strategy_version() -> str:
    version = str(os.getenv("AISTOCK_V4_LIVE_STRATEGY_VERSION", DEFAULT_LIVE_STRATEGY_VERSION) or "").strip()
    return version or DEFAULT_LIVE_STRATEGY_VERSION


def _strategy_version_label(strategy_version: str) -> str:
    version = str(strategy_version or "").strip()
    return STRATEGY_VERSION_LABELS.get(version, version or "未知策略版本")


def _strategy_version_sort_key(strategy_version: str) -> tuple[int, str]:
    version = str(strategy_version or "").strip()
    return STRATEGY_VERSION_ORDER.get(version, 999), version


def _list_backtest_strategy_versions() -> List[Dict[str, Any]]:
    sql = text(
        f"""
        SELECT strategy_version, MAX(updated_at) AS updated_at
        FROM {TRADING_ARTIFACT_TABLE}
        WHERE artifact_key = 'summary'
        GROUP BY strategy_version
        """
    )
    rows: List[Any] = []
    try:
        with db.engine.connect() as conn:
            rows = conn.execute(sql).fetchall()
    except Exception as exc:
        logger.warning(f"load backtest strategy versions failed: {exc}")

    versions: Dict[str, Dict[str, Any]] = {}
    for strategy_version, updated_at in rows:
        version = str(strategy_version or "").strip()
        if not version:
            continue
        versions[version] = {
            "value": version,
            "label": _strategy_version_label(version),
            "updated_at": str(updated_at) if updated_at is not None else None,
        }

    default_version = str(DEFAULT_BACKTEST_STRATEGY_VERSION or "").strip()
    if default_version and default_version not in versions:
        versions[default_version] = {
            "value": default_version,
            "label": _strategy_version_label(default_version),
            "updated_at": None,
        }

    ordered = sorted(versions.values(), key=lambda item: _strategy_version_sort_key(item.get("value")))
    return _sanitize(ordered)


def _resolve_effective_signal_date(decision: Dict[str, Any], selected_date: Optional[str]) -> str:
    """
    Resolve signal-date used by live scoring path.
    Default mode is same_day (signal_date == selected trade date).
    """
    selected = str(selected_date or "").strip()
    origin = str((decision or {}).get("signal_date") or "").strip()
    if DEFAULT_SIGNAL_EXECUTION_MODE == "t_plus_1":
        return origin or selected
    return selected or origin


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    if isinstance(value, (np.integer, np.floating)):
        value = value.item()
    if isinstance(value, float) and (np.isnan(value) or np.isinf(value)):
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        if isinstance(value, str) and value.strip() == "":
            return None
        val = float(value)
        if np.isnan(val) or np.isinf(val):
            return None
        return val
    except Exception:
        return None


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        if isinstance(value, str) and value.strip() == "":
            return default
        return int(float(value))
    except Exception:
        return default


def _load_gen2_risk_cool_shadow_ledger() -> pd.DataFrame:
    if not GEN2_RISK_COOL_SHADOW_LEDGER_PATH.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(GEN2_RISK_COOL_SHADOW_LEDGER_PATH)
    except Exception as exc:
        logger.warning(f"load gen2 risk_cool shadow ledger failed: {exc}")
        return pd.DataFrame()

    if "entry_date" in df.columns:
        df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    numeric_cols = [
        "day_signal_rank",
        "entry_price",
        "v4_rank",
        "v4_score",
        "rt_return_from_d1_close",
        "rt_30m_amount_ratio",
        "mom5",
        "mom20",
        "vol_ratio",
        "outcome_fwd_ret_5d",
        "outcome_fwd_ret_10d",
        "outcome_fwd_ret_20d",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ["stop5_touch_30m", "outcome_good", "outcome_bad"]:
        if col in df.columns:
            df[col] = df[col].map(lambda x: str(x).strip().lower() in {"1", "true", "yes", "y"})
    return df


def _load_gen2_risk_cool_shadow_summary() -> List[Dict[str, Any]]:
    if not GEN2_RISK_COOL_SHADOW_SUMMARY_PATH.exists():
        return []
    try:
        summary_df = pd.read_csv(GEN2_RISK_COOL_SHADOW_SUMMARY_PATH)
    except Exception as exc:
        logger.warning(f"load gen2 risk_cool shadow summary failed: {exc}")
        return []
    return _sanitize(summary_df.replace({np.nan: None}).to_dict("records"))


def _gen2_live_update_dates() -> List[str]:
    if not GEN2_RISK_COOL_SHADOW_LIVE_DIR.exists():
        return []
    dates: List[str] = []
    for path in GEN2_RISK_COOL_SHADOW_LIVE_DIR.glob("*_summary.json"):
        date_text = path.name.replace("_summary.json", "")
        normalized = _normalize_date_str(date_text)
        if normalized:
            dates.append(normalized)
    return sorted(set(dates))


def _gen2_selection_pool_effective_dates(dates: List[str], ledger_df: pd.DataFrame) -> List[str]:
    effective_dates = set()
    if not ledger_df.empty and "entry_date" in ledger_df.columns:
        for value in ledger_df["entry_date"].dropna().unique():
            normalized = _normalize_date_str(value)
            if normalized:
                effective_dates.add(normalized)
    for date_text in dates:
        summary = _load_gen2_live_update_summary(date_text)
        if not summary:
            continue
        counts = [
            summary.get("raw_candidates"),
            summary.get("filtered_signals_before_alpha191"),
            summary.get("filtered_signals"),
            summary.get("shadow_rows_for_date"),
        ]
        if any((_to_int(value, default=0) or 0) > 0 for value in counts):
            effective_dates.add(date_text)
    return sorted(effective_dates)


def _load_gen2_live_update_summary(signal_date: str) -> Dict[str, Any]:
    path = GEN2_RISK_COOL_SHADOW_LIVE_DIR / f"{signal_date}_summary.json"
    return _load_json_file(path, {}) if path.exists() else {}


def _load_gen2_live_update_frame(signal_date: str, name: str) -> pd.DataFrame:
    base = GEN2_RISK_COOL_SHADOW_LIVE_DIR / f"{signal_date}_{name}"
    parquet_path = base.with_suffix(".parquet")
    csv_path = base.with_suffix(".csv")
    try:
        if parquet_path.exists():
            df = pd.read_parquet(parquet_path)
        elif csv_path.exists():
            df = pd.read_csv(csv_path)
        else:
            return pd.DataFrame()
    except Exception as exc:
        logger.warning(f"load gen2 live update frame failed: {base.name}: {exc}")
        return pd.DataFrame()
    if df.empty:
        return df
    out = df.copy()
    if "entry_date" in out.columns:
        out["entry_date"] = pd.to_datetime(out["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    if "confirm_datetime" in out.columns:
        out["confirm_datetime"] = pd.to_datetime(out["confirm_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    if "code" in out.columns:
        out["code6"] = out["code"].map(_normalize_stock_code6)
    elif "code6" in out.columns:
        out["code6"] = out["code6"].map(_normalize_stock_code6)
    else:
        out["code6"] = ""
    if "confirm_datetime" not in out.columns:
        out["confirm_datetime"] = ""
    out["_pool_key"] = out["code6"].astype(str) + "|" + out["confirm_datetime"].astype(str)
    return out


def _load_gen2_v4_pool_context(signal_date: str, pool_rank: int, mode: str = "d1") -> pd.DataFrame:
    if not GEN2_V4_EVENT_DATASET_PATH.exists():
        return pd.DataFrame()
    columns = [
        "trade_date",
        "code",
        "name",
        "v4_rank",
        "v4_score",
        "entry_pass",
        "in_score_pool",
        "mom5",
        "mom10",
        "mom20",
        "vol_ratio",
        "vol10",
        "close",
    ]
    try:
        df = pd.read_parquet(GEN2_V4_EVENT_DATASET_PATH, columns=columns)
    except Exception as exc:
        logger.warning(f"load gen2 v4 pool context failed: {exc}")
        return pd.DataFrame()
    if df.empty:
        return df
    out = df.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    if mode == "d":
        source_date = signal_date
        if source_date not in set(out["trade_date"].dropna().astype(str).tolist()):
            return pd.DataFrame()
    else:
        available_dates = sorted([str(x) for x in out["trade_date"].dropna().unique() if str(x) < signal_date])
        if not available_dates:
            return pd.DataFrame()
        source_date = available_dates[-1]
    out = out[out["trade_date"].eq(source_date)].copy()
    out["v4_rank"] = pd.to_numeric(out.get("v4_rank"), errors="coerce")
    out = out[out["v4_rank"].le(int(pool_rank))].copy()
    if out.empty:
        return out
    out["code6"] = out["code"].map(_normalize_stock_code6)
    out = out.sort_values(["v4_rank", "v4_score", "code"], ascending=[True, False, True])
    out = out.drop_duplicates("code6", keep="first").head(int(pool_rank)).copy()
    out["entry_date"] = signal_date
    out["v4_pool_date"] = source_date
    out["v4_pool_mode"] = "D" if mode == "d" else "D-1"
    out["confirm_datetime"] = ""
    out["_pool_key"] = out["code6"].astype(str) + "|v4_pool"
    return out.reset_index(drop=True)


def _gen2_v4_pool_records(df: pd.DataFrame, selected_date: str, mode_label: str, limit: int) -> List[Dict[str, Any]]:
    if df.empty:
        return []
    records: List[Dict[str, Any]] = []
    for _, item in df.iterrows():
        code6 = str(item.get("code6") or "")
        records.append(
            {
                "key": f"{code6}|v4_{mode_label}",
                "code": _to_exchange_stock_code(code6) if code6 else str(item.get("code") or ""),
                "code6": code6,
                "name": str(item.get("name") or ""),
                "entry_date": selected_date,
                "v4_pool_date": str(item.get("v4_pool_date") or ""),
                "v4_pool_mode": mode_label,
                "v4_rank": _to_int(item.get("v4_rank"), default=0) or None,
                "v4_score": _to_float(item.get("v4_score")),
                "mom5": _to_float(item.get("mom5")),
                "mom10": _to_float(item.get("mom10")),
                "mom20": _to_float(item.get("mom20")),
                "vol_ratio": _to_float(item.get("vol_ratio")),
                "vol10": _to_float(item.get("vol10")),
                "close": _to_float(item.get("close")),
                "entry_pass": bool(item.get("entry_pass")),
                "in_score_pool": bool(item.get("in_score_pool")),
                "reason_text": (
                    "D-1 V4 命中，用于补录G2主力逻辑"
                    if mode_label == "D-1"
                    else "D 日 V4 命中，当日触发主力逻辑，G2内采用历史模式"
                ),
            }
        )
    return _sanitize(records[: int(limit)])


def _row_value(row: Optional[pd.Series], key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        value = row.get(key, default)
    except Exception:
        return default
    return default if pd.isna(value) else value


def _first_pool_row(*rows: Optional[pd.Series]) -> Optional[pd.Series]:
    for row in rows:
        if row is not None:
            return row
    return None


def _load_gen2_v2_complete_day(selected_date: str) -> pd.DataFrame:
    if not GEN2_V2_COMPLETE_SOURCE_PATH.exists():
        return pd.DataFrame()
    try:
        df = pd.read_parquet(GEN2_V2_COMPLETE_SOURCE_PATH)
    except Exception as exc:
        logger.warning(f"load G2 v2 complete source failed: {exc}")
        return pd.DataFrame()
    if df.empty or "entry_date" not in df.columns:
        return pd.DataFrame()
    d = df.copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d = d[d["entry_date"].eq(selected_date)].copy()
    if d.empty:
        return d
    d["code6"] = d["code"].map(_normalize_stock_code6)
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    d["_pool_key"] = d["code6"].astype(str) + "|" + d["confirm_datetime"].astype(str)
    return d.reset_index(drop=True)


def _load_gen2_v2_complete_dates() -> List[str]:
    if not GEN2_V2_COMPLETE_SOURCE_PATH.exists():
        return []
    try:
        df = pd.read_parquet(GEN2_V2_COMPLETE_SOURCE_PATH, columns=["entry_date"])
    except Exception as exc:
        logger.warning(f"load G2 v2 complete dates failed: {exc}")
        return []
    if df.empty or "entry_date" not in df.columns:
        return []
    dates = pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return sorted([str(x) for x in dates.dropna().unique() if str(x)])


def _load_gen2_v2_complete_summary() -> Dict[str, Any]:
    if not GEN2_V2_COMPLETE_SUMMARY_PATH.exists():
        return {}
    try:
        return json.loads(GEN2_V2_COMPLETE_SUMMARY_PATH.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        logger.warning(f"load G2 v2 complete summary failed: {exc}")
        return {}


def _report_dir_candidates(base_dir: Path) -> List[Path]:
    candidates = [base_dir]
    if "reports" in base_dir.parts:
        try:
            idx = base_dir.parts.index("reports")
            relative = Path(*base_dir.parts[idx + 1 :])
            candidates.append(REPO_ROOT / "reports" / relative)
        except Exception:
            pass
    out: List[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _report_file_candidates(base_dir: Path, *parts: str) -> List[Path]:
    out: List[Path] = []
    for root in _report_dir_candidates(base_dir):
        out.append(root.joinpath(*parts))
    return out


def _choose_existing_path(paths: List[Path]) -> Optional[Path]:
    for path in paths:
        if path.exists():
            return path
    return None


def _load_csv_candidates(paths: List[Path]) -> tuple[pd.DataFrame, Optional[Path], Optional[str]]:
    chosen = _choose_existing_path(paths)
    if chosen is None:
        return pd.DataFrame(), None, "missing"
    try:
        return pd.read_csv(chosen, encoding="utf-8-sig"), chosen, None
    except pd.errors.EmptyDataError:
        return pd.DataFrame(), chosen, None
    except Exception as exc:
        logger.warning(f"load csv failed: path={chosen}, error={exc}")
        return pd.DataFrame(), chosen, str(exc)


def _load_json_candidates(paths: List[Path]) -> tuple[Dict[str, Any], Optional[Path], Optional[str]]:
    chosen = _choose_existing_path(paths)
    if chosen is None:
        return {}, None, "missing"
    try:
        return json.loads(chosen.read_text(encoding="utf-8-sig")), chosen, None
    except Exception as exc:
        logger.warning(f"load json failed: path={chosen}, error={exc}")
        return {}, chosen, str(exc)


def _iso_mtime(path: Optional[Path]) -> Optional[str]:
    if path is None or not path.exists():
        return None
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _extract_df_latest_date(df: pd.DataFrame, columns: List[str]) -> Optional[str]:
    if df.empty:
        return None
    candidates: List[str] = []
    for col in columns:
        if col not in df.columns:
            continue
        vals = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d").dropna().tolist()
        candidates.extend([str(x) for x in vals if str(x)])
    return max(candidates) if candidates else None


def _filter_df_by_date(df: pd.DataFrame, selected_date: Optional[str], columns: List[str]) -> pd.DataFrame:
    if df.empty or not selected_date:
        return df.copy()
    for col in columns:
        if col not in df.columns:
            continue
        normalized = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d")
        matched = df[normalized.eq(selected_date)].copy()
        if not matched.empty:
            return matched
    return df.copy()


def _pick_gen2_mainline_selected_date(
    requested_date: Optional[str],
    sector_factor: pd.DataFrame,
    theme_pool: pd.DataFrame,
    watchlist_dates: List[str],
) -> tuple[Optional[str], Optional[str]]:
    requested = _normalize_date_str(requested_date)
    sector_dates: List[str] = []
    for col in ["date_text", "trade_date"]:
        if col in sector_factor.columns:
            vals = pd.to_datetime(sector_factor[col], errors="coerce").dt.strftime("%Y-%m-%d").dropna().tolist()
            sector_dates.extend([str(x) for x in vals if str(x)])
    theme_dates: List[str] = []
    if "trade_date" in theme_pool.columns:
        vals = pd.to_datetime(theme_pool["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d").dropna().tolist()
        theme_dates.extend([str(x) for x in vals if str(x)])
    available = sorted(set(sector_dates + theme_dates + [str(x) for x in watchlist_dates if str(x)]))
    if requested and requested in available:
        return requested, "exact"
    if requested and requested not in available and available:
        return available[-1], "fallback_latest"
    if available:
        return available[-1], "latest"
    return requested, "requested_only" if requested else None


def _build_source_status(
    name: str,
    label: str,
    path: Optional[Path],
    error: Optional[str],
    row_count: int,
    latest_date: Optional[str] = None,
) -> Dict[str, Any]:
    status = "ok" if path and not error else "missing" if path is None else "error"
    return {
        "name": name,
        "label": label,
        "status": status,
        "path": str(path) if path else None,
        "updated_at": _iso_mtime(path),
        "row_count": int(row_count),
        "latest_date": latest_date,
        "error": None if error in {None, "missing"} else str(error),
    }


def _load_mainline_watchlist_snapshot(selected_date: Optional[str]) -> tuple[pd.DataFrame, Dict[str, Any], Optional[Path], Optional[Path], List[str]]:
    roots = _report_dir_candidates(GEN2_MAINLINE_SECTOR_WATCHLIST_DIR)
    available_dates: List[str] = []
    chosen_dir: Optional[Path] = None
    for root in roots:
        if not root.exists():
            continue
        for child in root.iterdir():
            if child.is_dir():
                normalized = _normalize_date_str(child.name)
                if normalized:
                    available_dates.append(normalized)
                    if selected_date and normalized == selected_date and chosen_dir is None:
                        chosen_dir = child
    available_dates = sorted(set(available_dates))
    if chosen_dir is None and available_dates:
        latest = available_dates[-1]
        for root in roots:
            candidate = root / latest
            if candidate.exists():
                chosen_dir = candidate
                break
    if chosen_dir is None:
        return pd.DataFrame(), {}, None, None, available_dates
    csv_path = chosen_dir / "watchlist.csv"
    json_path = chosen_dir / "watchlist.json"
    watch_df = pd.DataFrame()
    watch_json: Dict[str, Any] = {}
    if csv_path.exists():
        try:
            watch_df = pd.read_csv(csv_path, encoding="utf-8-sig")
        except Exception as exc:
            logger.warning(f"load watchlist csv failed: path={csv_path}, error={exc}")
    if json_path.exists():
        try:
            watch_json = json.loads(json_path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            logger.warning(f"load watchlist json failed: path={json_path}, error={exc}")
    return watch_df, watch_json, csv_path if csv_path.exists() else None, json_path if json_path.exists() else None, available_dates


def _load_stock_daily_snapshot(trade_date: Optional[str], codes6: List[str]) -> pd.DataFrame:
    normalized_date = _normalize_date_str(trade_date)
    code_list = sorted({str(x).strip() for x in (codes6 or []) if str(x).strip()})
    if not normalized_date or not code_list:
        return pd.DataFrame(columns=["code6", "trade_date", "close", "change_pct", "amount"])
    placeholders = ",".join(["?"] * len(code_list))
    try:
        df = clickhouse_query_df(
            f"""
            SELECT
                substring(code, 1, 6) AS code6,
                trade_date,
                close,
                change_pct,
                amount
            FROM kline_daily
            WHERE trade_date = ?
              AND substring(code, 1, 6) IN ({placeholders})
            """,
            [normalized_date, *code_list],
        )
    except Exception as exc:
        logger.warning(f"load stock daily snapshot failed: trade_date={normalized_date}, error={exc}")
        return pd.DataFrame(columns=["code6", "trade_date", "close", "change_pct", "amount"])
    if df.empty:
        return pd.DataFrame(columns=["code6", "trade_date", "close", "change_pct", "amount"])
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["close", "change_pct", "amount"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _latest_mainline_factor_count() -> int:
    summary, _, error = _load_json_candidates(_report_file_candidates(GEN2_MAINLINE_INTRADAY_FACTOR_DIR, "summary.json"))
    if error or not isinstance(summary, dict):
        return 0
    return _to_int(summary.get("factor_count"), default=0)


def build_gen2_mainline_hotspots(requested_date: Optional[str], limit: int = 30) -> Dict[str, Any]:
    safe_limit = max(5, min(int(limit or 30), 120))
    sector_factor_df, sector_factor_path, sector_factor_error = _load_csv_candidates(
        _report_file_candidates(GEN2_MAINLINE_INTRADAY_FACTOR_DIR, "mainline_intraday_diffusion_factor.csv")
    )
    sector_overlay_df, sector_overlay_path, sector_overlay_error = _load_csv_candidates(
        _report_file_candidates(GEN2_MAINLINE_INTRADAY_OVERLAY_DIR, "mainline_candidate_overlay.csv")
    )
    member_snapshot_df, member_snapshot_path, member_snapshot_error = _load_csv_candidates(
        _report_file_candidates(GEN2_MAINLINE_INTRADAY_OVERLAY_DIR, "mainline_member_snapshot.csv")
    )
    theme_pool_df, theme_pool_path, theme_pool_error = _load_csv_candidates(
        _report_file_candidates(GEN2_MAINLINE_THEME_POOL_DIR, "observation_pool.csv")
    )
    theme_overlay_df, theme_overlay_path, theme_overlay_error = _load_csv_candidates(
        _report_file_candidates(GEN2_MAINLINE_THEME_OVERLAY_DIR, "strategy_overlay.csv")
    )
    theme_addon_df, theme_addon_path, theme_addon_error = _load_csv_candidates(
        _report_file_candidates(GEN2_MAINLINE_THEME_OVERLAY_DIR, "theme_addon.csv")
    )
    sector_summary_json, sector_summary_path, sector_summary_error = _load_json_candidates(
        _report_file_candidates(GEN2_MAINLINE_INTRADAY_FACTOR_DIR, "summary.json")
    )
    sector_overlay_summary_json, sector_overlay_summary_path, sector_overlay_summary_error = _load_json_candidates(
        _report_file_candidates(GEN2_MAINLINE_INTRADAY_OVERLAY_DIR, "summary.json")
    )
    watch_selected, _ = _pick_gen2_mainline_selected_date(
        requested_date=requested_date,
        sector_factor=sector_factor_df,
        theme_pool=theme_pool_df,
        watchlist_dates=[],
    )
    watchlist_df, watchlist_json, watchlist_csv_path, watchlist_json_path, watchlist_dates = _load_mainline_watchlist_snapshot(
        watch_selected
    )
    selected_date, date_resolution = _pick_gen2_mainline_selected_date(
        requested_date=requested_date,
        sector_factor=sector_factor_df,
        theme_pool=theme_pool_df,
        watchlist_dates=watchlist_dates,
    )
    latest_trade_date = _normalize_date_str(_resolve_latest_stock_trade_date())
    member_snapshot_source_date = _normalize_date_str(
        sector_overlay_summary_json.get("target_date") if isinstance(sector_overlay_summary_json, dict) else None
    ) or selected_date

    sector_factor_day = _filter_df_by_date(sector_factor_df, selected_date, ["date_text", "trade_date"]).copy()
    sector_factor_day["mainline_intraday_score"] = pd.to_numeric(
        sector_factor_day.get("mainline_intraday_score"), errors="coerce"
    )
    for col in ["true_index_window_score", "intraday_diffusion_score"]:
        if col in sector_factor_day.columns:
            sector_factor_day[col] = pd.to_numeric(sector_factor_day[col], errors="coerce")
    sector_sort_cols = [
        c for c in ["mainline_intraday_score", "true_index_window_score", "intraday_diffusion_score"] if c in sector_factor_day.columns
    ]
    if sector_sort_cols:
        sector_factor_day = sector_factor_day.sort_values(
            sector_sort_cols,
            ascending=[False] * len(sector_sort_cols),
            na_position="last",
        )
    sector_factor_day = sector_factor_day.head(safe_limit)

    sector_overlay_day = _filter_df_by_date(sector_overlay_df, selected_date, ["entry_date_text"]).copy()
    for col in ["mainline_intraday_score", "score", "l3_rt_strong3_ratio"]:
        if col in sector_overlay_day.columns:
            sector_overlay_day[col] = pd.to_numeric(sector_overlay_day[col], errors="coerce")
    overlay_sort_cols = [c for c in ["mainline_intraday_score", "score", "l3_rt_strong3_ratio"] if c in sector_overlay_day.columns]
    if overlay_sort_cols:
        sector_overlay_day = sector_overlay_day.sort_values(overlay_sort_cols, ascending=False, na_position="last")
    sector_overlay_day = sector_overlay_day.head(safe_limit)

    member_snapshot_day = member_snapshot_df.copy()
    for col in ["mainline_intraday_score", "change_pct", "amount"]:
        if col in member_snapshot_day.columns:
            member_snapshot_day[col] = pd.to_numeric(member_snapshot_day[col], errors="coerce")
    if "stock_code6" in member_snapshot_day.columns:
        member_snapshot_day["stock_code6"] = member_snapshot_day["stock_code6"].map(_normalize_stock_code6)
    member_snapshot_day["snapshot_trade_date"] = member_snapshot_source_date
    current_snapshot_df = _load_stock_daily_snapshot(
        latest_trade_date,
        member_snapshot_day.get("stock_code6", pd.Series(dtype=str)).dropna().astype(str).tolist(),
    )
    if not current_snapshot_df.empty:
        current_snapshot_df = current_snapshot_df.rename(
            columns={
                "trade_date": "current_trade_date",
                "close": "current_close",
                "change_pct": "current_change_pct",
                "amount": "current_amount",
            }
        )
        if "stock_code6" in member_snapshot_day.columns:
            member_snapshot_day = member_snapshot_day.merge(current_snapshot_df, left_on="stock_code6", right_on="code6", how="left")
            member_snapshot_day = member_snapshot_day.drop(columns=["code6"], errors="ignore")
    member_sort_cols = [c for c in ["mainline_intraday_score", "change_pct", "amount"] if c in member_snapshot_day.columns]
    if member_sort_cols:
        member_snapshot_day = member_snapshot_day.sort_values(
            member_sort_cols,
            ascending=[False] * len(member_sort_cols),
            na_position="last",
        )
    member_snapshot_day = member_snapshot_day.head(safe_limit)

    theme_pool_day = _filter_df_by_date(theme_pool_df, selected_date, ["trade_date"]).copy()
    for col in ["theme_candidate_score", "theme_weight_hint", "ret20", "ret10", "ret5"]:
        if col in theme_pool_day.columns:
            theme_pool_day[col] = pd.to_numeric(theme_pool_day[col], errors="coerce")
    if "code6" in theme_pool_day.columns:
        theme_pool_day["code6"] = theme_pool_day["code6"].map(_normalize_stock_code6)
    theme_pool_day["snapshot_trade_date"] = selected_date
    theme_current_snapshot_df = _load_stock_daily_snapshot(
        latest_trade_date,
        theme_pool_day.get("code6", pd.Series(dtype=str)).dropna().astype(str).tolist(),
    )
    if not theme_current_snapshot_df.empty and "code6" in theme_pool_day.columns:
        theme_current_snapshot_df = theme_current_snapshot_df.rename(
            columns={
                "trade_date": "current_trade_date",
                "close": "current_close",
                "change_pct": "current_change_pct",
                "amount": "current_amount",
            }
        )
        theme_pool_day = theme_pool_day.merge(theme_current_snapshot_df, on="code6", how="left")
    theme_pool_sort_cols = [c for c in ["theme_candidate_score", "theme_weight_hint"] if c in theme_pool_day.columns]
    if theme_pool_sort_cols:
        theme_pool_day = theme_pool_day.sort_values(theme_pool_sort_cols, ascending=False, na_position="last")
    theme_pool_day = theme_pool_day.head(safe_limit)

    theme_overlay_day = theme_overlay_df.copy()
    for col in ["overlay_score", "raw_score", "theme_weight_hint"]:
        if col in theme_overlay_day.columns:
            theme_overlay_day[col] = pd.to_numeric(theme_overlay_day[col], errors="coerce")
    theme_overlay_sort_cols = [c for c in ["overlay_score", "raw_score"] if c in theme_overlay_day.columns]
    if theme_overlay_sort_cols:
        theme_overlay_day = theme_overlay_day.sort_values(theme_overlay_sort_cols, ascending=False, na_position="last")
    theme_overlay_day = theme_overlay_day.head(safe_limit)

    theme_addon_day = theme_addon_df.copy()
    for col in ["theme_weight_hint", "theme_candidate_score"]:
        if col in theme_addon_day.columns:
            theme_addon_day[col] = pd.to_numeric(theme_addon_day[col], errors="coerce")
    theme_addon_sort_cols = [c for c in ["theme_candidate_score", "theme_weight_hint"] if c in theme_addon_day.columns]
    if theme_addon_sort_cols:
        theme_addon_day = theme_addon_day.sort_values(theme_addon_sort_cols, ascending=False, na_position="last")
    theme_addon_day = theme_addon_day.head(safe_limit)

    if not watchlist_df.empty:
        for col in ["candidate_score", "stock_current_from_h20", "stock_current_from_anchor", "sector_rank"]:
            if col in watchlist_df.columns:
                watchlist_df[col] = pd.to_numeric(watchlist_df[col], errors="coerce")
        watchlist_sort_cols = [c for c in ["candidate_score", "stock_current_from_h20"] if c in watchlist_df.columns]
        if watchlist_sort_cols:
            watchlist_df = watchlist_df.sort_values(watchlist_sort_cols, ascending=False, na_position="last")
        watchlist_df = watchlist_df.head(safe_limit)

    sector_watchlist_mode = "watchlist"
    sector_watchlist_hint = "严格候选观察池，偏研究跟踪，不建议直接买入。"
    sector_watchlist_display = watchlist_df.copy()
    if sector_watchlist_display.empty and not member_snapshot_day.empty:
        fallback = member_snapshot_day.copy()
        fallback["stock_code"] = fallback.get("stock_code_raw", fallback.get("stock_code6"))
        fallback["sector_rank"] = pd.to_numeric(fallback.get("rank_in_sector_by_day"), errors="coerce")
        fallback["candidate_score"] = pd.to_numeric(fallback.get("mainline_intraday_score"), errors="coerce")
        fallback["stock_current_from_anchor"] = pd.to_numeric(fallback.get("change_pct"), errors="coerce") / 100.0
        fallback["stock_current_from_h20"] = pd.NA
        fallback["amount"] = pd.to_numeric(fallback.get("amount"), errors="coerce")
        fallback = fallback.sort_values(
            [c for c in ["mainline_intraday_score", "rank_in_sector_by_day", "change_pct", "amount"] if c in fallback.columns],
            ascending=[False, True, False, False][: len([c for c in ["mainline_intraday_score", "rank_in_sector_by_day", "change_pct", "amount"] if c in fallback.columns])],
            na_position="last",
        ).head(safe_limit)
        sector_watchlist_display = fallback
        sector_watchlist_mode = "member_snapshot_fallback"
        sector_watchlist_hint = "严格观察池为空，当前回退展示主线板块中的强势成分股，仅供观察，不建议直接买入。"

    sector_count = len(sector_factor_day)
    overlay_count = len(sector_overlay_day)
    theme_pool_count = len(theme_pool_day)
    theme_match_count = (
        int(theme_overlay_day.get("has_mainline_theme", pd.Series(dtype=bool)).fillna(False).sum())
        if not theme_overlay_day.empty
        else 0
    )
    watchlist_count = len(watchlist_df)
    available = any(
        [
            not sector_factor_day.empty,
            not sector_overlay_day.empty,
            not theme_pool_day.empty,
            not theme_overlay_day.empty,
            not watchlist_df.empty,
        ]
    )
    missing_labels = []
    if sector_factor_path is None:
        missing_labels.append("\u677f\u5757\u4e3b\u7ebf\u56e0\u5b50")
    if theme_pool_path is None:
        missing_labels.append("\u4e3b\u9898\u89c2\u5bdf\u6c60")
    if watchlist_csv_path is None and watchlist_json_path is None:
        missing_labels.append("\u4e3b\u9898\u89c2\u5bdf\u6c60")
    message = None
    if not available:
        message = "\u4e3b\u7ebf\u70ed\u70b9\u7ed3\u679c\u6682\u4e0d\u53ef\u7528\uff1b\u8bf7\u5148\u751f\u6210\u4e3b\u7ebf\u677f\u5757\u6216\u4e3b\u9898\u7814\u7a76\u4ea7\u7269\u3002"
    elif missing_labels:
        message = "\u90e8\u5206\u4e3b\u7ebf\u70ed\u70b9\u4ea7\u7269\u7f3a\u5931\uff1a" + "\u3001".join(missing_labels)

    source_status = [
        _build_source_status(
            "sector_factor",
            "\u677f\u5757\u4e3b\u7ebf\u56e0\u5b50",
            sector_factor_path,
            sector_factor_error,
            len(sector_factor_df),
            _extract_df_latest_date(sector_factor_df, ["date_text", "trade_date"]),
        ),
        _build_source_status(
            "sector_overlay",
            "\u677f\u5757\u4e3b\u7ebf\u4e0e\u5019\u9009\u91cd\u5408",
            sector_overlay_path,
            sector_overlay_error,
            len(sector_overlay_df),
            _extract_df_latest_date(sector_overlay_df, ["entry_date_text"]),
        ),
        _build_source_status(
            "member_snapshot",
            "\u677f\u5757\u4e3b\u7ebf\u4e0e\u5019\u9009\u91cd\u5408",
            member_snapshot_path,
            member_snapshot_error,
            len(member_snapshot_df),
            selected_date,
        ),
        _build_source_status(
            "theme_pool",
            "\u4e3b\u7ebf\u4e3b\u9898\u89c2\u5bdf\u6c60",
            theme_pool_path,
            theme_pool_error,
            len(theme_pool_df),
            _extract_df_latest_date(theme_pool_df, ["trade_date"]),
        ),
        _build_source_status(
            "theme_overlay",
            "\u4e3b\u7ebf\u4e3b\u9898\u7b56\u7565\u53e0\u52a0",
            theme_overlay_path or theme_addon_path,
            theme_overlay_error or theme_addon_error,
            len(theme_overlay_df) or len(theme_addon_df),
            selected_date,
        ),
        _build_source_status(
            "sector_watchlist",
            "\u4e3b\u7ebf\u4e3b\u9898\u89c2\u5bdf\u6c60",
            watchlist_csv_path or watchlist_json_path,
            None if (watchlist_csv_path or watchlist_json_path) else "missing",
            len(watchlist_df),
            selected_date or (watchlist_dates[-1] if watchlist_dates else None),
        ),
    ]

    return _sanitize(
        {
            "available": available,
            "message": message,
            "requested_date": _normalize_date_str(requested_date),
            "selected_date": selected_date,
            "date_resolution": date_resolution,
            "latest_trade_date": latest_trade_date,
            "summary": {
                "sector_count": sector_count,
                "candidate_overlay_count": overlay_count,
                "theme_pool_count": theme_pool_count,
                "theme_match_count": theme_match_count,
                "watchlist_count": watchlist_count,
                "sector_watchlist_display_count": len(sector_watchlist_display),
                "watchlist_available_dates": watchlist_dates[-20:],
            },
            "sector_mainline": sector_factor_day.to_dict(orient="records"),
            "sector_candidate_overlay": sector_overlay_day.to_dict(orient="records"),
            "sector_member_snapshot": member_snapshot_day.to_dict(orient="records"),
            "theme_observation_pool": theme_pool_day.to_dict(orient="records"),
            "theme_strategy_overlay": theme_overlay_day.to_dict(orient="records"),
            "theme_addon": theme_addon_day.to_dict(orient="records"),
            "sector_watchlist": watchlist_df.to_dict(orient="records"),
            "sector_watchlist_display": sector_watchlist_display.to_dict(orient="records"),
            "sector_watchlist_mode": sector_watchlist_mode,
            "sector_watchlist_hint": sector_watchlist_hint,
            "raw_summaries": {
                "sector_factor": sector_summary_json,
                "sector_overlay": sector_overlay_summary_json,
                "sector_watchlist": watchlist_json,
            },
            "source_dates": {
                "sector_factor": _normalize_date_str(sector_summary_json.get("target_date")) if isinstance(sector_summary_json, dict) else None,
                "sector_overlay": member_snapshot_source_date,
                "sector_watchlist": _normalize_date_str(watchlist_json.get("trade_date")) if isinstance(watchlist_json, dict) else None,
                "theme_pool": _extract_df_latest_date(theme_pool_day, ["trade_date"]),
                "latest_trade": latest_trade_date,
            },
            "source_status": source_status,
            "paths": {
                "sector_summary": str(sector_summary_path) if sector_summary_path else None,
                "sector_overlay_summary": str(sector_overlay_summary_path) if sector_overlay_summary_path else None,
                "watchlist_json": str(watchlist_json_path) if watchlist_json_path else None,
            },
        }
    )

def _build_gen2_official_branch_freshness(requested_date: Optional[str], selected_date: Optional[str], official_dates: List[str]) -> Dict[str, Any]:
    latest_official_date = official_dates[-1] if official_dates else None
    target_date = _normalize_date_str(requested_date) or _normalize_date_str(selected_date)
    summary = _load_gen2_v2_complete_summary()
    latest_data_date = _normalize_date_str(summary.get("latest_data_date")) if isinstance(summary, dict) else None
    latest_source_date = _normalize_date_str(summary.get("latest_source_date")) if isinstance(summary, dict) else None
    is_fresh = bool(latest_official_date and target_date and latest_official_date >= target_date)
    if not latest_official_date:
        status = "missing"
        text = "g2_v2_complete 正式分支尚未生成"
    elif is_fresh:
        status = "fresh"
        text = f"g2_v2_complete 正式信号已覆盖到 {latest_official_date}"
    elif latest_data_date and target_date and latest_data_date >= target_date:
        status = "computed_no_signal"
        is_fresh = True
        text = (
            f"g2_v2_complete 已按 {latest_data_date} 最新数据重算；"
            f"最近正式买点停在 {latest_official_date}，当前请求日 {target_date} 无新增正式信号"
        )
    else:
        status = "stale"
        text = f"g2_v2_complete 正式信号最新仅到 {latest_official_date}，当前请求日为 {target_date}"
    return {
        "branch": "g2_v2_complete",
        "status": status,
        "latest_official_date": latest_official_date,
        "latest_data_date": latest_data_date,
        "latest_source_date": latest_source_date,
        "target_date": target_date,
        "is_fresh": is_fresh,
        "text": text,
    }


def _attach_gen2_quality_runup(rows: pd.DataFrame, factor_date: str) -> pd.DataFrame:
    if rows.empty or not factor_date:
        return rows.copy()
    codes = [str(x) for x in rows.get("code", pd.Series(dtype=str)).dropna().astype(str).unique().tolist()]
    if not codes:
        return rows.copy()
    placeholders = ", ".join(["?"] * len(codes))
    start = (pd.Timestamp(factor_date) - pd.Timedelta(days=180)).strftime("%Y-%m-%d")
    try:
        daily = clickhouse_query_df(
            f"""
            SELECT code, trade_date, low, close
            FROM kline_daily
            WHERE code IN ({placeholders})
              AND trade_date >= toDate(?)
              AND trade_date <= toDate(?)
            ORDER BY code, trade_date
            """,
            [*codes, start, factor_date],
        )
    except Exception as exc:
        logger.warning(f"load gen2 quality runup failed: {exc}")
        return rows.copy()
    if daily.empty:
        return rows.copy()
    daily["trade_date"] = pd.to_datetime(daily["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["low", "close"]:
        daily[col] = pd.to_numeric(daily[col], errors="coerce")
    daily = daily.dropna(subset=["code", "trade_date", "low", "close"]).copy()
    latest = daily[daily["trade_date"].eq(factor_date)].copy()
    if latest.empty:
        return rows.copy()
    low60 = (
        daily[daily["trade_date"].le(factor_date)]
        .sort_values(["code", "trade_date"])
        .groupby("code")
        .tail(60)
        .groupby("code")["low"]
        .min()
        .rename("low_60d")
        .reset_index()
    )
    latest = latest.merge(low60, on="code", how="left")
    latest["runup_from_60d_low"] = latest["close"] / latest["low_60d"] - 1.0
    return rows.merge(latest[["code", "runup_from_60d_low"]], on="code", how="left")


def _build_gen2_volume5_quality_rows(v4_pool_df: pd.DataFrame, selected_date: str, limit: int) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if v4_pool_df.empty:
        return [], {"available": False, "reason": "empty_v4_pool", "count": 0}
    try:
        from scripts.gen2_update_live_shadow import (
            ALPHA191_MAIN_THRESHOLD_Q,
            ALPHA191_VOLUME5_DIRECTIONS,
            ALPHA191_VOLUME5_FACTORS,
            _add_alpha191_percentile_scores,
            _load_alpha191_train_frame,
            _load_live_alpha191_values,
            _train_composite_threshold,
        )

        train = _load_alpha191_train_frame(
            GEN2_ALPHA191_TRAIN_SOURCE_PATH,
            GEN2_ALPHA191_TRAIN_VALUES_PATH,
            "2024-07-09",
            "2025-03-31",
        )
        base = v4_pool_df.copy()
        base["code"] = base["code"].astype(str)
        base["code6"] = base["code"].map(_normalize_stock_code6)
        live_values, factor_date = _load_live_alpha191_values(base, selected_date, factors=ALPHA191_VOLUME5_FACTORS)
        if live_values.empty:
            return [], {"available": False, "reason": "empty_alpha191_values", "count": 0}
        scored = base.merge(live_values.drop(columns=["date"], errors="ignore"), on="code6", how="left")
        scored = _add_alpha191_percentile_scores(
            scored,
            train,
            ALPHA191_VOLUME5_FACTORS,
            ALPHA191_VOLUME5_DIRECTIONS,
            "volume5",
        )
        threshold = _train_composite_threshold(
            train,
            ALPHA191_VOLUME5_FACTORS,
            ALPHA191_VOLUME5_DIRECTIONS,
            ALPHA191_MAIN_THRESHOLD_Q,
        )
        scored = _attach_gen2_quality_runup(scored, factor_date)
        scored["alpha191_volume5_rank_in_day"] = (
            pd.to_numeric(scored["alpha191_volume5_score"], errors="coerce")
            .rank(method="first", ascending=False, na_option="bottom")
            .fillna(999)
            .astype(int)
        )
        score = pd.to_numeric(scored["alpha191_volume5_score"], errors="coerce")
        runup = pd.to_numeric(scored.get("runup_from_60d_low"), errors="coerce")
        scored["pass_keep80"] = score.ge(float(threshold))
        scored["pass_runup"] = runup.le(1.00)
        scored["pass_quality"] = scored["pass_keep80"].fillna(False) & scored["pass_runup"].fillna(False)
        scored = scored.sort_values(
            ["pass_quality", "alpha191_volume5_score", "pass_runup", "v4_rank", "code"],
            ascending=[False, False, False, True, True],
            na_position="last",
        )
        records: List[Dict[str, Any]] = []
        for _, item in scored.head(int(limit)).iterrows():
            keep80 = bool(item.get("pass_keep80"))
            runup_ok = bool(item.get("pass_runup"))
            if keep80 and runup_ok:
                label = "通过"
                reason = "volume5_keep80 通过且runup<=1.0，分数足够"
            elif not keep80 and not runup_ok:
                label = "被拦截"
                reason = "volume5 未达到 keep80 或 runup 条件"
            elif not keep80:
                label = "量化条件未满足"
                reason = "volume5 未达到 keep80 阈值"
            else:
                label = "风险过滤"
                reason = "保持 60% 附近，runup 超限"
            code6 = str(item.get("code6") or _normalize_stock_code6(item.get("code")))
            records.append(
                {
                    "key": f"{code6}|volume5_quality",
                    "quality_family": "volume5",
                    "quality_family_label": "volume5",
                    "code": _to_exchange_stock_code(code6),
                    "code6": code6,
                    "name": str(item.get("name") or ""),
                    "entry_date": selected_date,
                    "factor_date": str(pd.Timestamp(factor_date).strftime("%Y-%m-%d") if factor_date else ""),
                    "v4_pool_date": str(item.get("v4_pool_date") or ""),
                    "v4_rank": _to_int(item.get("v4_rank"), default=0) or None,
                    "v4_score": _to_float(item.get("v4_score")),
                    "alpha191_volume5_score": _to_float(item.get("alpha191_volume5_score")),
                    "alpha191_volume5_rank_in_day": _to_int(item.get("alpha191_volume5_rank_in_day"), default=0) or None,
                    "alpha191_gate_threshold": _to_float(threshold),
                    "runup_from_60d_low": _to_float(item.get("runup_from_60d_low")),
                    "pass_keep80": keep80,
                    "pass_runup": runup_ok,
                    "pass_quality": bool(item.get("pass_quality")),
                    "quality_label": label,
                    "quality_type": "success" if bool(item.get("pass_quality")) else "warning",
                    "reason_text": reason,
                }
            )
        summary = {
            "available": True,
            "count": int(len(scored)),
            "pass_count": int(scored["pass_quality"].fillna(False).sum()),
            "threshold": _to_float(threshold),
            "factor_date": str(factor_date or ""),
            "factors": ALPHA191_VOLUME5_FACTORS,
        }
        return _sanitize(records), _sanitize(summary)
    except Exception as exc:
        logger.warning(f"build gen2 volume5 quality rows failed: {exc}")
        return [], {"available": False, "reason": str(exc), "count": 0}


def _build_gen2_breakout_quality_rows(v4_pool_df: pd.DataFrame, selected_date: str, limit: int) -> tuple[List[Dict[str, Any]], Dict[str, Any], pd.DataFrame]:
    if v4_pool_df.empty:
        return [], {"available": False, "reason": "empty_v4_pool", "count": 0, "stage3_count": 0, "stage4_count": 0}, pd.DataFrame()
    try:
        from scripts.gen2_backtest_open_v1_portfolio import _load_trade_dates
        from scripts.gen2_filter_signals_by_intraday_normal import DEFAULT_STATE_DAILY
        from scripts.gen2_update_live_shadow import _latest_breakout_setup, _load_daily_for_breakout, _load_g2_open_states

        trade_dates = _load_trade_dates("2024-07-09", selected_date)
        if selected_date not in trade_dates:
            return [], {"available": False, "reason": "not_trade_date", "count": 0, "stage3_count": 0, "stage4_count": 0}, pd.DataFrame()
        idx = trade_dates.index(selected_date)
        if idx <= 0:
            return [], {"available": False, "reason": "no_prev_trade_date", "count": 0, "stage3_count": 0, "stage4_count": 0}, pd.DataFrame()
        prev_date = trade_dates[idx - 1]

        base = v4_pool_df.copy()
        base["trade_date"] = prev_date
        if "code6" not in base.columns:
            base["code6"] = base["code"].map(_normalize_stock_code6)
        for col in ["entry_pass", "in_score_pool"]:
            if col in base.columns:
                base[col] = base[col].fillna(False).astype(bool)
        base = base.merge(_load_g2_open_states(Path(DEFAULT_STATE_DAILY)), on="trade_date", how="left")
        state_ok = base["g2_open_state"].isin(("NORMAL", "AGGRESSIVE")) | base["g2_open_state"].isna()
        mask = (
            base.get("in_score_pool", False).fillna(False).astype(bool)
            & pd.to_numeric(base.get("v4_rank"), errors="coerce").le(200)
            & state_ok
            & pd.to_numeric(base.get("mom10"), errors="coerce").ge(0.02)
            & pd.to_numeric(base.get("mom20"), errors="coerce").ge(0.03)
            & pd.to_numeric(base.get("mom5"), errors="coerce").le(0.12)
            & pd.to_numeric(base.get("vol_ratio"), errors="coerce").le(3.0)
            & pd.to_numeric(base.get("vol10"), errors="coerce").le(0.10)
        )
        contexts = base[mask].copy()
        if contexts.empty:
            return [], {"available": True, "count": 0, "stage3_count": 0, "stage4_count": 0, "factor_date": prev_date}, pd.DataFrame()

        codes = contexts["code"].dropna().astype(str).unique().tolist()
        daily = _load_daily_for_breakout(codes, (pd.Timestamp(selected_date) - pd.Timedelta(days=100)).strftime("%Y-%m-%d"), prev_date)
        if daily.empty:
            return [], {"available": False, "reason": "empty_daily", "count": int(len(contexts)), "stage3_count": 0, "stage4_count": 0}, pd.DataFrame()

        rows: List[Dict[str, Any]] = []
        ctx_map = {str(row.code): row._asdict() for row in contexts.itertuples(index=False)}
        for code, hist in daily.groupby("code", sort=False):
            setup = _latest_breakout_setup(hist, selected_date)
            if not setup:
                continue
            ctx = ctx_map.get(str(code), {})
            ctx_state = ctx.get("g2_open_state")
            ctx_state = "" if pd.isna(ctx_state) else str(ctx_state)
            setup_top = _to_float(setup.get("setup_big_bull_rebreak_2_5d_top"))
            prev_close = _to_float(setup.get("prev_close"))
            box_gap = (prev_close / setup_top - 1.0) if setup_top and prev_close else None
            stage_no = 4 if box_gap is not None and box_gap >= -0.03 else 3
            code6 = str(ctx.get("code6") or _normalize_stock_code6(code))
            reason = (
                "D-1: 反抽后10/15个点以内放量且涨幅未过3%。"
                if stage_no == 4
                else "D-1: 反抽后出现明显回踩/缺口，不满足主线质控要求。"
            )
            rows.append(
                {
                    **ctx,
                    **setup,
                    "key": f"{code6}|breakout_quality",
                    "quality_family": "breakout",
                    "quality_family_label": "二次突破",
                    "code": _to_exchange_stock_code(code6),
                    "code6": code6,
                    "name": str(ctx.get("name") or ""),
                    "entry_date": selected_date,
                    "factor_date": prev_date,
                    "v4_pool_date": str(ctx.get("v4_pool_date") or prev_date),
                    "v4_rank": _to_int(ctx.get("v4_rank"), default=0) or None,
                    "v4_score": _to_float(ctx.get("v4_score")),
                    "g2_open_state": ctx_state,
                    "setup_box_top": setup_top,
                    "setup_box_low": _to_float(setup.get("setup_big_bull_rebreak_2_5d_low")),
                    "setup_box_range": _to_float(setup.get("setup_big_bull_rebreak_2_5d_range")),
                    "setup_big_bull_date": str(setup.get("setup_big_bull_date") or ""),
                    "setup_big_bull_amount_ratio": _to_float(setup.get("setup_big_bull_amount_ratio")),
                    "big_bull_lag_days": _to_int(setup.get("big_bull_lag_days"), default=0) or None,
                    "prev_close": prev_close,
                    "close_vs_box_top": _to_float(box_gap),
                    "breakout_stage_no": stage_no,
                    "breakout_stage_label": "阶段4: 继续放量并持续上冲" if stage_no == 4 else "阶段3: 回踩后回升不够理想",
                    "pass_breakout_prefilter": True,
                    "pass_breakout_setup": True,
                    "pass_breakout_stage": True,
                    "pass_quality": True,
                    "quality_label": "阶段4: 继续放量并持续上冲" if stage_no == 4 else "阶段3: 回踩后回升不够理想",
                    "quality_type": "success" if stage_no == 4 else "warning",
                    "reason_text": reason,
                    "source_family": "big_bull_d1_stage",
                    "signal_family": f"d1_breakout_stage{stage_no}",
                    "g2_v2_buy_logic": "big_bull_rebreak_2_5d D-1 stage watch; wait intraday strength + sector_strong",
                    "confirm_datetime": "",
                    "entry_price": None,
                    "_pool_key": f"{code6}|breakout_stage",
                }
            )

        rows = sorted(
            rows,
            key=lambda item: (
                -int(item.get("breakout_stage_no") or 0),
                abs(_to_float(item.get("close_vs_box_top")) or 9),
                item.get("v4_rank") or 9999,
                item.get("code6") or "",
            ),
        )
        summary = {
            "available": True,
            "count": int(len(rows)),
            "stage3_count": int(sum(1 for row in rows if int(row.get("breakout_stage_no") or 0) == 3)),
            "stage4_count": int(sum(1 for row in rows if int(row.get("breakout_stage_no") or 0) == 4)),
            "factor_date": prev_date,
        }
        return _sanitize(rows[: int(limit)]), _sanitize(summary), pd.DataFrame(rows)
    except Exception as exc:
        logger.warning(f"build gen2 breakout quality rows failed: {exc}")
        return [], {"available": False, "reason": str(exc), "count": 0, "stage3_count": 0, "stage4_count": 0}, pd.DataFrame()


def _build_gen2_selection_pool(signal_date: Optional[str], limit: int) -> Dict[str, Any]:
    dates = _gen2_live_update_dates()
    ledger_df = _load_gen2_risk_cool_shadow_ledger()
    if ledger_df.empty:
        ledger_dates: List[str] = []
    else:
        ledger_dates = sorted([str(x) for x in ledger_df.get("entry_date", pd.Series(dtype=str)).dropna().unique() if str(x)])
    official_dates = _load_gen2_v2_complete_dates()
    all_dates = sorted(set(dates + ledger_dates + official_dates))
    selectable_dates = all_dates
    if not selectable_dates:
        return {
            "available": False,
            "rows": [],
            "message": "G2候选复权状态未准备好：暂无可用快照",
            "pipeline": [],
            "data_freshness": build_gen2_data_freshness(signal_date, None),
        }

    requested_date = _normalize_date_str(signal_date) if signal_date else None
    selected_date = selectable_dates[-1]
    if requested_date:
        earlier_dates = [x for x in selectable_dates if x <= requested_date]
        selected_date = earlier_dates[-1] if earlier_dates else selectable_dates[-1]
    branch_freshness = _build_gen2_official_branch_freshness(requested_date, selected_date, official_dates)
    official_branch_fresh = bool(branch_freshness.get("is_fresh"))

    summary = _load_gen2_live_update_summary(selected_date)
    pool_rank = _to_int(summary.get("pool_rank") if summary else None, default=200) or 200
    v4_pool_df = _load_gen2_v4_pool_context(selected_date, pool_rank, "d1")
    v4_d_pool_df = _load_gen2_v4_pool_context(selected_date, pool_rank, "d")
    volume5_quality_rows, volume5_quality_summary = _build_gen2_volume5_quality_rows(v4_pool_df, selected_date, int(limit))
    breakout_quality_rows, breakout_quality_summary, breakout_stage_df = _build_gen2_breakout_quality_rows(v4_pool_df, selected_date, int(limit))
    quality_rows = volume5_quality_rows + breakout_quality_rows
    quality_summary = {
        "available": bool(volume5_quality_summary.get("available") or breakout_quality_summary.get("available")),
        "count": int(volume5_quality_summary.get("count") or 0) + int(breakout_quality_summary.get("count") or 0),
        "pass_count": int(volume5_quality_summary.get("pass_count") or 0) + int(breakout_quality_summary.get("count") or 0),
        "volume5": volume5_quality_summary,
        "breakout": breakout_quality_summary,
    }
    volume5_quality_by_code = {
        str(row.get("code6") or _normalize_stock_code6(row.get("code"))): row
        for row in volume5_quality_rows
        if row.get("pass_quality")
    }
    raw_df = _load_gen2_live_update_frame(selected_date, "raw_candidates")
    pre_df = _load_gen2_live_update_frame(selected_date, "pre_alpha191_signals")
    final_df = _load_gen2_live_update_frame(selected_date, "filtered_signals")
    official_df = _load_gen2_v2_complete_day(selected_date)
    day_ledger = pd.DataFrame()
    if not ledger_df.empty and "entry_date" in ledger_df.columns:
        day_ledger = ledger_df[ledger_df["entry_date"].eq(selected_date)].copy()
        if not day_ledger.empty:
            day_ledger["code6"] = day_ledger["code"].map(_normalize_stock_code6)
            day_ledger["confirm_datetime"] = pd.to_datetime(day_ledger["confirm_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
            day_ledger["_pool_key"] = day_ledger["code6"].astype(str) + "|" + day_ledger["confirm_datetime"].astype(str)

    frame_maps = []
    for df in [v4_pool_df, breakout_stage_df, raw_df, pre_df, final_df, official_df, day_ledger]:
        frame_maps.append({str(row.get("_pool_key")): row for _, row in df.iterrows()} if not df.empty else {})
    v4_map, breakout_stage_map, raw_map, pre_map, final_map, official_map, ledger_map = frame_maps
    v4_codes = set(v4_pool_df["code6"].dropna().astype(str).tolist()) if not v4_pool_df.empty and "code6" in v4_pool_df.columns else set()
    keys: List[str] = []
    for df in [v4_pool_df, breakout_stage_df, raw_df, pre_df, final_df, official_df, day_ledger]:
        if not df.empty and "_pool_key" in df.columns:
            keys.extend([str(x) for x in df["_pool_key"].dropna().tolist() if str(x) and not str(x).startswith("|")])
    keys = list(dict.fromkeys(keys))

    rows: List[Dict[str, Any]] = []
    for key in keys:
        v4 = v4_map.get(key)
        breakout_stage = breakout_stage_map.get(key)
        raw = raw_map.get(key)
        pre = pre_map.get(key)
        final = final_map.get(key)
        official = official_map.get(key)
        ledger = ledger_map.get(key)
        source = _first_pool_row(ledger, final, official, pre, raw, breakout_stage, v4)
        if source is None:
            continue
        status = str(_row_value(ledger, "shadow_status", "") or "")
        is_suspended = status in {"suspended_by_two_stop_cd3", "suspended_by_stop_cd5"}
        pass_raw = raw is not None
        pass_pre = pre is not None
        pass_official = official is not None
        pass_official_live = bool(pass_official and official_branch_fresh)
        signal_family = str(_row_value(source, "signal_family", "") or "")
        source_family = str(_row_value(source, "source_family", "") or "")
        pass_breakout_stage = bool(breakout_stage is not None or source_family == "big_bull_d1_stage")
        pass_final = final is not None or pass_official_live
        code6 = str(_row_value(source, "code6", "") or "")
        volume5_quality = volume5_quality_by_code.get(code6)
        pass_volume5_quality = bool(volume5_quality)
        pass_volume5_trigger = bool(pass_raw and pass_volume5_quality)
        pass_mainline1 = bool(final is not None or signal_family.startswith("volume5") or source_family == "volume5" or pass_volume5_trigger)
        pass_mainline2 = bool(signal_family.startswith("breakout") or source_family == "big_bull")
        pass_v4_pool = v4 is not None or code6 in v4_codes or pass_raw or pass_pre or pass_final
        buyable = pass_final and not is_suspended
        if buyable:
            stage_label = "可买入"
            stage_type = "success"
            default_reason = "当前候选满足主线条件"
            if pass_mainline1 and pass_mainline2:
                default_reason = "当前阶段 volume5 与 breakout 同时满足"
            elif pass_mainline1:
                default_reason = "当前阶段 volume5 持续买入"
            elif pass_mainline2:
                default_reason = "当前阶段 breakout 触发且量能增强"
            reason = str(_row_value(ledger, "execution_note", "") or default_reason)
        elif pass_breakout_stage:
            stage_no = _to_int(_row_value(source, "breakout_stage_no"), default=0) or 0
            stage_label = "二突四层" if stage_no >= 4 else "二突三层"
            stage_type = "success" if stage_no >= 4 else "warning"
            reason = str(_row_value(source, "reason_text", "") or "当前阶段为突破候选，建议保持观察")
        elif pass_volume5_trigger:
            stage_label = "volume5触发"
            stage_type = "success" if pass_pre else "warning"
            reason = "最近持续触发 volume5 与大额信号，建议关注，后续按 2:00 复核"
        elif not pass_raw and not pass_official:
            stage_label = "无基础条件"
            stage_type = "info"
            reason = "D-1 V4 尚无有效基础信号，当前仅返回 2m/30m 预警提示"
        elif not pass_pre:
            stage_label = "风控过滤"
            stage_type = "danger"
            reason = "被风险降温控制拦截"
        elif not pass_final:
            stage_label = "主线过滤"
            stage_type = "warning"
            reason = "未通过最终风控门槛：volume5_keep80_runup 与突破阶段不兼容"
        elif is_suspended:
            stage_label = "熔断暂停"
            stage_type = "danger"
            reason = str(_row_value(ledger, "execution_note", "") or "该股票被交易所涨跌幅降级风控拦截")
        else:
            stage_label = "待观察"
            stage_type = "info"
            reason = str(_row_value(ledger, "execution_note", "") or "候选已通过风险复核，当前建议观望")
        rows.append(
            {
                "key": key,
                "code": _to_exchange_stock_code(code6) if code6 else str(_row_value(source, "code", "") or ""),
                "code6": code6,
                "name": str(_row_value(source, "name", "") or ""),
                "entry_date": str(_row_value(source, "entry_date", selected_date) or selected_date),
                "confirm_datetime": str(_row_value(source, "confirm_datetime", "") or ""),
                "entry_price": _to_float(_row_value(source, "entry_price")),
                "v4_rank": _to_int(_row_value(source, "alpha191_original_v4_rank", _row_value(source, "v4_rank")), default=0) or None,
                "v4_score": _to_float(_row_value(source, "alpha191_original_v4_score", _row_value(source, "v4_score"))),
                "alpha191_volume5_score": _to_float(_row_value(source, "alpha191_volume5_score", _row_value(source, "alpha191_gate_score"))) if not volume5_quality else _to_float(volume5_quality.get("alpha191_volume5_score")),
                "alpha191_volume5_rank_in_day": (_to_int(_row_value(source, "alpha191_volume5_rank_in_day"), default=0) or None) if not volume5_quality else (_to_int(volume5_quality.get("alpha191_volume5_rank_in_day"), default=0) or None),
                "alpha191_gate_threshold": _to_float(_row_value(source, "alpha191_gate_threshold")) if not volume5_quality else _to_float(volume5_quality.get("alpha191_gate_threshold")),
                "alpha191_factor_date": str((volume5_quality.get("factor_date") if volume5_quality else None) or _row_value(source, "alpha191_factor_date", "") or ""),
                "runup_from_60d_low": _to_float(_row_value(source, "runup_from_60d_low")) if not volume5_quality else _to_float(volume5_quality.get("runup_from_60d_low")),
                "overhead_pressure_amount_share": _to_float(_row_value(source, "overhead_pressure_amount_share")),
                "source_family": source_family,
                "signal_family": signal_family,
                "g2_v2_buy_logic": str(_row_value(source, "g2_v2_buy_logic", "") or ""),
                "sector_strong": bool(_row_value(source, "sector_strong", False)),
                "l3_rt_strong3_ratio": _to_float(_row_value(source, "l3_rt_strong3_ratio", _row_value(source, "l3_s3"))),
                "sector_score_bonus": _to_float(_row_value(source, "sector_score_bonus")),
                "v4_score_raw": _to_float(_row_value(source, "v4_score_raw")),
                "rt_return_pct": _pct_value(_row_value(source, "rt_return_from_d1_close")),
                "amount_ratio": _to_float(_row_value(source, "rt_30m_amount_ratio")),
                "pass_v4_pool": pass_v4_pool,
                "v4_pool_date": str(_row_value(source, "v4_pool_date", "") or ""),
                "pass_v4_g2_trigger": pass_raw,
                "pass_risk_cool": pass_pre,
                "pass_alpha191": pass_mainline1,
                "pass_volume5_quality": pass_volume5_quality,
                "pass_volume5_trigger": pass_volume5_trigger,
                "pass_mainline1": pass_mainline1,
                "pass_mainline2": pass_mainline2,
                "pass_breakout_stage": pass_breakout_stage,
                "breakout_stage_no": _to_int(_row_value(source, "breakout_stage_no"), default=0) or None,
                "breakout_stage_label": str(_row_value(source, "breakout_stage_label", "") or ""),
                "setup_big_bull_date": str(_row_value(source, "setup_big_bull_date", "") or ""),
                "big_bull_lag_days": _to_int(_row_value(source, "big_bull_lag_days"), default=0) or None,
                "setup_box_top": _to_float(_row_value(source, "setup_box_top", _row_value(source, "setup_big_bull_rebreak_2_5d_top"))),
                "setup_box_range": _to_float(_row_value(source, "setup_box_range", _row_value(source, "setup_big_bull_rebreak_2_5d_range"))),
                "close_vs_box_top": _to_float(_row_value(source, "close_vs_box_top")),
                "pass_official_v2": pass_official,
                "pass_official_v2_live": pass_official_live,
                "official_branch_fresh": official_branch_fresh,
                "buyable": buyable,
                "shadow_status": status,
                "status_label": _gen2_shadow_status_label(status) if status else "",
                "stage_label": stage_label,
                "stage_type": stage_type,
                "reason_text": reason,
            }
        )

    rows.sort(
        key=lambda item: (
            0 if item.get("buyable") else 1,
            1 if item.get("pass_breakout_stage") else 2 if item.get("pass_v4_pool") and not item.get("pass_v4_g2_trigger") else 0,
            item.get("alpha191_volume5_rank_in_day") or 9999,
            item.get("v4_rank") or 9999,
            item.get("confirm_datetime") or "",
        )
    )
    full_rows = rows
    trigger_rows = [row for row in full_rows if row.get("pass_v4_g2_trigger")]
    complete_rows = [
        row
        for row in full_rows
        if row.get("pass_official_v2_live") or row.get("pass_mainline1") or row.get("pass_mainline2") or row.get("pass_breakout_stage") or row.get("buyable")
    ]
    rows = full_rows[: int(limit)]
    raw_count = _to_int(summary.get("raw_candidates") if summary else None, default=len(raw_df))
    pre_count = _to_int(summary.get("filtered_signals_before_alpha191") if summary else None, default=len(pre_df))
    final_count = _to_int(summary.get("filtered_signals") if summary else None, default=len(final_df))
    official_volume5_count = int((official_df.get("source_family", pd.Series(dtype=str)).astype(str) == "volume5").sum()) if (official_branch_fresh and not official_df.empty) else 0
    official_breakout_count = int((official_df.get("source_family", pd.Series(dtype=str)).astype(str) == "big_bull").sum()) if (official_branch_fresh and not official_df.empty) else 0
    mainline1_count = max(final_count, official_volume5_count)
    mainline2_count = official_breakout_count
    intraday_state = summary.get("intraday_state") if isinstance(summary, dict) else {}
    v4_trigger_note = "D-1 V4 rank -> 30m before_confirm 的 NORMAL 门控。"
    if isinstance(intraday_state, dict):
        if intraday_state.get("fallback") == "skip_intraday_normal":
            v4_trigger_note = "D-1 V4 rank -> 30m before_confirm 全部未触发 NORMAL，已按配置跳过该门控回退。"
        elif intraday_state.get("reason") == "intraday_state_empty":
            v4_trigger_note = "D-1 V4 rank -> 30m before_confirm 未能构建 intraday_state，按无候选处理。"
        elif intraday_state.get("reason") == "all_intraday_normal_false":
            v4_trigger_note = "D-1 V4 rank -> 30m before_confirm 全部为 NORMAL=false。"
        elif intraday_state.get("reason"):
            v4_trigger_note = f"D-1 V4 rank -> 30m before_confirm 过滤原因: {intraday_state.get('reason')}。"
    pipeline = [
        {
            "key": "v4_pool",
            "label": "D-1 V4",
            "count": int(len(v4_pool_df)),
            "note": "D-1 V4 原始候选池：先按 30m before_confirm normal 门控后进入主链。",
        },
        {
            "key": "volume5_quality",
            "label": "volume5 质检",
            "count": int(quality_summary.get("pass_count") or 0),
            "note": "D-1 V4 + Alpha191 volume5（keep80 且 runup<=100%）。",
        },
        {
            "key": "v4_g2_trigger",
            "label": "V4/G2 触发",
            "count": raw_count,
            "note": v4_trigger_note,
        },
        {
            "key": "risk_cool",
            "label": "风控过滤",
            "count": pre_count,
            "note": "User V2 风控链路的预过滤结果。",
        },
        {
            "key": "mainline1",
            "label": "主线1 volume5",
            "count": mainline1_count,
            "note": "主线1：volume5_keep80_runup（volume5 正向主线）。",
        },
        {
            "key": "mainline2",
            "label": "主线2 breakout+big_bull",
            "count": mainline2_count,
            "note": "主线2：big_bull 与 3_rt_strong3_ratio 组合，通过二级筛选。",
        },
        {
            "key": "official_v2_complete",
            "label": "官方V2完整链路",
            "count": int(len(official_df)),
            "note": str(branch_freshness.get("text") or "g2_v2_complete 分支可用性不足"),
        },
        {
            "key": "buyable",
            "label": "可买标记",
            "count": int(sum(1 for row in full_rows if row.get("buyable"))),
            "note": "最终可买且通过主链条件的候选数。",
        },
    ]
    return _sanitize(
        {
            "available": True,
            "signal_date": selected_date,
            "requested_date": requested_date,
            "source_latest_date": all_dates[-1],
            "available_dates": selectable_dates,
            "branch_freshness": branch_freshness,
            "live_trade_mode": "latest_driven_mainline",
            "official_branch_blocks_live": False,
            "strategy_code": "g2_alpha191_volume5_keep80_runup",
            "strategy_name": "G2 主线：volume5量能 + 量价突破 + breakout趋势",
            "alpha191_gate": "g2_v2_complete",
            "strategy_mode": "g2_v2_complete",
            "summary": summary,
            "pipeline": pipeline,
            "row_count": len(full_rows),
            "visible_row_count": len(rows),
            "trigger_count": len(trigger_rows),
            "complete_count": len(complete_rows),
            "quality_count": int(quality_summary.get("count") or 0),
            "quality_pass_count": int(quality_summary.get("pass_count") or 0),
            "volume5_quality_count": int(volume5_quality_summary.get("count") or 0),
            "volume5_quality_pass_count": int(volume5_quality_summary.get("pass_count") or 0),
            "breakout_quality_count": int(breakout_quality_summary.get("count") or 0),
            "breakout_quality_stage3_count": int(breakout_quality_summary.get("stage3_count") or 0),
            "breakout_quality_stage4_count": int(breakout_quality_summary.get("stage4_count") or 0),
            "rows": rows,
            "trigger_rows": trigger_rows[: int(limit)],
            "complete_rows": complete_rows[: int(limit)],
            "quality_rows": quality_rows[: int(limit)],
            "quality_summary": quality_summary,
            "v4_d1_rows": _gen2_v4_pool_records(v4_pool_df, selected_date, "D-1", int(limit)),
            "v4_d_rows": _gen2_v4_pool_records(v4_d_pool_df, selected_date, "D", int(limit)),
            "data_freshness": build_gen2_data_freshness(requested_date, selected_date),
            "message": "返回 D-1 V4/G2候选；若 D 日数据不足会自动退化到 volume5+breakout 可买列表。",
        }
    )


def _load_gen2_open_state_for_date(signal_date: Optional[str]) -> Dict[str, Any]:
    selected_date = _normalize_date_str(signal_date)
    if not selected_date:
        return {"state": "UNKNOWN", "source": str(GEN2_OPEN_STATE_DAILY_PATH), "available": False}
    if not GEN2_OPEN_STATE_DAILY_PATH.exists():
        return {"state": "UNKNOWN", "source": str(GEN2_OPEN_STATE_DAILY_PATH), "available": False}
    try:
        df = pd.read_csv(GEN2_OPEN_STATE_DAILY_PATH)
    except Exception as exc:
        logger.warning(f"load G2 open state for ticket failed: {exc}")
        return {
            "state": "UNKNOWN",
            "source": str(GEN2_OPEN_STATE_DAILY_PATH),
            "available": False,
            "error": str(exc),
        }
    if df.empty or "trade_date" not in df.columns:
        return {"state": "UNKNOWN", "source": str(GEN2_OPEN_STATE_DAILY_PATH), "available": False}
    d = df.copy()
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    state_col = "g2_open_state" if "g2_open_state" in d.columns else "state"
    if state_col not in d.columns:
        return {"state": "UNKNOWN", "source": str(GEN2_OPEN_STATE_DAILY_PATH), "available": False}
    exact = d[d["trade_date"].eq(selected_date)].copy()
    date_source = "exact"
    if exact.empty:
        prior_dates = sorted([str(x) for x in d["trade_date"].dropna().unique() if str(x) <= selected_date])
        if not prior_dates:
            return {"state": "UNKNOWN", "source": str(GEN2_OPEN_STATE_DAILY_PATH), "available": False}
        exact = d[d["trade_date"].eq(prior_dates[-1])].copy()
        date_source = "fallback_previous"
    row = exact.tail(1).iloc[0]
    state = str(row.get(state_col) or "UNKNOWN").strip().upper() or "UNKNOWN"
    return _sanitize(
        {
            "state": state,
            "trade_date": str(row.get("trade_date") or ""),
            "requested_date": selected_date,
            "date_source": date_source,
            "source": str(GEN2_OPEN_STATE_DAILY_PATH),
            "available": True,
            "raw": row.where(pd.notna(row), None).to_dict(),
        }
    )


def _gen2_ticket_position_policy(open_state: str) -> Dict[str, Any]:
    state = str(open_state or "UNKNOWN").strip().upper()
    if state == "AGGRESSIVE":
        return {"can_open": True, "target_exposure": "60%-90%", "max_new_positions": 3, "single_position": "25%-35%"}
    if state == "NORMAL":
        return {"can_open": True, "target_exposure": "30%-60%", "max_new_positions": 2, "single_position": "20%-30%"}
    if state == "PROBE":
        return {"can_open": False, "target_exposure": "0%-20%", "max_new_positions": 0, "single_position": "0%"}
    if state == "OFF":
        return {"can_open": False, "target_exposure": "0%", "max_new_positions": 0, "single_position": "0%"}
    return {"can_open": False, "target_exposure": "0%", "max_new_positions": 0, "single_position": "0%"}


def _gen2_ticket_strategy_source(row: Dict[str, Any]) -> str:
    if row.get("pass_mainline1") and row.get("pass_mainline2"):
        return "volume5 + big_bull"
    if row.get("pass_mainline2"):
        return "big_bull"
    if row.get("pass_mainline1") or str(row.get("source_family") or "") == "volume5":
        return "volume5"
    return str(row.get("source_family") or row.get("signal_family") or "g2_v2_complete")


def _gen2_ticket_candidate(row: Dict[str, Any], index: int, policy: Dict[str, Any]) -> Dict[str, Any]:
    code = str(row.get("code") or row.get("code6") or "")
    name = str(row.get("name") or "")
    entry_price = _to_float(row.get("entry_price"))
    single_position = str(policy.get("single_position") or "0%")
    strategy_source = _gen2_ticket_strategy_source(row)
    buy_area = "等待盘中确认价"
    if entry_price is not None:
        buy_area = f"{entry_price:.2f} 附近，严禁明显高开/直线拉升后追价"
    return _sanitize(
        {
            "rank": int(index),
            "code": code,
            "code6": str(row.get("code6") or "") or _normalize_stock_code6(code),
            "name": name,
            "strategy_source": strategy_source,
            "stage_label": row.get("stage_label"),
            "confirm_datetime": row.get("confirm_datetime"),
            "entry_price": entry_price,
            "buy_area": buy_area,
            "suggested_position": single_position,
            "auto_order_allowed": False,
            "requires_manual_approval": True,
            "evidence": {
                "v4_rank": row.get("v4_rank"),
                "v4_score": row.get("v4_score"),
                "alpha191_volume5_score": row.get("alpha191_volume5_score"),
                "alpha191_volume5_rank_in_day": row.get("alpha191_volume5_rank_in_day"),
                "source_family": row.get("source_family"),
                "signal_family": row.get("signal_family"),
                "g2_v2_buy_logic": row.get("g2_v2_buy_logic"),
                "l3_rt_strong3_ratio": row.get("l3_rt_strong3_ratio"),
                "sector_score_bonus": row.get("sector_score_bonus"),
                "pass_mainline1": row.get("pass_mainline1"),
                "pass_mainline2": row.get("pass_mainline2"),
                "pass_official_v2_live": row.get("pass_official_v2_live"),
            },
            "buy_conditions": [
                "只在交易单允许开仓时执行",
                "必须属于 G2 V4 g2_v2_complete 正式候选",
                "盘中 15m/30m 确认信号不能撤销或过期",
                "不追直线拉升，不在明显高开透支后临时加价",
            ],
            "invalid_conditions": [
                "市场状态降为 PROBE/OFF",
                "候选从 G2 V4 正式可买池消失",
                "盘中跌破确认结构或买入理由失效",
                "出现 ST、停牌、涨停买不到或明显流动性异常",
            ],
            "risk_rules": [
                "单票浮亏 -4% 后禁止加仓",
                "单票浮亏 -6% 必须处理",
                "买入后 3 个交易日仍未按预期走强则降级复盘",
            ],
            "review_points": ["T+1 表现", "T+3 是否走强", "T+5 盈亏与买点质量归因"],
            "reason_text": row.get("reason_text"),
        }
    )


def _gen2_ticket_markdown(ticket: Dict[str, Any]) -> str:
    lines = [
        f"# G2 V4 Daily Trade Ticket - {ticket.get('signal_date') or ''}",
        "",
        f"- 生成时间：{ticket.get('generated_at') or ''}",
        f"- 策略口径：{ticket.get('strategy_code') or ''}",
        f"- 市场状态：{ticket.get('market_state') or 'UNKNOWN'}",
        f"- 今日是否允许开仓：{'是' if ticket.get('can_open') else '否'}",
        f"- 允许总仓位：{ticket.get('target_exposure') or '0%'}",
        f"- 正式候选数：{len(ticket.get('formal_candidates') or [])}",
        f"- 自动下单：关闭，必须人工确认",
        "",
        "## 今日纪律",
    ]
    for item in ticket.get("forbidden_actions") or []:
        lines.append(f"- {item}")
    lines.extend(["", "## 正式候选"])
    candidates = ticket.get("formal_candidates") or []
    if not candidates:
        lines.append("- 今日无正式买入候选。")
    for item in candidates:
        evidence = item.get("evidence") or {}
        lines.extend(
            [
                "",
                f"### {item.get('rank')}. {item.get('code')} {item.get('name')}",
                f"- 来源：{item.get('strategy_source')}",
                f"- 买入区间：{item.get('buy_area')}",
                f"- 建议仓位：{item.get('suggested_position')}",
                f"- 确认时间：{item.get('confirm_datetime') or '-'}",
                f"- V4：rank={evidence.get('v4_rank')}, score={evidence.get('v4_score')}",
                f"- 主线证据：mainline1={evidence.get('pass_mainline1')}, mainline2={evidence.get('pass_mainline2')}, l3={evidence.get('l3_rt_strong3_ratio')}",
                f"- 理由：{item.get('reason_text') or '-'}",
                "- 失效条件：" + "；".join(str(x) for x in (item.get("invalid_conditions") or [])),
                "- 风控规则：" + "；".join(str(x) for x in (item.get("risk_rules") or [])),
            ]
        )
    lines.extend(["", "## 数据状态"])
    for stage in ticket.get("pipeline") or []:
        lines.append(f"- {stage.get('label')}: {stage.get('count')} | {stage.get('note')}")
    return "\n".join(lines).rstrip() + "\n"


def _save_gen2_daily_trade_ticket(ticket: Dict[str, Any]) -> Dict[str, Any]:
    signal_date = _normalize_date_str(ticket.get("signal_date")) or datetime.now().strftime("%Y-%m-%d")
    GEN2_DAILY_TICKET_DIR.mkdir(parents=True, exist_ok=True)
    json_path = GEN2_DAILY_TICKET_DIR / f"{signal_date}.json"
    md_path = GEN2_DAILY_TICKET_DIR / f"{signal_date}.md"
    markdown = _gen2_ticket_markdown(ticket)
    payload = dict(ticket)
    payload["markdown"] = markdown
    json_text = json.dumps(_sanitize(payload), ensure_ascii=False, indent=2, default=str)
    json_path.write_text(json_text, encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
    GEN2_DAILY_TICKET_LATEST_PATH.write_text(json_text, encoding="utf-8")
    (GEN2_DAILY_TICKET_DIR / "latest.md").write_text(markdown, encoding="utf-8")
    return {"json_path": str(json_path), "markdown_path": str(md_path), "latest_path": str(GEN2_DAILY_TICKET_LATEST_PATH)}


def _load_gen2_daily_execution_ledger() -> List[Dict[str, Any]]:
    if not GEN2_DAILY_EXECUTION_LEDGER_PATH.exists():
        return []
    try:
        data = json.loads(GEN2_DAILY_EXECUTION_LEDGER_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"load gen2 daily execution ledger failed: {exc}")
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get("records"), list):
        return [item for item in data.get("records") if isinstance(item, dict)]
    return []


def _save_gen2_daily_execution_ledger(records: List[Dict[str, Any]]) -> None:
    GEN2_DAILY_TICKET_DIR.mkdir(parents=True, exist_ok=True)
    clean_records = sorted(
        [_sanitize(item) for item in records if isinstance(item, dict)],
        key=lambda item: (str(item.get("signal_date") or ""), str(item.get("updated_at") or "")),
        reverse=True,
    )
    GEN2_DAILY_EXECUTION_LEDGER_PATH.write_text(
        json.dumps(clean_records, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _find_gen2_daily_execution_record(signal_date: str, strategy_code: str = "g2_v2_complete") -> Optional[Dict[str, Any]]:
    target_date = _normalize_date_str(signal_date)
    target_strategy = str(strategy_code or "g2_v2_complete")
    if not target_date:
        return None
    for item in _load_gen2_daily_execution_ledger():
        if _normalize_date_str(item.get("signal_date")) == target_date and str(item.get("strategy_code") or "") == target_strategy:
            return item
    return None


def _upsert_gen2_daily_execution_record(payload: Dict[str, Any]) -> Dict[str, Any]:
    signal_date = _normalize_date_str(payload.get("signal_date"))
    if not signal_date:
        latest = _build_gen2_daily_trade_ticket(None, limit=30, formal_limit=3, persist=True)
        signal_date = _normalize_date_str(latest.get("signal_date"))
    if not signal_date:
        return {"ok": False, "error": "missing signal_date"}

    strategy_code = str(payload.get("strategy_code") or "g2_v2_complete")
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    records = _load_gen2_daily_execution_ledger()
    record_id = f"{signal_date}|{strategy_code}"
    existing_index = -1
    existing: Dict[str, Any] = {}
    for idx, item in enumerate(records):
        if str(item.get("record_id") or "") == record_id:
            existing_index = idx
            existing = dict(item)
            break
        if _normalize_date_str(item.get("signal_date")) == signal_date and str(item.get("strategy_code") or "") == strategy_code:
            existing_index = idx
            existing = dict(item)
            break

    allowed_status = {"pending", "followed", "no_trade", "partial", "violated", "reviewed"}
    execution_status = str(payload.get("execution_status") or existing.get("execution_status") or "pending").strip().lower()
    if execution_status not in allowed_status:
        execution_status = "pending"

    ticket = _build_gen2_daily_trade_ticket(signal_date, limit=30, formal_limit=3, persist=True)
    candidate_codes = [
        str(item.get("code") or item.get("code6") or "").strip()
        for item in (ticket.get("formal_candidates") or [])
        if isinstance(item, dict)
    ]
    record = {
        **existing,
        "record_id": record_id,
        "signal_date": signal_date,
        "strategy_code": strategy_code,
        "mode": ticket.get("mode") or "shadow_only",
        "market_state": ticket.get("market_state") or "",
        "can_open": bool(ticket.get("can_open")),
        "formal_candidate_count": int(ticket.get("formal_candidate_count") or 0),
        "formal_candidate_codes": candidate_codes,
        "execution_status": execution_status,
        "executed": bool(payload.get("executed")) if "executed" in payload else bool(existing.get("executed", False)),
        "discipline_ok": bool(payload.get("discipline_ok")) if "discipline_ok" in payload else bool(existing.get("discipline_ok", True)),
        "violation_tags": payload.get("violation_tags") if isinstance(payload.get("violation_tags"), list) else existing.get("violation_tags", []),
        "execution_note": str(payload.get("execution_note") if payload.get("execution_note") is not None else existing.get("execution_note", "")).strip(),
        "t1_review": str(payload.get("t1_review") if payload.get("t1_review") is not None else existing.get("t1_review", "")).strip(),
        "t3_review": str(payload.get("t3_review") if payload.get("t3_review") is not None else existing.get("t3_review", "")).strip(),
        "t5_review": str(payload.get("t5_review") if payload.get("t5_review") is not None else existing.get("t5_review", "")).strip(),
        "updated_at": now_text,
        "created_at": existing.get("created_at") or now_text,
    }
    if existing_index >= 0:
        records[existing_index] = record
    else:
        records.append(record)
    _save_gen2_daily_execution_ledger(records)
    return {"ok": True, "record": _sanitize(record), "ledger_path": str(GEN2_DAILY_EXECUTION_LEDGER_PATH)}


def _build_gen2_daily_trade_ticket(
    signal_date: Optional[str],
    limit: int = 30,
    formal_limit: int = 3,
    persist: bool = True,
) -> Dict[str, Any]:
    safe_limit = max(10, min(int(limit or 30), 100))
    safe_formal_limit = max(1, min(int(formal_limit or 3), 5))
    pool = _build_gen2_selection_pool(signal_date, safe_limit)
    selected_date = _normalize_date_str(pool.get("signal_date")) or _normalize_date_str(signal_date) or ""
    open_state = _load_gen2_open_state_for_date(selected_date)
    market_state = str(open_state.get("state") or "UNKNOWN").upper()
    policy = _gen2_ticket_position_policy(market_state)
    can_open = bool(policy.get("can_open"))
    source_rows = pool.get("rows") or []
    buyable_rows = [row for row in source_rows if row.get("buyable")]
    formal_rows = buyable_rows[:safe_formal_limit] if can_open else []
    formal_candidates = [
        _gen2_ticket_candidate(row, idx + 1, policy)
        for idx, row in enumerate(formal_rows)
    ]
    watch_rows = [
        row
        for row in source_rows
        if row not in formal_rows and (row.get("pass_mainline1") or row.get("pass_mainline2") or row.get("pass_breakout_stage"))
    ][: max(0, safe_limit - len(formal_rows))]
    holdings_cache = _load_ths_capital_holdings_cache()
    holding_rows = holdings_cache.get("rows") if isinstance(holdings_cache, dict) else None
    ticket = _sanitize(
        {
            "available": bool(pool.get("available")),
            "schema_version": 1,
            "mode": "shadow_only",
            "strategy_code": "g2_v2_complete",
            "strategy_name": "G2 V4 official daily trade ticket",
            "alpha191_gate": "g2_v2_complete",
            "signal_date": selected_date,
            "requested_date": _normalize_date_str(signal_date),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "market_state": market_state,
            "market_state_detail": open_state,
            "can_open": can_open,
            "target_exposure": policy.get("target_exposure"),
            "max_new_positions": policy.get("max_new_positions"),
            "single_position": policy.get("single_position"),
            "formal_candidate_count": len(formal_candidates),
            "formal_candidates": formal_candidates,
            "watch_candidates": watch_rows[:10],
            "existing_holdings": {
                "available": isinstance(holding_rows, list),
                "count": len(holding_rows) if isinstance(holding_rows, list) else 0,
                "note": "真实持仓仅作可选附加层，不作为 G2 V4 主流程 hard blocker。",
            },
            "forbidden_actions": [
                "不买非 G2 V4 正式候选",
                "不追直线拉升",
                "不补亏损票",
                "不超过交易单给出的持仓数量和仓位上限",
                "不因为盘感临时放宽市场状态门禁",
            ],
            "review_contract": {
                "t1": "记录是否按买入条件触发、是否违反纪律、次日浮盈亏",
                "t3": "判断是否按预期走强，未走强则降级",
                "t5": "归因选股、买点、市场状态、执行纪律",
            },
            "pipeline": pool.get("pipeline") or [],
            "data_freshness": pool.get("data_freshness"),
            "branch_freshness": pool.get("branch_freshness"),
            "source_summary": {
                "row_count": pool.get("row_count"),
                "trigger_count": pool.get("trigger_count"),
                "complete_count": pool.get("complete_count"),
                "quality_pass_count": pool.get("quality_pass_count"),
                "source_latest_date": pool.get("source_latest_date"),
            },
            "message": (
                "G2 V4 今日允许开仓，正式候选已生成。"
                if can_open and formal_candidates
                else "今日无正式开仓票；只允许管理已有持仓和观察 G2 V4 候选。"
            ),
        }
    )
    if persist:
        ticket["outputs"] = _save_gen2_daily_trade_ticket(ticket)
    ticket["execution_record"] = _find_gen2_daily_execution_record(
        str(ticket.get("signal_date") or ""),
        str(ticket.get("strategy_code") or "g2_v2_complete"),
    )
    return _sanitize(ticket)


def _build_gen2_live_selection_summary(signal_date: Optional[str]) -> Dict[str, Any]:
    requested_date = _normalize_date_str(signal_date)
    live_dates = _gen2_live_update_dates()
    official_dates = _load_gen2_v2_complete_dates()
    ledger_dates: List[str] = []
    try:
        ledger_df = _load_gen2_risk_cool_shadow_ledger()
        if not ledger_df.empty and "entry_date" in ledger_df.columns:
            ledger_dates = sorted([str(x) for x in ledger_df["entry_date"].dropna().unique() if str(x)])
    except Exception as exc:
        logger.warning(f"build gen2 live selection summary failed to load ledger dates: {exc}")
        ledger_dates = []

    selectable_dates = sorted(set(live_dates + ledger_dates + official_dates))
    if not selectable_dates:
        return {
            "summary": {
                "strategy_name": "G2 second-generation strategy",
                "candidate_count": 0,
            },
            "branch_freshness": {},
            "live_trade_mode": "latest_driven_mainline",
            "official_branch_blocks_live": False,
            "signal_date": "",
            "requested_date": requested_date or "",
            "source_latest_date": "",
        }

    selected_date = selectable_dates[-1]
    if requested_date:
        earlier_dates = [x for x in selectable_dates if x <= requested_date]
        selected_date = earlier_dates[-1] if earlier_dates else selectable_dates[-1]

    branch_freshness = _build_gen2_official_branch_freshness(requested_date, selected_date, official_dates)
    summary = _load_gen2_live_update_summary(selected_date)
    candidate_count = _to_int(summary.get("filtered_signals"), default=0)
    if candidate_count <= 0:
        candidate_count = _to_int(summary.get("raw_candidates"), default=0)
    if candidate_count <= 0:
        candidate_count = _to_int(summary.get("shadow_rows_for_date"), default=0)

    return _sanitize(
        {
            "summary": {
                "strategy_name": "G2 second-generation strategy",
                "candidate_count": int(candidate_count),
            },
            "branch_freshness": branch_freshness,
            "live_trade_mode": "latest_driven_mainline",
            "official_branch_blocks_live": False,
            "signal_date": selected_date,
            "requested_date": requested_date or "",
            "source_latest_date": selectable_dates[-1],
        }
    )


def _build_gen2_selection_pool_error_payload(
    signal_date: Optional[str],
    limit: int,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    del limit
    requested_date = _normalize_date_str(signal_date)
    live_dates = []
    try:
        live_dates = _gen2_live_update_dates()
    except Exception:
        live_dates = []
    official_dates = []
    try:
        official_dates = _load_gen2_v2_complete_dates()
    except Exception:
        official_dates = []
    selectable_dates = sorted(set(live_dates + official_dates))
    latest_date = selectable_dates[-1] if selectable_dates else ""
    selected_date = requested_date if requested_date and requested_date in selectable_dates else latest_date
    branch_freshness = _build_gen2_official_branch_freshness(requested_date, selected_date, official_dates)
    message = "G2 selection pool unavailable"
    if error:
        message = f"{message}: {error}"
    return _sanitize(
        {
            "available": False,
            "rows": [],
            "trigger_rows": [],
            "complete_rows": [],
            "quality_rows": [],
            "summary": {"available": False, "count": 0},
            "quality_summary": {
                "available": False,
                "count": 0,
                "pass_count": 0,
                "volume5": {"available": False, "count": 0, "pass_count": 0},
                "breakout": {"available": False, "count": 0, "pass_count": 0},
            },
            "pipeline": [
                {
                    "key": "selection_pool_build",
                    "label": "G2 selection pool build",
                    "count": 0,
                    "note": error or "selection pool build failed",
                }
            ],
            "signal_date": selected_date,
            "requested_date": requested_date,
            "source_latest_date": latest_date,
            "available_dates": selectable_dates,
            "branch_freshness": branch_freshness,
            "live_trade_mode": "latest_driven_mainline",
            "official_branch_blocks_live": False,
            "strategy_code": "g2_alpha191_volume5_keep80_runup",
            "strategy_name": "G2 二代策略（volume5 + 过滤 + breakout）",
            "alpha191_gate": "g2_v2_complete",
            "strategy_mode": "g2_v2_complete",
            "row_count": 0,
            "visible_row_count": 0,
            "trigger_count": 0,
            "complete_count": 0,
            "quality_count": 0,
            "quality_pass_count": 0,
            "volume5_quality_count": 0,
            "volume5_quality_pass_count": 0,
            "breakout_quality_count": 0,
            "breakout_quality_stage3_count": 0,
            "breakout_quality_stage4_count": 0,
            "v4_d1_rows": [],
            "v4_d_rows": [],
            "data_freshness": build_gen2_data_freshness(requested_date, selected_date),
            "message": message,
        }
    )


def _pct_value(value: Any) -> Optional[float]:
    val = _to_float(value)
    if val is None:
        return None
    return val * 100


def _gen2_shadow_status_label(status: str) -> str:
    if status == "executed":
        return "执行成功"
    if status == "suspended_by_two_stop_cd3":
        return "风控拦截"
    if status == "suspended_by_stop_cd5":
        return "风控拦截"
    if status == "observable":
        return "等待观察"
    return status or "未知"


def _text_arg(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        module_name = str(value.__class__.__module__ or "")
    except Exception:
        module_name = ""
    if module_name.startswith("fastapi"):
        value = getattr(value, "default", default)
        if value is None:
            return default
    text_val = str(value).strip()
    return text_val if text_val else default


def _resolve_live_hard_dd() -> float:
    raw = str(os.getenv(LIVE_HARD_DD_ENV_KEY, "") or "").strip()
    if not raw:
        return float(DEFAULT_LIVE_HARD_DD)
    parsed = _to_float(raw)
    if parsed is None:
        return float(DEFAULT_LIVE_HARD_DD)
    return float(parsed)


def _resolve_live_positive_int_env(env_key: str, default_value: int, min_value: int = 0) -> int:
    raw = str(os.getenv(env_key, "") or "").strip()
    if not raw:
        return max(min_value, int(default_value))
    parsed = _to_int(raw, default_value)
    return max(min_value, int(parsed))


def _apply_live_runtime_params(params: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(params or {})
    merged["hard_dd"] = float(_resolve_live_hard_dd())
    merged["freeze_days"] = _resolve_live_positive_int_env(
        LIVE_FREEZE_DAYS_ENV_KEY,
        default_value=DEFAULT_LIVE_FREEZE_DAYS,
        min_value=1,
    )
    merged["recovery_days"] = _resolve_live_positive_int_env(
        LIVE_RECOVERY_DAYS_ENV_KEY,
        default_value=_to_int(merged.get("recovery_days"), DEFAULT_LIVE_RECOVERY_DAYS),
        min_value=0,
    )
    merged["recovery_max_holdings"] = _resolve_live_positive_int_env(
        LIVE_RECOVERY_MAX_HOLDINGS_ENV_KEY,
        default_value=_to_int(merged.get("recovery_max_holdings"), DEFAULT_LIVE_RECOVERY_MAX_HOLDINGS),
        min_value=1,
    )
    return merged


def _summary_with_runtime_params(summary: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(summary, dict):
        return summary
    payload = dict(summary)
    assumptions = dict(payload.get("assumptions") or {})
    current_params = dict(assumptions.get("params") or {})
    current_params.update(dict(params or {}))
    assumptions["params"] = current_params
    payload["assumptions"] = assumptions
    return payload


def _ensure_artifact_table() -> None:
    global _ARTIFACT_TABLE_READY
    if _ARTIFACT_TABLE_READY:
        return
    sql = f"""
    CREATE TABLE IF NOT EXISTS {TRADING_ARTIFACT_TABLE} (
      strategy_version String NOT NULL,
      artifact_key String NOT NULL,
      payload_json String,
      updated_at DateTime DEFAULT now(),
      PRIMARY KEY (strategy_version, artifact_key)
    ) ENGINE = MergeTree()
    ORDER BY (strategy_version, artifact_key)
    """
    with db.engine.begin() as conn:
        conn.execute(text(sql))
    _ARTIFACT_TABLE_READY = True


def _ensure_selection_score_table() -> None:
    global _SELECTION_SCORE_TABLE_READY
    if _SELECTION_SCORE_TABLE_READY:
        return
    sql = f"""
    CREATE TABLE IF NOT EXISTS {TRADING_SELECTION_SCORE_TABLE} (
      strategy_version String NOT NULL,
      signal_date Date NOT NULL,
      code String NOT NULL,
      name String,
      score_rank Int32 NOT NULL DEFAULT 0,
      score_total Float64,
      score_pool_size Int32 NOT NULL DEFAULT 0,
      passed_entry_min_score UInt8 NOT NULL DEFAULT 0,
      mom5 Float64,
      mom10 Float64,
      vol_ratio Float64,
      amt20 Float64,
      vol10 Float64,
      ma10 Float64,
      close Float64,
      close_gt_ma10 UInt8 NOT NULL DEFAULT 0,
      r_mom5 Float64,
      r_mom10 Float64,
      r_vol_ratio Float64,
      r_amt20 Float64,
      r_vol10_low Float64,
      base_filter_json String,
      scoring_operator_json String,
      created_at DateTime DEFAULT now(),
      updated_at DateTime DEFAULT now()
    ) ENGINE = MergeTree()
    ORDER BY (strategy_version, signal_date, code)
    """
    with db.engine.begin() as conn:
        conn.execute(text(sql))
    _SELECTION_SCORE_TABLE_READY = True


def _upsert_strategy_artifact(strategy_version: str, artifact_key: str, payload: Any) -> None:
    _ensure_artifact_table()
    payload_json = json.dumps(payload, ensure_ascii=False, default=_json_default)
    strategy_key = str(strategy_version or "").strip()
    artifact_name = str(artifact_key or "").strip()
    with db.engine.begin() as conn:
        conn.execute(
            text(
                f"""
                DELETE FROM {TRADING_ARTIFACT_TABLE}
                WHERE strategy_version = :strategy_version
                  AND artifact_key = :artifact_key
                """
            ),
            {"strategy_version": strategy_key, "artifact_key": artifact_name},
        )
        conn.execute(
            text(
                f"""
                INSERT INTO {TRADING_ARTIFACT_TABLE}(strategy_version, artifact_key, payload_json, updated_at)
                VALUES(:strategy_version, :artifact_key, :payload_json, now())
                """
            ),
            {
                "strategy_version": strategy_key,
                "artifact_key": artifact_name,
                "payload_json": payload_json,
            },
        )


def _normalize_date_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text_val = str(value).strip()
    if not text_val:
        return None
    try:
        return str(pd.Timestamp(text_val).date())
    except Exception:
        return text_val


def _upsert_daily_artifact_row(
    strategy_version: str,
    artifact_key: str,
    date_key: str,
    row: Dict[str, Any],
) -> None:
    payloads = _load_strategy_artifacts(strategy_version=str(strategy_version or "").strip())
    current_rows = payloads.get(artifact_key)
    items: List[Dict[str, Any]] = []
    if isinstance(current_rows, list):
        for x in current_rows:
            if isinstance(x, dict):
                item = _sanitize(dict(x))
                normalized = _normalize_date_str(item.get(date_key))
                if normalized:
                    item[date_key] = normalized
                items.append(item)

    new_row = _sanitize(dict(row))
    normalized_new_date = _normalize_date_str(new_row.get(date_key))
    if normalized_new_date:
        new_row[date_key] = normalized_new_date

    replaced = False
    for i, item in enumerate(items):
        if str(item.get(date_key) or "") == str(new_row.get(date_key) or ""):
            items[i] = new_row
            replaced = True
            break
    if not replaced:
        items.append(new_row)

    items = sorted(items, key=lambda x: str(x.get(date_key) or ""))
    _upsert_strategy_artifact(
        strategy_version=str(strategy_version or "").strip(),
        artifact_key=artifact_key,
        payload=items,
    )


def _rows_to_df(rows: Any) -> pd.DataFrame:
    if not isinstance(rows, list) or not rows:
        return pd.DataFrame()
    try:
        df = pd.DataFrame(rows)
        for c in ("code",):
            if c in df.columns:
                df[c] = df[c].astype(str)
        return df
    except Exception as exc:
        logger.warning(f"rows->df failed: {exc}")
        return pd.DataFrame()


def _load_strategy_artifacts(strategy_version: str) -> Dict[str, Any]:
    _ensure_artifact_table()
    with db.engine.connect() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT artifact_key, payload_json, updated_at
                FROM {TRADING_ARTIFACT_TABLE}
                WHERE strategy_version = :strategy_version
                """
            ),
            {"strategy_version": strategy_version},
        ).fetchall()
    if not rows:
        return {}

    payloads: Dict[str, Any] = {}
    latest_updated = None
    for row in rows:
        key = str(row[0])
        payload_raw = row[1]
        updated_at = row[2]
        if latest_updated is None or (updated_at is not None and updated_at > latest_updated):
            latest_updated = updated_at
        try:
            payloads[key] = json.loads(payload_raw) if payload_raw else None
        except Exception as exc:
            logger.warning(f"artifact json decode failed: version={strategy_version}, key={key}, error={exc}")
            payloads[key] = None

    payloads["__meta_updated_at"] = str(latest_updated) if latest_updated is not None else None
    return payloads


def _decode_json_field(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _load_selection_score_rows(
    strategy_version: str,
    signal_date: str,
    limit: int = 30,
) -> List[Dict[str, Any]]:
    _ensure_selection_score_table()
    with db.engine.connect() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT
                  strategy_version, signal_date, code, name,
                  score_rank, score_total, score_pool_size, passed_entry_min_score,
                  mom5, mom10, vol_ratio, amt20, vol10, ma10, close, close_gt_ma10,
                  r_mom5, r_mom10, r_vol_ratio, r_amt20, r_vol10_low,
                  base_filter_json, scoring_operator_json, created_at, updated_at
                FROM {TRADING_SELECTION_SCORE_TABLE}
                WHERE strategy_version = :strategy_version
                  AND signal_date = :signal_date
                ORDER BY score_rank ASC
                LIMIT :limit_num
                """
            ),
            {
                "strategy_version": strategy_version,
                "signal_date": pipeline_signal_date,
                "limit_num": int(max(1, limit)),
            },
        ).mappings().all()

    data: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["base_filter_json"] = _decode_json_field(item.get("base_filter_json"))
        item["scoring_operator_json"] = _decode_json_field(item.get("scoring_operator_json"))
        data.append(_sanitize(item))
    return data


def _resolve_latest_stock_trade_date() -> Optional[str]:
    sql = text(
        """
        SELECT MAX(k.trade_date) AS latest_date
        FROM kline_daily k
        JOIN stocks s ON s.code = k.code
        WHERE s.type = 'stock'
          AND s.quit = 0
          AND (s.st = 0 OR s.st IS NULL)
        """
    )
    with db.engine.connect() as conn:
        latest = conn.execute(sql).scalar()
    if latest is None:
        return None
    try:
        return str(pd.Timestamp(latest).date())
    except Exception:
        return str(latest)


def _is_trading_day(date_text: Optional[str]) -> bool:
    normalized = _normalize_date_str(date_text)
    if not normalized:
        return False
    sql = text(
        """
        SELECT is_trading
        FROM trade_calendar
        WHERE market = 'SH'
          AND trade_date = :trade_date
        LIMIT 1
        """
    )
    try:
        with db.engine.connect() as conn:
            value = conn.execute(sql, {"trade_date": normalized}).scalar()
    except Exception as exc:
        logger.warning(f"load trade_calendar failed: trade_date={normalized}, error={exc}")
        value = None
    if value is not None:
        return bool(value)
    try:
        ts = pd.Timestamp(normalized)
        return ts.weekday() < 5
    except Exception:
        return False


def _today_trade_context_date() -> Optional[str]:
    today_text = str(pd.Timestamp(datetime.now()).date())
    return today_text if _is_trading_day(today_text) else None


def _build_live_fallback_info(
    requested_date: Optional[str],
    selected_date: Optional[str],
    latest_trade_date: Optional[str],
    latest_snapshot_date: Optional[str],
) -> Dict[str, Any]:
    requested = _normalize_date_str(requested_date)
    selected = _normalize_date_str(selected_date)
    latest_trade = _normalize_date_str(latest_trade_date)
    latest_snapshot = _normalize_date_str(latest_snapshot_date)
    requested_is_trading_day = _is_trading_day(requested) if requested else False
    rolled_back = bool(requested and selected and requested != selected)
    info: Dict[str, Any] = {
        "active": False,
        "requested_date": requested,
        "requested_is_trading_day": requested_is_trading_day,
        "selected_date": selected,
        "latest_trade_date": latest_trade,
        "latest_snapshot_date": latest_snapshot,
        "show_repair_entry": False,
        "repair_target_date": None,
        "repair_reason": "",
        "title": "",
        "message": "",
    }
    if not rolled_back:
        return info

    info["active"] = True
    info["title"] = f"{requested} 请求已回退，改用 V5 回看窗口"
    if requested_is_trading_day and latest_trade and requested > latest_trade:
        info["message"] = (
            f"{requested} 为交易日请求，但最新交易日仍为 {latest_trade}。"
            f" 已按 V5 交易时段14:35-14:50 的回看规则，回退到 {selected}。"
        )
        if latest_snapshot != latest_trade:
            info["show_repair_entry"] = True
            info["repair_target_date"] = latest_trade
            info["repair_reason"] = f"{latest_trade} 的快照与 V5 回看窗口未对齐，建议先执行修复。"
        return info

    if requested_is_trading_day:
        info["message"] = f"{requested} 为交易日请求，当前仅有 {selected} 的完整可选数据。"
        if latest_trade and latest_snapshot != latest_trade:
            info["show_repair_entry"] = True
            info["repair_target_date"] = latest_trade
            info["repair_reason"] = f"{latest_trade} 快照与 V5 回看窗口未对齐，需要补齐数据。"
        elif latest_trade and requested <= latest_trade:
            info["show_repair_entry"] = True
            info["repair_target_date"] = requested
            info["repair_reason"] = f"{requested} 交易日与当前 V5 回看状态不一致，请补齐该日数据。"
        return info

    info["message"] = f"{requested} 非交易日请求，已回退到 {selected}。"
    return info

def _load_v4_data(strategy_version: str) -> Dict[str, Any]:
    payloads = _load_strategy_artifacts(strategy_version=strategy_version)
    if not payloads:
        return {"available": False, "strategy_version": strategy_version}

    meta = payloads.get("meta") or {}
    summary = payloads.get("summary") or {}
    consistency = payloads.get("consistency") or {}
    decisions_df = _rows_to_df(payloads.get("decisions"))
    holdings_df = _rows_to_df(payloads.get("holdings"))
    trades_df = _rows_to_df(payloads.get("trades"))
    curve_df = _rows_to_df(payloads.get("curve"))

    output_dir = meta.get("output_dir")
    if not output_dir:
        output_dir = DEFAULT_V42_OUTPUT_DIR if str(strategy_version or "").lower().startswith("v4.2") else DEFAULT_V4_OUTPUT_DIR

    has_core = isinstance(summary, dict) and len(summary) > 0 and not curve_df.empty and not trades_df.empty
    return {
        "available": bool(has_core),
        "strategy_version": strategy_version,
        "output_dir": output_dir,
        "summary": summary,
        "consistency": consistency,
        "decisions_df": decisions_df,
        "holdings_df": holdings_df,
        "trades_df": trades_df,
        "curve_df": curve_df,
        "meta": meta,
        "updated_at": payloads.get("__meta_updated_at"),
    }


def _load_hist_with_factors(signal_date: str, lookback_days: int = 130) -> pd.DataFrame:
    if not signal_date:
        return pd.DataFrame()

    try:
        signal_ts = pd.Timestamp(signal_date).normalize()
    except Exception:
        return pd.DataFrame()

    cache_key = str(signal_ts.date())
    cached = _SIGNAL_DAY_CACHE.get(cache_key)
    if isinstance(cached, dict) and isinstance(cached.get("hist"), pd.DataFrame):
        return cached["hist"]

    start_ts = (signal_ts - pd.Timedelta(days=lookback_days)).date()
    end_ts = signal_ts.date()
    if clickhouse_available():
        hist = clickhouse_query_df(
            """
            SELECT
              substr(k.code, 1, 6) AS code,
              s.name AS name,
              k.trade_date AS date,
              k.close AS close,
              k.amount AS amount
            FROM kline_daily k
            JOIN stocks s ON s.code = k.code
            WHERE k.trade_date >= ?::DATE
              AND k.trade_date <= ?::DATE
              AND s.type = 'stock'
              AND s.quit = 0
              AND (s.st = 0 OR s.st IS NULL)
            ORDER BY k.code, k.trade_date
            """,
            [str(start_ts), str(end_ts)],
        )
    else:
        sql = text(
            f"""
            SELECT
              substr(k.code, 1, 6) AS code,
              s.name AS name,
              k.trade_date AS date,
              k.close AS close,
              k.amount AS amount
            FROM kline_daily k
            JOIN stocks s ON s.code = k.code
            WHERE k.trade_date >= :start_date
              AND k.trade_date <= :end_date
              AND s.type = 'stock'
              AND s.quit = 0
              AND (s.st = 0 OR s.st IS NULL)
            ORDER BY k.code, k.trade_date
            """
        )
        params = {"start_date": start_ts, "end_date": end_ts}
        hist = pd.read_sql(sql, db.engine, params=params)
    if hist.empty:
        return hist

    hist["date"] = pd.to_datetime(hist["date"], errors="coerce")
    hist["code"] = hist["code"].astype(str).str[:6]
    for col in ["close", "amount"]:
        if col in hist.columns:
            hist[col] = pd.to_numeric(hist[col], errors="coerce")
    hist = hist.dropna(subset=["date", "code", "close", "amount"]).copy()
    if hist.empty:
        return hist

    # MySQL amount currently是万元口径，统一换算为元”口径?    hist["amount"] = hist["amount"] * 10000.0
    hist = hist.sort_values(["code", "date"]).reset_index(drop=True)
    g = hist.groupby("code", group_keys=False)
    hist["ret1"] = g["close"].pct_change()
    hist["mom5"] = g["close"].pct_change(5)
    hist["mom10"] = g["close"].pct_change(10)
    hist["ma10"] = g["close"].rolling(10).mean().reset_index(level=0, drop=True)
    hist["amt20"] = g["amount"].rolling(20).mean().reset_index(level=0, drop=True)
    hist["amt5_prev"] = g["amount"].shift(1).rolling(5).mean().reset_index(level=0, drop=True)
    hist["vol_ratio"] = hist["amount"] / hist["amt5_prev"]
    hist["vol10"] = g["ret1"].rolling(10).std().reset_index(level=0, drop=True)
    hist["close_gt_ma10"] = hist["close"] > hist["ma10"]
    _SIGNAL_DAY_CACHE[cache_key] = {"hist": hist}
    return hist


def _score_candidates_on_signal_day(day_df: pd.DataFrame) -> pd.DataFrame:
    if day_df.empty:
        return pd.DataFrame()
    d = day_df.copy().replace([np.inf, -np.inf], np.nan)
    d = d.dropna(subset=["code", "mom5", "mom10", "vol_ratio", "amt20", "vol10", "ma10", "close"])
    d = d[(d["mom5"] > 0) & (d["close_gt_ma10"]) & (d["amt20"] >= SCORE_MIN_TURNOVER20)]
    d = d[(d["vol10"] >= SCORE_MIN_VOL10) & (d["vol10"] <= SCORE_MAX_VOL10)]
    if d.empty:
        return pd.DataFrame()

    for c in ["mom5", "mom10", "vol_ratio", "amt20"]:
        d[f"r_{c}"] = d[c].rank(pct=True)
    d["r_vol10_low"] = 1 - d["vol10"].rank(pct=True)
    d["score"] = (
        0.35 * d["r_mom5"]
        + 0.20 * d["r_mom10"]
        + 0.20 * d["r_vol_ratio"]
        + 0.20 * d["r_amt20"]
        + 0.05 * d["r_vol10_low"]
    )
    d = d.sort_values("score", ascending=False).reset_index(drop=True)
    d["score_rank"] = np.arange(1, len(d) + 1)
    return d


def _calc_signal_breadth(day_df: pd.DataFrame) -> Optional[float]:
    if day_df is None or day_df.empty:
        return None
    d = day_df.copy()
    if "close" not in d.columns or "ma10" not in d.columns:
        return None
    d = d.dropna(subset=["close", "ma10"])
    if d.empty:
        return None
    if "amt20" in d.columns:
        d = d[d["amt20"].fillna(0) >= SCORE_MIN_TURNOVER20]
    if d.empty:
        return None
    return float((d["close"] > d["ma10"]).mean())


def _resolve_mode_for_snapshot(
    breadth: Optional[float],
    signal_drawdown: Optional[float],
    params: Dict[str, Any],
) -> str:
    soft_dd = _to_float(params.get("soft_dd"))
    hard_dd = _to_float(params.get("hard_dd"))
    breadth_low = _to_float(params.get("breadth_low"))
    breadth_high = _to_float(params.get("breadth_high"))

    if signal_drawdown is not None and hard_dd is not None and signal_drawdown <= hard_dd:
        return "off"

    mode = "neutral"
    if breadth is not None:
        if breadth_high is not None and breadth >= breadth_high:
            mode = "on"
        elif breadth_low is not None and breadth <= breadth_low:
            mode = "off"
        else:
            mode = "neutral"

    if signal_drawdown is not None and soft_dd is not None and signal_drawdown <= soft_dd and mode == "on":
        mode = "neutral"
    return mode


def _last_row_on_or_before(df: pd.DataFrame, date_col: str, target_date: str) -> Dict[str, Any]:
    if df is None or df.empty or date_col not in df.columns:
        return {}
    d = df.copy()
    d[date_col] = d[date_col].astype(str)
    d = d[d[date_col] <= str(target_date)]
    if d.empty:
        return {}
    d = d.sort_values(date_col)
    return _sanitize(d.iloc[-1].to_dict())


def _estimate_signal_drawdown_for_snapshot(
    holdings_df: pd.DataFrame,
    run_date: str,
    equity_today: Optional[float],
    fallback_dd: Optional[float],
) -> float:
    eq_series: List[float] = []
    if holdings_df is not None and not holdings_df.empty and "date" in holdings_df.columns and "equity" in holdings_df.columns:
        d = holdings_df.copy()
        d["date"] = d["date"].astype(str)
        d = d[d["date"] <= str(run_date)]
        if not d.empty:
            d = d.sort_values("date")
            for x in d["equity"].tolist():
                fx = _to_float(x)
                if fx is not None and fx > 0:
                    eq_series.append(float(fx))
    if equity_today is not None and equity_today > 0:
        eq_series.append(float(equity_today))
    if not eq_series:
        return float(fallback_dd if fallback_dd is not None else 0.0)
    arr = np.array(eq_series, dtype=float)
    peak = np.maximum.accumulate(arr)
    last = float(arr[-1])
    last_peak = float(peak[-1]) if len(peak) else last
    if last_peak <= 0:
        return float(fallback_dd if fallback_dd is not None else 0.0)
    return float(last / last_peak - 1.0)


def _build_realtime_snapshot_rows(
    strategy_version: str,
    run_date: str,
    summary: Dict[str, Any],
    decisions_df: pd.DataFrame,
    holdings_df: pd.DataFrame,
    day_df: pd.DataFrame,
    scored_df: pd.DataFrame,
) -> Dict[str, Dict[str, Any]]:
    raw_params = ((summary.get("assumptions") or {}).get("params") or {}) if isinstance(summary, dict) else {}
    params = _apply_live_runtime_params(raw_params)
    entry_min_score = _to_float(params.get("entry_min_score")) or DEFAULT_ENTRY_MIN_SCORE
    keep_rank_mult = max(1, _to_int(params.get("keep_rank_mult"), DEFAULT_KEEP_RANK_MULT))

    prev_holding = _last_row_on_or_before(holdings_df, "date", run_date)
    prev_signal_dd = _to_float(prev_holding.get("signal_drawdown"))
    prev_cash = _to_float(prev_holding.get("cash")) or 0.0
    prev_freeze_left = max(0, _to_int(prev_holding.get("freeze_left"), 0))
    prev_recovery_left = max(0, _to_int(prev_holding.get("recovery_left"), 0))
    prev_holding_date = str(prev_holding.get("date") or "").strip()
    day_advanced = bool(prev_holding_date) and prev_holding_date < str(run_date)
    prev_signal_dd_for_cross = prev_signal_dd
    if decisions_df is not None and not decisions_df.empty and "trade_date" in decisions_df.columns:
        prev_decisions = decisions_df.copy()
        prev_decisions["trade_date"] = prev_decisions["trade_date"].astype(str)
        prev_decisions = prev_decisions[prev_decisions["trade_date"] < str(run_date)].sort_values("trade_date")
        if not prev_decisions.empty and "signal_drawdown" in prev_decisions.columns:
            prev_signal_dd_for_cross = _to_float(prev_decisions.iloc[-1].get("signal_drawdown"))

    holding_rows = _parse_holdings_desc(prev_holding.get("holdings"))
    day_raw_map: Dict[str, pd.Series] = {}
    if day_df is not None and not day_df.empty and "code" in day_df.columns:
        for _, raw in day_df.iterrows():
            day_raw_map[str(raw.get("code") or "")] = raw

    refreshed_holdings_desc_items: List[str] = []
    refreshed_holdings_codes: List[str] = []
    market_value = 0.0
    for h in holding_rows:
        code = str(h.get("code") or "").strip()
        shares = _to_int(h.get("shares"), 0)
        old_px = _to_float(h.get("current_price"))
        if not code or shares <= 0:
            continue
        raw = day_raw_map.get(code)
        px = _to_float(raw.get("close")) if raw is not None else old_px
        if px is None or px <= 0:
            px = old_px if old_px is not None and old_px > 0 else None
        if px is None:
            continue
        market_value += float(px) * shares
        refreshed_holdings_codes.append(code)
        refreshed_holdings_desc_items.append(f"{code}:{shares}@{float(px):.2f}")

    equity = float(prev_cash) + float(market_value)
    position_ratio = float(market_value / equity) if equity > 0 else 0.0

    breadth = _calc_signal_breadth(day_df)
    signal_drawdown = _estimate_signal_drawdown_for_snapshot(
        holdings_df=holdings_df,
        run_date=run_date,
        equity_today=equity,
        fallback_dd=prev_signal_dd,
    )
    base_mode = _resolve_mode_for_snapshot(
        breadth=breadth,
        signal_drawdown=signal_drawdown,
        params=params,
    )
    hard_dd = _to_float(params.get("hard_dd"))
    hard_breach = bool(signal_drawdown <= hard_dd) if hard_dd is not None else False
    freeze_days = max(1, _to_int(params.get("freeze_days"), DEFAULT_LIVE_FREEZE_DAYS))
    recovery_days = max(0, _to_int(params.get("recovery_days"), DEFAULT_LIVE_RECOVERY_DAYS))
    recovery_max_holdings = max(1, _to_int(params.get("recovery_max_holdings"), DEFAULT_LIVE_RECOVERY_MAX_HOLDINGS))
    hard_cross = bool(hard_breach and (prev_signal_dd_for_cross is None or prev_signal_dd_for_cross > hard_dd))

    if hard_cross:
        freeze_left = int(freeze_days)
        recovery_left = 0
    elif prev_freeze_left > 0:
        freeze_left = max(0, prev_freeze_left - (1 if day_advanced else 0))
        if prev_freeze_left > 0 and freeze_left == 0 and day_advanced and not hard_breach and recovery_days > 0:
            recovery_left = int(max(prev_recovery_left, recovery_days))
        else:
            recovery_left = int(prev_recovery_left)
    else:
        freeze_left = 0
        recovery_left = max(0, prev_recovery_left - (1 if day_advanced else 0))
        if hard_breach:
            recovery_left = 0

    recovery_active = bool(freeze_left == 0 and recovery_left > 0 and not hard_breach)
    mode = str(base_mode)
    mode_recovery_override = 0
    if recovery_active and mode == "off":
        mode = "neutral"
        mode_recovery_override = 1

    max_holdings = MODE_MAX_HOLDINGS.get(mode, MODE_MAX_HOLDINGS["neutral"])
    if recovery_active:
        max_holdings = max(1, min(int(max_holdings), int(recovery_max_holdings)))
    keep_top_n = int(max_holdings) * int(keep_rank_mult)

    target_pool: List[str] = []
    buy_candidates: List[str] = []
    if scored_df is not None and not scored_df.empty and "code" in scored_df.columns:
        ranked = scored_df.sort_values("score_rank").reset_index(drop=True)
        target_pool = [str(x) for x in ranked.head(max(1, keep_top_n))["code"].tolist()]
        held_in_target = len([c for c in refreshed_holdings_codes if c in target_pool])
        buy_slots = max(0, int(max_holdings) - held_in_target)
        if buy_slots > 0:
            for _, r in ranked.iterrows():
                code = str(r.get("code") or "").strip()
                if not code or code not in target_pool or code in refreshed_holdings_codes:
                    continue
                score_total = _to_float(r.get("score"))
                if score_total is None or score_total < float(entry_min_score):
                    continue
                buy_candidates.append(code)
                if len(buy_candidates) >= buy_slots:
                    break

    sell_plan_codes = [c for c in refreshed_holdings_codes if c not in target_pool]

    mode_reason = "保持跟踪，进入防守回撤模式"
    breadth_low = _to_float(params.get("breadth_low"))
    breadth_high = _to_float(params.get("breadth_high"))
    soft_dd = _to_float(params.get("soft_dd"))
    if hard_breach and hard_dd is not None:
        mode_reason = f"指标触发：signal_drawdown={signal_drawdown:.4f}，hard_dd={hard_dd:.4f}"
    elif breadth is None:
        mode_reason = "市场宽度缺失，无法判断，维持默认防守"
    elif breadth_high is not None and breadth >= breadth_high:
        mode_reason = f"市场宽度{breadth:.4f} >= breadth_high={breadth_high:.4f}"
    elif breadth_low is not None and breadth <= breadth_low:
        mode_reason = f"市场宽度{breadth:.4f} <= breadth_low={breadth_low:.4f}"
    else:
        mode_reason = "市场宽度处于安全区间"
    if soft_dd is not None and signal_drawdown is not None and signal_drawdown <= soft_dd and base_mode == "neutral":
        mode_reason = f"回撤已接近 soft_dd={soft_dd:.4f}，建议从防守模式恢复。"
    if mode_recovery_override:
        mode_reason = f"恢复中，剩余恢复窗口 {recovery_left} 分钟，继续观察并延迟动作。"

    decision_row = {
        "signal_date": run_date,
        "trade_date": run_date,
        "mode": mode,
        "base_mode": base_mode,
        "mode_reason": mode_reason,
        "hard_breach": 1 if hard_breach else 0,
        "hard_cross": 1 if hard_cross else 0,
        "recovery_active": 1 if recovery_active else 0,
        "recovery_left": int(recovery_left),
        "recovery_max_holdings": int(recovery_max_holdings),
        "mode_recovery_override": int(mode_recovery_override),
        "breadth": breadth,
        "signal_drawdown": signal_drawdown,
        "target_codes": "|".join(target_pool),
        "sell_plan_codes": "|".join(sell_plan_codes),
        "sold_today": "",
        "buy_candidates": "|".join(buy_candidates),
        "source": "realtime_selection_snapshot",
        "source_note": "由实时股接口生成",
    }

    holding_row = {
        "date": run_date,
        "cash": round(float(prev_cash), 2),
        "market_value": round(float(market_value), 2),
        "equity": round(float(equity), 2),
        "position_ratio": position_ratio,
        "holdings": "|".join(refreshed_holdings_desc_items),
        "freeze_left": int(freeze_left),
        "recovery_left": int(recovery_left),
        "breadth": breadth,
        "signal_drawdown": signal_drawdown,
        "source": "realtime_selection_snapshot",
        "source_note": "由实时股接口生成",
    }

    return {"decision_row": _sanitize(decision_row), "holding_row": _sanitize(holding_row)}


def _build_score_fail_reasons(raw_row: Optional[pd.Series]) -> str:
    if raw_row is None or (isinstance(raw_row, pd.Series) and raw_row.empty):
        return "无有效数据"

    reasons: List[str] = []
    mom5 = _to_float(raw_row.get("mom5"))
    mom10 = _to_float(raw_row.get("mom10"))
    ma10 = _to_float(raw_row.get("ma10"))
    close = _to_float(raw_row.get("close"))
    amt20 = _to_float(raw_row.get("amt20"))
    vol10 = _to_float(raw_row.get("vol10"))
    vol_ratio = _to_float(raw_row.get("vol_ratio"))

    if mom5 is None:
        reasons.append("mom5缺失")
    elif mom5 <= 0:
        reasons.append("mom5<=0")
    if mom10 is None:
        reasons.append("mom10缺失")
    if close is None or ma10 is None:
        reasons.append("close/MA10缺失")
    elif close <= ma10:
        reasons.append("close<=MA10")
    if amt20 is None:
        reasons.append("amt20缺失")
    elif amt20 < SCORE_MIN_TURNOVER20:
        reasons.append(f"amt20<{SCORE_MIN_TURNOVER20}")
    if vol10 is None:
        reasons.append("vol10缺失")
    elif vol10 < SCORE_MIN_VOL10 or vol10 > SCORE_MAX_VOL10:
        reasons.append(f"vol10不在范围[{SCORE_MIN_VOL10},{SCORE_MAX_VOL10}]")
    if vol_ratio is None:
        reasons.append("vol_ratio缺失")

    if not reasons:
        return "通过"
    return " | ".join(reasons)

def _persist_top_selection_scores(
    strategy_version: str,
    signal_date: str,
    day_df: pd.DataFrame,
    scored_df: pd.DataFrame,
    entry_min_score: float,
    keep_rank_mult: int,
    max_holdings: int,
    keep_top_n: int,
    top_n: int = 30,
) -> None:
    """
    Persist top-N scored candidates for each signal day, including
    factor values, rank operators, and base-filter / score-operator snapshots.
    """
    if not signal_date:
        return

    try:
        signal_day = str(pd.Timestamp(signal_date).date())
    except Exception:
        return

    _ensure_selection_score_table()

    base_filters = {
        "mom5_gt": 0.0,
        "close_gt_ma10": True,
        "amt20_min": float(SCORE_MIN_TURNOVER20),
        "vol10_min": float(SCORE_MIN_VOL10),
        "vol10_max": float(SCORE_MAX_VOL10),
    }
    scoring_operators = {
        "score_formula": "0.35*r_mom5 + 0.20*r_mom10 + 0.20*r_vol_ratio + 0.20*r_amt20 + 0.05*r_vol10_low",
        "weights": {
            "r_mom5": 0.35,
            "r_mom10": 0.20,
            "r_vol_ratio": 0.20,
            "r_amt20": 0.20,
            "r_vol10_low": 0.05,
        },
        "ranking_directions": {
            "mom5": "desc_pct_rank",
            "mom10": "desc_pct_rank",
            "vol_ratio": "desc_pct_rank",
            "amt20": "desc_pct_rank",
            "vol10": "asc_pct_rank_then_1_minus",
        },
        "entry_min_score": float(entry_min_score),
        "keep_rank_mult": int(keep_rank_mult),
        "max_holdings": int(max_holdings),
        "keep_top_n": int(keep_top_n),
        "top_n_persisted": int(top_n),
    }

    raw_map: Dict[str, pd.Series] = {}
    if not day_df.empty and "code" in day_df.columns:
        for _, raw in day_df.iterrows():
            raw_map[str(raw.get("code"))] = raw

    pool_size = int(len(scored_df))
    top_df = scored_df.head(max(1, int(top_n))) if not scored_df.empty else pd.DataFrame()
    rows: List[Dict[str, Any]] = []
    now_ts = pd.Timestamp.now().to_pydatetime()

    for _, row in top_df.iterrows():
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        raw = raw_map.get(code)

        score_total = _to_float(row.get("score"))
        payload = {
            "strategy_version": str(strategy_version or "").strip(),
            "signal_date": signal_day,
            "code": code,
            "name": str(raw.get("name")) if raw is not None and raw.get("name") is not None else None,
            "score_rank": _to_int(row.get("score_rank"), 0),
            "score_total": score_total,
            "score_pool_size": pool_size,
            "passed_entry_min_score": 1 if (score_total is not None and score_total >= float(entry_min_score)) else 0,
            "mom5": _to_float(raw.get("mom5")) if raw is not None else None,
            "mom10": _to_float(raw.get("mom10")) if raw is not None else None,
            "vol_ratio": _to_float(raw.get("vol_ratio")) if raw is not None else None,
            "amt20": _to_float(raw.get("amt20")) if raw is not None else None,
            "vol10": _to_float(raw.get("vol10")) if raw is not None else None,
            "ma10": _to_float(raw.get("ma10")) if raw is not None else None,
            "close": _to_float(raw.get("close")) if raw is not None else None,
            "close_gt_ma10": 1 if (raw is not None and bool(raw.get("close_gt_ma10"))) else 0,
            "r_mom5": _to_float(row.get("r_mom5")),
            "r_mom10": _to_float(row.get("r_mom10")),
            "r_vol_ratio": _to_float(row.get("r_vol_ratio")),
            "r_amt20": _to_float(row.get("r_amt20")),
            "r_vol10_low": _to_float(row.get("r_vol10_low")),
            "base_filter_json": json.dumps(_sanitize(base_filters), ensure_ascii=False),
            "scoring_operator_json": json.dumps(_sanitize(scoring_operators), ensure_ascii=False),
            "created_at": now_ts,
        }
        rows.append(payload)

    with db.engine.begin() as conn:
        conn.execute(
            text(
                f"""
                DELETE FROM {TRADING_SELECTION_SCORE_TABLE}
                WHERE strategy_version = :strategy_version
                  AND signal_date = :signal_date
                """
            ),
            {"strategy_version": str(strategy_version or "").strip(), "signal_date": signal_day},
        )

        if not rows:
            return

        insert_sql = text(
            f"""
            INSERT INTO {TRADING_SELECTION_SCORE_TABLE} (
              strategy_version, signal_date, code, name,
              score_rank, score_total, score_pool_size, passed_entry_min_score,
              mom5, mom10, vol_ratio, amt20, vol10, ma10, close, close_gt_ma10,
              r_mom5, r_mom10, r_vol_ratio, r_amt20, r_vol10_low,
              base_filter_json, scoring_operator_json, created_at
            ) VALUES (
              :strategy_version, :signal_date, :code, :name,
              :score_rank, :score_total, :score_pool_size, :passed_entry_min_score,
              :mom5, :mom10, :vol_ratio, :amt20, :vol10, :ma10, :close, :close_gt_ma10,
              :r_mom5, :r_mom10, :r_vol_ratio, :r_amt20, :r_vol10_low,
              :base_filter_json, :scoring_operator_json, :created_at
            """
        )
        conn.execute(insert_sql, rows)


def _build_today_trade_scoring(
    summary: Dict[str, Any],
    decision: Dict[str, Any],
    trades: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not trades:
        return {"enriched_trades": trades, "score_map": {}}

    signal_date = str(decision.get("signal_date") or "").strip()
    if not signal_date:
        enriched = []
        for t in trades:
            item = dict(t)
            item["score_note"] = "无法解析signal_date，直接返回原始成交"
            enriched.append(item)
        return {"enriched_trades": _sanitize(enriched), "score_map": {}}

    params = ((summary.get("assumptions") or {}).get("params") or {}) if isinstance(summary, dict) else {}
    entry_min_score = _to_float(params.get("entry_min_score")) or DEFAULT_ENTRY_MIN_SCORE
    keep_rank_mult = _to_int(params.get("keep_rank_mult"), DEFAULT_KEEP_RANK_MULT)

    mode = str(decision.get("mode", "")).lower()
    max_holdings = MODE_MAX_HOLDINGS.get(mode, MODE_MAX_HOLDINGS["neutral"])
    keep_top_n = max_holdings * max(1, keep_rank_mult)

    target_pool = set(_split_codes(decision.get("target_codes")))
    buy_candidates = set(_split_codes(decision.get("buy_candidates")))
    sell_plan_codes = set(_split_codes(decision.get("sell_plan_codes")))

    hist = _load_hist_with_factors(signal_date=signal_date)
    if hist.empty:
        enriched = []
        for t in trades:
            item = dict(t)
            item["score_note"] = "数据库评分数据不可用"
            enriched.append(item)
        return {"enriched_trades": _sanitize(enriched), "score_map": {}}

    try:
        signal_ts = pd.Timestamp(signal_date)
    except Exception:
        signal_ts = None
    if signal_ts is None:
        enriched = []
        for t in trades:
            item = dict(t)
            item["score_note"] = "signal_date鏍煎紡寮傚父"
            enriched.append(item)
        return {"enriched_trades": _sanitize(enriched), "score_map": {}}

    day_df = hist[hist["date"] == signal_ts].copy()
    if day_df.empty:
        response["message"] = (
            f"{signal_date} 当前不属于交易日或日内数据不足。"
            "系统将按可用 TopN 进行回退并给出最接近的候选集。"
            "若你持续看到零结果，请先确认行情是否已更新。"
        )
        return _sanitize(response)

    raw_map: Dict[str, pd.Series] = {}
    if not day_df.empty and "code" in day_df.columns:
        for _, row in day_df.iterrows():
            raw_map[str(row.get("code"))] = row

    scored = _score_candidates_on_signal_day(day_df=day_df)
    scored_map: Dict[str, pd.Series] = {}
    if not scored.empty:
        for _, row in scored.iterrows():
            scored_map[str(row.get("code"))] = row
    pool_size = int(len(scored))

    enriched_trades: List[Dict[str, Any]] = []
    score_map: Dict[str, Dict[str, Any]] = {}

    for trade in trades:
        item = dict(trade)
        code = str(item.get("code", ""))
        side = str(item.get("side", "")).upper()
        reason = str(item.get("reason", "") or "")

        score_total = None
        score_rank = None
        r_mom5 = None
        r_mom10 = None
        r_vol_ratio = None
        r_amt20 = None
        r_vol10_low = None
        in_keep_pool = False
        passed_score_pool = False

        if code in scored_map:
            sr = scored_map[code]
            score_total = _to_float(sr.get("score"))
            score_rank = _to_int(sr.get("score_rank"), 0)
            r_mom5 = _to_float(sr.get("r_mom5"))
            r_mom10 = _to_float(sr.get("r_mom10"))
            r_vol_ratio = _to_float(sr.get("r_vol_ratio"))
            r_amt20 = _to_float(sr.get("r_amt20"))
            r_vol10_low = _to_float(sr.get("r_vol10_low"))
            passed_score_pool = True
            in_keep_pool = (score_rank > 0) and (score_rank <= keep_top_n)

        if passed_score_pool:
            base = f"打分 {score_total:.3f}, rank={score_rank}/{pool_size}"
            comp = (
                f"comp [mom5:{(r_mom5 or 0):.3f}, mom10:{(r_mom10 or 0):.3f}, "
                f"vol_ratio:{(r_vol_ratio or 0):.3f}, amt20:{(r_amt20 or 0):.3f}, vol10_low:{(r_vol10_low or 0):.3f}]"
            )
            if side == "BUY":
                note = (
                    f"{base}，入场阈值={entry_min_score:.2f}，"
                    f"{'通过' if (score_total or 0) >= entry_min_score else '未过'}，{comp}"
                )
            elif "score_drop" in reason:
                note = (
                    f"{base}，Top{keep_top_n}筛选，"
                    f"{'已进入' if in_keep_pool else '未进入'}打分池，{comp}"
                )
            else:
                note = f"{base}, {comp}"
        else:
            fail_reason = _build_score_fail_reasons(raw_map.get(code))
            if "score_drop" in reason:
                note = f"评分通过失败：未满足评分阈值，原因：{fail_reason}"
            else:
                note = f"未通过评分池：{fail_reason}"

        item["score_total"] = score_total
        item["score_rank"] = score_rank
        item["score_pool_size"] = pool_size
        item["keep_top_n"] = keep_top_n
        item["entry_min_score"] = entry_min_score
        item["in_keep_pool"] = in_keep_pool
        item["in_target_pool"] = code in target_pool
        item["in_buy_candidates"] = code in buy_candidates
        item["in_sell_plan"] = code in sell_plan_codes
        item["score_components"] = {
            "r_mom5": r_mom5,
            "r_mom10": r_mom10,
            "r_vol_ratio": r_vol_ratio,
            "r_amt20": r_amt20,
            "r_vol10_low": r_vol10_low,
        }
        item["score_note"] = note

        enriched_trades.append(item)
        score_map[code] = {
            "score_total": score_total,
            "score_rank": score_rank,
            "score_pool_size": pool_size,
            "in_keep_pool": in_keep_pool,
            "score_note": note,
        }

    return {"enriched_trades": _sanitize(enriched_trades), "score_map": _sanitize(score_map)}


def _dedup_codes(codes: List[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for code in codes:
        c = str(code or "").strip()
        if not c or c in seen:
            continue
        seen.add(c)
        result.append(c)
    return result


def _load_daily_artifact_row(
    strategy_version: str,
    artifact_key: str,
    date_key: str,
    target_date: str,
) -> Dict[str, Any]:
    normalized_target = _normalize_date_str(target_date)
    if not normalized_target:
        return {}
    payloads = _load_strategy_artifacts(strategy_version=str(strategy_version or "").strip())
    rows = payloads.get(artifact_key)
    if not isinstance(rows, list):
        return {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        normalized_item_date = _normalize_date_str(item.get(date_key))
        if str(normalized_item_date or "") == str(normalized_target):
            return _sanitize(dict(item))
    return {}


def _normalize_intraday_cutoff_time(cutoff_time: Optional[str]) -> str:
    raw = str(cutoff_time or "").strip() or DEFAULT_INTRADAY_CUTOFF_TIME
    try:
        parsed = pd.Timestamp(f"2000-01-01 {raw}").time()
        return parsed.strftime("%H:%M")
    except Exception:
        return DEFAULT_INTRADAY_CUTOFF_TIME


def _load_intraday_minute_bars(
    code: str,
    signal_date: str,
    period: int,
    cutoff_time: Optional[str] = None,
) -> pd.DataFrame:
    normalized_code = str(code or "").strip()
    normalized_date = _normalize_date_str(signal_date)
    cutoff_text = _normalize_intraday_cutoff_time(cutoff_time)
    if not normalized_code or not normalized_date:
        return pd.DataFrame()
    start_dt = f"{normalized_date} 00:00:00"
    cutoff_dt = f"{normalized_date} {cutoff_text}:59"

    table_map = {15: "kline_minute_15", 30: "kline_minute_30"}
    table_name = table_map.get(int(period))
    if not table_name:
        return pd.DataFrame()

    try:
        if clickhouse_available():
            df = clickhouse_query_df(
                f"""
                SELECT code, datetime, open, high, low, close, volume, amount
                FROM {table_name}
                WHERE substr(code, 1, 6) = ?
                  AND datetime >= ?::TIMESTAMP
                  AND datetime <= ?::TIMESTAMP
                ORDER BY datetime ASC
                """,
                [normalized_code[:6], start_dt, cutoff_dt],
            )
        else:
            sql = text(
                f"""
                SELECT code, datetime, open, high, low, close, volume, amount
                FROM {table_name}
                WHERE (code = :code OR code LIKE :code_like)
                  AND datetime >= :start_dt
                  AND datetime <= :cutoff_dt
                ORDER BY datetime ASC
                """
            )
            with db.engine.connect() as conn:
                df = pd.read_sql(
                    sql,
                    conn,
                    params={
                        "code": normalized_code,
                        "code_like": f"{normalized_code}.%",
                        "start_dt": start_dt,
                        "cutoff_dt": cutoff_dt,
                    },
                )
    except Exception as exc:
        logger.warning(
            f"load intraday bars failed: code={normalized_code}, signal_date={normalized_date}, period={period}, error={exc}"
        )
        return pd.DataFrame()

    if df is None or df.empty:
        try:
            from scripts.collect_intraday_snapshots import load_snapshot_minute_bars

            df = load_snapshot_minute_bars(
                codes=[normalized_code],
                start_date=normalized_date,
                end_date=normalized_date,
                period_minutes=int(period),
                asset_type="stock",
            )
            if df is not None and not df.empty:
                cutoff_ts = pd.Timestamp(cutoff_dt)
                df = df[pd.to_datetime(df["datetime"], errors="coerce") <= cutoff_ts].copy()
                logger.info(
                    f"loaded intraday bars from quote snapshots: code={normalized_code}, "
                    f"signal_date={normalized_date}, period={period}, rows={len(df)}"
                )
        except Exception as exc:
            logger.warning(
                f"load snapshot intraday bars failed: code={normalized_code}, signal_date={normalized_date}, period={period}, error={exc}"
            )

    if df.empty:
        return df

    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime", "open", "high", "low", "close"]).reset_index(drop=True)
    return df


def _evaluate_intraday_buy_candidate(
    candidate: Dict[str, Any],
    bars15: pd.DataFrame,
    bars30: pd.DataFrame,
    cutoff_time: str,
) -> Dict[str, Any]:
    code = str(candidate.get("code") or "").strip()
    name = str(candidate.get("name") or "").strip()
    score_total = _to_float(candidate.get("score_total"))
    score_rank = _to_int(candidate.get("score_rank"), 0)

    payload: Dict[str, Any] = {
        "code": code,
        "name": name,
        "score_total": score_total,
        "score_rank": score_rank if score_rank > 0 else None,
        "cutoff_time": cutoff_time,
        "data_available": False,
        "should_buy": False,
        "action": "WAIT",
        "status": "missing",
        "trigger_time": None,
        "trigger_price": None,
        "signal_strength": None,
        "last_bar_time_15m": None,
        "last_bar_time_30m": None,
        "breakout_level_15m": None,
        "breakout_pct_15m": None,
        "volume_ratio_15m": None,
        "session_return": None,
        "trend_ok_30m": False,
        "breakout_ok_15m": False,
        "near_breakout_ok_15m": False,
        "volume_ok_15m": False,
        "stretch_ok": False,
        "reason": "分钟线数据缺失，无法确认尾盘买点",
    }

    if bars15.empty or bars30.empty:
        return payload
    if len(bars15) < 4 or len(bars30) < 3:
        payload["status"] = "insufficient"
        payload["reason"] = "分钟线样本不足，至少霢?4 ?5分钟K ?3 ?0分钟K"
        return payload

    latest15 = bars15.iloc[-1]
    prev15_window = bars15.iloc[-3:-1]
    prev15_high = float(prev15_window["high"].max()) if not prev15_window.empty else float(bars15.iloc[-2]["high"])
    recent15_base = bars15.iloc[-4:-1]
    latest30 = bars30.iloc[-1]
    prev30 = bars30.iloc[-2]
    ma30_3 = float(bars30["close"].tail(3).mean())

    latest15_close = _to_float(latest15.get("close"))
    latest15_open = _to_float(latest15.get("open"))
    latest15_volume = _to_float(latest15.get("volume"))
    volume_base = _to_float(recent15_base["volume"].mean())
    latest30_close = _to_float(latest30.get("close"))
    latest30_low = _to_float(latest30.get("low"))
    prev30_close = _to_float(prev30.get("close"))
    prev30_low = _to_float(prev30.get("low"))
    first_open = _to_float(bars15.iloc[0].get("open"))

    breakout_pct = None
    if latest15_close is not None and prev15_high > 0:
        breakout_pct = latest15_close / prev15_high - 1.0
    volume_ratio = None
    if latest15_volume is not None and volume_base is not None and volume_base > 0:
        volume_ratio = latest15_volume / volume_base
    session_return = None
    if latest15_close is not None and first_open is not None and first_open > 0:
        session_return = latest15_close / first_open - 1.0

    trend_ok = bool(
        latest30_close is not None
        and prev30_close is not None
        and latest30_low is not None
        and prev30_low is not None
        and latest30_close >= ma30_3
        and latest30_close >= prev30_close
        and latest30_low >= prev30_low * 0.995
    )
    breakout_ok = bool(
        latest15_close is not None
        and latest15_open is not None
        and latest15_close >= prev15_high * DEFAULT_INTRADAY_BREAKOUT_BUFFER
        and latest15_close > latest15_open
    )
    near_breakout_ok = bool(
        breakout_pct is not None
        and breakout_pct > -0.02
    )
    volume_ok = bool(volume_ratio is not None and volume_ratio >= DEFAULT_INTRADAY_VOLUME_RATIO_FLOOR)
    stretch_ok = bool(session_return is not None and session_return <= DEFAULT_INTRADAY_SESSION_RETURN_CAP)
    should_buy = bool(stretch_ok and trend_ok and (volume_ok or near_breakout_ok))

    strength = 0.0
    if score_total is not None:
        strength += min(max(score_total, 0.0), 1.0) * 0.45
    if breakout_pct is not None:
        strength += min(max(breakout_pct, 0.0) / 0.02, 1.0) * 0.20
    if volume_ratio is not None:
        strength += min(max(volume_ratio, 0.0) / 1.5, 1.0) * 0.20
    if trend_ok:
        strength += 0.10
    if stretch_ok:
        strength += 0.05

    reason_parts: List[str] = []
    reason_parts.append("30分钟趋势通过" if trend_ok else "30分钟趋势未通过")
    if breakout_ok:
        reason_parts.append("15分钟突破通过")
    elif near_breakout_ok:
        reason_parts.append("15分钟接近突破但未明确放量")
    else:
        reason_parts.append("15分钟突破未通过")
    reason_parts.append("15分钟量能通过" if volume_ok else "15分钟量能未通过")
    reason_parts.append("上影形态正常" if stretch_ok else "上影形态偏离")

    payload.update(
        {
            "data_available": True,
            "should_buy": should_buy,
            "action": "BUY" if should_buy else "WAIT",
            "status": "buy" if should_buy else "watch",
            "trigger_time": latest15["datetime"].strftime("%H:%M") if should_buy else None,
            "trigger_price": latest15_close if should_buy else None,
            "signal_strength": round(float(min(strength, 1.0)), 4),
            "last_bar_time_15m": latest15["datetime"].strftime("%H:%M"),
            "last_bar_time_30m": latest30["datetime"].strftime("%H:%M"),
            "breakout_level_15m": prev15_high,
            "breakout_pct_15m": breakout_pct,
            "volume_ratio_15m": volume_ratio,
        "session_return": session_return,
        "trend_ok_30m": trend_ok,
        "breakout_ok_15m": breakout_ok,
        "near_breakout_ok_15m": near_breakout_ok,
        "volume_ok_15m": volume_ok,
        "stretch_ok": stretch_ok,
        "reason": " | ".join(reason_parts),
    }
    )
    return payload


def _infer_sell_reason_code(candidate: Dict[str, Any]) -> str:
    raw_reason = str(candidate.get("planned_reason") or "").strip().lower()
    if raw_reason:
        return raw_reason
    reason_text = " ".join(
        [
            str(candidate.get("holding_reason") or ""),
            str(candidate.get("selection_note") or ""),
            str(candidate.get("notes_text") or ""),
        ]
    ).lower()
    if "stop_loss" in reason_text or "姝㈡崯" in reason_text:
        return "stop_loss"
    if "take_profit" in reason_text or "止盈" in reason_text:
        return "take_profit"
    if "trailing" in reason_text or "鍥炴挙" in reason_text:
        return "trailing_stop"
    if "score_drop" in reason_text or "评分" in reason_text:
        return "score_drop"
    if "portfolio_cut" in reason_text or "闄嶄粨" in reason_text:
        return "portfolio_cut"
    if "trend_break" in reason_text or "瓒嬪娍" in reason_text or "ma10" in reason_text:
        return "trend_break"
    return "sell_plan"


def _evaluate_intraday_sell_candidate(
    candidate: Dict[str, Any],
    bars15: pd.DataFrame,
    bars30: pd.DataFrame,
    cutoff_time: str,
) -> Dict[str, Any]:
    code = str(candidate.get("code") or "").strip()
    name = str(candidate.get("name") or "").strip()
    planned_reason = _infer_sell_reason_code(candidate)
    score_total = _to_float(candidate.get("score_total"))
    score_rank = _to_int(candidate.get("score_rank"), 0)
    stop_loss_triggered = bool(candidate.get("stop_loss_triggered"))
    take_profit_reached = bool(candidate.get("take_profit_reached"))

    payload: Dict[str, Any] = {
        "code": code,
        "name": name,
        "score_total": score_total,
        "score_rank": score_rank if score_rank > 0 else None,
        "cutoff_time": cutoff_time,
        "planned_reason": planned_reason,
        "data_available": False,
        "should_sell": False,
        "action": "HOLD",
        "status": "missing",
        "trigger_time": None,
        "trigger_price": None,
        "signal_strength": None,
        "last_bar_time_15m": None,
        "last_bar_time_30m": None,
        "weakness_level_15m": None,
        "weakness_pct_15m": None,
        "volume_ratio_15m": None,
        "trend_weak_30m": False,
        "weakness_ok_15m": False,
        "sell_pressure_ok": False,
        "stop_loss_triggered": stop_loss_triggered,
        "take_profit_reached": take_profit_reached,
        "reason": "分钟线数据缺失，无法确认盘中卖点",
    }

    if bars15.empty or bars30.empty:
        return payload
    if len(bars15) < 3 or len(bars30) < 2:
        payload["status"] = "insufficient"
        payload["reason"] = "分钟线样本不足，至少霢?3 ?5分钟K ?2 ?0分钟K"
        return payload

    latest15 = bars15.iloc[-1]
    prev15_window = bars15.iloc[-3:-1]
    prev15_low = float(prev15_window["low"].min()) if not prev15_window.empty else float(bars15.iloc[-2]["low"])
    recent15_base = bars15.iloc[-3:-1]
    latest30 = bars30.iloc[-1]
    prev30 = bars30.iloc[-2]
    ma30_3 = float(bars30["close"].tail(min(3, len(bars30))).mean())

    latest15_close = _to_float(latest15.get("close"))
    latest15_open = _to_float(latest15.get("open"))
    latest15_volume = _to_float(latest15.get("volume"))
    volume_base = _to_float(recent15_base["volume"].mean())
    latest30_close = _to_float(latest30.get("close"))
    latest30_open = _to_float(latest30.get("open"))
    prev30_close = _to_float(prev30.get("close"))

    weakness_pct = None
    if latest15_close is not None and prev15_low > 0:
        weakness_pct = latest15_close / prev15_low - 1.0
    volume_ratio = None
    if latest15_volume is not None and volume_base is not None and volume_base > 0:
        volume_ratio = latest15_volume / volume_base

    weakness_ok = bool(
        latest15_close is not None
        and latest15_open is not None
        and latest15_close <= prev15_low * 1.002
        and latest15_close < latest15_open
    )
    trend_weak = bool(
        latest30_close is not None
        and latest30_open is not None
        and prev30_close is not None
        and latest30_close < ma30_3
        and latest30_close <= latest30_open
        and latest30_close <= prev30_close * 1.002
    )
    sell_pressure_ok = bool(volume_ratio is not None and volume_ratio >= DEFAULT_INTRADAY_VOLUME_RATIO_FLOOR)

    risk_exit_reasons = {"stop_loss", "trend_break", "score_drop", "portfolio_cut", "sell_plan"}
    profit_exit_reasons = {"take_profit", "take_profit_partial", "take_profit_small_full", "trailing_stop"}

    should_sell = False
    if planned_reason in risk_exit_reasons:
        should_sell = bool(weakness_ok or (trend_weak and sell_pressure_ok) or stop_loss_triggered)
    elif planned_reason in profit_exit_reasons:
        should_sell = bool((weakness_ok and sell_pressure_ok) or (take_profit_reached and trend_weak))

    strength = 0.0
    if weakness_pct is not None:
        strength += min(max(-weakness_pct, 0.0) / 0.02, 1.0) * 0.40
    if volume_ratio is not None:
        strength += min(max(volume_ratio, 0.0) / 1.5, 1.0) * 0.25
    if trend_weak:
        strength += 0.20
    if stop_loss_triggered:
        strength += 0.10
    if planned_reason in risk_exit_reasons:
        strength += 0.05

    reason_parts = []
    reason_parts.append("15分钟走弱成立" if weakness_ok else "15分钟未明显走弱")
    reason_parts.append("30分钟趋势转弱" if trend_weak else "30分钟趋势无明显转弱")
    reason_parts.append("卖压量能通过" if sell_pressure_ok else "卖压量能不足")
    if stop_loss_triggered:
        reason_parts.append("触发止损线")
    if take_profit_reached:
        reason_parts.append("触发止盈线")

    payload.update(
        {
            "data_available": True,
            "should_sell": should_sell,
            "action": "SELL" if should_sell else "HOLD",
            "status": "sell" if should_sell else "watch",
            "trigger_time": latest15["datetime"].strftime("%H:%M") if should_sell else None,
            "trigger_price": latest15_close if should_sell else None,
            "signal_strength": round(float(min(strength, 1.0)), 4),
            "last_bar_time_15m": latest15["datetime"].strftime("%H:%M"),
            "last_bar_time_30m": latest30["datetime"].strftime("%H:%M"),
            "weakness_level_15m": prev15_low,
            "weakness_pct_15m": weakness_pct,
            "volume_ratio_15m": volume_ratio,
            "trend_weak_30m": trend_weak,
            "weakness_ok_15m": weakness_ok,
            "sell_pressure_ok": sell_pressure_ok,
            "reason": " | ".join(reason_parts),
        }
    )
    return payload


def _build_intraday_execution_analysis(
    strategy_version: str,
    signal_date: str,
    selection_analysis: Dict[str, Any],
    holdings_strategy: Optional[List[Dict[str, Any]]] = None,
    cutoff_time: Optional[str] = None,
    sell_cutoff_time: Optional[str] = None,
) -> Dict[str, Any]:
    signal_date_text = _normalize_date_str(signal_date) or ""
    cutoff_text = _normalize_intraday_cutoff_time(cutoff_time)
    sell_cutoff_text = _normalize_intraday_cutoff_time(sell_cutoff_time or DEFAULT_INTRADAY_SELL_CUTOFF_TIME)
    analysis: Dict[str, Any] = {
        "signal_date": signal_date_text,
        "cutoff_time": cutoff_text,
        "sell_cutoff_time": sell_cutoff_text,
        "data_status": "pending",
        "message": "",
        "mail_should_send": False,
        "triggered_count": 0,
        "triggered_codes": [],
        "buy_point_candidates": [],
        "sell_triggered_count": 0,
        "sell_triggered_codes": [],
        "sell_point_candidates": [],
    }

    candidate_details = selection_analysis.get("buy_candidate_details") or []
    if not isinstance(candidate_details, list) or not candidate_details:
        raw_codes = selection_analysis.get("buy_candidates") or []
        candidate_details = [{"code": code} for code in raw_codes if str(code or "").strip()]

    candidates = []
    seen = set()
    for item in candidate_details:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        candidates.append(item)

    if not signal_date_text:
        analysis["data_status"] = "missing"
        analysis["message"] = "缺少 signal_date，或日期解析失败，请检查传入日期参数。"
        return _sanitize(analysis)

    rows: List[Dict[str, Any]] = []
    data_ready_count = 0
    for candidate in candidates:
        code = str(candidate.get("code") or "").strip()
        bars15 = _load_intraday_minute_bars(code=code, signal_date=signal_date_text, period=15, cutoff_time=cutoff_text)
        bars30 = _load_intraday_minute_bars(code=code, signal_date=signal_date_text, period=30, cutoff_time=cutoff_text)
        row = _evaluate_intraday_buy_candidate(candidate=candidate, bars15=bars15, bars30=bars30, cutoff_time=cutoff_text)
        if row.get("data_available"):
            data_ready_count += 1
        rows.append(row)

    triggered_codes = [str(x.get("code") or "") for x in rows if x.get("should_buy")]
    analysis["buy_point_candidates"] = rows
    analysis["triggered_codes"] = triggered_codes
    analysis["triggered_count"] = len(triggered_codes)
    analysis["mail_should_send"] = bool(triggered_codes)

    holdings_items = holdings_strategy or []
    holdings_map: Dict[str, Dict[str, Any]] = {}
    for item in holdings_items:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if code:
            holdings_map[code] = item

    sell_candidates_raw = selection_analysis.get("sell_plan_details") or []
    sell_candidates: List[Dict[str, Any]] = []
    seen_sell = set()
    for item in sell_candidates_raw:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if not code or code in seen_sell:
            continue
        seen_sell.add(code)
        merged = dict(item)
        merged.update(holdings_map.get(code) or {})
        sell_candidates.append(merged)
    for code, item in holdings_map.items():
        if code in seen_sell:
            continue
        if bool(item.get("stop_loss_triggered")) or bool(item.get("take_profit_reached")):
            merged = dict(item)
            if bool(item.get("stop_loss_triggered")):
                merged["planned_reason"] = "stop_loss"
            elif bool(item.get("take_profit_reached")):
                merged["planned_reason"] = "take_profit"
            sell_candidates.append(merged)
            seen_sell.add(code)

    sell_rows: List[Dict[str, Any]] = []
    sell_data_ready_count = 0
    for candidate in sell_candidates:
        code = str(candidate.get("code") or "").strip()
        bars15 = _load_intraday_minute_bars(code=code, signal_date=signal_date_text, period=15, cutoff_time=sell_cutoff_text)
        bars30 = _load_intraday_minute_bars(code=code, signal_date=signal_date_text, period=30, cutoff_time=sell_cutoff_text)
        row = _evaluate_intraday_sell_candidate(candidate=candidate, bars15=bars15, bars30=bars30, cutoff_time=sell_cutoff_text)
        if row.get("data_available"):
            sell_data_ready_count += 1
        sell_rows.append(row)

    sell_triggered_codes = [str(x.get("code") or "") for x in sell_rows if x.get("should_sell")]
    analysis["sell_point_candidates"] = sell_rows
    analysis["sell_triggered_codes"] = sell_triggered_codes
    analysis["sell_triggered_count"] = len(sell_triggered_codes)

    if data_ready_count == 0 and sell_data_ready_count == 0:
        analysis["data_status"] = "missing"
        analysis["message"] = f"{signal_date_text} 分钟线尚未入库或未更新到买入{cutoff_text}/卖出{sell_cutoff_text}"
    elif triggered_codes:
        analysis["data_status"] = "buy_point_ready"
        analysis["message"] = f"尾盘分钟买点已触发：{', '.join(triggered_codes)}"
    elif sell_triggered_codes:
        analysis["data_status"] = "sell_point_ready"
        analysis["message"] = f"盘中分钟卖点已触发：{', '.join(sell_triggered_codes)}"
    elif not candidates and not sell_candidates:
        analysis["data_status"] = "no_candidates"
        analysis["message"] = "当日无待买入候，且无霢要分钟确认的卖出计划"
    else:
        analysis["data_status"] = "watch"
        analysis["message"] = "当前仅用于交易时段分析，等待后续数据更新后可继续执行。"

    try:
        _upsert_daily_artifact_row(
            strategy_version=str(strategy_version or "").strip(),
            artifact_key=TRADING_INTRADAY_SIGNAL_ARTIFACT_KEY,
            date_key="signal_date",
            row=analysis,
        )
    except Exception as exc:
        logger.warning(
            f"persist intraday execution failed: version={strategy_version}, signal_date={signal_date_text}, error={exc}"
        )
    return _sanitize(analysis)


def _build_today_selection_analysis(
    strategy_version: str,
    summary: Dict[str, Any],
    decision: Dict[str, Any],
    holding: Dict[str, Any],
    holdings_strategy: List[Dict[str, Any]],
) -> Dict[str, Any]:
    raw_params = ((summary.get("assumptions") or {}).get("params") or {}) if isinstance(summary, dict) else {}
    params = _apply_live_runtime_params(raw_params)
    entry_min_score = _to_float(params.get("entry_min_score")) or DEFAULT_ENTRY_MIN_SCORE
    keep_rank_mult = max(1, _to_int(params.get("keep_rank_mult"), DEFAULT_KEEP_RANK_MULT))
    mode = str(decision.get("mode", "")).lower()
    max_holdings = MODE_MAX_HOLDINGS.get(mode, MODE_MAX_HOLDINGS["neutral"])
    keep_top_n = max_holdings * keep_rank_mult

    target_pool = _dedup_codes(_split_codes(decision.get("target_codes")))
    strategy_holding_pool = list(target_pool)
    buy_candidates = _dedup_codes(_split_codes(decision.get("buy_candidates")))
    sell_plan_codes = _dedup_codes(_split_codes(decision.get("sell_plan_codes")))
    holding_rows = _parse_holdings_desc(holding.get("holdings"))
    holding_codes = _dedup_codes([str(x.get("code") or "") for x in holding_rows])

    holdings_map: Dict[str, Dict[str, Any]] = {
        str(x.get("code") or ""): x for x in holdings_strategy if str(x.get("code") or "").strip()
    }

    response = {
        "signal_date": str(decision.get("signal_date") or "").strip(),
        "mode": mode,
        "entry_min_score": entry_min_score,
        "keep_rank_mult": keep_rank_mult,
        "max_holdings": max_holdings,
        "keep_top_n": keep_top_n,
        "strategy_holding_pool": strategy_holding_pool,
        "target_pool": target_pool,
        "buy_candidates": buy_candidates,
        "sell_plan_codes": sell_plan_codes,
        "holding_codes": holding_codes,
        "score_pool_size": 0,
        "target_details": [],
        "buy_candidate_details": [],
        "sell_plan_details": [],
        "holding_details": [],
        "top_scored": [],
        "message": "",
    }

    signal_date = response["signal_date"]
    if not signal_date:
        response["message"] = "缺少signal_date，无法还原当日股评分"
        return _sanitize(response)

    hist = _load_hist_with_factors(signal_date=signal_date)
    if hist.empty:
        response["message"] = "数据库评分数据不可用"
        return _sanitize(response)

    try:
        signal_ts = pd.Timestamp(signal_date)
    except Exception:
        response["message"] = "signal_date鏍煎紡寮傚父"
        return _sanitize(response)

    day_df = hist[hist["date"] == signal_ts].copy()
    raw_map: Dict[str, pd.Series] = {}
    if not day_df.empty and "code" in day_df.columns:
        for _, row in day_df.iterrows():
            raw_map[str(row.get("code"))] = row

    scored = _score_candidates_on_signal_day(day_df=day_df)
    scored_map: Dict[str, pd.Series] = {}
    if not scored.empty:
        for _, row in scored.iterrows():
            scored_map[str(row.get("code"))] = row
    response["score_pool_size"] = int(len(scored))
    if response["score_pool_size"] <= 0:
        response["message"] = f"当前信号日 {signal_date} 评分为 0，可能为本日无可评分样本。"
    try:
        _persist_top_selection_scores(
            strategy_version=str(strategy_version or "").strip(),
            signal_date=signal_date,
            day_df=day_df,
            scored_df=scored,
            entry_min_score=float(entry_min_score),
            keep_rank_mult=int(keep_rank_mult),
            max_holdings=int(max_holdings),
            keep_top_n=int(keep_top_n),
            top_n=30,
        )
    except Exception as exc:
        logger.warning(f"persist top selection scores failed: signal_date={signal_date}, error={exc}")

    def _code_name(code: str) -> Optional[str]:
        if code in holdings_map and holdings_map[code].get("name"):
            return str(holdings_map[code].get("name"))
        raw = raw_map.get(code)
        if raw is None:
            return None
        name = raw.get("name")
        if name is None:
            return None
        try:
            if pd.isna(name):
                return None
        except Exception:
            pass
        return str(name)

    def _build_one(code: str) -> Dict[str, Any]:
        raw = raw_map.get(code)
        scored_row = scored_map.get(code)
        in_score_pool = scored_row is not None
        score_total = None
        score_rank = None
        r_mom5 = None
        r_mom10 = None
        r_vol_ratio = None
        r_amt20 = None
        r_vol10_low = None
        in_keep_pool = False
        passed_entry = False

        if in_score_pool:
            score_total = _to_float(scored_row.get("score"))
            score_rank = _to_int(scored_row.get("score_rank"), 0)
            r_mom5 = _to_float(scored_row.get("r_mom5"))
            r_mom10 = _to_float(scored_row.get("r_mom10"))
            r_vol_ratio = _to_float(scored_row.get("r_vol_ratio"))
            r_amt20 = _to_float(scored_row.get("r_amt20"))
            r_vol10_low = _to_float(scored_row.get("r_vol10_low"))
            in_keep_pool = score_rank > 0 and score_rank <= keep_top_n
            passed_entry = (score_total is not None) and (score_total >= entry_min_score)
            note = (
                f"当前得分{(score_total or 0):.3f}，排名{score_rank}/{len(scored)}，"
                f"阈值{entry_min_score:.2f}{'，已达入选条件' if passed_entry else '，未达入选条件'}"
            )
        else:
            fail_reason = _build_score_fail_reasons(raw)
            note = f"未命中得分池，原因：{fail_reason}"

        holding_item = holdings_map.get(code, {})
        notes = holding_item.get("notes")
        if isinstance(notes, list):
            hold_reason = " / ".join([str(x) for x in notes if str(x).strip()])
        elif notes:
            hold_reason = str(notes)
        else:
            hold_reason = ""

        return {
            "code": code,
            "name": _code_name(code),
            "score_total": score_total,
            "score_rank": score_rank,
            "score_pool_size": int(len(scored)),
            "in_score_pool": in_score_pool,
            "in_keep_pool": in_keep_pool,
            "passed_entry_min_score": passed_entry,
            "in_target_pool": code in target_pool,
            "in_buy_candidates": code in buy_candidates,
            "in_sell_plan": code in sell_plan_codes,
            "in_holdings": code in holding_codes,
            "score_components": {
                "r_mom5": r_mom5,
                "r_mom10": r_mom10,
                "r_vol_ratio": r_vol_ratio,
                "r_amt20": r_amt20,
                "r_vol10_low": r_vol10_low,
            },
            "selection_note": note,
            "holding_reason": hold_reason,
            "shares": holding_item.get("shares"),
            "current_price": holding_item.get("current_price"),
            "avg_cost_est": holding_item.get("avg_cost_est"),
        }

    response["target_details"] = [_build_one(code) for code in target_pool]
    response["buy_candidate_details"] = [_build_one(code) for code in buy_candidates]
    response["sell_plan_details"] = [_build_one(code) for code in sell_plan_codes]
    response["holding_details"] = [_build_one(code) for code in holding_codes]

    top_scored: List[Dict[str, Any]] = []
    score_reference_df = scored.copy() if not scored.empty else pd.DataFrame()
    diagnostic_mode = False
    if score_reference_df.empty and not day_df.empty:
        diag_df = day_df.copy().replace([np.inf, -np.inf], np.nan)
        diag_df = diag_df.dropna(subset=["code", "mom5", "mom10", "vol_ratio", "amt20", "vol10", "ma10", "close"])
        if not diag_df.empty:
            diagnostic_mode = True
            for c in ["mom5", "mom10", "vol_ratio", "amt20"]:
                diag_df[f"r_{c}"] = diag_df[c].rank(pct=True)
            diag_df["r_vol10_low"] = 1 - diag_df["vol10"].rank(pct=True)
            diag_df["score"] = (
                0.35 * diag_df["r_mom5"]
                + 0.20 * diag_df["r_mom10"]
                + 0.20 * diag_df["r_vol_ratio"]
                + 0.20 * diag_df["r_amt20"]
                + 0.05 * diag_df["r_vol10_low"]
            )
            diag_df = diag_df.sort_values(["score", "amount"], ascending=[False, False]).reset_index(drop=True)
            diag_df["score_rank"] = np.arange(1, len(diag_df) + 1)
            score_reference_df = diag_df

    if not score_reference_df.empty:
        for _, row in score_reference_df.head(30).iterrows():
            code = str(row.get("code"))
            score_total = _to_float(row.get("score"))
            score_rank = _to_int(row.get("score_rank"), 0)
            in_score_pool = code in scored_map
            passed_entry = in_score_pool and (score_total is not None) and (score_total >= entry_min_score)
            fail_reason = _build_score_fail_reasons(raw_map.get(code))
            top_scored.append(
                {
                    "code": code,
                    "name": _code_name(code),
                    "score_total": score_total,
                    "score_rank": score_rank,
                    "mom5": _to_float(row.get("mom5")),
                    "mom10": _to_float(row.get("mom10")),
                    "vol_ratio": _to_float(row.get("vol_ratio")),
                    "amt20": _to_float(row.get("amt20")),
                    "vol10": _to_float(row.get("vol10")),
                    "r_mom5": _to_float(row.get("r_mom5")),
                    "r_mom10": _to_float(row.get("r_mom10")),
                    "r_vol_ratio": _to_float(row.get("r_vol_ratio")),
                    "r_amt20": _to_float(row.get("r_amt20")),
                    "r_vol10_low": _to_float(row.get("r_vol10_low")),
                    "passed_entry_min_score": passed_entry,
                    "in_target_pool": code in target_pool,
                    "in_buy_candidates": code in buy_candidates,
                    "in_sell_plan": code in sell_plan_codes,
                    "in_holdings": code in holding_codes,
                    "selection_note": (
                        f"得分{(score_total or 0):.3f}，已达入选条件；阈值{entry_min_score:.2f}"
                        if passed_entry
                        else (
                            f"得分{(score_total or 0):.3f}，未达入选条件；原因：{fail_reason}"
                            if diagnostic_mode
                            else f"得分{(score_total or 0):.3f}，未达入选阈值 {entry_min_score:.2f}"
                        )
                    ),
                }
            )
    response["top_scored"] = top_scored
    if not buy_candidates and diagnostic_mode and top_scored:
        response["message"] = (
            f"{signal_date} 当前信号日评分计算后仍为0。"
            "页面上显示的是前30名评分样本，请继续检查是否因规则阈值过严导致无标的入选。"
        )
    elif not buy_candidates and not scored.empty:
        top_score = _to_float(scored.iloc[0].get("score"))
        top_score_text = f"{top_score:.3f}" if top_score is not None else "--"
        response["message"] = (
            f"{signal_date} 当前信号日评分计算完成，但目前未达到买入条件。"
            f"最高得分为 {top_score_text}，入选阈值 {entry_min_score:.2f}。"
            "页面上显示的是前30个评分样本，可在诊断模式中进一步查看。"
        )
    return _sanitize(response)


def _latest_date_from_df(df: pd.DataFrame, date_col: str) -> Optional[str]:
    if df.empty or date_col not in df.columns:
        return None
    dates = df[date_col].dropna().astype(str)
    if dates.empty:
        return None
    return dates.iloc[-1]


def _collect_available_trade_dates(
    decisions_df: pd.DataFrame,
    holdings_df: pd.DataFrame,
    trades_df: pd.DataFrame,
) -> List[str]:
    date_set = set()
    if not decisions_df.empty and "trade_date" in decisions_df.columns:
        date_set.update(decisions_df["trade_date"].dropna().astype(str).tolist())
    if not holdings_df.empty and "date" in holdings_df.columns:
        date_set.update(holdings_df["date"].dropna().astype(str).tolist())
    if not trades_df.empty and "date" in trades_df.columns:
        date_set.update(trades_df["date"].dropna().astype(str).tolist())
    valid_dates: List[str] = []
    for d in date_set:
        if not d:
            continue
        try:
            pd.Timestamp(d)
            valid_dates.append(d)
        except Exception:
            continue
    return sorted(valid_dates, key=lambda x: pd.Timestamp(x))


def _resolve_snapshot_date(
    target_date: Optional[str],
    fallback_date: Optional[str],
    decisions_df: pd.DataFrame,
    holdings_df: pd.DataFrame,
    trades_df: pd.DataFrame,
) -> Dict[str, Optional[str]]:
    requested = str(target_date).strip() if target_date else None
    selected = requested or fallback_date
    note = None
    if not selected:
        return {"selected_date": None, "note": note}

    available_dates = _collect_available_trade_dates(
        decisions_df=decisions_df,
        holdings_df=holdings_df,
        trades_df=trades_df,
    )
    if not available_dates:
        return {"selected_date": selected, "note": note}

    if selected in available_dates:
        return {"selected_date": selected, "note": note}

    try:
        selected_ts = pd.Timestamp(selected)
        backward_dates = [d for d in available_dates if pd.Timestamp(d) <= selected_ts]
    except Exception:
        backward_dates = []

    if backward_dates:
        rolled = backward_dates[-1]
    else:
        rolled = available_dates[-1]

    if requested:
        note = f"请求日期 {requested} 暂无交易快照，已回到最近有数据?{rolled}"
    return {"selected_date": rolled, "note": note}


def _split_codes(raw: Any) -> List[str]:
    if raw is None:
        return []
    text = str(raw).strip()
    if not text or text.lower() == "nan":
        return []
    return [item for item in text.split("|") if item]


def _parse_holdings_desc(raw: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if raw is None:
        return items
    text = str(raw).strip()
    if not text or text.lower() == "nan":
        return items
    for part in text.split("|"):
        part = part.strip()
        if not part:
            continue
        m = HOLDING_PATTERN.match(part)
        if not m:
            continue
        items.append(
            {
                "code": m.group("code"),
                "shares": int(m.group("shares")),
                "current_price": float(m.group("price")),
            }
        )
    return items


def _extract_daily_snapshot(
    target_date: str,
    decisions_df: pd.DataFrame,
    holdings_df: pd.DataFrame,
    trades_df: pd.DataFrame,
) -> Dict[str, Any]:
    decision: Dict[str, Any] = {}
    holding: Dict[str, Any] = {}
    trades: List[Dict[str, Any]] = []

    if not decisions_df.empty and "trade_date" in decisions_df.columns:
        day_dec = decisions_df[decisions_df["trade_date"].astype(str) == target_date]
        if not day_dec.empty:
            decision = _sanitize(day_dec.iloc[-1].to_dict())

    if not holdings_df.empty and "date" in holdings_df.columns:
        day_hold = holdings_df[holdings_df["date"].astype(str) == target_date]
        if not day_hold.empty:
            holding = _sanitize(day_hold.iloc[-1].to_dict())

    if not trades_df.empty and "date" in trades_df.columns:
        day_trades = trades_df[trades_df["date"].astype(str) == target_date].copy()
        if not day_trades.empty:
            if "side" in day_trades.columns:
                day_trades["side_order"] = day_trades["side"].map({"SELL": 0, "BUY": 1}).fillna(9)
            else:
                day_trades["side_order"] = 9
            day_trades = day_trades.sort_values(["side_order", "code"]).drop(columns=["side_order"])
            trades = [_sanitize(row) for row in day_trades.to_dict(orient="records")]

    return {"date": target_date, "decision": decision, "holding": holding, "trades": trades}


def _build_curve_payload(curve_df: pd.DataFrame, recent_days: int) -> Dict[str, Any]:
    if curve_df.empty:
        return {"recent": [], "max_drawdown_episode": {}}

    curve = curve_df.copy()
    if "date" in curve.columns:
        curve["date"] = curve["date"].astype(str)

    if "cum_ret" not in curve.columns and "equity" in curve.columns and len(curve) > 0:
        initial = float(curve["equity"].iloc[0]) if float(curve["equity"].iloc[0]) > 0 else 1.0
        curve["cum_ret"] = curve["equity"] / initial - 1.0
    if "drawdown" not in curve.columns and "equity" in curve.columns:
        curve["cum_max"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["cum_max"] - 1.0

    max_dd_episode: Dict[str, Any] = {}
    if "drawdown" in curve.columns and "equity" in curve.columns and len(curve) > 0:
        trough_idx = int(curve["drawdown"].astype(float).idxmin())
        peak_idx = int(curve.loc[:trough_idx, "equity"].astype(float).idxmax())
        peak_row = curve.iloc[peak_idx]
        trough_row = curve.iloc[trough_idx]
        max_dd_episode = {
            "peak_date": str(peak_row.get("date", "")),
            "trough_date": str(trough_row.get("date", "")),
            "max_drawdown": _to_float(trough_row.get("drawdown")),
            "peak_equity": _to_float(peak_row.get("equity")),
            "trough_equity": _to_float(trough_row.get("equity")),
        }

    recent = curve.tail(max(1, recent_days))
    keep_cols = [c for c in ["date", "equity", "cum_ret", "drawdown"] if c in recent.columns]
    recent_payload = [_sanitize(row) for row in recent[keep_cols].to_dict(orient="records")]
    return {"recent": recent_payload, "max_drawdown_episode": _sanitize(max_dd_episode)}


def _load_benchmark_index_daily(
    recent_dates: List[str], benchmark_code: str = "000300.SH"
) -> Dict[str, Any]:
    if not recent_dates:
        return {"code": benchmark_code, "name": "", "daily": []}

    normalized_dates = [str(d or "") for d in recent_dates if str(d or "").strip()]
    if not normalized_dates:
        return {"code": benchmark_code, "name": "", "daily": []}

    start_date = min(normalized_dates)
    end_date = max(normalized_dates)
    sql = text(
        """
        SELECT s.name, k.trade_date AS date, k.open, k.high, k.low, k.close
        FROM kline_daily k
        LEFT JOIN stocks s ON s.code = k.code
        WHERE k.code = :code
          AND k.trade_date >= :start_date
          AND k.trade_date <= :end_date
        ORDER BY k.trade_date
        """
    )
    try:
        df = pd.read_sql(
            sql,
            db.engine,
            params={"code": benchmark_code, "start_date": start_date, "end_date": end_date},
        )
    except Exception as exc:
        logger.warning(f"load benchmark index daily failed: {benchmark_code}, {exc}")
        return {"code": benchmark_code, "name": "", "daily": []}

    if df.empty:
        return {"code": benchmark_code, "name": "", "daily": []}

    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "open", "high", "low", "close"]).copy()
    if df.empty:
        return {"code": benchmark_code, "name": "", "daily": []}

    daily = [_sanitize(row) for row in df[["date", "open", "high", "low", "close"]].to_dict(orient="records")]
    name = str(df["name"].dropna().iloc[0]) if "name" in df.columns and df["name"].dropna().shape[0] else ""
    return {"code": benchmark_code, "name": name, "daily": daily}


def _build_regime_timeline(decisions_df: pd.DataFrame, recent_dates: List[str]) -> Dict[str, Any]:
    if decisions_df.empty or not recent_dates:
        return {"daily": [], "segments": []}

    date_list = [str(d or "") for d in recent_dates if str(d or "").strip()]
    if not date_list:
        return {"daily": [], "segments": []}

    decisions = decisions_df.copy()
    trade_col = "trade_date" if "trade_date" in decisions.columns else "date"
    if trade_col not in decisions.columns or "mode" not in decisions.columns:
        return {"daily": [], "segments": []}

    decisions[trade_col] = pd.to_datetime(decisions[trade_col], errors="coerce").dt.strftime("%Y-%m-%d")
    decisions["mode"] = decisions["mode"].astype(str).str.lower()
    decisions = decisions.dropna(subset=[trade_col, "mode"]).copy()
    if decisions.empty:
        return {"daily": [], "segments": []}

    mode_map = (
        decisions.sort_values(trade_col)
        .drop_duplicates(subset=[trade_col], keep="last")
        .set_index(trade_col)["mode"]
        .to_dict()
    )
    daily = [{"date": date_str, "mode": mode_map.get(date_str)} for date_str in date_list if mode_map.get(date_str)]
    if not daily:
        return {"daily": [], "segments": []}

    segments: List[Dict[str, Any]] = []
    current_mode = daily[0]["mode"]
    start_idx = 0
    for idx in range(1, len(daily)):
        mode = daily[idx]["mode"]
        if mode != current_mode:
            segments.append(
                {
                    "mode": current_mode,
                    "start_date": daily[start_idx]["date"],
                    "end_date": daily[idx - 1]["date"],
                    "start_index": start_idx,
                    "end_index": idx - 1,
                }
            )
            current_mode = mode
            start_idx = idx
    segments.append(
        {
            "mode": current_mode,
            "start_date": daily[start_idx]["date"],
            "end_date": daily[-1]["date"],
            "start_index": start_idx,
            "end_index": len(daily) - 1,
        }
    )
    return {"daily": _sanitize(daily), "segments": _sanitize(segments)}


def _build_annual_returns(curve_df: pd.DataFrame) -> List[Dict[str, Any]]:
    if curve_df.empty or "date" not in curve_df.columns or "equity" not in curve_df.columns:
        return []
    curve = curve_df.copy()
    curve["trade_date"] = pd.to_datetime(curve["date"], errors="coerce")
    curve = curve.dropna(subset=["trade_date"])
    if curve.empty:
        return []
    curve["year"] = curve["trade_date"].dt.year
    rows: List[Dict[str, Any]] = []
    for year, g in curve.groupby("year", sort=True):
        g = g.sort_values("trade_date")
        start_equity = float(g["equity"].iloc[0])
        end_equity = float(g["equity"].iloc[-1])
        yearly_ret = end_equity / start_equity - 1 if start_equity > 0 else None
        rows.append(
            {
                "year": int(year),
                "start_date": str(g["date"].iloc[0]),
                "end_date": str(g["date"].iloc[-1]),
                "start_equity": start_equity,
                "end_equity": end_equity,
                "return": yearly_ret,
            }
        )
    return _sanitize(rows)


def _build_drawdown_episodes(curve_df: pd.DataFrame, top_n: int = 20) -> List[Dict[str, Any]]:
    if curve_df.empty or "date" not in curve_df.columns or "equity" not in curve_df.columns:
        return []

    curve = curve_df.copy()
    curve["date"] = curve["date"].astype(str)
    curve["equity"] = curve["equity"].astype(float)

    episodes: List[Dict[str, Any]] = []
    peak_equity = float(curve["equity"].iloc[0])
    peak_date = str(curve["date"].iloc[0])
    in_dd = False
    episode_peak_date = peak_date
    trough_date = peak_date
    trough_dd = 0.0

    for _, row in curve.iterrows():
        date = str(row["date"])
        equity = float(row["equity"])

        if equity >= peak_equity:
            if in_dd:
                episodes.append(
                    {
                        "start_date": episode_peak_date,
                        "trough_date": trough_date,
                        "recovery_date": date,
                        "max_drawdown": trough_dd,
                    }
                )
                in_dd = False
            peak_equity = equity
            peak_date = date
            continue

        dd = equity / peak_equity - 1.0
        if not in_dd:
            in_dd = True
            episode_peak_date = peak_date
            trough_date = date
            trough_dd = dd
        elif dd < trough_dd:
            trough_dd = dd
            trough_date = date

    if in_dd:
        episodes.append(
            {
                "start_date": episode_peak_date,
                "trough_date": trough_date,
                "recovery_date": None,
                "max_drawdown": trough_dd,
            }
        )

    episodes = sorted(episodes, key=lambda x: abs(float(x["max_drawdown"])), reverse=True)[:top_n]
    return _sanitize(episodes)


def _reason_to_event_note(reason: str, side: str) -> str:
    reason_key = (reason or "").strip().lower()
    reason_map = {
        "target_entry": "目标入场：满足进场条件",
        "stop_loss": "目标出场：止损触发",
        "take_profit": "目标出场：止盈触发",
        "take_profit_half": "目标出场：减仓后继续观察",
        "trailing_stop": "目标出场：追踪止损触发",
        "trend_break": "目标出场：趋势转弱（下行偏离）",
        "score_drop": "目标出场：评分持续下滑",
        "portfolio_cut": "目标出场：风控裁员/仓位调整",
    }
    if reason_key in reason_map:
        return reason_map[reason_key]
    if side == "BUY":
        return f"目标入场：{reason or '执行入场信号'}"
    if side == "SELL":
        return f"目标出场：{reason or '执行出场信号'}"
    return reason or ""


def _v42_exec_clock(strategy_version: str, side: str) -> Optional[str]:
    version = str(strategy_version or "").lower()
    side_u = str(side or "").upper()
    if not version.startswith("v4.2"):
        return None
    if side_u == "SELL":
        return "09:30:00"
    if side_u == "BUY":
        if version.endswith("_30m"):
            return "10:00:00"
        return "09:45:00"
    return None


def _with_date_time(trade_date: str, clock: Optional[str]) -> Optional[str]:
    d = str(trade_date or "").strip()
    if not d:
        return None
    c = str(clock or "").strip()
    if not c:
        return d
    if len(c) == 5:
        c = f"{c}:00"
    return f"{d} {c}"


def _pick_row_trade_time(row: Dict[str, Any], trade_date: str, side: str, strategy_version: str) -> Optional[str]:
    time_candidates = ("trade_time", "datetime", "executed_at", "filled_at", "time")
    for key in time_candidates:
        val = row.get(key)
        if val is None:
            continue
        s = str(val).strip()
        if not s or s.lower() in {"nan", "nat", "none"}:
            continue
        s = s.replace("T", " ")
        if ":" in s and len(s) <= 8:
            return _with_date_time(trade_date, s)
        return s
    return _with_date_time(trade_date, _v42_exec_clock(strategy_version, side))


def _build_historical_trades(
    trades_df: pd.DataFrame,
    decisions_df: pd.DataFrame,
    strategy_version: str = "v4",
    max_rows: int = 300,
) -> List[Dict[str, Any]]:
    if trades_df.empty or "date" not in trades_df.columns:
        return []

    dec_map: Dict[str, Dict[str, Any]] = {}
    if not decisions_df.empty and "trade_date" in decisions_df.columns:
        decision_cols = [
            c
            for c in [
                "trade_date",
                "mode",
                "breadth",
                "signal_drawdown",
                "target_codes",
                "buy_candidates",
                "sell_plan_codes",
                "sold_today",
                "hard_breach",
            ]
            if c in decisions_df.columns
        ]
        tmp_dec = decisions_df[decision_cols].copy()
        tmp_dec["trade_date"] = tmp_dec["trade_date"].astype(str)
        for _, row in tmp_dec.iterrows():
            row_dict = row.to_dict()
            dec_map[str(row_dict.get("trade_date"))] = row_dict

    tmp = trades_df.copy()
    tmp["date"] = tmp["date"].astype(str)
    if "code" in tmp.columns:
        tmp["code"] = tmp["code"].astype(str)
    if "side" in tmp.columns:
        tmp["side"] = tmp["side"].astype(str).str.upper()
        tmp["side_order"] = tmp["side"].map({"SELL": 0, "BUY": 1}).fillna(9)
    else:
        tmp["side"] = ""
        tmp["side_order"] = 9
    tmp = tmp.sort_values(["date", "side_order", "code"], ascending=[True, True, True]).reset_index(drop=True)
    tmp["row_id"] = np.arange(len(tmp))

    lots_by_code: Dict[str, List[Dict[str, Any]]] = {}
    buy_row_meta: Dict[int, Dict[str, Any]] = {}
    sell_row_meta: Dict[int, Dict[str, Any]] = {}

    for row in tmp.to_dict(orient="records"):
        row_id = int(row["row_id"])
        code = str(row.get("code", ""))
        trade_date = str(row.get("date", ""))
        side = str(row.get("side", "")).upper()
        shares = _to_int(row.get("shares"), 0)
        if not code or shares <= 0:
            continue

        code_lots = lots_by_code.setdefault(code, [])
        if side == "BUY":
            code_lots.append({"row_id": row_id, "buy_date": trade_date, "remain": shares})
            buy_row_meta[row_id] = {
                "buy_time": _with_date_time(trade_date, _v42_exec_clock(strategy_version, "BUY")),
                "sell_time": None,
            }
            continue

        if side == "SELL":
            remaining = shares
            matched_buy_dates: List[str] = []
            while remaining > 0 and code_lots:
                lot = code_lots[0]
                consume = min(int(lot["remain"]), remaining)
                lot["remain"] = int(lot["remain"]) - consume
                remaining -= consume
                matched_buy_dates.append(str(lot["buy_date"]))
                buy_row_id = int(lot["row_id"])
                if buy_row_id not in buy_row_meta:
                    buy_row_meta[buy_row_id] = {
                        "buy_time": _with_date_time(str(lot["buy_date"]), _v42_exec_clock(strategy_version, "BUY")),
                        "sell_time": None,
                    }
                # Use latest covered sell date as the (estimated) exit date for that buy lot.
                buy_row_meta[buy_row_id]["sell_time"] = _with_date_time(
                    trade_date,
                    _v42_exec_clock(strategy_version, "SELL"),
                )
                if int(lot["remain"]) <= 0:
                    code_lots.pop(0)

            unique_dates = list(dict.fromkeys(matched_buy_dates))
            sell_row_meta[row_id] = {
                "buy_time": (
                    "|".join([_with_date_time(d, _v42_exec_clock(strategy_version, "BUY")) or d for d in unique_dates])
                    if unique_dates
                    else None
                ),
                "sell_time": _with_date_time(trade_date, _v42_exec_clock(strategy_version, "SELL")),
            }

    result_rows: List[Dict[str, Any]] = []
    for row in tmp.to_dict(orient="records"):
        row_id = int(row["row_id"])
        trade_date = str(row.get("date", ""))
        side = str(row.get("side", "")).upper()
        reason = str(row.get("reason", "") or "")
        day_decision = dec_map.get(trade_date, {})
        event_note = _reason_to_event_note(reason=reason, side=side)

        buy_time = None
        sell_time = None
        if side == "BUY":
            buy_time = (buy_row_meta.get(row_id) or {}).get("buy_time")
            sell_time = (buy_row_meta.get(row_id) or {}).get("sell_time")
        elif side == "SELL":
            buy_time = (sell_row_meta.get(row_id) or {}).get("buy_time")
            sell_time = (sell_row_meta.get(row_id) or {}).get("sell_time")
        trade_time = _pick_row_trade_time(
            row=row,
            trade_date=trade_date,
            side=side,
            strategy_version=strategy_version,
        )

        result_rows.append(
            {
                "date": trade_date,
                "trade_time": trade_time,
                "signal_date": row.get("signal_date"),
                "side": side,
                "code": row.get("code"),
                "name": row.get("name"),
                "shares": row.get("shares"),
                "price": row.get("price"),
                "reason": reason,
                "event_note": event_note,
                "buy_time": buy_time,
                "sell_time": sell_time,
                "realized_pnl": row.get("realized_pnl"),
                "gross": row.get("gross"),
                "fee": row.get("fee"),
                "mode": day_decision.get("mode"),
                "breadth": day_decision.get("breadth"),
                "signal_drawdown": day_decision.get("signal_drawdown"),
                "target_codes": day_decision.get("target_codes"),
                "buy_candidates": day_decision.get("buy_candidates"),
                "sell_plan_codes": day_decision.get("sell_plan_codes"),
                "sold_today": day_decision.get("sold_today"),
            }
        )

    result_rows = sorted(
        result_rows,
        key=lambda x: (str(x.get("date") or ""), 1 if str(x.get("side") or "") == "SELL" else 0, str(x.get("code") or "")),
        reverse=True,
    )[: max(1, max_rows)]
    return _sanitize(result_rows)


def _rebuild_open_positions_from_trades(trades_df: pd.DataFrame, target_date: str) -> Dict[str, Dict[str, Any]]:
    if trades_df.empty or "date" not in trades_df.columns:
        return {}

    data = trades_df.copy()
    data = data[data["date"].astype(str) <= target_date]
    if data.empty:
        return {}

    side_order = {"SELL": 0, "BUY": 1}
    data["side_order"] = data["side"].map(side_order).fillna(9)
    data = data.sort_values(["date", "side_order"]).reset_index(drop=True)

    pos: Dict[str, Dict[str, Any]] = {}
    for _, row in data.iterrows():
        code = str(row.get("code", ""))
        if not code:
            continue
        side = str(row.get("side", "")).upper()
        shares = _to_int(row.get("shares"), 0)
        price = _to_float(row.get("price"))
        name = row.get("name")
        if shares <= 0:
            continue

        state = pos.get(code, {"shares": 0, "avg_cost": None, "name": name})
        if side == "BUY":
            if price is None:
                continue
            old_shares = int(state["shares"])
            old_cost = _to_float(state.get("avg_cost")) or 0.0
            new_shares = old_shares + shares
            if new_shares <= 0:
                continue
            state["avg_cost"] = (old_cost * old_shares + float(price) * shares) / new_shares
            state["shares"] = new_shares
            state["name"] = name or state.get("name")
            pos[code] = state
        elif side == "SELL":
            old_shares = int(state["shares"])
            remain = max(0, old_shares - shares)
            state["shares"] = remain
            if remain == 0:
                state["avg_cost"] = None
            pos[code] = state

    return {code: item for code, item in pos.items() if int(item.get("shares", 0)) > 0}


def _build_strategy_logic(strategy_version: str = "v4") -> Dict[str, List[str]]:
    version = str(strategy_version or "").lower()
    if version.startswith("v5"):
        return {
            "universe": [
                "A股主流市场：日均成交额 >= 2亿元，且近20日波动率变化不低于2%。",
                "盘中关注：20分钟均量大于MA10和MA20，且20天振幅较昨日明显放大。",
                "目标：14:30 前完成基础样本清洗，并在14:45后仅执行一次高可信入场扫描。",
            ],
            "selection_scoring": [
                "核心评分：V5风格，优先使用动量+量能组合；对齐盘前与盘中条件。",
                "14:30-14:45 进行候选清理，关注是否已触发入场信号；若未触发则进入观察。",
            ],
            "buy_rules": [
                "盘前检查入场信号：只要 mode != off、freeze_left=0 且未持仓限制。",
                "14:50 后继续做一次入场评估；若出现错误信号则忽略。",
                "禁止无节奏追单：若连续触发过度且与风控不一致，则终止当次新增。",
            ],
            "sell_rules": [
                "V4 及更高版本默认采用：触发止损、趋势转弱与波动异常时执行止盈退出。",
                "若核心 rank 连续回撤且低于 score_rank 门槛，则按分数剔除。",
                "盘中若触发风控规则，立即退出并更新仓位状态。",
                "中期异常触发时，可进入观察窗口，不立即强平。",
            ],
            "risk_control": [
                "风控底线：单标仓位回撤 > -8% 时触发保护动作。",
                "停用恢复：触发风控后进入观察并延迟 1 条15m周期后再复评。",
                "风控类型：edge-triggered 与 time-gated 混合，避免重复触发。",
                "收盘复核：若未持有有效信号，清理异常状态并记录。",
            ],
        }

    is_v42 = version.startswith("v4.2")
    buy_rules = [
        "执行T+1：等待 signal 接收确认后再执行 trade。",
        "仅在盘前窗口允许入场，确认后在15m/30m链路继续验证。",
        f"入场最低分数门槛: {DEFAULT_LIVE_ENTRY_MIN_SCORE if 'DEFAULT_LIVE_ENTRY_MIN_SCORE' in globals() else 0.0}",
        f"入场强度约束：score_rank > 0 且 score_rank <= {DEFAULT_KEEP_RANK_MULT if 'DEFAULT_KEEP_RANK_MULT' in globals() else 0}",
    ]
    risk_control = [
        "节奏规则：on / neutral / off 与 breadth 阈值联动，默认优先 on。",
        "回撤触发后 edge-triggered 立刻处理，避免重复触发。",
        "恢复逻辑：遵循 recovery_days / recovery_max_holdings，先减仓再复评。",
        "结束条件：仓位达到阈值或持续无效时自动退出观察。",
    ]
    if is_v42:
        buy_rules[0] = "T+1 + intraday confirm：先确认信号，再在15m/30m上验证后交易。"
        buy_rules.append("盘中入场需满足 freeze_left=0 且未触发保护冻结。")
        risk_control.append("v4.2：出场优先于再次入场，执行顺序固定。")

    return {
        "universe": [
            "A股主流市场：日均成交额 >= 2亿元，且近20日回撤不低于2%。",
            "趋势约束：价格站上A10并低波动区域。",
            "情绪阈值：breadth在 [0.008, 0.09] 区间。",
        ],
        "selection_scoring": [
            "评分公式: 0.35*mom5 + 0.20*mom10 + 0.20*strength + 0.20*volume_ratio + 0.05*momentum10_low。",
            "筛选方式：按 on/neutral/off 模式决定买卖强度与排序。",
        ],
        "buy_rules": buy_rules,
        "sell_rules": [
            "止损触发：close <= avg_cost * (1 - stop_loss)。",
            "止盈触发：若收益率超过 take_profit 目标，分批止盈。",
            "趋势反转时执行 trailing stop，若连续跌破 A10 则加大回撤控制。",
            "低成交量且持仓时间过短可提前减仓。",
            "评分显著下跌且 low rank 小于阈值时剔除。",
            "无效信号超过阈值时进行 portfolio_cut 并记录。",
            "同一股票在同一日重复触发时按优先级仅执行一次。",
        ],
        "risk_control": risk_control,
    }
def _build_live_checks(
    decision: Dict[str, Any],
    holding: Dict[str, Any],
    params: Dict[str, Any],
) -> List[Dict[str, Any]]:
    mode = str(decision.get("mode", "")).lower()
    freeze_left = _to_int(holding.get("freeze_left"), 0)
    recovery_left = _to_int(holding.get("recovery_left"), _to_int(decision.get("recovery_left"), 0))
    recovery_max_holdings = max(
        1,
        _to_int(
            decision.get("recovery_max_holdings"),
            _to_int(params.get("recovery_max_holdings"), DEFAULT_LIVE_RECOVERY_MAX_HOLDINGS),
        ),
    )
    current_holdings_count = len(_parse_holdings_desc(holding.get("holdings")))

    buy_candidates = _split_codes(decision.get("buy_candidates"))
    target_pool = _split_codes(decision.get("target_codes"))
    signal_dd = _to_float(decision.get("signal_drawdown"))
    breadth = _to_float(decision.get("breadth"))
    soft_dd = _to_float(params.get("soft_dd"))
    hard_dd = _to_float(params.get("hard_dd"))
    breadth_low = _to_float(params.get("breadth_low"))
    breadth_high = _to_float(params.get("breadth_high"))

    hard_breach = bool(signal_dd <= hard_dd) if signal_dd is not None and hard_dd is not None else False
    recovery_active = bool(freeze_left == 0 and recovery_left > 0 and not hard_breach)

    recovery_cap_pass = True
    recovery_cap_reason = "未进入恢复状态，默认通过仓位上限检查。"
    if recovery_active:
        recovery_cap_pass = current_holdings_count <= recovery_max_holdings
        recovery_cap_reason = (
            f"恢复期仓位上限={recovery_max_holdings}，当前持仓={current_holdings_count}" if recovery_cap_pass else f"恢复期仓位超限={current_holdings_count} > 上限={recovery_max_holdings}"
        )

    checks = [
        {
            "key": "mode_allows_new_position",
            "label": "模式允许新仓位",
            "pass": mode in {"on", "neutral"},
            "value": mode,
            "reason": "mode=off 时不允许新增",
        },
        {
            "key": "freeze_cleared",
            "label": "观察/冻结状态",
            "pass": freeze_left == 0,
            "value": freeze_left,
            "reason": "freeze_left=0 时可进入恢复逻辑并新开仓位",
        },
        {
            "key": "target_pool_present",
            "label": "目标池候选池存在",
            "pass": len(target_pool) > 0,
            "value": len(target_pool),
            "reason": "目标池为空则无标准入场名单",
        },
        {
            "key": "buy_candidates_present",
            "label": "本次买入候选存在",
            "pass": len(buy_candidates) > 0,
            "value": len(buy_candidates),
            "reason": "当前无候选时不做新买入",
        },
    ]

    if signal_dd is not None and soft_dd is not None:
        checks.append(
            {
                "key": "drawdown_soft_guard",
                "label": "持仓回撤软阈值",
                "pass": signal_dd > soft_dd,
                "value": signal_dd,
                "reason": f"soft_dd={soft_dd}",
            }
        )
    if signal_dd is not None and hard_dd is not None:
        checks.append(
            {
                "key": "drawdown_hard_guard",
                "label": "持仓回撤硬阈值",
                "pass": signal_dd > hard_dd,
                "value": signal_dd,
                "reason": f"hard_dd={hard_dd}",
            }
        )
    if breadth is not None:
        checks.append(
            {
                "key": "breadth_context",
                "label": "市场宽度背景",
                "pass": True,
                "value": breadth,
                "reason": f"breadth_low={breadth_low}, breadth_high={breadth_high}",
            }
        )

    checks.append(
        {
            "key": "recovery_position_cap",
            "label": "恢复期仓位上限控制",
            "pass": recovery_cap_pass,
            "value": f"持仓{current_holdings_count}/{recovery_max_holdings}，recovery_left={recovery_left}",
            "reason": recovery_cap_reason,
        }
    )

    return _sanitize(checks)



def _build_holdings_strategy(
    snapshot_holding: Dict[str, Any],
    trades_df: pd.DataFrame,
    target_date: str,
    params: Dict[str, Any],
) -> List[Dict[str, Any]]:
    holding_rows = _parse_holdings_desc(snapshot_holding.get("holdings"))
    if not holding_rows:
        return []

    open_positions = _rebuild_open_positions_from_trades(trades_df=trades_df, target_date=target_date)
    stop_loss = _to_float(params.get("stop_loss"))
    take_profit = _to_float(params.get("take_profit"))
    trailing = _to_float(params.get("trailing"))

    result: List[Dict[str, Any]] = []
    for item in holding_rows:
        code = item["code"]
        current_price = float(item["current_price"])
        shares = int(item["shares"])
        pos = open_positions.get(code, {})
        avg_cost = _to_float(pos.get("avg_cost"))
        name = pos.get("name")

        stop_loss_price = None
        take_profit_price = None
        stop_hit = None
        tp_reached = None
        if avg_cost is not None and stop_loss is not None:
            stop_loss_price = avg_cost * (1.0 - stop_loss)
            stop_hit = current_price <= stop_loss_price
        if avg_cost is not None and take_profit is not None:
            take_profit_price = avg_cost * (1.0 + take_profit)
            tp_reached = current_price >= take_profit_price

        result.append(
            {
                "code": code,
                "name": name,
                "shares": shares,
                "current_price": current_price,
                "avg_cost_est": avg_cost,
                "stop_loss_price_est": stop_loss_price,
                "take_profit_price_est": take_profit_price,
                "trailing_ratio": trailing,
                "stop_loss_triggered": stop_hit,
                "take_profit_reached": tp_reached,
                "notes": [
                    "持仓纪律：若 close < MA10 且仍在回撤中则考虑止损。",
                    "评分纪律：分数持续下滑触发 score_drop。",
                    "趋势纪律：触发 trailing 或回撤异常则启动减仓。",
                ],
            }
        )
    return _sanitize(result)


def _set_gen2_shadow_update_task(task_id: str, payload: Dict[str, Any]) -> None:
    with GEN2_SHADOW_UPDATE_TASK_LOCK:
        task = GEN2_SHADOW_UPDATE_TASKS.setdefault(task_id, {})
        task.update(payload)
        task["updated_at"] = datetime.now().isoformat(timespec="seconds")


def _get_gen2_shadow_update_task(task_id: str) -> Optional[Dict[str, Any]]:
    with GEN2_SHADOW_UPDATE_TASK_LOCK:
        task = GEN2_SHADOW_UPDATE_TASKS.get(task_id)
        return dict(task) if task else None


def _set_gen2_backtest_update_task(task_id: str, payload: Dict[str, Any]) -> None:
    with GEN2_BACKTEST_UPDATE_TASK_LOCK:
        task = GEN2_BACKTEST_UPDATE_TASKS.setdefault(task_id, {})
        task.update(payload)
        task["updated_at"] = datetime.now().isoformat(timespec="seconds")


def _get_gen2_backtest_update_task(task_id: str) -> Optional[Dict[str, Any]]:
    with GEN2_BACKTEST_UPDATE_TASK_LOCK:
        task = GEN2_BACKTEST_UPDATE_TASKS.get(task_id)
        return dict(task) if task else None


def _set_gen2_mainline_update_task(task_id: str, payload: Dict[str, Any]) -> None:
    with GEN2_MAINLINE_UPDATE_TASK_LOCK:
        task = GEN2_MAINLINE_UPDATE_TASKS.setdefault(task_id, {})
        task.update(payload)


def _get_gen2_mainline_update_task(task_id: str) -> Optional[Dict[str, Any]]:
    with GEN2_MAINLINE_UPDATE_TASK_LOCK:
        task = GEN2_MAINLINE_UPDATE_TASKS.get(task_id)
        return dict(task) if task else None


def _get_active_gen2_mainline_update_task() -> Optional[Dict[str, Any]]:
    with GEN2_MAINLINE_UPDATE_TASK_LOCK:
        for task in GEN2_MAINLINE_UPDATE_TASKS.values():
            if task.get("status") in {"queued", "running"}:
                return dict(task)
    return None


def _run_gen2_mainline_script_step(
    step_name: str,
    script_name: str,
    args: List[str],
) -> Dict[str, Any]:
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / script_name), *args]
    started_at = datetime.now()
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
        cwd=str(REPO_ROOT),
    )
    finished_at = datetime.now()
    ok = proc.returncode == 0
    return {
        "name": step_name,
        "script": script_name,
        "cmd": cmd,
        "ok": ok,
        "status": "completed" if ok else "failed",
        "returncode": int(proc.returncode),
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": finished_at.isoformat(timespec="seconds"),
        "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
        "stdout_tail": "\n".join((proc.stdout or "").splitlines()[-40:]),
        "stderr_tail": "\n".join((proc.stderr or "").splitlines()[-40:]),
    }


def _gen2_intraday_cutoff_status(table: str, target_date: str, min_codes: int = 3000) -> Dict[str, Any]:
    normalized_date = _normalize_date_str(target_date)
    if not normalized_date:
        return {"ok": False, "table": table, "target_date": target_date, "error": "invalid target_date"}
    try:
        df = clickhouse_query_df(
            f"""
            SELECT
                datetime AS dt,
                uniq(code) AS codes
            FROM {table}
            WHERE toDate(datetime) = toDate(?)
            GROUP BY dt
            ORDER BY dt DESC
            LIMIT 12
            """,
            [normalized_date],
        )
    except Exception as exc:
        return {"ok": False, "table": table, "target_date": normalized_date, "error": str(exc)}
    if df is None or df.empty:
        return {
            "ok": False,
            "table": table,
            "target_date": normalized_date,
            "latest_cutoff": None,
            "latest_codes": 0,
            "min_codes": int(min_codes),
            "reason": "no_intraday_rows_for_target_date",
        }
    work = df.copy()
    work["codes"] = pd.to_numeric(work.get("codes"), errors="coerce").fillna(0).astype(int)
    latest = work.iloc[0]
    complete = work[work["codes"] >= int(min_codes)]
    latest_cutoff = pd.to_datetime(latest.get("dt"), errors="coerce")
    complete_cutoff = pd.to_datetime(complete.iloc[0].get("dt"), errors="coerce") if not complete.empty else pd.NaT
    return {
        "ok": not complete.empty,
        "table": table,
        "target_date": normalized_date,
        "latest_cutoff": None if pd.isna(latest_cutoff) else latest_cutoff.strftime("%Y-%m-%d %H:%M:%S"),
        "latest_codes": int(latest.get("codes") or 0),
        "complete_cutoff": None if pd.isna(complete_cutoff) else complete_cutoff.strftime("%Y-%m-%d %H:%M:%S"),
        "complete_codes": 0 if complete.empty else int(complete.iloc[0].get("codes") or 0),
        "min_codes": int(min_codes),
        "recent_slots": [
            {
                "dt": pd.to_datetime(row.dt, errors="coerce").strftime("%Y-%m-%d %H:%M:%S"),
                "codes": int(row.codes or 0),
            }
            for row in work.itertuples(index=False)
            if not pd.isna(pd.to_datetime(row.dt, errors="coerce"))
        ],
    }


def _run_gen2_intraday_minute_repair(target_date: str, periods: Optional[List[str]] = None, timeout_seconds: int = 1800) -> Dict[str, Any]:
    normalized_date = _normalize_date_str(target_date)
    if not normalized_date:
        return {"ok": False, "target_date": target_date, "error": "invalid target_date"}
    period_list = [str(p).strip() for p in (periods or ["15m", "30m"]) if str(p).strip()]
    argv = [
        "sync_intraday_minutes_fast.py",
        "--target-date",
        normalized_date,
        "--types",
        "stock,index",
        "--periods",
        ",".join(period_list),
        "--batch-size",
        "500",
        "--min-complete-codes",
        "3000",
    ]
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "sync_intraday_minutes_fast.py"), *argv[1:]]
    started_at = datetime.now()
    try:
        import contextlib
        import io

        import importlib
        import scripts.sync_intraday_minutes_fast as sync_intraday_minutes_fast

        sync_intraday_minutes_fast = importlib.reload(sync_intraday_minutes_fast)
        sync_intraday_minutes_fast_main = sync_intraday_minutes_fast.main

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        old_argv = sys.argv[:]
        try:
            sys.argv = argv
            with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
                returncode = int(sync_intraday_minutes_fast_main() or 0)
        finally:
            sys.argv = old_argv
        finished_at = datetime.now()
        return {
            "ok": returncode == 0,
            "status": "completed" if returncode == 0 else "failed",
            "target_date": normalized_date,
            "periods": period_list,
            "returncode": int(returncode),
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
            "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
            "cmd": cmd,
            "mode": "in_process",
            "stdout_tail": "\n".join(stdout_buf.getvalue().splitlines()[-80:]),
            "stderr_tail": "\n".join(stderr_buf.getvalue().splitlines()[-80:]),
        }
    except SystemExit as exc:
        finished_at = datetime.now()
        returncode = int(exc.code or 0) if isinstance(exc.code, int) else 1
        return {
            "ok": returncode == 0,
            "status": "completed" if returncode == 0 else "failed",
            "target_date": normalized_date,
            "periods": period_list,
            "returncode": returncode,
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
            "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
            "cmd": cmd,
            "mode": "in_process",
            "stdout_tail": "",
            "stderr_tail": str(exc),
        }
    except Exception as inproc_exc:
        logger.warning(f"G2 intraday minute in-process repair failed, fallback subprocess: {inproc_exc}")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(300, int(timeout_seconds or 1800)),
            cwd=str(REPO_ROOT),
        )
    except subprocess.TimeoutExpired as exc:
        finished_at = datetime.now()
        return {
            "ok": False,
            "status": "timeout",
            "target_date": normalized_date,
            "periods": period_list,
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
            "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
            "cmd": cmd,
            "stdout_tail": "\n".join((exc.stdout or "").splitlines()[-40:]),
            "stderr_tail": "\n".join((exc.stderr or "").splitlines()[-40:]),
        }
    finished_at = datetime.now()
    return {
        "ok": proc.returncode == 0,
        "status": "completed" if proc.returncode == 0 else "failed",
        "target_date": normalized_date,
        "periods": period_list,
        "returncode": int(proc.returncode),
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": finished_at.isoformat(timespec="seconds"),
        "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
        "cmd": cmd,
        "stdout_tail": "\n".join((proc.stdout or "").splitlines()[-60:]),
        "stderr_tail": "\n".join((proc.stderr or "").splitlines()[-60:]),
    }


def _ensure_gen2_mainline_intraday_minutes(target_date: str, min_codes: int = 3000) -> Dict[str, Any]:
    before = _gen2_intraday_cutoff_status("kline_minute_30", target_date, min_codes=min_codes)
    if before.get("ok"):
        return {"ok": True, "attempted_repair": False, "before": before, "after": before}
    repair = _run_gen2_intraday_minute_repair(target_date, periods=["15m", "30m"])
    after = _gen2_intraday_cutoff_status("kline_minute_30", target_date, min_codes=min_codes)
    return {
        "ok": bool(after.get("ok")),
        "attempted_repair": True,
        "before": before,
        "repair": repair,
        "after": after,
        "error": None if after.get("ok") else (repair.get("stderr_tail") or repair.get("stdout_tail") or after.get("reason") or after.get("error")),
    }


def _run_gen2_mainline_update_task(task_id: str, target_date: Optional[str], limit: int, mode: str = "all") -> None:
    selected_date = _normalize_date_str(target_date)
    normalized_mode = str(mode or "all").strip().lower()
    all_steps = [
        {
            "name": "sector_factor",
            "script": "generate_mainline_intraday_diffusion_factor.py",
            "args": (
                ["--table", "kline_minute_30", "--cutoff-time", "latest", "--target-date", selected_date]
                if selected_date
                else ["--table", "kline_minute_30", "--cutoff-time", "latest"]
            ),
            "optional": False,
        },
        {
            "name": "sector_overlay",
            "script": "generate_mainline_candidate_overlay.py",
            "args": ["--target-date", selected_date] if selected_date else [],
            "optional": True,
        },
        {
            "name": "sector_watchlist",
            "script": "build_mainline_sector_watchlist.py",
            "args": ["--trade-date", selected_date, "--limit", str(max(30, int(limit or 30)))] if selected_date else ["--limit", str(max(30, int(limit or 30)))],
            "optional": True,
        },
        {
            "name": "theme_clusters",
            "script": "generate_behavior_theme_clusters_v1.py",
            "args": ["--target-date", selected_date] if selected_date else [],
            "optional": True,
        },
        {
            "name": "theme_candidate_source",
            "script": "generate_behavior_theme_candidate_source_v1.py",
            "args": ["--target-date", selected_date, "--top-n", str(max(30, int(limit or 30)))] if selected_date else ["--top-n", str(max(30, int(limit or 30)))],
            "optional": True,
        },
        {
            "name": "theme_pool",
            "script": "generate_mainline_theme_observation_pool_v1.py",
            "args": ["--target-date", selected_date, "--top-n", str(max(30, int(limit or 30)))] if selected_date else ["--top-n", str(max(30, int(limit or 30)))],
            "optional": True,
        },
        {
            "name": "theme_overlay",
            "script": "generate_mainline_theme_strategy_overlay_v1.py",
            "args": ["--target-date", selected_date, "--top-n", str(max(30, int(limit or 30)))] if selected_date else ["--top-n", str(max(30, int(limit or 30)))],
            "optional": True,
        },
    ]
    if normalized_mode == "sector":
        steps = [step for step in all_steps if step["name"].startswith("sector")]
    elif normalized_mode == "theme":
        steps = [step for step in all_steps if step["name"].startswith("theme")]
    else:
        steps = list(all_steps)
    started_at = datetime.now()
    warnings_list: List[Dict[str, Any]] = []
    _set_gen2_mainline_update_task(
        task_id,
        {
            "task_id": task_id,
            "status": "running",
            "progress": 5,
            "target_date": selected_date,
            "limit": int(limit),
            "mode": normalized_mode,
            "started_at": started_at.isoformat(timespec="seconds"),
            "steps_total": len(steps),
            "steps_completed": 0,
            "steps": [],
            "current_step": steps[0]["name"] if steps else None,
            "message": "\u5f00\u59cb\u5237\u65b0\u7b2c\u4e8c\u4ee3\u4e3b\u7ebf\u70ed\u70b9\u4ea7\u7269",
        },
    )
    finished_steps: List[Dict[str, Any]] = []
    try:
        if selected_date and any(step["name"] == "sector_factor" for step in steps):
            _set_gen2_mainline_update_task(
                task_id,
                {
                    "current_step": "intraday_minute_precheck",
                    "progress": 5,
                    "message": "检查并修复G2主线所需的当日15m/30m分钟线",
                    "steps": finished_steps,
                },
            )
            minute_check = _ensure_gen2_mainline_intraday_minutes(selected_date)
            minute_step = {
                "name": "intraday_minute_precheck",
                "script": "sync_intraday_minutes_fast.py",
                "cmd": (minute_check.get("repair") or {}).get("cmd", []),
                "ok": bool(minute_check.get("ok")),
                "status": "completed" if minute_check.get("ok") else "failed",
                "optional": False,
                "returncode": (minute_check.get("repair") or {}).get("returncode"),
                "started_at": (minute_check.get("repair") or {}).get("started_at") or datetime.now().isoformat(timespec="seconds"),
                "finished_at": (minute_check.get("repair") or {}).get("finished_at") or datetime.now().isoformat(timespec="seconds"),
                "duration_seconds": (minute_check.get("repair") or {}).get("duration_seconds", 0.0),
                "stdout_tail": (minute_check.get("repair") or {}).get("stdout_tail", ""),
                "stderr_tail": (minute_check.get("repair") or {}).get("stderr_tail", ""),
                "data_status": minute_check,
            }
            finished_steps.append(minute_step)
            if not minute_step["ok"]:
                finished_at = datetime.now()
                _set_gen2_mainline_update_task(
                    task_id,
                    {
                        "status": "failed",
                        "progress": 100,
                        "finished_at": finished_at.isoformat(timespec="seconds"),
                        "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
                        "steps_completed": 0,
                        "steps": finished_steps,
                        "warnings": warnings_list,
                        "current_step": "intraday_minute_precheck",
                        "message": "主线热点刷新失败：当日30m分钟线缺失且自动修复未完成",
                        "error": minute_check.get("error") or "intraday minute data precheck failed",
                    },
                )
                return
        for idx, step in enumerate(steps, start=1):
            if step["name"] == "sector_overlay" and _latest_mainline_factor_count() <= 0:
                skipped = {
                    "name": step["name"],
                    "script": step["script"],
                    "cmd": [],
                    "ok": True,
                    "optional": True,
                    "status": "skipped",
                    "returncode": 0,
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                    "duration_seconds": 0.0,
                    "stdout_tail": "",
                    "stderr_tail": "",
                }
                finished_steps.append(skipped)
                _set_gen2_mainline_update_task(
                    task_id,
                    {
                        "steps": finished_steps,
                        "warnings": warnings_list,
                        "steps_completed": len([x for x in finished_steps if x.get("ok")]),
                        "message": "\u4e3b\u7ebf\u56e0\u5b50\u4e3a\u7a7a\uff0c\u5df2\u8df3\u8fc7\u677f\u5757\u5019\u9009\u53e0\u52a0",
                    },
                )
                continue
            _set_gen2_mainline_update_task(
                task_id,
                {
                    "current_step": step["name"],
                    "progress": min(95, 5 + int((idx - 1) / max(1, len(steps)) * 90)),
                    "message": f"\u6267\u884c {step['script']}",
                    "steps": finished_steps,
                },
            )
            result = _run_gen2_mainline_script_step(step["name"], step["script"], step["args"])
            result["optional"] = bool(step.get("optional"))
            finished_steps.append(result)
            if not result["ok"]:
                if bool(step.get("optional")):
                    result["status"] = "warning"
                    warning_item = {
                        "step": step["name"],
                        "script": step["script"],
                        "message": result.get("stderr_tail") or result.get("stdout_tail") or f"{step['script']} failed",
                    }
                    warnings_list.append(warning_item)
                    _set_gen2_mainline_update_task(
                        task_id,
                        {
                            "steps": finished_steps,
                            "warnings": warnings_list,
                            "steps_completed": len([x for x in finished_steps if x.get("ok")]),
                            "message": f"{step['script']} \u5931\u8d25\uff0c\u5df2\u6309\u53ef\u9009\u6b65\u9aa4\u8df3\u8fc7",
                        },
                    )
                    continue
                finished_at = datetime.now()
                _set_gen2_mainline_update_task(
                    task_id,
                    {
                        "status": "failed",
                        "progress": 100,
                        "finished_at": finished_at.isoformat(timespec="seconds"),
                        "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
                        "steps_completed": len(finished_steps) - 1,
                        "steps": finished_steps,
                        "warnings": warnings_list,
                        "current_step": step["name"],
                        "message": f"\u4e3b\u7ebf\u70ed\u70b9\u5237\u65b0\u5931\u8d25\uff0c\u5361\u5728 {step['script']}",
                        "error": result.get("stderr_tail") or result.get("stdout_tail") or f"{step['script']} failed",
                    },
                )
                return
        finished_at = datetime.now()
        _set_gen2_mainline_update_task(
            task_id,
            {
                "status": "completed",
                "progress": 100,
                "finished_at": finished_at.isoformat(timespec="seconds"),
                "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
                "steps_completed": len([x for x in finished_steps if x.get("ok")]),
                "steps": finished_steps,
                "warnings": warnings_list,
                "current_step": None,
                "message": "\u5f00\u59cb\u5237\u65b0\u7b2c\u4e8c\u4ee3\u4e3b\u7ebf\u70ed\u70b9\u4ea7\u7269" if not warnings_list else "\u4e3b\u7ebf\u70ed\u70b9\u5df2\u5b8c\u6210\uff0c\u90e8\u5206\u53ef\u9009\u6b65\u9aa4\u5df2\u8df3\u8fc7",
                "result_preview": build_gen2_mainline_hotspots(selected_date, min(int(limit or 30), 20)),
            },
        )
    except Exception as exc:
        finished_at = datetime.now()
        _set_gen2_mainline_update_task(
            task_id,
            {
                "status": "failed",
                "progress": 100,
                "finished_at": finished_at.isoformat(timespec="seconds"),
                "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
                "steps_completed": len(finished_steps),
                "steps": finished_steps,
                "warnings": warnings_list,
                "current_step": None,
                "message": "\u7b2c\u4e8c\u4ee3\u4e3b\u7ebf\u70ed\u70b9\u4ea7\u7269\u5237\u65b0\u5f02\u5e38\u7ec8\u6b62",
                "error": str(exc),
            },
        )


def _run_gen2_backtest_update_task(task_id: str, end_date: Optional[str] = None, legacy_330: bool = True) -> None:
    if legacy_330:
        cmd = [
            sys.executable,
            str(REPO_ROOT / "scripts" / "restore_g2_331_backtest_outputs.py"),
        ]
    else:
        cmd = [
            sys.executable,
            str(REPO_ROOT / "scripts" / "gen2_build_v2_complete_strategy.py"),
            "--replace-official",
            "--populate-sort-probe",
        ]
        if end_date:
            cmd.extend(["--end-date", end_date])
    started_at = datetime.now()
    _set_gen2_backtest_update_task(
        task_id,
        {
            "status": "running",
            "progress": 20,
            "command": " ".join(cmd),
            "started_at": started_at.isoformat(timespec="seconds"),
        },
    )
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
        )
        stdout_text = (proc.stdout or "").strip()
        stderr_text = (proc.stderr or "").strip()
        if proc.returncode != 0:
            raise RuntimeError((stderr_text or stdout_text or f"returncode={proc.returncode}")[-2000:])
        result_payload: Dict[str, Any] = {}
        if stdout_text:
            try:
                result_payload = json.loads(stdout_text[stdout_text.find("{") :])
            except Exception:
                result_payload = {"raw_stdout": stdout_text[-4000:]}
        _set_gen2_backtest_update_task(
            task_id,
            {
                "status": "completed",
                "progress": 100,
                "result": result_payload,
                "stdout_tail": stdout_text[-4000:],
                "stderr_tail": stderr_text[-4000:],
                "duration_seconds": round((datetime.now() - started_at).total_seconds(), 2),
                "completed_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
    except Exception as exc:
        logger.exception("G2 backtest update task failed")
        _set_gen2_backtest_update_task(
            task_id,
            {
                "status": "failed",
                "progress": 100,
                "error": str(exc),
                "duration_seconds": round((datetime.now() - started_at).total_seconds(), 2),
                "completed_at": datetime.now().isoformat(timespec="seconds"),
            },
        )


def _run_gen2_shadow_update_task(task_id: str, signal_date: str, pool_rank: int, alpha191_gate: str = "off") -> None:
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "gen2_update_live_shadow.py"),
        "--signal-date",
        signal_date,
        "--pool-rank",
        str(pool_rank),
        "--alpha191-gate",
        str(alpha191_gate or "off"),
    ]
    env = os.environ.copy()
    env["AISTOCK_GEN2_LIVE_SKIP_INTRADAY_NORMAL_EMPTY"] = "1"
    _set_gen2_shadow_update_task(
        task_id,
        {
            "status": "running",
            "progress": 10,
            "command": " ".join(cmd),
            "started_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=900,
        )
        stdout_tail = (proc.stdout or "")[-5000:]
        stderr_tail = (proc.stderr or "")[-5000:]
        if proc.returncode != 0:
            _set_gen2_shadow_update_task(
                task_id,
                {
                    "status": "failed",
                    "progress": 100,
                    "returncode": proc.returncode,
                    "stdout_tail": stdout_tail,
                    "stderr_tail": stderr_tail,
                    "error": (proc.stderr or proc.stdout or f"returncode={proc.returncode}")[-1200:],
                    "completed_at": datetime.now().isoformat(timespec="seconds"),
                },
            )
            return
        result_payload: Dict[str, Any] = {}
        try:
            result_payload = json.loads(proc.stdout or "{}")
        except Exception:
            result_payload = {}
        _set_gen2_shadow_update_task(
            task_id,
            {
                "status": "completed",
                "progress": 100,
                "returncode": proc.returncode,
                "result": result_payload,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
                "completed_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
    except subprocess.TimeoutExpired as exc:
        _set_gen2_shadow_update_task(
            task_id,
            {
                "status": "failed",
                "progress": 100,
                "error": f"timeout: {exc}",
                "completed_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
    except Exception as exc:
        logger.exception("G2 shadow update task failed")
        _set_gen2_shadow_update_task(
            task_id,
            {
                "status": "failed",
                "progress": 100,
                "error": str(exc),
                "completed_at": datetime.now().isoformat(timespec="seconds"),
            },
        )


@router.get("/v4/market-gate")
def get_v4_market_gate(
    trade_date: Optional[str] = Query(None, description="open gate trade date"),
):
    target_date = _normalize_date_str(trade_date) if isinstance(trade_date, (str, date, datetime)) else None
    params: List[Any] = ["999999.SH"]
    date_filter = ""
    if target_date:
        date_filter = "AND k.trade_date <= ?::DATE"
        params.append(target_date)
    try:
        df = clickhouse_query_df(
            f"""
            SELECT code, name, trade_date, close
            FROM (
                SELECT
                    k.code,
                    s.name,
                    k.trade_date,
                    k.close,
                    row_number() OVER (
                        PARTITION BY k.code, k.trade_date
                        ORDER BY ifNull(k.created_at, toDateTime('1970-01-01')) DESC, k.id DESC
                    ) AS rn
                FROM kline_daily k
                LEFT JOIN stocks s ON s.code = k.code
                WHERE k.code = ?
                  {date_filter}
            )
            WHERE rn = 1
            ORDER BY trade_date DESC
            LIMIT 30
            """,
            params,
        )
    except Exception as exc:
        logger.warning(f"load v4 market gate from ClickHouse failed: trade_date={target_date or ''}, error={exc}")
        return _sanitize(
            {
                "available": False,
                "can_open": False,
                "code": "999999.SH",
                "name": "指数数据",
                "message": "指数数据暂时不可用，已降级为不可开仓。",
            }
        )
    if df is None or df.empty:
        return _sanitize(
            {
                "available": False,
                "can_open": False,
                "code": "999999.SH",
                "name": "指数数据",
                "message": "指数数据不存在或暂时不可用。",
            }
        )
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["trade_date", "close"]).sort_values("trade_date").reset_index(drop=True)
    snapshot_date = target_date or _normalize_date_str(_resolve_latest_stock_trade_date())
    live_snapshot: Dict[str, Any] = {}
    if snapshot_date:
        try:
            if not clickhouse_table_exists("intraday_quote_snapshot"):
                snap_df = pd.DataFrame()
            else:
                snap_df = clickhouse_query_df(
                    """
                    SELECT code, name, snapshot_time, price
                    FROM intraday_quote_snapshot
                    WHERE code IN ('999999.SH', '000001.SH')
                      AND asset_type = 'index'
                      AND snapshot_date = ?::DATE
                      AND price > 0
                    ORDER BY snapshot_time DESC
                    LIMIT 1
                    """,
                    [snapshot_date],
                )
            if snap_df is not None and not snap_df.empty:
                snap = snap_df.iloc[0]
                live_snapshot = {
                    "code": str(snap.get("code") or "999999.SH"),
                    "name": str(snap.get("name") or "上证指数"),
                    "snapshot_time": str(snap.get("snapshot_time") or ""),
                    "price": _to_float(snap.get("price")),
                }
        except Exception as exc:
            logger.warning(f"load shanghai intraday market gate snapshot failed: {exc}")
    if live_snapshot.get("price") is not None:
        snap_day = pd.to_datetime(snapshot_date, errors="coerce")
        prev_df = df[df["trade_date"] < snap_day].copy() if pd.notna(snap_day) else pd.DataFrame()
        if len(prev_df) >= 19:
            close = _to_float(live_snapshot.get("price"))
            ma20 = _to_float((prev_df["close"].tail(19).sum() + float(close or 0.0)) / 20.0)
            can_open = bool(close is not None and ma20 is not None and close >= ma20)
            name = str(live_snapshot.get("name") or "指数")
            message = (
                f"{name} 现报 {close:.2f} >= MA20 {ma20:.2f}，判定可开启。"
                if can_open
                else f"{name} 现报 {close:.2f} < MA20 {ma20:.2f}，不满足开仓条件，建议延后。"
            )
            return _sanitize(
                {
                    "available": True,
                    "can_open": can_open,
                    "code": "999999.SH",
                    "name": name,
                    "trade_date": snapshot_date,
                    "close": close,
                    "ma20": ma20,
                    "source": "intraday_quote_snapshot",
                    "snapshot_time": live_snapshot.get("snapshot_time"),
                    "daily_base_count": 19,
                    "rule": "指数现报 >= MA20 且此前至少有19个交易日历史可用于计算。",
                    "message": message,
                }
            )
    if len(df) < 20:
        return _sanitize(
            {
                "available": False,
                "can_open": False,
                "code": "999999.SH",
                "name": "指数",
                "message": "指数 MA20 所需历史数据不足，暂时无法计算。",
            }
        )
    latest = df.iloc[-1]
    ma20 = _to_float(df["close"].tail(20).mean())
    close = _to_float(latest.get("close"))
    can_open = bool(close is not None and ma20 is not None and close >= ma20)
    trade_date_text = str(latest.get("trade_date").date())
    name = str(latest.get("name") or "指数")
    message = (
        f"{name} 收盘 {close:.2f} >= MA20 {ma20:.2f}，判定可开启。"
        if can_open
        else f"{name} 收盘 {close:.2f} < MA20 {ma20:.2f}，不满足开仓条件，建议延后。"
    )
    return _sanitize(
        {
            "available": True,
            "can_open": can_open,
            "code": "999999.SH",
            "name": name,
            "trade_date": trade_date_text,
            "close": close,
            "ma20": ma20,
            "rule": "指数收盘 >= MA20 且有足够历史样本可用于计算。",
            "message": message,
        }
    )


def _normalize_stock_code6(value: Any) -> str:
    raw = str(value or "").strip().upper()
    match = re.search(r"(\d{6})", raw)
    if match:
        return match.group(1)
    digits = re.sub(r"\D+", "", raw)
    if digits and len(digits) <= 6:
        return digits.zfill(6)
    return ""


def _to_exchange_stock_code(value: Any) -> str:
    code = _normalize_stock_code6(value)
    if not code:
        return ""
    return f"{code}.SH" if code.startswith("6") else f"{code}.SZ"


def _load_sector_collective_scores(signal_date: str) -> Dict[str, Dict[str, Any]]:
    try:
        stock_df = clickhouse_query_df(
            """
            SELECT k.code, k.trade_date, k.close, k.amount
            FROM kline_daily k
            JOIN stocks s ON s.code = k.code
            WHERE s.type='stock' AND COALESCE(s.st, 0)=0 AND COALESCE(s.quit, 0)=0
              AND k.trade_date <= ?::DATE
              AND k.trade_date >= ?::DATE - INTERVAL 45 DAY
            ORDER BY k.code, k.trade_date
            """,
            [signal_date, signal_date],
        )
        sector_df = clickhouse_query_df(
            """
            SELECT ss.stock_code, ss.sector_code, s.name AS sector_name
            FROM sector_stocks ss
            JOIN sectors s ON s.code = ss.sector_code
            WHERE s.type='industry' AND s.level=2
            """,
            [],
        )
    except Exception as exc:
        logger.warning(f"load sector collective scores failed: {exc}")
        return {}
    if stock_df is None or stock_df.empty or sector_df is None or sector_df.empty:
        return {}

    df = stock_df.copy()
    df["trade_date"] = df["trade_date"].astype(str).str[:10]
    for col in ["close", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["code", "trade_date", "close"]).sort_values(["code", "trade_date"])
    g = df.groupby("code", sort=False)
    df["ret1"] = g["close"].transform(lambda s: s.pct_change())
    df["mom5"] = g["close"].transform(lambda s: s / s.shift(5) - 1.0)
    df["ma10"] = g["close"].transform(lambda s: s.rolling(10, min_periods=10).mean())
    day_df = df[df["trade_date"] == signal_date].copy()
    if day_df.empty:
        return {}

    sectors = sector_df.copy()
    sectors["stock_code"] = sectors["stock_code"].astype(str)
    merged = day_df.merge(sectors, left_on="code", right_on="stock_code", how="inner")
    if merged.empty:
        return {}
    merged["is_up"] = pd.to_numeric(merged["ret1"], errors="coerce") > 0
    merged["is_breakout"] = (
        (pd.to_numeric(merged["mom5"], errors="coerce") > 0)
        & (pd.to_numeric(merged["close"], errors="coerce") > pd.to_numeric(merged["ma10"], errors="coerce"))
    )
    grouped = (
        merged.groupby(["sector_code", "sector_name"])
        .agg(
            valid_count=("code", "nunique"),
            up_ratio=("is_up", "mean"),
            breakout_ratio=("is_breakout", "mean"),
            avg_mom5=("mom5", "mean"),
        )
        .reset_index()
    )
    grouped = grouped[grouped["valid_count"] >= 5].copy()
    if grouped.empty:
        return {}
    grouped["sector_score"] = (
        grouped["up_ratio"].fillna(0)
        + grouped["breakout_ratio"].fillna(0)
        + grouped["avg_mom5"].clip(-0.1, 0.2).fillna(0) * 5.0
    )

    code_scores: Dict[str, Dict[str, Any]] = {}
    for _, row in merged[["code", "sector_code", "sector_name"]].drop_duplicates().iterrows():
        code = str(row.get("code") or "")
        sector_code = str(row.get("sector_code") or "")
        match = grouped[grouped["sector_code"] == sector_code]
        if match.empty:
            continue
        best = match.iloc[0]
        current = code_scores.get(code)
        score = _to_float(best.get("sector_score")) or 0.0
        if current and score <= float(current.get("sector_score") or 0):
            continue
        code_scores[code] = {
            "sector_code": sector_code,
            "sector_name": str(best.get("sector_name") or row.get("sector_name") or ""),
            "sector_score": round(score, 4),
            "sector_up_ratio": round((_to_float(best.get("up_ratio")) or 0) * 100, 2),
            "sector_breakout_ratio": round((_to_float(best.get("breakout_ratio")) or 0) * 100, 2),
            "sector_avg_mom5": round((_to_float(best.get("avg_mom5")) or 0) * 100, 2),
        }
    return code_scores


def _load_buy_pool_daily_features(signal_date: str, codes: List[str]) -> Dict[str, Dict[str, Any]]:
    full_codes = [_to_exchange_stock_code(code) for code in codes]
    full_codes = [code for code in full_codes if code]
    if not full_codes:
        return {}
    try:
        placeholders = ",".join(["?"] * len(full_codes))
        df = clickhouse_query_df(
            f"""
            SELECT code, trade_date, open, high, low, close, volume, amount
            FROM kline_daily
            WHERE code IN ({placeholders})
              AND trade_date <= ?::DATE
              AND trade_date >= ?::DATE - INTERVAL 140 DAY
            ORDER BY code, trade_date
            """,
            full_codes + [signal_date, signal_date],
        )
    except Exception as exc:
        logger.warning(f"load buy pool daily features failed: {exc}")
        return {}
    if df is None or df.empty:
        return {}

    hist = df.copy()
    hist["trade_date"] = hist["trade_date"].astype(str).str[:10]
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        hist[col] = pd.to_numeric(hist[col], errors="coerce")
    hist = hist.dropna(subset=["code", "trade_date", "close"]).sort_values(["code", "trade_date"])
    g = hist.groupby("code", sort=False)
    hist["ret1"] = g["close"].transform(lambda s: s.pct_change())
    hist["ma10"] = g["close"].transform(lambda s: s.rolling(10, min_periods=10).mean())
    hist["mom5"] = g["close"].transform(lambda s: s / s.shift(5) - 1.0)
    hist["mom10"] = g["close"].transform(lambda s: s / s.shift(10) - 1.0)
    hist["ret_prev1"] = g["ret1"].shift(1)
    hist["close_vs_2d"] = g["close"].transform(lambda s: s / s.shift(2) - 1.0)
    hist["range2"] = (
        g["high"].transform(lambda s: s.rolling(2, min_periods=2).max())
        / g["low"].transform(lambda s: s.rolling(2, min_periods=2).min())
        - 1.0
    )
    hist["vol5_prev"] = g["volume"].transform(lambda s: s.shift(1).rolling(5, min_periods=5).mean())
    hist["vol5_before_2d"] = g["volume"].transform(lambda s: s.shift(2).rolling(5, min_periods=5).mean())
    hist["volume_prev1"] = g["volume"].shift(1)
    hist["low20"] = g["low"].transform(lambda s: s.rolling(20, min_periods=20).min())
    hist["high20"] = g["high"].transform(lambda s: s.rolling(20, min_periods=20).max())
    hist["runup20"] = hist["close"] / hist["low20"] - 1.0
    hist["base_range20"] = hist["high20"] / hist["low20"] - 1.0
    hist["near_high20"] = hist["close"] / hist["high20"]

    result: Dict[str, Dict[str, Any]] = {}
    for full_code, sub in hist.groupby("code", sort=False):
        sub = sub[sub["trade_date"] <= signal_date].copy().reset_index(drop=True)
        if sub.empty:
            continue
        current = sub.iloc[-1]
        idx = len(sub) - 1
        start = max(0, idx - 20)
        reset_idx: Optional[int] = None
        for pos in range(idx - 1, start - 1, -1):
            close = _to_float(sub.at[pos, "close"])
            ma10 = _to_float(sub.at[pos, "ma10"])
            ret1 = _to_float(sub.at[pos, "ret1"])
            if (close is not None and ma10 is not None and close <= ma10) or (ret1 is not None and ret1 <= -0.03):
                reset_idx = pos
                break
        base_start = reset_idx if reset_idx is not None else start
        close_now = _to_float(current.get("close"))
        leg_low = _to_float(sub.iloc[base_start : idx + 1]["close"].min())
        leg_runup = (close_now / leg_low - 1.0) if close_now is not None and leg_low is not None and leg_low > 0 else None
        base_range = _to_float(current.get("base_range20"))
        near_high = _to_float(current.get("near_high20"))
        dense_score = None
        if base_range is not None and near_high is not None:
            dense_score = (1.0 / (1.0 + max(base_range, 0.0))) * min(near_high, 1.2)
        volume = _to_float(current.get("volume"))
        volume_prev1 = _to_float(current.get("volume_prev1"))
        vol5_prev = _to_float(current.get("vol5_prev"))
        vol5_before_2d = _to_float(current.get("vol5_before_2d"))
        shrink_1d = (volume / vol5_prev) if volume is not None and volume > 0 and vol5_prev is not None and vol5_prev > 0 else None
        shrink_2d = (
            ((volume or 0) + (volume_prev1 or 0)) / 2.0 / vol5_before_2d
            if volume is not None
            and volume > 0
            and volume_prev1 is not None
            and volume_prev1 > 0
            and vol5_before_2d is not None
            and vol5_before_2d > 0
            else None
        )
        result[str(full_code)] = {
            "trade_date": str(current.get("trade_date") or signal_date),
            "close": close_now,
            "mom5_pct": None if _to_float(current.get("mom5")) is None else round((_to_float(current.get("mom5")) or 0) * 100, 2),
            "mom10_pct": None if _to_float(current.get("mom10")) is None else round((_to_float(current.get("mom10")) or 0) * 100, 2),
            "day_ret_pct": None if _to_float(current.get("ret1")) is None else round((_to_float(current.get("ret1")) or 0) * 100, 2),
            "prev_day_ret_pct": None if _to_float(current.get("ret_prev1")) is None else round((_to_float(current.get("ret_prev1")) or 0) * 100, 2),
            "close_vs_2d_pct": None if _to_float(current.get("close_vs_2d")) is None else round((_to_float(current.get("close_vs_2d")) or 0) * 100, 2),
            "range2_pct": None if _to_float(current.get("range2")) is None else round((_to_float(current.get("range2")) or 0) * 100, 2),
            "shrink_1d_ratio": None if shrink_1d is None else round(shrink_1d, 2),
            "shrink_2d_ratio": None if shrink_2d is None else round(shrink_2d, 2),
            "leg_runup_pct": None if leg_runup is None else round(leg_runup * 100, 2),
            "runup20_pct": None if _to_float(current.get("runup20")) is None else round((_to_float(current.get("runup20")) or 0) * 100, 2),
            "base_range20_pct": None if base_range is None else round(base_range * 100, 2),
            "near_high20_pct": None if near_high is None else round(near_high * 100, 2),
            "dense_score": None if dense_score is None else round(dense_score, 4),
        }
    return result


@router.get("/gen2/lab")
def get_gen2_strategy_lab(
    signal_date: Optional[str] = Query(None, description="signal date"),
    limit: int = Query(30, ge=10, le=50, description="candidate row limit"),
):
    return build_gen2_strategy_lab(signal_date, int(limit))


@router.get("/gen2/live")
def get_gen2_live(
    target_date: Optional[str] = Query(None, description="target trade date"),
):
    state = _load_gen2_shadow_monitor_state()
    reconciled_official_rebuild = _reconcile_official_rebuild_state_if_needed(state, datetime.now())
    requested_date = _normalize_date_str(target_date)
    latest_trade_date = _normalize_date_str(_resolve_latest_stock_trade_date())
    if not latest_trade_date and not requested_date:
        return {
            "available": False,
            "message": "无法解析可用交易日",
            "requested_date": "",
            "selected_date": "",
            "latest_trade_date": "",
            "snapshot_status": "gen2_live",
            "selection": {},
            "positions": {"rows": []},
            "decision": {"mode": "gen2"},
            "gen2_shadow": {},
        }

    date_resolution = _resolve_gen2_signal_date(requested_date or latest_trade_date)
    selected_date = (
        _normalize_date_str(date_resolution.get("effective"))
        if isinstance(date_resolution, dict) and date_resolution.get("effective")
        else requested_date
        or latest_trade_date
    )
    try:
        shadow = get_gen2_risk_cool_shadow(selected_date, 30)
    except Exception as exc:
        logger.exception("gen2/live failed to load risk_cool_shadow")
        shadow = {
            "available": False,
            "rows": [],
            "message": f"G2 risk_cool shadow load failed: {exc}",
            "strategy_name": "G2 V3 User V2",
            "cooldown_policy": "two_stop_cd3_skip",
            "source_latest_date": selected_date or "",
            "signal_date": selected_date or "",
            "row_count": 0,
            "global_summary": _load_gen2_risk_cool_shadow_summary(),
            "data_freshness": build_gen2_data_freshness(selected_date, None),
            "verification_options": _gen2_shadow_verification_options(),
            "verification_summary": _build_gen2_shadow_verification_summary([]),
            "verification_global_summary": _build_gen2_shadow_verification_summary([]),
            "verification_queue": [],
            "requested_date": requested_date,
            "selection_signal_date": selection_signal_date,
            "display_signal_date": selection_signal_date or requested_date or "",
            "ledger_signal_date": "",
            "is_ledger_fallback": False,
            "date_notice": "",
        }
    try:
        selection_pool = _build_gen2_live_selection_summary(selected_date) if selected_date else {}
        selection_signal_date = _normalize_date_str(selection_pool.get("signal_date") or selected_date)
    except Exception as exc:
        logger.exception("gen2/live failed to build selection_pool")
        selection_pool = {
            "summary": {},
            "branch_freshness": {},
            "live_trade_mode": "latest_driven_mainline",
            "official_branch_blocks_live": False,
            "message": f"G2 selection pool build failed: {exc}",
        }
        selection_signal_date = selected_date
    latest = shadow.get("source_latest_date") or selected_date or ""
    shadow_signal_date = _normalize_date_str(shadow.get("signal_date"))
    fallback = _build_live_fallback_info(requested_date, selection_signal_date, latest_trade_date, latest)
    selected_date_display = _normalize_date_str(selection_signal_date or shadow_signal_date or selected_date or latest)
    fallback_title = str(fallback.get("title") or "").strip()
    return _sanitize(
        {
            "available": bool(shadow.get("available") or selected_date),
            "message": shadow.get("message") or "G2 live data loaded.",
            "requested_date": target_date or "",
            "selected_date": selected_date_display,
            "shadow_signal_date": shadow_signal_date or "",
            "selection_signal_date": selection_signal_date or "",
            "latest_trade_date": latest,
            "snapshot_status": "gen2_live",
            "date_resolution": date_resolution,
            "official_rebuild": reconciled_official_rebuild or state.get("last_official_rebuild_result"),
            "fallback": fallback,
            "fallback_notice": fallback_title,
            "selection": {
                "summary": {
                    "strategy_name": "G2 second-generation strategy",
                    "candidate_count": int(selection_pool.get("summary", {}).get("candidate_count") or shadow.get("row_count") or 0),
                },
                "branch_freshness": selection_pool.get("branch_freshness") or {},
                "live_trade_mode": selection_pool.get("live_trade_mode") or "latest_driven_mainline",
                "official_branch_blocks_live": bool(selection_pool.get("official_branch_blocks_live")),
            },
            "positions": {"rows": []},
            "decision": {
                "mode": "gen2",
                "can_open": True,
                "message": "G2 live mode uses second-generation signal, monitor and manual-holding controls.",
            },
            "gen2_shadow": shadow,
        }
    )


@router.get("/gen2/open-signals")
def get_gen2_open_signals(
    signal_date: Optional[str] = Query(None, description="signal date"),
    limit: int = Query(30, ge=10, le=100, description="candidate row limit"),
):
    return build_gen2_open_signals(signal_date, int(limit))


@router.get("/gen2/timing")
def get_gen2_timing(
    signal_date: Optional[str] = Query(None, description="signal date"),
    curve_days: int = Query(420, ge=80, le=1500, description="chart trading days"),
    index_code: str = Query("999999.SH", description="chart index code"),
):
    return build_gen2_timing(signal_date, int(curve_days), index_code)


@router.get("/gen2/backtest")
def get_gen2_backtest_history(
    strategy_code: str = Query("g2_alpha191_volume5_keep80_runup", description="gen2 strategy code"),
):
    return build_gen2_backtest_history(strategy_code)


@router.post("/gen2/backtest/update-latest")
def run_gen2_backtest_update_latest(
    end_date: Optional[str] = Query(None, description="optional backtest end date; blank means latest available source date"),
    legacy_330: bool = Query(True, description="Use legacy 330% g2_v2 complete build mode"),
):
    selected_end_date = _normalize_date_str(end_date) if end_date else None
    with GEN2_BACKTEST_UPDATE_TASK_LOCK:
        for task in GEN2_BACKTEST_UPDATE_TASKS.values():
            if task.get("status") in {"queued", "running"}:
                return dict(task)
    task_id = f"gen2_backtest_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"
    _set_gen2_backtest_update_task(
        task_id,
        {
            "task_id": task_id,
            "status": "queued",
            "progress": 0,
            "end_date": selected_end_date or "",
            "created_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    thread = threading.Thread(
        target=_run_gen2_backtest_update_task,
        args=(task_id, selected_end_date, bool(legacy_330)),
        name=f"gen2-backtest-update-{task_id}",
        daemon=True,
    )
    thread.start()
    return _get_gen2_backtest_update_task(task_id)


@router.get("/gen2/backtest/update-task/{task_id}")
def get_gen2_backtest_update_task(task_id: str):
    task = _get_gen2_backtest_update_task(task_id)
    if task:
        return task
    return {"task_id": task_id, "status": "missing", "progress": 0, "error": "task not found"}


@router.post("/gen2/official-rebuild/update-latest")
def run_gen2_official_rebuild_update_latest(
    end_date: Optional[str] = Query(None, description="optional official rebuild end date; blank means latest trade date"),
    mode: str = Query("full", description="mainline | breakout_only | full"),
    legacy_330: bool = Query(True, description="Use legacy 330% g2_v2 complete build mode"),
):
    selected_end_date = _normalize_date_str(end_date) if end_date else _normalize_date_str(_resolve_latest_stock_trade_date())
    if not selected_end_date:
        return {"status": "failed", "progress": 100, "error": "无法解析正式分支重建日期"}
    normalized_mode = str(mode or "full").strip().lower()
    if normalized_mode not in {"mainline", "breakout_only", "full"}:
        return {"status": "failed", "progress": 100, "error": "Unsupported mode"}
    state = _load_gen2_shadow_monitor_state()
    _reconcile_official_rebuild_state_if_needed(state)
    active = _find_active_gen2_official_rebuild_task()
    if active:
        return active
    return _start_gen2_official_rebuild_task(
        selected_end_date,
        include_breakout=(normalized_mode == "full"),
        timeout_seconds=int(state.get("official_rebuild_timeout_seconds") or 10800),
        breakout_only=(normalized_mode == "breakout_only"),
        legacy_330=bool(legacy_330),
    )


@router.get("/gen2/official-rebuild/update-task/{task_id}")
def get_gen2_official_rebuild_update_task(task_id: str):
    task = _get_gen2_official_rebuild_task(task_id)
    if task:
        return task
    return {"task_id": task_id, "status": "missing", "progress": 0, "error": "task not found"}


@router.get("/gen2/factors/registry")
def get_gen2_factor_registry():
    return build_gen2_factor_registry()


@router.get("/gen2/factors/test")
def get_gen2_factor_test(
    factor_id: str = Query("Alpha001", description="factor id, e.g. Alpha001"),
    start_date: Optional[str] = Query(None, description="test start date"),
    end_date: Optional[str] = Query(None, description="test end date"),
    horizon: int = Query(1, ge=1, le=20, description="forward return horizon in trading bars"),
    min_symbols: int = Query(80, ge=20, le=1000, description="minimum daily cross-section size"),
):
    return run_gen2_factor_test(factor_id, start_date, end_date, int(horizon), int(min_symbols))


@router.get("/gen2/selection-pool")
def get_gen2_selection_pool(
    signal_date: Optional[str] = Query(None, description="signal date"),
    limit: int = Query(80, ge=10, le=300, description="candidate row limit"),
):
    state = _load_gen2_shadow_monitor_state()
    reconciled_official_rebuild = _reconcile_official_rebuild_state_if_needed(state, datetime.now())
    requested_date = _normalize_date_str(signal_date)
    latest_trade_date = _normalize_date_str(_resolve_latest_stock_trade_date())
    date_resolution = _resolve_gen2_signal_date(requested_date or latest_trade_date) if requested_date or latest_trade_date else {}
    selected_date = (
        _normalize_date_str(date_resolution.get("effective"))
        if isinstance(date_resolution, dict) and date_resolution.get("effective")
        else requested_date
        or latest_trade_date
    )
    try:
        payload = _build_gen2_selection_pool(selected_date, int(limit))
        payload["official_rebuild"] = reconciled_official_rebuild or state.get("last_official_rebuild_result")
        payload["date_resolution"] = date_resolution
        payload["fallback"] = _build_live_fallback_info(
            requested_date,
            str(payload.get("signal_date") or selected_date),
            latest_trade_date,
            str(payload.get("source_latest_date") or payload.get("signal_date") or selected_date or ""),
        )
        return payload
    except Exception as exc:
        logger.exception("gen2/selection-pool failed, returning fallback payload")
        payload = _build_gen2_selection_pool_error_payload(selected_date, int(limit), str(exc))
        payload["official_rebuild"] = reconciled_official_rebuild or state.get("last_official_rebuild_result")
        payload["date_resolution"] = date_resolution
        payload["fallback"] = _build_live_fallback_info(
            requested_date,
            str(payload.get("signal_date") or selected_date),
            latest_trade_date,
            str(payload.get("source_latest_date") or payload.get("signal_date") or selected_date or ""),
        )
        return payload


@router.get("/gen2/mainline-hotspots")
def get_gen2_mainline_hotspots(
    target_date: Optional[str] = Query(None, description="target snapshot date"),
    limit: int = Query(30, ge=5, le=120, description="row limit per section"),
):
    return build_gen2_mainline_hotspots(target_date, int(limit))


@router.post("/gen2/mainline-hotspots/update")
def run_gen2_mainline_hotspots_update(
    target_date: Optional[str] = Query(None, description="target snapshot date"),
    limit: int = Query(30, ge=5, le=120, description="row limit per section"),
    mode: str = Query("all", description="refresh mode: all | sector | theme"),
):
    active_task = _get_active_gen2_mainline_update_task()
    if active_task:
        return active_task
    selected_date = _normalize_date_str(target_date) if target_date else None
    normalized_mode = str(mode or "all").strip().lower()
    if normalized_mode not in {"all", "sector", "theme"}:
        normalized_mode = "all"
    task_id = f"gen2_mainline_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"
    step_total = 7 if normalized_mode == "all" else 3 if normalized_mode == "sector" else 4
    _set_gen2_mainline_update_task(
        task_id,
        {
            "task_id": task_id,
            "status": "queued",
            "progress": 0,
            "target_date": selected_date,
            "limit": int(limit),
            "mode": normalized_mode,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "steps_total": step_total,
            "steps_completed": 0,
            "steps": [],
            "message": "主线热点产物刷新已入队",
        },
    )
    thread = threading.Thread(
        target=_run_gen2_mainline_update_task,
        args=(task_id, selected_date, int(limit), normalized_mode),
        name=f"gen2-mainline-update-{task_id}",
        daemon=True,
    )
    thread.start()
    return _get_gen2_mainline_update_task(task_id) or {"task_id": task_id, "status": "queued", "progress": 0}


@router.get("/gen2/mainline-hotspots/update-task/{task_id}")
def get_gen2_mainline_hotspots_update_task(task_id: str):
    task = _get_gen2_mainline_update_task(task_id)
    if not task:
        return {"task_id": task_id, "status": "missing", "progress": 0, "message": "任务不存在"}
    return _sanitize(task)


@router.get("/gen2/workflow/status")
def get_gen2_workflow_status(
    signal_date: Optional[str] = Query(None, description="signal date"),
):
    state = _load_gen2_shadow_monitor_state()
    reconciled_official_rebuild = _reconcile_official_rebuild_state_if_needed(state, datetime.now())
    requested_date = _normalize_date_str(signal_date)
    latest_trade_date = _normalize_date_str(_resolve_latest_stock_trade_date())
    if not requested_date:
        requested_date = latest_trade_date
    date_resolution = _resolve_gen2_signal_date(requested_date) if requested_date else {}
    selected_date = (
        _normalize_date_str(date_resolution.get("effective"))
        if isinstance(date_resolution, dict) and date_resolution.get("effective")
        else requested_date
        or latest_trade_date
    )
    date_resolution_reason = str(date_resolution.get("reason") or "").strip() or None
    date_resolution_status = date_resolution.get("status") if isinstance(date_resolution.get("status"), dict) else None
    if not selected_date:
        return {"available": False, "ok": False, "message": "无法解析目标信号日，返回空状态。", "pipeline_stages": [], "blockers": []}
    check = _check_gen2_shadow_blockers(
        selected_date,
        datetime.now(),
        alpha191_gate=str(state.get("alpha191_gate") or "g2_v2_complete"),
        recipient_override=state.get("recipient_email"),
    )
    monitor_status = _configure_gen2_shadow_buy_monitor_scheduler()
    return _sanitize(
        {
            "available": True,
            "ok": bool(check.get("ok")),
            "signal_date": selected_date,
            "checked_at": check.get("checked_at"),
            "prev_trade_date": check.get("prev_trade_date"),
            "alpha191_gate": str(state.get("alpha191_gate") or "g2_v2_complete"),
            "official_rebuild": reconciled_official_rebuild or state.get("last_official_rebuild_result"),
            "monitor": monitor_status,
            "pipeline_stages": check.get("pipeline_stages") or [],
            "pipeline_checks": check.get("pipeline_checks") or [],
            "requested_date": requested_date,
            "date_resolution": date_resolution,
            "date_resolution_reason": date_resolution_reason,
            "date_resolution_status": date_resolution_status,
            "lineage_items": _gen2_workflow_lineage(selected_date, check.get("prev_trade_date")),
            "blockers": check.get("blockers") or [],
            "last_result": state.get("last_result"),
            "message": "工作流当前可执行" if check.get("ok") else "工作流当前不可就绪，需先补齐上游依赖",
        }
    )


@router.get("/gen2/workflow/lineage")
def get_gen2_workflow_lineage(
    signal_date: Optional[str] = Query(None, description="signal date"),
):
    selected_date = _normalize_date_str(signal_date) if signal_date else _normalize_date_str(_resolve_latest_stock_trade_date())
    if not selected_date:
        return {"available": False, "items": [], "message": "无法解析目标信号日。"}
    prev_trade_date = _previous_stock_trade_date(selected_date)
    return _sanitize(
        {
            "available": True,
            "signal_date": selected_date,
            "prev_trade_date": prev_trade_date,
            "items": _gen2_workflow_lineage(selected_date, prev_trade_date),
        }
    )


@router.get("/gen2/daily-trade-ticket")
def get_gen2_daily_trade_ticket(
    signal_date: Optional[str] = Query(None, description="signal date"),
    limit: int = Query(30, ge=10, le=100, description="source candidate row limit"),
    formal_limit: int = Query(3, ge=1, le=5, description="formal trade ticket candidate limit"),
    persist: bool = Query(True, description="write JSON and Markdown ticket artifacts"),
):
    return _build_gen2_daily_trade_ticket(
        signal_date=signal_date,
        limit=limit,
        formal_limit=formal_limit,
        persist=bool(persist),
    )


@router.get("/gen2/daily-trade-ticket/execution")
def get_gen2_daily_trade_execution(
    signal_date: Optional[str] = Query(None, description="signal date"),
):
    selected_date = _normalize_date_str(signal_date)
    if not selected_date:
        ticket = _build_gen2_daily_trade_ticket(None, limit=30, formal_limit=3, persist=False)
        selected_date = _normalize_date_str(ticket.get("signal_date"))
    record = _find_gen2_daily_execution_record(selected_date or "")
    return _sanitize(
        {
            "available": bool(record),
            "signal_date": selected_date or "",
            "record": record,
            "ledger_path": str(GEN2_DAILY_EXECUTION_LEDGER_PATH),
        }
    )


@router.post("/gen2/daily-trade-ticket/execution")
def save_gen2_daily_trade_execution(
    payload: Dict[str, Any] = Body(...),
):
    return _upsert_gen2_daily_execution_record(payload or {})


@router.get("/gen2/risk-cool-shadow")
def get_gen2_risk_cool_shadow(
    signal_date: Optional[str] = Query(None, description="signal date"),
    limit: int = Query(30, ge=5, le=100, description="candidate row limit"),
):
    ledger_df = _load_gen2_risk_cool_shadow_ledger()
    requested_date = _normalize_date_str(signal_date) if signal_date else None
    selection_signal_date = ""
    try:
        selection_summary = _build_gen2_live_selection_summary(requested_date) if requested_date else {}
        selection_signal_date = _normalize_date_str(selection_summary.get("signal_date") or "")
    except Exception as exc:
        logger.warning(f"load gen2 live selection summary for shadow view failed: {exc}")
        selection_signal_date = ""
    if ledger_df.empty or "entry_date" not in ledger_df.columns:
        return {
            "available": False,
            "rows": [],
            "message": "G2 V3 User V2 two_stop_cd3影子台账尚未生成",
            "strategy_name": "G2 V3 User V2",
            "cooldown_policy": "two_stop_cd3_skip",
            "global_summary": _load_gen2_risk_cool_shadow_summary(),
            "data_freshness": build_gen2_data_freshness(signal_date, None),
            "verification_options": _gen2_shadow_verification_options(),
            "verification_summary": _build_gen2_shadow_verification_summary([]),
            "verification_global_summary": _build_gen2_shadow_verification_summary([]),
            "verification_queue": [],
            "requested_date": requested_date,
            "selection_signal_date": selection_signal_date,
            "display_signal_date": selection_signal_date or requested_date or "",
            "ledger_signal_date": "",
            "is_ledger_fallback": False,
            "date_notice": "",
        }

    all_dates = sorted([str(x) for x in ledger_df["entry_date"].dropna().unique() if str(x)])
    if not all_dates:
        return {
            "available": False,
            "rows": [],
            "message": "G2 V3 User V2 two_stop_cd3影子台账没有可用日期",
            "strategy_name": "G2 V3 User V2",
            "cooldown_policy": "two_stop_cd3_skip",
            "global_summary": _load_gen2_risk_cool_shadow_summary(),
            "data_freshness": build_gen2_data_freshness(signal_date, None),
            "verification_options": _gen2_shadow_verification_options(),
            "verification_summary": _build_gen2_shadow_verification_summary([]),
            "verification_global_summary": _build_gen2_shadow_verification_summary([]),
            "verification_queue": [],
            "requested_date": requested_date,
            "selection_signal_date": selection_signal_date,
            "display_signal_date": selection_signal_date or requested_date or "",
            "ledger_signal_date": "",
            "is_ledger_fallback": False,
            "date_notice": "",
        }

    selected_date = all_dates[-1]
    if requested_date:
        earlier_dates = [x for x in all_dates if x <= requested_date]
        selected_date = earlier_dates[-1] if earlier_dates else all_dates[-1]
    display_signal_date = selection_signal_date or requested_date or selected_date
    is_ledger_fallback = bool(display_signal_date and display_signal_date != selected_date)
    date_notice = (
        f"最新 G2 候选日期为 {display_signal_date}，但影子复盘台账最新仅到 {selected_date}；当前先展示最近可用复盘样本。"
        if is_ledger_fallback
        else ""
    )

    date_notice = (
        f"Latest G2 candidate date is {display_signal_date}, but shadow review ledger latest is {selected_date}; showing latest available review rows."
        if is_ledger_fallback
        else ""
    )
    day_df = ledger_df[ledger_df["entry_date"] == selected_date].copy()
    priority = {"executed": 0, "observable": 1, "suspended_by_two_stop_cd3": 2, "suspended_by_stop_cd5": 2}
    if "shadow_status" in day_df.columns:
        day_df["_status_priority"] = day_df["shadow_status"].map(lambda x: priority.get(str(x), 9))
    else:
        day_df["_status_priority"] = 9
    if "day_signal_rank" not in day_df.columns:
        day_df["day_signal_rank"] = np.nan
    day_df = day_df.sort_values(["_status_priority", "day_signal_rank", "confirm_datetime"], na_position="last").head(int(limit))

    rows: List[Dict[str, Any]] = []
    for _, item in day_df.iterrows():
        status = str(item.get("shadow_status") or "").strip()
        is_suspended = status in {"suspended_by_two_stop_cd3", "suspended_by_stop_cd5"}
        code = str(item.get("code") or "").strip()
        code6 = _normalize_stock_code6(code)
        reason = str(item.get("execution_note") or "").strip()
        if is_suspended:
            reason = reason or "已暂停：命中 two_stop_cd3_skip 策略拦截。"
        elif status == "observable":
            reason = reason or "候选信号待观察，尚未触发执行。"
        elif status == "executed":
            reason = reason or "买入指令已进入执行记录。"
        rows.append(
            {
                "code": _to_exchange_stock_code(code6) if code6 else code,
                "code6": code6,
                "name": str(item.get("name") or ""),
                "entry_date": str(item.get("entry_date") or ""),
                "confirm_datetime": str(item.get("confirm_datetime") or ""),
                "shadow_status": status,
                "status_label": _gen2_shadow_status_label(status),
                "can_observe": not is_suspended,
                "is_suspended": is_suspended,
                "day_signal_rank": _to_int(item.get("day_signal_rank"), default=0) or None,
                "entry_price": _to_float(item.get("entry_price")),
                "v4_rank": _to_int(item.get("v4_rank"), default=0) or None,
                "v4_score": _to_float(item.get("v4_score")),
                "rt_return_pct": _pct_value(item.get("rt_return_from_d1_close")),
                "amount_ratio": _to_float(item.get("rt_30m_amount_ratio")),
                "mom5_pct": _pct_value(item.get("mom5")),
                "mom20_pct": _pct_value(item.get("mom20")),
                "vol_ratio": _to_float(item.get("vol_ratio")),
                "alpha191_gate_enabled": bool(item.get("alpha191_gate_enabled")),
                "alpha191_gate_variant": "" if pd.isna(item.get("alpha191_gate_variant")) else str(item.get("alpha191_gate_variant") or ""),
                "alpha191_factor_date": str(item.get("alpha191_factor_date") or ""),
                "alpha191_gate_score": _to_float(item.get("alpha191_gate_score")),
                "alpha191_gate_threshold": _to_float(item.get("alpha191_gate_threshold")),
                "alpha191_gate_pass": bool(item.get("alpha191_gate_pass")),
                "alpha191_gate_reason": str(item.get("alpha191_gate_reason") or ""),
                "alpha191_volume5_score": _to_float(item.get("alpha191_volume5_score")),
                "alpha191_volume5_rank_in_day": _to_int(item.get("alpha191_volume5_rank_in_day"), default=0) or None,
                "alpha191_original_v4_rank": _to_int(item.get("alpha191_original_v4_rank"), default=0) or None,
                "runup_from_60d_low": _to_float(item.get("runup_from_60d_low")),
                "overhead_pressure_amount_share": _to_float(item.get("overhead_pressure_amount_share")),
                "fwd5_pct": _pct_value(item.get("outcome_fwd_ret_5d")),
                "fwd10_pct": _pct_value(item.get("outcome_fwd_ret_10d")),
                "fwd20_pct": _pct_value(item.get("outcome_fwd_ret_20d")),
                "stop5_touch_30m": bool(item.get("stop5_touch_30m")),
                "good": bool(item.get("outcome_good")),
                "bad": bool(item.get("outcome_bad")),
                "reason_text": reason,
            }
        )

    rows = _apply_gen2_shadow_verifications(rows)
    counts = {
        str(k): int(v)
        for k, v in ledger_df[ledger_df["entry_date"] == selected_date]["shadow_status"].value_counts(dropna=False).items()
    }
    result = {
        "available": True,
        "signal_date": selected_date,
        "requested_date": requested_date,
        "source_latest_date": all_dates[-1],
        "display_signal_date": display_signal_date,
        "selection_signal_date": selection_signal_date,
        "ledger_signal_date": selected_date,
        "is_ledger_fallback": is_ledger_fallback,
        "date_notice": date_notice,
        "strategy_name": "G2 V3 User V2",
        "cooldown_policy": "two_stop_cd3_skip",
        "cooldown_active": (counts.get("suspended_by_two_stop_cd3", 0) + counts.get("suspended_by_stop_cd5", 0)) > 0,
        "row_count": int(len(rows)),
        "counts": counts,
        "rows": rows,
        "global_summary": _load_gen2_risk_cool_shadow_summary(),
        "data_freshness": build_gen2_data_freshness(requested_date, selected_date),
        "verification_options": _gen2_shadow_verification_options(),
        "verification_summary": _build_gen2_shadow_verification_summary(rows),
        "verification_global_summary": _build_gen2_shadow_global_verification_summary(ledger_df),
        "verification_queue": _build_gen2_shadow_verification_queue(ledger_df),
        "message": "G2 V3 User V2 two_stop_cd3影子台账只用于观察和风控确认，不代表自动买入指令",
    }
    return _sanitize(result)


@router.post("/gen2/risk-cool-shadow/update")
def run_gen2_risk_cool_shadow_update(
    signal_date: Optional[str] = Query(None, description="signal date"),
    pool_rank: int = Query(200, ge=50, le=500, description="D-1 V4 rank pool upper bound"),
    alpha191_gate: str = Query("off", description="Alpha191 T-1 gate"),
):
    selected_date = _normalize_date_str(signal_date) if signal_date else _normalize_date_str(_resolve_latest_stock_trade_date())
    alpha191_gate = str(alpha191_gate or "off").strip().lower()
    if alpha191_gate not in GEN2_ALPHA191_ACTIVE_GATES:
        return {"status": "failed", "progress": 100, "error": "Unsupported alpha191_gate"}
    if not selected_date:
        return {"status": "failed", "progress": 100, "error": "无法解析影子交易更新日期"}
    with GEN2_SHADOW_UPDATE_TASK_LOCK:
        for task in GEN2_SHADOW_UPDATE_TASKS.values():
            if task.get("status") in {"queued", "running"}:
                return dict(task)
    task_id = f"gen2_shadow_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"
    _set_gen2_shadow_update_task(
        task_id,
        {
            "task_id": task_id,
            "status": "queued",
            "progress": 0,
            "signal_date": selected_date,
            "pool_rank": int(pool_rank),
            "alpha191_gate": alpha191_gate,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    thread = threading.Thread(
        target=_run_gen2_shadow_update_task,
        args=(task_id, selected_date, int(pool_rank), alpha191_gate),
        name=f"gen2-shadow-update-{task_id}",
        daemon=True,
    )
    thread.start()
    return _get_gen2_shadow_update_task(task_id)


@router.get("/gen2/risk-cool-shadow/update-task/{task_id}")
def get_gen2_risk_cool_shadow_update_task(task_id: str):
    task = _get_gen2_shadow_update_task(task_id)
    if task:
        return task
    return {"task_id": task_id, "status": "missing", "progress": 0, "error": "task not found"}


@router.post("/gen2/risk-cool-shadow/verification")
def save_gen2_risk_cool_shadow_verification(payload: Dict[str, Any]):
    return save_gen2_shadow_verification(payload if isinstance(payload, dict) else {})


@router.post("/gen2/backtest/trade-classification")
def save_gen2_backtest_trade_classification(payload: Dict[str, Any]):
    return update_gen2_trade_classification(payload if isinstance(payload, dict) else {})


def _default_gen2_paper_quantity(entry_price: Any) -> int:
    price = _to_float(entry_price)
    target_value = 150000 * 0.5
    if price is None or price <= 0:
        return 100
    lots = max(1, int(target_value // (price * 100)))
    return int(lots * 100)


def _find_gen2_live_candidate(signal_date: str, code6: str) -> Optional[Dict[str, Any]]:
    pool = _build_gen2_selection_pool(signal_date, 500)
    rows = pool.get("rows") if isinstance(pool, dict) else []
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _normalize_stock_code6(row.get("code6") or row.get("code")) == code6:
            return row
    return None


def _candidate_from_order_payload(data: Dict[str, Any], signal_date: str, code6: str) -> Optional[Dict[str, Any]]:
    candidate = data.get("candidate")
    if not isinstance(candidate, dict):
        candidate = data.get("row") if isinstance(data.get("row"), dict) else None
    if not isinstance(candidate, dict):
        return None
    candidate_code = _normalize_stock_code6(candidate.get("code6") or candidate.get("code"))
    candidate_date = _normalize_date_str(
        candidate.get("entry_date")
        or candidate.get("signal_date")
        or candidate.get("trade_date")
        or candidate.get("date")
    )
    if candidate_code != code6:
        return None
    if candidate_date and candidate_date != signal_date:
        return None
    row = dict(candidate)
    row.setdefault("code", code6)
    row.setdefault("code6", code6)
    row.setdefault("entry_date", signal_date)
    row["_candidate_source"] = "request_snapshot"
    return row


def _market_gate_from_order_payload(data: Dict[str, Any], signal_date: str) -> Optional[Dict[str, Any]]:
    gate = data.get("market_gate")
    if not isinstance(gate, dict):
        gate = data.get("gate") if isinstance(data.get("gate"), dict) else None
    if not isinstance(gate, dict):
        return None
    gate_date = _normalize_date_str(
        gate.get("trade_date")
        or gate.get("signal_date")
        or gate.get("snapshot_date")
        or gate.get("date")
    )
    if gate_date and gate_date != signal_date:
        return None
    if "available" not in gate or "can_open" not in gate:
        return None
    result = dict(gate)
    result.setdefault("trade_date", signal_date)
    result["_gate_source"] = "request_snapshot"
    return result


def _has_active_ptrade_order(code6: str, signal_date: str, side: str = "BUY") -> bool:
    try:
        rows = PTRADE_BRIDGE.list_orders(limit=500).get("rows") or []
    except Exception:
        return False
    active_statuses = {"pending", "processing", "dry_run", "waiting_approval", "submitted", "ack"}
    for item in rows:
        if not isinstance(item, dict):
            continue
        if _normalize_stock_code6(item.get("code")) != code6:
            continue
        if str(item.get("side") or "").upper() != side:
            continue
        if str(item.get("signal_date") or "") != signal_date:
            continue
        if str(item.get("_bridge_status") or "").lower() not in active_statuses:
            continue
        return True
    return False


def _ptrade_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
        return default
    return bool(value)


def _ptrade_payload_requests_live_submit(data: Dict[str, Any]) -> bool:
    dry_run = _ptrade_bool(data.get("dry_run"), default=True)
    require_approval = _ptrade_bool(data.get("require_approval"), default=True)
    approved = _ptrade_bool(data.get("approved"), default=False)
    return (not dry_run) and (approved or not require_approval)


def _ptrade_live_submit_readiness_guard(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not _ptrade_payload_requests_live_submit(data):
        return None
    status = PTRADE_BRIDGE.status()
    readiness = status.get("readiness") if isinstance(status, dict) else {}
    if isinstance(readiness, dict) and bool(readiness.get("ready_for_live_order")):
        return None
    return {
        "ok": False,
        "message": "PTrade live submit is not ready; run heartbeat/dry-run probe first.",
        "ready_for_live_order": False,
        "readiness": readiness,
    }


@router.post("/gen2/paper-order")
def submit_gen2_paper_order(payload: Dict[str, Any]):
    started_at = datetime.now()
    data = payload if isinstance(payload, dict) else {}
    signal_date = _normalize_date_str(data.get("signal_date")) or _normalize_date_str(_resolve_latest_stock_trade_date())
    code6 = _normalize_stock_code6(data.get("code"))
    if not signal_date:
        return _sanitize({"ok": False, "message": "无法解析 G2 目标日期。"})
    if not code6:
        return _sanitize({"ok": False, "message": "无法识别提交的 6 位代码。"})

    row = _candidate_from_order_payload(data, signal_date, code6)
    if row is None:
        submit_elapsed_seconds = round((datetime.now() - started_at).total_seconds(), 6)
        return _sanitize(
            {
                "ok": False,
                "message": f"{code6} {signal_date} 缺少页面候选股快照，已拒绝下单；请刷新 G2 实盘页后重试。",
                "candidate": None,
                "candidate_lookup_mode": "request_snapshot_missing",
                "submit_elapsed_seconds": submit_elapsed_seconds,
            }
        )
    candidate_lookup_mode = "request_snapshot"
    if not bool(row.get("buyable")):
        return _sanitize({"ok": False, "message": f"{code6} 当前不可买入。", "candidate": row, "candidate_lookup_mode": candidate_lookup_mode})
    if bool(row.get("is_suspended")):
        return _sanitize({"ok": False, "message": f"{code6} 当前处于停牌/涨停。", "candidate": row, "candidate_lookup_mode": candidate_lookup_mode})

    gate = _market_gate_from_order_payload(data, signal_date)
    if gate is None:
        submit_elapsed_seconds = round((datetime.now() - started_at).total_seconds(), 6)
        return _sanitize(
            {
                "ok": False,
                "message": f"{signal_date} 缺少页面大盘门禁快照，已拒绝下单；请刷新 G2 实盘页后重试。",
                "candidate": row,
                "market_gate": None,
                "candidate_lookup_mode": candidate_lookup_mode,
                "market_gate_lookup_mode": "request_snapshot_missing",
                "submit_elapsed_seconds": submit_elapsed_seconds,
            }
        )
    market_gate_lookup_mode = "request_snapshot"
    if not bool(gate.get("available")):
        return _sanitize({"ok": False, "message": gate.get("message") or "市场门控不可用，已禁止下单。", "candidate": row, "market_gate": gate, "candidate_lookup_mode": candidate_lookup_mode, "market_gate_lookup_mode": market_gate_lookup_mode})
    if not bool(gate.get("can_open")):
        return _sanitize({"ok": False, "message": gate.get("message") or "市场门控未满足开仓条件。", "candidate": row, "market_gate": gate, "candidate_lookup_mode": candidate_lookup_mode, "market_gate_lookup_mode": market_gate_lookup_mode})

    if _has_active_ptrade_order(code6, signal_date, side="BUY"):
        return _sanitize({"ok": False, "message": f"{code6} 在 {signal_date} 已存在未关闭买单。", "candidate": row, "candidate_lookup_mode": candidate_lookup_mode})

    quantity = _to_int(data.get("quantity"), default=0) or 0
    if quantity <= 0:
        quantity = _default_gen2_paper_quantity(row.get("entry_price"))
    if quantity <= 0 or quantity % 100 != 0:
        return _sanitize({"ok": False, "message": "下单数量需为正整数且为100的整数倍。", "candidate": row})

    limit_price = _to_float(data.get("price"))
    if limit_price is None:
        limit_price = _to_float(row.get("entry_price"))

    order_payload = {
        "source": "gen2_live_page",
        "strategy": "g2_v2_complete_paper",
        "signal_date": signal_date,
        "code": code6,
        "name": str(row.get("name") or ""),
        "side": "BUY",
        "quantity": quantity,
        "price": limit_price,
        "price_type": "limit" if limit_price else "market",
        "dry_run": bool(data.get("dry_run", True)),
        "require_approval": bool(data.get("require_approval", True)),
        "approved": bool(data.get("approved", False)),
        "reason": str(data.get("reason") or f"G2 g2_v2_complete {signal_date} {code6} {str(row.get('stage_label') or 'buyable')}"),
        "risk": {
            "market_gate": gate.get("message"),
            "v4_rank": row.get("v4_rank"),
            "alpha191_volume5_score": row.get("alpha191_volume5_score"),
            "alpha191_volume5_rank_in_day": row.get("alpha191_volume5_rank_in_day"),
            "runup_from_60d_low": row.get("runup_from_60d_low"),
            "source_family": row.get("source_family"),
            "signal_family": row.get("signal_family"),
            "confirm_datetime": row.get("confirm_datetime"),
            "entry_price": row.get("entry_price"),
        },
    }
    guard = _ptrade_live_submit_readiness_guard(order_payload)
    if guard:
        submit_elapsed_seconds = round((datetime.now() - started_at).total_seconds(), 6)
        return _sanitize(
            {
                **guard,
                "candidate": row,
                "market_gate": gate,
                "candidate_lookup_mode": candidate_lookup_mode,
                "market_gate_lookup_mode": market_gate_lookup_mode,
                "submit_elapsed_seconds": submit_elapsed_seconds,
            }
        )
    order = PTRADE_BRIDGE.submit_order(order_payload)
    submit_elapsed_seconds = round((datetime.now() - started_at).total_seconds(), 6)
    return _sanitize(
        {
            "ok": True,
            "message": f"{code6} 已提交纸面买单。",
            "order": order,
            "candidate": row,
            "market_gate": gate,
            "candidate_lookup_mode": candidate_lookup_mode,
            "market_gate_lookup_mode": market_gate_lookup_mode,
            "submit_elapsed_seconds": submit_elapsed_seconds,
        }
    )
@router.get("/ptrade/bridge/status")
def get_ptrade_bridge_status():
    return _sanitize(PTRADE_BRIDGE.status())


def _ptrade_acceptance_kwargs(data: Dict[str, Any]) -> Dict[str, Any]:
    quantity = max(100, _to_int(data.get("quantity"), 100))
    quantity = max(100, (quantity // 100) * 100)
    return {
        "bridge_dir": PTRADE_BRIDGE.paths.root,
        "heartbeat_timeout_seconds": min(180.0, max(0.1, _to_float(data.get("heartbeat_timeout_seconds")) or 120.0)),
        "poll_seconds": min(5.0, max(0.05, _to_float(data.get("poll_seconds")) or 1.0)),
        "max_heartbeat_age_seconds": max(1.0, _to_float(data.get("max_heartbeat_age_seconds")) or 30.0),
        "submit_dry_run": not bool(data.get("skip_dry_run", False)),
        "dry_run_timeout_seconds": min(60.0, max(0.1, _to_float(data.get("dry_run_timeout_seconds")) or 30.0)),
        "code": str(data.get("code") or "600000"),
        "price": _to_float(data.get("price")) or 10.5,
        "quantity": quantity,
    }


def _ptrade_live_submit_test_kwargs(data: Dict[str, Any]) -> Dict[str, Any]:
    quantity = max(100, _to_int(data.get("quantity"), 100))
    quantity = max(100, (quantity // 100) * 100)
    return {
        "bridge_dir": PTRADE_BRIDGE.paths.root,
        "approve_live_submit": bool(data.get("approve_live_submit", False)),
        "code": str(data.get("code") or "600000"),
        "side": str(data.get("side") or "BUY"),
        "price": _to_float(data.get("price")) or 10.5,
        "quantity": quantity,
        "timeout_seconds": min(60.0, max(0.1, _to_float(data.get("timeout_seconds")) or 30.0)),
        "poll_seconds": min(5.0, max(0.05, _to_float(data.get("poll_seconds")) or 1.0)),
        "max_submit_seconds": max(0.01, _to_float(data.get("max_submit_seconds")) or 0.5),
        "max_order_value": min(20000.0, max(100.0, _to_float(data.get("max_order_value")) or 20000.0)),
    }


def _ptrade_watch_acceptance_kwargs(data: Dict[str, Any]) -> Dict[str, Any]:
    quantity = max(100, _to_int(data.get("quantity"), 100))
    quantity = max(100, (quantity // 100) * 100)
    return {
        "bridge_dir": PTRADE_BRIDGE.paths.root,
        "watch_timeout_seconds": min(900.0, max(0.1, _to_float(data.get("watch_timeout_seconds")) or 600.0)),
        "poll_seconds": min(5.0, max(0.05, _to_float(data.get("poll_seconds")) or 1.0)),
        "max_heartbeat_age_seconds": max(1.0, _to_float(data.get("max_heartbeat_age_seconds")) or 30.0),
        "dry_run_timeout_seconds": min(60.0, max(0.1, _to_float(data.get("dry_run_timeout_seconds")) or 30.0)),
        "code": str(data.get("code") or "600000"),
        "price": _to_float(data.get("price")) or 10.5,
        "quantity": quantity,
    }


def _set_ptrade_acceptance_task(task_id: str, payload: Dict[str, Any]) -> None:
    with PTRADE_ACCEPTANCE_TASK_LOCK:
        task = PTRADE_ACCEPTANCE_TASKS.setdefault(task_id, {})
        task.update(_sanitize(payload))


def _get_ptrade_acceptance_task(task_id: str) -> Optional[Dict[str, Any]]:
    with PTRADE_ACCEPTANCE_TASK_LOCK:
        task = PTRADE_ACCEPTANCE_TASKS.get(task_id)
        return dict(task) if task else None


def _find_active_ptrade_acceptance_task() -> Optional[Dict[str, Any]]:
    with PTRADE_ACCEPTANCE_TASK_LOCK:
        for task in PTRADE_ACCEPTANCE_TASKS.values():
            if task.get("status") in {"queued", "running"}:
                return dict(task)
    return None


def _run_ptrade_acceptance_task(task_id: str, kwargs: Dict[str, Any]) -> None:
    _set_ptrade_acceptance_task(
        task_id,
        {
            "status": "running",
            "progress": 10,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    try:
        result = run_ptrade_bridge_acceptance(**kwargs)
        _set_ptrade_acceptance_task(
            task_id,
            {
                "status": "completed" if result.get("ok") else "failed",
                "progress": 100,
                "completed_at": datetime.now().isoformat(timespec="seconds"),
                "result": result,
                "error": "" if result.get("ok") else "; ".join(str(x) for x in (result.get("next_actions") or [])[:2]),
            },
        )
    except Exception as exc:
        _set_ptrade_acceptance_task(
            task_id,
            {
                "status": "failed",
                "progress": 100,
                "completed_at": datetime.now().isoformat(timespec="seconds"),
                "error": str(exc),
            },
        )


def _set_ptrade_live_submit_test_task(task_id: str, payload: Dict[str, Any]) -> None:
    with PTRADE_LIVE_SUBMIT_TEST_TASK_LOCK:
        task = PTRADE_LIVE_SUBMIT_TEST_TASKS.setdefault(task_id, {})
        task.update(_sanitize(payload))


def _get_ptrade_live_submit_test_task(task_id: str) -> Optional[Dict[str, Any]]:
    with PTRADE_LIVE_SUBMIT_TEST_TASK_LOCK:
        task = PTRADE_LIVE_SUBMIT_TEST_TASKS.get(task_id)
        return dict(task) if task else None


def _find_active_ptrade_live_submit_test_task() -> Optional[Dict[str, Any]]:
    with PTRADE_LIVE_SUBMIT_TEST_TASK_LOCK:
        for task in PTRADE_LIVE_SUBMIT_TEST_TASKS.values():
            if task.get("status") in {"queued", "running"}:
                return dict(task)
    return None


def _run_ptrade_live_submit_test_task(task_id: str, kwargs: Dict[str, Any]) -> None:
    _set_ptrade_live_submit_test_task(
        task_id,
        {
            "status": "running",
            "progress": 10,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    try:
        result = run_ptrade_bridge_live_submit_test(**kwargs)
        failed_checks = [
            item
            for item in (result.get("checks") or [])
            if isinstance(item, dict) and item.get("required") and not item.get("ok")
        ]
        _set_ptrade_live_submit_test_task(
            task_id,
            {
                "status": "completed" if result.get("ok") else "failed",
                "progress": 100,
                "completed_at": datetime.now().isoformat(timespec="seconds"),
                "result": result,
                "error": "" if result.get("ok") else "; ".join(str(x.get("name") or x) for x in failed_checks)[:200],
            },
        )
    except Exception as exc:
        _set_ptrade_live_submit_test_task(
            task_id,
            {
                "status": "failed",
                "progress": 100,
                "completed_at": datetime.now().isoformat(timespec="seconds"),
                "error": str(exc),
            },
        )


def _set_ptrade_watch_acceptance_task(task_id: str, payload: Dict[str, Any]) -> None:
    with PTRADE_WATCH_ACCEPTANCE_TASK_LOCK:
        task = PTRADE_WATCH_ACCEPTANCE_TASKS.setdefault(task_id, {})
        task.update(_sanitize(payload))


def _get_ptrade_watch_acceptance_task(task_id: str) -> Optional[Dict[str, Any]]:
    with PTRADE_WATCH_ACCEPTANCE_TASK_LOCK:
        task = PTRADE_WATCH_ACCEPTANCE_TASKS.get(task_id)
        return dict(task) if task else None


def _find_active_ptrade_watch_acceptance_task() -> Optional[Dict[str, Any]]:
    with PTRADE_WATCH_ACCEPTANCE_TASK_LOCK:
        for task in PTRADE_WATCH_ACCEPTANCE_TASKS.values():
            if task.get("status") in {"queued", "running"}:
                return dict(task)
    return None


def _run_ptrade_watch_acceptance_task(task_id: str, kwargs: Dict[str, Any]) -> None:
    _set_ptrade_watch_acceptance_task(
        task_id,
        {
            "status": "running",
            "progress": 10,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    try:
        result = run_ptrade_bridge_watch_acceptance(**kwargs)
        next_actions = result.get("next_actions") or []
        _set_ptrade_watch_acceptance_task(
            task_id,
            {
                "status": "completed" if result.get("ok") else "failed",
                "progress": 100,
                "completed_at": datetime.now().isoformat(timespec="seconds"),
                "result": result,
                "error": "" if result.get("ok") else "; ".join(str(x) for x in next_actions[:2])[:200],
            },
        )
    except Exception as exc:
        _set_ptrade_watch_acceptance_task(
            task_id,
            {
                "status": "failed",
                "progress": 100,
                "completed_at": datetime.now().isoformat(timespec="seconds"),
                "error": str(exc),
            },
        )


@router.get("/ptrade/bridge/readiness-audit")
def get_ptrade_bridge_readiness_audit(
    require_empty_queue_for_live: bool = Query(True, description="require empty pending/processing queue before live test"),
):
    return _sanitize(
        run_ptrade_bridge_readiness_audit(
            bridge_dir=PTRADE_BRIDGE.paths.root,
            require_empty_queue_for_live=bool(require_empty_queue_for_live),
        )
    )


@router.get("/ptrade/bridge/evidence-report")
def get_ptrade_bridge_evidence_report(
    refresh_order_path_probe: bool = Query(True, description="refresh isolated nonblocking order-path proof"),
):
    return _sanitize(
        build_ptrade_bridge_evidence_report(
            bridge_dir=PTRADE_BRIDGE.paths.root,
            refresh_order_path_probe=bool(refresh_order_path_probe),
        )
    )


@router.post("/ptrade/bridge/live-probe")
def run_ptrade_bridge_live_probe_api(payload: Optional[Dict[str, Any]] = None):
    data = payload if isinstance(payload, dict) else {}
    timeout_seconds = min(60.0, max(0.1, _to_float(data.get("timeout_seconds")) or 30.0))
    poll_seconds = min(5.0, max(0.05, _to_float(data.get("poll_seconds")) or 1.0))
    quantity = max(100, _to_int(data.get("quantity"), 100))
    quantity = max(100, (quantity // 100) * 100)
    result = run_ptrade_bridge_live_probe(
        bridge_dir=PTRADE_BRIDGE.paths.root,
        submit_dry_run=bool(data.get("submit_dry_run", False)),
        require_heartbeat=bool(data.get("require_heartbeat", False)),
        max_heartbeat_age_seconds=max(1.0, _to_float(data.get("max_heartbeat_age_seconds")) or 15.0),
        code=str(data.get("code") or "600000"),
        price=_to_float(data.get("price")) or 10.5,
        quantity=quantity,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
        max_submit_seconds=max(0.01, _to_float(data.get("max_submit_seconds")) or 0.5),
    )
    return _sanitize(result)


@router.post("/ptrade/bridge/acceptance")
def run_ptrade_bridge_acceptance_api(payload: Optional[Dict[str, Any]] = None):
    data = payload if isinstance(payload, dict) else {}
    result = run_ptrade_bridge_acceptance(**_ptrade_acceptance_kwargs(data))
    return _sanitize(result)


@router.post("/ptrade/bridge/acceptance/start")
def start_ptrade_bridge_acceptance_task(payload: Optional[Dict[str, Any]] = None):
    active = _find_active_ptrade_acceptance_task()
    if active:
        return _sanitize(active)
    data = payload if isinstance(payload, dict) else {}
    kwargs = _ptrade_acceptance_kwargs(data)
    task_id = f"ptrade_acceptance_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"
    _set_ptrade_acceptance_task(
        task_id,
        {
            "task_id": task_id,
            "status": "queued",
            "progress": 0,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "bridge_dir": str(kwargs.get("bridge_dir")),
            "heartbeat_timeout_seconds": kwargs.get("heartbeat_timeout_seconds"),
            "dry_run_timeout_seconds": kwargs.get("dry_run_timeout_seconds"),
            "submit_dry_run": kwargs.get("submit_dry_run"),
        },
    )
    thread = threading.Thread(
        target=_run_ptrade_acceptance_task,
        args=(task_id, kwargs),
        name=f"ptrade-acceptance-{task_id}",
        daemon=True,
    )
    thread.start()
    return _sanitize(_get_ptrade_acceptance_task(task_id) or {"task_id": task_id, "status": "queued", "progress": 0})


@router.get("/ptrade/bridge/acceptance-task/{task_id}")
def get_ptrade_bridge_acceptance_task(task_id: str):
    task = _get_ptrade_acceptance_task(task_id)
    if task:
        return _sanitize(task)
    return {"task_id": task_id, "status": "missing", "progress": 0, "error": "task not found"}


@router.post("/ptrade/bridge/watch-acceptance/start")
def start_ptrade_bridge_watch_acceptance_task(payload: Optional[Dict[str, Any]] = None):
    active = _find_active_ptrade_watch_acceptance_task()
    if active:
        return _sanitize(active)
    data = payload if isinstance(payload, dict) else {}
    kwargs = _ptrade_watch_acceptance_kwargs(data)
    task_id = f"ptrade_watch_acceptance_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"
    _set_ptrade_watch_acceptance_task(
        task_id,
        {
            "task_id": task_id,
            "status": "queued",
            "progress": 0,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "bridge_dir": str(kwargs.get("bridge_dir")),
            "watch_timeout_seconds": kwargs.get("watch_timeout_seconds"),
            "dry_run_timeout_seconds": kwargs.get("dry_run_timeout_seconds"),
        },
    )
    thread = threading.Thread(
        target=_run_ptrade_watch_acceptance_task,
        args=(task_id, kwargs),
        name=f"ptrade-watch-acceptance-{task_id}",
        daemon=True,
    )
    thread.start()
    return _sanitize(_get_ptrade_watch_acceptance_task(task_id) or {"task_id": task_id, "status": "queued", "progress": 0})


@router.get("/ptrade/bridge/watch-acceptance-task/{task_id}")
def get_ptrade_bridge_watch_acceptance_task(task_id: str):
    task = _get_ptrade_watch_acceptance_task(task_id)
    if task:
        return _sanitize(task)
    return {"task_id": task_id, "status": "missing", "progress": 0, "error": "task not found"}


@router.post("/ptrade/bridge/live-submit-test/start")
def start_ptrade_bridge_live_submit_test_task(payload: Optional[Dict[str, Any]] = None):
    active = _find_active_ptrade_live_submit_test_task()
    if active:
        return _sanitize(active)
    data = payload if isinstance(payload, dict) else {}
    kwargs = _ptrade_live_submit_test_kwargs(data)
    task_id = f"ptrade_live_submit_test_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"
    _set_ptrade_live_submit_test_task(
        task_id,
        {
            "task_id": task_id,
            "status": "queued",
            "progress": 0,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "bridge_dir": str(kwargs.get("bridge_dir")),
            "approve_live_submit": bool(kwargs.get("approve_live_submit")),
            "code": kwargs.get("code"),
            "price": kwargs.get("price"),
            "quantity": kwargs.get("quantity"),
            "max_order_value": kwargs.get("max_order_value"),
        },
    )
    thread = threading.Thread(
        target=_run_ptrade_live_submit_test_task,
        args=(task_id, kwargs),
        name=f"ptrade-live-submit-test-{task_id}",
        daemon=True,
    )
    thread.start()
    return _sanitize(_get_ptrade_live_submit_test_task(task_id) or {"task_id": task_id, "status": "queued", "progress": 0})


@router.get("/ptrade/bridge/live-submit-test-task/{task_id}")
def get_ptrade_bridge_live_submit_test_task(task_id: str):
    task = _get_ptrade_live_submit_test_task(task_id)
    if task:
        return _sanitize(task)
    return {"task_id": task_id, "status": "missing", "progress": 0, "error": "task not found"}


@router.post("/ptrade/bridge/config")
def save_ptrade_bridge_config(payload: Dict[str, Any]):
    data = payload if isinstance(payload, dict) else {}
    return _sanitize({"ok": True, "config": PTRADE_BRIDGE.save_config(data)})


@router.post("/ptrade/bridge/processing/recover-stale")
def recover_stale_ptrade_bridge_processing(payload: Optional[Dict[str, Any]] = None):
    data = payload if isinstance(payload, dict) else {}
    max_age_seconds = max(30, _to_int(data.get("max_age_seconds"), 300))
    reason = str(data.get("reason") or "").strip()
    return _sanitize(PTRADE_BRIDGE.mark_stale_processing(max_age_seconds=max_age_seconds, reason=reason))


@router.get("/ptrade/bridge/orders")
def list_ptrade_bridge_orders(limit: int = Query(100, ge=1, le=500)):
    return _sanitize(PTRADE_BRIDGE.list_orders(limit=int(limit)))


@router.get("/ptrade/bridge/fills")
def list_ptrade_bridge_fills(limit: int = Query(100, ge=1, le=500)):
    return _sanitize(PTRADE_BRIDGE.list_fills(limit=int(limit)))


@router.get("/ptrade/bridge/positions")
def list_ptrade_bridge_positions(limit: int = Query(20, ge=1, le=100)):
    snapshots = PTRADE_BRIDGE.list_position_snapshots(limit=int(limit))
    latest = PTRADE_BRIDGE.latest_positions()
    return _sanitize(
        {
            "ok": bool(latest.get("ok")),
            "latest": latest,
            "snapshots": snapshots.get("rows") or [],
            "updated_at": snapshots.get("updated_at"),
        }
    )


@router.post("/ptrade/bridge/orders")
def submit_ptrade_bridge_order(payload: Dict[str, Any]):
    started_at = datetime.now()
    try:
        data = payload if isinstance(payload, dict) else {}
        guard = _ptrade_live_submit_readiness_guard(data)
        if guard:
            submit_elapsed_seconds = round((datetime.now() - started_at).total_seconds(), 6)
            return _sanitize({**guard, "submit_elapsed_seconds": submit_elapsed_seconds})
        order_data = PTRADE_BRIDGE.submit_order(data)
        submit_elapsed_seconds = round((datetime.now() - started_at).total_seconds(), 6)
        return _sanitize({"ok": True, "order": order_data, "submit_elapsed_seconds": submit_elapsed_seconds})
    except Exception as exc:
        submit_elapsed_seconds = round((datetime.now() - started_at).total_seconds(), 6)
        return _sanitize({"ok": False, "message": str(exc), "submit_elapsed_seconds": submit_elapsed_seconds})


@router.post("/ptrade/bridge/orders/{order_id}/cancel")
def cancel_ptrade_bridge_order(order_id: str, payload: Optional[Dict[str, Any]] = None):
    reason = ""
    if isinstance(payload, dict):
        reason = str(payload.get("reason") or "")
    try:
        return _sanitize(PTRADE_BRIDGE.cancel_order(order_id, reason=reason))
    except Exception as exc:
        return _sanitize({"ok": False, "message": str(exc), "order_id": order_id})


@router.post("/v4/manual-holdings/signals")
def get_v4_manual_holdings_signals(payload: Dict[str, Any]):
    holdings = payload.get("holdings") if isinstance(payload, dict) else []
    if not isinstance(holdings, list):
        holdings = []
    result_rows: List[Dict[str, Any]] = []
    for item in holdings:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()[:6]
        if not code:
            continue
        name = str(item.get("name") or code)
        period15_df = _load_minute_bars_for_signal(code, 15, limit=240)
        period30_df = _load_minute_bars_for_signal(code, 30, limit=240)
        signal15 = _detect_bearish_divergence(period15_df) if not period15_df.empty else {"detected": False, "reason": "15m数据不足，暂不评估。"}
        signal30 = _detect_bearish_divergence(period30_df) if not period30_df.empty else {"detected": False, "reason": "30m数据不足，暂不评估。"}
        rsi_box_t = _detect_rsi_box_t_signal(period15_df) if not period15_df.empty else {
            "enabled": False,
            "status": "data_missing",
            "action": "observe",
            "recommendation": "15m数据不足，暂不评估箱体做T。",
        }
        risk_level = "low"
        if signal15.get("detected") and signal30.get("detected"):
            risk_level = "high"
        elif signal15.get("detected") or signal30.get("detected"):
            risk_level = "medium"
        suggestion = "观察中"
        if risk_level == "high":
            suggestion = "15m与30m均示弱，建议控制仓位并优先观察。"
        elif risk_level == "medium":
            suggestion = "短时出现回落信号，建议谨慎加码。"
        result_rows.append(
            {
                "code": code,
                "name": name,
                "rsi15_signal": signal15,
                "rsi30_signal": signal30,
                "rsi_box_t": rsi_box_t,
                "risk_level": risk_level,
                "suggestion": suggestion,
            }
        )
    return _sanitize({"rows": result_rows, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})


@router.post("/v4/manual-holdings/quotes")
def get_v4_manual_holdings_quotes(payload: Dict[str, Any]):
    holdings = payload.get("holdings") if isinstance(payload, dict) else []
    if not isinstance(holdings, list):
        holdings = []
    rows: List[Dict[str, Any]] = []
    for item in holdings:
        if not isinstance(item, dict):
            continue
        code6 = str(item.get("code") or "").strip()[:6]
        if not code6:
            continue
        cost_price = _to_float(item.get("cost_price"))
        latest_price = None
        prev_close = None
        latest_date = None
        if clickhouse_available():
            try:
                qdf = clickhouse_query_df(
                    """
                    SELECT code, trade_date, close
                    FROM kline_daily
                    WHERE substr(code, 1, 6) = ?
                    ORDER BY trade_date DESC
                    LIMIT 2
                    """,
                    [code6],
                )
                if qdf is not None and not qdf.empty:
                    latest_price = _to_float(qdf.iloc[0].get("close"))
                    latest_date = str(qdf.iloc[0].get("trade_date") or "")
                    if len(qdf) >= 2:
                        prev_close = _to_float(qdf.iloc[1].get("close"))
            except Exception as exc:
                logger.warning(f"load latest quote from ClickHouse failed: code={code6}, error={exc}")
        day_pnl = None
        shares = _to_float(item.get("shares"))
        if latest_price is not None and prev_close is not None and shares is not None:
            day_pnl = (latest_price - prev_close) * shares
        pnl_ratio = None
        if latest_price is not None and cost_price not in (None, 0):
            pnl_ratio = (latest_price / cost_price - 1.0) * 100.0
        rows.append(
            {
                "code": code6,
                "latest_price": latest_price,
                "prev_close": prev_close,
                "latest_trade_date": latest_date,
                "day_pnl": round(float(day_pnl), 2) if day_pnl is not None else None,
                "pnl_ratio": round(float(pnl_ratio), 4) if pnl_ratio is not None else None,
            }
        )
    return _sanitize({"rows": rows, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})


@router.post("/v4/manual-holdings/refresh-all")
def refresh_v4_manual_holdings_all(payload: Dict[str, Any]):
    holdings = payload.get("holdings") if isinstance(payload, dict) else []
    if not isinstance(holdings, list):
        holdings = []
    signal_date = _normalize_date_str(payload.get("signal_date")) if isinstance(payload, dict) else None
    if not signal_date:
        signal_date = _normalize_date_str(payload.get("target_date")) if isinstance(payload, dict) else None
    if not signal_date:
        signal_date = _normalize_date_str(_resolve_latest_stock_trade_date())

    # 1) latest quote + pnl
    quote_resp = get_v4_manual_holdings_quotes({"holdings": holdings})
    quote_map = {
        str(item.get("code") or "").strip()[:6]: item
        for item in (quote_resp.get("rows") or [])
        if isinstance(item, dict)
    }

    # 2) rsi signals
    signal_resp = get_v4_manual_holdings_signals({"holdings": holdings})
    signal_map = {
        str(item.get("code") or "").strip()[:6]: item
        for item in (signal_resp.get("rows") or [])
        if isinstance(item, dict)
    }

    # 3) v4 score/rank (latest <= signal_date)
    codes = [str(item.get("code") or "").strip()[:6] for item in holdings if isinstance(item, dict)]
    codes = [c for c in codes if c]
    score_map: Dict[str, Dict[str, Any]] = {}
    if codes and signal_date:
        placeholders = ", ".join([f":c{i}" for i in range(len(codes))])
        params: Dict[str, Any] = {
            "strategy_version": str(DEFAULT_LIVE_STRATEGY_VERSION or "").strip() or "v5",
            "signal_date": signal_date,
        }
        for i, code in enumerate(codes):
            params[f"c{i}"] = code
        sql = text(
            f"""
            SELECT t.code, t.signal_date, t.score_rank, t.score_total
            FROM {TRADING_SELECTION_SCORE_TABLE} t
            JOIN (
              SELECT code, MAX(signal_date) AS max_signal_date
              FROM {TRADING_SELECTION_SCORE_TABLE}
              WHERE strategy_version = :strategy_version
                AND signal_date <= :signal_date
                AND code IN ({placeholders})
              GROUP BY code
            ) x ON x.code = t.code AND x.max_signal_date = t.signal_date
            WHERE t.strategy_version = :strategy_version
            """
        )
        try:
            with db.engine.connect() as conn:
                df = pd.read_sql(sql, conn, params=params)
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    c = str(row.get("code") or "").strip()[:6]
                    if not c:
                        continue
                    score_map[c] = {
                        "v4_signal_date": str(row.get("signal_date") or ""),
                        "v4_rank": _to_int(row.get("score_rank"), 0),
                        "v4_score": _to_float(row.get("score_total")),
                    }
        except Exception as exc:
            logger.warning(f"load v4 scores for manual holdings failed: {exc}")

    # 3.1) fallback: if the score table misses any current holding, recompute V4 daily pool on signal_date
    missing_score_codes = [code for code in codes if code not in score_map]
    if missing_score_codes and signal_date:
        try:
            hist = _load_hist_with_factors(signal_date)
            if hist is not None and not hist.empty:
                day_df = hist[hist["date"] == pd.Timestamp(signal_date)].copy()
                scored_df = _score_candidates_on_signal_day(day_df)
                if scored_df is not None and not scored_df.empty:
                    scored_df["code"] = scored_df["code"].astype(str).str[:6]
                    for _, row in scored_df.iterrows():
                        c = str(row.get("code") or "").strip()[:6]
                        if not c or c not in missing_score_codes:
                            continue
                        score_map[c] = {
                            "v4_signal_date": signal_date,
                            "v4_rank": _to_int(row.get("score_rank"), 0),
                            "v4_score": _to_float(row.get("score")),
                        }
        except Exception as exc:
            logger.warning(f"fallback recompute v4 score map failed: signal_date={signal_date}, error={exc}")

    merged_rows: List[Dict[str, Any]] = []
    for item in holdings:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()[:6]
        if not code:
            continue
        quote_item = quote_map.get(code, {})
        signal_item = signal_map.get(code, {})
        score_item = score_map.get(code, {})
        in_score_pool = bool(score_item) and _to_int(score_item.get("v4_rank"), 0) > 0
        score_pool_status = "in_pool" if in_score_pool else "out_of_pool"
        risk_level = str(signal_item.get("risk_level") or "low")
        suggestion = str(signal_item.get("suggestion") or "继续观察")
        if not in_score_pool:
            risk_level = "high"
            suggestion = "暂未入池：评分池缺失或异常，建议仅观察不下单。"
        merged_rows.append(
            _sanitize(
                {
                    "code": code,
                    "name": item.get("name"),
                    "latest_price": quote_item.get("latest_price"),
                    "prev_close": quote_item.get("prev_close"),
                    "latest_trade_date": quote_item.get("latest_trade_date"),
                    "day_pnl": quote_item.get("day_pnl"),
                    "pnl_ratio": quote_item.get("pnl_ratio"),
                    "rsi15_signal": signal_item.get("rsi15_signal"),
                    "rsi30_signal": signal_item.get("rsi30_signal"),
                    "rsi_box_t": signal_item.get("rsi_box_t"),
                    "risk_level": risk_level,
                    "suggestion": suggestion,
                    "v4_signal_date": score_item.get("v4_signal_date"),
                    "v4_rank": score_item.get("v4_rank"),
                    "v4_score": score_item.get("v4_score"),
                    "in_score_pool": bool(in_score_pool),
                    "score_pool_status": score_pool_status,
                }
            )
        )
    return _sanitize(
        {
            "signal_date": signal_date,
            "rows": merged_rows,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )


@router.post("/v4/manual-holdings/state")
def save_v4_manual_holdings_state(payload: Dict[str, Any]):
    state = _load_v4_monitor_state()
    holdings = payload.get("holdings") if isinstance(payload, dict) else []
    if not isinstance(holdings, list):
        holdings = []
    state["holdings"] = holdings
    state["reminder_active"] = False
    state["reminder_activated_at"] = None
    state["last_alert_by_code"] = {}
    _save_v4_monitor_state(state)
    return _sanitize({"ok": True, "holdings_count": len(holdings)})


@router.get("/gen2/shadow-monitor/status")
def get_gen2_shadow_buy_monitor_status():
    return _sanitize(_configure_gen2_shadow_buy_monitor_scheduler())


@router.post("/gen2/shadow-monitor/config")
def set_gen2_shadow_buy_monitor_config(payload: Dict[str, Any]):
    state = _load_gen2_shadow_monitor_state()
    if isinstance(payload, dict):
        if "enabled" in payload:
            state["enabled"] = bool(payload.get("enabled"))
        if "official_rebuild_enabled" in payload:
            state["official_rebuild_enabled"] = bool(payload.get("official_rebuild_enabled"))
        if "official_rebuild_legacy_330" in payload:
            state["official_rebuild_legacy_330"] = bool(payload.get("official_rebuild_legacy_330"))
        if "official_rebuild_include_breakout" in payload:
            state["official_rebuild_include_breakout"] = bool(payload.get("official_rebuild_include_breakout"))
        if "official_rebuild_timeout_seconds" in payload:
            state["official_rebuild_timeout_seconds"] = max(300, _to_int(payload.get("official_rebuild_timeout_seconds"), 10800))
        if "official_rebuild_retry_minutes" in payload:
            state["official_rebuild_retry_minutes"] = max(1, _to_int(payload.get("official_rebuild_retry_minutes"), 10))
        if "interval_seconds" in payload:
            state["interval_seconds"] = max(120, _to_int(payload.get("interval_seconds"), 120))
        if "pool_rank" in payload:
            state["pool_rank"] = min(500, max(50, _to_int(payload.get("pool_rank"), 200)))
        if "alpha191_gate" in payload:
            alpha191_gate = str(payload.get("alpha191_gate") or "off").strip().lower()
            state["alpha191_gate"] = alpha191_gate if alpha191_gate in GEN2_ALPHA191_ACTIVE_GATES else "off"
        if "recipient_email" in payload:
            state["recipient_email"] = str(payload.get("recipient_email") or "").strip()
        if "trading_hours_only" in payload:
            state["trading_hours_only"] = bool(payload.get("trading_hours_only"))
        if "heartbeat_enabled" in payload:
            state["heartbeat_enabled"] = bool(payload.get("heartbeat_enabled"))
        if "heartbeat_email_minutes" in payload:
            state["heartbeat_email_minutes"] = max(30, _to_int(payload.get("heartbeat_email_minutes"), 30))
    _save_gen2_shadow_monitor_state(state)
    return _sanitize(_configure_gen2_shadow_buy_monitor_scheduler())


@router.post("/gen2/shadow-monitor/run-once")
def run_gen2_shadow_buy_monitor_once(payload: Dict[str, Any]):
    recipient_email = None
    force_send = False
    if isinstance(payload, dict):
        txt = str(payload.get("recipient_email") or "").strip()
        recipient_email = txt if txt else None
        force_send = bool(payload.get("force_send"))
    result = _run_gen2_shadow_buy_monitor(force_send=force_send, recipient_override=recipient_email)
    return _sanitize(result)


@router.get("/gen2/strategy-refresh/status")
def get_gen2_strategy_refresh_status():
    return _sanitize(_configure_gen2_strategy_refresh_scheduler())


@router.post("/gen2/strategy-refresh/config")
def set_gen2_strategy_refresh_config(payload: Dict[str, Any]):
    state = _load_gen2_strategy_refresh_state()
    if isinstance(payload, dict):
        if "enabled" in payload:
            state["enabled"] = bool(payload.get("enabled"))
        if "trading_hours_only" in payload:
            state["trading_hours_only"] = bool(payload.get("trading_hours_only"))
        if "data_delay_minutes" in payload:
            state["data_delay_minutes"] = min(20, max(0, _to_int(payload.get("data_delay_minutes"), 2)))
        if "run_shadow_monitor" in payload:
            state["run_shadow_monitor"] = bool(payload.get("run_shadow_monitor"))
        if "run_mainline_hotspots" in payload:
            state["run_mainline_hotspots"] = bool(payload.get("run_mainline_hotspots"))
        if "mainline_mode" in payload:
            mode = str(payload.get("mainline_mode") or "sector").strip().lower()
            state["mainline_mode"] = mode if mode in {"all", "sector", "theme"} else "sector"
        if "mainline_limit" in payload:
            state["mainline_limit"] = min(120, max(5, _to_int(payload.get("mainline_limit"), 30)))
        if "force_each_bar_once" in payload:
            state["force_each_bar_once"] = bool(payload.get("force_each_bar_once"))
    _save_gen2_strategy_refresh_state(state)
    return _sanitize(_configure_gen2_strategy_refresh_scheduler())


@router.post("/gen2/strategy-refresh/run-once")
def run_gen2_strategy_refresh_once(payload: Dict[str, Any]):
    force = True
    if isinstance(payload, dict) and "force" in payload:
        force = bool(payload.get("force"))
    return _sanitize(_run_gen2_strategy_refresh_30m(force=force, source="manual"))


@router.get("/v4/manual-holdings/monitor/status")
def get_v4_manual_holdings_monitor_status():
    return _sanitize(_configure_v4_monitor_scheduler())


@router.post("/v4/manual-holdings/monitor/config")
def set_v4_manual_holdings_monitor_config(payload: Dict[str, Any]):
    state = _load_v4_monitor_state()
    if isinstance(payload, dict):
        if "enabled" in payload:
            state["enabled"] = bool(payload.get("enabled"))
        if "interval_seconds" in payload:
            state["interval_seconds"] = max(30, _to_int(payload.get("interval_seconds"), 60))
        if "recipient_email" in payload:
            state["recipient_email"] = str(payload.get("recipient_email") or "").strip()
        if "trading_hours_only" in payload:
            state["trading_hours_only"] = bool(payload.get("trading_hours_only"))
        if "quiet_minutes" in payload:
            state["quiet_minutes"] = max(0, _to_int(payload.get("quiet_minutes"), 15))
        if "repeat_reminder_minutes" in payload:
            state["repeat_reminder_minutes"] = max(60, _to_int(payload.get("repeat_reminder_minutes"), 60))
        holdings = payload.get("holdings")
        if isinstance(holdings, list):
            state["holdings"] = holdings
            state["reminder_active"] = False
            state["reminder_activated_at"] = None
            state["last_alert_by_code"] = {}
    _save_v4_monitor_state(state)
    return _sanitize(_configure_v4_monitor_scheduler())


@router.post("/v4/manual-holdings/monitor/run-once")
def run_v4_manual_holdings_monitor_once(payload: Dict[str, Any]):
    recipient_email = None
    force_send = False
    if isinstance(payload, dict):
        txt = str(payload.get("recipient_email") or "").strip()
        recipient_email = txt if txt else None
        force_send = bool(payload.get("force_send"))
    result = _run_v4_manual_holdings_monitor(force_send=force_send, recipient_override=recipient_email)
    return _sanitize(result)


@router.post("/v4/manual-holdings/ths-current-table")
def read_v4_manual_holdings_ths_current_table():
    return _sanitize(_read_ths_current_table_text())


@router.post("/v4/manual-holdings/ths-delivery-file")
def read_v4_manual_holdings_ths_delivery_file():
    return _sanitize(_read_ths_delivery_file_text())


@router.post("/v4/manual-holdings/ths-capital-holdings")
def read_v4_manual_holdings_ths_capital_holdings():
    return _sanitize(_read_ths_capital_holdings_text())


@router.get("/v4/manual-holdings/entry-backtest")
def get_v4_manual_holding_entry_backtest(
    code: str = Query(..., description="6位股票代码"),
    strategy_version: Optional[str] = Query(None, description="策略版本"),
    max_samples: int = Query(400, ge=50, le=3000, description="朢大样本数"),
):
    code6 = str(code or "").strip()[:6]
    if not re.fullmatch(r"\d{6}", code6):
        return _sanitize({"ok": False, "message": "股票代码无效"})

    sv = str(strategy_version or DEFAULT_LIVE_STRATEGY_VERSION or "v5").strip()
    try:
        with db.engine.connect() as conn:
            signal_df = pd.read_sql(
                text(
                    f"""
                    SELECT signal_date, score_total, score_rank
                    FROM {TRADING_SELECTION_SCORE_TABLE}
                    WHERE strategy_version = :sv
                      AND code = :code
                      AND passed_entry_min_score = 1
                    ORDER BY signal_date DESC
                    LIMIT :max_samples
                    """
                ),
                conn,
                params={"sv": sv, "code": code6, "max_samples": int(max_samples)},
            )
            price_df = pd.read_sql(
                text(
                    """
                    SELECT trade_date, close
                    FROM kline_daily
                    WHERE substr(code, 1, 6) = :code
                    ORDER BY trade_date ASC
                    """
                ),
                conn,
                params={"code": code6},
            )
    except Exception as exc:
        logger.warning(f"manual holding entry backtest load failed: code={code6}, error={exc}")
        return _sanitize({"ok": False, "message": f"读取历史数据失败: {exc}"})

    if signal_df is None or signal_df.empty:
        return _sanitize(
            {
                "ok": True,
                "code": code6,
                "strategy_version": sv,
                "sample_count": 0,
                "horizons": {},
                "message": "该股在当前策略版本下暂无历史入场样本",
            }
        )
    if price_df is None or price_df.empty:
        return _sanitize({"ok": False, "message": "缺少日线数据"})

    signal_df = signal_df.copy()
    price_df = price_df.copy()
    signal_df["signal_date"] = pd.to_datetime(signal_df["signal_date"], errors="coerce").dt.date
    price_df["trade_date"] = pd.to_datetime(price_df["trade_date"], errors="coerce").dt.date
    price_df["close"] = pd.to_numeric(price_df["close"], errors="coerce")
    price_df = price_df.dropna(subset=["trade_date", "close"]).reset_index(drop=True)
    if price_df.empty:
        return _sanitize({"ok": False, "message": "行情数据为空，无法计算回测。"})

    closes = price_df["close"].tolist()
    date_to_idx: Dict[Any, int] = {d: i for i, d in enumerate(price_df["trade_date"].tolist())}

    horizons = [3, 5, 10]
    metrics: Dict[str, Dict[str, Any]] = {}
    realized_rows: List[Dict[str, Any]] = []
    valid_sample_count = 0

    for _, row in signal_df.iterrows():
        d = row.get("signal_date")
        idx = date_to_idx.get(d)
        if idx is None:
            continue
        valid_sample_count += 1
        row_item: Dict[str, Any] = {
            "signal_date": str(d),
            "score_total": _to_float(row.get("score_total")),
            "score_rank": _to_int(row.get("score_rank"), 0),
        }
        base = closes[idx]
        for n in horizons:
            key = f"d{n}"
            if idx + n < len(closes) and base and base > 0:
                ret = (closes[idx + n] / base - 1.0) * 100.0
                row_item[key] = ret
                metrics.setdefault(key, {"values": []})["values"].append(ret)
            else:
                row_item[key] = None
        realized_rows.append(row_item)

    horizon_stats: Dict[str, Any] = {}
    for n in horizons:
        key = f"d{n}"
        vals = metrics.get(key, {}).get("values", [])
        if not vals:
            horizon_stats[key] = {"sample_count": 0, "win_rate": None, "avg_return": None}
            continue
        arr = np.array(vals, dtype=float)
        win_rate = float((arr > 0).sum() / len(arr) * 100.0)
        avg_return = float(arr.mean())
        horizon_stats[key] = {
            "sample_count": int(len(arr)),
            "win_rate": round(win_rate, 2),
            "avg_return": round(avg_return, 2),
        }

    realized_rows = sorted(realized_rows, key=lambda x: str(x.get("signal_date") or ""), reverse=True)[:10]
    return _sanitize(
        {
            "ok": True,
            "code": code6,
            "strategy_version": sv,
            "sample_count": int(valid_sample_count),
            "horizons": horizon_stats,
            "recent_samples": realized_rows,
        }
    )

