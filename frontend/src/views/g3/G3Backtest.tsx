import { useCallback, useEffect, useState } from 'react'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/catalyst/table'
import { Card, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import {
  getG3BacktestReport,
  rebuildG3BacktestReport,
  type G3BacktestCurvePoint,
  type G3HistoricalBacktestReport,
} from '@/api/g3'

const fmtSigned = (v?: number | null, digits = 2, suffix = '%'): string => {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  const n = Number(v)
  return `${n > 0 ? '+' : ''}${n.toFixed(digits)}${suffix}`
}

const SERIES_CFG: Array<{
  key: keyof Pick<G3BacktestCurvePoint, 'ar45_l3b' | 'ar45_l4' | 'ar3_dual_ab' | 'ar3_etf01' | 'core_sat_aw' | 'pure_etf_aw' | 'hs300' | 'retired_mw04'>
  label: string
  color: string
  width: number
  dashed?: boolean
}> = [
  { key: 'ar45_l3b', label: '★ AR4.5-L3b 现役主策略(国家队+Top2逆波+南向)', color: '#f59e0b', width: 3.4 },
  { key: 'ar45_l4', label: '◆ AR4.5-L4 可选低回撤(赚钱效应自动切)', color: '#10b981', width: 2.5 },
  { key: 'ar3_dual_ab', label: '历史演进 · AR3 v3.0(固定50%+#01)', color: '#38bdf8', width: 1.9, dashed: true },
  { key: 'ar3_etf01', label: '历史演进 · AR3 #01 纯ETF', color: '#22d3ee', width: 1.5, dashed: true },
  { key: 'core_sat_aw', label: '历史演进 · AR1 v2.1 核心卫星', color: '#a78bfa', width: 1.3, dashed: true },
  { key: 'pure_etf_aw', label: '历史演进 · AR1 v2.1 纯ETF', color: '#60a5fa', width: 1.2, dashed: true },
  { key: 'hs300', label: '沪深300 满仓基准', color: '#9ca3af', width: 1.6, dashed: true },
  { key: 'retired_mw04', label: '已淘汰对照组(旧追高5日死止盈)', color: '#ef4444', width: 1.3, dashed: true },
]

function BacktestNavChart({
  points,
  splits,
}: {
  points: G3BacktestCurvePoint[]
  splits: G3HistoricalBacktestReport['splits']
}) {
  if (points.length < 2) return null

  const W = 1100
  const H = 340
  const padL = 54
  const padR = 24
  const padT = 22
  const padB = 34
  const plotW = W - padL - padR
  const plotH = H - padT - padB

  let minV = Infinity
  let maxV = -Infinity
  for (const p of points) {
    for (const s of SERIES_CFG) {
      const v = Number(p[s.key])
      if (Number.isFinite(v)) {
        if (v < minV) minV = v
        if (v > maxV) maxV = v
      }
    }
  }
  const margin = Math.max(0.05, (maxV - minV) * 0.08)
  minV = Math.max(0.3, minV - margin)
  maxV = maxV + margin

  const xOf = (idx: number) => padL + (idx / Math.max(1, points.length - 1)) * plotW
  const yOf = (val: number) => padT + ((maxV - val) / Math.max(1e-6, maxV - minV)) * plotH

  const findDateIdx = (d: string) => {
    const found = points.findIndex((p) => p.date >= d)
    return found >= 0 ? found : points.length - 1
  }
  const valStartIdx = findDateIdx(splits.val.from)
  const oosStartIdx = findDateIdx(splits.oos.from)

  const yTicks = 5
  const gridYs = Array.from({ length: yTicks + 1 }, (_, i) => minV + ((maxV - minV) * i) / yTicks)

  return (
    <div className="w-full overflow-x-auto">
      <div className="mb-2 flex flex-wrap items-center gap-4 text-xs">
        {SERIES_CFG.map((s) => (
          <span key={s.key} className="inline-flex items-center gap-1.5 text-zinc-200">
            <span
              className="inline-block h-2.5 w-5 rounded-sm"
              style={{
                backgroundColor: s.dashed ? 'transparent' : s.color,
                borderTop: s.dashed ? `2px dashed ${s.color}` : 'none',
              }}
            />
            {s.label}
          </span>
        ))}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full min-w-[760px] rounded-lg border border-zinc-800 bg-zinc-950/80">
        {/* OOS 盲测区间高亮底色 */}
        <rect
          x={xOf(oosStartIdx)}
          y={padT}
          width={W - padR - xOf(oosStartIdx)}
          height={plotH}
          fill="rgba(59, 130, 246, 0.07)"
        />
        {/* 网格线 */}
        {gridYs.map((yv, i) => (
          <g key={i}>
            <line x1={padL} y1={yOf(yv)} x2={W - padR} y2={yOf(yv)} stroke="#27272a" strokeWidth={1} />
            <text x={padL - 8} y={yOf(yv) + 4} textAnchor="end" fill="#a1a1aa" fontSize="11" fontFamily="monospace">
              {yv.toFixed(2)}
            </text>
          </g>
        ))}
        {/* 净值 1.00 基准虚线 */}
        {minV < 1.0 && maxV > 1.0 && (
          <line x1={padL} y1={yOf(1.0)} x2={W - padR} y2={yOf(1.0)} stroke="#52525b" strokeWidth={1} strokeDasharray="4 4" />
        )}
        {/* IS / Val / OOS 切分竖线 */}
        <line x1={xOf(valStartIdx)} y1={padT} x2={xOf(valStartIdx)} y2={H - padB} stroke="#f59e0b" strokeWidth={1} strokeDasharray="4 3" />
        <text x={xOf(valStartIdx) + 4} y={padT + 12} fill="#fbbf24" fontSize="10">Val验证起({splits.val.from})</text>
        <line x1={xOf(oosStartIdx)} y1={padT} x2={xOf(oosStartIdx)} y2={H - padB} stroke="#38bdf8" strokeWidth={1.2} strokeDasharray="4 3" />
        <text x={xOf(oosStartIdx) + 4} y={padT + 12} fill="#38bdf8" fontSize="10">OOS盲测起({splits.oos.from})</text>

        {/* 多策略净值折线 */}
        {SERIES_CFG.map((s) => {
          const validPts = points
            .map((p, idx) => ({ idx, val: Number(p[s.key]) }))
            .filter((x) => Number.isFinite(x.val))
          if (validPts.length < 2) return null
          const dStr = validPts
            .map((pt, j) => `${j === 0 ? 'M' : 'L'}${xOf(pt.idx).toFixed(1)},${yOf(pt.val).toFixed(1)}`)
            .join(' ')
          return (
            <path
              key={s.key}
              d={dStr}
              fill="none"
              stroke={s.color}
              strokeWidth={s.width}
              strokeDasharray={s.dashed ? '5 4' : undefined}
            />
          )
        })}

        {/* 底部时间刻度 */}
        {[0, Math.floor(points.length * 0.25), Math.floor(points.length * 0.5), Math.floor(points.length * 0.75), points.length - 1].map((idx) => (
          <text
            key={idx}
            x={xOf(idx)}
            y={H - 10}
            textAnchor="middle"
            fill="#a1a1aa"
            fontSize="11"
            fontFamily="monospace"
          >
            {points[idx]?.date}
          </text>
        ))}
      </svg>
    </div>
  )
}

export default function G3Backtest() {
  const [report, setReport] = useState<G3HistoricalBacktestReport | null>(null)
  const [loading, setLoading] = useState(false)
  const [rebuilding, setRebuilding] = useState(false)
  const [error, setError] = useState('')
  const [msg, setMsg] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const res = await getG3BacktestReport()
      setReport(res)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '加载历史回测报告失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const handleRebuild = async () => {
    setRebuilding(true)
    setError('')
    setMsg('')
    try {
      const res = await rebuildG3BacktestReport()
      setReport(res)
      setMsg(`已完成全市场单账户零杠杆历史回测重算（更新于 ${res.generated_at}）`)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '重跑回测失败')
    } finally {
      setRebuilding(false)
    }
  }

  const ar45L3b = report?.comparisons.find((c) => c.id === 'ar45_l3b_champion')
  const ar45L4 = report?.comparisons.find((c) => c.id === 'ar45_l4_lowdd')
  const ar3Dual = report?.comparisons.find((c) => c.id === 'ar3_v3_dual_core_ab_01')
  const hs300 = report?.comparisons.find((c) => c.id === 'bench_hs300')
  const primaryStrategy = ar45L3b ?? ar3Dual ?? report?.comparisons.find((c) => c.id === 'ar1_v2_1_allweather')
  const secondaryStrategy = ar45L4 ?? ar3Dual ?? report?.comparisons.find((c) => c.id === 'ar1_v2_1_pure_etf_allweather')

  const catBadge = (cat: string, id: string) => {
    if (id === 'ar45_l3b_champion') return { label: '★ AR4.5-L3b 现役主策略', color: 'amber' as const }
    if (id === 'ar45_l4_lowdd') return { label: '◆ AR4.5-L4 可选低回撤', color: 'emerald' as const }
    if (id.startsWith('ar3_')) return { label: 'AR3 历史演进对照', color: 'blue' as const }
    if (cat === 'legacy_combo' || id.startsWith('ar1_v2_1_')) return { label: 'AR1 历史演进对照', color: 'zinc' as const }
    if (cat === 'active_combo') return { label: '现役全天候', color: 'emerald' as const }
    if (cat === 'stage_strategy') return { label: '阶段独立策略', color: 'blue' as const }
    if (cat === 'benchmark') return { label: '市场基准', color: 'zinc' as const }
    return { label: '已淘汰反面教材', color: 'red' as const }
  }

  return (
    <div className="flex w-full flex-col gap-5">
      <PageHeader
        title="G3 历史回测对比与防幻觉验真（AR4.5-L3b 现役主策略）"
        subtitle={
          report
            ? `回测区间 ${report.period.start} ~ ${report.period.end}（${report.period.trade_days} 交易日 · 5阶段切换 ${report.period.regime_switches} 次）· AR4.5-L3b / AR4.5-L4 现役物理单账户零杠杆实证 × AR3 / AR1 历史演进对照 × 6 项防未来函数审计`
            : 'AR4.5-L3b 现役主策略 × AR4.5-L4 低回撤版 × AR3 / AR1 历史演进三段盲测对比 × 6 项防未来函数审计'
        }
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Badge color="amber">★ 现役主策略 · AR4.5-L3b</Badge>
            <Badge color="emerald">零未来函数 T-1→T+1</Badge>
            <Badge color="blue">单账户总仓位 ≤ 100%</Badge>
            <Button outline onClick={() => void load()} disabled={loading}>
              {loading ? '加载中…' : '刷新'}
            </Button>
            <Button onClick={() => void handleRebuild()} disabled={rebuilding}>
              {rebuilding ? '重算全市场回测中…' : '重跑全量回测'}
            </Button>
          </div>
        }
      />

      {error && (
        <div className="rounded-lg border border-red-500/40 bg-red-950/30 px-4 py-2.5 text-sm text-red-200">{error}</div>
      )}
      {msg && (
        <div className="rounded-lg border border-emerald-500/40 bg-emerald-950/30 px-4 py-2.5 text-sm text-emerald-200">{msg}</div>
      )}

      {report && (
        <>
          {/* 核心顶栏对比卡（AR4.5-L3b 现役主策略为主角，保留 AR3 演进对照） */}
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <StatCard
              label="★ AR4.5-L3b 现役主策略"
              value={fmtSigned(primaryStrategy?.full.cum_ret_pct, 1)}
              tone="up"
              hint={`年化 ${fmtSigned(primaryStrategy?.full.ann_ret_pct, 1)} · 回撤 ${primaryStrategy?.full.max_dd_pct}% · 卡玛 ${primaryStrategy?.full.calmar} · 夏普 ${primaryStrategy?.full.sharpe}`}
            />
            <StatCard
              label="◆ AR4.5-L4 可选低回撤版"
              value={fmtSigned(secondaryStrategy?.full.cum_ret_pct, 1)}
              tone="info"
              hint={`年化 ${fmtSigned(secondaryStrategy?.full.ann_ret_pct, 1)} · 回撤 ${secondaryStrategy?.full.max_dd_pct}% · 卡玛 ${secondaryStrategy?.full.calmar}`}
            />
            <StatCard
              label="历史演进 · AR3 v3.0（升级前）"
              value={fmtSigned(ar3Dual?.full.cum_ret_pct, 1)}
              hint={`年化 ${fmtSigned(ar3Dual?.full.ann_ret_pct, 1)} · 回撤 ${ar3Dual?.full.max_dd_pct}% · 卡玛 ${ar3Dual?.full.calmar}（vs 沪深300 ${fmtSigned(hs300?.full.cum_ret_pct, 1)}）`}
            />
            <StatCard
              label="2026 熊市盲测超额（AR4.5-L3b vs 沪深300）"
              value={fmtSigned((primaryStrategy?.segments.oos.cum_ret_pct ?? 0) - (hs300?.segments.oos.cum_ret_pct ?? 0), 1)}
              tone="up"
              hint={`L3b OOS ${fmtSigned(primaryStrategy?.segments.oos.cum_ret_pct, 1)} · OOS回撤仅 ${primaryStrategy?.segments.oos.max_dd_pct}%（沪深300 OOS ${fmtSigned(hs300?.segments.oos.cum_ret_pct, 1)}）`}
            />
          </div>

          {/* 1. 大盘 5 阶段切换标准与实证统计 */}
          <Card>
            <SectionTitle title={`1. AR4.5-L3b 大盘五阶段精确切换标准与历史 ${report.period.trade_days} 交易日实证统计`} />
            <div className="mt-2 overflow-x-auto">
              <Table dense>
                <TableHead>
                  <TableRow>
                    <TableHeader>大盘阶段</TableHeader>
                    <TableHeader>进入判定标准（严格 T 日收盘已知）</TableHeader>
                    <TableHeader>阶段切换 / 退出路径</TableHeader>
                    <TableHeader>确认窗 / 水位上限</TableHeader>
                    <TableHeader>历史天数 & 未来5/10日市场实证</TableHeader>
                    <TableHeader>该阶段生效策略</TableHeader>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {report.five_regime_rules.map((r) => (
                    <TableRow key={r.phase}>
                      <TableCell className="whitespace-nowrap text-xs">
                        <Badge color={r.tone === 'emerald' ? 'emerald' : r.tone === 'red' ? 'red' : 'amber'}>
                          {r.label}
                        </Badge>
                      </TableCell>
                      <TableCell className="min-w-[240px] max-w-sm whitespace-normal break-words text-xs leading-relaxed text-zinc-200">
                        {r.enter_rule}
                      </TableCell>
                      <TableCell className="min-w-[210px] max-w-xs whitespace-normal break-words text-xs leading-relaxed text-zinc-400">
                        {r.exit_switch}
                      </TableCell>
                      <TableCell className="whitespace-nowrap font-mono text-xs">
                        <div className="text-zinc-200">{r.confirm_days}</div>
                        <div className="mt-0.5 text-blue-300">上限 ≤ {r.position_cap_pct}%</div>
                      </TableCell>
                      <TableCell className="whitespace-nowrap font-mono text-xs">
                        <div className="text-zinc-100">{r.hist_days} 天（{r.hist_pct}%）</div>
                        <div className={r.fwd_5d_avg_pct >= 0 ? 'text-rose-400' : 'text-emerald-400'}>
                          5日 {fmtSigned(r.fwd_5d_avg_pct)}（胜率 {r.fwd_5d_win_pct}%）
                        </div>
                        <div className="text-[11px] text-zinc-400">10日 {fmtSigned(r.fwd_10d_avg_pct)}</div>
                      </TableCell>
                      <TableCell className="min-w-[230px] max-w-sm whitespace-normal break-words text-xs leading-relaxed text-zinc-300">
                        {r.active_strategy}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </Card>

          {/* 2. 多策略历史净值曲线可视化对比 */}
          <Card>
            <SectionTitle title="2. 基准 vs 各阶段选股策略历史净值曲线（含 IS / Val / OOS 盲测分界）" />
            <div className="mt-2">
              <BacktestNavChart points={report.curve_points} splits={report.splits} />
            </div>
          </Card>

          {/* 3. 基准与阶段策略三段量化指标对比表 */}
          <Card>
            <SectionTitle title="3. 基准 vs 阶段选股策略全周期与三段（IS / Val / OOS）硬核对比" />
            <div className="mt-2 overflow-x-auto">
              <Table dense>
                <TableHead>
                  <TableRow>
                    <TableHeader>分类</TableHeader>
                    <TableHeader>策略 / 基准名称与逻辑</TableHeader>
                    <TableHeader>全样本累计 / 年化</TableHeader>
                    <TableHeader>超额(vs沪深300)</TableHeader>
                    <TableHeader>最大回撤 / 卡玛 / 夏普</TableHeader>
                    <TableHeader>IS训练({report.splits.is.from.slice(2)}~{report.splits.is.to.slice(2)})</TableHeader>
                    <TableHeader>Val牛市({report.splits.val.from.slice(2)}~{report.splits.val.to.slice(2)})</TableHeader>
                    <TableHeader>OOS熊市盲测({report.splits.oos.from.slice(2)}~{report.splits.oos.to.slice(2)})</TableHeader>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {report.comparisons.map((row) => {
                    const cb = catBadge(row.category, row.id)
                    return (
                      <TableRow key={row.id} className={row.id.startsWith('ar3_') ? 'bg-amber-500/5' : undefined}>
                        <TableCell className="whitespace-nowrap">
                          <Badge color={cb.color}>{cb.label}</Badge>
                        </TableCell>
                        <TableCell className="min-w-[240px] max-w-sm whitespace-normal break-words text-xs">
                          <div className="font-semibold text-white">{row.name}</div>
                          <div className="mt-0.5 text-[11px] text-zinc-400">{row.role}</div>
                        </TableCell>
                        <TableCell className="whitespace-nowrap font-mono text-xs">
                          <div className={row.full.cum_ret_pct >= 0 ? 'font-semibold text-rose-400' : 'text-emerald-400'}>
                            {fmtSigned(row.full.cum_ret_pct, 1)}
                          </div>
                          <div className="text-[11px] text-zinc-400">年化 {fmtSigned(row.full.ann_ret_pct, 1)}</div>
                        </TableCell>
                        <TableCell className="whitespace-nowrap font-mono text-xs">
                          <span className={(row.full.excess_vs_hs300_pct ?? 0) >= 0 ? 'text-blue-300 font-semibold' : 'text-zinc-500'}>
                            {fmtSigned(row.full.excess_vs_hs300_pct, 1)}
                          </span>
                        </TableCell>
                        <TableCell className="whitespace-nowrap font-mono text-xs text-zinc-300">
                          <div>回撤 {row.full.max_dd_pct.toFixed(1)}%</div>
                          <div className="text-[11px] text-zinc-400">
                            卡玛 {row.full.calmar.toFixed(2)} · 夏普 {row.full.sharpe.toFixed(2)}
                          </div>
                        </TableCell>
                        <TableCell className="whitespace-nowrap font-mono text-xs">
                          <div className={row.segments.is.cum_ret_pct >= 0 ? 'text-rose-400' : 'text-emerald-400'}>
                            {fmtSigned(row.segments.is.cum_ret_pct, 1)}
                          </div>
                          <div className="text-[11px] text-zinc-400">DD {row.segments.is.max_dd_pct.toFixed(1)}%</div>
                        </TableCell>
                        <TableCell className="whitespace-nowrap font-mono text-xs">
                          <div className={row.segments.val.cum_ret_pct >= 0 ? 'text-rose-400' : 'text-emerald-400'}>
                            {fmtSigned(row.segments.val.cum_ret_pct, 1)}
                          </div>
                          <div className="text-[11px] text-zinc-400">DD {row.segments.val.max_dd_pct.toFixed(1)}%</div>
                        </TableCell>
                        <TableCell className="whitespace-nowrap font-mono text-xs">
                          <div className={row.segments.oos.cum_ret_pct >= 0 ? 'font-semibold text-rose-400' : 'text-emerald-400'}>
                            {fmtSigned(row.segments.oos.cum_ret_pct, 1)}
                          </div>
                          <div className="text-[11px] text-zinc-400">DD {row.segments.oos.max_dd_pct.toFixed(1)}%</div>
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            </div>
          </Card>

          {/* 4. 防未来函数与防回测幻觉 6 项硬核审计清单 */}
          <Card className="border-amber-500/35 bg-zinc-950/70">
            <SectionTitle title="4. 防未来函数与防回测幻觉 6 项代码级审计清单（绝不自欺欺人）" />
            <div className="mt-2 grid grid-cols-1 gap-3 lg:grid-cols-2">
              {report.anti_hallucination_audit.map((item) => (
                <div
                  key={item.id}
                  className={`rounded-lg border p-3 text-xs ${
                    item.status === 'PASS'
                      ? 'border-emerald-500/30 bg-emerald-950/15'
                      : 'border-amber-500/40 bg-amber-950/20'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-semibold text-white">{item.title}</span>
                    <Badge color={item.status === 'PASS' ? 'emerald' : 'amber'}>
                      {item.status === 'PASS' ? '✅ 审计通过' : '⚠️ 透明披露局限'}
                    </Badge>
                  </div>
                  <p className="mt-1.5 leading-relaxed text-zinc-300">{item.detail}</p>
                </div>
              ))}
            </div>
          </Card>
        </>
      )}
    </div>
  )
}
