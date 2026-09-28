from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from utils.ch_gateway import ch_gateway


@dataclass
class StockFilter:
    market: Optional[str] = None
    stock_type: Optional[str] = None
    search: Optional[str] = None
    include_quit: bool = False


class StockRepository:
    def _build_where(self, flt: StockFilter) -> tuple[str, List[Any]]:
        where = ["1=1"]
        params: List[Any] = []
        if not flt.include_quit:
            where.append("COALESCE(quit, 0) = 0")
        if flt.market:
            where.append("market = %s")
            params.append(flt.market)
        if flt.stock_type:
            where.append("type = %s")
            params.append(flt.stock_type)
        if flt.search:
            where.append("(code LIKE %s OR name LIKE %s)")
            kw = f"%{flt.search}%"
            params.extend([kw, kw])
        return " AND ".join(where), params

    def count_stocks(self, flt: StockFilter) -> int:
        where_sql, params = self._build_where(flt)
        sql = f"SELECT count() FROM stocks WHERE {where_sql}"
        value = ch_gateway.query_scalar(sql, params)
        return int(value or 0)

    def list_stocks(self, flt: StockFilter, *, offset: int, limit: int) -> List[Dict[str, Any]]:
        where_sql, params = self._build_where(flt)
        sql = f"""
        SELECT code, name, market, type, industry, region, industry_code, st, quit,
               list_date, float_share, total_share, self_selected, holding
        FROM stocks
        WHERE {where_sql}
        ORDER BY code
        LIMIT %s OFFSET %s
        """
        rows = ch_gateway.query_df(sql, [*params, int(limit), int(offset)])
        if rows is None or rows.empty:
            return []
        result: List[Dict[str, Any]] = []
        for row in rows.itertuples(index=False):
            result.append(
                {
                    "code": row.code,
                    "name": row.name,
                    "market": row.market,
                    "type": row.type,
                    "industry": row.industry,
                    "region": row.region,
                    "industry_code": row.industry_code,
                    "st": row.st,
                    "quit": row.quit,
                    "list_date": row.list_date.isoformat() if row.list_date else None,
                    "float_share": float(row.float_share or 0),
                    "total_share": float(row.total_share or 0),
                    "self_selected": row.self_selected,
                    "holding": row.holding,
                }
            )
        return result

    def latest_kline_map(self, codes: List[str]) -> Dict[str, Dict[str, Any]]:
        if not codes:
            return {}
        placeholders = ",".join(["%s"] * len(codes))
        sql = f"""
        WITH latest AS (
            SELECT code, MAX(trade_date) AS max_date
            FROM kline_daily
            WHERE code IN ({placeholders})
            GROUP BY code
        )
        SELECT k.code, k.trade_date, k.close, k.change_pct, k.amount
        FROM kline_daily k
        JOIN latest l ON k.code = l.code AND k.trade_date = l.max_date
        """
        df = ch_gateway.query_df(sql, codes)
        if df is None or df.empty:
            return {}
        data: Dict[str, Dict[str, Any]] = {}
        for row in df.itertuples(index=False):
            data[row.code] = {
                "trade_date": row.trade_date.isoformat() if row.trade_date else None,
                "close": float(row.close) if row.close is not None else None,
                "change_pct": float(row.change_pct) if row.change_pct is not None else None,
                "amount": float(row.amount) if row.amount is not None else None,
            }
        return data


stock_repo = StockRepository()

