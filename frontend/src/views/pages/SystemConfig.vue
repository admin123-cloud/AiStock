<template>
  <div class="system-config">
    <div class="config-header">
      <h1>系统配置</h1>
      <p>自动维护为主，手动修复兜底</p>
    </div>

    <section class="health-strip">
      <div class="health-item">
        <span class="health-label">核心状态</span>
        <strong :class="`state-${coreMaintenance.overall_status || 'idle'}`">
          {{ maintenanceStatusText }}
        </strong>
      </div>
      <div class="health-item">
        <span class="health-label">今日成功率</span>
        <strong>{{ formatRate(coreMaintenance.today_success_rate) }}</strong>
      </div>
      <div class="health-item">
        <span class="health-label">运行中任务</span>
        <strong>{{ runningTaskText }}</strong>
      </div>
      <div class="health-item">
        <span class="health-label">最近失败任务</span>
        <strong>{{ latestFailedText }}</strong>
      </div>
      <div class="health-item">
        <span class="health-label">最近刷新</span>
        <strong>{{ formatDateTime(lastStatusRefreshAt) }}</strong>
      </div>
    </section>

    <section class="section-card">
      <div class="section-title-row">
        <h2>Auto 核心数据自动维护</h2>
        <span class="status-pill" :class="coreMaintenance.enabled ? 'enabled' : 'paused'">
          {{ coreMaintenance.enabled ? '已启用' : '已暂停' }}
        </span>
      </div>

      <div class="task-table">
        <div class="task-row head">
          <span>任务</span>
          <span>状态</span>
          <span>说明</span>
          <span>最近耗时</span>
          <span>近10次均耗</span>
          <span>最近来源</span>
          <span>下次执行</span>
          <span>最近成功</span>
          <span>操作</span>
        </div>
        <div v-for="task in maintenanceTasks" :key="task.key" class="task-row">
          <span>{{ task.label }}</span>
          <span :class="`state-${getTaskStatus(task.key)}`">{{ getTaskStatusText(task.key) }}</span>
          <span>{{ getTaskMessage(task.key) }}</span>
          <span>{{ getTaskLastDuration(task.key) }}</span>
          <span>{{ getTaskAvgDuration(task.key) }}</span>
          <span>{{ getTaskTriggerSource(task.key) }}</span>
          <span>{{ getNextRunTime(task.key) }}</span>
          <span>{{ getTaskLastSuccess(task.key) }}</span>
          <span class="task-action-cell">
            <button
              class="btn task-sync-btn"
              :disabled="isMaintenanceTaskActionDisabled(task.key)"
              @click="syncMaintenanceTask(task)"
            >
              {{ isMaintenanceTaskSyncing(task.key) ? '同步中...' : '同步' }}
            </button>
          </span>
        </div>
      </div>

      <div class="maintenance-actions">
        <button class="btn primary" @click="toggleCoreMaintenance">
          {{ coreMaintenance.enabled ? '暂停自动维护' : '启动自动维护' }}
        </button>
        <button
          class="btn success"
          :disabled="runningCoreAssetsSyncNow"
          @click="syncCoreAssetsNow"
        >
          {{ runningCoreAssetsSyncNow ? '核心同步执行中...' : '立即同步核心数据' }}
        </button>
        <button
          class="btn warning"
          :disabled="runningOfficialDailyNow || isTaskRunning('official_daily')"
          @click="triggerOfficialDailyCloseNow"
        >
          {{ runningOfficialDailyNow || isTaskRunning('official_daily') ? '盘后正式日线执行中...' : '立即执行盘后正式日线' }}
        </button>
        <button
          class="btn danger"
          :disabled="runningMinuteKlineRepairNow || isTaskRunning('minute_kline_daily_repair_validate')"
          @click="triggerMinuteKlineRepairNow"
        >
          {{ runningMinuteKlineRepairNow || isTaskRunning('minute_kline_daily_repair_validate') ? '分钟数据重同步中...' : '一键重新同步分钟数据' }}
        </button>
        <button
          class="btn danger"
          :disabled="runningMarketMinuteHistoryRepairNow || getTaskRaw('market_minute_history_repair')?.is_running"
          @click="triggerMarketMinuteHistoryRepairNow"
        >
          {{ runningMarketMinuteHistoryRepairNow || getTaskRaw('market_minute_history_repair')?.is_running ? '历史分钟修复中...' : '股票/指数历史分钟修复' }}
        </button>
        <button class="btn" @click="refreshCoreMaintenanceStatus">刷新状态</button>
        <button class="btn ghost" @click="toggleTimelinePanel">
          {{ showTimelinePanel ? '收起任务时间线' : '查看任务时间线' }}
        </button>
      </div>

      <div v-if="showTimelinePanel" class="timeline-panel">
        <div class="timeline-title">最近任务时间线</div>
        <div v-if="!coreMaintenance.timeline?.length" class="timeline-empty">暂无任务记录</div>
        <div v-for="(item, idx) in coreMaintenance.timeline" :key="`${item.task_name}-${item.created_at}-${idx}`" class="timeline-item">
          <span class="timeline-name">{{ getTaskAlias(item.task_name) }}</span>
          <span :class="`state-${item.status || 'idle'}`">{{ getStateText(item.status) }}</span>
          <span class="timeline-msg">{{ item.message || '-' }}</span>
          <span class="timeline-duration">{{ formatDuration(item.duration_sec) }}</span>
          <span class="timeline-time">{{ formatDateTime(item.created_at) }}</span>
        </div>
      </div>
    </section>

    <section class="section-card">
      <div class="section-title-row">
        <h2>启动初始化基础数据</h2>
        <span class="status-pill" :class="startupReferenceSyncEnabled ? 'enabled' : 'paused'">
          {{ startupReferenceSyncEnabled ? '启动时开启' : '启动时关闭' }}
        </span>
      </div>
      <p class="section-tip">
        控制项目启动时是否自动更新交易日历、股票列表、指数列表、板块列表和板块成分股。
        调试阶段建议关闭，线上运行建议开启。修改后需重启后端生效。
      </p>
      <div class="maintenance-actions">
        <button class="btn primary" :disabled="startupReferenceSyncSaving" @click="toggleStartupReferenceSync">
          {{ startupReferenceSyncSaving ? '保存中...' : (startupReferenceSyncEnabled ? '关闭启动初始化' : '开启启动初始化') }}
        </button>
        <button class="btn" :disabled="startupReferenceSyncSaving" @click="loadStartupReferenceSyncSetting">刷新状态</button>
      </div>
      <div class="config-note">
        当前默认：{{ startupReferenceSyncDefaultEnabled ? '线上模式默认开启' : '调试模式默认关闭' }}
      </div>
    </section>

    <section class="section-card">
      <div class="section-title-row">
        <h2>高级维护（手动兜底）</h2>
        <button class="btn ghost" @click="advancedOpen = !advancedOpen">
          {{ advancedOpen ? '折叠' : '展开' }}
        </button>
      </div>
      <p class="section-tip">低频维护、重算、初始化任务放在这里。自动维护异常时再使用手工触发。</p>

      <div v-show="advancedOpen" class="advanced-grid">
        <article class="manual-card">
          <h3>股票数据</h3>
          <p>股票列表、日线修复、全历史修复、盘中股票快照手动触发。</p>
          <div class="tag-row">
            <span class="tag">初始化</span>
            <span class="tag">补数</span>
            <span class="tag">重算</span>
          </div>
          <div class="button-row">
            <button class="btn" :disabled="loading" @click="updateStockList">{{ loading ? '执行中...' : '更新股票列表' }}</button>
            <button class="btn" :disabled="repairingDailyKlines" @click="repairDailyKlinesData">{{ repairingDailyKlines ? '执行中...' : '修复日线' }}</button>
            <button class="btn danger" :disabled="repairingAllHistoryKlines" @click="repairAllHistoryKlines">{{ repairingAllHistoryKlines ? '执行中...' : '修复全历史K线' }}</button>
          </div>
          <div class="period-box">
            <span>盘中股票快照周期：</span>
            <label v-for="period in klinePeriods" :key="`stock-${period.value}`">
              <input type="checkbox" v-model="selectedStockUpdatePeriods" :value="period.value" />
              {{ period.label }}
            </label>
          </div>
          <button class="btn success" :disabled="updatingStockTodayData || selectedStockUpdatePeriods.length === 0" @click="updateStockTodayData">
            {{ updatingStockTodayData ? '执行中...' : '手动更新当天股票数据' }}
          </button>
        </article>

        <article class="manual-card">
          <h3>指数与日历</h3>
          <p>指数列表、历史K线、交易日历和盘中指数快照。</p>
          <div class="tag-row">
            <span class="tag">初始化</span>
            <span class="tag">补数</span>
          </div>
          <div class="button-row">
            <button class="btn" :disabled="updatingIndices" @click="updateIndices">{{ updatingIndices ? '执行中...' : '更新指数列表' }}</button>
            <button class="btn" :disabled="updatingTradeCalendar" @click="updateTradeCalendar">{{ updatingTradeCalendar ? '执行中...' : '更新交易日历' }}</button>
          </div>
          <div class="period-box">
            <span>指数历史K线周期：</span>
            <label v-for="period in klinePeriods" :key="`history-${period.value}`">
              <input type="checkbox" v-model="selectedPeriods" :value="period.value" />
              {{ period.label }}
            </label>
          </div>
          <button class="btn" :disabled="repairingHistoryKlines || selectedPeriods.length === 0" @click="repairHistoryKlines">
            {{ repairingHistoryKlines ? '执行中...' : '同步指数全量历史K线' }}
          </button>
          <div class="period-box">
            <span>盘中指数快照周期：</span>
            <label v-for="period in klinePeriods" :key="`index-${period.value}`">
              <input type="checkbox" v-model="selectedUpdatePeriods" :value="period.value" />
              {{ period.label }}
            </label>
          </div>
          <button class="btn success" :disabled="updatingTodayData || selectedUpdatePeriods.length === 0" @click="updateTodayData">
            {{ updatingTodayData ? '执行中...' : '手动更新当天指数数据' }}
          </button>
        </article>

        <article class="manual-card">
          <h3>板块数据</h3>
          <p>一级、二级、三级板块列表与板块历史统计补齐。</p>
          <div class="tag-row">
            <span class="tag">参考数据</span>
            <span class="tag">补数</span>
          </div>
          <div class="button-row">
            <button class="btn" :disabled="syncingSectors" @click="syncSectors">{{ syncingSectors ? '执行中...' : '同步一级二级三级板块' }}</button>
            <button class="btn success" :disabled="updatingSectorIntradayStats" @click="updateSectorIntradayStats">{{ updatingSectorIntradayStats ? '执行中...' : '刷新板块当日统计' }}</button>
          </div>
          <div class="days-box">
            <label>板块历史同步天数</label>
            <input type="number" v-model.number="syncHistoryDays" min="1" max="365" />
          </div>
          <button class="btn" :disabled="syncingSectorHistory" @click="syncSectorHistory">{{ syncingSectorHistory ? '执行中...' : '同步板块历史涨跌' }}</button>
        </article>

        <article class="manual-card">
          <h3>情绪周期</h3>
          <p>情绪周期重算与盘中自动修复开关。</p>
          <div class="tag-row">
            <span class="tag">重算</span>
            <span class="tag">盘中修复</span>
          </div>
          <div class="button-row">
            <button class="btn" :disabled="repairingEmotion30d" @click="repairEmotion30d">{{ repairingEmotion30d ? '执行中...' : '修复近30天情绪' }}</button>
            <button class="btn" :disabled="repairingEmotionLatest" @click="repairEmotionLatest">{{ repairingEmotionLatest ? '执行中...' : '更新最新情绪' }}</button>
          </div>
          <button class="btn" :class="emotionAutoEnabled ? 'warning' : 'success'" @click="toggleEmotionAuto5m">
            {{ emotionAutoEnabled ? '关闭盘中每5分钟自动修复' : '开启盘中每5分钟自动修复' }}
          </button>
        </article>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import axios from 'axios'
