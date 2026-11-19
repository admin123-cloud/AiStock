import { useCallback, useEffect, useState } from 'react'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Card, PageHeader, SectionTitle } from '@/components/ui'
import {
  getG3DailyReview,
  getG3ShadowReview,
  getG3SignalQuality,
  type G3DailyReviewResponse,
  type G3ShadowReviewResponse,
  type G3SignalQualityResponse,
} from '@/api/g3'

type TabKey = 'daily' | 'shadow' | 'quality'

export default function G3Review() {
  const [tab, setTab] = useState<TabKey>('daily')
  const [daily, setDaily] = useState<G3DailyReviewResponse | null>(null)
  const [shadow, setShadow] = useState<G3ShadowReviewResponse | null>(null)
  const [quality, setQuality] = useState<G3SignalQualityResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [d, s, q] = await Promise.all([
        getG3DailyReview(10),
        getG3ShadowReview(20),
        getG3SignalQuality(10),
      ])
      setDaily(d)
      setShadow(s)
      setQuality(q)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 p-4 sm:p-6">
      <PageHeader
        title="G3 复盘与影子台账"
        description="整合日终复盘、成交对账教练、R40 影子做T与每日信号质量日报"
        actions={
          <Button onClick={() => void load()} disabled={loading}>
            {loading ? '刷新中…' : '刷新'}
          </Button>
        }
      />

      {error && (
        <div className="rounded-lg border border-red-500/40 bg-red-500/10 px-4 py-2.5 text-sm text-red-200">
          {error}
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        <Button outline={tab !== 'daily'} onClick={() => setTab('daily')}>
          日终复盘与对账 ({daily?.daily_reviews?.length ?? 0})
        </Button>
        <Button outline={tab !== 'shadow'} onClick={() => setTab('shadow')}>
          R40 影子做T台账 ({shadow?.daily_records?.length ?? 0})
        </Button>
        <Button outline={tab !== 'quality'} onClick={() => setTab('quality')}>
          信号质量日报 ({quality?.reports?.length ?? 0})
        </Button>
      </div>

      {tab === 'daily' && (
        <div className="flex flex-col gap-4">
          {!daily?.daily_reviews?.length ? (
            <Card><div className="py-6 text-center text-sm text-zinc-400">暂无日终复盘记录</div></Card>
          ) : (
            daily.daily_reviews.map((item) => (
              <Card key={item.file}>
                <div className="mb-2 flex items-center justify-between">
                  <SectionTitle title={`日终复盘 ${item.date}`} />
                  <Badge color="zinc">{item.file}</Badge>
                </div>
                <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg bg-zinc-950/70 p-3 font-mono text-xs leading-relaxed text-zinc-200">
                  {item.content}
                </pre>
              </Card>
            ))
          )}
        </div>
      )}

      {tab === 'shadow' && (
        <div className="flex flex-col gap-4">
          {shadow?.rolling_summary && (
            <Card>
              <SectionTitle title="4 周滚动统计" />
              <pre className="mt-2 overflow-auto whitespace-pre-wrap rounded-lg bg-zinc-950/70 p-3 font-mono text-xs text-zinc-200">
                {JSON.stringify(shadow.rolling_summary, null, 2)}
              </pre>
            </Card>
          )}
          {!shadow?.daily_records?.length ? (
            <Card><div className="py-6 text-center text-sm text-zinc-400">暂无影子做T记录</div></Card>
          ) : (
            shadow.daily_records.map((rec, idx) => (
              <Card key={String(rec.file || idx)}>
                <div className="mb-2 flex items-center justify-between">
                  <span className="font-mono text-sm font-medium text-white">{String(rec.file || rec.date || `#${idx + 1}`)}</span>
                  <Badge color="blue">observe_only</Badge>
                </div>
                <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-lg bg-zinc-950/70 p-3 font-mono text-xs text-zinc-300">
                  {JSON.stringify(rec, null, 2)}
                </pre>
              </Card>
            ))
          )}
        </div>
      )}

      {tab === 'quality' && (
        <div className="flex flex-col gap-4">
          {!quality?.reports?.length ? (
            <Card><div className="py-6 text-center text-sm text-zinc-400">暂无信号质量日报</div></Card>
          ) : (
            quality.reports.map((rep) => (
              <Card key={rep.file}>
                <div className="mb-2 flex items-center justify-between">
                  <SectionTitle title={`信号质量日报 ${rep.date}`} />
                  <Badge color="emerald">{rep.file}</Badge>
                </div>
                <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg bg-zinc-950/70 p-3 font-mono text-xs leading-relaxed text-zinc-200">
                  {rep.markdown}
                </pre>
              </Card>
            ))
          )}
        </div>
      )}
    </div>
  )
}
