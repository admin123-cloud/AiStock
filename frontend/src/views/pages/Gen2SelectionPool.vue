<template>
  <div class="gen2-selection-page">
    <section class="top-band">
      <div>
        <p class="eyebrow">Gen2 Selection Pool</p>
        <h1>策略股票池</h1>
        <p class="subtitle">{{ strategyName }} 路径 {{ selectedDate || '--' }}</p>
      </div>
      <div class="actions">
        <el-date-picker
          v-model="signalDate"
          type="date"
          value-format="YYYY-MM-DD"
          format="YYYY-MM-DD"
          placeholder="选择信号日"
          clearable
          :disabled-date="disabledSignalDate"
          style="width: 180px"
          @change="fetchData"
        />
        <el-button :icon="Search" type="primary" :loading="loading" @click="fetchData">查询</el-button>
        <el-button :icon="Refresh" :loading="updateLoading" @click="updatePool">更新当日实时池</el-button>
      </div>
    </section>

    <el-alert
      v-if="!available && !loading"
      type="warning"
      :closable="false"
      :title="message || 'Selection pool data is unavailable'"
    />

    <template v-if="available">
      <el-alert
        v-if="fallbackNotice"
        type="warning"
        :closable="false"
        show-icon
        :title="fallbackNotice"
      />

      <el-alert
        v-if="branchFreshnessWarning"
        type="warning"
        :closable="false"
        show-icon
        :title="branchFreshnessWarning"
      />

      <el-alert
        v-if="dateResolutionNotice"
        type="warning"
        :closable="false"
        show-icon
        :title="dateResolutionNotice"
      />

      <el-alert
        v-if="officialRebuildNotice"
        type="warning"
        :closable="false"
        :show-icon="true"
        :title="officialRebuildNotice"
      />

      <section class="pipeline">
        <div v-for="(step, index) in pipeline" :key="step.key" class="pipeline-step">
          <div class="step-index">{{ index + 1 }}</div>
          <div class="step-body">
            <span>{{ step.label }}</span>
            <strong>{{ step.count ?? 0 }}</strong>
            <p>{{ step.note }}</p>
          </div>
        </div>
      </section>

      <section class="status-strip">
        <div class="status-item">
          <span>请求日期</span>
          <strong>{{ requestedDate || '--' }}</strong>
        </div>
        <div class="status-item">
          <span>实际展示</span>
          <strong>{{ selectedDate || '--' }}</strong>
        </div>
        <div class="status-item">
          <span>策略模式</span>
          <strong>{{ strategyMode || '--' }}</strong>
        </div>
        <div class="status-item">
          <span>可买观察</span>
          <strong class="up">{{ buyableCount }}</strong>
        </div>
        <div class="status-item">
          <span>数据状态</span>
          <strong :class="{ warn: freshnessWarning }">{{ freshnessText }}</strong>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>当日触发信号</h2>
            <p>{{ selectedDate || '--' }} 盘中已触发 G2 30m 底分型放量确认的个股</p>
          </div>
          <el-tag effect="plain">{{ triggerRows.length }} 只</el-tag>
        </div>

        <el-table
          v-loading="loading"
          :data="triggerRows"
          stripe
          size="small"
          height="360"
          empty-text="暂无当日触发信号"
        >
          <el-table-column label="状态" width="118" fixed>
            <template #default="{ row }">
              <el-tag :type="row.stage_type || 'info'" effect="light">{{ row.stage_label || '--' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="代码" width="126" fixed>
            <template #default="{ row }">
              <button class="copy-code" type="button" title="复制代码" @click="copyCode(row.code)">
                <span>{{ row.code }}</span>
                <el-icon><DocumentCopy /></el-icon>
              </button>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="名称" min-width="110" fixed />
          <el-table-column prop="confirm_datetime" label="盘中确认" width="165" />
          <el-table-column label="V4排名" width="90" align="right">
            <template #default="{ row }">{{ row.v4_rank || '--' }}</template>
          </el-table-column>
          <el-table-column label="盘中涨幅" width="100" align="right">
            <template #default="{ row }">{{ pctNumber(row.rt_return_pct) }}</template>
          </el-table-column>
          <el-table-column label="30m量比" width="100" align="right">
            <template #default="{ row }">{{ score(row.amount_ratio, 2) }}</template>
          </el-table-column>
          <el-table-column label="路径" min-width="240">
            <template #default="{ row }">
              <div class="path-line">
                <el-tag :type="row.pass_risk_cool ? 'success' : 'danger'" effect="plain">风控</el-tag>
                <el-tag :type="row.pass_mainline1 ? 'success' : 'info'" effect="plain">volume5</el-tag>
                <el-tag :type="row.pass_mainline2 ? 'success' : 'info'" effect="plain">突破</el-tag>
                <el-tag :type="row.buyable ? 'success' : 'info'" effect="plain">可买</el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="reason_text" label="记录说明" min-width="260" show-overflow-tooltip />
        </el-table>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>完整第二代</h2>
            <p>展示第二代完整体，含 volume5 主线与突破主线选出的全部股票</p>
          </div>
          <el-tag effect="plain">{{ completeRows.length }} 只</el-tag>
        </div>

        <el-table
          v-loading="loading"
          :data="completeRows"
          stripe
          size="small"
          height="420"
          empty-text="暂无完整主线信号"
        >
          <el-table-column label="体系" width="170" fixed>
            <template #default="{ row }">
              <div class="path-line">
                <el-tag v-if="row.pass_mainline1" type="success" effect="light">volume5</el-tag>
                <el-tag v-if="row.pass_mainline2" type="warning" effect="light">突破+板块</el-tag>
                <el-tag v-if="row.pass_breakout_stage" type="warning" effect="light">{{ row.breakout_stage_label || '二突观察' }}</el-tag>
                <el-tag v-if="!row.pass_mainline1 && !row.pass_mainline2 && !row.pass_breakout_stage" effect="light">观察</el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="代码" width="126" fixed>
            <template #default="{ row }">
              <button class="copy-code" type="button" title="复制代码" @click="copyCode(row.code)">
                <span>{{ row.code }}</span>
                <el-icon><DocumentCopy /></el-icon>
              </button>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="名称" min-width="110" fixed />
          <el-table-column prop="confirm_datetime" label="盘中确认" width="165" />
          <el-table-column label="V4排名" width="90" align="right">
            <template #default="{ row }">{{ row.v4_rank || '--' }}</template>
          </el-table-column>
          <el-table-column v-if="qualityMode === 'volume5'" label="volume5分" width="105" align="right">
            <template #default="{ row }">{{ score(row.alpha191_volume5_score) }}</template>
          </el-table-column>
          <el-table-column v-if="qualityMode === 'volume5'" label="volume5排名" width="110" align="right">
            <template #default="{ row }">{{ row.alpha191_volume5_rank_in_day || '--' }}</template>
          </el-table-column>
          <el-table-column label="L3强度%" width="100" align="right">
            <template #default="{ row }">{{ pct(row.l3_rt_strong3_ratio) }}</template>
          </el-table-column>
          <el-table-column label="60日低点涨幅%" width="120" align="right">
            <template #default="{ row }">{{ pct(row.runup_from_60d_low) }}</template>
          </el-table-column>
          <el-table-column prop="reason_text" label="记录说明" min-width="260" show-overflow-tooltip />
        </el-table>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>晋级池</h2>
            <p>{{ message }}</p>
          </div>
          <el-segmented v-model="stageFilter" :options="stageOptions" size="small" />
        </div>

        <el-table
          v-loading="loading"
          :data="promotedVisibleRows"
          stripe
          size="small"
          height="560"
          empty-text="暂无选股池记录"
        >
          <el-table-column label="状态" width="118" fixed>
            <template #default="{ row }">
              <el-tag :type="row.stage_type || 'info'" effect="light">{{ row.stage_label || '--' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="代码" width="126" fixed>
            <template #default="{ row }">
              <button class="copy-code" type="button" title="复制代码" @click="copyCode(row.code)">
                <span>{{ row.code }}</span>
                <el-icon><DocumentCopy /></el-icon>
              </button>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="名称" min-width="110" fixed />
          <el-table-column prop="confirm_datetime" label="盘中确认" width="165" />
          <el-table-column label="路径" min-width="280">
            <template #default="{ row }">
              <div class="path-line">
                <el-tag :type="row.pass_v4_pool ? 'success' : 'info'" effect="plain">V4筛选</el-tag>
                <el-tag :type="row.pass_v4_g2_trigger ? 'success' : 'info'" effect="plain">G2触发</el-tag>
                <el-tag :type="row.pass_risk_cool ? 'success' : 'danger'" effect="plain">风控</el-tag>
                <el-tag :type="row.pass_mainline1 ? 'success' : 'warning'" effect="plain">主线一</el-tag>
                <el-tag :type="row.pass_mainline2 || row.pass_breakout_stage ? 'success' : 'info'" effect="plain">主线二</el-tag>
                <el-tag v-if="row.pass_volume5_trigger" type="success" effect="plain">v5触发</el-tag>
                <el-tag v-if="row.pass_breakout_stage" type="warning" effect="plain">{{ row.breakout_stage_label || '二突层级' }}</el-tag>
                <el-tag :type="row.buyable ? 'success' : 'info'" effect="plain">可买</el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="V4排名" width="90" align="right">
            <template #default="{ row }">{{ row.v4_rank || '--' }}</template>
          </el-table-column>
          <el-table-column label="主线分" width="105" align="right">
            <template #default="{ row }">{{ score(row.alpha191_volume5_score) }}</template>
          </el-table-column>
          <el-table-column label="主线排名" width="95" align="right">
            <template #default="{ row }">{{ row.alpha191_volume5_rank_in_day || '--' }}</template>
          </el-table-column>
          <el-table-column label="L3强度%" width="100" align="right">
            <template #default="{ row }">{{ pct(row.l3_rt_strong3_ratio) }}</template>
          </el-table-column>
          <el-table-column label="60日低点涨幅%" width="120" align="right">
            <template #default="{ row }">{{ pct(row.runup_from_60d_low) }}</template>
          </el-table-column>
          <el-table-column label="盘中涨幅" width="100" align="right">
            <template #default="{ row }">{{ pctNumber(row.rt_return_pct) }}</template>
          </el-table-column>
          <el-table-column label="30m量比" width="100" align="right">
            <template #default="{ row }">{{ score(row.amount_ratio, 2) }}</template>
          </el-table-column>
          <el-table-column prop="reason_text" label="记录说明" min-width="260" show-overflow-tooltip />
        </el-table>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>候选股质量排序</h2>
            <p>D-1 V4参考池；Alpha150/070/095/132/144 计算 volume5，再筛 keep80 + runup&lt;=100%</p>
          </div>
          <div class="panel-tools">
            <el-segmented v-model="qualityMode" :options="qualityModeOptions" size="small" />
            <el-tag type="success" effect="plain">通过 {{ qualityVisiblePassCount }} 只</el-tag>
            <el-tag effect="plain">{{ qualityVisibleRows.length }} 只</el-tag>
          </div>
        </div>

        <el-table
          v-loading="loading"
          :data="qualityVisibleRows"
          stripe
          size="small"
          height="520"
          empty-text="暂无候选股质量排序"
        >
          <el-table-column label="质量" width="105" fixed>
            <template #default="{ row }">
              <el-tag :type="row.quality_type || 'info'" effect="light">{{ row.quality_label || '--' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="代码" width="126" fixed>
            <template #default="{ row }">
              <button class="copy-code" type="button" title="复制代码" @click="copyCode(row.code)">
                <span>{{ row.code }}</span>
                <el-icon><DocumentCopy /></el-icon>
              </button>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="名称" min-width="110" fixed />
          <el-table-column label="V4排名" width="90" align="right">
            <template #default="{ row }">{{ row.v4_rank || '--' }}</template>
          </el-table-column>
          <el-table-column label="V4总分" width="100" align="right">
            <template #default="{ row }">{{ score(row.v4_score) }}</template>
          </el-table-column>
          <el-table-column label="volume5分" width="105" align="right">
            <template #default="{ row }">{{ score(row.alpha191_volume5_score) }}</template>
          </el-table-column>
          <el-table-column label="volume5排名" width="110" align="right">
            <template #default="{ row }">{{ row.alpha191_volume5_rank_in_day || '--' }}</template>
          </el-table-column>
          <el-table-column v-if="qualityMode === 'volume5'" label="keep80阈值" width="105" align="right">
            <template #default="{ row }">{{ score(row.alpha191_gate_threshold) }}</template>
          </el-table-column>
          <el-table-column v-if="qualityMode === 'volume5'" label="keep80" width="90" align="center">
            <template #default="{ row }">
              <el-tag :type="row.pass_keep80 ? 'success' : 'warning'" effect="plain">
                {{ row.pass_keep80 ? '通过' : '未过' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column v-if="qualityMode === 'volume5'" label="60日低点涨幅%" width="125" align="right">
            <template #default="{ row }">{{ pct(row.runup_from_60d_low) }}</template>
          </el-table-column>
          <el-table-column v-if="qualityMode === 'volume5'" label="runup" width="90" align="center">
            <template #default="{ row }">
              <el-tag :type="row.pass_runup ? 'success' : 'warning'" effect="plain">
                {{ row.pass_runup ? '通过' : '未过' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column v-if="qualityMode === 'breakout'" label="层级" width="120">
            <template #default="{ row }">{{ row.breakout_stage_label || '--' }}</template>
          </el-table-column>
          <el-table-column v-if="qualityMode === 'breakout'" label="大阳线" width="115">
            <template #default="{ row }">{{ row.setup_big_bull_date || '--' }}</template>
          </el-table-column>
          <el-table-column v-if="qualityMode === 'breakout'" label="距箱体" width="100" align="right">
            <template #default="{ row }">{{ pct(row.close_vs_box_top) }}</template>
          </el-table-column>
          <el-table-column v-if="qualityMode === 'breakout'" label="箱体宽度" width="105" align="right">
            <template #default="{ row }">{{ pct(row.setup_box_range) }}</template>
          </el-table-column>
          <el-table-column v-if="qualityMode === 'breakout'" label="大阳量比" width="100" align="right">
            <template #default="{ row }">{{ score(row.setup_big_bull_amount_ratio, 2) }}</template>
          </el-table-column>
          <el-table-column prop="factor_date" label="因子日" width="115" />
          <el-table-column prop="reason_text" label="说明" min-width="240" show-overflow-tooltip />
        </el-table>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>V4参考选股池</h2>
            <p>{{ v4PoolDateLabel }} V4 rank00；D-1 用于 G2 盘中扫描入口，D 日用于复盘对照</p>
          </div>
          <div class="panel-tools">
            <el-segmented v-model="v4PoolMode" :options="v4PoolModeOptions" size="small" />
            <el-tag effect="plain">{{ v4DisplayRows.length }} 只</el-tag>
          </div>
        </div>

        <el-table
          v-loading="loading"
          :data="v4DisplayRows"
          stripe
          size="small"
          height="520"
          :empty-text="v4PoolMode === 'd1' ? '暂无 D-1 V4 参考选股' : '暂无 D 日 V4 参考选股'"
        >
          <el-table-column label="排名" width="70" align="right">
            <template #default="{ row }">{{ row.v4_rank || '--' }}</template>
          </el-table-column>
          <el-table-column label="代码" width="126" fixed>
            <template #default="{ row }">
              <button class="copy-code" type="button" title="复制代码" @click="copyCode(row.code)">
                <span>{{ row.code }}</span>
                <el-icon><DocumentCopy /></el-icon>
              </button>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="名称" min-width="120" fixed />
          <el-table-column label="V4总分" width="100" align="right">
            <template #default="{ row }">{{ score(row.v4_score) }}</template>
          </el-table-column>
          <el-table-column label="5日动量" width="100" align="right">
            <template #default="{ row }">{{ pct(row.mom5) }}</template>
          </el-table-column>
          <el-table-column label="10日动量" width="105" align="right">
            <template #default="{ row }">{{ pct(row.mom10) }}</template>
          </el-table-column>
          <el-table-column label="20日动量" width="105" align="right">
            <template #default="{ row }">{{ pct(row.mom20) }}</template>
          </el-table-column>
          <el-table-column label="量比" width="90" align="right">
            <template #default="{ row }">{{ score(row.vol_ratio, 2) }}</template>
          </el-table-column>
          <el-table-column label="10日波动" width="100" align="right">
            <template #default="{ row }">{{ pct(row.vol10) }}</template>
          </el-table-column>
          <el-table-column label="池日期" width="120">
            <template #default="{ row }">{{ row.v4_pool_date || '--' }}</template>
          </el-table-column>
          <el-table-column prop="reason_text" label="说明" min-width="280" show-overflow-tooltip />
        </el-table>
      </section>
    </template>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import dayjs from 'dayjs'
import { DocumentCopy, Refresh, Search } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import {
  getGen2RiskCoolShadowUpdateTask,
  getGen2SelectionPool,
  runGen2RiskCoolShadowUpdate
} from '@/api/trading'

const loading = ref(false)
const updateLoading = ref(false)
const signalDate = ref(dayjs().format('YYYY-MM-DD'))
const payload = ref({})
const stageFilter = ref('all')
const v4PoolMode = ref('d1')
const qualityMode = ref('volume5')
let updateTimer = null
let updatePollStartedAt = 0

const UPDATE_POLL_TIMEOUT_MS = 120000

const available = computed(() => !!payload.value?.available)
const message = computed(() => payload.value?.message || '')
const strategyName = computed(() => payload.value?.strategy_name || 'G2 第二代完整体：volume5主线 + 突破主线 + 板块扩散')
const selectedDate = computed(() => payload.value?.signal_date || '')
const requestedDate = computed(() => payload.value?.requested_date || signalDate.value || '')
const dateResolution = computed(() => payload.value?.date_resolution || null)
const dateResolutionNotice = computed(() => {
  const requested = String(requestedDate.value || '').trim()
  const selected = String(selectedDate.value || '').trim()
  if (!requested || !selected || requested === selected) return ''
  const reason = String(dateResolution.value?.reason || '').trim()
  const extra = reason ? ' (' + reason + ')' : ''
  if (requested > selected) return 'Request date ' + requested + ' not ready; fallback to ' + selected + extra
  return 'Request date ' + requested + ' differs from displayed date ' + selected
})
const officialRebuild = computed(() => payload.value?.official_rebuild || null)
const officialRebuildNotice = computed(() => {
  const rebuild = officialRebuild.value
  if (!rebuild || typeof rebuild !== 'object') return ''
  const status = String(rebuild.status || '').trim().toLowerCase()
  if (status === 'queued' || status === 'running') {
    const signalDate = String(rebuild.signal_date || selectedDate.value || '--')
    const phase = status === 'queued' ? 'queued' : 'running'
    return 'Official rebuild ' + phase + ' (signal_date=' + signalDate + ')'
  }
  if ((status === 'completed' || status === 'failed') && rebuild.ok === false) {
    const signalDate = String(rebuild.signal_date || selectedDate.value || '--')
    const err = String(rebuild.error || '').trim()
    return 'Official rebuild failed (signal_date=' + signalDate + ')' + (err ? ': ' + err : '')
  }
  return ''
})
const alphaGate = computed(() => payload.value?.alpha191_gate || '')
const strategyMode = computed(() => payload.value?.strategy_mode || alphaGate.value || '')
const branchFreshness = computed(() => payload.value?.branch_freshness || {})
const pipeline = computed(() => Array.isArray(payload.value?.pipeline) ? payload.value.pipeline : [])
const rows = computed(() => Array.isArray(payload.value?.rows) ? payload.value.rows : [])
const triggerRows = computed(() => {
  if (Array.isArray(payload.value?.trigger_rows)) return payload.value.trigger_rows
  return rows.value.filter((row) => row.pass_v4_g2_trigger)
})
const completeRows = computed(() => {
  if (Array.isArray(payload.value?.complete_rows)) return payload.value.complete_rows
  return rows.value.filter((row) => row.pass_official_v2_live || row.pass_mainline1 || row.pass_mainline2 || row.pass_breakout_stage || row.buyable)
})
const promotedRows = computed(() => {
  if (Array.isArray(payload.value?.promoted_rows)) return payload.value.promoted_rows
  return rows.value
})
const qualityRows = computed(() => Array.isArray(payload.value?.quality_rows) ? payload.value.quality_rows : [])
const qualityVisibleRows = computed(() => qualityRows.value.filter((row) => String(row.quality_family || 'volume5') === qualityMode.value))
const qualityVisiblePassCount = computed(() => qualityVisibleRows.value.filter((row) => row.pass_quality).length)
const v4D1Rows = computed(() => {
  if (Array.isArray(payload.value?.v4_d1_rows)) return payload.value.v4_d1_rows
  return rows.value.filter((row) => row.pass_v4_pool)
})
const v4DRows = computed(() => Array.isArray(payload.value?.v4_d_rows) ? payload.value.v4_d_rows : [])
const v4DisplayRows = computed(() => v4PoolMode.value === 'd' ? v4DRows.value : v4D1Rows.value)
const v4PoolDateLabel = computed(() => {
  const first = v4DisplayRows.value[0]
  const mode = v4PoolMode.value === 'd' ? 'D' : 'D-1'
  return `${mode} ${first?.v4_pool_date || '--'}`
})
const fallbackNotice = computed(() => {
  const title = String(payload.value?.fallback_notice || '').trim()
  if (title) return title
  if (!requestedDate.value || !selectedDate.value || requestedDate.value === selectedDate.value) return ''
  return 'Request date ' + requestedDate.value + ' has no matching selection data; fallback to ' + selectedDate.value
})
const branchFreshnessWarning = computed(() => {
  if (branchFreshness.value?.status !== 'stale') return ''
  return String(branchFreshness.value?.text || 'Official branch is stale; live trading already uses latest-driven mainline')
})
const dataFreshness = computed(() => payload.value?.data_freshness || {})
const freshnessWarning = computed(() => {
  const latestDaily = dataFreshness.value?.latest_daily_date
  const selected = selectedDate.value
  return latestDaily && selected && latestDaily > selected
})
const freshnessText = computed(() => {
  if (freshnessWarning.value) {
    return `日线${dataFreshness.value.latest_daily_date}`
  }
  return '同步'
})
const buyableCount = computed(() => promotedRows.value.filter((row) => row.buyable).length)

const stageOptions = [
  { label: '全部', value: 'all' },
  { label: '可买', value: 'buyable' },
  { label: '主线过滤', value: 'alpha' },
  { label: '风控过滤', value: 'risk' },
  { label: '熔断', value: 'suspended' }
]

const qualityModeOptions = [
  { label: 'volume5', value: 'volume5' },
  { label: 'breakout', value: 'breakout' }
]

const v4PoolModeOptions = [
  { label: 'D-1', value: 'd1' },
  { label: 'D', value: 'd' }
]

const disabledSignalDate = (date) => {
  const day = dayjs(date)
  const weekday = day.day()
  return weekday === 0 || weekday === 6 || day.isAfter(dayjs(), 'day')
}

const promotedVisibleRows = computed(() => {
  if (stageFilter.value === 'buyable') return promotedRows.value.filter((row) => row.buyable)
  if (stageFilter.value === 'alpha') return promotedRows.value.filter((row) => row.pass_risk_cool && !row.pass_mainline1 && !row.pass_mainline2)
  if (stageFilter.value === 'risk') return promotedRows.value.filter((row) => row.pass_v4_g2_trigger && !row.pass_risk_cool)
  if (stageFilter.value === 'suspended') {
    return promotedRows.value.filter((row) => ['suspended_by_two_stop_cd3', 'suspended_by_stop_cd5'].includes(row.shadow_status))
  }
  return promotedRows.value
})

const fetchData = async () => {
  loading.value = true
  try {
    payload.value = await getGen2SelectionPool({
      signal_date: signalDate.value || undefined,
      limit: 260
    })
  } finally {
    loading.value = false
  }
}

const pollUpdateTask = async (taskId) => {
  try {
    const task = await getGen2RiskCoolShadowUpdateTask(taskId)
    const status = String(task?.status || '').toLowerCase()
    if (['done', 'completed', 'success'].includes(status)) {
      updateLoading.value = false
      updatePollStartedAt = 0
      ElMessage.success('策略股票池已更新')
      await fetchData()
      return
    }
    if (['failed', 'missing', 'cancelled', 'canceled'].includes(status)) {
      updateLoading.value = false
      updatePollStartedAt = 0
      ElMessage.error(task?.error || 'Selection pool update task ended unexpectedly')
      return
    }
    if (updatePollStartedAt && Date.now() - updatePollStartedAt > UPDATE_POLL_TIMEOUT_MS) {
      updateLoading.value = false
      updatePollStartedAt = 0
      ElMessage.warning('Selection pool update is still running; stopped waiting on page')
      return
    }
    updateTimer = window.setTimeout(() => pollUpdateTask(taskId), 1500)
  } catch (error) {
    updateLoading.value = false
    updatePollStartedAt = 0
    ElMessage.error('Failed to query selection pool update status')
  }
}

const updatePool = async () => {
  if (updateTimer) {
    window.clearTimeout(updateTimer)
    updateTimer = null
  }
  updatePollStartedAt = Date.now()
  updateLoading.value = true
  try {
    const task = await runGen2RiskCoolShadowUpdate({
      signal_date: signalDate.value || undefined,
      pool_rank: 200,
      alpha191_gate: 'g2_v2_complete'
    })
    if (task?.status === 'failed') {
      updateLoading.value = false
      ElMessage.error(task.error || 'Selection pool update failed')
      return
    }
    if (task?.task_id) {
      await pollUpdateTask(task.task_id)
    } else {
      updateLoading.value = false
      await fetchData()
    }
  } catch (error) {
    updateLoading.value = false
  } finally {
    if (!updateLoading.value) {
      updatePollStartedAt = 0
    }
  }
}

const score = (value, digits = 3) => {
  const num = Number(value)
  return Number.isFinite(num) ? num.toFixed(digits) : '--'
}

const pct = (value) => {
  const num = Number(value)
  return Number.isFinite(num) ? `${(num * 100).toFixed(1)}%` : '--'
}

const pctNumber = (value) => {
  const num = Number(value)
  return Number.isFinite(num) ? `${num.toFixed(1)}%` : '--'
}

const copyCode = async (code) => {
  const text = String(code || '').match(/\d{6}/)?.[0] || ''
  if (!text) return
  await navigator.clipboard.writeText(text)
  ElMessage.success(`Copied ${text}`)
}

onMounted(fetchData)

onBeforeUnmount(() => {
  if (updateTimer) {
    window.clearTimeout(updateTimer)
  }
  updateLoading.value = false
})
</script>

<style scoped>
.gen2-selection-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.top-band,
.panel {
  background: #fff;
  border: 1px solid #e5e9f5;
  border-radius: 8px;
  box-shadow: 0 8px 24px rgba(29, 45, 86, 0.06);
}

.top-band {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: center;
  padding: 18px 20px;
}

.eyebrow {
  color: #5b6b9a;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0;
  text-transform: uppercase;
}

h1,
h2,
p {
  margin: 0;
}

h1 {
  color: #1d2a4d;
  font-size: 26px;
  line-height: 1.25;
}

h2 {
  color: #25345f;
  font-size: 16px;
}

.subtitle,
.panel-head p,
.pipeline-step p,
.status-item span {
  color: #6d7899;
  font-size: 13px;
}

.actions,
.panel-head,
.panel-tools,
.path-line {
  display: flex;
  align-items: center;
  gap: 10px;
}

.actions {
  flex-wrap: wrap;
  justify-content: flex-end;
}

.pipeline {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 12px;
}

.pipeline-step,
.status-item {
  background: #fff;
  border: 1px solid #e5e9f5;
  border-radius: 8px;
}

.pipeline-step {
  display: flex;
  gap: 12px;
  min-height: 126px;
  padding: 14px;
}

.step-index {
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: #eff4ff;
  color: #31549f;
  font-weight: 800;
  flex: 0 0 auto;
}

.step-body {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.step-body span {
  color: #354269;
  font-size: 13px;
  font-weight: 700;
}

.step-body strong {
  color: #182b57;
  font-size: 26px;
}

.status-strip {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
}

.status-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px 14px;
}

.status-item strong {
  color: #22305a;
  font-size: 18px;
}

.panel {
  padding: 16px;
}

.panel-head {
  justify-content: space-between;
  margin-bottom: 12px;
}

.panel-tools {
  flex-wrap: wrap;
  justify-content: flex-end;
}

.copy-code {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: 0;
  background: transparent;
  color: #2454a6;
  cursor: pointer;
  font: inherit;
  padding: 0;
}

.up {
  color: #cf3f3f !important;
}

.warn {
  color: #b7791f !important;
}

@media (max-width: 1180px) {
  .pipeline,
  .status-strip {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 760px) {
  .top-band,
  .panel-head {
    align-items: stretch;
    flex-direction: column;
  }

  .actions,
  .panel-tools {
    justify-content: flex-start;
  }

  .pipeline,
  .status-strip {
    grid-template-columns: 1fr;
  }
}
</style>
