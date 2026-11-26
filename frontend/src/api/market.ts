import request from '@/utils/request'

/**
 * 分页获取板块列表
 */
/**
 * 获取指数列表（支持 code 精确查询）
 */
export function getIndices(params) {
  return request({
    url: '/market/indices',
    method: 'get',
    params
  })
}

export function getSectors(params, config = {}) {
  return request({
    url: '/sectors/',
    method: 'get',
    params,
    ...config
  })
}

/**
 * 获取板块统计信息
 */
export function getSectorsStats() {
  return request({
    url: '/sectors/stats',
    method: 'get'
  })
}

/**
 * 获取板块详情（含成分股）
 */
export function getSectorDetail(sectorCode) {
  return request({
    url: `/sectors/${sectorCode}`,
    method: 'get'
  })
}

/**
 * 获取板块历史涨跌统计（近15天）
 */
export function getSectorHistory(sectorCode) {
  return request({
    url: `/sectors/${sectorCode}/history`,
    method: 'get'
  })
}

/**
 * 获取板块K线数据
 */
export function getSectorKline(sectorCode) {
  return request({
    url: `/sectors/${sectorCode}/kline`,
    method: 'get'
  })
}

/**
 * 获取板块历史上涨跌统计（5/10/15日）
 */
export function getSectorHistoricalStats(sectorCode, config = {}) {
  return request({
    url: `/sectors/${sectorCode}/historical-stats`,
    method: 'get',
    ...config
  })
}

/** 获取市场涨跌与情绪快照 */
export function getMarketSentiment() {
  return request({ url: '/market/sentiment', method: 'get' })
}

/** 获取 30 日情绪周期走势 */
export function getMarketEmotionTrend(params?: Record<string, unknown>) {
  return request({ url: '/market/emotion-trend', method: 'get', params })
}

/** 获取盘后数据发布控制面状态 */
export function getMarketPublicationStatus() {
  return request({ url: '/market/publication-status', method: 'get' })
}
