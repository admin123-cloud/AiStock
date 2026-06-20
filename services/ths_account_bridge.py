from __future__ import annotations

import csv
import os
import re
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

try:
    from utils.paths import runtime_path
except Exception:  # pragma: no cover
    runtime_path = None


def _now_text() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def _runtime_export_dir() -> Path:
    if callable(runtime_path):
        return runtime_path("ths_exports")
    return Path(r"F:\Stock\AiStockData\data\runtime\ths_exports")


@contextmanager
def _ths_window_operation_lock(timeout_seconds: int = 120):
    lock_path = _runtime_export_dir() / "ths_window.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("a+b")
    locked = False
    try:
        try:
            import msvcrt  # type: ignore

            deadline = time.time() + max(1, timeout_seconds)
            while time.time() < deadline:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    time.sleep(0.2)
            if not locked:
                raise TimeoutError(f"THS_WINDOW_LOCK_TIMEOUT: {lock_path}")
            yield
        except ImportError:
            yield
    finally:
        if locked:
            try:
                import msvcrt  # type: ignore

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        fh.close()


def _parse_number(value: Any) -> float | None:
    text = str(value or "").replace(",", "").strip()
    if not text or text in {"--", "-"}:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except Exception:
        return None


def _split_row(line: str) -> list[str]:
    raw = str(line or "").rstrip("\r\n")
    if not raw.strip():
        return []
    if "\t" in raw:
        parts = [part.strip() for part in raw.split("\t")]
        while parts and parts[-1] == "":
            parts.pop()
        return parts
    text = raw.strip()
    return [part.strip() for part in re.split(r"\s{2,}", text) if part.strip()]


def _read_text_file(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk", "utf-16", "utf-16le"):
        try:
            text = path.read_text(encoding=encoding)
        except Exception:
            continue
        if text.strip():
            return text
    return ""


def _auto_export_enabled() -> bool:
    return str(os.environ.get("AISTOCK_THS_ALLOW_UI_EXPORT") or "").strip().lower() in {"1", "true", "yes", "on"}


def _save_current_grid_to_file(output_path: Path) -> Path:
    from pywinauto import mouse
    from pywinauto.keyboard import send_keys

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)
    target = _find_xiadan_window()
    try:
        target.restore()
    except Exception:
        pass
    target.set_focus()
    time.sleep(0.3)
    rect = target.rectangle()

    # Right-click the main virtual grid and choose "保存(S)..."; this uses the
    # broker's built-in export path and does not touch clipboard/copy.
    mouse.click(button="right", coords=(rect.left + 480, rect.top + 200))
    time.sleep(0.4)
    mouse.click(button="left", coords=(rect.left + 510, rect.top + 376))
    time.sleep(1.0)

    # The Save As panel is embedded in the xiadan window. THS/Windows can mangle
    # a full path when it is typed with send_keys, so keep the dialog in its
    # remembered export directory and enter only the ASCII filename.
    mouse.click(button="left", coords=(rect.left + 519, rect.top + 528))
    time.sleep(0.2)
    send_keys("^a")
    time.sleep(0.1)
    send_keys(output_path.name, with_spaces=True)
    time.sleep(0.2)
    mouse.click(button="left", coords=(rect.left + 953, rect.top + 598))
    time.sleep(1.5)
    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError(f"THS_UI_EXPORT_FAILED: {output_path}")
    return output_path


def _click_query_menu_item(item: str) -> None:
    from pywinauto import mouse

    target = _find_xiadan_window()
    try:
        target.restore()
    except Exception:
        pass
    target.set_focus()
    time.sleep(0.3)
    rect = target.rectangle()
    menu_y_by_item = {
        "holdings": 287,
        "today_trades": 315,
        "history_trades": 371,
        "delivery": 455,
    }
    rel_y = menu_y_by_item.get(item)
    if rel_y is None:
        raise ValueError(f"unsupported THS query menu item: {item}")
    mouse.click(button="left", coords=(rect.left + 82, rect.top + rel_y))
    time.sleep(1.2)


def _try_auto_export_holdings_file() -> Path | None:
    if not _auto_export_enabled():
        return None
    output_path = _runtime_export_dir() / "ths_holdings_auto.txt"
    try:
        _click_query_menu_item("holdings")
        return _save_current_grid_to_file(output_path)
    except Exception:
        return None


def _try_auto_export_trades_file() -> Path | None:
    if not _auto_export_enabled():
        return None
    output_path = _runtime_export_dir() / "ths_history_auto.txt"
    try:
        _click_query_menu_item("history_trades")
        return _save_current_grid_to_file(output_path)
    except Exception:
        return None


def _configured_paths(env_name: str) -> list[str]:
    return [
        item.strip()
        for item in str(os.environ.get(env_name) or "").split(";")
        if item.strip()
    ]


