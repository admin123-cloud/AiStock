-- 创建板块日K线数据表
-- 如果表已存在，此脚本会跳过

CREATE TABLE IF NOT EXISTS `sector_kline_daily` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `code` VARCHAR(10) NOT NULL COMMENT '板块代码',
  `trade_date` DATE NOT NULL COMMENT '交易日期',
  `open` DECIMAL(10, 3) COMMENT '开盘点位',
  `high` DECIMAL(10, 3) COMMENT '最高点位',
  `low` DECIMAL(10, 3) COMMENT '最低点位',
  `close` DECIMAL(10, 3) COMMENT '收盘点位',
  `change_pct` DECIMAL(10, 2) COMMENT '涨跌幅(%)',

  -- 板块统计
  `stock_count` INT COMMENT '成分股数量',
  `rise_count` INT COMMENT '上涨股票数',
  `fall_count` INT COMMENT '下跌股票数',
  `flat_count` INT COMMENT '平盘股票数',
  `limit_up_count` INT COMMENT '涨停股票数',
  `limit_down_count` INT COMMENT '跌停股票数',

  -- 成交统计
  `total_amount` DECIMAL(20, 2) COMMENT '总成交额',
  `total_volume` BIGINT COMMENT '总成交量',

  `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',

  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_code_date` (`code`, `trade_date`),
  KEY `idx_code` (`code`),
  KEY `idx_trade_date` (`trade_date`),
  KEY `idx_code_date` (`code`, `trade_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='板块日K线数据表';
