import request from '@/utils/request'

/**
 * 运行回测
 */
export function runBacktest(data) {
  return request({
    url: '/backtest/run',
    method: 'post',
    data
  })
}

/**
 * 获取回测进度
 */
export function getBacktestProgress(taskId) {
  return request({
    url: `/backtest/progress/${taskId}`,
    method: 'get'
  })
}

/**
 * 获取回测结果
 */
export function getBacktestResult(taskId) {
  return request({
    url: `/backtest/result/${taskId}`,
    method: 'get'
  })
}

/**
 * 获取可用策略列表
 */
export function getAvailableStrategies() {
  return request({
    url: '/backtest/strategies',
    method: 'get'
  })
}
