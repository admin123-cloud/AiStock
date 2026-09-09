import { createRouter, createWebHistory } from 'vue-router'

const APP_TITLE = 'AiStock'

const routes = [
  {
    path: '/system/tasks', name: 'TaskCenter', component: () => import('@/views/operations/TaskCenter.vue'),
    meta: { title: '任务中心', group: 'system', groupLabel: '系统', groupOrder: 4, navOrder: 1 }
  },
  {
    path: '/data/health', name: 'DataHealth', component: () => import('@/views/operations/DataHealth.vue'),
    meta: { title: '数据健康', group: 'data', groupLabel: '数据', groupOrder: 3, navOrder: 1 }
  },
  {
    path: '/gen3/state-alpha/mainwave-evidence', name: 'MainwaveEvidence', component: () => import('@/views/g3/Gen3StateAlphaMainwave.vue'),
    meta: { title: '主升行业证据', group: 'gen3', hiddenInNav: true }
  },

  {
    path: '/',
    name: 'Home',
    component: () => import('@/views/market/Home.vue'),
    meta: {
      title: '首页',
      group: 'market',
      groupLabel: '市场',
      groupOrder: 1,
      navOrder: 1,
      navLabel: '首页',
      icon: '🏠'
    }
  },
  {
    path: '/stocks',
    name: 'Stocks',
    component: () => import('@/views/market/Stocks.vue'),
    meta: {
      title: '股票列表',
      group: 'market',
      groupLabel: '市场',
      groupOrder: 1,
      navOrder: 2,
      navLabel: '股票列表',
      icon: '📈'
    }
  },
  {
    path: '/limit-up-ladder',
    name: 'LimitUpLadder',
    component: () => import('@/views/market/LimitUpLadder.vue'),
    meta: {
      title: '连板天梯',
      group: 'market',
      groupLabel: '市场',
      groupOrder: 1,
      navOrder: 2.5,
      navLabel: '连板天梯',
      icon: '📊'
    }
  },
  {
    path: '/news/cls-radar',
    name: 'ClsNewsRadar',
    component: () => import('@/views/market/ClsNewsRadar.vue'),
    meta: {
      title: '财联社消息雷达',
      group: 'market',
      groupLabel: '市场',
      groupOrder: 1,
      navOrder: 2.7,
      navLabel: '财联社消息雷达',
      icon: 'N'
    }
  },
  {
    path: '/stock/:code',
    name: 'StockDetail',
    component: () => import('@/views/market/StockDetail.vue'),
    meta: {
      title: '股票详情',
      group: 'market',
      groupLabel: '市场',
      hiddenInNav: true,
      parentPath: '/stocks'
    }
  },
  {
    path: '/index/:code',
    name: 'IndexDetail',
    component: () => import('@/views/market/IndexDetail.vue'),
    meta: {
      title: '指数详情',
      group: 'market',
      groupLabel: '市场',
      hiddenInNav: true,
      parentPath: '/stocks'
    }
  },
  {
    path: '/sectors',
    name: 'Sectors',
    component: () => import('@/views/market/Sectors.vue'),
    meta: {
      title: '板块监控',
      group: 'market',
      groupLabel: '市场',
      groupOrder: 1,
      navOrder: 3,
      navLabel: '板块监控',
      icon: '🧩'
    }
  },
  {
    path: '/sector/:code',
    name: 'SectorDetail',
    component: () => import('@/views/market/SectorDetail.vue'),
    meta: {
      title: '板块详情',
      group: 'market',
      groupLabel: '市场',
      hiddenInNav: true,
      parentPath: '/sectors'
    }
  },
  {
    path: '/gen2/live',
    name: 'Gen2Live',
    component: () => import('@/views/g2/Gen2Live.vue'),
    meta: {
      title: '第二代策略实盘交易',
      group: 'gen2',
      groupLabel: '第二代策略',
      groupOrder: 2.5,
      navOrder: 0.5,
      navLabel: '实盘交易',
      hiddenInNav: true,
      icon: 'G2'
    }
  },
  {
    path: '/gen2/selection-pool',
    name: 'Gen2SelectionPool',
    component: () => import('@/views/g2/Gen2SelectionPool.vue'),
    meta: {
      title: '第二代策略选股池',
      group: 'gen2',
      groupLabel: '第二代策略',
      groupOrder: 2.5,
      navOrder: 0.7,
      navLabel: '策略选股池',
      hiddenInNav: true,
      icon: 'G2'
    }
  },
  {
    path: '/gen2/mainline-hotspots',
    name: 'Gen2MainlineHotspots',
    component: () => import('@/views/g2/Gen2MainlineHotspots.vue'),
    meta: {
      title: '第二代主线热点',
      group: 'gen2',
      groupLabel: '第二代策略',
      groupOrder: 2.5,
      navOrder: 0.8,
      navLabel: '主线热点',
      hiddenInNav: true,
      icon: 'G2'
    }
  },
  {
    path: '/gen2/lab',
    name: 'Gen2StrategyLab',
    component: () => import('@/views/g2/Gen2StrategyLab.vue'),
    meta: {
      title: '第二代策略实验台',
      group: 'gen2',
      groupLabel: '第二代策略',
      groupOrder: 2.5,
      navOrder: 1,
      navLabel: '策略实验台',
      hiddenInNav: true,
      icon: 'G2'
    }
  },
  {
    path: '/gen2/backtest',
    name: 'Gen2Backtest',
    component: () => import('@/views/g2/Gen2Backtest.vue'),
    meta: {
      title: '第二代策略历史回测',
      group: 'gen2',
      groupLabel: '第二代策略',
      groupOrder: 2.5,
      navOrder: 2,
      navLabel: '历史回测',
      hiddenInNav: true,
      icon: 'G2'
    }
  },
  {
    path: '/gen2/factors',
    name: 'Gen2FactorLab',
    component: () => import('@/views/g2/Gen2FactorLab.vue'),
    meta: {
      title: '第二代策略量化因子库',
      group: 'gen2',
      groupLabel: '第二代策略',
      groupOrder: 2.5,
      navOrder: 2.2,
      navLabel: '量化因子库',
      hiddenInNav: true,
      icon: 'G2'
    }
  },
  {
    path: '/gen2/timing',
    name: 'Gen2Timing',
    component: () => import('@/views/g2/Gen2Timing.vue'),
    meta: {
      title: '第二代策略交易择时',
      group: 'gen2',
      groupLabel: '第二代策略',
      groupOrder: 2.5,
      navOrder: 3,
      navLabel: '交易择时',
      hiddenInNav: true,
      icon: 'G2'
    }
  },
  {
    path: '/gen3/state-alpha',
    name: 'Gen3StateAlpha',
    component: () => import('@/views/g3/Gen3StateAlpha.vue'),
    meta: {
      title: 'G3 工作台',
      group: 'gen3',
      groupLabel: 'G3第三代策略',
      groupOrder: 2.7,
      navOrder: 1,
      navLabel: '工作台'
    }
  },
  {
    path: '/gen3/state-alpha/candidates',
    name: 'Gen3StateAlphaCandidates',
    component: () => import('@/views/g3/Gen3StateAlphaCandidates.vue'),
    meta: {
      title: 'G3 State Alpha 候选池',
      group: 'gen3',
      groupLabel: 'G3第三代策略',
      groupOrder: 2.7,
      navOrder: 1.05,
      navLabel: '候选池',
      hiddenInNav: true,
      icon: ''
    }
  },
  {
    path: '/gen3/state-alpha/holding-ticks',
    name: 'Gen3StateAlphaHoldingTicks',
    component: () => import('@/views/g3/Gen3StateAlphaHoldingTicks.vue'),
    meta: {
      title: '做T交易',
      group: 'gen3',
      groupLabel: 'G3第三代策略',
      groupOrder: 2.7,
      navOrder: 1.03,
      navLabel: '做T交易'
    }
  },
  {
    path: '/gen3/state-alpha/holding-t-review',
    name: 'Gen3HoldingTReview',
    component: () => import('@/views/g3/Gen3HoldingTReview.vue'),
    meta: {
      title: '做T复盘',
      group: 'gen3',
      groupLabel: 'G3第三代策略',
      groupOrder: 2.7,
      navOrder: 1.04,
      navLabel: '做T复盘'
    }
  },
  {
    path: '/gen3/state-alpha/mainwave',
    name: 'Gen3StateAlphaMainwave',
    component: () => import('@/views/g3/MainwaveDaily.vue'),
    meta: {
      title: '主升每日跟踪',
      group: 'gen3',
      groupLabel: 'G3第三代策略',
      groupOrder: 2.7,
      navOrder: 1.06,
      navLabel: '主升每日跟踪'
    }
  },
  {
    path: '/gen3/state-alpha/routes',
    name: 'Gen3StateAlphaRoutes',
    component: () => import('@/views/g3/Gen3StateAlphaRoutes.vue'),
    meta: {
      title: '策略有效性诊断',
      group: 'gen3',
      groupLabel: 'G3第三代策略',
      groupOrder: 2.7,
      navOrder: 1.08,
      navLabel: '有效性诊断'
    }
  },
  {
    path: '/gen3/state-alpha/risk',
    name: 'Gen3StateAlphaRisk',
    component: () => import('@/views/g3/Gen3StateAlphaRisk.vue'),
    meta: {
      title: '风控合同',
      group: 'gen3',
      groupLabel: 'G3第三代策略',
      groupOrder: 2.7,
      navOrder: 1.1,
      navLabel: '风控合同'
    }
  },
  {
    path: '/gen3/state-alpha/trades',
    name: 'Gen3StateAlphaTrades',
    component: () => import('@/views/g3/Gen3StateAlphaTrades.vue'),
    meta: {
      title: '历史成交复盘',
      group: 'gen3',
      groupLabel: 'G3第三代策略',
      groupOrder: 2.7,
      navOrder: 1.09,
      navLabel: '历史成交复盘'
    }
  },
  {
    path: '/gen3/state-alpha/replacement',
    name: 'Gen3StateAlphaReplacement',
    component: () => import('@/views/g3/Gen3StateAlphaReplacement.vue'),
    meta: {
      title: 'G3融合状态',
      group: 'gen3',
      groupLabel: 'G3第三代策略',
      groupOrder: 2.7,
      navOrder: 1.25,
      navLabel: '融合验收',
      hiddenInNav: true,
      icon: ''
    }
  },
  {
    path: '/data-stats',
    name: 'DataStats',
    component: () => import('@/views/operations/DataStats.vue'),
    meta: {
      title: '数据监控',
      group: 'data',
      groupLabel: '数据',
      groupOrder: 3,
      navOrder: 1,
      navLabel: '数据监控',
      icon: '📊'
    }
  },
  {
    path: '/kline-check',
    name: 'KlineCheck',
    component: () => import('@/views/operations/KlineCheck.vue'),
    meta: {
      title: 'K线巡检',
      group: 'data',
      groupLabel: '数据',
      groupOrder: 3,
      navOrder: 2,
      navLabel: 'K线巡检',
      icon: '🔍'
    }
  },
  {
    path: '/system-config',
    name: 'SystemConfig',
    component: () => import('@/views/operations/SystemConfig.vue'),
    meta: {
      title: '系统配置',
      group: 'system',
      groupLabel: '系统',
      groupOrder: 4,
      navOrder: 1,
      navLabel: '系统配置',
      icon: '⚙️'
    }
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'NotFound',
    component: () => import('@/views/shared/NotFound.vue'),
    meta: {
      title: '页面未找到',
      hiddenInNav: true
    }
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

const setPageTitle = (route) => {
  document.title = `${route.meta?.title || 'AiStock'} - ${APP_TITLE}`
}

router.afterEach((to) => {
  setPageTitle(to)
})

router.onError((error, to) => {
  setPageTitle(router.currentRoute.value)
  const message = String(error?.message || error || '')
  if (!message.includes('Failed to fetch dynamically imported module')) return

  const targetPath = to?.fullPath || window.location.pathname + window.location.search + window.location.hash
  const retryKey = `aistock_route_import_retry:${targetPath}`
  if (sessionStorage.getItem(retryKey)) {
    sessionStorage.removeItem(retryKey)
    return
  }
  sessionStorage.setItem(retryKey, '1')
  window.location.assign(targetPath)
})

router.isReady().then(() => {
  sessionStorage.removeItem(`aistock_route_import_retry:${router.currentRoute.value.fullPath}`)
  setPageTitle(router.currentRoute.value)
})

export default router
