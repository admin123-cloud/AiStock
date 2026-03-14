-- StockPy 数据库初始化脚本
-- 支持 SQLite 和 MySQL

-- ======================== 股票基础信息表 ========================
-- DROP TABLE IF EXISTS stock;
CREATE TABLE IF NOT EXISTS stock (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code VARCHAR(10) UNIQUE NOT NULL COMMENT '股票代码 如600000',
  name VARCHAR(50) NOT NULL COMMENT '股票名称',
  market VARCHAR(2) NOT NULL COMMENT '所属市场：SH(上海)、SZ(深圳)',
  industry VARCHAR(20) COMMENT '所属行业',
  is_active INTEGER DEFAULT 1 COMMENT '是否在交易中：1激活，0已停牌',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_stock_code ON stock(code);
CREATE INDEX IF NOT EXISTS idx_stock_code_market ON stock(code, market);


-- ======================== K线数据表 ========================
-- DROP TABLE IF EXISTS kline_data;
CREATE TABLE IF NOT EXISTS kline_data (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  stock_id INTEGER COMMENT '股票ID(外键)',
  code VARCHAR(10) NOT NULL COMMENT '股票代码',
  date DATE NOT NULL COMMENT '交易日期',
  period VARCHAR(2) NOT NULL COMMENT '周期：D(日)、W(周)、M(月)',
  open_price REAL COMMENT '开盘价',
  high_price REAL COMMENT '最高价',
  low_price REAL COMMENT '最低价',
  close_price REAL COMMENT '收盘价',
  volume INTEGER COMMENT '成交量（手）',
  amount REAL COMMENT '成交额（元）',
  change_percent REAL COMMENT '涨跌幅（%）',
  turnover_rate REAL COMMENT '成交量比（%）',
  ma5 REAL COMMENT '5日均线',
  ma10 REAL COMMENT '10日均线',
  ma20 REAL COMMENT '20日均线',
  ma50 REAL COMMENT '50日均线',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (stock_id) REFERENCES stock(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_kline_code ON kline_data(code);
CREATE INDEX IF NOT EXISTS idx_kline_date ON kline_data(date);
CREATE INDEX IF NOT EXISTS idx_kline_code_date_period ON kline_data(code, date, period);


-- ======================== 大盘情绪数据表 ========================
-- DROP TABLE IF EXISTS sentiment_data;
CREATE TABLE IF NOT EXISTS sentiment_data (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date DATE UNIQUE NOT NULL COMMENT '交易日期',
  up_count INTEGER NOT NULL COMMENT '上涨家数',
  down_count INTEGER NOT NULL COMMENT '下跌家数',
  unchanged_count INTEGER COMMENT '平盘家数',
  avg_change_percent REAL NOT NULL COMMENT '平均涨跌幅（%）',
  total_turnover_amount REAL NOT NULL COMMENT '沪深两市总成交额（亿元）',
  daily_turnover_change REAL COMMENT '日成交额环比变化（%）',
  up_5_percent_count INTEGER NOT NULL COMMENT '涨幅5%以上家数',
  down_5_percent_count INTEGER NOT NULL COMMENT '跌幅5%以上家数',
  limit_up_count INTEGER NOT NULL COMMENT '涨停家数',
  limit_down_count INTEGER NOT NULL COMMENT '跌停家数',
  sentiment_score REAL COMMENT '综合情绪指数（0-100）',
  sentiment_level VARCHAR(10) COMMENT '情绪等级',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sentiment_date ON sentiment_data(date);


-- ======================== 技术指标表 ========================
-- DROP TABLE IF EXISTS technical_indicator;
CREATE TABLE IF NOT EXISTS technical_indicator (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code VARCHAR(10) NOT NULL COMMENT '股票代码',
  date DATE NOT NULL COMMENT '交易日期',
  period VARCHAR(2) NOT NULL COMMENT '周期',
  macd_dif REAL COMMENT 'MACD DIF',
  macd_dea REAL COMMENT 'MACD DEA',
  macd_histogram REAL COMMENT 'MACD直方图',
  rsi_6 REAL COMMENT 'RSI(6)',
  rsi_12 REAL COMMENT 'RSI(12)',
  rsi_24 REAL COMMENT 'RSI(24)',
  bb_upper REAL COMMENT '布林带上轨',
  bb_middle REAL COMMENT '布林带中轨',
  bb_lower REAL COMMENT '布林带下轨',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_indicator_code_date ON technical_indicator(code, date);


-- ======================== 回测结果表 ========================
-- DROP TABLE IF EXISTS backtest_result;
CREATE TABLE IF NOT EXISTS backtest_result (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_name VARCHAR(50) NOT NULL COMMENT '策略名称',
  start_date DATE NOT NULL COMMENT '回测开始日期',
  end_date DATE NOT NULL COMMENT '回测结束日期',
  total_trades INTEGER NOT NULL COMMENT '总交易次数',
  winning_trades INTEGER NOT NULL COMMENT '盈利交易数',
  losing_trades INTEGER NOT NULL COMMENT '亏损交易数',
  total_return REAL NOT NULL COMMENT '总收益率（%）',
  annual_return REAL NOT NULL COMMENT '年化收益率（%）',
  max_drawdown REAL NOT NULL COMMENT '最大回撤（%）',
  sharpe_ratio REAL NOT NULL COMMENT '夏普比率',
  win_rate REAL NOT NULL COMMENT '胜率（%）',
  profit_factor REAL NOT NULL COMMENT '利润因子',
  initial_capital REAL NOT NULL COMMENT '初始资金（元）',
  final_capital REAL NOT NULL COMMENT '最终资金（元）',
  max_capital REAL NOT NULL COMMENT '最大账户净值（元）',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_backtest_strategy ON backtest_result(strategy_name);
CREATE INDEX IF NOT EXISTS idx_backtest_date ON backtest_result(start_date, end_date);


-- ======================== 示例数据 ========================
-- 插入示例股票数据
INSERT OR IGNORE INTO stock (code, name, market, industry, is_active)
VALUES
  ('sh000001', '上证指数', 'SH', '指数', 1),
  ('sz399001', '深证成指', 'SZ', '指数', 1),
  ('sz399006', '创业板指', 'SZ', '指数', 1),
  ('sh600000', '浦发银行', 'SH', '银行', 1),
  ('sz000858', '五粮液', 'SZ', '食品饮料', 1);

-- 提示：使用以下命令导入此文件
-- SQLite: sqlite3 stockpy.db < init.sql
-- MySQL: mysql -u user -p database < init.sql
