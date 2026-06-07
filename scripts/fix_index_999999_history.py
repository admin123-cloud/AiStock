"""
补全 999999.SH（上证指数）历史K线数据到 ClickHouse。
从 akshare 获取全量历史数据（1990年至今），只补 ClickHouse 中缺失的部分。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from datetime import datetime
from sqlalchemy import text
from loguru import logger

from utils.database import db
from utils.market_warehouse import market_source

TARGET_CODE = "999999.SH"

def get_existing_dates(engine) -> set:
    """查询 ClickHouse 中已有的交易日期集合。"""
    query = text("""
        SELECT DISTINCT trade_date 
        FROM kline_daily 
        WHERE code = :code
    """)
    with engine.connect() as conn:
        rows = conn.execute(query, {"code": TARGET_CODE}).fetchall()
    return {row[0] for row in rows}


def fetch_akshare_history():
    """从 akshare 获取上证指数全量历史数据。"""
    import akshare as ak
    df = ak.stock_zh_index_daily(symbol="sh000001")
    logger.info(f"akshare 返回 {len(df)} 条数据, 范围: {df['date'].min()} ~ {df['date'].max()}")
    return df


def compute_kline_fields(df: pd.DataFrame) -> pd.DataFrame:
    """计算涨跌幅、振幅等衍生字段（与 sync_all_klines.py 逻辑一致）。"""
    df = df.sort_values("date").copy()
    df["change_amount"] = df["close"].diff()
    df["change_pct"] = (df["change_amount"] / df["close"].shift(1)) * 100
    df["amplitude"] = ((df["high"] - df["low"]) / df["close"].shift(1)) * 100

    # 首行设 0
    df.loc[df.index[0], "change_amount"] = 0.0
    df.loc[df.index[0], "change_pct"] = 0.0
    df.loc[df.index[0], "amplitude"] = 0.0

    df["turnover_rate"] = 0.0  # 指数没有换手率
    df["code"] = TARGET_CODE
    df["created_at"] = datetime.now()  # clickhouse_connect 接受 datetime 对象

    # 重命名列
    df = df.rename(columns={"date": "trade_date", "volume": "volume"})
    # akshare 没有 amount 字段, 用 volume 填充（指数 volume 已经是成交额）
    df["amount"] = df["volume"]

    # NaN 替换为 0（ClickHouse Float64 不接受 NaN/None）
    for col in ["amplitude", "change_pct", "change_amount"]:
        df[col] = df[col].fillna(0.0)

    columns = [
        "code", "trade_date", "open", "high", "low", "close",
        "volume", "amount", "amplitude", "change_pct",
        "change_amount", "turnover_rate", "created_at"
    ]
    return df[[col for col in columns if col in df.columns]]


def main():
    engine = db._engine
    dialect = str(getattr(getattr(engine, "dialect", None), "name", "") or "").lower()
    is_ch = ("clickhouse" in dialect) or (str(market_source() or "").lower() == "clickhouse")
    logger.info(f"数据库引擎: {dialect}, ClickHouse: {is_ch}")

    # 1. 获取已有日期
    existing = get_existing_dates(engine)
    logger.info(f"ClickHouse 中 {TARGET_CODE} 已有 {len(existing)} 个交易日: "
                f"{min(existing) if existing else '无'} ~ {max(existing) if existing else '无'}")

    # 2. 获取 akshare 全量数据
    raw = fetch_akshare_history()
    raw["date"] = pd.to_datetime(raw["date"]).dt.date

    # 3. 过滤出缺失的日期
    missing_mask = ~raw["date"].isin(existing)
    missing = raw[missing_mask].copy()
    logger.info(f"需补充 {len(missing)} 条数据 (已过滤重复日期)")

    if missing.empty:
        logger.info("没有需要补充的数据，退出。")
        return

    # 4. 计算衍生字段
    df = compute_kline_fields(missing)

    # 5. 写入 ClickHouse（通过 SQLAlchemy engine，已正确配置 database=stock）
    table_name = "kline_daily"
    logger.info(f"正在写入 {len(df)} 条数据到 {table_name}...")

    # 分批写入（使用 clickhouse-connect 原生驱动，指定 stock 数据库）
    from clickhouse_connect import get_client
    ch_client = get_client(
        host=os.getenv("CLICKHOUSE_HOST", "127.0.0.1"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        database="stock",
    )

    batch_size = 1000
    total = len(df)
    col_names = [c for c in df.columns]
    now = datetime.now()
    for start in range(0, total, batch_size):
        end = min(start + batch_size, total)
        batch = df.iloc[start:end]

        # 构造 tuple list，确保 created_at 是 Python datetime 对象
        data_tuples = []
        for _, row in batch.iterrows():
            data_tuples.append(tuple(
                now if c == "created_at" else row[c]
                for c in col_names
            ))

        ch_client.insert(table_name, data_tuples, column_names=col_names)
        logger.info(f"  已写入 {end}/{total} 条")

    logger.info(f"成功写入 {total} 条记录到 ClickHouse stock.{table_name}")

    # 6. 验证
    after = get_existing_dates(engine)
    logger.info(f"补充后 {TARGET_CODE} 共有 {len(after)} 个交易日: "
                f"{min(after)} ~ {max(after)}")


if __name__ == "__main__":
    main()