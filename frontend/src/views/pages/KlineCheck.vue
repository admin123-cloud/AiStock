<template>
  <div class="kline-check">
    <div class="check-header">
      <h1>K线完整性检查</h1>
      <p>一键巡检所有类型（板块/指数/个股）的日线数据，按 code 聚合展示问题并支持分批重拉修复</p>
    </div>

    <!-- 检查配置 -->
    <div class="check-config">
      <div class="config-row">
        <div class="config-item">
          <label>返回数量：</label>
          <input v-model.number="checkAllConfig.limit_codes" type="number" min="100" max="2000" step="50" class="config-input" />
        </div>
        <div class="config-item">
          <label>日线起始：</label>
          <input v-model="checkAllConfig.start_base_date" type="date" class="config-input" />
        </div>
        <div class="config-item">
          <label>自动循环：</label>
          <label class="switch">
            <input type="checkbox" v-model="autoLoop.enabled" />
            <span class="switch-slider"></span>
          </label>
        </div>
        <div class="config-item">
          <label>每批修复：</label>
          <input v-model.number="autoLoop.batch_size" type="number" min="10" max="500" step="10" class="config-input small" />
        </div>
        <div class="config-tip">仅巡检日线（1d）</div>
      </div>

      <div class="config-actions">
        <button 
          class="check-btn"
          @click="startCheckAll"
          :disabled="checking || repairing"
        >
          <span v-if="checking">检查中...</span>
          <span v-else>一键巡检（所有类型/日线）</span>
        </button>

        <button 
          class="repair-btn"
          @click="repairSelected"
          :disabled="repairing || selectedKeys.length === 0"
        >
          <span v-if="repairing">修复中...</span>
          <span v-else>修复所选（重拉问题周期）</span>
        </button>

        <button
          class="auto-btn"
          @click="startAutoRepairLoop"
          :disabled="autoLoop.running || checking || repairing"
        >
          开始自动修复循环
        </button>
        <button
          class="stop-btn"
          @click="stopAutoRepairLoop"
          :disabled="!autoLoop.running"
        >
          停止
        </button>

        <button class="secondary-btn" @click="autoSelectTop(autoLoop.batch_size || 100)" :disabled="!checkResults.length || repairing || checking">
          自动选择前{{ autoLoop.batch_size || 100 }}
        </button>
        <button class="secondary-btn" @click="clearSelection" :disabled="selectedKeys.length === 0 || repairing || checking">
          清空选择
        </button>

        <button 
          class="export-btn"
          @click="exportResults"
          :disabled="!checkResults.length"
        >
          导出结果
        </button>
      </div>
    </div>

    <!-- 检查进度 -->
    <div v-if="checking" class="check-progress">
      <div class="progress-bar">
        <div 
          class="progress-fill" 
          :style="{ width: progressPercentage + '%' }"
        ></div>
      </div>
      <div class="progress-info">
        <span>已处理：{{ progressInfo.processed }} / {{ progressInfo.total }}</span>
        <span>完整：{{ progressInfo.complete }}</span>
        <span>待确认：{{ progressInfo.pending_confirm || 0 }}</span>
        <span>NoData: {{ progressInfo.no_data || 0 }}</span>
        <span>不完整：{{ progressInfo.incomplete }}</span>
        <span>错误：{{ progressInfo.error }}</span>
      </div>
    </div>

    <!-- 修复进度 -->
    <div v-if="repairing" class="check-progress">
      <div class="progress-bar">
        <div class="progress-fill" :style="{ width: repairPercentage + '%' }"></div>
      </div>
      <div class="progress-info">
        <span>已处理：{{ repairProgress.processed }} / {{ repairProgress.total }}</span>
        <span>成功：{{ repairProgress.success }}</span>
        <span>失败：{{ repairProgress.failed }}</span>
        <span v-if="repairProgress.current">当前：{{ repairProgress.current }}</span>
      </div>
    </div>

    <!-- 最近一次修复结果 -->
    <div v-if="repairLast && repairLast.summary" class="history-section" style="margin-top: 20px;">
      <div class="section-header">
        <h2>最近一次修复结果</h2>
        <div class="history-filters">
          <button class="export-btn" @click="exportRepairResults" :disabled="!repairLast.items || !repairLast.items.length">
            导出修复明细
          </button>
        </div>
      </div>
      <div class="results-meta">
        <span>总任务：{{ repairLast.summary.total }}</span>
        <span>成功：{{ repairLast.summary.success }}</span>
        <span>失败：{{ repairLast.summary.failed }}</span>
        <span>Improved: {{ repairLast.summary.improved || 0 }}</span>
        <span>BecameComplete: {{ repairLast.summary.became_complete || 0 }}</span>
        <span v-if="repairLast.generated_at">生成时间：{{ formatDateTime(repairLast.generated_at) }}</span>
      </div>
      <div class="results-table" v-if="repairLast.items && repairLast.items.length" style="margin-top: 12px;">
        <table>
          <thead>
            <tr>
              <th>类型</th>
              <th>代码</th>
              <th>名称</th>
              <th>周期</th>
              <th>Before Missing</th>
              <th>After Missing</th>
              <th>Delta</th>
              <th>结果</th>
              <th>错误</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(it, idx) in repairLast.items.slice(0, 50)" :key="idx">
              <td>{{ getDataTypeLabel(it.data_type) }}</td>
              <td class="mono">{{ it.code }}</td>
              <td>{{ it.name || '-' }}</td>
              <td class="mono">{{ it.period }}</td>
              <td class="mono">{{ it.before ? `${it.before.missing} (${Number(it.before.missing_rate || 0).toFixed(2)}%)` : "-" }}</td>
              <td class="mono">{{ it.after ? `${it.after.missing} (${Number(it.after.missing_rate || 0).toFixed(2)}%)` : "-" }}</td>
              <td class="mono" :class="it.improved ? 'complete' : ''">{{ it.missing_delta != null ? it.missing_delta : "-" }}</td>
              <td :class="it.success ? 'complete' : 'error'">{{ it.success ? '成功' : '失败' }}</td>
              <td class="mono">{{ it.error || '-' }}</td>
            </tr>
          </tbody>
        </table>
        <div class="config-tip" style="margin-top: 10px;">
          仅展示前 50 条（可用“导出修复明细”查看全量）
        </div>
      </div>
    </div>

    <!-- 检查结果汇总 -->
    <div v-if="checkResults.length" class="check-summary">
      <div class="summary-cards">
        <div class="summary-card complete">
          <div class="card-icon">✅</div>
          <div class="card-content">
            <div class="card-value">{{ summary.completeCount }}</div>
            <div class="card-label">完整</div>
          </div>
        </div>

        <div class="summary-card pending-confirm">
          <div class="card-icon">⚠️</div>
          <div class="card-content">
            <div class="card-value">{{ summary.pendingConfirmCount }}</div>
            <div class="card-label">待人工确认</div>
          </div>
        </div>

        <div class="summary-card error">
          <div class="card-icon">❌</div>
          <div class="card-content">
            <div class="card-value">{{ summary.actionableProblemCount }}</div>
            <div class="card-label">异常</div>
          </div>
        </div>

        <div class="summary-card rate">
          <div class="card-icon">📊</div>
          <div class="card-content">
            <div class="card-value">{{ summary.completeRate }}%</div>
            <div class="card-label">完整率</div>
          </div>
        </div>
      </div>
    </div>

    <!-- 检查结果列表 -->
    <div v-if="checkResults.length" class="check-results">
      <div class="results-header">
        <h2>检查结果（Top {{ checkResults.length }}）</h2>
        <div class="results-meta">
          <span v-if="checkAllSummary">全量 code：{{ checkAllSummary.total_codes }}，待人工确认：{{ checkAllSummary.pending_confirm_codes || 0 }}，异常：{{ checkAllSummary.actionable_problem_codes || 0 }}</span>
          <span>已选：{{ selectedKeys.length }}</span>
        </div>
      </div>

      <div class="results-table">
        <table>
          <thead>
            <tr>
              <th style="width: 44px;">
                <input type="checkbox" :checked="isAllSelected" @change="toggleSelectAll($event)" />
              </th>
              <th>类型</th>
              <th>代码</th>
              <th>名称</th>
              <th>最差周期</th>
              <th>完整周期</th>
              <th>待确认周期</th>
              <th>异常周期</th>
              <th>评分</th>
            </tr>
          </thead>
          <tbody>
            <tr 
              v-for="result in checkResults" 
              :key="result.data_type + '_' + result.code"
              :class="rowStatusClass(result)"
            >
              <td>
                <input type="checkbox" :value="makeKey(result)" v-model="selectedKeys" />
              </td>
              <td>{{ getDataTypeLabel(result.data_type) }}</td>
              <td>{{ result.code }}</td>
              <td>{{ result.name }}</td>
              <td class="mono">{{ result.worst_period || '-' }}</td>
              <td>
                <div class="period-tags">
                  <span v-for="p in (result.complete_periods || [])" :key="p" class="tag tag-ok">{{ p }}</span>
                </div>
              </td>
              <td>
                <div class="period-tags">
                  <span
                    v-for="p in (result.pending_confirm_periods || [])"
                    :key="p"
                    class="tag tag-review"
                  >
                    {{ p }}
                  </span>
                </div>
              </td>
              <td>
                <div class="period-tags">
                  <span
                    v-for="p in (result.incomplete_periods || [])"
                    :key="`warn_${p}`"
                    class="tag tag-warn"
                  >
                    {{ p }}
                  </span>
                  <span
                    v-for="p in (result.error_periods || [])"
                    :key="`err_${p}`"
                    class="tag"
                    :class="result?.period_stats?.[p]?.status === 'no_data' ? 'tag-muted' : 'tag-bad'"
                  >
                    {{ result?.period_stats?.[p]?.status === 'no_data' ? '无数据' : p }}
                  </span>
                </div>
              </td>
              <td class="mono">
                {{ Number(result.score || 0).toFixed(2) }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 历史检查记录 -->
    <div class="history-section">
      <div class="section-header">
        <h2>历史检查记录</h2>
        <div class="history-filters">
          <select v-model="historyFilter.data_type" @change="loadHistory">
            <option value="">全部类型</option>
            <option value="stock">股票</option>
            <option value="index">指数</option>
            <option value="sector">板块</option>
            <option value="all">巡检汇总</option>
            <option value="repair">修复记录</option>
          </select>
          <select v-model="historyFilter.period" @change="loadHistory">
            <option value="">全部周期</option>
            <option value="1d">日线</option>
          </select>
          <button @click="loadHistory" class="refresh-btn">刷新</button>
        </div>
      </div>

      <div v-if="historyLoading" class="history-loading">加载中...</div>
      
      <div v-else-if="historyRecords.length" class="history-list">
        <table class="history-table">
          <thead>
            <tr>
              <th>检查时间</th>
              <th>数据类型</th>
              <th>周期</th>
              <th>时间模式</th>
              <th>检查总数</th>
              <th>完整</th>
              <th>待确认</th>
              <th>异常</th>
              <th>完整率</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="record in historyRecords" :key="record.id">
              <td>{{ formatDateTime(record.check_time) }}</td>
              <td>{{ getDataTypeLabel(record.data_type) }}</td>
              <td>{{ record.period }}</td>
              <td>{{ record.time_mode === 'full' ? '历史全量' : '最近两周' }}</td>
              <td>{{ record.total_checked }}</td>
              <td class="complete">{{ record.complete_count }}</td>
              <td class="incomplete">{{ record.pending_confirm_count ?? 0 }}</td>
              <td class="error">{{ record.actionable_problem_count ?? record.error_count }}</td>
              <td :class="getCompleteRateClass(getRecordCompleteRate(record))">{{ getRecordCompleteRate(record) }}%</td>
              <td>
                <span :class="['status-badge', record.status]">{{ getStatusLabel(record.status) }}</span>
              </td>
              <td>
                <button @click="viewHistoryDetail(record.id)" class="view-btn">查看详情</button>
              </td>
            </tr>
          </tbody>
        </table>

        <!-- 分页 -->
        <div class="pagination" v-if="historyTotal > historyPageSize">
          <button @click="changeHistoryPage(historyPage - 1)" :disabled="historyPage === 1">上一页</button>
          <span>第 {{ historyPage }} 页 / 共 {{ Math.ceil(historyTotal / historyPageSize) }} 页</span>
          <button @click="changeHistoryPage(historyPage + 1)" :disabled="historyPage >= Math.ceil(historyTotal / historyPageSize)">下一页</button>
        </div>
      </div>

      <div v-else class="history-empty">
        <p>暂无历史检查记录</p>
      </div>
    </div>

    <!-- 历史记录详情弹窗 -->
    <div v-if="showHistoryDetail" class="modal-overlay" @click.self="closeHistoryDetail">
      <div class="modal-content">
        <div class="modal-header">
          <h3>检查记录详情</h3>
          <button @click="closeHistoryDetail" class="close-btn">&times;</button>
        </div>
        <div class="modal-body">
          <div v-if="historyDetailLoading" class="detail-loading">加载中...</div>
          <div v-else-if="historyDetail" class="detail-content">
            <div class="detail-summary">
              <div class="detail-item">
                <label>检查时间：</label>
                <span>{{ formatDateTime(historyDetail.check_time) }}</span>
              </div>
              <div class="detail-item">
                <label>数据类型：</label>
                <span>{{ getDataTypeLabel(historyDetail.data_type) }}</span>
              </div>
              <div class="detail-item">
                <label>K线周期：</label>
                <span>{{ historyDetail.period }}</span>
              </div>
              <div class="detail-item">
                <label>时间模式：</label>
                <span>{{ historyDetail.time_mode === 'full' ? '历史全量' : '最近两周' }}</span>
              </div>
              <div class="detail-item">
                <label>检查总数：</label>
                <span>{{ historyDetail.total_checked }}</span>
              </div>
              <div class="detail-item">
                <label>完整率：</label>
                <span :class="getCompleteRateClass(getRecordCompleteRate(historyDetail))">{{ getRecordCompleteRate(historyDetail) }}%</span>
              </div>
            </div>

            <div class="detail-stats">
              <div class="stat-box complete">
                <div class="stat-value">{{ historyDetail.complete_count }}</div>
                <div class="stat-label">完整</div>
              </div>
              <div class="stat-box pending-confirm">
                <div class="stat-value">{{ historyDetail.pending_confirm_count ?? 0 }}</div>
                <div class="stat-label">待确认</div>
              </div>
              <div class="stat-box error">
                <div class="stat-value">{{ historyDetail.actionable_problem_count ?? historyDetail.error_count }}</div>
                <div class="stat-label">异常</div>
              </div>
            </div>

            <div class="detail-table-wrapper" v-if="historyDetail.details && historyDetail.details.length">
              <h4>详细结果</h4>
              <table class="detail-table">
                <thead>
                  <tr>
                    <th>代码</th>
                    <th>名称</th>
                    <th>预期</th>
                    <th>实际</th>
                    <th>缺失</th>
                    <th>缺失率</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="item in historyDetail.details" :key="item.code" :class="item.status">
                    <td>{{ item.code }}</td>
                    <td>{{ item.name }}</td>
                    <td>{{ item.total_expected }}</td>
                    <td>{{ item.actual_count }}</td>
                    <td>{{ item.missing_count }}</td>
                    <td>{{ item.missing_rate }}%</td>
                    <td>
                      <span :class="['status-badge', item.status]">{{ getStatusText(item.status) }}</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, computed, onMounted } from 'vue'
