import { createRouter, createWebHistory } from 'vue-router'

const APP_TITLE = 'AiStock'

const routes = [
  {
    path: '/',
    name: 'Home',
    component: () => import('@/views/pages/Home.vue'),
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
    component: () => import('@/views/pages/Stocks.vue'),
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
    component: () => import('@/views/pages/LimitUpLadder.vue'),
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
    path: '/stock/:code',
    name: 'StockDetail',
    component: () => import('@/views/pages/StockDetail.vue'),
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
    component: () => import('@/views/pages/IndexDetail.vue'),
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
    component: () => import('@/views/pages/Sectors.vue'),
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
    component: () => import('@/views/pages/SectorDetail.vue'),
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
    component: () => import('@/views/pages/Gen2Live.vue'),
    meta: {
      title: '第二代策略实盘交易',
      group: 'gen2',
      groupLabel: '第二代策略',
      groupOrder: 2.5,
      navOrder: 0.5,
      navLabel: '实盘交易',
      icon: 'G2'
    }
  },
  {
    path: '/gen2/selection-pool',
    name: 'Gen2SelectionPool',
    component: () => import('@/views/pages/Gen2SelectionPool.vue'),
    meta: {
      title: '第二代策略选股池',
      group: 'gen2',
      groupLabel: '第二代策略',
      groupOrder: 2.5,
      navOrder: 0.7,
      navLabel: '策略选股池',
      icon: 'G2'
    }
  },
  {
    path: '/gen2/lab',
    name: 'Gen2StrategyLab',
    component: () => import('@/views/pages/Gen2StrategyLab.vue'),
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
    component: () => import('@/views/pages/Gen2Backtest.vue'),
    meta: {
      title: '第二代策略历史回测',
      group: 'gen2',
      groupLabel: '第二代策略',
      groupOrder: 2.5,
      navOrder: 2,
      navLabel: '历史回测',
      icon: 'G2'
    }
  },
  {
    path: '/gen2/factors',
    name: 'Gen2FactorLab',
    component: () => import('@/views/pages/Gen2FactorLab.vue'),
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
    component: () => import('@/views/pages/Gen2Timing.vue'),
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
    path: '/gen3/research',
    name: 'Gen3Research',
    component: () => import('@/views/pages/Gen3Research.vue'),
    meta: {
      title: 'G3第三代策略研究台',
      group: 'gen3',
      groupLabel: 'G3第三代策略',
      groupOrder: 2.7,
      navOrder: 1,
      navLabel: '研究台',
      icon: 'G3'
    }
  },
  {
    path: '/gen3/v3-backtest',
    name: 'Gen3V3Backtest',
    component: () => import('@/views/pages/Gen3V3Backtest.vue'),
    meta: {
      title: 'G3 V3历史回测',
      group: 'gen3',
      groupLabel: 'G3第三代策略',
      groupOrder: 2.7,
      navOrder: 2,
      navLabel: 'V3历史回测',
      icon: 'G3'
    }
  },
  {
    path: '/gen3/v4-research',
    name: 'Gen3V4ResearchBacktest',
    component: () => import('@/views/pages/Gen3V4ResearchBacktest.vue'),
    meta: {
      title: 'G3 V4研究回测',
      group: 'gen3',
      groupLabel: 'G3第三代策略',
      groupOrder: 2.7,
      navOrder: 3,
      navLabel: 'V4研究回测',
      icon: 'G3'
    }
  },
  {
    path: '/data-stats',
    name: 'DataStats',
    component: () => import('@/views/pages/DataStats.vue'),
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
    component: () => import('@/views/pages/KlineCheck.vue'),
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
    component: () => import('@/views/pages/SystemConfig.vue'),
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
    component: () => import('@/views/pages/NotFound.vue'),
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

router.beforeEach((to, from, next) => {
  document.title = `${to.meta.title || 'AiStock'} - ${APP_TITLE}`
  next()
})

export default router
