"""
大盘情绪计算服务模块
计算市场整体情绪指标
"""
import logging
from datetime import date
from typing import List, Dict, Optional
import numpy as np
from app.config import settings

logger = logging.getLogger(__name__)


class SentimentCalculator:
    """大盘情绪计算器"""
    
    def __init__(self, weights: Optional[Dict] = None):
        """
        初始化计算器
        
        Args:
            weights: 各指标权重配置
        """
        self.weights = weights or settings.sentiment_weights
        logger.info(f"情绪计算器初始化，权重配置：{self.weights}")
    
    def calculate_sentiment(
        self,
        klines: List[Dict],
        date: date
    ) -> Dict:
        """
        计算大盘综合情绪指数
        
        Args:
            klines: 当日所有股票K线数据
            date: 交易日期
            
        Returns:
            包含各项指标和综合评分的情绪数据字典
        """
        if not klines:
            logger.warning(f"未获取到 {date} 的K线数据")
            return self._create_empty_sentiment(date)
        
        # 统计基础指标
        up_count = sum(1 for k in klines if k.get('change_percent', 0) > 0)
        down_count = sum(1 for k in klines if k.get('change_percent', 0) < 0)
        unchanged_count = sum(1 for k in klines if k.get('change_percent', 0) == 0)
        
        # 计算平均涨跌幅
        changes = [k.get('change_percent', 0) for k in klines]
        avg_change_percent = np.mean(changes) if changes else 0
        
        # 计算成交额
        total_turnover = sum(k.get('amount', 0) for k in klines)
        total_turnover_amount = total_turnover / 1e8  # 转换为亿元
        
        # 统计涨跌幅特殊情况
        up_5_percent = sum(1 for k in klines if k.get('change_percent', 0) >= 5)
        down_5_percent = sum(1 for k in klines if k.get('change_percent', 0) <= -5)
        limit_up = sum(
            1 for k in klines 
            if (k.get('high_price', 0) == k.get('close_price', 0) and 
                k.get('change_percent', 0) > 0)
        )
        limit_down = sum(
            1 for k in klines 
            if (k.get('low_price', 0) == k.get('close_price', 0) and 
                k.get('change_percent', 0) < 0)
        )
        
        # 计算综合情绪

        sentiment_score = self._calculate_sentiment_score(
            up_count=up_count,
            down_count=down_count,
            avg_change=avg_change_percent,
            turnover=total_turnover_amount,
            up_5_percent=up_5_percent,
            down_5_percent=down_5_percent,
            limit_up=limit_up,
            limit_down=limit_down,
        )
        
        # 确定情绪等级
        sentiment_level = self._get_sentiment_level(sentiment_score)
        
        return {
            'date': date,
            'up_count': up_count,
            'down_count': down_count,
            'unchanged_count': unchanged_count,
            'avg_change_percent': round(avg_change_percent, 2),
            'total_turnover_amount': round(total_turnover_amount, 2),
            'up_5_percent_count': up_5_percent,
            'down_5_percent_count': down_5_percent,
            'limit_up_count': limit_up,
            'limit_down_count': limit_down,
            'sentiment_score': round(sentiment_score, 2),
            'sentiment_level': sentiment_level,
        }
    
    def _calculate_sentiment_score(
        self,
        up_count: int,
        down_count: int,
        avg_change: float,
        turnover: float,
        up_5_percent: int,
        down_5_percent: int,
        limit_up: int,
        limit_down: int,
    ) -> float:
        """
        计算综合情绪评分（0-100分）
        
        基础公式：
        - 上涨家数占比 (权重20%)
        - 下跌家数占比 (权重20%)
        - 平均涨跌幅 (权重15%)
        - 成交额 (权重15%)
        - 涨幅5%+ (权重10%)
        - 跌幅5%+ (权重10%)
        - 涨跌停 (权重10%)
        """
        total_count = up_count + down_count
        if total_count == 0:
            return 50  # 无数据时返回中性
        
        # 计算各分项评分（0-100）
        
        # 1. 上涨家数占比 (越高越乐观)
        up_ratio = min(up_count / total_count, 1.0)
        up_score = up_ratio * 100
        
        # 2. 下跌家数占比 (越低越乐观，取反)
        down_ratio = min(down_count / total_count, 1.0)
        down_score = (1 - down_ratio) * 100
        
        # 3. 平均涨跌幅 (范围通常-5% ~ +5%)
        change_score = 50 + avg_change * 5  # 归一化到0-100
        change_score = max(0, min(100, change_score))
        
        # 4. 成交额 (参考历史平均5000亿，超过为乐观)
        turnover_score = min(turnover / 5000 * 100, 100)
        
        # 5. 涨幅5%+ (越多越乐观)
        up_5_score = min(up_5_percent / (total_count * 0.1) * 100, 100)
        
        # 6. 跌幅5%+ (越少越乐观)
        down_5_score = max(0, 100 - down_5_percent / (total_count * 0.1) * 100)
        
        # 7. 涨跌停 (总体考量)
        limit_score = 50 + (limit_up - limit_down) / max(total_count * 0.01, 1) * 10
        limit_score = max(0, min(100, limit_score))
        
        # 加权计算综合评分
        sentiment_score = (
            up_score * self.weights.get('up_count', 0.2) +
            down_score * self.weights.get('down_count', 0.2) +
            change_score * self.weights.get('avg_change', 0.15) +
            turnover_score * self.weights.get('turnover_rate', 0.15) +
            up_5_score * self.weights.get('up_5_percent', 0.1) +
            down_5_score * self.weights.get('down_5_percent', 0.1) +
            limit_score * self.weights.get('limit_up', 0.05) +
            limit_score * self.weights.get('limit_down', 0.05)
        )
        
        return max(0, min(100, sentiment_score))
    
    def _get_sentiment_level(self, score: float) -> str:
        """根据评分确定情绪等级"""
        if score >= 80:
            return "极度乐观"
        elif score >= 65:
            return "乐观"
        elif score >= 55:
            return "中性偏乐观"
        elif score >= 45:
            return "中性"
        elif score >= 35:
            return "中性偏悲观"
        elif score >= 20:
            return "悲观"
        else:
            return "极度悲观"
    
    def _create_empty_sentiment(self, date: date) -> Dict:
        """创建空情绪数据"""
        return {
            'date': date,
            'up_count': 0,
            'down_count': 0,
            'unchanged_count': 0,
            'avg_change_percent': 0,
            'total_turnover_amount': 0,
            'up_5_percent_count': 0,
            'down_5_percent_count': 0,
            'limit_up_count': 0,
            'limit_down_count': 0,
            'sentiment_score': 50,
            'sentiment_level': '中性',
        }


class SentimentService:
    """情绪数据服务"""
    
    def __init__(self):
        self.calculator = SentimentCalculator()
    
    def calculate_daily_sentiment(
        self,
        klines: List[Dict],
        date: date
    ) -> Dict:
        """计算日度情绪数据"""
        return self.calculator.calculate_sentiment(klines, date)
    
    def get_sentiment_trend(
        self,
        sentiments: List[Dict],
        window: int = 5
    ) -> Dict:
        """
        计算情绪趋势（移动平均）
        
        Args:
            sentiments: 历史情绪数据列表
            window: 移动窗口大小
            
        Returns:
            包含趋势数据的字典
        """
        if not sentiments or len(sentiments) < window:
            return {}
        
        scores = [s.get('sentiment_score', 50) for s in sentiments[-window:]]
        trend_avg = np.mean(scores)
        trend_direction = "上升" if scores[-1] > scores[0] else "下降"
        
        return {
            'trend_average': round(trend_avg, 2),
            'trend_direction': trend_direction,
            'volatility': round(np.std(scores), 2),
        }
