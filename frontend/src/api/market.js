import request from '@/utils/request'

export function getLimitUpLadder(params) {
  return request({
    url: '/market/limit-up-ladder',
    method: 'get',
    params
  })
}
