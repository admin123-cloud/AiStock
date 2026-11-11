import { useCallback, useEffect, useMemo, useState } from 'react'
import request from '@/utils/request'
import { Card, EmptyState, SectionTitle } from '@/components/ui'

/* ------------------------------------------------------------------ */
/* R55（2026-11-11）：市场看板 P0 三件套                                */
/*   ① 市场宽度条（MA5/20/60 站上占比 + 60日新高/新低 + 高低比）        */
/*   ② 涨跌分布直方图（13 分桶 + 平均/中位，盘中快照/盘后日线双模式）   */
/*   ③ 涨停生态卡（封板率/炸板/最高连板 + 梯队迷你表）                 */
/* 数据随 R28 快照 5min 刷新（快照节奏对齐），全部 fail-closed 渲染。  */
/* ------------------------------------------------------------------ */

const UP = 'text-red-600 dark:text-red-400'
const DOWN = 'text-green-600 dark:text-green-500'

function fmtPct(value: unknown): string {
  if (value === null || value === undefined) return '—'
  return `${Number(value).toFixed(2)}%`
}

function fmtNum(value: unknown): string {
  if (value === null || value === undefined) return '—'
  return Number(value).toLocaleString('zh-CN')
}

function fmtAmountShort(value: unknown): string {
  const num = Number(value)
  if (!Number.isFinite(num) || num <= 0) return '—'
  if (num >= 1e8) return `${(num / 1e8).toFixed(1)}亿`
  if (num >= 1e4) return `${(num / 1e4).toFixed(0)}万`
  return num.toFixed(0)
}

/* ① 市场宽度条 */
function BreadthCard({ breadth }: { breadth: any | null }) {
  const rows = useMemo(() => {
    if (!breadth || breadth.error) return []
    return ([
      { key: 'MA5', pct: breadth.above_ma5_pct, base: breadth.base_counts?.ma5 },
      { key: 'MA20', pct: breadth.above_ma20_pct, base: breadth.base_counts?.ma20 },
      { key: 'MA60', pct: breadth.above_ma60_pct, base: breadth.base_counts?.ma60 },
    ])
  }, [breadth])

  return (
    <Card className="flex flex-col gap-3 px-4 pb-4 pt-4">
      <div className="flex items-baseline justify-between">
        <div className="text-sm font-medium text-zinc-500 dark:text-zinc-400">市场宽度</div>
        <div className="text-xs text-zinc-400 tabular-nums dark:text-zinc-500">{breadth?.date || ''}</div>
      </div>
      {rows.length === 0 ? (
        <EmptyState className="py-8" text="宽度数据不可用" />
      ) : (
        <>
          {rows.map((row) => {
            const pct = typeof row.pct === 'number' ? row.pct : null
            const width = pct === null ? 0 : Math.max(0, Math.min(100, pct))
            const tone = pct === null ? 'bg-zinc-300 dark:bg-zinc-600' : pct >= 50 ? 'bg-red-500' : 'bg-green-500'
            return (
              <div key={row.key}>
                <div className="mb-1 flex items-baseline justify-between text-xs tabular-nums">
                  <span className="text-zinc-500 dark:text-zinc-400">站上{row.key}占比</span>
                  <span className={pct === null ? 'text-zinc-400' : pct >= 50 ? UP : DOWN}>
                    {pct === null ? '—' : `${pct.toFixed(2)}%`}
                    <span className="ml-1 text-zinc-400 dark:text-zinc-500">/{fmtNum(row.base)}只</span>
                  </span>
                </div>
                <div className="h-2.5 w-full overflow-hidden rounded-full bg-zinc-200 dark:bg-slate-700">
                  <div className={`h-full rounded-full transition-all ${tone}`} style={{ width: `${width}%` }} />
                </div>
              </div>
            )
          })}
          <div className="mt-1 grid grid-cols-3 gap-2 border-t border-zinc-100 pt-2 text-center dark:border-slate-700/60">
            <div>
              <div className={`text-base font-bold tabular-nums ${UP}`}>{fmtNum(breadth.new_high_60d)}</div>
              <div className="text-[11px] text-zinc-400 dark:text-zinc-500">60日新高</div>
            </div>
            <div>
              <div className={`text-base font-bold tabular-nums ${DOWN}`}>{fmtNum(breadth.new_low_60d)}</div>
              <div className="text-[11px] text-zinc-400 dark:text-zinc-500">60日新低</div>
            </div>
            <div>
              <div className="text-base font-bold tabular-nums text-zinc-700 dark:text-zinc-200">
                {breadth.hl_ratio === null || breadth.hl_ratio === undefined ? '∞' : breadth.hl_ratio.toFixed(2)}
              </div>
              <div className="text-[11px] text-zinc-400 dark:text-zinc-500">高低比</div>
            </div>
          </div>
          <div className="text-[11px] leading-4 text-zinc-400 dark:text-zinc-500">
            共 {fmtNum(breadth.total)} 只；高低比=新高÷新低（新低为 0 时显示 ∞）
          </div>
        </>
      )}
    </Card>
  )
}

