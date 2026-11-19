import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { EChartsCoreOption } from 'echarts'
import {
  getGen2Backtest,
  getGen2BacktestUpdateTask,
  runGen2BacktestLatest,
  saveGen2TradeClassification,
} from '@/api/trading'
import { Alert } from '@/components/catalyst/alert'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Dialog, DialogActions, DialogBody, DialogTitle } from '@/components/catalyst/dialog'
import { Field, Label } from '@/components/catalyst/fieldset'
import { Select } from '@/components/catalyst/select'
import { Textarea } from '@/components/catalyst/textarea'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/catalyst/table'
import { useToast } from '@/components/toast'
import EChart from '@/components/EChart'
import { Card, ChangeValue, PageHeader, SectionTitle, StatCard } from '@/components/ui'

interface TradeRow {
  code?: string
  name?: string
  buy_date?: string
  sell_date?: string
  buy_datetime?: string
  sell_datetime?: string
  buy_strategy_label?: string
  buy_strategy_type?: string
  buy_sector_name?: string
  classification?: string
  classification_label?: string
  classification_type?: string
  classification_note?: string
  trade_key?: string
  return?: number
  pnl?: number
  sell_ratio?: number
  exit_reason?: string
  v4_rank?: number | string
  buy_price?: number
  sell_price?: number
}

interface SignalRow {
  entry_date?: string
  code?: string
  name?: string
  g2_open_state?: string
  pattern?: string
  trigger_type?: string
  confirm_datetime?: string
  v4_rank?: number | string
  entry_price?: number
  entry_fwd_ret_3d?: number
}

interface BacktestPayload {
  strategy_display_name?: string
  strategy_name?: string
  status?: string
  available?: boolean
  message?: string
  metrics?: Record<string, any>
  summary?: Record<string, any>
  rule_lines?: string[]
  equity_curve?: Array<{ date: string; strategy_equity?: number; benchmark_equity?: number; drawdown?: number }>
  trades?: TradeRow[]
  signals?: SignalRow[]
  trade_classifications?: Array<{ value: string; label: string; type?: string }>
}

interface ComparisonRow {
  strategy_code: string
  name: string
  positioning?: string
  available?: boolean
  detail_available?: boolean
  total_return?: number
  total_return_text?: string
  daily_sharpe_text?: string
  max_drawdown_text?: string
  top3_profit_share_text?: string
  remove_top3_return_text?: string
  trade_count?: number
}

const COMPARISON_STRATEGIES = [
  {
    code: 'g2_alpha191_volume5_keep80_runup',
    positioning: '第二代完整版',
    top3_profit_share_text: '20.96%',
    remove_top3_return_text: '--',
  },
]

const FALLBACK_CLASSIFICATIONS = [
  { value: '', label: '未分类', type: 'info' },
  { value: 'favorite', label: '最喜欢', type: 'success' },
  { value: 'watch', label: '值得研究', type: 'primary' },
  { value: 'normal', label: '普通样本', type: 'info' },
  { value: 'problem', label: '问题交易', type: 'warning' },
  { value: 'reject', label: '应过滤', type: 'danger' },
]

const UP = 'text-red-600 dark:text-red-400 font-bold tabular-nums'
const DOWN = 'text-green-600 dark:text-green-500 font-bold tabular-nums'

function numValue(value: unknown): number {
  const n = Number(value)
  return Number.isFinite(n) ? n : 0
}

