import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  getGen2MainlineHotspots,
  getGen2MainlineHotspotsUpdateTask,
  runGen2MainlineHotspotsUpdate,
} from '@/api/trading'
import { Alert } from '@/components/catalyst/alert'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Input } from '@/components/catalyst/input'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/catalyst/table'
import { useToast } from '@/components/toast'
import {
  Card,
  ChangeValue,
  EmptyState,
  PageHeader,
  PageLoading,
  SectionTitle,
  StatCard,
} from '@/components/ui'

const ANSI_PATTERN = /\u001b\[[0-9;]*m/g

const REFRESH_MODE_OPTIONS = [
  { label: '全部刷新', value: 'all' },
  { label: '只刷板块', value: 'sector' },
  { label: '只刷主题', value: 'theme' },
]

const STEP_LABELS: Record<string, string> = {
  sector_factor: '板块主线因子',
  sector_overlay: '板块候选重合',
  sector_watchlist: '板块观察池',
  theme_clusters: '主题聚类',
  theme_candidate_source: '主题候选源',
  theme_pool: '主题观察池',
  theme_overlay: '主题策略叠加',
}

type BadgeColor = Parameters<typeof Badge>[0]['color']

function cleanLogText(value: unknown): string {
  return String(value || '')
    .replace(ANSI_PATTERN, '')
    .replace(/\r/g, '')
    .trim()
}

function pct(value: unknown): string {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  return `${(n * 100).toFixed(2)}%`
}

function score(value: unknown, digits = 2): string {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  return n.toFixed(digits)
}

function percentValue(value: unknown, digits = 2): string {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  return `${n.toFixed(digits)}%`
}

function amountText(value: unknown): string {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  if (n >= 1e8) return `${(n / 1e8).toFixed(2)}亿`
  if (n >= 1e4) return `${(n / 1e4).toFixed(0)}万`
  return `${n.toFixed(0)}`
}

function modeText(mode: unknown): string {
  if (mode === 'sector') return '只刷板块'
  if (mode === 'theme') return '只刷主题'
  return '全部刷新'
}

function stepLabel(name: unknown): string {
  return STEP_LABELS[String(name || '')] || String(name || '--')
}

function stepTagColor(status: unknown): BadgeColor {
  if (status === 'completed' || status === 'ok') return 'emerald'
  if (status === 'warning') return 'amber'
  if (status === 'failed' || status === 'error') return 'red'
  return 'zinc'
}

function stepStatusText(status: unknown): string {
  if (status === 'completed' || status === 'ok') return '成功'
  if (status === 'warning') return '警告'
  if (status === 'failed' || status === 'error') return '失败'
  if (status === 'skipped') return '跳过'
  return '处理中'
}

function stepDuration(step: any): string {
  const seconds = Number(step?.duration_seconds)
  if (Number.isFinite(seconds)) return `${seconds.toFixed(1)}s`
  return '--'
}

function stepLog(step: any): string {
  const raw = cleanLogText(step?.stderr_tail || step?.stdout_tail || '')
  if (!raw) return ''
  return raw
    .split('\n')
    .map((line) => line.trimEnd())
    .filter(Boolean)
    .slice(-12)
    .join('\n')
}

function sourceTagColor(status: unknown): BadgeColor {
  if (status === 'ok') return 'emerald'
  if (status === 'warning') return 'amber'
  if (status === 'error') return 'red'
  return 'zinc'
}

function sourceTagText(status: unknown): string {
  if (status === 'ok') return '可用'
  if (status === 'warning') return '警告'
  if (status === 'error') return '异常'
  return '缺失'
}

function normalizeStockCode(value: unknown): string {
  const text = String(value || '').trim().toUpperCase()
  if (!text) return ''
  if (text.includes('.')) return text
  const digits = text.replace(/\D/g, '')
  if (/^\d{1,6}$/.test(digits) && digits) {
    const padded = digits.padStart(6, '0')
    return `${padded}${padded.startsWith('6') ? '.SH' : '.SZ'}`
  }
  return text
}

function stockCodeText(value: unknown): string {
  const normalized = normalizeStockCode(value)
  if (!normalized) return '--'
  return normalized.split('.')[0] || '--'
}

export default function Gen2MainlineHotspots() {
  const navigate = useNavigate()
  const toast = useToast()
  const [loading, setLoading] = useState(false)
  const [payload, setPayload] = useState<any>({})
  const [targetDate, setTargetDate] = useState('')
  const [limit, setLimit] = useState(30)
  const [updateTask, setUpdateTask] = useState<any>(null)
  const [activeTab, setActiveTab] = useState<'sector' | 'theme'>('sector')
  const [selectedSectorCode, setSelectedSectorCode] = useState('')
  const [selectedThemeKey, setSelectedThemeKey] = useState('')
  const [refreshMode, setRefreshMode] = useState('all')
  const taskTimer = useRef<number | null>(null)

  const available = !!payload?.available
  const message = payload?.message || ''
  const requestedDate = payload?.requested_date || ''
  const selectedDate = payload?.selected_date || ''
  const sourceDates: any = payload?.source_dates || {}
  const latestTradeDate = payload?.latest_trade_date || sourceDates?.latest_trade || ''
  const sectorSnapshotDate = sourceDates?.sector_overlay || sourceDates?.sector_factor || ''
  const summary: any = payload?.summary || {}
  const sourceStatus: any[] = Array.isArray(payload?.source_status) ? payload.source_status : []
  const sectorMainlineRows: any[] = Array.isArray(payload?.sector_mainline) ? payload.sector_mainline : []
  const sectorOverlayRows: any[] = Array.isArray(payload?.sector_candidate_overlay)
    ? payload.sector_candidate_overlay
    : []
  const watchlistRows: any[] = Array.isArray(payload?.sector_watchlist) ? payload.sector_watchlist : []
  const sectorMemberSnapshotRows: any[] = Array.isArray(payload?.sector_member_snapshot)
    ? payload.sector_member_snapshot
    : []
  const themePoolRows: any[] = Array.isArray(payload?.theme_observation_pool)
    ? payload.theme_observation_pool
    : []
  const themeOverlayRows: any[] = Array.isArray(payload?.theme_strategy_overlay)
    ? payload.theme_strategy_overlay
    : []
  const updateTaskSteps: any[] = Array.isArray(updateTask?.steps) ? updateTask.steps : []
  const updateRunning = ['queued', 'running'].includes(updateTask?.status)
  const updateWarnings: string[] = Array.isArray(updateTask?.warnings) ? updateTask.warnings : []

  const taskAlertColor: 'red' | 'amber' | 'green' | 'zinc' =
    updateTask?.status === 'failed'
      ? 'red'
      : updateTask?.status === 'completed' && updateWarnings.length
        ? 'amber'
        : updateTask?.status === 'completed'
          ? 'green'
          : 'zinc'

  const sectorStockMode = watchlistRows.length
    ? 'watchlist'
    : sectorMemberSnapshotRows.length
      ? 'member_snapshot'
      : 'empty'
  const normalizedSelectedSectorCode = String(selectedSectorCode || '').trim()
  const filteredWatchlistRows = useMemo(() => {
    const code = normalizedSelectedSectorCode
    if (!code) return watchlistRows
    return watchlistRows.filter((row) => String(row?.sector_code || '').trim() === code)
  }, [watchlistRows, normalizedSelectedSectorCode])
  const filteredSectorMemberSnapshotRows = useMemo(() => {
    const code = normalizedSelectedSectorCode
    if (!code) return sectorMemberSnapshotRows
    return sectorMemberSnapshotRows.filter((row) => String(row?.sector_code || '').trim() === code)
  }, [sectorMemberSnapshotRows, normalizedSelectedSectorCode])
  const filteredSectorOverlayRows = useMemo(() => {
    const code = normalizedSelectedSectorCode
    if (!code) return sectorOverlayRows
    return sectorOverlayRows.filter((row) => String(row?.sector_code || '').trim() === code)
  }, [sectorOverlayRows, normalizedSelectedSectorCode])
  const sectorStockRows =
    sectorStockMode === 'watchlist' ? filteredWatchlistRows : filteredSectorMemberSnapshotRows

  const selectedSectorName = useMemo(() => {
    const code = normalizedSelectedSectorCode
    if (!code) return ''
    const matched = sectorMainlineRows.find((row) => String(row?.sector_code || '').trim() === code)
    return String(matched?.sector_name || '').trim()
  }, [sectorMainlineRows, normalizedSelectedSectorCode])

  const sectorStockPanelTitle =
    sectorStockMode === 'watchlist' ? '主线板块观察池' : '主线板块成分股快照'
  const sectorStockPanelHint =
    sectorStockMode === 'watchlist'
      ? '更严格的观察池候选，偏研究观察，不直接下单'
      : `当前只展示左侧选中板块在主线识别日 ${sectorSnapshotDate || '--'} 的强势成分股快照；右侧同时补充 ${latestTradeDate || '--'} 的最新涨跌，方便对照今天盘面`
  const sectorStockEmptyText =
    sectorStockMode === 'watchlist' ? '暂无板块观察池' : '暂无主线板块成分股快照'
  const sectorSelectionMismatch = useMemo(() => {
    const code = normalizedSelectedSectorCode
    if (!code) return ''
    if (sectorStockMode === 'watchlist') return ''
    if (sectorMemberSnapshotRows.length > 0 && filteredSectorMemberSnapshotRows.length === 0) {
      return `${selectedSectorName || code} 当前没有匹配的成分股快照，右侧旧快照已被拦截；请重新刷新板块产物。`
    }
    return ''
  }, [normalizedSelectedSectorCode, sectorStockMode, sectorMemberSnapshotRows, filteredSectorMemberSnapshotRows, selectedSectorName])
  const sectorStockRankLabel = sectorStockMode === 'watchlist' ? '板块排名' : '板块内排位'
  const sectorStockScoreLabel = sectorStockMode === 'watchlist' ? '候选分' : '主线分'
  const sectorStockRet1Label = sectorStockMode === 'watchlist' ? '锚点后收益' : '识别日涨幅'
  const sectorStockRet2Label =
    sectorStockMode === 'watchlist' ? 'H20后收益' : `${latestTradeDate || '当前'}涨跌`
  const sectorStockRet3Label = sectorStockMode === 'watchlist' ? '' : `${latestTradeDate || '当前'}成交额`

  const themeSummaryRows = useMemo(() => {
    const groups = new Map<string, any>()
    for (const row of themePoolRows) {
      const label = String(row?.theme_label || '').trim()
      if (!label) continue
      if (!groups.has(label)) {
        groups.set(label, {
          theme_key: label,
          theme_title: label,
          best_level: String(row?.observation_level || '--'),
          max_theme_score: Number(row?.theme_candidate_score) || 0,
          stock_count: 0,
          l2_count: 0,
          l3_count: 0,
          l4_count: 0,
          risk_labels: new Set<string>(),
          current_change_sum: 0,
          current_change_count: 0,
          leader_name: '--',
          leader_current_change_pct: null as number | null,
        })
      }
      const item = groups.get(label)
      item.stock_count += 1
      const sc = Number(row?.theme_candidate_score)
      if (Number.isFinite(sc) && sc > item.max_theme_score) {
        item.max_theme_score = sc
        item.best_level = String(row?.observation_level || '--')
      }
      const level = String(row?.observation_level || '')
      if (level.startsWith('L2')) item.l2_count += 1
      else if (level.startsWith('L3')) item.l3_count += 1
      else if (level.startsWith('L4')) item.l4_count += 1
      const risk = String(row?.risk_label || '').trim()
      if (risk) item.risk_labels.add(risk)
      const currentChange = Number(row?.current_change_pct)
      if (Number.isFinite(currentChange)) {
        item.current_change_sum += currentChange
        item.current_change_count += 1
        if (item.leader_current_change_pct === null || currentChange > item.leader_current_change_pct) {
          item.leader_current_change_pct = currentChange
          item.leader_name = String(row?.display_name || '--')
        }
      }
    }
    return Array.from(groups.values())
      .map((item) => ({
        ...item,
        avg_current_change_pct: item.current_change_count
          ? item.current_change_sum / item.current_change_count
          : null,
        risk_mix: Array.from(item.risk_labels as Set<string>).join(' / ') || '--',
      }))
      .sort((a, b) => {
        if (b.max_theme_score !== a.max_theme_score) return b.max_theme_score - a.max_theme_score
        const avgDiff = (Number(b.avg_current_change_pct) || 0) - (Number(a.avg_current_change_pct) || 0)
        if (avgDiff !== 0) return avgDiff
        if ((b.l2_count || 0) !== (a.l2_count || 0)) return (b.l2_count || 0) - (a.l2_count || 0)
        return b.stock_count - a.stock_count
      })
  }, [themePoolRows])

  const selectedThemeRows = useMemo(() => {
    const key = String(selectedThemeKey || '').trim()
    if (!key) return []
    return themePoolRows
      .filter((row) => String(row?.theme_label || '').trim() === key)
      .slice()
      .sort((a, b) => {
        const scoreDiff = (Number(b?.theme_candidate_score) || 0) - (Number(a?.theme_candidate_score) || 0)
        if (scoreDiff !== 0) return scoreDiff
        return (Number(b?.theme_weight_hint) || 0) - (Number(a?.theme_weight_hint) || 0)
      })
  }, [themePoolRows, selectedThemeKey])

  const selectedThemeHint = !selectedThemeKey
    ? '选择左侧主题后，在右侧看对应股票'
    : `${selectedThemeKey} 的主题成分股明细，并补充 ${latestTradeDate || '--'} 的最新涨跌`

  // 自动选中第一行
  useEffect(() => {
    if (!sectorMainlineRows.length) {
      setSelectedSectorCode('')
      return
    }
    if (!sectorMainlineRows.some((row) => String(row?.sector_code || '').trim() === selectedSectorCode)) {
      setSelectedSectorCode(String(sectorMainlineRows[0]?.sector_code || '').trim())
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sectorMainlineRows])

  useEffect(() => {
    if (!themeSummaryRows.length) {
      setSelectedThemeKey('')
      return
    }
    if (!themeSummaryRows.some((row) => row.theme_key === selectedThemeKey)) {
      setSelectedThemeKey(themeSummaryRows[0].theme_key)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [themeSummaryRows])

  const dateResolutionText = useMemo(() => {
    const value = payload?.date_resolution
    if (value === 'exact') return '精确命中'
    if (value === 'fallback_latest') return '回退到最新快照'
    if (value === 'latest') return '使用最新快照'
    if (value === 'requested_only') return '仅保留请求日期'
    return '--'
  }, [payload])

  const updateTaskTitle = useMemo(() => {
    const task = updateTask || {}
    if (task.status === 'failed') return task.message || '主线热点产物刷新失败'
    if (task.status === 'completed') return task.message || '主线热点产物刷新完成'
    return task.message || '主线热点产物正在刷新'
  }, [updateTask])

  const updateTaskDescription = useMemo(() => {
    const task = updateTask || {}
    const parts: string[] = []
    if (task.target_date) parts.push(`目标日期: ${task.target_date}`)
    if (task.mode) parts.push(`刷新模式: ${modeText(task.mode)}`)
    if (task.current_step) parts.push(`当前步骤: ${stepLabel(task.current_step)}`)
    if (Number.isFinite(Number(task.progress))) parts.push(`进度: ${task.progress}%`)
    if (updateWarnings.length) parts.push(`警告: ${updateWarnings.length} 项`)
    if (task.error) parts.push(`错误: ${cleanLogText(task.error)}`)
    return parts.join(' | ')
  }, [updateTask, updateWarnings])

  const fetchData = useCallback(async () => {
    setLoading(true)
    try {
      const data = (await getGen2MainlineHotspots({
        target_date: targetDate || undefined,
        limit,
      })) as any
      setPayload(data || {})
    } catch (error: any) {
      setPayload({ available: false, message: error?.message || '读取第二代主线热点失败' })
    } finally {
      setLoading(false)
    }
  }, [targetDate, limit])

  const stopTaskPolling = useCallback(() => {
    if (taskTimer.current) {
      window.clearTimeout(taskTimer.current)
      taskTimer.current = null
    }
  }, [])

  const pollUpdateTask = useCallback(
    async (taskId: string) => {
      stopTaskPolling()
      try {
        const data = (await getGen2MainlineHotspotsUpdateTask(taskId)) as any
        setUpdateTask(data)
        if (data?.status === 'completed') {
          await fetchData()
          toast.success('主线热点产物刷新完成')
          stopTaskPolling()
          return
        }
        if (data?.status === 'failed' || data?.status === 'missing') {
          toast.error(data?.message || data?.error || '主线热点产物刷新失败')
          stopTaskPolling()
          return
        }
        taskTimer.current = window.setTimeout(() => pollUpdateTask(taskId), 3000)
      } catch {
        taskTimer.current = window.setTimeout(() => pollUpdateTask(taskId), 5000)
      }
    },
    [fetchData, stopTaskPolling, toast],
  )

  const startRefresh = useCallback(async () => {
    try {
      const task = (await runGen2MainlineHotspotsUpdate({
        target_date: targetDate || undefined,
        limit,
        mode: refreshMode,
      })) as any
      setUpdateTask(task)
      if (task?.task_id) {
        pollUpdateTask(task.task_id)
      }
    } catch (error: any) {
      toast.error(error?.message || '启动主线热点产物刷新失败')
    }
  }, [targetDate, limit, refreshMode, pollUpdateTask, toast])

  const resetAndReload = useCallback(() => {
    setTargetDate('')
    fetchData()
  }, [fetchData])

  const goStock = useCallback(
    (code: unknown) => {
      const normalized = normalizeStockCode(code)
      if (!normalized) {
        toast.info('缺少股票代码')
        return
      }
      navigate(`/stock/${normalized}`)
    },
    [navigate, toast],
  )

  useEffect(() => {
    fetchData()
    return stopTaskPolling()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const LinkButton = ({ onClick, children }: { onClick: () => void; children: React.ReactNode }) => (
    <button
      type="button"
      onClick={onClick}
      className="text-left text-blue-600 hover:underline dark:text-blue-400"
    >
      {children}
    </button>
  )

  const statusItems = [
    { label: '请求日期', value: requestedDate || '--' },
    { label: '展示日期', value: selectedDate || '--' },
    { label: '板块识别日', value: sectorSnapshotDate || '--' },
    { label: '最新交易日', value: latestTradeDate || '--' },
    { label: '日期解析', value: dateResolutionText },
    { label: '板块主线数', value: String(summary.sector_count || 0), tone: 'up' as const },
    { label: '主题主线池', value: String(summary.theme_pool_count || 0) },
    { label: '主题策略重合', value: String(summary.theme_match_count || 0), tone: 'accent' as const },
  ]

  return (
    <div className="flex flex-col gap-4 p-4">
      <PageHeader
        title="第二代主线热点"
        subtitle="把板块主线和主题主线拆开看。板块主线更偏确认，主题主线更偏线索与扩散。"
        actions={
          <>
            <div className="inline-flex overflow-hidden rounded-lg border border-zinc-200 dark:border-white/10">
              {REFRESH_MODE_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  className={
                    'px-3 py-1.5 text-xs font-medium transition-colors ' +
                    (refreshMode === opt.value
                      ? 'bg-blue-600 text-white'
                      : 'bg-white text-zinc-600 hover:bg-zinc-50 dark:bg-white/5 dark:text-zinc-300 dark:hover:bg-white/10')
                  }
                  onClick={() => setRefreshMode(opt.value)}
                >
                  {opt.label}
                </button>
              ))}
            </div>
            <Input
              type="date"
              value={targetDate}
              onChange={(e) => setTargetDate(e.target.value)}
              className="w-40"
            />
            <Input
              type="number"
              min={5}
              max={120}
              step={5}
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value) || 30)}
              className="w-24"
            />
            <Button color="blue" disabled={loading} onClick={fetchData}>
              {loading ? '查询中…' : '查询'}
            </Button>
            <Button color="amber" disabled={updateRunning} onClick={startRefresh}>
              {updateRunning ? '刷新中' : '刷新产物'}
            </Button>
            <Button disabled={loading} onClick={resetAndReload}>
              最新
            </Button>
          </>
        }
      />

      {loading && <PageLoading />}

      {updateTask && (
        <Alert color={taskAlertColor}>
          <div className="font-semibold">{updateTaskTitle}</div>
          {updateTaskDescription && <div className="mt-1 text-sm opacity-80">{updateTaskDescription}</div>}
        </Alert>
      )}

      {updateTaskSteps.length > 0 && (
        <Card>
          <SectionTitle
            title="刷新步骤"
            actions={
              <span className="text-sm text-zinc-500 dark:text-zinc-400">
                {updateTask?.steps_completed || 0} / {updateTask?.steps_total || updateTaskSteps.length}
              </span>
            }
          />
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {updateTaskSteps.map((step) => (
              <Card
                key={step.name}
                className="rounded-xl bg-gradient-to-b from-white to-zinc-50 dark:from-white/5 dark:to-transparent"
              >
                <div className="flex items-center justify-between gap-2">
                  <strong className="text-zinc-900 dark:text-white">{stepLabel(step.name)}</strong>
                  <Badge color={stepTagColor(step.status)}>{stepStatusText(step.status)}</Badge>
                </div>
                <div className="mt-2 flex justify-between text-xs text-zinc-500 dark:text-zinc-400">
                  <span>脚本 {step.script || '--'}</span>
                  <span>耗时 {stepDuration(step)}</span>
                </div>
                <div className="mt-1 flex justify-between text-xs text-zinc-500 dark:text-zinc-400">
                  <span>返回码 {step.returncode ?? '--'}</span>
                  <span>{step.optional ? '可选步骤' : '必跑步骤'}</span>
                </div>
                {stepLog(step) && (
                  <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-zinc-100 p-3 text-xs leading-relaxed text-zinc-600 dark:bg-white/5 dark:text-zinc-400">
                    {stepLog(step)}
                  </pre>
                )}
              </Card>
            ))}
          </div>
        </Card>
      )}

      {message && (
        <Alert color={available ? 'amber' : 'zinc'}>
          <div>{message}</div>
        </Alert>
      )}

      {!available && !loading && (
        <Alert color="amber">
          <div className="font-semibold">当前还没有可展示的主线热点结果</div>
          <div className="mt-1">页面已经接好，但需要先生成主线板块或主题研究产物。</div>
        </Alert>
      )}

      {(available || sourceStatus.length > 0) && (
        <>
          <section className="grid grid-cols-2 gap-3 lg:grid-cols-4 2xl:grid-cols-8">
            {statusItems.map((item) => (
              <StatCard
                key={item.label}
                label={item.label}
                value={item.value}
                tone={item.tone === 'up' ? 'up' : item.tone === 'accent' ? 'warning' : 'default'}
              />
            ))}
          </section>

          <section className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {sourceStatus.map((item) => (
              <Card key={item.name} className="flex flex-col gap-2.5">
                <div className="flex items-center justify-between gap-3">
                  <strong className="text-zinc-900 dark:text-white">{item.label}</strong>
                  <Badge color={sourceTagColor(item.status)}>{sourceTagText(item.status)}</Badge>
                </div>
                <div className="flex justify-between text-xs text-zinc-500 dark:text-zinc-400">
                  <span>行数 {item.row_count || 0}</span>
                  <span>最新 {item.latest_date || '--'}</span>
                </div>
                <div className="text-xs text-zinc-500 dark:text-zinc-400">更新 {item.updated_at || '--'}</div>
                <div className="break-all text-xs text-zinc-400 dark:text-zinc-500">
                  {item.path || '未找到产物文件'}
                </div>
              </Card>
            ))}
          </section>

          <Card>
            <SectionTitle
              title="主线视角"
              actions={
                <span className="text-sm text-zinc-500 dark:text-zinc-400">
                  顶部 Tab 只展示当前视角最重要的信息
                </span>
              }
            />

            <div className="mb-4 inline-flex overflow-hidden rounded-lg border border-zinc-200 dark:border-white/10">
              {(
                [
                  { label: '板块主线', value: 'sector' },
                  { label: '主题主线', value: 'theme' },
                ] as const
              ).map((tab) => (
                <button
                  key={tab.value}
                  type="button"
                  className={
                    'px-4 py-1.5 text-sm font-medium transition-colors ' +
                    (activeTab === tab.value
                      ? 'bg-blue-600 text-white'
                      : 'bg-white text-zinc-600 hover:bg-zinc-50 dark:bg-white/5 dark:text-zinc-300 dark:hover:bg-white/10')
                  }
                  onClick={() => setActiveTab(tab.value)}
                >
                  {tab.label}
                </button>
              ))}
            </div>

            {activeTab === 'sector' && (
              <>
                <section className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                  <div>
                    <SectionTitle
                      title="板块主线榜"
                      actions={
                        <span className="text-xs font-normal text-zinc-500 dark:text-zinc-400">
                          盘中扩散 + 板块强度的严格确认
                        </span>
                      }
                      className="mb-2"
                    />
                    <div className="max-h-[360px] overflow-y-auto rounded-lg border border-zinc-200 dark:border-white/10">
                      <Table dense striped>
                        <TableHead>
                          <TableRow>
                            <TableHeader>板块</TableHeader>
                            <TableHeader>阈值</TableHeader>
                            <TableHeader className="text-right">主线分</TableHeader>
                            <TableHeader className="text-right">窗口分</TableHeader>
                            <TableHeader className="text-right">扩散分</TableHeader>
                            <TableHeader className="text-right">60日涨幅</TableHeader>
                            <TableHeader className="text-right">上午上涨比</TableHeader>
                          </TableRow>
                        </TableHead>
                        <TableBody>
                          {sectorMainlineRows.length === 0 && (
                            <TableRow>
                              <TableCell colSpan={7}>
                                <EmptyState text="暂无板块主线" />
                              </TableCell>
                            </TableRow>
                          )}
                          {sectorMainlineRows.map((row) => (
                            <TableRow
                              key={row.sector_code}
                              className={
                                'cursor-pointer ' +
                                (String(row.sector_code || '').trim() === normalizedSelectedSectorCode
                                  ? 'bg-blue-50 dark:bg-blue-500/10'
                                  : '')
                              }
                              onClick={() => setSelectedSectorCode(String(row.sector_code || '').trim())}
                            >
                              <TableCell>
                                <LinkButton onClick={() => setSelectedSectorCode(String(row.sector_code || '').trim())}>
                                  {row.sector_name || '--'}
                                </LinkButton>
                                <div className="text-xs text-zinc-500 dark:text-zinc-400">
                                  {row.sector_code || '--'}
                                </div>
                              </TableCell>
                              <TableCell>{row.threshold_level || '--'}</TableCell>
                              <TableCell className="text-right tabular-nums">
                                {score(row.mainline_intraday_score)}
                              </TableCell>
                              <TableCell className="text-right tabular-nums">
                                {score(row.true_index_window_score)}
                              </TableCell>
                              <TableCell className="text-right tabular-nums">
                                {score(row.intraday_diffusion_score)}
                              </TableCell>
                              <TableCell className="text-right tabular-nums">{pct(row.ret60)}</TableCell>
                              <TableCell className="text-right tabular-nums">
                                {pct(row.morning_rise_ratio)}
                              </TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </div>
                  </div>

                  <div>
                    <SectionTitle
                      title={sectorStockPanelTitle}
                      actions={
                        <span className="text-xs font-normal text-zinc-500 dark:text-zinc-400">
                          {sectorStockPanelHint}
                        </span>
                      }
                      className="mb-2"
                    />
                    {sectorSelectionMismatch && (
                      <Alert color="amber" className="mb-2">
                        <div>{sectorSelectionMismatch}</div>
                      </Alert>
                    )}
                    <div className="max-h-[360px] overflow-y-auto rounded-lg border border-zinc-200 dark:border-white/10">
                      <Table dense striped>
                        <TableHead>
                          <TableRow>
                            <TableHeader>股票</TableHeader>
                            <TableHeader>板块</TableHeader>
                            <TableHeader className="text-right">{sectorStockRankLabel}</TableHeader>
                            <TableHeader className="text-right">{sectorStockScoreLabel}</TableHeader>
                            <TableHeader className="text-right">{sectorStockRet1Label}</TableHeader>
                            <TableHeader className="text-right">{sectorStockRet2Label}</TableHeader>
                            {sectorStockRet3Label && (
                              <TableHeader className="text-right">{sectorStockRet3Label}</TableHeader>
                            )}
                          </TableRow>
                        </TableHead>
                        <TableBody>
                          {sectorStockRows.length === 0 && (
                            <TableRow>
                              <TableCell colSpan={7}>
                                <EmptyState text={sectorStockEmptyText} />
                              </TableCell>
                            </TableRow>
                          )}
                          {sectorStockRows.map((row, i) => (
                            <TableRow key={`${row.stock_code || row.code}-${i}`}>
                              <TableCell>
                                <LinkButton
                                  onClick={() => goStock(row.stock_code || row.code || row.stock_code6)}
                                >
                                  {row.stock_name || row.name || '--'}
                                </LinkButton>
                                <div className="text-xs text-zinc-500 dark:text-zinc-400">
                                  {stockCodeText(row.stock_code || row.code || row.stock_code6)}
                                </div>
                              </TableCell>
                              <TableCell>{row.sector_name || '--'}</TableCell>
                              <TableCell className="text-right tabular-nums">{row.sector_rank || '--'}</TableCell>
                              <TableCell className="text-right tabular-nums">
                                {score(
                                  sectorStockMode === 'watchlist'
                                    ? row.candidate_score
                                    : row.mainline_intraday_score,
                                )}
                              </TableCell>
                              <TableCell className="text-right">
                                <ChangeValue
                                  value={
                                    sectorStockMode === 'watchlist'
                                      ? Number.isFinite(Number(row.stock_current_from_anchor))
                                        ? Number(row.stock_current_from_anchor) * 100
                                        : null
                                      : Number.isFinite(Number(row.change_pct))
                                        ? Number(row.change_pct)
                                        : null
                                  }
                                />
                              </TableCell>
                              <TableCell className="text-right">
                                <ChangeValue
                                  value={
                                    sectorStockMode === 'watchlist'
                                      ? Number.isFinite(Number(row.stock_current_from_h20))
                                        ? Number(row.stock_current_from_h20) * 100
                                        : null
                                      : Number.isFinite(Number(row.current_change_pct))
                                        ? Number(row.current_change_pct)
                                        : null
                                  }
                                />
                              </TableCell>
                              {sectorStockRet3Label && (
                                <TableCell className="text-right tabular-nums">
                                  {amountText(row.current_amount ?? row.amount)}
                                </TableCell>
                              )}
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </div>
                  </div>
                </section>

                <section className="mt-4">
                  <SectionTitle
                    title="板块主线与 G2/G3 候选重合"
                    actions={
                      <span className="text-xs font-normal text-zinc-500 dark:text-zinc-400">
                        看哪些策略候选正落在当前板块主线里
                      </span>
                    }
                    className="mb-2"
                  />
                  <div className="max-h-[320px] overflow-y-auto rounded-lg border border-zinc-200 dark:border-white/10">
                    <Table dense striped>
                      <TableHead>
                        <TableRow>
                          <TableHeader>来源</TableHeader>
                          <TableHeader>股票</TableHeader>
                          <TableHeader>板块</TableHeader>
                          <TableHeader>日期</TableHeader>
                          <TableHeader className="text-right">主线分</TableHeader>
                          <TableHeader className="text-right">候选分</TableHeader>
                          <TableHeader className="text-right">L3强度</TableHeader>
                          <TableHeader>正式买点</TableHeader>
                          <TableHeader>阻断原因</TableHeader>
                        </TableRow>
                      </TableHead>
                      <TableBody>
                        {filteredSectorOverlayRows.length === 0 && (
                          <TableRow>
                            <TableCell colSpan={9}>
                              <EmptyState text="暂无重合候选" />
                            </TableCell>
                          </TableRow>
                        )}
                        {filteredSectorOverlayRows.map((row, i) => (
                          <TableRow key={`${row.code}-${i}`}>
                            <TableCell>{row.candidate_source || '--'}</TableCell>
                            <TableCell>
                              <LinkButton onClick={() => goStock(row.code)}>{row.name || '--'}</LinkButton>
                              <div className="text-xs text-zinc-500 dark:text-zinc-400">
                                {stockCodeText(row.code)}
                              </div>
                            </TableCell>
                            <TableCell>{row.sector_name || '--'}</TableCell>
                            <TableCell className="tabular-nums">{row.entry_date_text || '--'}</TableCell>
                            <TableCell className="text-right tabular-nums">
                              {score(row.mainline_intraday_score)}
                            </TableCell>
                            <TableCell className="text-right tabular-nums">{score(row.score)}</TableCell>
                            <TableCell className="text-right tabular-nums">
                              {pct(row.l3_rt_strong3_ratio)}
                            </TableCell>
                            <TableCell>{row.formal_buy_signal || '--'}</TableCell>
                            <TableCell className="max-w-[180px] truncate" title={row.block_reason}>
                              {row.block_reason || '--'}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                </section>
              </>
            )}

            {activeTab === 'theme' && (
              <>
                <section className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                  <div>
                    <SectionTitle
                      title="主题主线榜"
                      actions={
                        <span className="text-xs font-normal text-zinc-500 dark:text-zinc-400">
                          先看主题容器，再看主题里有哪些股票；默认优先主题分，其次看今日均涨跌与 L2 数
                        </span>
                      }
                      className="mb-2"
                    />
                    <div className="max-h-[360px] overflow-y-auto rounded-lg border border-zinc-200 dark:border-white/10">
                      <Table dense striped>
                        <TableHead>
                          <TableRow>
                            <TableHeader>主题</TableHeader>
                            <TableHeader>层级</TableHeader>
                            <TableHeader className="text-right">主题分</TableHeader>
                            <TableHeader className="text-right">股票数</TableHeader>
                            <TableHeader className="text-right">L2数</TableHeader>
                            <TableHeader className="text-right">
                              {`${latestTradeDate || '当前'}均涨跌`}
                            </TableHeader>
                            <TableHeader>{`${latestTradeDate || '当前'}领涨股`}</TableHeader>
                            <TableHeader>风险</TableHeader>
                          </TableRow>
                        </TableHead>
                        <TableBody>
                          {themeSummaryRows.length === 0 && (
                            <TableRow>
                              <TableCell colSpan={8}>
                                <EmptyState text="暂无主题主线" />
                              </TableCell>
                            </TableRow>
                          )}
                          {themeSummaryRows.map((row) => (
                            <TableRow
                              key={row.theme_key}
                              className={
                                'cursor-pointer ' +
                                (row.theme_key === selectedThemeKey ? 'bg-blue-50 dark:bg-blue-500/10' : '')
                              }
                              onClick={() => setSelectedThemeKey(row.theme_key)}
                            >
                              <TableCell>
                                <LinkButton onClick={() => setSelectedThemeKey(row.theme_key)}>
                                  {row.theme_title || '--'}
                                </LinkButton>
                              </TableCell>
                              <TableCell>{row.best_level || '--'}</TableCell>
                              <TableCell className="text-right tabular-nums">{score(row.max_theme_score)}</TableCell>
                              <TableCell className="text-right tabular-nums">{row.stock_count || 0}</TableCell>
                              <TableCell className="text-right tabular-nums">{row.l2_count || 0}</TableCell>
                              <TableCell className="text-right">
                                <ChangeValue
                                  value={
                                    Number.isFinite(Number(row.avg_current_change_pct))
                                      ? Number(row.avg_current_change_pct)
                                      : null
                                  }
                                />
                              </TableCell>
                              <TableCell>
                                {row.leader_name && row.leader_name !== '--' ? (
                                  <span>
                                    {row.leader_name} {percentValue(row.leader_current_change_pct)}
                                  </span>
                                ) : (
                                  '--'
                                )}
                              </TableCell>
                              <TableCell className="max-w-[120px] truncate" title={row.risk_mix}>
                                {row.risk_mix}
                              </TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </div>
                  </div>

                  <div>
                    <SectionTitle
                      title="主题成分股"
                      actions={
                        <span className="text-xs font-normal text-zinc-500 dark:text-zinc-400">
                          {selectedThemeHint}
                        </span>
                      }
                      className="mb-2"
                    />
                    <div className="max-h-[360px] overflow-y-auto rounded-lg border border-zinc-200 dark:border-white/10">
                      <Table dense striped>
                        <TableHead>
                          <TableRow>
                            <TableHeader>股票</TableHeader>
                            <TableHeader>层级</TableHeader>
                            <TableHeader className="text-right">主题分</TableHeader>
                            <TableHeader className="text-right">权重提示</TableHeader>
                            <TableHeader className="text-right">{`${latestTradeDate || '当前'}涨跌`}</TableHeader>
                            <TableHeader className="text-right">{`${latestTradeDate || '当前'}成交额`}</TableHeader>
                            <TableHeader>风险</TableHeader>
                            <TableHeader>建议</TableHeader>
                          </TableRow>
                        </TableHead>
                        <TableBody>
                          {selectedThemeRows.length === 0 && (
                            <TableRow>
                              <TableCell colSpan={8}>
                                <EmptyState text="暂无主题成分股" />
                              </TableCell>
                            </TableRow>
                          )}
                          {selectedThemeRows.map((row, i) => (
                            <TableRow key={`${row.code6 || row.code}-${i}`}>
                              <TableCell>
                                <LinkButton onClick={() => goStock(row.code6 || row.code)}>
                                  {row.display_name || '--'}
                                </LinkButton>
                                <div className="text-xs text-zinc-500 dark:text-zinc-400">
                                  {stockCodeText(row.code6 || row.code)}
                                </div>
                              </TableCell>
                              <TableCell>{row.observation_level || '--'}</TableCell>
                              <TableCell className="text-right tabular-nums">
                                {score(row.theme_candidate_score)}
                              </TableCell>
                              <TableCell className="text-right tabular-nums">
                                {score(row.theme_weight_hint, 2)}
                              </TableCell>
                              <TableCell className="text-right">
                                <ChangeValue
                                  value={
                                    Number.isFinite(Number(row.current_change_pct))
                                      ? Number(row.current_change_pct)
                                      : null
                                  }
                                />
                              </TableCell>
                              <TableCell className="text-right tabular-nums">
                                {amountText(row.current_amount)}
                              </TableCell>
                              <TableCell>{row.risk_label || '--'}</TableCell>
                              <TableCell className="max-w-[180px] truncate" title={row.suggested_usage}>
                                {row.suggested_usage || '--'}
                              </TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </div>
                  </div>
                </section>

                <section className="mt-4">
                  <SectionTitle
                    title="主题与策略叠加"
                    actions={
                      <span className="text-xs font-normal text-zinc-500 dark:text-zinc-400">
                        看主题标签如何影响 G2 候选排序
                      </span>
                    }
                    className="mb-2"
                  />
                  <div className="max-h-[320px] overflow-y-auto rounded-lg border border-zinc-200 dark:border-white/10">
                    <Table dense striped>
                      <TableHead>
                        <TableRow>
                          <TableHeader>来源</TableHeader>
                          <TableHeader>股票</TableHeader>
                          <TableHeader>主题</TableHeader>
                          <TableHeader>层级</TableHeader>
                          <TableHeader className="text-right">原始分</TableHeader>
                          <TableHeader className="text-right">叠加分</TableHeader>
                          <TableHeader>动作</TableHeader>
                          <TableHeader>风险</TableHeader>
                        </TableRow>
                      </TableHead>
                      <TableBody>
                        {themeOverlayRows.length === 0 && (
                          <TableRow>
                            <TableCell colSpan={8}>
                              <EmptyState text="暂无主题策略叠加" />
                            </TableCell>
                          </TableRow>
                        )}
                        {themeOverlayRows.map((row, i) => (
                          <TableRow key={`${row.code6 || row.code}-${i}`}>
                            <TableCell>{row.strategy_source || '--'}</TableCell>
                            <TableCell>
                              <LinkButton onClick={() => goStock(row.code6 || row.code)}>
                                {row.display_name || '--'}
                              </LinkButton>
                              <div className="text-xs text-zinc-500 dark:text-zinc-400">
                                {stockCodeText(row.code6 || row.code)}
                              </div>
                            </TableCell>
                            <TableCell className="max-w-[140px] truncate" title={row.theme_label}>
                              {row.theme_label || '--'}
                            </TableCell>
                            <TableCell>{row.observation_level || '--'}</TableCell>
                            <TableCell className="text-right tabular-nums">{score(row.raw_score)}</TableCell>
                            <TableCell className="text-right tabular-nums">{score(row.overlay_score)}</TableCell>
                            <TableCell>{row.theme_overlay_action || '--'}</TableCell>
                            <TableCell>{row.risk_label || '--'}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                </section>
              </>
            )}
          </Card>
        </>
      )}
    </div>
  )
}
