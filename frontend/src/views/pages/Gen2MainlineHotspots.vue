<template>
  <div class="gen2-mainline-page">
    <section class="top-band">
      <div>
        <p class="eyebrow">Gen2 Mainline Hotspots</p>
        <h1>第二代主线热点</h1>
        <p class="subtitle">
          把板块主线和主题主线拆开看。板块主线更偏确认，主题主线更偏线索与扩散。
        </p>
      </div>
      <div class="actions">
        <el-segmented v-model="refreshMode" :options="refreshModeOptions" />
        <el-date-picker
          v-model="targetDate"
          type="date"
          value-format="YYYY-MM-DD"
          format="YYYY-MM-DD"
          placeholder="选择目标日期"
          clearable
          style="width: 180px"
        />
        <el-input-number v-model="limit" :min="5" :max="120" :step="5" style="width: 120px" />
        <el-button type="primary" :icon="Search" :loading="loading" @click="fetchData">查询</el-button>
        <el-button type="warning" :loading="updateRunning" @click="startRefresh">
          {{ updateRunning ? '刷新中' : '刷新产物' }}
        </el-button>
        <el-button :icon="Refresh" :loading="loading" @click="resetAndReload">最新</el-button>
      </div>
    </section>

    <el-alert
      v-if="updateTask"
      class="task-alert"
      :type="taskAlertType"
      :closable="false"
      show-icon
      :title="updateTaskTitle"
      :description="updateTaskDescription"
    />

    <section v-if="updateTaskSteps.length" class="panel">
      <div class="panel-head compact">
        <h2>刷新步骤</h2>
        <span class="muted">
          {{ updateTask?.steps_completed || 0 }} / {{ updateTask?.steps_total || updateTaskSteps.length }}
        </span>
      </div>
      <div class="task-step-grid">
        <article v-for="step in updateTaskSteps" :key="step.name" class="task-step-card">
          <div class="source-head">
            <strong>{{ stepLabel(step.name) }}</strong>
            <el-tag :type="stepTagType(step.status)" effect="light">
              {{ stepStatusText(step.status) }}
            </el-tag>
          </div>
          <div class="source-meta">
            <span>脚本 {{ step.script || '--' }}</span>
            <span>耗时 {{ stepDuration(step) }}</span>
          </div>
          <div class="source-meta">
            <span>返回码 {{ step.returncode ?? '--' }}</span>
            <span>{{ step.optional ? '可选步骤' : '必跑步骤' }}</span>
          </div>
          <div v-if="stepLog(step)" class="task-step-log">
            {{ stepLog(step) }}
          </div>
        </article>
      </div>
    </section>

    <el-alert
      v-if="message"
      :type="available ? 'warning' : 'info'"
      :closable="false"
      show-icon
      :title="message"
    />

    <el-alert
      v-if="!available && !loading"
      type="warning"
      :closable="false"
      show-icon
      title="当前还没有可展示的主线热点结果"
      description="页面已经接好，但需要先生成主线板块或主题研究产物。"
    />

    <template v-if="available || sourceStatus.length">
      <section class="status-strip">
        <div class="status-item">
          <span>请求日期</span>
          <strong>{{ requestedDate || '--' }}</strong>
        </div>
        <div class="status-item">
          <span>展示日期</span>
          <strong>{{ selectedDate || '--' }}</strong>
        </div>
        <div class="status-item">
          <span>板块识别日</span>
          <strong>{{ sectorSnapshotDate || '--' }}</strong>
        </div>
        <div class="status-item">
          <span>最新交易日</span>
          <strong>{{ latestTradeDate || '--' }}</strong>
        </div>
        <div class="status-item">
          <span>日期解析</span>
          <strong>{{ dateResolutionText }}</strong>
        </div>
        <div class="status-item">
          <span>板块主线数</span>
          <strong class="up">{{ summary.sector_count || 0 }}</strong>
        </div>
        <div class="status-item">
          <span>主题主线池</span>
          <strong>{{ summary.theme_pool_count || 0 }}</strong>
        </div>
        <div class="status-item">
          <span>主题策略重合</span>
          <strong class="accent">{{ summary.theme_match_count || 0 }}</strong>
        </div>
      </section>

      <section class="source-grid">
        <article v-for="item in sourceStatus" :key="item.name" class="source-card">
          <div class="source-head">
            <strong>{{ item.label }}</strong>
            <el-tag :type="sourceTagType(item.status)" effect="light">
              {{ sourceTagText(item.status) }}
            </el-tag>
          </div>
          <div class="source-meta">
            <span>行数 {{ item.row_count || 0 }}</span>
            <span>最新 {{ item.latest_date || '--' }}</span>
          </div>
          <div class="source-meta">
            <span>更新 {{ item.updated_at || '--' }}</span>
          </div>
          <div class="source-path">{{ item.path || '未找到产物文件' }}</div>
        </article>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2>主线视角</h2>
          <span class="muted">顶部 Tab 只展示当前视角最重要的信息</span>
        </div>
        <el-tabs v-model="activeTab" class="mainline-tabs">
          <el-tab-pane label="板块主线" name="sector">
            <section class="tab-section two-col">
              <div>
                <div class="panel-head compact">
                  <h2>板块主线榜</h2>
                  <span class="muted">盘中扩散 + 板块强度的严格确认</span>
                </div>
                <el-table
                  :data="sectorMainlineRows"
                  stripe
                  size="small"
                  height="360"
                  row-key="sector_code"
                  highlight-current-row
                  :current-row-key="selectedSectorCode"
                  empty-text="暂无板块主线"
                  @current-change="handleSectorRowChange"
                >
                  <el-table-column label="板块" min-width="150">
                    <template #default="{ row }">
                      <button class="link-button" type="button" @click="selectSector(row.sector_code)">
                        {{ row.sector_name || '--' }}
                      </button>
                      <div class="subtext">{{ row.sector_code || '--' }}</div>
                    </template>
                  </el-table-column>
                  <el-table-column prop="threshold_level" label="阈值" width="90" />
                  <el-table-column label="主线分" width="95" align="right">
                    <template #default="{ row }">{{ score(row.mainline_intraday_score) }}</template>
                  </el-table-column>
                  <el-table-column label="窗口分" width="95" align="right">
                    <template #default="{ row }">{{ score(row.true_index_window_score) }}</template>
                  </el-table-column>
                  <el-table-column label="扩散分" width="95" align="right">
                    <template #default="{ row }">{{ score(row.intraday_diffusion_score) }}</template>
                  </el-table-column>
                  <el-table-column label="60日涨幅" width="95" align="right">
                    <template #default="{ row }">{{ pct(row.ret60) }}</template>
                  </el-table-column>
                  <el-table-column label="上午上涨比" width="100" align="right">
                    <template #default="{ row }">{{ pct(row.morning_rise_ratio) }}</template>
                  </el-table-column>
                </el-table>
              </div>

              <div>
                <div class="panel-head compact">
                  <h2>{{ sectorStockPanelTitle }}</h2>
                  <span class="muted">{{ sectorStockPanelHint }}</span>
                </div>
                <el-alert
                  v-if="sectorSelectionMismatch"
                  type="warning"
                  :closable="false"
                  show-icon
                  :title="sectorSelectionMismatch"
                  class="inline-alert"
                />
                <el-table :data="sectorStockRows" stripe size="small" height="360" :empty-text="sectorStockEmptyText">
                  <el-table-column label="股票" min-width="160">
                    <template #default="{ row }">
                      <button class="link-button" type="button" @click="goStock(row.stock_code || row.code || row.stock_code6)">
                        {{ row.stock_name || row.name || '--' }}
                      </button>
                      <div class="subtext">{{ stockCodeText(row.stock_code || row.code || row.stock_code6) }}</div>
                    </template>
                  </el-table-column>
                  <el-table-column prop="sector_name" label="板块" min-width="110" />
                  <el-table-column :label="sectorStockRankLabel" width="90" align="right">
                    <template #default="{ row }">{{ row.sector_rank || '--' }}</template>
                  </el-table-column>
                  <el-table-column :label="sectorStockScoreLabel" width="95" align="right">
                    <template #default="{ row }">
                      {{ score(sectorStockMode === 'watchlist' ? row.candidate_score : row.mainline_intraday_score) }}
                    </template>
                  </el-table-column>
                  <el-table-column :label="sectorStockRet1Label" width="100" align="right">
                    <template #default="{ row }">
                      {{ sectorStockMode === 'watchlist' ? pct(row.stock_current_from_anchor) : percentValue(row.change_pct) }}
                    </template>
                  </el-table-column>
                  <el-table-column :label="sectorStockRet2Label" width="100" align="right">
                    <template #default="{ row }">
                      {{ sectorStockMode === 'watchlist' ? pct(row.stock_current_from_h20) : percentValue(row.current_change_pct) }}
                    </template>
                  </el-table-column>
                  <el-table-column v-if="sectorStockRet3Label" :label="sectorStockRet3Label" width="100" align="right">
                    <template #default="{ row }">
                      {{ amountText(row.current_amount ?? row.amount) }}
                    </template>
                  </el-table-column>
                </el-table>
              </div>
            </section>

            <section class="tab-section">
              <div class="panel-head">
                <h2>板块主线与 G2/G3 候选重合</h2>
                <span class="muted">看哪些策略候选正落在当前板块主线里</span>
              </div>
              <el-table :data="filteredSectorOverlayRows" stripe size="small" height="320" empty-text="暂无重合候选">
                <el-table-column prop="candidate_source" label="来源" width="135" />
                  <el-table-column label="股票" min-width="150">
                    <template #default="{ row }">
                      <button class="link-button" type="button" @click="goStock(row.code)">
                        {{ row.name || '--' }}
                      </button>
                      <div class="subtext">{{ stockCodeText(row.code) }}</div>
                    </template>
                  </el-table-column>
                <el-table-column prop="sector_name" label="板块" min-width="110" />
                <el-table-column prop="entry_date_text" label="日期" width="105" />
                <el-table-column label="主线分" width="95" align="right">
                  <template #default="{ row }">{{ score(row.mainline_intraday_score) }}</template>
                </el-table-column>
                <el-table-column label="候选分" width="95" align="right">
                  <template #default="{ row }">{{ score(row.score) }}</template>
                </el-table-column>
                <el-table-column label="L3强度" width="95" align="right">
                  <template #default="{ row }">{{ pct(row.l3_rt_strong3_ratio) }}</template>
                </el-table-column>
                <el-table-column prop="formal_buy_signal" label="正式买点" min-width="120" />
                <el-table-column prop="block_reason" label="阻断原因" min-width="180" show-overflow-tooltip />
              </el-table>
            </section>
          </el-tab-pane>

          <el-tab-pane label="主题主线" name="theme">
            <section class="tab-section two-col">
              <div>
                <div class="panel-head compact">
                  <h2>主题主线榜</h2>
                  <span class="muted">先看主题容器，再看主题里有哪些股票；默认优先主题分，其次看今日均涨跌与 L2 数</span>
                </div>
                <el-table
                  :data="themeSummaryRows"
                  stripe
                  size="small"
                  height="360"
                  row-key="theme_key"
                  highlight-current-row
                  :current-row-key="selectedThemeKey"
                  empty-text="暂无主题主线"
                  @current-change="handleThemeRowChange"
                >
                  <el-table-column label="主题" min-width="180">
                    <template #default="{ row }">
                      <button class="link-button" type="button" @click="selectTheme(row.theme_key)">
                        {{ row.theme_title || '--' }}
                      </button>
                    </template>
                  </el-table-column>
                  <el-table-column prop="best_level" label="层级" width="120" />
                  <el-table-column label="主题分" width="95" align="right">
                    <template #default="{ row }">{{ score(row.max_theme_score) }}</template>
                  </el-table-column>
                  <el-table-column label="股票数" width="85" align="right">
                    <template #default="{ row }">{{ row.stock_count || 0 }}</template>
                  </el-table-column>
                  <el-table-column label="L2数" width="80" align="right">
                    <template #default="{ row }">{{ row.l2_count || 0 }}</template>
                  </el-table-column>
                  <el-table-column :label="`${latestTradeDate || '当前'}均涨跌`" width="110" align="right">
                    <template #default="{ row }">{{ percentValue(row.avg_current_change_pct) }}</template>
                  </el-table-column>
                  <el-table-column :label="`${latestTradeDate || '当前'}领涨股`" min-width="170">
                    <template #default="{ row }">
                      <span v-if="row.leader_name && row.leader_name !== '--'">
                        {{ row.leader_name }} {{ percentValue(row.leader_current_change_pct) }}
                      </span>
                      <span v-else>--</span>
                    </template>
                  </el-table-column>
                  <el-table-column prop="risk_mix" label="风险" min-width="120" show-overflow-tooltip />
                </el-table>
              </div>

              <div>
                <div class="panel-head compact">
                  <h2>主题成分股</h2>
                  <span class="muted">{{ selectedThemeHint }}</span>
                </div>
                <el-table :data="selectedThemeRows" stripe size="small" height="360" empty-text="暂无主题成分股">
                  <el-table-column label="股票" min-width="160">
                    <template #default="{ row }">
                      <button class="link-button" type="button" @click="goStock(row.code6 || row.code)">
                        {{ row.display_name || '--' }}
                      </button>
                      <div class="subtext">{{ stockCodeText(row.code6 || row.code) }}</div>
                    </template>
                  </el-table-column>
                  <el-table-column prop="observation_level" label="层级" width="120" />
                  <el-table-column label="主题分" width="95" align="right">
                    <template #default="{ row }">{{ score(row.theme_candidate_score) }}</template>
                  </el-table-column>
                  <el-table-column label="权重提示" width="95" align="right">
                    <template #default="{ row }">{{ score(row.theme_weight_hint, 2) }}</template>
                  </el-table-column>
                  <el-table-column :label="`${latestTradeDate || '当前'}涨跌`" width="110" align="right">
                    <template #default="{ row }">{{ percentValue(row.current_change_pct) }}</template>
                  </el-table-column>
                  <el-table-column :label="`${latestTradeDate || '当前'}成交额`" width="110" align="right">
                    <template #default="{ row }">{{ amountText(row.current_amount) }}</template>
                  </el-table-column>
                  <el-table-column prop="risk_label" label="风险" width="120" />
                  <el-table-column prop="suggested_usage" label="建议" min-width="180" show-overflow-tooltip />
                </el-table>
              </div>
            </section>

            <section class="tab-section">
              <div class="panel-head">
                <h2>主题与策略叠加</h2>
                <span class="muted">看主题标签如何影响 G2 候选排序</span>
              </div>
                <el-table :data="themeOverlayRows" stripe size="small" height="320" empty-text="暂无主题策略叠加">
                <el-table-column prop="strategy_source" label="来源" width="150" />
                  <el-table-column label="股票" min-width="160">
                    <template #default="{ row }">
                      <button class="link-button" type="button" @click="goStock(row.code6 || row.code)">
                        {{ row.display_name || '--' }}
                      </button>
                      <div class="subtext">{{ stockCodeText(row.code6 || row.code) }}</div>
                    </template>
                  </el-table-column>
                <el-table-column prop="theme_label" label="主题" min-width="140" show-overflow-tooltip />
                <el-table-column prop="observation_level" label="层级" width="130" />
                <el-table-column label="原始分" width="90" align="right">
                  <template #default="{ row }">{{ score(row.raw_score) }}</template>
                </el-table-column>
                <el-table-column label="叠加分" width="90" align="right">
                  <template #default="{ row }">{{ score(row.overlay_score) }}</template>
                </el-table-column>
                <el-table-column prop="theme_overlay_action" label="动作" width="140" />
                <el-table-column prop="risk_label" label="风险" width="120" />
              </el-table>
            </section>
          </el-tab-pane>
        </el-tabs>
      </section>
    </template>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Refresh, Search } from '@element-plus/icons-vue'

