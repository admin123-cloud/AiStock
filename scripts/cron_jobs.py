"""
定时任务脚本

配置和管理定时任务，包括数据更新、模型训练、策略回测等
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import get_logger
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

logger = get_logger("CronJobs")


def update_daily_data():
    """更新日线数据任务"""
    logger.info("执行日线数据更新任务")
    try:
        from scripts.update_data import main as update_data_main
        update_data_main()
        logger.info("日线数据更新任务完成")
    except Exception as e:
        logger.error(f"日线数据更新任务失败: {e}")


def update_realtime_quotes():
    """更新实时行情任务"""
    logger.info("执行实时行情更新任务")
    try:
        from data_fetcher import EastMoneyDataSource
        source = EastMoneyDataSource()
        quotes = source.get_realtime_quotes(["000001", "000002", "600000"])
        logger.info(f"获取到 {len(quotes)} 条实时行情")
    except Exception as e:
        logger.error(f"实时行情更新任务失败: {e}")


def cleanup_old_data():
    """清理旧数据任务"""
    logger.info("执行数据清理任务")
    try:
        # 清理7天前的缓存数据
        import os
        from datetime import datetime, timedelta
        
        cache_dir = Path("data/cache")
        if cache_dir.exists():
            cutoff_time = datetime.now() - timedelta(days=7)
            
            for file in cache_dir.glob("*.cache"):
                if datetime.fromtimestamp(file.stat().st_mtime) < cutoff_time:
                    file.unlink()
                    logger.info(f"删除旧缓存文件: {file.name}")
        
        logger.info("数据清理任务完成")
    except Exception as e:
        logger.error(f"数据清理任务失败: {e}")


def main():
    """主函数"""
    logger.info("启动定时任务调度器...")
    
    # 创建调度器
    scheduler = BlockingScheduler()
    
    # 添加定时任务
    
    # 每天收盘后更新日线数据（15:30）
    scheduler.add_job(
        update_daily_data,
        CronTrigger(hour=15, minute=30),
        id='update_daily_data',
        name='更新日线数据',
        replace_existing=True,
    )
    
    # 交易时间内每5分钟更新实时行情（9:30-15:00）
    scheduler.add_job(
        update_realtime_quotes,
        CronTrigger(day_of_week='mon-fri', hour='9-14', minute='*/5'),
        id='update_realtime_quotes',
        name='更新实时行情',
        replace_existing=True,
    )
    
    # 每天晚上运行回测（21:00）
    scheduler.add_job(
        run_backtest_daily,
        CronTrigger(hour=21, minute=0),
        id='run_backtest_daily',
        name='运行回测',
        replace_existing=True,
    )
    
    # 每天凌晨清理旧数据（2:00）
    scheduler.add_job(
        cleanup_old_data,
        CronTrigger(hour=2, minute=0),
        id='cleanup_old_data',
        name='清理旧数据',
        replace_existing=True,
    )
    
    # 打印任务列表
    logger.info("已配置的定时任务:")
    for job in scheduler.get_jobs():
        logger.info(f"  - {job.name}: {job.next_run_time}")
    
    try:
        # 启动调度器
        logger.info("定时任务调度器已启动")
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("收到停止信号，正在关闭调度器...")
        scheduler.shutdown()
        logger.info("定时任务调度器已关闭")


if __name__ == "__main__":
    main()