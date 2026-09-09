<template>
  <div class="stock-detail">
    <!-- 侧边导航按钮 -->
    <div class="side-nav">
      <button @click="navigateToPrev" class="side-nav-btn prev" :disabled="loadingNav">
        ↑
      </button>
      <button @click="navigateToNext" class="side-nav-btn next" :disabled="loadingNav">
        ↓
      </button>
    </div>
    
    <div class="container">
      <!-- 返回按钮 -->
      <div class="back-bar">
        <button @click="goBack" class="back-btn">
          ← 返回
        </button>
      </div>

      <!-- 股票基本信息 -->
      <div class="header-card">
        <div v-if="loading" class="loading">加载中...</div>
        <div v-else-if="stockInfo" class="stock-info">
          <div class="info-header">
            <div class="info-left">
              <h1 class="stock-title">{{ stockInfo.name }} ({{ stockInfo.code }})</h1>
              <span class="stock-badge" :class="getMarketClass(stockInfo.market)">
                {{ getMarketLabel(stockInfo.market) }}
              </span>
              <span class="stock-badge type-badge">{{ getTypeLabel(stockInfo.type) }}</span>
            </div>
            <div class="info-right">
              <button
                @click="syncData"
                class="sync-btn"
                :disabled="syncing"
              >
                {{ syncing ? '同步中...' : '同步数据' }}
              </button>
            </div>
          </div>
          <div class="info-grid">
            <div class="info-item">
              <span class="info-label">行业:</span> {{ stockInfo.industry || '-' }}
            </div>
            <div class="info-item">
              <span class="info-label">地区:</span> {{ stockInfo.region || '-' }}
            </div>
            <div class="info-item">
              <span class="info-label">上市日期:</span> {{ formatDate(stockInfo.list_date) }}
            </div>
            <div class="info-item">
              <span class="info-label">状态:</span> <span class="status" :class="getStatusClass(stockInfo.status)">{{ getStatusLabel(stockInfo.status) }}</span>
            </div>
          </div>

          <!-- 所属板块 -->
          <div class="boards-section" v-if="stockBoards && stockBoards.length > 0">
            <div class="boards-header">
              <span class="boards-title">所属板块:</span>
              <button @click="loadStockBoards" class="refresh-boards-btn" :disabled="boardsLoading">
                {{ boardsLoading ? '加载中...' : '刷新' }}
              </button>
            </div>
            <div class="boards-tags">
              <span 
                v-for="board in stockBoards" 
                :key="board.code" 
                class="board-tag"
                :class="board.type"
              >
                {{ board.name }}
              </span>
            </div>
          </div>
        </div>
        <div v-else class="empty">股票信息不存在</div>
      </div>

      <!-- K线数据统计 -->
      <div class="stats-card">
        <div class="card-header">
          <h2>K线数据统计</h2>
          <button @click="refreshKlineStats" class="refresh-btn">刷新</button>
        </div>
        <div v-if="klineStatsLoading" class="loading">加载中...</div>
        <div v-else-if="syncMessage" class="sync-message" :class="{ success: syncMessage.includes('完成'), error: syncMessage.includes('失败') }">
          {{ syncMessage }}
        </div>
        <div v-else-if="klineStats" class="stats-grid">
          <div class="stat-summary">
            <div class="stat-item large">
              <div class="label">总数据条数</div>
              <div class="value">{{ klineStats.total_count || 0 }}</div>
            </div>
            <div class="stat-item large">
              <div class="label">有数据的周期</div>
              <div class="value">{{ klineStats.periods_with_data || 0 }} / {{ Object.keys(klineStats.periods || {}).length }}</div>
            </div>
          </div>
          <div class="period-stats">
            <h3>各周期数据量</h3>
            <div class="period-grid">
              <div 
                v-for="(stat, period) in klineStats.periods" 
                :key="period"
                class="period-item"
                :class="stat.count > 0 ? 'has-data' : 'no-data'"
              >
                <div class="period-label">{{ period }}</div>
                <div class="period-count">{{ stat.count }}</div>
                <div class="period-table">{{ stat.table }}</div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- K线图表 -->
      <div class="chart-card">
        <div class="card-header">
          <h2>K线图表</h2>
          <div class="period-selector">
            <button
              v-for="period in periods"
              :key="period.value"
              @click="selectPeriod(period.value)"
              :class="['period-btn', { active: selectedPeriod === period.value }]"
            >
              {{ period.label }}
            </button>
          </div>
        </div>
        <div v-if="chartLoading" class="loading">加载中...</div>
        <LocalKLineChart
          v-else
          :data="klineData?.data || []"
          :trade-markers="tradeMarkers"
          :interval="selectedPeriod"
          :title="`${stockInfo?.name || code} ${getPeriodLabel(selectedPeriod)}K线`"
          :height="820"
          @range-edge="handleChartRangeEdge"
        />
        <div v-if="!chartLoading && klineData && klineData.data && klineData.data.length > 0" class="chart-info">
          <span v-if="hasMoreHistory">显示 {{ klineData.data.length }} 条数据，按 ← 加载更多历史，按 → 回到最新</span>
          <span v-else>当前周期共 {{ klineData.data.length }} 条数据，已经到最早历史</span>
          <button @click="refreshKlineData" class="refresh-small-btn">刷新</button>
        </div>
        <div v-else-if="!chartLoading" class="empty" style="padding: 40px; text-align: center; color: #718096;">
          暂无K线数据，点击"同步数据"按钮获取数据
        </div>
      </div>

      <!-- 历史数据表格 -->
      <div class="table-card">
        <div class="card-header">
          <h2>历史数据 (最近20条)</h2>
        </div>
        <div v-if="chartLoading" class="loading">加载中...</div>
        <div v-else-if="!klineData || !klineData.data || klineData.data.length === 0" class="empty">暂无数据</div>
        <table v-else class="kline-table">
          <thead>
            <tr>
              <th>日期</th>
              <th>开盘</th>
              <th>最高</th>
              <th>最低</th>
              <th>收盘</th>
              <th>成交量</th>
              <th>成交额</th>
              <th>涨跌幅</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(item, index) in klineData.data.slice(-20).reverse()" :key="index">
              <td>{{ formatTradeDate(item.date || item.datetime) }}</td>
              <td>{{ formatPrice(item.open) }}</td>
              <td>{{ formatPrice(item.high) }}</td>
              <td>{{ formatPrice(item.low) }}</td>
              <td
                :class="{
                  'up': item.change_pct > 0,
                  'down': item.change_pct < 0
                }"
              >
                {{ formatPrice(item.close) }}
              </td>
              <td>{{ formatNumber(item.volume) }}</td>
              <td>{{ formatNumber(item.amount) }}</td>
              <td
                :class="{
                  'up': (item.change_pct && item.change_pct > 0) || (item.open && item.close && (item.close - item.open) > 0),
                  'down': (item.change_pct && item.change_pct < 0) || (item.open && item.close && (item.close - item.open) < 0)
                }"
              >
                <template v-if="item.change_pct !== undefined && item.change_pct !== null && item.change_pct !== 0">
                  {{ formatPercent(item.change_pct) }}
                </template>
                <template v-else-if="item.open && item.close">
                  {{ ((item.close - item.open) / item.open * 100).toFixed(2) + '%' }}
                </template>
                <template v-else>
                  -
                </template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onBeforeUnmount, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import axios from 'axios'