import { ElMessage } from 'element-plus'

const API_BASE = import.meta.env.VITE_API_BASE || '/api'

const loading = ref(false)
const updatingIndices = ref(false)
const syncingSectors = ref(false)
const syncingSectorHistory = ref(false)
const updatingSectorIntradayStats = ref(false)
const repairingHistoryKlines = ref(false)
const repairingDailyKlines = ref(false)
const repairingAllHistoryKlines = ref(false)
const updatingTodayData = ref(false)
const updatingStockTodayData = ref(false)
const updatingTradeCalendar = ref(false)
const repairingEmotion30d = ref(false)
const repairingEmotionLatest = ref(false)
const emotionAutoEnabled = ref(false)
const runningOfficialDailyNow = ref(false)
const runningCoreAssetsSyncNow = ref(false)
const runningMinuteKlineRepairNow = ref(false)
const runningMarketMinuteHistoryRepairNow = ref(false)

const advancedOpen = ref(false)
const showTimelinePanel = ref(false)
const lastStatusRefreshAt = ref(null)
const coreMaintenanceRefreshTimer = ref(null)
const startupReferenceSyncEnabled = ref(false)
const startupReferenceSyncDefaultEnabled = ref(false)
const startupReferenceSyncSaving = ref(false)
const maintenanceTaskSyncing = ref({})
const syncHistoryDays = ref(30)
const klinePeriods = ref([
  { value: '1m', label: '1分钟' },
  { value: '5m', label: '5分钟' },
  { value: '15m', label: '15分钟' },
  { value: '30m', label: '30分钟' },
  { value: '60m', label: '60分钟' },
  { value: '1d', label: '日线' },
  { value: '1w', label: '周线' },
  { value: '1mon', label: '月线' },
  { value: '1q', label: '季线' },
  { value: '1y', label: '年线' }
])
const selectedPeriods = ref(['1d', '1w', '1mon'])
const selectedUpdatePeriods = ref(['5m', '15m', '30m', '60m', '1d'])
const selectedStockUpdatePeriods = ref(['5m', '15m', '30m', '60m', '1d'])

