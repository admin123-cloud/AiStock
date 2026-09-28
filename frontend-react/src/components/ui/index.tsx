import React from 'react'
import clsx from 'clsx'

/** 统一页面头：标题 + 副标题 + 右侧操作区 */
export function PageHeader({
  title,
  subtitle,
  actions,
  className,
}: {
  title: React.ReactNode
  subtitle?: React.ReactNode
  actions?: React.ReactNode
  className?: string
}) {
  return (
    <div className={clsx('mb-5 flex flex-wrap items-start justify-between gap-3', className)}>
      <div className="min-w-0">
        <h1 className="text-2xl font-bold tracking-tight text-zinc-900 dark:text-white">{title}</h1>
        {subtitle && (
          <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">{subtitle}</p>
        )}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}

/** 统一卡片：内层容器，层级低于布局壳内容卡 */
export function Card({
  className,
  children,
  ...props
}: { className?: string; children?: React.ReactNode } & React.ComponentPropsWithoutRef<'div'>) {
  return (
    <div
      {...props}
      className={clsx(
        className,
        'rounded-lg bg-white p-4 ring-1 ring-zinc-950/10 dark:bg-zinc-900 dark:ring-white/10'
      )}
    >
      {children}
    </div>
  )
}

/** 统一节标题：左对齐，替代各页手写 h2 与 border-l-4 竖条 */
export function SectionTitle({
  title,
  actions,
  className,
}: {
  title: React.ReactNode
  actions?: React.ReactNode
  className?: string
}) {
  return (
    <div className={clsx('mb-3 flex flex-wrap items-center justify-between gap-2', className)}>
      <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">{title}</h2>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  )
}

type StatTone = 'default' | 'up' | 'down' | 'warning' | 'info'

const toneValue: Record<StatTone, string> = {
  default: 'text-zinc-900 dark:text-white',
  up: 'text-red-600 dark:text-red-400',
  down: 'text-green-600 dark:text-green-500',
  warning: 'text-amber-600 dark:text-amber-400',
  info: 'text-indigo-600 dark:text-indigo-400',
}

/** 统一指标大卡：标签 + 数值 + 说明 */
export function StatCard({
  label,
  value,
  tone = 'default',
  hint,
  className,
}: {
  label: React.ReactNode
  value: React.ReactNode
  tone?: StatTone
  hint?: React.ReactNode
  className?: string
}) {
  return (
    <Card className={clsx('flex flex-col gap-1', className)}>
      <div className="truncate text-xs text-zinc-500 dark:text-zinc-400">{label}</div>
      <div className={clsx('text-2xl font-bold tabular-nums leading-tight', toneValue[tone])}>
        {value}
      </div>
      {hint && <div className="truncate text-xs text-zinc-400 dark:text-zinc-500">{hint}</div>}
    </Card>
  )
}

/** 统一涨跌数字：A股红涨绿跌 + 箭头符号（色觉无障碍）+ 等宽数字 */
export function ChangeValue({
  value,
  digits = 2,
  suffix = '%',
  className,
}: {
  value: number | null | undefined
  digits?: number
  suffix?: string
  className?: string
}) {
  if (value == null || Number.isNaN(value)) {
    return <span className={clsx('tabular-nums text-zinc-400 dark:text-zinc-500', className)}>--</span>
  }
  const up = value > 0
  const flat = value === 0
  return (
    <span
      className={clsx(
        'tabular-nums font-medium',
        flat
          ? 'text-zinc-500 dark:text-zinc-400'
          : up
            ? 'text-red-600 dark:text-red-400'
            : 'text-green-600 dark:text-green-500',
        className
      )}
    >
      {flat ? '—' : up ? '↑' : '↓'}
      {Math.abs(value).toFixed(digits)}
      {suffix}
    </span>
  )
}

/** 统一空态 */
export function EmptyState({ text = '暂无数据', className }: { text?: string; className?: string }) {
  return (
    <div
      className={clsx(
        'flex items-center justify-center rounded-lg border border-dashed border-zinc-300 py-8 text-sm text-zinc-400 dark:border-zinc-700 dark:text-zinc-500',
        className
      )}
    >
      {text}
    </div>
  )
}

/** 统一加载态 */
export function PageLoading({ text = '加载中…', className }: { text?: string; className?: string }) {
  return (
    <div className={clsx('flex items-center justify-center gap-2 py-12 text-sm text-zinc-500 dark:text-zinc-400', className)}>
      <span className="inline-block size-4 animate-spin rounded-full border-2 border-zinc-300 border-t-indigo-500" />
      {text}
    </div>
  )
}
