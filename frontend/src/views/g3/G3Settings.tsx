import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/catalyst/table'
import { Card, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import { getG3Settings, type G3SettingsResponse } from '@/api/g3'

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

export default function G3Settings() {
  const [data, setData] = useState<G3SettingsResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const res = await getG3Settings()
      setData(res)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const rules = data?.t_trade_rules
  const matrix = data?.strategy_matrix

  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 p-4 sm:p-6">
      <PageHeader
        title="G3 策略矩阵与风控总控台"
        subtitle="Phase 4 三腿互补矩阵 × 动态资金配额 × 6 大宽基 ETF 波动率目标趋势 × R34/R51/L2 风控门禁"
        actions={
          <Button onClick={() => void load()} disabled={loading}>
            {loading ? '刷新中…' : '刷新'}
          </Button>
        }
      />

      {error && (
        <div className="rounded-lg border border-red-500/40 bg-red-500/10 px-4 py-2.5 text-sm text-red-200">
          {error}
        </div>
      )}

      {/* Phase 4：三腿互补策略矩阵与动态配额 */}
      {matrix && (
        <div className="flex flex-col gap-4">
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <StatCard
              label="进攻腿 · 主升与龙头"
              value={`${matrix.allocation.offense_pct}%`}
              hint={`大盘状态：${matrix.regime_label}`}
            />
            <StatCard
              label="稳健腿 · 宽基 ETF"
              value={`${matrix.allocation.defense_etf_pct}%`}
              hint={matrix.defense_auto_cash ? '宽基全破位，已自动转现金' : '按波动率目标趋势配置'}
            />
            <StatCard
              label="增强腿 · 底仓做T"
              value={`${matrix.allocation.t_enhance_pct}%`}
              hint={`当前双账号实际水位 ${matrix.allocation.actual_position_pct != null ? `${matrix.allocation.actual_position_pct}%` : '—'}`}
            />
            <StatCard
              label="现金留存目标"
              value={`${matrix.allocation.cash_pct}%`}
              hint={`有效投资上限 ≤ ${matrix.allocation.total_invest_cap_pct}%`}
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

          {/* 稳健腿：6 大宽基 ETF 实时波动率目标趋势信号 */}
          <Card>
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <SectionTitle title="稳健/防守腿 · 6 大宽基 ETF 波动率目标趋势信号（目标年化波动 15%）" />
              <Badge color={matrix.defense_auto_cash ? 'red' : 'emerald'}>
                {matrix.defense_auto_cash ? '全部破位 · 转现金防守' : '存在上升趋势可配标的'}
              </Badge>
            </div>
            <div className="overflow-x-auto">
              <Table>
                <TableHead>
                  <TableRow>
                    <TableHeader>场内 ETF</TableHeader>
                    <TableHeader>跟踪指数</TableHeader>
                    <TableHeader>风格</TableHeader>
                    <TableHeader>收盘 / MA20 / MA60</TableHeader>
                    <TableHeader>近20日涨跌</TableHeader>
                    <TableHeader>20日年化波动</TableHeader>
                    <TableHeader>趋势信号</TableHeader>
                    <TableHeader>波动率目标仓位</TableHeader>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {matrix.defense_etf_signals.map((row) => (
                    <TableRow key={row.index_code}>
                      <TableCell>
                        <div className="font-medium text-white">{row.etf_name}</div>
                        <div className="font-mono text-xs text-zinc-400">{row.etf_code}</div>
                      </TableCell>
                      <TableCell className="text-xs text-zinc-300">
                        {row.index_name} <span className="font-mono text-zinc-500">({row.index_code})</span>
                      </TableCell>
                      <TableCell className="text-xs text-zinc-400">{row.style}</TableCell>
                      <TableCell className="font-mono text-xs text-zinc-200">
                        {row.close ?? '—'} / {row.ma20 ?? '—'} / {row.ma60 ?? '—'}
                      </TableCell>
                      <TableCell className={`font-mono text-xs ${(row.ret_20d_pct ?? 0) > 0 ? 'text-red-400' : 'text-emerald-400'}`}>
                        {row.ret_20d_pct != null ? `${row.ret_20d_pct > 0 ? '+' : ''}${row.ret_20d_pct.toFixed(2)}%` : '—'}
                      </TableCell>
                      <TableCell className="font-mono text-xs text-zinc-300">
                        {row.ann_vol_20d_pct != null ? `${row.ann_vol_20d_pct.toFixed(1)}%` : '—'}
                      </TableCell>
                      <TableCell>
                        <Badge color={etfSignalColor(row.signal)}>{row.signal_label}</Badge>
                      </TableCell>
                      <TableCell className="font-mono text-xs font-semibold text-white">
                        {row.vol_target_weight_pct.toFixed(1)}%
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </Card>

          {/* 策略研发与末位淘汰 4 道门禁 */}
          <Card>
            <SectionTitle title="AI 研投副驾驶 · 策略准入与末位淘汰门禁" />
            <div className="mt-3 grid grid-cols-1 gap-2.5 md:grid-cols-2 lg:grid-cols-4">
              {matrix.governance_pipeline.map((g) => (
                <div key={g.stage} className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3 text-xs">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-semibold text-white">{g.stage}</span>
                    <Badge color={g.status === 'enforced' ? 'emerald' : 'blue'}>
                      {g.status === 'enforced' ? '生效中' : '按季执行'}
                    </Badge>
                  </div>
                  <p className="mt-1.5 leading-relaxed text-zinc-400">{g.rule}</p>
                </div>
              ))}
            </div>
          </Card>
        </div>
      )}

      {/* 当前生效做T规则 */}
      {rules && (
        <Card>
          <SectionTitle title="生效中的做T交易规则（R34 × R53 × R54 × R56）" />
          <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
            <div className="rounded-lg bg-zinc-900/80 p-3.5 text-sm">
              <div className="font-medium text-white">周期与方向门</div>
              <div className="mt-1.5 text-xs text-zinc-300">• {rules.regime_engine}</div>
              <div className="mt-1 text-xs text-zinc-300">• {rules.direction_gate}</div>
              <div className="mt-1 text-xs text-zinc-300">• {rules.position_discipline.lunch_gate}</div>
            </div>
            <div className="rounded-lg bg-zinc-900/80 p-3.5 text-sm">
              <div className="font-medium text-white">上升周期做T参数（{rules.uptrend_params.source}）</div>
              <div className="mt-1.5 text-xs text-zinc-300">
                • 卖出：RSI5m ≥ <span className="font-mono text-rose-300">{rules.uptrend_params.sell_trim_rsi5m}</span> 减半 / ≥{' '}
                <span className="font-mono text-rose-300">{rules.uptrend_params.sell_exit_rsi5m}</span> 清仓
              </div>
              <div className="mt-1 text-xs text-zinc-300">
                • 买入：RSI5m ≤ <span className="font-mono text-emerald-300">{rules.uptrend_params.buy_normal_rsi5m}</span> 常规 / ≤{' '}
                <span className="font-mono text-emerald-300">{rules.uptrend_params.buy_conviction_rsi5m}</span> 确定性买
              </div>
              <div className="mt-1 text-xs text-zinc-300">
                • 仓位上限：{rules.position_discipline.t_lot_formula} · 单票日限 {rules.position_discipline.max_daily_triggers_per_stock} 次 · 冷却 {rules.position_discipline.cooldown_minutes} 分钟
              </div>
            </div>
          </div>
        </Card>
      )}

      {/* 保留的有效策略合约 */}
      <SectionTitle title={`保留的有效策略合约（${data?.strategy_contracts?.length ?? 0} 份）`} />
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
        {(data?.strategy_contracts || []).map((c) => (
          <Card key={c.file}>
            <div className="flex items-start justify-between gap-2">
              <div className="text-sm font-semibold text-white">{c.name}</div>
              <Badge color={c.status.includes('formal') ? 'emerald' : 'blue'}>{c.status}</Badge>
            </div>
            <div className="mt-1 font-mono text-xs text-zinc-400">{c.file}</div>
            {c.description && <p className="mt-2 text-xs text-zinc-300">{c.description}</p>}
          </Card>
        ))}
      </div>

      {/* L2 风控门原始配置 */}
      <Card>
        <SectionTitle title="L2 风控闸门配置（l2_risk_config.json）" />
        <pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap rounded-lg bg-zinc-950/70 p-3 font-mono text-xs text-zinc-300">
          {JSON.stringify(data?.l2_risk_config || {}, null, 2)}
        </pre>
      </Card>
    </div>
  )
}
