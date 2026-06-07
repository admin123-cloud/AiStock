<template>
  <div class="page">
    <div class="head-card">
      <div>
        <h1>{{ pageTitle }}</h1>
        <p>{{ pageSubtitle }}</p>
      </div>
      <div class="actions">
        <el-date-picker
          v-model="targetDate"
          type="date"
          value-format="YYYY-MM-DD"
          format="YYYY-MM-DD"
          placeholder="Select trade date"
          clearable
          style="width: 180px"
        />
        <el-button type="primary" :loading="loading" @click="fetchData">鏌ヨ</el-button>
        <el-button :loading="refreshing" @click="refreshSnapshot">鏇存柊蹇収</el-button>
      </div>
    </div>

    <el-alert
      v-if="!available && !loading"
      type="warning"
      :closable="false"
      :title="message || unavailableTitle"
    />

    <template v-if="available">
      <el-alert
        v-if="selectionBranchFreshnessWarning"
        type="warning"
        :closable="false"
        show-icon
        :title="selectionBranchFreshnessWarning"
      />

      <el-alert
        v-if="dateResolutionNotice"
        type="warning"
        :closable="false"
        show-icon
        :title="dateResolutionNotice"
      />

      <el-alert
        v-if="liveFallbackNotice"
        type="warning"
        :closable="false"
        show-icon
        :title="liveFallbackNotice"
      />

      <el-alert
        v-if="officialRebuildNotice"
        type="warning"
        :closable="false"
        :show-icon="true"
        :title="officialRebuildNotice"
      />

      <div class="decision-strip">
        <div class="decision-card" :class="marketGate?.can_open ? 'pass' : 'block'">
          <span class="decision-label">寮€浠撻棬绂?/span>
          <strong>{{ marketGate?.can_open ? '鍏佽鏂板' : '绂佹鏂板' }}</strong>
          <small>{{ marketGateSummary }}</small>
        </div>
        <div class="decision-card" :class="actionableBuyRows.length ? 'pass' : 'neutral'">
          <span class="decision-label">姝ｅ紡涔扮偣</span>
          <strong>{{ actionableBuyRows.length }} 鍙?/strong>
          <small>{{ actionableBuyRows.length ? '鍙繘鍏ヤ氦鏄撹鍒欐鏌? : '褰撳墠娌℃湁姝ｅ紡鍙拱鍏ヨ偂绁? }}</small>
        </div>
        <div class="decision-card" :class="sellPriorityRows.length ? 'block' : 'pass'">
          <span class="decision-label">鎸佷粨椋庢帶</span>
          <strong>{{ sellPriorityRows.length }} 椤?/strong>
          <small>{{ sellPriorityRows.length ? '鍏堝鐞嗗崠鍑?鍑忎粨椋庨櫓' : '鏆傛棤纭鎺у姩浣? }}</small>
        </div>
        <div class="decision-card neutral">
          <span class="decision-label">鑷姩鎺㈡祴</span>
          <strong>{{ gen2ShadowMonitorStatus?.enabled ? '杩愯涓? : '鏈紑鍚? }}</strong>
          <small>{{ gen2ShadowMonitorResultText || gen2ShadowMonitorText }}</small>
        </div>
      </div>

      <div class="panel workflow-panel">
        <div class="sub-panel-header">
          <div>
            <div class="panel-title">绛栫暐宸ヤ綔娴佺洃鎺?/div>
            <div class="toolbar-note">{{ workflowSummaryText }}</div>
          </div>
          <div class="actions">
            <el-tag :type="workflowOk ? 'success' : 'danger'" effect="light">
              {{ workflowOk ? '鍏ㄩ摼璺甯? : '瀛樺湪闃诲' }}
            </el-tag>
            <el-button size="small" :loading="workflowLoading" @click="fetchWorkflowStatus(selectedDate)">鍒锋柊宸ヤ綔娴?/el-button>
          </div>
        </div>
        <div class="workflow-grid">
          <div
            v-for="stage in workflowStages"
            :key="stage.stage"
            class="workflow-step"
            :class="stage.ok ? 'ok' : 'blocked'"
          >
            <div class="workflow-step-head">
              <span>{{ workflowStageLabel(stage.stage) }}</span>
              <el-tag size="small" :type="stage.ok ? 'success' : 'danger'" effect="light">
                {{ stage.ok ? '閫氳繃' : `${stage.failed_count || stage.failed?.length || 0}椤归樆濉瀈 }}
              </el-tag>
            </div>
            <div class="workflow-step-meta">{{ workflowStageMeta(stage) }}</div>
          </div>
        </div>
        <el-table
          v-if="workflowFailedChecks.length"
          :data="workflowFailedChecks"
          stripe
          size="small"
          class="workflow-table"
          empty-text="鏆傛棤闃诲椤?
        >
          <el-table-column label="灞傜骇" width="120">
            <template #default="{ row }">{{ workflowStageLabel(row.stage || row.type) }}</template>
          </el-table-column>
          <el-table-column prop="name" label="妫€鏌ラ」" width="190" show-overflow-tooltip />
          <el-table-column prop="message" label="闃诲璇存槑" min-width="360" show-overflow-tooltip />
          <el-table-column prop="trade_date" label="鏃ユ湡" width="112" />
          <el-table-column label="缁撴灉" width="120">
            <template #default="{ row }">
              {{ workflowCheckResult(row) }}
            </template>
          </el-table-column>
        </el-table>
        <el-table
          v-if="workflowLineageRows.length"
          :data="workflowLineageRows"
          stripe
          size="small"
          class="workflow-table"
        >
          <el-table-column label="閾捐矾灞? width="110">
            <template #default="{ row }">{{ workflowStageLabel(row.stage) }}</template>
          </el-table-column>
          <el-table-column prop="label" label="鐪熸簮/浜х墿" width="170" show-overflow-tooltip />
          <el-table-column prop="source_type" label="绫诲瀷" width="90" />
          <el-table-column prop="storage" label="瀛樺偍浣嶇疆" min-width="260" show-overflow-tooltip />
          <el-table-column prop="official_reader" label="瀹樻柟璇昏矾寰? min-width="220" show-overflow-tooltip />
          <el-table-column label="璇诲洖缁撴灉" width="120">
            <template #default="{ row }">{{ workflowLineageResult(row) }}</template>
          </el-table-column>
        </el-table>
      </div>

      <div class="panel execution-panel">
        <div class="sub-panel-header">
          <div>
            <div class="panel-title">浠婃棩姝ｅ紡涔板叆鎵ц鍖?/div>
            <div class="toolbar-note">杩欓噷鍙樉绀哄凡缁忛€氳繃绛栫暐鐘舵€併€佺洏涓Е鍙戝拰椋庢帶鍊欓€夌姸鎬佺殑鍙拱鍏ユ爣鐨勶紱褰卞瓙鏍锋湰涓嶄細杩涘叆杩欓噷銆?/div>
            <div class="toolbar-note">{{ ptradeBridgeSummary }}</div>
            <div class="toolbar-note">{{ ptradeBridgeQueueText }}</div>
            <div class="toolbar-note">{{ ptradeBridgeReadinessText }}</div>
          </div>
          <div class="actions">
            <el-button type="primary" :loading="gen2ShadowMonitorLoading" @click="runGen2ShadowMonitorOnce">绔嬪嵆鎺㈡祴涔扮偣</el-button>
            <el-button :loading="allRefreshLoading || marketGateLoading || gen2ShadowLoading || holdingRefreshLoading" @click="refreshAllHoldingData">鍒锋柊浜ゆ槗鐘舵€?/el-button>
          </div>
        </div>
        <el-table :data="actionableBuyRows" stripe size="small" empty-text="褰撳墠娌℃湁姝ｅ紡鍙拱鍏ヨ偂绁?>
          <el-table-column label="浠ｇ爜" width="110">
            <template #default="{ row }">
              <el-button type="primary" link @click="copyPlainCode(row.code)">{{ normalizeCode(row.code) || '--' }}</el-button>
            </template>
          </el-table-column>
          <el-table-column label="鍚嶇О" min-width="120">
            <template #default="{ row }">
              <el-button type="primary" link @click="openStockDetail(row)">{{ row.name || '--' }}</el-button>
            </template>
          </el-table-column>
          <el-table-column label="纭鏃堕棿" width="150" show-overflow-tooltip>
            <template #default="{ row }">{{ row.confirm_datetime || '--' }}</template>
          </el-table-column>
          <el-table-column label="鍏ュ満浠? width="90">
            <template #default="{ row }">{{ formatPrice(row.entry_price) }}</template>
          </el-table-column>
          <el-table-column label="V4鎺掑悕" width="86">
            <template #default="{ row }">{{ row.v4_rank || '--' }}</template>
          </el-table-column>
          <el-table-column label="Alpha191" width="128">
            <template #default="{ row }">{{ formatScore(row.alpha191_volume5_score ?? row.alpha191_gate_score) }}</template>
          </el-table-column>
          <el-table-column label="娑ㄥ箙/閲忔瘮" width="116">
            <template #default="{ row }">{{ formatPct(row.rt_return_pct) }} / {{ formatRatio(row.amount_ratio) }}</template>
          </el-table-column>
          <el-table-column prop="reason_text" label="瑙﹀彂璇存槑" min-width="260" show-overflow-tooltip />
          <el-table-column label="鎿嶄綔" width="180" fixed="right">
            <template #default="{ row }">
              <el-button type="primary" link @click="openAddHoldingDialog(row, 'new')">瑙勫垯妫€鏌?/el-button>
              <el-button
                type="success"
                link
                :disabled="!canSubmitPaperOrder(row)"
                @click="submitPaperOrderForRow(row)"
              >
                {{ paperOrderButtonText(row) }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div class="panel risk-panel">
        <div class="sub-panel-header">
          <div>
            <div class="panel-title">鎸佷粨椋庢帶涓庡崠鍑轰紭鍏堢骇</div>
            <div class="toolbar-note">瀹炵洏鍏堝鐞嗗凡鏈変粨浣嶉闄╋紝鍐嶈€冭檻鏂板寮€浠擄紱杩欓噷姹囨€荤‖姝㈡崯銆佺Щ鍔ㄦ鐩堛€佽瘎鍒嗘睜鍜屽垎閽熷崠鐐广€?/div>
          </div>
          <el-button :loading="holdingRefreshLoading || signalLoading" @click="refreshCurrentHoldings">鍒锋柊鎸佷粨椋庢帶</el-button>
        </div>
        <el-table :data="sellPriorityRows" stripe size="small" empty-text="鏆傛棤闇€瑕佷紭鍏堝鐞嗙殑鎸佷粨椋庨櫓">
          <el-table-column label="浠ｇ爜" width="110">
            <template #default="{ row }">
              <el-button type="primary" link @click="openStockDetail(row)">{{ displayCode(row.code) }}</el-button>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="鍚嶇О" min-width="120" />
          <el-table-column label="鐩堜簭" width="90">
            <template #default="{ row }">
              <span :style="aSharePnlStyle(row.pnl_ratio)">{{ formatPct(row.pnl_ratio) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="risk_text" label="浼樺厛澶勭悊鍘熷洜" min-width="260" show-overflow-tooltip />
          <el-table-column prop="signal.suggestion" label="鍗栧嚭寤鸿" min-width="140" show-overflow-tooltip />
          <el-table-column label="鎿嶄綔" width="96" fixed="right">
            <template #default="{ row }">
              <el-button type="warning" link @click="openSellDialog(row, row.holding_index)">鍗栧嚭</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
      <div class="panel">
        <div class="panel-title">璧勯噾鑲＄エ鎬昏</div>
        <div class="capital-grid">
          <div class="capital-item">
            <label>鍒濆鎬昏祫閲?/label>
            <div class="capital-fixed">{{ formatMoney(FIXED_BASE_CAPITAL) }}</div>
          </div>
          <div class="capital-item readonly">
            <label>鎬昏祫閲戯紙绯荤粺缁存姢锛?/label>
            <div>{{ formatMoney(capitalSnapshot.total_capital) }}</div>
          </div>
          <div class="capital-item readonly">
            <label>鍙敤璧勯噾锛堢郴缁熺淮鎶わ級</label>
            <div>{{ formatMoney(capitalSnapshot.available_cash) }}</div>
          </div>
          <div class="capital-item readonly">
            <label>鎸佷粨甯傚€?/label>
            <div>{{ formatMoney(capitalSnapshot.market_value) }}</div>
          </div>
          <div class="capital-item readonly">
            <label>浠撲綅鍗犳瘮</label>
            <div :style="{ color: capitalSnapshot.position_ratio > 80 ? '#d4380d' : '#24355d' }">{{ formatPct(capitalSnapshot.position_ratio) }}</div>
          </div>
          <div class="capital-item readonly">
            <label>鎸佷粨鎴愭湰</label>
            <div>{{ formatMoney(capitalSnapshot.cost_value) }}</div>
          </div>
          <div class="capital-item readonly">
            <label>宸插疄鐜扮泩浜?/label>
            <div :style="aSharePnlStyle(capitalSnapshot.realized_pnl)">
              {{ formatMoney(capitalSnapshot.realized_pnl) }}
            </div>
          </div>
          <div class="capital-item readonly">
            <label>娴姩鐩堜簭</label>
            <div :style="{ color: capitalSnapshot.pnl_amount > 0 ? '#d4380d' : (capitalSnapshot.pnl_amount < 0 ? '#389e0d' : '#24355d') }">
              {{ formatMoney(capitalSnapshot.pnl_amount) }}锛坽{ formatPct(capitalSnapshot.pnl_ratio) }}锛?            </div>
          </div>
          <div class="capital-item readonly">
            <label>褰撴棩鐩堜簭</label>
            <div :style="aSharePnlStyle(capitalSnapshot.day_pnl)">
              {{ formatMoney(capitalSnapshot.day_pnl) }}
            </div>
          </div>
          <div class="capital-item readonly">
            <label>澶х洏寮€浠撻椄闂?/label>
            <div :style="{ color: marketGate?.can_open ? '#d4380d' : '#389e0d' }">
              {{ marketGate?.can_open ? '鍏佽鏂板' : '绂佹鏂板' }}
            </div>
            <small>{{ marketGateSummary }}</small>
          </div>
        </div>
        <el-alert
          v-if="fallback?.active"
          type="info"
          :closable="false"
          :title="fallback.title"
          :description="fallback.message"
          class="panel-alert"
        />
        <div v-if="capitalForm.synced_at" class="toolbar-note">鍚岃姳椤鸿祫閲戞寔鑲℃渶杩戝悓姝ワ細{{ capitalForm.synced_at }}</div>
        <div style="height: 12px" />
        <el-alert
          v-if="disciplineText"
          type="warning"
          :closable="false"
          :title="disciplineText"
          class="panel-alert"
        />
        <div class="holding-toolbar">
          <div class="actions">
            <el-button :loading="thsCapitalSyncLoading" @click="syncCapitalAndHoldingsFromThs">鍚屾鍚岃姳椤鸿祫閲戞寔鑲?/el-button>
            <el-button type="primary" @click="openAddHoldingDialog(null, 'new')">鏂板鎸佷粨</el-button>
          </div>
          <div class="actions">
            <el-switch v-model="monitorTradingHoursOnly" active-text="浠呬氦鏄撴椂娈? />
            <el-input-number v-model="monitorQuietMinutes" :min="0" :step="5" style="width: 150px" />
            <el-button type="success" :plain="monitorEnabled" :loading="monitorLoading" @click="toggleMonitor">
              {{ monitorEnabled ? '鍋滄1鍒嗛挓璺熻釜' : '寮€鍚?鍒嗛挓璺熻釜' }}
            </el-button>
            <el-button :loading="monitorLoading" @click="runMonitorNow">娴嬭瘯閫氱煡</el-button>
            <el-button :loading="allRefreshLoading || marketGateLoading || buyPoolLoading || gen2ShadowLoading || signalLoading" @click="refreshAllHoldingData">涓€閿埛鏂板叏閮?/el-button>
          </div>
        </div>
        <div class="toolbar-note">鍒嗛挓璺熻釜鐘舵€侊細{{ monitorStatusText }}</div>
        <div class="toolbar-note">鐩樹腑鑷姩鍒锋柊锛歿{ autoRefreshStatusText }}</div>
        <div class="toolbar-note">閫氱煡閭缁熶竴鍙栬嚜绯荤粺閰嶇疆锛屼笉鍐嶅湪瀹炵洏浜ゆ槗椤靛崟鐙淮鎶ゃ€?/div>
        <div class="toolbar-note">鏂板鎸佷粨锛氱敤浜庢寜绛栫暐璁″垝鏂板缓璺熻釜浠撲綅锛涘凡瀹為檯鍙戠敓鐨勬寔浠撹浼樺厛閫氳繃鍚岃姳椤鸿祫閲戞寔鑲″悓姝ャ€?/div>
        <div class="toolbar-note">褰撳墠鍚堝苟浜嗘€昏祫閲戙€佸綋鍓嶅疄鐩樻寔浠撲笌鎸佷粨绾緥锛屼究浜庡湪涓€涓潰鏉块噷鐪嬫竻璧勯噾鍜岃偂绁ㄣ€?/div>
        <div style="height: 10px" />
        <div v-if="!isGen2LiveMode" class="sub-panel">
          <div class="sub-panel-header">
            <div>
              <div class="sub-panel-title">{{ buyPoolPanelTitle }}</div>
              <div class="toolbar-note">{{ buyPoolSummary }}</div>
            </div>
            <el-button size="small" :loading="buyPoolLoading" @click="fetchBuyPool(selectedDate)">鍒锋柊鍊欓€?/el-button>
          </div>
          <el-table :data="buyPoolRows" stripe size="small" empty-text="鏆傛棤涔板叆瑙傚療姹犲€欓€?>
            <el-table-column label="姹犳帓鍚? width="76">
              <template #default="{ row }">{{ row.pool_rank || '--' }}</template>
            </el-table-column>
            <el-table-column label="浠ｇ爜" width="110">
              <template #default="{ row }">
                <el-button type="primary" link @click="copyPlainCode(row.code)">{{ normalizeCode(row.code) || '--' }}</el-button>
              </template>
            </el-table-column>
            <el-table-column label="鍚嶇О" min-width="110">
              <template #default="{ row }">
                <el-button type="primary" link @click="openStockDetail(row)">{{ row.name || '--' }}</el-button>
              </template>
            </el-table-column>
            <el-table-column label="V4鎺掑悕" width="86">
              <template #default="{ row }">{{ row.rank || '--' }}</template>
            </el-table-column>
            <el-table-column label="V4鍒嗘暟" width="90">
              <template #default="{ row }">{{ formatScore(row.score_total) }}</template>
            </el-table-column>
            <el-table-column label="涔扮偣绫诲瀷" min-width="132" show-overflow-tooltip>
              <template #default="{ row }">{{ row.setup_label || '--' }}</template>
            </el-table-column>
            <el-table-column label="5鏃ュ姩閲? width="96">
              <template #default="{ row }">{{ formatPct(row.mom5_pct) }}</template>
            </el-table-column>
            <el-table-column label="10鏃ュ姩閲? width="96">
              <template #default="{ row }">{{ formatPct(row.mom10_pct) }}</template>
            </el-table-column>
            <el-table-column label="缂╅噺" width="86">
              <template #default="{ row }">{{ formatRatio(row.shrink_2d_ratio || row.shrink_1d_ratio) }}</template>
            </el-table-column>
            <el-table-column label="鏉垮潡" min-width="116" show-overflow-tooltip>
              <template #default="{ row }">{{ row.sector_name || '--' }}</template>
            </el-table-column>
            <el-table-column label="鏉垮潡璇勫垎" width="96">
              <template #default="{ row }">{{ formatScore(row.sector_score) }}</template>
            </el-table-column>
            <el-table-column label="鐘舵€? width="92">
              <template #default="{ row }">
                <el-tag :type="row.can_buy ? 'success' : (row.candidate_status === 'primary' ? 'warning' : 'info')" effect="light">
                  {{ buyPoolStatusText(row) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="reason_text" label="鍏ユ睜鍘熷洜" min-width="260" show-overflow-tooltip />
            <el-table-column label="鎿嶄綔" width="138" fixed="right">
              <template #default="{ row }">
                <el-button type="primary" link @click="openAddHoldingDialog(row, 'new')">鏂板鎸佷粨</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div v-if="gen2VerificationQueueRows.length" class="verification-queue">
            <div class="sub-panel-title">Alpha191楠岃瘉寰呭姙</div>
            <div class="toolbar-note">鏈€杩戝凡鏈夎窡韪粨鏋溿€佸皻鏈墦鏍囩殑鍏ㄥ眬褰卞瓙鏍锋湰锛屼紭鍏堜粠杩欓噷寮€濮嬪鐩樸€?/div>
            <el-table :data="gen2VerificationQueueRows" stripe size="small" empty-text="鏆傛棤寰呴獙璇佹牱鏈?>
              <el-table-column label="淇″彿鏃? width="104">
                <template #default="{ row }">{{ row.entry_date || '--' }}</template>
              </el-table-column>
              <el-table-column label="浠ｇ爜" width="100">
                <template #default="{ row }">
                  <el-button type="primary" link @click="copyPlainCode(row.code)">{{ normalizeCode(row.code) || '--' }}</el-button>
                </template>
              </el-table-column>
              <el-table-column label="鍚嶇О" min-width="110">
                <template #default="{ row }">
                  <el-button type="primary" link @click="openStockDetail(row)">{{ row.name || '--' }}</el-button>
                </template>
              </el-table-column>
              <el-table-column label="纭鏃堕棿" width="150" show-overflow-tooltip>
                <template #default="{ row }">{{ row.confirm_datetime || '--' }}</template>
              </el-table-column>
              <el-table-column label="Alpha鍒? width="86">
                <template #default="{ row }">{{ formatScore(row.alpha191_gate_score) }}</template>
              </el-table-column>
              <el-table-column label="5/10/20鏃? width="142">
                <template #default="{ row }">{{ formatPct(row.fwd5_pct) }} / {{ formatPct(row.fwd10_pct) }} / {{ formatPct(row.fwd20_pct) }}</template>
              </el-table-column>
              <el-table-column label="姝㈡崯" width="74">
                <template #default="{ row }">
                  <el-tag :type="row.stop5_touch_30m ? 'danger' : 'success'" effect="light" size="small">
                    {{ row.stop5_touch_30m ? '瑙﹀彂' : '鏈Е鍙? }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="鎿嶄綔" width="92" fixed="right">
                <template #default="{ row }">
                  <el-button type="primary" link @click="openGen2VerificationDialog(row)">楠岃瘉</el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </div>
        <div style="height: 12px" />
        <div class="sub-panel">
          <div class="sub-panel-header">
            <div>
              <div class="sub-panel-title">{{ gen2ShadowPanelTitle }}</div>
              <div class="toolbar-note">{{ gen2ShadowSummary }}</div>
              <div class="toolbar-note warning-note">姝ゅ尯鏄奖瀛愯窡韪?澶嶇洏姹狅紝涓嶆槸寰呬拱鍏ユ竻鍗曪紱鍙湁鐘舵€佹槑纭负鈥滃彲涔板叆鈥濈殑琛屾墠鍏佽鏂板鎸佷粨銆?/div>
              <div class="toolbar-note">{{ gen2VerificationSummaryText }}</div>
              <div class="toolbar-note">
                鏅嬬骇鎻愮ず锛?                <el-tag :type="gen2VerificationPromotionType" effect="light" size="small">{{ gen2VerificationPromotionLabel }}</el-tag>
                {{ gen2VerificationPromotionMessage }}
              </div>
              <div v-if="gen2ShadowFreshnessNote" class="toolbar-note warning-note">{{ gen2ShadowFreshnessNote }}</div>
              <div v-if="gen2ShadowUpdateText" class="toolbar-note">{{ gen2ShadowUpdateText }}</div>
              <div class="toolbar-note">{{ gen2ShadowMonitorText }}</div>
              <div v-if="gen2ShadowMonitorResultText" class="toolbar-note">{{ gen2ShadowMonitorResultText }}</div>
            </div>
            <div class="actions">
              <el-tag v-if="isGen2LiveMode" type="info" effect="light">Alpha191 volume5 褰卞瓙璺熻釜/澶嶇洏姹?/el-tag>
              <el-select v-else v-model="gen2Alpha191Gate" size="small" style="width: 230px">
                <el-option label="Alpha191 off" value="off" />
                <el-option label="Alpha191 volume5 褰卞瓙璺熻釜/澶嶇洏姹? value="volume5_keep80_runup" />
              </el-select>
              <el-button size="small" :loading="gen2ShadowLoading" @click="fetchGen2RiskCoolShadow(selectedDate)">鍒锋柊褰卞瓙</el-button>
              <el-button size="small" type="primary" :loading="gen2ShadowUpdateLoading" @click="updateGen2RiskCoolShadow(selectedDate)">鏇存柊褰卞瓙浜ゆ槗</el-button>
              <el-button size="small" :loading="gen2ShadowMonitorLoading" @click="toggleGen2ShadowMonitor">
                {{ gen2ShadowMonitorStatus?.enabled ? '鍋滄15鍒嗛挓鎺㈡祴' : '寮€鍚?5鍒嗛挓鎺㈡祴' }}
              </el-button>
              <el-button size="small" type="warning" plain :loading="gen2ShadowMonitorLoading" @click="runGen2ShadowMonitorOnce">绔嬪嵆鎺㈡祴閭欢</el-button>
            </div>
          </div>
          <el-table :data="shadowReviewRows" stripe size="small" empty-text="鏆傛棤G2褰卞瓙瑙傚療鍊欓€?>
            <el-table-column label="鐘舵€? width="96">
              <template #default="{ row }">
                <el-tag :type="gen2ShadowTagType(row)" effect="light">{{ gen2ShadowStatusText(row) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="瀹炵洏楠岃瘉" width="104">
              <template #default="{ row }">
                <el-tag :type="gen2VerificationTagType(row)" effect="light">{{ gen2VerificationText(row) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="浠ｇ爜" width="110">
              <template #default="{ row }">
                <el-button type="primary" link @click="copyPlainCode(row.code)">{{ normalizeCode(row.code) || '--' }}</el-button>
              </template>
            </el-table-column>
            <el-table-column label="鍚嶇О" min-width="110">
              <template #default="{ row }">
                <el-button type="primary" link @click="openStockDetail(row)">{{ row.name || '--' }}</el-button>
              </template>
            </el-table-column>
            <el-table-column label="褰卞瓙鎺掑悕" width="86">
              <template #default="{ row }">{{ row.day_signal_rank || '--' }}</template>
            </el-table-column>
            <el-table-column label="纭鏃堕棿" width="150" show-overflow-tooltip>
              <template #default="{ row }">{{ row.confirm_datetime || '--' }}</template>
            </el-table-column>
            <el-table-column label="鍏ュ満浠? width="86">
              <template #default="{ row }">{{ formatPrice(row.entry_price) }}</template>
            </el-table-column>
            <el-table-column label="V4鎺掑悕" width="86">
              <template #default="{ row }">{{ row.v4_rank || '--' }}</template>
            </el-table-column>
            <el-table-column label="Alpha191" width="150">
              <template #default="{ row }">
                {{ formatScore(row.alpha191_volume5_score ?? row.alpha191_gate_score) }} / {{ row.alpha191_volume5_rank_in_day || row.alpha191_original_v4_rank || '--' }}
              </template>
            </el-table-column>
            <el-table-column label="娑ㄥ箙/鍘嬪姏" width="116">
              <template #default="{ row }">{{ formatPct(row.runup_from_60d_low) }} / {{ formatPct(row.overhead_pressure_amount_share) }}</template>
            </el-table-column>
            <el-table-column label="娑ㄥ箙/閲忔瘮" width="116">
              <template #default="{ row }">{{ formatPct(row.rt_return_pct) }} / {{ formatRatio(row.amount_ratio) }}</template>
            </el-table-column>
            <el-table-column label="5/10/20鏃ヨ窡韪? width="142">
              <template #default="{ row }">{{ formatPct(row.fwd5_pct) }} / {{ formatPct(row.fwd10_pct) }} / {{ formatPct(row.fwd20_pct) }}</template>
            </el-table-column>
            <el-table-column label="姝㈡崯" width="74">
              <template #default="{ row }">
                <el-tag :type="row.stop5_touch_30m ? 'danger' : 'success'" effect="light" size="small">
                  {{ row.stop5_touch_30m ? '瑙﹀彂' : '鏈Е鍙? }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="reason_text" label="鍘熷洜" min-width="240" show-overflow-tooltip />
            <el-table-column label="楠岃瘉澶囨敞" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">{{ gen2VerificationNoteText(row) }}</template>
            </el-table-column>
            <el-table-column label="鎿嶄綔" width="176" fixed="right">
              <template #default="{ row }">
                <el-button type="primary" link @click="openGen2VerificationDialog(row)">楠岃瘉</el-button>
                <el-button
                  type="primary"
                  link
                  :disabled="!gen2ShadowCanAddHolding(row)"
                  @click="openAddHoldingDialog(row, 'new')"
                >
                  {{ gen2ShadowCanAddHolding(row) ? '鏂板鎸佷粨' : '浠呭鐩? }}
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
        <div style="height: 12px" />
        <div class="sub-panel-header holding-list-header">
          <div>
            <div class="sub-panel-title">褰撳墠瀹炵洏鎸佷粨</div>
            <div class="toolbar-note">涓€閿洿鏂拌偂浠枫€佺泩浜忛噾棰濄€乂4鎺掑悕銆佽瘎鍒嗘睜鐘舵€佸拰鍗栫偣淇″彿銆?/div>
          </div>
          <el-button
            type="primary"
            size="small"
            :loading="holdingRefreshLoading || allRefreshLoading"
            @click="refreshCurrentHoldings"
          >
            涓€閿洿鏂版寔浠?          </el-button>
        </div>
        <el-table :data="currentHoldingRows" class="current-holding-table" stripe size="small" :fit="false" empty-text="鏆傛棤褰撳墠瀹炵洏鎸佷粨">
          <el-table-column label="浠ｇ爜" width="100">
            <template #default="{ row }">
              <el-button type="primary" link @click="openStockDetail(row)">{{ displayCode(row.code) }}</el-button>
            </template>
          </el-table-column>
          <el-table-column label="鍚嶇О" width="110" show-overflow-tooltip>
            <template #default="{ row }">
              <el-button type="primary" link @click="openStockDetail(row)">{{ row.name || '--' }}</el-button>
            </template>
          </el-table-column>
          <el-table-column label="V4鎺掑悕/鍒嗘暟" width="112">
            <template #default="{ row }">
              {{ v4SourceFor(row).rank_text }} / {{ v4SourceFor(row).score_text }}
            </template>
          </el-table-column>
          <el-table-column label="鏄惁鍦ㄦ睜" width="86">
            <template #default="{ row }">
              <el-tag :type="v4SourceFor(row).in_pool ? 'success' : 'danger'" effect="light" size="small">
                {{ v4SourceFor(row).in_pool ? '鍦ㄦ睜' : '鎺夋睜' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="shares" label="鏁伴噺" width="74" />
          <el-table-column prop="cost_price" label="鎴愭湰" width="80" />
          <el-table-column prop="current_price" label="褰撳墠浠? width="80">
            <template #default="{ row }">{{ formatPrice(row.current_price) }}</template>
          </el-table-column>
          <el-table-column label="甯傚€? width="102">
            <template #default="{ row }">{{ formatMoney(holdingMarketValue(row)) }}</template>
          </el-table-column>
          <el-table-column label="娴姩鐩堜簭" width="102">
            <template #default="{ row }">
              <span :style="aSharePnlStyle(floatingPnlAmount(row))">
                {{ formatMoney(floatingPnlAmount(row)) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="褰撴棩娑ㄨ穼" width="92">
            <template #default="{ row }">
              <span :style="aSharePnlStyle(dayChangePct(row))">
                {{ formatSignedPct(dayChangePct(row)) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column prop="pnl_ratio" label="鐩堜簭%" width="82">
            <template #default="{ row }">
              <span :style="aSharePnlStyle(row.pnl_ratio)">{{ formatPct(row.pnl_ratio) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="姝㈡崯浠? width="84">
            <template #default="{ row }">
              <span class="stop-loss-price">{{ formatPrice(stopLossPrice(row)) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="绾緥鐘舵€? width="104">
            <template #default="{ row }">
              <el-tag :type="disciplineTagType(row)" effect="light" size="small">{{ disciplineTagText(row) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="RSI鑳岀" width="112">
            <template #default="{ row }">
              <div class="rsi-cell">
                <el-tag :type="row.signal?.rsi15_signal?.detected ? 'danger' : 'success'" effect="light" size="small">
                  15{{ row.signal?.rsi15_signal?.detected ? '瑙? : '-' }}
                </el-tag>
                <el-tag :type="row.signal?.rsi30_signal?.detected ? 'danger' : 'success'" effect="light" size="small">
                  30{{ row.signal?.rsi30_signal?.detected ? '瑙? : '-' }}
                </el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="signal.suggestion" label="鍗栧嚭寤鸿" width="144" show-overflow-tooltip />
          <el-table-column prop="buy_time" label="涔板叆鏃堕棿" width="136" />
          <el-table-column label="鎿嶄綔" width="106">
            <template #default="{ row, $index }">
              <div class="op-cell">
                <el-button type="warning" link @click="openSellDialog(row, $index)">鍗栧嚭</el-button>
                <el-button type="danger" link @click="removeManualHolding($index)">鍒犻櫎</el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>

        <div v-if="focusCode" style="height: 12px" />
        <template v-if="focusCode">
          <el-alert
            type="info"
            :closable="false"
            :title="`褰撳墠鏍囩殑锛?{focusCode}${focusName ? ' ' + focusName : ''}`"
            :description="focusDescription"
            class="panel-alert"
          />
          <el-table :data="focusRows" stripe size="small" empty-text="褰撳墠蹇収鏈懡涓鏍囩殑锛屼粛鍙姞鍏ユ寔浠?>
            <el-table-column label="浠ｇ爜" width="110">
              <template #default="{ row }">
                <el-button type="primary" link @click="openStockDetail(row)">{{ displayCode(row.code) }}</el-button>
              </template>
            </el-table-column>
            <el-table-column label="鍚嶇О" min-width="120">
              <template #default="{ row }">
                <el-button type="primary" link @click="openStockDetail(row)">{{ row.name || '--' }}</el-button>
              </template>
            </el-table-column>
            <el-table-column prop="rank" label="鎺掑悕" width="80" />
            <el-table-column prop="score_total" label="鎬诲垎" width="90" />
            <el-table-column prop="reason_text" label="璇存槑" min-width="220" show-overflow-tooltip />
            <el-table-column label="鎿嶄綔" width="120">
              <template #default="{ row }">
                <el-button type="primary" link @click="openAddHoldingDialog(row)">鍔犲叆鎸佷粨</el-button>
              </template>
            </el-table-column>
          </el-table>
        </template>
      </div>

      <div class="panel">
        <div class="panel-title">鍘嗗彶鎴愪氦涓庢寔浠撹建杩?/div>
        <div class="holding-toolbar">
          <div class="actions">
            <el-button @click="openThsImportDialog">璇诲彇鍚岃姳椤烘垚浜?/el-button>
          </div>
        </div>
        <div class="toolbar-note">鎸夎偂绁ㄥ垎缁勫睍绀哄悓鑺遍『鍘嗗彶鎴愪氦锛岄粯璁ゆ寜鏈€杩戜拱鍏ユ椂闂村€掑簭锛涘睍寮€鍚庡彲鏌ョ湅璇ヨ偂瀹屾暣涔板崠鏄庣粏銆?/div>
        <el-table :data="pagedHistoryTradeGroups" stripe size="small" empty-text="鏆傛棤鍘嗗彶鎴愪氦">
          <el-table-column type="expand" width="52">
            <template #default="{ row }">
              <el-table :data="row.trades" size="small" stripe empty-text="璇ヨ偂鏆傛棤鏄庣粏">
                <el-table-column prop="time" label="鎴愪氦鏃堕棿" width="160" />
                <el-table-column prop="side" label="鏂瑰悜" width="72" />
                <el-table-column prop="shares" label="鏁伴噺" width="88" />
                <el-table-column prop="price" label="鎴愪氦浠? width="90">
                  <template #default="{ row: trade }">{{ formatPrice(trade.price) }}</template>
                </el-table-column>
                <el-table-column prop="before_shares" label="鍙樺姩鍓? width="88" />
                <el-table-column prop="after_shares" label="鍙樺姩鍚? width="88" />
                <el-table-column prop="realized_pnl" label="宸插疄鐜扮泩浜? width="120">
                  <template #default="{ row: trade }">
                    <span :style="aSharePnlStyle(trade.realized_pnl)">{{ formatMoney(trade.realized_pnl) }}</span>
                  </template>
                </el-table-column>
                <el-table-column prop="reason" label="鍘熷洜/澶囨敞" min-width="180" show-overflow-tooltip />
              </el-table>
            </template>
          </el-table-column>
          <el-table-column label="浠ｇ爜" width="110">
            <template #default="{ row }">
              <el-button type="primary" link @click="openStockDetail(row)">{{ displayCode(row.code) }}</el-button>
            </template>
          </el-table-column>
          <el-table-column label="鍚嶇О" min-width="120">
            <template #default="{ row }">
              <el-button type="primary" link @click="openStockDetail(row)">{{ row.name || '--' }}</el-button>
            </template>
          </el-table-column>
          <el-table-column prop="latest_buy_time" label="鏈€杩戜拱鍏ユ椂闂? width="160" />
          <el-table-column prop="first_buy_time" label="棣栨涔板叆鏃堕棿" width="160" />
          <el-table-column prop="buy_count" label="涔板叆娆℃暟" width="88" />
          <el-table-column prop="sell_count" label="鍗栧嚭娆℃暟" width="88" />
          <el-table-column prop="net_shares" label="鍑€鎸佷粨鑲℃暟" width="110" />
          <el-table-column label="褰撳墠鐘舵€? width="100">
            <template #default="{ row }">
              <el-tag :type="row.is_active ? 'success' : 'info'" effect="light">{{ row.is_active ? '鎸佹湁涓? : '宸插崠鍑? }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="褰撳墠鎸佷粨" width="100">
            <template #default="{ row }">{{ row.current_shares > 0 ? row.current_shares : '--' }}</template>
          </el-table-column>
          <el-table-column label="绱宸插疄鐜扮泩浜? width="130">
            <template #default="{ row }">
              <span :style="aSharePnlStyle(row.realized_pnl)">{{ formatMoney(row.realized_pnl) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="last_trade_time" label="鏈€杩戞垚浜? width="160" />
          <el-table-column prop="summary" label="鎽樿" min-width="240" show-overflow-tooltip />
        </el-table>
        <div class="history-pagination" v-if="historyTradeGroups.length > historyPageSize">
          <el-pagination
            v-model:current-page="historyPage"
            v-model:page-size="historyPageSize"
            :page-sizes="[10, 20, 50, 100]"
            :total="historyTradeGroups.length"
            layout="total, sizes, prev, pager, next, jumper"
            small
          />
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">绛栫暐鎸佷粨涓庡緟鍗栧嚭瑙傚療</div>
        <el-table :data="positions.rows || []" stripe size="small" empty-text="绛栫暐褰撳墠绌轰粨">
          <el-table-column label="浠ｇ爜" width="110">
            <template #default="{ row }">
              <el-button type="primary" link @click="openStockDetail(row)">{{ displayCode(row.code) }}</el-button>
            </template>
          </el-table-column>
          <el-table-column label="鍚嶇О" min-width="120">
            <template #default="{ row }">
              <el-button type="primary" link @click="openStockDetail(row)">{{ row.name || '--' }}</el-button>
            </template>
          </el-table-column>
          <el-table-column prop="shares" label="鏁伴噺" width="88" />
          <el-table-column prop="avg_cost" label="鎴愭湰" width="88" />
          <el-table-column prop="current_price" label="褰撳墠浠? width="88">
            <template #default="{ row }">{{ formatPrice(row.current_price) }}</template>
          </el-table-column>
          <el-table-column prop="pnl_ratio" label="鐩堜簭%" width="88">
            <template #default="{ row }">
              <span :style="aSharePnlStyle(row.pnl_ratio)">{{ formatPct(row.pnl_ratio) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="hold_days" label="鎸佹湁澶╂暟" width="96" />
        </el-table>
      </div>

      <div class="panel">
        <div class="sub-panel-header">
          <div>
            <div class="panel-title">妯℃嫙鐩樻ˉ鎺ュ洖鎵?/div>
            <div class="toolbar-note">缁熶竴鏌ョ湅 bridge 璁㈠崟銆佹垚浜や笌鏈€鏂版寔浠撳揩鐓э紱褰?PTrade 寮€濮嬪洖鍐欏悗锛岃繖閲屽氨鏄ā鎷熺洏闂幆鏍稿闈㈡澘銆?/div>
            <div class="toolbar-note">{{ ptradeBridgeSummary }}</div>
            <div class="toolbar-note">{{ ptradeBridgeQueueText }}</div>
          </div>
          <div class="actions">
            <el-button :loading="ptradeBridgeLoading" @click="fetchPtradeBridgeState">鍒锋柊妗ユ帴鐘舵€?/el-button>
            <el-button :loading="ptradeBridgeProbeLoading" @click="runPtradeBridgeProbe(false)">鍙鎺㈤拡</el-button>
            <el-button type="warning" plain :loading="ptradeBridgeProbeLoading" @click="runPtradeBridgeProbe(true)">dry-run 鎺㈤拡</el-button>
            <el-button type="success" plain :loading="ptradeBridgeAcceptanceLoading" @click="runPtradeBridgeAcceptanceGate">PTrade 楠屾敹闂ㄧ</el-button>
            <el-button type="info" plain :loading="ptradeBridgeWatchAcceptanceLoading" @click="runPtradeBridgeWatchAcceptance">绛夊緟 PTrade 骞堕獙鏀?/el-button>
            <el-button
              type="danger"
              plain
              :loading="ptradeBridgeLiveSubmitTestLoading"
              :disabled="!ptradeBridgeAudit?.gates?.live_submit_ready"
              @click="runPtradeBridgeLiveSubmitTest"
            >
              live-submit 灏忛楠屾敹
            </el-button>
            <el-button
              type="primary"
              :disabled="!(ptradeBridgePositions?.latest?.ok && (ptradeBridgePositions?.latest?.positions || []).length)"
              @click="importPtradePositionsToManualHoldings"
            >
              瀵煎叆妯℃嫙鐩樻寔浠?            </el-button>
          </div>
        </div>
        <el-alert
          v-if="ptradeBridgePositions?.latest?.snapshot_time"
          type="info"
          :closable="false"
          :title="`鏈€鏂版寔浠撳揩鐓э細${ptradeBridgePositions.latest.snapshot_time}`"
          :description="ptradeBridgePositions.latest.file || ''"
          class="panel-alert"
        />
        <el-alert
          v-if="ptradeBridgeProbeResult"
          :type="ptradeBridgeProbeResult.ok ? 'success' : 'warning'"
          :closable="false"
          :title="ptradeBridgeProbeSummary"
          :description="ptradeBridgeProbeResult.note || ''"
          class="panel-alert"
        />
        <el-alert
          v-if="ptradeBridgeAcceptanceResult"
          :type="ptradeBridgeAcceptanceResult.ok ? 'success' : 'warning'"
          :closable="false"
          :title="ptradeBridgeAcceptanceSummary"
          :description="ptradeBridgeAcceptanceResult.note || ''"
          class="panel-alert"
        />
        <el-alert
          v-if="ptradeBridgeWatchAcceptanceResult"
          :type="ptradeBridgeWatchAcceptanceResult.ok ? 'success' : 'warning'"
          :closable="false"
          :title="ptradeBridgeWatchAcceptanceSummary"
          :description="ptradeBridgeWatchAcceptanceResult.note || ''"
          class="panel-alert"
        />
        <el-alert
          v-if="ptradeBridgeLiveSubmitTestResult"
          :type="ptradeBridgeLiveSubmitTestResult.ok ? 'success' : 'warning'"
          :closable="false"
          :title="ptradeBridgeLiveSubmitTestSummary"
          :description="ptradeBridgeLiveSubmitTestResult.note || ''"
          class="panel-alert"
        />
        <el-alert
          v-if="ptradeBridgeEvidence"
          :type="ptradeBridgeEvidence.ok ? 'success' : 'warning'"
          :closable="false"
          :title="ptradeBridgeEvidenceSummary"
          :description="Array.isArray(ptradeBridgeEvidence.next_actions) ? ptradeBridgeEvidence.next_actions[0] : ''"
          class="panel-alert"
        />
        <div v-if="ptradeBridgeEvidenceRows.length" class="sub-panel" style="margin-top: 12px;">
          <div class="sub-panel-title">PTrade 楠屾敹璇佹嵁</div>
          <el-table :data="ptradeBridgeEvidenceRows" stripe size="small" empty-text="鏆傛棤 PTrade 楠屾敹璇佹嵁">
            <el-table-column prop="name" label="鏉′欢" min-width="180" />
            <el-table-column label="鐘舵€? width="90">
              <template #default="{ row }">
                <el-tag :type="row.ok ? 'success' : 'warning'" size="small">{{ row.ok ? '閫氳繃' : '鏈€氳繃' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="evidence" label="璇佹嵁" min-width="260" show-overflow-tooltip />
            <el-table-column prop="next_action" label="涓嬩竴姝? min-width="320" show-overflow-tooltip />
          </el-table>
        </div>
        <div class="sub-panel" style="margin-top: 12px;">
          <div class="sub-panel-title">鏈€鏂版ā鎷熺洏鎸佷粨</div>
          <el-table :data="ptradeLatestPositionRows" stripe size="small" empty-text="鏆傛棤妯℃嫙鐩樻寔浠撳揩鐓?>
            <el-table-column prop="code" label="浠ｇ爜" width="110">
              <template #default="{ row }">{{ displayCode(row.code) }}</template>
            </el-table-column>
            <el-table-column prop="name" label="鍚嶇О" min-width="120" />
            <el-table-column prop="shares" label="鏁伴噺" width="90" />
            <el-table-column label="鎴愭湰" width="90">
              <template #default="{ row }">{{ formatPrice(row.cost_price) }}</template>
            </el-table-column>
            <el-table-column label="鐜颁环" width="90">
              <template #default="{ row }">{{ formatPrice(row.current_price) }}</template>
            </el-table-column>
            <el-table-column label="甯傚€? width="110">
              <template #default="{ row }">{{ formatMoney(row.market_value) }}</template>
            </el-table-column>
            <el-table-column label="鐩堜簭%" width="90">
              <template #default="{ row }"><span :style="aSharePnlStyle(row.pnl_ratio)">{{ formatPct(row.pnl_ratio) }}</span></template>
            </el-table-column>
          </el-table>
        </div>
        <div class="sub-panel" style="margin-top: 12px;">
          <div class="sub-panel-title">鏈€杩戞ā鎷熺洏鎴愪氦</div>
          <el-table :data="ptradeBridgeFills" stripe size="small" empty-text="鏆傛棤妯℃嫙鐩樻垚浜ゅ洖鍐?>
            <el-table-column prop="order_id" label="璁㈠崟鍙? width="180" show-overflow-tooltip />
            <el-table-column prop="code" label="浠ｇ爜" width="110">
              <template #default="{ row }">{{ displayCode(row.code) }}</template>
            </el-table-column>
            <el-table-column prop="side" label="鏂瑰悜" width="80" />
            <el-table-column prop="quantity" label="鏁伴噺" width="90" />
            <el-table-column label="鎴愪氦浠? width="90">
              <template #default="{ row }">{{ formatPrice(row.fill_price ?? row.price) }}</template>
            </el-table-column>
            <el-table-column prop="filled_at" label="鎴愪氦鏃堕棿" width="168" show-overflow-tooltip />
            <el-table-column prop="message" label="璇存槑" min-width="180" show-overflow-tooltip />
          </el-table>
        </div>
        <div class="sub-panel" style="margin-top: 12px;">
          <div class="sub-panel-title">鏈€杩戞ā鎷熺洏璁㈠崟</div>
          <el-table :data="ptradeBridgeOrders.slice(0, 10)" stripe size="small" empty-text="鏆傛棤妯℃嫙鐩樿鍗?>
            <el-table-column prop="order_id" label="璁㈠崟鍙? width="180" show-overflow-tooltip />
            <el-table-column prop="code" label="浠ｇ爜" width="110">
              <template #default="{ row }">{{ displayCode(row.code) }}</template>
            </el-table-column>
            <el-table-column prop="side" label="鏂瑰悜" width="80" />
            <el-table-column prop="quantity" label="鏁伴噺" width="90" />
            <el-table-column label="浠锋牸" width="90">
              <template #default="{ row }">{{ formatPrice(row.price) }}</template>
            </el-table-column>
            <el-table-column prop="_bridge_status" label="鐘舵€? width="110" />
            <el-table-column prop="created_at" label="鍒涘缓鏃堕棿" width="168" show-overflow-tooltip />
            <el-table-column prop="reason" label="璇存槑" min-width="180" show-overflow-tooltip />
          </el-table>
        </div>
      </div>
    </template>

    <el-dialog v-model="gen2VerificationDialogVisible" title="G2褰卞瓙淇″彿楠岃瘉" width="480px">
      <el-form label-width="90px">
        <el-form-item label="鏍囩殑">
          <span>
            {{ normalizeCode(gen2VerificationForm.row?.code) || '--' }}
            {{ gen2VerificationForm.row?.name || '' }}
          </span>
        </el-form-item>
        <el-form-item label="楠岃瘉鐘舵€?>
          <el-select v-model="gen2VerificationForm.status" style="width: 220px">
            <el-option
              v-for="item in gen2VerificationOptions"
              :key="item.value || 'blank'"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="浠撲綅%">
          <el-input-number v-model="gen2VerificationForm.position_pct" :min="0" :max="100" :step="5" style="width: 180px" />
        </el-form-item>
        <el-form-item label="鎴愪氦浠?>
          <el-input-number v-model="gen2VerificationForm.fill_price" :min="0" :precision="3" :step="0.01" style="width: 180px" />
        </el-form-item>
        <el-form-item label="澶囨敞">
          <el-input
            v-model="gen2VerificationForm.note"
            type="textarea"
            :rows="3"
            placeholder="璁板綍涓轰粈涔堣窡銆佷负浠€涔堟斁寮冦€佺洏涓墽琛屽亸宸瓑"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="gen2VerificationDialogVisible = false">鍙栨秷</el-button>
        <el-button type="primary" :loading="gen2VerificationSaving" @click="saveGen2Verification">淇濆瓨楠岃瘉</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="addDialogVisible" title="鏂板瀹炵洏鎸佷粨" width="420px">
      <el-form label-width="90px">
        <el-form-item label="浠ｇ爜">
          <el-autocomplete
            v-model="addForm.code"
            :fetch-suggestions="querySearchStock"
            placeholder="杈撳叆浠ｇ爜鎴栧悕绉版ā绯婂尮閰?
            clearable
            @select="handleCodeSelect"
          />
        </el-form-item>
        <el-form-item label="鍚嶇О">
          <el-input v-model="addForm.name" placeholder="鍙€? />
        </el-form-item>
        <el-form-item v-if="codeLookupText" label="鍖归厤缁撴灉">
          <span>{{ codeLookupText }}</span>
        </el-form-item>
        <el-form-item label="鑲℃暟">
          <el-input-number v-model="addForm.shares" :min="0" :step="100" style="width: 180px" />
        </el-form-item>
        <el-form-item label="鎴愭湰浠?>
          <el-input-number v-model="addForm.cost_price" :min="0" :precision="3" :step="0.01" style="width: 180px" />
        </el-form-item>
        <el-form-item label="瀹炴椂鐩堜簭">
          <span :style="{ color: livePnlColor }">{{ livePnlText }}</span>
        </el-form-item>
        <el-form-item label="涔板叆鏃堕棿">
          <el-date-picker
            v-model="addForm.buy_time"
            type="datetime"
            value-format="YYYY-MM-DD HH:mm"
            format="YYYY-MM-DD HH:mm"
            placeholder="绮剧‘鍒板垎閽?
            style="width: 220px"
          />
        </el-form-item>
        <el-form-item v-if="addMode === 'new'" label="鍘嗗彶棰勬湡">
          <div v-if="entryBacktestLoading">姝ｅ湪璇勪及鍘嗗彶鏍锋湰...</div>
          <div v-else-if="entryBacktestText">{{ entryBacktestText }}</div>
          <div v-else>杈撳叆浠ｇ爜鍚庤嚜鍔ㄨ瘎浼?/div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addDialogVisible = false">鍙栨秷</el-button>
          <el-button type="primary" @click="confirmAddHolding">{{ addMode === 'new' ? '妫€鏌ヤ氦鏄撹鍒? : '纭鍔犲叆' }}</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="ruleDialogVisible" title="浜ゆ槗瑙勫垯妫€鏌? width="560px">
      <el-alert
        :type="ruleCheckPassed ? 'success' : 'warning'"
        :closable="false"
        :title="ruleCheckPassed ? '瑙勫垯妫€鏌ラ€氳繃锛屽彲浠ュ姞鍏ユ寔浠? : '瀛樺湪涓嶇鍚堢瓥鐣ョ殑瑙勫垯锛岃鍏堣皟鏁?"
        class="panel-alert"
      />
      <el-table :data="ruleChecklist" size="small" stripe>
        <el-table-column prop="label" label="瑙勫垯" min-width="260" />
        <el-table-column label="缁撴灉" width="80">
          <template #default="{ row }">
            <el-tag :type="row.pass ? 'success' : 'danger'" effect="light">{{ row.pass ? '閫氳繃' : '涓嶈繃' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="reason" label="璇存槑" min-width="180" />
      </el-table>
      <template #footer>
        <el-button @click="ruleDialogVisible = false">鍏抽棴</el-button>
        <el-button type="primary" :disabled="!ruleCheckPassed" @click="confirmAddHoldingAfterCheck">閫氳繃骞跺姞鍏?/el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="sellDialogVisible" title="鍗栧嚭鎸佷粨" width="430px">
      <el-form label-width="90px">
        <el-form-item label="浠ｇ爜">
          <span>{{ displayCode(sellForm.code) }}</span>
        </el-form-item>
        <el-form-item label="鍚嶇О">
          <span>{{ sellForm.name || '--' }}</span>
        </el-form-item>
        <el-form-item label="鍙崠鏁伴噺">
          <span>{{ sellForm.max_shares }}</span>
        </el-form-item>
        <el-form-item label="鍗栧嚭鏁伴噺">
          <el-input-number v-model="sellForm.shares" :min="1" :max="Math.max(1, Number(sellForm.max_shares || 1))" :step="1" />
          <el-button link type="primary" @click="sellHalfPosition">鍗栧嚭涓€鍗?/el-button>
          <el-button link type="primary" @click="sellAllPosition">鍏ㄩ儴鍗栧嚭</el-button>
        </el-form-item>
        <el-form-item label="鍗栧嚭浠锋牸">
          <el-input-number v-model="sellForm.price" :min="0" :precision="3" :step="0.01" />
        </el-form-item>
        <el-form-item label="鍗栧嚭鏃堕棿">
          <el-date-picker
            v-model="sellForm.time"
            type="datetime"
            value-format="YYYY-MM-DD HH:mm"
            format="YYYY-MM-DD HH:mm"
            placeholder="绮剧‘鍒板垎閽?
            style="width: 220px"
          />
        </el-form-item>
        <el-form-item label="鍘熷洜">
          <el-input v-model="sellForm.reason" placeholder="姝㈢泩/姝㈡崯/绛栫暐鍗栧嚭" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="sellDialogVisible = false">鍙栨秷</el-button>
        <el-button type="primary" @click="confirmSellHolding">纭鍗栧嚭</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="tradeLogVisible" title="璇曠洏浜ゆ槗璁板綍" width="820px">
      <el-table :data="manualTradeLogs" stripe size="small" empty-text="鏆傛棤浜ゆ槗璁板綍">
        <el-table-column prop="time" label="鏃堕棿" width="150" />
        <el-table-column prop="side" label="鏂瑰悜" width="70" />
        <el-table-column prop="code" label="浠ｇ爜" width="100">
          <template #default="{ row }">{{ displayCode(row.code) }}</template>
        </el-table-column>
        <el-table-column prop="name" label="鍚嶇О" width="120" />
        <el-table-column prop="shares" label="鏁伴噺" width="80" />
        <el-table-column prop="price" label="浠锋牸" width="90" />
        <el-table-column prop="before_shares" label="鍗栧墠" width="80" />
        <el-table-column prop="after_shares" label="鍗栧悗" width="80" />
        <el-table-column prop="realized_pnl" label="宸插疄鐜扮泩浜? width="110">
          <template #default="{ row }">
            <span :style="aSharePnlStyle(row.realized_pnl)">{{ formatMoney(row.realized_pnl) }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="reason" label="鍘熷洜" min-width="120" />
      </el-table>
      <template #footer>
        <el-button @click="tradeLogVisible = false">鍏抽棴</el-button>
        <el-button type="danger" @click="clearTradeLogs">娓呯┖璁板綍</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="thsImportVisible" title="璇诲彇鍚岃姳椤哄巻鍙叉垚浜? width="1100px">
      <el-alert
        type="info"
        :closable="false"
        title="浼樺厛璇诲彇浜ゅ壊鍗曟枃浠讹紱涔熷彲浠庡悓鑺遍『鍘嗗彶鎴愪氦椤靛鍒跺悗璇诲彇鍓创鏉裤€傜郴缁熶細鑷姩杩囨护鏃犳晥鎴愪氦銆?
        class="panel-alert"
      />
      <div class="holding-toolbar">
        <div class="actions">
          <el-button :loading="thsImportLoading" type="success" @click="readThsTradesFromDeliveryFile">璇诲彇浜ゅ壊鍗曟枃浠?/el-button>
          <el-button :loading="thsImportLoading" type="primary" @click="readThsTradesFromWindow">鐩存帴璇诲彇浜ゆ槗绐楀彛</el-button>
          <el-button :loading="thsImportLoading" type="primary" @click="readThsTradesFromClipboard">璇诲彇鍓创鏉?/el-button>
          <el-button :loading="thsImportLoading" @click="parseThsTradeText(thsImportRawText)">瑙ｆ瀽涓嬫柟鏂囨湰</el-button>
        </div>
        <div class="toolbar-note">宸茶瘑鍒?{{ thsImportRows.length }} 鏉″彲瀵煎叆鎴愪氦锛屽緟纭 {{ thsImportCheckedCount }} 鏉?/div>
      </div>
      <el-input
        v-model="thsImportRawText"
        type="textarea"
        :rows="6"
        placeholder="濡傛灉娴忚鍣ㄦ棤娉曠洿鎺ヨ鍙栧壀璐存澘锛岃鎶婂悓鑺遍『澶嶅埗鍐呭绮樿创鍒拌繖閲岋紝鍐嶇偣鍑昏В鏋愪笅鏂规枃鏈?
      />
      <div style="height: 12px" />
      <el-table :data="thsImportRows" stripe size="small" empty-text="鏆傛棤鍙鍏ユ垚浜?>
        <el-table-column label="纭" width="72">
          <template #default="{ row }">
            <el-checkbox v-model="row.checked" />
          </template>
        </el-table-column>
        <el-table-column prop="time" label="鏃堕棿" width="168" />
        <el-table-column prop="side" label="鏂瑰悜" width="80" />
        <el-table-column prop="code" label="浠ｇ爜" width="96" />
        <el-table-column prop="name" label="鍚嶇О" width="120" />
        <el-table-column prop="shares" label="鏁伴噺" width="90" />
        <el-table-column prop="price" label="鎴愪氦浠? width="90" />
        <el-table-column prop="amount" label="鎴愪氦棰? width="110" />
        <el-table-column prop="remark" label="澶囨敞" min-width="160" show-overflow-tooltip />
        <el-table-column prop="filter_reason" label="璇嗗埆璇存槑" min-width="160" show-overflow-tooltip />
      </el-table>
      <template #footer>
        <el-button @click="thsImportVisible = false">鍙栨秷</el-button>
        <el-button type="primary" :disabled="thsImportCheckedCount <= 0" @click="confirmImportThsTrades">鍕鹃€夌‘璁ゅ鍏?/el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import dayjs from 'dayjs'
import { useRoute, useRouter } from 'vue-router'
import request from '@/utils/request'
import {
  getGen2Live,
  getV4MarketGate,
  getGen2RiskCoolShadow,
  getGen2WorkflowStatus,
  runGen2RiskCoolShadowUpdate,
  getGen2RiskCoolShadowUpdateTask,
  saveGen2ShadowVerification,
  getGen2ShadowMonitorStatus,
  setGen2ShadowMonitorConfig,
  runGen2ShadowMonitorNow,
  getV4ManualHoldingSignals,
  getV4ManualHoldingQuotes,
  refreshV4ManualHoldingAll,
  saveV4ManualHoldingState,
  getV4ManualHoldingMonitorStatus,
  setV4ManualHoldingMonitorConfig,
  runV4ManualHoldingMonitorNow,
  getV4ManualHoldingEntryBacktest,
  readV4ManualHoldingThsCurrentTable,
  readV4ManualHoldingThsDeliveryFile,
  readV4ManualHoldingThsCapitalHoldings,
  getPtradeBridgeStatus,
  getPtradeBridgeReadinessAudit,
  getPtradeBridgeEvidenceReport,
  listPtradeBridgeOrders,
  listPtradeBridgeFills,
  getPtradeBridgePositions,
  runPtradeBridgeLiveProbe,
  startPtradeBridgeAcceptance,
  getPtradeBridgeAcceptanceTask,
  startPtradeBridgeWatchAcceptance,
  getPtradeBridgeWatchAcceptanceTask,
  startPtradeBridgeLiveSubmitTest,
  getPtradeBridgeLiveSubmitTestTask,
  submitGen2PaperOrder
} from '@/api/trading'
import { getStockDetail, getStockHistory } from '@/api/stock'
import { searchStocks } from '@/api/stock'

const loading = ref(false)
const refreshing = ref(false)
const available = ref(false)
const message = ref('')
const targetDate = ref('')
const payload = ref({})
const route = useRoute()
const router = useRouter()
const MANUAL_HOLDINGS_KEY = 'v4_manual_holdings'
const V4_CAPITAL_PROFILE_KEY = 'v4_capital_profile'
const manualHoldings = ref([])
const capitalForm = ref({
  base_capital: 0,
  realized_pnl: 0,
  synced_total_capital: null,
  synced_available_cash: null,
  synced_market_value: null,
  synced_cost_value: null,
  synced_at: ''
})
const signalLoading = ref(false)
const allRefreshLoading = ref(false)
const holdingRefreshLoading = ref(false)
const thsCapitalSyncLoading = ref(false)
const marketGateLoading = ref(false)
const marketGate = ref(null)
const buyPoolLoading = ref(false)
const buyPool = ref(null)
const gen2ShadowLoading = ref(false)
const gen2Shadow = ref(null)
const gen2ShadowUpdateLoading = ref(false)
const gen2ShadowUpdateTask = ref(null)
const GEN2_MAIN_ALPHA191_GATE = 'g2_v2_complete'
const gen2Alpha191Gate = ref(GEN2_MAIN_ALPHA191_GATE)
const gen2ShadowMonitorLoading = ref(false)
const gen2ShadowMonitorStatus = ref(null)
const workflowLoading = ref(false)
const workflowStatus = ref(null)
const ptradeBridgeLoading = ref(false)
const ptradeBridgeStatus = ref(null)
const ptradeBridgeAudit = ref(null)
const ptradeBridgeEvidence = ref(null)
const ptradeBridgeOrders = ref([])
const ptradeBridgeFills = ref([])
const ptradeBridgePositions = ref(null)
const ptradeBridgeProbeLoading = ref(false)
const ptradeBridgeProbeResult = ref(null)
const ptradeBridgeAcceptanceLoading = ref(false)
const ptradeBridgeAcceptanceResult = ref(null)
const ptradeBridgeWatchAcceptanceLoading = ref(false)
const ptradeBridgeWatchAcceptanceResult = ref(null)
const ptradeBridgeLiveSubmitTestLoading = ref(false)
const ptradeBridgeLiveSubmitTestResult = ref(null)
const paperOrderSubmittingCode = ref('')
const gen2VerificationDialogVisible = ref(false)
const gen2VerificationSaving = ref(false)
const gen2VerificationForm = ref({
  row: null,
  status: '',
  note: '',
  position_pct: null,
  fill_price: null
})
const addDialogVisible = ref(false)
const addMode = ref('new')
const addForm = ref({ code: '', name: '', shares: 0, cost_price: null, buy_time: '', source: '', signal_date: '' })
const ruleDialogVisible = ref(false)
const ruleChecklist = ref([])
const ruleCheckPassed = ref(false)
const pendingHoldingPayload = ref(null)
const codeLookupText = ref('')
const liveQuotePrice = ref(null)
const monitorEnabled = ref(false)
const monitorLoading = ref(false)
const monitorStatusText = ref('鏈紑鍚?)
const monitorTradingHoursOnly = ref(true)
const monitorQuietMinutes = ref(15)
const entryBacktestLoading = ref(false)
const entryBacktest = ref(null)
const sellDialogVisible = ref(false)
const sellForm = ref({ index: -1, code: '', name: '', max_shares: 0, shares: 0, price: null, time: '', reason: '' })
const tradeLogVisible = ref(false)
const MANUAL_TRADES_KEY = 'v4_manual_trade_logs'
const STOCK_DETAIL_TRADE_MARKS_PREFIX = 'stock_detail_trade_marks_'
const manualTradeLogs = ref([])
const historyPage = ref(1)
const historyPageSize = ref(20)
const FIXED_BASE_CAPITAL = 150000
const thsImportVisible = ref(false)
const thsImportLoading = ref(false)
const thsImportRawText = ref('')
const thsImportRows = ref([])
let codeLookupTimer = null
const STRONG_CHECK_MIN_WIN_RATE_5D = 55
const STRONG_CHECK_MIN_AVG_RETURN_5D = 0.8
const STRONG_CHECK_MIN_WIN_RATE_10D = 50
const STRONG_CHECK_MIN_AVG_RETURN_10D = 1.2
const TRAILING_TP_ACTIVATE_PNL = 5
const TRAILING_TP_DRAWDOWN = 4
const SINGLE_TRADE_LOSS_CAP_PCT_OF_TOTAL = 2
const AUTO_REFRESH_INTERVAL_MS = 15 * 1000
const AUTO_REFRESH_HOLDINGS_MIN_MS = 30 * 1000
const AUTO_REFRESH_PTRADE_BRIDGE_MIN_MS = 30 * 1000
const AUTO_REFRESH_MARKET_GATE_MIN_MS = 60 * 1000
const AUTO_REFRESH_GEN2_SHADOW_MIN_MS = 180 * 1000
const AUTO_REFRESH_WORKFLOW_MIN_MS = 180 * 1000
let autoRefreshTimer = null
const autoRefreshRunning = ref(false)
const lastAutoRefreshAt = ref('')
const lastHoldingAutoRefreshAt = ref(0)
const lastPtradeBridgeAutoRefreshAt = ref(0)
const lastMarketGateAutoRefreshAt = ref(0)
const lastGen2ShadowAutoRefreshAt = ref(0)
const lastWorkflowAutoRefreshAt = ref(0)

const isGen2LiveMode = computed(() => true)
const pageTitle = computed(() => 'G2 瀹炵洏浜ゆ槗椹鹃┒鑸?)
const pageSubtitle = computed(() => (
  isGen2LiveMode.value
    ? '绗竴灞忓彧灞曠ず姝ｅ紡涔扮偣銆佸紑浠撻棬绂併€佹寔浠撻鎺у拰浜ゆ槗鎵ц鐘舵€侊紱褰卞瓙鏍锋湰浠呯敤浜庡鐩橀獙璇併€?
    : '浠庝氦鏄撻€夎偂鍒版寔浠撶鐞嗛棴鐜紝骞剁粰鍑?15m/30m RSI 鑳岀鍗栧嚭鎻愮ず銆?
))
const unavailableTitle = computed(() => '绗簩浠ｇ瓥鐣ュ疄鐩樹氦鏄撴暟鎹笉鍙敤')
const buyPoolPanelTitle = computed(() => 'G2鍙傝€冧拱鍏ヨ瀵熸睜')
const gen2ShadowPanelTitle = computed(() => 'Alpha191 volume5 褰卞瓙璺熻釜/澶嶇洏姹?)

const selection = computed(() => payload.value?.selection || {})
const selectionBranchFreshness = computed(() => selection.value?.branch_freshness || {})
const selectionBranchFreshnessWarning = computed(() => {
  if (selectionBranchFreshness.value?.status !== 'stale') return ''
  return `${selectionBranchFreshness.value?.text || 'g2_v2_complete 姝ｅ紡灞曠ず鍒嗘敮婊炲悗'}锛涘綋鍓嶅疄鐩樻墽琛岀户缁寜 latest-driven 涓婚摼鍒ゆ柇锛屼笉浼氬洜涓烘棫灞曠ず鍒嗘敮鍋滃湪鍘嗗彶鑰屽仠鎽嗐€俙
})
const workflowStages = computed(() => {
  const stages = Array.isArray(workflowStatus.value?.pipeline_stages) ? workflowStatus.value.pipeline_stages : []
  return stages.map((stage) => ({
    ...stage,
    failed_count: Array.isArray(stage.failed) ? stage.failed.length : 0,
    check_count: Array.isArray(stage.checks) ? stage.checks.length : 0
  }))
})
const workflowFailedChecks = computed(() => {
  const blockers = Array.isArray(workflowStatus.value?.blockers) ? workflowStatus.value.blockers : []
  if (blockers.length) return blockers
  return workflowStages.value.flatMap((stage) => Array.isArray(stage.failed) ? stage.failed : [])
})
const workflowLineageRows = computed(() => (
  Array.isArray(workflowStatus.value?.lineage_items) ? workflowStatus.value.lineage_items : []
))
const workflowOk = computed(() => workflowStatus.value?.available !== false && workflowStages.value.length > 0 && workflowStages.value.every((stage) => !!stage.ok))
const workflowSummaryText = computed(() => {
  const data = workflowStatus.value || {}
  if (workflowLoading.value && !data.checked_at) return '姝ｅ湪妫€鏌ョ瓥鐣ユ祦姘寸嚎...'
  if (!data.available && data.message) return data.message
  const failed = workflowFailedChecks.value.length
  const checkedAt = data.checked_at ? `锛涙鏌?${data.checked_at}` : ''
  const prev = data.prev_trade_date ? `锛汥-1 ${data.prev_trade_date}` : ''
  const gate = data.alpha191_gate ? `锛沢ate ${data.alpha191_gate}` : ''
  return failed
    ? `${data.signal_date || selectedDate.value} 鏈?${failed} 涓樆濉炵偣${prev}${gate}${checkedAt}`
    : `${data.signal_date || selectedDate.value} 鏁版嵁婧愩€侀€夎偂銆佽瘎鍒嗐€佺瓥鐣ュ拰閫氱煡閾捐矾閫氳繃${prev}${gate}${checkedAt}`
})
const positions = computed(() => payload.value?.positions || {})
const fallback = computed(() => payload.value?.fallback || null)
const selectedDate = computed(() => payload.value?.selected_date || '--')
const requestedDate = computed(() => payload.value?.requested_date || '')
const liveDateResolution = computed(() => payload.value?.date_resolution || null)
const dateResolutionNotice = computed(() => {
  const requested = String(requestedDate.value || '').trim()
  const selected = String(selectedDate.value || '').trim()
  if (!requested || !selected || requested === selected) return ''
  const reason = String(liveDateResolution.value?.reason || '').trim()
  const extra = reason ? ' (' + reason + ')' : ''
  if (requested > selected) {
    return 'Request date ' + requested + ' not ready; fallback to ' + selected + extra
  }
  return 'Request date ' + requested + ' differs from displayed date ' + selected
})
const liveFallbackNotice = computed(() => {
  const title = String(fallback.value?.title || payload.value?.fallback_notice || '').trim()
  if (title) return title
  if (payload.value?.requested_date && payload.value?.selected_date && payload.value.requested_date !== payload.value.selected_date) {
    return 'Request date ' + payload.value.requested_date + ' is incomplete; displaying ' + payload.value.selected_date
  }
  return ''
})
const officialRebuild = computed(() => payload.value?.official_rebuild || workflowStatus.value?.official_rebuild || null)
const officialRebuildNotice = computed(() => {
  const rebuild = officialRebuild.value
  if (!rebuild || typeof rebuild !== 'object') return ''
  const status = String(rebuild.status || '').trim().toLowerCase()
  if (status === 'queued' || status === 'running') {
    const signalDate = String(rebuild.signal_date || selectedDate.value || '--')
    const phase = status === 'queued' ? 'queued' : 'running'
    return 'Official rebuild ' + phase + ' (signal_date=' + signalDate + ')'
  }
  if (status === 'completed' || status === 'failed') {
    if (rebuild.ok) return ''
    const signalDate = String(rebuild.signal_date || selectedDate.value || '--')
    const err = String(rebuild.error || '').trim()
    return 'Official rebuild failed (signal_date=' + signalDate + ')' + (err ? ': ' + err : '')
  }
  return ''
})
const latestTradeDate = computed(() => payload.value?.latest_trade_date || '--')
const buyPoolRows = computed(() => Array.isArray(buyPool.value?.rows) ? buyPool.value.rows : [])
const buyPoolMap = computed(() => {
  const map = new Map()
  for (const row of buyPoolRows.value) {
    const code = normalizeCode(row?.code)
    if (code) map.set(code, row)
  }
  return map
})
const buyPoolSummary = computed(() => {
  const data = buyPool.value || {}
  if (!data.available) return data.message || '涔板叆瑙傚療姹犲緟鐢熸垚'
  const stage = data.market_stage_label ? `锛屽ぇ鐩橀樁娈碉細${data.market_stage_label}` : ''
  return `${data.signal_date || '--'} 鍊欓€?${data.candidate_count || 0} 鍙紝杈惧埌V4鍏ュ満闃堝€?${data.primary_count || 0} 鍙?{stage}`
})
const workflowStageLabel = (stage) => ({
  data_source: '鏁版嵁婧?,
  selection: '閫夎偂鐢熸垚',
  selection_context: '鏉垮潡涓婁笅鏂?,
  scoring: '璇勫垎鍥犲瓙',
  strategy: '绛栫暐浜х墿',
  intraday_data: '鐩樹腑鏁版嵁',
  notification: '閫氱煡閾捐矾'
}[stage] || stage || '--')
const workflowStageMeta = (stage) => {
  const total = Number(stage?.check_count || stage?.checks?.length || 0)
  const failed = Number(stage?.failed_count || stage?.failed?.length || 0)
  return stage?.ok ? `${total}椤规鏌ラ€氳繃` : `${failed}/${total}椤规湭閫氳繃`
}
const workflowCheckResult = (row) => {
  if (row?.row_count !== undefined) return `rows ${row.row_count}`
  if (row?.code_count !== undefined) return `codes ${row.code_count}`
  if (row?.size !== undefined) return `${row.size} bytes`
  if (row?.max_datetime) return row.max_datetime
  return row?.ok ? 'ok' : 'blocked'
}
const workflowLineageResult = (row) => {
  const status = row?.status || {}
  return status.target_rows !== undefined && status.target_rows !== null ? `rows ${status.target_rows}` : (row?.ok ? 'ok' : 'pending')
}
const gen2ShadowRows = computed(() => Array.isArray(gen2Shadow.value?.rows) ? gen2Shadow.value.rows : [])
const actionableBuyRows = computed(() => gen2ShadowRows.value.filter((row) => gen2ShadowCanAddHolding(row)))
const shadowReviewRows = computed(() => gen2ShadowRows.value.filter((row) => !gen2ShadowCanAddHolding(row)))
const ptradeActiveOrders = computed(() => {
  const rows = Array.isArray(ptradeBridgeOrders.value) ? ptradeBridgeOrders.value : []
  return rows.filter((item) => ['pending', 'processing', 'ack', 'dry_run', 'waiting_approval', 'submitted', 'cancel_requested'].includes(String(item?._bridge_status || '').toLowerCase()))
})
const ptradeBridgeSummary = computed(() => {
  const status = ptradeBridgeStatus.value || {}
  if (!status.ok) return '妯℃嫙鐩樻ˉ鎺ョ姸鎬佹湭璇诲彇銆?
  const lastAck = status.last_ack_file ? '锛涘凡鏈夊洖鎵? : ''
  const lastFill = status.last_fill_file ? '锛涘凡鏈夋垚浜ゅ洖鍐? : ''
  const processing = Number(status.processing_count || 0)
  const stale = Number(status.processing_stale_count || 0)
  const heartbeatAge = Number(status.ptrade_heartbeat_age_seconds)
  const processingText = processing ? `锛宲rocessing ${processing}` : ''
  const staleText = stale ? `锛岄檲鏃?processing ${stale}` : ''
  const heartbeatText = Number.isFinite(heartbeatAge) ? `锛孭Trade蹇冭烦 ${Math.round(heartbeatAge)}s` : '锛孭Trade蹇冭烦鏈'
  return `妯℃嫙鐩樻ˉ鎺ワ細pending ${status.pending_count || 0}${processingText}${staleText}${heartbeatText}锛屾椿鍔ㄨ鍗?${ptradeActiveOrders.value.length}锛宒ry_run 榛樿 ${status.dry_run_default ? '寮€' : '鍏?}锛屽鎵归粯璁?${status.require_approval_default ? '寮€' : '鍏?}${lastAck}${lastFill}`
})
const ptradeBridgeQueueText = computed(() => {
  const status = ptradeBridgeStatus.value || {}
  const queue = status.ptrade_strategy_queue || {}
  if (!status.ok) return 'PTrade 闃熷垪閬ユ祴鏈鍙栥€?
  if (!queue || !Object.keys(queue).length) return 'PTrade 绛栫暐绔槦鍒楅仴娴嬫湭瑙侊紱AiStock 浠嶅彧鍐欐湰鍦?pending锛屼笉绛夊緟 ack銆?
  const pending = Number(queue.pending_count)
  const processing = Number(queue.processing_count)
  const cancelRequests = Number(queue.cancel_request_count)
  const oldestPending = Number(queue.oldest_pending_age_seconds)
  const oldestProcessing = Number(queue.oldest_processing_age_seconds)
  const processed = Number(queue.total_order_processed)
  const errors = Number(queue.total_bridge_errors)
  const pendingText = Number.isFinite(pending) ? `pending ${pending}` : 'pending --'
  const processingText = Number.isFinite(processing) ? `processing ${processing}` : 'processing --'
  const cancelText = Number.isFinite(cancelRequests) ? `cancel ${cancelRequests}` : 'cancel --'
  const pendingAgeText = Number.isFinite(oldestPending) ? `鏈€鑰乸ending ${Math.round(oldestPending)}s` : '鏈€鑰乸ending --'
  const processingAgeText = Number.isFinite(oldestProcessing) ? `鏈€鑰乸rocessing ${Math.round(oldestProcessing)}s` : '鏈€鑰乸rocessing --'
  const processedText = Number.isFinite(processed) ? `绱娑堣垂 ${processed}` : '绱娑堣垂 --'
  const errorText = Number.isFinite(errors) ? `閿欒 ${errors}` : '閿欒 --'
  const lastError = queue.last_bridge_error ? `锛涙渶杩戦敊璇細${queue.last_bridge_error}` : ''
  return `PTrade 绛栫暐绔槦鍒楋細${pendingText}锛?{processingText}锛?{cancelText}锛?{pendingAgeText}锛?{processingAgeText}锛?{processedText}锛?{errorText}${lastError}`
})
const ptradeBridgeReadinessText = computed(() => {
  const gates = ptradeBridgeAudit.value?.gates || {}
  if (Object.keys(gates).length) {
    const dryRun = gates.dry_run_probe_ready ? 'dry-run probe ready' : 'dry-run probe not ready'
    const live = gates.live_submit_ready ? 'live-submit test ready' : 'live-submit test not ready'
    const liveEnabledCheck = Array.isArray(ptradeBridgeAudit.value?.checks)
      ? ptradeBridgeAudit.value.checks.find((item) => item?.name === 'ptrade_live_order_enabled')
      : null
    const liveEnabled = liveEnabledCheck ? (liveEnabledCheck.ok ? 'PTrade live enabled' : 'PTrade live disabled') : ''
    const next = Array.isArray(ptradeBridgeAudit.value?.next_actions) ? ptradeBridgeAudit.value.next_actions[0] : ''
    return `PTrade readiness audit: ${dryRun}; ${live}${liveEnabled ? `; ${liveEnabled}` : ''}${next ? `; next: ${next}` : ''}`
  }
  const readiness = ptradeBridgeStatus.value?.readiness || {}
  const live = readiness.ready_for_live_order ? 'ready for approved live order' : 'not ready for approved live order'
  const heartbeat = readiness.ptrade_heartbeat_recent ? 'heartbeat ok' : 'heartbeat missing/stale'
  const probe = readiness.dry_run_probe_ack_recent ? 'dry-run ack ok' : 'dry-run ack missing/stale'
  const liveEnabled = readiness.ptrade_live_order_enabled ? 'PTrade live enabled' : 'PTrade live disabled'
  return `PTrade readiness: ${live}; ${heartbeat}; ${probe}; ${liveEnabled}`
})
const ptradeBridgeEvidenceSummary = computed(() => {
  const evidence = ptradeBridgeEvidence.value
  if (!evidence) return ''
  const checks = Array.isArray(evidence.checks) ? evidence.checks : []
  const failed = checks.filter((item) => !item?.ok)
  const generatedAt = evidence.generated_at || '--'
  const blocking = evidence.blocking || {}
  const reason = blocking.reason || evidence.blocking_reason || ''
  const stage = blocking.stage || evidence.blocking_stage || ''
  const blockingText = reason ? `锛涢樆濉烇細${stage ? `${stage}锛宍 : ''}${reason}` : ''
  return `PTrade evidence: ${evidence.ok ? 'ready' : 'not ready'}; failed ${failed.length}; generated ${generatedAt}${blockingText}`
})
const ptradeBridgeEvidenceRows = computed(() => {
  const checks = ptradeBridgeEvidence.value?.checks
  return Array.isArray(checks) ? checks : []
})
const ptradeBridgeProbeSummary = computed(() => {
  const result = ptradeBridgeProbeResult.value
  if (!result) return ''
  const checks = Array.isArray(result.checks) ? result.checks : []
  const failed = checks.filter((item) => item?.required && !item?.ok)
  const heartbeat = Number(result.final_status?.ptrade_heartbeat_age_seconds)
  const heartbeatText = Number.isFinite(heartbeat) ? `PTrade蹇冭烦 ${Math.round(heartbeat)}s` : 'PTrade蹇冭烦鏈'
  const ackStatus = result.ack_result?.ack?.status
  const ackText = result.submit_dry_run ? `锛沝ry-run ack ${ackStatus || '鏈敹鍒?}` : ''
  return `${result.ok ? '鎺㈤拡閫氳繃' : '鎺㈤拡鏈€氳繃'}锛?{heartbeatText}${ackText}锛涘繀闇€妫€鏌ュけ璐?${failed.length}`
})
const ptradeBridgeAcceptanceSummary = computed(() => {
  const result = ptradeBridgeAcceptanceResult.value
  if (!result) return ''
  const checks = Array.isArray(result.checks) ? result.checks : []
  const failed = checks.filter((item) => item?.required && !item?.ok)
  const heartbeatAge = Number(result.heartbeat?.heartbeat_age_seconds)
  const heartbeatText = Number.isFinite(heartbeatAge) ? `heartbeat ${Math.round(heartbeatAge)}s` : 'heartbeat 鏈'
  const ackStatus = result.dry_run_probe?.ack_result?.ack?.status
  const liveReady = result.readiness_audit?.gates?.live_submit_ready ? 'live-submit ready' : 'live-submit not ready'
  return `PTrade 楠屾敹${result.ok ? '閫氳繃' : '鏈€氳繃'}锛?{heartbeatText}锛沝ry-run ack ${ackStatus || '鏈敹鍒?}锛?{liveReady}锛涘け璐?${failed.length}`
})
const ptradeBridgeWatchAcceptanceSummary = computed(() => {
  const result = ptradeBridgeWatchAcceptanceResult.value
  if (!result) return ''
  const acceptance = result.acceptance || {}
  const checks = Array.isArray(acceptance.checks) ? acceptance.checks : []
  const failed = checks.filter((item) => item?.required && !item?.ok)
  const heartbeat = result.heartbeat || acceptance.heartbeat || null
  const heartbeatAge = Number(heartbeat?.heartbeat_age_seconds)
  const heartbeatText = Number.isFinite(heartbeatAge) ? `heartbeat ${Math.round(heartbeatAge)}s` : 'heartbeat 鏈'
  const ackStatus = acceptance.dry_run_probe?.ack_result?.ack?.status
  return `PTrade 绛夊緟楠屾敹${result.ok ? '閫氳繃' : '鏈€氳繃'}锛?{heartbeatText}锛沝ry-run ack ${ackStatus || '鏈敹鍒?}锛涘け璐?${failed.length}`
})
const ptradeBridgeLiveSubmitTestSummary = computed(() => {
  const result = ptradeBridgeLiveSubmitTestResult.value
  if (!result) return ''
  const checks = Array.isArray(result.checks) ? result.checks : []
  const failed = checks.filter((item) => item?.required && !item?.ok)
  const submitElapsed = Number(result.submit_result?.submit_elapsed_seconds)
  const submitText = Number.isFinite(submitElapsed) ? `submit ${submitElapsed.toFixed(3)}s` : 'submit not written'
  const ackStatus = result.ack_result?.ack?.status || 'ack missing'
  return `PTrade live-submit test ${result.ok ? 'passed' : 'blocked'}; ${submitText}; ${ackStatus}; failed checks ${failed.length}`
})
const ptradeLatestPositionRows = computed(() => {
  const rows = ptradeBridgePositions.value?.latest?.positions
  return Array.isArray(rows) ? rows : []
})
const sellPriorityRows = computed(() => {
  return currentHoldingRows.value
    .map((row) => {
      const flags = []
      let priority = 9
      const pnl = Number(row?.pnl_ratio)
      if (Number.isFinite(pnl) && pnl <= -6) {
        flags.push('纭鎹熻揪鍒?-6%')
        priority = Math.min(priority, 1)
      } else if (Number.isFinite(pnl) && pnl <= -4) {
        flags.push('浜忔崯杈惧埌 -4%锛岀姝㈠姞浠撳苟浼樺厛瑙傚療')
        priority = Math.min(priority, 2)
      }
      if (isSingleTradeLossCapBreached(row)) {
        flags.push(`缁勫悎鎷栫疮瓒呰繃 ${SINGLE_TRADE_LOSS_CAP_PCT_OF_TOTAL}%`)
        priority = Math.min(priority, 1)
      }
      if (isTrailingTakeProfitTriggered(row)) {
        flags.push('瑙﹀彂绉诲姩姝㈢泩鍥炴挙')
        priority = Math.min(priority, 2)
      }
      if (row?.score_pool_status === 'out_of_pool' || row?.in_score_pool === false) {
        flags.push('宸叉帀鍑鸿瘎鍒嗘睜')
        priority = Math.min(priority, 3)
      }
      if (row?.signal?.rsi15_signal?.detected || row?.signal?.rsi30_signal?.detected) {
        flags.push('15m/30m 鍗栫偣瑙﹀彂')
        priority = Math.min(priority, 2)
      }
      if (row?.signal?.suggestion) {
        flags.push(row.signal.suggestion)
        priority = Math.min(priority, 4)
      }
      const holdingIndex = manualHoldings.value.findIndex((item) => normalizeCode(item?.code) === normalizeCode(row?.code))
      return {
        ...row,
        holding_index: holdingIndex >= 0 ? holdingIndex : 0,
        risk_priority: priority,
        risk_text: flags.join('锛?)
      }
    })
    .filter((row) => row.risk_text)
    .sort((a, b) => a.risk_priority - b.risk_priority || Number(a.pnl_ratio || 0) - Number(b.pnl_ratio || 0))
})
const gen2VerificationQueueRows = computed(() => Array.isArray(gen2Shadow.value?.verification_queue) ? gen2Shadow.value.verification_queue : [])
const gen2VerificationOptions = computed(() => {
  const options = gen2Shadow.value?.verification_options
  if (Array.isArray(options) && options.length) return options
  return [
    { value: '', label: '鏈爣璁?, type: 'info' },
    { value: 'watch', label: '瑙傚療', type: 'warning' },
    { value: 'paper', label: '妯℃嫙璺熻釜', type: 'primary' },
    { value: 'small_buy', label: '灏忎粨涔板叆', type: 'success' },
    { value: 'skip', label: '鏀惧純', type: 'info' },
    { value: 'reject', label: '搴旇繃婊?, type: 'danger' }
  ]
})
const gen2ShadowSummary = computed(() => {
  const data = gen2Shadow.value || {}
  if (!data.available) return data.message || 'G2 V3 User V2褰卞瓙瑙傚療寰呯敓鎴?
  const counts = data.counts || {}
  const observable = counts.observable || 0
  const suspended = (counts.suspended_by_two_stop_cd3 || 0) + (counts.suspended_by_stop_cd5 || 0)
  const executed = counts.executed || 0
  return `${data.signal_date || '--'} G2褰卞瓙 ${data.row_count || gen2ShadowRows.value.length} 鏉★紝鍙瀵?${observable}锛岀啍鏂殏鍋?${suspended}锛屽凡鎵ц褰卞瓙 ${executed}`
})
const gen2VerificationSummaryText = computed(() => {
  const summary = gen2Shadow.value?.verification_global_summary || gen2Shadow.value?.verification_summary || {}
  const marked = Number(summary.marked || 0)
  const total = Number(summary.total || gen2ShadowRows.value.length || 0)
  const actionCount = Number(summary.action_count || 0)
  const rejectedCount = Number(summary.rejected_count || 0)
  const stopCount = Number(summary.stop5_touch_count || 0)
  const avg5 = Number(summary.avg_marked_fwd5_pct)
  const avg10 = Number(summary.avg_marked_fwd10_pct)
  const avg20 = Number(summary.avg_marked_fwd20_pct)
  const avgText = Number.isFinite(avg5)
    ? `宸叉爣璁板潎鍊?5/10/20鏃?${avg5.toFixed(2)}% / ${Number.isFinite(avg10) ? avg10.toFixed(2) : '--'}% / ${Number.isFinite(avg20) ? avg20.toFixed(2) : '--'}%`
    : '宸叉爣璁版牱鏈殏鏃犲畬鏁磋窡韪敹鐩?
  return `鍏ㄥ眬楠岃瘉鍙拌处锛氬凡鏍囪 ${marked}/${total}锛岃瀵?妯℃嫙/灏忎粨 ${actionCount}锛屾斁寮?搴旇繃婊?${rejectedCount}锛?0m姝㈡崯 ${stopCount}锛?{avgText}`
})
const gen2VerificationPromotionSource = computed(() => gen2Shadow.value?.verification_global_summary || gen2Shadow.value?.verification_summary || {})
const gen2VerificationPromotionType = computed(() => gen2VerificationPromotionSource.value?.promotion_type || 'info')
const gen2VerificationPromotionLabel = computed(() => gen2VerificationPromotionSource.value?.promotion_label || '鏍锋湰鏀堕泦涓?)
const gen2VerificationPromotionMessage = computed(() => gen2VerificationPromotionSource.value?.promotion_message || '鑷冲皯鍏堢疮璁?0鏉″凡鏍囪鏍锋湰锛屽啀鍒ゆ柇鏄惁杩涘叆灏忎粨瀹炵洏楠岃瘉銆?)
const gen2ShadowFreshnessNote = computed(() => {
  const d = gen2Shadow.value?.data_freshness || {}
  if (!d.latest_daily_date || !d.latest_shadow_date || d.latest_daily_date <= d.latest_shadow_date) return ''
  return `鏁版嵁鎻愮ず锛歏4鏃ョ嚎宸插埌 ${d.latest_daily_date}锛屼絾G2鏈夋晥褰卞瓙鍙拌处鏈€鏂颁负 ${d.latest_shadow_date}锛涜嫢椤甸潰浠嶆樉绀烘棫淇″彿鏃ワ紝璇存槑褰撳墠瑙勫垯灏氭湭浜х敓鏂扮殑鏈夋晥G2淇″彿銆俙
})
const gen2ShadowUpdateText = computed(() => {
  const task = gen2ShadowUpdateTask.value || {}
  if (!task.status) return ''
  if (task.status === 'completed') {
    const result = task.result || {}
    return `褰卞瓙浜ゆ槗宸叉洿鏂帮細鍘熷鍊欓€?${result.raw_candidates ?? '--'}锛岃繃婊ゅ悗 ${result.filtered_signals ?? '--'}锛屽綋鏃ュ奖瀛?${result.shadow_rows_for_date ?? '--'}銆俙
  }
  if (task.status === 'failed') return `褰卞瓙浜ゆ槗鏇存柊澶辫触锛?{task.error || '鏈煡閿欒'}`
  return `褰卞瓙浜ゆ槗鏇存柊涓細${task.status} ${task.progress || 0}%`
})
const gen2ShadowMonitorText = computed(() => {
  const status = gen2ShadowMonitorStatus.value || {}
  const interval = Math.max(1, Math.round(Number(status.interval_seconds || 60) / 60))
  const state = status.enabled ? `鑷姩鎺㈡祴涓紝姣?{interval}鍒嗛挓` : '鑷姩鎺㈡祴鏈紑鍚?
  const gate = status.alpha191_gate ? `锛汚lpha191 ${status.alpha191_gate}` : ''
  const nextRun = status.next_run_time ? `锛涗笅娆?${status.next_run_time}` : ''
  const lastRun = status.last_run_at ? `锛涙渶杩?${status.last_run_at}` : ''
  const lastMail = status.last_email_sent_at ? `锛涙渶杩戦偖浠?${status.last_email_sent_at}` : ''
  const lastHeartbeat = status.last_heartbeat_sent_at ? `锛涙渶杩戝績璺?${status.last_heartbeat_sent_at}` : ''
  const err = status.last_error ? `锛涢敊璇?${status.last_error}` : ''
  return `${state}${gate}${nextRun}${lastRun}${lastMail}${lastHeartbeat}${err}`
})
const gen2ShadowMonitorResultText = computed(() => {
  const result = gen2ShadowMonitorStatus.value?.last_result || {}
  if (!Object.keys(result).length) return ''
  if (result.skipped) return `鏈€杩戞帰娴嬭烦杩囷細${result.reason || '--'}`
  if (result.blocked) return `鏈€杩戞帰娴嬮樆濉烇細${result.blocker_count ?? '--'} 涓樆濉炵偣${result.blocker_email_sent ? '锛屽凡鍙戦€侀樆濉炴彁閱? : ''}`
  const parts = [
    `淇″彿鏃?${result.signal_date || '--'}`,
    `鍘熷 ${result.raw_candidates ?? '--'}`,
    `杩囨护 ${result.filtered_signals ?? '--'}`,
    `褰卞瓙 ${result.shadow_rows_for_date ?? '--'}`,
    `鍙彁閱?${result.candidate_count ?? '--'}`,
    `鏂板 ${result.new_count ?? '--'}`
  ]
  if (result.email_sent) parts.push('宸插彂涔扮偣閭欢')
  else if (result.heartbeat_email_sent) parts.push('宸插彂鏃犱拱鐐瑰績璺冲洖鎵?)
  else parts.push('鏈疆鏃犳柊閭欢')
  return `鏈€杩戞帰娴嬶細${parts.join('锛?)}`
})
const marketGateSummary = computed(() => {
  const gate = marketGate.value || {}
  if (!gate.available) return gate.message || '涓婅瘉鎸囨暟鏁版嵁鏆備笉鍙敤'
  const close = Number(gate.close)
  const ma20 = Number(gate.ma20)
  const date = gate.trade_date || '--'
  return `${date} 鏀剁洏 ${Number.isFinite(close) ? close.toFixed(2) : '--'} / MA20 ${Number.isFinite(ma20) ? ma20.toFixed(2) : '--'}`
})
const autoRefreshStatusText = computed(() => {
  const running = autoRefreshRunning.value ? '鍒锋柊涓? : '寰呭懡'
  const last = lastAutoRefreshAt.value ? `锛涙渶杩戝埛鏂?${lastAutoRefreshAt.value}` : ''
  return `浜ゆ槗鏃舵鍒嗗眰鑷姩鍒锋柊锛氭ˉ鎺?鎸佷粨30绉掞紝澶х洏闂搁棬60绉掞紝G2褰卞瓙/宸ヤ綔娴?80绉掞紱褰撳墠${running}${last}`
})
const shouldRunAutoRefresh = (lastRef, minMs, nowMs) => {
  if (!lastRef.value) return true
  return nowMs - Number(lastRef.value || 0) >= minMs
}
const markAutoRefresh = (lastRef, nowMs) => {
  lastRef.value = nowMs
}
const parseTimeMs = (value) => {
  const txt = String(value || '').trim()
  if (!txt) return 0
  const ts = new Date(txt.replace(' ', 'T')).getTime()
  return Number.isFinite(ts) ? ts : 0
}
const isAshareTradingTime = (base = new Date()) => {
  const day = base.getDay()
  if (day === 0 || day === 6) return false
  const minutes = base.getHours() * 60 + base.getMinutes()
  const morningStart = 9 * 60 + 25
  const morningEnd = 11 * 60 + 35
  const afternoonStart = 12 * 60 + 55
  const afternoonEnd = 15 * 60 + 5
  return (minutes >= morningStart && minutes <= morningEnd) || (minutes >= afternoonStart && minutes <= afternoonEnd)
}
const recentTrades = computed(() => {
  return (manualTradeLogs.value || [])
    .map((x) => ({
      date: String(x?.time || '').slice(0, 10),
      time: String(x?.time || ''),
      side: String(x?.side || ''),
      code: displayCode(x?.code),
      name: String(x?.name || ''),
      price: x?.price,
      shares: x?.shares,
      reason: x?.reason || '瀹炵洏璁板綍'
    }))
    .sort((a, b) => parseTimeMs(b.time) - parseTimeMs(a.time))
})
const realizedPnlTotal = computed(() => {
  return (manualTradeLogs.value || []).reduce((sum, item) => {
    const value = Number(item?.realized_pnl)
    return sum + (Number.isFinite(value) ? value : 0)
  }, 0)
})
const currentHoldingRows = computed(() => {
  return [...(manualHoldings.value || [])].sort((a, b) => parseTimeMs(b?.buy_time) - parseTimeMs(a?.buy_time))
})
const historyTradeGroups = computed(() => {
  const groups = new Map()
  const currentHoldingMap = new Map((manualHoldings.value || []).map((item) => [normalizeCode(item?.code), item]))
  for (const trade of manualTradeLogs.value || []) {
    const code = normalizeCode(trade?.code)
    if (!code) continue
    if (!groups.has(code)) {
      groups.set(code, {
        code,
        name: String(trade?.name || currentHoldingMap.get(code)?.name || ''),
        trades: []
      })
    }
    const entry = groups.get(code)
    entry.trades.push({
      ...trade,
      code,
      name: String(trade?.name || entry.name || currentHoldingMap.get(code)?.name || '')
    })
    if (!entry.name && trade?.name) entry.name = String(trade.name)
  }
  return [...groups.values()]
    .map((group) => {
      const trades = [...group.trades].sort((a, b) => parseTimeMs(a?.time) - parseTimeMs(b?.time))
      const buyTrades = trades.filter((item) => String(item?.side || '') === '涔板叆')
      const sellTrades = trades.filter((item) => String(item?.side || '') === '鍗栧嚭')
      const totalBought = buyTrades.reduce((sum, item) => sum + Math.max(0, Math.trunc(Number(item?.shares || 0))), 0)
      const totalSold = sellTrades.reduce((sum, item) => sum + Math.max(0, Math.trunc(Number(item?.shares || 0))), 0)
      const currentHolding = currentHoldingMap.get(group.code)
      const hasCurrentHolding = currentHoldingMap.has(group.code)
      const currentSharesBase = hasCurrentHolding ? (currentHolding?.shares ?? 0) : 0
      const currentShares = Math.max(0, Math.trunc(Number(currentSharesBase)))
      const realizedPnl = trades.reduce((sum, item) => {
        const value = Number(item?.realized_pnl)
        return sum + (Number.isFinite(value) ? value : 0)
      }, 0)
      const latestBuyTrade = [...buyTrades].sort((a, b) => parseTimeMs(b?.time) - parseTimeMs(a?.time))[0]
      const firstBuyTrade = buyTrades[0]
      const latestTrade = [...trades].sort((a, b) => parseTimeMs(b?.time) - parseTimeMs(a?.time))[0]
      return {
        code: group.code,
        name: group.name || currentHolding?.name || '--',
        trades: trades.map((item) => ({ ...item, time: String(item?.time || '') })),
        latest_buy_time: String(latestBuyTrade?.time || latestTrade?.time || '--'),
        first_buy_time: String(firstBuyTrade?.time || latestTrade?.time || '--'),
        last_trade_time: String(latestTrade?.time || '--'),
        buy_count: buyTrades.length,
        sell_count: sellTrades.length,
        net_shares: totalBought - totalSold,
        current_shares: currentShares,
        is_active: hasCurrentHolding && currentShares > 0,
        realized_pnl: realizedPnl,
        summary: `涔板叆 ${buyTrades.length} 娆★紝鍗栧嚭 ${sellTrades.length} 娆★紱绱涔板叆 ${totalBought} 鑲★紝绱鍗栧嚭 ${totalSold} 鑲
      }
    })
    .sort((a, b) => {
      const buyDiff = parseTimeMs(b.latest_buy_time) - parseTimeMs(a.latest_buy_time)
      if (buyDiff !== 0) return buyDiff
      return parseTimeMs(b.last_trade_time) - parseTimeMs(a.last_trade_time)
    })
})
const pagedHistoryTradeGroups = computed(() => {
  const pageSize = Math.max(1, Number(historyPageSize.value) || 20)
  const total = historyTradeGroups.value.length
  const maxPage = Math.max(1, Math.ceil(total / pageSize))
  const page = Math.min(Math.max(1, Number(historyPage.value) || 1), maxPage)
  const start = (page - 1) * pageSize
  return historyTradeGroups.value.slice(start, start + pageSize)
})
const focusCode = computed(() => String(route.query?.focus_code || '').trim())
const focusName = computed(() => String(route.query?.focus_name || '').trim())
const focusSource = computed(() => String(route.query?.source || '').trim())
const focusSignalDate = computed(() => String(route.query?.signal_date || '').trim())
const focusDescription = computed(() => {
  const sourceText = focusSource.value ? `鏉ユ簮锛?{focusSource.value}` : '鏉ユ簮锛氭墜宸ュ叆鍙?
  const signalText = focusSignalDate.value ? `锛涗俊鍙锋棩锛?{focusSignalDate.value}` : ''
  return `${sourceText}${signalText}銆傝鍏ュ彛鐢ㄤ簬鎶婁氦鏄撻€夎偂涓殑鐩爣鑲＄洿鎺ュ姞鍏ュ疄鐩樹氦鏄撴寔浠撱€俙
})
const currentPriceMap = computed(() => {
  const m = new Map()
  for (const row of positions.value?.rows || []) {
    m.set(normalizeCode(row?.code), row)
  }
  return m
})
const disciplineText = computed(() => {
  const lines = []
  if (manualHoldings.value.length >= 3) lines.push('绾緥1锛氭寔浠撳凡杈句笂闄?3 鍙紝绂佹鏂板')
  const hardRisk = manualHoldings.value.filter((x) => Number(x.pnl_ratio) <= -6).map((x) => x.code)
  if (hardRisk.length) lines.push(`绾緥4锛?{hardRisk.join('銆?)} 宸茶Е鍙?-6%锛屽簲浼樺厛澶勭悊`)
  const warnRisk = manualHoldings.value.filter((x) => Number(x.pnl_ratio) <= -4 && Number(x.pnl_ratio) > -6).map((x) => x.code)
  if (warnRisk.length) lines.push(`绾緥4锛?{warnRisk.join('銆?)} 宸插埌 -4%锛岀姝㈠姞浠揱)
  const trailingRisk = manualHoldings.value.filter((x) => isTrailingTakeProfitTriggered(x)).map((x) => x.code)
  if (trailingRisk.length) lines.push(`绾緥5锛?{trailingRisk.join('銆?)} 瑙﹀彂绉诲姩姝㈢泩鍥炴挙锛屽缓璁垎鎵规鐩坄)
  const lossCapRisk = manualHoldings.value.filter((x) => isSingleTradeLossCapBreached(x)).map((x) => x.code)
  if (lossCapRisk.length) lines.push(`绾緥6锛?{lossCapRisk.join('銆?)} 缁勫悎鎷栫疮瓒呰繃 ${SINGLE_TRADE_LOSS_CAP_PCT_OF_TOTAL}%`)
  const outPoolRisk = manualHoldings.value
    .filter((x) => x?.score_pool_status === 'out_of_pool' || x?.in_score_pool === false)
    .map((x) => x.code)
  if (outPoolRisk.length) lines.push(`绾緥7锛?{outPoolRisk.join('銆?)} 宸叉帀鍑鸿瘎鍒嗘睜锛屾寜鍗栧嚭瑙勫垯浼樺厛澶勭悊`)
  return lines.join('锛?)
})
const capitalSnapshot = computed(() => {
  let marketValue = 0
  let costValue = 0
  let dayPnl = 0
  for (const row of manualHoldings.value) {
    const shares = Number(row?.shares)
    if (!(Number.isFinite(shares) && shares > 0)) continue
    const cost = Number(row?.cost_price)
    if (Number.isFinite(cost) && cost > 0) {
      costValue += cost * shares
    }
    const price = Number(row?.current_price)
    if (Number.isFinite(price) && price > 0) {
      marketValue += price * shares
    } else if (Number.isFinite(cost) && cost > 0) {
      marketValue += cost * shares
    }
    const itemDayPnl = dayPnlAmount(row)
    if (Number.isFinite(itemDayPnl)) {
      dayPnl += itemDayPnl
    }
  }
  const baseCapital = FIXED_BASE_CAPITAL
  const tradeLogRealizedPnl = Number(realizedPnlTotal.value) || 0
  const syncedAvailableCash = Number(capitalForm.value.synced_available_cash)
  const syncedTotalCapital = Number(capitalForm.value.synced_total_capital)
  const pnlAmount = marketValue - costValue
  const pnlRatio = costValue > 0 ? (pnlAmount / costValue) * 100 : 0
  const hasSyncedTotal = Number.isFinite(syncedTotalCapital) && syncedTotalCapital > 0
  const hasSyncedCash = Number.isFinite(syncedAvailableCash) && syncedAvailableCash >= 0
  const computedTotalCapital = hasSyncedCash
    ? marketValue + syncedAvailableCash
    : baseCapital + tradeLogRealizedPnl + pnlAmount
  const totalCapital = hasSyncedCash ? computedTotalCapital : (hasSyncedTotal ? syncedTotalCapital : computedTotalCapital)
  const availableCash = hasSyncedCash ? syncedAvailableCash : (totalCapital - marketValue)
  const realizedPnl = (hasSyncedTotal || hasSyncedCash)
    ? totalCapital - baseCapital + pnlAmount
    : tradeLogRealizedPnl
  const positionRatio = totalCapital > 0 ? (marketValue / totalCapital) * 100 : 0
  return {
    total_capital: Number.isFinite(totalCapital) ? totalCapital : 0,
    available_cash: Number.isFinite(availableCash) ? availableCash : 0,
    market_value: marketValue,
    cost_value: costValue,
    realized_pnl: Number.isFinite(realizedPnl) ? realizedPnl : 0,
    pnl_amount: pnlAmount,
    pnl_ratio: pnlRatio,
    day_pnl: dayPnl,
    position_ratio: positionRatio
  }
})
const thsImportCheckedCount = computed(() => thsImportRows.value.filter((item) => !!item.checked).length)

const focusRows = computed(() => {
  if (!focusCode.value) return []
  const pools = [
    ...(buyPoolRows.value || []),
    ...(selection.value?.eligible_rows || []),
    ...(selection.value?.v4_reference?.top30 || []),
    ...(selection.value?.top30 || [])
  ]
  const hit = pools.find((item) => normalizeCode(item?.code) === normalizeCode(focusCode.value))
  if (hit) return [hit]
  return [{ code: focusCode.value, name: focusName.value || '--', rank: '--', score_total: '--', reason_text: '褰撳墠蹇収鏈懡涓鑲★紝鍙洿鎺ュ姞鍏ユ寔浠撱€? }]
})

const v4SourceMap = computed(() => {
  const map = new Map()
  const pools = [
    ...(buyPoolRows.value || []),
    ...(selection.value?.eligible_rows || []),
    ...(selection.value?.v4_reference?.top30 || []),
    ...(selection.value?.top30 || [])
  ]
  for (const item of pools) {
    const code = normalizeCode(item?.code)
    if (!code) continue
    if (!map.has(code)) {
      map.set(code, item)
    }
  }
  return map
})

const v4SourceFor = (row) => {
  const directRank = Number(row?.v4_rank)
  const directScore = Number(row?.v4_score)
  const directInPool =
    row?.score_pool_status === 'in_pool'
      ? true
      : (row?.score_pool_status === 'out_of_pool' ? false : (typeof row?.in_score_pool === 'boolean' ? row.in_score_pool : null))
  if ((Number.isFinite(directRank) && directRank > 0) || Number.isFinite(directScore)) {
    return {
      rank_text: Number.isFinite(directRank) && directRank > 0 ? String(directRank) : '--',
      score_text: Number.isFinite(directScore) ? directScore.toFixed(4) : '--',
      in_pool: directInPool !== null ? directInPool : (Number.isFinite(directRank) && directRank > 0)
    }
  }
  const code = normalizeCode(row?.code)
  const source = v4SourceMap.value.get(code)
  if (!source) {
    return { rank_text: '--', score_text: '--', in_pool: directInPool === true }
  }
  const rankVal = source?.score_rank ?? source?.rank ?? source?.rank_change
  const scoreVal = source?.score_total ?? source?.score
  const rankText = rankVal === null || rankVal === undefined || rankVal === '' ? '--' : String(rankVal)
  const scoreNum = Number(scoreVal)
  const scoreText = Number.isFinite(scoreNum) ? scoreNum.toFixed(4) : '--'
  const inPoolBySource = rankVal !== null && rankVal !== undefined && rankVal !== ''
  return { rank_text: rankText, score_text: scoreText, in_pool: directInPool !== null ? directInPool : inPoolBySource }
}

const normalizeCode = (value) => String(value || '').replace(/\D/g, '').slice(0, 6)
const toExchangeCode = (value) => {
  const code = normalizeCode(value)
  if (!code) return ''
  if (code.startsWith('6')) return code + '.SH'
  if (code.startsWith('4') || code.startsWith('8')) return code + '.BJ'
  return code + '.SZ'
}

const THS_FILTER_KEYWORDS = ['鎵撴柊', '鏂拌偂', '閰嶅彿', '瓒呴厤', '绾㈠埄', '鑲℃伅', '娓呯畻', '鍒╃◣', '鍒╂伅', '閰嶅敭']

const normalizeTradeTime = (dateText, timeText) => {
  const ds = String(dateText || '').replace(/\D/g, '')
  if (ds.length !== 8) return ''
  const day = `${ds.slice(0, 4)}-${ds.slice(4, 6)}-${ds.slice(6, 8)}`
  const rawTime = String(timeText || '').trim()
  if (!rawTime) return `${day} 00:00:00`
  const parts = rawTime.split(':').map((x) => x.padStart(2, '0'))
  if (parts.length === 2) return `${day} ${parts[0]}:${parts[1]}:00`
  if (parts.length >= 3) return `${day} ${parts[0]}:${parts[1]}:${parts[2]}`
  return `${day} 00:00:00`
}

const parseThsNumber = (value) => {
  const text = String(value ?? '').replace(/,/g, '').trim()
  if (!text) return null
  const num = Number(text)
  return Number.isFinite(num) ? num : null
}

const parseThsTradeSide = (action) => {
  const text = String(action || '').trim()
  if (text === '璇佸埜涔板叆' || text.includes('涔板叆')) return '涔板叆'
  if (text === '璇佸埜鍗栧嚭' || text.includes('鍗栧嚭')) return '鍗栧嚭'
  return ''
}

const tradeSignature = (item) => {
  const time = String(item?.time || '').trim()
  const side = String(item?.side || '').trim()
  const code = normalizeCode(item?.code)
  const shares = Math.abs(Math.trunc(Number(item?.shares || 0)))
  const price = Number(item?.price)
  return [time, side, code, shares, Number.isFinite(price) ? price.toFixed(3) : ''].join('|')
}

const openThsImportDialog = () => {
  thsImportVisible.value = true
}

const readThsTradesFromClipboard = async () => {
  thsImportLoading.value = true
  try {
    const text = await navigator.clipboard.readText()
    thsImportRawText.value = text
    parseThsTradeText(text)
  } catch (error) {
    ElMessage.warning(error?.message || '璇诲彇鍓创鏉垮け璐ワ紝璇锋墜鍔ㄧ矘璐?)
  } finally {
    thsImportLoading.value = false
  }
}

const readThsTradesFromWindow = async () => {
  thsImportLoading.value = true
  try {
    const resp = await readV4ManualHoldingThsCurrentTable()
    const text = String(resp?.raw_text || '')
    thsImportRawText.value = text
    if (!resp?.ok) {
      ElMessage.warning(resp?.message || '璇诲彇鍚岃姳椤虹獥鍙ｅけ璐?)
      return
    }
    if (!resp?.recognized_recent_trade) {
      const viewDesc = String(resp?.view_type_desc || '鏈煡琛ㄦ牸')
      ElMessage.warning('褰撳墠璇诲彇鍒扮殑鏄? + viewDesc + '锛屼笉鏄€滃巻鍙叉垚浜も€?)
      return
    }
    parseThsTradeText(text)
  } catch (error) {
    ElMessage.warning(error?.message || '璇诲彇鍚岃姳椤虹獥鍙ｅけ璐?)
  } finally {
    thsImportLoading.value = false
  }
}

const readThsTradesFromDeliveryFile = async () => {
  thsImportLoading.value = true
  try {
    const resp = await readV4ManualHoldingThsDeliveryFile()
    const text = String(resp?.raw_text || '')
    thsImportRawText.value = text
    if (!resp?.ok) {
      ElMessage.warning(resp?.message || '璇诲彇浜ゅ壊鍗曟枃浠跺け璐?)
      return
    }
    parseThsTradeText(text)
    if (resp?.file_path) {
      const fileTimeText = resp?.file_mtime ? '锛堟洿鏂颁簬 ' + resp.file_mtime + '锛? : ''
      ElMessage.success('宸茶鍙栦氦鍓插崟鏂囦欢锛? + resp.file_path + fileTimeText)
    }
  } catch (error) {
    ElMessage.warning(error?.message || '璇诲彇浜ゅ壊鍗曟枃浠跺け璐?)
  } finally {
    thsImportLoading.value = false
  }
}

const parseThsTradeText = (rawText) => {
  const text = String(rawText || '')
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
  if (!lines.length) {
    thsImportRows.value = []
    ElMessage.warning('鏈鍙栧埌鍚岃姳椤烘垚浜ゆ枃鏈?)
    return
  }
  const rows = lines.map((line) => line.split('\t'))
  const headerIndex = rows.findIndex((cells) => cells.includes('鎴愪氦鏃ユ湡') && cells.includes('璇佸埜浠ｇ爜') && cells.includes('鎿嶄綔'))
  if (headerIndex < 0) {
    thsImportRows.value = []
    ElMessage.warning('鏈瘑鍒埌鍚岃姳椤烘垚浜よ〃澶达紝璇风‘璁ゅ鍒剁殑鏄€滃巻鍙叉垚浜も€濊〃鏍?)
    return
  }
  const header = rows[headerIndex]
  const idx = (name) => header.indexOf(name)
  const dateIdx = idx('鎴愪氦鏃ユ湡')
  const timeIdx = idx('鎴愪氦鏃堕棿')
  const codeIdx = idx('璇佸埜浠ｇ爜')
  const nameIdx = idx('璇佸埜鍚嶇О')
  const actionIdx = idx('鎿嶄綔')
  const sharesIdx = idx('鎴愪氦鏁伴噺')
  const priceIdx = idx('鎴愪氦鍧囦环')
  const amountIdx = idx('鎴愪氦閲戦')
  const remarkIdx = idx('澶囨敞')
  const parsed = []
  for (const cells of rows.slice(headerIndex + 1)) {
    const dateText = cells[dateIdx]
    const action = String(cells[actionIdx] || '').trim()
    const code = normalizeCode(cells[codeIdx])
    const name = String(cells[nameIdx] || '').trim()
    const remark = String(remarkIdx >= 0 ? cells[remarkIdx] || '' : '').trim()
    const sharesRaw = parseThsNumber(cells[sharesIdx])
    const priceRaw = parseThsNumber(cells[priceIdx])
    const amountRaw = parseThsNumber(cells[amountIdx])
    const timeText = timeIdx >= 0 ? cells[timeIdx] : ''
    const time = normalizeTradeTime(dateText, timeText)
    const side = parseThsTradeSide(action)
    const shares = Math.abs(Math.trunc(Number(sharesRaw || 0)))
    const price = Number(priceRaw)
    const textBlob = [action, name, remark].join(' ')
    const keywordHit = THS_FILTER_KEYWORDS.find((word) => textBlob.includes(word))
    if (!time || code.length !== 6 || !side) continue
    if (keywordHit) continue
    if (!(shares > 0 && Number.isFinite(price) && price > 0)) continue
    if (!/^(璇佸埜涔板叆|璇佸埜鍗栧嚭)$/.test(action)) continue
    parsed.push({
      checked: true,
      time,
      side,
      code,
      name,
      shares,
      price: Number(price.toFixed(3)),
      amount: Number.isFinite(amountRaw) ? Number(amountRaw.toFixed(3)) : null,
      remark,
      filter_reason: '鐪熷疄璇佸埜鎴愪氦'
    })
  }
  const dedup = []
  const seen = new Set()
  for (const item of parsed) {
    const sig = tradeSignature(item)
    if (seen.has(sig)) continue
    seen.add(sig)
    dedup.push(item)
  }
  dedup.sort((a, b) => new Date(b.time.replace(' ', 'T')).getTime() - new Date(a.time.replace(' ', 'T')).getTime())
  thsImportRows.value = dedup
  if (!dedup.length) {
    ElMessage.warning('宸茶鍙栧唴瀹癸紝浣嗚繃婊ゅ悗娌℃湁鍙鍏ョ殑鐪熷疄璇佸埜鎴愪氦')
    return
  }
  ElMessage.success('宸茶瘑鍒?' + dedup.length + ' 鏉＄湡瀹炶瘉鍒告垚浜わ紝璇峰嬀閫夌‘璁?)
}

const syncWatchlistHoldingByCode = async (code) => {
  const normalizedCode = normalizeCode(code)
  if (!normalizedCode) return
  const row = manualHoldings.value.find((item) => normalizeCode(item?.code) === normalizedCode)
  try {
    await request({
      url: '/watchlist/' + normalizedCode,
      method: 'post',
      params: { group: 'v4_live' },
      skipErrorHandler: true
    })
    await request({
      url: '/watchlist/' + normalizedCode,
      method: 'put',
      params: {
        is_holding: !!row,
        position_shares: Number(row?.shares || 0) || 0,
        position_cost: Number(row?.cost_price || 0) || 0
      },
      skipErrorHandler: true
    })
  } catch {}
}

const confirmImportThsTrades = async () => {
  const selected = thsImportRows.value.filter((item) => !!item.checked)
  if (!selected.length) {
    ElMessage.warning('璇疯嚦灏戝嬀閫変竴鏉℃垚浜よ褰?)
    return
  }
  const existingSignatures = new Set((manualTradeLogs.value || []).map((item) => tradeSignature(item)))
  const ordered = [...selected].sort((a, b) => new Date(a.time.replace(' ', 'T')).getTime() - new Date(b.time.replace(' ', 'T')).getTime())
  let imported = 0
  let duplicated = 0
  let orphanSell = 0
  const touchedCodes = new Set()
  for (const tx of ordered) {
    const sig = tradeSignature(tx)
    if (existingSignatures.has(sig)) {
      duplicated += 1
      continue
    }
    const code = normalizeCode(tx.code)
    const shares = Math.abs(Math.trunc(Number(tx.shares || 0)))
    const price = Number(tx.price)
    const time = String(tx.time || nowMinuteText())
    const existing = manualHoldings.value.find((item) => normalizeCode(item?.code) === code)
    if (tx.side === '' || tx.side === '涔板叆') {
      if (!existing) {
        manualHoldings.value.unshift({
          code,
          name: String(tx.name || ''),
          shares,
          cost_price: Number.isFinite(price) ? Number(price.toFixed(3)) : null,
          buy_count_week: 1,
          buy_week_key: weekKeyFromTime(time),
          buy_time: time.slice(0, 16),
          source: 'ths_import',
          signal_date: ''
        })
      } else {
        const oldShares = Math.max(0, Math.trunc(Number(existing?.shares || 0)))
        const oldCost = Number(existing?.cost_price)
        const nextShares = oldShares + shares
        existing.shares = nextShares
        existing.buy_time = time.slice(0, 16)
        existing.buy_week_key = weekKeyFromTime(time)
        existing.buy_count_week = Number(existing.buy_count_week || 0) + 1
        if (Number.isFinite(oldCost) && oldCost > 0 && Number.isFinite(price) && price > 0 && nextShares > 0) {
          existing.cost_price = Number((((oldCost * oldShares) + (price * shares)) / nextShares).toFixed(3))
        } else if (Number.isFinite(price) && price > 0) {
          existing.cost_price = Number(price.toFixed(3))
        }
        if (!existing.name && tx.name) existing.name = String(tx.name)
      }
      manualTradeLogs.value.unshift({
        time,
        side: '涔板叆',
        code,
        name: String(tx.name || existing?.name || ''),
        shares,
        price: Number.isFinite(price) ? Number(price.toFixed(3)) : null,
        before_shares: existing ? Math.max(0, Math.trunc(Number(existing.shares || 0))) - shares : 0,
        after_shares: existing ? Math.max(0, Math.trunc(Number(existing.shares || 0))) : shares,
        realized_pnl: null,
        reason: '鍚岃姳椤哄鍏?
      })
      imported += 1
      touchedCodes.add(code)
      existingSignatures.add(sig)
      continue
    }
    if (!existing) {
      manualTradeLogs.value.unshift({
        time,
        side: '鍗栧嚭',
        code,
        name: String(tx.name || ''),
        shares,
        price: Number.isFinite(price) ? Number(price.toFixed(3)) : null,
        before_shares: null,
        after_shares: null,
        realized_pnl: null,
        reason: '鍚岃姳椤哄鍏ワ紙鏈尮閰嶅埌鐜版寔浠擄級'
      })
      imported += 1
      orphanSell += 1
      existingSignatures.add(sig)
      continue
    }
    const beforeShares = Math.max(0, Math.trunc(Number(existing?.shares || 0)))
    const nextShares = Math.max(0, beforeShares - shares)
    const costPrice = Number(existing?.cost_price)
    let realizedPnl = null
    if (Number.isFinite(price) && Number.isFinite(costPrice)) {
      realizedPnl = (price - costPrice) * Math.min(beforeShares, shares)
    }
    if (nextShares <= 0) {
      manualHoldings.value = manualHoldings.value.filter((item) => normalizeCode(item?.code) !== code)
    } else {
      existing.shares = nextShares
    }
    manualTradeLogs.value.unshift({
      time,
      side: '鍗栧嚭',
      code,
      name: String(tx.name || existing?.name || ''),
      shares,
      price: Number.isFinite(price) ? Number(price.toFixed(3)) : null,
      before_shares: beforeShares,
      after_shares: nextShares,
      realized_pnl: realizedPnl,
      reason: '鍚岃姳椤哄鍏?
    })
    imported += 1
    touchedCodes.add(code)
    existingSignatures.add(sig)
  }
  saveManualTradeLogs()
  saveManualHoldings()
  for (const code of touchedCodes) {
    await syncWatchlistHoldingByCode(code)
  }
  thsImportVisible.value = false
  await refreshManualPnlByDetail()
  await refreshAllHoldingData()
  await refreshManualSignals()
  const duplicateText = duplicated ? '锛岃烦杩囬噸澶?' + duplicated + ' 鏉? : ''
  const orphanSellText = orphanSell ? '锛屽叾涓?' + orphanSell + ' 鏉″崠鍑轰粎璁板叆娴佹按' : ''
  ElMessage.success('宸插鍏?' + imported + ' 鏉℃垚浜? + duplicateText + orphanSellText)
}
const displayCode = (value) => {
  const raw = String(value || '').trim().toUpperCase()
  if (!raw) return '--'
  if (raw.includes('.')) return raw
  const code = normalizeCode(raw)
  if (!code) return raw
  if (code.startsWith('6')) return code + '.SH'
  if (code.startsWith('4') || code.startsWith('8')) return code + '.BJ'
  return code + '.SZ'
}
const copyPlainCode = async (value) => {
  const code = normalizeCode(value)
  if (!code) {
    ElMessage.warning('鑲＄エ浠ｇ爜鏃犳晥锛屾棤娉曞鍒?)
    return
  }
  try {
    if (navigator?.clipboard?.writeText) {
      await navigator.clipboard.writeText(code)
    } else {
      const input = document.createElement('textarea')
      input.value = code
      input.setAttribute('readonly', 'readonly')
      input.style.position = 'fixed'
      input.style.opacity = '0'
      document.body.appendChild(input)
      input.select()
      document.execCommand('copy')
      document.body.removeChild(input)
    }
      ElMessage.success('宸插鍒朵唬鐮侊細' + code)
  } catch (error) {
    ElMessage.warning('澶嶅埗澶辫触锛岃鎵嬪姩澶嶅埗浠ｇ爜')
  }
}
const weekKeyFromTime = (buyTime) => {
  const base = buyTime ? new Date(String(buyTime).replace(' ', 'T')) : new Date()
  const year = base.getFullYear()
  const daySeed = base.getMonth() * 31 + base.getDate()
  return year + '-' + Math.ceil(daySeed / 7)
}

const querySearchStock = async (queryString, cb) => {
  const keyword = String(queryString || '').trim()
  const localPools = [
    ...(buyPoolRows.value || []),
    ...(selection.value?.eligible_rows || []),
    ...(selection.value?.v4_reference?.top30 || []),
    ...(selection.value?.top30 || []),
    ...(positions.value?.rows || []),
    ...(recentTrades.value || []),
    ...(manualHoldings.value || [])
  ]
  const localMap = new Map()
  for (const item of localPools) {
    const code = normalizeCode(item?.code)
    const name = String(item?.name || '').trim()
    if (!code) continue
    localMap.set(code, {
      value: code + (name ? ' ' + name : ''),
      code,
      name
    })
  }
  let localRows = Array.from(localMap.values())
  if (keyword) {
    localRows = localRows.filter((x) => x.code.includes(keyword) || x.name.includes(keyword))
  }
  localRows = localRows.slice(0, 20)
  if (keyword.length < 2) {
    cb(localRows)
    return
  }
  try {
    const remote = await searchStocks(keyword)
    const remoteRows = Array.isArray(remote) ? remote : (Array.isArray(remote?.items) ? remote.items : [])
    for (const item of remoteRows) {
      const code = normalizeCode(item?.code)
      const name = String(item?.name || item?.stock_name || '').trim()
      if (!code) continue
      if (!localMap.has(code)) {
        localMap.set(code, {
          value: code + (name ? ' ' + name : ''),
          code,
          name
        })
      }
    }
    cb(Array.from(localMap.values()).filter((x) => x.code.includes(keyword) || x.name.includes(keyword)).slice(0, 20))
  } catch {
    cb(localRows)
  }
}

const handleCodeSelect = async (item) => {
  const code = normalizeCode(item?.code || item?.value || '')
  if (!code) return
  addForm.value.code = code
  if (item?.name) addForm.value.name = String(item.name)
  await resolveStockByCode(code)
}

const livePnlPct = computed(() => {
  const cost = Number(addForm.value.cost_price)
  const quote = Number(liveQuotePrice.value)
  if (!Number.isFinite(cost) || cost <= 0 || !Number.isFinite(quote) || quote <= 0) return null
  return ((quote / cost) - 1) * 100
})

const livePnlText = computed(() => {
  const quote = Number(liveQuotePrice.value)
  if (!Number.isFinite(quote) || quote <= 0) return '鐜颁环寰呰幏鍙?
  const pnl = livePnlPct.value
  if (pnl === null) return '鐜颁环 ' + quote.toFixed(3) + '锛岃濉啓鎴愭湰浠?
  return '鐜颁环 ' + quote.toFixed(3) + '锛岀泩浜?' + (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + '%'
})

const livePnlColor = computed(() => {
  const pnl = livePnlPct.value
  if (pnl === null) return '#66708b'
  if (pnl > 0) return '#d4380d'
  if (pnl < 0) return '#389e0d'
  return '#66708b'
})

const entryBacktestText = computed(() => {
  const data = entryBacktest.value
  if (!data || !data.ok) return ''
  const count = Number(data.sample_count || 0)
  if (!count) return '璇ヨ偂鏆傛棤鍘嗗彶鍏ュ満鏍锋湰'
  const d3 = data?.horizons?.d3 || {}
  const d5 = data?.horizons?.d5 || {}
  const d10 = data?.horizons?.d10 || {}
  const line = (label, item) => {
    const win = item?.win_rate
    const avg = item?.avg_return
    const n = Number(item?.sample_count || 0)
    const winText = Number.isFinite(Number(win)) ? Number(win).toFixed(2) + '%' : '--'
    const avgText = Number.isFinite(Number(avg)) ? Number(avg).toFixed(2) + '%' : '--'
    return label + ' 鎴愬姛鐜?' + winText + '锛屽钩鍧囨定骞?' + avgText + '锛堟牱鏈?' + n + '锛?
  }
  return '鏍锋湰鎬绘暟 ' + count + '锛? + line('3鏃?, d3) + '锛? + line('5鏃?, d5) + '锛? + line('10鏃?, d10)
})

const resolveStockByCode = async (code) => {
  const normalized = normalizeCode(code)
  if (normalized.length !== 6) {
    codeLookupText.value = ''
    liveQuotePrice.value = null
    return
  }
  const localPools = [
    ...(selection.value?.eligible_rows || []),
    ...(selection.value?.v4_reference?.top30 || []),
    ...(selection.value?.top30 || []),
    ...(positions.value?.rows || []),
    ...(recentTrades.value || []),
    ...(manualHoldings.value || [])
  ]
  const localHit = localPools.find((item) => normalizeCode(item?.code) === normalized && String(item?.name || '').trim())
  if (localHit?.name) {
    addForm.value.name = String(localHit.name)
    codeLookupText.value = `宸插尮閰嶏細${normalized} ${String(localHit.name)}`
  } else {
    codeLookupText.value = '姝ｅ湪鍖归厤浠ｇ爜...'
  }
  try {
    const detail = await getStockDetail(toExchangeCode(normalized), { skipErrorHandler: true })
    const name = String(detail?.name || detail?.stock_name || '').trim()
    if (name) {
      addForm.value.name = name
      codeLookupText.value = `宸插尮閰嶏細${normalized} ${name}`
      let detailPrice = Number(detail?.price ?? detail?.close ?? detail?.current_price ?? detail?.last_price)
      if (!(Number.isFinite(detailPrice) && detailPrice > 0)) {
        try {
          const history = await getStockHistory(toExchangeCode(normalized), { period: '1d', limit: 2 }, { skipErrorHandler: true })
          detailPrice = Number(extractLatestPriceFromHistory(history))
        } catch {}
      }
      if (Number.isFinite(detailPrice) && detailPrice > 0) liveQuotePrice.value = detailPrice
    } else if (!localHit?.name) {
      codeLookupText.value = '浠ｇ爜搴撴湭鍖归厤鍒拌鑲＄エ锛岃鎵嬪姩濉啓鍚嶇О'
    }
  } catch {
    if (!localHit?.name) codeLookupText.value = '浠ｇ爜搴撴湭鍖归厤鍒拌鑲＄エ锛岃鎵嬪姩濉啓鍚嶇О'
  }
  if (addMode.value === 'new') {
    await loadEntryBacktest(normalized)
  }
}

const loadEntryBacktest = async (code) => {
  const normalized = normalizeCode(code)
  if (normalized.length !== 6) {
    entryBacktest.value = null
    return
  }
  entryBacktestLoading.value = true
  try {
    const resp = await getV4ManualHoldingEntryBacktest({ code: normalized })
    entryBacktest.value = resp
  } catch {
    entryBacktest.value = { ok: false }
  } finally {
    entryBacktestLoading.value = false
  }
}

const loadManualHoldings = () => {
  try {
    const raw = localStorage.getItem(MANUAL_HOLDINGS_KEY)
    const parsed = raw ? JSON.parse(raw) : []
    manualHoldings.value = Array.isArray(parsed) ? parsed : []
  } catch {
    manualHoldings.value = []
  }
}

const loadManualTradeLogs = () => {
  try {
    const raw = localStorage.getItem(MANUAL_TRADES_KEY)
    const parsed = raw ? JSON.parse(raw) : []
    manualTradeLogs.value = Array.isArray(parsed) ? parsed : []
  } catch {
    manualTradeLogs.value = []
  }
}

const saveManualTradeLogs = () => {
  localStorage.setItem(MANUAL_TRADES_KEY, JSON.stringify(manualTradeLogs.value))
}

const clearTradeLogs = () => {
  manualTradeLogs.value = []
  saveManualTradeLogs()
  ElMessage.success('浜ゆ槗璁板綍宸叉竻绌?)
}

const saveManualHoldings = () => {
  localStorage.setItem(MANUAL_HOLDINGS_KEY, JSON.stringify(manualHoldings.value))
  syncManualHoldingsStateToServer()
}

const applyTrailingTakeProfitState = () => {
  let changed = false
  manualHoldings.value = manualHoldings.value.map((item) => {
    const next = { ...item }
    const pnl = Number(next?.pnl_ratio)
    const peak = Number(next?.peak_pnl_ratio)
    if (Number.isFinite(pnl)) {
      if (!Number.isFinite(peak) || pnl > peak) {
        next.peak_pnl_ratio = pnl
        changed = true
      }
    }
    return next
  })
  if (changed) saveManualHoldings()
}

const trailingDrawdownPct = (row) => {
  const pnl = Number(row?.pnl_ratio)
  const peak = Number(row?.peak_pnl_ratio)
  if (!Number.isFinite(pnl) || !Number.isFinite(peak)) return null
  return peak - pnl
}

const isTrailingTakeProfitTriggered = (row) => {
  const peak = Number(row?.peak_pnl_ratio)
  const dd = trailingDrawdownPct(row)
  if (!Number.isFinite(peak) || !Number.isFinite(dd)) return false
  return peak >= TRAILING_TP_ACTIVATE_PNL && dd >= TRAILING_TP_DRAWDOWN
}

const holdingMarketValue = (row) => {
  const shares = Number(row?.shares)
  const price = Number(row?.current_price)
  if (!(Number.isFinite(shares) && shares > 0 && Number.isFinite(price) && price > 0)) return null
  return shares * price
}

const floatingPnlAmount = (row) => {
  const shares = Number(row?.shares)
  const cost = Number(row?.cost_price)
  const price = Number(row?.current_price)
  if (!(Number.isFinite(shares) && shares > 0 && Number.isFinite(cost) && cost > 0 && Number.isFinite(price) && price > 0)) return null
  return (price - cost) * shares
}

const stopLossPrice = (row) => {
  const cost = Number(row?.cost_price)
  if (!(Number.isFinite(cost) && cost > 0)) return null
  return cost * 0.95
}

const dayPnlAmount = (row) => {
  const explicit = Number(row?.day_pnl)
  if (Number.isFinite(explicit)) return explicit
  const shares = Number(row?.shares)
  const price = Number(row?.current_price)
  const prevClose = Number(row?.prev_close)
  if (!(Number.isFinite(shares) && shares > 0 && Number.isFinite(price) && price > 0 && Number.isFinite(prevClose) && prevClose > 0)) return null
  return (price - prevClose) * shares
}

const dayChangePct = (row) => {
  const explicit = Number(row?.day_change_pct ?? row?.change_pct ?? row?.pct_chg)
  if (Number.isFinite(explicit)) return explicit
  const price = Number(row?.current_price)
  const prevClose = Number(row?.prev_close)
  if (!(Number.isFinite(price) && price > 0 && Number.isFinite(prevClose) && prevClose > 0)) return null
  return ((price - prevClose) / prevClose) * 100
}

const lossPctOfTotalCapital = (row) => {
  const loss = floatingPnlAmount(row)
  const total = Number(capitalSnapshot.value.total_capital)
  if (!(Number.isFinite(loss) && Number.isFinite(total) && total > 0)) return null
  return (loss / total) * 100
}

const isSingleTradeLossCapBreached = (row) => {
  const pct = lossPctOfTotalCapital(row)
  if (!Number.isFinite(pct)) return false
  return pct <= -Math.abs(SINGLE_TRADE_LOSS_CAP_PCT_OF_TOTAL)
}

const syncManualHoldingsStateToServer = async () => {
  try {
    await saveV4ManualHoldingState({ holdings: manualHoldings.value })
  } catch {}
}

const loadCapitalProfile = () => {
  try {
    const raw = localStorage.getItem(V4_CAPITAL_PROFILE_KEY)
    const parsed = raw ? JSON.parse(raw) : {}
    const legacyTotal = Number(parsed?.total_capital)
    capitalForm.value = {
      base_capital: FIXED_BASE_CAPITAL,
      realized_pnl: Number(parsed?.realized_pnl) || 0,
      synced_total_capital: parsed?.synced_total_capital ?? null,
      synced_available_cash: parsed?.synced_available_cash ?? null,
      synced_market_value: parsed?.synced_market_value ?? null,
      synced_cost_value: parsed?.synced_cost_value ?? null,
      synced_at: String(parsed?.synced_at || '')
    }
  } catch {
    capitalForm.value = {
      base_capital: FIXED_BASE_CAPITAL,
      realized_pnl: 0,
      synced_total_capital: null,
      synced_available_cash: null,
      synced_market_value: null,
      synced_cost_value: null,
      synced_at: ''
    }
  }
}

const saveCapitalProfile = () => {
  localStorage.setItem(V4_CAPITAL_PROFILE_KEY, JSON.stringify(capitalForm.value))
}

const mergeMarketStats = () => {
  manualHoldings.value = manualHoldings.value.map((item) => {
    const hit = currentPriceMap.value.get(normalizeCode(item.code))
    if (!hit) return item
    const pnl = Number(hit?.pnl_ratio)
    return { ...item, pnl_ratio: Number.isFinite(pnl) ? pnl : item.pnl_ratio, current_price: hit?.current_price ?? item.current_price }
  })
}

const extractLatestPriceFromDetail = (detail) => {
  const candidates = [
    detail?.price,
    detail?.close,
    detail?.latest_close,
    detail?.current_price,
    detail?.last_price,
    detail?.kline?.close
  ]
  for (const value of candidates) {
    const n = Number(value)
    if (Number.isFinite(n) && n > 0) return n
  }
  return null
}

const extractLatestPriceFromHistory = (historyResp) => {
  const rows = Array.isArray(historyResp)
    ? historyResp
    : (Array.isArray(historyResp?.items) ? historyResp.items : (Array.isArray(historyResp?.data) ? historyResp.data : []))
  if (!rows.length) return null
  const last = rows[rows.length - 1] || {}
  const candidates = [last?.close, last?.price, last?.last_price, last?.current_price]
  for (const value of candidates) {
    const n = Number(value)
    if (Number.isFinite(n) && n > 0) return n
  }
  return null
}

const refreshManualPnlByDetail = async () => {
  if (!manualHoldings.value.length) return
  try {
    const resp = await getV4ManualHoldingQuotes({ holdings: manualHoldings.value })
    const quoteMap = new Map((resp?.rows || []).map((x) => [normalizeCode(x?.code), x]))
    let changed = false
    const nextRows = manualHoldings.value.map((item) => {
      const code = normalizeCode(item?.code)
      const quote = quoteMap.get(code)
      if (!quote) return { ...item }
      const next = { ...item }
      const latest = Number(quote?.latest_price)
      const pnl = Number(quote?.pnl_ratio)
      if (Number.isFinite(latest) && latest > 0) {
        next.current_price = latest
      }
      const prevClose = Number(quote?.prev_close)
      if (Number.isFinite(prevClose) && prevClose > 0) {
        next.prev_close = prevClose
      }
      const dayPnl = Number(quote?.day_pnl)
      if (Number.isFinite(dayPnl)) {
        next.day_pnl = dayPnl
      }
      if (Number.isFinite(pnl)) {
        next.pnl_ratio = pnl
      }
      if (
        next.current_price !== item.current_price
        || next.prev_close !== item.prev_close
        || next.day_pnl !== item.day_pnl
        || next.pnl_ratio !== item.pnl_ratio
      ) {
        changed = true
      }
      return next
    })
    for (const item of nextRows) {
      const code = normalizeCode(item?.code)
      const latest = Number(item?.current_price)
      if (code.length !== 6 || (Number.isFinite(latest) && latest > 0)) continue
      try {
        const detail = await getStockDetail(toExchangeCode(code), { skipErrorHandler: true })
        let detailPrice = Number(extractLatestPriceFromDetail(detail))
        if (!(Number.isFinite(detailPrice) && detailPrice > 0)) {
          try {
            const history = await getStockHistory(toExchangeCode(code), { period: '1d', limit: 2 }, { skipErrorHandler: true })
            detailPrice = Number(extractLatestPriceFromHistory(history))
          } catch {}
        }
        if (Number.isFinite(detailPrice) && detailPrice > 0) {
          item.current_price = detailPrice
          const cost = Number(item?.cost_price)
          if (Number.isFinite(cost) && cost > 0) {
            item.pnl_ratio = ((detailPrice / cost) - 1) * 100
          }
          changed = true
        }
      } catch {}
    }
    manualHoldings.value = nextRows
    applyTrailingTakeProfitState()
    if (changed) {
      saveManualHoldings()
    }
  } catch {}
}

const repairExistingManualHoldings = async () => {
  if (!manualHoldings.value.length) return
  let changed = false
  const repaired = []
  for (const item of manualHoldings.value) {
    const code = normalizeCode(item?.code)
    let next = { ...item, code }
    if (!code) {
      repaired.push(next)
      continue
    }
    if (!String(next.name || '').trim()) {
      try {
        const detail = await getStockDetail(toExchangeCode(code), { skipErrorHandler: true })
        const fixedName = String(detail?.name || detail?.stock_name || '').trim()
        if (fixedName) {
          next.name = fixedName
          changed = true
        }
      } catch {}
    }
    if ((next.pnl_ratio === null || next.pnl_ratio === undefined || next.pnl_ratio === '') && Number(next.cost_price) > 0) {
      const marketHit = currentPriceMap.value.get(code)
      const latest = Number(marketHit?.current_price)
      const cost = Number(next.cost_price)
      if (Number.isFinite(latest) && latest > 0 && Number.isFinite(cost) && cost > 0) {
        next.current_price = latest
        next.pnl_ratio = ((latest / cost) - 1) * 100
        changed = true
      }
    }
    repaired.push(next)
  }
  if (changed) {
    manualHoldings.value = repaired
    saveManualHoldings()
    ElMessage.success('宸蹭慨澶嶅巻鍙叉寔浠擄細鍚嶇О涓庣泩浜忓凡鏇存柊')
  }
}

const refreshManualSignals = async (options = {}) => {
  if (!manualHoldings.value.length) return
  signalLoading.value = true
  try {
    const resp = await getV4ManualHoldingSignals({ holdings: manualHoldings.value })
    const signalMap = new Map((resp?.rows || []).map((r) => [String(r.code || ''), r]))
    manualHoldings.value = manualHoldings.value.map((item) => ({ ...item, signal: signalMap.get(String(item.code || '')) || null }))
    applyTrailingTakeProfitState()
    saveManualHoldings()
  } catch (error) {
    if (!options.silent) ElMessage.warning(error?.message || '鍒锋柊RSI鍗栫偣澶辫触')
  } finally {
    signalLoading.value = false
  }
}

const refreshAllHoldingData = async (options = {}) => {
  allRefreshLoading.value = true
  try {
    if (!manualHoldings.value.length) {
      if (options.includePool !== false) {
        await Promise.all([
          fetchMarketGate(),
          ...(isGen2LiveMode.value ? [] : [fetchBuyPool(selectedDate.value)]),
          fetchGen2RiskCoolShadow(selectedDate.value)
        ])
        mergeMarketStats()
      }
      if (!options.silent) {
        ElMessage[options.includePool === false ? 'warning' : 'success'](
          options.includePool === false ? '褰撳墠娌℃湁鎸佷粨鑲″彲鏇存柊' : '宸插埛鏂板ぇ鐩橀椄闂ㄤ笌涔板叆瑙傚療姹?
        )
      }
      return
    }

    const resp = await refreshV4ManualHoldingAll({
      holdings: manualHoldings.value,
      signal_date: selectedDate.value
    })
    const map = new Map((resp?.rows || []).map((x) => [normalizeCode(x?.code), x]))
    manualHoldings.value = manualHoldings.value.map((item) => {
      const hit = map.get(normalizeCode(item?.code))
      if (!hit) return item
      return {
        ...item,
        current_price: hit.latest_price ?? item.current_price,
        prev_close: hit.prev_close ?? item.prev_close,
        day_pnl: hit.day_pnl ?? item.day_pnl,
        pnl_ratio: hit.pnl_ratio ?? item.pnl_ratio,
        signal: {
          rsi15_signal: hit.rsi15_signal || item?.signal?.rsi15_signal || null,
          rsi30_signal: hit.rsi30_signal || item?.signal?.rsi30_signal || null,
          risk_level: hit.risk_level || item?.signal?.risk_level || '',
          suggestion: hit.suggestion || item?.signal?.suggestion || ''
        },
        v4_rank: hit.v4_rank ?? item.v4_rank,
        v4_score: hit.v4_score ?? item.v4_score,
        v4_signal_date: hit.v4_signal_date ?? item.v4_signal_date,
        in_score_pool: typeof hit.in_score_pool === 'boolean' ? hit.in_score_pool : item.in_score_pool,
        score_pool_status: hit.score_pool_status || item.score_pool_status || ''
      }
    })
    applyTrailingTakeProfitState()
    mergeMarketStats()
    saveManualHoldings()
    if (options.includePool !== false) {
      await Promise.all([
        fetchMarketGate(),
        ...(isGen2LiveMode.value ? [] : [fetchBuyPool(selectedDate.value)]),
        fetchGen2RiskCoolShadow(selectedDate.value)
      ])
      mergeMarketStats()
    }
    lastAutoRefreshAt.value = dayjs().format('YYYY-MM-DD HH:mm:ss')
    if (!options.silent) {
      ElMessage.success(options.successMessage || '宸蹭竴閿埛鏂帮細浠锋牸/鐩堜簭/鍗栫偣/绉诲姩姝㈢泩/澶х洏闂搁棬/涔板叆姹?)
    }
  } catch (error) {
    if (!options.silent) ElMessage.warning(error?.message || '涓€閿埛鏂板け璐?)
  } finally {
    allRefreshLoading.value = false
  }
}

const refreshCurrentHoldings = async () => {
  holdingRefreshLoading.value = true
  try {
    await refreshAllHoldingData({
      includePool: false,
      successMessage: '宸叉洿鏂版寔浠撹偂锛氳偂浠枫€佺泩浜忛噾棰濄€乂4鎺掑悕鍜屽崠鐐逛俊鍙?
    })
  } finally {
    holdingRefreshLoading.value = false
  }
}

const refreshLiveTradingState = async (options = {}) => {
  if (autoRefreshRunning.value) return
  autoRefreshRunning.value = true
  try {
    const nowMs = Date.now()
    const tasks = []
    if (shouldRunAutoRefresh(lastPtradeBridgeAutoRefreshAt, AUTO_REFRESH_PTRADE_BRIDGE_MIN_MS, nowMs)) {
      markAutoRefresh(lastPtradeBridgeAutoRefreshAt, nowMs)
      tasks.push(fetchPtradeBridgeState())
    }
    if (manualHoldings.value.length && shouldRunAutoRefresh(lastHoldingAutoRefreshAt, AUTO_REFRESH_HOLDINGS_MIN_MS, nowMs)) {
      markAutoRefresh(lastHoldingAutoRefreshAt, nowMs)
      tasks.push(refreshAllHoldingData({ silent: true, includePool: false }))
    }
    if (shouldRunAutoRefresh(lastMarketGateAutoRefreshAt, AUTO_REFRESH_MARKET_GATE_MIN_MS, nowMs)) {
      markAutoRefresh(lastMarketGateAutoRefreshAt, nowMs)
      tasks.push(fetchMarketGate(selectedDate.value))
    }
    if (shouldRunAutoRefresh(lastGen2ShadowAutoRefreshAt, AUTO_REFRESH_GEN2_SHADOW_MIN_MS, nowMs)) {
      markAutoRefresh(lastGen2ShadowAutoRefreshAt, nowMs)
      tasks.push(fetchGen2RiskCoolShadow(selectedDate.value))
    }
    if (shouldRunAutoRefresh(lastWorkflowAutoRefreshAt, AUTO_REFRESH_WORKFLOW_MIN_MS, nowMs)) {
      markAutoRefresh(lastWorkflowAutoRefreshAt, nowMs)
      tasks.push(fetchWorkflowStatus(selectedDate.value))
    }
    if (tasks.length) {
      await Promise.allSettled(tasks)
    }
    lastAutoRefreshAt.value = dayjs().format('YYYY-MM-DD HH:mm:ss')
  } catch (error) {
    if (!options.silent) ElMessage.warning(error?.message || '鑷姩鍒锋柊瀹炵洏鐘舵€佸け璐?)
  } finally {
    autoRefreshRunning.value = false
  }
}

const startAutoRefreshTimer = () => {
  if (autoRefreshTimer) clearInterval(autoRefreshTimer)
  autoRefreshTimer = setInterval(() => {
    if (!isAshareTradingTime()) return
    refreshLiveTradingState({ silent: true })
  }, AUTO_REFRESH_INTERVAL_MS)
}

const stopAutoRefreshTimer = () => {
  if (autoRefreshTimer) {
    clearInterval(autoRefreshTimer)
    autoRefreshTimer = null
  }
}

const syncCapitalAndHoldingsFromThs = async () => {
  thsCapitalSyncLoading.value = true
  try {
    const resp = await readV4ManualHoldingThsCapitalHoldings()
    if (!resp?.ok) {
      ElMessage.warning(resp?.message || '璇诲彇鍚岃姳椤鸿祫閲戣偂绁ㄥけ璐?)
      return
    }
    const syncedHoldings = Array.isArray(resp?.holdings) ? resp.holdings : []
    const oldMap = new Map((manualHoldings.value || []).map((item) => [normalizeCode(item?.code), item]))
    manualHoldings.value = syncedHoldings
      .map((item) => {
        const code = normalizeCode(item?.code)
        if (!code) return null
        const old = oldMap.get(code) || {}
        const shares = Math.max(0, Math.trunc(Number(item?.shares || 0)))
        const costPrice = Number(item?.cost_price)
        const currentPrice = Number(item?.current_price)
        const nextPnl = Number(item?.pnl_ratio)
        return {
          ...old,
          code: displayCode(code),
          name: String(item?.name || old?.name || ''),
          shares,
          cost_price: Number.isFinite(costPrice) && costPrice > 0 ? Number(costPrice.toFixed(3)) : old?.cost_price ?? null,
          current_price: Number.isFinite(currentPrice) && currentPrice > 0 ? Number(currentPrice.toFixed(3)) : old?.current_price ?? null,
          prev_close: old?.prev_close ?? null,
          day_pnl: old?.day_pnl ?? null,
          pnl_ratio: Number.isFinite(nextPnl) ? nextPnl : old?.pnl_ratio ?? null,
          market_value: item?.market_value ?? old?.market_value ?? null,
          source: old?.source || 'ths_sync',
          buy_time: old?.buy_time || '',
          buy_count_week: old?.buy_count_week || 0,
          buy_week_key: old?.buy_week_key || '',
          signal_date: old?.signal_date || '',
          signal: old?.signal || null,
          v4_rank: old?.v4_rank ?? null,
          v4_score: old?.v4_score ?? null,
          v4_signal_date: old?.v4_signal_date ?? '',
          in_score_pool: typeof old?.in_score_pool === 'boolean' ? old.in_score_pool : null,
          score_pool_status: old?.score_pool_status || ''
        }
      })
      .filter(Boolean)
    const syncedCapital = resp?.capital && typeof resp.capital === 'object' ? resp.capital : {}
    const syncedCapitalValue = (key, fallback) => (
      Object.prototype.hasOwnProperty.call(syncedCapital, key) ? syncedCapital[key] : fallback
    )
    capitalForm.value = {
      ...capitalForm.value,
      synced_total_capital: syncedCapitalValue('total_capital', capitalForm.value.synced_total_capital),
      synced_available_cash: syncedCapitalValue('available_cash', capitalForm.value.synced_available_cash),
      synced_market_value: syncedCapitalValue('market_value', capitalForm.value.synced_market_value),
      synced_cost_value: syncedCapitalValue('cost_value', capitalForm.value.synced_cost_value),
      synced_at: String(resp?.synced_at || nowMinuteText())
    }
    saveCapitalProfile()
    saveManualHoldings()
    applyTrailingTakeProfitState()
    await refreshManualSignals()
    const capitalText = capitalForm.value.synced_total_capital ? `锛涙€昏祫浜?${formatMoney(capitalForm.value.synced_total_capital)}` : ''
    if (resp?.fallback_cache) {
      ElMessage.warning(`瀹炴椂绐楀彛璇诲彇澶辫触锛屽凡鍥為€€鍒版渶杩戜竴娆℃垚鍔熷悓姝ュ揩鐓э細${manualHoldings.value.length} 鍙?{capitalText}`)
    } else {
      ElMessage.success(`宸插悓姝ュ悓鑺遍『璧勯噾鎸佽偂锛?{manualHoldings.value.length} 鍙?{capitalText}`)
    }
  } catch (error) {
    ElMessage.warning(error?.message || '鍚屾鍚岃姳椤鸿祫閲戞寔鑲″け璐?)
  } finally {
    thsCapitalSyncLoading.value = false
  }
}

const pullMonitorStatus = async () => {
  try {
    const status = await getV4ManualHoldingMonitorStatus()
    monitorEnabled.value = !!status?.enabled
    monitorTradingHoursOnly.value = status?.trading_hours_only !== false
    monitorQuietMinutes.value = Number(status?.quiet_minutes ?? 15)
    const nextRun = status?.next_run_time ? `锛涗笅娆℃墽琛?${status.next_run_time}` : ''
    const lastRun = status?.last_run_at ? `锛涙渶杩戞墽琛?${status.last_run_at}` : ''
    const lastMail = status?.last_email_sent_at ? `锛涙渶杩戦偖浠?${status.last_email_sent_at}` : ''
    const err = status?.last_error ? `锛涙渶杩戦敊璇?${status.last_error}` : ''
    monitorStatusText.value = `${monitorEnabled.value ? '杩愯涓? : '鏈紑鍚?}${nextRun}${lastRun}${lastMail}${err}`
  } catch {
    monitorStatusText.value = '鐘舵€佽鍙栧け璐?
  }
}

const toggleMonitor = async () => {
  monitorLoading.value = true
  try {
    await syncManualHoldingsStateToServer()
    await setV4ManualHoldingMonitorConfig({
      enabled: !monitorEnabled.value,
      interval_seconds: 60,
      trading_hours_only: !!monitorTradingHoursOnly.value,
      quiet_minutes: Number(monitorQuietMinutes.value || 0),
      recipient_email: ''
    })
    await pullMonitorStatus()
    ElMessage.success(monitorEnabled.value ? '宸插紑鍚瘡鍒嗛挓璺熻釜' : '宸插仠姝㈡瘡鍒嗛挓璺熻釜')
  } catch (error) {
    ElMessage.warning(error?.message || '璁剧疆鍒嗛挓璺熻釜澶辫触')
  } finally {
    monitorLoading.value = false
  }
}

const runMonitorNow = async () => {
  monitorLoading.value = true
  try {
    await syncManualHoldingsStateToServer()
    const resp = await runV4ManualHoldingMonitorNow({
      force_send: true
    })
    await pullMonitorStatus()
    if (resp?.email_sent) ElMessage.success('娴嬭瘯閭欢宸插彂閫?)
    else ElMessage.info(resp?.message || '鏈鏃犳柊淇″彿锛屾湭鍙戦€侀偖浠?)
  } catch (error) {
    ElMessage.warning(error?.message || '娴嬭瘯閫氱煡澶辫触')
  } finally {
    monitorLoading.value = false
  }
}

const quickAddHolding = async (row) => {
  const code = normalizeCode(row?.code)
  if (!code) return
  if (manualHoldings.value.length >= 3 && !manualHoldings.value.find((x) => normalizeCode(x.code) === code)) {
    ElMessage.warning('鎸佷粨涓婇檺 3 鍙紝璇峰厛鏇挎崲鍚庡啀鏂板')
    return
  }
  const weekKey = weekKeyFromTime(row?.buy_time)
  const exists = manualHoldings.value.find((x) => normalizeCode(x.code) === code)
  const addShares = Math.max(0, Math.trunc(Number(row?.shares || 0)))
  const addCost = Number(row?.cost_price)
  let beforeShares = 0
  let afterShares = 0
  let tradeCost = Number.isFinite(addCost) ? addCost : null
  if (!exists) {
    beforeShares = 0
    afterShares = addShares
    manualHoldings.value.unshift({
      code,
      name: row?.name || focusName.value || '',
      shares: addShares,
      cost_price: row?.cost_price === null || row?.cost_price === undefined ? null : Number(row?.cost_price),
      buy_count_week: 1,
      buy_week_key: weekKey,
      buy_time: row?.buy_time || '',
      source: focusSource.value || 'v4_top30',
      signal_date: focusSignalDate.value || selectedDate.value || ''
    })
    saveManualHoldings()
  } else {
    const costPnl = Number(exists?.pnl_ratio)
    if (Number.isFinite(costPnl) && costPnl < 0) {
      ElMessage.warning('浜忔崯鐘舵€佺姝㈣ˉ浠?)
      return
    }
    const sameWeekCount = exists.buy_week_key === weekKey ? Number(exists.buy_count_week || 1) : 0
    if (sameWeekCount >= 2) {
      ElMessage.warning('鍚屼竴鑲＄エ鏈懆鏈€澶氫拱鍏?2 娆?)
      return
    }
    exists.buy_week_key = weekKey
    exists.buy_count_week = sameWeekCount + 1
    beforeShares = Math.max(0, Math.trunc(Number(exists?.shares || 0)))
    afterShares = beforeShares + addShares
    const oldCost = Number(exists?.cost_price)
    const hasOld = Number.isFinite(oldCost) && oldCost > 0 && beforeShares > 0
    const hasNew = Number.isFinite(addCost) && addCost > 0 && addShares > 0
    if (addShares > 0) {
      exists.shares = afterShares
      if (hasOld && hasNew) {
        exists.cost_price = Number((((oldCost * beforeShares) + (addCost * addShares)) / afterShares).toFixed(3))
        tradeCost = addCost
      } else if (!hasOld && hasNew) {
        exists.cost_price = Number(addCost.toFixed(3))
        tradeCost = addCost
      } else if (hasOld) {
        tradeCost = oldCost
      }
    }
    if (row?.name && !exists.name) {
      exists.name = String(row.name)
    }
    if (row?.buy_time) {
      exists.buy_time = String(row.buy_time)
    }
    saveManualHoldings()
  }
  manualTradeLogs.value.unshift({
    time: String(row?.buy_time || nowMinuteText()),
    side: '涔板叆',
    code,
    name: String(row?.name || exists?.name || focusName.value || ''),
    shares: addShares,
    price: Number.isFinite(tradeCost) ? Number(tradeCost.toFixed(3)) : null,
    before_shares: beforeShares,
    after_shares: afterShares,
    realized_pnl: null,
    reason: '鏂板鎸佷粨'
  })
  saveManualTradeLogs()
  await syncWatchlistHoldingByCode(code)
  ElMessage.success(`${code} 宸插姞鍏ュ疄鐩樻寔浠撴睜`)
  await refreshManualSignals()
  await refreshManualPnlByDetail()
}

const openAddHoldingDialog = (row, mode = 'new') => {
  const code = normalizeCode(row?.code)
  addMode.value = mode
  addForm.value = {
    code,
    name: row?.name || focusName.value || '',
    shares: 0,
    cost_price: null,
    buy_time: '',
    source: focusSource.value || (row?.shadow_status ? 'gen2_risk_cool_shadow' : (row?.pool_rank ? 'daily_buy_pool' : 'v4_top30')),
    signal_date: focusSignalDate.value || selectedDate.value || ''
  }
  codeLookupText.value = ''
  liveQuotePrice.value = null
  entryBacktest.value = null
  addDialogVisible.value = true
  if (code.length === 6) {
    resolveStockByCode(code)
  }
}

const confirmAddHolding = async () => {
  const normalizedCode = normalizeCode(addForm.value.code)
  if (!normalizedCode) {
    ElMessage.warning('璇疯緭鍏?6 浣嶈偂绁ㄤ唬鐮?)
    return
  }
  if (!String(addForm.value.buy_time || '').trim()) {
    ElMessage.warning('璇峰～鍐欎拱鍏ユ椂闂达紙绮剧‘鍒板垎閽燂級')
    return
  }
  const payload = {
    code: normalizedCode,
    name: addForm.value.name,
    shares: Number(addForm.value.shares || 0),
    cost_price: addForm.value.cost_price === null ? null : Number(addForm.value.cost_price),
    buy_time: String(addForm.value.buy_time || '').trim(),
    source: addForm.value.source,
    signal_date: addForm.value.signal_date
  }
  await fetchMarketGate()
  await loadEntryBacktest(normalizedCode)
  const checks = evaluateNewHoldingRules(payload)
  ruleChecklist.value = checks
  ruleCheckPassed.value = checks.every((item) => !!item.pass)
  pendingHoldingPayload.value = payload
  addDialogVisible.value = false
  ruleDialogVisible.value = true
}

const evaluateNewHoldingRules = (holdingPayload) => {
  const code = normalizeCode(holdingPayload?.code)
  const gen2MainHit = isGen2LiveMode.value
    ? gen2ShadowRows.value.find((item) => normalizeCode(item?.code) === code)
    : null
  const buyPoolHit = isGen2LiveMode.value ? gen2MainHit : buyPoolMap.value.get(code)
  const poolRows = [
    ...(isGen2LiveMode.value ? gen2ShadowRows.value : buyPoolRows.value || []),
    ...(selection.value?.eligible_rows || []),
    ...(selection.value?.v4_reference?.top30 || []),
    ...(selection.value?.top30 || [])
  ]
  const inPool = poolRows.some((item) => normalizeCode(item?.code) === code)
  const existing = manualHoldings.value.find((x) => normalizeCode(x.code) === code)
  const sameWeekKey = weekKeyFromTime(holdingPayload?.buy_time)
  const sameWeekCount = existing && existing.buy_week_key === sameWeekKey ? Number(existing.buy_count_week || 1) : 0
  const existingPnl = Number(existing?.pnl_ratio)
  const mode = String(payload.value?.decision?.mode || '').trim().toLowerCase()
  const canOpen = !!payload.value?.decision?.can_open
  const gate = marketGate.value || {}
  const marketGatePass = !!gate.available && !!gate.can_open
  const backtestOk = !!entryBacktest.value?.ok
  const backtestCount = Number(entryBacktest.value?.sample_count || 0)
  const h5 = entryBacktest.value?.horizons?.d5 || {}
  const h10 = entryBacktest.value?.horizons?.d10 || {}
  const h5Win = Number(h5?.win_rate)
  const h5Avg = Number(h5?.avg_return)
  const h10Win = Number(h10?.win_rate)
  const h10Avg = Number(h10?.avg_return)
  const backtest5Pass = Number.isFinite(h5Win) && Number.isFinite(h5Avg) && h5Win >= STRONG_CHECK_MIN_WIN_RATE_5D && h5Avg >= STRONG_CHECK_MIN_AVG_RETURN_5D
  const backtest10Pass = Number.isFinite(h10Win) && Number.isFinite(h10Avg) && h10Win >= STRONG_CHECK_MIN_WIN_RATE_10D && h10Avg >= STRONG_CHECK_MIN_AVG_RETURN_10D
  if (isGen2LiveMode.value) {
    const alphaScore = Number(gen2MainHit?.alpha191_volume5_score ?? gen2MainHit?.alpha191_gate_score)
    const alphaRank = Number(gen2MainHit?.alpha191_volume5_rank_in_day || gen2MainHit?.v4_rank)
    const runup = Number(gen2MainHit?.runup_from_60d_low)
    const statusOk = !!gen2MainHit && !gen2MainHit.is_suspended
    const alphaOk = !!gen2MainHit && Number.isFinite(alphaScore)
    const runupOk = !!gen2MainHit && (!Number.isFinite(runup) || runup <= 100)
    return [
      { label: '涓荤瓥鐣ユ牎楠岋細Alpha191 volume5 涓诲€欓€夊懡涓?, pass: !!gen2MainHit, reason: gen2MainHit ? `Alpha191鎺掑悕 ${Number.isFinite(alphaRank) ? alphaRank : '--'}锛?{gen2MainHit.reason_text || ''}` : '褰撳墠鏍囩殑涓嶅湪 Alpha191 volume5 涓绘墽琛屽€欓€夋睜' },
      { label: '涓荤瓥鐣ユ牎楠岋細鍊欓€夋湭琚啍鏂?鏆傚仠', pass: statusOk, reason: gen2MainHit ? (gen2MainHit.status_label || gen2MainHit.shadow_status || '鍊欓€夌姸鎬佸彲鐢?) : '鍊欓€変笉瀛樺湪' },
      { label: '涓荤瓥鐣ユ牎楠岋細Alpha191 volume5 鍒嗘暟鏈夋晥', pass: alphaOk, reason: Number.isFinite(alphaScore) ? `Alpha191 volume5=${alphaScore.toFixed(4)}` : '缂哄皯 Alpha191 volume5 鍒嗘暟' },
      { label: '涓荤瓥鐣ユ牎楠岋細runup_from_60d_low <= 100%', pass: runupOk, reason: Number.isFinite(runup) ? `60鏃ヤ綆鐐逛互鏉ユ定骞?${runup.toFixed(2)}%` : '鍊欓€夋湭鎻愪緵娑ㄥ箙瀛楁锛屾寜涓诲€欓€夌粨鏋滄斁琛? },
      { label: '澶х洏闂搁棬锛氫笂璇佹寚鏁版敹鐩樹环 >= MA20', pass: marketGatePass, reason: gate.message || '涓婅瘉鎸囨暟寮€浠撶姸鎬佷笉鍙敤锛屾殏涓嶅厑璁告柊澧炴寔浠? },
      { label: '绾緥1锛氭寔浠撴暟閲忎笂闄愶紙鏈€澶?鍙級', pass: manualHoldings.value.length < 3 || !!existing, reason: `褰撳墠鎸佷粨 ${manualHoldings.value.length}/3` },
      { label: '绾緥2锛氬悓涓€鑲＄エ褰撳懆鏈€澶氫拱鍏?娆?, pass: sameWeekCount < 2, reason: `鏈懆宸蹭拱鍏?${sameWeekCount} 娆 },
      { label: '绾緥3锛氫簭鎹熺姸鎬佷笉鍏佽琛ヤ粨', pass: !(Number.isFinite(existingPnl) && existingPnl < 0), reason: Number.isFinite(existingPnl) && existingPnl < 0 ? `褰撳墠鐩堜簭 ${existingPnl.toFixed(2)}%` : '褰撳墠鏃犱簭鎹熻ˉ浠撻闄? }
    ]
  }
  return [
    { label: '澶х洏闂搁棬锛氫笂璇佹寚鏁版敹鐩樹环 >= MA20', pass: marketGatePass, reason: gate.message || '涓婅瘉鎸囨暟寮€浠撶姸鎬佷笉鍙敤锛屾殏涓嶅厑璁告柊澧炴寔浠? },
    { label: '寮烘牎楠岋細绛栫暐闃舵鍏佽寮€浠?, pass: canOpen, reason: payload.value?.decision?.message || '闇€澶勪簬鍙紑浠撻樁娈? },
    { label: '寮烘牎楠岋細浠呭厑璁镐富鍗囨ā寮忓紑鏂颁粨', pass: mode === 'on', reason: `褰撳墠妯″紡 ${mode || '--'}锛涢€€娼?涓€ч樁娈电姝㈡柊澧瀈 },
    { label: '寮烘牎楠岋細涔板叆瑙傚療姹犲懡涓?, pass: !!buyPoolHit, reason: buyPoolHit ? `涔板叆姹犵 ${buyPoolHit.pool_rank || '--'} 鍚嶏紱${buyPoolHit.reason_text || ''}` : '褰撳墠鏍囩殑涓嶅湪浠婃棩涔板叆瑙傚療姹? },
    { label: '寮烘牎楠岋細鍔ㄩ噺宸茬粡鍑虹幇', pass: !!buyPoolHit?.momentum_pass, reason: buyPoolHit ? `5鏃ュ姩閲?${formatPct(buyPoolHit.mom5_pct)}锛?0鏃ュ姩閲?${formatPct(buyPoolHit.mom10_pct)}` : '涔板叆姹犳暟鎹笉瓒? },
    { label: '寮烘牎楠岋細V4鍏ュ満闃堝€肩‘璁?, pass: !!buyPoolHit?.adjustment_pass, reason: buyPoolHit ? `${buyPoolHit.setup_label || '瑙傚療'}锛?{buyPoolHit.reason_text || ''}` : '涔板叆姹犳暟鎹笉瓒? },
    { label: '寮烘牎楠岋細鍊欓€夋睜鍛戒腑锛堜富鍊欓€夋垨V4鍓?0锛?, pass: inPool, reason: inPool ? '褰撳墠鏍囩殑鍛戒腑鍊欓€夋睜' : '褰撳墠鏍囩殑涓嶅湪鍊欓€夋睜' },
    { label: '寮烘牎楠岋細5鏃ラ鏈熻揪鏍?, pass: backtest5Pass, reason: backtestOk && backtestCount > 0 ? `5鏃ユ垚鍔熺巼 ${Number.isFinite(h5Win) ? h5Win.toFixed(2) : '--'}%锛?鏃ュ潎娑?${Number.isFinite(h5Avg) ? h5Avg.toFixed(2) : '--'}%锛堥槇鍊?${STRONG_CHECK_MIN_WIN_RATE_5D}% / ${STRONG_CHECK_MIN_AVG_RETURN_5D}%锛塦 : '鍘嗗彶鏍锋湰涓嶈冻鎴栧洖娴嬪け璐? },
    { label: '寮烘牎楠岋細10鏃ラ鏈熻揪鏍?, pass: backtest10Pass, reason: backtestOk && backtestCount > 0 ? `10鏃ユ垚鍔熺巼 ${Number.isFinite(h10Win) ? h10Win.toFixed(2) : '--'}%锛?0鏃ュ潎娑?${Number.isFinite(h10Avg) ? h10Avg.toFixed(2) : '--'}%锛堥槇鍊?${STRONG_CHECK_MIN_WIN_RATE_10D}% / ${STRONG_CHECK_MIN_AVG_RETURN_10D}%锛塦 : '鍘嗗彶鏍锋湰涓嶈冻鎴栧洖娴嬪け璐? },
    { label: '绾緥1锛氭寔浠撴暟閲忎笂闄愶紙鏈€澶?鍙級', pass: manualHoldings.value.length < 3 || !!existing, reason: `褰撳墠鎸佷粨 ${manualHoldings.value.length}/3` },
    { label: '绾緥2锛氬悓涓€鑲＄エ褰撳懆鏈€澶氫拱鍏?娆?, pass: sameWeekCount < 2, reason: `鏈懆宸蹭拱鍏?${sameWeekCount} 娆 },
    { label: '绾緥3锛氫簭鎹熺姸鎬佷笉鍏佽琛ヤ粨', pass: !(Number.isFinite(existingPnl) && existingPnl < 0), reason: Number.isFinite(existingPnl) && existingPnl < 0 ? `褰撳墠鐩堜簭 ${existingPnl.toFixed(2)}%` : '褰撳墠鏃犱簭鎹熻ˉ浠撻闄? }
  ]
}

const confirmAddHoldingAfterCheck = async () => {
  if (!ruleCheckPassed.value || !pendingHoldingPayload.value) return
  ruleDialogVisible.value = false
  await quickAddHolding(pendingHoldingPayload.value)
  pendingHoldingPayload.value = null
}

const removeManualHolding = (index) => {
  manualHoldings.value.splice(index, 1)
  saveManualHoldings()
}

const openSellDialog = (row, index) => {
  const maxShares = Math.max(0, Math.trunc(Number(row?.shares || 0)))
  sellForm.value = {
    index,
    code: normalizeCode(row?.code),
    name: String(row?.name || ''),
    max_shares: maxShares,
    shares: maxShares > 0 ? maxShares : 1,
    price: Number(row?.current_price || row?.cost_price || 0) || null,
    time: nowMinuteText(),
    reason: '绛栫暐鍗栧嚭'
  }
  sellDialogVisible.value = true
}

const sellHalfPosition = () => {
  const maxShares = Math.max(1, Math.trunc(Number(sellForm.value.max_shares || 1)))
  sellForm.value.shares = Math.max(1, Math.floor(maxShares / 2))
}

const sellAllPosition = () => {
  const maxShares = Math.max(1, Math.trunc(Number(sellForm.value.max_shares || 1)))
  sellForm.value.shares = maxShares
}

const confirmSellHolding = async () => {
  const idx = Number(sellForm.value.index)
  if (idx < 0 || idx >= manualHoldings.value.length) {
    ElMessage.warning('鎸佷粨绱㈠紩鏃犳晥')
    return
  }
  const shares = Math.max(0, Math.trunc(Number(sellForm.value.shares || 0)))
  const maxShares = Math.max(0, Math.trunc(Number(sellForm.value.max_shares || 0)))
  if (!Number.isFinite(shares) || shares <= 0 || shares > maxShares) {
    ElMessage.warning('鍗栧嚭鏁伴噺涓嶅悎娉?)
    return
  }
  const row = { ...manualHoldings.value[idx] }
  const remain = Math.max(0, maxShares - shares)
  const beforeShares = maxShares
  if (remain <= 0) {
    manualHoldings.value.splice(idx, 1)
  } else {
    row.shares = remain
    manualHoldings.value[idx] = row
  }
  const sellPrice = Number(sellForm.value.price)
  const costPrice = Number(row.cost_price)
  let realizedPnl = null
  if (Number.isFinite(sellPrice) && Number.isFinite(costPrice)) {
    realizedPnl = (sellPrice - costPrice) * shares
  }
  manualTradeLogs.value.unshift({
    time: String(sellForm.value.time || nowMinuteText()),
    side: '鍗栧嚭',
    code: normalizeCode(sellForm.value.code),
    name: String(sellForm.value.name || row.name || ''),
    shares,
    price: Number.isFinite(sellPrice) ? Number(sellPrice.toFixed(3)) : null,
    before_shares: beforeShares,
    after_shares: remain,
    realized_pnl: realizedPnl,
    reason: String(sellForm.value.reason || '')
  })
  saveManualTradeLogs()
  saveManualHoldings()
  await syncWatchlistHoldingByCode(sellForm.value.code)
  sellDialogVisible.value = false
  ElMessage.success(remain <= 0 ? '宸叉竻浠撳苟鍚屾' : `宸插崠鍑?${shares} 鑲★紝鍓╀綑 ${remain} 鑲)
}

const disciplineTagText = (row) => {
  if (isSingleTradeLossCapBreached(row)) return '瓒呴槇鍊?
  if (isTrailingTakeProfitTriggered(row)) return '绉诲姩姝㈢泩'
  const pnl = Number(row?.pnl_ratio)
  if (Number.isFinite(pnl) && pnl <= -6) return '搴斿鐞?
  if (Number.isFinite(pnl) && pnl <= -4) return '绂佸姞浠?
  return '鏈Е鍙?
}

const disciplineTagType = (row) => {
  const text = disciplineTagText(row)
  if (text === '瓒呴槇鍊?) return 'danger'
  if (text === '绉诲姩姝㈢泩') return 'warning'
  if (text === '搴斿鐞?) return 'danger'
  if (text === '绂佸姞浠?) return 'warning'
  return 'success'
}

const formatPct = (value) => {
  if (value === null || value === undefined || value === '') return '--'
  const n = Number(value)
  return Number.isFinite(n) ? `${n.toFixed(2)}%` : '--'
}

const formatScore = (value) => {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(4) : '--'
}

const formatRatio = (value) => {
  const n = Number(value)
  return Number.isFinite(n) ? `${n.toFixed(2)}鍊峘 : '--'
}

const formatMoney = (value) => {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(2) : '--'
}

const formatPrice = (value) => {
  const n = Number(value)
  return Number.isFinite(n) && n > 0 ? n.toFixed(3) : '--'
}

const formatSignedPct = (value) => {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  return `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`
}

const aSharePnlStyle = (value, emphasize = false) => {
  const n = Number(value)
  let color = '#24355d'
  if (Number.isFinite(n) && n > 0) color = '#d4380d'
  if (Number.isFinite(n) && n < 0) color = '#389e0d'
  return {
    color,
    fontWeight: emphasize ? 700 : 400
  }
}

const buyPoolStatusText = (row) => {
  if (row?.can_buy) return '鍙拱鍏?
  if (row?.candidate_status === 'primary') return '寰呴椄闂?
  return '鐟欏倸鐧?
}

const gen2ShadowTagType = (row) => {
  if (row?.shadow_status === 'suspended_by_two_stop_cd3' || row?.shadow_status === 'suspended_by_stop_cd5') return 'danger'
  if (row?.shadow_status === 'executed') return 'success'
  if (row?.shadow_status === 'observable') return 'warning'
  return 'info'
}

const gen2ShadowCanAddHolding = (row) => {
  if (!row || row.is_suspended) return false
  return row.can_buy === true || row.buyable === true || row.stage_label === '鍙拱鍏ヨ瀵?
}

const gen2ShadowStatusText = (row) => {
  if (row?.status_label) return row.status_label
  if (row?.shadow_status === 'suspended_by_two_stop_cd3' || row?.shadow_status === 'suspended_by_stop_cd5') return '鐔旀柇鏆傚仠'
  if (row?.shadow_status === 'executed') return '宸叉墽琛屽奖瀛?
  if (row?.shadow_status === 'observable') return '鍙瀵?
  return '鏈煡'
}

const gen2VerificationTagType = (row) => row?.verification_type || 'info'

const gen2VerificationText = (row) => row?.verification_label || '鏈爣璁?

const gen2VerificationNoteText = (row) => {
  const parts = []
  const position = Number(row?.verification_position_pct)
  const fill = Number(row?.verification_fill_price)
  if (Number.isFinite(position) && position > 0) parts.push(`${position.toFixed(0)}%浠揱)
  if (Number.isFinite(fill) && fill > 0) parts.push(`鎴愪氦${fill.toFixed(3)}`)
  if (row?.verification_note) parts.push(row.verification_note)
  return parts.join(' / ') || '--'
}

const openGen2VerificationDialog = (row) => {
  gen2VerificationForm.value = {
    row,
    status: row?.verification_status || '',
    note: row?.verification_note || '',
    position_pct: row?.verification_position_pct ?? null,
    fill_price: row?.verification_fill_price ?? null
  }
  gen2VerificationDialogVisible.value = true
}

const saveGen2Verification = async () => {
  const row = gen2VerificationForm.value.row
  if (!row) return
  gen2VerificationSaving.value = true
  try {
    const payload = {
      verification_key: row.verification_key,
      code: row.code,
      code6: row.code6,
      entry_date: row.entry_date,
      confirm_datetime: row.confirm_datetime,
      alpha191_gate_variant: row.alpha191_gate_variant,
      status: gen2VerificationForm.value.status || '',
      note: gen2VerificationForm.value.note || '',
      position_pct: gen2VerificationForm.value.position_pct,
      fill_price: gen2VerificationForm.value.fill_price
    }
    const saved = await saveGen2ShadowVerification(payload)
    if (!saved?.ok) {
      ElMessage.warning(saved?.error || 'G2楠岃瘉璁板綍淇濆瓨澶辫触')
      return
    }
    row.verification_key = saved.verification_key || row.verification_key
    row.verification_status = saved.status || ''
    row.verification_label = saved.label || '鏈爣璁?
    row.verification_type = saved.type || 'info'
    row.verification_note = saved.note || ''
    row.verification_position_pct = saved.position_pct ?? null
    row.verification_fill_price = saved.fill_price ?? null
    row.verification_updated_at = saved.updated_at || ''
    gen2VerificationDialogVisible.value = false
    await fetchGen2RiskCoolShadow(selectedDate.value)
    ElMessage.success('G2楠岃瘉璁板綍宸蹭繚瀛?)
  } catch (err) {
    ElMessage.warning(err?.message || 'G2楠岃瘉璁板綍淇濆瓨澶辫触')
  } finally {
    gen2VerificationSaving.value = false
  }
}

const nowMinuteText = () => {
  const d = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

const buildTradeMarkerKey = (row) => {
  const normalizedCode = normalizeCode(row?.code)
  return `${STOCK_DETAIL_TRADE_MARKS_PREFIX}${normalizedCode || 'unknown'}_${Date.now()}`
}

const saveTradeMarkersForDetail = (row) => {
  const trades = Array.isArray(row?.trades) ? row.trades : []
  if (!trades.length) return ''
  const payload = trades.map((item) => ({
    code: normalizeCode(item?.code || row?.code),
    name: String(item?.name || row?.name || ''),
    side: String(item?.side || ''),
    time: String(item?.time || ''),
    price: Number(item?.price),
    shares: Math.max(0, Math.trunc(Number(item?.shares || 0))),
    reason: String(item?.reason || '')
  })).filter((item) => item.code && item.time)
  if (!payload.length) return ''
  const key = buildTradeMarkerKey(row)
  sessionStorage.setItem(key, JSON.stringify(payload))
  return key
}

const openStockDetail = (row) => {
  const code = toExchangeCode(row?.code)
  if (!code) {
    ElMessage.warning('鑲＄エ浠ｇ爜鏃犳晥锛屾棤娉曟墦寮€璇︽儏')
    return
  }
  const tradeMarksKey = saveTradeMarkersForDetail(row)
  router.push({
    name: 'StockDetail',
    params: { code },
    query: tradeMarksKey ? { trade_marks_key: tradeMarksKey } : undefined
  })
}

const fetchMarketGate = async (tradeDate) => {
  marketGateLoading.value = true
  try {
    const params = {}
    if (typeof tradeDate === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(tradeDate)) {
      params.trade_date = tradeDate
    }
    marketGate.value = await getV4MarketGate(params)
  } catch (err) {
    marketGate.value = {
      available: false,
      can_open: false,
      message: '澶х洏寮€浠撻椄闂ㄨ鍙栧け璐ワ紝鏆備笉鍏佽鏂板鎸佷粨銆?
    }
  } finally {
    marketGateLoading.value = false
  }
}

const fetchBuyPool = async () => {
  buyPoolLoading.value = true
  try {
    buyPool.value = null
  } finally {
    buyPoolLoading.value = false
  }
}

const fetchGen2RiskCoolShadow = async (tradeDate) => {
  gen2ShadowLoading.value = true
  try {
    const params = { limit: 30 }
    if (typeof tradeDate === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(tradeDate)) {
      params.signal_date = tradeDate
    }
    gen2Shadow.value = await getGen2RiskCoolShadow(params)
  } catch (err) {
    const previousRows = Array.isArray(gen2Shadow.value?.rows) ? gen2Shadow.value.rows : []
    gen2Shadow.value = {
      ...(gen2Shadow.value || {}),
      available: previousRows.length > 0,
      rows: previousRows,
      message: previousRows.length > 0
        ? 'G2褰卞瓙瑙傚療鏈鍒锋柊澶辫触锛屽凡淇濈暀涓婁竴娆℃垚鍔熺粨鏋溿€?
        : 'G2褰卞瓙瑙傚療璇诲彇澶辫触锛岃绋嶅悗閲嶈瘯銆?
    }
  } finally {
    gen2ShadowLoading.value = false
  }
}

const fetchWorkflowStatus = async (tradeDate) => {
  workflowLoading.value = true
  try {
    const params = {}
    if (typeof tradeDate === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(tradeDate)) {
      params.signal_date = tradeDate
    }
    workflowStatus.value = await getGen2WorkflowStatus(params)
  } catch (err) {
    workflowStatus.value = {
      available: false,
      ok: false,
      message: err?.message || '绛栫暐宸ヤ綔娴佺姸鎬佽鍙栧け璐?,
      pipeline_stages: [],
      blockers: []
    }
  } finally {
    workflowLoading.value = false
  }
}

const waitForGen2ShadowUpdateTask = async (taskId) => {
  for (let i = 0; i < 90; i += 1) {
    const task = await getGen2RiskCoolShadowUpdateTask(taskId)
    gen2ShadowUpdateTask.value = task || {}
    if (task?.status === 'completed' || task?.status === 'failed' || task?.status === 'missing') {
      return task
    }
    await new Promise((resolve) => setTimeout(resolve, 2000))
  }
  return gen2ShadowUpdateTask.value
}

const updateGen2RiskCoolShadow = async (tradeDate) => {
  gen2ShadowUpdateLoading.value = true
  try {
    const params = { pool_rank: 200, alpha191_gate: isGen2LiveMode.value ? GEN2_MAIN_ALPHA191_GATE : (gen2Alpha191Gate.value || 'off') }
    if (typeof tradeDate === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(tradeDate)) {
      params.signal_date = tradeDate
    }
    const task = await runGen2RiskCoolShadowUpdate(params)
    gen2ShadowUpdateTask.value = task || {}
    const finalTask = task?.task_id ? await waitForGen2ShadowUpdateTask(task.task_id) : task
    if (finalTask?.status === 'completed') {
      ElMessage.success('G2褰卞瓙浜ゆ槗宸叉洿鏂?)
      await fetchGen2RiskCoolShadow(tradeDate)
    } else {
      ElMessage.warning(finalTask?.error || 'G2褰卞瓙浜ゆ槗鏇存柊鏈畬鎴?)
    }
  } catch (err) {
    ElMessage.warning(err?.message || 'G2褰卞瓙浜ゆ槗鏇存柊澶辫触')
  } finally {
    gen2ShadowUpdateLoading.value = false
  }
}

const pullGen2ShadowMonitorStatus = async () => {
  try {
    gen2ShadowMonitorStatus.value = await getGen2ShadowMonitorStatus()
    if (gen2ShadowMonitorStatus.value?.alpha191_gate) {
      gen2Alpha191Gate.value = isGen2LiveMode.value ? GEN2_MAIN_ALPHA191_GATE : gen2ShadowMonitorStatus.value.alpha191_gate
    }
  } catch {
    gen2ShadowMonitorStatus.value = {
      enabled: false,
      last_error: '鐘舵€佽鍙栧け璐?
    }
  }
}

const fetchPtradeBridgeState = async () => {
  ptradeBridgeLoading.value = true
  try {
    const [status, audit, evidence, orders, fills, positions] = await Promise.all([
      getPtradeBridgeStatus(),
      getPtradeBridgeReadinessAudit({ require_empty_queue_for_live: true }),
      getPtradeBridgeEvidenceReport(),
      listPtradeBridgeOrders({ limit: 50 }),
      listPtradeBridgeFills({ limit: 50 }),
      getPtradeBridgePositions({ limit: 20 })
    ])
    ptradeBridgeStatus.value = status || {}
    ptradeBridgeAudit.value = audit || null
    ptradeBridgeEvidence.value = evidence || null
    ptradeBridgeOrders.value = Array.isArray(orders?.rows) ? orders.rows : []
    ptradeBridgeFills.value = Array.isArray(fills?.rows) ? fills.rows : []
    ptradeBridgePositions.value = positions || null
  } catch (err) {
    ptradeBridgeStatus.value = {
      ok: false,
      message: err?.message || '妯℃嫙鐩樻ˉ鎺ョ姸鎬佽鍙栧け璐?
    }
    ptradeBridgeAudit.value = null
    ptradeBridgeEvidence.value = null
    ptradeBridgeOrders.value = []
    ptradeBridgeFills.value = []
    ptradeBridgePositions.value = null
  } finally {
    ptradeBridgeLoading.value = false
  }
}

const runPtradeBridgeProbe = async (submitDryRun = false) => {
  ptradeBridgeProbeLoading.value = true
  try {
    const result = await runPtradeBridgeLiveProbe({
      submit_dry_run: !!submitDryRun,
      require_heartbeat: false,
      timeout_seconds: submitDryRun ? 30 : 3,
      poll_seconds: 1,
      code: '600000',
      price: 10.5,
      quantity: 100
    })
    ptradeBridgeProbeResult.value = result || null
    ptradeBridgeStatus.value = result?.final_status || ptradeBridgeStatus.value
    if (result?.ok) {
      ElMessage.success(submitDryRun ? 'PTrade dry-run 鎺㈤拡閫氳繃' : 'PTrade 鍙鎺㈤拡瀹屾垚')
    } else {
      ElMessage.warning(result?.message || 'PTrade 鎺㈤拡鏈€氳繃锛岃鏌ョ湅妗ユ帴鐘舵€?)
    }
    await fetchPtradeBridgeState()
  } catch (err) {
    ElMessage.warning(err?.message || 'PTrade 鎺㈤拡鎵ц澶辫触')
  } finally {
    ptradeBridgeProbeLoading.value = false
  }
}

const waitForPtradeBridgeAcceptanceTask = async (taskId) => {
  let latest = null
  for (let i = 0; i < 90; i += 1) {
    const task = await getPtradeBridgeAcceptanceTask(taskId)
    latest = task || latest
    if (task?.status === 'completed' || task?.status === 'failed' || task?.status === 'missing') {
      return task
    }
    await new Promise((resolve) => setTimeout(resolve, 2000))
  }
  return latest || { status: 'timeout', error: 'PTrade 楠屾敹闂ㄧ绛夊緟瓒呮椂' }
}

const runPtradeBridgeAcceptanceGate = async () => {
  ptradeBridgeAcceptanceLoading.value = true
  try {
    const task = await startPtradeBridgeAcceptance({
      heartbeat_timeout_seconds: 120,
      dry_run_timeout_seconds: 30,
      poll_seconds: 1,
      code: '600000',
      price: 10.5,
      quantity: 100
    })
    const finalTask = task?.task_id ? await waitForPtradeBridgeAcceptanceTask(task.task_id) : task
    const result = finalTask?.result || null
    ptradeBridgeAcceptanceResult.value = result || null
    ptradeBridgeStatus.value = result?.readiness_audit?.status || ptradeBridgeStatus.value
    ptradeBridgeAudit.value = result?.readiness_audit || ptradeBridgeAudit.value
    if (result?.ok) {
      ElMessage.success('PTrade 楠屾敹闂ㄧ閫氳繃锛屽彲杩涘叆灏忛浜哄伐鎵瑰噯 live-submit 娴嬭瘯')
    } else {
      const nextAction = Array.isArray(result?.next_actions) ? result.next_actions[0] : ''
      ElMessage.warning(finalTask?.error || nextAction || 'PTrade 楠屾敹闂ㄧ鏈€氳繃')
    }
    await fetchPtradeBridgeState()
  } catch (err) {
    ElMessage.warning(err?.message || 'PTrade 楠屾敹闂ㄧ鎵ц澶辫触')
  } finally {
    ptradeBridgeAcceptanceLoading.value = false
  }
}

const waitForPtradeBridgeWatchAcceptanceTask = async (taskId) => {
  let latest = null
  for (let i = 0; i < 360; i += 1) {
    const task = await getPtradeBridgeWatchAcceptanceTask(taskId)
    latest = task || latest
    if (task?.status === 'completed' || task?.status === 'failed' || task?.status === 'missing') {
      return task
    }
    await new Promise((resolve) => setTimeout(resolve, 2000))
  }
  return latest || { status: 'timeout', error: 'PTrade 绛夊緟楠屾敹瓒呮椂' }
}

const runPtradeBridgeWatchAcceptance = async () => {
  ptradeBridgeWatchAcceptanceLoading.value = true
  try {
    const task = await startPtradeBridgeWatchAcceptance({
      watch_timeout_seconds: 600,
      dry_run_timeout_seconds: 30,
      poll_seconds: 1,
      code: '600000',
      price: 10.5,
      quantity: 100
    })
    const finalTask = task?.task_id ? await waitForPtradeBridgeWatchAcceptanceTask(task.task_id) : task
    const result = finalTask?.result || null
    ptradeBridgeWatchAcceptanceResult.value = result || null
    ptradeBridgeAcceptanceResult.value = result?.acceptance || ptradeBridgeAcceptanceResult.value
    ptradeBridgeStatus.value = result?.readiness_audit?.status || result?.acceptance?.readiness_audit?.status || ptradeBridgeStatus.value
    ptradeBridgeAudit.value = result?.readiness_audit || result?.acceptance?.readiness_audit || ptradeBridgeAudit.value
    if (result?.ok) {
      ElMessage.success('PTrade 绛夊緟楠屾敹閫氳繃')
    } else {
      const nextAction = Array.isArray(result?.next_actions) ? result.next_actions[0] : ''
      ElMessage.warning(finalTask?.error || nextAction || 'PTrade 绛夊緟楠屾敹鏈€氳繃')
    }
    await fetchPtradeBridgeState()
  } catch (err) {
    ElMessage.warning(err?.message || 'PTrade 绛夊緟楠屾敹鎵ц澶辫触')
  } finally {
    ptradeBridgeWatchAcceptanceLoading.value = false
  }
}

const waitForPtradeBridgeLiveSubmitTestTask = async (taskId) => {
  let latest = null
  for (let i = 0; i < 90; i += 1) {
    const task = await getPtradeBridgeLiveSubmitTestTask(taskId)
    latest = task || latest
    if (task?.status === 'completed' || task?.status === 'failed' || task?.status === 'missing') {
      return task
    }
    await new Promise((resolve) => setTimeout(resolve, 2000))
  }
  return latest || { status: 'timeout', error: 'PTrade live-submit test timeout' }
}

const runPtradeBridgeLiveSubmitTest = async () => {
  if (!ptradeBridgeAudit.value?.gates?.live_submit_ready) {
    ElMessage.warning('PTrade live-submit test is not ready')
    return
  }
  try {
    await ElMessageBox.confirm(
      'Confirm one approved small live-submit test in Xiangcai PTrade cloud simulation?',
      'PTrade live-submit test',
      { confirmButtonText: 'Confirm', cancelButtonText: 'Cancel', type: 'warning' }
    )
  } catch {
    return
  }
  ptradeBridgeLiveSubmitTestLoading.value = true
  try {
    const task = await startPtradeBridgeLiveSubmitTest({
      approve_live_submit: true,
      code: '600000',
      side: 'BUY',
      price: 10.5,
      quantity: 100,
      timeout_seconds: 30,
      poll_seconds: 1,
      max_submit_seconds: 0.5,
      max_order_value: 20000
    })
    const finalTask = task?.task_id ? await waitForPtradeBridgeLiveSubmitTestTask(task.task_id) : task
    const result = finalTask?.result || null
    ptradeBridgeLiveSubmitTestResult.value = result || null
    ptradeBridgeStatus.value = result?.final_status || ptradeBridgeStatus.value
    ptradeBridgeAudit.value = result?.readiness_audit || ptradeBridgeAudit.value
    if (result?.ok) {
      ElMessage.success('PTrade live-submit test passed')
    } else {
      ElMessage.warning(finalTask?.error || 'PTrade live-submit test blocked')
    }
    await fetchPtradeBridgeState()
  } catch (err) {
    ElMessage.warning(err?.message || 'PTrade live-submit test failed')
  } finally {
    ptradeBridgeLiveSubmitTestLoading.value = false
  }
}

const isSubmittingPaperOrder = (row) => paperOrderSubmittingCode.value && normalizeCode(row?.code) === paperOrderSubmittingCode.value

const hasActivePaperOrder = (row) => {
  const code = normalizeCode(row?.code)
  const signalDate = String(row?.entry_date || selectedDate.value || '')
  return ptradeActiveOrders.value.some((item) => normalizeCode(item?.code) === code && String(item?.signal_date || '') === signalDate && String(item?.side || '').toUpperCase() === 'BUY')
}

const paperOrderBlockReason = (row) => {
  const code = normalizeCode(row?.code)
  if (!code) return '缂哄皯鑲＄エ浠ｇ爜'
  if (isSubmittingPaperOrder(row)) return '鎻愪氦涓?
  if (hasActivePaperOrder(row)) return '宸叉湁鎸傚崟'
  const signalDate = String(row?.entry_date || selectedDate.value || '')
  if (!signalDate) return '缂哄皯淇″彿鏃ユ湡'
  const rowDate = String(row?.entry_date || row?.signal_date || row?.trade_date || row?.date || selectedDate.value || '')
  if (!row || normalizeCode(row?.code) !== code || rowDate !== signalDate) return '闇€鍒锋柊蹇収'
  const gate = marketGate.value || {}
  const gateDate = String(gate.trade_date || gate.signal_date || gate.snapshot_date || gate.date || '')
  if (!gateDate || gateDate !== signalDate || typeof gate.available === 'undefined' || typeof gate.can_open === 'undefined') return '闇€鍒锋柊闂ㄧ'
  if (!gate.available) return '闂ㄧ涓嶅彲鐢?
  if (!gate.can_open) return '闂ㄧ鏈€氳繃'
  return ''
}

const canSubmitPaperOrder = (row) => !paperOrderBlockReason(row)

const paperOrderButtonText = (row) => paperOrderBlockReason(row) || '妯℃嫙鐩樹笅鍗?

const submitPaperOrderForRow = async (row) => {
  const code = normalizeCode(row?.code)
  if (!code) {
    ElMessage.warning('缂哄皯鑲＄エ浠ｇ爜锛屾棤娉曟彁浜ゆā鎷熺洏璁㈠崟')
    return
  }
  const blockReason = paperOrderBlockReason(row)
  if (blockReason) {
    ElMessage.warning(`${code} ${blockReason}锛岃鍒锋柊 G2 瀹炵洏椤靛悗閲嶈瘯`)
    return
  }
  paperOrderSubmittingCode.value = code
  try {
    const resp = await submitGen2PaperOrder({
      code,
      signal_date: String(row?.entry_date || selectedDate.value || ''),
      candidate: row,
      market_gate: marketGate.value,
      dry_run: true,
      require_approval: true
    })
    if (resp?.ok) {
      ElMessage.success(resp?.message || `${code} 宸叉彁浜ゅ埌妯℃嫙鐩樻ˉ鎺ョ洰褰昤)
      await fetchPtradeBridgeState()
    } else {
      ElMessage.warning(resp?.message || `${code} 妯℃嫙鐩樹笅鍗曞け璐)
    }
  } catch (err) {
    ElMessage.warning(err?.message || `${code} 妯℃嫙鐩樹笅鍗曞け璐)
  } finally {
    paperOrderSubmittingCode.value = ''
  }
}

const importPtradePositionsToManualHoldings = async () => {
  const rows = ptradeLatestPositionRows.value
  if (!rows.length) {
    ElMessage.warning('鏆傛棤鍙鍏ョ殑妯℃嫙鐩樻寔浠撳揩鐓?)
    return
  }
  const oldMap = new Map((manualHoldings.value || []).map((item) => [normalizeCode(item?.code), item]))
  manualHoldings.value = rows
    .map((item) => {
      const code = normalizeCode(item?.code)
      if (!code) return null
      const old = oldMap.get(code) || {}
      const shares = Math.max(0, Math.trunc(Number(item?.shares || item?.quantity || 0)))
      const costPrice = Number(item?.cost_price ?? item?.avg_cost)
      const currentPrice = Number(item?.current_price ?? item?.price)
      const pnlRatio = Number(item?.pnl_ratio)
      return {
        ...old,
        code: displayCode(code),
        name: String(item?.name || old?.name || ''),
        shares,
        cost_price: Number.isFinite(costPrice) && costPrice > 0 ? Number(costPrice.toFixed(3)) : old?.cost_price ?? null,
        current_price: Number.isFinite(currentPrice) && currentPrice > 0 ? Number(currentPrice.toFixed(3)) : old?.current_price ?? null,
        pnl_ratio: Number.isFinite(pnlRatio) ? pnlRatio : old?.pnl_ratio ?? null,
        market_value: item?.market_value ?? old?.market_value ?? null,
        source: 'ptrade_bridge',
        buy_time: old?.buy_time || '',
        buy_count_week: old?.buy_count_week || 0,
        buy_week_key: old?.buy_week_key || '',
        signal_date: old?.signal_date || '',
        signal: old?.signal || null,
        v4_rank: old?.v4_rank ?? null,
        v4_score: old?.v4_score ?? null,
        v4_signal_date: old?.v4_signal_date ?? '',
        in_score_pool: typeof old?.in_score_pool === 'boolean' ? old.in_score_pool : null,
        score_pool_status: old?.score_pool_status || ''
      }
    })
    .filter(Boolean)
  saveManualHoldings()
  await refreshManualSignals()
  ElMessage.success(`宸插鍏ユā鎷熺洏鎸佷粨锛?{manualHoldings.value.length} 鍙猔)
}

const toggleGen2ShadowMonitor = async () => {
  gen2ShadowMonitorLoading.value = true
  try {
    const enabled = !gen2ShadowMonitorStatus.value?.enabled
    gen2ShadowMonitorStatus.value = await setGen2ShadowMonitorConfig({
      enabled,
      interval_seconds: 60,
      pool_rank: 200,
      alpha191_gate: isGen2LiveMode.value ? GEN2_MAIN_ALPHA191_GATE : (gen2Alpha191Gate.value || 'off'),
      trading_hours_only: true,
      recipient_email: ''
    })
    ElMessage.success(enabled ? '宸插紑鍚疓2 15鍒嗛挓涔扮偣鎺㈡祴' : '宸插仠姝2涔扮偣鎺㈡祴')
  } catch (err) {
    ElMessage.warning(err?.message || '璁剧疆G2涔扮偣鎺㈡祴澶辫触')
  } finally {
    gen2ShadowMonitorLoading.value = false
  }
}

const runGen2ShadowMonitorOnce = async () => {
  gen2ShadowMonitorLoading.value = true
  try {
    const resp = await runGen2ShadowMonitorNow({ force_send: true })
    await pullGen2ShadowMonitorStatus()
    await fetchGen2RiskCoolShadow(resp?.signal_date || selectedDate.value)
    await fetchWorkflowStatus(resp?.signal_date || selectedDate.value)
    if (resp?.email_sent) ElMessage.success('G2涔扮偣鎻愰啋閭欢宸插彂閫?)
    else ElMessage.info(resp?.message || '鏈娌℃湁鏂板G2涔扮偣锛屾湭鍙戦€侀偖浠?)
  } catch (err) {
    ElMessage.warning(err?.message || 'G2涔扮偣鎺㈡祴澶辫触')
  } finally {
    gen2ShadowMonitorLoading.value = false
  }
}

const fetchData = async () => {
  loading.value = true
  try {
    const resp = await getGen2Live({ target_date: targetDate.value || undefined })
    available.value = !!resp?.available
    message.value = resp?.message || ''
    payload.value = resp || {}
    await fetchMarketGate()
    if (!isGen2LiveMode.value) {
      await fetchBuyPool(resp?.selected_date || targetDate.value || undefined)
    } else {
      buyPool.value = null
    }
    await fetchGen2RiskCoolShadow(resp?.selected_date || targetDate.value || undefined)
    await fetchWorkflowStatus(resp?.selected_date || targetDate.value || undefined)
    mergeMarketStats()
    saveManualHoldings()
    if (manualHoldings.value.length) {
      await refreshAllHoldingData({ silent: true, includePool: false })
    }
  } finally {
    loading.value = false
  }
}

const refreshSnapshot = async () => {
  refreshing.value = true
  try {
    const candidateDate = [targetDate.value, selectedDate.value, latestTradeDate.value].find((item) => typeof item === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(item))
    const signalDate = candidateDate || undefined
    await updateGen2RiskCoolShadow(signalDate)
    await fetchData()
    ElMessage.success('宸叉洿鏂板揩鐓?)
  } finally {
    refreshing.value = false
  }
}

watch(
  () => route.query,
  async () => {
    if (String(route.query?.add_hold || '') === '1' && focusCode.value) {
      openAddHoldingDialog({ code: focusCode.value, name: focusName.value })
    }
  },
  { deep: true }
)

watch(
  () => addForm.value.code,
  (val) => {
    const normalized = normalizeCode(val)
    if (normalized !== val) addForm.value.code = normalized
    if (codeLookupTimer) clearTimeout(codeLookupTimer)
    codeLookupTimer = setTimeout(() => {
      resolveStockByCode(normalized)
    }, 220)
  }
)
watch(
  () => [capitalForm.value.synced_total_capital, capitalForm.value.synced_available_cash, capitalForm.value.synced_market_value, capitalForm.value.synced_cost_value, capitalForm.value.synced_at],
  () => {
    saveCapitalProfile()
  }
)

watch(
  () => [historyTradeGroups.value.length, historyPageSize.value],
  () => {
    const pageSize = Math.max(1, Number(historyPageSize.value) || 20)
    const maxPage = Math.max(1, Math.ceil(historyTradeGroups.value.length / pageSize))
    if (historyPage.value > maxPage) historyPage.value = maxPage
    if (historyPage.value < 1) historyPage.value = 1
  }
)

onMounted(async () => {
  loadCapitalProfile()
  loadManualHoldings()
  loadManualTradeLogs()
  applyTrailingTakeProfitState()
  await syncManualHoldingsStateToServer()
  await repairExistingManualHoldings()
  await fetchData()
  mergeMarketStats()
  await refreshAllHoldingData({ silent: true, includePool: true })
  await refreshManualPnlByDetail()
  await pullMonitorStatus()
  await pullGen2ShadowMonitorStatus()
  await fetchPtradeBridgeState()
  startAutoRefreshTimer()
  if (String(route.query?.add_hold || '') === '1' && focusCode.value) {
    openAddHoldingDialog({ code: focusCode.value, name: focusName.value })
  } else {
    await refreshManualSignals()
  }
})

onBeforeUnmount(() => {
  stopAutoRefreshTimer()
  if (codeLookupTimer) clearTimeout(codeLookupTimer)
})
</script>

<style scoped>
.page { padding: 16px; background: #f5f7ff; min-height: calc(100vh - 98px); }
.head-card,.panel { background:#fff; border:1px solid #e6ebff; border-radius:12px; }
.head-card { padding:16px; margin-bottom:14px; display:flex; justify-content:space-between; align-items:center; gap:12px; }
.head-card h1 { margin:0 0 6px; font-size:24px; color:#1f2a44; }
.head-card p { margin:0; color:#66708b; font-size:13px; }
.actions { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.decision-strip { display:grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }
.decision-card { background:#fff; border:1px solid #e6ebff; border-left:4px solid #8c9ab8; border-radius:8px; padding:12px 14px; min-height:94px; display:flex; flex-direction:column; gap:6px; color:#24355d; }
.decision-card.pass { border-left-color:#389e0d; }
.decision-card.block { border-left-color:#fa8c16; }
.decision-card.neutral { border-left-color:#597ef7; }
.decision-label { font-size:12px; color:#66708b; }
.decision-card strong { font-size:22px; line-height:1.2; }
.decision-card small { color:#66708b; line-height:1.45; overflow:hidden; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; }
.execution-panel { border-color:#d6e4ff; }
.workflow-panel { border-color:#d9f7be; }
.workflow-grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap:10px; margin-top:8px; }
.workflow-step { border:1px solid #e6ebff; border-left:4px solid #8c9ab8; border-radius:8px; padding:10px; background:#fbfcff; min-height:72px; }
.workflow-step.ok { border-left-color:#52c41a; }
.workflow-step.blocked { border-left-color:#ff4d4f; background:#fff7f7; }
.workflow-step-head { display:flex; align-items:center; justify-content:space-between; gap:8px; font-weight:700; color:#24355d; }
.workflow-step-meta { margin-top:8px; color:#66708b; font-size:12px; }
.workflow-table { margin-top:12px; }
.risk-panel { border-color:#ffe7ba; }
.capital-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px 14px; }
.capital-item { border: 1px solid #e6ebff; border-radius: 10px; padding: 10px 12px; background: #fafbff; display: flex; flex-direction: column; gap: 6px; color: #24355d; }
.capital-item label { font-size: 12px; color: #66708b; }
.capital-item.readonly div { font-weight: 600; }
.capital-fixed { min-height: 32px; display: flex; align-items: center; padding: 0 2px; font-weight: 600; color: #24355d; }
.holding-toolbar { margin-bottom: 10px; display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
.holding-list-header { margin-bottom: 8px; }
.current-holding-table :deep(.el-table__cell) { padding: 7px 0; }
.current-holding-table :deep(.cell) { padding: 0 8px; line-height: 1.45; }
.stop-loss-price { color: #cf1322; font-weight: 600; }
.rsi-cell { display: flex; align-items: center; gap: 6px; }
.op-cell { display: flex; align-items: center; gap: 8px; white-space: nowrap; }
.op-cell :deep(.el-button) { margin-left: 0; }
.panel { padding:14px; margin-bottom:14px; }
.panel-title { font-size:16px; font-weight:700; color:#24355d; margin-bottom:12px; }
.panel-alert { margin-bottom:14px; }
.sub-panel { border: 1px solid #edf1ff; border-radius: 8px; padding: 10px; background: #fbfcff; }
.sub-panel-header { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 8px; }
.sub-panel-title { font-size: 14px; font-weight: 700; color: #24355d; }
.toolbar-note { color: #66708b; font-size: 13px; line-height: 1.6; }
.warning-note { color: #ad6800; }
.verification-queue { margin-top: 12px; border-top: 1px solid #edf1f7; padding-top: 10px; }
.history-pagination { margin-top: 12px; display: flex; justify-content: flex-end; }
@media (max-width: 1200px) {
  .decision-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 720px) {
  .head-card { align-items:flex-start; flex-direction:column; }
  .decision-strip { grid-template-columns: 1fr; }
}
</style>
