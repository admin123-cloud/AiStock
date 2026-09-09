<template>
  <div class="sector-detail">
    <div class="container">
      <!-- 返回按钮 -->
      <div class="back-bar">
        <button @click="goBack" class="back-btn">
          ← 返回
        </button>
      </div>

      <!-- 板块基本信息 -->
      <div class="header-card">
        <div v-if="loading" class="loading">加载中...</div>
        <div v-else-if="sectorInfo" class="sector-info">
          <div class="info-header">
            <div class="info-left">
              <h1 class="sector-title">{{ sectorInfo.name }} ({{ sectorInfo.code }})</h1>
              <span class="sector-badge" :class="sectorInfo.type">
                {{ getTypeLabel(sectorInfo.type) }}
              </span>
              <span class="stock-count-badge">{{ sectorInfo.stock_count }} 只成分股</span>
            </div>
            <div class="info-right">
              <span class="change-pct" :class="getChangeClass(sectorInfo.avg_change_pct)">
                {{ sectorInfo.avg_change_pct ? sectorInfo.avg_change_pct.toFixed(2) + '%' : 'N/A' }}
              </span>
            </div>
          </div>
          <div class="info-stats">
            <div class="stat-item">
              <span class="stat-label">交易日期</span>
              <span class="stat-value">{{ sectorInfo.trade_date || 'N/A' }}</span>
            </div>
            <div class="stat-item">
              <span class="stat-label">总成交量</span>
              <span class="stat-value">{{ formatVolume(sectorInfo.total_volume) }}</span>
            </div>
            <div class="stat-item">
              <span class="stat-label">总成交额</span>
              <span class="stat-value">{{ formatAmount(sectorInfo.total_amount) }}</span>
            </div>
            <div class="stat-item">
              <span class="stat-label">上涨/下跌/平盘</span>
              <span class="stat-value">
                <span class="rise-count">{{ sectorInfo.rise_count }}</span> / 
                <span class="fall-count">{{ sectorInfo.fall_count }}</span> / 
                <span class="flat-count">{{ sectorInfo.flat_count }}</span>
              </span>
            </div>
          </div>
        </div>
      </div>

      <!-- 板块K线图和成交量 -->
      <div class="kline-section">
        <h2>板块K线图</h2>
        <div class="chart-container">
          <div id="kline-chart" class="chart"></div>
        </div>
        <h2>成交量/成交额</h2>
        <div class="chart-container">
          <div id="volume-chart" class="chart"></div>
        </div>
      </div>

      <!-- 板块历史数据 -->
      <div class="history-section">
        <h2>历史涨跌统计 (过去15天)</h2>
        <div v-if="historyLoading" class="loading">加载中...</div>
        <div v-else-if="sectorHistory.length > 0" class="history-table">
          <table>
            <thead>
              <tr>
                <th>日期</th>
                <th>上涨家数</th>
                <th>下跌家数</th>
                <th>平盘家数</th>
                <th>上涨比例</th>
                <th>涨跌幅</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in sectorHistory" :key="item.trade_date">
                <td>{{ item.trade_date }}</td>
                <td class="rise-count">{{ item.rise_count }}</td>
                <td class="fall-count">{{ item.fall_count }}</td>
                <td class="flat-count">{{ item.flat_count }}</td>
                <td>
                  <span :class="{ 'bold-text': calculateRiseRatioValue(item.rise_count, item.fall_count, item.flat_count) > 50 }">
                    {{ calculateRiseRatio(item.rise_count, item.fall_count, item.flat_count) }}
                  </span>
                </td>
                <td>
                  <span :class="'change-pct ' + getChangeClass(item.change_pct)">
                    {{ item.change_pct ? item.change_pct.toFixed(2) + '%' : 'N/A' }}
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="empty">
          暂无历史数据
        </div>
      </div>

      <!-- 成分股列表 -->
      <div class="stocks-section">
        <h2>成分股列表</h2>
        <div class="stocks-table">
          <table>
            <thead>
              <tr>
                <th>代码</th>
                <th>名称</th>
                <th>市场</th>
                <th>涨跌幅</th>
                <th>成交量</th>
                <th>成交额</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="stock in sectorStocks" :key="stock.code">
                <td>{{ stock.code }}</td>
                <td>{{ stock.name }}</td>
                <td>{{ stock.market || '-' }}</td>
                <td>
                  <span class="change-pct" :class="getChangeClass(stock.change_pct)">
                    {{ stock.change_pct ? stock.change_pct.toFixed(2) + '%' : 'N/A' }}
                  </span>
                </td>
                <td>{{ formatVolume(stock.volume) }}</td>
                <td>{{ formatAmount(stock.amount) }}</td>
                <td>
                  <button @click="goToStock(stock.code)" class="view-btn">查看</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="!loading && sectorStocks.length === 0" class="empty">
          暂无成分股数据
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import axios from 'axios'
import * as echarts from 'echarts'

