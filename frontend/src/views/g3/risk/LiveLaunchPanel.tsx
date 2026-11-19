// G3 实战启动包（上半部分：决策卡 / readiness audit / live replay cockpit / 历史决策复盘）
import { Alert } from '@/components/catalyst/alert'
import { Card } from '@/components/ui'
import { AuditGrid, AuditMain, Btn, DataTable, DecisionCard, Panel, Tag, useRisk } from './shared'
import type { Col } from './shared'
import {
  isStrategyTuningTaskSaving,
  truthy,
  type Row
} from './labels'
import StrategyTuningBlock from './StrategyTuningBlock'
import LiveLaunchTail from './LiveLaunchTail'

export default function LiveLaunchPanel() {
  const { v, a } = useRisk()
  const decision = v.liveLaunchDecision
  const audit = v.liveLaunchReadinessAudit
  const auditSummary = v.liveLaunchReadinessAuditSummary

  const hardGapCols: Col[] = [
    { key: 'gap', label: 'gap', width: 210 },
    { key: 'count', label: 'count', width: 86, align: 'right' },
    { key: 'next_action', label: 'next action', minWidth: 300 },
    { key: 'trade_impact', label: 'trade impact', minWidth: 240 }
  ]

  const riskCols: Col[] = [
    { key: 'priority', label: '#', width: 64 },
    { key: 'origin', label: '来源', width: 180 },
    { key: 'learning_status', label: '状态', width: 120 },
    { key: 'issue_area', label: '复盘轴', width: 150 },
    { key: 'object', label: '对象', minWidth: 180 },
    { key: 'problem_signal', label: '问题信号', minWidth: 300 },
    { key: 'suggested_learning', label: '学习方向', minWidth: 320 },
    { key: 'natural_trade_boundary', label: '自然交易边界', minWidth: 320 }
  ]

  const cockpitActionCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'kind', label: 'kind', width: 170 },
    { key: 'axis', label: 'axis', width: 170 },
    { key: 'status', label: 'status', width: 110 },
    { key: 'object', label: 'object', minWidth: 190 },
    { key: 'next_action', label: 'next action', minWidth: 340 },
    { key: 'review_boundary', label: 'boundary', minWidth: 360 }
  ]

  const cockpitAxisCols: Col[] = [
    { key: 'axis_label', label: 'axis', width: 190 },
    { key: 'task_count', label: 'tasks', width: 80, align: 'right' },
    { key: 'todo_count', label: 'todo', width: 80, align: 'right' },
    { key: 'followup_count', label: 'followup', width: 94, align: 'right' },
    { key: 'progress_pct', label: 'progress', width: 94, align: 'right' },
    { key: 'next_review_object', label: 'next object', minWidth: 200 },
    { key: 'review_boundary', label: 'boundary', minWidth: 360 }
  ]

  const cockpitCheckCols: Col[] = [
    { key: 'check', label: 'check', width: 190 },
    { key: 'status', label: 'status', width: 110 },
    { key: 'evidence', label: 'evidence', minWidth: 180 },
    { key: 'required_behavior', label: 'required behavior', minWidth: 360 }
  ]

  const histAxisCols: Col[] = [
    { key: 'axis_label', label: 'axis', width: 170 },
    { key: 'axis_status', label: 'status', width: 130 },
    { key: 'task_count', label: 'tasks', width: 80, align: 'right' },
    { key: 'todo_count', label: 'todo', width: 80, align: 'right' },
    { key: 'followup_count', label: 'followup', width: 96, align: 'right' },
    { key: 'reviewed_count', label: 'reviewed', width: 96, align: 'right' },
    { key: 'next_review_object', label: 'next object', minWidth: 180 },
    { key: 'next_review_focus', label: 'focus', minWidth: 340 },
    { key: 'optimization_boundary', label: 'boundary', minWidth: 340 }
  ]

  const histTaskCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'severity', label: 'risk', width: 90 },
    { key: 'axis_label', label: 'axis', width: 170 },
    { key: 'entry_date', label: 'entry', width: 110 },
    { key: 'object', label: 'object', minWidth: 170 },
    { key: 'route', label: 'route', width: 160 },
    { key: 'net_ret', label: 'ret', width: 90, align: 'right' },
    { key: 'problem_type', label: 'problem', minWidth: 220 },
    { key: 'replay_focus', label: 'replay focus', minWidth: 360 },
    { key: 'optimization_boundary', label: 'boundary', minWidth: 340 },
    {
      key: 'review',
      label: 'review',
      width: 250,
      render: (row: Row) => (
        <div className="flex gap-1.5">
          <Btn tone="success" loading={isStrategyTuningTaskSaving(row, v)} onClick={() => a.saveStrategyTuningTaskReview(row, 'validated')}>
            valid
          </Btn>
          <Btn tone="warning" loading={isStrategyTuningTaskSaving(row, v)} onClick={() => a.saveStrategyTuningTaskReview(row, 'issue_found')}>
            issue
          </Btn>
          <Btn tone="info" loading={isStrategyTuningTaskSaving(row, v)} onClick={() => a.saveStrategyTuningTaskReview(row, 'continue_watch')}>
            watch
          </Btn>
        </div>
      )
    }
  ]

  return (
    <Panel
      className="mt-5"
      title="G3 实战启动包"
      desc="把烟测、准入、盘前步骤和自然交易边界合并成一张启动状态卡；它只用于复盘和放行判断，不开启自动下单。"
      actions={
        <>
          <Btn tone="primary" loading={v.liveLaunchPacketLoading} onClick={() => a.refreshLiveLaunchPacket(false)}>
            读取启动包
          </Btn>
          <Btn tone="primary" loading={v.liveLaunchPacketLoading} onClick={() => a.refreshLiveLaunchPacket(true)}>
            重建启动包
          </Btn>
          <Btn tone="success" loading={v.liveLaunchSnapshotLoading} onClick={() => a.recordLiveLaunchReviewSnapshot()}>
            记录启动快照
          </Btn>
        </>
      }
    >
      <DecisionCard
        tone={v.liveLaunchDecisionTone}
        label="实战放行判断"
        strong={decision.decision_label || '--'}
        text={decision.primary_blocker || decision.next_action || '--'}
        detail={[
          { label: '人工实盘复核', value: truthy(decision.can_enter_live_manual_review) ? '可进入' : '暂不放行' },
          {
            label: '阻断/证据',
            value: `${decision.blocking_command_count ?? '--'} / ${decision.pending_evidence_count ?? '--'}`
          },
          {
            label: '统一隐患',
            value: `${decision.live_learning_ledger_count ?? '--'} 项；待复盘 ${
              decision.live_learning_needs_review_count ?? '--'
            }`
          }
        ]}
        flags={[
          { tone: truthy(decision.can_buy) ? 'success' : 'danger', text: '真实买入锁定' },
          { tone: 'info', text: '不按单日收益放宽' }
        ]}
      />

      <Card className="mt-4">
        <AuditMain
          label="live readiness audit"
          strong={audit.audit_label || audit.audit_status || '--'}
          text={audit.primary_blocker || audit.next_action || '--'}
        />
        <AuditGrid
          items={[
            { label: 'hard gaps', value: auditSummary.hard_gap_count ?? '--' },
            { label: 'watch gaps', value: auditSummary.watch_gap_count ?? '--' },
            { label: 'broker preflight', value: auditSummary.broker_preflight_status || '--' },
            { label: 'staleness', value: `${auditSummary.broker_staleness_minutes ?? '--'}m` }
          ]}
        />
        {v.liveLaunchReadinessHardGaps.length > 0 && (
          <DataTable rows={v.liveLaunchReadinessHardGaps} cols={hardGapCols} empty="no hard gaps" />
        )}
      </Card>

      {v.liveLaunchDecisionRiskRows.length > 0 && (
        <DataTable rows={v.liveLaunchDecisionRiskRows} cols={riskCols} empty="暂无决策卡隐患" />
      )}

      {/* live replay cockpit */}
      <Card className="mt-4">
        <AuditMain
          label="live replay cockpit"
          strong={`${v.liveReplayCockpitSummary.current_action_key || '--'} / ${
            v.liveReplayCockpitSummary.next_review_axis || '--'
          }`}
          text={v.liveReplayCockpitSummary.natural_trade_boundary || '--'}
        />
        <AuditGrid
          items={[
            { label: 'todo', value: v.liveReplayCockpitSummary.todo_count ?? '--' },
            { label: 'followup', value: v.liveReplayCockpitSummary.followup_count ?? '--' },
            { label: 'progress', value: `${v.liveReplayCockpitSummary.progress_pct ?? 0}%` },
            {
              label: 'trade lock',
              value: truthy(v.liveReplayCockpitSummary.can_execute_trade) ? 'open' : 'locked'
            }
          ]}
        />
        <DataTable rows={v.liveReplayCockpitActionItems} cols={cockpitActionCols} empty="no replay actions" />
        <DataTable rows={v.liveReplayCockpitAxisProgress} cols={cockpitAxisCols} empty="no replay axis progress" />
        <DataTable rows={v.liveReplayCockpitChecks} cols={cockpitCheckCols} empty="no natural trade checks" />
      </Card>

      {/* historical replay audit */}
      <Card className="mt-4">
        <AuditMain
          label="historical replay audit"
          strong={`${v.historicalDecisionReplayAudit.audit_status || '--'} / ${
            v.historicalDecisionReplayAuditSummary.todo_count ?? '--'
          } todo`}
          text={
            v.historicalDecisionReplayAudit.next_action ||
            v.historicalDecisionReplayAudit.natural_trade_boundary ||
            'review historical axes before claiming live readiness'
          }
        />
        <AuditGrid
          items={[
            { label: 'required axes', value: v.historicalDecisionReplayAuditSummary.required_axis_count ?? '--' },
            { label: 'blocked axes', value: v.historicalDecisionReplayAuditSummary.blocked_axis_count ?? '--' },
            { label: 'followup', value: v.historicalDecisionReplayAuditSummary.followup_count ?? '--' },
            {
              label: 'trade lock',
              value: truthy(v.historicalDecisionReplayAuditSummary.can_execute_trade) ? 'open' : 'locked'
            }
          ]}
        />
        {v.historicalDecisionReplayHardGaps.length > 0 && (
          <div className="mt-3">
            <Alert color="amber">
              {v.historicalDecisionReplayHardGaps[0].next_action || v.historicalDecisionReplayHardGaps[0].gap}
            </Alert>
          </div>
        )}
        <DataTable rows={v.historicalDecisionReplayAxisRows} cols={histAxisCols} empty="no historical replay axis audit" />
      </Card>

      {/* historical decision replay */}
      <Card className="mt-4">
        <AuditMain
          label="historical decision replay"
          strong={`${v.historicalDecisionReplaySummary.task_count ?? '--'} tasks / ${
            v.historicalDecisionReplaySummary.loss_trade_count ?? '--'
          } losses`}
          text={
            v.historicalDecisionReplaySummary.natural_trade_boundary ||
            'historical replay only; no trade unlock'
          }
        />
        <AuditGrid
          items={[
            { label: 'trade count', value: v.historicalDecisionReplaySummary.trade_count ?? '--' },
            { label: 'hard stop', value: v.historicalDecisionReplaySummary.hard_stop_count ?? '--' },
            {
              label: 'contract',
              value: truthy(v.historicalDecisionReplaySummary.can_change_strategy_contract) ? 'open' : 'locked'
            },
            {
              label: 'profit-only',
              value: truthy(v.historicalDecisionReplaySummary.profit_only_optimization_allowed) ? 'allowed' : 'disabled'
            }
          ]}
        />
        <DataTable rows={v.historicalDecisionReplayTasks} cols={histTaskCols} empty="no historical replay tasks" />
      </Card>

      <StrategyTuningBlock />
      <LiveLaunchTail />
    </Panel>
  )
}
