"""
回测模块

提供回测引擎和绩效评估功能，包括：
- 回测引擎核心
- 绩效评估
- 风险分析
- 可视化结果
"""

from .backtest_engine import BacktestEngine
from .performance import PerformanceEvaluator
from .risk_analysis import RiskAnalyzer
from .visualization import BacktestVisualizer

__all__ = [
    'BacktestEngine',
    'PerformanceEvaluator',
    'RiskAnalyzer',
    'BacktestVisualizer',
]