def _candidate_export_files() -> list[Path]:
    runtime_dir = _runtime_export_dir()
    defaults = [
        runtime_dir / "资金持仓.txt",
        runtime_dir / "资金持仓.csv",
        runtime_dir / "持仓明细.txt",
        runtime_dir / "持仓明细.csv",
        runtime_dir / "证券持仓.txt",
        runtime_dir / "证券持仓.csv",
        Path(r"D:\THS_资金持仓.txt"),
        Path(r"D:\THS_资金持仓.csv"),
        Path(r"D:\THS_持仓明细.txt"),
        Path(r"D:\THS_持仓明细.csv"),
        Path(r"D:\THS_证券持仓.txt"),
        Path(r"D:\THS_证券持仓.csv"),
        runtime_dir / "ths_holdings_auto.txt",
        runtime_dir / "ths_holdings_auto.csv",
    ]
    return [Path(item) for item in _configured_paths("AISTOCK_THS_HOLDINGS_EXPORT_FILE")] + defaults


def _candidate_trade_export_files() -> list[Path]:
    runtime_dir = _runtime_export_dir()
    defaults = [
        runtime_dir / "历史成交.txt",
        runtime_dir / "历史成交.csv",
        runtime_dir / "当日成交.txt",
        runtime_dir / "当日成交.csv",
        runtime_dir / "成交明细.txt",
        runtime_dir / "成交明细.csv",
        runtime_dir / "交割单.txt",
        runtime_dir / "交割单.csv",
        Path(r"D:\THS_历史成交.txt"),
        Path(r"D:\THS_历史成交.csv"),
        Path(r"D:\THS_当日成交.txt"),
        Path(r"D:\THS_当日成交.csv"),
        Path(r"D:\THS_成交明细.txt"),
        Path(r"D:\THS_成交明细.csv"),
        Path(r"D:\THS_交割单.txt"),
        Path(r"D:\THS_交割单.csv"),
        Path(r"D:\THS_交割单_1.txt"),
        runtime_dir / "ths_history_auto.txt",
        runtime_dir / "ths_history_auto.csv",
        runtime_dir / "ths_trades_auto.txt",
        runtime_dir / "ths_trades_auto.csv",
    ]
    return [Path(item) for item in _configured_paths("AISTOCK_THS_TRADES_EXPORT_FILE")] + defaults


def _extract_numeric_after_labels(raw_text: str, labels: Iterable[str]) -> float | None:
    text = str(raw_text or "")
    label_list = list(labels)
    ratio_label = any("比" in label or "比例" in label for label in label_list)
    tokens = [tok.strip() for tok in re.split(r"[\t\r\n]+", text) if tok.strip()]
    for idx, token in enumerate(tokens[:-1]):
        normalized_token = re.sub(r"\s+", "", token)
        if any(re.sub(r"\s+", "", label) in normalized_token for label in label_list):
            value_idx = idx + 1
            if "%" in tokens[value_idx] and not ratio_label and value_idx + 1 < len(tokens):
                value_idx += 1
            value = _parse_number(tokens[value_idx])
            if value is not None:
                return value
    for label in label_list:
        pattern = rf"{re.escape(label)}[^\d\-]{{0,20}}(-?\d[\d,]*(?:\.\d+)?)"
        match = re.search(pattern, text)
        if match:
            value = _parse_number(match.group(1))
            if value is not None:
                return value
    return None


def _find_header_index(headers: list[str], candidates: Iterable[str]) -> int:
    for idx, header in enumerate(headers):
        normalized = str(header or "").replace(" ", "")
        if any(candidate in normalized for candidate in candidates):
            return idx
    return -1


def _normalize_code6(value: Any) -> str:
    raw = str(value or "").strip().upper()
    match = re.search(r"\d{6}", raw)
    return match.group(0) if match else ""


def _normalize_trade_datetime(date_value: Any, time_value: Any = "") -> str | None:
    date_text = str(date_value or "").strip()
    time_text = str(time_value or "").strip()
    if not date_text:
        return None
    date_digits = re.sub(r"\D", "", date_text)
    if len(date_digits) >= 8:
        date_norm = f"{date_digits[:4]}-{date_digits[4:6]}-{date_digits[6:8]}"
    else:
        try:
            date_norm = datetime.fromisoformat(date_text.replace("/", "-")[:10]).strftime("%Y-%m-%d")
        except Exception:
            return None
    if time_text:
        time_digits = re.sub(r"\D", "", time_text)
        if len(time_digits) >= 6:
            time_norm = f"{time_digits[:2]}:{time_digits[2:4]}:{time_digits[4:6]}"
        elif len(time_digits) >= 4:
            time_norm = f"{time_digits[:2]}:{time_digits[2:4]}:00"
        else:
            time_norm = "00:00:00"
    else:
        time_norm = "00:00:00"
    return f"{date_norm} {time_norm}"


def _parse_trade_side(value: Any) -> str:
    text = str(value or "").strip().lower()
    if any(token in text for token in ("买", "买入", "证券买入", "buy")):
        return "BUY"
    if any(token in text for token in ("卖", "卖出", "证券卖出", "sell")):
        return "SELL"
    return ""


def _csv_text_to_tab_text(text: str) -> str:
    try:
        rows = list(csv.reader(text.splitlines()))
        return "\n".join("\t".join(cell.strip() for cell in row) for row in rows)
    except Exception:
        return text


