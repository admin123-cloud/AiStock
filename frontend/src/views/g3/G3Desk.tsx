import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { EChart } from '@/components/EChart'
import { Card, PageHeader } from '@/components/ui'
import AR3DualCorePanel from '@/views/g3/AR3DualCorePanel'
import {
  generateG3PremarketBrief,
  getG3AR3Radar,
  getG3AR3RebalancePlan,
  getG3Desk,
  pushG3PremarketBriefWechat,
  upsertG3Watchlist,
  type G3AR3DualCoreRadarResponse,
  type G3AR3RebalancePlanResponse,
  type G3DeskResponse,
} from '@/api/g3'

const fmtNum = (v?: number | null, digits = 2): string =>
  v != null && Number.isFinite(Number(v))
    ? Number(v).toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
    : '—'

const fmtTimeOnly = (raw?: unknown): string => {
  const s = String(raw || '').trim()
  if (!s) return '—'
  if (s.includes(' ')) return s.split(' ')[1]?.slice(0, 8) || s
  if (s.includes('T')) return s.split('T')[1]?.slice(0, 8) || s
  return s.slice(0, 8)
}

const toneToBadgeColor = (tone?: string) => {
  if (tone === 'red') return 'red' as const
  if (tone === 'emerald') return 'emerald' as const
  if (tone === 'amber') return 'amber' as const
  if (tone === 'blue') return 'blue' as const
  return 'zinc' as const
}

