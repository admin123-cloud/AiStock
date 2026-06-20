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

export function getGen3StateAlphaBrokerHoldings(params) {
  return request({
    url: '/gen3-state-alpha/broker/holdings',
    method: 'get',
    params,
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

export function submitGen3StateAlphaPtradeBuyOrder(data) {
  return request({
    url: '/gen3-state-alpha/ptrade/buy-order',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function submitGen3StateAlphaPtradeSellOrder(data) {
  return request({
    url: '/gen3-state-alpha/ptrade/sell-order',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function getGen3StateAlphaPtradeStatus(params) {
  return request({
    url: '/gen3-state-alpha/ptrade/status',
    method: 'get',
    params,
    timeout: 120000
  })
}

export function runGen3StateAlphaPtradeE2eAcceptance(data) {
  return request({
    url: '/gen3-state-alpha/ptrade/e2e-acceptance',
    method: 'post',
    data,
    timeout: 120000
  })
}

export function runGen3StateAlphaPtradeInternalStrategyAcceptance(data) {
  return request({
    url: '/gen3-state-alpha/ptrade/internal-strategy-acceptance',
    method: 'post',
    data,
    timeout: 120000
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

export function getPtradeBridgeStatus() {
  return request({
    url: '/trading/ptrade/bridge/status',
    method: 'get'
  })
}

export function getPtradeBridgeReadinessAudit(params) {
  return request({
    url: '/trading/ptrade/bridge/readiness-audit',
    method: 'get',
    params
  })
}

export function getPtradeBridgeEvidenceReport() {
  return request({
    url: '/trading/ptrade/bridge/evidence-report',
    method: 'get'
  })
}

export function savePtradeBridgeConfig(data) {
  return request({
    url: '/trading/ptrade/bridge/config',
    method: 'post',
    data
  })
}

export function runPtradeBridgeLiveProbe(data) {
  return request({
    url: '/trading/ptrade/bridge/live-probe',
    method: 'post',
    data,
    timeout: 70000
  })
}

export function runPtradeBridgeAcceptance(data) {
  return request({
    url: '/trading/ptrade/bridge/acceptance',
    method: 'post',
    data,
    timeout: 200000
  })
}

export function startPtradeBridgeAcceptance(data) {
  return request({
    url: '/trading/ptrade/bridge/acceptance/start',
    method: 'post',
    data
  })
}

export function getPtradeBridgeAcceptanceTask(taskId) {
  return request({
    url: `/trading/ptrade/bridge/acceptance-task/${taskId}`,
    method: 'get'
  })
}

export function startPtradeBridgeWatchAcceptance(data) {
  return request({
    url: '/trading/ptrade/bridge/watch-acceptance/start',
    method: 'post',
    data
  })
}

export function getPtradeBridgeWatchAcceptanceTask(taskId) {
  return request({
    url: `/trading/ptrade/bridge/watch-acceptance-task/${taskId}`,
    method: 'get'
  })
}

export function startPtradeBridgeLiveSubmitTest(data) {
  return request({
    url: '/trading/ptrade/bridge/live-submit-test/start',
    method: 'post',
    data
  })
}

export function getPtradeBridgeLiveSubmitTestTask(taskId) {
  return request({
    url: `/trading/ptrade/bridge/live-submit-test-task/${taskId}`,
    method: 'get'
  })
}

export function recoverStalePtradeBridgeProcessing(data) {
  return request({
    url: '/trading/ptrade/bridge/processing/recover-stale',
    method: 'post',
    data
  })
}

export function listPtradeBridgeOrders(params) {
  return request({
    url: '/trading/ptrade/bridge/orders',
    method: 'get',
    params
  })
}

export function listPtradeBridgeFills(params) {
  return request({
    url: '/trading/ptrade/bridge/fills',
    method: 'get',
    params
  })
}

export function getPtradeBridgePositions(params) {
  return request({
    url: '/trading/ptrade/bridge/positions',
    method: 'get',
    params
  })
}

export function submitPtradeBridgeOrder(data) {
  return request({
    url: '/trading/ptrade/bridge/orders',
    method: 'post',
    data
  })
}

export function cancelPtradeBridgeOrder(orderId, data) {
  return request({
    url: `/trading/ptrade/bridge/orders/${orderId}/cancel`,
    method: 'post',
    data
  })
}
