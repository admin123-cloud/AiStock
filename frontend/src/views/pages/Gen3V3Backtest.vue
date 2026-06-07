<template>
  <div class="page">
    <section class="top-band">
      <div>
        <p class="eyebrow">G3 V3 Backtest</p>
        <h1>G3 V3 历史回测</h1>
        <p class="subtitle">
          独立展示第三代 V3 历史曲线、年度稳定性、链路归因和交易明细；只读研究口径，不进入 G2 实盘链路。
        </p>
      </div>
      <div class="actions">
        <el-button :icon="Refresh" :loading="loading" type="primary" @click="fetchData">刷新回测</el-button>
        <router-link to="/gen3/research" custom v-slot="{ navigate }">
          <el-button @click="navigate">研究台</el-button>
        </router-link>
      </div>
    </section>

    <el-alert
      class="panel-alert"
      :type="summary.goal_complete ? 'success' : 'warning'"
      :closable="false"
      show-icon
      :title="statusText"
    />

    <section class="metric-grid">
      <div class="metric-card">
        <span>回测周期</span>
        <strong>{{ overview.start_date || '--' }} 至 {{ overview.end_date || '--' }}</strong>
        <small>{{ overview.trading_days || 0 }} 个交易日</small>
      </div>
      <div class="metric-card strong">
        <span>总收益</span>
        <strong :class="tone(overview.total_return)">{{ pct(overview.total_return) }}</strong>
        <small>最终权益 {{ money(overview.final_equity) }}</small>
      </div>
      <div class="metric-card">
        <span>最大回撤</span>
        <strong class="down">{{ pct(overview.max_drawdown) }}</strong>
        <small>{{ drawdownText }}</small>
      </div>
      <div class="metric-card">
        <span>交易次数</span>
        <strong>{{ overview.trade_count ?? '--' }}</strong>
        <small>平均持仓 {{ fixed(overview.avg_open_positions, 2) }}</small>
      </div>
      <div class="metric-card guard">
        <span>实盘状态</span>
        <strong>研究只读</strong>
        <small>正式买点/下单关闭</small>
      </div>
      <div class="metric-card guard">
        <span>强势 Guard</span>
        <strong>{{ strongGuardLabel }}</strong>
        <small>{{ strongGuardText }}</small>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>收益曲线</h2>
          <p>权益、累计收益和回撤按逐日 MTM 展示，方便直接观察收益来源和回撤段。</p>
        </div>
        <el-tag type="info">{{ curve.length }} days</el-tag>
      </div>
      <div ref="chartRef" class="curve-chart"></div>
    </section>

    <section class="two-col">
      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>年度表现</h2>
            <p>拆开看每一年是否稳定，而不是只靠 2025 以后贡献。</p>
          </div>
        </div>
        <el-table :data="annualRows" stripe size="small" empty-text="暂无年度数据">
          <el-table-column prop="year" label="年份" width="76" />
          <el-table-column label="收益" width="96" align="right">
            <template #default="{ row }"><span :class="tone(row.return)">{{ pct(row.return) }}</span></template>
          </el-table-column>
          <el-table-column label="回撤" width="96" align="right">
            <template #default="{ row }"><span class="down">{{ pct(row.max_drawdown) }}</span></template>
          </el-table-column>
          <el-table-column prop="trade_count" label="交易" width="76" align="right" />
          <el-table-column label="胜率" width="88" align="right">
            <template #default="{ row }">{{ pct(row.win_rate) }}</template>
          </el-table-column>
          <el-table-column label="最差浮亏" min-width="98" align="right">
            <template #default="{ row }"><span class="down">{{ pct(row.worst_open_mtm_ret) }}</span></template>
          </el-table-column>
        </el-table>
      </div>

      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>分窗口稳定性</h2>
            <p>保留 train、valid、blind 和 weak_gap 的窗口审计。</p>
          </div>
        </div>
        <el-table :data="windowRows" stripe size="small" empty-text="暂无窗口数据">
          <el-table-column prop="window" label="窗口" width="140" show-overflow-tooltip />
          <el-table-column prop="profile" label="口径" min-width="210" show-overflow-tooltip />
          <el-table-column label="收益" width="96" align="right">
            <template #default="{ row }"><span :class="tone(row.return)">{{ pct(row.return) }}</span></template>
          </el-table-column>
          <el-table-column label="回撤" width="96" align="right">
            <template #default="{ row }"><span class="down">{{ pct(row.max_drawdown) }}</span></template>
          </el-table-column>
          <el-table-column prop="trade_count" label="交易" width="74" align="right" />
        </el-table>
      </div>
    </section>

    <section class="two-col">
      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>链路归因</h2>
            <p>分别看 down_panic、range_gap、strong_main 的真实贡献。</p>
          </div>
        </div>
        <el-table :data="routeTradeRows" stripe size="small" empty-text="暂无链路数据">
          <el-table-column prop="route" label="链路" width="120" />
          <el-table-column prop="trade_count" label="交易" width="76" align="right" />
          <el-table-column label="胜率" width="92" align="right">
            <template #default="{ row }">{{ pct(row.win_rate) }}</template>
          </el-table-column>
          <el-table-column label="均值" width="92" align="right">
            <template #default="{ row }"><span :class="tone(row.avg_trade_return)">{{ pct(row.avg_trade_return) }}</span></template>
          </el-table-column>
          <el-table-column label="最差" width="92" align="right">
            <template #default="{ row }"><span class="down">{{ pct(row.worst_trade) }}</span></template>
          </el-table-column>
          <el-table-column label="PNL" min-width="100" align="right">
            <template #default="{ row }">{{ money(row.sum_realized_pnl) }}</template>
          </el-table-column>
        </el-table>
      </div>

      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>目标审计</h2>
            <p>明确哪些已通过，哪些还不能当成可实盘结论。</p>
          </div>
        </div>
        <el-table :data="goalRows" stripe size="small" empty-text="暂无目标审计">
          <el-table-column prop="goal" label="目标" width="150" show-overflow-tooltip />
          <el-table-column label="结论" width="116">
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
          <h2>Strong Guard 对照</h2>
          <p>板块/指数逻辑作为 strong route 的环境和质量门槛，不是全局买入条件。</p>
        </div>
        <el-tag type="warning">shadow only</el-tag>
      </div>
      <el-table :data="guardRows" stripe size="small" empty-text="暂无 strong guard 审计">
        <el-table-column prop="guard" label="Guard" min-width="230" show-overflow-tooltip />
        <el-table-column label="收益" width="96" align="right">
          <template #default="{ row }"><span :class="tone(row.total_return)">{{ pct(row.total_return) }}</span></template>
        </el-table-column>
        <el-table-column label="回撤" width="96" align="right">
          <template #default="{ row }"><span class="down">{{ pct(row.max_drawdown) }}</span></template>
        </el-table-column>
        <el-table-column prop="trade_count" label="交易" width="76" align="right" />
        <el-table-column label="胜率" width="88" align="right">
          <template #default="{ row }">{{ pct(row.win_rate) }}</template>
        </el-table-column>
        <el-table-column label="均值" width="88" align="right">
          <template #default="{ row }"><span :class="tone(row.avg_trade_return)">{{ pct(row.avg_trade_return) }}</span></template>
        </el-table-column>
        <el-table-column prop="desc" label="说明" min-width="260" show-overflow-tooltip />
      </el-table>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>交易明细</h2>
          <p>按入场日期倒序展示，便于复盘最近样本和异常交易。</p>
        </div>
        <el-tag type="info">{{ tradePage.total || 0 }} trades</el-tag>
      </div>
      <el-table :data="tradeRows" stripe size="small" empty-text="暂无交易明细">
        <el-table-column prop="entry_date" label="入场日" width="104" />
        <el-table-column prop="stress_exit_date" label="退出日" width="104" />
        <el-table-column prop="route" label="链路" width="116" />
        <el-table-column prop="code" label="代码" width="104" />
        <el-table-column prop="name" label="名称" min-width="110" show-overflow-tooltip />
        <el-table-column label="收益" width="92" align="right">
          <template #default="{ row }"><span :class="tone(row.stress_net_ret)">{{ pct(row.stress_net_ret) }}</span></template>
        </el-table-column>
        <el-table-column label="PNL" width="96" align="right">
          <template #default="{ row }">{{ money(row.realized_pnl) }}</template>
        </el-table-column>
        <el-table-column prop="stress_exit_source" label="退出源" min-width="150" show-overflow-tooltip />
        <el-table-column prop="mixed_profile" label="口径" min-width="230" show-overflow-tooltip />
      </el-table>
    </section>
  </div>