import {
  checkKlineIntegrity,
  checkKlineAllIntegrity,
  getCheckProgress,
  getCheckResults,
  getCheckAllResults,
  repairKlineData,
  repairKlineDataBatch,
  getRepairProgress,
  getRepairResults,
  getCheckHistory,
  getCheckHistoryDetail
} from '@/api/klineCheck'

export default {
  name: 'KlineCheck',
  setup() {
    const checkAllConfig = ref({
      limit_codes: 1000,
      start_base_date: '2020-01-01'
    })
    const autoLoop = ref({
      enabled: true,
      batch_size: 100,
      running: false
    })

    const checking = ref(false)
    const repairing = ref(false)
    const checkResults = ref([])
    const currentTaskId = ref('')
    const checkAllSummary = ref(null)
    const progressInfo = ref({
      total: 0,
      processed: 0,
      complete: 0,
      pending_confirm: 0,
      incomplete: 0,
      no_data: 0,
      error: 0
    })
    const selectedKeys = ref([])
    const repairTaskId = ref('')
    const repairProgress = ref({ total: 0, processed: 0, success: 0, failed: 0, status: 'idle' })
    const repairLast = ref(null)

    // 历史检查记录
    const historyRecords = ref([])
    const historyLoading = ref(false)
    const historyPage = ref(1)
    const historyPageSize = ref(10)
    const historyTotal = ref(0)
    const historyFilter = ref({
      data_type: '',
      period: ''
    })
    const showHistoryDetail = ref(false)
    const historyDetail = ref(null)
    const historyDetailLoading = ref(false)

    const progressPercentage = computed(() => {
      if (progressInfo.value.total === 0) return 0
      return Math.round((progressInfo.value.processed / progressInfo.value.total) * 100)
    })

    const repairPercentage = computed(() => {
      if (!repairProgress.value || repairProgress.value.total === 0) return 0
      return Math.round((repairProgress.value.processed / repairProgress.value.total) * 100)
    })

    const summary = computed(() => {
      const total = Number(checkAllSummary.value?.total_codes ?? checkResults.value.length)
      const fallbackPendingConfirm = checkResults.value.filter(r => (r.pending_confirm_periods || []).length > 0).length
      const fallbackIncomplete = checkResults.value.filter(r => (r.incomplete_periods || []).length > 0 || (r.error_periods || []).length > 0).length
      const pendingConfirmCount = Number(checkAllSummary.value?.pending_confirm_codes ?? fallbackPendingConfirm)
      const actionableProblemCount = Number(checkAllSummary.value?.actionable_problem_codes ?? fallbackIncomplete)
      const noDataCount = Number(checkAllSummary.value?.no_data_codes ?? 0)
      const completeCount = Number(checkAllSummary.value?.complete_codes ?? Math.max(0, total - pendingConfirmCount - actionableProblemCount - noDataCount))
      const completeRate = total > 0 ? ((completeCount / total) * 100).toFixed(2) : '0.00'
      return { total, completeCount, pendingConfirmCount, actionableProblemCount, noDataCount, completeRate }
    })

    const makeKey = (row) => `${row.data_type}::${row.code}`

    const isAllSelected = computed(() => {
      if (!checkResults.value.length) return false
      return selectedKeys.value.length === checkResults.value.length
    })

    const toggleSelectAll = (evt) => {
      const checked = !!evt.target.checked
      if (!checked) {
        selectedKeys.value = []
        return
      }
      selectedKeys.value = checkResults.value.map(makeKey)
    }

    const clearSelection = () => {
      selectedKeys.value = []
    }

    const isRepairableRow = (row) => {
      const st = row?.period_stats?.['1d']?.status
      // no_data：数据库完全无数据/数据源不支持，修复循环应跳过
      if (st === 'no_data') return false
      if (st === 'pending_confirm') return false
      // 修复记录行不参与修复
      if (row?.data_type === 'repair') return false
      return true
    }

    const autoSelectTop = (n = 100) => {
      const picked = []
      for (const row of checkResults.value) {
        if (!isRepairableRow(row)) continue
        picked.push(row)
        if (picked.length >= n) break
      }
      selectedKeys.value = picked.map(makeKey)
    }

    const rowStatusClass = (row) => {
      const st = row?.period_stats?.['1d']?.status
      if (st === 'no_data') return 'no-data'
      if (st === 'pending_confirm') return 'pending-confirm'
      if ((row.error_periods || []).length > 0) return 'error'
      if ((row.incomplete_periods || []).length > 0) return 'incomplete'
      return 'complete'
    }

    const startCheckAll = async () => {
      checking.value = true
      checkResults.value = []
      checkAllSummary.value = null
      selectedKeys.value = []

      try {
        const resp = await checkKlineAllIntegrity(checkAllConfig.value)
        currentTaskId.value = resp.task_id

        await waitForCheckCompleted(currentTaskId.value)
        const results = await getCheckAllResults(currentTaskId.value)
        checkResults.value = results.items || []
        checkAllSummary.value = results.summary || null
        checking.value = false
      } catch (error) {
        console.error('巡检失败:', error)
        checking.value = false
        alert('巡检失败：' + error.message)
      }
    }

    const waitForCheckCompleted = async (taskId, stopSignal) => {
      // 轮询直到 completed/failed
      for (;;) {
        if (stopSignal && stopSignal()) throw new Error('stopped')
        const progress = await getCheckProgress(taskId)
        progressInfo.value = progress
        if (progress.status === 'completed') return progress
        if (progress.status === 'failed') throw new Error(progress.error || '巡检失败')
        await new Promise(resolve => setTimeout(resolve, 1000))
      }
    }

    const waitForRepairCompleted = async (taskId, stopSignal) => {
      for (;;) {
        if (stopSignal && stopSignal()) throw new Error('stopped')
        const p = await getRepairProgress(taskId)
        repairProgress.value = p
        if (p.status === 'completed') return p
        if (p.status === 'failed') throw new Error(p.error || '修复失败')
        await new Promise(resolve => setTimeout(resolve, 1000))
      }
    }

    const buildRepairBatchItems = (keys) => {
      const selectedSet = new Set(keys)
      return checkResults.value
        .filter(r => selectedSet.has(makeKey(r)))
        .filter(isRepairableRow)
        .map(r => ({
          code: r.code,
          data_type: r.data_type,
          periods: [...(r.incomplete_periods || []), ...(r.error_periods || [])]
        }))
        .filter(i => i.periods && i.periods.length)
    }

    const repairSelectedInternal = async ({ skipConfirm = false, stopSignal } = {}) => {
      if (!selectedKeys.value.length) return null
      if (!skipConfirm) {
        // 统计不可修复（无数据）条目，提示用户
        const selectedSet = new Set(selectedKeys.value)
        const selectedRows = checkResults.value.filter(r => selectedSet.has(makeKey(r)))
        const skipCount = selectedRows.filter(r => !isRepairableRow(r)).length
        const realCount = selectedRows.length - skipCount

        const tip = skipCount > 0 ? `（将自动跳过 ${skipCount} 个无数据标的）` : ''
        if (!confirm(`确定要修复所选 ${selectedRows.length} 个 code 的问题周期吗？${tip}\n将执行“重拉同步”。`)) {
          return null
        }
      }

      repairing.value = true
      repairProgress.value = { total: 0, processed: 0, success: 0, failed: 0, status: 'running' }
      repairLast.value = null

      try {
        const items = buildRepairBatchItems(selectedKeys.value)
        if (!items.length) {
          repairing.value = false
          if (!skipConfirm) {
            alert('所选条目均为“无数据/不可修复”，已跳过。')
          }
          return null
        }

        const resp = await repairKlineDataBatch({ items, start_date: checkAllConfig.value.start_base_date })
        repairTaskId.value = resp.task_id

        const p = await waitForRepairCompleted(repairTaskId.value, stopSignal)
        try {
          const r = await getRepairResults(repairTaskId.value)
          repairLast.value = r || null
        } catch (e) {
          // ignore
        }
        selectedKeys.value = []
        repairing.value = false

        return p
      } catch (error) {
        console.error('修复失败:', error)
        repairing.value = false
        if (error && error.message === 'stopped') return null
        alert('修复失败：' + error.message)
        return null
      }
    }

    const repairSelected = async () => {
      const p = await repairSelectedInternal({ skipConfirm: false })
      if (p) alert(`修复完成：成功 ${p.success}，失败 ${p.failed}。你可以继续选择下一批再修复。`)
    }

    const startAutoRepairLoop = async () => {
      if (autoLoop.value.running) return
      if (!confirm('将开始自动循环：巡检→自动选择前N→重拉修复→再巡检，直到没有问题或你点击停止。确定继续吗？')) {
        return
      }

      autoLoop.value.enabled = true
      autoLoop.value.running = true

      const shouldStop = () => !autoLoop.value.running

      try {
        // 主循环：每轮先巡检刷新，再修复一批
        // 注：修复可能很慢，stop 会在轮询点生效
        for (;;) {
          if (shouldStop()) break

          // 1) 巡检刷新
          checking.value = true
          checkResults.value = []
          checkAllSummary.value = null
          selectedKeys.value = []

          const resp = await checkKlineAllIntegrity(checkAllConfig.value)
          currentTaskId.value = resp.task_id
          await waitForCheckCompleted(currentTaskId.value, shouldStop)
          const results = await getCheckAllResults(currentTaskId.value)
          checkResults.value = results.items || []
          checkAllSummary.value = results.summary || null
          checking.value = false

          if (shouldStop()) break

          // 2) 自动选择一批
          const batchSize = Number(autoLoop.value.batch_size || 100)
          autoSelectTop(batchSize)

          if (!selectedKeys.value.length) {
            // 没有可修复对象了（或返回为空）
            break
          }

          // 3) 修复该批（不再弹确认）
          await repairSelectedInternal({ skipConfirm: true, stopSignal: shouldStop })

          // 4) 进入下一轮（会再巡检刷新）
          await new Promise(resolve => setTimeout(resolve, 300))
        }
      } catch (e) {
        if (!(e && e.message === 'stopped')) {
          console.error('自动循环异常:', e)
          alert('自动循环异常：' + (e.message || String(e)))
        }
      } finally {
        autoLoop.value.running = false
        checking.value = false
        repairing.value = false
      }
    }

    const stopAutoRepairLoop = () => {
      autoLoop.value.running = false
    }

    const exportResults = () => {
      const headers = ['类型', '代码', '名称', '最差周期', '完整周期', '待确认周期', '异常周期', '评分']
      const rows = checkResults.value.map(r => [
        getDataTypeLabel(r.data_type),
        r.code,
        r.name,
        r.worst_period || '',
        (r.complete_periods || []).join('|'),
        (r.pending_confirm_periods || []).join('|'),
        [...(r.incomplete_periods || []), ...(r.error_periods || [])].join('|'),
        r.score
      ])

      let csv = headers.join(',') + '\n'
      rows.forEach(row => {
        csv += row.map(v => `"${String(v ?? '').replaceAll('"', '""')}"`).join(',') + '\n'
      })

      const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
      const link = document.createElement('a')
      link.href = URL.createObjectURL(blob)
      link.download = `kline_check_all_${new Date().getTime()}.csv`
      link.click()
    }

    const exportRepairResults = () => {
      if (!repairLast.value || !repairLast.value.items || !repairLast.value.items.length) return
      const headers = ['类型', '代码', '名称', '周期', '结果', '错误']
      const rows = repairLast.value.items.map(it => [
        getDataTypeLabel(it.data_type),
        it.code,
        it.name || '',
        it.period,
        it.success ? '成功' : '失败',
        it.error || ''
      ])
      let csv = headers.join(',') + '\n'
      rows.forEach(row => {
        csv += row.map(v => `"${String(v ?? '').replaceAll('"', '""')}"`).join(',') + '\n'
      })
      const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
      const link = document.createElement('a')
      link.href = URL.createObjectURL(blob)
      link.download = `kline_repair_${new Date().getTime()}.csv`
      link.click()
    }

    const getStatusText = (status) => {
      const statusMap = {
        'complete': '完整',
        'pending_confirm': '待人工确认',
        'incomplete': '不完整',
        'error': '错误'
      }
      return statusMap[status] || status
    }

    // 历史检查记录相关方法
    const loadHistory = async () => {
      historyLoading.value = true
      try {
        const params = {
          page: historyPage.value,
          page_size: historyPageSize.value
        }
        if (historyFilter.value.data_type) {
          params.data_type = historyFilter.value.data_type
        }
        if (historyFilter.value.period) {
          params.period = historyFilter.value.period
        }
        
        const response = await getCheckHistory(params)
        historyRecords.value = response.items || []
        historyTotal.value = response.total || 0
      } catch (error) {
        console.error('加载历史记录失败:', error)
      } finally {
        historyLoading.value = false
      }
    }

    const changeHistoryPage = (page) => {
      historyPage.value = page
      loadHistory()
    }

    const viewHistoryDetail = async (id) => {
      showHistoryDetail.value = true
      historyDetailLoading.value = true
      try {
        const response = await getCheckHistoryDetail(id)
        historyDetail.value = response
      } catch (error) {
        console.error('加载历史记录详情失败:', error)
      } finally {
        historyDetailLoading.value = false
      }
    }

    const closeHistoryDetail = () => {
      showHistoryDetail.value = false
      historyDetail.value = null
    }

    const formatDateTime = (dateTimeStr) => {
      if (!dateTimeStr) return '-'
      const date = new Date(dateTimeStr)
      return date.toLocaleString('zh-CN')
    }

    const getDataTypeLabel = (type) => {
      const typeMap = {
        'stock': '股票',
        'index': '指数',
        'sector': '板块',
        'all': '巡检汇总',
        'repair': '修复记录'
      }
      return typeMap[type] || type
    }

    const getStatusLabel = (status) => {
      const statusMap = {
        'completed': '已完成',
        'running': '进行中',
        'failed': '失败'
      }
      return statusMap[status] || status
    }

    const getCompleteRateClass = (rate) => {
      if (rate >= 95) return 'complete'
      if (rate >= 80) return 'warning'
      return 'error'
    }

    const getRecordCompleteRate = (record) => {
      const total = Number(record?.total_checked || 0)
      const complete = Number(record?.complete_count || 0)
      if (!total) return 0
      return Number(((complete / total) * 100).toFixed(2))
    }

    onMounted(() => {
      // 加载历史记录
      loadHistory()
    })

    return {
      checkAllConfig,
      autoLoop,
      checking,
      repairing,
      checkResults,
      checkAllSummary,
      progressInfo,
      progressPercentage,
      repairPercentage,
      summary,
      selectedKeys,
      isAllSelected,
      toggleSelectAll,
      clearSelection,
      autoSelectTop,
      rowStatusClass,
      makeKey,
      startCheckAll,
      repairSelected,
      startAutoRepairLoop,
      stopAutoRepairLoop,
      exportResults,
      exportRepairResults,
      repairProgress,
      repairPercentage,
      repairLast,
      // 历史检查记录
      historyRecords,
      historyLoading,
      historyPage,
      historyPageSize,
      historyTotal,
      historyFilter,
      showHistoryDetail,
      historyDetail,
      historyDetailLoading,
      loadHistory,
      changeHistoryPage,
      viewHistoryDetail,
      closeHistoryDetail,
      formatDateTime,
      getDataTypeLabel,
      getStatusText,
      getStatusLabel,
      getCompleteRateClass,
      getRecordCompleteRate
    }
  }
}
</script>

