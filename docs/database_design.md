# 股票数据监控系统 - 数据库设计方案

## 一、数据量分析

### 1. 监控对象统计
- **股票**：约5,235只（上证+深圳+北交所）
- **板块**：约654个（行业板块）
- **题材**：约500个（概念题材）
- **指数**：约100个
- **总计**：约6,500个监控对象

### 2. 数据量估算（按时间维度）

| 数据类型 | 对象数 | 时间跨度 | 记录数估算 |
|---------|--------|---------|-----------|
| 日线 | 6,500 | 10年(2,500天) | 1,625万 |
| 周线 | 6,500 | 10年(520周) | 338万 |
| 月线 | 6,500 | 10年(120月) | 78万 |
| 季度线 | 6,500 | 10年(40季) | 26万 |
| 分钟线 | 100(自选) | 1年(6万分钟) | 600万/年 |

**历史总量**：约2,700万条记录  
**年增长量**：约1,700万条（日线+分钟线为主）

### 3. 存储空间估算
- 每条K线记录约200字节
- 历史数据：约5.4GB
- 年增长：约3.4GB
- 加上索引：总计约15-20GB/年

---

## 二、数据库选型建议

### 方案A：MySQL 8.0+（推荐用于快速起步）
**优点**：
- 成熟稳定，生态完善
- 支持分区表，可处理千万级数据
- 运维简单，人才储备丰富
- 支持JSON类型存储灵活数据

**缺点**：
- 时序数据查询性能不如专业时序库
- 大表分区管理较复杂

**适用场景**：
- 数据量 < 5000万
- 团队熟悉MySQL
- 快速开发和部署

### 方案B：PostgreSQL + TimescaleDB（推荐用于生产环境）
**优点**：
- TimescaleDB是专为时序数据设计的PostgreSQL扩展
- 自动分区管理，写入性能极佳
- 支持时序特有函数（时间聚合、降采样）
- 压缩功能，节省70%存储空间
- 完全兼容PostgreSQL生态

**缺点**：
- 学习成本稍高
- 部署相对复杂

**适用场景**：
- 数据量 > 5000万
- 需要高性能时序查询
- 长期生产环境

---

## 三、表结构设计（以MySQL为例）

### 1. 基础信息表

#### 1.1 股票基础信息表 (stocks)
```sql
CREATE TABLE stocks (
    id INT PRIMARY KEY AUTO_INCREMENT,
    code VARCHAR(10) NOT NULL UNIQUE COMMENT '股票代码',
    name VARCHAR(50) NOT NULL COMMENT '股票名称',
    market VARCHAR(20) NOT NULL COMMENT '市场：sh/sz/bj',
    type VARCHAR(20) NOT NULL COMMENT '类型：stock/index/sector/theme',
    industry VARCHAR(50) COMMENT '所属行业',
    sector VARCHAR(100) COMMENT '所属板块',
    list_date DATE COMMENT '上市日期',
    status VARCHAR(20) DEFAULT 'active' COMMENT '状态：active/delisted/suspended',
    is_self_selected BOOLEAN DEFAULT FALSE COMMENT '是否自选',
    is_holding BOOLEAN DEFAULT FALSE COMMENT '是否持仓',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_market (market),
    INDEX idx_type (type),
    INDEX idx_industry (industry),
    INDEX idx_self_selected (is_self_selected),
    INDEX idx_holding (is_holding)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='股票基础信息表';
```

#### 1.2 板块信息表 (sectors)
```sql
CREATE TABLE sectors (
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
```

#### 1.3 板块成分股关联表 (sector_stocks)
```sql
CREATE TABLE sector_stocks (
    id INT PRIMARY KEY AUTO_INCREMENT,
    sector_code VARCHAR(10) NOT NULL COMMENT '板块代码',
    stock_code VARCHAR(10) NOT NULL COMMENT '股票代码',
    weight DECIMAL(10, 4) COMMENT '权重',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_sector_stock (sector_code, stock_code),
    INDEX idx_stock (stock_code),
    INDEX idx_sector (sector_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='板块成分股关联表';
```

