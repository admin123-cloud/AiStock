"""
实盘交易脚本

运行实时交易系统
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import get_logger
from execution import Broker, OrderManager, PositionManager, RiskController
from strategies import BaseStrategy
from data_fetcher import TdxDataSource

logger = get_logger("LiveTrading")


def main():
    """主函数"""
    logger.info("启动实盘交易系统...")
    
    try:
        # 初始化数据源
        data_source = TdxDataSource()
        
        # 初始化券商接口
        broker = Broker()
        
        # 初始化订单管理器
        order_manager = OrderManager(broker=broker)
        
        # 初始化仓位管理器
        position_manager = PositionManager(broker=broker)
        
        # 初始化风控模块
        risk_controller = RiskController(
            max_position_ratio=0.3,      # 单只股票最大仓位比例
            max_total_position=0.8,       # 总仓位上限
            max_drawdown=0.15,            # 最大回撤限制
            stop_loss_ratio=0.05,         # 止损比例
        )
        
        # 初始化策略
        strategy = None  # 这里需要传入具体的策略实例
        
        # 运行实时交易
        while True:
            try:
                # 获取实时行情
                quotes = data_source.get_realtime_quotes(["000001", "000002", "600000"])
                
                # 策略信号生成
                signals = strategy.generate_signals(quotes)
                
                # 风控检查
                for signal in signals:
                    if risk_controller.check_risk(signal):
                        # 执行订单
                        order_manager.execute_order(signal)
                
                # 更新仓位
                position_manager.update_positions()
                
                # 记录日志
                logger.info(f"交易循环完成，当前持仓: {position_manager.get_positions()}")
                
                # 等待下一个交易周期
                import time
                time.sleep(60)  # 每分钟执行一次
                
            except KeyboardInterrupt:
                logger.info("收到停止信号，正在关闭交易系统...")
                break
            except Exception as e:
                logger.error(f"交易循环出错: {e}")
                continue
        
        # 清理资源
        order_manager.close()
        position_manager.close()
        
        logger.info("实盘交易系统已关闭")
        
    except Exception as e:
        logger.error(f"实盘交易系统启动失败: {e}")
        raise


if __name__ == "__main__":
    main()