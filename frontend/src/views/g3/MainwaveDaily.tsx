import { useCallback, useEffect, useMemo, useState } from 'react'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Alert, AlertTitle } from '@/components/catalyst/alert'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/catalyst/table'
import { Dialog, DialogBody, DialogTitle } from '@/components/catalyst/dialog'
import { Card, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import {
  dateTime, reasonText, statusLabel, statusTone, type StatusTone,
} from '@/api/operations'
import { getG3MainwaveDaily, type G3AiScreeningItem, type G3AiScreeningSummary } from '@/api/g3'

interface MainwaveCandidate {
  code?: string
  name?: string
  industry?: string
  industry_source?: string
  stage?: string
  score?: number | string | null
  resonance?: number | string | null
  reason?: string | null
  m30_source?: string
  conflict_rows?: number
  confirmation_time?: string
  reference_price?: number | string | null
  entry_date?: string
  decision_date?: string
  ai_screening?: G3AiScreeningItem
}

interface MainwaveCheck { name?: string; message?: string; ok?: boolean }
interface HistoryDay { date?: string; accepted?: boolean; reason?: string; repair_triggered?: boolean }
interface MainwaveChange { code?: string; name?: string; before?: string; after?: string }

interface MainwaveDailyData {
  state?: string
  stale_batch?: boolean
  entry_date?: string
  decision_date?: string
  source_generated_at?: string
  batch?: { ok?: boolean; batch_id?: string }
  summary?: {
    candidate_count?: number
    data_blocked_count?: number
    confirmed_count?: number
    accepted_days?: number
    observed_days?: number
  }
  checks?: MainwaveCheck[]
  candidates?: MainwaveCandidate[]
  comparison_status?: string
  comparison_date?: string
  changes?: MainwaveChange[]
  history?: HistoryDay[]
  contract?: {
    entry?: {
      minimum_score?: number | string
      minimum_same_day_industry_mainwave_count?: number | string
      index_mom60_max?: number
    }
  }
  contract_metadata?: { strategy_id?: string; sha256?: string }
  ai_screening_summary?: G3AiScreeningSummary
}

type FilterKey = 'all' | 'green_only' | 'data_blocked' | 'waiting_30m' | 'confirmed'

const FILTERS: { key: FilterKey; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'green_only', label: '绿灯精选' },
  { key: 'data_blocked', label: '数据阻断' },
  { key: 'waiting_30m', label: '待确认' },
  { key: 'confirmed', label: '已确认' },
]

const lightBadge = (light?: string) => {
  if (light === 'green') return { label: '绿灯通过', color: 'emerald' as const }
  if (light === 'red') return { label: '红灯否决', color: 'red' as const }
  return { label: '黄灯留意', color: 'amber' as const }
}

const toneBadge = (tone: StatusTone) =>
  tone === 'danger' ? 'red' : tone === 'success' ? 'emerald' : 'amber'

const score = (v: number | string | null | undefined): string =>
  v != null && v !== '' && Number.isFinite(Number(v)) ? Number(v).toFixed(1) : '待评估'

