import { useEffect, useState } from 'react'
import { Alert } from '@/components/catalyst/alert'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Input } from '@/components/catalyst/input'
import { Select } from '@/components/catalyst/select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/catalyst/table'
import { useToast } from '@/components/toast'
import { Card, ChangeValue, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import { getGen3HoldingTReview, recordGen3HoldingTManualExecution } from '@/api/trading'

type Row = Record<string, any>

function price(value: any): string {
  return value === null || value === undefined ? '--' : Number(value).toFixed(3)
}

function reviewStatus(value: any): string {
  return ({ completed: '已完成归因', needs_actual_fill: '待录入实际成交', observe_only: '仅观察' } as Record<string, string>)[value] || '待复盘'
}

function statusColor(value: any): 'emerald' | 'amber' | 'zinc' {
  return value === 'completed' ? 'emerald' : value === 'needs_actual_fill' ? 'amber' : 'zinc'
}

export default function Gen3HoldingTReview() {
  const toast = useToast()
  const today = new Date().toISOString().slice(0, 10)
  const [tradeDate, setTradeDate] = useState(today)
  const [review, setReview] = useState<Row>({})
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [form, setForm] = useState({
    code: '',
    side: 'sell',
    price: '' as string | number,
    shares: 100,
    filled_at: '',
    note: '',
  })
  const stocks: Row[] = review.stocks || []

  async function load(date = tradeDate) {
    setLoading(true)
    try {
      const payload: any = await getGen3HoldingTReview({ trade_date: date })
      setReview(payload || {})
      if (!form.code && (payload?.stocks || []).length) {
        setForm((prev) => ({ ...prev, code: payload.stocks[0].code }))
      }
    } catch (error: any) {
      toast.error(error?.message || '读取做T复盘失败')
    } finally {
      setLoading(false)
    }
  }

  async function saveExecution() {
    if (!form.code || !form.price || !form.shares || !form.filled_at) {
      toast.error('请完整填写标的、成交价、股数和成交时间')
      return
    }
    setSaving(true)
    try {
      const payload: any = await recordGen3HoldingTManualExecution({ ...form })
      if (!payload?.ok) throw new Error(payload?.message || '保存失败')
      setReview(payload.review)
      toast.success('实际成交已保存并完成归因')
    } catch (error: any) {
      toast.error(error?.message || '保存实际成交失败')
    } finally {
      setSaving(false)
    }
  }

  useEffect(() => {
    load(today)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="flex flex-col">
      <PageHeader
        title="做T复盘"
        subtitle={
          <>
            <span className="font-bold">G3第三代策略 / 盘后复盘</span>
            {' · 逐股还原盘中信号、五档盘口与实际成交，计算相对持股不动的超额收益；本页不连接下单接口。'}
          </>
        }
        actions={
          <div className="flex items-center gap-2">
            <div className="w-44">
              <Input
                type="date"
                value={tradeDate}
                onChange={(e) => {
                  setTradeDate(e.target.value)
                  if (e.target.value) load(e.target.value)
                }}
              />
            </div>
            <Button color="blue" disabled={loading} onClick={() => load()}>{loading ? '刷新中…' : '刷新复盘'}</Button>
          </div>
        }
      />

      <Alert color="zinc" className="text-sm">
        盘中按分钟保存决策快照与五档盘口；实际成交价、时间、数量需由你录入，系统只做归因，不会提交委托。
      </Alert>

      <section className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard label="复盘日期" value={review.trade_date || tradeDate} tone="info" hint="16:00 自动生成" />
        <StatCard label="逐股数量" value={review.stock_count || 0} hint="仅保存的做T标的" />
        <StatCard label="盘中快照" value={review.snapshot_count || 0} hint="每分钟决策与五档" />
        <StatCard label="下单路径" value="已锁定" hint="人工成交仅用于复盘" />
      </section>

      <Card className="mt-4">
        <SectionTitle title="记录实际成交" />
        <p className="mb-3 text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">填写券商实际成交后，系统自动将先卖后买配对，计算扣除预设摩擦成本后的做T超额。</p>
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-sm text-zinc-600 dark:text-zinc-300">
            标的
            <div className="w-40">
              <Select value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })}>
                <option value="">选择标的</option>
                {stocks.map((row: Row) => (
                  <option key={row.code} value={row.code}>{row.code}</option>
                ))}
              </Select>
            </div>
          </label>
          <label className="flex flex-col gap-1 text-sm text-zinc-600 dark:text-zinc-300">
            方向
            <div className="w-36">
              <Select value={form.side} onChange={(e) => setForm({ ...form, side: e.target.value })}>
                <option value="sell">卖出T仓</option>
                <option value="buy">买回T仓</option>
              </Select>
            </div>
          </label>
          <label className="flex flex-col gap-1 text-sm text-zinc-600 dark:text-zinc-300">
            成交价
            <div className="w-32">
              <Input type="number" min={0} step={0.001} value={form.price} onChange={(e) => setForm({ ...form, price: e.target.value })} />
            </div>
          </label>
          <label className="flex flex-col gap-1 text-sm text-zinc-600 dark:text-zinc-300">
            股数
            <div className="w-32">
              <Input type="number" min={1} step={100} value={form.shares} onChange={(e) => setForm({ ...form, shares: Number(e.target.value) })} />
            </div>
          </label>
          <label className="flex flex-col gap-1 text-sm text-zinc-600 dark:text-zinc-300">
            成交时间
            <div className="w-56">
              <Input type="datetime-local" step={1} value={form.filled_at} onChange={(e) => setForm({ ...form, filled_at: e.target.value.replace('T', ' ') })} />
            </div>
          </label>
          <Button color="blue" disabled={saving} onClick={saveExecution}>{saving ? '保存中…' : '保存成交'}</Button>
        </div>
      </Card>

      {stocks.map((stock: Row) => (
        <Card key={stock.code} className="mt-4">
          <SectionTitle title={stock.code} actions={<Badge color={statusColor(stock.review_status)}>{reviewStatus(stock.review_status)}</Badge>} />
          <p className="text-sm text-zinc-500 dark:text-zinc-400">复盘状态：{reviewStatus(stock.review_status)} · 快照 {stock.snapshot_count} 条</p>
          <div className="my-3.5 flex flex-wrap gap-x-6 gap-y-2 text-sm text-zinc-500 dark:text-zinc-400">
            <span>开/收：<strong className="tabular-nums text-zinc-800 dark:text-zinc-200">{price(stock.market_path?.first_price)} / {price(stock.market_path?.last_price)}</strong></span>
            <span>日内高低：<strong className="tabular-nums text-zinc-800 dark:text-zinc-200">{price(stock.market_path?.high_price)} / {price(stock.market_path?.low_price)}</strong></span>
            <span>新鲜Tick：<strong className="tabular-nums text-zinc-800 dark:text-zinc-200">{stock.data_quality?.fresh_tick_count || 0}</strong></span>
            <span>五档留档：<strong className="tabular-nums text-zinc-800 dark:text-zinc-200">{stock.data_quality?.five_level_book_count || 0}</strong></span>
          </div>
          <Table dense>
            <TableHead>
              <TableRow>
                {['信号时间', '动作', '价格', '相对VWAP', 'RSI6', '依据'].map((h) => <TableHeader key={h}>{h}</TableHeader>)}
              </TableRow>
            </TableHead>
            <TableBody>
              {(stock.decision_summary?.signal_events || []).length === 0 && (
                <TableRow>
                  <TableCell colSpan={6} className="text-center text-zinc-500 dark:text-zinc-400">当日无做T信号</TableCell>
                </TableRow>
              )}
              {(stock.decision_summary?.signal_events || []).map((row: Row, idx: number) => (
                <TableRow key={`${row.checked_at}-${idx}`}>
                  <TableCell>{row.checked_at}</TableCell>
                  <TableCell>{row.action}</TableCell>
                  <TableCell className="tabular-nums">{price(row.price)}</TableCell>
                  <TableCell><ChangeValue value={row.price_vs_vwap_pct == null ? null : Number(row.price_vs_vwap_pct)} digits={3} /></TableCell>
                  <TableCell>{row.rsi6}</TableCell>
                  <TableCell className="max-w-[320px] whitespace-normal">{(row.reasons || []).join('；')}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <div className="mt-3.5">
            <Table dense>
              <TableHead>
                <TableRow>
                  {['卖出时间', '卖出价', '买回时间', '买回价', '股数', 'T腿超额', '总资金超额'].map((h) => <TableHeader key={h}>{h}</TableHeader>)}
                </TableRow>
              </TableHead>
              <TableBody>
                {(stock.realized_t_cycles || []).length === 0 && (
                  <TableRow>
                    <TableCell colSpan={7} className="text-center text-zinc-500 dark:text-zinc-400">尚无可配对的实际卖出/买回成交</TableCell>
                  </TableRow>
                )}
                {(stock.realized_t_cycles || []).map((row: Row, idx: number) => (
                  <TableRow key={`${row.sell_filled_at}-${idx}`}>
                    <TableCell>{row.sell_filled_at}</TableCell>
                    <TableCell className="tabular-nums">{row.sell_price}</TableCell>
                    <TableCell>{row.buy_filled_at}</TableCell>
                    <TableCell className="tabular-nums">{row.buy_price}</TableCell>
                    <TableCell className="tabular-nums">{row.shares}</TableCell>
                    <TableCell><ChangeValue value={row.t_leg_excess_pct == null ? null : Number(row.t_leg_excess_pct)} digits={3} /></TableCell>
                    <TableCell><ChangeValue value={row.total_capital_excess_pct == null ? null : Number(row.total_capital_excess_pct)} digits={3} /></TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </Card>
      ))}
    </div>
  )
}
