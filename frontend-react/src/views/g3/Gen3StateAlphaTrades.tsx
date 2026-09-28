import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert } from '@/components/catalyst/alert'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/catalyst/table'
import { useToast } from '@/components/toast'
import EChart from '@/components/EChart'
import {
  Card,
  ChangeValue,
  EmptyState,
  PageHeader,
  SectionTitle,
  StatCard,
} from '@/components/ui'
import { compactStatusWithZh } from '@/utils/g3StatusText'
import {
  getGen3StateAlphaCurrent,
  getGen3StateAlphaHistoricalTrades,
} from '@/api/trading'

type Row = Record<string, any>

function truthy(value: any): boolean {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0
  if (typeof value === 'string') return ['1', 'true', 'yes', 'ok'].includes(value.trim().toLowerCase())
  return false
}

function fmt(value: any, digits = 2): string {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  return n.toFixed(digits)
}

function pct(value: any): string {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  return `${(n * 100).toFixed(1)}%`
}

function pctNum(value: any): number | undefined {
  const n = Number(value)
  return Number.isFinite(n) ? n * 100 : undefined
}

function price(value: any): string {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  return n.toFixed(2)
}

function money(value: any): string {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  return n.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}

function pnlClass(value: any): string {
  return Number(value) >= 0
    ? 'font-semibold text-red-600 dark:text-red-400'
    : 'font-semibold text-green-600 dark:text-green-500'
}

const ROUTE_NAMES: Record<string, string> = {
  institutional_mainwave: '机构主升浪',
  score120_core: '机构主升浪',
  strong_main: '强势突破来源',
  panic_repair: '恐慌修复',
  g2_gap_supplement: '量能续强补位',
  old_g3_route_v3: '旧G3原生来源',
  range_gap: '震荡弱势修复来源',
  down_panic: '恐慌出清修复来源',
}

function routeName(route: any): string {
  return ROUTE_NAMES[route] || route || '--'
}

const STRATEGY_NAMES: Record<string, string> = {
  range_weak_repair: '震荡弱势修复',
  panic_capitulation_repair: '恐慌出清修复',
  institutional_mainwave_score88: '机构主升Score88',
  institutional_score120_mainwave: '机构主升Score120（历史）',
  old_g3_strong_breakout: '强势突破',
  mainwave_breakout_offense: '主升/突破进攻',
  volume_runup_supplement: '量能续强补位',
  institutional_mainwave: '机构主升浪',
  institutional_score120_core: '机构主升浪Score120核心',
  institutional_institutional_mainwave: '机构主升浪',
  score120_core: '机构主升浪Score120核心',
  panic_repair: '恐慌修复',
  panic_repair_range: '震荡恐慌修复',
  panic_repair_downtrend: '下跌恐慌修复',
  g2_gap_supplement: '量能续强补位',
  g2_volume5_keep80_runup_sector_bonus: 'G2量能续强+板块加分',
  g2_volume5_keep80_runup: 'G2量能续强',
  g2_g2_gap_supplement: 'G2补位未细分',
  old_g3_old_g3_route_v3: '旧G3原生来源未细分',
  old_g3_strong_main: '强势突破',
  old_g3_strong_trend_breakout: '强势突破',
  old_g3_range_gap: '震荡弱势修复',
  old_g3_weak_rebound_repair: '震荡弱势修复',
  old_g3_range_box_bottom: '震荡弱势修复观察',
  old_g3_down_panic: '恐慌出清修复',
  old_g3_downtrend_panic_capitulation: '恐慌出清修复',
}

function routeStrategyName(row: Row): string {
  if (row?.trade_strategy_label) return row.trade_strategy_label
  if (row?.route_strategy_label) return row.route_strategy_label
  if (row?.mode === 'old_g3_route_v3' && row?.route) {
    return STRATEGY_NAMES[`old_g3_${row.route}`] || '旧G3原生来源未细分'
  }
  const key = row?.trade_strategy || row?.route_strategy || row?.route
  if (key === 'strong_main') return row?.mode === 'old_g3_route_v3' ? '强势突破' : '机构主升浪'
  return STRATEGY_NAMES[key] || routeName(row?.route)
}

function entryTimeText(row: Row): string {
  return row?.entry_ts || row?.entry_datetime || row?.buy_datetime || row?.planned_entry_ts || row?.entry_date || '--'
}

function exitTimeText(row: Row): string {
  if (row?.trade_status === 'open_shadow') return '持有中'
  return row?.exit_ts || row?.exit_datetime || row?.sell_datetime || row?.policy_exit_datetime || row?.exit_date || row?.policy_exit_date || '--'
}

function scoreScale(row: Row): string {
  const score = Number(row?.score)
  const scale = String(row?.score_scale || '')
  const route = String(row?.route || row?.mode || '')
  const mode = String(row?.mode || '')
  const status = String(row?.trade_status || '')
  if (!Number.isFinite(score)) return '--'
  if (scale === 'mainwave_0_140') return '主升0-140'
  if (scale === 'legacy_scheduler') return '历史调度'
  if (scale === 'route_internal') return route === 'panic_repair' ? '修复0-1' : '路线内分'
  if (status === 'open_shadow' && score >= 20) return '主升0-140'
  if (route === 'institutional_mainwave' && score >= 20) return '主升0-140'
  if (route === 'score120_core' || mode === 'institutional_mainwave') return '历史调度'
  if (route === 'panic_repair') return '修复0-1'
  if (route === 'range_gap' || route === 'down_panic' || route === 'old_g3_route_v3') return '结构0-1'
  return score >= 20 ? '主升0-140' : '路线内分'
}

function displayScore(row: Row): any {
  if (row?.route === 'institutional_mainwave') {
    return row.wave_style_score ?? row.selected_score ?? row.score ?? row.candidate_score
  }
  return row.score ?? row.candidate_score ?? row.selected_score ?? row.mode_score
}

function replayRowDate(row: Row, fallbackEntryDate?: any): any {
  return row?.entry_date || row?.signal_date || row?.decision_date || fallbackEntryDate
}

function isReplayTicket(row: Row): boolean {
  return Boolean(row?.ticket_key || row?.qualified_shadow_buy || row?.paper_trade_ready || row?.replay_decision === 'bought')
}

function replayRowKind(row: Row): string {
  if (isReplayTicket(row)) return '推荐买入'
  if (truthy(row?.router_eligible)) return '合格候选'
  return '阻断候选'
}

