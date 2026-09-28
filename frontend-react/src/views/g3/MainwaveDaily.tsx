import { useCallback, useEffect, useMemo, useState } from 'react'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Alert, AlertTitle } from '@/components/catalyst/alert'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/catalyst/table'
import { Dialog, DialogBody, DialogTitle } from '@/components/catalyst/dialog'
import { Card, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import {
  dateTime, getMainwaveDaily, reasonText, statusLabel, statusTone, type StatusTone,
} from '@/api/operations'

interface MainwaveCandidate {
  code?: string
  name?: string
  industry?: string
  industry_source?: string
  stage?: string
  score?: number | string | null
  resonance?: number | string | null
  reason?: string | null
  m30_source?: string
  conflict_rows?: number
  confirmation_time?: string
  reference_price?: number | string | null
  entry_date?: string
  decision_date?: string
}

interface MainwaveCheck { name?: string; message?: string; ok?: boolean }
interface HistoryDay { date?: string; accepted?: boolean; reason?: string; repair_triggered?: boolean }
interface MainwaveChange { code?: string; name?: string; before?: string; after?: string }

interface MainwaveDailyData {
  state?: string
  stale_batch?: boolean
  entry_date?: string
  decision_date?: string
  source_generated_at?: string
  batch?: { ok?: boolean; batch_id?: string }
  summary?: {
    candidate_count?: number
    data_blocked_count?: number
    confirmed_count?: number
    accepted_days?: number
    observed_days?: number
  }
  checks?: MainwaveCheck[]
  candidates?: MainwaveCandidate[]
  comparison_status?: string
  comparison_date?: string
  changes?: MainwaveChange[]
  history?: HistoryDay[]
  contract?: {
    entry?: {
      minimum_score?: number | string
      minimum_same_day_industry_mainwave_count?: number | string
      index_mom60_max?: number
    }
  }
  contract_metadata?: { strategy_id?: string; sha256?: string }
}

type FilterKey = 'all' | 'data_blocked' | 'waiting_30m' | 'confirmed'

const FILTERS: { key: FilterKey; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'data_blocked', label: '数据阻断' },
  { key: 'waiting_30m', label: '待确认' },
  { key: 'confirmed', label: '已确认' },
]

const toneBadge = (tone: StatusTone) =>
  tone === 'danger' ? 'red' : tone === 'success' ? 'emerald' : 'amber'

const score = (v: number | string | null | undefined): string =>
  v != null && v !== '' && Number.isFinite(Number(v)) ? Number(v).toFixed(1) : '待评估'

