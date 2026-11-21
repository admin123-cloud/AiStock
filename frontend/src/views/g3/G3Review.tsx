import { useCallback, useEffect, useState } from 'react'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/catalyst/table'
import { Card, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import {
  getG3DailyReview,
  getG3ReviewAttribution,
  getG3ReviewCoach,
  getG3ReviewForwardEval,
  getG3ShadowReview,
  getG3SignalQuality,
  type G3AttributionResponse,
  type G3DailyReviewResponse,
  type G3ForwardEvalResponse,
  type G3ShadowReviewResponse,
  type G3SignalQualityResponse,
  type G3TradingCoachResponse,
} from '@/api/g3'

type TabKey = 'attribution' | 'coach' | 'forward' | 'daily' | 'shadow' | 'quality'

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
  const [coach, setCoach] = useState<G3TradingCoachResponse | null>(null)
  const [forwardEval, setForwardEval] = useState<G3ForwardEvalResponse | null>(null)
  const [daily, setDaily] = useState<G3DailyReviewResponse | null>(null)
  const [shadow, setShadow] = useState<G3ShadowReviewResponse | null>(null)
  const [quality, setQuality] = useState<G3SignalQualityResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [attr, c, fe, d, s, q] = await Promise.all([
        getG3ReviewAttribution(windowDays, benchmark),
        getG3ReviewCoach(14),
        getG3ReviewForwardEval(60),
        getG3DailyReview(10),
        getG3ShadowReview(20),
        getG3SignalQuality(10),
      ])
      setAttribution(attr)
      setCoach(c)
      setForwardEval(fe)
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
        <Button color={tab === 'coach' ? 'blue' : 'zinc'} onClick={() => setTab('coach')}>
          AI 交易教练 ({coach?.bleeding_points?.length ?? 0} 项出血点)
        </Button>
        <Button color={tab === 'forward' ? 'blue' : 'zinc'} onClick={() => setTab('forward')}>
          前向验真 & ETF 影子 ({forwardEval?.ai_screening_forward?.samples_total ?? 0})
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

      {tab === 'coach' && (
        <div className="flex flex-col gap-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <StatCard
              label="近 14 日纪律评级"
              value={coach?.grade || '—'}
              hint={`平均纪律分 ${coach?.metrics?.avg_discipline_score ?? '—'}`}
            />
            <StatCard
              label="止损违约标的"
              value={`${coach?.metrics?.stop_ignored_codes ?? 0} 只`}
              hint={`估算风险拖延 ${fmtMoney(coach?.metrics?.estimated_stop_drag_pnl)}`}
            />
            <StatCard
              label="接飞刀 / 计划外交易"
              value={`${coach?.metrics?.knife_catch_total ?? 0} / ${coach?.metrics?.off_plan_total ?? 0}`}
              hint={`总违约 ${coach?.metrics?.violations_total ?? 0} 次`}
            />
            <StatCard
              label="近 14 日做T净增厚"
              value={fmtMoney(coach?.metrics?.t_trade_pnl)}
              hint={`未跟单草案 ${coach?.metrics?.unmatched_drafts_total ?? 0} 条`}
            />
          </div>

          <Card className="border-red-500/40 bg-red-950/10">
            <div className="mb-2 flex items-center justify-between">
              <SectionTitle title="下周必须守住的行为红线（Phase 3A）" />
              <Badge color="red">止血铁律</Badge>
            </div>
            <ul className="space-y-2 text-sm text-zinc-100">
              {(coach?.next_week_red_lines || []).map((line, i) => (
                <li key={i} className="rounded-md border border-zinc-800 bg-zinc-950/60 px-3 py-2">
                  {line}
                </li>
              ))}
            </ul>
          </Card>

          {coach?.agent_notes && (
            <Card className="border-blue-500/40 bg-blue-950/10">
              <div className="mb-2 flex items-center justify-between">
                <SectionTitle title={`Agent 深度教练点评 · ${coach.agent_notes.headline}`} />
                <Badge color="blue">{coach.agent_notes.date}</Badge>
              </div>
              <p className="whitespace-pre-wrap text-sm leading-relaxed text-zinc-200">
                {coach.agent_notes.weekly_diagnosis}
              </p>
            </Card>
          )}

          <Card>
            <div className="mb-2 flex items-center justify-between">
              <SectionTitle title="行为出血点诊断清单" />
              <Badge color="amber">{coach?.bleeding_points?.length ?? 0} 项</Badge>
            </div>
            {!coach?.bleeding_points?.length ? (
              <div className="py-6 text-center text-sm text-emerald-300">近 14 日未检出显著行为出血点，继续保持纪律！</div>
            ) : (
              <div className="space-y-2">
                {coach.bleeding_points.map((bp, idx) => (
                  <div key={`${bp.tag}-${idx}`} className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3 text-xs">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex items-center gap-2">
                        <Badge color={bp.severity === 'critical' ? 'red' : bp.severity === 'high' ? 'amber' : 'blue'}>
                          {bp.severity === 'critical' ? '严重' : bp.severity === 'high' ? '高危' : '关注'}
                        </Badge>
                        <span className="text-sm font-semibold text-white">{bp.title}</span>
                      </div>
                      {bp.impact_estimate != null && (
                        <span className="font-mono text-amber-300">估算影响 {fmtMoney(bp.impact_estimate)}</span>
                      )}
                    </div>
                    <p className="mt-1.5 text-zinc-300">{bp.detail}</p>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </div>
      )}

      {tab === 'forward' && (
        <div className="flex flex-col gap-4">
          {/* A. 稳健腿 6 大宽基 ETF 自动影子台账 */}
          <Card>
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <SectionTitle title="稳健腿 · 6 大宽基 ETF 波动率目标自动影子台账（T-1 无未来函数回放）" />
              {forwardEval?.defense_etf_shadow?.available && (
                <Badge color="blue">
                  {forwardEval.defense_etf_shadow.from_date} ~ {forwardEval.defense_etf_shadow.to_date}（{forwardEval.defense_etf_shadow.window_days} 交易日）
                </Badge>
              )}
            </div>
            {forwardEval?.defense_etf_shadow?.available && forwardEval.defense_etf_shadow.summary ? (() => {
              const es = forwardEval.defense_etf_shadow.summary
              return (
                <div className="space-y-4">
                  <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                    <StatCard
                      label="稳健腿影子收益"
                      value={fmtPct(es.cum_ret_pct)}
                      hint={`沪深300 基准 ${fmtPct(es.bench_cum_ret_pct)} · 超额 ${fmtPct(es.excess_ret_pct)}`}
                    />
                    <StatCard
                      label="稳健腿最大回撤"
                      value={fmtPct(es.max_drawdown_pct)}
                      hint={`沪深300 回撤 ${fmtPct(es.bench_max_drawdown_pct)}`}
                    />
                    <StatCard
                      label="回撤保护节省"
                      value={`${es.drawdown_saved_pct >= 0 ? '+' : ''}${es.drawdown_saved_pct.toFixed(2)} pp`}
                      hint={`调仓切换 ${es.switch_events} 次`}
                    />
                    <StatCard
                      label="最新影子仓位"
                      value={`${es.latest_invested_pct.toFixed(1)}%`}
                      hint={es.latest_active_etfs.length > 0 ? es.latest_active_etfs.join(' / ') : '100% 现金防守'}
                    />
                  </div>
                  <div className="overflow-x-auto">
                    <Table>
                      <TableHead>
                        <TableRow>
                          <TableHeader>日期</TableHeader>
                          <TableHeader>影子仓位 / 现金</TableHeader>
                          <TableHeader>持仓宽基 ETF</TableHeader>
                          <TableHeader>当日收益</TableHeader>
                          <TableHeader>沪深300</TableHeader>
                          <TableHeader>影子净值 / 基准</TableHeader>
                        </TableRow>
                      </TableHead>
                      <TableBody>
                        {forwardEval.defense_etf_shadow.daily_curve.map((r) => (
                          <TableRow key={r.date}>
                            <TableCell className="font-mono text-xs text-zinc-200">{r.date}</TableCell>
                            <TableCell className="font-mono text-xs text-zinc-300">
                              {r.invested_pct.toFixed(1)}% / {r.cash_pct.toFixed(1)}%
                            </TableCell>
                            <TableCell className="text-xs text-zinc-300">
                              {r.active_etfs.length > 0 ? r.active_etfs.join('、') : <Badge color="amber">现金防守</Badge>}
                            </TableCell>
                            <TableCell className={`font-mono text-xs ${pnlColor(r.day_ret_pct)}`}>{fmtPct(r.day_ret_pct)}</TableCell>
                            <TableCell className={`font-mono text-xs ${pnlColor(r.bench_day_ret_pct)}`}>{fmtPct(r.bench_day_ret_pct)}</TableCell>
                            <TableCell className="font-mono text-xs text-zinc-200">
                              {r.nav.toFixed(4)} / <span className="text-zinc-400">{r.bench_nav.toFixed(4)}</span>
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                </div>
              )
            })() : (
              <div className="py-4 text-sm text-zinc-400">{forwardEval?.defense_etf_shadow?.reason || '暂无 ETF 影子台账数据'}</div>
            )}
          </Card>

          {/* B. AI 排雷前向绩效追踪（绿灯 vs 黄灯 vs 红灯） */}
          <Card>
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <SectionTitle title="AI 候选股排雷前向验真（绿灯 vs 黄灯 vs 红灯 T+5 / T+10）" />
              <Badge color="emerald">
                已锁定 {forwardEval?.ai_screening_forward?.batches_count ?? 0} 批次 · {forwardEval?.ai_screening_forward?.samples_total ?? 0} 只样本
              </Badge>
            </div>
            {forwardEval?.ai_screening_forward && (
              <div className="space-y-4">
                <p className="text-xs text-zinc-400">{forwardEval.ai_screening_forward.note}</p>
                <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                  {(['green', 'yellow', 'red'] as const).map((light) => {
                    const g = forwardEval.ai_screening_forward.by_light[light]
                    const label = light === 'green' ? '🛡️ 绿灯精选池' : light === 'yellow' ? '⚠️ 黄灯留意池' : '⛔ 红灯否决池'
                    return (
                      <div key={light} className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3 text-xs">
                        <div className="flex items-center justify-between">
                          <span className="font-semibold text-white">{label}</span>
                          <Badge color={light === 'green' ? 'emerald' : light === 'red' ? 'red' : 'amber'}>
                            {g?.total_samples ?? 0} 样本
                          </Badge>
                        </div>
                        <div className="mt-2 grid grid-cols-2 gap-2 font-mono">
                          <div>
                            <div className="text-zinc-400">T+5 均值 / 胜率</div>
                            <div className="text-sm text-white">
                              {g?.t5?.avg_pct != null ? `${fmtPct(g.t5.avg_pct)} / ${g.t5.win_rate_pct}%` : '待满5日'}
                            </div>
                          </div>
                          <div>
                            <div className="text-zinc-400">T+10 均值 / 胜率</div>
                            <div className="text-sm text-white">
                              {g?.t10?.avg_pct != null ? `${fmtPct(g.t10.avg_pct)} / ${g.t10.win_rate_pct}%` : '待满10日'}
                            </div>
                          </div>
                        </div>
                      </div>
                    )
                  })}
                </div>
                <div className="max-h-96 overflow-auto">
                  <Table>
                    <TableHead>
                      <TableRow>
                        <TableHeader>决策日</TableHeader>
                        <TableHeader>标的</TableHeader>
                        <TableHeader>排雷灯 / 催化</TableHeader>
                        <TableHeader>基准价</TableHeader>
                        <TableHeader>已过交易日</TableHeader>
                        <TableHeader>最新 / T+5 / T+10</TableHeader>
                        <TableHeader>状态</TableHeader>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {forwardEval.ai_screening_forward.records.map((rec, i) => (
                        <TableRow key={`${rec.decision_date}-${rec.code}-${i}`}>
                          <TableCell className="font-mono text-xs text-zinc-400">{rec.decision_date}</TableCell>
                          <TableCell className="text-xs text-white">
                            {rec.name} <span className="font-mono text-zinc-400">{rec.code}</span>
                          </TableCell>
                          <TableCell>
                            <div className="flex items-center gap-1">
                              <Badge color={rec.risk_light === 'green' ? 'emerald' : rec.risk_light === 'red' ? 'red' : 'amber'}>
                                {rec.risk_light === 'green' ? '绿灯' : rec.risk_light === 'red' ? '红灯' : '黄灯'}
                              </Badge>
                              <Badge color="blue">{rec.catalyst_score}/5</Badge>
                            </div>
                          </TableCell>
                          <TableCell className="font-mono text-xs text-zinc-300">{rec.base_close ?? '—'}</TableCell>
                          <TableCell className="font-mono text-xs text-zinc-300">{rec.days_elapsed} 日</TableCell>
                          <TableCell className="font-mono text-xs">
                            <span className={pnlColor(rec.ret_latest_pct)}>{fmtPct(rec.ret_latest_pct)}</span>
                            {' / '}
                            <span className={pnlColor(rec.ret_5d_pct)}>{fmtPct(rec.ret_5d_pct)}</span>
                            {' / '}
                            <span className={pnlColor(rec.ret_10d_pct)}>{fmtPct(rec.ret_10d_pct)}</span>
                          </TableCell>
                          <TableCell>
                            <Badge color={rec.status === 'pending_t1' ? 'zinc' : 'blue'}>
                              {rec.status === 'pending_t1' ? '待T+1开盘' : rec.status === 'tracking' ? '追踪中' : '已定格'}
                            </Badge>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              </div>
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