const maintenanceTasks = [
  { key: 'trade_calendar', label: '交易日历同步' },
  { key: 'stock_list_sync', label: '股票列表同步' },
  { key: 'index_list_sync', label: '指数列表同步' },
  { key: 'sector_list_sync', label: '板块列表同步' },
  { key: 'stock_intraday', label: '盘中股票/指数日线快照' },
  { key: 'market_intraday_minutes', label: '盘中股票/指数分钟级快照' },
  { key: 'minute_kline_daily_repair_validate', label: '最近2日分钟K线补全验证' },
  { key: 'sector_intraday_stats_refresh', label: '板块当日统计刷新' },
  { key: 'official_daily', label: '盘后正式日线写库' },
  { key: 'repair_daily', label: '次日自动巡检修复' }
]

const maintenanceTaskNameMap = {
  trade_calendar: 'update_trade_calendar',
  stock_list_sync: 'update_stock_list',
  index_list_sync: 'update_index_list',
  sector_list_sync: 'sync_sectors',
  stock_intraday: 'update_stock_today_data',
  market_intraday_minutes: 'update_market_today_minute_data',
  minute_kline_daily_repair_validate: 'minute_kline_daily_repair_validate',
  sector_intraday_stats_refresh: 'update_sector_intraday_stats',
  official_daily: 'official_daily_close_sync',
  repair_daily: 'repair_previous_daily_kline'
}

const coreMaintenance = ref({
  enabled: false,
  overall_status: 'idle',
  today_success_rate: 100,
  latest_failed_task: null,
  running_task: null,
  jobs: {},
  task_status: {},
  task_status_normalized: {},
  timeline: []
})