import LocalKLineChart from '@/components/LocalKLineChart.vue'

const route = useRoute()
const router = useRouter()
const API_BASE = import.meta.env.VITE_API_BASE || '/api'

const code = ref(route.params.code)

// 监听路由参数变化，更新股票代码
watch(() => route.params.code, (newCode) => {
  if (newCode && newCode !== code.value) {
    code.value = newCode
    klineData.value = null
    hasMoreHistory.value = true
    loadTradeMarkersFromRoute()
    console.log('route code changed:', newCode)
    loadStockInfo()
    loadKlineStats()
    loadKlineData()
    loadStockBoards()
  }
})

watch(() => route.query.trade_marks_key, () => {
  loadTradeMarkersFromRoute()
})

const previousRoute = ref(null)

// 响应式数据
const loading = ref(false)
const stockInfo = ref(null)
const loadingNav = ref(false)

const klineStatsLoading = ref(false)
const klineStats = ref(null)

const chartLoading = ref(false)
const klineData = ref(null)
const selectedPeriod = ref('1d')
const klineLimit = ref(100)
const loadingMoreKline = ref(false)
const hasMoreHistory = ref(true)

const syncing = ref(false)
const syncMessage = ref('')
const tradeMarkers = ref([])
const TRADE_MARKS_STORAGE_PREFIX = 'stock_detail_trade_marks_'

