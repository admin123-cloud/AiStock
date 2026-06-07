<template>
  <div class="data-stats">
    <div class="container">
      <div class="header">
        <h1>数据缺失统计</h1>
        <p>分析股票数据库的数据完整性，识别缺失数据和数据质量问题</p>
      </div>
      
      <div class="score-card">
        <div class="score">{{ completenessScore }}</div>
        <div class="label">数据完整性评分</div>
      </div>
      
      <div class="grid">
        <div class="card">
          <h2>数据总览</h2>
          <div v-if="loading" class="loading">加载中...</div>
          <div v-else-if="overviewData">
            <div class="stat-grid">
              <div class="stat-item">
                <div class="label">股票总数</div>
                <div class="value">{{ overviewData.total_stocks }}</div>
              </div>
              <div class="stat-item">
                <div class="label">市场分布</div>
                <div class="value">{{ Object.keys(overviewData.market_stats).length }}</div>
              </div>
            </div>
            <h3>市场分布</h3>
            <table class="table">
              <thead>
                <tr>
                  <th>市场</th>
                  <th>数量</th>
                  <th>占比</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(count, market) in overviewData.market_stats" :key="market">
                  <td>{{ market }}</td>
                  <td>{{ count }}</td>
                  <td>{{ ((count / overviewData.total_stocks) * 100).toFixed(1) }}%</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
        
        <div class="card">
          <h2>K线数据缺失</h2>
          <div class="controls">
            <button class="btn" @click="refreshKlineStats">刷新</button>
            <select v-model="selectedDays" @change="refreshKlineStats" class="btn btn-outline">
              <option value="30">最近30天</option>
              <option value="60">最近60天</option>
              <option value="90">最近90天</option>
            </select>
          </div>
          <div v-if="klineLoading" class="loading">加载中...</div>
          <div v-else-if="klineData">
            <div class="stat-grid">
              <div class="stat-item">
                <div class="label">股票总数</div>
                <div class="value">{{ klineData.total_stocks }}</div>
              </div>
              <div class="stat-item">
                <div class="label">有数据</div>
                <div class="value">{{ klineData.stocks_with_data }}</div>
                <span class="percentage good">{{ ((klineData.stocks_with_data / klineData.total_stocks) * 100).toFixed(1) }}%</span>
              </div>
              <div class="stat-item">
                <div class="label">无数据</div>
                <div class="value">{{ klineData.stocks_without_data }}</div>
                <span class="percentage bad">{{ ((klineData.stocks_without_data / klineData.total_stocks) * 100).toFixed(1) }}%</span>
              </div>
              <div class="stat-item">
                <div class="label">总体缺失率</div>
                <div class="value">{{ klineData.overall_missing_rate }}%</div>
                <span class="percentage" :class="getRateClass(klineData.overall_missing_rate)">{{ klineData.overall_missing_rate }}%</span>
              </div>
            </div>
            <h3>缺失情况</h3>
            <div class="progress-bar">
              <div class="progress-fill" :style="{ width: (100 - klineData.overall_missing_rate) + '%' }"></div>
            </div>
          </div>
        </div>
        
        <div class="card">
          <h2>字段缺失统计</h2>
          <div v-if="fieldLoading" class="loading">加载中...</div>
          <div v-else-if="fieldData">
            <div class="stat-grid">
              <div class="stat-item">
                <div class="label">行业字段缺失</div>
                <div class="value">{{ fieldData.missing_industry.count }}</div>
                <span class="percentage" :class="getRateClass(fieldData.missing_industry.rate)">{{ fieldData.missing_industry.rate }}%</span>
              </div>
              <div class="stat-item">
                <div class="label">板块字段缺失</div>
                <div class="value">{{ fieldData.missing_sector.count }}</div>
                <span class="percentage" :class="getRateClass(fieldData.missing_sector.rate)">{{ fieldData.missing_sector.rate }}%</span>
              </div>
              <div class="stat-item">
                <div class="label">上市日期缺失</div>
                <div class="value">{{ fieldData.missing_list_date.count }}</div>
                <span class="percentage" :class="getRateClass(fieldData.missing_list_date.rate)">{{ fieldData.missing_list_date.rate }}%</span>
              </div>
              <div class="stat-item">
                <div class="label">总体完整性</div>
                <div class="value">{{ (100 - (fieldData.missing_industry.rate + fieldData.missing_sector.rate + fieldData.missing_list_date.rate) / 3).toFixed(1) }}%</div>
              </div>
            </div>
          </div>
        </div>
        
        <div class="card">
          <h2>市场分布</h2>
          <div ref="marketChart" class="chart"></div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, nextTick } from 'vue'
import axios from 'axios'
import * as echarts from 'echarts'

const API_BASE = import.meta.env.VITE_API_BASE || '/api'

const loading = ref(true)
const klineLoading = ref(true)
const fieldLoading = ref(true)
const completenessScore = ref('--')
const overviewData = ref(null)
const klineData = ref(null)
const fieldData = ref(null)
const selectedDays = ref(30)
const marketChart = ref(null)

const getRateClass = (rate) => {
  return rate > 50 ? 'bad' : rate > 20 ? 'warning' : 'good'
}

const loadCompletenessScore = async () => {
  try {
    const response = await axios.get(`${API_BASE}/data-statistics/completeness`)
    completenessScore.value = response.data.overall_score
  } catch (error) {
    console.error('加载评分失败:', error)
  }
}