---

### 2. K线数据表（核心表）

#### 2.1 日线数据表 (kline_daily)
```sql
CREATE TABLE kline_daily (
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
```

#### 2.2 周线数据表 (kline_weekly)
```sql
CREATE TABLE kline_weekly (
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
```

#### 2.3 月线数据表 (kline_monthly)
```sql
CREATE TABLE kline_monthly (
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
```

#### 2.4 季度线数据表 (kline_quarterly)
```sql
CREATE TABLE kline_quarterly (
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
```

#### 2.5 分钟线数据表 (kline_minute) - 核心高频表
```sql
CREATE TABLE kline_minute (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    code VARCHAR(10) NOT NULL COMMENT '股票代码',
    datetime DATETIME NOT NULL COMMENT '日期时间',
    period INT NOT NULL DEFAULT 1 COMMENT '周期(分钟)：1/5/15/30/60',
    open DECIMAL(10, 3) NOT NULL COMMENT '开盘价',
    high DECIMAL(10, 3) NOT NULL COMMENT '最高价',
    low DECIMAL(10, 3) NOT NULL COMMENT '最低价',
    close DECIMAL(10, 3) NOT NULL COMMENT '收盘价',
    volume BIGINT NOT NULL DEFAULT 0 COMMENT '成交量(手)',
    amount DECIMAL(20, 2) NOT NULL DEFAULT 0 COMMENT '成交额(元)',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code_datetime_period (code, datetime, period),
    INDEX idx_datetime (datetime),
    INDEX idx_period (period)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 
PARTITION BY RANGE (TO_DAYS(datetime)) (
    PARTITION p202501 VALUES LESS THAN (TO_DAYS('2025-02-01')),
    PARTITION p202502 VALUES LESS THAN (TO_DAYS('2025-03-01')),
    PARTITION p202503 VALUES LESS THAN (TO_DAYS('2025-04-01')),
    PARTITION pfuture VALUES LESS THAN MAXVALUE
) COMMENT='分钟线数据表';
```

---

### 3. 技术指标表

#### 3.1 技术指标表 (technical_indicators)
```sql
CREATE TABLE technical_indicators (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    code VARCHAR(10) NOT NULL COMMENT '股票代码',
    trade_date DATE NOT NULL COMMENT '交易日期',
    period_type VARCHAR(10) NOT NULL COMMENT '周期类型：daily/weekly/monthly',
    
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
```

---

### 4. 资金流向表

#### 4.1 资金流向表 (money_flow)
```sql
CREATE TABLE money_flow (
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
```

---

### 5. 板块数据表

#### 5.1 板块日线数据表 (sector_kline_daily)
```sql
CREATE TABLE sector_kline_daily (
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
```

---

### 6. 系统配置表

#### 6.1 数据更新记录表 (data_update_log)
```sql
CREATE TABLE data_update_log (
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
```

#### 6.2 自选股配置表 (user_stocks)
```sql
CREATE TABLE user_stocks (
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
```

---

### 7. 市场情绪数据表

#### 7.1 情绪周期表 (emotion_cycle)
```sql
CREATE TABLE emotion_cycle (
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
```

**用途**：记录市场情绪周期的各项指标，用于判断市场整体情绪状态。

**核心指标**：
- `close_up_rate`：收盘上涨率，反映市场收盘时的整体表现
- `intraday_up_rate`：盘中上涨率，反映日内交易的活跃度
- `strong_up_rate` / `weak_up_rate`：强势/弱势上涨率，判断市场强弱
- `limit_up_follow_rate`：涨停溢价率，反映涨停板效应

#### 7.2 市场情绪数据表 (sentiment_data)
```sql
CREATE TABLE sentiment_data (
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
    sentiment_level VARCHAR(10) NULL DEFAULT NULL COMMENT '情绪等级：极度悲观/悲观/中性/乐观/极度乐观',
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
```

**用途**：记录每日市场情绪数据，包括涨跌统计、成交额、连板股数量等，用于市场情绪分析和预警。

