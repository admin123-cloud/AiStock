<template>
  <div class="cls-radar-page">
    <section class="top-band">
      <div>
        <p class="eyebrow">CLS News Radar</p>
        <h1>财联社消息雷达</h1>
      </div>
      <div class="actions">
        <el-input-number v-model="limit" :min="10" :max="200" :step="10" />
        <el-select v-model="classFilter" clearable placeholder="信号分层" style="width: 150px">
          <el-option label="A1 主升确认" value="A1 主升确认" />
          <el-option label="A2 可短线跟随" value="A2 可短线跟随" />
          <el-option label="B1 只观察" value="B1 只观察" />
          <el-option label="C1 疑似兑现" value="C1 疑似兑现" />
          <el-option label="D1 噪音过滤" value="D1 噪音过滤" />
        </el-select>
        <el-input-number v-model="minScore" :min="0" :max="100" :step="5" />
        <el-button :icon="Search" :loading="loading" @click="loadSignals">查询</el-button>
        <el-button type="primary" :icon="Refresh" :loading="syncing" @click="syncNow">同步</el-button>
      </div>
    </section>

    <section class="status-strip">
      <div class="status-item">
        <span>最近同步</span>
        <strong>{{ statusTime }}</strong>
      </div>
      <div class="status-item">
        <span>原始消息</span>
        <strong>{{ rawCount }}</strong>
      </div>
      <div class="status-item">
        <span>信号记录</span>
        <strong>{{ signalCount }}</strong>
      </div>
      <div class="status-item">
        <span>最新消息 ID</span>
        <strong>{{ latestId }}</strong>
      </div>
      <div class="status-item">
        <span>最新新增</span>
        <strong>{{ latestNewRows }}</strong>
      </div>
      <div class="status-item">
        <span>可用上下文</span>
        <strong>{{ usableContextCount }} / {{ contextCount }}</strong>
      </div>
    </section>

    <section class="panel summary-panel">
      <div class="panel-head">
        <h2>雷达总览</h2>
        <span>{{ radarSummary.signal_count || 0 }} 条信号</span>
      </div>
      <div class="summary-grid">
        <div>
          <div class="summary-title">分层</div>
          <div class="summary-line" v-for="item in summaryPairs(radarSummary.class_counts)" :key="`class-${item[0]}`">
            <span>{{ item[0] }}</span><strong>{{ item[1] }}</strong>
          </div>
        </div>
        <div>
          <div class="summary-title">候选状态</div>
          <div class="summary-line" v-for="item in summaryPairs(radarSummary.setup_counts)" :key="`setup-${item[0]}`">
            <span>{{ item[0] }}</span><strong>{{ item[1] }}</strong>
          </div>
        </div>
        <div>
          <div class="summary-title">资金状态</div>
          <div class="summary-line" v-for="item in summaryPairs(radarSummary.capital_counts)" :key="`capital-${item[0]}`">
            <span>{{ item[0] }}</span><strong>{{ item[1] }}</strong>
          </div>
        </div>
        <div>
          <div class="summary-title">题材</div>
          <div class="summary-line" v-for="item in summaryPairs(radarSummary.theme_counts).slice(0, 6)" :key="`theme-${item[0]}`">
            <span>{{ item[0] }}</span><strong>{{ item[1] }}</strong>
          </div>
        </div>
      </div>
      <el-table :data="radarHotEvents" stripe size="small" height="220" row-key="event_id" empty-text="暂无可映射事件">
        <el-table-column label="时间" width="115">
          <template #default="{ row }">{{ timeText(row.event_time) }}</template>
        </el-table-column>
        <el-table-column prop="title" label="事件" min-width="280" show-overflow-tooltip />
        <el-table-column label="最强候选" min-width="160">
          <template #default="{ row }">{{ row.best_name || row.best_code || '--' }} {{ row.best_code || '' }}</template>
        </el-table-column>
        <el-table-column label="候选分" width="80" align="right">
          <template #default="{ row }">{{ score(row.best_score) }}</template>
        </el-table-column>
        <el-table-column prop="best_setup_tag" label="状态" width="130" show-overflow-tooltip />
        <el-table-column prop="best_capital_tag" label="资金" width="130" show-overflow-tooltip />
        <el-table-column prop="best_action_tag" label="动作" width="130" show-overflow-tooltip />
      </el-table>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>消息机会池</h2>
        <span>{{ signals.length }} 条</span>
      </div>
      <el-table
        :data="signals"
        stripe
        size="small"
        height="640"
        row-key="id"
        highlight-current-row
        empty-text="暂无财联社消息信号"
        @current-change="handleSignalChange"
      >
        <el-table-column label="时间" width="150">
          <template #default="{ row }">{{ timeText(row.event_time) }}</template>
        </el-table-column>
        <el-table-column label="分层" width="125">
          <template #default="{ row }">
            <el-tag :type="classTagType(row.signal_class)" effect="light">
              {{ row.signal_class || '--' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="机会分" width="90" align="right">
          <template #default="{ row }">
            <strong :class="scoreClass(row.opportunity_score)">{{ score(row.opportunity_score) }}</strong>
          </template>
        </el-table-column>
        <el-table-column label="消息" min-width="360">
          <template #default="{ row }">
            <div class="news-title">{{ row.title || shortText(row.content, 48) }}</div>
            <div class="news-content">{{ row.content }}</div>
            <div class="meta-line">
              <span v-if="row.themes_text">题材 {{ row.themes_text }}</span>
              <span v-if="row.subjects_text">话题 {{ row.subjects_text }}</span>
              <span v-if="row.related_codes">个股 {{ row.related_codes }}</span>
              <span v-if="row.strategy_overlap_names">策略重合 {{ row.strategy_overlap_names }}</span>
              <span v-if="row.theme_overlap_labels">主线重合 {{ row.theme_overlap_labels }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="消息分" width="85" align="right">
          <template #default="{ row }">{{ score(row.event_score) }}</template>
        </el-table-column>
        <el-table-column label="提前埋伏" width="95" align="right">
          <template #default="{ row }">{{ score(row.pre_position_score) }}</template>
        </el-table-column>
        <el-table-column label="后确认" width="85" align="right">
          <template #default="{ row }">{{ score(row.post_confirm_score) }}</template>
        </el-table-column>
        <el-table-column label="跟风风险" width="95" align="right">
          <template #default="{ row }">{{ score(row.chase_risk_score) }}</template>
        </el-table-column>
        <el-table-column label="策略重合" width="95" align="right">
          <template #default="{ row }">{{ score(row.strategy_overlap_score) }}</template>
        </el-table-column>
        <el-table-column label="题材重合" width="95" align="right">
          <template #default="{ row }">{{ score(row.theme_overlap_score) }}</template>
        </el-table-column>
        <el-table-column prop="reason" label="触发原因" min-width="240" show-overflow-tooltip />
      </el-table>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>事件候选</h2>
        <span>{{ candidateSummaryText }}</span>
      </div>
      <el-table
        v-loading="candidateLoading"
        :data="candidates"
        stripe
        size="small"
        height="360"
        row-key="code"
        empty-text="当前消息暂无可展开候选"
      >
        <el-table-column label="关系" width="100">
          <template #default="{ row }">
            <el-tag :type="row.relation === 'direct' ? 'danger' : 'info'" effect="light">
              {{ row.relation === 'direct' ? '直接关联' : '板块扩散' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="股票" min-width="150">
          <template #default="{ row }">
            <div class="news-title">{{ row.name || row.code }}</div>
            <div class="meta-line">{{ row.code }} {{ row.industry || '' }}</div>
          </template>
        </el-table-column>
        <el-table-column prop="sector_names" label="板块" min-width="180" show-overflow-tooltip />
        <el-table-column label="候选分" width="90" align="right">
          <template #default="{ row }">
            <strong :class="scoreClass(row.candidate_score)">{{ score(row.candidate_score) }}</strong>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag :type="candidateTagType(row.setup_tag)" effect="light">
              {{ row.setup_tag || '--' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="action_tag" label="动作" width="120" show-overflow-tooltip />
        <el-table-column prop="risk_tag" label="风险" min-width="150" show-overflow-tooltip />
        <el-table-column label="资金分" width="90" align="right">
          <template #default="{ row }">{{ score(row.capital_proxy_score) }}</template>
        </el-table-column>
        <el-table-column prop="capital_tag" label="资金状态" width="130" show-overflow-tooltip />
        <el-table-column label="5日量比" width="80" align="right">
          <template #default="{ row }">{{ ratioText(row.amount_ratio5) }}</template>
        </el-table-column>
        <el-table-column label="20日量比" width="85" align="right">
          <template #default="{ row }">{{ ratioText(row.amount_ratio20) }}</template>
        </el-table-column>
        <el-table-column label="涨跌幅" width="90" align="right">
          <template #default="{ row }">{{ pct(row.change_pct) }}</template>
        </el-table-column>
        <el-table-column label="成交额" width="100" align="right">
          <template #default="{ row }">{{ amountText(row.amount) }}</template>
        </el-table-column>
        <el-table-column label="快照价" width="95" align="right">
          <template #default="{ row }">{{ priceText(row.snapshot_price) }}</template>
        </el-table-column>
        <el-table-column prop="decision_reason" label="判断" min-width="260" show-overflow-tooltip />
        <el-table-column prop="snapshot_time" label="快照时间" width="170" show-overflow-tooltip />
      </el-table>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, Search } from '@element-plus/icons-vue'
import { getClsEventCandidates, getClsNewsSignals, getClsNewsStatus, getClsRadarSummary, syncClsNews } from '@/api/news'

const loading = ref(false)
const syncing = ref(false)
const limit = ref(80)
const minScore = ref(0)
const classFilter = ref('')
const status = ref({})
const radarSummary = ref({})
const signals = ref([])
const selectedSignalId = ref(null)
const candidateLoading = ref(false)
const candidates = ref([])
const candidateSummary = ref({})

const statusTime = computed(() => status.value?.state?.last_sync_at || '--')
const latestId = computed(() => status.value?.state?.latest_id || '--')
const latestNewRows = computed(() => status.value?.state?.last_result?.new_rows ?? '--')
const rawCount = computed(() => status.value?.raw?.cnt ?? 0)
const signalCount = computed(() => status.value?.signals?.cnt ?? 0)
const contextItems = computed(() => Array.isArray(status.value?.context) ? status.value.context : [])
const contextCount = computed(() => contextItems.value.length)
const usableContextCount = computed(() => contextItems.value.filter((item) => item?.usable).length)
const radarHotEvents = computed(() => Array.isArray(radarSummary.value?.hot_events) ? radarSummary.value.hot_events : [])
const candidateSummaryText = computed(() => {
  if (!selectedSignalId.value) return '选择一条消息后展示'
  return `直接 ${candidateSummary.value?.direct || 0} / 扩散 ${candidateSummary.value?.sector_peer || 0} / 共 ${candidateSummary.value?.total || 0}`
})

function score(value) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '--'
  return num.toFixed(1)
}

function summaryPairs(value) {
  return Object.entries(value || {}).slice(0, 8)
}

function scoreClass(value) {
  const num = Number(value)
  if (num >= 70) return 'score-hot'
  if (num >= 55) return 'score-watch'
  return ''
}

function classTagType(value) {
  const text = String(value || '')
  if (text.startsWith('A1')) return 'danger'
  if (text.startsWith('A2')) return 'warning'
  if (text.startsWith('C1')) return 'info'
  if (text.startsWith('D1')) return 'info'
  return 'success'
}

function candidateTagType(value) {
  const text = String(value || '')
  if (text.includes('主升浪')) return 'danger'
  if (text.includes('埋伏')) return 'warning'
  if (text.includes('追高')) return 'info'
  if (text.includes('跟风')) return 'success'
  return ''
}

function timeText(value) {
  if (!value) return '--'
  return String(value).replace('T', ' ').replace('+08:00', '').slice(5, 16)
}

function shortText(value, length) {
  const text = String(value || '')
  return text.length > length ? `${text.slice(0, length)}...` : text
}

function pct(value) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '--'
  return `${num.toFixed(2)}%`
}

function ratioText(value) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '--'
  return `${num.toFixed(2)}x`
}

function amountText(value) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '--'
  if (num >= 1e4) return `${(num / 1e4).toFixed(2)}亿`
  return `${num.toFixed(0)}万`
}

function priceText(value) {
  const num = Number(value)
  if (!Number.isFinite(num) || num <= 0) return '--'
  return num.toFixed(2)
}

async function loadCandidates(eventId) {
  if (!eventId) {
    candidates.value = []
    candidateSummary.value = {}
    return
  }
  candidateLoading.value = true
  try {
    const data = await getClsEventCandidates(eventId, { limit: 40, peer_limit: 25 })
    candidates.value = Array.isArray(data?.items) ? data.items : []
    candidateSummary.value = data?.summary || {}
  } finally {
    candidateLoading.value = false
  }
}

function handleSignalChange(row) {
  selectedSignalId.value = row?.id || null
  loadCandidates(selectedSignalId.value)
}

async function loadStatus() {
  status.value = await getClsNewsStatus()
}

async function loadRadarSummary() {
  radarSummary.value = await getClsRadarSummary({ limit: 50, top_events: 10, peer_limit: 12 })
}

async function loadSignals() {
  loading.value = true
  try {
    const data = await getClsNewsSignals({
      limit: limit.value,
      min_score: minScore.value,
      signal_class: classFilter.value || undefined
    })
    signals.value = Array.isArray(data?.items) ? data.items : []
    await loadStatus()
    await loadRadarSummary()
    if (signals.value.length) {
      const stillExists = signals.value.some((row) => row.id === selectedSignalId.value)
      const nextId = stillExists ? selectedSignalId.value : signals.value[0].id
      selectedSignalId.value = nextId
      await loadCandidates(nextId)
    } else {
      selectedSignalId.value = null
      candidates.value = []
      candidateSummary.value = {}
    }
  } finally {
    loading.value = false
  }
}

async function syncNow() {
  syncing.value = true
  try {
    const result = await syncClsNews({ pages: 1, rn: 50 })
    ElMessage.success(`同步完成，新增 ${result?.new_rows ?? 0} 条`)
    await loadSignals()
  } finally {
    syncing.value = false
  }
}

onMounted(() => {
  loadSignals()
})
</script>

<style scoped>
.cls-radar-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.top-band {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 14px;
  padding: 16px 18px;
  border: 1px solid #dfe6f2;
  border-radius: 8px;
  background: #f8fafc;
}

.eyebrow {
  margin: 0 0 5px;
  color: #64748b;
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
}

.top-band h1 {
  margin: 0;
  color: #172033;
  font-size: 24px;
}

.actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}

.status-strip {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 10px;
}

.status-item,
.panel {
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  background: #fff;
}

.status-item {
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.status-item span {
  color: #64748b;
  font-size: 12px;
}

.status-item strong {
  color: #172033;
  font-size: 18px;
}

.panel {
  padding: 14px;
}

.panel-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}

.panel-head h2 {
  margin: 0;
  color: #172033;
  font-size: 18px;
}

.panel-head span {
  color: #64748b;
  font-size: 13px;
}

.summary-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.summary-title {
  margin-bottom: 7px;
  color: #334155;
  font-size: 13px;
  font-weight: 700;
}

.summary-line {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  padding: 4px 0;
  border-bottom: 1px solid #eef2f7;
  color: #475569;
  font-size: 12px;
}

.summary-line span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.summary-line strong {
  color: #172033;
}

.news-title {
  color: #172033;
  font-weight: 700;
  line-height: 1.45;
}

.news-content {
  margin-top: 4px;
  color: #475569;
  line-height: 1.5;
  white-space: normal;
}

.meta-line {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 6px;
  color: #64748b;
  font-size: 12px;
}

.score-hot {
  color: #c2410c;
}

.score-watch {
  color: #b45309;
}

@media (max-width: 1100px) {
  .status-strip,
  .summary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .top-band {
    flex-direction: column;
  }

  .actions {
    justify-content: flex-start;
  }
}

@media (max-width: 720px) {
  .status-strip,
  .summary-grid {
    grid-template-columns: 1fr;
  }
}
</style>
