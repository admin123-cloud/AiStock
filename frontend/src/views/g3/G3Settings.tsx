import { useCallback, useEffect, useState } from 'react'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Card, PageHeader, SectionTitle } from '@/components/ui'
import { getG3Settings, type G3SettingsResponse } from '@/api/g3'

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

  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 p-4 sm:p-6">
      <PageHeader
        title="G3 策略合约与风控"
        description="生效中的做T规则（R34 / R53 / R54 / R56）、L2 风控闸门与保留的有效策略合约"
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
