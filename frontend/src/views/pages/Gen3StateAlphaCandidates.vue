<template>
  <div class="g3-page">
    <header class="topbar">
      <div>
        <div class="eyebrow">G3 State Alpha</div>
        <h1>候选池审计</h1>
        <p>从全量来源、路由筛选、阻断原因到最终影子票据，逐层确认今日信号为什么进入或退出。</p>
      </div>
      <div class="actions">
        <el-button type="primary" :loading="loading" @click="load">刷新</el-button>
      </div>
    </header>

    <el-alert :closable="false" show-icon :type="blockedCount ? 'warning' : 'success'" :title="statusText" />

    <section class="metric-grid">
      <div class="metric-card">
        <span>入场日</span>
        <strong>{{ summary.entry_date || '--' }}</strong>
        <small>决策日 {{ summary.decision_date || '--' }}</small>
      </div>
      <div class="metric-card accent">
        <span>全量来源</span>
        <strong>{{ allCandidates.length }}</strong>
        <small>接口返回上限内候选</small>
      </div>
      <div class="metric-card">
        <span>路由选中</span>
        <strong>{{ selectedCandidates.length }}</strong>
        <small>{{ routeName(summary.selected_route) }}</small>
      </div>
      <div class="metric-card">
        <span>影子票据</span>
        <strong>{{ shadowTickets.length }}</strong>
        <small>合格 {{ qualifiedTickets.length }}</small>
      </div>
      <div class="metric-card danger">
        <span>阻断候选</span>
        <strong>{{ blockedCount }}</strong>
        <small>{{ firstBlockReason }}</small>
      </div>
      <div class="metric-card locked">
        <span>正式交易</span>
        <strong>锁定</strong>
        <small>formal / auto order 均为 false</small>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>信号链路</h2>
          <p>每个阶段都必须留下数量和说明；任何阻断都不应被误报为可买。</p>
        </div>
      </div>
      <div class="pipeline">
        <div v-for="item in pipeline" :key="item.key || item.title" class="stage" :class="`stage-${item.status || 'idle'}`">
          <div class="stage-title">
            <span>{{ item.title || item.name || item.key }}</span>
            <strong v-if="item.count !== null && item.count !== undefined">{{ item.count }}</strong>
          </div>
          <p>{{ item.detail || item.message || '--' }}</p>
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head table-head">
        <div>
          <h2>全量候选</h2>
          <p>用于审计路由准入、排序、30m 确认、阻断原因和最终影子状态。</p>
        </div>
        <div class="tools">
          <el-select v-model="routeFilter" size="small" style="width: 190px">
            <el-option label="全部路线" value="all" />
            <el-option v-for="item in routeOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
          <el-segmented v-model="candidateMode" :options="modeOptions" size="small" />
        </div>
      </div>
      <el-table v-loading="loading" :data="visibleCandidates" stripe size="small" height="560" empty-text="暂无候选记录">
        <el-table-column prop="code" label="代码" width="110" fixed />
        <el-table-column prop="name" label="名称" min-width="110" fixed />
        <el-table-column label="交易策略" width="150">
          <template #default="{ row }">{{ tradeStrategyName(row) }}</template>
        </el-table-column>
        <el-table-column label="来源路线" width="140">
          <template #default="{ row }">{{ row.route_label || routeName(row.route || row.mode) }}</template>
        </el-table-column>
        <el-table-column label="板块机会" min-width="280" show-overflow-tooltip>
          <template #default="{ row }">{{ opportunityText(row) }}</template>
        </el-table-column>
        <el-table-column label="准入" width="86">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.router_eligible || row.qualified_shadow_buy) ? 'success' : 'warning'">
              {{ truthy(row.router_eligible || row.qualified_shadow_buy) ? '通过' : '阻断' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="30m" width="86">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.m30_confirmed) ? 'success' : 'info'">
              {{ compactStatusWithZh(row.m30_status || (truthy(row.m30_confirmed) ? 'pass' : 'pending_confirm')) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="得分" width="86" align="right">
          <template #default="{ row }">{{ fmt(row.score || row.mode_pick_score || row.v4_score) }}</template>
        </el-table-column>
        <el-table-column label="仓位" width="82" align="right">
          <template #default="{ row }">{{ pct(row.position_pct || row.slot_pct) }}</template>
        </el-table-column>
        <el-table-column label="自然纪律" width="132">
          <template #default="{ row }">
            <el-tag size="small" :type="naturalActionTagType(row)">
              {{ row.natural_action_label || '观察' }}
            </el-tag>
            <small class="natural-position">{{ pct(row.natural_position_pct) }}</small>
          </template>
        </el-table-column>
        <el-table-column prop="planned_entry_ts" label="计划时间" width="160" show-overflow-tooltip />
        <el-table-column prop="confirm_datetime" label="确认时间" width="160" show-overflow-tooltip />
        <el-table-column label="影子状态" width="190" show-overflow-tooltip>
          <template #default="{ row }">{{ compactStatusWithZh(row.shadow_status) }}</template>
        </el-table-column>
        <el-table-column label="自然说明" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">{{ naturalDisciplineText(row) }}</template>
        </el-table-column>
        <el-table-column prop="block_reason" label="阻断/说明" min-width="260" show-overflow-tooltip />
      </el-table>
    </section>

    <section class="two-col">
      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>最终影子票据</h2>
            <p>进入纸面执行前的可交易合同视图。</p>
          </div>
        </div>
        <el-table :data="shadowTickets" stripe size="small" empty-text="暂无影子票据">
          <el-table-column prop="code" label="代码" width="110" />
          <el-table-column prop="name" label="名称" min-width="110" />
          <el-table-column label="交易策略" width="150">
            <template #default="{ row }">{{ tradeStrategyName(row) }}</template>
          </el-table-column>
          <el-table-column prop="route_label" label="来源路线" width="130" />
          <el-table-column label="板块机会" min-width="260" show-overflow-tooltip>
            <template #default="{ row }">{{ opportunityText(row) }}</template>
          </el-table-column>
          <el-table-column label="仓位" width="82" align="right">
            <template #default="{ row }">{{ pct(row.position_pct) }}</template>
          </el-table-column>
          <el-table-column label="自然纪律" width="132">
            <template #default="{ row }">
              <el-tag size="small" :type="naturalActionTagType(row)">
                {{ row.natural_action_label || '观察' }}
              </el-tag>
              <small class="natural-position">{{ pct(row.natural_position_pct) }}</small>
            </template>
          </el-table-column>
          <el-table-column label="硬止损" width="90" align="right">
            <template #default="{ row }">{{ price(row.hard_stop) }}</template>
          </el-table-column>
          <el-table-column prop="exit_contract" label="退出合同" min-width="260" show-overflow-tooltip />
        </el-table>
      </div>

      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>准入检查</h2>
            <p>页面结论必须和运行链路状态一致。</p>
          </div>
        </div>
        <div class="check-list">
          <div v-for="item in readinessChecks" :key="item.key || item.label" class="check-row">
            <el-tag size="small" :type="checkType(item.status)">{{ checkText(item.status) }}</el-tag>
            <div>
              <strong>{{ item.label || item.name || item.key }}</strong>
              <p>{{ item.detail || item.message || '--' }}</p>
            </div>
          </div>
          <el-empty v-if="!readinessChecks.length" description="暂无准入检查" />
        </div>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { compactStatusWithZh, statusWithZh } from '@/utils/g3StatusText'
import { getGen3StateAlphaCurrent } from '@/api/trading'

const loading = ref(false)
const payload = ref({})
const routeFilter = ref('all')
const candidateMode = ref('all')

const modeOptions = [
  { label: '全部', value: 'all' },
  { label: '选中', value: 'selected' },
  { label: '阻断', value: 'blocked' },
  { label: '票据', value: 'tickets' }
]

const summary = computed(() => payload.value.summary || {})
const allCandidates = computed(() => Array.isArray(payload.value.all_source_candidates) ? payload.value.all_source_candidates : [])
const selectedCandidates = computed(() => Array.isArray(payload.value.selected_candidates) ? payload.value.selected_candidates : [])
const blockedCandidates = computed(() => Array.isArray(payload.value.blocked_candidates) ? payload.value.blocked_candidates : [])
const shadowTickets = computed(() => Array.isArray(payload.value.shadow_tickets) ? payload.value.shadow_tickets : [])
const qualifiedTickets = computed(() => Array.isArray(payload.value.qualified_tickets) ? payload.value.qualified_tickets : shadowTickets.value.filter((row) => truthy(row.qualified_shadow_buy)))
const pipeline = computed(() => Array.isArray(payload.value.pipeline) ? payload.value.pipeline : [])
const readinessChecks = computed(() => Array.isArray(payload.value.readiness_checks) ? payload.value.readiness_checks : [])
const blockedCount = computed(() => blockedCandidates.value.length)
const firstBlockReason = computed(() => blockedCandidates.value[0]?.block_reason || blockedCandidates.value[0]?.top_block_reason || '暂无主要阻断')
const statusText = computed(() => `当前 ${statusWithZh(summary.value.diagnosis_code)}，全量 ${allCandidates.value.length}，选中 ${selectedCandidates.value.length}，合格票据 ${qualifiedTickets.value.length}`)

const routeOptions = computed(() => {
  const routes = new Map()
  ;[...allCandidates.value, ...selectedCandidates.value, ...shadowTickets.value].forEach((row) => {
    const value = row.route || row.mode
    if (!value) return
    routes.set(value, row.route_label || routeName(value))
  })
  return Array.from(routes, ([value, label]) => ({ value, label }))
})

const baseCandidates = computed(() => {
  if (candidateMode.value === 'selected') return selectedCandidates.value
  if (candidateMode.value === 'blocked') return blockedCandidates.value
  if (candidateMode.value === 'tickets') return shadowTickets.value
  return allCandidates.value
})

const visibleCandidates = computed(() => {
  if (routeFilter.value === 'all') return baseCandidates.value
  return baseCandidates.value.filter((row) => (row.route || row.mode) === routeFilter.value)
})

function truthy(value) {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0
  if (typeof value === 'string') return ['1', 'true', 'yes', 'ok', 'pass'].includes(value.trim().toLowerCase())
  return false
}

function routeName(route) {
  const names = {
    institutional_mainwave: '机构主升',
    panic_repair: '恐慌修复',
    old_g3_route_v3: '旧G3兼容',
    old_g3_route: '旧G3兼容'
  }
  return names[route] || route || '--'
}

function tradeStrategyName(row) {
  const names = {
    range_weak_repair: '震荡弱势修复',
    panic_capitulation_repair: '恐慌出清修复',
    institutional_score120_mainwave: '机构主升Score120',
    old_g3_strong_breakout: '强势突破',
    mainwave_breakout_offense: '主升/突破进攻',
    volume_runup_supplement: '量能续强补位'
  }
  return row?.trade_strategy_label || names[row?.trade_strategy] || row?.route_strategy_label || routeName(row?.route || row?.mode)
}

function opportunityText(row) {
  if (!row) return '--'
  if (row.opportunity_explain) return row.opportunity_explain
  const route = String(row.route || row.trade_strategy || '')
  const sector = row.sector_name || row.l2_sector_name || row.industry || row.template_label || '未识别板块'
  const diffusion = Number(row.sector_diffusion_score)
  const label = row.sector_diffusion_label || (Number.isFinite(diffusion)
    ? (diffusion >= 80 ? '强扩散' : diffusion >= 65 ? '有效扩散' : diffusion >= 50 ? '观察扩散' : '扩散不足')
    : '扩散未知')
  if (route === 'institutional_mainwave' || route === 'institutional_score120_mainwave') {
    const details = [`机构主升机会落在${sector}`, label]
    if (Number.isFinite(diffusion)) details.push(`扩散分${diffusion.toFixed(1)}`)
    if (row.sector_candidate_count !== undefined && row.sector_candidate_count !== null) details.push(`同板块候选${Number(row.sector_candidate_count).toFixed(0)}只`)
    if (row.same_sector_selected_count >= 2) details.push('同板块共振允许')
    return `${details.join('；')}。`
  }
  return `非机构主升路线；板块${sector}只用于重复暴露检查。`
}

function naturalActionTagType(row) {
  const action = String(row?.natural_action || '')
  if (action === 'skip') return 'danger'
  if (action === 'allow_reduced') return 'warning'
  if (action === 'allow') return 'success'
  return 'info'
}

function naturalDisciplineText(row) {
  if (!row?.code && !row?.code_raw) return '自然纪律：暂无候选。'
  const label = row.natural_action_label || '观察'
  const naturalPct = Number(row.natural_position_pct)
  const pctText = Number.isFinite(naturalPct) ? `，自然仓位 ${pct(naturalPct)}` : ''
  const reason = row.natural_reason || '符合当前自然交易观察合同'
  return `自然纪律：${label}${pctText}；${reason}`
}

function checkType(status) {
  if (status === 'pass') return 'success'
  if (status === 'locked' || status === 'warn') return 'warning'
  return 'danger'
}

function checkText(status) {
  if (status === 'pass') return '通过'
  if (status === 'locked') return '锁定'
  if (status === 'warn') return '观察'
  return '阻断'
}

function fmt(value, digits = 2) {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(digits) : '--'
}

function pct(value) {
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : '--'
}

function price(value) {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(2) : '--'
}

async function load() {
  loading.value = true
  try {
    payload.value = await getGen3StateAlphaCurrent({ limit: 200 })
  } catch (error) {
    ElMessage.warning(error?.message || '读取 G3 候选池失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.g3-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
  color: #1f2a44;
}

.topbar,
.panel,
.metric-card {
  background: #fff;
  border: 1px solid #e4e9f1;
  border-radius: 8px;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 16px;
}

.eyebrow {
  color: #2f80ed;
  font-size: 12px;
  font-weight: 700;
  margin-bottom: 4px;
}

h1,
h2,
p {
  margin: 0;
}

h1 {
  font-size: 26px;
  line-height: 1.2;
}

h2 {
  font-size: 16px;
}

.topbar p,
.panel-head p,
.metric-card small,
.stage p,
.check-row p {
  color: #667085;
  line-height: 1.5;
}

.actions,
.tools {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 10px;
}

.metric-card {
  min-height: 92px;
  padding: 12px;
  border-left: 4px solid #2f80ed;
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.metric-card.accent {
  border-left-color: #12b76a;
}

.metric-card.danger {
  border-left-color: #d92d20;
}

.metric-card.locked {
  border-left-color: #b54708;
}

.metric-card span {
  color: #667085;
  font-size: 12px;
}

.metric-card strong {
  font-size: 21px;
  line-height: 1.2;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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

.pipeline {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.stage {
  min-height: 86px;
  border: 1px solid #e4e9f1;
  border-left: 4px solid #98a2b3;
  border-radius: 8px;
  padding: 10px;
  background: #f8fafc;
}

.stage-pass {
  border-left-color: #12b76a;
}

.stage-blocked {
  border-left-color: #d92d20;
}

.stage-locked {
  border-left-color: #b54708;
}

.stage-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 8px;
}

.stage-title span,
.check-row strong {
  font-size: 14px;
  font-weight: 700;
}

.natural-position {
  display: block;
  margin-top: 4px;
  color: #667085;
  font-size: 12px;
  line-height: 1.2;
}

.two-col {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 14px;
}

.check-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.check-row {
  display: grid;
  grid-template-columns: 54px minmax(0, 1fr);
  gap: 10px;
  align-items: flex-start;
  border-bottom: 1px solid #eef2f6;
  padding-bottom: 10px;
}

@media (max-width: 1320px) {
  .metric-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .pipeline {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 980px) {
  .two-col {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 760px) {
  .topbar,
  .panel-head {
    flex-direction: column;
    align-items: flex-start;
  }

  .metric-grid,
  .pipeline {
    grid-template-columns: 1fr;
  }
}
</style>
