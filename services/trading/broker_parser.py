"""Domain transformations; no scheduler, notification or order side effects."""
import re
import pandas as pd
from typing import Any
from strategies.contracts import formal_g3_score88_contract

def _normalize_code6(value: Any) -> str:
    raw = str(value or "").strip().upper()
    hit = re.search(r"\d{6}", raw)
    return hit.group(0) if hit else ""


def _split_table_row(line: str) -> list[str]:
    raw = str(line or "").rstrip("\r\n")
    if not raw.strip():
        return []
    if "\t" in raw:
        return [part.strip() for part in raw.split("\t")]
    return [part.strip() for part in re.split(r"\s{2,}", raw.strip()) if part.strip()]


def _parse_broker_number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except Exception:
        return None


def _normalize_trade_datetime(date_text: Any, time_text: Any = "") -> str | None:
    d = str(date_text or "").strip()
    t = str(time_text or "").strip()
    if not d:
        return None
    digits = re.sub(r"\D", "", d)
    if len(digits) >= 8:
        d = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    try:
        if t:
            t_digits = re.sub(r"\D", "", t)
            if len(t_digits) >= 6:
                t = f"{t_digits[:2]}:{t_digits[2:4]}:{t_digits[4:6]}"
            elif len(t_digits) >= 4:
                t = f"{t_digits[:2]}:{t_digits[2:4]}:00"
            return pd.Timestamp(f"{d} {t}").strftime("%Y-%m-%d %H:%M:%S")
        return pd.Timestamp(d).strftime("%Y-%m-%d 00:00:00")
    except Exception:
        return None


def _parse_trade_side(action: Any) -> str:
    text = str(action or "").strip().lower()
    if "买" in text or "buy" in text:
        return "BUY"
    if "卖" in text or "sell" in text:
        return "SELL"
    return ""


def _trade_signature(row: dict[str, Any]) -> str:
    return "|".join(
        [
            str(row.get("trade_time") or ""),
            str(row.get("side") or ""),
            str(row.get("code") or ""),
            str(row.get("shares") or ""),
            str(row.get("price") or ""),
        ]
    )


def _parse_broker_trade_text(raw_text: str) -> dict[str, Any]:
    lines = [line.rstrip("\r") for line in str(raw_text or "").splitlines() if str(line).strip()]
    rows = [_split_table_row(line) for line in lines]
    header_idx = -1
    headers: list[str] = []
    for idx, cells in enumerate(rows):
        compact = "|".join(cells)
        if ("成交" in compact and "日期" in compact and "代码" in compact and "操作" in compact):
            header_idx = idx
            headers = cells
            break
    if header_idx < 0:
        return {"ok": False, "message": "未识别到同花顺历史成交表头", "rows": [], "raw_line_count": len(lines)}

    def idx_of(*names: str) -> int:
        for i, header in enumerate(headers):
            text = str(header or "").replace(" ", "")
            if any(name in text for name in names):
                return i
        return -1

    date_idx = idx_of("成交日期", "日期")
    time_idx = idx_of("成交时间", "时间")
    code_idx = idx_of("证券代码", "股票代码", "代码")
    name_idx = idx_of("证券名称", "股票名称", "名称")
    action_idx = idx_of("操作", "买卖")
    shares_idx = idx_of("成交数量", "数量")
    price_idx = idx_of("成交均价", "成交价格", "价格", "均价")
    amount_idx = idx_of("成交金额", "金额")
    remark_idx = idx_of("备注")

    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cells in rows[header_idx + 1 :]:
        if not cells:
            continue
        code = _normalize_code6(cells[code_idx] if 0 <= code_idx < len(cells) else "")
        trade_time = _normalize_trade_datetime(
            cells[date_idx] if 0 <= date_idx < len(cells) else "",
            cells[time_idx] if 0 <= time_idx < len(cells) else "",
        )
        side = _parse_trade_side(cells[action_idx] if 0 <= action_idx < len(cells) else "")
        shares = int(abs(_parse_broker_number(cells[shares_idx] if 0 <= shares_idx < len(cells) else "") or 0))
        price = _parse_broker_number(cells[price_idx] if 0 <= price_idx < len(cells) else "")
        amount = _parse_broker_number(cells[amount_idx] if 0 <= amount_idx < len(cells) else "")
        if not code or not trade_time or side not in {"BUY", "SELL"} or shares <= 0 or not price or price <= 0:
            continue
        row = {
            "trade_time": trade_time,
            "trade_date": trade_time[:10],
            "side": side,
            "side_label": "买入" if side == "BUY" else "卖出",
            "code": code,
            "name": str(cells[name_idx]).strip() if 0 <= name_idx < len(cells) else "",
            "shares": shares,
            "price": round(float(price), 3),
            "amount": round(float(amount), 3) if amount is not None else round(float(price) * shares, 3),
            "remark": str(cells[remark_idx]).strip() if 0 <= remark_idx < len(cells) else "",
            "source": "ths_history_trade",
        }
        sig = _trade_signature(row)
        if sig in seen:
            continue
        seen.add(sig)
        parsed.append(row)
    parsed.sort(key=lambda item: str(item.get("trade_time") or ""), reverse=True)
    return {"ok": True, "rows": parsed, "parsed_count": len(parsed), "raw_line_count": len(lines)}


