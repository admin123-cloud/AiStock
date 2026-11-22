import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/catalyst/table'
import { Card, SectionTitle, StatCard } from '@/components/ui'
import {
  deleteG3TExecution,
  dispatchG3PrefillOrder,
  generateG3DailyPlan,
  getG3Desk,
  getG3PrefillStatus,
  prefillCheckG3Order,
  recordG3TExecution,
  syncG3DeskFills,
  syncG3WatchlistFromThs,
  upsertG3Watchlist,
  type G3DeskCard,
  type G3DeskResponse,
  type G3PrefillCheckResponse,
  type G3PrefillStatusResponse,
  type G3PriceGauge,
} from '@/api/g3'

/** 使用 Web Audio API 合成做T提示音（不依赖外部音频文件，支持买/卖/风控三种音色） */
function playAlertTone(kind: 'buy' | 'sell' | 'danger' | 'info' = 'info') {
  try {
    const AudioCtx = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
    if (!AudioCtx) return
    const ctx = new AudioCtx()
    const notes: Array<[number, number, number]> =
      kind === 'buy'
        ? [[660, 0, 0.14], [880, 0.16, 0.22]]
        : kind === 'sell'
          ? [[880, 0, 0.12], [988, 0.14, 0.12], [1175, 0.28, 0.24]]
          : kind === 'danger'
            ? [[520, 0, 0.15], [390, 0.18, 0.15], [520, 0.36, 0.22]]
            : [[784, 0, 0.15], [1046, 0.18, 0.18]]

    notes.forEach(([freq, startOffset, dur]) => {
      const osc = ctx.createOscillator()
      const gain = ctx.createGain()
      osc.type = kind === 'danger' ? 'sawtooth' : 'sine'
      osc.frequency.setValueAtTime(freq, ctx.currentTime + startOffset)
      gain.gain.setValueAtTime(0.001, ctx.currentTime + startOffset)
      gain.gain.exponentialRampToValueAtTime(0.18, ctx.currentTime + startOffset + 0.02)
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + startOffset + dur)
      osc.connect(gain)
      gain.connect(ctx.destination)
      osc.start(ctx.currentTime + startOffset)
      osc.stop(ctx.currentTime + startOffset + dur + 0.02)
    })
  } catch {
    // 浏览器未交互前自动播放可能受限，静默降级
  }
}

/** 触发 Windows 浏览器系统级桌面通知（即使页面在后台也能弹窗） */
function fireDesktopNotification(title: string, body: string, tag?: string): boolean {
  if (typeof window === 'undefined' || !('Notification' in window)) return false
  if (Notification.permission !== 'granted') return false
  try {
    const n = new Notification(title, {
      body,
      tag: tag || `g3-desk-${Date.now()}`,
      requireInteraction: true,
    })
    n.onclick = () => {
      window.focus()
      n.close()
    }
    return true
  } catch {
    return false
  }
}

const fmtTimeOnly = (raw?: unknown): string => {
  const s = String(raw || '').trim()
  if (!s) return '—'
  if (s.includes(' ')) return s.split(' ')[1]?.slice(0, 8) || s
  if (s.includes('T')) return s.split('T')[1]?.slice(0, 8) || s
  return s.slice(0, 8)
}

const draftStatusBadge = (statusRaw: string) => {
  if (statusRaw === 'open') return { label: '待确认', color: 'amber' as const }
  if (statusRaw === 'confirmed_bar') return { label: '已确认', color: 'emerald' as const }
  if (statusRaw === 'expired_unconfirmed') return { label: '已过期', color: 'zinc' as const }
  return { label: statusRaw || '—', color: 'zinc' as const }
}

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
  order_type: 't_trade' | 'base_rebalance'
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
  order_type: 't_trade',
  price: '',
  shares: '100',
  account_label: '',
  note: '',
}

interface LiveAlertBanner {
  id: string
  tone: 'buy' | 'sell' | 'danger' | 'info'
  title: string
  body: string
  ts: string
  code?: string
  side?: 'BUY' | 'SELL'
  price?: number | null
}

