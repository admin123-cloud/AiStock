import request from '@/utils/request'

/** 任务中心：任务看板 / 异常记录（axios 拦截器已解包 data，此处断言为具体负载） */
export const getTasks = () =>
  request.get('/operations/tasks') as unknown as Promise<OperationsBoard>
export const getIncidents = () =>
  request.get('/operations/incidents') as unknown as Promise<{ incidents: OperationsIncident[] }>

export interface OperationsTask {
  name: string
  title?: string
  purpose?: string
  produces?: string
  failure_impact?: string
  business_reason?: string
  consolidation_note?: string
  replacement?: string
  executor?: string
  status?: string
  business_status?: string
  scheduler_paused?: boolean
  running_seconds?: number | null
  last_run?: string
  next_run?: string
  last_result?: number | string | null
  schedule_description?: string
  window?: string
  last_success?: string
  last_finished?: string
  duration_seconds?: number | null
  runs?: number | null
  failures?: number | null
  last_schedule_warning?: string
  last_warning_at?: string
  error?: string
  group?: string
  ingestion?: {
    generated_at?: string
    phase?: string
    snapshot_target_seconds?: number
    lanes?: {
      name: string
      status?: string
      duration_seconds?: number | null
      running_seconds?: number | null
      error?: string
      result?: {
        universe_count?: number
        source_fresh_300s?: number
        errors?: Record<string, string>
      }
    }[]
  }
}

export interface OperationsIncident {
  key: string
  status?: string
  notification?: string
  recovery_notification?: string
  first_seen?: string
  deadline?: string
  attempts?: number
  error?: string
  detail?: { message?: string; reason?: string }
}

export interface OperationsBoard {
  generated_at?: string
  api_runtime?: {
    source?: string
    ready?: boolean
    fresh?: boolean
    pid?: number
    started_at?: string
    generated_at?: string
    startups?: { name: string; status?: string; error?: string; finished_at?: string }[]
  }
  operations_publisher?: {
    publisher_status?: string
    notifications_enabled?: boolean
    notification_transport_ok?: boolean
    generated_at?: string
    notification_transport?: { state?: string; last_smtp_accepted_at?: string }
    repair?: {
      status?: string
      held?: { key: string; status?: string }[]
      verified?: string[]
    }
  }
  host_inventory_status?: string
  host_inventory_at?: string
  weekly_audit?: {
    status?: string
    generated_at?: string
    days?: number
    problem_days?: number
    datasets?: { id: string; label?: string; checked_days?: number; problem_days?: number; missing_keys?: number }[]
  }
  historical_recovery?: {
    total?: number
    verified?: number
    rows?: number
    cutoff?: string
    incomplete_buckets?: number
    updated_at?: string
    record_stale?: boolean
    candidate_complete?: boolean
    pending?: { key: string; state?: string; error?: string }[]
  }
  summary?: {
    registered_windows?: number
    running?: number
    failed?: number
    retired?: number
  }
  groups?: { id: string; label?: string; count?: number; purpose?: string; attention?: number }[]
  tasks?: OperationsTask[]
  source_metrics?: {
    sources?: {
      source: string
      operation?: string
      requests?: number
      failures?: number
      timeouts?: number
      p95_seconds?: number | null
      received?: number | null
      requested?: number | null
      source_at?: string
      persisted_at?: string
      last_request_at?: string
    }[]
  }
}

const labels: Record<string, string> = {
  backup_status_unavailable_or_stale: '备份状态缺失或已过期',
  backup_execution_failed: '最近备份执行失败',
  backup_success_not_verified: '尚无完成备份的证据',
  backup_older_than_25_hours: '超过25小时没有完成新备份',
  restore_drill_not_verified: '尚无隔离恢复验证证据',
  restore_drill_older_than_7_days: '隔离恢复验证已超过7天',
  host_archive_not_verified: '宿主归档尚未验收',
  backup_execution_unknown: '备份执行状态未知',
  backup_reports_degraded: '备份生产者报告验收未通过',
  isolated_restore_drill_required: '需要隔离恢复验证',

  budget_exhausted: '自动修复批次预算已用尽',
  no_progress_requires_review: '连续复验无进展 · 已停修',
  requires_review: '执行结果需人工核实 · 已停修',
  awaiting_job: '等待队列任务结束',
  awaiting_fresh_verification: '等待任务结束后的独立复验',
  configured_unverified: '已配置，发送尚未验收',
  configuration_incomplete: '通知配置不完整',
  failed_or_uncertain: '发送失败或结果不确定',
  queued: '已排队',
  awaiting_verification: '等待独立复验',
  not_requested: '尚未启用',
  not_required: '无需通知',
  retired: '已退役',
  external: '外部联动',
  ready: '启动成功',
  not_ready: '尚未就绪',
  starting: '启动中',
  stopped: '已停止',
  healthy: '正常',
  blocked: '阻断',
  degraded: '降级',
  stale: '已过期',
  deferred: '休市待刷新',
  unknown: '状态未知',
  not_observed: '未发现实例',
  disabled: '已停用',
  configured: '已配置 · 待核在线',
  running: '运行中',
  failed: '失败',
  idle: '待下次运行',
  never_run: '尚未运行',
  unverified: '待验收',
  not_due: '尚未到期',
  complete: '覆盖齐全',
  partial: '部分缺失',
  missing: '缺失',
  observing: '恢复窗口内',
  overdue: '恢复已超时',
  resolved: '已恢复',
  waiting: '等待恢复',
  uncertain: '发送结果不确定 · 需人工核实',
  smtp_accepted: 'SMTP已接受',
  sending: '发送中',
  data_blocked: '数据不可判断',
  confirmed: '已完成30m确认',
  waiting_30m: '等待30m确认',
  candidates: '候选跟踪中',
  no_candidates: '本批次无候选',
  stale_batch: '历史批次',
  candidate_observing: '门槛未达 · 继续观察',
}

export const statusLabel = (value: string): string => labels[value] || value || '未知'

export type StatusTone = 'danger' | 'success' | 'warning'

export const statusTone = (value: string): StatusTone =>
  ['blocked', 'failed', 'missing', 'overdue', 'data_blocked'].includes(value)
    ? 'danger'
    : ['healthy', 'complete', 'resolved', 'confirmed'].includes(value)
      ? 'success'
      : 'warning'

export const dateTime = (value?: string): string =>
  value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '暂无记录'

const reasons: Record<string, string> = {
  institutional_30m_source_conflict: '30分钟主表与阶段表存在来源冲突',
  data_gap_30m: '30分钟数据缺口，验收未通过',
  data_repair_failed: '自动数据修复未通过',
  accepted: '当日验收通过',
}

export const reasonText = (value?: string | null): string =>
  (value && reasons[value] ? `${value}（${reasons[value]}）` : value) || '未提供原因'
