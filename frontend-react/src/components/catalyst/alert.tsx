import clsx from 'clsx'
import type React from 'react'
import { ExclamationCircleIcon } from '@heroicons/react/20/solid'

const styles = {
  base: [
    // Base
    'relative isolate w-full gap-x-3 rounded-lg border border-zinc-950/10 bg-white/70 px-4 py-3 text-sm/6 text-zinc-950 ring-1 ring-zinc-950/5',
    'flex items-start',
    // Dark mode
    'dark:border-white/10 dark:bg-white/5 dark:text-white dark:ring-white/10',
  ],
  colors: {
    zinc: [],
    red: [
      'border-red-500/20 bg-red-50/70 ring-red-500/5 dark:border-red-500/20 dark:bg-red-500/5 dark:ring-red-500/10',
    ],
    green: [
      'border-emerald-500/20 bg-emerald-50/70 ring-emerald-500/5 dark:border-emerald-500/20 dark:bg-emerald-500/5 dark:ring-emerald-500/10',
    ],
    amber: [
      'border-amber-500/20 bg-amber-50/70 ring-amber-500/5 dark:border-amber-500/20 dark:bg-amber-500/5 dark:ring-amber-500/10',
    ],
  },
} as const

export type AlertColor = keyof typeof styles.colors

export function Alert({
  color = 'zinc',
  className,
  children,
  ...props
}: { color?: AlertColor; className?: string; children: React.ReactNode } & Omit<
  React.ComponentPropsWithoutRef<'div'>,
  'color'
>) {
  return (
    <div role="alert" {...props} className={clsx(className, styles.base, styles.colors[color])}>
      {color === 'red' && (
        <ExclamationCircleIcon className="mt-0.5 size-5 shrink-0 text-red-500 dark:text-red-400" aria-hidden="true" />
      )}
      <div className="min-w-0">{children}</div>
    </div>
  )
}

export function AlertTitle({ className, ...props }: React.ComponentPropsWithoutRef<'h2'>) {
  return (
    <h2 {...props} className={clsx(className, 'text-sm/6 font-semibold text-zinc-950 dark:text-white')} />
  )
}

export function AlertDescription({ className, ...props }: React.ComponentPropsWithoutRef<'div'>) {
  return <div {...props} className={clsx(className, 'text-sm/6 text-zinc-700 dark:text-zinc-300')} />
}

export function AlertBody({ className, ...props }: React.ComponentPropsWithoutRef<'div'>) {
  return <div {...props} className={clsx(className, 'text-sm/6 text-zinc-700 dark:text-zinc-300')} />
}

export function AlertActions({ className, ...props }: React.ComponentPropsWithoutRef<'div'>) {
  return <div {...props} className={clsx(className, 'mt-4 flex flex-row justify-end gap-3')} />
}