def _is_auto_export_path(path: Path) -> bool:
    return path.name.lower() in {
        "ths_holdings_auto.txt",
        "ths_holdings_auto.csv",
        "ths_history_auto.txt",
        "ths_history_auto.csv",
        "ths_trades_auto.txt",
        "ths_trades_auto.csv",
    }


def _parse_trade_history_text(raw_text: str, source: str) -> dict[str, Any]:
    lines = [line.rstrip("\r") for line in str(raw_text or "").splitlines() if line.strip()]
    rows = [_split_row(line) for line in lines]
    header_idx = -1
    headers: list[str] = []
    for idx, row in enumerate(rows):
        compact = "".join(row)
        if ("成交" in compact and ("日期" in compact or "时间" in compact) and "代码" in compact) or (
            "证券代码" in compact and ("成交数量" in compact or "成交金额" in compact)
        ):
            header_idx = idx
            headers = row
            break
    if header_idx < 0:
        return {"ok": False, "message": "未识别到同花顺历史成交表头", "rows": [], "raw_line_count": len(lines)}

    date_idx = _find_header_index(headers, ["成交日期", "交易日期", "日期"])
    time_idx = _find_header_index(headers, ["成交时间", "交易时间", "时间"])
    code_idx = _find_header_index(headers, ["证券代码", "股票代码", "代码"])
    name_idx = _find_header_index(headers, ["证券名称", "股票名称", "名称"])
    action_idx = _find_header_index(headers, ["操作", "买卖", "业务名称", "委托类别"])
    shares_idx = _find_header_index(headers, ["成交数量", "成交股数", "数量", "发生数量"])
    price_idx = _find_header_index(headers, ["成交均价", "成交价格", "成交价", "价格", "均价"])
    amount_idx = _find_header_index(headers, ["成交金额", "发生金额", "金额"])
    remark_idx = _find_header_index(headers, ["备注", "说明"])

    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows[header_idx + 1 :]:
        if not row:
            continue
        code = _normalize_code6(row[code_idx] if 0 <= code_idx < len(row) else "")
        if not code:
            code = next((_normalize_code6(item) for item in row if _normalize_code6(item)), "")
        trade_time = _normalize_trade_datetime(
            row[date_idx] if 0 <= date_idx < len(row) else "",
            row[time_idx] if 0 <= time_idx < len(row) else "",
        )
        side = _parse_trade_side(row[action_idx] if 0 <= action_idx < len(row) else "")
        shares = int(abs(_parse_number(row[shares_idx] if 0 <= shares_idx < len(row) else "") or 0))
        price = _parse_number(row[price_idx] if 0 <= price_idx < len(row) else "")
        amount = _parse_number(row[amount_idx] if 0 <= amount_idx < len(row) else "")
        if not code or not trade_time or side not in {"BUY", "SELL"} or shares <= 0 or not price or price <= 0:
            continue
        item = {
            "trade_time": trade_time,
            "trade_date": trade_time[:10],
            "side": side,
            "side_label": "买入" if side == "BUY" else "卖出",
            "code": code,
            "name": str(row[name_idx]).strip() if 0 <= name_idx < len(row) else "",
            "shares": shares,
            "price": round(float(price), 3),
            "amount": round(float(amount), 3) if amount is not None else round(float(price) * shares, 3),
            "remark": str(row[remark_idx]).strip() if 0 <= remark_idx < len(row) else "",
            "source": source,
        }
        sig = "|".join([item["trade_time"], item["side"], item["code"], str(item["shares"]), str(item["price"])])
        if sig in seen:
            continue
        seen.add(sig)
        parsed.append(item)
    parsed.sort(key=lambda item: str(item.get("trade_time") or ""), reverse=True)
    return {
        "ok": True,
        "view_type": "trade_history",
        "view_type_desc": "历史成交",
        "rows": parsed,
        "parsed_count": len(parsed),
        "raw_line_count": len(lines),
        "preview_lines": lines[:16],
        "raw_text": "\n".join(lines),
        "synced_at": _now_text(),
        "source": source,
    }


