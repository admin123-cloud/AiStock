import { useCallback, useEffect, useState } from 'react'
import {
  getClsEventCandidates,
  getClsNewsSignals,
  getClsNewsStatus,
  getClsRadarSummary,
  syncClsNews,
} from '@/api/news'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { useToast } from '@/components/toast'
import { Input } from '@/components/catalyst/input'
import { Card, ChangeValue, EmptyState, PageHeader, PageLoading, SectionTitle, StatCard } from '@/components/ui'

interface SignalRow {
  id: number | string
  event_time?: string
  signal_class?: string
  opportunity_score?: number | string
  title?: string
  content?: string
  event_score?: number | string
  pre_position_score?: number | string
  post_confirm_score?: number | string
  chase_risk_score?: number | string
  strategy_overlap_score?: number | string
  theme_overlap_score?: number | string
  reason?: string
  themes_text?: string
  subjects_text?: string
  related_codes?: string
  strategy_overlap_names?: string
  theme_overlap_labels?: string
}

interface HotEventRow {
  event_id: string | number
  event_time?: string
  title?: string
  best_name?: string
  best_code?: string
  best_score?: number | string
  best_setup_tag?: string
  best_capital_tag?: string
  best_action_tag?: string
}

interface CandidateRow {
  code: string
  name?: string
  relation?: string
  industry?: string
  sector_names?: string
  candidate_score?: number | string
  setup_tag?: string
  action_tag?: string
  risk_tag?: string
  capital_proxy_score?: number | string
  capital_tag?: string
  amount_ratio5?: number | string
  amount_ratio20?: number | string
  change_pct?: number | string
  amount?: number | string
  snapshot_price?: number | string
  decision_reason?: string
  snapshot_time?: string
}

type CountMap = Record<string, number>

interface RadarSummaryData {
  signal_count?: number
  class_counts?: CountMap
  setup_counts?: CountMap
  capital_counts?: CountMap
  theme_counts?: CountMap
  hot_events?: HotEventRow[]
}

interface StatusData {
  state?: {
    last_sync_at?: string
    latest_id?: string | number
    last_result?: { new_rows?: number }
  }
  raw?: { cnt?: number }
  signals?: { cnt?: number }
  context?: Array<{ usable?: boolean }>
}

const CLASS_OPTIONS = [
  'A1 主升确认',
  'A2 可短线跟随',
  'B1 只观察',
  'C1 疑似兑现',
  'D1 噪音过滤',
]

function score(value: unknown) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '--'
  return num.toFixed(1)
}

function summaryPairs(value: CountMap | undefined) {
  return Object.entries(value || {}).slice(0, 8)
}

function scoreClass(value: unknown) {
  const num = Number(value)
  if (num >= 70) return 'text-orange-700 dark:text-orange-400'
  if (num >= 55) return 'text-amber-700 dark:text-amber-400'
  return ''
}

function classBadgeColor(value: unknown): 'red' | 'amber' | 'zinc' | 'emerald' {
  const text = String(value || '')
  if (text.startsWith('A1')) return 'red'
  if (text.startsWith('A2')) return 'amber'
  if (text.startsWith('C1') || text.startsWith('D1')) return 'zinc'
  return 'emerald'
}

function candidateBadgeColor(value: unknown): 'red' | 'amber' | 'zinc' | 'emerald' | undefined {
  const text = String(value || '')
  if (text.includes('主升浪')) return 'red'
  if (text.includes('埋伏')) return 'amber'
  if (text.includes('追高')) return 'zinc'
  if (text.includes('跟风')) return 'emerald'
  return undefined
}

function timeText(value: unknown) {
  if (!value) return '--'
  return String(value).replace('T', ' ').replace('+08:00', '').slice(5, 16)
}

function shortText(value: unknown, length: number) {
  const text = String(value || '')
  return text.length > length ? `${text.slice(0, length)}...` : text
}

function ratioText(value: unknown) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '--'
  return `${num.toFixed(2)}x`
}

function amountText(value: unknown) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '--'
  if (num >= 1e4) return `${(num / 1e4).toFixed(2)}亿`
  return `${num.toFixed(0)}万`
}

function priceText(value: unknown) {
  const num = Number(value)
  if (!Number.isFinite(num) || num <= 0) return '--'
  return num.toFixed(2)
}

