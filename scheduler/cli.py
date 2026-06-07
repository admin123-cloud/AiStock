"""定时任务命令行工具"""

import argparse
import asyncio
import os
import sys
from datetime import datetime

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from utils.logger import get_logger
from scheduler.config import SCHEDULER_CONFIG
from scheduler.scheduler import SchedulerManager
from scheduler.trading_calendar import TradingCalendar
from scheduler.jobs import SchedulerJobs

logger = get_logger("SchedulerCLI")


def show_config():
    """显示配置信息"""
    print("\n" + "=" * 60)
    print("定时任务配置")
    print("=" * 60)

    print(f"\n启用状态: {SCHEDULER_CONFIG['enabled']}")

    print("\n定时任务列表:")
    for job_name, job_config in SCHEDULER_CONFIG["jobs"].items():
        print(f"\n  [{job_name}]")
        print(f"    启用: {job_config['enabled']}")
        print(f"    描述: {job_config['description']}")

        if "cron_expression" in job_config:
            print(f"    Cron 表达式: {job_config['cron_expression']}")
        if "interval" in job_config:
            print(f"    执行间隔: {job_config['interval']} 秒")
        if "trading_days_only" in job_config:
            print(f"    仅交易日: {job_config['trading_days_only']}")

    print("\n数据同步配置:")
    sync_config = SCHEDULER_CONFIG["sync"]
    print("\n  K线同步:")
    print(f"    周期: {sync_config['kline']['periods']}")
    print(f"    数量: {sync_config['kline']['count']}")
    print(f"    复权: {sync_config['kline']['adjust']}")
    print(f"    批次大小: {sync_config['kline']['batch_size']}")
    print(f"    批次延迟: {sync_config['kline']['delay_between_batches']} 秒")

    print("\n  行情同步:")
    print(f"    最大重试: {sync_config['quotes']['max_retries']}")
    print(f"    超时: {sync_config['quotes']['timeout']} 秒")

    print("\n" + "=" * 60)


def show_trading_info():
    """显示交易日信息"""
    print("\n" + "=" * 60)
    print("交易日信息")
    print("=" * 60)

    now = datetime.now()

    print(f"\n当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"是否交易日: {TradingCalendar.is_trading_day(now)}")
    print(f"是否交易时间: {TradingCalendar.is_trading_time(now)}")
    print(f"市场是否开放: {TradingCalendar.is_market_open(now)}")

    time_to_open = TradingCalendar.time_to_market_open(now)
    if time_to_open:
        print(f"距离开盘: {time_to_open}")

    time_to_close = TradingCalendar.time_to_market_close(now)
    if time_to_close:
        print(f"距离收盘: {time_to_close}")

    print("\n" + "=" * 60)


async def run_full_sync():
    """手动执行全量 K 线同步"""
    print("\n" + "=" * 60)
    print("开始手动执行全量 K 线同步")
    print("=" * 60)

    try:
        jobs = SchedulerJobs()
        await jobs.full_sync_kline_job()
        print("\n全量 K 线同步完成")
    except Exception as e:
        logger.error(f"全量 K 线同步失败: {e}")
        sys.exit(1)


async def run_realtime_quotes():
    """手动执行实时行情更新"""
    print("\n" + "=" * 60)
    print("开始手动执行实时行情更新")
    print("=" * 60)

    try:
        jobs = SchedulerJobs()
        await jobs.realtime_quotes_job()
        print("\n实时行情更新完成")
    except Exception as e:
        logger.error(f"实时行情更新失败: {e}")
        sys.exit(1)


async def start_scheduler():
    """启动调度器"""
    print("\n" + "=" * 60)
    print("启动定时任务调度器")
    print("=" * 60)

    try:
        scheduler = SchedulerManager.get_instance()
        scheduler.register_signal_handlers()
        await scheduler.start()

        print("\n调度器正在运行，按 Ctrl+C 停止...")
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("\n\n正在停止调度器...")
        scheduler = SchedulerManager.get_instance()
        await scheduler.stop()
        print("调度器已停止")
    except Exception as e:
        logger.error(f"调度器运行失败: {e}")
        sys.exit(1)


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="AiStock 定时任务管理工具")
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    subparsers.add_parser("config", help="显示配置信息")
    subparsers.add_parser("trading", help="显示交易日信息")
    subparsers.add_parser("sync", help="手动执行全量 K 线同步")
    subparsers.add_parser("quotes", help="手动执行实时行情更新")
    subparsers.add_parser("start", help="启动定时任务调度器")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "config":
        show_config()
    elif args.command == "trading":
        show_trading_info()
    elif args.command == "sync":
        asyncio.run(run_full_sync())
    elif args.command == "quotes":
        asyncio.run(run_realtime_quotes())
    elif args.command == "start":
        asyncio.run(start_scheduler())


if __name__ == "__main__":
    main()