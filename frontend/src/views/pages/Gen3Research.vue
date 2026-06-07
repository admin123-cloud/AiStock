<template>
  <div class="page">
    <div class="head-card">
      <div>
        <h1>G3 第三代策略研究台</h1>
        <p>独立展示第三代策略候选、压力测试和可见性审计；当前固定研究观察，不进入 G2 实盘交易链路。</p>
      </div>
      <div class="actions">
        <el-button type="primary" :loading="loading" @click="fetchData">刷新G3研究</el-button>
        <el-button :loading="refreshingV2" @click="refreshV2Shadow">刷新V2影子源</el-button>
      </div>
    </div>

    <el-alert
      class="panel-alert"
      :type="overallAlertType"
      :closable="false"
      show-icon
      :title="overallMessage"
    />

    <section class="metric-grid">
      <div class="metric-card strong">
        <span>最新候选</span>
        <strong>{{ v3Summary.candidate || '--' }}</strong>
        <small>{{ v3Summary.generated_at || '--' }}</small>
      </div>
      <div class="metric-card">
        <span>V3主口径</span>
        <strong>{{ pct(v3Summary.main_route_execution_profile?.total_return) }}</strong>
        <small>回撤 {{ pct(v3Summary.main_route_execution_profile?.max_drawdown) }}</small>
      </div>
      <div class="metric-card">
        <span>保守100bps</span>
        <strong>{{ pct(v3Summary.conservative_route_execution_profile?.total_return) }}</strong>
        <small>回撤 {{ pct(v3Summary.conservative_route_execution_profile?.max_drawdown) }}</small>
      </div>
      <div class="metric-card">
        <span>weak_gap 2022-2024</span>
        <strong>{{ pct(v3Summary.weak_gap_2022_2024_conservative_return) }}</strong>
        <small>保守口径</small>
      </div>
      <div class="metric-card guard">
        <span>实盘状态</span>
        <strong>研究观察</strong>
        <small>下单/正式买点关闭</small>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>候选版本对比</h2>
          <p>V3 继承 V2 选股体系，只改变 down/range 的执行约束；V2 仍作为影子源对照。</p>
        </div>
        <el-tag type="info">shadow only</el-tag>
      </div>
      <el-table :data="profileRows" stripe size="small" empty-text="暂无G3候选版本数据">
        <el-table-column prop="profile" label="口径" min-width="260" show-overflow-tooltip />
        <el-table-column label="交易数" width="90">
          <template #default="{ row }">{{ row.trade_count ?? '--' }}</template>
        </el-table-column>
        <el-table-column label="收益" width="110">
          <template #default="{ row }">{{ pct(row.total_return) }}</template>
        </el-table-column>
        <el-table-column label="回撤" width="110">
          <template #default="{ row }">{{ pct(row.max_drawdown) }}</template>
        </el-table-column>
        <el-table-column label="胜率" width="100">
          <template #default="{ row }">{{ pct(row.win_rate) }}</template>
        </el-table-column>
        <el-table-column label="最差单笔" width="110">
          <template #default="{ row }">{{ pct(row.worst_trade) }}</template>
        </el-table-column>
      </el-table>
    </section>

    <section class="two-col">
      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>目标审计</h2>
            <p>按当前目标逐条确认完成度，不把研究进展误判为可实盘。</p>
          </div>
        </div>
        <el-table :data="goalRows" stripe size="small" empty-text="暂无目标审计">
          <el-table-column prop="goal" label="目标" min-width="130" />
          <el-table-column label="结论" width="110">
            <template #default="{ row }">
              <el-tag :type="verdictType(row.verdict)" effect="light">{{ row.verdict || '--' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="evidence" label="证据" min-width="280" show-overflow-tooltip />
        </el-table>
      </div>

      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>退出可见性</h2>
            <p>检查 down/range 同日退出是否依赖未来函数。</p>
          </div>
          <el-tag :type="v3Summary.visibility_verdict === 'PASS' ? 'success' : 'danger'">
            {{ v3Summary.visibility_verdict || 'UNKNOWN' }}
          </el-tag>
        </div>
        <el-table :data="visibilityRows" stripe size="small" empty-text="暂无可见性审计">
          <el-table-column prop="route" label="链路" width="110" />
          <el-table-column prop="item" label="审计项" width="170" show-overflow-tooltip />
          <el-table-column prop="value" label="结果" width="110" />
          <el-table-column label="结论" width="90">
            <template #default="{ row }">
              <el-tag :type="verdictType(row.verdict)" effect="light">{{ row.verdict || '--' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="evidence" label="证据" min-width="260" show-overflow-tooltip />
        </el-table>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>分窗口稳定性</h2>
          <p>重点看 weak_gap、valid、blind 是否都能站住。</p>
        </div>
      </div>
      <el-table :data="windowRows" stripe size="small" empty-text="暂无分窗口数据">
        <el-table-column prop="profile" label="口径" min-width="230" show-overflow-tooltip />
        <el-table-column prop="window" label="窗口" width="150" />
        <el-table-column label="收益" width="100">
          <template #default="{ row }">{{ pct(row.return) }}</template>
        </el-table-column>
        <el-table-column label="回撤" width="100">
          <template #default="{ row }">{{ pct(row.max_drawdown) }}</template>
        </el-table-column>
        <el-table-column prop="trade_count" label="交易" width="80" />
        <el-table-column label="胜率" width="100">
          <template #default="{ row }">{{ pct(row.win_rate) }}</template>
        </el-table-column>
      </el-table>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>链路归因</h2>
          <p>确认强势、弱势、震荡三条链路分别贡献，而不是靠单一年份或单一路径撑起曲线。</p>
        </div>
      </div>
      <el-table :data="routeRows" stripe size="small" empty-text="暂无链路归因">
        <el-table-column prop="profile" label="口径" min-width="230" show-overflow-tooltip />
        <el-table-column prop="route" label="链路" width="120" />
        <el-table-column prop="trade_count" label="交易" width="80" />
        <el-table-column label="胜率" width="100">
          <template #default="{ row }">{{ pct(row.win_rate) }}</template>
        </el-table-column>
        <el-table-column label="均值" width="100">
          <template #default="{ row }">{{ pct(row.avg_trade_return) }}</template>
        </el-table-column>
        <el-table-column label="最差" width="100">
          <template #default="{ row }">{{ pct(row.worst_trade) }}</template>
        </el-table-column>
      </el-table>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>今日影子候选</h2>
          <p>{{ v2SummaryText }}</p>
        </div>
        <el-tag :type="v2TodayRows.length ? 'warning' : 'success'">{{ v2TodayRows.length ? '仅观察' : '今日无候选' }}</el-tag>
      </div>
      <el-table :data="v2TodayRows" stripe size="small" empty-text="今日暂无G3影子候选">
        <el-table-column prop="route" label="链路" width="120" />
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="120" />
        <el-table-column prop="entry_date" label="入场日" width="112" />
        <el-table-column prop="confirm_datetime" label="确认时间" width="160" show-overflow-tooltip />
        <el-table-column prop="block_reason" label="状态说明" min-width="260" show-overflow-tooltip />
      </el-table>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getGen3RangeFilteredShadow, getGen3RouteExecutionV3 } from '@/api/trading'

const loading = ref(false)
const refreshingV2 = ref(false)
const v3 = ref(null)
const v2 = ref(null)

const v3Summary = computed(() => v3.value?.summary || {})
const v2Summary = computed(() => v2.value?.summary || {})
const profileRows = computed(() => Array.isArray(v3.value?.profiles) ? v3.value.profiles : [])
const goalRows = computed(() => Array.isArray(v3.value?.goal_audit) ? v3.value.goal_audit : [])
const visibilityRows = computed(() => Array.isArray(v3.value?.visibility_audit) ? v3.value.visibility_audit : [])
const windowRows = computed(() => Array.isArray(v3.value?.windows) ? v3.value.windows : [])
const routeRows = computed(() => Array.isArray(v3.value?.route_attribution) ? v3.value.route_attribution : [])
const v2TodayRows = computed(() => Array.isArray(v2.value?.candidates) ? v2.value.candidates : [])

const overallAlertType = computed(() => {
  if (v3Summary.value.goal_complete === true) return 'success'
  if (goalRows.value.some((row) => row.verdict === 'NOT_PASS')) return 'warning'
  return 'info'
})

const overallMessage = computed(() => {
  if (!v3.value) return 'G3第三代策略研究状态尚未读取。'
  return 'G3 V3 已形成独立研究候选包，但仍未接实盘。下一关是同日真实成交偏差压力测试。'
})

const v2SummaryText = computed(() => {
  const d = v2Summary.value || {}
  return `V2影子源：payload ${d.payload_rows ?? '--'}；今日 ${d.today_shadow_candidates ?? 0}；最新 ${d.latest_entry_date || '--'}；自动交易关闭。`
})

function pct(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  return `${n >= 0 ? '+' : ''}${(n * 100).toFixed(2)}%`
}

function verdictType(verdict) {
  if (verdict === 'PASS') return 'success'
  if (verdict === 'PARTIAL_PASS' || verdict === 'INFO') return 'warning'
  if (verdict === 'NOT_PASS' || verdict === 'FAIL') return 'danger'
  return 'info'
}

async function fetchData() {
  loading.value = true
  try {
    const [v3Resp, v2Resp] = await Promise.all([
      getGen3RouteExecutionV3({ limit: 30 }),
      getGen3RangeFilteredShadow({ refresh: false, limit: 30 })
    ])
    v3.value = v3Resp
    v2.value = v2Resp
  } catch (error) {
    ElMessage.warning(error?.message || '读取G3研究数据失败')
  } finally {
    loading.value = false
  }
}

async function refreshV2Shadow() {
  refreshingV2.value = true
  try {
    v2.value = await getGen3RangeFilteredShadow({ refresh: true, limit: 30 })
    ElMessage.success('G3 V2影子源已刷新')
  } catch (error) {
    ElMessage.warning(error?.message || '刷新G3 V2影子源失败')
  } finally {
    refreshingV2.value = false
  }
}

onMounted(fetchData)
</script>

<style scoped>
.page { display:flex; flex-direction:column; gap:14px; }
.head-card,.panel,.metric-card { background:#fff; border:1px solid #e6ebff; border-radius:8px; }
.head-card { padding:16px; display:flex; align-items:center; justify-content:space-between; gap:12px; }
.head-card h1 { margin:0 0 6px; color:#1f2a44; font-size:24px; }
.head-card p { margin:0; color:#66708b; font-size:13px; line-height:1.6; }
.actions { display:flex; gap:8px; flex-wrap:wrap; }
.panel-alert { margin-bottom:0; }
.metric-grid { display:grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap:10px; }
.metric-card { padding:12px; min-height:94px; display:flex; flex-direction:column; gap:8px; border-left:4px solid #597ef7; }
.metric-card.strong { border-left-color:#d4380d; }
.metric-card.guard { border-left-color:#faad14; }
.metric-card span { color:#66708b; font-size:12px; }
.metric-card strong { color:#24355d; font-size:20px; line-height:1.2; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.metric-card small { color:#66708b; line-height:1.4; }
.panel { padding:14px; }
.panel-head { display:flex; align-items:flex-start; justify-content:space-between; gap:10px; margin-bottom:12px; }
.panel-head h2 { margin:0 0 6px; font-size:16px; color:#24355d; }
.panel-head p { margin:0; color:#66708b; font-size:13px; line-height:1.6; }
.two-col { display:grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap:14px; }
@media (max-width: 1280px) {
  .metric-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 840px) {
  .head-card { flex-direction:column; align-items:flex-start; }
  .metric-grid,.two-col { grid-template-columns: 1fr; }
}
</style>
