import { useCallback, useEffect, useState } from 'react'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/catalyst/table'
import { Card, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import { inspectKline, type KlineInspectResponse } from '@/api/dataCenter'

const QUICK_ETF_CODES = [
  { code: '510880.SH', label: '红利ETF' },
  { code: '512890.SH', label: '红利低波' },
  { code: '511260.SH', label: '十年国债' },
  { code: '511090.SH', label: '30年国债' },
  { code: '518880.SH', label: '黄金ETF' },
  { code: '510300.SH', label: '沪深300ETF' },
  { code: '513100.SH', label: '纳指ETF' },
  { code: '513180.SH', label: '恒生科技' },
]

const QUICK_EQUITY_CODES = [
  { code: '000001.SH', label: '上证指数' },
  { code: '399006.SZ', label: '创业板指' },
  { code: '002015.SZ', label: '协鑫能科' },
  { code: '603881.SH', label: '数据港' },
  { code: '600519.SH', label: '贵州茅台' },
]

interface KlineCheckProps {
  embedded?: boolean
  initialCode?: string
}

export default function KlineCheck({ embedded = false, initialCode }: KlineCheckProps) {
  const defaultCode = (initialCode || '510880.SH').trim()
  const [inputCode, setInputCode] = useState(defaultCode)
  const [days, setDays] = useState(10)
  const [data, setData] = useState<KlineInspectResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const runInspect = useCallback(async (targetCode?: string) => {
    const code = (targetCode ?? inputCode).trim()
    if (!code) return
    setLoading(true)
    setError('')
    try {
      const res = await inspectKline(code, days)
      setData(res)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '体检查询失败')
    } finally {
      setLoading(false)
    }
  }, [inputCode, days])

  useEffect(() => {
    const target = (initialCode || '510880.SH').trim()
    setInputCode(target)
    void runInspect(target)
  }, [initialCode]) // eslint-disable-line react-hooks/exhaustive-deps

  const allComplete = data?.periods?.length ? data.periods.every((p) => p.complete) : false
  const isEtf = Boolean(data?.is_etf)
  const hasShare = Boolean(data?.has_share)

  return (
    <div className={embedded ? 'flex w-full flex-col gap-5' : 'mx-auto flex w-full max-w-6xl flex-col gap-5 p-4 sm:p-6'}>
      {!embedded && (
        <PageHeader
          title="单标的 K 线完整性体检"
          subtitle="支持股票/核心指数全周期（日线/5m/15m/30m/60m）及宽基/红利/国债/黄金/跨境 ETF 日线与国家队份额逐日核验"
        />
      )}

      {error && (
        <div className="rounded-lg border border-red-500/40 bg-red-500/10 px-4 py-2.5 text-sm text-red-200">
          {error}
        </div>
      )}

      {/* 查询栏 + 快捷标的 */}
      <Card>
        <div className="flex flex-wrap items-center gap-3">
          <input
            type="text"
            value={inputCode}
            onChange={(e) => setInputCode(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void runInspect() }}
            placeholder="输入 6 位代码如 510880、511260、002015 或 000001.SH"
            className="w-72 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-1.5 font-mono text-sm text-white placeholder-zinc-500 focus:border-blue-500 focus:outline-none"
          />
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-100 focus:border-blue-500 focus:outline-none"
          >
            <option value={10}>核验近 10 个交易日</option>
            <option value={20}>核验近 20 个交易日</option>
            <option value={30}>核验近 30 个交易日</option>
          </select>
          <Button onClick={() => void runInspect()} disabled={loading}>
            {loading ? '体检中…' : '立即体检'}
          </Button>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
          <span className="text-blue-400 font-medium">实战 ETF 抽检：</span>
          {QUICK_ETF_CODES.map((q) => (
            <button
              key={q.code}
              type="button"
              onClick={() => { setInputCode(q.code); void runInspect(q.code) }}
              className="rounded-md border border-blue-500/30 bg-blue-500/10 px-2.5 py-1 text-zinc-100 hover:border-blue-400/70 hover:bg-blue-500/20"
            >
              {q.label} <span className="font-mono text-zinc-400">{q.code}</span>
            </button>
          ))}
        </div>

        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
          <span className="text-zinc-400">股票 / 指数抽检：</span>
          {QUICK_EQUITY_CODES.map((q) => (
            <button
              key={q.code}
              type="button"
              onClick={() => { setInputCode(q.code); void runInspect(q.code) }}
              className="rounded-md border border-zinc-700 bg-zinc-800/70 px-2.5 py-1 text-zinc-200 hover:border-blue-500/60 hover:text-white"
            >
              {q.label} <span className="font-mono text-zinc-400">{q.code}</span>
            </button>
          ))}
        </div>
      </Card>

      {data && (
        <>
          {/* 标的摘要 */}
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <StatCard
              label="体检标的"
              value={data.name}
              hint={`${data.code} · ${data.type}`}
            />
            <StatCard
              label="全周期闭合结论"
              value={allComplete ? '100% 完整' : '存在缺口'}
              hint={`近 ${data.days} 个已收盘交易日`}
            />
            <StatCard
              label="日线总根数"
              value={(data.periods.find((p) => p.period === 'daily')?.total_bars ?? 0).toLocaleString()}
              hint={data.periods.find((p) => p.period === 'daily')?.max_time || '—'}
            />
            {isEtf ? (
              <StatCard
                label={hasShare ? '国家队份额记录' : '数据表来源'}
                value={
                  hasShare
                    ? (data.periods.find((p) => p.period === 'share')?.total_bars ?? 0).toLocaleString()
                    : 'etf_kline_daily'
                }
                hint={
                  hasShare
                    ? data.periods.find((p) => p.period === 'share')?.max_time || '—'
                    : '跨资产前复权日线'
                }
              />
            ) : (
              <StatCard
                label="5m 总根数"
                value={(data.periods.find((p) => p.period === '5m')?.total_bars ?? 0).toLocaleString()}
                hint={data.periods.find((p) => p.period === '5m')?.max_time || '—'}
              />
            )}
          </div>

          {/* 周期汇总 */}
          <Card>
            <SectionTitle title="历史覆盖与近 N 日达标率" />
            <Table dense>
              <TableHead>
                <TableRow>
                  <TableHeader>周期 / 数据表</TableHeader>
                  <TableHeader className="text-right">每日标准槽</TableHeader>
                  <TableHeader className="text-right">历史总根数</TableHeader>
                  <TableHeader>最早记录</TableHeader>
                  <TableHeader>最新记录</TableHeader>
                  <TableHeader className="text-right">近期达标</TableHeader>
                  <TableHeader>结论</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {data.periods.map((p) => (
                  <TableRow key={p.period}>
                    <TableCell className="font-medium text-white">{p.label}</TableCell>
                    <TableCell className="text-right font-mono text-zinc-300">{p.expected_per_day}</TableCell>
                    <TableCell className="text-right font-mono text-zinc-200">{p.total_bars.toLocaleString()}</TableCell>
                    <TableCell className="font-mono text-xs text-zinc-400">{p.min_time || '—'}</TableCell>
                    <TableCell className="font-mono text-xs text-emerald-300">{p.max_time || '—'}</TableCell>
                    <TableCell className="text-right font-mono text-zinc-200">
                      {p.recent_ok_days} / {p.recent_total_days}
                    </TableCell>
                    <TableCell>
                      <Badge color={p.complete ? 'emerald' : 'amber'}>
                        {p.complete ? '完整闭合' : '存在缺槽'}
                      </Badge>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>

          {/* 逐日槽位明细 */}
          <Card>
            <SectionTitle title={`近 ${data.days} 个交易日逐日槽位明细`} />
            <Table dense>
              <TableHead>
                <TableRow>
                  <TableHeader>交易日</TableHeader>
                  <TableHeader className="text-right">日线 (应1)</TableHeader>
                  {isEtf ? (
                    hasShare && <TableHeader className="text-right">国家队份额 (T+1 应1)</TableHeader>
                  ) : (
                    <>
                      <TableHeader className="text-right">5m (应48)</TableHeader>
                      <TableHeader className="text-right">15m (应16)</TableHeader>
                      <TableHeader className="text-right">30m (应8)</TableHeader>
                      <TableHeader className="text-right">60m (应4)</TableHeader>
                    </>
                  )}
                  <TableHeader>当日状态</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {data.daily_rows.map((r) => (
                  <TableRow key={r.date}>
                    <TableCell className="font-mono text-zinc-100">{r.date}</TableCell>
                    <TableCell className="text-right font-mono text-zinc-200">{r.daily}</TableCell>
                    {isEtf ? (
                      hasShare && (
                        <TableCell className="text-right font-mono text-zinc-200">
                          {r.share ?? '—'}
                        </TableCell>
                      )
                    ) : (
                      <>
                        <TableCell className="text-right font-mono text-zinc-200">{r['5m'] ?? 0}</TableCell>
                        <TableCell className="text-right font-mono text-zinc-200">{r['15m'] ?? 0}</TableCell>
                        <TableCell className="text-right font-mono text-zinc-200">{r['30m'] ?? 0}</TableCell>
                        <TableCell className="text-right font-mono text-zinc-200">{r['60m'] ?? 0}</TableCell>
                      </>
                    )}
                    <TableCell>
                      <Badge color={r.all_ok ? 'emerald' : 'red'}>
                        {r.all_ok ? '闭合' : '缺槽'}
                      </Badge>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>
        </>
      )}
    </div>
  )
}