const loadDataOverview = async () => {
  loading.value = true
  try {
    const response = await axios.get(`${API_BASE}/data-statistics/overview`)
    overviewData.value = response.data
    
    await nextTick()
    renderMarketChart(overviewData.value.market_stats)
  } catch (error) {
    console.error('加载总览失败:', error)
  } finally {
    loading.value = false
  }
}

const refreshKlineStats = async () => {
  klineLoading.value = true
  try {
    const response = await axios.get(`${API_BASE}/data-statistics/kline-missing`, {
      params: { days: parseInt(selectedDays.value) }
    })
    klineData.value = response.data
  } catch (error) {
    console.error('加载K线统计失败:', error)
  } finally {
    klineLoading.value = false
  }
}

const loadFieldStats = async () => {
  fieldLoading.value = true
  try {
    const response = await axios.get(`${API_BASE}/data-statistics/field-missing`)
    fieldData.value = response.data
  } catch (error) {
    console.error('加载字段统计失败:', error)
  } finally {
    fieldLoading.value = false
  }
}

const renderMarketChart = (marketStats) => {
  if (!marketChart.value) return
  
  const chart = echarts.init(marketChart.value)
  
  const option = {
    tooltip: {
      trigger: 'item',
      formatter: '{b}: {c} ({d}%)'
    },
    legend: {
      orient: 'vertical',
      left: 'left'
    },
    series: [
      {
        name: '市场分布',
        type: 'pie',
        radius: ['40%', '70%'],
        avoidLabelOverlap: false,
        itemStyle: {
          borderRadius: 10,
          borderColor: '#fff',
          borderWidth: 2
        },
        label: {
          show: false,
          position: 'center'
        },
        emphasis: {
          label: {
            show: true,
            fontSize: 20,
            fontWeight: 'bold'
          }
        },
        labelLine: {
          show: false
        },
        data: Object.entries(marketStats).map(([name, value]) => ({
          name,
          value,
          itemStyle: {
            color: {
              'SH': '#667eea',
              'SZ': '#764ba2',
              'BJ': '#f56565'
            }[name] || '#a0aec0'
          }
        }))
      }
    ]
  }
  
  chart.setOption(option)
}

onMounted(() => {
  loadCompletenessScore()
  loadDataOverview()
  refreshKlineStats()
  loadFieldStats()
})
</script>

<style scoped>
.data-stats {
  min-height: 100vh;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  padding: 20px;
}

.container {
  max-width: 1400px;
  margin: 0 auto;
}

.header {
  background: rgba(255, 255, 255, 0.95);
  border-radius: 16px;
  padding: 30px;
  margin-bottom: 24px;
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.1);
}

.header h1 {
  color: #2d3748;
  font-size: 28px;
  margin-bottom: 8px;
}

.header p {
  color: #718096;
  font-size: 14px;
}

.score-card {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  border-radius: 12px;
  padding: 24px;
  color: white;
  text-align: center;
  margin-bottom: 24px;
  box-shadow: 0 8px 32px rgba(102, 126, 234, 0.3);
}

.score-card .score {
  font-size: 48px;
  font-weight: bold;
  margin-bottom: 8px;
}

.score-card .label {
  font-size: 16px;
  opacity: 0.9;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 20px;
}

.card {
  background: rgba(255, 255, 255, 0.95);
  border-radius: 16px;
  padding: 24px;
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.08);
}

.card h2 {
  color: #2d3748;
  font-size: 20px;
  margin-bottom: 20px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.card h2::before {
  content: '';
  width: 4px;
  height: 20px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  border-radius: 2px;
}

.card h3 {
  color: #4a5568;
  font-size: 16px;
  margin: 20px 0 12px 0;
}

.stat-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 16px;
  margin-bottom: 20px;
}

.stat-item {
  background: #f7fafc;
  border-radius: 8px;
  padding: 16px;
}

.stat-item .label {
  color: #718096;
  font-size: 12px;
  margin-bottom: 4px;
}

.stat-item .value {
  color: #2d3748;
  font-size: 24px;
  font-weight: bold;
}

.stat-item .percentage {
  font-size: 12px;
  margin-left: 8px;
}

.stat-item .percentage.good {
  color: #48bb78;
}

.stat-item .percentage.warning {
  color: #ed8936;
}

.stat-item .percentage.bad {
  color: #f56565;
}

.chart {
  width: 100%;
  height: 300px;
}

.table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 16px;
}

.table th {
  background: #f7fafc;
  color: #4a5568;
  font-weight: 600;
  padding: 12px;
  text-align: left;
  font-size: 12px;
  border-bottom: 2px solid #e2e8f0;
}

.table td {
  padding: 12px;
  border-bottom: 1px solid #e2e8f0;
  font-size: 12px;
  color: #4a5568;
}

.table tr:hover {
  background: #f7fafc;
}

.controls {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}

.btn {
  padding: 8px 16px;
  border: none;
  border-radius: 6px;
  background: #667eea;
  color: white;
  cursor: pointer;
  font-size: 14px;
  transition: all 0.3s;
}

.btn:hover {
  background: #5568d3;
  transform: translateY(-2px);
}

.btn-outline {
  background: transparent;
  border: 2px solid #667eea;
  color: #667eea;
}

.btn-outline:hover {
  background: #667eea;
  color: white;
}

.loading {
  text-align: center;
  padding: 40px;
  color: #718096;
}

.progress-bar {
  width: 100%;
  height: 8px;
  background: #e2e8f0;
  border-radius: 4px;
  overflow: hidden;
  margin: 16px 0;
}

.progress-fill {
  height: 100%;
  background: linear-gradient(90deg, #667eea 0%, #764ba2 100%);
  transition: width 0.5s;
}
</style>
