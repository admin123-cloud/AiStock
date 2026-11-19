import React, { useMemo, useState, useEffect } from 'react'
import * as echarts from 'echarts/core'
import { RadarChart } from 'echarts/charts'
import {
  TitleComponent,
  TooltipComponent,
  LegendComponent,
  ToolboxComponent
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  RadarChart,
  TitleComponent,
  TooltipComponent,
  LegendComponent,
  ToolboxComponent,
  CanvasRenderer
])

interface DimensionItem {
  label: string
  value: number
  raw_value: number | string
  weight: number
}

interface RadarData {
  date: string
  as_of: string
  composite_score: number
  dimensions: Record<string, DimensionItem>
  weights: Record<string, number>
  available: boolean
  source: string
}

interface SentimentRadarProps {
  data: RadarData | null
  loading?: boolean
}

const DIMENSION_ORDER = [
  'up_down_ratio',
  'limit_up_seal_rate',
  'max_continuous_board',
  'limit_up_break_rate',
  'ma20_breadth',
  'turnover_percentile'
]

const DIMENSION_COLORS: Record<string, string> = {
  up_down_ratio: '#5470c6',
  limit_up_seal_rate: '#91cc75',
  max_continuous_board: '#fac858',
  limit_up_break_rate: '#ee6666',
  ma20_breadth: '#73c0de',
  turnover_percentile: '#3ba272'
}

function ScoreGauge({ score }: { score: number }) {
  const bgColor =
    score >= 80 ? '#22c55e' : score >= 60 ? '#84cc16' : score >= 40 ? '#eab308' : score >= 20 ? '#f97316' : '#ef4444'
  return (
    <div className="flex items-center gap-2">
      <div className="relative h-3 w-full rounded-full bg-zinc-700">
        <div
          className="absolute left-0 top-0 h-full rounded-full transition-all duration-500"
          style={{ width: `${Math.min(100, Math.max(0, score))}%`, backgroundColor: bgColor }}
        />
      </div>
      <span className="w-16 text-right text-xl font-bold" style={{ color: bgColor }}>
        {score.toFixed(1)}
      </span>
    </div>
  )
}

export default function SentimentRadar({ data, loading }: SentimentRadarProps) {
  const chartRef = React.useRef<HTMLDivElement>(null)
  const [chartInstance, setChartInstance] = useState<echarts.ECharts | null>(null)

  const radarOption = useMemo(() => {
    if (!data || !data.available) return null

    const indicators = DIMENSION_ORDER.map((key) => {
      const dim = data.dimensions[key]
      if (!dim) return null
      return { name: dim.label, max: 100 }
    }).filter(Boolean) as { name: string; max: number }[]

    const values = DIMENSION_ORDER.map((key) => {
      const dim = data.dimensions[key]
      return dim?.value ?? 0
    })

    return {
      tooltip: {
        trigger: 'item' as const,
        formatter: (params: any) => {
          if (!params.value) return ''
          const name = params.name || '情绪雷达'
          const lines = DIMENSION_ORDER.map((key, i) => {
            const dim = data.dimensions[key]
            return dim
              ? `${dim.label}: ${dim.value.toFixed(1)} (原始: ${dim.raw_value})`
              : ''
          }).filter(Boolean)
          return `<strong>${name}</strong><br/>${lines.join('<br/>')}<br/><strong>综合评分: ${data.composite_score}</strong>`
        }
      },
      radar: {
        indicator: indicators,
        radius: '65%',
        center: ['50%', '55%'],
        axisName: {
          color: '#a0aec0',
          fontSize: 11
        },
        splitArea: {
          areaStyle: {
            color: ['rgba(255,255,255,0.02)', 'rgba(255,255,255,0.05)']
          }
        },
        axisLine: {
          lineStyle: {
            color: 'rgba(255,255,255,0.15)'
          }
        }
      },
      series: [
        {
          type: 'radar',
          data: [
            {
              values: Object.values(data.dimensions).map(d => d.value),
              name: '今日情绪',
              areaStyle: {
                color: 'rgba(84, 112, 198, 0.3)'
              },
              lineStyle: {
                color: '#5470c6',
                width: 2
              },
              itemStyle: {
                color: '#5470c6'
              }
            }
          ]
        }
      ]
    }
  }, [data])

  useEffect(() => {
    if (!chartRef.current || !radarOption) return
    if (!chartInstance) {
      const instance = echarts.init(chartRef.current, undefined, { renderer: 'canvas' })
      setChartInstance(instance)
      return
    }
    chartInstance.setOption(radarOption, true)
    const handleResize = () => chartInstance.resize()
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [radarOption, chartInstance])

  // 当 chartRef 挂载后创建实例
  useEffect(() => {
    if (chartRef.current && !chartInstance) {
      const instance = echarts.init(chartRef.current, undefined, { renderer: 'canvas' })
      setChartInstance(instance)
    }
  }, [chartRef, chartInstance])

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16 text-zinc-400">
        加载中…
      </div>
    )
  }

  if (!data || !data.available) {
    return (
      <div className="flex items-center justify-center py-16 text-zinc-500">
        情绪雷达数据暂不可用
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* 综合评分 */}
      <div className="rounded-lg border border-zinc-700 bg-zinc-800/50 p-3">
        <div className="mb-1 text-xs text-zinc-400">综合评分</div>
        <ScoreGauge score={data.composite_score} />
        <div className="mt-1 text-right text-xs text-zinc-500">数据日期: {data.date}</div>
      </div>

      {/* 雷达图 */}
      <div ref={chartRef} className="h-72 w-full" />

      {/* 六维详情 */}
      <div className="grid grid-cols-2 gap-2 text-xs">
        {DIMENSION_ORDER.map((key) => {
          const dim = data.dimensions[key]
          if (!dim) return null
          return (
            <div key={key} className="flex items-center justify-between rounded border border-zinc-700/50 px-2 py-1.5">
              <span className="text-zinc-400" style={{ borderLeft: `3px solid ${DIMENSION_COLORS[key] || '#5470c6'}`, paddingLeft: 6 }}>
                {dim.label}
              </span>
              <span className="font-medium text-zinc-200" style={{ color: DIMENSION_COLORS[key] }}>
                {dim.value.toFixed(1)}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}