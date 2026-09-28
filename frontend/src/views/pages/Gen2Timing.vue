<template>
  <div class="gen2-timing-page">
    <section class="top-band">
      <div>
        <p class="eyebrow">Gen2 Timing</p>
        <h1>第二代交易系统：交易择时</h1>
        <p class="subtitle">默认用上证指数 K 线解释 G2 四状态、当日是否开仓，以及状态与开仓信号的时间关系，可切换创业板指或深证成指。</p>
      </div>
      <div class="actions">
        <el-date-picker
          v-model="signalDate"
          type="date"
          value-format="YYYY-MM-DD"
          format="YYYY-MM-DD"
          placeholder="选择交易日"
          clearable
          style="width: 180px"
        />
        <el-input-number v-model="curveDays" :min="80" :max="1500" :step="20" />
        <el-button type="primary" :loading="loading" @click="fetchData">查询</el-button>
        <el-button :loading="loading" @click="resetDateAndReload">最新</el-button>
      </div>
    </section>

    <el-alert
      v-if="!available && !loading"
      type="warning"
      :closable="false"
      :title="message || '第二代交易择时数据不可用'"
    />

    <template v-if="available">
      <section class="decision-band" :class="current.is_open_day ? 'decision-band--open' : 'decision-band--closed'">
        <div>
          <span class="date-text">{{ selectedDate }}</span>
          <h2>{{ current.decision_title || '--' }}</h2>
          <p>{{ current.decision_message || '--' }}</p>
        </div>
        <div class="state-pill" :class="`state-pill--${current.state}`">
          <strong>{{ current.state || '--' }}</strong>
          <span>{{ current.state_title || '--' }}</span>
        </div>
      </section>

      <section class="metric-grid">
        <div class="metric">
          <span>当前状态</span>
          <strong>{{ current.state || '--' }}</strong>
          <em>{{ current.state_meaning || '--' }}</em>
        </div>
        <div class="metric">
          <span>建议总仓位</span>
          <strong>{{ current.target_exposure || '--' }}</strong>
          <em>{{ current.can_observe_open ? '允许观察开仓信号' : '不新增仓位' }}</em>
        </div>
        <div class="metric">
          <span>当日开仓信号</span>
          <strong>{{ current.exact_signal_count || 0 }}</strong>
          <em>G2 Open V1 精确匹配数量</em>
        </div>
        <div class="metric">
          <span>图表窗口</span>
          <strong>{{ chartDaily.length }}</strong>
          <em>{{ chartRange }}</em>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2>1) 四个状态的含义</h2>
          <el-tag type="info">OFF / PROBE / NORMAL / AGGRESSIVE</el-tag>
        </div>
        <div class="state-grid">
          <div v-for="item in stateCards" :key="item.state" class="state-card" :class="`state-card--${item.state}`">
            <div class="state-card-head">
              <strong>{{ item.state }}</strong>
              <span>{{ item.title }}</span>
            </div>
            <p>{{ item.meaning }}</p>
            <div class="state-foot">
              <span>仓位：{{ item.target_exposure }}</span>
              <em>{{ item.trade_rule }}</em>
            </div>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2>2) 当日开仓判断</h2>
          <el-tag :type="current.is_open_day ? 'success' : 'danger'">
            {{ current.is_open_day ? '开仓日' : '非开仓日' }}
          </el-tag>
        </div>
        <el-table :data="current.condition_rows || []" stripe size="small" empty-text="暂无判断条件">
          <el-table-column prop="label" label="检查项" min-width="190" />
          <el-table-column prop="value" label="当前值" width="140" />
          <el-table-column prop="reason" label="说明" min-width="280" />
          <el-table-column label="结果" width="110">
            <template #default="{ row }">
              <el-tag :type="row.pass ? 'success' : 'warning'">{{ row.pass ? '通过' : '未通过' }}</el-tag>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="(current.not_open_reasons || []).length" class="reason-list">
          <span v-for="reason in current.not_open_reasons" :key="reason">{{ reason }}</span>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2>3) 状态与指数 K 线</h2>
          <div class="chart-head-actions">
            <el-select v-model="indexCode" size="small" style="width: 150px" @change="fetchData">
              <el-option
                v-for="item in indexOptions"
                :key="item.code"
                :label="item.name"
                :value="item.code"
              />
            </el-select>
            <el-tag type="info">{{ benchmarkTitle }}</el-tag>
          </div>
        </div>
        <div class="state-summary">
          <div v-for="item in stateSummaryCards" :key="item.state" class="summary-chip" :class="`summary-chip--${item.state}`">
            <strong>{{ item.state }}</strong>
            <span>{{ item.days }}天</span>
          </div>
        </div>
        <div ref="chartRef" class="timing-chart"></div>
      </section>

      <section class="panel two-col">
        <div>
          <div class="panel-head compact">
            <h2>四状态回测对照</h2>
            <span class="muted">同一买点，仅切换 g2_open_state</span>
          </div>
          <el-table :data="stateBacktestRows" stripe size="small" empty-text="暂无回测对照">
            <el-table-column prop="state" label="状态" width="110" />
            <el-table-column prop="signal_count" label="信号" width="80" />
            <el-table-column prop="trade_count" label="交易" width="80" />
            <el-table-column label="总收益" width="105"><template #default="{ row }">{{ pct(row.total_return) }}</template></el-table-column>
            <el-table-column label="超额" width="105"><template #default="{ row }">{{ pct(row.excess_return) }}</template></el-table-column>
            <el-table-column label="回撤" width="105"><template #default="{ row }">{{ pct(row.max_drawdown) }}</template></el-table-column>
            <el-table-column label="胜率" width="95"><template #default="{ row }">{{ pct(row.win_rate) }}</template></el-table-column>
          </el-table>
        </div>
        <div>
          <div class="panel-head compact">
            <h2>最近开仓信号</h2>
            <span class="muted">{{ recentSignals.length }} 条</span>
          </div>
          <el-table :data="recentSignals" stripe size="small" height="300" empty-text="暂无信号">
            <el-table-column prop="signal_date" label="日期" width="105" />
            <el-table-column prop="code" label="代码" width="110" />
            <el-table-column prop="name" label="名称" min-width="110" />
            <el-table-column label="3日收益" width="95">
              <template #default="{ row }">{{ pct(row.historical_ret_3d) }}</template>
            </el-table-column>
          </el-table>
        </div>
      </section>
    </template>
  </div>
