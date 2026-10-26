import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Card, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import {
  getKlineCheckHistory,
  getKlineCheckHistoryDetail,
  getKlineCheckProgress,
  getKlineCheckResults,
  getKlineRepairProgress,
  getKlineRepairResults,
  repairKlineBatch,
  repairKlineData,
  startKlineCheckAll,
  type KlineCheckRow,
  type KlineCheckSummary,
  type KlineHistoryRecord,
  type KlineProgress,
  type KlineRepairResult,
} from '@/api/klineCheck'

const typeLabels: Record<string, string> = { stock: '股票', index: '指数', sector: '板块', all: '巡检汇总', repair: '修复记录' }

function label(value?: string) {
  return typeLabels[value || ''] || value || '—'
}

function rowKey(row: KlineCheckRow) {
  return `${row.data_type || ''}::${row.code || ''}`
}

function sleep(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

function downloadCsv(filename: string, headers: string[], rows: unknown[][]) {
  const csv = [headers, ...rows].map((row) => row.map((value) => `"${String(value ?? '').replaceAll('"', '""')}"`).join(',')).join('\n')
  const link = document.createElement('a')
  link.href = URL.createObjectURL(new Blob([`${csv}\n`], { type: 'text/csv;charset=utf-8;' }))
  link.download = filename
  link.click()
  URL.revokeObjectURL(link.href)
}

function formatDateTime(value: unknown) {
  if (!value) return '—'
  const date = new Date(String(value))
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN')
}

export default function KlineCheck() {
  const [limitCodes, setLimitCodes] = useState(1000)
  const [startDate, setStartDate] = useState('2020-01-01')
  const [batchSize, setBatchSize] = useState(100)
  const [checking, setChecking] = useState(false)
  const [repairing, setRepairing] = useState(false)
  const [autoRunning, setAutoRunning] = useState(false)
  const [progress, setProgress] = useState<KlineProgress>({})
  const [repairProgress, setRepairProgress] = useState<KlineProgress & { success?: number; failed?: number }>({})
  const [rows, setRows] = useState<KlineCheckRow[]>([])
  const [summary, setSummary] = useState<KlineCheckSummary | null>(null)
  const [selected, setSelected] = useState<string[]>([])
  const [repairResult, setRepairResult] = useState<KlineRepairResult | null>(null)
  const [history, setHistory] = useState<KlineHistoryRecord[]>([])
  const [historyPage, setHistoryPage] = useState(1)
  const [historyTotal, setHistoryTotal] = useState(0)
  const [historyType, setHistoryType] = useState('')
  const [historyPeriod, setHistoryPeriod] = useState('')
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyDetail, setHistoryDetail] = useState<KlineHistoryRecord | null>(null)
  const [error, setError] = useState('')
  const stopAutoRef = useRef(false)

  const loadHistory = useCallback(async (page = historyPage) => {
    setHistoryLoading(true)
    try {
      const result = await getKlineCheckHistory({ page, page_size: 10, data_type: historyType || undefined, period: historyPeriod || undefined })
      setHistory(result.items || [])
      setHistoryTotal(Number(result.total || 0))
    } catch (e) {
      setError(e instanceof Error ? e.message : '历史记录加载失败')
    } finally {
      setHistoryLoading(false)
    }
  }, [historyPage, historyPeriod, historyType])

  useEffect(() => { void loadHistory(1) }, [historyType, historyPeriod, loadHistory])

  async function waitForCheck(taskId: string, stopped?: () => boolean) {
    for (;;) {
      if (stopped?.()) throw new Error('stopped')
      const current = await getKlineCheckProgress(taskId)
      setProgress(current)
      if (current.status === 'completed') return current
      if (current.status === 'failed') throw new Error(current.message || '巡检失败')
      await sleep(1000)
    }
  }

  async function waitForRepair(taskId: string, stopped?: () => boolean) {
    for (;;) {
      if (stopped?.()) throw new Error('stopped')
      const current = await getKlineRepairProgress(taskId)
      setRepairProgress(current)
      if (current.status === 'completed') return current
      if (current.status === 'failed') throw new Error(current.error || '修复失败')
      await sleep(1000)
    }
  }

  async function runCheck() {
    setChecking(true); setError(''); setRows([]); setSelected([]); setSummary(null)
    try {
      const task = await startKlineCheckAll({ limit_codes: limitCodes, start_base_date: startDate })
      await waitForCheck(task.task_id)
      const result = await getKlineCheckResults(task.task_id)
      setRows(result.items || []); setSummary(result.summary || null)
    } catch (e) {
      setError(e instanceof Error ? e.message : '巡检失败')
    } finally { setChecking(false) }
  }

  const isRepairable = (row: KlineCheckRow) => row.data_type !== 'repair' && row.period_stats?.['1d']?.status !== 'no_data' && row.period_stats?.['1d']?.status !== 'pending_confirm'
  const autoSelectTop = (count = batchSize) => setSelected(rows.filter(isRepairable).slice(0, count).map(rowKey))

  async function repairSelectedInternal(skipConfirm = false, stopped?: () => boolean) {
    const picked = rows.filter((row) => selected.includes(rowKey(row)))
    const items = picked.filter(isRepairable).map((row) => ({ code: row.code || '', data_type: row.data_type || 'stock', periods: [...(row.incomplete_periods || []), ...(row.error_periods || [])] })).filter((item) => item.code && item.periods.length)
    if (!items.length) { if (!skipConfirm) setError('所选条目没有可修复周期'); return null }
    if (!skipConfirm && !window.confirm(`确定修复所选 ${picked.length} 个代码的问题周期吗？`)) return null
    setRepairing(true); setRepairResult(null); setError('')
    try {
      const task = await repairKlineBatch({ items, start_date: startDate })
      const result = await waitForRepair(task.task_id, stopped)
      const details = await getKlineRepairResults(task.task_id).catch(() => null)
      setRepairResult(details); setSelected([]); void loadHistory(1)
      return result
    } catch (e) {
      if (!(e instanceof Error && e.message === 'stopped')) setError(e instanceof Error ? e.message : '修复失败')
      return null
    } finally { setRepairing(false) }
  }

  async function repairOne(row: KlineCheckRow) {
    if (!row.code || !row.data_type || !isRepairable(row)) return
    if (!window.confirm(`确定修复 ${row.code} 的 ${[...(row.incomplete_periods || []), ...(row.error_periods || [])].join('、')} 吗？`)) return
    setRepairing(true); setError('')
    try {
      await repairKlineData({ code: row.code, data_type: row.data_type, period: row.worst_period || '1d', start_date: startDate })
      await loadHistory(1)
    } catch (e) { setError(e instanceof Error ? e.message : '单条修复失败') }
    finally { setRepairing(false) }
  }

  async function runAutoLoop() {
    if (autoRunning) return
    if (!window.confirm('将循环执行“巡检→选择前 N 条→批量修复”，直到没有可修复对象或点击停止。继续吗？')) return
    stopAutoRef.current = false; setAutoRunning(true)
    const stopped = () => stopAutoRef.current
    try {
      for (;;) {
        if (stopped()) break
        setSelected([]); setRows([]); setSummary(null); setChecking(true)
        const task = await startKlineCheckAll({ limit_codes: limitCodes, start_base_date: startDate })
        await waitForCheck(task.task_id, stopped)
        const result = await getKlineCheckResults(task.task_id)
        const nextRows = result.items || []
        setRows(nextRows); setSummary(result.summary || null); setChecking(false)
        const next = nextRows.filter(isRepairable).slice(0, batchSize).map(rowKey)
        setSelected(next)
        if (!next.length) break
        const items = nextRows.filter((row) => next.includes(rowKey(row)) && isRepairable(row)).map((row) => ({ code: row.code || '', data_type: row.data_type || 'stock', periods: [...(row.incomplete_periods || []), ...(row.error_periods || [])] })).filter((item) => item.code && item.periods.length)
        if (!items.length) break
        setRepairing(true)
        const repairTask = await repairKlineBatch({ items, start_date: startDate })
        await waitForRepair(repairTask.task_id, stopped)
        setRepairResult(await getKlineRepairResults(repairTask.task_id).catch(() => null)); setRepairing(false)
        await sleep(300)
      }
    } catch (e) {
      if (!(e instanceof Error && e.message === 'stopped')) setError(e instanceof Error ? e.message : '自动循环失败')
    } finally { setChecking(false); setRepairing(false); setAutoRunning(false) }
  }

  const counts = useMemo(() => {
    const total = Number(summary?.total_codes ?? rows.length)
    const pending = Number(summary?.pending_confirm_codes ?? rows.filter((r) => (r.pending_confirm_periods || []).length).length)
    const problem = Number(summary?.actionable_problem_codes ?? rows.filter((r) => (r.incomplete_periods || []).length || (r.error_periods || []).length).length)
    const noData = Number(summary?.no_data_codes ?? rows.filter((r) => r.period_stats?.['1d']?.status === 'no_data').length)
    const complete = Number(summary?.complete_codes ?? Math.max(0, total - pending - problem - noData))
    return { total, pending, problem, complete, rate: total ? ((complete / total) * 100).toFixed(2) : '0.00' }
  }, [rows, summary])

  const exportCheck = () => downloadCsv(`kline_check_all_${Date.now()}.csv`, ['类型', '代码', '名称', '最差周期', '完整周期', '待确认周期', '异常周期', '评分'], rows.map((r) => [label(r.data_type), r.code, r.name, r.worst_period, (r.complete_periods || []).join('|'), (r.pending_confirm_periods || []).join('|'), [...(r.incomplete_periods || []), ...(r.error_periods || [])].join('|'), r.score]))
  const exportRepair = () => { if (repairResult?.items?.length) downloadCsv(`kline_repair_${Date.now()}.csv`, ['类型', '代码', '名称', '周期', '结果', '错误'], repairResult.items.map((r) => [label(r.data_type), r.code, r.name, r.period, r.success ? '成功' : '失败', r.error])) }
  const progressPercent = progress.total ? Math.round((Number(progress.processed || 0) / Number(progress.total)) * 100) : 0
  const repairPercent = repairProgress.total ? Math.round((Number(repairProgress.processed || 0) / Number(repairProgress.total)) * 100) : 0

  return <div className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
    <PageHeader title="K线完整性检查" subtitle="巡检股票、指数和板块日线，支持批量、单条和自动循环修复。" actions={<div className="flex flex-wrap gap-2"><Button onClick={() => void runCheck()} disabled={checking || repairing || autoRunning}>{checking ? '巡检中…' : '一键巡检'}</Button><Button outline onClick={() => void runAutoLoop()} disabled={checking || repairing || autoRunning}>{autoRunning ? '自动循环中…' : '自动循环修复'}</Button>{autoRunning && <Button color="red" onClick={() => { stopAutoRef.current = true }}>停止</Button>}</div>} />
    {error && <div role="alert" className="mb-4 rounded-lg border border-red-500/20 bg-red-50 px-4 py-3 text-sm text-red-700 dark:bg-red-500/10 dark:text-red-300">{error}</div>}
    <Card className="mb-6"><SectionTitle title="巡检配置" /><div className="mt-4 flex flex-wrap items-end gap-4"><label className="text-sm text-zinc-600 dark:text-zinc-300">返回数量<input className="mt-1 block w-32 rounded-md border border-zinc-300 bg-transparent px-3 py-2" type="number" min={100} max={2000} step={50} value={limitCodes} onChange={(e) => setLimitCodes(Number(e.target.value))} /></label><label className="text-sm text-zinc-600 dark:text-zinc-300">起始日期<input className="mt-1 block rounded-md border border-zinc-300 bg-transparent px-3 py-2" type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} /></label><label className="text-sm text-zinc-600 dark:text-zinc-300">自动批次<input className="mt-1 block w-28 rounded-md border border-zinc-300 bg-transparent px-3 py-2" type="number" min={10} max={500} step={10} value={batchSize} onChange={(e) => setBatchSize(Number(e.target.value))} /></label><Button outline onClick={() => void repairSelectedInternal()} disabled={repairing || checking || autoRunning || selected.length === 0}>修复所选（{selected.length}）</Button><Button plain onClick={() => autoSelectTop()} disabled={!rows.length || repairing || checking || autoRunning}>自动选择前 {batchSize}</Button><Button plain onClick={exportCheck} disabled={!rows.length}>导出巡检</Button></div>{(checking || repairing) && <div className="mt-4 space-y-2 text-xs text-zinc-500"><div>巡检 {progressPercent}% · {progress.processed || 0} / {progress.total || 0} · {String(progress.status || '—')}</div><div className="h-2 overflow-hidden rounded bg-zinc-200 dark:bg-zinc-700"><div className="h-full bg-blue-500" style={{ width: `${progressPercent}%` }} /></div><div>修复 {repairPercent}% · {repairProgress.processed || 0} / {repairProgress.total || 0} · 成功 {repairProgress.success || 0} · 失败 {repairProgress.failed || 0}</div></div>}</Card>
    <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-5"><StatCard label="总代码" value={counts.total} /><StatCard label="完整" value={counts.complete} tone="up" /><StatCard label="待确认" value={counts.pending} tone="warning" /><StatCard label="异常" value={counts.problem} tone="down" /><StatCard label="完整率" value={`${counts.rate}%`} /></div>
    <Card className="mb-6"><SectionTitle title={`巡检结果（${rows.length}）`} actions={<div className="flex items-center gap-2">{rows.length > 0 && <input type="checkbox" checked={selected.length === rows.length} onChange={(e) => setSelected(e.target.checked ? rows.map(rowKey) : [])} />}<Badge color="zinc">已选 {selected.length}</Badge></div>} /><div className="mt-4 overflow-x-auto"><table className="min-w-full text-sm"><thead><tr className="text-left text-xs text-zinc-500"><th className="px-2 py-2" /><th className="px-2 py-2">类型</th><th className="px-2 py-2">代码</th><th className="px-2 py-2">名称</th><th className="px-2 py-2">最差周期</th><th className="px-2 py-2">异常周期</th><th className="px-2 py-2">评分</th><th className="px-2 py-2">操作</th></tr></thead><tbody>{rows.map((row) => { const key = rowKey(row); const bad = [...(row.incomplete_periods || []), ...(row.error_periods || [])]; return <tr key={key} className="border-t border-zinc-200/70 dark:border-white/10"><td className="px-2 py-2"><input type="checkbox" checked={selected.includes(key)} onChange={(e) => setSelected((current) => e.target.checked ? [...current, key] : current.filter((item) => item !== key))} /></td><td className="px-2 py-2">{label(row.data_type)}</td><td className="px-2 py-2 font-mono">{row.code || '—'}</td><td className="px-2 py-2">{row.name || '—'}</td><td className="px-2 py-2">{row.worst_period || '—'}</td><td className="px-2 py-2">{bad.length ? bad.join('、') : '完整'}</td><td className="px-2 py-2">{Number(row.score || 0).toFixed(2)}</td><td className="px-2 py-2"><Button plain onClick={() => void repairOne(row)} disabled={repairing || checking || autoRunning || !isRepairable(row)}>单条修复</Button></td></tr> })}</tbody></table></div>{!rows.length && <p className="mt-4 text-sm text-zinc-500">尚未执行巡检。</p>}</Card>
    {repairResult?.items?.length ? <Card className="mb-6"><SectionTitle title="最近一次修复结果" actions={<Button plain onClick={exportRepair}>导出 CSV</Button>} /><p className="mt-3 text-sm text-zinc-600 dark:text-zinc-300">总任务 {repairResult.summary?.total ?? repairResult.items.length} · 成功 {repairResult.summary?.success ?? repairResult.items.filter((r) => r.success).length} · 失败 {repairResult.summary?.failed ?? repairResult.items.filter((r) => !r.success).length} · 改善 {repairResult.summary?.improved ?? 0}</p><div className="mt-3 overflow-x-auto"><table className="min-w-full text-sm"><tbody>{repairResult.items.slice(0, 50).map((item, index) => <tr key={index} className="border-t border-zinc-200/70 dark:border-white/10"><td className="px-2 py-2">{label(item.data_type)}</td><td className="px-2 py-2 font-mono">{item.code || '—'}</td><td className="px-2 py-2">{item.period || '—'}</td><td className="px-2 py-2">{item.success ? '成功' : `失败：${item.error || '—'}`}</td></tr>)}</tbody></table></div></Card> : null}
    <Card><SectionTitle title="历史检查记录" actions={<div className="flex flex-wrap gap-2"><select className="rounded-md border border-zinc-300 bg-transparent px-2 py-1 text-sm" value={historyType} onChange={(e) => { setHistoryType(e.target.value); setHistoryPage(1) }}><option value="">全部类型</option><option value="stock">股票</option><option value="index">指数</option><option value="sector">板块</option><option value="repair">修复记录</option></select><input className="w-28 rounded-md border border-zinc-300 bg-transparent px-2 py-1 text-sm" placeholder="周期" value={historyPeriod} onChange={(e) => { setHistoryPeriod(e.target.value); setHistoryPage(1) }} /><Button plain onClick={() => void loadHistory(historyPage)}>刷新</Button></div>} /><div className="mt-4 overflow-x-auto"><table className="min-w-full text-sm"><thead><tr className="text-left text-xs text-zinc-500"><th className="px-2 py-2">时间</th><th className="px-2 py-2">类型</th><th className="px-2 py-2">周期</th><th className="px-2 py-2">总数</th><th className="px-2 py-2">完整</th><th className="px-2 py-2">状态</th><th className="px-2 py-2">详情</th></tr></thead><tbody>{history.map((record, index) => <tr key={String(record.id ?? index)} className="border-t border-zinc-200/70 dark:border-white/10"><td className="px-2 py-2">{formatDateTime(record.check_time)}</td><td className="px-2 py-2">{label(String(record.data_type || ''))}</td><td className="px-2 py-2">{String(record.period || '—')}</td><td className="px-2 py-2">{String(record.total_checked ?? '—')}</td><td className="px-2 py-2">{String(record.complete_count ?? '—')}</td><td className="px-2 py-2">{String(record.status || '—')}</td><td className="px-2 py-2"><Button plain onClick={async () => { if (record.id == null) return; try { setHistoryDetail(await getKlineCheckHistoryDetail(record.id)) } catch (e) { setError(e instanceof Error ? e.message : '详情加载失败') } }}>查看</Button></td></tr>)}</tbody></table></div>{historyLoading && <p className="mt-3 text-sm text-zinc-500">加载中…</p>}{!historyLoading && !history.length && <p className="mt-3 text-sm text-zinc-500">暂无历史记录。</p>}<div className="mt-4 flex items-center justify-between text-sm text-zinc-500"><span>第 {historyPage} 页 · 共 {historyTotal} 条</span><div className="flex gap-2"><Button plain disabled={historyPage <= 1} onClick={() => { const p = historyPage - 1; setHistoryPage(p); void loadHistory(p) }}>上一页</Button><Button plain disabled={historyPage * 10 >= historyTotal} onClick={() => { const p = historyPage + 1; setHistoryPage(p); void loadHistory(p) }}>下一页</Button></div></div></Card>
    {historyDetail && <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={() => setHistoryDetail(null)}><div className="max-h-[80vh] w-full max-w-4xl overflow-auto rounded-xl bg-white p-6 shadow-xl dark:bg-zinc-900" onClick={(e) => e.stopPropagation()}><div className="flex items-center justify-between"><h2 className="text-lg font-semibold">检查详情</h2><Button plain onClick={() => setHistoryDetail(null)}>关闭</Button></div><pre className="mt-4 whitespace-pre-wrap text-xs text-zinc-600 dark:text-zinc-300">{JSON.stringify(historyDetail, null, 2)}</pre></div></div>}
  </div>
}
