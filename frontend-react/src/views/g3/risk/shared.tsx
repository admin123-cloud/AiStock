// 通用 UI 原语 + 弹窗 hook + 页面上下文（Gen3StateAlphaRisk 迁移用）
import { createContext, useCallback, useContext, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import clsx from 'clsx'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Dialog, DialogActions, DialogBody, DialogTitle } from '@/components/catalyst/dialog'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/catalyst/table'
import { Textarea } from '@/components/catalyst/textarea'
import { Card, EmptyState, StatCard } from '@/components/ui'
import type { Row, Tone } from './labels'

// ---------- 上下文：v = 视图模型（含 loading 标志），a = 动作 ----------
export interface RiskCtxValue {
  v: Record<string, any>
  a: Record<string, (...args: any[]) => any>
}

const RiskCtx = createContext<RiskCtxValue | null>(null)
export const RiskProvider = RiskCtx.Provider

export function useRisk(): RiskCtxValue {
  const ctx = useContext(RiskCtx)
  if (!ctx) throw new Error('RiskCtx missing')
  return ctx
}

// ---------- Tag / Button 包装 ----------
const toneBadgeColor: Record<Tone, string> = {
  success: 'emerald',
  danger: 'red',
  warning: 'amber',
  info: 'zinc',
  primary: 'sky'
}

export function Tag({ tone = 'info', children }: { tone?: Tone; children: ReactNode }) {
  return <Badge color={toneBadgeColor[tone] as any}>{children}</Badge>
}

const btnToneClass: Record<Tone, string> = {
  success: 'text-emerald-700 dark:text-emerald-400',
  danger: 'text-red-700 dark:text-red-400',
  warning: 'text-amber-700 dark:text-amber-400',
  info: 'text-zinc-700 dark:text-zinc-300',
  primary: 'text-indigo-700 dark:text-indigo-400'
}

export function Btn({
  tone = 'info',
  loading,
  disabled,
  children,
  onClick
}: {
  tone?: Tone
  loading?: boolean
  disabled?: boolean
  children: ReactNode
  onClick?: () => void
}) {
  return (
    <Button
      outline
      disabled={Boolean(loading) || Boolean(disabled)}
      className={clsx('px-2.5 py-1 text-xs sm:px-2.5 sm:py-1 sm:text-xs', btnToneClass[tone])}
      onClick={onClick}
    >
      {children}
    </Button>
  )
}

// ---------- 文本单元 ----------
export function textOf(value: any): string {
  return value === null || value === undefined || value === '' ? '--' : String(value)
}

export function CellText({ value }: { value: any }) {
  const text = textOf(value)
  return (
    <span title={text} className="block max-w-[460px] truncate">
      {text}
    </span>
  )
}

// ---------- 表格 ----------
export interface Col {
  key: string
  label: string
  align?: 'left' | 'right'
  width?: number
  minWidth?: number
  render?: (row: Row) => ReactNode
}

export function DataTable({ rows, cols, empty = '暂无数据' }: { rows: Row[]; cols: Col[]; empty?: string }) {
  const list = Array.isArray(rows) ? rows : []
  return (
    <Table dense striped className="mt-3">
      <TableHead>
        <TableRow>
          {cols.map((c) => (
            <TableHeader
              key={c.key}
              className={c.align === 'right' ? 'text-right' : undefined}
              style={{ width: c.width, minWidth: c.minWidth ?? c.width }}
            >
              {c.label}
            </TableHeader>
          ))}
        </TableRow>
      </TableHead>
      <TableBody>
        {list.length === 0 ? (
          <TableRow>
            <TableCell colSpan={cols.length}>
              <EmptyState text={empty} className="py-6" />
            </TableCell>
          </TableRow>
        ) : (
          list.map((row, i) => (
            <TableRow key={String(row.key ?? row.id ?? row.review_key ?? i) + ':' + i}>
              {cols.map((c) => (
                <TableCell
                  key={c.key}
                  className={c.align === 'right' ? 'text-right tabular-nums' : undefined}
                >
                  {c.render ? c.render(row) : <CellText value={row[c.key]} />}
                </TableCell>
              ))}
            </TableRow>
          ))
        )}
      </TableBody>
    </Table>
  )
}

