# 股票列表最新K线数据显示功能完成

## ✅ 已完成的修改

### 1. 后端API修改

**文件**: `backend/src/api/stocks.py`

**修改内容**: 在 `get_stocks_with_limit` 函数中添加了最新K线数据查询逻辑

**关键代码**:
```python
# 获取每只股票的最新K线数据
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
            "trade_date": latest_kline.trade_date.isoformat() if latest_kline.trade_date else None,
            "close": float(latest_kline.close) if latest_kline.close is not None else None,
            "change_pct": float(latest_kline.change_pct) if latest_kline.change_pct is not None else None,
            "amount": float(latest_kline.amount) if latest_kline.amount is not None else None
        } if latest_kline else None
    }
    stocks_with_kline.append(stock_data)
```

### 2. 前端页面修改

**文件**: `frontend/src/views/pages/Stocks.vue`

#### 2.1 修改表头（第58-69行）

```vue
<thead>
  <tr>
    <th>代码</th>
    <th>名称</th>
    <th>市场</th>
    <th>类型</th>
    <th>最新价</th>      <!-- 新增 -->
    <th>涨跌幅</th>      <!-- 新增 -->
    <th>成交额</th>      <!-- 新增 -->
    <th>操作</th>
  </tr>
</thead>
```

#### 2.2 修改表格行（第70-83行）

```vue
<tbody>
  <tr v-for="stock in stocks" :key="stock.code">
    <td>{{ stock.code }}</td>
    <td>{{ stock.name }}</td>
    <td>{{ getMarketLabel(stock.market) }}</td>
    <td>{{ getTypeLabel(stock.type) }}</td>

    <!-- 最新K线数据 -->
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
    <!-- 最新K线数据结束 -->

    <td>
      <button @click="goToDetail(stock.code)" class="detail-btn">详情</button>
    </td>
  </tr>
</tbody>
```

#### 2.3 添加格式化函数（第421-466行）

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

#### 2.4 添加样式（第667-678行）

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

## 📊 页面效果

修改完成后，股票列表页面将显示：

| 代码 | 名称 | 市场 | 类型 | 最新价 | 涨跌幅 | 成交额 | 操作 |
|------|------|------|------|--------|--------|--------|------|
| 000001 | 平安银行 | 上海 | 股票 | 10.56 | +2.35% | 12.5亿 | 详情 |
| 000002 | 万科A | 深圳 | 股票 | 8.32 | -1.54% | 8.9亿 | 详情 |
| 600000 | 浦发银行 | 上海 | 股票 | 9.87 | 0.00% | 5.6亿 | 详情 |

### 特点

- ✅ **最新价**: 显示最新K线的收盘价，保留2位小数
- ✅ **涨跌幅**: 显示涨跌幅百分比，红色上涨，绿色下跌，灰色平盘
- ✅ **成交额**: 显示成交额，亿/万单位自动转换
- ✅ **无数据处理**: 如果没有K线数据，显示"-"

## 🚀 使用说明

### 启动服务

1. 重启后端服务：
```bash
cd backend
python main.py
```

2. 刷新前端页面（自动热更新）

### 如果显示"-"

如果表格中显示"-"而不是实际数据，可能的原因：

1. **缺少K线数据**: 运行"修复历史数据"功能
2. **API未加载**: 刷新浏览器页面
3. **数据库连接问题**: 检查后端日志

### 检查后端日志

打开浏览器开发者工具Console，查看API响应：

```javascript
{
  "items": [
    {
      "code": "000001",
      "name": "平安银行",
      "market": "sz",
      "type": "stock",
      "latest_kline": {
        "trade_date": "2026-03-24",
        "close": 10.56,
        "change_pct": 2.35,
        "amount": 1250000000.0
      }
    }
  ],
  "total": 5000,
  "page": 1,
  "page_size": 20
}
```

## 📝 API说明

### 获取股票列表

**接口**: `GET /api/stocks/with-limit`

**参数**:
- `page`: 页码（从1开始）
- `page_size`: 每页数量（20/50/100）
- `market`: 市场筛选（sh/sz/bj）
- `stock_type`: 类型筛选（stock/index/industry/sector）
- `search`: 搜索关键词

**返回数据结构**:
```json
{
  "items": [
    {
      "code": "000001",
      "name": "平安银行",
      "market": "sz",
      "type": "stock",
      "industry": "银行",
      "region": "广东",
      "list_date": "1991-04-03",
      "status": "active",
      "latest_kline": {
        "trade_date": "2026-03-24",
        "close": 10.56,
        "change_pct": 2.35,
        "amount": 1250000000.0
      }
    }
  ],
  "total": 5000,
  "page": 1,
  "page_size": 20,
  "total_pages": 250
}
```

## ✅ 测试检查清单

- [x] 后端API修改完成
- [x] 前端表头修改完成
- [x] 前端表格行修改完成
- [x] 格式化函数添加完成
- [x] 样式添加完成
- [x] 无Linter错误
- [ ] 后端服务重启
- [ ] 前端页面刷新
- [ ] 数据显示验证

## 📅 修改时间

2026-03-24

## 🔗 相关文件

- `backend/src/api/stocks.py` - 后端股票API
- `frontend/src/views/pages/Stocks.vue` - 前端股票列表页面
- `backend/src/models/stock.py` - 数据模型定义
