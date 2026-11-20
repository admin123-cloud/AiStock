import { lazy } from 'react'

export interface RouteMeta {
  title: string
  group?: string
  groupLabel?: string
  groupOrder?: number
  navOrder?: number
  navLabel?: string
  icon?: string
  hiddenInNav?: boolean
  parentPath?: string
}

export interface AppRoute {
  path: string
  name: string
  meta: RouteMeta
  component: React.LazyExoticComponent<React.ComponentType>
}

const page = (path: string, name: string, meta: RouteMeta, loader: () => Promise<{ default: React.ComponentType }>) => ({
  path,
  name,
  meta,
  component: lazy(loader)
})

export const routes: AppRoute[] = [
  page(
    '/accounts',
    'BrokerAccounts',
    { title: '资金账号管理', group: 'system', groupLabel: '系统', groupOrder: 4, navOrder: 0.5, navLabel: '资金账号管理' },
    () => import('@/views/operations/BrokerAccounts')
  ),
  page(
    '/system/tasks',
    'TaskCenter',
    { title: '任务中心', group: 'system', groupLabel: '系统', groupOrder: 4, navOrder: 1 },
    () => import('@/views/operations/TaskCenter')
  ),
  page(
    '/data/health',
    'DataCenter',
    { title: '数据健康与监控', group: 'data', groupLabel: '数据', groupOrder: 3, navOrder: 1, navLabel: '数据健康与监控' },
    () => import('@/views/operations/DataCenter')
  ),
  page(
    '/kline-check',
    'KlineCheck',
    { title: 'K线完整性检查', group: 'data', groupLabel: '数据', groupOrder: 3, navOrder: 2, navLabel: 'K线完整性', icon: '✓' },
    () => import('@/views/operations/KlineCheck')
  ),
  page(
    '/',
    'Home',
    { title: '首页', group: 'market', groupLabel: '市场', groupOrder: 1, navOrder: 1, navLabel: '首页', icon: '🏠' },
    () => import('@/views/market/Home')
  ),
  page(
    '/stocks',
    'Stocks',
    { title: '股票列表', group: 'market', groupLabel: '市场', groupOrder: 1, navOrder: 2, navLabel: '股票列表', icon: '📈' },
    () => import('@/views/market/Stocks')
  ),
  page(
    '/limit-up-ladder',
    'LimitUpLadder',
    { title: '连板天梯', group: 'market', groupLabel: '市场', groupOrder: 1, navOrder: 2.5, navLabel: '连板天梯', icon: '📊' },
    () => import('@/views/market/LimitUpLadder')
  ),
  page(
    '/stock/:code',
    'StockDetail',
    { title: '股票详情', group: 'market', groupLabel: '市场', hiddenInNav: true, parentPath: '/stocks' },
    () => import('@/views/market/StockDetail')
  ),
  page(
    '/index/:code',
    'IndexDetail',
    { title: '指数详情', group: 'market', groupLabel: '市场', hiddenInNav: true, parentPath: '/stocks' },
    () => import('@/views/market/IndexDetail')
  ),
  page(
    '/sectors',
    'Sectors',
    { title: '板块监控', group: 'market', groupLabel: '市场', groupOrder: 1, navOrder: 3, navLabel: '板块监控', icon: '🧩' },
    () => import('@/views/market/Sectors')
  ),
  page(
    '/sector/:code',
    'SectorDetail',
    { title: '板块详情', group: 'market', groupLabel: '市场', hiddenInNav: true, parentPath: '/sectors' },
    () => import('@/views/market/SectorDetail')
  ),
  page(
    '/g3/desk',
    'G3Desk',
    { title: 'G3 做T实战台', group: 'gen3', groupLabel: 'G3第三代策略', groupOrder: 2.7, navOrder: 1, navLabel: '做T实战台' },
    () => import('@/views/g3/G3Desk')
  ),
  page(
    '/g3/review',
    'G3Review',
    { title: 'G3 复盘与影子台账', group: 'gen3', groupLabel: 'G3第三代策略', groupOrder: 2.7, navOrder: 2, navLabel: '做T复盘' },
    () => import('@/views/g3/G3Review')
  ),
  page(
    '/g3/mainwave',
    'G3MainwaveDaily',
    { title: '主升浪每日选股池', group: 'gen3', groupLabel: 'G3第三代策略', groupOrder: 2.7, navOrder: 3, navLabel: '主升每日选股' },
    () => import('@/views/g3/MainwaveDaily')
  ),
  page(
    '/g3/settings',
    'G3Settings',
    { title: 'G3 策略合约与风控', group: 'gen3', groupLabel: 'G3第三代策略', groupOrder: 2.7, navOrder: 4, navLabel: '策略与风控' },
    () => import('@/views/g3/G3Settings')
  ),
  // 兼容旧书签路径，自动落入新做T实战台
  page(
    '/gen3/state-alpha',
    'Gen3LegacyRedirect',
    { title: 'G3 做T实战台', group: 'gen3', hiddenInNav: true },
    () => import('@/views/g3/G3Desk')
  ),
  // 兼容旧 /data-stats 书签，自动落入统一数据健康与监控页
  page(
    '/data-stats',
    'DataStatsRedirect',
    { title: '数据健康与监控', group: 'data', hiddenInNav: true },
    () => import('@/views/operations/DataCenter')
  ),
  page(
    '/system-config',
    'SystemConfig',
    { title: '系统运行状态', group: 'system', groupLabel: '系统', groupOrder: 4, navOrder: 1, navLabel: '系统状态', icon: '⚙️' },
    () => import('@/views/operations/SystemConfig')
  ),
  page(
    '/:pathMatch(.*)*',
    'NotFound',
    { title: '页面未找到', hiddenInNav: true },
    () => import('@/views/shared/NotFound')
  )
]

export function navGroups() {
  const visible = routes
    .filter((route) => route.meta.group && !route.meta.hiddenInNav && !route.path.includes(':'))
    .map((route) => ({
      path: route.path,
      label: route.meta.navLabel || route.meta.title || route.name,
      icon: route.meta.icon || '',
      group: route.meta.group!,
      groupLabel: route.meta.groupLabel || route.meta.group!,
      groupOrder: Number(route.meta.groupOrder ?? 99),
      navOrder: Number(route.meta.navOrder ?? 99)
    }))
    .sort((a, b) => (a.groupOrder !== b.groupOrder ? a.groupOrder - b.groupOrder : a.navOrder - b.navOrder))

  const groupMap = new Map<string, { key: string; label: string; order: number; items: typeof visible }>()
  visible.forEach((item) => {
    if (!groupMap.has(item.group)) {
      groupMap.set(item.group, { key: item.group, label: item.groupLabel, order: item.groupOrder, items: [] })
    }
    groupMap.get(item.group)!.items.push(item)
  })
  return Array.from(groupMap.values()).sort((a, b) => a.order - b.order)
}
