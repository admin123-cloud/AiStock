import React, { useEffect, useMemo, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import {
  ArrowTrendingUpIcon,
  ArrowsRightLeftIcon,
  ChartBarIcon,
  ChartPieIcon,
  ChevronRightIcon,
  ClipboardDocumentCheckIcon,
  ClipboardDocumentListIcon,
  ClockIcon,
  Cog6ToothIcon,
  ComputerDesktopIcon,
  HeartIcon,
  HomeIcon,
  ListBulletIcon,
  MagnifyingGlassIcon,
  MapIcon,
  MoonIcon,
  ShieldCheckIcon,
  SignalIcon,
  Squares2X2Icon,
  SunIcon,
  TrophyIcon,
} from '@heroicons/react/20/solid'
import type { ComponentType } from 'react'
import { SidebarLayout } from '@/components/catalyst/sidebar-layout'
import {
  Sidebar,
  SidebarBody,
  SidebarHeader,
  SidebarItem,
  SidebarLabel,
  SidebarSection
} from '@/components/catalyst/sidebar'
import { Navbar, NavbarItem, NavbarSection, NavbarSpacer } from '@/components/catalyst/navbar'
import { Badge } from '@/components/catalyst/badge'
import { Avatar } from '@/components/catalyst/avatar'
import { Input } from '@/components/catalyst/input'
import { navGroups, routes } from '@/router'
import { useDarkMode } from '@/hooks/useDarkMode'
import { RuntimeBanner } from '@/components/RuntimeBanner'
import request from '@/utils/request'

const EXPANDED_KEY = 'aistock_nav_expanded_groups'

/** 路由路径 → Heroicons 图标（20px solid，配 Catalyst SidebarItem） */
const NAV_ICONS: Record<string, ComponentType> = {
  '/': HomeIcon,
  '/stocks': ChartBarIcon,
  '/limit-up-ladder': TrophyIcon,
  '/news/cls-radar': SignalIcon,
  '/sectors': Squares2X2Icon,
  '/gen3/state-alpha': ComputerDesktopIcon,
  '/gen3/state-alpha/holding-ticks': ArrowsRightLeftIcon,
  '/gen3/state-alpha/holding-t-review': ClockIcon,
  '/gen3/state-alpha/mainwave': ArrowTrendingUpIcon,
  '/gen3/state-alpha/routes': MapIcon,
  '/gen3/state-alpha/risk': ShieldCheckIcon,
  '/gen3/state-alpha/trades': ListBulletIcon,
  '/data-stats': ChartPieIcon,
  '/data/reviews': ClipboardDocumentCheckIcon,
  '/data/health': HeartIcon,
  '/system/tasks': ClipboardDocumentListIcon,
  '/system-config': Cog6ToothIcon,
}

function Clock() {
  const [now, setNow] = useState('')
  useEffect(() => {
    const format = () => {
      const d = new Date()
      const p = (n: number) => String(n).padStart(2, '0')
      setNow(`${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`)
    }
    format()
    const timer = setInterval(format, 30000)
    return () => clearInterval(timer)
  }, [])
  return <span className="text-xs text-zinc-500 dark:text-zinc-400 tabular-nums">{now}</span>
}

/** 后端版本徽章（一次拉取，失败隐藏） */
function BackendVersion() {
  const [version, setVersion] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    request
      .get('/health/check', { skipErrorHandler: true })
      .then((res: unknown) => {
        const v = (res as { version?: string })?.version
        if (!cancelled && v) setVersion(v)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [])
  if (!version) return null
  return (
    <Badge color="indigo" className="font-mono">
      API {version.slice(0, 7)}
    </Badge>
  )
}

function AppNavbar() {
  const { dark, toggle } = useDarkMode()
  const location = useLocation()
  const breadcrumb = useBreadcrumb()

  return (
    <Navbar>
      <NavbarSection>
        {/* 移动端显示品牌（桌面端品牌在侧边栏） */}
        <Link
          to="/"
          className="text-lg font-extrabold tracking-tight text-zinc-950 lg:hidden dark:text-white"
        >
          AiStock
        </Link>
        <nav aria-label="面包屑" className="hidden items-center gap-1.5 text-sm lg:flex">
          {breadcrumb.map((item, index) => (
            <React.Fragment key={`${item.label}-${index}`}>
              {index > 0 && <ChevronRightIcon className="size-3.5 text-zinc-400 dark:text-zinc-600" />}
              {item.path ? (
                <Link
                  to={item.path}
                  className="text-zinc-500 transition-colors hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
                >
                  {item.label}
                </Link>
              ) : (
                <span
                  className={
                    index === breadcrumb.length - 1
                      ? 'font-semibold text-zinc-900 dark:text-zinc-100'
                      : 'text-zinc-500 dark:text-zinc-400'
                  }
                >
                  {item.label}
                </span>
              )}
            </React.Fragment>
          ))}
        </nav>
      </NavbarSection>
      <NavbarSpacer />
      <NavbarSection>
        <BackendVersion />
        <Clock />
        <NavbarItem onClick={toggle} aria-label="切换明暗主题">
          {dark ? (
            <SunIcon data-slot="icon" className="size-5 text-amber-400" />
          ) : (
            <MoonIcon data-slot="icon" className="size-5 text-zinc-500" />
          )}
        </NavbarItem>
        <NavbarItem aria-label="访客">
          <Avatar initials="访" square={false} />
        </NavbarItem>
      </NavbarSection>
    </Navbar>
  )
}

/** 基于路由表的面包屑：组名 / 父级页 / 当前页；移动端不显示 */
function useBreadcrumb(): { label: string; path?: string }[] {
  const location = useLocation()
  return useMemo(() => {
    const exact = routes.find((route) => route.path === location.pathname)
    const detail =
      exact ||
      routes.find(
        (route) =>
          route.path.includes(':') &&
          new RegExp(`^${route.path.replace(/:[^/]+/g, '[^/]+')}/?$`).test(location.pathname)
      )
    if (!detail) return []
    const meta = detail.meta
    const items: { label: string; path?: string }[] = []
    if (meta.groupLabel) items.push({ label: meta.groupLabel })
    if (meta.parentPath) {
      const parent = routes.find((route) => route.path === meta.parentPath)
      if (parent && !parent.meta.hiddenInNav) {
        items.push({ label: parent.meta.navLabel || parent.meta.title, path: parent.path })
      }
    }
    items.push({ label: meta.navLabel || meta.title })
    return items
  }, [location.pathname])
}

function AppSidebar() {
  const location = useLocation()
  const [keyword, setKeyword] = useState('')
  const [expanded, setExpanded] = useState<string[]>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(EXPANDED_KEY) || '[]')
      if (Array.isArray(saved) && saved.length > 0) return saved
    } catch {
      /* ignore */
    }
    return ['market']
  })

  const groups = useMemo(() => navGroups(), [])
  const currentGroup = useMemo(() => {
    return groups.find((g) => g.items.some((item) => item.path === location.pathname))?.key
  }, [location.pathname, groups])

  useEffect(() => {
    if (currentGroup && !expanded.includes(currentGroup)) {
      setExpanded((prev) => {
        const next = [...prev, currentGroup]
        localStorage.setItem(EXPANDED_KEY, JSON.stringify(next))
        return next
      })
    }
  }, [currentGroup]) // eslint-disable-line react-hooks/exhaustive-deps

  const toggleGroup = (key: string) => {
    setExpanded((prev) => {
      const next = prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]
      localStorage.setItem(EXPANDED_KEY, JSON.stringify(next))
      return next
    })
  }

  const isRouteActive = (path: string) => {
    if (path === '/') return location.pathname === '/'
    if (location.pathname === path) return true
    if (!location.pathname.startsWith(`${path}/`)) return false
    const visibleItems = groups.flatMap((group) => group.items)
    return !visibleItems.some((item) => {
      if (!item.path || item.path === path) return false
      if (!item.path.startsWith(`${path}/`)) return false
      return location.pathname === item.path || location.pathname.startsWith(`${item.path}/`)
    })
  }

  const filtered = keyword
    ? groups
        .map((group) => ({
          ...group,
          items: group.items.filter((item) => item.label.toLowerCase().includes(keyword.toLowerCase()))
        }))
        .filter((group) => group.items.length > 0)
    : groups

  return (
    <Sidebar>
      <SidebarHeader>
        <div className="flex items-center gap-2.5 pb-1">
          <div className="flex size-8 items-center justify-center rounded-lg bg-gradient-to-br from-indigo-500 to-purple-600 text-sm font-black text-white shadow-md">
            A
          </div>
          <div className="min-w-0">
            <div className="text-base font-extrabold tracking-tight text-zinc-900 dark:text-white">AiStock</div>
            <div className="truncate text-[11px] text-zinc-500 dark:text-zinc-400">策略观察工作台</div>
          </div>
        </div>
        <Input
          aria-label="搜索页面"
          placeholder="搜索页面..."
          value={keyword}
          onChange={(e) => setKeyword(e.target.value.trim())}
        />
      </SidebarHeader>
      <SidebarBody>
        {filtered.map((group) => (
          <SidebarSection key={group.key}>
            <button
              type="button"
              onClick={() => toggleGroup(group.key)}
              className="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-xs font-semibold uppercase tracking-wide text-zinc-500 transition-colors hover:bg-zinc-950/5 hover:text-zinc-800 dark:text-zinc-400 dark:hover:bg-white/5 dark:hover:text-zinc-200"
            >
              <ChevronRightIcon
                className={`size-3.5 transition-transform ${expanded.includes(group.key) || keyword ? 'rotate-90' : ''}`}
              />
              {group.label}
            </button>
            {(expanded.includes(group.key) || keyword) &&
              group.items.map((item) => {
                const Icon = NAV_ICONS[item.path] ?? ChartBarIcon
                return (
                  <SidebarItem key={item.path} href={item.path} current={isRouteActive(item.path)}>
                    <Icon data-slot="icon" />
                    <SidebarLabel>{item.label}</SidebarLabel>
                  </SidebarItem>
                )
              })}
          </SidebarSection>
        ))}
      </SidebarBody>
    </Sidebar>
  )
}

export function AppLayout({ children }: { children: React.ReactNode }) {
  const location = useLocation()
  return (
    <SidebarLayout navbar={<AppNavbar />} sidebar={<AppSidebar />}>
      {location.pathname !== '/' && <RuntimeBanner />}
      {children}
    </SidebarLayout>
  )
}
