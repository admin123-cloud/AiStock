import { useCallback, useEffect, useMemo, useState } from 'react'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/catalyst/table'
import { Card, EmptyState, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import { getGen3StateAlphaCurrent, getGen3StateAlphaHistoricalTrades } from '@/api/trading'

/* ---------- 类型（宽松，与后端负载对齐） ---------- */

interface Row {
  route?: string; mode?: string
  route_label?: string; route_strategy_label?: string
  trade_strategy?: string; trade_strategy_label?: string
  route_strategy?: string; strategy?: string; route_parent?: string; source_route?: string
  strategy_summary?: string
  code?: string; name?: string
  router_eligible?: boolean | string | number
  qualified_shadow_buy?: boolean | string | number
  block_reason?: string; skip_reason?: string
  score?: number | string; candidate_score?: number | string; mode_pick_score?: number | string
  v4_score?: number | string; selected_score?: number | string; wave_style_score?: number | string
  [key: string]: unknown
}

interface RouteDiag extends Row {
  rows?: number; source_rows?: number
  eligible_rows?: number; final_candidates?: number
  blocked_rows?: number
  route_health_ok?: boolean | string; ok?: boolean | string
  route_health_count?: number; sample_count?: number
  route_health_avg_ret?: number; avg_trade_return?: number
  route_health_worst_ret?: number; worst_trade?: number
  top_block_reason?: string
}

interface CurrentPayload {
  summary?: {
    selected_route?: string
    route_reason?: string
  }
  route_diagnostics?: RouteDiag[]
  all_source_candidates?: Row[]
  pipeline?: { key?: string; title?: string; detail?: string; status?: string; count?: number | null }[]
}

interface HistoryPayload {
  metrics?: {
    worst_trade?: number | string
    avg_trade_return?: number | string
  }
  route_metrics?: RouteDiag[]
  market_style_metrics?: RouteDiag[]
  strategy_reduction_recovery?: {
    ok?: boolean
    headline?: RecoveryHeadline
    formal_profile?: { total_return?: number | string; trade_count?: number | string }
    practical_fusion_contract?: {
      summary?: {
        recommended_total_return?: number | string
        recommended_scenario?: string
        recommended_trade_count?: number | string
        recommended_retain_rate?: number | string
        min_accept_retain_rate?: number | string
      }
    }
    return_gap_audit?: { summary?: { return_gap?: number | string; status?: string } }
    fusion_guardrails?: {
      summary?: { ok?: boolean; hard_fail_count?: number | string; warn_count?: number | string }
      checks?: { key?: string; title?: string; status?: string; evidence?: string; action?: string }[]
    }
    strategy_compare?: RouteDiag[]
    scenario_matrix?: RecoveryScenario[]
    m30_integrity?: {
      summary?: { m30_data_ok_rate?: number | string; m30_pre_signal_ok_rate?: number | string; m30_confirmed_proxy_rate?: number | string }
      by_strategy?: RouteDiag[]
      native_semantics?: {
        summary?: { native_m30_confirmed_rate?: number | string; proxy_kill_count?: number | string; scenario_proxy_m30_total_return?: number | string }
        scenario_matrix?: RecoveryScenario[]
      }
    }
  }
}

interface RecoveryHeadline {
  total_return?: number | string
  max_drawdown?: number | string
  closed_trades?: number | string
  candidate_rows?: number | string
  selected_rows?: number | string
  win_rate?: number | string
  avg_ret?: number | string
}

interface RecoveryScenario {
  scenario?: string
  total_return?: number | string
  max_drawdown?: number | string
  closed?: number | string
  win_rate?: number | string
  candidates?: number | string
  candidate_count?: number | string
  m30_data_ok_rate?: number | string
  m30_pre_signal_ok_rate?: number | string
  m30_confirmed_proxy_rate?: number | string
  avg_m30_day_close_pos?: number | string
  trade_strategy?: string
  trade_strategy_label?: string
}

/* ---------- 工具 ---------- */

const truthy = (value: unknown): boolean => {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0
  if (typeof value === 'string') return ['1', 'true', 'yes', 'ok', 'pass'].includes(value.trim().toLowerCase())
  return false
}

const ROUTE_NAMES: Record<string, string> = {
  institutional_mainwave: '机构主升',
  panic_repair: '恐慌修复',
  g2_gap_supplement: '量能续强补位',
  old_g3_route_v3: '旧G3原生来源',
  old_g3_route: '旧G3原生来源',
  range_gap: '震荡弱势修复来源',
  down_panic: '恐慌出清修复来源',
  strong_main: '强势突破来源',
}
const routeName = (route?: string | null): string => ROUTE_NAMES[route || ''] || route || '--'

const TRADE_STRATEGY_NAMES: Record<string, string> = {
  range_weak_repair: '震荡弱势修复',
  panic_capitulation_repair: '恐慌出清修复',
  institutional_mainwave_score88: '机构主升Score88',
  institutional_score120_mainwave: '机构主升Score120（历史）',
  old_g3_strong_breakout: '强势突破',
  mainwave_breakout_offense: '主升/突破进攻',
  volume_runup_supplement: '量能续强补位',
}
const tradeStrategyName = (row?: Row | RouteDiag | RecoveryScenario): string =>
  (row as Row)?.trade_strategy_label || TRADE_STRATEGY_NAMES[(row as Row)?.trade_strategy || ''] ||
  (row as Row)?.route_strategy_label || routeName((row as Row)?.route || (row as Row)?.mode)

const fmt = (value: unknown, digits = 2): string => {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(digits) : '--'
}
const pct = (value: unknown): string => {
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : '--'
}
const money = (value: unknown): string => {
  const n = Number(value)
  return Number.isFinite(n) ? n.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) : '--'
}