</template>

<script setup>
import * as echarts from 'echarts'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { getGen3RouteExecutionV3Backtest } from '@/api/trading'

const loading = ref(false)
const payload = ref({})
const chartRef = ref(null)
let chart = null

const summary = computed(() => payload.value?.summary || {})
const overview = computed(() => payload.value?.overview || {})
const curve = computed(() => Array.isArray(payload.value?.curve) ? payload.value.curve : [])
const annualRows = computed(() => Array.isArray(payload.value?.annual) ? payload.value.annual : [])
const windowRows = computed(() => Array.isArray(payload.value?.windows) ? payload.value.windows : [])
const routeTradeRows = computed(() => Array.isArray(payload.value?.route_trade_summary) ? payload.value.route_trade_summary : [])
const goalRows = computed(() => Array.isArray(payload.value?.goal_audit) ? payload.value.goal_audit : [])
const guardRows = computed(() => Array.isArray(payload.value?.sector_index_guard_audit) ? payload.value.sector_index_guard_audit : [])
const tradeRows = computed(() => Array.isArray(payload.value?.trades) ? payload.value.trades : [])
const tradePage = computed(() => payload.value?.trade_page || {})
const drawdown = computed(() => payload.value?.drawdown_window || {})

const statusText = computed(() => {
  if (!payload.value?.ok) return 'G3 V3 历史回测数据尚未读取。'
  return '当前为 G3 V3 历史回测研究页，正式买点、自动下单和 G2 实盘链路均关闭。'
})