def _parse_capital_holdings_text(raw_text: str, source: str) -> dict[str, Any]:
    lines = [line.rstrip("\r") for line in str(raw_text or "").splitlines() if line.strip()]
    rows = [_split_row(line) for line in lines]
    header_idx = -1
    headers: list[str] = []
    for idx, row in enumerate(rows):
        compact = "".join(row)
        if ("代码" in compact or "证券代码" in compact or "股票代码" in compact) and (
            "名称" in compact or "证券名称" in compact or "股票名称" in compact
        ):
            header_idx = idx
            headers = row
            break

    code_idx = _find_header_index(headers, ["证券代码", "股票代码", "代码"])
    name_idx = _find_header_index(headers, ["证券名称", "股票名称", "名称"])
    shares_idx = _find_header_index(headers, ["证券数量", "股票余额", "当前持仓", "持仓数量", "可用余额"])
    available_shares_idx = _find_header_index(headers, ["可用数量", "可卖数量", "股份可用", "可用余额"])
    market_idx = _find_header_index(headers, ["参考市值", "股票市值", "市值", "最新市值"])
    cost_idx = _find_header_index(headers, ["成本价", "摊薄成本", "保本价"])
    current_idx = _find_header_index(headers, ["市价", "最新价", "现价", "当前价"])
    holding_pnl_idx = _find_header_index(headers, ["持仓盈亏", "浮动盈亏", "盈亏"])
    pnl_idx = _find_header_index(headers, ["盈亏比例", "盈亏比", "持仓盈亏比例"])
    day_pnl_idx = _find_header_index(headers, ["当日盈亏"])
    position_pct_idx = _find_header_index(headers, ["仓位占比", "仓位比例", "仓位"])

    holdings: list[dict[str, Any]] = []
    if header_idx >= 0:
        for row in rows[header_idx + 1 :]:
            if not row:
                continue
            code = _normalize_code6(row[code_idx] if 0 <= code_idx < len(row) else "")
            if not code:
                code = next((_normalize_code6(item) for item in row if _normalize_code6(item)), "")
            if not code:
                continue
            name = str(row[name_idx]).strip() if 0 <= name_idx < len(row) else ""
            shares = _parse_number(row[shares_idx]) if 0 <= shares_idx < len(row) else None
            available_shares = _parse_number(row[available_shares_idx]) if 0 <= available_shares_idx < len(row) else None
            market_value = _parse_number(row[market_idx]) if 0 <= market_idx < len(row) else None
            cost_price = _parse_number(row[cost_idx]) if 0 <= cost_idx < len(row) else None
            current_price = _parse_number(row[current_idx]) if 0 <= current_idx < len(row) else None
            holding_pnl = _parse_number(row[holding_pnl_idx]) if 0 <= holding_pnl_idx < len(row) else None
            pnl_ratio = _parse_number(row[pnl_idx]) if 0 <= pnl_idx < len(row) else None
            day_pnl = _parse_number(row[day_pnl_idx]) if 0 <= day_pnl_idx < len(row) else None
            position_pct = _parse_number(row[position_pct_idx]) if 0 <= position_pct_idx < len(row) else None
            shares_int = int(shares or 0)
            if shares_int <= 0:
                continue
            if (current_price is None or current_price <= 0) and market_value and shares_int:
                current_price = market_value / shares_int
            holdings.append(
                {
                    "code": code,
                    "name": name,
                    "shares": shares_int,
                    "available_shares": int(available_shares or 0) if available_shares is not None else None,
                    "market_value": market_value,
                    "cost_price": cost_price,
                    "current_price": current_price,
                    "holding_pnl": holding_pnl,
                    "pnl_ratio": pnl_ratio,
                    "day_pnl": day_pnl,
                    "position_pct": (position_pct / 100.0) if position_pct is not None and position_pct > 1 else position_pct,
                }
            )

    holding_market_value = sum(float(item.get("market_value") or 0.0) for item in holdings) if holdings else None
    cost_value = (
        sum(float(item.get("cost_price") or 0.0) * int(item.get("shares") or 0) for item in holdings)
        if holdings
        else None
    )
    capital = {
        "fund_balance": _extract_numeric_after_labels(raw_text, ["资金余额"]),
        "available_cash": _extract_numeric_after_labels(raw_text, ["可用金额", "可用资金", "可用现金"]),
        "withdrawable_cash": _extract_numeric_after_labels(raw_text, ["可取金额"]),
        "frozen_cash": _extract_numeric_after_labels(raw_text, ["冻结金额"]),
        "market_value": _extract_numeric_after_labels(raw_text, ["股票市值", "参考市值", "持仓市值", "证券市值"]),
        "total_capital": _extract_numeric_after_labels(raw_text, ["总资产", "资产总值"]),
        "holding_pnl": _extract_numeric_after_labels(raw_text, ["持仓盈亏"]),
        "day_pnl": _extract_numeric_after_labels(raw_text, ["当日盈亏"]),
        "cost_value": cost_value,
    }
    row_holding_pnl = [float(item.get("holding_pnl")) for item in holdings if item.get("holding_pnl") is not None]
    row_day_pnl = [float(item.get("day_pnl")) for item in holdings if item.get("day_pnl") is not None]
    if row_holding_pnl:
        capital["holding_pnl"] = sum(row_holding_pnl)
    if row_day_pnl:
        capital["day_pnl"] = sum(row_day_pnl)
    if holding_market_value and (not capital.get("market_value") or capital["market_value"] < holding_market_value * 0.5):
        capital["market_value"] = holding_market_value
    if not capital.get("total_capital") and holdings:
        position_total_candidates = [
            float(item.get("market_value") or 0.0) / float(item.get("position_pct") or 0.0)
            for item in holdings
            if float(item.get("market_value") or 0.0) > 0 and float(item.get("position_pct") or 0.0) > 0
        ]
        if position_total_candidates:
            capital["total_capital"] = sum(position_total_candidates) / len(position_total_candidates)
    if not capital.get("total_capital") and capital.get("fund_balance") is not None and capital.get("market_value") is not None:
        capital["total_capital"] = float(capital["fund_balance"]) + float(capital["market_value"])
    if not capital.get("total_capital") and capital.get("available_cash") is not None and capital.get("market_value") is not None:
        capital["total_capital"] = float(capital["available_cash"]) + float(capital["market_value"])
    if not capital.get("available_cash") and capital.get("total_capital") is not None and capital.get("market_value") is not None:
        capital["available_cash"] = float(capital["total_capital"]) - float(capital["market_value"])

    return {
        "ok": bool(holdings and capital.get("market_value") is not None and capital.get("total_capital") is not None),
        "view_type": "capital_holdings",
        "view_type_desc": "资金持仓",
        "capital": {key: value for key, value in capital.items() if value is not None},
        "holdings": holdings,
        "preview_lines": lines[:16],
        "raw_text": "\n".join(lines),
        "synced_at": _now_text(),
        "fallback_cache": False,
        "source": source,
    }


