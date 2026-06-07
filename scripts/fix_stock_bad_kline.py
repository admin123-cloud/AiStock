"""
修复42只个股的异常K线数据（股价串位为指数数据）
方案：用 TDXQuant 重新拉取正确数据覆盖
"""
import os, sys, time
from datetime import datetime, timedelta
import pandas as pd
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from utils.market_warehouse import clickhouse_client, clickhouse_query_df
from data_fetcher.sources.tdxquant import TdxQuantDataSource
from utils.logger import get_logger

logger = get_logger("FixStockBadKline")

BATCH_SIZE = 10

def get_bad_stocks():
    """获取有异常数据的股票代码和时间范围"""
    df = clickhouse_query_df("""
        SELECT k.code, s.name,
               min(k.trade_date) as from_dt,
               max(k.trade_date) as to_dt,
               count() as bad_cnt
        FROM kline_daily k JOIN stocks s ON k.code = s.code
        WHERE s.type = 'stock'
          AND k.volume > 0 AND k.amount > 0
          AND abs(k.volume*k.close - k.amount)/k.amount > 0.2
        GROUP BY k.code, s.name
        ORDER BY k.code
    """)
    stocks = []
    for _, row in df.iterrows():
        stocks.append({
            "code": row["code"],
            "name": row["name"],
            "from_dt": str(row["from_dt"])[:10],
            "to_dt": str(row["to_dt"])[:10],
            "bad_cnt": row["bad_cnt"],
        })
    return stocks


def fix_stock(tdx, client, stock, extra_days=5):
    """修复单个股票：用TDXQuant拉取+DELETE+INSERT"""
    code = stock["code"]
    name = stock["name"]
    from_dt = stock["from_dt"]
    to_dt = stock["to_dt"]

    # 扩展日期范围确保覆盖
    dt_from = (datetime.strptime(from_dt, "%Y-%m-%d") - timedelta(days=extra_days)).strftime("%Y-%m-%d")
    dt_to = (datetime.strptime(to_dt, "%Y-%m-%d") + timedelta(days=extra_days)).strftime("%Y-%m-%d")

    logger.info(f"  {code} {name}: 拉取 {dt_from} ~ {dt_to}")

    try:
        df = tdx.get_stock_history(
            stock_code=code,
            start_date=dt_from,
            end_date=dt_to,
            period="1d"
        )
        if df is None or df.empty:
            logger.warning(f"  {code}: 无数据，跳过")
            return False

        # 准备列名
        df["code"] = code
        df.rename(columns={"date": "trade_date"}, inplace=True)
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date

        # 从数据库中获取这个时间段内的异常日期
        bad_sql = f"""
            SELECT trade_date FROM kline_daily
            WHERE code = '{code}'
              AND trade_date BETWEEN '{dt_from}' AND '{dt_to}'
              AND volume > 0 AND amount > 0
              AND abs(volume*close - amount)/amount > 0.2
        """
        bad_dates_result = client.query(bad_sql).result_rows
        bad_dates = set(str(r[0])[:10] for r in bad_dates_result)

        if not bad_dates:
            logger.info(f"  {code}: 无需修复")
            return True

        # 筛选出修正范围的数据
        fix_df = df[df["trade_date"].astype(str).isin(bad_dates)]
        if fix_df.empty:
            logger.info(f"  {code}: TDXQuant返回数据中无对应日期")
            return True

        # DELETE + INSERT
        date_list = ", ".join(f"'{d}'" for d in bad_dates)
        client.command(f"ALTER TABLE stock.kline_daily DELETE WHERE code = '{code}' AND trade_date IN ({date_list})")

        rows = []
        for _, r in fix_df.iterrows():
            d = r["trade_date"]
            if d is None:
                continue
            rows.append([
                code,
                d,
                float(r.get("open", 0)),
                float(r.get("high", 0)),
                float(r.get("low", 0)),
                float(r.get("close", 0)),
                float(r.get("volume", 0)),
                float(r.get("amount", 0)) * 10000,  # TDX amount 为万元，转为元
            ])

        if rows:
            client.insert("stock.kline_daily", rows,
                          column_names=["code", "trade_date", "open", "high", "low", "close", "volume", "amount"])
            logger.info(f"  ✓ {code}: 修复 {len(rows)} 条")

        return True
    except Exception as e:
        logger.error(f"  ✗ {code}: {e}")
        return False


def main():
    from datetime import datetime, timedelta
    import pandas as pd

    logger.info("=" * 60)
    logger.info("开始修复个股异常K线数据")
    logger.info("=" * 60)

    stocks = get_bad_stocks()
    logger.info(f"需要修复: {len(stocks)} 只股票")

    tdx = TdxQuantDataSource(name="tdxquant", config={"enabled": True})
    client = clickhouse_client()

    success = 0
    fail = 0
    for idx, stock in enumerate(stocks):
        code = stock["code"]
        name = stock["name"]
        logger.info(f"[{idx+1}/{len(stocks)}] {code} {name} ({stock['bad_cnt']}条异常 {stock['from_dt']}~{stock['to_dt']})")
        if fix_stock(tdx, client, stock):
            success += 1
        else:
            fail += 1
        time.sleep(0.3)

    logger.info(f"\n完成: 成功{success} 失败{fail}")

    # 最终验证
    remaining = get_bad_stocks()
    if remaining:
        logger.warning(f"仍有 {len(remaining)} 只股票存在异常:")
        for s in remaining:
            logger.warning(f"  {s['code']} {s['name']}: {s['bad_cnt']}条")
    else:
        logger.info("所有股票数据已通过 volume×close≈amount 一致性校验 ✅")


if __name__ == "__main__":
    main()
