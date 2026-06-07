# 数据库初始化指南

## 快速开始

### 1. 创建MySQL数据库

```bash
# 登录MySQL
mysql -u root -p

# 执行建表脚本
source scripts/create_tables.sql
```

或者使用Python脚本：

```bash
# 安装依赖
pip install -r requirements.txt

# 初始化数据库
python scripts/init_database.py
```

### 2. 验证数据库

```sql
-- 查看数据库
SHOW DATABASES;

-- 使用数据库
USE stockpy;

-- 查看所有表
SHOW TABLES;

-- 查看表结构
DESC stocks;
DESC kline_daily;
DESC kline_minute;
DESC emotion_cycle;
DESC sentiment_data;
```

## 数据库架构说明

### 核心表说明

| 表名 | 用途 | 数据量估算 | 分区策略 |
|------|------|-----------|---------|
| stocks | 股票基础信息 | ~5000条 | 无需分区 |
| sectors | 板块信息 | ~1000条 | 无需分区 |
| kline_daily | 日线数据 | ~1500万条 | 按年分区 |
| kline_weekly | 周线数据 | ~300万条 | 无需分区 |
| kline_monthly | 月线数据 | ~70万条 | 无需分区 |
| kline_quarterly | 季度线 | ~25万条 | 无需分区 |
| kline_minute | 分钟线 | ~600万条/年 | 按月分区 |
| emotion_cycle | 情绪周期 | ~2500条(10年) | 无需分区 |
| sentiment_data | 市场情绪 | ~2500条(10年) | 无需分区 |

### 索引优化

所有表都已添加必要的索引：
- 主键索引
- 唯一约束索引（股票代码+日期）
- 查询条件索引（日期、市场、类型等）

### 分区管理

#### 添加新年份分区（日线）

```sql
ALTER TABLE kline_daily ADD PARTITION (
    PARTITION p2026 VALUES LESS THAN (TO_DAYS('2027-01-01'))
);
```

#### 添加新月份分区（分钟线）

```sql
ALTER TABLE kline_minute ADD PARTITION (
    PARTITION p202504 VALUES LESS THAN (TO_DAYS('2025-05-01'))
);
```

#### 删除旧分区（可选）

```sql
-- 删除2020年数据
ALTER TABLE kline_daily DROP PARTITION p2020;
```

## 性能优化建议

### 1. 查询优化

```sql
-- 使用分区裁剪（只查询相关分区）
SELECT * FROM kline_daily 
WHERE code = '000001' 
  AND trade_date >= '2024-01-01'
  AND trade_date < '2025-01-01';

-- 使用覆盖索引
SELECT code, trade_date, close, volume 
FROM kline_daily
WHERE code = '000001'
ORDER BY trade_date DESC
LIMIT 100;

-- 避免SELECT *
SELECT code, trade_date, close, volume
FROM kline_daily
WHERE trade_date = '2024-12-01';
```

### 2. 批量插入优化

```python
# 使用SQLAlchemy批量插入
from src.utils.database import get_db
from src.models import KlineDaily

# 准备数据
klines = [
    KlineDaily(
        code='000001',
        trade_date='2024-12-01',
        open=10.5,
        high=10.8,
        low=10.3,
        close=10.6,
        volume=1000000,
        amount=10500000
    ),
    # ... 更多数据
]

# 批量插入
with next(get_db()) as session:
    session.bulk_save_objects(klines)
    session.commit()
```

### 3. 数据归档策略

```sql
-- 创建归档表（保留表结构）
CREATE TABLE kline_minute_archive LIKE kline_minute;
ALTER TABLE kline_minute_archive REMOVE PARTITIONING;

-- 归档旧数据
INSERT INTO kline_minute_archive 
SELECT * FROM kline_minute 
WHERE datetime < '2024-01-01';

-- 删除已归档的分区
ALTER TABLE kline_minute DROP PARTITION p202301, p202302;
```

## 监控与维护

### 1. 监控表大小

```sql
-- 查看表大小
SELECT 
    table_name,
    ROUND(data_length / 1024 / 1024, 2) AS 'Data Size (MB)',
    ROUND(index_length / 1024 / 1024, 2) AS 'Index Size (MB)',
    table_rows
FROM information_schema.tables
WHERE table_schema = 'stockpy'
ORDER BY data_length DESC;
```

### 2. 监控分区使用情况

```sql
-- 查看分区信息
SELECT 
    partition_name,
    partition_description,
    table_rows
FROM information_schema.partitions
WHERE table_name = 'kline_daily'
ORDER BY partition_ordinal_position;
```

### 3. 优化表

```sql
-- 优化表（重建表，回收空间）
OPTIMIZE TABLE kline_daily;
OPTIMIZE TABLE kline_minute;
```

## 备份策略

### 1. 逻辑备份

```bash
# 备份所有数据
mysqldump -u root -p stockpy > backup_$(date +%Y%m%d).sql

# 只备份表结构
mysqldump -u root -p --no-data stockpy > schema.sql

# 备份特定表
mysqldump -u root -p stockpy stocks kline_daily > kline_backup.sql
```

### 2. 物理备份

```bash
# 使用xtrabackup进行热备份
xtrabackup --backup --target-dir=/backup/mysql/$(date +%Y%m%d)
```

## 故障排查

### 1. 连接失败

```bash
# 检查MySQL服务状态
systemctl status mysql

# 检查端口
netstat -tunlp | grep 3306

# 测试连接
mysql -u root -p -h 127.0.0.1 -P 3306
```

### 2. 性能问题

```sql
-- 查看慢查询
SHOW VARIABLES LIKE 'slow_query_log';
SET GLOBAL slow_query_log = 'ON';
SET GLOBAL long_query_time = 2;

-- 分析慢查询日志
mysqldumpslow -s t /var/log/mysql/mysql-slow.log
```

### 3. 锁等待

```sql
-- 查看当前锁
SHOW ENGINE INNODB STATUS\G

-- 查看进程列表
SHOW PROCESSLIST;

-- 杀死阻塞的进程
KILL <process_id>;
```

## 下一步

1. 导入股票基础信息
2. 采集历史K线数据
3. 设置定时任务更新数据
4. 添加自选股监控

详细文档请查看：`docs/database_design.md`
