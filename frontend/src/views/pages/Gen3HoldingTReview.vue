<template>
  <div class="t-review-page">
    <header class="topbar">
      <div>
        <div class="eyebrow">G3第三代策略 / 盘后复盘</div>
        <h1>做T复盘</h1>
        <p>逐股还原盘中信号、五档盘口与实际成交，计算相对持股不动的超额收益；本页不连接下单接口。</p>
      </div>
      <div class="actions">
        <el-date-picker v-model="tradeDate" type="date" value-format="YYYY-MM-DD" :clearable="false" @change="load" />
        <el-button type="primary" :loading="loading" @click="load">刷新复盘</el-button>
      </div>
    </header>

    <el-alert type="info" :closable="false" show-icon>
      <template #title>盘中按分钟保存决策快照与五档盘口；实际成交价、时间、数量需由你录入，系统只做归因，不会提交委托。</template>
    </el-alert>

    <section class="summary-grid">
      <div class="metric-card accent"><span>复盘日期</span><strong>{{ review.trade_date || tradeDate }}</strong><small>16:00 自动生成</small></div>
      <div class="metric-card"><span>逐股数量</span><strong>{{ review.stock_count || 0 }}</strong><small>仅保存的做T标的</small></div>
      <div class="metric-card"><span>盘中快照</span><strong>{{ review.snapshot_count || 0 }}</strong><small>每分钟决策与五档</small></div>
      <div class="metric-card locked"><span>下单路径</span><strong>已锁定</strong><small>人工成交仅用于复盘</small></div>
    </section>

    <section class="panel">
      <div class="panel-head"><div><h2>记录实际成交</h2><p>填写券商实际成交后，系统自动将先卖后买配对，计算扣除预设摩擦成本后的做T超额。</p></div></div>
      <el-form :model="form" inline @submit.prevent>
        <el-form-item label="标的"><el-select v-model="form.code" filterable placeholder="选择标的"><el-option v-for="row in stocks" :key="row.code" :label="row.code" :value="row.code" /></el-select></el-form-item>
        <el-form-item label="方向"><el-select v-model="form.side"><el-option label="卖出T仓" value="sell" /><el-option label="买回T仓" value="buy" /></el-select></el-form-item>
        <el-form-item label="成交价"><el-input-number v-model="form.price" :min="0" :precision="3" /></el-form-item>
        <el-form-item label="股数"><el-input-number v-model="form.shares" :min="1" :step="100" /></el-form-item>
        <el-form-item label="成交时间"><el-date-picker v-model="form.filled_at" type="datetime" value-format="YYYY-MM-DD HH:mm:ss" /></el-form-item>
        <el-button type="primary" :loading="saving" @click="saveExecution">保存成交</el-button>
      </el-form>
    </section>

    <section v-for="stock in stocks" :key="stock.code" class="panel stock-panel">
      <div class="panel-head">
        <div><h2>{{ stock.code }}</h2><p>复盘状态：{{ reviewStatus(stock.review_status) }} · 快照 {{ stock.snapshot_count }} 条</p></div>
        <el-tag :type="statusType(stock.review_status)">{{ reviewStatus(stock.review_status) }}</el-tag>
      </div>
      <div class="stock-metrics">
        <span>开/收：<strong>{{ price(stock.market_path?.first_price) }} / {{ price(stock.market_path?.last_price) }}</strong></span>
        <span>日内高低：<strong>{{ price(stock.market_path?.high_price) }} / {{ price(stock.market_path?.low_price) }}</strong></span>
        <span>新鲜Tick：<strong>{{ stock.data_quality?.fresh_tick_count || 0 }}</strong></span>
        <span>五档留档：<strong>{{ stock.data_quality?.five_level_book_count || 0 }}</strong></span>
      </div>
      <el-table :data="stock.decision_summary?.signal_events || []" size="small" empty-text="当日无做T信号">
        <el-table-column prop="checked_at" label="信号时间" width="168" />
        <el-table-column prop="action" label="动作" width="170" />
        <el-table-column prop="price" label="价格" width="100"><template #default="{ row }">{{ price(row.price) }}</template></el-table-column>
        <el-table-column prop="price_vs_vwap_pct" label="相对VWAP" width="110"><template #default="{ row }">{{ pct(row.price_vs_vwap_pct) }}</template></el-table-column>
        <el-table-column prop="rsi6" label="RSI6" width="85" />
        <el-table-column label="依据" min-width="240"><template #default="{ row }">{{ (row.reasons || []).join('；') }}</template></el-table-column>
      </el-table>
      <el-table :data="stock.realized_t_cycles || []" size="small" class="cycle-table" empty-text="尚无可配对的实际卖出/买回成交">
        <el-table-column prop="sell_filled_at" label="卖出时间" width="168" />
        <el-table-column prop="sell_price" label="卖出价" width="90" />
        <el-table-column prop="buy_filled_at" label="买回时间" width="168" />
        <el-table-column prop="buy_price" label="买回价" width="90" />
        <el-table-column prop="shares" label="股数" width="80" />
        <el-table-column prop="t_leg_excess_pct" label="T腿超额"><template #default="{ row }">{{ pct(row.t_leg_excess_pct) }}</template></el-table-column>
        <el-table-column prop="total_capital_excess_pct" label="总资金超额"><template #default="{ row }">{{ pct(row.total_capital_excess_pct) }}</template></el-table-column>
      </el-table>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getGen3HoldingTReview, recordGen3HoldingTManualExecution } from '@/api/trading'

