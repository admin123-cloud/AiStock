import { useCallback, useEffect, useMemo, useState } from 'react'
import { alpha191Factors, alpha191Source } from '@/data/gen2Alpha191'
import { getGen2FactorRegistry, runGen2FactorTest } from '@/api/trading'
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
import { Card, PageHeader, SectionTitle, StatCard } from '@/components/ui'

interface FactorRow {
  id: string
  name: string
  theme: string
  status: string
  fields: string[]
  formula: string
  backend_status: string
  fieldsText: string
  dataNote: string
  testPlan: string
  guardrail: string
}

interface FactorTestResult {
  available?: boolean
  message?: string
  sample?: { valid_rows?: number; trade_days?: number; symbols?: number }
  ic?: { mean_ic?: number; days?: number; ic_ir?: number; positive_ratio?: number }
  spread?: { top_bottom_spread?: number; buckets?: Array<{ bucket: number | string; mean_return?: number }> }
  latest_rows?: Array<{ date?: string; code?: string; name?: string; factor_value?: number; [key: string]: any }>
  params?: { target?: string }
}

function buildDataNote(row: { status: string; fields: string[] }): string {
  if (row.status === 'data_gap') return '依赖基准指数、风格因子或递归状态等额外口径，需先确认历史可见数据源。'
  if ((row.fields || []).includes('vwap')) return '依赖 VWAP，需用成交额/成交量确认复权口径后生成。'
  return '依赖字段可由现有日线 OHLCV、收益率和滚动窗口生成，适合进入首批批量计算。'
}

function buildTestPlan(row: { theme: string }): string {
  const base = '单因子先跑 RankIC、分组收益、覆盖率、缺失率和极端值稳定性。'
  if (row.theme === 'price_volume_corr' || row.theme === 'price_volume') {
    return base + ' 再检查它对 G2 候选池量价拥挤和放量追高失败样本的过滤价值。'
  }
  return base + ' 通过后再接入 G2 排序或过滤层做组合验证。'
}

function buildGuardrail(row: { fields: string[] }): string {
  if ((row.fields || []).includes('vwap')) {
    return '复权价格、成交额和成交量口径必须一致；盘中版本只能用确认时点以前的分钟数据重算。'
  }
  return '默认 T 日收盘后生成，用于 T+1 决策；盘中 10:00/10:30 买点不得读取当日完整日线 OHLC。'
}

function themeText(theme?: string): string {
  const map: Record<string, string> = {
    benchmark_style: '基准/风格',
    neutralize: '中性化',
    size: '规模',
    price_volume_corr: '价量相关',
    vwap_deviation: 'VWAP偏离',
    price_volume: '量价',
    momentum_reversal: '动量反转',
    volatility: '波动',
    price_structure: '价格结构',
  }
  return map[theme || ''] || theme || '--'
}

function statusText(status?: string): string {
  if (status === 'ready') return '可直接测试'
  if (status === 'data_gap') return '需补数据'
  return '待拆解'
}

function statusBadgeColor(status?: string): 'emerald' | 'amber' | 'zinc' {
  if (status === 'ready') return 'emerald'
  if (status === 'data_gap') return 'amber'
  return 'zinc'
}

function fixed(value: unknown, digits = 2): string {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(digits) : '--'
}

