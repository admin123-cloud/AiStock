<template>
  <div class="page">
    <section class="top-band">
      <div>
        <p class="eyebrow">G3 V4 Research</p>
        <h1>G3 V4 研究回测</h1>
        <p class="subtitle">
          V4 仍是 research-only 候选包；板块/指数强势逻辑只进入 strong_main 路线的环境与质量门槛。
        </p>
      </div>
      <div class="actions">
        <el-segmented v-model="variant" :options="variantOptions" />
        <el-select v-model="profile" class="profile-select" size="default">
          <el-option v-for="item in profileOptions" :key="item.value" :label="item.label" :value="item.value" />
        </el-select>
        <el-button :icon="Refresh" :loading="loading" type="primary" @click="fetchData">刷新</el-button>
      </div>
    </section>

    <el-alert
      class="panel-alert"
      type="warning"
      :closable="false"
      show-icon
      title="V4 当前仍是研究包：无正式买点、无自动下单、无实盘成交模型。"
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
        <span>Strong Guard</span>
        <strong>{{ strongGuardLabel }}</strong>
        <small>{{ strongGuardText }}</small>
      </div>
      <div class="metric-card guard">
        <span>研究约束</span>
        <strong>暂不实盘</strong>
        <small>formal=false / auto=false</small>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>Strong Guard 对照</h2>
          <p>只约束 strong_main 路线，不作为全局买入 gate。</p>
        </div>
      </div>
      <el-table :data="guardRows" stripe size="small" empty-text="暂无 Strong Guard 审计">
        <el-table-column prop="variant" label="版本" width="130" />
        <el-table-column prop="guard" label="Guard" min-width="220" show-overflow-tooltip />
        <el-table-column prop="status" label="状态" width="110" />
        <el-table-column prop="raw_strong_count" label="原 strong" width="100" align="right" />
        <el-table-column prop="guarded_strong_count" label="保留" width="90" align="right" />
        <el-table-column prop="dropped_strong_count" label="过滤" width="90" align="right" />
        <el-table-column prop="market_breadth_gt" label="广度>" width="90" align="right" />
        <el-table-column prop="g3_strong_score_gt" label="G3分>" width="90" align="right" />
        <el-table-column prop="score_volume5_ge" label="V5分>=" width="90" align="right" />
      </el-table>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>压力口径对照</h2>
          <p>同一候选在成本、range 冲击、全链路冲击下的变化。</p>
        </div>
      </div>
      <el-table :data="profileRows" stripe size="small" empty-text="暂无压力口径">
        <el-table-column prop="variant" label="版本" width="126" />
        <el-table-column prop="profile" label="口径" width="160" />
        <el-table-column label="总收益" width="100" align="right">
          <template #default="{ row }"><span :class="tone(row.total_return)">{{ pct(row.total_return) }}</span></template>
        </el-table-column>
        <el-table-column label="回撤" width="100" align="right">
          <template #default="{ row }"><span class="down">{{ pct(row.max_drawdown) }}</span></template>
        </el-table-column>
        <el-table-column label="近两年" width="100" align="right">
          <template #default="{ row }"><span :class="tone(row.recent_return)">{{ pct(row.recent_return) }}</span></template>
        </el-table-column>
        <el-table-column prop="trade_count" label="交易" width="78" align="right" />
        <el-table-column label="均笔" width="90" align="right">
          <template #default="{ row }">{{ pct(row.avg_trade_return) }}</template>
        </el-table-column>
        <el-table-column label="最差浮亏" width="110" align="right">
          <template #default="{ row }"><span class="down">{{ pct(row.worst_open_mtm_ret) }}</span></template>
        </el-table-column>
      </el-table>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>收益曲线</h2>
          <p>逐日 MTM 权益、累计收益和回撤。</p>
        </div>
        <el-tag type="info">{{ curve.length }} days</el-tag>
      </div>
      <div ref="chartRef" class="curve-chart"></div>
    </section>

    <section class="two-col">
      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>分窗口稳定性</h2>
            <p>保留 train / valid / blind / weak_gap 拆分。</p>
          </div>
        </div>
        <el-table :data="windowRows" stripe size="small" empty-text="暂无窗口数据">
          <el-table-column prop="window" label="窗口" min-width="150" show-overflow-tooltip />
          <el-table-column label="收益" width="100" align="right">
            <template #default="{ row }"><span :class="tone(row.return)">{{ pct(row.return) }}</span></template>
          </el-table-column>
          <el-table-column label="回撤" width="100" align="right">
            <template #default="{ row }"><span class="down">{{ pct(row.max_drawdown) }}</span></template>
          </el-table-column>
          <el-table-column prop="trade_count" label="交易" width="76" align="right" />
          <el-table-column label="胜率" width="88" align="right">
            <template #default="{ row }">{{ pct(row.win_rate) }}</template>
          </el-table-column>
        </el-table>
      </div>

      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>目标审计</h2>
            <p>是否继续研究、是否可实盘的硬约束结论。</p>
          </div>
        </div>
        <el-table :data="goalRows" stripe size="small" empty-text="暂无目标审计">
          <el-table-column prop="item" label="事项" width="160" show-overflow-tooltip />
          <el-table-column label="结论" width="130">
            <template #default="{ row }">
              <el-tag :type="verdictType(row.verdict)" effect="light">{{ row.verdict || '--' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="evidence" label="证据" min-width="260" show-overflow-tooltip />
        </el-table>
      </div>
    </section>

    <section class="two-col">
      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>链路归因</h2>
            <p>拆开 strong_main、range_gap、down_panic 的贡献。</p>
          </div>
        </div>
        <el-table :data="routeRows" stripe size="small" empty-text="暂无链路数据">
          <el-table-column prop="route" label="链路" width="120" />
          <el-table-column prop="trade_count" label="交易" width="76" align="right" />
          <el-table-column label="胜率" width="92" align="right">
            <template #default="{ row }">{{ pct(row.win_rate) }}</template>
          </el-table-column>
          <el-table-column label="均笔" width="92" align="right">
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
            <h2>年度表现</h2>
            <p>检查收益来源是否过度集中。</p>
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
        </el-table>
      </div>
    </section>

    <section class="panel research-panel">
      <div class="panel-head">
        <div>
          <h2>横盘/冰点二次承接研究标签</h2>
          <p>{{ rangeResearch.label_cn || '横盘/冰点二次承接结构否决版' }}，仅展示历史验证，不进入实盘。</p>
        </div>
        <el-tag type="warning">研究标签 / 不进实盘</el-tag>
      </div>

      <div class="research-grid">
        <div class="research-stat">
          <span>回测窗口</span>
          <strong>{{ rangeResearch.backtest_start || '--' }} 至 {{ rangeResearch.backtest_end || '--' }}</strong>
          <small>曲线 {{ rangeOverview.start_date || '--' }} 至 {{ rangeOverview.end_date || '--' }}</small>
        </div>
        <div class="research-stat">
          <span>交易次数</span>
          <strong>{{ rangeOverview.trade_count ?? '--' }}</strong>
          <small>{{ rangeOverview.trading_days || 0 }} 个交易日</small>
        </div>
        <div class="research-stat">
          <span>总收益</span>
          <strong :class="tone(rangeOverview.total_return)">{{ pct(rangeOverview.total_return) }}</strong>
          <small>{{ rangeResearch.profile || 'cost30' }}：30bps 成本</small>
        </div>
        <div class="research-stat">
          <span>最大回撤</span>
          <strong class="down">{{ pct(rangeOverview.max_drawdown) }}</strong>
          <small>胜率 {{ pct(rangeOverview.win_rate) }}</small>
        </div>
        <div class="research-stat">
          <span>集中度</span>
          <strong>{{ money(rangeConcentration.total_pnl) }}</strong>
          <small>剔除Top3后 {{ money(rangeConcentration.top3_removed_pnl) }}</small>
        </div>
      </div>

      <div class="strategy-explain">
        <span v-for="(text, key) in rangeNameExplain" :key="key">
          <b>{{ key }}</b>：{{ text }}
        </span>
      </div>

      <div ref="rangeChartRef" class="range-chart"></div>

      <section class="two-col nested-research">
        <div>
          <div class="mini-title">分窗口表现</div>
          <el-table :data="rangeWindows" stripe size="small" empty-text="暂无分窗口数据">
            <el-table-column prop="window" label="窗口" min-width="145" show-overflow-tooltip />
            <el-table-column label="收益" width="96" align="right">
              <template #default="{ row }"><span :class="tone(row.return)">{{ pct(row.return) }}</span></template>
            </el-table-column>
            <el-table-column label="回撤" width="96" align="right">
              <template #default="{ row }"><span class="down">{{ pct(row.max_drawdown) }}</span></template>
            </el-table-column>
            <el-table-column prop="trade_count" label="交易" width="76" align="right" />
          </el-table>
        </div>
        <div>
          <div class="mini-title">覆盖与剔除效果</div>
          <el-table :data="rangeCoverageRows" stripe size="small" empty-text="暂无覆盖数据">
            <el-table-column prop="signal_count" label="保留" width="76" align="right" />
            <el-table-column prop="removed_count" label="剔除" width="76" align="right" />
            <el-table-column prop="removed_loss_count" label="剔除亏损" width="92" align="right" />
            <el-table-column label="原始均值" width="100" align="right">
              <template #default="{ row }">{{ pct(row.raw_avg_5d) }}</template>
            </el-table-column>
            <el-table-column label="原始胜率" width="100" align="right">
              <template #default="{ row }">{{ pct(row.raw_win_rate) }}</template>
            </el-table-column>
          </el-table>
        </div>
      </section>

      <div class="mini-title trade-title">历史成交</div>
      <el-table :data="rangeTrades" stripe size="small" empty-text="暂无横盘/冰点二次承接成交">
        <el-table-column prop="entry_date" label="入场日" width="104" />
        <el-table-column prop="policy_exit_date" label="退出日" width="104" />
        <el-table-column prop="code" label="代码" width="104" />
        <el-table-column prop="name" label="名称" min-width="110" show-overflow-tooltip />
        <el-table-column label="收益" width="92" align="right">
          <template #default="{ row }"><span :class="tone(row.policy_net_ret)">{{ pct(row.policy_net_ret) }}</span></template>
        </el-table-column>
        <el-table-column label="PNL" width="96" align="right">
          <template #default="{ row }">{{ money(row.realized_pnl) }}</template>
        </el-table-column>
        <el-table-column prop="emotion_signal" label="情绪" width="86" />
        <el-table-column prop="source_desc" label="来源窗口" min-width="140" show-overflow-tooltip />
        <el-table-column label="箱体位置" width="96" align="right">
          <template #default="{ row }">{{ pct(row.range_pos60) }}</template>
        </el-table-column>
        <el-table-column label="收盘修复" width="96" align="right">
          <template #default="{ row }">{{ pct(row.close_position) }}</template>
        </el-table-column>
        <el-table-column label="30m量比" width="92" align="right">
          <template #default="{ row }">{{ fixed(row.amount_ratio3, 2) }}</template>
        </el-table-column>
        <el-table-column prop="d0_second_accept" label="D0承接" width="86" />
        <el-table-column prop="d1_second_accept" label="D1承接" width="86" />
      </el-table>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>当前 G3 V4 强势历史成交</h2>
          <p>来自 strong_second_score_ge_093 的 strong_main 成交与失败归因，按入场日倒序展示。</p>
        </div>
        <el-tag type="info">{{ strongHistoryPage.total || 0 }} trades</el-tag>
      </div>
      <el-table :data="strongHistoryRows" stripe size="small" empty-text="暂无 G3 V4 强势历史成交">
        <el-table-column prop="entry_date" label="入场日" width="104" />
        <el-table-column prop="policy_exit_date" label="退出日" width="104" />
        <el-table-column prop="code" label="代码" width="104" />
        <el-table-column prop="name" label="名称" min-width="110" show-overflow-tooltip />
        <el-table-column label="收益" width="92" align="right">
          <template #default="{ row }"><span :class="tone(row.policy_net_ret)">{{ pct(row.policy_net_ret) }}</span></template>
        </el-table-column>
        <el-table-column label="PNL" width="96" align="right">
          <template #default="{ row }">{{ money(row.realized_pnl) }}</template>
        </el-table-column>
        <el-table-column label="评分" width="82" align="right">
          <template #default="{ row }">{{ fixed(row.score, 3) }}</template>
        </el-table-column>
        <el-table-column prop="strong_day_rank" label="排名" width="72" align="right" />
        <el-table-column label="最低" width="92" align="right">
          <template #default="{ row }"><span class="down">{{ pct(row.min_low_ret) }}</span></template>
        </el-table-column>
        <el-table-column label="最高" width="92" align="right">
          <template #default="{ row }"><span :class="tone(row.max_high_ret)">{{ pct(row.max_high_ret) }}</span></template>
        </el-table-column>
        <el-table-column label="回吐" width="92" align="right">
          <template #default="{ row }">{{ pct(row.giveback_from_high) }}</template>
        </el-table-column>
        <el-table-column label="D1" width="84" align="right">
          <template #default="{ row }"><span :class="tone(row.d1_close_ret)">{{ pct(row.d1_close_ret) }}</span></template>
        </el-table-column>
        <el-table-column label="D2" width="84" align="right">
          <template #default="{ row }"><span :class="tone(row.d2_close_ret)">{{ pct(row.d2_close_ret) }}</span></template>
        </el-table-column>
        <el-table-column label="上影" width="86" align="right">
          <template #default="{ row }">{{ pct(row.entry_upper_shadow) }}</template>
        </el-table-column>
        <el-table-column label="突破20日" width="96" align="right">
          <template #default="{ row }"><span :class="tone(row.entry_break_high20)">{{ pct(row.entry_break_high20) }}</span></template>
        </el-table-column>
        <el-table-column prop="failure_tag" label="归因标签" min-width="210" show-overflow-tooltip>
          <template #default="{ row }">
            <el-tag :type="failureTagType(row.failure_tag)" effect="light">{{ row.failure_tag || '--' }}</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>交易明细</h2>
          <p>按入场日倒序展示。</p>
        </div>
        <el-tag type="info">{{ tradePage.total || 0 }} trades</el-tag>
      </div>
      <el-table :data="tradeRows" stripe size="small" empty-text="暂无交易明细">
        <el-table-column prop="entry_date" label="入场日" width="104" />
        <el-table-column prop="policy_exit_date" label="退出日" width="104" />
        <el-table-column prop="route" label="链路" width="116" />
        <el-table-column prop="code" label="代码" width="104" />
        <el-table-column prop="name" label="名称" min-width="110" show-overflow-tooltip />
        <el-table-column label="收益" width="92" align="right">
          <template #default="{ row }"><span :class="tone(row.policy_net_ret)">{{ pct(row.policy_net_ret) }}</span></template>
        </el-table-column>
        <el-table-column label="PNL" width="96" align="right">
          <template #default="{ row }">{{ money(row.realized_pnl) }}</template>
        </el-table-column>
        <el-table-column prop="route_source" label="触发源" min-width="230" show-overflow-tooltip />
        <el-table-column prop="stress_profile" label="压力口径" width="130" />
      </el-table>
    </section>
  </div>
</template>

<script setup>
import * as echarts from 'echarts'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { getGen3V4ResearchBacktest } from '@/api/trading'

const loading = ref(false)
const payload = ref({})
const strongHistoryFallback = ref({})
const rangeResearchFallback = ref({})
const chartRef = ref(null)
const rangeChartRef = ref(null)
const variant = ref('v4_h10_margin')
const profile = ref('cost30')
let chart = null
let rangeChart = null

const variantOptions = [
  { label: 'H10 margin', value: 'v4_h10_margin' },
  { label: 'H5 margin', value: 'v4_h5_margin' }
]

const profileOptions = [
  { label: '30bps 基准', value: 'cost30' },
  { label: '50bps 滑点', value: 'cost50' },
  { label: '100bps 滑点', value: 'cost100' },
  { label: '30bps + range -2%', value: 'cost30_range_shock2' },
  { label: '100bps + range -2%', value: 'cost100_range_shock2' },
  { label: '30bps + 全链路 -2%', value: 'cost30_all_shock2' }
]

const overview = computed(() => payload.value?.overview || {})
const curve = computed(() => Array.isArray(payload.value?.curve) ? payload.value.curve : [])
const profileRows = computed(() => Array.isArray(payload.value?.profiles) ? payload.value.profiles : [])
const annualRows = computed(() => Array.isArray(payload.value?.annual) ? payload.value.annual : [])
const windowRows = computed(() => Array.isArray(payload.value?.windows) ? payload.value.windows : [])
const routeRows = computed(() => Array.isArray(payload.value?.route_attribution) ? payload.value.route_attribution : [])
const goalRows = computed(() => Array.isArray(payload.value?.goal_audit) ? payload.value.goal_audit : [])
const guardRows = computed(() => Array.isArray(payload.value?.sector_index_guard_audit) ? payload.value.sector_index_guard_audit : [])
const strongHistoryRows = computed(() => {
  const apiRows = Array.isArray(payload.value?.strong_history_trades) ? payload.value.strong_history_trades : []
  if (apiRows.length) return apiRows
  return Array.isArray(strongHistoryFallback.value?.records) ? strongHistoryFallback.value.records : []
})
const strongHistoryPage = computed(() => {
  if (payload.value?.strong_history_page?.total) return payload.value.strong_history_page
  return {
    total: strongHistoryFallback.value?.total || 0,
    returned: strongHistoryRows.value.length,
    source: strongHistoryFallback.value?.source || ''
  }
})
const tradeRows = computed(() => Array.isArray(payload.value?.trades) ? payload.value.trades : [])
const tradePage = computed(() => payload.value?.trade_page || {})
const rangeResearch = computed(() => payload.value?.range_second_acceptance_research || rangeResearchFallback.value || {})
const rangeOverview = computed(() => rangeResearch.value?.overview || {})
const rangeWindows = computed(() => Array.isArray(rangeResearch.value?.windows) ? rangeResearch.value.windows : [])
const rangeCurve = computed(() => Array.isArray(rangeResearch.value?.curve) ? rangeResearch.value.curve : [])
const rangeTrades = computed(() => Array.isArray(rangeResearch.value?.trades) ? rangeResearch.value.trades : [])
const rangeCoverageRows = computed(() => rangeResearch.value?.coverage ? [rangeResearch.value.coverage] : [])
const rangeConcentration = computed(() => rangeResearch.value?.concentration || {})
const rangeNameExplain = computed(() => rangeResearch.value?.name_explain || {})
const drawdown = computed(() => payload.value?.drawdown_window || {})
const strongGuardLabel = computed(() => payload.value?.summary?.strong_guard?.profile || '未接入')
const strongGuardText = computed(() => {
  const t = payload.value?.summary?.strong_guard?.thresholds || {}
  if (!Object.keys(t).length) return '等待 V4 包刷新'
  return `breadth>${t.market_breadth_gt}, g3>${t.g3_strong_score_gt}, v5>=${t.score_volume5_ge}`
})

const drawdownText = computed(() => {
  if (!drawdown.value?.peak_date) return '峰谷区间 --'
  return `${drawdown.value.peak_date} 至 ${drawdown.value.trough_date}`
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
  if (verdict === 'PASS' || verdict === 'PASS_RESEARCH') return 'success'
  if (verdict === 'PARTIAL_PASS' || verdict === 'INFO') return 'warning'
  if (verdict === 'NOT_PASS' || verdict === 'FAIL') return 'danger'
  return 'info'
}

function failureTagType(tag) {
  const text = String(tag || '')
  if (text.includes('早期失败') || text.includes('持仓期失败')) return 'danger'
  if (text.includes('大回吐') || text.includes('指数冲击')) return 'warning'
  if (text.includes('低开') || text.includes('噪音')) return 'info'
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
        lineStyle: { width: 1.6, color: '#d97706' }
      },
      {
        name: '回撤',
        type: 'line',
        xAxisIndex: 1,
        yAxisIndex: 2,
        data: rows.map((row) => row.drawdown),
        showSymbol: false,
        areaStyle: { opacity: 0.12, color: '#16a34a' },
        lineStyle: { width: 1.4, color: '#16a34a' }
      }
    ]
  })
}