/* ② 涨跌分布直方图（13 桶，红涨绿跌） */
function DistributionCard({ distribution }: { distribution: any | null }) {
  const buckets = distribution?.buckets || []
  const maxCount = useMemo(
    () => buckets.reduce((acc: number, b: any) => Math.max(acc, b?.count || 0), 0),
    [buckets]
  )

  return (
    <Card className="flex flex-col gap-3 px-4 pb-4 pt-4">
      <div className="flex items-baseline justify-between">
        <div className="text-sm font-medium text-zinc-500 dark:text-zinc-400">涨跌分布</div>
        <div className="text-xs text-zinc-400 tabular-nums dark:text-zinc-500">
          {distribution?.mode === 'intraday'
            ? `盘中 ${distribution.as_of || ''}`
            : `盘后 ${distribution?.date || ''}`}
        </div>
      </div>
      {buckets.length === 0 ? (
        <EmptyState className="py-8" text="分布数据不可用" />
      ) : (
        <>
          <div className="flex h-32 items-end gap-[3px]">
            {buckets.map((bucket: any, idx: number) => {
              const height = maxCount > 0 ? Math.max(2, ((bucket.count || 0) / maxCount) * 100) : 2
              const color =
                bucket.side === 'up'
                  ? 'bg-red-500/85 dark:bg-red-500/75'
                  : bucket.side === 'down'
                    ? 'bg-green-500/85 dark:bg-green-500/75'
                    : 'bg-zinc-400/80 dark:bg-zinc-500/80'
              return (
                <div
                  key={idx}
                  className="group relative flex h-full flex-1 flex-col justify-end"
                  title={`${bucket.label}: ${fmtNum(bucket.count)}`}
                >
                  <div
                    className={`w-full rounded-t-[3px] transition-all ${color}`}
                    style={{ height: `${height}%` }}
                  />
                </div>
              )
            })}
          </div>
          <div className="flex gap-[3px] text-center text-[9px] leading-tight text-zinc-400 dark:text-zinc-500">
            {buckets.map((bucket: any, idx: number) => (
              <div key={idx} className="flex-1 truncate">{bucket.label}</div>
            ))}
          </div>
          <div className="grid grid-cols-2 gap-2 border-t border-zinc-100 pt-2 text-center dark:border-slate-700/60">
            <div>
              <div className={`text-base font-bold tabular-nums ${UP}`}>{fmtPct(distribution.avg_change_pct)}</div>
              <div className="text-[11px] text-zinc-400 dark:text-zinc-500">平均涨跌</div>
            </div>
            <div>
              <div className={`text-base font-bold tabular-nums ${(distribution.median_change_pct ?? 0) >= 0 ? UP : DOWN}`}>
                {fmtPct(distribution.median_change_pct)}
              </div>
              <div className="text-[11px] text-zinc-400 dark:text-zinc-500">中位涨跌</div>
            </div>
          </div>
          <div className="text-[11px] leading-4 text-zinc-400 dark:text-zinc-500">
            覆盖 {fmtNum(distribution.covered)} 只 · 分桶左闭右开（%）
          </div>
        </>
      )}
    </Card>
  )
}