import {
  getGen2MainlineHotspots,
  getGen2MainlineHotspotsUpdateTask,
  runGen2MainlineHotspotsUpdate
} from '@/api/trading'

const router = useRouter()

const loading = ref(false)
const payload = ref({})
const targetDate = ref('')
const limit = ref(30)
const updateTask = ref(null)
const activeTab = ref('sector')
const selectedSectorCode = ref('')
const selectedThemeKey = ref('')
const refreshMode = ref('all')
const refreshModeOptions = [
  { label: '全部刷新', value: 'all' },
  { label: '只刷板块', value: 'sector' },
  { label: '只刷主题', value: 'theme' }
]
let updateTaskTimer = null

const ANSI_PATTERN = /\u001b\[[0-9;]*m/g

const available = computed(() => !!payload.value?.available)
const message = computed(() => payload.value?.message || '')
const requestedDate = computed(() => payload.value?.requested_date || '')
const selectedDate = computed(() => payload.value?.selected_date || '')
const sourceDates = computed(() => payload.value?.source_dates || {})
const latestTradeDate = computed(() => payload.value?.latest_trade_date || sourceDates.value?.latest_trade || '')
const sectorSnapshotDate = computed(() => sourceDates.value?.sector_overlay || sourceDates.value?.sector_factor || '')
const summary = computed(() => payload.value?.summary || {})
const sourceStatus = computed(() => Array.isArray(payload.value?.source_status) ? payload.value.source_status : [])
const sectorMainlineRows = computed(() => Array.isArray(payload.value?.sector_mainline) ? payload.value.sector_mainline : [])
const sectorOverlayRows = computed(() => Array.isArray(payload.value?.sector_candidate_overlay) ? payload.value.sector_candidate_overlay : [])
const watchlistRows = computed(() => Array.isArray(payload.value?.sector_watchlist) ? payload.value.sector_watchlist : [])
const sectorMemberSnapshotRows = computed(() => Array.isArray(payload.value?.sector_member_snapshot) ? payload.value.sector_member_snapshot : [])
const themePoolRows = computed(() => Array.isArray(payload.value?.theme_observation_pool) ? payload.value.theme_observation_pool : [])
const themeOverlayRows = computed(() => Array.isArray(payload.value?.theme_strategy_overlay) ? payload.value.theme_strategy_overlay : [])
const updateTaskSteps = computed(() => Array.isArray(updateTask.value?.steps) ? updateTask.value.steps : [])
const updateRunning = computed(() => ['queued', 'running'].includes(updateTask.value?.status))
const updateWarnings = computed(() => Array.isArray(updateTask.value?.warnings) ? updateTask.value.warnings : [])
const taskAlertType = computed(() => {
  const task = updateTask.value || {}
  if (task.status === 'failed') return 'error'
  if (task.status === 'completed' && updateWarnings.value.length) return 'warning'
  if (task.status === 'completed') return 'success'
  return 'info'
})

const sectorStockMode = computed(() => (
  watchlistRows.value.length ? 'watchlist' : sectorMemberSnapshotRows.value.length ? 'member_snapshot' : 'empty'
))
const normalizedSelectedSectorCode = computed(() => String(selectedSectorCode.value || '').trim())
const filteredWatchlistRows = computed(() => {
  const code = normalizedSelectedSectorCode.value
  if (!code) return watchlistRows.value
  return watchlistRows.value.filter((row) => String(row?.sector_code || '').trim() === code)
})
const filteredSectorMemberSnapshotRows = computed(() => {
  const code = normalizedSelectedSectorCode.value
  if (!code) return sectorMemberSnapshotRows.value
  return sectorMemberSnapshotRows.value.filter((row) => String(row?.sector_code || '').trim() === code)
})
const filteredSectorOverlayRows = computed(() => {
  const code = normalizedSelectedSectorCode.value
  if (!code) return sectorOverlayRows.value
  return sectorOverlayRows.value.filter((row) => String(row?.sector_code || '').trim() === code)
})
const sectorStockRows = computed(() => (
  sectorStockMode.value === 'watchlist' ? filteredWatchlistRows.value : filteredSectorMemberSnapshotRows.value
))
const selectedSectorName = computed(() => {
  const code = normalizedSelectedSectorCode.value
  if (!code) return ''
  const matched = sectorMainlineRows.value.find((row) => String(row?.sector_code || '').trim() === code)
  return String(matched?.sector_name || '').trim()
})
const sectorStockPanelTitle = computed(() => (
  sectorStockMode.value === 'watchlist' ? '主线板块观察池' : '主线板块成分股快照'
))
const sectorStockPanelHint = computed(() => (
  sectorStockMode.value === 'watchlist'
    ? '更严格的观察池候选，偏研究观察，不直接下单'
    : `当前只展示左侧选中板块在主线识别日 ${sectorSnapshotDate.value || '--'} 的强势成分股快照；右侧同时补充 ${latestTradeDate.value || '--'} 的最新涨跌，方便对照今天盘面`
))
const sectorStockEmptyText = computed(() => (
  sectorStockMode.value === 'watchlist' ? '暂无板块观察池' : '暂无主线板块成分股快照'
))
const sectorSelectionMismatch = computed(() => {
  const code = normalizedSelectedSectorCode.value
  if (!code) return ''
  if (sectorStockMode.value === 'watchlist') return ''
  if (sectorMemberSnapshotRows.value.length > 0 && filteredSectorMemberSnapshotRows.value.length === 0) {
    return `${selectedSectorName.value || code} 当前没有匹配的成分股快照，右侧旧快照已被拦截；请重新刷新板块产物。`
  }
  return ''
})
const sectorStockRankLabel = computed(() => (
  sectorStockMode.value === 'watchlist' ? '板块排名' : '板块内排位'
))
const sectorStockScoreLabel = computed(() => (
  sectorStockMode.value === 'watchlist' ? '候选分' : '主线分'
))
const sectorStockRet1Label = computed(() => (
  sectorStockMode.value === 'watchlist' ? '锚点后收益' : '识别日涨幅'
))
const sectorStockRet2Label = computed(() => (
  sectorStockMode.value === 'watchlist' ? 'H20后收益' : `${latestTradeDate.value || '当前'}涨跌`
))
const sectorStockRet3Label = computed(() => (
  sectorStockMode.value === 'watchlist' ? '' : `${latestTradeDate.value || '当前'}成交额`
))

const themeSummaryRows = computed(() => {
  const groups = new Map()
  for (const row of themePoolRows.value) {
    const label = String(row?.theme_label || '').trim()
    if (!label) continue
    const themeKey = label
    if (!groups.has(themeKey)) {
      groups.set(themeKey, {
        theme_key: themeKey,
        theme_title: label,
        best_level: String(row?.observation_level || '--'),
        max_theme_score: Number(row?.theme_candidate_score) || 0,
        stock_count: 0,
        l2_count: 0,
        l3_count: 0,
        l4_count: 0,
        risk_labels: new Set(),
        current_change_sum: 0,
        current_change_count: 0,
        leader_name: '--',
        leader_current_change_pct: null
      })
    }
    const item = groups.get(themeKey)
    item.stock_count += 1
    const score = Number(row?.theme_candidate_score)
    if (Number.isFinite(score) && score > item.max_theme_score) {
      item.max_theme_score = score
      item.best_level = String(row?.observation_level || '--')
    }
    const level = String(row?.observation_level || '')
    if (level.startsWith('L2')) item.l2_count += 1
    else if (level.startsWith('L3')) item.l3_count += 1
    else if (level.startsWith('L4')) item.l4_count += 1
    const risk = String(row?.risk_label || '').trim()
    if (risk) item.risk_labels.add(risk)
    const currentChange = Number(row?.current_change_pct)
    if (Number.isFinite(currentChange)) {
      item.current_change_sum += currentChange
      item.current_change_count += 1
      if (item.leader_current_change_pct === null || currentChange > item.leader_current_change_pct) {
        item.leader_current_change_pct = currentChange
        item.leader_name = String(row?.display_name || '--')
      }
    }
  }
  return Array.from(groups.values())
    .map((item) => ({
      ...item,
      avg_current_change_pct: item.current_change_count ? item.current_change_sum / item.current_change_count : null,
      risk_mix: Array.from(item.risk_labels).join(' / ') || '--'
    }))
    .sort((a, b) => {
      if (b.max_theme_score !== a.max_theme_score) return b.max_theme_score - a.max_theme_score
      const avgDiff = (Number(b.avg_current_change_pct) || 0) - (Number(a.avg_current_change_pct) || 0)
      if (avgDiff !== 0) return avgDiff
      if ((b.l2_count || 0) !== (a.l2_count || 0)) return (b.l2_count || 0) - (a.l2_count || 0)
      return b.stock_count - a.stock_count
    })
})

const selectedThemeRows = computed(() => {
  const key = String(selectedThemeKey.value || '').trim()
  if (!key) return []
  return themePoolRows.value
    .filter((row) => String(row?.theme_label || '').trim() === key)
    .slice()
    .sort((a, b) => {
      const scoreDiff = (Number(b?.theme_candidate_score) || 0) - (Number(a?.theme_candidate_score) || 0)
      if (scoreDiff !== 0) return scoreDiff
      return (Number(b?.theme_weight_hint) || 0) - (Number(a?.theme_weight_hint) || 0)
    })
})

const selectedThemeHint = computed(() => {
  if (!selectedThemeKey.value) return '选择左侧主题后，在右侧看对应股票'
  return `${selectedThemeKey.value} 的主题成分股明细，并补充 ${latestTradeDate.value || '--'} 的最新涨跌`
})

watch(
  sectorMainlineRows,
  (rows) => {
    if (!rows.length) {
      selectedSectorCode.value = ''
      return
    }
    if (!rows.some((row) => String(row?.sector_code || '').trim() === selectedSectorCode.value)) {
      selectedSectorCode.value = String(rows[0]?.sector_code || '').trim()
    }
  },
  { immediate: true }
)

watch(
  themeSummaryRows,
  (rows) => {
    if (!rows.length) {
      selectedThemeKey.value = ''
      return
    }
    if (!rows.some((row) => row.theme_key === selectedThemeKey.value)) {
      selectedThemeKey.value = rows[0].theme_key
    }
  },
  { immediate: true }
)

const dateResolutionText = computed(() => {
  const value = payload.value?.date_resolution
  if (value === 'exact') return '精确命中'
  if (value === 'fallback_latest') return '回退到最新快照'
  if (value === 'latest') return '使用最新快照'
  if (value === 'requested_only') return '仅保留请求日期'
  return '--'
})

const updateTaskTitle = computed(() => {
  const task = updateTask.value || {}
  if (task.status === 'failed') return task.message || '主线热点产物刷新失败'
  if (task.status === 'completed') return task.message || '主线热点产物刷新完成'
  return task.message || '主线热点产物正在刷新'
})

const updateTaskDescription = computed(() => {
  const task = updateTask.value || {}
  const parts = []
  if (task.target_date) parts.push(`目标日期: ${task.target_date}`)
  if (task.mode) parts.push(`刷新模式: ${modeText(task.mode)}`)
  if (task.current_step) parts.push(`当前步骤: ${stepLabel(task.current_step)}`)
  if (Number.isFinite(Number(task.progress))) parts.push(`进度: ${task.progress}%`)
  if (updateWarnings.value.length) parts.push(`警告: ${updateWarnings.value.length} 项`)
  if (task.error) parts.push(`错误: ${cleanLogText(task.error)}`)
  return parts.join(' | ')
})

function cleanLogText(value) {
  return String(value || '')
    .replace(ANSI_PATTERN, '')
    .replace(/\r/g, '')
    .trim()
}

function stepLog(step) {
  const raw = cleanLogText(step?.stderr_tail || step?.stdout_tail || '')
  if (!raw) return ''
  return raw
    .split('\n')
    .map((line) => line.trimEnd())
    .filter(Boolean)
    .slice(-12)
    .join('\n')
}

function pct(value) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '--'
  return `${(num * 100).toFixed(2)}%`
}

