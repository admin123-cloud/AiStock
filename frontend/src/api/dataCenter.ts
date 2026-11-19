import request from '@/utils/request'

export interface TableStat {
  table: string
  label: string
  rows: number
  size_mb: number
  min_time: string | null
  max_time: string | null
}

export interface DataOverviewResponse {
  generated_at: string
  latest_daily_date: string | null
  summary: {
    active_stocks: number
    core_indices: number
    total_indices_in_catalog: number
    industry_sectors: number
    total_rows: number
    total_size_gb: number
  }
  tables: TableStat[]
}

export interface HealthMatrixCell {
  date: string
  actual: number
  expected: number
  coverage: number | null
  missing: number
  status: 'ok' | 'warn' | 'missing' | 'pending' | 'optional' | 'unknown'
}

export interface HealthMatrixDataset {
  id: string
  label: string
  note: string
  cells: HealthMatrixCell[]
}

export interface HealthMatrixResponse {
  generated_at: string
  days: number
  dates: string[]
  core_indices: string[]
  datasets: HealthMatrixDataset[]
}

export interface KlineInspectPeriod {
  period: string
  label: string
  expected_per_day: number
  total_bars: number
  min_time: string | null
  max_time: string | null
  recent_ok_days: number
  recent_total_days: number
  complete: boolean
}

export interface KlineInspectDayRow {
  date: string
  daily: number
  '5m': number
  '15m': number
  '30m': number
  '60m': number
  all_ok: boolean
}

export interface KlineInspectResponse {
  generated_at: string
  code: string
  name: string
  type: string
  days: number
  periods: KlineInspectPeriod[]
  daily_rows: KlineInspectDayRow[]
}

export const getDataOverview = (refresh = false) =>
  request.get<unknown, DataOverviewResponse>('/data-center/overview', {
    params: refresh ? { refresh: true } : undefined,
  })

export const getHealthMatrix = (days = 10, refresh = false) =>
  request.get<unknown, HealthMatrixResponse>('/data-center/health-matrix', {
    params: { days, ...(refresh ? { refresh: true } : {}) },
    timeout: 60000,
  })

export const inspectKline = (code: string, days = 10) =>
  request.get<unknown, KlineInspectResponse>('/data-center/kline-inspect', {
    params: { code, days },
    timeout: 30000,
  })
