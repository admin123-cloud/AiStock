import { useCallback, useEffect, useState } from 'react'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/catalyst/table'
import { Card, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import {
  getG3DailyReview,
  getG3ReviewAttribution,
  getG3ShadowReview,
  getG3SignalQuality,
  type G3AttributionResponse,
  type G3DailyReviewResponse,
  type G3ShadowReviewResponse,
  type G3SignalQualityResponse,
} from '@/api/g3'

type TabKey = 'attribution' | 'daily' | 'shadow' | 'quality'

const fmtMoney = (v?: number | null): string => {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  const n = Number(v)
  return `${n > 0 ? '+' : ''}¥${n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

const fmtPct = (v?: number | null): string => {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  const n = Number(v)
  return `${n > 0 ? '+' : ''}${n.toFixed(2)}%`
}

const pnlColor = (v?: number | null): string => {
  if (v == null || v === 0) return 'text-zinc-300'
  return v > 0 ? 'text-red-400' : 'text-emerald-400'
}

export default function G3Review() {
  const [tab, setTab] = useState<TabKey>('attribution')
  const [windowDays, setWindowDays] = useState<number>(30)
  const [benchmark, setBenchmark] = useState<string>('000300.SH')
  const [attribution, setAttribution] = useState<G3AttributionResponse | null>(null)
  const [daily, setDaily] = useState<G3DailyReviewResponse | null>(null)
  const [shadow, setShadow] = useState<G3ShadowReviewResponse | null>(null)
  const [quality, setQuality] = useState<G3SignalQualityResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [attr, d, s, q] = await Promise.all([
        getG3ReviewAttribution(windowDays, benchmark),
        getG3DailyReview(10),
        getG3ShadowReview(20),
        getG3SignalQuality(10),
      ])
      setAttribution(attr)
      setDaily(d)
      setShadow(s)
      setQuality(q)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }, [windowDays, benchmark])

  useEffect(() => { void load() }, [load])

  const summary = attribution?.summary

  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 p-4 sm:p-6">
      <PageHeader
        title="G3 复盘与收益归因台账"
        subtitle="整合四维收益归因（Beta/Alpha/做T/执行偏离）、日终复盘、R40 影子做T与信号质量日报"
        actions={
          <Button onClick={() => void load()} disabled={loading}>
            {loading ? '刷新中…' : '刷新'}
          </Button>
        }
      />

      {error && (
        <div className="rounded-lg border border-red-500/40 bg-red-500/10 px-4 py-2.5 text-sm text-red-200">
          {error}
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        <Button color={tab === 'attribution' ? 'blue' : 'zinc'} onClick={() => setTab('attribution')}>
          四维收益归因 ({attribution?.daily_rows?.length ?? 0} 天)
        </Button>
        <Button color={tab === 'daily' ? 'blue' : 'zinc'} onClick={() => setTab('daily')}>
          日终复盘与对账 ({daily?.daily_reviews?.length ?? 0})
        </Button>
        <Button color={tab === 'shadow' ? 'blue' : 'zinc'} onClick={() => setTab('shadow')}>
          R40 影子做T台账 ({shadow?.daily_records?.length ?? 0})
        </Button>
        <Button color={tab === 'quality' ? 'blue' : 'zinc'} onClick={() => setTab('quality')}>
          信号质量日报 ({quality?.reports?.length ?? 0})
        </Button>
      </div>

      {tab === 'attribution' && (
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-2">
              {[7, 30, 90].map((d) => (
                <Button key={d} color={windowDays === d ? 'blue' : 'zinc'} onClick={() => setWindowDays(d)}>
                  近 {d} 天
                </Button>
              ))}
              <select
                value={benchmark}
                onChange={(e) => setBenchmark(e.target.value)}
                className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-xs text-white"
              >
                <option value="000300.SH">基准：沪深300 (000300.SH)</option>
                <option value="000001.SH">基准：上证指数 (000001.SH)</option>
              </select>
            </div>
            <span className="font-mono text-xs text-zinc-400">
              窗口 {attribution?.from_date ?? '—'} ~ {attribution?.to_date ?? '—'} · 覆盖 {summary?.coverage_days ?? 0} 天
            </span>
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <StatCard
              label="① 大盘 Beta 贡献"
              value={fmtMoney(summary?.beta_pnl)}
              hint={`基准 ${attribution?.benchmark?.name ?? '沪深300'} · 匹配 ${attribution?.benchmark?.matched_days ?? 0} 日`}
            />
            <StatCard
              label="② 选股 Alpha 贡献"
              value={fmtMoney(summary?.alpha_pnl)}
              hint={`总盈亏 ${fmtMoney(summary?.total_pnl)} − Beta − 做T`}
            />
            <StatCard
              label="③ 做T 滚动净增厚"
              value={fmtMoney(summary?.t_trade_pnl)}
              hint={`配对 ${summary?.t_round_trips ?? 0} 轮 · 对账 ${summary?.ledger_days ?? 0} 天`}
            />
            <StatCard
              label="④ 执行纪律 & 偏离警示"
              value={summary?.avg_discipline_score != null ? `${summary.avg_discipline_score} 分` : '—'}
              hint={`违约 ${summary?.violations_total ?? 0} 次 · 止损拖延估算 ${fmtMoney(summary?.estimated_stop_drag_pnl)}`}
            />
          </div>

          {attribution?.friction_events && attribution.friction_events.length > 0 && (
            <Card className="border-amber-500/40 bg-amber-950/10">
              <div className="mb-2 flex items-center justify-between">
                <SectionTitle title="近期执行偏离 / 风控违约警示" />
                <Badge color="red">{attribution.friction_events.length} 条</Badge>
              </div>
              <div className="space-y-1.5 text-xs">
                {attribution.friction_events.map((ev, idx) => (
                  <div key={`${ev.date}-${ev.code}-${idx}`} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-zinc-800 bg-zinc-950/60 px-3 py-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-zinc-400">{ev.date}</span>
                      <Badge color="amber">{ev.code}</Badge>
                      <span className="text-zinc-200">{ev.label}</span>
                    </div>
                    {ev.estimated_drag_pnl != null && (
                      <span className="font-mono text-amber-300">
                        估算风险拖延 {fmtMoney(ev.estimated_drag_pnl)}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            </Card>
          )}

          <Card>
            <div className="mb-3 flex items-center justify-between">
              <SectionTitle title="每日四维收益归因明细" />
              <Badge color="zinc">红涨绿跌（A股口径）</Badge>
            </div>
            {!attribution?.daily_rows?.length ? (
              <div className="py-6 text-center text-sm text-zinc-400">当前窗口暂无可归因的账户快照或对账记录</div>
            ) : (
              <div className="overflow-x-auto">
                <Table>
                  <TableHead>
                    <TableRow>
                      <TableHeader>日期</TableHeader>
                      <TableHeader>水位</TableHeader>
                      <TableHeader>基准涨跌</TableHeader>
                      <TableHeader>当日总盈亏</TableHeader>
                      <TableHeader>① 大盘 Beta</TableHeader>
                      <TableHeader>② 选股 Alpha</TableHeader>
                      <TableHeader>③ 做T净额</TableHeader>
                      <TableHeader>④ 纪律/违约</TableHeader>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {attribution.daily_rows.map((row) => (
                      <TableRow key={row.date}>
                        <TableCell className="font-mono text-xs text-zinc-200">{row.date}</TableCell>
                        <TableCell className="font-mono text-xs text-zinc-300">
                          {row.position_pct != null ? `${row.position_pct.toFixed(1)}%` : '—'}
                        </TableCell>
                        <TableCell className={`font-mono text-xs ${pnlColor(row.benchmark_ret_pct)}`}>
                          {fmtPct(row.benchmark_ret_pct)}
                        </TableCell>
                        <TableCell className={`font-mono text-xs font-semibold ${pnlColor(row.total_pnl)}`}>
                          {fmtMoney(row.total_pnl)}
                        </TableCell>
                        <TableCell className={`font-mono text-xs ${pnlColor(row.beta_pnl)}`}>
                          {fmtMoney(row.beta_pnl)}
                        </TableCell>
                        <TableCell className={`font-mono text-xs ${pnlColor(row.alpha_pnl)}`}>
                          {fmtMoney(row.alpha_pnl)}
                        </TableCell>
                        <TableCell className={`font-mono text-xs ${pnlColor(row.t_trade_pnl)}`}>
                          {fmtMoney(row.t_trade_pnl)} {row.t_round_trips > 0 ? `(${row.t_round_trips}轮)` : ''}
                        </TableCell>
                        <TableCell className="text-xs">
                          <div className="flex items-center gap-1.5">
                            <span className="font-mono text-zinc-300">
                              {row.discipline_score != null ? `${row.discipline_score}分` : '—'}
                            </span>
                            {row.violations_total > 0 && (
                              <Badge color="red">违约 {row.violations_total}</Badge>
                            )}
                          </div>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
            {attribution?.data_notes && attribution.data_notes.length > 0 && (
              <ul className="mt-3 space-y-1 border-t border-zinc-800 pt-2.5 text-xs text-zinc-400">
                {attribution.data_notes.map((n, i) => (
                  <li key={i}>• {n}</li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      )}

      {tab === 'daily' && (
        <div className="flex flex-col gap-4">
          {!daily?.daily_reviews?.length ? (
            <Card><div className="py-6 text-center text-sm text-zinc-400">暂无日终复盘记录</div></Card>
          ) : (
            daily.daily_reviews.map((item) => (
              <Card key={item.file}>
                <div className="mb-2 flex items-center justify-between">
                  <SectionTitle title={`日终复盘 ${item.date}`} />
                  <Badge color="zinc">{item.file}</Badge>
                </div>
                <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg bg-zinc-950/70 p-3 font-mono text-xs leading-relaxed text-zinc-200">
                  {item.content}
                </pre>
              </Card>
            ))
          )}
        </div>
      )}

      {tab === 'shadow' && (
        <div className="flex flex-col gap-4">
          {shadow?.rolling_summary && (
            <Card>
              <SectionTitle title="4 周滚动统计" />
              <pre className="mt-2 overflow-auto whitespace-pre-wrap rounded-lg bg-zinc-950/70 p-3 font-mono text-xs text-zinc-200">
                {JSON.stringify(shadow.rolling_summary, null, 2)}
              </pre>
            </Card>
          )}
          {!shadow?.daily_records?.length ? (
            <Card><div className="py-6 text-center text-sm text-zinc-400">暂无影子做T记录</div></Card>
          ) : (
            shadow.daily_records.map((rec, idx) => (
              <Card key={String(rec.file || idx)}>
                <div className="mb-2 flex items-center justify-between">
                  <span className="font-mono text-sm font-medium text-white">{String(rec.file || rec.date || `#${idx + 1}`)}</span>
                  <Badge color="blue">observe_only</Badge>
                </div>
                <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-lg bg-zinc-950/70 p-3 font-mono text-xs text-zinc-300">
                  {JSON.stringify(rec, null, 2)}
                </pre>
              </Card>
            ))
          )}
        </div>
      )}

      {tab === 'quality' && (
        <div className="flex flex-col gap-4">
          {!quality?.reports?.length ? (
            <Card><div className="py-6 text-center text-sm text-zinc-400">暂无信号质量日报</div></Card>
          ) : (
            quality.reports.map((rep) => (
              <Card key={rep.file}>
                <div className="mb-2 flex items-center justify-between">
                  <SectionTitle title={`信号质量日报 ${rep.date}`} />
                  <Badge color="emerald">{rep.file}</Badge>
                </div>
                <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg bg-zinc-950/70 p-3 font-mono text-xs leading-relaxed text-zinc-200">
                  {rep.markdown}
                </pre>
              </Card>
            ))
          )}
        </div>
      )}
    </div>
  )
}