<style scoped>
.kline-check {
  padding: 20px;
  max-width: 1400px;
  margin: 0 auto;
}

.check-header {
  text-align: center;
  margin-bottom: 30px;
}

.check-header h1 {
  font-size: 28px;
  color: #333;
  margin-bottom: 10px;
}

.check-header p {
  color: #666;
  font-size: 14px;
}

.check-config {
  background: white;
  padding: 20px;
  border-radius: 8px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
  margin-bottom: 20px;
}

.config-row {
  display: flex;
  flex-wrap: wrap;
  gap: 20px;
  margin-bottom: 20px;
}

.config-item {
  display: flex;
  align-items: center;
  gap: 10px;
}
.config-tip {
  flex: 1;
  display: flex;
  align-items: center;
  color: #64748b;
  font-size: 13px;
}

.config-item label {
  font-weight: 500;
  color: #333;
  min-width: 80px;
}

.config-select,
.config-input {
  padding: 8px 12px;
  border: 1px solid #ddd;
  border-radius: 4px;
  font-size: 14px;
  min-width: 150px;
}
.config-input.small {
  min-width: 110px;
}

.switch {
  position: relative;
  display: inline-flex;
  width: 46px;
  height: 26px;
  align-items: center;
}
.switch input {
  opacity: 0;
  width: 0;
  height: 0;
}
.switch-slider {
  position: absolute;
  cursor: pointer;
  inset: 0;
  background-color: #e5e7eb;
  border-radius: 999px;
  transition: 0.2s;
}
.switch-slider:before {
  position: absolute;
  content: "";
  height: 20px;
  width: 20px;
  left: 3px;
  top: 3px;
  background-color: white;
  border-radius: 999px;
  transition: 0.2s;
  box-shadow: 0 2px 6px rgba(0,0,0,.18);
}
.switch input:checked + .switch-slider {
  background-color: #1890ff;
}
.switch input:checked + .switch-slider:before {
  transform: translateX(20px);
}