export default function G3Desk() {
  const navigate = useNavigate()
  const [data, setData] = useState<G3DeskResponse | null>(null)
  const [radar, setRadar] = useState<G3AR3DualCoreRadarResponse | null>(null)
  const [plan, setPlan] = useState<G3AR3RebalancePlanResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [briefBusy, setBriefBusy] = useState(false)
  const [pushingBrief, setPushingBrief] = useState(false)
  const [showGateRules, setShowGateRules] = useState(false)
  const [showBriefBody, setShowBriefBody] = useState(false)
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')

  const load = useCallback(async (refreshQuotes = false) => {
    setLoading(true)
    setError('')
    try {
      const [deskRes, radarRes, planRes] = await Promise.allSettled([
        getG3Desk(refreshQuotes),
        getG3AR3Radar(false),
        getG3AR3RebalancePlan('acct1', true),
      ])
      if (deskRes.status === 'fulfilled') {
        setData(deskRes.value)
      } else {
        const err = deskRes.reason as { response?: { data?: { detail?: string } }; message?: string }
        setError(err?.response?.data?.detail || err?.message || '加载实战交易台失败')
      }
      if (radarRes.status === 'fulfilled') setRadar(radarRes.value)
      if (planRes.status === 'fulfilled') setPlan(planRes.value)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '加载实战交易台失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load(false)
  }, [load])

  const handleGenerateBrief = async () => {
    setBriefBusy(true)
    setMsg('')
    setError('')
    try {
      const brief = await generateG3PremarketBrief()
      setMsg(`已生成盘前主线简报（${brief.date} · ${brief.market_stance.regime_label}）`)
      await load(false)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '生成盘前简报失败')
    } finally {
      setBriefBusy(false)
    }
  }

  const handlePushBriefWechat = async () => {
    setPushingBrief(true)
    setMsg('')
    setError('')
    try {
      const res = await pushG3PremarketBriefWechat()
      if (res.ok) {
        setMsg(`已通过 ${res.channel} 推送盘前主线简报（${res.date || ''}）`)
      } else {
        setError(`盘前简报微信推送未成功：${res.detail}`)
      }
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '推送微信失败')
    } finally {
      setPushingBrief(false)
    }
  }

  const gate = data?.market_regime_gate
  const cmd = data?.trading_command
  const panicSignal = cmd?.panic_rebound_signal
  const redOrBrokenHoldings = (cmd?.holding_actions || []).filter(
    (h) => h.ai_risk_light === 'red' || h.broken_stop
  )

  return (
    <div className="flex w-full flex-col gap-4">
      <PageHeader
        title="G3 AR4.6 实战交易台"
        subtitle={`主力账号 ct1曾 · 交易日 ${data?.trade_date || '—'}`}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Button outline onClick={() => void load(true)} disabled={loading}>
              {loading ? '刷新中…' : '刷新行情'}
            </Button>
            <Link to="/g3/ai-live">
              <Button outline>📡 AI 盘面直播 →</Button>
            </Link>
            <Link to="/g3/accounts?tab=ttrade">
              <Button outline>资金与做T →</Button>
            </Link>
            <Link to="/g3/signals">
              <Button outline>选股雷达 →</Button>
            </Link>
          </div>
        }
      />

      {error && (
        <div className="rounded-lg border border-red-500/40 bg-red-500/10 px-4 py-2.5 text-sm text-red-200">
          {error}
        </div>
      )}
      {msg && (
        <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-4 py-2.5 text-sm text-emerald-200">
          {msg}
        </div>
      )}

      {/* 条件触发顶部强预警条（无异常时不占位，保持首屏清爽） */}
      {(panicSignal?.triggered || redOrBrokenHoldings.length > 0) && (
        <div className="flex flex-col gap-2">
          {panicSignal?.triggered && (
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-emerald-400/60 bg-emerald-950/30 px-4 py-2.5 text-xs text-emerald-100">
              <div className="flex flex-wrap items-center gap-2">
                <Badge color="emerald">🎯 冰点企稳反抽已触发</Badge>
                <span className="font-medium">{panicSignal.headline}</span>
              </div>
              <Link to="/g3/signals" className="font-semibold text-emerald-300 hover:underline">
                前往选股雷达查看明细 →
              </Link>
            </div>
          )}

          {redOrBrokenHoldings.length > 0 && (
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-red-500/50 bg-red-950/25 px-4 py-2.5 text-xs text-red-100">
              <div className="flex flex-wrap items-center gap-2">
                <Badge color="red">⚠️ 持仓风控预警（{redOrBrokenHoldings.length} 只）</Badge>
                <span>
                  {redOrBrokenHoldings
                    .map((h) => `${h.name}(${h.code})${h.broken_stop ? '破止损' : ''}${h.ai_risk_light === 'red' ? 'AI红灯' : ''}`)
                    .join(' · ')}
                </span>
              </div>
              <Link to="/g3/accounts?tab=ttrade" className="font-semibold text-red-300 hover:underline">
                前往资金账号与做T处置 →
              </Link>
            </div>
          )}
        </div>
      )}

      {/* 1. 大盘择时总闸门（双栏结构化：左=当前周期与判断依据，右=AR4.6持仓依据与实盘核对） */}
      {gate && (() => {
        const src = gate.sources
        const breadthPct = src.breadth?.above_ma20_pct
        const inPanicWatch = breadthPct != null && breadthPct <= 18.0 && gate.regime !== 'offensive'
        const idx = src.index_r51
        const idxBiasMa20 =
          idx?.close != null && idx?.ma20 != null && idx.ma20 > 0
            ? ((idx.close / idx.ma20 - 1) * 100)
            : null
        const mood = src.market_mood
        const sealRatePct = mood?.seal_rate != null ? mood.seal_rate * 100 : null

        const isOff = Boolean(radar?.is_offensive ?? (gate.regime === 'offensive' || gate.phase_5 === 'extreme_offensive'))
        const panicCamp = plan?.panic_campaign
        const isPanicCampActive = Boolean(panicCamp?.active && !isOff)
        const isPanicAb = Boolean(radar?.is_panic_ab_active || (panicSignal?.triggered && !isOff))
        const stockTargetPct = isOff
          ? Number(radar?.etf_radar?.stock_budget_active_pct ?? radar?.config?.effective_stock_budget_cap_pct ?? 50)
          : isPanicAb || isPanicCampActive
            ? 50
            : 0
        const etfTargetPct = Math.max(0, 100 - stockTargetPct)
        const etfTargets = radar?.etf_radar?.targets ?? []
        const etfTargetMap = new Map(etfTargets.map((t) => [t.code, t]))
        const macro = radar?.macro_dashboard
        const macroDec = macro?.decision

        // 拆解主力账号 acct1 实盘持仓：严格区分 A 股个股 vs 跨资产 ETF vs 现金（彻底告别旧 R51 把 ETF 混进个股上限的误导！）
        const acct = plan?.account
        const totalAssets = acct?.total_assets ?? 0
        const holdings = acct?.holdings ?? []
        const actualStockMv = holdings.filter((h) => !h.is_etf).reduce((s, h) => s + (h.market_value || 0), 0)
        const actualEtfMv = holdings.filter((h) => h.is_etf).reduce((s, h) => s + (h.market_value || 0), 0)
        const actualStockPct = totalAssets > 0 ? (actualStockMv / totalAssets) * 100 : 0
        const actualEtfPct = totalAssets > 0 ? (actualEtfMv / totalAssets) * 100 : 0
        const actualCashPct = totalAssets > 0 ? ((acct?.available_cash ?? 0) / totalAssets) * 100 : 0
        const stockCompliant = actualStockPct <= stockTargetPct + 2.0

        const phaseExplain = isOff
          ? 'AR4.6 五态「主升进攻窗」：宽基趋势、市场宽度与情绪共振，允许按国家队分位动态配股。'
          : isPanicAb
            ? 'AR4.6 五态「极端冰点 30m 触发」：宽度 ≤ 18% 且创业板+中证1000 30m K线右侧确认，临时开放 50% 半仓切入 10 万可执行 AB 真龙。'
            : inPanicWatch
              ? 'AR4.6 五态「防守退潮 · 已入冰点戒备」：宽度已 ≤ 18%，待 10:30~14:30 任一根 30m K线（创业板+中证1000双收阳且日内均涨 ≥+0.80%）首次达标即刻触发，暂保持 0% 个股待命。'
              : gate.regime === 'defensive'
                ? 'AR4.6 五态「防守退潮浪」：趋势向下或宽度跌破防守线，严防主跌浪，个股硬锁 0%。'
                : 'AR4.6 五态「横盘平衡期」：未达连续 3 日主升进攻门槛，18%~52% 半山腰个股硬锁 0%，改由跨资产 ETF 稳赚底薪。'

        return (
          <Card
            className={
              gate.regime === 'defensive'
                ? 'border-red-500/40 bg-red-950/10'
                : gate.regime === 'offensive'
                  ? 'border-emerald-500/40 bg-emerald-950/10'
                  : 'border-amber-500/40 bg-amber-950/10'
            }
          >
            {/* 顶栏：大盘状态 + 目标配比 + 规则折叠开关 */}
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-zinc-800/90 pb-3">
              <div className="flex flex-wrap items-center gap-2.5">
                <span className="text-lg font-bold text-white">大盘择时总闸门</span>
                <Badge color={toneToBadgeColor(gate.tone)}>{gate.regime_label}</Badge>
                {isPanicCampActive ? (
                  <Badge color="emerald">
                    🧊 冰点战役 T+{panicCamp?.held_days ?? 0}/{panicCamp?.max_hold_days ?? 5} · B档监控中
                  </Badge>
                ) : isPanicAb ? (
                  <Badge color="emerald">⚡ 冰点半仓已触发</Badge>
                ) : inPanicWatch ? (
                  <Badge color="amber">🧊 冰点戒备（宽度 {fmtNum(breadthPct)}%）</Badge>
                ) : isOff ? (
                  <Badge color="emerald">进攻开放 · 个股 {stockTargetPct}%</Badge>
                ) : (
                  <Badge color="red">个股 0% · 100% 避险 ETF</Badge>
                )}
              </div>
              <div className="flex items-center gap-3 text-sm text-zinc-300">
                <span className="font-mono">{gate.ref_date}</span>
                <button
                  type="button"
                  onClick={() => setShowGateRules((v) => !v)}
                  className="rounded border border-zinc-700 bg-zinc-900 px-2.5 py-1 text-xs text-zinc-200 hover:border-zinc-500"
                >
                  {showGateRules ? '收起判定规则 ▴' : '判定规则 ▾'}
                </button>
              </div>
            </div>

            {/* 主体双栏：左 = 4 项大号核心指标；右 = 目标配比 vs 实盘持仓 */}
            <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-12">
              <div className="grid grid-cols-2 gap-3 lg:col-span-6">
                <div className="rounded-lg border border-zinc-800/80 bg-zinc-950/65 px-4 py-3">
                  <div className="text-sm text-zinc-300">全市场宽度 (MA20)</div>
                  <div
                    className={`mt-1 font-mono text-2xl font-bold ${
                      breadthPct != null && breadthPct >= 52
                        ? 'text-emerald-300'
                        : breadthPct != null && breadthPct <= 18
                          ? 'text-amber-300'
                          : 'text-red-300'
                    }`}
                  >
                    {breadthPct != null ? `${fmtNum(breadthPct)}%` : '—'}
                  </div>
                  <div className="mt-1 text-xs text-zinc-400">进攻 ≥52% · 冰点 ≤18%</div>
                </div>

                <div className="rounded-lg border border-zinc-800/80 bg-zinc-950/65 px-4 py-3">
                  <div className="text-sm text-zinc-300">上证指数 vs MA20</div>
                  <div
                    className={`mt-1 font-mono text-2xl font-bold ${
                      idxBiasMa20 != null && idxBiasMa20 >= 0 ? 'text-emerald-300' : 'text-red-300'
                    }`}
                  >
                    {idx?.close != null ? fmtNum(idx.close, 0) : '—'}
                    <span className="ml-1.5 text-base">
                      ({idxBiasMa20 != null ? `${idxBiasMa20 >= 0 ? '+' : ''}${idxBiasMa20.toFixed(1)}%` : '—'})
                    </span>
                  </div>
                  <div className="mt-1 text-xs text-zinc-400">
                    MA20 {idx?.ma20 != null ? fmtNum(idx.ma20, 0) : '—'} ·{' '}
                    {idx?.confirmed_regime === 'up'
                      ? `连涨 ${idx?.streak_days ?? 0} 日`
                      : idx?.confirmed_regime === 'down'
                        ? `连跌 ${idx?.streak_days ?? 0} 日`
                        : '震荡'}
                  </div>
                </div>

                <div className="rounded-lg border border-zinc-800/80 bg-zinc-950/65 px-4 py-3">
                  <div className="text-sm text-zinc-300">市场情绪 · 涨跌停</div>
                  <div className="mt-1 font-mono text-xl font-bold text-white">
                    {mood?.mood_label || '—'} ({mood?.limit_up_count ?? '—'}/{mood?.limit_down_count ?? '—'})
                  </div>
                  <div className="mt-1 text-xs text-zinc-400">
                    封板率 {sealRatePct != null ? `${sealRatePct.toFixed(0)}%` : '—'} · 最高 {mood?.max_streak ?? '—'} 板
                  </div>
                </div>

                <div className="rounded-lg border border-zinc-800/80 bg-zinc-950/65 px-4 py-3">
                  <div className="text-sm text-zinc-300">全市场成交额</div>
                  <div className="mt-1 font-mono text-2xl font-bold text-white">
                    {src.turnover_forecast?.forecast_trillion != null
                      ? `${src.turnover_forecast.forecast_trillion.toFixed(2)} 万亿`
                      : '—'}
                  </div>
                  <div className="mt-1 text-xs text-zinc-400">
                    {src.turnover_forecast?.mode === 'intraday_forecast'
                      ? '盘中预测'
                      : src.turnover_forecast?.mode === 'daily_actual'
                        ? `正式收盘${src.turnover_forecast?.vol_ratio_5d != null ? ` · 5日量比 ${src.turnover_forecast.vol_ratio_5d.toFixed(2)}x` : ''}`
                        : '最新成交额'}
                  </div>
                </div>
              </div>

              {/* 右半区（红框升级）：策略目标 vs 实盘持仓 双环形图 + 合规体检结论 */}
              <div className="flex flex-col justify-between gap-2 rounded-lg border border-zinc-800/80 bg-zinc-950/65 p-4 lg:col-span-6">
                {(() => {
                  const comp = plan?.portfolio_compliance
                  const targetPalette = ['#38bdf8', '#10b981', '#a78bfa', '#f59e0b', '#ec4899']
                  const targetCodeColor = new Map<string, string>()
                  const targetPieData: Array<{ name: string; value: number; itemStyle: { color: string } }> = []
                  const allocList =
                    plan?.target_allocation && plan.target_allocation.length > 0
                      ? plan.target_allocation
                      : etfTargets.map((t) => ({
                          code: t.code,
                          name: t.name,
                          asset_kind: 'etf',
                          target_weight_pct: Number(t.account_weight_pct ?? 0),
                          ref_price: t.close,
                        }))
                  allocList.forEach((t, idx) => {
                    const color = targetPalette[idx % targetPalette.length]
                    targetCodeColor.set(t.code, color)
                    if (t.target_weight_pct > 0.5) {
                      targetPieData.push({
                        name: t.name,
                        value: Number(t.target_weight_pct.toFixed(1)),
                        itemStyle: { color },
                      })
                    }
                  })
                  const tgtSum = targetPieData.reduce((s, d) => s + d.value, 0)
                  if (tgtSum < 99) {
                    targetPieData.push({
                      name: '现金',
                      value: Number((100 - tgtSum).toFixed(1)),
                      itemStyle: { color: '#52525b' },
                    })
                  }

                  const unexpectedColors = ['#ef4444', '#f97316', '#f43f5e', '#fb7185', '#dc2626']
                  let unexpIdx = 0
                  const actualPieData: Array<{ name: string; value: number; itemStyle: { color: string } }> = []
                  const dHoldings = plan?.decision_holdings
                  if (dHoldings && dHoldings.length > 0) {
                    for (const h of dHoldings) {
                      if (h.actual_weight_pct <= 0.2) continue
                      const inTarget = targetCodeColor.get(h.code)
                      const color =
                        inTarget ??
                        (h.alignment === 'unexpected'
                          ? unexpectedColors[unexpIdx++ % unexpectedColors.length]
                          : '#34d399')
                      actualPieData.push({
                        name: h.alignment === 'unexpected' ? `${h.name}(预期外)` : h.name,
                        value: h.actual_weight_pct,
                        itemStyle: { color },
                      })
                    }
                  } else {
                    for (const h of holdings) {
                      const wPct = totalAssets > 0 ? Number(((h.market_value / totalAssets) * 100).toFixed(1)) : 0
                      if (wPct <= 0.2) continue
                      const inTarget = targetCodeColor.get(h.code)
                      actualPieData.push({
                        name: inTarget ? h.name : `${h.name}(预期外)`,
                        value: wPct,
                        itemStyle: { color: inTarget ?? unexpectedColors[unexpIdx++ % unexpectedColors.length] },
                      })
                    }
                  }
                  if (actualCashPct > 0.5 || actualPieData.length === 0) {
                    actualPieData.push({
                      name: '可用现金',
                      value: Number(actualCashPct.toFixed(1)),
                      itemStyle: { color: '#52525b' },
                    })
                  }

                  const dualPieOption = {
                    tooltip: {
                      trigger: 'item',
                      backgroundColor: '#18181b',
                      borderColor: '#3f3f46',
                      textStyle: { color: '#f4f4f5', fontSize: 13 },
                      formatter: '{a}<br/>{b}: <b>{c}%</b>',
                    },
                    title: [
                      {
                        text: '策略目标结构',
                        left: '24%',
                        top: '2%',
                        textAlign: 'center',
                        textStyle: { color: '#93c5fd', fontSize: 13, fontWeight: 600 },
                      },
                      {
                        text: '实盘持仓结构',
                        left: '74%',
                        top: '2%',
                        textAlign: 'center',
                        textStyle: { color: '#e4e4e7', fontSize: 13, fontWeight: 600 },
                      },
                    ],
                    series: [
                      {
                        name: 'AR4.6 策略目标',
                        type: 'pie',
                        radius: ['42%', '70%'],
                        center: ['25%', '56%'],
                        avoidLabelOverlap: true,
                        itemStyle: { borderColor: '#09090b', borderWidth: 2 },
                        label: {
                          show: true,
                          color: '#e4e4e7',
                          fontSize: 11,
                          formatter: '{b}\n{c}%',
                        },
                        labelLine: { length: 8, length2: 6 },
                        data: targetPieData,
                      },
                      {
                        name: 'acct1 实盘持仓',
                        type: 'pie',
                        radius: ['42%', '70%'],
                        center: ['75%', '56%'],
                        avoidLabelOverlap: true,
                        itemStyle: { borderColor: '#09090b', borderWidth: 2 },
                        label: {
                          show: true,
                          color: '#e4e4e7',
                          fontSize: 11,
                          formatter: '{b}\n{c}%',
                        },
                        labelLine: { length: 8, length2: 6 },
                        data: actualPieData,
                      },
                    ],
                  }

                  const complianceBadgeColor =
                    comp?.tone === 'emerald' ? 'emerald' : comp?.tone === 'amber' ? 'amber' : stockCompliant ? 'emerald' : 'red'

                  return (
                    <>
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-base font-bold text-white">持仓结构合规对比</span>
                          <Badge color={complianceBadgeColor}>
                            {comp?.headline || (stockCompliant ? '结构合规' : `个股超配 ${actualStockPct.toFixed(0)}%`)}
                          </Badge>
                        </div>
                        <span className="font-mono text-sm text-zinc-300">
                          合规度 <b className="text-white">{comp ? `${comp.compliance_pct.toFixed(0)}%` : '—'}</b> · 总资产{' '}
                          <b className="text-white">¥{fmtNum(totalAssets, 0)}</b>
                        </span>
                      </div>

                      <div className="w-full">
                        <EChart option={dualPieOption} height={195} />
                      </div>

                      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-zinc-800/80 pt-2 text-xs text-zinc-300">
                        <div className="flex flex-wrap items-center gap-3">
                          <span className="inline-flex items-center gap-1.5">
                            <span className="h-2.5 w-2.5 rounded-full bg-sky-400" />
                            策略内 ETF <b>{actualEtfPct.toFixed(0)}%</b>（目标 {etfTargetPct}%）
                          </span>
                          <span className="inline-flex items-center gap-1.5">
                            <span className="h-2.5 w-2.5 rounded-full bg-red-500" />
                            个股 <b>{actualStockPct.toFixed(0)}%</b>（目标 {stockTargetPct}%）
                          </span>
                          <span className="inline-flex items-center gap-1.5">
                            <span className="h-2.5 w-2.5 rounded-full bg-zinc-500" />
                            现金 <b>{actualCashPct.toFixed(0)}%</b>（¥{fmtNum(acct?.available_cash, 0)}）
                          </span>
                        </div>
                      </div>
                    </>
                  )
                })()}
              </div>
            </div>

            {/* 折叠的详细规则说明（默认隐藏，避免文字堆砌） */}
            {showGateRules && (
              <div className="mt-3 space-y-2 rounded-lg border border-zinc-800 bg-zinc-950/75 p-3.5 text-sm leading-6 text-zinc-200">
                <div>{phaseExplain}</div>
                {gate.reasons.length > 0 && (
                  <div>
                    <span className="font-semibold text-zinc-300">判定依据：</span>
                    {gate.reasons.join('；')}
                  </div>
                )}
                <div className="text-zinc-300">
                  <span className="font-semibold text-zinc-200">切换门槛：</span>
                  {isOff
                    ? '宽度 <52% 或上证破 MA20 → 切回平衡；连续 2 日宽度 <30% 或单日 <25% → 切入防守。'
                    : '平衡需宽度 ≥30% 且上证企稳；冰点半仓需宽度 ≤18% 且 10:30~14:30 30m 创业板+中证1000双阳均涨 ≥+0.80%；进攻需宽度 ≥52% + 上证 > MA20 + 20日动量 >+1% 连 3 日。'}
                </div>
                <div className="text-zinc-300">
                  <span className="font-semibold text-zinc-200">宏观参考：</span>
                  国家队 250 日分位 {macro?.nt_ratio_pctile_250d != null ? `${macro.nt_ratio_pctile_250d.toFixed(1)}%` : '—'}（
                  {macroDec?.phase_label || '—'}）· ERP {macro?.erp_pct != null ? `${macro.erp_pct.toFixed(2)}%` : '—'} · 南向 20 日{' '}
                  {macro?.sb_net_20d_yi != null ? `${macro.sb_net_20d_yi >= 0 ? '+' : ''}${macro.sb_net_20d_yi.toFixed(0)}亿` : '—'}
                </div>
              </div>
            )}
          </Card>
        )
      })()}

      {/* 2. 绝对核心主视图：AR4.6 今日买卖执行单 + 宏观 5 卡仪表盘（精简模式，15只ETF与选股明细已迁至 /g3/signals，AI 10m 直播与参谋已独立至 /g3/ai-live） */}
      <AR3DualCorePanel
        mode="desk_compact"
        externalRadar={radar}
        externalRadarLoading={loading}
        externalPlan={plan}
        onRadarUpdated={(nextRadar) => setRadar(nextRadar)}
        onPlanUpdated={(nextPlan) => setPlan(nextPlan)}
        onPrefillOrder={() => {
          navigate('/g3/accounts?tab=ttrade')
        }}
        onAddWatchlist={(code, name) => {
          void (async () => {
            try {
              await upsertG3Watchlist({ add: [{ code, qty: 0 }] })
              setMsg(`已将 ${name}(${code}) 加入 G3 做T观察池`)
            } catch (e) {
              const err = e as { message?: string }
              setError(err.message || `加入观察池失败：${code}`)
            }
          })()
        }}
      />

      {/* 3. 底部折叠卡：盘前晨会简报（默认收起，减少主界面文字干扰） */}
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2.5">
            <span className="text-base font-semibold text-white">盘前晨会简报</span>
            {data?.premarket_brief ? (
              <span className="font-mono text-sm text-zinc-300">
                {data.premarket_brief.date} · {fmtTimeOnly(data.premarket_brief.generated_at)}
              </span>
            ) : (
              <Badge color="zinc">尚未生成</Badge>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button outline onClick={() => setShowBriefBody((v) => !v)}>
              {showBriefBody ? '收起简报 ▴' : '展开简报 ▾'}
            </Button>
            <Button outline onClick={() => void handleGenerateBrief()} disabled={briefBusy}>
              {briefBusy ? '生成中…' : '刷新简报'}
            </Button>
            <Button outline onClick={() => void handlePushBriefWechat()} disabled={pushingBrief}>
              {pushingBrief ? '推送中…' : '推送微信'}
            </Button>
          </div>
        </div>

        {showBriefBody && (
          data?.premarket_brief ? (
            <div className="mt-3 space-y-2.5 border-t border-zinc-800 pt-3 text-sm">
              {data.premarket_brief.agent_thesis && (
                <div className="rounded-md border border-blue-500/30 bg-blue-500/10 px-3.5 py-2.5 text-blue-100">
                  <b>盘面研判：</b>
                  {data.premarket_brief.agent_thesis}
                </div>
              )}
              <div className="grid grid-cols-1 gap-2 text-zinc-100 md:grid-cols-2">
                {data.premarket_brief.action_checklist.map((line, i) => (
                  <div key={i} className="rounded border border-zinc-800/70 bg-zinc-950/50 px-3 py-2 leading-relaxed">
                    {i + 1}. {line}
                  </div>
                ))}
              </div>
              {data.premarket_brief.overnight_notes.length > 0 && (
                <div className="text-zinc-300">
                  隔夜要点：{data.premarket_brief.overnight_notes.join('；')}
                </div>
              )}
            </div>
          ) : (
            <p className="mt-3 border-t border-zinc-800 pt-3 text-sm text-zinc-400">
              暂无今日简报，点击「刷新简报」可立即生成。
            </p>
          )
        )}
      </Card>
    </div>
  )
}