const guardrailTagColor = (status?: string) =>
  status === 'pass' ? 'emerald' : status === 'fail' ? 'red' : status === 'warn' ? 'amber' : 'zinc'
const guardrailStatusLabel = (status?: string) =>
  status === 'pass' ? '通过' : status === 'fail' ? '失败' : status === 'warn' ? '提示' : status || '--'

const warningTagColor = (level: string) => (level === 'danger' ? 'red' : level === 'warning' ? 'amber' : 'zinc')
const warningLabel = (level: string) => (level === 'danger' ? '风险' : level === 'warning' ? '观察' : '提示')

/* ---------- 五策略静态定义（与旧版一致） ---------- */

interface StrategyDef {
  key: string
  label: string
  role: string
  badge: string
  tagColor: 'emerald' | 'amber' | 'zinc'
  tone: string
  routes: string[]
  aliases: string[]
  summary: string
  sources: string
  entry: string
  exit: string
  diagnosis: string
}

const STRATEGY_DEFINITIONS: StrategyDef[] = [
  {
    key: 'institutional_mainwave_score88',
    label: '机构主升 Score88',
    role: '主收益引擎',
    badge: '进攻',
    tagColor: 'emerald',
    tone: 'mainwave',
    routes: ['institutional_mainwave', 'score120_core'],
    aliases: ['institutional_mainwave_score88', 'institutional_score120_mainwave', 'institutional_score120_core', 'mainwave_breakout_offense'],
    summary: '只在 Score88、行业共振和30分钟确认同时成立时出手，承担 G3 的主要进攻收益。',
    sources: 'institutional_mainwave；旧 Score120 只保留为历史证据来源。',
    entry: 'score>=120、sector_diffusion>=65、30m close>=MA20、index_mom60<=5%。index_mom60>5% 只观察，不进入影子盘或买入候选。',
    exit: '默认单槽 50%；12% 硬止损、12% 先减半、剩余仓以前低或 30m 转弱退出。',
    diagnosis: '重点看主升分数口径是否统一、指数 60 日动量是否过热、连续两笔机构主升亏损后的动态冷却是否触发。',
  },
  {
    key: 'range_weak_repair',
    label: '震荡弱势修复',
    role: '弱势修复',
    badge: '修复',
    tagColor: 'amber',
    tone: 'repair',
    routes: ['old_g3_route_v3', 'panic_repair', 'range_gap'],
    aliases: ['range_weak_repair', 'repair_range_weak'],
    summary: '处理非主升、非下跌主杀环境里的震荡或弱反弹修复，不和机构主升混排评分。',
    sources: 'old_g3_route_v3、panic_repair range 来源；用 source_strategy_label 保留来源分层。',
    entry: 'market_style 以 standard_range / weak_rebound 为主；日线出现中短期回撤后的收盘修复、下影或反包，震荡恐慌来源优先要求 30m 修复确认。',
    exit: '基础首仓 20%，最高 25%；-6% 结构止损、-10% 硬止损、+8% 先减半，剩余仓以前低或 30m 转弱退出。',
    diagnosis: '重点看候选是否真是弱势修复而不是追高，压力释放过滤是否过松，30m 修复确认是否缺失。',
  },
  {
    key: 'panic_capitulation_repair',
    label: '恐慌出清修复',
    role: '压力释放',
    badge: '防守',
    tagColor: 'amber',
    tone: 'panic',
    routes: ['panic_repair', 'old_g3_route_v3', 'down_panic'],
    aliases: ['panic_capitulation_repair', 'panic_repair_downtrend'],
    summary: '在恐慌扩散或下跌压力释放后做修复介入，保留独立风控，不再拆成多个下跌修复策略。',
    sources: 'old_g3 down_panic、panic_repair standard_downtrend；来源不同，交易策略合并。',
    entry: '日线先出现恐慌出清或压力释放；有盘中数据时优先要求 30m 修复确认，无确认时先作为观察票据。',
    exit: '基础首仓 20%，压力态可降到 12.5%；-5% 结构止损、-8% 硬止损、+8% 先减半，剩余仓以前低或 30m 转弱退出。',
    diagnosis: '重点看恐慌是否真的释放、下跌压力态是否降仓、修复票是否被误当成主升进攻票。',
  },
  {
    key: 'volume_runup_supplement',
    label: '量能续强补位',
    role: '退役观察',
    badge: '观察',
    tagColor: 'zinc',
    tone: 'supplement',
    routes: ['g2_gap_supplement'],
    aliases: ['volume_runup_supplement', 'g2_gap_supplement'],
    summary: '历史回放显示负贡献且未改善组合回撤，已退出实盘买入路线，仅保留观察和归因。',
    sources: 'g2_gap_supplement；来自 G2 量能续强和板块加分信号，不触碰 G2 runtime。',
    entry: '不再进入影子票据、纸面交易或正式买入候选；源新鲜时也只进入观察池。',
    exit: '无实盘仓位合同；历史持仓仍按原退出合同复盘。',
    diagnosis: '重点看源质量、子来源归因和是否存在未来可重新验证的严格版本；默认不再为填空槽而交易。',
  },
  {
    key: 'old_g3_strong_breakout',
    label: '强势突破',
    role: '结构突破',
    badge: '证据',
    tagColor: 'zinc',
    tone: 'breakout',
    routes: ['old_g3_route_v3', 'strong_main'],
    aliases: ['old_g3_strong_breakout', 'strong_breakout', 'mainwave_breakout_offense'],
    summary: '保留旧 G3 跨周期结构链路里的强势突破，不等同于机构主升 Score120。',
    sources: 'old_g3_route_v3 strong_main；作为正式五策略主体之一保留归因和诊断。',
    entry: '跨周期结构确认后才纳入，重点看突破是否具备趋势延续和市场适配；不和机构主升使用同一套分数直接比较。',
    exit: '默认单槽 50%；沿用 G3 统一退出合同，12% 硬止损、12% 先减半，剩余仓以前低或 30m 转弱退出。',
    diagnosis: '重点看强势突破是否被旧信号噪声放大、是否与机构主升重复计分、是否只在单一市场风格有效。',
  },
]

