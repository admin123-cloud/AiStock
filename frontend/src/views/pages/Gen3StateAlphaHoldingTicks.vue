<template>
  <div class="tick-page">
    <header class="topbar">
      <div>
        <div class="eyebrow">G3第三代策略 / 只读行情</div>
        <h1>做T交易</h1>
        <p>结合 QMT 五档盘口与本地 5 分钟确认，识别做T的卖出与回补时机、盘口承接和行情异常；本页不调用下单接口。</p>
      </div>
      <div class="actions">
        <el-tag v-if="trackedCodes.length" type="success" effect="plain">持续跟踪 {{ trackedCodes.length }} 只 / 5秒</el-tag>
        <el-tag v-if="loading" type="info" effect="plain">异步刷新中</el-tag>
        <el-button type="primary" :loading="loading" @click="load">刷新 Tick</el-button>
      </div>
    </header>

    <el-alert class="read-only-alert" type="info" :closable="false" show-icon>
      <template #title>常态建议需同一方向连续 3 次 Tick（约15秒）才切换；实时信号可变化，但不能单独推翻常态判断。仍须核实5分钟、30分钟结构、板块状态及可用仓位；页面不自动下单。</template>
    </el-alert>

    <section class="panel">
      <div class="panel-head"><div><h2>手动仓位确认</h2><p>目标：两只各 50%，单次调整总资金 25%。仅记录你的确认，不会下单。</p></div></div>
      <div class="position-actions"><div v-for="(position, code) in portfolioState.positions || {}" :key="code" class="position-card"><strong>{{ code }}</strong><span>当前 {{ position.current_weight_pct || 0 }}% / 目标 {{ position.target_weight_pct || 50 }}%</span><el-button type="primary" plain size="small" @click="openPositionConfirmation(code, 'buy')">确认买入 25%</el-button><el-button type="danger" plain size="small" @click="openPositionConfirmation(code, 'sell')">确认卖出 25%</el-button></div></div>
    </section>

    <el-dialog v-model="positionConfirmationOpen" :title="`确认${positionConfirmationLabel} 25%`" width="420px" :close-on-click-modal="false">
      <el-alert type="info" :closable="false" show-icon title="只记录手动确认，不会向券商提交委托。请按实际成交填写价格和数量。" />
      <el-form class="position-confirmation-form" label-position="top">
        <el-form-item label="标的"><el-input :model-value="positionConfirmation.code" disabled /></el-form-item>
        <el-form-item label="成交价格（元）" required><el-input-number v-model="positionConfirmation.price" :min="0.001" :precision="3" controls-position="right" /></el-form-item>
        <el-form-item label="成交数量（股）" required><el-input-number v-model="positionConfirmation.shares" :min="1" :precision="0" :step="100" controls-position="right" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="positionConfirmationOpen = false">取消</el-button>
        <el-button :type="positionConfirmation.side === 'buy' ? 'primary' : 'danger'" :loading="positionConfirmationSubmitting" @click="submitPositionConfirmation">确认{{ positionConfirmationLabel }}</el-button>
      </template>
    </el-dialog>

    <section class="panel controls">
      <div>
        <h2>观察标的</h2>
        <p>留空则读取账户持仓快照；可一次输入多只自选或持仓代码，以逗号、空格或换行分隔。</p>
      </div>
      <div class="code-control">
        <el-input v-model="codes" clearable placeholder="例如：002245,300037,300054" @keyup.enter="load" />
        <el-button :loading="loading" @click="load">分析</el-button>
        <el-button type="primary" plain :disabled="!batchCodes.length && !items.some(item => item.available)" @click="trackBatch">批量持续跟踪</el-button>
        <el-button v-if="trackedCodes.length" type="danger" plain @click="stopAllTracking">停止全部跟踪</el-button>
      </div>
    </section>

    <el-alert v-if="warnings.length" class="warning-alert" type="warning" :closable="false" show-icon>
      <template #title>{{ warnings.join('；') }}</template>
    </el-alert>

    <section class="metric-grid">
      <div class="metric-card accent">
        <span>行情来源</span>
        <strong>QMT {{ source.port || '--' }}</strong>
        <small>{{ source.host || '--' }}</small>
      </div>
      <div class="metric-card" :class="items.length ? 'success' : 'warning'">
        <span>可用 Tick</span>
        <strong>{{ availableCount }}/{{ items.length }}</strong>
        <small>只读快照</small>
      </div>
      <div class="metric-card">
        <span>账户快照时间</span>
        <strong class="date-value">{{ brokerUpdatedAt || '--' }}</strong>
        <small>不在盘中自动同步账户</small>
      </div>
      <div class="metric-card locked">
        <span>下单路径</span>
        <strong>已锁定</strong>
        <small>Tick 页面不产生委托</small>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>持仓盘口与异常诊断</h2>
          <p>“五档失衡”=(买五总量−卖五总量)/(买五总量+卖五总量)。仅持续异常且经5分钟确认后才应进入风控复核。</p>
        </div>
      </div>
      <el-table :data="items" stripe size="small" @row-click="openChart" empty-text="暂无可分析标的，请输入代码或先刷新账户持仓。">
        <el-table-column label="标的 / Tick" width="168">
          <template #default="{ row }">
            <strong>{{ row.name || row.code }}</strong><small>{{ row.code }}</small>
            <small :class="row.tick_fresh ? 'fresh' : 'stale'">{{ tickAge(row) }} · {{ row.tick_time || '--' }}</small>
          </template>
        </el-table-column>
        <el-table-column label="价格趋势" width="170" align="right">
          <template #default="{ row }">
            <strong>{{ price(row.last_price) }}</strong><span :class="returnClass(row.change_pct)"> {{ changePct(row.change_pct) }}</span>
            <small>区间 {{ price(row.low) }} - {{ price(row.high) }}</small>
            <small>当日 VWAP {{ price(row.vwap_5m) }}</small>
          </template>
        </el-table-column>
        <el-table-column label="RSI 情绪" width="136" align="right">
          <template #default="{ row }">
            <span :class="rsiClass(row.rsi_5m_14)">5m {{ rsi(row.rsi_5m_14) }}</span><small>{{ rsiState(row.rsi_5m_14) }}</small>
            <span :class="rsiClass(row.rsi_15m_14)">15m {{ rsi(row.rsi_15m_14) }}</span><small>{{ rsiState(row.rsi_15m_14) }}</small>
          </template>
        </el-table-column>
        <el-table-column label="盘口" width="145" align="right">
          <template #default="{ row }">
            <span>{{ price(row.bid1) }} / {{ price(row.ask1) }}</span>
            <small :class="returnClass(row.order_book_imbalance)">五档 {{ ratioPct(row.order_book_imbalance) }}</small>
          </template>
        </el-table-column>
        <el-table-column label="诊断 / 常态建议" min-width="390">
          <template #default="{ row }">
            <el-tag :type="row.level || 'info'" size="small">{{ row.diagnosis || '行情缺失' }}</el-tag>
            <el-tag :type="actionSuggestion(row).type" size="small">{{ actionSuggestion(row).label }}</el-tag>
            <el-button v-if="isTracked(row.code)" type="danger" plain size="small" @click.stop="stopTracking(row.code)">停止跟踪</el-button>
            <el-button v-else type="primary" plain size="small" :disabled="!row.available" @click.stop="startTracking(row.code)">持续跟踪</el-button>
            <el-button link type="primary" size="small" @click.stop="openChart(row)">查看K线</el-button>
            <small class="action-detail">{{ actionSuggestion(row).detail }}</small>
            <small v-if="row.anomalies?.length" class="anomaly-detail">{{ row.anomalies.join('；') }}</small>
          </template>
        </el-table-column>
      </el-table>
    </section>
    <section class="panel"><div class="panel-head"><div><h2>模拟做T记录</h2><p>点击“编辑”后填写模拟价格和数量，再点击该行“保存”。</p></div><el-button type="primary" size="small" @click="saveSimRecords">保存全部</el-button></div><el-table :data="portfolioState.events || []" size="small" empty-text="暂无模拟做T记录"><el-table-column prop="at" label="确认时间" width="170" /><el-table-column prop="code" label="标的" width="110" /><el-table-column prop="side" label="方向" width="80" /><el-table-column prop="weight_pct" label="仓位" width="80"><template #default="{ row }">{{ row.weight_pct }}%</template></el-table-column><el-table-column label="模拟买卖价" width="150"><template #default="{ row }"><el-input-number v-model="row.price" :disabled="!row._editing" :min="0" :precision="3" size="small" /></template></el-table-column><el-table-column label="数量" width="140"><template #default="{ row }"><el-input-number v-model="row.shares" :disabled="!row._editing" :min="0" :step="100" size="small" /></template></el-table-column><el-table-column prop="after_weight_pct" label="确认后仓位" width="110"><template #default="{ row }">{{ row.after_weight_pct }}%</template></el-table-column><el-table-column label="操作" width="145"><template #default="{ row }"><el-button v-if="!row._editing" link type="primary" @click="editSimRecord(row)">编辑</el-button><el-button v-else type="primary" size="small" @click="saveSimRecord(row)">保存</el-button></template></el-table-column></el-table></section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>板块跟随确认</h2>
          <p>从 QMT 同步的行业成分中，按最近成交额抽样至多 10 只同板块股票。至少 4 只可用行情才给出结论；它只用于确认个股走势，不产生买卖委托。</p>
        </div>
      </div>
      <el-table :data="items" stripe size="small" empty-text="暂无板块跟随数据">
        <el-table-column label="持仓" min-width="135"><template #default="{ row }"><strong>{{ row.name || row.code }}</strong><small>{{ row.code }}</small></template></el-table-column>
        <el-table-column label="行业 / 样本" min-width="175"><template #default="{ row }"><span>{{ row.sector_name || '--' }}</span><small>抽样 {{ row.peer_sample_size || 0 }} / 全板块 {{ row.sector_member_count || '--' }} 只</small></template></el-table-column>
        <el-table-column label="板块扩散" width="138" align="right"><template #default="{ row }">{{ sectorBreadth(row) }}</template></el-table-column>
        <el-table-column label="样本中位涨跌" width="120" align="right"><template #default="{ row }"><span :class="returnClass(row.peer_median_change_pct)">{{ changePct(row.peer_median_change_pct) }}</span></template></el-table-column>
        <el-table-column label="个股相对强弱" width="126" align="right"><template #default="{ row }"><span :class="returnClass(row.relative_strength_pct)">{{ changePct(row.relative_strength_pct) }}</span></template></el-table-column>
        <el-table-column label="跟随结论" min-width="240"><template #default="{ row }"><el-tag size="small" :type="sectorTagType(row.sector_follow_state)">{{ row.sector_follow_state || '未映射' }}</el-tag><small>{{ row.sector_follow_label }}</small></template></el-table-column>
        <el-table-column label="强势样本" min-width="220"><template #default="{ row }">{{ peerLeaders(row) }}</template></el-table-column>
      </el-table>
    </section>
    <el-drawer v-model="chartOpen" :title="`${selectedChartRow?.name || selectedChartRow?.code || ''} 多周期K线`" size="88%" destroy-on-close>
      <el-alert v-if="selectedChartRow" :title="trendText(selectedChartRow)" :type="selectedChartRow.daily_trend?.intraday_trend_broken ? 'error' : 'info'" :closable="false" show-icon />
      <div v-loading="chartLoading">
        <div v-if="chartReady" class="chart-grid">
          <LocalKLineChart v-for="period in chartPeriods" :key="period" compact :data="chartRows(period)" :trend-line="trendLinePoints(period)" :title="`${selectedChartRow?.name || selectedChartRow?.code || ''} ${period}`" :interval="period" :height="390" />
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import LocalKLineChart from '@/components/LocalKLineChart.vue'
import { getStockDetail } from '@/api/stock'
import {
  getGen3StateAlphaHoldingTickAnalysis,
  getGen3StateAlphaHoldingTickChart,
  getGen3StateAlphaHoldingTickWatchlist,
  saveGen3StateAlphaHoldingTickWatchlist,
  getGen3HoldingTPortfolioState,
  confirmGen3HoldingTPortfolio,
  saveGen3HoldingTPortfolioState
} from '@/api/trading'
import { ElMessage } from 'element-plus'