def _normalize_qmtmini_trade_time(value: Any) -> str | None:
    text = str(value or "").strip()
    digits = re.sub(r"\D", "", text)
    try:
        if len(digits) >= 14:
            return pd.Timestamp(
                f"{digits[:4]}-{digits[4:6]}-{digits[6:8]} {digits[8:10]}:{digits[10:12]}:{digits[12:14]}"
            ).strftime("%Y-%m-%d %H:%M:%S")
        if len(digits) == 8:
            return pd.Timestamp(f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}").strftime("%Y-%m-%d 00:00:00")
        if text:
            return pd.Timestamp(text).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None
    return None


def _parse_qmtmini_trade_side(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"23", "stock_buy"} or "buy" in text or "买" in text:
        return "BUY"
    if text in {"24", "stock_sell"} or "sell" in text or "卖" in text:
        return "SELL"
    return _parse_trade_side(value)



def _qmtmini_position_to_broker_holding(row: dict[str, Any]) -> dict[str, Any]:
    contract = formal_g3_score88_contract()
    exit_rules = contract["exit"]
    code = str(row.get("stock_code") or row.get("code") or "").strip().upper()
    code6 = _normalize_code6(code)
    shares = int(_parse_broker_number(row.get("volume")) or 0)
    available_shares = int(_parse_broker_number(row.get("can_use_volume")) or 0)
    cost_price = (
        _parse_broker_number(row.get("avg_price"))
        or _parse_broker_number(row.get("cost_price"))
        or _parse_broker_number(row.get("open_price"))
    )
    current_price = _parse_broker_number(row.get("last_price"))
    market_value = _parse_broker_number(row.get("market_value"))
    if current_price is None and market_value is not None and shares > 0:
        current_price = market_value / shares
    return {
        "code": code6,
        "code_raw": code,
        "name": row.get("stock_name") or code6,
        "shares": shares,
        "available_shares": available_shares,
        "cost_price": cost_price,
        "current_price": current_price,
        "market_value": market_value,
        "position_cost": _parse_broker_number(row.get("position_cost")),
        "route": "broker_real_position",
        "route_label": "QMT Mini real position",
        "trade_status": "broker_open",
        "management_action": "check_exit_contract",
        "exit_contract": f"QMT Mini只读持仓；当前合同 {contract['strategy_id']}：止损 {exit_rules['hard_stop_loss_pct']:.0%}，止盈 {exit_rules['take_profit_pct']:.0%} 后卖出 {exit_rules['take_profit_sell_ratio']:.0%}，余仓退出 {exit_rules['remaining_position_exit']}。",
        "source": "qmtmini_readonly_snapshot",
    }

