import request from '@/utils/request'

export function getGen2Live(params) {
  return request({
    url: '/trading/gen2/live',
    method: 'get',
    params
  })
}
export function getGen3ShadowLive(params) {
  return request({
    url: '/gen3-shadow/live',
    method: 'get',
    params,
    timeout: 300000
  })
}

export function getGen3StateAlphaContract(params) {
  return request({
    url: '/gen3-state-alpha/contract',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function getGen3StateAlphaCurrent(params) {
  return request({
    url: '/gen3-state-alpha/current',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function getGen3StateAlphaRealtimeReadinessReview(params) {
  return request({
    url: '/gen3-state-alpha/realtime-readiness-review',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function runGen3StateAlphaRealtimeReadinessReview(data) {
  return request({
    url: '/gen3-state-alpha/realtime-readiness-review/run',
    method: 'post',
    data,
    timeout: 180000
  })
}

export function getGen3StateAlphaLiveLaunchPacket(params) {
  return request({
    url: '/gen3-state-alpha/live-launch-packet',
    method: 'get',
    params,
    timeout: 180000
  })
}

export function runGen3StateAlphaLiveLaunchPacket(data) {
  return request({
    url: '/gen3-state-alpha/live-launch-packet/run',
    method: 'post',
    data,
    timeout: 240000
  })
}

export function getGen3StateAlphaLiveLearningLedger(params) {
  return request({
    url: '/gen3-state-alpha/live-learning-ledger',
    method: 'get',
    params,
    timeout: 180000
  })
}

export function getGen3StateAlphaLiveLaunchDecision(params) {
  return request({
    url: '/gen3-state-alpha/live-launch-decision',
    method: 'get',
    params,
    timeout: 180000
  })
}

export function getGen3StateAlphaLiveBlockerEvidenceBoard(params) {
  return request({
    url: '/gen3-state-alpha/live-blocker-evidence-board',
    method: 'get',
    params,
    timeout: 180000
  })
}

export function getGen3StateAlphaLiveLaunchReadinessAudit(params) {
  return request({
    url: '/gen3-state-alpha/live-launch-readiness-audit',
    method: 'get',
    params,
    timeout: 180000
  })
}

export function getGen3StateAlphaStrategyTuningAxisBoard(params) {
  return request({
    url: '/gen3-state-alpha/strategy-tuning-axis-board',
    method: 'get',
    params,
    timeout: 180000
  })
}

export function getGen3StateAlphaStrategyTuningReviewQueue(params) {
  return request({
    url: '/gen3-state-alpha/strategy-tuning-review-queue',
    method: 'get',
    params,
    timeout: 180000
  })
}

export function getGen3StateAlphaStrategyTuningCompletionAudit(params) {
  return request({
    url: '/gen3-state-alpha/strategy-tuning-completion-audit',
    method: 'get',
    params,
    timeout: 180000
  })
}

export function getGen3StateAlphaStrategyTuningReplaySuggestions(params) {
  return request({
    url: '/gen3-state-alpha/strategy-tuning-replay-suggestions',
    method: 'get',
    params,
    timeout: 180000
  })
}

export function getGen3StateAlphaStrategyTuningReplaySession(params) {
  return request({
    url: '/gen3-state-alpha/strategy-tuning-replay-session',
    method: 'get',
    params,
    timeout: 180000
  })
}

export function getGen3StateAlphaStrategyTuningCurrentStepCompletionPacket(params) {
  return request({
    url: '/gen3-state-alpha/strategy-tuning-current-step-completion-packet',
    method: 'get',
    params,
    timeout: 180000
  })
}

export function getGen3StateAlphaLiveReplayCockpit(params) {
  return request({
    url: '/gen3-state-alpha/live-replay-cockpit',
    method: 'get',
    params,
    timeout: 180000
  })
}

export function getGen3StateAlphaStrategyTuningTaskReviews(params) {
  return request({
    url: '/gen3-state-alpha/strategy-tuning-task-reviews',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function saveGen3StateAlphaStrategyTuningTaskReview(data) {
  return request({
    url: '/gen3-state-alpha/strategy-tuning-task-review',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function getGen3StateAlphaLiveLaunchReviewSnapshots(params) {
  return request({
    url: '/gen3-state-alpha/live-launch-review-snapshots',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function getGen3StateAlphaLiveLaunchReviewSnapshotDetail(snapshotId) {
  return request({
    url: `/gen3-state-alpha/live-launch-review-snapshot/${encodeURIComponent(snapshotId)}`,
    method: 'get',
    timeout: 120000
  })
}

export function recordGen3StateAlphaLiveLaunchReviewSnapshot(data) {
  return request({
    url: '/gen3-state-alpha/live-launch-review-snapshot',
    method: 'post',
    data,
    timeout: 240000
  })
}

export function getGen3StateAlphaLaunchDayPlaybookReviews(params) {
  return request({
    url: '/gen3-state-alpha/launch-day-playbook-reviews',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function saveGen3StateAlphaLaunchDayPlaybookReviewAndRun(data) {
  return request({
    url: '/gen3-state-alpha/launch-day-playbook-review-and-run',
    method: 'post',
    data,
    timeout: 240000
  })
}

export function getGen3StateAlphaPremarketControl(params) {
  return request({
    url: '/gen3-state-alpha/premarket-control',
    method: 'get',
    params,
    timeout: 180000
  })
}

export function executeGen3StateAlphaPremarketNextAction(data) {
  return request({
    url: '/gen3-state-alpha/premarket-control/execute-next',
    method: 'post',
    data,
    timeout: 240000
  })
}

export function getGen3StateAlphaMainwaveOpportunities(params) {
  return request({
    url: '/gen3-state-alpha/mainwave-opportunities',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function getGen3StateAlphaHistoricalTrades(params) {
  return request({
    url: '/gen3-state-alpha/historical-trades',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function getGen3StateAlphaHistoricalDecisionReplayTasks(params) {
  return request({
    url: '/gen3-state-alpha/historical-decision-replay-tasks',
    method: 'get',
    params,
    timeout: 180000
  })
}

export function getGen3StateAlphaHistoricalDecisionReplayAudit(params) {
  return request({
    url: '/gen3-state-alpha/historical-decision-replay-audit',
    method: 'get',
    params,
    timeout: 180000
  })
}

export function getGen3StateAlphaBrokerHoldings(params) {
  return request({
    url: '/gen3-state-alpha/broker/holdings',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function getGen3StateAlphaBrokerHoldingsSyncPreflight(params) {
  return request({
    url: '/gen3-state-alpha/broker/holdings/sync-ths-preflight',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function getGen3StateAlphaBrokerHoldingsSyncConfirmationPacket(params) {
  return request({
    url: '/gen3-state-alpha/broker/holdings/sync-ths-confirmation-packet',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function getGen3StateAlphaBrokerHoldingsSyncOutcome(params) {
  return request({
    url: '/gen3-state-alpha/broker/holdings/sync-ths-outcome',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function getGen3StateAlphaBrokerPostSyncAcceptance(params) {
  return request({
    url: '/gen3-state-alpha/broker/holdings/post-sync-acceptance',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function recordGen3StateAlphaBrokerPostSyncExecutionEvidence(data) {
  return request({
    url: '/gen3-state-alpha/broker/holdings/post-sync-acceptance/record-execution-evidence',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function getGen3StateAlphaLiveActionConsole(params) {
  return request({
    url: '/gen3-state-alpha/live-action-console',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function saveGen3StateAlphaLiveActionConsoleStepReview(data) {
  return request({
    url: '/gen3-state-alpha/live-action-console/step-review',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function syncGen3StateAlphaBrokerHoldingsFromThs(data) {
  return request({
    url: '/gen3-state-alpha/broker/holdings/sync-ths',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function syncGen3StateAlphaBrokerHoldingsFromThsAndReview(data) {
  return request({
    url: '/gen3-state-alpha/broker/holdings/sync-ths-and-review',
    method: 'post',
    data,
    timeout: 240000
  })
}

export function refreshGen3StateAlphaBrokerSnapshot(data) {
  return request({
    url: '/gen3-state-alpha/broker/refresh',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function getGen3StateAlphaBrokerTrades(params) {
  return request({
    url: '/gen3-state-alpha/broker/trades',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function syncGen3StateAlphaBrokerTradesFromThs(data) {
  return request({
    url: '/gen3-state-alpha/broker/trades/sync-ths',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function getGen3StateAlphaWorkflowStatus(params) {
  return request({
    url: '/gen3-state-alpha/workflow/status',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function runGen3StateAlphaRefreshOnce(data) {
  return request({
    url: '/gen3-state-alpha/workflow/refresh/run-once',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function getGen3StateAlphaRefreshTask(taskId) {
  return request({
    url: `/gen3-state-alpha/workflow/refresh-task/${taskId}`,
    method: 'get',
    timeout: 120000
  })
}

export function getGen3StateAlphaShadowMonitorStatus() {
  return request({
    url: '/gen3-state-alpha/shadow-monitor/status',
    method: 'get',
    timeout: 120000
  })
}

export function setGen3StateAlphaShadowMonitorConfig(data) {
  return request({
    url: '/gen3-state-alpha/shadow-monitor/config',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function runGen3StateAlphaShadowMonitorOnce(data) {
  return request({
    url: '/gen3-state-alpha/shadow-monitor/run-once',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function saveGen3StateAlphaShadowVerification(data) {
  return request({
    url: '/gen3-state-alpha/shadow-verification',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function getGen3StateAlphaPretradeTicketReviews(params) {
  return request({
    url: '/gen3-state-alpha/pretrade-ticket-reviews',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function saveGen3StateAlphaPretradeTicketReview(data) {
  return request({
    url: '/gen3-state-alpha/pretrade-ticket-review',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function startGen3StateAlphaPretradePaperWatchBatch(data) {
  return request({
    url: '/gen3-state-alpha/pretrade-paper-watch-batch',
    method: 'post',
    data,
    timeout: 180000
  })
}

export function getGen3StateAlphaPaperWatchReviews(params) {
  return request({
    url: '/gen3-state-alpha/paper-watch-reviews',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function saveGen3StateAlphaPaperWatchReview(data) {
  return request({
    url: '/gen3-state-alpha/paper-watch-review',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function getGen3StateAlphaCandidateOmissionReviews(params) {
  return request({
    url: '/gen3-state-alpha/candidate-omission-reviews',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function saveGen3StateAlphaCandidateOmissionReviewAndRun(data) {
  return request({
    url: '/gen3-state-alpha/candidate-omission-review-and-run',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function getGen3StateAlphaNoTradeDayReviews(params) {
  return request({
    url: '/gen3-state-alpha/no-trade-day-reviews',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function saveGen3StateAlphaNoTradeDayReview(data) {
  return request({
    url: '/gen3-state-alpha/no-trade-day-review',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function saveGen3StateAlphaNoTradeDayReviewAndRun(data) {
  return request({
    url: '/gen3-state-alpha/no-trade-day-review-and-run',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function getGen3StateAlphaFormalActionReviews(params) {
  return request({
    url: '/gen3-state-alpha/formal-action-reviews',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function saveGen3StateAlphaFormalActionReview(data) {
  return request({
    url: '/gen3-state-alpha/formal-action-review',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function saveGen3StateAlphaFormalActionReviewAndRun(data) {
  return request({
    url: '/gen3-state-alpha/formal-action-review-and-run',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function getGen3StateAlphaDailyReviewChecklistReviews(params) {
  return request({
    url: '/gen3-state-alpha/daily-review-checklist-reviews',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function saveGen3StateAlphaDailyReviewChecklistReview(data) {
  return request({
    url: '/gen3-state-alpha/daily-review-checklist-review',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function saveGen3StateAlphaDailyReviewChecklistReviewAndRun(data) {
  return request({
    url: '/gen3-state-alpha/daily-review-checklist-review-and-run',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function getGen3StateAlphaPaperExecutions(params) {
  return request({
    url: '/gen3-state-alpha/paper-executions',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function submitGen3StateAlphaPaperOrder(data) {
  return request({
    url: '/gen3-state-alpha/paper-order',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function startGen3StateAlphaDay1PaperExecutionBatch(data) {
  return request({
    url: '/gen3-state-alpha/day1-paper-execution-batch',
    method: 'post',
    data,
    timeout: 180000
  })
}

export function getGen3StateAlphaEvidenceInventory(params) {
  return request({
    url: '/gen3-state-alpha/evidence-inventory',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function getGen3StateAlphaReplacementAssessment(params) {
  return request({
    url: '/gen3-state-alpha/replacement-assessment',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function getGen3StateAlphaObservationSnapshots(params) {
  return request({
    url: '/gen3-state-alpha/observation-snapshots',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function runGen3StateAlphaObservationSnapshot(data) {
  return request({
    url: '/gen3-state-alpha/observation-snapshot/run-once',
    method: 'post',
    data,
    timeout: 300000
  })
}

export function getGen3StateAlphaObservationSchedulerStatus(params) {
  return request({
    url: '/gen3-state-alpha/observation-scheduler/status',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function setGen3StateAlphaObservationSchedulerConfig(data) {
  return request({
    url: '/gen3-state-alpha/observation-scheduler/config',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function runGen3StateAlphaObservationSchedulerOnce(data) {
  return request({
    url: '/gen3-state-alpha/observation-scheduler/run-once',
    method: 'post',
    data,
    timeout: 300000
  })
}

export function getGen3GuardedShadow(params) {
  return request({
    url: '/gen3-shadow/guarded',
    method: 'get',
    params,
    timeout: 300000
  })
}

export function getGen3RangeFilteredShadow(params) {
  return request({
    url: '/gen3-shadow/range-filtered',
    method: 'get',
    params,
    timeout: 300000
  })
}

export function getV4MarketGate(params) {
  return request({
    url: '/trading/v4/market-gate',
    method: 'get',
    params
  })
}

export function getGen2StrategyLab(params) {
  return request({
    url: '/trading/gen2/lab',
    method: 'get',
    params
  })
}

export function getGen2OpenSignals(params) {
  return request({
    url: '/trading/gen2/open-signals',
    method: 'get',
    params
  })
}

export function getGen2Timing(params) {
  return request({
    url: '/trading/gen2/timing',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function getGen2Backtest(params) {
  return request({
    url: '/trading/gen2/backtest',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function runGen2BacktestLatest(params) {
  return request({
    url: '/trading/gen2/backtest/update-latest',
    method: 'post',
    params,
    timeout: 120000
  })
}

export function getGen2BacktestUpdateTask(taskId) {
  return request({
    url: `/trading/gen2/backtest/update-task/${taskId}`,
    method: 'get',
    timeout: 120000
  })
}

export function getGen2StrategyRefreshStatus() {
  return request({
    url: '/trading/gen2/strategy-refresh/status',
    method: 'get',
    timeout: 120000
  })
}

export function setGen2StrategyRefreshConfig(data) {
  return request({
    url: '/trading/gen2/strategy-refresh/config',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function runGen2StrategyRefreshNow(data) {
  return request({
    url: '/trading/gen2/strategy-refresh/run-once',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function getGen2FactorRegistry(params) {
  return request({
    url: '/trading/gen2/factors/registry',
    method: 'get',
    params
  })
}

export function runGen2FactorTest(params) {
  return request({
    url: '/trading/gen2/factors/test',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function getGen2SelectionPool(params) {
  return request({
    url: '/trading/gen2/selection-pool',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function getGen2MainlineHotspots(params) {
  return request({
    url: '/trading/gen2/mainline-hotspots',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function runGen2MainlineHotspotsUpdate(params) {
  return request({
    url: '/trading/gen2/mainline-hotspots/update',
    method: 'post',
    params,
    timeout: 120000
  })
}

export function getGen2MainlineHotspotsUpdateTask(taskId) {
  return request({
    url: `/trading/gen2/mainline-hotspots/update-task/${taskId}`,
    method: 'get',
    timeout: 120000
  })
}

export function getGen2WorkflowStatus(params) {
  return request({
    url: '/trading/gen2/workflow/status',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function getGen2WorkflowLineage(params) {
  return request({
    url: '/trading/gen2/workflow/lineage',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function getGen2DailyTradeTicket(params) {
  return request({
    url: '/trading/gen2/daily-trade-ticket',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function getGen2DailyTradeExecution(params) {
  return request({
    url: '/trading/gen2/daily-trade-ticket/execution',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function saveGen2DailyTradeExecution(data) {
  return request({
    url: '/trading/gen2/daily-trade-ticket/execution',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function getGen2RiskCoolShadow(params) {
  return request({
    url: '/trading/gen2/risk-cool-shadow',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function runGen2RiskCoolShadowUpdate(params) {
  return request({
    url: '/trading/gen2/risk-cool-shadow/update',
    method: 'post',
    params,
    timeout: 120000
  })
}

export function getGen2RiskCoolShadowUpdateTask(taskId) {
  return request({
    url: `/trading/gen2/risk-cool-shadow/update-task/${taskId}`,
    method: 'get',
    timeout: 120000
  })
}

export function saveGen2ShadowVerification(data) {
  return request({
    url: '/trading/gen2/risk-cool-shadow/verification',
    method: 'post',
    data
  })
}

export function getGen2ShadowMonitorStatus() {
  return request({
    url: '/trading/gen2/shadow-monitor/status',
    method: 'get'
  })
}

export function setGen2ShadowMonitorConfig(data) {
  return request({
    url: '/trading/gen2/shadow-monitor/config',
    method: 'post',
    data
  })
}

export function runGen2ShadowMonitorNow(data) {
  return request({
    url: '/trading/gen2/shadow-monitor/run-once',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function saveGen2TradeClassification(data) {
  return request({
    url: '/trading/gen2/backtest/trade-classification',
    method: 'post',
    data
  })
}

export function submitGen2PaperOrder(data) {
  return request({
    url: '/trading/gen2/paper-order',
    method: 'post',
    data
  })
}

export function getV4ManualHoldingSignals(data) {
  return request({
    url: '/trading/v4/manual-holdings/signals',
    method: 'post',
    data
  })
}

export function getV4ManualHoldingQuotes(data) {
  return request({
    url: '/trading/v4/manual-holdings/quotes',
    method: 'post',
    data
  })
}

export function refreshV4ManualHoldingAll(data) {
  return request({
    url: '/trading/v4/manual-holdings/refresh-all',
    method: 'post',
    data
  })
}

export function saveV4ManualHoldingState(data) {
  return request({
    url: '/trading/v4/manual-holdings/state',
    method: 'post',
    data
  })
}

export function getV4ManualHoldingMonitorStatus() {
  return request({
    url: '/trading/v4/manual-holdings/monitor/status',
    method: 'get'
  })
}

export function setV4ManualHoldingMonitorConfig(data) {
  return request({
    url: '/trading/v4/manual-holdings/monitor/config',
    method: 'post',
    data
  })
}

export function runV4ManualHoldingMonitorNow(data) {
  return request({
    url: '/trading/v4/manual-holdings/monitor/run-once',
    method: 'post',
    data
  })
}

export function getV4ManualHoldingEntryBacktest(params) {
  return request({
    url: '/trading/v4/manual-holdings/entry-backtest',
    method: 'get',
    params
  })
}

export function readV4ManualHoldingThsCurrentTable() {
  return request({
    url: '/trading/v4/manual-holdings/ths-current-table',
    method: 'post'
  })
}

export function readV4ManualHoldingThsDeliveryFile() {
  return request({
    url: '/trading/v4/manual-holdings/ths-delivery-file',
    method: 'post'
  })
}

export function readV4ManualHoldingThsCapitalHoldings() {
  return request({
    url: '/trading/v4/manual-holdings/ths-capital-holdings',
    method: 'post',
    timeout: 120000
  })
}