const router = useRouter()
const route = useRoute()
const API_BASE = import.meta.env.VITE_API_BASE || '/api'

// 响应式数据
const loading = ref(false)
const sectorInfo = ref(null)
const sectorStocks = ref([])
const sectorHistory = ref([])
const historyLoading = ref(false)
const klineData = ref([])
const klineChart = ref(null)
const volumeChart = ref(null)

// 加载板块历史数据
const loadSectorHistory = async () => {
  const code = route.params.code
  if (!code) {
    return
  }

  historyLoading.value = true
  try {
    const response = await axios.get(`${API_BASE}/sectors/${code}/history`)
    sectorHistory.value = response.data
  } catch (error) {
    console.error('加载板块历史数据失败:', error)
  } finally {
    historyLoading.value = false
  }
}

// 加载板块详情
const loadSectorDetail = async () => {
  const code = route.params.code
  if (!code) {
    router.push('/sectors')
    return
  }

  loading.value = true
  try {
    const response = await axios.get(`${API_BASE}/sectors/${code}`)
    sectorInfo.value = response.data
    sectorStocks.value = response.data.stocks || []
    // 加载板块历史数据
    await loadSectorHistory()
  } catch (error) {
    console.error('加载板块详情失败:', error)
    alert('加载板块详情失败: ' + (error.response?.data?.detail || error.message))
  } finally {
    loading.value = false
  }
}



// 跳转到股票详情
const goToStock = (code) => {
  router.push(`/stock/${code}`)
}

// 返回板块列表
const goBack = () => {
  router.push('/sectors')
}

// 工具函数
const getTypeLabel = (type) => {
  const typeMap = {
    'industry': '行业板块',
    'theme': '概念板块'
  }
  return typeMap[type] || type
}

const getChangeClass = (change_pct) => {
  if (change_pct > 0) return 'rise'
  if (change_pct < 0) return 'fall'
  return 'flat'
}

const formatVolume = (volume) => {
  if (!volume) return 'N/A'
  if (volume >= 100000000) {
    return (volume / 100000000).toFixed(2) + '亿'
  } else if (volume >= 10000) {
    return (volume / 10000).toFixed(2) + '万'
  }
  return volume
}

const formatAmount = (amount) => {
  if (!amount) return 'N/A'
  if (amount >= 100000000) {
    return (amount / 100000000).toFixed(2) + '亿'
  } else if (amount >= 10000) {
    return (amount / 10000).toFixed(2) + '万'
  }
  return amount
}

const calculateRiseRatioValue = (rise_count, fall_count, flat_count) => {
  const total = rise_count + fall_count + flat_count
  if (total === 0) return 0
  return (rise_count / total) * 100
}

const calculateRiseRatio = (rise_count, fall_count, flat_count) => {
  const ratio = calculateRiseRatioValue(rise_count, fall_count, flat_count)
  return ratio.toFixed(2) + '%'
}

// 加载板块K线数据
const loadSectorKline = async () => {
  const code = route.params.code
  if (!code) {
    return
  }

  try {
    const response = await axios.get(`${API_BASE}/sectors/${code}/kline`)
    // 后端返回的数据格式是 { code, name, klines: [] }
    // 反转数据，使其按时间正序排列
    klineData.value = (response.data.klines || []).reverse()
    // 初始化图表
    initCharts()
  } catch (error) {
    console.error('加载板块K线数据失败:', error)
  }
}

