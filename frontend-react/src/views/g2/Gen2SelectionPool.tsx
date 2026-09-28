import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  getGen2RiskCoolShadowUpdateTask,
  getGen2SelectionPool,
  runGen2RiskCoolShadowUpdate,
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

const UPDATE_POLL_TIMEOUT_MS = 120000

const STAGE_OPTIONS = [
  { label: '全部', value: 'all' },
  { label: '可买', value: 'buyable' },
  { label: '主线过滤', value: 'alpha' },
  { label: '风控过滤', value: 'risk' },
  { label: '熔断', value: 'suspended' },
]

const QUALITY_MODE_OPTIONS = [
  { label: 'volume5', value: 'volume5' },
  { label: 'breakout', value: 'breakout' },
]

const V4_POOL_MODE_OPTIONS = [
  { label: 'D-1', value: 'd1' },
  { label: 'D', value: 'd' },
]

function todayText(): string {
  const d = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

function score(value: unknown, digits = 3): string {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(digits) : '--'
}

function pct(value: unknown): string {
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : '--'
}

type BadgeColor = Parameters<typeof Badge>[0]['color']

function tagColor(type: unknown): BadgeColor {
  const text = String(type || '')
  if (['success', 'emerald', 'green'].includes(text)) return 'emerald'
  if (['danger', 'error', 'red'].includes(text)) return 'red'
  if (['warning', 'warn', 'amber', 'yellow'].includes(text)) return 'amber'
  if (['primary', 'blue'].includes(text)) return 'blue'
  return 'zinc'
}

export default function Gen2SelectionPool() {
  const toast = useToast()
  const [loading, setLoading] = useState(false)
  const [updateLoading, setUpdateLoading] = useState(false)
  const [signalDate, setSignalDate] = useState(todayText())
  const [payload, setPayload] = useState<any>({})
  const [stageFilter, setStageFilter] = useState('all')
  const [v4PoolMode, setV4PoolMode] = useState('d1')
  const [qualityMode, setQualityMode] = useState('volume5')
  const updateTimer = useRef<number | null>(null)
  const updatePollStartedAt = useRef(0)

  const available = !!payload?.available
  const message = payload?.message || ''
  const strategyName =
    payload?.strategy_name || 'G2 第二代完整体：volume5主线 + 突破主线 + 板块扩散'
  const selectedDate = payload?.signal_date || ''
  const requestedDate = payload?.requested_date || signalDate || ''
  const dateResolution = payload?.date_resolution || null

  const dateResolutionNotice = useMemo(() => {
    const requested = String(requestedDate || '').trim()
    const selected = String(selectedDate || '').trim()
    if (!requested || !selected || requested === selected) return ''
    const reason = String((dateResolution as any)?.reason || '').trim()
    const extra = reason ? ` (${reason})` : ''
    if (requested > selected) return `Request date ${requested} not ready; fallback to ${selected}${extra}`
    return `Request date ${requested} differs from displayed date ${selected}`
  }, [requestedDate, selectedDate, dateResolution])

  const officialRebuild: any = payload?.official_rebuild || null
  const officialRebuildNotice = useMemo(() => {
    if (!officialRebuild || typeof officialRebuild !== 'object') return ''
    const status = String(officialRebuild.status || '').trim().toLowerCase()
    if (status === 'queued' || status === 'running') {
      const date = String(officialRebuild.signal_date || selectedDate || '--')
      return `Official rebuild ${status === 'queued' ? 'queued' : 'running'} (signal_date=${date})`
    }
    if ((status === 'completed' || status === 'failed') && officialRebuild.ok === false) {
      const date = String(officialRebuild.signal_date || selectedDate || '--')
      const err = String(officialRebuild.error || '').trim()
      return `Official rebuild failed (signal_date=${date})${err ? `: ${err}` : ''}`
    }
    return ''
  }, [officialRebuild, selectedDate])

  const alphaGate = payload?.alpha191_gate || ''
  const strategyMode = payload?.strategy_mode || alphaGate || ''
  const branchFreshness: any = payload?.branch_freshness || {}
  const branchFreshnessWarning =
    branchFreshness?.status === 'stale'
      ? String(
          branchFreshness?.text ||
            'Official branch is stale; live trading already uses latest-driven mainline',
        )
      : ''
  const pipeline: any[] = Array.isArray(payload?.pipeline) ? payload.pipeline : []
  const rows: any[] = Array.isArray(payload?.rows) ? payload.rows : []

  const triggerRows = useMemo(() => {
    if (Array.isArray(payload?.trigger_rows)) return payload.trigger_rows
    return rows.filter((row) => row.pass_v4_g2_trigger)
  }, [payload, rows])

  const completeRows = useMemo(() => {
    if (Array.isArray(payload?.complete_rows)) return payload.complete_rows
    return rows.filter(
      (row) =>
        row.pass_official_v2_live ||
        row.pass_mainline1 ||
        row.pass_mainline2 ||
        row.pass_breakout_stage ||
        row.buyable,
    )
  }, [payload, rows])

  const promotedRows = useMemo(() => {
    if (Array.isArray(payload?.promoted_rows)) return payload.promoted_rows
    return rows
  }, [payload, rows])

  const promotedVisibleRows = useMemo(() => {
    if (stageFilter === 'buyable') return promotedRows.filter((row) => row.buyable)
    if (stageFilter === 'alpha')
      return promotedRows.filter((row) => row.pass_risk_cool && !row.pass_mainline1 && !row.pass_mainline2)
    if (stageFilter === 'risk')
      return promotedRows.filter((row) => row.pass_v4_g2_trigger && !row.pass_risk_cool)
    if (stageFilter === 'suspended')
      return promotedRows.filter((row) =>
        ['suspended_by_two_stop_cd3', 'suspended_by_stop_cd5'].includes(row.shadow_status),
      )
    return promotedRows
  }, [promotedRows, stageFilter])

  const qualityRows: any[] = Array.isArray(payload?.quality_rows) ? payload.quality_rows : []
  const qualityVisibleRows = useMemo(
    () => qualityRows.filter((row) => String(row.quality_family || 'volume5') === qualityMode),
    [qualityRows, qualityMode],
  )
  const qualityVisiblePassCount = qualityVisibleRows.filter((row) => row.pass_quality).length

  const v4D1Rows = useMemo(() => {
    if (Array.isArray(payload?.v4_d1_rows)) return payload.v4_d1_rows
    return rows.filter((row) => row.pass_v4_pool)
  }, [payload, rows])
  const v4DRows: any[] = Array.isArray(payload?.v4_d_rows) ? payload.v4_d_rows : []
  const v4DisplayRows = v4PoolMode === 'd' ? v4DRows : v4D1Rows
  const v4PoolDateLabel = `${v4PoolMode === 'd' ? 'D' : 'D-1'} ${v4DisplayRows[0]?.v4_pool_date || '--'}`

  const fallbackNotice = useMemo(() => {
    const title = String(payload?.fallback_notice || '').trim()
    if (title) return title
    if (!requestedDate || !selectedDate || requestedDate === selectedDate) return ''
    return `Request date ${requestedDate} has no matching selection data; fallback to ${selectedDate}`
  }, [payload, requestedDate, selectedDate])

  const dataFreshness: any = payload?.data_freshness || {}
  const freshnessWarning = !!(
    dataFreshness?.latest_daily_date &&
    selectedDate &&
    dataFreshness.latest_daily_date > selectedDate
  )
  const freshnessText = freshnessWarning ? `日线${dataFreshness.latest_daily_date}` : '同步'
  const buyableCount = promotedRows.filter((row) => row.buyable).length

  const fetchData = useCallback(async () => {
    setLoading(true)
    try {
      const data = (await getGen2SelectionPool({
        signal_date: signalDate || undefined,
        limit: 260,
      })) as any
      setPayload(data || {})
    } catch {
      // 拦截器已 toast
    } finally {
      setLoading(false)
    }
  }, [signalDate])

  const pollUpdateTask = useCallback(
    async (taskId: string) => {
      try {
        const task = (await getGen2RiskCoolShadowUpdateTask(taskId)) as any
        const status = String(task?.status || '').toLowerCase()
        if (['done', 'completed', 'success'].includes(status)) {
          setUpdateLoading(false)
          updatePollStartedAt.current = 0
          toast.success('策略股票池已更新')
          await fetchData()
          return
        }
        if (['failed', 'missing', 'cancelled', 'canceled'].includes(status)) {
          setUpdateLoading(false)
          updatePollStartedAt.current = 0
          toast.error(task?.error || 'Selection pool update task ended unexpectedly')
          return
        }
        if (updatePollStartedAt.current && Date.now() - updatePollStartedAt.current > UPDATE_POLL_TIMEOUT_MS) {
          setUpdateLoading(false)
          updatePollStartedAt.current = 0
          toast.info('Selection pool update is still running; stopped waiting on page')
          return
        }
        updateTimer.current = window.setTimeout(() => pollUpdateTask(taskId), 1500)
      } catch {
        setUpdateLoading(false)
        updatePollStartedAt.current = 0
        toast.error('Failed to query selection pool update status')
      }
    },
    [fetchData, toast],
  )

  const updatePool = useCallback(async () => {
    if (updateTimer.current) {
      window.clearTimeout(updateTimer.current)
      updateTimer.current = null
    }
    updatePollStartedAt.current = Date.now()
    setUpdateLoading(true)
    try {
      const task = (await runGen2RiskCoolShadowUpdate({
        signal_date: signalDate || undefined,
        pool_rank: 200,
        alpha191_gate: 'g2_v2_complete',
      })) as any
      if (task?.status === 'failed') {
        setUpdateLoading(false)
        toast.error(task.error || 'Selection pool update failed')
        return
      }
      if (task?.task_id) {
        await pollUpdateTask(task.task_id)
      } else {
        setUpdateLoading(false)
        await fetchData()
      }
    } catch {
      setUpdateLoading(false)
    } finally {
      updatePollStartedAt.current = 0
    }
  }, [signalDate, pollUpdateTask, fetchData, toast])

  const copyCode = useCallback(
    async (code: unknown) => {
      const text = String(code || '').match(/\d{6}/)?.[0] || ''
      if (!text) return
      await navigator.clipboard.writeText(text)
      toast.success(`Copied ${text}`)
    },
    [toast],
  )

  useEffect(() => {
    fetchData()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(
    () => () => {
      if (updateTimer.current) window.clearTimeout(updateTimer.current)
    },
    [],
  )

  const CopyCodeCell = ({ code }: { code: unknown }) => (
    <button
      type="button"
      title="复制代码"
      className="inline-flex items-center gap-1 text-blue-600 hover:underline dark:text-blue-400 tabular-nums"
      onClick={() => copyCode(code)}
    >
      <span>{String(code || '--')}</span>
      <svg viewBox="0 0 20 20" fill="currentColor" className="size-3.5 opacity-70">
        <path d="M7 3.5A1.5 1.5 0 0 1 8.5 2h3.879a1.5 1.5 0 0 1 1.06.44l3.122 3.12A1.5 1.5 0 0 1 17 6.622V12.5a1.5 1.5 0 0 1-1.5 1.5h-1v-3.379a3 3 0 0 0-.879-2.121L10.5 5.379A3 3 0 0 0 8.379 4.5H7v-1Z" />
        <path d="M4.5 6A1.5 1.5 0 0 0 3 7.5v9A1.5 1.5 0 0 0 4.5 18h7a1.5 1.5 0 0 0 1.5-1.5v-9A1.5 1.5 0 0 0 11.5 6h-7Z" />
      </svg>
    </button>
  )

  const Segmented = ({
    options,
    value,
    onChange,
  }: {
    options: { label: string; value: string }[]
    value: string
    onChange: (v: string) => void
  }) => (
    <div className="inline-flex overflow-hidden rounded-lg border border-zinc-200 dark:border-white/10">
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          className={
            'px-3 py-1 text-xs font-medium transition-colors ' +
            (value === opt.value
              ? 'bg-blue-600 text-white'
              : 'bg-white text-zinc-600 hover:bg-zinc-50 dark:bg-white/5 dark:text-zinc-300 dark:hover:bg-white/10')
          }
          onClick={() => onChange(opt.value)}
        >
          {opt.label}
        </button>
      ))}
    </div>
  )

  const statusItems = [
    { label: '请求日期', value: requestedDate || '--' },
    { label: '实际展示', value: selectedDate || '--' },
    { label: '策略模式', value: strategyMode || '--' },
    { label: '可买观察', value: String(buyableCount), up: true },
    { label: '数据状态', value: freshnessText, warn: freshnessWarning },
  ]

  return (
    <div className="flex flex-col gap-4 p-4">
      <PageHeader
        title="策略股票池"
        subtitle={`${strategyName} 路径 ${selectedDate || '--'}`}
        actions={
          <>
            <Input
              type="date"
              value={signalDate}
              onChange={(e) => setSignalDate(e.target.value)}
              className="w-40"
            />
            <Button color="blue" disabled={loading} onClick={fetchData}>
              {loading ? '查询中…' : '查询'}
            </Button>
            <Button disabled={updateLoading} onClick={updatePool}>
              {updateLoading ? '更新中…' : '更新当日实时池'}
            </Button>
          </>
        }
      />

      {loading && <PageLoading />}

      {!available && !loading && (
        <Alert color="amber">
          <div>{message || 'Selection pool data is unavailable'}</div>
        </Alert>
      )}

      {available && (
        <>
          {fallbackNotice && (
            <Alert color="amber">
              <div>{fallbackNotice}</div>
            </Alert>
          )}
          {branchFreshnessWarning && (
            <Alert color="amber">
              <div>{branchFreshnessWarning}</div>
            </Alert>
          )}
          {dateResolutionNotice && (
            <Alert color="amber">
              <div>{dateResolutionNotice}</div>
            </Alert>
          )}
          {officialRebuildNotice && (
            <Alert color="amber">
              <div>{officialRebuildNotice}</div>
            </Alert>
          )}

          <section className="grid grid-cols-2 gap-3 xl:grid-cols-6">
            {pipeline.map((step, index) => (
              <StatCard
                key={step.key || index}
                label={
                  <span className="flex items-center gap-1.5">
                    <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-blue-50 text-xs font-extrabold text-blue-700 dark:bg-blue-500/15 dark:text-blue-300">
                      {index + 1}
                    </span>
                    {step.label}
                  </span>
                }
                value={step.count ?? 0}
                hint={step.note}
              />
            ))}
          </section>

          <section className="grid grid-cols-2 gap-3 xl:grid-cols-5">
            {statusItems.map((item) => (
              <StatCard
                key={item.label}
                label={item.label}
                value={item.value}
                tone={item.up ? 'up' : item.warn ? 'warning' : 'default'}
              />
            ))}
          </section>

          <Card>
            <SectionTitle
              title={
                <>
                  <span>当日触发信号</span>
                  <span className="mt-0.5 block text-[13px] font-normal text-zinc-500 dark:text-zinc-400">
                    {selectedDate || '--'} 盘中已触发 G2 30m 底分型放量确认的个股
                  </span>
                </>
              }
              actions={<Badge color="zinc">{triggerRows.length} 只</Badge>}
            />
            <div className="max-h-[360px] overflow-y-auto">
              <Table dense>
                <TableHead>
                  <TableRow>
                    <TableHeader>状态</TableHeader>
                    <TableHeader>代码</TableHeader>
                    <TableHeader>名称</TableHeader>
                    <TableHeader>盘中确认</TableHeader>
                    <TableHeader className="text-right">V4排名</TableHeader>
                    <TableHeader className="text-right">盘中涨幅</TableHeader>
                    <TableHeader className="text-right">30m量比</TableHeader>
                    <TableHeader>路径</TableHeader>
                    <TableHeader>记录说明</TableHeader>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {triggerRows.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={9}>
                        <EmptyState text="暂无当日触发信号" />
                      </TableCell>
                    </TableRow>
                  )}
                  {triggerRows.map((row, i) => (
                    <TableRow key={`${row.code}-${i}`}>
                      <TableCell>
                        <Badge color={tagColor(row.stage_type)}>{row.stage_label || '--'}</Badge>
                      </TableCell>
                      <TableCell>
                        <CopyCodeCell code={row.code} />
                      </TableCell>
                      <TableCell>{row.name || '--'}</TableCell>
                      <TableCell className="tabular-nums">{row.confirm_datetime || '--'}</TableCell>
                      <TableCell className="text-right tabular-nums">{row.v4_rank || '--'}</TableCell>
                      <TableCell className="text-right">
                        <ChangeValue
                          value={Number.isFinite(Number(row.rt_return_pct)) ? Number(row.rt_return_pct) : null}
                          digits={1}
                        />
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{score(row.amount_ratio, 2)}</TableCell>
                      <TableCell>
                        <div className="flex items-center gap-1">
                          <Badge color={row.pass_risk_cool ? 'emerald' : 'red'}>风控</Badge>
                          <Badge color={row.pass_mainline1 ? 'emerald' : 'zinc'}>volume5</Badge>
                          <Badge color={row.pass_mainline2 ? 'emerald' : 'zinc'}>突破</Badge>
                          <Badge color={row.buyable ? 'emerald' : 'zinc'}>可买</Badge>
                        </div>
                      </TableCell>
                      <TableCell className="max-w-[260px] truncate" title={row.reason_text}>
                        {row.reason_text || '--'}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </Card>

          <Card>
            <SectionTitle
              title={
                <>
                  <span>完整第二代</span>
                  <span className="mt-0.5 block text-[13px] font-normal text-zinc-500 dark:text-zinc-400">
                    展示第二代完整体，含 volume5 主线与突破主线选出的全部股票
                  </span>
                </>
              }
              actions={<Badge color="zinc">{completeRows.length} 只</Badge>}
            />
            <div className="max-h-[420px] overflow-y-auto">
              <Table dense>
                <TableHead>
                  <TableRow>
                    <TableHeader>体系</TableHeader>
                    <TableHeader>代码</TableHeader>
                    <TableHeader>名称</TableHeader>
                    <TableHeader>盘中确认</TableHeader>
                    <TableHeader className="text-right">V4排名</TableHeader>
                    {qualityMode === 'volume5' && <TableHeader className="text-right">volume5分</TableHeader>}
                    {qualityMode === 'volume5' && <TableHeader className="text-right">volume5排名</TableHeader>}
                    <TableHeader className="text-right">L3强度%</TableHeader>
                    <TableHeader className="text-right">60日低点涨幅%</TableHeader>
                    <TableHeader>记录说明</TableHeader>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {completeRows.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={10}>
                        <EmptyState text="暂无完整主线信号" />
                      </TableCell>
                    </TableRow>
                  )}
                  {completeRows.map((row, i) => (
                    <TableRow key={`${row.code}-${i}`}>
                      <TableCell>
                        <div className="flex items-center gap-1">
                          {row.pass_mainline1 && <Badge color="emerald">volume5</Badge>}
                          {row.pass_mainline2 && <Badge color="amber">突破+板块</Badge>}
                          {row.pass_breakout_stage && (
                            <Badge color="amber">{row.breakout_stage_label || '二突观察'}</Badge>
                          )}
                          {!row.pass_mainline1 && !row.pass_mainline2 && !row.pass_breakout_stage && (
                            <Badge color="zinc">观察</Badge>
                          )}
                        </div>
                      </TableCell>
                      <TableCell>
                        <CopyCodeCell code={row.code} />
                      </TableCell>
                      <TableCell>{row.name || '--'}</TableCell>
                      <TableCell className="tabular-nums">{row.confirm_datetime || '--'}</TableCell>
                      <TableCell className="text-right tabular-nums">{row.v4_rank || '--'}</TableCell>
                      {qualityMode === 'volume5' && (
                        <TableCell className="text-right tabular-nums">
                          {score(row.alpha191_volume5_score)}
                        </TableCell>
                      )}
                      {qualityMode === 'volume5' && (
                        <TableCell className="text-right tabular-nums">
                          {row.alpha191_volume5_rank_in_day || '--'}
                        </TableCell>
                      )}
                      <TableCell className="text-right tabular-nums">{pct(row.l3_rt_strong3_ratio)}</TableCell>
                      <TableCell className="text-right tabular-nums">{pct(row.runup_from_60d_low)}</TableCell>
                      <TableCell className="max-w-[260px] truncate" title={row.reason_text}>
                        {row.reason_text || '--'}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </Card>

          <Card>
            <SectionTitle
              title={
                <>
                  <span>晋级池</span>
                  {message && (
                    <span className="mt-0.5 block text-[13px] font-normal text-zinc-500 dark:text-zinc-400">
                      {message}
                    </span>
                  )}
                </>
              }
              actions={<Segmented options={STAGE_OPTIONS} value={stageFilter} onChange={setStageFilter} />}
            />
            <div className="max-h-[560px] overflow-y-auto">
              <Table dense>
                <TableHead>
                  <TableRow>
                    <TableHeader>状态</TableHeader>
                    <TableHeader>代码</TableHeader>
                    <TableHeader>名称</TableHeader>
                    <TableHeader>盘中确认</TableHeader>
                    <TableHeader>路径</TableHeader>
                    <TableHeader className="text-right">V4排名</TableHeader>
                    <TableHeader className="text-right">主线分</TableHeader>
                    <TableHeader className="text-right">主线排名</TableHeader>
                    <TableHeader className="text-right">L3强度%</TableHeader>
                    <TableHeader className="text-right">60日低点涨幅%</TableHeader>
                    <TableHeader className="text-right">盘中涨幅</TableHeader>
                    <TableHeader className="text-right">30m量比</TableHeader>
                    <TableHeader>记录说明</TableHeader>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {promotedVisibleRows.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={13}>
                        <EmptyState text="暂无选股池记录" />
                      </TableCell>
                    </TableRow>
                  )}
                  {promotedVisibleRows.map((row, i) => (
                    <TableRow key={`${row.code}-${i}`}>
                      <TableCell>
                        <Badge color={tagColor(row.stage_type)}>{row.stage_label || '--'}</Badge>
                      </TableCell>
                      <TableCell>
                        <CopyCodeCell code={row.code} />
                      </TableCell>
                      <TableCell>{row.name || '--'}</TableCell>
                      <TableCell className="tabular-nums">{row.confirm_datetime || '--'}</TableCell>
                      <TableCell>
                        <div className="flex flex-wrap items-center gap-1">
                          <Badge color={row.pass_v4_pool ? 'emerald' : 'zinc'}>V4筛选</Badge>
                          <Badge color={row.pass_v4_g2_trigger ? 'emerald' : 'zinc'}>G2触发</Badge>
                          <Badge color={row.pass_risk_cool ? 'emerald' : 'red'}>风控</Badge>
                          <Badge color={row.pass_mainline1 ? 'emerald' : 'amber'}>主线一</Badge>
                          <Badge color={row.pass_mainline2 || row.pass_breakout_stage ? 'emerald' : 'zinc'}>
                            主线二
                          </Badge>
                          {row.pass_volume5_trigger && <Badge color="emerald">v5触发</Badge>}
                          {row.pass_breakout_stage && (
                            <Badge color="amber">{row.breakout_stage_label || '二突层级'}</Badge>
                          )}
                          <Badge color={row.buyable ? 'emerald' : 'zinc'}>可买</Badge>
                        </div>
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{row.v4_rank || '--'}</TableCell>
                      <TableCell className="text-right tabular-nums">{score(row.alpha191_volume5_score)}</TableCell>
                      <TableCell className="text-right tabular-nums">
                        {row.alpha191_volume5_rank_in_day || '--'}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{pct(row.l3_rt_strong3_ratio)}</TableCell>
                      <TableCell className="text-right tabular-nums">{pct(row.runup_from_60d_low)}</TableCell>
                      <TableCell className="text-right">
                        <ChangeValue
                          value={Number.isFinite(Number(row.rt_return_pct)) ? Number(row.rt_return_pct) : null}
                          digits={1}
                        />
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{score(row.amount_ratio, 2)}</TableCell>
                      <TableCell className="max-w-[260px] truncate" title={row.reason_text}>
                        {row.reason_text || '--'}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </Card>

          <Card>
            <SectionTitle
              title={
                <>
                  <span>候选股质量排序</span>
                  <span className="mt-0.5 block text-[13px] font-normal text-zinc-500 dark:text-zinc-400">
                    D-1 V4参考池；Alpha150/070/095/132/144 计算 volume5，再筛 keep80 + runup&lt;=100%
                  </span>
                </>
              }
              actions={
                <div className="flex flex-wrap items-center gap-2">
                  <Segmented options={QUALITY_MODE_OPTIONS} value={qualityMode} onChange={setQualityMode} />
                  <Badge color="emerald">通过 {qualityVisiblePassCount} 只</Badge>
                  <Badge color="zinc">{qualityVisibleRows.length} 只</Badge>
                </div>
              }
            />
            <div className="max-h-[520px] overflow-y-auto">
              <Table dense>
                <TableHead>
                  <TableRow>
                    <TableHeader>质量</TableHeader>
                    <TableHeader>代码</TableHeader>
                    <TableHeader>名称</TableHeader>
                    <TableHeader className="text-right">V4排名</TableHeader>
                    <TableHeader className="text-right">V4总分</TableHeader>
                    <TableHeader className="text-right">volume5分</TableHeader>
                    <TableHeader className="text-right">volume5排名</TableHeader>
                    {qualityMode === 'volume5' && <TableHeader className="text-right">keep80阈值</TableHeader>}
                    {qualityMode === 'volume5' && <TableHeader className="text-center">keep80</TableHeader>}
                    {qualityMode === 'volume5' && <TableHeader className="text-right">60日低点涨幅%</TableHeader>}
                    {qualityMode === 'volume5' && <TableHeader className="text-center">runup</TableHeader>}
                    {qualityMode === 'breakout' && <TableHeader>层级</TableHeader>}
                    {qualityMode === 'breakout' && <TableHeader>大阳线</TableHeader>}
                    {qualityMode === 'breakout' && <TableHeader className="text-right">距箱体</TableHeader>}
                    {qualityMode === 'breakout' && <TableHeader className="text-right">箱体宽度</TableHeader>}
                    {qualityMode === 'breakout' && <TableHeader className="text-right">大阳量比</TableHeader>}
                    <TableHeader>因子日</TableHeader>
                    <TableHeader>说明</TableHeader>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {qualityVisibleRows.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={18}>
                        <EmptyState text="暂无候选股质量排序" />
                      </TableCell>
                    </TableRow>
                  )}
                  {qualityVisibleRows.map((row, i) => (
                    <TableRow key={`${row.code}-${i}`}>
                      <TableCell>
                        <Badge color={tagColor(row.quality_type)}>{row.quality_label || '--'}</Badge>
                      </TableCell>
                      <TableCell>
                        <CopyCodeCell code={row.code} />
                      </TableCell>
                      <TableCell>{row.name || '--'}</TableCell>
                      <TableCell className="text-right tabular-nums">{row.v4_rank || '--'}</TableCell>
                      <TableCell className="text-right tabular-nums">{score(row.v4_score)}</TableCell>
                      <TableCell className="text-right tabular-nums">{score(row.alpha191_volume5_score)}</TableCell>
                      <TableCell className="text-right tabular-nums">
                        {row.alpha191_volume5_rank_in_day || '--'}
                      </TableCell>
                      {qualityMode === 'volume5' && (
                        <TableCell className="text-right tabular-nums">
                          {score(row.alpha191_gate_threshold)}
                        </TableCell>
                      )}
                      {qualityMode === 'volume5' && (
                        <TableCell className="text-center">
                          <Badge color={row.pass_keep80 ? 'emerald' : 'amber'}>
                            {row.pass_keep80 ? '通过' : '未过'}
                          </Badge>
                        </TableCell>
                      )}
                      {qualityMode === 'volume5' && (
                        <TableCell className="text-right tabular-nums">{pct(row.runup_from_60d_low)}</TableCell>
                      )}
                      {qualityMode === 'volume5' && (
                        <TableCell className="text-center">
                          <Badge color={row.pass_runup ? 'emerald' : 'amber'}>
                            {row.pass_runup ? '通过' : '未过'}
                          </Badge>
                        </TableCell>
                      )}
                      {qualityMode === 'breakout' && <TableCell>{row.breakout_stage_label || '--'}</TableCell>}
                      {qualityMode === 'breakout' && <TableCell>{row.setup_big_bull_date || '--'}</TableCell>}
                      {qualityMode === 'breakout' && (
                        <TableCell className="text-right tabular-nums">{pct(row.close_vs_box_top)}</TableCell>
                      )}
                      {qualityMode === 'breakout' && (
                        <TableCell className="text-right tabular-nums">{pct(row.setup_box_range)}</TableCell>
                      )}
                      {qualityMode === 'breakout' && (
                        <TableCell className="text-right tabular-nums">
                          {score(row.setup_big_bull_amount_ratio, 2)}
                        </TableCell>
                      )}
                      <TableCell className="tabular-nums">{row.factor_date || '--'}</TableCell>
                      <TableCell className="max-w-[240px] truncate" title={row.reason_text}>
                        {row.reason_text || '--'}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </Card>

          <Card>
            <SectionTitle
              title={
                <>
                  <span>V4参考选股池</span>
                  <span className="mt-0.5 block text-[13px] font-normal text-zinc-500 dark:text-zinc-400">
                    {v4PoolDateLabel} V4 rank00；D-1 用于 G2 盘中扫描入口，D 日用于复盘对照
                  </span>
                </>
              }
              actions={
                <div className="flex items-center gap-2">
                  <Segmented options={V4_POOL_MODE_OPTIONS} value={v4PoolMode} onChange={setV4PoolMode} />
                  <Badge color="zinc">{v4DisplayRows.length} 只</Badge>
                </div>
              }
            />
            <div className="max-h-[520px] overflow-y-auto">
              <Table dense>
                <TableHead>
                  <TableRow>
                    <TableHeader className="text-right">排名</TableHeader>
                    <TableHeader>代码</TableHeader>
                    <TableHeader>名称</TableHeader>
                    <TableHeader className="text-right">V4总分</TableHeader>
                    <TableHeader className="text-right">5日动量</TableHeader>
                    <TableHeader className="text-right">10日动量</TableHeader>
                    <TableHeader className="text-right">20日动量</TableHeader>
                    <TableHeader className="text-right">量比</TableHeader>
                    <TableHeader className="text-right">10日波动</TableHeader>
                    <TableHeader>池日期</TableHeader>
                    <TableHeader>说明</TableHeader>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {v4DisplayRows.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={11}>
                        <EmptyState
                          text={v4PoolMode === 'd1' ? '暂无 D-1 V4 参考选股' : '暂无 D 日 V4 参考选股'}
                        />
                      </TableCell>
                    </TableRow>
                  )}
                  {v4DisplayRows.map((row, i) => (
                    <TableRow key={`${row.code}-${i}`}>
                      <TableCell className="text-right tabular-nums">{row.v4_rank || '--'}</TableCell>
                      <TableCell>
                        <CopyCodeCell code={row.code} />
                      </TableCell>
                      <TableCell>{row.name || '--'}</TableCell>
                      <TableCell className="text-right tabular-nums">{score(row.v4_score)}</TableCell>
                      <TableCell className="text-right tabular-nums">{pct(row.mom5)}</TableCell>
                      <TableCell className="text-right tabular-nums">{pct(row.mom10)}</TableCell>
                      <TableCell className="text-right tabular-nums">{pct(row.mom20)}</TableCell>
                      <TableCell className="text-right tabular-nums">{score(row.vol_ratio, 2)}</TableCell>
                      <TableCell className="text-right tabular-nums">{pct(row.vol10)}</TableCell>
                      <TableCell className="tabular-nums">{row.v4_pool_date || '--'}</TableCell>
                      <TableCell className="max-w-[280px] truncate" title={row.reason_text}>
                        {row.reason_text || '--'}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </Card>
        </>
      )}
    </div>
  )
}
