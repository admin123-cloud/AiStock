import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/catalyst/table'
import { Card, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import {
  getG3Desk,
  syncG3WatchlistFromThs,
  upsertG3Watchlist,
  type G3DeskCard,
  type G3DeskResponse,
} from '@/api/g3'

const fmtNum = (v?: number | null, digits = 2): string =>
  v != null && Number.isFinite(Number(v)) ? Number(v).toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits }) : '—'

const fmtBand = (band?: [number, number] | null): string =>
  band && band.length === 2 ? `${fmtNum(band[0])} ~ ${fmtNum(band[1])}` : '—'

const regimeBadge = (regime: string) => {
  if (regime === 'up') return { label: '上升周期', color: 'emerald' as const }
  if (regime === 'down') return { label: '下降周期', color: 'red' as const }
  if (regime === 'sideways') return { label: '横盘周期', color: 'amber' as const }
  return { label: '未判定', color: 'zinc' as const }
}

export default function G3Desk() {
  const [data, setData] = useState<G3DeskResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [newCode, setNewCode] = useState('')
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const res = await getG3Desk()
      setData(res)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const handleSyncThs = async () => {
    setSyncing(true)
    setMsg('')
    setError('')
    try {
      await syncG3WatchlistFromThs()
      setMsg('已从双账号同花顺持仓同步做T观察名单')
      await load()
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
      await load()
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
      await load()
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '移除失败')
    }
  }

  const summary = data?.accounts_summary
  const cards: G3DeskCard[] = data?.cards || []

  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 p-4 sm:p-6">
      <PageHeader
        title="G3 做T实战台"
        description={`交易日 ${data?.trade_date || '—'} · 三周期判定 × 1m快/5m慢双车道 × 双账号真实持仓`}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Button outline onClick={handleSyncThs} disabled={syncing}>
              {syncing ? '同步中…' : '从同花顺同步名单'}
            </Button>
            <Button onClick={() => void load()} disabled={loading}>
              {loading ? '刷新中…' : '刷新'}
            </Button>
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

      {/* 顶部核心指标 */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard
          label="双账号总资产"
          value={summary?.total_assets != null ? `¥${fmtNum(summary.total_assets)}` : '—'}
          hint={`持仓市值 ¥${fmtNum(summary?.total_market_value)}`}
        />
        <StatCard
          label="账户与桥接"
          value={`${summary?.accounts_ready ?? 0} / ${summary?.account_count ?? 0} 就绪`}
          hint={summary?.bridge_online ? '桌面桥接在线' : '桌面桥接离线'}
        />
        <StatCard
          label="做T监控池"
          value={`${cards.length} 只`}
          hint={`真实持仓 ${data?.holding_count ?? 0} · 观察名单 ${data?.watchlist_count ?? 0}`}
        />
        <StatCard
          label="当日信号草案"
          value={`${data?.drafts?.length ?? 0} 条`}
          hint={`周期版本 ${data?.regime_meta?.version || '—'}`}
        />
      </div>

      {/* 手动添加观察标的 */}
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="text-sm text-zinc-300">
            实时票池由 <span className="font-medium text-white">双账号真实持仓 ∪ 做T观察名单</span> 自动合并，T 仓上限按 <code className="rounded bg-zinc-800 px-1.5 py-0.5 text-xs text-zinc-200">floor(底仓/3/100)×100</code> 计算。
          </div>
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={newCode}
              onChange={(e) => setNewCode(e.target.value)}
              placeholder="输入 6 位代码如 002015"
              className="w-44 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm text-white placeholder-zinc-500 focus:border-blue-500 focus:outline-none"
            />
            <Button outline onClick={handleAddWatch}>加入观察</Button>
          </div>
        </div>
      </Card>

      {/* 逐票实战卡片 */}
      <SectionTitle title="持仓与监控标的（三周期 × 价带 × 布防）" />
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        {cards.map((c) => {
          const rb = regimeBadge(c.regime)
          const armedCount = Object.values(c.armed_sides || {}).filter((s) => s?.armed).length
          return (
            <Card key={c.code}>
              <div className="flex flex-wrap items-start justify-between gap-2 border-b border-zinc-800 pb-3">
                <div>
                  <div className="flex items-center gap-2">
                    <Link
                      to={`/stock/${c.code}`}
                      className="text-base font-semibold text-white hover:text-blue-400"
                    >
                      {c.name} <span className="font-mono text-sm text-zinc-400">{c.code}</span>
                    </Link>
                    <Badge color={rb.color}>{rb.label}</Badge>
                    {c.in_holdings && <Badge color="blue">持仓</Badge>}
                    {c.in_watchlist && <Badge color="zinc">观察</Badge>}
                  </div>
                  <div className="mt-1 text-xs text-zinc-400">
                    昨收 {fmtNum(c.prev_close)} · 5日涨跌 {c.chg5d_pct != null ? `${fmtNum(c.chg5d_pct)}%` : '—'} · 20日位置 {c.pos20 != null ? `${(c.pos20 * 100).toFixed(1)}%` : '—'}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <div className="text-right">
                    <div className="text-sm font-medium text-white">底仓 {c.total_shares.toLocaleString()} 股</div>
                    <div className="text-xs text-emerald-400">单笔 T 上限 {c.t_lot_cap.toLocaleString()} 股</div>
                  </div>
                  {c.in_watchlist && (
                    <Button plain onClick={() => void handleRemoveWatch(c.code)}>
                      移出
                    </Button>
                  )}
                </div>
              </div>

              {/* 均线与做T带位参数 */}
              <div className="mt-3 grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
                <div className="rounded-lg bg-zinc-900/80 p-2.5">
                  <div className="text-zinc-400">买入价带</div>
                  <div className="mt-1 font-mono text-sm font-medium text-emerald-300">{fmtBand(c.params?.buy_band)}</div>
                  <div className="mt-0.5 text-zinc-500">RSI5m ≤ {c.params?.buy_rsi5_max ?? '—'}</div>
                </div>
                <div className="rounded-lg bg-zinc-900/80 p-2.5">
                  <div className="text-zinc-400">卖出价带</div>
                  <div className="mt-1 font-mono text-sm font-medium text-rose-300">{fmtBand(c.params?.sell_band)}</div>
                  <div className="mt-0.5 text-zinc-500">RSI5m ≥ {c.params?.sell_rsi5_min ?? '—'}</div>
                </div>
                <div className="rounded-lg bg-zinc-900/80 p-2.5">
                  <div className="text-zinc-400">日线均线</div>
                  <div className="mt-1 font-mono text-zinc-200">
                    M5 {fmtNum(c.ma5)} / M10 {fmtNum(c.ma10)}
                  </div>
                  <div className="mt-0.5 font-mono text-zinc-400">M20 {fmtNum(c.ma20)}</div>
                </div>
                <div className="rounded-lg bg-zinc-900/80 p-2.5">
                  <div className="text-zinc-400">止损与布防</div>
                  <div className="mt-1 font-mono text-sm text-amber-300">止损 {fmtNum(c.params?.stop)}</div>
                  <div className="mt-0.5 text-zinc-400">在防通道 {armedCount} 个</div>
                </div>
              </div>

              {/* 分账号持仓明细 */}
              {c.holdings.length > 0 && (
                <div className="mt-3 border-t border-zinc-800/80 pt-2.5 text-xs text-zinc-300">
                  {c.holdings.map((h, idx) => (
                    <div key={`${h.account_id}-${idx}`} className="flex flex-wrap items-center justify-between gap-2 py-0.5">
                      <span>
                        <span className="font-medium text-zinc-100">{h.account_label}</span> · 持有 {h.shares} 股（可用 {h.available_shares}）
                      </span>
                      <span className="font-mono">
                        成本 {fmtNum(h.cost_price, 3)} · 现价 {fmtNum(h.current_price)} · 浮盈{' '}
                        <span className={Number(h.profit_loss || 0) >= 0 ? 'text-rose-400' : 'text-emerald-400'}>
                          {fmtNum(h.profit_loss)} ({fmtNum(h.profit_pct)}%)
                        </span>
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          )
        })}
      </div>

      {/* 当日信号草案 */}
      <Card>
        <SectionTitle title="当日做T信号草案（快车道 / 慢车道）" />
        {!data?.drafts?.length ? (
          <div className="py-6 text-center text-sm text-zinc-400">今日暂无触发草案</div>
        ) : (
          <Table dense>
            <TableHead>
              <TableRow>
                <TableHeader>时间</TableHeader>
                <TableHeader>标的</TableHeader>
                <TableHeader>方向</TableHeader>
                <TableHeader>触发类型</TableHeader>
                <TableHeader>触发价</TableHeader>
                <TableHeader>状态</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {data.drafts.map((d, i) => (
                <TableRow key={i}>
                  <TableCell className="font-mono text-xs text-zinc-300">{String(d.ts || d.created_at || '—')}</TableCell>
                  <TableCell className="font-mono text-zinc-100">{String(d.code || '—')}</TableCell>
                  <TableCell>{String(d.side || '—')}</TableCell>
                  <TableCell className="text-xs text-zinc-300">{String(d.trigger_kind || d.reason || '—')}</TableCell>
                  <TableCell className="font-mono">{String(d.price ?? d.trigger_price ?? '—')}</TableCell>
                  <TableCell>
                    <Badge color="zinc">{String(d.bar_status || d.status || '—')}</Badge>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Card>
    </div>
  )
}
