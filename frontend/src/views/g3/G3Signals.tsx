import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/catalyst/table'
import { Card, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import AR3DualCorePanel from '@/views/g3/AR3DualCorePanel'
import {
  getG3AR3Radar,
  getG3Desk,
  getG3ReviewForwardEval,
  type G3AR3DualCoreRadarResponse,
  type G3DeskResponse,
  type G3ForwardEvalResponse,
} from '@/api/g3'

type SignalsTab = 'radar' | 'screening'
type LightFilter = 'all' | 'red' | 'yellow' | 'green'

interface UnifiedScreeningRow {
  code: string
  name: string
  group: 'holding' | 'f2a' | 'f3a'
  groupLabel: string
  industry: string
  risk_light: string
  catalyst_score: number | null
  risk_flags: string[]
  thesis: string
  extraMetric: string
}

const lightBadge = (light?: string) => {
  if (light === 'red') return { label: '红灯禁买', color: 'red' as const }
  if (light === 'yellow') return { label: '黄灯慎买', color: 'amber' as const }
  if (light === 'green') return { label: '绿灯通过', color: 'emerald' as const }
  return { label: '待评估', color: 'zinc' as const }
}

const fmtSignedPct = (v?: number | null): string => {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  const n = Number(v)
  return `${n > 0 ? '+' : ''}${n.toFixed(2)}%`
}

export default function G3Signals() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const activeTab: SignalsTab = searchParams.get('tab') === 'screening' ? 'screening' : 'radar'

  const [radar, setRadar] = useState<G3AR3DualCoreRadarResponse | null>(null)
  const [desk, setDesk] = useState<G3DeskResponse | null>(null)
  const [forwardEval, setForwardEval] = useState<G3ForwardEvalResponse | null>(null)
  const [loadingScreening, setLoadingScreening] = useState(false)
  const [lightFilter, setLightFilter] = useState<LightFilter>('all')
  const [error, setError] = useState('')

  const switchTab = (next: SignalsTab) => {
    const params = new URLSearchParams(searchParams)
    if (next === 'radar') {
      params.delete('tab')
    } else {
      params.set('tab', next)
    }
    setSearchParams(params, { replace: true })
  }

  const loadScreening = useCallback(async () => {
    setLoadingScreening(true)
    setError('')
    try {
      const [r, d, f] = await Promise.all([
        getG3AR3Radar(false).catch(() => null),
        getG3Desk(false).catch(() => null),
        getG3ReviewForwardEval(60).catch(() => null),
      ])
      if (r) setRadar(r)
      if (d) setDesk(d)
      if (f) setForwardEval(f)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '加载 AI 排雷档案失败')
    } finally {
      setLoadingScreening(false)
    }
  }, [])

  useEffect(() => {
    if (activeTab === 'screening') {
      void loadScreening()
    }
  }, [activeTab, loadScreening])

  const screeningRows = useMemo<UnifiedScreeningRow[]>(() => {
    const rows: UnifiedScreeningRow[] = []
    const seen = new Set<string>()

    // 1. 当前持仓 AI 风险哨兵（最高优先级）
    for (const h of desk?.trading_command?.holding_actions || []) {
      const key = `holding_${h.code}`
      if (seen.has(key)) continue
      seen.add(key)
      rows.push({
        code: h.code,
        name: h.name,
        group: 'holding',
        groupLabel: '实盘持仓',
        industry: '—',
        risk_light: h.ai_risk_light || 'unknown',
        catalyst_score: h.ai_catalyst_score ?? null,
        risk_flags: h.broken_stop ? ['已破止损', ...h.reasons.slice(0, 2)] : h.reasons.slice(0, 2),
        thesis: h.ai_thesis || h.verdict_label || '—',
        extraMetric: h.profit_pct != null ? `浮盈 ${fmtSignedPct(h.profit_pct)}` : '—',
      })
    }

    // 2. AR4.6 候选股（F3A + F2A）
    const addStockList = (list: G3AR3DualCoreRadarResponse['stock_radar']['f2a_candidates'], grp: 'f2a' | 'f3a', grpLabel: string) => {
      for (const s of list || []) {
        const key = `${grp}_${s.code}`
        if (seen.has(key)) continue
        seen.add(key)
        rows.push({
          code: s.code,
          name: s.name,
          group: grp,
          groupLabel: grpLabel,
          industry: s.industry || '—',
          risk_light: s.ai_risk_light || 'unknown',
          catalyst_score: s.ai_catalyst_score ?? null,
          risk_flags: s.ai_risk_flags || [],
          thesis: s.ai_thesis || `综合分 ${s.score ?? '—'}`,
          extraMetric: s.net_profit_yoy_pct != null ? `净利同比 ${fmtSignedPct(s.net_profit_yoy_pct)}` : `评分 ${s.score ?? '—'}`,
        })
      }
    }
    addStockList(radar?.stock_radar?.f3a_candidates, 'f3a', 'F3A 绩优回踩')
    addStockList(radar?.stock_radar?.f2a_candidates, 'f2a', 'F2A 业绩主升')

    const order: Record<string, number> = { red: 0, yellow: 1, green: 2, unknown: 3 }
    return rows.sort((a, b) => (order[a.risk_light] ?? 9) - (order[b.risk_light] ?? 9))
  }, [desk, radar])

  const filteredRows = useMemo(() => {
    if (lightFilter === 'all') return screeningRows
    return screeningRows.filter((r) => r.risk_light === lightFilter)
  }, [screeningRows, lightFilter])

  const lightCounts = useMemo(() => {
    let red = 0
    let yellow = 0
    let green = 0
    for (const r of screeningRows) {
      if (r.risk_light === 'red') red += 1
      else if (r.risk_light === 'yellow') yellow += 1
      else if (r.risk_light === 'green') green += 1
    }
    return { red, yellow, green, total: screeningRows.length }
  }, [screeningRows])

  const fwd = forwardEval?.ai_screening_forward

  return (
    <div className="flex w-full flex-col gap-4">
      <PageHeader
        title="AR4.6 选股与排雷雷达中心"
        subtitle="14 只跨资产全天候 ETF 深度全景 × F2A/F3A 业绩双核选股队列 × AI 红黄绿灯排雷档案与前向胜率验真"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Button outline onClick={() => navigate('/g3/desk')}>
              ⚡ 前往实战交易台预填 →
            </Button>
            <Link to="/g3/backtest">
              <Button plain>📈 历史回测验真</Button>
            </Link>
          </div>
        }
      />

      {/* 双 Tab 切换 */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-zinc-800 pb-3">
        <div className="inline-flex rounded-xl border border-zinc-800 bg-zinc-900/80 p-1">
          <button
            type="button"
            onClick={() => switchTab('radar')}
            className={`rounded-lg px-4 py-1.5 text-xs font-semibold transition ${
              activeTab === 'radar'
                ? 'bg-blue-600 text-white shadow-sm'
                : 'text-zinc-400 hover:text-zinc-100'
            }`}
          >
            📡 AR4.6 双核雷达（14只ETF + F2A/F3A）
          </button>
          <button
            type="button"
            onClick={() => switchTab('screening')}
            className={`rounded-lg px-4 py-1.5 text-xs font-semibold transition ${
              activeTab === 'screening'
                ? 'bg-blue-600 text-white shadow-sm'
                : 'text-zinc-400 hover:text-zinc-100'
            }`}
          >
            🚨 AI 排雷雷区档案与胜率验真
          </button>
        </div>

        {activeTab === 'screening' && (
          <Button outline onClick={() => void loadScreening()} disabled={loadingScreening}>
            {loadingScreening ? '刷新中…' : '刷新排雷档案'}
          </Button>
        )}
      </div>

      {activeTab === 'radar' ? (
        <AR3DualCorePanel
          mode="signals_full"
          onPrefillOrder={() => {
            navigate('/g3/desk')
          }}
        />
      ) : (
        <div className="flex flex-col gap-5">
          {error && (
            <div className="rounded-lg border border-red-500/40 bg-red-500/10 px-4 py-2.5 text-sm text-red-200">
              {error}
            </div>
          )}

          {/* KPI 概览 */}
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <StatCard
              label="🔴 红灯一票否决"
              value={`${lightCounts.red} 只`}
              hint="存在业绩爆雷 / 减持 / 监管风险，严禁买入"
            />
            <StatCard
              label="🟡 黄灯谨慎观察"
              value={`${lightCounts.yellow} 只`}
              hint="需核对公告或等催化确认"
            />
            <StatCard
              label="🟢 绿灯排雷通过"
              value={`${lightCounts.green} 只`}
              hint="基本面与公告无明显雷点"
            />
            <StatCard
              label="前向 5 日绿灯 vs 红灯超额"
              value={
                fwd?.alpha_green_vs_red_5d_pct != null
                  ? fmtSignedPct(fwd.alpha_green_vs_red_5d_pct)
                  : '累计追踪中'
              }
              hint={fwd ? `已追踪 ${fwd.batches_count} 批次 · ${fwd.samples_total} 条样本` : '盘后 17:45 Sidecar 自动沉淀'}
            />
          </div>

          {/* 1. 实盘持仓 + AR4.6 候选股实时排雷明细 */}
          <Card>
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <SectionTitle title={`1. 实时持仓与 AR4.6 候选股 AI 排雷明细（共 ${filteredRows.length} 只）`} />
              <div className="flex flex-wrap items-center gap-1.5 text-xs">
                {(['all', 'red', 'yellow', 'green'] as LightFilter[]).map((f) => (
                  <button
                    key={f}
                    type="button"
                    onClick={() => setLightFilter(f)}
                    className={`rounded-md border px-2.5 py-1 font-medium transition ${
                      lightFilter === f
                        ? 'border-blue-400 bg-blue-500/20 text-white'
                        : 'border-zinc-800 bg-zinc-900 text-zinc-400 hover:text-zinc-200'
                    }`}
                  >
                    {f === 'all'
                      ? `全部 (${lightCounts.total})`
                      : f === 'red'
                        ? `🔴 红灯 (${lightCounts.red})`
                        : f === 'yellow'
                          ? `🟡 黄灯 (${lightCounts.yellow})`
                          : `🟢 绿灯 (${lightCounts.green})`}
                  </button>
                ))}
              </div>
            </div>

            {filteredRows.length === 0 ? (
              <div className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-4 text-xs text-zinc-400">
                当前筛选条件下暂无标的。盘后 17:45 Sidecar 会自动结合最新公告、业绩预告与舆情更新 AI 排雷结论。
              </div>
            ) : (
              <div className="overflow-x-auto">
                <Table dense>
                  <TableHead>
                    <TableRow>
                      <TableHeader>标的</TableHeader>
                      <TableHeader>来源分组</TableHeader>
                      <TableHeader>AI 风险灯</TableHeader>
                      <TableHeader>催化分</TableHeader>
                      <TableHeader>核心指标</TableHeader>
                      <TableHeader>雷区标签 / AI 研判</TableHeader>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {filteredRows.map((row) => {
                      const badge = lightBadge(row.risk_light)
                      return (
                        <TableRow key={`${row.group}_${row.code}`}>
                          <TableCell className="text-xs">
                            <Link to={`/stock/${row.code}`} className="font-semibold text-white hover:text-blue-400">
                              {row.name}
                            </Link>
                            <div className="font-mono text-[11px] text-zinc-400">
                              {row.code} {row.industry !== '—' ? `· ${row.industry}` : ''}
                            </div>
                          </TableCell>
                          <TableCell className="text-xs">
                            <Badge color={row.group === 'holding' ? 'blue' : 'zinc'}>{row.groupLabel}</Badge>
                          </TableCell>
                          <TableCell>
                            <Badge color={badge.color}>{badge.label}</Badge>
                          </TableCell>
                          <TableCell className="font-mono text-xs text-zinc-200">
                            {row.catalyst_score != null ? `${row.catalyst_score} / 5` : '—'}
                          </TableCell>
                          <TableCell className="font-mono text-xs text-zinc-300">{row.extraMetric}</TableCell>
                          <TableCell className="max-w-lg whitespace-normal text-xs text-zinc-300">
                            {row.risk_flags.length > 0 && (
                              <div className="mb-1 flex flex-wrap gap-1">
                                {row.risk_flags.map((flg, idx) => (
                                  <span
                                    key={idx}
                                    className="rounded bg-red-950/60 px-1.5 py-0.5 text-[10px] text-red-200 ring-1 ring-red-500/30"
                                  >
                                    {flg}
                                  </span>
                                ))}
                              </div>
                            )}
                            <div className="leading-relaxed">{row.thesis}</div>
                          </TableCell>
                        </TableRow>
                      )
                    })}
                  </TableBody>
                </Table>
              </div>
            )}
          </Card>

          {/* 2. AI 排雷前向胜率验真（红灯 vs 黄灯 vs 绿灯 真实未来 5日/10日表现） */}
          {fwd && (
            <Card>
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <SectionTitle title="2. AI 排雷历史前向验真（绿灯 vs 黄灯 vs 红灯真实后验表现）" />
                <span className="font-mono text-xs text-zinc-400">截至 {fwd.as_of}</span>
              </div>
              <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                {(['green', 'yellow', 'red'] as const).map((k) => {
                  const g = fwd.by_light?.[k]
                  const b = lightBadge(k)
                  return (
                    <div key={k} className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3 text-xs">
                      <div className="flex items-center justify-between">
                        <Badge color={b.color}>{b.label}</Badge>
                        <span className="font-mono text-zinc-400">样本 {g?.total_samples ?? 0} 只</span>
                      </div>
                      <div className="mt-2.5 grid grid-cols-3 gap-2 font-mono">
                        <div>
                          <div className="text-[10px] text-zinc-500">T+5 均收益</div>
                          <div className="font-semibold text-zinc-100">{fmtSignedPct(g?.t5?.avg_pct)}</div>
                        </div>
                        <div>
                          <div className="text-[10px] text-zinc-500">T+10 均收益</div>
                          <div className="font-semibold text-zinc-100">{fmtSignedPct(g?.t10?.avg_pct)}</div>
                        </div>
                        <div>
                          <div className="text-[10px] text-zinc-500">最新均收益</div>
                          <div className="font-semibold text-zinc-100">{fmtSignedPct(g?.latest?.avg_pct)}</div>
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>
              {fwd.note && <p className="mt-2.5 text-xs text-zinc-400">{fwd.note}</p>}
            </Card>
          )}
        </div>
      )}
    </div>
  )
}
