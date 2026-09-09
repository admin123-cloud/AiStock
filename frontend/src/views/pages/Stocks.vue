<template>
  <div class="stocks">
    <div class="container">
      <div class="header">
        <div class="header-title">
          <h1>股票列表</h1>
          <p>查看所有A股股票的基本信息和行情数据</p>
        </div>
        <div class="header-actions">
        <button @click="refreshData" class="refresh-btn">刷新</button>
      </div>
      </div>

      <!-- 筛选区域 -->
      <div class="filter-bar">
        <div class="filter-item">
          <label>市场:</label>
          <select v-model="filters.market" @change="loadStocks">
            <option value="">全部</option>
            <option value="sh">上海</option>
            <option value="sz">深圳</option>
            <option value="bj">北京</option>
          </select>
        </div>
        <div class="filter-item">
          <label>类型:</label>
          <select v-model="filters.type" @change="loadStocks">
            <option value="">全部</option>
            <option value="stock">股票</option>
            <option value="index">指数</option>
            <option value="industry">行业</option>
            <option value="sector">板块</option>
          </select>
        </div>
        <div class="filter-item">
          <label>搜索:</label>
          <input 
            v-model="filters.search" 
            placeholder="代码或名称" 
            @input="handleSearch"
          />
        </div>
        <div class="filter-item">
          <button @click="refreshData" class="refresh-btn">刷新</button>
        </div>
      </div>

      <!-- 数据表格 -->
      <div class="table-container">
        <div v-if="loading" class="loading">加载中...</div>
        <div v-else-if="stocks.length === 0" class="empty">暂无数据</div>
        <table v-else class="stock-table">
          <thead>
            <tr>
              <th>代码</th>
              <th>名称</th>
              <th>市场</th>
              <th>类型</th>
              <th>最新价</th>
              <th>涨跌幅</th>
              <th>成交额</th>
              <th>操作</th>
            </tr>
          </thead>
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
        </table>
      </div>

      <!-- 分页 -->
      <div class="pagination" v-if="total > 0">
        <button 
          @click="changePage(page - 1)" 
          :disabled="page === 1"
          class="page-btn"
        >
          上一页
        </button>
        <span class="page-info">
          第 {{ page }} 页 / 共 {{ totalPages }} 页 ({{ total }} 条)
        </span>
        <button 
          @click="changePage(page + 1)" 
          :disabled="page === totalPages"
          class="page-btn"
        >
          下一页
        </button>
        <select v-model="pageSize" @change="loadStocks" class="page-size">
          <option :value="20">20条/页</option>
          <option :value="50">50条/页</option>
          <option :value="100">100条/页</option>
        </select>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import axios from 'axios'

const router = useRouter()

const API_BASE = import.meta.env.VITE_API_BASE || '/api'

