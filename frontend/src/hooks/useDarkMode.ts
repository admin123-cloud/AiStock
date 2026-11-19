import React, { useEffect, useState } from 'react'

const DARK_KEY = 'aistock_dark_mode_v2' // v2: 默认改为暗色，避开旧偏好

function readStored(): boolean | null {
  try {
    const saved = localStorage.getItem(DARK_KEY)
    if (saved === '1') return true
    if (saved === '0') return false
  } catch {
    /* ignore */
  }
  return null
}

export function useDarkMode() {
  // 默认暗色系（盯盘场景）；用户手动切换后记忆到 localStorage
  const [dark, setDark] = useState<boolean>(() => readStored() ?? true)

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    try {
      localStorage.setItem(DARK_KEY, dark ? '1' : '0')
    } catch {
      /* ignore */
    }
  }, [dark])

  return { dark, toggle: () => setDark((value) => !value) }
}
