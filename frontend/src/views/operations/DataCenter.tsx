import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Dialog, DialogBody, DialogTitle } from '@/components/catalyst/dialog'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/catalyst/table'
import { Card, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import KlineCheck from '@/views/operations/KlineCheck'
import {
  getDataOverview,
  getHealthMatrix,
  type DataOverviewResponse,
  type HealthMatrixCell,
  type HealthMatrixDataset,
  type HealthMatrixResponse,
} from '@/api/dataCenter'

type DataTab = 'overview' | 'inspect'

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

const tableGroupBadge = (group?: string) => {
  if (group === 'g3_strategy') return <Badge color="blue">G3 实战策略</Badge>
  if (group === 'reference') return <Badge color="zinc">基础主数据</Badge>
  return <Badge color="emerald">A股行情</Badge>
}

interface DataCenterProps {
  defaultTab?: DataTab
}

export default function DataCenter({ defaultTab = 'overview' }: DataCenterProps) {
  const [searchParams, setSearchParams] = useSearchParams()
  const tabParam = searchParams.get('tab')
  const activeTab: DataTab =
    tabParam === 'inspect' ? 'inspect' : tabParam === 'overview' ? 'overview' : defaultTab
  const inspectCodeParam = searchParams.get('code') || undefined

  const [days, setDays] = useState(10)
  const [overview, setOverview] = useState<DataOverviewResponse | null>(null)
  const [matrix, setMatrix] = useState<HealthMatrixResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<{ dataset: HealthMatrixDataset; cell: HealthMatrixCell } | null>(null)

  const switchTab = (next: DataTab, code?: string) => {
    const params = new URLSearchParams(searchParams)
    if (next === 'overview') {
      params.delete('tab')
      params.delete('code')
    } else {
      params.set('tab', 'inspect')
      if (code) params.set('code', code)
    }
    setSearchParams(params, { replace: true })
  }

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

  useEffect(() => {
    if (activeTab === 'overview') {
      void load(false)
    }
  }, [activeTab, load])

  // 按业务域分组渲染交付矩阵（G3 实战策略数据置顶，A 股行情紧随其后）
  const groupedDatasets = useMemo(() => {
    const list = matrix?.datasets || []
    const groups: { key: string; label: string; items: HealthMatrixDataset[] }[] = []
    for (const ds of list) {
      const key = ds.group || 'market'
      const label =
        ds.group_label ||
        (key === 'g3_strategy' ? 'G3 实战策略数据（宽基 / 国债 / 宏观 / 南向）' : 'A 股行情、核心指数与行业板块')
      let found = groups.find((g) => g.key === key)
      if (!found) {
        found = { key, label, items: [] }
        groups.push(found)
      }
      found.items.push(ds)
    }
    return groups
  }, [matrix])

  const s = overview?.summary

  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 p-4 sm:p-6">
      <PageHeader
        title="数据健康与监控"
        subtitle="全量覆盘 AiStock 交易系统 ClickHouse 物理表（股票/指数/板块 + 实战宽基/红利/国债/黄金/跨境 ETF + 国家队份额/宏观 ERP/南向/财报）"
        actions={
          activeTab === 'overview' ? (
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
          ) : undefined
        }
      />

      {/* 双 Tab 切换条（合并原「K线完整性」入口） */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-zinc-800 pb-3">
        <div className="inline-flex rounded-xl border border-zinc-800 bg-zinc-900/80 p-1">
          <button
            type="button"
            onClick={() => switchTab('overview')}
            className={`rounded-lg px-4 py-1.5 text-xs font-semibold transition ${
              activeTab === 'overview'
                ? 'bg-blue-600 text-white shadow-sm'
                : 'text-zinc-400 hover:text-zinc-100'
            }`}
          >
            📊 全局数据交付与存储
          </button>
          <button
            type="button"
            onClick={() => switchTab('inspect')}
            className={`rounded-lg px-4 py-1.5 text-xs font-semibold transition ${
              activeTab === 'inspect'
                ? 'bg-blue-600 text-white shadow-sm'
                : 'text-zinc-400 hover:text-zinc-100'
            }`}
          >
            🔍 单标的 K 线体检（股票 / 指数 / ETF）
          </button>
        </div>

        <div className="text-xs text-zinc-400">
          {activeTab === 'overview'
            ? '点击矩阵中任意缺口格子可查看具体缺失标的并一键下钻体检'
            : '支持股票/指数全周期槽位校验及宽基/红利/国债/黄金 ETF 日线与份额校验'}
        </div>
      </div>

      {activeTab === 'inspect' ? (
        <KlineCheck embedded initialCode={inspectCodeParam} />
      ) : (
        <>
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
              hint={`ETF ${s?.latest_etf_date || '—'} · 宏观 ${s?.latest_macro_date || '—'}`}
            />
            <StatCard
              label="ClickHouse 总行数"
              value={s ? `${(s.total_rows / 1e8).toFixed(2)} 亿` : '—'}
              hint={`物理占用 ${s?.total_size_gb ?? 0} GB · ${overview?.tables?.length ?? 0} 张核心表`}
            />
            <StatCard
              label="A 股与核心指数池"
              value={s ? s.active_stocks.toLocaleString() : '—'}
              hint={`核心指数 ${s?.core_indices ?? 11} 只 · 行业 ${s?.industry_sectors ?? 0} 个`}
            />
            <StatCard
              label="G3 实战 ETF 与宏观池"
              value={s ? `${s.core_etfs ?? 14} 核心 / ${s.total_etfs ?? 54} 全量` : '—'}
              hint={`国家队份额 ${s?.national_team_etfs ?? 15} 只 · 份额日 ${s?.latest_etf_share_date || '—'}`}
            />
          </div>

          {/* 交易日交付矩阵（按业务域分组） */}
          <Card>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <SectionTitle title="交易日交付矩阵" />
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <Badge color="emerald">完整</Badge>
                <Badge color="amber">轻微缺口 (95%~99%)</Badge>
                <Badge color="red">异常缺失 (&lt;95%)</Badge>
                <Badge color="zinc">尚未到期 / T+1待公布</Badge>
              </div>
            </div>

            <div className="mt-4 space-y-5">
              {groupedDatasets.map((grp) => (
                <div key={grp.key} className="space-y-2">
                  <div className="flex items-center gap-2 text-xs font-semibold tracking-wide text-zinc-300">
                    <span
                      className={`h-2 w-2 rounded-full ${
                        grp.key === 'g3_strategy' ? 'bg-blue-400' : 'bg-emerald-400'
                      }`}
                    />
                    {grp.label}
                  </div>
                  <div className="overflow-x-auto">
                    <table className="w-full border-separate border-spacing-1.5 text-xs">
                      <thead>
                        <tr>
                          <th className="min-w-[170px] px-2 py-1.5 text-left font-medium text-zinc-400">数据集</th>
                          {(matrix?.dates || []).map((d) => (
                            <th key={d} className="min-w-[68px] px-1 py-1.5 text-center font-mono font-medium text-zinc-400">
                              {d.slice(5)}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {grp.items.map((ds) => (
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
                </div>
              ))}
            </div>
          </Card>

          {/* ClickHouse 存储与时间范围明细 */}
          <Card>
            <SectionTitle title="ClickHouse 现役交易系统核心数据表监控" />
            <Table dense>
              <TableHead>
                <TableRow>
                  <TableHeader>所属域</TableHeader>
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
                    <TableCell>{tableGroupBadge(t.group)}</TableCell>
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

          {/* 格子详情弹窗（支持查看具体缺失标的并一键跳转单标的体检） */}
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
                      <div className="text-xs text-zinc-400">实到数量</div>
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

                  {selected.cell.missing_items && selected.cell.missing_items.length > 0 && (
                    <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3">
                      <div className="mb-2 text-xs font-semibold text-amber-300">
                        缺失标的明细（点击可直接跳转单标的体检）：
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {selected.cell.missing_items.map((item) => {
                          const code = item.split(' ')[0]
                          return (
                            <button
                              key={item}
                              type="button"
                              onClick={() => {
                                setSelected(null)
                                switchTab('inspect', code)
                              }}
                              className="rounded border border-amber-400/40 bg-zinc-900/80 px-2 py-1 font-mono text-xs text-amber-100 hover:border-blue-400 hover:text-white"
                            >
                              {item}
                            </button>
                          )
                        })}
                      </div>
                    </div>
                  )}

                  <div className="flex justify-end pt-2">
                    <Button onClick={() => setSelected(null)}>关闭</Button>
                  </div>
                </div>
              )}
            </DialogBody>
          </Dialog>
        </>
      )}
    </div>
  )
}
