import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/catalyst/table'
import { Card } from '@/components/ui'
import AR3ExecutionOrderCard from '@/views/g3/AR3ExecutionOrderCard'
import {
  getG3AR3Radar,
  syncG3AR3Data,
  syncG3AR4MacroData,
  updateG3AR3Config,
  type G3AR3Config,
  type G3AR3DualCoreRadarResponse,
  type G3AR3RebalancePlanResponse,
  type G3AR3StockCandidate,
} from '@/api/g3'

interface AR3DualCorePanelProps {
  onPrefillOrder: (payload: {
    code: string
    name: string
    side: 'BUY' | 'SELL'
    order_type: 'base_rebalance' | 't_trade'
    price: string
    shares: string
    note: string
  }) => void
  onAddWatchlist?: (code: string, name: string) => void
  mode?: 'desk_compact' | 'signals_full' | 'all'
  externalRadar?: G3AR3DualCoreRadarResponse | null
  externalRadarLoading?: boolean
  externalPlan?: G3AR3RebalancePlanResponse | null
  onRadarUpdated?: (radar: G3AR3DualCoreRadarResponse) => void
}

const fmtSigned = (v?: number | null, digits = 2): string => {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  const n = Number(v)
  return `${n > 0 ? '+' : ''}${n.toFixed(digits)}%`
}

