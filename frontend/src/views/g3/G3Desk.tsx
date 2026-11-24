import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
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
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')

  const load = useCallback(async (refreshQuotes = false) => {
    setLoading(true)
    setError('')
    try {
      const [deskRes, radarRes, planRes] = await Promise.allSettled([
        getG3Desk(refreshQuotes),
        getG3AR3Radar(refreshQuotes),
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
        subtitle={`主力账号 ct1曾 · 今日一键调仓与风险总览（极简执行动线 · 规划日 ${data?.trade_date || '—'} · 严格 prefill_only 不自动下单）`}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Button outline onClick={() => void load(true)} disabled={loading}>
              {loading ? '刷新中…' : '刷新行情'}
            </Button>
            <Link to="/g3/accounts?tab=ttrade">
              <Button outline>底仓做T与资金 →</Button>
            </Link>
            <Link to="/g3/signals">
              <Button outline>选股与ETF雷达 →</Button>
            </Link>
            <Link to="/g3/lab?tab=settings">
              <Button plain>通知与风控设置</Button>
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
        const isPanicAb = Boolean(radar?.is_panic_ab_active || (panicSignal?.triggered && !isOff))
        const stockTargetPct = isOff
          ? Number(radar?.etf_radar?.stock_budget_active_pct ?? radar?.config?.effective_stock_budget_cap_pct ?? 50)
          : isPanicAb
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
            {/* 顶栏：当前周期 + 核心状态徽章 */}
            <div className="flex flex-wrap items-center justify-between gap-2.5 border-b border-zinc-800/90 pb-3">
              <div className="flex flex-wrap items-center gap-2.5">
                <span className="text-base font-semibold text-white">大盘择时总闸门（AR4.6）</span>
                <Badge color={toneToBadgeColor(gate.tone)}>{gate.regime_label}</Badge>
                {isPanicAb ? (
                  <Badge color="emerald">⚡ 冰点 30m 已触发 · 半仓 50% AB 真龙 + 50% 避险 ETF</Badge>
                ) : inPanicWatch ? (
                  <Badge color="amber">🧊 宽度 {fmtNum(breadthPct)}% ≤ 18% · 冰点戒备（待 30m 阳线 ≥+0.80%）</Badge>
                ) : isOff ? (
                  <Badge color="emerald">主升进攻开放 · 个股目标 {stockTargetPct}% + 宽基 ETF {etfTargetPct}%</Badge>
                ) : (
                  <Badge color="red">非进攻期 · A股个股锁 0% + 100% 跨资产逆波 ETF</Badge>
                )}
              </div>
              <div className="font-mono text-xs text-zinc-300">
                参考日 {gate.ref_date} · 宽度日 {src.breadth?.date || '—'}
              </div>
            </div>

            {/* 主体双栏：左 = ① 当前处于什么周期 & 判断依据；右 = ② 当前的持仓依据 */}
            <div className="mt-3.5 grid grid-cols-1 gap-4 lg:grid-cols-12">
              {/* 左半区：1) 当前处于什么周期 & 判断依据 */}
              <div className="flex flex-col gap-3 rounded-lg border border-zinc-800/80 bg-zinc-950/55 p-4 lg:col-span-6">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm font-semibold text-white">① 当前周期与判断依据</span>
                  <span className="text-xs font-medium text-zinc-300">
                    {idx?.confirmed_regime === 'up'
                      ? `上证上升连 ${idx?.streak_days ?? 0} 日`
                      : idx?.confirmed_regime === 'down'
                        ? `上证下降连 ${idx?.streak_days ?? 0} 日`
                        : '上证震荡过渡'}
                  </span>
                </div>
                <div className="text-sm leading-6 text-zinc-100">{phaseExplain}</div>

                {/* 4 项硬指标实测 vs 阈值 */}
                <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
                  <div className="rounded-md border border-zinc-800/80 bg-zinc-900/70 px-3 py-2.5">
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="text-xs text-zinc-300">全市场宽度 (MA20占比)</span>
                      <span className={breadthPct != null && breadthPct >= 52 ? 'font-mono text-base font-bold text-emerald-300' : breadthPct != null && breadthPct <= 18 ? 'font-mono text-base font-bold text-amber-300' : 'font-mono text-base font-bold text-red-300'}>
                        {breadthPct != null ? `${fmtNum(breadthPct)}%` : '—'}
                      </span>
                    </div>
                    <div className="mt-1 text-xs text-zinc-400">
                      进攻 ≥52%连3日 · 防守 &lt;30% · 冰点 ≤18%
                    </div>
                  </div>

                  <div className="rounded-md border border-zinc-800/80 bg-zinc-900/70 px-3 py-2.5">
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="text-xs text-zinc-300">上证指数 vs MA20</span>
                      <span className={idxBiasMa20 != null && idxBiasMa20 >= 0 ? 'font-mono text-base font-bold text-emerald-300' : 'font-mono text-base font-bold text-red-300'}>
                        {idx?.close != null ? `${fmtNum(idx.close, 0)} (${idxBiasMa20 != null ? `${idxBiasMa20 >= 0 ? '+' : ''}${idxBiasMa20.toFixed(1)}%` : '—'})` : '—'}
                      </span>
                    </div>
                    <div className="mt-1 text-xs text-zinc-400">
                      MA20 {idx?.ma20 != null ? fmtNum(idx.ma20, 0) : '—'} · 60日动量 {idx?.index_mom60 != null ? `${idx.index_mom60 >= 0 ? '+' : ''}${(idx.index_mom60 * 100).toFixed(1)}%` : '—'}
                    </div>
                  </div>

                  <div className="rounded-md border border-zinc-800/80 bg-zinc-900/70 px-3 py-2.5">
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="text-xs text-zinc-300">市场情绪 · 涨跌停</span>
                      <span className="font-mono text-base font-bold text-zinc-100">
                        {mood?.mood_label || '—'} ({mood?.limit_up_count ?? '—'}/{mood?.limit_down_count ?? '—'})
                      </span>
                    </div>
                    <div className="mt-1 text-xs text-zinc-400">
                      封板率 {sealRatePct != null ? `${sealRatePct.toFixed(1)}%` : '—'} · 炸板 {mood?.broke_count ?? '—'} · 最高 {mood?.max_streak ?? '—'} 板
                    </div>
                  </div>

                  <div className="rounded-md border border-zinc-800/80 bg-zinc-900/70 px-3 py-2.5">
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="text-xs text-zinc-300">全市场成交额</span>
                      <span className="font-mono text-base font-bold text-zinc-100">
                        {src.turnover_forecast?.forecast_trillion != null ? `${src.turnover_forecast.forecast_trillion.toFixed(2)} 万亿` : '—'}
                      </span>
                    </div>
                    <div className="mt-1 text-xs text-zinc-400">
                      {src.turnover_forecast?.mode === 'intraday_forecast' ? '盘中全天预测额' : '最新全市场成交额'}
                    </div>
                  </div>
                </div>

                {/* 触发依据 + 下一周期切换条件 */}
                <div className="space-y-1.5 border-t border-zinc-800/70 pt-2.5 text-[13px] leading-6">
                  {gate.reasons.length > 0 && (
                    <div className="text-zinc-100">
                      <span className="font-semibold text-zinc-300">判定依据：</span>
                      {gate.reasons.join('；')}
                    </div>
                  )}
                  <div className="text-zinc-300">
                    <span className="font-semibold text-zinc-200">切换门槛：</span>
                    {isOff
                      ? '宽度跌破 52% 或上证破 MA20 → 切回平衡清股；连续 2 日宽度 <30% 或单日 <25% → 切入防守。'
                      : '切平衡需宽度 ≥30% 且上证企稳；触发冰点半仓真龙需宽度 ≤18% 且 10:30~14:30 任一根 30m K线，创业板+中证1000双收阳且日内均涨 ≥+0.80%（首次达标即刻触发，无需等尾盘）；升级进攻需宽度 ≥52% + 上证 > MA20 + 20d 动量 > +1% 连 3 日。'}
                  </div>
                </div>
              </div>

              {/* 右半区：2) 当前的持仓依据是什么（目标配比逻辑 + acct1 实盘对照） */}
              <div className="flex flex-col gap-3 rounded-lg border border-zinc-800/80 bg-zinc-950/55 p-4 lg:col-span-6">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-sm font-semibold text-white">② 当前持仓依据（AR4.6 目标 vs acct1 实盘）</span>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge color={stockCompliant ? 'emerald' : 'red'}>
                      A股个股 {actualStockPct.toFixed(1)}% / 目标 {stockTargetPct}%
                    </Badge>
                    <Badge color="blue">
                      ETF {actualEtfPct.toFixed(1)}% / 目标 {etfTargetPct}%
                    </Badge>
                  </div>
                </div>

                {/* 依据 1：为什么个股是这个仓位 */}
                <div className="space-y-2.5 text-[13px] leading-6">
                  <div className="rounded-md border border-zinc-800/70 bg-zinc-900/60 px-3 py-2 text-zinc-100">
                    <span className="font-semibold text-amber-300">个股仓位依据（目标 {stockTargetPct}%）：</span>
                    {isOff
                      ? `主升进攻确认，${macroDec?.cap_reason || `按国家队分位动态配 ${stockTargetPct}%`}，精选 F3A+F2A（AB_BLEND）前 4 只（同业限 1，日收盘 -10% 宽追踪）。`
                      : isPanicAb
                        ? '极端冰点 30m 创业板+中证1000右侧确认，临时开放 50% 半仓切入 10 万可执行 AB_BLEND Top4 真龙（单手 ≤1.5 万，逐票 -6% 止损 / +5% 后 -3.5% 追踪 / ≤5 日）。'
                        : `当前非进攻期（4 年实证 18%~52% 与下跌期买个股全亏），个股硬锁 0%；宏观底托参考：国家队 250 日分位 ${macro?.nt_ratio_pctile_250d != null ? `${macro.nt_ratio_pctile_250d.toFixed(1)}%` : '—'}（${macroDec?.phase_label || '—'}）· ERP ${macro?.erp_pct != null ? `${macro.erp_pct.toFixed(2)}%` : '—'}，等右侧确认再放大进攻股仓。`}
                  </div>

                  {/* 依据 2：为什么 ETF 选这几只 & 按此权重配 */}
                  <div className="rounded-md border border-zinc-800/70 bg-zinc-900/60 px-3 py-2 text-zinc-100">
                    <div className="font-semibold text-sky-300">
                      ETF 配置依据（目标 {etfTargetPct}% · {isOff ? 'A股宽基 20d 动量 Top2' : '非A股风险调整动量 Top2 逆波动率加权'}）：
                    </div>
                    {etfTargets.length > 0 ? (
                      <div className="mt-1.5 flex flex-wrap gap-2">
                        {etfTargets.map((t) => (
                          <span
                            key={t.code}
                            className="inline-flex items-center gap-1.5 rounded-md bg-zinc-800/95 px-2.5 py-1 font-mono text-xs text-zinc-100"
                          >
                            <b className="text-sky-300">{t.name}({t.code})</b>
                            <span className="font-semibold text-white">{(t.account_weight_pct ?? 0).toFixed(1)}%</span>
                            <span className="text-zinc-300">
                              (20d {t.ret_20d_pct >= 0 ? '+' : ''}{t.ret_20d_pct.toFixed(1)}% · 波动 {t.vol_20d_pct.toFixed(1)}%)
                            </span>
                          </span>
                        ))}
                      </div>
                    ) : (
                      <div className="mt-1 text-xs text-zinc-300">{radar?.etf_radar?.headline || '加载目标 ETF 依据中…'}</div>
                    )}
                    <div className="mt-1.5 text-xs text-zinc-300">
                      低波动多配、高波动少配（风险平价控回撤） · 南向 20 日净流{' '}
                      {macro?.sb_net_20d_yi != null ? `${macro.sb_net_20d_yi >= 0 ? '+' : ''}${macro.sb_net_20d_yi.toFixed(0)}亿` : '—'}
                      {macro?.sb_net_20d_yi != null && macro.sb_net_20d_yi < 0 ? '（港科已拦截）' : '（港科未拦截）'}
                    </div>
                  </div>

                  {/* 依据 3：acct1 当前实盘持仓逐只核对 */}
                  <div className="rounded-md border border-zinc-800/70 bg-zinc-900/60 px-3 py-2">
                    <div className="flex items-center justify-between text-zinc-200">
                      <span className="font-semibold">
                        acct1 实盘持仓核对（总仓位 {acct?.position_pct != null ? `${acct.position_pct.toFixed(1)}%` : '—'} · 现金 {actualCashPct.toFixed(1)}% / ¥{fmtNum(acct?.available_cash, 0)}）
                      </span>
                    </div>
                    {holdings.length > 0 ? (
                      <div className="mt-2 flex flex-wrap gap-2">
                        {holdings.map((h) => {
                          const wPct = totalAssets > 0 ? (h.market_value / totalAssets) * 100 : 0
                          const tgt = etfTargetMap.get(h.code)
                          const isPanicPair = h.code === '159915' || h.code === '512100'
                          const tag = tgt
                            ? `目标(${(tgt.account_weight_pct ?? 0).toFixed(0)}%)`
                            : isPanicPair
                              ? '冰点反抽仓(盯-6%止损/反抽兑现)'
                              : h.is_etf
                                ? '非目标ETF(择机切目标)'
                                : isOff
                                  ? '进攻持仓'
                                  : '非目标旧股(按步长清退)'
                          const tagCls = tgt
                            ? 'border-emerald-500/45 bg-emerald-950/35 text-emerald-100'
                            : isPanicPair
                              ? 'border-amber-500/45 bg-amber-950/35 text-amber-100'
                              : 'border-red-500/45 bg-red-950/35 text-red-100'
                          return (
                            <span
                              key={h.code}
                              className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs ${tagCls}`}
                            >
                              <b className="font-mono">{h.name}({h.code})</b>
                              <span className="font-mono font-semibold">{wPct.toFixed(1)}%</span>
                              {h.profit_pct != null && (
                                <span className="font-mono">
                                  ({h.profit_pct >= 0 ? '+' : ''}{h.profit_pct.toFixed(1)}%)
                                </span>
                              )}
                              <span>· {tag}</span>
                            </span>
                          )
                        })}
                      </div>
                    ) : (
                      <div className="mt-1 text-xs text-zinc-400">当前空仓（100% 现金待配置）</div>
                    )}
                  </div>
                </div>
              </div>
            </div>
          </Card>
        )
      })()}

      {/* 2. 绝对核心主视图：AR4.6 今日买卖执行单 + 宏观 5 卡仪表盘（精简模式，14只ETF与选股明细已迁至 /g3/signals） */}
      <AR3DualCorePanel
        mode="desk_compact"
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

      {/* 3. 底部紧凑卡：盘前 AI 晨会核心研判（AR4.6 单一口径） */}
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800 pb-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold text-white">盘前 AI 晨会简报（AR4.6 单一口径）</span>
            {data?.premarket_brief ? (
              <>
                <Badge color={data.premarket_brief.source === 'agent_enriched' ? 'emerald' : 'blue'}>
                  {data.premarket_brief.source === 'agent_enriched' ? 'Agent 增强版' : 'AR4.6 自动基线版'}
                </Badge>
                <span className="font-mono text-xs text-zinc-400">
                  {data.premarket_brief.date} · {fmtTimeOnly(data.premarket_brief.generated_at)}
                </span>
              </>
            ) : (
              <Badge color="zinc">尚未生成今日简报</Badge>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button outline onClick={() => void handleGenerateBrief()} disabled={briefBusy}>
              {briefBusy ? '生成中…' : '生成/刷新简报'}
            </Button>
            <Button outline onClick={() => void handlePushBriefWechat()} disabled={pushingBrief}>
              {pushingBrief ? '推送中…' : '一键推微信'}
            </Button>
          </div>
        </div>

        {data?.premarket_brief ? (
          <div className="mt-3 space-y-2 text-xs">
            {data.premarket_brief.agent_thesis && (
              <div className="rounded-md border border-blue-500/30 bg-blue-500/10 px-3 py-2 text-blue-200">
                <b>AI 盘面研判：</b>
                {data.premarket_brief.agent_thesis}
              </div>
            )}
            <div className="grid grid-cols-1 gap-1.5 text-zinc-200 md:grid-cols-2">
              {data.premarket_brief.action_checklist.map((line, i) => (
                <div key={i} className="rounded border border-zinc-800/70 bg-zinc-950/50 px-2.5 py-1.5 leading-relaxed">
                  {i + 1}. {line}
                </div>
              ))}
            </div>
            {data.premarket_brief.overnight_notes.length > 0 && (
              <div className="text-zinc-400">
                隔夜要点：{data.premarket_brief.overnight_notes.join('；')}
              </div>
            )}
          </div>
        ) : (
          <p className="mt-2.5 text-xs text-zinc-400">
            点击右上角「生成/刷新简报」或等待每日 09:10 盘前 Agent 自动生成 AR4.6 晨会简报并推送微信。
          </p>
        )}
      </Card>
    </div>
  )
}