const normalizeCoreMaintenance = (payload = {}) => {
  return {
    enabled: !!payload.enabled || !!payload.is_enabled,
    overall_status: payload.overall_status || 'idle',
    today_success_rate: payload.today_success_rate ?? 100,
    latest_failed_task: payload.latest_failed_task || null,
    latest_failed_at: payload.latest_failed_at || null,
    running_task: payload.running_task || null,
    jobs: payload.jobs || {},
    task_status: payload.task_status || {},
    task_status_normalized: payload.task_status_normalized || {},
    timeline: payload.timeline || []
  }
}

const formatDateTime = (value) => {
  if (!value) return '-'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return String(value)
  return d.toLocaleString('zh-CN', { hour12: false })
}

const formatRate = (value) => `${Number(value || 0).toFixed(2)}%`
const formatDuration = (valueSec) => {
  const sec = Number(valueSec)
  if (!Number.isFinite(sec) || sec < 0) return '-'
  if (sec < 1) return `${Math.round(sec * 1000)}ms`
  if (sec < 60) return `${sec.toFixed(2)}s`
  const min = Math.floor(sec / 60)
  const remain = sec - min * 60
  return `${min}m ${remain.toFixed(1)}s`
}

const getTaskState = (taskKey) => {
  return coreMaintenance.value.task_status_normalized?.[taskKey] || {}
}

const getTaskRaw = (taskKey) => {
  return coreMaintenance.value.task_status?.[taskKey] || {}
}

const getTaskStatus = (taskKey) => {
  const status = getTaskState(taskKey).status
  if (status) return status
  const raw = getTaskRaw(taskKey)
  if (raw?.is_running) return 'running'
  if (raw?.error) return 'failed'
  if (raw?.results) return 'success'
  const scheduled = coreMaintenance.value.jobs?.[taskKey]?.enabled
  return scheduled ? 'idle' : 'paused'
}

const getTaskStatusText = (taskKey) => getStateText(getTaskStatus(taskKey))

const getTaskMessage = (taskKey) => {
  const state = getTaskState(taskKey)
  if (state?.display_message) return state.display_message
  const task = getTaskRaw(taskKey)
  if (task?.progress?.message) return task.progress.message
  if (task?.error) return task.error
  if (task?.results?.message) return task.results.message
  return coreMaintenance.value.jobs?.[taskKey]?.enabled ? '已调度，等待执行' : '未调度'
}

const getTaskLastSuccess = (taskKey) => {
  const val = getTaskState(taskKey).last_success_at || getTaskRaw(taskKey).last_success_at
  return formatDateTime(val)
}

const getTaskLastDuration = (taskKey) => {
  const value = getTaskState(taskKey).last_duration_sec
  return formatDuration(value)
}

const getTaskAvgDuration = (taskKey) => {
  const state = getTaskState(taskKey)
  const avg = state.recent_avg_duration_sec
  const count = Number(state.recent_run_count || 0)
  if (avg === null || avg === undefined || count <= 0) return '-'
  return `${formatDuration(avg)} (${count}次)`
}

const getTriggerSourceText = (source) => {
  const map = {
    auto: '自动定时',
    manual: '手动触发',
    startup: '启动快刷'
  }
  return map[source] || '-'
}

const getTaskTriggerSource = (taskKey) => {
  const source = getTaskState(taskKey).trigger_source || getTaskRaw(taskKey).trigger_source
  return getTriggerSourceText(source)
}

const getNextRunTime = (taskKey) => formatDateTime(coreMaintenance.value.jobs?.[taskKey]?.next_run_time)

const isTaskRunning = (taskKey) => !!getTaskRaw(taskKey)?.is_running
const isMaintenanceTaskSyncing = (taskKey) => !!maintenanceTaskSyncing.value[taskKey]
const isMaintenanceTaskActionDisabled = (taskKey) => isTaskRunning(taskKey) || isMaintenanceTaskSyncing(taskKey)

const getStateText = (status) => {
  const map = {
    running: '运行中',
    success: '成功',
    failed: '失败',
    paused: '已暂停',
    idle: '待执行'
  }
  return map[status] || '待执行'
}

const taskAliasMap = {
  update_trade_calendar: '交易日历同步',
  update_stock_list: '股票列表同步',
  update_index_list: '指数列表同步',
  sync_sectors: '板块列表同步',
  update_stock_today_data: '盘中股票/指数日线快照',
  update_today_data: '盘中指数分钟级快照',
  update_market_today_minute_data: '盘中股票/指数分钟级快照',
  minute_kline_daily_repair_validate: '分钟K线补全验证',
  market_minute_history_repair: '股票/指数历史分钟级修复',
  update_sector_intraday_stats: '板块当日统计刷新',
  official_daily_close_sync: '盘后正式日线写库',
  repair_previous_daily_kline: '次日自动巡检修复',
  core_data_maintenance: '核心数据自动维护'
}

const getTaskAlias = (taskName) => taskAliasMap[taskName] || taskName

const maintenanceStatusText = computed(() => getStateText(coreMaintenance.value.overall_status))
const runningTaskText = computed(() => {
  if (!coreMaintenance.value.running_task) return '-'
  return maintenanceTasks.find((item) => item.key === coreMaintenance.value.running_task)?.label || coreMaintenance.value.running_task
})
const latestFailedText = computed(() => {
  if (!coreMaintenance.value.latest_failed_task) return '-'
  return maintenanceTasks.find((item) => item.key === coreMaintenance.value.latest_failed_task)?.label || coreMaintenance.value.latest_failed_task
})

