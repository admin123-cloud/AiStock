"""
回测API

提供策略回测相关接口
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import List, Dict, Optional, Any
from datetime import datetime
from utils.logger import get_logger

router = APIRouter(prefix="/backtest", tags=["策略回测"])
logger = get_logger("backtest")


class BacktestRequest(BaseModel):
    """回测请求"""
    strategy_type: str  # 策略类型：ma_cross, macd, kdj, rsi, boll, custom
    stock_pool: str  # 股票池：all, watchlist, holding, custom
    custom_stocks: Optional[str] = None  # 自定义股票代码
    period: str  # 周期：1d, 1w, 1M
    start_date: str  # 开始日期
    end_date: str  # 结束日期
    initial_capital: float = 100000  # 初始资金
    commission_rate: float = 0.0003  # 手续费率
    slippage: float = 0.1  # 滑点
    params: Dict[str, Any] = {}  # 策略参数
    max_position_size: float = 20  # 单笔最大仓位
    max_positions: int = 5  # 最大持仓数
    stop_loss_ratio: float = 5  # 止损比例
    take_profit_ratio: float = 10  # 止盈比例


class TradeRecord(BaseModel):
    """交易记录"""
    code: str
    name: str
    buy_date: str
    buy_price: float
    sell_date: Optional[str] = None
    sell_price: Optional[float] = None
    holding_days: int
    profit_loss_pct: float
    profit_loss: float


class MonthlyReturn(BaseModel):
    """月度收益"""
    month: str
    return_value: float  # 使用return_value避免与Python关键字冲突


class BacktestResult(BaseModel):
    """回测结果"""
    total_return: float  # 总收益率
    annual_return: float  # 年化收益率
    sharpe_ratio: float  # 夏普比率
    max_drawdown: float  # 最大回撤
    win_rate: float  # 胜率
    profit_loss_ratio: float  # 盈亏比
    total_trades: int  # 交易次数
    avg_holding_days: float  # 平均持仓天数
    trades: List[TradeRecord]  # 交易记录
    monthly_returns: List[MonthlyReturn]  # 月度收益


# 回测任务状态管理
backtest_tasks = {}
backtest_lock = __import__('threading').Lock()

# 定期清理完成的任务（保留最近100个）
def cleanup_old_backtest_tasks():
    """清理旧的任务记录，防止内存无限增长"""
    global backtest_tasks
    with backtest_lock:
        # 获取所有任务ID
        task_ids = list(backtest_tasks.keys())
        
        # 如果任务数超过100，删除最旧的任务
        if len(task_ids) > 100:
            # 按任务ID排序（任务ID包含时间戳）
            task_ids.sort()
            # 删除前50个最旧的任务
            for task_id in task_ids[:50]:
                if task_id in backtest_tasks:
                    # 清理结果数据以释放内存
                    if 'result' in backtest_tasks[task_id]:
                        backtest_tasks[task_id]['result'] = None
                    del backtest_tasks[task_id]
                    logger.debug(f"清理旧回测任务: {task_id}")


@router.post("/run", response_model=BacktestResult)
def run_backtest(request: BacktestRequest, background_tasks: BackgroundTasks):
    """
    运行回测
    
    Args:
        request: 回测请求参数
    
    Returns:
        回测结果
    """
    logger.info(f"开始回测: {request.strategy_type} {request.period}")
    
    # 验证参数
    if request.initial_capital <= 0:
        raise HTTPException(status_code=400, detail="初始资金必须大于0")
    
    if request.commission_rate < 0 or request.commission_rate > 0.1:
        raise HTTPException(status_code=400, detail="手续费率必须在0-0.1之间")
    
    if request.slippage < 0 or request.slippage > 10:
        raise HTTPException(status_code=400, detail="滑点必须在0-10之间")
    
    # 生成任务ID
    task_id = f"backtest_{request.strategy_type}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    
    # 初始化任务状态
    with backtest_lock:
        backtest_tasks[task_id] = {
            'status': 'running',
            'progress': 0,
            'total': 0,
            'current_stock': '',
            'result': None,
            'error': None
        }
    
    # 后台执行回测任务
    def run_backtest_task():
        try:
            from scripts.backtest_engine import BacktestEngine
            
            # 创建回测引擎
            engine = BacktestEngine(
                strategy_type=request.strategy_type,
                stock_pool=request.stock_pool,
                custom_stocks=request.custom_stocks,
                period=request.period,
                start_date=request.start_date,
                end_date=request.end_date,
                initial_capital=request.initial_capital,
                commission_rate=request.commission_rate,
                slippage=request.slippage,
                params=request.params,
                max_position_size=request.max_position_size,
                max_positions=request.max_positions,
                stop_loss_ratio=request.stop_loss_ratio,
                take_profit_ratio=request.take_profit_ratio
            )
            
            # 执行回测
            result = engine.run()
            
            # 更新任务状态
            with backtest_lock:
                backtest_tasks[task_id]['status'] = 'completed'
                backtest_tasks[task_id]['result'] = result
                backtest_tasks[task_id]['progress'] = 100
            
            # 清理旧任务
            cleanup_old_backtest_tasks()
                
        except Exception as e:
            logger.error(f"回测任务失败: {e}")
            
            with backtest_lock:
                backtest_tasks[task_id]['status'] = 'failed'
                backtest_tasks[task_id]['error'] = str(e)
            
            # 清理旧任务
            cleanup_old_backtest_tasks()
    
    background_tasks.add_task(run_backtest_task)
    
    # 返回任务ID（实际结果需要通过查询接口获取）
    return BacktestResult(
        total_return=0.0,
        annual_return=0.0,
        sharpe_ratio=0.0,
        max_drawdown=0.0,
        win_rate=0.0,
        profit_loss_ratio=0.0,
        total_trades=0,
        avg_holding_days=0.0,
        trades=[],
        monthly_returns=[]
    )


@router.get("/progress/{task_id}")
def get_backtest_progress(task_id: str):
    """
    获取回测进度
    
    Args:
        task_id: 任务ID
    
    Returns:
        回测进度信息
    """
    with backtest_lock:
        if task_id not in backtest_tasks:
            raise HTTPException(status_code=404, detail="任务不存在")
        
        task = backtest_tasks[task_id]
        
        return {
            'task_id': task_id,
            'status': task['status'],
            'progress': task['progress'],
            'total': task['total'],
            'current_stock': task['current_stock'],
            'error': task.get('error')
        }


@router.get("/result/{task_id}", response_model=BacktestResult)
def get_backtest_result(task_id: str):
    """
    获取回测结果
    
    Args:
        task_id: 任务ID
    
    Returns:
        回测结果
    """
    with backtest_lock:
        if task_id not in backtest_tasks:
            raise HTTPException(status_code=404, detail="任务不存在")
        
        task = backtest_tasks[task_id]
        
        if task['status'] != 'completed':
            raise HTTPException(status_code=400, detail="回测尚未完成")
        
        return task['result']


@router.get("/strategies")
def get_available_strategies():
    """
    获取可用策略列表
    
    Returns:
        策略列表
    """
    strategies = [
        {
            'id': 'ma_cross',
            'name': '均线交叉策略',
            'description': '基于短期和长期均线的交叉信号进行买卖',
            'params': {
                'ma_short': {'name': '短期均线', 'default': 5, 'min': 1, 'max': 200},
                'ma_long': {'name': '长期均线', 'default': 20, 'min': 1, 'max': 200}
            }
        },
        {
            'id': 'macd',
            'name': 'MACD策略',
            'description': '基于MACD指标的信号进行买卖',
            'params': {
                'macd_fast': {'name': '快线周期', 'default': 12, 'min': 1, 'max': 50},
                'macd_slow': {'name': '慢线周期', 'default': 26, 'min': 1, 'max': 200},
                'macd_signal': {'name': '信号周期', 'default': 9, 'min': 1, 'max': 50}
            }
        },
        {
            'id': 'kdj',
            'name': 'KDJ策略',
            'description': '基于KDJ指标的超买超卖信号进行买卖',
            'params': {
                'kdj_k': {'name': 'K周期', 'default': 9, 'min': 1, 'max': 100},
                'kdj_d': {'name': 'D周期', 'default': 3, 'min': 1, 'max': 100},
                'kdj_j': {'name': 'J周期', 'default': 3, 'min': 1, 'max': 100}
            }
        },
        {
            'id': 'rsi',
            'name': 'RSI策略',
            'description': '基于RSI指标的超买超卖信号进行买卖',
            'params': {
                'rsi_period': {'name': 'RSI周期', 'default': 14, 'min': 1, 'max': 100},
                'rsi_oversold': {'name': '超卖线', 'default': 30, 'min': 0, 'max': 100},
                'rsi_overbought': {'name': '超买线', 'default': 70, 'min': 0, 'max': 100}
            }
        },
        {
            'id': 'boll',
            'name': '布林带策略',
            'description': '基于布林带的突破信号进行买卖',
            'params': {
                'boll_period': {'name': '布林周期', 'default': 20, 'min': 1, 'max': 100},
                'boll_std_dev': {'name': '标准差倍数', 'default': 2, 'min': 1, 'max': 5}
            }
        }
    ]
    
    return strategies
