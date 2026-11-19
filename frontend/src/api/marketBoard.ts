import request from '@/utils/request'

/**
 * R56 P1 市场看板三件套 API
 */

/** 1) 概念/行业热度榜 */
export function getSectorHeat(params?: {
  board_type?: 'concept' | 'industry' | 'region'
  sort_by?: string
  top_n?: number
}) {
  return request({
    url: '/market/sector-heat',
    method: 'get',
    params
  })
}

/** 成分股钻取 */
export function getSectorHeatStocks(code: string) {
  return request({
    url: `/market/sector-heat/${code}/stocks`,
    method: 'get'
  })
}

/** 2) 竞价异动查询 */
export function getAuctionAnomaly(params?: { date?: string; snap?: string }) {
  return request({
    url: '/market/auction-anomaly',
    method: 'get',
    params
  })
}

/** 3) 情绪雷达 + 综合评分 */
export function getSentimentRadar() {
  return request({
    url: '/market/sentiment-radar',
    method: 'get'
  })
}