**核心指标**：
- `up_count` / `down_count`：涨跌家数，最直观的市场情绪指标
- `limit_up_count` / `limit_down_count`：涨跌停家数，反映市场极端情绪
- `sentiment_score`：综合情绪指数（0-100），量化市场情绪
- `sentiment_level`：情绪等级分类
- `consecutive_X_limit`：连板股数量，反映市场接力情绪

---

## 四、TimescaleDB 方案（推荐用于生产）

如果选择 PostgreSQL + TimescaleDB，核心K线表改为：

```sql
-- 创建超表(Hypertable)
CREATE TABLE kline_daily (
    code VARCHAR(10) NOT NULL,
    trade_date TIMESTAMPTZ NOT NULL,
    open NUMERIC(10, 3) NOT NULL,
    high NUMERIC(10, 3) NOT NULL,
    low NUMERIC(10, 3) NOT NULL,
    close NUMERIC(10, 3) NOT NULL,
    volume BIGINT NOT NULL,
    amount NUMERIC(20, 2) NOT NULL,
    PRIMARY KEY (code, trade_date)
);

-- 转换为超表，按时间分区
SELECT create_hypertable('kline_daily', 'trade_date', 
    partition_time_interval => INTERVAL '1 month');

-- 创建压缩策略
ALTER TABLE kline_daily SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'code'
);

-- 自动压缩90天前的数据
SELECT add_compression_policy('kline_daily', INTERVAL '90 days');

-- 创建连续聚合视图（预计算周线）
CREATE MATERIALIZED VIEW kline_weekly
WITH (timescaledb.continuous) AS
SELECT 
    code,
    time_bucket('7 days', trade_date) AS week_start,
    FIRST(open, trade_date) AS open,
    MAX(high) AS high,
    MIN(low) AS low,
    LAST(close, trade_date) AS close,
    SUM(volume) AS volume,
    SUM(amount) AS amount
FROM kline_daily
GROUP BY code, time_bucket('7 days', trade_date);

-- 自动刷新聚合视图
SELECT add_continuous_aggregate_policy('kline_weekly',
    start_offset => INTERVAL '1 week',
    end_offset => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day');
```

---

## 五、性能优化建议

### 1. 索引优化
- 为所有查询条件创建复合索引
- 使用覆盖索引减少回表
- 定期分析索引使用情况，删除无用索引

### 2. 分区策略
- 日线表：按年分区
- 分钟线表：按月分区
- 定期维护分区（添加新分区、删除旧分区）

### 3. 查询优化
- 使用分区裁剪（只查询相关分区）
- 避免SELECT *，只查询需要的字段
- 使用LIMIT限制返回结果
- 合理使用缓存（Redis）

### 4. 写入优化
- 批量插入代替单条插入
- 使用LOAD DATA INFILE导入大批量数据
- 临时关闭索引和约束，导入后重建

### 5. 归档策略
- 分钟线数据保留1-2年
- 日线数据长期保留
- 定期归档历史数据到冷存储

---

## 六、推荐方案总结

### 当前阶段推荐：MySQL 8.0+

**理由**：
1. 你已经配置了MySQL，快速起步
2. 数据量在可控范围内（<5000万）
3. 团队学习成本低
4. 通过分区表可以满足性能需求

### 未来升级路线：PostgreSQL + TimescaleDB

**何时升级**：
- 数据量超过5000万
- 分钟线数据需要保留更长时间
- 需要复杂时序分析
- 查询性能出现瓶颈

**升级方案**：
- 使用数据迁移工具将MySQL数据导入TimescaleDB
- 保持应用层API不变
- 获得更好的性能和压缩比

---

## 七、实施建议

### 第一阶段（立即实施）
1. 创建MySQL数据库和基础表结构
2. 导入股票基础信息
3. 开始采集日线、周线、月线数据

### 第二阶段（1个月内）
1. 实现分钟线数据采集（针对自选股）
2. 添加技术指标计算
3. 实现数据更新自动化

### 第三阶段（3个月内）
1. 评估系统性能
2. 如有需要，迁移至TimescaleDB
3. 添加更多数据分析功能
