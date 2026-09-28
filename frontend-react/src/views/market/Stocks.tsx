import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { getStockList, getStocksWithLimit } from '@/api/stock'
import { Button } from '@/components/catalyst/button'
import { Input } from '@/components/catalyst/input'
import { Select } from '@/components/catalyst/select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow
} from '@/components/catalyst/table'
import { Card, ChangeValue, EmptyState, PageHeader, PageLoading } from '@/components/ui'

interface LatestKline {
  close?: number | string | null
  change_pct?: number | string | null
  amount?: number | string | null
}

interface StockItem {
  code: string
  name: string
  market?: string
  type?: string
  latest_kline?: LatestKline | null
}

const MARKET_LABELS: Record<string, string> = { sh: '上海', sz: '深圳', bj: '北京' }
const TYPE_LABELS: Record<string, string> = {
  stock: '股票',
  index: '指数',
  industry: '行业',
  sector: '板块'
}

function formatPrice(price: number | string | null | undefined): string {
  if (price === null || price === undefined) return '-'
  return Number(price).toFixed(2)
}

function formatAmount(amount: number | string | null | undefined): string {
  if (amount === null || amount === undefined) return '-'
  const value = Number(amount)
  if (!Number.isFinite(value)) return '-'
  // 股票列表接口返回的成交额单位为元，直接按元换算展示。
  if (value >= 100000000) return (value / 100000000).toFixed(2) + '亿'
  if (value >= 10000) return (value / 10000).toFixed(2) + '万'
  return value.toFixed(2)
}

type SortKey = 'code' | 'name' | 'market' | 'type' | 'close' | 'change_pct' | 'amount'

