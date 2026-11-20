import { useCallback, useEffect, useState } from 'react'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Dialog, DialogBody, DialogTitle } from '@/components/catalyst/dialog'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/catalyst/table'
import { Card, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import {
  getDataOverview,
  getHealthMatrix,
  type DataOverviewResponse,
  type HealthMatrixCell,
  type HealthMatrixDataset,
  type HealthMatrixResponse,
} from '@/api/dataCenter'

const cellStyle = (status: HealthMatrixCell['status']): string => {
  if (status === 'ok') return 'border-emerald-500/40 bg-emerald-500/15 text-emerald-200 hover:bg-emerald-500/25'
  if (status === 'warn') return 'border-amber-500/40 bg-amber-500/15 text-amber-200 hover:bg-amber-500/25'
  if (status === 'missing') return 'border-red-500/50 bg-red-500/15 text-red-200 hover:bg-red-500/25'
  if (status === 'optional') return 'border-zinc-700/60 bg-zinc-800/40 text-zinc-400 hover:bg-zinc-800/70'
  return 'border-zinc-700/60 bg-zinc-900/60 text-zinc-400'
}

const cellLabel = (c: HealthMatrixCell): string => {
  if (c.status === 'pending') return '尚未到期'
  if (c.status === 'optional') return c.actual > 0 ? `${c.coverage}%` : '未启用'
  if (c.status === 'ok') return '完整'
  if (c.status === 'warn') return '轻微缺口'
  return '异常缺失'
}

export default function DataCenter() {
  const [days, setDays] = useState(10)
  const [overview, setOverview] = useState<DataOverviewResponse | null>(null)
  const [matrix, setMatrix] = useState<HealthMatrixResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<{ dataset: HealthMatrixDataset; cell: HealthMatrixCell } | null>(null)

  const load = useCallback(async (forceRefresh = false) => {
    setLoading(true)
    setError('')
    try {
      const [ov, mx] = await Promise.all([
        getDataOverview(forceRefresh),
        getHealthMatrix(days, forceRefresh),
      ])
      setOverview(ov)
      setMatrix(mx)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }, [days])

  useEffect(() => { void load(false) }, [load])

  const s = overview?.summary

  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 p-4 sm:p-6">
      <PageHeader
        title="数据健康与监控"
        subtitle="直连 ClickHouse 物理表实时验收 · 科学分母（分钟线对标当日有成交日线、指数对标 11 大核心指数）"
        actions={
          <div className="flex items-center gap-2">
            <select
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
              className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-100 focus:border-blue-500 focus:outline-none"
            >
              <option value={10}>近 10 个交易日</option>
              <option value={20}>近 20 个交易日</option>
              <option value={30}>近 30 个交易日</option>
            </select>
            <Button onClick={() => void load(true)} disabled={loading}>
              {loading ? '验收中…' : '刷新验收'}
            </Button>
          </div>
        }
      />

      {error && (
        <div className="rounded-lg border border-red-500/40 bg-red-500/10 px-4 py-2.5 text-sm text-red-200">
          {error}
        </div>
      )}

      {/* 顶部核心数据资产大卡 */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard
          label="最新交付交易日"
          value={overview?.latest_daily_date || '—'}
          hint={`验收时间 ${overview?.generated_at?.slice(11, 19) || '—'}`}
        />
        <StatCard
          label="ClickHouse 总行数"
          value={s ? `${(s.total_rows / 1e8).toFixed(2)} 亿` : '—'}
          hint={`物理占用 ${s?.total_size_gb ?? 0} GB`}
        />
        <StatCard
          label="在市股票池"
          value={s ? s.active_stocks.toLocaleString() : '—'}
          hint={`核心维护指数 ${s?.core_indices ?? 11} 只`}
        />
        <StatCard
          label="通达信标准行业"
          value={s ? `${s.industry_sectors} 个` : '—'}
          hint="通达信板块统一体系"
        />
      </div>

      {/* 交易日交付矩阵 */}
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <SectionTitle title="交易日交付矩阵" />
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <Badge color="emerald">完整</Badge>
            <Badge color="amber">轻微缺口 (95%~99%)</Badge>
            <Badge color="red">异常缺失 (&lt;95%)</Badge>
            <Badge color="zinc">尚未到期 / 未启用</Badge>
          </div>
        </div>

        <div className="mt-3 overflow-x-auto">
          <table className="w-full border-separate border-spacing-1.5 text-xs">
            <thead>
              <tr>
                <th className="min-w-[140px] px-2 py-1.5 text-left font-medium text-zinc-400">数据集</th>
                {(matrix?.dates || []).map((d) => (
                  <th key={d} className="min-w-[68px] px-1 py-1.5 text-center font-mono font-medium text-zinc-400">
                    {d.slice(5)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {(matrix?.datasets || []).map((ds) => (
                <tr key={ds.id}>
                  <td className="px-2 py-1 font-medium text-zinc-200" title={ds.note}>
                    {ds.label}
                  </td>
                  {ds.cells.map((c) => (
                    <td key={c.date} className="p-0">
                      <button
                        type="button"
                        onClick={() => setSelected({ dataset: ds, cell: c })}
                        className={`flex w-full flex-col items-center justify-center rounded-md border px-1.5 py-1.5 transition ${cellStyle(c.status)}`}
                      >
                        <span className="font-medium leading-tight">{cellLabel(c)}</span>
                        <span className="mt-0.5 font-mono text-[11px] opacity-85">
                          {c.coverage != null ? `${c.coverage.toFixed(1)}%` : '—'}
                        </span>
                      </button>
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {/* ClickHouse 存储与时间范围明细 */}
      <Card>
        <SectionTitle title="ClickHouse 核心数据表存储监控" />
        <Table dense>
          <TableHead>
            <TableRow>
              <TableHeader>数据表</TableHeader>
              <TableHeader>用途</TableHeader>
              <TableHeader className="text-right">总行数</TableHeader>
              <TableHeader className="text-right">磁盘占用</TableHeader>
              <TableHeader>最早时间</TableHeader>
              <TableHeader>最新时间</TableHeader>
            </TableRow>
          </TableHead>
          <TableBody>
            {(overview?.tables || []).map((t) => (
              <TableRow key={t.table}>
                <TableCell className="font-mono text-xs text-zinc-100">{t.table}</TableCell>
                <TableCell className="text-zinc-300">{t.label}</TableCell>
                <TableCell className="text-right font-mono text-zinc-200">{t.rows.toLocaleString()}</TableCell>
                <TableCell className="text-right font-mono text-zinc-300">
                  {t.size_mb >= 1024 ? `${(t.size_mb / 1024).toFixed(2)} GB` : `${t.size_mb.toFixed(2)} MB`}
                </TableCell>
                <TableCell className="font-mono text-xs text-zinc-400">{t.min_time || '—'}</TableCell>
                <TableCell className="font-mono text-xs text-emerald-300">{t.max_time || '—'}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Card>

      {/* 格子详情弹窗 */}
      <Dialog open={selected != null} onClose={() => setSelected(null)}>
        <DialogTitle>
          {selected?.dataset.label} · {selected?.cell.date}
        </DialogTitle>
        <DialogBody>
          {selected && (
            <div className="space-y-3 text-sm text-zinc-300">
              <div className="text-xs text-zinc-400">{selected.dataset.note}</div>
              <div className="grid grid-cols-3 gap-2 text-center">
                <div className="rounded-lg bg-zinc-900 p-3">
                  <div className="text-xs text-zinc-400">实到标的</div>
                  <div className="mt-1 font-mono text-lg font-bold text-white">{selected.cell.actual}</div>
                </div>
                <div className="rounded-lg bg-zinc-900 p-3">
                  <div className="text-xs text-zinc-400">应到基准</div>
                  <div className="mt-1 font-mono text-lg font-bold text-zinc-200">{selected.cell.expected}</div>
                </div>
                <div className="rounded-lg bg-zinc-900 p-3">
                  <div className="text-xs text-zinc-400">覆盖率</div>
                  <div className="mt-1 font-mono text-lg font-bold text-emerald-400">
                    {selected.cell.coverage != null ? `${selected.cell.coverage.toFixed(1)}%` : '—'}
                  </div>
                </div>
              </div>
              <div className="flex justify-end pt-2">
                <Button onClick={() => setSelected(null)}>关闭</Button>
              </div>
            </div>
          )}
        </DialogBody>
      </Dialog>
    </div>
  )
}