const refreshCoreMaintenanceStatus = async () => {
  try {
    const response = await axios.get(`${API_BASE}/system/core-data-maintenance/status`)
    coreMaintenance.value = normalizeCoreMaintenance(response.data?.data || {})
    lastStatusRefreshAt.value = new Date().toISOString()
  } catch (error) {
    ElMessage.error('获取核心自动维护状态失败')
  }
}

const loadStartupReferenceSyncSetting = async () => {
  try {
    const response = await axios.get(`${API_BASE}/system/startup-reference-sync-setting`)
    const data = response.data?.data || {}
    startupReferenceSyncEnabled.value = !!data.enabled
    startupReferenceSyncDefaultEnabled.value = !!data.default_enabled
  } catch (error) {
    ElMessage.error('获取启动初始化配置失败')
  }
}

const refreshTimeline = async () => {
  try {
    const response = await axios.get(`${API_BASE}/system/core-data-maintenance/timeline?limit=30`)
    if (response.data?.success) {
      coreMaintenance.value.timeline = response.data.data?.timeline || []
    }
  } catch (error) {
    ElMessage.error('获取任务时间线失败')
  }
}

const toggleTimelinePanel = async () => {
  showTimelinePanel.value = !showTimelinePanel.value
  if (showTimelinePanel.value) {
    await refreshTimeline()
  }
}

const toggleCoreMaintenance = async () => {
  try {
    if (coreMaintenance.value.enabled) {
      await axios.post(`${API_BASE}/system/core-data-maintenance/stop`)
      ElMessage.success('核心数据自动维护已暂停')
    } else {
      await axios.post(`${API_BASE}/system/core-data-maintenance/start`)
      ElMessage.success('核心数据自动维护已启动')
    }
    await refreshCoreMaintenanceStatus()
    if (showTimelinePanel.value) await refreshTimeline()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || error.message || '操作失败')
  }
}

const toggleStartupReferenceSync = async () => {
  startupReferenceSyncSaving.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/startup-reference-sync-setting`, {
      enabled: !startupReferenceSyncEnabled.value
    })
    if (response.data?.success) {
      startupReferenceSyncEnabled.value = !!response.data.data?.enabled
      startupReferenceSyncDefaultEnabled.value = !!response.data.data?.default_enabled
      ElMessage.success(response.data?.message || '启动初始化配置已更新')
    } else {
      ElMessage.error('保存失败')
    }
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || error.message || '保存失败')
  } finally {
    startupReferenceSyncSaving.value = false
  }
}

const pollTaskStatus = async (taskName, onDone) => {
  try {
    const response = await axios.get(`${API_BASE}/system/task-status/${taskName}`)
    const task = response.data?.task || {}
    if (task.is_running) {
      setTimeout(() => pollTaskStatus(taskName, onDone), 3000)
      return
    }
    if (onDone) onDone(task)
    if (task.results) {
      ElMessage.success(task.results.message || '任务完成')
    } else if (task.error) {
      ElMessage.error(`任务失败: ${task.error}`)
    } else {
      ElMessage.info('任务已完成')
    }
    await refreshCoreMaintenanceStatus()
    if (showTimelinePanel.value) await refreshTimeline()
  } catch (error) {
    if (onDone) onDone()
  }
}

const triggerOfficialDailyCloseNow = async () => {
  if (!confirm('确认立即执行盘后正式日线写库吗？')) return
  runningOfficialDailyNow.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/core-data-maintenance/run-official-daily-close`)
    if (response.data?.success) {
      ElMessage.success('盘后正式日线任务已启动')
      pollTaskStatus('official_daily_close_sync', () => {
        runningOfficialDailyNow.value = false
      })
    } else {
      runningOfficialDailyNow.value = false
      ElMessage.error(response.data?.message || '启动失败')
    }
  } catch (error) {
    runningOfficialDailyNow.value = false
    ElMessage.error(error.response?.data?.detail || error.message || '启动失败')
  }
}

const triggerMinuteKlineRepairNow = async () => {
  if (!confirm('确认重新同步最近2个交易日的5m/15m/30m/60m分钟K线吗？')) return
  runningMinuteKlineRepairNow.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/core-data-maintenance/run-task/minute_kline_daily_repair_validate`)
    if (response.data?.success) {
      ElMessage.success(response.data?.message || '分钟数据重同步任务已启动')
      pollTaskStatus(response.data?.task_name || 'minute_kline_daily_repair_validate', () => {
        runningMinuteKlineRepairNow.value = false
      })
      return
    }
    runningMinuteKlineRepairNow.value = false
    ElMessage.error(response.data?.message || '启动失败')
  } catch (error) {
    runningMinuteKlineRepairNow.value = false
    ElMessage.error(error.response?.data?.detail || error.message || '启动失败')
  }
}

const triggerMarketMinuteHistoryRepairNow = async () => {
  if (!confirm('确认修复最近30个交易日的股票/指数5m/15m/30m/60m历史分钟K线吗？')) return
  runningMarketMinuteHistoryRepairNow.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/core-data-maintenance/run-task/market_minute_history_repair`)
    if (response.data?.success) {
      ElMessage.success(response.data?.message || '股票/指数历史分钟修复任务已启动')
      pollTaskStatus(response.data?.task_name || 'market_minute_history_repair', () => {
        runningMarketMinuteHistoryRepairNow.value = false
      })
      return
    }
    runningMarketMinuteHistoryRepairNow.value = false
    ElMessage.error(response.data?.message || '启动失败')
  } catch (error) {
    runningMarketMinuteHistoryRepairNow.value = false
    ElMessage.error(error.response?.data?.detail || error.message || '启动失败')
  }
}

