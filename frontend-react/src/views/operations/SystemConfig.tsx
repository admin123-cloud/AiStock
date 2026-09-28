import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Input } from '@/components/catalyst/input'
import { Select } from '@/components/catalyst/select'
import { useToast } from '@/components/toast'
import { Card, EmptyState, PageHeader, SectionTitle } from '@/components/ui'
import request from '@/utils/request'

// ---------- 类型 ----------
interface TaskState {
  status?: string
  message?: string
  last_success_at?: string | null
  updated_at?: string
  task_id?: string
}
interface CoreMaintenance {
  enabled: boolean
  overall_status: string
  today_success_rate: number | null
  latest_failed_task: string | null
  running_task: string | null
  jobs: Record<string, { enabled?: boolean; next_run_time?: string }>
  task_status: Record<string, Record<string, unknown> & { is_running?: boolean; error?: string; last_success_at?: string; results?: { message?: string } | null; progress?: { message?: string } }>
  task_status_normalized: Record<string, { status?: string; display_message?: string; last_success_at?: string }>
  timeline: TimelineItem[]
}
interface TimelineItem {
  task_name: string
  status?: string
  message?: string
  duration_sec?: number | null
  created_at?: string
}
interface RuntimeComponent {
  name: string
  status?: string
  age_seconds?: number | null
  remediation_owner?: string
}
interface RuntimeHealth {
  status: string
  strategy_actionable: boolean
  components: RuntimeComponent[]
}
interface QmtPeriod {
  period: string
  complete_codes?: number
  expected_codes?: number
  issue_codes?: string
  reason_counts?: Record<string, number>
}
interface QmtAfterClose {
  available?: boolean
  closed?: boolean
  schedule?: { start?: string; retry_minutes?: number; active_end?: string }
  trade_date?: string
  remaining_period_code_records?: number
  initial_period_code_records?: number
  initial_unique_codes?: number
  periods?: QmtPeriod[]
  overnight_repair?: {
    available?: boolean
    execution_ok?: boolean
    validation_closed?: boolean
    schedule?: { start?: string; active_end?: string }
    trade_dates?: string[]
    updated_at?: string
    remaining_issue_count?: number
    execution_message?: string
  }
}
interface DataSourcePeriod {
  period: string
  complete?: boolean
  row_count?: number
  expected_row_count?: number
  coverage_rate?: number | null
  missing_rows?: number
  extra_rows?: number
  extended_rows?: number
}
interface DataSourceRow {
  key: string
  label?: string
  ok?: boolean
  complete?: boolean
  verification_status?: string
  code_count?: number
  row_count?: number
  coverage_rate?: number | null
  expected_row_count?: number
  missing_rows?: number
  extra_rows?: number
  extended_rows?: number
  latest_date?: string
  periods?: DataSourcePeriod[]
}
interface DataSourceCounts {
  trade_date: string
  latest_available_date: string | null
  checked_at: string | null
  summary?: { incomplete?: number; missing?: number }
  rows: DataSourceRow[]
}
interface StrategyTask {
  key: string
  label: string
  mode: 'daily' | 'heavy'
  description: string
}

const STRATEGY_SUCCESS_STORAGE_KEY = 'aistock.strategyMaintenance.lastSuccess.v1'

const strategyMaintenanceTasks: StrategyTask[] = [
  { key: 'g3_state_alpha_refresh', label: 'G3 State Alpha刷新', mode: 'daily', description: '刷新第三代市场状态Alpha影子工作流，生成当日候选、影子票据和台账。' },
  { key: 'g3_shadow_monitor', label: 'G3影子信号监控', mode: 'daily', description: '手动运行G3影子监控与通知链路，检查阻断、心跳和候选变化。' },
  { key: 'g3_workflow_status', label: 'G3信号链路自检', mode: 'daily', description: '检查G3运行链路、候选池、30m确认、风控合同和正式交易闸门。' },
  { key: 'g3_current_shadow', label: 'G3今日影子票据', mode: 'daily', description: '读取G3今日影子买入票据、候选路由和影子台账可用性。' },
  { key: 'g3_historical_trades', label: 'G3历史成交复核', mode: 'heavy', description: '复核G3 State Alpha历史成交、权益曲线、路线收益和未来函数审计。' },
  { key: 'g3_evidence_inventory', label: 'G3证据归档复核', mode: 'heavy', description: '检查G3研究证据归档、蓝图报告和清理清单是否仍可读取。' },
  { key: 'g3_monitor_status', label: 'G3调度监控', mode: 'daily', description: '检查G3 shadow monitor调度器、邮件、心跳和下一次运行时间。' },
  { key: 'g3_current_readiness', label: 'G3当前准入检查', mode: 'daily', description: '读取G3当前工作台数据，确认shadow-only、observe-only和正式交易锁。' },
]

const klinePeriods = [
  { value: '1m', label: '1分钟' }, { value: '5m', label: '5分钟' }, { value: '15m', label: '15分钟' },
  { value: '30m', label: '30分钟' }, { value: '60m', label: '60分钟' }, { value: '1d', label: '日线' },
  { value: '1w', label: '周线' }, { value: '1mon', label: '月线' }, { value: '1q', label: '季线' }, { value: '1y', label: '年线' },
]

const autoRepairDataScopes = [
  { value: 'minute_history', label: '股票/指数历史分钟线' },
  { value: 'index_history', label: '指数历史K线' },
  { value: 'daily_stock', label: '股票日线修复' },
  { value: 'all_stock_history', label: '股票全历史K线' },
  { value: 'emotion_cycle', label: '情绪周期重算' },
]
const autoRepairDayOptions = [
  { value: 2, label: '近2个交易日' }, { value: 5, label: '近5个交易日' }, { value: 10, label: '近10个交易日' },
  { value: 30, label: '近30个交易日' }, { value: 60, label: '近60个交易日' }, { value: 120, label: '近120个交易日' },
]
const autoRepairPeriodOptions = [
  { value: '5m', label: '5分钟' }, { value: '15m', label: '15分钟' }, { value: '30m', label: '30分钟' },
  { value: '60m', label: '60分钟' }, { value: '1d', label: '日线' }, { value: '1w', label: '周线' }, { value: '1mon', label: '月线' },
]

const taskAliasMap: Record<string, string> = {
  update_trade_calendar: '交易日历同步', update_stock_list: '股票列表同步', update_index_list: '指数列表同步',
  sync_sectors: '板块列表同步', update_stock_today_data: '盘中股票/指数日线快照', update_today_data: '盘中指数分钟级快照',
  update_market_today_minute_data: '盘中股票/指数分钟级快照', sync_today_intraday_kline: '当天15/30m分钟K线落库',
  minute_kline_daily_repair_validate: '分钟K线补全验证', market_minute_history_repair: '股票/指数历史分钟级修复',
  today_full_market_refresh: '当天全市场更新自检', update_sector_intraday_stats: '板块当日统计刷新',
  official_daily_close_sync: '盘后正式日线写库', repair_previous_daily_kline: '次日自动巡检修复',
  core_data_maintenance: '核心数据自动维护',
}
const getTaskAlias = (name?: string) => taskAliasMap[name || ''] || name || '-'

// ---------- 工具 ----------
const errDetail = (e: unknown) =>
  (e as { response?: { data?: { detail?: string } }; message?: string })?.response?.data?.detail ||
  (e as Error)?.message || '请求失败'

const formatDateTime = (value?: string | null) => {
  if (!value) return '-'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return String(value)
  return d.toLocaleString('zh-CN', { hour12: false })
}
const formatRate = (value: number | null) => (value == null ? '暂无执行样本' : `${Number(value).toFixed(2)}%`)
const formatInteger = (value: number | null | undefined) => {
  const n = Number(value)
  if (!Number.isFinite(n)) return '-'
  return n.toLocaleString('zh-CN')
}
const formatCoverage = (value: number | null | undefined) => {
  if (value == null) return '待验收'
  const n = Number(value)
  if (!Number.isFinite(n)) return '-'
  return `${(n * 100).toFixed(2)}%`
}
const formatDuration = (valueSec?: number | null) => {
  const sec = Number(valueSec)
  if (!Number.isFinite(sec) || sec < 0) return '-'
  if (sec < 1) return `${Math.round(sec * 1000)}ms`
  if (sec < 60) return `${sec.toFixed(2)}s`
  const min = Math.floor(sec / 60)
  return `${min}m ${(sec - min * 60).toFixed(1)}s`
}

const getStateText = (status?: string) =>
  ({ running: '运行中', success: '成功', failed: '失败', paused: '已暂停', idle: '待执行' })[status || ''] || '待执行'

const normalizeStrategyStatus = (status: unknown, fallback = 'idle') => {
  const value = String(status || '').toLowerCase()
  if (['running', 'queued', 'pending'].includes(value)) return 'running'
  if (['completed', 'success', 'ok', 'passed'].includes(value)) return 'success'
  if (['failed', 'error', 'missing'].includes(value)) return 'failed'
  if (value === 'paused' || value === 'disabled') return 'paused'
  return fallback
}

const strategyTaskMessageFromResult = (payload: Record<string, any> | null | undefined): string => {
  if (!payload || typeof payload !== 'object') return ''
  if (payload.message) return String(payload.message)
  if (payload.error) return String(payload.error)
  if (payload.reason) return String(payload.reason)
  if (payload.last_error) return String(payload.last_error)
  if (payload.last_result?.reason) return String(payload.last_result.reason)
  if (payload.last_result?.message) return String(payload.last_result.message)
  return ''
}

const stateColor = (status?: string) =>
  status === 'running' ? 'text-blue-600 dark:text-blue-400'
    : status === 'success' ? 'text-emerald-600 dark:text-emerald-400'
      : status === 'failed' ? 'text-red-600 dark:text-red-400'
        : status === 'paused' ? 'text-amber-600 dark:text-amber-400'
          : 'text-zinc-500 dark:text-zinc-400'

const runtimeStatusTextMap: Record<string, string> = {
  healthy: '健康', deferred: '休市待刷新', degraded: '降级', blocked: '阻断', loading: '加载中',
}
const runtimeComponentLabel = (name?: string) =>
  ({
    daily_kline_coverage: '日线交易日覆盖', qmt_after_close_validation: 'QMT分钟数据闭环',
    g3_strategy_summary: 'G3策略摘要', broker_snapshot: '账户快照',
  })[name || ''] || name || '未命名组件'
const runtimeComponentStatusText = (item: RuntimeComponent) => {
  const status = ({ healthy: '正常', deferred: '休市待刷新', stale: '已过期', blocked: '已阻断' })[item?.status || ''] || '未知'
  return item?.age_seconds == null ? status : `${status}，${Math.round(item.age_seconds / 60)}分钟前更新`
}

