<template>
  <div class="index-detail">
    <div class="container">
      <!-- 返回按钮 -->
      <div class="back-bar">
        <button @click="goBack" class="back-btn">
          ← 返回
        </button>
      </div>

      <!-- 指数基本信息 -->
      <div class="header-card">
        <div v-if="loading" class="loading">加载中...</div>
        <div v-else-if="indexInfo" class="index-info">
          <div class="info-header">
            <div class="info-left">
              <h1 class="index-title">{{ indexInfo.name }} ({{ indexInfo.code }})</h1>
              <span class="index-badge">{{ getMarketLabel(indexInfo.market) }}</span>
              <span v-if="isNotIndex" class="index-badge stock-badge">股票</span>
            </div>
            <div class="info-right">
              <span class="change-pct" :class="getChangeClass(indexInfo.change_pct)">
                {{ indexInfo.change_pct ? indexInfo.change_pct.toFixed(2) + '%' : 'N/A' }}
              </span>
            </div>
          </div>
          <div class="info-stats">
            <div class="stat-item">
              <span class="stat-label">最新价</span>
              <span class="stat-value">{{ indexInfo.price ? indexInfo.price.toFixed(2) : 'N/A' }}</span>
            </div>
            <div class="stat-item">
              <span class="stat-label">涨跌幅</span>
              <span class="stat-value" :class="getChangeClass(indexInfo.change_pct)">
                {{ indexInfo.change_pct ? indexInfo.change_pct.toFixed(2) + '%' : 'N/A' }}
              </span>
            </div>
            <div class="stat-item">
              <span class="stat-label">涨跌额</span>
              <span class="stat-value" :class="getChangeClass(indexInfo.change)">
                {{ indexInfo.change ? indexInfo.change.toFixed(2) : 'N/A' }}
              </span>
            </div>
            <div class="stat-item">
              <span class="stat-label">成交额</span>
              <span class="stat-value">{{ formatAmount(indexInfo.amount) }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- K线图表 -->
      <div class="chart-card">
        <div class="card-header">
          <h2>K线图表 (最近90天)</h2>
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
          :interval="selectedPeriod"
          :title="`${indexInfo?.name || code} ${getPeriodLabel(selectedPeriod)}K线`"
          :height="820"
        />
        <div v-if="!chartLoading && klineData && klineData.data && klineData.data.length > 0" class="chart-info">
          <span>显示 {{ klineData.total ?? klineData.count ?? klineData.data.length }} 条数据</span>
          <button @click="refreshKlineData" class="refresh-small-btn">刷新</button>
        </div>
        <div v-else-if="!chartLoading" class="empty" style="padding: 40px; text-align: center; color: #718096;">
          暂无K线数据
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
              <td>{{ item.open }}</td>
              <td>{{ item.high }}</td>
              <td>{{ item.low }}</td>
              <td
                :class="{
                  'up': item.change_pct > 0,
                  'down': item.change_pct < 0
                }"
              >
                {{ item.close }}
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
                  {{ item.change_pct.toFixed(2) + '%' }}
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
import { ref, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import axios from 'axios'
import LocalKLineChart from '../../components/LocalKLineChart.vue'

const route = useRoute()
const router = useRouter()
const API_BASE = import.meta.env.VITE_API_BASE || '/api'

const code = ref(route.params.code)
const indexInfo = ref(null)
const klineData = ref(null)
const loading = ref(false)
const chartLoading = ref(false)
const selectedPeriod = ref('1d')
const isNotIndex = ref(false)

const periods = [
  { label: '15分钟', value: '15m' },
  { label: '30分钟', value: '30m' },
  { label: '60分钟', value: '60m' },
  { label: '日K', value: '1d' },
  { label: '周K', value: '1w' },
  { label: '月K', value: '1mon' }
]

const periodLabelMap = Object.fromEntries(periods.map((item) => [item.value, item.label]))

watch(() => route.params.code, (newCode) => {
  if (newCode && newCode !== code.value) {
    code.value = newCode
    loadIndexInfo()
    loadKlineData()
  }
})

const loadIndexInfo = async () => {
  if (!code.value) return

  loading.value = true
  try {
    const response = await axios.get(`${API_BASE}/market/indices`, {
      params: { code: code.value }
    })
    if (response.data.indices && response.data.indices.length > 0) {
      indexInfo.value = response.data.indices[0]
    } else {
      indexInfo.value = null
      // 如果不是指数，尝试获取股票信息
      await loadStockInfo()
    }
  } catch (error) {
    console.error('加载指数信息失败:', error)
    indexInfo.value = null
    // 如果获取指数信息失败，尝试获取股票信息
    await loadStockInfo()
  } finally {
    loading.value = false
  }
}

const loadStockInfo = async () => {
  try {
    const response = await axios.get(`${API_BASE}/stocks/${code.value}`)
    if (response.data && response.data.stock) {
      isNotIndex.value = true
      indexInfo.value = {
        code: response.data.stock.code,
        name: response.data.stock.name,
        market: response.data.stock.market,
        price: response.data.stock.price,
        change: response.data.stock.change,
        change_pct: response.data.stock.change_pct,
        amount: response.data.stock.amount
      }
    }
  } catch (error) {
    console.error('加载股票信息失败:', error)
  }
}

const loadKlineData = async () => {
  if (!code.value) return

  chartLoading.value = true
  try {
    const isMinutePeriod = ['15m', '30m', '60m'].includes(selectedPeriod.value)
    const limit = isMinutePeriod ? 240 : 90
    const response = await axios.get(`${API_BASE}/stocks/${code.value}/kline/${selectedPeriod.value}`, {
      params: { limit }
    })
    klineData.value = response.data
  } catch (error) {
    console.error('加载K线数据失败:', error)
    klineData.value = null
  } finally {
    chartLoading.value = false
  }
}

const selectPeriod = (period) => {
  selectedPeriod.value = period
  loadKlineData()
}

const refreshKlineData = () => {
  loadKlineData()
}

const getPeriodLabel = (period) => periodLabelMap[period] || period

const getMarketLabel = (market) => {
  const normalized = String(market || '').toLowerCase()
  const labels = { sh: '上海', sz: '深圳', bj: '北京' }
  return labels[normalized] || '指数'
}

const getChangeClass = (change) => {
  if (change > 0) return 'rise'
  if (change < 0) return 'fall'
  return 'flat'
}

const formatAmount = (amount) => {
  if (amount === null || amount === undefined) return 'N/A'
  if (typeof amount === 'string') return amount
  if (amount >= 100000000) return (amount / 100000000).toFixed(2) + '亿'
  if (amount >= 10000) return (amount / 10000).toFixed(2) + '万'
  return Number(amount).toFixed(2)
}

const formatNumber = (num) => {
  if (num === null || num === undefined) return '-'
  if (typeof num === 'string') return num
  if (num >= 100000000) return (num / 100000000).toFixed(2) + '亿'
  if (num >= 10000) return (num / 10000).toFixed(2) + '万'
  return Number(num).toFixed(2)
}

const formatTradeDate = (value) => {
  if (!value) return '-'
  const text = String(value).trim()
  if (text.includes('T')) return text.split('T')[0]
  if (text.includes(' ')) return text.split(' ')[0]
  return text.slice(0, 10)
}

const goBack = () => {
  router.back()
}

onMounted(() => {
  loadIndexInfo()
  loadKlineData()
})
</script>

<style scoped>
.index-detail {
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

.header-card,
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

/* 指数基本信息 */
.index-info .info-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 20px;
  flex-wrap: wrap;
}

.index-info .info-left {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.index-title {
  color: #2d3748;
  font-size: 28px;
  margin: 0;
}

.index-badge {
  padding: 4px 12px;
  border-radius: 4px;
  font-size: 13px;
  font-weight: 500;
  background: #e3f2fd;
  color: #1976d2;
}

.info-right {
  display: flex;
  align-items: center;
}

.change-pct {
  font-size: 20px;
  min-width: 80px;
  text-align: right;
}

.change-pct.rise {
  color: #f5222d;
}

.change-pct.fall {
  color: #52c41a;
}

.change-pct.flat {
  color: #666;
}

.info-stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
  margin-top: 20px;
  padding-top: 20px;
  border-top: 1px solid #f0f0f0;
}

.stat-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.stat-label {
  font-size: 14px;
  color: #666;
}

.stat-value {
  font-size: 16px;
  font-weight: bold;
  color: #333;
}

.stat-value.rise {
  color: #f5222d;
}

.stat-value.fall {
  color: #52c41a;
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

@media (max-width: 960px) {
  .index-detail {
    padding: 14px;
  }

  .header-card,
  .chart-card,
  .table-card {
    padding: 18px;
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
  color: #f5222d;
}

.kline-table .down {
  color: #52c41a;
}

.kline-table tbody tr:hover {
  background: #f7fafc;
}
</style>
