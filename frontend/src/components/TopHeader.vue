<template>
  <header class="top-header">
    <div class="left-section">
      <button class="menu-btn" @click="$emit('toggle-sidebar')">☰</button>
      <router-link to="/" class="brand-link">
        <span class="brand-title">AiStock</span>
      </router-link>
      <div class="system-badge">
        <span class="dot"></span>
        <span>系统运行中</span>
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
  background: #fff;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 18px;
  color: #4b5565;
  position: sticky;
  top: 0;
  z-index: 1300;
  border-bottom: 1px solid #edf0f6;
  box-shadow: none;
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

.left-section { visibility: hidden; width: 1px; overflow: hidden; }

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
  color: #687386;
}

.user-box {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border-radius: 999px;
  background: #fff;
  border: 1px solid #e5e7eb;
  font-size: 13px;
  color: #4b5565;
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
