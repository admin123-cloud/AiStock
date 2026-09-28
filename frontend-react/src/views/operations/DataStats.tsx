import { useCallback, useEffect, useMemo, useState } from 'react'
import type { EChartsCoreOption } from 'echarts'
import { Button } from '@/components/catalyst/button'
import { Select } from '@/components/catalyst/select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/catalyst/table'
import EChart from '@/components/EChart'
import { Card, EmptyState, PageHeader, PageLoading, SectionTitle, StatCard } from '@/components/ui'
import request from '@/utils/request'

// 响应拦截器已解包 data，但 axios 类型仍是 AxiosResponse，这里统一断言
async function getJson<T>(url: string, config?: object): Promise<T> {
  return (await request.get(url, config)) as unknown as T
}

interface OverviewData {
  total_stocks: number
  market_stats: Record<string, number>
}

interface KlineMissingData {
  total_stocks: number
  stocks_with_data: number
  stocks_without_data: number
  overall_missing_rate: number
}

interface FieldStat {
  count: number
  rate: number
}

interface FieldMissingData {
  missing_industry: FieldStat
  missing_sector: FieldStat
  missing_list_date: FieldStat
}

type RateTone = 'good' | 'warning' | 'bad'

const rateTone = (rate: number): RateTone => (rate > 50 ? 'bad' : rate > 20 ? 'warning' : 'good')

const toneClasses: Record<RateTone, string> = {
  good: 'text-emerald-600 dark:text-emerald-400',
  warning: 'text-orange-600 dark:text-orange-400',
  bad: 'text-red-600 dark:text-red-400',
}

const rateStatTone = (rate: number): 'warning' | 'default' => (rate > 20 ? 'warning' : 'default')

