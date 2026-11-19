import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  getAccountsOverview,
  triggerAccountsSync,
  type AccountsOverviewResponse,
  type BrokerAccountDetail,
} from '@/api/accounts'

function fmtMoney(val?: number | null): string {
  if (val === null || val === undefined || Number.isNaN(val)) return '--'
  return val.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function fmtSigned(val?: number | null, suffix = ''): string {
  if (val === null || val === undefined || Number.isNaN(val)) return '--'
  const sign = val > 0 ? '+' : ''
  return `${sign}${val.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${suffix}`
}

function pnlColor(val?: number | null): string {
  if (val === null || val === undefined || val === 0) return 'text-zinc-700 dark:text-zinc-200'
  return val > 0 ? 'text-red-600 dark:text-red-400' : 'text-emerald-600 dark:text-emerald-400'
}

export default function BrokerAccounts() {
  const [data, setData] = useState<AccountsOverviewResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [activeTab, setActiveTab] = useState<string>('all')
  const [syncing, setSyncing] = useState(false)
  const [syncNotice, setSyncNotice] = useState<string | null>(null)

  const loadData = useCallback(async () => {
    try {
      setError(null)
      const res = await getAccountsOverview()
      setData(res)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载资金账号概览失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadData()
    const timer = setInterval(loadData, 15000)
    return () => clearInterval(timer)
  }, [loadData])

  const handleSync = async (targetAccounts?: string[]) => {
    try {
      setSyncing(true)
      setSyncNotice(null)
      const res = await triggerAccountsSync(targetAccounts, true)
      if (res.status === 'already_queued') {
        setSyncNotice(`已有只读同步指令排队中（${res.request_id}），请等待宿主机桌面桥空闲窗口执行。`)
      } else {
        setSyncNotice(`已下发只读同步指令（${res.request_id}），宿主机桌面桥将在满足 90 秒空闲守卫后采集。`)
      }
      await loadData()
    } catch (err) {
      setSyncNotice(err instanceof Error ? `同步指令下发失败：${err.message}` : '同步指令下发失败')
    } finally {
      setSyncing(false)
    }
  }

  const visibleAccounts: BrokerAccountDetail[] = useMemo(() => {
    if (!data) return []
    if (activeTab === 'all') return data.accounts
    return data.accounts.filter((a) => a.account_id === activeTab)
  }, [data, activeTab])

  const mergedHoldings = useMemo(() => {
    return visibleAccounts.flatMap((acct) =>
      (acct.holdings || []).map((h) => ({
        ...h,
        _account_id: acct.account_id,
        _account_label: acct.label,
      }))
    )
  }, [visibleAccounts])

  const mergedFills = useMemo(() => {
    return visibleAccounts.flatMap((acct) =>
      (acct.fills || []).map((f) => ({
        ...f,
        _account_id: acct.account_id,
        _account_label: acct.label,
      }))
    )
  }, [visibleAccounts])

  if (loading && !data) {
    return (
      <div className="p-6 text-sm text-zinc-500 dark:text-zinc-400">
        正在加载双账号资金与持仓概览…
      </div>
    )
  }

  return (
    <div className="space-y-6 p-6">
      {/* 顶栏标题 + 宿主机桌面桥状态 + 一键同步 */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-zinc-950 dark:text-white">
            资金账号管理
          </h1>
          <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
            同花顺远航双账号只读监控（凭证零接触 · 绝不下单 · 原生控件安全切号）
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          {data?.bridge && (
            <span
              className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium ${
                data.bridge.online
                  ? 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200 dark:bg-emerald-950/50 dark:text-emerald-300 dark:ring-emerald-800'
                  : 'bg-zinc-100 text-zinc-600 ring-1 ring-zinc-200 dark:bg-zinc-800 dark:text-zinc-300 dark:ring-zinc-700'
              }`}
            >
              <span
                className={`h-2 w-2 rounded-full ${
                  data.bridge.online ? 'bg-emerald-500' : 'bg-zinc-400'
                }`}
              />
              桌面桥：{data.bridge.online ? `在线 (${data.bridge.state})` : '离线（展示最近落盘快照）'}
              {data.bridge.pending_count > 0 && ` · 排队 ${data.bridge.pending_count}`}
            </span>
          )}

          <button
            type="button"
            disabled={syncing}
            onClick={() => handleSync(activeTab === 'all' ? undefined : [activeTab])}
            className="rounded-lg bg-indigo-600 px-3.5 py-2 text-xs font-semibold text-white shadow-sm hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {syncing ? '正在下发同步…' : activeTab === 'all' ? '一键同步双账号' : `同步 ${activeTab}`}
          </button>

          <button
            type="button"
            onClick={loadData}
            className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-xs font-medium text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-200 dark:hover:bg-zinc-800"
          >
            刷新视图
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </div>
      )}

      {syncNotice && (
        <div className="rounded-lg border border-indigo-200 bg-indigo-50 px-4 py-3 text-xs text-indigo-800 dark:border-indigo-900 dark:bg-indigo-950/40 dark:text-indigo-200">
          {syncNotice}
        </div>
      )}

      {/* 合并 KPI 汇总卡片 */}
      {data && (
        <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
          <div className="rounded-xl border border-zinc-200 bg-white p-4 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
            <div className="text-xs text-zinc-500 dark:text-zinc-400">合并总资产</div>
            <div className="mt-1 text-xl font-bold tabular-nums text-zinc-950 dark:text-white">
              {fmtMoney(data.merged.total_assets)}
            </div>
            <div className="mt-1 text-[11px] text-zinc-400">
              已就绪 {data.accounts_ready}/{data.accounts_total} 账号
            </div>
          </div>

          <div className="rounded-xl border border-zinc-200 bg-white p-4 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
            <div className="text-xs text-zinc-500 dark:text-zinc-400">股票总市值</div>
            <div className="mt-1 text-xl font-bold tabular-nums text-zinc-950 dark:text-white">
              {fmtMoney(data.merged.market_value)}
            </div>
            <div className="mt-1 text-[11px] text-zinc-400">
              持仓 {data.merged.holdings_count} 只
            </div>
          </div>

          <div className="rounded-xl border border-zinc-200 bg-white p-4 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
            <div className="text-xs text-zinc-500 dark:text-zinc-400">可用资金合计</div>
            <div className="mt-1 text-xl font-bold tabular-nums text-zinc-950 dark:text-white">
              {fmtMoney(data.merged.available)}
            </div>
            <div className="mt-1 text-[11px] text-zinc-400">
              冻结 {fmtMoney(data.merged.frozen)}
            </div>
          </div>

          <div className="rounded-xl border border-zinc-200 bg-white p-4 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
            <div className="text-xs text-zinc-500 dark:text-zinc-400">当日盈亏合计</div>
            <div className={`mt-1 text-xl font-bold tabular-nums ${pnlColor(data.merged.daily_profit)}`}>
              {fmtSigned(data.merged.daily_profit)}
            </div>
            <div className={`mt-1 text-[11px] tabular-nums ${pnlColor(data.merged.daily_profit_ratio)}`}>
              {fmtSigned(data.merged.daily_profit_ratio, '%')}
            </div>
          </div>

          <div className="rounded-xl border border-zinc-200 bg-white p-4 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
            <div className="text-xs text-zinc-500 dark:text-zinc-400">持仓盈亏合计</div>
            <div className={`mt-1 text-xl font-bold tabular-nums ${pnlColor(data.merged.holding_profit)}`}>
              {fmtSigned(data.merged.holding_profit)}
            </div>
            <div className="mt-1 text-[11px] text-zinc-400">按最新快照实时汇总</div>
          </div>
        </div>
      )}

      {/* 账号切换 Tab */}
      {data && (
        <div className="flex flex-wrap items-center gap-2 border-b border-zinc-200 pb-3 dark:border-zinc-800">
          <button
            type="button"
            onClick={() => setActiveTab('all')}
            className={`rounded-lg px-3 py-1.5 text-xs font-medium transition ${
              activeTab === 'all'
                ? 'bg-zinc-900 text-white dark:bg-white dark:text-zinc-900'
                : 'bg-zinc-100 text-zinc-700 hover:bg-zinc-200 dark:bg-zinc-800 dark:text-zinc-300'
            }`}
          >
            全部汇总（{data.accounts.length}）
          </button>
          {data.accounts.map((acct) => (
            <button
              key={acct.account_id}
              type="button"
              onClick={() => setActiveTab(acct.account_id)}
              className={`rounded-lg px-3 py-1.5 text-xs font-medium transition ${
                activeTab === acct.account_id
                  ? 'bg-zinc-900 text-white dark:bg-white dark:text-zinc-900'
                  : 'bg-zinc-100 text-zinc-700 hover:bg-zinc-200 dark:bg-zinc-800 dark:text-zinc-300'
              }`}
            >
              {acct.account_id} · {acct.label}（{acct.masked_id}）
            </button>
          ))}
        </div>
      )}

      {/* 分账号资金卡片 */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {visibleAccounts.map((acct) => (
          <div
            key={acct.account_id}
            className="rounded-xl border border-zinc-200 bg-white p-4 shadow-sm dark:border-zinc-800 dark:bg-zinc-900"
          >
            <div className="flex items-center justify-between gap-2">
              <div>
                <span className="text-sm font-semibold text-zinc-950 dark:text-white">
                  {acct.label}
                </span>
                <span className="ml-2 font-mono text-xs text-zinc-500 dark:text-zinc-400">
                  {acct.account_id} · {acct.masked_id}
                </span>
              </div>
              <span
                className={`rounded-full px-2.5 py-0.5 text-[11px] font-medium ${
                  acct.available
                    ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300'
                    : 'bg-amber-50 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300'
                }`}
              >
                {acct.status}
              </span>
            </div>

            {acct.funds ? (
              <div className="mt-3 grid grid-cols-3 gap-3 text-xs">
                <div>
                  <div className="text-zinc-500">总资产</div>
                  <div className="mt-0.5 font-semibold tabular-nums">{fmtMoney(acct.funds.total_assets)}</div>
                </div>
                <div>
                  <div className="text-zinc-500">股票市值</div>
                  <div className="mt-0.5 font-semibold tabular-nums">{fmtMoney(acct.funds.market_value)}</div>
                </div>
                <div>
                  <div className="text-zinc-500">可用资金</div>
                  <div className="mt-0.5 font-semibold tabular-nums">{fmtMoney(acct.funds.available)}</div>
                </div>
                <div>
                  <div className="text-zinc-500">当日盈亏</div>
                  <div className={`mt-0.5 font-semibold tabular-nums ${pnlColor(acct.funds.daily_profit)}`}>
                    {fmtSigned(acct.funds.daily_profit)} ({fmtSigned(acct.funds.daily_profit_ratio, '%')})
                  </div>
                </div>
                <div>
                  <div className="text-zinc-500">持仓盈亏</div>
                  <div className={`mt-0.5 font-semibold tabular-nums ${pnlColor(acct.funds.holding_profit)}`}>
                    {fmtSigned(acct.funds.holding_profit)}
                  </div>
                </div>
                <div>
                  <div className="text-zinc-500">采集时间</div>
                  <div className="mt-0.5 truncate font-mono text-[11px] text-zinc-500">
                    {acct.generated_at ? acct.generated_at.slice(0, 19).replace('T', ' ') : '--'}
                  </div>
                </div>
              </div>
            ) : (
              <div className="mt-3 text-xs text-zinc-500">暂无有效资金快照（{acct.reason || '未采集'}）</div>
            )}
          </div>
        ))}
      </div>

      {/* 持仓明细表 */}
      <div className="rounded-xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
        <div className="border-b border-zinc-200 px-4 py-3 dark:border-zinc-800">
          <h2 className="text-sm font-semibold text-zinc-950 dark:text-white">
            持仓明细（{mergedHoldings.length}）
          </h2>
        </div>
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-zinc-200 text-xs dark:divide-zinc-800">
            <thead className="bg-zinc-50 dark:bg-zinc-950/40">
              <tr>
                <th className="px-4 py-2.5 text-left font-medium text-zinc-500">账号</th>
                <th className="px-4 py-2.5 text-left font-medium text-zinc-500">代码 / 名称</th>
                <th className="px-4 py-2.5 text-right font-medium text-zinc-500">持仓 / 可用</th>
                <th className="px-4 py-2.5 text-right font-medium text-zinc-500">成本价</th>
                <th className="px-4 py-2.5 text-right font-medium text-zinc-500">现价</th>
                <th className="px-4 py-2.5 text-right font-medium text-zinc-500">当日盈亏</th>
                <th className="px-4 py-2.5 text-right font-medium text-zinc-500">持仓盈亏</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800/70">
              {mergedHoldings.length === 0 ? (
                <tr>
                  <td colSpan={7} className="px-4 py-6 text-center text-zinc-400">
                    暂无持仓记录
                  </td>
                </tr>
              ) : (
                mergedHoldings.map((h, idx) => (
                  <tr key={`${h._account_id}-${h.code}-${idx}`} className="hover:bg-zinc-50/70 dark:hover:bg-zinc-800/40">
                    <td className="px-4 py-2.5 font-mono text-zinc-600 dark:text-zinc-300">{h._account_id}</td>
                    <td className="px-4 py-2.5">
                      <span className="font-mono font-medium text-zinc-900 dark:text-white">{h.code}</span>
                      <span className="ml-2 text-zinc-600 dark:text-zinc-300">{h.name}</span>
                    </td>
                    <td className="px-4 py-2.5 text-right tabular-nums">
                      {h.quantity ?? '--'} / {h.available_quantity ?? '--'}
                    </td>
                    <td className="px-4 py-2.5 text-right tabular-nums">{h.cost_price ?? '--'}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums">{h.current_price ?? '--'}</td>
                    <td className={`px-4 py-2.5 text-right tabular-nums ${pnlColor(h.daily_profit)}`}>
                      {fmtSigned(h.daily_profit)} ({fmtSigned(h.daily_profit_ratio, '%')})
                    </td>
                    <td className={`px-4 py-2.5 text-right tabular-nums ${pnlColor(h.floating_profit)}`}>
                      {fmtSigned(h.floating_profit)} ({fmtSigned(h.profit_ratio, '%')})
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* 历史成交表 */}
      <div className="rounded-xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
        <div className="border-b border-zinc-200 px-4 py-3 dark:border-zinc-800">
          <h2 className="text-sm font-semibold text-zinc-950 dark:text-white">
            最近采集历史成交（{mergedFills.length}）
          </h2>
        </div>
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-zinc-200 text-xs dark:divide-zinc-800">
            <thead className="bg-zinc-50 dark:bg-zinc-950/40">
              <tr>
                <th className="px-4 py-2.5 text-left font-medium text-zinc-500">账号</th>
                <th className="px-4 py-2.5 text-left font-medium text-zinc-500">日期时间</th>
                <th className="px-4 py-2.5 text-left font-medium text-zinc-500">代码 / 名称</th>
                <th className="px-4 py-2.5 text-left font-medium text-zinc-500">方向</th>
                <th className="px-4 py-2.5 text-right font-medium text-zinc-500">成交数量</th>
                <th className="px-4 py-2.5 text-right font-medium text-zinc-500">成交价</th>
                <th className="px-4 py-2.5 text-right font-medium text-zinc-500">成交额 / 发生额</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800/70">
              {mergedFills.length === 0 ? (
                <tr>
                  <td colSpan={7} className="px-4 py-6 text-center text-zinc-400">
                    暂无历史成交记录
                  </td>
                </tr>
              ) : (
                mergedFills.map((f, idx) => (
                  <tr key={`${f._account_id}-${f.contract_no || idx}`} className="hover:bg-zinc-50/70 dark:hover:bg-zinc-800/40">
                    <td className="px-4 py-2.5 font-mono text-zinc-600 dark:text-zinc-300">{f._account_id}</td>
                    <td className="px-4 py-2.5 font-mono text-zinc-600 dark:text-zinc-300">
                      {f.date || '--'} {f.time || ''}
                    </td>
                    <td className="px-4 py-2.5">
                      <span className="font-mono font-medium text-zinc-900 dark:text-white">{f.code || '--'}</span>
                      <span className="ml-2 text-zinc-600 dark:text-zinc-300">{f.name || ''}</span>
                    </td>
                    <td className="px-4 py-2.5">
                      <span
                        className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${
                          String(f.side || '').toLowerCase() === 'buy'
                            ? 'bg-red-50 text-red-700 dark:bg-red-950/50 dark:text-red-300'
                            : 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300'
                        }`}
                      >
                        {String(f.side || '').toLowerCase() === 'buy' ? '买入' : f.side || '--'}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-right tabular-nums">{f.quantity ?? '--'}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums">{f.price ?? '--'}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums">
                      {fmtMoney(f.amount)} / {fmtMoney(f.net_amount)}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
