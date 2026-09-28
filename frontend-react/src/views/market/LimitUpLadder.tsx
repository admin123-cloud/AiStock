import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { getLimitUpLadder } from '@/api/market'
import { Button } from '@/components/catalyst/button'
import { Card, ChangeValue, EmptyState, PageHeader, PageLoading, SectionTitle, StatCard } from '@/components/ui'

interface LadderStock {
  code: string
  name: string
  change_pct?: number | string
  streak?: number
  turnover_rate?: number | string
}

interface LadderBucket {
  streak: number
  label: string
  count: number
  items: LadderStock[]
}

interface LadderHistoryRow {
  date: string
  buckets?: Record<string, number>
}

interface LadderSummary {
  limit_up_total: number
  ladder_total: number
  highest_streak: number
}

interface LadderParams {
  min_streak: number
  max_streak: number
  history_days: number
}

const DEFAULT_SUMMARY: LadderSummary = { limit_up_total: 0, ladder_total: 0, highest_streak: 0 }
const DEFAULT_PARAMS: LadderParams = { min_streak: 2, max_streak: 7, history_days: 10 }

function formatPct(value: unknown) {
  const n = Number(value ?? 0)
  return `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`
}

export default function LimitUpLadder() {
  const navigate = useNavigate()
  const [loading, setLoading] = useState(false)
  const [buckets, setBuckets] = useState<LadderBucket[]>([])
  const [history, setHistory] = useState<LadderHistoryRow[]>([])
  const [tradeDate, setTradeDate] = useState('')
  const [params, setParams] = useState<LadderParams>(DEFAULT_PARAMS)
  const [summary, setSummary] = useState<LadderSummary>(DEFAULT_SUMMARY)

  const loadLadder = useCallback(async () => {
    setLoading(true)
    try {
      const response = (await getLimitUpLadder(params)) as
        | (LadderResponse & { error?: string })
        | undefined
      if (response?.error) {
        throw new Error(response.error)
      }
      setBuckets(Array.isArray(response?.buckets) ? response.buckets : [])
      setHistory(Array.isArray(response?.history) ? response.history : [])
      setTradeDate(response?.trade_date || '')
      setSummary(response?.summary || DEFAULT_SUMMARY)
      setParams(response?.params || DEFAULT_PARAMS)
    } catch (error) {
      console.error('加载连板天梯失败:', error)
      setBuckets([])
      setHistory([])
      setTradeDate('')
      setSummary(DEFAULT_SUMMARY)
    } finally {
      setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params])

  useEffect(() => {
    void loadLadder()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="mx-auto max-w-[1500px] p-5">
      <PageHeader
        title="连板天梯"
        subtitle="按最新交易日连板高度分层展示涨停梯队"
        actions={
          <>
            <span className="text-sm text-zinc-500 tabular-nums dark:text-zinc-400">
              交易日：{tradeDate || '--'}
            </span>
            <Button outline onClick={() => void loadLadder()} disabled={loading}>
              {loading ? '加载中...' : '刷新'}
            </Button>
          </>
        }
      />

      <section className="mb-3.5 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <StatCard label="当日涨停总数" value={summary.limit_up_total} tone="up" />
        <StatCard label="连板梯队总数" value={summary.ladder_total} />
        <StatCard label="最高连板" value={`${summary.highest_streak} 板`} tone="info" />
      </section>

      {loading ? (
        <PageLoading className="mb-4" />
      ) : (
        <section className="mb-4 grid grid-cols-1 items-start gap-3 min-[1281px]:grid-cols-6 lg:grid-cols-3 xl:grid-cols-6">
          {buckets.map((bucket) => (
            <Card key={bucket.streak} className="min-h-[260px] p-0">
              <div className="flex items-center justify-between border-b border-zinc-200 px-3 pt-3 pb-2 dark:border-zinc-800">
                <div className="text-[15px] font-bold text-zinc-800 dark:text-zinc-200">
                  {bucket.label}
                </div>
                <div className="min-w-[28px] rounded-full bg-blue-800 px-2 py-0.5 text-center text-xs font-bold text-white tabular-nums dark:bg-blue-600">
                  {bucket.count}
                </div>
              </div>
              {bucket.items.length === 0 ? (
                <EmptyState text="暂无" className="mx-2.5 my-5 py-4" />
              ) : (
                <div className="flex flex-col gap-2 p-2.5">
                  {bucket.items.map((stock) => (
                    <button
                      key={stock.code}
                      type="button"
                      onClick={() => navigate(`/stock/${stock.code}`)}
                      className="cursor-pointer rounded-lg border border-zinc-200 bg-white px-2.5 py-2 text-left transition-colors hover:border-blue-400 dark:border-zinc-700 dark:bg-zinc-800 dark:hover:border-blue-500"
                    >
                      <div className="mb-1 flex items-baseline justify-between gap-1.5">
                        <span className="text-sm font-semibold text-zinc-800 dark:text-zinc-200">
                          {stock.name}
                        </span>
                        <span className="text-xs text-zinc-500 tabular-nums dark:text-zinc-400">
                          {stock.code}
                        </span>
                      </div>
                      <div className="flex items-center justify-between gap-1.5 text-xs text-zinc-500 dark:text-zinc-400">
                        <ChangeValue value={Number(stock.change_pct)} className="font-bold" />
                        <span className="tabular-nums">连板 {stock.streak}</span>
                        <span className="tabular-nums">换手 {formatPct(stock.turnover_rate)}</span>
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </Card>
          ))}
        </section>
      )}

      {history.length > 0 && (
        <Card className="p-3.5">
          <SectionTitle title={`近${params.history_days || 10}日梯队趋势`} className="mb-2.5" />
          <div className="overflow-x-auto">
              <table className="w-full border-collapse text-[13px]">
                <thead>
                  <tr>
                    <th className="border-b border-slate-200 px-2.5 py-2 text-left font-semibold text-slate-600 whitespace-nowrap dark:border-zinc-800 dark:text-zinc-400">
                      日期
                    </th>
                    {buckets.map((bucket) => (
                      <th
                        key={`head-${bucket.streak}`}
                        className="border-b border-slate-200 px-2.5 py-2 text-center font-semibold text-slate-600 whitespace-nowrap dark:border-zinc-800 dark:text-zinc-400"
                      >
                        {bucket.label}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {history.map((row) => (
                    <tr key={row.date}>
                      <td className="border-b border-slate-200 px-2.5 py-2 text-left text-slate-700 whitespace-nowrap tabular-nums dark:border-zinc-800 dark:text-zinc-300">
                        {row.date}
                      </td>
                      {buckets.map((bucket) => (
                        <td
                          key={`${row.date}-${bucket.streak}`}
                          className="border-b border-slate-200 px-2.5 py-2 text-center text-slate-700 whitespace-nowrap tabular-nums dark:border-zinc-800 dark:text-zinc-300"
                        >
                          {row.buckets?.[String(bucket.streak)] || 0}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
          </div>
        </Card>
      )}
    </div>
  )
}

interface LadderResponse {
  buckets?: LadderBucket[]
  history?: LadderHistoryRow[]
  trade_date?: string
  summary?: LadderSummary
  params?: LadderParams
}