function pct(value: unknown, digits = 2): string {
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(digits)}%` : '--'
}

function numValue(value: unknown): number {
  const n = Number(value)
  return Number.isFinite(n) ? n : 0
}

const TEST_PROTOCOL = [
  { title: '样本范围', text: '默认从 A 股可交易标的池开始，剔除停牌、ST、上市不足窗口的样本。' },
  { title: '收益标签', text: '至少比较 T+1、T+3、T+5，G2 入场场景另看确认 bar 后收益。' },
  { title: '评价指标', text: '单因子先看 RankIC、分组单调性、覆盖率、缺失率和极端值稳定性。' },
  { title: '交易验证', text: '通过单因子后再接入 G2 候选池，比较收益、回撤、胜率和交易次数变化。' },
  { title: '反过拟合', text: '训练、验证、盲测分开，参数和方向不能按单次 Sharpe 最高直接定稿。' },
  { title: '时间边界', text: '盘中策略不读取当日完整日线，默认使用 T-1 已确认因子值。' },
]

const EMPTY_FACTOR: FactorRow = {
  id: '',
  name: '',
  theme: '',
  status: 'todo',
  backend_status: 'pending_formula_engine',
  fields: [],
  formula: '',
  fieldsText: '',
  dataNote: '',
  testPlan: '',
  guardrail: '',
}

export default function Gen2FactorLab() {
  const [keyword, setKeyword] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const [backendRegistry, setBackendRegistry] = useState<any>({})
  const [selected, setSelected] = useState<FactorRow | null>(null)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<FactorTestResult | null>(null)
  const [testMessage, setTestMessage] = useState('')
  const [testParams, setTestParams] = useState({ start_date: '', end_date: '', horizon: 1 })

  const factorRows = useMemo<FactorRow[]>(() => {
    const backendMap = new Map<any, any>((backendRegistry?.factors || []).map((item: any) => [item.id, item]))
    return alpha191Factors.map((item: any) => {
      const backend = backendMap.get(item.id) || {}
      return {
        ...item,
        backend_status: backend.backend_status || (item.id === 'Alpha001' ? 'implemented' : 'pending_formula_engine'),
        fieldsText: (item.fields || []).join(', '),
        dataNote: buildDataNote(item),
        testPlan: buildTestPlan(item),
        guardrail: buildGuardrail(item),
      }
    })
  }, [backendRegistry])

  const filteredFactors = useMemo(() => {
    const kw = keyword.trim().toLowerCase()
    return factorRows.filter((item) => {
      const statusOk = statusFilter === 'all' || item.status === statusFilter
      const text = `${item.id} ${item.name} ${item.theme} ${item.formula} ${item.fieldsText}`.toLowerCase()
      return statusOk && (!kw || text.includes(kw))
    })
  }, [factorRows, keyword, statusFilter])

  const readyCount = factorRows.filter((item) => item.status === 'ready').length
  const implementedCount = factorRows.filter((item) => item.backend_status === 'implemented').length
  const activeFactor = selected || factorRows[0] || EMPTY_FACTOR

  const selectFactor = useCallback((row: FactorRow) => {
    setSelected(row)
    setTestMessage('')
    setTestResult(null)
  }, [])

  const runSelectedFactorTest = useCallback(async () => {
    if (!activeFactor?.id) return
    setTesting(true)
    setTestMessage('')
    try {
      const data: any = await runGen2FactorTest({
        factor_id: activeFactor.id,
        start_date: testParams.start_date || undefined,
        end_date: testParams.end_date || undefined,
        horizon: testParams.horizon,
      })
      setTestResult(data)
      setTestMessage(data?.message || '')
    } catch (error) {
      setTestResult(null)
      setTestMessage((error as Error)?.message || '因子测试失败')
    } finally {
      setTesting(false)
    }
  }, [activeFactor, testParams])

  useEffect(() => {
    ;(async () => {
      try {
        const data: any = await getGen2FactorRegistry(undefined)
        setBackendRegistry(data || {})
      } catch {
        setBackendRegistry({})
      }
    })()
  }, [])

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="第二代策略量化因子库"
        subtitle={
          <>
            以国泰君安 Alpha191 为主线，逐个拆解公式、标记数据可得性，并把可量化因子接入 G2 候选池测试流程。
            <span className="mt-1 block text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">
              来源：{alpha191Source.origin}{' '}
              <a
                href={alpha191Source.url}
                target="_blank"
                rel="noreferrer"
                className="font-semibold text-blue-600 hover:underline dark:text-blue-400"
              >
                查看研报 PDF
              </a>
            </span>
          </>
        }
        actions={
          <>
            <Field>
              <Label htmlFor="factor-keyword">搜索</Label>
              <Input
                id="factor-keyword"
                value={keyword}
                onChange={(e) => setKeyword(e.target.value)}
                placeholder="因子、字段、公式"
                className="w-56"
              />
            </Field>
            <Field>
              <Label htmlFor="factor-status">状态</Label>
              <Select
                id="factor-status"
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
                className="w-40"
              >
                <option value="all">全部状态</option>
                <option value="ready">可直接测试</option>
                <option value="data_gap">需补数据</option>
                <option value="todo">待拆解</option>
              </Select>
            </Field>
          </>
        }
      />

      <section className="grid grid-cols-[repeat(auto-fit,minmax(190px,1fr))] gap-3">
        {[
          { label: '目标因子', value: '191', note: 'GTJA Alpha191' },
          { label: '已建档', value: factorRows.length, note: '来自研报表 6 因子明细' },
          { label: '可直接测试', value: readyCount, note: 'OHLCV、收益率、滚动窗口优先' },
          { label: '后端已接入', value: implementedCount, note: '按 Alpha001 -> Alpha191 逐个补齐' },
        ].map((metric) => (
          <StatCard key={metric.label} label={metric.label} value={metric.value} hint={metric.note} />
        ))}
      </section>

      <section className="grid gap-4 lg:grid-cols-[minmax(0,1.45fr)_minmax(340px,0.55fr)]">
        <Card className="p-5">
          <SectionTitle
            title="因子建档清单"
            actions={<span className="text-sm text-zinc-500 dark:text-zinc-400">显示 {filteredFactors.length} 条</span>}
          />
          <div className="max-h-[520px] overflow-auto rounded-lg">
            <Table dense striped className="min-w-full">
              <TableHead>
                <TableRow>
                  <TableHeader>编号</TableHeader>
                  <TableHeader>名称</TableHeader>
                  <TableHeader>类型</TableHeader>
                  <TableHeader>数据</TableHeader>
                  <TableHeader>后端</TableHeader>
                  <TableHeader>字段</TableHeader>
                  <TableHeader>公式</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {filteredFactors.map((row) => (
                  <TableRow
                    key={row.id}
                    onClick={() => selectFactor(row)}
                    className={`cursor-pointer hover:bg-zinc-100 dark:hover:bg-white/10 ${
                      selected ? (selected.id === row.id ? 'bg-blue-50 dark:bg-blue-500/15' : '') : row === factorRows[0] ? 'bg-blue-50 dark:bg-blue-500/15' : ''
                    }`}
                  >
                    <TableCell className="font-mono">{row.id}</TableCell>
                    <TableCell>{row.name}</TableCell>
                    <TableCell>{themeText(row.theme)}</TableCell>
                    <TableCell>
                      <Badge color={statusBadgeColor(row.status)}>{statusText(row.status)}</Badge>
                    </TableCell>
                    <TableCell>
                      <Badge color={row.backend_status === 'implemented' ? 'emerald' : 'zinc'}>
                        {row.backend_status === 'implemented' ? '已接入' : '待接入'}
                      </Badge>
                    </TableCell>
                    <TableCell className="max-w-[220px] truncate" title={row.fieldsText}>{row.fieldsText}</TableCell>
                    <TableCell className="max-w-[300px] truncate font-mono text-xs" title={row.formula}>{row.formula}</TableCell>
                  </TableRow>
                ))}
                {!filteredFactors.length && (
                  <TableRow>
                    <TableCell colSpan={7} className="text-center text-zinc-400">暂无匹配因子</TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </div>
        </Card>

        <Card className="h-fit p-5">
          <SectionTitle
            title="拆解卡片"
            actions={<Badge color={statusBadgeColor(activeFactor.status)}>{statusText(activeFactor.status)}</Badge>}
          />
          <div className="flex flex-col gap-1.5 border-b border-zinc-200 pb-3 dark:border-white/10">
            <strong className="text-lg text-zinc-900 dark:text-white">{activeFactor.id} {activeFactor.name}</strong>
            <span className="text-sm text-zinc-500 dark:text-zinc-400">
              {themeText(activeFactor.theme)} / {activeFactor.backend_status === 'implemented' ? '后端已接入' : '等待后端接入'}
            </span>
          </div>
          <dl className="mt-3">
            {[
              { term: '原始公式', node: <dd className="font-mono text-xs break-words leading-relaxed text-zinc-700 dark:text-zinc-300">{activeFactor.formula}</dd> },
              {
                term: '可量化字段',
                node: (
                  <dd className="flex flex-wrap gap-1.5">
                    {activeFactor.fields.map((field) => (
                      <Badge key={field} color="zinc">{field}</Badge>
                    ))}
                  </dd>
                ),
              },
              { term: '数据状态', node: <dd className="leading-relaxed text-zinc-700 dark:text-zinc-300">{activeFactor.dataNote}</dd> },
              { term: '测试计划', node: <dd className="leading-relaxed text-zinc-700 dark:text-zinc-300">{activeFactor.testPlan}</dd> },
              { term: 'G2 使用边界', node: <dd className="leading-relaxed text-zinc-700 dark:text-zinc-300">{activeFactor.guardrail}</dd> },
            ].map((item) => (
              <div key={item.term}>
                <dt className="mt-3 mb-1.5 text-xs font-bold text-zinc-500 dark:text-zinc-400">{item.term}</dt>
                {item.node}
              </div>
            ))}
          </dl>
        </Card>
      </section>

      <Card className="p-5">
        <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
          <SectionTitle
            className="mb-0"
            title="单因子测试"
            actions={<Badge color="emerald">No Future Function</Badge>}
          />
          <p className="w-full text-sm text-zinc-500 dark:text-zinc-400">
            默认按 T 日收盘后因子值预测未来收益，不读取当日完整日线做盘中买点判断。
          </p>
        </div>
        <div className="mb-3 flex flex-wrap items-end gap-3">
          <Field>
            <Label htmlFor="factor-start">开始日期</Label>
            <Input
              id="factor-start"
              type="date"
              value={testParams.start_date}
              onChange={(e) => setTestParams((prev) => ({ ...prev, start_date: e.target.value }))}
              className="w-44"
            />
          </Field>
          <Field>
            <Label htmlFor="factor-end">结束日期</Label>
            <Input
              id="factor-end"
              type="date"
              value={testParams.end_date}
              onChange={(e) => setTestParams((prev) => ({ ...prev, end_date: e.target.value }))}
              className="w-44"
            />
          </Field>
          <Field>
            <Label htmlFor="factor-horizon">持有期</Label>
            <Select
              id="factor-horizon"
              value={testParams.horizon}
              onChange={(e) => setTestParams((prev) => ({ ...prev, horizon: Number(e.target.value) }))}
              className="w-32"
            >
              <option value={1}>T+1</option>
              <option value={3}>T+3</option>
              <option value={5}>T+5</option>
            </Select>
          </Field>
          <Button disabled={testing} onClick={runSelectedFactorTest}>{testing ? '测试中…' : '运行测试'}</Button>
        </div>

        {testMessage && (
          <div className="mb-3">
            <Alert color={testResult?.available ? 'green' : 'amber'}>{testMessage}</Alert>
          </div>
        )}

        {testResult?.available && (
          <>
            <section className="grid grid-cols-[repeat(auto-fit,minmax(190px,1fr))] gap-3">
              {[
                {
                  label: '有效样本',
                  value: testResult.sample?.valid_rows || 0,
                  note: `${testResult.sample?.trade_days || 0} 个交易日 / ${testResult.sample?.symbols || 0} 只股票`,
                  tone: 'default' as const,
                },
                {
                  label: 'Mean RankIC',
                  value: pct(testResult.ic?.mean_ic),
                  note: `IC 天数 ${testResult.ic?.days || 0}`,
                  tone: (numValue(testResult.ic?.mean_ic) >= 0 ? 'up' : 'down') as 'up' | 'down',
                },
                {
                  label: 'ICIR',
                  value: fixed(testResult.ic?.ic_ir, 2),
                  note: `正 IC 占比 ${pct(testResult.ic?.positive_ratio)}`,
                  tone: 'default' as const,
                },
                {
                  label: 'Top-Bottom',
                  value: pct(testResult.spread?.top_bottom_spread),
                  note: '价量方向未固化，仅作单因子观察',
                  tone: (numValue(testResult.spread?.top_bottom_spread) >= 0 ? 'up' : 'down') as 'up' | 'down',
                },
              ].map((card) => (
                <StatCard key={card.label} label={card.label} value={card.value} hint={card.note} tone={card.tone} />
              ))}
            </section>

            <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(260px,0.7fr)_minmax(420px,1.3fr)]">
              <div>
                <div className="mb-2 flex items-center justify-between gap-3">
                  <h3 className="font-semibold text-zinc-900 dark:text-white">五分组收益</h3>
                  <span className="text-sm text-zinc-500 dark:text-zinc-400">低分 -&gt; 高分</span>
                </div>
                <Table dense className="min-w-full">
                  <TableHead>
                    <TableRow>
                      <TableHeader>组</TableHeader>
                      <TableHeader>平均收益</TableHeader>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {(testResult.spread?.buckets || []).map((row) => (
                      <TableRow key={row.bucket} className="bg-white dark:bg-zinc-950">
                        <TableCell>{row.bucket}</TableCell>
                        <TableCell className="tabular-nums">{pct(row.mean_return)}</TableCell>
                      </TableRow>
                    ))}
                    {!(testResult.spread?.buckets || []).length && (
                      <TableRow className="bg-white dark:bg-zinc-950">
                        <TableCell colSpan={2} className="text-center text-zinc-400">暂无分组</TableCell>
                      </TableRow>
                    )}
                  </TableBody>
                </Table>
              </div>
              <div>
                <div className="mb-2 flex items-center justify-between gap-3">
                  <h3 className="font-semibold text-zinc-900 dark:text-white">最新高分样本</h3>
                  <span className="text-sm text-zinc-500 dark:text-zinc-400">{testResult.params?.target}</span>
                </div>
                <div className="max-h-[240px] overflow-auto rounded-lg">
                  <Table dense striped className="min-w-full">
                    <TableHead>
                      <TableRow>
                        <TableHeader>日期</TableHeader>
                        <TableHeader>代码</TableHeader>
                        <TableHeader>名称</TableHeader>
                        <TableHeader>因子值</TableHeader>
                        <TableHeader>收益</TableHeader>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {(testResult.latest_rows || []).map((row, idx) => (
                        <TableRow key={`${row.code}-${row.date}-${idx}`}>
                          <TableCell className="tabular-nums">{row.date}</TableCell>
                          <TableCell className="font-mono">{row.code}</TableCell>
                          <TableCell>{row.name}</TableCell>
                          <TableCell className="tabular-nums">{fixed(row.factor_value, 4)}</TableCell>
                          <TableCell className="tabular-nums">{pct(row[testResult.params?.target || ''])}</TableCell>
                        </TableRow>
                      ))}
                      {!(testResult.latest_rows || []).length && (
                        <TableRow>
                          <TableCell colSpan={5} className="text-center text-zinc-400">暂无样本</TableCell>
                        </TableRow>
                      )}
                    </TableBody>
                  </Table>
                </div>
              </div>
            </div>
          </>
        )}
      </Card>

      <Card className="p-5">
        <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
          <SectionTitle
            className="mb-0"
            title="批量测试设计"
            actions={<Badge color="zinc">Alpha001 -&gt; Alpha191</Badge>}
          />
          <p className="w-full text-sm text-zinc-500 dark:text-zinc-400">
            后续每接入一个因子，都按同一套口径输出 IC、分组收益、覆盖率和 G2 组合验证。
          </p>
        </div>
        <div className="grid grid-cols-[repeat(auto-fit,minmax(240px,1fr))] gap-3">
          {TEST_PROTOCOL.map((item) => (
            <Card key={item.title}>
              <strong className="text-zinc-900 dark:text-white">{item.title}</strong>
              <p className="mt-2 text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">{item.text}</p>
            </Card>
          ))}
        </div>
      </Card>
    </div>
  )
}