function score(value, digits = 2) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '--'
  return num.toFixed(digits)
}

function percentValue(value, digits = 2) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '--'
  return `${num.toFixed(digits)}%`
}

function amountText(value) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '--'
  if (num >= 1e8) return `${(num / 1e8).toFixed(2)}亿`
  if (num >= 1e4) return `${(num / 1e4).toFixed(0)}万`
  return `${num.toFixed(0)}`
}

function modeText(mode) {
  if (mode === 'sector') return '只刷板块'
  if (mode === 'theme') return '只刷主题'
  return '全部刷新'
}

function stepLabel(name) {
  const mapping = {
    sector_factor: '板块主线因子',
    sector_overlay: '板块候选重合',
    sector_watchlist: '板块观察池',
    theme_clusters: '主题聚类',
    theme_candidate_source: '主题候选源',
    theme_pool: '主题观察池',
    theme_overlay: '主题策略叠加'
  }
  return mapping[name] || name || '--'
}

function stepTagType(status) {
  if (status === 'completed' || status === 'ok') return 'success'
  if (status === 'warning') return 'warning'
  if (status === 'failed' || status === 'error') return 'danger'
  if (status === 'skipped') return 'info'
  return 'info'
}

function stepStatusText(status) {
  if (status === 'completed' || status === 'ok') return '成功'
  if (status === 'warning') return '警告'
  if (status === 'failed' || status === 'error') return '失败'
  if (status === 'skipped') return '跳过'
  return '处理中'
}