// 初始化图表
const initCharts = () => {
  if (klineData.value.length === 0) {
    return
  }

  // 准备K线数据（蜡烛图）
  const klineSeries = klineData.value.map(item => [
    item.open || 0,
    item.close || 0,
    item.low || 0,
    item.high || 0
  ])

  // 准备成交量数据
  const volumeSeries = klineData.value.map(item => [
    item.trade_date,
    item.total_volume || 0
  ])

  // 准备成交额数据
  const amountSeries = klineData.value.map(item => [
    item.trade_date,
    item.total_amount || 0
  ])

  // 初始化K线图（蜡烛图）
  if (klineChart.value) {
    klineChart.value.dispose()
  }
  klineChart.value = echarts.init(document.getElementById('kline-chart'))
  // 计算价格范围，使K线显示更合理
  const prices = klineData.value.flatMap(item => [item.open, item.high, item.low, item.close].filter(p => p > 0))
  const minPrice = Math.min(...prices)
  const maxPrice = Math.max(...prices)
  const priceRange = maxPrice - minPrice
  // 计算更合理的价格范围，留出更多空间
  const minPriceDisplay = Math.floor((minPrice - priceRange * 0.05) / 10) * 10
  const maxPriceDisplay = Math.ceil((maxPrice + priceRange * 0.05) / 10) * 10

  klineChart.value.setOption({
    title: {
      text: '板块K线图',
      left: 'center'
    },
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: 'cross'
      },
      formatter: function(params) {
        const data = params[0].data
        return `
          <div style="font-weight:bold;margin-bottom:5px;">${params[0].name}</div>
          <div>开盘: ${data[0].toFixed(2)}</div>
          <div>收盘: ${data[1].toFixed(2)}</div>
          <div>最低: ${data[2].toFixed(2)}</div>
          <div>最高: ${data[3].toFixed(2)}</div>
        `
      }
    },
    grid: {
      left: '8%',
      right: '5%',
      bottom: '15%', // 增加底部空间，避免日期标签重叠
      top: '15%', // 增加顶部空间
      containLabel: true
    },
    xAxis: {
      type: 'category',
      data: klineData.value.map(item => item.trade_date),
      axisLabel: {
        rotate: 45,
        interval: Math.floor(klineData.value.length / 8), // 只显示约8个日期标签
        formatter: function(value) {
          // 只显示月和日，不显示年
          return value.substring(5)
        }
      },
      boundaryGap: false
    },
    yAxis: {
      type: 'value',
      min: minPriceDisplay,
      max: maxPriceDisplay,
      axisLabel: {
        formatter: function(value) {
          // 价格标签显示为整数
          return Math.round(value)
        }
      },
      splitNumber: 5 // 只显示5条水平网格线
    },
    series: [{
      name: 'K线',
      type: 'candlestick',
      data: klineSeries,
      itemStyle: {
        color: '#f5222d',  // 上涨颜色
        color0: '#52c41a', // 下跌颜色
        borderColor: '#f5222d',
        borderColor0: '#52c41a'
      },
      barWidth: '60%',
      barMaxWidth: 10
    }]
  })

  // 初始化成交量图
  if (volumeChart.value) {
    volumeChart.value.dispose()
  }
  volumeChart.value = echarts.init(document.getElementById('volume-chart'))
  volumeChart.value.setOption({
    title: {
      text: '成交量/成交额',
      left: 'center'
    },
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: 'cross'
      }
    },
    legend: {
      data: ['成交量', '成交额'],
      top: 30
    },
    grid: {
      left: '8%',
      right: '5%',
      bottom: '15%', // 增加底部空间，避免日期标签重叠
      top: '15%', // 增加顶部空间
      containLabel: true
    },
    xAxis: {
      type: 'category',
      data: klineData.value.map(item => item.trade_date),
      axisLabel: {
        rotate: 45,
        interval: Math.floor(klineData.value.length / 8), // 只显示约8个日期标签
        formatter: function(value) {
          // 只显示月和日，不显示年
          return value.substring(5)
        }
      },
      boundaryGap: false
    },
    yAxis: [
      {
        type: 'value',
        name: '成交量',
        position: 'left',
        splitNumber: 4 // 只显示4条水平网格线
      },
      {
        type: 'value',
        name: '成交额',
        position: 'right',
        splitNumber: 4 // 只显示4条水平网格线
      }
    ],
    series: [
      {
        name: '成交量',
        type: 'bar',
        data: volumeSeries,
        itemStyle: {
          color: function(params) {
            const idx = params.dataIndex
            const item = klineData.value[idx]
            if (item.open !== undefined && item.close !== undefined) {
              // 根据K线的涨跌情况设置颜色
              return item.close >= item.open ? '#f5222d' : '#52c41a'
            }
            return '#1890ff'
          }
        },
        barWidth: '60%',
        barMaxWidth: 10
      },
      {
        name: '成交额',
        type: 'line',
        yAxisIndex: 1,
        data: amountSeries,
        lineStyle: {
          color: '#1890ff'
        },
        symbol: 'circle',
        symbolSize: 4
      }
    ]
  })

  // 监听窗口大小变化
  window.addEventListener('resize', handleResize)
}