.config-actions {
  display: flex;
  gap: 10px;
  justify-content: flex-end;
}

.check-btn,
.repair-btn,
.export-btn {
  padding: 10px 20px;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  transition: all 0.3s;
}

.secondary-btn {
  padding: 10px 16px;
  border: 1px solid #d9d9d9;
  background: #fff;
  border-radius: 4px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  transition: all 0.3s;
}

.auto-btn {
  padding: 10px 16px;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 600;
  background: #7c3aed;
  color: #fff;
  transition: all 0.3s;
}
.auto-btn:hover:not(:disabled) {
  background: #6d28d9;
}
.auto-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.stop-btn {
  padding: 10px 16px;
  border: 1px solid #fecaca;
  background: #fff1f2;
  color: #be123c;
  border-radius: 4px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 600;
  transition: all 0.3s;
}
.stop-btn:hover:not(:disabled) {
  background: #ffe4e6;
}
.stop-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.secondary-btn:hover:not(:disabled) {
  border-color: #1890ff;
  color: #1890ff;
}

.secondary-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.check-btn {
  background: #1890ff;
  color: white;
}

.check-btn:hover:not(:disabled) {
  background: #40a9ff;
}

.repair-btn {
  background: #52c41a;
  color: white;
}

.repair-btn:hover:not(:disabled) {
  background: #73d13d;
}

