import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/catalyst/table'
import { triggerAccountsSync } from '@/api/accounts'
import {
  batchPrefillG3AR3Orders,
  confirmG3Acct1Fill,
  getG3Acct1AdvisorStatus,
  getG3AR3RebalancePlan,
  getG3PrefillStatus,
  pushG3AR3PlanWechat,
  type G3Acct1AdvisorStatusResponse,
  type G3AR3BatchPrefillResponse,
  type G3AR3RebalancePlanResponse,
  type G3DecisionHoldingRow,
  type G3DecisionPendingBuyRow,
  type G3PrefillStatusResponse,
} from '@/api/g3'

interface AR3ExecutionOrderCardProps {
  reloadToken?: number
  externalPlan?: G3AR3RebalancePlanResponse | null
  onPlanUpdated?: (plan: G3AR3RebalancePlanResponse) => void
}

const fmtYuan = (v?: number | null): string =>
  v != null && Number.isFinite(Number(v)) ? `¥${Math.round(Number(v)).toLocaleString('zh-CN')}` : '—'

const fmtPx = (v?: number | null, isEtf = false): string =>
  v != null && Number.isFinite(Number(v)) ? `¥${Number(v).toFixed(isEtf ? 3 : 2)}` : '—'

const toneToBadge = (tone?: string) => {
  if (tone === 'emerald') return 'emerald' as const
  if (tone === 'red') return 'red' as const
  if (tone === 'amber') return 'amber' as const
  if (tone === 'blue') return 'blue' as const
  return 'zinc' as const
}

