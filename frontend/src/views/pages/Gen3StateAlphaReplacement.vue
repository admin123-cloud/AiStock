<template>
  <div class="g3-page">
    <header class="topbar">
      <div>
        <div class="eyebrow">G3 State Alpha</div>
        <h1>G3融合成熟度</h1>
        <p>用工作流、历史样本、未来函数审计、30 日观察和纸面执行一致性，确认 G2 已作为补位能力稳定归入 G3 最终版。</p>
      </div>
      <div class="actions">
        <el-button :loading="snapshotLoading" @click="runSnapshot">记录今日观察</el-button>
        <el-button type="primary" :loading="loading" @click="load">刷新</el-button>
      </div>
    </header>

    <el-alert :closable="false" show-icon :type="canExit ? 'success' : 'warning'" :title="verdict" />

    <section class="metric-grid">
      <div class="metric-card">
        <span>融合状态</span>
        <strong>{{ canExit ? '达标' : '观察' }}</strong>
        <small>以动态审计为准</small>
      </div>
      <div class="metric-card accent">
        <span>观察日</span>
        <strong>{{ acceptedDays }}/30</strong>
        <small>剩余 {{ summary.accepted_observation_days_remaining ?? Math.max(0, 30 - acceptedDays) }} 天</small>
      </div>
      <div class="metric-card">
        <span>历史交易</span>
        <strong>{{ fmt(summary.closed_trade_count ?? summary.trade_count, 0) }}</strong>
        <small>胜率 {{ pct(summary.win_rate) }}</small>
      </div>
      <div class="metric-card">
        <span>历史收益</span>
        <strong>{{ pct(summary.total_return) }}</strong>
        <small>最大回撤 {{ pct(summary.max_drawdown) }}</small>
      </div>
      <div class="metric-card danger">
        <span>阻断项</span>
        <strong>{{ blockingGateCount }}</strong>
        <small>{{ firstBlockingGate }}</small>
      </div>
      <div class="metric-card locked">
        <span>当前主路线</span>
        <strong>{{ routeName(assessment.selected_route || evidence.workflow?.selected_route) }}</strong>
        <small>{{ assessment.contract || '--' }}</small>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>融合验收项</h2>
          <p>所有关键项通过前，G2 只作为 G3 的空档补位能力观察，不再作为前台并列策略。</p>
        </div>
      </div>
      <div class="gate-list">
        <div v-for="item in gates" :key="item.key || item.name" class="gate-row">
          <el-tag size="small" :type="gateType(item)">{{ gateText(item) }}</el-tag>
          <div>
            <strong>{{ item.name || item.key }}</strong>
            <span>{{ item.detail || item.message || '--' }}</span>
          </div>
        </div>
        <el-empty v-if="!gates.length" description="暂无替代门槛" />
      </div>
    </section>

    <section class="two-col">
      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>观察快照</h2>
            <p>30 个通过观察日是 G3 接管前的硬门槛。</p>
          </div>
        </div>
        <el-table :data="snapshots" stripe size="small" height="360" empty-text="暂无观察快照">
          <el-table-column prop="observation_date" label="日期" width="110" />
          <el-table-column label="状态" width="86">
            <template #default="{ row }">
              <el-tag size="small" :type="row.accepted ? 'success' : 'danger'">{{ row.accepted ? '通过' : '阻断' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="smoke_verdict" label="烟测" width="130" />
          <el-table-column label="票据" width="82" align="right">
            <template #default="{ row }">{{ fmt(row.qualified_ticket_count, 0) }}</template>
          </el-table-column>
          <el-table-column label="分类" width="132">
            <template #default="{ row }">
              <el-tag size="small" :type="observationSeverityType(row.severity)">{{ observationCategoryLabel(row.failure_category) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="动作" width="132" show-overflow-tooltip>
            <template #default="{ row }">{{ observationActionLabel(row.action_required) }}</template>
          </el-table-column>
          <el-table-column label="30m" width="80">
            <template #default="{ row }">
              <el-tag size="small" :type="row.m30_ok ? 'success' : 'danger'">{{ row.m30_ok ? '通过' : '阻断' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="纸面" width="80">
            <template #default="{ row }">
              <el-tag size="small" :type="row.paper_ok ? 'success' : 'danger'">{{ row.paper_ok ? '通过' : '阻断' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="created_at" label="记录时间" width="170" />
          <el-table-column label="阻断原因" min-width="240" show-overflow-tooltip>
            <template #default="{ row }">{{ blockerText(row) }}</template>
          </el-table-column>
        </el-table>
      </div>

      <div class="panel">
        <div class="panel-head">
          <div>
          <h2>证据摘要</h2>
          <p>融合判断必须能回到工作流、历史审计、影子票据和纸面台账。</p>
          </div>
        </div>
        <div class="evidence-list">
          <div v-for="item in evidenceRows" :key="item.label">
            <span>{{ item.label }}</span>
            <strong>{{ item.value }}</strong>
            <small>{{ item.detail }}</small>
          </div>
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>下一步</h2>
          <p>这些动作通过后，G3 最终版才算把 G2 成熟能力真正吃进去，而不是只换了名字。</p>
        </div>
      </div>
      <div class="daily-action-list">
        <div v-for="item in dailyActions" :key="item.key || item.title" class="daily-action-card">
          <div class="daily-action-title">
            <el-tag size="small" :type="actionType(item.priority)">{{ item.priority || 'normal' }}</el-tag>
            <strong>{{ item.title || item.key }}</strong>
            <span>{{ actionStatusLabel(item.status) }}</span>
          </div>
          <p>{{ item.detail || '--' }}</p>
        </div>
        <el-empty v-if="!dailyActions.length" description="暂无今日操作计划" />
      </div>
      <div class="next-list">
        <div v-for="item in nextActions" :key="item">{{ item }}</div>
        <el-empty v-if="!nextActions.length" description="暂无下一步建议" />
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { statusWithZh } from '@/utils/g3StatusText'
import {
  getGen3StateAlphaObservationSnapshots,
  getGen3StateAlphaReplacementAssessment,
  runGen3StateAlphaObservationSchedulerOnce
} from '@/api/trading'

const loading = ref(false)
const snapshotLoading = ref(false)
const payload = ref({})
const observationPayload = ref({})

const assessment = computed(() => payload.value || {})
const gates = computed(() => Array.isArray(assessment.value.gates) ? assessment.value.gates : [])
const evidence = computed(() => assessment.value.evidence || {})
const summary = computed(() => assessment.value.summary || assessment.value.metrics || {})
const dailyActions = computed(() => Array.isArray(assessment.value.daily_action_plan) ? assessment.value.daily_action_plan : [])
const nextActions = computed(() => {
  if (Array.isArray(assessment.value.next_convergence_steps)) return assessment.value.next_convergence_steps
  if (Array.isArray(assessment.value.next_actions)) return assessment.value.next_actions
  return []
})
const canExit = computed(() => Boolean(assessment.value.g2_can_exit_now))
const verdict = computed(() => {
  if (assessment.value.verdict) {
    return String(assessment.value.verdict)
      .replace('G2 退出', 'G3融合')
      .replace('G2 暂不退出', 'G3仍需观察')
  }
  return 'G3最终版仍处于融合观察阶段，G2只作为空档补位能力保留。'
})
const acceptedDays = computed(() => Number(assessment.value.accepted_observation_days ?? observationPayload.value.accepted_observation_days ?? 0))
const acceptedDates = computed(() => Array.isArray(assessment.value.accepted_observation_dates) ? assessment.value.accepted_observation_dates : (observationPayload.value.accepted_observation_dates || []))
const snapshots = computed(() => Array.isArray(observationPayload.value.snapshots) ? observationPayload.value.snapshots : [])
const blockingGateCount = computed(() => gates.value.filter((item) => item.blocking || item.status === 'blocked' || item.ok === false).length)
const firstBlockingGate = computed(() => {
  const item = gates.value.find((gate) => gate.blocking || gate.status === 'blocked' || gate.ok === false)
  return item?.name || item?.key || '暂无阻断'
})
const evidenceRows = computed(() => [
  {
    label: '工作流',
    value: evidence.value.workflow?.ok ? '通过' : '阻断',
    detail: evidence.value.workflow?.diagnosis_code ? statusWithZh(evidence.value.workflow.diagnosis_code) : (evidence.value.workflow?.entry_date || '--')
  },
  {
    label: '观察日',
    value: `${acceptedDays.value}/30`,
    detail: acceptedDates.value.slice(-5).join(', ') || '--'
  },
  {
    label: '观察新鲜度',
    value: evidence.value.observation_freshness_audit?.ok ? '通过' : '阻断',
    detail: observationFreshnessDetail.value
  },
  {
    label: '观察质量',
    value: evidence.value.observation_quality_audit?.ok ? '通过' : '阻断',
    detail: observationQualityDetail.value
  },
  {
    label: '融合窗口',
    value: evidence.value.g2_retirement_window_audit?.ok ? '已规划' : '待规划',
    detail: retirementWindowDetail.value
  },
  {
    label: '交易许可',
    value: evidence.value.strategy_trade_permission_audit?.ok ? permissionLabel.value : '暂停',
    detail: tradePermissionDetail.value
  },
  {
    label: '正式交易',
    value: assessment.value.formal_buy_signal === false ? '锁定' : '需复核',
    detail: 'formal/auto/order path must stay closed'
  },
  {
    label: '纸面台账',
    value: evidence.value.workflow?.paper_execution_path ? '已接入' : '待补齐',
    detail: '每张合格票据必须可复现'
  },
  {
    label: '近期回放',
    value: evidence.value.recent_replay_audit?.ok ? '通过' : '阻断',
    detail: recentReplayDetail.value
  },
  {
    label: '实时烟测',
    value: evidence.value.pretrade_smoke_audit?.ok ? '通过' : '阻断',
    detail: pretradeSmokeDetail.value
  },
  {
    label: '纸面复现',
    value: evidence.value.current_paper_execution_audit?.ok ? '通过' : '阻断',
    detail: currentPaperDetail.value
  },
  {
    label: '自动观察',
    value: evidence.value.observation_scheduler_audit?.ok ? '通过' : '阻断',
    detail: observationSchedulerDetail.value
  },
  {
    label: '影子监控',
    value: evidence.value.shadow_monitor_audit?.ok ? '通过' : '阻断',
    detail: shadowMonitorDetail.value
  }
])

const recentReplayDetail = computed(() => {
  const checks = evidence.value.recent_replay_audit?.checks || {}
  const loss = Number(checks.max_account_loss_pct)
  const lossText = Number.isFinite(loss) ? `${(loss * 100).toFixed(1)}%` : '--'
  return `${checks.recent_trade_count ?? 0}笔 / ${checks.recent_entry_day_count ?? 0}日，最大单笔账户亏损 ${lossText}`
})

const pretradeSmokeDetail = computed(() => {
  const checks = evidence.value.pretrade_smoke_audit?.checks || {}
  const age = Number(checks.age_hours)
  const ageText = Number.isFinite(age) ? `${age.toFixed(2)}小时` : '--'
  return `${checks.verdict || '--'}，票据 ${checks.qualified_ticket_count ?? 0}/${checks.runtime_qualified_ticket_count ?? 0}，${ageText}`
})

const currentPaperDetail = computed(() => {
  const checks = evidence.value.current_paper_execution_audit?.checks || {}
  return `票据 ${checks.matched_paper_count ?? 0}/${checks.qualified_ticket_count ?? 0}，缺失 ${checks.missing_paper_count ?? 0}`
})

const observationSchedulerDetail = computed(() => {
  const checks = evidence.value.observation_scheduler_audit?.checks || {}
  return `下次 ${checks.next_run_time || '--'}，30m修复 ${checks.auto_repair_minute30 ? '开' : '关'}`
})

const observationFreshnessDetail = computed(() => {
  const checks = evidence.value.observation_freshness_audit?.checks || {}
  return `最新 ${checks.latest_accepted_observation_date || '--'}，工作流 ${checks.workflow_entry_date || '--'}`
})

const observationQualityDetail = computed(() => {
  const checks = evidence.value.observation_quality_audit?.checks || {}
  const rate = Number(checks.accepted_rate)
  const rateText = Number.isFinite(rate) ? `${(rate * 100).toFixed(1)}%` : '--'
  return `通过率 ${rateText}，阻断 ${checks.blocked_count ?? 0}，连续阻断 ${checks.consecutive_blocked_count ?? 0}`
})

const retirementWindowDetail = computed(() => {
  const checks = evidence.value.g2_retirement_window_audit?.checks || {}
  const progress = Number(checks.progress)
  const progressText = Number.isFinite(progress) ? `${(progress * 100).toFixed(1)}%` : '--'
  return `最早 ${checks.earliest_g2_retirement_review_date || '--'}，剩余 ${checks.remaining_observation_days ?? '--'} 天，进度 ${progressText}`
})

const permissionLabel = computed(() => {
  const decision = evidence.value.strategy_trade_permission_audit?.decision
  if (decision === 'reduce_risk') return '降权'
  if (decision === 'allow_shadow_buy') return '允许'
  return decision || '通过'
})

const tradePermissionDetail = computed(() => {
  const audit = evidence.value.strategy_trade_permission_audit || {}
  const checks = audit.checks || {}
  const scale = Number(checks.position_scale)
  const scaleText = Number.isFinite(scale) ? scale.toFixed(2) : '--'
  return `${audit.decision || '--'}，账户 ${checks.account_risk_action || '--'}，路线 ${checks.route_health_ok ? '健康' : '异常'}，仓位系数 ${scaleText}`
})

const shadowMonitorDetail = computed(() => {
  const checks = evidence.value.shadow_monitor_audit?.checks || {}
  return `下次 ${checks.next_run_time || '--'}，刷新 ${checks.run_current_refresh ? '开' : '关'}，邮件 ${checks.email_config_ok ? '可用' : '异常'}`
})

function gateType(item) {
  if (item?.ok) return 'success'
  if (item?.blocking || item?.status === 'blocked') return 'danger'
  return 'warning'
}

function gateText(item) {
  if (item?.ok) return '通过'
  if (item?.blocking || item?.status === 'blocked') return '阻断'
  return '观察'
}

function actionType(priority) {
  if (priority === 'critical') return 'danger'
  if (priority === 'high') return 'warning'
  return 'info'
}

function actionStatusLabel(status) {
  const labels = {
    open: '待处理',
    in_progress: '进行中',
    active: '生效中',
    ready: '可启动',
    review: '待复核'
  }
  return labels[status] || status || '--'
}

function routeName(route) {
  const names = {
    institutional_mainwave: '机构主升',
    panic_repair: '恐慌修复',
    g2_gap_supplement: 'G2空档补位',
    old_g3_route_v3: '旧G3兼容',
    old_g3_route: '旧G3兼容'
  }
  return names[route] || route || '--'
}

function fmt(value, digits = 2) {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(digits) : '--'
}

function pct(value) {
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : '--'
}

function observationSeverityType(severity) {
  if (severity === 'info') return 'success'
  if (severity === 'warning') return 'warning'
  if (severity === 'error') return 'danger'
  return 'info'
}

function observationCategoryLabel(category) {
  const labels = {
    accepted: '已验收',
    auto_repaired_accepted: '修复后验收',
    data_repair_failed: '数据修复失败',
    data_gap_30m: '30m缺口',
    paper_execution_required: '待纸面执行',
    workflow_blocked: '工作流阻断',
    pretrade_smoke_blocked: '盘前烟测阻断',
    unknown_blocked: '未知阻断'
  }
  return labels[category] || category || '--'
}

function observationActionLabel(action) {
  const labels = {
    none: '无需处理',
    keep_repair_evidence: '保留修复证据',
    inspect_minute30_repair: '检查30m修复',
    repair_minute30_data: '补30m数据',
    record_paper_execution: '补纸面台账',
    fix_workflow: '修复工作流',
    inspect_pretrade_smoke: '检查烟测',
    inspect_observation: '人工复核'
  }
  return labels[action] || action || '--'
}

function blockerText(row) {
  const blockers = Array.isArray(row?.blockers) ? row.blockers : []
  if (!blockers.length) return '无'
  return blockers.map((item) => `${item.gate || 'gate'}: ${item.message || '--'}`).join('；')
}

async function load() {
  loading.value = true
  try {
    const [assessmentData, observations] = await Promise.all([
      getGen3StateAlphaReplacementAssessment(),
      getGen3StateAlphaObservationSnapshots({ limit: 80 })
    ])
    payload.value = assessmentData
    observationPayload.value = observations
  } catch (error) {
    ElMessage.warning(error?.message || '读取 G3 替代门槛失败')
  } finally {
    loading.value = false
  }
}

async function runSnapshot() {
  snapshotLoading.value = true
  try {
    const result = await runGen3StateAlphaObservationSchedulerOnce({ run_smoke: true, refresh_smoke: true, auto_repair_minute30: true })
    if (result?.ok) ElMessage.success('今日 G3 观察已通过')
    else ElMessage.warning(result?.blockers?.[0]?.message || '今日 G3 观察存在阻断')
    await load()
  } catch (error) {
    ElMessage.warning(error?.message || '记录 G3 观察失败')
  } finally {
    snapshotLoading.value = false
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
.gate-row span,
.evidence-list small {
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

.gate-list,
.daily-action-list,
.next-list {
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

.evidence-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.evidence-list > div,
.daily-action-card,
.next-list > div {
  border: 1px solid #eef2f6;
  border-radius: 8px;
  padding: 10px;
  background: #f8fafc;
}

.daily-action-list {
  margin-bottom: 12px;
}

.daily-action-card p {
  margin-top: 8px;
  color: #667085;
  line-height: 1.5;
}

.daily-action-title {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.daily-action-title strong {
  flex: 1;
  color: #1f2a44;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.daily-action-title span {
  color: #667085;
  font-size: 12px;
}

.evidence-list span {
  display: block;
  color: #667085;
  font-size: 12px;
  margin-bottom: 5px;
}

.evidence-list strong {
  display: block;
  color: #1f2a44;
  font-size: 17px;
  margin-bottom: 3px;
}

@media (max-width: 1320px) {
  .metric-grid {
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
  .evidence-list {
    grid-template-columns: 1fr;
  }
}
</style>
