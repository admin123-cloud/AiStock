import { useCallback, useEffect, useMemo, useState } from 'react'
import type { EChartsCoreOption } from 'echarts'
import { getGen2Timing } from '@/api/trading'
import { Alert } from '@/components/catalyst/alert'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Input } from '@/components/catalyst/input'
import { Field, Label } from '@/components/catalyst/fieldset'
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
import { Card, ChangeValue, EmptyState, PageHeader, SectionTitle, StatCard } from '@/components/ui'

const DOWN = 'text-green-600 dark:text-green-500 font-bold tabular-nums'

const FALLBACK_INDEX_OPTIONS = [
  { code: '999999.SH', name: '上证指数' },
  { code: '399006.SZ', name: '创业板指' },
  { code: '399001.SZ', name: '深证成指' },
]

const STATE_PILL_STYLES: Record<string, string> = {
  NORMAL: 'border-red-200 bg-red-50 dark:border-red-400/30 dark:bg-red-400/10',
  OFF: 'border-teal-200 bg-teal-50 dark:border-teal-400/30 dark:bg-teal-400/10',
  PROBE: 'border-blue-200 bg-blue-50 dark:border-blue-400/30 dark:bg-blue-400/10',
  AGGRESSIVE: 'border-orange-200 bg-orange-50 dark:border-orange-400/30 dark:bg-orange-400/10',
}

