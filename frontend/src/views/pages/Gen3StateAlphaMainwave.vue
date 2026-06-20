<template>
  <div class="g3-page mainwave-page">
    <header class="topbar">
      <div>
        <div class="eyebrow">G3 第三代策略 / institutional_mainwave</div>
        <h1>主升行业机会</h1>
        <p>用机构主升浪路线识别当下的板块扩散、主升分和下一交易日买入关注。</p>
      </div>
      <div class="actions">
        <el-button type="primary" :loading="loading" @click="load">刷新</el-button>
      </div>
    </header>

    <el-alert
      class="strategy-alert"
      type="info"
      :closable="false"
      show-icon
      title="当前 G3 中识别机构集体机会或行业机会的策略是 institutional_mainwave（机构主升浪）。"
    >
      <template #default>
        <span>{{ strategyText }}</span>
      </template>
    </el-alert>

    <section class="metric-grid">
      <div class="metric-card accent">
        <span>机会交易日</span>
        <strong>{{ summary.entry_date || '--' }}</strong>
        <small>决策日 {{ summary.decision_date || '--' }}</small>
      </div>
      <div class="metric-card">
        <span>主升行业</span>
        <strong>{{ fmt(summary.strong_sector_count, 0) }}</strong>
        <small>重要机会 {{ fmt(summary.important_sector_count, 0) }} / 全部 {{ fmt(summary.sector_count, 0) }}</small>
      </div>
      <div class="metric-card accent">
        <span>下一交易日推荐</span>
        <strong>{{ fmt(summary.recommended_count, 0) }}</strong>
        <small>{{ recommendedNames || '暂无推荐买入票据' }}</small>
      </div>
      <div class="metric-card">
        <span>机构主升候选</span>
        <strong>{{ fmt(summary.candidate_count, 0) }}</strong>
        <small>30m 通过 {{ fmt(summary.m30_ok_count, 0) }}</small>
      </div>
      <div class="metric-card" :class="heatCardClass">
        <span>指数60日热度</span>
        <strong>{{ pct(summary.index_mom60) }}</strong>
        <small>{{ summary.index_heat_label || '--' }}</small>
      </div>
      <div class="metric-card locked">
        <span>最强行业</span>
        <strong>{{ summary.strongest_sector || '--' }}</strong>
        <small>{{ summary.strongest_sector_state || '--' }}</small>
      </div>
    </section>

    <section class="two-col">
      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>行业机会排序</h2>
            <p>按板块扩散、最高主升分、候选密度和下一交易日推荐票综合排序。</p>
          </div>
        </div>
        <el-table
          v-loading="loading"
          :data="sectorRows"
          stripe
          size="small"
          highlight-current-row
          empty-text="暂无机构主升行业机会"
          @row-click="selectSector"
        >
          <el-table-column label="行业/板块" min-width="135">
            <template #default="{ row }">
              <div class="sector-cell">
                <strong>{{ row.sector_name }}</strong>
                <small>{{ row.top_candidate_names || '--' }}</small>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="112">
            <template #default="{ row }">
              <el-tag size="small" :type="sectorTagType(row.state)">{{ row.state_label }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="扩散分" width="90" align="right">
            <template #default="{ row }">{{ fmt(row.avg_sector_diffusion_score, 1) }}</template>
          </el-table-column>
          <el-table-column label="候选" width="76" align="right">
            <template #default="{ row }">{{ fmt(row.candidate_count, 0) }}</template>
          </el-table-column>
          <el-table-column label="推荐" width="76" align="right">
            <template #default="{ row }">{{ fmt(row.recommended_count, 0) }}</template>
          </el-table-column>
          <el-table-column label="最高主升" width="96" align="right">
            <template #default="{ row }">{{ fmt(row.max_wave_style_score, 1) }}</template>
          </el-table-column>
        </el-table>
      </div>

      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>策略口径</h2>
            <p>这不是新策略，是把当前 G3 主路由的行业机会证据集中展示。</p>
          </div>
        </div>
        <div class="rule-list">
          <div>
            <span>识别策略</span>
            <strong>{{ payload.strategy?.route_label || '机构主升浪' }}</strong>
            <small>{{ payload.strategy?.route || 'institutional_mainwave' }}</small>
          </div>
          <div>
            <span>主升分门槛</span>
            <strong>{{ fmt(summary.min_score, 0) }}</strong>
            <small>wave_style_score 主升分</small>
          </div>
          <div>
            <span>板块扩散门槛</span>
            <strong>{{ fmt(summary.min_sector_diffusion, 0) }}</strong>
            <small>sector_diffusion_score</small>
          </div>
          <div>
            <span>市场热度上限</span>
            <strong>{{ pct(summary.max_index_mom60) }}</strong>
            <small>5%-10% 保留候选但降仓</small>
          </div>
        </div>

        <div class="recommend-box">
          <div class="recommend-head">
            <strong>下一交易日买入关注</strong>
            <el-tag size="small" type="success">{{ fmt(recommendedRows.length, 0) }} 张</el-tag>
          </div>
          <div v-if="recommendedRows.length" class="recommend-list">
            <div v-for="item in recommendedRows" :key="item.code" class="recommend-row">
              <div>
                <strong>{{ item.name }}</strong>
                <small>{{ item.code }} / {{ item.sector_name }}</small>
              </div>
              <div class="recommend-score">
                <span>{{ fmt(item.wave_style_score, 1) }}</span>
                <small>{{ fmt(item.position_pct * 100, 1) }}%</small>
              </div>
            </div>
          </div>
          <el-empty v-else description="暂无下一交易日推荐买入票据" />
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head table-head">
        <div>
          <h2>{{ selectedSector || '全部' }} 候选明细</h2>
          <p>推荐票来自下一交易日票据，其余为机构主升预确认候选；当前持仓不在这里冒充候选。</p>
        </div>
        <div class="filters">
          <el-select v-model="selectedSector" clearable filterable placeholder="全部行业" size="small">
            <el-option v-for="item in sectorOptions" :key="item" :label="item" :value="item" />
          </el-select>
          <el-segmented v-model="candidateMode" :options="candidateModeOptions" size="small" />
        </div>
      </div>
      <el-table v-loading="loading" :data="filteredCandidates" stripe size="small" empty-text="暂无机构主升候选">
        <el-table-column label="代码" width="108" prop="code" />
        <el-table-column label="名称" width="116" prop="name" />
        <el-table-column label="行业" width="116" prop="sector_name" />
        <el-table-column label="类型" width="104">
          <template #default="{ row }">
            <el-tag size="small" :type="row.is_recommended ? 'success' : 'info'">
              {{ row.is_recommended ? '推荐买入' : '候选观察' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="模板" min-width="130" prop="template_label" show-overflow-tooltip />
        <el-table-column label="计划日" width="108" prop="entry_date" />
        <el-table-column label="主升分" width="92" align="right">
          <template #default="{ row }">{{ fmt(row.wave_style_score, 1) }}</template>
        </el-table-column>
        <el-table-column label="扩散分" width="92" align="right">
          <template #default="{ row }">{{ fmt(row.sector_diffusion_score, 1) }}</template>
        </el-table-column>
        <el-table-column label="30m" width="82">
          <template #default="{ row }">
            <el-tag size="small" :type="row.m30_confirmed ? 'success' : 'warning'">
              {{ statusLabel(row.m30_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="来源" min-width="150">
          <template #default="{ row }">{{ sourceLabel(row.source) }}</template>
        </el-table-column>
      </el-table>
    </section>

    <section class="panel muted-panel">
      <div class="panel-head">
        <div>
          <h2>数据来源</h2>
          <p>{{ diagnosticsText }}</p>
        </div>
      </div>
      <div class="artifact-grid">
        <div v-for="item in artifactRows" :key="item.key">
          <span>{{ item.label }}</span>
          <strong>{{ item.exists ? '存在' : '缺失' }}</strong>
          <small>{{ item.modified_at || item.path || '--' }}</small>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getGen3StateAlphaMainwaveOpportunities } from '@/api/trading'

const loading = ref(false)
const payload = ref({})
const selectedSector = ref('')
const candidateMode = ref('all')

const candidateModeOptions = [
  { label: '全部', value: 'all' },
  { label: '推荐买入', value: 'recommended' },
  { label: '候选观察', value: 'watch' }
]

const summary = computed(() => payload.value.summary || {})
const sectorRows = computed(() => payload.value.sector_opportunities || [])
const candidateRows = computed(() => payload.value.candidates || [])
const recommendedRows = computed(() => payload.value.recommended_tickets || [])
const diagnostics = computed(() => payload.value.diagnostics || {})

const strategyText = computed(() => {
  const strategy = payload.value.strategy || {}
  return strategy.purpose || '机构主升浪用于识别机构集体主升、板块扩散和重要行业机会，并输出下一交易日候选。'
})

const recommendedNames = computed(() => recommendedRows.value.map((item) => item.name || item.code).filter(Boolean).join(' / '))

const heatCardClass = computed(() => {
  const value = Number(summary.value.index_mom60)
  if (!Number.isFinite(value)) return ''
  if (value > 0.1) return 'danger'
  if (value > 0.05) return 'warning'
  return 'accent'
})

const sectorOptions = computed(() => sectorRows.value.map((item) => item.sector_name).filter(Boolean))

const filteredCandidates = computed(() => {
  return candidateRows.value.filter((item) => {
    if (selectedSector.value && item.sector_name !== selectedSector.value) return false
    if (candidateMode.value === 'recommended') return Boolean(item.is_recommended)
    if (candidateMode.value === 'watch') return !item.is_recommended
    return true
  })
})

const diagnosticsText = computed(() => {
  const code = diagnostics.value.diagnosis_code || 'NO_DIAGNOSIS'
  const source = summary.value.source_label || '--'
  const path = summary.value.source_path || '--'
  return `当前读取源 ${source}，诊断码 ${code}，路径 ${path}`
})

const artifactRows = computed(() => {
  const artifacts = payload.value.artifacts || {}
  const labels = {
    state_alpha_afterhours_summary: '盘后候选摘要',
    state_alpha_afterhours_tickets: '盘后买入票据',
    state_alpha_summary: '当前摘要',
    state_alpha_tickets: '当前票据',
    mainwave_runtime_summary: '机构主升摘要',
    mainwave_runtime_candidates: '机构主升候选',
    mainwave_runtime_blocked_candidates: '机构主升阻断'
  }
  return Object.entries(labels).map(([key, label]) => ({
    key,
    label,
    ...(artifacts[key] || {})
  }))
})

async function load() {
  loading.value = true
  try {
    const res = await getGen3StateAlphaMainwaveOpportunities({ limit: 120 })
    payload.value = res || {}
    if (!selectedSector.value && sectorRows.value.length) {
      selectedSector.value = sectorRows.value[0].sector_name
    }
  } catch (error) {
    ElMessage.error(`主升行业机会加载失败：${error?.message || error}`)
  } finally {
    loading.value = false
  }
}

function selectSector(row) {
  selectedSector.value = row?.sector_name || ''
}

function fmt(value, digits = 2) {
  const number = Number(value)
  if (!Number.isFinite(number)) return '--'
  return number.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  })
}

function pct(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return '--'
  return `${(number * 100).toFixed(2)}%`
}

function sectorTagType(state) {
  if (state === 'strong_mainwave') return 'success'
  if (state === 'important_industry') return 'warning'
  return 'info'
}

function statusLabel(value) {
  const raw = String(value || 'unknown')
  const labels = {
    ok: 'ok（通过）',
    blocked: 'blocked（阻断）',
    missing: 'missing（缺失）',
    unknown: 'unknown（未知）'
  }
  return labels[raw] || raw
}

function sourceLabel(value) {
  const raw = String(value || '')
  const labels = {
    next_trade_ticket: '下一交易日票据',
    pre_confirm_preview: '机构主升预确认',
    mainwave_latest_candidates: '机构主升当前候选'
  }
  return labels[raw] || raw || '--'
}

onMounted(load)
</script>

<style scoped>
.g3-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.topbar,
.panel,
.strategy-alert {
  border: 1px solid #d9e2f1;
  border-radius: 8px;
  background: #fff;
}

.topbar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 16px;
}

.eyebrow {
  color: #3760a8;
  font-size: 12px;
  font-weight: 700;
  margin-bottom: 6px;
}

h1,
h2,
p {
  margin: 0;
}

h1 {
  color: #071a44;
  font-size: 24px;
  line-height: 1.2;
}

h2 {
  color: #071a44;
  font-size: 16px;
}

p,
small {
  color: #667085;
}

.actions,
.filters {
  display: flex;
  align-items: center;
  gap: 8px;
}

.strategy-alert {
  padding: 0;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 10px;
}

.metric-card {
  min-height: 92px;
  padding: 12px;
  border: 1px solid #e4e9f1;
  border-left: 4px solid #98a2b3;
  border-radius: 8px;
  background: #fff;
}

.metric-card.accent {
  border-left-color: #12b76a;
}

.metric-card.warning {
  border-left-color: #f79009;
}

.metric-card.danger {
  border-left-color: #d92d20;
}

.metric-card.locked {
  border-left-color: #3760a8;
}

.metric-card span,
.rule-list span,
.artifact-grid span {
  display: block;
  color: #667085;
  font-size: 12px;
}

.metric-card strong {
  display: block;
  margin: 8px 0 4px;
  color: #071a44;
  font-size: 20px;
  line-height: 1.2;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.two-col {
  display: grid;
  grid-template-columns: minmax(0, 1.25fr) minmax(360px, 0.75fr);
  gap: 14px;
}

.panel {
  padding: 14px;
}

.panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.table-head {
  align-items: center;
}

.sector-cell,
.recommend-row > div:first-child {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.sector-cell strong,
.recommend-row strong,
.rule-list strong,
.artifact-grid strong {
  color: #071a44;
}

.sector-cell small,
.recommend-row small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rule-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.rule-list > div,
.artifact-grid > div {
  min-height: 78px;
  padding: 10px;
  border: 1px solid #eef2f6;
  border-radius: 8px;
  background: #f8fafc;
}

.rule-list strong {
  display: block;
  margin: 6px 0 3px;
  font-size: 18px;
}

.recommend-box {
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px solid #eef2f6;
}

.recommend-head,
.recommend-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.recommend-head {
  margin-bottom: 8px;
}

.recommend-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.recommend-row {
  min-height: 58px;
  padding: 9px 10px;
  border: 1px solid #d9e2f1;
  border-radius: 8px;
  background: #fbfdff;
}

.recommend-score {
  text-align: right;
}

.recommend-score span {
  display: block;
  color: #071a44;
  font-weight: 800;
}

.artifact-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.artifact-grid strong {
  display: block;
  margin: 6px 0 3px;
}

.artifact-grid small {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.muted-panel {
  background: #fbfdff;
}

@media (max-width: 1320px) {
  .metric-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .two-col {
    grid-template-columns: 1fr;
  }

  .artifact-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 760px) {
  .topbar,
  .panel-head,
  .filters {
    align-items: flex-start;
    flex-direction: column;
  }

  .metric-grid,
  .rule-list,
  .artifact-grid {
    grid-template-columns: 1fr;
  }
}
</style>
