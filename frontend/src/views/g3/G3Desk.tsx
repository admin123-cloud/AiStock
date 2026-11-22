import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Card, PageHeader } from '@/components/ui'
import AR3DualCorePanel from '@/views/g3/AR3DualCorePanel'
import {
  generateG3PremarketBrief,
  getG3Desk,
  pushG3PremarketBriefWechat,
  upsertG3Watchlist,
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
  const [loading, setLoading] = useState(false)
  const [briefBusy, setBriefBusy] = useState(false)
  const [pushingBrief, setPushingBrief] = useState(false)
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')

  const load = useCallback(async (refreshQuotes = false) => {
    setLoading(true)
    setError('')
    try {
      const res = await getG3Desk(refreshQuotes)
      setData(res)
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
        title="G3 AR4.5-L3b 实战交易台"
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

      {/* 1. 大盘择时总闸门（单卡紧凑条） */}
      {gate && (() => {
        const wl = gate.r51_waterline
        const src = gate.sources
        const actualPct = wl.actual_pct ?? 0
        const capPct = wl.target_cap_pct
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
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/90 pb-2.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-semibold text-white">大盘择时总闸门</span>
                <Badge color={toneToBadgeColor(gate.tone)}>{gate.regime_label}</Badge>
                <Badge color={gate.allow_new_mainwave ? 'emerald' : 'red'}>
                  {gate.allow_new_mainwave ? '允许 AR4.5 个股开新仓' : '防守切 100% AR4.5 逆波ETF · 严禁买A股'}
                </Badge>
                <Badge color="blue">A股风险上限 ≤ {capPct.toFixed(0)}%</Badge>
              </div>
              <div className="flex flex-wrap items-center gap-3 font-mono text-[11px] text-zinc-400">
                <span>情绪 {src.market_mood.mood_label || '—'} ({src.market_mood.limit_up_count ?? '—'}/{src.market_mood.limit_down_count ?? '—'})</span>
                <span>宽度 {src.breadth.above_ma20_pct != null ? `${fmtNum(src.breadth.above_ma20_pct)}%` : '—'}</span>
                <span>成交 {src.turnover_forecast.forecast_trillion != null ? `${src.turnover_forecast.forecast_trillion.toFixed(2)}万亿` : '—'}</span>
                <span>参考日 {gate.ref_date}</span>
              </div>
            </div>

            <div className="mt-2.5 grid grid-cols-1 gap-3 lg:grid-cols-12">
              <div className="lg:col-span-7">
                <div className="text-xs font-medium text-zinc-100">{gate.action_summary}</div>
                <div className="mt-1.5 text-[11px] leading-relaxed text-zinc-400">{wl.advice}</div>
              </div>
              <div className="flex flex-col justify-center lg:col-span-5">
                <div className="flex items-center justify-between text-xs">
                  <span className="text-zinc-300">
                    实盘水位 <b className="font-mono text-white">{wl.actual_pct != null ? `${wl.actual_pct.toFixed(1)}%` : '—'}</b>
                    <span className="mx-1.5 text-zinc-600">/</span>
                    个股上限 <b className="font-mono text-blue-300">{capPct.toFixed(0)}%</b>
                  </span>
                  <Badge color={wl.status === 'overweight' ? 'red' : wl.status === 'underweight' ? 'blue' : wl.status === 'aligned' ? 'emerald' : 'zinc'}>
                    {wl.status === 'overweight' ? '超配需降水位' : wl.status === 'underweight' ? '低配可分步加' : wl.status === 'aligned' ? '水位匹配' : '待同步'}
                  </Badge>
                </div>
                <div className="relative mt-1.5 h-2 w-full overflow-hidden rounded-full bg-zinc-800">
                  <div
                    className={wl.status === 'overweight' ? 'h-full bg-red-500/85' : 'h-full bg-blue-500/85'}
                    style={{ width: `${Math.min(100, Math.max(0, actualPct))}%` }}
                  />
                  <div
                    className="absolute top-0 bottom-0 w-0.5 bg-amber-300"
                    style={{ left: `${Math.min(100, Math.max(0, capPct))}%` }}
                    title={`A股风险上限 ${capPct}%`}
                  />
                </div>
              </div>
            </div>
          </Card>
        )
      })()}

      {/* 2. 绝对核心主视图：AR4.5-L3b 今日买卖执行单 + 宏观 5 卡仪表盘（精简模式，14只ETF与选股明细已迁至 /g3/signals） */}
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

      {/* 3. 底部紧凑卡：盘前 AI 晨会核心研判（AR4.5-L3b 单一口径） */}
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800 pb-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold text-white">盘前 AI 晨会简报（AR4.5-L3b 单一口径）</span>
            {data?.premarket_brief ? (
              <>
                <Badge color={data.premarket_brief.source === 'agent_enriched' ? 'emerald' : 'blue'}>
                  {data.premarket_brief.source === 'agent_enriched' ? 'Agent 增强版' : 'AR4.5 自动基线版'}
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
            点击右上角「生成/刷新简报」或等待每日 09:10 盘前 Agent 自动生成 AR4.5-L3b 晨会简报并推送微信。
          </p>
        )}
      </Card>
    </div>
  )
}