function replayRowKindTagType(row: Row): 'emerald' | 'blue' | 'amber' {
  if (isReplayTicket(row)) return 'emerald'
  if (truthy(row?.router_eligible)) return 'blue'
  return 'amber'
}

function replayRowKey(row: Row): string {
  return [
    row?.ticket_key || '',
    row?.candidate_key || '',
    row?.entry_date || '',
    row?.route || '',
    row?.code || '',
  ].filter(Boolean).join('|')
}

function dedupeReplayRows(rows: Row[]): Row[] {
  const seen = new Set<string>()
  const out: Row[] = []
  rows.forEach((row) => {
    const key = replayRowKey(row)
    if (!key || seen.has(key)) return
    seen.add(key)
    out.push(row)
  })
  return out
}

function gateTagType(item: Row): 'emerald' | 'red' | 'amber' {
  if (item?.ok) return 'emerald'
  if (item?.blocking) return 'red'
  return 'amber'
}

function gateText(item: Row): string {
  if (item?.ok) return '通过'
  if (item?.blocking) return '阻断'
  return '观察'
}

function naturalActionTagType(row: Row): 'red' | 'amber' | 'emerald' | 'zinc' {
  const action = String(row?.natural_action || '')
  const exitAction = String(row?.exit_shadow_action || '')
  if (action === 'skip') return 'red'
  if (exitAction === 'buy_point_review') return 'red'
  if (exitAction === 'early_exit_review') return 'amber'
  if (action === 'allow_reduced') return 'amber'
  if (action === 'allow') return 'emerald'
  return 'zinc'
}

const inputClass =
  'w-40 rounded-lg border border-zinc-200 bg-white px-2.5 py-1.5 text-sm text-zinc-900 dark:border-white/10 dark:bg-white/5 dark:text-zinc-100'
const tableScrollClass = 'max-h-[560px] overflow-auto'

function SummaryCard({ label, value, hint }: { label: string; value: any; hint?: string }) {
  return (
    <Card className="min-w-0 gap-1 p-2.5">
      <span className="block text-xs text-zinc-500 dark:text-zinc-400">{label}</span>
      <strong className="mt-0.5 block truncate text-lg font-semibold text-zinc-900 tabular-nums dark:text-zinc-100">{value}</strong>
      {hint ? <small className="block text-xs leading-relaxed text-zinc-500 dark:text-zinc-400">{hint}</small> : null}
    </Card>
  )
}

function EmptyRow({ colSpan, text }: { colSpan: number; text: string }) {
  return (
    <TableRow>
      <TableCell colSpan={colSpan} className="text-center text-zinc-500 dark:text-zinc-400">
        {text}
      </TableCell>
    </TableRow>
  )
}

const DATASET_OPTIONS: { label: string; value: string }[] = [
  { label: '旧历史参考', value: 'reference' },
  { label: 'Score88 全历史复现（研究）', value: 'score88_replay' },
]
const ROUTE_OPTIONS: { label: string; value: string }[] = [
  { label: '全部路由', value: 'all' },
  { label: '机构主升浪', value: 'institutional_mainwave' },
  { label: '恐慌修复', value: 'panic_repair' },
  { label: '旧G3原生来源', value: 'old_g3_route_v3' },
]
const WINDOW_OPTIONS: { label: string; value: string }[] = [
  { label: '全周期', value: 'all' },
  { label: '2024年9月前', value: 'pre_2024_09' },
  { label: '2024年9月后', value: 'post_2024_09' },
  { label: '2022弱市', value: 'weak_2022' },
  { label: '2024验证段', value: 'valid_2024' },
  { label: '2026年内盲测', value: 'blind_2026ytd' },
]
const SORT_BY_OPTIONS: { label: string; value: string }[] = [
  { label: '入场日', value: 'entry_date' },
  { label: '收益率', value: 'net_ret' },
  { label: '实现盈亏', value: 'realized_pnl' },
  { label: '得分', value: 'score' },
]

