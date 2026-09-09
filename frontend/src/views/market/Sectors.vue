<template>
  <div class="sectors">
    <div class="container">
      <div class="header">
        <div class="header-content">
          <div class="header-title">
            <h1>板块监控</h1>
            <p>实时监控各行业板块和概念题材</p>
          </div>
          <div class="header-actions">
            <button @click="refreshData" class="refresh-btn">刷新</button>
          </div>
        </div>
        <!-- 热点题材统计 -->
        <div class="stats-cards" v-if="hotSectors.length > 0">
          <div class="stat-card hot" :class="{ active: filters.type === 'hot' }" @click="filterByType('hot')">
            <div class="stat-value">{{ hotSectors.length }}</div>
            <div class="stat-label">热点题材</div>
          </div>
        </div>
      </div>

      <!-- 筛选区域 -->
      <div class="filter-bar">
        <div class="filter-item">
          <label>类型:</label>
          <select v-model="filters.type" @change="handleTypeChange">
            <option value="">全部</option>
            <option value="industry_level1">一级行业</option>
            <option value="industry_level2">二级行业</option>
            <option value="industry_level3">三级行业</option>
          </select>
        </div>
        <div class="filter-item">
          <label>排序:</label>
          <select v-model="filters.sortBy" @change="loadSectors">
            <option value="rise_ratio_desc">今日涨幅（降序）</option>
            <option value="rise_ratio_asc">今日涨幅（升序）</option>
            <option value="stats_15d_rise_ratio_desc">15日涨幅（降序）</option>
            <option value="stats_15d_rise_ratio_asc">15日涨幅（升序）</option>
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
      </div>

      <!-- 板块列表 -->
      <div class="sectors-grid" v-if="!loading && sectors.length > 0">
        <div
          v-for="sector in sectors"
          :key="sector.code"
          class="sector-card"
          :class="sector.type"
          @click="goToSectorDetail(sector.code)"
        >
          <div class="sector-header">
            <span class="sector-type" :class="sector.type">
              {{ getTypeLabel(sector.type, sector.level) }}
            </span>
            <span class="sector-change-pct" :class="getChangePctClass(sector.change_pct)">
              {{ formatChangePct(sector.change_pct) }}
            </span>
          </div>
          <div class="sector-name-container">
            <span class="sector-name">{{ sector.name }}</span>
            <span class="sector-code">{{ sector.code }}</span>
          </div>

          <!-- 上涨率统计 -->
          <div class="historical-stats">
            <!-- 今日上涨率 -->
            <div class="stat-row">
              <span class="stat-label">今日:</span>
              <span class="stat-value">
                <span class="rise-count">{{ sector.rise_count || 0 }}</span> / 
                <span class="fall-count">{{ sector.fall_count || 0 }}</span> / 
                <span class="total-count">{{ sector.stock_count || 0 }}</span>
                <span class="rise-ratio">({{ sector.rise_ratio !== undefined && sector.rise_ratio !== null ? sector.rise_ratio.toFixed(2) + '%' : 'N/A' }})</span>
              </span>
            </div>
            <!-- 5日上涨率 -->
            <div class="stat-row">
              <span class="stat-label">5日:</span>
              <span class="stat-value">
                <template v-if="sectorHistoricalStats[sector.code]?.stats_5d">
                  <span class="rise-count">{{ sectorHistoricalStats[sector.code].stats_5d.rise_days }}</span> / 
                  <span class="fall-count">{{ sectorHistoricalStats[sector.code].stats_5d.total_days - sectorHistoricalStats[sector.code].stats_5d.rise_days }}</span> / 
                  <span class="total-count">{{ sectorHistoricalStats[sector.code].stats_5d.total_days }}</span>
                  <span class="rise-ratio">({{ sectorHistoricalStats[sector.code].stats_5d.rise_ratio }}%)</span>
                </template>
                <template v-else>
                  --/--/-- (--%)
                </template>
              </span>
            </div>
            <!-- 10日上涨率 -->
            <div class="stat-row">
              <span class="stat-label">10日:</span>
              <span class="stat-value">
                <template v-if="sectorHistoricalStats[sector.code]?.stats_10d">
                  <span class="rise-count">{{ sectorHistoricalStats[sector.code].stats_10d.rise_days }}</span> / 
                  <span class="fall-count">{{ sectorHistoricalStats[sector.code].stats_10d.total_days - sectorHistoricalStats[sector.code].stats_10d.rise_days }}</span> / 
                  <span class="total-count">{{ sectorHistoricalStats[sector.code].stats_10d.total_days }}</span>
                  <span class="rise-ratio">({{ sectorHistoricalStats[sector.code].stats_10d.rise_ratio }}%)</span>
                </template>
                <template v-else>
                  --/--/-- (--%)
                </template>
              </span>
            </div>
            <!-- 15日上涨率 -->
            <div class="stat-row">
              <span class="stat-label">15日:</span>
              <span class="stat-value">
                <template v-if="sectorHistoricalStats[sector.code]?.stats_15d">
                  <span class="rise-count">{{ sectorHistoricalStats[sector.code].stats_15d.rise_days }}</span> / 
                  <span class="fall-count">{{ sectorHistoricalStats[sector.code].stats_15d.total_days - sectorHistoricalStats[sector.code].stats_15d.rise_days }}</span> / 
                  <span class="total-count">{{ sectorHistoricalStats[sector.code].stats_15d.total_days }}</span>
                  <span class="rise-ratio">({{ sectorHistoricalStats[sector.code].stats_15d.rise_ratio }}%)</span>
                </template>
                <template v-else>
                  --/--/-- (--%)
                </template>
              </span>
            </div>
          </div>

          <!-- 涨跌停统计 -->
          <div class="sector-stats" v-if="sector.limit_up_count !== undefined || sector.limit_down_count !== undefined">
            <div class="stat-row">
              <span class="stat-label">涨停:</span>
              <span class="stat-value highlight">{{ sector.limit_up_count || 0 }}</span>
            </div>
            <div class="stat-row">
              <span class="stat-label">跌停:</span>
              <span class="stat-value">{{ sector.limit_down_count || 0 }}</span>
            </div>
          </div>

          <div class="sector-footer">
            <span class="stock-count">{{ sector.stock_count || 0 }} 只成分股</span>
          </div>
        </div>
      </div>

      <!-- 加载中 -->
      <div v-if="loading" class="loading">加载中...</div>

      <!-- 无数据 -->
      <div v-if="!loading && sectors.length === 0" class="empty">
        <p>暂无板块数据</p>
        <p class="hint">点击上方"同步板块"按钮从 efinance 获取板块列表</p>
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
        <select v-model="pageSize" @change="loadSectors" class="page-size">
          <option :value="12">12条/页</option>
          <option :value="20">20条/页</option>
          <option :value="50">50条/页</option>
          <option :value="100">100条/页</option>
        </select>
      </div>
    </div>

    <!-- 板块详情弹窗 -->
    <div class="modal-overlay" v-if="showDetail" @click.self="closeDetail">
      <div class="modal-content">
        <div class="modal-header">
          <h3>{{ selectedSector.name }}</h3>
          <div class="header-right">
            <span class="sector-type-badge" :class="selectedSector.type">
              {{ getTypeLabel(selectedSector.type, selectedSector.level) }}
            </span>
            <button 
              @click="updateSectorKlineStats" 
              class="update-kline-btn" 
              :disabled="updatingKlineStats"
            >
              {{ updatingKlineStats ? '更新中...' : '更新成分涨跌' }}
            </button>
            <button class="close-btn" @click="closeDetail">&times;</button>
          </div>
        </div>
        <div class="modal-body">
          <div class="detail-info">
            <span>代码: {{ selectedSector.code }}</span>
            <span>成分股: {{ selectedSector.stock_count || sectorStocks.length }} 只</span>
          </div>
          <div class="stocks-list" v-if="sectorStocks.length > 0">
            <div class="stocks-header">成分股列表</div>
            <div class="stock-item" v-for="stock in sectorStocks" :key="stock.code">
              <span class="stock-code">{{ stock.code }}</span>
              <span class="stock-name">{{ stock.name }}</span>
              <button class="view-btn" @click="goToStock(stock.code)">查看</button>
            </div>
          </div>
          <div class="no-stocks" v-else>
            暂无成分股数据
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import axios from 'axios'

