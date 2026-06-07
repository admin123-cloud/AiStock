<template>
  <div id="app" class="app-shell">
    <TopHeader @toggle-sidebar="toggleSidebar" />

    <div class="shell-body">
      <SideNav
        :open="isSidebarOpen"
        :is-mobile="isMobile"
        @update:open="isSidebarOpen = $event"
        @navigate="handleNavNavigate"
      />

      <main class="main-panel">
        <div class="breadcrumb-bar">
          <router-link
            v-for="(item, index) in breadcrumbItems"
            :key="`${item.label}-${index}`"
            :to="item.path || ''"
            class="breadcrumb-item"
            :class="{ current: index === breadcrumbItems.length - 1, clickable: !!item.path }"
            @click.prevent="navigateBreadcrumb(item)"
          >
            {{ item.label }}
          </router-link>
        </div>

        <div class="page-content">
          <router-view />
        </div>
      </main>
    </div>

    <div v-if="isMobile && isSidebarOpen" class="mobile-mask" @click="isSidebarOpen = false"></div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import SideNav from '@/components/SideNav.vue'
import TopHeader from '@/components/TopHeader.vue'

const route = useRoute()
const router = useRouter()

const isSidebarOpen = ref(false)
const isMobile = ref(false)

const checkMobile = () => {
  isMobile.value = window.innerWidth <= 1024
  if (!isMobile.value) {
    isSidebarOpen.value = false
  }
}

onMounted(() => {
  checkMobile()
  window.addEventListener('resize', checkMobile)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', checkMobile)
})

const visibleRoutes = computed(() =>
  router.getRoutes().filter((item) => item.meta?.group && !item.meta?.hiddenInNav && !item.path.includes(':'))
)

const routeMap = computed(() => {
  const map = new Map()
  visibleRoutes.value.forEach((item) => {
    map.set(item.path, item)
  })
  return map
})

const breadcrumbItems = computed(() => {
  const meta = route.meta || {}
  const items = []
  const groupLabel = meta.groupLabel || meta.group
  if (groupLabel) {
    items.push({ label: groupLabel })
  }

  if (meta.hiddenInNav && meta.parentPath) {
    const parentRoute = routeMap.value.get(meta.parentPath)
    if (parentRoute) {
      items.push({
        label: parentRoute.meta?.navLabel || parentRoute.meta?.title || parentRoute.name,
        path: parentRoute.path
      })
    }
    items.push({ label: meta.title || route.name })
    return items
  }

  items.push({
    label: meta.navLabel || meta.title || route.name
  })
  return items
})

const toggleSidebar = () => {
  isSidebarOpen.value = !isSidebarOpen.value
}

const handleNavNavigate = () => {
  if (isMobile.value) {
    isSidebarOpen.value = false
  }
}

const navigateBreadcrumb = (item) => {
  if (item.path) {
    router.push(item.path)
  }
}
</script>

<style>
:root {
  font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
}

* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

html,
body,
#app {
  width: 100%;
  height: 100%;
  min-height: 100vh;
}

body {
  font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  background: #f5f7ff;
}

.app-shell {
  min-height: 100vh;
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.shell-body {
  display: flex;
  align-items: stretch;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.main-panel {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}

.breadcrumb-bar {
  height: 42px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 16px;
  background: #fff;
  border-bottom: 1px solid #eaeeff;
  overflow-x: auto;
  white-space: nowrap;
}

.breadcrumb-item {
  color: #6b7699;
  text-decoration: none;
  font-size: 13px;
  position: relative;
}

.breadcrumb-item.clickable {
  cursor: pointer;
}

.breadcrumb-item.current {
  color: #2d3f78;
  font-weight: 700;
}

.breadcrumb-item:not(:last-child)::after {
  content: "/";
  margin-left: 8px;
  color: #bbc5e6;
}

.page-content {
  width: 100%;
  flex: 1;
  min-height: 0;
  min-width: 0;
  overflow: auto;
  padding: 16px;
}

.mobile-mask {
  position: fixed;
  inset: 56px 0 0 0;
  background: rgba(21, 28, 52, 0.45);
  z-index: 1100;
}
</style>
