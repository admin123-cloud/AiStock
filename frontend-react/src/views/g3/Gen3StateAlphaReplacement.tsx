import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert } from '@/components/catalyst/alert'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/catalyst/table'
import { useToast } from '@/components/toast'
import { Card, EmptyState, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import { statusWithZh } from '@/utils/g3StatusText'
import {
  getGen3StateAlphaObservationSnapshots,
  getGen3StateAlphaReplacementAssessment,
  runGen3StateAlphaObservationSchedulerOnce,
} from '@/api/trading'

type Row = Record<string, any>

function fmt(value: any, digits = 2): string {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(digits) : '--'
}

function pct(value: any): string {
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : '--'
}

const ROUTE_NAMES: Record<string, string> = {
  institutional_mainwave: '机构主升',
  panic_repair: '恐慌修复',
  g2_gap_supplement: 'G2空档补位',
  old_g3_route_v3: '旧G3兼容',
  old_g3_route: '旧G3兼容',
}

function routeName(route: any): string {
  return ROUTE_NAMES[route] || route || '--'
}

function gateType(item: Row): 'emerald' | 'red' | 'amber' {
  if (item?.ok) return 'emerald'
  if (item?.blocking || item?.status === 'blocked') return 'red'
  return 'amber'
}

function gateText(item: Row): string {
  if (item?.ok) return '通过'
  if (item?.blocking || item?.status === 'blocked') return '阻断'
  return '观察'
}

function actionType(priority: any): 'red' | 'amber' | 'zinc' {
  if (priority === 'critical') return 'red'
  if (priority === 'high') return 'amber'
  return 'zinc'
}

const ACTION_STATUS_LABELS: Record<string, string> = {
  open: '待处理',
  in_progress: '进行中',
  active: '生效中',
  ready: '可启动',
  review: '待复核',
}

function actionStatusLabel(status: any): string {
  return ACTION_STATUS_LABELS[status] || status || '--'
}

function observationSeverityType(severity: any): 'emerald' | 'amber' | 'red' | 'zinc' {
  if (severity === 'info') return 'emerald'
  if (severity === 'warning') return 'amber'
  if (severity === 'error') return 'red'
  return 'zinc'
}

const OBSERVATION_CATEGORY_LABELS: Record<string, string> = {
  accepted: '已验收',
  auto_repaired_accepted: '修复后验收',
  data_repair_failed: '数据修复失败',
  data_gap_30m: '30m缺口',
  paper_execution_required: '待纸面执行',
  workflow_blocked: '工作流阻断',
  pretrade_smoke_blocked: '盘前烟测阻断',
  unknown_blocked: '未知阻断',
}

function observationCategoryLabel(category: any): string {
  return OBSERVATION_CATEGORY_LABELS[category] || category || '--'
}

const OBSERVATION_ACTION_LABELS: Record<string, string> = {
  none: '无需处理',
  keep_repair_evidence: '保留修复证据',
  inspect_minute30_repair: '检查30m修复',
  repair_minute30_data: '补30m数据',
  record_paper_execution: '补纸面台账',
  fix_workflow: '修复工作流',
  inspect_pretrade_smoke: '检查烟测',
  inspect_observation: '人工复核',
}

function observationActionLabel(action: any): string {
  return OBSERVATION_ACTION_LABELS[action] || action || '--'
}

function blockerText(row: Row): string {
  const blockers = Array.isArray(row?.blockers) ? row.blockers : []
  if (!blockers.length) return '无'
  return blockers.map((item: Row) => `${item.gate || 'gate'}: ${item.message || '--'}`).join('；')
}


function EmptyRow({ colSpan, text }: { colSpan: number; text: string }) {
  return (
    <TableRow>
      <TableCell colSpan={colSpan} className="text-center text-zinc-500 dark:text-zinc-400">
        {text}
      </TableCell>
    </TableRow>
  )
}

export default function Gen3StateAlphaReplacement() {
  const toast = useToast()
  const [loading, setLoading] = useState(false)
  const [snapshotLoading, setSnapshotLoading] = useState(false)
  const [payload, setPayload] = useState<Row>({})
  const [observationPayload, setObservationPayload] = useState<Row>({})

  const assessment = payload || {}
  const gates = useMemo(() => (Array.isArray(assessment.gates) ? assessment.gates : []), [assessment])
  const evidence = useMemo(() => assessment.evidence || {}, [assessment])
  const summary = useMemo(() => assessment.summary || assessment.metrics || {}, [assessment])
  const dailyActions = useMemo(() => (Array.isArray(assessment.daily_action_plan) ? assessment.daily_action_plan : []), [assessment])
  const nextActions = useMemo(() => {
    if (Array.isArray(assessment.next_convergence_steps)) return assessment.next_convergence_steps
    if (Array.isArray(assessment.next_actions)) return assessment.next_actions
    return []
  }, [assessment])
  const canExit = Boolean(assessment.g2_can_exit_now)
  const verdict = useMemo(() => {
    if (assessment.verdict) {
      return String(assessment.verdict).replace('G2 退出', 'G3融合').replace('G2 暂不退出', 'G3仍需观察')
    }
    return 'G3最终版仍处于融合观察阶段，G2只作为空档补位能力保留。'
  }, [assessment])
  const acceptedDays = Number(
    assessment.accepted_observation_days ?? observationPayload.accepted_observation_days ?? 0,
  )
  const acceptedDates = Array.isArray(assessment.accepted_observation_dates)
    ? assessment.accepted_observation_dates
    : observationPayload.accepted_observation_dates || []
  const snapshots = Array.isArray(observationPayload.snapshots) ? observationPayload.snapshots : []
  const blockingGateCount = gates.filter((item: Row) => item.blocking || item.status === 'blocked' || item.ok === false).length
  const firstBlockingGate = useMemo(() => {
    const item = gates.find((gate: Row) => gate.blocking || gate.status === 'blocked' || gate.ok === false)
    return item?.name || item?.key || '暂无阻断'
  }, [gates])

  const recentReplayDetail = useMemo(() => {
    const checks = evidence.recent_replay_audit?.checks || {}
    const loss = Number(checks.max_account_loss_pct)
    const lossText = Number.isFinite(loss) ? `${(loss * 100).toFixed(1)}%` : '--'
    return `${checks.recent_trade_count ?? 0}笔 / ${checks.recent_entry_day_count ?? 0}日，最大单笔账户亏损 ${lossText}`
  }, [evidence])

  const pretradeSmokeDetail = useMemo(() => {
    const checks = evidence.pretrade_smoke_audit?.checks || {}
    const age = Number(checks.age_hours)
    const ageText = Number.isFinite(age) ? `${age.toFixed(2)}小时` : '--'
    return `${checks.verdict || '--'}，票据 ${checks.qualified_ticket_count ?? 0}/${checks.runtime_qualified_ticket_count ?? 0}，${ageText}`
  }, [evidence])

  const currentPaperDetail = useMemo(() => {
    const checks = evidence.current_paper_execution_audit?.checks || {}
    return `票据 ${checks.matched_paper_count ?? 0}/${checks.qualified_ticket_count ?? 0}，缺失 ${checks.missing_paper_count ?? 0}`
  }, [evidence])

  const observationSchedulerDetail = useMemo(() => {
    const checks = evidence.observation_scheduler_audit?.checks || {}
    return `下次 ${checks.next_run_time || '--'}，30m修复 ${checks.auto_repair_minute30 ? '开' : '关'}`
  }, [evidence])

  const observationFreshnessDetail = useMemo(() => {
    const checks = evidence.observation_freshness_audit?.checks || {}
    return `最新 ${checks.latest_accepted_observation_date || '--'}，工作流 ${checks.workflow_entry_date || '--'}`
  }, [evidence])

  const observationQualityDetail = useMemo(() => {
    const checks = evidence.observation_quality_audit?.checks || {}
    const rate = Number(checks.accepted_rate)
    const rateText = Number.isFinite(rate) ? `${(rate * 100).toFixed(1)}%` : '--'
    return `通过率 ${rateText}，阻断 ${checks.blocked_count ?? 0}，连续阻断 ${checks.consecutive_blocked_count ?? 0}`
  }, [evidence])

  const retirementWindowDetail = useMemo(() => {
    const checks = evidence.g2_retirement_window_audit?.checks || {}
    const progress = Number(checks.progress)
    const progressText = Number.isFinite(progress) ? `${(progress * 100).toFixed(1)}%` : '--'
    return `最早 ${checks.earliest_g2_retirement_review_date || '--'}，剩余 ${checks.remaining_observation_days ?? '--'} 天，进度 ${progressText}`
  }, [evidence])

  const permissionLabel = useMemo(() => {
    const decision = evidence.strategy_trade_permission_audit?.decision
    if (decision === 'reduce_risk') return '降权'
    if (decision === 'allow_shadow_buy') return '允许'
    return decision || '通过'
  }, [evidence])

  const tradePermissionDetail = useMemo(() => {
    const audit = evidence.strategy_trade_permission_audit || {}
    const checks = audit.checks || {}
    const scale = Number(checks.position_scale)
    const scaleText = Number.isFinite(scale) ? scale.toFixed(2) : '--'
    return `${audit.decision || '--'}，账户 ${checks.account_risk_action || '--'}，路线 ${checks.route_health_ok ? '健康' : '异常'}，仓位系数 ${scaleText}`
  }, [evidence])

  const shadowMonitorDetail = useMemo(() => {
    const checks = evidence.shadow_monitor_audit?.checks || {}
    return `下次 ${checks.next_run_time || '--'}，刷新 ${checks.run_current_refresh ? '开' : '关'}，邮件 ${checks.email_config_ok ? '可用' : '异常'}`
  }, [evidence])

  const evidenceRows = useMemo(() => [
    {
      label: '工作流',
      value: evidence.workflow?.ok ? '通过' : '阻断',
      detail: evidence.workflow?.diagnosis_code ? statusWithZh(evidence.workflow.diagnosis_code) : (evidence.workflow?.entry_date || '--'),
    },
    {
      label: '观察日',
      value: `${acceptedDays}/30`,
      detail: acceptedDates.slice(-5).join(', ') || '--',
    },
    {
      label: '观察新鲜度',
      value: evidence.observation_freshness_audit?.ok ? '通过' : '阻断',
      detail: observationFreshnessDetail,
    },
    {
      label: '观察质量',
      value: evidence.observation_quality_audit?.ok ? '通过' : '阻断',
      detail: observationQualityDetail,
    },
    {
      label: '融合窗口',
      value: evidence.g2_retirement_window_audit?.ok ? '已规划' : '待规划',
      detail: retirementWindowDetail,
    },
    {
      label: '交易许可',
      value: evidence.strategy_trade_permission_audit?.ok ? permissionLabel : '暂停',
      detail: tradePermissionDetail,
    },
    {
      label: '正式交易',
      value: assessment.formal_buy_signal === false ? '锁定' : '需复核',
      detail: 'formal/auto/order path must stay closed',
    },
    {
      label: '纸面台账',
      value: evidence.workflow?.paper_execution_path ? '已接入' : '待补齐',
      detail: '每张合格票据必须可复现',
    },
    {
      label: '近期回放',
      value: evidence.recent_replay_audit?.ok ? '通过' : '阻断',
      detail: recentReplayDetail,
    },
    {
      label: '实时烟测',
      value: evidence.pretrade_smoke_audit?.ok ? '通过' : '阻断',
      detail: pretradeSmokeDetail,
    },
    {
      label: '纸面复现',
      value: evidence.current_paper_execution_audit?.ok ? '通过' : '阻断',
      detail: currentPaperDetail,
    },
    {
      label: '自动观察',
      value: evidence.observation_scheduler_audit?.ok ? '通过' : '阻断',
      detail: observationSchedulerDetail,
    },
    {
      label: '影子监控',
      value: evidence.shadow_monitor_audit?.ok ? '通过' : '阻断',
      detail: shadowMonitorDetail,
    },
  ], [evidence, acceptedDays, acceptedDates, observationFreshnessDetail, observationQualityDetail, retirementWindowDetail, permissionLabel, tradePermissionDetail, recentReplayDetail, pretradeSmokeDetail, currentPaperDetail, observationSchedulerDetail, shadowMonitorDetail, assessment.formal_buy_signal])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [assessmentData, observations] = await Promise.all([
        getGen3StateAlphaReplacementAssessment({}) as unknown as Row,
        getGen3StateAlphaObservationSnapshots({ limit: 80 }) as unknown as Row,
      ])
      setPayload(assessmentData)
      setObservationPayload(observations)
    } catch (error: any) {
      toast.info(error?.message || '读取 G3 替代门槛失败')
    } finally {
      setLoading(false)
    }
  }, [toast])

  const runSnapshot = useCallback(async () => {
    setSnapshotLoading(true)
    try {
      const result = await runGen3StateAlphaObservationSchedulerOnce({
        run_smoke: true,
        refresh_smoke: true,
        auto_repair_minute30: true,
      }) as unknown as Row
      if (result?.ok) toast.success('今日 G3 观察已通过')
      else toast.info(result?.blockers?.[0]?.message || '今日 G3 观察存在阻断')
      await load()
    } catch (error: any) {
      toast.info(error?.message || '记录 G3 观察失败')
    } finally {
      setSnapshotLoading(false)
    }
  }, [toast, load])

  useEffect(() => {
    load()
  }, [load])

  return (
    <div className="flex flex-col gap-3.5 text-zinc-900 dark:text-zinc-100">
      <PageHeader
        title="G3融合成熟度"
        subtitle="G3 State Alpha · 用工作流、历史样本、未来函数审计、30 日观察和纸面执行一致性，确认 G2 已作为补位能力稳定归入 G3 最终版。"
        actions={
          <>
            <Button outline disabled={snapshotLoading} onClick={runSnapshot}>{snapshotLoading ? '记录中…' : '记录今日观察'}</Button>
            <Button color="blue" disabled={loading} onClick={load}>{loading ? '刷新中…' : '刷新'}</Button>
          </>
        }
      />

      <Alert color={canExit ? 'green' : 'amber'}>{verdict}</Alert>

      <section className="grid grid-cols-1 gap-2.5 md:grid-cols-3 min-[1321px]:grid-cols-6">
        <StatCard
          label="融合状态"
          value={canExit ? '达标' : '观察'}
          tone={canExit ? 'up' : 'warning'}
          hint="以动态审计为准"
        />
        <StatCard
          label="观察日"
          value={`${acceptedDays}/30`}
          tone="info"
          hint={`剩余 ${summary.accepted_observation_days_remaining ?? Math.max(0, 30 - acceptedDays)} 天`}
        />
        <StatCard label="历史交易" value={fmt(summary.closed_trade_count ?? summary.trade_count, 0)} hint={`胜率 ${pct(summary.win_rate)}`} />
        <StatCard label="历史收益" value={pct(summary.total_return)} hint={`最大回撤 ${pct(summary.max_drawdown)}`} />
        <StatCard
          label="阻断项"
          value={fmt(blockingGateCount, 0)}
          tone={blockingGateCount > 0 ? 'down' : 'up'}
          hint={firstBlockingGate}
        />
        <StatCard
          label="当前主路线"
          value={routeName(assessment.selected_route || evidence.workflow?.selected_route)}
          hint={assessment.contract || '--'}
          className="min-w-0"
        />
      </section>

      <Card>
        <SectionTitle title="融合验收项" />
        <p className="mb-3 text-sm text-zinc-500 dark:text-zinc-400">
          所有关键项通过前，G2 只作为 G3 的空档补位能力观察，不再作为前台并列策略。
        </p>
        <div className="flex flex-col gap-2.5">
          {gates.map((item: Row) => (
            <div key={item.key || item.name} className="grid grid-cols-[64px_minmax(0,1fr)] items-start gap-2.5 border-b border-zinc-100 pb-2.5 last:border-b-0 last:pb-0 dark:border-white/10">
              <Badge color={gateType(item)}>{gateText(item)}</Badge>
              <div>
                <strong className="mb-0.5 block text-zinc-900 dark:text-zinc-100">{item.name || item.key}</strong>
                <span className="text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">{item.detail || item.message || '--'}</span>
              </div>
            </div>
          ))}
          {gates.length === 0 ? (
            <EmptyState text="暂无替代门槛" />
          ) : null}
        </div>
      </Card>

      <section className="grid grid-cols-1 gap-3.5 lg:grid-cols-2">
        <Card>
          <SectionTitle title="观察快照" />
          <p className="mb-3 text-sm text-zinc-500 dark:text-zinc-400">30 个通过观察日是 G3 接管前的硬门槛。</p>
          <div className="max-h-[420px] overflow-auto">
            <Table dense striped className="[&_td]:whitespace-nowrap">
              <TableHead>
                <TableRow>
                  <TableHeader>日期</TableHeader>
                  <TableHeader>状态</TableHeader>
                  <TableHeader>烟测</TableHeader>
                  <TableHeader className="text-right">票据</TableHeader>
                  <TableHeader>分类</TableHeader>
                  <TableHeader>动作</TableHeader>
                  <TableHeader>30m</TableHeader>
                  <TableHeader>纸面</TableHeader>
                  <TableHeader>记录时间</TableHeader>
                  <TableHeader>阻断原因</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {snapshots.length === 0 ? <EmptyRow colSpan={10} text="暂无观察快照" /> : snapshots.map((row: Row, i: number) => (
                  <TableRow key={`${row.observation_date}-${i}`}>
                    <TableCell className="tabular-nums">{row.observation_date}</TableCell>
                    <TableCell><Badge color={row.accepted ? 'emerald' : 'red'}>{row.accepted ? '通过' : '阻断'}</Badge></TableCell>
                    <TableCell>{row.smoke_verdict}</TableCell>
                    <TableCell className="text-right tabular-nums">{fmt(row.qualified_ticket_count, 0)}</TableCell>
                    <TableCell><Badge color={observationSeverityType(row.severity)}>{observationCategoryLabel(row.failure_category)}</Badge></TableCell>
                    <TableCell className="max-w-40 truncate">{observationActionLabel(row.action_required)}</TableCell>
                    <TableCell><Badge color={row.m30_ok ? 'emerald' : 'red'}>{row.m30_ok ? '通过' : '阻断'}</Badge></TableCell>
                    <TableCell><Badge color={row.paper_ok ? 'emerald' : 'red'}>{row.paper_ok ? '通过' : '阻断'}</Badge></TableCell>
                    <TableCell className="tabular-nums">{row.created_at}</TableCell>
                    <TableCell className="max-w-60 truncate">{blockerText(row)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </Card>

        <Card>
          <SectionTitle title="证据摘要" />
          <p className="mb-3 text-sm text-zinc-500 dark:text-zinc-400">融合判断必须能回到工作流、历史审计、影子票据和纸面台账。</p>
          <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
            {evidenceRows.map((item) => (
              <Card key={item.label} className="gap-1 p-2.5">
                <span className="mb-1 block text-xs text-zinc-500 dark:text-zinc-400">{item.label}</span>
                <strong className="mb-0.5 block text-[17px] text-zinc-900 dark:text-zinc-100">{item.value}</strong>
                <small className="block text-xs leading-relaxed break-words text-zinc-500 dark:text-zinc-400">{item.detail}</small>
              </Card>
            ))}
          </div>
        </Card>
      </section>

      <Card>
        <SectionTitle title="下一步" />
        <p className="mb-3 text-sm text-zinc-500 dark:text-zinc-400">
          这些动作通过后，G3 最终版才算把 G2 成熟能力真正吃进去，而不是只换了名字。
        </p>
        <div className="mb-3 flex flex-col gap-2.5">
          {dailyActions.map((item: Row) => (
            <Card key={item.key || item.title} className="p-2.5">
              <div className="flex min-w-0 items-center gap-2">
                <Badge color={actionType(item.priority)}>{item.priority || 'normal'}</Badge>
                <strong className="min-w-0 flex-1 truncate text-zinc-900 dark:text-zinc-100">{item.title || item.key}</strong>
                <span className="text-xs text-zinc-500 dark:text-zinc-400">{actionStatusLabel(item.status)}</span>
              </div>
              <p className="mt-2 text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">{item.detail || '--'}</p>
            </Card>
          ))}
          {dailyActions.length === 0 ? (
            <EmptyState text="暂无今日操作计划" />
          ) : null}
        </div>
        <div className="flex flex-col gap-2.5">
          {nextActions.map((item: string) => (
            <Card key={item} className="px-3 py-2.5 text-sm text-zinc-700 dark:text-zinc-300">
              {item}
            </Card>
          ))}
          {nextActions.length === 0 ? (
            <EmptyState text="暂无下一步建议" />
          ) : null}
        </div>
      </Card>
    </div>
  )
}
