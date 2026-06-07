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
 * 搜索股票
 */
export function searchStocks(keyword) {
  return request({
    url: '/stocks/search',
    method: 'get',
    params: { keyword }
  })
}
