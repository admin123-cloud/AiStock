"""
修复指数日线 Kline 数据中的 volume 字段

问题：kline_daily 表中，1990~2025 年所有指数的 volume == amount（成交额被错误写入成交量字段）。
2026年数据基本正确，但 999999.SH 的 2026-05-11 仍有异常。

修复方案（方案A）：用 TDXQuant 重新拉取所有指数的日线历史数据，覆盖修复。

流程：
1. 获取所有指数代码（type='index'）
2. 对每个指数，找出 volume==amount 的异常日期范围
3. 用 TDXQuant 拉取该时间段的正确日线数据
4. DELETE 异常旧记录 + INSERT 新数据
"""

import sys
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
import pandas as pd

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from data_fetcher.sources.tdxquant import TdxQuantDataSource
from utils.market_warehouse import clickhouse_client
from utils.kline_store import filter_trading_day_tuples
from utils.kline_units import normalize_tdxquant_daily_units
from utils.logger import get_logger

logger = get_logger("FixIndexKlineVolume")

BATCH_SIZE = 10  # 每批处理多少个指数
DELAY_BETWEEN_INDICES = 0.3  # 指数间间隔（秒）


def get_problematic_indices() -> List[Dict]:
    """获取所有存在 volume==amount 异常数据的指数"""
    client = clickhouse_client()
    rows = client.query("""
        SELECT s.code, s.name,
               MIN(k.trade_date) as earliest_bad,
               MAX(k.trade_date) as latest_bad,
               countIf(k.volume = k.amount) as bad_count,
               count() as total_count
        FROM stock.stocks s
        JOIN stock.kline_daily k ON s.code = k.code
        WHERE s.type = 'index'
          AND k.volume = k.amount
        GROUP BY s.code, s.name
        ORDER BY bad_count DESC
    """).result_rows

    indices = []
    for row in rows:
        indices.append({
            "code": row[0],
            "name": row[1],
            "earliest_bad": str(row[2])[:10] if row[2] else None,
            "latest_bad": str(row[3])[:10] if row[3] else None,
            "bad_count": row[4],
            "total_count": row[5],
        })
    return indices


def get_bad_dates(code: str) -> List[str]:
    """获取指定指数所有 volume==amount 的异常日期"""
    from utils.market_warehouse import clickhouse_query_df
    df = clickhouse_query_df(
        "SELECT trade_date FROM kline_daily WHERE code = ? AND volume = amount ORDER BY trade_date",
        [code]
    )
    if df is None or df.empty:
        return []
    return [str(d)[:10] for d in df["trade_date"].tolist()]


def fetch_correct_kline(tdx: TdxQuantDataSource, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
    """用 TDXQuant 拉取指定指数在日期范围内的日线数据"""
    try:
        df = tdx.get_stock_history(
            stock_code=code,
            start_date=start_date,
            end_date=end_date,
            period="1d"
        )
        if df is None or df.empty:
            logger.warning(f"  {code}: TDXQuant 返回空数据 ({start_date}~{end_date})")
            return None

        # 添加 code 列，重命名字段以匹配 kline_daily
        df["code"] = code
        df.rename(columns={"date": "trade_date"}, inplace=True)
        if "trade_date" in df.columns:
            # 确保 trade_date 是 date 类型
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        df = normalize_tdxquant_daily_units(df, instrument_type="index")

        return df
    except Exception as e:
        logger.error(f"  {code}: TDXQuant 拉取失败: {e}")
        return None


def get_bad_date_range(indices: List[Dict]) -> Tuple[str, str]:
    """计算全局最早的异常日期和最晚的异常日期"""
    earliest = "1990-01-01"
    latest = datetime.now().strftime("%Y-%m-%d")
    for idx in indices:
        if idx["earliest_bad"] and idx["earliest_bad"] < earliest:
            earliest = idx["earliest_bad"]
        if idx["latest_bad"] and idx["latest_bad"] > latest:
            latest = idx["latest_bad"]
    return earliest, latest


def delete_and_insert(client, code: str, df: pd.DataFrame):
    """
    先 DELETE 已存在的异常记录（按 code + trade_date），再 INSERT 新数据
    """
    if df is None or df.empty:
        return 0

    # 获取所有要写入的日期列表
    dates = []
    for _, row in df.iterrows():
        d = row.get("trade_date")
        if d is None:
            continue
        if isinstance(d, datetime):
            d = d.date()
        dates.append(d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10])

    if not dates:
        return 0

    # 分批 DELETE（ClickHouse ALTER TABLE DELETE 不支持 IN 大量值）
    chunk_size = 500
    total_deleted = 0
    for i in range(0, len(dates), chunk_size):
        chunk = dates[i:i + chunk_size]
        date_list = ", ".join(f"'{d}'" for d in chunk)
        try:
            client.command(
                f"ALTER TABLE stock.kline_daily DELETE "
                f"WHERE code = '{code}' AND trade_date IN ({date_list})"
            )
            total_deleted += len(chunk)
            logger.info(f"  {code}: DELETE {len(chunk)} 条 (第{i//chunk_size+1}批)")
        except Exception as e:
            logger.error(f"  {code}: DELETE 失败 ({chunk[0]}~{chunk[-1]}): {e}")
            return 0

    # 准备插入数据
    insert_rows = []
    for _, row in df.iterrows():
        d = row.get("trade_date")
        if d is None:
            continue
        try:
            if isinstance(d, datetime):
                d_date = d.date()
            else:
                d_date = d if hasattr(d, "strftime") else datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
        except Exception:
            continue

        insert_rows.append([
            code,
            d_date,
            float(row.get("open", 0)),
            float(row.get("high", 0)),
            float(row.get("low", 0)),
            float(row.get("close", 0)),
            float(row.get("volume", 0)),
            float(row.get("amount", 0)),
        ])

    if insert_rows:
        try:
            column_names = ["code", "trade_date", "open", "high", "low", "close", "volume", "amount"]
            insert_rows = filter_trading_day_tuples("1d", [tuple(row) for row in insert_rows], column_names)
            if not insert_rows:
                return 0
            client.insert(
                "stock.kline_daily",
                insert_rows,
                column_names=column_names
            )
            logger.info(f"  {code}: INSERT {len(insert_rows)} 条")
        except Exception as e:
            logger.error(f"  {code}: INSERT 失败: {e}")
            return 0

    return len(insert_rows)


