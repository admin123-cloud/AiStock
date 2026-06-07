<template>
  <div class="kline-chart-shell" :style="{ height: `${chartHeight}px` }">
    <div class="chart-toolbar">
      <div class="chart-title-group">
        <h3 class="chart-title">{{ title }}</h3>
        <div class="summary-strip">
          <div
            v-for="item in summaryStats"
            :key="item.label"
            class="summary-card"
            :class="item.tone"
          >
            <span class="summary-label">{{ item.label }}</span>
            <strong class="summary-value">{{ item.value }}</strong>
          </div>
        </div>
        <div class="chart-legend">
          <template v-if="legendBar">
            <span class="legend-time">{{ legendBar.label }}</span>
            <span class="legend-item">开 {{ formatPrice(legendBar.open) }}</span>
            <span class="legend-item">高 {{ formatPrice(legendBar.high) }}</span>
            <span class="legend-item">低 {{ formatPrice(legendBar.low) }}</span>
            <span class="legend-item">收 {{ formatPrice(legendBar.close) }}</span>
            <span class="legend-item" :class="legendBar.changeClass">
              {{ legendBar.changeText }}
            </span>
            <span class="legend-item">量 {{ formatLargeNumber(legendBar.volume) }}</span>
            <span class="legend-item">额 {{ formatLargeNumber(legendBar.amount) }}</span>
          </template>
          <span v-else class="legend-empty">暂无可展示的K线数据</span>
        </div>
        <div v-if="activeMarkerGroup" class="trade-marker-panel">
          <div class="trade-marker-header">
            <span class="trade-marker-title">&#25104;&#20132;&#26631;&#35760;</span>
            <span class="trade-marker-time">{{ activeMarkerGroup.label }}</span>
            <span class="trade-marker-summary">{{ activeMarkerGroup.summary }}</span>
          </div>
          <div class="trade-marker-list">
            <div
              v-for="(trade, index) in activeMarkerGroup.trades"
              :key="`${trade.side}-${trade.time}-${trade.price}-${trade.shares}-${index}`"
              class="trade-marker-item"
              :class="trade.tone"
            >
              <span class="trade-marker-side">{{ trade.side }}</span>
              <span>{{ trade.timeText }}</span>
              <span>{{ trade.sharesText }}</span>
              <span>{{ trade.priceText }}</span>
              <span v-if="trade.reasonText">{{ trade.reasonText }}</span>
            </div>
          </div>
        </div>
      </div>
      <div class="chart-zoom-controls">
        <button type="button" class="zoom-btn" @click="zoomOut">缩小</button>
        <button type="button" class="zoom-btn" @click="zoomIn">放大</button>
        <button type="button" class="zoom-btn" @click="resetZoom">重置</button>
      </div>
      <div class="chart-badges">
        <span class="badge">本地数据</span>
        <span class="badge badge-secondary">{{ intervalLabel }}</span>
        <span class="badge badge-line">MA5</span>
        <span class="badge badge-line-alt">MA10</span>
      </div>
    </div>
    <div ref="chartRef" class="kline-chart" @wheel.passive="handleMouseWheel"></div>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { CandlestickSeries, ColorType, createChart, HistogramSeries, LineSeries } from 'lightweight-charts'

const props = defineProps({
  data: {
    type: Array,
    default: () => []
  },
  tradeMarkers: {
    type: Array,
    default: () => []
  },
  title: {
    type: String,
    default: 'K线图'
  },
  interval: {
    type: String,
    default: '1d'
  },
  height: {
    type: Number,
    default: 760
  }
})

const emit = defineEmits(['range-edge'])

const chartRef = ref(null)
const chart = ref(null)
const candleSeries = ref(null)
const volumeSeries = ref(null)
const ma5Series = ref(null)
const ma10Series = ref(null)
const resizeObserver = ref(null)
const bars = ref([])
const legendIndex = ref(-1)
const lastRangeEdgeAt = ref(0)
const currentBarSpacing = ref(12)