const router = useRouter()
const API_BASE = import.meta.env.VITE_API_BASE || '/api'

// 响应式数据
const sectors = ref([])
const hotSectors = ref([])
const stats = ref(null)
const loading = ref(false)
const syncingComponents = ref(false)
const syncingData = ref(false)
const updatingKlineStats = ref(false)
const updatingAllKlineStats = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = ref(12)
const sectorHistoricalStats = ref({})



const filters = ref({
  type: '',
  search: '',
  sortBy: 'rise_ratio_desc'  // 默认按今日涨幅降序排序
})

// 板块详情
const showDetail = ref(false)
const selectedSector = ref({})
const sectorStocks = ref([])

// 计算总页数
const totalPages = computed(() => {
  return Math.ceil(total.value / pageSize.value)
})

// 加载板块历史上涨率数据
const loadSectorHistoricalStats = async (sectorCode) => {
  try {
    const response = await axios.get(`${API_BASE}/sectors/${sectorCode}/historical-stats`)
    if (response.data) {
      sectorHistoricalStats.value[sectorCode] = response.data
    }
  } catch (error) {
    console.error(`加载板块 ${sectorCode} 历史数据失败:`, error)
  }
}

// 加载板块列表
const loadSectors = async () => {
  loading.value = true
  try {
    const params = {
      page: page.value,
      page_size: pageSize.value
    }

    if (filters.value.type) {
      if (filters.value.type === 'hot') {
        // 热点题材筛选，在客户端处理
        console.log('热点题材筛选')
      } else if (filters.value.type.startsWith('industry_level')) {
        // 处理行业级别筛选
        params.sector_type = 'industry'
        const level = parseInt(filters.value.type.replace('industry_level', '')) // 获取级别数字并转换为整数
        params.level = level
        console.log('行业级别筛选:', level)
      } else {
        // 其他类型筛选
        params.sector_type = filters.value.type
      }
    }
    console.log('请求参数:', params)
    if (filters.value.search && filters.value.search.trim()) {
      params.search = filters.value.search.trim()
    }
    if (filters.value.sortBy) {
      params.sort_by = filters.value.sortBy
    }

    const response = await axios.get(`${API_BASE}/sectors/`, { params })

    if (response.data && response.data.items) {
      sectors.value = response.data.items
      total.value = response.data.total || 0

      // 加载每个板块的历史上涨率数据
      for (const sector of sectors.value) {
        await loadSectorHistoricalStats(sector.code)
      }

      // 计算热点题材：当天板块上涨比例超过70%的板块
      hotSectors.value = sectors.value.filter(sector => {
        return sector.rise_ratio !== undefined && sector.rise_ratio !== null && sector.rise_ratio > 70
      })

      // 热点题材筛选
      if (filters.value.type === 'hot') {
        sectors.value = hotSectors.value
        total.value = hotSectors.value.length
      }

      // 如果是按上涨比例或15日涨幅排序，进行客户端排序
      if (filters.value.sortBy && (filters.value.sortBy.startsWith('rise_ratio_') || filters.value.sortBy.startsWith('stats_15d_rise_ratio_'))) {
        sortSectorsByRiseRatio()
      }
    } else {
      sectors.value = []
      hotSectors.value = []
      total.value = 0
    }
  } catch (error) {
    console.error('加载板块列表失败:', error)
    sectors.value = []
    hotSectors.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}



// 按上涨比例排序
const sortSectorsByRiseRatio = () => {
  const sortBy = filters.value.sortBy
  
  if (sortBy.startsWith('rise_ratio_')) {
    // 今日涨幅排序
    const key = sortBy === 'rise_ratio_desc' ? -1 : 1
    sectors.value.sort((a, b) => {
      const ratioA = a.rise_ratio || -1
      const ratioB = b.rise_ratio || -1
      return (ratioA - ratioB) * key
    })
  } else if (sortBy.startsWith('stats_15d_rise_ratio_')) {
    // 15日涨幅排序
    const key = sortBy === 'stats_15d_rise_ratio_desc' ? -1 : 1
    sectors.value.sort((a, b) => {
      const ratioA = sectorHistoricalStats.value[a.code]?.stats_15d?.rise_ratio || -1
      const ratioB = sectorHistoricalStats.value[b.code]?.stats_15d?.rise_ratio || -1
      return (ratioA - ratioB) * key
    })
  }
}

// 加载统计信息
const loadStats = async () => {
  try {
    const response = await axios.get(`${API_BASE}/sectors/stats`)
    stats.value = response.data
  } catch (error) {
    console.error('加载统计信息失败:', error)
  }
}

// 更新成分
const updateComponents = async () => {
  if (!confirm('确定要从 efinance 更新板块成分股吗？')) {
    return
  }

  syncingComponents.value = true
  try {
    const response = await axios.post(`${API_BASE}/sectors/sync`)

    if (response.data.status === 'started') {
      alert('板块成分更新任务已启动，请稍候刷新页面')
      pollComponentSyncStatus()
    } else if (response.data.status === 'already_running') {
      alert('更新任务已在运行中')
      pollComponentSyncStatus()
    } else {
      alert('启动更新任务失败')
      syncingComponents.value = false
    }
  } catch (error) {
    console.error('更新板块成分失败:', error)
    alert('更新失败: ' + (error.response?.data?.detail || error.message))
    syncingComponents.value = false
  }
}

// 轮询成分更新状态
const pollComponentSyncStatus = async () => {
  try {
    const response = await axios.get(`${API_BASE}/sectors/sync/status`)
    const status = response.data

    if (status.is_running) {
      setTimeout(pollComponentSyncStatus, 2000)
    } else {
      syncingComponents.value = false
      if (status.results) {
        alert(`更新完成！\n新增: ${status.results.saved_count}\n更新: ${status.results.updated_count}`)
        loadSectors()
        loadStats()
      } else if (status.error) {
        alert('更新失败: ' + status.error)
      }
    }
  } catch (error) {
    console.error('获取更新状态失败:', error)
    syncingComponents.value = false
  }
}

// 同步板块K线数据
const syncSectorData = async () => {
  if (!confirm('确定要同步板块K线数据吗？这将根据成分股计算板块涨跌幅。')) {
    return
  }

  syncingData.value = true
  try {
    const response = await axios.post(`${API_BASE}/sectors/sync-kline`)

    if (response.data.status === 'started') {
      alert('板块K线数据同步任务已启动，请稍候刷新页面')
      pollKlineSyncStatus()
    } else if (response.data.status === 'already_running') {
      alert('同步任务已在运行中')
      pollKlineSyncStatus()
    } else {
      alert('启动同步任务失败')
      syncingData.value = false
    }
  } catch (error) {
    console.error('同步板块K线数据失败:', error)
    alert('同步失败: ' + (error.response?.data?.detail || error.message))
    syncingData.value = false
  }
}

// 轮询K线数据同步状态
const pollKlineSyncStatus = async () => {
  try {
    const response = await axios.get(`${API_BASE}/sectors/sync-kline/status`)
    const status = response.data

    if (status.is_running) {
      setTimeout(pollKlineSyncStatus, 2000)
    } else {
      syncingData.value = false
      if (status.results) {
        alert(`同步完成！\n生成记录数: ${status.results.total_count}`)
        loadSectors()
        loadStats()
      } else if (status.error) {
        alert('同步失败: ' + status.error)
      }
    }
  } catch (error) {
    console.error('获取K线同步状态失败:', error)
    syncingData.value = false
  }
}

// 批量更新所有板块成分涨跌数据
const updateAllKlineStats = async () => {
  if (!confirm('确定要批量更新所有板块的成分涨跌数据吗？这可能需要一些时间。')) {
    return
  }

  updatingAllKlineStats.value = true
  try {
    const response = await axios.post(`${API_BASE}/sectors/update-all-kline-stats`)

    if (response.data.status === 'started') {
      alert('批量更新任务已启动，请稍候刷新页面')
      pollUpdateAllStatsStatus()
    } else if (response.data.status === 'already_running') {
      alert('批量更新任务已在运行中')
      pollUpdateAllStatsStatus()
    } else {
      alert('启动批量更新任务失败')
      updatingAllKlineStats.value = false
    }
  } catch (error) {
    console.error('批量更新板块成分涨跌失败:', error)
    alert('批量更新失败: ' + (error.response?.data?.detail || error.message))
    updatingAllKlineStats.value = false
  }
}

// 轮询批量更新状态
const pollUpdateAllStatsStatus = async () => {
  try {
    const response = await axios.get(`${API_BASE}/sectors/update-all-kline-stats/status`)
    const status = response.data

    if (status.is_running) {
      const progress = status.progress
      console.log(`批量更新进度: ${progress.current}/${progress.total} - ${progress.current_sector}`)
      setTimeout(pollUpdateAllStatsStatus, 2000)
    } else {
      updatingAllKlineStats.value = false
      if (status.results) {
        alert(`批量更新完成！\n总板块数: ${status.results.total_sectors}\n成功更新: ${status.results.updated_sectors}\n失败: ${status.results.failed_sectors}`)
        loadSectors()
        loadStats()
      } else if (status.error) {
        alert('批量更新失败: ' + status.error)
      }
    }
  } catch (error) {
    console.error('获取批量更新状态失败:', error)
    updatingAllKlineStats.value = false
  }
}

// 显示板块详情
const showSectorDetail = async (sector) => {
  selectedSector.value = sector
  showDetail.value = true

  // 加载成分股
  try {
    const response = await axios.get(`${API_BASE}/sectors/${sector.code}/stocks`)
    sectorStocks.value = response.data.stocks || []
  } catch (error) {
    console.error('加载成分股失败:', error)
    sectorStocks.value = []
  }
}

// 关闭详情
const closeDetail = () => {
  showDetail.value = false
  selectedSector.value = {}
  sectorStocks.value = []
}

// 跳转到股票详情
const goToStock = (code) => {
  router.push(`/stock/${code}`)
}

// 跳转到板块详情
const goToSectorDetail = (code) => {
  router.push(`/sector/${code}`)
}

// 点击统计卡片筛选
const filterByType = (type) => {
  filters.value.type = type
  filters.value.sortBy = 'rise_ratio_desc'  // 选择类型时默认按今日涨幅降序排序
  page.value = 1
  loadSectors()
}

// 类型改变处理
const handleTypeChange = () => {
  page.value = 1
  loadSectors()
}

// 搜索处理（防抖）
let searchTimeout = null
const handleSearch = () => {
  clearTimeout(searchTimeout)
  searchTimeout = setTimeout(() => {
    page.value = 1
    loadSectors()
  }, 500)
}

// 翻页
const changePage = (newPage) => {
  if (newPage >= 1 && newPage <= totalPages.value) {
    page.value = newPage
    loadSectors()
  }
}

// 刷新数据
const refreshData = () => {
  page.value = 1
  loadSectors()
  loadStats()
}

// 更新板块成分涨跌数据
const updateSectorKlineStats = async () => {
  if (!selectedSector.value || !selectedSector.value.code) {
    alert('请先选择板块')
    return
  }

  if (!confirm(`确定要更新板块【${selectedSector.value.name}】的成分涨跌数据吗？`)) {
    return
  }

  updatingKlineStats.value = true
  try {
    const response = await axios.post(
      `${API_BASE}/sectors/${selectedSector.value.code}/update-kline-stats`
    )

    if (response.data.status === 'success') {
      alert(`更新完成！\n板块: ${response.data.name}\n更新记录数: ${response.data.updated_count}`)
    } else {
      alert('更新失败')
    }
  } catch (error) {
    console.error('更新板块成分涨跌失败:', error)
    alert('更新失败: ' + (error.response?.data?.detail || error.message))
  } finally {
    updatingKlineStats.value = false
  }
}

// 获取类型标签
const getTypeLabel = (type, level) => {
  const labels = {
    'industry': '行业',
    'concept': '概念',
    'index': '指数'
  }
  const baseLabel = labels[type] || type
  // 如果是行业板块，显示级别
  if (type === 'industry' && level) {
    return `${level}级${baseLabel}`
  }
  return baseLabel
}

// 格式化涨跌幅
const formatChangePct = (value) => {
  if (value === null || value === undefined) {
    return '--'
  }
  return `${value > 0 ? '+' : ''}${value}%`
}

// 获取涨跌幅样式类
const getChangePctClass = (value) => {
  if (value === null || value === undefined) {
    return ''
  }
  if (value > 0) {
    return 'up'
  } else if (value < 0) {
    return 'down'
  }
  return 'flat'
}

// 格式化历史统计
const formatHistoricalStat = (stat) => {
  if (!stat) {
    return '--'
  }
  return `${stat.rise_days}/${stat.total_days} (${stat.rise_ratio}%)`
}

// 获取涨跌比例样式类
const getRatioClass = (stat) => {
  if (!stat) {
    return ''
  }
  if (stat.rise_ratio >= 70) {
    return 'strong-up'
  } else if (stat.rise_ratio >= 50) {
    return 'up'
  } else if (stat.rise_ratio >= 30) {
    return 'neutral'
  } else {
    return 'down'
  }
}

// 获取今日上涨率样式类
const getRiseRatioClass = (rise_ratio) => {
  if (rise_ratio === null || rise_ratio === undefined) {
    return ''
  }
  if (rise_ratio >= 70) {
    return 'strong-up'
  } else if (rise_ratio >= 50) {
    return 'up'
  } else if (rise_ratio >= 30) {
    return 'neutral'
  } else {
    return 'down'
  }
}

// 组件挂载时加载数据
onMounted(() => {
  loadSectors()
  loadStats()
})
</script>

<style scoped>
.sectors {
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
}

.header-content {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
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
}

.update-all-btn {
  padding: 8px 20px;
  background: linear-gradient(135deg, #ff9a56 0%, #ff6b35 100%);
  color: white;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  transition: all 0.3s;
}

.update-all-btn:hover:not(:disabled) {
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(255, 107, 53, 0.4);
}

.update-all-btn:disabled {
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

.sync-data-btn {
  padding: 8px 20px;
  background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
  color: white;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  transition: transform 0.3s;
}

.sync-data-btn:hover:not(:disabled) {
  transform: translateY(-2px);
}

.sync-data-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

/* 统计卡片 */
.stats-cards {
  display: flex;
  gap: 20px;
}

.stat-card {
  flex: 1;
  padding: 20px;
  border-radius: 12px;
  text-align: center;
  cursor: pointer;
  transition: all 0.3s;
  position: relative;
}

.stat-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
}

.stat-card.active {
  box-shadow: 0 0 0 3px rgba(255, 255, 255, 0.8), 0 4px 12px rgba(0, 0, 0, 0.2);
  transform: scale(1.02);
}

.stat-card.total {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
}

.stat-card.industry {
  background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
  color: white;
}

.stat-card.concept {
  background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
  color: white;
}

.stat-card.index {
  background: linear-gradient(135deg, #fa709a 0%, #fee140 100%);
  color: white;
}

.stat-value {
  font-size: 32px;
  font-weight: 700;
}

.stat-label {
  font-size: 14px;
  opacity: 0.9;
  margin-top: 4px;
}

/* 筛选栏 */
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

/* 板块网格 */
.sectors-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 16px;
  margin-bottom: 20px;
}

.sector-card {
  background: white;
  border-radius: 12px;
  padding: 20px;
  cursor: pointer;
  transition: all 0.3s;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
  border-left: 4px solid #667eea;
}

.sector-card:hover {
  transform: translateY(-4px);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
}

.sector-card.industry {
  border-left-color: #f5576c;
}

.sector-card.concept {
  border-left-color: #00f2fe;
}

.sector-card.index {
  border-left-color: #fee140;
}

.sector-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.sector-code {
  font-size: 13px;
  color: #718096;
  font-family: monospace;
}

.sector-type {
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 500;
}

.sector-change-pct {
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 13px;
  font-weight: 600;
}

.sector-change-pct.up {
  background: #fee2e2;
  color: #dc2626;
}

.sector-change-pct.down {
  background: #dcfce7;
  color: #16a34a;
}

.sector-change-pct.flat {
  background: #f3f4f6;
  color: #6b7280;
}

.sector-type.industry {
  background: #fce4ec;
  color: #c2185b;
}

.sector-type.concept {
  background: #e0f7fa;
  color: #00838f;
}

.sector-type.index {
  background: #fff8e1;
  color: #f57c00;
}

.sector-name-container {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.sector-name {
  font-size: 16px;
  font-weight: 600;
  color: #2d3748;
  flex: 1;
}

.sector-code {
  font-size: 13px;
  color: #718096;
  font-family: monospace;
  flex-shrink: 0;
}

.historical-stats {
  background: #f8fafc;
  border-radius: 8px;
  padding: 10px;
  margin-bottom: 12px;
}

.historical-stats .stat-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}

.historical-stats .stat-row:last-child {
  margin-bottom: 0;
}

.historical-stats .stat-label {
  font-size: 12px;
  color: #718096;
  font-weight: 500;
}

.historical-stats .stat-value {
  font-size: 12px;
  font-weight: 600;
}

.historical-stats .stat-value.strong-up {
  color: #dc2626;
  background: #fee2e2;
  padding: 2px 6px;
  border-radius: 4px;
}

.historical-stats .stat-value.up {
  color: #f97316;
}

.historical-stats .stat-value.neutral {
  color: #4b5563;
}

.historical-stats .stat-value.down {
  color: #16a34a;
}

.sector-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.stock-count {
  font-size: 13px;
  color: #718096;
}

/* 板块统计 */
.sector-stats {
  padding: 12px;
  background: #f8fafc;
  border-radius: 8px;
  margin-top: 8px;
}

/* 上涨下跌统计样式 */
.rise-count {
  color: #dc2626;
  font-weight: 600;
}

.fall-count {
  color: #16a34a;
  font-weight: 600;
}

.total-count {
  color: #4b5563;
  font-weight: 600;
}

.rise-ratio {
  color: #718096;
  font-size: 11px;
  margin-left: 4px;
}

.sector-stats .stat-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}

.sector-stats .stat-row:last-child {
  margin-bottom: 0;
}

.sector-stats .stat-label {
  font-size: 12px;
  color: #718096;
}

.sector-stats .stat-value {
  font-size: 13px;
  font-weight: 600;
  color: #2d3748;
}

.sector-stats .stat-value.highlight {
  color: #e53e3e;
  font-weight: 700;
}

/* 加载和空状态 */
.loading,
.empty {
  text-align: center;
  padding: 60px 20px;
  background: white;
  border-radius: 12px;
  color: #718096;
  font-size: 16px;
}

.empty .hint {
  font-size: 14px;
  color: #a0aec0;
  margin-top: 8px;
}

/* 分页 */
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

/* 弹窗 */
.modal-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.modal-content {
  background: white;
  border-radius: 16px;
  width: 90%;
  max-width: 600px;
  max-height: 80vh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.modal-header {
  padding: 20px;
  border-bottom: 1px solid #e2e8f0;
  display: flex;
  align-items: center;
  gap: 12px;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
  flex: 1;
  justify-content: flex-end;
}

.update-kline-btn {
  padding: 6px 16px;
  background: linear-gradient(135deg, #ff6b6b 0%, #ee5a6f 100%);
  color: white;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 500;
  transition: all 0.3s;
  white-space: nowrap;
}

.update-kline-btn:hover:not(:disabled) {
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(238, 90, 111, 0.4);
}

.update-kline-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.modal-header h3 {
  flex: 1;
  margin: 0;
  font-size: 20px;
  color: #2d3748;
}

.sector-type-badge {
  padding: 4px 12px;
  border-radius: 6px;
  font-size: 13px;
  font-weight: 500;
}

.sector-type-badge.industry {
  background: #fce4ec;
  color: #c2185b;
}

.sector-type-badge.concept {
  background: #e0f7fa;
  color: #00838f;
}

.close-btn {
  background: none;
  border: none;
  font-size: 24px;
  cursor: pointer;
  color: #718096;
  padding: 0 8px;
}

.close-btn:hover {
  color: #2d3748;
}

.modal-body {
  padding: 20px;
  overflow-y: auto;
  flex: 1;
}

.detail-info {
  display: flex;
  gap: 20px;
  margin-bottom: 20px;
  color: #718096;
  font-size: 14px;
}

.stocks-header {
  font-weight: 600;
  color: #2d3748;
  margin-bottom: 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid #e2e8f0;
}

.stock-item {
  display: flex;
  align-items: center;
  padding: 10px 0;
  border-bottom: 1px solid #f7fafc;
}

.stock-item:last-child {
  border-bottom: none;
}

.stock-code {
  width: 80px;
  font-family: monospace;
  color: #667eea;
  font-size: 13px;
}

.stock-name {
  flex: 1;
  color: #2d3748;
}

.view-btn {
  padding: 4px 12px;
  background: #667eea;
  color: white;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
}

.view-btn:hover {
  background: #5a67d8;
}

.no-stocks {
  text-align: center;
  color: #718096;
  padding: 40px 20px;
}
</style>