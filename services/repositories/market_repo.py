from __future__ import annotations

from typing import Any, Dict, List, Optional

from utils.ch_gateway import ch_gateway

CORE_INDEX_CODES = {"999999.SH", "399001.SZ"}
DEFAULT_INDEX_JUMP_THRESHOLD = 20.0
CORE_INDEX_JUMP_THRESHOLD = 15.0


class MarketRepository:
    def get_market_indices(self, code: Optional[str] = None) -> Dict[str, Any]:
        stock_where = "type = 'index'"
        params: List[Any] = []
        if code:
            stock_where += " AND code = %s"
            params.append(code)

        stocks_df = ch_gateway.query_df(
            f"SELECT code, name FROM stocks WHERE {stock_where} ORDER BY code",
            params,
        )
        if stocks_df is None or stocks_df.empty:
            return {
                "indices": [],
                "trading_date": None,
                "validation_status": "failed",
                "validation_reason": "no_index_stocks",
                "blocked_codes": [],
            }

        latest_date = ch_gateway.query_scalar(
            """
            SELECT MAX(k.trade_date)
            FROM kline_daily k
            JOIN stocks s ON s.code = k.code
            WHERE s.type = 'index'
            """
        )
        if not latest_date:
            return {
                "indices": [],
                "trading_date": None,
                "validation_status": "failed",
                "validation_reason": "no_kline_data",
                "blocked_codes": [],
            }

        previous_date = ch_gateway.query_scalar(
            """
            SELECT MAX(trade_date)
            FROM trade_calendar
            WHERE trade_date < %s AND is_trading = 1
            """,
            [str(latest_date)[:10]],
        )
        if not previous_date:
            previous_date = latest_date

        code_list = [str(v) for v in stocks_df["code"].tolist()]
        placeholders = ",".join(["%s"] * len(code_list))
        k_df = ch_gateway.query_df(
            f"""
            SELECT code, trade_date, close, volume, amount
            FROM kline_daily
            WHERE code IN ({placeholders})
              AND trade_date IN (%s, %s)
            """,
            [*code_list, str(latest_date)[:10], str(previous_date)[:10]],
        )
        k_map: Dict[tuple[str, str], Dict[str, Any]] = {}
        if k_df is not None and not k_df.empty:
            for row in k_df.itertuples(index=False):
                k_map[(row.code, str(row.trade_date)[:10])] = {
                    "trade_date": row.trade_date,
                    "close": float(row.close or 0),
                    "volume": row.volume,
                    "amount": row.amount,
                }

        latest_key = str(latest_date)[:10]
        previous_key = str(previous_date)[:10]
        indices: List[Dict[str, Any]] = []
        blocked_codes: List[Dict[str, Any]] = []
        present_core_codes = set()
        name_map = {row.code: row.name for row in stocks_df.itertuples(index=False)}

        for c in code_list:
            latest = k_map.get((c, latest_key))
            if not latest:
                if c in CORE_INDEX_CODES:
                    blocked_codes.append({"code": c, "reason": "missing_latest_kline"})
                continue
            prev = k_map.get((c, previous_key))
            latest_close = float(latest.get("close") or 0)
            change = 0.0
            change_pct = 0.0
            if prev and float(prev.get("close") or 0) > 0:
                prev_close = float(prev["close"])
                change = latest_close - prev_close
                change_pct = change / prev_close * 100

            threshold = CORE_INDEX_JUMP_THRESHOLD if c in CORE_INDEX_CODES else DEFAULT_INDEX_JUMP_THRESHOLD
            is_abnormal = latest_close <= 0 or (prev is not None and abs(change_pct) > threshold)
            reason = "ok"
            if latest_close <= 0:
                reason = "invalid_close"
            elif prev is not None and abs(change_pct) > threshold:
                reason = f"change_pct_exceeds_{threshold}%"

            if c in CORE_INDEX_CODES:
                present_core_codes.add(c)
                if is_abnormal:
                    blocked_codes.append({"code": c, "reason": reason, "change_pct": change_pct})

            dv = latest.get("trade_date")
            indices.append(
                {
                    "code": c,
                    "name": name_map.get(c),
                    "price": latest.get("close"),
                    "change": change,
                    "change_pct": change_pct,
                    "volume": latest.get("volume"),
                    "amount": latest.get("amount"),
                    "date": dv.strftime("%Y-%m-%d") if hasattr(dv, "strftime") else str(dv)[:10],
                    "is_abnormal": is_abnormal,
                    "validation_reason": reason,
                }
            )

        missing_core = CORE_INDEX_CODES - present_core_codes
        for missing_code in sorted(missing_core):
            blocked_codes.append({"code": missing_code, "reason": "missing_core_index"})

        return {
            "indices": indices,
            "trading_date": str(latest_date)[:10],
            "validation_status": "passed" if not blocked_codes else "failed",
            "validation_reason": "ok" if not blocked_codes else "core_index_validation_failed",
            "blocked_codes": blocked_codes,
            "source": "clickhouse",
        }

    def get_market_sentiment(self) -> Dict[str, Any]:
        latest_date = ch_gateway.query_scalar(
            """
            SELECT MAX(k.trade_date)
            FROM kline_daily k
            JOIN stocks s ON s.code = k.code
            WHERE s.type = 'stock'
            """
        )
        if not latest_date:
            return {"error": "数据库中没有K线数据"}
        d = str(latest_date)[:10]
        row_df = ch_gateway.query_df(
            """
            WITH base AS (
                SELECT
                    k.amount,
                    COALESCE(k.change_pct, (k.close - k.open) / NULLIF(k.open, 0) * 100) AS pct
                FROM kline_daily k
                JOIN stocks s ON s.code = k.code
                WHERE s.type = 'stock'
                  AND k.trade_date = %s
                  AND k.open > 0
            )
            SELECT
                SUM(CASE WHEN pct > 0 THEN 1 ELSE 0 END) AS up_count,
                SUM(CASE WHEN pct < 0 THEN 1 ELSE 0 END) AS down_count,
                SUM(CASE WHEN pct = 0 THEN 1 ELSE 0 END) AS unchanged_count,
                SUM(CASE WHEN pct >= 5 THEN 1 ELSE 0 END) AS up_5_count,
                SUM(CASE WHEN pct <= -5 THEN 1 ELSE 0 END) AS down_5_count,
                SUM(CASE WHEN pct >= 9.9 THEN 1 ELSE 0 END) AS limit_up_count,
                SUM(CASE WHEN pct <= -9.9 THEN 1 ELSE 0 END) AS limit_down_count,
                SUM(amount) AS total_amount
            FROM base
            """,
            [d],
        )
        if row_df is None or row_df.empty:
            return {"error": "无法获取涨跌统计数据"}
        row = row_df.iloc[0]
        up = int(row.get("up_count") or 0)
        down = int(row.get("down_count") or 0)
        unchanged = int(row.get("unchanged_count") or 0)
        up5 = int(row.get("up_5_count") or 0)
        down5 = int(row.get("down_5_count") or 0)
        lup = int(row.get("limit_up_count") or 0)
        ldown = int(row.get("limit_down_count") or 0)
        total_amount = float(row.get("total_amount") or 0)
        total = up + down + unchanged
        score = 50 + (up - down) / total * 50 if total > 0 else 50
        level = "中性"
        if score >= 80:
            level = "极度乐观"
        elif score >= 65:
            level = "乐观"
        elif score >= 55:
            level = "偏乐观"
        elif score >= 45:
            level = "中性"
        elif score >= 35:
            level = "偏悲观"
        elif score >= 20:
            level = "悲观"
        else:
            level = "极度悲观"
        turnover_df = ch_gateway.query_df(
            """
            SELECT s.market, SUM(k.amount) AS amount
            FROM kline_daily k
            JOIN stocks s ON s.code = k.code
            WHERE s.type = 'stock'
              AND s.market IN ('SH', 'SZ')
              AND k.trade_date = %s
              AND k.open > 0
            GROUP BY s.market
            """,
            [d],
        )
        tm = {}
        if turnover_df is not None and not turnover_df.empty:
            for x in turnover_df.itertuples(index=False):
                tm[str(x.market)] = float(x.amount or 0)

        return {
            "date": d,
            "up_count": up,
            "down_count": down,
            "unchanged_count": unchanged,
            "up_5_percent_count": up5,
            "down_5_percent_count": down5,
            "limit_up_count": lup,
            "limit_down_count": ldown,
            "avg_change_percent": 0,
            "total_turnover": total_amount,
            "sh_amount": float(tm.get("SH") or 0),
            "sz_amount": float(tm.get("SZ") or 0),
            "turnover_source": "stock_aggregate",
            "sentiment_score": score,
            "sentiment_level": level,
            "source": "clickhouse",
        }


market_repo = MarketRepository()