const chartHeight = computed(() => Math.max(Number(props.height) || 760, 520))
const latestBar = computed(() => (bars.value.length ? bars.value[bars.value.length - 1] : null))
const activeBar = computed(() => {
  if (legendIndex.value >= 0 && bars.value[legendIndex.value]) {
    return bars.value[legendIndex.value]
  }
  return latestBar.value
})
const markerGroups = computed(() => groupTradeMarkers(props.tradeMarkers, props.interval))
const activeMarkerGroup = computed(() => {
  const bar = activeBar.value
  if (!bar) return null
  const key = serializeBarTime(bar.time, props.interval)
  return markerGroups.value.find((item) => item.key === key) || null
})

const intervalLabel = computed(() => {
  const mapping = {
    '1m': '1分钟',
    '15m': '15分钟',
    '30m': '30分钟',
    '60m': '60分钟',
    '1d': '日线',
    '1w': '周线',
    '1mon': '月线',
    '1q': '季线'
  }
  return mapping[props.interval] || props.interval
})

const legendBar = computed(() => {
  const bar = activeBar.value
  if (!bar) return null

  const change = bar.close - bar.open
  const changePct = bar.open ? (change / bar.open) * 100 : 0
  return {
    ...bar,
    changeClass: change > 0 ? 'rise' : change < 0 ? 'fall' : 'flat',
    changeText: `${change >= 0 ? '+' : ''}${formatPrice(change)} (${changePct >= 0 ? '+' : ''}${changePct.toFixed(2)}%)`
  }
})

const summaryStats = computed(() => {
  const latest = latestBar.value
  if (!latest) return []

  const previous = bars.value.length > 1 ? bars.value[bars.value.length - 2] : null
  const prevClose = previous?.close ?? latest.open
  const diff = latest.close - prevClose
  const diffPct = prevClose ? (diff / prevClose) * 100 : 0
  const amplitudeBase = latest.low !== 0 ? ((latest.high - latest.low) / latest.low) * 100 : 0

  return [
    {
      label: '最新价',
      value: formatPrice(latest.close),
      tone: diff > 0 ? 'rise' : diff < 0 ? 'fall' : 'flat'
    },
    {
      label: '较前收',
      value: `${diff >= 0 ? '+' : ''}${formatPrice(diff)} / ${diffPct >= 0 ? '+' : ''}${diffPct.toFixed(2)}%`,
      tone: diff > 0 ? 'rise' : diff < 0 ? 'fall' : 'flat'
    },
    {
      label: '振幅',
      value: `${amplitudeBase.toFixed(2)}%`,
      tone: 'neutral'
    },
    {
      label: '成交量',
      value: formatLargeNumber(latest.volume),
      tone: 'neutral'
    }
  ]
})

function parseChartTime(value, interval) {
  if (!value) return null
  const text = String(value).trim()
  if (!text) return null

  if (['1d', '1w', '1mon', '1q'].includes(interval) && /^\d{4}-\d{2}-\d{2}$/.test(text)) {
    const [year, month, day] = text.split('-').map(Number)
    return { year, month, day }
  }

  const normalized = text.replace(' ', 'T')
  const date = new Date(normalized)
  if (!Number.isNaN(date.getTime())) {
    return Math.floor(date.getTime() / 1000)
  }

  return null
}

function formatLabel(value, interval) {
  const text = String(value || '').replace('T', ' ')
  if (!text) return '-'
  if (['1d', '1w', '1mon', '1q'].includes(interval)) {
    return text.slice(0, 10)
  }
  return text.length >= 16 ? text.slice(0, 16) : text
}

function formatPrice(value) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return num.toFixed(2)
}

function formatLargeNumber(value) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  if (Math.abs(num) >= 100000000) return `${(num / 100000000).toFixed(2)}亿`
  if (Math.abs(num) >= 10000) return `${(num / 10000).toFixed(2)}万`
  return num.toFixed(2)
}

function serializeBarTime(time, interval) {
  if (time === null || time === undefined) return ''
  if (['1d', '1w', '1mon', '1q'].includes(interval) && typeof time === 'object') {
    const year = String(time.year || '').padStart(4, '0')
    const month = String(time.month || '').padStart(2, '0')
    const day = String(time.day || '').padStart(2, '0')
    return `${year}-${month}-${day}`
  }
  return String(time)
}

