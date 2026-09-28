-- ========================================
-- AiStock 数据库建表脚本
-- 数据库：MySQL 8.0+
-- 字符集：utf8mb4
-- ========================================

-- 创建数据库
CREATE DATABASE IF NOT EXISTS stock
DEFAULT CHARACTER SET utf8mb4 
COLLATE utf8mb4_unicode_ci;

USE stock;

-- ========================================
-- 1. 基础信息表
-- ========================================

-- 1.1 股票基础信息表  ✅️
CREATE TABLE `stocks`  (
  `id` int NOT NULL AUTO_INCREMENT,
  `code` varchar(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '股票代码',
  `name` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '股票名称',
  `market` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '市场：sh/sz/bj',
  `type` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '类型：stock/index/sector/theme',
  `region` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '所属地区',
  `industry` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '所属行业',
  `industry_code` varchar(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '行业指数代码',
  `st` tinyint(1) NULL DEFAULT 0 COMMENT '是否ST 0=否 1=是',
  `quit` tinyint(1) NULL DEFAULT 0 COMMENT '是否退市 0=否 1=是',
  `list_date` date NULL DEFAULT NULL COMMENT '上市日期',
  `self_selected` tinyint(1) NULL DEFAULT 0 COMMENT '是否自选',
  `holding` tinyint(1) NULL DEFAULT 0 COMMENT '是否持仓',
  `float_share` decimal(18,4) NULL DEFAULT 0.0000 COMMENT '流通股本(万股)',
  `total_share` decimal(18,4) NULL DEFAULT 0.0000 COMMENT '总股本(万股)',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `code`(`code`) USING BTREE,  -- 必须保留
  INDEX `idx_market_type`(`market`, `type`) USING BTREE,  -- 高频查询
  INDEX `idx_industry`(`industry`) USING BTREE  -- 行业筛选
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '股票基础信息表';


-- 1.2 板块信息表
CREATE TABLE IF NOT EXISTS sectors (
    id INT PRIMARY KEY AUTO_INCREMENT,
    code VARCHAR(10) NOT NULL UNIQUE COMMENT '板块代码',
    name VARCHAR(50) NOT NULL COMMENT '板块名称',
    type VARCHAR(20) NOT NULL COMMENT '类型：industry/theme',
    parent_code VARCHAR(10) COMMENT '父板块代码',
    level INT DEFAULT 1 COMMENT '层级',
    stock_count INT DEFAULT 0 COMMENT '成分股数量',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_type (type),
    INDEX idx_parent (parent_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='板块信息表';

-- 1.3 板块成分股关联表
CREATE TABLE IF NOT EXISTS sector_stocks (
    id INT PRIMARY KEY AUTO_INCREMENT,
    sector_code VARCHAR(10) NOT NULL COMMENT '板块代码',
    stock_code VARCHAR(10) NOT NULL COMMENT '股票代码',
    weight DECIMAL(10, 4) COMMENT '权重',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_sector_stock (sector_code, stock_code),
    INDEX idx_stock (stock_code),
    INDEX idx_sector (sector_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='板块成分股关联表';

-- ========================================
-- 2. K线数据表
-- ========================================

-- 2.1 日线数据表（按年分区）
CREATE TABLE IF NOT EXISTS kline_daily (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    code VARCHAR(10) NOT NULL COMMENT '股票代码',
    trade_date DATE NOT NULL COMMENT '交易日期',
    open DECIMAL(10, 3) NOT NULL COMMENT '开盘价',
    high DECIMAL(10, 3) NOT NULL COMMENT '最高价',
    low DECIMAL(10, 3) NOT NULL COMMENT '最低价',
    close DECIMAL(10, 3) NOT NULL COMMENT '收盘价',
    volume BIGINT NOT NULL DEFAULT 0 COMMENT '成交量(手)',
    amount DECIMAL(20, 2) NOT NULL DEFAULT 0 COMMENT '成交额(元)',
    amplitude DECIMAL(10, 2) COMMENT '振幅(%)',
    change_pct DECIMAL(10, 2) COMMENT '涨跌幅(%)',
    change_amount DECIMAL(10, 3) COMMENT '涨跌额',
    turnover_rate DECIMAL(10, 2) COMMENT '换手率(%)',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code_date (code, trade_date),
    INDEX idx_date (trade_date),
    INDEX idx_close (close)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 
PARTITION BY RANGE (TO_DAYS(trade_date)) (
    PARTITION p2020 VALUES LESS THAN (TO_DAYS('2021-01-01')),
    PARTITION p2021 VALUES LESS THAN (TO_DAYS('2022-01-01')),
    PARTITION p2022 VALUES LESS THAN (TO_DAYS('2023-01-01')),
    PARTITION p2023 VALUES LESS THAN (TO_DAYS('2024-01-01')),
    PARTITION p2024 VALUES LESS THAN (TO_DAYS('2025-01-01')),
    PARTITION p2025 VALUES LESS THAN (TO_DAYS('2026-01-01')),
    PARTITION pfuture VALUES LESS THAN MAXVALUE
) COMMENT='日线数据表';

-- 2.2 周线数据表
CREATE TABLE IF NOT EXISTS kline_weekly (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    code VARCHAR(10) NOT NULL COMMENT '股票代码',
    week_start_date DATE NOT NULL COMMENT '周开始日期',
    week_end_date DATE NOT NULL COMMENT '周结束日期',
    open DECIMAL(10, 3) NOT NULL COMMENT '开盘价',
    high DECIMAL(10, 3) NOT NULL COMMENT '最高价',
    low DECIMAL(10, 3) NOT NULL COMMENT '最低价',
    close DECIMAL(10, 3) COMMENT '收盘价',
    volume BIGINT NOT NULL DEFAULT 0 COMMENT '成交量(手)',
    amount DECIMAL(20, 2) NOT NULL DEFAULT 0 COMMENT '成交额(元)',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code_week (code, week_start_date),
    INDEX idx_week_end (week_end_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='周线数据表';

-- 2.3 月线数据表
CREATE TABLE IF NOT EXISTS kline_monthly (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    code VARCHAR(10) NOT NULL COMMENT '股票代码',
    month_start_date DATE NOT NULL COMMENT '月开始日期',
    month_end_date DATE NOT NULL COMMENT '月结束日期',
    open DECIMAL(10, 3) NOT NULL COMMENT '开盘价',
    high DECIMAL(10, 3) NOT NULL COMMENT '最高价',
    low DECIMAL(10, 3) NOT NULL COMMENT '最低价',
    close DECIMAL(10, 3) COMMENT '收盘价',
    volume BIGINT NOT NULL DEFAULT 0 COMMENT '成交量(手)',
    amount DECIMAL(20, 2) NOT NULL DEFAULT 0 COMMENT '成交额(元)',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code_month (code, month_start_date),
    INDEX idx_month_end (month_end_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='月线数据表';

-- 2.4 季度线数据表
CREATE TABLE IF NOT EXISTS kline_quarterly (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    code VARCHAR(10) NOT NULL COMMENT '股票代码',
    quarter INT NOT NULL COMMENT '季度(1-4)',
    year INT NOT NULL COMMENT '年份',
    open DECIMAL(10, 3) NOT NULL COMMENT '开盘价',
    high DECIMAL(10, 3) NOT NULL COMMENT '最高价',
    low DECIMAL(10, 3) NOT NULL COMMENT '最低价',
    close DECIMAL(10, 3) COMMENT '收盘价',
    volume BIGINT NOT NULL DEFAULT 0 COMMENT '成交量(手)',
    amount DECIMAL(20, 2) NOT NULL DEFAULT 0 COMMENT '成交额(元)',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code_quarter (code, year, quarter),
    INDEX idx_year (year)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='季度线数据表';

-- 2.5 年线数据表
CREATE TABLE IF NOT EXISTS kline_yearly (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    code VARCHAR(10) NOT NULL COMMENT '股票代码',
    year INT NOT NULL COMMENT '年份',
    open DECIMAL(10, 3) NOT NULL COMMENT '开盘价',
    high DECIMAL(10, 3) NOT NULL COMMENT '最高价',
    low DECIMAL(10, 3) NOT NULL COMMENT '最低价',
    close DECIMAL(10, 3) COMMENT '收盘价',
    volume BIGINT NOT NULL DEFAULT 0 COMMENT '成交量(手)',
    amount DECIMAL(20, 2) NOT NULL DEFAULT 0 COMMENT '成交额(元)',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code_year (code, year),
    INDEX idx_year (year)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='年线数据表';

-- 2.6 1分钟K线数据表（按月分区）
CREATE TABLE IF NOT EXISTS kline_minute_1 (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    code VARCHAR(10) NOT NULL COMMENT '股票代码',
    datetime DATETIME NOT NULL COMMENT '日期时间',
    open DECIMAL(10, 3) NOT NULL COMMENT '开盘价',
    high DECIMAL(10, 3) NOT NULL COMMENT '最高价',
    low DECIMAL(10, 3) NOT NULL COMMENT '最低价',
    close DECIMAL(10, 3) NOT NULL COMMENT '收盘价',
    volume BIGINT NOT NULL DEFAULT 0 COMMENT '成交量(手)',
    amount DECIMAL(20, 2) NOT NULL DEFAULT 0 COMMENT '成交额(元)',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code_datetime (code, datetime),
    INDEX idx_datetime (datetime),
    INDEX idx_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 
PARTITION BY RANGE (TO_DAYS(datetime)) (
    PARTITION p202501 VALUES LESS THAN (TO_DAYS('2025-02-01')),
    PARTITION p202502 VALUES LESS THAN (TO_DAYS('2025-03-01')),
    PARTITION p202503 VALUES LESS THAN (TO_DAYS('2025-04-01')),
    PARTITION pfuture VALUES LESS THAN MAXVALUE
) COMMENT='1分钟K线数据表';

-- 2.7 5分钟K线数据表（按月分区）
CREATE TABLE IF NOT EXISTS kline_minute_5 (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    code VARCHAR(10) NOT NULL COMMENT '股票代码',
    datetime DATETIME NOT NULL COMMENT '日期时间',
    open DECIMAL(10, 3) NOT NULL COMMENT '开盘价',
    high DECIMAL(10, 3) NOT NULL COMMENT '最高价',
    low DECIMAL(10, 3) NOT NULL COMMENT '最低价',
    close DECIMAL(10, 3) NOT NULL COMMENT '收盘价',
    volume BIGINT NOT NULL DEFAULT 0 COMMENT '成交量(手)',
    amount DECIMAL(20, 2) NOT NULL DEFAULT 0 COMMENT '成交额(元)',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code_datetime (code, datetime),
    INDEX idx_datetime (datetime),
    INDEX idx_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 
PARTITION BY RANGE (TO_DAYS(datetime)) (
    PARTITION p202501 VALUES LESS THAN (TO_DAYS('2025-02-01')),
    PARTITION p202502 VALUES LESS THAN (TO_DAYS('2025-03-01')),
    PARTITION p202503 VALUES LESS THAN (TO_DAYS('2025-04-01')),
    PARTITION pfuture VALUES LESS THAN MAXVALUE
) COMMENT='5分钟K线数据表';

-- 2.8 15分钟K线数据表（按月分区）
CREATE TABLE IF NOT EXISTS kline_minute_15 (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    code VARCHAR(10) NOT NULL COMMENT '股票代码',
    datetime DATETIME NOT NULL COMMENT '日期时间',
    open DECIMAL(10, 3) NOT NULL COMMENT '开盘价',
    high DECIMAL(10, 3) NOT NULL COMMENT '最高价',
    low DECIMAL(10, 3) NOT NULL COMMENT '最低价',
    close DECIMAL(10, 3) NOT NULL COMMENT '收盘价',
    volume BIGINT NOT NULL DEFAULT 0 COMMENT '成交量(手)',
    amount DECIMAL(20, 2) NOT NULL DEFAULT 0 COMMENT '成交额(元)',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code_datetime (code, datetime),
    INDEX idx_datetime (datetime),
    INDEX idx_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 
PARTITION BY RANGE (TO_DAYS(datetime)) (
    PARTITION p202501 VALUES LESS THAN (TO_DAYS('2025-02-01')),
    PARTITION p202502 VALUES LESS THAN (TO_DAYS('2025-03-01')),
    PARTITION p202503 VALUES LESS THAN (TO_DAYS('2025-04-01')),
    PARTITION pfuture VALUES LESS THAN MAXVALUE
) COMMENT='15分钟K线数据表';

-- 2.9 30分钟K线数据表（按月分区）
CREATE TABLE IF NOT EXISTS kline_minute_30 (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    code VARCHAR(10) NOT NULL COMMENT '股票代码',
    datetime DATETIME NOT NULL COMMENT '日期时间',
    open DECIMAL(10, 3) NOT NULL COMMENT '开盘价',
    high DECIMAL(10, 3) NOT NULL COMMENT '最高价',
    low DECIMAL(10, 3) NOT NULL COMMENT '最低价',
    close DECIMAL(10, 3) NOT NULL COMMENT '收盘价',
    volume BIGINT NOT NULL DEFAULT 0 COMMENT '成交量(手)',
    amount DECIMAL(20, 2) NOT NULL DEFAULT 0 COMMENT '成交额(元)',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code_datetime (code, datetime),
    INDEX idx_datetime (datetime),
    INDEX idx_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 
PARTITION BY RANGE (TO_DAYS(datetime)) (
    PARTITION p202501 VALUES LESS THAN (TO_DAYS('2025-02-01')),
    PARTITION p202502 VALUES LESS THAN (TO_DAYS('2025-03-01')),
    PARTITION p202503 VALUES LESS THAN (TO_DAYS('2025-04-01')),
    PARTITION pfuture VALUES LESS THAN MAXVALUE
) COMMENT='30分钟K线数据表';

-- 2.10 60分钟K线数据表（按月分区）
CREATE TABLE IF NOT EXISTS kline_minute_60 (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    code VARCHAR(10) NOT NULL COMMENT '股票代码',
    datetime DATETIME NOT NULL COMMENT '日期时间',
    open DECIMAL(10, 3) NOT NULL COMMENT '开盘价',
    high DECIMAL(10, 3) NOT NULL COMMENT '最高价',
    low DECIMAL(10, 3) NOT NULL COMMENT '最低价',
    close DECIMAL(10, 3) NOT NULL COMMENT '收盘价',
    volume BIGINT NOT NULL DEFAULT 0 COMMENT '成交量(手)',
    amount DECIMAL(20, 2) NOT NULL DEFAULT 0 COMMENT '成交额(元)',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code_datetime (code, datetime),
    INDEX idx_datetime (datetime),
    INDEX idx_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 
PARTITION BY RANGE (TO_DAYS(datetime)) (
    PARTITION p202501 VALUES LESS THAN (TO_DAYS('2025-02-01')),
    PARTITION p202502 VALUES LESS THAN (TO_DAYS('2025-03-01')),
    PARTITION p202503 VALUES LESS THAN (TO_DAYS('2025-04-01')),
    PARTITION pfuture VALUES LESS THAN MAXVALUE
) COMMENT='60分钟K线数据表';

-- 2.11 分钟线数据表（按月分区） - 保留用于兼容

-- ========================================
-- 3. 技术指标表
-- ========================================

CREATE TABLE IF NOT EXISTS technical_indicators (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    code VARCHAR(10) NOT NULL COMMENT '股票代码',
    trade_date DATE NOT NULL COMMENT '交易日期',
    period_type VARCHAR(10) NOT NULL COMMENT '周期类型：daily/weekly/monthly/quarterly/yearly',
    
    -- 均线指标
    ma5 DECIMAL(10, 3) COMMENT '5日均线',
    ma10 DECIMAL(10, 3) COMMENT '10日均线',
    ma20 DECIMAL(10, 3) COMMENT '20日均线',
    ma60 DECIMAL(10, 3) COMMENT '60日均线',
    
    -- MACD
    macd DECIMAL(10, 3) COMMENT 'MACD',
    macd_signal DECIMAL(10, 3) COMMENT 'MACD信号线',
    macd_hist DECIMAL(10, 3) COMMENT 'MACD柱',
    
    -- KDJ
    k_value DECIMAL(10, 3) COMMENT 'K值',
    d_value DECIMAL(10, 3) COMMENT 'D值',
    j_value DECIMAL(10, 3) COMMENT 'J值',
    
    -- RSI
    rsi6 DECIMAL(10, 3) COMMENT 'RSI6',
    rsi12 DECIMAL(10, 3) COMMENT 'RSI12',
    rsi24 DECIMAL(10, 3) COMMENT 'RSI24',
    
    -- 布林带
    boll_upper DECIMAL(10, 3) COMMENT '布林上轨',
    boll_middle DECIMAL(10, 3) COMMENT '布林中轨',
    boll_lower DECIMAL(10, 3) COMMENT '布林下轨',
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code_date_period (code, trade_date, period_type),
    INDEX idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='技术指标表';

-- ========================================
-- 4. 资金流向表
-- ========================================

CREATE TABLE IF NOT EXISTS money_flow (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    code VARCHAR(10) NOT NULL COMMENT '股票代码',
    trade_date DATE NOT NULL COMMENT '交易日期',
    
    -- 主力资金
    main_net_inflow DECIMAL(20, 2) COMMENT '主力净流入(元)',
    main_net_inflow_pct DECIMAL(10, 2) COMMENT '主力净流入占比(%)',
    
    -- 超大单
    super_large_net_inflow DECIMAL(20, 2) COMMENT '超大单净流入',
    super_large_net_inflow_pct DECIMAL(10, 2) COMMENT '超大单净流入占比(%)',
    
    -- 大单
    large_net_inflow DECIMAL(20, 2) COMMENT '大单净流入',
    large_net_inflow_pct DECIMAL(10, 2) COMMENT '大单净流入占比(%)',
    
    -- 中单
    medium_net_inflow DECIMAL(20, 2) COMMENT '中单净流入',
    medium_net_inflow_pct DECIMAL(10, 2) COMMENT '中单净流入占比(%)',
    
    -- 小单
    small_net_inflow DECIMAL(20, 2) COMMENT '小单净流入',
    small_net_inflow_pct DECIMAL(10, 2) COMMENT '小单净流入占比(%)',
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code_date (code, trade_date),
    INDEX idx_date (trade_date),
    INDEX idx_main_inflow (main_net_inflow)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='资金流向表';

-- ========================================
-- 5. 板块数据表
-- ========================================

CREATE TABLE IF NOT EXISTS sector_kline_daily (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    code VARCHAR(10) NOT NULL COMMENT '板块代码',
    trade_date DATE NOT NULL COMMENT '交易日期',
    open DECIMAL(10, 3) COMMENT '开盘点位',
    high DECIMAL(10, 3) COMMENT '最高点位',
    low DECIMAL(10, 3) COMMENT '最低点位',
    close DECIMAL(10, 3) COMMENT '收盘点位',
    change_pct DECIMAL(10, 2) COMMENT '涨跌幅(%)',
    
    -- 板块统计
    stock_count INT COMMENT '成分股数量',
    rise_count INT COMMENT '上涨股票数',
    fall_count INT COMMENT '下跌股票数',
    flat_count INT COMMENT '平盘股票数',
    limit_up_count INT COMMENT '涨停股票数',
    limit_down_count INT COMMENT '跌停股票数',
    
    -- 成交统计
    total_amount DECIMAL(20, 2) COMMENT '总成交额',
    total_volume BIGINT COMMENT '总成交量',
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code_date (code, trade_date),
    INDEX idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='板块日线数据表';

-- ========================================
-- 6. 系统配置表
-- ========================================

-- 6.1 数据更新记录表  ✅️
CREATE TABLE IF NOT EXISTS data_update_log (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    data_type VARCHAR(50) NOT NULL COMMENT '数据类型',
    code VARCHAR(10) COMMENT '股票代码(可选)',
    update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
    record_count INT COMMENT '更新记录数',
    status VARCHAR(20) COMMENT '状态：success/failed',
    error_message TEXT COMMENT '错误信息',
    INDEX idx_data_type (data_type),
    INDEX idx_update_time (update_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='数据更新记录表';

-- 6.2 自选股配置表
CREATE TABLE IF NOT EXISTS user_stocks (
    id INT PRIMARY KEY AUTO_INCREMENT,
    user_id INT NOT NULL COMMENT '用户ID',
    code VARCHAR(10) NOT NULL COMMENT '股票代码',
    stock_name VARCHAR(50) COMMENT '股票名称',
    group_name VARCHAR(50) DEFAULT 'default' COMMENT '分组名称',
    is_holding BOOLEAN DEFAULT FALSE COMMENT '是否持仓',
    position_shares INT COMMENT '持仓股数',
    position_cost DECIMAL(10, 3) COMMENT '持仓成本',
    notes TEXT COMMENT '备注',
    sort_order INT DEFAULT 0 COMMENT '排序',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_user_code (user_id, code),
    INDEX idx_group (user_id, group_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='自选股配置表';

-- ========================================
-- 7. 市场情绪数据表
-- ========================================

-- 7.1 情绪周期表
CREATE TABLE IF NOT EXISTS emotion_cycle (
    id INT PRIMARY KEY AUTO_INCREMENT,
    date DATE NULL DEFAULT NULL COMMENT '交易日期',
    close_up_rate FLOAT NULL DEFAULT NULL COMMENT '收盘上涨率（%）',
    intraday_up_rate FLOAT NULL DEFAULT NULL COMMENT '盘中上涨率（%）',
    sh_up_rate FLOAT NULL DEFAULT NULL COMMENT '上证上涨率（%）',
    sz_up_rate FLOAT NULL DEFAULT NULL COMMENT '深证上涨率（%）',
    gem_up_rate FLOAT NULL DEFAULT NULL COMMENT '创业板上涨率（%）',
    strong_up_rate FLOAT NULL DEFAULT NULL COMMENT '强势上涨率（%）',
    limit_up_follow_rate FLOAT NULL DEFAULT NULL COMMENT '涨停溢价率（%）',
    weak_up_rate FLOAT NULL DEFAULT NULL COMMENT '弱势上涨率（%）',
    total_stocks INT NULL DEFAULT NULL COMMENT '统计股票数',
    is_confirmed INT NULL DEFAULT 0 COMMENT '数据是否已确认：0未确认，1已确认',
    created_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE INDEX ix_emotion_cycle_date (date ASC),
    INDEX ix_emotion_cycle_id (id ASC)
) ENGINE=InnoDB AUTO_INCREMENT=1 CHARACTER SET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='情绪周期表';

-- 7.2 市场情绪数据表
CREATE TABLE IF NOT EXISTS sentiment_data (
    id INT PRIMARY KEY AUTO_INCREMENT,
    date DATE NULL DEFAULT NULL COMMENT '交易日期',
    up_count INT NULL DEFAULT NULL COMMENT '上涨家数',
    down_count INT NULL DEFAULT NULL COMMENT '下跌家数',
    unchanged_count INT NULL DEFAULT NULL COMMENT '平盘家数',
    avg_change_percent FLOAT NULL DEFAULT NULL COMMENT '平均涨跌幅（%）',
    total_turnover_amount FLOAT NULL DEFAULT NULL COMMENT '沪深两市总成交额（亿元）',
    daily_turnover_change FLOAT NULL DEFAULT NULL COMMENT '日成交额环比变化（%）',
    up_5_percent_count INT NULL DEFAULT NULL COMMENT '涨幅5%以上家数',
    down_5_percent_count INT NULL DEFAULT NULL COMMENT '跌幅5%以上家数',
    limit_up_count INT NULL DEFAULT NULL COMMENT '涨停家数',
    limit_down_count INT NULL DEFAULT NULL COMMENT '跌停家数',
    sentiment_score FLOAT NULL DEFAULT NULL COMMENT '综合情绪指数（0-100）',
    sentiment_level VARCHAR(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '情绪等级：极度悲观/悲观/中性/乐观/极度乐观',
    consecutive_2_limit INT NULL DEFAULT 0 COMMENT '2连板家数',
    consecutive_3_limit INT NULL DEFAULT 0 COMMENT '3连板家数',
    consecutive_4_limit INT NULL DEFAULT 0 COMMENT '4连板家数',
    consecutive_5_plus_limit INT NULL DEFAULT 0 COMMENT '5连板及以上家数',
    created_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE INDEX ix_sentiment_data_date (date ASC),
    INDEX ix_sentiment_date (date ASC),
    INDEX ix_sentiment_data_id (id ASC)
) ENGINE=InnoDB AUTO_INCREMENT=1 CHARACTER SET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='市场情绪数据表';

-- ========================================
-- 完成
-- ========================================
SELECT 'Tables created successfully!' AS message;