def optimize_table(client):
    """触发 ReplacingMergeTree 合并去重"""
    try:
        client.command("OPTIMIZE TABLE stock.kline_daily FINAL")
        logger.info("OPTIMIZE TABLE stock.kline_daily FINAL 完成")
    except Exception as e:
        logger.warning(f"OPTIMIZE 执行失败（非关键，查询时可用 FINAL 关键字）: {e}")


def main():
    logger.info("=" * 60)
    logger.info("开始修复指数日线 Kline Volume 数据")
    logger.info("=" * 60)

    # 1. 获取需要修复的指数列表
    problematic = get_problematic_indices()
    if not problematic:
        logger.info("没有发现需要修复的指数数据")
        return

    logger.info(f"发现 {len(problematic)} 个需要修复的指数")
    for idx in problematic:
        logger.info(f"  {idx['code']} {idx['name']}: {idx['bad_count']}/{idx['total_count']} 条异常 "
                     f"(范围 {idx['earliest_bad']} ~ {idx['latest_bad']})")

    # 2. 计算全局日期范围
    global_start, global_end = get_bad_date_range(problematic)
    logger.info(f"\n需要修复的日期范围: {global_start} ~ {global_end}")

    # 3. 逐个指数拉取并修复
    tdx = TdxQuantDataSource(name="tdxquant", config={"enabled": True})
    client = clickhouse_client()
    import time

    total_fixed = 0
    total_indices = len(problematic)
    batch_count = (total_indices + BATCH_SIZE - 1) // BATCH_SIZE

    for batch_idx in range(batch_count):
        batch_start = batch_idx * BATCH_SIZE
        batch_end = min(batch_start + BATCH_SIZE, total_indices)
        batch = problematic[batch_start:batch_end]

        logger.info(f"\n--- 处理批次 {batch_idx + 1}/{batch_count} (指数 {batch_start + 1}~{batch_end}) ---")

        for idx in batch:
            code = idx["code"]
            name = idx["name"]
            logger.info(f"\n处理: {code} {name} ({idx['bad_count']} 条异常)")

            # 4a. 获取该指数的所有异常日期
            bad_dates = get_bad_dates(code)
            if not bad_dates:
                logger.info(f"  {code}: 无异常数据，跳过")
                continue

            bad_start = bad_dates[0]
            bad_end = bad_dates[-1]
            # 多拉一些天数，确保覆盖
            fetch_start = (datetime.strptime(bad_start, "%Y-%m-%d") - timedelta(days=5)).strftime("%Y-%m-%d")
            fetch_end = (datetime.strptime(bad_end, "%Y-%m-%d") + timedelta(days=5)).strftime("%Y-%m-%d")
            if fetch_end > datetime.now().strftime("%Y-%m-%d"):
                fetch_end = datetime.now().strftime("%Y-%m-%d")

            # 4b. 从 TDXQuant 拉取正确的数据
            logger.info(f"  {code}: 拉取 TDXQuant 数据 ({fetch_start} ~ {fetch_end})")
            tdx_df = fetch_correct_kline(tdx, code, fetch_start, fetch_end)

            if tdx_df is None or tdx_df.empty:
                logger.warning(f"  {code}: 无法从 TDXQuant 获取数据，跳过")
                continue

            # 4c. 只保留异常日期附近的数据（避免覆盖大量正常数据）
            bad_date_set = set(bad_dates)
            fix_df = tdx_df[tdx_df["trade_date"].astype(str).isin(bad_date_set)].copy()
            if fix_df.empty:
                logger.info(f"  {code}: TDXQuant 数据中无对应异常日期的数据，跳过")
                continue

            # 5. DELETE + INSERT
            n = delete_and_insert(client, code, fix_df)
            if n > 0:
                total_fixed += n
                logger.info(f"  ✓ {code}: 已修复 {n} 条")
            else:
                logger.warning(f"  ✗ {code}: 修复失败")

            time.sleep(DELAY_BETWEEN_INDICES)

    # 6. 触发 OPTIMIZE（去重）
    logger.info(f"\n{'='*60}")
    logger.info(f"修复完成，共处理 {total_fixed} 条数据")
    if total_fixed > 0:
        optimize_table(client)

    # 7. 验证修复结果
    logger.info(f"\n{'='*60}")
    logger.info("验证修复结果:")
    remaining = get_problematic_indices()
    if remaining:
        logger.warning(f"仍有 {len(remaining)} 个指数存在异常:")
        for idx in remaining:
            logger.warning(f"  {idx['code']} {idx['name']}: {idx['bad_count']}/{idx['total_count']} 条")
    else:
        logger.info("所有指数数据已修复，volume 中不再有等于 amount 的异常值")


if __name__ == "__main__":
    main()