export default function AR3DualCorePanel({
  onPrefillOrder,
  onAddWatchlist,
  mode = 'all',
  externalRadar,
  externalRadarLoading = false,
  externalPlan,
  onRadarUpdated,
}: AR3DualCorePanelProps) {
  const isExternallyManaged = externalRadar !== undefined
  const [radar, setRadar] = useState<G3AR3DualCoreRadarResponse | null>(externalRadar ?? null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [syncingMacro, setSyncingMacro] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [stockTab, setStockTab] = useState<'exec' | 'f2a' | 'f3a'>('exec')
  const [showEtfUniverse, setShowEtfUniverse] = useState(mode === 'signals_full')
  const [planReloadToken, setPlanReloadToken] = useState(0)

  useEffect(() => {
    if (externalRadar !== undefined) {
      setRadar(externalRadar)
    }
  }, [externalRadar])

  const loadRadar = useCallback(async (forceRefresh = false) => {
    setLoading(true)
    setError('')
    try {
      const res = await getG3AR3Radar(forceRefresh)
      setRadar(res)
      onRadarUpdated?.(res)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '加载 AR4.6 双核雷达失败')
    } finally {
      setLoading(false)
    }
  }, [onRadarUpdated])

  useEffect(() => {
    if (!isExternallyManaged) {
      void loadRadar(false)
    }
  }, [isExternallyManaged, loadRadar])

  const handleSwitchConfig = async (patch: Partial<G3AR3Config>, label: string) => {
    setSaving(true)
    setError('')
    setNotice('')
    try {
      const res = await updateG3AR3Config(patch)
      setRadar(res.radar)
      onRadarUpdated?.(res.radar)
      setPlanReloadToken((n) => n + 1)
      setNotice(`已切换主引擎配置：${label}，上方最终买卖单已同步重算。`)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '切换 AR3 主引擎失败')
    } finally {
      setSaving(false)
    }
  }

  const handleSyncEtf = async () => {
    setSyncing(true)
    setError('')
    setNotice('')
    try {
      const res = await syncG3AR3Data({ sync_etf: true, sync_financials: false })
      setRadar(res.radar)
      onRadarUpdated?.(res.radar)
      setNotice('已完成 15 只核心跨资产 ETF 最新日线同步并刷新双核雷达。')
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '同步跨资产 ETF 数据失败')
    } finally {
      setSyncing(false)
    }
  }

  const handleSyncMacro = async () => {
    setSyncingMacro(true)
    setError('')
    setNotice('')
    try {
      await syncG3AR4MacroData()
      await loadRadar(true)
      setPlanReloadToken((n) => n + 1)
      setNotice('已同步上交所官方国家队 ETF 份额与 ERP 宏观数据，动态股仓与上方买卖单已同步更新。')
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '同步国家队份额与宏观数据失败')
    } finally {
      setSyncingMacro(false)
    }
  }

  const cfg = radar?.config
  const etf = radar?.etf_radar
  const sr = radar?.stock_radar
  const me = sr?.money_effect_20d
  const macro = radar?.macro_dashboard
  const macroDec = macro?.decision

  const activeRows: G3AR3StockCandidate[] =
    stockTab === 'f2a'
      ? (sr?.f2a_candidates || [])
      : stockTab === 'f3a'
        ? (sr?.f3a_candidates || [])
        : (sr?.exec_queue || [])

  return (
    <Card className="border-amber-500/45 bg-zinc-950/90 ring-1 ring-amber-500/15">
      {/* 顶栏：AR4.6 终极双核总控 */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800 pb-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-base font-bold text-white">⚡ AR4.6 终极双核作战雷达</span>
          <Badge color="amber">国家队动态股仓 + 风险调整Top2逆波 + 南向港科滤网</Badge>
          <span className="font-mono text-[11px] text-zinc-400">
            物理单账户实证：4年 +308.0% · 年化 +43.5% · 回撤 19.1% · 卡玛 2.28 · 2026熊市OOS +11.3%（回撤 10.7%）
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <Button outline disabled={loading || externalRadarLoading} onClick={() => void loadRadar(true)}>
            {(loading || externalRadarLoading) ? '扫描中…' : '刷新雷达'}
          </Button>
          <Button outline disabled={syncing} onClick={() => void handleSyncEtf()}>
            {syncing ? '同步中…' : '同步ETF日线'}
          </Button>
          <Link to="/g3/backtest" className="text-amber-300 hover:text-amber-200">
            历史回测验真 →
          </Link>
        </div>
      </div>

      {error && (
        <div className="mt-2.5 rounded-md border border-red-500/40 bg-red-950/30 px-3 py-2 text-xs text-red-200">
          {error}
        </div>
      )}
      {notice && (
        <div className="mt-2.5 rounded-md border border-emerald-500/40 bg-emerald-950/25 px-3 py-2 text-xs text-emerald-200">
          {notice}
        </div>
      )}

      {!radar && loading && (
        <div className="py-8 text-center text-xs text-zinc-400">
          正在扫描 15 只核心跨资产 ETF 与全市场 PIT 基本面（F2A 趋势主升 + F3A 绩优回踩）双雷达…
        </div>
      )}

      {radar && cfg && (
        <div className="mt-3 space-y-4">
          {/* 0. 唯一权威：主力单账号 ct1曾 今日最终买卖执行单（仅实战交易台显示，避免与选股中心重复） */}
          {mode !== 'signals_full' && (
            <AR3ExecutionOrderCard reloadToken={planReloadToken} externalPlan={externalPlan} />
          )}

          {/* 0.5 国家队持仓分位 + 5日净申购 + 南向资金 + ERP 实时仪表盘（仅交易台/全量模式展示） */}
          {mode !== 'signals_full' && macro && (
            <div className="rounded-lg border border-emerald-500/35 bg-gradient-to-r from-emerald-950/20 via-zinc-900/70 to-violet-950/20 p-3 text-xs">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-bold text-white">🏛️ 国家队分位 + 5日净申购 + 南向资金 + ERP 实时仪表盘</span>
                  {macroDec && (
                    <Badge
                      color={
                        macroDec.tone === 'red'
                          ? 'red'
                          : macroDec.tone === 'emerald'
                            ? 'green'
                            : macroDec.tone === 'amber'
                              ? 'amber'
                              : 'blue'
                      }
                    >
                      {macroDec.phase_label}
                    </Badge>
                  )}
                  {macro.trade_date && (
                    <span className="font-mono text-[11px] text-zinc-400">截至 {macro.trade_date}</span>
                  )}
                </div>

                <div className="flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    disabled={saving}
                    onClick={() =>
                      void handleSwitchConfig(
                        { enable_macro_dynamic_cap: !(cfg.enable_macro_dynamic_cap ?? true) },
                        (cfg.enable_macro_dynamic_cap ?? true) ? '关闭国家队动态股仓调节（锁回固定基准）' : '开启国家队分位动态股仓调节（0/35/50/65%）'
                      )
                    }
                    className={`rounded-md border px-2.5 py-1 text-[11px] font-semibold transition ${
                      (cfg.enable_macro_dynamic_cap ?? true)
                        ? 'border-emerald-400/70 bg-emerald-500/20 text-emerald-200'
                        : 'border-zinc-700 bg-zinc-950/70 text-zinc-400 hover:border-zinc-500'
                    }`}
                  >
                    {(cfg.enable_macro_dynamic_cap ?? true) ? '● 动态股仓联动：已开启' : '○ 动态股仓联动：已关闭'}
                  </button>
                  <button
                    type="button"
                    disabled={syncingMacro}
                    onClick={() => void handleSyncMacro()}
                    className="rounded-md border border-zinc-700 bg-zinc-950/70 px-2 py-1 text-[11px] text-zinc-300 transition hover:border-sky-400 hover:text-sky-200 disabled:opacity-50"
                  >
                    {syncingMacro ? '同步中…' : '同步国家队份额'}
                  </button>
                  <Link to="/" className="text-[11px] font-medium text-sky-300 hover:text-sky-200">
                    首页看周期大图 →
                  </Link>
                </div>
              </div>

              {macro.available && macroDec ? (
                <>
                  <div className="mt-2.5 grid grid-cols-2 gap-2 lg:grid-cols-5">
                    <div className="rounded-md border border-zinc-800 bg-zinc-950/80 px-2.5 py-2">
                      <div className="text-[11px] text-zinc-400">国家队 250 日持仓分位</div>
                      <div className="mt-0.5 flex items-baseline gap-2 font-mono">
                        <span className="text-base font-bold text-amber-300">
                          {macro.nt_ratio_pctile_250d?.toFixed(1)}%
                        </span>
                        <span className="text-[10px] text-zinc-400">
                          3大300ETF ¥{macro.hs300_top3_scale_yi?.toFixed(0)}亿
                        </span>
                      </div>
                    </div>

                    <div className="rounded-md border border-zinc-800 bg-zinc-950/80 px-2.5 py-2">
                      <div className="text-[11px] text-zinc-400">6 大宽基 5 日纯份额净申购</div>
                      <div className="mt-0.5 flex items-baseline gap-2 font-mono">
                        <span
                          className={`text-base font-bold ${
                            (macro.broad6_net_inflow_5d_yi ?? 0) >= 0 ? 'text-rose-400' : 'text-emerald-400'
                          }`}
                        >
                          {(macro.broad6_net_inflow_5d_yi ?? 0) >= 0 ? '+' : ''}
                          {macro.broad6_net_inflow_5d_yi?.toFixed(1)}亿
                        </span>
                        <span className="text-[10px] text-zinc-400">
                          5日份额 {fmtSigned(macro.nt_share_chg_5d_pct, 2)}
                        </span>
                      </div>
                    </div>

                    <div className="rounded-md border border-zinc-800 bg-zinc-950/80 px-2.5 py-2">
                      <div className="text-[11px] text-zinc-400">南向资金 20 日净买入（港科滤网）</div>
                      <div className="mt-0.5 flex items-baseline gap-2 font-mono">
                        <span
                          className={`text-base font-bold ${
                            macro.sb_hk_status === 'blocked'
                              ? 'text-emerald-400'
                              : macro.sb_hk_status === 'boosted'
                                ? 'text-rose-400'
                                : 'text-sky-300'
                          }`}
                        >
                          {(macro.sb_net_20d_yi ?? 0) >= 0 ? '+' : ''}
                          {(macro.sb_net_20d_yi ?? 0).toFixed(1)}亿
                        </span>
                        <span className="text-[10px] text-zinc-400">
                          {macro.sb_hk_status === 'blocked'
                            ? '拦截港科'
                            : macro.sb_hk_status === 'boosted'
                              ? '港科+25%'
                              : '中性放行'}
                        </span>
                      </div>
                    </div>

                    <div className="rounded-md border border-zinc-800 bg-zinc-950/80 px-2.5 py-2">
                      <div className="text-[11px] text-zinc-400">沪深300 股债性价比 ERP</div>
                      <div className="mt-0.5 flex items-baseline gap-2 font-mono">
                        <span
                          className={`text-base font-bold ${
                            macroDec.erp_zone === 'overheated' ? 'text-red-400' : 'text-violet-300'
                          }`}
                        >
                          {macro.erp_pct?.toFixed(2)}%
                        </span>
                        <span className="text-[10px] text-zinc-400">
                          PE {macro.hs300_pe_ttm?.toFixed(1)} · 10Y债 {macro.cn_bond_10y?.toFixed(2)}%
                        </span>
                      </div>
                    </div>

                    <div className="rounded-md border border-zinc-800 bg-zinc-950/80 px-2.5 py-2">
                      <div className="text-[11px] text-zinc-400">AR4.6 进攻期生效股仓上限</div>
                      <div className="mt-0.5 flex items-baseline gap-2 font-mono">
                        <span className="text-base font-bold text-emerald-300">
                          {cfg.stock_budget_cap_pct}% → {cfg.effective_stock_budget_cap_pct ?? macroDec.effective_stock_cap_pct}%
                        </span>
                        <span className="text-[10px] text-zinc-400">
                          {radar.is_offensive ? '进攻期即时生效' : '防守期仍硬锁0%'}
                        </span>
                      </div>
                    </div>
                  </div>
                  <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-[11px] text-zinc-300">
                    <span>📌 {cfg.dynamic_cap_reason || macroDec.cap_reason}</span>
                    <span className="text-zinc-400">铁律：非进攻期绝不左侧抢跑；右侧转进攻后按国家队分位自动放/缩股仓。</span>
                  </div>
                </>
              ) : (
                <div className="mt-2 text-[11px] text-amber-300">
                  {macro.error || '宏观国家队数据暂未同步，请点击右上角「同步国家队份额」。'}
                </div>
              )}
            </div>
          )}

          {mode === 'desk_compact' ? (
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-zinc-800/80 bg-zinc-900/40 px-3 py-2 text-xs text-zinc-300">
              <div className="flex flex-wrap items-center gap-3">
                <span>
                  🎯 当前生效配置：
                  <b className="text-amber-300">{cfg.active_stock_mode}</b> +{' '}
                  <b className="text-cyan-300">{etf?.etf_core_label || 'AR4.6 Top2逆波'}</b>
                </span>
                {etf?.headline && <span className="text-zinc-400">· {etf.headline}</span>}
              </div>
              <div className="flex items-center gap-3 shrink-0">
                <Link to="/g3/signals" className="font-semibold text-amber-300 hover:text-amber-200">
                  展开 15 只 ETF 全景 & F2A/F3A 选股排雷 →
                </Link>
                <Link to="/g3/accounts" className="font-medium text-sky-300 hover:text-sky-200">
                  资金账号与底仓做T →
                </Link>
              </div>
            </div>
          ) : (
            <>
              {/* 1. 主引擎热切换控制台 + 近20日赚钱效应指示仪 */}
              <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-3 text-xs">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold text-zinc-200">个股主引擎（随行情赚钱效应热切换）：</span>
                {radar.stock_mode_options.map((opt) => {
                  const active = cfg.active_stock_mode === opt.id
                  return (
                    <button
                      key={opt.id}
                      type="button"
                      disabled={saving}
                      onClick={() => void handleSwitchConfig({ active_stock_mode: opt.id }, opt.short_label)}
                      title={opt.desc}
                      className={`rounded-md border px-2.5 py-1 text-xs font-medium transition ${
                        active
                          ? 'border-amber-400/70 bg-amber-500/20 text-amber-200'
                          : 'border-zinc-700 bg-zinc-950/70 text-zinc-400 hover:border-zinc-500 hover:text-zinc-200'
                      }`}
                    >
                      {active ? '● ' : ''}
                      {opt.short_label}
                    </button>
                  )
                })}
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <span className="text-zinc-400">跨资产ETF底座：</span>
                {radar.etf_mode_options.map((opt) => {
                  const active = cfg.etf_core_mode === opt.id
                  return (
                    <button
                      key={opt.id}
                      type="button"
                      disabled={saving}
                      onClick={() => void handleSwitchConfig({ etf_core_mode: opt.id }, opt.short_label)}
                      title={opt.desc}
                      className={`rounded-md border px-2 py-1 text-[11px] font-medium transition ${
                        active
                          ? 'border-cyan-400/70 bg-cyan-500/20 text-cyan-200'
                          : 'border-zinc-700 bg-zinc-950/70 text-zinc-400 hover:border-zinc-500 hover:text-zinc-200'
                      }`}
                    >
                      {opt.id === 'regime_focus' ? 'AR4.6 风险调整Top2逆波' : '#02 全天候跨类分散Top2'}
                    </button>
                  )
                })}
              </div>
            </div>

            {/* 近 20 日双线赚钱效应监测 */}
            {me && (
              <div className="mt-2.5 grid grid-cols-1 gap-2 border-t border-zinc-800/80 pt-2.5 lg:grid-cols-12">
                <div className="flex flex-wrap items-center gap-3 lg:col-span-5 font-mono text-[11px]">
                  <span className="text-zinc-400">近20日赚钱效应 ({me.window}):</span>
                  <span className={(me.f2a.avg_5d_ret_pct ?? 0) >= 0 ? 'text-emerald-300' : 'text-rose-300'}>
                    F2A趋势 5日均 <b>{fmtSigned(me.f2a.avg_5d_ret_pct)}</b> (胜{me.f2a.win_rate_pct ?? '—'}% · {me.f2a.samples}笔)
                  </span>
                  <span className={(me.f3a.avg_5d_ret_pct ?? 0) >= 0 ? 'text-emerald-300' : 'text-rose-300'}>
                    F3A回踩 5日均 <b>{fmtSigned(me.f3a.avg_5d_ret_pct)}</b> (胜{me.f3a.win_rate_pct ?? '—'}% · {me.f3a.samples}笔)
                  </span>
                </div>
                <div className="lg:col-span-7 text-zinc-300">
                  🧭 <span className="text-zinc-200">{me.headline}</span>
                </div>
              </div>
            )}
          </div>

          {/* 2. #01 / #02 跨资产 ETF 轮动指令 */}
          <div className="rounded-lg border border-cyan-500/35 bg-cyan-950/15 p-3 text-xs">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-bold text-white">
                  🌐 跨资产全天候 ETF 引擎（{etf?.etf_core_label || cfg.etf_core_mode}）
                </span>
                <Badge color="blue">
                  今日 ETF 资金预算：{etf?.etf_total_budget_pct ?? 100}%
                  {radar.is_offensive ? '（含个股未满槽闲置现金）' : '（非进攻期满额承接）'}
                </Badge>
                {etf?.latest_date && (
                  <span className="font-mono text-[11px] text-zinc-400">数据截止 {etf.latest_date}</span>
                )}
              </div>
              <button
                type="button"
                onClick={() => setShowEtfUniverse((v) => !v)}
                className="text-[11px] text-cyan-300 hover:text-cyan-200"
              >
                {showEtfUniverse ? '收起 15 只ETF全景排行 ▲' : '展开 15 只跨资产ETF动量排行 ▼'}
              </button>
            </div>

            {etf?.available ? (
              <>
                <p className="mt-1.5 text-zinc-200">{etf.headline}</p>
                <div className="mt-2.5 grid grid-cols-1 gap-2 md:grid-cols-2 lg:grid-cols-3">
                  {etf.targets.length === 0 && etf.fallback_mode === 'cash_defense' && (
                    <div className="flex flex-col justify-between gap-2 rounded-md border border-amber-500/40 bg-amber-950/20 p-2.5 md:col-span-2 lg:col-span-3">
                      <div className="flex flex-wrap items-center justify-between gap-1.5">
                        <span className="font-semibold text-amber-200">
                          🛡️ F3 分层避险 · 现金 / 国债逆回购 / 短融货基
                        </span>
                        <Badge color="amber">建议现金留存 {etf.cash_defense_pct ?? etf.etf_total_budget_pct ?? 100}%</Badge>
                      </div>
                      <div className="text-[11px] text-amber-100/80">
                        全池跨资产及十年国债(511260)均处于弱势（20日动量 ≤ 0），已自动拦截硬买破位国债；建议空仓持现金、做国债逆回购或配置场内短融/货币 ETF 等待趋势修复。
                      </div>
                    </div>
                  )}
                  {etf.targets.map((item) => (
                    <div
                      key={item.ts_code}
                      className="flex flex-col justify-between gap-2 rounded-md border border-zinc-800 bg-zinc-950/80 p-2.5"
                    >
                      <div>
                        <div className="flex flex-wrap items-center justify-between gap-1.5">
                          <span className="font-semibold text-white">
                            {item.name} <span className="font-mono text-zinc-400">{item.code}</span>
                          </span>
                          <div className="flex items-center gap-1.5">
                            <Badge color="zinc">{item.asset_class_label}</Badge>
                            <Badge color="emerald">建议配额 {item.account_weight_pct}%</Badge>
                          </div>
                        </div>
                        <div className="mt-1 flex flex-wrap items-center gap-2 font-mono text-[11px] text-zinc-300">
                          <span>现价 <b>{item.close.toFixed(3)}</b></span>
                          <span className={item.ret_1d_pct >= 0 ? 'text-rose-300' : 'text-emerald-300'}>
                            日 {fmtSigned(item.ret_1d_pct)}
                          </span>
                          <span className={item.ret_20d_pct >= 0 ? 'text-amber-300' : 'text-zinc-400'}>
                            20日 {fmtSigned(item.ret_20d_pct)}
                          </span>
                          <span>{item.above_ma20 ? '站上MA20 ✅' : '低于MA20'}</span>
                        </div>
                        <div className="mt-1 text-[11px] text-zinc-400">{item.reason}</div>
                        {item.qdii_notice && (
                          <div className="mt-1 text-[11px] text-amber-300">⚠️ {item.qdii_notice}</div>
                        )}
                      </div>
                      <div className="flex justify-end">
                        <Button
                          outline
                          onClick={() =>
                            onPrefillOrder({
                              code: item.code,
                              name: item.name,
                              side: 'BUY',
                              order_type: 'base_rebalance',
                              price: item.close.toFixed(3),
                              shares: '1000',
                              note: `AR4.6 跨资产ETF(${item.asset_class_label}) 目标配额${item.account_weight_pct}%`,
                            })
                          }
                        >
                          一键 L2 预填买入
                        </Button>
                      </div>
                    </div>
                  ))}
                </div>

                {showEtfUniverse && etf.universe.length > 0 && (
                  <div className="mt-3 overflow-x-auto rounded-md border border-zinc-800 bg-zinc-950/70">
                    <Table dense>
                      <TableHead>
                        <TableRow>
                          <TableHeader>标的</TableHeader>
                          <TableHeader>大类</TableHeader>
                          <TableHeader>收盘价</TableHeader>
                          <TableHeader>当日</TableHeader>
                          <TableHeader>10日</TableHeader>
                          <TableHeader>20日动量</TableHeader>
                          <TableHeader>20日波动</TableHeader>
                          <TableHeader>风险调整动量</TableHeader>
                          <TableHeader>站上MA20</TableHeader>
                        </TableRow>
                      </TableHead>
                      <TableBody>
                        {etf.universe.map((u) => (
                          <TableRow key={u.ts_code}>
                            <TableCell className="whitespace-nowrap font-medium text-zinc-100">
                              {u.name} <span className="font-mono text-zinc-400">{u.code}</span>
                            </TableCell>
                            <TableCell className="whitespace-nowrap text-zinc-300">{u.asset_class_label}</TableCell>
                            <TableCell className="whitespace-nowrap font-mono">{u.close.toFixed(3)}</TableCell>
                            <TableCell className={`whitespace-nowrap font-mono ${u.ret_1d_pct >= 0 ? 'text-rose-300' : 'text-emerald-300'}`}>
                              {fmtSigned(u.ret_1d_pct)}
                            </TableCell>
                            <TableCell className="whitespace-nowrap font-mono">{fmtSigned(u.ret_10d_pct)}</TableCell>
                            <TableCell className={`whitespace-nowrap font-mono font-semibold ${u.ret_20d_pct >= 0 ? 'text-amber-300' : 'text-zinc-400'}`}>
                              {fmtSigned(u.ret_20d_pct)}
                            </TableCell>
                            <TableCell className="whitespace-nowrap font-mono text-zinc-400">{u.vol_20d_pct}%</TableCell>
                            <TableCell className="whitespace-nowrap font-mono">{u.risk_adj_mom.toFixed(2)}</TableCell>
                            <TableCell className="whitespace-nowrap">
                              <Badge color={u.eligible ? 'emerald' : 'zinc'}>
                                {u.eligible ? '合格趋势' : '未达标'}
                              </Badge>
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                )}
              </>
            ) : (
              <div className="mt-2 text-amber-300">{etf?.error || 'ETF 雷达暂不可用'}</div>
            )}
          </div>

          {/* 3. F2A（业绩趋势主升）& F3A（绩优高增回踩低吸）双选股雷达 */}
          <div className="rounded-lg border border-zinc-800 bg-zinc-950/70 p-3 text-xs">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-bold text-white">🎯 AR4.6 基本面业绩 × 趋势双选股雷达</span>
                <Badge color={radar.is_offensive ? 'emerald' : 'red'}>
                  {radar.is_offensive
                    ? `进攻期开放 · 个股预算 ${cfg.stock_budget_cap_pct}%（${cfg.max_slots}槽，每行业限1只）`
                    : '非进攻期锁定 · 个股预算 0%（仅作备选观察池）'}
                </Badge>
                {sr?.latest_date && (
                  <span className="font-mono text-[11px] text-zinc-400">
                    PIT公告日对齐截止 {sr.latest_date} · 业绩排雷盾已启用
                  </span>
                )}
              </div>

              <div className="flex flex-wrap items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => setStockTab('exec')}
                  className={`rounded-md border px-2.5 py-1 text-xs font-medium ${
                    stockTab === 'exec'
                      ? 'border-amber-400/70 bg-amber-500/20 text-amber-200'
                      : 'border-zinc-700 bg-zinc-900 text-zinc-400 hover:text-zinc-200'
                  }`}
                >
                  优先执行队列 ({sr?.exec_queue?.length ?? 0})
                </button>
                <button
                  type="button"
                  onClick={() => setStockTab('f2a')}
                  className={`rounded-md border px-2.5 py-1 text-xs font-medium ${
                    stockTab === 'f2a'
                      ? 'border-emerald-400/70 bg-emerald-500/20 text-emerald-200'
                      : 'border-zinc-700 bg-zinc-900 text-zinc-400 hover:text-zinc-200'
                  }`}
                >
                  F2A 业绩趋势主升 ({sr?.f2a_total_hits ?? 0})
                </button>
                <button
                  type="button"
                  onClick={() => setStockTab('f3a')}
                  className={`rounded-md border px-2.5 py-1 text-xs font-medium ${
                    stockTab === 'f3a'
                      ? 'border-blue-400/70 bg-blue-500/20 text-blue-200'
                      : 'border-zinc-700 bg-zinc-900 text-zinc-400 hover:text-zinc-200'
                  }`}
                >
                  F3A 绩优回踩低吸 ({sr?.f3a_total_hits ?? 0})
                </button>
              </div>
            </div>

            {!radar.is_offensive && (
              <div className="mt-2 rounded-md border border-amber-500/35 bg-amber-950/20 px-3 py-1.5 text-[11px] text-amber-200">
                🔒 当前大盘处于非进攻期（{radar.regime}）：按 AR4.6 物理单账户纪律，个股仓位预算为 0%，禁止新开个股仓；下方名单仅保留作为大盘转入进攻期后的第一梯队基本面储备池。
              </div>
            )}

            {sr?.available ? (
              activeRows.length === 0 ? (
                <div className="py-5 text-center text-zinc-400">
                  {stockTab === 'f3a'
                    ? '今日暂无同时满足「高增长高ROE + 中期多头 + 缩量回踩MA10且当日收阳 >+0.5%」的 F3A 严格买点（宁缺毋滥）。'
                    : '今日暂无满足严格 PIT 业绩 + 趋势共振门槛的候选标的。'}
                </div>
              ) : (
                <div className="mt-2.5 overflow-x-auto">
                  <Table dense>
                    <TableHead>
                      <TableRow>
                        <TableHeader>槽位 / 来源</TableHeader>
                        <TableHeader>标的 & 行业</TableHeader>
                        <TableHeader>现价 / 当日 / 20日</TableHeader>
                        <TableHeader>PIT 公告日 & 业绩</TableHeader>
                        <TableHeader>均额 / MA10乖离</TableHeader>
                        <TableHeader>硬止损(-10%)</TableHeader>
                        <TableHeader>操作</TableHeader>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {activeRows.map((row) => (
                        <TableRow key={`${stockTab}-${row.ts_code}`}>
                          <TableCell className="whitespace-nowrap">
                            <div className="flex items-center gap-1.5">
                              {row.industry_duplicate ? (
                                <Badge color="zinc">同业跳过</Badge>
                              ) : row.slot_rank && row.slot_rank <= cfg.max_slots ? (
                                <Badge color={radar.is_offensive ? 'emerald' : 'amber'}>#{row.slot_rank}槽</Badge>
                              ) : (
                                <Badge color="zinc">备选</Badge>
                              )}
                              <Badge
                                color={
                                  row.source_engine === 'BOTH'
                                    ? 'amber'
                                    : row.source_engine === 'F2A'
                                      ? 'emerald'
                                      : 'blue'
                                }
                              >
                                {row.source_label}
                              </Badge>
                            </div>
                          </TableCell>
                          <TableCell className="whitespace-nowrap">
                            <div className="font-semibold text-white">
                              {row.name} <span className="font-mono text-zinc-400">{row.code}</span>
                            </div>
                            <div className="text-[11px] text-zinc-400">{row.industry} · 评分 {row.score}</div>
                          </TableCell>
                          <TableCell className="whitespace-nowrap font-mono">
                            <div className="text-zinc-100">
                              ¥{row.close.toFixed(2)}{' '}
                              <span className={row.ret_1d_pct >= 0 ? 'text-rose-300' : 'text-emerald-300'}>
                                {fmtSigned(row.ret_1d_pct)}
                              </span>
                            </div>
                            <div className="text-[11px] text-zinc-400">
                              20日 {fmtSigned(row.ret_20d_pct)} · RS {fmtSigned(row.rs_20d_pct)}
                            </div>
                          </TableCell>
                          <TableCell className="whitespace-nowrap font-mono">
                            <div className="text-emerald-300">
                              净利 {fmtSigned(row.net_profit_yoy_pct, 1)} · 营收 {fmtSigned(row.revenue_yoy_pct, 1)} · ROE {row.roe_pct ?? '—'}%
                            </div>
                            <div className="text-[11px] text-zinc-400">
                              公告日 {row.notice_date}（报告期 {row.report_date}）
                            </div>
                          </TableCell>
                          <TableCell className="whitespace-nowrap font-mono text-zinc-300">
                            <div>{row.amt_20d_yi.toFixed(1)}亿/日</div>
                            <div className="text-[11px] text-zinc-400">
                              MA10乖离 {fmtSigned(row.ma10_bias_pct)} · 量比 {row.vol_ratio_20d.toFixed(2)}
                            </div>
                          </TableCell>
                          <TableCell className="whitespace-nowrap font-mono text-amber-300">
                            ¥{row.stop_loss_price.toFixed(2)}
                          </TableCell>
                          <TableCell className="whitespace-nowrap">
                            <div className="flex items-center gap-1.5">
                              {onAddWatchlist && (
                                <Button outline onClick={() => onAddWatchlist(row.code, row.name)}>
                                  +观察池
                                </Button>
                              )}
                              <Button
                                outline
                                disabled={!radar.is_offensive}
                                onClick={() =>
                                  onPrefillOrder({
                                    code: row.code,
                                    name: row.name,
                                    side: 'BUY',
                                    order_type: 'base_rebalance',
                                    price: row.close.toFixed(2),
                                    shares: '100',
                                    note: `AR4.6 ${row.source_label} 公告${row.notice_date} 净利+${row.net_profit_yoy_pct}% 止损${row.stop_loss_price}`,
                                  })
                                }
                              >
                                {radar.is_offensive ? '一键预填' : '非进攻锁定'}
                              </Button>
                            </div>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              )
            ) : (
              <div className="mt-2 text-amber-300">{sr?.error || '双选股雷达暂不可用'}</div>
            )}
          </div>
            </>
          )}
        </div>
      )}
    </Card>
  )
}
