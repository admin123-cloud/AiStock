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
        <el-button type="primary" :loading="loading" @click="fetchData">查询</el-button>
        <el-button :loading="refreshing" @click="refreshSnapshot">刷新快照</el-button>
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

      <div v-if="false" class="decision-strip">
        <div class="decision-card" :class="marketGate?.can_open ? 'pass' : 'block'">
          <span class="decision-label">开仓门槛</span>
          <strong>{{ marketGate?.can_open ? '允许新增' : '禁止新增' }}</strong>
          <small>{{ marketGateSummary }}</small>
        </div>
        <div class="decision-card" :class="actionableBuyRows.length ? 'pass' : 'neutral'">
          <span class="decision-label">正式买点</span>
          <strong>{{ actionableBuyRows.length }} 只</strong>
          <small>{{ actionableBuyRows.length ? '可进入交易规则检查' : '当前没有正式可买入股票' }}</small>
        </div>
        <div class="decision-card" :class="sellPriorityRows.length ? 'block' : 'pass'">
          <span class="decision-label">持仓风控</span>
          <strong>{{ sellPriorityRows.length }} 项</strong>
          <small>{{ sellPriorityRows.length ? '优先处理卖出、减仓风险' : '暂无硬风控动作' }}</small>
        </div>
        <div class="decision-card neutral">
          <span class="decision-label">自动探测</span>
          <strong>{{ gen2ShadowMonitorStatus?.enabled ? '运行中' : '未开启' }}</strong>
          <small>{{ gen2ShadowMonitorResultText || gen2ShadowMonitorText }}</small>
        </div>
      </div>

      <div class="panel trade-ticket-panel">
        <div class="sub-panel-header">
          <div>
            <div class="panel-title">今日行动工作台</div>
            <div class="toolbar-note">G2 V4 交易单、开仓门槛、持仓风控、执行回填合并在这里处理。</div>
            <div v-if="dailyTradeTicketOutputs" class="toolbar-note">审计文件：{{ dailyTradeTicketOutputs }}</div>
          </div>
          <div class="actions">
            <el-tag :type="dailyTradeTicket?.can_open ? 'success' : 'warning'" effect="light">
              {{ dailyTradeTicket?.can_open ? '允许开仓' : '禁止新仓' }}
            </el-tag>
            <el-tag :type="sellPriorityRows.length ? 'danger' : 'success'" effect="light">
              {{ sellPriorityRows.length ? '先处理持仓风险' : '持仓风控正常' }}
            </el-tag>
            <el-tag type="info" effect="light">{{ dailyTradeTicket?.mode || 'shadow_only' }}</el-tag>
            <el-button size="small" type="primary" plain :loading="gen2ShadowMonitorLoading" @click="runGen2ShadowMonitorOnce">探测买点</el-button>
            <el-button size="small" :loading="allRefreshLoading || marketGateLoading || gen2ShadowLoading || holdingRefreshLoading" @click="refreshAllHoldingData">刷新交易状态</el-button>
            <el-button size="small" :loading="dailyTradeTicketLoading" @click="fetchDailyTradeTicket(selectedDate)">刷新交易单</el-button>
          </div>
        </div>

        <div class="ticket-grid">
          <div class="ticket-metric">
            <label>信号日</label>
            <strong>{{ dailyTradeTicket?.signal_date || '--' }}</strong>
          </div>
          <div class="ticket-metric" :class="dailyTradeTicket?.can_open ? 'ok' : 'warn'">
            <label>市场状态</label>
            <strong>{{ dailyTradeTicket?.market_state || '--' }}</strong>
          </div>
          <div class="ticket-metric">
            <label>允许仓位</label>
            <strong>{{ dailyTradeTicket?.target_exposure || '--' }}</strong>
          </div>
          <div class="ticket-metric">
            <label>正式候选</label>
            <strong>{{ dailyTradeTicketFormalRows.length }} / {{ dailyTradeTicket?.max_new_positions ?? '--' }}</strong>
          </div>
          <div class="ticket-metric" :class="sellPriorityRows.length ? 'warn' : 'ok'">
            <label>持仓风控</label>
            <strong>{{ sellPriorityRows.length }} 项</strong>
          </div>
          <div class="ticket-metric">
            <label>自动探测</label>
            <strong>{{ gen2ShadowMonitorStatus?.enabled ? '运行中' : '未开启' }}</strong>
          </div>
        </div>

        <el-alert
          v-if="dailyTradeTicket?.message"
          :type="dailyTradeTicket?.can_open ? 'success' : 'warning'"
          :closable="false"
          :title="dailyTradeTicket.message"
          class="panel-alert"
        />

        <el-table
          :data="dailyTradeTicketFormalRows"
          stripe
          size="small"
          empty-text="今日交易单没有正式买入候选"
        >
          <el-table-column label="代码" width="110">
            <template #default="{ row }">
              <el-button type="primary" link @click="copyPlainCode(row.code)">{{ displayCode(row.code) }}</el-button>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="名称" min-width="110" show-overflow-tooltip />
          <el-table-column prop="strategy_source" label="来源" width="150" show-overflow-tooltip />
          <el-table-column prop="buy_area" label="买入区间" min-width="220" show-overflow-tooltip />
          <el-table-column prop="suggested_position" label="建议仓位" width="96" />
          <el-table-column prop="confirm_datetime" label="确认时间" width="150" show-overflow-tooltip />
          <el-table-column label="V4证据" width="130">
            <template #default="{ row }">
              rank {{ row.evidence?.v4_rank || '--' }} / {{ formatScore(row.evidence?.v4_score) }}
            </template>
          </el-table-column>
          <el-table-column label="风控" min-width="240" show-overflow-tooltip>
            <template #default="{ row }">{{ (row.risk_rules || []).join('；') }}</template>
          </el-table-column>
        </el-table>

        <div class="ticket-discipline">
          <el-tag
            v-for="item in dailyTradeTicketForbiddenActions"
            :key="item"
            type="danger"
            effect="plain"
          >
            {{ item }}
          </el-tag>
        </div>

        <div class="daily-execution-box">
          <div class="sub-panel-header">
            <div>
              <div class="sub-panel-title">执行与复盘记录</div>
              <div class="toolbar-note">{{ dailyExecutionSummary }}</div>
            </div>
            <div class="actions">
              <el-tag :type="dailyExecutionForm.discipline_ok ? 'success' : 'danger'" effect="light">
                {{ dailyExecutionForm.discipline_ok ? '纪律正常' : '纪律问题' }}
              </el-tag>
              <el-button size="small" :loading="dailyExecutionSaving" @click="saveDailyExecution('no_trade')">今日无交易</el-button>
              <el-button size="small" :loading="dailyExecutionSaving" @click="inferDailyExecutionFromLocalTrades">从成交归因</el-button>
              <el-button size="small" type="primary" plain :loading="dailyExecutionSaving" @click="saveDailyExecution('followed')">已照单执行</el-button>
              <el-button size="small" type="danger" plain :loading="dailyExecutionSaving" @click="saveDailyExecution('violated')">标记违规</el-button>
            </div>
          </div>
          <el-form class="daily-execution-form" label-position="top" size="small">
            <el-row :gutter="10">
              <el-col :xs="24" :sm="8">
                <el-form-item label="执行状态">
                  <el-select v-model="dailyExecutionForm.execution_status" placeholder="选择状态">
                    <el-option label="待记录" value="pending" />
                    <el-option label="照单执行" value="followed" />
                    <el-option label="无交易" value="no_trade" />
                    <el-option label="部分执行" value="partial" />
                    <el-option label="纪律违规" value="violated" />
                    <el-option label="已复盘" value="reviewed" />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :xs="24" :sm="8">
                <el-form-item label="是否有实际执行">
                  <el-switch v-model="dailyExecutionForm.executed" active-text="有执行" inactive-text="无执行" />
                </el-form-item>
              </el-col>
              <el-col :xs="24" :sm="8">
                <el-form-item label="是否遵守交易单">
                  <el-switch v-model="dailyExecutionForm.discipline_ok" active-text="遵守" inactive-text="违规" />
                </el-form-item>
              </el-col>
            </el-row>
            <el-form-item label="执行备注">
              <el-input v-model="dailyExecutionForm.execution_note" type="textarea" :rows="2" placeholder="记录是否按交易单执行、是否有临时冲动、是否错过买点" />
            </el-form-item>
            <el-row :gutter="10">
              <el-col :xs="24" :sm="8">
                <el-form-item label="T+1 复盘">
                  <el-input v-model="dailyExecutionForm.t1_review" type="textarea" :rows="2" placeholder="次日表现、是否符合预期" />
                </el-form-item>
              </el-col>
              <el-col :xs="24" :sm="8">
                <el-form-item label="T+3 复盘">
                  <el-input v-model="dailyExecutionForm.t3_review" type="textarea" :rows="2" placeholder="三日是否走强，是否需要降级" />
                </el-form-item>
              </el-col>
              <el-col :xs="24" :sm="8">
                <el-form-item label="T+5 复盘">
                  <el-input v-model="dailyExecutionForm.t5_review" type="textarea" :rows="2" placeholder="盈亏、买点质量和纪律归因" />
                </el-form-item>
              </el-col>
            </el-row>
            <div class="actions">
              <el-button type="primary" :loading="dailyExecutionSaving" @click="saveDailyExecution()">保存执行与复盘记录</el-button>
            </div>
          </el-form>
        </div>
      </div>

      <div class="panel strategy-refresh-panel">
        <div class="sub-panel-header">
          <div>
            <div class="panel-title">G2 30m 策略自动刷新</div>
            <div class="toolbar-note">{{ gen2StrategyRefreshSummary }}</div>
          </div>
          <div class="actions">
            <el-tag :type="gen2StrategyRefreshStatus?.enabled ? 'success' : 'info'" effect="light">
              {{ gen2StrategyRefreshStatus?.enabled ? '半小时自动刷新已开启' : '半小时自动刷新未开启' }}
            </el-tag>
            <el-button size="small" :loading="gen2StrategyRefreshLoading" @click="fetchGen2StrategyRefreshStatus">刷新状态</el-button>
            <el-button size="small" type="primary" plain :loading="gen2StrategyRefreshRunning" @click="runGen2StrategyRefreshOnce">立即执行一次</el-button>
          </div>
        </div>
        <div class="refresh-grid">
          <div class="refresh-item">
            <label>当前30m窗口</label>
            <strong>{{ gen2StrategyRefreshStatus?.current_bar_slot || '--' }}</strong>
          </div>
          <div class="refresh-item">
            <label>下次自动刷新</label>
            <strong>{{ gen2StrategyRefreshStatus?.next_run_time || '--' }}</strong>
          </div>
          <div class="refresh-item">
            <label>上次刷新</label>
            <strong>{{ gen2StrategyRefreshStatus?.last_success_at || gen2StrategyRefreshStatus?.last_run_at || '--' }}</strong>
          </div>
          <div class="refresh-item">
            <label>上次结果</label>
            <strong>{{ gen2StrategyRefreshLastResultText }}</strong>
          </div>
        </div>
      </div>

      <div class="panel workflow-panel">
        <div class="sub-panel-header">
          <div>
            <div class="panel-title">策略工作流监控</div>
            <div class="toolbar-note">{{ workflowSummaryText }}</div>
          </div>
          <div class="actions">
            <el-tag :type="workflowOk ? 'success' : 'danger'" effect="light">
              {{ workflowOk ? '全链路正常' : '存在阻塞' }}
            </el-tag>
            <el-tag v-if="notificationStageExists" :type="notificationStageOk ? 'success' : 'warning'" effect="light" size="small">
              {{ notificationStageLabel }}
            </el-tag>
            <el-button size="small" :loading="workflowLoading" @click="fetchWorkflowStatus(selectedDate)">刷新工作流</el-button>
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
                {{ stage.ok ? '通过' : `${stage.failed_count || stage.failed?.length || 0}项阻塞` }}
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
          empty-text="暂无阻塞项"
        >
          <el-table-column label="层级" width="120">
            <template #default="{ row }">{{ workflowStageLabel(row.stage || row.type) }}</template>
          </el-table-column>
          <el-table-column prop="name" label="检查项" width="190" show-overflow-tooltip />
          <el-table-column prop="message" label="阻塞说明" min-width="360" show-overflow-tooltip />
          <el-table-column prop="trade_date" label="日期" width="112" />
          <el-table-column label="结果" width="120">
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
          <el-table-column label="链路层" width="110">
            <template #default="{ row }">{{ workflowStageLabel(row.stage) }}</template>
          </el-table-column>
          <el-table-column prop="label" label="真源/产物" width="170" show-overflow-tooltip />
          <el-table-column prop="source_type" label="类型" width="90" />
          <el-table-column prop="storage" label="存储位置" min-width="260" show-overflow-tooltip />
          <el-table-column prop="official_reader" label="官方读取路径" min-width="220" show-overflow-tooltip />
          <el-table-column label="读取结果" width="120">
            <template #default="{ row }">{{ workflowLineageResult(row) }}</template>
          </el-table-column>
        </el-table>
      </div>

      <div v-if="false" class="panel execution-panel">
        <div class="sub-panel-header">
          <div>
            <div class="panel-title">今日正式买入执行区</div>
            <div class="toolbar-note">这里只显示已经通过策略状态、盘中触发和风控候选状态的可买入标的；影子样本不会进入这里。</div>
            <div class="toolbar-note">{{ ptradeBridgeSummary }}</div>
            <div class="toolbar-note">{{ ptradeBridgeQueueText }}</div>
            <div class="toolbar-note">{{ ptradeBridgeReadinessText }}</div>
          </div>
          <div class="actions">
            <el-button type="primary" :loading="gen2ShadowMonitorLoading" @click="runGen2ShadowMonitorOnce">立即探测买点</el-button>
            <el-button :loading="allRefreshLoading || marketGateLoading || gen2ShadowLoading || holdingRefreshLoading" @click="refreshAllHoldingData">刷新交易状态</el-button>
          </div>
        </div>
        <el-table :data="actionableBuyRows" stripe size="small" empty-text="当前没有正式可买入股票">
          <el-table-column label="代码" width="110">
            <template #default="{ row }">
              <el-button type="primary" link @click="copyPlainCode(row.code)">{{ normalizeCode(row.code) || '--' }}</el-button>
            </template>
          </el-table-column>
          <el-table-column label="名称" min-width="120">
            <template #default="{ row }">
              <el-button type="primary" link @click="openStockDetail(row)">{{ row.name || '--' }}</el-button>
            </template>
          </el-table-column>
          <el-table-column label="确认时间" width="150" show-overflow-tooltip>
            <template #default="{ row }">{{ row.confirm_datetime || '--' }}</template>
          </el-table-column>
          <el-table-column label="入场价" width="90">
            <template #default="{ row }">{{ formatPrice(row.entry_price) }}</template>
          </el-table-column>
          <el-table-column label="V4排名" width="86">
            <template #default="{ row }">{{ row.v4_rank || '--' }}</template>
          </el-table-column>
          <el-table-column label="Alpha191" width="128">
            <template #default="{ row }">{{ formatScore(row.alpha191_volume5_score ?? row.alpha191_gate_score) }}</template>
          </el-table-column>
          <el-table-column label="涨幅/量比" width="116">
            <template #default="{ row }">{{ formatPct(row.rt_return_pct) }} / {{ formatRatio(row.amount_ratio) }}</template>
          </el-table-column>
          <el-table-column prop="reason_text" label="触发说明" min-width="260" show-overflow-tooltip />
          <el-table-column label="操作" width="180" fixed="right">
            <template #default="{ row }">
              <el-button type="primary" link @click="openAddHoldingDialog(row, 'new')">规则检查</el-button>
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
            <div class="panel-title">持仓风控与卖出优先级</div>
            <div class="toolbar-note">实盘先处理已有仓位风险，再考虑新增开仓；这里汇总硬止损、移动止盈、评分池和分钟卖点。</div>
          </div>
          <el-button :loading="holdingRefreshLoading || signalLoading" @click="refreshCurrentHoldings">刷新持仓风控</el-button>
        </div>
        <el-table :data="sellPriorityRows" stripe size="small" empty-text="暂无相关持仓标记">
          <el-table-column label="代码" width="110">
            <template #default="{ row }">
              <el-button type="primary" link @click="openStockDetail(row)">{{ displayCode(row.code) }}</el-button>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="名称" min-width="120" />
          <el-table-column label="收益率" width="90">
            <template #default="{ row }">
              <span :style="aSharePnlStyle(row.pnl_ratio)">{{ formatPct(row.pnl_ratio) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="risk_text" label="风险原因" min-width="260" show-overflow-tooltip />
          <el-table-column prop="signal.suggestion" label="交易建议" min-width="140" show-overflow-tooltip />
          <el-table-column label="操作" width="96" fixed="right">
            <template #default="{ row }">
              <el-button type="warning" link @click="openSellDialog(row, row.holding_index)">卖出</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
      <div class="panel">
        <div class="panel-title">资金股票总览</div>
        <div class="capital-grid">
          <div class="capital-item">
            <label>初始总资金</label>
            <div class="capital-fixed">{{ formatMoney(FIXED_BASE_CAPITAL) }}</div>
          </div>
          <div class="capital-item readonly">
            <label>总资金（系统维护）</label>
            <div>{{ formatMoney(capitalSnapshot.total_capital) }}</div>
          </div>
          <div class="capital-item readonly">
            <label>可用资金/可用冻结</label>
            <div>{{ formatMoney(capitalSnapshot.available_cash) }}</div>
          </div>
          <div class="capital-item readonly">
            <label>持仓市值</label>
            <div>{{ formatMoney(capitalSnapshot.market_value) }}</div>
          </div>
          <div class="capital-item readonly">
            <label>持仓占比</label>
            <div :style="{ color: capitalSnapshot.position_ratio > 80 ? '#d4380d' : '#24355d' }">{{ formatPct(capitalSnapshot.position_ratio) }}</div>
          </div>
          <div class="capital-item readonly">
            <label>持仓成本</label>
            <div>{{ formatMoney(capitalSnapshot.cost_value) }}</div>
          </div>
          <div class="capital-item readonly">
            <label>已实现盈亏</label>
            <div :style="aSharePnlStyle(capitalSnapshot.realized_pnl)">
              {{ formatMoney(capitalSnapshot.realized_pnl) }}
            </div>
          </div>
          <div class="capital-item readonly">
            <label>当日盈亏</label>
            <div :style="{ color: capitalSnapshot.pnl_amount > 0 ? '#d4380d' : (capitalSnapshot.pnl_amount < 0 ? '#389e0d' : '#24355d') }">
              {{ formatMoney(capitalSnapshot.pnl_amount) }}（{{ formatPct(capitalSnapshot.pnl_ratio) }}）
            </div>
          </div>
          <div class="capital-item readonly">
            <label>当日盈亏</label>
            <div :style="aSharePnlStyle(capitalSnapshot.day_pnl)">
              {{ formatMoney(capitalSnapshot.day_pnl) }}
            </div>
          </div>
          <div class="capital-item readonly">
            <label>大盘开仓阈值</label>
            <div :style="{ color: marketGate?.can_open ? '#d4380d' : '#389e0d' }">
              {{ marketGate?.can_open ? '允许新增' : '禁止新增' }}
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
        <div v-if="capitalForm.synced_at" class="toolbar-note">同花顺资金持股最近同步：{{ capitalForm.synced_at }}</div>
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
            <el-button :loading="thsCapitalSyncLoading" @click="syncCapitalAndHoldingsFromThs">同步同花顺资金持股</el-button>
            <el-button type="primary" @click="openAddHoldingDialog(null, 'new')">新增持仓</el-button>
          </div>
          <div class="actions">
            <el-switch v-model="monitorTradingHoursOnly" active-text="仅交易时段" />
            <el-input-number v-model="monitorQuietMinutes" :min="0" :step="5" style="width: 150px" />
            <el-button type="success" :plain="monitorEnabled" :loading="monitorLoading" @click="toggleMonitor">
              {{ monitorEnabled ? '停止1分钟跟踪' : '开启1分钟跟踪' }}
            </el-button>
            <el-button :loading="monitorLoading" @click="runMonitorNow">测试通知</el-button>
            <el-button :loading="allRefreshLoading || marketGateLoading || buyPoolLoading || gen2ShadowLoading || signalLoading" @click="refreshAllHoldingData">一键刷新全部</el-button>
          </div>
        </div>
        <div class="toolbar-note">分钟跟踪状态：{{ monitorStatusText }}</div>
        <div class="toolbar-note">盘中自动刷新：{{ autoRefreshStatusText }}</div>
        <div class="toolbar-note">通知邮箱统一取自系统配置，不再在实盘交易页单独维护。</div>
        <div class="toolbar-note">新增持仓用于按策略计划新建跟踪仓位；已实际发生的持仓请优先通过同花顺资金持股同步。</div>
        <div class="toolbar-note">当前合并了总资金、当前实盘持仓与持仓纪律，便于在一个面板里看清资金和股票。</div>
        <div style="height: 10px" />
        <div v-if="!isGen2LiveMode" class="sub-panel">
          <div class="sub-panel-header">
            <div>
              <div class="sub-panel-title">{{ buyPoolPanelTitle }}</div>
              <div class="toolbar-note">{{ buyPoolSummary }}</div>
            </div>
            <el-button size="small" :loading="buyPoolLoading" @click="fetchBuyPool(selectedDate)">刷新候选</el-button>
          </div>
          <el-table :data="buyPoolRows" stripe size="small" empty-text="暂无买入观察池候选">
            <el-table-column label="池排名" width="76">
              <template #default="{ row }">{{ row.pool_rank || '--' }}</template>
            </el-table-column>
            <el-table-column label="代码" width="110">
              <template #default="{ row }">
                <el-button type="primary" link @click="copyPlainCode(row.code)">{{ normalizeCode(row.code) || '--' }}</el-button>
              </template>
            </el-table-column>
            <el-table-column label="名称" min-width="110">
              <template #default="{ row }">
                <el-button type="primary" link @click="openStockDetail(row)">{{ row.name || '--' }}</el-button>
              </template>
            </el-table-column>
            <el-table-column label="V4排名" width="86">
              <template #default="{ row }">{{ row.rank || '--' }}</template>
            </el-table-column>
            <el-table-column label="V4分数" width="90">
              <template #default="{ row }">{{ formatScore(row.score_total) }}</template>
            </el-table-column>
            <el-table-column label="买点类型" min-width="132" show-overflow-tooltip>
              <template #default="{ row }">{{ row.setup_label || '--' }}</template>
            </el-table-column>
            <el-table-column label="5日动量" width="96">
              <template #default="{ row }">{{ formatPct(row.mom5_pct) }}</template>
            </el-table-column>
            <el-table-column label="10日动量" width="96">
              <template #default="{ row }">{{ formatPct(row.mom10_pct) }}</template>
            </el-table-column>
            <el-table-column label="缩量" width="86">
              <template #default="{ row }">{{ formatRatio(row.shrink_2d_ratio || row.shrink_1d_ratio) }}</template>
            </el-table-column>
            <el-table-column label="板块" min-width="116" show-overflow-tooltip>
              <template #default="{ row }">{{ row.sector_name || '--' }}</template>
            </el-table-column>
            <el-table-column label="板块评分" width="96">
              <template #default="{ row }">{{ formatScore(row.sector_score) }}</template>
            </el-table-column>
            <el-table-column label="状态" width="92">
              <template #default="{ row }">
                <el-tag :type="row.can_buy ? 'success' : (row.candidate_status === 'primary' ? 'warning' : 'info')" effect="light">
                  {{ buyPoolStatusText(row) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="reason_text" label="入池原因" min-width="260" show-overflow-tooltip />
            <el-table-column label="操作" width="138" fixed="right">
              <template #default="{ row }">
                <el-button type="primary" link @click="openAddHoldingDialog(row, 'new')">新增持仓</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div v-if="gen2VerificationQueueRows.length" class="verification-queue">
            <div class="sub-panel-title">Alpha191 验证待办</div>
            <div class="toolbar-note">最近已有跟踪结果、尚未打标的全局影子样本，优先从这里开始复盘。</div>
            <el-table :data="gen2VerificationQueueRows" stripe size="small" empty-text="暂无待验证样本">
              <el-table-column label="信号日" width="104">
                <template #default="{ row }">{{ row.entry_date || '--' }}</template>
              </el-table-column>
              <el-table-column label="代码" width="100">
                <template #default="{ row }">
                  <el-button type="primary" link @click="copyPlainCode(row.code)">{{ normalizeCode(row.code) || '--' }}</el-button>
                </template>
              </el-table-column>
              <el-table-column label="名称" min-width="110">
                <template #default="{ row }">
                  <el-button type="primary" link @click="openStockDetail(row)">{{ row.name || '--' }}</el-button>
                </template>
              </el-table-column>
              <el-table-column label="确认时间" width="150" show-overflow-tooltip>
                <template #default="{ row }">{{ row.confirm_datetime || '--' }}</template>
              </el-table-column>
              <el-table-column label="Alpha分" width="86">
                <template #default="{ row }">{{ formatScore(row.alpha191_gate_score) }}</template>
              </el-table-column>
              <el-table-column label="5/10/20日" width="142">
                <template #default="{ row }">{{ formatPct(row.fwd5_pct) }} / {{ formatPct(row.fwd10_pct) }} / {{ formatPct(row.fwd20_pct) }}</template>
              </el-table-column>
              <el-table-column label="止损" width="74">
                <template #default="{ row }">
                  <el-tag :type="row.stop5_touch_30m ? 'danger' : 'success'" effect="light" size="small">
                    {{ row.stop5_touch_30m ? '触发' : '未触发' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="92" fixed="right">
                <template #default="{ row }">
                  <el-button type="primary" link @click="openGen2VerificationDialog(row)">验证</el-button>
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
              <div class="toolbar-note">{{ gen2ShadowSummaryDisplay }}</div>
              <div class="toolbar-note warning-note">此区是影子跟踪/复盘池，不是待买入清单；只有状态明确为“可买入”的行才允许新增持仓。</div>
              <div class="toolbar-note">{{ gen2VerificationSummaryText }}</div>
              <div class="toolbar-note">
                升级提示：
                <el-tag :type="gen2VerificationPromotionType" effect="light" size="small">{{ gen2VerificationPromotionLabel }}</el-tag>
                {{ gen2VerificationPromotionMessage }}
              </div>
              <div v-if="gen2ShadowFreshnessNote" class="toolbar-note warning-note">{{ gen2ShadowFreshnessNote }}</div>
              <div v-if="gen2ShadowUpdateText" class="toolbar-note">{{ gen2ShadowUpdateText }}</div>
              <div class="toolbar-note">{{ gen2ShadowMonitorText }}</div>
              <div v-if="gen2ShadowMonitorResultText" class="toolbar-note">{{ gen2ShadowMonitorResultText }}</div>
            </div>
            <div class="actions">
              <el-tag v-if="isGen2LiveMode" type="info" effect="light">Alpha191 volume5 影子跟踪/复盘池</el-tag>
              <el-select v-else v-model="gen2Alpha191Gate" size="small" style="width: 230px">
                <el-option label="Alpha191 off" value="off" />
                <el-option label="Alpha191 volume5 影子跟踪/复盘池" value="volume5_keep80_runup" />
              </el-select>
              <el-button size="small" :loading="gen2ShadowLoading" @click="fetchGen2RiskCoolShadow(selectedDate)">刷新候选</el-button>
              <el-button size="small" type="primary" :loading="gen2ShadowUpdateLoading" @click="updateGen2RiskCoolShadow(selectedDate)">更新候选快照</el-button>
              <el-button size="small" :loading="gen2ShadowMonitorLoading" @click="toggleGen2ShadowMonitor">
                {{ gen2ShadowMonitorStatus?.enabled ? '停止15分钟探测' : '开启15分钟探测' }}
              </el-button>
              <el-button size="small" type="warning" plain :loading="gen2ShadowMonitorLoading" @click="runGen2ShadowMonitorOnce">立即探测邮件</el-button>
            </div>
          </div>
          <el-table :data="shadowReviewRows" stripe size="small" empty-text="暂无 G2 影子观察候选">
            <el-table-column label="状态" width="96">
              <template #default="{ row }">
                <el-tag :type="gen2ShadowTagType(row)" effect="light">{{ gen2ShadowStatusText(row) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="实盘验证" width="104">
              <template #default="{ row }">
                <el-tag :type="gen2VerificationTagType(row)" effect="light">{{ gen2VerificationText(row) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="代码" width="110">
              <template #default="{ row }">
                <el-button type="primary" link @click="copyPlainCode(row.code)">{{ normalizeCode(row.code) || '--' }}</el-button>
              </template>
            </el-table-column>
            <el-table-column label="名称" min-width="110">
              <template #default="{ row }">
                <el-button type="primary" link @click="openStockDetail(row)">{{ row.name || '--' }}</el-button>
              </template>
            </el-table-column>
            <el-table-column label="影子排名" width="86">
              <template #default="{ row }">{{ row.day_signal_rank || '--' }}</template>
            </el-table-column>
            <el-table-column label="确认时间" width="150" show-overflow-tooltip>
              <template #default="{ row }">{{ row.confirm_datetime || '--' }}</template>
            </el-table-column>
            <el-table-column label="入场价" width="86">
              <template #default="{ row }">{{ formatPrice(row.entry_price) }}</template>
            </el-table-column>
            <el-table-column label="V4排名" width="86">
              <template #default="{ row }">{{ row.v4_rank || '--' }}</template>
            </el-table-column>
            <el-table-column label="Alpha191" width="150">
              <template #default="{ row }">
                {{ formatScore(row.alpha191_volume5_score ?? row.alpha191_gate_score) }} / {{ row.alpha191_volume5_rank_in_day || row.alpha191_original_v4_rank || '--' }}
              </template>
            </el-table-column>
            <el-table-column label="涨幅/压力" width="116">
              <template #default="{ row }">{{ formatPct(row.runup_from_60d_low) }} / {{ formatPct(row.overhead_pressure_amount_share) }}</template>
            </el-table-column>
            <el-table-column label="涨幅/量比" width="116">
              <template #default="{ row }">{{ formatPct(row.rt_return_pct) }} / {{ formatRatio(row.amount_ratio) }}</template>
            </el-table-column>
            <el-table-column label="5/10/20日跟踪" width="142">
              <template #default="{ row }">{{ formatPct(row.fwd5_pct) }} / {{ formatPct(row.fwd10_pct) }} / {{ formatPct(row.fwd20_pct) }}</template>
            </el-table-column>
            <el-table-column label="止损" width="74">
              <template #default="{ row }">
                <el-tag :type="row.stop5_touch_30m ? 'danger' : 'success'" effect="light" size="small">
                  {{ row.stop5_touch_30m ? '触发' : '未触发' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="reason_text" label="理由" min-width="240" show-overflow-tooltip />
            <el-table-column label="验证说明" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">{{ gen2VerificationNoteText(row) }}</template>
            </el-table-column>
              <el-table-column label="操作" width="176" fixed="right">
              <template #default="{ row }">
                <el-button type="primary" link @click="openGen2VerificationDialog(row)">验证</el-button>
                <el-button
                  type="primary"
                  link
                  :disabled="!gen2ShadowCanAddHolding(row)"
                  @click="openAddHoldingDialog(row, 'new')"
                >
                  {{ gen2ShadowCanAddHolding(row) ? '新增持仓' : '仅复盘' }}
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
        <div style="height: 12px" />
        <div class="sub-panel-header holding-list-header">
          <div>
            <div class="sub-panel-title">当前实盘持仓</div>
            <div class="toolbar-note">一键更新股价、盈亏金额、V4 排名、评分池状态和卖点信号。</div>
          </div>
          <el-button
            type="primary"
            size="small"
            :loading="holdingRefreshLoading || allRefreshLoading"
            @click="refreshCurrentHoldings"
          >
            一键更新持仓
          </el-button>
        </div>
        <el-table :data="currentHoldingRows" class="current-holding-table" stripe size="small" :fit="false" empty-text="暂无当前实盘持仓">
          <el-table-column label="代码" width="100">
            <template #default="{ row }">
              <el-button type="primary" link @click="openStockDetail(row)">{{ displayCode(row.code) }}</el-button>
            </template>
          </el-table-column>
          <el-table-column label="名称" width="110" show-overflow-tooltip>
            <template #default="{ row }">
              <el-button type="primary" link @click="openStockDetail(row)">{{ row.name || '--' }}</el-button>
            </template>
          </el-table-column>
          <el-table-column label="V4评分/排名" width="112">
            <template #default="{ row }">
              {{ v4SourceFor(row).rank_text }} / {{ v4SourceFor(row).score_text }}
            </template>
          </el-table-column>
          <el-table-column label="是否在池" width="86">
            <template #default="{ row }">
              <el-tag :type="v4SourceFor(row).in_pool ? 'success' : 'danger'" effect="light" size="small">
                {{ v4SourceFor(row).in_pool ? '在池' : '不在' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="shares" label="数量" width="74" />
          <el-table-column prop="cost_price" label="成本" width="80" />
          <el-table-column prop="current_price" label="当前价" width="80">
            <template #default="{ row }">{{ formatPrice(row.current_price) }}</template>
          </el-table-column>
          <el-table-column label="市值" width="102">
            <template #default="{ row }">{{ formatMoney(holdingMarketValue(row)) }}</template>
          </el-table-column>
          <el-table-column label="浮动盈亏" width="102">
            <template #default="{ row }">
              <span :style="aSharePnlStyle(floatingPnlAmount(row))">
                {{ formatMoney(floatingPnlAmount(row)) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="当日涨跌" width="92">
            <template #default="{ row }">
              <span :style="aSharePnlStyle(dayChangePct(row))">
                {{ formatSignedPct(dayChangePct(row)) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column prop="pnl_ratio" label="盈亏%" width="82">
            <template #default="{ row }">
              <span :style="aSharePnlStyle(row.pnl_ratio)">{{ formatPct(row.pnl_ratio) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="止损价" width="84">
            <template #default="{ row }">
              <span class="stop-loss-price">{{ formatPrice(stopLossPrice(row)) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="纪律状态" width="104">
            <template #default="{ row }">
              <el-tag :type="disciplineTagType(row)" effect="light" size="small">{{ disciplineTagText(row) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="RSI15/30" width="112">
            <template #default="{ row }">
              <div class="rsi-cell">
                <el-tag :type="row.signal?.rsi15_signal?.detected ? 'danger' : 'success'" effect="light" size="small">
                  15{{ row.signal?.rsi15_signal?.detected ? '触' : '-' }}
                </el-tag>
                <el-tag :type="row.signal?.rsi30_signal?.detected ? 'danger' : 'success'" effect="light" size="small">
                  30{{ row.signal?.rsi30_signal?.detected ? '触' : '-' }}
                </el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="箱体T" width="132" show-overflow-tooltip>
            <template #default="{ row }">
              <el-tag :type="rsiBoxTTagType(row)" effect="light" size="small" :title="row.signal?.rsi_box_t?.recommendation || ''">
                {{ rsiBoxTTagText(row) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="signal.suggestion" label="交易建议" width="144" show-overflow-tooltip />
          <el-table-column prop="buy_time" label="入场时间" width="136" />
          <el-table-column label="操作" width="106">
            <template #default="{ row, $index }">
              <div class="op-cell">
                <el-button type="warning" link @click="openSellDialog(row, $index)">卖出</el-button>
                <el-button type="danger" link @click="removeManualHolding($index)">删除</el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>

        <div v-if="focusCode" style="height: 12px" />
        <template v-if="focusCode">
          <el-alert
            type="info"
            :closable="false"
            :title="`当前标的：${focusCode}${focusName ? ` ${focusName}` : ''}`"
            :description="focusDescription"
            class="panel-alert"
          />
          <el-table :data="focusRows" stripe size="small" empty-text="当前快照未命中该标的，仍可加入持仓">
            <el-table-column label="代码" width="110">
              <template #default="{ row }">
                <el-button type="primary" link @click="openStockDetail(row)">{{ displayCode(row.code) }}</el-button>
              </template>
            </el-table-column>
            <el-table-column label="名称" min-width="120">
              <template #default="{ row }">
                <el-button type="primary" link @click="openStockDetail(row)">{{ row.name || '--' }}</el-button>
              </template>
            </el-table-column>
            <el-table-column prop="rank" label="排名" width="80" />
            <el-table-column prop="score_total" label="总分" width="90" />
            <el-table-column prop="reason_text" label="说明" min-width="220" show-overflow-tooltip />
            <el-table-column label="操作" width="120">
              <template #default="{ row }">
                <el-button type="primary" link @click="openAddHoldingDialog(row)">加入持仓</el-button>
              </template>
            </el-table-column>
          </el-table>
        </template>
      </div>

      <div class="panel">
        <div class="panel-title">历史成交与持仓轨迹</div>
        <div class="holding-toolbar">
          <div class="actions">
            <el-button @click="openThsImportDialog">读取同花顺成交</el-button>
          </div>
        </div>
        <div class="toolbar-note">按股票分组展示同花顺历史成交，默认按最近买入时间倒序；展开后可查看该股完整买卖明细。</div>
        <el-table :data="pagedHistoryTradeGroups" stripe size="small" empty-text="暂无历史成交">
          <el-table-column type="expand" width="52">
            <template #default="{ row }">
              <el-table :data="row.trades" size="small" stripe empty-text="该股暂无明细">
                <el-table-column prop="time" label="成交时间" width="160" />
                <el-table-column prop="side" label="方向" width="72" />
                <el-table-column prop="shares" label="数量" width="88" />
                <el-table-column prop="price" label="成交价" width="90">
                  <template #default="{ row: trade }">{{ formatPrice(trade.price) }}</template>
                </el-table-column>
                <el-table-column prop="before_shares" label="变动前" width="88" />
                <el-table-column prop="after_shares" label="变动后" width="88" />
                <el-table-column prop="realized_pnl" label="已实现盈亏" width="120">
                  <template #default="{ row: trade }">
                    <span :style="aSharePnlStyle(trade.realized_pnl)">{{ formatMoney(trade.realized_pnl) }}</span>
                  </template>
                </el-table-column>
                <el-table-column prop="reason" label="原因/备注" min-width="180" show-overflow-tooltip />
              </el-table>
            </template>
          </el-table-column>
          <el-table-column label="代码" width="110">
            <template #default="{ row }">
              <el-button type="primary" link @click="openStockDetail(row)">{{ displayCode(row.code) }}</el-button>
            </template>
          </el-table-column>
          <el-table-column label="名称" min-width="120">
            <template #default="{ row }">
              <el-button type="primary" link @click="openStockDetail(row)">{{ row.name || '--' }}</el-button>
            </template>
          </el-table-column>
          <el-table-column prop="latest_buy_time" label="最近买入时间" width="160" />
          <el-table-column prop="first_buy_time" label="首次买入时间" width="160" />
          <el-table-column prop="buy_count" label="买入次数" width="88" />
          <el-table-column prop="sell_count" label="卖出次数" width="88" />
          <el-table-column prop="net_shares" label="净持仓股数" width="110" />
          <el-table-column label="当前状态" width="100">
            <template #default="{ row }">
              <el-tag :type="row.is_active ? 'success' : 'info'" effect="light">{{ row.is_active ? '持有中' : '已卖出' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="当前持仓" width="100">
            <template #default="{ row }">{{ row.current_shares > 0 ? row.current_shares : '--' }}</template>
          </el-table-column>
          <el-table-column label="累计已实现盈亏" width="130">
            <template #default="{ row }">
              <span :style="aSharePnlStyle(row.realized_pnl)">{{ formatMoney(row.realized_pnl) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="last_trade_time" label="最近成交" width="160" />
          <el-table-column prop="summary" label="摘要" min-width="240" show-overflow-tooltip />
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
        <div class="panel-title">交易历史持仓明细</div>
        <el-table :data="positions.rows || []" stripe size="small" empty-text="暂无当前交易持仓">
          <el-table-column label="代码" width="110">
            <template #default="{ row }">
              <el-button type="primary" link @click="openStockDetail(row)">{{ displayCode(row.code) }}</el-button>
            </template>
          </el-table-column>
          <el-table-column label="名称" min-width="120">
            <template #default="{ row }">
              <el-button type="primary" link @click="openStockDetail(row)">{{ row.name || '--' }}</el-button>
            </template>
          </el-table-column>
          <el-table-column prop="shares" label="数量" width="88" />
          <el-table-column prop="avg_cost" label="成本" width="88" />
          <el-table-column prop="current_price" label="当前价" width="88">
            <template #default="{ row }">{{ formatPrice(row.current_price) }}</template>
          </el-table-column>
          <el-table-column prop="pnl_ratio" label="盈亏%" width="88">
            <template #default="{ row }">
              <span :style="aSharePnlStyle(row.pnl_ratio)">{{ formatPct(row.pnl_ratio) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="hold_days" label="持有天数" width="96" />
        </el-table>
      </div>

      <div class="panel">
        <div class="sub-panel-header">
          <div>
            <div class="panel-title">模拟盘桥接回放</div>
            <div class="toolbar-note">统一查看 bridge 订单、成交与最新持仓快照；等 PTrade 开始回写后，这里就是模拟盘闭环核对面板。</div>
            <div class="toolbar-note">{{ ptradeBridgeSummary }}</div>
            <div class="toolbar-note">{{ ptradeBridgeQueueText }}</div>
          </div>
          <div class="actions">
            <el-button :loading="ptradeBridgeLoading" @click="fetchPtradeBridgeState">刷新桥接状态</el-button>
            <el-button :loading="ptradeBridgeProbeLoading" @click="runPtradeBridgeProbe(false)">只读探针</el-button>
            <el-button type="warning" plain :loading="ptradeBridgeProbeLoading" @click="runPtradeBridgeProbe(true)">dry-run 探针</el-button>
            <el-button type="success" plain :loading="ptradeBridgeAcceptanceLoading" @click="runPtradeBridgeAcceptanceGate">PTrade 验收门禁</el-button>
            <el-button type="info" plain :loading="ptradeBridgeWatchAcceptanceLoading" @click="runPtradeBridgeWatchAcceptance">等待 PTrade 并验收</el-button>
            <el-button
              type="danger"
              plain
              :loading="ptradeBridgeLiveSubmitTestLoading"
              :disabled="!ptradeBridgeAudit?.gates?.live_submit_ready"
              @click="runPtradeBridgeLiveSubmitTest"
            >
              live-submit 小额验收
            </el-button>
            <el-button
              type="primary"
              :disabled="!(ptradeBridgePositions?.latest?.ok && (ptradeBridgePositions?.latest?.positions || []).length)"
              @click="importPtradePositionsToManualHoldings"
            >
              导入模拟盘持仓</el-button>
          </div>
        </div>
        <el-alert
          v-if="ptradeBridgePositions?.latest?.snapshot_time"
          type="info"
          :closable="false"
          :title="`最近模拟盘持仓快照：${ptradeBridgePositions.latest.snapshot_time}`"
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
          <div class="sub-panel-title">PTrade 验收证据</div>
          <el-table :data="ptradeBridgeEvidenceRows" stripe size="small" empty-text="暂无 PTrade 验收证据">
            <el-table-column prop="name" label="条件" min-width="180" />
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag :type="row.ok ? 'success' : 'warning'" size="small">{{ row.ok ? '通过' : '未通过' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="evidence" label="证据" min-width="260" show-overflow-tooltip />
            <el-table-column prop="next_action" label="下一步" min-width="320" show-overflow-tooltip />
          </el-table>
        </div>
        <div class="sub-panel" style="margin-top: 12px;">
          <div class="sub-panel-title">最新模拟盘持仓</div>
          <el-table :data="ptradeLatestPositionRows" stripe size="small" empty-text="暂无模拟盘持仓快照">
            <el-table-column prop="code" label="代码" width="110">
              <template #default="{ row }">{{ displayCode(row.code) }}</template>
            </el-table-column>
            <el-table-column prop="name" label="名称" min-width="120" />
            <el-table-column prop="shares" label="数量" width="90" />
            <el-table-column label="成本" width="90">
              <template #default="{ row }">{{ formatPrice(row.cost_price) }}</template>
            </el-table-column>
            <el-table-column label="现价" width="90">
              <template #default="{ row }">{{ formatPrice(row.current_price) }}</template>
            </el-table-column>
            <el-table-column label="市值" width="110">
              <template #default="{ row }">{{ formatMoney(row.market_value) }}</template>
            </el-table-column>
            <el-table-column label="盈亏%" width="90">
              <template #default="{ row }"><span :style="aSharePnlStyle(row.pnl_ratio)">{{ formatPct(row.pnl_ratio) }}</span></template>
            </el-table-column>
          </el-table>
        </div>
        <div class="sub-panel" style="margin-top: 12px;">
          <div class="sub-panel-title">最近模拟盘成交</div>
          <el-table :data="ptradeBridgeFills" stripe size="small" empty-text="暂无模拟盘成交回放">
            <el-table-column prop="order_id" label="订单号" width="180" show-overflow-tooltip />
            <el-table-column prop="code" label="代码" width="110">
              <template #default="{ row }">{{ displayCode(row.code) }}</template>
            </el-table-column>
            <el-table-column prop="side" label="方向" width="80" />
            <el-table-column prop="quantity" label="数量" width="90" />
            <el-table-column label="成交价" width="90">
              <template #default="{ row }">{{ formatPrice(row.fill_price ?? row.price) }}</template>
            </el-table-column>
            <el-table-column prop="filled_at" label="成交时间" width="168" show-overflow-tooltip />
            <el-table-column prop="message" label="说明" min-width="180" show-overflow-tooltip />
          </el-table>
        </div>
        <div class="sub-panel" style="margin-top: 12px;">
          <div class="sub-panel-title">最近模拟盘订单</div>
          <el-table :data="ptradeBridgeOrders.slice(0, 10)" stripe size="small" empty-text="暂无模拟盘订单">
            <el-table-column prop="order_id" label="订单号" width="180" show-overflow-tooltip />
            <el-table-column prop="code" label="代码" width="110">
              <template #default="{ row }">{{ displayCode(row.code) }}</template>
            </el-table-column>
            <el-table-column prop="side" label="方向" width="80" />
            <el-table-column prop="quantity" label="数量" width="90" />
            <el-table-column label="价格" width="90">
              <template #default="{ row }">{{ formatPrice(row.price) }}</template>
            </el-table-column>
            <el-table-column prop="_bridge_status" label="状态" width="110" />
            <el-table-column prop="created_at" label="创建时间" width="168" show-overflow-tooltip />
            <el-table-column prop="reason" label="说明" min-width="180" show-overflow-tooltip />
          </el-table>
        </div>
      </div>
    </template>

    <el-dialog v-model="gen2VerificationDialogVisible" title="G2候选确认验证" width="480px">
      <el-form label-width="90px">
        <el-form-item label="代码">
          <span>
            {{ normalizeCode(gen2VerificationForm.row?.code) || '--' }}
            {{ gen2VerificationForm.row?.name || '' }}
          </span>
        </el-form-item>
        <el-form-item label="验证状态">
          <el-select v-model="gen2VerificationForm.status" style="width: 220px">
            <el-option
              v-for="item in gen2VerificationOptions"
              :key="item.value || 'blank'"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="仓位%">
          <el-input-number v-model="gen2VerificationForm.position_pct" :min="0" :max="100" :step="5" style="width: 180px" />
        </el-form-item>
        <el-form-item label="成交价">
          <el-input-number v-model="gen2VerificationForm.fill_price" :min="0" :precision="3" :step="0.01" style="width: 180px" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="gen2VerificationForm.note"
            type="textarea"
            :rows="3"
            placeholder="记录为什么跟、为什么放弃、盘中执行偏差等"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="gen2VerificationDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="gen2VerificationSaving" @click="saveGen2Verification">保存验证</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="addDialogVisible" title="新增实盘持仓" width="420px">
      <el-form label-width="90px">
        <el-form-item label="代码">
          <el-autocomplete
            v-model="addForm.code"
            :fetch-suggestions="querySearchStock"
            placeholder="输入代码或名称模糊匹配"
            clearable
            @select="handleCodeSelect"
          />
        </el-form-item>
        <el-form-item label="名称">
          <el-input v-model="addForm.name" placeholder="可选" />
        </el-form-item>
        <el-form-item v-if="codeLookupText" label="匹配结果">
          <span>{{ codeLookupText }}</span>
        </el-form-item>
        <el-form-item label="股数">
          <el-input-number v-model="addForm.shares" :min="0" :step="100" style="width: 180px" />
        </el-form-item>
        <el-form-item label="成本价">
          <el-input-number v-model="addForm.cost_price" :min="0" :precision="3" :step="0.01" style="width: 180px" />
        </el-form-item>
        <el-form-item label="实时盈亏">
          <span :style="{ color: livePnlColor }">{{ livePnlText }}</span>
        </el-form-item>
        <el-form-item label="入场时间">
          <el-date-picker
            v-model="addForm.buy_time"
            type="datetime"
            value-format="YYYY-MM-DD HH:mm"
            format="YYYY-MM-DD HH:mm"
            placeholder="精确到分钟"
            style="width: 220px"
          />
        </el-form-item>
        <el-form-item v-if="addMode === 'new'" label="历史预期">
          <div v-if="entryBacktestLoading">正在评估历史样本...</div>
          <div v-else-if="entryBacktestText">{{ entryBacktestText }}</div>
          <div v-else>输入代码后自动评估</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="confirmAddHolding">{{ addMode === 'new' ? '检查交易规则' : '确认加入' }}</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="ruleDialogVisible" title="交易规则检查" width="560px">
      <el-alert
        :type="ruleCheckPassed ? 'success' : 'warning'"
        :closable="false"
        :title="ruleCheckPassed ? '规则检查通过，可以加入持仓' : '存在不符合策略的规则，请先调整'"
        class="panel-alert"
      />
      <el-table :data="ruleChecklist" size="small" stripe>
        <el-table-column prop="label" label="规则" min-width="260" />
        <el-table-column label="结果" width="80">
          <template #default="{ row }">
            <el-tag :type="row.pass ? 'success' : 'danger'" effect="light">{{ row.pass ? '通过' : '不通过' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="reason" label="说明" min-width="180" />
      </el-table>
      <template #footer>
        <el-button @click="ruleDialogVisible = false">关闭</el-button>
        <el-button type="primary" :disabled="!ruleCheckPassed" @click="confirmAddHoldingAfterCheck">通过并加入</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="sellDialogVisible" title="卖出持仓" width="430px">
      <el-form label-width="90px">
        <el-form-item label="代码">
          <span>{{ displayCode(sellForm.code) }}</span>
        </el-form-item>
        <el-form-item label="名称">
          <span>{{ sellForm.name || '--' }}</span>
        </el-form-item>
        <el-form-item label="可卖数量">
          <span>{{ sellForm.max_shares }}</span>
        </el-form-item>
        <el-form-item label="卖出数量">
          <el-input-number v-model="sellForm.shares" :min="1" :max="Math.max(1, Number(sellForm.max_shares || 1))" :step="1" />
          <el-button link type="primary" @click="sellHalfPosition">卖出一半</el-button>
          <el-button link type="primary" @click="sellAllPosition">全部卖出</el-button>
        </el-form-item>
        <el-form-item label="卖出价格">
          <el-input-number v-model="sellForm.price" :min="0" :precision="3" :step="0.01" />
        </el-form-item>
        <el-form-item label="卖出时间">
          <el-date-picker
            v-model="sellForm.time"
            type="datetime"
            value-format="YYYY-MM-DD HH:mm"
            format="YYYY-MM-DD HH:mm"
            placeholder="精确到分钟"
            style="width: 220px"
          />
        </el-form-item>
        <el-form-item label="原因">
          <el-input v-model="sellForm.reason" placeholder="止盈/止损/策略卖出" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="sellDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="confirmSellHolding">确认卖出</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="tradeLogVisible" title="实盘持仓交易记录" width="820px">
      <el-table :data="manualTradeLogs" stripe size="small" empty-text="暂无交易记录">
        <el-table-column prop="time" label="时间" width="150" />
        <el-table-column prop="side" label="方向" width="70" />
        <el-table-column prop="code" label="代码" width="100">
          <template #default="{ row }">{{ displayCode(row.code) }}</template>
        </el-table-column>
        <el-table-column prop="name" label="名称" width="120" />
        <el-table-column prop="shares" label="数量" width="80" />
        <el-table-column prop="price" label="价格" width="90" />
        <el-table-column prop="before_shares" label="卖前" width="80" />
        <el-table-column prop="after_shares" label="卖后" width="80" />
        <el-table-column prop="realized_pnl" label="已实现盈亏" width="110">
          <template #default="{ row }">
            <span :style="aSharePnlStyle(row.realized_pnl)">{{ formatMoney(row.realized_pnl) }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="reason" label="原因" min-width="120" />
      </el-table>
      <template #footer>
        <el-button @click="tradeLogVisible = false">关闭</el-button>
        <el-button type="danger" @click="clearTradeLogs">清空记录</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="thsImportVisible" title="读取同花顺历史成交" width="1100px">
      <el-alert
        type="info"
        :closable="false"
        title="优先读取交割单文件；也可以从同花顺历史成交页复制后读取剪贴板。系统会自动过滤无效成交。"
        class="panel-alert"
      />
      <div class="holding-toolbar">
        <div class="actions">
          <el-button :loading="thsImportLoading" type="success" @click="readThsTradesFromDeliveryFile">读取交割单文件</el-button>
          <el-button :loading="thsImportLoading" type="primary" @click="readThsTradesFromWindow">直接读取交易窗口</el-button>
          <el-button :loading="thsImportLoading" type="primary" @click="readThsTradesFromClipboard">读取剪贴板</el-button>
          <el-button :loading="thsImportLoading" @click="parseThsTradeText(thsImportRawText)">解析下方文本</el-button>
        </div>
        <div class="toolbar-note">已识别 {{ thsImportRows.length }} 条可导入成交，待确认 {{ thsImportCheckedCount }} 条</div>
      </div>
      <el-input
        v-model="thsImportRawText"
        type="textarea"
        :rows="6"
        placeholder="如果浏览器无法直接读取剪贴板，请把同花顺复制内容粘贴到这里，再点击解析下方文本"
      />
      <div style="height: 12px" />
      <el-table :data="thsImportRows" stripe size="small" empty-text="暂无可导入成交">
        <el-table-column label="确认" width="72">
          <template #default="{ row }">
            <el-checkbox v-model="row.checked" />
          </template>
        </el-table-column>
        <el-table-column prop="time" label="时间" width="168" />
        <el-table-column prop="side" label="方向" width="80" />
        <el-table-column prop="code" label="代码" width="96" />
        <el-table-column prop="name" label="名称" width="120" />
        <el-table-column prop="shares" label="数量" width="90" />
        <el-table-column prop="price" label="成交价" width="90" />
        <el-table-column prop="amount" label="成交额" width="110" />
        <el-table-column prop="remark" label="备注" min-width="160" show-overflow-tooltip />
        <el-table-column prop="filter_reason" label="识别说明" min-width="160" show-overflow-tooltip />
      </el-table>
      <template #footer>
        <el-button @click="thsImportVisible = false">取消</el-button>
        <el-button type="primary" :disabled="thsImportCheckedCount <= 0" @click="confirmImportThsTrades">勾选确认导入</el-button>
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
  getGen2DailyTradeTicket,
  getGen2DailyTradeExecution,
  saveGen2DailyTradeExecution,
  runGen2RiskCoolShadowUpdate,
  getGen2RiskCoolShadowUpdateTask,
  saveGen2ShadowVerification,
  getGen2ShadowMonitorStatus,
  setGen2ShadowMonitorConfig,
  runGen2ShadowMonitorNow,
  getGen2StrategyRefreshStatus,
  runGen2StrategyRefreshNow,
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
const gen2StrategyRefreshLoading = ref(false)
const gen2StrategyRefreshRunning = ref(false)
const gen2StrategyRefreshStatus = ref(null)
const workflowLoading = ref(false)
const workflowStatus = ref(null)
const dailyTradeTicketLoading = ref(false)
const dailyTradeTicket = ref(null)
const dailyExecutionLoading = ref(false)
const dailyExecutionSaving = ref(false)
const dailyExecution = ref(null)
const dailyExecutionForm = ref({
  execution_status: 'pending',
  executed: false,
  discipline_ok: true,
  violation_tags: [],
  execution_note: '',
  t1_review: '',
  t3_review: '',
  t5_review: ''
})
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
const monitorStatusText = ref('未开启')
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
const pageTitle = computed(() => 'G2 实盘交易驾驶舱')
const pageSubtitle = computed(() => (
  isGen2LiveMode.value
    ? '第一页只展示正式买点、开仓门槛、持仓风控和交易执行状态；影子样本仅用于复盘验证。'
    : '从交易选股到持仓管理闭环，并给出 15m/30m RSI 背离卖出提示。'
))
const unavailableTitle = computed(() => '第二代策略实盘交易数据不可用')
const buyPoolPanelTitle = computed(() => 'G2 参考买入观察池')
const gen2ShadowPanelTitle = computed(() => 'Alpha191 volume5 影子跟踪/复盘池')
const gen2StrategyRefreshLastResultText = computed(() => {
  const result = gen2StrategyRefreshStatus.value?.last_result || {}
  if (!result || !Object.keys(result).length) return '--'
  if (result.skipped) return result.reason || '已跳过'
  if (result.ok === false) return result.error || '刷新失败'
  const signalDate = result.signal_date ? ` ${result.signal_date}` : ''
  return `成功${signalDate}`
})
const gen2StrategyRefreshSummary = computed(() => {
  const status = gen2StrategyRefreshStatus.value || {}
  if (gen2StrategyRefreshLoading.value && !status.next_run_time) return '正在读取 G2 半小时策略刷新状态...'
  if (status.last_error) return `最近一次刷新异常：${status.last_error}`
  const delay = status.data_delay_minutes ?? 2
  const next = status.next_run_time || '--'
  const last = status.last_success_at || status.last_run_at || '--'
  return `按 30m 数据落盘后 ${delay} 分钟触发；下次 ${next}；上次 ${last}；结果 ${gen2StrategyRefreshLastResultText.value}`
})

const selection = computed(() => payload.value?.selection || {})
const selectionBranchFreshness = computed(() => selection.value?.branch_freshness || {})
const selectionBranchFreshnessWarning = computed(() => {
  if (selectionBranchFreshness.value?.status !== 'stale') return ''
  return (selectionBranchFreshness.value?.text || 'g2_v2_complete 正式展示分支滞后') + '；当前实盘执行继续按 latest-driven 主链判断，不会因为旧展示分支停在历史而停摆。'
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
const notificationStage = computed(() => workflowStages.value.find((stage) => stage.stage === 'notification'))
const notificationStageExists = computed(() => !!notificationStage.value)
const notificationStageOk = computed(() => notificationStage.value?.ok === true)
const notificationStageLabel = computed(() => notificationStageOk.value ? '通知链路正常' : '通知链路异常')
const workflowLineageRows = computed(() => (
  Array.isArray(workflowStatus.value?.lineage_items) ? workflowStatus.value.lineage_items : []
))
const workflowOk = computed(() => workflowStatus.value?.available !== false && workflowStages.value.length > 0 && workflowStages.value.every((stage) => !!stage.ok))
const workflowSummaryText = computed(() => {
  const data = workflowStatus.value || {}
  if (workflowLoading.value && !data.checked_at) return '正在检查策略流水线...'
  if (!data.available && data.message) return data.message
  const failed = workflowFailedChecks.value.length
  const checkedAt = data.checked_at ? '; 检查 ' + data.checked_at : ''
  const prev = data.prev_trade_date ? '; D-1 ' + data.prev_trade_date : ''
  const gate = data.alpha191_gate ? '; Gate ' + data.alpha191_gate : ''
  const notificationText = notificationStageExists.value
    ? (notificationStageOk.value ? '; 通知链路通过' : '; 通知链路异常')
    : '; 未检测到通知链路'
  return failed
    ? String(data.signal_date || selectedDate.value) + ' 有 ' + failed + ' 个阻塞点' + prev + gate + notificationText + checkedAt
    : String(data.signal_date || selectedDate.value) + ' 数据源、选股、评分、策略和通知链路通过' + prev + gate + notificationText + checkedAt
})
const dailyTradeTicketFormalRows = computed(() => (
  Array.isArray(dailyTradeTicket.value?.formal_candidates) ? dailyTradeTicket.value.formal_candidates : []
))
const dailyTradeTicketForbiddenActions = computed(() => (
  Array.isArray(dailyTradeTicket.value?.forbidden_actions) ? dailyTradeTicket.value.forbidden_actions : []
))
const dailyTradeTicketSummary = computed(() => {
  const ticket = dailyTradeTicket.value || {}
  if (dailyTradeTicketLoading.value && !ticket.generated_at) return '正在生成 G2 V4 今日交易单...'
  if (!ticket.available && ticket.message) return ticket.message
  const state = ticket.market_state || '--'
  const exposure = ticket.target_exposure || '--'
  const count = dailyTradeTicketFormalRows.value.length
  const generated = ticket.generated_at ? '；生成 ' + ticket.generated_at : ''
  return `状态 ${state}；仓位 ${exposure}；正式候选 ${count} 只${generated}`
})
const dailyTradeTicketOutputs = computed(() => {
  const outputs = dailyTradeTicket.value?.outputs || {}
  const jsonPath = outputs.json_path || outputs.latest_path || ''
  const mdPath = outputs.markdown_path || ''
  if (jsonPath && mdPath) return `${jsonPath}；${mdPath}`
  return jsonPath || mdPath || ''
})
const dailyExecutionRecord = computed(() => dailyExecution.value?.record || dailyTradeTicket.value?.execution_record || null)
const dailyExecutionStatusText = computed(() => {
  const status = String(dailyExecutionForm.value.execution_status || dailyExecutionRecord.value?.execution_status || 'pending')
  const labels = {
    pending: '待记录',
    followed: '照单执行',
    no_trade: '无交易',
    partial: '部分执行',
    violated: '纪律违规',
    reviewed: '已复盘'
  }
  return labels[status] || status
})
const dailyExecutionSummary = computed(() => {
  const record = dailyExecutionRecord.value
  if (dailyExecutionLoading.value && !record) return '正在读取今日执行台账...'
  if (!record) return '今日交易单尚未回填执行记录'
  const updated = record.updated_at ? '；更新 ' + record.updated_at : ''
  const discipline = record.discipline_ok === false ? '；存在纪律问题' : '；纪律正常'
  return dailyExecutionStatusText.value + discipline + updated
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
  if (!data.available) return data.message || '买入观察池待生成'
  const stage = data.market_stage_label ? '，大盘阶段：' + data.market_stage_label : ''
  return String(data.signal_date || '--') + ' 候选 ' + (data.candidate_count || 0) + ' 只，达到 V4 入场阈值 ' + (data.primary_count || 0) + ' 只' + stage
})
const workflowStageLabel = (stage) => ({
  data_source: '数据源',
  selection: '选股生成',
  selection_context: '板块上下文',
  scoring: '评分因子',
  strategy: '策略产物',
  intraday_data: '盘中数据',
  notification: '通知链路'
}[stage] || stage || '--')
const workflowStageMeta = (stage) => {
  const total = Number(stage?.check_count || stage?.checks?.length || 0)
  const failed = Number(stage?.failed_count || stage?.failed?.length || 0)
  return stage?.ok ? String(total) + '项检查通过' : String(failed) + '/' + String(total) + '项未通过'
}
const workflowCheckResult = (row) => {
  if (row?.row_count !== undefined) return 'rows ' + String(row.row_count)
  if (row?.code_count !== undefined) return 'codes ' + String(row.code_count)
  if (row?.size !== undefined) return String(row.size) + ' bytes'
  if (row?.max_datetime) return row.max_datetime
  return row?.ok ? 'ok' : 'blocked'
}
const workflowLineageResult = (row) => {
  const status = row?.status || {}
  return status.target_rows !== undefined && status.target_rows !== null ? 'rows ' + String(status.target_rows) : (row?.ok ? 'ok' : 'pending')
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
  if (!status.ok) return '模拟盘桥接状态未读取。'
  const lastAck = status.last_ack_file ? '；已有回执' : ''
  const lastFill = status.last_fill_file ? '；已有成交回写' : ''
  const processing = Number(status.processing_count || 0)
  const stale = Number(status.processing_stale_count || 0)
  const heartbeatAge = Number(status.ptrade_heartbeat_age_seconds)
  const processingText = processing ? '; processing ' + String(processing) : ''
  const staleText = stale ? '; stale processing ' + String(stale) : ''
  const heartbeatText = Number.isFinite(heartbeatAge) ? '; PTrade心跳 ' + String(Math.round(heartbeatAge)) + 's' : '; PTrade心跳未见'
  return '模拟盘桥接：pending ' + String(status.pending_count || 0) + processingText + staleText + heartbeatText + '；活动订单 ' + String(ptradeActiveOrders.value.length) + '；dry_run 默认 ' + (status.dry_run_default ? '开' : '关') + '；审批默认 ' + (status.require_approval_default ? '开' : '关') + lastAck + lastFill
})
const ptradeBridgeQueueText = computed(() => {
  const status = ptradeBridgeStatus.value || {}
  const queue = status.ptrade_strategy_queue || {}
  if (!status.ok) return 'PTrade 队列遥测未读取。'
  if (!queue || !Object.keys(queue).length) return 'PTrade 策略端队列遥测未见；AiStock 仍只写本地 pending，不等待 ack。'
  const pending = Number(queue.pending_count)
  const processing = Number(queue.processing_count)
  const cancelRequests = Number(queue.cancel_request_count)
  const oldestPending = Number(queue.oldest_pending_age_seconds)
  const oldestProcessing = Number(queue.oldest_processing_age_seconds)
  const processed = Number(queue.total_order_processed)
  const errors = Number(queue.total_bridge_errors)
  const pendingText = Number.isFinite(pending) ? 'pending ' + String(pending) : 'pending --'
  const processingText = Number.isFinite(processing) ? 'processing ' + String(processing) : 'processing --'
  const cancelText = Number.isFinite(cancelRequests) ? 'cancel ' + String(cancelRequests) : 'cancel --'
  const pendingAgeText = Number.isFinite(oldestPending) ? '最老 pending ' + String(Math.round(oldestPending)) + 's' : '最老 pending --'
  const processingAgeText = Number.isFinite(oldestProcessing) ? '最老 processing ' + String(Math.round(oldestProcessing)) + 's' : '最老 processing --'
  const processedText = Number.isFinite(processed) ? '累计消费 ' + String(processed) : '累计消费 --'
  const errorText = Number.isFinite(errors) ? '错误 ' + String(errors) : '错误 --'
  const lastError = queue.last_bridge_error ? '；最近错误：' + queue.last_bridge_error : ''
  return 'PTrade 策略端队列：' + pendingText + '；' + processingText + '；' + cancelText + '；' + pendingAgeText + '；' + processingAgeText + '；' + processedText + '；' + errorText + lastError
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
    return 'PTrade readiness audit: ' + dryRun + '; ' + live + (liveEnabled ? '; ' + liveEnabled : '') + (next ? '; next: ' + next : '')
  }
  const readiness = ptradeBridgeStatus.value?.readiness || {}
  const live = readiness.ready_for_live_order ? 'ready for approved live order' : 'not ready for approved live order'
  const heartbeat = readiness.ptrade_heartbeat_recent ? 'heartbeat ok' : 'heartbeat missing/stale'
  const probe = readiness.dry_run_probe_ack_recent ? 'dry-run ack ok' : 'dry-run ack missing/stale'
  const liveEnabled = readiness.ptrade_live_order_enabled ? 'PTrade live enabled' : 'PTrade live disabled'
  return 'PTrade readiness: ' + live + '; ' + heartbeat + '; ' + probe + '; ' + liveEnabled
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
  const blockingText = reason ? ('；阻塞：' + (stage ? stage + '，' : '') + reason) : ''
  return 'PTrade evidence: ' + (evidence.ok ? 'ready' : 'not ready') + '; failed ' + String(failed.length) + '; generated ' + String(generatedAt) + blockingText
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
  const heartbeatText = Number.isFinite(heartbeat) ? 'PTrade心跳 ' + String(Math.round(heartbeat)) + 's' : 'PTrade心跳未见'
  const ackStatus = result.ack_result?.ack?.status
  const ackText = result.submit_dry_run ? ('；dry-run ack ' + String(ackStatus || '未收到')) : ''
  return (result.ok ? '探针通过' : '探针未通过') + '；' + heartbeatText + ackText + '；必需检查失败 ' + String(failed.length)
})
const ptradeBridgeAcceptanceSummary = computed(() => {
  const result = ptradeBridgeAcceptanceResult.value
  if (!result) return ''
  const checks = Array.isArray(result.checks) ? result.checks : []
  const failed = checks.filter((item) => item?.required && !item?.ok)
  const heartbeatAge = Number(result.heartbeat?.heartbeat_age_seconds)
  const heartbeatText = Number.isFinite(heartbeatAge) ? 'heartbeat ' + String(Math.round(heartbeatAge)) + 's' : 'heartbeat 未见'
  const ackStatus = result.dry_run_probe?.ack_result?.ack?.status
  const liveReady = result.readiness_audit?.gates?.live_submit_ready ? 'live-submit ready' : 'live-submit not ready'
  return 'PTrade 验收' + (result.ok ? '通过' : '未通过') + '；' + heartbeatText + '；dry-run ack ' + String(ackStatus || '未收到') + '；' + liveReady + '；失败 ' + String(failed.length)
})
const ptradeBridgeWatchAcceptanceSummary = computed(() => {
  const result = ptradeBridgeWatchAcceptanceResult.value
  if (!result) return ''
  const acceptance = result.acceptance || {}
  const checks = Array.isArray(acceptance.checks) ? acceptance.checks : []
  const failed = checks.filter((item) => item?.required && !item?.ok)
  const heartbeat = result.heartbeat || acceptance.heartbeat || null
  const heartbeatAge = Number(heartbeat?.heartbeat_age_seconds)
  const heartbeatText = Number.isFinite(heartbeatAge) ? 'heartbeat ' + String(Math.round(heartbeatAge)) + 's' : 'heartbeat 未见'
  const ackStatus = acceptance.dry_run_probe?.ack_result?.ack?.status
  return 'PTrade 等待验收' + (result.ok ? '通过' : '未通过') + '；' + heartbeatText + '；dry-run ack ' + String(ackStatus || '未收到') + '；失败 ' + String(failed.length)
})
const ptradeBridgeLiveSubmitTestSummary = computed(() => {
  const result = ptradeBridgeLiveSubmitTestResult.value
  if (!result) return ''
  const checks = Array.isArray(result.checks) ? result.checks : []
  const failed = checks.filter((item) => item?.required && !item?.ok)
  const submitElapsed = Number(result.submit_result?.submit_elapsed_seconds)
  const submitText = Number.isFinite(submitElapsed) ? 'submit ' + String(submitElapsed.toFixed(3)) + 's' : 'submit not written'
  const ackStatus = result.ack_result?.ack?.status || 'ack missing'
  return 'PTrade live-submit test ' + (result.ok ? 'passed' : 'blocked') + '; ' + submitText + '; ' + ackStatus + '; failed checks ' + String(failed.length)
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
        flags.push('硬止损达到 -6%')
        priority = Math.min(priority, 1)
      } else if (Number.isFinite(pnl) && pnl <= -4) {
        flags.push('亏损达到 -4%，禁止加仓并优先观察')
        priority = Math.min(priority, 2)
      }
      if (isSingleTradeLossCapBreached(row)) {
        flags.push('组合拖累超过 ' + String(SINGLE_TRADE_LOSS_CAP_PCT_OF_TOTAL) + '%')
        priority = Math.min(priority, 1)
      }
      if (isTrailingTakeProfitTriggered(row)) {
        flags.push('触发移动止盈回撤')
        priority = Math.min(priority, 2)
      }
      if (row?.score_pool_status === 'out_of_pool' || row?.in_score_pool === false) {
        flags.push('离场提示')
        priority = Math.min(priority, 3)
      }
      if (row?.signal?.rsi15_signal?.detected || row?.signal?.rsi30_signal?.detected) {
        flags.push('15m/30m 移动止盈触发')
        priority = Math.min(priority, 2)
      }
      const boxAction = String(row?.signal?.rsi_box_t?.action || '')
      if (boxAction === 'sell_half' || boxAction === 'sell_part') {
        flags.push(row?.signal?.rsi_box_t?.recommendation || '箱体内 RSI 做T减仓触发')
        priority = Math.min(priority, boxAction === 'sell_half' ? 2 : 3)
      } else if (boxAction === 'risk_control_no_t') {
        flags.push(row?.signal?.rsi_box_t?.recommendation || '跌破箱体，停止做T并风控')
        priority = Math.min(priority, 2)
      } else if (boxAction === 'hold_trend_no_t') {
        flags.push(row?.signal?.rsi_box_t?.recommendation || '突破箱体，停止做T')
        priority = Math.min(priority, 5)
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
        risk_text: flags.join('；')
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
    { value: '', label: '未标记', type: 'info' },
    { value: 'watch', label: '观察', type: 'warning' },
    { value: 'paper', label: '模拟跟踪', type: 'primary' },
    { value: 'small_buy', label: '小仓买入', type: 'success' },
    { value: 'skip', label: '放弃', type: 'info' },
    { value: 'reject', label: '应过滤', type: 'danger' }
  ]
})
const gen2ShadowSummary = computed(() => {
  const data = gen2Shadow.value || {}
  if (!data.available) return data.message || 'G2 V3 User V2 影子观察待生成'
  const counts = data.counts || {}
  const observable = counts.observable || 0
  const suspended = (counts.suspended_by_two_stop_cd3 || 0) + (counts.suspended_by_stop_cd5 || 0)
  const executed = counts.executed || 0
  return String(data.signal_date || '--') + ' G2影子 ' + String(data.row_count || gen2ShadowRows.value.length) + ' 条，可观察 ' + String(observable) + '，熔断暂停 ' + String(suspended) + '，已执行影子 ' + String(executed)
})
const gen2ShadowSummaryDisplay = computed(() => {
  const data = gen2Shadow.value || {}
  if (!data.available) return data.message || 'G2 shadow review pending'
  const counts = data.counts || {}
  const observable = counts.observable || 0
  const suspended = (counts.suspended_by_two_stop_cd3 || 0) + (counts.suspended_by_stop_cd5 || 0)
  const executed = counts.executed || 0
  const displayDate = String(data.display_signal_date || data.selection_signal_date || data.signal_date || '--')
  const ledgerDate = String(data.ledger_signal_date || data.signal_date || '--')
  const summaryCore = displayDate + ' shadow rows ' + String(data.row_count || gen2ShadowRows.value.length) + ', observable ' + String(observable) + ', suspended ' + String(suspended) + ', executed ' + String(executed)
  if (data.is_ledger_fallback && ledgerDate && ledgerDate !== displayDate) {
    return summaryCore + '; ledger=' + ledgerDate
  }
  return summaryCore
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
    ? '已标记均值 5/10/20日 ' + avg5.toFixed(2) + '% / ' + (Number.isFinite(avg10) ? avg10.toFixed(2) : '--') + '% / ' + (Number.isFinite(avg20) ? avg20.toFixed(2) : '--') + '%'
    : '已标记样本暂无完整跟踪收益'
  return '全局验证台账：已标记 ' + String(marked) + '/' + String(total) + '，观察/模拟/小仓 ' + String(actionCount) + '，放弃/应过滤 ' + String(rejectedCount) + '，30m止损 ' + String(stopCount) + '，' + avgText
})
const gen2VerificationPromotionSource = computed(() => gen2Shadow.value?.verification_global_summary || gen2Shadow.value?.verification_summary || {})
const gen2VerificationPromotionType = computed(() => gen2VerificationPromotionSource.value?.promotion_type || 'info')
const gen2VerificationPromotionLabel = computed(() => gen2VerificationPromotionSource.value?.promotion_label || '样本收集中')
const gen2VerificationPromotionMessage = computed(() => gen2VerificationPromotionSource.value?.promotion_message || '至少先累计 20 条已标记样本，再判断是否进入小仓实盘验证。')
const gen2ShadowFreshnessNote = computed(() => {
  const d = gen2Shadow.value?.data_freshness || {}
  const dateNotice = String(gen2Shadow.value?.date_notice || '').trim()
  if (dateNotice) return dateNotice
  if (!d.latest_daily_date || !d.latest_shadow_date || d.latest_daily_date <= d.latest_shadow_date) return ''
  return '数据提示：V4日线已到 ' + String(d.latest_daily_date) + '，但G2有效影子台账最新为 ' + String(d.latest_shadow_date) + '；若页面仍显示旧信号日，说明当前规则尚未产生新的有效G2信号。'
})
const gen2ShadowUpdateText = computed(() => {
  const task = gen2ShadowUpdateTask.value || {}
  if (!task.status) return ''
  if (task.status === 'completed') {
    const result = task.result || {}
    return '影子交易已更新：原始候选 ' + String(result.raw_candidates ?? '--') + '，过滤后 ' + String(result.filtered_signals ?? '--') + '，当日影子 ' + String(result.shadow_rows_for_date ?? '--') + '。'
  }
  if (task.status === 'failed') return '影子交易更新失败：' + String(task.error || '未知错误')
  return '影子交易更新中：' + String(task.status) + ' ' + String(task.progress || 0) + '%'
})
const gen2ShadowMonitorText = computed(() => {
  const status = gen2ShadowMonitorStatus.value || {}
  const interval = Math.max(1, Math.round(Number(status.interval_seconds || 60) / 60))
  const state = status.enabled ? ('自动探测中，每 ' + String(interval) + ' 分钟') : '自动探测未开启'
  const gate = status.alpha191_gate ? ('；Alpha191 ' + String(status.alpha191_gate)) : ''
  const nextRun = status.next_run_time ? ('；下次 ' + String(status.next_run_time)) : ''
  const lastRun = status.last_run_at ? ('；最近 ' + String(status.last_run_at)) : ''
  const lastMail = status.last_email_sent_at ? ('；最近邮件 ' + String(status.last_email_sent_at)) : ''
  const lastHeartbeat = status.last_heartbeat_sent_at ? ('；最近心跳 ' + String(status.last_heartbeat_sent_at)) : ''
  const err = status.last_error ? ('；错误 ' + String(status.last_error)) : ''
  return state + gate + nextRun + lastRun + lastMail + lastHeartbeat + err
})
const gen2ShadowMonitorResultText = computed(() => {
  const result = gen2ShadowMonitorStatus.value?.last_result || {}
  if (!Object.keys(result).length) return ''
  if (result.skipped) return '最近探测跳过：' + String(result.reason || '--')
  if (result.blocked) return '最近探测阻塞：' + String(result.blocker_count ?? '--') + ' 个阻塞点' + (result.blocker_email_sent ? '，已发送阻塞提醒' : '')
  const parts = [
    '信号日 ' + String(result.signal_date || '--'),
    '原始 ' + String(result.raw_candidates ?? '--'),
    '过滤 ' + String(result.filtered_signals ?? '--'),
    '影子 ' + String(result.shadow_rows_for_date ?? '--'),
    '可提醒 ' + String(result.candidate_count ?? '--'),
    '新增 ' + String(result.new_count ?? '--')
  ]
  if (result.email_sent) parts.push('已发送买点邮件')
  else if (result.heartbeat_email_sent) parts.push('已发无买点心跳回执')
  else parts.push('本轮无新邮件')
  return '最近探测：' + parts.join('；')
})
const marketGateSummary = computed(() => {
  const gate = marketGate.value || {}
  if (!gate.available) return gate.message || '上证指数数据暂不可用'
  const close = Number(gate.close)
  const ma20 = Number(gate.ma20)
  const date = gate.trade_date || '--'
  return String(date) + ' 收盘 ' + (Number.isFinite(close) ? close.toFixed(2) : '--') + ' / MA20 ' + (Number.isFinite(ma20) ? ma20.toFixed(2) : '--')
})
const autoRefreshStatusText = computed(() => {
  const running = autoRefreshRunning.value ? '刷新中' : '待命'
  const last = lastAutoRefreshAt.value ? ('；最近刷新 ' + String(lastAutoRefreshAt.value)) : ''
  return '交易时段分层自动刷新：桥接/持仓30秒，大盘门槛60秒，G2影子/工作流180秒；当前' + running + last
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
      reason: x?.reason || '实盘记录'
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
      const buyTrades = trades.filter((item) => String(item?.side || '') === '买入')
      const sellTrades = trades.filter((item) => String(item?.side || '') === '卖出')
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
        summary: '买入 ' + String(buyTrades.length) + ' 次，卖出 ' + String(sellTrades.length) + ' 次；累计买入 ' + String(totalBought) + ' 股，累计卖出 ' + String(totalSold) + ' 股'
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
  const sourceText = focusSource.value ? ('来源：' + String(focusSource.value)) : '来源：手工入口'
  const signalText = focusSignalDate.value ? ('；信号日：' + String(focusSignalDate.value)) : ''
  return sourceText + signalText + '。该入口用于把交易选股中的目标股直接加入实盘交易持仓。'
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
  if (manualHoldings.value.length >= 3) lines.push('纪律1：持仓已达上限 3 只，禁止新增')
  const hardRisk = manualHoldings.value.filter((x) => Number(x.pnl_ratio) <= -6).map((x) => x.code)
  if (hardRisk.length) lines.push('纪律4：' + hardRisk.join('、') + ' 已触发 -6%，应优先处理')
  const warnRisk = manualHoldings.value.filter((x) => Number(x.pnl_ratio) <= -4 && Number(x.pnl_ratio) > -6).map((x) => x.code)
  if (warnRisk.length) lines.push('纪律4：' + warnRisk.join('、') + ' 已到 -4%，禁止加仓')
  const trailingRisk = manualHoldings.value.filter((x) => isTrailingTakeProfitTriggered(x)).map((x) => x.code)
  if (trailingRisk.length) lines.push('纪律5：' + trailingRisk.join('、') + ' 触发移动止盈回撤，建议分批止盈')
  const lossCapRisk = manualHoldings.value.filter((x) => isSingleTradeLossCapBreached(x)).map((x) => x.code)
  if (lossCapRisk.length) lines.push('纪律6：' + lossCapRisk.join('、') + ' 组合拖累超过 ' + String(SINGLE_TRADE_LOSS_CAP_PCT_OF_TOTAL) + '%')
  const outPoolRisk = manualHoldings.value
    .filter((x) => x?.score_pool_status === 'out_of_pool' || x?.in_score_pool === false)
    .map((x) => x.code)
  if (outPoolRisk.length) lines.push('纪律7：' + outPoolRisk.join('、') + ' 已掉出评分池，按卖出规则优先处理')
  return lines.join('；')
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
  return [{ code: focusCode.value, name: focusName.value || '--', rank: '--', score_total: '--', reason_text: '当前快照未命中该股，可直接加入持仓。' }]
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

const THS_FILTER_KEYWORDS = ['打新', '新股', '配号', '超配', '红利', '股息', '清算', '利税', '利息', '配售']

const normalizeTradeTime = (dateText, timeText) => {
  const ds = String(dateText || '').replace(/\D/g, '')
  if (ds.length !== 8) return ''
  const day = ds.slice(0, 4) + '-' + ds.slice(4, 6) + '-' + ds.slice(6, 8)
  const rawTime = String(timeText || '').trim()
  if (!rawTime) return day + ' 00:00:00'
  const parts = rawTime.split(':').map((x) => x.padStart(2, '0'))
  if (parts.length === 2) return day + ' ' + parts[0] + ':' + parts[1] + ':00'
  if (parts.length >= 3) return day + ' ' + parts[0] + ':' + parts[1] + ':' + parts[2]
  return day + ' 00:00:00'
}

const parseThsNumber = (value) => {
  const text = String(value ?? '').replace(/,/g, '').trim()
  if (!text) return null
  const num = Number(text)
  return Number.isFinite(num) ? num : null
}

const parseThsTradeSide = (action) => {
  const text = String(action || '').trim()
  if (text === '买入' || text.includes('买入')) return '买入'
  if (text === '卖出' || text.includes('卖出')) return '卖出'
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
    ElMessage.warning(error?.message || '读取剪贴板失败，请手动粘贴')
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
      ElMessage.warning(resp?.message || '读取同花顺窗口失败')
      return
    }
    if (!resp?.recognized_recent_trade) {
      const viewDesc = String(resp?.view_type_desc || '未知表格')
      ElMessage.warning('当前读取到的是 ' + viewDesc + '，不是“历史成交”')
      return
    }
    parseThsTradeText(text)
  } catch (error) {
    ElMessage.warning(error?.message || '读取同花顺窗口失败')
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
      ElMessage.warning(resp?.message || '读取交割单文件失败')
      return
    }
    parseThsTradeText(text)
    if (resp?.file_path) {
      const fileTimeText = resp?.file_mtime ? '（更新于 ' + resp.file_mtime + '）' : ''
      ElMessage.success('已读取交割单文件：' + resp.file_path + fileTimeText)
    }
  } catch (error) {
    ElMessage.warning(error?.message || '读取交割单文件失败')
  } finally {
    thsImportLoading.value = false
  }
}

const parseThsTradeText = (rawText) => {
  const text = String(rawText || '')
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
  if (!lines.length) {
    thsImportRows.value = []
    ElMessage.warning('未读取到同花顺成交文本')
    return
  }
  const rows = lines.map((line) => line.split('\t'))
  const headerIndex = rows.findIndex((cells) => cells.includes('成交日期') && cells.includes('证券代码') && cells.includes('操作'))
  if (headerIndex < 0) {
    thsImportRows.value = []
    ElMessage.warning('未识别到同花顺成交表头，请确认复制的是“历史成交”表格')
    return
  }
  const header = rows[headerIndex]
  const idx = (name) => header.indexOf(name)
  const dateIdx = idx('成交日期')
  const timeIdx = idx('成交时间')
  const codeIdx = idx('证券代码')
  const nameIdx = idx('证券名称')
  const actionIdx = idx('操作')
  const sharesIdx = idx('成交数量')
  const priceIdx = idx('成交均价')
  const amountIdx = idx('成交金额')
  const remarkIdx = idx('备注')
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
    if (!/^(证券买入|证券卖出)$/.test(action)) continue
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
      filter_reason: '真实证券成交'
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
    ElMessage.warning('已读取内容，但过滤后没有可导入的真实证券成交')
    return
  }
  ElMessage.success('已识别 ' + String(dedup.length) + ' 条真实证券成交，请勾选确认')
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
    ElMessage.warning('请至少勾选一条成交记录')
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
    if (tx.side === '' || tx.side === '买入') {
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
        side: '买入',
        code,
        name: String(tx.name || existing?.name || ''),
        shares,
        price: Number.isFinite(price) ? Number(price.toFixed(3)) : null,
        before_shares: existing ? Math.max(0, Math.trunc(Number(existing.shares || 0))) - shares : 0,
        after_shares: existing ? Math.max(0, Math.trunc(Number(existing.shares || 0))) : shares,
        realized_pnl: null,
        reason: '同花顺导入'
      })
      imported += 1
      touchedCodes.add(code)
      existingSignatures.add(sig)
      continue
    }
    if (!existing) {
      manualTradeLogs.value.unshift({
        time,
        side: '卖出',
        code,
        name: String(tx.name || ''),
        shares,
        price: Number.isFinite(price) ? Number(price.toFixed(3)) : null,
        before_shares: null,
        after_shares: null,
        realized_pnl: null,
        reason: '同花顺导入（未匹配到现持仓）'
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
      side: '卖出',
      code,
      name: String(tx.name || existing?.name || ''),
      shares,
      price: Number.isFinite(price) ? Number(price.toFixed(3)) : null,
      before_shares: beforeShares,
      after_shares: nextShares,
      realized_pnl: realizedPnl,
      reason: '同花顺导入'
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
  const duplicateText = duplicated ? '，跳过重复 ' + String(duplicated) + ' 条' : ''
  const orphanSellText = orphanSell ? '，其中 ' + String(orphanSell) + ' 条卖出仅记入流水' : ''
  ElMessage.success('已导入 ' + String(imported) + ' 条成交' + duplicateText + orphanSellText)
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
    ElMessage.warning('股票代码无效，无法复制')
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
      ElMessage.success('已复制代码：' + code)
  } catch (error) {
    ElMessage.warning('复制失败，请手动复制代码')
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
  if (!Number.isFinite(quote) || quote <= 0) return '现价待获取'
  const pnl = livePnlPct.value
  if (pnl === null) return '现价 ' + quote.toFixed(3) + '，请填写成本价'
  return '现价 ' + quote.toFixed(3) + '，盈亏 ' + (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + '%'
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
  if (!count) return '该股暂无历史入场样本'
  const d3 = data?.horizons?.d3 || {}
  const d5 = data?.horizons?.d5 || {}
  const d10 = data?.horizons?.d10 || {}
  const line = (label, item) => {
    const win = item?.win_rate
    const avg = item?.avg_return
    const n = Number(item?.sample_count || 0)
    const winText = Number.isFinite(Number(win)) ? Number(win).toFixed(2) + '%' : '--'
    const avgText = Number.isFinite(Number(avg)) ? Number(avg).toFixed(2) + '%' : '--'
    return label + ' 胜率 ' + winText + '，平均收益 ' + avgText + '（样本 ' + String(n) + '）'
  }
  return '样本总数 ' + String(count) + '；' + line('3日', d3) + '；' + line('5日', d5) + '；' + line('10日', d10)
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
    codeLookupText.value = '已匹配：' + normalized + ' ' + String(localHit.name)
  } else {
    codeLookupText.value = '正在匹配代码...'
  }
  try {
    const detail = await getStockDetail(toExchangeCode(normalized), { skipErrorHandler: true })
    const name = String(detail?.name || detail?.stock_name || '').trim()
    if (name) {
      addForm.value.name = name
      codeLookupText.value = '已匹配：' + normalized + ' ' + name
      let detailPrice = Number(detail?.price ?? detail?.close ?? detail?.current_price ?? detail?.last_price)
      if (!(Number.isFinite(detailPrice) && detailPrice > 0)) {
        try {
          const history = await getStockHistory(toExchangeCode(normalized), { period: '1d', limit: 2 }, { skipErrorHandler: true })
          detailPrice = Number(extractLatestPriceFromHistory(history))
        } catch {}
      }
      if (Number.isFinite(detailPrice) && detailPrice > 0) liveQuotePrice.value = detailPrice
    } else if (!localHit?.name) {
      codeLookupText.value = '代码未匹配到该股票，请手动补充名称'
    }
  } catch {
    if (!localHit?.name) codeLookupText.value = '代码未匹配到该股票，请手动补充名称'
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
  ElMessage.success('交易记录已清空')
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
    ElMessage.success('手工持仓修复：名称与金额字段已更新')
  }
}

const refreshManualSignals = async (options = {}) => {
  if (!manualHoldings.value.length) return
  signalLoading.value = true
  try {
    const resp = await getV4ManualHoldingSignals({ holdings: manualHoldings.value })
    const signalMap = new Map((resp?.rows || []).map((r) => [String(r.code || ''), r]))
    manualHoldings.value = manualHoldings.value.map((item) => {
      const hit = signalMap.get(String(item.code || ''))
      if (!hit) return { ...item, signal: null }
      return {
        ...item,
        signal: {
          rsi15_signal: hit.rsi15_signal || null,
          rsi30_signal: hit.rsi30_signal || null,
          rsi_box_t: hit.rsi_box_t || null,
          risk_level: hit.risk_level || '',
          suggestion: hit.suggestion || ''
        }
      }
    })
    applyTrailingTakeProfitState()
    saveManualHoldings()
  } catch (error) {
    if (!options.silent) ElMessage.warning(error?.message || '刷新 RSI 点位失败')
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
          options.includePool === false ? '当前没有持仓可更新' : '已成功刷新买点与买入观察池'
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
          rsi_box_t: hit.rsi_box_t || item?.signal?.rsi_box_t || null,
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
      ElMessage.success(options.successMessage || '本次自动刷新：行情/规则/信号/动作/买卖/交投/买入池已更新')
    }
  } catch (error) {
    if (!options.silent) ElMessage.warning(error?.message || '一次自动刷新失败')
  } finally {
    allRefreshLoading.value = false
  }
}

const refreshCurrentHoldings = async () => {
  holdingRefreshLoading.value = true
  try {
    await refreshAllHoldingData({
      includePool: false,
      successMessage: '刷新手动持仓与流水与行情：买入和卖出已更新'
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
    if (!options.silent) ElMessage.warning(error?.message || '自动刷新执行失败')
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
      ElMessage.warning(resp?.message || '读取同花顺资金持股失败')
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
    const capitalText = capitalForm.value.synced_total_capital ? ('；总资产 ' + formatMoney(capitalForm.value.synced_total_capital)) : ''
    if (resp?.fallback_cache) {
      ElMessage.warning('实时窗口读取失败，已回退到最近一次成功同步快照：' + String(manualHoldings.value.length) + ' 只' + capitalText)
    } else {
      ElMessage.success('已同步同花顺资金持股：' + String(manualHoldings.value.length) + ' 只' + capitalText)
    }
  } catch (error) {
    ElMessage.warning(error?.message || '同步同花顺资金持股失败')
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
    const nextRun = status?.next_run_time ? ('；下次执行 ' + String(status.next_run_time)) : ''
    const lastRun = status?.last_run_at ? ('；最近执行 ' + String(status.last_run_at)) : ''
    const lastMail = status?.last_email_sent_at ? ('；最近邮件 ' + String(status.last_email_sent_at)) : ''
    const err = status?.last_error ? ('；最近错误 ' + String(status.last_error)) : ''
    monitorStatusText.value = (monitorEnabled.value ? '运行中' : '未开启') + nextRun + lastRun + lastMail + err
  } catch {
    monitorStatusText.value = '状态读取失败'
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
    ElMessage.success(monitorEnabled.value ? '已开启每分钟跟踪' : '已停止每分钟跟踪')
  } catch (error) {
    ElMessage.warning(error?.message || '设置每分钟跟踪失败')
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
    if (resp?.email_sent) ElMessage.success('测试邮件已发送')
    else ElMessage.info(resp?.message || '本次暂未触发发送')
  } catch (error) {
    ElMessage.warning(error?.message || '测试提醒发送失败')
  } finally {
    monitorLoading.value = false
  }
}

const quickAddHolding = async (row) => {
  const code = normalizeCode(row?.code)
  if (!code) return
  if (manualHoldings.value.length >= 3 && !manualHoldings.value.find((x) => normalizeCode(x.code) === code)) {
    ElMessage.warning('持仓上限 3 只，请先移除后再新增')
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
      ElMessage.warning('已建仓后盈利未正向，请先查看风险')
      return
    }
    const sameWeekCount = exists.buy_week_key === weekKey ? Number(exists.buy_count_week || 1) : 0
    if (sameWeekCount >= 2) {
      ElMessage.warning('同一自然周最多允许再买 2 次')
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
    side: '买入',
    code,
    name: String(row?.name || exists?.name || focusName.value || ''),
    shares: addShares,
    price: Number.isFinite(tradeCost) ? Number(tradeCost.toFixed(3)) : null,
    before_shares: beforeShares,
    after_shares: afterShares,
    realized_pnl: null,
    reason: '新增持仓'
  })
  saveManualTradeLogs()
  await syncWatchlistHoldingByCode(code)
  ElMessage.success(String(code) + ' 已加入实盘持仓池')
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
    ElMessage.warning('新增持仓时请先输入有效股票代码')
    return
  }
  if (!String(addForm.value.buy_time || '').trim()) {
    ElMessage.warning('请填写买入时间')
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
      { label: '主策略校验：Alpha191 volume5 主候选命中', pass: !!gen2MainHit, reason: gen2MainHit ? ('Alpha191排名 ' + (Number.isFinite(alphaRank) ? String(alphaRank) : '--') + '；' + String(gen2MainHit.reason_text || '')) : '当前标的不在 Alpha191 volume5 主执行候选池' },
      { label: '主策略校验：候选未被熔断/暂停', pass: statusOk, reason: gen2MainHit ? String(gen2MainHit.status_label || gen2MainHit.shadow_status || '候选状态可用') : '候选不存在' },
      { label: '主策略校验：Alpha191 volume5 分数有效', pass: alphaOk, reason: Number.isFinite(alphaScore) ? ('Alpha191 volume5=' + alphaScore.toFixed(4)) : '缺少 Alpha191 volume5 分数' },
      { label: '主策略校验：runup_from_60d_low <= 100%', pass: runupOk, reason: Number.isFinite(runup) ? ('60日低点以来涨幅 ' + runup.toFixed(2) + '%') : '候选未提供涨幅字段，按主候选结果放行' },
      { label: '大盘门槛：上证指数收盘价 >= MA20', pass: marketGatePass, reason: gate.message || '上证指数开仓状态不可用，暂不允许新增持仓' },
      { label: '纪律1：持仓数量上限（最多3只）', pass: manualHoldings.value.length < 3 || !!existing, reason: '当前持仓 ' + String(manualHoldings.value.length) + '/3' },
      { label: '纪律2：同一股票当周最多买入2次', pass: sameWeekCount < 2, reason: '本周已买入 ' + String(sameWeekCount) + ' 次' },
      { label: '纪律3：亏损状态不允许补仓', pass: !(Number.isFinite(existingPnl) && existingPnl < 0), reason: Number.isFinite(existingPnl) && existingPnl < 0 ? ('当前盈亏 ' + existingPnl.toFixed(2) + '%') : '当前无亏损补仓风险' }
    ]
  }
  return [
    { label: '大盘门槛：上证指数收盘价 >= MA20', pass: marketGatePass, reason: gate.message || '上证指数开仓状态不可用，暂不允许新增持仓' },
    { label: '强校验：策略阶段允许开仓', pass: canOpen, reason: payload.value?.decision?.message || '需处于可开仓阶段' },
    { label: '强校验：仅允许主升模式开新仓', pass: mode === 'on', reason: '当前模式 ' + String(mode || '--') + '；退潮/中性阶段禁止新增' },
    { label: '强校验：买入观察池命中', pass: !!buyPoolHit, reason: buyPoolHit ? ('买入池第 ' + String(buyPoolHit.pool_rank || '--') + ' 名；' + String(buyPoolHit.reason_text || '')) : '当前标的不在今日买入观察池' },
    { label: '强校验：动量已经出现', pass: !!buyPoolHit?.momentum_pass, reason: buyPoolHit ? ('5日动量 ' + formatPct(buyPoolHit.mom5_pct) + '；10日动量 ' + formatPct(buyPoolHit.mom10_pct)) : '买入池数据不足' },
    { label: '强校验：V4入场阈值确认', pass: !!buyPoolHit?.adjustment_pass, reason: buyPoolHit ? (String(buyPoolHit.setup_label || '观察') + '；' + String(buyPoolHit.reason_text || '')) : '买入池数据不足' },
    { label: '强校验：候选池命中（主候选或V4前30）', pass: inPool, reason: inPool ? '当前标的命中候选池' : '当前标的不在候选池' },
    { label: '强校验：5日预期达标', pass: backtest5Pass, reason: backtestOk && backtestCount > 0 ? ('5日成功率 ' + (Number.isFinite(h5Win) ? h5Win.toFixed(2) : '--') + '%；日均收益 ' + (Number.isFinite(h5Avg) ? h5Avg.toFixed(2) : '--') + '%（阈值 ' + String(STRONG_CHECK_MIN_WIN_RATE_5D) + '% / ' + String(STRONG_CHECK_MIN_AVG_RETURN_5D) + '%）') : '历史样本不足或回测失败' },
    { label: '强校验：10日预期达标', pass: backtest10Pass, reason: backtestOk && backtestCount > 0 ? ('10日成功率 ' + (Number.isFinite(h10Win) ? h10Win.toFixed(2) : '--') + '%；10日均收益 ' + (Number.isFinite(h10Avg) ? h10Avg.toFixed(2) : '--') + '%（阈值 ' + String(STRONG_CHECK_MIN_WIN_RATE_10D) + '% / ' + String(STRONG_CHECK_MIN_AVG_RETURN_10D) + '%）') : '历史样本不足或回测失败' },
    { label: '纪律1：持仓数量上限（最多3只）', pass: manualHoldings.value.length < 3 || !!existing, reason: '当前持仓 ' + String(manualHoldings.value.length) + '/3' },
    { label: '纪律2：同一股票当周最多买入2次', pass: sameWeekCount < 2, reason: '本周已买入 ' + String(sameWeekCount) + ' 次' },
    { label: '纪律3：亏损状态不允许补仓', pass: !(Number.isFinite(existingPnl) && existingPnl < 0), reason: Number.isFinite(existingPnl) && existingPnl < 0 ? ('当前盈亏 ' + existingPnl.toFixed(2) + '%') : '当前无亏损补仓风险' }
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
    reason: '策略卖出'
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
    ElMessage.warning('持仓索引无效')
    return
  }
  const shares = Math.max(0, Math.trunc(Number(sellForm.value.shares || 0)))
  const maxShares = Math.max(0, Math.trunc(Number(sellForm.value.max_shares || 0)))
  if (!Number.isFinite(shares) || shares <= 0 || shares > maxShares) {
    ElMessage.warning('卖出股数必须介于 1 到可卖股数之间')
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
    side: '卖出',
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
  ElMessage.success(remain <= 0 ? '已清仓并同步' : ('已卖出 ' + String(shares) + ' 股，剩余 ' + String(remain) + ' 股'))
}

const disciplineTagText = (row) => {
  if (isSingleTradeLossCapBreached(row)) return '超阈值'
  if (isTrailingTakeProfitTriggered(row)) return '移动止盈'
  const pnl = Number(row?.pnl_ratio)
  if (Number.isFinite(pnl) && pnl <= -6) return '重度亏损'
  if (Number.isFinite(pnl) && pnl <= -4) return '中度亏损'
  return '轻度波动'
}

const disciplineTagType = (row) => {
  const text = disciplineTagText(row)
  if (text === '重度亏损') return 'danger'
  if (text === '超阈值') return 'warning'
  if (text === '轻度波动') return 'info'
  if (text === '中度亏损') return 'warning'
  return 'success'
}

const rsiBoxTSignal = (row) => row?.signal?.rsi_box_t || null

const rsiBoxTTagText = (row) => {
  const signal = rsiBoxTSignal(row)
  const action = String(signal?.action || '')
  if (!signal) return '未检测'
  if (action === 'sell_half') return '背离减半'
  if (action === 'sell_part') return '跌80减仓'
  if (action === 'buyback_t') return '回20买回'
  if (action === 'wait_buyback') return '超卖等待'
  if (action === 'hold_trend_no_t') return '突破停T'
  if (action === 'risk_control_no_t') return '跌破停T'
  if (signal?.enabled) return '箱内等待'
  return '不做T'
}

const rsiBoxTTagType = (row) => {
  const signal = rsiBoxTSignal(row)
  const action = String(signal?.action || '')
  if (action === 'sell_half' || action === 'sell_part') return 'warning'
  if (action === 'buyback_t') return 'success'
  if (action === 'hold_trend_no_t') return 'primary'
  if (action === 'risk_control_no_t') return 'danger'
  if (action === 'wait_buyback') return 'info'
  if (signal?.enabled) return 'success'
  return 'info'
}

const formatPct = (value) => {
  if (value === null || value === undefined || value === '') return '--'
  const n = Number(value)
  return Number.isFinite(n) ? (n.toFixed(2) + '%') : '--'
}

const formatScore = (value) => {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(4) : '--'
}

const formatRatio = (value) => {
  const n = Number(value)
  return Number.isFinite(n) ? (n.toFixed(2) + '倍') : '--'
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
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%'
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
  if (row?.can_buy) return '可买'
  if (row?.candidate_status === 'primary') return '主池'
  return '观察'
}

const gen2ShadowTagType = (row) => {
  if (row?.shadow_status === 'suspended_by_two_stop_cd3' || row?.shadow_status === 'suspended_by_stop_cd5') return 'danger'
  if (row?.shadow_status === 'executed') return 'success'
  if (row?.shadow_status === 'observable') return 'warning'
  return 'info'
}

const gen2ShadowCanAddHolding = (row) => {
  if (!row || row.is_suspended) return false
  return row.can_buy === true || row.buyable === true || row.stage_label === '可买'
}

const gen2ShadowStatusText = (row) => {
  if (row?.status_label) return row.status_label
  if (row?.shadow_status === 'suspended_by_two_stop_cd3' || row?.shadow_status === 'suspended_by_stop_cd5') return '风控暂停'
  if (row?.shadow_status === 'executed') return '已执行'
  if (row?.shadow_status === 'observable') return '观察中'
  return '未知'
}

const gen2VerificationTagType = (row) => row?.verification_type || 'info'

const gen2VerificationText = (row) => row?.verification_label || '未标记'

const gen2VerificationNoteText = (row) => {
  const parts = []
  const position = Number(row?.verification_position_pct)
  const fill = Number(row?.verification_fill_price)
  if (Number.isFinite(position) && position > 0) parts.push(position.toFixed(0) + '%仓位')
  if (Number.isFinite(fill) && fill > 0) parts.push('成交 ' + fill.toFixed(3))
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
      ElMessage.warning(saved?.error || 'G2 验证记录保存失败')
      return
    }
    row.verification_key = saved.verification_key || row.verification_key
    row.verification_status = saved.status || ''
    row.verification_label = saved.label || '未标记'
    row.verification_type = saved.type || 'info'
    row.verification_note = saved.note || ''
    row.verification_position_pct = saved.position_pct ?? null
    row.verification_fill_price = saved.fill_price ?? null
    row.verification_updated_at = saved.updated_at || ''
    gen2VerificationDialogVisible.value = false
    await fetchGen2RiskCoolShadow(selectedDate.value)
    ElMessage.success('G2 验证记录已保存')
  } catch (err) {
    ElMessage.warning(err?.message || 'G2 验证记录保存失败')
  } finally {
    gen2VerificationSaving.value = false
  }
}

const nowMinuteText = () => {
  const d = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  return String(d.getFullYear()) + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes())
}

const buildTradeMarkerKey = (row) => {
  const normalizedCode = normalizeCode(row?.code)
  return STOCK_DETAIL_TRADE_MARKS_PREFIX + (normalizedCode || 'unknown') + '_' + String(Date.now())
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
    ElMessage.warning('股票代码无效，无法打开详情')
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
      message: '行情服务异常，暂不建议下单'
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
        ? 'G2主图买点池刷新失败，先用旧缓存展示'
        : 'G2主图买点池读取失败，请稍后再试'
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
      message: err?.message || '策略工作流抓取失败，请稍后重试',
      pipeline_stages: [],
      blockers: []
    }
  } finally {
    workflowLoading.value = false
  }
}

const fetchDailyTradeTicket = async (tradeDate) => {
  dailyTradeTicketLoading.value = true
  try {
    const params = { limit: 30, formal_limit: 3, persist: true }
    if (typeof tradeDate === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(tradeDate)) {
      params.signal_date = tradeDate
    }
    dailyTradeTicket.value = await getGen2DailyTradeTicket(params)
  } catch (err) {
    dailyTradeTicket.value = {
      available: false,
      can_open: false,
      formal_candidates: [],
      forbidden_actions: [],
      message: err?.message || 'G2 V4 今日交易单生成失败'
    }
  } finally {
    dailyTradeTicketLoading.value = false
  }
}

const applyDailyExecutionRecord = (record) => {
  dailyExecutionForm.value = {
    execution_status: record?.execution_status || 'pending',
    executed: !!record?.executed,
    discipline_ok: record?.discipline_ok !== false,
    violation_tags: Array.isArray(record?.violation_tags) ? record.violation_tags : [],
    execution_note: record?.execution_note || '',
    t1_review: record?.t1_review || '',
    t3_review: record?.t3_review || '',
    t5_review: record?.t5_review || ''
  }
}

const fetchDailyExecution = async (tradeDate) => {
  dailyExecutionLoading.value = true
  try {
    const params = {}
    if (typeof tradeDate === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(tradeDate)) {
      params.signal_date = tradeDate
    }
    const resp = await getGen2DailyTradeExecution(params)
    dailyExecution.value = resp
    applyDailyExecutionRecord(resp?.record || dailyTradeTicket.value?.execution_record || null)
  } catch (err) {
    dailyExecution.value = {
      available: false,
      record: dailyTradeTicket.value?.execution_record || null,
      message: err?.message || '今日执行台账读取失败'
    }
    applyDailyExecutionRecord(dailyExecution.value.record)
  } finally {
    dailyExecutionLoading.value = false
  }
}

const isBuyTradeSide = (value) => {
  const text = String(value || '').trim().toLowerCase()
  return text === 'buy' || text.includes('买') || text.includes('涔')
}

const isSellTradeSide = (value) => {
  const text = String(value || '').trim().toLowerCase()
  return text === 'sell' || text.includes('卖') || text.includes('鍗')
}

const inferDailyExecutionFromLocalTrades = async () => {
  const signalDate = dailyTradeTicket.value?.signal_date || selectedDate.value
  if (!signalDate || signalDate === '--') {
    ElMessage.warning('缺少交易单信号日，无法自动归因')
    return
  }
  const tradeRows = (manualTradeLogs.value || [])
    .filter((item) => String(item?.time || '').slice(0, 10) === signalDate)
    .map((item) => ({
      ...item,
      code6: normalizeCode(item?.code),
      side_text: String(item?.side || '')
    }))
  const buyRows = tradeRows.filter((item) => isBuyTradeSide(item.side_text))
  const sellRows = tradeRows.filter((item) => isSellTradeSide(item.side_text))
  const formalCodes = new Set(
    (dailyTradeTicketFormalRows.value || [])
      .map((item) => normalizeCode(item?.code || item?.code6))
      .filter(Boolean)
  )
  const illegalBuys = buyRows.filter((item) => !dailyTradeTicket.value?.can_open || !formalCodes.has(item.code6))
  const matchedBuys = buyRows.filter((item) => formalCodes.has(item.code6))
  let status = 'no_trade'
  let disciplineOk = true
  if (illegalBuys.length) {
    status = 'violated'
    disciplineOk = false
  } else if (matchedBuys.length && matchedBuys.length === buyRows.length) {
    status = buyRows.length >= Math.max(1, formalCodes.size || 1) ? 'followed' : 'partial'
  } else if (buyRows.length || sellRows.length) {
    status = 'partial'
  }
  const parts = [
    `auto attribution from local trade logs: signal_date=${signalDate}`,
    `trades=${tradeRows.length}`,
    `buys=${buyRows.length}`,
    `sells=${sellRows.length}`,
    `formal_candidates=${formalCodes.size}`,
    `illegal_buys=${illegalBuys.map((item) => displayCode(item.code6)).join(',') || '-'}`
  ]
  dailyExecutionForm.value = {
    ...dailyExecutionForm.value,
    execution_status: status,
    executed: tradeRows.length > 0,
    discipline_ok: disciplineOk,
    violation_tags: illegalBuys.length ? ['outside_daily_trade_ticket'] : [],
    execution_note: parts.join('; ')
  }
  await saveDailyExecution(status)
}

const saveDailyExecution = async (status) => {
  const signalDate = dailyTradeTicket.value?.signal_date || selectedDate.value
  if (!signalDate || signalDate === '--') {
    ElMessage.warning('缺少交易单信号日，无法保存执行记录')
    return
  }
  dailyExecutionSaving.value = true
  try {
    const payload = {
      signal_date: signalDate,
      strategy_code: dailyTradeTicket.value?.strategy_code || 'g2_v2_complete',
      ...dailyExecutionForm.value
    }
    if (status) {
      payload.execution_status = status
      dailyExecutionForm.value.execution_status = status
    }
    const resp = await saveGen2DailyTradeExecution(payload)
    if (!resp?.ok) {
      ElMessage.warning(resp?.error || '今日执行记录保存失败')
      return
    }
    dailyExecution.value = { available: true, record: resp.record, ledger_path: resp.ledger_path }
    applyDailyExecutionRecord(resp.record)
    await fetchDailyTradeTicket(signalDate)
    ElMessage.success('今日执行记录已保存')
  } catch (err) {
    ElMessage.warning(err?.message || '今日执行记录保存失败')
  } finally {
    dailyExecutionSaving.value = false
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
      ElMessage.success('G2买点数据刷新已提交')
      await fetchGen2RiskCoolShadow(tradeDate)
    } else {
      ElMessage.warning(finalTask?.error || 'G2买点刷新未完成')
    }
  } catch (err) {
    ElMessage.warning(err?.message || 'G2 影子交易更新失败')
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
        last_error: '运行状态读取失败'
      }
  }
}

const fetchGen2StrategyRefreshStatus = async () => {
  gen2StrategyRefreshLoading.value = true
  try {
    gen2StrategyRefreshStatus.value = await getGen2StrategyRefreshStatus()
  } catch (err) {
    gen2StrategyRefreshStatus.value = {
      enabled: false,
      last_error: err?.message || 'G2 30m策略刷新接口未加载，请重启PyCharm后端服务',
      last_result: {
        ok: false,
        error: err?.message || 'strategy-refresh status unavailable'
      }
    }
  } finally {
    gen2StrategyRefreshLoading.value = false
  }
}

const runGen2StrategyRefreshOnce = async () => {
  gen2StrategyRefreshRunning.value = true
  try {
    const result = await runGen2StrategyRefreshNow({ force: true })
    gen2StrategyRefreshStatus.value = {
      ...(gen2StrategyRefreshStatus.value || {}),
      last_result: result,
      last_run_at: result?.finished_at || result?.started_at || gen2StrategyRefreshStatus.value?.last_run_at,
      last_success_at: result?.ok ? (result?.finished_at || gen2StrategyRefreshStatus.value?.last_success_at) : gen2StrategyRefreshStatus.value?.last_success_at,
      last_error: result?.ok === false ? (result?.error || '刷新失败') : null
    }
    await fetchGen2StrategyRefreshStatus()
    if (result?.ok === false) {
      ElMessage.warning(result?.error || 'G2 30m策略刷新失败')
    } else if (result?.skipped) {
      ElMessage.info(result?.reason || 'G2 30m策略刷新已跳过')
    } else {
      ElMessage.success('G2 30m策略刷新已执行')
    }
  } catch (err) {
    ElMessage.warning(err?.message || 'G2 30m策略刷新接口未加载，请重启PyCharm后端服务')
    await fetchGen2StrategyRefreshStatus()
  } finally {
    gen2StrategyRefreshRunning.value = false
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
      message: err?.message || '模拟盘桥接状态抓取失败'
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
      ElMessage.success(submitDryRun ? 'PTrade dry-run 探针通过' : 'PTrade 提交已接受')
    } else {
      ElMessage.warning(result?.message || 'PTrade 探针失败，请检查关联配置')
    }
    await fetchPtradeBridgeState()
  } catch (err) {
    ElMessage.warning(err?.message || 'PTrade 探针执行失败')
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
  return latest || { status: 'timeout', error: 'PTrade 连通性探测超时' }
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
      ElMessage.success('PTrade 连通性通过，可继续做 live-submit 压测')
    } else {
      const nextAction = Array.isArray(result?.next_actions) ? result.next_actions[0] : ''
      ElMessage.warning(finalTask?.error || nextAction || 'PTrade 连通性失败')
    }
    await fetchPtradeBridgeState()
  } catch (err) {
    ElMessage.warning(err?.message || 'PTrade 连通性执行失败')
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
  return latest || { status: 'timeout', error: 'PTrade 观察验收等待超时' }
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
      ElMessage.success('PTrade 等待验收通过')
    } else {
      const nextAction = Array.isArray(result?.next_actions) ? result.next_actions[0] : ''
      ElMessage.warning(finalTask?.error || nextAction || 'PTrade 等待验收失败')
    }
    await fetchPtradeBridgeState()
  } catch (err) {
    ElMessage.warning(err?.message || 'PTrade 等待验收执行失败')
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
  if (!code) return '代码无效'
  if (isSubmittingPaperOrder(row)) return '提交中'
  if (hasActivePaperOrder(row)) return '已有订单'
  const signalDate = String(row?.entry_date || selectedDate.value || '')
  if (!signalDate) return '信号日期缺失'
  const rowDate = String(row?.entry_date || row?.signal_date || row?.trade_date || row?.date || selectedDate.value || '')
  if (!row || normalizeCode(row?.code) !== code || rowDate !== signalDate) return '请刷新标的'
  const gate = marketGate.value || {}
  const gateDate = String(gate.trade_date || gate.signal_date || gate.snapshot_date || gate.date || '')
  if (!gateDate || gateDate !== signalDate || typeof gate.available === 'undefined' || typeof gate.can_open === 'undefined') return '请刷新行情门控'
  if (!gate.available) return '门控不可开'
  if (!gate.can_open) return '仅测试时段'
  return ''
}

const canSubmitPaperOrder = (row) => !paperOrderBlockReason(row)

const paperOrderButtonText = (row) => paperOrderBlockReason(row) || '模拟盘下单'

const submitPaperOrderForRow = async (row) => {
  const code = normalizeCode(row?.code)
  if (!code) {
    ElMessage.warning('缺少股票代码，无法提交模拟盘订单')
    return
  }
  const blockReason = paperOrderBlockReason(row)
  if (blockReason) {
    ElMessage.warning(code + ' ' + blockReason + '，请刷新 G2 实盘页后重试')
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
      ElMessage.success(resp?.message || (code + ' 已提交到模拟盘桥接队列'))
      await fetchPtradeBridgeState()
    } else {
      ElMessage.warning(resp?.message || (code + ' 模拟盘下单失败'))
    }
  } catch (err) {
    ElMessage.warning(err?.message || (code + ' 模拟盘下单失败'))
  } finally {
    paperOrderSubmittingCode.value = ''
  }
}

const importPtradePositionsToManualHoldings = async () => {
  const rows = ptradeLatestPositionRows.value
  if (!rows.length) {
    ElMessage.warning('暂无可导入的模拟盘持仓快照')
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
  ElMessage.success('已导入模拟盘持仓，共 ' + String(manualHoldings.value.length) + ' 只')
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
    ElMessage.success(enabled ? '已开启 G2 15 分钟买点探测' : '已停止 G2 买点探测')
  } catch (err) {
    ElMessage.warning(err?.message || '设置 G2 买点探测失败')
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
    if (resp?.email_sent) ElMessage.success('G2 买点提醒邮件已发送')
    else ElMessage.info(resp?.message || '本次没有新增 G2 买点，未发送邮件')
  } catch (err) {
    ElMessage.warning(err?.message || 'G2 买点探测失败')
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
    await fetchDailyTradeTicket(resp?.selected_date || targetDate.value || undefined)
    await fetchDailyExecution(dailyTradeTicket.value?.signal_date || resp?.selected_date || targetDate.value || undefined)
    await fetchWorkflowStatus(resp?.selected_date || targetDate.value || undefined)
    await fetchGen2StrategyRefreshStatus()
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
    await fetchGen2StrategyRefreshStatus()
    ElMessage.success('已更新快照')
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
.trade-ticket-panel { border-color:#91caff; background: linear-gradient(135deg, #ffffff 0%, #f4f9ff 100%); }
.command-workbench { border-width:1px; }
.ticket-grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:10px; margin: 10px 0 12px; }
.ticket-metric { border:1px solid #d6e4ff; border-radius:8px; padding:10px 12px; background:rgba(255,255,255,0.78); min-height:68px; color:#24355d; }
.ticket-metric.ok { border-left:4px solid #52c41a; }
.ticket-metric.warn { border-left:4px solid #faad14; }
.ticket-metric label { display:block; margin-bottom:6px; color:#66708b; font-size:12px; }
.ticket-metric strong { display:block; font-size:16px; line-height:1.35; word-break:break-word; }
.ticket-discipline { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
.daily-execution-box { margin-top:14px; border-top:1px solid #d6e4ff; padding-top:12px; }
.daily-execution-form { margin-top:8px; }
.daily-execution-form :deep(.el-form-item) { margin-bottom:10px; }
.execution-panel { border-color:#d6e4ff; }
.strategy-refresh-panel { border-color:#b7eb8f; background: linear-gradient(135deg, #ffffff 0%, #f8ffef 100%); }
.refresh-grid { display:grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap:10px; }
.refresh-item { border:1px solid #e4f4cf; border-radius:8px; padding:10px 12px; background:rgba(255,255,255,0.72); color:#24355d; min-height:66px; }
.refresh-item label { display:block; margin-bottom:6px; color:#66708b; font-size:12px; }
.refresh-item strong { display:block; font-size:14px; line-height:1.35; word-break:break-word; }
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
  .refresh-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .ticket-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 720px) {
  .head-card { align-items:flex-start; flex-direction:column; }
  .decision-strip { grid-template-columns: 1fr; }
  .refresh-grid { grid-template-columns: 1fr; }
  .ticket-grid { grid-template-columns: 1fr; }
}
</style>