.export-btn {
  background: #faad14;
  color: white;
}

.export-btn:hover:not(:disabled) {
  background: #ffc53d;
}

.check-btn:disabled,
.repair-btn:disabled,
.export-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.check-progress {
  background: white;
  padding: 20px;
  border-radius: 8px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
  margin-bottom: 20px;
}

.progress-bar {
  width: 100%;
  height: 30px;
  background: #f0f0f0;
  border-radius: 15px;
  overflow: hidden;
  margin-bottom: 15px;
}

.progress-fill {
  height: 100%;
  background: linear-gradient(90deg, #1890ff, #40a9ff);
  transition: width 0.3s;
  display: flex;
  align-items: center;
  justify-content: center;
  color: white;
  font-weight: 500;
}

.progress-info {
  display: flex;
  justify-content: space-around;
  font-size: 14px;
  color: #666;
}

.check-summary {
  background: white;
  padding: 20px;
  border-radius: 8px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
  margin-bottom: 20px;
}

.summary-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 20px;
}

.summary-card {
  display: flex;
  align-items: center;
  gap: 15px;
  padding: 20px;
  border-radius: 8px;
  background: #f9f9f9;
}

.summary-card.complete {
  background: #f6ffed;
  border: 1px solid #b7eb8f;
}

.summary-card.incomplete,
.summary-card.pending-confirm {
  background: #fffbe6;
  border: 1px solid #ffe58f;
}