function normalizeMarkerTime(value, interval) {
  if (!value) return null
  const text = String(value).trim().replace(' ', 'T')
  if (!text) return null

  if (['1d', '1w', '1mon', '1q'].includes(interval)) {
    const dateText = text.slice(0, 10)
    if (!/^\d{4}-\d{2}-\d{2}$/.test(dateText)) return null
    const [year, month, day] = dateText.split('-').map(Number)
    return { year, month, day }
  }

  const date = new Date(text)
  if (Number.isNaN(date.getTime())) return null
  date.setSeconds(0, 0)
  return Math.floor(date.getTime() / 1000)
}

function normalizeMarkerLabel(value, interval) {
  const text = String(value || '').trim().replace('T', ' ')
  if (!text) return ''
  if (['1d', '1w', '1mon', '1q'].includes(interval)) {
    return text.slice(0, 10)
  }
  return text.length >= 16 ? text.slice(0, 16) : text
}

function findMarkerBarTime(marker, interval) {
  const normalized = normalizeMarkerTime(marker?.time, interval)
  if (normalized === null) return null
  const label = normalizeMarkerLabel(marker?.time, interval)

  if (['1d', '1w', '1mon', '1q'].includes(interval)) {
    const hit = bars.value.find((bar) => String(bar.label || '').slice(0, 10) === label)
    return hit?.time ?? normalized
  }

  const exact = bars.value.find((bar) => bar.time === normalized)
  if (exact) return exact.time
  const sameMinute = bars.value.find((bar) => String(bar.label || '') === label)
  return sameMinute?.time ?? normalized
}

function groupTradeMarkers(markers, interval) {
  const source = Array.isArray(markers) ? markers : []
  const grouped = new Map()
  source.forEach((item) => {
    const time = findMarkerBarTime(item, interval)
    if (!time) return
    const key = serializeBarTime(time, interval)
    const side = String(item?.side || '').trim()
    const isSell = side.includes('\u5356')
    const shares = Number(item?.shares || 0)
    const price = Number(item?.price)
    if (!grouped.has(key)) {
      grouped.set(key, {
        key,
        time,
        label: normalizeMarkerLabel(item?.time, interval),
        buyCount: 0,
        sellCount: 0,
        buyShares: 0,
        sellShares: 0,
        trades: []
      })
    }
    const group = grouped.get(key)
    if (isSell) {
      group.sellCount += 1
      group.sellShares += Number.isFinite(shares) ? Math.max(0, Math.trunc(shares)) : 0
    } else {
      group.buyCount += 1
      group.buyShares += Number.isFinite(shares) ? Math.max(0, Math.trunc(shares)) : 0
    }
    group.trades.push({
      side: side || (isSell ? '\u5356\u51fa' : '\u4e70\u5165'),
      time: String(item?.time || ''),
      timeText: normalizeMarkerLabel(item?.time, interval),
      shares: Number.isFinite(shares) ? Math.max(0, Math.trunc(shares)) : 0,
      sharesText: Number.isFinite(shares) && shares > 0 ? `${Math.trunc(shares)}\u80a1` : '--',
      price: Number.isFinite(price) ? price : null,
      priceText: Number.isFinite(price) && price > 0 ? `@${price.toFixed(2)}` : '@--',
      reasonText: String(item?.reason || '').trim(),
      tone: isSell ? 'sell' : 'buy'
    })
  })
  return [...grouped.values()]
    .map((group) => {
      const parts = []
      if (group.buyCount > 0) parts.push(`B${group.buyCount}/${group.buyShares}`)
      if (group.sellCount > 0) parts.push(`S${group.sellCount}/${group.sellShares}`)
      return {
        ...group,
        summary: parts.join('  ')
      }
    })
    .sort((a, b) => {
      const aTime = typeof a.time === 'number' ? a.time : Date.parse(`${serializeBarTime(a.time, interval)}T00:00:00`)
      const bTime = typeof b.time === 'number' ? b.time : Date.parse(`${serializeBarTime(b.time, interval)}T00:00:00`)
      return aTime - bTime
    })
}

function buildTradeMarkers(groups) {
  return (Array.isArray(groups) ? groups : [])
    .map((group) => {
      const hasSell = group.sellCount > 0
      const hasBuy = group.buyCount > 0
      let markerText = ''
      if (hasBuy) markerText += `B${group.buyCount}`
      if (hasSell) markerText += `${markerText ? '/' : ''}S${group.sellCount}`
      return {
        time: group.time,
        position: hasSell ? 'aboveBar' : 'belowBar',
        color: hasSell ? '#16a34a' : '#d4380d',
        shape: hasSell ? 'arrowDown' : 'arrowUp',
        text: markerText || 'T'
      }
    })
}