const TONE_BORDER: Record<string, string> = {
  mainwave: 'border-l-emerald-500',
  repair: 'border-l-blue-500',
  panic: 'border-l-amber-500',
  supplement: 'border-l-zinc-400',
  breakout: 'border-l-violet-500',
}

const scoreOf = (row: Row): number | string | undefined => {
  if ((row.route || row.mode) === 'institutional_mainwave') {
    return row.wave_style_score ?? row.selected_score ?? row.score ?? row.candidate_score
  }
  return row.score ?? row.candidate_score ?? row.mode_pick_score ?? row.v4_score ?? row.selected_score
}

/* ---------- 组件 ---------- */

export default function Gen3StateAlphaRoutes() {
  const [loading, setLoading] = useState(false)
  const [currentPayload, setCurrentPayload] = useState<CurrentPayload>({})
  const [historyPayload, setHistoryPayload] = useState<HistoryPayload>({})
  const [routeFilter, setRouteFilter] = useState('all')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [current, history] = await Promise.all([
        getGen3StateAlphaCurrent({ limit: 200 }),
        getGen3StateAlphaHistoricalTrades({ limit: 500 }),
      ])
      setCurrentPayload((current as unknown as CurrentPayload) || {})
      setHistoryPayload((history as unknown as HistoryPayload) || {})
    } catch (e) {
      // 错误已由 request 拦截器 toast
      console.error('读取 G3 路由诊断失败', e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const summary = currentPayload.summary || {}
  const routeDiagnostics = currentPayload.route_diagnostics || []
  const allCandidates = currentPayload.all_source_candidates || []
  const pipeline = currentPayload.pipeline || []
  const metrics = historyPayload.metrics || {}
  const routeMetrics = historyPayload.route_metrics || []
  const marketStyleMetrics = historyPayload.market_style_metrics || []
  const recovery = historyPayload.strategy_reduction_recovery || {}
  const recoveryHeadline = recovery.headline || {}
  const formalProfile = recovery.formal_profile || {}
  const practicalFusionSummary = recovery.practical_fusion_contract?.summary || {}
  const returnGapSummary = recovery.return_gap_audit?.summary || {}
  const fusionGuardrails = recovery.fusion_guardrails || {}
  const fusionGuardrailSummary = fusionGuardrails.summary || {}
  const fusionGuardrailChecks = fusionGuardrails.checks || []
  const recoveryStrategies = recovery.strategy_compare || []
  const recoveryScenarios = recovery.scenario_matrix || []
  const recoveryM30 = recovery.m30_integrity || {}
  const recoveryM30Summary = recoveryM30.summary || {}
  const recoveryM30Strategies = recoveryM30.by_strategy || []
  const recoveryM30NativeSummary = recoveryM30.native_semantics?.summary || {}
  const recoveryM30NativeScenarios = recoveryM30.native_semantics?.scenario_matrix || []

  const selectedRouteDiagnostic = useMemo(
    () => routeDiagnostics.find((row) => row.route === summary.selected_route) || null,
    [routeDiagnostics, summary.selected_route],
  )
  const selectedRouteAvgReturn =
    selectedRouteDiagnostic?.route_health_avg_ret
    ?? selectedRouteDiagnostic?.avg_trade_return
    ?? metrics.avg_trade_return

  const eligibleCandidateCount = allCandidates.filter((row) => truthy(row.router_eligible || row.qualified_shadow_buy)).length
  const blockedCandidateCount = allCandidates.filter(
    (row) => !truthy(row.router_eligible || row.qualified_shadow_buy) || row.block_reason,
  ).length
  const candidateEligibleRate = allCandidates.length ? eligibleCandidateCount / allCandidates.length : null

  const routeCandidateCount = useCallback((route: string) =>
    allCandidates.filter((row) => (row.route || row.mode) === route).length, [allCandidates])

  const effectivenessWarnings = useMemo(() => {
    const items: { key: string; level: string; title: string; detail: string }[] = []
    if (!summary.selected_route) {
      items.push({ key: 'no_selected_route', level: 'warning', title: '今日没有选中主路由', detail: '没有主路由时，只能观察候选池和补位路线。' })
    }
    if (!eligibleCandidateCount) {
      items.push({ key: 'no_eligible_candidates', level: 'warning', title: '今日没有合格候选', detail: '如果连续多日无候选，要检查市场适配或数据链路。' })
    }
    const routeAvg = Number(selectedRouteAvgReturn)
    if (Number.isFinite(routeAvg) && routeAvg <= 0) {
      items.push({ key: 'route_avg_non_positive', level: 'warning', title: '主路由滚动均笔不为正', detail: `当前均笔 ${pct(routeAvg)}，route_health 仍只观察，不硬阻断。` })
    }
    const worst = Number(metrics.worst_trade)
    if (Number.isFinite(worst) && worst <= -0.08) {
      items.push({ key: 'tail_loss_large', level: 'danger', title: '历史尾部亏损偏大', detail: `最差单笔 ${pct(worst)}，需要继续盯单笔账户亏损。` })
    }
    const mainwaveCount = routeCandidateCount('institutional_mainwave')
    const g2Count = routeCandidateCount('g2_gap_supplement')
    if (g2Count > 0 && mainwaveCount === 0) {
      items.push({ key: 'g2_supplement_retired_observe', level: 'warning', title: 'G2补位仅观察', detail: '量能续强补位已退出实盘交易；候选只用于源质量和历史归因复盘。' })
    }
    return items
  }, [summary.selected_route, eligibleCandidateCount, selectedRouteAvgReturn, metrics.worst_trade, routeCandidateCount])

  const strategyStatus = useMemo(() => {
    const danger = effectivenessWarnings.filter((item) => item.level === 'danger')
    const warn = effectivenessWarnings.filter((item) => item.level === 'warning')
    if (danger.length) return { label: '降权观察', reason: danger[0].title }
    if (warn.length) return { label: '有效待验', reason: warn[0].title }
    return { label: '有效', reason: '路由、候选和风险暂无明显异常' }
  }, [effectivenessWarnings])

  const todayAction = !eligibleCandidateCount
    ? '观察'
    : effectivenessWarnings.some((item) => item.level === 'danger')
      ? '降权'
      : '可影子'

  const worstTradeTone = (() => {
    const worst = Number(metrics.worst_trade)
    if (Number.isFinite(worst) && worst <= -0.08) return 'up' as const
    if (Number.isFinite(worst) && worst <= -0.05) return 'warning' as const
    return 'info' as const
  })()

  const topBlockReason = useMemo(() => {
    const counts = new Map<string, number>()
    allCandidates.forEach((row) => {
      const reason = String(row.block_reason || row.skip_reason || '').trim()
      if (!reason) return
      counts.set(reason, (counts.get(reason) || 0) + 1)
    })
    const top = Array.from(counts.entries()).sort((a, b) => b[1] - a[1])[0]
    return top ? { reason: top[0], count: top[1] } : { reason: '无', count: 0 }
  }, [allCandidates])

  const routeOptions = useMemo(() => {
    const routes = new Map<string, string>()
    ;[...routeDiagnostics, ...allCandidates, ...routeMetrics].forEach((row) => {
      const value = row.route || row.mode
      if (!value) return
      routes.set(value, row.route_label || routeName(value))
    })
    return Array.from(routes, ([value, label]) => ({ value, label }))
  }, [routeDiagnostics, allCandidates, routeMetrics])

  const visibleCandidates = routeFilter === 'all'
    ? allCandidates
    : allCandidates.filter((row) => (row.route || row.mode) === routeFilter)

  const matchesStrategy = (row: Row | null | undefined, item: StrategyDef): boolean => {
    if (!row) return false
    const values = [
      row.trade_strategy, row.route_strategy, row.strategy, row.route, row.mode, row.route_parent, row.source_route,
    ].map((v) => String(v || '').trim()).filter(Boolean)
    return values.some((v) => item.aliases.includes(v) || item.routes.includes(v))
  }

  const strategyDetails = useMemo(() => {
    return STRATEGY_DEFINITIONS.map((item) => {
      const metric = routeMetrics.find((row) => matchesStrategy(row, item)) || null
      const recoveryMetric = recoveryStrategies.find((row) => matchesStrategy(row, item)) || null
      const m30Metric = recoveryM30Strategies.find((row) => matchesStrategy(row, item)) || null
      return {
        ...item,
        candidateCount: allCandidates.filter((row) => matchesStrategy(row, item)).length,
        tradeCount: metric?.trade_count ?? recoveryMetric?.closed ?? recoveryMetric?.trade_count,
        winRate: metric?.win_rate ?? recoveryMetric?.win_rate_native_bridge_five_strategy ?? recoveryMetric?.win_rate,
        avgReturn: metric?.avg_trade_return ?? recoveryMetric?.avg_ret_native_bridge_five_strategy ?? recoveryMetric?.avg_ret,
        worstTrade: metric?.worst_trade ?? recoveryMetric?.worst_trade,
        diagnosis: m30Metric?.m30_data_ok_rate != null
          ? `${item.diagnosis} 30m 数据覆盖率 ${pct(m30Metric.m30_data_ok_rate)}，用于判断该策略是否具备实盘复现条件。`
          : item.diagnosis,
      }
    })
  }, [routeMetrics, recoveryStrategies, recoveryM30Strategies, allCandidates])

  type StatTone = 'default' | 'up' | 'down' | 'warning' | 'info'
  const metricCard = (label: string, value: string, sub?: string, tone: StatTone = 'default') => (
    <StatCard label={label} value={value} hint={sub} tone={tone} />
  )

  const infoCell = (label: string, value: string, sub: string) => (
    <div className="min-h-[5rem] rounded-lg border border-zinc-100 bg-zinc-50 px-3 py-2 dark:border-zinc-800 dark:bg-zinc-800/40">
      <span className="block text-xs text-zinc-500 dark:text-zinc-400">{label}</span>
      <strong className="mt-1 block truncate text-base tabular-nums text-zinc-950 dark:text-white">{value}</strong>
      <small className="block truncate text-xs text-zinc-500 dark:text-zinc-400">{sub}</small>
    </div>
  )

  const STAGE_BORDER: Record<string, string> = {
    pass: 'border-l-emerald-500',
    blocked: 'border-l-red-500',
    locked: 'border-l-amber-600',
    idle: 'border-l-zinc-400',
  }

  return (
    <div className="mx-auto flex w-full max-w-[100rem] flex-col gap-4 p-4 sm:p-6">
      <PageHeader
        title="有效性诊断"
        subtitle={
          <>
            <span className="block text-xs font-bold text-indigo-600 dark:text-indigo-400">G3 正式五策略</span>
            按融合后的正式 G3 五策略合同判断当前策略是否有效、今天走哪类策略、风险来自市场、候选质量还是执行链路。
          </>
        }
        actions={<Button onClick={() => void load()} disabled={loading}>{loading ? '刷新中…' : '刷新'}</Button>}
      />

      <section className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        {metricCard('策略状态', strategyStatus.label, strategyStatus.reason)}
        {metricCard('当前主路由', routeName(summary.selected_route), summary.route_reason || '--')}
        {metricCard('合格候选', String(eligibleCandidateCount), `全量 ${allCandidates.length} / 阻断 ${blockedCandidateCount}`)}
        {metricCard('主路由近样本', fmt(selectedRouteDiagnostic?.route_health_count || selectedRouteDiagnostic?.sample_count, 0), `均笔 ${pct(selectedRouteAvgReturn)}`)}
        {metricCard('最差单笔', pct(metrics.worst_trade), '账户连续亏损阈值优先看这里', worstTradeTone)}
        {metricCard('今日动作', todayAction, 'route_health 观察，不作为硬 gate', 'warning')}
      </section>

      <Card>
        <SectionTitle title="信号链路" />
        <p className="-mt-1 mb-3 text-sm text-zinc-500 dark:text-zinc-400">从市场状态到影子票据逐层确认，任何一层阻断都不能变成合格买入。</p>
        <div className="grid grid-cols-2 gap-2 xl:grid-cols-4">
          {pipeline.length === 0 && (
            <EmptyState text="暂无链路数据" className="col-span-full" />
          )}
          {pipeline.map((item) => (
            <div key={item.key} className={`min-h-[5rem] rounded-lg border border-zinc-200 border-l-4 bg-zinc-50 p-3 dark:border-zinc-800 dark:bg-zinc-800/40 ${STAGE_BORDER[item.status || 'idle'] || 'border-l-zinc-400'}`}>
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-bold text-zinc-950 dark:text-white">{item.title}</span>
                {item.count !== null && item.count !== undefined && (
                  <strong className="text-lg tabular-nums text-zinc-950 dark:text-white">{item.count}</strong>
                )}
              </div>
              <p className="mt-1 text-sm leading-snug text-zinc-500 dark:text-zinc-400">{item.detail || '--'}</p>
            </div>
          ))}
        </div>
      </Card>

      <section className="grid gap-4 xl:grid-cols-2">
        <Card>
          <SectionTitle title="失效预警" />
          <p className="-mt-1 mb-3 text-sm text-zinc-500 dark:text-zinc-400">这里集中展示不该分散在工作台、候选池和历史成交页里的策略有效性判断。</p>
          <div className="flex flex-col gap-2.5">
            {effectivenessWarnings.length === 0 && (
              <EmptyState text="暂无明显失效预警" />
            )}
            {effectivenessWarnings.map((item) => (
              <div key={item.key} className={`grid grid-cols-16 gap-2.5 rounded-lg border border-zinc-100 border-l-4 bg-zinc-50 p-2.5 dark:border-zinc-800 dark:bg-zinc-800/40 ${item.level === 'danger' ? 'border-l-red-500' : item.level === 'warning' ? 'border-l-amber-500' : 'border-l-zinc-400'}`}>
                <div><Badge color={warningTagColor(item.level)}>{warningLabel(item.level)}</Badge></div>
                <div className="min-w-0">
                  <strong className="block text-sm text-zinc-950 dark:text-white">{item.title}</strong>
                  <span className="text-sm leading-snug text-zinc-500 dark:text-zinc-400">{item.detail}</span>
                </div>
              </div>
            ))}
          </div>
        </Card>

        <Card>
          <SectionTitle title="候选质量" />
          <p className="-mt-1 mb-3 text-sm text-zinc-500 dark:text-zinc-400">候选池前排质量是比已成交收益更早的失效信号。</p>
          <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
            {infoCell('合格率', pct(candidateEligibleRate), '通过 / 全量候选')}
            {infoCell('机构主升候选', fmt(routeCandidateCount('institutional_mainwave'), 0), '主收益来源')}
            {infoCell('G2补位候选', fmt(routeCandidateCount('g2_gap_supplement'), 0), '已退出实盘，仅观察')}
            {infoCell('阻断最多', topBlockReason.reason, `${topBlockReason.count} 条`)}
          </div>
        </Card>
      </section>

      <Card>
        <SectionTitle title="交易策略详情" />
        <p className="-mt-1 mb-3 text-sm text-zinc-500 dark:text-zinc-400">
            把每个正式交易策略的职责、来源、入场合同、退出边界和诊断重点讲清楚；风控合同仍保留在独立页面，本页只做有效性解释和证据对照。
          </p>
        <div className="grid gap-3 xl:grid-cols-2">          {strategyDetails.map((item) => (
            <article key={item.key} className={`min-w-0 rounded-xl border border-zinc-200 border-l-4 bg-zinc-50 p-3 dark:border-zinc-800 dark:bg-zinc-900 ${TONE_BORDER[item.tone]}`}>
              <div className="mb-2 flex items-start justify-between gap-2.5">
                <div>
                  <span className="block text-xs text-zinc-500 dark:text-zinc-400">{item.role}</span>
                  <h3 className="text-base font-semibold text-zinc-950 dark:text-white">{item.label}</h3>
                </div>
                <Badge color={item.tagColor}>{item.badge}</Badge>
              </div>
              <p className="mb-2.5 text-sm leading-relaxed text-zinc-600 dark:text-zinc-300">{item.summary}</p>
              <div className="mb-2.5 grid grid-cols-5 gap-1.5">
                {[
                  ['候选', fmt(item.candidateCount, 0)],
                  ['交易', fmt(item.tradeCount, 0)],
                  ['胜率', pct(item.winRate)],
                  ['均笔', pct(item.avgReturn)],
                  ['最差', pct(item.worstTrade)],
                ].map(([label, value]) => (
                  <div key={label} className="min-h-[3.5rem] rounded-lg border border-zinc-100 bg-white px-2 py-1.5 dark:border-zinc-800 dark:bg-zinc-900">
                    <span className="block text-xs text-zinc-500 dark:text-zinc-400">{label}</span>
                    <strong className="block truncate text-sm tabular-nums text-zinc-950 dark:text-white">{value}</strong>
                  </div>
                ))}
              </div>
              <dl className="flex flex-col gap-2 text-sm">
                {([['来源路线', item.sources], ['入场合同', item.entry], ['仓位/退出', item.exit], ['诊断重点', item.diagnosis]] as [string, string][]).map(([dt, dd]) => (
                  <div key={dt} className="grid grid-cols-[5rem_1fr] gap-2">
                    <dt className="font-bold text-zinc-700 dark:text-zinc-300">{dt}</dt>
                    <dd className="m-0 leading-relaxed text-zinc-500 dark:text-zinc-400">{dd}</dd>
                  </div>
                ))}
              </dl>
            </article>
          ))}
        </div>
      </Card>

      <Card>
        <SectionTitle title="当前路由健康" />
        <p className="-mt-1 mb-3 text-sm text-zinc-500 dark:text-zinc-400">展示今日路由来源、合格数量、观察健康度和阻断原因。收益判断集中在本页，不再分散到工作台重复展示。</p>
        <div className="max-h-[28rem] overflow-auto">
          <Table dense striped>
            <TableHead>
              <TableRow>
                <TableHeader>路线</TableHeader>
                <TableHeader className="text-right">来源</TableHeader>
                <TableHeader className="text-right">合格</TableHeader>
                <TableHeader className="text-right">阻断</TableHeader>
                <TableHeader>健康</TableHeader>
                <TableHeader className="text-right">近样本</TableHeader>
                <TableHeader className="text-right">均笔</TableHeader>
                <TableHeader className="text-right">最差单笔</TableHeader>
                <TableHeader>阻断/说明</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {routeDiagnostics.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={9} className="py-8 text-center text-sm text-zinc-500 dark:text-zinc-400">暂无路由诊断</TableCell>
                </TableRow>
              ) : routeDiagnostics.map((row, i) => (
                <TableRow key={row.route || i}>
                  <TableCell>{row.route_label || routeName(row.route)}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmt(row.rows || row.source_rows, 0)}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmt(row.eligible_rows || row.final_candidates, 0)}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmt(row.blocked_rows, 0)}</TableCell>
                  <TableCell>
                    <Badge color={truthy(row.route_health_ok) || truthy(row.ok) ? 'emerald' : 'amber'}>
                      {truthy(row.route_health_ok) || truthy(row.ok) ? '通过' : '观察'}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{fmt(row.route_health_count || row.sample_count, 0)}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.route_health_avg_ret || row.avg_trade_return)}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.route_health_worst_ret || row.worst_trade)}</TableCell>
                  <TableCell className="max-w-[16rem] truncate text-zinc-500 dark:text-zinc-400">{row.top_block_reason}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </Card>

      <section className="grid gap-4 xl:grid-cols-2">
        <Card>
          <SectionTitle title="历史已平仓策略收益归因" />
          <p className="-mt-1 mb-3 text-sm text-zinc-500 dark:text-zinc-400">按正式交易策略统计已平仓样本；最差单笔允许为负，代表历史止损或退出尾部，不代表当前候选票的预期收益。</p>
          <Table dense striped>
            <TableHead>
              <TableRow>
                <TableHeader>交易策略</TableHeader>
                <TableHeader className="text-right">交易</TableHeader>
                <TableHeader className="text-right">胜率</TableHeader>
                <TableHeader className="text-right">均笔</TableHeader>
                <TableHeader className="text-right">最差单笔</TableHeader>
                <TableHeader className="text-right">盈亏</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {routeMetrics.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={6} className="py-8 text-center text-sm text-zinc-500 dark:text-zinc-400">暂无历史策略归因</TableCell>
                </TableRow>
              ) : routeMetrics.map((row, i) => (
                <TableRow key={i}>
                  <TableCell>
                    <div className="flex min-w-0 flex-col gap-0.5">
                      <strong className="text-[13px] text-zinc-950 dark:text-white">{row.route_label || tradeStrategyName(row)}</strong>
                      {row.strategy_summary && <small className="truncate text-xs text-zinc-500 dark:text-zinc-400">含：{row.strategy_summary}</small>}
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
        </Card>

        <Card>
          <SectionTitle title="市场风格归因" />
          <p className="-mt-1 mb-3 text-sm text-zinc-500 dark:text-zinc-400">防止路线只在单一市场环境中有效。</p>
          <Table dense striped>
            <TableHead>
              <TableRow>
                <TableHeader>风格</TableHeader>
                <TableHeader className="text-right">交易</TableHeader>
                <TableHeader className="text-right">胜率</TableHeader>
                <TableHeader className="text-right">均笔</TableHeader>
                <TableHeader className="text-right">最差</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {marketStyleMetrics.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={5} className="py-8 text-center text-sm text-zinc-500 dark:text-zinc-400">暂无市场风格归因</TableCell>
                </TableRow>
              ) : marketStyleMetrics.map((row, i) => (
                <TableRow key={i}>
                  <TableCell>{row.market_style as string}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmt(row.trade_count, 0)}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.win_rate)}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.avg_trade_return)}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.worst_trade)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      </section>

      <Card>
        <SectionTitle
          title="五策略正式合同验收"
          actions={<Badge color={recovery.ok ? 'emerald' : 'amber'}>{recovery.ok ? '已恢复召回' : '待修复'}</Badge>}
        />
        <p className="-mt-1 mb-3 text-sm text-zinc-500 dark:text-zinc-400">确认 5 个正式策略主体仍能还原 G3 最终版收益；正式收益固定看 G3最终版，桥接和简化场景只作为诊断证据。</p>
        <div className="mb-3 grid grid-cols-2 gap-2.5 lg:grid-cols-4">
          {infoCell('合同验收', fusionGuardrailSummary.ok ? '通过' : '失败', `硬失败 ${fmt(fusionGuardrailSummary.hard_fail_count, 0)} / 提示 ${fmt(fusionGuardrailSummary.warn_count, 0)}`)}
          {infoCell('正式主收益', pct(formalProfile.total_return), `G3 final 完整交易合同 / ${fmt(formalProfile.trade_count, 0)} 笔`)}
          {infoCell('简化运行收益', pct(practicalFusionSummary.recommended_total_return), `${practicalFusionSummary.recommended_scenario || 'score_range_g2'} / ${fmt(practicalFusionSummary.recommended_trade_count, 0)} 笔`)}
          {infoCell('收益保留率', pct(practicalFusionSummary.recommended_retain_rate), `默认门槛 ${pct(practicalFusionSummary.min_accept_retain_rate)}`)}
          {infoCell('桥接验收收益', pct(recoveryHeadline.total_return), '只用于验证 5 策略标签，不作为正式目标')}
          {infoCell('收益缺口', pct(returnGapSummary.return_gap), returnGapSummary.status || 'needs_rebuild')}
          {infoCell('最大回撤', pct(recoveryHeadline.max_drawdown), '研究排除源不进入默认合同')}
          {infoCell('桥接交易', fmt(recoveryHeadline.closed_trades, 0), `候选 ${fmt(recoveryHeadline.candidate_rows, 0)} / 选票 ${fmt(recoveryHeadline.selected_rows, 0)}`)}
          {infoCell('胜率 / 均笔', pct(recoveryHeadline.win_rate), `均笔 ${pct(recoveryHeadline.avg_ret)}`)}
          {infoCell('30m数据可用', pct(recoveryM30Summary.m30_data_ok_rate), `入场前窗口 ${pct(recoveryM30Summary.m30_pre_signal_ok_rate)}`)}
          {infoCell('30m代理确认', pct(recoveryM30Summary.m30_confirmed_proxy_rate), '低通过率时保留原生确认语义')}
          {infoCell('原生30m确认', pct(recoveryM30NativeSummary.native_m30_confirmed_rate), `代理误杀 ${fmt(recoveryM30NativeSummary.proxy_kill_count, 0)} 个候选`)}
          {infoCell('代理筛选收益', pct(recoveryM30NativeSummary.scenario_proxy_m30_total_return), '研究过滤，不作为默认硬 gate')}
        </div>

        <Table dense striped>
          <TableHead>
            <TableRow>
              <TableHeader>验收项</TableHeader>
              <TableHeader>状态</TableHeader>
              <TableHeader>证据</TableHeader>
              <TableHeader>处理口径</TableHeader>
            </TableRow>
          </TableHead>
          <TableBody>
            {fusionGuardrailChecks.length === 0 ? (
              <TableRow>
                <TableCell colSpan={4} className="py-8 text-center text-sm text-zinc-500 dark:text-zinc-400">暂无合同验收项</TableCell>
              </TableRow>
            ) : fusionGuardrailChecks.map((row) => (
              <TableRow key={row.key}>
                <TableCell>{row.title || row.key}</TableCell>
                <TableCell><Badge color={guardrailTagColor(row.status)}>{guardrailStatusLabel(row.status)}</Badge></TableCell>
                <TableCell className="max-w-[16rem] truncate text-zinc-500 dark:text-zinc-400">{row.evidence}</TableCell>
                <TableCell className="max-w-[16rem] truncate text-zinc-500 dark:text-zinc-400">{row.action}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>

        <div className="mt-3">
          <Table dense striped>
            <TableHead>
              <TableRow>
                <TableHeader>交易策略</TableHeader>
                <TableHeader className="text-right">历史贡献</TableHeader>
                <TableHeader className="text-right">日线代理</TableHeader>
                <TableHeader className="text-right">原生桥接</TableHeader>
                <TableHeader className="text-right">桥接胜率</TableHeader>
                <TableHeader className="text-right">桥接均笔</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {recoveryStrategies.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={6} className="py-8 text-center text-sm text-zinc-500 dark:text-zinc-400">暂无策略还原数据</TableCell>
                </TableRow>
              ) : recoveryStrategies.map((row, i) => (
                <TableRow key={i}>
                  <TableCell>{row.trade_strategy_label || row.trade_strategy}</TableCell>
                  <TableCell className="text-right tabular-nums">{money(row.sum_pnl_historical_g3_final)}</TableCell>
                  <TableCell className="text-right tabular-nums">{money(row.sum_pnl_daily_proxy_five_strategy)}</TableCell>
                  <TableCell className="text-right tabular-nums">{money(row.sum_pnl_native_bridge_five_strategy)}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.win_rate_native_bridge_five_strategy)}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.avg_ret_native_bridge_five_strategy)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>

        <div className="mt-3">
          <Table dense striped>
            <TableHead>
              <TableRow>
                <TableHeader>场景</TableHeader>
                <TableHeader className="text-right">收益</TableHeader>
                <TableHeader className="text-right">回撤</TableHeader>
                <TableHeader className="text-right">交易</TableHeader>
                <TableHeader className="text-right">胜率</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {recoveryScenarios.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={5} className="py-8 text-center text-sm text-zinc-500 dark:text-zinc-400">暂无场景矩阵</TableCell>
                </TableRow>
              ) : recoveryScenarios.map((row, i) => (
                <TableRow key={i}>
                  <TableCell>{row.scenario}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.total_return)}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.max_drawdown)}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmt(row.closed, 0)}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.win_rate)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>

        <div className="mt-3">
          <Table dense striped>
            <TableHead>
              <TableRow>
                <TableHeader>交易策略</TableHeader>
                <TableHeader className="text-right">候选</TableHeader>
                <TableHeader className="text-right">30m数据</TableHeader>
                <TableHeader className="text-right">入场前窗口</TableHeader>
                <TableHeader className="text-right">代理确认</TableHeader>
                <TableHeader className="text-right">均30m位置</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {recoveryM30Strategies.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={6} className="py-8 text-center text-sm text-zinc-500 dark:text-zinc-400">暂无30m完整性数据</TableCell>
                </TableRow>
              ) : recoveryM30Strategies.map((row, i) => (
                <TableRow key={i}>
                  <TableCell>{row.trade_strategy_label || row.trade_strategy}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmt(row.candidate_count, 0)}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.m30_data_ok_rate)}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.m30_pre_signal_ok_rate)}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.m30_confirmed_proxy_rate)}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.avg_m30_day_close_pos)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>

        <div className="mt-3">
          <Table dense striped>
            <TableHead>
              <TableRow>
                <TableHeader>30m语义场景</TableHeader>
                <TableHeader className="text-right">候选</TableHeader>
                <TableHeader className="text-right">交易</TableHeader>
                <TableHeader className="text-right">收益</TableHeader>
                <TableHeader className="text-right">回撤</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {recoveryM30NativeScenarios.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={5} className="py-8 text-center text-sm text-zinc-500 dark:text-zinc-400">暂无原生30m语义场景</TableCell>
                </TableRow>
              ) : recoveryM30NativeScenarios.map((row, i) => (
                <TableRow key={i}>
                  <TableCell>{row.scenario}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmt(row.candidates, 0)}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmt(row.closed, 0)}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.total_return)}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.max_drawdown)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </Card>

      <Card>
        <SectionTitle
          title="路线候选明细"
          actions={
            <select
              className="h-8 rounded-lg border border-zinc-200 bg-white px-2 text-sm text-zinc-900 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-100"
              value={routeFilter}
              onChange={(e) => setRouteFilter(e.target.value)}
            >
              <option value="all">全部路线</option>
              {routeOptions.map((item) => (
                <option key={item.value} value={item.value}>{item.label}</option>
              ))}
            </select>
          }
        />
        <p className="-mt-1 mb-3 text-sm text-zinc-500 dark:text-zinc-400">只保留路线级候选复核；完整逐日候选复盘进入候选池页。</p>
        <div className="max-h-[26rem] overflow-auto">
          <Table dense striped>
            <TableHead>
              <TableRow>
                <TableHeader>代码</TableHeader>
                <TableHeader>名称</TableHeader>
                <TableHeader>交易策略</TableHeader>
                <TableHeader>来源路线</TableHeader>
                <TableHeader>准入</TableHeader>
                <TableHeader className="text-right">得分</TableHeader>
                <TableHeader>阻断/说明</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {visibleCandidates.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={7} className="py-8 text-center text-sm text-zinc-500 dark:text-zinc-400">暂无候选明细</TableCell>
                </TableRow>
              ) : visibleCandidates.map((row) => (
                <TableRow key={row.code}>
                  <TableCell className="tabular-nums">{row.code}</TableCell>
                  <TableCell>{row.name}</TableCell>
                  <TableCell>{tradeStrategyName(row)}</TableCell>
                  <TableCell>{row.route_label || routeName(row.route || row.mode)}</TableCell>
                  <TableCell>
                    <Badge color={truthy(row.router_eligible || row.qualified_shadow_buy) ? 'emerald' : 'amber'}>
                      {truthy(row.router_eligible || row.qualified_shadow_buy) ? '通过' : '阻断'}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{fmt(scoreOf(row))}</TableCell>
                  <TableCell className="max-w-[18rem] truncate text-zinc-500 dark:text-zinc-400">{row.block_reason}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </Card>
    </div>
  )
}