export default function G3TTradeWorkbench() {
  const [data, setData] = useState<G3DeskResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [prefillBusy, setPrefillBusy] = useState(false)
  const [prefillRes, setPrefillRes] = useState<G3PrefillCheckResponse | null>(null)
  const [dispatchingPrefill, setDispatchingPrefill] = useState(false)
  const [prefillExecStatus, setPrefillExecStatus] = useState<G3PrefillStatusResponse | null>(null)
  const [syncingFills, setSyncingFills] = useState(false)
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [countdown, setCountdown] = useState(15)
  const [showIneligible, setShowIneligible] = useState(false)
  const [newCode, setNewCode] = useState('')
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')
  const [execForm, setExecForm] = useState<ExecFormState>(EMPTY_EXEC_FORM)
  const [submittingExec, setSubmittingExec] = useState(false)

  // Windows 桌面通知与提示音（微信配置已迁至「策略研投与设置 → 参数与通知设置」）
  const [desktopPerm, setDesktopPerm] = useState<NotificationPermission | 'unsupported'>(() =>
    typeof window !== 'undefined' && 'Notification' in window ? Notification.permission : 'unsupported'
  )
  const [soundEnabled, setSoundEnabled] = useState<boolean>(() => {
    try {
      return localStorage.getItem('g3_desk_sound') !== '0'
    } catch {
      return true
    }
  })
  const [liveBanner, setLiveBanner] = useState<LiveAlertBanner | null>(null)

  const initializedRef = useRef(false)
  const seenDraftIdsRef = useRef<Set<string>>(new Set())
  const alertCooldownRef = useRef<Map<string, number>>(new Map())
  const baseTitleRef = useRef<string>(typeof document !== 'undefined' ? document.title : 'AiStock')

  const triggerSignalAlert = useCallback(
    (banner: LiveAlertBanner) => {
      setLiveBanner(banner)
      if (soundEnabled) {
        playAlertTone(banner.tone)
      }
      fireDesktopNotification(banner.title, banner.body, banner.id)
      if (typeof document !== 'undefined') {
        document.title = `🔔 ${banner.title}`
      }
    },
    [soundEnabled]
  )

  useEffect(() => {
    const onFocus = () => {
      if (typeof document !== 'undefined') {
        document.title = baseTitleRef.current
      }
    }
    window.addEventListener('focus', onFocus)
    return () => {
      window.removeEventListener('focus', onFocus)
      if (typeof document !== 'undefined') {
        document.title = baseTitleRef.current
      }
    }
  }, [])

  const load = useCallback(async (refreshQuotes = false, silent = false) => {
    if (!silent) setLoading(true)
    setError('')
    try {
      const res = await getG3Desk(refreshQuotes)
      setData(res)

      const draftsList = res.drafts || []
      if (!initializedRef.current) {
        draftsList.forEach((d, idx) => {
          const id = String(d.id || `${d.ts || ''}_${d.code || ''}_${idx}`)
          seenDraftIdsRef.current.add(id)
        })
        initializedRef.current = true
        return
      }

      for (let i = 0; i < draftsList.length; i++) {
        const d = draftsList[i]
        const id = String(d.id || `${d.ts || ''}_${d.code || ''}_${i}`)
        if (seenDraftIdsRef.current.has(id)) continue
        seenDraftIdsRef.current.add(id)
        const status = String(d.bar_status || d.status || '')
        if (status === 'expired_unconfirmed') continue
        const code = String(d.code || '')
        const card = (res.cards || []).find((x) => x.code === code)
        const name = String(d.name || card?.name || code)
        const sideStr = String(d.side || '')
        const isBuy = sideStr.toLowerCase().includes('buy')
        const px = Number(d.price ?? d.trigger_price ?? d.px ?? 0)
        const qty = Number(d.qty || card?.daily_plan?.recommended_lot || card?.t_lot_cap || 0)
        triggerSignalAlert({
          id,
          tone: isBuy ? 'buy' : 'sell',
          title: `${isBuy ? '买入T信号' : '卖出T信号'} · ${name}(${code}) @ ${px ? px.toFixed(2) : '—'}`,
          body: `建议 ${isBuy ? '买入' : '卖出'} ${qty || '—'} 股 · 触发时间 ${fmtTimeOnly(d.ts || d.created_at)}`,
          ts: fmtTimeOnly(d.ts || d.created_at),
          code,
          side: isBuy ? 'BUY' : 'SELL',
          price: px || null,
        })
      }

      const nowMs = Date.now()
      for (const c of res.cards || []) {
        const plan = c.daily_plan
        if (!plan?.eligible) continue
        const px = c.live_quote?.price
        const zone = plan.price_gauge?.zone
        if (zone === 'below_stop' && px) {
          const key = `stop_${c.code}`
          if (nowMs - (alertCooldownRef.current.get(key) || 0) > 5 * 60 * 1000) {
            alertCooldownRef.current.set(key, nowMs)
            triggerSignalAlert({
              id: `${key}_${nowMs}`,
              tone: 'danger',
              title: `⚠️ 止损风控预警 · ${c.name}(${c.code}) @ ${px.toFixed(2)}`,
              body: `现价已低于风控止损线 ${fmtNum(c.effective_params?.stop)}，请严禁低吸并关注防守`,
              ts: c.live_quote?.quote_ts || new Date().toTimeString().slice(0, 8),
              code: c.code,
              side: 'SELL',
              price: px,
            })
          }
        }
      }
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '加载做T工作台失败')
    } finally {
      if (!silent) setLoading(false)
    }
  }, [triggerSignalAlert])

  const handleRequestDesktopPermission = async () => {
    if (typeof window === 'undefined' || !('Notification' in window)) {
      setError('当前浏览器不支持系统桌面通知，请使用 Chrome / Edge 打开')
      return
    }
    const perm = await Notification.requestPermission()
    setDesktopPerm(perm)
    if (perm === 'granted') {
      setMsg('已开启 Windows 桌面通知！即使浏览器切到后台看同花顺也能弹出提醒')
      fireDesktopNotification('AiStock 底仓做T工作台', 'Windows 桌面通知已就绪，触发做T信号时将实时弹出提醒！', 'g3-perm-ok')
      if (soundEnabled) playAlertTone('info')
    } else if (perm === 'denied') {
      setError('浏览器已拦截通知权限，请点击地址栏左侧锁图标 → 允许「通知」')
    }
  }

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
    setPrefillRes(null)
    setPrefillExecStatus(null)
    setExecForm({
      open: true,
      code: card.code,
      name: card.name,
      side,
      order_type: 't_trade',
      price: px ? Number(px).toFixed(2) : '',
      shares: String(lot),
      account_label: card.holdings[0]?.account_label || '',
      note,
    })
  }

  const handlePrefillCheck = async () => {
    const price = Number(execForm.price)
    const shares = Number(execForm.shares)
    if (!execForm.code || !Number.isFinite(price) || price <= 0 || !Number.isFinite(shares) || shares < 100 || shares % 100 !== 0) {
      setError('请先填写有效的委托价与整百股数再执行 L2 风控预检')
      return
    }
    setPrefillBusy(true)
    setPrefillExecStatus(null)
    setError('')
    try {
      const res = await prefillCheckG3Order({
        code: execForm.code,
        name: execForm.name,
        side: execForm.side,
        price,
        shares,
        order_type: execForm.order_type,
      })
      setPrefillRes(res)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || 'L2 风控预检失败')
    } finally {
      setPrefillBusy(false)
    }
  }

  const handleDispatchPrefill = async (draftId: string) => {
    setDispatchingPrefill(true)
    setError('')
    setMsg('')
    try {
      const dis = await dispatchG3PrefillOrder(draftId)
      if (dis.status === 'already_processed') {
        setPrefillExecStatus({
          draft_id: draftId,
          state: 'done',
          bridge: dis.bridge,
          result: (dis.result as G3PrefillStatusResponse['result']) || null,
        })
        setMsg(`该放行码（${draftId}）已由宿主机处理完毕（幂等保护，不重复填单）`)
        return
      }
      setPrefillExecStatus({
        draft_id: draftId,
        state: 'queued',
        bridge: dis.bridge,
        result: null,
      })
      // 轮询宿主机预填结果（最长约 12 秒）
      for (let i = 0; i < 10; i++) {
        await new Promise((resolve) => window.setTimeout(resolve, 1200))
        const st = await getG3PrefillStatus(draftId)
        setPrefillExecStatus(st)
        if (st.state === 'done') {
          if (st.result?.ok) {
            setMsg(`已预填至同花顺远航下单框（仅填单未提交），请切换到同花顺人工核对后点击确认！`)
          } else {
            setError(`宿主机预填未完成：${st.result?.message || st.result?.reason || '请检查同花顺远航窗口是否已打开'}`)
          }
          return
        }
      }
      setError('预填指令已入队，但 12 秒内未收到宿主机回执，请确认宿主机 g3_prefill_bridge 已在线运行')
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '派发同花顺预填失败')
    } finally {
      setDispatchingPrefill(false)
    }
  }

  const handleSyncFills = async () => {
    setSyncingFills(true)
    setError('')
    setMsg('')
    try {
      const res = await syncG3DeskFills()
      setMsg(
        `同花顺当日成交同步完成：共扫描 ${res.broker_fills_found} 笔，新入账 ${res.imported} 笔（幂等去重跳过 ${res.idempotent_skipped} 笔）`
      )
      await load(false, false)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '同步同花顺成交回执失败')
    } finally {
      setSyncingFills(false)
    }
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
        order_type: execForm.order_type,
        price,
        shares,
        account_label: execForm.account_label,
        note: execForm.note,
      })
      const typeLabel = execForm.order_type === 'base_rebalance' ? 'AR4.5-L3b调仓' : '做T'
      setMsg(`已登记${typeLabel}成交：${execForm.name} ${execForm.side === 'BUY' ? '买入' : '卖出'} ${shares} 股 @ ${price.toFixed(2)}`)
      setExecForm(EMPTY_EXEC_FORM)
      setPrefillRes(null)
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
    <div className="flex w-full flex-col gap-5">
      {/* 底仓做T专属控制条 */}
      <Card className="border-zinc-800/90 bg-zinc-900/70">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-semibold text-white">底仓做T 实时作战与执行台账</span>
              <Badge color="blue">规划日 {data?.trade_date || '—'}</Badge>
              <Badge color={data?.market_regime_gate?.regime === 'defensive' ? 'amber' : 'emerald'}>
                {data?.market_regime_gate?.regime === 'defensive' ? '防守期仅允许高抛反T / 严禁低吸' : '按个股周期做T'}
              </Badge>
            </div>
            <p className="mt-1 text-xs text-zinc-400">
              针对双账号已有底仓（满足 ≥300 股）提供实时 5m RSI 标尺、止损红线预警、L2 风控预检与同花顺远航预填（仅填单不提交）。
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button onClick={handleGeneratePlan} disabled={generating}>
              {generating ? '生成中…' : '生成/更新今日做T规划'}
            </Button>
            <Button
              outline
              onClick={() => {
                if (desktopPerm !== 'granted') {
                  void handleRequestDesktopPermission()
                } else {
                  const next = !soundEnabled
                  setSoundEnabled(next)
                  try {
                    localStorage.setItem('g3_desk_sound', next ? '1' : '0')
                  } catch {
                    // ignore
                  }
                  if (next) playAlertTone('info')
                }
              }}
            >
              {desktopPerm !== 'granted'
                ? '🔔 开启桌面弹窗'
                : soundEnabled
                  ? '🔊 提示音：开'
                  : '🔇 提示音：关'}
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
              {syncing ? '同步中…' : '同步同花顺观察池'}
            </Button>
            <Button outline onClick={() => void load(true, false)} disabled={loading}>
              {loading ? '刷新中…' : '刷新行情'}
            </Button>
            <Link to="/g3/lab?tab=settings">
              <Button plain>微信推送设置 →</Button>
            </Link>
          </div>
        </div>
      </Card>

      {/* 实时做T信号顶部强提醒横幅 */}
      {liveBanner && (
        <div
          className={`flex flex-wrap items-center justify-between gap-3 rounded-lg border px-4 py-3 text-sm shadow-lg ${
            liveBanner.tone === 'buy'
              ? 'border-emerald-400/70 bg-emerald-500/20 text-emerald-100'
              : liveBanner.tone === 'sell'
                ? 'border-rose-400/70 bg-rose-500/20 text-rose-100'
                : 'border-red-400/70 bg-red-500/20 text-red-100'
          }`}
        >
          <div>
            <div className="font-semibold">🔔 {liveBanner.title}</div>
            <div className="mt-0.5 text-xs opacity-90">{liveBanner.body}</div>
          </div>
          <div className="flex items-center gap-2">
            {liveBanner.code && (
              <Button
                onClick={() => {
                  const matched = cards.find((x) => x.code === liveBanner.code)
                  if (matched) {
                    openExecModal(matched, liveBanner.side || 'SELL', liveBanner.price, matched.daily_plan?.recommended_lot, '实时通知跟单')
                  }
                  setLiveBanner(null)
                  if (typeof document !== 'undefined') document.title = baseTitleRef.current
                }}
              >
                去登记执行
              </Button>
            )}
            <Button
              outline
              onClick={() => {
                setLiveBanner(null)
                if (typeof document !== 'undefined') document.title = baseTitleRef.current
              }}
            >
              知道了
            </Button>
          </div>
        </div>
      )}

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

      {/* 1. 核心实盘作战指标（已移除与大盘总闸门重复的旧做T总纲卡） */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
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

      {/* 快捷手工成交登记 + Phase 3B R51分型 + Phase 3C L2风控预检面板 */}
      {execForm.open && (
        <Card className="border-blue-500/50 bg-blue-950/20">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800 pb-2.5">
            <div className="text-sm font-semibold text-white">
              登记当日成交 / L2 风控预检 · {execForm.name} ({execForm.code})
            </div>
            <Button plain onClick={() => { setExecForm(EMPTY_EXEC_FORM); setPrefillRes(null) }}>取消</Button>
          </div>
          <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-7">
            <div>
              <label className="block text-xs text-zinc-400">单据分型（AR4.5-L3b）</label>
              <select
                value={execForm.order_type}
                onChange={(e) => { setExecForm((f) => ({ ...f, order_type: e.target.value as 't_trade' | 'base_rebalance' })); setPrefillRes(null) }}
                className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-sm text-white"
              >
                <option value="t_trade">做T滚动单 (t_trade)</option>
                <option value="base_rebalance">AR4.5-L3b 精准调仓 (rebalance)</option>
              </select>
            </div>
            <div>
              <label className="block text-xs text-zinc-400">方向</label>
              <select
                value={execForm.side}
                onChange={(e) => { setExecForm((f) => ({ ...f, side: e.target.value as 'BUY' | 'SELL' })); setPrefillRes(null) }}
                className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-sm text-white"
              >
                <option value="SELL">卖出（先卖/高抛/减水位）</option>
                <option value="BUY">买入（低吸/接回/加底仓）</option>
              </select>
            </div>
            <div>
              <label className="block text-xs text-zinc-400">价格</label>
              <input
                type="number"
                step="0.01"
                value={execForm.price}
                onChange={(e) => { setExecForm((f) => ({ ...f, price: e.target.value })); setPrefillRes(null) }}
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
                onChange={(e) => { setExecForm((f) => ({ ...f, shares: e.target.value })); setPrefillRes(null) }}
                className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 font-mono text-sm text-white"
              />
            </div>
            <div>
              <label className="block text-xs text-zinc-400">备注（选填）</label>
              <input
                type="text"
                value={execForm.note}
                onChange={(e) => setExecForm((f) => ({ ...f, note: e.target.value }))}
                placeholder="如：反T卖1 / AR4.5减仓"
                className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-sm text-white"
              />
            </div>
            <div className="flex items-end">
              <Button outline onClick={() => void handlePrefillCheck()} disabled={prefillBusy} className="w-full">
                {prefillBusy ? '预检中…' : 'L2 风控预检'}
              </Button>
            </div>
            <div className="flex items-end">
              <Button onClick={handleSubmitExec} disabled={submittingExec} className="w-full">
                {submittingExec ? '提交中…' : '确认登记'}
              </Button>
            </div>
          </div>

          {prefillRes && (
            <div
              className={`mt-3 rounded-lg border p-3 text-xs ${
                prefillRes.approved
                  ? 'border-emerald-500/40 bg-emerald-950/20'
                  : 'border-red-500/40 bg-red-950/20'
              }`}
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge color={prefillRes.approved ? 'emerald' : 'red'}>
                    {prefillRes.approved ? 'L2 预检通过 · 人工核对后下单' : 'L2 硬风控拦截'}
                  </Badge>
                  <span className="font-mono text-zinc-300">放行码 {prefillRes.draft_id}</span>
                  <Badge color="blue">{prefillRes.order.order_type_label}</Badge>
                </div>
                <span className="font-mono text-zinc-300">
                  预计金额 ¥{fmtNum(prefillRes.order.amount)}
                </span>
              </div>
              <p className="mt-1.5 font-medium text-zinc-100">{prefillRes.summary}</p>
              <div className="mt-2 grid grid-cols-1 gap-1 sm:grid-cols-2">
                {prefillRes.checks.map((c, idx) => (
                  <div key={idx} className="flex items-center gap-1.5 text-[11px]">
                    <span>{c.ok ? '✅' : c.hard ? '⛔' : '⚠️'}</span>
                    <span className={c.ok ? 'text-zinc-300' : c.hard ? 'text-red-300' : 'text-amber-200'}>
                      {c.detail}
                    </span>
                  </div>
                ))}
              </div>

              {/* Phase 3C 进阶：一键派发宿主机同花顺远航预填（强制 prefill_only，永不自动提交） */}
              {prefillRes.approved && (
                <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-emerald-500/30 pt-2.5">
                  <div className="flex flex-wrap items-center gap-2 text-[11px]">
                    <Badge color={data?.prefill_bridge?.online ? 'emerald' : 'amber'}>
                      {data?.prefill_bridge?.online ? '预填桥在线' : '预填桥离线'}
                    </Badge>
                    <span className="text-zinc-300">
                      安全边界：仅填入代码 / 价格 / 股数，绝不点击「下单确认」
                    </span>
                  </div>
                  <Button
                    onClick={() => void handleDispatchPrefill(prefillRes.draft_id)}
                    disabled={dispatchingPrefill || !data?.prefill_bridge?.online}
                  >
                    {dispatchingPrefill ? '正在预填至同花顺…' : '一键预填至同花顺（仅填单不提交）'}
                  </Button>
                </div>
              )}

              {prefillExecStatus && prefillExecStatus.draft_id === prefillRes.draft_id && (
                <div
                  className={`mt-2 rounded-md border px-2.5 py-2 text-[11px] ${
                    prefillExecStatus.state === 'done' && prefillExecStatus.result?.ok
                      ? 'border-emerald-400/50 bg-emerald-500/15 text-emerald-100'
                      : prefillExecStatus.state === 'queued'
                        ? 'border-blue-400/40 bg-blue-500/15 text-blue-100'
                        : 'border-amber-400/50 bg-amber-500/15 text-amber-100'
                  }`}
                >
                  {prefillExecStatus.state === 'queued' && (
                    <span>⏳ 指令已入宿主机预填队列，等待同花顺远航桌面桥执行填单…</span>
                  )}
                  {prefillExecStatus.state === 'done' && prefillExecStatus.result?.ok && (
                    <span>
                      ✅ 预填完成（状态：{String(prefillExecStatus.result.status || 'prefilled_awaiting_human_confirm')}）— 请切换至同花顺远航窗口人工核对后手动提交。
                    </span>
                  )}
                  {prefillExecStatus.state === 'done' && !prefillExecStatus.result?.ok && (
                    <span>
                      ⚠️ 预填未通过/异常：{String(prefillExecStatus.result?.message || prefillExecStatus.result?.reason || '请检查同花顺窗口状态')}
                    </span>
                  )}
                </div>
              )}
            </div>
          )}
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
                      {c.ai_sentinel ? (
                        <Badge
                          color={
                            c.ai_sentinel.risk_light === 'red'
                              ? 'red'
                              : c.ai_sentinel.risk_light === 'yellow'
                                ? 'amber'
                                : 'emerald'
                          }
                        >
                          {c.ai_sentinel.forbid_forward_t
                            ? '⛔ AI哨兵·利空禁正T'
                            : c.ai_sentinel.risk_light === 'yellow'
                              ? '⚠️ AI哨兵·留意风险'
                              : '🛡️ AI哨兵·无雷'}
                        </Badge>
                      ) : (
                        <Badge color="zinc">AI哨兵·待排雷</Badge>
                      )}
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

                {/* AI 公告/事件哨兵详情条（与 /g3/mainwave 共用排雷库） */}
                {c.ai_sentinel && (
                  <div
                    className={`mt-2.5 rounded-lg border px-3 py-2 text-xs ${
                      c.ai_sentinel.forbid_forward_t
                        ? 'border-red-500/50 bg-red-950/30 text-red-200'
                        : c.ai_sentinel.risk_light === 'yellow'
                          ? 'border-amber-500/40 bg-amber-950/20 text-amber-200'
                          : 'border-emerald-500/30 bg-emerald-950/15 text-emerald-200'
                    }`}
                  >
                    <div className="flex flex-wrap items-center justify-between gap-1">
                      <span className="font-medium">
                        {c.ai_sentinel.forbid_forward_t
                          ? '⛔ AI 事件哨兵拦截：触发基本面/公告红灯，今日严禁正T低吸防接飞刀'
                          : `🛡️ AI 事件哨兵（催化 ${c.ai_sentinel.catalyst_score}/5 · ${c.ai_sentinel.catalyst_type || '已核查'}）`}
                      </span>
                      <span className="font-mono text-[11px] opacity-80">
                        {c.ai_sentinel.decision_date || ''}
                      </span>
                    </div>
                    <p className="mt-1 leading-relaxed text-zinc-200">{c.ai_sentinel.one_line_thesis}</p>
                    {c.ai_sentinel.risk_flags.length > 0 && (
                      <div className="mt-1 text-[11px] text-amber-300">
                        风险标签：{c.ai_sentinel.risk_flags.join('；')}
                      </div>
                    )}
                  </div>
                )}

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
                  const stockName = String(d.name || matchedCard?.name || '')
                  const sideStr = String(d.side || '')
                  const side: 'BUY' | 'SELL' = sideStr.toLowerCase().includes('buy') ? 'BUY' : 'SELL'
                  const px = Number(d.price ?? d.trigger_price ?? d.px ?? 0)
                  const st = draftStatusBadge(String(d.bar_status || d.status || ''))
                  return (
                    <TableRow key={i}>
                      <TableCell className="font-mono text-xs text-zinc-300">{fmtTimeOnly(d.ts || d.created_at)}</TableCell>
                      <TableCell className="text-xs text-zinc-100">
                        {stockName ? `${stockName} ` : ''}
                        <span className="font-mono text-zinc-400">{code || '—'}</span>
                      </TableCell>
                      <TableCell>
                        <Badge color={side === 'BUY' ? 'emerald' : 'red'}>{side === 'BUY' ? '买入T' : '卖出T'}</Badge>
                      </TableCell>
                      <TableCell className="font-mono text-xs">{px ? fmtNum(px) : '—'}</TableCell>
                      <TableCell>
                        <Badge color={st.color}>{st.label}</Badge>
                      </TableCell>
                      <TableCell>
                        {matchedCard && (
                          <Button
                            plain
                            onClick={() => openExecModal(matchedCard, side, px || null, matchedCard.t_lot_cap, `跟单草案 ${String(d.trigger_kind || '')}`)}
                          >
                            登记
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
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <SectionTitle title="今日做T执行台账（日内敞口闭环 · 回执自动入账）" />
            <Button outline onClick={() => void handleSyncFills()} disabled={syncingFills}>
              {syncingFills ? '同步回执中…' : '同步同花顺成交回执'}
            </Button>
          </div>
          {executions.length === 0 ? (
            <div className="py-6 text-center text-sm text-zinc-400">
              今日尚未登记做T成交；同花顺成交后会自动/一键入账，也可在上方卡片点击「记一笔卖T / 买T」手工补录。
            </div>
          ) : (
            <Table dense>
              <TableHead>
                <TableRow>
                  <TableHeader>时间</TableHeader>
                  <TableHeader>标的</TableHeader>
                  <TableHeader>方向 / 分型</TableHeader>
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
                      <div className="flex flex-wrap items-center gap-1">
                        <Badge color={ex.side === 'BUY' ? 'emerald' : 'red'}>
                          {ex.side === 'BUY' ? '买入' : '卖出'}
                        </Badge>
                        <Badge color={ex.order_type === 'base_rebalance' ? 'amber' : 'blue'}>
                          {ex.order_type === 'base_rebalance' ? 'AR4.5调仓' : '做T'}
                        </Badge>
                      </div>
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
