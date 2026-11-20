import { useEffect, useState } from 'react'
import request from '@/utils/request'

interface HealthComponent {
  name: string
  label?: string
  status: string
}

interface RuntimeHealth {
  strategy_actionable?: boolean
  publisher_status?: string
  latest_trade_date?: string
  summary_text?: string
  components?: HealthComponent[]
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
      ? '数据状态检查中'
      : blocked
        ? '数据交付存在缺口'
        : '数据交付正常'

  const badNames = (health?.components || [])
    .filter((x) => !['healthy', 'deferred'].includes(x.status))
    .map((x) => x.label || x.name)

  const detail =
    health == null
      ? '正在连接 ClickHouse 校验最新交易日覆盖情况'
      : badNames.length > 0
        ? `异常项：${badNames.join('、')}（详见左侧「数据健康与监控」）`
        : health.summary_text ||
          (health.latest_trade_date
            ? `最新交易日 ${health.latest_trade_date}，股票与核心指数全周期完整`
            : '股票与核心指数全周期完整，详见左侧「数据健康与监控」')

  const bannerClass =
    health == null
      ? 'bg-zinc-100 text-zinc-700 dark:bg-zinc-900 dark:text-zinc-300'
      : blocked
        ? 'bg-amber-50 text-amber-800 dark:bg-amber-950/50 dark:text-amber-300'
        : 'bg-emerald-50 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-300'

  return (
    <div
      role="status"
      className={`mb-4 flex flex-wrap gap-x-3 rounded-lg px-4 py-2 text-sm leading-relaxed ${bannerClass}`}
    >
      <strong className="shrink-0">{title}</strong>
      <span>{detail}</span>
    </div>
  )
}
