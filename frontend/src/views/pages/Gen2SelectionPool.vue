<template>
  <div class="gen2-selection-page">
    <section class="top-band">
      <div>
        <p class="eyebrow">Gen2 Selection Pool</p>
        <h1>绛栫暐閫夎偂姹</h1>
        <p class="subtitle">{{ strategyName }} 路 {{ selectedDate || '--' }}</p>
      </div>
      <div class="actions">
        <el-date-picker
          v-model="signalDate"
          type="date"
          value-format="YYYY-MM-DD"
          format="YYYY-MM-DD"
          placeholder="Select signal date"
          clearable
          :disabled-date="disabledSignalDate"
          style="width: 180px"
          @change="fetchData"
        />
        <el-button :icon="Search" type="primary" :loading="loading" @click="fetchData">鏌ヨ</el-button>
        <el-button :icon="Refresh" :loading="updateLoading" @click="updatePool">鏇存柊褰撴棩瀹炴椂姹</el-button>
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
          <span>璇锋眰鏃ユ湡</span>
          <strong>{{ requestedDate || '--' }}</strong>
        </div>
        <div class="status-item">
          <span>瀹為檯灞曠ず</span>
          <strong>{{ selectedDate || '--' }}</strong>
        </div>
        <div class="status-item">
          <span>绛栫暐妯″紡</span>
          <strong>{{ strategyMode || '--' }}</strong>
        </div>
        <div class="status-item">
          <span>鍙拱瑙傚療</span>
          <strong class="up">{{ buyableCount }}</strong>
        </div>
        <div class="status-item">
          <span>鏁版嵁鐘舵€</span>
          <strong :class="{ warn: freshnessWarning }">{{ freshnessText }}</strong>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>褰撴棩瑙﹀彂淇″彿</h2>
            <p>{{ selectedDate || '--' }} 鐩樹腑宸茬粡瑙﹀彂 G2 30m 搴曞垎鍨嬫斁閲忕‘璁ょ殑涓偂銆</p>
          </div>
          <el-tag effect="plain">{{ triggerRows.length }} 鍙</el-tag>
        </div>

        <el-table
          v-loading="loading"
          :data="triggerRows"
          stripe
          size="small"
          height="360"
          empty-text="鏆傛棤褰撴棩瑙﹀彂淇″彿"
        >
          <el-table-column label="鐘舵€?" width="118" fixed>
            <template #default="{ row }">
              <el-tag :type="row.stage_type || 'info'" effect="light">{{ row.stage_label || '--' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="浠ｇ爜" width="126" fixed>
            <template #default="{ row }">
              <button class="copy-code" type="button" title="澶嶅埗浠ｇ爜" @click="copyCode(row.code)">
                <span>{{ row.code }}</span>
                <el-icon><DocumentCopy /></el-icon>
              </button>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="鍚嶇О" min-width="110" fixed />
          <el-table-column prop="confirm_datetime" label="鐩樹腑纭" width="165" />
          <el-table-column label="V4鎺掑悕" width="90" align="right">
            <template #default="{ row }">{{ row.v4_rank || '--' }}</template>
          </el-table-column>
          <el-table-column label="鐩樹腑娑ㄥ箙" width="100" align="right">
            <template #default="{ row }">{{ pctNumber(row.rt_return_pct) }}</template>
          </el-table-column>
          <el-table-column label="30m閲忔瘮" width="100" align="right">
            <template #default="{ row }">{{ score(row.amount_ratio, 2) }}</template>
          </el-table-column>
          <el-table-column label="璺緞" min-width="240">
            <template #default="{ row }">
              <div class="path-line">
                <el-tag :type="row.pass_risk_cool ? 'success' : 'danger'" effect="plain">椋庢帶</el-tag>
                <el-tag :type="row.pass_mainline1 ? 'success' : 'info'" effect="plain">volume5</el-tag>
                <el-tag :type="row.pass_mainline2 ? 'success' : 'info'" effect="plain">绐佺牬</el-tag>
                <el-tag :type="row.buyable ? 'success' : 'info'" effect="plain">鍙拱</el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="reason_text" label="璁板綍璇存槑" min-width="260" show-overflow-tooltip />
        </el-table>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>瀹屾暣鐗堜袱濂椾綋绯</h2>
            <p>灞曠ず绗簩浠ｅ畬鏁寸増涓?volume5 涓荤嚎涓庣獊鐮?鏉垮潡涓荤嚎閫夊嚭鐨勫叏閮ㄨ偂绁ㄣ€</p>
          </div>
          <el-tag effect="plain">{{ completeRows.length }} 鍙</el-tag>
        </div>

        <el-table
          v-loading="loading"
          :data="completeRows"
          stripe
          size="small"
          height="420"
          empty-text="鏆傛棤瀹屾暣鐗堜富绾夸俊鍙?"`r`n        >
          <el-table-column label="浣撶郴" width="170" fixed>
            <template #default="{ row }">
              <div class="path-line">
                <el-tag v-if="row.pass_mainline1" type="success" effect="light">volume5</el-tag>
                <el-tag v-if="row.pass_mainline2" type="warning" effect="light">绐佺牬+鏉垮潡</el-tag>
                <el-tag v-if="row.pass_breakout_stage" type="warning" effect="light">{{ row.breakout_stage_label || '浜岀獊瑙傚療' }}</el-tag>
                <el-tag v-if="!row.pass_mainline1 && !row.pass_mainline2 && !row.pass_breakout_stage" effect="light">瑙傚療</el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="浠ｇ爜" width="126" fixed>
            <template #default="{ row }">
              <button class="copy-code" type="button" title="澶嶅埗浠ｇ爜" @click="copyCode(row.code)">
                <span>{{ row.code }}</span>
                <el-icon><DocumentCopy /></el-icon>
              </button>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="鍚嶇О" min-width="110" fixed />
          <el-table-column prop="confirm_datetime" label="鐩樹腑纭" width="165" />
          <el-table-column label="V4鎺掑悕" width="90" align="right">
            <template #default="{ row }">{{ row.v4_rank || '--' }}</template>
          </el-table-column>
          <el-table-column v-if="qualityMode === 'volume5'" label="volume5鍒?" width="105" align="right">
            <template #default="{ row }">{{ score(row.alpha191_volume5_score) }}</template>
          </el-table-column>
          <el-table-column v-if="qualityMode === 'volume5'" label="volume5鎺掑悕" width="110" align="right">
            <template #default="{ row }">{{ row.alpha191_volume5_rank_in_day || '--' }}</template>
          </el-table-column>
          <el-table-column label="L3寮?%" width="100" align="right">
            <template #default="{ row }">{{ pct(row.l3_rt_strong3_ratio) }}</template>
          </el-table-column>
          <el-table-column label="60鏃ヤ綆鐐规定骞?" width="120" align="right">
            <template #default="{ row }">{{ pct(row.runup_from_60d_low) }}</template>
          </el-table-column>
          <el-table-column prop="reason_text" label="璁板綍璇存槑" min-width="260" show-overflow-tooltip />
        </el-table>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>鏅嬬骇琛</h2>
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
          empty-text="鏆傛棤閫夎偂姹犺褰?"`r`n        >
          <el-table-column label="鐘舵€?" width="118" fixed>
            <template #default="{ row }">
              <el-tag :type="row.stage_type || 'info'" effect="light">{{ row.stage_label || '--' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="浠ｇ爜" width="126" fixed>
            <template #default="{ row }">
              <button class="copy-code" type="button" title="澶嶅埗浠ｇ爜" @click="copyCode(row.code)">
                <span>{{ row.code }}</span>
                <el-icon><DocumentCopy /></el-icon>
              </button>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="鍚嶇О" min-width="110" fixed />
          <el-table-column prop="confirm_datetime" label="鐩樹腑纭" width="165" />
          <el-table-column label="璺緞" min-width="280">
            <template #default="{ row }">
              <div class="path-line">
                <el-tag :type="row.pass_v4_pool ? 'success' : 'info'" effect="plain">V4姹</el-tag>
                <el-tag :type="row.pass_v4_g2_trigger ? 'success' : 'info'" effect="plain">G2瑙﹀彂</el-tag>
                <el-tag :type="row.pass_risk_cool ? 'success' : 'danger'" effect="plain">椋庢帶</el-tag>
                <el-tag :type="row.pass_mainline1 ? 'success' : 'warning'" effect="plain">涓荤嚎涓€</el-tag>
                <el-tag :type="row.pass_mainline2 || row.pass_breakout_stage ? 'success' : 'info'" effect="plain">涓荤嚎浜</el-tag>
                <el-tag v-if="row.pass_volume5_trigger" type="success" effect="plain">v5瑙﹀彂</el-tag>
                <el-tag v-if="row.pass_breakout_stage" type="warning" effect="plain">{{ row.breakout_stage_label || '浜岀獊灞傜骇' }}</el-tag>
                <el-tag :type="row.buyable ? 'success' : 'info'" effect="plain">鍙拱</el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="V4鎺掑悕" width="90" align="right">
            <template #default="{ row }">{{ row.v4_rank || '--' }}</template>
          </el-table-column>
          <el-table-column label="涓荤嚎鍒?" width="105" align="right">
            <template #default="{ row }">{{ score(row.alpha191_volume5_score) }}</template>
          </el-table-column>
          <el-table-column label="涓荤嚎鎺掑悕" width="95" align="right">
            <template #default="{ row }">{{ row.alpha191_volume5_rank_in_day || '--' }}</template>
          </el-table-column>
          <el-table-column label="L3寮?%" width="100" align="right">
            <template #default="{ row }">{{ pct(row.l3_rt_strong3_ratio) }}</template>
          </el-table-column>
          <el-table-column label="60鏃ヤ綆鐐规定骞?" width="120" align="right">
            <template #default="{ row }">{{ pct(row.runup_from_60d_low) }}</template>
          </el-table-column>
          <el-table-column label="鐩樹腑娑ㄥ箙" width="100" align="right">
            <template #default="{ row }">{{ pctNumber(row.rt_return_pct) }}</template>
          </el-table-column>
          <el-table-column label="30m閲忔瘮" width="100" align="right">
            <template #default="{ row }">{{ score(row.amount_ratio, 2) }}</template>
          </el-table-column>
          <el-table-column prop="reason_text" label="璁板綍璇存槑" min-width="260" show-overflow-tooltip />
        </el-table>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>鍊欓€夎偂璐ㄩ噺鎺掑簭</h2>
            <p>D-1 V4鍙傝€冩睜鎸?Alpha150/070/095/132/144 璁＄畻 volume5锛屽啀妫€鏌?keep80 涓?runup&lt;=100%銆</p>
          </div>
          <div class="panel-tools">
            <el-segmented v-model="qualityMode" :options="qualityModeOptions" size="small" />
            <el-tag type="success" effect="plain">閫氳繃 {{ qualityVisiblePassCount }} 鍙</el-tag>
            <el-tag effect="plain">鍏?{{ qualityVisibleRows.length }} 鍙</el-tag>
          </div>
        </div>

        <el-table
          v-loading="loading"
          :data="qualityVisibleRows"
          stripe
          size="small"
          height="520"
          empty-text="鏆傛棤鍊欓€夎偂璐ㄩ噺鎺掑簭"
        >
          <el-table-column label="璐ㄩ噺" width="105" fixed>
            <template #default="{ row }">
              <el-tag :type="row.quality_type || 'info'" effect="light">{{ row.quality_label || '--' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="浠ｇ爜" width="126" fixed>
            <template #default="{ row }">
              <button class="copy-code" type="button" title="澶嶅埗浠ｇ爜" @click="copyCode(row.code)">
                <span>{{ row.code }}</span>
                <el-icon><DocumentCopy /></el-icon>
              </button>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="鍚嶇О" min-width="110" fixed />
          <el-table-column label="V4鎺掑悕" width="90" align="right">
            <template #default="{ row }">{{ row.v4_rank || '--' }}</template>
          </el-table-column>
          <el-table-column label="V4鎬诲垎" width="100" align="right">
            <template #default="{ row }">{{ score(row.v4_score) }}</template>
          </el-table-column>
          <el-table-column label="volume5鍒?" width="105" align="right">
            <template #default="{ row }">{{ score(row.alpha191_volume5_score) }}</template>
          </el-table-column>
          <el-table-column label="volume5鎺掑悕" width="110" align="right">
            <template #default="{ row }">{{ row.alpha191_volume5_rank_in_day || '--' }}</template>
          </el-table-column>
          <el-table-column v-if="qualityMode === 'volume5'" label="keep80闃堝€?" width="105" align="right">
            <template #default="{ row }">{{ score(row.alpha191_gate_threshold) }}</template>
          </el-table-column>
          <el-table-column v-if="qualityMode === 'volume5'" label="keep80" width="90" align="center">
            <template #default="{ row }">
              <el-tag :type="row.pass_keep80 ? 'success' : 'warning'" effect="plain">
                {{ row.pass_keep80 ? '閫氳繃' : '鏈繃' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column v-if="qualityMode === 'volume5'" label="60鏃ヤ綆鐐规定骞?" width="125" align="right">
            <template #default="{ row }">{{ pct(row.runup_from_60d_low) }}</template>
          </el-table-column>
          <el-table-column v-if="qualityMode === 'volume5'" label="runup" width="90" align="center">
            <template #default="{ row }">
              <el-tag :type="row.pass_runup ? 'success' : 'warning'" effect="plain">
                {{ row.pass_runup ? '閫氳繃' : '鏈繃' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column v-if="qualityMode === 'breakout'" label="灞傜骇" width="120">
            <template #default="{ row }">{{ row.breakout_stage_label || '--' }}</template>
          </el-table-column>
          <el-table-column v-if="qualityMode === 'breakout'" label="澶ч槼鏃?" width="115">
            <template #default="{ row }">{{ row.setup_big_bull_date || '--' }}</template>
          </el-table-column>
          <el-table-column v-if="qualityMode === 'breakout'" label="璺濈椤?" width="100" align="right">
            <template #default="{ row }">{{ pct(row.close_vs_box_top) }}</template>
          </el-table-column>
          <el-table-column v-if="qualityMode === 'breakout'" label="绠变綋瀹藉害" width="105" align="right">
            <template #default="{ row }">{{ pct(row.setup_box_range) }}</template>
          </el-table-column>
          <el-table-column v-if="qualityMode === 'breakout'" label="澶ч槼閲忔瘮" width="100" align="right">
            <template #default="{ row }">{{ score(row.setup_big_bull_amount_ratio, 2) }}</template>
          </el-table-column>
          <el-table-column prop="factor_date" label="鍥犲瓙鏃?" width="115" />
          <el-table-column prop="reason_text" label="璇存槑" min-width="240" show-overflow-tooltip />
        </el-table>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>V4鍙傝€冮€夎偂姹</h2>
            <p>{{ v4PoolDateLabel }} V4 rank鍓?00锛汥-1鐢ㄤ簬G2鐩樹腑鎵弿鍏ュ彛锛孌鏃ョ敤浜庡鐩樺鐓с€</p>
          </div>
          <div class="panel-tools">
            <el-segmented v-model="v4PoolMode" :options="v4PoolModeOptions" size="small" />
            <el-tag effect="plain">{{ v4DisplayRows.length }} 鍙</el-tag>
          </div>
        </div>

        <el-table
          v-loading="loading"
          :data="v4DisplayRows"
          stripe
          size="small"
          height="520"
          :empty-text="v4PoolMode === 'd1' ? '鏆傛棤D-1 V4鍙傝€冮€夎偂' : '鏆傛棤D鏃4鍙傝€冮€夎偂'"
        >
          <el-table-column label="鎺掑悕" width="70" align="right">
            <template #default="{ row }">{{ row.v4_rank || '--' }}</template>
          </el-table-column>
          <el-table-column label="浠ｇ爜" width="126" fixed>
            <template #default="{ row }">
              <button class="copy-code" type="button" title="澶嶅埗浠ｇ爜" @click="copyCode(row.code)">
                <span>{{ row.code }}</span>
                <el-icon><DocumentCopy /></el-icon>
              </button>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="鍚嶇О" min-width="120" fixed />
          <el-table-column label="V4鎬诲垎" width="100" align="right">
            <template #default="{ row }">{{ score(row.v4_score) }}</template>
          </el-table-column>
          <el-table-column label="5鏃ュ姩閲?" width="100" align="right">
            <template #default="{ row }">{{ pct(row.mom5) }}</template>
          </el-table-column>
          <el-table-column label="10鏃ュ姩閲?" width="105" align="right">
            <template #default="{ row }">{{ pct(row.mom10) }}</template>
          </el-table-column>
          <el-table-column label="20鏃ュ姩閲?" width="105" align="right">
            <template #default="{ row }">{{ pct(row.mom20) }}</template>
          </el-table-column>
          <el-table-column label="閲忔瘮" width="90" align="right">
            <template #default="{ row }">{{ score(row.vol_ratio, 2) }}</template>
          </el-table-column>
          <el-table-column label="10鏃ユ尝鍔?" width="100" align="right">
            <template #default="{ row }">{{ pct(row.vol10) }}</template>
          </el-table-column>
          <el-table-column label="姹犳棩鏈?" width="120">
            <template #default="{ row }">{{ row.v4_pool_date || '--' }}</template>
          </el-table-column>
          <el-table-column prop="reason_text" label="璇存槑" min-width="280" show-overflow-tooltip />
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
const strategyName = computed(() => payload.value?.strategy_name || 'G2 绗簩浠ｅ畬鏁寸増锛歷olume5涓荤嚎 + 绐佺牬涓荤嚎 + 鏉垮潡鎵╂暎')
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
const qualityRows = computed(() => Array.isArray(payload.value?.quality_rows) ? payload.value.quality_rows : [])
const qualityVisibleRows = computed(() => qualityRows.value.filter((row) => String(row.quality_family || 'volume5') === qualityMode.value))
const qualityPassCount = computed(() => Number(payload.value?.quality_pass_count ?? qualityRows.value.filter((row) => row.pass_quality).length))
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
    return `鏃ョ嚎${dataFreshness.value.latest_daily_date}`
  }
  return '鍚屾'
})

const stageOptions = [
  { label: '鍏ㄩ儴', value: 'all' },
  { label: '鍙拱', value: 'buyable' },
  { label: '涓荤嚎杩囨护', value: 'alpha' },
  { label: '椋庢帶杩囨护', value: 'risk' },
  { label: '鐔旀柇', value: 'suspended' }
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

const visibleRows = computed(() => {
  if (stageFilter.value === 'v4') return rows.value.filter((row) => row.pass_v4_pool)
  if (stageFilter.value === 'no_trigger') return rows.value.filter((row) => row.pass_v4_pool && !row.pass_v4_g2_trigger)
  if (stageFilter.value === 'buyable') return rows.value.filter((row) => row.buyable)
  if (stageFilter.value === 'alpha') return rows.value.filter((row) => row.pass_risk_cool && !row.pass_mainline1 && !row.pass_mainline2)
  if (stageFilter.value === 'risk') return rows.value.filter((row) => row.pass_v4_g2_trigger && !row.pass_risk_cool)
  if (stageFilter.value === 'suspended') {
    return rows.value.filter((row) => ['suspended_by_two_stop_cd3', 'suspended_by_stop_cd5'].includes(row.shadow_status))
  }
  return rows.value
})

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
      ElMessage.success('绛栫暐閫夎偂姹犲凡鏇存柊')
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