def _ocr_enabled() -> bool:
    return str(os.environ.get("AISTOCK_THS_DISABLE_OCR") or "").strip().lower() not in {"1", "true", "yes", "on"}


def _ocr_xiadan_window() -> tuple[list[dict[str, Any]], list[str], str]:
    from PIL import ImageGrab
    from rapidocr_onnxruntime import RapidOCR

    target = _find_xiadan_window()
    try:
        target.restore()
    except Exception:
        pass
    target.set_focus()
    time.sleep(0.3)
    rect = target.rectangle()
    img = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))
    screenshot_path = _runtime_export_dir() / "ths_ocr_window.png"
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(screenshot_path)

    result, _ = RapidOCR()(str(screenshot_path))
    items: list[dict[str, Any]] = []
    for row in result or []:
        if len(row) < 3:
            continue
        box, text, score = row[0], str(row[1] or "").strip(), float(row[2] or 0.0)
        if not text:
            continue
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        items.append(
            {
                "x": sum(xs) / len(xs),
                "y": sum(ys) / len(ys),
                "text": text,
                "score": score,
            }
        )
    lines = _ocr_items_to_lines(items)
    return items, lines, str(screenshot_path)


def _ocr_items_to_lines(items: list[dict[str, Any]], row_delta: float = 9.0) -> list[str]:
    grouped: list[list[dict[str, Any]]] = []
    for item in sorted(items, key=lambda obj: (float(obj.get("y") or 0.0), float(obj.get("x") or 0.0))):
        y = float(item.get("y") or 0.0)
        if not grouped:
            grouped.append([item])
            continue
        last_y = sum(float(obj.get("y") or 0.0) for obj in grouped[-1]) / max(1, len(grouped[-1]))
        if abs(y - last_y) <= row_delta:
            grouped[-1].append(item)
        else:
            grouped.append([item])
    lines: list[str] = []
    for group in grouped:
        cells = [str(obj.get("text") or "").strip() for obj in sorted(group, key=lambda obj: float(obj.get("x") or 0.0))]
        line = "\t".join(cell for cell in cells if cell)
        if line:
            lines.append(line)
    return lines


def _ocr_group_rows(items: list[dict[str, Any]], row_delta: float = 9.0) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = []
    for item in sorted(items, key=lambda obj: (float(obj.get("y") or 0.0), float(obj.get("x") or 0.0))):
        y = float(item.get("y") or 0.0)
        if not rows:
            rows.append([item])
            continue
        avg_y = sum(float(obj.get("y") or 0.0) for obj in rows[-1]) / max(1, len(rows[-1]))
        if abs(y - avg_y) <= row_delta:
            rows[-1].append(item)
        else:
            rows.append([item])
    return [sorted(row, key=lambda obj: float(obj.get("x") or 0.0)) for row in rows]


def _ocr_text_in_range(row: list[dict[str, Any]], x_min: float, x_max: float) -> str:
    parts = [
        str(item.get("text") or "").strip()
        for item in row
        if x_min <= float(item.get("x") or 0.0) <= x_max and str(item.get("text") or "").strip()
    ]
    return "".join(parts).strip()


