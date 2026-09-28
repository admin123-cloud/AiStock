import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { getSectors, getSectorsStats, getSectorHistoricalStats } from '@/api/market'
import { Card, EmptyState, PageHeader, PageLoading, StatCard } from '@/components/ui'
import { Button } from '@/components/catalyst/button'
import { Input } from '@/components/catalyst/input'
import { Select } from '@/components/catalyst/select'

interface HistoricalStat {
  rise_days: number
  total_days: number
  rise_ratio: number
}

interface SectorItem {
  code: string
  name: string
  type?: string
  level?: number | null
  change_pct?: number | null
  rise_count?: number | null
  fall_count?: number | null
  stock_count?: number | null
  rise_ratio?: number | null
  limit_up_count?: number | null
  limit_down_count?: number | null
}

type SectorHistoricalStats = Partial<Record<'stats_5d' | 'stats_10d' | 'stats_15d', HistoricalStat>>

function getTypeLabel(type?: string, level?: number | null): string {
  const labels: Record<string, string> = { industry: '行业', concept: '概念', index: '指数' }
  const base = labels[type || ''] || type || ''
  if (type === 'industry' && level) return `${level}级${base}`
  return base
}

function formatChangePct(value?: number | null): string {
  if (value === null || value === undefined) return '--'
  return `${value > 0 ? '+' : ''}${value}%`
}

function changeClass(value?: number | null): string {
  if (value === null || value === undefined) return 'bg-zinc-100 text-zinc-600 dark:bg-white/10 dark:text-zinc-300'
  if (value > 0) return 'bg-red-100 text-red-600 dark:bg-red-500/15 dark:text-red-400'
  if (value < 0) return 'bg-green-100 text-green-600 dark:bg-green-500/15 dark:text-green-500'
  return 'bg-zinc-100 text-zinc-600 dark:bg-white/10 dark:text-zinc-300'
}

function ratioClass(riseRatio?: number | null): string {
  if (riseRatio === null || riseRatio === undefined) return ''
  if (riseRatio >= 70) return 'text-red-600 dark:text-red-400'
  if (riseRatio >= 50) return 'text-orange-600 dark:text-orange-400'
  if (riseRatio >= 30) return 'text-zinc-600 dark:text-zinc-300'
  return 'text-green-600 dark:text-green-500'
}

const SORT_OPTIONS = [
  { value: 'rise_ratio_desc', label: '今日涨幅（降序）' },
  { value: 'rise_ratio_asc', label: '今日涨幅（升序）' },
  { value: 'stats_15d_rise_ratio_desc', label: '15日涨幅（降序）' },
  { value: 'stats_15d_rise_ratio_asc', label: '15日涨幅（升序）' }
]

const TYPE_OPTIONS = [
  { value: '', label: '全部' },
  { value: 'industry_level1', label: '一级行业' },
  { value: 'industry_level2', label: '二级行业' },
  { value: 'industry_level3', label: '三级行业' },
  { value: 'hot', label: '热点题材' }
]

function HistoricalRow({ label, stat }: { label: string; stat?: HistoricalStat }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-xs font-medium text-zinc-500 dark:text-zinc-400">{label}:</span>
      <span className="text-xs font-semibold tabular-nums">
        {stat ? (
          <>
            <span className="text-red-600 dark:text-red-400">{stat.rise_days}</span>
            {' / '}
            <span className="text-green-600 dark:text-green-500">{stat.total_days - stat.rise_days}</span>
            {' / '}
            <span className="text-zinc-600 dark:text-zinc-300">{stat.total_days}</span>
            <span className="ml-1 text-[11px] text-zinc-500 dark:text-zinc-400">({stat.rise_ratio}%)</span>
          </>
        ) : (
          <span className="text-zinc-500 dark:text-zinc-400">--/--/-- (--%)</span>
        )}
      </span>
    </div>
  )
}