.summary-card.error {
  background: #fff1f0;
  border: 1px solid #ffccc7;
}

.summary-card.rate {
  background: #e6f7ff;
  border: 1px solid #91d5ff;
}

.card-icon {
  font-size: 32px;
}

.card-content {
  flex: 1;
}

.card-value {
  font-size: 24px;
  font-weight: bold;
  color: #333;
  margin-bottom: 5px;
}

.card-label {
  font-size: 14px;
  color: #666;
}

.check-results {
  background: white;
  padding: 20px;
  border-radius: 8px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
}

.results-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}

.results-header h2 {
  font-size: 20px;
  color: #333;
}

.filter-options {
  display: flex;
  gap: 20px;
}

.results-meta {
  display: flex;
  gap: 16px;
  color: #666;
  font-size: 13px;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
}

.period-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.tag {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  border: 1px solid transparent;
  background: #f8fafc;
}

.tag-ok {
  background: #f6ffed;
  border-color: #b7eb8f;
  color: #389e0d;
}
.tag-warn {
  background: #fffbe6;
  border-color: #ffe58f;
  color: #d48806;
}
.tag-review {
  background: #fff7e6;
  border-color: #ffd591;
  color: #d46b08;
}
.tag-bad {
  background: #fff1f0;
  border-color: #ffccc7;
  color: #cf1322;
}

