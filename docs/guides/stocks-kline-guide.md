# 股票列表页面最新K线数据显示测试指南

## 修改内容

### 1. 后端API修改
需要修改 `backend/src/api/stocks.py` 中的 `get_stocks_with_limit` 函数，添加以下功能：
- 获取每只股票的最新K线数据
- 将最新K线数据包含在返回结果中

关键修改点：
```python
# 在获取股票列表后，添加以下代码
from src.models.stock import KlineDaily

stocks_with_kline = []
for stock in stocks:
    # 获取最新一条K线数据
    latest_kline = session.query(KlineDaily).filter(
        KlineDaily.code == stock.code
    ).order_by(KlineDaily.trade_date.desc()).first()

    stock_data = {
        "code": stock.code,
        "name": stock.name,
        "market": stock.market,
        "type": stock.type,
        "industry": stock.industry,
        "region": stock.region,
        "list_date": stock.list_date.isoformat() if stock.list_date else None,
        "status": stock.status if stock.status else "unknown",
        # 添加最新K线数据
        "latest_kline": {
            "trade_date": latest_kline.trade_date.isoformat() if latest_kline else None,
            "close": float(latest_kline.close) if latest_kline else None,
            "change_pct": float(latest_kline.change_pct) if latest_kline else None,
            "amount": float(latest_kline.amount) if latest_kline else None
        } if latest_kline else None
    }
    stocks_with_kline.append(stock_data)

# 修改返回结果
return {
    "items": stocks_with_kline,  # 返回包含K线数据的列表
    "total": total,
    "page": page,
    "page_size": page_size,
    "total_pages": (total + page_size - 1) // page_size
}
```

### 2. 前端页面修改

#### 2.1 修改表头（58-67行）
```vue
<thead>
  <tr>
    <th>代码</th>
    <th>名称</th>
    <th>市场</th>
    <th>类型</th>
    <th>行业</th>
    <th>最新价</th>      <!-- 新增 -->
    <th>涨跌幅</th>      <!-- 新增 -->
    <th>成交额</th>      <!-- 新增 -->
    <th>操作</th>
  </tr>
</thead>
```

#### 2.2 修改表格行（71-82行）
```vue
<tr v-for="stock in stocks" :key="stock.code">
  <td>{{ stock.code }}</td>
  <td>{{ stock.name }}</td>
  <td>{{ getMarketLabel(stock.market) }}</td>
  <td>{{ getTypeLabel(stock.type) }}</td>
  <td>{{ stock.industry || '-' }}</td>

  <!-- 新增：最新K线数据 -->
  <td v-if="stock.latest_kline" class="price-col">
    {{ formatPrice(stock.latest_kline.close) }}
  </td>
  <td v-else>-</td>

  <td v-if="stock.latest_kline" class="change-col">
    <span :class="getChangeClass(stock.latest_kline.change_pct)">
      {{ formatChangePct(stock.latest_kline.change_pct) }}
    </span>
  </td>
  <td v-else>-</td>

  <td v-if="stock.latest_kline" class="amount-col">
    {{ formatAmount(stock.latest_kline.amount) }}
  </td>
  <td v-else>-</td>
  <!-- 新增结束 -->

  <td>
    <button @click="goToDetail(stock.code)" class="detail-btn">详情</button>
  </td>
</tr>
```

#### 2.3 在script setup中添加格式化函数（在formatDate函数之后）
```javascript
// 格式化价格
const formatPrice = (price) => {
  if (price === null || price === undefined) return '-'
  return parseFloat(price).toFixed(2)
}

// 格式化涨跌幅
const formatChangePct = (pct) => {
  if (pct === null || pct === undefined) return '-'
  const value = parseFloat(pct)
  return (value >= 0 ? '+' : '') + value.toFixed(2) + '%'
}

// 格式化成交额
const formatAmount = (amount) => {
  if (amount === null || amount === undefined) return '-'
  const value = parseFloat(amount)
  if (value >= 100000000) {
    return (value / 100000000).toFixed(2) + '亿'
  } else if (value >= 10000) {
    return (value / 10000).toFixed(2) + '万'
  }
  return value.toFixed(2)
}

// 获取涨跌样式类
const getChangeClass = (pct) => {
  if (pct === null || pct === undefined) return ''
  const value = parseFloat(pct)
  if (value > 0) return 'up'
  if (value < 0) return 'down'
  return 'flat'
}
```

#### 2.4 在style中添加样式（在.stock-table tbody tr:hover之后）
```css
.price-col,
.change-col,
.amount-col {
  font-family: 'Monaco', 'Consolas', monospace;
  font-weight: 600;
}

.up {
  color: #e53e3e;  /* 红色上涨 */
}

.down {
  color: #38a169;  /* 绿色下跌 */
}

.flat {
  color: #718096;  /* 灰色平盘 */
}
```

## 预期效果

修改完成后，股票列表页面将显示以下信息：

| 代码 | 名称 | 市场 | 类型 | 行业 | 最新价 | 涨跌幅 | 成交额 | 操作 |
|------|------|------|------|------|--------|--------|--------|------|
| 000001 | 平安银行 | 上海 | 股票 | 银行 | 10.56 | +2.35% | 12.5亿 | 详情 |
| 000002 | 万科A | 深圳 | 股票 | 房地产 | 8.32 | -1.54% | 8.9亿 | 详情 |

## 测试步骤

1. 修改后端API文件 `backend/src/api/stocks.py`
2. 修改前端页面文件 `frontend/src/views/market/Stocks.vue`
3. 重启后端服务
4. 刷新前端页面
5. 查看股票列表是否显示最新价、涨跌幅、成交额

## 常见问题

### Q: 显示 "-" 而不是数据？
A: 可能是数据库中没有该股票的K线数据，需要先运行"修复历史数据"功能。

### Q: 涨跌幅颜色不对？
A: 检查前端代码中 `.up`、`.down`、`.flat` 样式是否正确设置颜色。

### Q: 后端报错？
A: 检查是否正确导入 `KlineDaily` 模型：
```python
from src.models.stock import KlineDaily
```

## 相关API

- 股票列表API: `GET /api/stocks/with-limit`
- 修复K线数据: `POST /api/stocks/repair-light` 或 `/api/stocks/repair-all`
- K线数据表: `kline_daily`
