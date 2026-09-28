import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import type { EChartsCoreOption } from 'echarts'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
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
import EChart from '@/components/EChart'
import LocalKLineChart, { type KLineDataItem } from '@/components/LocalKLineChart'
import { getSectorDetail, getSectorHistory, getSectorKline } from '@/api/market'
import { toast } from '@/components/toast'

interface SectorStock {
  code: string
  name: string
  market?: string
  change_pct?: number | null
  volume?: number | null
  amount?: number | null
}

interface SectorInfo {
  code: string
  name: string
  type?: string
  stock_count?: number
  trade_date?: string
  avg_change_pct?: number | null
  total_volume?: number | null
  total_amount?: number | null
  rise_count?: number
  fall_count?: number
  flat_count?: number
  stocks?: SectorStock[]
}

interface SectorHistoryItem {
  trade_date: string
  rise_count: number
  fall_count: number
  flat_count: number
  change_pct?: number | null
}

interface SectorKlineItem {
  trade_date: string
  open?: number | null
  close?: number | null
  low?: number | null
  high?: number | null
  total_volume?: number | null
  total_amount?: number | null
}

// ---------- 工具函数（与旧版一致） ----------

function getTypeLabel(type?: string) {
  const typeMap: Record<string, string> = {
    industry: '行业板块',
    theme: '概念板块',
  }
  return type ? (typeMap[type] ?? type) : ''
}

function changeClass(changePct?: number | null) {
  if ((changePct ?? 0) > 0) return 'text-red-600 dark:text-red-400'
  if ((changePct ?? 0) < 0) return 'text-green-600 dark:text-green-500'
  return 'text-zinc-600 dark:text-zinc-400'
}

function formatVolume(volume?: number | null): string {
  if (!volume) return 'N/A'
  if (volume >= 100000000) return (volume / 100000000).toFixed(2) + '亿'
  if (volume >= 10000) return (volume / 10000).toFixed(2) + '万'
  return String(volume)
}

function formatAmount(amount?: number | null): string {
  if (!amount) return 'N/A'
  if (amount >= 100000000) return (amount / 100000000).toFixed(2) + '亿'
  if (amount >= 10000) return (amount / 10000).toFixed(2) + '万'
  return String(amount)
}

function riseRatioValue(rise: number, fall: number, flat: number) {
  const total = rise + fall + flat
  if (total === 0) return 0
  return (rise / total) * 100
}

function formatPct(pct?: number | null) {
  return pct != null && !Number.isNaN(pct) ? pct.toFixed(2) + '%' : 'N/A'
}

// ---------- 组件 ----------