const loading = ref(false)
const codes = ref('')
const items = ref([])
const warnings = ref([])
const source = ref({})
const brokerUpdatedAt = ref('')
const savedCodes = ref([])
const trackedCodes = ref([])
const TRACKED_CODES_STORAGE_KEY = 'aistock:g3-holding-tick-tracked-codes'
const ACTION_STATES_STORAGE_KEY = 'aistock:g3-holding-tick-action-states'
const ACTION_CONFIRMATIONS_REQUIRED = 3
const actionStates = ref({})
const portfolioState = ref({ positions: {} })
const positionConfirmationOpen = ref(false)
const positionConfirmationSubmitting = ref(false)
const positionConfirmation = ref({ code: '', side: 'buy', price: null, shares: null })
const chartOpen = ref(false)
const chartLoading = ref(false)
const chartReady = ref(false)
const selectedChartRow = ref(null)
const chartPayload = ref({ charts: {} })
const chartPeriods = ['1d', '60m', '15m', '5m']
const stockNameCache = new Map()
let trackingTimer = null
const availableCount = computed(() => items.value.filter(item => item.available).length)
const batchCodes = computed(() => parseCodes(codes.value))
const positionConfirmationLabel = computed(() => positionConfirmation.value.side === 'buy' ? '买入' : '卖出')
const chartRows = (period) => {
  const rows = chartPayload.value?.charts?.[period] || []
  // FastAPI serializes ClickHouse DateTime values as ISO strings.  Give the
  // local chart its native daily/minute fields explicitly so every timeframe
  // uses the same parsing path (not just the daily chart).
  return rows.map((row) => {
    const sourceTime = String(row?.datetime || row?.time || row?.date || '')
    if (period === '1d') {
      return { ...row, date: sourceTime.slice(0, 10) }
    }
    return {
      ...row,
      // unix_time is produced by the chart endpoint, so minute charts never
      // rely on embedded-browser Date parsing.
      chartTime: Number(row?.unix_time),
      datetime: sourceTime.replace('T', ' ')
    }
  })
}
const trendLinePoints = (period) => {
  if (period !== '1d') return []
  const trend = selectedChartRow.value?.daily_trend || {}
  const anchor = trend.anchor_low_2
  const slope = Number(trend.trend_slope_per_day)
  const rows = chartRows(period)
  if (!anchor || !Number.isFinite(slope)) return []
  const anchorDate = String(anchor.trade_date || anchor.date || '').slice(0, 10)
  const start = rows.findIndex(row => String(row.date || row.datetime || row.time || '').slice(0, 10) === anchorDate)
  const base = Number(anchor.price)
  // The analysis window can be shorter than the chart window. Match dates
  // instead of reusing its positional index, which would shift the red line.
  if (!anchorDate || start < 0 || !Number.isFinite(base)) return []
  return rows.slice(start).map((row, offset) => ({
    // Keep the trend line on the exact same daily time format as its candles.
    time: row.date || row.datetime || row.time,
    value: base + slope * offset
  }))
}
const trendText = row => {
  const trend = row?.daily_trend || {}
  const line = Number(trend.intraday_trend_line_price)
  if (!Number.isFinite(line)) return '日线趋势线数据不足，仍可查看多周期K线。'
  return trend.intraday_trend_broken
    ? `盘中价格已跌破日线主升趋势线 ${line.toFixed(2)}：卖出/换股复核。`
    : `盘中价格仍在日线主升趋势线 ${line.toFixed(2)} 之上。`
}

