import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/catalyst/table'
import { Card, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import {
  deleteG3TExecution,
  generateG3DailyPlan,
  getG3Desk,
  recordG3TExecution,
  syncG3WatchlistFromThs,
  upsertG3Watchlist,
  type G3DeskCard,
  type G3DeskResponse,
  type G3PriceGauge,
} from '@/api/g3'

const fmtNum = (v?: number | null, digits = 2): string =>
  v != null && Number.isFinite(Number(v))
    ? Number(v).toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
    : '—'

const fmtSignedPct = (v?: number | null): string => {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  const n = Number(v)
  return `${n > 0 ? '+' : ''}${n.toFixed(2)}%`
}

const fmtBand = (band?: [number, number] | null): string =>
  band && band.length === 2 ? `${fmtNum(band[0])} ~ ${fmtNum(band[1])}` : '—'

const regimeBadge = (regime: string) => {
  if (regime === 'up') return { label: '上升周期', color: 'emerald' as const }
  if (regime === 'down') return { label: '下降周期', color: 'red' as const }
  if (regime === 'sideways') return { label: '横盘周期', color: 'amber' as const }
  return { label: '未判定', color: 'zinc' as const }
}

const toneToBadgeColor = (tone?: string) => {
  if (tone === 'red') return 'red' as const
  if (tone === 'emerald') return 'emerald' as const
  if (tone === 'amber') return 'amber' as const
  if (tone === 'blue') return 'blue' as const
  return 'zinc' as const
}

function PriceGaugeBar({ gauge }: { gauge: G3PriceGauge }) {
  const buyLeft = gauge.buy_start_pct ?? 0
  const buyWidth =
    gauge.buy_start_pct != null && gauge.buy_end_pct != null
      ? Math.max(2, gauge.buy_end_pct - gauge.buy_start_pct)
      : 0
  const sellLeft = gauge.sell_start_pct ?? 0
  const sellWidth =
    gauge.sell_start_pct != null && gauge.sell_end_pct != null
      ? Math.max(2, gauge.sell_end_pct - gauge.sell_start_pct)
      : 0

  return (
    <div className="rounded-lg border border-zinc-800/90 bg-zinc-950/70 p-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs">
        <span className="font-medium text-zinc-200">
          实时价位标尺 · <span className="text-blue-300">{gauge.zone_label}</span>
        </span>
        <div className="flex flex-wrap items-center gap-3 font-mono text-[11px]">
          <span className="text-amber-300">
            距止损 {gauge.dist_to_stop_pct != null ? fmtSignedPct(gauge.dist_to_stop_pct) : '—'}
          </span>
          <span className="text-emerald-300">
            距买带 {gauge.dist_to_buy_pct != null ? fmtSignedPct(gauge.dist_to_buy_pct) : '—'}
          </span>
          <span className="text-rose-300">
            距卖带 {gauge.dist_to_sell_pct != null ? fmtSignedPct(gauge.dist_to_sell_pct) : '—'}
          </span>
        </div>
      </div>

      {/* 标尺轨道 */}
      <div className="relative my-3 h-3 w-full rounded-full bg-zinc-800/90">
        {/* 买入价带区间 */}
        {buyWidth > 0 && (
          <div
            className="absolute top-0 h-full rounded-full bg-emerald-500/35 ring-1 ring-emerald-400/50"
            style={{ left: `${buyLeft}%`, width: `${buyWidth}%` }}
            title="买入价带"
          />
        )}
        {/* 卖出价带区间 */}
        {sellWidth > 0 && (
          <div
            className="absolute top-0 h-full rounded-full bg-rose-500/35 ring-1 ring-rose-400/50"
            style={{ left: `${sellLeft}%`, width: `${sellWidth}%` }}
            title="卖出价带"
          />
        )}
        {/* 止损竖线 */}
        {gauge.stop_pct != null && (
          <div
            className="absolute -top-1 h-5 w-0.5 bg-amber-400"
            style={{ left: `${gauge.stop_pct}%` }}
            title={`止损位 ${fmtNum(gauge.stop_px)}`}
          />
        )}
        {/* 分时均价 VWAP 竖线 */}
        {gauge.vwap_pct != null && (
          <div
            className="absolute -top-1 h-5 w-0.5 bg-sky-400"
            style={{ left: `${gauge.vwap_pct}%` }}
            title={`分时均价 VWAP ${fmtNum(gauge.vwap_px)}`}
          />
        )}
        {/* 当前现价游标 */}
        {gauge.current_pct != null && (
          <div
            className="absolute top-1/2 h-4 w-4 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white bg-blue-500 shadow"
            style={{ left: `${gauge.current_pct}%` }}
            title={`现价 ${fmtNum(gauge.current_px)}`}
          />
        )}
      </div>

      {/* 底部图例 */}
      <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-zinc-400">
        <span className="font-mono">低 {fmtNum(gauge.min_px)}</span>
        <div className="flex flex-wrap items-center gap-3">
          <span className="inline-flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-full bg-amber-400" />
            止损 {fmtNum(gauge.stop_px)}
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-full bg-sky-400" />
            均价 {fmtNum(gauge.vwap_px)}
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-full bg-blue-500 ring-1 ring-white" />
            现价 {fmtNum(gauge.current_px)}
          </span>
        </div>
        <span className="font-mono">高 {fmtNum(gauge.max_px)}</span>
      </div>
    </div>
  )
}

interface ExecFormState {
  open: boolean
  code: string
  name: string
  side: 'BUY' | 'SELL'
  price: string
  shares: string
  account_label: string
  note: string
}

const EMPTY_EXEC_FORM: ExecFormState = {
  open: false,
  code: '',
  name: '',
  side: 'SELL',
  price: '',
  shares: '100',
  account_label: '',
  note: '',
}

export default function G3Desk() {
  const [data, setData] = useState<G3DeskResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [countdown, setCountdown] = useState(15)
  const [showIneligible, setShowIneligible] = useState(false)
  const [newCode, setNewCode] = useState('')
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')
  const [execForm, setExecForm] = useState<ExecFormState>(EMPTY_EXEC_FORM)
  const [submittingExec, setSubmittingExec] = useState(false)

  const load = useCallback(async (refreshQuotes = false, silent = false) => {
    if (!silent) setLoading(true)
    setError('')
    try {
      const res = await getG3Desk(refreshQuotes)
      setData(res)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '加载失败')
    } finally {
      if (!silent) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load(false, false)
  }, [load])

  // 盘中自动轮询（每 15 秒刷新一次实时行情、RSI 与草案）
  useEffect(() => {
    if (!autoRefresh) return
    const timer = window.setInterval(() => {
      setCountdown((prev) => {
        if (prev <= 1) {
          void load(false, true)
          return 15
        }
        return prev - 1
      })
    }, 1000)
    return () => window.clearInterval(timer)
  }, [autoRefresh, load])

  const handleGeneratePlan = async () => {
    setGenerating(true)
    setMsg('')
    setError('')
    try {
      const res = await generateG3DailyPlan()
      setMsg(`已生成今日（${res.date}）做T规划，覆盖 ${res.codes_count} 只标的（${res.generated_at}）`)
      await load(true, false)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '生成今日规划失败')
    } finally {
      setGenerating(false)
    }
  }

  const handleSyncThs = async () => {
    setSyncing(true)
    setMsg('')
    setError('')
    try {
      await syncG3WatchlistFromThs()
      setMsg('已从双账号同花顺持仓同步做T观察名单')
      await load(true, false)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '同步失败')
    } finally {
      setSyncing(false)
    }
  }

  const handleAddWatch = async () => {
    const code = newCode.trim()
    if (!code) return
    setMsg('')
    setError('')
    try {
      await upsertG3Watchlist({ add: [code] })
      setNewCode('')
      setMsg(`已加入观察名单：${code}`)
      await load(true, false)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '添加失败')
    }
  }

  const handleRemoveWatch = async (code: string) => {
    setMsg('')
    setError('')
    try {
      await upsertG3Watchlist({ remove: [code] })
      setMsg(`已移出观察名单：${code}`)
      await load(false, false)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '移除失败')
    }
  }

  const openExecModal = (
    card: G3DeskCard,
    side: 'BUY' | 'SELL',
    presetPrice?: number | null,
    presetLot?: number,
    note = ''
  ) => {
    const px = presetPrice ?? card.live_quote?.price ?? card.prev_close ?? 0
    const lot = presetLot && presetLot >= 100 ? presetLot : Math.max(100, card.daily_plan?.recommended_lot || card.t_lot_cap || 100)
    setExecForm({
      open: true,
      code: card.code,
      name: card.name,
      side,
      price: px ? Number(px).toFixed(2) : '',
      shares: String(lot),
      account_label: card.holdings[0]?.account_label || '',
      note,
    })
  }

  const handleSubmitExec = async () => {
    const price = Number(execForm.price)
    const shares = Number(execForm.shares)
    if (!execForm.code || !Number.isFinite(price) || price <= 0 || !Number.isFinite(shares) || shares < 100 || shares % 100 !== 0) {
      setError('请输入有效的成交价和整百股数（如 200、500）')
      return
    }
    setSubmittingExec(true)
    setError('')
    setMsg('')
    try {
      await recordG3TExecution({
        code: execForm.code,
        name: execForm.name,
        side: execForm.side,
        price,
        shares,
        account_label: execForm.account_label,
        note: execForm.note,
      })
      setMsg(`已登记做T成交：${execForm.name} ${execForm.side === 'BUY' ? '买入' : '卖出'} ${shares} 股 @ ${price.toFixed(2)}`)
      setExecForm(EMPTY_EXEC_FORM)
      await load(false, false)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '登记成交失败')
    } finally {
      setSubmittingExec(false)
    }
  }

  const handleDeleteExec = async (execId: string) => {
    setError('')
    try {
      await deleteG3TExecution(execId)
      setMsg('已撤销该笔做T记录')
      await load(false, false)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '撤销失败')
    }
  }

  const summary = data?.accounts_summary
  const directive = data?.market_directive
  const intraday = data?.intraday_summary
  const cards: G3DeskCard[] = data?.cards || []

  const { eligibleCards, ineligibleCards } = useMemo(() => {
    const elig: G3DeskCard[] = []
    const inelig: G3DeskCard[] = []
    for (const c of cards) {
      if (c.daily_plan ? c.daily_plan.eligible : c.t_lot_cap >= 100) {
        elig.push(c)
      } else {
        inelig.push(c)
      }
    }
    return { eligibleCards: elig, ineligibleCards: inelig }
  }, [cards])

  const executions = data?.executions || []

  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 p-4 sm:p-6">
      <PageHeader
        title="G3 做T实战台 · 每日作战规划"
        subtitle={`规划日期 ${data?.trade_date || '—'} · 生成于 ${data?.plan_meta?.generated_at || data?.regime_meta?.generated_at || '—'} · 实时行情 × 价位标尺 × 日内敞口闭环`}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Button onClick={handleGeneratePlan} disabled={generating}>
              {generating ? '生成中…' : '生成/更新今日规划'}
            </Button>
            <Button
              outline
              onClick={() => {
                setAutoRefresh((v) => !v)
                setCountdown(15)
              }}
            >
              {autoRefresh ? `自动刷新 ${countdown}s` : '自动刷新：关'}
            </Button>
            <Button outline onClick={handleSyncThs} disabled={syncing}>
              {syncing ? '同步中…' : '同步同花顺持仓'}
            </Button>
            <Button outline onClick={() => void load(true, false)} disabled={loading}>
              {loading ? '刷新中…' : '刷新行情'}
            </Button>
          </div>
        }
      />

      {/* 规划过期强告警 */}
      {data?.is_stale_regime && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-amber-500/50 bg-amber-500/15 px-4 py-3 text-sm text-amber-100">
          <div>
            <span className="font-semibold">⚠️ 当前展示的是历史日期规划（{data.trade_date}），非今日（{data.today}）最新规划。</span>
            <span className="ml-2 text-amber-200/80">建议点击右侧按钮立即基于最新日线重算今日做T剧本。</span>
          </div>
          <Button onClick={handleGeneratePlan} disabled={generating}>
            {generating ? '生成中…' : '立即生成今日规划'}
          </Button>
        </div>
      )}

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

      {/* 1. 今日做T总纲 + 核心作战指标 */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        <Card className="lg:col-span-7">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800 pb-3">
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold text-white">今日做T总纲（盘前定调）</span>
              {directive && <Badge color={toneToBadgeColor(directive.tone)}>{directive.mode_label}</Badge>}
            </div>
            <div className="flex flex-wrap items-center gap-3 font-mono text-xs text-zinc-400">
              <span>站上MA20: <b className="text-zinc-100">{directive?.above_ma20_pct != null ? `${fmtNum(directive.above_ma20_pct)}%` : '—'}</b></span>
              <span>60日新高/新低: <b className="text-zinc-100">{directive?.new_high_60d ?? '—'}/{directive?.new_low_60d ?? '—'}</b></span>
            </div>
          </div>
          <p className="mt-2.5 text-sm leading-relaxed text-zinc-200">
            {directive?.summary || '等待生成今日大盘定调与市场宽度指标…'}
          </p>
          {directive?.rules && directive.rules.length > 0 && (
            <ul className="mt-2.5 space-y-1 text-xs text-zinc-400">
              {directive.rules.map((r, i) => (
                <li key={i} className="flex items-start gap-2">
                  <span className="mt-0.5 text-blue-400">•</span>
                  <span>{r}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <div className="grid grid-cols-2 gap-3 lg:col-span-5">
          <StatCard
            label="双账号可用现金"
            value={summary?.total_cash != null ? `¥${fmtNum(summary.total_cash, 0)}` : '—'}
            hint={`总资产 ¥${fmtNum(summary?.total_assets, 0)}`}
          />
          <StatCard
            label="可做T标的"
            value={`${intraday?.eligible_count ?? eligibleCards.length} 只`}
            hint={`${summary?.bridge_online ? '桥接在线' : '桥接离线'} · 零股 ${intraday?.ineligible_count ?? ineligibleCards.length} 只`}
          />
          <StatCard
            label="日内未平T敞口"
            value={
              intraday && intraday.open_exposure_shares > 0
                ? `${intraday.open_exposure_shares.toLocaleString()} 股`
                : '0 股（已平）'
            }
            hint={
              intraday && intraday.open_exposure_codes > 0
                ? `⚠️ ${intraday.open_exposure_codes} 只待日内平仓`
                : '无隔夜敞口风险'
            }
          />
          <StatCard
            label="今日已平T盈亏"
            value={
              intraday
                ? `${intraday.realized_t_pnl > 0 ? '+' : ''}¥${fmtNum(intraday.realized_t_pnl)}`
                : '¥0.00'
            }
            hint={`已记 ${intraday?.execution_count ?? 0} 笔 · 草案 ${data?.drafts?.length ?? 0} 条`}
          />
        </div>
      </div>

      {/* 手动添加观察标的 */}
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="text-xs text-zinc-300 sm:text-sm">
            实时票池 = <span className="font-medium text-white">双账号真实持仓 ∪ 做T观察名单</span>，单笔 T 上限按{' '}
            <code className="rounded bg-zinc-800 px-1.5 py-0.5 text-xs text-zinc-200">floor(底仓/3/100)×100</code> 计算；零股（&lt;300股）自动折叠。
          </div>
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={newCode}
              onChange={(e) => setNewCode(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void handleAddWatch()
              }}
              placeholder="输入 6 位代码如 002015"
              className="w-44 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm text-white placeholder-zinc-500 focus:border-blue-500 focus:outline-none"
            />
            <Button outline onClick={handleAddWatch}>加入观察</Button>
          </div>
        </div>
      </Card>

      {/* 快捷手工做T登记面板（内联弹出，不遮挡盯盘） */}
      {execForm.open && (
        <Card className="border-blue-500/50 bg-blue-950/20">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800 pb-2.5">
            <div className="text-sm font-semibold text-white">
              登记当日做T成交 · {execForm.name} ({execForm.code})
            </div>
            <Button plain onClick={() => setExecForm(EMPTY_EXEC_FORM)}>取消</Button>
          </div>
          <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-6">
            <div>
              <label className="block text-xs text-zinc-400">方向</label>
              <select
                value={execForm.side}
                onChange={(e) => setExecForm((f) => ({ ...f, side: e.target.value as 'BUY' | 'SELL' }))}
                className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-sm text-white"
              >
                <option value="SELL">卖出T（先卖/高抛）</option>
                <option value="BUY">买入T（低吸/接回）</option>
              </select>
            </div>
            <div>
              <label className="block text-xs text-zinc-400">成交价</label>
              <input
                type="number"
                step="0.01"
                value={execForm.price}
                onChange={(e) => setExecForm((f) => ({ ...f, price: e.target.value }))}
                className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 font-mono text-sm text-white"
              />
            </div>
            <div>
              <label className="block text-xs text-zinc-400">股数（整百）</label>
              <input
                type="number"
                step="100"
                min="100"
                value={execForm.shares}
                onChange={(e) => setExecForm((f) => ({ ...f, shares: e.target.value }))}
                className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 font-mono text-sm text-white"
              />
            </div>
            <div className="col-span-2">
              <label className="block text-xs text-zinc-400">备注（选填）</label>
              <input
                type="text"
                value={execForm.note}
                onChange={(e) => setExecForm((f) => ({ ...f, note: e.target.value }))}
                placeholder="如：反T卖1成交 / 急杀接回"
                className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-sm text-white"
              />
            </div>
            <div className="flex items-end">
              <Button onClick={handleSubmitExec} disabled={submittingExec} className="w-full">
                {submittingExec ? '提交中…' : '确认登记'}
              </Button>
            </div>
          </div>
        </Card>
      )}

      {/* 2. 可做T标的作战卡片 */}
      <SectionTitle title={`可做T标的作战规划（${eligibleCards.length} 只 · 实时标尺 × 挂单阶梯 × 情景剧本）`} />
      {eligibleCards.length === 0 ? (
        <Card>
          <div className="py-6 text-center text-sm text-zinc-400">当前暂无满足底仓 ≥ 300 股的可做T标的</div>
        </Card>
      ) : (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          {eligibleCards.map((c) => {
            const rb = regimeBadge(c.regime)
            const plan = c.daily_plan
            const eff = c.effective_params || c.params
            const q = c.live_quote || {}
            const armedCount = Object.values(c.armed_sides || {}).filter((s) => s?.armed).length
            const chg = q.change_pct
            const chgColor = chg == null ? 'text-zinc-300' : chg > 0 ? 'text-rose-400' : chg < 0 ? 'text-emerald-400' : 'text-zinc-300'
            const rsi5 = q.rsi5m
            const rsi5BuyHit = rsi5 != null && eff?.buy_rsi5_max != null && rsi5 <= eff.buy_rsi5_max
            const rsi5SellHit = rsi5 != null && eff?.sell_rsi5_min != null && rsi5 >= eff.sell_rsi5_min

            return (
              <Card
                key={c.code}
                className={
                  plan?.action_mode === 'stop_alert'
                    ? 'border-red-500/60 ring-1 ring-red-500/30'
                    : undefined
                }
              >
                {/* 头部：股票名 + 实时价 + 今日主策略 */}
                <div className="flex flex-wrap items-start justify-between gap-3 border-b border-zinc-800 pb-3">
                  <div>
                    <div className="flex flex-wrap items-center gap-2">
                      <Link
                        to={`/stock/${c.code}`}
                        className="text-base font-semibold text-white hover:text-blue-400"
                      >
                        {c.name} <span className="font-mono text-sm text-zinc-400">{c.code}</span>
                      </Link>
                      <Badge color={rb.color}>{rb.label}</Badge>
                      {plan && <Badge color={toneToBadgeColor(plan.action_tone)}>{plan.action_label}</Badge>}
                      {c.open_t_exposure && c.open_t_exposure.net_shares !== 0 && (
                        <Badge color="amber">{c.open_t_exposure.status_label}</Badge>
                      )}
                    </div>
                    <div className="mt-1.5 flex flex-wrap items-center gap-3 text-xs text-zinc-400">
                      <span>昨收 {fmtNum(c.prev_close)}</span>
                      <span>
                        VWAP{' '}
                        <span className="font-mono text-sky-300">
                          {fmtNum(q.vwap)} ({fmtSignedPct(q.vwap_dev_pct)})
                        </span>
                      </span>
                      <span>5日 {c.chg5d_pct != null ? `${fmtNum(c.chg5d_pct)}%` : '—'}</span>
                      <span>20日位 {c.pos20 != null ? `${(c.pos20 * 100).toFixed(0)}%` : '—'}</span>
                    </div>
                  </div>

                  {/* 右侧大号实时现价与涨跌幅 */}
                  <div className="flex items-start gap-3">
                    <div className="text-right">
                      <div className={`font-mono text-xl font-bold leading-none ${chgColor}`}>
                        {fmtNum(q.price ?? c.prev_close)}
                      </div>
                      <div className={`mt-1 font-mono text-xs font-medium ${chgColor}`}>
                        {fmtSignedPct(chg)} {q.quote_ts ? `· ${q.quote_ts}` : ''}
                      </div>
                    </div>
                    {c.in_watchlist && (
                      <Button plain onClick={() => void handleRemoveWatch(c.code)}>
                        移出
                      </Button>
                    )}
                  </div>
                </div>

                {/* 风险/机会预警条 */}
                {plan?.alerts && plan.alerts.length > 0 && (
                  <div className="mt-3 space-y-1.5">
                    {plan.alerts.map((a, idx) => (
                      <div
                        key={idx}
                        className={`rounded-md px-2.5 py-1.5 text-xs ${
                          a.level === 'danger'
                            ? 'border border-red-500/40 bg-red-500/15 text-red-200'
                            : a.level === 'warn'
                              ? 'border border-amber-500/40 bg-amber-500/10 text-amber-200'
                              : a.level === 'opportunity'
                                ? 'border border-emerald-500/40 bg-emerald-500/15 text-emerald-200'
                                : 'border border-zinc-700 bg-zinc-900/80 text-zinc-300'
                        }`}
                      >
                        {a.text}
                      </div>
                    ))}
                  </div>
                )}

                {/* 可视化价格标尺条 */}
                {plan?.price_gauge && (
                  <div className="mt-3">
                    <PriceGaugeBar gauge={plan.price_gauge} />
                  </div>
                )}

                {/* 实时指标 + 挂单阶梯 */}
                <div className="mt-3 grid grid-cols-2 gap-2.5 text-xs sm:grid-cols-4">
                  <div className="rounded-lg bg-zinc-900/85 p-2.5">
                    <div className="text-zinc-400">有效买入带</div>
                    <div className="mt-1 font-mono text-sm font-semibold text-emerald-300">
                      {fmtBand(eff?.buy_band)}
                    </div>
                    <div className={`mt-0.5 font-mono ${rsi5BuyHit ? 'font-semibold text-emerald-300' : 'text-zinc-400'}`}>
                      RSI5m {rsi5 != null ? rsi5.toFixed(1) : '—'} / ≤{eff?.buy_rsi5_max ?? '—'}
                    </div>
                  </div>
                  <div className="rounded-lg bg-zinc-900/85 p-2.5">
                    <div className="text-zinc-400">有效卖出带</div>
                    <div className="mt-1 font-mono text-sm font-semibold text-rose-300">
                      {fmtBand(eff?.sell_band)}
                    </div>
                    <div className={`mt-0.5 font-mono ${rsi5SellHit ? 'font-semibold text-rose-300' : 'text-zinc-400'}`}>
                      RSI5m {rsi5 != null ? rsi5.toFixed(1) : '—'} / ≥{eff?.sell_rsi5_min ?? '—'}
                    </div>
                  </div>
                  <div className="rounded-lg bg-zinc-900/85 p-2.5">
                    <div className="text-zinc-400">均线 & RSI15m</div>
                    <div className="mt-1 font-mono text-zinc-200">
                      M5 {fmtNum(c.ma5)} / M10 {fmtNum(c.ma10)}
                    </div>
                    <div className="mt-0.5 font-mono text-zinc-400">
                      RSI15m {q.rsi15m != null ? q.rsi15m.toFixed(1) : '—'} · M20 {fmtNum(c.ma20)}
                    </div>
                  </div>
                  <div className="rounded-lg bg-zinc-900/85 p-2.5">
                    <div className="text-zinc-400">T仓上限 & 风控</div>
                    <div className="mt-1 font-mono text-sm font-semibold text-amber-300">
                      建议 {plan?.recommended_lot?.toLocaleString() || c.t_lot_cap.toLocaleString()} 股
                    </div>
                    <div className="mt-0.5 text-zinc-400">
                      止损 {fmtNum(eff?.stop)} · 布防 {armedCount}
                    </div>
                  </div>
                </div>

                {/* 挂单阶梯明细 + 快捷记一笔 */}
                {plan && (plan.price_ladder.buy_levels.length > 0 || plan.price_ladder.sell_levels.length > 0) && (
                  <div className="mt-3 grid grid-cols-1 gap-2 rounded-lg bg-zinc-900/50 p-2.5 text-xs sm:grid-cols-2">
                    <div className="space-y-1">
                      <div className="font-medium text-emerald-300">买入/接回阶梯</div>
                      {plan.price_ladder.buy_levels.map((lv, i) => (
                        <div key={i} className="flex items-center justify-between gap-2 text-zinc-300">
                          <span>{lv.tier}</span>
                          <span className="font-mono text-emerald-300">
                            {fmtNum(lv.price)} {lv.lot > 0 ? `× ${lv.lot}` : '（禁正T）'}
                          </span>
                        </div>
                      ))}
                    </div>
                    <div className="space-y-1">
                      <div className="font-medium text-rose-300">卖出/反T阶梯</div>
                      {plan.price_ladder.sell_levels.map((lv, i) => (
                        <div key={i} className="flex items-center justify-between gap-2 text-zinc-300">
                          <span>{lv.tier}</span>
                          <span className="font-mono text-rose-300">
                            {fmtNum(lv.price)} × {lv.lot}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* 今日盘中情景剧本 */}
                {plan?.scenarios && plan.scenarios.length > 0 && (
                  <div className="mt-3 space-y-1.5 border-t border-zinc-800/80 pt-2.5 text-xs">
                    {plan.scenarios.map((sc, idx) => (
                      <div key={idx} className="flex items-start gap-2 text-zinc-300">
                        <Badge color={toneToBadgeColor(sc.tone === 'rose' ? 'red' : sc.tone)}>{sc.tag}</Badge>
                        <span className="leading-relaxed">{sc.text}</span>
                      </div>
                    ))}
                    {plan.cash_note && (
                      <div className="text-[11px] text-amber-300/90">资金约束：{plan.cash_note}</div>
                    )}
                  </div>
                )}

                {/* 底部：分账号持仓 + 快捷登记成交按钮 */}
                <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-zinc-800/80 pt-2.5 text-xs">
                  <div className="space-y-0.5 text-zinc-300">
                    {c.holdings.map((h, idx) => (
                      <div key={`${h.account_id}-${idx}`}>
                        <span className="font-medium text-zinc-100">{h.account_label}</span> · 持 {h.shares}（可用 {h.available_shares}） · 成本{' '}
                        <span className="font-mono">{fmtNum(h.cost_price, 3)}</span> · 浮盈{' '}
                        <span className={`font-mono ${Number(h.profit_loss || 0) >= 0 ? 'text-rose-400' : 'text-emerald-400'}`}>
                          {fmtNum(h.profit_loss, 0)} ({fmtNum(h.profit_pct)}%)
                        </span>
                      </div>
                    ))}
                  </div>
                  <div className="flex items-center gap-1.5">
                    <Button
                      outline
                      onClick={() => openExecModal(c, 'SELL', plan?.price_ladder.sell_levels[0]?.price ?? q.price, plan?.recommended_lot, '反T/高抛卖出')}
                    >
                      记一笔卖T
                    </Button>
                    <Button
                      outline
                      onClick={() => openExecModal(c, 'BUY', plan?.price_ladder.buy_levels[0]?.price ?? q.price, plan?.recommended_lot, '正T/接回买入')}
                    >
                      记一笔买T
                    </Button>
                  </div>
                </div>
              </Card>
            )
          })}
        </div>
      )}

      {/* 3. 暂不可做T / 零股持仓（折叠区，消除主屏噪音） */}
      {ineligibleCards.length > 0 && (
        <Card>
          <div className="flex items-center justify-between gap-2">
            <div className="text-sm text-zinc-300">
              暂不可做T / 零股持仓（<span className="font-mono text-white">{ineligibleCards.length}</span> 只，底仓 &lt; 300 股或无持仓）
            </div>
            <Button plain onClick={() => setShowIneligible((v) => !v)}>
              {showIneligible ? '收起' : '展开查看'}
            </Button>
          </div>
          {showIneligible && (
            <div className="mt-3 divide-y divide-zinc-800/80 border-t border-zinc-800/80 pt-2 text-xs">
              {ineligibleCards.map((c) => (
                <div key={c.code} className="flex flex-wrap items-center justify-between gap-2 py-2">
                  <div className="flex items-center gap-2">
                    <Link to={`/stock/${c.code}`} className="font-medium text-white hover:text-blue-400">
                      {c.name} <span className="font-mono text-zinc-400">{c.code}</span>
                    </Link>
                    <Badge color="zinc">{c.daily_plan?.ineligible_reason || '不可做T'}</Badge>
                  </div>
                  <div className="flex items-center gap-3 font-mono text-zinc-300">
                    <span>底仓 {c.total_shares} 股</span>
                    <span>现价 {fmtNum(c.live_quote?.price ?? c.holdings[0]?.current_price)}</span>
                    {c.in_watchlist && (
                      <Button plain onClick={() => void handleRemoveWatch(c.code)}>移出</Button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      )}

      {/* 4. 当日做T信号草案 + 今日手工做T执行台账 */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Card>
          <SectionTitle title="当日做T信号草案（快车道 / 慢车道）" />
          {!data?.drafts?.length ? (
            <div className="py-6 text-center text-sm text-zinc-400">今日暂无自动触发草案</div>
          ) : (
            <Table dense>
              <TableHead>
                <TableRow>
                  <TableHeader>时间</TableHeader>
                  <TableHeader>标的</TableHeader>
                  <TableHeader>方向</TableHeader>
                  <TableHeader>触发价</TableHeader>
                  <TableHeader>状态</TableHeader>
                  <TableHeader>操作</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {data.drafts.map((d, i) => {
                  const code = String(d.code || '')
                  const matchedCard = cards.find((x) => x.code === code)
                  const sideStr = String(d.side || '')
                  const side: 'BUY' | 'SELL' = sideStr.toLowerCase().includes('buy') ? 'BUY' : 'SELL'
                  const px = Number(d.price ?? d.trigger_price ?? d.px ?? 0)
                  return (
                    <TableRow key={i}>
                      <TableCell className="font-mono text-xs text-zinc-300">{String(d.ts || d.created_at || '—')}</TableCell>
                      <TableCell className="font-mono text-zinc-100">{code || '—'}</TableCell>
                      <TableCell>
                        <Badge color={side === 'BUY' ? 'emerald' : 'red'}>{sideStr || '—'}</Badge>
                      </TableCell>
                      <TableCell className="font-mono">{px ? fmtNum(px) : '—'}</TableCell>
                      <TableCell>
                        <Badge color="zinc">{String(d.bar_status || d.status || '—')}</Badge>
                      </TableCell>
                      <TableCell>
                        {matchedCard && (
                          <Button
                            plain
                            onClick={() => openExecModal(matchedCard, side, px || null, matchedCard.t_lot_cap, `跟单草案 ${String(d.trigger_kind || '')}`)}
                          >
                            登记执行
                          </Button>
                        )}
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          )}
        </Card>

        <Card>
          <SectionTitle title="今日做T执行台账（日内敞口闭环）" />
          {executions.length === 0 ? (
            <div className="py-6 text-center text-sm text-zinc-400">
              今日尚未登记做T成交；在上方卡片点击「记一笔卖T / 买T」即可实时追踪日内敞口与做T盈亏。
            </div>
          ) : (
            <Table dense>
              <TableHead>
                <TableRow>
                  <TableHeader>时间</TableHeader>
                  <TableHeader>标的</TableHeader>
                  <TableHeader>方向</TableHeader>
                  <TableHeader>成交价</TableHeader>
                  <TableHeader>股数</TableHeader>
                  <TableHeader>备注</TableHeader>
                  <TableHeader>操作</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {executions.map((ex) => (
                  <TableRow key={ex.id}>
                    <TableCell className="font-mono text-xs text-zinc-300">{ex.ts}</TableCell>
                    <TableCell className="text-xs text-zinc-100">
                      {ex.name} <span className="font-mono text-zinc-400">{ex.code}</span>
                    </TableCell>
                    <TableCell>
                      <Badge color={ex.side === 'BUY' ? 'emerald' : 'red'}>
                        {ex.side === 'BUY' ? '买入T' : '卖出T'}
                      </Badge>
                    </TableCell>
                    <TableCell className="font-mono text-xs">{fmtNum(ex.price)}</TableCell>
                    <TableCell className="font-mono text-xs">{ex.shares}</TableCell>
                    <TableCell className="text-xs text-zinc-400">{ex.note || '—'}</TableCell>
                    <TableCell>
                      <Button plain onClick={() => void handleDeleteExec(ex.id)}>撤销</Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Card>
      </div>
    </div>
  )
}