// 周期选项
// K线周期选项（使用统一的时间单位标准）
// 标准格式：1m, 15m, 30m, 60m, 1d, 1w, 1mon, 1q, 1y
const periods = [
  { label: '1分钟', value: '1m' },
  { label: '15分钟', value: '15m' },
  { label: '30分钟', value: '30m' },
  { label: '60分钟', value: '60m' },
  { label: '日K', value: '1d' },
  { label: '周K', value: '1w' },
  { label: '月K', value: '1mon' },
  { label: '季K', value: '1q' }
]

const periodLabelMap = Object.fromEntries(periods.map((item) => [item.value, item.label]))

const normalizeTradeMarkerCode = (value) => {
  const text = String(value || '').trim().toUpperCase()
  if (!text) return ''
  return text.includes('.') ? text.split('.')[0] : text
}

const loadTradeMarkersFromRoute = () => {
  const key = String(route.query?.trade_marks_key || '').trim()
  if (!key || !key.startsWith(TRADE_MARKS_STORAGE_PREFIX)) {
    tradeMarkers.value = []
    return
  }
  try {
    const raw = sessionStorage.getItem(key)
    const parsed = JSON.parse(raw || '[]')
    const currentCode = normalizeTradeMarkerCode(code.value)
    tradeMarkers.value = (Array.isArray(parsed) ? parsed : []).filter((item) => {
      const markerCode = normalizeTradeMarkerCode(item?.code)
      return markerCode && markerCode === currentCode
    })
  } catch (error) {
    console.error('load trade markers failed:', error)
    tradeMarkers.value = []
  }
}

// 加载股票基本信息
const loadStockInfo = async () => {
  const targetCode = code.value
  loading.value = true
  try {
    const response = await axios.get(`${API_BASE}/stocks/${targetCode}`)
    if (targetCode !== code.value) return
    stockInfo.value = response.data
    console.log('股票信息:', response.data)
    console.log('股票状态:', response.data.status)
  } catch (error) {
    if (targetCode !== code.value) return
    console.error('加载股票信息失败:', error)
    stockInfo.value = null
  } finally {
    if (targetCode === code.value) loading.value = false
  }
}

// 加载K线统计
const loadKlineStats = async () => {
  klineStatsLoading.value = true
  try {
    const response = await axios.get(`${API_BASE}/stocks/${code.value}/kline-stats`)
    klineStats.value = response.data
    console.log('K线统计:', response.data)
  } catch (error) {
    console.error('加载K线统计失败:', error)
    klineStats.value = null
  } finally {
    klineStatsLoading.value = false
  }
}

// 加载K线数据
const loadKlineData = async (limit = klineLimit.value) => {
  chartLoading.value = true
  try {
    const previousCount = Array.isArray(klineData.value?.data) ? klineData.value.data.length : 0
    const requestedLimit = limit
    klineLimit.value = limit
    const response = await axios.get(
      `${API_BASE}/stocks/${code.value}/kline/${selectedPeriod.value}`,
      { params: { limit } }
    )
    klineData.value = response.data
    const nextCount = Array.isArray(response.data?.data) ? response.data.data.length : 0
    hasMoreHistory.value = nextCount >= requestedLimit || (requestedLimit > previousCount && nextCount > previousCount)
    console.log('K线数据:', response.data)
  } catch (error) {
    console.error('加载K线数据失败:', error)
    klineData.value = null
    hasMoreHistory.value = false
  } finally {
    chartLoading.value = false
  }
}