def _parse_holdings_from_ocr_items(items: list[dict[str, Any]], capital: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    rows = _ocr_group_rows(items)
    header_y = 240.0
    for row in rows:
        text = "".join(str(item.get("text") or "") for item in row)
        if "股票余额" in text or ("证券代码" in text and "市值" in text):
            header_y = sum(float(item.get("y") or 0.0) for item in row) / max(1, len(row))
            break

    holdings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        avg_y = sum(float(item.get("y") or 0.0) for item in row) / max(1, len(row))
        if avg_y <= header_y + 8 or avg_y > 680:
            continue
        row_text = "\t".join(str(item.get("text") or "") for item in row)
        code = next((_normalize_code6(item.get("text")) for item in row if _normalize_code6(item.get("text"))), "")
        if not code or code in seen:
            continue
        shares = _parse_number(_ocr_text_in_range(row, 400, 465))
        available_shares = _parse_number(_ocr_text_in_range(row, 470, 535))
        market_value = _parse_number(_ocr_text_in_range(row, 540, 610))
        cost_price = _parse_number(_ocr_text_in_range(row, 620, 680))
        current_price = _parse_number(_ocr_text_in_range(row, 690, 750))
        holding_pnl = _parse_number(_ocr_text_in_range(row, 760, 830))
        pnl_ratio = _parse_number(_ocr_text_in_range(row, 850, 925))
        day_pnl = _parse_number(_ocr_text_in_range(row, 930, 990))
        name = _ocr_text_in_range(row, 305, 390)
        if not name:
            name = next(
                (
                    str(item.get("text") or "").strip()
                    for item in row
                    if 295 <= float(item.get("x") or 0.0) <= 410 and not _normalize_code6(item.get("text"))
                ),
                "",
            )
        shares_int = int(shares or 0)
        if shares_int <= 0:
            continue
        if (current_price is None or current_price <= 0) and market_value:
            current_price = float(market_value) / shares_int
        total_capital = _parse_number((capital or {}).get("total_capital"))
        position_pct = (float(market_value) / total_capital) if market_value and total_capital else None
        holdings.append(
            {
                "code": code,
                "name": name,
                "shares": shares_int,
                "available_shares": int(available_shares or 0) if available_shares is not None else None,
                "market_value": market_value,
                "cost_price": cost_price,
                "current_price": current_price,
                "holding_pnl": holding_pnl,
                "pnl_ratio": pnl_ratio,
                "day_pnl": day_pnl,
                "position_pct": position_pct,
                "source": "ths_ocr_snapshot",
                "ocr_row_text": row_text,
            }
        )
        seen.add(code)
    return holdings


def _parse_trades_from_ocr_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = _ocr_group_rows(items)
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        avg_y = sum(float(item.get("y") or 0.0) for item in row) / max(1, len(row))
        if avg_y < 135 or avg_y > 680:
            continue
        date_text = _ocr_text_in_range(row, 220, 270)
        if not re.fullmatch(r"\d{8}", re.sub(r"\D", "", date_text or "")):
            continue
        trade_time = _normalize_trade_datetime(date_text, _ocr_text_in_range(row, 285, 340))
        code = _normalize_code6(_ocr_text_in_range(row, 345, 400)) or next(
            (_normalize_code6(item.get("text")) for item in row if _normalize_code6(item.get("text"))),
            "",
        )
        side_text = _ocr_text_in_range(row, 465, 540)
        side = _parse_trade_side(side_text)
        shares = int(abs(_parse_number(_ocr_text_in_range(row, 550, 610)) or 0))
        price = _parse_number(_ocr_text_in_range(row, 620, 680))
        amount = _parse_number(_ocr_text_in_range(row, 690, 760))
        if not trade_time or not code or side not in {"BUY", "SELL"} or shares <= 0 or not price:
            continue
        item = {
            "trade_time": trade_time,
            "trade_date": trade_time[:10],
            "side": side,
            "side_label": "买入" if side == "BUY" else "卖出",
            "code": code,
            "name": _ocr_text_in_range(row, 405, 465),
            "shares": shares,
            "price": round(float(price), 3),
            "amount": round(float(amount), 3) if amount is not None else round(float(price) * shares, 3),
            "remark": side_text,
            "source": "ths_trade_ocr_snapshot",
            "ocr_row_text": "\t".join(str(obj.get("text") or "") for obj in row),
        }
        sig = "|".join([item["trade_time"], item["side"], item["code"], str(item["shares"]), str(item["price"])])
        if sig in seen:
            continue
        seen.add(sig)
        parsed.append(item)
    parsed.sort(key=lambda item: str(item.get("trade_time") or ""), reverse=True)
    return parsed


def _read_from_ths_ocr_holdings() -> dict[str, Any]:
    _click_query_menu_item("holdings")
    items, lines, screenshot_path = _ocr_xiadan_window()
    uia_result: dict[str, Any] = {}
    try:
        uia_result = _read_from_ths_uia_snapshot()
    except Exception:
        uia_result = {}
    capital = uia_result.get("capital") if isinstance(uia_result.get("capital"), dict) else {}
    holdings = _parse_holdings_from_ocr_items(items, capital=capital)
    if not holdings:
        raise RuntimeError("EMPTY_THS_OCR_HOLDINGS")
    parsed = _parse_capital_holdings_text("\n".join(lines), source="ths_ocr_snapshot")
    if capital:
        parsed["capital"] = capital
    parsed["holdings"] = holdings
    parsed["ok"] = True
    parsed["source"] = "ths_ocr_snapshot"
    parsed["capital_only"] = False
    parsed["screenshot_path"] = screenshot_path
    parsed["ocr_line_count"] = len(lines)
    parsed["message"] = "OCR截图识别到同花顺资金持仓表。"
    return parsed


def _read_from_ths_ocr_trades() -> dict[str, Any]:
    _click_query_menu_item("history_trades")
    items, lines, screenshot_path = _ocr_xiadan_window()
    trades = _parse_trades_from_ocr_items(items)
    if not trades:
        raise RuntimeError("EMPTY_THS_OCR_TRADES")
    return {
        "ok": True,
        "view_type": "trade_history",
        "view_type_desc": "历史成交",
        "rows": trades,
        "parsed_count": len(trades),
        "raw_line_count": len(lines),
        "preview_lines": lines[:16],
        "raw_text": "\n".join(lines),
        "synced_at": _now_text(),
        "source": "ths_trade_ocr_snapshot",
        "screenshot_path": screenshot_path,
        "ocr_line_count": len(lines),
        "message": "OCR截图识别到同花顺历史成交表。",
    }


def _read_from_export_file() -> dict[str, Any]:
    searched: list[str] = []
    for path in _candidate_export_files():
        searched.append(str(path))
        if _is_auto_export_path(path):
            continue
        if not path.exists() or not path.is_file():
            continue
        text = _read_text_file(path)
        if not text.strip():
            continue
        if path.suffix.lower() == ".csv" and "\t" not in text:
            text = _csv_text_to_tab_text(text)
        parsed = _parse_capital_holdings_text(text, source="ths_export_file")
        parsed["file_path"] = str(path)
        parsed["file_mtime"] = datetime.fromtimestamp(path.stat().st_mtime).isoformat(sep=" ", timespec="seconds")
        if parsed.get("ok"):
            return parsed
    exported = _try_auto_export_holdings_file()
    if exported is not None:
        searched.append(str(exported))
        text = _read_text_file(exported)
        if exported.suffix.lower() == ".csv" and "\t" not in text:
            text = _csv_text_to_tab_text(text)
        parsed = _parse_capital_holdings_text(text, source="ths_ui_export_file")
        parsed["file_path"] = str(exported)
        parsed["file_mtime"] = datetime.fromtimestamp(exported.stat().st_mtime).isoformat(sep=" ", timespec="seconds")
        if parsed.get("ok"):
            return parsed
    suffix = ""
    if not _auto_export_enabled():
        suffix = "；UI保存导出默认禁用，避免触发同花顺保存/复制保护验证码"
    return {"ok": False, "message": "未找到可解析的同花顺持仓导出文件" + suffix, "searched_paths": searched}


def read_ths_trade_history() -> dict[str, Any]:
    searched: list[str] = []
    errors: list[str] = []
    for path in _candidate_trade_export_files():
        searched.append(str(path))
        if _is_auto_export_path(path):
            continue
        if not path.exists() or not path.is_file():
            continue
        text = _read_text_file(path)
        if not text.strip():
            continue
        if path.suffix.lower() == ".csv" and "\t" not in text:
            text = _csv_text_to_tab_text(text)
        parsed = _parse_trade_history_text(text, source="ths_trade_export_file")
        parsed["file_path"] = str(path)
        parsed["file_mtime"] = datetime.fromtimestamp(path.stat().st_mtime).isoformat(sep=" ", timespec="seconds")
        if parsed.get("ok"):
            return parsed
    exported = _try_auto_export_trades_file()
    if exported is not None:
        searched.append(str(exported))
        text = _read_text_file(exported)
        if exported.suffix.lower() == ".csv" and "\t" not in text:
            text = _csv_text_to_tab_text(text)
        parsed = _parse_trade_history_text(text, source="ths_trade_ui_export_file")
        parsed["file_path"] = str(exported)
        parsed["file_mtime"] = datetime.fromtimestamp(exported.stat().st_mtime).isoformat(sep=" ", timespec="seconds")
        if parsed.get("ok"):
            return parsed
    if _ocr_enabled():
        try:
            with _ths_window_operation_lock():
                return _read_from_ths_ocr_trades()
        except Exception as exc:
            errors.append(f"ocr={exc}")
    return {
        "ok": False,
        "ready": False,
        "source": "ths_account_bridge",
        "message": "未找到可解析的同花顺历史成交导出文件"
        + ("；UI保存导出默认禁用，避免触发同花顺保存/复制保护验证码" if not _auto_export_enabled() else ""),
        "errors": errors,
        "searched_paths": searched,
        "updated_at": _now_text(),
    }


def _get_clipboard_text(timeout: int = 5) -> str:
    try:
        import win32clipboard  # type: ignore

        deadline = time.time() + max(1, timeout)
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                win32clipboard.OpenClipboard()
                try:
                    return str(win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT) or "").strip()
                finally:
                    win32clipboard.CloseClipboard()
            except Exception as exc:
                last_error = exc
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
                time.sleep(0.1)
        raise RuntimeError(f"GET_CLIPBOARD_WIN32_FAILED: {last_error}")
    except ImportError:
        pass

    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-STA",
            "-Command",
            "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Clipboard]::GetText()",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(str(proc.stderr or "").strip() or "GET_CLIPBOARD_FAILED")
    return str(proc.stdout or "").strip()


