import request from '@/utils/request'

/**
 * 检查K线数据完整性
 */
export function checkKlineIntegrity(data) {
  return request({
    url: '/kline-check/check',
    method: 'post',
    data
  })
}

/**
 * 获取检查进度
 */
export function getCheckProgress(taskId) {
  return request({
    url: `/kline-check/progress/${taskId}`,
    method: 'get'
  })
}

/**
 * 获取检查结果
 */
export function getCheckResults(taskId) {
  return request({
    url: `/kline-check/results/${taskId}`,
    method: 'get'
  })
}

/**
 * 一键全量巡检（所有类型+所有周期，按code聚合 TopN）
 */
export function checkKlineAllIntegrity(data) {
  return request({
    url: '/kline-check/check-all',
    method: 'post',
    data
  })
}

/**
 * 获取全量巡检结果
 */
export function getCheckAllResults(taskId) {
  return request({
    url: `/kline-check/results-all/${taskId}`,
    method: 'get'
  })
}

/**
 * 修复K线数据
 */
export function repairKlineData(data) {
  return request({
    url: '/kline-check/repair',
    method: 'post',
    data
  })
}

/**
 * 分批重拉修复
 */
export function repairKlineDataBatch(data) {
  return request({
    url: '/kline-check/repair-batch',
    method: 'post',
    data
  })
}

/**
 * One-click repair previous trade date data, then optionally refresh G3.
 */
export function repairPreviousTradeDateData(data) {
  return request({
    url: '/kline-check/repair-previous-trade-date',
    method: 'post',
    data
  })
}

export function getRepairProgress(taskId) {
  return request({
    url: `/kline-check/repair-progress/${taskId}`,
    method: 'get'
  })
}

export function getRepairResults(taskId) {
  return request({
    url: `/kline-check/repair-results/${taskId}`,
    method: 'get'
  })
}

/**
 * 获取历史检查记录
 */
export function getCheckHistory(params) {
  return request({
    url: '/kline-check/history',
    method: 'get',
    params
  })
}

/**
 * 获取历史检查记录详情
 */
export function getCheckHistoryDetail(checkId) {
  return request({
    url: `/kline-check/history/${checkId}`,
    method: 'get'
  })
}
