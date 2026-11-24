import { useCallback, useEffect, useState } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/catalyst/table'
import { Card, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import G3Mining from '@/views/g3/G3Mining'
import {
  getG3NotifyConfig,
  getG3Settings,
  saveG3NotifyConfig,
  testG3WechatNotify,
  type G3NotifyConfig,
  type G3SettingsResponse,
} from '@/api/g3'

type LabTab = 'mining' | 'settings'

const legBadgeColor = (status: string) => {
  if (status === 'active') return 'emerald' as const
  if (status === 'halted' || status === 'review_needed') return 'red' as const
  if (status === 'cash_defense' || status === 'throttled' || status === 'observe_only') return 'amber' as const
  return 'zinc' as const
}

const etfSignalColor = (sig: string) => {
  if (sig === 'long_trend') return 'emerald' as const
  if (sig === 'neutral') return 'amber' as const
  if (sig === 'cash_defense') return 'red' as const
  return 'zinc' as const
}

const AUTO_JOBS = [
  { time: '工作日 16:15', name: '全市场日线 K 线同步', runner: 'aistock-pipeline-runner', status: '24/7 容器级' },
  { time: '工作日 16:40', name: '市场情绪 / 涨跌停宽度计算', runner: 'aistock-pipeline-runner', status: '24/7 容器级' },
  { time: '工作日 17:15', name: 'AR4.6 全链路自动喂料（ETF+国家队+南向+策略刷新）', runner: 'aistock-pipeline-runner', status: '24/7 容器级' },
  { time: '工作日 09:10 / 17:45', name: '盘前晨会简报 & 盘后 AI 排雷与复盘', runner: 'Antigravity Sidecar', status: '宿主机常驻' },
  { time: '工作日 09:15 / 15:15', name: 'acct1 AR4.6 盘前计划 & 日终真钱复盘推送', runner: 'aistock-pipeline-runner', status: '24/7 容器级' },
  { time: '交易日 09:35~14:50', name: 'acct1 AR4.6 盘中每10m智能买卖提醒（微信单向推）', runner: 'aistock-pipeline-runner', status: '24/7 容器级' },
]

export default function G3Lab() {
  const location = useLocation()
  const [searchParams, setSearchParams] = useSearchParams()
  const isSettingsRoute = location.pathname === '/g3/settings'
  const activeTab: LabTab =
    searchParams.get('tab') === 'settings' || (isSettingsRoute && searchParams.get('tab') !== 'mining')
      ? 'settings'
      : 'mining'

  const [settingsData, setSettingsData] = useState<G3SettingsResponse | null>(null)
  const [notifyCfg, setNotifyCfg] = useState<G3NotifyConfig | null>(null)
  const [loadingSettings, setLoadingSettings] = useState(false)
  const [showRawJson, setShowRawJson] = useState(false)

  // 通知配置表单状态
  const [desktopPerm, setDesktopPerm] = useState<NotificationPermission | 'unsupported'>(() =>
    typeof window !== 'undefined' && 'Notification' in window ? Notification.permission : 'unsupported'
  )
  const [wxEnabled, setWxEnabled] = useState(true)
  const [wxChannel, setWxChannel] = useState('ilink_bridge')
  const [wxPushplusToken, setWxPushplusToken] = useState('')
  const [wxWecomWebhook, setWxWecomWebhook] = useState('')
  const [wxServerchanKey, setWxServerchanKey] = useState('')
  const [wxQqEmail, setWxQqEmail] = useState('')
  const [wxOnlyTrading, setWxOnlyTrading] = useState(true)
  const [savingNotify, setSavingNotify] = useState(false)
  const [testingWechat, setTestingWechat] = useState(false)
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')

  const switchTab = (next: LabTab) => {
    const params = new URLSearchParams(searchParams)
    params.set('tab', next)
    setSearchParams(params, { replace: true })
  }

  const syncNotifyForm = useCallback((cfg?: G3NotifyConfig | null) => {
    if (!cfg) return
    setNotifyCfg(cfg)
    setWxEnabled(Boolean(cfg.enabled))
    setWxChannel(cfg.channel || 'ilink_bridge')
    setWxQqEmail(cfg.qq_email_to || '')
    setWxOnlyTrading(cfg.only_trading_hours !== false)
  }, [])

  const loadSettings = useCallback(async () => {
    setLoadingSettings(true)
    setError('')
    try {
      const [s, n] = await Promise.all([
        getG3Settings(),
        getG3NotifyConfig().catch(() => null),
      ])
      setSettingsData(s)
      syncNotifyForm(n)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '加载策略参数与通知配置失败')
    } finally {
      setLoadingSettings(false)
    }
  }, [syncNotifyForm])

  useEffect(() => {
    if (activeTab === 'settings') {
      void loadSettings()
    }
  }, [activeTab, loadSettings])

  const handleRequestDesktopPermission = async () => {
    if (typeof window === 'undefined' || !('Notification' in window)) {
      setError('当前浏览器不支持系统桌面通知，请使用 Chrome / Edge 打开')
      return
    }
    const perm = await Notification.requestPermission()
    setDesktopPerm(perm)
    if (perm === 'granted') {
      setMsg('已开启 Windows 桌面通知！')
    } else if (perm === 'denied') {
      setError('浏览器已拦截通知权限，请点击地址栏左侧锁图标 → 允许「通知」')
    }
  }

  const handleSaveNotify = async () => {
    setSavingNotify(true)
    setError('')
    setMsg('')
    try {
      const res = await saveG3NotifyConfig({
        enabled: wxEnabled,
        channel: wxChannel,
        pushplus_token: wxPushplusToken || undefined,
        wecom_webhook: wxWecomWebhook || undefined,
        serverchan_sendkey: wxServerchanKey || undefined,
        qq_email_to: wxQqEmail,
        only_trading_hours: wxOnlyTrading,
      })
      syncNotifyForm(res.config)
      setWxPushplusToken('')
      setWxWecomWebhook('')
      setWxServerchanKey('')
      setMsg('微信通知配置已保存！')
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '保存配置失败')
    } finally {
      setSavingNotify(false)
    }
  }

  const handleTestWechat = async () => {
    setTestingWechat(true)
    setError('')
    setMsg('')
    try {
      const res = await testG3WechatNotify()
      setMsg(`微信测试通知发送成功（${res.channel}）：${res.detail}`)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '微信测试发送失败')
    } finally {
      setTestingWechat(false)
    }
  }

  const matrix = settingsData?.strategy_matrix
  const rules = settingsData?.t_trade_rules
  const l2 = (settingsData?.l2_risk_config || {}) as Record<string, unknown>

  return (
    <div className="flex w-full flex-col gap-4">
      <PageHeader
        title="G3 策略研投与风控设置中心"
        subtitle="AI 策略挖掘与防过拟合实验工坊 × AR4.6 策略矩阵、L2 硬风控门禁、24/7 自动喂料与微信通知配置"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Link to="/g3/desk">
              <Button outline>⚡ 返回实战交易台 →</Button>
            </Link>
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
            onClick={() => switchTab('mining')}
            className={`rounded-lg px-4 py-1.5 text-xs font-semibold transition ${
              activeTab === 'mining'
                ? 'bg-blue-600 text-white shadow-sm'
                : 'text-zinc-400 hover:text-zinc-100'
            }`}
          >
            🧪 AI 策略挖掘工坊
          </button>
          <button
            type="button"
            onClick={() => switchTab('settings')}
            className={`rounded-lg px-4 py-1.5 text-xs font-semibold transition ${
              activeTab === 'settings'
                ? 'bg-blue-600 text-white shadow-sm'
                : 'text-zinc-400 hover:text-zinc-100'
            }`}
          >
            ⚙️ 策略矩阵、风控与通知设置
          </button>
        </div>

        {activeTab === 'settings' && (
          <Button outline onClick={() => void loadSettings()} disabled={loadingSettings}>
            {loadingSettings ? '刷新中…' : '刷新配置'}
          </Button>
        )}
      </div>

      {activeTab === 'mining' ? (
        <G3Mining embedded />
      ) : (
        <div className="flex flex-col gap-5">
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

          {/* 1. 通知中心（从原交易台迁移至此，彻底清爽） */}
          <Card className="border-blue-500/35 bg-zinc-900/80">
            <SectionTitle title="1. 交易与风控通知中心（Windows 桌面弹窗 + 单向微信推送）" />
            <div className="mt-3 grid grid-cols-1 gap-4 lg:grid-cols-2">
              <div className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3.5 text-xs">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm font-medium text-white">Windows 浏览器桌面通知</span>
                  <Badge color={desktopPerm === 'granted' ? 'emerald' : desktopPerm === 'denied' ? 'red' : 'amber'}>
                    {desktopPerm === 'granted' ? '已授权' : desktopPerm === 'denied' ? '已拦截' : '待授权'}
                  </Badge>
                </div>
                <p className="mt-1.5 leading-relaxed text-zinc-400">
                  开启后，即使浏览器切到后台看同花顺，只要「底仓做T」或「止损哨兵」触发预警，Windows 右下角会立即弹出系统提醒。
                </p>
                {desktopPerm !== 'granted' && (
                  <div className="mt-3">
                    <Button onClick={() => void handleRequestDesktopPermission()}>一键开启 Windows 桌面通知</Button>
                  </div>
                )}
              </div>

              <div className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3.5 text-xs">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm font-medium text-white">服务端微信推送通道</span>
                  <label className="inline-flex cursor-pointer items-center gap-1.5 text-zinc-200">
                    <input
                      type="checkbox"
                      checked={wxEnabled}
                      onChange={(e) => setWxEnabled(e.target.checked)}
                      className="h-4 w-4 rounded border-zinc-600 bg-zinc-900"
                    />
                    启用微信推送
                  </label>
                </div>

                <div className="mt-2.5 grid grid-cols-1 gap-2.5 sm:grid-cols-2">
                  <div>
                    <label className="block text-zinc-400">推送通道</label>
                    <select
                      value={wxChannel}
                      onChange={(e) => setWxChannel(e.target.value)}
                      className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-xs text-white"
                    >
                      <option value="ilink_bridge">AiStock 专属微信（iLink 免费通道）</option>
                      <option value="qq_email">QQ 邮箱微信提醒（SMTP）</option>
                      <option value="wecom_webhook">企业微信群机器人 Webhook</option>
                      <option value="serverchan">Server酱 Turbo</option>
                      <option value="pushplus">PushPlus 推送加</option>
                    </select>
                  </div>

                  {wxChannel === 'ilink_bridge' && (
                    <div className="rounded-lg border border-zinc-800 bg-zinc-900/70 px-2.5 py-2 text-zinc-300">
                      <div>
                        桥接状态：
                        <span className={notifyCfg?.ilink_bridge_online ? 'font-semibold text-emerald-400' : 'font-semibold text-amber-400'}>
                          {notifyCfg?.ilink_bridge_online ? '在线（已连微信）' : '离线'}
                        </span>
                      </div>
                      <div className="mt-0.5 text-[11px] text-zinc-400">
                        账号 {notifyCfg?.ilink_account_id || 'f9fe6b0d0535@im.bot'}
                      </div>
                    </div>
                  )}

                  {wxChannel === 'pushplus' && (
                    <div>
                      <label className="block text-zinc-400">PushPlus Token</label>
                      <input
                        type="password"
                        value={wxPushplusToken}
                        onChange={(e) => setWxPushplusToken(e.target.value)}
                        placeholder={notifyCfg?.has_pushplus_token ? '留空保持原 Token' : '输入 Token'}
                        className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-xs text-white"
                      />
                    </div>
                  )}

                  {wxChannel === 'wecom_webhook' && (
                    <div>
                      <label className="block text-zinc-400">企业微信 Webhook</label>
                      <input
                        type="text"
                        value={wxWecomWebhook}
                        onChange={(e) => setWxWecomWebhook(e.target.value)}
                        placeholder={notifyCfg?.has_wecom_webhook ? '留空保持原 Webhook' : 'https://qyapi.weixin.qq.com/...'}
                        className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-xs text-white"
                      />
                    </div>
                  )}

                  {wxChannel === 'serverchan' && (
                    <div>
                      <label className="block text-zinc-400">Server酱 SendKey</label>
                      <input
                        type="password"
                        value={wxServerchanKey}
                        onChange={(e) => setWxServerchanKey(e.target.value)}
                        placeholder={notifyCfg?.has_serverchan_sendkey ? '留空保持原 SendKey' : 'SCT...'}
                        className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-xs text-white"
                      />
                    </div>
                  )}

                  {wxChannel === 'qq_email' && (
                    <div>
                      <label className="block text-zinc-400">接收 QQ 邮箱</label>
                      <input
                        type="email"
                        value={wxQqEmail}
                        onChange={(e) => setWxQqEmail(e.target.value)}
                        placeholder="290402028@qq.com"
                        className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-xs text-white"
                      />
                    </div>
                  )}
                </div>

                <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
                  <label className="inline-flex cursor-pointer items-center gap-1.5 text-zinc-400">
                    <input
                      type="checkbox"
                      checked={wxOnlyTrading}
                      onChange={(e) => setWxOnlyTrading(e.target.checked)}
                      className="h-3.5 w-3.5 rounded border-zinc-600 bg-zinc-900"
                    />
                    仅交易时段（09:15~15:05）自动推
                  </label>
                  <div className="flex items-center gap-2">
                    <Button outline onClick={() => void handleSaveNotify()} disabled={savingNotify}>
                      {savingNotify ? '保存中…' : '保存配置'}
                    </Button>
                    <Button onClick={() => void handleTestWechat()} disabled={testingWechat}>
                      {testingWechat ? '发送中…' : '发一条测试微信'}
                    </Button>
                  </div>
                </div>
              </div>
            </div>
          </Card>

          {/* 2. 24/7 自动喂料与任务调度总览 */}
          <Card>
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <SectionTitle title="2. AR4.6 24/7 自动喂料与策略切换调度" />
              <Badge color="emerald">全自动无值守运行中</Badge>
            </div>
            <div className="grid grid-cols-1 gap-2.5 md:grid-cols-2 lg:grid-cols-5">
              {AUTO_JOBS.map((job) => (
                <div key={job.name} className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3 text-xs">
                  <div className="flex items-center justify-between gap-1">
                    <span className="font-mono font-semibold text-blue-300">{job.time}</span>
                    <Badge color="emerald">{job.status}</Badge>
                  </div>
                  <div className="mt-1.5 font-medium text-white">{job.name}</div>
                  <div className="mt-1 font-mono text-[11px] text-zinc-400">{job.runner}</div>
                </div>
              ))}
            </div>
          </Card>

          {/* 3. AR4.6 策略矩阵与三腿配额 */}
          {matrix && (
            <>
              <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                <StatCard
                  label="进攻核 · AR4.6 个股 (AB混合)"
                  value={`${matrix.allocation.offense_pct}%`}
                  hint={`大盘状态：${matrix.regime_label}`}
                />
                <StatCard
                  label="压舱核 · AR4.6 Top2 逆波 ETF"
                  value={`${matrix.allocation.defense_etf_pct}%`}
                  hint="跨资产全天候风险调整轮动"
                />
                <StatCard
                  label="辅助核 · 已有底仓做T与止损哨兵"
                  value={`${matrix.allocation.t_enhance_pct}%`}
                  hint={`当前实盘水位 ${matrix.allocation.actual_position_pct != null ? `${matrix.allocation.actual_position_pct}%` : '—'}`}
                />
                <StatCard
                  label="机动现金留存"
                  value={`${matrix.allocation.cash_pct}%`}
                  hint={`有效总投资上限 ≤ ${matrix.allocation.total_invest_cap_pct}%`}
                />
              </div>

              <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
                {matrix.legs.map((leg) => (
                  <Card key={leg.leg_id}>
                    <div className="flex items-start justify-between gap-2">
                      <div>
                        <div className="text-sm font-semibold text-white">{leg.name}</div>
                        <div className="mt-0.5 text-xs text-zinc-400">{leg.role}</div>
                      </div>
                      <Badge color={legBadgeColor(leg.status)}>{leg.status_label}</Badge>
                    </div>
                    <div className="mt-3 flex items-center justify-between rounded-md bg-zinc-950/70 px-3 py-2 text-xs">
                      <span className="text-zinc-400">目标配额</span>
                      <span className="font-mono text-base font-bold text-white">{leg.target_pct}%</span>
                    </div>
                    <p className="mt-2.5 text-xs leading-relaxed text-zinc-300">{leg.feedback_note}</p>
                    <div className="mt-3 flex items-center justify-between border-t border-zinc-800 pt-2 text-xs">
                      <span className="text-zinc-400">绑定合约：{leg.contracts_count} 份</span>
                      <Link to={leg.action_route} className="font-medium text-blue-400 hover:underline">
                        {leg.action_label} →
                      </Link>
                    </div>
                  </Card>
                ))}
              </div>

              {/* 6 大宽基参考温度计 */}
              <Card>
                <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                  <SectionTitle title="3. A 股 6 大宽基趋势温度计（参考）" />
                  <Badge color={matrix.defense_auto_cash ? 'amber' : 'emerald'}>
                    {matrix.defense_auto_cash ? 'A 股宽基全破位 · 已切非A股 Top2 逆波ETF' : 'A 股宽基存在上升趋势'}
                  </Badge>
                </div>
                <div className="overflow-x-auto">
                  <Table dense>
                    <TableHead>
                      <TableRow>
                        <TableHeader>场内 ETF</TableHeader>
                        <TableHeader>风格</TableHeader>
                        <TableHeader>收盘 / MA20 / MA60</TableHeader>
                        <TableHeader>近20日涨跌</TableHeader>
                        <TableHeader>20日年化波动</TableHeader>
                        <TableHeader>趋势信号</TableHeader>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {matrix.defense_etf_signals.map((row) => (
                        <TableRow key={row.index_code}>
                          <TableCell className="text-xs">
                            <span className="font-medium text-white">{row.etf_name}</span>{' '}
                            <span className="font-mono text-zinc-400">{row.etf_code}</span>
                          </TableCell>
                          <TableCell className="text-xs text-zinc-400">{row.style}</TableCell>
                          <TableCell className="font-mono text-xs text-zinc-200">
                            {row.close ?? '—'} / {row.ma20 ?? '—'} / {row.ma60 ?? '—'}
                          </TableCell>
                          <TableCell className={`font-mono text-xs ${(row.ret_20d_pct ?? 0) > 0 ? 'text-rose-400' : 'text-emerald-400'}`}>
                            {row.ret_20d_pct != null ? `${row.ret_20d_pct > 0 ? '+' : ''}${row.ret_20d_pct.toFixed(2)}%` : '—'}
                          </TableCell>
                          <TableCell className="font-mono text-xs text-zinc-300">
                            {row.ann_vol_20d_pct != null ? `${row.ann_vol_20d_pct.toFixed(1)}%` : '—'}
                          </TableCell>
                          <TableCell>
                            <Badge color={etfSignalColor(row.signal)}>{row.signal_label}</Badge>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              </Card>
            </>
          )}

          {/* 4. L2 硬风控门禁 + 做T规则 */}
          <Card>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <SectionTitle title="4. L2 硬风控门禁与底仓做T规则" />
              <Button plain onClick={() => setShowRawJson((v) => !v)}>
                {showRawJson ? '收起原始 JSON' : '查看原始 l2_risk_config.json'}
              </Button>
            </div>

            <div className="mt-3 grid grid-cols-2 gap-3 text-xs md:grid-cols-4">
              <div className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3">
                <div className="text-zinc-400">下单安全模式</div>
                <div className="mt-1 font-mono text-sm font-bold text-emerald-300">
                  {String(l2.mode || 'prefill_only')}
                </div>
                <div className="mt-0.5 text-[11px] text-zinc-500">绝不自动点击买入/卖出确认</div>
              </div>
              <div className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3">
                <div className="text-zinc-400">单笔金额硬上限</div>
                <div className="mt-1 font-mono text-sm font-bold text-white">
                  ¥{Number(l2.max_single_order_yuan ?? 25000).toLocaleString('zh-CN')}
                </div>
                <div className="mt-0.5 text-[11px] text-zinc-500">超额自动拆分至 L2 门禁内</div>
              </div>
              <div className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3">
                <div className="text-zinc-400">做T触发频次 / 冷却</div>
                <div className="mt-1 font-mono text-sm font-bold text-white">
                  {rules?.position_discipline.max_daily_triggers_per_stock ?? 3} 次/日 · {rules?.position_discipline.cooldown_minutes ?? 30}m
                </div>
                <div className="mt-0.5 text-[11px] text-zinc-500">{rules?.position_discipline.t_lot_formula || 'floor(底仓/3/100)*100'}</div>
              </div>
              <div className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3">
                <div className="text-zinc-400">有效策略合约</div>
                <div className="mt-1 font-mono text-sm font-bold text-white">
                  {settingsData?.strategy_contracts?.length ?? 0} 份
                </div>
                <div className="mt-0.5 text-[11px] text-zinc-500">含 AR4.6 与做T正式合约</div>
              </div>
            </div>

            {showRawJson && (
              <pre className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap rounded-lg bg-zinc-950/70 p-3 font-mono text-xs text-zinc-300">
                {JSON.stringify(settingsData?.l2_risk_config || {}, null, 2)}
              </pre>
            )}
          </Card>
        </div>
      )}
    </div>
  )
}