def _find_xiadan_window():
    from pywinauto import Application

    app = Application(backend="uia").connect(path="xiadan.exe", timeout=4)
    windows = []
    for win in app.windows():
        try:
            title = str(win.window_text() or "").strip()
        except Exception:
            title = ""
        if title:
            windows.append((title, win))
    for title, win in windows:
        if "网上股票交易系统" in title or "交易系统" in title or "xiadan" in title.lower():
            return win
    raise RuntimeError("NO_XIADAN_WINDOW")


def _read_from_ths_uia_snapshot() -> dict[str, Any]:
    target = _find_xiadan_window()
    rows_by_top: dict[int, list[tuple[int, str]]] = {}
    for ctrl in target.descendants():
        try:
            text = str(ctrl.window_text() or "").strip()
        except Exception:
            continue
        if not text:
            continue
        try:
            rect = ctrl.rectangle()
            top = int(round(rect.top / 6.0) * 6)
            left = int(rect.left)
        except Exception:
            top = len(rows_by_top) * 6
            left = 0
        rows_by_top.setdefault(top, []).append((left, text))

    lines: list[str] = []
    seen_lines: set[str] = set()
    for top in sorted(rows_by_top):
        cells = [text for _, text in sorted(rows_by_top[top], key=lambda item: item[0])]
        compact_cells: list[str] = []
        for cell in cells:
            if compact_cells and compact_cells[-1] == cell:
                continue
            compact_cells.append(cell)
        line = "\t".join(compact_cells).strip()
        if line and line not in seen_lines:
            seen_lines.add(line)
            lines.append(line)
    raw_text = "\n".join(lines)
    if not raw_text.strip():
        raise RuntimeError("EMPTY_THS_UIA_SNAPSHOT")
    parsed = _parse_capital_holdings_text(raw_text, source="ths_uia_snapshot")
    parsed["uia_line_count"] = len(lines)
    capital = parsed.get("capital") if isinstance(parsed.get("capital"), dict) else {}
    if not parsed.get("ok") and capital.get("total_capital") is not None and capital.get("market_value") is not None:
        parsed["ok"] = True
        parsed["capital_only"] = True
        parsed["message"] = "UIA 只读快照仅识别到资金区，未识别到完整持仓表；调用方应保留原持仓。"
        return parsed
    if not parsed.get("ok"):
        raise RuntimeError("INVALID_THS_UIA_SNAPSHOT_PARSE")
    return parsed