// ---------- 组件 ----------
export default function SystemConfig() {
  const toast = useToast()

  const [coreMaintenance, setCoreMaintenance] = useState<CoreMaintenance>({
    enabled: false, overall_status: 'idle', today_success_rate: null, latest_failed_task: null,
    running_task: null, jobs: {}, task_status: {}, task_status_normalized: {}, timeline: [],
  })
  const [runtimeHealth, setRuntimeHealth] = useState<RuntimeHealth>({ status: 'loading', strategy_actionable: false, components: [] })
  const [qmtAfterClose, setQmtAfterClose] = useState<QmtAfterClose>({ available: false, schedule: {} })
  const [lastStatusRefreshAt, setLastStatusRefreshAt] = useState<string | null>(null)
  const [showTimelinePanel, setShowTimelinePanel] = useState(false)

  // 数据源统计
  const [dataSourceCountsLoading, setDataSourceCountsLoading] = useState(false)
  const [dataSourceRepairing, setDataSourceRepairing] = useState(false)
  const [dataSourceCountDate, setDataSourceCountDate] = useState('')
  const [dataSourceCounts, setDataSourceCounts] = useState<DataSourceCounts>({
    trade_date: '', latest_available_date: null, checked_at: null, summary: {}, rows: [],
  })

  // 策略维护
  const [strategyTaskStates, setStrategyTaskStates] = useState<Record<string, TaskState>>({})
  const [strategyTaskSyncing, setStrategyTaskSyncing] = useState<Record<string, boolean>>({})
  const [runningAllStrategyMaintenanceNow, setRunningAllStrategyMaintenanceNow] = useState(false)
  const pollTimersRef = useRef<Record<string, ReturnType<typeof setTimeout> | null>>({})

  // 启动初始化
  const [startupReferenceSyncEnabled, setStartupReferenceSyncEnabled] = useState(false)
  const [startupReferenceSyncDefaultEnabled, setStartupReferenceSyncDefaultEnabled] = useState(false)
  const [startupReferenceSyncSaving, setStartupReferenceSyncSaving] = useState(false)

  // 高级维护
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [updatingIndices, setUpdatingIndices] = useState(false)
  const [syncingSectors, setSyncingSectors] = useState(false)
  const [syncingSectorHistory, setSyncingSectorHistory] = useState(false)
  const [updatingSectorIntradayStats, setUpdatingSectorIntradayStats] = useState(false)
  const [repairingHistoryKlines, setRepairingHistoryKlines] = useState(false)
  const [repairingDailyKlines, setRepairingDailyKlines] = useState(false)
  const [repairingAllHistoryKlines, setRepairingAllHistoryKlines] = useState(false)
  const [updatingTodayData, setUpdatingTodayData] = useState(false)
  const [updatingStockTodayData, setUpdatingStockTodayData] = useState(false)
  const [updatingTradeCalendar, setUpdatingTradeCalendar] = useState(false)
  const [repairingEmotion30d, setRepairingEmotion30d] = useState(false)
  const [repairingEmotionLatest, setRepairingEmotionLatest] = useState(false)
  const [emotionAutoEnabled, setEmotionAutoEnabled] = useState(false)
  const [runningCoreAssetsSyncNow, setRunningCoreAssetsSyncNow] = useState(false)
  const [runningTodayFullMarketRefreshNow, setRunningTodayFullMarketRefreshNow] = useState(false)
  const [runningMarketMinuteHistoryRepairNow, setRunningMarketMinuteHistoryRepairNow] = useState(false)
  const [autoRepairRunning, setAutoRepairRunning] = useState(false)
  const [syncHistoryDays, setSyncHistoryDays] = useState(30)
  const [selectedPeriods, setSelectedPeriods] = useState<string[]>(['1d', '1w', '1mon'])
  const [selectedUpdatePeriods, setSelectedUpdatePeriods] = useState<string[]>(['5m', '15m', '30m', '60m', '1d'])
  const [selectedStockUpdatePeriods, setSelectedStockUpdatePeriods] = useState<string[]>(['5m', '15m', '30m', '60m', '1d'])
  const [autoRepairCategory, setAutoRepairCategory] = useState<'data' | 'strategy'>('data')
  const [autoRepairDataScope, setAutoRepairDataScope] = useState('minute_history')
  const [autoRepairDays, setAutoRepairDays] = useState(30)
  const [autoRepairPeriods, setAutoRepairPeriods] = useState<string[]>(['5m', '15m', '30m', '60m'])
  const [autoRepairStrategyMode, setAutoRepairStrategyMode] = useState('daily')

  const showTimelinePanelRef = useRef(showTimelinePanel)
  showTimelinePanelRef.current = showTimelinePanel

  // ---- 策略状态辅助 ----
  const loadStrategySuccessMemory = useCallback(() => {
    try {
      const raw = localStorage.getItem(STRATEGY_SUCCESS_STORAGE_KEY)
      const data = raw ? JSON.parse(raw) : {}
      Object.entries(data || {}).forEach(([taskKey, lastSuccessAt]) => {
        if (lastSuccessAt) setStrategyTaskStates((prev) => ({ ...prev, [taskKey]: { ...prev[taskKey], last_success_at: lastSuccessAt as string } }))
      })
    } catch { /* ignore */ }
  }, [])

  const setStrategyTaskState = useCallback((taskKey: string, patch: TaskState) => {
    setStrategyTaskStates((prev) => {
      const previousState = prev[taskKey] || {}
      const nextState = { ...previousState, ...patch }
      const rememberedSuccessAt =
        nextState.last_success_at ||
        previousState.last_success_at ||
        (normalizeStrategyStatus(nextState.status) === 'success' ? nextState.updated_at || new Date().toISOString() : null)
      if (rememberedSuccessAt) nextState.last_success_at = rememberedSuccessAt
      if (rememberedSuccessAt) {
        try {
          const raw = localStorage.getItem(STRATEGY_SUCCESS_STORAGE_KEY)
          const data = raw ? JSON.parse(raw) : {}
          data[taskKey] = rememberedSuccessAt
          localStorage.setItem(STRATEGY_SUCCESS_STORAGE_KEY, JSON.stringify(data))
        } catch { /* ignore */ }
      }
      return { ...prev, [taskKey]: nextState }
    })
  }, [])

  const setStrategyTaskSyncingState = useCallback((taskKey: string, syncing: boolean) => {
    setStrategyTaskSyncing((prev) => ({ ...prev, [taskKey]: syncing }))
  }, [])

  const getStrategyTaskStatus = useCallback(
    (taskKey: string) => (strategyTaskSyncing[taskKey] ? 'running' : normalizeStrategyStatus(strategyTaskStates[taskKey]?.status, 'idle')),
    [strategyTaskSyncing, strategyTaskStates]
  )

  const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

  const clearStrategyTaskPollTimer = (taskKey: string) => {
    const timer = pollTimersRef.current[taskKey]
    if (timer) clearTimeout(timer)
    pollTimersRef.current = { ...pollTimersRef.current, [taskKey]: null }
  }

  const pollStrategyAsyncTask = useCallback(
    async (task: StrategyTask, taskId: string, statusUrl: string, options?: { scheduleNext?: boolean; showToast?: boolean }) => {
      const { scheduleNext = true, showToast = true } = options || {}
      clearStrategyTaskPollTimer(task.key)
      try {
        const payload = (await request.get(statusUrl)) as Record<string, any>
        const rawStatus = payload.status || payload.task?.status
        const status = normalizeStrategyStatus(rawStatus)
        const statePatch: TaskState = {
          status,
          task_id: taskId,
          message: strategyTaskMessageFromResult(payload) || `${task.label} ${status === 'running' ? '执行中' : '已更新'}`,
          updated_at: payload.updated_at || payload.finished_at || payload.task?.updated_at || new Date().toISOString(),
        }
        if (status === 'success') {
          statePatch.last_success_at = payload.finished_at || payload.updated_at || payload.task?.updated_at || new Date().toISOString()
        }
        setStrategyTaskState(task.key, statePatch)
        if (status === 'running') {
          if (scheduleNext) {
            pollTimersRef.current = {
              ...pollTimersRef.current,
              [task.key]: setTimeout(() => void pollStrategyAsyncTask(task, taskId, statusUrl), 3000),
            }
          }
          return status
        }
        setStrategyTaskSyncingState(task.key, false)
        if (status === 'failed') {
          if (showToast) toast.error(`${task.label}失败: ${strategyTaskMessageFromResult(payload) || rawStatus || '未知错误'}`)
        } else if (showToast) {
          toast.success(`${task.label}完成`)
        }
        return status
      } catch (error) {
        setStrategyTaskSyncingState(task.key, false)
        setStrategyTaskState(task.key, {
          status: 'failed',
          message: errDetail(error) || '查询任务状态失败',
          updated_at: new Date().toISOString(),
        })
        if (showToast) toast.error(`${task.label}状态查询失败`)
        return 'failed'
      }
    },
    [setStrategyTaskState, setStrategyTaskSyncingState, toast]
  )

  const waitStrategyAsyncTask = async (task: StrategyTask, taskId: string, statusUrl: string) => {
    let status = 'running'
    while (status === 'running') {
      status = await pollStrategyAsyncTask(task, taskId, statusUrl, { scheduleNext: false, showToast: false })
      if (status === 'running') await sleep(3000)
    }
    return status
  }

  // ---- 状态刷新 ----
  const refreshCoreMaintenanceStatus = useCallback(async () => {
    try {
      const body = (await request.get('/system/core-data-maintenance/status')) as { data?: Record<string, any> }
      const payload = body?.data || {}
      setCoreMaintenance({
        enabled: !!payload.enabled || !!payload.is_enabled,
        overall_status: payload.overall_status || 'idle',
        today_success_rate: payload.today_success_rate ?? null,
        latest_failed_task: payload.latest_failed_task || null,
        running_task: payload.running_task || null,
        jobs: payload.jobs || {},
        task_status: payload.task_status || {},
        task_status_normalized: payload.task_status_normalized || {},
        timeline: payload.timeline || [],
      })
      setLastStatusRefreshAt(new Date().toISOString())
    } catch {
      toast.error('获取核心自动维护状态失败')
    }
  }, [toast])

  const refreshRuntimeHealth = useCallback(async () => {
    try {
      const payload = (await request.get('/health/runtime')) as Record<string, any>
      setRuntimeHealth({
        status: payload.status || 'blocked',
        strategy_actionable: !!payload.strategy_actionable,
        components: Array.isArray(payload.components) ? payload.components : [],
      })
    } catch {
      setRuntimeHealth({ status: 'blocked', strategy_actionable: false, components: [] })
    }
  }, [])

  const refreshQmtAfterCloseExecution = useCallback(async () => {
    try {
      const body = (await request.get('/system/qmt-after-close-execution')) as { data?: QmtAfterClose }
      setQmtAfterClose(body?.data || { available: false, schedule: {} })
    } catch {
      setQmtAfterClose({ available: false, schedule: {} })
    }
  }, [])

  const refreshTimeline = useCallback(async () => {
    try {
      const body = (await request.get('/system/core-data-maintenance/timeline', { params: { limit: 30 } })) as {
        success?: boolean
        data?: { timeline?: TimelineItem[] }
      }
      if (body?.success) {
        setCoreMaintenance((prev) => ({ ...prev, timeline: body.data?.timeline || [] }))
      }
    } catch {
      toast.error('获取任务时间线失败')
    }
  }, [toast])

  const refreshDataSourceCounts = useCallback(async () => {
    setDataSourceCountsLoading(true)
    try {
      const body = (await request.get('/system/data-source-counts', {
        params: dataSourceCountDate ? { trade_date: dataSourceCountDate } : undefined,
      })) as { data?: DataSourceCounts }
      setDataSourceCounts(
        body?.data || { trade_date: dataSourceCountDate, latest_available_date: null, checked_at: null, summary: {}, rows: [] }
      )
      if (body?.data?.trade_date) setDataSourceCountDate(body.data.trade_date)
    } catch (error) {
      toast.error(errDetail(error))
    } finally {
      setDataSourceCountsLoading(false)
    }
  }, [dataSourceCountDate, toast])

  const refreshStrategyMaintenanceStatus = useCallback(async () => {
    const statusRequests: { key: string; url: string; map: (data: Record<string, any>) => TaskState }[] = [
      {
        key: 'g3_state_alpha_refresh',
        url: '/gen3-state-alpha/shadow-monitor/status',
        map: (data) => ({
          status: data.workflow?.ok === false || data.monitor?.last_error ? 'failed' : 'success',
          message: data.workflow?.message || strategyTaskMessageFromResult(data.monitor?.last_result) || 'G3 State Alpha运行链路可用',
          last_success_at: data.monitor?.last_success_at || data.workflow?.checked_at || null,
          updated_at: data.workflow?.checked_at || data.monitor?.last_run_at || data.scheduler?.next_run_time,
        }),
      },
      {
        key: 'g3_shadow_monitor',
        url: '/gen3-state-alpha/shadow-monitor/status',
        map: (data) => ({
          status: data.monitor?.last_error ? 'failed' : data.monitor?.enabled ? 'idle' : 'paused',
          message: data.scheduler?.message || (data.monitor?.enabled ? 'G3影子监控已启用，等待触发' : 'G3影子监控已暂停'),
          last_success_at: data.monitor?.last_error ? null : data.monitor?.last_success_at,
          updated_at: data.monitor?.last_run_at || data.monitor?.last_heartbeat_sent_at || data.scheduler?.next_run_time,
        }),
      },
      {
        key: 'g3_workflow_status',
        url: '/gen3-state-alpha/workflow/status',
        map: (data) => ({
          status: data.ok === false ? 'failed' : 'success',
          message: data.message || `路线=${data.selected_route || '-'}，阻断=${Array.isArray(data.blockers) ? data.blockers.length : 0}`,
          last_success_at: data.ok === false ? null : data.checked_at,
          updated_at: data.checked_at,
        }),
      },
      {
        key: 'g3_current_shadow',
        url: '/gen3-state-alpha/current?refresh=false&limit=80',
        map: (data) => ({
          status: data.ok === false ? 'failed' : 'success',
          message: strategyTaskMessageFromResult(data) || data.diagnosis || 'G3当前影子票据可读取',
          last_success_at: data.ok === false ? null : data.updated_at || data.generated_at || new Date().toISOString(),
          updated_at: data.updated_at || data.generated_at || new Date().toISOString(),
        }),
      },
      {
        key: 'g3_monitor_status',
        url: '/gen3-state-alpha/shadow-monitor/status',
        map: (data) => ({
          status: data.scheduler?.enabled ? 'success' : data.monitor?.enabled ? 'success' : 'paused',
          message: data.scheduler?.enabled ? `下次运行 ${formatDateTime(data.scheduler.next_run_time)}` : 'G3监控调度未启用',
          last_success_at: data.monitor?.last_success_at || null,
          updated_at: data.monitor?.last_run_at || data.scheduler?.next_run_time,
        }),
      },
      {
        key: 'g3_current_readiness',
        url: '/gen3-state-alpha/current?refresh=false&limit=80',
        map: (data) => ({
          status: data.ok === false ? 'failed' : data.formal_buy_signal || data.auto_order_allowed ? 'failed' : 'success',
          message:
            data.formal_buy_signal || data.auto_order_allowed
              ? '正式交易闸门异常开启，请立即复核'
              : 'shadow-only / observe-only，正式交易闸门锁定',
          last_success_at: data.ok === false ? null : data.updated_at || data.generated_at || new Date().toISOString(),
          updated_at: data.updated_at || data.generated_at || new Date().toISOString(),
        }),
      },
      {
        key: 'g3_historical_trades',
        url: '/gen3-state-alpha/historical-trades?limit=80',
        map: (data) => ({
          status: data.ok === false ? 'failed' : 'success',
          message:
            data.metrics?.trade_count != null
              ? `历史成交 ${data.metrics.trade_count} 笔，未来函数审计 ${data.future_leak_audit?.status || '已读取'}`
              : strategyTaskMessageFromResult(data) || 'G3历史成交复核可读取',
          last_success_at: data.ok === false ? null : new Date().toISOString(),
          updated_at: new Date().toISOString(),
        }),
      },
      {
        key: 'g3_evidence_inventory',
        url: '/gen3-state-alpha/evidence-inventory?limit=200',
        map: (data) => ({
          status: data.ok === false ? 'failed' : 'success',
          message: `证据归档 ${Array.isArray(data.inventory) ? data.inventory.length : 0} 条，蓝图报告${data.artifacts?.blueprint_report?.ok ? '可读' : '待复核'}`,
          last_success_at: data.ok === false ? null : new Date().toISOString(),
          updated_at: new Date().toISOString(),
        }),
      },
    ]

    await Promise.all(
      statusRequests.map(async (item) => {
        try {
          const data = (await request.get(item.url)) as Record<string, any>
          setStrategyTaskState(item.key, item.map(data))
        } catch (error) {
          setStrategyTaskState(item.key, {
            status: 'failed',
            message: errDetail(error) || '获取状态失败',
            updated_at: new Date().toISOString(),
          })
        }
      })
    )
  }, [setStrategyTaskState])

  const loadStartupReferenceSyncSetting = useCallback(async () => {
    try {
      const body = (await request.get('/system/startup-reference-sync-setting')) as { data?: { enabled?: boolean; default_enabled?: boolean } }
      setStartupReferenceSyncEnabled(!!body?.data?.enabled)
      setStartupReferenceSyncDefaultEnabled(!!body?.data?.default_enabled)
    } catch {
      toast.error('获取启动初始化配置失败')
    }
  }, [toast])

  // ---- 任务轮询 ----
  const pollTaskStatus = useCallback(
    async (taskName: string, onDone?: () => void) => {
      try {
        const body = (await request.get(`/system/task-status/${taskName}`)) as {
          task?: { is_running?: boolean; error?: string; results?: { message?: string } | null }
        }
        const task = body?.task || {}
        if (task.is_running) {
          setTimeout(() => void pollTaskStatus(taskName, onDone), 3000)
          return
        }
        onDone?.()
        if (task.results) {
          toast.success(task.results.message || '任务完成')
        } else if (task.error) {
          toast.error(`任务失败: ${task.error}`)
        } else {
          toast.info('任务已完成')
        }
        await refreshCoreMaintenanceStatus()
        if (showTimelinePanelRef.current) await refreshTimeline()
      } catch {
        onDone?.()
      }
    },
    [refreshCoreMaintenanceStatus, refreshTimeline, toast]
  )

  // ---- 策略任务执行 ----
  const runStrategyMaintenanceTask = useCallback(
    async (task: StrategyTask, options?: { waitForCompletion?: boolean; showToast?: boolean }): Promise<string> => {
      const { waitForCompletion = false, showToast: show = true } = options || {}
      setStrategyTaskSyncingState(task.key, true)
      setStrategyTaskState(task.key, {
        status: 'running',
        message: `${task.label} 已触发...`,
        updated_at: new Date().toISOString(),
      })

      try {
        let payload: Record<string, any>
        if (task.key === 'g3_state_alpha_refresh') {
          payload = (await request.post('/gen3-state-alpha/workflow/refresh/run-once', { force_send: false })) as Record<string, any>
          if (payload.task_id) {
            const statusUrl = `/gen3-state-alpha/workflow/refresh-task/${payload.task_id}`
            return waitForCompletion
              ? await waitStrategyAsyncTask(task, payload.task_id, statusUrl)
              : await pollStrategyAsyncTask(task, payload.task_id, statusUrl, { showToast: show })
          }
          setStrategyTaskState(task.key, {
            status: payload.ok === false ? 'failed' : 'success',
            message: strategyTaskMessageFromResult(payload) || 'G3 State Alpha刷新完成',
            last_success_at: payload.ok === false ? null : new Date().toISOString(),
            updated_at: payload.checked_at || new Date().toISOString(),
          })
          setStrategyTaskSyncingState(task.key, false)
          await refreshStrategyMaintenanceStatus()
          return getStrategyTaskStatus(task.key)
        }

        if (task.key === 'g3_shadow_monitor') {
          payload = (await request.post('/gen3-state-alpha/shadow-monitor/run-once', { force_send: true })) as Record<string, any>
          if (payload.task_id) {
            const statusUrl = `/gen3-state-alpha/workflow/refresh-task/${payload.task_id}`
            return waitForCompletion
              ? await waitStrategyAsyncTask(task, payload.task_id, statusUrl)
              : await pollStrategyAsyncTask(task, payload.task_id, statusUrl, { showToast: show })
          }
          setStrategyTaskState(task.key, {
            status: payload.ok === false ? 'failed' : 'success',
            message: strategyTaskMessageFromResult(payload) || 'G3影子监控完成',
            last_success_at: payload.ok === false ? null : new Date().toISOString(),
            updated_at: payload.checked_at || new Date().toISOString(),
          })
          setStrategyTaskSyncingState(task.key, false)
          await refreshStrategyMaintenanceStatus()
          return getStrategyTaskStatus(task.key)
        }

        if (task.key === 'g3_workflow_status') {
          payload = (await request.get('/gen3-state-alpha/workflow/status')) as Record<string, any>
          setStrategyTaskState(task.key, {
            status: payload.ok === false ? 'failed' : 'success',
            message: payload.message || `路线=${payload.selected_route || '-'}，阻断=${Array.isArray(payload.blockers) ? payload.blockers.length : 0}`,
            last_success_at: payload.ok === false ? null : payload.checked_at || new Date().toISOString(),
            updated_at: payload.checked_at || new Date().toISOString(),
          })
          setStrategyTaskSyncingState(task.key, false)
          return getStrategyTaskStatus(task.key)
        }

        if (task.key === 'g3_current_shadow' || task.key === 'g3_current_readiness') {
          payload = (await request.get('/gen3-state-alpha/current?refresh=false&limit=80')) as Record<string, any>
          const guardrailOpen = !!payload.formal_buy_signal || !!payload.auto_order_allowed
          setStrategyTaskState(task.key, {
            status: payload.ok === false || guardrailOpen ? 'failed' : 'success',
            message: guardrailOpen
              ? '正式交易闸门异常开启，请立即复核'
              : strategyTaskMessageFromResult(payload) || 'G3当前影子票据与准入检查可读取',
            last_success_at: payload.ok === false || guardrailOpen ? null : new Date().toISOString(),
            updated_at: payload.updated_at || payload.generated_at || new Date().toISOString(),
          })
          setStrategyTaskSyncingState(task.key, false)
          return getStrategyTaskStatus(task.key)
        }

        if (task.key === 'g3_monitor_status') {
          payload = (await request.get('/gen3-state-alpha/shadow-monitor/status')) as Record<string, any>
          setStrategyTaskState(task.key, {
            status: payload.scheduler?.enabled ? 'success' : payload.monitor?.enabled ? 'success' : 'paused',
            message: payload.scheduler?.enabled ? `下次运行 ${formatDateTime(payload.scheduler.next_run_time)}` : 'G3监控调度未启用',
            last_success_at: payload.monitor?.last_success_at || null,
            updated_at: payload.monitor?.last_run_at || payload.scheduler?.next_run_time || new Date().toISOString(),
          })
          setStrategyTaskSyncingState(task.key, false)
          return getStrategyTaskStatus(task.key)
        }

        if (task.key === 'g3_historical_trades') {
          payload = (await request.get('/gen3-state-alpha/historical-trades?limit=80')) as Record<string, any>
          setStrategyTaskState(task.key, {
            status: payload.ok === false ? 'failed' : 'success',
            message:
              payload.metrics?.trade_count != null
                ? `历史成交 ${payload.metrics.trade_count} 笔，未来函数审计 ${payload.future_leak_audit?.status || '已读取'}`
                : strategyTaskMessageFromResult(payload) || 'G3历史成交复核完成',
            last_success_at: payload.ok === false ? null : new Date().toISOString(),
            updated_at: new Date().toISOString(),
          })
          setStrategyTaskSyncingState(task.key, false)
          return getStrategyTaskStatus(task.key)
        }

        if (task.key === 'g3_evidence_inventory') {
          payload = (await request.get('/gen3-state-alpha/evidence-inventory?limit=200')) as Record<string, any>
          setStrategyTaskState(task.key, {
            status: payload.ok === false ? 'failed' : 'success',
            message: `证据归档 ${Array.isArray(payload.inventory) ? payload.inventory.length : 0} 条，蓝图报告${payload.artifacts?.blueprint_report?.ok ? '可读' : '待复核'}`,
            last_success_at: payload.ok === false ? null : new Date().toISOString(),
            updated_at: new Date().toISOString(),
          })
          setStrategyTaskSyncingState(task.key, false)
          return getStrategyTaskStatus(task.key)
        }

        throw new Error('未配置策略维护任务')
      } catch (error) {
        setStrategyTaskSyncingState(task.key, false)
        setStrategyTaskState(task.key, {
          status: 'failed',
          message: errDetail(error) || '执行失败',
          updated_at: new Date().toISOString(),
        })
        if (show) toast.error(`${task.label}执行失败`)
        return 'failed'
      }
    },
    [getStrategyTaskStatus, pollStrategyAsyncTask, refreshStrategyMaintenanceStatus, setStrategyTaskState, setStrategyTaskSyncingState, toast]
  )

  const strategyMaintenanceRunning = useMemo(
    () =>
      runningAllStrategyMaintenanceNow ||
      Object.values(strategyTaskSyncing).some(Boolean) ||
      Object.values(strategyTaskStates).some((state) => normalizeStrategyStatus(state?.status) === 'running'),
    [runningAllStrategyMaintenanceNow, strategyTaskSyncing, strategyTaskStates]
  )

  const runAllStrategyMaintenanceTasks = async (mode: 'daily' | 'heavy') => {
    const selectedTasks = strategyMaintenanceTasks.filter((task) => task.mode === mode)
    const modeText = mode === 'heavy' ? '证据复核' : 'G3日常维护'
    if (!selectedTasks.length) {
      toast.info(`暂无可执行的${modeText}任务`)
      return
    }
    const confirmText =
      mode === 'heavy'
        ? '确认一键执行G3历史成交和证据归档复核吗？该操作只读取研究证据，不会打开正式交易闸门。'
        : '确认按顺序一键执行G3日常维护吗？将刷新影子工作流并检查监控、票据、台账和正式交易锁。'
    if (!window.confirm(confirmText)) return
    setRunningAllStrategyMaintenanceNow(true)
    try {
      let failedCount = 0
      for (const task of selectedTasks) {
        const status = await runStrategyMaintenanceTask(task, { waitForCompletion: true, showToast: false })
        if (status === 'failed') failedCount += 1
      }
      await refreshStrategyMaintenanceStatus()
      if (failedCount > 0) toast.error(`策略${modeText}一键执行完成，失败 ${failedCount} 项`)
      else toast.success(`策略${modeText}一键执行完成`)
    } finally {
      setRunningAllStrategyMaintenanceNow(false)
    }
  }

  // ---- 核心命令 ----
  const toggleCoreMaintenance = async () => {
    try {
      if (coreMaintenance.enabled) {
        await request.post('/system/core-data-maintenance/stop')
        toast.success('核心数据自动维护已暂停')
      } else {
        await request.post('/system/core-data-maintenance/start')
        toast.success('核心数据自动维护已启动')
      }
      await refreshCoreMaintenanceStatus()
      if (showTimelinePanel) await refreshTimeline()
    } catch (error) {
      toast.error(errDetail(error))
    }
  }

  const refreshTodayFullMarketNow = async () => {
    if (!window.confirm('确认一键更新当天所有股票、指数、板块数据，并在盘后执行完整性自检吗？')) return
    setRunningTodayFullMarketRefreshNow(true)
    try {
      const body = (await request.post('/system/core-data-maintenance/refresh-today-full-market')) as {
        success?: boolean
        message?: string
        task_name?: string
      }
      if (body?.success) {
        toast.success(body?.message || '当天全市场更新自检任务已启动')
        void pollTaskStatus(body?.task_name || 'today_full_market_refresh', () => setRunningTodayFullMarketRefreshNow(false))
        return
      }
      setRunningTodayFullMarketRefreshNow(false)
      toast.error(body?.message || '启动失败')
    } catch (error) {
      setRunningTodayFullMarketRefreshNow(false)
      toast.error(errDetail(error))
    }
  }

  const triggerMarketMinuteHistoryRepairNow = async () => {
    if (!window.confirm('确认修复最近30个交易日的股票/指数5m/15m/30m/60m历史分钟K线吗？')) return
    setRunningMarketMinuteHistoryRepairNow(true)
    try {
      const body = (await request.post('/system/core-data-maintenance/run-task/market_minute_history_repair')) as {
        success?: boolean
        message?: string
        task_name?: string
      }
      if (body?.success) {
        toast.success(body?.message || '股票/指数历史分钟修复任务已启动')
        void pollTaskStatus(body?.task_name || 'market_minute_history_repair', () => setRunningMarketMinuteHistoryRepairNow(false))
        return
      }
      setRunningMarketMinuteHistoryRepairNow(false)
      toast.error(body?.message || '启动失败')
    } catch (error) {
      setRunningMarketMinuteHistoryRepairNow(false)
      toast.error(errDetail(error))
    }
  }

  const toggleTimelinePanel = async () => {
    const next = !showTimelinePanel
    setShowTimelinePanel(next)
    if (next) await refreshTimeline()
  }

  const repairDataSourceDate = async () => {
    const date = dataSourceCountDate || dataSourceCounts?.trade_date || ''
    if (!date) {
      toast.info('请先选择统计日期')
      return
    }
    if (!window.confirm(`确认修复 ${date} 的日线和 5m/15m/30m/60m 分钟数据吗？`)) return
    setDataSourceRepairing(true)
    try {
      const body = (await request.post(`/system/data-source-counts/repair-date?trade_date=${encodeURIComponent(date)}`)) as {
        success?: boolean
        message?: string
        task_name?: string
      }
      if (body?.success) {
        toast.success(body?.message || '按日期修复任务已启动')
        void pollTaskStatus(body?.task_name || 'data_source_date_repair', () => {
          setDataSourceRepairing(false)
          void refreshDataSourceCounts()
        })
        return
      }
      setDataSourceRepairing(false)
      toast.error(body?.message || '启动修复失败')
    } catch (error) {
      setDataSourceRepairing(false)
      toast.error(errDetail(error))
    }
  }

  const toggleStartupReferenceSync = async () => {
    setStartupReferenceSyncSaving(true)
    try {
      const body = (await request.post('/system/startup-reference-sync-setting', {
        enabled: !startupReferenceSyncEnabled,
      })) as { success?: boolean; message?: string; data?: { enabled?: boolean; default_enabled?: boolean } }
      if (body?.success) {
        setStartupReferenceSyncEnabled(!!body.data?.enabled)
        setStartupReferenceSyncDefaultEnabled(!!body.data?.default_enabled)
        toast.success(body?.message || '启动初始化配置已更新')
      } else {
        toast.error('保存失败')
      }
    } catch (error) {
      toast.error(errDetail(error))
    } finally {
      setStartupReferenceSyncSaving(false)
    }
  }

  // ---- 高级维护：通用任务启动 ----
  const runPostTask = async (
    url: string,
    taskName: string,
    busySetter: (v: boolean) => void,
    successMsg: string,
    confirmText?: string,
    body?: unknown
  ) => {
    if (confirmText && !window.confirm(confirmText)) return
    busySetter(true)
    try {
      const resp = (await request.post(url, body)) as { success?: boolean; message?: string; task_name?: string; error?: string }
      if (resp?.success) {
        toast.success(resp.message || successMsg)
        void pollTaskStatus(resp.task_name || taskName, () => busySetter(false))
        return
      }
      busySetter(false)
      toast.error(resp?.message || resp?.error || '启动失败')
    } catch (error) {
      busySetter(false)
      toast.error(`启动失败: ${errDetail(error)}`)
    }
  }

  const autoRepairSelectedStrategyTasks = useMemo(() => {
    if (autoRepairStrategyMode === 'all') return strategyMaintenanceTasks
    return strategyMaintenanceTasks.filter((task) => task.mode === autoRepairStrategyMode)
  }, [autoRepairStrategyMode])

  const autoRepairStrategyPreview = autoRepairSelectedStrategyTasks.length
    ? autoRepairSelectedStrategyTasks.map((t) => t.label).join(' / ')
    : '暂无可执行策略维护任务'

  const canRunAutoRepair =
    autoRepairCategory === 'strategy'
      ? autoRepairSelectedStrategyTasks.length > 0
      : autoRepairDataScope === 'daily_stock' || autoRepairDataScope === 'all_stock_history' || autoRepairPeriods.length > 0

  const runAutoRepair = async () => {
    if (!canRunAutoRepair) {
      toast.info('请先选择修复范围')
      return
    }
    const categoryText = autoRepairCategory === 'strategy' ? '策略' : '数据'
    if (!window.confirm(`确认执行${categoryText}自动修复吗？`)) return
    setAutoRepairRunning(true)
    try {
      if (autoRepairCategory === 'strategy') {
        let failedCount = 0
        for (const task of autoRepairSelectedStrategyTasks) {
          const status = await runStrategyMaintenanceTask(task, { waitForCompletion: true, showToast: false })
          if (status === 'failed') failedCount += 1
        }
        await refreshStrategyMaintenanceStatus()
        if (failedCount > 0) toast.error(`策略自动修复完成，失败 ${failedCount} 项`)
        else toast.success('策略自动修复完成')
        setAutoRepairRunning(false)
        return
      }

      let taskName = ''
      if (autoRepairDataScope === 'minute_history') {
        const params = new URLSearchParams({ days: String(autoRepairDays || 30) })
        autoRepairPeriods.forEach((p) => params.append('periods', p))
        const body = (await request.post(`/system/core-data-maintenance/run-task/market_minute_history_repair?${params.toString()}`)) as {
          task_name?: string
        }
        taskName = body?.task_name || 'market_minute_history_repair'
        await handleAutoRepairResponse(body, taskName)
        return
      }
      if (autoRepairDataScope === 'index_history') {
        const body = (await request.post('/system/repair-history-klines', { periods: autoRepairPeriods, type: 'index' })) as Record<string, any>
        await handleAutoRepairResponse(body, 'repair_history_klines')
        return
      }
      if (autoRepairDataScope === 'daily_stock') {
        const body = (await request.post('/system/repair-daily-klines')) as Record<string, any>
        await handleAutoRepairResponse(body, 'repair_daily_klines')
        return
      }
      if (autoRepairDataScope === 'all_stock_history') {
        const body = (await request.post('/system/repair-all-history-klines')) as Record<string, any>
        await handleAutoRepairResponse(body, 'repair_all_history_klines')
        return
      }
      if (autoRepairDataScope === 'emotion_cycle') {
        const body = (await request.post(`/system/repair-emotion-cycle-30d?days=${autoRepairDays || 30}`)) as Record<string, any>
        await handleAutoRepairResponse(body, 'repair_emotion_cycle_30d')
        return
      }
    } catch (error) {
      setAutoRepairRunning(false)
      toast.error(errDetail(error))
    }
  }

  const handleAutoRepairResponse = async (body: Record<string, any>, taskName: string) => {
    if (body?.success && taskName) {
      toast.success(body?.message || '自动修复任务已启动')
      void pollTaskStatus(taskName, () => setAutoRepairRunning(false))
      return
    }
    setAutoRepairRunning(false)
    toast.error(body?.message || '启动失败')
  }

  const updateIndices = async () => {
    setUpdatingIndices(true)
    try {
      const body = (await request.post('/stocks/update-indices')) as { success?: number; error?: string }
      if (Number(body?.success) > 0) {
        toast.success(`成功更新 ${body.success} 个指数`)
      } else {
        toast.error(`更新指数列表失败: ${body?.error || '无数据'}`)
      }
    } catch (error) {
      toast.error(`更新指数列表失败: ${errDetail(error)}`)
    } finally {
      setUpdatingIndices(false)
    }
  }

  const toggleEmotionAuto5m = async () => {
    const enable = !emotionAutoEnabled
    try {
      await request.post(`/system/emotion-cycle-auto-5m/${enable ? 'start' : 'stop'}`)
      setEmotionAutoEnabled(enable)
      toast.success(enable ? '已开启盘中每5分钟自动修复' : '已关闭盘中自动修复')
    } catch (error) {
      toast.error(`${enable ? '开启' : '关闭'}失败: ${errDetail(error)}`)
    }
  }

  // ---- 生命周期 ----
  useEffect(() => {
    loadStrategySuccessMemory()
    void refreshCoreMaintenanceStatus()
    void refreshRuntimeHealth()
    void refreshQmtAfterCloseExecution()
    void refreshDataSourceCounts()
    void refreshStrategyMaintenanceStatus()
    void loadStartupReferenceSyncSetting()
    const timer = setInterval(() => {
      void (async () => {
        await refreshCoreMaintenanceStatus()
        await refreshRuntimeHealth()
        await refreshStrategyMaintenanceStatus()
        if (showTimelinePanelRef.current) await refreshTimeline()
      })()
    }, 10000)
    return () => {
      clearInterval(timer)
      Object.values(pollTimersRef.current || {}).forEach((t) => {
        if (t) clearTimeout(t)
      })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ---- 渲染辅助 ----
  const maintenanceStatusText = getStateText(coreMaintenance.overall_status)
  const latestFailedText = coreMaintenance.latest_failed_task ? getTaskAlias(coreMaintenance.latest_failed_task) : '-'

  const getDataSourceStatusText = (row: DataSourceRow) => {
    if (row?.verification_status === 'unverified') return row.ok ? '有数据 / 待验收' : '无记录 / 待验收'
    if (!row?.ok) return '缺失'
    if (row.complete) return '完整'
    return '有缺口'
  }
  const getDataSourceStatusColor = (row: DataSourceRow): 'emerald' | 'amber' | 'red' =>
    !row?.ok ? 'red' : row.complete ? 'emerald' : 'amber'

  const eyebrow = 'text-xs font-extrabold tracking-widest text-slate-500 uppercase dark:text-slate-400'
  const metaText = 'text-xs text-zinc-500 dark:text-zinc-400'

  const togglePeriod = (list: string[], setter: (v: string[]) => void, value: string) =>
    setter(list.includes(value) ? list.filter((p) => p !== value) : [...list, value])

  const periodBox = (title: string, list: string[], setter: (v: string[]) => void) => (
    <div className="mt-3 flex flex-wrap items-center gap-2 text-sm text-zinc-600 dark:text-zinc-300">
      <span className="text-xs text-zinc-500 dark:text-zinc-400">{title}</span>
      {klinePeriods.map((period) => (
        <label key={`${title}-${period.value}`} className="flex cursor-pointer items-center gap-1 text-xs">
          <input
            type="checkbox"
            className="size-3.5 accent-blue-600"
            checked={list.includes(period.value)}
            onChange={() => togglePeriod(list, setter, period.value)}
          />
          {period.label}
        </label>
      ))}
    </div>
  )

  const dataSourceTable = (
    <div className="mt-3 overflow-x-auto">
      <table className="min-w-full border-separate border-spacing-0 text-sm">
        <thead>
          <tr>
            {['数据源', '覆盖状态', '标的数量', '数据行数', '完整度', '最新日期', '分钟明细'].map((h) => (
              <th key={h} className="border-b border-zinc-950/10 px-3 py-2 text-left text-xs font-semibold text-zinc-500 dark:border-white/10 dark:text-zinc-400">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {!dataSourceCountsLoading && !dataSourceCounts.rows.length && (
            <tr>
              <td colSpan={7} className="py-6 text-center text-sm text-zinc-500 dark:text-zinc-400">暂无统计数据</td>
            </tr>
          )}
          {dataSourceCounts.rows.map((row) => (
            <tr
              key={row.key}
              className={!row?.ok ? 'bg-red-500/5' : row?.ok && !row?.complete ? 'bg-amber-400/5' : ''}
            >
              <td className="px-3 py-2">
                <strong className="text-zinc-950 dark:text-white">{row.label}</strong>
                <small className="block text-xs text-zinc-500 dark:text-zinc-400">{row.key}</small>
              </td>
              <td className="px-3 py-2">
                <Badge color={getDataSourceStatusColor(row)}>{getDataSourceStatusText(row)}</Badge>
              </td>
              <td className="px-3 py-2 tabular-nums">{formatInteger(row.code_count)}</td>
              <td className="px-3 py-2 tabular-nums">{formatInteger(row.row_count)}</td>
              <td className="px-3 py-2">
                <strong className="tabular-nums">{formatCoverage(row.coverage_rate)}</strong>
                {!!row.expected_row_count && (
                  <small className="block text-xs text-zinc-500 dark:text-zinc-400">应有 {formatInteger(row.expected_row_count)}</small>
                )}
                {(!!row.missing_rows || !!row.extra_rows) && (
                  <small className="block text-xs text-zinc-500 dark:text-zinc-400">
                    缺 {formatInteger(row.missing_rows)} / 多 {formatInteger(row.extra_rows)}
                  </small>
                )}
                {!!row.extended_rows && (
                  <small className="block text-xs text-zinc-500 dark:text-zinc-400">延长时段 +{formatInteger(row.extended_rows)}</small>
                )}
              </td>
              <td className="px-3 py-2">{row.latest_date || '-'}</td>
              <td className="px-3 py-2">
                {row.periods?.length ? (
                  <div className="flex flex-col gap-1">
                    {row.periods.map((period) => (
                      <span
                        key={`${row.key}-${period.period}`}
                        className={`inline-flex flex-wrap items-center gap-1 rounded-md px-2 py-1 text-xs ${
                          period.complete
                            ? 'bg-emerald-500/15 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300'
                            : 'bg-amber-400/20 text-amber-700 dark:bg-amber-400/10 dark:text-amber-300'
                        }`}
                      >
                        <strong>{period.period}</strong>
                        <span className="tabular-nums">{formatInteger(period.row_count)}/{formatInteger(period.expected_row_count)}</span>
                        <em className="not-italic tabular-nums">{formatCoverage(period.coverage_rate)}</em>
                        {(!!period.missing_rows || !!period.extra_rows) && (
                          <small>缺{formatInteger(period.missing_rows)} 多{formatInteger(period.extra_rows)}</small>
                        )}
                        {!!period.extended_rows && <small>延长时段 +{formatInteger(period.extended_rows)}</small>}
                      </span>
                    ))}
                  </div>
                ) : (
                  '-'
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )

  return (
    <div className="mx-auto max-w-6xl px-4 py-8 sm:px-6">
      {/* Hero */}
      <PageHeader
        title="系统配置"
        subtitle="把日常维护收敛成一个主入口：自动调度负责常规更新，手动按钮只用于补链路、复核和救场。"
        actions={
          <Badge color={coreMaintenance.enabled ? 'emerald' : 'amber'} className="text-sm">
            {coreMaintenance.enabled ? '自动维护运行中' : '自动维护已暂停'}
          </Badge>
        }
      />
      <Card className="mb-6">
        <div>
          <strong className={`block text-3xl tracking-tight ${stateColor(coreMaintenance.overall_status)}`}>{maintenanceStatusText}</strong>
          <span className={metaText}>API任务成功率 {formatRate(coreMaintenance.today_success_rate)}</span>
        </div>
        <div className="mt-3 flex flex-col gap-2 rounded-xl border border-zinc-950/10 bg-sky-50/80 p-3 dark:border-white/10 dark:bg-white/5">
            <div className="flex items-center justify-between">
              <span className={metaText}>运行健康</span>
              <strong className={`text-sm ${
                runtimeHealth.status === 'healthy' ? 'text-emerald-600 dark:text-emerald-400'
                  : runtimeHealth.status === 'degraded' ? 'text-amber-600 dark:text-amber-400'
                    : runtimeHealth.status === 'deferred' ? 'text-zinc-500 dark:text-zinc-400'
                      : 'text-red-600 dark:text-red-400'
              }`}>
                {runtimeStatusTextMap[runtimeHealth.status] || '未知'}
              </strong>
            </div>
            <p className={metaText}>
              {runtimeHealth.status === 'deferred'
                ? '非交易日：收盘产物等待下一交易日调度刷新，未发现数据缺口；策略仍保持观察锁定。'
                : runtimeHealth.strategy_actionable
                  ? '数据链路可用；G3仍遵循既有交易闸门。'
                  : '数据链路未就绪；策略结论已降级为观察状态。'}
            </p>
            <div className="flex flex-col gap-1.5">
              {runtimeHealth.components.map((item) => (
                <div key={item.name} className="flex items-start gap-2">
                  <span
                    className={`mt-1 inline-block size-2 shrink-0 rounded-full ${
                      item.status === 'healthy' ? 'bg-emerald-500'
                        : item.status === 'blocked' ? 'bg-red-500'
                          : item.status === 'stale' ? 'bg-amber-500'
                            : 'bg-zinc-400'
                    }`}
                  />
                  <div>
                    <strong className="block text-xs text-zinc-950 dark:text-white">{runtimeComponentLabel(item.name)}</strong>
                    <small className={metaText}>{runtimeComponentStatusText(item)} · 责任：{item.remediation_owner || '待配置'}</small>
                  </div>
                </div>
              ))}
            </div>
          </div>
          <div className="flex flex-col gap-1 text-xs text-zinc-500 dark:text-zinc-400">
            <span>最近失败：{latestFailedText}</span>
            <span>刷新于：{formatDateTime(lastStatusRefreshAt)}</span>
          </div>
      </Card>

      {/* 核心数据链路 */}
      <Card className="mb-6">
        <SectionTitle
          title="核心数据链路"
          actions={
            <div className="flex gap-2">
              <Button outline onClick={() => void refreshCoreMaintenanceStatus()}>刷新状态</Button>
              <Button outline onClick={() => void toggleTimelinePanel()}>
                {showTimelinePanel ? '收起时间线' : '查看时间线'}
              </Button>
            </div>
          }
        />

        <div className="mt-4 grid gap-4 lg:grid-cols-2">
          {qmtAfterClose.available ? (
            <article className="rounded-xl border border-amber-500/30 bg-amber-50/50 p-4 dark:border-amber-500/20 dark:bg-amber-500/5">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <span className={`${eyebrow} text-amber-700 dark:text-amber-400`}>QMT Execution</span>
                  <h3 className="text-base font-semibold text-zinc-950 dark:text-white">QMT盘后执行结果</h3>
                  <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">
                    仅展示 Windows 主机 QMT/xtquant 实际产物，不再使用旧调度器的推测状态。
                  </p>
                </div>
                <Badge color={qmtAfterClose.closed ? 'emerald' : 'amber'}>
                  {qmtAfterClose.closed ? '已闭环' : '未闭环'}
                </Badge>
              </div>
              <div className="mt-3 grid gap-2 sm:grid-cols-2">
                <div className="rounded-lg bg-white/70 p-3 text-xs dark:bg-white/5">
                  <div className="flex items-center justify-between">
                    <strong className="text-zinc-950 dark:text-white">执行窗口</strong>
                    <span className="text-emerald-600 dark:text-emerald-400">
                      {qmtAfterClose.schedule?.start} 起，每 {qmtAfterClose.schedule?.retry_minutes} 分钟重试
                    </span>
                  </div>
                  <p className="mt-1 text-zinc-600 dark:text-zinc-400">窗口截止 {qmtAfterClose.schedule?.active_end}；闭环后自动跳过后续触发。</p>
                </div>
                <div className="rounded-lg bg-white/70 p-3 text-xs dark:bg-white/5">
                  <div className="flex items-center justify-between">
                    <strong className="text-zinc-950 dark:text-white">盘后最终校验</strong>
                    <span className={qmtAfterClose.closed ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400'}>
                      {qmtAfterClose.remaining_period_code_records || 0} 条周期-代码缺口记录
                    </span>
                  </div>
                  <p className="mt-1 text-zinc-600 dark:text-zinc-400">
                    交易日：{qmtAfterClose.trade_date || '-'}；初始：{qmtAfterClose.initial_period_code_records ?? '-'} 条记录 /{' '}
                    {qmtAfterClose.initial_unique_codes ?? '-'} 只唯一码。
                  </p>
                </div>
                {(qmtAfterClose.periods || []).map((period) => (
                  <div key={period.period} className="rounded-lg bg-white/70 p-3 text-xs dark:bg-white/5">
                    <div className="flex items-center justify-between">
                      <strong className="text-zinc-950 dark:text-white">{period.period}</strong>
                      <span className={period.issue_codes ? 'text-red-600 dark:text-red-400' : 'text-emerald-600 dark:text-emerald-400'}>
                        {period.complete_codes}/{period.expected_codes}
                      </span>
                    </div>
                    <p className="mt-1 text-zinc-600 dark:text-zinc-400">
                      缺口代码：{period.issue_codes}；{period.reason_counts?.incomplete ? `不完整 ${period.reason_counts.incomplete}` : '完整'}
                    </p>
                  </div>
                ))}
              </div>
            </article>
          ) : (
            <article className="rounded-xl border border-amber-500/30 bg-amber-50/50 p-4 dark:border-amber-500/20 dark:bg-amber-500/5">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <span className={`${eyebrow} text-amber-700 dark:text-amber-400`}>QMT Execution</span>
                  <h3 className="text-base font-semibold text-zinc-950 dark:text-white">QMT盘后执行结果</h3>
                  <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">正在读取 Windows 主机 QMT/xtquant 的实际执行产物。</p>
                </div>
                <Badge color="amber">加载中</Badge>
              </div>
            </article>
          )}

          {qmtAfterClose.overnight_repair?.available && (
            <article className="rounded-xl border border-emerald-500/30 bg-emerald-50/50 p-4 dark:border-emerald-500/20 dark:bg-emerald-500/5">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <span className={`${eyebrow} text-emerald-700 dark:text-emerald-400`}>QMT Overnight Repair</span>
                  <h3 className="text-base font-semibold text-zinc-950 dark:text-white">午夜滚动修复结果</h3>
                  <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">
                    展示最近一次历史分钟K线修复的真实执行状态，并与最终数据闭环状态分开呈现。
                  </p>
                </div>
                <Badge
                  color={
                    qmtAfterClose.overnight_repair.execution_ok
                      ? 'emerald'
                      : qmtAfterClose.overnight_repair.validation_closed
                        ? 'amber'
                        : 'red'
                  }
                >
                  {qmtAfterClose.overnight_repair.execution_ok
                    ? '执行完成'
                    : qmtAfterClose.overnight_repair.validation_closed
                      ? '校验闭环 / 执行异常'
                      : '未闭环'}
                </Badge>
              </div>
              <div className="mt-3 grid gap-2 sm:grid-cols-2">
                <div className="rounded-lg bg-white/70 p-3 text-xs dark:bg-white/5">
                  <div className="flex items-center justify-between">
                    <strong className="text-zinc-950 dark:text-white">执行窗口</strong>
                    <span className="text-emerald-600 dark:text-emerald-400">
                      {qmtAfterClose.overnight_repair.schedule?.start} - {qmtAfterClose.overnight_repair.schedule?.active_end}
                    </span>
                  </div>
                  <p className="mt-1 text-zinc-600 dark:text-zinc-400">每日午夜滚动复核前一周的 5m/15m/30m/60m 数据。</p>
                </div>
                <div className="rounded-lg bg-white/70 p-3 text-xs dark:bg-white/5">
                  <div className="flex items-center justify-between">
                    <strong className="text-zinc-950 dark:text-white">最近修复范围</strong>
                    <span>{qmtAfterClose.overnight_repair.trade_dates?.join(' ~ ') || '-'}</span>
                  </div>
                  <p className="mt-1 text-zinc-600 dark:text-zinc-400">产物时间：{formatDateTime(qmtAfterClose.overnight_repair.updated_at)}</p>
                </div>
                <div className="rounded-lg bg-white/70 p-3 text-xs dark:bg-white/5">
                  <div className="flex items-center justify-between">
                    <strong className="text-zinc-950 dark:text-white">最终校验</strong>
                    <span className={qmtAfterClose.overnight_repair.validation_closed ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400'}>
                      {qmtAfterClose.overnight_repair.remaining_issue_count || 0} 条剩余缺口
                    </span>
                  </div>
                  <p className="mt-1 text-zinc-600 dark:text-zinc-400">{qmtAfterClose.overnight_repair.execution_message}</p>
                </div>
              </div>
            </article>
          )}
        </div>

        {showTimelinePanel && (
          <div className="mt-4 rounded-xl border border-zinc-950/10 p-4 dark:border-white/10">
            <div className="text-sm font-semibold text-zinc-950 dark:text-white">最近任务时间线</div>
            {!coreMaintenance.timeline?.length ? (
              <EmptyState text="暂无任务记录" className="mt-2" />
            ) : (
              <div className="mt-2 flex flex-col divide-y divide-zinc-950/5 dark:divide-white/5">
                {coreMaintenance.timeline.map((item, idx) => (
                  <div key={`${item.task_name}-${item.created_at}-${idx}`} className="grid gap-1 py-2 text-xs sm:grid-cols-[10rem_4rem_1fr_5rem_10rem]">
                    <span className="font-medium text-zinc-950 dark:text-white">{getTaskAlias(item.task_name)}</span>
                    <span className={stateColor(item.status || 'idle')}>{getStateText(item.status)}</span>
                    <span className="text-zinc-600 dark:text-zinc-400">{item.message || '-'}</span>
                    <span className="tabular-nums text-zinc-500 dark:text-zinc-400">{formatDuration(item.duration_sec)}</span>
                    <span className="text-zinc-500 dark:text-zinc-400">{formatDateTime(item.created_at)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </Card>

      {/* 命令区 */}
      <section className="mb-6 grid gap-4 lg:grid-cols-3">
        <article className="rounded-2xl border border-blue-500/30 bg-blue-50/60 p-5 dark:border-blue-500/20 dark:bg-blue-500/5">
          <span className={`${eyebrow} text-blue-700 dark:text-blue-400`}>推荐操作</span>
          <h2 className="mt-1 text-lg font-semibold text-zinc-950 dark:text-white">一键修复今日市场数据</h2>
          <p className="mt-2 text-xs leading-5 text-zinc-600 dark:text-zinc-400">
            覆盖交易日历、股票/指数/板块基础数据、当天日线、分钟快照、15/30m分钟K线、板块统计和盘后自检。
          </p>
          <Button
            color="blue"
            className="mt-4"
            disabled={runningTodayFullMarketRefreshNow || !!coreMaintenance.task_status?.today_full_market_refresh?.is_running}
            onClick={() => void refreshTodayFullMarketNow()}
          >
            {runningTodayFullMarketRefreshNow || coreMaintenance.task_status?.today_full_market_refresh?.is_running
              ? '正在更新并自检...'
              : '立即执行今日全市场更新'}
          </Button>
        </article>
        <article className="rounded-2xl border border-zinc-950/10 bg-white p-5 dark:border-white/10 dark:bg-white/5">
          <span className={eyebrow}>自动化</span>
          <h3 className="mt-1 text-base font-semibold text-zinc-950 dark:text-white">
            {coreMaintenance.enabled ? '自动维护已接管' : '自动维护已暂停'}
          </h3>
          <p className="mt-2 text-xs leading-5 text-zinc-600 dark:text-zinc-400">
            常规同步建议交给调度器。只有排障时才暂停或重启自动维护。
          </p>
          <Button outline className="mt-4" onClick={() => void toggleCoreMaintenance()}>
            {coreMaintenance.enabled ? '暂停自动维护' : '启动自动维护'}
          </Button>
        </article>
        <article className="rounded-2xl border border-red-500/25 bg-red-50/50 p-5 dark:border-red-500/20 dark:bg-red-500/5">
          <span className={`${eyebrow} text-red-700 dark:text-red-400`}>排障</span>
          <h3 className="mt-1 text-base font-semibold text-zinc-950 dark:text-white">历史分钟K线修复</h3>
          <p className="mt-2 text-xs leading-5 text-zinc-600 dark:text-zinc-400">
            用于回补最近30个交易日股票/指数分钟K线，耗时较长，不作为日常主入口。
          </p>
          <Button
            color="red"
            className="mt-4"
            disabled={runningMarketMinuteHistoryRepairNow || !!coreMaintenance.task_status?.market_minute_history_repair?.is_running}
            onClick={() => void triggerMarketMinuteHistoryRepairNow()}
          >
            {runningMarketMinuteHistoryRepairNow || coreMaintenance.task_status?.market_minute_history_repair?.is_running
              ? '修复中...'
              : '执行历史分钟修复'}
          </Button>
        </article>
      </section>

      {/* 数据源覆盖 */}
      <Card className="mb-6">
        <SectionTitle
          title="按日期统计数据源数量"
          actions={
            <>
              <Input
                type="date"
                className="w-40"
                value={dataSourceCountDate}
                onChange={(e) => setDataSourceCountDate(e.target.value)}
              />
              <Button outline disabled={dataSourceCountsLoading} onClick={() => void refreshDataSourceCounts()}>
                {dataSourceCountsLoading ? '统计中...' : '刷新统计'}
              </Button>
              <Button color="red" disabled={dataSourceRepairing || !dataSourceCountDate} onClick={() => void repairDataSourceDate()}>
                {dataSourceRepairing ? '修复中...' : '修复当前日期'}
              </Button>
            </>
          }
        />
        <div className="mt-2 flex flex-wrap gap-4 text-xs text-zinc-500 dark:text-zinc-400">
          <span>统计日期：{dataSourceCounts.trade_date || dataSourceCountDate}</span>
          <span>最新可用：{dataSourceCounts.latest_available_date || '-'}</span>
          <span>缺口项：{dataSourceCounts.summary?.incomplete ?? dataSourceCounts.summary?.missing ?? '-'}</span>
          <span>检查时间：{formatDateTime(dataSourceCounts.checked_at)}</span>
        </div>
        {dataSourceTable}
      </Card>

      {/* 策略维护 */}
      <Card className="mb-6">
        <SectionTitle
          title="策略维护"
          actions={
            <Badge color={strategyMaintenanceRunning ? 'emerald' : 'amber'}>
              {strategyMaintenanceRunning ? '执行中' : '待命'}
            </Badge>
          }
        />
        <div className="mt-3 flex flex-wrap gap-2">
          <Button color="emerald" disabled={strategyMaintenanceRunning} onClick={() => void runAllStrategyMaintenanceTasks('daily')}>
            {runningAllStrategyMaintenanceNow ? 'G3日常维护执行中...' : '一键执行G3日常维护'}
          </Button>
          <Button color="red" disabled={strategyMaintenanceRunning} onClick={() => void runAllStrategyMaintenanceTasks('heavy')}>
            一键执行证据复核
          </Button>
          <Button outline onClick={() => void refreshStrategyMaintenanceStatus()}>刷新G3状态</Button>
        </div>
        <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {strategyMaintenanceTasks.map((task) => {
            const status = getStrategyTaskStatus(task.key)
            const state = strategyTaskStates[task.key] || {}
            return (
              <article
                key={task.key}
                className={`flex flex-col rounded-xl border p-4 ${
                  task.mode === 'heavy'
                    ? 'border-red-500/20 bg-red-50/40 dark:border-red-500/15 dark:bg-red-500/5'
                    : 'border-zinc-950/10 bg-white dark:border-white/10 dark:bg-white/5'
                }`}
              >
                <div className="flex items-start justify-between gap-2">
                  <h3 className="text-sm font-semibold text-zinc-950 dark:text-white">{task.label}</h3>
                  <div className="flex flex-col items-end gap-1">
                    <Badge color={task.mode === 'heavy' ? 'red' : 'zinc'}>{task.mode === 'heavy' ? '重任务' : '日常'}</Badge>
                    <span className={`text-xs font-medium ${stateColor(status)}`}>{getStateText(status)}</span>
                  </div>
                </div>
                <p className="mt-2 flex-1 text-xs leading-5 text-zinc-600 dark:text-zinc-400">
                  {state.message || task.description}
                </p>
                <div className="mt-3 flex items-center justify-between text-xs">
                  <span className="text-zinc-500 dark:text-zinc-400">上次成功</span>
                  <strong className="text-zinc-950 dark:text-white">{formatDateTime(state.last_success_at)}</strong>
                </div>
                <Button
                  plain
                  className="mt-3 text-sm"
                  disabled={strategyMaintenanceRunning || !!strategyTaskSyncing[task.key]}
                  onClick={() => void runStrategyMaintenanceTask(task)}
                >
                  {strategyTaskSyncing[task.key] ? '执行中' : task.mode === 'heavy' ? '重型执行' : '执行一次'}
                </Button>
              </article>
            )
          })}
        </div>
      </Card>

      {/* 启动初始化 */}
      <Card className="mb-6">
        <SectionTitle
          title="启动初始化"
          actions={
            <Badge color={startupReferenceSyncEnabled ? 'emerald' : 'amber'}>
              {startupReferenceSyncEnabled ? '启动时开启' : '启动时关闭'}
            </Badge>
          }
        />
        <p className="mt-2 text-xs leading-5 text-zinc-600 dark:text-zinc-400">
          控制项目启动时是否自动更新交易日历、股票列表、指数列表、板块列表和板块成分股。
          调试阶段建议关闭，线上运行建议开启。修改后需重启后端生效。
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          <Button color="blue" disabled={startupReferenceSyncSaving} onClick={() => void toggleStartupReferenceSync()}>
            {startupReferenceSyncSaving ? '保存中...' : startupReferenceSyncEnabled ? '关闭启动初始化' : '开启启动初始化'}
          </Button>
          <Button outline disabled={startupReferenceSyncSaving} onClick={() => void loadStartupReferenceSyncSetting()}>刷新状态</Button>
        </div>
        <p className="mt-2 text-xs text-zinc-500 dark:text-zinc-400">
          当前默认：{startupReferenceSyncDefaultEnabled ? '线上模式默认开启' : '调试模式默认关闭'}
        </p>
      </Card>

      {/* 高级维护 */}
      <Card>
        <SectionTitle
          title="高级维护"
          actions={
            <Button plain onClick={() => setAdvancedOpen(!advancedOpen)}>{advancedOpen ? '折叠' : '展开'}</Button>
          }
        />
        <p className="mt-2 text-xs leading-5 text-zinc-600 dark:text-zinc-400">
          低频维护、重算、初始化任务放在这里。自动维护异常时再使用手工触发；“立即同步核心数据”已降级为参考数据入口，避免和今日全市场闭环重复。
        </p>

        {advancedOpen && (
          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            {/* 自动修复控制台 */}
            <article className="rounded-xl border border-indigo-500/25 bg-indigo-50/40 p-4 dark:border-indigo-500/20 dark:bg-indigo-500/5 lg:col-span-2">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <span className={`${eyebrow} text-indigo-700 dark:text-indigo-400`}>Auto Repair</span>
                  <h3 className="text-base font-semibold text-zinc-950 dark:text-white">自动修复控制台</h3>
                  <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">
                    按数据和策略两大类执行维护；数据修复可选择时间范围和 K 线周期，策略修复可选择日常或证据复核批次。
                  </p>
                </div>
                <Badge color={autoRepairRunning ? 'emerald' : 'amber'}>{autoRepairRunning ? '执行中' : '待命'}</Badge>
              </div>
              <div className="mt-4 flex flex-col gap-4 lg:flex-row lg:items-start">
                <div className="inline-flex overflow-hidden rounded-lg border border-zinc-950/10 dark:border-white/15">
                  {([['data', '数据修复'], ['strategy', '策略修复']] as ['data' | 'strategy', string][]).map(([value, text]) => (
                    <button
                      key={value}
                      type="button"
                      className={`px-4 py-1.5 text-sm ${autoRepairCategory === value ? 'bg-zinc-950 text-white dark:bg-white dark:text-zinc-950' : 'text-zinc-600 dark:text-zinc-300'}`}
                      onClick={() => setAutoRepairCategory(value)}
                    >
                      {text}
                    </button>
                  ))}
                </div>
                {autoRepairCategory === 'data' ? (
                  <div className="flex flex-1 flex-wrap items-center gap-4">
                    <label className="flex items-center gap-2 text-sm text-zinc-700 dark:text-zinc-300">
                      <span className="text-xs text-zinc-500 dark:text-zinc-400">修复对象</span>
                      <div className="w-48">
                        <Select value={autoRepairDataScope} onChange={(e) => setAutoRepairDataScope(e.target.value)} aria-label="修复对象">
                          {autoRepairDataScopes.map((item) => (
                            <option key={item.value} value={item.value}>{item.label}</option>
                          ))}
                        </Select>
                      </div>
                    </label>
                    <label className="flex items-center gap-2 text-sm text-zinc-700 dark:text-zinc-300">
                      <span className="text-xs text-zinc-500 dark:text-zinc-400">时间范围</span>
                      <div className="w-36">
                        <Select value={autoRepairDays} onChange={(e) => setAutoRepairDays(Number(e.target.value))} aria-label="时间范围">
                          {autoRepairDayOptions.map((item) => (
                            <option key={item.value} value={item.value}>{item.label}</option>
                          ))}
                        </Select>
                      </div>
                    </label>
                    <div className="flex flex-wrap items-center gap-2 text-xs text-zinc-600 dark:text-zinc-300">
                      <span className="text-zinc-500 dark:text-zinc-400">修复周期</span>
                      {autoRepairPeriodOptions.map((period) => (
                        <label key={`auto-${period.value}`} className="flex cursor-pointer items-center gap-1">
                          <input
                            type="checkbox"
                            className="size-3.5 accent-blue-600"
                            checked={autoRepairPeriods.includes(period.value)}
                            onChange={() => togglePeriod(autoRepairPeriods, setAutoRepairPeriods, period.value)}
                          />
                          {period.label}
                        </label>
                      ))}
                    </div>
                  </div>
                ) : (
                  <div className="flex flex-1 flex-wrap items-center gap-4">
                    <label className="flex items-center gap-2 text-sm text-zinc-700 dark:text-zinc-300">
                      <span className="text-xs text-zinc-500 dark:text-zinc-400">策略批次</span>
                      <div className="w-44">
                        <Select value={autoRepairStrategyMode} onChange={(e) => setAutoRepairStrategyMode(e.target.value)} aria-label="策略批次">
                          <option value="daily">G3 日常维护</option>
                          <option value="heavy">G3 证据复核</option>
                          <option value="all">全部策略维护</option>
                        </Select>
                      </div>
                    </label>
                    <p className="text-xs text-zinc-500 dark:text-zinc-400">{autoRepairStrategyPreview}</p>
                  </div>
                )}
                <Button color="blue" disabled={autoRepairRunning || !canRunAutoRepair} onClick={() => void runAutoRepair()}>
                  {autoRepairRunning ? '自动修复执行中...' : '执行自动修复'}
                </Button>
              </div>
            </article>

            {/* 基础参考数据 */}
            <article className="rounded-xl border border-emerald-500/25 bg-emerald-50/40 p-4 dark:border-emerald-500/20 dark:bg-emerald-500/5">
              <h3 className="text-base font-semibold text-zinc-950 dark:text-white">基础参考数据</h3>
              <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">
                只同步交易日历、股票/指数/板块基础列表和当日日线。日常请优先使用顶部“一键修复今日市场数据”。
              </p>
              <div className="mt-2 flex gap-2">
                <Badge color="zinc">参考数据</Badge>
                <Badge color="zinc">轻量同步</Badge>
              </div>
              <Button
                color="emerald"
                className="mt-3"
                disabled={runningCoreAssetsSyncNow}
                onClick={() =>
                  void runPostTask(
                    '/system/core-data-maintenance/sync-core-assets',
                    'core_data_manual_sync',
                    setRunningCoreAssetsSyncNow,
                    '核心数据同步任务已启动'
                  )
                }
              >
                {runningCoreAssetsSyncNow ? '基础同步中...' : '只同步基础参考数据'}
              </Button>
            </article>

            {/* 股票数据 */}
            <article className="rounded-xl border border-zinc-950/10 bg-white p-4 dark:border-white/10 dark:bg-white/5">
              <h3 className="text-base font-semibold text-zinc-950 dark:text-white">股票数据</h3>
              <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">股票列表、日线修复、全历史修复、盘中股票快照手动触发。</p>
              <div className="mt-2 flex gap-2">
                <Badge color="zinc">初始化</Badge><Badge color="zinc">补数</Badge><Badge color="zinc">重算</Badge>
              </div>
              <div className="mt-3 flex flex-wrap gap-2">
                <Button outline disabled={loading} onClick={() => void runPostTask('/system/update-stock-list', 'update_stock_list', setLoading, '股票列表更新任务已启动')}>
                  {loading ? '执行中...' : '更新股票列表'}
                </Button>
                <Button outline disabled={repairingDailyKlines} onClick={() => void runPostTask('/system/repair-daily-klines', 'repair_daily_klines', setRepairingDailyKlines, '日K线修复任务已启动', '确认修复日K线数据吗？')}>
                  {repairingDailyKlines ? '执行中...' : '修复日线'}
                </Button>
                <Button color="red" disabled={repairingAllHistoryKlines} onClick={() => void runPostTask('/system/repair-all-history-klines', 'repair_all_history_klines', setRepairingAllHistoryKlines, '全量历史K线修复任务已启动', '确认修复全量历史K线吗？该任务耗时较长。')}>
                  {repairingAllHistoryKlines ? '执行中...' : '修复全历史K线'}
                </Button>
              </div>
              {periodBox('盘中股票快照周期：', selectedStockUpdatePeriods, setSelectedStockUpdatePeriods)}
              <Button
                color="emerald"
                className="mt-3"
                disabled={updatingStockTodayData || selectedStockUpdatePeriods.length === 0}
                onClick={() =>
                  void runPostTask(
                    '/system/update-stock-today-data',
                    'update_stock_today_data',
                    setUpdatingStockTodayData,
                    '当天股票数据更新任务已启动',
                    '确认更新当天股票数据吗？',
                    { periods: selectedStockUpdatePeriods, force: true }
                  )
                }
              >
                {updatingStockTodayData ? '执行中...' : '手动更新当天股票数据'}
              </Button>
            </article>

            {/* 指数与日历 */}
            <article className="rounded-xl border border-zinc-950/10 bg-white p-4 dark:border-white/10 dark:bg-white/5">
              <h3 className="text-base font-semibold text-zinc-950 dark:text-white">指数与日历</h3>
              <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">指数列表、历史K线、交易日历和盘中指数快照。</p>
              <div className="mt-2 flex gap-2"><Badge color="zinc">初始化</Badge><Badge color="zinc">补数</Badge></div>
              <div className="mt-3 flex flex-wrap gap-2">
                <Button outline disabled={updatingIndices} onClick={() => void updateIndices()}>
                  {updatingIndices ? '执行中...' : '更新指数列表'}
                </Button>
                <Button outline disabled={updatingTradeCalendar} onClick={() => void runPostTask('/system/update-trade-calendar', 'update_trade_calendar', setUpdatingTradeCalendar, '交易日历更新任务已启动', '确认更新交易日历吗？')}>
                  {updatingTradeCalendar ? '执行中...' : '更新交易日历'}
                </Button>
              </div>
              {periodBox('指数历史K线周期：', selectedPeriods, setSelectedPeriods)}
              <Button
                outline
                className="mt-3"
                disabled={repairingHistoryKlines || selectedPeriods.length === 0}
                onClick={() =>
                  void runPostTask(
                    '/system/repair-history-klines',
                    'repair_history_klines',
                    setRepairingHistoryKlines,
                    '历史K线同步任务已启动',
                    '确认同步指数全量历史K线吗？',
                    { periods: selectedPeriods, type: 'index' }
                  )
                }
              >
                {repairingHistoryKlines ? '执行中...' : '同步指数全量历史K线'}
              </Button>
              {periodBox('盘中指数快照周期：', selectedUpdatePeriods, setSelectedUpdatePeriods)}
              <Button
                color="emerald"
                className="mt-3"
                disabled={updatingTodayData || selectedUpdatePeriods.length === 0}
                onClick={() =>
                  void runPostTask(
                    '/system/update-today-data',
                    'update_today_data',
                    setUpdatingTodayData,
                    '当天指数数据更新任务已启动',
                    '确认更新当天指数数据吗？',
                    { periods: selectedUpdatePeriods, force: true }
                  )
                }
              >
                {updatingTodayData ? '执行中...' : '手动更新当天指数数据'}
              </Button>
            </article>

            {/* 板块数据 */}
            <article className="rounded-xl border border-zinc-950/10 bg-white p-4 dark:border-white/10 dark:bg-white/5">
              <h3 className="text-base font-semibold text-zinc-950 dark:text-white">板块数据</h3>
              <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">一级、二级、三级板块列表与板块历史统计补齐。</p>
              <div className="mt-2 flex gap-2"><Badge color="zinc">参考数据</Badge><Badge color="zinc">补数</Badge></div>
              <div className="mt-3 flex flex-wrap gap-2">
                <Button outline disabled={syncingSectors} onClick={() => void runPostTask('/system/sync-sectors', 'sync_sectors', setSyncingSectors, '行业板块同步任务已启动', '确认同步一二三级行业板块数据吗？')}>
                  {syncingSectors ? '执行中...' : '同步一级二级三级板块'}
                </Button>
                <Button color="emerald" disabled={updatingSectorIntradayStats} onClick={() => void runPostTask('/system/update-sector-intraday-stats?force=true', 'update_sector_intraday_stats', setUpdatingSectorIntradayStats, '板块当日统计刷新任务已启动', '确认刷新板块当日成分涨跌统计吗？')}>
                  {updatingSectorIntradayStats ? '执行中...' : '刷新板块当日统计'}
                </Button>
              </div>
              <label className="mt-3 flex items-center gap-2 text-sm text-zinc-700 dark:text-zinc-300">
                <span className="text-xs text-zinc-500 dark:text-zinc-400">板块历史同步天数</span>
                <Input
                  type="number" min={1} max={365} className="w-24"
                  value={syncHistoryDays}
                  onChange={(e) => setSyncHistoryDays(Number(e.target.value))}
                />
              </label>
              <Button
                outline
                className="mt-3"
                disabled={syncingSectorHistory}
                onClick={() => void runPostTask(`/system/sync-sector-history?days=${syncHistoryDays}`, 'sync_sector_history', setSyncingSectorHistory, '板块历史同步任务已启动', `确认同步近 ${syncHistoryDays} 天板块历史涨跌数据吗？`)}
              >
                {syncingSectorHistory ? '执行中...' : '同步板块历史涨跌'}
              </Button>
            </article>

            {/* 情绪周期 */}
            <article className="rounded-xl border border-zinc-950/10 bg-white p-4 dark:border-white/10 dark:bg-white/5">
              <h3 className="text-base font-semibold text-zinc-950 dark:text-white">情绪周期</h3>
              <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">情绪周期重算与盘中自动修复开关。</p>
              <div className="mt-2 flex gap-2"><Badge color="zinc">重算</Badge><Badge color="zinc">盘中修复</Badge></div>
              <div className="mt-3 flex flex-wrap gap-2">
                <Button outline disabled={repairingEmotion30d} onClick={() => void runPostTask('/system/repair-emotion-cycle-30d?days=30', 'repair_emotion_cycle_30d', setRepairingEmotion30d, '情绪周期30天修复任务已启动', '确认修复近30天情绪周期数据吗？')}>
                  {repairingEmotion30d ? '执行中...' : '修复近30天情绪'}
                </Button>
                <Button outline disabled={repairingEmotionLatest} onClick={() => void runPostTask('/system/repair-emotion-cycle-latest', 'repair_emotion_cycle_latest', setRepairingEmotionLatest, '最新情绪周期更新任务已启动', '确认更新最新交易日情绪周期吗？')}>
                  {repairingEmotionLatest ? '执行中...' : '更新最新情绪'}
                </Button>
              </div>
              <Button color={emotionAutoEnabled ? 'amber' : 'emerald'} className="mt-3" onClick={() => void toggleEmotionAuto5m()}>
                {emotionAutoEnabled ? '关闭盘中每5分钟自动修复' : '开启盘中每5分钟自动修复'}
              </Button>
            </article>
          </div>
        )}
      </Card>
    </div>
  )
}
