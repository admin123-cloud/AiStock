import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { Alert } from '@/components/catalyst/alert'

type ToastKind = 'error' | 'success' | 'info'
interface ToastItem {
  id: number
  kind: ToastKind
  message: string
}

interface ToastApi {
  error: (message: string) => void
  success: (message: string) => void
  info: (message: string) => void
}

const ToastContext = createContext<ToastApi | null>(null)

let nextId = 1

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])

  const push = useCallback((kind: ToastKind, message: string) => {
    const id = nextId++
    setItems((prev) => [...prev, { id, kind, message }])
    setTimeout(() => {
      setItems((prev) => prev.filter((item) => item.id !== id))
    }, kind === 'error' ? 5000 : 3000)
  }, [])

  const api = useMemo<ToastApi>(
    () => ({
      error: (m) => push('error', m),
      success: (m) => push('success', m),
      info: (m) => push('info', m)
    }),
    [push]
  )

  // 接收非组件模块通过 window 事件发来的 toast
  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent).detail as { kind: ToastKind; message: string }
      push(detail.kind, detail.message)
    }
    window.addEventListener('aistock-toast', handler)
    return () => window.removeEventListener('aistock-toast', handler)
  }, [push])

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="pointer-events-none fixed top-4 right-4 z-[9999] flex w-80 flex-col gap-2">
        {items.map((item) => (
          <div key={item.id} className="pointer-events-auto shadow-lg">
            <Alert color={item.kind === 'error' ? 'red' : item.kind === 'success' ? 'green' : 'zinc'}>
              {item.message}
            </Alert>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext)
  if (!ctx) {
    throw new Error('useToast 必须在 ToastProvider 内使用')
  }
  return ctx
}

// 供非组件模块（如 request.ts）直接使用的轻量入口
export const toast: ToastApi = {
  error: (m) => window.dispatchEvent(new CustomEvent('aistock-toast', { detail: { kind: 'error', message: m } })),
  success: (m) => window.dispatchEvent(new CustomEvent('aistock-toast', { detail: { kind: 'success', message: m } })),
  info: (m) => window.dispatchEvent(new CustomEvent('aistock-toast', { detail: { kind: 'info', message: m } }))
}