export default function SectorDetail() {
  const { code } = useParams()
  const navigate = useNavigate()

  const [loading, setLoading] = useState(false)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [sectorInfo, setSectorInfo] = useState<SectorInfo | null>(null)
  const [sectorStocks, setSectorStocks] = useState<SectorStock[]>([])
  const [sectorHistory, setSectorHistory] = useState<SectorHistoryItem[]>([])
  const [klineData, setKlineData] = useState<SectorKlineItem[]>([])

  useEffect(() => {
    if (!code) {
      navigate('/sectors')
      return
    }

    let cancelled = false

    const loadDetail = async () => {
      setLoading(true)
      try {
        // 响应拦截器已解包 data（运行时返回业务数据），此处做类型断言
        const data = (await getSectorDetail(code)) as unknown as SectorInfo
        if (cancelled) return
        setSectorInfo(data)
        setSectorStocks(data.stocks ?? [])
        // 加载板块历史数据（旧版在详情成功后加载）
        setHistoryLoading(true)
        try {
          const history = (await getSectorHistory(code)) as unknown as SectorHistoryItem[]
          if (!cancelled) setSectorHistory(history ?? [])
        } catch (err) {
          console.error('加载板块历史数据失败:', err)
        } finally {
          if (!cancelled) setHistoryLoading(false)
        }
      } catch (err: unknown) {
        console.error('加载板块详情失败:', err)
        toast.error('加载板块详情失败')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    const loadKline = async () => {
      try {
        const res = (await getSectorKline(code)) as unknown as { klines?: SectorKlineItem[] }
        if (cancelled) return
        // 后端倒序返回，反转为时间正序
        setKlineData([...(res.klines ?? [])].reverse())
      } catch (err) {
        console.error('加载板块K线数据失败:', err)
      }
    }

    loadDetail()
    loadKline()

    return () => {
      cancelled = true
    }
  }, [code, navigate])

  // K线图数据（LocalKLineChart）
  const chartData: KLineDataItem[] = useMemo(
    () =>
      klineData.map((item) => ({
        date: item.trade_date,
        open: item.open ?? 0,
        high: item.high ?? 0,
        low: item.low ?? 0,
        close: item.close ?? 0,
        volume: item.total_volume ?? 0,
        amount: item.total_amount ?? 0,
      })),
    [klineData],
  )

  // 成交量/成交额图（ECharts，配色随暗色主题由 EChart 内部重建）
  const volumeOption: EChartsCoreOption = useMemo(() => {
    const dates = klineData.map((item) => item.trade_date)
    const interval = Math.max(0, Math.floor(klineData.length / 8))
    return {
      title: { text: '成交量/成交额', left: 'center' },
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      legend: { data: ['成交量', '成交额'], top: 30 },
      grid: { left: '8%', right: '5%', bottom: '15%', top: '15%', containLabel: true },
      xAxis: {
        type: 'category',
        data: dates,
        axisLabel: {
          rotate: 45,
          interval,
          formatter: (value: string) => value.substring(5),
        },
        boundaryGap: false,
      },
      yAxis: [
        { type: 'value', name: '成交量', position: 'left', splitNumber: 4 },
        { type: 'value', name: '成交额', position: 'right', splitNumber: 4 },
      ],
      series: [
        {
          name: '成交量',
          type: 'bar',
          data: klineData.map((item) => item.total_volume ?? 0),
          // A股语义：红涨绿跌，颜色跟随当日K线
          itemStyle: {
            color: (params: { dataIndex: number }) => {
              const item = klineData[params.dataIndex]
              if (item && item.open != null && item.close != null) {
                return item.close >= item.open ? '#f5222d' : '#52c41a'
              }
              return '#1890ff'
            },
          },
          barWidth: '60%',
          barMaxWidth: 10,
        },
        {
          name: '成交额',
          type: 'line',
          yAxisIndex: 1,
          data: klineData.map((item) => item.total_amount ?? 0),
          lineStyle: { color: '#1890ff' },
          itemStyle: { color: '#1890ff' },
          symbol: 'circle',
          symbolSize: 4,
        },
      ],
    }
  }, [klineData])

  return (
    <div className="mx-auto max-w-6xl px-4 py-6">
      {/* 页头 */}
      <PageHeader
        title={sectorInfo ? `${sectorInfo.name} (${sectorInfo.code})` : `板块 ${code || ''}`}
        subtitle={loading ? '加载中…' : sectorInfo?.type ? getTypeLabel(sectorInfo.type) : '板块详情'}
        actions={
          <Button outline onClick={() => navigate('/sectors')}>
            ← 返回
          </Button>
        }
      />

      {/* 板块基本信息 */}
      <Card className="mb-5">
        {loading ? (
          <PageLoading />
        ) : sectorInfo ? (
          <div>
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex flex-wrap items-center gap-3">
                {sectorInfo.type && (
                  <Badge color={sectorInfo.type === 'theme' ? 'green' : 'sky'}>
                    {getTypeLabel(sectorInfo.type)}
                  </Badge>
                )}
                <Badge color="zinc">{sectorInfo.stock_count ?? 0} 只成分股</Badge>
              </div>
              <ChangeValue value={sectorInfo.avg_change_pct} className="text-xl" />
            </div>
            <div className="mt-5 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <StatCard label="交易日期" value={sectorInfo.trade_date || 'N/A'} />
              <StatCard label="总成交量" value={formatVolume(sectorInfo.total_volume)} />
              <StatCard label="总成交额" value={formatAmount(sectorInfo.total_amount)} />
              <StatCard
                label="上涨/下跌/平盘"
                value={
                  <>
                    <span className="text-red-600 dark:text-red-400">{sectorInfo.rise_count ?? 0}</span>
                    {' / '}
                    <span className="text-green-600 dark:text-green-500">{sectorInfo.fall_count ?? 0}</span>
                    {' / '}
                    <span className="text-zinc-600 dark:text-zinc-400">{sectorInfo.flat_count ?? 0}</span>
                  </>
                }
              />
            </div>
          </div>
        ) : (
          <EmptyState text="暂无数据" />
        )}
      </Card>

      {/* 板块K线图和成交量 */}
      <Card className="mb-5">
        <SectionTitle title="板块K线图" />
        {chartData.length > 0 ? (
          <div className="mb-8">
            <LocalKLineChart data={chartData} title="板块K线图" height={500} />
          </div>
        ) : (
          <EmptyState text="暂无K线数据" />
        )}
        <SectionTitle title="成交量/成交额" />
        {klineData.length > 0 ? (
          <EChart option={volumeOption} height={320} />
        ) : (
          <EmptyState text="暂无K线数据" />
        )}
      </Card>

      {/* 板块历史数据 */}
      <Card className="mb-5">
        <SectionTitle title="历史涨跌统计 (过去15天)" />
        {historyLoading ? (
          <PageLoading />
        ) : sectorHistory.length > 0 ? (
          <div className="overflow-x-auto">
            <Table dense>
              <TableHead>
                <TableRow>
                  <TableHeader>日期</TableHeader>
                  <TableHeader>上涨家数</TableHeader>
                  <TableHeader>下跌家数</TableHeader>
                  <TableHeader>平盘家数</TableHeader>
                  <TableHeader>上涨比例</TableHeader>
                  <TableHeader>涨跌幅</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {sectorHistory.map((item) => {
                  const ratio = riseRatioValue(item.rise_count, item.fall_count, item.flat_count)
                  return (
                    <TableRow key={item.trade_date}>
                      <TableCell className="tabular-nums">{item.trade_date}</TableCell>
                      <TableCell className="tabular-nums text-red-600 dark:text-red-400">
                        {item.rise_count}
                      </TableCell>
                      <TableCell className="tabular-nums text-green-600 dark:text-green-500">
                        {item.fall_count}
                      </TableCell>
                      <TableCell className="tabular-nums text-zinc-600 dark:text-zinc-400">
                        {item.flat_count}
                      </TableCell>
                      <TableCell className={`tabular-nums ${ratio > 50 ? 'font-bold' : ''}`}>
                        {ratio.toFixed(2)}%
                      </TableCell>
                      <TableCell className={`tabular-nums ${changeClass(item.change_pct)}`}>
                        {formatPct(item.change_pct)}
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          </div>
        ) : (
          <EmptyState text="暂无历史数据" />
        )}
      </Card>

      {/* 成分股列表 */}
      <Card>
        <SectionTitle title="成分股列表" />
        <div className="overflow-x-auto">
          <Table dense>
            <TableHead>
              <TableRow>
                <TableHeader>代码</TableHeader>
                <TableHeader>名称</TableHeader>
                <TableHeader>市场</TableHeader>
                <TableHeader>涨跌幅</TableHeader>
                <TableHeader>成交量</TableHeader>
                <TableHeader>成交额</TableHeader>
                <TableHeader>操作</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {sectorStocks.map((stock) => (
                <TableRow key={stock.code}>
                  <TableCell className="tabular-nums">{stock.code}</TableCell>
                  <TableCell>{stock.name}</TableCell>
                  <TableCell className="text-zinc-500 dark:text-zinc-400">{stock.market || '-'}</TableCell>
                  <TableCell className={`tabular-nums ${changeClass(stock.change_pct)}`}>
                    {formatPct(stock.change_pct)}
                  </TableCell>
                  <TableCell className="tabular-nums">{formatVolume(stock.volume)}</TableCell>
                  <TableCell className="tabular-nums">{formatAmount(stock.amount)}</TableCell>
                  <TableCell>
                    <Button outline onClick={() => navigate(`/stock/${stock.code}`)}>
                      查看
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
        {!loading && sectorStocks.length === 0 && <EmptyState text="暂无成分股数据" />}
      </Card>
    </div>
  )
}
