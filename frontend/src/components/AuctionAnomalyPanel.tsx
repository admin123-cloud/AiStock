import React, { useEffect, useState } from 'react'
import { getAuctionAnomaly } from '@/api/marketBoard'

interface AnomalyItem {
  code: string
  name: string
  gap_pct_920: number | null
  gap_pct_924: number | null
  gap_delta: number | null
  vol_920: number
  vol_924: number
  vol_ratio_30d: number | null
}

interface AnomalySummary {
  total_merged: number
  gap_ge_5pct: number
  gap_3_to_5pct: number
  vol_spike_ge_2x: number
  top_gap_up: AnomalyItem[]
  top_vol_spike: AnomalyItem[]
}

interface AuctionData {
  date: string
  available: boolean
  message?: string
  snap1_920?: AnomalyItem[]
  snap2_924?: AnomalyItem[]
  anomaly_summary?: AnomalySummary
}

export default function AuctionAnomalyPanel() {
  const [data, setData] = useState<AuctionData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [view, setView] = useState<'gap' | 'vol'>('gap')

  const fetchData = () => {
    setLoading(true)
    setError(null)
    getAuctionAnomaly()
      .then((res: any) => setData(res))
      .catch((err) => setError(err?.detail || '加载失败'))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    fetchData()
  }, [])

  if (loading) {
    return <div className="py-8 text-center text-zinc-500 text-xs">加载竞价异动…</div>
  }

  if (error) {
    return (
      <div className="py-4 text-center">
        <div className="text-red-400 text-xs mb-2">{error}</div>
        <button onClick={fetchData} className="text-[10px] text-blue-400 underline">
          重试
        </button>
      </div>
    )
  }

  if (!data || !data.available) {
    return (
      <div className="py-8 text-center text-zinc-500 text-xs">
        {data?.message || '竞价快照暂不可用（非交易日或尚未采集）'}
      </div>
    )
  }

  const summary = data.anomaly_summary
  const items = view === 'gap' ? summary?.top_gap_up || [] : summary?.top_vol_spike || []

  return (
    <div className="space-y-2">
      {/* 统计摘要条 */}
      {summary && (
        <div className="flex gap-2 text-[10px]">
          <span className="rounded bg-zinc-700/50 px-2 py-0.5 text-zinc-300">
            高开≥5%: <span className="text-red-400 font-medium">{summary.gap_ge_5pct}</span>
          </span>
          <span className="rounded bg-zinc-700/50 px-2 py-0.5 text-zinc-300">
            高开3~5%: <span className="text-orange-400 font-medium">{summary.gap_3_to_5pct}</span>
          </span>
          <span className="rounded bg-zinc-700/50 px-2 py-0.5 text-zinc-300">
            竞价量≥2x: <span className="text-yellow-400 font-medium">{summary.vol_spike_ge_2x}</span>
          </span>
        </div>
      )}

      {/* 对比卡插：gap vs vol */}
      <div className="flex gap-2 text-[10px]">
        <button
          className={`px-2 py-0.5 rounded ${view === 'gap' ? 'bg-zinc-600 text-zinc-100' : 'bg-zinc-800 text-zinc-500'}`}
          onClick={() => setView('gap')}
        >
          高开异动
        </button>
        <button
          className={`px-2 py-0.5 rounded ${view === 'vol' ? 'bg-zinc-600 text-zinc-100' : 'bg-zinc-800 text-zinc-500'}`}
          onClick={() => setView('vol')}
        >
          放量异动
        </button>
      </div>

      {/* 高开异动榜 */}
      {view === 'gap' && (
        <div className="space-y-1 max-h-48 overflow-y-auto">
          {items.map((item) => (
            <div key={item.code} className="flex items-center justify-between rounded px-2 py-1 text-[11px] hover:bg-zinc-800">
              <div className="flex items-center gap-2 min-w-0 flex-1">
                <span className="font-medium text-zinc-200 truncate">{item.name}</span>
                <span className="text-zinc-500 shrink-0">{item.code}</span>
              </div>
              <div className="flex items-center gap-2 shrink-0 ml-2">
                <span className="text-zinc-500 text-[10px]">
                  {item.gap_pct_920?.toFixed(1)}→{item.gap_pct_924?.toFixed(1)}
                </span>
                {item.gap_delta !== null && (
                  <span className={`font-mono ${item.gap_delta >= 0 ? 'text-red-400' : 'text-green-400'}`}>
                    {item.gap_delta >= 0 ? '+' : ''}{item.gap_delta.toFixed(2)}
                  </span>
                )}
              </div>
            </div>
          ))}
          {items.length === 0 && (
            <div className="text-zinc-500 text-[10px] py-2 text-center">无显著高开异动</div>
          )}
        </div>
      )}

      {/* 放量异动榜 */}
      {view === 'vol' && (
        <div className="space-y-1 max-h-48 overflow-y-auto">
          {items.map((item) => (
            <div key={item.code} className="flex items-center justify-between rounded px-2 py-1 text-[11px] hover:bg-zinc-800">
              <div className="flex items-center gap-2 min-w-0 flex-1">
                <span className="font-medium text-zinc-200 truncate">{item.name}</span>
                <span className="text-zinc-500 shrink-0">{item.code}</span>
              </div>
              <div className="flex items-center gap-2 shrink-0 ml-2">
                <span className="text-zinc-400 text-[10px]">
                  量比: <span className="text-yellow-400 font-mono">{item.vol_ratio_30d?.toFixed(1)}x</span>
                </span>
                <span className="text-zinc-500 text-[10px]">
                  vol: {(item.vol_924 / 10000).toFixed(0)}万
                </span>
              </div>
            </div>
          ))}
          {items.length === 0 && (
            <div className="text-zinc-500 text-[10px] py-2 text-center">无显著放量异动</div>
          )}
        </div>
      )}

      <div className="flex justify-between text-[10px]">
        <span className="text-zinc-600">数据日期: {data.date}</span>
        <button onClick={fetchData} className="text-blue-400 underline">刷新</button>
      </div>
    </div>
  )
}