function renderRangeChart() {
  if (!rangeChartRef.value) return
  if (!rangeChart) rangeChart = echarts.init(rangeChartRef.value)
  const rows = rangeCurve.value
  const dates = rows.map((row) => row.date)
  rangeChart.setOption({
    grid: [
      { left: 54, right: 28, top: 34, height: 150 },
      { left: 54, right: 28, top: 220, height: 70 }
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
      { type: 'slider', xAxisIndex: [0, 1], bottom: 4, height: 20 }
    ],
    series: [
      {
        name: '权益',
        type: 'line',
        data: rows.map((row) => row.equity),
        showSymbol: false,
        smooth: true,
        lineStyle: { width: 2, color: '#0f766e' }
      },
      {
        name: '累计收益',
        type: 'line',
        yAxisIndex: 1,
        data: rows.map((row) => row.ret_from_start),
        showSymbol: false,
        smooth: true,
        lineStyle: { width: 1.6, color: '#d97706' }
      },
      {
        name: '回撤',
        type: 'line',
        xAxisIndex: 1,
        yAxisIndex: 2,
        data: rows.map((row) => row.drawdown),
        showSymbol: false,
        areaStyle: { opacity: 0.12, color: '#16a34a' },
        lineStyle: { width: 1.4, color: '#16a34a' }
      }
    ]
  })
}

