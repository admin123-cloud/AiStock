import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Button } from '@/components/catalyst/button'
import { Badge } from '@/components/catalyst/badge'
import {
  Card,
  ChangeValue,
  EmptyState,
  PageHeader,
  PageLoading,
  SectionTitle,
  StatCard,
} from '@/components/ui'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/catalyst/table'
import LocalKLineChart, { type KLineDataItem } from '@/components/LocalKLineChart'
import { getStockDetail, getStockKline } from '@/api/stock'
import { getIndices } from '@/api/market'

interface IndexInfo {
  code: string
  name: string
  market?: string
  price?: number | null
  change?: number | null
  change_pct?: number | null
  amount?: number | string | null
  [key: string]: unknown
}

interface KlineResponse {
  data?: KLineDataItem[]
  total?: number
  count?: number
}

const PERIODS = [
  { label: '15分钟', value: '15m' },
  { label: '30分钟', value: '30m' },
  { label: '60分钟', value: '60m' },
  { label: '日K', value: '1d' },
  { label: '周K', value: '1w' },
  { label: '月K', value: '1mon' },
]

const PERIOD_LABEL_MAP: Record<string, string> = Object.fromEntries(
  PERIODS.map((p) => [p.value, p.label]),
)

function getMarketLabel(market?: string): string {
  const normalized = String(market || '').toLowerCase()
  const labels: Record<string, string> = { sh: '上海', sz: '深圳', bj: '北京' }
  return labels[normalized] || '指数'
}

function changeTone(value?: number | null): 'rise' | 'fall' | 'flat' {
  if (value !== null && value !== undefined && value > 0) return 'rise'
  if (value !== null && value !== undefined && value < 0) return 'fall'
  return 'flat'
}

const TONE_CLASS: Record<'rise' | 'fall' | 'flat', string> = {
  rise: 'text-red-600 dark:text-red-400',
  fall: 'text-green-600 dark:text-green-500',
  flat: 'text-zinc-600 dark:text-zinc-300',
}

function formatAmount(amount?: number | string | null): string {
  if (amount === null || amount === undefined) return '未知'
  if (typeof amount === 'string') return amount
  if (amount >= 100000000) return (amount / 100000000).toFixed(2) + '亿'
  if (amount >= 10000) return (amount / 10000).toFixed(2) + '万'
  return Number(amount).toFixed(2)
}

function formatNumber(num?: number | string | null): string {
  if (num === null || num === undefined) return '未知'
  if (typeof num === 'string') return num
  if (num >= 100000000) return (num / 100000000).toFixed(2) + '亿'
  if (num >= 10000) return (num / 10000).toFixed(2) + '万'
  return Number(num).toFixed(2)
}

function formatTradeDate(value?: string | number | null): string {
  if (!value) return '-'
  const text = String(value).trim()
  if (text.includes('T')) return text.split('T')[0]
  if (text.includes(' ')) return text.split(' ')[0]
  return text.slice(0, 10)
}