/* ③ 涨停生态卡 */
function LimitUpCard({ stats }: { stats: any | null }) {
  const ladder = stats?.ladder || []
  const sealRate = stats?.seal_rate
  const sealPct = typeof sealRate === 'number' ? `${(sealRate * 100).toFixed(1)}%` : '—'

  return (
    <Card className="flex flex-col gap-3 px-4 pb-4 pt-4">
      <div className="flex items-baseline justify-between">
        <div className="text-sm font-medium text-zinc-500 dark:text-zinc-400">涨停生态</div>
        <div className="text-xs text-zinc-400 tabular-nums dark:text-zinc-500">
          {stats?.mode === 'intraday' ? `盘中 ${stats.as_of || ''}` : `收盘 ${stats?.date || ''}`}
        </div>
      </div>
      {!stats || stats.error || typeof stats.limit_up_count !== 'number' ? (
        <EmptyState className="py-8" text="涨停生态数据不可用" />
      ) : (
        <>
          <div className="grid grid-cols-5 gap-1 text-center">
            <div>
              <div className={`text-lg font-bold tabular-nums ${UP}`}>{fmtNum(stats.limit_up_count)}</div>
              <div className="text-[11px] text-zinc-400 dark:text-zinc-500">涨停</div>
            </div>
            <div>
              <div className={`text-lg font-bold tabular-nums ${DOWN}`}>{fmtNum(stats.limit_down_count)}</div>
              <div className="text-[11px] text-zinc-400 dark:text-zinc-500">跌停</div>
            </div>
            <div>
              <div className="text-lg font-bold tabular-nums text-zinc-700 dark:text-zinc-200">{sealPct}</div>
              <div className="text-[11px] text-zinc-400 dark:text-zinc-500">封板率</div>
            </div>
            <div>
              <div className="text-lg font-bold tabular-nums text-amber-600 dark:text-amber-400">{fmtNum(stats.broke_count)}</div>
              <div className="text-[11px] text-zinc-400 dark:text-zinc-500">炸板</div>
            </div>
            <div>
              <div className="text-lg font-bold tabular-nums text-zinc-700 dark:text-zinc-200">{fmtNum(stats.max_streak)}板</div>
              <div className="text-[11px] text-zinc-400 dark:text-zinc-500">最高连板</div>
            </div>
          </div>
          {ladder.length === 0 ? (
            <div className="py-3 text-center text-xs text-zinc-400 dark:text-zinc-500">今日无连板梯队</div>
          ) : (
            <div className="max-h-44 overflow-y-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-zinc-400 dark:text-zinc-500">
                    <th className="py-1 text-left font-normal">梯队</th>
                    <th className="py-1 text-right font-normal">数量</th>
                    <th className="py-1 text-left font-normal">代表股（成交额序）</th>
                  </tr>
                </thead>
                <tbody className="tabular-nums">
                  {ladder.map((row: any) => (
                    <tr key={row.streak} className="border-t border-zinc-100 dark:border-slate-700/50">
                      <td className="py-1 font-semibold text-zinc-700 dark:text-zinc-200">{row.label}</td>
                      <td className="py-1 text-right">{fmtNum(row.count)}</td>
                      <td className="py-1 truncate text-zinc-500 dark:text-zinc-400">
                        {(row.reps || []).map((rep: any) => rep.name).join('、')}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="text-[11px] leading-4 text-zinc-400 dark:text-zinc-500">
            封板率=现仍封÷曾涨停；炸板=曾涨停−现仍封；涨停价=昨收×板块规则
          </div>
        </>
      )}
    </Card>
  )
}

/* 主组件：三区块 + 5min 随快照刷新 */
export default function MarketDashboardBlocks() {
  const [breadth, setBreadth] = useState<any>(null)
  const [distribution, setDistribution] = useState<any>(null)
  const [limitStats, setLimitStats] = useState<any>(null)

  const load = useCallback(async () => {
    const params = { _ts: Date.now() }
    try {
      const [breadthRes, distRes, statsRes] = (await Promise.all([
        request.get('/market/breadth', { params, skipErrorHandler: true }),
        request.get('/market/advance-decline-distribution', { params, skipErrorHandler: true }),
        request.get('/market/limit-up-stats', { params, skipErrorHandler: true }),
      ])) as any[]
      setBreadth(breadthRes?.error ? null : breadthRes)
      setDistribution(distRes?.error ? null : distRes)
      setLimitStats(statsRes?.error ? null : statsRes)
    } catch (error) {
      console.error('市场看板三件套加载失败:', error)
    }
  }, [])

  useEffect(() => {
    load()
    const timer = window.setInterval(() => load(), 5 * 60 * 1000)
    return () => window.clearInterval(timer)
  }, [load])

  return (
    <section className="mb-8">
      <SectionTitle title="市场看板" />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <BreadthCard breadth={breadth} />
        <DistributionCard distribution={distribution} />
        <LimitUpCard stats={limitStats} />
      </div>
    </section>
  )
}
