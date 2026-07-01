<template>
  <div class="g3-alpha-page">
    <header class="topbar">
      <div class="title-block">
        <div class="eyebrow">第三代定型策略</div>
        <h1>G3 工作台</h1>
        <p>正式实盘路由聚焦机构主升、强势突破、震荡弱势修复和恐慌出清修复；量能续强补位已退役为观察源。</p>
      </div>
      <div class="top-actions">
        <el-button :loading="loading" @click="load(false)">刷新页面</el-button>
        <el-button type="primary" :loading="refreshing" @click="load(true)">运行今日影子刷新</el-button>
      </div>
    </header>

    <el-alert :type="alertType" :closable="false" show-icon :title="statusText" />

    <section class="panel management-panel">
      <div class="panel-head">
        <div>
          <h2>当前持仓 / 退出管理</h2>
          <p>以同花顺真实账户的持仓金额、仓位和可用资金作为买卖决策依据；先处理真实持仓退出，再判断今日是否还有买入容量。</p>
        </div>
        <div class="ops-actions">
          <el-tag :type="realHoldingRows.length ? 'warning' : 'info'" effect="dark">
            真实持仓 {{ realHoldingRows.length }} / 仓位 {{ pct(realAccountPositionPct) }}
          </el-tag>
          <el-button size="small" :loading="brokerLoading" @click="loadBrokerState">刷新真实账户</el-button>
          <el-button size="small" type="primary" plain :loading="brokerHoldingSyncLoading" @click="syncBrokerHoldingsFromThs">同步同花顺持仓</el-button>
        </div>
      </div>
      <div class="summary-list broker-summary">
        <div>
          <span>同花顺真实持仓</span>
          <strong>{{ brokerHoldings.length }}</strong>
          <small>更新时间 {{ brokerPayload?.updated_at || '--' }}{{ brokerCapital.holdings_stale ? ' / 明细待刷新' : '' }}</small>
        </div>
        <div>
          <span>可用资金</span>
          <strong>{{ money(brokerAvailableCash) }}</strong>
          <small>持仓市值 {{ money(brokerHoldingMarketValue) }} / 总资产 {{ money(brokerCapital.total_capital) }}</small>
        </div>
        <div>
          <span>剩余买入容量</span>
          <strong>{{ pct(remainingBuyPositionPct) }}</strong>
          <small>按真实账户仓位测算，现金可买 {{ pct(availableCashPositionPct) }}</small>
        </div>
        <div>
          <span>实时卖出建议</span>
          <strong>{{ exitSummary.urgent_exit_rows || 0 }}</strong>
          <small>清仓/减半持仓数；{{ exitSummary.checked_at || '--' }}</small>
        </div>
      </div>

      <div class="account-subhead">
        <h3>同花顺真实持仓</h3>
        <small>{{ brokerCapital.holdings_stale ? '资金摘要已按同花顺当前窗口校正，持仓明细仍待实时同步刷新' : '只显示同花顺账户当前真实持有的股票' }}</small>
      </div>
      <el-table :data="realHoldingRows" stripe size="small" empty-text="暂无同花顺真实持仓">
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column label="路由" width="126">
          <template #default="{ row }">{{ row.route_label || routeLabel(row.route) }}</template>
        </el-table-column>
        <el-table-column prop="entry_date" label="入场日" width="104" />
        <el-table-column label="仓位" width="82" align="right">
          <template #default="{ row }">{{ pct(row.position_pct || row.slot_pct) }}</template>
        </el-table-column>
        <el-table-column label="入场/参考" width="98" align="right">
          <template #default="{ row }">{{ price(row.entry_price || row.reference_close || row.execution_price) }}</template>
        </el-table-column>
        <el-table-column label="最新价" width="90" align="right">
          <template #default="{ row }">{{ price(row.current_price_for_exit || row.current_price || row.reference_close) }}</template>
        </el-table-column>
        <el-table-column label="实时盈亏" width="98" align="right">
          <template #default="{ row }">
            <el-tag size="small" :type="pnlTagType(row.pnl_ratio_for_exit)">
              {{ signedPct(row.pnl_ratio_for_exit) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="硬止损" width="90" align="right">
          <template #default="{ row }">{{ price(row.hard_stop) }}</template>
        </el-table-column>
        <el-table-column label="结构保护" width="96" align="right">
          <template #default="{ row }">{{ price(row.structure_stop || row.previous_low_stop) }}</template>
        </el-table-column>
        <el-table-column label="止盈一" width="90" align="right">
          <template #default="{ row }">{{ price(row.take_profit_1) }}</template>
        </el-table-column>
        <el-table-column label="纸面" width="86">
          <template #default="{ row }">
            <el-tag size="small" :type="row.paper_recorded ? 'success' : 'warning'">{{ row.paper_recorded ? '已复现' : '待复现' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="动作" width="104">
          <template #default="{ row }">
            <el-tag size="small" :type="managementActionType(row)">{{ row.management_action }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="触发原因" min-width="170" show-overflow-tooltip>
          <template #default="{ row }">{{ exitReasonText(row) }}</template>
        </el-table-column>
        <el-table-column label="退出合同" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">{{ exitContractText(row) }}</template>
        </el-table-column>
      </el-table>

      <div class="account-subhead shadow-holding-subhead">
        <h3>影子盘持仓</h3>
        <small>{{ shadowHoldingExplainText }}</small>
      </div>
      <el-table :data="currentShadowHoldingRows" stripe size="small" empty-text="暂无影子盘持仓">
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column label="路由" width="130">
          <template #default="{ row }">{{ row.route_label || routeLabel(row.route) }}</template>
        </el-table-column>
        <el-table-column label="板块机会" min-width="260" show-overflow-tooltip>
          <template #default="{ row }">{{ opportunityText(row) }}</template>
        </el-table-column>
        <el-table-column label="入场时间" width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ row.planned_entry_ts || row.confirm_datetime || row.entry_date || '--' }}</template>
        </el-table-column>
        <el-table-column label="影子仓位" width="92" align="right">
          <template #default="{ row }">{{ pct(row.position_pct || row.slot_pct) }}</template>
        </el-table-column>
        <el-table-column label="入场/参考" width="98" align="right">
          <template #default="{ row }">{{ price(row.entry_price || row.reference_close || row.execution_price) }}</template>
        </el-table-column>
        <el-table-column label="最新价" width="90" align="right">
          <template #default="{ row }">{{ price(currentShadowPrice(row)) }}</template>
        </el-table-column>
        <el-table-column label="实时盈亏" width="98" align="right">
          <template #default="{ row }">
            <el-tag size="small" :type="pnlTagType(shadowPnlRatio(row))">
              {{ signedPct(shadowPnlRatio(row)) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="硬止损" width="90" align="right">
          <template #default="{ row }">{{ price(row.hard_stop) }}</template>
        </el-table-column>
        <el-table-column label="结构保护" width="96" align="right">
          <template #default="{ row }">{{ price(row.structure_stop || row.previous_low_stop) }}</template>
        </el-table-column>
        <el-table-column label="止盈一" width="90" align="right">
          <template #default="{ row }">{{ price(row.take_profit_1) }}</template>
        </el-table-column>
        <el-table-column label="退出建议" width="112">
          <template #default="{ row }">
            <el-tag size="small" :type="managementActionType(row)">{{ row.management_action }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="触发原因" min-width="170" show-overflow-tooltip>
          <template #default="{ row }">{{ exitReasonText(row) }}</template>
        </el-table-column>
        <el-table-column label="纸面" width="86">
          <template #default="{ row }">
            <el-tag size="small" :type="row.paper_recorded ? 'success' : 'warning'">{{ row.paper_recorded ? '已复现' : '待复现' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="170" show-overflow-tooltip>
          <template #default="{ row }">{{ compactStatusWithZh(row.shadow_status || row.last_state || row.status) }}</template>
        </el-table-column>
        <el-table-column label="退出合同" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">{{ exitContractText(row) }}</template>
        </el-table-column>
      </el-table>

    </section>

    <section class="panel action-panel">
      <div class="panel-head">
        <div>
          <h2>买入建议</h2>
          <p>买入建议按当前策略候选批次展示；先用真实账户回答还能买多少，最终买入仓位受真实持仓、现金和交易时点约束。</p>
        </div>
        <el-tag :type="realAccountBuyCapacityType" effect="dark">
          {{ realAccountBuyCapacityText }}
        </el-tag>
      </div>
      <div class="date-analysis-strip">
        <el-tag size="small" :type="dateDisplayTagType">{{ dateDisplayStatusText }}</el-tag>
        <span>{{ dateDisplayAnalysis.message || shadowTicketTableExplain }}</span>
      </div>

      <div v-if="selectedTicket.code" class="ticket-detail">
        <div class="ticket-main">
          <span class="route-chip">{{ selectedTicket.route_label || routeLabel(selectedTicket.route) }}</span>
          <h3>{{ selectedTicket.name }} <small>{{ selectedTicket.code }}</small></h3>
          <div class="ticket-sub">
            <span>计划时间 {{ plannedEntryDisplayText(selectedTicket) }}</span>
            <span>确认时间 {{ selectedTicket.confirm_datetime || '--' }}</span>
            <span>入场方式 {{ entryPolicyText(selectedTicket) }}</span>
          </div>
          <div class="decision-note">{{ opportunityText(selectedTicket) }}</div>
          <div class="decision-note">{{ selectedTicketExplainText }}</div>
          <div class="decision-note">{{ naturalDisciplineText(selectedTicket) }}</div>
        </div>
        <div class="risk-strip">
          <div>
            <span>合同仓位</span>
            <strong>{{ pct(selectedTicket.position_pct) }}</strong>
          </div>
          <div>
            <span>可买仓位</span>
            <strong>{{ pct(selectedTicketExecutablePositionPct) }}</strong>
          </div>
          <div>
            <span>参考价</span>
            <strong>{{ price(selectedTicket.reference_close) }}</strong>
          </div>
          <div>
            <span>结构止损</span>
            <strong>{{ price(selectedTicket.structure_stop) }}</strong>
          </div>
          <div>
            <span>硬止损</span>
            <strong>{{ price(selectedTicket.hard_stop) }}</strong>
          </div>
          <div>
            <span>止盈一</span>
            <strong>{{ price(selectedTicket.take_profit_1) }}</strong>
          </div>
        </div>
        <div class="exit-contract">{{ exitContractText(selectedTicket) }}</div>
        <div class="paper-execution-bar">
          <div>
            <span>真实账户执行依据</span>
            <strong>{{ realAccountBuyCapacityText }}</strong>
            <small>{{ realAccountBuyCapacityReason }}</small>
          </div>
          <el-button
            type="primary"
            :loading="paperSubmitting"
            :disabled="!canSubmitPaperOrder"
            @click="submitSelectedPaperOrder"
          >
            {{ selectedPaperExecution ? '已记录纸面执行' : '记录纸面执行' }}
          </el-button>
        </div>
      </div>

      <el-empty v-else description="暂无合格买入建议票据" />

      <div class="account-subhead shadow-subhead">
        <h3>{{ shadowTicketTableTitle }}</h3>
        <small>{{ shadowTicketTableExplain }}</small>
      </div>
      <el-table :data="displayShadowTickets" stripe size="small" empty-text="暂无买入建议票据">
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column prop="route_label" label="路由" width="120" />
        <el-table-column label="板块机会" min-width="280" show-overflow-tooltip>
          <template #default="{ row }">{{ opportunityText(row) }}</template>
        </el-table-column>
        <el-table-column label="计划时间" width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ plannedEntryDisplayText(row) }}</template>
        </el-table-column>
        <el-table-column label="合同仓位" width="92" align="right">
          <template #default="{ row }">{{ pct(row.position_pct) }}</template>
        </el-table-column>
        <el-table-column label="自然纪律" width="132">
          <template #default="{ row }">
            <el-tag size="small" :type="naturalActionTagType(row)">
              {{ row.natural_action_label || '观察' }}
            </el-tag>
            <small class="natural-position">{{ pct(row.natural_position_pct) }}</small>
          </template>
        </el-table-column>
        <el-table-column label="可买仓位" width="92" align="right">
          <template #default="{ row }">{{ pct(executablePositionPct(row)) }}</template>
        </el-table-column>
        <el-table-column label="参考价" width="90" align="right">
          <template #default="{ row }">{{ price(row.reference_close) }}</template>
        </el-table-column>
        <el-table-column label="结构止损" width="96" align="right">
          <template #default="{ row }">{{ price(row.structure_stop) }}</template>
        </el-table-column>
        <el-table-column label="硬止损" width="90" align="right">
          <template #default="{ row }">{{ price(row.hard_stop) }}</template>
        </el-table-column>
        <el-table-column label="30m" width="84">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.m30_confirmed) ? 'success' : 'danger'">
              {{ m30Text(row) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="190" show-overflow-tooltip>
          <template #default="{ row }">{{ compactStatusWithZh(row.shadow_status) }}</template>
        </el-table-column>
        <el-table-column label="自然说明" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">{{ naturalDisciplineText(row) }}</template>
        </el-table-column>
      </el-table>

    </section>

    <section class="panel ops-panel">
      <div class="panel-head">
        <div>
          <h2>运行闭环</h2>
          <p>迁移 G2 已成熟的运行能力：状态检查、阻断诊断、影子监控、手动刷新任务和运行记录。</p>
        </div>
        <div class="ops-actions">
          <el-button size="small" :loading="opsLoading" @click="loadOps">刷新运行状态</el-button>
          <el-button size="small" type="primary" :loading="opsRunLoading" @click="runOpsOnce">立即运行 G3 影子刷新</el-button>
          <el-button size="small" type="warning" plain :loading="opsNotifyLoading" @click="runNotifyProbe">立即探测提醒</el-button>
          <el-button size="small" :loading="opsConfigLoading" @click="toggleMonitor">
            {{ monitorState.enabled ? '暂停监控' : '开启监控' }}
          </el-button>
        </div>
      </div>
      <div class="ops-grid">
        <div class="ops-card" :class="workflowStatus.ok ? 'ok' : 'blocked'">
          <span>工作流状态</span>
          <strong>{{ workflowStatus.ok ? '可运行' : '有阻断' }}</strong>
          <small>{{ workflowStatus.message || '--' }}</small>
        </div>
        <div class="ops-card">
          <span>影子监控</span>
          <strong>{{ monitorState.enabled ? '开启' : '暂停' }}</strong>
          <small>{{ monitorState.interval_seconds || 120 }} 秒 / {{ monitorState.trading_hours_only ? '交易时段' : '全天' }}</small>
        </div>
        <div class="ops-card">
          <span>最后成功</span>
          <strong>{{ monitorState.last_success_at || '--' }}</strong>
          <small>最后运行 {{ monitorState.last_run_at || '--' }}</small>
        </div>
        <div class="ops-card" :class="workflowBlockers.length ? 'blocked' : 'ok'">
          <span>阻断项</span>
          <strong>{{ workflowBlockers.length }}</strong>
          <small>{{ workflowBlockers[0]?.message || '暂无阻断' }}</small>
        </div>
        <div class="ops-card">
          <span>刷新任务</span>
          <strong>{{ statusWithZh(refreshTask.status) }}</strong>
          <small>{{ refreshTask.message || refreshTask.task_id || '--' }}</small>
        </div>
        <div class="ops-card" :class="monitorState.last_email_error ? 'blocked' : 'ok'">
          <span>影子提醒</span>
          <strong>{{ monitorState.email_enabled === false ? '关闭' : '开启' }}</strong>
          <small>最近邮件 {{ monitorState.last_email_sent_at || '--' }}</small>
        </div>
        <div class="ops-card" :class="monitorState.last_email_error ? 'blocked' : 'ok'">
          <span>邮件异常</span>
          <strong>{{ monitorState.last_email_error ? '有异常' : '正常' }}</strong>
          <small>{{ monitorState.last_email_error || '暂无异常' }}</small>
        </div>
        <div class="ops-card locked">
          <span>G2 补位状态</span>
          <strong>{{ parityCount }}/{{ parityTotal }}</strong>
          <small>已退出实盘交易，仅保留观察</small>
        </div>
      </div>
      <el-table :data="workflowChecks" stripe size="small" empty-text="暂无运行检查">
        <el-table-column prop="name" label="检查项" min-width="150" />
        <el-table-column label="状态" width="88">
          <template #default="{ row }">
            <el-tag size="small" :type="row.ok ? 'success' : 'danger'">{{ row.ok ? '通过' : '阻断' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="message" label="说明" min-width="240" show-overflow-tooltip />
        <el-table-column prop="modified_at" label="更新时间" width="170" />
      </el-table>
      <el-table :data="recentEvents" stripe size="small" class="ops-events-table" empty-text="暂无运行事件">
        <el-table-column prop="created_at" label="时间" width="170" />
        <el-table-column prop="type" label="类型" width="110" />
        <el-table-column prop="status" label="状态" width="100" />
        <el-table-column label="邮件" width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="row.email_sent ? 'success' : 'info'">{{ row.email_sent ? '已发' : '未发' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="message" label="说明" min-width="220" show-overflow-tooltip />
        <el-table-column prop="error" label="异常" min-width="220" show-overflow-tooltip />
      </el-table>
    </section>

    <section class="panel observation-panel">
      <div class="panel-head">
        <div>
          <h2>G3 融合观察</h2>
          <p>每天用 workflow、盘前烟测、30m 确认和纸面执行台账做一次验收；G2 补位只保留源和历史归因观察，不再作为实盘买入路线。</p>
        </div>
        <div class="ops-actions">
          <el-button size="small" :loading="observationLoading" @click="loadObservations">刷新观察</el-button>
          <el-button size="small" type="primary" :loading="observationRunLoading" @click="runObservationOnce">
            记录今日观察
          </el-button>
        </div>
      </div>
      <div class="ops-grid">
        <div class="ops-card" :class="acceptedObservationDays >= 30 ? 'ok' : 'blocked'">
          <span>验收观察日</span>
          <strong>{{ acceptedObservationDays }}/30</strong>
          <small>按当前 2槽/50% 合同重新累计</small>
        </div>
        <div class="ops-card" :class="latestObservation?.accepted ? 'ok' : 'blocked'">
          <span>最近快照</span>
          <strong>{{ latestObservation?.status || '--' }}</strong>
          <small>{{ latestObservation?.observation_date || '--' }}</small>
        </div>
        <div class="ops-card" :class="latestObservation?.smoke_ok ? 'ok' : 'blocked'">
          <span>盘前烟测</span>
          <strong>{{ latestObservation?.smoke_verdict || '--' }}</strong>
          <small>{{ latestObservation?.smoke_trade_action || '--' }}</small>
        </div>
        <div class="ops-card" :class="latestObservation?.paper_ok ? 'ok' : 'blocked'">
          <span>纸面复现</span>
          <strong>{{ latestObservation?.paper_ok ? '通过' : '待补' }}</strong>
          <small>缺失 {{ latestObservation?.missing_paper_count ?? 0 }} 笔</small>
        </div>
        <div class="ops-card" :class="observationScheduler.scheduler_enabled ? 'ok' : 'blocked'">
          <span>自动观察</span>
          <strong>{{ observationScheduler.enabled ? '开启' : '关闭' }}</strong>
          <small>{{ observationScheduler.hour ?? 15 }}:{{ String(observationScheduler.minute ?? 40).padStart(2, '0') }}</small>
        </div>
        <div class="ops-card">
          <span>下次验收</span>
          <strong>{{ observationScheduler.next_run_time || '--' }}</strong>
          <small>{{ observationScheduler.last_error || '自动积累观察日' }}</small>
        </div>
      </div>
      <el-table :data="observationSnapshots" stripe size="small" class="ops-events-table" empty-text="暂无 G3 观察快照">
        <el-table-column prop="observation_date" label="观察日" width="110" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.accepted ? 'success' : 'danger'">{{ row.accepted ? '通过' : '阻断' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="failure_category" label="分类" width="150" show-overflow-tooltip />
        <el-table-column prop="action_required" label="动作" width="170" show-overflow-tooltip />
        <el-table-column prop="smoke_verdict" label="烟测" width="130" />
        <el-table-column prop="qualified_ticket_count" label="票据" width="80" align="right" />
        <el-table-column label="30m" width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="row.m30_ok ? 'success' : 'danger'">{{ row.m30_ok ? '通过' : '阻断' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="修复" width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="row.minute30_repair_triggered ? 'warning' : 'info'">
              {{ row.minute30_repair_triggered ? '已触发' : '未触发' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="纸面" width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="row.paper_ok ? 'success' : 'danger'">{{ row.paper_ok ? '通过' : '阻断' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="记录时间" width="170" />
        <el-table-column label="阻断原因" min-width="260" show-overflow-tooltip>
          <template #default="{ row }">{{ observationBlockerText(row) }}</template>
        </el-table-column>
      </el-table>
    </section>

    <section class="metric-grid">
      <div class="metric-card">
        <span>入场交易日</span>
        <strong>{{ summary.entry_date || '--' }}</strong>
        <small>决策日 {{ summary.decision_date || '--' }}</small>
      </div>
      <div class="metric-card accent">
        <span>合格买入候选</span>
        <strong>{{ summary.qualified_shadow_buy_rows ?? 0 }}</strong>
        <small>票据 {{ shadowTickets.length }} / 来源 {{ summary.source_rows ?? 0 }}</small>
      </div>
      <div class="metric-card">
        <span>今日选中路由</span>
        <strong>{{ routeLabel(summary.selected_route) }}</strong>
        <small>{{ summary.route_reason || '--' }}</small>
      </div>
      <div class="metric-card">
        <span>全周期回测</span>
        <strong>{{ pct(backtestProfile.total_return) }}</strong>
        <small>{{ backtestProfileText }}</small>
      </div>
      <div class="metric-card">
        <span>交易样本</span>
        <strong>{{ fmt(backtestProfile.trade_count, 0) }}</strong>
        <small>胜率 {{ pct(backtestProfile.win_rate) }}</small>
      </div>
      <div class="metric-card" :class="g2SupplementStatus.ok ? 'ok' : 'blocked'">
        <span>G2补位源</span>
        <strong>{{ g2SupplementStatus.live_enabled ? '启用' : '已退役' }}</strong>
        <small>{{ g2SupplementStatusText }}</small>
      </div>
      <div class="metric-card accent">
        <span>默认合同</span>
        <strong>2槽 / 50%</strong>
        <small>四条实盘路由；G2补位仅观察</small>
      </div>
      <div class="metric-card">
        <span>账户风控</span>
        <strong>{{ summary.account_risk?.action || 'normal' }}</strong>
        <small>{{ summary.account_risk?.reason || '按影子台账判断' }}</small>
      </div>
      <div class="metric-card locked">
        <span>正式交易</span>
        <strong>锁定</strong>
        <small>正式买点与自动下单均关闭</small>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head table-head">
        <div>
          <h2>候选池入口</h2>
          <p>工作台只展示候选概览；全量候选、逐日复盘和阻断明细进入候选池页。</p>
        </div>
      </div>
      <div class="summary-list three">
        <div>
          <span>全量候选</span>
          <strong>{{ candidates.length }}</strong>
          <small>来自全部路由</small>
        </div>
        <div>
          <span>合格候选</span>
          <strong>{{ eligibleCandidateCount }}</strong>
          <small>可进入影子选择</small>
        </div>
        <div>
          <span>阻断候选</span>
          <strong>{{ blockedCandidateCount }}</strong>
          <small>进入候选池页看原因</small>
        </div>
      </div>
    </section>

    <section class="two-col">
      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>影子台账</h2>
            <p>同一票据只保留最新状态，用于后续补成交、退出和复盘。</p>
          </div>
        </div>
        <el-table :data="shadowLedger" stripe size="small" empty-text="暂无影子台账">
          <el-table-column prop="entry_date" label="入场日" width="112" />
          <el-table-column prop="code" label="代码" width="110" />
          <el-table-column prop="name" label="名称" min-width="110" />
          <el-table-column label="状态" width="190" show-overflow-tooltip>
            <template #default="{ row }">{{ compactStatusWithZh(row.shadow_status) }}</template>
          </el-table-column>
          <el-table-column label="阶段" width="140" show-overflow-tooltip>
            <template #default="{ row }">{{ compactStatusWithZh(row.last_state) }}</template>
          </el-table-column>
        </el-table>
      </div>

      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>数据产物</h2>
            <p>页面所有结论都要能追到运行产物，方便审计一致性。</p>
          </div>
        </div>
        <el-table :data="artifactRows" stripe size="small" empty-text="暂无产物信息">
          <el-table-column prop="name" label="产物" min-width="150" />
          <el-table-column label="状态" width="86">
            <template #default="{ row }">
              <el-tag size="small" :type="row.exists ? 'success' : 'danger'">{{ row.exists ? '存在' : '缺失' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="modified_at" label="更新时间" width="170" />
          <el-table-column prop="path" label="路径" min-width="320" show-overflow-tooltip />
        </el-table>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { compactStatusWithZh, statusWithZh } from '@/utils/g3StatusText'
import {
  getGen3StateAlphaCurrent,
  getGen3StateAlphaBrokerHoldings,
  getGen3StateAlphaBrokerTrades,
  getGen3StateAlphaObservationSnapshots,
  getGen3StateAlphaPaperExecutions,
  getGen3StateAlphaRefreshTask,
  getGen3StateAlphaWorkflowStatus,
  syncGen3StateAlphaBrokerHoldingsFromThs,
  syncGen3StateAlphaBrokerTradesFromThs,
  runGen3StateAlphaObservationSchedulerOnce,
  runGen3StateAlphaRefreshOnce,
  runGen3StateAlphaShadowMonitorOnce,
  setGen3StateAlphaShadowMonitorConfig,
  submitGen3StateAlphaPaperOrder
} from '@/api/trading'

const loading = ref(false)
const refreshing = ref(false)
const opsLoading = ref(false)
const opsRunLoading = ref(false)
const opsNotifyLoading = ref(false)
const opsConfigLoading = ref(false)
const paperSubmitting = ref(false)
const observationLoading = ref(false)
const observationRunLoading = ref(false)
const brokerLoading = ref(false)
const brokerHoldingSyncLoading = ref(false)
const brokerTradeSyncLoading = ref(false)
const payload = ref(null)
const workflowPayload = ref(null)
const refreshTask = ref({})
const paperExecutions = ref([])
const brokerPayload = ref(null)
const brokerTradesPayload = ref(null)
const observationPayload = ref(null)
const candidateRoute = ref('all')

const summary = computed(() => payload.value?.summary || {})
const shadowTickets = computed(() => Array.isArray(payload.value?.shadow_tickets) ? payload.value.shadow_tickets : [])
const afterhoursSummary = computed(() => payload.value?.afterhours_summary || {})
const afterhoursShadowTickets = computed(() => Array.isArray(payload.value?.afterhours_shadow_tickets) ? payload.value.afterhours_shadow_tickets : [])
const dateDisplayAnalysis = computed(() => payload.value?.date_display_analysis || {})
const nextTradeBuySummary = computed(() => payload.value?.next_trade_buy_summary || afterhoursSummary.value || {})
const nextTradeBuyTickets = computed(() => {
  const rows = Array.isArray(payload.value?.next_trade_buy_tickets)
    ? payload.value.next_trade_buy_tickets
    : afterhoursShadowTickets.value
  return rows
})
const selectedTicket = computed(() => nextTradeBuyTickets.value[0] || {})
const displayShadowTickets = computed(() => nextTradeBuyTickets.value)
const shadowLedger = computed(() => Array.isArray(payload.value?.shadow_ledger) ? payload.value.shadow_ledger : [])
const routeDiagnostics = computed(() => Array.isArray(payload.value?.route_diagnostics) ? payload.value.route_diagnostics : [])
const candidates = computed(() => Array.isArray(payload.value?.all_source_candidates) ? payload.value.all_source_candidates : [])
const eligibleCandidateCount = computed(() => candidates.value.filter((item) => truthy(item.router_eligible || item.qualified_shadow_buy)).length)
const blockedCandidateCount = computed(() => candidates.value.filter((item) => !truthy(item.router_eligible || item.qualified_shadow_buy) || item.block_reason).length)
const backtestProfile = computed(() => payload.value?.backtest_snapshot?.profile || {})
const backtestWindows = computed(() => Array.isArray(payload.value?.backtest_snapshot?.windows) ? payload.value.backtest_snapshot.windows : [])
const routeAttribution = computed(() => Array.isArray(payload.value?.backtest_snapshot?.route_attribution) ? payload.value.backtest_snapshot.route_attribution : [])
const workflowStatus = computed(() => workflowPayload.value || {})
const monitorState = computed(() => workflowPayload.value?.monitor || {})
const workflowChecks = computed(() => Array.isArray(workflowPayload.value?.pipeline_checks) ? workflowPayload.value.pipeline_checks : [])
const workflowBlockers = computed(() => Array.isArray(workflowPayload.value?.blockers) ? workflowPayload.value.blockers : [])
const recentEvents = computed(() => Array.isArray(workflowPayload.value?.recent_events) ? workflowPayload.value.recent_events : [])
const observationSnapshots = computed(() => Array.isArray(observationPayload.value?.snapshots) ? observationPayload.value.snapshots : [])
const brokerHoldings = computed(() => Array.isArray(brokerPayload.value?.holdings) ? brokerPayload.value.holdings : [])
const brokerTrades = computed(() => Array.isArray(brokerTradesPayload.value?.broker_trades) ? brokerTradesPayload.value.broker_trades : [])
const brokerCapital = computed(() => brokerPayload.value?.capital || {})
const exitManagement = computed(() => payload.value?.exit_management || {})
const exitSummary = computed(() => exitManagement.value?.summary || brokerPayload.value?.exit_advice_summary || {})
const filteredPaperExecutions = computed(() => paperExecutions.value.slice(0, 30))
const brokerHoldingMarketValue = computed(() => {
  const explicitValue = Number(brokerCapital.value.holding_market_value ?? brokerCapital.value.market_value)
  if (Number.isFinite(explicitValue)) return explicitValue
  const total = brokerHoldings.value.reduce((sum, item) => {
    const marketValue = Number(item?.market_value)
    return Number.isFinite(marketValue) ? sum + marketValue : sum
  }, 0)
  return total > 0 ? total : null
})
const brokerAvailableCash = computed(() => {
  const availableCash = Number(brokerCapital.value.available_cash)
  if (brokerCapital.value.available_cash !== null && brokerCapital.value.available_cash !== undefined && Number.isFinite(availableCash)) {
    return availableCash
  }
  const totalCapital = Number(brokerCapital.value.total_capital)
  const holdingMarketValue = Number(brokerHoldingMarketValue.value)
  if (Number.isFinite(totalCapital) && Number.isFinite(holdingMarketValue)) {
    return Math.max(totalCapital - holdingMarketValue, 0)
  }
  return null
})
const realAccountPositionPct = computed(() => {
  const totalCapital = Number(brokerCapital.value.total_capital)
  const holdingMarketValue = Number(brokerHoldingMarketValue.value)
  if (Number.isFinite(totalCapital) && totalCapital > 0 && Number.isFinite(holdingMarketValue)) {
    return Math.min(Math.max(holdingMarketValue / totalCapital, 0), 1)
  }
  const totalPct = realHoldingRows.value.reduce((sum, item) => {
    const pctValue = Number(item?.position_pct || item?.slot_pct)
    return Number.isFinite(pctValue) ? sum + pctValue : sum
  }, 0)
  return Math.min(Math.max(totalPct, 0), 1)
})
const availableCashPositionPct = computed(() => {
  const totalCapital = Number(brokerCapital.value.total_capital)
  const availableCash = Number(brokerAvailableCash.value)
  if (!Number.isFinite(totalCapital) || totalCapital <= 0 || !Number.isFinite(availableCash)) return 0
  return Math.min(Math.max(availableCash / totalCapital, 0), 1)
})
const remainingBuyPositionPct = computed(() => {
  const byPosition = Math.max(1 - realAccountPositionPct.value, 0)
  return Math.min(byPosition, availableCashPositionPct.value)
})
const selectedTicketExecutablePositionPct = computed(() => executablePositionPct(selectedTicket.value))
const realAccountBuyCapacityText = computed(() => {
  if (!displayShadowTickets.value.length) return '暂无买入候选'
  if (remainingBuyPositionPct.value <= 0.001) return '真实账户仓位已满'
  if (selectedTicketExecutablePositionPct.value <= 0.001) return '现金不足'
  return `可买 ${pct(selectedTicketExecutablePositionPct.value)}`
})
const realAccountBuyCapacityReason = computed(() => {
  const totalCapitalText = money(brokerCapital.value.total_capital)
  const cashText = money(brokerAvailableCash.value)
  return `真实仓位 ${pct(realAccountPositionPct.value)}，可用资金 ${cashText}，总资产 ${totalCapitalText}`
})
const realAccountBuyCapacityType = computed(() => (
  selectedTicketExecutablePositionPct.value > 0.001 ? 'warning' : 'info'
))
const acceptedObservationDays = computed(() => Number(observationPayload.value?.accepted_observation_days || 0))
const latestObservation = computed(() => observationSnapshots.value[0] || null)
const observationScheduler = computed(() => observationPayload.value?.scheduler || workflowPayload.value?.observation_scheduler || {})
const parityCount = computed(() => {
  const parity = workflowPayload.value?.g2_parity || {}
  return Object.values(parity).filter(Boolean).length
})
const parityTotal = computed(() => {
  const parity = workflowPayload.value?.g2_parity || {}
  return Object.values(parity).length || 0
})

const finalProfileName = computed(() => (
  summary.value.final_profile_name
  || payload.value?.strategy_contract?.profile_name
  || 'G3最终版：二槽主升 + G2空档补位'
))
const currentTicketDate = computed(() => shadowTickets.value[0]?.entry_date || summary.value.entry_date || '--')
const afterhoursTicketDate = computed(() => afterhoursShadowTickets.value[0]?.entry_date || afterhoursSummary.value.entry_date || '')
const nextTradeTicketDate = computed(() => (
  nextTradeBuyTickets.value[0]?.entry_date
  || dateDisplayAnalysis.value.display_entry_date
  || dateDisplayAnalysis.value.expected_next_trade_date
  || nextTradeBuySummary.value.entry_date
  || afterhoursTicketDate.value
  || ''
))
const shadowTicketTableTitle = computed(() => (
  nextTradeTicketDate.value ? `${nextTradeTicketDate.value} 买入建议票据` : '买入建议票据'
))
const shadowTicketTableExplain = computed(() => {
  if (nextTradeBuyTickets.value.length) {
    const currentDate = currentTicketDate.value
    const latestDate = nextTradeTicketDate.value || '--'
    return `这里展示 ${latestDate} 当前应重点关注的买入建议；候选日期用于标记批次，非交易时段不会自动改写当前持仓。当前影子台账日期 ${currentDate}。`
  }
  return dateDisplayAnalysis.value.message || '当前没有独立的买入建议；这里不会用正在持有的影子仓位兜底。'
})
const dateDisplayStatusText = computed(() => {
  const status = dateDisplayAnalysis.value.status || ''
  const date = dateDisplayAnalysis.value.display_entry_date || dateDisplayAnalysis.value.expected_next_trade_date || nextTradeTicketDate.value || '--'
  if (status === 'ready') return `${date} 候选有效`
  if (status === 'filtered_non_trading') return '已过滤非交易日'
  if (status === 'date_note') return '日期提示'
  if (status === 'date_mismatch') return '日期提示'
  return `${date} 暂无候选`
})
const dateDisplayTagType = computed(() => {
  const status = dateDisplayAnalysis.value.status || ''
  if (status === 'ready') return 'success'
  if (status === 'filtered_non_trading' || status === 'date_note' || status === 'date_mismatch') return 'warning'
  return 'info'
})
const shadowHoldingExplainText = computed(() => {
  const holdingNames = currentShadowHoldingRows.value.map((item) => item.name || item.code).filter(Boolean).join('、') || '暂无'
  const latestNames = nextTradeBuyTickets.value.map((item) => item.name || item.code).filter(Boolean).join('、')
  if (nextTradeBuyTickets.value.length) {
    return `当前影子持仓来自 ${currentTicketDate.value} 台账：${holdingNames}；盘后最新候选是 ${latestNames}，候选变化不等于已换仓。`
  }
  return '记录当前仍在持有的影子交易，仅用于跟踪信号合同，不参与真实账户仓位和可用资金计算。'
})
const selectedTicketExplainText = computed(() => tradeDecisionExplain(selectedTicket.value))
const backtestProfileText = computed(() => {
  const model = backtestProfile.value.model || backtestProfile.value.profile || 'g3_final_with_g2_gap_supplement'
  return `最新正式G3合同 ${model}，最大回撤 ${pct(backtestProfile.value.max_drawdown)}`
})
const primaryTickets = computed(() => shadowTickets.value.filter((item) => item.route !== 'g2_gap_supplement'))
const g2SupplementTickets = computed(() => shadowTickets.value.filter((item) => item.route === 'g2_gap_supplement'))
const g2SupplementCandidates = computed(() => candidates.value.filter((item) => item.route === 'g2_gap_supplement'))
const g2SupplementDiagnostic = computed(() => routeDiagnostics.value.find((item) => item.route === 'g2_gap_supplement') || null)
const g2SupplementStatus = computed(() => summary.value.g2_gap_supplement_status || {})
const g2SupplementStatusText = computed(() => {
  const status = g2SupplementStatus.value || {}
  if (status.message) return status.message
  const requested = status.requested_entry_date || summary.value.entry_date || '--'
  const latest = status.latest_source_date || status.latest_live_update_date || status.latest_ledger_entry_date || '--'
  return `请求 ${requested} / 最新 ${latest}`
})
const g2SupplementEnabled = computed(() => (
  truthy(summary.value.g2_gap_supplement_enabled)
  || truthy(summary.value.g2_gap_supplement_live_enabled)
))
const g2SupplementRows = computed(() => Number(
  summary.value.g2_gap_supplement_rows
  ?? g2SupplementDiagnostic.value?.rows
  ?? g2SupplementCandidates.value.length
  ?? 0
))
const g2SupplementCandidateCount = computed(() => Number(
  g2SupplementDiagnostic.value?.eligible_rows
  ?? g2SupplementCandidates.value.filter((item) => truthy(item.router_eligible)).length
  ?? 0
))
const latestCandidateByCode = computed(() => {
  const rows = new Map()
  candidates.value.forEach((item) => {
    const code = String(item?.code || item?.code_raw || '').toUpperCase()
    if (!code) return
    const prev = rows.get(code)
    const itemTime = String(item?.trade_date || item?.date || item?.confirm_datetime || item?.created_at || '')
    const prevTime = String(prev?.trade_date || prev?.date || prev?.confirm_datetime || prev?.created_at || '')
    if (!prev || itemTime >= prevTime) rows.set(code, item)
  })
  return rows
})
const duplicateSkipText = computed(() => {
  const codeSkips = Number(summary.value.skipped_duplicate_code || 0)
  const sectorSkips = Number(summary.value.skipped_duplicate_sector || 0)
  if (!codeSkips && !sectorSkips) return '无'
  return `代码${codeSkips} / 板块${sectorSkips}`
})
const fusionRows = computed(() => {
  const selectedRows = shadowTickets.value.map((item) => ({
    ...item,
    fusion_role: item.route === 'g2_gap_supplement' ? '补位票' : '主路由票'
  }))
  const supplementRows = g2SupplementCandidates.value
    .filter((item) => !selectedRows.some((selected) => (selected.code || selected.code_raw) === (item.code || item.code_raw)))
    .slice(0, 20)
    .map((item) => ({
      ...item,
      fusion_role: truthy(item.router_eligible) ? '候选补位' : '未补原因'
    }))
  return [...selectedRows, ...supplementRows]
})

const selectedTicketKey = computed(() => (
  selectedTicket.value?.ticket_key
  || selectedTicket.value?.candidate_key
  || selectedTicket.value?.trade_key
  || ''
))

const selectedPaperExecution = computed(() => {
  const key = String(selectedTicketKey.value || '')
  const code = String(selectedTicket.value?.code || selectedTicket.value?.code_raw || '').toUpperCase()
  const entryDate = String(selectedTicket.value?.entry_date || '')
  return paperExecutions.value.find((item) => {
    if (key && item.ticket_key === key) return true
    return code && entryDate && String(item.code || '').toUpperCase() === code && item.entry_date === entryDate
  }) || null
})

const normalizeManagementRow = (raw, source) => {
  const paper = findPaperExecution(raw)
  const code = String(raw?.code || raw?.code_raw || '').toUpperCase()
  return {
    ...raw,
    code,
    entry_date: String(raw?.entry_date || raw?.signal_date || ''),
    paper_recorded: !!paper || source === 'broker' || truthy(raw?.paper_recorded),
    management_action: raw?.exit_action_label || raw?.management_action || managementAction(raw, paper),
    pnl_ratio_for_exit: raw?.pnl_ratio_for_exit ?? shadowPnlRatio(raw)
  }
}

const realHoldingRows = computed(() => (
  brokerHoldings.value
    .filter((item) => item?.code || item?.code_raw)
    .map((item) => normalizeManagementRow({ ...item, paper_recorded: true }, 'broker'))
))

const latestShadowLedgerCreatedAt = computed(() => {
  const times = shadowLedger.value
    .map((item) => String(item?.created_at || '').trim())
    .filter((value) => value && isShadowLedgerSessionCreatedAt(value))
    .sort()
  if (times.length) return times[times.length - 1]
  const fallbackTimes = shadowLedger.value
    .map((item) => String(item?.created_at || '').trim())
    .filter(Boolean)
    .sort()
  return fallbackTimes[fallbackTimes.length - 1] || ''
})

const currentShadowHoldingRows = computed(() => (
  shadowLedger.value
    .filter((item) => isCurrentShadowHolding(item))
    .map((item) => normalizeManagementRow(item, 'shadow'))
))

const canSubmitPaperOrder = computed(() => (
  !!selectedTicket.value?.code
  && truthy(selectedTicket.value?.qualified_shadow_buy)
  && truthy(selectedTicket.value?.m30_confirmed)
  && selectedTicketExecutablePositionPct.value > 0.001
  && !selectedPaperExecution.value
))

const candidateRouteOptions = computed(() => {
  const routes = new Map([['all', '全部']])
  candidates.value.forEach((item) => {
    const key = item.route || 'unknown'
    routes.set(key, item.route_label || routeLabel(key))
  })
  return Array.from(routes, ([value, label]) => ({ value, label }))
})

const filteredCandidates = computed(() => {
  if (candidateRoute.value === 'all') return candidates.value
  return candidates.value.filter((item) => item.route === candidateRoute.value)
})

const artifactRows = computed(() => {
  const artifacts = payload.value?.artifacts || {}
  const backtestArtifacts = payload.value?.backtest_snapshot?.artifacts || {}
  return [
    ...Object.entries(artifacts).map(([name, item]) => ({ name, ...(item || {}) })),
    ...Object.entries(backtestArtifacts).map(([name, item]) => ({ name: `backtest_${name}`, ...(item || {}) })),
  ]
})

const alertType = computed(() => {
  if ((summary.value.qualified_shadow_buy_rows || 0) > 0) return 'warning'
  if (summary.value.diagnosis_code === 'NO_STATE_ROUTER_CANDIDATE') return 'success'
  return 'info'
})

const statusText = computed(() => {
  const code = summary.value.diagnosis_code || '--'
  const count = summary.value.qualified_shadow_buy_rows ?? 0
  return `状态 ${statusWithZh(code)}，合格买入候选 ${count}。正式买点、自动下单、订单路径均关闭。`
})

function truthy(value) {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0
  if (typeof value === 'string') return ['1', 'true', 'yes', 'ok'].includes(value.trim().toLowerCase())
  return false
}

function findPaperExecution(row) {
  const key = String(row?.ticket_key || row?.candidate_key || row?.trade_key || '')
  const code = String(row?.code || row?.code_raw || '').toUpperCase()
  const entryDate = String(row?.entry_date || row?.signal_date || '')
  return paperExecutions.value.find((item) => {
    if (key && item.ticket_key === key) return true
    return code && entryDate && String(item.code || '').toUpperCase() === code && item.entry_date === entryDate
  }) || null
}

function executionSideText(row) {
  const status = String(row?.status || '').toLowerCase()
  if (status.includes('sell') || status.includes('卖')) return 'SELL'
  if (status.includes('buy') || status.includes('买')) return 'BUY'
  return '--'
}

function isCurrentShadowHolding(row) {
  if (!row?.code && !row?.code_raw) return false
  const createdAt = String(row?.created_at || '').trim()
  if (latestShadowLedgerCreatedAt.value && createdAt !== latestShadowLedgerCreatedAt.value) return false
  const status = String(row?.shadow_status || row?.trade_status || row?.status || row?.last_state || '').toLowerCase()
  if (['exit', 'sell', 'sold', 'closed', 'cancel', 'invalid', 'expired'].some((token) => status.includes(token))) return false
  if (['退出', '卖出', '已清仓', '已关闭', '取消', '失效', '过期'].some((token) => status.includes(token))) return false
  return truthy(row?.qualified_shadow_buy) || status.includes('qualified') || status.includes('open') || status.includes('hold') || status.includes('planned')
}

function isShadowLedgerSessionCreatedAt(value) {
  const text = String(value || '').trim()
  const match = text.match(/(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/)
  if (!match) return false
  const date = new Date(`${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:00`)
  const day = date.getDay()
  if (day === 0 || day === 6) return false
  const minutes = Number(match[4]) * 60 + Number(match[5])
  return minutes >= 9 * 60 && minutes <= 15 * 60 + 30
}

function currentShadowPrice(row) {
  const direct = firstPositiveNumber(row?.current_price, row?.latest_price, row?.last_price, row?.close)
  if (direct !== null) return direct
  const code = String(row?.code || row?.code_raw || '').toUpperCase()
  const latest = latestCandidateByCode.value.get(code) || {}
  const fallback = firstPositiveNumber(
    latest.current_price,
    latest.latest_price,
    latest.last_price,
    latest.close,
    latest.reference_close,
    latest.execution_price
  )
  return fallback
}

function firstPositiveNumber(...values) {
  for (const value of values) {
    const n = Number(value)
    if (Number.isFinite(n) && n > 0) return n
  }
  return null
}

function shadowPnlRatio(row) {
  const entry = Number(row?.entry_price ?? row?.reference_close ?? row?.execution_price)
  const current = Number(currentShadowPrice(row))
  if (!Number.isFinite(entry) || entry <= 0 || !Number.isFinite(current) || current <= 0) return null
  return current / entry - 1
}

function signedPct(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  const sign = n > 0 ? '+' : ''
  return `${sign}${(n * 100).toFixed(2)}%`
}

function pnlTagType(value) {
  const n = Number(value)
  if (!Number.isFinite(n) || Math.abs(n) < 0.0001) return 'info'
  return n > 0 ? 'success' : 'danger'
}

function managementAction(row, paper) {
  if (row?.exit_action_label) return row.exit_action_label
  if (row?.source === 'ths_capital_holdings' || row?.route === 'broker_real_position') return '实仓退出检查'
  const status = String(row?.shadow_status || row?.trade_status || row?.status || '').toLowerCase()
  if (status.includes('stop') || status.includes('止损')) return '止损检查'
  if (status.includes('exit') || status.includes('退出')) return '退出检查'
  if (truthy(row?.take_profit_1_hit) || truthy(row?.half_take_profit_done)) return '减半后保护'
  if (!paper) return '补纸面'
  return '持有观察'
}

function managementActionType(row) {
  const priority = String(row?.exit_priority || '')
  const actionKey = String(row?.exit_action || '')
  if (priority === 'critical' || actionKey === 'sell_all') return 'danger'
  if (priority === 'take_profit' || actionKey === 'sell_half') return 'success'
  if (priority === 'warning' || actionKey === 'refresh_before_decision' || actionKey === 'data_missing') return 'warning'
  if (priority === 'protect' || actionKey === 'protect_remaining') return 'warning'
  const action = String(row?.management_action || '')
  if (action.includes('止损') || action.includes('退出')) return 'danger'
  if (action.includes('实仓')) return 'warning'
  if (action.includes('补纸面')) return 'warning'
  if (action.includes('减半')) return 'success'
  return 'info'
}

function exitReasonText(row) {
  const parts = []
  const reason = String(row?.exit_reason_label || row?.exit_reason || '').trim()
  if (reason) parts.push(reason)
  const trigger = price(row?.trigger_price)
  if (trigger !== '--') parts.push(`触发价 ${trigger}`)
  const stale = Number(row?.price_stale_minutes)
  if (Number.isFinite(stale) && stale > 30) parts.push(`数据${stale.toFixed(0)}分钟前`)
  return parts.join(' / ') || '--'
}

function routeLabel(route) {
  const labels = {
    institutional_mainwave: '机构主升浪',
    institutional_score120_mainwave: '机构主升Score120',
    panic_repair: '恐慌修复',
    panic_capitulation_repair: '恐慌出清修复',
    range_weak_repair: '震荡弱势修复',
    old_g3_strong_breakout: '强势突破',
    g2_gap_supplement: 'G2空档补位',
    old_g3_route_v3: '旧G3原生来源',
    old_g3_route: '旧G3原生来源'
  }
  return labels[route] || route || '--'
}

function entryPolicyText(row) {
  const value = String(row?.entry_price_policy || '')
  if (!value) return '下一交易日开盘参考，人工确认后执行'
  const labels = {
    next_trade_day_open_or_manual_shadow_fill: '下一交易日开盘参考，人工确认后执行'
  }
  return labels[value] || statusWithZh(value)
}

function plannedEntryDisplayText(row) {
  const entryDate = String(row?.entry_date || '').slice(0, 10)
  const planned = String(row?.planned_entry_ts || '').trim()
  const displayDate = entryDate || dateDisplayAnalysis.value.display_entry_date || dateDisplayAnalysis.value.expected_next_trade_date || ''
  if (planned && (!displayDate || planned.slice(0, 10) === displayDate)) return planned
  if (displayDate) return `${displayDate} 09:30:00`
  return planned || '--'
}

function exitContractText(row) {
  const text = String(row?.exit_contract || '')
  if (!text) return '暂无退出合同'
  if (text.includes('institutional mainwave dynamic contract')) return '机构主升动态合同：2槽、单槽50%；index_mom60<=5%；连续2笔已平仓机构主升亏损后暂停新买至少3个交易日，行情恢复后重启，最多15日复评；30m硬止损-12%，上涨+12%先卖一半，剩余仓按前低保护。'
  if (text.includes('2-slot compound default')) return '二槽复利合同：30m硬止损-12%；上涨+12%先卖一半；剩余仓按前一日低点保护；机构主升新买冷却按连续2笔已平仓亏损的动态规则执行。'
  if (text.includes('G2 gap supplement')) return 'G2补位合同：使用G2成熟买点和止损线；30m 硬止损约 -5%；上涨约 +10% 先减半；后续按结构转弱或前低跌破退出。'
  if (text.includes('panic capitulation repair')) return '恐慌出清修复合同：恐慌释放后介入；结构止损约 -5%，硬止损约 -8%；上涨约 +8% 先减半；剩余仓位按前低跌破或30m转弱退出。'
  if (text.includes('range weak repair')) return '震荡弱势修复合同：弱势/震荡修复介入；结构止损约 -6%，硬止损约 -10%；上涨约 +8% 先减半；剩余仓位按前低跌破或30m转弱退出。'
  if (text.includes('old G3 route')) return '强势突破/旧G3合同：按旧G3原生信号介入；用30m转弱和前一日低点跌破作为退出保护。'
  return text
}

function m30Text(row) {
  if (truthy(row?.m30_confirmed)) return '通过'
  const status = String(row?.m30_status || '')
  if (!status || status === 'nan') return '通过'
  return compactStatusWithZh(status)
}

function tradeDecisionExplain(row) {
  if (!row?.code && !row?.code_raw) return '当前没有可展示的买入票据。'
  const finalPct = Number(row?.position_pct || 0)
  const heatState = String(row?.index_mom60_heat_state || '')
  const parts = []
  if (afterhoursShadowTickets.value.length) {
    parts.push(`这是 ${afterhoursTicketDate.value || '--'} 盘后最新候选，不代表已经替换当前影子持仓。`)
  }
  if (heatState === 'high_heat_reduce_position' || heatState === 'high_heat_observe' || heatState === 'institutional_mom60_gt_5_block') {
    parts.push(`机构主升 mom60 超过 5% 时只观察，不进入影子盘或买入候选；当前仓位为 ${pct(finalPct)}。`)
  } else {
    parts.push(`仓位按当前策略合同计算为 ${pct(finalPct)}。`)
  }
  parts.push(`策略为 ${row.trade_strategy_label || routeLabel(row.trade_strategy || row.route)}。`)
  return parts.join(' ')
}

function naturalActionTagType(row) {
  const action = String(row?.natural_action || '')
  if (action === 'skip') return 'danger'
  if (action === 'allow_reduced') return 'warning'
  if (action === 'allow') return 'success'
  return 'info'
}

function naturalDisciplineText(row) {
  if (!row?.code && !row?.code_raw) return '自然纪律：暂无候选。'
  const label = row.natural_action_label || '观察'
  const naturalPct = Number(row.natural_position_pct)
  const pctText = Number.isFinite(naturalPct) ? `，自然仓位 ${pct(naturalPct)}` : ''
  const reason = row.natural_reason || '符合当前自然交易观察合同'
  return `自然纪律：${label}${pctText}；${reason}`
}

function opportunityText(row) {
  if (!row) return '--'
  if (row.opportunity_explain) return row.opportunity_explain
  const route = String(row.route || row.trade_strategy || '')
  const sector = row.sector_name || row.l2_sector_name || row.industry || row.template_label || '未识别板块'
  const diffusion = Number(row.sector_diffusion_score)
  const label = row.sector_diffusion_label || (Number.isFinite(diffusion)
    ? (diffusion >= 80 ? '强扩散' : diffusion >= 65 ? '有效扩散' : diffusion >= 50 ? '观察扩散' : '扩散不足')
    : '扩散未知')
  if (route === 'institutional_mainwave' || route === 'institutional_score120_mainwave') {
    const details = [`机构主升机会落在${sector}`, label]
    if (Number.isFinite(diffusion)) details.push(`扩散分${diffusion.toFixed(1)}`)
    if (row.sector_candidate_count !== undefined && row.sector_candidate_count !== null) details.push(`同板块候选${Number(row.sector_candidate_count).toFixed(0)}只`)
    if (row.same_sector_selected_count >= 2) details.push('同板块共振允许')
    return `${details.join('；')}。`
  }
  return `非机构主升路线；板块${sector}只用于重复暴露检查。`
}

function pct(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  return `${(n * 100).toFixed(1)}%`
}

function executablePositionPct(row) {
  if (!row?.code && !row?.code_raw) return 0
  const contractPct = Number(row?.position_pct || row?.slot_pct || 0)
  if (!Number.isFinite(contractPct) || contractPct <= 0) return 0
  return Math.min(contractPct, remainingBuyPositionPct.value)
}

function price(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  return n.toFixed(2)
}

function fmt(value, digits = 2) {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  return n.toFixed(digits)
}

function money(value) {
  if (value === null || value === undefined || value === '') return '--'
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  return n.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}

function observationBlockerText(row) {
  const blockers = Array.isArray(row?.blockers) ? row.blockers : []
  if (!blockers.length) return '无'
  return blockers.map((item) => `${item.gate || 'gate'}: ${item.message || '--'}`).join('；')
}

async function load(refresh = false) {
  if (refresh) refreshing.value = true
  else loading.value = true
  try {
    payload.value = await getGen3StateAlphaCurrent({ refresh, limit: 120 })
    await Promise.all([loadOps(), loadPaperExecutions(), loadBrokerState(), loadObservations()])
    if (refresh) ElMessage.success('G3正式五策略已刷新')
  } catch (error) {
    ElMessage.warning(error?.message || '读取G3正式五策略失败')
  } finally {
    refreshing.value = false
    loading.value = false
  }
}

async function loadPaperExecutions() {
  try {
    const data = await getGen3StateAlphaPaperExecutions({ limit: 100 })
    paperExecutions.value = Array.isArray(data?.paper_executions) ? data.paper_executions : []
  } catch (error) {
    paperExecutions.value = []
  }
}

async function loadBrokerState() {
  brokerLoading.value = true
  try {
    const [holdings, trades] = await Promise.all([
      getGen3StateAlphaBrokerHoldings({}),
      getGen3StateAlphaBrokerTrades({ limit: 200 })
    ])
    brokerPayload.value = holdings || {}
    brokerTradesPayload.value = trades || {}
  } catch (error) {
    brokerPayload.value = { holdings: [], capital: {} }
    brokerTradesPayload.value = { broker_trades: [] }
  } finally {
    brokerLoading.value = false
  }
}

async function syncBrokerHoldingsFromThs() {
  brokerHoldingSyncLoading.value = true
  try {
    const result = await syncGen3StateAlphaBrokerHoldingsFromThs({})
    brokerPayload.value = result || {}
    if (result?.ok) ElMessage.success(`已同步同花顺真实持仓：${brokerHoldings.value.length} 只`)
    else ElMessage.warning(result?.message || '同步同花顺真实持仓失败')
  } catch (error) {
    ElMessage.warning(error?.message || '同步同花顺真实持仓失败')
  } finally {
    brokerHoldingSyncLoading.value = false
  }
}

async function syncBrokerTradesFromThs() {
  brokerTradeSyncLoading.value = true
  try {
    const result = await syncGen3StateAlphaBrokerTradesFromThs({ source: 'delivery_file' })
    brokerTradesPayload.value = result || {}
    if (result?.ok) ElMessage.success(`已同步同花顺历史成交：新增解析 ${result.imported_count || 0} 条`)
    else ElMessage.warning(result?.message || '同步同花顺历史成交失败')
  } catch (error) {
    ElMessage.warning(error?.message || '同步同花顺历史成交失败')
  } finally {
    brokerTradeSyncLoading.value = false
  }
}

async function loadObservations() {
  observationLoading.value = true
  try {
    observationPayload.value = await getGen3StateAlphaObservationSnapshots({ limit: 60 })
  } catch (error) {
    observationPayload.value = { snapshots: [], accepted_observation_days: 0 }
  } finally {
    observationLoading.value = false
  }
}

async function loadOps() {
  opsLoading.value = true
  try {
    workflowPayload.value = await getGen3StateAlphaWorkflowStatus()
  } catch (error) {
    workflowPayload.value = {
      ok: false,
      message: error?.message || 'G3 运行状态不可用',
      pipeline_checks: [],
      blockers: []
    }
  } finally {
    opsLoading.value = false
  }
}

async function runObservationOnce() {
  observationRunLoading.value = true
  try {
    const result = await runGen3StateAlphaObservationSchedulerOnce({ run_smoke: true, refresh_smoke: true, auto_repair_minute30: true })
    await Promise.all([loadObservations(), loadOps()])
    if (result?.ok) ElMessage.success('G3 今日观察已验收通过')
    else ElMessage.warning(result?.blockers?.[0]?.message || 'G3 今日观察存在阻断')
  } catch (error) {
    ElMessage.warning(error?.message || 'G3 今日观察记录失败')
  } finally {
    observationRunLoading.value = false
  }
}

async function submitSelectedPaperOrder() {
  if (!canSubmitPaperOrder.value) return
  paperSubmitting.value = true
  try {
    const ticket = selectedTicket.value || {}
    const result = await submitGen3StateAlphaPaperOrder({
      ticket_key: selectedTicketKey.value,
      code: ticket.code || ticket.code_raw,
      execution_price: ticket.reference_close,
      position_pct: selectedTicketExecutablePositionPct.value,
      base_capital: brokerCapital.value.total_capital,
      notes: 'submitted from G3 formal five strategy page'
    })
    if (result?.ok) {
      ElMessage.success('G3 纸面执行已记录')
      await Promise.all([loadPaperExecutions(), loadOps(), loadObservations()])
    } else {
      ElMessage.warning(result?.message || 'G3 纸面执行记录失败')
    }
  } catch (error) {
    ElMessage.warning(error?.message || 'G3 纸面执行记录失败')
  } finally {
    paperSubmitting.value = false
  }
}

async function waitRefreshTask(taskId) {
  for (let i = 0; i < 90; i += 1) {
    const task = await getGen3StateAlphaRefreshTask(taskId)
    refreshTask.value = task || {}
    if (!['queued', 'running'].includes(task?.status)) return task
    await new Promise((resolve) => setTimeout(resolve, 3000))
  }
  return refreshTask.value
}

async function runOpsOnce() {
  opsRunLoading.value = true
  try {
    const task = await runGen3StateAlphaRefreshOnce({})
    refreshTask.value = task || {}
    const finalTask = task?.task_id ? await waitRefreshTask(task.task_id) : task
    await load(false)
    if (finalTask?.status === 'success') ElMessage.success('G3 影子刷新完成')
    else ElMessage.warning(finalTask?.message || 'G3 影子刷新结束但存在阻断')
  } catch (error) {
    ElMessage.warning(error?.message || 'G3 影子刷新失败')
  } finally {
    opsRunLoading.value = false
  }
}

async function runNotifyProbe() {
  opsNotifyLoading.value = true
  try {
    const task = await runGen3StateAlphaShadowMonitorOnce({ force_send: true })
    refreshTask.value = task || {}
    const finalTask = task?.task_id ? await waitRefreshTask(task.task_id) : task
    await load(false)
    if (finalTask?.alert?.email_sent) ElMessage.success('G3 影子提醒邮件已发送')
    else if (finalTask?.alert?.error) ElMessage.warning(finalTask.alert.error)
    else ElMessage.info('本次没有新的 G3 影子提醒邮件')
  } catch (error) {
    ElMessage.warning(error?.message || 'G3 影子提醒探测失败')
  } finally {
    opsNotifyLoading.value = false
  }
}

async function toggleMonitor() {
  opsConfigLoading.value = true
  try {
    const nextEnabled = !monitorState.value.enabled
    await setGen3StateAlphaShadowMonitorConfig({ enabled: nextEnabled })
    await loadOps()
    ElMessage.success(nextEnabled ? 'G3 影子监控已开启' : 'G3 影子监控已暂停')
  } catch (error) {
    ElMessage.warning(error?.message || 'G3 影子监控配置失败')
  } finally {
    opsConfigLoading.value = false
  }
}

onMounted(async () => {
  await load(false)
  await Promise.all([loadOps(), loadPaperExecutions(), loadBrokerState(), loadObservations()])
})
</script>

<style scoped>
.g3-alpha-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
  color: #1f2a44;
}

.g3-alpha-page > section {
  order: 50;
}

.g3-alpha-page > .el-alert {
  order: 2;
}

.topbar,
.panel,
.metric-card {
  background: #fff;
  border: 1px solid #e4e9f1;
  border-radius: 8px;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 16px;
  order: 1;
}

.eyebrow {
  color: #2f80ed;
  font-size: 12px;
  font-weight: 700;
  margin-bottom: 4px;
}

.title-block h1 {
  margin: 0 0 6px;
  font-size: 26px;
  line-height: 1.2;
}

.title-block p,
.panel-head p,
.metric-card small,
.stage p,
.check-row p,
.ticket-sub,
.exit-contract {
  margin: 0;
  color: #667085;
  line-height: 1.5;
}

.top-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.ops-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
  order: 80;
}

.ops-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.ops-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 10px;
}


.ops-card {
  min-height: 86px;
  border: 1px solid #e4e9f1;
  border-left: 4px solid #2f80ed;
  border-radius: 8px;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: #f8fafc;
}





.ops-card.ok {
  border-left-color: #12b76a;
}

.ops-card.blocked {
  border-left-color: #d92d20;
}

.ops-card.locked {
  border-left-color: #b54708;
}

.summary-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.summary-list.three {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.summary-list > div {
  min-height: 82px;
  border: 1px solid #eef2f6;
  border-radius: 8px;
  padding: 10px;
  background: #f8fafc;
}

.summary-list span {
  display: block;
  color: #667085;
  font-size: 12px;
  margin-bottom: 6px;
}

.summary-list strong {
  display: block;
  color: #1f2a44;
  font-size: 20px;
  line-height: 1.2;
  margin-bottom: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.summary-list small {
  color: #667085;
  line-height: 1.45;
}

.ops-card span {
  color: #667085;
  font-size: 12px;
}

.ops-card strong {
  font-size: 18px;
  line-height: 1.2;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ops-card small {
  color: #667085;
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ops-events-table {
  margin-top: 12px;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 10px;
  order: 30;
}

.metric-card {
  min-height: 96px;
  padding: 12px;
  border-left: 4px solid #2f80ed;
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.metric-card.accent {
  border-left-color: #d92d20;
}

.metric-card.ok {
  border-left-color: #12b76a;
}

.metric-card.blocked {
  border-left-color: #d92d20;
}

.metric-card.locked {
  border-left-color: #b54708;
}

.metric-card span {
  color: #667085;
  font-size: 12px;
}

.metric-card strong {
  font-size: 21px;
  line-height: 1.2;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.panel {
  padding: 14px;
}

.panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.panel-head h2 {
  margin: 0 0 6px;
  font-size: 16px;
}

.table-head {
  align-items: center;
}

.action-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
  order: 20;
}

.date-analysis-strip {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 34px;
  padding: 8px 10px;
  border: 1px solid #e4e9f1;
  border-radius: 8px;
  background: #f8fafc;
  color: #344054;
  font-size: 13px;
  line-height: 1.45;
}

.management-panel {
  order: 10;
}

.account-subhead {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin: 12px 0 8px;
}

.account-subhead h3 {
  margin: 0;
  font-size: 14px;
  color: #1f2a44;
}

.account-subhead small {
  color: #667085;
}

.shadow-subhead {
  margin-top: 16px;
}

.observation-panel {
  order: 90;
}

.ticket-detail {
  border: 1px solid #e4e9f1;
  border-radius: 8px;
  padding: 12px;
  background: #f8fafc;
}

.ticket-main {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 12px;
}

.route-chip {
  width: fit-content;
  color: #fff;
  background: #2f80ed;
  border-radius: 6px;
  padding: 4px 8px;
  font-size: 12px;
  font-weight: 700;
}

.ticket-main h3 {
  margin: 0;
  font-size: 22px;
}

.ticket-main small {
  color: #667085;
  font-size: 14px;
  font-weight: 500;
}

.ticket-sub {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 13px;
}

.decision-note {
  border: 1px solid #fedf89;
  border-radius: 8px;
  padding: 8px 10px;
  background: #fffbeb;
  color: #7a4b00;
  line-height: 1.5;
  font-size: 13px;
}

.risk-strip {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 10px;
}

.risk-strip div {
  background: #fff;
  border: 1px solid #e4e9f1;
  border-radius: 8px;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.risk-strip span {
  color: #667085;
  font-size: 12px;
}

.risk-strip strong {
  font-size: 18px;
}

.exit-contract {
  background: #fff;
  border: 1px solid #e4e9f1;
  border-radius: 8px;
  padding: 10px;
}

.paper-execution-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 10px;
  background: #fff;
  border: 1px solid #e4e9f1;
  border-radius: 8px;
  padding: 10px;
}

.paper-execution-bar > div {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.paper-execution-bar span {
  color: #667085;
  font-size: 12px;
}

.paper-execution-bar strong {
  color: #1f2a44;
  font-size: 17px;
}

.paper-execution-bar small {
  color: #667085;
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.paper-table {
  margin-top: 12px;
}

.natural-position {
  display: block;
  margin-top: 4px;
  color: #667085;
  font-size: 12px;
  line-height: 1.2;
}




.check-row strong {
  font-size: 14px;
  font-weight: 700;
}

.two-col {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 14px;
}

.check-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.check-row {
  display: grid;
  grid-template-columns: 54px minmax(0, 1fr);
  gap: 10px;
  align-items: flex-start;
  border-bottom: 1px solid #eef2f6;
  padding-bottom: 10px;
}

.check-row:last-child {
  border-bottom: 0;
  padding-bottom: 0;
}

@media (max-width: 1320px) {
  .metric-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .ops-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 980px) {
  .two-col {
    grid-template-columns: 1fr;
  }

  .risk-strip {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 760px) {
  .topbar,
  .panel-head,
  .table-head {
    flex-direction: column;
    align-items: flex-start;
  }

  .top-actions {
    justify-content: flex-start;
  }

  .metric-grid,
  .ops-grid,
  .risk-strip,
  .summary-list,
  .summary-list.three {
    grid-template-columns: 1fr;
  }
}
</style>