const drawdownText = computed(() => {
  if (!drawdown.value?.peak_date) return '峰谷区间 --'
  return `${drawdown.value.peak_date} 至 ${drawdown.value.trough_date}`
})

const strongGuardLabel = computed(() => summary.value?.strong_guard?.profile || '未接入')
const strongGuardText = computed(() => {
  const guard = summary.value?.strong_guard
  if (!guard) return '仅读取旧 V3 包'
  const breadth = guard.market_breadth_min ?? '--'
  const score = guard.g3_strong_score_min ?? '--'
  const volume5 = guard.score_volume5_min ?? '--'
  return `广度>${breadth} 分数>${score} volume5>=${volume5}`
})

function num(value) {
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function pct(value) {
  const n = num(value)
  if (n === null) return '--'
  return `${n >= 0 ? '+' : ''}${(n * 100).toFixed(2)}%`
}

function fixed(value, digits = 2) {
  const n = num(value)
  return n === null ? '--' : n.toFixed(digits)
}

function money(value) {
  const n = num(value)
  if (n === null) return '--'
  return n.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}

function tone(value) {
  const n = num(value)
  if (n === null) return ''
  return n >= 0 ? 'up' : 'down'
}

function verdictType(verdict) {
  if (verdict === 'PASS') return 'success'
  if (verdict === 'PARTIAL_PASS' || verdict === 'INFO') return 'warning'
  if (verdict === 'NOT_PASS' || verdict === 'FAIL') return 'danger'
  return 'info'
}

function renderChart() {
  if (!chartRef.value) return
  if (!chart) chart = echarts.init(chartRef.value)
  const rows = curve.value
  const dates = rows.map((row) => row.date)
  chart.setOption({
    grid: [
      { left: 54, right: 28, top: 34, height: 210 },
      { left: 54, right: 28, top: 290, height: 92 }
    ],
    tooltip: {
      trigger: 'axis',
      valueFormatter: (value) => {
        const n = Number(value)
        if (!Number.isFinite(n)) return value
        return Math.abs(n) < 2 ? pct(n) : money(n)
      }
    },
    legend: { top: 0, data: ['权益', '累计收益', '回撤'] },
    xAxis: [
      { type: 'category', data: dates, boundaryGap: false },
      { type: 'category', data: dates, boundaryGap: false, gridIndex: 1 }
    ],
    yAxis: [
      { type: 'value', scale: true },
      { type: 'value', axisLabel: { formatter: (v) => `${(v * 100).toFixed(0)}%` } },
      { type: 'value', gridIndex: 1, axisLabel: { formatter: (v) => `${(v * 100).toFixed(0)}%` } }
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1] },
      { type: 'slider', xAxisIndex: [0, 1], bottom: 4, height: 22 }
    ],
    series: [
      {
        name: '权益',
        type: 'line',
        data: rows.map((row) => row.equity),
        showSymbol: false,
        smooth: true,
        lineStyle: { width: 2, color: '#2563eb' }
      },
      {
        name: '累计收益',
        type: 'line',
        yAxisIndex: 1,
        data: rows.map((row) => row.ret_from_start),
        showSymbol: false,
        smooth: true,
        lineStyle: { width: 1.6, color: '#16a34a' }
      },
      {
        name: '回撤',
        type: 'line',
        xAxisIndex: 1,
        yAxisIndex: 2,
        data: rows.map((row) => row.drawdown),
        showSymbol: false,
        areaStyle: { opacity: 0.12, color: '#dc2626' },
        lineStyle: { width: 1.4, color: '#dc2626' }
      }
    ]
  })
}

