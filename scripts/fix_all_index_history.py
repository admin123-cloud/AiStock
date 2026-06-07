"""
补全所有指数历史K线数据到 ClickHouse。

从 akshare 获取各指数全量历史数据，只补 ClickHouse 中缺失的部分。
支持 93 个可通过 akshare stock_zh_index_daily() 获取的指数。

用法:
    python scripts/fix_all_index_history.py                     # 补全所有指数
    python scripts/fix_all_index_history.py --codes 000300.SH    # 只补指定指数
    python scripts/fix_all_index_history.py --dry-run            # 只打印计划，不写入
"""

import sys
import os
import time
import argparse
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from loguru import logger
from clickhouse_connect import get_client


def get_ch_client():
    return get_client(
        host=os.getenv("CLICKHOUSE_HOST", "127.0.0.1"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        database="stock",
    )


def code_to_akshare_symbol(code: str) -> Optional[str]:
    """
    将 ts_code (如 000300.SH) 映射为 akshare stock_zh_index_daily 可接受的 symbol。
    """
    num = code.split(".")[0]
    market = code.split(".")[1].upper()
    if market == "SH":
        return f"sh{num}"
    elif market == "SZ":
        if num.startswith("899"):
            return f"bj{num}"
        return f"sz{num}"
    elif market == "BJ":
        return f"bj{num}"
    return None


def get_all_indices(client) -> list[dict]:
    """从 ClickHouse 获取所有未退市指数及其当前数据概况。"""
    rows = client.query(
        """
        SELECT s.code, s.name, s.market,
               count(k.code) AS cnt,
               min(k.trade_date) AS min_d,
               max(k.trade_date) AS max_d
        FROM stocks s
        LEFT JOIN kline_daily k ON k.code = s.code
        WHERE s.type = 'index'
          AND (s.quit = 0 OR s.quit IS NULL)
        GROUP BY s.code, s.name, s.market
        ORDER BY s.code
        """
    ).result_rows

    indices = []
    for row in rows:
        indices.append({
            "code": row[0],
            "name": row[1],
            "market": row[2],
            "count": int(row[3]),
            "min_date": str(row[4]) if row[4] else None,
            "max_date": str(row[5]) if row[5] else None,
        })
    return indices


def get_existing_dates(client, code: str) -> set:
    """查询某个指数已有的交易日期。"""
    rows = client.query(
        "SELECT DISTINCT trade_date FROM kline_daily WHERE code = %(code)s",
        parameters={"code": code},
    ).result_rows
    return {row[0] for row in rows}


def compute_kline_fields(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """
    计算涨跌幅、振幅等衍生字段。
    逻辑与 sync_all_klines.py 中的 _save_kline_to_db 一致。
    """
    df = df.sort_values("date").copy()
    df["change_amount"] = df["close"].diff()
    df["change_pct"] = (df["change_amount"] / df["close"].shift(1)) * 100
    df["amplitude"] = ((df["high"] - df["low"]) / df["close"].shift(1)) * 100

    # 首行设 0
    df.loc[df.index[0], "change_amount"] = 0.0
    df.loc[df.index[0], "change_pct"] = 0.0
    df.loc[df.index[0], "amplitude"] = 0.0

    df["turnover_rate"] = 0.0  # 指数没有换手率
    df["code"] = code

    # 重命名列
    df = df.rename(columns={"date": "trade_date"})
    # akshare 中 volume 列是成交量，amount 列是成交额
    if "amount" not in df.columns:
        df["amount"] = df.get("volume", 0)

    # NaN 替换为 0
    for col in ["amplitude", "change_pct", "change_amount"]:
        df[col] = df[col].fillna(0.0)

    columns = [
        "code", "trade_date", "open", "high", "low", "close",
        "volume", "amount", "amplitude", "change_pct",
        "change_amount", "turnover_rate",
    ]
    return df[[col for col in columns if col in df.columns]]


def fetch_akshare_history(symbol: str, code: str) -> Optional[pd.DataFrame]:
    """从 akshare 获取指数全量历史数据。"""
    import akshare as ak
    try:
        df = ak.stock_zh_index_daily(symbol=symbol)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        logger.info(f"  akshare symbol={symbol} ({code}): {len(df)} 条, {min(df['date'])} ~ {max(df['date'])}")
        return df
    except Exception as e:
        logger.warning(f"  akshare symbol={symbol} ({code}) 获取失败: {e}")
        return None


def fix_index(client, index: dict, dry_run: bool = False) -> tuple[int, int]:
    """
    修复单个指数。返回 (新增条数, 总条数修复后)。
    """
    code = index["code"]
    name = index["name"]
    existing_count = index["count"]

    # 1. 获取已有日期
    existing_dates = get_existing_dates(client, code)

    # 2. 获取 akshare 数据
    symbol = code_to_akshare_symbol(code)
    if not symbol:
        logger.warning(f"  {code} {name}: 无法映射 akshare symbol")
        return 0, existing_count

    logger.info(f"[{code}] {name}: 当前 {existing_count} 条, 已有 {len(existing_dates)} 个不同交易日")
    raw = fetch_akshare_history(symbol, code)
    if raw is None or raw.empty:
        return 0, existing_count

    # 3. 过滤缺失日期
    missing_mask = ~raw["date"].isin(existing_dates)
    missing = raw[missing_mask].copy()
    logger.info(f"  需补充 {len(missing)} 条 (已过滤重复)")

    if missing.empty:
        logger.info(f"  ✅ {code} {name}: 已完整，无需补充")
        return 0, existing_count

    # 4. 计算衍生字段
    df = compute_kline_fields(missing, code)

    # 5. 写入 ClickHouse
    if dry_run:
        logger.info(f"  [DRY-RUN] 将写入 {len(df)} 条到 {code}")
        return len(df), existing_count + len(df)

    table_name = "kline_daily"
    batch_size = 1000
    total = len(df)
    col_names = [c for c in df.columns]
    now = datetime.now()

    for start in range(0, total, batch_size):
        end = min(start + batch_size, total)
        batch = df.iloc[start:end]

        data_tuples = []
        for _, row in batch.iterrows():
            data_tuples.append(tuple(
                now if c == "created_at" else row[c]
                for c in col_names
            ))

        # 额外加 created_at 列
        full_cols = col_names + ["created_at"]
        full_data = []
        for tup in data_tuples:
            full_data.append(tuple(list(tup) + [now]))

        client.insert(table_name, full_data, column_names=full_cols)
        logger.info(f"    已写入 {end}/{total} 条")

    # 6. 验证
    after_count = len(get_existing_dates(client, code))
    logger.info(f"  ✅ {code} {name}: 补充前 {existing_count} 条 → 补充后 {after_count} 条 "
                f"(新增 {after_count - existing_count} 条)")
    return total, after_count


def main():
    parser = argparse.ArgumentParser(description="补全所有指数历史K线数据")
    parser.add_argument("--codes", type=str, help="指定指数代码，逗号分隔 (如 000300.SH,399001.SZ)")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写入")
    args = parser.parse_args()

    # 设置无代理环境变量（akshare 需要）
    os.environ.pop("HTTP_PROXY", None)
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)

    client = get_ch_client()

    # 获取所有指数
    all_indices = get_all_indices(client)
    logger.info(f"总共 {len(all_indices)} 个指数")

    # 过滤指定代码
    if args.codes:
        target_codes = {c.strip() for c in args.codes.split(",")}
        all_indices = [idx for idx in all_indices if idx["code"] in target_codes]
        logger.info(f"过滤后 {len(all_indices)} 个指数 (指定代码: {args.codes})")

    # 跳过已完整的指数（数据覆盖3年以上）
    skip_codes = set()
    # 999999.SH 已修复，跳过
    skip_codes.add("999999.SH")

    indices_to_fix = [idx for idx in all_indices if idx["code"] not in skip_codes]
    logger.info(f"将修复 {len(indices_to_fix)} 个指数 (已跳过 {len(all_indices) - len(indices_to_fix)} 个)")

    if not indices_to_fix:
        logger.info("没有需要修复的指数")
        return

    # 打印汇总
    logger.info(f"\n{'='*80}")
    logger.info(f"即将{'模拟' if args.dry_run else '执行'}修复 {len(indices_to_fix)} 个指数:")
    logger.info(f"{'代码':15s} {'名称':20s} {'现有条数':>10s} {'最早日期':15s} {'最晚日期':15s}")
    logger.info("-" * 75)
    for idx in indices_to_fix:
        logger.info(f"{idx['code']:15s} {idx['name']:20s} {idx['count']:>10d} "
                    f"{str(idx['min_date'] or '无'):15s} {str(idx['max_date'] or '无'):15s}")
    logger.info("=" * 80)

    if args.dry_run:
        logger.info("DRY-RUN 模式，不会写入任何数据")
        return

    # 逐个修复（每次请求间隔 1-2 秒避免被限流）
    total_new = 0
    success_count = 0
    fail_count = 0
    failed_indices = []

    for i, idx in enumerate(indices_to_fix):
        logger.info(f"\n[{i+1}/{len(indices_to_fix)}] 正在修复 {idx['code']} {idx['name']}...")
        try:
            new_rows, after = fix_index(client, idx)
            if new_rows > 0:
                total_new += new_rows
                success_count += 1
            else:
                # new_rows=0 但可能已经是完整的，也算成功
                success_count += 1
            # 避免请求太频繁
            time.sleep(1.5)
        except Exception as e:
            logger.error(f"  ❌ {idx['code']} {idx['name']} 修复失败: {e}")
            fail_count += 1
            failed_indices.append(idx["code"])
            time.sleep(3)

    # 最终汇总
    logger.info(f"\n{'='*80}")
    logger.info("指数历史数据修复完成!")
    logger.info(f"成功: {success_count}, 失败: {fail_count}, 新增总条数: {total_new}")
    if failed_indices:
        logger.warning(f"失败的指数: {', '.join(failed_indices)}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()