def _read_from_ths_clipboard() -> dict[str, Any]:
    from pywinauto.keyboard import send_keys

    target = _find_xiadan_window()
    target.set_focus()
    time.sleep(0.3)

    clicked_menu = False
    for title in ("资金持仓", "持仓", "股份持仓", "查询资金股票"):
        try:
            item = target.child_window(title=title, control_type="TreeItem")
            if item.exists(timeout=1):
                item.click_input()
                clicked_menu = True
                time.sleep(0.8)
                break
        except Exception:
            continue
    if not clicked_menu:
        time.sleep(0.3)

    clicked_grid = False
    for kwargs in (
        {"title": "Custom1", "auto_id": "1047", "control_type": "Pane"},
        {"title": "Custom1", "control_type": "Pane"},
        {"control_type": "DataGrid"},
        {"control_type": "Table"},
    ):
        try:
            grid = target.child_window(**kwargs)
            if grid.exists(timeout=1):
                grid.click_input(coords=(220, 35))
                clicked_grid = True
                time.sleep(0.2)
                break
        except Exception:
            continue
    if not clicked_grid:
        target.click_input(coords=(420, 300))
        time.sleep(0.2)

    send_keys("^a")
    time.sleep(0.2)
    send_keys("^c")
    time.sleep(0.6)
    text = _get_clipboard_text(timeout=6)
    if not text:
        raise RuntimeError("EMPTY_THS_CLIPBOARD")
    parsed = _parse_capital_holdings_text(text, source="ths_clipboard")
    if not parsed.get("ok"):
        raise RuntimeError("INVALID_THS_CLIPBOARD_PARSE")
    return parsed


def read_ths_capital_holdings() -> dict[str, Any]:
    errors: list[str] = []
    file_result = _read_from_export_file()
    if file_result.get("ok"):
        return file_result
    errors.append(f"export_file={file_result.get('message')}")

    exported = _try_auto_export_holdings_file()
    if exported is not None:
        text = _read_text_file(exported)
        if exported.suffix.lower() == ".csv" and "\t" not in text:
            text = _csv_text_to_tab_text(text)
        parsed = _parse_capital_holdings_text(text, source="ths_ui_export_file")
        parsed["file_path"] = str(exported)
        parsed["file_mtime"] = datetime.fromtimestamp(exported.stat().st_mtime).isoformat(sep=" ", timespec="seconds")
        if parsed.get("ok"):
            return parsed
        errors.append("ui_export=INVALID_THS_HOLDINGS_EXPORT_PARSE")

    if _ocr_enabled():
        try:
            with _ths_window_operation_lock():
                return _read_from_ths_ocr_holdings()
        except Exception as exc:
            errors.append(f"ocr={exc}")

    allow_uia = str(os.environ.get("AISTOCK_THS_DISABLE_UIA_READ") or "").strip().lower() not in {"1", "true", "yes", "on"}
    if allow_uia:
        try:
            with _ths_window_operation_lock():
                _click_query_menu_item("holdings")
                return _read_from_ths_uia_snapshot()
        except Exception as exc:
            errors.append(f"uia_snapshot={exc}")

    return {
        "ok": False,
        "ready": False,
        "source": "ths_account_bridge",
        "message": "实时读取同花顺资金持仓失败: " + "; ".join([*errors, "clipboard=disabled_to_avoid_ths_copy_protection"]),
        "updated_at": _now_text(),
    }
