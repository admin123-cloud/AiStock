import { useCallback, useEffect, useState } from 'react'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/catalyst/table'
import {
  batchPrefillG3AR3Orders,
  getG3AR3RebalancePlan,
  getG3AR3ShadowReconcile,
  pushG3AR3PlanWechat,
  type G3AR3BatchPrefillResponse,
  type G3AR3RebalancePlanResponse,
  type G3AR3ShadowReconcileResponse,
} from '@/api/g3'

interface EditableOrder {
  key: string
  checked: boolean
  code: string
  name: string
  side: 'SELL' | 'BUY'
  price: number
  shares: number
  is_etf: boolean
  stop_loss_price?: number
  reason: string
  badge_text: string
  qdii_notice?: string | null
}

interface AR3ExecutionOrderCardProps {
  /** 当外部热切换了主引擎配置时传入版本号，触发调仓单同步重算 */
  reloadToken?: number
}

const fmtYuan = (v?: number | null) =>
  v != null && Number.isFinite(Number(v))
    ? `¥${Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}`
    : '—'

export default function AR3ExecutionOrderCard({ reloadToken = 0 }: AR3ExecutionOrderCardProps) {
  const [accountId, setAccountId] = useState('acct1')
  const [smoothStep, setSmoothStep] = useState(true)
  const [buyBasis, setBuyBasis] = useState<'post_sell' | 'immediate'>('post_sell')
  const [plan, setPlan] = useState<G3AR3RebalancePlanResponse | null>(null)
  const [orders, setOrders] = useState<EditableOrder[]>([])
  const [shadow, setShadow] = useState<G3AR3ShadowReconcileResponse | null>(null)
  const [showShadow, setShowShadow] = useState(false)
  const [loading, setLoading] = useState(false)
  const [dispatching, setDispatching] = useState(false)
  const [pushingWx, setPushingWx] = useState(false)
  const [batchRes, setBatchRes] = useState<G3AR3BatchPrefillResponse | null>(null)
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')

  const buildEditableList = useCallback(
    (p: G3AR3RebalancePlanResponse, basis: 'post_sell' | 'immediate'): EditableOrder[] => {
      const list: EditableOrder[] = []
      for (const s of p.sell_orders || []) {
        if (s.suggested_shares > 0) {
          list.push({
            key: `SELL-${s.code}`,
            checked: true,
            code: s.code,
            name: s.name,
            side: 'SELL',
            price: s.limit_price,
            shares: s.suggested_shares,
            is_etf: s.is_etf,
            reason: s.reason,
            badge_text: `可用${s.available_shares}股 / 现${s.current_shares}股`,
          })
        }
      }
      for (const b of p.buy_orders || []) {
        const sh = basis === 'immediate' ? b.immediate_shares : b.post_sell_shares
        if (sh > 0) {
          list.push({
            key: `BUY-${b.code}`,
            checked: true,
            code: b.code,
            name: b.name,
            side: 'BUY',
            price: b.limit_price,
            shares: sh,
            is_etf: b.is_etf,
            stop_loss_price: b.stop_loss_price,
            reason: b.reason,
            badge_text:
              basis === 'immediate'
                ? `现有现金立买 · 目标${b.target_weight_pct}%`
                : `卖后应买(现可先买${b.immediate_shares}股) · 目标${b.target_weight_pct}%`,
            qdii_notice: b.qdii_notice,
          })
        }
      }
      return list
    },
    []
  )

  const loadPlan = useCallback(
    async (aid: string, smooth: boolean) => {
      setLoading(true)
      setErr('')
      setBatchRes(null)
      try {
        const [p, rec] = await Promise.all([
          getG3AR3RebalancePlan(aid, smooth),
          getG3AR3ShadowReconcile(aid, 20).catch(() => null),
        ])
        setPlan(p)
        setOrders(buildEditableList(p, buyBasis))
        if (rec) setShadow(rec)
      } catch (e) {
        const ex = e as { response?: { data?: { detail?: string } }; message?: string }
        setErr(ex.response?.data?.detail || ex.message || '加载 AR3 每日买卖单失败')
      } finally {
        setLoading(false)
      }
    },
    [buildEditableList, buyBasis]
  )

  useEffect(() => {
    void loadPlan(accountId, smoothStep)
  }, [accountId, smoothStep, reloadToken, loadPlan])

  useEffect(() => {
    if (plan) {
      setOrders(buildEditableList(plan, buyBasis))
    }
  }, [buyBasis, plan, buildEditableList])

  const handleBatchPrefill = async () => {
    const selected = orders.filter((o) => o.checked && o.shares >= 100 && o.price > 0)
    if (selected.length === 0) {
      setErr('请至少勾选 1 笔有效委托（股数 ≥ 100 且价格 > 0）')
      return
    }
    setDispatching(true)
    setErr('')
    setMsg('')
    setBatchRes(null)
    try {
      const res = await batchPrefillG3AR3Orders({
        account_id: accountId,
        auto_split_l2: true,
        orders: selected.map((o) => ({
          code: o.code,
          name: o.name,
          side: o.side,
          price: o.price,
          shares: o.shares,
          order_type: 'base_rebalance',
        })),
      })
      setBatchRes(res)
      if (res.dispatched_count > 0) {
        setMsg(
          `已按「先卖后买」顺序向同花顺远航预填桥派发 ${res.dispatched_count}/${res.total_orders} 笔合规委托（严格 prefill_only，请在同花顺界面核对后确认下单）。`
        )
      } else {
        setErr(`全部 ${res.total_orders} 笔委托未通过 L2 风控预检，请查看下方逐笔明细。`)
      }
    } catch (e) {
      const ex = e as { response?: { data?: { detail?: string } }; message?: string }
      setErr(ex.response?.data?.detail || ex.message || '批量预填派发失败')
    } finally {
      setDispatching(false)
    }
  }

  const handlePushWechat = async () => {
    setPushingWx(true)
    setErr('')
    setMsg('')
    try {
      const res = await pushG3AR3PlanWechat(accountId, smoothStep)
      if (res.status === 'ok') {
        setMsg(`已将 ${res.plan_date || '今日'} AR3 v3.0 最终买卖调仓单推送到你的微信。`)
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

  const selectedCount = orders.filter((o) => o.checked && o.shares >= 100).length
  const acct = plan?.account
  const cf = plan?.cash_flow

  return (
    <div className="rounded-lg border border-emerald-500/45 bg-emerald-950/15 p-3 text-xs">
      {/* 顶栏：主力单账号 + 资金诊断 + 一键操作 */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/90 pb-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-bold text-white">📋 AR3 v3.0 今日最终买卖执行单</span>
          <Badge color="emerald">主力单账号：{acct?.label || 'ct1曾 (acct1)'}</Badge>
          {acct && acct.available && (
            <span className="font-mono text-[11px] text-zinc-200">
              总资产 <b>{fmtYuan(acct.total_assets)}</b> · 可用现金{' '}
              <b className="text-emerald-300">{fmtYuan(acct.available_cash)}</b> · 现仓位{' '}
              <b>{acct.position_pct}%</b>
            </span>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {/* 减仓步长切换 */}
          <div className="inline-flex rounded-md border border-zinc-700 bg-zinc-950 p-0.5 text-[11px]">
            <button
              type="button"
              onClick={() => setSmoothStep(true)}
              className={`rounded px-2 py-0.5 ${smoothStep ? 'bg-amber-500/25 text-amber-200' : 'text-zinc-400'}`}
            >
              平稳分步(≤30%/日)
            </button>
            <button
              type="button"
              onClick={() => setSmoothStep(false)}
              className={`rounded px-2 py-0.5 ${!smoothStep ? 'bg-rose-500/25 text-rose-200' : 'text-zinc-400'}`}
            >
              一步到位清退
            </button>
          </div>

          {/* 买单资金口径切换 */}
          <div className="inline-flex rounded-md border border-zinc-700 bg-zinc-950 p-0.5 text-[11px]">
            <button
              type="button"
              onClick={() => setBuyBasis('post_sell')}
              className={`rounded px-2 py-0.5 ${buyBasis === 'post_sell' ? 'bg-blue-500/25 text-blue-200' : 'text-zinc-400'}`}
            >
              买单按卖后现金
            </button>
            <button
              type="button"
              onClick={() => setBuyBasis('immediate')}
              className={`rounded px-2 py-0.5 ${buyBasis === 'immediate' ? 'bg-emerald-500/25 text-emerald-200' : 'text-zinc-400'}`}
            >
              买单仅按现有现金
            </button>
          </div>

          {acct && acct.available_accounts && acct.available_accounts.length > 1 && (
            <select
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
              className="rounded-md border border-zinc-700 bg-zinc-950 px-2 py-1 text-[11px] text-zinc-200"
            >
              {acct.available_accounts.map((a) => (
                <option key={a.account_id} value={a.account_id}>
                  {a.label} ({a.account_id})
                </option>
              ))}
            </select>
          )}

          <Button outline disabled={pushingWx} onClick={() => void handlePushWechat()}>
            {pushingWx ? '推送中…' : '推送买卖单到微信'}
          </Button>
          <Button disabled={dispatching || selectedCount === 0} onClick={() => void handleBatchPrefill()}>
            {dispatching ? 'L2预检派发中…' : `一键顺序预填至同花顺 (${selectedCount}笔)`}
          </Button>
        </div>
      </div>

      {/* 现金守恒预览条 */}
      {cf && (
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2 font-mono text-[11px] text-zinc-300">
          <div className="flex flex-wrap items-center gap-3">
            <span>① 现有可用：{fmtYuan(cf.current_available_cash)}</span>
            <span>→ ② 预计卖出回款：<b className="text-amber-300">+{fmtYuan(cf.est_sell_proceeds)}</b></span>
            <span>→ ③ 卖后可买上限：<b className="text-emerald-300">{fmtYuan(cf.post_sell_buy_budget)}</b></span>
            <span className="text-zinc-400">（单笔超 ¥25,000 派发时自动拆 L2 合规子单 · 严格 prefill_only 不自动提交）</span>
          </div>
          <button
            type="button"
            onClick={() => setShowShadow((v) => !v)}
            className="text-cyan-300 hover:text-cyan-200"
          >
            {showShadow ? '收起 Shadow 实盘对账 ▲' : `Shadow 实盘对账 (${shadow?.days_tracked ?? 0}日) ▼`}
          </button>
        </div>
      )}

      {err && (
        <div className="mt-2 rounded border border-red-500/40 bg-red-950/30 px-2.5 py-1.5 text-red-200">{err}</div>
      )}
      {msg && (
        <div className="mt-2 rounded border border-emerald-500/40 bg-emerald-950/30 px-2.5 py-1.5 text-emerald-200">
          {msg}
        </div>
      )}

      {/* 最终买卖明细表（支持人工勾选与微调股数/价格） */}
      {loading && !plan ? (
        <div className="py-5 text-center text-zinc-400">正在基于主力账号真实持仓计算 AR3 v3.0 精确买卖股数…</div>
      ) : orders.length === 0 ? (
        <div className="py-4 text-center text-zinc-300">
          ✅ 当前账户持仓已与 AR3 v3.0 目标组合对齐（差额均小于 ¥1,800），今日无需调仓。
        </div>
      ) : (
        <div className="mt-2.5 overflow-x-auto rounded-md border border-zinc-800 bg-zinc-950/80">
          <Table dense>
            <TableHead>
              <TableRow>
                <TableHeader>选</TableHeader>
                <TableHeader>顺序 / 方向</TableHeader>
                <TableHeader>标的</TableHeader>
                <TableHeader>建议股数 (可微调)</TableHeader>
                <TableHeader>参考限价</TableHeader>
                <TableHeader>预计金额</TableHeader>
                <TableHeader>依据与风控止损</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {orders.map((o, idx) => (
                <TableRow key={o.key}>
                  <TableCell>
                    <input
                      type="checkbox"
                      checked={o.checked}
                      onChange={(e) =>
                        setOrders((prev) =>
                          prev.map((x) => (x.key === o.key ? { ...x, checked: e.target.checked } : x))
                        )
                      }
                      className="h-3.5 w-3.5 rounded border-zinc-600 bg-zinc-900"
                    />
                  </TableCell>
                  <TableCell className="whitespace-nowrap">
                    <div className="flex items-center gap-1.5">
                      <span className="font-mono text-zinc-400">#{idx + 1}</span>
                      <Badge color={o.side === 'SELL' ? 'amber' : 'emerald'}>
                        {o.side === 'SELL' ? '第一步·卖出' : '第二步·买入'}
                      </Badge>
                    </div>
                  </TableCell>
                  <TableCell className="whitespace-nowrap">
                    <div className="font-semibold text-white">
                      {o.name} <span className="font-mono text-zinc-400">{o.code}</span>
                    </div>
                    <div className="text-[11px] text-zinc-400">{o.badge_text}</div>
                  </TableCell>
                  <TableCell className="whitespace-nowrap">
                    <input
                      type="number"
                      step={100}
                      min={0}
                      value={o.shares}
                      onChange={(e) => {
                        const val = Math.max(0, Math.floor(Number(e.target.value) || 0))
                        setOrders((prev) => prev.map((x) => (x.key === o.key ? { ...x, shares: val } : x)))
                      }}
                      className="w-24 rounded border border-zinc-700 bg-zinc-900 px-2 py-0.5 font-mono text-xs text-white"
                    />
                  </TableCell>
                  <TableCell className="whitespace-nowrap">
                    <input
                      type="number"
                      step={o.is_etf ? 0.001 : 0.01}
                      min={0}
                      value={o.price}
                      onChange={(e) => {
                        const val = Math.max(0, Number(e.target.value) || 0)
                        setOrders((prev) => prev.map((x) => (x.key === o.key ? { ...x, price: val } : x)))
                      }}
                      className="w-20 rounded border border-zinc-700 bg-zinc-900 px-2 py-0.5 font-mono text-xs text-white"
                    />
                  </TableCell>
                  <TableCell className="whitespace-nowrap font-mono font-semibold text-zinc-100">
                    {fmtYuan(o.shares * o.price)}
                  </TableCell>
                  <TableCell className="max-w-[420px] text-[11px] text-zinc-300">
                    <div>{o.reason}</div>
                    {o.stop_loss_price != null && (
                      <div className="font-mono text-amber-300">参考止损位：¥{o.stop_loss_price}</div>
                    )}
                    {o.qdii_notice && <div className="text-amber-300">⚠️ {o.qdii_notice}</div>}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {/* 批量预填派发逐笔回执 */}
      {batchRes && (
        <div className="mt-2.5 rounded-md border border-zinc-800 bg-zinc-950/90 p-2.5 text-[11px]">
          <div className="font-semibold text-white">
            L2 预检与同花顺远航预填桥派发回执（已派发 {batchRes.dispatched_count}/{batchRes.total_orders} 笔 · 严格 prefill_only）：
          </div>
          <div className="mt-1.5 space-y-1">
            {batchRes.results.map((r, i) => (
              <div key={`${r.code}-${i}`} className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-900 pb-1">
                <span className="font-mono text-zinc-200">
                  {r.side} {r.name}({r.code}) {r.shares}股 @ ¥{r.price}
                </span>
                <div className="flex items-center gap-2">
                  <Badge color={r.dispatched ? 'emerald' : 'red'}>
                    {r.dispatched ? '已入预填队列 ✅' : 'L2拦截 ⛔'}
                  </Badge>
                  {r.blockers.length > 0 && <span className="text-red-300">{r.blockers.join('；')}</span>}
                  {r.warnings.length > 0 && <span className="text-amber-300">{r.warnings.join('；')}</span>}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Shadow Ledger 近 20 日实盘执行对账 */}
      {showShadow && shadow && (
        <div className="mt-2.5 rounded-md border border-cyan-500/30 bg-zinc-950/85 p-2.5 text-[11px]">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-semibold text-cyan-200">
              📊 Shadow Ledger 实盘对账（{shadow.account_label} · 已跟踪 {shadow.days_tracked} 个交易日）
            </span>
            <div className="flex flex-wrap items-center gap-3 font-mono text-zinc-300">
              <span>计划委托：{shadow.total_planned_orders} 笔</span>
              <span>已执行：{shadow.total_executed_orders} 笔</span>
              <span>完成率：<b>{shadow.overall_completion_pct != null ? `${shadow.overall_completion_pct}%` : '—'}</b></span>
              <span>平均滑点：<b>{shadow.avg_slippage_bps != null ? `${shadow.avg_slippage_bps} bps` : '待成交累积'}</b></span>
            </div>
          </div>
          {shadow.daily_records.length > 0 && (
            <div className="mt-2 max-h-40 overflow-y-auto space-y-1">
              {shadow.daily_records.slice(0, 7).map((d) => (
                <div key={d.date} className="flex flex-wrap items-center justify-between border-t border-zinc-900 pt-1 font-mono text-zinc-400">
                  <span>{d.date} ({d.regime})</span>
                  <span>计划 {d.planned_orders} 笔 · 成交 {d.executed_orders} 笔 · 完成率 {d.completion_pct}%</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