export default function MainwaveDaily() {
  const [data, setData] = useState<MainwaveDailyData>({})
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [filter, setFilter] = useState<FilterKey>('all')
  const [selected, setSelected] = useState<MainwaveCandidate | null>(null)
  const [historyDay, setHistoryDay] = useState<HistoryDay | null>(null)

  const load = useCallback(async (forceRefresh = false) => {
    setLoading(true)
    setError('')
    try {
      const res = await getG3MainwaveDaily(forceRefresh)
      setData(res as MainwaveDailyData)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const headline = loading && !data.state ? '正在读取当日证据' : statusLabel(data.state ?? '')
  const explanation = data.state === 'data_blocked'
    ? '当前数据不足以形成可信买入结论，候选为0不等于今天没有机会。'
    : data.stale_batch
      ? '当前显示历史批次，不作为今天的新买点。'
      : '按当前合同跟踪候选；确认结果仍需通过账户与执行安全检查。'

  const blockedChecks = useMemo(() => (data.checks || []).filter((x) => !x.ok), [data.checks])
  const candidates = useMemo(
    () =>
      (data.candidates || []).filter((x) => {
        if (filter === 'all') return true
        if (filter === 'green_only') return x.ai_screening?.risk_light === 'green'
        return x.stage === filter
      }),
    [data.candidates, filter],
  )
  const aiSummary = data.ai_screening_summary
  const alertTone = statusTone(data.state ?? '')
  const minimumScore = data.contract?.entry?.minimum_score

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-4 sm:p-6">
      <PageHeader
        title="主升每日跟踪"
        subtitle="先判断数据是否可信，再看候选推进与每日验收。"
        actions={<Button onClick={() => void load(true)} disabled={loading}>{loading ? '刷新中…' : '刷新跟踪'}</Button>}
      />

      {error && (
        <div role="alert" className="rounded-lg border border-red-500/30 bg-red-50 px-4 py-3 text-sm text-red-700 dark:bg-red-500/10 dark:text-red-300">
          {error}；当前结果未更新。
        </div>
      )}

      <p className="text-sm text-zinc-500 dark:text-zinc-400">
        批次校验：{data.batch?.ok ? '完整批次已验证' : '未通过 / 旧产物待重新发布'} · {data.batch?.batch_id || '暂无批次号'}
      </p>

      <Alert color={alertTone === 'danger' ? 'red' : alertTone === 'success' ? 'green' : 'amber'}>
        <AlertTitle>{headline}</AlertTitle>
        <div className="mt-1 space-y-1 text-sm text-zinc-700 dark:text-zinc-300">
          <p>{explanation}</p>
          <p>
            执行日 {data.entry_date || '待确认'} · 决策日 {data.decision_date || '待确认'} · 信号批次 {dateTime(data.source_generated_at)}
          </p>
        </div>
      </Alert>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {[
          { label: '主升候选', value: data.summary?.candidate_count ?? '—' },
          { label: '数据不可判断', value: data.summary?.data_blocked_count ?? '—' },
          { label: '30m已确认', value: data.summary?.confirmed_count ?? '—' },
          { label: '最近观察通过', value: `${data.summary?.accepted_days ?? '—'} / ${data.summary?.observed_days ?? '—'}` },
        ].map((m) => (
          <StatCard key={m.label} label={m.label} value={m.value} />
        ))}
      </div>

      {aiSummary && (
        <Card className={aiSummary.allow_new_mainwave ? 'border-emerald-500/30 bg-emerald-950/10' : 'border-amber-500/40 bg-amber-950/10'}>
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800 pb-2.5">
            <div className="flex flex-wrap items-center gap-2">
              <SectionTitle title="AI 候选股排雷与催化精选（Phase 2A）" />
              <Badge color={aiSummary.mode === 'agent_enriched' ? 'emerald' : 'blue'}>
                {aiSummary.mode === 'agent_enriched'
                  ? `Agent 深度穿透 (${aiSummary.agent_enriched_count}/${aiSummary.total_candidates})`
                  : '确定性底线排雷（待 Agent 穿透）'}
              </Badge>
              <Badge color={aiSummary.allow_new_mainwave ? 'emerald' : 'red'}>
                {aiSummary.allow_new_mainwave ? '大盘允许开仓' : '大盘防守·仅观察'}
              </Badge>
            </div>
            <div className="flex items-center gap-2 text-xs">
              <Badge color="emerald">绿灯 {aiSummary.counts.green}</Badge>
              <Badge color="amber">黄灯 {aiSummary.counts.yellow}</Badge>
              <Badge color="red">红灯 {aiSummary.counts.red}</Badge>
            </div>
          </div>

          <p className="mt-2 text-xs leading-relaxed text-zinc-300">{aiSummary.gate_notice}</p>
          {aiSummary.agent_summary_note && (
            <p className="mt-1 text-xs text-blue-300">Agent 摘要：{aiSummary.agent_summary_note}</p>
          )}

          {aiSummary.top_picks.length > 0 && (
            <div className="mt-3 grid grid-cols-1 gap-2.5 md:grid-cols-2 lg:grid-cols-3">
              {aiSummary.top_picks.map((pick) => (
                <div
                  key={pick.code}
                  className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-2.5 text-xs"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-semibold text-white">
                      {pick.name || pick.code} <span className="font-mono text-zinc-400">{pick.code}</span>
                    </span>
                    <Badge color="emerald">催化 {pick.catalyst_score}/5</Badge>
                  </div>
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-zinc-400">
                    <span>{pick.industry || '—'}</span>
                    <span>· 主升 {score(pick.score)}</span>
                    <span>· {pick.catalyst_type}</span>
                  </div>
                  <p className="mt-1.5 line-clamp-2 leading-relaxed text-zinc-300">{pick.one_line_thesis}</p>
                </div>
              ))}
            </div>
          )}
        </Card>
      )}

      {blockedChecks.length > 0 && (
        <Card>
          <SectionTitle title="今天先处理这些问题" />
          <div className="flex flex-col gap-2">
            {blockedChecks.map((check) => (
              <div key={check.name} className="rounded-lg border border-red-500/20 bg-red-50/60 px-3 py-2 text-sm dark:bg-red-500/10">
                <strong className="text-zinc-900 dark:text-red-300">{check.message}</strong>
                <p className="text-xs text-zinc-500 dark:text-zinc-400">{check.name}</p>
              </div>
            ))}
          </div>
        </Card>
      )}

      <Card>
        <SectionTitle
          title="候选进展"
          actions={
            <div className="flex flex-wrap gap-1">
              {FILTERS.map((f) =>
                filter === f.key ? (
                  <Button key={f.key} onClick={() => setFilter(f.key)}>{f.label}</Button>
                ) : (
                  <Button key={f.key} plain onClick={() => setFilter(f.key)}>{f.label}</Button>
                ),
              )}
            </div>
          }
        />
        <div className="max-h-[28rem] overflow-auto">
          <Table>
            <TableHead>
              <TableRow>
                <TableHeader className="min-w-[10rem]">股票</TableHeader>
                <TableHeader className="min-w-[8rem]">所属行业</TableHeader>
                <TableHeader className="min-w-[9rem]">当前阶段</TableHeader>
                <TableHeader>主升分 / 门槛</TableHeader>
                <TableHeader>同业共振</TableHeader>
                <TableHeader className="min-w-[8rem]">AI 排雷 / 催化</TableHeader>
                <TableHeader className="min-w-[14rem]">核心逻辑 / 阻断说明</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {candidates.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={7} className="py-8 text-center text-sm text-zinc-500 dark:text-zinc-400">
                    本批次无可展示候选；请先查看上方数据状态
                  </TableCell>
                </TableRow>
              ) : candidates.map((row) => {
                const lb = lightBadge(row.ai_screening?.risk_light)
                return (
                <TableRow key={row.code} className="cursor-pointer" onClick={() => setSelected(row)}>
                  <TableCell>
                    <button type="button" className="text-sm font-medium text-indigo-600 hover:underline dark:text-indigo-400"
                      onClick={(e) => { e.stopPropagation(); setSelected(row) }}>
                      {row.name || row.code}
                    </button>
                    <div className="text-xs tabular-nums text-zinc-500 dark:text-zinc-400">{row.code}</div>
                  </TableCell>
                  <TableCell>{row.industry || '--'}</TableCell>
                  <TableCell>
                    <Badge color={toneBadge(statusTone(row.stage ?? ''))}>{statusLabel(row.stage ?? '')}</Badge>
                  </TableCell>
                  <TableCell className="tabular-nums">{score(row.score)} / {minimumScore ?? '--'}</TableCell>
                  <TableCell className="tabular-nums">{row.resonance ?? '--'}</TableCell>
                  <TableCell>
                    <div className="flex flex-wrap items-center gap-1">
                      <Badge color={lb.color}>{lb.label}</Badge>
                      {row.ai_screening?.catalyst_score != null && (
                        <Badge color="blue">{row.ai_screening.catalyst_score}/5</Badge>
                      )}
                    </div>
                  </TableCell>
                  <TableCell className="max-w-[20rem] truncate text-xs text-zinc-400">
                    {row.ai_screening?.one_line_thesis || reasonText(row.reason)}
                  </TableCell>
                </TableRow>
                )
              })}
            </TableBody>
          </Table>
        </div>
      </Card>

      <Card>
        <SectionTitle title="较上个有效交易日的变化" />
        <div className="space-y-1 text-sm text-zinc-600 dark:text-zinc-300">
          {data.comparison_status === 'data_blocked' ? (
            <p>当前数据未通过，不把异常造成的候选消失解释为策略退出。</p>
          ) : !data.comparison_date ? (
            <p>尚无同合同的有效比较基线。发布器将开始记录每日状态。</p>
          ) : (
            <>
              <p>比较基线 {data.comparison_date}</p>
              {!data.changes?.length && <p>本批次未发现阶段变化。</p>}
              {data.changes?.map((change) => (
                <p key={change.code}>
                  {change.name || change.code}：{change.before ? statusLabel(change.before) : '新进入'} → {change.after ? statusLabel(change.after) : '退出候选'}
                </p>
              ))}
            </>
          )}
        </div>
      </Card>

      <Card>
        <SectionTitle title="最近每日验收" />
        <p className="text-sm text-zinc-500 dark:text-zinc-400">数据冲突日与正常空仓日分开；通过天数只计当前合同。</p>
        <div className="mt-3 overflow-x-auto pb-1">
          <div className="flex gap-1.5">
            {data.history?.map((day) => (
              <button key={day.date} type="button" title={`${day.date} ${day.reason ?? ''}`}
                className={`flex min-w-[3.2rem] flex-col items-center rounded-lg border px-2 py-1.5 text-xs transition
                  ${day.accepted
                    ? 'border-emerald-500/30 bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300'
                    : 'border-red-500/30 bg-red-50 text-red-700 dark:bg-red-500/10 dark:text-red-300'}`}
                onClick={() => setHistoryDay(day)}>
                <span className="tabular-nums">{day.date?.slice(5)}</span>
                <small>{day.accepted ? '通过' : '阻断'}</small>
              </button>
            ))}
          </div>
        </div>
        {historyDay && (
          <p aria-live="polite" className="mt-2 text-sm text-zinc-600 dark:text-zinc-300">
            {historyDay.date}：{reasonText(historyDay.reason)}；分钟修复{historyDay.repair_triggered ? '已触发' : '未触发'}。
          </p>
        )}
      </Card>

      <Card>
        <SectionTitle title="当前合同" />
        <p className="text-sm text-zinc-600 dark:text-zinc-300">
          主升分 ≥ {minimumScore ?? '--'} · 同业共振 ≥ {data.contract?.entry?.minimum_same_day_industry_mainwave_count ?? '--'} · 指数60日涨幅 ≤ {((data.contract?.entry?.index_mom60_max || 0) * 100).toFixed(0)}% · 已完成30m放量突破前20根高点
        </p>
        <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
          {data.contract_metadata?.strategy_id} · {data.contract_metadata?.sha256?.slice(0, 12)} · 影子观察，正式买点和自动下单保持锁定。
        </p>
      </Card>

      <Dialog open={!!selected} onClose={() => setSelected(null)} size="2xl">
        <DialogTitle>候选证据</DialogTitle>
        <DialogBody>
          {selected && (
            <div className="space-y-2 text-sm text-zinc-700 dark:text-zinc-300">
              <h3 className="text-base font-semibold text-zinc-950 dark:text-white">
                {selected.name} {selected.code}
              </h3>
              <div><Badge color={toneBadge(statusTone(selected.stage ?? ''))}>{statusLabel(selected.stage ?? '')}</Badge></div>
              <p>所属行业：{selected.industry}（{selected.industry_source || '来源待确认'}）</p>
              <p>主升分：{score(selected.score)}；同业共振：{selected.resonance}</p>
              <p>30m来源：{selected.m30_source || '未知'}；冲突行数：{selected.conflict_rows || 0}</p>
              <p>确认时间：{selected.confirmation_time || '尚未确认'}</p>
              <p>原因：{reasonText(selected.reason)}</p>
              <p>参考价格：{selected.reference_price || '未提供'}</p>
              <p>执行日 {selected.entry_date}；决策日 {selected.decision_date}</p>
              {selected.ai_screening && (
                <div className="mt-3 rounded-lg border border-zinc-800 bg-zinc-950/60 p-3 space-y-1.5">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-semibold text-white">AI 排雷与催化评估</span>
                    <Badge color={lightBadge(selected.ai_screening.risk_light).color}>
                      {lightBadge(selected.ai_screening.risk_light).label}
                    </Badge>
                    <Badge color="blue">
                      催化 {selected.ai_screening.catalyst_score}/5 · {selected.ai_screening.catalyst_type}
                    </Badge>
                  </div>
                  <p className="text-xs text-zinc-200">{selected.ai_screening.one_line_thesis}</p>
                  {selected.ai_screening.risk_flags.length > 0 && (
                    <p className="text-xs text-amber-300">风险标签：{selected.ai_screening.risk_flags.join('；')}</p>
                  )}
                </div>
              )}
            </div>
          )}
        </DialogBody>
      </Dialog>
    </div>
  )
}