async function fetchStrongHistoryFallback() {
  if (Array.isArray(payload.value?.strong_history_trades) && payload.value.strong_history_trades.length) return
  try {
    const response = await fetch('/gen3_v4_strong_history.json')
    if (!response.ok) return
    strongHistoryFallback.value = await response.json()
  } catch {
    strongHistoryFallback.value = {}
  }
}

async function fetchRangeResearchFallback() {
  if (payload.value?.range_second_acceptance_research?.overview?.trade_count) return
  try {
    const response = await fetch('/gen3_range_second_acceptance_research.json')
    if (!response.ok) return
    rangeResearchFallback.value = await response.json()
  } catch {
    rangeResearchFallback.value = {}
  }
}

async function fetchData() {
  loading.value = true
  try {
    payload.value = await getGen3V4ResearchBacktest({
      variant: variant.value,
      profile: profile.value,
      trade_limit: 180,
      trade_offset: 0,
      strong_trade_limit: 500,
      strong_trade_offset: 0
    })
    await fetchStrongHistoryFallback()
    await fetchRangeResearchFallback()
    await nextTick()
    renderChart()
    renderRangeChart()
  } catch (error) {
    ElMessage.warning(error?.message || '读取 G3 V4 研究回测失败')
  } finally {
    loading.value = false
  }
}