export default function Gen3StateAlphaTrades() {
  const toast = useToast()
  const [loading, setLoading] = useState(false)
  const [payload, setPayload] = useState<Row>({})
  const [currentPayload, setCurrentPayload] = useState<Row>({})
  const [replayDate, setReplayDate] = useState('')
  const [query, setQuery] = useState({
    dataset: 'reference',
    route: 'all',
    window: 'all',
    sort_by: 'entry_date',
    sort_order: 'desc',
    limit: 300,
  })

  const trades = useMemo(() => (Array.isArray(payload.historical_trades) ? payload.historical_trades : []), [payload])
  const metrics = useMemo(() => payload.metrics || {}, [payload])
  const routeMetrics = useMemo(() => (Array.isArray(payload.route_metrics) ? payload.route_metrics : []), [payload])
  const windowMetrics = useMemo(() => (Array.isArray(payload.window_metrics) ? payload.window_metrics : []), [payload])
  const marketStyleMetrics = useMemo(() => (Array.isArray(payload.market_style_metrics) ? payload.market_style_metrics : []), [payload])
  const assessment = useMemo(() => payload.replacement_assessment || {}, [payload])
  const score88Replay = useMemo(() => payload.score88_replay || {}, [payload])
  const historicalReference = useMemo(() => payload.historical_reference || {}, [payload])
  const replacementGates = useMemo(() => (Array.isArray(assessment.gates) ? assessment.gates : []), [assessment])
  const equityCurve = useMemo(() => (Array.isArray(payload.equity_curve) ? payload.equity_curve : []), [payload])
  const exposure = useMemo(() => payload.exposure_metrics || {}, [payload])
  const futureLeakAudit = useMemo(() => payload.future_leak_audit || {}, [payload])
  const naturalPolicyShadow = useMemo(() => payload.natural_policy_shadow || {}, [payload])
  const naturalSummary = useMemo(() => naturalPolicyShadow.summary || {}, [naturalPolicyShadow])
  const naturalContract = useMemo(() => naturalPolicyShadow.contract || {}, [naturalPolicyShadow])
  const naturalEntrySummary = useMemo(() => (
    Array.isArray(naturalPolicyShadow.entry_position_summary) ? naturalPolicyShadow.entry_position_summary : []
  ), [naturalPolicyShadow])
  const naturalExitSummary = useMemo(() => (
    Array.isArray(naturalPolicyShadow.exit_summary) ? naturalPolicyShadow.exit_summary : []
  ), [naturalPolicyShadow])
  const naturalPolicyStatusText = useMemo(() => {
    const status = naturalContract.status || naturalSummary.status || 'shadow_only'
    if (status === 'shadow_only') return '影子观察'
    return compactStatusWithZh(status)
  }, [naturalContract, naturalSummary])

  const currentSummary = useMemo(() => currentPayload.summary || {}, [currentPayload])
  const currentShadowTickets = useMemo(() => (
    Array.isArray(currentPayload.shadow_tickets) ? currentPayload.shadow_tickets : []
  ), [currentPayload])
  const currentShadowLedger = useMemo(() => (
    Array.isArray(currentPayload.shadow_ledger) ? currentPayload.shadow_ledger : []
  ), [currentPayload])
  const currentShadowRows = useMemo(() => dedupeReplayRows([
    ...currentShadowTickets,
    ...currentShadowLedger.map((row: Row) => ({
      ...row,
      trade_status: row.trade_status || 'open_shadow',
      source_type: row.source_type || 'runtime_shadow_ledger',
    })),
  ]), [currentShadowTickets, currentShadowLedger])
  const currentAfterhoursTickets = useMemo(() => (
    Array.isArray(currentPayload.afterhours_shadow_tickets) ? currentPayload.afterhours_shadow_tickets : []
  ), [currentPayload])
  const currentNextTradeTickets = useMemo(() => (
    Array.isArray(currentPayload.next_trade_buy_tickets) ? currentPayload.next_trade_buy_tickets : []
  ), [currentPayload])
  const currentAllCandidates = useMemo(() => (
    Array.isArray(currentPayload.all_source_candidates) ? currentPayload.all_source_candidates : []
  ), [currentPayload])
  const historicalReplayCandidates = useMemo(() => (
    Array.isArray(payload.historical_replay_candidates) ? payload.historical_replay_candidates : []
  ), [payload])

  const fusionText = useMemo(() => (
    historicalReference.message
    || assessment.verdict
    || 'G3 正在按最终版合同融合 G2 补位能力；观察期内保留深链验收，不再作为独立主策略展示。'
  ), [historicalReference, assessment])

  const replayDateOptions = useMemo(() => {
    const dates = new Set<string>()
    const fallback = currentSummary.entry_date
    trades.forEach((row: Row) => {
      if (row.entry_date) dates.add(String(row.entry_date))
    })
    historicalReplayCandidates.forEach((row: Row) => {
      const date = replayRowDate(row, fallback)
      if (date) dates.add(String(date).slice(0, 10))
    })
    currentAllCandidates.forEach((row: Row) => {
      const date = replayRowDate(row, fallback)
      if (date) dates.add(String(date).slice(0, 10))
    })
    ;[currentNextTradeTickets, currentAfterhoursTickets, currentShadowRows].forEach((rows) => {
      rows.forEach((row: Row) => {
        const date = replayRowDate(row, fallback)
        if (date) dates.add(String(date).slice(0, 10))
      })
    })
    return Array.from(dates).sort().reverse()
  }, [trades, historicalReplayCandidates, currentAllCandidates, currentNextTradeTickets, currentAfterhoursTickets, currentShadowRows, currentSummary])

  const activeReplayDate = replayDate || replayDateOptions[0] || ''

  const replayTrades = useMemo(() => {
    if (!activeReplayDate) return []
    return trades.filter((row: Row) => String(row.entry_date || '').slice(0, 10) === activeReplayDate)
  }, [trades, activeReplayDate])

  const currentReplayCandidates = useMemo(() => {
    if (!activeReplayDate) return []
    const fallback = currentSummary.entry_date
    return [...historicalReplayCandidates, ...currentAllCandidates].filter((row: Row) => {
      const date = replayRowDate(row, fallback)
      return String(date || '').slice(0, 10) === activeReplayDate
    })
  }, [historicalReplayCandidates, currentAllCandidates, activeReplayDate, currentSummary])

  const currentReplayTickets = useMemo(() => {
    if (!activeReplayDate) return []
    const fallback = currentSummary.entry_date
    return dedupeReplayRows(
      [...currentNextTradeTickets, ...currentAfterhoursTickets, ...currentShadowRows].filter(
        (row: Row) => String(replayRowDate(row, fallback) || '').slice(0, 10) === activeReplayDate,
      ),
    )
  }, [currentNextTradeTickets, currentAfterhoursTickets, currentShadowRows, activeReplayDate, currentSummary])

  const currentReplayRows = useMemo(() => dedupeReplayRows([...currentReplayTickets, ...currentReplayCandidates]), [currentReplayTickets, currentReplayCandidates])

  const replayEligibleCount = currentReplayRows.filter((row: Row) => truthy(row.router_eligible || row.qualified_shadow_buy)).length
  const replayBlockedCount = currentReplayRows.filter((row: Row) => !truthy(row.router_eligible || row.qualified_shadow_buy) || row.block_reason).length
  const replayTopBlockReason = useMemo(() => {
    const counts = new Map<string, number>()
    currentReplayRows.forEach((row: Row) => {
      const reason = String(row.block_reason || row.block_detail || '').trim()
      if (!reason) return
      counts.set(reason, (counts.get(reason) || 0) + 1)
    })
    const sorted = Array.from(counts.entries()).sort((a, b) => b[1] - a[1])
    return sorted[0]?.[0] || ''
  }, [currentReplayRows])
  const replayRecommendedNames = currentReplayTickets.map((item: Row) => item.name || item.code).filter(Boolean).join(' / ')
  const replayDecisionStatus = useMemo(() => {
    if (!currentReplayRows.length) return '无归档'
    if (!currentReplayTickets.length) return '无买入'
    const invalidTickets = currentReplayTickets.filter((row: Row) => !truthy(row.router_eligible || row.qualified_shadow_buy))
    return invalidTickets.length ? '需复核' : '正确'
  }, [currentReplayRows, currentReplayTickets])
  const replayDecisionAlertColor: 'zinc' | 'amber' | 'green' = !currentReplayRows.length
    ? 'zinc'
    : !currentReplayTickets.length ? 'amber' : replayDecisionStatus === '正确' ? 'green' : 'amber'
  const replayCandidateAuditText = useMemo(() => {
    if (!activeReplayDate) return '请选择复盘日期。'
    if (!currentReplayRows.length) return `${activeReplayDate} 暂无买入票据或候选归档。`
    if (!currentReplayTickets.length) {
      const reason = replayTopBlockReason ? `主阻断：${replayTopBlockReason}` : '未发现合格买入票。'
      return `${activeReplayDate} 没有推荐买入；候选 ${currentReplayRows.length} 条，${reason}`
    }
    const blockedText = replayBlockedCount ? `另有 ${replayBlockedCount} 条候选被阻断。` : '无阻断候选。'
    return `${activeReplayDate} 推荐买入：${replayRecommendedNames}；买入票来自合格票据，${blockedText}`
  }, [activeReplayDate, currentReplayRows, currentReplayTickets, replayTopBlockReason, replayBlockedCount, replayRecommendedNames])

  const curveOption = useMemo(() => {
    const rows = equityCurve
    const dates = rows.map((row: Row) => row.date)
    const returns = rows.map((row: Row) => {
      const n = Number(row.ret_from_start)
      return Number.isFinite(n) ? Number((n * 100).toFixed(2)) : null
    })
    const mtmReturns = rows.map((row: Row) => {
      const n = Number(row.ret_from_start_mtm)
      return Number.isFinite(n) ? Number((n * 100).toFixed(2)) : null
    })
    const drawdowns = rows.map((row: Row) => {
      const n = Number.isFinite(Number(row.drawdown_mtm)) ? Number(row.drawdown_mtm) : Number(row.drawdown)
      return Number.isFinite(n) ? Number((n * 100).toFixed(2)) : null
    })
    return {
      animation: false,
      grid: { left: 48, right: 22, top: 36, bottom: 42 },
      tooltip: {
        trigger: 'axis',
        valueFormatter: (value: any) => `${Number(value).toFixed(2)}%`,
      },
      legend: { top: 4, data: ['账面累计收益', 'MTM累计收益', 'MTM回撤'] },
      xAxis: { type: 'category', data: dates, boundaryGap: false },
      yAxis: { type: 'value', axisLabel: { formatter: '{value}%' } },
      series: [
        {
          name: '账面累计收益',
          type: 'line',
          data: returns,
          showSymbol: false,
          smooth: false,
          lineStyle: { width: 2, color: '#b42318' },
          itemStyle: { color: '#b42318' },
        },
        {
          name: 'MTM累计收益',
          type: 'line',
          data: mtmReturns,
          showSymbol: false,
          smooth: false,
          lineStyle: { width: 1.5, color: '#f79009' },
          itemStyle: { color: '#f79009' },
        },
        {
          name: 'MTM回撤',
          type: 'line',
          data: drawdowns,
          showSymbol: false,
          smooth: false,
          lineStyle: { width: 1.5, color: '#067647' },
          itemStyle: { color: '#067647' },
        },
      ],
    } as any
  }, [equityCurve])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [history, current] = await Promise.all([
        getGen3StateAlphaHistoricalTrades({ ...query }) as unknown as Row,
        getGen3StateAlphaCurrent({ limit: 30 }) as unknown as Row,
      ])
      setPayload(history)
      setCurrentPayload(current)
    } catch (error: any) {
      toast.info(error?.message || '读取 G3 历史成交失败')
    } finally {
      setLoading(false)
    }
  }, [query, toast])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    if (!replayDate && replayDateOptions.length) {
      setReplayDate(replayDateOptions[0])
    }
  }, [replayDate, replayDateOptions])

  return (
    <div className="flex flex-col gap-3.5 text-zinc-900 dark:text-zinc-100">
      <PageHeader
        title="历史成交复盘"
        subtitle="G3 正式五策略 · 上半部分看收益曲线、窗口表现和成交明细；下半部分按日期复盘候选池、选中票、跳过原因和当日决策。"
        actions={
          <Button color="blue" onClick={load} disabled={loading}>{loading ? '加载中…' : '刷新'}</Button>
        }
      />

      <Alert color="amber">{fusionText}</Alert>

      <Card>
        <SectionTitle
          title="Score88 独立历史复现"
          actions={
            <Badge color={score88Replay.available ? 'blue' : 'amber'}>
              {score88Replay.available ? '研究级 OHLC 代理' : '工件不可用'}
            </Badge>
          }
        />
        <p className="mb-3 text-sm text-zinc-500 dark:text-zinc-400">
          {score88Replay.message || '尚未加载 Score88 复现工件。'}
        </p>
        {score88Replay.available ? (
          <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2 lg:grid-cols-4">
            <SummaryCard label="复现交易" value={fmt(score88Replay.metrics?.trade_count, 0)} hint="两槽成交" />
            <SummaryCard label="胜率" value={pct(score88Replay.metrics?.win_rate)} hint="非实盘胜率" />
            <SummaryCard label="平均收益" value={pct(score88Replay.metrics?.avg_trade_return)} hint="OHLC 代理" />
            <SummaryCard label="盈亏比" value={fmt(score88Replay.metrics?.payoff_ratio, 2)} hint="研究口径" />
          </div>
        ) : null}
        {score88Replay.audit?.windows?.blind_2026ytd ? (
          <Alert color="amber" className="mt-3">
            {`2026 盲测：${fmt(score88Replay.audit.windows.blind_2026ytd.trade_count, 0)} 笔，胜率 ${pct(score88Replay.audit.windows.blind_2026ytd.win_rate)}，平均收益 ${pct(score88Replay.audit.windows.blind_2026ytd.avg_trade_return)}，盈亏比 ${fmt(score88Replay.audit.windows.blind_2026ytd.payoff_ratio, 2)}；该窗口未达到推广证据标准。`}
          </Alert>
        ) : null}
      </Card>

      <Card>
        <SectionTitle
          title="当前影子持有"
          actions={
            <Badge color={currentShadowRows.length ? 'amber' : 'zinc'}>
              {currentSummary.entry_date || '--'} / {currentShadowRows.length ? '有影子持有' : '暂无影子持有'}
            </Badge>
          }
        />
        <p className="mb-3 text-sm text-zinc-500 dark:text-zinc-400">
          这里合并最新影子票和影子台账；下方成交明细是历史回测闭合成交。
        </p>
        <Table dense className="[&_td]:whitespace-nowrap">
          <TableHead>
            <TableRow>
              <TableHeader>入场日</TableHeader>
              <TableHeader>代码</TableHeader>
              <TableHeader>名称</TableHeader>
              <TableHeader>路由</TableHeader>
              <TableHeader>计划时间</TableHeader>
              <TableHeader className="text-right">仓位</TableHeader>
              <TableHeader className="text-right">参考价</TableHeader>
              <TableHeader>状态</TableHeader>
              <TableHeader>来源</TableHeader>
              <TableHeader>阻断/说明</TableHeader>
            </TableRow>
          </TableHead>
          <TableBody>
            {currentShadowRows.length === 0 ? <EmptyRow colSpan={10} text="暂无当前影子持有" /> : currentShadowRows.map((row: Row, i: number) => (
              <TableRow key={`${row.code}-${row.entry_date}-${i}`}>
                <TableCell className="tabular-nums">{row.entry_date}</TableCell>
                <TableCell>{row.code}</TableCell>
                <TableCell>{row.name}</TableCell>
                <TableCell>{row.route_label || routeName(row.route)}</TableCell>
                <TableCell className="max-w-40 truncate">{row.planned_entry_ts}</TableCell>
                <TableCell className="text-right tabular-nums">{pct(row.position_pct)}</TableCell>
                <TableCell className="text-right tabular-nums">{price(row.reference_close)}</TableCell>
                <TableCell className="max-w-48 truncate">{compactStatusWithZh(row.shadow_status)}</TableCell>
                <TableCell>{row.source_type === 'runtime_shadow_ledger' ? '影子台账' : '最新票据'}</TableCell>
                <TableCell className="max-w-56 truncate">{row.block_reason}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Card>

      <Card>
        <div className="flex flex-wrap items-end gap-4">
          <label className="flex flex-col gap-1 text-xs text-zinc-500 dark:text-zinc-400">
            数据集
            <select className={inputClass} value={query.dataset} onChange={(e) => setQuery({ ...query, dataset: e.target.value })}>
              {DATASET_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-zinc-500 dark:text-zinc-400">
            路由
            <select className={inputClass} value={query.route} onChange={(e) => setQuery({ ...query, route: e.target.value })}>
              {ROUTE_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-zinc-500 dark:text-zinc-400">
            窗口
            <select className={inputClass} value={query.window} onChange={(e) => setQuery({ ...query, window: e.target.value })}>
              {WINDOW_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-zinc-500 dark:text-zinc-400">
            排序
            <select className={inputClass} value={query.sort_by} onChange={(e) => setQuery({ ...query, sort_by: e.target.value })}>
              {SORT_BY_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </label>
          <div className="flex flex-col gap-1 text-xs text-zinc-500 dark:text-zinc-400">
            方向
            <div className="flex gap-1.5">
              {query.sort_order === 'desc' ? (
                <Button color="dark/zinc">降序</Button>
              ) : (
                <Button plain onClick={() => setQuery({ ...query, sort_order: 'desc' })}>降序</Button>
              )}
              {query.sort_order === 'asc' ? (
                <Button color="dark/zinc">升序</Button>
              ) : (
                <Button plain onClick={() => setQuery({ ...query, sort_order: 'asc' })}>升序</Button>
              )}
            </div>
          </div>
          <label className="flex flex-col gap-1 text-xs text-zinc-500 dark:text-zinc-400">
            行数
            <input
              type="number"
              min={50}
              max={2000}
              step={50}
              className={`${inputClass} w-28 tabular-nums`}
              value={query.limit}
              onChange={(e) => setQuery({ ...query, limit: Number(e.target.value) || 50 })}
            />
          </label>
        </div>
      </Card>

      <section className="grid grid-cols-1 gap-2.5 md:grid-cols-3 min-[1321px]:grid-cols-6">
        <StatCard label="交易数" value={fmt(metrics.trade_count, 0)} hint={`盈利 ${fmt(metrics.winning_trades, 0)} / 亏损 ${fmt(metrics.losing_trades, 0)}`} />
        <StatCard label="胜率" value={pct(metrics.win_rate)} hint={`均笔 ${pct(metrics.avg_trade_return)}`} />
        <StatCard label="中位收益" value={pct(metrics.median_trade_return)} hint={`最好 ${pct(metrics.best_trade)}`} />
        <StatCard label="最差单笔" value={pct(metrics.worst_trade)} tone="down" hint="单笔大亏仍需继续压缩" />
        <StatCard
          label="实现盈亏"
          value={money(metrics.sum_realized_pnl)}
          tone={Number(metrics.sum_realized_pnl) >= 0 ? 'up' : 'down'}
          hint="按回测资金口径"
        />
        <StatCard
          label="G2退场"
          value={assessment.g2_can_exit_now ? '可以' : '暂不'}
          tone={assessment.g2_can_exit_now ? 'info' : 'warning'}
          hint="先降权观察，不直接删除"
        />
      </section>

      <Card className="flex flex-col gap-3">
        <SectionTitle title="自然交易纪律" actions={<Badge color="amber">{naturalPolicyStatusText}</Badge>} />
        <p className="-mt-1 text-sm text-zinc-500 dark:text-zinc-400">
          这是一层影子合同，只用于观察交易行为是否自然、可解释、可执行，不改变正式买卖决策。
        </p>
        <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2 lg:grid-cols-4">
          <SummaryCard label="N1 同票跳过" value={fmt(naturalSummary.skip_count, 0)} hint="避免隐式加仓伪装成独立二槽" />
          <SummaryCard label="N3 补位降仓" value={fmt(naturalSummary.reduced_position_count, 0)} hint="G2 空档补位回到补位身份" />
          <SummaryCard label="N2 退出复核" value={fmt(naturalSummary.early_exit_review_count, 0)} hint="被动到期仓位需要继续持有证据" />
          <SummaryCard label="N5 买点体检" value={fmt(naturalSummary.buy_point_review_count, 0)} hint="开仓后三日快速失败进入复盘池" />
        </div>
        <div className="grid grid-cols-1 gap-2.5 lg:grid-cols-2">
          <div className="overflow-x-auto">
            <Table dense>
              <TableHead>
                <TableRow>
                  <TableHeader>入场动作</TableHeader>
                  <TableHeader className="text-right">交易数</TableHeader>
                  <TableHeader className="text-right">胜率</TableHeader>
                  <TableHeader className="text-right">均笔</TableHeader>
                  <TableHeader className="text-right">原始盈亏</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {naturalEntrySummary.length === 0 ? <EmptyRow colSpan={5} text="暂无入场纪律汇总" /> : naturalEntrySummary.map((row: Row, i: number) => (
                  <TableRow key={i}>
                    <TableCell>{row.natural_action_label}</TableCell>
                    <TableCell className="text-right tabular-nums">{fmt(row.trade_count, 0)}</TableCell>
                    <TableCell className="text-right tabular-nums">{pct(row.win_rate)}</TableCell>
                    <TableCell className="text-right tabular-nums">{pct(row.avg_ret)}</TableCell>
                    <TableCell className="text-right tabular-nums">{money(row.original_pnl)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          <div className="overflow-x-auto">
            <Table dense>
              <TableHead>
                <TableRow>
                  <TableHeader>卖出复核</TableHeader>
                  <TableHeader className="text-right">交易数</TableHeader>
                  <TableHeader className="text-right">胜率</TableHeader>
                  <TableHeader className="text-right">均笔</TableHeader>
                  <TableHeader className="text-right">原始盈亏</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {naturalExitSummary.length === 0 ? <EmptyRow colSpan={5} text="暂无卖出复核汇总" /> : naturalExitSummary.map((row: Row, i: number) => (
                  <TableRow key={i}>
                    <TableCell>{row.exit_shadow_label}</TableCell>
                    <TableCell className="text-right tabular-nums">{fmt(row.trade_count, 0)}</TableCell>
                    <TableCell className="text-right tabular-nums">{pct(row.win_rate)}</TableCell>
                    <TableCell className="text-right tabular-nums">{pct(row.avg_ret)}</TableCell>
                    <TableCell className="text-right tabular-nums">{money(row.original_pnl)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </div>
      </Card>

      <section className="grid grid-cols-1 gap-3.5 lg:grid-cols-2">
        <Card>
          <SectionTitle title="收益曲线" />
          <p className="mb-3 text-sm text-zinc-500 dark:text-zinc-400">
            按历史回测权益、累计收益和回撤展示，方便观察收益来源是否集中在单一行情窗口。
          </p>
          <EChart option={curveOption} height={320} />
        </Card>

        <Card>
          <SectionTitle
            title="未来函数与仓位审计"
            actions={
              <Badge color={futureLeakAudit.has_high_risk_issue ? 'red' : 'emerald'}>
                {futureLeakAudit.has_high_risk_issue ? '需修复' : '通过'}
              </Badge>
            }
          />
          <p className="mb-3 text-sm text-zinc-500 dark:text-zinc-400">检查确认时间、上下文日期、候选排序和交易时段实际仓位。</p>
          {futureLeakAudit.has_high_risk_issue ? (
            <Alert color="red" className="mb-3">发现高风险未来函数：历史候选排序疑似使用最终收益 net_ret。</Alert>
          ) : null}
          <div className="mb-3 grid grid-cols-1 gap-2.5 sm:grid-cols-3">
            <SummaryCard label="交易日平均仓位" value={pct(exposure.avg_exposure_active_days)} />
            <SummaryCard label="全周期平均仓位" value={pct(exposure.avg_exposure_all_days)} />
            <SummaryCard label="持仓日占比" value={pct(exposure.active_day_ratio)} />
            <SummaryCard label="平均持仓数" value={fmt(exposure.avg_open_positions_active_days, 2)} />
            <SummaryCard label="最大持仓数" value={fmt(exposure.max_open_positions, 0)} />
            <SummaryCard label="单槽仓位" value={pct(exposure.median_slot_pct)} />
          </div>
          <p className="mb-3 rounded-lg bg-zinc-50 px-3 py-2.5 text-sm leading-relaxed text-zinc-700 dark:bg-white/5 dark:text-zinc-300">
            {exposure.comment || '--'}
          </p>
          <div className="flex max-h-40 flex-col gap-2 overflow-auto">
            {(futureLeakAudit.issues || []).map((item: Row) => (
              <div key={item.item} className="border-l-[3px] border-l-red-600 bg-zinc-50 px-2.5 py-2 dark:bg-white/5">
                <strong className="mb-0.5 block text-xs text-zinc-900 dark:text-zinc-100">{item.item}</strong>
                <span className="block text-xs text-zinc-500 dark:text-zinc-400">{item.impact}</span>
              </div>
            ))}
            {(futureLeakAudit.passes || []).map((item: Row) => (
              <div key={item.item} className="border-l-[3px] border-l-emerald-500 bg-zinc-50 px-2.5 py-2 dark:bg-white/5">
                <strong className="mb-0.5 block text-xs text-zinc-900 dark:text-zinc-100">{item.item}</strong>
                <span className="block text-xs text-zinc-500 dark:text-zinc-400">{item.evidence}</span>
              </div>
            ))}
          </div>
        </Card>
      </section>

      <section className="grid grid-cols-1 gap-3.5 lg:grid-cols-2">
        <Card>
          <SectionTitle title="路由成交归因" />
          <p className="mb-3 text-sm text-zinc-500 dark:text-zinc-400">按正式交易策略归并相似成交，来源路由保留追溯。</p>
          <div className="overflow-x-auto">
            <Table dense>
              <TableHead>
                <TableRow>
                  <TableHeader>交易策略</TableHeader>
                  <TableHeader className="text-right">交易数</TableHeader>
                  <TableHeader className="text-right">胜率</TableHeader>
                  <TableHeader className="text-right">均笔</TableHeader>
                  <TableHeader className="text-right">最差</TableHeader>
                  <TableHeader className="text-right">盈亏</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {routeMetrics.length === 0 ? <EmptyRow colSpan={6} text="暂无路由归因" /> : routeMetrics.map((row: Row, i: number) => (
                  <TableRow key={i}>
                    <TableCell>
                      <div className="flex flex-col gap-0.5 leading-tight">
                        <strong className="text-zinc-900 dark:text-zinc-100">{row.route_label || routeStrategyName(row)}</strong>
                        {row.strategy_summary ? (
                          <small className="whitespace-nowrap text-[11px] text-zinc-500 dark:text-zinc-400">含：{row.strategy_summary}</small>
                        ) : row.parent_route_label || row.route_parent_label ? (
                          <small className="whitespace-nowrap text-[11px] text-zinc-500 dark:text-zinc-400">来源：{row.parent_route_label || row.route_parent_label}</small>
                        ) : null}
                      </div>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{fmt(row.trade_count, 0)}</TableCell>
                    <TableCell className="text-right tabular-nums">{pct(row.win_rate)}</TableCell>
                    <TableCell className="text-right tabular-nums">{pct(row.avg_trade_return)}</TableCell>
                    <TableCell className="text-right tabular-nums">{pct(row.worst_trade)}</TableCell>
                    <TableCell className="text-right tabular-nums">{money(row.sum_realized_pnl)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </Card>

        <Card>
          <SectionTitle title="窗口表现" />
          <p className="mb-3 text-sm text-zinc-500 dark:text-zinc-400">重点看 2024年9月前后差异，防止单一行情拟合。</p>
          <div className="overflow-x-auto">
            <Table dense>
              <TableHead>
                <TableRow>
                  <TableHeader>窗口</TableHeader>
                  <TableHeader className="text-right">交易数</TableHeader>
                  <TableHeader className="text-right">收益</TableHeader>
                  <TableHeader className="text-right">回撤</TableHeader>
                  <TableHeader className="text-right">胜率</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {windowMetrics.length === 0 ? <EmptyRow colSpan={5} text="暂无窗口表现" /> : windowMetrics.map((row: Row, i: number) => (
                  <TableRow key={i}>
                    <TableCell>{row.window}</TableCell>
                    <TableCell className="text-right tabular-nums">{fmt(row.trade_count, 0)}</TableCell>
                    <TableCell className="text-right tabular-nums">{pct(row.return)}</TableCell>
                    <TableCell className="text-right tabular-nums">{pct(row.max_drawdown)}</TableCell>
                    <TableCell className="text-right tabular-nums">{pct(row.win_rate)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </Card>
      </section>

      <Card>
        <SectionTitle title="成交明细" />
        <p className="mb-3 text-sm text-zinc-500 dark:text-zinc-400">每一笔都保留入场、退出、路由、市场状态、收益和正式交易闸门。</p>
        <div className={`${tableScrollClass} overflow-x-auto`}>
          <Table dense className="[&_td]:whitespace-nowrap">
            <TableHead>
              <TableRow>
                <TableHeader>入场日</TableHeader>
                <TableHeader>退出日</TableHeader>
                <TableHeader>买入时间</TableHeader>
                <TableHeader>卖出时间</TableHeader>
                <TableHeader>状态</TableHeader>
                <TableHeader>代码</TableHeader>
                <TableHeader>名称</TableHeader>
                <TableHeader>路由</TableHeader>
                <TableHeader>策略</TableHeader>
                <TableHeader className="text-right">收益</TableHeader>
                <TableHeader className="text-right">盈亏</TableHeader>
                <TableHeader className="text-right">入场价</TableHeader>
                <TableHeader className="text-right">仓位</TableHeader>
                <TableHeader className="text-right">得分</TableHeader>
                <TableHeader>市场风格</TableHeader>
                <TableHeader className="text-right">指数20日</TableHeader>
                <TableHeader className="text-right">上涨率</TableHeader>
                <TableHeader>确认时间</TableHeader>
                <TableHeader>Live</TableHeader>
                <TableHeader>正式</TableHeader>
                <TableHeader>自然纪律</TableHeader>
                <TableHeader>阻断/说明</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {trades.length === 0 ? <EmptyRow colSpan={22} text="暂无历史成交" /> : trades.map((row: Row, i: number) => (
                <TableRow key={`${row.code}-${row.entry_date}-${i}`}>
                  <TableCell className="tabular-nums">{row.entry_date}</TableCell>
                  <TableCell>{row.trade_status === 'open_shadow' ? '持有中' : row.policy_exit_date}</TableCell>
                  <TableCell className="max-w-40 truncate">{entryTimeText(row)}</TableCell>
                  <TableCell className="max-w-40 truncate">{exitTimeText(row)}</TableCell>
                  <TableCell>
                    <Badge color={row.trade_status === 'open_shadow' ? 'amber' : 'emerald'}>
                      {row.trade_status === 'open_shadow' ? '影子持有' : '已退出'}
                    </Badge>
                  </TableCell>
                  <TableCell>{row.code}</TableCell>
                  <TableCell>{row.name}</TableCell>
                  <TableCell>{row.route_label || routeName(row.route)}</TableCell>
                  <TableCell>{row.route_strategy_label || routeStrategyName(row)}</TableCell>
                  <TableCell className="text-right"><ChangeValue value={pctNum(row.net_ret)} digits={1} suffix="%" /></TableCell>
                  <TableCell className={`text-right tabular-nums ${pnlClass(row.realized_pnl)}`}>{money(row.realized_pnl)}</TableCell>
                  <TableCell className="text-right tabular-nums">{price(row.entry_price)}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.slot_pct)}</TableCell>
                  <TableCell className="text-right">
                    <div className="flex flex-col items-end gap-0.5 leading-tight">
                      <strong className="tabular-nums text-zinc-900 dark:text-zinc-100">{fmt(row.score, 2)}</strong>
                      <small className="whitespace-nowrap text-[11px] text-zinc-500 dark:text-zinc-400">{scoreScale(row)}</small>
                    </div>
                  </TableCell>
                  <TableCell>{row.market_style}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.index_mom20)}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.up_rate)}</TableCell>
                  <TableCell className="max-w-40 truncate">{row.confirm_datetime}</TableCell>
                  <TableCell><Badge color={truthy(row.live_ready) ? 'emerald' : 'zinc'}>{truthy(row.live_ready) ? '是' : '否'}</Badge></TableCell>
                  <TableCell><Badge color={truthy(row.formal_buy_signal) ? 'red' : 'zinc'}>{truthy(row.formal_buy_signal) ? '开' : '关'}</Badge></TableCell>
                  <TableCell>
                    <div className="flex flex-col gap-1 leading-tight">
                      <Badge color={naturalActionTagType(row)}>{row.natural_action_label || '未标记'}</Badge>
                      {row.exit_shadow_label && row.exit_shadow_action !== 'none' ? (
                        <small className="whitespace-nowrap text-[11px] text-zinc-500 dark:text-zinc-400">{row.exit_shadow_label}</small>
                      ) : null}
                    </div>
                  </TableCell>
                  <TableCell className="max-w-56 truncate">{row.block_reason}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </Card>

      <Card className="flex flex-col gap-3">
        <SectionTitle
          title="逐日买入与候选复盘"
          actions={
            <select className={inputClass} value={replayDate} onChange={(e) => setReplayDate(e.target.value)}>
              {replayDateOptions.length === 0 ? <option value="">选择复盘日期</option> : null}
              {replayDateOptions.map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
          }
        />
        <p className="-mt-1 text-sm text-zinc-500 dark:text-zinc-400">
          核心校验每天应该买入哪些票、候选池是否完整、买入票是否来自合格候选，以及阻断票为什么没有进入买入。
        </p>

        {!currentReplayRows.length ? <Alert color="zinc">该日暂无候选归档；已先展示历史成交切片。</Alert> : null}

        <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2 lg:grid-cols-4">
          <SummaryCard label="复盘日期" value={activeReplayDate || '--'} hint={`历史成交 ${replayTrades.length} 笔；未发生交易日允许为空`} />
          <SummaryCard label="推荐买入" value={currentReplayTickets.length} hint={replayRecommendedNames || '--'} />
          <SummaryCard label="候选池" value={currentReplayRows.length} hint={`合格 ${replayEligibleCount} / 阻断 ${replayBlockedCount}`} />
          <SummaryCard label="校验结论" value={replayDecisionStatus} hint={replayCandidateAuditText} />
        </div>

        {currentReplayRows.length ? <Alert color={replayDecisionAlertColor}>{replayCandidateAuditText}</Alert> : null}

        <div className="overflow-x-auto">
          <Table dense className="[&_td]:whitespace-nowrap">
            <TableHead>
              <TableRow>
                <TableHeader>入场日</TableHeader>
                <TableHeader>代码</TableHeader>
                <TableHeader>名称</TableHeader>
                <TableHeader>路由</TableHeader>
                <TableHeader className="text-right">收益</TableHeader>
                <TableHeader className="text-right">仓位</TableHeader>
                <TableHeader>自然纪律</TableHeader>
                <TableHeader>说明</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {replayTrades.length === 0 ? <EmptyRow colSpan={8} text="该日暂无历史成交" /> : replayTrades.map((row: Row, i: number) => (
                <TableRow key={`${row.code}-${i}`}>
                  <TableCell className="tabular-nums">{row.entry_date}</TableCell>
                  <TableCell>{row.code}</TableCell>
                  <TableCell>{row.name}</TableCell>
                  <TableCell>{row.route_label || routeName(row.route)}</TableCell>
                  <TableCell className="text-right"><ChangeValue value={pctNum(row.net_ret)} digits={1} suffix="%" /></TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.slot_pct)}</TableCell>
                  <TableCell>
                    <Badge color={naturalActionTagType(row)}>{row.natural_action_label || row.exit_shadow_label || '未标记'}</Badge>
                  </TableCell>
                  <TableCell className="max-w-56 truncate">{row.block_reason}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>

        <div className={`${tableScrollClass} overflow-x-auto`}>
          <Table dense className="[&_td]:whitespace-nowrap">
            <TableHead>
              <TableRow>
                <TableHeader>类型</TableHeader>
                <TableHeader>代码</TableHeader>
                <TableHeader>名称</TableHeader>
                <TableHeader>路由</TableHeader>
                <TableHeader>准入</TableHeader>
                <TableHeader>30m</TableHeader>
                <TableHeader className="text-right">得分</TableHeader>
                <TableHeader>确认时间</TableHeader>
                <TableHeader>影子状态</TableHeader>
                <TableHeader>阻断/说明</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {currentReplayRows.length === 0 ? <EmptyRow colSpan={10} text="暂无该日全量候选" /> : currentReplayRows.map((row: Row, i: number) => (
                <TableRow key={`${row.code}-${i}`}>
                  <TableCell><Badge color={replayRowKindTagType(row)}>{replayRowKind(row)}</Badge></TableCell>
                  <TableCell>{row.code}</TableCell>
                  <TableCell>{row.name}</TableCell>
                  <TableCell>{row.route_label || routeName(row.route)}</TableCell>
                  <TableCell>
                    <Badge color={truthy(row.router_eligible || row.qualified_shadow_buy) ? 'emerald' : 'amber'}>
                      {truthy(row.router_eligible || row.qualified_shadow_buy) ? '通过' : '阻断'}
                    </Badge>
                  </TableCell>
                  <TableCell><Badge color={truthy(row.m30_confirmed) ? 'emerald' : 'zinc'}>{compactStatusWithZh(row.m30_status)}</Badge></TableCell>
                  <TableCell className="text-right tabular-nums">{fmt(displayScore(row), 2)}</TableCell>
                  <TableCell className="max-w-40 truncate">{row.confirm_datetime}</TableCell>
                  <TableCell className="max-w-48 truncate">{compactStatusWithZh(row.shadow_status)}</TableCell>
                  <TableCell className="max-w-60 truncate">{row.block_reason}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </Card>

      <section className="grid grid-cols-1 gap-3.5 lg:grid-cols-2">
        <Card>
          <SectionTitle title="市场风格归因" />
          <p className="mb-3 text-sm text-zinc-500 dark:text-zinc-400">检查哪些市场环境贡献收益，哪些环境拖累。</p>
          <div className="overflow-x-auto">
            <Table dense>
              <TableHead>
                <TableRow>
                  <TableHeader>市场风格</TableHeader>
                  <TableHeader className="text-right">交易数</TableHeader>
                  <TableHeader className="text-right">胜率</TableHeader>
                  <TableHeader className="text-right">均笔</TableHeader>
                  <TableHeader className="text-right">最差</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {marketStyleMetrics.length === 0 ? <EmptyRow colSpan={5} text="暂无风格归因" /> : marketStyleMetrics.map((row: Row, i: number) => (
                  <TableRow key={i}>
                    <TableCell>{row.market_style}</TableCell>
                    <TableCell className="text-right tabular-nums">{fmt(row.trade_count, 0)}</TableCell>
                    <TableCell className="text-right tabular-nums">{pct(row.win_rate)}</TableCell>
                    <TableCell className="text-right tabular-nums">{pct(row.avg_trade_return)}</TableCell>
                    <TableCell className="text-right tabular-nums">{pct(row.worst_trade)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </Card>

        <Card>
          <SectionTitle title="正式合同验收" />
          <p className="mb-3 text-sm text-zinc-500 dark:text-zinc-400">
            不是把 G2 当成独立主策略保留，而是确认补位、提醒、台账和稳定运行能力已经归入 G3。
          </p>
          <div className="flex flex-col gap-2.5">
            {replacementGates.map((item: Row) => (
              <div key={item.name} className="grid grid-cols-[72px_minmax(0,1fr)] items-start gap-2.5 border-b border-zinc-100 pb-2.5 last:border-b-0 last:pb-0 dark:border-white/10">
                <Badge color={gateTagType(item)}>{gateText(item)}</Badge>
                <div>
                  <strong className="mb-0.5 block text-sm text-zinc-900 dark:text-zinc-100">{item.name}</strong>
                  <span className="text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">{item.detail}</span>
                </div>
              </div>
            ))}
            {replacementGates.length === 0 ? (
              <EmptyState text="暂无替代审计门槛" />
            ) : null}
            {(assessment.minimum_exit_gates || []).map((item: string) => (
              <div key={item} className="flex items-start gap-2.5">
                <Badge color="amber">待确认</Badge>
                <span className="text-sm text-zinc-700 dark:text-zinc-300">{item}</span>
              </div>
            ))}
          </div>
        </Card>
      </section>
    </div>
  )
}
