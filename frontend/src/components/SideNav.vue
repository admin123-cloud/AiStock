<template>
  <aside class="side-nav" :class="{ open: open }">
    <div class="search-box">
      <input
        v-model.trim="searchKeyword"
        type="text"
        placeholder="搜索页面..."
      />
    </div>

    <div class="group-list">
      <section v-for="group in filteredGroups" :key="group.key" class="group">
        <button class="group-header" @click="toggleGroup(group.key)">
          <span class="arrow" :class="{ expanded: isGroupExpanded(group.key) }">▸</span>
          <span class="group-title">{{ group.label }}</span>
        </button>
        <div v-show="isGroupExpanded(group.key)" class="group-children">
          <router-link
            v-for="item in group.items"
            :key="item.path"
            :to="item.path"
            class="nav-item"
            :class="{ active: isRouteActive(item.path) }"
            @click="handleNavigate(item.path)"
          >
            <span class="item-icon">{{ item.icon || "•" }}</span>
            <span class="item-title">{{ item.label }}</span>
          </router-link>
        </div>
      </section>
    </div>
  </aside>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const props = defineProps({
  open: {
    type: Boolean,
    default: false
  },
  isMobile: {
    type: Boolean,
    default: false
  }
})

const emit = defineEmits(['update:open', 'navigate'])

const route = useRoute()
const router = useRouter()

const EXPANDED_KEY = 'aistock_nav_expanded_groups'
const LAST_VISITED_KEY = 'aistock_last_nav_path'

const searchKeyword = ref('')
const expandedGroups = ref([])

const menuGroups = computed(() => {
  const routeList = router
    .getRoutes()
    .filter((item) => item.meta?.group && !item.meta?.hiddenInNav && !item.path.includes(':'))
    .map((item) => ({
      path: item.path,
      label: item.meta?.navLabel || item.meta?.title || item.name,
      icon: item.meta?.icon || '',
      group: item.meta?.group,
      groupLabel: item.meta?.groupLabel || item.meta?.group,
      groupOrder: Number(item.meta?.groupOrder || 99),
      navOrder: Number(item.meta?.navOrder || 99)
    }))
    .sort((a, b) => {
      if (a.groupOrder !== b.groupOrder) return a.groupOrder - b.groupOrder
      return a.navOrder - b.navOrder
    })

  const groupMap = new Map()
  routeList.forEach((item) => {
    if (!groupMap.has(item.group)) {
      groupMap.set(item.group, {
        key: item.group,
        label: item.groupLabel,
        order: item.groupOrder,
        items: []
      })
    }
    groupMap.get(item.group).items.push(item)
  })
  return Array.from(groupMap.values()).sort((a, b) => a.order - b.order)
})

const filteredGroups = computed(() => {
  const keyword = searchKeyword.value.toLowerCase()
  if (!keyword) return menuGroups.value
  return menuGroups.value
    .map((group) => ({
      ...group,
      items: group.items.filter((item) => item.label.toLowerCase().includes(keyword))
    }))
    .filter((group) => group.items.length > 0)
})

const isGroupExpanded = (groupKey) => expandedGroups.value.includes(groupKey)

const toggleGroup = (groupKey) => {
  if (isGroupExpanded(groupKey)) {
    expandedGroups.value = expandedGroups.value.filter((item) => item !== groupKey)
  } else {
    expandedGroups.value = [...expandedGroups.value, groupKey]
  }
  localStorage.setItem(EXPANDED_KEY, JSON.stringify(expandedGroups.value))
}

const isRouteActive = (path) => {
  if (path === '/') return route.path === '/'
  return route.path === path || route.path.startsWith(`${path}/`)
}

const ensureCurrentGroupExpanded = () => {
  const currentGroup = route.meta?.group
  if (!currentGroup) return
  if (!expandedGroups.value.includes(currentGroup)) {
    expandedGroups.value = [...expandedGroups.value, currentGroup]
    localStorage.setItem(EXPANDED_KEY, JSON.stringify(expandedGroups.value))
  }
}

const handleNavigate = (path) => {
  localStorage.setItem(LAST_VISITED_KEY, path)
  emit('navigate')
  if (props.isMobile) {
    emit('update:open', false)
  }
}

const bootstrapExpandedGroups = () => {
  try {
    const saved = JSON.parse(localStorage.getItem(EXPANDED_KEY) || '[]')
    if (Array.isArray(saved)) {
      expandedGroups.value = saved
    }
  } catch (error) {
    expandedGroups.value = []
  }

  if (expandedGroups.value.length === 0) {
    const currentGroup = route.meta?.group
    if (currentGroup) {
      expandedGroups.value = [currentGroup]
    }
  }
  localStorage.setItem(EXPANDED_KEY, JSON.stringify(expandedGroups.value))
}

bootstrapExpandedGroups()
ensureCurrentGroupExpanded()

watch(
  () => route.fullPath,
  () => {
    ensureCurrentGroupExpanded()
    localStorage.setItem(LAST_VISITED_KEY, route.path)
  }
)
</script>

<style scoped>
.side-nav {
  width: 248px;
  flex: 0 0 248px;
  background: linear-gradient(180deg, #f8fbff 0%, #f4f7ff 100%);
  border-right: 1px solid #e5ebff;
  height: 100%;
  min-height: 0;
  overflow-y: auto;
  padding: 12px 10px;
  transition: transform 0.2s ease;
}

.search-box {
  margin-bottom: 10px;
}

.search-box input {
  width: 100%;
  border: 1px solid #d7e0ff;
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 13px;
  outline: none;
}

.search-box input:focus {
  border-color: #6c8cff;
  box-shadow: 0 0 0 2px rgba(108, 140, 255, 0.15);
}

.group {
  margin-bottom: 8px;
}

.group-header {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  border: none;
  background: transparent;
  cursor: pointer;
  padding: 8px 10px;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 700;
  color: #2a3563;
}

.group-header:hover {
  background: #eaf0ff;
}

.arrow {
  transition: transform 0.2s ease;
}

.arrow.expanded {
  transform: rotate(90deg);
}

.group-children {
  margin-top: 4px;
  padding-left: 6px;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 8px;
  text-decoration: none;
  color: #42507f;
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 13px;
  margin-bottom: 3px;
}

.nav-item:hover {
  background: #edf2ff;
}

.nav-item.active {
  background: linear-gradient(135deg, #5d7eff 0%, #6f67ff 100%);
  color: #fff;
  font-weight: 600;
}

.item-icon {
  width: 18px;
  text-align: center;
}

@media (max-width: 1024px) {
  .side-nav {
    position: fixed;
    left: 0;
    top: 56px;
    z-index: 1200;
    transform: translateX(-100%);
    box-shadow: 8px 0 24px rgba(33, 45, 88, 0.2);
  }

  .side-nav.open {
    transform: translateX(0);
  }
}
</style>
