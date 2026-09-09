<template>
  <header class="top-header">
    <div class="left-section">
      <button class="menu-btn" @click="$emit('toggle-sidebar')">☰</button>
      <router-link to="/" class="brand-link">
        <span class="brand-title">AiStock</span>
      </router-link>
      <div class="system-badge">
        <span>策略观察工作台</span>
      </div>
    </div>

    <div class="right-section">
      <span class="time-text">{{ nowText }}</span>
      <div class="user-box">
        <span class="avatar">👤</span>
        <span>访客</span>
      </div>
    </div>
  </header>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'

defineEmits(['toggle-sidebar'])

const nowText = ref('')
let timer = null

const formatNow = () => {
  const now = new Date()
  const yyyy = now.getFullYear()
  const mm = String(now.getMonth() + 1).padStart(2, '0')
  const dd = String(now.getDate()).padStart(2, '0')
  const hh = String(now.getHours()).padStart(2, '0')
  const mi = String(now.getMinutes()).padStart(2, '0')
  return `${yyyy}-${mm}-${dd} ${hh}:${mi}`
}

onMounted(() => {
  nowText.value = formatNow()
  timer = window.setInterval(() => {
    nowText.value = formatNow()
  }, 30000)
})

onBeforeUnmount(() => {
  if (timer) {
    window.clearInterval(timer)
    timer = null
  }
})
</script>

<style scoped>
.top-header {
  height: 56px;
  background: linear-gradient(90deg, #5c74e9 0%, #785ec9 100%);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 14px;
  color: #fff;
  position: sticky;
  top: 0;
  z-index: 1300;
  box-shadow: 0 2px 12px rgba(33, 45, 88, 0.25);
}

.left-section,
.right-section {
  display: flex;
  align-items: center;
  gap: 10px;
}

.menu-btn {
  border: 1px solid rgba(255, 255, 255, 0.35);
  background: rgba(255, 255, 255, 0.18);
  color: #fff;
  border-radius: 8px;
  width: 34px;
  height: 34px;
  cursor: pointer;
  font-size: 16px;
}

.menu-btn:hover {
  background: rgba(255, 255, 255, 0.28);
}

.brand-link {
  color: #fff;
  text-decoration: none;
}

.brand-title {
  font-size: 24px;
  font-weight: 800;
  letter-spacing: 0.3px;
}

.system-badge {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.15);
  font-size: 12px;
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #4fe37d;
}

.time-text {
  font-size: 12px;
  opacity: 0.92;
}

.user-box {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.15);
  font-size: 13px;
}

@media (max-width: 1024px) {
  .brand-title {
    font-size: 20px;
  }

  .system-badge {
    display: none;
  }
}

@media (min-width: 1025px) {
  .menu-btn {
    display: none;
  }
}
</style>
