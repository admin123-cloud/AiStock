import request from '@/utils/request'

export type KlineCheckRow = {
  data_type?: string
  code?: string
  name?: string
  score?: number
  worst_period?: string
  complete_periods?: string[]
  pending_confirm_periods?: string[]
  incomplete_periods?: string[]
  error_periods?: string[]
  period_stats?: Record<string, { status?: string }>
}

export type KlineCheckSummary = {
  total_codes?: number
  complete_codes?: number
  pending_confirm_codes?: number
  actionable_problem_codes?: number
  no_data_codes?: number
}

export type KlineCheckResult = { items?: KlineCheckRow[]; summary?: KlineCheckSummary }

export type KlineProgress = {
  status?: string
  total?: number
  processed?: number
  complete?: number
  pending_confirm?: number
  incomplete?: number
  no_data?: number
  error?: number
  message?: string
}

export type KlineRepairItem = {
  data_type?: string
  code?: string
  name?: string
  period?: string
  success?: boolean
  error?: string
  improved?: boolean
  became_complete?: boolean
}

export type KlineRepairResult = {
  items?: KlineRepairItem[]
  summary?: { total?: number; success?: number; failed?: number; improved?: number; became_complete?: number }
  generated_at?: string
}

export type KlineHistoryRecord = Record<string, unknown> & { id?: number | string }

export function startKlineCheckAll(data: { limit_codes: number; start_base_date: string }) {
  return request.post('/kline-check/check-all', data) as unknown as Promise<{ task_id: string }>
}

export function getKlineCheckProgress(taskId: string) {
  return request.get(`/kline-check/progress/${taskId}`) as unknown as Promise<KlineProgress>
}

export function getKlineCheckResults(taskId: string) {
  return request.get(`/kline-check/results-all/${taskId}`) as unknown as Promise<KlineCheckResult>
}

export function repairKlineBatch(data: { items: Array<{ code: string; data_type: string; periods: string[] }>; start_date: string }) {
  return request.post('/kline-check/repair-batch', data) as unknown as Promise<{ task_id: string }>
}

export function getKlineRepairProgress(taskId: string) {
  return request.get(`/kline-check/repair-progress/${taskId}`) as unknown as Promise<KlineProgress & { success?: number; failed?: number; current?: string; error?: string }>
}

export function getKlineRepairResults(taskId: string) {
  return request.get(`/kline-check/repair-results/${taskId}`) as unknown as Promise<KlineRepairResult>
}

export function getKlineCheckHistory(params: { page: number; page_size: number; data_type?: string; period?: string }) {
  return request.get('/kline-check/history', { params }) as unknown as Promise<{ items?: KlineHistoryRecord[]; total?: number }>
}

export function getKlineCheckHistoryDetail(checkId: number | string) {
  return request.get(`/kline-check/history/${checkId}`) as unknown as Promise<KlineHistoryRecord>
}

export function repairKlineData(data: { code: string; data_type: string; period: string; start_date: string }) {
  return request.post('/kline-check/repair', data) as unknown as Promise<{ task_id?: string }>
}
