# 板块K线数据 - 快速开始

## 📋 概述

板块K线数据表 `sector_kline_daily` 已包含完整的板块统计指标：

| 字段 | 说明 |
|-----|------|
| rise_count | 上涨股票数 |
| fall_count | 下跌股票数 |
| flat_count | 平盘股票数 |
| limit_up_count | 涨停股票数 |
| limit_down_count | 跌停股票数 |
| total_amount | 总成交额 |
| total_volume | 总成交量 |

板块涨跌幅基于成分股历史K线数据，采用**简单平均方式**计算。

## 🚀 快速开始（Windows）

### 方式1：使用批处理脚本

```bash
cd backend
quickstart_sector_kline.bat
```

### 方式2：手动执行

```bash
# 1. 进入后端目录
cd backend

# 2. 生成最近30个交易日的板块K线数据
python scripts/generate_sector_kline.py --mode recent --days 30
```

## 🎯 日常使用

### 每日更新

```bash
# Windows PowerShell
cd backend
$today = Get-Date -Format "yyyy-MM-dd"
python scripts/generate_sector_kline.py --mode date --date $today
```

### 生成历史数据

```bash
# 生成指定日期范围
python scripts/generate_sector_kline.py --mode range --start 2024-01-01 --end 2024-12-31
```

## 📊 API使用

### 获取板块K线历史

```http
GET /api/sectors/BK0308/kline?days=30
```

### 获取板块列表（含涨跌幅）

```http
GET /api/sectors/?sector_type=industry&sort_by=change_pct
```

## 📖 详细文档

完整文档请查看：`backend/scripts/SECTOR_KLINE_GUIDE.md`

## 🔍 测试

运行测试脚本验证功能：

```bash
cd backend
python test_sector_kline.py
```

## ⚠️ 注意事项

1. 确保股票K线数据（`kline_daily`表）已更新
2. 确保板块成分股数据（`sector_stocks`表）已同步
3. 脚本会自动跳过周末（周一到周五）
4. 重复生成相同日期的数据会自动更新现有记录