export default function Sectors() {
  const navigate = useNavigate()

  const [sectors, setSectors] = useState<SectorItem[]>([])
  const [hotSectors, setHotSectors] = useState<SectorItem[]>([])
  const [loading, setLoading] = useState(false)
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(12)
  const [type, setType] = useState('')
  const [search, setSearch] = useState('')
  const [sortBy, setSortBy] = useState('rise_ratio_desc')
  const [historicalStats, setHistoricalStats] = useState<Record<string, SectorHistoricalStats>>({})

  const queryRef = useRef({ type, search, sortBy, page, pageSize })
  queryRef.current = { type, search, sortBy, page, pageSize }

  const loadStats = useCallback(async () => {
    try {
      await getSectorsStats()
    } catch {
      // 统计信息失败不影响列表
    }
  }, [])

  const loadSectors = useCallback(async (overrides?: { type?: string; search?: string; sortBy?: string; page?: number; pageSize?: number }) => {
    const { type: t, search: s, sortBy: sb, page: p, pageSize: ps } = {
      ...queryRef.current,
      ...overrides
    }
    setLoading(true)
    try {
      const params: Record<string, string | number> = { page: p, page_size: ps }
      if (t === 'hot') {
        // 热点题材筛选，在客户端处理
      } else if (t.startsWith('industry_level')) {
        params.sector_type = 'industry'
        params.level = parseInt(t.replace('industry_level', ''), 10)
      } else if (t) {
        params.sector_type = t
      }
      const trimmed = s?.trim()
      if (trimmed) params.search = trimmed
      if (sb) params.sort_by = sb

      const data = (await getSectors(params, { skipErrorHandler: true })) as {
        items?: SectorItem[]
        total?: number
      }

      if (data && Array.isArray(data.items)) {
        let items = data.items
        let newTotal = data.total || 0

        // 并行加载每页板块的历史上涨率数据
        const statsResults = await Promise.all(
          items.map((sector) =>
            getSectorHistoricalStats(sector.code, { skipErrorHandler: true })
              .then((res) => [sector.code, res] as const)
              .catch(() => [sector.code, null] as const)
          )
        )
        const statsMap: Record<string, SectorHistoricalStats> = {}
        for (const [code, res] of statsResults) {
          if (res) statsMap[code] = res as SectorHistoricalStats
        }
        setHistoricalStats((prev) => ({ ...prev, ...statsMap }))

        // 热点题材：当天板块上涨比例超过70%
        const hot = items.filter(
          (sector) => sector.rise_ratio !== undefined && sector.rise_ratio !== null && sector.rise_ratio > 70
        )
        setHotSectors(hot)

        if (t === 'hot') {
          items = hot
          newTotal = hot.length
        }

        // 客户端按上涨比例 / 15日涨幅排序
        if (sb.startsWith('rise_ratio_')) {
          const key = sb === 'rise_ratio_desc' ? -1 : 1
          items = [...items].sort((a, b) => ((a.rise_ratio ?? -1) - (b.rise_ratio ?? -1)) * key)
        } else if (sb.startsWith('stats_15d_rise_ratio_')) {
          const key = sb === 'stats_15d_rise_ratio_desc' ? -1 : 1
          items = [...items].sort(
            (a, b) =>
              ((statsMap[a.code]?.stats_15d?.rise_ratio ?? -1) -
                (statsMap[b.code]?.stats_15d?.rise_ratio ?? -1)) *
              key
          )
        }

        setSectors(items)
        setTotal(newTotal)
      } else {
        setSectors([])
        setHotSectors([])
        setTotal(0)
      }
    } catch {
      setSectors([])
      setHotSectors([])
      setTotal(0)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadSectors()
    loadStats()
  }, [loadSectors, loadStats, page, pageSize, type])

  // 搜索防抖（500ms，与旧版一致）
  const searchTimer = useRef<number | null>(null)
  const handleSearchInput = (value: string) => {
    setSearch(value)
    if (searchTimer.current !== null) window.clearTimeout(searchTimer.current)
    searchTimer.current = window.setTimeout(() => {
      if (queryRef.current.page === 1) loadSectors({ search: value })
      else setPage(1)
    }, 500)
  }
  useEffect(() => {
    return () => {
      if (searchTimer.current !== null) window.clearTimeout(searchTimer.current)
    }
  }, [])

  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  const refreshData = () => {
    if (queryRef.current.page === 1) {
      loadSectors()
      loadStats()
    } else {
      setPage(1)
    }
  }

  const filterByType = (value: string) => {
    setType(value)
    setSortBy('rise_ratio_desc')
    setPage(1)
  }

  return (
    <div className="mx-auto max-w-[1400px] space-y-5 p-5">
      {/* 页头 */}
      <PageHeader
        title="板块监控"
        subtitle="实时监控各行业板块和概念题材"
        actions={<Button onClick={refreshData}>刷新</Button>}
      />
      {/* 热点题材统计卡片 */}
      {hotSectors.length > 0 && (
        <button
          type="button"
          onClick={() => filterByType('hot')}
          className={`w-full max-w-xs cursor-pointer rounded-lg text-left transition hover:-translate-y-0.5 ${
            type === 'hot' ? 'ring-2 ring-red-500 dark:ring-red-400' : ''
          }`}
        >
          <StatCard
            label="热点题材"
            value={hotSectors.length}
            tone="up"
            hint="当日上涨比例超 70% 的板块，点击筛选"
          />
        </button>
      )}

      {/* 筛选区 */}
      <Card className="flex flex-wrap items-center gap-4">
        <label className="flex items-center gap-2 text-sm font-medium text-zinc-700 dark:text-zinc-300">
          类型:
          <Select
            value={type}
            onChange={(e) => { setType(e.target.value); setPage(1) }}
            className="w-36"
          >
            {TYPE_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </Select>
        </label>
        <label className="flex items-center gap-2 text-sm font-medium text-zinc-700 dark:text-zinc-300">
          排序:
          <Select value={sortBy} onChange={(e) => { setSortBy(e.target.value); loadSectors({ sortBy: e.target.value }) }} className="w-48">
            {SORT_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </Select>
        </label>
        <label className="flex items-center gap-2 text-sm font-medium text-zinc-700 dark:text-zinc-300">
          搜索:
          <Input
            value={search}
            placeholder="代码或名称"
            onChange={(e) => handleSearchInput(e.target.value)}
            className="w-44"
          />
        </label>
      </Card>

      {/* 板块卡片网格 */}
      {loading ? (
        <Card>
          <PageLoading />
        </Card>
      ) : sectors.length === 0 ? (
        <Card>
          <EmptyState text="暂无板块数据" />
        </Card>
      ) : (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(280px,1fr))] gap-4">
          {sectors.map((sector) => (
            <button
              type="button"
              key={sector.code}
              onClick={() => navigate(`/sector/${sector.code}`)}
              className="cursor-pointer rounded-lg bg-white p-5 text-left ring-1 ring-zinc-950/10 transition hover:-translate-y-1 hover:shadow-lg dark:bg-zinc-900 dark:ring-white/10"
            >
              <div className="mb-3 flex items-center justify-between">
                <span
                  className={`rounded px-2 py-0.5 text-xs font-medium ${
                    sector.type === 'industry'
                      ? 'bg-pink-100 text-pink-700 dark:bg-pink-500/15 dark:text-pink-300'
                      : sector.type === 'concept'
                        ? 'bg-cyan-100 text-cyan-700 dark:bg-cyan-500/15 dark:text-cyan-300'
                        : 'bg-amber-100 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300'
                  }`}
                >
                  {getTypeLabel(sector.type, sector.level)}
                </span>
                <span className={`rounded px-2 py-0.5 text-[13px] font-semibold tabular-nums ${changeClass(sector.change_pct)}`}>
                  {formatChangePct(sector.change_pct)}
                </span>
              </div>

              <div className="mb-3 flex items-center justify-between gap-2">
                <span className="flex-1 truncate text-base font-semibold text-zinc-900 dark:text-white">
                  {sector.name}
                </span>
                <span className="shrink-0 font-mono text-[13px] text-zinc-500 dark:text-zinc-400">
                  {sector.code}
                </span>
              </div>

              {/* 上涨率统计 */}
              <div className="mb-3 space-y-1.5 rounded-lg bg-zinc-50 p-2.5 dark:bg-white/5">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-medium text-zinc-500 dark:text-zinc-400">今日:</span>
                  <span className={`text-xs font-semibold tabular-nums ${ratioClass(sector.rise_ratio)}`}>
                    <span className="text-red-600 dark:text-red-400">{sector.rise_count || 0}</span>
                    {' / '}
                    <span className="text-green-600 dark:text-green-500">{sector.fall_count || 0}</span>
                    {' / '}
                    <span className="text-zinc-600 dark:text-zinc-300">{sector.stock_count || 0}</span>
                    <span className="ml-1 text-[11px] text-zinc-500 dark:text-zinc-400">
                      ({sector.rise_ratio !== undefined && sector.rise_ratio !== null
                        ? sector.rise_ratio.toFixed(2) + '%'
                        : 'N/A'})
                    </span>
                  </span>
                </div>
                <HistoricalRow label="5日" stat={historicalStats[sector.code]?.stats_5d} />
                <HistoricalRow label="10日" stat={historicalStats[sector.code]?.stats_10d} />
                <HistoricalRow label="15日" stat={historicalStats[sector.code]?.stats_15d} />
              </div>

              {/* 涨跌停统计 */}
              {(sector.limit_up_count !== undefined || sector.limit_down_count !== undefined) && (
                <div className="mb-2 space-y-1.5 rounded-lg bg-zinc-50 p-3 dark:bg-white/5">
                  <div className="flex items-center justify-between">
                    <span className="text-xs text-zinc-500 dark:text-zinc-400">涨停:</span>
                    <span className="text-[13px] font-bold text-red-600 dark:text-red-400 tabular-nums">
                      {sector.limit_up_count || 0}
                    </span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-xs text-zinc-500 dark:text-zinc-400">跌停:</span>
                    <span className="text-[13px] font-semibold text-zinc-700 dark:text-zinc-200 tabular-nums">
                      {sector.limit_down_count || 0}
                    </span>
                  </div>
                </div>
              )}

              <div className="flex items-center justify-between">
                <span className="text-[13px] text-zinc-500 dark:text-zinc-400">
                  {sector.stock_count || 0} 只成分股
                </span>
              </div>
            </button>
          ))}
        </div>
      )}

      {/* 分页 */}
      {total > 0 && (
        <Card className="flex items-center justify-center gap-4">
          <Button outline disabled={page === 1} onClick={() => setPage(page - 1)}>
            上一页
          </Button>
          <span className="text-sm text-zinc-600 dark:text-zinc-300">
            第 {page} 页 / 共 {totalPages} 页 ({total} 条)
          </span>
          <Button outline disabled={page === totalPages} onClick={() => setPage(page + 1)}>
            下一页
          </Button>
          <Select
            value={String(pageSize)}
            onChange={(e) => { setPageSize(Number(e.target.value)); setPage(1) }}
            className="w-32"
          >
            <option value="12">12条/页</option>
            <option value="20">20条/页</option>
            <option value="50">50条/页</option>
            <option value="100">100条/页</option>
          </Select>
        </Card>
      )}
    </div>
  )
}
