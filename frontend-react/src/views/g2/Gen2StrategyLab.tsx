import { useCallback, useEffect, useState } from 'react'
import { getGen2StrategyLab } from '@/api/trading'
import { Alert } from '@/components/catalyst/alert'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Input } from '@/components/catalyst/input'
import { Field, Label } from '@/components/catalyst/fieldset'
import { Card, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/catalyst/table'

interface StrategyLabPayload {
  selected_date?: string
  meta?: { status?: string }
  v4_reference?: { score_pool_size?: number; candidate_count?: number }
  patterns?: {
    primary_count?: number
    message?: string
    rules?: string[]
    rows?: Array<{
      rank?: number | string
      rank_change_status?: string
      rank_change_text?: string
      code?: string
      name?: string
      gen2_score?: number | string
      score_total?: number | string
      mom5?: number | string
      mom10?: number | string
      pattern_tags?: Array<{ key: string; label: string; level?: string }>
      pattern_summary?: string
    }>
  }
  reports?: Array<{
    source_label?: string
    name?: string
    total_return_pct?: number | string
    max_drawdown_pct?: number | string
    sharpe?: number
    trade_count?: number | string
  }>
  next_steps?: string[]
  data_freshness?: {
    latest_daily_date?: string
    latest_trigger_date?: string
    latest_valid_signal_date?: string
    latest_shadow_date?: string
    notes?: string[]
  }
}

function num(value: unknown, digits = 2): string {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(digits) : '--'
}

function todayStr(): string {
  const d = new Date()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${m}-${day}`
}

function rankBadgeColor(status?: string): 'red' | 'amber' | 'emerald' | 'zinc' {
  if (status === 'up') return 'red'
  if (status === 'new') return 'amber'
  if (status === 'down') return 'emerald'
  return 'zinc'
}

function patternBadgeColor(level?: string): 'red' | 'emerald' | 'zinc' {
  if (level === 'hot') return 'red'
  if (level === 'primary') return 'emerald'
  return 'zinc'
}

export default function Gen2StrategyLab() {
  const [loading, setLoading] = useState(false)
  const [available, setAvailable] = useState(false)
  const [message, setMessage] = useState('')
  const [signalDate, setSignalDate] = useState(todayStr())
  const [payload, setPayload] = useState<StrategyLabPayload>({})

  const fetchData = useCallback(async () => {
    setLoading(true)
    try {
      const data = ((await getGen2StrategyLab({ signal_date: signalDate, limit: 30 })) as any) || {}
      setPayload(data)
      setAvailable(!!data.available)
      setMessage(data.message || '')
    } catch (error) {
      setAvailable(false)
      setMessage((error as Error)?.message || '读取第二代策略实验数据失败')
    } finally {
      setLoading(false)
    }
  }, [signalDate])

  useEffect(() => {
    fetchData()
  }, [fetchData])

  const selectedDate = payload.selected_date || ''
  const meta = payload.meta || {}
  const v4 = payload.v4_reference || {}
  const patterns = payload.patterns || { rows: [], rules: [] }
  const reports = payload.reports || []
  const nextSteps = payload.next_steps || []
  const dataFreshness = payload.data_freshness || null

  const freshnessSummary = (() => {
    const d = dataFreshness || {}
    if (!d.latest_shadow_date) return 'G2影子台账尚未生成可展示日期。'
    if (d.latest_daily_date && d.latest_daily_date > d.latest_shadow_date) {
      return `页面当前信号日来自G2有效影子台账；日线数据已到 ${d.latest_daily_date}，有效信号最新为 ${d.latest_shadow_date}。`
    }
    return '页面当前信号日与G2有效影子台账保持一致。'
  })()

  const freshnessLagged = !!(
    dataFreshness?.latest_daily_date &&
    dataFreshness?.latest_shadow_date &&
    dataFreshness.latest_daily_date > dataFreshness.latest_shadow_date
  )

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="第二代策略实验台"
        subtitle="当前读取 G2 V3 User V2 two_stop_cd3_skip 影子台账，用于观察候选、熔断状态和盘中确认质量。"
        actions={
          <>
            <Field>
              <Label htmlFor="gen2-lab-date">信号日</Label>
              <Input
                id="gen2-lab-date"
                type="date"
                value={signalDate}
                onChange={(e) => setSignalDate(e.target.value)}
                className="w-44"
              />
            </Field>
            <Button disabled={loading} onClick={fetchData}>{loading ? '查询中…' : '查询'}</Button>
          </>
        }
      />

      {!available && !loading && (
        <Alert color="amber">{message || '第二代策略实验数据不可用'}</Alert>
      )}

      {available && (
        <>
          <section className="grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3">
            {[
              { label: '信号日', value: selectedDate || '--' },
              { label: '影子候选', value: v4.score_pool_size || 0 },
              { label: '可观察/已执行', value: v4.candidate_count || 0 },
              { label: 'G2主观察', value: patterns.primary_count || 0 },
            ].map((metric) => (
              <StatCard key={metric.label} label={metric.label} value={metric.value} />
            ))}
          </section>

          {dataFreshness && (
            <Card className="border border-amber-300 bg-amber-50 p-5 ring-0 dark:border-amber-400/30 dark:bg-amber-400/10 dark:ring-0">
              <div className="mb-3 flex items-start justify-between gap-3">
                <div>
                  <SectionTitle className="mb-1" title="数据链路诊断" />
                  <p className="text-sm text-zinc-600 dark:text-zinc-400">{freshnessSummary}</p>
                </div>
                <Badge color={freshnessLagged ? 'amber' : 'emerald'}>
                  {freshnessLagged ? '有效信号滞后' : '已对齐'}
                </Badge>
              </div>
              <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-2.5">
                {[
                  { label: 'V4日线', value: dataFreshness.latest_daily_date },
                  { label: 'G2原始触发', value: dataFreshness.latest_trigger_date },
                  { label: '有效信号', value: dataFreshness.latest_valid_signal_date },
                  { label: '影子台账', value: dataFreshness.latest_shadow_date },
                ].map((item) => (
                  <div
                    key={item.label}
                    className="flex flex-col gap-1 rounded-lg border border-amber-200 bg-white p-3 dark:border-amber-400/20 dark:bg-white/5"
                  >
                    <span className="text-xs text-amber-700 dark:text-amber-400">{item.label}</span>
                    <strong className="text-lg text-zinc-900 dark:text-white tabular-nums">{item.value || '--'}</strong>
                  </div>
                ))}
              </div>
              {(dataFreshness.notes || []).length > 0 && (
                <ul className="mt-3 list-disc pl-5 text-sm leading-relaxed text-zinc-600 dark:text-zinc-400">
                  {(dataFreshness.notes || []).map((note) => (
                    <li key={note}>{note}</li>
                  ))}
                </ul>
              )}
            </Card>
          )}

          <Card className="p-5">
            <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
              <SectionTitle
                className="mb-0"
                title="模式观察池"
                actions={<Badge color="zinc">{meta.status || 'research_only'}</Badge>}
              />
              <p className="w-full text-sm text-zinc-500 dark:text-zinc-400">
                {patterns.message || '以 G2 V3 User V2 two_stop_cd3_skip 影子台账为基础生成观察标签。'}
              </p>
            </div>
            <div className="max-h-[480px] overflow-auto rounded-lg">
              <Table dense className="min-w-full">
                <TableHead>
                  <TableRow>
                    <TableHeader>V4排名</TableHeader>
                    <TableHeader>变化</TableHeader>
                    <TableHeader>代码</TableHeader>
                    <TableHeader>名称</TableHeader>
                    <TableHeader>Gen2分</TableHeader>
                    <TableHeader>V4分</TableHeader>
                    <TableHeader>5日动量%</TableHeader>
                    <TableHeader>10日动量%</TableHeader>
                    <TableHeader>模式标签</TableHeader>
                    <TableHeader>说明</TableHeader>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {(patterns.rows || []).map((row, idx) => (
                    <TableRow key={`${row.code}-${row.rank}-${idx}`} className="bg-white dark:bg-zinc-950">
                      <TableCell className="tabular-nums">{row.rank}</TableCell>
                      <TableCell>
                        <Badge color={rankBadgeColor(row.rank_change_status)}>{row.rank_change_text || '--'}</Badge>
                      </TableCell>
                      <TableCell className="font-mono">{row.code}</TableCell>
                      <TableCell>{row.name}</TableCell>
                      <TableCell className="tabular-nums">{row.gen2_score}</TableCell>
                      <TableCell className="tabular-nums">{row.score_total}</TableCell>
                      <TableCell className="tabular-nums">{row.mom5}</TableCell>
                      <TableCell className="tabular-nums">{row.mom10}</TableCell>
                      <TableCell>
                        <div className="flex flex-wrap items-center gap-1.5">
                          {(row.pattern_tags || []).map((tag) => (
                            <Badge key={tag.key} color={patternBadgeColor(tag.level)}>
                              {tag.label}
                            </Badge>
                          ))}
                          {!(row.pattern_tags || []).length && (
                            <span className="text-xs text-zinc-400">等待确认</span>
                          )}
                        </div>
                      </TableCell>
                      <TableCell className="max-w-[260px] truncate" title={row.pattern_summary}>
                        {row.pattern_summary}
                      </TableCell>
                    </TableRow>
                  ))}
                  {!(patterns.rows || []).length && (
                    <TableRow className="bg-white dark:bg-zinc-950">
                      <TableCell colSpan={10} className="text-center text-zinc-400">暂无模式候选</TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </div>
          </Card>

          <section className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(280px,1fr)]">
            <Card className="p-5">
              <SectionTitle title="现有实验摘要" />
              <div className="overflow-auto rounded-lg">
                <Table dense className="min-w-full">
                  <TableHead>
                    <TableRow>
                      <TableHeader>来源</TableHeader>
                      <TableHeader>实验</TableHeader>
                      <TableHeader>收益%</TableHeader>
                      <TableHeader>回撤%</TableHeader>
                      <TableHeader>Sharpe</TableHeader>
                      <TableHeader>交易</TableHeader>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {reports.map((row, idx) => (
                      <TableRow key={`${row.name}-${idx}`} className="bg-white dark:bg-zinc-950">
                        <TableCell>{row.source_label}</TableCell>
                        <TableCell className="max-w-[220px] truncate" title={row.name}>{row.name}</TableCell>
                        <TableCell className="tabular-nums">{row.total_return_pct}</TableCell>
                        <TableCell className="tabular-nums">{row.max_drawdown_pct}</TableCell>
                        <TableCell className="tabular-nums">{num(row.sharpe, 2)}</TableCell>
                        <TableCell className="tabular-nums">{row.trade_count}</TableCell>
                      </TableRow>
                    ))}
                    {!reports.length && (
                      <TableRow className="bg-white dark:bg-zinc-950">
                        <TableCell colSpan={6} className="text-center text-zinc-400">暂无实验报告</TableCell>
                      </TableRow>
                    )}
                  </TableBody>
                </Table>
              </div>
            </Card>

            <Card className="p-5">
              <SectionTitle title="开发边界" />
              <ul className="list-disc pl-5 text-sm leading-loose text-zinc-600 dark:text-zinc-400">
                {(patterns.rules || []).map((rule) => (
                  <li key={rule}>{rule}</li>
                ))}
                {nextSteps.map((step) => (
                  <li key={step}>{step}</li>
                ))}
              </ul>
            </Card>
          </section>
        </>
      )}
    </div>
  )
}
