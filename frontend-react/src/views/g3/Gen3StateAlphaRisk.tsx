// G3 风控合同页（迁移自 frontend/src/views/g3/Gen3StateAlphaRisk.vue）
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useToast } from '@/components/toast'
import {
  getGen3StateAlphaContract,
  getGen3StateAlphaCurrent,
  getGen3StateAlphaRealtimeReadinessReview,
  getGen3StateAlphaPretradeTicketReviews,
  getGen3StateAlphaPaperWatchReviews,
  runGen3StateAlphaRealtimeReadinessReview,
  getGen3StateAlphaLiveLaunchPacket,
  runGen3StateAlphaLiveLaunchPacket,
  getGen3StateAlphaLiveLearningLedger,
  getGen3StateAlphaLiveLaunchDecision,
  getGen3StateAlphaLiveBlockerEvidenceBoard,
  getGen3StateAlphaLiveLaunchReadinessAudit,
  getGen3StateAlphaStrategyTuningAxisBoard,
  getGen3StateAlphaStrategyTuningReviewQueue,
  getGen3StateAlphaStrategyTuningCompletionAudit,
  getGen3StateAlphaStrategyTuningReplaySuggestions,
  getGen3StateAlphaStrategyTuningReplaySession,
  getGen3StateAlphaStrategyTuningCurrentStepCompletionPacket,
  getGen3StateAlphaLiveReplayCockpit,
  saveGen3StateAlphaStrategyTuningTaskReview,
  getGen3StateAlphaLiveLaunchReviewSnapshots,
  getGen3StateAlphaLiveLaunchReviewSnapshotDetail,
  recordGen3StateAlphaLiveLaunchReviewSnapshot,
  getGen3StateAlphaLaunchDayPlaybookReviews,
  saveGen3StateAlphaLaunchDayPlaybookReviewAndRun,
  getGen3StateAlphaHistoricalDecisionReplayTasks,
  getGen3StateAlphaHistoricalDecisionReplayAudit,
  getGen3StateAlphaPremarketControl,
  executeGen3StateAlphaPremarketNextAction,
  getGen3StateAlphaBrokerHoldingsSyncPreflight,
  getGen3StateAlphaBrokerHoldingsSyncConfirmationPacket,
  getGen3StateAlphaBrokerHoldingsSyncOutcome,
  getGen3StateAlphaBrokerPostSyncAcceptance,
  recordGen3StateAlphaBrokerPostSyncExecutionEvidence,
  getGen3StateAlphaLiveActionConsole,
  saveGen3StateAlphaLiveActionConsoleStepReview,
  syncGen3StateAlphaBrokerHoldingsFromThsAndReview,
  saveGen3StateAlphaPretradeTicketReview,
  startGen3StateAlphaPretradePaperWatchBatch,
  startGen3StateAlphaDay1PaperExecutionBatch,
  saveGen3StateAlphaPaperWatchReview,
  saveGen3StateAlphaCandidateOmissionReviewAndRun,
  saveGen3StateAlphaNoTradeDayReviewAndRun,
  saveGen3StateAlphaFormalActionReviewAndRun,
  getGen3StateAlphaDailyReviewChecklistReviews,
  saveGen3StateAlphaDailyReviewChecklistReviewAndRun
} from '@/api/trading'
import { RiskProvider, useDialogs } from './risk/shared'
import type { Row } from './risk/labels'
import {
  dailyReviewChecklistAutoText,
  dailyReviewChecklistResultLabel,
  dailyReviewChecklistRowKey,
  formalActionAutoText,
  formalActionReviewLabel,
  formalActionButtonLabel,
  formalActionRowKey,
  isCandidateOmissionReview,
  isDay1JournalNoTradeRow,
  launchDayPlaybookAutoText,
  launchDayPlaybookRowKey,
  noTradeAutoReviewText,
  noTradeReviewResultLabel,
  paperWatchAutoReviewText,
  paperWatchIssueArea,
  paperWatchResultLabel,
  noTradeIssueArea,
  reviewAxisLabel,
  reviewRowKey,
  strategyTuningReviewResultLabel,
  strategyTuningTaskRowKey,
  taskToReviewRow,
  truthy,
  pct,
  price
} from './risk/labels'
import MetricHeader from './risk/MetricHeader'
import PremarketPanel from './risk/PremarketPanel'
import LiveLaunchPanel from './risk/LiveLaunchPanel'
import ReadinessPanelA from './risk/ReadinessPanelA'
import ContractsPanel from './risk/ContractsPanel'