function stepDuration(step) {
  const seconds = Number(step?.duration_seconds)
  if (Number.isFinite(seconds)) return `${seconds.toFixed(1)}s`
  return '--'
}

function sourceTagType(status) {
  if (status === 'ok') return 'success'
  if (status === 'warning') return 'warning'
  if (status === 'error') return 'danger'
  return 'info'
}

function sourceTagText(status) {
  if (status === 'ok') return '可用'
  if (status === 'warning') return '警告'
  if (status === 'error') return '异常'
  return '缺失'
}

function selectSector(sectorCode) {
  selectedSectorCode.value = String(sectorCode || '').trim()
}

function handleSectorRowChange(row) {
  if (row?.sector_code) {
    selectedSectorCode.value = String(row.sector_code || '').trim()
  }
}

function selectTheme(themeKey) {
  selectedThemeKey.value = String(themeKey || '').trim()
}

function handleThemeRowChange(row) {
  if (row?.theme_key) {
    selectedThemeKey.value = row.theme_key
  }
}

async function fetchData() {
  loading.value = true
  try {
    payload.value = await getGen2MainlineHotspots({
      target_date: targetDate.value || undefined,
      limit: limit.value
    })
  } catch (error) {
    payload.value = {
      available: false,
      message: error?.message || '读取第二代主线热点失败'
    }
  } finally {
    loading.value = false
  }
}

