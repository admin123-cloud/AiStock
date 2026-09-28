import request from '@/utils/request'

/**
 * 获取股票列表
 */
export function getStockList(params) {
  return request({
    url: '/stocks',
    method: 'get',
    params
  })
}

/**
 * 分页获取股票列表（含最新K线行情）
 */
export function getStocksWithLimit(params, config = {}) {
  return request({
    url: '/stocks/with-limit',
    method: 'get',
    params,
    ...config
  })
}

/**
 * 获取股票详情
 */
export function getStockDetail(code, config = {}) {
  return request({
    url: `/stocks/${code}`,
    method: 'get',
    ...config
  })
}

/**
 * 获取股票行情
 */
export function getStockQuote(code) {
  return request({
    url: `/stocks/${code}/quote`,
    method: 'get'
  })
}

/**
 * 获取股票历史数据
 */
export function getStockHistory(code, params, config = {}) {
  return request({
    url: `/stocks/${code}/history`,
    method: 'get',
    params,
    ...config
  })
}

/**
 * 获取股票 K 线数据（period: 15m/30m/60m/1d/1w/1mon）
 */
export function getStockKline(code, period, params) {
  return request({
    url: `/stocks/${code}/kline/${period}`,
    method: 'get',
    params
  })
}

/**
 * 搜索股票
 */
export function searchStocks(keyword) {
  return request({
    url: '/stocks/search',
    method: 'get',
    params: { keyword }
  })
}
