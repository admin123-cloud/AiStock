# -*- coding: utf-8 -*-
"""AiStock 通用「AI 策略挖掘工坊（Alpha Mining Studio）」核心包。

模块分层：
1. feature_store: 只读 ClickHouse 全市场历史行情，构建严格 T-1 无未来函数特征宽表与本地 Parquet 缓存；
2. backtest_engine: 严格向量化日频组合回测器（三段切分 + 每日槽位等权 + A股摩擦与涨跌停约束）；
3. quality_gates: 五道防过拟合硬门禁与失败归因诊断；
4. rule_miner: Pure-NumPy 浅层决策树规则提纯 + 模板假设族双引擎挖掘；
5. contract_exporter: 兼容 config/strategy_contracts/ 的候选影子合约导出器。
"""
from __future__ import annotations

from typing import Any

MINING_TEMPLATES: dict[str, dict[str, Any]] = {
    "mainwave_offense": {
        "template_id": "mainwave_offense",
        "name": "主升进攻选股因子增强",
        "description": "严格锁死纯主升进攻周期（offensive/extreme_offensive），挖掘趋势多头、相对强度与行业共振的高超额因子",
        "target_regimes": ["offensive", "extreme_offensive"],
        "default_hold_days": 5,
        "default_stop_loss": -0.07,
        "default_take_profit": 0.15,
        "default_rank_by": "rs_20d_vs_hs300",
        "max_drawdown_limit": 0.22,
    },
    "panic_rebound": {
        "template_id": "panic_rebound",
        "name": "防守/极端冰点超跌反抽",
        "description": "面向防守与恐慌冰点期，挖掘连续杀跌+放量承接后的 1~3 日短线高胜率反抽规则",
        "target_regimes": ["defensive", "extreme_panic"],
        "default_hold_days": 3,
        "default_stop_loss": -0.05,
        "default_take_profit": 0.08,
        "default_rank_by": "vol_ratio_5d",
        "max_drawdown_limit": 0.15,
    },
    "sideways_swing": {
        "template_id": "sideways_swing",
        "name": "横盘震荡波段买点",
        "description": "严格聚焦横盘平衡市（balanced），挖掘缩量回踩支撑位与强行业低吸的高胜率波段规则",
        "target_regimes": ["balanced"],
        "default_hold_days": 4,
        "default_stop_loss": -0.06,
        "default_take_profit": 0.10,
        "default_rank_by": "industry_ret_5d_rank",
        "max_drawdown_limit": 0.16,
    },
    "sector_rotation": {
        "template_id": "sector_rotation",
        "name": "板块轮动与龙二补涨",
        "description": "依托行业板块截面强弱，挖掘主线板块启动初期的补涨/领涨个股规则",
        "target_regimes": ["offensive", "balanced", "extreme_offensive"],
        "default_hold_days": 5,
        "default_stop_loss": -0.07,
        "default_take_profit": 0.14,
        "default_rank_by": "industry_ret_5d_rank",
        "max_drawdown_limit": 0.20,
    },
    "custom_hypothesis": {
        "template_id": "custom_hypothesis",
        "name": "自定义交易假设验证",
        "description": "输入自定义 DSL 条件，走完整三段切分与五道防过拟合硬门禁验真",
        "target_regimes": ["offensive", "extreme_offensive", "balanced", "defensive", "extreme_panic"],
        "default_hold_days": 5,
        "default_stop_loss": -0.07,
        "default_take_profit": 0.12,
        "default_rank_by": "rs_20d_vs_hs300",
        "max_drawdown_limit": 0.22,
    },
}