export default function AR3ExecutionOrderCard({
  reloadToken = 0,
  externalPlan,
  onPlanUpdated,
}: AR3ExecutionOrderCardProps) {
  const [accountId, setAccountId] = useState<string>('acct1')
  const [smoothStep, setSmoothStep] = useState<boolean>(true)
  const [plan, setPlan] = useState<G3AR3RebalancePlanResponse | null>(externalPlan ?? null)
  const [advisorStatus, setAdvisorStatus] = useState<G3Acct1AdvisorStatusResponse | null>(null)
  const [edits, setEdits] = useState<Record<string, { shares: number; price: number }>>({})
  const [loading, setLoading] = useState(false)
  const [singleBusyKey, setSingleBusyKey] = useState<string | null>(null)
  const [confirmBusyKey, setConfirmBusyKey] = useState<string | null>(null)
  const [pushingWx, setPushingWx] = useState(false)
  const [syncingBroker, setSyncingBroker] = useState(false)
  const [batchRes, setBatchRes] = useState<G3AR3BatchPrefillResponse | null>(null)
  const [prefillLiveMap, setPrefillLiveMap] = useState<Record<string, G3PrefillStatusResponse>>({})
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')

  const pollPrefillDrafts = useCallback(async (draftIds: string[]) => {
    const uniqueIds = Array.from(new Set(draftIds.filter(Boolean)))
    if (uniqueIds.length === 0) return
    const pending = new Set(uniqueIds)
    for (let tick = 0; tick < 95 && pending.size > 0; tick += 1) {
      await new Promise((r) => window.setTimeout(r, tick === 0 ? 700 : 1000))
      const results = await Promise.all(
        Array.from(pending).map(async (id) => {
          try {
            const st = await getG3PrefillStatus(id)
            return [id, st] as const
          } catch {
            return null
          }
        }),
      )
      let anyDone = false
      let lastOkMsg = ''
      let lastFailMsg = ''
      for (const item of results) {
        if (!item) continue
        const [id, st] = item
        setPrefillLiveMap((prev) => ({ ...prev, [id]: st }))
        if (st.state === 'done') {
          pending.delete(id)
          anyDone = true
          if (st.result?.ok) {
            lastOkMsg = st.result.message || '已精准填入同花顺，请切到同花顺点击确认'
          } else {
            lastFailMsg = st.result?.message || st.result?.reason || '同花顺填单未成功'
          }
        }
      }
      if (anyDone && pending.size === 0) {
        if (lastFailMsg) {
          setMsg('')
          setErr(`填单失败：${lastFailMsg}`)
        } else if (lastOkMsg) {
          setErr('')
          setMsg(`✅ ${lastOkMsg}`)
        }
        break
      }
    }
  }, [])

  const loadAdvisorStatus = useCallback(async () => {
    try {
      const st = await getG3Acct1AdvisorStatus()
      setAdvisorStatus(st)
    } catch {
      // 静默忽略定时轮询抖动
    }
  }, [])

  const loadPlan = useCallback(
    async (aid: string, smooth: boolean, silent = false) => {
      if (!silent) {
        setLoading(true)
        setErr('')
      }
      try {
        const p = await getG3AR3RebalancePlan(aid, smooth)
        setPlan(p)
        if (aid === 'acct1') onPlanUpdated?.(p)
      } catch (e) {
        if (!silent) {
          const ex = e as { response?: { data?: { detail?: string } }; message?: string }
          setErr(ex.response?.data?.detail || ex.message || '加载 AI 持仓与待买决策表失败')
        }
      } finally {
        if (!silent) setLoading(false)
      }
    },
    [onPlanUpdated],
  )

  useEffect(() => {
    const canReuseExternal =
      externalPlan !== undefined &&
      accountId === 'acct1' &&
      smoothStep === true &&
      reloadToken === 0
    if (canReuseExternal && externalPlan) {
      setPlan(externalPlan)
      return
    }
    void loadPlan(accountId, smoothStep)
  }, [accountId, smoothStep, reloadToken, externalPlan, loadPlan])

  // 每 60 秒自动同步 AI 10 分钟巡检结果与实时决策表（无需手动刷新）
  useEffect(() => {
    void loadAdvisorStatus()
    const timer = window.setInterval(() => {
      void loadAdvisorStatus()
      void loadPlan(accountId, smoothStep, true)
    }, 60000)
    return () => window.clearInterval(timer)
  }, [accountId, smoothStep, loadAdvisorStatus, loadPlan])

  const getRowDraft = (key: string, defaultShares: number, defaultPrice: number) =>
    edits[key] ?? { shares: defaultShares, price: defaultPrice }

  const setRowDraft = (key: string, patch: Partial<{ shares: number; price: number }>, baseShares: number, basePrice: number) => {
    setEdits((prev) => {
      const cur = prev[key] ?? { shares: baseShares, price: basePrice }
      return { ...prev, [key]: { ...cur, ...patch } }
    })
  }

  const handleSinglePrefill = async (o: {
    key: string
    code: string
    name: string
    side: 'SELL' | 'BUY'
    price: number
    shares: number
  }) => {
    if (o.shares < 100 || o.price <= 0) {
      setErr('单笔预填要求股数 ≥ 100 且价格 > 0')
      return
    }
    setSingleBusyKey(o.key)
    setErr('')
    setMsg('')
    setBatchRes(null)
    try {
      const res = await batchPrefillG3AR3Orders({
        account_id: accountId,
        auto_split_l2: true,
        orders: [
          {
            code: o.code,
            name: o.name,
            side: o.side,
            price: o.price,
            shares: o.shares,
            order_type: 'base_rebalance',
          },
        ],
      })
      setBatchRes(res)
      const first = res.results[0]
      if (res.dispatched_count > 0 && first?.draft_id) {
        setMsg(`⏳ 已派发 ${o.side === 'SELL' ? '卖出' : '买入'} ${o.name}(${o.code}) ${o.shares}股 @ ${o.price}，正在自动唤醒同花顺填单核验…`)
        void pollPrefillDrafts([String(first.draft_id)])
      } else {
        const reason = first?.blockers?.join('；') || '风控预检未通过'
        setErr(`单笔填单未放行：${reason}`)
      }
    } catch (e) {
      const ex = e as { response?: { data?: { detail?: string } }; message?: string }
      setErr(ex.response?.data?.detail || ex.message || '单笔预填派发失败')
    } finally {
      setSingleBusyKey(null)
    }
  }

  const handleConfirmFill = async (o: {
    key: string
    code: string
    name: string
    side: 'SELL' | 'BUY'
    price: number
    shares: number
  }) => {
    const label = `${o.side === 'SELL' ? '卖出' : '买入'} ${o.name}(${o.code}) ${o.shares}股 @ ¥${o.price}`
    if (!window.confirm(`确认已在同花顺成交以下委托并立即校准账本？\n\n${label}`)) return
    setConfirmBusyKey(o.key)
    setErr('')
    setMsg('')
    try {
      await confirmG3Acct1Fill({
        code: o.code,
        name: o.name,
        side: o.side,
        shares: o.shares,
        price: o.price,
        note: '实战交易台一键登记成交',
      })
      setMsg(`✅ 已登记成交：${label}，持仓与可用现金已立即刷新。`)
      await Promise.all([loadPlan(accountId, smoothStep), loadAdvisorStatus()])
    } catch (e) {
      const ex = e as { response?: { data?: { detail?: string } }; message?: string }
      setErr(ex.response?.data?.detail || ex.message || '登记成交失败')
    } finally {
      setConfirmBusyKey(null)
    }
  }

  const handleSyncBroker = async () => {
    setSyncingBroker(true)
    setErr('')
    setMsg('')
    try {
      const res = await triggerAccountsSync([accountId], true)
      if (!res.bridge?.online) {
        setErr('同花顺桌面桥当前离线，请检查宿主机 AiStock_THSDesktopBridge 任务。')
      } else {
        setMsg(`⏳ 已派发同花顺只读同步（${res.request_id}），约 30~60 秒完成，页面将自动刷新实盘持仓…`)
        window.setTimeout(() => void loadPlan(accountId, smoothStep, true), 15000)
        window.setTimeout(() => void loadPlan(accountId, smoothStep, true), 40000)
        window.setTimeout(() => void loadPlan(accountId, smoothStep, true), 65000)
      }
    } catch (e) {
      const ex = e as { response?: { data?: { detail?: string } }; message?: string }
      setErr(ex.response?.data?.detail || ex.message || '触发同花顺实盘同步失败')
    } finally {
      setSyncingBroker(false)
    }
  }

  const handlePushWechat = async () => {
    setPushingWx(true)
    setErr('')
    setMsg('')
    try {
      const res = await pushG3AR3PlanWechat(accountId, smoothStep)
      if (res.status === 'ok') {
        setMsg(`已将 ${res.plan_date || '今日'} AR4.6 调仓单推送到你的微信。`)
      } else {
        setErr(`微信推送未成功：${JSON.stringify(res.wechat)}`)
      }
    } catch (e) {
      const ex = e as { response?: { data?: { detail?: string } }; message?: string }
      setErr(ex.response?.data?.detail || ex.message || '微信推送失败')
    } finally {
      setPushingWx(false)
    }
  }

  const acct = plan?.account
  const decisionHoldings: G3DecisionHoldingRow[] = plan?.decision_holdings ?? []
  const pendingBuys: G3DecisionPendingBuyRow[] = plan?.decision_pending_buys ?? []
  const lastPollAt = advisorStatus?.last_poll_at || plan?.portfolio_compliance?.live_poll_at || null
  const liveTriggeredCount =
    decisionHoldings.filter((r) => r.live_10m_triggered).length +
    pendingBuys.filter((r) => r.live_10m_triggered).length

  return (
    <div className="space-y-4 text-sm">
      {/* 顶栏：账号状态 + AI 10 分钟巡检同步状态 + 轻量操作 */}
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-zinc-800 bg-zinc-950/75 px-4 py-3">
        <div className="flex flex-wrap items-center gap-2.5">
          <Badge color={liveTriggeredCount > 0 ? 'amber' : 'emerald'}>
            {liveTriggeredCount > 0 ? `⚡ 10m 实时触发 ${liveTriggeredCount} 笔` : '⏱️ AI 每10分钟动态巡检'}
          </Badge>
          {acct && acct.available && (
            <span className="font-mono text-sm text-zinc-100">
              总资产 <b>{fmtYuan(acct.total_assets)}</b> · 可用{' '}
              <b className="text-emerald-300">{fmtYuan(acct.available_cash)}</b> · 仓位{' '}
              <b>{acct.position_pct}%</b>
            </span>
          )}
          <span className="font-mono text-xs text-zinc-400">
            上次巡检：{lastPollAt || '交易时段 09:35~14:45 自动轮询'}
          </span>
          {acct?.snapshot_at_bj && (
            <span className="font-mono text-xs text-zinc-400">
              · 快照：{acct.snapshot_at_bj.slice(5, 16)}
            </span>
          )}
          {acct?.snapshot_stale_prev_day && (
            <Badge color="amber">昨日快照 · T+1已自动推算解冻</Badge>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <div className="inline-flex rounded-md border border-zinc-700 bg-zinc-900 p-0.5 text-xs">
            <button
              type="button"
              onClick={() => setSmoothStep(true)}
              className={`rounded px-2.5 py-1 font-medium transition ${
                smoothStep ? 'bg-amber-500/25 text-amber-200' : 'text-zinc-400 hover:text-zinc-200'
              }`}
            >
              分批卖(≤30%)
            </button>
            <button
              type="button"
              onClick={() => setSmoothStep(false)}
              className={`rounded px-2.5 py-1 font-medium transition ${
                !smoothStep ? 'bg-amber-500/25 text-amber-200' : 'text-zinc-400 hover:text-zinc-200'
              }`}
            >
              一步清仓
            </button>
          </div>

          {acct?.available_accounts && acct.available_accounts.length > 1 && (
            <select
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
              className="rounded-md border border-zinc-700 bg-zinc-900 px-2.5 py-1 text-xs text-zinc-200"
            >
              {acct.available_accounts.map((a) => (
                <option key={a.account_id} value={a.account_id}>
                  {a.label} ({a.account_id})
                </option>
              ))}
            </select>
          )}

          <Button outline onClick={() => void handleSyncBroker()} disabled={syncingBroker || loading}>
            {syncingBroker ? '同步中…' : '同步实盘'}
          </Button>
          <Button outline onClick={() => void handlePushWechat()} disabled={pushingWx || loading}>
            {pushingWx ? '推送中…' : '推微信'}
          </Button>
          <Button outline onClick={() => void Promise.all([loadPlan(accountId, smoothStep), loadAdvisorStatus()])} disabled={loading}>
            {loading ? '刷新中…' : '刷新'}
          </Button>
          <Link to="/g3/ai-live" className="text-xs font-medium text-sky-300 hover:text-sky-200">
            📡 AI 盘面直播 →
          </Link>
        </div>
      </div>

      {err && (
        <div className="rounded-md border border-red-500/40 bg-red-950/30 px-4 py-2.5 text-sm text-red-200">
          {err}
        </div>
      )}
      {msg && (
        <div className="rounded-md border border-emerald-500/40 bg-emerald-950/30 px-4 py-2.5 text-sm text-emerald-200">
          {msg}
        </div>
      )}

      {plan?.panic_campaign?.active && plan.panic_campaign.summary && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-sky-500/40 bg-sky-950/25 px-4 py-2.5 text-xs text-sky-100">
          <div className="flex flex-wrap items-center gap-2">
            <Badge color="blue">🧊 AR4.6 冰点战役进行中</Badge>
            <span>{plan.panic_campaign.summary}</span>
          </div>
          <span className="text-sky-300/90">B 档退出：-6% 硬止损 / +5% 后回撤 -3.5% / 宽度 ≥38% / T+5 尾盘</span>
        </div>
      )}

      {/* ==================== 表 1：当前实盘持仓列表（全量展示，绝不隐藏） ==================== */}
      <div className="rounded-lg border border-zinc-800 bg-zinc-950/75 p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2.5">
            <span className="text-base font-bold text-white">1. 当前实盘持仓列表（共 {decisionHoldings.length} 只）</span>
            <span className="text-xs text-zinc-400">
              策略预期内且已对齐的持仓不显示操作按钮；预期外或需调仓标的按 AI 10 分钟动态门禁给出买入 / 卖出按钮
            </span>
          </div>
          {plan?.cash_flow && plan.cash_flow.est_sell_proceeds > 0 && (
            <span className="font-mono text-xs text-emerald-300">
              预计卖回款 +{fmtYuan(plan.cash_flow.est_sell_proceeds)} → 卖后可用 {fmtYuan(plan.cash_flow.est_post_sell_cash)}
            </span>
          )}
        </div>

        {decisionHoldings.length === 0 ? (
          <div className="rounded-md border border-zinc-800 bg-zinc-900/50 px-4 py-4 text-center text-sm text-zinc-400">
            当前账户空仓（100% 现金），请查看下方「待买入列表」。
          </div>
        ) : (
          <div className="overflow-x-auto">
            <Table dense>
              <TableHead>
                <TableRow>
                  <TableHeader>标的</TableHeader>
                  <TableHeader>实盘持仓 / 盈亏</TableHeader>
                  <TableHeader>策略目标 / 差额</TableHeader>
                  <TableHeader>AI 10分钟动态判定 & 价格红线</TableHeader>
                  <TableHeader className="text-right">交易操作</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {decisionHoldings.map((row) => {
                  const rowKey = `H-${row.code}-${row.action_side || 'HOLD'}`
                  const draft = getRowDraft(rowKey, row.action_shares, row.action_price)
                  const isAligned = row.alignment === 'aligned'
                  const isUnexpected = row.alignment === 'unexpected'
                  return (
                    <TableRow
                      key={row.code}
                      className={
                        row.live_10m_triggered
                          ? 'bg-amber-950/20'
                          : isUnexpected
                            ? 'bg-red-950/10'
                            : undefined
                      }
                    >
                      <TableCell className="whitespace-nowrap">
                        <div className="flex items-center gap-2">
                          <span className="text-base font-bold text-white">{row.name}</span>
                          <span className="font-mono text-xs text-zinc-400">{row.code}</span>
                          <Badge color={isUnexpected ? 'red' : isAligned ? 'emerald' : 'blue'}>
                            {isUnexpected ? '预期外' : isAligned ? '预期内' : row.alignment === 'overweight' ? '超配' : '欠配'}
                          </Badge>
                        </div>
                        <div className="mt-0.5 font-mono text-xs text-zinc-400">
                          现价 {fmtPx(row.current_price, row.is_etf)} · {row.shares}股（可用 {row.available_shares}股
                          {row.t1_inferred_unlocked ? '·跨日解冻' : ''}）
                        </div>
                      </TableCell>

                      <TableCell className="whitespace-nowrap font-mono">
                        <div className="text-sm font-semibold text-white">
                          {row.actual_weight_pct.toFixed(1)}% · {fmtYuan(row.market_value)}
                        </div>
                        <div
                          className={`mt-0.5 text-xs ${
                            row.profit_pct == null
                              ? 'text-zinc-400'
                              : row.profit_pct >= 0
                                ? 'text-rose-300'
                                : 'text-emerald-300'
                          }`}
                        >
                          浮盈 {row.profit_pct != null ? `${row.profit_pct >= 0 ? '+' : ''}${row.profit_pct.toFixed(2)}%` : '—'}
                        </div>
                      </TableCell>

                      <TableCell className="whitespace-nowrap font-mono">
                        <div className="text-sm text-zinc-200">
                          目标 <b>{row.target_weight_pct.toFixed(1)}%</b>
                        </div>
                        <div
                          className={`mt-0.5 text-xs font-semibold ${
                            isAligned
                              ? 'text-emerald-300'
                              : row.weight_diff_pct > 0
                                ? 'text-red-300'
                                : 'text-sky-300'
                          }`}
                        >
                          {isAligned
                            ? '已对齐'
                            : row.weight_diff_pct > 0
                              ? `超目标 +${row.weight_diff_pct.toFixed(1)}%`
                              : `欠配 ${row.weight_diff_pct.toFixed(1)}%`}
                        </div>
                      </TableCell>

                      <TableCell className="max-w-md whitespace-normal">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <Badge color={toneToBadge(row.ai_status_tone)}>{row.ai_status_label}</Badge>
                          {row.live_10m_triggered && <Badge color="amber">10m 实时信号</Badge>}
                        </div>
                        <div className="mt-1 text-xs leading-relaxed text-zinc-300">{row.ai_status_detail}</div>
                        {row.action_side === 'BUY' && row.min_buy_price != null && (
                          <div className="mt-1 font-mono text-xs text-amber-300">
                            放弃线：跌破 {fmtPx(row.min_buy_price, row.is_etf)} 不再加仓
                            {row.max_buy_price != null && (
                              <span className="ml-2 text-zinc-400">
                                · 不追高 {fmtPx(row.max_buy_price, row.is_etf)}
                              </span>
                            )}
                          </div>
                        )}
                      </TableCell>

                      <TableCell className="whitespace-nowrap text-right">
                        {isAligned || !row.action_side ? (
                          <span className="text-xs text-zinc-500">—</span>
                        ) : !row.can_execute ? (
                          <span className="inline-block rounded border border-zinc-800 bg-zinc-900/80 px-2.5 py-1 text-xs text-zinc-400">
                            {row.ai_status_label}
                          </span>
                        ) : (
                          <div className="flex flex-wrap items-center justify-end gap-2">
                            <input
                              type="number"
                              step={100}
                              min={100}
                              value={draft.shares}
                              onChange={(e) =>
                                setRowDraft(
                                  rowKey,
                                  { shares: Math.max(0, Number(e.target.value) || 0) },
                                  row.action_shares,
                                  row.action_price,
                                )
                              }
                              title="委托股数"
                              className="w-20 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-xs text-white"
                            />
                            <input
                              type="number"
                              step={row.is_etf ? 0.001 : 0.01}
                              min={0.01}
                              value={draft.price}
                              onChange={(e) =>
                                setRowDraft(
                                  rowKey,
                                  { price: Math.max(0, Number(e.target.value) || 0) },
                                  row.action_shares,
                                  row.action_price,
                                )
                              }
                              title="委托限价"
                              className="w-20 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-xs text-white"
                            />
                            <Button
                              color={row.action_side === 'SELL' ? 'amber' : 'cyan'}
                              disabled={singleBusyKey === rowKey}
                              onClick={() =>
                                void handleSinglePrefill({
                                  key: rowKey,
                                  code: row.code,
                                  name: row.name,
                                  side: row.action_side as 'SELL' | 'BUY',
                                  price: draft.price,
                                  shares: draft.shares,
                                })
                              }
                            >
                              {singleBusyKey === rowKey
                                ? '填单中…'
                                : row.action_side === 'SELL'
                                  ? '⚡ 一键填单(卖)'
                                  : '⚡ 一键填单(买)'}
                            </Button>
                            {accountId === 'acct1' && (
                              <Button
                                outline
                                disabled={confirmBusyKey === rowKey}
                                onClick={() =>
                                  void handleConfirmFill({
                                    key: rowKey,
                                    code: row.code,
                                    name: row.name,
                                    side: row.action_side as 'SELL' | 'BUY',
                                    price: draft.price,
                                    shares: draft.shares,
                                  })
                                }
                              >
                                {confirmBusyKey === rowKey ? '核销中…' : '已成交'}
                              </Button>
                            )}
                          </div>
                        )}
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          </div>
        )}
      </div>

      {/* ==================== 表 2：待买入股票 / ETF 列表（尚未持仓的策略新目标） ==================== */}
      <div className="rounded-lg border border-emerald-500/35 bg-emerald-950/10 p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2.5">
            <span className="text-base font-bold text-white">2. 待买入标的列表（共 {pendingBuys.length} 只）</span>
            <span className="text-xs text-zinc-400">
              当前未持仓的策略新目标；若实时价跌破放弃线将自动标红禁止买入，避免错过买点后接飞刀
            </span>
          </div>
        </div>

        {pendingBuys.length === 0 ? (
          <div className="rounded-md border border-zinc-800/80 bg-zinc-950/60 px-4 py-3 text-sm text-zinc-300">
            ✅ 当前策略目标均已在上方实盘持仓中，暂无新增待买入标的。
          </div>
        ) : (
          <div className="overflow-x-auto">
            <Table dense>
              <TableHead>
                <TableRow>
                  <TableHeader>标的 & 来源</TableHeader>
                  <TableHeader>目标仓位 / 现价</TableHeader>
                  <TableHeader>买入红线（跌破不买 / 不追高）</TableHeader>
                  <TableHeader>AI 10分钟动态判定</TableHeader>
                  <TableHeader className="text-right">交易操作</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {pendingBuys.map((row) => {
                  const rowKey = `B-${row.code}`
                  const baseSh = row.immediate_shares >= 100 ? row.immediate_shares : row.suggested_shares
                  const draft = getRowDraft(rowKey, baseSh, row.limit_price)
                  const isBrokenMin = row.buy_price_status === 'below_min'
                  return (
                    <TableRow
                      key={row.code}
                      className={
                        isBrokenMin
                          ? 'bg-red-950/20'
                          : row.live_10m_triggered
                            ? 'bg-emerald-950/25'
                            : undefined
                      }
                    >
                      <TableCell className="whitespace-nowrap">
                        <div className="flex items-center gap-2">
                          <span className="text-base font-bold text-white">{row.name}</span>
                          <span className="font-mono text-xs text-zinc-400">{row.code}</span>
                          <Badge color="blue">{row.source_label}</Badge>
                        </div>
                        <div className="mt-0.5 text-xs text-zinc-400">
                          {row.qdii_notice ? `⚠️ ${row.qdii_notice}` : row.reason}
                        </div>
                      </TableCell>

                      <TableCell className="whitespace-nowrap font-mono">
                        <div className="text-sm font-semibold text-white">
                          目标 {row.target_weight_pct.toFixed(1)}% · 现价 {fmtPx(row.ref_price, row.is_etf)}
                        </div>
                        <div className="mt-0.5 text-xs text-zinc-400">
                          现可买 {row.immediate_shares}股 / 卖后可买 {row.post_sell_shares}股
                        </div>
                      </TableCell>

                      <TableCell className="whitespace-nowrap font-mono text-xs">
                        {row.min_buy_price != null && (
                          <div className={isBrokenMin ? 'font-bold text-red-300' : 'text-amber-300'}>
                            跌破 {fmtPx(row.min_buy_price, row.is_etf)} 放弃（{row.min_buy_reason}）
                          </div>
                        )}
                        <div className="mt-0.5 text-zinc-400">
                          {row.max_buy_price != null && <>超 {fmtPx(row.max_buy_price, row.is_etf)} 不追高</>}
                          {row.stop_loss_price != null && (
                            <span className="ml-2">· 止损 {fmtPx(row.stop_loss_price, row.is_etf)}</span>
                          )}
                        </div>
                      </TableCell>

                      <TableCell className="max-w-sm whitespace-normal">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <Badge color={toneToBadge(row.ai_status_tone)}>{row.ai_status_label}</Badge>
                          {row.live_10m_triggered && <Badge color="emerald">10m 实时触发</Badge>}
                        </div>
                        <div className="mt-1 text-xs text-zinc-300">{row.ai_status_detail}</div>
                      </TableCell>

                      <TableCell className="whitespace-nowrap text-right">
                        {!row.can_execute || isBrokenMin ? (
                          <span
                            className={`inline-block rounded border px-2.5 py-1 text-xs ${
                              isBrokenMin
                                ? 'border-red-500/40 bg-red-950/40 font-semibold text-red-200'
                                : 'border-zinc-800 bg-zinc-900/80 text-zinc-400'
                            }`}
                          >
                            {isBrokenMin ? '🚫 已破放弃线，暂不买入' : row.ai_status_label}
                          </span>
                        ) : (
                          <div className="flex flex-wrap items-center justify-end gap-2">
                            <input
                              type="number"
                              step={100}
                              min={100}
                              value={draft.shares}
                              onChange={(e) =>
                                setRowDraft(
                                  rowKey,
                                  { shares: Math.max(0, Number(e.target.value) || 0) },
                                  baseSh,
                                  row.limit_price,
                                )
                              }
                              title="买入股数"
                              className="w-20 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-xs text-white"
                            />
                            <input
                              type="number"
                              step={row.is_etf ? 0.001 : 0.01}
                              min={0.01}
                              value={draft.price}
                              onChange={(e) =>
                                setRowDraft(
                                  rowKey,
                                  { price: Math.max(0, Number(e.target.value) || 0) },
                                  baseSh,
                                  row.limit_price,
                                )
                              }
                              title="买入限价"
                              className="w-20 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-xs text-white"
                            />
                            <Button
                              color="cyan"
                              disabled={singleBusyKey === rowKey}
                              onClick={() =>
                                void handleSinglePrefill({
                                  key: rowKey,
                                  code: row.code,
                                  name: row.name,
                                  side: 'BUY',
                                  price: draft.price,
                                  shares: draft.shares,
                                })
                              }
                            >
                              {singleBusyKey === rowKey ? '填单中…' : '⚡ 一键填单(买)'}
                            </Button>
                            {accountId === 'acct1' && (
                              <Button
                                outline
                                disabled={confirmBusyKey === rowKey}
                                onClick={() =>
                                  void handleConfirmFill({
                                    key: rowKey,
                                    code: row.code,
                                    name: row.name,
                                    side: 'BUY',
                                    price: draft.price,
                                    shares: draft.shares,
                                  })
                                }
                              >
                                {confirmBusyKey === rowKey ? '核销中…' : '已成交'}
                              </Button>
                            )}
                          </div>
                        )}
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          </div>
        )}
      </div>

      {/* 同花顺预填实时回执（含宿主机真实填单结果与读回校验反馈） */}
      {batchRes && (
        <div className="rounded-md border border-zinc-700 bg-zinc-950/90 p-3.5 text-sm">
          <div className="mb-1.5 font-semibold text-white">
            同花顺预填实时回执（严格 prefill_only · 绝不自动下单）：
          </div>
          <div className="space-y-1.5">
            {batchRes.results.map((r, idx) => {
              const live = r.draft_id ? prefillLiveMap[String(r.draft_id)] : undefined
              const doneRes = live?.state === 'done' ? live.result : undefined
              return (
                <div key={`${r.code}-${idx}`} className="flex flex-wrap items-center gap-2 font-mono text-xs">
                  <Badge color={r.side === 'SELL' ? 'amber' : 'emerald'}>
                    {r.side === 'SELL' ? '卖出' : '买入'}
                  </Badge>
                  <span className="font-semibold text-white">
                    {r.name}({r.code}) {r.shares}股 @ ¥{r.price}
                  </span>
                  {r.dispatched ? (
                    <Badge color={doneRes ? (doneRes.ok ? 'emerald' : 'red') : 'blue'}>
                      {!doneRes
                        ? '正在填入同花顺… ⏳'
                        : doneRes.ok
                          ? '已填入同花顺 ✅ 等待你点击确认'
                          : '填单失败 ❌'}
                    </Badge>
                  ) : (
                    <Badge color="red">预检拦截</Badge>
                  )}
                  {doneRes && !doneRes.ok && (
                    <span className="text-red-300">{doneRes.message || doneRes.reason || '填单失败'}</span>
                  )}
                  {!r.dispatched && r.blockers.length > 0 && (
                    <span className="text-red-300">{r.blockers.join('；')}</span>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