function buildBars(data, interval) {
  return (Array.isArray(data) ? data : [])
    .map((item) => {
      const label = item.date || item.datetime || item.time
      const time = parseChartTime(label, interval)
      const open = Number(item.open)
      const high = Number(item.high)
      const low = Number(item.low)
      const close = Number(item.close)
      const volume = Number(item.volume || 0)
      const amount = Number(item.amount || 0)

      if (!time || ![open, high, low, close].every(Number.isFinite)) {
        return null
      }

      return {
        time,
        open,
        high,
        low,
        close,
        volume,
        amount,
        color: close >= open ? '#ff4d4f' : '#22c55e',
        label: formatLabel(label, interval)
      }
    })
    .filter(Boolean)
}

function createMovingAverage(data, period) {
  const result = []
  let sum = 0

  data.forEach((item, index) => {
    sum += item.close
    if (index >= period) {
      sum -= data[index - period].close
    }
    if (index >= period - 1) {
      result.push({
        time: item.time,
        value: Number((sum / period).toFixed(2))
      })
    }
  })

  return result
}

function destroyChart() {
  if (resizeObserver.value) {
    resizeObserver.value.disconnect()
    resizeObserver.value = null
  }

  if (chart.value) {
    chart.value.remove()
    chart.value = null
  }

  candleSeries.value = null
  volumeSeries.value = null
  ma5Series.value = null
  ma10Series.value = null
}

function getDefaultBarSpacing(interval) {
  return ['1m', '15m', '30m', '60m'].includes(interval) ? 10 : 14
}

function applyBarSpacing(spacing) {
  if (!chart.value) return
  const next = Math.max(2, Math.min(30, Number(spacing) || getDefaultBarSpacing(props.interval)))
  currentBarSpacing.value = next
  chart.value.applyOptions({
    timeScale: {
      barSpacing: next
    }
  })
}

function requestMoreHistory(side = 'left', reason = 'range-change') {
  if (!bars.value.length) return
  const now = Date.now()
  if (now - lastRangeEdgeAt.value < 800) return
  lastRangeEdgeAt.value = now
  emit('range-edge', { side, reason })
}

function maybeRequestMoreHistory(reason = 'range-change') {
  const range = chart.value?.timeScale()?.getVisibleLogicalRange?.()
  if (!range) return
  if (typeof range.from === 'number' && range.from <= 3) {
    requestMoreHistory('left', reason)
  }
}

function zoomIn() {
  applyBarSpacing(currentBarSpacing.value + 2)
}

function zoomOut() {
  applyBarSpacing(currentBarSpacing.value - 2)
  requestAnimationFrame(() => {
    maybeRequestMoreHistory('zoom-out')
  })
}

function handleMouseWheel(event) {
  if (!event || event.deltaY <= 0) return
  requestAnimationFrame(() => {
    maybeRequestMoreHistory('wheel-zoom-out')
  })
}

function resetZoom() {
  applyBarSpacing(getDefaultBarSpacing(props.interval))
  chart.value?.timeScale().fitContent()
}

function syncChartData() {
  if (!chart.value || !candleSeries.value || !volumeSeries.value) return

  bars.value = buildBars(props.data, props.interval)
  legendIndex.value = -1

  candleSeries.value.setData(
    bars.value.map((item) => ({
      time: item.time,
      open: item.open,
      high: item.high,
      low: item.low,
      close: item.close
    }))
  )

  volumeSeries.value.setData(
    bars.value.map((item) => ({
      time: item.time,
      value: item.volume,
      color: item.color
    }))
  )

  ma5Series.value.setData(createMovingAverage(bars.value, 5))
  ma10Series.value.setData(createMovingAverage(bars.value, 10))
  if (typeof candleSeries.value?.setMarkers === 'function') {
    candleSeries.value.setMarkers(buildTradeMarkers(markerGroups.value))
  }
  chart.value.timeScale().fitContent()
}

