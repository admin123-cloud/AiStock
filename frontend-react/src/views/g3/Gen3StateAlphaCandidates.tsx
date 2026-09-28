import { useEffect, useMemo, useState } from 'react'
import { Alert } from '@/components/catalyst/alert'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Select } from '@/components/catalyst/select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/catalyst/table'
import { useToast } from '@/components/toast'
import { Card, EmptyState, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import { getGen3StateAlphaCurrent } from '@/api/trading'
import { compactStatusWithZh, statusWithZh } from '@/utils/g3StatusText'

type Row = Record<string, any>

function truthy(value: any): boolean {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0
  if (typeof value === 'string') return ['1', 'true', 'yes', 'ok', 'pass'].includes(value.trim().toLowerCase())
  return false
}

function routeName(route: any): string {
  const names: Record<string, string> = {
    institutional_mainwave: '机构主升',
    panic_repair: '恐慌修复',
    old_g3_route_v3: '旧G3兼容',
    old_g3_route: '旧G3兼容',
  }
  return names[route] || route || '--'
}

function tradeStrategyName(row: Row): string {
  const names: Record<string, string> = {
    range_weak_repair: '震荡弱势修复',
    panic_capitulation_repair: '恐慌出清修复',
    institutional_mainwave_score88: '机构主升Score88',
    institutional_score120_mainwave: '机构主升Score120（历史）',
    old_g3_strong_breakout: '强势突破',
    mainwave_breakout_offense: '主升/突破进攻',
    volume_runup_supplement: '量能续强补位',
  }
  return row?.trade_strategy_label || names[row?.trade_strategy] || row?.route_strategy_label || routeName(row?.route || row?.mode)
}

function opportunityText(row: Row | null): string {
  if (!row) return '--'
  if (row.opportunity_explain) return row.opportunity_explain
  const route = String(row.route || row.trade_strategy || '')
  const sector = row.sector_name || row.l2_sector_name || row.industry || row.template_label || '未识别板块'
  const diffusion = Number(row.sector_diffusion_score)
  const label = row.sector_diffusion_label || (Number.isFinite(diffusion)
    ? (diffusion >= 80 ? '强扩散' : diffusion >= 65 ? '有效扩散' : diffusion >= 50 ? '观察扩散' : '扩散不足')
    : '扩散未知')
  if (route === 'institutional_mainwave' || route === 'institutional_mainwave_score88' || route === 'institutional_score120_mainwave') {
    const details = [`机构主升机会落在${sector}`, label]
    if (Number.isFinite(diffusion)) details.push(`扩散分${diffusion.toFixed(1)}`)
    if (row.sector_candidate_count !== undefined && row.sector_candidate_count !== null) details.push(`同板块候选${Number(row.sector_candidate_count).toFixed(0)}只`)
    if (row.same_sector_selected_count >= 2) details.push('同板块共振允许')
    return `${details.join('；')}。`
  }
  return `非机构主升路线；板块${sector}只用于重复暴露检查。`
}

type TagColor = 'red' | 'emerald' | 'amber' | 'zinc'

function naturalActionTagType(row: Row): TagColor {
  const action = String(row?.natural_action || '')
  if (action === 'skip') return 'red'
  if (action === 'allow_reduced') return 'amber'
  if (action === 'allow') return 'emerald'
  return 'zinc'
}

function pct(value: any): string {
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : '--'
}

function price(value: any): string {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(2) : '--'
}

function fmt(value: any, digits = 2): string {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(digits) : '--'
}

function checkType(status: any): TagColor {
  if (status === 'pass') return 'emerald'
  if (status === 'locked' || status === 'warn') return 'amber'
  return 'red'
}

function checkText(status: any): string {
  if (status === 'pass') return '通过'
  if (status === 'locked') return '锁定'
  if (status === 'warn') return '观察'
  return '阻断'
}

const MODE_OPTIONS = [
  { label: '全部', value: 'all' },
  { label: '选中', value: 'selected' },
  { label: '阻断', value: 'blocked' },
  { label: '票据', value: 'tickets' },
]


export default function Gen3StateAlphaCandidates() {
  const toast = useToast()
  const [loading, setLoading] = useState(false)
  const [payload, setPayload] = useState<Row>({})
  const [routeFilter, setRouteFilter] = useState('all')
  const [candidateMode, setCandidateMode] = useState('all')

  const summary = payload.summary || {}
  const allCandidates = useMemo(() => (Array.isArray(payload.all_source_candidates) ? payload.all_source_candidates : []), [payload])
  const selectedCandidates = useMemo(() => (Array.isArray(payload.selected_candidates) ? payload.selected_candidates : []), [payload])
  const blockedCandidates = useMemo(() => (Array.isArray(payload.blocked_candidates) ? payload.blocked_candidates : []), [payload])
  const shadowTickets = useMemo(() => (Array.isArray(payload.shadow_tickets) ? payload.shadow_tickets : []), [payload])
  const qualifiedTickets = useMemo(() => (Array.isArray(payload.qualified_tickets) ? payload.qualified_tickets : shadowTickets.filter((row: Row) => truthy(row.qualified_shadow_buy))), [payload, shadowTickets])
  const pipeline = useMemo(() => (Array.isArray(payload.pipeline) ? payload.pipeline : []), [payload])
  const readinessChecks = useMemo(() => (Array.isArray(payload.readiness_checks) ? payload.readiness_checks : []), [payload])
  const blockedCount = blockedCandidates.length
  const firstBlockReason = blockedCandidates[0]?.block_reason || blockedCandidates[0]?.top_block_reason || '暂无主要阻断'
  const statusText = `当前 ${statusWithZh(summary.diagnosis_code)}，全量 ${allCandidates.length}，选中 ${selectedCandidates.length}，合格票据 ${qualifiedTickets.length}`

  const routeOptions = useMemo(() => {
    const routes = new Map<string, string>()
    ;[...allCandidates, ...selectedCandidates, ...shadowTickets].forEach((row: Row) => {
      const value = row.route || row.mode
      if (!value) return
      routes.set(value, row.route_label || routeName(value))
    })
    return Array.from(routes, ([value, label]) => ({ value, label }))
  }, [allCandidates, selectedCandidates, shadowTickets])

  const baseCandidates = useMemo(() => {
    if (candidateMode === 'selected') return selectedCandidates
    if (candidateMode === 'blocked') return blockedCandidates
    if (candidateMode === 'tickets') return shadowTickets
    return allCandidates
  }, [candidateMode, selectedCandidates, blockedCandidates, shadowTickets, allCandidates])

  const visibleCandidates = useMemo(() => {
    if (routeFilter === 'all') return baseCandidates
    return baseCandidates.filter((row: Row) => (row.route || row.mode) === routeFilter)
  }, [baseCandidates, routeFilter])

  async function load() {
    setLoading(true)
    try {
      setPayload(await getGen3StateAlphaCurrent({ limit: 200 }))
    } catch (error: any) {
      toast.error(error?.message || '读取 G3 候选池失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="flex flex-col gap-3.5">
      <PageHeader
        title="候选池审计"
        subtitle={
          <>
            <span className="font-bold text-blue-600 dark:text-blue-400">G3 State Alpha</span>
            {' · 从全量来源、路由筛选、阻断原因到最终影子票据，逐层确认今日信号为什么进入或退出。'}
          </>
        }
        actions={<Button color="blue" disabled={loading} onClick={load}>{loading ? '刷新中…' : '刷新'}</Button>}
      />

      <Alert color={blockedCount ? 'amber' : 'green'} className="text-sm">{statusText}</Alert>

      <section className="grid grid-cols-1 gap-2.5 sm:grid-cols-3 xl:grid-cols-6">
        <StatCard label="入场日" value={summary.entry_date || '--'} hint={`决策日 ${summary.decision_date || '--'}`} />
        <StatCard label="全量来源" value={String(allCandidates.length)} hint="接口返回上限内候选" />
        <StatCard label="路由选中" value={String(selectedCandidates.length)} hint={routeName(summary.selected_route)} />
        <StatCard label="影子票据" value={String(shadowTickets.length)} hint={`合格 ${qualifiedTickets.length}`} />
        <StatCard label="阻断候选" value={String(blockedCount)} tone={blockedCount ? 'up' : 'default'} hint={firstBlockReason} />
        <StatCard label="正式交易" value="锁定" tone="warning" hint="formal / auto order 均为 false" />
      </section>

      <Card>
        <SectionTitle title="信号链路" />
        <p className="mb-3 text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">每个阶段都必须留下数量和说明；任何阻断都不应被误报为可买。</p>
        <div className="grid grid-cols-1 gap-2.5 md:grid-cols-2 xl:grid-cols-4">
          {pipeline.map((item: Row) => {
            const stageColor = item.status === 'pass' ? 'border-l-emerald-500' : item.status === 'blocked' ? 'border-l-red-600' : item.status === 'locked' ? 'border-l-amber-600' : 'border-l-zinc-400'
            return (
              <div key={item.key || item.title} className={`flex min-h-[86px] flex-col gap-1.5 rounded-lg border border-zinc-200 border-l-4 bg-zinc-50 p-2.5 dark:border-white/10 dark:bg-white/5 ${stageColor}`}>
                <div className="flex items-center justify-between gap-2.5">
                  <span className="text-sm font-bold text-zinc-900 dark:text-white">{item.title || item.name || item.key}</span>
                  {item.count !== null && item.count !== undefined && (
                    <strong className="text-base tabular-nums text-zinc-900 dark:text-white">{item.count}</strong>
                  )}
                </div>
                <p className="text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">{item.detail || item.message || '--'}</p>
              </div>
            )
          })}
        </div>
      </Card>

      <Card>
        <SectionTitle
          title="全量候选"
          actions={
            <div className="flex flex-wrap items-center justify-end gap-2">
              <div className="w-48">
                <Select value={routeFilter} onChange={(e) => setRouteFilter(e.target.value)}>
                  <option value="all">全部路线</option>
                  {routeOptions.map((item) => (
                    <option key={item.value} value={item.value}>{item.label}</option>
                  ))}
                </Select>
              </div>
              <div className="flex overflow-hidden rounded-lg border border-zinc-200 dark:border-white/15">
                {MODE_OPTIONS.map((item) => (
                  <button
                    key={item.value}
                    type="button"
                    onClick={() => setCandidateMode(item.value)}
                    className={`px-3 py-1.5 text-sm font-medium transition-colors ${
                      candidateMode === item.value
                        ? 'bg-blue-600 text-white'
                        : 'bg-transparent text-zinc-700 hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-white/10'
                    }`}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            </div>
          }
        />
        <p className="mb-3 text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">用于审计路由准入、排序、30m 确认、阻断原因和最终影子状态。</p>
        <div className="max-h-[560px] overflow-y-auto">
          <Table dense striped>
            <TableHead>
              <TableRow>
                {['代码', '名称', '交易策略', '来源路线', '板块机会', '准入', '30m', '得分', '仓位', '自然纪律', '计划时间', '确认时间', '影子状态', '自然说明', '阻断/说明'].map((h) => (
                  <TableHeader key={h}>{h}</TableHeader>
                ))}
              </TableRow>
            </TableHead>
            <TableBody>
              {visibleCandidates.length === 0 && (
                <TableRow>
                  <TableCell colSpan={15} className="text-center text-zinc-500 dark:text-zinc-400">暂无候选记录</TableCell>
                </TableRow>
              )}
              {visibleCandidates.map((row: Row, idx: number) => (
                <TableRow key={`${row.code}-${idx}`}>
                  <TableCell className="tabular-nums">{row.code}</TableCell>
                  <TableCell>{row.name}</TableCell>
                  <TableCell>{tradeStrategyName(row)}</TableCell>
                  <TableCell>{row.route_label || routeName(row.route || row.mode)}</TableCell>
                  <TableCell className="max-w-[300px] truncate">{opportunityText(row)}</TableCell>
                  <TableCell>
                    <Badge color={truthy(row.router_eligible || row.qualified_shadow_buy) ? 'emerald' : 'amber'}>
                      {truthy(row.router_eligible || row.qualified_shadow_buy) ? '通过' : '阻断'}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <Badge color={truthy(row.m30_confirmed) ? 'emerald' : 'zinc'}>
                      {compactStatusWithZh(row.m30_status || (truthy(row.m30_confirmed) ? 'pass' : 'pending_confirm'))}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{fmt(row.score || row.mode_pick_score || row.v4_score)}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.position_pct || row.slot_pct)}</TableCell>
                  <TableCell>
                    <Badge color={naturalActionTagType(row)}>{row.natural_action_label || '观察'}</Badge>
                    <small className="mt-1 block text-xs text-zinc-500 dark:text-zinc-400">{pct(row.natural_position_pct)}</small>
                  </TableCell>
                  <TableCell className="max-w-[170px] truncate">{row.planned_entry_ts}</TableCell>
                  <TableCell className="max-w-[170px] truncate">{row.confirm_datetime}</TableCell>
                  <TableCell className="max-w-[200px] truncate">{compactStatusWithZh(row.shadow_status)}</TableCell>
                  <TableCell className="max-w-[260px] truncate">
                    自然纪律：{row.natural_action_label || '观察'}
                    {Number.isFinite(Number(row.natural_position_pct)) ? `，自然仓位 ${pct(row.natural_position_pct)}` : ''}；{row.natural_reason || '符合当前自然交易观察合同'}
                  </TableCell>
                  <TableCell className="max-w-[280px] truncate">{row.block_reason}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </Card>

      <section className="grid grid-cols-1 gap-3.5 xl:grid-cols-2">
        <Card>
          <SectionTitle title="最终影子票据" />
          <p className="mb-3 text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">进入纸面执行前的可交易合同视图。</p>
          <Table dense striped>
            <TableHead>
              <TableRow>
                {['代码', '名称', '交易策略', '来源路线', '板块机会', '仓位', '自然纪律', '硬止损', '退出合同'].map((h) => (
                  <TableHeader key={h}>{h}</TableHeader>
                ))}
              </TableRow>
            </TableHead>
            <TableBody>
              {shadowTickets.length === 0 && (
                <TableRow>
                  <TableCell colSpan={9} className="text-center text-zinc-500 dark:text-zinc-400">暂无影子票据</TableCell>
                </TableRow>
              )}
              {shadowTickets.map((row: Row, idx: number) => (
                <TableRow key={`${row.code}-${idx}`}>
                  <TableCell className="tabular-nums">{row.code}</TableCell>
                  <TableCell>{row.name}</TableCell>
                  <TableCell>{tradeStrategyName(row)}</TableCell>
                  <TableCell>{row.route_label}</TableCell>
                  <TableCell className="max-w-[280px] truncate">{opportunityText(row)}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(row.position_pct)}</TableCell>
                  <TableCell>
                    <Badge color={naturalActionTagType(row)}>{row.natural_action_label || '观察'}</Badge>
                    <small className="mt-1 block text-xs text-zinc-500 dark:text-zinc-400">{pct(row.natural_position_pct)}</small>
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{price(row.hard_stop)}</TableCell>
                  <TableCell className="max-w-[280px] truncate">{row.exit_contract}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>

        <Card>
          <SectionTitle title="准入检查" />
          <p className="mb-3 text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">页面结论必须和运行链路状态一致。</p>
          <div className="flex flex-col gap-2.5">
            {readinessChecks.map((item: Row) => (
              <div key={item.key || item.label} className="grid grid-cols-[54px_minmax(0,1fr)] items-start gap-2.5 border-b border-zinc-100 pb-2.5 last:border-b-0 dark:border-white/5">
                <Badge color={checkType(item.status)}>{checkText(item.status)}</Badge>
                <div>
                  <strong className="text-sm font-bold text-zinc-900 dark:text-white">{item.label || item.name || item.key}</strong>
                  <p className="text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">{item.detail || item.message || '--'}</p>
                </div>
              </div>
            ))}
            {readinessChecks.length === 0 && (
              <EmptyState text="暂无准入检查" />
            )}
          </div>
        </Card>
      </section>
    </div>
  )
}
