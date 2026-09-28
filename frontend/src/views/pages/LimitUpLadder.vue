<template>
  <div class="limit-up-ladder">
    <div class="container">
      <header class="page-header">
        <div>
          <h1>连板天梯</h1>
          <p>按最新交易日连板高度分层展示涨停梯队</p>
        </div>
        <div class="header-actions">
          <div class="trade-date">交易日：{{ tradeDateText }}</div>
          <button class="refresh-btn" @click="loadLadder" :disabled="loading">
            {{ loading ? '加载中...' : '刷新' }}
          </button>
        </div>
      </header>

      <section class="summary-grid">
        <div class="summary-card">
          <div class="label">当日涨停总数</div>
          <div class="value">{{ summary.limit_up_total }}</div>
        </div>
        <div class="summary-card">
          <div class="label">连板梯队总数</div>
          <div class="value">{{ summary.ladder_total }}</div>
        </div>
        <div class="summary-card">
          <div class="label">最高连板</div>
          <div class="value">{{ summary.highest_streak }} 板</div>
        </div>
      </section>

      <section v-if="!loading" class="ladder-board">
        <div
          v-for="bucket in buckets"
          :key="bucket.streak"
          class="bucket-column"
        >
          <div class="bucket-head">
            <div class="bucket-title">{{ bucket.label }}</div>
            <div class="bucket-count">{{ bucket.count }}</div>
          </div>

          <div v-if="bucket.items.length === 0" class="bucket-empty">暂无</div>

          <div v-else class="stock-list">
            <article
              v-for="stock in bucket.items"
              :key="stock.code"
              class="stock-card"
              @click="goToStock(stock.code)"
            >
              <div class="stock-main">
                <span class="stock-name">{{ stock.name }}</span>
                <span class="stock-code">{{ stock.code }}</span>
              </div>
              <div class="stock-sub">
                <span class="change up">{{ formatPct(stock.change_pct) }}</span>
                <span>连板 {{ stock.streak }}</span>
                <span>换手 {{ formatPct(stock.turnover_rate) }}</span>
              </div>
            </article>
          </div>
        </div>
      </section>

      <section class="history-panel" v-if="history.length > 0">
        <h2>近{{ params.history_days || 10 }}日梯队趋势</h2>
        <div class="history-table-wrap">
          <table class="history-table">
            <thead>
              <tr>
                <th>日期</th>
                <th v-for="bucket in buckets" :key="`head-${bucket.streak}`">{{ bucket.label }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in history" :key="row.date">
                <td>{{ row.date }}</td>
                <td v-for="bucket in buckets" :key="`${row.date}-${bucket.streak}`">
                  {{ row.buckets?.[String(bucket.streak)] || 0 }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { getLimitUpLadder } from '@/api/market'

const router = useRouter()
const loading = ref(false)
const buckets = ref([])
const history = ref([])
const tradeDate = ref('')
const params = ref({ min_streak: 2, max_streak: 7, history_days: 10 })
const summary = ref({ limit_up_total: 0, ladder_total: 0, highest_streak: 0 })

const tradeDateText = computed(() => tradeDate.value || '--')

const formatPct = (value) => {
  const n = Number(value || 0)
  return `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`
}

const goToStock = (code) => {
  router.push(`/stock/${code}`)
}

const loadLadder = async () => {
  loading.value = true
  try {
    const response = await getLimitUpLadder(params.value)
    if (response?.error) {
      throw new Error(response.error)
    }
    buckets.value = Array.isArray(response?.buckets) ? response.buckets : []
    history.value = Array.isArray(response?.history) ? response.history : []
    tradeDate.value = response?.trade_date || ''
    summary.value = response?.summary || { limit_up_total: 0, ladder_total: 0, highest_streak: 0 }
    params.value = response?.params || params.value
  } catch (error) {
    console.error('加载连板天梯失败:', error)
    buckets.value = []
    history.value = []
    tradeDate.value = ''
    summary.value = { limit_up_total: 0, ladder_total: 0, highest_streak: 0 }
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  loadLadder()
})
</script>

<style scoped>
.limit-up-ladder {
  min-height: calc(100vh - 60px);
  background: linear-gradient(155deg, #0b1d3b 0%, #163e73 45%, #1f6b74 100%);
  padding: 20px;
}

.container {
  max-width: 1500px;
  margin: 0 auto;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 16px;
  color: #f7fafc;
  margin-bottom: 16px;
}

.page-header h1 {
  font-size: 30px;
  margin-bottom: 6px;
}

.page-header p {
  color: rgba(247, 250, 252, 0.85);
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.trade-date {
  font-size: 13px;
  color: rgba(247, 250, 252, 0.88);
}

.refresh-btn {
  border: 1px solid rgba(255, 255, 255, 0.3);
  background: rgba(255, 255, 255, 0.14);
  color: #fff;
  border-radius: 8px;
  padding: 8px 14px;
  cursor: pointer;
}

.refresh-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}

.summary-card {
  background: rgba(248, 250, 252, 0.92);
  border-radius: 12px;
  padding: 14px 16px;
}

.summary-card .label {
  color: #5b6478;
  font-size: 13px;
  margin-bottom: 6px;
}

.summary-card .value {
  font-size: 28px;
  color: #12264a;
  font-weight: 700;
}

.ladder-board {
  display: grid;
  grid-template-columns: repeat(6, minmax(180px, 1fr));
  gap: 12px;
  align-items: start;
  margin-bottom: 16px;
}

.bucket-column {
  background: rgba(244, 247, 255, 0.96);
  border-radius: 12px;
  min-height: 260px;
  box-shadow: 0 8px 24px rgba(10, 15, 30, 0.16);
}

.bucket-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 12px 8px;
  border-bottom: 1px solid #e6ebf8;
}

.bucket-title {
  font-size: 15px;
  color: #25365d;
  font-weight: 700;
}

.bucket-count {
  background: #123a77;
  color: #fff;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  min-width: 28px;
  padding: 2px 8px;
  text-align: center;
}

.bucket-empty {
  padding: 22px 10px;
  text-align: center;
  color: #95a1bc;
}

.stock-list {
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.stock-card {
  background: #fff;
  border-radius: 8px;
  padding: 8px 10px;
  cursor: pointer;
  border: 1px solid #e8edf9;
}

.stock-card:hover {
  border-color: #9cb7f0;
}

.stock-main {
  display: flex;
  justify-content: space-between;
  gap: 6px;
  margin-bottom: 5px;
}

.stock-name {
  font-size: 14px;
  color: #1c2d50;
  font-weight: 600;
}

.stock-code {
  font-size: 12px;
  color: #6d7893;
}

.stock-sub {
  display: flex;
  justify-content: space-between;
  gap: 6px;
  color: #5a6785;
  font-size: 12px;
}

.stock-sub .up {
  color: #d22d2d;
  font-weight: 700;
}

.history-panel {
  background: rgba(244, 247, 255, 0.96);
  border-radius: 12px;
  padding: 14px;
}

.history-panel h2 {
  font-size: 16px;
  color: #25365d;
  margin-bottom: 10px;
}

.history-table-wrap {
  overflow-x: auto;
}

.history-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.history-table th,
.history-table td {
  border-bottom: 1px solid #e6ebf8;
  padding: 8px 10px;
  text-align: center;
  white-space: nowrap;
}

.history-table th:first-child,
.history-table td:first-child {
  text-align: left;
}

@media (max-width: 1280px) {
  .ladder-board {
    grid-template-columns: repeat(3, minmax(180px, 1fr));
  }
}

@media (max-width: 760px) {
  .page-header {
    flex-direction: column;
    align-items: flex-start;
  }

  .summary-grid {
    grid-template-columns: 1fr;
  }

  .ladder-board {
    grid-template-columns: 1fr;
  }
}
</style>
