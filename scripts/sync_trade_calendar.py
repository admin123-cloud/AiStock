"""同步交易日历数据（ClickHouse 版）"""
import os
import sys
from datetime import datetime

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from utils.logger import get_logger
from utils.market_warehouse import clickhouse_client
from data_fetcher.sources.tdxquant_pool import tdxquant_pool

logger = get_logger("trade_calendar")


def sync_trade_calendar():
    """同步交易日历数据到 ClickHouse（全量原子替换）。"""
    try:
        source = "tdxquant"
        try:
            if not tdxquant_pool._initialize(force=True):
                raise RuntimeError(tdxquant_pool._last_init_error or "unknown error")

            tq = tdxquant_pool.get_client()
            logger.info("开始从 TdxQuant 获取交易日历数据")
            trade_dates = tq.get_trading_dates(market="SH", start_time="19900101", end_time="", count=-1)
        except Exception as tdx_exc:
            source = "akshare_sina"
            logger.warning(f"TdxQuant 交易日历获取失败，改用 AkShare/Sina: {tdx_exc}")
            import akshare as ak

            df = ak.tool_trade_date_hist_sina()
            trade_dates = [
                item.strftime("%Y%m%d") if hasattr(item, "strftime") else str(item).replace("-", "")
                for item in df["trade_date"].tolist()
            ]

        logger.info(f"获取到 {len(trade_dates)} 个交易日, source={source}")

        insert_rows = []
        for date_str in trade_dates:
            trade_date = datetime.strptime(date_str, "%Y%m%d").date()
            insert_rows.append((trade_date, "SH", 1))
            insert_rows.append((trade_date, "SZ", 1))

        if not insert_rows:
            logger.warning("没有可写入的交易日历数据")
            return 0

        client = clickhouse_client()
        client.command(
            """
            CREATE TABLE IF NOT EXISTS trade_calendar
            (
                trade_date Date,
                market String,
                is_trading UInt8
            )
            ENGINE = ReplacingMergeTree()
            ORDER BY (trade_date, market)
            """
        )

        tmp_table = "trade_calendar_sync_tmp"
        backup_table = "trade_calendar_sync_backup"
        client.command(f"DROP TABLE IF EXISTS {tmp_table}")
        client.command(f"DROP TABLE IF EXISTS {backup_table}")
        client.command(f"CREATE TABLE {tmp_table} AS trade_calendar")
        client.insert(
            tmp_table,
            insert_rows,
            column_names=["trade_date", "market", "is_trading"],
        )
        client.command(f"RENAME TABLE trade_calendar TO {backup_table}, {tmp_table} TO trade_calendar")
        client.command(f"DROP TABLE IF EXISTS {backup_table}")

        logger.info(f"交易日历写入完成: {len(insert_rows)} rows")
        return len(insert_rows)
    except Exception as exc:
        logger.error(f"同步交易日历失败: {exc}")
        raise


def get_trading_dates(start_date: str, end_date: str, market: str = "SH") -> list:
    """获取指定日期范围内的交易日。"""
    client = clickhouse_client()
    rs = client.query(
        """
        SELECT trade_date
        FROM trade_calendar
        WHERE trade_date >= %(start)s
          AND trade_date <= %(end)s
          AND market = %(market)s
          AND is_trading = 1
        ORDER BY trade_date
        """,
        parameters={"start": start_date, "end": end_date, "market": market},
    )
    return [str(r[0]) for r in rs.result_rows]


if __name__ == "__main__":
    sync_trade_calendar()