function stopTaskPolling() {
  if (updateTaskTimer) {
    clearTimeout(updateTaskTimer)
    updateTaskTimer = null
  }
}

async function pollUpdateTask(taskId) {
  stopTaskPolling()
  try {
    const data = await getGen2MainlineHotspotsUpdateTask(taskId)
    updateTask.value = data
    if (data?.status === 'completed') {
      await fetchData()
      ElMessage.success('主线热点产物刷新完成')
      stopTaskPolling()
      return
    }
    if (data?.status === 'failed' || data?.status === 'missing') {
      ElMessage.error(data?.message || data?.error || '主线热点产物刷新失败')
      stopTaskPolling()
      return
    }
    updateTaskTimer = setTimeout(() => pollUpdateTask(taskId), 3000)
  } catch (error) {
    updateTaskTimer = setTimeout(() => pollUpdateTask(taskId), 5000)
  }
}

async function startRefresh() {
  try {
    const task = await runGen2MainlineHotspotsUpdate({
      target_date: targetDate.value || undefined,
      limit: limit.value,
      mode: refreshMode.value
    })
    updateTask.value = task
    if (task?.task_id) {
      pollUpdateTask(task.task_id)
    }
  } catch (error) {
    ElMessage.error(error?.message || '启动主线热点产物刷新失败')
  }
}