export default function Stocks() {
  const navigate = useNavigate()

  const [stocks, setStocks] = useState<StockItem[]>([])
  const [loading, setLoading] = useState(false)
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [market, setMarket] = useState('')
  const [stockType, setStockType] = useState('')
  const [search, setSearch] = useState('')
  const [sortKey, setSortKey] = useState<SortKey | null>(null)
  const [sortAsc, setSortAsc] = useState(true)

  // 筛选条件需要参与请求，用 ref 保存最新值供轮询读取
  const queryRef = useRef({ market, stockType, search, page, pageSize })
  queryRef.current = { market, stockType, search, page, pageSize }

  const loadStocks = useCallback(async (overrides?: { market?: string; stockType?: string; search?: string; page?: number; pageSize?: number }) => {
    const { market: m, stockType: t, search: s, page: p, pageSize: ps } = {
      ...queryRef.current,
      ...overrides
    }
    setLoading(true)
    try {
      const params: Record<string, string | number> = {
        page: p,
        page_size: ps,
        // 默认只查询股票类型，不包含指数
        stock_type: 'stock'
      }
      if (m) params.market = m
      if (t) params.stock_type = t
      const trimmed = s?.trim()
      if (trimmed) params.search = trimmed

      const data = (await getStocksWithLimit(params, { skipErrorHandler: true })) as
        | { items?: StockItem[]; total?: number }
        | StockItem[]

      if (data && !Array.isArray(data) && Array.isArray(data.items)) {
        setStocks(data.items)
        setTotal(data.total || 0)
      } else {
        setStocks([])
        setTotal(0)
      }
    } catch {
      // 备用 API：/stocks?skip&limit
      const { market: m, stockType: t, search: s, page: p, pageSize: ps } = queryRef.current
      try {
        const params2: Record<string, string | number> = {
          skip: (p - 1) * ps,
          limit: ps,
          stock_type: 'stock'
        }
        const trimmed = s?.trim()
        if (m) params2.market = m
        if (t) params2.stock_type = t
        if (trimmed) params2.search = trimmed
        const data2 = (await getStockList(params2)) as unknown as StockItem[]
        if (Array.isArray(data2)) {
          setStocks(data2)
          setTotal(data2.length)
        } else {
          setStocks([])
          setTotal(0)
        }
      } catch {
        setStocks([])
        setTotal(0)
      }
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadStocks()
    // 列表由5分钟K线驱动，页面打开期间自动保持新鲜
    const timer = window.setInterval(loadStocks, 60 * 1000)
    return () => window.clearInterval(timer)
  }, [loadStocks, page, pageSize, market, stockType])

  // 搜索防抖（500ms，与旧版一致）
  const searchTimer = useRef<number | null>(null)
  const handleSearchInput = (value: string) => {
    setSearch(value)
    if (searchTimer.current !== null) window.clearTimeout(searchTimer.current)
    searchTimer.current = window.setTimeout(() => {
      // search 不在 effect 依赖里：page 已是 1 时手动带最新 search 请求，否则仅重置 page 由 effect 触发
      if (queryRef.current.page === 1) loadStocks({ page: 1, search: value })
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
    // page 已是 1 时手动刷新，否则重置 page 由 effect 触发
    if (queryRef.current.page === 1) loadStocks()
    else setPage(1)
  }

  const changePage = (newPage: number) => {
    if (newPage >= 1 && newPage <= totalPages) setPage(newPage)
  }

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      if (sortAsc) setSortAsc(false)
      else setSortKey(null)
    } else {
      setSortKey(key)
      setSortAsc(true)
    }
  }

  const sortIndicator = (key: SortKey) =>
    sortKey === key ? (sortAsc ? ' ↑' : ' ↓') : ''

  const sortedStocks = [...stocks].sort((a, b) => {
    if (!sortKey) return 0
    let va: string | number = ''
    let vb: string | number = ''
    if (sortKey === 'close' || sortKey === 'change_pct' || sortKey === 'amount') {
      const field = sortKey as 'close' | 'change_pct' | 'amount'
      va = a.latest_kline ? Number(a.latest_kline[field] ?? -Infinity) : -Infinity
      vb = b.latest_kline ? Number(b.latest_kline[field] ?? -Infinity) : -Infinity
    } else if (sortKey === 'market') {
      va = MARKET_LABELS[a.market || ''] || a.market || ''
      vb = MARKET_LABELS[b.market || ''] || b.market || ''
    } else if (sortKey === 'type') {
      va = TYPE_LABELS[a.type || ''] || a.type || ''
      vb = TYPE_LABELS[b.type || ''] || b.type || ''
    } else {
      va = a.code
      vb = b.code
      if (sortKey === 'name') {
        va = a.name
        vb = b.name
      }
    }
    const cmp =
      typeof va === 'number' && typeof vb === 'number'
        ? va - vb
        : String(va).localeCompare(String(vb), 'zh')
    return sortAsc ? cmp : -cmp
  })

  return (
    <div className="mx-auto max-w-[1400px] space-y-5 p-5">
      {/* 页头 */}
      <PageHeader
        title="股票列表"
        subtitle="查看所有A股股票的基本信息和行情数据"
        actions={<Button onClick={refreshData}>刷新</Button>}
      />

      {/* 筛选区 */}
      <Card className="flex flex-wrap items-center gap-4 p-5">
        <label className="flex items-center gap-2 text-sm font-medium text-zinc-700 dark:text-zinc-300">
          市场:
          <Select value={market} onChange={(e) => { setMarket(e.target.value); setPage(1) }} className="w-32">
            <option value="">全部</option>
            <option value="sh">上海</option>
            <option value="sz">深圳</option>
            <option value="bj">北京</option>
          </Select>
        </label>
        <label className="flex items-center gap-2 text-sm font-medium text-zinc-700 dark:text-zinc-300">
          类型:
          <Select value={stockType} onChange={(e) => { setStockType(e.target.value); setPage(1) }} className="w-32">
            <option value="">全部</option>
            <option value="stock">股票</option>
            <option value="index">指数</option>
            <option value="industry">行业</option>
            <option value="sector">板块</option>
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
        <Button onClick={refreshData}>刷新</Button>
      </Card>

      {/* 表格 */}
      <Card className="p-5">
        {loading ? (
          <PageLoading />
        ) : stocks.length === 0 ? (
          <EmptyState />
        ) : (
          <Table bleed striped>
            <TableHead>
              <TableRow>
                <TableHeader className="cursor-pointer select-none" onClick={() => handleSort('code')}>
                  代码{sortIndicator('code')}
                </TableHeader>
                <TableHeader className="cursor-pointer select-none" onClick={() => handleSort('name')}>
                  名称{sortIndicator('name')}
                </TableHeader>
                <TableHeader className="cursor-pointer select-none" onClick={() => handleSort('market')}>
                  市场{sortIndicator('market')}
                </TableHeader>
                <TableHeader className="cursor-pointer select-none" onClick={() => handleSort('type')}>
                  类型{sortIndicator('type')}
                </TableHeader>
                <TableHeader className="cursor-pointer select-none text-right" onClick={() => handleSort('close')}>
                  最新价{sortIndicator('close')}
                </TableHeader>
                <TableHeader className="cursor-pointer select-none text-right" onClick={() => handleSort('change_pct')}>
                  涨跌幅{sortIndicator('change_pct')}
                </TableHeader>
                <TableHeader className="cursor-pointer select-none text-right" onClick={() => handleSort('amount')}>
                  成交额{sortIndicator('amount')}
                </TableHeader>
                <TableHeader>操作</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {sortedStocks.map((stock) => (
                <TableRow key={stock.code}>
                  <TableCell className="tabular-nums">{stock.code}</TableCell>
                  <TableCell>{stock.name}</TableCell>
                  <TableCell>{MARKET_LABELS[stock.market || ''] || stock.market}</TableCell>
                  <TableCell>{TYPE_LABELS[stock.type || ''] || stock.type}</TableCell>
                  <TableCell className="text-right font-semibold tabular-nums">
                    {stock.latest_kline ? formatPrice(stock.latest_kline.close) : '-'}
                  </TableCell>
                  <TableCell className="text-right font-semibold">
                    {stock.latest_kline ? (
                      <ChangeValue value={Number(stock.latest_kline.change_pct)} />
                    ) : (
                      '-'
                    )}
                  </TableCell>
                  <TableCell className="text-right font-semibold tabular-nums">
                    {stock.latest_kline ? formatAmount(stock.latest_kline.amount) : '-'}
                  </TableCell>
                  <TableCell>
                    <Button outline onClick={() => navigate(`/stock/${stock.code}`)}>
                      详情
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Card>

      {/* 分页 */}
      {total > 0 && (
        <Card className="flex flex-wrap items-center justify-center gap-4 p-4">
          <Button outline disabled={page === 1} onClick={() => changePage(page - 1)}>
            上一页
          </Button>
          <span className="text-sm text-zinc-600 dark:text-zinc-300">
            第 {page} 页 / 共 {totalPages} 页 ({total} 条)
          </span>
          <Button outline disabled={page === totalPages} onClick={() => changePage(page + 1)}>
            下一页
          </Button>
          <Select
            value={String(pageSize)}
            onChange={(e) => { setPageSize(Number(e.target.value)); setPage(1) }}
            className="w-32"
          >
            <option value="20">20条/页</option>
            <option value="50">50条/页</option>
            <option value="100">100条/页</option>
          </Select>
        </Card>
      )}
    </div>
  )
}