function initChart() {
  if (!chartRef.value) return

  destroyChart()
  currentBarSpacing.value = getDefaultBarSpacing(props.interval)

  chart.value = createChart(chartRef.value, {
    width: chartRef.value.clientWidth,
    height: chartHeight.value - 96,
    layout: {
      background: { type: ColorType.Solid, color: '#ffffff' },
      textColor: '#475569',
      attributionLogo: false
    },
    grid: {
      vertLines: { color: '#eef2f7' },
      horzLines: { color: '#eef2f7' }
    },
    crosshair: {
      mode: 0,
      vertLine: {
        color: '#94a3b8',
        style: 2,
        labelBackgroundColor: '#475569'
      },
      horzLine: {
        color: '#94a3b8',
        style: 2,
        labelBackgroundColor: '#475569'
      }
    },
    rightPriceScale: {
      borderColor: '#e2e8f0',
      scaleMargins: {
        top: 0.08,
        bottom: 0.22
      }
    },
    timeScale: {
      borderColor: '#e2e8f0',
      timeVisible: !['1d', '1w', '1mon', '1q'].includes(props.interval),
      secondsVisible: false,
      rightOffset: 6,
      barSpacing: currentBarSpacing.value,
      minBarSpacing: 4
    },
    localization: {
      locale: 'zh-CN'
    }
  })

  candleSeries.value = chart.value.addSeries(CandlestickSeries, {
    upColor: '#ff4d4f',
    downColor: '#22c55e',
    borderVisible: false,
    wickUpColor: '#ff4d4f',
    wickDownColor: '#22c55e',
    priceLineVisible: true,
    lastValueVisible: true
  })

  volumeSeries.value = chart.value.addSeries(HistogramSeries, {
    priceFormat: {
      type: 'volume'
    },
    priceScaleId: '',
    lastValueVisible: false,
    priceLineVisible: false
  })
  volumeSeries.value.priceScale().applyOptions({
    scaleMargins: {
      top: 0.72,
      bottom: 0
    },
    mode: 2  // 对数比例，避免巨量日压扁小量柱
  })

  ma5Series.value = chart.value.addSeries(LineSeries, {
    color: '#2563eb',
    lineWidth: 2,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false
  })

  ma10Series.value = chart.value.addSeries(LineSeries, {
    color: '#f59e0b',
    lineWidth: 2,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false
  })

  chart.value.subscribeCrosshairMove((param) => {
    if (!param?.time || !param.point) {
      legendIndex.value = -1
      return
    }

    if (typeof param.logical === 'number') {
      const logicalIndex = Math.round(param.logical)
      legendIndex.value = logicalIndex >= 0 && logicalIndex < bars.value.length ? logicalIndex : -1
    }
  })

  resizeObserver.value = new ResizeObserver(() => {
    if (!chart.value || !chartRef.value) return
    chart.value.applyOptions({
      width: chartRef.value.clientWidth,
      height: chartHeight.value - 96,
      timeScale: {
        timeVisible: !['1d', '1w', '1mon', '1q'].includes(props.interval),
        barSpacing: currentBarSpacing.value
      }
    })
    chart.value.timeScale().fitContent()
  })
  resizeObserver.value.observe(chartRef.value)

  syncChartData()
}

watch(
  () => [props.interval, props.height],
  async () => {
    await nextTick()
    initChart()
  }
)

watch(
  () => props.data,
  () => {
    syncChartData()
  },
  { deep: true }
)

watch(
  () => props.tradeMarkers,
  () => {
    if (typeof candleSeries.value?.setMarkers === 'function') {
      candleSeries.value.setMarkers(buildTradeMarkers(markerGroups.value))
    }
  },
  { deep: true }
)

onMounted(() => {
  initChart()
})

onBeforeUnmount(() => {
  destroyChart()
})
</script>

<style scoped>
.kline-chart-shell {
  position: relative;
  width: 100%;
  padding: 18px 18px 8px;
  border: 1px solid #e2e8f0;
  border-radius: 16px;
  background:
    radial-gradient(circle at top right, rgba(37, 99, 235, 0.1), transparent 28%),
    radial-gradient(circle at top left, rgba(245, 158, 11, 0.08), transparent 24%),
    linear-gradient(180deg, #ffffff 0%, #f8fbff 100%);
  overflow: hidden;
}

.kline-chart-shell::after {
  content: '';
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient(rgba(148, 163, 184, 0.05) 1px, transparent 1px),
    linear-gradient(90deg, rgba(148, 163, 184, 0.05) 1px, transparent 1px);
  background-size: 28px 28px;
  pointer-events: none;
  mask-image: linear-gradient(180deg, rgba(0, 0, 0, 0.35), transparent 72%);
}

.chart-toolbar {
  position: relative;
  z-index: 1;
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 12px;
}

.chart-title-group {
  flex: 1;
  min-width: 0;
}

.chart-title {
  margin: 0 0 10px;
  font-size: 22px;
  line-height: 1.2;
  color: #1e293b;
}

.summary-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 10px;
}

