<template>
  <div class="factor-page">
    <section class="top-band">
      <div>
        <p class="eyebrow">Gen2 Factor Research</p>
        <h1>第二代策略量化因子库</h1>
        <p class="subtitle">
          以国泰君安 Alpha191 为主线，逐个拆解公式、标记数据可得性，并把可量化因子接入 G2 候选池测试流程。
        </p>
        <p class="source-line">
          来源：{{ alpha191Source.origin }}
          <a :href="alpha191Source.url" target="_blank" rel="noreferrer">查看研报 PDF</a>
        </p>
      </div>
      <div class="actions">
        <el-input v-model.trim="keyword" clearable placeholder="搜索因子、字段、公式" style="width: 220px" />
        <el-select v-model="statusFilter" style="width: 150px">
          <el-option label="全部状态" value="all" />
          <el-option label="可直接测试" value="ready" />
          <el-option label="需补数据" value="data_gap" />
          <el-option label="待拆解" value="todo" />
        </el-select>
      </div>
    </section>

    <section class="metric-grid">
      <div class="metric">
        <span>目标因子</span>
        <strong>191</strong>
        <em>GTJA Alpha191</em>
      </div>
      <div class="metric">
        <span>已建档</span>
        <strong>{{ factorRows.length }}</strong>
        <em>来自研报表 6 因子明细</em>
      </div>
      <div class="metric">
        <span>可直接测试</span>
        <strong>{{ readyCount }}</strong>
        <em>OHLCV、收益率、滚动窗口优先</em>
      </div>
      <div class="metric">
        <span>后端已接入</span>
        <strong>{{ implementedCount }}</strong>
        <em>按 Alpha001 -> Alpha191 逐个补齐</em>
      </div>
    </section>

    <section class="two-col">
      <div class="panel">
        <div class="panel-head compact">
          <h2>因子建档清单</h2>
          <span class="muted">显示 {{ filteredFactors.length }} 条</span>
        </div>
        <el-table
          :data="filteredFactors"
          stripe
          size="small"
          height="520"
          empty-text="暂无匹配因子"
          highlight-current-row
          @current-change="selectFactor"
        >
          <el-table-column prop="id" label="编号" width="92" />
          <el-table-column prop="name" label="名称" min-width="110" show-overflow-tooltip />
          <el-table-column label="类型" width="120">
            <template #default="{ row }">{{ themeText(row.theme) }}</template>
          </el-table-column>
          <el-table-column label="数据" width="112">
            <template #default="{ row }">
              <el-tag :type="statusType(row.status)" effect="light">{{ statusText(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="后端" width="112">
            <template #default="{ row }">
              <el-tag :type="row.backend_status === 'implemented' ? 'success' : 'info'" effect="light">
                {{ row.backend_status === 'implemented' ? '已接入' : '待接入' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="fieldsText" label="字段" min-width="200" show-overflow-tooltip />
          <el-table-column prop="formula" label="公式" min-width="280" show-overflow-tooltip />
        </el-table>
      </div>

      <div class="panel detail-panel">
        <div class="panel-head compact">
          <h2>拆解卡片</h2>
          <el-tag :type="statusType(activeFactor.status)" effect="light">{{ statusText(activeFactor.status) }}</el-tag>
        </div>
        <div class="detail-title">
          <strong>{{ activeFactor.id }} {{ activeFactor.name }}</strong>
          <span>{{ themeText(activeFactor.theme) }} / {{ activeFactor.backend_status === 'implemented' ? '后端已接入' : '等待后端接入' }}</span>
        </div>
        <dl class="detail-list">
          <dt>原始公式</dt>
          <dd>{{ activeFactor.formula }}</dd>
          <dt>可量化字段</dt>
          <dd>
            <el-tag v-for="field in activeFactor.fields" :key="field" effect="plain">{{ field }}</el-tag>
          </dd>
          <dt>数据状态</dt>
          <dd>{{ activeFactor.dataNote }}</dd>
          <dt>测试计划</dt>
          <dd>{{ activeFactor.testPlan }}</dd>
          <dt>G2 使用边界</dt>
          <dd>{{ activeFactor.guardrail }}</dd>
        </dl>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>单因子测试</h2>
          <p>默认按 T 日收盘后因子值预测未来收益，不读取当日完整日线做盘中买点判断。</p>
        </div>
        <el-tag type="success">No Future Function</el-tag>
      </div>
      <div class="test-actions">
        <el-date-picker
          v-model="testParams.start_date"
          type="date"
          value-format="YYYY-MM-DD"
          format="YYYY-MM-DD"
          placeholder="开始日期"
          clearable
          style="width: 160px"
        />
        <el-date-picker
          v-model="testParams.end_date"
          type="date"
          value-format="YYYY-MM-DD"
          format="YYYY-MM-DD"
          placeholder="结束日期"
          clearable
          style="width: 160px"
        />
        <el-select v-model="testParams.horizon" style="width: 120px">
          <el-option label="T+1" :value="1" />
          <el-option label="T+3" :value="3" />
          <el-option label="T+5" :value="5" />
        </el-select>
        <el-button type="primary" :loading="testing" @click="runSelectedFactorTest">运行测试</el-button>
      </div>

      <el-alert
        v-if="testMessage"
        :type="testResult?.available ? 'success' : 'warning'"
        :closable="false"
        :title="testMessage"
        class="test-alert"
      />

      <template v-if="testResult?.available">
        <section class="result-grid">
          <div class="result-card">
            <span>有效样本</span>
            <strong>{{ testResult.sample?.valid_rows || 0 }}</strong>
            <em>{{ testResult.sample?.trade_days || 0 }} 个交易日 / {{ testResult.sample?.symbols || 0 }} 只股票</em>
          </div>
          <div class="result-card">
            <span>Mean RankIC</span>
            <strong :class="num(testResult.ic?.mean_ic) >= 0 ? 'up' : 'down'">{{ pct(testResult.ic?.mean_ic) }}</strong>
            <em>IC 天数 {{ testResult.ic?.days || 0 }}</em>
          </div>
          <div class="result-card">
            <span>ICIR</span>
            <strong>{{ fixed(testResult.ic?.ic_ir, 2) }}</strong>
            <em>正 IC 占比 {{ pct(testResult.ic?.positive_ratio) }}</em>
          </div>
          <div class="result-card">
            <span>Top-Bottom</span>
            <strong :class="num(testResult.spread?.top_bottom_spread) >= 0 ? 'up' : 'down'">
              {{ pct(testResult.spread?.top_bottom_spread) }}
            </strong>
            <em>价量方向未固化，仅作单因子观察</em>
          </div>
        </section>

        <div class="result-tables">
          <div>
            <div class="panel-head compact">
              <h2>五分组收益</h2>
              <span class="muted">低分 -> 高分</span>
            </div>
            <el-table :data="testResult.spread?.buckets || []" stripe size="small" empty-text="暂无分组">
              <el-table-column prop="bucket" label="组" width="80" />
              <el-table-column label="平均收益">
                <template #default="{ row }">{{ pct(row.mean_return) }}</template>
              </el-table-column>
            </el-table>
          </div>
          <div>
            <div class="panel-head compact">
              <h2>最新高分样本</h2>
              <span class="muted">{{ testResult.params?.target }}</span>
            </div>
            <el-table :data="testResult.latest_rows || []" stripe size="small" height="240" empty-text="暂无样本">
              <el-table-column prop="date" label="日期" width="105" />
              <el-table-column prop="code" label="代码" width="90" />
              <el-table-column prop="name" label="名称" min-width="110" />
              <el-table-column label="因子值" width="100">
                <template #default="{ row }">{{ fixed(row.factor_value, 4) }}</template>
              </el-table-column>
              <el-table-column label="收益" width="90">
                <template #default="{ row }">{{ pct(row[testResult.params?.target]) }}</template>
              </el-table-column>
            </el-table>
          </div>
        </div>
      </template>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>批量测试设计</h2>
          <p>后续每接入一个因子，都按同一套口径输出 IC、分组收益、覆盖率和 G2 组合验证。</p>
        </div>
        <el-tag type="info">Alpha001 -> Alpha191</el-tag>
      </div>
      <div class="test-grid">
        <div v-for="item in testProtocol" :key="item.title" class="protocol-item">
          <strong>{{ item.title }}</strong>
          <p>{{ item.text }}</p>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { alpha191Factors, alpha191Source } from '@/data/gen2Alpha191'
import { getGen2FactorRegistry, runGen2FactorTest } from '@/api/trading'

const keyword = ref('')
const statusFilter = ref('all')
const backendRegistry = ref({})
const selected = ref(null)
const testing = ref(false)
const testResult = ref(null)
const testMessage = ref('')
const testParams = ref({ start_date: '', end_date: '', horizon: 1 })

function buildDataNote(row) {
  if (row.status === 'data_gap') return '依赖基准指数、风格因子或递归状态等额外口径，需先确认历史可见数据源。'
  if ((row.fields || []).includes('vwap')) return '依赖 VWAP，需用成交额/成交量确认复权口径后生成。'
  return '依赖字段可由现有日线 OHLCV、收益率和滚动窗口生成，适合进入首批批量计算。'
}

function buildTestPlan(row) {
  const base = '单因子先跑 RankIC、分组收益、覆盖率、缺失率和极端值稳定性。'
  if (row.theme === 'price_volume_corr' || row.theme === 'price_volume') {
    return base + ' 再检查它对 G2 候选池量价拥挤和放量追高失败样本的过滤价值。'
  }
  return base + ' 通过后再接入 G2 排序或过滤层做组合验证。'
}

function buildGuardrail(row) {
  if ((row.fields || []).includes('vwap')) {
    return '复权价格、成交额和成交量口径必须一致；盘中版本只能用确认时点以前的分钟数据重算。'
  }
  return '默认 T 日收盘后生成，用于 T+1 决策；盘中 10:00/10:30 买点不得读取当日完整日线 OHLC。'
}

const factorRows = computed(() => {
  const backendMap = new Map((backendRegistry.value?.factors || []).map((item) => [item.id, item]))
  return alpha191Factors.map((item) => {
    const backend = backendMap.get(item.id) || {}
    return {
      ...item,
      backend_status: backend.backend_status || (item.id === 'Alpha001' ? 'implemented' : 'pending_formula_engine'),
      fieldsText: item.fields.join(', '),
      dataNote: buildDataNote(item),
      testPlan: buildTestPlan(item),
      guardrail: buildGuardrail(item)
    }
  })
})

const filteredFactors = computed(() => {
  const kw = keyword.value.toLowerCase()
  return factorRows.value.filter((item) => {
    const statusOk = statusFilter.value === 'all' || item.status === statusFilter.value
    const text = `${item.id} ${item.name} ${item.theme} ${item.formula} ${item.fieldsText}`.toLowerCase()
    return statusOk && (!kw || text.includes(kw))
  })
})

const readyCount = computed(() => factorRows.value.filter((item) => item.status === 'ready').length)
const implementedCount = computed(() => factorRows.value.filter((item) => item.backend_status === 'implemented').length)
const activeFactor = computed(() => selected.value || factorRows.value[0] || {
  id: '',
  name: '',
  theme: '',
  status: 'todo',
  backend_status: 'pending_formula_engine',
  fields: [],
  formula: '',
  dataNote: '',
  testPlan: '',
  guardrail: ''
})

const roadmap = [
  { no: '01', title: '公式建档', text: '保留研报表达式，并翻译成 pandas/SQL 可执行口径。' },
  { no: '02', title: '字段映射', text: '标记依赖字段是否已有，区分行情、基准、风格和递归状态。' },
  { no: '03', title: '单因子测试', text: '输出 IC、分组收益、覆盖率、缺失率和样本稳定性。' },
  { no: '04', title: 'G2 组合验证', text: '只把有增益且不过拟合的因子放进 G2 排序或过滤层。' }
]

const testProtocol = [
  { title: '样本范围', text: '默认从 A 股可交易标的池开始，剔除停牌、ST、上市不足窗口的样本。' },
  { title: '收益标签', text: '至少比较 T+1、T+3、T+5，G2 入场场景另看确认 bar 后收益。' },
  { title: '评价指标', text: '单因子先看 RankIC、分组单调性、覆盖率、缺失率和极端值稳定性。' },
  { title: '交易验证', text: '通过单因子后再接入 G2 候选池，比较收益、回撤、胜率和交易次数变化。' },
  { title: '反过拟合', text: '训练、验证、盲测分开，参数和方向不能按单次 Sharpe 最高直接定稿。' },
  { title: '时间边界', text: '盘中策略不读取当日完整日线，默认使用 T-1 已确认因子值。' }
]

function selectFactor(row) {
  if (row) {
    selected.value = row
    testMessage.value = ''
    testResult.value = null
  }
}

async function fetchBackendRegistry() {
  try {
    backendRegistry.value = await getGen2FactorRegistry()
  } catch (error) {
    backendRegistry.value = {}
  }
}

async function runSelectedFactorTest() {
  if (!activeFactor.value?.id) return
  testing.value = true
  testMessage.value = ''
  try {
    const data = await runGen2FactorTest({
      factor_id: activeFactor.value.id,
      start_date: testParams.value.start_date || undefined,
      end_date: testParams.value.end_date || undefined,
      horizon: testParams.value.horizon
    })
    testResult.value = data
    testMessage.value = data?.message || ''
  } catch (error) {
    testResult.value = null
    testMessage.value = error?.message || '因子测试失败'
  } finally {
    testing.value = false
  }
}

function themeText(theme) {
  const map = {
    benchmark_style: '基准/风格',
    neutralize: '中性化',
    size: '规模',
    price_volume_corr: '价量相关',
    vwap_deviation: 'VWAP偏离',
    price_volume: '量价',
    momentum_reversal: '动量反转',
    volatility: '波动',
    price_structure: '价格结构'
  }
  return map[theme] || theme || '--'
}

function statusText(status) {
  if (status === 'ready') return '可直接测试'
  if (status === 'data_gap') return '需补数据'
  return '待拆解'
}

function statusType(status) {
  if (status === 'ready') return 'success'
  if (status === 'data_gap') return 'warning'
  return 'info'
}

function num(value) {
  const n = Number(value)
  return Number.isFinite(n) ? n : 0
}

function fixed(value, digits = 2) {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(digits) : '--'
}

function pct(value, digits = 2) {
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(digits)}%` : '--'
}

onMounted(async () => {
  await fetchBackendRegistry()
  selected.value = factorRows.value[0]
})
</script>

<style scoped>
.factor-page { display: flex; flex-direction: column; gap: 16px; }
.top-band { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; padding: 24px 28px; background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; }
.eyebrow { margin: 0 0 8px; font-size: 12px; color: #64748b; text-transform: uppercase; letter-spacing: 0; }
h1 { margin: 0; font-size: 28px; color: #172033; }
.subtitle { margin: 8px 0 0; color: #5b667a; line-height: 1.6; max-width: 760px; }
.source-line { margin: 8px 0 0; color: #64748b; font-size: 13px; line-height: 1.6; }
.source-line a { color: #2563eb; text-decoration: none; font-weight: 600; }
.source-line a:hover { text-decoration: underline; }
.actions, .test-actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
.test-actions { justify-content: flex-start; margin-bottom: 12px; }
.metric-grid, .result-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }
.metric, .panel, .result-card { background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; }
.metric, .result-card { padding: 16px; display: flex; flex-direction: column; gap: 6px; }
.metric span, .result-card span { color: #64748b; font-size: 12px; }
.metric strong, .result-card strong { color: #172033; font-size: 24px; }
.metric em, .result-card em { color: #64748b; font-size: 12px; font-style: normal; line-height: 1.5; }
.panel { padding: 18px 20px; min-width: 0; }
.panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.panel-head.compact { align-items: center; }
h2 { margin: 0 0 8px; font-size: 18px; color: #172033; }
.panel-head p { margin: 0; color: #64748b; line-height: 1.6; }
.two-col { display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(340px, 0.55fr); gap: 16px; }
.muted { color: #64748b; font-size: 13px; }
.detail-panel { align-self: start; }
.detail-title { display: flex; flex-direction: column; gap: 6px; padding: 12px 0 14px; border-bottom: 1px solid #e2e8f0; }
.detail-title strong { color: #172033; font-size: 18px; }
.detail-title span { color: #64748b; font-size: 13px; }
.detail-list { margin: 14px 0 0; }
.detail-list dt { margin: 14px 0 6px; color: #64748b; font-size: 12px; font-weight: 700; }
.detail-list dd { margin: 0; color: #334155; line-height: 1.7; word-break: break-word; }
.detail-list dd .el-tag { margin: 0 6px 6px 0; }
.test-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
.protocol-item { padding: 14px; border: 1px solid #e2e8f0; border-radius: 8px; background: #fbfdff; }
.protocol-item strong { color: #172033; }
.protocol-item p { margin: 8px 0 0; color: #64748b; line-height: 1.6; font-size: 13px; }
.test-alert { margin: 10px 0 12px; }
.result-tables { display: grid; grid-template-columns: minmax(260px, 0.7fr) minmax(420px, 1.3fr); gap: 16px; margin-top: 14px; }
.up { color: #dc2626 !important; }
.down { color: #16a34a !important; }
@media (max-width: 1080px) {
  .top-band { flex-direction: column; }
  .actions { justify-content: flex-start; }
  .two-col, .result-tables { grid-template-columns: 1fr; }
}
</style>
