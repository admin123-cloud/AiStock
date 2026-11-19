import { useCallback, useEffect, useMemo, useState } from 'react'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Input } from '@/components/catalyst/input'
import { Card, EmptyState, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import {
  dateTime,
  getIncidents,
  getTasks,
  statusLabel,
  statusTone,
  type OperationsBoard,
  type OperationsIncident,
  type OperationsTask,
} from '@/api/operations'

const badgeColor = { danger: 'red', success: 'emerald', warning: 'amber' } as const

const recoveryStage = (state?: string) =>
  ({ checking_source: '核验5分钟源数据', writing: '写入候选', validating: '校验候选与源数据', blocked: '已阻断，需排查' })[state || ''] || state || ''

const recoveryPeriod = (key: string) => {
  const match = /^(15|30|60):(\d{4})(\d{2})$/.exec(key)
  return match ? `${match[2]}年${match[3]}月 · ${match[1]}分钟` : key
}

const isRepairStopped = (status?: string) =>
  ['budget_exhausted', 'no_progress_requires_review', 'requires_review'].includes(status || '')

type Scope = 'core' | 'external' | 'retired'
type Filter = 'all' | 'attention' | 'running' | 'disabled'

export default function TaskCenter() {
  const [board, setBoard] = useState<OperationsBoard>({})
  const [events, setEvents] = useState<OperationsIncident[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  const [search, setSearch] = useState('')
  const [scope, setScope] = useState<Scope>('core')
  const [group, setGroup] = useState('delivery')
  const [eventPage, setEventPage] = useState(1)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  const load = useCallback(async () => {
    if (loading) return
    setLoading(true)
    setError('')
    try {
      const [tasks, incidents] = await Promise.all([getTasks(), getIncidents()])
      setBoard(tasks)
      const list = incidents.incidents || []
      setEvents(list)
      setEventPage((page) => Math.min(page, Math.max(1, Math.ceil(list.length / 10))))
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '请求失败')
    } finally {
      setLoading(false)
    }
  }, [loading])

  useEffect(() => {
    void load()
    const timer = setInterval(() => {
      if (!document.hidden) void load()
    }, 10000)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const repairStopped = useMemo(
    () => (board.operations_publisher?.repair?.held || []).filter((x) => isRepairStopped(x.status)),
    [board]
  )

  const metrics = [
    { label: 'Windows已登记', value: board.summary?.registered_windows },
    { label: '运行中', value: board.summary?.running },
    { label: '最近执行失败', value: board.summary?.failed },
    { label: '历史 / 退役入口', value: board.summary?.retired },
  ]

  const visibleGroups = (board.groups || []).filter((x) =>
    scope === 'core' ? !['external', 'retired'].includes(x.id) : x.id === scope
  )
  const groupRows = (board.tasks || []).filter((x) => x.group === group)
  const groupTitle = (board.groups || []).find((x) => x.id === group)?.label || '后台任务'

  const changeScope = (next: Scope) => {
    setScope(next)
    setGroup(next === 'core' ? 'delivery' : next)
    setFilter('all')
    setSearch('')
  }

  const filtered = groupRows.filter((x) => {
    const passFilter =
      filter === 'all' ||
      (filter === 'attention'
        ? ['failed', 'unknown', 'not_observed'].includes(x.status || '') ||
          ['blocked', 'stale', 'degraded'].includes(x.business_status || '')
        : x.status === filter)
    const q = search.toLowerCase()
    const passSearch = `${x.name} ${x.title || ''} ${x.purpose || ''} ${x.produces || ''}`.toLowerCase().includes(q)
    return passFilter && passSearch
  })

  const eventsOnPage = events.slice((eventPage - 1) * 10, eventPage * 10)
  const totalEventPages = Math.ceil(events.length / 10)

  const meta = 'text-xs text-zinc-500 dark:text-zinc-400'
  const th = 'px-3 py-2 text-left text-xs font-semibold text-zinc-500 dark:text-zinc-400'
  const td = 'px-3 py-2 text-sm text-zinc-700 dark:text-zinc-300'

  const renderRow = (row: OperationsTask) => {
    const open = !!expanded[row.name]
    return (
      <>
        <tr key={row.name} className={open ? 'bg-zinc-500/5 dark:bg-white/5' : ''}>
          <td className={td}>
            <button
              type="button"
              className="mr-1 inline-flex size-6 items-center justify-center rounded border border-zinc-950/10 text-xs text-zinc-600 dark:border-white/15 dark:text-zinc-300"
              aria-label={open ? '收起' : '展开'}
              onClick={() => setExpanded((prev) => ({ ...prev, [row.name]: !prev[row.name] }))}
            >
              {open ? '−' : '+'}
            </button>
            <strong className="text-zinc-950 dark:text-white">{row.title || row.name}</strong>
            <p className={`${meta} mt-0.5`}>{row.purpose}</p>
          </td>
          <td className={td}>{row.executor}</td>
          <td className={td}>
            <Badge color={badgeColor[statusTone(error ? 'unknown' : row.status || 'unknown')]}>
              {statusLabel(error ? 'unknown' : row.status || 'unknown')}
            </Badge>
            {row.scheduler_paused && <small className="ml-1"> 调度已暂停</small>}
            {row.status === 'running' && row.running_seconds != null && <small> {row.running_seconds}秒</small>}
          </td>
          <td className={td}>{statusLabel(row.business_status || '')}</td>
          <td className={td}>{dateTime(row.last_run)}</td>
          <td className={td}>{dateTime(row.next_run)}</td>
          <td className={`${td} tabular-nums`}>{row.last_result ?? ''}</td>
        </tr>
        {open && (
          <tr key={`${row.name}-detail`} className="bg-zinc-500/5 dark:bg-white/5">
            <td colSpan={7} className="px-4 py-4">
              <div className="grid gap-6 text-sm text-zinc-700 sm:grid-cols-2 dark:text-zinc-300">
                <div>
                  <strong className="text-zinc-950 dark:text-white">执行责任</strong>
                  <p className="mt-1">{row.purpose}</p>
                  <p>执行时机：{row.schedule_description}</p>
                  <p>实际计划：{row.window || '以宿主机触发器为准'}</p>
                  <p className={meta}>登记名称：{row.name}</p>
                  <p>最近成功：{dateTime(row.last_success)}</p>
                  <p>最近完成：{dateTime(row.last_finished)} · 耗时 {row.duration_seconds ?? '—'} 秒</p>
                  <p>本进程执行 {row.runs ?? '—'} 次 · 失败 {row.failures ?? '—'} 次</p>
                  {row.last_schedule_warning && (
                    <p>{row.last_schedule_warning} · {dateTime(row.last_warning_at)}</p>
                  )}
                </div>
                <div>
                  <strong className="text-zinc-950 dark:text-white">产物与失败影响</strong>
                  <p className="mt-1">{row.produces}</p>
                  <p>{row.failure_impact}</p>
                  <p>{row.business_reason || '执行完成不代表数据验收通过'}</p>
                  {row.consolidation_note && <p>整合说明：{row.consolidation_note}</p>}
                  {row.replacement && <p>接管入口：{row.replacement}</p>}
                  {!!row.ingestion?.lanes?.length && (
                    <div className="mt-2">
                      <strong className="text-zinc-950 dark:text-white">盘中采集通道</strong>
                      <p>
                        记录时间：{dateTime(row.ingestion.generated_at)} ·{' '}
                        {row.ingestion.phase === 'running' ? '运行记录' : '已停止'} · 快照目标{' '}
                        {row.ingestion.snapshot_target_seconds}秒
                      </p>
                      {row.ingestion.lanes.map((lane) => (
                        <p key={lane.name}>
                          {lane.name === 'snapshot' ? '全市场快照' : '分钟K线'}：{statusLabel(lane.status || '')} · 最近耗时{' '}
                          {lane.duration_seconds ?? '—'}秒
                          {lane.running_seconds != null && ` · 已运行 ${lane.running_seconds}秒`}
                          {!!lane.result?.universe_count &&
                            ` · 源时间在5分钟内 ${lane.result.source_fresh_300s}/${lane.result.universe_count}（含指数，豁免待核对）`}
                          {lane.error && ` · ${lane.error}`}
                          {lane.result?.errors && !!Object.keys(lane.result.errors).length && ` · ${JSON.stringify(lane.result.errors)}`}
                        </p>
                      ))}
                      <p className={meta}>记录时间与源时间需分别核对；历史运行记录不代表当前在线。</p>
                    </div>
                  )}
                  {row.error && (
                    <p role="alert" className="mt-2 text-red-600 dark:text-red-400">{row.error}</p>
                  )}
                </div>
              </div>
            </td>
          </tr>
        )}
      </>
    )
  }

  return (
    <div className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
      <PageHeader
        title="后台任务与健康"
        subtitle={
          <>
            每10秒自动刷新 · 最近读取 {dateTime(board.generated_at)} ·{' '}
            {board.api_runtime?.source === 'live' ? 'API实时状态' : '已发布快照'}
          </>
        }
        actions={
          <Button outline onClick={() => void load()} disabled={loading}>
            {loading ? '刷新中…' : '刷新状态'}
          </Button>
        }
      />

      {error && (
        <div role="alert" className="mb-4 rounded-lg border border-red-500/20 bg-red-50/70 px-4 py-3 text-sm text-red-700 dark:border-red-500/20 dark:bg-red-500/5 dark:text-red-400">
          {error}；下方保留上次读取结果。
        </div>
      )}

      <Card aria-label="服务启动与健康" className="mb-4">
        <SectionTitle
          title="服务启动与健康"
          actions={
            <Badge color={!error && board.api_runtime?.ready ? 'emerald' : 'red'}>
              {error
                ? '连接中断'
                : !board.api_runtime?.fresh
                  ? '状态未知 / 心跳过期'
                  : board.api_runtime?.ready
                    ? '调度服务已就绪'
                    : '服务未就绪'}
            </Badge>
          }
        />
        <p className="mt-2 text-sm text-zinc-700 dark:text-zinc-300">
          服务启动 {dateTime(board.api_runtime?.started_at)} · 心跳 {dateTime(board.api_runtime?.generated_at)} · 进程{' '}
          {board.api_runtime?.pid || '未知'}
        </p>
        <p className={`${meta} mt-1`}>
          API任务心跳超过30秒视为未知；Windows任务状态按发布器采集时间判断。服务就绪不代表行情与策略数据已验收。
        </p>
        {!board.api_runtime?.startups?.length && (
          <p className="mt-2 text-sm text-zinc-500 dark:text-zinc-400">尚未收到正式服务的启动记录。</p>
        )}
        {(board.api_runtime?.startups || []).filter((x) => x.status === 'failed').map((item) => (
          <p key={item.name} role="alert" className="mt-2 text-sm text-red-600 dark:text-red-400">
            {item.name}：{item.error}
          </p>
        ))}
        {!!board.api_runtime?.startups?.length && (
          <details className="mt-2">
            <summary className="cursor-pointer py-2 text-sm text-zinc-600 dark:text-zinc-400">
              查看全部 {board.api_runtime.startups.length} 项启动检查
            </summary>
            {board.api_runtime.startups.map((item) => (
              <div key={item.name} className="rounded-lg bg-zinc-500/5 px-3 py-2 dark:bg-white/5">
                <div className="flex flex-wrap items-center gap-3">
                  <strong className="text-sm text-zinc-950 dark:text-white">{item.name}</strong>
                  <Badge color={badgeColor[statusTone(board.api_runtime?.fresh ? item.status || 'unknown' : 'unknown')]}>
                    {statusLabel(board.api_runtime?.fresh ? item.status || 'unknown' : 'unknown')}
                  </Badge>
                  <span className={meta}>{dateTime(item.finished_at)}</span>
                </div>
              </div>
            ))}
          </details>
        )}
      </Card>

      {(board.operations_publisher?.publisher_status !== 'healthy' ||
        !board.operations_publisher?.notifications_enabled ||
        !board.operations_publisher?.notification_transport_ok) && (
        <div className="mb-4 rounded-lg border border-amber-500/25 bg-amber-50/70 px-4 py-3 text-sm text-amber-800 dark:border-amber-500/20 dark:bg-amber-500/5 dark:text-amber-300">
          <p className="font-semibold">独立数据告警尚未接管或心跳已过期</p>
          <p>此处记录异常不等于已发送通知；上线需同时部署通知发布器。原G3通知仅在自身运行窗口内兜底。</p>
        </div>
      )}
      <p className={`${meta} mb-4`}>
        通知通道：{statusLabel(board.operations_publisher?.notification_transport?.state || '')} · 最近SMTP接受{' '}
        {dateTime(board.operations_publisher?.notification_transport?.last_smtp_accepted_at)}
      </p>

      {board.host_inventory_status !== 'healthy' && (
        <div className="mb-4 rounded-lg border border-amber-500/25 bg-amber-50/70 px-4 py-3 text-sm text-amber-800 dark:border-amber-500/20 dark:bg-amber-500/5 dark:text-amber-300">
          <p className="font-semibold">Windows任务清单尚未发布或已过期</p>
          <p>未把缺少心跳解释为任务正常。请检查 Operations Publisher 的运行状态。</p>
        </div>
      )}

      <Card aria-label="每周数据审计" className="mb-4">
        <SectionTitle
          title="每周数据审计"
          actions={
            <Badge color={badgeColor[statusTone(board.weekly_audit?.status || 'unknown')]}>
              {statusLabel(board.weekly_audit?.status || 'unknown')}
            </Badge>
          }
        />
        <p className="mt-2 text-sm text-zinc-700 dark:text-zinc-300">
          最近审计 {dateTime(board.weekly_audit?.generated_at)} · 范围 {board.weekly_audit?.days ?? '未知'} 个交易日 ·
          异常组合 {board.weekly_audit?.problem_days ?? '未知'} 个
        </p>
        {(board.weekly_audit?.datasets || []).map((item) => (
          <p
            key={item.id}
            className={`mt-1 text-sm ${item.problem_days ? 'text-red-600 dark:text-red-400' : meta}`}
          >
            {item.label}：{item.checked_days} 日已核验，{item.problem_days} 日异常，缺键 {item.missing_keys}
          </p>
        ))}
        <p className={`${meta} mt-1`}>此任务只读检查交付覆盖：不下载行情、不写主表、不自动修复，也不创建数据库备份。</p>
      </Card>

      <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard label={metrics[0].label} value={metrics[0].value ?? '—'} />
        <StatCard label={metrics[1].label} value={metrics[1].value ?? '—'} tone="info" />
        <StatCard label={metrics[2].label} value={metrics[2].value ?? '—'} tone="warning" />
        <StatCard label={metrics[3].label} value={metrics[3].value ?? '—'} />
      </div>

      {!!board.historical_recovery?.total && (
        <Card aria-label="历史数据恢复进度" className="mb-4">
          <SectionTitle
            title="历史数据恢复"
            actions={
              <Badge color="amber">
                {board.historical_recovery.candidate_complete
                  ? '候选已校验，待上线验收'
                  : board.historical_recovery.pending?.some((x) => x.state === 'blocked')
                    ? '恢复已阻断'
                    : '候选构建记录'}
              </Badge>
            }
          />
          <p className="mt-2 text-sm text-zinc-700 dark:text-zinc-300">
            已校验 {board.historical_recovery.verified} / {board.historical_recovery.total} 个月份周期 ·{' '}
            {Number(board.historical_recovery.rows).toLocaleString()} 行
          </p>
          <div className="mt-3 h-4 w-full overflow-hidden rounded-full bg-zinc-200/60 dark:bg-white/10">
            <div
              className="h-full bg-amber-500 transition-all"
              style={{
                width: `${Math.floor(((board.historical_recovery.verified || 0) / board.historical_recovery.total!) * 100)}%`,
              }}
            />
          </div>
          <p className="mt-2 text-sm text-zinc-700 dark:text-zinc-300">
            数据截止 {board.historical_recovery.cutoff} · 不完整组合 {board.historical_recovery.incomplete_buckets} 个（未生成完整K线）
          </p>
          {(board.historical_recovery.pending || []).map((item) => (
            <p
              key={item.key}
              className={`mt-1 text-sm ${item.state === 'blocked' ? 'text-red-600 dark:text-red-400' : meta}`}
            >
              {recoveryPeriod(item.key)} · {recoveryStage(item.state)}
              {item.error && `：${item.error}`}
            </p>
          ))}
          <p className={`${meta} mt-1`}>
            最近记录 {dateTime(board.historical_recovery.updated_at)}
            {board.historical_recovery.record_stale && ' · 记录已过期，需核对执行进程'}。此处显示恢复检查点，不代表进程在线；候选构建不会自动替换正式表。
          </p>
        </Card>
      )}

      <Card aria-label="任务职责分组" className="mb-4">
        <SectionTitle
          title="按职责查看任务"
          actions={
            <div className="inline-flex overflow-hidden rounded-lg border border-zinc-950/10 dark:border-white/15">
              {([['core', '核心任务'], ['external', '外部联动'], ['retired', '已退役']] as [Scope, string][]).map(([value, text]) => (
                <button
                  key={value}
                  type="button"
                  className={`px-3 py-1.5 text-sm ${scope === value ? 'bg-zinc-950 text-white dark:bg-white dark:text-zinc-950' : 'text-zinc-600 dark:text-zinc-300'}`}
                  onClick={() => changeScope(value)}
                >
                  {text}
                </button>
              ))}
            </div>
          }
        />
        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {visibleGroups.map((item) => (
            <button
              key={item.id}
              type="button"
              aria-pressed={group === item.id}
              onClick={() => setGroup(item.id)}
              className={`rounded-xl border p-4 text-left transition-colors ${
                group === item.id
                  ? 'border-blue-500/50 bg-blue-500/10'
                  : 'border-zinc-950/10 bg-white hover:border-blue-500/30 dark:border-white/10 dark:bg-white/5'
              }`}
            >
              <strong className="text-sm text-zinc-950 dark:text-white">
                {item.label} <span className="font-normal">{item.count}项</span>
              </strong>
              <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">{item.purpose}</p>
              <small className={meta}>{item.attention ? `${item.attention}项需要核对` : '查看任务用途与执行详情'}</small>
            </button>
          ))}
        </div>
        <p className={`${meta} mt-3`}>按业务职责归类，保留真实子任务和执行窗口。已退役入口不计作当前生产任务。</p>
      </Card>

      <Card className="mb-4">
        <SectionTitle
          title={`${groupTitle} · ${groupRows.length}项`}
          actions={
            <>
              <div className="inline-flex overflow-hidden rounded-lg border border-zinc-950/10 dark:border-white/15">
                {([['all', '全部'], ['attention', '需处理'], ['running', '运行中'], ['disabled', '已停用']] as [Filter, string][]).map(
                  ([value, text]) => (
                    <button
                      key={value}
                      type="button"
                      className={`px-3 py-1.5 text-sm ${filter === value ? 'bg-zinc-950 text-white dark:bg-white dark:text-zinc-950' : 'text-zinc-600 dark:text-zinc-300'}`}
                      onClick={() => setFilter(value)}
                    >
                      {text}
                    </button>
                  )
                )}
              </div>
              <div className="w-60">
                <Input
                  aria-label="搜索任务或用途"
                  placeholder="搜索本组任务或用途"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
              </div>
            </>
          }
        />
        <div className="mt-3 overflow-x-auto">
          {filtered.length === 0 ? (
            <EmptyState text="尚无任务清单" />
          ) : (
            <table className="min-w-full border-separate border-spacing-0">
              <thead>
                <tr>
                  {['任务 / 作用', '执行器', '运行状态', '业务验收', '最近执行', '下次执行', '退出码'].map((h) => (
                    <th key={h} className={`${th} border-b border-zinc-950/10 dark:border-white/10`}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>{filtered.map(renderRow)}</tbody>
            </table>
          )}
        </div>
        <p className={`${meta} mt-3`}>
          宿主机清单：{dateTime(board.host_inventory_at)}。展开任务可查看失败原因、最近成功和耗时；退出码与业务结果分别展示。
        </p>
      </Card>

      <Card aria-label="数据源稳定性" className="mb-4">
        <SectionTitle title="数据源稳定性 · 最近24小时" />
        {!board.source_metrics?.sources?.length ? (
          <EmptyState text="尚无真实请求样本，成功率与延迟未知。" className="mt-2" />
        ) : (
          board.source_metrics.sources.map((source) => (
            <div key={source.source + source.operation} className="mt-3 rounded-lg bg-zinc-500/5 px-3 py-2 dark:bg-white/5">
              <strong className="text-sm text-zinc-950 dark:text-white">{source.source} · {source.operation}</strong>
              <p className="text-sm text-zinc-700 dark:text-zinc-300">
                请求 {source.requests} 次 · 失败 {source.failures} · 超时 {source.timeouts} · P95{' '}
                {source.p95_seconds?.toFixed(2)}秒
              </p>
              <p className="text-sm text-zinc-700 dark:text-zinc-300">
                本次返回 {source.received ?? '未知'} / {source.requested ?? '未知'} · 源时间 {dateTime(source.source_at)} ·
                落盘时间 {dateTime(source.persisted_at)}
              </p>
              <p className={meta}>
                最近采样 {dateTime(source.last_request_at)}；请求返回覆盖不等于有效证券完整交付，历史记录不代表当前在线。
              </p>
            </div>
          ))
        )}
      </Card>

      <Card aria-label="自动修复与复验" className="mb-4">
        <SectionTitle
          title="自动修复与复验"
          actions={
            <Badge color={repairStopped.length ? 'red' : 'amber'}>
              {repairStopped.length ? `${repairStopped.length}项已停止自动修复` : statusLabel(board.operations_publisher?.repair?.status || '')}
            </Badge>
          }
        />
        <p className={`${meta} mt-2`}>
          状态采集 {dateTime(board.operations_publisher?.generated_at)} ·{' '}
          {board.operations_publisher?.publisher_status === 'healthy' ? '本次已发布状态' : '快照已过期，当前状态未知'}。任务完成后仍须独立数据复验；预算耗尽或连续无进展时停止补数并进入告警窗口。
        </p>
        {(board.operations_publisher?.repair?.held || []).map((item) => (
          <div key={item.key} className="mt-3 rounded-lg bg-zinc-500/5 px-3 py-2 dark:bg-white/5">
            <strong className="break-all text-sm text-zinc-950 dark:text-white">{item.key}</strong>
            <p className={`text-sm ${isRepairStopped(item.status) ? 'text-red-600 dark:text-red-400' : meta}`}>
              {statusLabel(item.status || '')}
            </p>
          </div>
        ))}
        {!!board.operations_publisher?.repair?.verified?.length && (
          <p className="mt-2 text-sm text-zinc-700 dark:text-zinc-300">
            本轮复验通过：{board.operations_publisher.repair.verified.join('、')}
          </p>
        )}
      </Card>

      <Card>
        <SectionTitle
          title="异常与恢复记录"
          actions={
            <p className="text-sm font-normal text-zinc-500 dark:text-zinc-400">
              异常先进入恢复窗口，超时后合并提醒；SMTP接受不代表收件人已收到或阅读。
            </p>
          }
        />
        {events.length === 0 ? (
          <EmptyState text="尚无已记录事件；这不代表所有数据已通过验收" />
        ) : (
          <>
            <p className={`${meta} mt-2`}>已加载最近 {events.length} 条记录，每页10条</p>
            {eventsOnPage.map((event) => (
              <div key={event.key} className="mt-3 rounded-lg bg-zinc-500/5 px-3 py-2 dark:bg-white/5">
                <div className="flex flex-wrap items-center gap-2">
                  <strong className="text-sm text-zinc-950 dark:text-white">
                    {event.detail?.message || event.detail?.reason || event.key}
                  </strong>
                  <Badge color={badgeColor[statusTone(event.status || '')]}>{statusLabel(event.status || '')}</Badge>
                  <Badge color="zinc">{statusLabel(event.notification || '')}</Badge>
                </div>
                <p className={meta}>
                  首次发现 {dateTime(event.first_seen)} · 恢复窗口 {dateTime(event.deadline)} · 发送尝试 {event.attempts} ·
                  恢复通知 {statusLabel(event.recovery_notification || '')}
                </p>
                {event.error && <p className="text-sm text-red-600 dark:text-red-400">{event.error}</p>}
              </div>
            ))}
            {events.length > 10 && (
              <div className="mt-4 flex items-center justify-center gap-3 text-sm text-zinc-600 dark:text-zinc-400">
                <Button outline disabled={eventPage === 1} onClick={() => setEventPage((p) => p - 1)}>上一页</Button>
                <span>
                  第 {eventPage} 页 / 共 {totalEventPages} 页
                </span>
                <Button outline disabled={eventPage >= totalEventPages} onClick={() => setEventPage((p) => p + 1)}>下一页</Button>
              </div>
            )}
          </>
        )}
      </Card>
    </div>
  )
}
