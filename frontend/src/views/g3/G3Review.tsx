import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import type * as echarts from 'echarts'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/catalyst/table'
import { EChart } from '@/components/EChart'
import { Card, EmptyState, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import { getG3AR45Review, type G3AR45ReviewResponse } from '@/api/g3'

const PERIOD_OPTIONS = [
  { label: '近 7 日', value: 7 },
  { label: '近 30 日', value: 30 },
  { label: '近 90 日', value: 90 },
]

const fmtYuan = (v?: number | null, signed = true): string => {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  const n = Number(v)
  const prefix = signed && n > 0 ? '+' : n < 0 ? '-' : ''
  return `${prefix}¥${Math.abs(n).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}`
}

const fmtPct = (v?: number | null, digits = 2): string => {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  const n = Number(v)
  return `${n > 0 ? '+' : ''}${n.toFixed(digits)}%`
}

export default function G3Review() {
  const [days, setDays] = useState<number>(30)
  const [data, setData] = useState<G3AR45ReviewResponse | null>(null)
  const [loading, setLoading] = useState<boolean>(false)
  const [error, setError] = useState<string>('')
  const [expandedDay, setExpandedDay] = useState<string | null>(null)

  const load = useCallback(async (targetDays: number) => {
    setLoading(true)
    setError('')
    try {
      const res = await getG3AR45Review(targetDays, 'acct1')
      setData(res)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '加载 AR4.5 实盘复盘与归因数据失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load(days)
  }, [days, load])

  const navChartOption = useMemo<echarts.EChartsCoreOption | null>(() => {
    const pts = data?.nav_curve || []
    if (pts.length < 2) return null
    const dates = pts.map((p) => p.date)
    return {
      animationDuration: 300,
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(15,23,42,0.95)',
        borderColor: 'rgba(148,163,184,0.3)',
        textStyle: { color: '#e2e8f0', fontSize: 12 },
      },
      legend: {
        top: 2,
        textStyle: { color: '#cbd5e1', fontSize: 11 },
        data: ['实盘净值 (ct1曾)', 'AR4.5-L3b 理想影子净值', '沪深300 基准'],
      },
      grid: { left: 52, right: 24, top: 36, bottom: 28 },
      xAxis: {
        type: 'category',
        data: dates,
        axisLabel: { color: '#94a3b8', fontSize: 10 },
        axisLine: { lineStyle: { color: 'rgba(148,163,184,0.2)' } },
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: { color: '#94a3b8', fontSize: 10, formatter: (v: number) => `${((v - 1) * 100).toFixed(1)}%` },
        splitLine: { lineStyle: { color: 'rgba(148,163,184,0.12)' } },
      },
      series: [
        {
          name: '实盘净值 (ct1曾)',
          type: 'line',
          data: pts.map((p) => p.actual_nav),
          smooth: 0.15,
          lineStyle: { width: 2.8, color: '#f59e0b' },
          itemStyle: { color: '#f59e0b' },
        },
        {
          name: 'AR4.5-L3b 理想影子净值',
          type: 'line',
          data: pts.map((p) => p.ar45_ideal_nav),
          smooth: 0.15,
          lineStyle: { width: 2, color: '#10b981', type: 'dashed' },
          itemStyle: { color: '#10b981' },
        },
        {
          name: '沪深300 基准',
          type: 'line',
          data: pts.map((p) => p.hs300_nav),
          smooth: 0.15,
          lineStyle: { width: 1.6, color: '#94a3b8', type: 'dotted' },
          itemStyle: { color: '#94a3b8' },
        },
      ],
    }
  }, [data])

  const kpis = data?.kpis
  const shadow = data?.shadow_reconcile
  const coach = data?.coach

  return (
    <div className="flex w-full flex-col gap-4">
      <PageHeader
        title="AR4.5-L3b 实盘复盘与真钱归因"
        subtitle={
          data
            ? `主力账号 ${data.account.label}（总资产 ${fmtYuan(data.account.total_assets, false)} · 现仓位 ${data.account.position_pct}%）· 真实盈亏归因 × Shadow 执行对账 × AI 交易教练`
            : '围绕主力账号真实资金、Shadow 计划单执行对账与三腿策略盈亏拆解的盘后复盘中心'
        }
        actions={
          <div className="flex flex-wrap items-center gap-2">
            {PERIOD_OPTIONS.map((opt) =>
              days === opt.value ? (
                <Button key={opt.value} onClick={() => setDays(opt.value)}>
                  {opt.label}
                </Button>
              ) : (
                <Button key={opt.value} outline onClick={() => setDays(opt.value)}>
                  {opt.label}
                </Button>
              )
            )}
            <Button outline disabled={loading} onClick={() => void load(days)}>
              {loading ? '刷新中…' : '刷新'}
            </Button>
            <Link to="/g3/desk" className="text-xs font-medium text-amber-300 hover:text-amber-200">
              返回实战交易台 →
            </Link>
          </div>
        }
      />

      {error && (
        <div className="rounded-lg border border-red-500/40 bg-red-950/30 px-4 py-2.5 text-sm text-red-200">
          {error}
        </div>
      )}

      {data && data.data_notes.length > 0 && (
        <div className="rounded-lg border border-sky-500/30 bg-sky-950/20 px-3.5 py-2 text-xs text-sky-200">
          {data.data_notes.map((n, i) => (
            <div key={i}>ℹ️ {n}</div>
          ))}
        </div>
      )}

      {/* 1. 4 枚真钱实盘复盘 KPI */}
      {kpis && (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatCard
            label={`ct1曾 实盘区间盈亏（${kpis.tracked_days} 交易日）`}
            value={fmtYuan(kpis.period_pnl_yuan)}
            tone={kpis.period_pnl_yuan >= 0 ? 'up' : 'down'}
            hint={`实盘 ${fmtPct(kpis.actual_ret_pct)} · 同期沪深300 ${fmtPct(kpis.hs300_ret_pct)} · 超额 ${fmtPct(kpis.excess_vs_hs300_pct)}`}
          />
          <StatCard
            label="Shadow 计划执行完成率"
            value={kpis.completion_pct != null ? `${kpis.completion_pct.toFixed(1)}%` : '待首日成交'}
            tone={kpis.completion_pct != null && kpis.completion_pct >= 80 ? 'up' : 'info'}
            hint={`均滑点 ${kpis.avg_slippage_bps != null ? `${kpis.avg_slippage_bps.toFixed(1)} bps` : '—'} · 已追踪 ${shadow?.days_tracked ?? 0} 天`}
          />
          <StatCard
            label="策略内浮盈 vs 旧底仓拖累"
            value={`${fmtYuan(kpis.in_strategy_floating_pnl)} / ${fmtYuan(kpis.legacy_drag_floating_pnl)}`}
            tone={kpis.legacy_drag_floating_pnl < -5000 ? 'down' : 'info'}
            hint={`左=AR4.5策略腿浮盈 · 右=历史旧底仓浮亏 · 做T净增厚 ${fmtYuan(kpis.t_trade_pnl)}`}
          />
          <StatCard
            label="风控与止损纪律得分"
            value={`${kpis.discipline_score} / 100`}
            tone={kpis.discipline_score >= 85 ? 'up' : kpis.discipline_score >= 65 ? 'info' : 'down'}
            hint={
              kpis.stop_violation_count > 0
                ? `⚠️ 仍有 ${kpis.stop_violation_count} 只旧票破 -10% 硬止损待清退`
                : '✅ 当前无破 -10% 硬止损违约持仓'
            }
          />
        </div>
      )}

      {/* 2. 实盘净值 vs AR4.5 理想影子曲线 vs 沪深300 */}
      {data && (
        <Card>
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <SectionTitle title="1. 实盘净值 vs AR4.5-L3b 理想影子曲线 vs 沪深300（检视执行剪刀差）" />
            <span className="font-mono text-[11px] text-zinc-400">
              按「当日真实盈亏 ÷ 前日总资产」累乘，自动剔除出入金干扰
            </span>
          </div>
          {navChartOption ? (
            <EChart option={navChartOption} height={290} />
          ) : (
            <div className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-4 text-xs text-zinc-300">
              <div className="font-semibold text-amber-300">
                📈 净值追踪已启动（当前累计 {data.nav_curve.length} 个交易日快照）
              </div>
              <p className="mt-1 text-zinc-400">
                每个交易日 15:15 自动采集主力账号真实资产后，满 2 个交易日起自动绘制「实盘净值 vs AR4.5-L3b 理想影子曲线 vs 沪深300」三线对比图。
              </p>
              {data.nav_curve.length === 1 && (
                <div className="mt-2 flex flex-wrap gap-4 font-mono text-[11px] text-zinc-200">
                  <span>起始基准日：{data.nav_curve[0].date}</span>
                  <span>总资产：{fmtYuan(data.nav_curve[0].total_assets, false)}</span>
                  <span>仓位：{data.nav_curve[0].position_pct}%</span>
                  <span>当日盈亏：{fmtYuan(data.nav_curve[0].daily_profit)}</span>
                </div>
              )}
            </div>
          )}
        </Card>
      )}

      {/* 3. 三大策略腿真钱持仓归因拆解 */}
      {data && (
        <Card>
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <SectionTitle title="2. 三大策略腿真钱持仓归因（一眼分清：是策略在亏，还是历史旧底仓在拖累）" />
            <Link to="/g3/accounts" className="text-xs text-sky-300 hover:text-sky-200">
              前往资金账号与做T处理旧底仓 →
            </Link>
          </div>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            {data.legs.map((leg) => (
              <div
                key={leg.leg_id}
                className={`rounded-lg border p-3 text-xs ${
                  leg.leg_id === 'legacy_and_t' && leg.count > 0
                    ? 'border-red-500/35 bg-red-950/15'
                    : 'border-zinc-800 bg-zinc-950/70'
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="font-semibold text-white">{leg.label}</span>
                  <Badge color={leg.leg_id === 'legacy_and_t' ? (leg.count > 0 ? 'red' : 'emerald') : 'blue'}>
                    {leg.count} 只
                  </Badge>
                </div>
                <div className="mt-2 grid grid-cols-3 gap-2 font-mono">
                  <div>
                    <div className="text-[10px] text-zinc-400">持仓市值</div>
                    <div className="text-zinc-100">{fmtYuan(leg.market_value, false)}</div>
                  </div>
                  <div>
                    <div className="text-[10px] text-zinc-400">当日盈亏</div>
                    <div className={leg.daily_profit >= 0 ? 'text-rose-400' : 'text-emerald-400'}>
                      {fmtYuan(leg.daily_profit)}
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] text-zinc-400">累计浮盈亏</div>
                    <div className={leg.floating_profit >= 0 ? 'text-rose-400' : 'text-emerald-400'}>
                      {fmtYuan(leg.floating_profit)}
                    </div>
                  </div>
                </div>
              </div>
            ))}
          </div>

          <div className="mt-3 overflow-x-auto">
            <Table dense>
              <TableHead>
                <TableRow>
                  <TableHeader>所属腿</TableHeader>
                  <TableHeader>标的</TableHeader>
                  <TableHeader>持仓股数 / 占比</TableHeader>
                  <TableHeader>成本 / 现价</TableHeader>
                  <TableHeader>当日盈亏</TableHeader>
                  <TableHeader>累计浮盈亏</TableHeader>
                  <TableHeader>归因判定</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {data.legs.flatMap((leg) =>
                  leg.items.map((item) => (
                    <TableRow key={`${leg.leg_id}-${item.code}`}>
                      <TableCell className="text-xs text-zinc-400">{leg.label}</TableCell>
                      <TableCell className="font-medium text-white">
                        {item.name} <span className="font-mono text-xs text-zinc-400">{item.code}</span>
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        {item.shares.toLocaleString()} 股 ({item.weight_pct}%)
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        {item.cost_price.toFixed(3)} / {item.current_price.toFixed(3)}
                      </TableCell>
                      <TableCell className={`font-mono text-xs ${item.daily_profit >= 0 ? 'text-rose-400' : 'text-emerald-400'}`}>
                        {fmtYuan(item.daily_profit)}
                      </TableCell>
                      <TableCell className={`font-mono text-xs ${item.floating_profit >= 0 ? 'text-rose-400' : 'text-emerald-400'}`}>
                        {fmtYuan(item.floating_profit)} ({fmtPct(item.profit_pct, 1)})
                      </TableCell>
                      <TableCell>
                        <Badge color={item.breached_stop ? 'red' : leg.leg_id === 'legacy_and_t' ? 'amber' : 'emerald'}>
                          {item.breached_stop ? '破-10%硬止损·须清退' : item.status_tag}
                        </Badge>
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </div>
        </Card>
      )}

      {/* 4. Shadow Ledger 逐日计划单 vs 实盘成交对账主表 */}
      {shadow && (
        <Card>
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <SectionTitle title="3. Shadow Ledger 逐日计划单 vs 实盘成交对账（漏单与滑点透明审计）" />
            <div className="flex items-center gap-3 font-mono text-xs text-zinc-400">
              <span>计划总笔数：{shadow.total_planned_orders}</span>
              <span>已执行：{shadow.total_executed_orders}</span>
              <span>均滑点：{shadow.avg_slippage_bps != null ? `${shadow.avg_slippage_bps} bps` : '—'}</span>
            </div>
          </div>
          {shadow.daily_records.length === 0 ? (
            <EmptyState text="暂无历史计划单对账记录：每次在交易台编译并执行 AR4.5-L3b 买卖单后，15:15 同步同花顺回执将自动生成逐笔滑点与完成率审计。" />
          ) : (
            <div className="space-y-2">
              {shadow.daily_records.map((rec) => {
                const open = expandedDay === rec.date
                return (
                  <div key={rec.date} className="rounded-lg border border-zinc-800 bg-zinc-950/70 p-2.5 text-xs">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex items-center gap-2 font-mono">
                        <span className="font-bold text-white">{rec.date}</span>
                        <Badge color="zinc">{rec.regime}</Badge>
                        <span className="text-zinc-300">
                          执行 {rec.executed_orders}/{rec.planned_orders} 笔（完成率 {rec.completion_pct}%）
                        </span>
                      </div>
                      <button
                        type="button"
                        onClick={() => setExpandedDay(open ? null : rec.date)}
                        className="text-sky-300 hover:underline"
                      >
                        {open ? '收起逐笔明细 ▲' : '展开逐笔明细 ▼'}
                      </button>
                    </div>
                    {open && rec.items.length > 0 && (
                      <div className="mt-2 overflow-x-auto border-t border-zinc-800 pt-2">
                        <Table dense>
                          <TableHead>
                            <TableRow>
                              <TableHeader>方向</TableHeader>
                              <TableHeader>标的</TableHeader>
                              <TableHeader>计划股数 / 价</TableHeader>
                              <TableHeader>实盘股数 / 价</TableHeader>
                              <TableHeader>滑点(bps)</TableHeader>
                              <TableHeader>状态</TableHeader>
                            </TableRow>
                          </TableHead>
                          <TableBody>
                            {rec.items.map((it, idx) => (
                              <TableRow key={`${rec.date}-${it.code}-${idx}`}>
                                <TableCell>
                                  <Badge color={it.side === 'SELL' ? 'red' : 'emerald'}>{it.side}</Badge>
                                </TableCell>
                                <TableCell className="text-white">
                                  {it.name} <span className="font-mono text-zinc-400">{it.code}</span>
                                </TableCell>
                                <TableCell className="font-mono">
                                  {it.plan_shares} @ ¥{it.plan_price}
                                </TableCell>
                                <TableCell className="font-mono">
                                  {it.actual_shares} @ {it.actual_price != null ? `¥${it.actual_price}` : '—'}
                                </TableCell>
                                <TableCell className="font-mono">
                                  {it.slippage_bps != null ? `${it.slippage_bps}` : '—'}
                                </TableCell>
                                <TableCell>
                                  <Badge color={it.status === 'executed' ? 'emerald' : 'amber'}>{it.status}</Badge>
                                </TableCell>
                              </TableRow>
                            ))}
                          </TableBody>
                        </Table>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </Card>
      )}

      {/* 5. AI 交易教练 1 屏行动复盘 */}
      {coach && (
        <Card>
          <SectionTitle title="4. AI 交易教练复盘与下周行为红线" />
          <div className="mt-2.5 grid grid-cols-1 gap-4 lg:grid-cols-12 text-xs">
            <div className="lg:col-span-7 space-y-2.5">
              {coach.agent_notes?.headline ? (
                <div className="rounded-lg border border-blue-500/35 bg-blue-950/20 p-3">
                  <div className="font-semibold text-blue-200">🤖 {coach.agent_notes.headline}</div>
                  <p className="mt-1 leading-relaxed text-zinc-200">{coach.agent_notes.weekly_diagnosis}</p>
                </div>
              ) : (
                <div className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3 text-zinc-400">
                  每日 17:45 盘后复盘任务会自动生成围绕 AR4.5-L3b 纪律的 AI 深度点评。
                </div>
              )}

              {(coach.bleeding_points || []).length > 0 && (
                <div className="space-y-1.5">
                  <div className="font-semibold text-zinc-200">出血点诊断：</div>
                  {coach.bleeding_points.map((bp, i) => (
                    <div key={i} className="rounded-md border border-red-500/30 bg-red-950/15 px-3 py-2">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="font-semibold text-red-200">{bp.title}</span>
                        {bp.impact_estimate != null && (
                          <span className="font-mono text-xs text-amber-200">
                            影响估算：{fmtYuan(bp.impact_estimate)}
                          </span>
                        )}
                      </div>
                      <div className="mt-0.5 text-zinc-300">{bp.detail}</div>
                      {bp.dates?.length > 0 && (
                        <div className="mt-0.5 font-mono text-[11px] text-zinc-400">
                          涉及日期：{bp.dates.join('、')}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="lg:col-span-5 rounded-lg border border-amber-500/30 bg-amber-950/15 p-3">
              <div className="font-semibold text-amber-200">🚧 下周必须守住的行为红线</div>
              <ul className="mt-2 space-y-1.5 text-zinc-200">
                {(coach.agent_notes?.red_lines?.length ? coach.agent_notes.red_lines : coach.next_week_red_lines || []).map(
                  (line, idx) => (
                    <li key={idx} className="leading-relaxed">
                      {idx + 1}. {line}
                    </li>
                  )
                )}
              </ul>
            </div>
          </div>
        </Card>
      )}
    </div>
  )
}
