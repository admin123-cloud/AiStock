// strategy tuning axis 大区块（旧页 live-launch-panel 内的 strategy tuning audit-card）
import { Card } from '@/components/ui'
import { AuditGrid, AuditMain, Btn, DataTable, useRisk } from './shared'
import type { Col } from './shared'
import { isStrategyTuningTaskSaving, truthy, type Row } from './labels'

export default function StrategyTuningBlock() {
  const { v, a } = useRisk()

  const axisCols: Col[] = [
    { key: 'axis_label', label: 'axis', width: 190 },
    { key: 'risk_count', label: 'risks', width: 80, align: 'right' },
    { key: 'needs_review_count', label: 'review', width: 90, align: 'right' },
    { key: 'watch_count', label: 'watch', width: 86, align: 'right' },
    { key: 'top_objects', label: 'objects', minWidth: 180 },
    { key: 'recommended_review', label: 'review focus', minWidth: 360 },
    { key: 'optimization_boundary', label: 'boundary', minWidth: 360 }
  ]

  const riskCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'axis_label', label: 'axis', width: 180 },
    { key: 'origin', label: 'origin', width: 190 },
    { key: 'learning_status', label: 'status', width: 140 },
    { key: 'object', label: 'object', minWidth: 170 },
    { key: 'problem_signal', label: 'problem', minWidth: 300 },
    { key: 'suggested_learning', label: 'learning', minWidth: 320 }
  ]

  const sessionStepCols: Col[] = [
    { key: 'step', label: '#', width: 56 },
    { key: 'session_stage', label: 'stage', width: 160 },
    { key: 'axis_label', label: 'axis', width: 170 },
    { key: 'suggested_review_result', label: 'suggest', width: 130 },
    { key: 'object', label: 'object', minWidth: 170 },
    { key: 'replay_focus', label: 'current focus', minWidth: 340 },
    { key: 'hidden_risk', label: 'hidden risk', minWidth: 320 },
    { key: 'evidence_required', label: 'evidence', minWidth: 320 }
  ]

  const postGapCols: Col[] = [
    { key: 'gap', label: 'post gap', width: 230 },
    { key: 'next_action', label: 'next action', minWidth: 360 },
    { key: 'trade_impact', label: 'trade impact', minWidth: 220 }
  ]

  const consoleCols: Col[] = [
    { key: 'step', label: '#', width: 56 },
    { key: 'status', label: 'status', width: 100 },
    { key: 'action_label', label: 'action', width: 210 },
    { key: 'current_evidence', label: 'evidence', minWidth: 280 },
    { key: 'next_action', label: 'next action', minWidth: 340 },
    { key: 'done_when', label: 'done when', minWidth: 300 },
    { key: 'trade_impact', label: 'impact', minWidth: 240 },
    {
      key: 'actions',
      label: 'action',
      width: 230,
      render: (row: Row) => (
        <div className="flex gap-1.5">
          <Btn
            tone="primary"
            disabled={!truthy(row.can_execute_now)}
            loading={v.liveActionConsoleLoading || v.brokerSyncLoading || v.brokerPostSyncEvidenceLoading || v.reviewLoading}
            onClick={() => a.handleLiveActionConsoleStep(row)}
          >
            run step
          </Btn>
          <Btn
            tone="warning"
            loading={v.liveActionConsoleReviewSavingKey === row.action_key}
            onClick={() => a.saveLiveActionConsoleStepIssue(row)}
          >
            issue
          </Btn>
        </div>
      )
    }
  ]

  const stepGapCols: Col[] = [
    { key: 'gap', label: 'gap', width: 210 },
    { key: 'next_action', label: 'next action', minWidth: 360 },
    { key: 'trade_impact', label: 'trade impact', minWidth: 220 }
  ]

  const reviewAxisCols: Col[] = [
    { key: 'axis_label', label: 'axis', width: 190 },
    { key: 'task_count', label: 'tasks', width: 86, align: 'right' },
    { key: 'todo_count', label: 'todo', width: 86, align: 'right' },
    { key: 'watch_count', label: 'watch', width: 86, align: 'right' },
    { key: 'first_next_action', label: 'next action', minWidth: 320 },
    { key: 'completion_evidence', label: 'completion evidence', minWidth: 380 }
  ]

  const reviewTaskCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'task_status', label: 'task', width: 90 },
    { key: 'axis_label', label: 'axis', width: 170 },
    { key: 'object', label: 'object', minWidth: 170 },
    { key: 'problem_signal', label: 'problem', minWidth: 280 },
    { key: 'review_method', label: 'review method', minWidth: 380 },
    { key: 'completion_evidence', label: 'done evidence', minWidth: 360 },
    { key: 'review_result', label: 'result', width: 130 },
    { key: 'review_note', label: 'note', minWidth: 260 },
    {
      key: 'review',
      label: 'review',
      width: 260,
      render: (row: Row) => (
        <div className="flex gap-1.5">
          <Btn tone="info" loading={isStrategyTuningTaskSaving(row, v)} onClick={() => a.saveStrategyTuningTaskReview(row, 'validated')}>
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

  const suggestionCols: Col[] = [
    { key: 'axis_label', label: 'axis', width: 170 },
    { key: 'object', label: 'object', minWidth: 170 },
    { key: 'suggested_review_result', label: 'suggest', width: 140 },
    { key: 'replay_focus', label: 'replay focus', minWidth: 340 },
    { key: 'suggested_hidden_risk', label: 'hidden risk', minWidth: 340 },
    { key: 'evidence_required', label: 'evidence', minWidth: 340 },
    {
      key: 'apply',
      label: 'apply',
      width: 150,
      render: (row: Row) => (
        <Btn tone="info" loading={isStrategyTuningTaskSaving(row, v)} onClick={() => a.applyStrategyTuningReplaySuggestion(row)}>
          record
        </Btn>
      )
    }
  ]

  const completionAxisCols: Col[] = [
    { key: 'axis_label', label: 'axis', width: 190 },
    { key: 'axis_status', label: 'status', width: 130 },
    { key: 'todo_count', label: 'todo', width: 80, align: 'right' },
    { key: 'followup_count', label: 'followup', width: 100, align: 'right' },
    { key: 'reviewed_count', label: 'reviewed', width: 100, align: 'right' },
    { key: 'watch_count', label: 'watch', width: 80, align: 'right' },
    { key: 'completion_evidence', label: 'completion evidence', minWidth: 360 }
  ]

  const openTaskCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'axis_label', label: 'axis', width: 170 },
    { key: 'task_status', label: 'task', width: 100 },
    { key: 'object', label: 'object', minWidth: 170 },
    { key: 'problem_signal', label: 'problem', minWidth: 280 },
    { key: 'next_action', label: 'next action', minWidth: 320 }
  ]

  return (
    <Card className="mt-4">
      <AuditMain
        label="strategy tuning axis"
        strong={`${v.strategyTuningAxisSummary.risk_item_count ?? '--'} risks / ${
          v.strategyTuningAxisSummary.needs_review_count ?? '--'
        } review`}
        text={
          v.strategyTuningAxisSummary.primary_blocker ||
          v.strategyTuningAxisSummary.next_action ||
          'read-only tuning board; profit-only optimization is disabled'
        }
      />
      <AuditGrid
        items={[
          {
            label: 'buy/order lock',
            value: truthy(v.strategyTuningAxisSummary.live_admission_buy_allowed) ? 'review only' : 'locked'
          },
          {
            label: 'profit-only',
            value: truthy(v.strategyTuningAxisSummary.profit_only_optimization_allowed) ? 'allowed' : 'disabled'
          },
          {
            label: 'contract change',
            value: truthy(v.strategyTuningAxisSummary.can_change_strategy_contract) ? 'allowed' : 'locked'
          },
          { label: 'admission', value: v.strategyTuningAxisSummary.live_admission_status || '--' }
        ]}
      />
      <DataTable rows={v.strategyTuningAxisRows} cols={axisCols} empty="no tuning axis rows" />
      <DataTable rows={v.strategyTuningRiskRows} cols={riskCols} empty="no tuning risk rows" />

      <AuditMain
        label="tuning review queue"
        strong={`${v.strategyTuningReviewQueueSummary.todo_count ?? '--'} todo / ${
          v.strategyTuningReviewQueueSummary.watch_count ?? '--'
        } watch`}
        text={
          v.strategyTuningReviewQueueSummary.primary_blocker ||
          v.strategyTuningReviewQueueSummary.next_action ||
          'review queue only; no trade execution'
        }
      />
      <AuditMain
        label="tuning completion audit"
        strong={
          v.strategyTuningCompletionAudit.audit_label || v.strategyTuningCompletionAudit.audit_status || '--'
        }
        text={
          v.strategyTuningCompletionAudit.primary_gap ||
          v.strategyTuningCompletionAudit.next_action ||
          '--'
        }
      />
      <AuditGrid
        items={[
          { label: 'todo', value: v.strategyTuningCompletionSummary.todo_count ?? '--' },
          { label: 'followup', value: v.strategyTuningCompletionSummary.followup_count ?? '--' },
          { label: 'reviewed', value: v.strategyTuningCompletionSummary.reviewed_count ?? '--' },
          {
            label: 'trade lock',
            value: truthy(v.strategyTuningCompletionSummary.can_execute_trade) ? 'open' : 'locked'
          }
        ]}
      />
      <AuditMain
        label="replay session"
        strong={`${v.strategyTuningReplaySession.session_status || '--'} / ${
          v.strategyTuningReplaySessionSummary.progress_pct ?? 0
        }%`}
        text={
          v.strategyTuningReplaySession.current_step_label ||
          v.strategyTuningReplaySession.next_action ||
          'process replay steps from top to bottom'
        }
      />
      <DataTable rows={v.strategyTuningReplaySessionSteps} cols={sessionStepCols} empty="no replay session steps" />

      <AuditMain
        label="current step completion"
        strong={v.strategyTuningCurrentStepCompletionPacket.completion_status || '--'}
        text={v.strategyTuningCurrentStepCompletionPacket.after_done_check || '--'}
      />
      <AuditGrid
        items={[
          {
            label: 'broker fresh',
            value: truthy(v.strategyTuningCurrentStepBrokerEvidence.broker?.is_fresh) ? 'yes' : 'no'
          },
          { label: 'sync attempts', value: v.strategyTuningCurrentStepBrokerEvidence.sync_attempt_count ?? '--' },
          {
            label: 'prompt',
            value: truthy(v.strategyTuningCurrentStepBrokerEvidence.safe_to_prompt) ? 'ready' : 'wait'
          },
          {
            label: 'mark done',
            value: truthy(v.strategyTuningCurrentStepCompletionPacket.can_mark_step_done) ? 'ready' : 'locked'
          }
        ]}
      />

      <AuditMain
        label="post-sync acceptance"
        strong={v.brokerPostSyncAcceptance.acceptance_status || '--'}
        text={v.brokerPostSyncAcceptance.next_action || '--'}
        extra={
          <div className="mt-2">
            <Btn
              tone="primary"
              disabled={!truthy(v.brokerPostSyncAcceptance.can_record_execution_evidence)}
              loading={v.brokerPostSyncEvidenceLoading}
              onClick={() => a.recordBrokerPostSyncExecutionEvidence()}
            >
              record execution evidence
            </Btn>
          </div>
        }
      />
      <AuditGrid
        items={[
          {
            label: 'record evidence',
            value: truthy(v.brokerPostSyncAcceptance.can_record_execution_evidence) ? 'ready' : 'locked'
          },
          {
            label: 'manual live',
            value: truthy(v.brokerPostSyncAcceptance.manual_live_review_allowed) ? 'open' : 'locked'
          },
          { label: 'post broker', value: truthy(v.brokerPostSyncAcceptanceBroker.is_fresh) ? 'fresh' : 'stale' },
          { label: 'post stale', value: `${v.brokerPostSyncAcceptanceBroker.staleness_minutes ?? '--'}m` }
        ]}
      />
      {v.brokerPostSyncAcceptanceGaps.length > 0 && (
        <DataTable rows={v.brokerPostSyncAcceptanceGaps} cols={postGapCols} empty="no post-sync gaps" />
      )}

      <AuditMain
        label="live action console"
        strong={`${v.liveActionConsoleSummary.current_action_key || '--'} / ${
          v.liveActionConsoleSummary.current_action_status || '--'
        }`}
        text={v.liveActionConsoleSummary.natural_trade_boundary || '--'}
      />
      <DataTable rows={v.liveActionConsoleSteps} cols={consoleCols} empty="no live action steps" />

      {v.strategyTuningCurrentStepCompletionGaps.length > 0 && (
        <DataTable
          rows={v.strategyTuningCurrentStepCompletionGaps}
          cols={stepGapCols}
          empty="no current step gaps"
        />
      )}
      <DataTable
        rows={v.strategyTuningReviewAxisSummaryRows}
        cols={reviewAxisCols}
        empty="no tuning review axis summary"
      />
      <DataTable rows={v.strategyTuningReviewTaskRows} cols={reviewTaskCols} empty="no tuning review tasks" />

      <Card className="mt-4">
        <div className="text-sm font-semibold text-zinc-900 dark:text-white">
          replay suggestions: {v.strategyTuningReplaySuggestionSummary.suggestion_count ?? '--'}
        </div>
        <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-300">
          system-generated replay advice only; applying a suggestion records review evidence but never changes contract
          or opens trade paths
        </p>
      </Card>
      <DataTable
        rows={v.strategyTuningReplaySuggestionRows}
        cols={suggestionCols}
        empty="no tuning replay suggestions"
      />
      <DataTable
        rows={v.strategyTuningCompletionAxisRows}
        cols={completionAxisCols}
        empty="no tuning completion axis rows"
      />
      {v.strategyTuningCompletionOpenTasks.length > 0 && (
        <DataTable
          rows={v.strategyTuningCompletionOpenTasks}
          cols={openTaskCols}
          empty="no open tuning tasks"
        />
      )}
    </Card>
  )
}