.summary-card {
  min-width: 130px;
  padding: 10px 12px;
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid rgba(226, 232, 240, 0.92);
  box-shadow: 0 8px 24px rgba(15, 23, 42, 0.06);
}

.summary-card.rise {
  border-color: rgba(220, 38, 38, 0.2);
  background: linear-gradient(180deg, rgba(254, 242, 242, 0.95), rgba(255, 255, 255, 0.95));
}

.summary-card.fall {
  border-color: rgba(22, 163, 74, 0.2);
  background: linear-gradient(180deg, rgba(240, 253, 244, 0.95), rgba(255, 255, 255, 0.95));
}

.summary-card.neutral,
.summary-card.flat {
  border-color: rgba(59, 130, 246, 0.12);
}

.summary-label {
  display: block;
  margin-bottom: 6px;
  font-size: 12px;
  color: #64748b;
}

.summary-value {
  font-size: 16px;
  color: #0f172a;
}

.chart-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 14px;
  font-size: 13px;
  color: #64748b;
}

.legend-time,
.legend-item {
  white-space: nowrap;
}

.legend-time {
  font-weight: 700;
  color: #334155;
}

.legend-item.rise {
  color: #dc2626;
}

.legend-item.fall {
  color: #16a34a;
}

.legend-item.flat {
  color: #475569;
}

.legend-empty {
  color: #94a3b8;
}

.trade-marker-panel {
  margin-top: 10px;
  padding: 10px 12px;
  border-radius: 12px;
  border: 1px solid rgba(148, 163, 184, 0.2);
  background: rgba(248, 250, 252, 0.92);
}

.trade-marker-header {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  margin-bottom: 8px;
  font-size: 12px;
  color: #475569;
}

.trade-marker-title {
  font-weight: 700;
  color: #0f172a;
}

.trade-marker-time {
  font-weight: 600;
}

.trade-marker-summary {
  color: #64748b;
}

.trade-marker-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.trade-marker-item {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 12px;
  line-height: 1.5;
}

.trade-marker-item.buy {
  color: #d4380d;
}

.trade-marker-item.sell {
  color: #389e0d;
}

.trade-marker-side {
  font-weight: 700;
}

.chart-badges {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.chart-zoom-controls {
  display: flex;
  gap: 8px;
  align-items: center;
}

.zoom-btn {
  padding: 6px 10px;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  background: #ffffff;
  color: #334155;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.2s ease;
}

.zoom-btn:hover {
  background: #f8fafc;
  border-color: #94a3b8;
}

.badge {
  padding: 6px 10px;
  border-radius: 999px;
  background: rgba(37, 99, 235, 0.1);
  color: #1d4ed8;
  font-size: 12px;
  font-weight: 700;
}

.badge-secondary {
  background: rgba(245, 158, 11, 0.14);
  color: #b45309;
}

.badge-line {
  background: rgba(37, 99, 235, 0.12);
  color: #1d4ed8;
}

.badge-line-alt {
  background: rgba(245, 158, 11, 0.14);
  color: #b45309;
}

.kline-chart {
  position: relative;
  z-index: 1;
  width: 100%;
  height: calc(100% - 70px);
  min-height: 420px;
}

@media (max-width: 900px) {
  .chart-toolbar {
    flex-direction: column;
  }

  .chart-badges {
    justify-content: flex-start;
  }

  .chart-title {
    font-size: 18px;
  }

  .summary-strip {
    gap: 8px;
  }

  .summary-card {
    min-width: 120px;
    padding: 9px 10px;
  }

  .chart-legend {
    gap: 8px 10px;
    font-size: 12px;
  }

  .kline-chart-shell {
    padding: 14px 14px 6px;
  }
}
</style>
