"""
更新数据脚本

从数据源获取最新的股票数据并更新到数据库
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import get_logger
from data_fetcher import TdxDataSource, EastMoneyDataSource
from datetime import datetime, timedelta

logger = get_logger("UpdateData")


def main():
    """主函数"""
    logger.info("开始更新数据...")
    
    try:
        # 初始化数据源
        tdx_source = TdxDataSource()
        eastmoney_source = EastMoneyDataSource()
        
        # 获取股票列表
        stock_list = tdx_source.get_stock_list()
        logger.info(f"获取到 {len(stock_list)} 只股票")
        
        # 更新日线数据
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        
        success_count = 0
        fail_count = 0
        
        for i, stock in enumerate(stock_list, 1):
            try:
                # 获取日线数据
                df = tdx_source.get_kline(
                    symbol=stock['code'],
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                )
                
                if not df.empty:
                    # 保存到数据库
                    # 这里需要实现数据库保存逻辑
                    # database.save_kline_data(stock['code'], df)
                    success_count += 1
                
                # 进度显示
                if i % 100 == 0:
                    logger.info(f"已处理 {i}/{len(stock_list)} 只股票")
                
            except Exception as e:
                logger.error(f"更新股票 {stock['code']} 数据失败: {e}")
                fail_count += 1
                continue
        
        # 更新实时行情
        logger.info("更新实时行情...")
        realtime_quotes = eastmoney_source.get_realtime_quotes([stock['code'] for stock in stock_list[:100]])
        logger.info(f"获取到 {len(realtime_quotes)} 条实时行情")
        
        # 输出统计信息
        logger.info(f"数据更新完成: 成功 {success_count} 只，失败 {fail_count} 只")
        
    except Exception as e:
        logger.error(f"数据更新失败: {e}")
        raise


if __name__ == "__main__":
    main()