export default function MainwaveDaily() {
  const [data, setData] = useState<MainwaveDailyData>({})
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [filter, setFilter] = useState<FilterKey>('all')
  const [selected, setSelected] = useState<MainwaveCandidate | null>(null)
  const [historyDay, setHistoryDay] = useState<HistoryDay | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const res = await getMainwaveDaily()
      setData(res as MainwaveDailyData)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const headline = loading && !data.state ? '正在读取当日证据' : statusLabel(data.state ?? '')
  const explanation = data.state === 'data_blocked'
    ? '当前数据不足以形成可信买入结论，候选为0不等于今天没有机会。'
    : data.stale_batch
      ? '当前显示历史批次，不作为今天的新买点。'
      : '按当前合同跟踪候选；确认结果仍需通过账户与执行安全检查。'

  const blockedChecks = useMemo(() => (data.checks || []).filter((x) => !x.ok), [data.checks])
  const candidates = useMemo(
    () => (data.candidates || []).filter((x) => filter === 'all' || x.stage === filter),
    [data.candidates, filter],
  )
  const alertTone = statusTone(data.state ?? '')
  const minimumScore = data.contract?.entry?.minimum_score

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-4 sm:p-6">
      <PageHeader
        title="主升每日跟踪"
        subtitle="先判断数据是否可信，再看候选推进与每日验收。"
        actions={<Button onClick={() => void load()} disabled={loading}>{loading ? '刷新中…' : '刷新跟踪'}</Button>}
      />

      {error && (
        <div role="alert" className="rounded-lg border border-red-500/30 bg-red-50 px-4 py-3 text-sm text-red-700 dark:bg-red-500/10 dark:text-red-300">
          {error}；当前结果未更新。
        </div>
      )}

      <p className="text-sm text-zinc-500 dark:text-zinc-400">
        批次校验：{data.batch?.ok ? '完整批次已验证' : '未通过 / 旧产物待重新发布'} · {data.batch?.batch_id || '暂无批次号'}
      </p>

      <Alert color={alertTone === 'danger' ? 'red' : alertTone === 'success' ? 'green' : 'amber'}>
        <AlertTitle>{headline}</AlertTitle>
        <div className="mt-1 space-y-1 text-sm text-zinc-700 dark:text-zinc-300">
          <p>{explanation}</p>
          <p>
            执行日 {data.entry_date || '待确认'} · 决策日 {data.decision_date || '待确认'} · 信号批次 {dateTime(data.source_generated_at)}
          </p>
        </div>
      </Alert>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {[
          { label: '主升候选', value: data.summary?.candidate_count ?? '—' },
          { label: '数据不可判断', value: data.summary?.data_blocked_count ?? '—' },
          { label: '30m已确认', value: data.summary?.confirmed_count ?? '—' },
          { label: '最近观察通过', value: `${data.summary?.accepted_days ?? '—'} / ${data.summary?.observed_days ?? '—'}` },
        ].map((m) => (
          <StatCard key={m.label} label={m.label} value={m.value} />
        ))}
      </div>

      {blockedChecks.length > 0 && (
        <Card>
          <SectionTitle title="今天先处理这些问题" />
          <div className="flex flex-col gap-2">
            {blockedChecks.map((check) => (
              <div key={check.name} className="rounded-lg border border-red-500/20 bg-red-50/60 px-3 py-2 text-sm dark:bg-red-500/10">
                <strong className="text-zinc-900 dark:text-red-300">{check.message}</strong>
                <p className="text-xs text-zinc-500 dark:text-zinc-400">{check.name}</p>
              </div>
            ))}
          </div>
        </Card>
      )}

      <Card>
        <SectionTitle
          title="候选进展"
          actions={
            <div className="flex flex-wrap gap-1">
              {FILTERS.map((f) =>
                filter === f.key ? (
                  <Button key={f.key} onClick={() => setFilter(f.key)}>{f.label}</Button>
                ) : (
                  <Button key={f.key} plain onClick={() => setFilter(f.key)}>{f.label}</Button>
                ),
              )}
            </div>
          }
        />
        <div className="max-h-[28rem] overflow-auto">
          <Table>
            <TableHead>
              <TableRow>
                <TableHeader className="min-w-[10rem]">股票</TableHeader>
                <TableHeader className="min-w-[8rem]">所属行业</TableHeader>
                <TableHeader className="min-w-[9rem]">当前阶段</TableHeader>
                <TableHeader>主升分 / 门槛</TableHeader>
                <TableHeader>同业共振</TableHeader>
                <TableHeader className="min-w-[11rem]">阻断原因</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {candidates.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={6} className="py-8 text-center text-sm text-zinc-500 dark:text-zinc-400">
                    本批次无可展示候选；请先查看上方数据状态
                  </TableCell>
                </TableRow>
              ) : candidates.map((row) => (
                <TableRow key={row.code} className="cursor-pointer" onClick={() => setSelected(row)}>
                  <TableCell>
                    <button type="button" className="text-sm font-medium text-indigo-600 hover:underline dark:text-indigo-400"
                      onClick={(e) => { e.stopPropagation(); setSelected(row) }}>
                      {row.name || row.code}
                    </button>
                    <div className="text-xs tabular-nums text-zinc-500 dark:text-zinc-400">{row.code}</div>
                  </TableCell>
                  <TableCell>{row.industry || '--'}</TableCell>
                  <TableCell>
                    <Badge color={toneBadge(statusTone(row.stage ?? ''))}>{statusLabel(row.stage ?? '')}</Badge>
                  </TableCell>
                  <TableCell className="tabular-nums">{score(row.score)} / {minimumScore ?? '--'}</TableCell>
                  <TableCell className="tabular-nums">{row.resonance ?? '--'}</TableCell>
                  <TableCell className="max-w-[18rem] truncate text-zinc-500 dark:text-zinc-400">{reasonText(row.reason)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </Card>

      <Card>
        <SectionTitle title="较上个有效交易日的变化" />
        <div className="space-y-1 text-sm text-zinc-600 dark:text-zinc-300">
          {data.comparison_status === 'data_blocked' ? (
            <p>当前数据未通过，不把异常造成的候选消失解释为策略退出。</p>
          ) : !data.comparison_date ? (
            <p>尚无同合同的有效比较基线。发布器将开始记录每日状态。</p>
          ) : (
            <>
              <p>比较基线 {data.comparison_date}</p>
              {!data.changes?.length && <p>本批次未发现阶段变化。</p>}
              {data.changes?.map((change) => (
                <p key={change.code}>
                  {change.name || change.code}：{change.before ? statusLabel(change.before) : '新进入'} → {change.after ? statusLabel(change.after) : '退出候选'}
                </p>
              ))}
            </>
          )}
        </div>
      </Card>

      <Card>
        <SectionTitle title="最近每日验收" />
        <p className="text-sm text-zinc-500 dark:text-zinc-400">数据冲突日与正常空仓日分开；通过天数只计当前合同。</p>
        <div className="mt-3 overflow-x-auto pb-1">
          <div className="flex gap-1.5">
            {data.history?.map((day) => (
              <button key={day.date} type="button" title={`${day.date} ${day.reason ?? ''}`}
                className={`flex min-w-[3.2rem] flex-col items-center rounded-lg border px-2 py-1.5 text-xs transition
                  ${day.accepted
                    ? 'border-emerald-500/30 bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300'
                    : 'border-red-500/30 bg-red-50 text-red-700 dark:bg-red-500/10 dark:text-red-300'}`}
                onClick={() => setHistoryDay(day)}>
                <span className="tabular-nums">{day.date?.slice(5)}</span>
                <small>{day.accepted ? '通过' : '阻断'}</small>
              </button>
            ))}
          </div>
        </div>
        {historyDay && (
          <p aria-live="polite" className="mt-2 text-sm text-zinc-600 dark:text-zinc-300">
            {historyDay.date}：{reasonText(historyDay.reason)}；分钟修复{historyDay.repair_triggered ? '已触发' : '未触发'}。
          </p>
        )}
      </Card>

      <Card>
        <SectionTitle title="当前合同" />
        <p className="text-sm text-zinc-600 dark:text-zinc-300">
          主升分 ≥ {minimumScore ?? '--'} · 同业共振 ≥ {data.contract?.entry?.minimum_same_day_industry_mainwave_count ?? '--'} · 指数60日涨幅 ≤ {((data.contract?.entry?.index_mom60_max || 0) * 100).toFixed(0)}% · 已完成30m放量突破前20根高点
        </p>
        <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
          {data.contract_metadata?.strategy_id} · {data.contract_metadata?.sha256?.slice(0, 12)} · 影子观察，正式买点和自动下单保持锁定。
        </p>
      </Card>

      <Dialog open={!!selected} onClose={() => setSelected(null)} size="2xl">
        <DialogTitle>候选证据</DialogTitle>
        <DialogBody>
          {selected && (
            <div className="space-y-2 text-sm text-zinc-700 dark:text-zinc-300">
              <h3 className="text-base font-semibold text-zinc-950 dark:text-white">
                {selected.name} {selected.code}
              </h3>
              <div><Badge color={toneBadge(statusTone(selected.stage ?? ''))}>{statusLabel(selected.stage ?? '')}</Badge></div>
              <p>所属行业：{selected.industry}（{selected.industry_source || '来源待确认'}）</p>
              <p>主升分：{score(selected.score)}；同业共振：{selected.resonance}</p>
              <p>30m来源：{selected.m30_source || '未知'}；冲突行数：{selected.conflict_rows || 0}</p>
              <p>确认时间：{selected.confirmation_time || '尚未确认'}</p>
              <p>原因：{reasonText(selected.reason)}</p>
              <p>参考价格：{selected.reference_price || '未提供'}</p>
              <p>执行日 {selected.entry_date}；决策日 {selected.decision_date}</p>
            </div>
          )}
        </DialogBody>
      </Dialog>
    </div>
  )
}
