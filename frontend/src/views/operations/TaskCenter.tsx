import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/catalyst/table'
import { Card, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import {
  getTaskCenterBoard,
  type TaskCenterBoard,
  type TaskCenterDeliveryItem,
  type TaskCenterServiceItem,
  type TaskCenterTaskItem,
} from '@/api/operations'

const statusBadgeColor = (status: string): 'emerald' | 'amber' | 'red' | 'zinc' => {
  if (status === 'ok') return 'emerald'
  if (status === 'warn') return 'amber'
  if (status === 'error') return 'red'
  return 'zinc'
}

const statusText = (status: string): string => {
  if (status === 'ok') return '正常'
  if (status === 'warn') return '关注'
  if (status === 'error') return '异常'
  return '待命'
}

const categoryBadgeColor = (category: string): 'blue' | 'purple' | 'zinc' => {
  if (category === '盘中实时') return 'blue'
  if (category === '盘后数据') return 'purple'
  return 'zinc'
}

export default function TaskCenter() {
  const navigate = useNavigate()
  const [board, setBoard] = useState<TaskCenterBoard | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [categoryFilter, setCategoryFilter] = useState<string>('all')

  const load = useCallback(async (forceRefresh = false) => {
    setLoading(true)
    setError('')
    try {
      const data = await getTaskCenterBoard(forceRefresh)
      setBoard(data)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '加载任务中心失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load(false)
    const timer = window.setInterval(() => void load(false), 30_000)
    return () => window.clearInterval(timer)
  }, [load])

  const s = board?.summary
  const tasks: TaskCenterTaskItem[] = (board?.tasks || []).filter(
    (t) => categoryFilter === 'all' || t.category === categoryFilter
  )
  const services: TaskCenterServiceItem[] = board?.services || []
  const deliveries: TaskCenterDeliveryItem[] = board?.delivery_summary || []

  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 p-4 sm:p-6">
      <PageHeader
        title="任务中心"
        subtitle={
          board
            ? `直连 ClickHouse 与容器运行态实时监测 · 更新于 ${board.generated_at.slice(11, 19)}（每 30 秒自动刷新）`
            : '直连 ClickHouse 与容器运行态实时监测'
        }
        actions={
          <div className="flex items-center gap-2">
            {board && (
              <Badge color={board.trading_now ? 'blue' : 'zinc'}>
                {board.trading_now ? '盘中交易时段' : '非交易 / 盘后时段'}
              </Badge>
            )}
            <Button onClick={() => void load(true)} disabled={loading}>
              {loading ? '刷新中…' : '立即刷新'}
            </Button>
          </div>
        }
      />

      {error && (
        <div className="rounded-lg border border-red-500/40 bg-red-500/10 px-4 py-2.5 text-sm text-red-200">
          {error}
        </div>
      )}

      {/* 顶部 4 项核心指标 */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard
          label="核心服务健康"
          value={s ? `${s.services_ok} / ${s.services_total}` : '—'}
          hint={s && s.services_ok === s.services_total ? '全部核心服务在线' : '存在需关注服务'}
        />
        <StatCard
          label="在运调度任务"
          value={s ? `${s.tasks_ok} / ${s.tasks_total}` : '—'}
          hint={
            s
              ? s.tasks_error > 0
                ? `异常 ${s.tasks_error} · 关注 ${s.tasks_warn}`
                : s.tasks_warn > 0
                  ? `0 异常 · 关注 ${s.tasks_warn}`
                  : '全部调度任务运行正常'
              : '—'
          }
        />
        <StatCard
          label="今日盘中快照"
          value={s ? `${s.intraday_today_rounds} 轮` : '—'}
          hint={s?.latest_intraday_ts ? `最新 ${s.latest_intraday_ts.slice(5, 16)}` : '尚未采集'}
        />
        <StatCard
          label="最新认证数据日"
          value={s?.latest_certified_date || s?.latest_daily_date || '—'}
          hint={`ClickHouse 日线 ${s?.latest_daily_date || '—'}`}
        />
      </div>

      {/* 核心服务运行态 */}
      <Card>
        <SectionTitle title="核心服务与守护进程" />
        <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {services.map((svc) => (
            <div
              key={svc.id}
              className="flex flex-col justify-between rounded-lg border border-zinc-800 bg-zinc-900/50 p-3.5"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-medium text-zinc-100">{svc.name}</span>
                <Badge color={statusBadgeColor(svc.status)}>{statusText(svc.status)}</Badge>
              </div>
              <div className="mt-2 text-xs text-zinc-400">{svc.detail}</div>
              <div className="mt-2 font-mono text-[11px] text-zinc-500">心跳: {svc.heartbeat}</div>
            </div>
          ))}
        </div>
      </Card>

      {/* 在运调度任务清单 */}
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <SectionTitle title="在运定时调度任务" />
          <div className="flex items-center gap-1.5">
            {['all', '盘中实时', '盘后数据', '策略监控'].map((cat) => (
              <Button
                key={cat}
                plain={categoryFilter !== cat}
                outline={categoryFilter === cat}
                onClick={() => setCategoryFilter(cat)}
                className="px-2.5 py-1 text-xs"
              >
                {cat === 'all' ? '全部' : cat}
              </Button>
            ))}
          </div>
        </div>
        <div className="mt-3 overflow-x-auto">
          <Table dense>
            <TableHead>
              <TableRow>
                <TableHeader>任务名称</TableHeader>
                <TableHeader>类别</TableHeader>
                <TableHeader>调度计划</TableHeader>
                <TableHeader>状态</TableHeader>
                <TableHeader>最近运行</TableHeader>
                <TableHeader>实况摘要</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {tasks.map((task) => (
                <TableRow key={task.id}>
                  <TableCell>
                    <div className="font-medium text-zinc-100">{task.name}</div>
                    <div className="font-mono text-[11px] text-zinc-500">{task.target}</div>
                  </TableCell>
                  <TableCell>
                    <Badge color={categoryBadgeColor(task.category)}>{task.category}</Badge>
                  </TableCell>
                  <TableCell className="text-xs text-zinc-300">{task.schedule}</TableCell>
                  <TableCell>
                    <Badge color={statusBadgeColor(task.status)}>{statusText(task.status)}</Badge>
                  </TableCell>
                  <TableCell className="font-mono text-xs text-zinc-300">
                    {task.last_run}
                    {task.duration_sec !== null && (
                      <span className="ml-1.5 text-zinc-500">({task.duration_sec}s)</span>
                    )}
                  </TableCell>
                  <TableCell className="text-xs text-zinc-300">{task.summary}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </Card>

      {/* 近 10 日核心数据交付摘要 */}
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <SectionTitle title="近 10 个交易日核心数据交付摘要" />
          <Button outline onClick={() => navigate('/data/health')} className="text-xs">
            查看完整交付矩阵
          </Button>
        </div>
        <div className="mt-3 grid grid-cols-1 gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
          {deliveries.map((d) => (
            <div
              key={d.id}
              onClick={() => navigate('/data/health')}
              className="flex cursor-pointer items-center justify-between rounded-lg border border-zinc-800 bg-zinc-900/40 px-3.5 py-2.5 transition-colors hover:border-zinc-700 hover:bg-zinc-900"
            >
              <div>
                <div className="text-sm font-medium text-zinc-200">{d.label}</div>
                <div className="mt-0.5 text-xs text-zinc-500">
                  合格 {d.ok_days}/{d.checked_days} 日 · 最新 {d.latest_date || '—'}
                </div>
              </div>
              <div className="text-right">
                <Badge color={statusBadgeColor(d.status)}>
                  {d.latest_coverage !== null ? `${d.latest_coverage}%` : statusText(d.status)}
                </Badge>
              </div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  )
}
