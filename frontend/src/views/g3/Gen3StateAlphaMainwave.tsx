import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import type { EChartsCoreOption } from 'echarts'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Alert } from '@/components/catalyst/alert'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/catalyst/table'
import { EChart } from '@/components/EChart'
import { Card, ChangeValue, EmptyState, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import { getGen3StateAlphaMainwaveOpportunities } from '@/api/trading'

interface KlineRow {
  date?: string; trade_date?: string
  open?: number | string; close?: number | string; low?: number | string; high?: number | string
  volume?: number | string; amount?: number | string
  change_pct?: number | string
  synthetic_ohlc?: boolean
}

interface ComponentStock {
  code?: string; name?: string; latest_close?: number | string; change_pct?: number | string
  amount?: number | string; latest_date?: string; weight?: number | string
}

interface SectorRow {
  sector_name?: string
  mainline_sector_code?: string; sector_code?: string; l2_sector_code?: string
  top_candidate_names?: string
  state?: string; state_label?: string
  avg_sector_diffusion_score?: number | string
  raw_sector_diffusion_score?: number | string
  candidate_count?: number; watch_candidate_count?: number
  max_wave_style_score?: number | string
  kline_daily?: KlineRow[]
  component_stocks?: ComponentStock[]
}

interface CandidateRow {
  code?: string; name?: string; sector_name?: string
  is_recommended?: boolean
  template_label?: string
  wave_style_score?: number | string
  sector_signal_count?: number | string
  m30_confirmed?: boolean; m30_status?: string
  source?: string
  score_gap_to_formal?: number | string
  sector_diffusion_score?: number | string
  ret5?: number | string; ret20?: number | string
  block_reason?: string
}

interface ArtifactInfo { exists?: boolean; modified_at?: string; path?: string }

interface MainwavePayload {
  summary?: {
    entry_date?: string; decision_date?: string
    sector_count?: number; important_sector_count?: number; strong_sector_count?: number
    recommended_count?: number; candidate_count?: number; m30_ok_count?: number
    sector_watch_candidate_count?: number; watch_candidate_count?: number
    index_mom60?: number | string; index_heat_label?: string
    min_score?: number | string; min_sector_signal_count?: number | string
    max_index_mom60?: number | string
    source_label?: string; source_path?: string
  }
  sector_opportunities?: SectorRow[]
  sector_kline_by_name?: Record<string, KlineRow[]>
  candidates?: CandidateRow[]
  watch_candidates?: CandidateRow[]
  sector_watch_candidates?: CandidateRow[]
  recommended_tickets?: { name?: string; code?: string }[]
  diagnostics?: { diagnosis_code?: string }
  artifacts?: Record<string, ArtifactInfo>
}

type CandidateMode = 'all' | 'recommended' | 'watch'
type WatchMode = 'sector' | 'all'

const fmt = (value: unknown, digits = 2): string => {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  return n.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}
const pct = (value: unknown): string => {
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(2)}%` : '--'
}
const signedPctValue = (value: unknown): string => {
  const n = Number(value)
  return Number.isFinite(n) ? `${n > 0 ? '+' : ''}${n.toFixed(2)}%` : '--'
}
const money = (value: unknown): string => {
  const n = Number(value)
  if (!Number.isFinite(n) || n <= 0) return '--'
  if (n >= 100000000) return `${(n / 100000000).toFixed(2)}亿`
  if (n >= 10000) return `${(n / 10000).toFixed(2)}万`
  return fmt(n, 0)
}

const sectorTagColor = (state?: string) =>
  state === 'strong_mainwave' ? 'emerald' : state === 'important_industry' ? 'amber' : 'zinc'

const statusLabel = (value?: string) => {
  const labels: Record<string, string> = {
    ok: '通过', blocked: '阻断', missing: '缺失', unknown: '未确认', not_formal_candidate: '非正式候选',
  }
  const raw = String(value || 'unknown')
  return labels[raw] || raw
}

const sourceLabel = (value?: string) => {
  const labels: Record<string, string> = {
    next_trade_ticket: '下一交易日票据',
    pre_confirm_preview: '机构主升预确认',
    mainwave_latest_candidates: '机构主升当前候选',
    mainwave_pool_top500: '主升观察池',
    current_wave_top_candidates: '观察池Top',
  }
  const raw = String(value || '')
  return labels[raw] || raw || '--'
}

const sectorCode = (row: SectorRow): string => {
  const code = String(row?.mainline_sector_code || row?.sector_code || row?.l2_sector_code || '').trim()
  if (code.startsWith('qmt:SW') || code.startsWith('industry:')) return ''
  return code
}

const hasTrendAdjustment = (row: SectorRow): boolean => {
  const raw = Number(row?.raw_sector_diffusion_score)
  const adjusted = Number(row?.avg_sector_diffusion_score)
  return Number.isFinite(raw) && Number.isFinite(adjusted) && Math.abs(raw - adjusted) >= 0.05
}

const ARTIFACT_LABELS: Record<string, string> = {
  state_alpha_afterhours_summary: '盘后候选摘要',
  state_alpha_afterhours_tickets: '盘后买入票据',
  state_alpha_summary: '当前摘要',
  state_alpha_tickets: '当前票据',
  mainwave_runtime_summary: '机构主升摘要',
  mainwave_runtime_candidates: '机构主升候选',
  mainwave_runtime_blocked_candidates: '机构主升阻断',
  mainwave_watch_pool: '主升观察池',
  current_wave_top_candidates: '观察池Top',
  current_wave_template_pass: '模板通过池',
}

function windowReturn(rows: KlineRow[], days: number): number | null {
  if (!Array.isArray(rows) || !rows.length) return null
  const span = rows.slice(-Math.min(Number(days) || rows.length, rows.length))
  if (span.length < 2) return null
  const firstClose = Number(span[0]?.close)
  const lastClose = Number(span[span.length - 1]?.close)
  if (!Number.isFinite(firstClose) || firstClose <= 0 || !Number.isFinite(lastClose)) return null
  return lastClose / firstClose - 1
}

export default function Gen3StateAlphaMainwave() {
  const [loading, setLoading] = useState(false)
  const [payload, setPayload] = useState<MainwavePayload>({})
  const [selectedSector, setSelectedSector] = useState('')
  const [candidateMode, setCandidateMode] = useState<CandidateMode>('all')
  const [watchMode, setWatchMode] = useState<WatchMode>('sector')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await getGen3StateAlphaMainwaveOpportunities({ limit: 120 })
      setPayload((res as unknown as MainwavePayload) || {})
    } catch (e) {
      // 错误已由 request 拦截器 toast
      console.error('主升行业机会加载失败', e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const summary = payload.summary || {}
  const sectorRows = payload.sector_opportunities || []

  useEffect(() => {
    if (!selectedSector && sectorRows.length) setSelectedSector(sectorRows[0].sector_name || '')
  }, [sectorRows, selectedSector])

  const selectedSectorRow = useMemo(
    () => sectorRows.find((item) => item.sector_name === selectedSector) || {} as SectorRow,
    [sectorRows, selectedSector],
  )
  const selectedKlineRows = useMemo(() => {
    const fromRow = selectedSectorRow.kline_daily
    if (Array.isArray(fromRow) && fromRow.length) return fromRow
    const fromMap = payload.sector_kline_by_name?.[selectedSector]
    return Array.isArray(fromMap) ? fromMap : []
  }, [selectedSectorRow, payload.sector_kline_by_name, selectedSector])

  const selectedComponentRows = useMemo(() => {
    const rows = selectedSectorRow.component_stocks || []
    return rows.slice().sort((a, b) => {
      const aAmount = Number(a?.amount); const bAmount = Number(b?.amount)
      if (Number.isFinite(aAmount) || Number.isFinite(bAmount)) return (Number.isFinite(bAmount) ? bAmount : -1) - (Number.isFinite(aAmount) ? aAmount : -1)
      const aWeight = Number(a?.weight); const bWeight = Number(b?.weight)
      if (Number.isFinite(aWeight) || Number.isFinite(bWeight)) return (Number.isFinite(bWeight) ? bWeight : -1) - (Number.isFinite(aWeight) ? aWeight : -1)
      return String(a?.code || '').localeCompare(String(b?.code || ''))
    })
  }, [selectedSectorRow])

  const selectedKlineStats = useMemo(() => {
    const rows = selectedKlineRows
    if (!rows.length) return {} as {
      date?: string; close?: number; changePct?: number; ret20?: number | null
      ret60?: number | null; ret120?: number | null; amount?: number; synthetic?: boolean
    }
    const last = rows[rows.length - 1] || {}
    return {
      date: last.date || last.trade_date || '',
      close: Number(last.close),
      changePct: Number(last.change_pct),
      ret20: windowReturn(rows, 20),
      ret60: windowReturn(rows, 60),
      ret120: windowReturn(rows, 120),
      amount: Number(last.amount),
      synthetic: Boolean(last.synthetic_ohlc),
    }
  }, [selectedKlineRows])

  const filteredCandidates = useMemo(() => {
    return (payload.candidates || []).filter((item) => {
      if (selectedSector && item.sector_name !== selectedSector) return false
      if (candidateMode === 'recommended') return Boolean(item.is_recommended)
      if (candidateMode === 'watch') return !item.is_recommended
      return true
    })
  }, [payload.candidates, selectedSector, candidateMode])

  const filteredWatchCandidates = useMemo(() => {
    const base = watchMode === 'sector' ? (payload.sector_watch_candidates || []) : (payload.watch_candidates || [])
    return base.filter((item) => !selectedSector || item.sector_name === selectedSector)
  }, [watchMode, payload.sector_watch_candidates, payload.watch_candidates, selectedSector])

  const recommendedNames = (payload.recommended_tickets || [])
    .map((item) => item.name || item.code).filter(Boolean).join(' / ')

  const chartOption = useMemo<EChartsCoreOption>(() => {
    const rows = selectedKlineRows
    if (!rows.length) return {}
    const dates = rows.map((item) => item.date || item.trade_date)
    const candles = rows.map((item) => [Number(item.open), Number(item.close), Number(item.low), Number(item.high)])
    const volumes = rows.map((item, index) => {
      const open = Number(item.open); const close = Number(item.close)
      return {
        value: Number(item.volume || 0),
        itemStyle: { color: close >= open ? '#d92d20' : '#039855' },
        name: dates[index],
      }
    })
    const validPrices = rows.flatMap((item) => [Number(item.low), Number(item.high)])
      .filter((v) => Number.isFinite(v) && v > 0)
    const minPrice = validPrices.length ? Math.min(...validPrices) : null
    const maxPrice = validPrices.length ? Math.max(...validPrices) : null
    const padding = minPrice !== null && maxPrice !== null ? Math.max((maxPrice - minPrice) * 0.08, maxPrice * 0.01) : 0
    const visibleBars = Math.min(160, rows.length)
    const zoomStart = rows.length > visibleBars ? Math.max(0, ((rows.length - visibleBars) / rows.length) * 100) : 0
    return {
      animation: false,
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        formatter(params: unknown) {
          const list = params as { dataIndex?: number }[]
          const idx = list?.[0]?.dataIndex ?? 0
          const item = rows[idx] || {}
          return [
            `${selectedSector || '--'} ${item.date || '--'}`,
            `开 ${fmt(item.open, 2)} 高 ${fmt(item.high, 2)} 低 ${fmt(item.low, 2)} 收 ${fmt(item.close, 2)}`,
            `涨跌 ${signedPctValue(item.change_pct)}`,
            `成交量 ${fmt(item.volume, 0)}`,
            `成交额 ${money(item.amount)}`,
          ].join('<br/>')
        },
      },
      grid: [
        { left: 54, right: 18, top: 18, height: '66%' },
        { left: 54, right: 18, top: '80%', height: '12%' },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: zoomStart, end: 100, filterMode: 'none' },
        { type: 'slider', xAxisIndex: [0, 1], start: zoomStart, end: 100, height: 24, bottom: 2, brushSelect: false, filterMode: 'none' },
      ],
      xAxis: [
        { type: 'category', data: dates, boundaryGap: false, axisLine: { onZero: false }, splitLine: { show: false } },
        { type: 'category', gridIndex: 1, data: dates, boundaryGap: false, axisLabel: { show: false }, axisTick: { show: false }, axisLine: { show: false } },
      ],
      yAxis: [
        { scale: true, min: minPrice === null ? null : Math.max(0, minPrice - padding), max: maxPrice === null ? null : maxPrice + padding },
        { gridIndex: 1, scale: true, axisLabel: { formatter: (v: number) => money(v) }, splitLine: { show: false } },
      ],
      series: [
        {
          name: '日K',
          type: 'candlestick',
          data: candles,
          barMinWidth: 4,
          barMaxWidth: 12,
          itemStyle: { color: '#d92d20', color0: '#039855', borderColor: '#d92d20', borderColor0: '#039855' },
        },
        { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: volumes, barMinWidth: 3, barMaxWidth: 10 },
      ],
    } as EChartsCoreOption
  }, [selectedKlineRows, selectedSector])

  const heatTone = (() => {
    const value = Number(summary.index_mom60)
    if (!Number.isFinite(value)) return 'default' as const
    if (value > 0.1) return 'up' as const
    if (value > 0.05) return 'warning' as const
    return 'down' as const
  })()

  const diagnosticsText = `当前读取源 ${summary.source_label || '--'}，诊断码 ${payload.diagnostics?.diagnosis_code || 'NO_DIAGNOSIS'}，路径 ${summary.source_path || '--'}`

  const statCell = (label: string, value: ReactNode, sub?: string) => (
    <div className="min-h-[4.5rem] rounded-lg border border-zinc-100 bg-zinc-50 px-3 py-2 dark:border-zinc-800 dark:bg-zinc-800/40">
      <span className="block text-xs text-zinc-500 dark:text-zinc-400">{label}</span>
      <strong className="mt-1 block truncate text-base tabular-nums text-zinc-950 dark:text-white">{value}</strong>
      {sub && <small className="block truncate text-xs text-zinc-500 dark:text-zinc-400">{sub}</small>}
    </div>
  )

  return (
    <div className="mx-auto flex w-full max-w-[100rem] flex-col gap-4 p-4 sm:p-6">
      <PageHeader
        title="主升行业机会"
        subtitle={
          <>
            <span className="block text-xs font-bold text-indigo-600 dark:text-indigo-400">G3 第三代策略 / institutional_mainwave</span>
            把行业机会、板块内观察票和正式买入候选分层展示，避免把强板块里的观察票误当成买点。
          </>
        }
        actions={<Button onClick={() => void load()} disabled={loading}>{loading ? '刷新中…' : '刷新'}</Button>}
      />

      <Alert>
        <span className="text-sm text-zinc-700 dark:text-zinc-300">
          行业机会来自板块扩散和主升观察池；当前合同主升分门槛 {summary.min_score ?? '待加载'}，同业共振 {summary.min_sector_signal_count ?? '待加载'}，仍需已完成30m确认和指数门槛。
        </span>
      </Alert>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <StatCard label="机会交易日" value={summary.entry_date || '--'} hint={`决策日 ${summary.decision_date || '--'}`} />
        <StatCard label="行业机会" value={fmt(summary.sector_count, 0)} hint={`重要 ${fmt(summary.important_sector_count, 0)} / 强主升 ${fmt(summary.strong_sector_count, 0)}`} />
        <StatCard label="正式买入票" value={fmt(summary.recommended_count, 0)} hint={recommendedNames || '暂无正式推荐'} />
        <StatCard label="正式候选" value={fmt(summary.candidate_count, 0)} hint={`30m 通过 ${fmt(summary.m30_ok_count, 0)}`} />
        <StatCard label="板块观察票" value={fmt(summary.sector_watch_candidate_count, 0)} hint={`观察池 ${fmt(summary.watch_candidate_count, 0)}`} tone="warning" />
        <StatCard label="指数60日热度" value={pct(summary.index_mom60)} hint={summary.index_heat_label || '--'} tone={heatTone} />
      </section>

      <section className="grid gap-4 xl:grid-cols-[0.9fr_1.1fr]">
        <Card>
          <SectionTitle title="行业机会排序" />
          <p className="-mt-1 mb-3 text-sm text-zinc-500 dark:text-zinc-400">正式候选和观察票分开计数；点击行业后，下方两张表同步过滤。</p>
          <div className="max-h-[32rem] overflow-auto">
            <Table dense>
              <TableHead>
                <TableRow>
                  <TableHeader>行业/板块</TableHeader>
                  <TableHeader>状态</TableHeader>
                  <TableHeader className="text-right">机会分</TableHeader>
                  <TableHeader className="text-right">正式</TableHeader>
                  <TableHeader className="text-right">观察</TableHeader>
                  <TableHeader className="text-right">最高主升</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {sectorRows.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={6} className="py-8 text-center text-sm text-zinc-500 dark:text-zinc-400">暂无行业机会</TableCell>
                  </TableRow>
                ) : sectorRows.map((row) => (
                  <TableRow
                    key={row.sector_name}
                    className={`cursor-pointer ${selectedSector === row.sector_name ? 'bg-zinc-100 dark:bg-zinc-800' : ''}`}
                    onClick={() => setSelectedSector(row.sector_name || '')}
                  >
                    <TableCell>
                      <div className="flex items-center gap-1.5">
                        <strong className="truncate text-sm text-zinc-950 dark:text-white">{row.sector_name || '--'}</strong>
                        {sectorCode(row) && (
                          <span className="shrink-0 rounded border border-zinc-200 bg-zinc-50 px-1 font-mono text-[11px] leading-4 text-zinc-500 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-400">
                            {sectorCode(row)}
                          </span>
                        )}
                      </div>
                      <small className="block truncate text-xs text-zinc-500 dark:text-zinc-400">{row.top_candidate_names || '--'}</small>
                    </TableCell>
                    <TableCell>
                      <Badge color={sectorTagColor(row.state)}>{row.state_label || statusLabel(row.state)}</Badge>
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex flex-col items-end leading-tight">
                        <strong className="text-[13px] tabular-nums text-zinc-950 dark:text-white">{fmt(row.avg_sector_diffusion_score, 1)}</strong>
                        {hasTrendAdjustment(row) && (
                          <small className="whitespace-nowrap text-[11px] text-zinc-400">原扩散 {fmt(row.raw_sector_diffusion_score, 1)}</small>
                        )}
                      </div>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{fmt(row.candidate_count, 0)}</TableCell>
                    <TableCell className="text-right tabular-nums">{fmt(row.watch_candidate_count, 0)}</TableCell>
                    <TableCell className="text-right tabular-nums">{fmt(row.max_wave_style_score, 1)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
          </Table>
          </div>
        </Card>

        <Card className="min-w-0">
          <SectionTitle
            title={`${selectedSector || '选择行业'} 日K走势`}
            actions={<Badge color={selectedKlineRows.length >= 260 ? 'emerald' : 'amber'}>{fmt(selectedKlineRows.length, 0)} 日</Badge>}
          />
          <p className="-mt-1 mb-3 text-sm text-zinc-500 dark:text-zinc-400">点击左侧行业后，查看该板块近 260 个交易日的走势、成交量和量价节奏。</p>

          <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
            {statCell('最新收盘', fmt(selectedKlineStats.close, 2), `${selectedKlineStats.synthetic ? '净值代理 ' : ''}${selectedKlineStats.date || '--'}`)}
            {statCell('当日涨跌', <ChangeValue value={selectedKlineStats.changePct} />, '板块指数口径')}
            {statCell('20日涨幅', <ChangeValue value={selectedKlineStats.ret20 == null ? null : selectedKlineStats.ret20 * 100} />, '近期强弱')}
            {statCell('60日涨幅', <ChangeValue value={selectedKlineStats.ret60 == null ? null : selectedKlineStats.ret60 * 100} />, '主升观察')}
            {statCell('120日涨幅', <ChangeValue value={selectedKlineStats.ret120 == null ? null : selectedKlineStats.ret120 * 100} />, '中期背景')}
            {statCell('成交额', money(selectedKlineStats.amount), '最新交易日')}
          </div>

          {selectedKlineRows.length > 0 ? (
            <EChart option={chartOption} height={460} loading={loading} />
          ) : (
            <EmptyState text="暂无该板块日K数据" className="h-[460px]" />
          )}

          <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
            {statCell('正式门槛', fmt(summary.min_score, 0), '主升分')}
            {statCell('同业共振', fmt(summary.min_sector_signal_count, 0), '同日主升骨架数')}
            {statCell('市场热度', pct(summary.max_index_mom60), '高于只观察')}
          </div>

          <div className="mt-4 border-t border-zinc-100 pt-3 dark:border-zinc-800">
            <div className="mb-2 flex items-center justify-between">
              <h3 className="text-sm font-semibold text-zinc-950 dark:text-white">当前板块成分股</h3>
              <small className="text-xs text-zinc-500 dark:text-zinc-400">{selectedSector || '--'} · {fmt(selectedComponentRows.length, 0)} 只</small>
            </div>
            <div className="max-h-72 overflow-auto">
              <Table dense>
                <TableHead>
                  <TableRow>
                    <TableHeader>代码</TableHeader>
                    <TableHeader>名称</TableHeader>
                    <TableHeader className="text-right">最新价</TableHeader>
                    <TableHeader className="text-right">涨跌幅</TableHeader>
                    <TableHeader className="text-right">成交额</TableHeader>
                    <TableHeader>日期</TableHeader>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {selectedComponentRows.length === 0 ? (
                    <TableRow>
                      <TableCell colSpan={6} className="py-6 text-center text-sm text-zinc-500 dark:text-zinc-400">暂无成分股数据</TableCell>
                    </TableRow>
                  ) : selectedComponentRows.map((row) => (
                    <TableRow key={row.code}>
                      <TableCell className="tabular-nums">{row.code}</TableCell>
                      <TableCell className="max-w-[10rem] truncate">{row.name}</TableCell>
                      <TableCell className="text-right tabular-nums">{fmt(row.latest_close, 2)}</TableCell>
                      <TableCell className="text-right tabular-nums"><ChangeValue value={Number(row.change_pct)} /></TableCell>
                      <TableCell className="text-right tabular-nums">{money(row.amount)}</TableCell>
                      <TableCell className="tabular-nums">{row.latest_date}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </div>
        </Card>
      </section>

      <Card>
        <SectionTitle
          title={`${selectedSector || '全部'} 正式候选`}
          actions={
            <div className="flex flex-wrap items-center gap-2">
              <select
                className="h-8 rounded-lg border border-zinc-200 bg-white px-2 text-sm text-zinc-900 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-100"
                value={selectedSector}
                onChange={(e) => setSelectedSector(e.target.value)}
              >
                <option value="">全部行业</option>
                {sectorRows.map((item) => item.sector_name).filter(Boolean).map((name) => (
                  <option key={name} value={name}>{name}</option>
                ))}
              </select>
              <div className="flex gap-1">
                {([['all', '全部'], ['recommended', '推荐买入'], ['watch', '候选观察']] as [CandidateMode, string][]).map(([mode, label]) =>
                  candidateMode === mode
                    ? <Button key={mode} onClick={() => setCandidateMode(mode)}>{label}</Button>
                    : <Button key={mode} plain onClick={() => setCandidateMode(mode)}>{label}</Button>,
                )}
              </div>
            </div>
          }
        />
        <p className="-mt-1 mb-3 text-sm text-zinc-500 dark:text-zinc-400">只展示已经进入 G3 主升候选链或票据链的股票。</p>
        <div className="max-h-[26rem] overflow-auto">
          <Table dense>
            <TableHead>
              <TableRow>
                <TableHeader>代码</TableHeader>
                <TableHeader>名称</TableHeader>
                <TableHeader>行业</TableHeader>
                <TableHeader>类型</TableHeader>
                <TableHeader>模板</TableHeader>
                <TableHeader className="text-right">主升分</TableHeader>
                <TableHeader className="text-right">同业共振</TableHeader>
                <TableHeader>30m突破</TableHeader>
                <TableHeader>来源</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {filteredCandidates.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={9} className="py-8 text-center text-sm text-zinc-500 dark:text-zinc-400">暂无正式候选</TableCell>
                </TableRow>
              ) : filteredCandidates.map((row) => (
                <TableRow key={row.code}>
                  <TableCell className="tabular-nums">{row.code}</TableCell>
                  <TableCell>{row.name}</TableCell>
                  <TableCell>{row.sector_name}</TableCell>
                  <TableCell><Badge color={row.is_recommended ? 'emerald' : 'zinc'}>{row.is_recommended ? '推荐买入' : '候选观察'}</Badge></TableCell>
                  <TableCell className="max-w-[10rem] truncate">{row.template_label}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmt(row.wave_style_score, 1)}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmt(row.sector_signal_count, 0)}</TableCell>
                  <TableCell><Badge color={row.m30_confirmed ? 'emerald' : 'amber'}>{statusLabel(row.m30_status)}</Badge></TableCell>
                  <TableCell>{sourceLabel(row.source)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </Card>

      <Card>
        <SectionTitle
          title={`${selectedSector || '全部'} 板块内观察票`}
          actions={
            <div className="flex gap-1">
              {([['sector', '强板块'], ['all', '全部观察']] as [WatchMode, string][]).map(([mode, label]) =>
                watchMode === mode
                  ? <Button key={mode} onClick={() => setWatchMode(mode)}>{label}</Button>
                  : <Button key={mode} plain onClick={() => setWatchMode(mode)}>{label}</Button>,
              )}
            </div>
          }
        />
        <p className="-mt-1 mb-3 text-sm text-zinc-500 dark:text-zinc-400">展示强板块里的观察票，例如江化微这类“板块强、个股未达正式买点”的股票。</p>
        <div className="max-h-[26rem] overflow-auto">
          <Table dense>
            <TableHead>
              <TableRow>
                <TableHeader>代码</TableHeader>
                <TableHeader>名称</TableHeader>
                <TableHeader>行业</TableHeader>
                <TableHeader>标签</TableHeader>
                <TableHeader className="text-right">主升分</TableHeader>
                <TableHeader className="text-right">差正式线</TableHeader>
                <TableHeader className="text-right">扩散分</TableHeader>
                <TableHeader className="text-right">5日</TableHeader>
                <TableHeader className="text-right">20日</TableHeader>
                <TableHeader>阻断原因</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {filteredWatchCandidates.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={10} className="py-8 text-center text-sm text-zinc-500 dark:text-zinc-400">暂无板块观察票</TableCell>
                </TableRow>
              ) : filteredWatchCandidates.map((row) => (
                <TableRow key={row.code}>
                  <TableCell className="tabular-nums">{row.code}</TableCell>
                  <TableCell>{row.name}</TableCell>
                  <TableCell>{row.sector_name}</TableCell>
                  <TableCell>{row.template_label}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmt(row.wave_style_score, 1)}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmt(row.score_gap_to_formal, 1)}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmt(row.sector_diffusion_score, 1)}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.ret5)}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.ret20)}</TableCell>
                  <TableCell className="max-w-[14rem] truncate text-zinc-500 dark:text-zinc-400">{row.block_reason}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </Card>

      <Card>
        <SectionTitle title="数据来源" />
        <p className="-mt-1 mb-3 text-sm text-zinc-500 dark:text-zinc-400">{diagnosticsText}</p>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-4">
          {Object.entries(ARTIFACT_LABELS).map(([key, label]) => {
            const info = payload.artifacts?.[key] || {}
            return (
              <div key={key} className="min-h-[4.5rem] rounded-lg border border-zinc-100 bg-white px-3 py-2 dark:border-zinc-800 dark:bg-zinc-900">
                <span className="block text-xs text-zinc-500 dark:text-zinc-400">{label}</span>
                <strong className={`mt-1 block text-sm ${info.exists ? 'text-emerald-600 dark:text-emerald-400' : 'text-zinc-400'}`}>
                  {info.exists ? '存在' : '缺失'}
                </strong>
                <small className="block truncate text-xs text-zinc-400">{info.modified_at || info.path || '--'}</small>
              </div>
            )
          })}
        </div>
      </Card>
    </div>
  )
}