const selectPeriod = (period) => {
  selectedPeriod.value = period
  klineLimit.value = ['1m', '15m', '30m', '60m'].includes(period) ? 240 : 100
  hasMoreHistory.value = true
  loadKlineData()
}

// 刷新数据
const refreshKlineStats = () => {
  loadKlineStats()
}

const refreshKlineData = () => {
  loadKlineData()
}

const loadMoreKlineData = async () => {
  if (loadingMoreKline.value || chartLoading.value || !hasMoreHistory.value) return
  loadingMoreKline.value = true
  try {
    const step = ['1m', '15m', '30m', '60m'].includes(selectedPeriod.value) ? 240 : 100
    await loadKlineData(klineLimit.value + step)
  } finally {
    loadingMoreKline.value = false
  }
}

const resetKlineToLatest = () => {
  klineLimit.value = ['1m', '15m', '30m', '60m'].includes(selectedPeriod.value) ? 240 : 100
  hasMoreHistory.value = true
  loadKlineData()
}

const handleChartRangeEdge = ({ side }) => {
  if (side === 'left') {
    loadMoreKlineData()
  }
}

const handleKlineKeydown = (event) => {
  const tagName = String(event.target?.tagName || '').toLowerCase()
  if (['input', 'textarea', 'select'].includes(tagName)) return
  if (event.key === 'ArrowLeft') {
    loadMoreKlineData()
  } else if (event.key === 'ArrowRight') {
    resetKlineToLatest()
  }
}

const getPeriodLabel = (period) => periodLabelMap[period] || period

// 工具函数
const getMarketLabel = (market) => {
  const labels = { 'sh': '上海', 'sz': '深圳', 'bj': '北京' }
  return labels[market] || market
}

const getMarketClass = (market) => {
  return `market-${market}`
}

const getTypeLabel = (type) => {
  const labels = { 'stock': '股票', 'index': '指数', 'industry': '行业', 'sector': '板块' }
  return labels[type] || type
}

const getStatusLabel = (status) => {
  const labels = { 'active': '正常', 'delisted': '退市', 'unknown': '未知' }
  return labels[status] || status || '未知'
}

const getStatusClass = (status) => {
  return `status-${status || 'unknown'}`
}

const formatDate = (date) => {
  if (!date) return '-'
  if (typeof date === 'string') return date.substring(0, 10)
  return date.toISOString().substring(0, 10)
}

const toFiniteNumber = (value) => {
  const num = Number(value)
  return Number.isFinite(num) ? num : null
}

const formatPrice = (value) => {
  const num = toFiniteNumber(value)
  return num === null ? '-' : num.toFixed(2)
}

const formatPercent = (value) => {
  const num = toFiniteNumber(value)
  return num === null ? '-' : `${num.toFixed(2)}%`
}

const formatNumber = (num) => {
  const value = toFiniteNumber(num)
  if (value === null) return '-'
  if (Math.abs(value) >= 100000000) {
    return (value / 100000000).toFixed(2) + '亿'
  } else if (Math.abs(value) >= 10000) {
    return (value / 10000).toFixed(2) + '万'
  }
  return value.toFixed(2)
}

// 返回上一页
const formatTradeDate = (value) => {
  if (!value) return '-'
  const text = String(value).trim()
  if (text.includes('T')) return text.split('T')[0]
  if (text.includes(' ')) return text.split(' ')[0]
  return text.slice(0, 10)
}

const goBack = () => {
  // 使用浏览器历史记录返回
  router.back()
}

