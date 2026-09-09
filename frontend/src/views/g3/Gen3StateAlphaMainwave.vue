<template>
  <div class="g3-page mainwave-page">
    <header class="topbar">
      <div>
        <div class="eyebrow">G3 第三代策略 / institutional_mainwave</div>
        <h1>主升行业机会</h1>
        <p>把行业机会、板块内观察票和正式买入候选分层展示，避免把强板块里的观察票误当成买点。</p>
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
      title="页面分层说明"
    >
      <template #default>
        <span>行业机会来自板块扩散和主升观察池；当前合同主升分门槛 {{ summary.min_score ?? "待加载" }}，同业共振 {{ summary.min_sector_signal_count ?? "待加载" }}，仍需已完成30m确认和指数门槛。</span>
      </template>
    </el-alert>

    <section class="metric-grid">
      <div class="metric-card accent">
        <span>机会交易日</span>
        <strong>{{ summary.entry_date || '--' }}</strong>
        <small>决策日 {{ summary.decision_date || '--' }}</small>
      </div>
      <div class="metric-card">
        <span>行业机会</span>
        <strong>{{ fmt(summary.sector_count, 0) }}</strong>
        <small>重要 {{ fmt(summary.important_sector_count, 0) }} / 强主升 {{ fmt(summary.strong_sector_count, 0) }}</small>
      </div>
      <div class="metric-card accent">
        <span>正式买入票</span>
        <strong>{{ fmt(summary.recommended_count, 0) }}</strong>
        <small>{{ recommendedNames || '暂无正式推荐' }}</small>
      </div>
      <div class="metric-card">
        <span>正式候选</span>
        <strong>{{ fmt(summary.candidate_count, 0) }}</strong>
        <small>30m 通过 {{ fmt(summary.m30_ok_count, 0) }}</small>
      </div>
      <div class="metric-card warning">
        <span>板块观察票</span>
        <strong>{{ fmt(summary.sector_watch_candidate_count, 0) }}</strong>
        <small>观察池 {{ fmt(summary.watch_candidate_count, 0) }}</small>
      </div>
      <div class="metric-card" :class="heatCardClass">
        <span>指数60日热度</span>
        <strong>{{ pct(summary.index_mom60) }}</strong>
        <small>{{ summary.index_heat_label || '--' }}</small>
      </div>
    </section>

    <section class="two-col">
      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>行业机会排序</h2>
            <p>正式候选和观察票分开计数；点击行业后，下方两张表同步过滤。</p>
          </div>
        </div>
        <el-table
          v-loading="loading"
          :data="sectorRows"
          stripe
          size="small"
          highlight-current-row
          empty-text="暂无行业机会"
          @row-click="selectSector"
        >
          <el-table-column label="行业/板块" min-width="140">
            <template #default="{ row }">
              <div class="sector-cell">
                <div class="sector-title-line">
                  <strong>{{ row.sector_name || '--' }}</strong>
                  <span v-if="sectorCode(row)" class="sector-code-tag">{{ sectorCode(row) }}</span>
                </div>
                <small>{{ row.top_candidate_names || '--' }}</small>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="106">
            <template #default="{ row }">
              <el-tag size="small" :type="sectorTagType(row.state)">{{ row.state_label || statusLabel(row.state) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="机会分" width="96" align="right">
            <template #default="{ row }">
              <div class="score-cell">
                <strong>{{ fmt(row.avg_sector_diffusion_score, 1) }}</strong>
                <small v-if="hasTrendAdjustment(row)">原扩散 {{ fmt(row.raw_sector_diffusion_score, 1) }}</small>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="正式" width="72" align="right">
            <template #default="{ row }">{{ fmt(row.candidate_count, 0) }}</template>
          </el-table-column>
          <el-table-column label="观察" width="72" align="right">
            <template #default="{ row }">{{ fmt(row.watch_candidate_count, 0) }}</template>
          </el-table-column>
          <el-table-column label="最高主升" width="94" align="right">
            <template #default="{ row }">{{ fmt(row.max_wave_style_score, 1) }}</template>
          </el-table-column>
        </el-table>
      </div>

      <div class="panel market-panel">
        <div class="panel-head">
          <div>
            <h2>{{ selectedSector || '选择行业' }} 日K走势</h2>
            <p>点击左侧行业后，查看该板块近 260 个交易日的走势、成交量和量价节奏。</p>
          </div>
          <el-tag size="small" :type="selectedKlineRows.length >= 260 ? 'success' : 'warning'">
            {{ fmt(selectedKlineRows.length, 0) }} 日
          </el-tag>
        </div>

        <div class="sector-market-strip">
          <div>
            <span>最新收盘</span>
            <strong>{{ fmt(selectedKlineStats.close, 2) }}</strong>
            <small>{{ selectedKlineStats.synthetic ? '净值代理 ' : '' }}{{ selectedKlineStats.date || '--' }}</small>
          </div>
          <div>
            <span>当日涨跌</span>
            <strong :class="returnClass(selectedKlineStats.changePct)">{{ signedPctValue(selectedKlineStats.changePct) }}</strong>
            <small>板块指数口径</small>
          </div>
          <div>
            <span>20日涨幅</span>
            <strong :class="returnClass(selectedKlineStats.ret20)">{{ signedPct(selectedKlineStats.ret20) }}</strong>
            <small>近期强弱</small>
          </div>
          <div>
            <span>60日涨幅</span>
            <strong :class="returnClass(selectedKlineStats.ret60)">{{ signedPct(selectedKlineStats.ret60) }}</strong>
            <small>主升观察</small>
          </div>
          <div>
            <span>120日涨幅</span>
            <strong :class="returnClass(selectedKlineStats.ret120)">{{ signedPct(selectedKlineStats.ret120) }}</strong>
            <small>中期背景</small>
          </div>
          <div>
            <span>成交额</span>
            <strong>{{ money(selectedKlineStats.amount) }}</strong>
            <small>最新交易日</small>
          </div>
        </div>

        <div v-show="selectedKlineRows.length" ref="sectorChartRef" class="sector-chart"></div>
        <el-empty v-if="!selectedKlineRows.length" description="暂无该板块日K数据" />

        <div class="contract-strip">
          <div>
            <span>正式门槛</span>
            <strong>{{ fmt(summary.min_score, 0) }}</strong>
            <small>主升分</small>
          </div>
          <div>
            <span>同业共振</span>
            <strong>{{ fmt(summary.min_sector_signal_count, 0) }}</strong>
            <small>同日主升骨架数</small>
          </div>
          <div>
            <span>市场热度</span>
            <strong>{{ pct(summary.max_index_mom60) }}</strong>
            <small>高于只观察</small>
          </div>
        </div>

        <div class="component-section">
          <div class="component-head">
            <div>
              <h3>当前板块成分股</h3>
              <small>{{ selectedSector || '--' }} · {{ fmt(selectedComponentRows.length, 0) }} 只</small>
            </div>
          </div>
          <el-table
            :data="selectedComponentRows"
            stripe
            size="small"
            max-height="280"
            empty-text="暂无成分股数据"
          >
            <el-table-column label="代码" width="98" prop="code" />
            <el-table-column label="名称" min-width="108" prop="name" show-overflow-tooltip />
            <el-table-column label="最新价" width="82" align="right">
              <template #default="{ row }">{{ fmt(row.latest_close, 2) }}</template>
            </el-table-column>
            <el-table-column label="涨跌幅" width="84" align="right">
              <template #default="{ row }">
                <span :class="returnClass(row.change_pct)">{{ signedPctValue(row.change_pct) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="成交额" width="92" align="right">
              <template #default="{ row }">{{ money(row.amount) }}</template>
            </el-table-column>
            <el-table-column label="日期" width="96" prop="latest_date" />
          </el-table>
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head table-head">
        <div>
          <h2>{{ selectedSector || '全部' }} 正式候选</h2>
          <p>只展示已经进入 G3 主升候选链或票据链的股票。</p>
        </div>
        <div class="filters">
          <el-select v-model="selectedSector" clearable filterable placeholder="全部行业" size="small">
            <el-option v-for="item in sectorOptions" :key="item" :label="item" :value="item" />
          </el-select>
          <el-segmented v-model="candidateMode" :options="candidateModeOptions" size="small" />
        </div>
      </div>
      <el-table v-loading="loading" :data="filteredCandidates" stripe size="small" empty-text="暂无正式候选">
        <el-table-column label="代码" width="108" prop="code" />
        <el-table-column label="名称" width="116" prop="name" />
        <el-table-column label="行业" width="120" prop="sector_name" />
        <el-table-column label="类型" width="104">
          <template #default="{ row }">
            <el-tag size="small" :type="row.is_recommended ? 'success' : 'info'">
              {{ row.is_recommended ? '推荐买入' : '候选观察' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="模板" min-width="130" prop="template_label" show-overflow-tooltip />
        <el-table-column label="主升分" width="88" align="right">
          <template #default="{ row }">{{ fmt(row.wave_style_score, 1) }}</template>
        </el-table-column>
        <el-table-column label="同业共振" width="88" align="right">
          <template #default="{ row }">{{ fmt(row.sector_signal_count, 0) }}</template>
        </el-table-column>
        <el-table-column label="30m突破" width="88">
          <template #default="{ row }">
            <el-tag size="small" :type="row.m30_confirmed ? 'success' : 'warning'">{{ statusLabel(row.m30_status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="来源" min-width="150">
          <template #default="{ row }">{{ sourceLabel(row.source) }}</template>
        </el-table-column>
      </el-table>
    </section>

    <section class="panel">
      <div class="panel-head table-head">
        <div>
          <h2>{{ selectedSector || '全部' }} 板块内观察票</h2>
          <p>展示强板块里的观察票，例如江化微这类“板块强、个股未达正式买点”的股票。</p>
        </div>
        <div class="filters">
          <el-segmented v-model="watchMode" :options="watchModeOptions" size="small" />
        </div>
      </div>
      <el-table v-loading="loading" :data="filteredWatchCandidates" stripe size="small" empty-text="暂无板块观察票">
        <el-table-column label="代码" width="108" prop="code" />
        <el-table-column label="名称" width="116" prop="name" />
        <el-table-column label="行业" width="120" prop="sector_name" />
        <el-table-column label="标签" width="126" prop="template_label" />
        <el-table-column label="主升分" width="88" align="right">
          <template #default="{ row }">{{ fmt(row.wave_style_score, 1) }}</template>
        </el-table-column>
        <el-table-column label="差正式线" width="92" align="right">
          <template #default="{ row }">{{ fmt(row.score_gap_to_formal, 1) }}</template>
        </el-table-column>
        <el-table-column label="扩散分" width="88" align="right">
          <template #default="{ row }">{{ fmt(row.sector_diffusion_score, 1) }}</template>
        </el-table-column>
        <el-table-column label="5日" width="82" align="right">
          <template #default="{ row }">{{ pct(row.ret5) }}</template>
        </el-table-column>
        <el-table-column label="20日" width="82" align="right">
          <template #default="{ row }">{{ pct(row.ret20) }}</template>
        </el-table-column>
        <el-table-column label="阻断原因" min-width="210" prop="block_reason" show-overflow-tooltip />
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
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from '@/utils/charts'
import { ElMessage } from 'element-plus'
import { getGen3StateAlphaMainwaveOpportunities } from '@/api/trading'

const loading = ref(false)
const payload = ref({})
const selectedSector = ref('')
const candidateMode = ref('all')
const watchMode = ref('sector')
const sectorChartRef = ref(null)
let sectorChart = null

const candidateModeOptions = [
  { label: '全部', value: 'all' },
  { label: '推荐买入', value: 'recommended' },
  { label: '候选观察', value: 'watch' }
]

const watchModeOptions = [
  { label: '强板块', value: 'sector' },
  { label: '全部观察', value: 'all' }
]

const summary = computed(() => payload.value.summary || {})
const cooldown = computed(() => summary.value.mainwave_dynamic_cooldown || {})
const sectorRows = computed(() => Array.isArray(payload.value.sector_opportunities) ? payload.value.sector_opportunities : [])
const sectorKlineByName = computed(() => payload.value.sector_kline_by_name || {})
const candidateRows = computed(() => Array.isArray(payload.value.candidates) ? payload.value.candidates : [])
const watchRows = computed(() => Array.isArray(payload.value.watch_candidates) ? payload.value.watch_candidates : [])
const sectorWatchRows = computed(() => Array.isArray(payload.value.sector_watch_candidates) ? payload.value.sector_watch_candidates : [])
const recommendedRows = computed(() => Array.isArray(payload.value.recommended_tickets) ? payload.value.recommended_tickets : [])
const diagnostics = computed(() => payload.value.diagnostics || {})

const recommendedNames = computed(() => recommendedRows.value.map((item) => item.name || item.code).filter(Boolean).join(' / '))

const heatCardClass = computed(() => {
  const value = Number(summary.value.index_mom60)
  if (!Number.isFinite(value)) return ''
  if (value > 0.1) return 'danger'
  if (value > 0.05) return 'warning'
  return 'accent'
})

const sectorOptions = computed(() => sectorRows.value.map((item) => item.sector_name).filter(Boolean))
const selectedSectorRow = computed(() => sectorRows.value.find((item) => item.sector_name === selectedSector.value) || {})
const selectedKlineRows = computed(() => {
  const fromRow = Array.isArray(selectedSectorRow.value.kline_daily) ? selectedSectorRow.value.kline_daily : []
  if (fromRow.length) return fromRow
  const fromMap = sectorKlineByName.value[selectedSector.value]
  return Array.isArray(fromMap) ? fromMap : []
})
const selectedComponentRows = computed(() => {
  const rows = Array.isArray(selectedSectorRow.value.component_stocks) ? selectedSectorRow.value.component_stocks : []
  return rows.slice().sort((a, b) => {
    const aAmount = Number(a?.amount)
    const bAmount = Number(b?.amount)
    if (Number.isFinite(aAmount) || Number.isFinite(bAmount)) return (Number.isFinite(bAmount) ? bAmount : -1) - (Number.isFinite(aAmount) ? aAmount : -1)
    const aWeight = Number(a?.weight)
    const bWeight = Number(b?.weight)
    if (Number.isFinite(aWeight) || Number.isFinite(bWeight)) return (Number.isFinite(bWeight) ? bWeight : -1) - (Number.isFinite(aWeight) ? aWeight : -1)
    return String(a?.code || '').localeCompare(String(b?.code || ''))
  })
})

function windowReturn(rows, days) {
  if (!Array.isArray(rows) || !rows.length) return null
  const span = rows.slice(-Math.min(Number(days) || rows.length, rows.length))
  if (span.length < 2) return null
  const firstClose = Number(span[0]?.close)
  const lastClose = Number(span[span.length - 1]?.close)
  if (!Number.isFinite(firstClose) || firstClose <= 0 || !Number.isFinite(lastClose)) return null
  return (lastClose / firstClose) - 1
}

const selectedKlineStats = computed(() => {
  const rows = selectedKlineRows.value
  if (!rows.length) return {}
  const last = rows[rows.length - 1] || {}
  const lastClose = Number(last.close)
  return {
    date: last.date || last.trade_date || '',
    close: lastClose,
    changePct: Number(last.change_pct),
    ret20: windowReturn(rows, 20),
    ret60: windowReturn(rows, 60),
    ret120: windowReturn(rows, 120),
    volume: Number(last.volume),
    amount: Number(last.amount),
    synthetic: Boolean(last.synthetic_ohlc)
  }
})

const filteredCandidates = computed(() => {
  return candidateRows.value.filter((item) => {
    if (selectedSector.value && item.sector_name !== selectedSector.value) return false
    if (candidateMode.value === 'recommended') return Boolean(item.is_recommended)
    if (candidateMode.value === 'watch') return !item.is_recommended
    return true
  })
})

const filteredWatchCandidates = computed(() => {
  const base = watchMode.value === 'sector' ? sectorWatchRows.value : watchRows.value
  return base.filter((item) => !selectedSector.value || item.sector_name === selectedSector.value)
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
    mainwave_runtime_blocked_candidates: '机构主升阻断',
    mainwave_watch_pool: '主升观察池',
    current_wave_top_candidates: '观察池Top',
    current_wave_template_pass: '模板通过池'
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
    await nextTick()
    renderSectorChart()
  } catch (error) {
    ElMessage.error(`主升行业机会加载失败：${error?.message || error}`)
  } finally {
    loading.value = false
  }
}

function selectSector(row) {
  selectedSector.value = row?.sector_name || ''
}

function sectorCode(row) {
  const code = String(row?.mainline_sector_code || row?.sector_code || row?.l2_sector_code || '').trim()
  if (code.startsWith('qmt:SW') || code.startsWith('industry:')) return ''
  return code || ''
}

function hasTrendAdjustment(row) {
  const raw = Number(row?.raw_sector_diffusion_score)
  const adjusted = Number(row?.avg_sector_diffusion_score)
  return Number.isFinite(raw) && Number.isFinite(adjusted) && Math.abs(raw - adjusted) >= 0.05
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

function signedPct(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return '--'
  const sign = number > 0 ? '+' : ''
  return `${sign}${(number * 100).toFixed(2)}%`
}

function signedPctValue(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return '--'
  const sign = number > 0 ? '+' : ''
  return `${sign}${number.toFixed(2)}%`
}

function returnClass(value) {
  const number = Number(value)
  if (!Number.isFinite(number) || number === 0) return ''
  return number > 0 ? 'return-up' : 'return-down'
}

function money(value) {
  const number = Number(value)
  if (!Number.isFinite(number) || number <= 0) return '--'
  if (number >= 100000000) return `${(number / 100000000).toFixed(2)}亿`
  if (number >= 10000) return `${(number / 10000).toFixed(2)}万`
  return fmt(number, 0)
}

function renderSectorChart() {
  if (!sectorChartRef.value) return
  const rows = selectedKlineRows.value
  if (!rows.length) {
    if (sectorChart) sectorChart.clear()
    return
  }
  if (!sectorChart) sectorChart = echarts.init(sectorChartRef.value)
  const dates = rows.map((item) => item.date || item.trade_date)
  const candles = rows.map((item) => [Number(item.open), Number(item.close), Number(item.low), Number(item.high)])
  const volumes = rows.map((item, index) => {
    const open = Number(item.open)
    const close = Number(item.close)
    return {
      value: Number(item.volume || 0),
      itemStyle: { color: close >= open ? '#d92d20' : '#039855' },
      name: dates[index]
    }
  })
  const validPrices = rows
    .flatMap((item) => [Number(item.low), Number(item.high)])
    .filter((value) => Number.isFinite(value) && value > 0)
  const minPrice = validPrices.length ? Math.min(...validPrices) : null
  const maxPrice = validPrices.length ? Math.max(...validPrices) : null
  const padding = minPrice !== null && maxPrice !== null ? Math.max((maxPrice - minPrice) * 0.08, maxPrice * 0.01) : 0
  const visibleBars = Math.min(160, rows.length)
  const zoomStart = rows.length > visibleBars ? Math.max(0, ((rows.length - visibleBars) / rows.length) * 100) : 0
  sectorChart.setOption({
    animation: false,
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      formatter(params) {
        const idx = params?.[0]?.dataIndex ?? 0
        const item = rows[idx] || {}
        return [
          `${selectedSector.value || '--'} ${item.date || '--'}`,
          `开 ${fmt(item.open, 2)} 高 ${fmt(item.high, 2)} 低 ${fmt(item.low, 2)} 收 ${fmt(item.close, 2)}`,
          `涨跌 ${signedPctValue(item.change_pct)}`,
          `成交量 ${fmt(item.volume, 0)}`,
          `成交额 ${money(item.amount)}`
        ].join('<br/>')
      }
    },
    grid: [
      { left: 54, right: 18, top: 18, height: '66%' },
      { left: 54, right: 18, top: '80%', height: '12%' }
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: zoomStart, end: 100, filterMode: 'none' },
      { type: 'slider', xAxisIndex: [0, 1], start: zoomStart, end: 100, height: 24, bottom: 2, brushSelect: false, filterMode: 'none' }
    ],
    xAxis: [
      { type: 'category', data: dates, boundaryGap: false, axisLine: { onZero: false }, splitLine: { show: false } },
      { type: 'category', gridIndex: 1, data: dates, boundaryGap: false, axisLabel: { show: false }, axisTick: { show: false }, axisLine: { show: false } }
    ],
    yAxis: [
      { scale: true, min: minPrice === null ? null : Math.max(0, minPrice - padding), max: maxPrice === null ? null : maxPrice + padding },
      { gridIndex: 1, scale: true, axisLabel: { formatter: (value) => money(value) }, splitLine: { show: false } }
    ],
    series: [
      {
        name: '日K',
        type: 'candlestick',
        data: candles,
        barMinWidth: 4,
        barMaxWidth: 12,
        itemStyle: { color: '#d92d20', color0: '#039855', borderColor: '#d92d20', borderColor0: '#039855' }
      },
      {
        name: '成交量',
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: volumes,
        barMinWidth: 3,
        barMaxWidth: 10
      }
    ]
  }, true)
}

function sectorTagType(state) {
  if (state === 'strong_mainwave') return 'success'
  if (state === 'important_industry') return 'warning'
  if (state === 'sector_watch') return 'info'
  return 'info'
}

function statusLabel(value) {
  const raw = String(value || 'unknown')
  const labels = {
    ok: '通过',
    blocked: '阻断',
    missing: '缺失',
    unknown: '未确认',
    not_formal_candidate: '非正式候选'
  }
  return labels[raw] || raw
}

function sourceLabel(value) {
  const raw = String(value || '')
  const labels = {
    next_trade_ticket: '下一交易日票据',
    pre_confirm_preview: '机构主升预确认',
    mainwave_latest_candidates: '机构主升当前候选',
    mainwave_pool_top500: '主升观察池',
    current_wave_top_candidates: '观察池Top'
  }
  return labels[raw] || raw || '--'
}

watch(selectedKlineRows, async () => {
  await nextTick()
  renderSectorChart()
})

function resizeSectorChart() {
  sectorChart?.resize()
}

onMounted(() => {
  window.addEventListener('resize', resizeSectorChart)
  load()
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resizeSectorChart)
  sectorChart?.dispose()
  sectorChart = null
})
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
  grid-template-columns: minmax(420px, 0.9fr) minmax(520px, 1.1fr);
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

.sector-title-line {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.sector-title-line strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sector-code-tag {
  flex: 0 0 auto;
  padding: 1px 5px;
  border: 1px solid #d6e2f5;
  border-radius: 4px;
  background: #f4f8ff;
  color: #476282;
  font-size: 11px;
  line-height: 16px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
}

.sector-cell small,
.recommend-row small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.score-cell {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
  line-height: 1.15;
}

.score-cell strong {
  color: #071a44;
  font-size: 13px;
}

.score-cell small {
  color: #98a2b3;
  font-size: 11px;
  white-space: nowrap;
}

.rule-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.sector-market-strip,
.contract-strip {
  display: grid;
  gap: 10px;
}

.sector-market-strip {
  grid-template-columns: repeat(6, minmax(0, 1fr));
  margin-bottom: 10px;
}

.contract-strip {
  grid-template-columns: repeat(3, minmax(0, 1fr));
  margin-top: 10px;
}

.rule-list > div,
.sector-market-strip > div,
.contract-strip > div,
.artifact-grid > div {
  min-height: 78px;
  padding: 10px;
  border: 1px solid #eef2f6;
  border-radius: 8px;
  background: #f8fafc;
}

.sector-market-strip span,
.contract-strip span {
  display: block;
  color: #667085;
  font-size: 12px;
}

.rule-list strong {
  display: block;
  margin: 6px 0 3px;
  font-size: 18px;
}

.sector-market-strip strong,
.contract-strip strong {
  display: block;
  margin: 6px 0 3px;
  color: #071a44;
  font-size: 17px;
  line-height: 1.2;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sector-chart {
  width: 100%;
  height: 460px;
  min-height: 460px;
}

.component-section {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid #eef2f6;
}

.component-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}

.component-head h3 {
  margin: 0;
  color: #071a44;
  font-size: 15px;
  line-height: 1.2;
}

.component-head small {
  display: block;
  margin-top: 3px;
}

.market-panel {
  min-width: 0;
}

.return-up {
  color: #d92d20 !important;
}

.return-down {
  color: #039855 !important;
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

  .sector-market-strip,
  .contract-strip {
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
  .sector-market-strip,
  .contract-strip,
  .artifact-grid {
    grid-template-columns: 1fr;
  }

  .sector-chart {
    height: 360px;
    min-height: 360px;
  }
}
</style>
