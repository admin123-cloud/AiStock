import request from '@/utils/request'

export function syncClsNews(params) {
  return request({
    url: '/news/cls/sync',
    method: 'post',
    params,
    timeout: 120000
  })
}

export function getClsNewsStatus() {
  return request({
    url: '/news/cls/status',
    method: 'get'
  })
}

export function getClsNewsSignals(params) {
  return request({
    url: '/news/cls/signals',
    method: 'get',
    params
  })
}

export function getClsRadarSummary(params) {
  return request({
    url: '/news/cls/radar-summary',
    method: 'get',
    params
  })
}

export function getClsLatestNews(params) {
  return request({
    url: '/news/cls/latest',
    method: 'get',
    params
  })
}

export function getClsEventCandidates(eventId, params) {
  return request({
    url: `/news/cls/event/${eventId}/candidates`,
    method: 'get',
    params
  })
}