</template>

<script setup>
import * as echarts from 'echarts'
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { getGen2Timing } from '@/api/trading'

const loading = ref(false)
const available = ref(false)
const message = ref('')
const signalDate = ref('')
const curveDays = ref(420)
const indexCode = ref('999999.SH')
const payload = ref({})
const chartRef = ref(null)
let chart = null

const fallbackIndexOptions = [
  { code: '999999.SH', name: '上证指数' },
  { code: '399006.SZ', name: '创业板指' },
  { code: '399001.SZ', name: '深证成指' }
]

const selectedDate = computed(() => payload.value?.selected_date || '--')
const current = computed(() => payload.value?.current || {})
const chartPayload = computed(() => payload.value?.chart || { daily: [], segments: [], state_counts: {}, signal_markers: [] })
const chartDaily = computed(() => chartPayload.value?.daily || [])
const stateBacktestRows = computed(() => payload.value?.state_backtest_rows || [])
const recentSignals = computed(() => payload.value?.open_signals?.recent_rows || [])
const indexOptions = computed(() => {
  const rows = payload.value?.available_indices
  return Array.isArray(rows) && rows.length ? rows : fallbackIndexOptions
})
const benchmarkTitle = computed(() => {
  const index = chartPayload.value?.index || {}
  return index.name || index.code || '上证指数'
})
const chartRange = computed(() => {
  const rows = chartDaily.value
  if (!rows.length) return '--'
  return `${rows[0].date} ~ ${rows[rows.length - 1].date}`
})