const thClass =
  'sticky top-0 z-10 border-b border-zinc-200 bg-white px-2.5 py-2 text-left text-xs font-semibold whitespace-nowrap text-zinc-500 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-400'
const tdClass = 'border-b border-zinc-100 px-2.5 py-2 text-sm whitespace-nowrap text-zinc-700 dark:border-zinc-800/60 dark:text-zinc-300'

export default function ClsNewsRadar() {
  const toast = useToast()
  const [loading, setLoading] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [limit, setLimit] = useState(80)
  const [minScore, setMinScore] = useState(0)
  const [classFilter, setClassFilter] = useState('')
  const [status, setStatus] = useState<StatusData>({})
  const [radarSummary, setRadarSummary] = useState<RadarSummaryData>({})
  const [signals, setSignals] = useState<SignalRow[]>([])
  const [selectedSignalId, setSelectedSignalId] = useState<number | string | null>(null)
  const [candidateLoading, setCandidateLoading] = useState(false)
  const [candidates, setCandidates] = useState<CandidateRow[]>([])
  const [candidateSummary, setCandidateSummary] = useState<{ direct?: number; sector_peer?: number; total?: number }>({})

  const contextItems = Array.isArray(status?.context) ? status.context : []
  const usableContextCount = contextItems.filter((item) => item?.usable).length
  const radarHotEvents = Array.isArray(radarSummary?.hot_events) ? radarSummary.hot_events : []

  const candidateSummaryText = !selectedSignalId
    ? '选择一条消息后展示'
    : `直接 ${candidateSummary?.direct || 0} / 扩散 ${candidateSummary?.sector_peer || 0} / 共 ${candidateSummary?.total || 0}`

  const loadCandidates = useCallback(async (eventId: number | string | null) => {
    if (!eventId) {
      setCandidates([])
      setCandidateSummary({})
      return
    }
    setCandidateLoading(true)
    try {
      const data = (await getClsEventCandidates(eventId, { limit: 40, peer_limit: 25 })) as
        | { items?: CandidateRow[]; summary?: { direct?: number; sector_peer?: number; total?: number } }
        | undefined
      setCandidates(Array.isArray(data?.items) ? data.items : [])
      setCandidateSummary(data?.summary || {})
    } finally {
      setCandidateLoading(false)
    }
  }, [])

  const loadSignals = useCallback(async () => {
    setLoading(true)
    try {
      const data = (await getClsNewsSignals({
        limit,
        min_score: minScore,
        signal_class: classFilter || undefined,
      })) as { items?: SignalRow[] } | undefined
      const items = Array.isArray(data?.items) ? data.items : []
      setSignals(items)
      setStatus(((await getClsNewsStatus()) as StatusData) || {})
      setRadarSummary(((await getClsRadarSummary({ limit: 50, top_events: 10, peer_limit: 12 })) as RadarSummaryData) || {})
      if (items.length) {
        const stillExists = items.some((row) => row.id === selectedSignalId)
        const nextId = stillExists ? selectedSignalId : items[0].id
        setSelectedSignalId(nextId)
        await loadCandidates(nextId)
      } else {
        setSelectedSignalId(null)
        setCandidates([])
        setCandidateSummary({})
      }
    } finally {
      setLoading(false)
    }
  }, [limit, minScore, classFilter, selectedSignalId, loadCandidates])

  const syncNow = useCallback(async () => {
    setSyncing(true)
    try {
      const result = (await syncClsNews({ pages: 1, rn: 50 })) as { new_rows?: number } | undefined
      toast.success(`同步完成，新增 ${result?.new_rows ?? 0} 条`)
      await loadSignals()
    } finally {
      setSyncing(false)
    }
  }, [loadSignals, toast])

  useEffect(() => {
    void loadSignals()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleSignalClick = (row: SignalRow) => {
    setSelectedSignalId(row?.id || null)
    void loadCandidates(row?.id || null)
  }

  const statusItems: Array<[string, string]> = [
    ['最近同步', status?.state?.last_sync_at || '--'],
    ['原始消息', String(status?.raw?.cnt ?? 0)],
    ['信号记录', String(status?.signals?.cnt ?? 0)],
    ['最新消息 ID', String(status?.state?.latest_id || '--')],
    ['最新新增', String(status?.state?.last_result?.new_rows ?? '--')],
    ['可用上下文', `${usableContextCount} / ${contextItems.length}`],
  ]

  return (
    <div className="flex flex-col gap-3.5">
      <PageHeader
        title="财联社消息雷达"
        subtitle="CLS News Radar"
        actions={
          <>
            <Input
              type="number"
              min={10}
              max={200}
              step={10}
              value={String(limit)}
              onChange={(e) => setLimit(Number(e.target.value) || 0)}
              className="w-24 tabular-nums"
              aria-label="条数"
            />
            <select
              value={classFilter}
              onChange={(e) => setClassFilter(e.target.value)}
              className="h-9 w-40 rounded-lg border border-zinc-200 bg-white px-3 text-sm text-zinc-900 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-100"
            >
              <option value="">信号分层(全部)</option>
              {CLASS_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
            <Input
              type="number"
              min={0}
              max={100}
              step={5}
              value={String(minScore)}
              onChange={(e) => setMinScore(Number(e.target.value) || 0)}
              className="w-20 tabular-nums"
              aria-label="最低分"
            />
            <Button outline disabled={loading} onClick={() => void loadSignals()}>
              查询
            </Button>
            <Button color="blue" disabled={syncing} onClick={() => void syncNow()}>
              {syncing ? '同步中...' : '同步'}
            </Button>
          </>
        }
      />

      <section className="grid grid-cols-1 gap-2.5 sm:grid-cols-2 min-[1101px]:grid-cols-6">
        {statusItems.map(([label, value]) => (
          <StatCard key={label} label={label} value={value} />
        ))}
      </section>

      <Card className="flex flex-col gap-3 p-3.5">
        <SectionTitle
          title="雷达总览"
          actions={
            <span className="text-[13px] text-zinc-500 tabular-nums dark:text-zinc-400">
              {radarSummary.signal_count || 0} 条信号
            </span>
          }
        />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 min-[1101px]:grid-cols-4">
          {(
            [
              ['分层', radarSummary.class_counts],
              ['候选状态', radarSummary.setup_counts],
              ['资金状态', radarSummary.capital_counts],
            ] as Array<[string, CountMap | undefined]>
          ).map(([title, counts]) => (
            <div key={title}>
              <div className="mb-1.5 text-[13px] font-bold text-slate-600 dark:text-zinc-300">{title}</div>
              {summaryPairs(counts).map(([key, value]) => (
                <div
                  key={key}
                  className="flex justify-between gap-2 border-b border-zinc-100 py-1 text-xs text-slate-500 dark:border-zinc-800 dark:text-zinc-400"
                >
                  <span className="truncate">{key}</span>
                  <strong className="text-zinc-900 tabular-nums dark:text-zinc-100">{value}</strong>
                </div>
              ))}
            </div>
          ))}
          <div>
            <div className="mb-1.5 text-[13px] font-bold text-slate-600 dark:text-zinc-300">题材</div>
            {summaryPairs(radarSummary.theme_counts)
              .slice(0, 6)
              .map(([key, value]) => (
                <div
                  key={key}
                  className="flex justify-between gap-2 border-b border-zinc-100 py-1 text-xs text-slate-500 dark:border-zinc-800 dark:text-zinc-400"
                >
                  <span className="truncate">{key}</span>
                  <strong className="text-zinc-900 tabular-nums dark:text-zinc-100">{value}</strong>
                </div>
              ))}
          </div>
        </div>
        <div className="max-h-[220px] overflow-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr>
                <th className={thClass}>时间</th>
                <th className={thClass}>事件</th>
                <th className={thClass}>最强候选</th>
                <th className={`${thClass} text-right`}>候选分</th>
                <th className={thClass}>状态</th>
                <th className={thClass}>资金</th>
                <th className={thClass}>动作</th>
              </tr>
            </thead>
            <tbody>
              {radarHotEvents.length === 0 ? (
                <tr>
                  <td colSpan={7} className="border-b-0 py-0">
                    <EmptyState text="暂无可映射事件" className="border-0 py-6" />
                  </td>
                </tr>
              ) : (
                radarHotEvents.map((row) => (
                  <tr key={row.event_id} className="odd:bg-zinc-50/60 dark:odd:bg-white/[0.02]">
                    <td className={`${tdClass} tabular-nums`}>{timeText(row.event_time)}</td>
                    <td className={`${tdClass} max-w-[360px] truncate`} title={row.title}>
                      {row.title}
                    </td>
                    <td className={tdClass}>
                      {row.best_name || row.best_code || '--'} {row.best_code || ''}
                    </td>
                    <td className={`${tdClass} text-right tabular-nums`}>{score(row.best_score)}</td>
                    <td className={`${tdClass} max-w-[160px] truncate`} title={row.best_setup_tag}>
                      {row.best_setup_tag}
                    </td>
                    <td className={`${tdClass} max-w-[160px] truncate`} title={row.best_capital_tag}>
                      {row.best_capital_tag}
                    </td>
                    <td className={`${tdClass} max-w-[160px] truncate`} title={row.best_action_tag}>
                      {row.best_action_tag}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </Card>

      <Card className="p-3.5">
        <SectionTitle
          title="消息机会池"
          actions={
            <span className="text-[13px] text-zinc-500 tabular-nums dark:text-zinc-400">
              {signals.length} 条
            </span>
          }
        />
        <div className="max-h-[640px] overflow-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr>
                <th className={thClass}>时间</th>
                <th className={thClass}>分层</th>
                <th className={`${thClass} text-right`}>机会分</th>
                <th className={thClass}>消息</th>
                <th className={`${thClass} text-right`}>消息分</th>
                <th className={`${thClass} text-right`}>提前埋伏</th>
                <th className={`${thClass} text-right`}>后确认</th>
                <th className={`${thClass} text-right`}>跟风风险</th>
                <th className={`${thClass} text-right`}>策略重合</th>
                <th className={`${thClass} text-right`}>题材重合</th>
                <th className={thClass}>触发原因</th>
              </tr>
            </thead>
            <tbody>
              {signals.length === 0 ? (
                <tr>
                  <td colSpan={11} className="border-b-0 py-0">
                    <EmptyState text="暂无财联社消息信号" className="border-0 py-6" />
                  </td>
                </tr>
              ) : (
                signals.map((row) => (
                  <tr
                    key={row.id}
                    onClick={() => handleSignalClick(row)}
                    className={`cursor-pointer odd:bg-zinc-50/60 hover:bg-blue-50/70 dark:odd:bg-white/[0.02] dark:hover:bg-white/5 ${
                      row.id === selectedSignalId
                        ? 'bg-blue-100/80! dark:bg-blue-500/15!'
                        : ''
                    }`}
                  >
                    <td className={`${tdClass} tabular-nums`}>{timeText(row.event_time)}</td>
                    <td className={tdClass}>
                      <Badge color={classBadgeColor(row.signal_class)}>{row.signal_class || '--'}</Badge>
                    </td>
                    <td className={`${tdClass} text-right tabular-nums`}>
                      <strong className={scoreClass(row.opportunity_score)}>
                        {score(row.opportunity_score)}
                      </strong>
                    </td>
                    <td className={`${tdClass} max-w-[420px] whitespace-normal`}>
                      <div className="font-bold text-zinc-900 dark:text-zinc-100">
                        {row.title || shortText(row.content, 48)}
                      </div>
                      <div className="mt-1 leading-relaxed text-slate-500 dark:text-zinc-400">
                        {row.content}
                      </div>
                      <div className="mt-1.5 flex flex-wrap gap-2 text-xs text-zinc-500 dark:text-zinc-400">
                        {row.themes_text && <span>题材 {row.themes_text}</span>}
                        {row.subjects_text && <span>话题 {row.subjects_text}</span>}
                        {row.related_codes && <span>个股 {row.related_codes}</span>}
                        {row.strategy_overlap_names && <span>策略重合 {row.strategy_overlap_names}</span>}
                        {row.theme_overlap_labels && <span>主线重合 {row.theme_overlap_labels}</span>}
                      </div>
                    </td>
                    <td className={`${tdClass} text-right tabular-nums`}>{score(row.event_score)}</td>
                    <td className={`${tdClass} text-right tabular-nums`}>{score(row.pre_position_score)}</td>
                    <td className={`${tdClass} text-right tabular-nums`}>{score(row.post_confirm_score)}</td>
                    <td className={`${tdClass} text-right tabular-nums`}>{score(row.chase_risk_score)}</td>
                    <td className={`${tdClass} text-right tabular-nums`}>{score(row.strategy_overlap_score)}</td>
                    <td className={`${tdClass} text-right tabular-nums`}>{score(row.theme_overlap_score)}</td>
                    <td className={`${tdClass} max-w-[280px] truncate`} title={row.reason}>
                      {row.reason}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </Card>

      <Card className="p-3.5">
        <SectionTitle
          title="事件候选"
          actions={
            <span className="text-[13px] text-zinc-500 dark:text-zinc-400">{candidateSummaryText}</span>
          }
        />
        {candidateLoading ? (
          <PageLoading text="候选加载中..." />
        ) : (
          <div className="max-h-[360px] overflow-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  <th className={thClass}>关系</th>
                  <th className={thClass}>股票</th>
                  <th className={thClass}>板块</th>
                  <th className={`${thClass} text-right`}>候选分</th>
                  <th className={thClass}>状态</th>
                  <th className={thClass}>动作</th>
                  <th className={thClass}>风险</th>
                  <th className={`${thClass} text-right`}>资金分</th>
                  <th className={thClass}>资金状态</th>
                  <th className={`${thClass} text-right`}>5日量比</th>
                  <th className={`${thClass} text-right`}>20日量比</th>
                  <th className={`${thClass} text-right`}>涨跌幅</th>
                  <th className={`${thClass} text-right`}>成交额</th>
                  <th className={`${thClass} text-right`}>快照价</th>
                  <th className={thClass}>判断</th>
                  <th className={thClass}>快照时间</th>
                </tr>
              </thead>
              <tbody>
                {candidates.length === 0 ? (
                  <tr>
                    <td colSpan={16} className="border-b-0 py-0">
                      <EmptyState text="当前消息暂无可展开候选" className="border-0 py-6" />
                    </td>
                  </tr>
                ) : (
                  candidates.map((row) => (
                      <tr key={row.code} className="odd:bg-zinc-50/60 dark:odd:bg-white/[0.02]">
                        <td className={tdClass}>
                          <Badge color={row.relation === 'direct' ? 'red' : 'zinc'}>
                            {row.relation === 'direct' ? '直接关联' : '板块扩散'}
                          </Badge>
                        </td>
                        <td className={`${tdClass} whitespace-normal`}>
                          <div className="font-bold text-zinc-900 dark:text-zinc-100">
                            {row.name || row.code}
                          </div>
                          <div className="text-xs text-zinc-500 dark:text-zinc-400">
                            {row.code} {row.industry || ''}
                          </div>
                        </td>
                        <td className={`${tdClass} max-w-[200px] truncate`} title={row.sector_names}>
                          {row.sector_names}
                        </td>
                        <td className={`${tdClass} text-right tabular-nums`}>
                          <strong className={scoreClass(row.candidate_score)}>
                            {score(row.candidate_score)}
                          </strong>
                        </td>
                        <td className={tdClass}>
                          <Badge color={candidateBadgeColor(row.setup_tag) || 'zinc'}>
                            {row.setup_tag || '--'}
                          </Badge>
                        </td>
                        <td className={`${tdClass} max-w-[140px] truncate`} title={row.action_tag}>
                          {row.action_tag}
                        </td>
                        <td className={`${tdClass} max-w-[170px] truncate`} title={row.risk_tag}>
                          {row.risk_tag}
                        </td>
                        <td className={`${tdClass} text-right tabular-nums`}>{score(row.capital_proxy_score)}</td>
                        <td className={`${tdClass} max-w-[150px] truncate`} title={row.capital_tag}>
                          {row.capital_tag}
                        </td>
                        <td className={`${tdClass} text-right tabular-nums`}>{ratioText(row.amount_ratio5)}</td>
                        <td className={`${tdClass} text-right tabular-nums`}>{ratioText(row.amount_ratio20)}</td>
                        <td className={`${tdClass} text-right font-semibold`}>
                          <ChangeValue value={row.change_pct} />
                        </td>
                        <td className={`${tdClass} text-right tabular-nums`}>{amountText(row.amount)}</td>
                        <td className={`${tdClass} text-right tabular-nums`}>{priceText(row.snapshot_price)}</td>
                        <td className={`${tdClass} max-w-[280px] truncate`} title={row.decision_reason}>
                          {row.decision_reason}
                        </td>
                        <td className={`${tdClass} max-w-[190px] truncate`} title={row.snapshot_time}>
                          {row.snapshot_time}
                        </td>
                      </tr>
                    ))
                )}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}