.tag-muted {
  background: #f1f5f9;
  border-color: #cbd5e1;
  color: #475569;
}

.filter-options label {
  display: flex;
  align-items: center;
  gap: 5px;
  cursor: pointer;
}

.results-table {
  overflow-x: auto;
}

.results-table table {
  width: 100%;
  border-collapse: collapse;
}

.results-table th,
.results-table td {
  padding: 12px;
  text-align: left;
  border-bottom: 1px solid #e8e8e8;
}

.results-table th {
  background: #fafafa;
  font-weight: 500;
  color: #333;
}

.results-table tbody tr:hover {
  background: #f5f5f5;
}

.results-table tbody tr.complete {
  background: #f6ffed;
}

.results-table tbody tr.incomplete {
  background: #fffbe6;
}

.results-table tbody tr.error {
  background: #fff1f0;
}

.results-table tbody tr.no-data {
  background: #f8fafc;
}

.results-table tbody tr.pending-confirm {
  background: #fffaf0;
}

.status-complete {
  color: #52c41a;
  font-weight: 500;
}

.status-incomplete {
  color: #faad14;
  font-weight: 500;
}

.status-error {
  color: #f5222d;
  font-weight: 500;
}

.repair-single-btn {
  padding: 5px 10px;
  background: #52c41a;
  color: white;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
}