export default function IndexDetail() {
  const { code: routeCode } = useParams()
  const navigate = useNavigate()

  const [code, setCode] = useState(routeCode)
  const [indexInfo, setIndexInfo] = useState<IndexInfo | null>(null)
  const [klineData, setKlineData] = useState<KlineResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [chartLoading, setChartLoading] = useState(false)
  const [selectedPeriod, setSelectedPeriod] = useState('1d')
  const [isNotIndex, setIsNotIndex] = useState(false)

  const loadStockInfo = useCallback(async (stockCode: string) => {
    try {
      const data: any = await getStockDetail(stockCode)
      if (data && data.stock) {
        setIsNotIndex(true)
        setIndexInfo({
          code: data.stock.code,
          name: data.stock.name,
          market: data.stock.market,
          price: data.stock.price,
          change: data.stock.change,
          change_pct: data.stock.change_pct,
          amount: data.stock.amount,
        })
      }
    } catch (error) {
      console.error('加载股票信息失败:', error)
    }
  }, [])

  const loadIndexInfo = useCallback(
    async (stockCode: string) => {
      if (!stockCode) return
      setLoading(true)
      try {
        const data: any = await getIndices({ code: stockCode })
        if (data && data.indices && data.indices.length > 0) {
          setIndexInfo(data.indices[0])
        } else {
          setIndexInfo(null)
          await loadStockInfo(stockCode)
        }
      } catch (error) {
        console.error('加载指数信息失败:', error)
        setIndexInfo(null)
        await loadStockInfo(stockCode)
      } finally {
        setLoading(false)
      }
    },
    [loadStockInfo],
  )

  const loadKlineData = useCallback(async (stockCode: string, period: string) => {
    if (!stockCode) return
    setChartLoading(true)
    try {
      const isMinutePeriod = ['15m', '30m', '60m'].includes(period)
      const limit = isMinutePeriod ? 240 : 90
      const data: any = await getStockKline(stockCode, period, { limit })
      setKlineData(data)
    } catch (error) {
      console.error('加载K线数据失败:', error)
      setKlineData(null)
    } finally {
      setChartLoading(false)
    }
  }, [])

  // 路由参数变化 + 首次加载
  useEffect(() => {
    if (routeCode && routeCode !== code) {
      setCode(routeCode)
    }
  }, [routeCode]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!code) return
    setIsNotIndex(false)
    loadIndexInfo(code)
    loadKlineData(code, selectedPeriod)
  }, [code]) // eslint-disable-line react-hooks/exhaustive-deps

  const selectPeriod = (period: string) => {
    setSelectedPeriod(period)
    loadKlineData(code!, period)
  }

  const refreshKlineData = () => {
    loadKlineData(code!, selectedPeriod)
  }

  const goBack = () => navigate(-1)

  const bars = klineData?.data ?? []

  const pctTone = changeTone(indexInfo?.change_pct) === 'rise' ? 'up' : changeTone(indexInfo?.change_pct) === 'fall' ? 'down' : 'default'
  const chgTone = changeTone(indexInfo?.change) === 'rise' ? 'up' : changeTone(indexInfo?.change) === 'fall' ? 'down' : 'default'

  return (
    <div className="min-h-[calc(100vh-60px)] bg-zinc-50 p-5 dark:bg-zinc-950">
      <div className="mx-auto max-w-[1400px]">
        {/* 页头 */}
        <PageHeader
          title={indexInfo ? `${indexInfo.name} (${indexInfo.code})` : `指数 ${code || ''}`}
          subtitle={
            loading
              ? '加载中…'
              : indexInfo
                ? getMarketLabel(indexInfo.market) + (isNotIndex ? ' · 股票' : ' · 指数')
                : '指数详情'
          }
          actions={
            <Button plain onClick={goBack}>
              ← 返回
            </Button>
          }
        />

        {/* 指数基本信息 */}
        <Card className="mb-5">
          {loading ? (
            <PageLoading />
          ) : indexInfo ? (
            <div>
              <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
                <div className="flex flex-wrap items-center gap-3">
                  <Badge color="blue">{getMarketLabel(indexInfo.market)}</Badge>
                  {isNotIndex && <Badge color="emerald">股票</Badge>}
                </div>
                <ChangeValue value={indexInfo.change_pct} className="text-xl" />
              </div>
              <div className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-4">
                <StatCard label="最新价" value={indexInfo.price ? indexInfo.price.toFixed(2) : 'N/A'} />
                <StatCard label="涨跌幅" value={<ChangeValue value={indexInfo.change_pct} />} tone={pctTone} />
                <StatCard label="涨跌额" value={<ChangeValue value={indexInfo.change} suffix="" />} tone={chgTone} />
                <StatCard label="成交额" value={formatAmount(indexInfo.amount)} />
              </div>
            </div>
          ) : null}
        </Card>

        {/* K线图表 */}
        <Card className="mb-5">
          <SectionTitle
            title="K线图表 (最近90天)"
            actions={
              <div className="flex flex-wrap justify-end gap-2">
                {PERIODS.map((period) => (
                  <Button
                    key={period.value}
                    {...(selectedPeriod === period.value ? { color: 'indigo' as const } : { plain: true as const })}
                    onClick={() => selectPeriod(period.value)}
                  >
                    {period.label}
                  </Button>
                ))}
              </div>
            }
          />
          {chartLoading ? (
            <PageLoading />
          ) : (
            <LocalKLineChart
              data={bars}
              interval={selectedPeriod}
              title={`${indexInfo?.name || code || ''} ${PERIOD_LABEL_MAP[selectedPeriod] || selectedPeriod}K线`}
              height={820}
            />
          )}
          {!chartLoading && bars.length > 0 ? (
            <div className="mt-4 flex items-center justify-between border-t border-zinc-200 pt-4 text-sm text-zinc-500 dark:border-zinc-800 dark:text-zinc-400">
              <span>显示 {klineData?.total ?? klineData?.count ?? bars.length} 条数据</span>
              <Button plain onClick={refreshKlineData}>
                刷新
              </Button>
            </div>
          ) : null}
          {!chartLoading && bars.length === 0 ? (
            <EmptyState text="暂无K线数据" />
          ) : null}
        </Card>

        {/* 历史数据表格 */}
        <Card className="mb-5">
          <SectionTitle title="历史数据 (最近20条)" />
          {chartLoading ? (
            <PageLoading />
          ) : bars.length === 0 ? (
            <EmptyState text="暂无数据" />
          ) : (
            <Table>
              <TableHead>
                <TableRow>
                  <TableHeader>日期</TableHeader>
                  <TableHeader>开盘</TableHeader>
                  <TableHeader>最高</TableHeader>
                  <TableHeader>最低</TableHeader>
                  <TableHeader>收盘</TableHeader>
                  <TableHeader>成交量</TableHeader>
                  <TableHeader>成交额</TableHeader>
                  <TableHeader>涨跌幅</TableHeader>
                  <TableHeader>换手率</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {[...bars].slice(-20).reverse().map((item, i) => {
                  const closePct = item.change_pct == null ? NaN : Number(item.change_pct)
                  const closeUp = closePct > 0
                  const closeDown = closePct < 0
                  const closeTone = closeUp ? 'rise' : closeDown ? 'fall' : 'flat'
                  let pctText = '未知'
                  const pct = item.change_pct
                  if (pct !== undefined && pct !== null) {
                    pctText = `${Number(pct).toFixed(2)}%`
                  }
                  return (
                    <TableRow key={i}>
                      <TableCell className="tabular-nums">{formatTradeDate(item.date ?? item.datetime)}</TableCell>
                      <TableCell className="tabular-nums">{item.open ?? '未知'}</TableCell>
                      <TableCell className="tabular-nums">{item.high ?? '未知'}</TableCell>
                      <TableCell className="tabular-nums">{item.low ?? '未知'}</TableCell>
                      <TableCell className={`tabular-nums ${TONE_CLASS[closeTone]}`}>{item.close ?? '未知'}</TableCell>
                      <TableCell className="tabular-nums">{formatNumber(item.volume)}</TableCell>
                      <TableCell className="tabular-nums">{formatNumber(item.amount)}</TableCell>
                      <TableCell className={`tabular-nums ${TONE_CLASS[closeTone]}`}>{pctText}</TableCell>
                      <TableCell className="tabular-nums">{item.turnover_rate == null ? '未知' : `${Number(item.turnover_rate).toFixed(2)}%`}</TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          )}
        </Card>
      </div>
    </div>
  )
}
