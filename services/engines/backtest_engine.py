"""
回测引擎

实现策略回测的核心逻辑
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from utils.logger import get_logger

logger = get_logger("backtest_engine")


class BacktestEngine:
    """回测引擎"""
    
    def __init__(
        self,
        strategy_type: str,
        stock_pool: str,
        custom_stocks: Optional[str] = None,
        period: str = '1d',
        start_date: str = '',
        end_date: str = '',
        initial_capital: float = 100000,
        commission_rate: float = 0.0003,
        slippage: float = 0.1,
        params: Dict[str, Any] = {},
        max_position_size: float = 20,
        max_positions: int = 5,
        stop_loss_ratio: float = 5,
        take_profit_ratio: float = 10
    ):
        """
        初始化回测引擎
        
        Args:
            strategy_type: 策略类型
            stock_pool: 股票池
            custom_stocks: 自定义股票代码
            period: 周期
            start_date: 开始日期
            end_date: 结束日期
            initial_capital: 初始资金
            commission_rate: 手续费率
            slippage: 滑点
            params: 策略参数
            max_position_size: 单笔最大仓位
            max_positions: 最大持仓数
            stop_loss_ratio: 止损比例
            take_profit_ratio: 止盈比例
        """
        self.strategy_type = strategy_type
        self.stock_pool = stock_pool
        self.custom_stocks = custom_stocks
        self.period = period
        self.start_date = start_date
        self.end_date = end_date
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.slippage = slippage
        self.params = params
        self.max_position_size = max_position_size
        self.max_positions = max_positions
        self.stop_loss_ratio = stop_loss_ratio
        self.take_profit_ratio = take_profit_ratio
        
        # 回测状态
        self.current_capital = initial_capital
        self.positions = {}  # 当前持仓
        self.trades = []  # 交易记录
        self.equity_curve = []  # 资金曲线
        self.monthly_returns = {}  # 月度收益
        
    def run(self) -> Dict[str, Any]:
        """
        运行回测
        
        Returns:
            回测结果
        """
        logger.info(f"开始回测: {self.strategy_type}")
        
        try:
            # 获取股票列表
            stocks = self._get_stock_list()
            
            if not stocks:
                raise Exception("未找到股票列表")
            
            # 遍历每只股票进行回测
            for stock in stocks:
                self._backtest_stock(stock)
            
            # 计算回测结果
            result = self._calculate_results()
            
            logger.info(f"回测完成: 总收益率 {result['total_return']}%")
            
            return result
            
        except Exception as e:
            logger.error(f"回测失败: {e}")
            raise
    
    def _get_stock_list(self) -> List[Dict[str, str]]:
        """获取股票列表"""
        if self.stock_pool == 'all':
            # 全部股票
            from data_fetcher.manager import DataSourceManager

            data_sources = DataSourceManager()
            result = data_sources.get_stock_list(market='ALL', stock_type='stock')
        elif self.stock_pool == 'watchlist':
            # 自选股（从数据库查询）
            from models.stock_models import UserStock
            from utils.database import db
            session = next(db.get_session())
            watchlist = session.query(UserStock).all()
            result = [{'code': w.code, 'name': w.stock_name or w.code} for w in watchlist]
            session.close()
        elif self.stock_pool == 'holding':
            from models.stock_models import UserStock
            # 持仓股（从数据库查询）
            # Holding stocks come from user_stocks.
            from utils.database import db
            session = next(db.get_session())
            positions = session.query(UserStock).filter(UserStock.is_holding.is_(True)).all()
            result = [{'code': p.code, 'name': p.stock_name or p.code} for p in positions]
            session.close()
        elif self.stock_pool == 'custom':
            # 自定义股票
            if not self.custom_stocks:
                return []
            codes = [c.strip() for c in self.custom_stocks.split(',')]
            result = []
            for code in codes:
                # 获取股票名称
                from models.stock_models import Stock
                from utils.database import db
                session = next(db.get_session())
                stock = session.query(Stock).filter(Stock.code == code).first()
                if stock:
                    result.append({'code': code, 'name': stock.name})
                session.close()
        else:
            result = []
        
        return result
    
    def _backtest_stock(self, stock: Dict[str, str]):
        """
        对单只股票进行回测
        
        Args:
            stock: 股票信息
        """
        code = stock['code']
        name = stock['name']
        
        try:
            # 获取K线数据
            df = self._get_kline_data(code)
            
            if df is None or df.empty:
                logger.warning(f"{code} {name} 无K线数据")
                return
            
            # 计算技术指标
            df = self._calculate_indicators(df)
            
            # 执行策略
            self._execute_strategy(df, stock)
            
        except Exception as e:
            logger.error(f"回测股票 {code} {name} 失败: {e}")
    
    def _get_kline_data(self, code: str) -> pd.DataFrame:
        """获取K线数据"""
        from data_fetcher.manager import DataSourceManager

        data_sources = DataSourceManager()
        df = data_sources.get_stock_history(
            stock_code=code,
            start_date=self.start_date,
            end_date=self.end_date,
            period=self.period,
        )
        
        return df
    
    def _calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        计算技术指标
        
        Args:
            df: K线数据
        
        Returns:
            添加了技术指标的数据
        """
        df = df.copy()
        
        # 计算均线
        if 'ma_short' in self.params:
            df['ma_short'] = df['close'].rolling(window=self.params['ma_short']).mean()
        if 'ma_long' in self.params:
            df['ma_long'] = df['close'].rolling(window=self.params['ma_long']).mean()
        
        # 计算MACD
        if 'macd_fast' in self.params:
            fast = self.params['macd_fast']
            slow = self.params['macd_slow']
            signal = self.params['macd_signal']
            
            ema_fast = df['close'].ewm(span=fast).mean()
            ema_slow = df['close'].ewm(span=slow).mean()
            df['macd'] = ema_fast - ema_slow
            df['macd_signal'] = df['macd'].ewm(span=signal).mean()
            df['macd_hist'] = df['macd'] - df['macd_signal']
        
        # 计算KDJ
        if 'kdj_k' in self.params:
            k_period = self.params['kdj_k']
            d_period = self.params['kdj_d']
            j_period = self.params['kdj_j']
            
            low_min = df['low'].rolling(window=k_period).min()
            high_max = df['high'].rolling(window=k_period).max()
            rsv = (df['close'] - low_min) / (high_max - low_min) * 100
            
            df['kdj_k'] = rsv.ewm(com=2).mean()
            df['kdj_d'] = df['kdj_k'].ewm(com=2).mean()
            df['kdj_j'] = 3 * df['kdj_k'] - 2 * df['kdj_d']
        
        # 计算RSI
        if 'rsi_period' in self.params:
            period = self.params['rsi_period']
            
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
            
            rs = gain / loss
            df['rsi'] = 100 - (100 / (1 + rs))
        
        # 计算布林带
        if 'boll_period' in self.params:
            period = self.params['boll_period']
            std_dev = self.params['boll_std_dev']
            
            df['boll_mid'] = df['close'].rolling(window=period).mean()
            df['boll_std'] = df['close'].rolling(window=period).std()
            df['boll_upper'] = df['boll_mid'] + std_dev * df['boll_std']
            df['boll_lower'] = df['boll_mid'] - std_dev * df['boll_std']
        
        return df
    
    def _execute_strategy(self, df: pd.DataFrame, stock: Dict[str, str]):
        """
        执行策略
        
        Args:
            df: K线数据
            stock: 股票信息
        """
        code = stock['code']
        name = stock['name']
        
        for i in range(len(df)):
            row = df.iloc[i]
            
            # 检查是否需要买入
            if self._should_buy(row, i):
                self._buy_stock(row, stock)
            
            # 检查是否需要卖出
            elif self._should_sell(row, i, code):
                self._sell_stock(row, code)
            
            # 检查止损止盈
            self._check_stop_loss_take_profit(row, code)
    
    def _should_buy(self, row: pd.Series, index: int) -> bool:
        """判断是否应该买入"""
        if self.strategy_type == 'ma_cross':
            # 均线交叉策略
            if pd.isna(row['ma_short']) or pd.isna(row['ma_long']):
                return False
            return row['ma_short'] > row['ma_long']
        
        elif self.strategy_type == 'macd':
            # MACD策略
            if pd.isna(row['macd']) or pd.isna(row['macd_signal']):
                return False
            return row['macd'] > row['macd_signal'] and row['macd_hist'] > 0
        
        elif self.strategy_type == 'kdj':
            # KDJ策略
            if pd.isna(row['kdj_k']) or pd.isna(row['kdj_d']):
                return False
            return row['kdj_k'] > row['kdj_d'] and row['kdj_k'] < 30
        
        elif self.strategy_type == 'rsi':
            # RSI策略
            if pd.isna(row['rsi']):
                return False
            oversold = self.params.get('rsi_oversold', 30)
            return row['rsi'] < oversold
        
        elif self.strategy_type == 'boll':
            # 布林带策略
            if pd.isna(row['boll_lower']):
                return False
            return row['close'] < row['boll_lower']
        
        return False
    
    def _should_sell(self, row: pd.Series, index: int, code: str) -> bool:
        """判断是否应该卖出"""
        if code not in self.positions:
            return False
        
        if self.strategy_type == 'ma_cross':
            # 均线交叉策略
            if pd.isna(row['ma_short']) or pd.isna(row['ma_long']):
                return False
            return row['ma_short'] < row['ma_long']
        
        elif self.strategy_type == 'macd':
            # MACD策略
            if pd.isna(row['macd']) or pd.isna(row['macd_signal']):
                return False
            return row['macd'] < row['macd_signal'] and row['macd_hist'] < 0
        
        elif self.strategy_type == 'kdj':
            # KDJ策略
            if pd.isna(row['kdj_k']) or pd.isna(row['kdj_d']):
                return False
            return row['kdj_k'] < row['kdj_d'] and row['kdj_k'] > 70
        
        elif self.strategy_type == 'rsi':
            # RSI策略
            if pd.isna(row['rsi']):
                return False
            overbought = self.params.get('rsi_overbought', 70)
            return row['rsi'] > overbought
        
        elif self.strategy_type == 'boll':
            # 布林带策略
            if pd.isna(row['boll_upper']):
                return False
            return row['close'] > row['boll_upper']
        
        return False
    
    def _buy_stock(self, row: pd.Series, stock: Dict[str, str]):
        """买入股票"""
        code = stock['code']
        name = stock['name']
        
        # 检查持仓数量限制
        if len(self.positions) >= self.max_positions:
            return
        
        # 检查是否已经持仓
        if code in self.positions:
            return
        
        # 计算买入金额
        buy_amount = self.current_capital * (self.max_position_size / 100)
        
        # 计算买入价格（考虑滑点）
        buy_price = row['close'] * (1 + self.slippage / 100)
        
        # 计算买入数量
        buy_quantity = int(buy_amount / buy_price / 100) * 100  # 整手买入
        
        if buy_quantity == 0:
            return
        
        # 计算手续费
        commission = buy_price * buy_quantity * self.commission_rate
        
        # 扣除资金
        total_cost = buy_price * buy_quantity + commission
        if total_cost > self.current_capital:
            return
        
        self.current_capital -= total_cost
        
        # 记录持仓
        self.positions[code] = {
            'code': code,
            'name': name,
            'buy_price': buy_price,
            'buy_date': row['trade_date'].strftime('%Y-%m-%d'),
            'quantity': buy_quantity,
            'cost': total_cost
        }
    
    def _sell_stock(self, row: pd.Series, code: str):
        """卖出股票"""
        if code not in self.positions:
            return
        
        position = self.positions[code]
        
        # 计算卖出价格（考虑滑点）
        sell_price = row['close'] * (1 - self.slippage / 100)
        
        # 计算手续费
        commission = sell_price * position['quantity'] * self.commission_rate
        
        # 计算卖出金额
        sell_amount = sell_price * position['quantity'] - commission
        
        # 增加资金
        self.current_capital += sell_amount
        
        # 计算盈亏
        profit_loss = sell_amount - position['cost']
        profit_loss_pct = (profit_loss / position['cost']) * 100
        
        # 计算持仓天数
        buy_date = datetime.strptime(position['buy_date'], '%Y-%m-%d')
        sell_date = row['trade_date']
        holding_days = (sell_date - buy_date).days
        
        # 记录交易
        self.trades.append({
            'code': code,
            'name': position['name'],
            'buy_date': position['buy_date'],
            'buy_price': position['buy_price'],
            'sell_date': sell_date.strftime('%Y-%m-%d'),
            'sell_price': sell_price,
            'holding_days': holding_days,
            'profit_loss_pct': round(profit_loss_pct, 2),
            'profit_loss': round(profit_loss, 2)
        })
        
        # 删除持仓
        del self.positions[code]
    
    def _check_stop_loss_take_profit(self, row: pd.Series, code: str):
        """检查止损止盈"""
        if code not in self.positions:
            return
        
        position = self.positions[code]
        current_price = row['close']
        
        # 计算盈亏比例
        profit_loss_pct = ((current_price - position['buy_price']) / position['buy_price']) * 100
        
        # 检查止损
        if profit_loss_pct <= -self.stop_loss_ratio:
            self._sell_stock(row, code)
        
        # 检查止盈
        elif profit_loss_pct >= self.take_profit_ratio:
            self._sell_stock(row, code)
    
    def _calculate_results(self) -> Dict[str, Any]:
        """计算回测结果"""
        # 计算总收益率
        total_return = ((self.current_capital - self.initial_capital) / self.initial_capital) * 100
        
        # 计算年化收益率
        if self.start_date and self.end_date:
            start = datetime.strptime(self.start_date, '%Y-%m-%d')
            end = datetime.strptime(self.end_date, '%Y-%m-%d')
            days = (end - start).days
            if days > 0:
                annual_return = ((self.current_capital / self.initial_capital) ** (365 / days) - 1) * 100
            else:
                annual_return = 0
        else:
            annual_return = 0
        
        # 计算胜率
        if self.trades:
            win_trades = [t for t in self.trades if t['profit_loss'] > 0]
            win_rate = (len(win_trades) / len(self.trades)) * 100
        else:
            win_rate = 0
        
        # 计算盈亏比
        if self.trades:
            win_trades = [t for t in self.trades if t['profit_loss'] > 0]
            loss_trades = [t for t in self.trades if t['profit_loss'] < 0]
            
            if win_trades and loss_trades:
                avg_win = np.mean([t['profit_loss'] for t in win_trades])
                avg_loss = np.mean([abs(t['profit_loss']) for t in loss_trades])
                profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0
            else:
                profit_loss_ratio = 0
        else:
            profit_loss_ratio = 0
        
        # 计算平均持仓天数
        if self.trades:
            avg_holding_days = np.mean([t['holding_days'] for t in self.trades])
        else:
            avg_holding_days = 0
        
        # 计算最大回撤（简化版）
        max_drawdown = 0
        if self.trades:
            equity = self.initial_capital
            peak_equity = self.initial_capital
            
            for trade in self.trades:
                equity += trade['profit_loss']
                peak_equity = max(peak_equity, equity)
                drawdown = (peak_equity - equity) / peak_equity * 100
                max_drawdown = max(max_drawdown, drawdown)
        
        # 计算夏普比率（简化版，假设无风险利率为3%）
        if self.trades:
            returns = [t['profit_loss_pct'] for t in self.trades]
            if len(returns) > 1:
                avg_return = np.mean(returns)
                std_return = np.std(returns)
                if std_return > 0:
                    sharpe_ratio = (avg_return - 3) / std_return
                else:
                    sharpe_ratio = 0
            else:
                sharpe_ratio = 0
        else:
            sharpe_ratio = 0
        
        # 计算月度收益
        monthly_returns = []
        if self.trades:
            monthly_data = {}
            for trade in self.trades:
                month = trade['sell_date'][:7] if trade['sell_date'] else trade['buy_date'][:7]
                if month not in monthly_data:
                    monthly_data[month] = 0
                monthly_data[month] += trade['profit_loss']
            
            for month, profit in monthly_data.items():
                monthly_returns.append({
                    'month': month,
                    'return': round((profit / self.initial_capital) * 100, 2)
                })
        
        return {
            'total_return': round(total_return, 2),
            'annual_return': round(annual_return, 2),
            'sharpe_ratio': round(sharpe_ratio, 2),
            'max_drawdown': round(-max_drawdown, 2),
            'win_rate': round(win_rate, 2),
            'profit_loss_ratio': round(profit_loss_ratio, 2),
            'total_trades': len(self.trades),
            'avg_holding_days': round(avg_holding_days, 1),
            'trades': self.trades,
            'monthly_returns': monthly_returns
        }
