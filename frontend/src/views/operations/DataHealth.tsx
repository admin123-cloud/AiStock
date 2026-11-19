import { useCallback, useEffect, useState } from 'react'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Select } from '@/components/catalyst/select'
import { Alert } from '@/components/catalyst/alert'
import { Card, EmptyState, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import {
  getDataCalendar,
  statusLabel,
  statusTone,
  dateTime,
  type DataCalendar,
  type DataCalendarCell,
} from '@/api/operations'

type SelectedCell = DataCalendarCell & { dataset: string; error?: string }

const badgeColor = { danger: 'red', success: 'emerald', warning: 'amber' } as const

function cellClasses(cell: DataCalendarCell): string {
  const severe = cell.coverage != null && cell.coverage < 0.9
  const tone = statusTone(cell.status)
  if (tone === 'success')
    return 'bg-emerald-500/15 text-emerald-800 hover:bg-emerald-500/25 dark:text-emerald-300'
  if (tone === 'danger')
    return 'bg-red-500/15 text-red-800 hover:bg-red-500/25 dark:text-red-300'
  if (cell.status === 'not_due' || cell.status === 'unknown')
    return 'bg-zinc-500/10 text-zinc-600 hover:bg-zinc-500/20 dark:bg-white/5 dark:text-zinc-400'
  return 'bg-amber-400/20 text-amber-800 hover:bg-amber-400/30 dark:text-amber-300'
}

export default function DataHealth() {
  const [calendar, setCalendar] = useState<DataCalendar | null>(null)
  const [selected, setSelected] = useState<SelectedCell | null>(null)
  const [days, setDays] = useState(10)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(
    async (range: number) => {
      setLoading(true)
      setError('')
      try {
        const data = await getDataCalendar(range)
        setCalendar(data)
        setSelected(null)
      } catch (e: unknown) {
        const err = e as { response?: { data?: { detail?: string } }; message?: string }
        setError(err.response?.data?.detail || err.message || '请求失败')
      } finally {
        setLoading(false)
      }
    },
    []
  )

  useEffect(() => {
    void load(days)
  }, [days, load])

  const dates = calendar?.dates ?? []
  const datasets = calendar?.datasets ?? []

  return (
    <div className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
      <PageHeader
        title="数据健康"
        subtitle="按交易日检查应有数据，区分尚未到期、覆盖缺失与来源冲突。"
        actions={
          <>
            <div className="w-36">
              <Select
                aria-label="交易日范围"
                value={days}
                onChange={(e) => setDays(Number(e.target.value))}
              >
                <option value={10}>近10个交易日</option>
                <option value={30}>近30个交易日</option>
                <option value={60}>近60个交易日</option>
              </Select>
            </div>
            <Button onClick={() => void load(days)} disabled={loading}>
              {loading ? '核对中…' : '刷新验收'}
            </Button>
          </>
        }
      />

      {error && (
        <div
          role="alert"
          className="mb-4 rounded-lg border border-red-500/20 bg-red-50/70 px-4 py-3 text-sm text-red-700 dark:border-red-500/20 dark:bg-red-500/5 dark:text-red-400"
        >
          {error}；没有将查询错误显示成零缺口。
        </div>
      )}

      {loading && (
        <Alert color="amber" className="mb-4">
          <p className="font-semibold text-zinc-950 dark:text-white">正在按交易日核对唯一时间键</p>
          <p className="text-zinc-700 dark:text-zinc-300">
            首次读取可能需要等待。保留上次结果及其验收时间。
          </p>
        </Alert>
      )}

      <Card className="mb-6">
        <SectionTitle title="交易日交付矩阵" />
        <p className="text-sm text-zinc-500 dark:text-zinc-400">
          {calendar?.scope || '等待独立覆盖验收，未用实际记录数充当应有数量。'}
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          <Badge color="emerald">覆盖齐全</Badge>
          <Badge color="amber">部分缺失 / 待验收</Badge>
          <Badge color="red">缺失 / 覆盖不足90%</Badge>
          <Badge color="zinc">未知 / 未到期</Badge>
        </div>
        <div className="mt-4 overflow-x-auto">
          <table className="min-w-full border-separate border-spacing-0 text-sm">
            <thead>
              <tr>
                <th className="sticky left-0 z-10 bg-white px-3 py-2 text-left text-xs font-semibold text-zinc-500 dark:bg-zinc-900 dark:text-zinc-400">
                  数据集
                </th>
                {dates.map((day) => (
                  <th
                    key={day}
                    className="whitespace-nowrap px-2 py-2 text-center text-xs font-semibold text-zinc-500 dark:text-zinc-400"
                  >
                    {day.slice(5)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {datasets.map((dataset) => (
                <tr key={dataset.id}>
                  <th className="sticky left-0 z-10 whitespace-nowrap bg-white px-3 py-1.5 text-left text-sm font-medium text-zinc-950 dark:bg-zinc-900 dark:text-white">
                    {dataset.label}
                  </th>
                  {dataset.cells.map((cell) => {
                    const severe = cell.coverage != null && cell.coverage < 0.9
                    return (
                      <td key={cell.date} className="px-1 py-1 text-center">
                        <button
                          type="button"
                          className={`inline-flex min-w-16 flex-col items-center rounded-md px-2 py-1 text-xs font-medium transition-colors ${cellClasses(cell)} ${
                            severe ? 'ring-2 ring-red-500/60' : ''
                          }`}
                          aria-label={`${dataset.label} ${cell.date} ${statusLabel(cell.status)}`}
                          onClick={() =>
                            setSelected({ ...cell, dataset: dataset.label, error: dataset.error })
                          }
                        >
                          {statusLabel(cell.status)}
                          {cell.coverage != null && (
                            <small className="tabular-nums opacity-80">
                              {(cell.coverage * 100).toFixed(1)}%
                            </small>
                          )}
                        </button>
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!loading && datasets.length === 0 && <EmptyState text="暂无验收数据" className="mt-4" />}
        <p className="mt-3 text-xs text-zinc-500 dark:text-zinc-400">
          验收时间 {dateTime(calendar?.generated_at)} · 发布快照有效期15分钟，即时查询缓存5分钟。
          点击格子查看缺口与样例。
        </p>
      </Card>

      {selected && (
        <Card aria-live="polite">
          <SectionTitle
            title={`${selected.dataset} · ${selected.date}`}
            actions={<Badge color={badgeColor[statusTone(selected.status)]}>{statusLabel(selected.status)}</Badge>}
          />
          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <StatCard label="应有唯一键" value={selected.expected ?? '待定义'} />
            <StatCard label="实际覆盖" value={selected.actual ?? '未知'} />
            <StatCard label="缺失键" value={selected.missing ?? '未验收'} tone="warning" />
            <StatCard label="有依据的业务豁免标的" value={selected.business_exceptions ?? '—'} />
          </div>
          {selected.missing_codes_sample?.length ? (
            <p className="mt-3 text-sm text-zinc-700 dark:text-zinc-300">
              缺口标的（最多30只）：{selected.missing_codes_sample.join('、')}
            </p>
          ) : null}
          {selected.error ? (
            <p className="mt-2 text-sm text-red-600 dark:text-red-400">{selected.error}</p>
          ) : null}
        </Card>
      )}
    </div>
  )
}
