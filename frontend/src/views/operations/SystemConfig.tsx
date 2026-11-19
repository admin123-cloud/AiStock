import { useCallback, useEffect, useState } from 'react'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Card, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import request from '@/utils/request'

interface HealthCheckData {
  status?: string
  service?: string
  version?: string
  runtime_health?: Record<string, unknown>
}

interface TradeCalendarStatus {
  status?: string
  total_days?: number
  min_date?: string | null
  max_date?: string | null
  latest_open_trade_date?: string | null
  error?: string
}

interface PublicationStatus {
  trade_date?: string
  state?: string
  published_at?: string | null
  [key: string]: unknown
}

export default function SystemConfig() {
  const [health, setHealth] = useState<HealthCheckData | null>(null)
  const [calendar, setCalendar] = useState<TradeCalendarStatus | null>(null)
  const [pubStatus, setPubStatus] = useState<PublicationStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [h, c, p] = await Promise.all([
        request.get<unknown, HealthCheckData>('/health/check'),
        request.get<unknown, TradeCalendarStatus>('/system/trade-calendar/status'),
        request.get<unknown, PublicationStatus>('/market/publication-status'),
      ])
      setHealth(h)
      setCalendar(c)
      setPubStatus(p)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-5 p-4 sm:p-6">
      <PageHeader
        title="系统运行状态"
        subtitle="只读安全观察面 · 盘中/盘后数据采集由 aistock-pipeline-runner 容器统一编排"
        actions={
          <Button onClick={() => void load()} disabled={loading}>
            {loading ? '刷新中…' : '刷新'}
          </Button>
        }
      />

      {error && (
        <div className="rounded-lg border border-red-500/40 bg-red-500/10 px-4 py-2.5 text-sm text-red-200">
          {error}
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard
          label="Web API 状态"
          value={health?.status === 'ok' ? '健康运行' : '异常'}
          hint={`${health?.service || 'AiStock Backend'} v${health?.version || '—'}`}
        />
        <StatCard
          label="最新交易日"
          value={calendar?.latest_open_trade_date || '—'}
          hint={`日历覆盖 ${calendar?.min_date || '—'} ~ ${calendar?.max_date || '—'}`}
        />
        <StatCard
          label="盘后发布状态"
          value={String(pubStatus?.state || pubStatus?.status || '—')}
          hint={`交易日 ${pubStatus?.trade_date || '—'}`}
        />
        <StatCard
          label="安全架构模式"
          value="只读观察面"
          hint="仅放行受控 Scoped-Write 通道"
        />
      </div>

      <Card>
        <SectionTitle title="容器与宿主机架构分工" />
        <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2 text-xs text-zinc-300">
          <div className="rounded-lg bg-zinc-900/80 p-3.5 space-y-1.5">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-white">Docker Desktop 生产栈</span>
              <Badge color="emerald">5 容器运行中</Badge>
            </div>
            <div>• <code>aistock-frontend</code> (:3000) — React 静态界面与 /api 反向代理</div>
            <div>• <code>aistock-backend</code> (:8077) — FastAPI 只读观察面 + 受控写入</div>
            <div>• <code>aistock-pipeline-runner</code> — 盘中快照、盘后 ETL、做T快慢车道</div>
            <div>• <code>aistock-postclose-watcher</code> — 盘后死线监控与邮件告警</div>
            <div>• <code>aistock-clickhouse</code> (:8123) — 核心行情分析型数仓</div>
          </div>
          <div className="rounded-lg bg-zinc-900/80 p-3.5 space-y-1.5">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-white">宿主机 Windows 桌面桥接</span>
              <Badge color="blue">host_bridges/</Badge>
            </div>
            <div>• <code>AiStock_TDXAutoDownload</code> — 通达信收盘后自动下载至 D:\TDX</div>
            <div>• <code>AiStock_THSDesktopBridge</code> — 同花顺远航双账号常驻指令桥接</div>
            <div>• <code>AiStock_THSReadAccount / Multi</code> — 定时采集双账号资金持仓</div>
            <div className="pt-1 text-zinc-400">
              注：为保证正式行情表与账户安全，Web 页面不再暴露高风险手动写库或旧 QMT 按钮。
            </div>
          </div>
        </div>
      </Card>

      <Card>
        <SectionTitle title="盘后发布控制面原始状态" />
        <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap rounded-lg bg-zinc-950/70 p-3 font-mono text-xs text-zinc-300">
          {JSON.stringify(pubStatus || {}, null, 2)}
        </pre>
      </Card>
    </div>
  )
}