// 同步数据
const syncData = async () => {
  syncing.value = true
  syncMessage.value = '正在同步数据...'
  
  try {
    const response = await axios.post(`${API_BASE}/stocks/${code.value}/sync`)
    
    if (response.data.success) {
      syncMessage.value = `同步完成！${response.data.message || ''}`
      // 刷新数据
      await loadKlineStats()
      await loadKlineData()
      
      // 3秒后清除消息
      setTimeout(() => {
        syncMessage.value = ''
      }, 3000)
    } else {
      syncMessage.value = `同步失败：${response.data.message || '未知错误'}`
    }
  } catch (error) {
    console.error('同步数据失败:', error)
    syncMessage.value = `同步失败：${error.response?.data?.detail || error.message || '网络错误'}`
    
    // 5秒后清除错误消息
    setTimeout(() => {
      syncMessage.value = ''
    }, 5000)
  } finally {
    syncing.value = false
  }
}

const navigateToAdjacent = async (direction) => {
  if (loadingNav.value) return
  loadingNav.value = true
  try {
    const response = await axios.get(`${API_BASE}/stocks/${code.value}/navigation`)
    const target = response.data?.[direction]
    if (target?.code) await router.push(`/stock/${target.code}`)
  } catch (error) {
    console.error('导航失败:', error)
  } finally {
    loadingNav.value = false
  }
}

const navigateToPrev = () => navigateToAdjacent('previous')
const navigateToNext = () => navigateToAdjacent('next')

// 股票所属板块
const stockBoards = ref([])
const boardsLoading = ref(false)

// 加载股票所属板块
const loadStockBoards = async () => {
  boardsLoading.value = true
  try {
    const response = await axios.get(`${API_BASE}/stocks/${code.value}/boards`)
    stockBoards.value = response.data.boards || []
  } catch (error) {
    console.error('获取板块数据失败:', error)
    stockBoards.value = []
  } finally {
    boardsLoading.value = false
  }
}

// 组件挂载
onMounted(() => {
  // 保存来源路由信息 - 使用当前路由状态
  const currentState = router.currentRoute.value

  // 如果有查询参数，保存它们以便返回时恢复
  if (currentState.query && Object.keys(currentState.query).length > 0) {
    previousRoute.value = {
      path: '/stocks',
      query: currentState.query
    }
    console.log('保存路由信息:', previousRoute.value)
  }

  // 监听窗口大小变化，调整图表尺寸

  loadTradeMarkersFromRoute()
  
  loadStockInfo()
  loadKlineStats()
  loadKlineData()
  loadStockBoards()
  window.addEventListener('keydown', handleKlineKeydown)
})

// 组件卸载
onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleKlineKeydown)
})
</script>

<style scoped>
.stock-detail {
  min-height: calc(100vh - 60px);
  background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
  padding: 20px;
}

.container {
  max-width: 1400px;
  margin: 0 auto;
}

/* 返回按钮 */
.back-bar {
  margin-bottom: 20px;
}

.back-btn {
  padding: 10px 24px;
  background: white;
  color: #4a5568;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  transition: all 0.3s;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}

.back-btn:hover {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  border-color: #667eea;
  transform: translateX(-4px);
  box-shadow: 0 4px 12px rgba(102, 126, 234, 0.3);
}

/* 侧边导航按钮 */
.side-nav {
  position: fixed;
  right: 30px;
  top: 50%;
  transform: translateY(-50%);
  z-index: 100;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.side-nav-btn {
  width: 56px;
  height: 56px;
  border-radius: 50%;
  border: none;
  background: linear-gradient(135deg, #ffffff 0%, #f8f9fa 100%);
  color: #667eea;
  font-size: 24px;
  font-weight: bold;
  cursor: pointer;
  transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 6px 20px rgba(102, 126, 234, 0.15);
  backdrop-filter: blur(10px);
  border: 1px solid rgba(102, 126, 234, 0.2);
  position: relative;
}

.side-nav-btn:hover:not(:disabled) {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  transform: scale(1.15) translateY(0);
  box-shadow: 0 8px 25px rgba(102, 126, 234, 0.4);
}

.side-nav-btn:active:not(:disabled) {
  transform: scale(1.05);
  box-shadow: 0 4px 15px rgba(102, 126, 234, 0.3);
}

.side-nav-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
  background: #f8f9fa;
  color: #ced4da;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
  border-color: #e9ecef;
}

.side-nav-btn.prev:hover:not(:disabled) {
  transform: scale(1.15) translateY(-4px);
}

.side-nav-btn.next:hover:not(:disabled) {
  transform: scale(1.15) translateY(4px);
}

/* 添加脉冲效果 */
.side-nav-btn::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  border-radius: 50%;
  border: 2px solid rgba(102, 126, 234, 0.3);
  opacity: 0;
  transition: opacity 0.3s ease;
}

.side-nav-btn:hover::before {
  opacity: 1;
  animation: pulse 2s infinite;
}

@keyframes pulse {
  0% {
    transform: scale(1);
    opacity: 1;
  }
  100% {
    transform: scale(1.3);
    opacity: 0;
  }
}

.header-card,
.stats-card,
.chart-card,
.table-card {
  background: white;
  border-radius: 12px;
  padding: 24px;
  margin-bottom: 20px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.08);
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 20px;
}

