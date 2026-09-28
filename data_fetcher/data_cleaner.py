"""
数据清洗模块

提供数据清洗、格式转换、异常值处理等功能
"""

import pandas as pd
import numpy as np
from typing import Optional, Dict, Any
from datetime import datetime


class DataCleaner:
    """数据清洗器"""
    
    @staticmethod
    def clean_kline_data(df: pd.DataFrame) -> pd.DataFrame:
        """
        清洗K线数据
        
        Args:
            df: 原始K线数据
        
        Returns:
            清洗后的K线数据
        """
        if df.empty:
            return df
        
        # 移除重复数据
        df = df.drop_duplicates()
        
        # 按时间排序
        if 'datetime' in df.columns:
            df = df.sort_values('datetime')
        elif 'timestamp' in df.columns:
            df = df.sort_values('timestamp')
        
        # 处理缺失值
        numeric_columns = ['open', 'high', 'low', 'close', 'volume', 'amount']
        for col in numeric_columns:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        
        # 前向填充缺失值
        df = df.fillna(method='ffill')
        
        # 移除无效数据（价格为0或负数）
        if 'close' in df.columns:
            df = df[df['close'] > 0]
        
        return df
    
    @staticmethod
    def clean_quote_data(df: pd.DataFrame) -> pd.DataFrame:
        """
        清洗行情数据
        
        Args:
            df: 原始行情数据
        
        Returns:
            清洗后的行情数据
        """
        if df.empty:
            return df
        
        # 移除重复数据
        df = df.drop_duplicates()
        
        # 处理数值字段
        numeric_fields = ['price', 'last_close', 'open', 'high', 'low', 'volume', 'amount']
        for field in numeric_fields:
            if field in df.columns:
                df[field] = pd.to_numeric(df[field], errors='coerce')
        
        return df
    
    @staticmethod
    def validate_stock_code(code: str) -> bool:
        """
        验证股票代码格式
        
        Args:
            code: 股票代码
        
        Returns:
            是否有效
        """
        if not code or not isinstance(code, str):
            return False
        
        # A股代码规则
        if len(code) == 6:
            # 沪市主板(600/601/603/605)、科创板(688)
            if code.startswith(('600', '601', '603', '605', '688')):
                return True
            # 深市主板(000/001)、原中小板(002)、创业板(300/301)
            elif code.startswith(('000', '001', '002', '300', '301')):
                return True
            # 北交所(83/87/88)
            elif code.startswith(('83', '87', '88')):
                return True
        
        return False
    
    @staticmethod
    def normalize_data_types(df: pd.DataFrame) -> pd.DataFrame:
        """
        标准化数据类型
        
        Args:
            df: 原始数据
        
        Returns:
            标准化后的数据
        """
        if df.empty:
            return df
        
        # 时间字段转换
        time_fields = ['datetime', 'timestamp', 'date', 'time']
        for field in time_fields:
            if field in df.columns:
                df[field] = pd.to_datetime(df[field], errors='coerce')
        
        # 数值字段转换
        for col in df.columns:
            if col not in time_fields:
                if df[col].dtype == 'object':
                    # 尝试转换为数值
                    numeric_series = pd.to_numeric(df[col], errors='coerce')
                    if not numeric_series.isna().all():
                        df[col] = numeric_series
        
        return df
    
    @staticmethod
    def remove_outliers(df: pd.DataFrame, column: str, method: str = 'iqr', threshold: float = 3.0) -> pd.DataFrame:
        """
        移除异常值
        
        Args:
            df: 原始数据
            column: 列名
            method: 方法 ('iqr' 或 'zscore')
            threshold: 阈值
        
        Returns:
            移除异常值后的数据
        """
        if df.empty or column not in df.columns:
            return df
        
        if method == 'iqr':
            Q1 = df[column].quantile(0.25)
            Q3 = df[column].quantile(0.75)
            IQR = Q3 - Q1
            lower_bound = Q1 - threshold * IQR
            upper_bound = Q3 + threshold * IQR
            df = df[(df[column] >= lower_bound) & (df[column] <= upper_bound)]
        
        elif method == 'zscore':
            z_scores = np.abs((df[column] - df[column].mean()) / df[column].std())
            df = df[z_scores < threshold]
        
        return df
    
    @staticmethod
    def merge_multiple_sources(data_list: list, on: str = 'datetime', how: str = 'outer') -> pd.DataFrame:
        """
        合并多个数据源的数据
        
        Args:
            data_list: 数据列表
            on: 合并键
            how: 合并方式
        
        Returns:
            合并后的数据
        """
        if not data_list:
            return pd.DataFrame()
        
        if len(data_list) == 1:
            return data_list[0]
        
        merged_df = data_list[0]
        for df in data_list[1:]:
            merged_df = pd.merge(merged_df, df, on=on, how=how)
        
        return merged_df


def clean_dataframe(df: pd.DataFrame, data_type: str = 'kline') -> pd.DataFrame:
    """
    清洗DataFrame的便捷函数
    
    Args:
        df: 原始数据
        data_type: 数据类型 ('kline', 'quote', 'general')
    
    Returns:
        清洗后的数据
    """
    cleaner = DataCleaner()
    
    if data_type == 'kline':
        df = cleaner.clean_kline_data(df)
    elif data_type == 'quote':
        df = cleaner.clean_quote_data(df)
    
    df = cleaner.normalize_data_types(df)
    
    return df