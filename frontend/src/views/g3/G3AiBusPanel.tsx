import { useEffect, useRef, useState } from 'react'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Card } from '@/components/ui'
import {
  askG3AiBus,
  getG3AiBusJob,
  getG3AiBusStatus,
  type G3AiBusJobResult,
  type G3AiBusStatusResponse,
} from '@/api/g3'

const QUICK_PROMPTS: Array<{ label: string; kind: string; prompt: string }> = [
  {
    label: '研判当前盘面与冰点信号',
    kind: 'desk_qa',
    prompt: '请结合当前最新盘口、大盘宽度与 AR4.6 规则，研判今天是否具备冰点反抽或切仓条件，给出明确操作纪律。',
  },
  {
    label: '复核当前持仓风险与调仓',
    kind: 'desk_qa',
    prompt: '请核查当前 acct1 持仓（红利低波/十年国债等）与今日 AR4.6 目标配比，评估是否有风险或需要优先处理的调仓动作。',
  },
  {
    label: '扫描今日全市场赚钱效应',
    kind: 'desk_qa',
    prompt: '请扫描当前 A 股、港股、跨境 ETF 与主要行业板块的赚钱效应，总结资金流向与需要回避的亏钱效应方向。',
  },
]

export default function G3AiBusPanel() {
  const [status, setStatus] = useState<G3AiBusStatusResponse | null>(null)
  const [prompt, setPrompt] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [activeJobId, setActiveJobId] = useState<string | null>(null)
  const [activeResult, setActiveResult] = useState<G3AiBusJobResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const pollRef = useRef<number | null>(null)

  const refreshStatus = async () => {
    try {
      const res = await getG3AiBusStatus(8)
      setStatus(res)
      if (!activeResult && res.recent_results.length > 0) {
        setActiveResult(res.recent_results[0])
      }
    } catch {
      // 静默忽略状态轮询抖动
    }
  }

  useEffect(() => {
    void refreshStatus()
    const t = window.setInterval(() => {
      void refreshStatus()
    }, 15000)
    return () => {
      window.clearInterval(t)
      if (pollRef.current) window.clearInterval(pollRef.current)
    }
  }, [])

  const submitQuestion = async (text: string, kind = 'desk_qa') => {
    const clean = text.trim()
    if (!clean || submitting) return
    setSubmitting(true)
    setError(null)
    try {
      const res = await askG3AiBus({ prompt: clean, kind, source: 'desk_ui' })
      if (!res.ok || !res.job_id) {
        setError(res.detail || '提交 AI 思考任务失败')
        setSubmitting(false)
        return
      }
      const jid = res.job_id
      setActiveJobId(jid)
      setPrompt('')
      void refreshStatus()

      if (pollRef.current) window.clearInterval(pollRef.current)
      let ticks = 0
      pollRef.current = window.setInterval(async () => {
        ticks += 1
        try {
          const jr = await getG3AiBusJob(jid)
          if (jr.status === 'completed' && jr.result) {
            if (pollRef.current) window.clearInterval(pollRef.current)
            pollRef.current = null
            setActiveJobId(null)
            setActiveResult(jr.result)
            setSubmitting(false)
            void refreshStatus()
          } else if (ticks >= 90) {
            if (pollRef.current) window.clearInterval(pollRef.current)
            pollRef.current = null
            setSubmitting(false)
            setError('等待超过 3 分钟，任务已保留在队列中，会话恢复后仍会处理。')
          }
        } catch {
          // 忽略单次轮询网络波动
        }
      }, 2000)
    } catch (e) {
      const err = e as { message?: string }
      setError(err.message || '提交失败')
      setSubmitting(false)
    }
  }

  return (
    <Card className="border-cyan-500/30 bg-zinc-900/70 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold text-white">🤖 在线 AI 参谋（Antigravity 决策总线）</span>
          <Badge color={status?.session_online ? 'emerald' : 'zinc'}>
            {status?.session_online ? '🟢 会话在线待命' : '⚪ 会话离线（排队）'}
          </Badge>
          {(status?.pending_count ?? 0) > 0 && (
            <Badge color="amber">排队中 {status?.pending_count}</Badge>
          )}
        </div>
        <span className="text-[11px] text-zinc-400">
          仅做深度研判与风险排雷 · 严禁自动下单
          {status?.heartbeat_at ? ` · 心跳 ${status.heartbeat_at}` : ''}
        </span>
      </div>

      <div className="mt-2.5 flex flex-wrap gap-1.5">
        {QUICK_PROMPTS.map((q) => (
          <Button
            key={q.label}
            outline
            disabled={submitting}
            onClick={() => void submitQuestion(q.prompt, q.kind)}
          >
            {q.label}
          </Button>
        ))}
      </div>

      <div className="mt-2.5 flex gap-2">
        <input
          type="text"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              void submitQuestion(prompt)
            }
          }}
          placeholder="直接向在线 Antigravity 会话提问（可查库/搜实时新闻/核查持仓与风控）..."
          className="flex-1 rounded-md border border-zinc-700 bg-zinc-950 px-3 py-1.5 text-xs text-white placeholder-zinc-500 focus:border-cyan-400 focus:outline-none"
        />
        <Button color="cyan" disabled={submitting || !prompt.trim()} onClick={() => void submitQuestion(prompt)}>
          {submitting ? 'AI 思考中...' : '提交研判'}
        </Button>
      </div>

      {activeJobId && (
        <div className="mt-2 rounded-md border border-cyan-500/30 bg-cyan-950/30 px-3 py-2 text-xs text-cyan-200">
          ⏳ 已提交至本地决策总线（<code>{activeJobId}</code>），正在等待在线 Antigravity 会话深度分析并写回...
        </div>
      )}

      {error && (
        <div className="mt-2 rounded-md border border-amber-500/40 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
          ⚠️ {error}
        </div>
      )}

      {activeResult && (
        <div className="mt-3 rounded-md border border-zinc-800 bg-zinc-950/80 p-3 text-xs">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-semibold text-cyan-300">
              最新研判：{activeResult.summary || activeResult.prompt.slice(0, 48)}
            </span>
            <div className="flex items-center gap-1.5">
              <Badge color="cyan">{activeResult.decision || 'advisory'}</Badge>
              <span className="font-mono text-[11px] text-zinc-500">{activeResult.completed_at}</span>
            </div>
          </div>
          <div className="mt-1 text-[11px] text-zinc-400">问题：{activeResult.prompt}</div>
          <div className="mt-2 whitespace-pre-wrap leading-relaxed text-zinc-100">{activeResult.answer}</div>
          {activeResult.risk_flags.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {activeResult.risk_flags.map((r, i) => (
                <Badge key={i} color="rose">⚠️ {r}</Badge>
              ))}
            </div>
          )}
          {activeResult.actions.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {activeResult.actions.map((a, i) => (
                <Badge key={i} color="emerald">👉 {a}</Badge>
              ))}
            </div>
          )}
        </div>
      )}

      {status && status.recent_results.length > 1 && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px] text-zinc-400">
          <span>历史记录：</span>
          {status.recent_results.slice(0, 5).map((item) => (
            <button
              key={item.job_id}
              type="button"
              onClick={() => setActiveResult(item)}
              className={`rounded border px-2 py-0.5 transition ${
                activeResult?.job_id === item.job_id
                  ? 'border-cyan-500/60 bg-cyan-950/40 text-cyan-200'
                  : 'border-zinc-800 bg-zinc-950 text-zinc-400 hover:text-zinc-200'
              }`}
            >
              {item.completed_at.slice(5, 16)} · {(item.summary || item.prompt).slice(0, 14)}
            </button>
          ))}
        </div>
      )}
    </Card>
  )
}
