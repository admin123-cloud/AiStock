import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Card, PageHeader } from '@/components/ui'
import G3AiBusPanel from '@/views/g3/G3AiBusPanel'
import {
  getG3AiBroadcasts,
  triggerG3AiBroadcastNow,
  type G3AiBroadcastFeedResponse,
  type G3AiBroadcastItem,
} from '@/api/g3'

export default function G3AiLive() {
  const [feed, setFeed] = useState<G3AiBroadcastFeedResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [triggering, setTriggering] = useState(false)
  const [filter, setFilter] = useState<'all' | 'ai_bus' | 'rule_snapshot'>('all')
  const [autoPlay, setAutoPlay] = useState(true)
  const [tickerIdx, setTickerIdx] = useState(0)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const feedRef = useRef<HTMLDivElement | null>(null)

  const loadFeed = useCallback(async (silent = false) => {
    if (!silent) setLoading(true)
    try {
      const res = await getG3AiBroadcasts(60)
      setFeed(res)
    } catch (e) {
      const err = e as { message?: string }
      if (!silent) setError(err.message || '加载 10 分钟盘面播报失败')
    } finally {
      if (!silent) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadFeed(false)
    const timer = window.setInterval(() => {
      void loadFeed(true)
    }, 15000)
    return () => window.clearInterval(timer)
  }, [loadFeed])

  const filteredItems: G3AiBroadcastItem[] = (feed?.items || []).filter((it) => {
    if (filter === 'all') return true
    return it.channel === filter
  })

  // 顶部轮播滚动播放条：每 6 秒自动切到下一条最新播报（可手动暂停/切条）
  useEffect(() => {
    if (!autoPlay || filteredItems.length <= 1) return
    const t = window.setInterval(() => {
      setTickerIdx((prev) => (prev + 1) % Math.min(filteredItems.length, 8))
    }, 6000)
    return () => window.clearInterval(t)
  }, [autoPlay, filteredItems.length])

  useEffect(() => {
    if (tickerIdx >= Math.max(1, Math.min(filteredItems.length, 8))) {
      setTickerIdx(0)
    }
  }, [filteredItems.length, tickerIdx])

  const handleBroadcastNow = async () => {
    setTriggering(true)
    setError('')
    setNotice('')
    try {
      const res = await triggerG3AiBroadcastNow()
      setNotice(
        res.ai_broadcast_job_id
          ? `已立即生成盘面快照并派发 AI 深度播报（${res.ai_broadcast_job_id}），仅上屏展示、不发微信。`
          : '已立即生成一条最新盘面规则快照，仅上屏展示、不发微信。',
      )
      await loadFeed(true)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '立即触发盘面播报失败')
    } finally {
      setTriggering(false)
    }
  }

  const activeTicker = filteredItems[tickerIdx] || filteredItems[0] || null

  return (
    <div className="space-y-4">
      <PageHeader
        title="AI 盘面直播与在线参谋"
        subtitle="交易时段每 10 分钟自动盘面播报（仅页面展示）"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Link
              to="/g3/desk"
              className="inline-flex items-center gap-1.5 rounded-md border border-emerald-500/50 bg-emerald-500/15 px-3 py-1.5 text-xs font-semibold text-emerald-200 transition hover:bg-emerald-500/25"
            >
              <span>⚡ 前往实战交易台一键填单</span>
              {(feed?.live_orders_count ?? 0) > 0 && (
                <Badge color="amber">{feed?.live_orders_count} 笔待填</Badge>
              )}
            </Link>
            <Button outline disabled={triggering} onClick={() => void handleBroadcastNow()}>
              {triggering ? '播报生成中…' : '立即播报一次'}
            </Button>
            <Button outline disabled={loading} onClick={() => void loadFeed(false)}>
              {loading ? '刷新中…' : '刷新播报'}
            </Button>
          </div>
        }
      />

      {/* 顶部滚动播放横幅 */}
      <Card className="border-sky-500/35 bg-gradient-to-r from-sky-950/35 via-zinc-900/90 to-cyan-950/30 px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex min-w-0 flex-1 items-center gap-2.5">
            <Badge color="sky">📡 10m 滚动直播</Badge>
            <Badge color={feed?.session_online ? 'emerald' : 'zinc'}>
              {feed?.session_online ? '🟢 AI 在线' : '⚪ 规则兜底模式'}
            </Badge>
            {activeTicker ? (
              <div className="min-w-0 flex-1 truncate text-xs font-medium text-white sm:text-sm">
                <span className="mr-2 font-mono text-sky-300">[{activeTicker.ts.slice(11, 16)}]</span>
                <span>{activeTicker.headline}</span>
              </div>
            ) : (
              <span className="text-xs text-zinc-400">
                等待交易时段每 10 分钟（09:35~14:55）自动播报，或点击右上角「立即播报一次」即时生成。
              </span>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-2 text-[11px]">
            {filteredItems.length > 1 && (
              <span className="font-mono text-zinc-400">
                {tickerIdx + 1}/{Math.min(filteredItems.length, 8)}
              </span>
            )}
            <button
              type="button"
              onClick={() => setAutoPlay((v) => !v)}
              className="rounded border border-zinc-700 bg-zinc-950 px-2 py-0.5 text-zinc-300 hover:border-sky-400 hover:text-sky-200"
            >
              {autoPlay ? '⏸ 暂停滚动' : '▶ 继续滚动'}
            </button>
          </div>
        </div>
      </Card>

      {error && (
        <div className="rounded-md border border-red-500/40 bg-red-950/30 px-3 py-2 text-xs text-red-200">
          ⚠️ {error}
        </div>
      )}
      {notice && (
        <div className="rounded-md border border-emerald-500/40 bg-emerald-950/25 px-3 py-2 text-xs text-emerald-200">
          ✅ {notice}
        </div>
      )}

      {/* 主体双栏：左 7 列 = 10 分钟盘面总结滚动时间轴；右 5 列 = 在线 AI 参谋（常驻展开） */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
        <div className="xl:col-span-7">
          <Card className="border-zinc-800 bg-zinc-950/85 p-4">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800 pb-2.5">
              <div className="flex items-center gap-2">
                <span className="text-sm font-bold text-white">🕒 每 10 分钟盘面总结时间轴</span>
                <span className="font-mono text-[11px] text-zinc-400">
                  共 {filteredItems.length} 条 · 每 15 秒自动刷新 · 绝不推微信
                </span>
              </div>
              <div className="inline-flex rounded-md border border-zinc-800 bg-zinc-900 p-0.5 text-[11px]">
                {([
                  ['all', '全部'],
                  ['ai_bus', 'AI 深度播报'],
                  ['rule_snapshot', '规则引擎快照'],
                ] as const).map(([val, label]) => (
                  <button
                    key={val}
                    type="button"
                    onClick={() => setFilter(val)}
                    className={`rounded px-2 py-0.5 transition ${
                      filter === val ? 'bg-sky-500/25 text-sky-200' : 'text-zinc-400 hover:text-zinc-200'
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>

            <div
              ref={feedRef}
              className="mt-3 max-h-[680px] space-y-3 overflow-y-auto pr-1"
            >
              {filteredItems.length === 0 ? (
                <div className="py-12 text-center text-xs text-zinc-400">
                  暂无播报记录。交易时段每 10 分钟自动更新，也可以点击右上角「立即播报一次」先生成当前盘面总结。
                </div>
              ) : (
                filteredItems.map((item) => (
                  <div
                    key={item.id}
                    className={`rounded-lg border p-4 text-sm ${
                      item.channel === 'ai_bus'
                        ? 'border-cyan-500/35 bg-cyan-950/15'
                        : 'border-zinc-800 bg-zinc-900/65'
                    }`}
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <Badge color={item.channel === 'ai_bus' ? 'cyan' : 'zinc'}>
                          {item.channel === 'ai_bus' ? '🤖 AI 深度播报' : '⚙️ 规则快照'}
                        </Badge>
                        {item.regime_label && <Badge color="amber">{item.regime_label}</Badge>}
                        {(item.executable_count ?? 0) > 0 && (
                          <Badge color="emerald">触发 {item.executable_count} 笔待填单</Badge>
                        )}
                      </div>
                      <span className="font-mono text-[11px] text-zinc-400">{item.ts}</span>
                    </div>

                    <div className="mt-2 font-semibold text-white">{item.headline}</div>
                    {item.body && (
                      <div className="mt-1.5 whitespace-pre-wrap leading-relaxed text-zinc-200">
                        {item.body}
                      </div>
                    )}

                    {item.risk_flags.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {item.risk_flags.map((rf, i) => (
                          <Badge key={i} color="rose">⚠️ {rf}</Badge>
                        ))}
                      </div>
                    )}

                    {item.actions.length > 0 && (
                      <div className="mt-2 flex flex-wrap items-center gap-1.5">
                        {item.actions.map((act, i) => (
                          <Badge key={i} color="emerald">👉 {act}</Badge>
                        ))}
                        {(item.executable_count ?? 0) > 0 && (
                          <Link
                            to="/g3/desk"
                            className="ml-auto text-[11px] font-semibold text-emerald-300 hover:text-emerald-200"
                          >
                            去实战交易台一键填单 →
                          </Link>
                        )}
                      </div>
                    )}
                  </div>
                ))
              )}
            </div>
          </Card>
        </div>

        <div className="xl:col-span-5">
          <G3AiBusPanel alwaysExpanded />
        </div>
      </div>
    </div>
  )
}