const today = new Date().toISOString().slice(0, 10)
const tradeDate = ref(today)
const review = ref({})
const loading = ref(false)
const saving = ref(false)
const form = reactive({ code: '', side: 'sell', price: null, shares: 100, filled_at: '', note: '' })
const stocks = computed(() => review.value.stocks || [])

function price (value) { return value === null || value === undefined ? '--' : Number(value).toFixed(3) }
function pct (value) { return value === null || value === undefined ? '--' : `${Number(value).toFixed(3)}%` }
function reviewStatus (value) { return ({ completed: '已完成归因', needs_actual_fill: '待录入实际成交', observe_only: '仅观察' })[value] || '待复盘' }
function statusType (value) { return value === 'completed' ? 'success' : value === 'needs_actual_fill' ? 'warning' : 'info' }

async function load () {
  loading.value = true
  try {
    const payload = await getGen3HoldingTReview({ trade_date: tradeDate.value })
    review.value = payload || {}
    if (!form.code && stocks.value.length) form.code = stocks.value[0].code
  } catch (error) {
    ElMessage.error(error?.message || '读取做T复盘失败')
  } finally { loading.value = false }
}

async function saveExecution () {
  if (!form.code || !form.price || !form.shares || !form.filled_at) {
    ElMessage.warning('请完整填写标的、成交价、股数和成交时间')
    return
  }
  saving.value = true
  try {
    const payload = await recordGen3HoldingTManualExecution({ ...form })
    if (!payload?.ok) throw new Error(payload?.message || '保存失败')
    review.value = payload.review
    ElMessage.success('实际成交已保存并完成归因')
  } catch (error) {
    ElMessage.error(error?.message || '保存实际成交失败')
  } finally { saving.value = false }
}

onMounted(load)
</script>

<style scoped>
.t-review-page { padding: 18px; color: #25324a; }
.topbar, .panel-head, .actions, .stock-metrics { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.topbar { margin-bottom: 16px; } .eyebrow { color: #70819d; font-size: 13px; } h1, h2, p { margin: 0; } h1 { margin-top: 4px; } p { margin-top: 7px; color: #73809a; }
.summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin: 16px 0; }
.metric-card { border: 1px solid #e3e9f2; border-radius: 8px; padding: 14px; display: grid; gap: 4px; background: #fff; }.metric-card span, .metric-card small { color: #71809a; }.metric-card strong { font-size: 20px; }.accent { border-top: 3px solid #3b82f6; }.locked { border-top: 3px solid #94a3b8; }
.panel { margin-top: 16px; padding: 16px; border: 1px solid #e3e9f2; border-radius: 8px; background: #fff; }.stock-metrics { justify-content: flex-start; flex-wrap: wrap; margin: 14px 0; color: #64748b; }.stock-metrics strong { color: #263752; }.cycle-table { margin-top: 14px; }
@media (max-width: 900px) { .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }.topbar { align-items: flex-start; flex-direction: column; } }
</style>