function handleResize() {
  chart?.resize()
  rangeChart?.resize()
}

watch([variant, profile], fetchData)

onMounted(() => {
  fetchData()
  window.addEventListener('resize', handleResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  chart?.dispose()
  rangeChart?.dispose()
  chart = null
  rangeChart = null
})
</script>

<style scoped>
.page { display:flex; flex-direction:column; gap:14px; }
.top-band,.panel,.metric-card { background:#fff; border:1px solid #e6ebff; border-radius:8px; }
.top-band { padding:16px; display:flex; align-items:center; justify-content:space-between; gap:12px; }
.eyebrow { margin:0 0 4px; color:#2563eb; font-size:12px; font-weight:700; text-transform:uppercase; }
.top-band h1 { margin:0 0 6px; color:#1f2a44; font-size:24px; }
.subtitle { margin:0; color:#66708b; font-size:13px; line-height:1.6; max-width:780px; }
.actions { display:flex; gap:8px; flex-wrap:wrap; align-items:center; justify-content:flex-end; }
.profile-select { width:190px; }
.panel-alert { margin-bottom:0; }
.metric-grid { display:grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap:10px; }
.metric-card { padding:12px; min-height:94px; display:flex; flex-direction:column; gap:8px; border-left:4px solid #2563eb; }
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
.research-panel { border-color:#cce7e2; }
.research-grid { display:grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap:10px; margin-bottom:12px; }
.research-stat { border:1px solid #d7ede9; border-radius:8px; padding:10px; display:flex; flex-direction:column; gap:7px; background:#fbfffe; min-height:86px; }
.research-stat span { color:#66708b; font-size:12px; }
.research-stat strong { color:#1f2a44; font-size:16px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.research-stat small { color:#66708b; line-height:1.35; }
.strategy-explain { display:flex; flex-direction:column; gap:6px; margin:6px 0 10px; color:#4d5875; font-size:13px; line-height:1.55; }
.strategy-explain b { color:#1f2a44; }
.range-chart { height:330px; width:100%; margin-top:8px; }
.nested-research { margin-top:12px; }
.mini-title { color:#24355d; font-weight:700; font-size:13px; margin:4px 0 8px; }
.trade-title { margin-top:14px; }
.two-col { display:grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap:14px; }
.up { color:#d4380d; }
.down { color:#1f9d55; }
@media (max-width: 1280px) {
  .metric-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .research-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 900px) {
  .top-band { flex-direction:column; align-items:flex-start; }
  .actions { justify-content:flex-start; }
  .metric-grid,.two-col,.research-grid { grid-template-columns:1fr; }
}
</style>