.card-header h2 {
  color: #2d3748;
  font-size: 20px;
  margin: 0;
}

.loading,
.empty {
  text-align: center;
  padding: 40px;
  color: #718096;
}

.sync-message {
  text-align: center;
  padding: 16px;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 20px;
}

.sync-message.success {
  background: #e8f5e9;
  color: #2e7d32;
  border: 1px solid #4caf50;
}

.sync-message.error {
  background: #ffebee;
  color: #c62828;
  border: 1px solid #f44336;
}

/* 股票基本信息 */
.stock-info .info-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 20px;
  flex-wrap: wrap;
}

.stock-info .info-left {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.stock-info .info-right {
  display: flex;
  align-items: center;
}

.stock-title {
  color: #2d3748;
  font-size: 28px;
  margin: 0;
}

.stock-badge {
  padding: 4px 12px;
  border-radius: 4px;
  font-size: 13px;
  font-weight: 500;
}

/* 同步按钮 */
.sync-btn {
  padding: 8px 20px;
  background: linear-gradient(135deg, #4caf50 0%, #45a049 100%);
  color: white;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  transition: all 0.3s;
  box-shadow: 0 2px 8px rgba(76, 175, 80, 0.2);
}

.sync-btn:hover:not(:disabled) {
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(76, 175, 80, 0.3);
}

.sync-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.market-sh {
  background: #e3f2fd;
  color: #1976d2;
}

.market-sz {
  background: #f3e5f5;
  color: #7b1fa2;
}

.market-bj {
  background: #fff3e0;
  color: #f57c00;
}

.type-badge {
  background: #e8f5e9;
  color: #388e3c;
}

.info-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
}

.info-item {
  display: flex;
  align-items: center;
  gap: 8px;
}

.info-item .info-label {
  font-size: 14px;
  color: #718096;
  font-weight: 500;
}

/* 板块展示 */
.boards-section {
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid #e2e8f0;
}

.boards-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.boards-title {
  font-size: 14px;
  color: #718096;
  font-weight: 500;
}

.refresh-boards-btn {
  padding: 4px 12px;
  font-size: 12px;
  background: #f7fafc;
  border: 1px solid #e2e8f0;
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.2s;
}

.refresh-boards-btn:hover:not(:disabled) {
  background: #edf2f7;
}

.refresh-boards-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.boards-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.board-tag {
  padding: 4px 12px;
  font-size: 13px;
  border-radius: 16px;
  cursor: pointer;
  transition: all 0.2s;
}

.board-tag:hover {
  transform: translateY(-1px);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
}

.board-tag.concept {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
}

.board-tag.industry {
  background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
  color: white;
}

.board-tag.other {
  background: #e2e8f0;
  color: #4a5568;
}

.info-item span {
  font-size: 15px;
  color: #2d3748;
}

.info-item .status {
  color: #388e3c;
  font-weight: 500;
  padding: 2px 8px;
  border-radius: 4px;
}

.info-item .status.status-active {
  background: #e8f5e9;
  color: #2e7d32;
}

.info-item .status.status-delisted {
  background: #ffebee;
  color: #c62828;
}

.info-item .status.status-unknown {
  background: #f5f5f5;
  color: #757575;
}

/* K线统计 */
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

.stat-summary {
  display: flex;
  gap: 24px;
  margin-bottom: 24px;
}

.stat-item {
  flex: 1;
  padding: 20px;
  background: #f7fafc;
  border-radius: 8px;
}

.stat-item.large {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
}

.stat-item .label {
  font-size: 14px;
  margin-bottom: 8px;
}

.stat-item.large .label {
  opacity: 0.9;
}

.stat-item .value {
  font-size: 32px;
  font-weight: bold;
}

.stat-item.large .value {
  font-size: 36px;
}

.period-stats h3 {
  color: #2d3748;
  font-size: 16px;
  margin-bottom: 16px;
}

.period-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(100px, 1fr));
  gap: 12px;
}

