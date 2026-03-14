"""
技术指标计算服务模块
实现常用的技术分析指标
"""
import logging
from typing import List, Dict, Optional
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class IndicatorCalculator:
    """技术指标计算器"""
    
    @staticmethod
    def calculate_ma(prices: List[float], period: int) -> List[Optional[float]]:
        """
        计算移动平均线（SMA）
        
        Args:
            prices: 价格列表
            period: 周期
            
        Returns:
            MA值列表
        """
        if len(prices) < period:
            return [None] * len(prices)
        
        ma_values = [None] * (period - 1)
        for i in range(period - 1, len(prices)):
            ma_values.append(np.mean(prices[i - period + 1:i + 1]))
        
        return ma_values
    
    @staticmethod
    def calculate_ema(prices: List[float], period: int) -> List[Optional[float]]:
        """
        计算指数移动平均线（EMA）
        
        Args:
            prices: 价格列表
            period: 周期
            
        Returns:
            EMA值列表
        """
        if len(prices) < period:
            return [None] * len(prices)
        
        multiplier = 2 / (period + 1)
        ema_values = [None] * (period - 1)
        ema_values.append(np.mean(prices[:period]))  # 初始EMA为SMA
        
        for i in range(period, len(prices)):
            ema = prices[i] * multiplier + ema_values[-1] * (1 - multiplier)
            ema_values.append(ema)
        
        return ema_values
    
    @staticmethod
    def calculate_macd(
        prices: List[float],
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9
    ) -> Dict[str, List[Optional[float]]]:
        """
        计算MACD指标
        
        Args:
            prices: 价格列表
            fast_period: 快速EMA周期
            slow_period: 缓慢EMA周期
            signal_period: 信号线周期
            
        Returns:
            包含DIF、DEA、HISTOGRAM的字典
        """
        fast_ema = IndicatorCalculator.calculate_ema(prices, fast_period)
        slow_ema = IndicatorCalculator.calculate_ema(prices, slow_period)
        
        # 计算DIF（差离值）
        dif = []
        for i in range(len(prices)):
            if fast_ema[i] is not None and slow_ema[i] is not None:
                dif.append(fast_ema[i] - slow_ema[i])
            else:
                dif.append(None)
        
        # 计算DEA（信号线）
        dea = IndicatorCalculator.calculate_ema([d for d in dif if d is not None], signal_period)
        
        # 补齐长度
        dea = [None] * (len(dif) - len(dea)) + dea
        
        # 计算HISTOGRAM（柱状图）
        histogram = []
        for i in range(len(dif)):
            if dif[i] is not None and dea[i] is not None:
                histogram.append(dif[i] - dea[i])
            else:
                histogram.append(None)
        
        return {
            "dif": dif,
            "dea": dea,
            "histogram": histogram
        }
    
    @staticmethod
    def calculate_rsi(prices: List[float], period: int = 14) -> List[Optional[float]]:
        """
        计算RSI（相对强弱指数）
        
        Args:
            prices: 价格列表
            period: 周期
            
        Returns:
            RSI值列表
        """
        if len(prices) < period + 1:
            return [None] * len(prices)
        
        rsi_values = [None] * period
        
        for i in range(period, len(prices)):
            ups = []
            downs = []
            
            for j in range(i - period + 1, i + 1):
                change = prices[j] - prices[j - 1]
                if change > 0:
                    ups.append(change)
                    downs.append(0)
                else:
                    ups.append(0)
                    downs.append(abs(change))
            
            avg_up = np.mean(ups)
            avg_down = np.mean(downs)
            
            if avg_down == 0:
                rsi = 100 if avg_up > 0 else 0
            else:
                rs = avg_up / avg_down
                rsi = 100 - (100 / (1 + rs))
            
            rsi_values.append(rsi)
        
        return rsi_values
    
    @staticmethod
    def calculate_bollinger_bands(
        prices: List[float],
        period: int = 20,
        std_dev: int = 2
    ) -> Dict[str, List[Optional[float]]]:
        """
        计算布林带（Bollinger Bands）
        
        Args:
            prices: 价格列表
            period: 周期
            std_dev: 标准差倍数
            
        Returns:
            包含上轨、中轨、下轨的字典
        """
        if len(prices) < period:
            return {
                "upper": [None] * len(prices),
                "middle": [None] * len(prices),
                "lower": [None] * len(prices),
            }
        
        middle = IndicatorCalculator.calculate_ma(prices, period)
        upper = []
        lower = []
        
        for i in range(len(prices)):
            if middle[i] is None:
                upper.append(None)
                lower.append(None)
            else:
                start = max(0, i - period + 1)
                std = np.std(prices[start:i + 1])
                upper.append(middle[i] + std_dev * std)
                lower.append(middle[i] - std_dev * std)
        
        return {
            "upper": upper,
            "middle": middle,
            "lower": lower
        }
    
    @staticmethod
    def calculate_atr(
        high: List[float],
        low: List[float],
        close: List[float],
        period: int = 14
    ) -> List[Optional[float]]:
        """
        计算ATR（真实波幅）
        
        Args:
            high: 最高价列表
            low: 最低价列表
            close: 收盘价列表
            period: 周期
            
        Returns:
            ATR值列表
        """
        if len(high) < period:
            return [None] * len(high)
        
        # 计算真实波幅
        tr_values = []
        for i in range(len(high)):
            if i == 0:
                tr = high[i] - low[i]
            else:
                tr = max(
                    high[i] - low[i],
                    abs(high[i] - close[i - 1]),
                    abs(low[i] - close[i - 1])
                )
            tr_values.append(tr)
        
        # 计算ATR
        atr_values = [None] * (period - 1)
        atr_values.append(np.mean(tr_values[:period]))
        
        for i in range(period, len(tr_values)):
            atr = (atr_values[-1] * (period - 1) + tr_values[i]) / period
            atr_values.append(atr)
        
        return atr_values


class IndicatorService:
    """指标计算服务"""
    
    def __init__(self):
        self.calculator = IndicatorCalculator()
    
    def calculate_indicators_from_klines(self, klines: List[Dict]) -> List[Dict]:
        """
        从K线数据计算所有技术指标
        
        Args:
            klines: K线数据列表
            
        Returns:
            包含指标数据的K线列表
        """
        if not klines:
            return []
        
        df = pd.DataFrame(klines)
        
        # 提取价格数据
        closes = df['close_price'].tolist()
        highs = df['high_price'].tolist() if 'high_price' in df.columns else closes
        lows = df['low_price'].tolist() if 'low_price' in df.columns else closes
        
        # 计算MA
        for period in [5, 10, 20, 50, 100, 200]:
            ma_values = self.calculator.calculate_ma(closes, period)
            df[f'ma{period}'] = ma_values
        
        # 计算MACD
        macd = self.calculator.calculate_macd(closes)
        df['macd_dif'] = macd['dif']
        df['macd_dea'] = macd['dea']
        df['macd_histogram'] = macd['histogram']
        
        # 计算RSI
        for period in [6, 12, 24]:
            rsi_values = self.calculator.calculate_rsi(closes, period)
            df[f'rsi{period}'] = rsi_values
        
        # 计算布林带
        bb = self.calculator.calculate_bollinger_bands(closes)
        df['bb_upper'] = bb['upper']
        df['bb_middle'] = bb['middle']
        df['bb_lower'] = bb['lower']
        
        # 计算ATR
        atr_values = self.calculator.calculate_atr(highs, lows, closes)
        df['atr'] = atr_values
        
        return df.to_dict('records')