const price = value => Number.isFinite(Number(value)) ? Number(value).toFixed(2) : '--'
const changePct = value => Number.isFinite(Number(value)) ? `${Number(value) >= 0 ? '+' : ''}${Number(value).toFixed(2)}%` : '--'
const ratioPct = value => Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(0)}%` : '--'
const rsi = value => Number.isFinite(Number(value)) ? Number(value).toFixed(1) : '--'
const rsiState = value => Number(value) >= 70 ? '超买' : Number(value) <= 30 ? '超卖' : '中性'
const rsiClass = value => Number(value) >= 70 ? 'overbought' : Number(value) <= 30 ? 'oversold' : ''
const sectorBreadth = row => {
  const sampleSize = Number(row.peer_sample_size)
  const positiveRatio = Number(row.peer_positive_ratio)
  if (!Number.isFinite(sampleSize) || sampleSize <= 0 || !Number.isFinite(positiveRatio)) return '--'
  const upCount = Math.round(sampleSize * positiveRatio)
  return `${ratioPct(positiveRatio)}（${upCount}/${sampleSize}上涨）`
}
const returnClass = value => Number(value) > 0 ? 'up' : Number(value) < 0 ? 'down' : ''
const tickAge = row => Number.isFinite(Number(row.tick_age_seconds)) ? `${Math.round(Number(row.tick_age_seconds))}秒前` : '新鲜度未知'
const sectorTagType = value => ({ '同步增强': 'success', '同步走弱': 'danger', '分化': 'warning', '样本不足': 'info' }[value] || 'info')
const peerLeaders = row => (row.peer_changes || []).map(item => `${item.name || item.code} ${changePct(item.change_pct)}`).join('；') || '--'
const isTracked = code => trackedCodes.value.includes(normalizeCode(code))

function isCodeLikeName (name, code) {
  const value = String(name || '').trim().toUpperCase()
  if (!value) return true
  return value === normalizeCode(code) || value === String(normalizeCode(code)).split('.')[0]
}

async function enrichStockNames (rows) {
  const missingCodes = [...new Set(rows
    .filter(row => isCodeLikeName(row.name, row.code))
    .map(row => normalizeCode(row.code))
    .filter(Boolean))]
  await Promise.all(missingCodes.map(async (code) => {
    if (stockNameCache.has(code)) return
    try {
      const payload = await getStockDetail(code)
      const name = String(payload?.name || '').trim()
      if (name && !isCodeLikeName(name, code)) stockNameCache.set(code, name)
    } catch {
      // A missing name must not block the read-only Tick refresh.
    }
  }))
  rows.forEach((row) => {
    const name = stockNameCache.get(normalizeCode(row.code))
    if (name && isCodeLikeName(row.name, row.code)) row.name = name
  })
}

function evaluateActionSuggestion (row) {
  const last = Number(row.last_price)
  const open = Number(row.open)
  const low = Number(row.low)
  const vwap = Number(row.vwap_5m)
  const change = Number(row.change_pct)
  const imbalance = Number(row.order_book_imbalance)
  const rsi5 = Number(row.rsi_5m_14)
  const rsi15 = Number(row.rsi_15m_14)
  const belowVwap = Number.isFinite(last) && Number.isFinite(vwap) && last < vwap
  const nearLow = Number.isFinite(last) && Number.isFinite(low) && last <= low * 1.005
  const sellerDominant = Number.isFinite(imbalance) && imbalance <= -0.35
  const buyerDominant = Number.isFinite(imbalance) && imbalance >= 0.35
  const sectorStrong = row.sector_follow_state === '同步增强'
  const sectorWeak = row.sector_follow_state === '同步走弱'
  const relativeWeak = Number(row.relative_strength_pct) <= -0.8
  const overbought = rsi5 >= 70 || rsi15 >= 70
  const oversold = rsi5 <= 30 || rsi15 <= 30

  if (!row.available || !row.tick_fresh) {
    return { key: 'observe', type: 'info', label: '仅观察', detail: '行情不可用或已过期，不生成交易候选。' }
  }
  if (belowVwap && sellerDominant && (nearLow || change <= -5 || sectorWeak)) {
    return { key: 'sell', type: 'danger', label: '建议卖出候选', detail: '弱于当日VWAP且卖盘占优；需5分钟、30分钟确认后再核实。' }
  }
  if (change >= 3 && overbought && belowVwap && (sellerDominant || sectorWeak || last < open)) {
    return { key: 't_sell', type: 'warning', label: '建议做T卖出候选', detail: '日内较强且RSI进入超买后转弱；仅针对可卖老仓，需5分钟确认。' }
  }
  if (!belowVwap && buyerDominant && sectorStrong && !relativeWeak) {
    return { key: 'buy', type: 'success', label: '建议买入候选', detail: '个股站上当日VWAP、买盘占优且板块同步增强；仍需30分钟结构确认。' }
  }
  if (sectorStrong && relativeWeak) {
    return { key: 'no_add', type: 'warning', label: '暂不建议补仓', detail: '板块偏强但个股明显落后，等待个股重新跟随。' }
  }
  if (oversold && belowVwap) {
    return { key: 'oversold_wait', type: 'warning', label: '超卖观察', detail: 'RSI进入超卖但尚未出现承接确认，不建议直接抄底。' }
  }
  return { key: 'hold', type: 'info', label: '持有观察', detail: '当前证据不足以形成买卖或做T候选。' }
}

function actionSuggestion (row) {
  const raw = evaluateActionSuggestion(row)
  if (raw.key === 'observe') return raw
  const state = actionStates.value[row.code]
  const stable = state?.stable || { key: 'hold', type: 'info', label: '持有观察', detail: '尚未形成连续确认的方向。' }
  const confirmations = state?.pendingKey === raw.key ? state.pendingCount : 1
  const confirmed = stable.key === raw.key && confirmations >= ACTION_CONFIRMATIONS_REQUIRED
  const realtimeDetail = confirmed
    ? `同向信号已连续核实 ${ACTION_CONFIRMATIONS_REQUIRED}/${ACTION_CONFIRMATIONS_REQUIRED} 次。`
    : `实时提示为“${raw.label}”，连续核实 ${confirmations}/${ACTION_CONFIRMATIONS_REQUIRED} 次。`
  return { ...stable, detail: `${stable.detail} ${realtimeDetail}` }
}

function persistActionStates () {
  window.localStorage.setItem(ACTION_STATES_STORAGE_KEY, JSON.stringify(actionStates.value))
}

function updateActionStates (rows) {
  const next = { ...actionStates.value }
  for (const row of rows) {
    const raw = evaluateActionSuggestion(row)
    const previous = next[row.code] || {
      stable: { key: 'hold', type: 'info', label: '持有观察', detail: '尚未形成连续确认的方向。' },
      pendingKey: '',
      pendingCount: 0
    }
    if (raw.key === 'observe') {
      next[row.code] = { ...previous, raw, pendingKey: '', pendingCount: 0 }
      continue
    }
    const pendingCount = raw.key === previous.pendingKey
      ? Math.min(previous.pendingCount + 1, ACTION_CONFIRMATIONS_REQUIRED)
      : 1
    next[row.code] = {
      stable: pendingCount >= ACTION_CONFIRMATIONS_REQUIRED ? raw : previous.stable,
      raw,
      pendingKey: raw.key,
      pendingCount
    }
  }
  actionStates.value = next
  persistActionStates()
}

function parseCodes (value) {
  return [...new Set(String(value || '').split(/[,，\s]+/).map(normalizeCode).filter(Boolean))]
}

function normalizeCode (value) {
  const code = String(value || '').trim().toUpperCase()
  if (!code) return ''
  if (/^\d{6}\.(SH|SZ|BJ)$/.test(code)) return code
  if (!/^\d{6}$/.test(code)) return code
  if (/^(4|8)/.test(code)) return `${code}.BJ`
  return `${code}.${/^(5|6|9)/.test(code) ? 'SH' : 'SZ'}`
}

function persistTrackedCodes () {
  window.localStorage.setItem(TRACKED_CODES_STORAGE_KEY, JSON.stringify(trackedCodes.value))
}

function resetTrackingTimer () {
  if (trackingTimer) window.clearInterval(trackingTimer)
  trackingTimer = null
  if (!trackedCodes.value.length) return
  trackingTimer = window.setInterval(() => {
    if (!loading.value) load(trackedCodes.value.join(','), { persistInput: false })
  }, 5000)
}

async function startTracking (code) {
  const normalizedCode = normalizeCode(code)
  if (!normalizedCode || isTracked(normalizedCode)) return
  trackedCodes.value = [...trackedCodes.value, normalizedCode]
  persistTrackedCodes()
  resetTrackingTimer()
  await load(trackedCodes.value.join(','), { persistInput: false })
}

function stopTracking (code) {
  const normalizedCode = normalizeCode(code)
  trackedCodes.value = trackedCodes.value.filter(item => item !== normalizedCode)
  persistTrackedCodes()
  resetTrackingTimer()
}

async function trackBatch () {
  const codesToTrack = batchCodes.value.length
    ? batchCodes.value
    : items.value.filter(item => item.available).map(item => item.code)
  if (!codesToTrack.length) {
    warnings.value = ['请先输入至少一只股票代码，或先分析并加载持仓。']
    return
  }
  trackedCodes.value = [...new Set([...trackedCodes.value, ...codesToTrack])]
  persistTrackedCodes()
  resetTrackingTimer()
  await load(trackedCodes.value.join(','), { persistInput: false })
}

function stopAllTracking () {
  trackedCodes.value = []
  persistTrackedCodes()
  resetTrackingTimer()
}

async function saveWatchlist (codeList) {
  const payload = await saveGen3StateAlphaHoldingTickWatchlist(codeList)
  if (!payload?.ok) throw new Error(payload?.message || 'Failed to save holding Tick watchlist')
  savedCodes.value = parseCodes((payload.codes || []).join(','))
}

async function openChart (row) {
  if (!row?.code) return
  selectedChartRow.value = row
  chartOpen.value = true
  chartReady.value = false
  chartLoading.value = true
  try {
    const payload = await getGen3StateAlphaHoldingTickChart(row.code)
    chartPayload.value = payload
    // Mount all chart instances only after their complete data is available.
    // Otherwise the minute charts can stay bound to their initial empty data.
    await nextTick()
    chartReady.value = true
  } catch (error) {
    warnings.value = [error?.message || '多周期K线加载失败']
    chartPayload.value = { charts: {} }
  } finally {
    chartLoading.value = false
  }
}

async function load (overrideCodes = '', { persistInput = true } = {}) {
  loading.value = true
  try {
    // Keep manually analysed codes and already-tracked codes together, so
    // adding a second symbol does not replace the first tracked diagnosis.
    // A template click passes MouseEvent as the first argument.
    const explicitCodes = typeof overrideCodes === 'string' ? overrideCodes.trim() : ''
    const inputCodes = parseCodes(explicitCodes || codes.value)
    if (persistInput && inputCodes.length) {
      try {
        await saveWatchlist(inputCodes)
      } catch (error) {
        warnings.value = [error?.message || 'Holding Tick watchlist save failed; analysis will continue']
      }
    }
    const requestCodes = [...new Set([...trackedCodes.value, ...inputCodes])].join(',')
    const response = await getGen3StateAlphaHoldingTickAnalysis(requestCodes ? { codes: requestCodes } : {})
    const responseItems = response.items || []
    await enrichStockNames(responseItems)
    items.value = responseItems
    updateActionStates(items.value)
    warnings.value = response.warnings || []
    source.value = response.source || {}
    brokerUpdatedAt.value = response.broker_updated_at || ''
  } catch (error) {
    warnings.value = [error?.message || 'Tick 分析请求失败']
    items.value = []
  } finally {
    loading.value = false
  }
}

async function loadPortfolioState () {
  const payload = await getGen3HoldingTPortfolioState()
  portfolioState.value = payload?.state || { positions: {} }
}

function openPositionConfirmation (code, side) {
  const latest = items.value.find(item => normalizeCode(item.code) === normalizeCode(code))
  const latestPrice = Number(latest?.last_price)
  positionConfirmation.value = {
    code: normalizeCode(code),
    side,
    price: Number.isFinite(latestPrice) && latestPrice > 0 ? latestPrice : null,
    shares: null
  }
  positionConfirmationOpen.value = true
}

async function submitPositionConfirmation () {
  const { code, side } = positionConfirmation.value
  const price = Number(positionConfirmation.value.price)
  const shares = Number(positionConfirmation.value.shares)
  const label = side === 'buy' ? '买入' : '卖出'
  if (!Number.isFinite(price) || price <= 0 || !Number.isInteger(shares) || shares <= 0) {
    ElMessage.warning('请填写正的成交价格和整数成交数量')
    return
  }
  positionConfirmationSubmitting.value = true
  try {
    const payload = await confirmGen3HoldingTPortfolio({ code, side, weight_pct: 25, price, shares })
    if (!payload?.ok) throw new Error(payload?.message || '保存失败')
    portfolioState.value = payload.state
    positionConfirmationOpen.value = false
    ElMessage.success(`已持久化确认${label}：${shares} 股，${price.toFixed(3)} 元`)
  } catch (error) {
    ElMessage.error(error?.message || '确认失败')
  } finally {
    positionConfirmationSubmitting.value = false
  }
}

async function saveSimRecords () {
  try {
    const payload = await saveGen3HoldingTPortfolioState(portfolioState.value)
    if (!payload?.ok) throw new Error(payload?.message || '保存失败')
    portfolioState.value = payload.state
    ElMessage.success('模拟做T记录已保存')
  } catch (error) { ElMessage.error(error?.message || '保存失败') }
}

function editSimRecord (row) { row._editing = true }
async function saveSimRecord (row) { row._editing = false; await saveSimRecords() }

onMounted(async () => {
  await loadPortfolioState()
  try {
    const saved = JSON.parse(window.localStorage.getItem(TRACKED_CODES_STORAGE_KEY) || '[]')
    if (Array.isArray(saved)) trackedCodes.value = parseCodes(saved.join(','))
  } catch {
    window.localStorage.removeItem(TRACKED_CODES_STORAGE_KEY)
  }
  try {
    const saved = JSON.parse(window.localStorage.getItem(ACTION_STATES_STORAGE_KEY) || '{}')
    if (saved && typeof saved === 'object' && !Array.isArray(saved)) actionStates.value = saved
  } catch {
    window.localStorage.removeItem(ACTION_STATES_STORAGE_KEY)
  }
  try {
    const payload = await getGen3StateAlphaHoldingTickWatchlist()
    if (payload?.ok) {
      savedCodes.value = parseCodes((payload.codes || []).join(','))
      trackedCodes.value = [...new Set([...trackedCodes.value, ...savedCodes.value])]
      persistTrackedCodes()
      if (!codes.value && savedCodes.value.length) codes.value = savedCodes.value.join(',')
    }
  } catch (error) {
    warnings.value = [error?.message || 'Saved holding Tick watchlist load failed']
  }
  resetTrackingTimer()
  load(codes.value || trackedCodes.value.join(','), { persistInput: false })
})
onBeforeUnmount(() => {
  if (trackingTimer) window.clearInterval(trackingTimer)
})
</script>

<style scoped>
.tick-page { padding: 20px; }
.topbar, .panel-head, .controls, .actions { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }
.actions { align-items: center; }
.topbar { margin-bottom: 16px; }
.topbar h1, .panel h2 { margin: 4px 0; }
.topbar p, .panel p, small { color: var(--el-text-color-secondary); }
.eyebrow { color: var(--el-color-primary); font-size: 12px; font-weight: 700; }
.read-only-alert, .warning-alert, .panel { margin-bottom: 16px; }
.panel { padding: 18px; background: var(--el-bg-color); border: 1px solid var(--el-border-color-lighter); border-radius: 8px; }
.code-control { display: flex; width: min(760px, 100%); gap: 8px; }
.metric-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px; }
.metric-card { min-height: 82px; padding: 14px; border: 1px solid var(--el-border-color-lighter); border-radius: 8px; display: flex; flex-direction: column; gap: 6px; }
.position-actions { display: flex; gap: 12px; flex-wrap: wrap; }.position-card { display: flex; align-items: center; gap: 9px; padding: 10px; border: 1px solid var(--el-border-color-lighter); border-radius: 7px; }.position-card span { color: var(--el-text-color-secondary); }
.position-confirmation-form { margin-top: 16px; }.position-confirmation-form .el-input-number { width: 100%; }
.metric-card strong { font-size: 18px; }.metric-card.accent { border-top: 3px solid var(--el-color-primary); }.metric-card.success { border-top: 3px solid var(--el-color-success); }.metric-card.warning { border-top: 3px solid var(--el-color-warning); }.metric-card.locked { border-top: 3px solid var(--el-color-info); }.date-value { font-size: 14px !important; }
.up { color: var(--el-color-danger); }.down { color: var(--el-color-success); }.fresh { color: var(--el-color-success); }.stale { color: var(--el-color-warning); }.overbought { color: var(--el-color-danger); font-weight: 700; }.oversold { color: var(--el-color-success); font-weight: 700; }
.el-table small { display: block; }.el-table .el-button { margin-left: 6px; }.action-detail, .anomaly-detail { max-width: 380px; line-height: 1.35; }.anomaly-detail { color: var(--el-color-warning); }
.chart-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 10px; }
@media (max-width: 960px) { .topbar, .controls { flex-direction: column; }.metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }.code-control { width: 100%; } }
</style>