const syncMaintenanceTask = async (task) => {
  const backendTaskName = maintenanceTaskNameMap[task.key]
  if (!backendTaskName) {
    ElMessage.error('未配置对应的同步任务')
    return
  }

  maintenanceTaskSyncing.value = {
    ...maintenanceTaskSyncing.value,
    [task.key]: true
  }

  try {
    const response = await axios.post(`${API_BASE}/system/core-data-maintenance/run-task/${task.key}`)
    if (response.data?.success) {
      ElMessage.success(response.data?.message || '任务已启动')
      pollTaskStatus(response.data?.task_name || backendTaskName, () => {
        maintenanceTaskSyncing.value = {
          ...maintenanceTaskSyncing.value,
          [task.key]: false
        }
      })
      return
    }

    maintenanceTaskSyncing.value = {
      ...maintenanceTaskSyncing.value,
      [task.key]: false
    }
    ElMessage.error(response.data?.message || '启动失败')
  } catch (error) {
    maintenanceTaskSyncing.value = {
      ...maintenanceTaskSyncing.value,
      [task.key]: false
    }
    ElMessage.error(error.response?.data?.detail || error.message || '启动失败')
  }
}

const syncCoreAssetsNow = async () => {
  runningCoreAssetsSyncNow.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/core-data-maintenance/sync-core-assets`)
    if (response.data?.success) {
      ElMessage.success(response.data?.message || '核心数据同步任务已启动')
      pollTaskStatus(response.data?.task_name || 'core_data_manual_sync', () => {
        runningCoreAssetsSyncNow.value = false
      })
      return
    }
    runningCoreAssetsSyncNow.value = false
    ElMessage.error(response.data?.message || '启动失败')
  } catch (error) {
    runningCoreAssetsSyncNow.value = false
    ElMessage.error(error.response?.data?.detail || error.message || '启动失败')
  }
}

const updateStockList = async () => {
  loading.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/update-stock-list`)
    if (response.data?.success) {
      ElMessage.success('股票列表更新任务已启动')
      pollTaskStatus('update_stock_list', () => { loading.value = false })
      return
    }
    loading.value = false
    ElMessage.error('启动失败')
  } catch (error) {
    loading.value = false
    ElMessage.error('股票列表更新失败')
  }
}

const updateIndices = async () => {
  updatingIndices.value = true
  try {
    const response = await axios.post(`${API_BASE}/stocks/update-indices`)
    if (response.data?.success > 0) {
      ElMessage.success(`成功更新 ${response.data.success} 个指数`)
    } else {
      ElMessage.error(`更新指数列表失败: ${response.data?.error || '无数据'}`)
    }
  } catch (error) {
    ElMessage.error(`更新指数列表失败: ${error.response?.data?.detail || error.message}`)
  } finally {
    updatingIndices.value = false
  }
}

const syncSectors = async () => {
  if (!confirm('确认同步一二三级行业板块数据吗？')) return
  syncingSectors.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/sync-sectors`)
    if (response.data?.success) {
      ElMessage.success('行业板块同步任务已启动')
      pollTaskStatus('sync_sectors', () => { syncingSectors.value = false })
    } else {
      syncingSectors.value = false
      ElMessage.error('启动失败')
    }
  } catch (error) {
    syncingSectors.value = false
    ElMessage.error(`启动失败: ${error.response?.data?.detail || error.message}`)
  }
}

const syncSectorHistory = async () => {
  if (!confirm(`确认同步近 ${syncHistoryDays.value} 天板块历史涨跌数据吗？`)) return
  syncingSectorHistory.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/sync-sector-history?days=${syncHistoryDays.value}`)
    if (response.data?.success) {
      ElMessage.success('板块历史同步任务已启动')
      pollTaskStatus('sync_sector_history', () => { syncingSectorHistory.value = false })
    } else {
      syncingSectorHistory.value = false
      ElMessage.error('启动失败')
    }
  } catch (error) {
    syncingSectorHistory.value = false
    ElMessage.error(`启动失败: ${error.response?.data?.detail || error.message}`)
  }
}

const updateSectorIntradayStats = async () => {
  if (!confirm('确认刷新板块当日成分涨跌统计吗？')) return
  updatingSectorIntradayStats.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/update-sector-intraday-stats`)
    if (response.data?.success) {
      ElMessage.success('板块当日统计刷新任务已启动')
      pollTaskStatus('update_sector_intraday_stats', () => { updatingSectorIntradayStats.value = false })
    } else {
      updatingSectorIntradayStats.value = false
      ElMessage.error('启动失败')
    }
  } catch (error) {
    updatingSectorIntradayStats.value = false
    ElMessage.error(`启动失败: ${error.response?.data?.detail || error.message}`)
  }
}

const repairHistoryKlines = async () => {
  if (!selectedPeriods.value.length) {
    ElMessage.warning('请至少选择一个K线周期')
    return
  }
  if (!confirm('确认同步指数全量历史K线吗？')) return
  repairingHistoryKlines.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/repair-history-klines`, { periods: selectedPeriods.value, type: 'index' })
    if (response.data?.success) {
      ElMessage.success('历史K线同步任务已启动')
      pollTaskStatus('repair_history_klines', () => { repairingHistoryKlines.value = false })
    } else {
      repairingHistoryKlines.value = false
      ElMessage.error('启动失败')
    }
  } catch (error) {
    repairingHistoryKlines.value = false
    ElMessage.error(`启动失败: ${error.response?.data?.detail || error.message}`)
  }
}