// 处理窗口大小变化
const handleResize = () => {
  klineChart.value?.resize()
  volumeChart.value?.resize()
}

// 销毁图表
const destroyCharts = () => {
  klineChart.value?.dispose()
  volumeChart.value?.dispose()
  window.removeEventListener('resize', handleResize)
}

// 生命周期
onMounted(() => {
  loadSectorDetail()
  loadSectorKline()
})

onUnmounted(() => {
  destroyCharts()
})
</script>

<style scoped>
.sector-detail {
  min-height: 100vh;
  background-color: #f5f5f5;
  padding: 20px 0;
}

.container {
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 20px;
}

.back-bar {
  margin-bottom: 20px;
}

.back-btn {
  background-color: #1890ff;
  color: white;
  border: none;
  padding: 8px 16px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 14px;
}

.back-btn:hover {
  background-color: #40a9ff;
}

.header-card {
  background-color: white;
  border-radius: 8px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
  padding: 20px;
  margin-bottom: 20px;
}

.loading {
  text-align: center;
  padding: 40px 0;
  color: #666;
}

.sector-info {
  width: 100%;
}

.info-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}

.info-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.sector-title {
  font-size: 24px;
  font-weight: bold;
  margin: 0;
}

.sector-badge {
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 12px;
  color: white;
}

.sector-badge.industry {
  background-color: #1890ff;
}

.sector-badge.theme {
  background-color: #52c41a;
}

.stock-count-badge {
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 12px;
  background-color: #f0f0f0;
  color: #666;
}

.info-right {
  display: flex;
  align-items: center;
  gap: 12px;
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

.rise-count {
  color: #f5222d;
}

.fall-count {
  color: #52c41a;
}

.flat-count {
  color: #666;
}

.bold-text {
  font-weight: bold;
  font-size: 14px;
}

.history-section {
  background-color: white;
  border-radius: 8px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
  padding: 20px;
  margin-bottom: 20px;
}

.history-section h2 {
  font-size: 18px;
  font-weight: bold;
  margin: 0 0 20px 0;
}

.history-table {
  overflow-x: auto;
}

.history-table table {
  width: 100%;
  border-collapse: collapse;
}

.history-table th,
.history-table td {
  padding: 12px;
  text-align: left;
  border-bottom: 1px solid #f0f0f0;
}

.history-table th {
  background-color: #fafafa;
  font-weight: bold;
  font-size: 14px;
  color: #333;
}

.history-table td {
  font-size: 14px;
  color: #333;
}

.stocks-section {
  background-color: white;
  border-radius: 8px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
  padding: 20px;
}

.stocks-section h2 {
  font-size: 18px;
  font-weight: bold;
  margin: 0 0 20px 0;
}

.stocks-table {
  overflow-x: auto;
}

.stocks-table table {
  width: 100%;
  border-collapse: collapse;
}

.stocks-table th,
.stocks-table td {
  padding: 12px;
  text-align: left;
  border-bottom: 1px solid #f0f0f0;
}

.stocks-table th {
  background-color: #fafafa;
  font-weight: bold;
  font-size: 14px;
  color: #333;
}

.stocks-table td {
  font-size: 14px;
  color: #333;
}

.view-btn {
  background-color: #1890ff;
  color: white;
  border: none;
  padding: 4px 8px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
}

.view-btn:hover {
  background-color: #40a9ff;
}

.kline-section {
  background-color: white;
  border-radius: 8px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
  padding: 20px;
  margin-bottom: 20px;
}

.kline-section h2 {
  font-size: 18px;
  font-weight: bold;
  margin: 0 0 20px 0;
}

.chart-container {
  margin-bottom: 30px;
}

.chart {
  width: 100%;
  height: 500px;
}

.empty {
  text-align: center;
  padding: 40px 0;
  color: #666;
}

@media (max-width: 768px) {
  .info-header {
    flex-direction: column;
    align-items: flex-start;
    gap: 12px;
  }

  .info-right {
    width: 100%;
    justify-content: space-between;
  }

  .info-stats {
    grid-template-columns: 1fr;
  }
}
</style>