.repair-single-btn:hover:not(:disabled) {
  background: #73d13d;
}

.repair-single-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 历史检查记录样式 */
.history-section {
  margin-top: 30px;
  background: white;
  padding: 20px;
  border-radius: 8px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
}

.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}

.section-header h2 {
  font-size: 20px;
  color: #333;
  margin: 0;
}

.history-filters {
  display: flex;
  gap: 10px;
  align-items: center;
}

.history-filters select {
  padding: 6px 12px;
  border: 1px solid #ddd;
  border-radius: 4px;
}

.refresh-btn {
  padding: 6px 16px;
  background: #1890ff;
  color: white;
  border: none;
  border-radius: 4px;
  cursor: pointer;
}

.refresh-btn:hover {
  background: #40a9ff;
}

.history-loading,
.history-empty {
  text-align: center;
  padding: 40px;
  color: #999;
}

.history-table {
  width: 100%;
  border-collapse: collapse;
}

.history-table th,
.history-table td {
  padding: 12px;
  text-align: left;
  border-bottom: 1px solid #f0f0f0;
}

.history-table th {
  background: #fafafa;
  font-weight: 500;
  color: #333;
}

.history-table .complete {
  color: #52c41a;
}

.history-table .incomplete {
  color: #faad14;
}

.history-table .error {
  color: #ff4d4f;
}

.history-table .warning {
  color: #faad14;
}

.status-badge {
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
}

.status-badge.completed {
  background: #f6ffed;
  color: #52c41a;
  border: 1px solid #b7eb8f;
}

.status-badge.pending_confirm {
  background: #fff7e6;
  color: #d46b08;
  border: 1px solid #ffd591;
}

.status-badge.running {
  background: #e6f7ff;
  color: #1890ff;
  border: 1px solid #91d5ff;
}

.status-badge.failed {
  background: #fff1f0;
  color: #ff4d4f;
  border: 1px solid #ffa39e;
}

.view-btn {
  padding: 4px 12px;
  background: #1890ff;
  color: white;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
}

.view-btn:hover {
  background: #40a9ff;
}

/* 分页样式 */
.pagination {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 10px;
  margin-top: 20px;
}

.pagination button {
  padding: 6px 12px;
  background: white;
  border: 1px solid #d9d9d9;
  border-radius: 4px;
  cursor: pointer;
}

.pagination button:hover:not(:disabled) {
  border-color: #1890ff;
  color: #1890ff;
}

.pagination button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 弹窗样式 */
.modal-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  justify-content: center;
  align-items: center;
  z-index: 1000;
}

.modal-content {
  background: white;
  border-radius: 8px;
  width: 90%;
  max-width: 1000px;
  max-height: 90vh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 20px;
  border-bottom: 1px solid #f0f0f0;
}

.modal-header h3 {
  margin: 0;
  font-size: 18px;
  color: #333;
}

.close-btn {
  background: none;
  border: none;
  font-size: 24px;
  color: #999;
  cursor: pointer;
}

.close-btn:hover {
  color: #333;
}

.modal-body {
  padding: 20px;
  overflow-y: auto;
  flex: 1;
}

.detail-loading {
  text-align: center;
  padding: 40px;
  color: #999;
}

.detail-summary {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
  margin-bottom: 20px;
}

.detail-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.detail-item label {
  color: #666;
  font-size: 12px;
}

.detail-item span {
  color: #333;
  font-weight: 500;
}

.detail-stats {
  display: flex;
  gap: 16px;
  margin-bottom: 20px;
}

.stat-box {
  flex: 1;
  padding: 16px;
  border-radius: 8px;
  text-align: center;
}

.stat-box.complete {
  background: #f6ffed;
  border: 1px solid #b7eb8f;
}

.stat-box.incomplete,
.stat-box.pending-confirm {
  background: #fffbe6;
  border: 1px solid #ffe58f;
}

.stat-box.error {
  background: #fff1f0;
  border: 1px solid #ffa39e;
}

.stat-value {
  font-size: 24px;
  font-weight: bold;
  margin-bottom: 4px;
}

.stat-box.complete .stat-value {
  color: #52c41a;
}

.stat-box.incomplete .stat-value {
  color: #faad14;
}

.stat-box.error .stat-value {
  color: #ff4d4f;
}

.stat-label {
  font-size: 12px;
  color: #666;
}

.detail-table-wrapper {
  margin-top: 20px;
}

.detail-table-wrapper h4 {
  margin-bottom: 12px;
  color: #333;
}

.detail-table {
  width: 100%;
  border-collapse: collapse;
}

.detail-table th,
.detail-table td {
  padding: 10px;
  text-align: left;
  border-bottom: 1px solid #f0f0f0;
}

.detail-table th {
  background: #fafafa;
  font-weight: 500;
  color: #333;
}

.detail-table tr.complete {
  background: #f6ffed;
}

.detail-table tr.incomplete {
  background: #fffbe6;
}

.detail-table tr.error {
  background: #fff1f0;
}
</style>