const repairDailyKlinesData = async () => {
  if (!confirm('确认修复日K线数据吗？')) return
  repairingDailyKlines.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/repair-daily-klines`)
    if (response.data?.success) {
      ElMessage.success('日K线修复任务已启动')
      pollTaskStatus('repair_daily_klines', () => { repairingDailyKlines.value = false })
    } else {
      repairingDailyKlines.value = false
      ElMessage.error('启动失败')
    }
  } catch (error) {
    repairingDailyKlines.value = false
    ElMessage.error(`启动失败: ${error.response?.data?.detail || error.message}`)
  }
}

const repairAllHistoryKlines = async () => {
  if (!confirm('确认修复全量历史K线吗？该任务耗时较长。')) return
  repairingAllHistoryKlines.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/repair-all-history-klines`)
    if (response.data?.success) {
      ElMessage.success('全量历史K线修复任务已启动')
      pollTaskStatus('repair_all_history_klines', () => { repairingAllHistoryKlines.value = false })
    } else {
      repairingAllHistoryKlines.value = false
      ElMessage.error('启动失败')
    }
  } catch (error) {
    repairingAllHistoryKlines.value = false
    ElMessage.error(`启动失败: ${error.response?.data?.detail || error.message}`)
  }
}

const updateTradeCalendar = async () => {
  if (!confirm('确认更新交易日历吗？')) return
  updatingTradeCalendar.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/update-trade-calendar`)
    if (response.data?.success) {
      ElMessage.success('交易日历更新任务已启动')
      pollTaskStatus('update_trade_calendar', () => { updatingTradeCalendar.value = false })
    } else {
      updatingTradeCalendar.value = false
      ElMessage.error('启动失败')
    }
  } catch (error) {
    updatingTradeCalendar.value = false
    ElMessage.error(`启动失败: ${error.response?.data?.detail || error.message}`)
  }
}

const updateTodayData = async () => {
  if (!selectedUpdatePeriods.value.length) {
    ElMessage.warning('请至少选择一个K线周期')
    return
  }
  if (!confirm('确认更新当天指数数据吗？')) return
  updatingTodayData.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/update-today-data`, { periods: selectedUpdatePeriods.value })
    if (response.data?.success) {
      ElMessage.success('当天指数数据更新任务已启动')
      pollTaskStatus('update_today_data', () => { updatingTodayData.value = false })
    } else {
      updatingTodayData.value = false
      ElMessage.error('启动失败')
    }
  } catch (error) {
    updatingTodayData.value = false
    ElMessage.error(`启动失败: ${error.response?.data?.detail || error.message}`)
  }
}

const updateStockTodayData = async () => {
  if (!selectedStockUpdatePeriods.value.length) {
    ElMessage.warning('请至少选择一个K线周期')
    return
  }
  if (!confirm('确认更新当天股票数据吗？')) return
  updatingStockTodayData.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/update-stock-today-data`, { periods: selectedStockUpdatePeriods.value })
    if (response.data?.success) {
      ElMessage.success('当天股票数据更新任务已启动')
      pollTaskStatus('update_stock_today_data', () => { updatingStockTodayData.value = false })
    } else {
      updatingStockTodayData.value = false
      ElMessage.error('启动失败')
    }
  } catch (error) {
    updatingStockTodayData.value = false
    ElMessage.error(`启动失败: ${error.response?.data?.detail || error.message}`)
  }
}

const repairEmotion30d = async () => {
  if (!confirm('确认修复近30天情绪周期数据吗？')) return
  repairingEmotion30d.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/repair-emotion-cycle-30d?days=30`)
    if (response.data?.success) {
      ElMessage.success('情绪周期30天修复任务已启动')
      pollTaskStatus('repair_emotion_cycle_30d', () => { repairingEmotion30d.value = false })
    } else {
      repairingEmotion30d.value = false
      ElMessage.error('启动失败')
    }
  } catch (error) {
    repairingEmotion30d.value = false
    ElMessage.error(`启动失败: ${error.response?.data?.detail || error.message}`)
  }
}

