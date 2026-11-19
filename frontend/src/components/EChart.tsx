import { useEffect, useRef } from 'react'
import * as echarts from 'echarts'

export interface EChartProps {
  option: echarts.EChartsCoreOption
  /** 容器高度，默认 320 */
  height?: number | string
  className?: string
  loading?: boolean
}

function isDarkMode(): boolean {
  return typeof document !== 'undefined' && document.documentElement.classList.contains('dark')
}

/**
 * 通用 ECharts 6 封装：
 * - ResizeObserver 自适应容器尺寸
 * - 监听 <html> 的 class 变化，dark 切换时以对应主题 dispose 重建
 * - 卸载时 dispose
 */
export function EChart({ option, height = 320, className, loading = false }: EChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)
  const optionRef = useRef(option)
  optionRef.current = option

  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    let currentThemeDark: boolean | null = null

    const createInstance = () => {
      const dark = isDarkMode()
      currentThemeDark = dark
      // 暗色用内置 dark 主题配色，但背景强制透明以融入卡片底色
      chartRef.current = echarts.init(container, dark ? 'dark' : undefined, {
        renderer: 'canvas',
      })
      chartRef.current.setOption({
        backgroundColor: 'transparent',
        ...optionRef.current,
      })
    }

    createInstance()

    const resizeObserver = new ResizeObserver(() => {
      chartRef.current?.resize()
    })
    resizeObserver.observe(container)

    // 监听 documentElement class 变化，dark 切换时重建图表
    const classObserver = new MutationObserver(() => {
      if (!chartRef.current || currentThemeDark === null) return
      const dark = isDarkMode()
      if (dark === currentThemeDark) return
      chartRef.current.dispose()
      chartRef.current = null
      createInstance()
    })
    classObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['class'],
    })

    return () => {
      resizeObserver.disconnect()
      classObserver.disconnect()
      chartRef.current?.dispose()
      chartRef.current = null
    }
  }, [])

  // option 变化时更新（不做 deep diff，交给 ECharts 自身的 merge）
  useEffect(() => {
    chartRef.current?.setOption(option)
  }, [option])

  // loading 状态
  useEffect(() => {
    if (loading) {
      chartRef.current?.showLoading('default', {
        text: '',
        color: '#2563eb',
        maskColor: 'rgba(255, 255, 255, 0.6)',
      })
    } else {
      chartRef.current?.hideLoading()
    }
  }, [loading])

  return (
    <div
      ref={containerRef}
      className={className}
      style={{ width: '100%', height: typeof height === 'number' ? `${height}px` : height }}
    />
  )
}

export default EChart