.period-item {
  padding: 16px;
  border-radius: 8px;
  text-align: center;
  transition: all 0.3s;
}

.period-item.has-data {
  background: #e8f5e9;
  border: 2px solid #4caf50;
}

.period-item.no-data {
  background: #ffebee;
  border: 2px solid #f44336;
  opacity: 0.6;
}

.period-label {
  font-size: 18px;
  font-weight: bold;
  color: #2d3748;
  margin-bottom: 8px;
}

.period-count {
  font-size: 24px;
  font-weight: bold;
  margin-bottom: 4px;
}

.period-item.has-data .period-count {
  color: #4caf50;
}

.period-item.no-data .period-count {
  color: #f44336;
}

.period-table {
  font-size: 12px;
  color: #718096;
}

/* K线图表 */
.period-selector {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.period-btn {
  padding: 8px 16px;
  background: white;
  color: #4a5568;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 500;
  transition: all 0.3s;
}

.period-btn:hover {
  background: #f7fafc;
}

.period-btn.active {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  border-color: #667eea;
}

.chart-container {
  margin-top: 20px;
}

.chart-info {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid #e2e8f0;
  font-size: 14px;
  color: #718096;
}

.refresh-small-btn {
  padding: 6px 12px;
  background: #f7fafc;
  color: #4a5568;
  border: 1px solid #e2e8f0;
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
  transition: all 0.3s;
}

.refresh-small-btn:hover {
  background: #667eea;
  color: white;
  border-color: #667eea;
}

@media (max-width: 1100px) {
  .side-nav {
    display: none;
  }
}

@media (max-width: 960px) {
  .stock-detail {
    padding: 14px;
  }

  .header-card,
  .stats-card,
  .chart-card,
  .table-card {
    padding: 18px;
  }

  .info-right {
    width: 100%;
    flex-wrap: wrap;
    gap: 10px;
  }

  .sync-btn {
    margin-right: 0;
  }

  .card-header {
    align-items: flex-start;
  }

  .period-selector {
    width: 100%;
    overflow-x: auto;
    padding-bottom: 4px;
    flex-wrap: nowrap;
    justify-content: flex-start;
  }

  .period-btn {
    flex: 0 0 auto;
    white-space: nowrap;
  }

  .chart-info {
    flex-direction: column;
    align-items: flex-start;
    gap: 10px;
  }

  .stat-summary {
    flex-direction: column;
    gap: 16px;
  }

  .kline-table {
    display: block;
    overflow-x: auto;
    white-space: nowrap;
  }
}

/* 数据表格 */
.kline-table {
  width: 100%;
  border-collapse: collapse;
}

.kline-table thead {
  background: #f7fafc;
}

.kline-table th {
  padding: 12px;
  text-align: left;
  font-weight: 600;
  color: #2d3748;
  font-size: 14px;
}

.kline-table td {
  padding: 12px;
  border-bottom: 1px solid #e2e8f0;
  color: #4a5568;
  font-size: 14px;
}

.kline-table .up {
  color: #f44336;
}

.kline-table .down {
  color: #4caf50;
}

.kline-table tbody tr:hover {
  background: #f7fafc;
}
</style>