const stateCards = computed(() => {
  const defs = payload.value?.state_definitions || {}
  return ['OFF', 'PROBE', 'NORMAL', 'AGGRESSIVE'].map((state) => ({
    state,
    ...(defs[state] || {})
  }))
})

const stateSummaryCards = computed(() => {
  const counts = chartPayload.value?.state_counts || {}
  return ['OFF', 'PROBE', 'NORMAL', 'AGGRESSIVE'].map((state) => ({
    state,
    days: Number(counts[state] || 0)
  }))
})

function num(value, digits = 2) {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(digits) : '--'
}

function pct(value, digits = 2) {
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(digits)}%` : '--'
}

function stateColor(state, alpha = 0.18) {
  const colors = {
    OFF: `rgba(20, 184, 166, ${alpha})`,
    PROBE: `rgba(59, 130, 246, ${alpha})`,
    NORMAL: `rgba(220, 38, 38, ${alpha})`,
    AGGRESSIVE: `rgba(245, 158, 11, ${alpha})`
  }
  return colors[state] || `rgba(148, 163, 184, ${alpha})`
}

function calcVisiblePriceRange(daily, startPercent = 0, endPercent = 100) {
  if (!Array.isArray(daily) || daily.length === 0) return { min: null, max: null }
  const startIdx = Math.max(0, Math.floor((Number(startPercent || 0) / 100) * daily.length))
  const endIdx = Math.min(daily.length, Math.ceil((Number(endPercent || 100) / 100) * daily.length))
  const visible = daily.slice(startIdx, Math.max(startIdx + 1, endIdx))
  const prices = visible.flatMap((item) => [Number(item.low), Number(item.high)]).filter((item) => Number.isFinite(item) && item > 0)
  if (!prices.length) return { min: null, max: null }
  const min = Math.min(...prices)
  const max = Math.max(...prices)
  const padding = Math.max((max - min) * 0.12, min * 0.01)
  return { min: Math.max(0, min - padding), max: max + padding }
}

function renderChart() {
  if (!chartRef.value) return
  if (!chart) chart = echarts.init(chartRef.value)
  const daily = chartDaily.value
  const dates = daily.map((item) => item.date)
  const candles = daily.map((item) => [item.open, item.close, item.low, item.high])
  const segments = chartPayload.value?.segments || []
  const markers = chartPayload.value?.signal_markers || []
  const defaultVisibleDays = dates.length > 240 ? 180 : Math.max(dates.length, 1)
  const zoomStart = dates.length > defaultVisibleDays ? Math.max(0, ((dates.length - defaultVisibleDays) / dates.length) * 100) : 0
  const initialRange = calcVisiblePriceRange(daily, zoomStart, 100)
  const dailyByDate = new Map(daily.map((item) => [item.date, item]))
  const markAreas = segments.map((seg) => [
    {
      name: seg.state,
      xAxis: seg.start_date,
      itemStyle: { color: stateColor(seg.state, 0.16) }
    },
    { xAxis: seg.end_date }
  ])
  const band = daily.map((item) => ({
    value: item.g2_open_state ? 1 : 0,
    itemStyle: { color: stateColor(item.g2_open_state, 0.42) }
  }))
  const signalPoints = markers
    .filter((item) => dailyByDate.has(item.date))
    .map((item) => {
      const day = dailyByDate.get(item.date) || {}
      return {
        value: [item.date, Number(day.low || day.close || item.price || 0) * 0.985, item.name || '', item.reason || ''],
        label: { show: false }
      }
    })

  chart.setOption({
    animation: false,
    legend: { data: [benchmarkTitle.value, 'G2状态', '开仓信号'], top: 0 },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      formatter(params) {
        if (!params?.length) return ''
        const idx = params[0]?.dataIndex ?? 0
        const item = daily[idx] || {}
        const signalText = markers
          .filter((marker) => marker.date === item.date)
          .map((marker) => `${marker.name || '--'}：${marker.reason || '--'}`)
          .join('<br/>')
        return [
          item.date || '--',
          `${benchmarkTitle.value} O:${num(item.open)} H:${num(item.high)} L:${num(item.low)} C:${num(item.close)}`,
          `G2状态: ${item.g2_open_state || '--'}`,
          `目标仓位: ${pct(item.target_exposure_min, 0)} ~ ${pct(item.target_exposure_max, 0)}`,
          `G2代理(中证1000) MA20:${num(item.csi1000_ma20)} MA60:${num(item.csi1000_ma60)} MA120:${num(item.csi1000_ma120)}`,
          `广度: ${pct(item.breadth_ma20)} / MA20斜率5日: ${num(item.csi1000_ma20_slope5, 4)}`,
          `风险确认: ${item.risk_index_ok ? '通过' : '未通过'} / 因子风险偏好: ${item.factor_risk_on ? '通过' : '未通过'}`,
          `开仓信号: ${signalText || '--'}`
        ].join('<br/>')
      }
    },
    grid: [
      { left: 52, right: 18, top: 44, height: '64%' },
      { left: 52, right: 18, top: '80%', height: '8%' }
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: zoomStart, end: 100, filterMode: 'none' },
      { type: 'slider', xAxisIndex: [0, 1], start: zoomStart, end: 100, height: 24, bottom: 10, brushSelect: false, filterMode: 'none' }
    ],
    xAxis: [
      { type: 'category', data: dates, boundaryGap: false, axisLine: { onZero: false }, splitLine: { show: false } },
      { type: 'category', gridIndex: 1, data: dates, boundaryGap: false, axisLabel: { show: false }, axisTick: { show: false }, axisLine: { show: false } }
    ],
    yAxis: [
      { scale: true, min: initialRange.min, max: initialRange.max },
      { gridIndex: 1, min: 0, max: 1, axisLabel: { show: false }, axisTick: { show: false }, splitLine: { show: false } }
    ],
    series: [
      {
        name: benchmarkTitle.value,
        type: 'candlestick',
        data: candles,
        itemStyle: { color: '#dc2626', color0: '#16a34a', borderColor: '#dc2626', borderColor0: '#16a34a' },
        markArea: { silent: true, data: markAreas, label: { show: false } }
      },
      {
        name: '开仓信号',
        type: 'scatter',
        data: signalPoints,
        symbol: 'triangle',
        symbolSize: 14,
        itemStyle: { color: '#7c3aed', borderColor: '#fff', borderWidth: 1 },
        z: 12
      },
      { name: 'G2状态', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: band, barWidth: '100%', silent: true }
    ]
  }, true)

  chart.off('datazoom')
  chart.on('datazoom', () => {
    const option = chart.getOption()
    const zoom = Array.isArray(option?.dataZoom) && option.dataZoom.length ? option.dataZoom[0] : {}
    const range = calcVisiblePriceRange(daily, zoom?.start ?? 0, zoom?.end ?? 100)
    chart.setOption({ yAxis: [{ min: range.min, max: range.max }, {}] })
  })
}

async function fetchData() {
  loading.value = true
  try {
    const data = await getGen2Timing({ signal_date: signalDate.value || undefined, curve_days: curveDays.value, index_code: indexCode.value })
    payload.value = data || {}
    indexCode.value = data?.selected_index_code || data?.chart?.index?.code || indexCode.value
    available.value = !!data?.available
    message.value = data?.message || ''
    await nextTick()
    renderChart()
  } catch (error) {
    available.value = false
    message.value = error?.message || '读取第二代交易择时失败'
  } finally {
    loading.value = false
  }
}

async function resetDateAndReload() {
  signalDate.value = ''
  await fetchData()
}

function resizeChart() {
  chart?.resize()
}

onMounted(() => {
  fetchData()
  window.addEventListener('resize', resizeChart)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resizeChart)
  chart?.dispose()
  chart = null
})
</script>

<style scoped>
.gen2-timing-page { display: flex; flex-direction: column; gap: 16px; }
.top-band { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; padding: 24px 28px; background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; }
.eyebrow { margin: 0 0 8px; font-size: 12px; color: #64748b; text-transform: uppercase; letter-spacing: 0; }
h1 { margin: 0; font-size: 28px; color: #172033; }
.subtitle { margin: 8px 0 0; color: #5b667a; line-height: 1.6; }
.actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.decision-band { display: flex; justify-content: space-between; gap: 16px; align-items: center; padding: 20px 22px; border: 1px solid #e2e8f0; border-radius: 8px; background: #fff; }
.decision-band--open { background: #fff5f5; border-color: #fecaca; }
.decision-band--closed { background: #f8fafc; border-color: #dbe4ef; }
.date-text { display: block; margin-bottom: 6px; color: #64748b; font-size: 13px; }
h2 { margin: 0; font-size: 18px; color: #172033; }
.decision-band h2 { font-size: 28px; }
.decision-band p { margin: 8px 0 0; color: #475569; }
.state-pill { min-width: 150px; padding: 14px 16px; border-radius: 8px; text-align: center; border: 1px solid #dbe4ef; background: #fff; }
.state-pill strong { display: block; font-size: 24px; color: #172033; }
.state-pill span { display: block; margin-top: 4px; color: #64748b; font-size: 13px; }
.state-pill--NORMAL { border-color: #fecaca; background: #fff1f2; }
.state-pill--OFF { border-color: #99f6e4; background: #f0fdfa; }
.state-pill--PROBE { border-color: #bfdbfe; background: #eff6ff; }
.state-pill--AGGRESSIVE { border-color: #fed7aa; background: #fff7ed; }
.metric-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }
.metric, .panel { background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; }
.metric { padding: 16px; display: flex; flex-direction: column; gap: 6px; }
.metric span { color: #64748b; font-size: 12px; }
.metric strong { color: #172033; font-size: 24px; }
.metric em { color: #64748b; font-size: 12px; font-style: normal; line-height: 1.5; }
.panel { padding: 18px 20px; min-width: 0; }
.panel-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.panel-head.compact { margin-bottom: 10px; }
.chart-head-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.state-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
.state-card { padding: 14px; border: 1px solid #e2e8f0; border-radius: 8px; background: #fbfdff; }
.state-card-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin-bottom: 8px; }
.state-card-head strong { font-size: 18px; color: #172033; }
.state-card-head span { color: #475569; font-size: 13px; font-weight: 700; }
.state-card p { min-height: 66px; margin: 0; color: #475569; line-height: 1.6; }
.state-foot { display: flex; flex-direction: column; gap: 4px; margin-top: 10px; padding-top: 10px; border-top: 1px solid #e2e8f0; color: #64748b; font-size: 12px; }
.state-foot em { font-style: normal; color: #334155; }
.state-card--NORMAL { background: #fff7f7; border-color: #fecaca; }
.state-card--OFF { background: #f0fdfa; border-color: #99f6e4; }
.state-card--PROBE { background: #eff6ff; border-color: #bfdbfe; }
.state-card--AGGRESSIVE { background: #fff7ed; border-color: #fed7aa; }
.reason-list { display: flex; flex-direction: column; gap: 6px; margin-top: 12px; color: #b45309; font-size: 13px; }
.state-summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; margin-bottom: 12px; }
.summary-chip { display: flex; justify-content: space-between; align-items: center; padding: 10px 12px; border-radius: 8px; border: 1px solid #e2e8f0; }
.summary-chip strong { color: #172033; }
.summary-chip span { color: #64748b; }
.summary-chip--NORMAL { background: #fff1f2; border-color: #fecaca; }
.summary-chip--OFF { background: #f0fdfa; border-color: #99f6e4; }
.summary-chip--PROBE { background: #eff6ff; border-color: #bfdbfe; }
.summary-chip--AGGRESSIVE { background: #fff7ed; border-color: #fed7aa; }
.timing-chart { width: 100%; height: 560px; }
.two-col { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(360px, 0.8fr); gap: 16px; }
.muted { color: #64748b; font-size: 13px; }
@media (max-width: 1080px) {
  .top-band, .decision-band { flex-direction: column; align-items: flex-start; }
  .two-col { grid-template-columns: 1fr; }
  .timing-chart { height: 480px; }
}
</style>
