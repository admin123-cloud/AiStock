import { useEffect, useState } from 'react'
import request from '@/utils/request'

interface HealthComponent {
  name: string
  status: string
}

interface RuntimeHealth {
  preview?: boolean
  strategy_actionable?: boolean
  publisher_status?: string
  components?: HealthComponent[]
}

const COMPONENT_LABELS: Record<string, string> = {
  daily_kline_coverage: '日线覆盖',
  qmt_after_close_validation: '分钟盘后验收',
  g3_strategy_summary: 'G3摘要',
  broker_snapshot: '账户快照',
}

export function RuntimeBanner() {
  const [health, setHealth] = useState<RuntimeHealth | null>(null)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const data = (await request.get('/health/runtime', { skipErrorHandler: true })) as unknown as RuntimeHealth
        if (!cancelled) setHealth(data)
      } catch {
        if (!cancelled) setHealth(null)
      }
    }
    load()
    const timer = setInterval(load, 60000)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [])

  const blocked = !health?.strategy_actionable
  const title =
    health == null
      ? '数据状态待确认'
      : blocked
        ? '数据交付未通过'
        : '数据交付检查通过'
  const detail =
    health?.publisher_status === 'stale'
      ? '健康发布已过期，不能沿用上次结论'
      : (health?.components || [])
          .filter((x) => !['healthy', 'deferred'].includes(x.status))
          .map((x) => COMPONENT_LABELS[x.name] || x.name)
          .join('、') || '详细验收见左侧数据健康与监控；策略准入另行检查'

  const bannerClass = blocked
    ? 'bg-orange-50 text-orange-800 dark:bg-orange-950/50 dark:text-orange-300'
    : 'bg-emerald-50 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-300'

  return (
    <div
      role="status"
      className={`mb-4 flex flex-wrap gap-x-3 rounded-lg px-4 py-2.5 text-sm leading-relaxed ${bannerClass}`}
    >
      <strong className="shrink-0">
        {health?.preview ? '只读快照预览 · ' : ''}
        {title}
      </strong>
      <span>{detail}</span>
    </div>
  )
}