function pct(value: unknown, digits = 2): string {
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(digits)}%` : '--'
}

function num(value: unknown, digits = 2): string {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(digits) : '--'
}

function stateColor(state: string | undefined, alpha = 0.18): string {
  const colors: Record<string, string> = {
    OFF: `rgba(20, 184, 166, ${alpha})`,
    PROBE: `rgba(59, 130, 246, ${alpha})`,
    NORMAL: `rgba(220, 38, 38, ${alpha})`,
    AGGRESSIVE: `rgba(245, 158, 11, ${alpha})`,
  }
  return colors[state || ''] || `rgba(148, 163, 184, ${alpha})`
}

export default function Gen2Timing() {
  const [loading, setLoading] = useState(false)
  const [available, setAvailable] = useState(false)
  const [message, setMessage] = useState('')
  const [signalDate, setSignalDate] = useState('')
  const [curveDays, setCurveDays] = useState(420)
  const [indexCode, setIndexCode] = useState('999999.SH')
  const [payload, setPayload] = useState<any>({})

  const current = payload?.current || {}
  const chartPayload = payload?.chart || { daily: [], segments: [], state_counts: {}, signal_markers: [] }
  const chartDaily: any[] = chartPayload?.daily || []
  const stateBacktestRows: any[] = payload?.state_backtest_rows || []
  const recentSignals: any[] = payload?.open_signals?.recent_rows || []
  const indexOptions = useMemo(() => {
    const rows = payload?.available_indices
    return Array.isArray(rows) && rows.length ? rows : FALLBACK_INDEX_OPTIONS
  }, [payload])
  const benchmarkTitle = chartPayload?.index?.name || chartPayload?.index?.code || '上证指数'
  const chartRange = chartDaily.length
    ? `${chartDaily[0].date} ~ ${chartDaily[chartDaily.length - 1].date}`
    : '--'
  const selectedDate = payload?.selected_date || '--'

  const stateCards = useMemo(() => {
    const defs = payload?.state_definitions || {}
    return ['OFF', 'PROBE', 'NORMAL', 'AGGRESSIVE'].map((state) => ({ state, ...(defs[state] || {}) }))
  }, [payload])

  const stateSummaryCards = useMemo(() => {
    const counts = chartPayload?.state_counts || {}
    return ['OFF', 'PROBE', 'NORMAL', 'AGGRESSIVE'].map((state) => ({
      state,
      days: Number(counts[state] || 0),
    }))
  }, [chartPayload])

  const chartOption = useMemo<EChartsCoreOption | null>(() => {
    if (!chartDaily.length) return null
    const dates = chartDaily.map((item) => item.date)
    const candles = chartDaily.map((item) => [item.open, item.close, item.low, item.high])
    const segments = chartPayload?.segments || []
    const markers = chartPayload?.signal_markers || []
    const defaultVisibleDays = dates.length > 240 ? 180 : Math.max(dates.length, 1)
    const zoomStart = dates.length > defaultVisibleDays ? Math.max(0, ((dates.length - defaultVisibleDays) / dates.length) * 100) : 0
    const dailyByDate = new Map(chartDaily.map((item) => [item.date, item]))
    const markAreas = segments.map((seg: any) => [
      {
        name: seg.state,
        xAxis: seg.start_date,
        itemStyle: { color: stateColor(seg.state, 0.16) },
      },
      { xAxis: seg.end_date },
    ])
    const band = chartDaily.map((item) => ({
      value: item.g2_open_state ? 1 : 0,
      itemStyle: { color: stateColor(item.g2_open_state, 0.42) },
    }))
    const signalPoints = markers
      .filter((item: any) => dailyByDate.has(item.date))
      .map((item: any) => {
        const day = dailyByDate.get(item.date) || {}
        return {
          value: [item.date, Number(day.low || day.close || item.price || 0) * 0.985, item.name || '', item.reason || ''],
          label: { show: false },
        }
      })

    return {
      animation: false,
      legend: { data: [benchmarkTitle, 'G2状态', '开仓信号'], top: 0 },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        formatter(params: any) {
          if (!params?.length) return ''
          const idx = params[0]?.dataIndex ?? 0
          const item = chartDaily[idx] || {}
          const signalText = markers
            .filter((marker: any) => marker.date === item.date)
            .map((marker: any) => `${marker.name || '--'}：${marker.reason || '--'}`)
            .join('<br/>')
          return [
            item.date || '--',
            `${benchmarkTitle} O:${num(item.open)} H:${num(item.high)} L:${num(item.low)} C:${num(item.close)}`,
            `G2状态: ${item.g2_open_state || '--'}`,
            `目标仓位: ${pct(item.target_exposure_min, 0)} ~ ${pct(item.target_exposure_max, 0)}`,
            `G2代理(中证1000) MA20:${num(item.csi1000_ma20)} MA60:${num(item.csi1000_ma60)} MA120:${num(item.csi1000_ma120)}`,
            `广度: ${pct(item.breadth_ma20)} / MA20斜率5日: ${num(item.csi1000_ma20_slope5, 4)}`,
            `风险确认: ${item.risk_index_ok ? '通过' : '未通过'} / 因子风险偏好: ${item.factor_risk_on ? '通过' : '未通过'}`,
            `开仓信号: ${signalText || '--'}`,
          ].join('<br/>')
        },
      },
      grid: [
        { left: 52, right: 18, top: 44, height: '64%' },
        { left: 52, right: 18, top: '80%', height: '8%' },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: zoomStart, end: 100 },
        { type: 'slider', xAxisIndex: [0, 1], start: zoomStart, end: 100, height: 24, bottom: 10, brushSelect: false },
      ],
      xAxis: [
        { type: 'category', data: dates, boundaryGap: false, axisLine: { onZero: false }, splitLine: { show: false } },
        { type: 'category', gridIndex: 1, data: dates, boundaryGap: false, axisLabel: { show: false }, axisTick: { show: false }, axisLine: { show: false } },
      ],
      yAxis: [
        { scale: true },
        { gridIndex: 1, min: 0, max: 1, axisLabel: { show: false }, axisTick: { show: false }, splitLine: { show: false } },
      ],
      series: [
        {
          name: benchmarkTitle,
          type: 'candlestick',
          data: candles,
          itemStyle: { color: '#dc2626', color0: '#16a34a', borderColor: '#dc2626', borderColor0: '#16a34a' },
          markArea: { silent: true, data: markAreas, label: { show: false } },
        },
        {
          name: '开仓信号',
          type: 'scatter',
          data: signalPoints,
          symbol: 'triangle',
          symbolSize: 14,
          itemStyle: { color: '#7c3aed', borderColor: '#fff', borderWidth: 1 },
          z: 12,
        },
        { name: 'G2状态', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: band, barWidth: '100%', silent: true },
      ],
    } as EChartsCoreOption
  }, [benchmarkTitle, chartDaily, chartPayload])

  const fetchData = useCallback(async () => {
    setLoading(true)
    try {
      const data: any = (await getGen2Timing({
        signal_date: signalDate || undefined,
        curve_days: curveDays,
        index_code: indexCode,
      })) || {}
      setPayload(data)
      setIndexCode(data?.selected_index_code || data?.chart?.index?.code || indexCode)
      setAvailable(!!data?.available)
      setMessage(data?.message || '')
    } catch (error) {
      setAvailable(false)
      setMessage((error as Error)?.message || '读取第二代交易择时失败')
    } finally {
      setLoading(false)
    }
  }, [curveDays, indexCode, signalDate])

  const resetDateAndReload = useCallback(async () => {
    setSignalDate('')
    // fetchData 依赖 signalDate，这里直接传空日期重新请求
    setLoading(true)
    try {
      const data: any = (await getGen2Timing({
        signal_date: undefined,
        curve_days: curveDays,
        index_code: indexCode,
      })) || {}
      setPayload(data)
      setIndexCode(data?.selected_index_code || data?.chart?.index?.code || indexCode)
      setAvailable(!!data?.available)
      setMessage(data?.message || '')
    } catch (error) {
      setAvailable(false)
      setMessage((error as Error)?.message || '读取第二代交易择时失败')
    } finally {
      setLoading(false)
    }
  }, [curveDays, indexCode])

  useEffect(() => {
    fetchData()
  }, [fetchData])

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="第二代交易系统：交易择时"
        subtitle="默认用上证指数 K 线解释 G2 四状态、当日是否开仓，以及状态与开仓信号的时间关系，可切换创业板指或深证成指。"
        actions={
          <>
            <Field>
              <Label htmlFor="timing-date">交易日</Label>
              <Input
                id="timing-date"
                type="date"
                value={signalDate}
                onChange={(e) => setSignalDate(e.target.value)}
                className="w-44"
              />
            </Field>
            <Field>
              <Label htmlFor="timing-days">K线天数</Label>
              <Input
                id="timing-days"
                type="number"
                min={80}
                max={1500}
                step={20}
                value={curveDays}
                onChange={(e) => {
                  const v = Number(e.target.value)
                  if (Number.isFinite(v)) setCurveDays(Math.min(1500, Math.max(80, v)))
                }}
                className="w-28"
              />
            </Field>
            <Button disabled={loading} onClick={fetchData}>{loading ? '查询中…' : '查询'}</Button>
            <Button outline disabled={loading} onClick={resetDateAndReload}>最新</Button>
          </>
        }
      />

      {!available && !loading && <Alert color="amber">{message || '第二代交易择时数据不可用'}</Alert>}

      {available && (
        <>
          <section
            className={`flex flex-col items-start justify-between gap-4 rounded-lg border p-5 lg:flex-row lg:items-center ${
              current.is_open_day
                ? 'border-red-200 bg-red-50 dark:border-red-400/30 dark:bg-red-400/10'
                : 'border-zinc-200 bg-zinc-50 dark:border-white/10 dark:bg-white/5'
            }`}
          >
            <div>
              <span className="mb-1 block text-sm text-zinc-500 dark:text-zinc-400">{selectedDate}</span>
              <h2 className="text-2xl font-semibold text-zinc-900 dark:text-white">{current.decision_title || '--'}</h2>
              <p className="mt-2 text-zinc-600 dark:text-zinc-400">{current.decision_message || '--'}</p>
            </div>
            <div className={`min-w-[150px] rounded-lg border p-4 text-center ${STATE_PILL_STYLES[current.state] || 'border-zinc-200 bg-white dark:border-white/10 dark:bg-white/5'}`}>
              <strong className="block text-2xl text-zinc-900 dark:text-white">{current.state || '--'}</strong>
              <span className="mt-1 block text-sm text-zinc-500 dark:text-zinc-400">{current.state_title || '--'}</span>
            </div>
          </section>

          <section className="grid grid-cols-[repeat(auto-fit,minmax(190px,1fr))] gap-3">
            {[
              { label: '当前状态', value: current.state || '--', note: current.state_meaning || '--' },
              { label: '建议总仓位', value: current.target_exposure || '--', note: current.can_observe_open ? '允许观察开仓信号' : '不新增仓位' },
              { label: '当日开仓信号', value: current.exact_signal_count || 0, note: 'G2 Open V1 精确匹配数量' },
              { label: '图表窗口', value: chartDaily.length, note: chartRange },
            ].map((metric) => (
              <StatCard key={metric.label} label={metric.label} value={metric.value} hint={metric.note} />
            ))}
          </section>

          <Card className="p-5">
            <SectionTitle
              title="1) 四个状态的含义"
              actions={<Badge color="zinc">OFF / PROBE / NORMAL / AGGRESSIVE</Badge>}
            />
            <div className="grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-3">
              {stateCards.map((item: any) => (
                <div
                  key={item.state}
                  className={`rounded-lg border p-4 dark:bg-white/5 ${STATE_PILL_STYLES[item.state] || 'border-zinc-200 bg-zinc-50 dark:border-white/10'}`}
                >
                  <div className="mb-2 flex items-baseline justify-between gap-2">
                    <strong className="text-lg text-zinc-900 dark:text-white">{item.state}</strong>
                    <span className="text-sm font-bold text-zinc-600 dark:text-zinc-400">{item.title}</span>
                  </div>
                  <p className="min-h-[66px] leading-relaxed text-zinc-600 dark:text-zinc-400">{item.meaning}</p>
                  <div className="mt-2 flex flex-col gap-1 border-t border-zinc-200 pt-2 text-xs text-zinc-500 dark:border-white/10 dark:text-zinc-400">
                    <span>仓位：{item.target_exposure}</span>
                    <em className="not-italic text-zinc-700 dark:text-zinc-300">{item.trade_rule}</em>
                  </div>
                </div>
              ))}
            </div>
          </Card>

          <Card className="p-5">
            <SectionTitle
              title="2) 当日开仓判断"
              actions={
                <Badge color={current.is_open_day ? 'emerald' : 'red'}>
                  {current.is_open_day ? '开仓日' : '非开仓日'}
                </Badge>
              }
            />
            <div className="overflow-auto rounded-lg">
              <Table dense striped className="min-w-full">
                <TableHead>
                  <TableRow>
                    <TableHeader>检查项</TableHeader>
                    <TableHeader>当前值</TableHeader>
                    <TableHeader>说明</TableHeader>
                    <TableHeader>结果</TableHeader>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {(current.condition_rows || []).map((row: any, idx: number) => (
                    <TableRow key={`${row.label}-${idx}`}>
                      <TableCell>{row.label}</TableCell>
                      <TableCell className="tabular-nums">{row.value}</TableCell>
                      <TableCell>{row.reason}</TableCell>
                      <TableCell>
                        <Badge color={row.pass ? 'emerald' : 'amber'}>{row.pass ? '通过' : '未通过'}</Badge>
                      </TableCell>
                    </TableRow>
                  ))}
                  {!(current.condition_rows || []).length && (
                    <TableRow>
                      <TableCell colSpan={4} className="text-center text-zinc-400">暂无判断条件</TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </div>
            {(current.not_open_reasons || []).length > 0 && (
              <div className="mt-3 flex flex-col gap-1.5 text-sm text-amber-700 dark:text-amber-400">
                {(current.not_open_reasons || []).map((reason: string) => (
                  <span key={reason}>{reason}</span>
                ))}
              </div>
            )}
          </Card>

          <Card className="p-5">
            <SectionTitle
              title="3) 状态与指数 K 线"
              actions={
                <div className="flex flex-wrap items-center gap-2">
                  <Select
                    value={indexCode}
                    onChange={(e) => setIndexCode(e.target.value)}
                    className="w-40"
                    aria-label="指数"
                  >
                    {indexOptions.map((item: any) => (
                      <option key={item.code} value={item.code}>{item.name}</option>
                    ))}
                  </Select>
                  <Badge color="zinc">{benchmarkTitle}</Badge>
                </div>
              }
            />
            <div className="mb-3 grid grid-cols-[repeat(auto-fit,minmax(120px,1fr))] gap-2.5">
              {stateSummaryCards.map((item) => (
                <div
                  key={item.state}
                  className={`flex items-center justify-between rounded-lg border px-3 py-2.5 dark:bg-white/5 ${STATE_PILL_STYLES[item.state] || 'border-zinc-200 dark:border-white/10'}`}
                >
                  <strong className="text-zinc-900 dark:text-white">{item.state}</strong>
                  <span className="text-zinc-500 dark:text-zinc-400 tabular-nums">{item.days}天</span>
                </div>
              ))}
            </div>
            {chartOption ? (
              <EChart option={chartOption} height={560} />
            ) : (
              <EmptyState text="暂无K线数据" className="h-[560px]" />
            )}
          </Card>

          <Card className="grid gap-4 p-5 lg:grid-cols-[minmax(0,1.2fr)_minmax(360px,0.8fr)]">
            <div>
              <SectionTitle
                title="四状态回测对照"
                actions={<span className="text-sm text-zinc-500 dark:text-zinc-400">同一买点，仅切换 g2_open_state</span>}
              />
              <div className="overflow-auto rounded-lg">
                <Table dense striped className="min-w-full">
                  <TableHead>
                    <TableRow>
                      <TableHeader>状态</TableHeader>
                      <TableHeader>信号</TableHeader>
                      <TableHeader>交易</TableHeader>
                      <TableHeader>总收益</TableHeader>
                      <TableHeader>超额</TableHeader>
                      <TableHeader>回撤</TableHeader>
                      <TableHeader>胜率</TableHeader>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {stateBacktestRows.map((row, idx) => (
                      <TableRow key={`${row.state}-${idx}`}>
                        <TableCell>{row.state}</TableCell>
                        <TableCell className="tabular-nums">{row.signal_count}</TableCell>
                        <TableCell className="tabular-nums">{row.trade_count}</TableCell>
                        <TableCell><ChangeValue value={Number(row.total_return) * 100} /></TableCell>
                        <TableCell><ChangeValue value={Number(row.excess_return) * 100} /></TableCell>
                        <TableCell><span className={DOWN}>{pct(row.max_drawdown)}</span></TableCell>
                        <TableCell className="tabular-nums">{pct(row.win_rate)}</TableCell>
                      </TableRow>
                    ))}
                    {!stateBacktestRows.length && (
                      <TableRow>
                        <TableCell colSpan={7} className="text-center text-zinc-400">暂无回测对照</TableCell>
                      </TableRow>
                    )}
                  </TableBody>
                </Table>
              </div>
            </div>
            <div>
              <SectionTitle
                title="最近开仓信号"
                actions={<span className="text-sm text-zinc-500 dark:text-zinc-400">{recentSignals.length} 条</span>}
              />
              <div className="max-h-[300px] overflow-auto rounded-lg">
                <Table dense striped className="min-w-full">
                  <TableHead>
                    <TableRow>
                      <TableHeader>日期</TableHeader>
                      <TableHeader>代码</TableHeader>
                      <TableHeader>名称</TableHeader>
                      <TableHeader>3日收益</TableHeader>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {recentSignals.map((row, idx) => (
                      <TableRow key={`${row.code}-${row.signal_date}-${idx}`}>
                        <TableCell className="tabular-nums">{row.signal_date}</TableCell>
                        <TableCell className="font-mono">{row.code}</TableCell>
                        <TableCell>{row.name}</TableCell>
                        <TableCell><ChangeValue value={Number(row.historical_ret_3d) * 100} /></TableCell>
                      </TableRow>
                    ))}
                    {!recentSignals.length && (
                      <TableRow>
                        <TableCell colSpan={4} className="text-center text-zinc-400">暂无信号</TableCell>
                      </TableRow>
                    )}
                  </TableBody>
                </Table>
              </div>
            </div>
          </Card>
        </>
      )}
    </div>
  )
}