async function fetchData() {
  loading.value = true
  try {
    payload.value = await getGen3RouteExecutionV3Backtest({ trade_limit: 160, trade_offset: 0 })
    await nextTick()
    renderChart()
  } catch (error) {
    ElMessage.warning(error?.message || '读取 G3 V3 历史回测失败')
  } finally {
    loading.value = false
  }
}

function handleResize() {
  chart?.resize()
}

onMounted(() => {
  fetchData()
  window.addEventListener('resize', handleResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  chart?.dispose()
  chart = null
})
</script>

<style scoped>
.page { display:flex; flex-direction:column; gap:14px; }
.top-band,.panel,.metric-card { background:#fff; border:1px solid #e6ebff; border-radius:8px; }
.top-band { padding:16px; display:flex; align-items:center; justify-content:space-between; gap:12px; }
.eyebrow { margin:0 0 4px; color:#597ef7; font-size:12px; font-weight:700; text-transform:uppercase; }
.top-band h1 { margin:0 0 6px; color:#1f2a44; font-size:24px; }
.subtitle { margin:0; color:#66708b; font-size:13px; line-height:1.6; }
.actions { display:flex; gap:8px; flex-wrap:wrap; }
.panel-alert { margin-bottom:0; }
.metric-grid { display:grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap:10px; }
.metric-card { padding:12px; min-height:94px; display:flex; flex-direction:column; gap:8px; border-left:4px solid #597ef7; }
.metric-card.strong { border-left-color:#d4380d; }
.metric-card.guard { border-left-color:#faad14; }
.metric-card span { color:#66708b; font-size:12px; }
.metric-card strong { color:#24355d; font-size:18px; line-height:1.25; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.metric-card small { color:#66708b; line-height:1.4; }
.panel { padding:14px; }
.panel-head { display:flex; align-items:flex-start; justify-content:space-between; gap:10px; margin-bottom:12px; }
.panel-head h2 { margin:0 0 6px; font-size:16px; color:#24355d; }
.panel-head p { margin:0; color:#66708b; font-size:13px; line-height:1.6; }
.curve-chart { height:430px; width:100%; }
.two-col { display:grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap:14px; }
.up { color:#d4380d; }
.down { color:#1f9d55; }
@media (max-width: 1280px) {
  .metric-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 900px) {
  .top-band { flex-direction:column; align-items:flex-start; }
  .metric-grid,.two-col { grid-template-columns:1fr; }
}
</style>