function resetAndReload() {
  targetDate.value = ''
  fetchData()
}

function normalizeStockCode(value) {
  const text = String(value || '').trim().toUpperCase()
  if (!text) return ''
  if (text.includes('.')) return text
  const digits = text.replace(/\D/g, '')
  if (/^\d{1,6}$/.test(digits) && digits) {
    const padded = digits.padStart(6, '0')
    return `${padded}${padded.startsWith('6') ? '.SH' : '.SZ'}`
  }
  return text
}

function stockCodeText(value) {
  const normalized = normalizeStockCode(value)
  if (!normalized) return '--'
  return normalized.split('.')[0] || '--'
}

function goStock(code) {
  const normalized = normalizeStockCode(code)
  if (!normalized) {
    ElMessage.warning('缺少股票代码')
    return
  }
  router.push(`/stock/${normalized}`)
}

function goSector(code) {
  const text = String(code || '').trim()
  if (!text) {
    ElMessage.warning('缺少板块代码')
    return
  }
  router.push(`/sector/${text}`)
}

onMounted(() => {
  fetchData()
})

onBeforeUnmount(() => {
  stopTaskPolling()
})
</script>

<style scoped>
.gen2-mainline-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.top-band {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 20px 22px;
  border-radius: 18px;
  background: linear-gradient(135deg, #f7fbff 0%, #eef4ff 52%, #fdf5e8 100%);
  border: 1px solid #dce7ff;
}

.eyebrow {
  margin: 0 0 6px;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #6f7c9f;
}

.top-band h1 {
  margin: 0 0 8px;
  font-size: 28px;
  color: #1f2a44;
}

.subtitle {
  margin: 0;
  color: #5f6d8f;
  line-height: 1.6;
  max-width: 760px;
}

.actions {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  flex-wrap: wrap;
}

.task-alert {
  margin-top: -4px;
}

.task-step-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.task-step-card {
  padding: 14px 16px;
  border: 1px solid #e7ecff;
  border-radius: 14px;
  background: linear-gradient(180deg, #fbfcff 0%, #f5f8ff 100%);
}

.task-step-log {
  margin-top: 10px;
  padding: 10px 12px;
  border-radius: 10px;
  background: #f6f8fc;
  color: #5b6685;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 260px;
  overflow: auto;
}

.status-strip {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 12px;
}

.status-item,
.source-card,
.panel {
  background: #fff;
  border: 1px solid #e7ecff;
  border-radius: 16px;
  box-shadow: 0 10px 24px rgba(52, 77, 145, 0.06);
}

.status-item {
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.status-item span {
  color: #7180a6;
  font-size: 12px;
}

.status-item strong {
  color: #1d2948;
  font-size: 18px;
}

.status-item strong.up {
  color: #0f8a5f;
}

.status-item strong.accent {
  color: #b85f00;
}

.source-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.source-card {
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.source-head,
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.source-head strong,
.panel-head h2 {
  margin: 0;
  color: #203055;
}

.source-meta {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  color: #6f7c9f;
  font-size: 12px;
}

.source-path {
  color: #8a95b3;
  font-size: 12px;
  line-height: 1.5;
  word-break: break-all;
}

.panel {
  padding: 16px;
}

.panel-head {
  margin-bottom: 12px;
}

.panel-head.compact {
  margin-bottom: 10px;
}

.mainline-tabs :deep(.el-tabs__header) {
  margin-bottom: 12px;
}

.tab-section + .tab-section {
  margin-top: 16px;
}

.muted,
.subtext {
  color: #7b88a8;
  font-size: 12px;
}

.subtext {
  margin-top: 2px;
}

.two-col {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.link-button {
  border: none;
  background: transparent;
  padding: 0;
  color: #2a5bd7;
  cursor: pointer;
  font: inherit;
  text-align: left;
}

.link-button:hover {
  color: #17398f;
  text-decoration: underline;
}

@media (max-width: 1280px) {
  .status-strip {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .source-grid,
  .task-step-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 900px) {
  .top-band,
  .two-col {
    grid-template-columns: 1fr;
    display: grid;
  }

  .status-strip,
  .source-grid,
  .task-step-grid {
    grid-template-columns: 1fr;
  }
}
</style>