// ---------- 布局原语（视觉对齐全局 @/components/ui：Card / SectionTitle / StatCard） ----------
export function Panel({
  title,
  desc,
  actions,
  children,
  className
}: { title?: ReactNode; desc?: ReactNode; actions?: ReactNode; children?: ReactNode; className?: string }) {
  return (
    <Card className={clsx('p-5', className)}>
      {(title || actions) && (
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            {title && (
              <h2 className="text-left text-base font-semibold text-zinc-900 dark:text-zinc-100">{title}</h2>
            )}
            {desc && <p className="mt-1 max-w-3xl text-sm text-zinc-500 dark:text-zinc-400">{desc}</p>}
          </div>
          {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
        </div>
      )}
      {children}
    </Card>
  )
}

export function StatGrid({ items, className }: { items: { span: ReactNode; strong: ReactNode; small?: ReactNode }[]; className?: string }) {
  return (
    <div className={clsx('mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4', className)}>
      {items.map((item, i) => (
        <StatCard key={i} label={item.span} value={item.strong} hint={item.small} />
      ))}
    </div>
  )
}

export function KVGrid({ items }: { items: { label: ReactNode; value: ReactNode }[] }) {
  return (
    <div className="mt-4 grid gap-3 sm:grid-cols-2">
      {items.map((item, i) => (
        <StatCard key={i} label={item.label} value={item.value} />
      ))}
    </div>
  )
}

export function AuditMain({ label, strong, text, extra }: { label: ReactNode; strong: ReactNode; text?: ReactNode; extra?: ReactNode }) {
  return (
    <Card className="mt-4">
      <div className="text-xs uppercase tracking-wide text-zinc-500 dark:text-zinc-400">{label}</div>
      <div className="mt-1 text-sm font-semibold text-zinc-900 dark:text-white">{strong}</div>
      {text && <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-300">{text}</p>}
      {extra}
    </Card>
  )
}

export function AuditGrid({ items }: { items: { label: ReactNode; value: ReactNode }[] }) {
  return (
    <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      {items.map((item, i) => (
        <StatCard key={i} label={item.label} value={item.value} />
      ))}
    </div>
  )
}

const decisionToneClass: Record<string, string> = {
  block: 'border-red-300 bg-red-50 dark:border-red-500/30 dark:bg-red-500/10',
  ready: 'border-emerald-300 bg-emerald-50 dark:border-emerald-500/30 dark:bg-emerald-500/10',
  observe: 'border-sky-300 bg-sky-50 dark:border-sky-500/30 dark:bg-sky-500/10',
  watch: 'border-amber-300 bg-amber-50 dark:border-amber-500/30 dark:bg-amber-500/10'
}

export function DecisionCard({
  tone,
  label,
  strong,
  text,
  detail,
  flags
}: {
  tone: string
  label: ReactNode
  strong: ReactNode
  text?: ReactNode
  detail?: { label: ReactNode; value: ReactNode }[]
  flags?: { tone: Tone; text: ReactNode }[]
}) {
  return (
    <div className={clsx('mt-4 rounded-lg border p-4', decisionToneClass[tone] || decisionToneClass.observe)}>
      <div>
        <div className="text-xs uppercase tracking-wide opacity-70">{label}</div>
        <div className="mt-1 text-lg font-semibold">{strong}</div>
        {text && <p className="mt-1 text-sm opacity-80">{text}</p>}
      </div>
      {detail && detail.length > 0 && (
        <div className="mt-3 grid gap-3 sm:grid-cols-3">
          {detail.map((item, i) => (
            <div key={i}>
              <div className="text-xs opacity-70">{item.label}</div>
              <p className="mt-1 text-sm">{item.value}</p>
            </div>
          ))}
        </div>
      )}
      {flags && flags.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {flags.map((f, i) => (
            <Tag key={i} tone={f.tone}>
              {f.text}
            </Tag>
          ))}
        </div>
      )}
    </div>
  )
}

// ---------- ElMessageBox 替代：confirm / prompt ----------
export interface PromptOptions {
  title: string
  message: string
  confirmText?: string
  cancelText?: string
  placeholder?: string
  initialValue?: string
  minLength?: number
}

interface DialogState extends PromptOptions {
  mode: 'prompt' | 'confirm'
  resolve: (value: any) => void
}

export function useDialogs() {
  const [state, setState] = useState<DialogState | null>(null)
  const [value, setValue] = useState('')
  const resolverRef = useRef<((value: any) => void) | null>(null)

  const confirm = useCallback((opts: { title: string; message: string; confirmText?: string; cancelText?: string }) => {
    return new Promise<boolean>((resolve) => {
      resolverRef.current = resolve as any
      setState({ ...opts, mode: 'confirm', resolve: resolve as any })
      setValue('')
    })
  }, [])

  const prompt = useCallback((opts: PromptOptions) => {
    return new Promise<string | null>((resolve) => {
      resolverRef.current = resolve as any
      setState({ ...opts, mode: 'prompt', resolve: resolve as any })
      setValue(opts.initialValue || '')
    })
  }, [])

  const finish = useCallback((result: any) => {
    resolverRef.current?.(result)
    resolverRef.current = null
    setState(null)
  }, [])

  const minLength = state?.minLength ?? 0
  const invalid = state?.mode === 'prompt' && value.trim().length < minLength

  const dialogElement = state ? (
    <Dialog open onClose={() => finish(state.mode === 'confirm' ? false : null)} size="2xl">
      <DialogTitle>{state.title}</DialogTitle>
      <DialogBody>
        <p className="whitespace-pre-wrap text-sm text-zinc-700 dark:text-zinc-300">{state.message}</p>
        {state.mode === 'prompt' && (
          <div className="mt-3">
            <Textarea
              rows={3}
              value={value}
              placeholder={state.placeholder}
              onChange={(e: any) => setValue(e.target.value)}
            />
            {invalid && <p className="mt-1 text-xs text-red-600 dark:text-red-400">请至少写一句依据</p>}
          </div>
        )}
      </DialogBody>
      <DialogActions>
        <Button plain onClick={() => finish(state.mode === 'confirm' ? false : null)}>
          {state.cancelText || '取消'}
        </Button>
        <Button
          color="indigo"
          disabled={invalid}
          onClick={() => finish(state.mode === 'confirm' ? true : value.trim())}
        >
          {state.confirmText || '确定'}
        </Button>
      </DialogActions>
    </Dialog>
  ) : null

  return { confirm, prompt, dialogElement }
}
