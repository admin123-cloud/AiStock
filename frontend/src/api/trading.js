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

export function getGen3RouteExecutionV3(params) {
  return request({
    url: '/gen3-shadow/route-execution-v3',
    method: 'get',
    params,
    timeout: 300000
  })
}

export function getGen3RouteExecutionV3Backtest(params) {
  return request({
    url: '/gen3-shadow/route-execution-v3/backtest',
    method: 'get',
    params,
    timeout: 300000
  })
}

export function getGen3V4ResearchBacktest(params) {
  return request({
    url: '/gen3-shadow/v4-research/backtest',
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