const repairEmotionLatest = async () => {
  if (!confirm('确认更新最新交易日情绪周期吗？')) return
  repairingEmotionLatest.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/repair-emotion-cycle-latest`)
    if (response.data?.success) {
      ElMessage.success('最新情绪周期更新任务已启动')
      pollTaskStatus('repair_emotion_cycle_latest', () => { repairingEmotionLatest.value = false })
    } else {
      repairingEmotionLatest.value = false
      ElMessage.error('启动失败')
    }
  } catch (error) {
    repairingEmotionLatest.value = false
    ElMessage.error(`启动失败: ${error.response?.data?.detail || error.message}`)
  }
}

const toggleEmotionAuto5m = async () => {
  const enable = !emotionAutoEnabled.value
  try {
    const endpoint = enable ? 'start' : 'stop'
    const response = await axios.post(`${API_BASE}/system/emotion-cycle-auto-5m/${endpoint}`)
    if (response.data?.success) {
      emotionAutoEnabled.value = enable
      ElMessage.success(enable ? '已开启盘中每5分钟自动修复' : '已关闭盘中自动修复')
    }
  } catch (error) {
    ElMessage.error(`${enable ? '开启' : '关闭'}失败: ${error.response?.data?.detail || error.message}`)
  }
}

onMounted(() => {
  refreshCoreMaintenanceStatus()
  loadStartupReferenceSyncSetting()
  coreMaintenanceRefreshTimer.value = setInterval(async () => {
    await refreshCoreMaintenanceStatus()
    if (showTimelinePanel.value) await refreshTimeline()
  }, 10000)
})

onUnmounted(() => {
  if (coreMaintenanceRefreshTimer.value) {
    clearInterval(coreMaintenanceRefreshTimer.value)
    coreMaintenanceRefreshTimer.value = null
  }
})
</script>

<style scoped>
.system-config {
  max-width: 1240px;
  margin: 0 auto;
  padding: 20px;
}

.config-header {
  text-align: center;
  margin-bottom: 20px;
}

.config-header h1 {
  margin: 0;
  font-size: 34px;
  color: #1f2a44;
}

.config-header p {
  margin-top: 8px;
  color: #607189;
}

.health-strip {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 18px;
}

.health-item {
  background: linear-gradient(135deg, #f8fbff, #f3f7ff);
  border: 1px solid #dde8ff;
  border-radius: 12px;
  padding: 12px;
}

.health-label {
  display: block;
  font-size: 12px;
  color: #5d6d87;
  margin-bottom: 6px;
}

.section-card {
  background: #fff;
  border: 1px solid #e6edf7;
  border-radius: 14px;
  padding: 16px;
  margin-bottom: 18px;
}

.section-title-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
}

.section-title-row h2 {
  margin: 0;
  color: #203153;
}

.status-pill {
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 12px;
}

.status-pill.enabled {
  color: #1e7e34;
  background: #e8f8ee;
}

.status-pill.paused {
  color: #805500;
  background: #fff4d8;
}

.task-table {
  border: 1px solid #edf2f9;
  border-radius: 10px;
  overflow: hidden;
}

.task-row {
  display: grid;
  grid-template-columns: 1.1fr 0.7fr 1.5fr 0.8fr 0.9fr 0.9fr 1fr 1fr 0.8fr;
  gap: 8px;
  padding: 10px 12px;
  border-bottom: 1px solid #edf2f9;
  align-items: center;
  font-size: 13px;
}

.task-row:last-child {
  border-bottom: none;
}

.task-row.head {
  font-weight: 600;
  background: #f6f9ff;
  color: #35507b;
}

.task-action-cell {
  display: flex;
  justify-content: flex-start;
}

.task-sync-btn {
  padding: 7px 12px;
  font-size: 12px;
  white-space: nowrap;
}

.maintenance-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 14px;
}

.btn {
  border: none;
  border-radius: 8px;
  padding: 10px 14px;
  color: #fff;
  background: #4d65d9;
  cursor: pointer;
}

.btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.btn.primary {
  background: #3f62e5;
}

.btn.warning {
  background: #ca6a00;
}

.btn.success {
  background: #0e8b57;
}

.btn.danger {
  background: #ba3b2d;
}

.btn.ghost {
  background: #6b7387;
}

.timeline-panel {
  margin-top: 14px;
  border: 1px solid #ebeff7;
  border-radius: 10px;
  padding: 10px;
  background: #fbfdff;
}

.timeline-title {
  font-weight: 600;
  margin-bottom: 8px;
  color: #2b4269;
}

.timeline-empty {
  color: #70839f;
  font-size: 13px;
}

.timeline-item {
  display: grid;
  grid-template-columns: 1.2fr 0.8fr 1.8fr 0.8fr 1.2fr;
  gap: 8px;
  font-size: 12px;
  border-bottom: 1px solid #eef3fb;
  padding: 8px 0;
}

.timeline-item:last-child {
  border-bottom: none;
}

.timeline-msg {
  color: #5e6f89;
}

.timeline-duration {
  color: #38506f;
  font-weight: 600;
}

.section-tip {
  margin-top: 0;
  margin-bottom: 12px;
  color: #667997;
}

.config-note {
  margin-top: 10px;
  color: #667997;
  font-size: 13px;
}

.advanced-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.manual-card {
  border: 1px solid #ebeff7;
  border-radius: 10px;
  padding: 12px;
}

.manual-card h3 {
  margin: 0;
  color: #243b63;
}

.manual-card p {
  font-size: 13px;
  color: #667a97;
}

.tag-row {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}

.tag {
  font-size: 12px;
  color: #3766ae;
  background: #edf4ff;
  border-radius: 999px;
  padding: 2px 8px;
}

.button-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.period-box {
  font-size: 12px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 10px;
}

.days-box {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 10px;
}

.days-box input {
  width: 90px;
  padding: 6px;
  border: 1px solid #d4deef;
  border-radius: 6px;
}

.state-running {
  color: #1f66d5;
}

.state-success {
  color: #1a8f58;
}

.state-failed {
  color: #cf352e;
}

.state-paused {
  color: #7a6a3d;
}

.state-idle {
  color: #5f6f8a;
}

@media (max-width: 980px) {
  .health-strip {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .advanced-grid {
    grid-template-columns: 1fr;
  }

  .task-row {
    grid-template-columns: 1fr;
  }

  .timeline-item {
    grid-template-columns: 1fr;
  }
}
</style>
