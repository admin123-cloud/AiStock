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
    '/system/tasks',
    'TaskCenter',
    { title: '任务中心', group: 'system', groupLabel: '系统', groupOrder: 4, navOrder: 1 },
    () => import('@/views/operations/TaskCenter')
  ),
  page(
    '/data/health',
    'DataHealth',
    { title: '数据健康', group: 'data', groupLabel: '数据', groupOrder: 3, navOrder: 1 },
    () => import('@/views/operations/DataHealth')
  ),
  page(
    '/gen3/state-alpha/mainwave-evidence',
    'MainwaveEvidence',
    { title: '主升行业证据', group: 'gen3', hiddenInNav: true },
    () => import('@/views/g3/Gen3StateAlphaMainwave')
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
    '/news/cls-radar',
    'ClsNewsRadar',
    { title: '财联社消息雷达', group: 'market', groupLabel: '市场', groupOrder: 1, navOrder: 2.7, navLabel: '财联社消息雷达', icon: 'N' },
    () => import('@/views/market/ClsNewsRadar')
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
    '/gen2/live',
    'Gen2Live',
    { title: '第二代策略实盘交易', group: 'gen2', groupLabel: '第二代策略', groupOrder: 2.5, navOrder: 0.5, navLabel: '实盘交易', hiddenInNav: true, icon: 'G2' },
    () => import('@/views/g2/Gen2Live')
  ),
  page(
    '/gen2/selection-pool',
    'Gen2SelectionPool',
    { title: '第二代策略选股池', group: 'gen2', groupLabel: '第二代策略', groupOrder: 2.5, navOrder: 0.7, navLabel: '策略选股池', hiddenInNav: true, icon: 'G2' },
    () => import('@/views/g2/Gen2SelectionPool')
  ),
  page(
    '/gen2/mainline-hotspots',
    'Gen2MainlineHotspots',
    { title: '第二代主线热点', group: 'gen2', groupLabel: '第二代策略', groupOrder: 2.5, navOrder: 0.8, navLabel: '主线热点', hiddenInNav: true, icon: 'G2' },
    () => import('@/views/g2/Gen2MainlineHotspots')
  ),
  page(
    '/gen2/lab',
    'Gen2StrategyLab',
    { title: '第二代策略实验台', group: 'gen2', groupLabel: '第二代策略', groupOrder: 2.5, navOrder: 1, navLabel: '策略实验台', hiddenInNav: true, icon: 'G2' },
    () => import('@/views/g2/Gen2StrategyLab')
  ),
  page(
    '/gen2/backtest',
    'Gen2Backtest',
    { title: '第二代策略历史回测', group: 'gen2', groupLabel: '第二代策略', groupOrder: 2.5, navOrder: 2, navLabel: '历史回测', hiddenInNav: true, icon: 'G2' },
    () => import('@/views/g2/Gen2Backtest')
  ),
  page(
    '/gen2/factors',
    'Gen2FactorLab',
    { title: '第二代策略量化因子库', group: 'gen2', groupLabel: '第二代策略', groupOrder: 2.5, navOrder: 2.2, navLabel: '量化因子库', hiddenInNav: true, icon: 'G2' },
    () => import('@/views/g2/Gen2FactorLab')
  ),
  page(
    '/gen2/timing',
    'Gen2Timing',
    { title: '第二代策略交易择时', group: 'gen2', groupLabel: '第二代策略', groupOrder: 2.5, navOrder: 3, navLabel: '交易择时', hiddenInNav: true, icon: 'G2' },
    () => import('@/views/g2/Gen2Timing')
  ),
  page(
    '/gen3/state-alpha',
    'Gen3StateAlpha',
    { title: 'G3 工作台', group: 'gen3', groupLabel: 'G3第三代策略', groupOrder: 2.7, navOrder: 1, navLabel: '工作台' },
    () => import('@/views/g3/Gen3StateAlpha')
  ),
  page(
    '/gen3/state-alpha/candidates',
    'Gen3StateAlphaCandidates',
    { title: 'G3 State Alpha 候选池', group: 'gen3', groupLabel: 'G3第三代策略', groupOrder: 2.7, navOrder: 1.05, navLabel: '候选池', hiddenInNav: true, icon: '' },
    () => import('@/views/g3/Gen3StateAlphaCandidates')
  ),
  page(
    '/gen3/state-alpha/holding-ticks',
    'Gen3StateAlphaHoldingTicks',
    { title: '做T交易', group: 'gen3', groupLabel: 'G3第三代策略', groupOrder: 2.7, navOrder: 1.03, navLabel: '做T交易' },
    () => import('@/views/g3/Gen3StateAlphaHoldingTicks')
  ),
  page(
    '/gen3/state-alpha/holding-t-review',
    'Gen3HoldingTReview',
    { title: '做T复盘', group: 'gen3', groupLabel: 'G3第三代策略', groupOrder: 2.7, navOrder: 1.04, navLabel: '做T复盘' },
    () => import('@/views/g3/Gen3HoldingTReview')
  ),
  page(
    '/gen3/state-alpha/mainwave',
    'Gen3StateAlphaMainwaveDaily',
    { title: '主升每日跟踪', group: 'gen3', groupLabel: 'G3第三代策略', groupOrder: 2.7, navOrder: 1.06, navLabel: '主升每日跟踪' },
    () => import('@/views/g3/MainwaveDaily')
  ),
  page(
    '/gen3/state-alpha/routes',
    'Gen3StateAlphaRoutes',
    { title: '策略有效性诊断', group: 'gen3', groupLabel: 'G3第三代策略', groupOrder: 2.7, navOrder: 1.08, navLabel: '有效性诊断' },
    () => import('@/views/g3/Gen3StateAlphaRoutes')
  ),
  page(
    '/gen3/state-alpha/risk',
    'Gen3StateAlphaRisk',
    { title: '风控合同', group: 'gen3', groupLabel: 'G3第三代策略', groupOrder: 2.7, navOrder: 1.1, navLabel: '风控合同' },
    () => import('@/views/g3/Gen3StateAlphaRisk')
  ),
  page(
    '/gen3/state-alpha/trades',
    'Gen3StateAlphaTrades',
    { title: '历史成交复盘', group: 'gen3', groupLabel: 'G3第三代策略', groupOrder: 2.7, navOrder: 1.09, navLabel: '历史成交复盘' },
    () => import('@/views/g3/Gen3StateAlphaTrades')
  ),
  page(
    '/gen3/state-alpha/replacement',
    'Gen3StateAlphaReplacement',
    { title: 'G3融合状态', group: 'gen3', groupLabel: 'G3第三代策略', groupOrder: 2.7, navOrder: 1.25, navLabel: '融合验收', hiddenInNav: true, icon: '' },
    () => import('@/views/g3/Gen3StateAlphaReplacement')
  ),
  page(
    '/data-stats',
    'DataStats',
    { title: '数据监控', group: 'data', groupLabel: '数据', groupOrder: 3, navOrder: 1, navLabel: '数据监控', icon: '📊' },
    () => import('@/views/operations/DataStats')
  ),
  page(
    '/data/reviews',
    'DataReviewQueue',
    { title: '数据审核队列', group: 'data', groupLabel: '数据', groupOrder: 3, navOrder: 1.5, navLabel: '数据审核' },
    () => import('@/views/operations/DataReviewQueue')
  ),
  page(
    '/system-config',
    'SystemConfig',
    { title: '系统配置', group: 'system', groupLabel: '系统', groupOrder: 4, navOrder: 1, navLabel: '系统配置', icon: '⚙️' },
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