export default function Gen3StateAlphaRisk() {
  const toast = useToast()
  const { prompt, confirm, dialogElement } = useDialogs()

  const [payload, setPayload] = useState<Row>({})
  const [currentPayload, setCurrentPayload] = useState<Row>({})
  const [readinessReview, setReadinessReview] = useState<Row>({})
  const [premarketControl, setPremarketControl] = useState<Row>({})
  const [liveLaunchPacketPayload, setLiveLaunchPacketPayload] = useState<Row>({})
  const [liveLaunchDecisionPayload, setLiveLaunchDecisionPayload] = useState<Row>({})
  const [liveBlockerEvidenceBoardPayload, setLiveBlockerEvidenceBoardPayload] = useState<Row>({})
  const [liveLaunchReadinessAuditPayload, setLiveLaunchReadinessAuditPayload] = useState<Row>({})
  const [strategyTuningAxisBoardPayload, setStrategyTuningAxisBoardPayload] = useState<Row>({})
  const [strategyTuningReviewQueuePayload, setStrategyTuningReviewQueuePayload] = useState<Row>({})
  const [strategyTuningCompletionAuditPayload, setStrategyTuningCompletionAuditPayload] = useState<Row>({})
  const [strategyTuningReplaySuggestionsPayload, setStrategyTuningReplaySuggestionsPayload] = useState<Row>({})
  const [strategyTuningReplaySessionPayload, setStrategyTuningReplaySessionPayload] = useState<Row>({})
  const [strategyTuningCurrentStepCompletionPacketPayload, setStrategyTuningCurrentStepCompletionPacketPayload] =
    useState<Row>({})
  const [liveReplayCockpitPayload, setLiveReplayCockpitPayload] = useState<Row>({})
  const [historicalDecisionReplayPayload, setHistoricalDecisionReplayPayload] = useState<Row>({})
  const [historicalDecisionReplayAuditPayload, setHistoricalDecisionReplayAuditPayload] = useState<Row>({})
  const [brokerSyncPreflightPayload, setBrokerSyncPreflightPayload] = useState<Row>({})
  const [brokerSyncConfirmationPacketPayload, setBrokerSyncConfirmationPacketPayload] = useState<Row>({})
  const [brokerSyncOutcomePayload, setBrokerSyncOutcomePayload] = useState<Row>({})
  const [brokerPostSyncAcceptancePayload, setBrokerPostSyncAcceptancePayload] = useState<Row>({})
  const [liveActionConsolePayload, setLiveActionConsolePayload] = useState<Row>({})
  const [liveLearningLedgerPayload, setLiveLearningLedgerPayload] = useState<Row>({})
  const [pretradeReviewLedger, setPretradeReviewLedger] = useState<Row[]>([])
  const [paperWatchReviewLedger, setPaperWatchReviewLedger] = useState<Row[]>([])
  const [dailyReviewChecklistReviewLedger, setDailyReviewChecklistReviewLedger] = useState<Row[]>([])
  const [launchDayPlaybookReviewLedger, setLaunchDayPlaybookReviewLedger] = useState<Row[]>([])
  const [liveLaunchReviewSnapshotLedger, setLiveLaunchReviewSnapshotLedger] = useState<Row[]>([])
  const [liveLaunchSnapshotDetailPayload, setLiveLaunchSnapshotDetailPayload] = useState<Row>({})
  const [liveLaunchSnapshotDetailVisible, setLiveLaunchSnapshotDetailVisible] = useState(false)
  const [liveLaunchSnapshotDetailId, setLiveLaunchSnapshotDetailId] = useState('')

  const [loading, setLoading] = useState(false)
  const [reviewLoading, setReviewLoading] = useState(false)
  const [brokerSyncLoading, setBrokerSyncLoading] = useState(false)
  const [brokerPostSyncEvidenceLoading, setBrokerPostSyncEvidenceLoading] = useState(false)
  const [liveActionConsoleLoading, setLiveActionConsoleLoading] = useState(false)
  const [premarketControlLoading, setPremarketControlLoading] = useState(false)
  const [premarketNextActionLoading, setPremarketNextActionLoading] = useState(false)
  const [brokerSyncPreflightLoading, setBrokerSyncPreflightLoading] = useState(false)
  const [liveLaunchPacketLoading, setLiveLaunchPacketLoading] = useState(false)
  const [liveLaunchSnapshotLoading, setLiveLaunchSnapshotLoading] = useState(false)
  const [liveLaunchSnapshotDetailLoading, setLiveLaunchSnapshotDetailLoading] = useState(false)
  const [batchReviewLoading, setBatchReviewLoading] = useState(false)
  const [day1ExecutionLoading, setDay1ExecutionLoading] = useState(false)
  const [reviewSavingKey, setReviewSavingKey] = useState('')
  const [formalActionSavingKey, setFormalActionSavingKey] = useState('')
  const [dailyReviewChecklistSavingKey, setDailyReviewChecklistSavingKey] = useState('')
  const [launchDayPlaybookSavingKey, setLaunchDayPlaybookSavingKey] = useState('')
  const [strategyTuningTaskSavingKey, setStrategyTuningTaskSavingKey] = useState('')
  const [liveActionConsoleReviewSavingKey, setLiveActionConsoleReviewSavingKey] = useState('')

  // ---------- 派生值 ----------
  const vm = useMemo(() => {
    const contract = payload.contract || currentPayload.strategy_contract || {}
    const summary = currentPayload.summary || {}
    const portfolio = contract.portfolio_contract || contract.portfolio || {}
    const exitContract = contract.exit_contract || {}
    const tradeStrategyPolicy = Array.isArray(contract.trade_strategy_policy) ? contract.trade_strategy_policy : []
    const shadowTickets = Array.isArray(currentPayload.shadow_tickets) ? currentPayload.shadow_tickets : []
    const qualifiedTickets = shadowTickets.filter((row: Row) => truthy(row.qualified_shadow_buy))
    const artifacts = payload.artifacts || {}
    const artifactRows = Object.entries(artifacts).map(([name, item]) => ({ name, ...(item as Row) || {} }))
    const readinessSummary = readinessReview.summary || {}
    const premarketCommand = premarketControl.command || {}
    const brokerSyncPreflight = brokerSyncPreflightPayload || {}
    const brokerSyncConfirmationPacket = brokerSyncConfirmationPacketPayload || {}
    const brokerSyncConfirmationFingerprint = brokerSyncConfirmationPacket.action_fingerprint || {}
    const brokerSyncOutcome = brokerSyncOutcomePayload.outcome || {}
    const brokerSyncLatestAttempt = brokerSyncOutcomePayload.latest_sync_attempt || {}
    const brokerSyncOutcomeBroker = brokerSyncOutcomePayload.broker || {}
    const brokerPostSyncAcceptance = brokerPostSyncAcceptancePayload.acceptance || {}
    const brokerPostSyncAcceptanceGaps = Array.isArray(brokerPostSyncAcceptancePayload.gaps)
      ? brokerPostSyncAcceptancePayload.gaps
      : []
    const brokerPostSyncAcceptanceBroker = brokerPostSyncAcceptancePayload.broker || {}
    const liveActionConsoleSummary = liveActionConsolePayload.summary || {}
    const liveActionConsoleSteps = Array.isArray(liveActionConsolePayload.steps) ? liveActionConsolePayload.steps : []
    const launchPacket = liveLaunchPacketPayload.packet || {}
    const launchPacketSummary = launchPacket.summary || {}
    const liveLaunchDecision = liveLaunchDecisionPayload.decision || {}
    const liveLaunchDecisionTone = (() => {
      const tone = String(liveLaunchDecision.risk_tone || '').trim()
      if (tone === 'block') return 'block'
      if (tone === 'ready') return 'ready'
      return 'observe'
    })()
    const liveLaunchDecisionRiskRows = Array.isArray(liveLaunchDecisionPayload.top_learning_risks)
      ? liveLaunchDecisionPayload.top_learning_risks
      : []
    const liveBlockerEvidenceBoardRows = Array.isArray(liveBlockerEvidenceBoardPayload.rows)
      ? liveBlockerEvidenceBoardPayload.rows
      : []
    const liveLaunchReadinessAudit = liveLaunchReadinessAuditPayload.audit || {}
    const liveLaunchReadinessAuditSummary = liveLaunchReadinessAuditPayload.summary || {}
    const liveLaunchReadinessHardGaps = Array.isArray(liveLaunchReadinessAuditPayload.hard_gaps)
      ? liveLaunchReadinessAuditPayload.hard_gaps
      : []
    const strategyTuningAxisSummary = strategyTuningAxisBoardPayload.summary || {}
    const strategyTuningAxisRows = Array.isArray(strategyTuningAxisBoardPayload.axis_rows)
      ? strategyTuningAxisBoardPayload.axis_rows
      : []
    const strategyTuningRiskRows = Array.isArray(strategyTuningAxisBoardPayload.risk_rows)
      ? strategyTuningAxisBoardPayload.risk_rows
      : []
    const strategyTuningReviewQueueSummary = strategyTuningReviewQueuePayload.summary || {}
    const strategyTuningReviewAxisSummaryRows = Array.isArray(strategyTuningReviewQueuePayload.axis_task_summary)
      ? strategyTuningReviewQueuePayload.axis_task_summary
      : []
    const strategyTuningReviewTaskRows = Array.isArray(strategyTuningReviewQueuePayload.task_rows)
      ? strategyTuningReviewQueuePayload.task_rows
      : []
    const strategyTuningReplaySuggestionSummary = strategyTuningReplaySuggestionsPayload.summary || {}
    const strategyTuningReplaySuggestionRows = Array.isArray(strategyTuningReplaySuggestionsPayload.suggestions)
      ? strategyTuningReplaySuggestionsPayload.suggestions
      : []
    const strategyTuningReplaySession = strategyTuningReplaySessionPayload.session || {}
    const strategyTuningReplaySessionSummary = strategyTuningReplaySessionPayload.summary || {}
    const strategyTuningReplaySessionSteps = Array.isArray(strategyTuningReplaySessionPayload.session_steps)
      ? strategyTuningReplaySessionPayload.session_steps
      : []
    const strategyTuningCurrentStepCompletionPacket =
      strategyTuningCurrentStepCompletionPacketPayload.packet || {}
    const strategyTuningCurrentStepCompletionGaps = Array.isArray(
      strategyTuningCurrentStepCompletionPacket.gaps
    )
      ? strategyTuningCurrentStepCompletionPacket.gaps
      : []
    const strategyTuningCurrentStepBrokerEvidence =
      strategyTuningCurrentStepCompletionPacket.current_evidence?.broker_sync || {}
    const liveReplayCockpitSummary = liveReplayCockpitPayload.summary || {}
    const liveReplayCockpitActionItems = Array.isArray(liveReplayCockpitPayload.action_items)
      ? liveReplayCockpitPayload.action_items
      : []
    const liveReplayCockpitAxisProgress = Array.isArray(liveReplayCockpitPayload.axis_progress)
      ? liveReplayCockpitPayload.axis_progress
      : []
    const liveReplayCockpitChecks = Array.isArray(liveReplayCockpitPayload.natural_trade_checks)
      ? liveReplayCockpitPayload.natural_trade_checks
      : []
    const historicalDecisionReplaySummary = historicalDecisionReplayPayload.summary || {}
    const historicalDecisionReplayTasks = Array.isArray(historicalDecisionReplayPayload.tasks)
      ? historicalDecisionReplayPayload.tasks
      : []
    const historicalDecisionReplayAudit = historicalDecisionReplayAuditPayload.audit || {}
    const historicalDecisionReplayAuditSummary = historicalDecisionReplayAuditPayload.summary || {}
    const historicalDecisionReplayAxisRows = Array.isArray(historicalDecisionReplayAuditPayload.axis_audit_rows)
      ? historicalDecisionReplayAuditPayload.axis_audit_rows
      : []
    const historicalDecisionReplayHardGaps = Array.isArray(historicalDecisionReplayAuditPayload.hard_gaps)
      ? historicalDecisionReplayAuditPayload.hard_gaps
      : []
    const strategyTuningCompletionAudit = strategyTuningCompletionAuditPayload.audit || {}
    const strategyTuningCompletionSummary = strategyTuningCompletionAuditPayload.summary || {}
    const strategyTuningCompletionAxisRows = Array.isArray(
      strategyTuningCompletionAuditPayload.axis_completion_rows
    )
      ? strategyTuningCompletionAuditPayload.axis_completion_rows
      : []
    const strategyTuningCompletionOpenTasks = Array.isArray(strategyTuningCompletionAuditPayload.top_open_tasks)
      ? strategyTuningCompletionAuditPayload.top_open_tasks
      : []
    const arr = (obj: any, key: string, fallbackKey?: string): Row[] => {
      const primary = obj?.[key]
      if (Array.isArray(primary) && primary.length) return primary
      if (fallbackKey) {
        const fb = obj?.[fallbackKey]
        if (Array.isArray(fb)) return fb
      }
      return []
    }
    const liveLaunchStepRows = arr(liveLaunchPacketPayload, 'premarket_step_cards', 'launch_steps')
    const liveLaunchReviewAxisRows = (() => {
      const primary = liveLaunchPacketPayload.review_axis_matrix
      if (Array.isArray(primary) && primary.length) return primary
      const fb = launchPacket.review_axis_matrix
      return Array.isArray(fb) ? fb : []
    })()
    const liveLaunchLearningQueueRows = (() => {
      const primary = liveLaunchPacketPayload.launch_day_learning_queue
      if (Array.isArray(primary) && primary.length) return primary
      const fb = launchPacket.launch_day_learning_queue
      return Array.isArray(fb) ? fb : []
    })()
    const liveLaunchDayPlaybookRows = (() => {
      const primary = liveLaunchPacketPayload.launch_day_playbook
      if (Array.isArray(primary) && primary.length) return primary
      const fb = launchPacket.launch_day_playbook
      return Array.isArray(fb) ? fb : []
    })()
    const liveLaunchReviewSnapshotRows = [...liveLaunchReviewSnapshotLedger].sort((a, b) =>
      String(b?.generated_at || '').localeCompare(String(a?.generated_at || ''))
    )
    const liveLearningLedgerRows = Array.isArray(liveLearningLedgerPayload.learning_ledger)
      ? liveLearningLedgerPayload.learning_ledger
      : []
    const liveLaunchSnapshotDetail =
      liveLaunchSnapshotDetailPayload.detail || liveLaunchSnapshotDetailPayload.snapshot || null
    const liveLaunchSnapshotPremarketRows = (() => {
      const command = liveLaunchSnapshotDetail?.premarket_command || {}
      const action = command.current_action || {}
      return Object.keys(action).length ? [action] : []
    })()
    const liveLaunchSnapshotPlaybookRows = Array.isArray(liveLaunchSnapshotDetail?.launch_day_playbook)
      ? liveLaunchSnapshotDetail.launch_day_playbook
      : []
    const liveLaunchSnapshotLearningRows = Array.isArray(liveLaunchSnapshotDetail?.launch_day_learning_queue)
      ? liveLaunchSnapshotDetail.launch_day_learning_queue
      : []
    const liveLaunchSnapshotLearningLedgerRows = Array.isArray(liveLaunchSnapshotDetail?.live_learning_ledger)
      ? liveLaunchSnapshotDetail.live_learning_ledger
      : []
    const launchPacketStatusType = (() => {
      const status = String(launchPacket.status || '').trim()
      if (status === 'manual_review_ready') return 'success'
      if (status === 'blocked') return 'danger'
      return 'warning'
    })()
    const listOf = (key: string): Row[] => (Array.isArray(readinessReview[key]) ? readinessReview[key] : [])
    const reviewTicketRows = listOf('next_trade_ticket_review')
    const unreviewedTicketRows = reviewTicketRows.filter(
      (row: Row) => !String(row?.pretrade_review_action || '').trim()
    )
    const pretradeReviewLedgerRows = [...pretradeReviewLedger].sort((a, b) =>
      String(b?.updated_at || '').localeCompare(String(a?.updated_at || ''))
    )
    const paperWatchFollowupRows = (() => {
      const direct = readinessReview.paper_watch_followup
      if (Array.isArray(direct) && direct.length) return direct
      const reviewMap = new Map(
        paperWatchReviewLedger.map((item) => [String(item?.key || item?.ticket_key || ''), item])
      )
      return pretradeReviewLedgerRows
        .filter((row) => row?.review_action === 'paper_watch')
        .map((row, index) => {
          const key = reviewRowKey(row)
          const review = reviewMap.get(key) || {}
          return {
            rank: index + 1,
            ...row,
            ...review,
            ticket_key: row.ticket_key || review.ticket_key || key,
            watch_result_label: review.watch_result_label || '待观察',
            followup_status: review.watch_result
              ? review.watch_result === 'as_expected'
                ? 'validated'
                : review.watch_result === 'continue_watch'
                  ? 'continue_watch'
                  : 'issue_found'
              : 'pending_observation'
          }
        })
    })()
    const strategyPositionSummary = '主升/强突/震荡/G2 50%；恐慌出清 25%-50%'
    const portfolioRows = [
      { label: '最大槽位', value: portfolio.max_slots ?? portfolio.slots ?? '--' },
      { label: '单槽仓位', value: pct(portfolio.per_slot_position_pct || portfolio.slot_pct) },
      { label: '最大总仓', value: pct(portfolio.max_total_position_pct) },
      { label: '正式策略数', value: portfolio.trade_strategy_count ?? (tradeStrategyPolicy.length || '--') },
      { label: '策略框架', value: portfolio.trade_strategy_framework || '--' },
      {
        label: '账户风险闸门',
        value: contract.guardrails?.account_risk_gate || contract.account_risk_gate || '--'
      },
      { label: '五策略仓位', value: strategyPositionSummary }
    ]
    const exitRows = [
      { label: '硬止损', value: pct(exitContract.hard_stop_pct) },
      { label: '第一止盈', value: pct(exitContract.first_take_profit_pct) },
      {
        label: '前低保护',
        value: exitContract.previous_low_protection || exitContract.low_protection || '--'
      },
      {
        label: '结构止损',
        value: exitContract.structure_stop || exitContract.structure_stop_policy || '--'
      },
      {
        label: '回撤减仓',
        value: pct(exitContract.risk_limits?.mtm_drawdown_reduce_risk_pct || exitContract.mtm_drawdown_reduce_risk_pct)
      },
      {
        label: '合同说明',
        value: exitContract.description || contract.default_exit_contract || '--'
      }
    ]
    const guardrailRows = [
      {
        label: '正式买点',
        ok: currentPayload.formal_buy_signal === false,
        detail: `formal_buy_signal=${currentPayload.formal_buy_signal}`
      },
      {
        label: '自动下单',
        ok: currentPayload.auto_order_allowed === false,
        detail: `auto_order_allowed=${currentPayload.auto_order_allowed}`
      },
      {
        label: '订单路径',
        ok: currentPayload.order_path_enabled === false,
        detail: `order_path_enabled=${currentPayload.order_path_enabled}`
      },
      { label: '影子模式', ok: currentPayload.shadow_only === true, detail: `shadow_only=${currentPayload.shadow_only}` },
      {
        label: '观察模式',
        ok: currentPayload.observe_only === true,
        detail: `observe_only=${currentPayload.observe_only}`
      }
    ]
    const firstLiveDecisionCardRows = listOf('first_live_decision_card')
    const firstLiveDecisionCard = firstLiveDecisionCardRows[0] || null
    const liveAdmissionSnapshotCardRows = listOf('live_admission_snapshot')
    const liveAdmissionSnapshotCard = liveAdmissionSnapshotCardRows[0] || null
    const toneOf = (card: Row | null) => {
      const tone = String(card?.risk_tone || '').trim()
      if (['block', 'ready', 'watch', 'observe'].includes(tone)) return tone
      return 'watch'
    }
    const firstLiveDecisionTone = toneOf(firstLiveDecisionCard)
    const liveAdmissionTone = (() => {
      const tone = String(liveAdmissionSnapshotCard?.risk_tone || '').trim()
      if (['block', 'ready', 'watch', 'observe'].includes(tone)) return tone
      if (truthy(liveAdmissionSnapshotCard?.live_buy_allowed)) return 'ready'
      return 'watch'
    })()

    return {
      payload,
      contract,
      summary,
      portfolio,
      exitContract,
      tradeStrategyPolicy,
      shadowTickets,
      qualifiedTickets,
      artifactRows,
      readinessSummary,
      premarketCommand,
      brokerSyncPreflight,
      brokerSyncConfirmationPacket,
      brokerSyncConfirmationFingerprint,
      brokerSyncOutcome,
      brokerSyncLatestAttempt,
      brokerSyncOutcomeBroker,
      brokerSyncOutcomePayload,
      brokerPostSyncAcceptance,
      brokerPostSyncAcceptanceGaps,
      brokerPostSyncAcceptanceBroker,
      liveActionConsoleSummary,
      liveActionConsoleSteps,
      launchPacket,
      launchPacketSummary,
      liveLaunchDecision,
      liveLaunchDecisionTone,
      liveLaunchDecisionRiskRows,
      liveBlockerEvidenceBoardRows,
      liveLaunchReadinessAudit,
      liveLaunchReadinessAuditSummary,
      liveLaunchReadinessHardGaps,
      strategyTuningAxisSummary,
      strategyTuningAxisRows,
      strategyTuningRiskRows,
      strategyTuningReviewQueueSummary,
      strategyTuningReviewAxisSummaryRows,
      strategyTuningReviewTaskRows,
      strategyTuningReplaySuggestionSummary,
      strategyTuningReplaySuggestionRows,
      strategyTuningReplaySession,
      strategyTuningReplaySessionSummary,
      strategyTuningReplaySessionSteps,
      strategyTuningCurrentStepCompletionPacket,
      strategyTuningCurrentStepCompletionGaps,
      strategyTuningCurrentStepBrokerEvidence,
      strategyTuningCompletionAudit,
      strategyTuningCompletionSummary,
      strategyTuningCompletionAxisRows,
      strategyTuningCompletionOpenTasks,
      liveReplayCockpitSummary,
      liveReplayCockpitActionItems,
      liveReplayCockpitAxisProgress,
      liveReplayCockpitChecks,
      historicalDecisionReplaySummary,
      historicalDecisionReplayTasks,
      historicalDecisionReplayAudit,
      historicalDecisionReplayAuditSummary,
      historicalDecisionReplayAxisRows,
      historicalDecisionReplayHardGaps,
      liveLaunchStepRows,
      liveLaunchReviewAxisRows,
      liveLaunchLearningQueueRows,
      liveLaunchDayPlaybookRows,
      liveLaunchReviewSnapshotRows,
      liveLearningLedgerRows,
      liveLaunchSnapshotDetail,
      liveLaunchSnapshotPremarketRows,
      liveLaunchSnapshotPlaybookRows,
      liveLaunchSnapshotLearningRows,
      liveLaunchSnapshotLearningLedgerRows,
      launchPacketStatusType,
      readinessGates: listOf('readiness_gates'),
      pretradeActionRows: listOf('pretrade_action_checklist'),
      formalLaunchRows: listOf('formal_launch_checklist'),
      executionModeRows: listOf('execution_mode_matrix'),
      formalLaunchActionRows: listOf('formal_launch_action_queue'),
      premarketPlaybookRows: listOf('premarket_execution_playbook'),
      liveReviewTaskQueueRows: listOf('live_review_task_queue'),
      livePremarketCommandRows: listOf('live_premarket_command_sheet'),
      livePremarketActionSequenceRows: listOf('live_premarket_action_sequence'),
      livePremarketExecutionRecheckRows: listOf('live_premarket_execution_recheck'),
      livePremarketActionAttemptRows: listOf('live_premarket_action_attempts'),
      liveManualLaunchAcceptanceRows: listOf('live_manual_launch_acceptance'),
      liveDay1ReviewJournalRows: listOf('live_day1_review_journal'),
      liveBlockerResolutionRows: listOf('live_blocker_resolution_plan'),
      liveBlockerEvidenceRows: listOf('live_blocker_evidence_ledger'),
      reviewCoverageDashboardRows: listOf('review_coverage_dashboard'),
      liveReviewEvidenceRubricRows: listOf('live_review_evidence_rubric'),
      dailyLiveReviewBoardRows: listOf('daily_live_review_board'),
      strategyLearningBacklogRows: listOf('strategy_learning_backlog'),
      liveHiddenRiskWatchlistRows: listOf('live_hidden_risk_watchlist'),
      liveDailyReviewActionLayerRows: listOf('live_daily_review_action_layers'),
      liveDailyReviewExecutionChecklistRows: listOf('live_daily_review_execution_checklist'),
      noTradeDayReviewRows: listOf('no_trade_day_review'),
      day1PaperReviewRows: listOf('day1_paper_review_pack'),
      day1AfterCloseReviewRows: listOf('day1_after_close_review_queue'),
      pretradeEvidenceRows: listOf('pretrade_review_evidence'),
      naturalConsistencyRows: listOf('natural_trade_consistency_review'),
      naturalExecutionRows: listOf('natural_execution_decision_matrix'),
      reviewTicketRows,
      unreviewedTicketRows,
      pretradeReviewLedgerRows,
      paperWatchFollowupRows,
      ticketChecklistRows: listOf('ticket_review_checklist'),
      holdingRefreshRows: listOf('holding_refresh_evidence'),
      holdingChecklistRows: listOf('holding_exit_checklist'),
      candidateChecklistRows: listOf('candidate_omission_checklist'),
      hazardRows: listOf('hazard_register'),
      portfolioRows,
      exitRows,
      guardrailRows,
      firstLiveDecisionCard,
      liveAdmissionSnapshotCard,
      firstLiveDecisionTone,
      liveAdmissionTone
    }
  }, [
    payload,
    currentPayload,
    readinessReview,
    premarketControl,
    liveLaunchPacketPayload,
    liveLaunchDecisionPayload,
    liveBlockerEvidenceBoardPayload,
    liveLaunchReadinessAuditPayload,
    strategyTuningAxisBoardPayload,
    strategyTuningReviewQueuePayload,
    strategyTuningCompletionAuditPayload,
    strategyTuningReplaySuggestionsPayload,
    strategyTuningReplaySessionPayload,
    strategyTuningCurrentStepCompletionPacketPayload,
    liveReplayCockpitPayload,
    historicalDecisionReplayPayload,
    historicalDecisionReplayAuditPayload,
    brokerSyncPreflightPayload,
    brokerSyncConfirmationPacketPayload,
    brokerSyncOutcomePayload,
    brokerPostSyncAcceptancePayload,
    liveActionConsolePayload,
    liveLearningLedgerPayload,
    pretradeReviewLedger,
    paperWatchReviewLedger,
    liveLaunchReviewSnapshotLedger,
    liveLaunchSnapshotDetailPayload
  ])

  // ---------- 批量刷新辅助 ----------
  const fetchBundle = async (opts: {
    withCurrent?: boolean
    currentReason?: string
    runReadiness?: boolean
    readinessReason?: string
    runLaunchSource?: string
  }) => {
    const withCurrent = opts.withCurrent !== false
    const results = await Promise.all([
      withCurrent
        ? getGen3StateAlphaCurrent({ limit: 100, ...(opts.currentReason ? { reason: opts.currentReason } : {}) })
        : Promise.resolve(null),
      opts.runReadiness
        ? runGen3StateAlphaRealtimeReadinessReview(
            opts.readinessReason ? { reason: opts.readinessReason } : {}
          )
        : getGen3StateAlphaRealtimeReadinessReview({}),
      getGen3StateAlphaPremarketControl({ refresh: false }),
      getGen3StateAlphaBrokerHoldingsSyncPreflight({}),
      getGen3StateAlphaBrokerHoldingsSyncConfirmationPacket({}),
      getGen3StateAlphaBrokerHoldingsSyncOutcome({}),
      getGen3StateAlphaBrokerPostSyncAcceptance({}),
      getGen3StateAlphaLiveActionConsole({}),
      opts.runLaunchSource
        ? runGen3StateAlphaLiveLaunchPacket({ source: opts.runLaunchSource })
        : getGen3StateAlphaLiveLaunchPacket({}),
      getGen3StateAlphaLiveLearningLedger({}),
      getGen3StateAlphaLiveLaunchDecision({}),
      getGen3StateAlphaLiveBlockerEvidenceBoard({}),
      getGen3StateAlphaLiveLaunchReadinessAudit({}),
      getGen3StateAlphaStrategyTuningAxisBoard({}),
      getGen3StateAlphaStrategyTuningReviewQueue({}),
      getGen3StateAlphaStrategyTuningReplaySuggestions({}),
      getGen3StateAlphaStrategyTuningReplaySession({}),
      getGen3StateAlphaStrategyTuningCurrentStepCompletionPacket({}),
      getGen3StateAlphaStrategyTuningCompletionAudit({}),
      getGen3StateAlphaLiveReplayCockpit({}),
      getGen3StateAlphaHistoricalDecisionReplayAudit({}),
      getGen3StateAlphaHistoricalDecisionReplayTasks({ limit: 80 }),
      getGen3StateAlphaPretradeTicketReviews({}),
      getGen3StateAlphaPaperWatchReviews({}),
      getGen3StateAlphaDailyReviewChecklistReviews({}),
      getGen3StateAlphaLaunchDayPlaybookReviews({}),
      getGen3StateAlphaLiveLaunchReviewSnapshots({ limit: 100 })
    ])
    return {
      current: results[0] as Row,
      review: results[1] as Row,
      control: results[2] as Row,
      preflight: results[3] as Row,
      confirmationPacket: results[4] as Row,
      outcome: results[5] as Row,
      postSync: results[6] as Row,
      console: results[7] as Row,
      launchPacket: results[8] as Row,
      learningLedger: results[9] as Row,
      launchDecision: results[10] as Row,
      blockerBoard: results[11] as Row,
      readinessAudit: results[12] as Row,
      axisBoard: results[13] as Row,
      reviewQueue: results[14] as Row,
      suggestions: results[15] as Row,
      session: results[16] as Row,
      completionPacket: results[17] as Row,
      completionAudit: results[18] as Row,
      cockpit: results[19] as Row,
      histAudit: results[20] as Row,
      histTasks: results[21] as Row,
      ledger: results[22] as Row,
      paperLedger: results[23] as Row,
      dailyLedger: results[24] as Row,
      playbookLedger: results[25] as Row,
      snapshots: results[26] as Row
    }
  }

  const applyBundle = (b: any, keep: boolean) => {
    const pick = (nv: any, cur: Row) => (keep ? nv || cur || {} : nv || {})
    if (b.current) setCurrentPayload((prev) => pick(b.current, prev))
    setReadinessReview((prev) => pick(b.review, prev))
    setPremarketControl((prev) => pick(b.control, prev))
    setBrokerSyncPreflightPayload((prev) => pick(b.preflight, prev))
    setBrokerSyncConfirmationPacketPayload((prev) => pick(b.confirmationPacket, prev))
    setBrokerSyncOutcomePayload((prev) => pick(b.outcome, prev))
    setBrokerPostSyncAcceptancePayload((prev) => pick(b.postSync, prev))
    setLiveActionConsolePayload((prev) => pick(b.console, prev))
    setLiveLaunchPacketPayload((prev) => pick(b.launchPacket, prev))
    setLiveLearningLedgerPayload((prev) => pick(b.learningLedger, prev))
    setLiveLaunchDecisionPayload((prev) => pick(b.launchDecision, prev))
    setLiveBlockerEvidenceBoardPayload((prev) => pick(b.blockerBoard, prev))
    setLiveLaunchReadinessAuditPayload((prev) => pick(b.readinessAudit, prev))
    setStrategyTuningAxisBoardPayload((prev) => pick(b.axisBoard, prev))
    setStrategyTuningReviewQueuePayload((prev) => pick(b.reviewQueue, prev))
    setStrategyTuningReplaySuggestionsPayload((prev) => pick(b.suggestions, prev))
    setStrategyTuningReplaySessionPayload((prev) => pick(b.session, prev))
    setStrategyTuningCurrentStepCompletionPacketPayload((prev) => pick(b.completionPacket, prev))
    setStrategyTuningCompletionAuditPayload((prev) => pick(b.completionAudit, prev))
    setLiveReplayCockpitPayload((prev) => pick(b.cockpit, prev))
    setHistoricalDecisionReplayAuditPayload((prev) => pick(b.histAudit, prev))
    setHistoricalDecisionReplayPayload((prev) => pick(b.histTasks, prev))
    setPretradeReviewLedger(Array.isArray(b.ledger?.reviews) ? b.ledger.reviews : [])
    setPaperWatchReviewLedger(Array.isArray(b.paperLedger?.reviews) ? b.paperLedger.reviews : [])
    setDailyReviewChecklistReviewLedger(Array.isArray(b.dailyLedger?.reviews) ? b.dailyLedger.reviews : [])
    setLaunchDayPlaybookReviewLedger(Array.isArray(b.playbookLedger?.reviews) ? b.playbookLedger.reviews : [])
    setLiveLaunchReviewSnapshotLedger(Array.isArray(b.snapshots?.snapshots) ? b.snapshots.snapshots : [])
  }

  // ---------- 动作 ----------
  const load = useCallback(async () => {
    setLoading(true)
    try {
      const contractPayload = (await getGen3StateAlphaContract({})) as Row
      const b = await fetchBundle({ withCurrent: true })
      setPayload(contractPayload || {})
      applyBundle(b, false)
    } catch (error: any) {
      toast.error(error?.message || '读取 G3 风控合同失败')
    } finally {
      setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const refreshReadinessAndLedger = async (reason = '') => {
    const review = (await runGen3StateAlphaRealtimeReadinessReview(reason ? { reason } : {})) as Row
    const b = await fetchBundle({ withCurrent: false, runReadiness: true, runLaunchSource: 'g3_risk_page_after_readiness' })
    b.review = review
    applyBundle(b, false)
    return review
  }

  const refreshCurrentAndLedgers = async (reason = '') => {
    const b = await fetchBundle({ withCurrent: true, currentReason: reason })
    applyBundle(b, true)
    return b.review
  }

  const refreshLiveLaunchPacket = async (rebuild = false) => {
    setLiveLaunchPacketLoading(true)
    try {
      const result = rebuild
        ? ((await runGen3StateAlphaLiveLaunchPacket({ source: 'g3_risk_page_manual_rebuild' })) as Row)
        : ((await getGen3StateAlphaLiveLaunchPacket({})) as Row)
      setLiveLaunchPacketPayload(result || {})
      setLiveLearningLedgerPayload(((await getGen3StateAlphaLiveLearningLedger({})) as Row) || {})
      setLiveLaunchDecisionPayload(((await getGen3StateAlphaLiveLaunchDecision({})) as Row) || {})
      setLiveBlockerEvidenceBoardPayload(((await getGen3StateAlphaLiveBlockerEvidenceBoard({})) as Row) || {})
      setLiveLaunchReadinessAuditPayload(((await getGen3StateAlphaLiveLaunchReadinessAudit({})) as Row) || {})
      toast.success(rebuild ? '已重建 G3 实战启动包' : '已读取 G3 实战启动包')
      return result
    } catch (error: any) {
      toast.error(error?.message || '读取 G3 实战启动包失败')
      return null
    } finally {
      setLiveLaunchPacketLoading(false)
    }
  }

  const upsertLiveLaunchSnapshot = (snapshot: Row = {}) => {
    const key = String(snapshot?.snapshot_id || '')
    if (!key) return
    setLiveLaunchReviewSnapshotLedger((prev) => [
      snapshot,
      ...prev.filter((item) => String(item?.snapshot_id || '') !== key)
    ])
  }

  const recordLiveLaunchReviewSnapshot = async () => {
    const note = await prompt({
      title: '记录启动快照',
      message: '记录当前 G3 实战启动复盘快照。请写一句本次快照的判断或处理节点。',
      placeholder: '例如：Day0 盘前，仍被真实持仓价格刷新阻断；不买入，等待同步后重跑审计。',
      minLength: 4,
      confirmText: '记录快照'
    })
    if (note === null) return
    setLiveLaunchSnapshotLoading(true)
    try {
      const result = (await recordGen3StateAlphaLiveLaunchReviewSnapshot({
        source: 'g3_risk_page_manual_snapshot',
        phase: 'manual_checkpoint',
        note,
        refresh: false
      })) as Row
      if (!result?.ok) {
        toast.error(result?.error || '启动复盘快照记录失败')
        return
      }
      if (result.snapshot) upsertLiveLaunchSnapshot(result.snapshot)
      toast.success('已记录启动复盘快照')
    } catch (error: any) {
      toast.error(error?.message || '启动复盘快照记录失败')
    } finally {
      setLiveLaunchSnapshotLoading(false)
    }
  }

  const openLiveLaunchSnapshotDetail = async (row: Row = {}) => {
    const snapshotId = String(row?.snapshot_id || '').trim()
    if (!snapshotId) {
      toast.info('缺少快照编号')
      return
    }
    setLiveLaunchSnapshotDetailId(snapshotId)
    setLiveLaunchSnapshotDetailLoading(true)
    try {
      const result = (await getGen3StateAlphaLiveLaunchReviewSnapshotDetail(snapshotId)) as Row
      if (!result?.ok) {
        toast.error(result?.error || '读取启动复盘快照详情失败')
        return
      }
      setLiveLaunchSnapshotDetailPayload(result || {})
      setLiveLaunchSnapshotDetailVisible(true)
    } catch (error: any) {
      toast.error(error?.message || '读取启动复盘快照详情失败')
    } finally {
      setLiveLaunchSnapshotDetailLoading(false)
    }
  }

  const closeLiveLaunchSnapshotDetail = () => setLiveLaunchSnapshotDetailVisible(false)

  const syncBrokerHoldingsAndReview = async () => {
    const ok = await confirm({
      title: '同步真实持仓',
      message: '确认手动同步同花顺真实持仓并重跑 G3 实战前审计？这会刷新真实账户持仓/价格证据，但不会触发下单。',
      confirmText: '同步并复审'
    })
    if (!ok) return
    setBrokerSyncLoading(true)
    try {
      const syncResult = (await syncGen3StateAlphaBrokerHoldingsFromThsAndReview({
        source: 'g3_pretrade_readiness_review'
      })) as Row
      if (syncResult?.live_launch_review_snapshot) {
        upsertLiveLaunchSnapshot(syncResult.live_launch_review_snapshot)
      }
      if (!syncResult?.ok) {
        toast.error(
          syncResult?.message || syncResult?.sync?.message || syncResult?.error || '同步同花顺真实持仓并复审失败'
        )
        await refreshCurrentAndLedgers('broker_sync_attempted')
        return
      }
      const b = await fetchBundle({ withCurrent: true, runLaunchSource: 'g3_risk_page_after_broker_sync' })
      applyBundle(b, true)
      const admission = syncResult?.admission || {}
      toast.success(
        `已同步真实持仓并复审：阻断数 ${
          admission.live_admission_blocking_command_count ?? '--'
        }，下一步 ${admission.live_premarket_next_action || '--'}`
      )
    } catch (error: any) {
      toast.error(error?.message || '同步真实持仓并复审失败')
    } finally {
      setBrokerSyncLoading(false)
    }
  }

  const refreshPremarketControl = async (refresh = false) => {
    setPremarketControlLoading(true)
    try {
      const control = (await getGen3StateAlphaPremarketControl({ refresh })) as Row
      setPremarketControl(control || {})
      setLiveLaunchDecisionPayload(((await getGen3StateAlphaLiveLaunchDecision({})) as Row) || {})
      setLiveBlockerEvidenceBoardPayload(((await getGen3StateAlphaLiveBlockerEvidenceBoard({})) as Row) || {})
      setLiveLaunchReadinessAuditPayload(((await getGen3StateAlphaLiveLaunchReadinessAudit({})) as Row) || {})
      setStrategyTuningAxisBoardPayload(((await getGen3StateAlphaStrategyTuningAxisBoard({})) as Row) || {})
      setStrategyTuningReviewQueuePayload(((await getGen3StateAlphaStrategyTuningReviewQueue({})) as Row) || {})
      setStrategyTuningReplaySuggestionsPayload(
        ((await getGen3StateAlphaStrategyTuningReplaySuggestions({})) as Row) || {}
      )
      setStrategyTuningReplaySessionPayload(((await getGen3StateAlphaStrategyTuningReplaySession({})) as Row) || {})
      setStrategyTuningCurrentStepCompletionPacketPayload(
        ((await getGen3StateAlphaStrategyTuningCurrentStepCompletionPacket({})) as Row) || {}
      )
      setStrategyTuningCompletionAuditPayload(((await getGen3StateAlphaStrategyTuningCompletionAudit({})) as Row) || {})
      setLiveReplayCockpitPayload(((await getGen3StateAlphaLiveReplayCockpit({})) as Row) || {})
      setBrokerSyncPreflightPayload(((await getGen3StateAlphaBrokerHoldingsSyncPreflight({})) as Row) || {})
      setBrokerSyncConfirmationPacketPayload(
        ((await getGen3StateAlphaBrokerHoldingsSyncConfirmationPacket({})) as Row) || {}
      )
      setBrokerSyncOutcomePayload(((await getGen3StateAlphaBrokerHoldingsSyncOutcome({})) as Row) || {})
      setBrokerPostSyncAcceptancePayload(((await getGen3StateAlphaBrokerPostSyncAcceptance({})) as Row) || {})
      setLiveActionConsolePayload(((await getGen3StateAlphaLiveActionConsole({})) as Row) || {})
      if (refresh) {
        if (control?.summary) {
          setReadinessReview((prev) => ({ ...(prev || {}), summary: control.summary }))
        }
        toast.success('已刷新盘前指挥卡')
      }
      return control
    } catch (error: any) {
      toast.error(error?.message || '读取盘前指挥卡失败')
      return null
    } finally {
      setPremarketControlLoading(false)
    }
  }

  const refreshBrokerSyncPreflight = async () => {
    setBrokerSyncPreflightLoading(true)
    try {
      const result = (await getGen3StateAlphaBrokerHoldingsSyncPreflight({})) as Row
      setBrokerSyncPreflightPayload(result || {})
      setBrokerSyncConfirmationPacketPayload(
        ((await getGen3StateAlphaBrokerHoldingsSyncConfirmationPacket({})) as Row) || {}
      )
      setBrokerSyncOutcomePayload(((await getGen3StateAlphaBrokerHoldingsSyncOutcome({})) as Row) || {})
      setBrokerPostSyncAcceptancePayload(((await getGen3StateAlphaBrokerPostSyncAcceptance({})) as Row) || {})
      setLiveActionConsolePayload(((await getGen3StateAlphaLiveActionConsole({})) as Row) || {})
      setLiveLaunchReadinessAuditPayload(((await getGen3StateAlphaLiveLaunchReadinessAudit({})) as Row) || {})
      setStrategyTuningAxisBoardPayload(((await getGen3StateAlphaStrategyTuningAxisBoard({})) as Row) || {})
      setStrategyTuningReviewQueuePayload(((await getGen3StateAlphaStrategyTuningReviewQueue({})) as Row) || {})
      setStrategyTuningReplaySuggestionsPayload(
        ((await getGen3StateAlphaStrategyTuningReplaySuggestions({})) as Row) || {}
      )
      setStrategyTuningReplaySessionPayload(((await getGen3StateAlphaStrategyTuningReplaySession({})) as Row) || {})
      setStrategyTuningCurrentStepCompletionPacketPayload(
        ((await getGen3StateAlphaStrategyTuningCurrentStepCompletionPacket({})) as Row) || {}
      )
      setStrategyTuningCompletionAuditPayload(((await getGen3StateAlphaStrategyTuningCompletionAudit({})) as Row) || {})
      setLiveReplayCockpitPayload(((await getGen3StateAlphaLiveReplayCockpit({})) as Row) || {})
      toast.success('已刷新同步预检')
      return result
    } catch (error: any) {
      toast.error(error?.message || '同步预检失败')
      return null
    } finally {
      setBrokerSyncPreflightLoading(false)
    }
  }

  const executePremarketNextAction = async () => {
    const command = vm.premarketCommand || {}
    if (!command.action_group) {
      toast.info('当前没有可执行的盘前动作')
      return
    }
    const ok = await confirm({
      title: '执行盘前下一步',
      message: `确认执行当前盘前动作：${
        command.action_label || command.next_action || command.action_group
      }？系统会再次校验当前动作，且不会开启自动下单。`,
      confirmText: command.requires_confirmation ? '确认执行' : '执行'
    })
    if (!ok) return
    setPremarketNextActionLoading(true)
    try {
      const result = (await executeGen3StateAlphaPremarketNextAction({
        source: 'g3_state_alpha_risk_page',
        action_group: command.action_group,
        confirm: Boolean(command.requires_confirmation)
      })) as Row
      if (result?.live_launch_review_snapshot) {
        upsertLiveLaunchSnapshot(result.live_launch_review_snapshot)
      }
      if (!result?.ok) {
        toast.error(result?.message || result?.error || '盘前动作未完成')
        await refreshCurrentAndLedgers('premarket_next_action_attempted')
        return
      }
      if (result.review) setReadinessReview(result.review)
      if (result.command) setPremarketControl((prev) => ({ ...(prev || {}), command: result.command }))
      await refreshCurrentAndLedgers('premarket_next_action_executed')
      const admission = result.admission || {}
      toast.success(
        `盘前动作已复审：阻断数 ${
          admission.live_admission_blocking_command_count ?? '--'
        }，下一步 ${admission.live_premarket_next_action || result.command?.next_action || '--'}`
      )
    } catch (error: any) {
      toast.error(error?.message || '执行盘前下一步失败')
    } finally {
      setPremarketNextActionLoading(false)
    }
  }

  const runReadinessReview = async () => {
    setReviewLoading(true)
    try {
      const review = await refreshReadinessAndLedger()
      if (review?.ok) toast.success('已重跑 G3 实战前逐票审计')
      else toast.error('审计已返回，但存在运行异常，请查看报告')
    } catch (error: any) {
      toast.error(error?.message || '重跑 G3 实战前审计失败')
    } finally {
      setReviewLoading(false)
    }
  }

  const handleFormalReviewBoardAction = async (row: Row) => {
    const key = formalActionRowKey(row)
    const label = formalActionButtonLabel(row)
    setFormalActionSavingKey(key)
    try {
      if (label === '同步持仓并复审') {
        await syncBrokerHoldingsAndReview()
        return
      }
      if (label === '重跑审计') {
        await runReadinessReview()
        return
      }
      toast.info('请在对应来源表格中处理该正式动作')
    } finally {
      setFormalActionSavingKey('')
    }
  }

  const handleBlockerResolutionAction = async (row: Row) => {
    const type = String(row?.resolution_type || '')
    if (type === 'broker_holding_price_refresh') {
      await syncBrokerHoldingsAndReview()
      return
    }
    if (type === 'rerun_readiness_audit') {
      await runReadinessReview()
      return
    }
    if (type === 'manual_formal_action_review') {
      await saveFormalActionReview(taskToReviewRow(row), 'manual_done')
      return
    }
    toast.info('该阻断需要在对应复盘入口补充证据')
  }

  const handlePremarketSequenceAction = async (row: Row) => {
    const group = String(row?.action_group || '')
    if (group === 'sync_broker_holding_price') {
      await syncBrokerHoldingsAndReview()
      return
    }
    if (group === 'rerun_readiness_audit') {
      await runReadinessReview()
      return
    }
    if (group === 'manual_formal_action_review') {
      await saveFormalActionReview(taskToReviewRow(row), 'manual_done')
      return
    }
    toast.info('请按顺序完成前置事实后再记录该步骤')
  }

  const handleDay1JournalPrimaryAction = async (row: Row) => {
    const actionText = `${row?.action || ''} ${row?.next_action || ''} ${row?.object || ''}`
    if (actionText.includes('同步') || actionText.includes('持仓') || actionText.includes('价格')) {
      await syncBrokerHoldingsAndReview()
      return
    }
    if (actionText.includes('重跑') || actionText.includes('审计')) {
      await runReadinessReview()
      return
    }
    if (isDay1JournalNoTradeRow(row)) {
      await saveNoTradeDayReview(taskToReviewRow(row), 'natural_no_trade')
      return
    }
    if (row?.source_table === 'live_manual_launch_acceptance') {
      toast.info('验收项只显示准入状态；请先处理对应盘前阻断或逐项复盘证据。')
      return
    }
    toast.info('请在同一行的证据处理按钮中记录复盘结论。')
  }

  const saveFormalActionReview = async (row: Row, result: string) => {
    const key = formalActionRowKey(row)
    const label = formalActionReviewLabel(result)
    const note = await prompt({
      title: '正式动作复核',
      message: `记录正式动作：${row?.object || '--'}，结果为“${label}”。请写一句处理证据或卡住原因。`,
      placeholder: '例如：已手动刷新真实持仓并重跑审计，或：同花顺窗口不可用，暂不能确认退出价。',
      minLength: 4,
      confirmText: '保存复核'
    })
    if (note === null) return
    const autoText = formalActionAutoText(result, row)
    setFormalActionSavingKey(key)
    try {
      const saved = (await saveGen3StateAlphaFormalActionReviewAndRun({
        review_key: row?.review_key || key,
        object: row?.object,
        action: row?.required_action,
        source: row?.source,
        review_result: result,
        issue_area: autoText.issueArea,
        evidence: row?.evidence,
        natural_decision: autoText.naturalDecision,
        optimization_suggestion: autoText.suggestion,
        review_note: note
      })) as Row
      if (!saved?.ok) {
        toast.error(saved?.error || '正式动作复核保存失败')
        return
      }
      if (saved.review) setReadinessReview(saved.review)
      toast.success('已保存正式动作复核并完成复审')
      await refreshCurrentAndLedgers('formal_action_review_saved')
    } catch (error: any) {
      toast.error(error?.message || '正式动作复核保存失败')
    } finally {
      setFormalActionSavingKey('')
    }
  }

  const refreshTuningBoards = async (withHistorical: boolean) => {
    const tasks = [
      getGen3StateAlphaStrategyTuningAxisBoard({}),
      getGen3StateAlphaStrategyTuningReviewQueue({}),
      getGen3StateAlphaStrategyTuningReplaySuggestions({}),
      getGen3StateAlphaStrategyTuningReplaySession({}),
      getGen3StateAlphaStrategyTuningCurrentStepCompletionPacket({}),
      getGen3StateAlphaStrategyTuningCompletionAudit({}),
      getGen3StateAlphaLiveReplayCockpit({}),
      ...(withHistorical
        ? [
            getGen3StateAlphaHistoricalDecisionReplayAudit({}),
            getGen3StateAlphaHistoricalDecisionReplayTasks({ limit: 80 })
          ]
        : [])
    ]
    const results = await Promise.all(tasks)
    const pick = (nv: any, setter: (fn: (prev: Row) => Row) => void, prevKey: string) => setter(() => nv || prevRef[prevKey] || {})
    const prevRef: Record<string, Row> = {
      axisBoard: strategyTuningAxisBoardPayload,
      queue: strategyTuningReviewQueuePayload,
      suggestions: strategyTuningReplaySuggestionsPayload,
      session: strategyTuningReplaySessionPayload,
      completionPacket: strategyTuningCurrentStepCompletionPacketPayload,
      completionAudit: strategyTuningCompletionAuditPayload,
      cockpit: liveReplayCockpitPayload,
      histAudit: historicalDecisionReplayAuditPayload,
      histTasks: historicalDecisionReplayPayload
    }
    pick(results[0], setStrategyTuningAxisBoardPayload, 'axisBoard')
    pick(results[1], setStrategyTuningReviewQueuePayload, 'queue')
    pick(results[2], setStrategyTuningReplaySuggestionsPayload, 'suggestions')
    pick(results[3], setStrategyTuningReplaySessionPayload, 'session')
    pick(results[4], setStrategyTuningCurrentStepCompletionPacketPayload, 'completionPacket')
    pick(results[5], setStrategyTuningCompletionAuditPayload, 'completionAudit')
    pick(results[6], setLiveReplayCockpitPayload, 'cockpit')
    if (withHistorical) {
      pick(results[7], setHistoricalDecisionReplayAuditPayload, 'histAudit')
      pick(results[8], setHistoricalDecisionReplayPayload, 'histTasks')
    }
  }

  const saveStrategyTuningTaskReview = async (row: Row, result: string) => {
    const key = strategyTuningTaskRowKey(row)
    const label = strategyTuningReviewResultLabel(result)
    const note = await prompt({
      title: 'strategy tuning review',
      message: `Record tuning review: ${row?.axis_label || row?.axis || '--'} / ${row?.object || '--'} / ${label}. This only records evidence and never enables trading.`,
      placeholder: 'Evidence, decision, or reason to keep watching',
      minLength: 4,
      confirmText: 'save review',
      cancelText: 'cancel'
    })
    if (note === null) return
    setStrategyTuningTaskSavingKey(key)
    try {
      const saved = (await saveGen3StateAlphaStrategyTuningTaskReview({
        task_key: key,
        axis: row?.axis,
        axis_label: row?.axis_label,
        origin: row?.origin,
        object: row?.object,
        problem_signal: row?.problem_signal,
        source_status: row?.source_status,
        review_result: result,
        evidence: row?.evidence,
        completion_evidence: row?.completion_evidence,
        review_note: note,
        hidden_risk: row?.problem_signal,
        decision: label,
        optimization_suggestion: row?.suggested_learning,
        next_action: row?.next_action,
        optimization_boundary: row?.optimization_boundary,
        can_execute_trade: false,
        can_change_strategy_contract: false,
        profit_only_optimization_allowed: false
      })) as Row
      if (!saved?.ok) {
        toast.error(saved?.error || 'strategy tuning review save failed')
        return
      }
      await refreshTuningBoards(true)
      toast.success('strategy tuning review saved')
    } catch (error: any) {
      toast.error(error?.message || 'strategy tuning review save failed')
    } finally {
      setStrategyTuningTaskSavingKey('')
    }
  }

  const applyStrategyTuningReplaySuggestion = async (row: Row) => {
    const key = strategyTuningTaskRowKey(row)
    const result = row?.suggested_review_result || 'continue_watch'
    setStrategyTuningTaskSavingKey(key)
    try {
      const saved = (await saveGen3StateAlphaStrategyTuningTaskReview({
        task_key: key,
        axis: row?.axis,
        axis_label: row?.axis_label,
        object: row?.object,
        problem_signal: row?.problem_signal,
        source_status: row?.source_status,
        review_result: result,
        evidence: row?.evidence_required,
        completion_evidence: row?.evidence_required,
        review_note: row?.suggested_review_note || row?.replay_focus,
        hidden_risk: row?.suggested_hidden_risk,
        decision: row?.suggested_decision,
        optimization_suggestion: row?.suggested_optimization,
        next_action: row?.next_action,
        optimization_boundary: row?.optimization_boundary,
        can_execute_trade: false,
        can_change_strategy_contract: false,
        profit_only_optimization_allowed: false
      })) as Row
      if (!saved?.ok) {
        toast.error(saved?.error || 'strategy tuning suggestion save failed')
        return
      }
      await refreshTuningBoards(true)
      toast.success('strategy tuning suggestion recorded')
    } catch (error: any) {
      toast.error(error?.message || 'strategy tuning suggestion save failed')
    } finally {
      setStrategyTuningTaskSavingKey('')
    }
  }

  const recordBrokerPostSyncExecutionEvidence = async () => {
    if (!truthy(vm.brokerPostSyncAcceptance.can_record_execution_evidence)) {
      toast.error('post-sync acceptance is not ready')
      return
    }
    setBrokerPostSyncEvidenceLoading(true)
    try {
      const saved = (await recordGen3StateAlphaBrokerPostSyncExecutionEvidence({
        source: 'g3_risk_page_post_sync_acceptance',
        refresh: false
      })) as Row
      if (!saved?.ok) {
        const gapCount = Array.isArray(saved?.gaps) ? saved.gaps.length : 0
        toast.error(saved?.error || `post-sync acceptance still blocked: ${gapCount} gaps`)
        setBrokerPostSyncAcceptancePayload(((await getGen3StateAlphaBrokerPostSyncAcceptance({})) as Row) || {})
        setLiveActionConsolePayload(((await getGen3StateAlphaLiveActionConsole({})) as Row) || {})
        setLiveReplayCockpitPayload(((await getGen3StateAlphaLiveReplayCockpit({})) as Row) || {})
        return
      }
      const [acceptance, consolePayload] = await Promise.all([
        getGen3StateAlphaBrokerPostSyncAcceptance({}),
        getGen3StateAlphaLiveActionConsole({})
      ])
      setBrokerPostSyncAcceptancePayload((prev) => (acceptance as Row) || prev || {})
      setLiveActionConsolePayload((prev) => (consolePayload as Row) || prev || {})
      await refreshTuningBoards(false)
      toast.success('execution evidence recorded')
    } catch (error: any) {
      toast.error(error?.message || 'record execution evidence failed')
    } finally {
      setBrokerPostSyncEvidenceLoading(false)
    }
  }

  const handleLiveActionConsoleStep = async (row: Row = {}) => {
    const key = String(row?.action_key || '').trim()
    if (!key) {
      toast.info('no action key')
      return
    }
    setLiveActionConsoleLoading(true)
    try {
      if (key === 'sync_broker_holding_price') {
        setLiveActionConsoleLoading(false)
        await syncBrokerHoldingsAndReview()
        setLiveActionConsolePayload(((await getGen3StateAlphaLiveActionConsole({})) as Row) || {})
        setLiveReplayCockpitPayload(((await getGen3StateAlphaLiveReplayCockpit({})) as Row) || {})
        return
      }
      if (key === 'post_sync_acceptance') {
        setBrokerPostSyncAcceptancePayload(
          ((await getGen3StateAlphaBrokerPostSyncAcceptance({ refresh: false })) as Row) || {}
        )
        setLiveActionConsolePayload(((await getGen3StateAlphaLiveActionConsole({})) as Row) || {})
        setLiveReplayCockpitPayload(((await getGen3StateAlphaLiveReplayCockpit({})) as Row) || {})
        toast.success('post-sync acceptance refreshed')
        return
      }
      if (key === 'record_execution_evidence') {
        setLiveActionConsoleLoading(false)
        await recordBrokerPostSyncExecutionEvidence()
        setLiveActionConsolePayload(((await getGen3StateAlphaLiveActionConsole({})) as Row) || {})
        setLiveReplayCockpitPayload(((await getGen3StateAlphaLiveReplayCockpit({})) as Row) || {})
        return
      }
      if (key === 'rerun_tuning_completion') {
        setStrategyTuningCompletionAuditPayload(
          ((await getGen3StateAlphaStrategyTuningCompletionAudit({ refresh: true })) as Row) || {}
        )
        setStrategyTuningReplaySessionPayload(((await getGen3StateAlphaStrategyTuningReplaySession({})) as Row) || {})
        setStrategyTuningCurrentStepCompletionPacketPayload(
          ((await getGen3StateAlphaStrategyTuningCurrentStepCompletionPacket({})) as Row) || {}
        )
        setLiveActionConsolePayload(((await getGen3StateAlphaLiveActionConsole({})) as Row) || {})
        setLiveReplayCockpitPayload(((await getGen3StateAlphaLiveReplayCockpit({})) as Row) || {})
        toast.success('tuning completion audit refreshed')
        return
      }
      if (key === 'manual_live_review_gate') {
        setLiveLaunchReadinessAuditPayload(
          ((await getGen3StateAlphaLiveLaunchReadinessAudit({ refresh: true })) as Row) || {}
        )
        setLiveActionConsolePayload(((await getGen3StateAlphaLiveActionConsole({})) as Row) || {})
        setLiveReplayCockpitPayload(((await getGen3StateAlphaLiveReplayCockpit({})) as Row) || {})
        toast.success('live readiness audit refreshed')
        return
      }
      toast.info(`no handler for ${key}`)
    } catch (error: any) {
      toast.error(error?.message || 'live action step failed')
    } finally {
      setLiveActionConsoleLoading(false)
    }
  }

  const saveLiveActionConsoleStepIssue = async (row: Row = {}) => {
    const key = String(row?.action_key || '').trim()
    if (!key) {
      toast.info('no action key')
      return
    }
    const note = await prompt({
      title: 'live action issue',
      message: `Record blocker for ${row?.action_label || key}. This only writes replay evidence and will not unlock trading.`,
      placeholder: 'What evidence shows this step is not naturally ready?',
      initialValue: row?.next_action || row?.current_evidence || '',
      confirmText: 'record',
      cancelText: 'cancel'
    })
    if (note === null) return
    setLiveActionConsoleReviewSavingKey(key)
    try {
      const saved = (await saveGen3StateAlphaLiveActionConsoleStepReview({
        action_key: key,
        review_result: 'issue_found',
        review_note: note,
        problem_signal: `${key} status=${row?.status || '--'}`,
        hidden_risk: row?.trade_impact,
        next_action: row?.next_action,
        optimization_suggestion:
          'Use this step as execution-evidence replay material before tuning buy/sell/selection/model switching.',
        can_execute_trade: false,
        can_change_strategy_contract: false,
        profit_only_optimization_allowed: false
      })) as Row
      if (!saved?.ok) {
        toast.error(saved?.error || 'live action issue record failed')
        return
      }
      const consolePayload = await getGen3StateAlphaLiveActionConsole({})
      setLiveActionConsolePayload((prev) => (consolePayload as Row) || prev || {})
      await refreshTuningBoards(false)
      toast.success('live action issue recorded')
    } catch (error: any) {
      toast.error(error?.message || 'live action issue record failed')
    } finally {
      setLiveActionConsoleReviewSavingKey('')
    }
  }

  const saveLaunchDayPlaybookReview = async (row: Row, result: string) => {
    const key = launchDayPlaybookRowKey(row)
    const label = dailyReviewChecklistResultLabel(result)
    const note = await prompt({
      title: '启动日剧本落账',
      message: `记录启动日剧本复盘：${row?.checkpoint_time || '--'} / ${row?.action_type || '--'}，结果为“${label}”。请写一句证据或判断。`,
      placeholder: '例如：同步真实持仓后 blocker 仍未消失；或：无票日归因符合自然空仓，不补票。',
      minLength: 4,
      confirmText: '保存复盘'
    })
    if (note === null) return
    const autoText = launchDayPlaybookAutoText(result, row)
    setLaunchDayPlaybookSavingKey(key)
    try {
      const saved = (await saveGen3StateAlphaLaunchDayPlaybookReviewAndRun({
        review_key: row.review_key || key,
        priority: row.priority,
        window: row.window,
        checkpoint_time: row.checkpoint_time,
        action_type: row.action_type,
        review_axis: row.review_axis,
        object: row.object,
        required_action: row.required_action,
        evidence_to_collect: row.evidence_to_collect,
        pass_condition: row.pass_condition,
        fail_condition: row.fail_condition,
        review_result: result,
        issue_area: row.review_axis,
        evidence: row.evidence_to_collect,
        natural_decision: autoText.naturalDecision,
        optimization_suggestion: autoText.suggestion,
        review_note: note,
        source: 'launch_day_playbook'
      })) as Row
      if (!saved?.ok) {
        toast.error(saved?.error || '启动日剧本复盘保存失败')
        return
      }
      if (saved.packet) setLiveLaunchPacketPayload(saved.packet)
      setLiveLearningLedgerPayload(((await getGen3StateAlphaLiveLearningLedger({})) as Row) || {})
      setLiveLaunchDecisionPayload(((await getGen3StateAlphaLiveLaunchDecision({})) as Row) || {})
      setLiveBlockerEvidenceBoardPayload(((await getGen3StateAlphaLiveBlockerEvidenceBoard({})) as Row) || {})
      setLiveLaunchReadinessAuditPayload(((await getGen3StateAlphaLiveLaunchReadinessAudit({})) as Row) || {})
      await refreshTuningBoards(false)
      if (saved.live_launch_review_snapshot) {
        upsertLiveLaunchSnapshot(saved.live_launch_review_snapshot)
      }
      if (saved.save?.review) {
        setLaunchDayPlaybookReviewLedger((prev) => [
          saved.save.review,
          ...prev.filter((item) => String(item?.key || item?.review_key || '') !== key)
        ])
      }
      toast.success('已保存启动日剧本复盘并重建启动包')
    } catch (error: any) {
      toast.error(error?.message || '启动日剧本复盘保存失败')
    } finally {
      setLaunchDayPlaybookSavingKey('')
    }
  }

  const saveDailyReviewChecklistReview = async (row: Row, result: string) => {
    const key = dailyReviewChecklistRowKey(row)
    const label = dailyReviewChecklistResultLabel(result)
    const note = await prompt({
      title: '逐项复盘落账',
      message: `记录逐项复盘：${reviewAxisLabel(row?.review_axis)} / ${row?.object || row?.code || '--'}，结果为“${label}”。请写一句证据或判断。`,
      placeholder: '例如：盘后走势验证为自然空仓；或：候选被挡后走强，需复核选股阻断理由。',
      minLength: 4,
      confirmText: '保存复盘'
    })
    if (note === null) return
    const autoText = dailyReviewChecklistAutoText(result, row)
    setDailyReviewChecklistSavingKey(key)
    try {
      const saved = (await saveGen3StateAlphaDailyReviewChecklistReviewAndRun({
        review_key: row.review_key || key,
        ticket_key: row.ticket_key,
        review_axis: row.review_axis,
        review_scope: row.review_scope,
        object: row.object,
        code: row.code,
        name: row.name,
        entry_date: row.entry_date,
        review_result: result,
        issue_area: row.review_axis,
        evidence: row.evidence_to_collect || row.evidence_to_record,
        natural_decision: autoText.naturalDecision,
        optimization_suggestion: autoText.suggestion,
        review_note: note,
        source: row.source || 'live_daily_review_execution_checklist'
      })) as Row
      if (!saved?.ok) {
        toast.error(saved?.error || '逐项复盘保存失败')
        return
      }
      if (saved.review) setReadinessReview(saved.review)
      toast.success('已保存逐项复盘并完成复审')
      await refreshCurrentAndLedgers('daily_review_checklist_saved')
    } catch (error: any) {
      toast.error(error?.message || '逐项复盘保存失败')
    } finally {
      setDailyReviewChecklistSavingKey('')
    }
  }

  // ---------- 逐票复盘 payload 构建 ----------
  const splitReviewItems = (text: any) =>
    String(text || '')
      .split(/[；;、\n]/)
      .map((item) => item.trim())
      .filter(Boolean)

  const buildPretradeReviewPayload = (row: Row, action: string) => {
    const code = String(row?.code || '').trim()
    const matched = vm.ticketChecklistRows.find((item: Row) => String(item?.code || '').trim() === code) || {}
    const ticketItems = splitReviewItems(matched.manual_approval_checklist || row?.manual_approval_checklist || '')
    const naturalDecision = vm.naturalExecutionRows.find((item: Row) => String(item?.code || '').trim() === code) || {}
    const naturalConsistency =
      vm.naturalConsistencyRows.find((item: Row) => String(item?.code || '').trim() === code) || {}
    const confirmationItems = [
      ...ticketItems,
      naturalDecision.required_confirmation,
      naturalDecision.downgrade_rule ? `降级规则：${naturalDecision.downgrade_rule}` : '',
      '确认本次记录不会触发自动下单，正式执行仍需盘前价格/持仓复核'
    ].filter(Boolean)
    const items = Array.from(new Set(confirmationItems))
    const codeName = `${row?.code || ''} ${row?.name || ''}`.trim()
    const deductions = naturalConsistency?.deductions || naturalDecision?.source_deductions || ''
    const reasonText = (() => {
      if (action === 'manual_approved') {
        return `${codeName} 已完成人工放行复核；核心依据：${deductions || naturalDecision?.required_confirmation || '逐票确认通过'}`
      }
      if (action === 'paper_watch') {
        return `${codeName} 先纸面观察；原因：${deductions || naturalDecision?.required_confirmation || '实战前仍需观察买点、热度和执行约束'}`
      }
      if (action === 'wait_refresh') {
        return `${codeName} 等待刷新后再判断；原因：${naturalConsistency?.readiness_gaps || naturalDecision?.required_confirmation || '价格、持仓或信号证据未闭环'}`
      }
      if (action === 'skip') {
        return `${codeName} 本次跳过；原因：${deductions || naturalDecision?.downgrade_rule || '人工判断不进入本次执行'}`
      }
      return `${codeName} 记录逐票复盘结论`
    })()
    const triggerText = (() => {
      if (action === 'manual_approved') return '盘前同步真实持仓和盘口价格后重跑实战前审计'
      if (action === 'paper_watch') return '收盘后对照次日走势、买点新鲜度和策略解释复盘'
      if (action === 'wait_refresh') return '完成真实持仓/价格/信号刷新后重跑实战前审计'
      if (action === 'skip') return naturalDecision?.downgrade_rule || '本次不再进入执行，收盘后复盘是否误杀'
      return '重跑 G3 实战前审计'
    })()
    const notesMap: Record<string, string> = {
      paper_watch: '实战前逐票复盘：先按纸面观察记录，不开启自动实盘',
      manual_approved: '实战前逐票复盘：人工确认票据、风控与执行约束后放行',
      wait_refresh: '实战前逐票复盘：等待价格/持仓/信号刷新后再判断',
      skip: '实战前逐票复盘：人工跳过，不进入本次执行'
    }
    const noteText = notesMap[action] || '实战前逐票复盘：记录人工判断'
    return {
      ticket_key: row?.ticket_key,
      code: row?.code,
      name: row?.name,
      entry_date: row?.entry_date,
      route: row?.route,
      natural_action: row?.natural_action,
      natural_reason: row?.notes,
      review_action: action,
      review_confidence: action === 'manual_approved' ? 'high' : action === 'paper_watch' ? 'medium' : 'low',
      risk_acknowledged: action === 'manual_approved',
      confirmation_items: action === 'manual_approved' ? items : [],
      checked_items: action === 'manual_approved' ? items : [],
      execution_posture: naturalDecision.execution_posture,
      decision_level: naturalDecision.decision_level,
      consistency_grade: naturalConsistency.consistency_grade,
      consistency_score: naturalConsistency.consistency_score,
      operational_readiness_score: naturalConsistency.operational_readiness_score,
      required_confirmation: naturalDecision.required_confirmation,
      downgrade_rule: naturalDecision.downgrade_rule,
      review_decision_reason: reasonText,
      next_review_trigger: triggerText,
      review_evidence: {
        buy_point_logic: naturalConsistency.buy_point_logic,
        sell_point_logic: naturalConsistency.sell_point_logic,
        selection_logic: naturalConsistency.selection_logic,
        model_switch_logic: naturalConsistency.model_switch_logic,
        logic_deductions: naturalConsistency.deductions,
        readiness_gaps: naturalConsistency.readiness_gaps,
        source_deductions: naturalDecision.source_deductions
      },
      note:
        action === 'manual_approved' && items.length
          ? `${noteText}；确认项：${items.join('；')}`
          : noteText
    }
  }

  const savePretradeReview = async (row: Row, action: string) => {
    const key = reviewRowKey(row)
    const payloadRow = buildPretradeReviewPayload(row, action)
    if (action === 'manual_approved') {
      const ok = await confirm({
        title: '逐票人工放行确认',
        message: `确认将 ${row?.code || ''} ${row?.name || ''} 标记为“人工放行”？这只记录复盘结论，不会触发下单。\n\n必须确认：\n- ${(
          payloadRow.confirmation_items || []
        ).join('\n- ')}`,
        confirmText: '确认放行'
      })
      if (!ok) return
    }
    setReviewSavingKey(key)
    try {
      const result = (await saveGen3StateAlphaPretradeTicketReview(payloadRow)) as Row
      if (result?.ok) {
        toast.success('已记录逐票复盘结论')
        await refreshReadinessAndLedger('pretrade_ticket_review_saved')
      } else {
        toast.error(result?.error || '逐票复盘记录失败')
      }
    } catch (error: any) {
      toast.error(error?.message || '逐票复盘记录失败')
    } finally {
      setReviewSavingKey('')
    }
  }

  const markUnreviewedAsPaperWatch = async () => {
    if (!vm.unreviewedTicketRows.length) {
      toast.info('当前没有未复盘票据')
      return
    }
    setBatchReviewLoading(true)
    let preview: Row
    try {
      preview = (await startGen3StateAlphaPretradePaperWatchBatch({
        reason: '实战启动批次：未复盘票据先进入纸面观察',
        include_existing: false,
        dry_run: true,
        run_readiness_review: false
      })) as Row
    } catch (error: any) {
      setBatchReviewLoading(false)
      toast.error(error?.message || '纸面观察批次预检失败')
      return
    }
    const previewRows = Array.isArray(preview?.preview_reviews) ? preview.preview_reviews : []
    const skippedRows = Array.isArray(preview?.skipped) ? preview.skipped : []
    if (!previewRows.length) {
      setBatchReviewLoading(false)
      toast.info(
        skippedRows.length ? `没有新增纸面观察票；已跳过 ${skippedRows.length} 张` : '当前没有可初始化的纸面观察票'
      )
      return
    }
    const previewText = previewRows.map((row: Row) => `${row.code || '--'} ${row.name || ''}`).join('、')
    const ok = await confirm({
      title: '批量纸面观察',
      message: `确认把 ${previewRows.length} 张未复盘票据记录为“纸面观察”？\n\n预检票据：${previewText}\n\n这不会触发下单，也不会视为正式放行。`,
      confirmText: '记录纸面观察'
    })
    if (!ok) {
      setBatchReviewLoading(false)
      return
    }
    try {
      const batch = (await startGen3StateAlphaPretradePaperWatchBatch({
        reason: '实战启动批次：未复盘票据先进入纸面观察',
        include_existing: false,
        run_readiness_review: true
      })) as Row
      await Promise.all([
        getGen3StateAlphaPretradeTicketReviews({}).then((ledger: any) => {
          setPretradeReviewLedger(Array.isArray(ledger?.reviews) ? ledger.reviews : [])
        }),
        getGen3StateAlphaPaperWatchReviews({}).then((ledger: any) => {
          setPaperWatchReviewLedger(Array.isArray(ledger?.reviews) ? ledger.reviews : [])
        }),
        getGen3StateAlphaRealtimeReadinessReview({}).then((review: any) => {
          setReadinessReview(review || {})
        })
      ])
      if (!batch?.ok) {
        toast.error(
          batch?.saved_count
            ? `已记录 ${batch.saved_count} 张，仍有异常需复查`
            : batch?.message || '批量纸面观察未完成'
        )
      } else if (batch?.skipped_count) {
        toast.success(
          `已启动纸面观察批次 ${batch.batch_id}：新增 ${batch.saved_count} 张，跳过 ${batch.skipped_count} 张`
        )
      } else {
        toast.success(`已启动纸面观察批次 ${batch.batch_id}：${batch.saved_count} 张`)
      }
    } catch (error: any) {
      toast.error(error?.message || '批量记录纸面观察失败')
    } finally {
      setBatchReviewLoading(false)
    }
  }

  const recordDay1PaperExecutions = async () => {
    if (!vm.day1PaperReviewRows.length) {
      toast.info('当前没有Day1纸面复盘包')
      return
    }
    setDay1ExecutionLoading(true)
    let preview: Row
    try {
      preview = (await startGen3StateAlphaDay1PaperExecutionBatch({
        dry_run: true,
        include_existing: false,
        run_readiness_review: false
      })) as Row
    } catch (error: any) {
      setDay1ExecutionLoading(false)
      toast.error(error?.message || 'Day1纸面执行预检失败')
      return
    }
    const previewRows = Array.isArray(preview?.preview_executions) ? preview.preview_executions : []
    const skippedRows = Array.isArray(preview?.skipped) ? preview.skipped : []
    if (!previewRows.length) {
      setDay1ExecutionLoading(false)
      toast.info(
        skippedRows.length
          ? `没有新增纸面执行；已跳过 ${skippedRows.length} 张`
          : '当前没有可记录的Day1纸面执行'
      )
      return
    }
    const previewText = previewRows
      .map((row: Row) => `${row.code || '--'} ${row.name || ''} @${price(row.execution_price)} x ${row.quantity || 0}`)
      .join('\n')
    const ok = await confirm({
      title: 'Day1纸面执行',
      message: `确认把 ${previewRows.length} 张票写入Day1纸面执行台账？\n\n${previewText}\n\n这只记录纸面执行，不会触发真实下单，也不等于正式放行。`,
      confirmText: '写入纸面台账'
    })
    if (!ok) {
      setDay1ExecutionLoading(false)
      return
    }
    try {
      const batch = (await startGen3StateAlphaDay1PaperExecutionBatch({
        include_existing: false,
        run_readiness_review: true
      })) as Row
      const [current, review] = await Promise.all([
        getGen3StateAlphaCurrent({ limit: 100 }),
        getGen3StateAlphaRealtimeReadinessReview({})
      ])
      setCurrentPayload((current as Row) || {})
      setReadinessReview((review as Row) || {})
      if (!batch?.ok) {
        toast.error(
          batch?.saved_count
            ? `已写入 ${batch.saved_count} 张，仍有异常需复查`
            : batch?.message || 'Day1纸面执行未完成'
        )
      } else if (batch?.skipped_count) {
        toast.success(
          `已写入Day1纸面执行批次 ${batch.batch_id}：新增 ${batch.saved_count} 张，跳过 ${batch.skipped_count} 张`
        )
      } else {
        toast.success(`已写入Day1纸面执行批次 ${batch.batch_id}：${batch.saved_count} 张`)
      }
    } catch (error: any) {
      toast.error(error?.message || 'Day1纸面执行写入失败')
    } finally {
      setDay1ExecutionLoading(false)
    }
  }

  const savePaperWatchFollowup = async (row: Row, result: string) => {
    const label = paperWatchResultLabel(result)
    const note = await prompt({
      title: '纸面观察后评估',
      message: `记录 ${row?.code || ''} ${row?.name || ''} 的纸面观察结果：${label}。请写一句观察依据，例如走势是否验证买点、是否追高、是否暴露选股或卖点隐患。`,
      placeholder: '观察依据',
      minLength: 4,
      confirmText: '保存评估'
    })
    if (note === null) return
    const reviewContext = isCandidateOmissionReview(row) ? 'candidate_omission' : 'paper_watch'
    const autoText = paperWatchAutoReviewText(result, row)
    try {
      const payloadRow = {
        ticket_key: row?.ticket_key || row?.key,
        code: row?.code,
        name: row?.name,
        entry_date: row?.entry_date,
        route: row?.route,
        source:
          row?.source ||
          (reviewContext === 'candidate_omission' ? 'candidate_omission_checklist' : 'g3_state_alpha_risk_page'),
        review_context: reviewContext,
        watch_result: result,
        issue_area: paperWatchIssueArea(result),
        hidden_risk: autoText.hiddenRisk,
        optimization_suggestion: autoText.suggestion,
        selection_state: row?.selection_state || row?.evidence,
        strategy_switch_assessment: row?.strategy_switch_assessment || row?.natural_decision,
        candidate_block_reason: row?.hidden_risks || row?.optimization_focus,
        action_recommendation: row?.action_recommendation || row?.required_action,
        review_note: note
      }
      const saved = (reviewContext === 'candidate_omission'
        ? await saveGen3StateAlphaCandidateOmissionReviewAndRun({ ...payloadRow, review_result: result })
        : await saveGen3StateAlphaPaperWatchReview(payloadRow)) as Row
      if (!saved?.ok) {
        toast.error(saved?.error || '纸面观察评估保存失败')
        return
      }
      if (saved.review) setReadinessReview(saved.review)
      toast.success(
        reviewContext === 'candidate_omission' ? '已保存候选遗漏复盘并完成复审' : '已保存纸面观察后评估'
      )
      if (reviewContext === 'candidate_omission') {
        await refreshCurrentAndLedgers('candidate_omission_review_saved')
      } else {
        await refreshReadinessAndLedger('paper_watch_followup_saved')
      }
    } catch (error: any) {
      toast.error(error?.message || '纸面观察评估保存失败')
    }
  }

  const saveNoTradeDayReview = async (row: Row, result: string) => {
    const label = noTradeReviewResultLabel(result)
    const note = await prompt({
      title: '无票日复盘',
      message: `记录 ${row?.entry_date || ''} 无票复盘：${label}。请写一句依据，例如是否自然空仓、是否误杀候选、是否来自数据或流程缺口。`,
      placeholder: '复盘依据',
      minLength: 4,
      confirmText: '保存复盘'
    })
    if (note === null) return
    const autoText = noTradeAutoReviewText(result, row)
    try {
      const saved = (await saveGen3StateAlphaNoTradeDayReviewAndRun({
        review_key: row?.review_key,
        entry_date: row?.entry_date,
        review_type: row?.review_type,
        posture: row?.posture,
        source: row?.source,
        review_result: result,
        issue_area: noTradeIssueArea(result),
        evidence: row?.evidence,
        hidden_risk: autoText.hiddenRisk,
        natural_decision: row?.natural_decision,
        optimization_suggestion: autoText.suggestion,
        review_note: note
      })) as Row
      if (!saved?.ok) {
        toast.error(saved?.error || '无票日复盘保存失败')
        return
      }
      if (saved.review) setReadinessReview(saved.review)
      toast.success('已保存无票日复盘并完成复审')
      await refreshCurrentAndLedgers('no_trade_day_review_saved')
    } catch (error: any) {
      toast.error(error?.message || '无票日复盘保存失败')
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const flags = {
    loading,
    reviewLoading,
    brokerSyncLoading,
    brokerPostSyncEvidenceLoading,
    liveActionConsoleLoading,
    premarketControlLoading,
    premarketNextActionLoading,
    brokerSyncPreflightLoading,
    liveLaunchPacketLoading,
    liveLaunchSnapshotLoading,
    liveLaunchSnapshotDetailLoading,
    liveLaunchSnapshotDetailId,
    liveLaunchSnapshotDetailVisible,
    batchReviewLoading,
    day1ExecutionLoading,
    reviewSavingKey,
    formalActionSavingKey,
    dailyReviewChecklistSavingKey,
    launchDayPlaybookSavingKey,
    strategyTuningTaskSavingKey,
    liveActionConsoleReviewSavingKey
  }

  const actions = {
    load,
    markUnreviewedAsPaperWatch,
    recordDay1PaperExecutions,
    syncBrokerHoldingsAndReview,
    runReadinessReview,
    refreshBrokerSyncPreflight,
    refreshPremarketControl,
    executePremarketNextAction,
    refreshLiveLaunchPacket,
    recordLiveLaunchReviewSnapshot,
    openLiveLaunchSnapshotDetail,
    closeLiveLaunchSnapshotDetail,
    saveStrategyTuningTaskReview,
    applyStrategyTuningReplaySuggestion,
    recordBrokerPostSyncExecutionEvidence,
    handleLiveActionConsoleStep,
    saveLiveActionConsoleStepIssue,
    saveLaunchDayPlaybookReview,
    saveDailyReviewChecklistReview,
    handleDay1JournalPrimaryAction,
    saveNoTradeDayReview,
    saveFormalActionReview,
    savePaperWatchFollowup,
    handleFormalReviewBoardAction,
    handlePremarketSequenceAction,
    handleBlockerResolutionAction,
    savePretradeReview
  }

  return (
    <div className="mx-auto max-w-[1600px] px-4 py-6 sm:px-6 lg:px-8">
      <RiskProvider value={{ v: { ...vm, ...flags }, a: actions }}>
        <MetricHeader />
        <PremarketPanel />
        <LiveLaunchPanel />
        <ReadinessPanelA />
        <ContractsPanel />
      </RiskProvider>
      {dialogElement}
    </div>
  )
}
