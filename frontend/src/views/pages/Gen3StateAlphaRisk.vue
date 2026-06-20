<template>
  <div class="g3-page">
    <header class="topbar">
      <div>
        <div class="eyebrow">G3 正式五策略</div>
        <h1>风控合同</h1>
        <p>集中查看五策略仓位、止损、止盈、退出合同和正式交易硬锁，保证页面、回测和未来调度使用同一套交易口径。</p>
      </div>
      <div class="actions">
        <el-button type="primary" :loading="loading" @click="load">刷新</el-button>
      </div>
    </header>

    <section class="metric-grid">
      <div class="metric-card">
        <span>策略阶段</span>
        <strong>{{ contract.stage || payload.stage || '--' }}</strong>
        <small>shadow / observe 优先</small>
      </div>
      <div class="metric-card accent">
        <span>组合槽位</span>
        <strong>{{ portfolio.max_slots ?? portfolio.slots ?? '--' }}</strong>
        <small>单槽 {{ pct(portfolio.per_slot_position_pct || portfolio.slot_pct) }}</small>
      </div>
      <div class="metric-card">
        <span>硬止损</span>
        <strong>{{ pct(exitContract.hard_stop_pct) }}</strong>
        <small>先减半 {{ pct(exitContract.first_take_profit_pct) }}</small>
      </div>
      <div class="metric-card">
        <span>今日票据</span>
        <strong>{{ shadowTickets.length }}</strong>
        <small>合格 {{ qualifiedTickets.length }}</small>
      </div>
      <div class="metric-card danger">
        <span>账户风控</span>
        <strong>{{ summary.account_risk?.action || 'normal' }}</strong>
        <small>{{ summary.account_risk?.reason || '按影子台账判断' }}</small>
      </div>
      <div class="metric-card locked">
        <span>下单通道</span>
        <strong>关闭</strong>
        <small>正式买点、自动下单、订单路径均锁定</small>
      </div>
    </section>

    <section class="two-col">
      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>组合合同</h2>
            <p>仓位和账户级风险约束。</p>
          </div>
        </div>
        <div class="kv-grid">
          <div v-for="item in portfolioRows" :key="item.label">
            <span>{{ item.label }}</span>
            <strong>{{ item.value }}</strong>
          </div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>退出合同</h2>
            <p>每张影子票据必须能追溯到明确退出规则。</p>
          </div>
        </div>
        <div class="kv-grid">
          <div v-for="item in exitRows" :key="item.label">
            <span>{{ item.label }}</span>
            <strong>{{ item.value }}</strong>
          </div>
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>正式交易策略</h2>
          <p>路由只作为来源通道，风控合同按正式交易策略保持一致。</p>
        </div>
      </div>
      <el-table :data="tradeStrategyPolicy" stripe size="small" empty-text="暂无正式策略合同">
        <el-table-column prop="label_cn" label="交易策略" width="150" />
        <el-table-column label="默认仓位" width="90" align="right">
          <template #default="{ row }">{{ pct(row.default_position_pct) }}</template>
        </el-table-column>
        <el-table-column label="来源路由" min-width="180">
          <template #default="{ row }">{{ listText(row.source_routes) }}</template>
        </el-table-column>
        <el-table-column label="合并来源" min-width="260">
          <template #default="{ row }">{{ listText(row.source_strategies) }}</template>
        </el-table-column>
        <el-table-column prop="risk_note" label="风控说明" min-width="260" show-overflow-tooltip />
      </el-table>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>今日票据风控</h2>
          <p>用于纸面执行前复核仓位、参考价、结构止损、硬止损和止盈。</p>
        </div>
      </div>
      <el-table v-loading="loading" :data="shadowTickets" stripe size="small" empty-text="暂无影子票据">
        <el-table-column prop="code" label="代码" width="110" fixed />
        <el-table-column prop="name" label="名称" min-width="110" fixed />
        <el-table-column label="交易策略" width="150">
          <template #default="{ row }">{{ row.trade_strategy_label || row.route_strategy_label || row.route_label || '--' }}</template>
        </el-table-column>
        <el-table-column prop="route_label" label="来源路线" width="130" />
        <el-table-column label="仓位" width="82" align="right">
          <template #default="{ row }">{{ pct(row.position_pct) }}</template>
        </el-table-column>
        <el-table-column label="参考价" width="90" align="right">
          <template #default="{ row }">{{ price(row.reference_close) }}</template>
        </el-table-column>
        <el-table-column label="结构止损" width="96" align="right">
          <template #default="{ row }">{{ price(row.structure_stop) }}</template>
        </el-table-column>
        <el-table-column label="硬止损" width="90" align="right">
          <template #default="{ row }">{{ price(row.hard_stop) }}</template>
        </el-table-column>
        <el-table-column label="止盈一" width="90" align="right">
          <template #default="{ row }">{{ price(row.take_profit_1) }}</template>
        </el-table-column>
        <el-table-column label="合格" width="82">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.qualified_shadow_buy) ? 'success' : 'warning'">
              {{ truthy(row.qualified_shadow_buy) ? '是' : '否' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="exit_contract" label="退出合同" min-width="300" show-overflow-tooltip />
      </el-table>
    </section>

    <section class="two-col">
      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>正式交易硬锁</h2>
            <p>这些状态必须保持关闭，直到替代门槛全部通过。</p>
          </div>
        </div>
        <div class="gate-list">
          <div v-for="item in guardrailRows" :key="item.label" class="gate-row">
            <el-tag size="small" :type="item.ok ? 'success' : 'danger'">{{ item.ok ? '锁定' : '异常' }}</el-tag>
            <div>
              <strong>{{ item.label }}</strong>
              <span>{{ item.detail }}</span>
            </div>
          </div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>合同产物</h2>
            <p>确保风控规则能追溯到运行产物和蓝图。</p>
          </div>
        </div>
        <el-table :data="artifactRows" stripe size="small" empty-text="暂无合同产物">
          <el-table-column prop="name" label="产物" min-width="160" />
          <el-table-column label="状态" width="86">
            <template #default="{ row }">
              <el-tag size="small" :type="row.exists ? 'success' : 'danger'">{{ row.exists ? '存在' : '缺失' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="modified_at" label="更新时间" width="170" />
          <el-table-column prop="path" label="路径" min-width="260" show-overflow-tooltip />
        </el-table>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getGen3StateAlphaContract, getGen3StateAlphaCurrent } from '@/api/trading'

const loading = ref(false)
const payload = ref({})
const currentPayload = ref({})

const contract = computed(() => payload.value.contract || currentPayload.value.strategy_contract || {})
const summary = computed(() => currentPayload.value.summary || {})
const portfolio = computed(() => contract.value.portfolio_contract || contract.value.portfolio || {})
const exitContract = computed(() => contract.value.exit_contract || {})
const tradeStrategyPolicy = computed(() => Array.isArray(contract.value.trade_strategy_policy) ? contract.value.trade_strategy_policy : [])
const shadowTickets = computed(() => Array.isArray(currentPayload.value.shadow_tickets) ? currentPayload.value.shadow_tickets : [])
const qualifiedTickets = computed(() => shadowTickets.value.filter((row) => truthy(row.qualified_shadow_buy)))
const artifacts = computed(() => payload.value.artifacts || {})
const artifactRows = computed(() => Object.entries(artifacts.value).map(([name, item]) => ({ name, ...(item || {}) })))
const strategyPositionSummary = computed(() => '主升/强突/震荡/G2 50%；恐慌出清 25%-50%')

const portfolioRows = computed(() => [
  { label: '最大槽位', value: portfolio.value.max_slots ?? portfolio.value.slots ?? '--' },
  { label: '单槽仓位', value: pct(portfolio.value.per_slot_position_pct || portfolio.value.slot_pct) },
  { label: '最大总仓', value: pct(portfolio.value.max_total_position_pct) },
  { label: '正式策略数', value: portfolio.value.trade_strategy_count ?? (tradeStrategyPolicy.value.length || '--') },
  { label: '策略框架', value: portfolio.value.trade_strategy_framework || '--' },
  { label: '账户风险闸门', value: contract.value.guardrails?.account_risk_gate || contract.value.account_risk_gate || '--' },
  { label: '五策略仓位', value: strategyPositionSummary.value }
])

const exitRows = computed(() => [
  { label: '硬止损', value: pct(exitContract.value.hard_stop_pct) },
  { label: '第一止盈', value: pct(exitContract.value.first_take_profit_pct) },
  { label: '前低保护', value: exitContract.value.previous_low_protection || exitContract.value.low_protection || '--' },
  { label: '结构止损', value: exitContract.value.structure_stop || exitContract.value.structure_stop_policy || '--' },
  { label: '回撤减仓', value: pct(exitContract.value.risk_limits?.mtm_drawdown_reduce_risk_pct || exitContract.value.mtm_drawdown_reduce_risk_pct) },
  { label: '合同说明', value: exitContract.value.description || contract.value.default_exit_contract || '--' }
])

const guardrailRows = computed(() => [
  { label: '正式买点', ok: currentPayload.value.formal_buy_signal === false, detail: `formal_buy_signal=${currentPayload.value.formal_buy_signal}` },
  { label: '自动下单', ok: currentPayload.value.auto_order_allowed === false, detail: `auto_order_allowed=${currentPayload.value.auto_order_allowed}` },
  { label: '订单路径', ok: currentPayload.value.order_path_enabled === false, detail: `order_path_enabled=${currentPayload.value.order_path_enabled}` },
  { label: '影子模式', ok: currentPayload.value.shadow_only === true, detail: `shadow_only=${currentPayload.value.shadow_only}` },
  { label: '观察模式', ok: currentPayload.value.observe_only === true, detail: `observe_only=${currentPayload.value.observe_only}` }
])

function truthy(value) {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0
  if (typeof value === 'string') return ['1', 'true', 'yes', 'ok', 'pass'].includes(value.trim().toLowerCase())
  return false
}

function pct(value) {
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : '--'
}

function price(value) {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(2) : '--'
}

function listText(value) {
  return Array.isArray(value) ? value.join('、') : (value || '--')
}

async function load() {
  loading.value = true
  try {
    const [contractPayload, current] = await Promise.all([
      getGen3StateAlphaContract(),
      getGen3StateAlphaCurrent({ limit: 100 })
    ])
    payload.value = contractPayload
    currentPayload.value = current
  } catch (error) {
    ElMessage.warning(error?.message || '读取 G3 风控合同失败')
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
.gate-row span {
  color: #667085;
  line-height: 1.5;
}

.actions {
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

.two-col {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 14px;
}

.kv-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}

.kv-grid > div {
  min-height: 74px;
  border: 1px solid #eef2f6;
  border-radius: 8px;
  padding: 10px;
  background: #f8fafc;
}

.kv-grid span {
  display: block;
  color: #667085;
  font-size: 12px;
  margin-bottom: 6px;
}

.kv-grid strong {
  color: #1f2a44;
  font-size: 16px;
  line-height: 1.35;
  word-break: break-word;
}

.gate-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.gate-row {
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr);
  gap: 10px;
  align-items: flex-start;
  border-bottom: 1px solid #eef2f6;
  padding-bottom: 10px;
}

.gate-row strong {
  display: block;
  margin-bottom: 3px;
}

@media (max-width: 1320px) {
  .metric-grid,
  .kv-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
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
  .kv-grid {
    grid-template-columns: 1fr;
  }
}
</style>
