import React, { useEffect, useState } from 'react'
import { getSectorHeat, getSectorHeatStocks } from '@/api/marketBoard'
import { Button } from '@/components/catalyst/button'
import { Badge } from '@/components/catalyst/badge'

interface SectorItem {
  code: string
  name: string
  type: string
  change_pct: number | null
  price: number | null
  up_count: number
  down_count: number
  rise_count: number
  fall_count: number
  total_count: number | null
}

interface SectorHeatProps {
  boardType?: 'concept' | 'industry' | 'region'
  compact?: boolean
}

function ChangePct({ value }: { value: number | null }) {
  if (value === null || value === undefined) return <span className="text-zinc-500">-</span>
  const isUp = value >= 0
  const color = value >= 9.5 ? '#ef4444' : value >= 5 ? '#f97316' : value >= 0 ? '#22c55e' : '#22c55e'
  return (
    <span className={`font-mono font-medium ${isUp ? 'text-red-400' : 'text-green-400'}`}>
      {isUp ? '+' : ''}{value.toFixed(2)}%
    </span>
  )
}

export default function SectorHeatPanel({ boardType, compact }: SectorHeatProps) {
  const [sectors, setSectors] = useState<SectorItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expandedCode, setExpandedCode] = useState<string | null>(null)
  const [stocks, setStocks] = useState<any[]>([])
  const [stocksLoading, setStocksLoading] = useState(false)
  const [tab, setTab] = useState<any>(boardType || 'concept')

  useEffect(() => {
    setLoading(true)
    setError(null)
    getSectorHeat({ board_type: tab, top_n: compact ? 10 : 20, sort_by: 'change_pct' })
      .then((res: any) => {
        setSectors(res?.items || [])
      })
      .catch((err) => {
        setError(err?.detail || '数据加载失败')
        setSectors([])
      })
      .finally(() => setLoading(false))
  }, [tab, compact])

  const handleExpand = async (code: string) => {
    if (expandedCode === code) {
      setExpandedCode(null)
      setStocks([])
      return
    }
    setExpandedCode(code)
    setStocksLoading(true)
    try {
      const res: any = await getSectorHeatStocks(code)
      setStocks(res?.stocks || [])
    } catch {
      setStocks([])
    } finally {
      setStocksLoading(false)
    }
  }

  return (
    <div className="space-y-2">
      {/* 标签切换 */}
      {!boardType && (
        <div className="flex gap-1 border-b border-zinc-700 pb-1">
          <Button
            outline={tab !== 'concept' as any}
            onClick={() => setTab('concept')}
            className={`px-3 py-1 text-xs ${tab === 'concept' ? '' : 'opacity-60'}`}
          >
            概念
          </Button>
          <Button
            outline={tab !== 'industry' as any}
            onClick={() => setTab('industry')}
            className={`px-3 py-1 text-xs ${tab === 'industry' ? '' : 'opacity-60'}`}
          >
            行业
          </Button>
        </div>
      )}

      {loading && <div className="py-8 text-center text-zinc-500 text-xs">加载中…</div>}
      {error && <div className="py-8 text-center text-red-400 text-xs">{error}</div>}
      {!loading && !error && sectors.length === 0 && (
        <div className="py-8 text-center text-zinc-500 text-xs">暂无数据</div>
      )}

      {/* 板块列表 */}
      <div className="space-y-1 max-h-96 overflow-y-auto">
        {sectors.map((s) => (
          <div key={s.code}>
            <div
              className={`flex items-center justify-between rounded px-2 py-1.5 text-xs cursor-pointer transition-colors
                ${expandedCode === s.code ? 'bg-zinc-700/50' : 'hover:bg-zinc-800'}`}
              onClick={() => handleExpand(s.code)}
            >
              <div className="flex items-center gap-2 min-w-0 flex-1">
                <span className="truncate font-medium text-zinc-200">{s.name}</span>
                <Badge className="text-[10px] px-1 py-0 bg-zinc-700 text-zinc-400">
                  {s.total_count || '-'}
                </Badge>
              </div>
              <div className="flex items-center gap-3 ml-2 shrink-0">
                <span className="text-zinc-500 text-[10px]">
                  {s.up_count}/{s.down_count}
                </span>
                <ChangePct value={s.change_pct} />
              </div>
            </div>

            {/* 成分股钻取 */}
            {expandedCode === s.code && (
              <div className="ml-3 border-l border-zinc-700 pl-2 py-1 space-y-0.5 max-h-40 overflow-y-auto">
                {stocksLoading && <span className="text-zinc-500 text-[10px]">加载成分股…</span>}
                {!stocksLoading &&
                  stocks.slice(0, 30).map((st: any) => (
                    <div key={st.code} className="flex items-center justify-between text-[10px] py-0.5">
                      <div className="flex items-center gap-2">
                        <span className="text-zinc-400">{st.code}</span>
                        <span className="text-zinc-300">{st.name}</span>
                      </div>
                      <ChangePct value={st.change_pct} />
                    </div>
                  ))}
                {!stocksLoading && stocks.length > 30 && (
                  <div className="text-zinc-500 text-[10px]">…还有 {stocks.length - 30} 只</div>
                )}
              </div>
            )}
          </div>
        ))}
      </div>

      <div className="text-[10px] text-zinc-600 text-right">东财数据 | 10s 缓存</div>
    </div>
  )
}