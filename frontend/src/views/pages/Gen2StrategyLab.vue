<template>
  <div class="gen2-page">
    <section class="head-band">
      <div>
        <p class="eyebrow">Gen2 Research</p>
        <h1>第二代策略实验台</h1>
        <p class="subtitle">当前读取 G2 V3 User V2 two_stop_cd3_skip 影子台账，用于观察候选、熔断状态和盘中确认质量。</p>
      </div>
      <div class="actions">
        <el-date-picker
          v-model="signalDate"
          type="date"
          value-format="YYYY-MM-DD"
          format="YYYY-MM-DD"
          placeholder="选择信号日"
          clearable
          style="width: 180px"
        />
        <el-button type="primary" :loading="loading" @click="fetchData">查询</el-button>
      </div>
    </section>

    <el-alert
      v-if="!available && !loading"
      type="warning"
      :closable="false"
      :title="message || '第二代策略实验数据不可用'"
    />

    <template v-if="available">
      <section class="summary-grid">
        <div class="metric">
          <span>信号日</span>
          <strong>{{ selectedDate || '--' }}</strong>
        </div>
        <div class="metric">
          <span>影子候选</span>
          <strong>{{ v4.score_pool_size || 0 }}</strong>
        </div>
        <div class="metric">
          <span>可观察/已执行</span>
          <strong>{{ v4.candidate_count || 0 }}</strong>
        </div>
        <div class="metric">
          <span>G2主观察</span>
          <strong>{{ patterns.primary_count || 0 }}</strong>
        </div>
      </section>

      <section class="panel freshness-panel" v-if="dataFreshness">
        <div class="panel-head">
          <div>
            <h2>数据链路诊断</h2>
            <p>{{ freshnessSummary }}</p>
          </div>
          <el-tag :type="freshnessTagType">{{ freshnessTagText }}</el-tag>
        </div>
        <div class="freshness-grid">
          <div class="freshness-item">
            <span>V4日线</span>
            <strong>{{ dataFreshness.latest_daily_date || '--' }}</strong>
          </div>
          <div class="freshness-item">
            <span>G2原始触发</span>
            <strong>{{ dataFreshness.latest_trigger_date || '--' }}</strong>
          </div>
          <div class="freshness-item">
            <span>有效信号</span>
            <strong>{{ dataFreshness.latest_valid_signal_date || '--' }}</strong>
          </div>
          <div class="freshness-item">
            <span>影子台账</span>
            <strong>{{ dataFreshness.latest_shadow_date || '--' }}</strong>
          </div>
        </div>
        <ul class="rules freshness-notes">
          <li v-for="note in dataFreshness.notes || []" :key="note">{{ note }}</li>
        </ul>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>模式观察池</h2>
            <p>{{ v4.message || '以 G2 V3 User V2 two_stop_cd3_skip 影子台账为基础生成观察标签。' }}</p>
          </div>
          <el-tag type="info">{{ meta.status || 'research_only' }}</el-tag>
        </div>
        <el-table :data="patterns.rows || []" stripe size="small" empty-text="暂无模式候选">
          <el-table-column prop="rank" label="V4排名" width="90" />
          <el-table-column label="变化" width="100">
            <template #default="{ row }">
              <el-tag :type="rankTag(row.rank_change_status)" effect="light">{{ row.rank_change_text || '--' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="code" label="代码" width="110" />
          <el-table-column prop="name" label="名称" min-width="120" />
          <el-table-column prop="gen2_score" label="Gen2分" width="100" />
          <el-table-column prop="score_total" label="V4分" width="90" />
          <el-table-column prop="mom5" label="5日动量%" width="110" />
          <el-table-column prop="mom10" label="10日动量%" width="115" />
          <el-table-column label="模式标签" min-width="260">
            <template #default="{ row }">
              <div class="tag-line">
                <el-tag
                  v-for="tag in row.pattern_tags || []"
                  :key="tag.key"
                  :type="patternTagType(tag.level)"
                  effect="light"
                >
                  {{ tag.label }}
                </el-tag>
                <span v-if="!(row.pattern_tags || []).length" class="muted">等待确认</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="pattern_summary" label="说明" min-width="220" show-overflow-tooltip />
        </el-table>
      </section>

      <section class="two-col">
        <div class="panel">
          <h2>现有实验摘要</h2>
          <el-table :data="reports || []" stripe size="small" empty-text="暂无实验报告">
            <el-table-column prop="source_label" label="来源" min-width="140" />
            <el-table-column prop="name" label="实验" min-width="180" show-overflow-tooltip />
            <el-table-column prop="total_return_pct" label="收益%" width="90" />
            <el-table-column prop="max_drawdown_pct" label="回撤%" width="90" />
            <el-table-column prop="sharpe" label="Sharpe" width="90">
              <template #default="{ row }">{{ num(row.sharpe, 2) }}</template>
            </el-table-column>
            <el-table-column prop="trade_count" label="交易" width="80" />
          </el-table>
        </div>

        <div class="panel">
          <h2>开发边界</h2>
          <ul class="rules">
            <li v-for="rule in patterns.rules || []" :key="rule">{{ rule }}</li>
            <li v-for="step in nextSteps || []" :key="step">{{ step }}</li>
          </ul>
        </div>
      </section>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import dayjs from 'dayjs'
import { getGen2StrategyLab } from '@/api/trading'

const loading = ref(false)
const available = ref(false)
const message = ref('')
const signalDate = ref(dayjs().format('YYYY-MM-DD'))
const payload = ref({})

const selectedDate = computed(() => payload.value?.selected_date || '')
const meta = computed(() => payload.value?.meta || {})
const v4 = computed(() => payload.value?.v4_reference || {})
const patterns = computed(() => payload.value?.patterns || { rows: [], rules: [] })
const reports = computed(() => payload.value?.reports || [])
const nextSteps = computed(() => payload.value?.next_steps || [])
const dataFreshness = computed(() => payload.value?.data_freshness || null)
const freshnessSummary = computed(() => {
  const d = dataFreshness.value || {}
  if (!d.latest_shadow_date) return 'G2影子台账尚未生成可展示日期。'
  if (d.latest_daily_date && d.latest_daily_date > d.latest_shadow_date) {
    return `页面当前信号日来自G2有效影子台账；日线数据已到 ${d.latest_daily_date}，有效信号最新为 ${d.latest_shadow_date}。`
  }
  return '页面当前信号日与G2有效影子台账保持一致。'
})
const freshnessTagType = computed(() => {
  const d = dataFreshness.value || {}
  return d.latest_daily_date && d.latest_shadow_date && d.latest_daily_date > d.latest_shadow_date ? 'warning' : 'success'
})
const freshnessTagText = computed(() => {
  const d = dataFreshness.value || {}
  return d.latest_daily_date && d.latest_shadow_date && d.latest_daily_date > d.latest_shadow_date ? '有效信号滞后' : '已对齐'
})

function rankTag(status) {
  if (status === 'up') return 'danger'
  if (status === 'new') return 'warning'
  if (status === 'down') return 'success'
  return 'info'
}

function patternTagType(level) {
  if (level === 'hot') return 'danger'
  if (level === 'primary') return 'success'
  return 'info'
}

function num(value, digits = 2) {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(digits) : '--'
}

async function fetchData() {
  loading.value = true
  try {
    const data = await getGen2StrategyLab({ signal_date: signalDate.value, limit: 30 })
    payload.value = data || {}
    available.value = !!data?.available
    message.value = data?.message || ''
  } catch (error) {
    available.value = false
    message.value = error?.message || '读取第二代策略实验数据失败'
  } finally {
    loading.value = false
  }
}

onMounted(fetchData)
</script>

<style scoped>
.gen2-page { display:flex; flex-direction:column; gap:16px; }
.head-band { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; padding:24px 28px; background:#ffffff; border:1px solid #e5e9f2; border-radius:8px; }
.eyebrow { margin:0 0 8px; font-size:12px; color:#64748b; text-transform:uppercase; letter-spacing:0; }
h1 { margin:0; font-size:28px; color:#172033; }
.subtitle { margin:8px 0 0; color:#5b667a; line-height:1.6; }
.actions { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.summary-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; }
.metric,.panel { background:#fff; border:1px solid #e5e9f2; border-radius:8px; }
.metric { padding:16px; display:flex; flex-direction:column; gap:6px; }
.metric span { font-size:12px; color:#667085; }
.metric strong { font-size:24px; color:#182033; }
.panel { padding:18px 20px; }
.panel-head { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; margin-bottom:12px; }
h2 { margin:0 0 8px; font-size:18px; color:#182033; }
.panel p { margin:0; color:#667085; }
.tag-line { display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
.muted { color:#98a2b3; font-size:12px; }
.two-col { display:grid; grid-template-columns:minmax(0,2fr) minmax(280px,1fr); gap:16px; }
.rules { margin:0; padding-left:18px; color:#475467; line-height:1.8; }
.freshness-panel { border-color:#f3d19e; background:#fffaf0; }
.freshness-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin-top:12px; }
.freshness-item { padding:10px 12px; border:1px solid #f5dfbb; border-radius:8px; background:#fff; display:flex; flex-direction:column; gap:4px; }
.freshness-item span { font-size:12px; color:#8a6d3b; }
.freshness-item strong { font-size:18px; color:#172033; }
.freshness-notes { margin-top:12px; }
@media (max-width: 980px) {
  .head-band { flex-direction:column; }
  .two-col { grid-template-columns:1fr; }
}
</style>