export default function DataStats() {
  const [loading, setLoading] = useState(true)
  const [klineLoading, setKlineLoading] = useState(true)
  const [fieldLoading, setFieldLoading] = useState(true)
  const [completenessScore, setCompletenessScore] = useState<string>('--')
  const [overviewData, setOverviewData] = useState<OverviewData | null>(null)
  const [klineData, setKlineData] = useState<KlineMissingData | null>(null)
  const [fieldData, setFieldData] = useState<FieldMissingData | null>(null)
  const [selectedDays, setSelectedDays] = useState(30)

  const loadCompletenessScore = useCallback(async () => {
    try {
      const data = await getJson<{ overall_score: string }>('/data-statistics/completeness', {
        skipErrorHandler: true,
      })
      setCompletenessScore(String(data.overall_score))
    } catch (error) {
      console.error('加载评分失败:', error)
    }
  }, [])

  const loadDataOverview = useCallback(async () => {
    setLoading(true)
    try {
      const data = await getJson<OverviewData>('/data-statistics/overview', {
        skipErrorHandler: true,
      })
      setOverviewData(data)
    } catch (error) {
      console.error('加载总览失败:', error)
    } finally {
      setLoading(false)
    }
  }, [])

  const refreshKlineStats = useCallback(async (days: number) => {
    setKlineLoading(true)
    try {
      const data = await getJson<KlineMissingData>('/data-statistics/kline-missing', {
        params: { days },
        skipErrorHandler: true,
      })
      setKlineData(data)
    } catch (error) {
      console.error('加载K线统计失败:', error)
    } finally {
      setKlineLoading(false)
    }
  }, [])

  const loadFieldStats = useCallback(async () => {
    setFieldLoading(true)
    try {
      const data = await getJson<FieldMissingData>('/data-statistics/field-missing', {
        skipErrorHandler: true,
      })
      setFieldData(data)
    } catch (error) {
      console.error('加载字段统计失败:', error)
    } finally {
      setFieldLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadCompletenessScore()
    void loadDataOverview()
    void refreshKlineStats(30)
    void loadFieldStats()
  }, [loadCompletenessScore, loadDataOverview, refreshKlineStats, loadFieldStats])

  const onDaysChange = (value: number) => {
    setSelectedDays(value)
    void refreshKlineStats(value)
  }

  const chartOption = useMemo<EChartsCoreOption>(() => {
    const marketStats = overviewData?.market_stats ?? {}
    const palette: Record<string, string> = {
      SH: '#6366f1',
      SZ: '#8b5cf6',
      BJ: '#f56565',
    }
    return {
      tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
      legend: { orient: 'vertical', left: 'left' },
      series: [
        {
          name: '市场分布',
          type: 'pie',
          radius: ['40%', '70%'],
          avoidLabelOverlap: false,
          itemStyle: { borderRadius: 10 },
          label: { show: false, position: 'center' },
          emphasis: { label: { show: true, fontSize: 20, fontWeight: 'bold' } },
          labelLine: { show: false },
          data: Object.entries(marketStats).map(([name, value]) => ({
            name,
            value,
            itemStyle: { color: palette[name] || '#a0aec0' },
          })),
        },
      ],
    }
  }, [overviewData])

  return (
    <div className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
      <PageHeader
        title="数据缺失统计"
        subtitle="分析股票数据库的数据完整性，识别缺失数据和数据质量问题"
      />

      <StatCard
        label="数据完整性评分"
        value={completenessScore}
        tone="info"
        className="mb-6 text-center"
      />

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <Card>
          <SectionTitle title="数据总览" />
          {loading ? (
            <PageLoading />
          ) : overviewData ? (
            <div>
              <div className="grid grid-cols-2 gap-4">
                <StatCard label="股票总数" value={overviewData.total_stocks} />
                <StatCard label="市场分布" value={Object.keys(overviewData.market_stats).length} />
              </div>
              <h3 className="mt-5 mb-2 text-base font-medium text-zinc-700 dark:text-zinc-300">
                市场分布
              </h3>
              <Table dense>
                <TableHead>
                  <TableRow>
                    <TableHeader>市场</TableHeader>
                    <TableHeader>数量</TableHeader>
                    <TableHeader>占比</TableHeader>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {Object.entries(overviewData.market_stats).map(([market, count]) => (
                    <TableRow key={market}>
                      <TableCell>{market}</TableCell>
                      <TableCell className="tabular-nums">{count}</TableCell>
                      <TableCell className="tabular-nums">
                        {((count / overviewData.total_stocks) * 100).toFixed(1)}%
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          ) : (
            <EmptyState />
          )}
        </Card>

        <Card>
          <SectionTitle
            title="K线数据缺失"
            actions={
              <>
                <Button onClick={() => void refreshKlineStats(selectedDays)}>刷新</Button>
                <div className="w-36">
                  <Select
                    aria-label="统计范围"
                    value={selectedDays}
                    onChange={(e) => onDaysChange(Number(e.target.value))}
                  >
                    <option value={30}>最近30天</option>
                    <option value={60}>最近60天</option>
                    <option value={90}>最近90天</option>
                  </Select>
                </div>
              </>
            }
          />
          {klineLoading ? (
            <PageLoading />
          ) : klineData ? (
            <div>
              <div className="grid grid-cols-2 gap-4">
                <StatCard label="股票总数" value={klineData.total_stocks} />
                <StatCard
                  label="有数据"
                  value={klineData.stocks_with_data}
                  hint={
                    <span className={toneClasses.good}>
                      {((klineData.stocks_with_data / klineData.total_stocks) * 100).toFixed(1)}%
                    </span>
                  }
                />
                <StatCard
                  label="无数据"
                  value={klineData.stocks_without_data}
                  tone="warning"
                  hint={
                    <span className={toneClasses.bad}>
                      {((klineData.stocks_without_data / klineData.total_stocks) * 100).toFixed(1)}%
                    </span>
                  }
                />
                <StatCard
                  label="总体缺失率"
                  value={`${klineData.overall_missing_rate}%`}
                  tone="warning"
                  hint={
                    <span className={toneClasses[rateTone(klineData.overall_missing_rate)]}>
                      {klineData.overall_missing_rate}%
                    </span>
                  }
                />
              </div>
              <h3 className="mt-5 mb-2 text-base font-medium text-zinc-700 dark:text-zinc-300">
                缺失情况
              </h3>
              <div className="h-2 w-full overflow-hidden rounded bg-zinc-200 dark:bg-zinc-700">
                <div
                  className="h-full bg-gradient-to-r from-indigo-500 to-purple-700 transition-[width] duration-500"
                  style={{ width: `${100 - klineData.overall_missing_rate}%` }}
                />
              </div>
            </div>
          ) : (
            <EmptyState />
          )}
        </Card>

        <Card>
          <SectionTitle title="字段缺失统计" />
          {fieldLoading ? (
            <PageLoading />
          ) : fieldData ? (
            <div className="grid grid-cols-2 gap-4">
              <StatCard
                label="行业字段缺失"
                value={fieldData.missing_industry.count}
                tone={rateStatTone(fieldData.missing_industry.rate)}
                hint={
                  <span className={toneClasses[rateTone(fieldData.missing_industry.rate)]}>
                    {fieldData.missing_industry.rate}%
                  </span>
                }
              />
              <StatCard
                label="板块字段缺失"
                value={fieldData.missing_sector.count}
                tone={rateStatTone(fieldData.missing_sector.rate)}
                hint={
                  <span className={toneClasses[rateTone(fieldData.missing_sector.rate)]}>
                    {fieldData.missing_sector.rate}%
                  </span>
                }
              />
              <StatCard
                label="上市日期缺失"
                value={fieldData.missing_list_date.count}
                tone={rateStatTone(fieldData.missing_list_date.rate)}
                hint={
                  <span className={toneClasses[rateTone(fieldData.missing_list_date.rate)]}>
                    {fieldData.missing_list_date.rate}%
                  </span>
                }
              />
              <StatCard
                label="总体完整性"
                tone="info"
                value={`${(
                  100 -
                  (fieldData.missing_industry.rate +
                    fieldData.missing_sector.rate +
                    fieldData.missing_list_date.rate) /
                    3
                ).toFixed(1)}%`}
              />
            </div>
          ) : (
            <EmptyState />
          )}
        </Card>

        <Card>
          <SectionTitle title="市场分布" />
          <EChart option={chartOption} height={300} loading={loading} />
        </Card>
      </div>
    </div>
  )
}
