"""Domain logic extracted without changing existing API behavior."""
import re
from typing import Any, Dict, List, Optional


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

