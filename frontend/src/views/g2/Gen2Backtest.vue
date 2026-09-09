<template>
  <div class="gen2-backtest-page">
    <section class="top-band">
      <div>
        <p class="eyebrow">Gen2 Strategy</p>
        <h1>第二代策略历史回测</h1>
        <p class="subtitle">{{ strategyName }} · {{ statusText }}</p>
      </div>
      <div class="actions">
        <el-select v-model="strategyCode" style="width: 240px" @change="fetchData">
          <el-option label="G2 第二代完整版" value="g2_alpha191_volume5_keep80_runup" />
        </el-select>
        <el-button :icon="Refresh" :loading="loading" @click="fetchData">刷新</el-button>
        <el-button type="primary" :icon="Upload" :loading="updateRunning" @click="startUpdateLatest">
          {{ updateRunning ? '更新中' : '更新到最新数据' }}
        </el-button>
      </div>
    </section>

    <el-alert
      v-if="updateTask"
      class="task-alert"
      :type="updateTask.status === 'failed' ? 'error' : updateTask.status === 'completed' ? 'success' : 'info'"
      :closable="false"
      :title="updateTaskTitle"
      :description="updateTaskDescription"
      show-icon
    />

    <el-alert
      v-if="!available && !loading"
      type="warning"
      :closable="false"
      :title="message || '第二代策略回测数据不可用'"
      show-icon
    />

    <section class="panel comparison-panel">
      <div class="panel-head">
        <h2>第二代回测版本对比</h2>
        <span class="muted">点击行查看明细</span>
      </div>
      <el-table
        v-loading="comparisonLoading"
        :data="comparisonRows"
        stripe
        size="small"
        :row-class-name="comparisonRowClass"
        empty-text="暂无回测结果"
        @row-click="selectComparisonRow"
      >
        <el-table-column prop="name" label="版本" min-width="230" />
        <el-table-column label="总收益" width="95" align="right">
          <template #default="{ row }">
            <span :class="num(row.total_return) >= 0 ? 'up' : 'down'">{{ row.total_return_text || '--' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="daily_sharpe_text" label="Sharpe" width="82" align="right" />
        <el-table-column label="最大回撤" width="100" align="right">
          <template #default="{ row }">
            <span class="down">{{ row.max_drawdown_text || '--' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="top3_profit_share_text" label="Top3贡献" width="105" align="right" />
        <el-table-column prop="remove_top3_return_text" label="去Top3收益" width="115" align="right" />
        <el-table-column prop="trade_count" label="交易数" width="80" align="right" />
        <el-table-column prop="positioning" label="定位" min-width="140" />
      </el-table>
    </section>

    <template v-if="available">
      <section class="metrics-grid">
        <div class="metric">
          <span>回测窗口</span>
          <strong>{{ summary.start_date || '--' }} 至 {{ summary.end_date || '--' }}</strong>
        </div>
        <div class="metric">
          <span>总收益</span>
          <strong :class="num(metrics.total_return) >= 0 ? 'up' : 'down'">{{ metrics.total_return_text || '--' }}</strong>
        </div>
        <div class="metric">
          <span>超额收益</span>
          <strong :class="num(metrics.excess_return) >= 0 ? 'up' : 'down'">{{ metrics.excess_return_text || '--' }}</strong>
        </div>
        <div class="metric">
          <span>最大回撤</span>
          <strong class="down">{{ metrics.max_drawdown_text || '--' }}</strong>
        </div>
        <div class="metric">
          <span>Sharpe</span>
          <strong>{{ metrics.daily_sharpe_text || '--' }}</strong>
        </div>
        <div class="metric">
          <span>胜率</span>
          <strong>{{ metrics.win_rate_text || '--' }}</strong>
        </div>
        <div class="metric">
          <span>单笔均值</span>
          <strong>{{ metrics.avg_trade_return_text || '--' }}</strong>
        </div>
        <div class="metric">
          <span>交易次数</span>
          <strong>{{ metrics.trade_count || 0 }}</strong>
        </div>
      </section>

      <section v-if="metrics.profit_concentration_level" class="concentration-strip">
        <div class="concentration-item">
          <span>集中度风险</span>
          <strong :class="concentrationClass">{{ concentrationText }}</strong>
        </div>
        <div class="concentration-item">
          <span>Top1 盈利贡献</span>
          <strong>{{ metrics.top1_profit_share_text || '--' }}</strong>
        </div>
        <div class="concentration-item">
          <span>Top3 盈利贡献</span>
          <strong>{{ metrics.top3_profit_share_text || '--' }}</strong>
        </div>
        <div class="concentration-item">
          <span>Top5 盈利贡献</span>
          <strong>{{ metrics.top5_profit_share_text || '--' }}</strong>
        </div>
        <div class="concentration-item">
          <span>去 Top3 后收益</span>
          <strong>{{ metrics.remove_top3_return_text || '--' }}</strong>
        </div>
      </section>

      <section class="two-col">
        <div class="panel chart-panel">
          <div class="panel-head">
            <h2>收益曲线</h2>
            <el-tag type="info">{{ summary.start_date }} 至 {{ summary.end_date }}</el-tag>
          </div>
          <div ref="chartRef" class="equity-chart"></div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <h2>固化交易规则</h2>
            <el-tag type="success">已固化</el-tag>
          </div>
          <ul class="rules">
            <li v-for="line in ruleLines" :key="line">{{ line }}</li>
          </ul>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2>每笔交易</h2>
          <span class="muted">完整 {{ trades.length }} 条</span>
        </div>
        <el-table :data="trades" stripe size="small" height="460" empty-text="暂无交易">
          <el-table-column prop="buy_date" label="买入日" width="105" />
          <el-table-column prop="sell_date" label="卖出日" width="105" />
          <el-table-column label="代码" width="126">
            <template #default="{ row }">
              <button class="copy-code" type="button" title="复制代码" @click="copyCode(row.code)">
                <span>{{ row.code }}</span>
                <el-icon><DocumentCopy /></el-icon>
              </button>
            </template>
          </el-table-column>
          <el-table-column label="分类" width="105">
            <template #default="{ row }">
              <el-tag :type="row.classification_type || 'info'" effect="light">
                {{ row.classification_label || '未分类' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="买入策略" min-width="160" show-overflow-tooltip>
            <template #default="{ row }">
              <el-tag :type="row.buy_strategy_type || 'info'" effect="light">
                {{ row.buy_strategy_label || '未知来源' }}
              </el-tag>
              <span v-if="row.buy_sector_name" class="strategy-subtext">{{ row.buy_sector_name }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="名称" min-width="110" />
          <el-table-column prop="buy_datetime" label="买入确认" width="165" />
          <el-table-column prop="sell_datetime" label="卖出时间" width="165" />
          <el-table-column label="收益" width="95" align="right">
            <template #default="{ row }">
              <span :class="num(row.return) >= 0 ? 'up' : 'down'">{{ pct(row.return) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="盈亏" width="105" align="right">
            <template #default="{ row }">
              <span :class="num(row.pnl) >= 0 ? 'up' : 'down'">{{ money(row.pnl) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="卖出比例" width="90" align="right">
            <template #default="{ row }">{{ pct(row.sell_ratio, 0) }}</template>
          </el-table-column>
          <el-table-column label="退出原因" width="155">
            <template #default="{ row }">
              <el-tag :type="exitTag(row.exit_reason)" effect="light">{{ exitText(row.exit_reason) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="v4_rank" label="V4排名" width="90" align="right" />
          <el-table-column label="买入价" width="95" align="right">
            <template #default="{ row }">{{ price(row.buy_price) }}</template>
          </el-table-column>
          <el-table-column label="卖出价" width="95" align="right">
            <template #default="{ row }">{{ price(row.sell_price) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="92" fixed="right">
            <template #default="{ row }">
              <el-button size="small" :icon="Edit" @click="openClassifyDialog(row)">修改</el-button>
            </template>
          </el-table-column>
        </el-table>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2>触发信号</h2>
          <span class="muted">展示 {{ signals.length }} 条</span>
        </div>
        <el-table :data="signals" stripe size="small" height="360" empty-text="暂无信号">
          <el-table-column prop="entry_date" label="入场日" width="105" />
          <el-table-column label="代码" width="126">
            <template #default="{ row }">
              <button class="copy-code" type="button" title="复制代码" @click="copyCode(row.code)">
                <span>{{ row.code }}</span>
                <el-icon><DocumentCopy /></el-icon>
              </button>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="名称" min-width="110" />
          <el-table-column prop="g2_open_state" label="状态" width="105" />
          <el-table-column prop="pattern" label="模式" min-width="190" show-overflow-tooltip />
          <el-table-column prop="trigger_type" label="触发" min-width="220" show-overflow-tooltip />
          <el-table-column prop="confirm_datetime" label="确认时间" width="165" />
          <el-table-column prop="v4_rank" label="V4排名" width="90" align="right" />
          <el-table-column label="入场价" width="95" align="right">
            <template #default="{ row }">{{ price(row.entry_price) }}</template>
          </el-table-column>
          <el-table-column label="3日收益" width="100" align="right">
            <template #default="{ row }">
              <span :class="num(row.entry_fwd_ret_3d) >= 0 ? 'up' : 'down'">{{ pct(row.entry_fwd_ret_3d) }}</span>
            </template>
          </el-table-column>
        </el-table>
      </section>
    </template>

    <el-dialog v-model="classifyDialogVisible" title="修改交易分类" width="420px">
      <div v-if="classifyForm.row" class="classify-summary">
        <strong>{{ classifyForm.row.code }} {{ classifyForm.row.name }}</strong>
        <span>{{ classifyForm.row.buy_datetime }} / {{ pct(classifyForm.row.return) }}</span>
      </div>
      <el-form label-position="top">
        <el-form-item label="分类">
          <el-select v-model="classifyForm.classification" style="width: 100%">
            <el-option v-for="item in tradeClassifications" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="classifyForm.note"
            type="textarea"
            :rows="3"
            maxlength="160"
            show-word-limit
            placeholder="可选：记录为什么这样分类"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="classifyDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="classifySaving" @click="saveClassification">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import * as echarts from '@/utils/charts'
import { DocumentCopy, Edit, Refresh, Upload } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  getGen2Backtest,
  getGen2BacktestUpdateTask,
  runGen2BacktestLatest,
  saveGen2TradeClassification
} from '@/api/trading'

const loading = ref(false)
const comparisonLoading = ref(false)
const available = ref(false)
const message = ref('')
const strategyCode = ref('g2_alpha191_volume5_keep80_runup')
const payload = ref({})
const comparisonRows = ref([])
const chartRef = ref(null)
const classifyDialogVisible = ref(false)
const classifySaving = ref(false)
const classifyForm = ref({ row: null, classification: '', note: '' })
const updateTask = ref(null)
const updateRunning = ref(false)
let chart = null
let updateTaskTimer = null

const comparisonStrategies = [
  {
    code: 'g2_alpha191_volume5_keep80_runup',
    positioning: '第二代完整版',
    top3_profit_share_text: '20.96%',
    remove_top3_return_text: '--'
  }
]

const strategyName = computed(() => payload.value?.strategy_display_name || payload.value?.strategy_name || 'G2 第二代完整版')
const statusText = computed(() => payload.value?.status === 'frozen_research' ? '研究版已固化' : (payload.value?.status || ''))
const metrics = computed(() => payload.value?.metrics || {})
const summary = computed(() => payload.value?.summary || {})
const ruleLines = computed(() => payload.value?.rule_lines || [])
const equityCurve = computed(() => payload.value?.equity_curve || [])
function latestTimeValue(row, keys) {
  for (const key of keys) {
    const value = String(row?.[key] || '').trim()
    if (value) return value
  }
  return ''
}

function sortByLatestTime(rows, preferredKeys, fallbackKeys = []) {
  return [...(Array.isArray(rows) ? rows : [])].sort((a, b) => {
    const aTime = latestTimeValue(a, [...preferredKeys, ...fallbackKeys])
    const bTime = latestTimeValue(b, [...preferredKeys, ...fallbackKeys])
    if (aTime !== bTime) return aTime < bTime ? 1 : -1
    const aTie = String(a?.code || a?.name || a?.trade_key || '')
    const bTie = String(b?.code || b?.name || b?.trade_key || '')
    return aTie < bTie ? 1 : aTie > bTie ? -1 : 0
  })
}

const trades = computed(() => sortByLatestTime(payload.value?.trades || [], ['sell_datetime', 'sell_date'], ['buy_datetime', 'buy_date']))
const signals = computed(() => sortByLatestTime(payload.value?.signals || [], ['confirm_datetime', 'entry_date'], ['code', 'trade_key']))
const tradeClassifications = computed(() => payload.value?.trade_classifications || [
  { value: '', label: '未分类', type: 'info' },
  { value: 'favorite', label: '最喜欢', type: 'success' },
  { value: 'watch', label: '值得研究', type: 'primary' },
  { value: 'normal', label: '普通样本', type: 'info' },
  { value: 'problem', label: '问题交易', type: 'warning' },
  { value: 'reject', label: '应过滤', type: 'danger' }
])
const concentrationText = computed(() => {
  const level = metrics.value.profit_concentration_level
  if (level === 'high') return '高'
  if (level === 'medium') return '中'
  if (level === 'low') return '低'
  return '--'
})
const concentrationClass = computed(() => {
  const level = metrics.value.profit_concentration_level
  if (level === 'high') return 'down'
  if (level === 'medium') return 'warn'
  return 'up'
})
const updateTaskTitle = computed(() => {
  const task = updateTask.value || {}
  if (task.status === 'queued') return '历史回测更新已排队'
  if (task.status === 'running') return `历史回测更新中：${task.progress || 0}%`
  if (task.status === 'completed') return '历史回测已更新到最新可用数据'
  if (task.status === 'failed') return `历史回测更新失败：${task.error || '后台任务异常'}`
  return `历史回测任务状态：${task.status || '--'}`
})
const updateTaskDescription = computed(() => {
  const task = updateTask.value || {}
  const result = task.result || {}
  const endDate = result.backtest_end_date || result.latest_source_date || task.end_date || ''
  if (task.status === 'completed' && endDate) return `已生成并替换正式回测产物，最新结束日：${endDate}`
  if (task.status === 'running') return '正在重建第二代完整版信号源、回测窗口和正式展示产物。'
  return task.stderr_tail || task.stdout_tail || ''
})

function num(value) {
  const n = Number(value)
  return Number.isFinite(n) ? n : 0
}

function pct(value, digits = 2) {
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(digits)}%` : '--'
}

function money(value) {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(2) : '--'
}

function price(value) {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(3) : '--'
}

function comparisonRowClass({ row }) {
  return row?.strategy_code === strategyCode.value ? 'selected-comparison-row' : ''
}

async function selectComparisonRow(row) {
  if (!row?.strategy_code || row.strategy_code === strategyCode.value) return
  if (row.detail_available === false) {
    ElMessage.warning('该版本结果已展示；重启后端后可查看曲线和交易明细')
    return
  }
  strategyCode.value = row.strategy_code
  await fetchData()
}

function exitText(reason) {
  const map = {
    stop_loss_30m: '30m止损',
    take_profit_partial_30m: '止盈半仓',
    weak_prev_day_low_break_30m: '跌破昨日低点',
    time_exit: '时间退出'
  }
  return map[reason] || reason || '--'
}

function exitTag(reason) {
  if (reason === 'stop_loss_30m') return 'danger'
  if (reason === 'take_profit_partial_30m') return 'success'
  if (reason === 'weak_prev_day_low_break_30m') return 'warning'
  return 'info'
}

async function copyCode(code) {
  const raw = String(code || '').trim()
  const text = raw.match(/\d{6}/)?.[0] || raw
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success(`已复制 ${text}`)
  } catch {
    ElMessage.error('复制失败，请手动选择代码')
  }
}

function classificationMeta(value) {
  return tradeClassifications.value.find(item => item.value === value) || tradeClassifications.value[0] || { label: '未分类', type: 'info' }
}

function buildTradeKey(row) {
  if (!row) return ''
  return [row.code, row.buy_datetime || row.buy_date, row.sell_datetime || row.sell_date, row.sell_ratio, row.exit_reason]
    .map(item => String(item || '').trim())
    .join('|')
}

function openClassifyDialog(row) {
  classifyForm.value = {
    row,
    classification: row.classification || '',
    note: row.classification_note || ''
  }
  classifyDialogVisible.value = true
}

async function saveClassification() {
  const row = classifyForm.value.row
  const tradeKey = row?.trade_key || buildTradeKey(row)
  if (!tradeKey) {
    ElMessage.error('缺少交易标识，无法保存分类')
    return
  }
  classifySaving.value = true
  try {
    const result = await saveGen2TradeClassification({
      strategy_code: strategyCode.value,
      trade_key: tradeKey,
      classification: classifyForm.value.classification,
      note: classifyForm.value.note
    })
    if (!result?.ok) throw new Error(result?.message || '保存失败')
    const meta = classificationMeta(classifyForm.value.classification)
    row.classification = classifyForm.value.classification
    row.trade_key = tradeKey
    row.classification_label = result.classification_label || meta.label
    row.classification_type = result.classification_type || meta.type
    row.classification_note = classifyForm.value.note
    classifyDialogVisible.value = false
    ElMessage.success('交易分类已保存')
  } catch (error) {
    ElMessage.error(error?.message || '保存交易分类失败')
  } finally {
    classifySaving.value = false
  }
}

function buildChartOption() {
  const rows = equityCurve.value
  const dates = rows.map((row) => row.date)
  const strategy = rows.map((row) => Number(row.strategy_equity || 1))
  const benchmark = rows.map((row) => Number(row.benchmark_equity || 1))
  const drawdown = rows.map((row) => Number(row.drawdown || 0) * 100)
  return {
    color: ['#1769aa', '#7f8c9a', '#d64f4f'],
    tooltip: { trigger: 'axis', valueFormatter: (value) => Number(value).toFixed(2) },
    legend: { top: 0, data: ['策略净值', '000852.SH', '回撤%'] },
    grid: [
      { left: 48, right: 56, top: 42, height: '58%' },
      { left: 48, right: 56, top: '74%', height: '18%' }
    ],
    xAxis: [
      { type: 'category', data: dates, boundaryGap: false, axisLabel: { show: false } },
      { type: 'category', data: dates, boundaryGap: false, gridIndex: 1 }
    ],
    yAxis: [
      { type: 'value', scale: true, axisLabel: { formatter: '{value}x' } },
      { type: 'value', gridIndex: 1, axisLabel: { formatter: '{value}%' } }
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1] },
      { type: 'slider', xAxisIndex: [0, 1], bottom: 0, height: 18 }
    ],
    series: [
      { name: '策略净值', type: 'line', data: strategy, showSymbol: false, smooth: true, lineStyle: { width: 2.4 } },
      { name: '000852.SH', type: 'line', data: benchmark, showSymbol: false, smooth: true, lineStyle: { width: 1.8, type: 'dashed' } },
      { name: '回撤%', type: 'line', data: drawdown, xAxisIndex: 1, yAxisIndex: 1, showSymbol: false, areaStyle: { opacity: 0.12 }, lineStyle: { width: 1.8 } }
    ]
  }
}

async function renderChart() {
  await nextTick()
  if (!chartRef.value || !equityCurve.value.length) return
  if (!chart) chart = echarts.init(chartRef.value)
  chart.setOption(buildChartOption(), true)
  chart.resize()
}

async function fetchData() {
  loading.value = true
  try {
    const data = await getGen2Backtest({ strategy_code: strategyCode.value })
    payload.value = data || {}
    available.value = !!data?.available
    message.value = data?.message || ''
    await renderChart()
  } catch (error) {
    available.value = false
    message.value = error?.message || '读取第二代策略回测失败'
  } finally {
    loading.value = false
  }
}

async function fetchComparison() {
  comparisonLoading.value = true
  try {
    const results = await Promise.all(comparisonStrategies.map(async (item) => {
      try {
        const data = await getGen2Backtest({ strategy_code: item.code })
        const m = data?.metrics || {}
        return {
          strategy_code: item.code,
          name: data?.strategy_display_name || data?.strategy_name || item.code,
          positioning: item.positioning,
          available: !!data?.available,
          detail_available: !!data?.available,
          total_return: m.total_return,
          total_return_text: m.total_return_text,
          daily_sharpe: m.daily_sharpe,
          daily_sharpe_text: m.daily_sharpe_text,
          max_drawdown: m.max_drawdown,
          max_drawdown_text: m.max_drawdown_text,
          top3_profit_share: m.top3_profit_share ?? item.top3_profit_share,
          top3_profit_share_text: m.top3_profit_share_text || item.top3_profit_share_text,
          remove_top3_return: m.remove_top3_return ?? item.remove_top3_return,
          remove_top3_return_text: m.remove_top3_return_text || item.remove_top3_return_text,
          trade_count: m.trade_count || 0
        }
      } catch {
        return { strategy_code: item.code, name: item.code, positioning: item.positioning, available: false }
      }
    }))
    comparisonRows.value = results.filter(row => row.available)
  } finally {
    comparisonLoading.value = false
  }
}

function stopUpdatePolling() {
  if (updateTaskTimer) clearInterval(updateTaskTimer)
  updateTaskTimer = null
}

async function pollUpdateTask(taskId) {
  try {
    const task = await getGen2BacktestUpdateTask(taskId)
    updateTask.value = task || null
    const status = task?.status
    updateRunning.value = status === 'queued' || status === 'running'
    if (status === 'completed') {
      stopUpdatePolling()
      ElMessage.success('历史回测已更新，正在刷新结果')
      await fetchComparison()
      await fetchData()
    } else if (status === 'failed') {
      stopUpdatePolling()
      ElMessage.error(task?.error || '历史回测更新失败')
    }
  } catch (error) {
    console.warn('轮询 Gen2 历史回测任务失败:', error?.response?.data || error?.message || error)
  }
}

async function startUpdateLatest() {
  if (updateRunning.value) return
  updateRunning.value = true
  try {
    const task = await runGen2BacktestLatest()
    if (!task?.task_id) throw new Error('历史回测更新任务启动失败')
    updateTask.value = task
    ElMessage.success('历史回测更新任务已提交')
    stopUpdatePolling()
    updateTaskTimer = setInterval(() => pollUpdateTask(task.task_id), 2500)
    await pollUpdateTask(task.task_id)
  } catch (error) {
    updateRunning.value = false
    ElMessage.error(error?.response?.data?.detail || error.message || '启动历史回测更新失败')
  }
}

function handleResize() {
  chart?.resize()
}

onMounted(() => {
  fetchComparison()
  fetchData()
  window.addEventListener('resize', handleResize)
})

onBeforeUnmount(() => {
  stopUpdatePolling()
  window.removeEventListener('resize', handleResize)
  chart?.dispose()
  chart = null
})
</script>

<style scoped>
.gen2-backtest-page { display: flex; flex-direction: column; gap: 16px; }
.top-band { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; padding: 24px 28px; background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; }
.eyebrow { margin: 0 0 8px; font-size: 12px; color: #64748b; text-transform: uppercase; letter-spacing: 0; }
h1 { margin: 0; font-size: 28px; color: #172033; }
.subtitle { margin: 8px 0 0; color: #5b667a; line-height: 1.6; }
.actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
.task-alert { border-radius: 8px; }
.metrics-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.metric, .panel { background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; }
.metric { padding: 16px; display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.metric span { color: #64748b; font-size: 12px; }
.metric strong { color: #172033; font-size: 22px; line-height: 1.2; overflow-wrap: anywhere; }
.concentration-strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; padding: 14px 16px; background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; }
.concentration-item { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.concentration-item span { color: #64748b; font-size: 12px; }
.concentration-item strong { color: #172033; font-size: 20px; }
.up { color: #c2413b; font-weight: 700; }
.down { color: #15803d; font-weight: 700; }
.warn { color: #b45309; font-weight: 700; }
.panel { padding: 18px 20px; min-width: 0; }
.panel-head { display: flex; justify-content: space-between; gap: 12px; align-items: center; margin-bottom: 12px; }
.comparison-panel :deep(.selected-comparison-row td) { background: #eef6ff !important; }
.comparison-panel :deep(.el-table__row) { cursor: pointer; }
h2 { margin: 0; font-size: 18px; color: #172033; }
.two-col { display: grid; grid-template-columns: minmax(0, 2fr) minmax(320px, 1fr); gap: 16px; }
.chart-panel { min-height: 440px; }
.equity-chart { width: 100%; height: 380px; }
.rules { margin: 0; padding-left: 18px; color: #475569; line-height: 1.8; }
.muted { color: #64748b; font-size: 13px; }
.copy-code { display: inline-flex; align-items: center; gap: 6px; min-width: 92px; height: 26px; padding: 0 8px; border: 1px solid #d7e3f3; border-radius: 6px; background: #f8fbff; color: #1d4f8f; font-size: 12px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; cursor: pointer; }
.copy-code:hover { border-color: #8bb7ea; background: #eef6ff; color: #0f3f78; }
.copy-code .el-icon { font-size: 13px; color: #5f7fa7; }
.strategy-subtext { display: block; margin-top: 4px; color: #64748b; font-size: 12px; line-height: 1.2; }
.classify-summary { display: flex; flex-direction: column; gap: 4px; margin-bottom: 14px; color: #172033; }
.classify-summary span { color: #64748b; font-size: 13px; }
@media (max-width: 1080px) {
  .top-band { flex-direction: column; }
  .actions { justify-content: flex-start; }
  .two-col { grid-template-columns: 1fr; }
}
</style>