function pct(value: unknown, digits = 2): string {
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(digits)}%` : '--'
}

function price(value: unknown): string {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(3) : '--'
}

function exitText(reason?: string): string {
  const map: Record<string, string> = {
    stop_loss_30m: '30m止损',
    take_profit_partial_30m: '止盈半仓',
    weak_prev_day_low_break_30m: '跌破昨日低点',
    time_exit: '时间退出',
  }
  return map[reason || ''] || reason || '--'
}

function exitBadgeColor(reason?: string): 'red' | 'emerald' | 'amber' | 'zinc' {
  if (reason === 'stop_loss_30m') return 'red'
  if (reason === 'take_profit_partial_30m') return 'emerald'
  if (reason === 'weak_prev_day_low_break_30m') return 'amber'
  return 'zinc'
}

function tagTypeToBadgeColor(type?: string): 'red' | 'amber' | 'emerald' | 'blue' | 'zinc' {
  if (type === 'danger' || type === 'hot') return 'red'
  if (type === 'warning') return 'amber'
  if (type === 'success' || type === 'primary') return 'emerald'
  if (type === 'primary') return 'blue'
  return 'zinc'
}

function latestTimeValue(row: any, keys: string[]): string {
  for (const key of keys) {
    const value = String(row?.[key] || '').trim()
    if (value) return value
  }
  return ''
}

function sortByLatestTime<T>(rows: T[] | undefined, preferredKeys: string[], fallbackKeys: string[] = []): T[] {
  return [...(Array.isArray(rows) ? rows : [])].sort((a, b) => {
    const aTime = latestTimeValue(a, [...preferredKeys, ...fallbackKeys])
    const bTime = latestTimeValue(b, [...preferredKeys, ...fallbackKeys])
    if (aTime !== bTime) return aTime < bTime ? 1 : -1
    const aTie = String((a as any)?.code || (a as any)?.name || (a as any)?.trade_key || '')
    const bTie = String((b as any)?.code || (b as any)?.name || (b as any)?.trade_key || '')
    return aTie < bTie ? 1 : aTie > bTie ? -1 : 0
  })
}

function buildTradeKey(row: TradeRow): string {
  if (!row) return ''
  return [row.code, row.buy_datetime || row.buy_date, row.sell_datetime || row.sell_date, row.sell_ratio, row.exit_reason]
    .map((item) => String(item || '').trim())
    .join('|')
}

export default function Gen2Backtest() {
  const toastApi = useToast()
  const [loading, setLoading] = useState(false)
  const [comparisonLoading, setComparisonLoading] = useState(false)
  const [available, setAvailable] = useState(false)
  const [message, setMessage] = useState('')
  const [strategyCode, setStrategyCode] = useState('g2_alpha191_volume5_keep80_runup')
  const [payload, setPayload] = useState<BacktestPayload>({})
  const [comparisonRows, setComparisonRows] = useState<ComparisonRow[]>([])
  const [classifyDialogVisible, setClassifyDialogVisible] = useState(false)
  const [classifySaving, setClassifySaving] = useState(false)
  const [classifyForm, setClassifyForm] = useState<{ row: TradeRow | null; classification: string; note: string }>({
    row: null,
    classification: '',
    note: '',
  })
  const [updateTask, setUpdateTask] = useState<any>(null)
  const [updateRunning, setUpdateRunning] = useState(false)
  const updateTaskTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const metrics = payload.metrics || {}
  const summary = payload.summary || {}
  const ruleLines = payload.rule_lines || []
  const equityCurve = payload.equity_curve || []
  const tradeClassifications = payload.trade_classifications || FALLBACK_CLASSIFICATIONS
  const strategyName = payload.strategy_display_name || payload.strategy_name || 'G2 第二代完整版'
  const statusText = payload.status === 'frozen_research' ? '研究版已固化' : payload.status || ''

  const trades = useMemo(
    () => sortByLatestTime(payload.trades, ['sell_datetime', 'sell_date'], ['buy_datetime', 'buy_date']),
    [payload.trades],
  )
  const signals = useMemo(
    () => sortByLatestTime(payload.signals, ['confirm_datetime', 'entry_date'], ['code', 'trade_key']),
    [payload.signals],
  )

  const concentrationText = (() => {
    const level = metrics.profit_concentration_level
    if (level === 'high') return '高'
    if (level === 'medium') return '中'
    if (level === 'low') return '低'
    return '--'
  })()
  const concentrationClass = (() => {
    const level = metrics.profit_concentration_level
    if (level === 'high') return DOWN
    if (level === 'medium') return 'font-bold tabular-nums text-amber-600 dark:text-amber-400'
    return UP
  })()

  const updateTaskTitle = (() => {
    const task = updateTask || {}
    if (task.status === 'queued') return '历史回测更新已排队'
    if (task.status === 'running') return `历史回测更新中：${task.progress || 0}%`
    if (task.status === 'completed') return '历史回测已更新到最新可用数据'
    if (task.status === 'failed') return `历史回测更新失败：${task.error || '后台任务异常'}`
    return `历史回测任务状态：${task.status || '--'}`
  })()
  const updateTaskDescription = (() => {
    const task = updateTask || {}
    const result = task.result || {}
    const endDate = result.backtest_end_date || result.latest_source_date || task.end_date || ''
    if (task.status === 'completed' && endDate) return `已生成并替换正式回测产物，最新结束日：${endDate}`
    if (task.status === 'running') return '正在重建第二代完整版信号源、回测窗口和正式展示产物。'
    return task.stderr_tail || task.stdout_tail || ''
  })()

  const chartOption = useMemo<EChartsCoreOption>(() => {
    const dates = equityCurve.map((row) => row.date)
    const strategy = equityCurve.map((row) => Number(row.strategy_equity || 1))
    const benchmark = equityCurve.map((row) => Number(row.benchmark_equity || 1))
    const drawdown = equityCurve.map((row) => Number(row.drawdown || 0) * 100)
    return {
      color: ['#1769aa', '#7f8c9a', '#d64f4f'],
      tooltip: { trigger: 'axis', valueFormatter: (value: unknown) => Number(value).toFixed(2) },
      legend: { top: 0, data: ['策略净值', '000852.SH', '回撤%'] },
      grid: [
        { left: 48, right: 56, top: 42, height: '58%' },
        { left: 48, right: 56, top: '74%', height: '18%' },
      ],
      xAxis: [
        { type: 'category', data: dates, boundaryGap: false, axisLabel: { show: false } },
        { type: 'category', data: dates, boundaryGap: false, gridIndex: 1 },
      ],
      yAxis: [
        { type: 'value', scale: true, axisLabel: { formatter: '{value}x' } },
        { type: 'value', gridIndex: 1, axisLabel: { formatter: '{value}%' } },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1] },
        { type: 'slider', xAxisIndex: [0, 1], bottom: 0, height: 18 },
      ],
      series: [
        { name: '策略净值', type: 'line', data: strategy, showSymbol: false, smooth: true, lineStyle: { width: 2.4 } },
        { name: '000852.SH', type: 'line', data: benchmark, showSymbol: false, smooth: true, lineStyle: { width: 1.8, type: 'dashed' } },
        {
          name: '回撤%',
          type: 'line',
          data: drawdown,
          xAxisIndex: 1,
          yAxisIndex: 1,
          showSymbol: false,
          areaStyle: { opacity: 0.12 },
          lineStyle: { width: 1.8 },
        },
      ],
    }
  }, [equityCurve])

  const fetchData = useCallback(async () => {
    setLoading(true)
    try {
      const data = ((await getGen2Backtest({ strategy_code: strategyCode })) as any) || {}
      setPayload(data)
      setAvailable(!!data.available)
      setMessage(data.message || '')
    } catch (error) {
      setAvailable(false)
      setMessage((error as Error)?.message || '读取第二代策略回测失败')
    } finally {
      setLoading(false)
    }
  }, [strategyCode])

  const fetchComparison = useCallback(async () => {
    setComparisonLoading(true)
    try {
      const results = await Promise.all(
        COMPARISON_STRATEGIES.map(async (item): Promise<ComparisonRow> => {
          try {
            const data: any = await getGen2Backtest({ strategy_code: item.code })
            const m = data?.metrics || {}
            return {
              strategy_code: item.code,
              name: data?.strategy_display_name || data?.strategy_name || item.code,
              positioning: item.positioning,
              available: !!data?.available,
              detail_available: !!data?.available,
              total_return: m.total_return,
              total_return_text: m.total_return_text,
              daily_sharpe_text: m.daily_sharpe_text,
              max_drawdown_text: m.max_drawdown_text,
              top3_profit_share_text: m.top3_profit_share_text || item.top3_profit_share_text,
              remove_top3_return_text: m.remove_top3_return_text || item.remove_top3_return_text,
              trade_count: m.trade_count || 0,
            }
          } catch {
            return { strategy_code: item.code, name: item.code, positioning: item.positioning, available: false }
          }
        }),
      )
      setComparisonRows(results.filter((row) => row.available))
    } finally {
      setComparisonLoading(false)
    }
  }, [])

  const stopUpdatePolling = useCallback(() => {
    if (updateTaskTimerRef.current) clearInterval(updateTaskTimerRef.current)
    updateTaskTimerRef.current = null
  }, [])

  const pollUpdateTask = useCallback(
    async (taskId: string) => {
      try {
        const task = ((await getGen2BacktestUpdateTask(taskId)) as any) || null
        setUpdateTask(task)
        const status = task?.status
        setUpdateRunning(status === 'queued' || status === 'running')
        if (status === 'completed') {
          stopUpdatePolling()
          toastApi.success('历史回测已更新，正在刷新结果')
          await fetchComparison()
          await fetchData()
        } else if (status === 'failed') {
          stopUpdatePolling()
          toastApi.error(task?.error || '历史回测更新失败')
        }
      } catch (error) {
        console.warn('轮询 Gen2 历史回测任务失败:', error)
      }
    },
    [fetchComparison, fetchData, stopUpdatePolling, toastApi],
  )

  const startUpdateLatest = useCallback(async () => {
    if (updateRunning) return
    setUpdateRunning(true)
    try {
      const task: any = await runGen2BacktestLatest(undefined)
      if (!task?.task_id) throw new Error('历史回测更新任务启动失败')
      setUpdateTask(task)
      toastApi.success('历史回测更新任务已提交')
      stopUpdatePolling()
      updateTaskTimerRef.current = setInterval(() => pollUpdateTask(task.task_id), 2500)
      await pollUpdateTask(task.task_id)
    } catch (error) {
      setUpdateRunning(false)
      toastApi.error((error as Error)?.message || '启动历史回测更新失败')
    }
  }, [pollUpdateTask, stopUpdatePolling, toastApi, updateRunning])

  const selectComparisonRow = useCallback(
    async (row: ComparisonRow) => {
      if (!row?.strategy_code || row.strategy_code === strategyCode) return
      if (row.detail_available === false) {
        toastApi.info('该版本结果已展示；重启后端后可查看曲线和交易明细')
        return
      }
      setStrategyCode(row.strategy_code)
    },
    [strategyCode, toastApi],
  )

  const copyCode = useCallback(
    async (code?: string) => {
      const raw = String(code || '').trim()
      const text = raw.match(/\d{6}/)?.[0] || raw
      if (!text) return
      try {
        await navigator.clipboard.writeText(text)
        toastApi.success(`已复制 ${text}`)
      } catch {
        toastApi.error('复制失败，请手动选择代码')
      }
    },
    [toastApi],
  )

  const openClassifyDialog = useCallback((row: TradeRow) => {
    setClassifyForm({ row, classification: row.classification || '', note: row.classification_note || '' })
    setClassifyDialogVisible(true)
  }, [])

  const saveClassification = useCallback(async () => {
    const row = classifyForm.row
    const tradeKey = row?.trade_key || buildTradeKey(row as TradeRow)
    if (!tradeKey) {
      toastApi.error('缺少交易标识，无法保存分类')
      return
    }
    setClassifySaving(true)
    try {
      const result: any = await saveGen2TradeClassification({
        strategy_code: strategyCode,
        trade_key: tradeKey,
        classification: classifyForm.classification,
        note: classifyForm.note,
      })
      if (!result?.ok) throw new Error(result?.message || '保存失败')
      const meta =
        tradeClassifications.find((item) => item.value === classifyForm.classification) ||
        tradeClassifications[0] || { label: '未分类' }
      setPayload((prev) => ({
        ...prev,
        trades: (prev.trades || []).map((item) =>
          item === row
            ? {
                ...item,
                classification: classifyForm.classification,
                trade_key: tradeKey,
                classification_label: result.classification_label || meta.label,
                classification_type: result.classification_type,
                classification_note: classifyForm.note,
              }
            : item,
        ),
      }))
      setClassifyDialogVisible(false)
      toastApi.success('交易分类已保存')
    } catch (error) {
      toastApi.error((error as Error)?.message || '保存交易分类失败')
    } finally {
      setClassifySaving(false)
    }
  }, [classifyForm, strategyCode, toastApi, tradeClassifications])

  useEffect(() => {
    fetchComparison()
    fetchData()
  }, [fetchComparison, fetchData])

  useEffect(() => stopUpdatePolling, [stopUpdatePolling])

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="第二代策略历史回测"
        subtitle={`${strategyName} · ${statusText}`}
        actions={
          <>
            <Select value={strategyCode} onChange={(e) => setStrategyCode(e.target.value)} className="w-60">
              <option value="g2_alpha191_volume5_keep80_runup">G2 第二代完整版</option>
            </Select>
            <Button outline disabled={loading} onClick={fetchData}>{loading ? '刷新中…' : '刷新'}</Button>
            <Button disabled={updateRunning} onClick={startUpdateLatest}>
              {updateRunning ? '更新中' : '更新到最新数据'}
            </Button>
          </>
        }
      />

      {updateTask && (
        <Alert color={updateTask.status === 'failed' ? 'red' : updateTask.status === 'completed' ? 'green' : 'zinc'}>
          <span className="font-semibold">{updateTaskTitle}</span>
          {updateTaskDescription ? <span className="block text-sm opacity-80">{updateTaskDescription}</span> : null}
        </Alert>
      )}

      {!available && !loading && <Alert color="amber">{message || '第二代策略回测数据不可用'}</Alert>}

      <Card className="p-5">
        <SectionTitle
          title="第二代回测版本对比"
          actions={<span className="text-sm text-zinc-500 dark:text-zinc-400">点击行查看明细</span>}
        />
        <div className="overflow-auto rounded-lg">
          <Table dense className="min-w-full">
            <TableHead>
              <TableRow>
                <TableHeader>版本</TableHeader>
                <TableHeader>总收益</TableHeader>
                <TableHeader>Sharpe</TableHeader>
                <TableHeader>最大回撤</TableHeader>
                <TableHeader>Top3贡献</TableHeader>
                <TableHeader>去Top3收益</TableHeader>
                <TableHeader>交易数</TableHeader>
                <TableHeader>定位</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {comparisonRows.map((row) => (
                <TableRow
                  key={row.strategy_code}
                  onClick={() => selectComparisonRow(row)}
                  className={`cursor-pointer hover:bg-zinc-100 dark:hover:bg-white/10 ${
                    row.strategy_code === strategyCode ? 'bg-blue-50 dark:bg-blue-500/15' : ''
                  }`}
                >
                  <TableCell>{row.name}</TableCell>
                  <TableCell className="text-right">
                    <span className={numValue(row.total_return) >= 0 ? UP : DOWN}>{row.total_return_text || '--'}</span>
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{row.daily_sharpe_text}</TableCell>
                  <TableCell className="text-right"><span className={DOWN}>{row.max_drawdown_text || '--'}</span></TableCell>
                  <TableCell className="text-right tabular-nums">{row.top3_profit_share_text}</TableCell>
                  <TableCell className="text-right tabular-nums">{row.remove_top3_return_text}</TableCell>
                  <TableCell className="text-right tabular-nums">{row.trade_count}</TableCell>
                  <TableCell>{row.positioning}</TableCell>
                </TableRow>
              ))}
              {!comparisonRows.length && (
                <TableRow>
                  <TableCell colSpan={8} className="text-center text-zinc-400">
                    {comparisonLoading ? '加载中…' : '暂无回测结果'}
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </div>
      </Card>

      {available && (
        <>
          <section className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-3">
            {[
              { label: '回测窗口', value: `${summary.start_date || '--'} 至 ${summary.end_date || '--'}`, tone: 'default' as const },
              { label: '总收益', value: metrics.total_return_text || '--', tone: (numValue(metrics.total_return) >= 0 ? 'up' : 'down') as 'up' | 'down' },
              { label: '超额收益', value: metrics.excess_return_text || '--', tone: (numValue(metrics.excess_return) >= 0 ? 'up' : 'down') as 'up' | 'down' },
              { label: '最大回撤', value: metrics.max_drawdown_text || '--', tone: 'down' as const },
              { label: 'Sharpe', value: metrics.daily_sharpe_text || '--', tone: 'default' as const },
              { label: '胜率', value: metrics.win_rate_text || '--', tone: 'default' as const },
              { label: '单笔均值', value: metrics.avg_trade_return_text || '--', tone: 'default' as const },
              { label: '交易次数', value: metrics.trade_count || 0, tone: 'default' as const },
            ].map((metric) => (
              <StatCard key={metric.label} label={metric.label} value={metric.value} tone={metric.tone} />
            ))}
          </section>

          {metrics.profit_concentration_level && (
            <Card className="grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3 p-4">
              {[
                { label: '集中度风险', value: concentrationText, cls: concentrationClass },
                { label: 'Top1 盈利贡献', value: metrics.top1_profit_share_text || '--', cls: '' },
                { label: 'Top3 盈利贡献', value: metrics.top3_profit_share_text || '--', cls: '' },
                { label: 'Top5 盈利贡献', value: metrics.top5_profit_share_text || '--', cls: '' },
                { label: '去 Top3 后收益', value: metrics.remove_top3_return_text || '--', cls: '' },
              ].map((item) => (
                <div key={item.label} className="flex min-w-0 flex-col gap-1.5">
                  <span className="text-xs text-zinc-500 dark:text-zinc-400">{item.label}</span>
                  <strong className={`text-lg text-zinc-900 dark:text-white ${item.cls}`}>{item.value}</strong>
                </div>
              ))}
            </Card>
          )}

          <section className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(320px,1fr)]">
            <Card className="p-5">
              <SectionTitle
                title="收益曲线"
                actions={<Badge color="zinc">{summary.start_date} 至 {summary.end_date}</Badge>}
              />
              <EChart option={chartOption} height={380} />
            </Card>
            <Card className="p-5">
              <SectionTitle title="固化交易规则" actions={<Badge color="emerald">已固化</Badge>} />
              <ul className="list-disc pl-5 text-sm leading-loose text-zinc-600 dark:text-zinc-400">
                {ruleLines.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </Card>
          </section>

          <Card className="p-5">
            <SectionTitle
              title="每笔交易"
              actions={<span className="text-sm text-zinc-500 dark:text-zinc-400">完整 {trades.length} 条</span>}
            />
            <div className="max-h-[460px] overflow-auto rounded-lg">
              <Table dense striped className="min-w-full">
                <TableHead>
                  <TableRow>
                    <TableHeader>买入日</TableHeader>
                    <TableHeader>卖出日</TableHeader>
                    <TableHeader>代码</TableHeader>
                    <TableHeader>分类</TableHeader>
                    <TableHeader>买入策略</TableHeader>
                    <TableHeader>名称</TableHeader>
                    <TableHeader>买入确认</TableHeader>
                    <TableHeader>卖出时间</TableHeader>
                    <TableHeader>收益</TableHeader>
                    <TableHeader>盈亏</TableHeader>
                    <TableHeader>卖出比例</TableHeader>
                    <TableHeader>退出原因</TableHeader>
                    <TableHeader>V4排名</TableHeader>
                    <TableHeader>买入价</TableHeader>
                    <TableHeader>卖出价</TableHeader>
                    <TableHeader>操作</TableHeader>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {trades.map((row, idx) => (
                    <TableRow key={row.trade_key || `${row.code}-${idx}`}>
                      <TableCell className="tabular-nums">{row.buy_date}</TableCell>
                      <TableCell className="tabular-nums">{row.sell_date}</TableCell>
                      <TableCell>
                        <button
                          type="button"
                          title="复制代码"
                          onClick={() => copyCode(row.code)}
                          className="inline-flex h-6 min-w-[92px] items-center gap-1.5 rounded-md border border-blue-200 bg-blue-50 px-2 font-mono text-xs text-blue-800 hover:border-blue-400 hover:bg-blue-100 dark:border-blue-500/30 dark:bg-blue-500/10 dark:text-blue-300"
                        >
                          {row.code}
                          <span aria-hidden>⧉</span>
                        </button>
                      </TableCell>
                      <TableCell>
                        <Badge color={tagTypeToBadgeColor(row.classification_type)}>
                          {row.classification_label || '未分类'}
                        </Badge>
                      </TableCell>
                      <TableCell className="max-w-[200px]">
                        <Badge color={tagTypeToBadgeColor(row.buy_strategy_type)}>
                          {row.buy_strategy_label || '未知来源'}
                        </Badge>
                        {row.buy_sector_name && (
                          <span className="mt-1 block text-xs text-zinc-500 dark:text-zinc-400">{row.buy_sector_name}</span>
                        )}
                      </TableCell>
                      <TableCell>{row.name}</TableCell>
                      <TableCell className="tabular-nums">{row.buy_datetime}</TableCell>
                      <TableCell className="tabular-nums">{row.sell_datetime}</TableCell>
                      <TableCell className="text-right"><ChangeValue value={numValue(row.return) * 100} /></TableCell>
                      <TableCell className="text-right"><ChangeValue value={numValue(row.pnl)} suffix="" /></TableCell>
                      <TableCell className="text-right tabular-nums">{pct(row.sell_ratio, 0)}</TableCell>
                      <TableCell>
                        <Badge color={exitBadgeColor(row.exit_reason)}>{exitText(row.exit_reason)}</Badge>
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{row.v4_rank}</TableCell>
                      <TableCell className="text-right tabular-nums">{price(row.buy_price)}</TableCell>
                      <TableCell className="text-right tabular-nums">{price(row.sell_price)}</TableCell>
                      <TableCell>
                        <Button outline onClick={() => openClassifyDialog(row)}>修改</Button>
                      </TableCell>
                    </TableRow>
                  ))}
                  {!trades.length && (
                    <TableRow>
                      <TableCell colSpan={16} className="text-center text-zinc-400">暂无交易</TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </div>
          </Card>

          <Card className="p-5">
            <SectionTitle
              title="触发信号"
              actions={<span className="text-sm text-zinc-500 dark:text-zinc-400">展示 {signals.length} 条</span>}
            />
            <div className="max-h-[360px] overflow-auto rounded-lg">
              <Table dense striped className="min-w-full">
                <TableHead>
                  <TableRow>
                    <TableHeader>入场日</TableHeader>
                    <TableHeader>代码</TableHeader>
                    <TableHeader>名称</TableHeader>
                    <TableHeader>状态</TableHeader>
                    <TableHeader>模式</TableHeader>
                    <TableHeader>触发</TableHeader>
                    <TableHeader>确认时间</TableHeader>
                    <TableHeader>V4排名</TableHeader>
                    <TableHeader>入场价</TableHeader>
                    <TableHeader>3日收益</TableHeader>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {signals.map((row, idx) => (
                    <TableRow key={`${row.code}-${row.entry_date}-${idx}`}>
                      <TableCell className="tabular-nums">{row.entry_date}</TableCell>
                      <TableCell>
                        <button
                          type="button"
                          title="复制代码"
                          onClick={() => copyCode(row.code)}
                          className="inline-flex h-6 min-w-[92px] items-center gap-1.5 rounded-md border border-blue-200 bg-blue-50 px-2 font-mono text-xs text-blue-800 hover:border-blue-400 hover:bg-blue-100 dark:border-blue-500/30 dark:bg-blue-500/10 dark:text-blue-300"
                        >
                          {row.code}
                          <span aria-hidden>⧉</span>
                        </button>
                      </TableCell>
                      <TableCell>{row.name}</TableCell>
                      <TableCell>{row.g2_open_state}</TableCell>
                      <TableCell className="max-w-[220px] truncate" title={row.pattern}>{row.pattern}</TableCell>
                      <TableCell className="max-w-[260px] truncate" title={row.trigger_type}>{row.trigger_type}</TableCell>
                      <TableCell className="tabular-nums">{row.confirm_datetime}</TableCell>
                      <TableCell className="text-right tabular-nums">{row.v4_rank}</TableCell>
                      <TableCell className="text-right tabular-nums">{price(row.entry_price)}</TableCell>
                      <TableCell className="text-right"><ChangeValue value={numValue(row.entry_fwd_ret_3d) * 100} /></TableCell>
                    </TableRow>
                  ))}
                  {!signals.length && (
                    <TableRow>
                      <TableCell colSpan={10} className="text-center text-zinc-400">暂无信号</TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </div>
          </Card>
        </>
      )}

      <Dialog open={classifyDialogVisible} onClose={setClassifyDialogVisible} size="sm">
        <DialogTitle>修改交易分类</DialogTitle>
        <DialogBody>
          {classifyForm.row && (
            <div className="mb-4 flex flex-col gap-1">
              <strong className="text-zinc-900 dark:text-white">
                {classifyForm.row.code} {classifyForm.row.name}
              </strong>
              <span className="text-sm text-zinc-500 dark:text-zinc-400">
                {classifyForm.row.buy_datetime} / {pct(classifyForm.row.return)}
              </span>
            </div>
          )}
          <div className="flex flex-col gap-4">
            <Field>
              <Label htmlFor="classify-select">分类</Label>
              <Select
                id="classify-select"
                value={classifyForm.classification}
                onChange={(e) => setClassifyForm((prev) => ({ ...prev, classification: e.target.value }))}
              >
                {tradeClassifications.map((item) => (
                  <option key={item.value} value={item.value}>{item.label}</option>
                ))}
              </Select>
            </Field>
            <Field>
              <Label htmlFor="classify-note">备注</Label>
              <Textarea
                id="classify-note"
                rows={3}
                maxLength={160}
                placeholder="可选：记录为什么这样分类"
                value={classifyForm.note}
                onChange={(e) => setClassifyForm((prev) => ({ ...prev, note: e.target.value }))}
              />
            </Field>
          </div>
        </DialogBody>
        <DialogActions>
          <Button plain onClick={() => setClassifyDialogVisible(false)}>取消</Button>
          <Button disabled={classifySaving} onClick={saveClassification}>{classifySaving ? '保存中…' : '保存'}</Button>
        </DialogActions>
      </Dialog>
    </div>
  )
}