// 响应式数据
const stocks = ref([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)

const filters = ref({
  market: '',
  type: '',
  search: ''
})

// 计算总页数
const totalPages = computed(() => {
  return Math.ceil(total.value / pageSize.value)
})

// 加载股票列表
const loadStocks = async () => {
  loading.value = true
  try {
    const params = {
      page: page.value,
      page_size: pageSize.value,
      // 默认只查询股票类型，不包含指数
      stock_type: 'stock'
    }

    // 只添加非空的筛选参数
    if (filters.value.market) {
      params.market = filters.value.market
    }
    if (filters.value.type) {
      params.stock_type = filters.value.type
    }
    if (filters.value.search && filters.value.search.trim()) {
      params.search = filters.value.search.trim()
    }

    console.log('请求参数:', params)

    const response = await axios.get(`${API_BASE}/stocks/with-limit`, { params })
    
    console.log('API响应:', response.data)
    
    if (response.data && response.data.items) {
      stocks.value = response.data.items
      total.value = response.data.total || 0
    } else {
      stocks.value = []
      total.value = 0
    }
  } catch (error) {
    console.error('加载股票列表失败:', error)
    // 尝试另一个API
    try {
      const params2 = {
        skip: (page.value - 1) * pageSize.value,
        limit: pageSize.value,
        // 默认只查询股票类型，不包含指数
        stock_type: 'stock'
      }
      
      if (filters.value.market) {
        params2.market = filters.value.market
      }
      if (filters.value.type) {
        params2.stock_type = filters.value.type
      }
      if (filters.value.search && filters.value.search.trim()) {
        params2.search = filters.value.search.trim()
      }

      const response2 = await axios.get(`${API_BASE}/stocks`, { params: params2 })
      
      console.log('备用API响应:', response2.data)
      
      if (Array.isArray(response2.data)) {
        stocks.value = response2.data
        total.value = response2.data.length
      } else {
        stocks.value = []
        total.value = 0
      }
    } catch (error2) {
      console.error('备用API也失败:', error2)
      stocks.value = []
      total.value = 0
    }
  } finally {
    loading.value = false
  }
}

// 搜索处理（防抖）
let searchTimeout = null
const handleSearch = () => {
  clearTimeout(searchTimeout)
  searchTimeout = setTimeout(() => {
    page.value = 1
    loadStocks()
  }, 500)
}

// 翻页
const changePage = (newPage) => {
  if (newPage >= 1 && newPage <= totalPages.value) {
    page.value = newPage
    loadStocks()
  }
}

// 跳转详情
const goToDetail = (code) => {
  router.push(`/stock/${code}`)
}

// 刷新数据
const refreshData = () => {
  page.value = 1
  loadStocks()
}

// 获取市场标签
const getMarketLabel = (market) => {
  const labels = {
    'sh': '上海',
    'sz': '深圳',
    'bj': '北京'
  }
  return labels[market] || market
}

// 获取类型标签
const getTypeLabel = (type) => {
  const labels = {
    'stock': '股票',
    'index': '指数',
    'industry': '行业',
    'sector': '板块'
  }
  return labels[type] || type
}

// 格式化日期
const formatDate = (date) => {
  if (!date) return '-'
  if (typeof date === 'string') {
    return date.substring(0, 10)
  }
  return date.toISOString().substring(0, 10)
}

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
  if (!Number.isFinite(value)) return '-'
  // 股票列表接口返回的成交额单位为元，直接按元换算展示。
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

// 组件挂载时加载数据
let intradayRefreshTimer = null

onMounted(() => {
  loadStocks()
  // The list is backed by five-minute bars during the trading session.  Keep
  // an open page fresh without requiring the user to manually refresh it.
  intradayRefreshTimer = window.setInterval(loadStocks, 60 * 1000)
})

onUnmounted(() => {
  if (intradayRefreshTimer) {
    window.clearInterval(intradayRefreshTimer)
  }
})
</script>

<style scoped>
.stocks {
  min-height: calc(100vh - 60px);
  background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
  padding: 20px;
}

.container {
  max-width: 1400px;
  margin: 0 auto;
}

.header {
  background: white;
  border-radius: 12px;
  padding: 24px;
  margin-bottom: 20px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.08);
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.header-title h1 {
  color: #2d3748;
  font-size: 24px;
  margin-bottom: 8px;
}

.header-title p {
  color: #718096;
  font-size: 14px;
}

.header-actions {
  display: flex;
  gap: 12px;
  align-items: center;
}

.repair-light-btn {
  padding: 8px 20px;
  background: linear-gradient(135deg, #2196f3 0%, #1976d2 100%);
  color: white;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  transition: all 0.3s;
}

.repair-light-btn:hover:not(:disabled) {
  transform: translateY(-2px);
}

.repair-light-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.repair-btn {
  padding: 8px 20px;
  background: linear-gradient(135deg, #f56565 0%, #ed8936 100%);
  color: white;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  transition: all 0.3s;
}

.repair-btn:hover:not(:disabled) {
  transform: translateY(-2px);
}

.repair-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.repair-minute-btn {
  padding: 8px 20px;
  background: linear-gradient(135deg, #8b5cf6 0%, #6366f1 100%);
  color: white;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  transition: all 0.3s;
}

.repair-minute-btn:hover:not(:disabled) {
  transform: translateY(-2px);
}

.repair-minute-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.sync-btn {
  padding: 8px 20px;
  background: linear-gradient(135deg, #38a169 0%, #48bb78 100%);
  color: white;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  transition: all 0.3s;
}

.sync-btn:hover:not(:disabled) {
  transform: translateY(-2px);
}

.sync-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.filter-bar {
  background: white;
  border-radius: 12px;
  padding: 20px;
  margin-bottom: 20px;
  display: flex;
  gap: 16px;
  align-items: center;
  flex-wrap: wrap;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.08);
}

.filter-item {
  display: flex;
  align-items: center;
  gap: 8px;
}

.filter-item label {
  font-size: 14px;
  color: #4a5568;
  font-weight: 500;
}

.filter-item select,
.filter-item input {
  padding: 8px 12px;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  font-size: 14px;
  outline: none;
  transition: border-color 0.3s;
}

.filter-item select:focus,
.filter-item input:focus {
  border-color: #667eea;
}

.filter-item input {
  width: 150px;
}

.refresh-btn {
  padding: 8px 20px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  transition: transform 0.3s;
}

.refresh-btn:hover {
  transform: translateY(-2px);
}

.table-container {
  background: white;
  border-radius: 12px;
  padding: 20px;
  margin-bottom: 20px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.08);
  overflow-x: auto;
}

.loading,
.empty {
  text-align: center;
  padding: 60px 20px;
  color: #718096;
  font-size: 16px;
}

.stock-table {
  width: 100%;
  border-collapse: collapse;
}

.stock-table thead {
  background: #f7fafc;
}

.stock-table th {
  padding: 12px 8px;
  text-align: left;
  font-weight: 600;
  color: #2d3748;
  font-size: 14px;
  white-space: nowrap;
}

.stock-table td {
  padding: 12px 8px;
  border-bottom: 1px solid #e2e8f0;
  color: #4a5568;
  font-size: 14px;
  white-space: nowrap;
}

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

.stock-table tbody tr:hover {
  background: #f7fafc;
}

.detail-btn {
  padding: 6px 16px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 500;
  transition: transform 0.3s;
}

.detail-btn:hover {
  transform: translateY(-1px);
}

.pagination {
  background: white;
  border-radius: 12px;
  padding: 16px 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 16px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.08);
}

.page-btn {
  padding: 8px 16px;
  background: white;
  color: #4a5568;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
  transition: all 0.3s;
}

.page-btn:hover:not(:disabled) {
  background: #667eea;
  color: white;
  border-color: #667eea;
}

.page-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.page-info {
  color: #4a5568;
  font-size: 14px;
}

.page-size {
  padding: 8px 12px;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  font-size: 14px;
  outline: none;
  cursor: pointer;
}

.page-size:focus {
  border-color: #667eea;
}
</style>
