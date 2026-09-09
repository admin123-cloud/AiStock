<template>
  <div class="g3-page">
    <header class="topbar">
      <div>
        <div class="eyebrow">G3 正式五策略</div>
        <h1>风控合同</h1>
        <p>集中查看五策略仓位、止损、止盈、退出合同和正式交易硬锁，保证页面、回测和未来调度使用同一套交易口径。</p>
      </div>
      <div class="actions">
        <el-button type="primary" :loading="loading" @click="load">刷新</el-button>
        <el-button type="primary" plain :loading="batchReviewLoading" :disabled="!unreviewedTicketRows.length" @click="markUnreviewedAsPaperWatch">未复盘转纸面观察</el-button>
        <el-button type="success" plain :loading="day1ExecutionLoading" :disabled="!day1PaperReviewRows.length" @click="recordDay1PaperExecutions">记录Day1纸面执行</el-button>
        <el-button type="success" plain :loading="brokerSyncLoading" @click="syncBrokerHoldingsAndReview">同步真实持仓并复审</el-button>
        <el-button type="warning" plain :loading="reviewLoading" @click="runReadinessReview">重跑实战前审计</el-button>
      </div>
    </header>

    <section class="metric-grid">
      <div class="metric-card">
        <span>策略阶段</span>
        <strong>{{ contract.stage || payload.stage || '--' }}</strong>
        <small>shadow / observe 优先</small>
      </div>
      <div class="metric-card accent">
        <span>组合槽位</span>
        <strong>{{ portfolio.max_slots ?? portfolio.slots ?? '--' }}</strong>
        <small>单槽 {{ pct(portfolio.per_slot_position_pct || portfolio.slot_pct) }}</small>
      </div>
      <div class="metric-card">
        <span>硬止损</span>
        <strong>{{ pct(exitContract.hard_stop_pct) }}</strong>
        <small>先减半 {{ pct(exitContract.first_take_profit_pct) }}</small>
      </div>
      <div class="metric-card">
        <span>今日票据</span>
        <strong>{{ shadowTickets.length }}</strong>
        <small>合格 {{ qualifiedTickets.length }}</small>
      </div>
      <div class="metric-card danger">
        <span>账户风控</span>
        <strong>{{ summary.account_risk?.action || 'normal' }}</strong>
        <small>{{ summary.account_risk?.reason || '按影子台账判断' }}</small>
      </div>
      <div class="metric-card locked">
        <span>下单通道</span>
        <strong>关闭</strong>
        <small>正式买点、自动下单、订单路径均锁定</small>
      </div>
      <div class="confirmation-card">
        <div>
          <span>confirmation packet</span>
          <strong>{{ brokerSyncConfirmationPacket.packet_status || '--' }}</strong>
          <small>{{ truthy(brokerSyncConfirmationPacket.safe_to_prompt) ? 'safe to prompt' : 'not ready' }}</small>
        </div>
        <div>
          <span>action fingerprint</span>
          <strong>{{ brokerSyncConfirmationFingerprint.action_group || '--' }}</strong>
          <small>{{ brokerSyncConfirmationFingerprint.primary_blocking_key || brokerSyncConfirmationFingerprint.object || '--' }}</small>
        </div>
        <div>
          <span>completion check</span>
          <strong>{{ brokerSyncConfirmationPacket.completion_check || '--' }}</strong>
          <small>{{ brokerSyncConfirmationPacket.stop_if_fail || '--' }}</small>
        </div>
      </div>
      <div class="confirmation-card">
        <div>
          <span>sync outcome</span>
          <strong>{{ brokerSyncOutcome.outcome_status || '--' }}</strong>
          <small>{{ brokerSyncOutcome.next_action || '--' }}</small>
        </div>
        <div>
          <span>latest attempt</span>
          <strong>{{ brokerSyncOutcomePayload.sync_attempt_count ?? '--' }}</strong>
          <small>{{ brokerSyncLatestAttempt.attempted_at || 'no attempt yet' }}</small>
        </div>
        <div>
          <span>broker freshness</span>
          <strong>{{ truthy(brokerSyncOutcomeBroker.is_fresh) ? 'fresh' : 'stale' }}</strong>
          <small>{{ brokerSyncOutcomeBroker.staleness_minutes ?? '--' }}m / {{ brokerSyncOutcomeBroker.holdings_count ?? '--' }} holdings</small>
        </div>
      </div>
    </section>

    <section class="panel premarket-control-panel">
      <div class="panel-head">
        <div>
          <h2>盘前指挥卡</h2>
          <p>系统只给出当前下一步和复审边界；真实买入仍保持人工确认与下单锁定。</p>
        </div>
        <div class="actions">
          <el-button size="small" plain :loading="brokerSyncPreflightLoading" @click="refreshBrokerSyncPreflight">同步预检</el-button>
          <el-button size="small" plain :loading="premarketControlLoading" @click="refreshPremarketControl(true)">刷新指挥卡</el-button>
          <el-button
            size="small"
            type="primary"
            :loading="premarketNextActionLoading || brokerSyncLoading || reviewLoading"
            :disabled="!premarketCommand.action_group || !truthy(premarketCommand.can_execute_now)"
            @click="executePremarketNextAction"
          >
            执行当前下一步
          </el-button>
        </div>
      </div>
      <div class="premarket-command-grid">
        <div>
          <span>准入状态</span>
          <strong>{{ premarketCommand.status || readinessSummary.live_admission_status || '--' }}</strong>
          <small>{{ premarketCommand.status_label || readinessSummary.live_admission_label || '--' }}</small>
        </div>
        <div>
          <span>当前动作</span>
          <strong>{{ premarketCommand.action_label || premarketCommand.next_action || '--' }}</strong>
          <small>{{ premarketCommand.action_group || '--' }}</small>
        </div>
        <div>
          <span>阻断/证据</span>
          <strong>{{ premarketCommand.blocking_command_count ?? readinessSummary.live_admission_blocking_command_count ?? '--' }} / {{ premarketCommand.pending_evidence_count ?? readinessSummary.live_blocker_pending_evidence_count ?? '--' }}</strong>
          <small>阻断动作 / 待证据</small>
        </div>
        <div>
          <span>执行边界</span>
          <strong>{{ truthy(premarketCommand.live_buy_allowed) ? '人工复核' : '禁止买入' }}</strong>
          <small>{{ premarketCommand.requires_confirmation ? '需要确认；不触发自动下单' : '不需要确认；仍锁定下单通道' }}</small>
        </div>
      </div>
      <div class="premarket-command-detail">
        <div>
          <span>推荐动作</span>
          <strong>{{ premarketCommand.current_action?.action || premarketCommand.recommended_ui_action || '--' }}</strong>
        </div>
        <div>
          <span>完成检查</span>
          <strong>{{ premarketCommand.current_action?.completion_check || '--' }}</strong>
        </div>
        <div>
          <span>失败边界</span>
          <strong>{{ premarketCommand.current_action?.stop_if_fail || '--' }}</strong>
        </div>
        <div>
          <span>自然交易边界</span>
          <strong>{{ premarketCommand.current_action?.natural_trade_boundary || premarketCommand.current_recheck?.natural_trade_boundary || '--' }}</strong>
        </div>
      </div>
      <div class="premarket-command-grid">
        <div>
          <span>同步预检</span>
          <strong>{{ brokerSyncPreflight.preflight_status || '--' }}</strong>
          <small>{{ truthy(brokerSyncPreflight.can_prompt_manual_sync) ? '可进入人工确认' : '暂不建议执行' }}</small>
        </div>
        <div>
          <span>网关配置</span>
          <strong>{{ truthy(brokerSyncPreflight.gateway_configured) ? '已配置' : '未配置' }}</strong>
          <small>{{ brokerSyncPreflight.gateway_url_hint || '--' }}</small>
        </div>
        <div>
          <span>持仓缓存</span>
          <strong>{{ brokerSyncPreflight.holdings_count ?? '--' }} / {{ brokerSyncPreflight.broker_staleness_minutes ?? '--' }}m</strong>
          <small>{{ brokerSyncPreflight.broker_updated_at || '--' }}</small>
        </div>
        <div>
          <span>预检下一步</span>
          <strong>{{ brokerSyncPreflight.current_action_label || premarketCommand.action_label || '--' }}</strong>
          <small>{{ brokerSyncPreflight.next_manual_action || '--' }}</small>
        </div>
      </div>
    </section>

    <section class="panel live-launch-panel">
      <div class="panel-head">
        <div>
          <h2>G3 实战启动包</h2>
          <p>把烟测、准入、盘前步骤和自然交易边界合并成一张启动状态卡；它只用于复盘和放行判断，不开启自动下单。</p>
        </div>
        <div class="actions">
          <el-button size="small" plain :loading="liveLaunchPacketLoading" @click="refreshLiveLaunchPacket(false)">读取启动包</el-button>
          <el-button size="small" type="primary" plain :loading="liveLaunchPacketLoading" @click="refreshLiveLaunchPacket(true)">重建启动包</el-button>
          <el-button size="small" type="success" plain :loading="liveLaunchSnapshotLoading" @click="recordLiveLaunchReviewSnapshot">记录启动快照</el-button>
        </div>
      </div>
      <div class="decision-card live-launch-decision-card" :class="`decision-card--${liveLaunchDecisionTone}`">
        <div class="decision-main">
          <span>实战放行判断</span>
          <strong>{{ liveLaunchDecision.decision_label || '--' }}</strong>
          <p>{{ liveLaunchDecision.primary_blocker || liveLaunchDecision.next_action || '--' }}</p>
        </div>
        <div class="decision-detail">
          <div>
            <span>人工实盘复核</span>
            <p>{{ truthy(liveLaunchDecision.can_enter_live_manual_review) ? '可进入' : '暂不放行' }}</p>
          </div>
          <div>
            <span>阻断/证据</span>
            <p>{{ liveLaunchDecision.blocking_command_count ?? '--' }} / {{ liveLaunchDecision.pending_evidence_count ?? '--' }}</p>
          </div>
          <div>
            <span>统一隐患</span>
            <p>{{ liveLaunchDecision.live_learning_ledger_count ?? '--' }} 项；待复盘 {{ liveLaunchDecision.live_learning_needs_review_count ?? '--' }}</p>
          </div>
        </div>
        <div class="decision-flags">
          <el-tag size="small" :type="truthy(liveLaunchDecision.can_buy) ? 'success' : 'danger'">真实买入锁定</el-tag>
          <el-tag size="small" type="info">不按单日收益放宽</el-tag>
        </div>
      </div>
      <div class="audit-card">
        <div class="audit-card-main">
          <span>live readiness audit</span>
          <strong>{{ liveLaunchReadinessAudit.audit_label || liveLaunchReadinessAudit.audit_status || '--' }}</strong>
          <p>{{ liveLaunchReadinessAudit.primary_blocker || liveLaunchReadinessAudit.next_action || '--' }}</p>
        </div>
        <div class="audit-card-grid">
          <div>
            <span>hard gaps</span>
            <strong>{{ liveLaunchReadinessAuditSummary.hard_gap_count ?? '--' }}</strong>
          </div>
          <div>
            <span>watch gaps</span>
            <strong>{{ liveLaunchReadinessAuditSummary.watch_gap_count ?? '--' }}</strong>
          </div>
          <div>
            <span>broker preflight</span>
            <strong>{{ liveLaunchReadinessAuditSummary.broker_preflight_status || '--' }}</strong>
          </div>
          <div>
            <span>staleness</span>
            <strong>{{ liveLaunchReadinessAuditSummary.broker_staleness_minutes ?? '--' }}m</strong>
          </div>
        </div>
        <el-table v-if="liveLaunchReadinessHardGaps.length" :data="liveLaunchReadinessHardGaps" stripe size="small" class="review-checklist-table" empty-text="no hard gaps">
          <el-table-column prop="gap" label="gap" width="210" show-overflow-tooltip />
          <el-table-column prop="count" label="count" width="86" align="right" />
          <el-table-column prop="next_action" label="next action" min-width="300" show-overflow-tooltip />
          <el-table-column prop="trade_impact" label="trade impact" min-width="240" show-overflow-tooltip />
        </el-table>
      </div>
      <el-table v-if="liveLaunchDecisionRiskRows.length" :data="liveLaunchDecisionRiskRows" stripe size="small" class="review-checklist-table" empty-text="暂无决策卡隐患">
        <el-table-column prop="priority" label="#" width="64" fixed />
        <el-table-column prop="origin" label="来源" width="180" show-overflow-tooltip />
        <el-table-column prop="learning_status" label="状态" width="120" show-overflow-tooltip />
        <el-table-column prop="issue_area" label="复盘轴" width="150" show-overflow-tooltip />
        <el-table-column prop="object" label="对象" min-width="180" show-overflow-tooltip />
        <el-table-column prop="problem_signal" label="问题信号" min-width="300" show-overflow-tooltip />
        <el-table-column prop="suggested_learning" label="学习方向" min-width="320" show-overflow-tooltip />
        <el-table-column prop="natural_trade_boundary" label="自然交易边界" min-width="320" show-overflow-tooltip />
      </el-table>
      <div class="audit-card live-replay-cockpit">
        <div class="audit-card-main">
          <span>live replay cockpit</span>
          <strong>{{ liveReplayCockpitSummary.current_action_key || '--' }} / {{ liveReplayCockpitSummary.next_review_axis || '--' }}</strong>
          <p>{{ liveReplayCockpitSummary.natural_trade_boundary || '--' }}</p>
        </div>
        <div class="audit-card-grid">
          <div>
            <span>todo</span>
            <strong>{{ liveReplayCockpitSummary.todo_count ?? '--' }}</strong>
          </div>
          <div>
            <span>followup</span>
            <strong>{{ liveReplayCockpitSummary.followup_count ?? '--' }}</strong>
          </div>
          <div>
            <span>progress</span>
            <strong>{{ liveReplayCockpitSummary.progress_pct ?? 0 }}%</strong>
          </div>
          <div>
            <span>trade lock</span>
            <strong>{{ truthy(liveReplayCockpitSummary.can_execute_trade) ? 'open' : 'locked' }}</strong>
          </div>
        </div>
        <el-table :data="liveReplayCockpitActionItems" stripe size="small" class="review-checklist-table" empty-text="no replay actions">
          <el-table-column prop="priority" label="#" width="56" fixed />
          <el-table-column prop="kind" label="kind" width="170" show-overflow-tooltip />
          <el-table-column prop="axis" label="axis" width="170" show-overflow-tooltip />
          <el-table-column prop="status" label="status" width="110" show-overflow-tooltip />
          <el-table-column prop="object" label="object" min-width="190" show-overflow-tooltip />
          <el-table-column prop="next_action" label="next action" min-width="340" show-overflow-tooltip />
          <el-table-column prop="review_boundary" label="boundary" min-width="360" show-overflow-tooltip />
        </el-table>
        <el-table :data="liveReplayCockpitAxisProgress" stripe size="small" class="review-checklist-table" empty-text="no replay axis progress">
          <el-table-column prop="axis_label" label="axis" width="190" fixed show-overflow-tooltip />
          <el-table-column prop="task_count" label="tasks" width="80" align="right" />
          <el-table-column prop="todo_count" label="todo" width="80" align="right" />
          <el-table-column prop="followup_count" label="followup" width="94" align="right" />
          <el-table-column prop="progress_pct" label="progress" width="94" align="right" />
          <el-table-column prop="next_review_object" label="next object" min-width="200" show-overflow-tooltip />
          <el-table-column prop="review_boundary" label="boundary" min-width="360" show-overflow-tooltip />
        </el-table>
        <el-table :data="liveReplayCockpitChecks" stripe size="small" class="review-checklist-table" empty-text="no natural trade checks">
          <el-table-column prop="check" label="check" width="190" fixed show-overflow-tooltip />
          <el-table-column prop="status" label="status" width="110" show-overflow-tooltip />
          <el-table-column prop="evidence" label="evidence" min-width="180" show-overflow-tooltip />
          <el-table-column prop="required_behavior" label="required behavior" min-width="360" show-overflow-tooltip />
        </el-table>
      </div>
      <div class="audit-card historical-replay-card">
        <div class="audit-card-main">
          <span>historical replay audit</span>
          <strong>{{ historicalDecisionReplayAudit.audit_status || '--' }} / {{ historicalDecisionReplayAuditSummary.todo_count ?? '--' }} todo</strong>
          <p>{{ historicalDecisionReplayAudit.next_action || historicalDecisionReplayAudit.natural_trade_boundary || 'review historical axes before claiming live readiness' }}</p>
        </div>
        <div class="audit-card-grid">
          <div>
            <span>required axes</span>
            <strong>{{ historicalDecisionReplayAuditSummary.required_axis_count ?? '--' }}</strong>
          </div>
          <div>
            <span>blocked axes</span>
            <strong>{{ historicalDecisionReplayAuditSummary.blocked_axis_count ?? '--' }}</strong>
          </div>
          <div>
            <span>followup</span>
            <strong>{{ historicalDecisionReplayAuditSummary.followup_count ?? '--' }}</strong>
          </div>
          <div>
            <span>trade lock</span>
            <strong>{{ truthy(historicalDecisionReplayAuditSummary.can_execute_trade) ? 'open' : 'locked' }}</strong>
          </div>
        </div>
        <el-alert
          v-if="historicalDecisionReplayHardGaps.length"
          :title="historicalDecisionReplayHardGaps[0].next_action || historicalDecisionReplayHardGaps[0].gap"
          type="warning"
          show-icon
          :closable="false"
          class="review-alert"
        />
        <el-table :data="historicalDecisionReplayAxisRows" stripe size="small" class="review-checklist-table" empty-text="no historical replay axis audit">
          <el-table-column prop="axis_label" label="axis" width="170" fixed show-overflow-tooltip />
          <el-table-column prop="axis_status" label="status" width="130" show-overflow-tooltip />
          <el-table-column prop="task_count" label="tasks" width="80" align="right" />
          <el-table-column prop="todo_count" label="todo" width="80" align="right" />
          <el-table-column prop="followup_count" label="followup" width="96" align="right" />
          <el-table-column prop="reviewed_count" label="reviewed" width="96" align="right" />
          <el-table-column prop="next_review_object" label="next object" min-width="180" show-overflow-tooltip />
          <el-table-column prop="next_review_focus" label="focus" min-width="340" show-overflow-tooltip />
          <el-table-column prop="optimization_boundary" label="boundary" min-width="340" show-overflow-tooltip />
        </el-table>
      </div>
      <div class="audit-card historical-replay-card">
        <div class="audit-card-main">
          <span>historical decision replay</span>
          <strong>{{ historicalDecisionReplaySummary.task_count ?? '--' }} tasks / {{ historicalDecisionReplaySummary.loss_trade_count ?? '--' }} losses</strong>
          <p>{{ historicalDecisionReplaySummary.natural_trade_boundary || 'historical replay only; no trade unlock' }}</p>
        </div>
        <div class="audit-card-grid">
          <div>
            <span>trade count</span>
            <strong>{{ historicalDecisionReplaySummary.trade_count ?? '--' }}</strong>
          </div>
          <div>
            <span>hard stop</span>
            <strong>{{ historicalDecisionReplaySummary.hard_stop_count ?? '--' }}</strong>
          </div>
          <div>
            <span>contract</span>
            <strong>{{ truthy(historicalDecisionReplaySummary.can_change_strategy_contract) ? 'open' : 'locked' }}</strong>
          </div>
          <div>
            <span>profit-only</span>
            <strong>{{ truthy(historicalDecisionReplaySummary.profit_only_optimization_allowed) ? 'allowed' : 'disabled' }}</strong>
          </div>
        </div>
        <el-table :data="historicalDecisionReplayTasks" stripe size="small" class="review-checklist-table" empty-text="no historical replay tasks">
          <el-table-column prop="priority" label="#" width="56" fixed />
          <el-table-column prop="severity" label="risk" width="90" show-overflow-tooltip />
          <el-table-column prop="axis_label" label="axis" width="170" show-overflow-tooltip />
          <el-table-column prop="entry_date" label="entry" width="110" show-overflow-tooltip />
          <el-table-column prop="object" label="object" min-width="170" show-overflow-tooltip />
          <el-table-column prop="route" label="route" width="160" show-overflow-tooltip />
          <el-table-column prop="net_ret" label="ret" width="90" align="right" />
          <el-table-column prop="problem_type" label="problem" min-width="220" show-overflow-tooltip />
          <el-table-column prop="replay_focus" label="replay focus" min-width="360" show-overflow-tooltip />
          <el-table-column prop="optimization_boundary" label="boundary" min-width="340" show-overflow-tooltip />
          <el-table-column label="review" width="250" fixed="right">
            <template #default="{ row }">
              <el-button size="small" plain type="success" :loading="isStrategyTuningTaskSaving(row)" @click="saveStrategyTuningTaskReview(row, 'validated')">valid</el-button>
              <el-button size="small" plain type="warning" :loading="isStrategyTuningTaskSaving(row)" @click="saveStrategyTuningTaskReview(row, 'issue_found')">issue</el-button>
              <el-button size="small" plain :loading="isStrategyTuningTaskSaving(row)" @click="saveStrategyTuningTaskReview(row, 'continue_watch')">watch</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
      <div class="audit-card">
        <div class="audit-card-main">
          <span>strategy tuning axis</span>
          <strong>{{ strategyTuningAxisSummary.risk_item_count ?? '--' }} risks / {{ strategyTuningAxisSummary.needs_review_count ?? '--' }} review</strong>
          <p>{{ strategyTuningAxisSummary.primary_blocker || strategyTuningAxisSummary.next_action || 'read-only tuning board; profit-only optimization is disabled' }}</p>
        </div>
        <div class="audit-card-grid">
          <div>
            <span>buy/order lock</span>
            <strong>{{ truthy(strategyTuningAxisSummary.live_admission_buy_allowed) ? 'review only' : 'locked' }}</strong>
          </div>
          <div>
            <span>profit-only</span>
            <strong>{{ truthy(strategyTuningAxisSummary.profit_only_optimization_allowed) ? 'allowed' : 'disabled' }}</strong>
          </div>
          <div>
            <span>contract change</span>
            <strong>{{ truthy(strategyTuningAxisSummary.can_change_strategy_contract) ? 'allowed' : 'locked' }}</strong>
          </div>
          <div>
            <span>admission</span>
            <strong>{{ strategyTuningAxisSummary.live_admission_status || '--' }}</strong>
          </div>
        </div>
        <el-table :data="strategyTuningAxisRows" stripe size="small" class="review-checklist-table" empty-text="no tuning axis rows">
          <el-table-column prop="axis_label" label="axis" width="190" fixed />
          <el-table-column prop="risk_count" label="risks" width="80" align="right" />
          <el-table-column prop="needs_review_count" label="review" width="90" align="right" />
          <el-table-column prop="watch_count" label="watch" width="86" align="right" />
          <el-table-column prop="top_objects" label="objects" min-width="180" show-overflow-tooltip />
          <el-table-column prop="recommended_review" label="review focus" min-width="360" show-overflow-tooltip />
          <el-table-column prop="optimization_boundary" label="boundary" min-width="360" show-overflow-tooltip />
        </el-table>
        <el-table :data="strategyTuningRiskRows" stripe size="small" class="review-checklist-table" empty-text="no tuning risk rows">
          <el-table-column prop="priority" label="#" width="56" fixed />
          <el-table-column prop="axis_label" label="axis" width="180" show-overflow-tooltip />
          <el-table-column prop="origin" label="origin" width="190" show-overflow-tooltip />
          <el-table-column prop="learning_status" label="status" width="140" show-overflow-tooltip />
          <el-table-column prop="object" label="object" min-width="170" show-overflow-tooltip />
          <el-table-column prop="problem_signal" label="problem" min-width="300" show-overflow-tooltip />
          <el-table-column prop="suggested_learning" label="learning" min-width="320" show-overflow-tooltip />
        </el-table>
        <div class="audit-card-main">
          <span>tuning review queue</span>
          <strong>{{ strategyTuningReviewQueueSummary.todo_count ?? '--' }} todo / {{ strategyTuningReviewQueueSummary.watch_count ?? '--' }} watch</strong>
          <p>{{ strategyTuningReviewQueueSummary.primary_blocker || strategyTuningReviewQueueSummary.next_action || 'review queue only; no trade execution' }}</p>
        </div>
        <div class="audit-card-main">
          <span>tuning completion audit</span>
          <strong>{{ strategyTuningCompletionAudit.audit_label || strategyTuningCompletionAudit.audit_status || '--' }}</strong>
          <p>{{ strategyTuningCompletionAudit.primary_gap || strategyTuningCompletionAudit.next_action || '--' }}</p>
        </div>
        <div class="audit-card-grid">
          <div>
            <span>todo</span>
            <strong>{{ strategyTuningCompletionSummary.todo_count ?? '--' }}</strong>
          </div>
          <div>
            <span>followup</span>
            <strong>{{ strategyTuningCompletionSummary.followup_count ?? '--' }}</strong>
          </div>
          <div>
            <span>reviewed</span>
            <strong>{{ strategyTuningCompletionSummary.reviewed_count ?? '--' }}</strong>
          </div>
          <div>
            <span>trade lock</span>
            <strong>{{ truthy(strategyTuningCompletionSummary.can_execute_trade) ? 'open' : 'locked' }}</strong>
          </div>
        </div>
        <div class="audit-card-main">
          <span>replay session</span>
          <strong>{{ strategyTuningReplaySession.session_status || '--' }} / {{ strategyTuningReplaySessionSummary.progress_pct ?? 0 }}%</strong>
          <p>{{ strategyTuningReplaySession.current_step_label || strategyTuningReplaySession.next_action || 'process replay steps from top to bottom' }}</p>
        </div>
        <el-table :data="strategyTuningReplaySessionSteps" stripe size="small" class="review-checklist-table" empty-text="no replay session steps">
          <el-table-column prop="step" label="#" width="56" fixed />
          <el-table-column prop="session_stage" label="stage" width="160" show-overflow-tooltip />
          <el-table-column prop="axis_label" label="axis" width="170" show-overflow-tooltip />
          <el-table-column prop="suggested_review_result" label="suggest" width="130" show-overflow-tooltip />
          <el-table-column prop="object" label="object" min-width="170" show-overflow-tooltip />
          <el-table-column prop="replay_focus" label="current focus" min-width="340" show-overflow-tooltip />
          <el-table-column prop="hidden_risk" label="hidden risk" min-width="320" show-overflow-tooltip />
          <el-table-column prop="evidence_required" label="evidence" min-width="320" show-overflow-tooltip />
        </el-table>
        <div class="audit-card-main">
          <span>current step completion</span>
          <strong>{{ strategyTuningCurrentStepCompletionPacket.completion_status || '--' }}</strong>
          <p>{{ strategyTuningCurrentStepCompletionPacket.after_done_check || '--' }}</p>
        </div>
        <div class="audit-card-grid">
          <div>
            <span>broker fresh</span>
            <strong>{{ truthy(strategyTuningCurrentStepBrokerEvidence.broker?.is_fresh) ? 'yes' : 'no' }}</strong>
          </div>
          <div>
            <span>sync attempts</span>
            <strong>{{ strategyTuningCurrentStepBrokerEvidence.sync_attempt_count ?? '--' }}</strong>
          </div>
          <div>
            <span>prompt</span>
            <strong>{{ truthy(strategyTuningCurrentStepBrokerEvidence.safe_to_prompt) ? 'ready' : 'wait' }}</strong>
          </div>
          <div>
            <span>mark done</span>
            <strong>{{ truthy(strategyTuningCurrentStepCompletionPacket.can_mark_step_done) ? 'ready' : 'locked' }}</strong>
          </div>
        </div>
        <div class="audit-card-main">
          <span>post-sync acceptance</span>
          <strong>{{ brokerPostSyncAcceptance.acceptance_status || '--' }}</strong>
          <p>{{ brokerPostSyncAcceptance.next_action || '--' }}</p>
          <el-button
            size="small"
            plain
            type="primary"
            :disabled="!truthy(brokerPostSyncAcceptance.can_record_execution_evidence)"
            :loading="brokerPostSyncEvidenceLoading"
            @click="recordBrokerPostSyncExecutionEvidence"
          >record execution evidence</el-button>
        </div>
        <div class="audit-card-grid">
          <div>
            <span>record evidence</span>
            <strong>{{ truthy(brokerPostSyncAcceptance.can_record_execution_evidence) ? 'ready' : 'locked' }}</strong>
          </div>
          <div>
            <span>manual live</span>
            <strong>{{ truthy(brokerPostSyncAcceptance.manual_live_review_allowed) ? 'open' : 'locked' }}</strong>
          </div>
          <div>
            <span>post broker</span>
            <strong>{{ truthy(brokerPostSyncAcceptanceBroker.is_fresh) ? 'fresh' : 'stale' }}</strong>
          </div>
          <div>
            <span>post stale</span>
            <strong>{{ brokerPostSyncAcceptanceBroker.staleness_minutes ?? '--' }}m</strong>
          </div>
        </div>
        <el-table v-if="brokerPostSyncAcceptanceGaps.length" :data="brokerPostSyncAcceptanceGaps" stripe size="small" class="review-checklist-table" empty-text="no post-sync gaps">
          <el-table-column prop="gap" label="post gap" width="230" fixed show-overflow-tooltip />
          <el-table-column prop="next_action" label="next action" min-width="360" show-overflow-tooltip />
          <el-table-column prop="trade_impact" label="trade impact" min-width="220" show-overflow-tooltip />
        </el-table>
        <div class="audit-card-main">
          <span>live action console</span>
          <strong>{{ liveActionConsoleSummary.current_action_key || '--' }} / {{ liveActionConsoleSummary.current_action_status || '--' }}</strong>
          <p>{{ liveActionConsoleSummary.natural_trade_boundary || '--' }}</p>
        </div>
        <el-table :data="liveActionConsoleSteps" stripe size="small" class="review-checklist-table" empty-text="no live action steps">
          <el-table-column prop="step" label="#" width="56" fixed />
          <el-table-column prop="status" label="status" width="100" />
          <el-table-column prop="action_label" label="action" width="210" show-overflow-tooltip />
          <el-table-column prop="current_evidence" label="evidence" min-width="280" show-overflow-tooltip />
          <el-table-column prop="next_action" label="next action" min-width="340" show-overflow-tooltip />
          <el-table-column prop="done_when" label="done when" min-width="300" show-overflow-tooltip />
          <el-table-column prop="trade_impact" label="impact" min-width="240" show-overflow-tooltip />
          <el-table-column label="action" width="230" fixed="right">
            <template #default="{ row }">
              <el-button
                size="small"
                plain
                type="primary"
                :disabled="!truthy(row.can_execute_now)"
                :loading="liveActionConsoleLoading || brokerSyncLoading || brokerPostSyncEvidenceLoading || reviewLoading"
                @click="handleLiveActionConsoleStep(row)"
              >run step</el-button>
              <el-button
                size="small"
                plain
                type="warning"
                :loading="liveActionConsoleReviewSavingKey === row.action_key"
                @click="saveLiveActionConsoleStepIssue(row)"
              >issue</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-table v-if="strategyTuningCurrentStepCompletionGaps.length" :data="strategyTuningCurrentStepCompletionGaps" stripe size="small" class="review-checklist-table" empty-text="no current step gaps">
          <el-table-column prop="gap" label="gap" width="210" fixed show-overflow-tooltip />
          <el-table-column prop="next_action" label="next action" min-width="360" show-overflow-tooltip />
          <el-table-column prop="trade_impact" label="trade impact" min-width="220" show-overflow-tooltip />
        </el-table>
        <el-table :data="strategyTuningReviewAxisSummaryRows" stripe size="small" class="review-checklist-table" empty-text="no tuning review axis summary">
          <el-table-column prop="axis_label" label="axis" width="190" fixed />
          <el-table-column prop="task_count" label="tasks" width="86" align="right" />
          <el-table-column prop="todo_count" label="todo" width="86" align="right" />
          <el-table-column prop="watch_count" label="watch" width="86" align="right" />
          <el-table-column prop="first_next_action" label="next action" min-width="320" show-overflow-tooltip />
          <el-table-column prop="completion_evidence" label="completion evidence" min-width="380" show-overflow-tooltip />
        </el-table>
        <el-table :data="strategyTuningReviewTaskRows" stripe size="small" class="review-checklist-table" empty-text="no tuning review tasks">
          <el-table-column prop="priority" label="#" width="56" fixed />
          <el-table-column prop="task_status" label="task" width="90" show-overflow-tooltip />
          <el-table-column prop="axis_label" label="axis" width="170" show-overflow-tooltip />
          <el-table-column prop="object" label="object" min-width="170" show-overflow-tooltip />
          <el-table-column prop="problem_signal" label="problem" min-width="280" show-overflow-tooltip />
          <el-table-column prop="review_method" label="review method" min-width="380" show-overflow-tooltip />
          <el-table-column prop="completion_evidence" label="done evidence" min-width="360" show-overflow-tooltip />
          <el-table-column prop="review_result" label="result" width="130" show-overflow-tooltip />
          <el-table-column prop="review_note" label="note" min-width="260" show-overflow-tooltip />
          <el-table-column label="review" width="260" fixed="right">
            <template #default="{ row }">
              <el-button size="small" plain :loading="isStrategyTuningTaskSaving(row)" @click="saveStrategyTuningTaskReview(row, 'validated')">valid</el-button>
              <el-button size="small" plain type="warning" :loading="isStrategyTuningTaskSaving(row)" @click="saveStrategyTuningTaskReview(row, 'issue_found')">issue</el-button>
              <el-button size="small" plain type="info" :loading="isStrategyTuningTaskSaving(row)" @click="saveStrategyTuningTaskReview(row, 'continue_watch')">watch</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div class="audit-card-main">
          <strong>replay suggestions: {{ strategyTuningReplaySuggestionSummary.suggestion_count ?? '--' }}</strong>
          <p>system-generated replay advice only; applying a suggestion records review evidence but never changes contract or opens trade paths</p>
        </div>
        <el-table :data="strategyTuningReplaySuggestionRows" stripe size="small" class="review-checklist-table" empty-text="no tuning replay suggestions">
          <el-table-column prop="axis_label" label="axis" width="170" fixed show-overflow-tooltip />
          <el-table-column prop="object" label="object" min-width="170" show-overflow-tooltip />
          <el-table-column prop="suggested_review_result" label="suggest" width="140" show-overflow-tooltip />
          <el-table-column prop="replay_focus" label="replay focus" min-width="340" show-overflow-tooltip />
          <el-table-column prop="suggested_hidden_risk" label="hidden risk" min-width="340" show-overflow-tooltip />
          <el-table-column prop="evidence_required" label="evidence" min-width="340" show-overflow-tooltip />
          <el-table-column label="apply" width="150" fixed="right">
            <template #default="{ row }">
              <el-button size="small" plain type="info" :loading="isStrategyTuningTaskSaving(row)" @click="applyStrategyTuningReplaySuggestion(row)">record</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-table :data="strategyTuningCompletionAxisRows" stripe size="small" class="review-checklist-table" empty-text="no tuning completion axis rows">
          <el-table-column prop="axis_label" label="axis" width="190" fixed />
          <el-table-column prop="axis_status" label="status" width="130" show-overflow-tooltip />
          <el-table-column prop="todo_count" label="todo" width="80" align="right" />
          <el-table-column prop="followup_count" label="followup" width="100" align="right" />
          <el-table-column prop="reviewed_count" label="reviewed" width="100" align="right" />
          <el-table-column prop="watch_count" label="watch" width="80" align="right" />
          <el-table-column prop="completion_evidence" label="completion evidence" min-width="360" show-overflow-tooltip />
        </el-table>
        <el-table v-if="strategyTuningCompletionOpenTasks.length" :data="strategyTuningCompletionOpenTasks" stripe size="small" class="review-checklist-table" empty-text="no open tuning tasks">
          <el-table-column prop="priority" label="#" width="56" fixed />
          <el-table-column prop="axis_label" label="axis" width="170" show-overflow-tooltip />
          <el-table-column prop="task_status" label="task" width="100" show-overflow-tooltip />
          <el-table-column prop="object" label="object" min-width="170" show-overflow-tooltip />
          <el-table-column prop="problem_signal" label="problem" min-width="280" show-overflow-tooltip />
          <el-table-column prop="next_action" label="next action" min-width="320" show-overflow-tooltip />
        </el-table>
      </div>
      <el-table :data="liveBlockerEvidenceBoardRows" stripe size="small" class="review-checklist-table" empty-text="暂无阻断消证项">
        <el-table-column prop="priority" label="#" width="56" fixed />
        <el-table-column label="证据状态" width="118">
          <template #default="{ row }">
            <el-tag size="small" :type="reviewBoardStatusType(row.evidence_status)">
              {{ reviewBoardStatusLabel(row.evidence_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="当前可做" width="92">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.can_execute_now) ? 'success' : 'info'">
              {{ truthy(row.can_execute_now) ? '是' : '等待' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="resolution_type" label="处理路径" width="190" show-overflow-tooltip />
        <el-table-column prop="object" label="对象" min-width="170" show-overflow-tooltip />
        <el-table-column prop="action" label="阻断动作" min-width="280" show-overflow-tooltip />
        <el-table-column prop="learning_axis" label="影响轴" width="160" show-overflow-tooltip />
        <el-table-column prop="trade_impact" label="交易影响" min-width="280" show-overflow-tooltip />
        <el-table-column prop="completion_check" label="完成标准" min-width="320" show-overflow-tooltip />
      </el-table>
      <div class="launch-summary-grid">
        <div>
          <span>启动状态</span>
          <strong>
            <el-tag :type="launchPacketStatusType">{{ launchPacket.status || '--' }}</el-tag>
          </strong>
          <small>{{ launchPacket.generated_at || '--' }}</small>
        </div>
        <div>
          <span>烟测状态</span>
          <strong>{{ launchPacketSummary.smoke_status || '--' }}</strong>
          <small>{{ launchPacketSummary.smoke_generated_at || '--' }}</small>
        </div>
        <div>
          <span>实盘准入</span>
          <strong>{{ launchPacketSummary.live_admission_status || '--' }}</strong>
          <small>{{ truthy(launchPacketSummary.live_admission_buy_allowed) ? '可进入人工复核' : '禁止真实买入' }}</small>
        </div>
        <div>
          <span>下一动作</span>
          <strong>{{ launchPacketSummary.live_premarket_next_action || premarketCommand.next_action || '--' }}</strong>
          <small>阻断 {{ launchPacketSummary.live_admission_blocking_command_count ?? '--' }} 项</small>
        </div>
        <div>
          <span>下一交易日</span>
          <strong>{{ launchPacketSummary.next_trade_entry_date || '--' }}</strong>
          <small>票据 {{ launchPacketSummary.next_trade_ticket_count ?? '--' }} 张</small>
        </div>
        <div>
          <span>遗漏复盘</span>
          <strong>{{ launchPacketSummary.candidate_omission_pending_count ?? '--' }}</strong>
          <small>无票日 {{ launchPacketSummary.no_trade_day_pending_review_count ?? '--' }} / 每日待办 {{ launchPacketSummary.daily_live_review_board_pending_count ?? '--' }}</small>
        </div>
        <div>
          <span>启动日学习</span>
          <strong>{{ launchPacketSummary.launch_day_learning_queue_count ?? '--' }}</strong>
          <small>需调优 {{ launchPacketSummary.launch_day_learning_needs_review_count ?? '--' }} / 继续观察 {{ launchPacketSummary.launch_day_learning_watch_more_count ?? '--' }}</small>
        </div>
      </div>
      <el-table :data="liveLaunchReviewSnapshotRows" stripe size="small" class="review-checklist-table" empty-text="暂无启动复盘快照">
        <el-table-column prop="generated_at" label="记录时间" width="170" fixed />
        <el-table-column prop="phase" label="阶段" width="130" show-overflow-tooltip />
        <el-table-column prop="entry_date" label="交易日" width="110" show-overflow-tooltip />
        <el-table-column prop="live_admission_status" label="准入状态" min-width="180" show-overflow-tooltip />
        <el-table-column prop="blocking_command_count" label="阻断" width="80" align="right" />
        <el-table-column prop="next_action" label="下一动作" min-width="220" show-overflow-tooltip />
        <el-table-column prop="launch_day_learning_queue_count" label="学习样本" width="100" align="right" />
        <el-table-column prop="current_action_label" label="当前动作" min-width="220" show-overflow-tooltip />
        <el-table-column prop="note" label="备注" min-width="260" show-overflow-tooltip />
        <el-table-column label="证据" width="92" fixed="right">
          <template #default="{ row }">
            <el-button size="small" plain :loading="liveLaunchSnapshotDetailLoading && liveLaunchSnapshotDetailId === row.snapshot_id" @click="openLiveLaunchSnapshotDetail(row)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-table :data="liveLaunchStepRows" stripe size="small" class="review-checklist-table" empty-text="暂无启动步骤">
        <el-table-column prop="step" label="#" width="56" />
        <el-table-column prop="action_label" label="启动动作" min-width="180" show-overflow-tooltip />
        <el-table-column prop="action_group" label="动作组" min-width="170" show-overflow-tooltip />
        <el-table-column label="当前可做" width="92">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.can_execute_now) ? 'success' : 'info'">
              {{ truthy(row.can_execute_now) ? '可执行' : '等前置' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="需确认" width="86">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.requires_manual_confirmation) ? 'warning' : 'info'">
              {{ truthy(row.requires_manual_confirmation) ? '需要' : '无需' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="expected_effect" label="预期效果" min-width="280" show-overflow-tooltip />
        <el-table-column prop="completion_check" label="完成检查" min-width="300" show-overflow-tooltip />
        <el-table-column prop="stop_if_fail" label="失败边界" min-width="280" show-overflow-tooltip />
        <el-table-column prop="natural_trade_boundary" label="自然交易边界" min-width="320" show-overflow-tooltip />
      </el-table>
      <el-table :data="liveLaunchDayPlaybookRows" stripe size="small" class="review-checklist-table" empty-text="暂无启动日剧本">
        <el-table-column prop="priority" label="#" width="64" fixed />
        <el-table-column prop="window" label="窗口" width="110" show-overflow-tooltip />
        <el-table-column prop="checkpoint_time" label="时间点" width="140" show-overflow-tooltip />
        <el-table-column prop="action_type" label="动作类型" min-width="150" show-overflow-tooltip />
        <el-table-column prop="review_axis" label="复盘轴" width="150" show-overflow-tooltip />
        <el-table-column prop="object" label="对象" min-width="180" show-overflow-tooltip />
        <el-table-column prop="required_action" label="必须动作" min-width="320" show-overflow-tooltip />
        <el-table-column prop="evidence_to_collect" label="应收集证据" min-width="340" show-overflow-tooltip />
        <el-table-column prop="pass_condition" label="通过条件" min-width="300" show-overflow-tooltip />
        <el-table-column prop="fail_condition" label="失败边界" min-width="300" show-overflow-tooltip />
        <el-table-column prop="launch_permission_effect" label="准入影响" min-width="220" show-overflow-tooltip />
        <el-table-column label="复盘状态" width="116" fixed="right">
          <template #default="{ row }">
            <el-tag size="small" :type="launchDayPlaybookStatusType(row.review_status)">
              {{ launchDayPlaybookStatusLabel(row.review_status, row.review_result_label) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="落账" width="330" fixed="right">
          <template #default="{ row }">
            <div class="review-action-buttons">
              <el-button size="small" plain type="success" :loading="launchDayPlaybookRowLoading(row)" @click="saveLaunchDayPlaybookReview(row, 'validated')">符合预期</el-button>
              <el-button size="small" plain type="danger" :loading="launchDayPlaybookRowLoading(row)" @click="saveLaunchDayPlaybookReview(row, 'issue_found')">发现隐患</el-button>
              <el-button size="small" plain type="warning" :loading="launchDayPlaybookRowLoading(row)" @click="saveLaunchDayPlaybookReview(row, 'continue_watch')">继续观察</el-button>
              <el-button size="small" plain :loading="launchDayPlaybookRowLoading(row)" @click="saveLaunchDayPlaybookReview(row, 'data_gap')">数据缺口</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
      <el-table :data="liveLaunchLearningQueueRows" stripe size="small" class="review-checklist-table" empty-text="暂无启动日学习样本">
        <el-table-column prop="priority" label="#" width="64" fixed />
        <el-table-column prop="learning_status" label="学习状态" width="120" show-overflow-tooltip />
        <el-table-column prop="axis_label" label="复盘轴" width="130" show-overflow-tooltip />
        <el-table-column prop="object" label="对象" min-width="200" show-overflow-tooltip />
        <el-table-column prop="evidence" label="证据" min-width="300" show-overflow-tooltip />
        <el-table-column prop="hidden_risk" label="隐患" min-width="320" show-overflow-tooltip />
        <el-table-column prop="optimization_direction" label="优化方向" min-width="320" show-overflow-tooltip />
        <el-table-column prop="natural_trade_boundary" label="自然交易边界" min-width="320" show-overflow-tooltip />
      </el-table>
      <el-table :data="liveLaunchReviewAxisRows" stripe size="small" class="review-checklist-table" empty-text="暂无复盘轴矩阵">
        <el-table-column prop="axis_label" label="复盘轴" width="130" fixed />
        <el-table-column prop="pending_count" label="待复盘" width="86" align="right" />
        <el-table-column prop="high_risk_count" label="高风险" width="86" align="right" />
        <el-table-column prop="formal_required_count" label="正式必处理" width="104" align="right" />
        <el-table-column prop="first_object" label="首个对象" min-width="180" show-overflow-tooltip />
        <el-table-column prop="next_action" label="下一步证据" min-width="320" show-overflow-tooltip />
        <el-table-column prop="pass_condition" label="通过条件" min-width="320" show-overflow-tooltip />
        <el-table-column prop="fail_condition" label="失败边界" min-width="320" show-overflow-tooltip />
        <el-table-column prop="launch_decision" label="启动决策" min-width="260" show-overflow-tooltip />
      </el-table>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>实战前逐票复盘</h2>
          <p>结合下一交易日票据、准入阻断与已平仓归因，逐票确认是否具备可执行性。</p>
        </div>
        <el-tag :type="readinessReviewTagType" effect="dark">{{ readinessSummary.verdict || '未生成' }}</el-tag>
      </div>
      <div class="review-grid">
        <div>
          <span>下一交易日</span>
          <strong>{{ readinessSummary.next_trade_entry_date || '--' }}</strong>
          <small>票据 {{ readinessSummary.next_trade_ticket_count ?? '--' }} 张</small>
        </div>
        <div>
          <span>Gate 阻断/警告</span>
          <strong>{{ readinessSummary.gate_block_count ?? '--' }} / {{ readinessSummary.gate_warn_count ?? '--' }}</strong>
          <small>{{ readinessSummary.generated_at || '--' }}</small>
        </div>
        <div>
          <span>票据阻断/警告</span>
          <strong>{{ readinessSummary.ticket_block_count ?? '--' }} / {{ readinessSummary.ticket_warning_count ?? '--' }}</strong>
          <small>逐票执行前确认</small>
        </div>
        <div>
          <span>逐票复盘状态</span>
          <strong>就绪 {{ readinessSummary.pretrade_review_formal_ready_count ?? '--' }} / 未复盘 {{ readinessSummary.pretrade_review_unreviewed_count ?? '--' }}</strong>
          <small>纸面 {{ readinessSummary.pretrade_review_paper_watch_count ?? '--' }} / 待刷新 {{ readinessSummary.pretrade_review_wait_refresh_count ?? '--' }} / 跳过 {{ readinessSummary.pretrade_review_skip_reject_count ?? '--' }}</small>
        </div>
        <div>
          <span>持仓阻断/警告</span>
          <strong>{{ readinessSummary.holding_block_count ?? '--' }} / {{ readinessSummary.holding_warning_count ?? '--' }}</strong>
          <small>正式刷新 {{ readinessSummary.holding_formal_refresh_pending_count ?? '--' }} / 观察 {{ readinessSummary.holding_observation_refresh_count ?? '--' }}</small>
        </div>
        <div>
          <span>正式动作</span>
          <strong>{{ readinessSummary.pretrade_formal_action_pending_count ?? '--' }}</strong>
          <small>观察 {{ readinessSummary.pretrade_observation_action_pending_count ?? '--' }} / 总待处理 {{ readinessSummary.pretrade_action_pending_count ?? '--' }}</small>
        </div>
        <div>
          <span>组合决策</span>
          <strong>{{ readinessSummary.portfolio_decision_block_count ?? '--' }} / {{ readinessSummary.portfolio_decision_warn_watch_count ?? '--' }}</strong>
          <small>阻断 / 关注</small>
        </div>
        <div>
          <span>正式放行</span>
          <strong>{{ readinessSummary.formal_launch_status || '--' }}</strong>
          <small>缺口 {{ readinessSummary.formal_launch_missing_count ?? '--' }} / {{ readinessSummary.formal_launch_next_step || '--' }}</small>
        </div>
        <div>
          <span>执行入口</span>
          <strong>{{ readinessSummary.execution_mode_ready_count ?? '--' }} / {{ readinessSummary.execution_mode_locked_count ?? '--' }}</strong>
          <small>可用 / 锁定，dry-run 不等于真实下单</small>
        </div>
        <div>
          <span>放行队列</span>
          <strong>{{ readinessSummary.formal_launch_action_queue_count ?? '--' }}</strong>
          <small>按优先级处理</small>
        </div>
        <div>
          <span>盘前剧本</span>
          <strong>{{ readinessSummary.premarket_playbook_step_count ?? '--' }}</strong>
          <small>按窗口顺序执行，不把观察项当硬阻断</small>
        </div>
        <div>
          <span>盘前指挥单</span>
          <strong>{{ readinessSummary.live_premarket_blocking_command_count ?? '--' }} / {{ readinessSummary.live_premarket_command_count ?? '--' }}</strong>
          <small>阻止真实买入 / 指挥动作；正式必处理 {{ readinessSummary.live_premarket_formal_command_count ?? '--' }}</small>
        </div>
        <div>
          <span>无票复盘</span>
          <strong>{{ readinessSummary.no_trade_day_review_count ?? '--' }}</strong>
          <small>待复盘 {{ readinessSummary.no_trade_day_pending_review_count ?? '--' }} / 已验证 {{ readinessSummary.no_trade_day_validated_count ?? '--' }} / 隐患 {{ readinessSummary.no_trade_day_issue_found_count ?? '--' }}</small>
        </div>
        <div>
          <span>每日复盘看板</span>
          <strong>{{ readinessSummary.daily_live_review_board_count ?? '--' }}</strong>
          <small>待处理 {{ readinessSummary.daily_live_review_board_pending_count ?? '--' }} / 正式必处理 {{ readinessSummary.daily_live_review_board_formal_required_count ?? '--' }}</small>
        </div>
        <div>
          <span>实战任务队列</span>
          <strong>{{ readinessSummary.live_review_task_count ?? '--' }}</strong>
          <small>盘前 {{ readinessSummary.live_review_premarket_task_count ?? '--' }} / 盘中盘后 {{ readinessSummary.live_review_intraday_after_task_count ?? '--' }} / 盘后 {{ readinessSummary.live_review_after_close_task_count ?? '--' }}</small>
        </div>
        <div>
          <span>复盘覆盖率</span>
          <strong>{{ readinessSummary.review_coverage_pending_scope_count ?? '--' }} / {{ readinessSummary.review_coverage_scope_count ?? '--' }}</strong>
          <small>待复盘 / 总范围；隐患 {{ readinessSummary.review_coverage_issue_scope_count ?? '--' }} / 继续观察 {{ readinessSummary.review_coverage_continue_watch_scope_count ?? '--' }}</small>
        </div>
        <div>
          <span>复盘证据矩阵</span>
          <strong>{{ readinessSummary.live_review_rubric_pending_scope_count ?? '--' }} / {{ readinessSummary.live_review_rubric_scope_count ?? '--' }}</strong>
          <small>待复盘范围 / 规则范围；正式必处理 {{ readinessSummary.live_review_rubric_formal_scope_count ?? '--' }}</small>
        </div>
        <div>
          <span>学习隐患队列</span>
          <strong>{{ readinessSummary.strategy_learning_backlog_count ?? '--' }}</strong>
          <small>需调优 {{ readinessSummary.strategy_learning_needs_review_count ?? '--' }} / 继续观察 {{ readinessSummary.strategy_learning_watch_more_count ?? '--' }}</small>
        </div>
        <div>
          <span>候选遗漏复盘</span>
          <strong>{{ readinessSummary.candidate_omission_pending_count ?? '--' }}</strong>
          <small>待观察 / 已验证 {{ readinessSummary.candidate_omission_validated_count ?? '--' }} / 继续 {{ readinessSummary.candidate_omission_continue_watch_count ?? '--' }} / 隐患 {{ readinessSummary.candidate_omission_issue_found_count ?? '--' }}</small>
        </div>
        <div>
          <span>Day1 纸面复盘包</span>
          <strong>{{ readinessSummary.day1_paper_review_ticket_count ?? '--' }}</strong>
          <small>待纸面/待刷新 {{ readinessSummary.day1_paper_review_pending_count ?? '--' }}，不等于正式买入</small>
        </div>
        <div>
          <span>Day1 盘后复盘</span>
          <strong>{{ readinessSummary.day1_after_close_review_count ?? '--' }}</strong>
          <small>待执行 {{ readinessSummary.day1_after_close_pending_execution_count ?? '--' }} / 待复盘 {{ readinessSummary.day1_after_close_pending_review_count ?? '--' }} / 隐患 {{ readinessSummary.day1_after_close_issue_found_count ?? '--' }}</small>
        </div>
        <div>
          <span>复盘证据</span>
          <strong>{{ readinessSummary.pretrade_review_evidence_count ?? '--' }}</strong>
          <small>人工放行必须同时有风险确认</small>
        </div>
        <div>
          <span>自然交易一致性</span>
          <strong>{{ readinessSummary.natural_consistency_min_score ?? '--' }}</strong>
          <small>偏拧/不顺 {{ readinessSummary.natural_consistency_strained_count ?? '--' }} 张；执行阻断 {{ readinessSummary.natural_execution_block_count ?? '--' }}</small>
        </div>
      </div>

      <el-table :data="liveLearningLedgerRows" stripe size="small" class="review-checklist-table" empty-text="暂无实战学习隐患">
        <el-table-column prop="priority" label="#" width="56" fixed />
        <el-table-column prop="origin" label="来源" width="170" show-overflow-tooltip />
        <el-table-column prop="learning_status" label="学习状态" width="120" show-overflow-tooltip />
        <el-table-column prop="risk_level" label="风险级别" width="110" show-overflow-tooltip />
        <el-table-column prop="issue_area" label="问题轴" width="150" show-overflow-tooltip />
        <el-table-column prop="object" label="对象" min-width="180" show-overflow-tooltip />
        <el-table-column prop="problem_signal" label="问题信号/隐患" min-width="320" show-overflow-tooltip />
        <el-table-column prop="evidence" label="证据/缺口" min-width="320" show-overflow-tooltip />
        <el-table-column prop="suggested_learning" label="学习/处理方向" min-width="340" show-overflow-tooltip />
        <el-table-column prop="natural_trade_boundary" label="自然交易边界" min-width="340" show-overflow-tooltip />
      </el-table>

      <el-table :data="livePremarketExecutionRecheckRows" stripe size="small" class="review-checklist-table" empty-text="暂无盘前执行后复查">
        <el-table-column label="复查结论" width="160">
          <template #default="{ row }">
            <el-tag size="small" :type="reviewBoardStatusType(row.recheck_status)">
              {{ reviewBoardStatusLabel(row.recheck_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="真实买入" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.live_buy_allowed) ? 'success' : 'danger'">
              {{ truthy(row.live_buy_allowed) ? '允许' : '禁止' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="blocking_command_count" label="阻断动作" width="92" />
        <el-table-column prop="pending_evidence_count" label="待证据" width="88" />
        <el-table-column prop="current_action_label" label="当前步骤" min-width="210" show-overflow-tooltip />
        <el-table-column prop="next_operator_action" label="下一动作" min-width="340" show-overflow-tooltip />
        <el-table-column prop="recheck_rule" label="复查规则" min-width="360" show-overflow-tooltip />
        <el-table-column prop="natural_trade_boundary" label="自然交易边界" min-width="360" show-overflow-tooltip />
      </el-table>

      <el-table :data="livePremarketActionAttemptRows" stripe size="small" class="review-checklist-table" empty-text="暂无盘前动作尝试">
        <el-table-column prop="priority" label="#" width="56" />
        <el-table-column prop="attempted_at" label="尝试时间" width="170" show-overflow-tooltip />
        <el-table-column prop="action_label" label="动作" min-width="190" show-overflow-tooltip />
        <el-table-column label="结果" width="92">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.ok) ? 'success' : 'danger'">
              {{ truthy(row.ok) ? '成功' : '未完成' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="同步/复审" width="120">
          <template #default="{ row }">
            {{ row.sync_mode === 'not_applicable' ? '无需同步' : (truthy(row.sync_ok) ? '同步' : '同步失败') }} / {{ truthy(row.review_ok) ? '复审' : '复审失败' }}
          </template>
        </el-table-column>
        <el-table-column prop="review_key" label="复盘键" min-width="180" show-overflow-tooltip />
        <el-table-column prop="review_status" label="复盘状态" width="110" show-overflow-tooltip />
        <el-table-column prop="live_admission_blocking_command_count" label="复审阻断" width="96" />
        <el-table-column label="真实买入" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.live_admission_buy_allowed) ? 'success' : 'danger'">
              {{ truthy(row.live_admission_buy_allowed) ? '允许' : '禁止' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="live_premarket_next_action" label="复审后下一步" min-width="260" show-overflow-tooltip />
        <el-table-column prop="evidence_boundary" label="证据边界" min-width="360" show-overflow-tooltip />
      </el-table>

      <el-table :data="liveManualLaunchAcceptanceRows" stripe size="small" class="review-checklist-table" empty-text="暂无人工实战验收单">
        <el-table-column prop="priority" label="#" width="56" />
        <el-table-column prop="acceptance_label" label="验收项" min-width="180" show-overflow-tooltip />
        <el-table-column label="状态" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'pass' ? 'success' : row.status === 'block' ? 'danger' : 'warning'">
              {{ row.status_label || reviewBoardStatusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="硬阻断" width="92">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.is_hard_blocker) ? 'danger' : 'info'">
              {{ truthy(row.is_hard_blocker) ? '是' : '否' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="current_value" label="当前值" min-width="150" show-overflow-tooltip />
        <el-table-column prop="required_value" label="要求" min-width="180" show-overflow-tooltip />
        <el-table-column prop="evidence" label="证据" min-width="260" show-overflow-tooltip />
        <el-table-column prop="next_action" label="下一步" min-width="320" show-overflow-tooltip />
        <el-table-column prop="natural_trade_boundary" label="自然交易边界" min-width="340" show-overflow-tooltip />
      </el-table>

      <el-table :data="liveDay1ReviewJournalRows" stripe size="small" class="review-checklist-table" empty-text="暂无Day1实战复盘日志">
        <el-table-column prop="priority" label="#" width="56" fixed />
        <el-table-column prop="journal_window" label="窗口" width="110" show-overflow-tooltip />
        <el-table-column prop="checkpoint_time" label="时间点" width="130" show-overflow-tooltip />
        <el-table-column prop="journal_type" label="日志类型" width="150" show-overflow-tooltip />
        <el-table-column label="状态" width="130">
          <template #default="{ row }">
            <el-tag size="small" :type="reviewBoardStatusType(row.status)">
              {{ reviewBoardStatusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="阻断" width="82">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.is_blocking) ? 'danger' : 'info'">
              {{ truthy(row.is_blocking) ? '是' : '否' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="review_axis" label="复盘轴" width="160" show-overflow-tooltip />
        <el-table-column prop="object" label="对象" min-width="180" show-overflow-tooltip />
        <el-table-column prop="action" label="动作" min-width="280" show-overflow-tooltip />
        <el-table-column prop="evidence_to_record" label="应记录证据" min-width="320" show-overflow-tooltip />
        <el-table-column prop="pass_condition" label="通过条件" min-width="280" show-overflow-tooltip />
        <el-table-column prop="fail_condition" label="失败边界" min-width="280" show-overflow-tooltip />
        <el-table-column prop="next_action" label="下一步" min-width="300" show-overflow-tooltip />
        <el-table-column prop="natural_trade_boundary" label="自然交易边界" min-width="320" show-overflow-tooltip />
        <el-table-column prop="review_key" label="Review Key" min-width="260" show-overflow-tooltip />
        <el-table-column label="处理" width="390" fixed="right">
          <template #default="{ row }">
            <template v-if="row.source_table === 'live_premarket_action_sequence' || row.source_table === 'live_manual_launch_acceptance'">
              <el-button size="small" plain type="primary" :loading="day1JournalRowLoading(row)" @click="handleDay1JournalPrimaryAction(row)">执行下一步</el-button>
            </template>
            <template v-else-if="row.source_table === 'live_blocker_evidence_ledger'">
              <template v-if="isDay1JournalNoTradeRow(row)">
                <el-button size="small" plain type="success" :loading="day1JournalRowLoading(row)" @click="saveNoTradeDayReview(taskToReviewRow(row), 'natural_no_trade')">空仓合理</el-button>
                <el-button size="small" plain type="danger" :loading="day1JournalRowLoading(row)" @click="saveNoTradeDayReview(taskToReviewRow(row), 'process_gap')">流程缺口</el-button>
              </template>
              <template v-else>
                <el-button size="small" plain type="success" :loading="day1JournalRowLoading(row)" @click="saveFormalActionReview(taskToReviewRow(row), 'manual_done')">已验证</el-button>
                <el-button size="small" plain type="danger" :loading="day1JournalRowLoading(row)" @click="saveFormalActionReview(taskToReviewRow(row), 'blocked')">发现隐患</el-button>
                <el-button size="small" plain type="warning" :loading="day1JournalRowLoading(row)" @click="saveFormalActionReview(taskToReviewRow(row), 'continue_watch')">继续观察</el-button>
              </template>
            </template>
            <template v-else-if="row.source_table === 'live_daily_review_execution_checklist'">
              <el-button size="small" plain type="success" :loading="day1JournalRowLoading(row)" @click="saveDailyReviewChecklistReview(row, 'validated')">符合预期</el-button>
              <el-button size="small" plain type="danger" :loading="day1JournalRowLoading(row)" @click="saveDailyReviewChecklistReview(row, 'issue_found')">发现隐患</el-button>
              <el-button size="small" plain type="warning" :loading="day1JournalRowLoading(row)" @click="saveDailyReviewChecklistReview(row, 'continue_watch')">继续观察</el-button>
            </template>
            <template v-else>
              <span class="muted-action">按盘后队列复盘</span>
            </template>
          </template>
        </el-table-column>
      </el-table>

      <el-table :data="livePremarketCommandRows" stripe size="small" class="review-checklist-table" empty-text="暂无实战盘前指挥单">
        <el-table-column prop="priority" label="#" width="56" fixed />
        <el-table-column prop="command_window" label="窗口" width="104" show-overflow-tooltip />
        <el-table-column prop="command_type" label="动作类型" width="140" show-overflow-tooltip />
        <el-table-column prop="decision_gate" label="放行判断" min-width="180" show-overflow-tooltip />
        <el-table-column label="当前状态" width="126">
          <template #default="{ row }">
            <el-tag size="small" :type="reviewBoardStatusType(row.current_status)">
              {{ reviewBoardStatusLabel(row.current_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="unlock_status" label="解锁状态" width="150" show-overflow-tooltip />
        <el-table-column label="阻止买入" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.blocks_live_buy) ? 'danger' : 'info'">
              {{ truthy(row.blocks_live_buy) ? '阻止' : '不阻止' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="正式必处理" width="104">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.formal_trade_required) ? 'warning' : 'info'">
              {{ truthy(row.formal_trade_required) ? '是' : '观察' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="review_scope" label="复盘范围" width="150" show-overflow-tooltip />
        <el-table-column prop="code" label="代码" width="100" />
        <el-table-column prop="name" label="名称" min-width="100" />
        <el-table-column prop="object" label="对象" min-width="170" show-overflow-tooltip />
        <el-table-column prop="action" label="动作" min-width="340" show-overflow-tooltip />
        <el-table-column prop="acceptance" label="验收标准" min-width="320" show-overflow-tooltip />
        <el-table-column prop="evidence_required" label="证据要求" min-width="340" show-overflow-tooltip />
        <el-table-column prop="optimization_boundary" label="优化边界" min-width="340" show-overflow-tooltip />
        <el-table-column prop="post_action_check" label="处理后复核" min-width="360" show-overflow-tooltip />
        <el-table-column prop="ledger_key" label="Ledger Key" min-width="280" show-overflow-tooltip />
        <el-table-column prop="fallback" label="失败退路" min-width="280" show-overflow-tooltip />
        <el-table-column label="处理" width="430" fixed="right">
          <template #default="{ row }">
            <template v-if="row.review_scope === 'first_live_decision'">
              <el-button size="small" plain type="warning" :loading="reviewLoading" @click="runReadinessReview">重跑审计</el-button>
            </template>
            <template v-else-if="row.review_scope === 'formal_action'">
              <el-button size="small" plain type="primary" :loading="formalActionRowLoading(taskToReviewRow(row))" @click="handleFormalReviewBoardAction(taskToReviewRow(row))">
                {{ formalActionButtonLabel(taskToReviewRow(row)) }}
              </el-button>
              <el-button size="small" plain type="success" :loading="formalActionRowLoading(taskToReviewRow(row))" @click="saveFormalActionReview(taskToReviewRow(row), 'manual_done')">已处理</el-button>
              <el-button size="small" plain type="danger" :loading="formalActionRowLoading(taskToReviewRow(row))" @click="saveFormalActionReview(taskToReviewRow(row), 'blocked')">卡住</el-button>
            </template>
            <template v-else-if="row.review_scope === 'no_trade_day'">
              <el-button size="small" plain type="success" @click="saveNoTradeDayReview(taskToReviewRow(row), 'natural_no_trade')">空仓合理</el-button>
              <el-button size="small" plain type="warning" @click="saveNoTradeDayReview(taskToReviewRow(row), 'missed_opportunity')">可能误杀</el-button>
              <el-button size="small" plain type="danger" @click="saveNoTradeDayReview(taskToReviewRow(row), 'process_gap')">流程缺口</el-button>
            </template>
            <template v-else-if="row.review_scope === 'candidate_omission'">
              <el-button size="small" plain type="success" @click="savePaperWatchFollowup(taskToReviewRow(row), 'as_expected')">挡得合理</el-button>
              <el-button size="small" plain type="warning" @click="savePaperWatchFollowup(taskToReviewRow(row), 'missed_opportunity')">可能误杀</el-button>
              <el-button size="small" plain @click="savePaperWatchFollowup(taskToReviewRow(row), 'continue_watch')">继续观察</el-button>
            </template>
            <span v-else class="muted-action">按任务队列处理</span>
          </template>
        </el-table-column>
      </el-table>

      <el-table :data="livePremarketActionSequenceRows" stripe size="small" class="review-checklist-table" empty-text="暂无盘前执行顺序">
        <el-table-column prop="step" label="#" width="56" fixed />
        <el-table-column prop="action_label" label="执行步骤" min-width="190" show-overflow-tooltip />
        <el-table-column prop="blocker_count" label="合并阻断" width="92" />
        <el-table-column label="当前可做" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.can_execute_now) ? 'success' : 'info'">
              {{ truthy(row.can_execute_now) ? '可执行' : '等前置' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="object" label="覆盖对象" min-width="220" show-overflow-tooltip />
        <el-table-column prop="evidence_statuses" label="证据状态" width="150" show-overflow-tooltip />
        <el-table-column prop="expected_effect" label="预期效果" min-width="300" show-overflow-tooltip />
        <el-table-column prop="stop_if_fail" label="失败边界" min-width="300" show-overflow-tooltip />
        <el-table-column prop="completion_check" label="完成检查" min-width="320" show-overflow-tooltip />
        <el-table-column prop="natural_trade_boundary" label="自然交易边界" min-width="320" show-overflow-tooltip />
        <el-table-column label="执行" width="260" fixed="right">
          <template #default="{ row }">
            <template v-if="row.action_group === 'sync_broker_holding_price'">
              <el-button size="small" plain type="primary" :disabled="!truthy(row.can_execute_now)" :loading="brokerSyncLoading" @click="handlePremarketSequenceAction(row)">同步并复审</el-button>
            </template>
            <template v-else-if="row.action_group === 'rerun_readiness_audit'">
              <el-button size="small" plain type="warning" :disabled="!truthy(row.can_execute_now)" :loading="reviewLoading" @click="handlePremarketSequenceAction(row)">重跑审计</el-button>
            </template>
            <template v-else-if="row.action_group === 'review_no_trade_context'">
              <el-button size="small" plain type="success" :disabled="!truthy(row.can_execute_now)" @click="saveNoTradeDayReview(taskToReviewRow(row), 'natural_no_trade')">空仓合理</el-button>
              <el-button size="small" plain type="danger" :disabled="!truthy(row.can_execute_now)" @click="saveNoTradeDayReview(taskToReviewRow(row), 'process_gap')">流程缺口</el-button>
            </template>
            <template v-else>
              <el-button size="small" plain type="success" :disabled="!truthy(row.can_execute_now)" :loading="formalActionRowLoading(taskToReviewRow(row))" @click="saveFormalActionReview(taskToReviewRow(row), 'manual_done')">已处理</el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>

      <el-table :data="liveBlockerResolutionRows" stripe size="small" class="review-checklist-table" empty-text="暂无实盘阻断处理路线图">
        <el-table-column prop="priority" label="#" width="56" fixed />
        <el-table-column prop="resolution_type" label="处理路径" width="190" show-overflow-tooltip />
        <el-table-column prop="execution_owner" label="执行归属" width="190" show-overflow-tooltip />
        <el-table-column label="可触发" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.can_auto_trigger) ? 'success' : 'info'">
              {{ truthy(row.can_auto_trigger) ? '可' : '人工' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="人工确认" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.requires_manual_confirmation) ? 'warning' : 'info'">
              {{ truthy(row.requires_manual_confirmation) ? '需要' : '不需要' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="object" label="对象" min-width="170" show-overflow-tooltip />
        <el-table-column prop="action" label="阻断动作" min-width="300" show-overflow-tooltip />
        <el-table-column prop="recommended_ui_action" label="页面入口" min-width="240" show-overflow-tooltip />
        <el-table-column prop="recommended_api_action" label="接口入口" min-width="300" show-overflow-tooltip />
        <el-table-column prop="evidence_required" label="证据要求" min-width="300" show-overflow-tooltip />
        <el-table-column prop="completion_check" label="完成检查" min-width="320" show-overflow-tooltip />
        <el-table-column prop="fallback" label="失败退路" min-width="300" show-overflow-tooltip />
        <el-table-column prop="natural_trade_boundary" label="自然交易边界" min-width="320" show-overflow-tooltip />
        <el-table-column label="处理" width="300" fixed="right">
          <template #default="{ row }">
            <template v-if="row.resolution_type === 'broker_holding_price_refresh'">
              <el-button size="small" plain type="primary" :loading="brokerSyncLoading" @click="handleBlockerResolutionAction(row)">同步并复审</el-button>
            </template>
            <template v-else-if="row.resolution_type === 'rerun_readiness_audit'">
              <el-button size="small" plain type="warning" :loading="reviewLoading" @click="handleBlockerResolutionAction(row)">重跑审计</el-button>
            </template>
            <template v-else-if="row.resolution_type === 'no_trade_context_review'">
              <el-button size="small" plain type="success" @click="saveNoTradeDayReview(taskToReviewRow(row), 'natural_no_trade')">空仓合理</el-button>
              <el-button size="small" plain type="danger" @click="saveNoTradeDayReview(taskToReviewRow(row), 'process_gap')">流程缺口</el-button>
            </template>
            <template v-else>
              <el-button size="small" plain type="success" :loading="formalActionRowLoading(taskToReviewRow(row))" @click="saveFormalActionReview(taskToReviewRow(row), 'manual_done')">已处理</el-button>
              <el-button size="small" plain type="danger" :loading="formalActionRowLoading(taskToReviewRow(row))" @click="saveFormalActionReview(taskToReviewRow(row), 'blocked')">卡住</el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>

      <el-table :data="liveBlockerEvidenceRows" stripe size="small" class="review-checklist-table" empty-text="暂无实盘阻断证据闭环">
        <el-table-column prop="priority" label="#" width="56" fixed />
        <el-table-column label="证据状态" width="118">
          <template #default="{ row }">
            <el-tag size="small" :type="reviewBoardStatusType(row.evidence_status)">
              {{ reviewBoardStatusLabel(row.evidence_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="resolution_type" label="处理路径" width="190" show-overflow-tooltip />
        <el-table-column prop="object" label="对象" min-width="170" show-overflow-tooltip />
        <el-table-column prop="action" label="阻断动作" min-width="260" show-overflow-tooltip />
        <el-table-column prop="review_result_label" label="复盘结果" width="140" show-overflow-tooltip />
        <el-table-column prop="issue_area" label="问题域" width="150" show-overflow-tooltip />
        <el-table-column prop="evidence_required" label="证据要求" min-width="280" show-overflow-tooltip />
        <el-table-column prop="review_note" label="人工证据" min-width="280" show-overflow-tooltip />
        <el-table-column prop="recommended_ui_action" label="推荐入口" min-width="220" show-overflow-tooltip />
        <el-table-column prop="next_action" label="下一步" min-width="320" show-overflow-tooltip />
        <el-table-column prop="completion_check" label="完成检查" min-width="320" show-overflow-tooltip />
        <el-table-column prop="natural_trade_boundary" label="自然交易边界" min-width="320" show-overflow-tooltip />
        <el-table-column label="证据处理" width="390" fixed="right">
          <template #default="{ row }">
            <template v-if="row.review_scope === 'no_trade_day'">
              <el-button size="small" plain type="success" @click="saveNoTradeDayReview(taskToReviewRow(row), 'natural_no_trade')">空仓合理</el-button>
              <el-button size="small" plain type="danger" @click="saveNoTradeDayReview(taskToReviewRow(row), 'process_gap')">流程缺口</el-button>
            </template>
            <template v-else>
              <el-button size="small" plain type="success" :loading="formalActionRowLoading(taskToReviewRow(row))" @click="saveFormalActionReview(taskToReviewRow(row), 'manual_done')">已验证</el-button>
              <el-button size="small" plain type="danger" :loading="formalActionRowLoading(taskToReviewRow(row))" @click="saveFormalActionReview(taskToReviewRow(row), 'blocked')">发现隐患</el-button>
              <el-button size="small" plain type="warning" :loading="formalActionRowLoading(taskToReviewRow(row))" @click="saveFormalActionReview(taskToReviewRow(row), 'continue_watch')">继续观察</el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>

      <el-table :data="liveReviewTaskQueueRows" stripe size="small" class="review-checklist-table" empty-text="暂无实战复盘任务队列">
        <el-table-column prop="priority" label="#" width="56" fixed />
        <el-table-column prop="window" label="窗口" width="104" show-overflow-tooltip />
        <el-table-column prop="task_type" label="任务" width="132" show-overflow-tooltip />
        <el-table-column label="状态" width="130">
          <template #default="{ row }">
            <el-tag size="small" :type="reviewBoardStatusType(row.task_status)">
              {{ reviewBoardStatusLabel(row.task_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="正式必处理" width="104">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.formal_trade_required) ? 'warning' : 'info'">
              {{ truthy(row.formal_trade_required) ? '是' : '观察' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column prop="object" label="对象" min-width="170" show-overflow-tooltip />
        <el-table-column prop="action" label="动作" min-width="340" show-overflow-tooltip />
        <el-table-column prop="acceptance" label="验收条件" min-width="320" show-overflow-tooltip />
        <el-table-column prop="review_method" label="复盘入口" min-width="320" show-overflow-tooltip />
        <el-table-column prop="fallback" label="失败退路" min-width="280" show-overflow-tooltip />
        <el-table-column label="处理" width="430" fixed="right">
          <template #default="{ row }">
            <template v-if="row.review_scope === 'first_live_decision'">
              <el-button size="small" plain type="warning" :loading="reviewLoading" @click="runReadinessReview">重跑审计</el-button>
            </template>
            <template v-else-if="row.review_scope === 'formal_action'">
              <el-button size="small" plain type="primary" :loading="formalActionRowLoading(taskToReviewRow(row))" @click="handleFormalReviewBoardAction(taskToReviewRow(row))">
                {{ formalActionButtonLabel(taskToReviewRow(row)) }}
              </el-button>
              <el-button size="small" plain type="success" :loading="formalActionRowLoading(taskToReviewRow(row))" @click="saveFormalActionReview(taskToReviewRow(row), 'manual_done')">已处理</el-button>
              <el-button size="small" plain type="danger" :loading="formalActionRowLoading(taskToReviewRow(row))" @click="saveFormalActionReview(taskToReviewRow(row), 'blocked')">卡住</el-button>
            </template>
            <template v-else-if="row.review_scope === 'no_trade_day'">
              <el-button size="small" plain type="success" @click="saveNoTradeDayReview(taskToReviewRow(row), 'natural_no_trade')">空仓合理</el-button>
              <el-button size="small" plain type="warning" @click="saveNoTradeDayReview(taskToReviewRow(row), 'missed_opportunity')">可能误杀</el-button>
              <el-button size="small" plain type="danger" @click="saveNoTradeDayReview(taskToReviewRow(row), 'process_gap')">流程缺口</el-button>
            </template>
            <template v-else-if="row.review_scope === 'candidate_omission'">
              <el-button size="small" plain type="success" @click="savePaperWatchFollowup(taskToReviewRow(row), 'as_expected')">挡得合理</el-button>
              <el-button size="small" plain type="warning" @click="savePaperWatchFollowup(taskToReviewRow(row), 'missed_opportunity')">可能误杀</el-button>
              <el-button size="small" plain @click="savePaperWatchFollowup(taskToReviewRow(row), 'continue_watch')">继续观察</el-button>
            </template>
            <template v-else-if="row.review_scope === 'day1_after_close' || row.review_scope === 'day1_paper_pack'">
              <el-button size="small" plain type="success" @click="savePaperWatchFollowup(taskToReviewRow(row), 'as_expected')">符合预期</el-button>
              <el-button size="small" plain type="warning" @click="savePaperWatchFollowup(taskToReviewRow(row), 'buy_point_chasing')">买点偏急</el-button>
              <el-button size="small" plain type="danger" @click="savePaperWatchFollowup(taskToReviewRow(row), 'risk_exit_issue')">卖点/风控</el-button>
            </template>
            <span v-else class="muted-action">按下方看板处理</span>
          </template>
        </el-table-column>
      </el-table>

      <el-table :data="reviewCoverageDashboardRows" stripe size="small" class="review-checklist-table" empty-text="暂无复盘覆盖率看板">
        <el-table-column prop="priority" label="#" width="56" />
        <el-table-column prop="review_scope" label="复盘范围" width="170" show-overflow-tooltip />
        <el-table-column prop="task_type" label="任务类型" width="160" show-overflow-tooltip />
        <el-table-column label="覆盖状态" width="126">
          <template #default="{ row }">
            <el-tag size="small" :type="reviewBoardStatusType(row.coverage_status)">
              {{ reviewBoardStatusLabel(row.coverage_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="total_count" label="任务数" width="82" align="right" />
        <el-table-column prop="covered_count" label="已覆盖" width="82" align="right" />
        <el-table-column prop="pending_count" label="待复盘" width="82" align="right" />
        <el-table-column prop="issue_found_count" label="隐患" width="76" align="right" />
        <el-table-column prop="continue_watch_count" label="观察" width="76" align="right" />
        <el-table-column prop="formal_required_count" label="正式必处理" width="104" align="right" />
        <el-table-column label="覆盖率" width="92" align="right">
          <template #default="{ row }">{{ pct(row.coverage_pct) }}</template>
        </el-table-column>
        <el-table-column prop="next_action" label="下一步" min-width="360" show-overflow-tooltip />
      </el-table>

      <el-table :data="liveReviewEvidenceRubricRows" stripe size="small" class="review-checklist-table" empty-text="暂无实战复盘证据矩阵">
        <el-table-column prop="priority" label="#" width="56" />
        <el-table-column prop="review_scope" label="复盘范围" width="160" show-overflow-tooltip />
        <el-table-column prop="task_type" label="任务类型" width="150" show-overflow-tooltip />
        <el-table-column prop="review_axes" label="复盘轴" min-width="190" show-overflow-tooltip />
        <el-table-column prop="task_count" label="任务" width="70" align="right" />
        <el-table-column prop="pending_count" label="待复盘" width="82" align="right" />
        <el-table-column prop="formal_required_count" label="正式必处理" width="104" align="right" />
        <el-table-column prop="evidence_required" label="证据要求" min-width="360" show-overflow-tooltip />
        <el-table-column prop="decision_rule" label="判断规则" min-width="340" show-overflow-tooltip />
        <el-table-column prop="optimization_boundary" label="优化边界" min-width="360" show-overflow-tooltip />
        <el-table-column prop="promotion_rule" label="升级条件" min-width="340" show-overflow-tooltip />
        <el-table-column prop="next_action" label="下一步" min-width="320" show-overflow-tooltip />
      </el-table>

      <el-table :data="formalLaunchRows" stripe size="small" class="review-checklist-table" empty-text="暂无正式放行清单">
        <el-table-column prop="priority" label="#" width="56" />
        <el-table-column prop="item" label="检查项" min-width="180" show-overflow-tooltip />
        <el-table-column label="状态" width="86">
          <template #default="{ row }">
            <el-tag size="small" :type="gateType(row.status)">{{ row.status || '--' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="evidence" label="证据" min-width="240" show-overflow-tooltip />
        <el-table-column prop="required_action" label="缺口动作" min-width="320" show-overflow-tooltip />
      </el-table>

      <el-table :data="executionModeRows" stripe size="small" class="review-checklist-table" empty-text="暂无执行入口矩阵">
        <el-table-column prop="priority" label="#" width="56" />
        <el-table-column prop="mode" label="入口" min-width="150" show-overflow-tooltip />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="executionModeType(row.status, row.allowed_now)">
              {{ row.status || '--' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="当前允许" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.allowed_now) ? 'success' : 'info'">
              {{ truthy(row.allowed_now) ? '允许' : '不允许' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="evidence" label="证据" min-width="320" show-overflow-tooltip />
        <el-table-column prop="required_before_use" label="使用前要求" min-width="320" show-overflow-tooltip />
        <el-table-column prop="failure_mode" label="误用风险" min-width="260" show-overflow-tooltip />
      </el-table>

      <el-table :data="formalLaunchActionRows" stripe size="small" class="review-checklist-table" empty-text="暂无正式放行优先队列">
        <el-table-column prop="priority" label="#" width="56" />
        <el-table-column prop="stage" label="阶段" width="150" show-overflow-tooltip />
        <el-table-column label="级别" width="86">
          <template #default="{ row }">
            <el-tag size="small" :type="gateType(row.severity)">{{ row.severity || '--' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="object" label="对象" min-width="160" show-overflow-tooltip />
        <el-table-column prop="action" label="动作" min-width="360" show-overflow-tooltip />
        <el-table-column prop="unlocks" label="解锁" min-width="260" show-overflow-tooltip />
        <el-table-column prop="source" label="来源" width="180" show-overflow-tooltip />
      </el-table>

      <el-table :data="premarketPlaybookRows" stripe size="small" class="review-checklist-table" empty-text="暂无盘前执行剧本">
        <el-table-column prop="priority" label="#" width="56" />
        <el-table-column prop="window" label="窗口" width="120" show-overflow-tooltip />
        <el-table-column prop="step" label="步骤" min-width="160" show-overflow-tooltip />
        <el-table-column prop="action" label="动作" min-width="340" show-overflow-tooltip />
        <el-table-column prop="pass_condition" label="通过条件" min-width="280" show-overflow-tooltip />
        <el-table-column prop="fallback_if_fail" label="失败退路" min-width="260" show-overflow-tooltip />
        <el-table-column prop="source" label="来源" width="190" show-overflow-tooltip />
      </el-table>

      <el-table :data="dailyLiveReviewBoardRows" stripe size="small" class="review-checklist-table" empty-text="暂无每日实战复盘看板">
        <el-table-column prop="priority" label="#" width="56" fixed />
        <el-table-column prop="stage" label="阶段" width="126" show-overflow-tooltip />
        <el-table-column prop="review_scope" label="复盘范围" width="150" show-overflow-tooltip />
        <el-table-column label="状态" width="126">
          <template #default="{ row }">
            <el-tag size="small" :type="reviewBoardStatusType(row.review_status)">
              {{ reviewBoardStatusLabel(row.review_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="正式必处理" width="104">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.formal_trade_required) ? 'warning' : 'info'">
              {{ truthy(row.formal_trade_required) ? '是' : '观察' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column prop="entry_date" label="交易日" width="104" />
        <el-table-column prop="object" label="对象" min-width="180" show-overflow-tooltip />
        <el-table-column prop="required_action" label="必须动作" min-width="340" show-overflow-tooltip />
        <el-table-column prop="evidence" label="证据" min-width="320" show-overflow-tooltip />
        <el-table-column prop="natural_decision" label="自然决策/观察" min-width="300" show-overflow-tooltip />
        <el-table-column prop="optimization_focus" label="学习重点" min-width="340" show-overflow-tooltip />
        <el-table-column prop="source" label="来源" width="190" show-overflow-tooltip />
        <el-table-column label="处理复盘项" width="430" fixed="right">
          <template #default="{ row }">
            <template v-if="row.review_scope === 'no_trade_day'">
              <el-button size="small" plain type="success" @click="saveNoTradeDayReview(row, 'natural_no_trade')">空仓合理</el-button>
              <el-button size="small" plain type="warning" @click="saveNoTradeDayReview(row, 'missed_opportunity')">可能误杀</el-button>
              <el-button size="small" plain type="warning" @click="saveNoTradeDayReview(row, 'data_gap')">数据缺口</el-button>
              <el-button size="small" plain type="danger" @click="saveNoTradeDayReview(row, 'process_gap')">流程缺口</el-button>
              <el-button size="small" plain @click="saveNoTradeDayReview(row, 'continue_watch')">继续观察</el-button>
            </template>
            <template v-else-if="row.review_scope === 'candidate_omission'">
              <el-button size="small" plain type="success" @click="savePaperWatchFollowup(row, 'as_expected')">挡得合理</el-button>
              <el-button size="small" plain type="warning" @click="savePaperWatchFollowup(row, 'missed_opportunity')">可能误杀</el-button>
              <el-button size="small" plain type="danger" @click="savePaperWatchFollowup(row, 'invalid_signal')">信号失效</el-button>
              <el-button size="small" plain @click="savePaperWatchFollowup(row, 'continue_watch')">继续观察</el-button>
            </template>
            <template v-else-if="row.review_scope === 'day1_after_close' || row.review_scope === 'day1_paper_pack'">
              <el-button size="small" plain type="success" @click="savePaperWatchFollowup(row, 'as_expected')">符合预期</el-button>
              <el-button size="small" plain type="warning" @click="savePaperWatchFollowup(row, 'buy_point_chasing')">买点偏急</el-button>
              <el-button size="small" plain type="warning" @click="savePaperWatchFollowup(row, 'selection_issue')">选股隐患</el-button>
              <el-button size="small" plain type="danger" @click="savePaperWatchFollowup(row, 'risk_exit_issue')">卖点/风控</el-button>
              <el-button size="small" plain @click="savePaperWatchFollowup(row, 'continue_watch')">继续观察</el-button>
            </template>
            <template v-else-if="row.review_scope === 'formal_action'">
              <el-button size="small" plain type="primary" :loading="formalActionRowLoading(row)" @click="handleFormalReviewBoardAction(row)">
                {{ formalActionButtonLabel(row) }}
              </el-button>
              <el-button size="small" plain type="success" :loading="formalActionRowLoading(row)" @click="saveFormalActionReview(row, 'manual_done')">已处理</el-button>
              <el-button size="small" plain type="danger" :loading="formalActionRowLoading(row)" @click="saveFormalActionReview(row, 'blocked')">卡住</el-button>
            </template>
            <span v-else class="muted-action">按来源处理</span>
          </template>
        </el-table-column>
      </el-table>

      <el-table :data="liveDailyReviewActionLayerRows" stripe size="small" class="review-checklist-table" empty-text="暂无实战复盘行动层">
        <el-table-column prop="priority" label="#" width="56" />
        <el-table-column prop="layer_label" label="行动层" width="150" show-overflow-tooltip />
        <el-table-column prop="review_window" label="窗口" width="110" show-overflow-tooltip />
        <el-table-column prop="checkpoint_time" label="检查时间" width="160" show-overflow-tooltip />
        <el-table-column prop="item_count" label="处理项" width="82" />
        <el-table-column prop="pending_count" label="待处理" width="82" />
        <el-table-column prop="high_risk_count" label="高风险" width="82" />
        <el-table-column prop="review_axes" label="复盘轴" min-width="190" show-overflow-tooltip />
        <el-table-column prop="first_action" label="首要动作" min-width="320" show-overflow-tooltip />
        <el-table-column prop="buy_permission_effect" label="买入影响" min-width="280" show-overflow-tooltip />
        <el-table-column prop="operator_instruction" label="操作说明" min-width="360" show-overflow-tooltip />
      </el-table>

      <el-table :data="liveDailyReviewExecutionChecklistRows" stripe size="small" class="review-checklist-table" empty-text="暂无实战逐项复盘执行清单">
        <el-table-column prop="priority" label="#" width="56" />
        <el-table-column prop="review_window" label="窗口" width="110" show-overflow-tooltip />
        <el-table-column prop="checkpoint_time" label="检查时间" width="170" show-overflow-tooltip />
        <el-table-column label="复盘轴" width="140">
          <template #default="{ row }">{{ reviewAxisLabel(row.review_axis) }}</template>
        </el-table-column>
        <el-table-column label="风险" width="88">
          <template #default="{ row }">
            <el-tag size="small" :type="hiddenRiskLevelType(row.risk_level)">
              {{ hiddenRiskLevelLabel(row.risk_level) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="reviewBoardStatusType(row.watch_status)">
              {{ reviewBoardStatusLabel(row.watch_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="review_scope" label="范围" width="160" show-overflow-tooltip />
        <el-table-column label="复盘状态" width="126">
          <template #default="{ row }">
            <el-tag size="small" :type="reviewBoardStatusType(row.review_status)">
              {{ reviewBoardStatusLabel(row.review_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="复盘落账" width="310" fixed="right">
          <template #default="{ row }">
            <el-button size="small" plain type="success" :loading="dailyReviewChecklistRowLoading(row)" @click="saveDailyReviewChecklistReview(row, 'validated')">符合预期</el-button>
            <el-button size="small" plain type="danger" :loading="dailyReviewChecklistRowLoading(row)" @click="saveDailyReviewChecklistReview(row, 'issue_found')">发现隐患</el-button>
            <el-button size="small" plain type="warning" :loading="dailyReviewChecklistRowLoading(row)" @click="saveDailyReviewChecklistReview(row, 'continue_watch')">继续观察</el-button>
            <el-button size="small" plain :loading="dailyReviewChecklistRowLoading(row)" @click="saveDailyReviewChecklistReview(row, 'data_gap')">数据缺口</el-button>
          </template>
        </el-table-column>
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column prop="entry_date" label="交易日" width="104" />
        <el-table-column prop="object" label="对象" min-width="180" show-overflow-tooltip />
        <el-table-column prop="evidence_to_collect" label="要收集的证据" min-width="320" show-overflow-tooltip />
        <el-table-column prop="pass_condition" label="通过条件" min-width="340" show-overflow-tooltip />
        <el-table-column prop="fail_condition" label="进入调优条件" min-width="340" show-overflow-tooltip />
        <el-table-column prop="target_action" label="目标动作" min-width="360" show-overflow-tooltip />
        <el-table-column prop="natural_trade_boundary" label="自然交易边界" min-width="360" show-overflow-tooltip />
      </el-table>

      <el-table :data="liveHiddenRiskWatchlistRows" stripe size="small" class="review-checklist-table" empty-text="暂无实战隐患观察登记">
        <el-table-column prop="priority" label="#" width="56" />
        <el-table-column label="状态" width="122">
          <template #default="{ row }">
            <el-tag size="small" :type="reviewBoardStatusType(row.watch_status)">
              {{ reviewBoardStatusLabel(row.watch_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="风险级别" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="hiddenRiskLevelType(row.risk_level)">
              {{ hiddenRiskLevelLabel(row.risk_level) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="issue_area" label="归因" width="150" show-overflow-tooltip />
        <el-table-column prop="review_scope" label="复盘范围" width="170" show-overflow-tooltip />
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column prop="entry_date" label="交易日" width="104" />
        <el-table-column prop="object" label="对象" min-width="180" show-overflow-tooltip />
        <el-table-column prop="risk_signal" label="风险信号" min-width="260" show-overflow-tooltip />
        <el-table-column prop="evidence_gap" label="证据缺口" min-width="320" show-overflow-tooltip />
        <el-table-column prop="next_review_action" label="下一步复盘" min-width="340" show-overflow-tooltip />
        <el-table-column prop="natural_trade_boundary" label="自然交易边界" min-width="360" show-overflow-tooltip />
        <el-table-column prop="source" label="来源" width="210" show-overflow-tooltip />
      </el-table>

      <el-table :data="strategyLearningBacklogRows" stripe size="small" class="review-checklist-table" empty-text="暂无策略学习隐患队列">
        <el-table-column prop="priority" label="#" width="56" />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="learningStatusType(row.learning_status)">
              {{ learningStatusLabel(row.learning_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="issue_area" label="归因" width="150" show-overflow-tooltip />
        <el-table-column prop="review_scope" label="来源范围" width="160" show-overflow-tooltip />
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column prop="entry_date" label="交易日" width="104" />
        <el-table-column prop="object" label="对象" min-width="180" show-overflow-tooltip />
        <el-table-column prop="problem_signal" label="问题信号" min-width="180" show-overflow-tooltip />
        <el-table-column prop="evidence" label="复盘证据" min-width="320" show-overflow-tooltip />
        <el-table-column prop="suggested_learning" label="学习/调优重点" min-width="360" show-overflow-tooltip />
        <el-table-column prop="source" label="来源" width="190" show-overflow-tooltip />
      </el-table>

      <el-table :data="noTradeDayReviewRows" stripe size="small" class="review-checklist-table" empty-text="暂无无票日复盘">
        <el-table-column prop="rank" label="#" width="56" />
        <el-table-column prop="entry_date" label="交易日" width="104" />
        <el-table-column prop="review_type" label="复盘类型" min-width="180" show-overflow-tooltip />
        <el-table-column prop="posture" label="姿态" width="150" show-overflow-tooltip />
        <el-table-column label="复盘结论" width="130">
          <template #default="{ row }">
            <el-tag size="small" :type="noTradeReviewStatusType(row.review_status)">
              {{ row.review_result_label || noTradeReviewResultLabel(row.review_result) || '待复盘' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="正式必处理" width="104">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.formal_trade_required) ? 'warning' : 'info'">
              {{ truthy(row.formal_trade_required) ? '是' : '观察' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="evidence" label="证据" min-width="340" show-overflow-tooltip />
        <el-table-column prop="hidden_risk" label="隐藏风险" min-width="340" show-overflow-tooltip />
        <el-table-column prop="natural_decision" label="自然决策" min-width="320" show-overflow-tooltip />
        <el-table-column prop="next_action" label="下一步" min-width="340" show-overflow-tooltip />
        <el-table-column prop="review_note" label="人工说明" min-width="240" show-overflow-tooltip />
        <el-table-column prop="source" label="来源" width="190" show-overflow-tooltip />
        <el-table-column label="记录无票复盘" width="430" fixed="right">
          <template #default="{ row }">
            <el-button size="small" plain type="success" @click="saveNoTradeDayReview(row, 'natural_no_trade')">空仓合理</el-button>
            <el-button size="small" plain type="warning" @click="saveNoTradeDayReview(row, 'missed_opportunity')">可能误杀</el-button>
            <el-button size="small" plain type="warning" @click="saveNoTradeDayReview(row, 'data_gap')">数据缺口</el-button>
            <el-button size="small" plain type="danger" @click="saveNoTradeDayReview(row, 'process_gap')">流程缺口</el-button>
            <el-button size="small" plain @click="saveNoTradeDayReview(row, 'continue_watch')">继续观察</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-table :data="day1PaperReviewRows" stripe size="small" class="review-checklist-table" empty-text="暂无第1天纸面复盘包">
        <el-table-column prop="rank" label="#" width="56" />
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column prop="entry_date" label="入场日" width="104" />
        <el-table-column label="启动姿态" width="150">
          <template #default="{ row }">
            <el-tag size="small" :type="day1PostureType(row.launch_posture)">
              {{ day1PostureLabel(row.launch_posture) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="buy_point_review" label="买点复盘" min-width="300" show-overflow-tooltip />
        <el-table-column prop="selection_review" label="选股复盘" min-width="300" show-overflow-tooltip />
        <el-table-column prop="model_switch_review" label="策略切换" min-width="300" show-overflow-tooltip />
        <el-table-column prop="exit_contract_review" label="卖点合同" min-width="300" show-overflow-tooltip />
        <el-table-column prop="hidden_risk_focus" label="隐患重点" min-width="340" show-overflow-tooltip />
        <el-table-column prop="after_close_required_note" label="盘后归因" min-width="360" show-overflow-tooltip />
        <el-table-column prop="next_action" label="下一步" min-width="320" show-overflow-tooltip />
      </el-table>

      <el-table :data="day1AfterCloseReviewRows" stripe size="small" class="review-checklist-table" empty-text="暂无Day1盘后复盘队列">
        <el-table-column prop="rank" label="#" width="56" />
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column prop="entry_date" label="入场日" width="104" />
        <el-table-column label="盘后状态" width="150">
          <template #default="{ row }">
            <el-tag size="small" :type="afterCloseStatusType(row.after_close_status)">
              {{ afterCloseStatusLabel(row.after_close_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="execution_price" label="纸面价" width="92" align="right">
          <template #default="{ row }">{{ price(row.execution_price) }}</template>
        </el-table-column>
        <el-table-column prop="quantity" label="数量" width="82" align="right" />
        <el-table-column prop="watch_result_label" label="观察结果" min-width="130" show-overflow-tooltip />
        <el-table-column prop="issue_area" label="归因" width="120" show-overflow-tooltip />
        <el-table-column prop="hidden_risk_focus" label="隐患重点" min-width="320" show-overflow-tooltip />
        <el-table-column prop="review_note" label="复盘说明" min-width="260" show-overflow-tooltip />
        <el-table-column prop="next_action" label="下一步" min-width="320" show-overflow-tooltip />
        <el-table-column label="记录盘后结果" width="430" fixed="right">
          <template #default="{ row }">
            <el-button size="small" plain type="success" @click="savePaperWatchFollowup(row, 'as_expected')">符合预期</el-button>
            <el-button size="small" plain type="warning" @click="savePaperWatchFollowup(row, 'buy_point_chasing')">买点偏急</el-button>
            <el-button size="small" plain type="warning" @click="savePaperWatchFollowup(row, 'selection_issue')">选股隐患</el-button>
            <el-button size="small" plain type="danger" @click="savePaperWatchFollowup(row, 'risk_exit_issue')">卖点/风控</el-button>
            <el-button size="small" plain @click="savePaperWatchFollowup(row, 'continue_watch')">继续观察</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-table :data="pretradeEvidenceRows" stripe size="small" class="review-checklist-table" empty-text="暂无逐票复盘证据">
        <el-table-column prop="rank" label="#" width="56" />
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column label="复盘" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="pretradeReviewType(row.review_action)">
              {{ row.review_label || row.review_action || '未复盘' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="风险确认" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.risk_acknowledged) ? 'success' : 'info'">
              {{ truthy(row.risk_acknowledged) ? '已确认' : '未确认' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="确认项" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.confirmation_complete) ? 'success' : 'warning'">
              {{ truthy(row.confirmation_complete) ? '完整' : '缺失' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="正式就绪" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.formal_ready) ? 'success' : 'warning'">
              {{ truthy(row.formal_ready) ? '就绪' : '未就绪' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="formal_ready_reason" label="原因" min-width="220" show-overflow-tooltip />
        <el-table-column prop="missing_confirmation_items" label="缺失确认项" min-width="260" show-overflow-tooltip />
        <el-table-column prop="review_decision_reason" label="复盘原因" min-width="360" show-overflow-tooltip />
        <el-table-column prop="next_review_trigger" label="下次触发" min-width="300" show-overflow-tooltip />
        <el-table-column prop="portfolio_decision" label="组合决策" min-width="220" show-overflow-tooltip />
        <el-table-column prop="next_action" label="下一步" min-width="320" show-overflow-tooltip />
      </el-table>

      <el-table :data="naturalConsistencyRows" stripe size="small" class="review-checklist-table" empty-text="暂无自然交易一致性审计">
        <el-table-column prop="rank" label="#" width="56" />
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column prop="consistency_score" label="一致性分" width="96" />
        <el-table-column prop="operational_readiness_score" label="就绪分" width="82" />
        <el-table-column label="等级" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="naturalConsistencyType(row.consistency_grade)">
              {{ naturalConsistencyLabel(row.consistency_grade) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="buy_point_logic" label="买点逻辑" min-width="280" show-overflow-tooltip />
        <el-table-column prop="sell_point_logic" label="卖点合同" min-width="260" show-overflow-tooltip />
        <el-table-column prop="selection_logic" label="选股逻辑" min-width="240" show-overflow-tooltip />
        <el-table-column prop="model_switch_logic" label="策略切换" min-width="260" show-overflow-tooltip />
        <el-table-column prop="deductions" label="逻辑扣分" min-width="320" show-overflow-tooltip />
        <el-table-column prop="readiness_gaps" label="就绪缺口" min-width="260" show-overflow-tooltip />
        <el-table-column prop="recommended_action" label="建议动作" min-width="340" show-overflow-tooltip />
      </el-table>

      <el-table :data="naturalExecutionRows" stripe size="small" class="review-checklist-table" empty-text="暂无自然执行决策">
        <el-table-column prop="priority" label="#" width="56" />
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column label="等级" width="86">
          <template #default="{ row }">
            <el-tag size="small" :type="gateType(row.decision_level)">
              {{ row.decision_level || '--' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="execution_posture" label="执行姿态" min-width="170" show-overflow-tooltip />
        <el-table-column label="人工实盘" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.manual_live_allowed_after_review) ? 'success' : 'info'">
              {{ truthy(row.manual_live_allowed_after_review) ? '可候选' : '未允许' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="required_confirmation" label="必须确认" min-width="420" show-overflow-tooltip />
        <el-table-column prop="downgrade_rule" label="降级规则" min-width="320" show-overflow-tooltip />
        <el-table-column prop="source_deductions" label="来源扣分" min-width="300" show-overflow-tooltip />
      </el-table>

      <el-table :data="reviewTicketRows" stripe size="small" class="review-ticket-table" empty-text="暂无下一交易日逐票复盘">
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column prop="entry_date" label="入场日" width="104" />
        <el-table-column prop="strategy" label="策略" min-width="130" show-overflow-tooltip />
        <el-table-column label="自然纪律" width="120">
          <template #default="{ row }">{{ row.natural_action_label || row.natural_action || '--' }}</template>
        </el-table-column>
        <el-table-column label="人工复盘" width="128">
          <template #default="{ row }">
            <el-tag size="small" :type="pretradeReviewType(row.pretrade_review_action)">
              {{ row.pretrade_review_label || '未复盘' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="warnings" label="警告" min-width="220" show-overflow-tooltip />
        <el-table-column label="记录" width="330" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="primary" plain :loading="isReviewSaving(row)" @click="savePretradeReview(row, 'paper_watch')">纸面观察</el-button>
            <el-button size="small" type="success" plain :loading="isReviewSaving(row)" @click="savePretradeReview(row, 'manual_approved')">人工放行</el-button>
            <el-button size="small" type="warning" plain :loading="isReviewSaving(row)" @click="savePretradeReview(row, 'wait_refresh')">待刷新</el-button>
            <el-button size="small" type="danger" plain :loading="isReviewSaving(row)" @click="savePretradeReview(row, 'skip')">跳过</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-table :data="pretradeReviewLedgerRows" stripe size="small" class="review-checklist-table" empty-text="暂无逐票复盘台账">
        <el-table-column prop="updated_at" label="记录时间" width="170" show-overflow-tooltip />
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column prop="entry_date" label="入场日" width="104" />
        <el-table-column label="结论" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="pretradeReviewType(row.review_action)">
              {{ row.review_label || row.review_action || '--' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="正式就绪" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.risk_acknowledged) && truthy(row.confirmation_complete) && row.review_action === 'manual_approved' ? 'success' : 'info'">
              {{ truthy(row.risk_acknowledged) && truthy(row.confirmation_complete) && row.review_action === 'manual_approved' ? '就绪' : '未就绪' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="review_decision_reason" label="复盘依据" min-width="360" show-overflow-tooltip />
        <el-table-column prop="next_review_trigger" label="下次触发" min-width="300" show-overflow-tooltip />
        <el-table-column prop="note" label="备注" min-width="260" show-overflow-tooltip />
      </el-table>

      <el-table :data="paperWatchFollowupRows" stripe size="small" class="review-checklist-table" empty-text="暂无纸面观察后评估">
        <el-table-column prop="updated_at" label="评估时间" width="170" show-overflow-tooltip />
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column prop="entry_date" label="入场日" width="104" />
        <el-table-column label="评估" width="128">
          <template #default="{ row }">
            <el-tag size="small" :type="paperWatchStatusType(row.followup_status)">
              {{ row.watch_result_label || paperWatchResultLabel(row.watch_result) || '待观察' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="issue_area" label="归因" width="120" show-overflow-tooltip />
        <el-table-column prop="hidden_risk" label="发现隐患" min-width="260" show-overflow-tooltip />
        <el-table-column prop="optimization_suggestion" label="优化建议" min-width="300" show-overflow-tooltip />
        <el-table-column prop="review_note" label="观察说明" min-width="280" show-overflow-tooltip />
        <el-table-column label="记录观察结果" width="430" fixed="right">
          <template #default="{ row }">
            <el-button size="small" plain type="success" @click="savePaperWatchFollowup(row, 'as_expected')">符合预期</el-button>
            <el-button size="small" plain type="warning" @click="savePaperWatchFollowup(row, 'buy_point_chasing')">买点偏急</el-button>
            <el-button size="small" plain type="warning" @click="savePaperWatchFollowup(row, 'selection_issue')">选股隐患</el-button>
            <el-button size="small" plain type="danger" @click="savePaperWatchFollowup(row, 'risk_exit_issue')">卖点/风控</el-button>
            <el-button size="small" plain @click="savePaperWatchFollowup(row, 'continue_watch')">继续观察</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-table :data="pretradeActionRows" stripe size="small" class="review-checklist-table" empty-text="暂无实战前动作清单">
        <el-table-column prop="priority" label="#" width="56" />
        <el-table-column prop="phase" label="阶段" width="120" show-overflow-tooltip />
        <el-table-column label="级别" width="86">
          <template #default="{ row }">
            <el-tag size="small" :type="gateType(row.severity)">{{ row.severity || '--' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="属性" width="86">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.formal_trade_required) ? 'danger' : 'info'">
              {{ truthy(row.formal_trade_required) ? '必处理' : '观察' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="object" label="对象" min-width="150" show-overflow-tooltip />
        <el-table-column prop="action" label="动作" min-width="280" show-overflow-tooltip />
        <el-table-column prop="done_condition" label="完成条件" min-width="320" show-overflow-tooltip />
        <el-table-column prop="source" label="来源" width="180" show-overflow-tooltip />
      </el-table>

      <el-table :data="ticketChecklistRows" stripe size="small" class="review-checklist-table" empty-text="暂无逐票复盘底稿">
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column label="等级" width="86">
          <template #default="{ row }">
            <el-tag size="small" :type="gateType(row.checklist_level)">{{ row.checklist_level || '--' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="buy_point_assessment" label="买点判断" min-width="260" show-overflow-tooltip />
        <el-table-column prop="exit_plan" label="卖点合同" min-width="260" show-overflow-tooltip />
        <el-table-column prop="selection_pattern" label="选股模式" min-width="230" show-overflow-tooltip />
        <el-table-column label="组合决策" min-width="260" show-overflow-tooltip>
          <template #default="{ row }">
            <el-tag size="small" :type="gateType(row.portfolio_decision_level)">
              {{ row.portfolio_decision_level || '--' }}
            </el-tag>
            <span class="inline-note">{{ row.portfolio_decision || '--' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="portfolio_decision_reason" label="组合原因" min-width="320" show-overflow-tooltip />
        <el-table-column prop="model_switch_assessment" label="策略切换" min-width="230" show-overflow-tooltip />
        <el-table-column prop="manual_approval_checklist" label="放行确认" min-width="360" show-overflow-tooltip />
        <el-table-column prop="hidden_risks" label="隐藏风险" min-width="300" show-overflow-tooltip />
        <el-table-column prop="manual_questions" label="人工问题" min-width="320" show-overflow-tooltip />
        <el-table-column prop="action_recommendation" label="动作建议" min-width="220" show-overflow-tooltip />
      </el-table>

      <el-table :data="holdingRefreshRows" stripe size="small" class="review-checklist-table" empty-text="暂无持仓刷新证明">
        <el-table-column prop="rank" label="#" width="56" />
        <el-table-column prop="source" label="来源" width="120" show-overflow-tooltip />
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column label="属性" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.formal_trade_required) ? 'danger' : 'info'">
              {{ truthy(row.formal_trade_required) ? '必刷新' : '观察' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="150">
          <template #default="{ row }">
            <el-tag size="small" :type="holdingRefreshType(row.refresh_status)">
              {{ row.refresh_status || '--' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="management_action" label="管理动作" min-width="140" show-overflow-tooltip />
        <el-table-column prop="staleness_evidence" label="新鲜度证据" min-width="300" show-overflow-tooltip />
        <el-table-column prop="pass_condition" label="通过条件" min-width="320" show-overflow-tooltip />
        <el-table-column prop="fallback_if_fail" label="失败退路" min-width="240" show-overflow-tooltip />
        <el-table-column prop="next_action" label="下一步" min-width="280" show-overflow-tooltip />
      </el-table>

      <el-table :data="holdingChecklistRows" stripe size="small" class="review-checklist-table" empty-text="暂无持仓退出底稿">
        <el-table-column prop="source" label="来源" width="110" />
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column label="等级" width="86">
          <template #default="{ row }">
            <el-tag size="small" :type="gateType(row.checklist_level)">{{ row.checklist_level || '--' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="exit_state" label="退出状态" min-width="260" show-overflow-tooltip />
        <el-table-column prop="management_action" label="管理动作" min-width="140" show-overflow-tooltip />
        <el-table-column prop="exit_reason" label="退出原因" min-width="240" show-overflow-tooltip />
        <el-table-column prop="hidden_risks" label="隐藏风险" min-width="300" show-overflow-tooltip />
        <el-table-column prop="manual_questions" label="人工问题" min-width="320" show-overflow-tooltip />
        <el-table-column prop="action_recommendation" label="动作建议" min-width="220" show-overflow-tooltip />
      </el-table>

      <el-table :data="candidateChecklistRows" stripe size="small" class="review-checklist-table" empty-text="暂无候选遗漏底稿">
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column label="等级" width="86">
          <template #default="{ row }">
            <el-tag size="small" :type="gateType(row.checklist_level)">{{ row.checklist_level || '--' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="入选" width="86">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.selected_next_trade) ? 'success' : 'info'">{{ truthy(row.selected_next_trade) ? '已入选' : '未入选' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="strategy" label="策略" min-width="130" show-overflow-tooltip />
        <el-table-column label="观察结论" width="130">
          <template #default="{ row }">
            <el-tag size="small" :type="paperWatchStatusType(row.omission_watch_status)">
              {{ row.watch_result_label || paperWatchResultLabel(row.watch_result) || '待观察' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="selection_state" label="选股状态" min-width="280" show-overflow-tooltip />
        <el-table-column prop="strategy_switch_assessment" label="策略切换" min-width="260" show-overflow-tooltip />
        <el-table-column prop="hidden_risks" label="隐藏风险" min-width="300" show-overflow-tooltip />
        <el-table-column prop="manual_questions" label="人工问题" min-width="320" show-overflow-tooltip />
        <el-table-column prop="action_recommendation" label="动作建议" min-width="220" show-overflow-tooltip />
        <el-table-column label="记录遗漏复盘" width="420" fixed="right">
          <template #default="{ row }">
            <el-button size="small" plain type="success" @click="savePaperWatchFollowup(row, 'as_expected')">挡得合理</el-button>
            <el-button size="small" plain type="warning" @click="savePaperWatchFollowup(row, 'missed_opportunity')">可能误杀</el-button>
            <el-button size="small" plain type="danger" @click="savePaperWatchFollowup(row, 'invalid_signal')">信号失效</el-button>
            <el-button size="small" plain @click="savePaperWatchFollowup(row, 'continue_watch')">继续观察</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="two-col review-tables">
        <el-table :data="readinessGates" stripe size="small" empty-text="暂无实战 Gate">
          <el-table-column prop="gate" label="Gate" min-width="170" />
          <el-table-column label="状态" width="86">
            <template #default="{ row }">
              <el-tag size="small" :type="gateType(row.status)">{{ row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="message" label="说明" min-width="260" show-overflow-tooltip />
        </el-table>
        <el-table :data="hazardRows" stripe size="small" empty-text="暂无隐患登记">
          <el-table-column prop="severity" label="级别" width="86">
            <template #default="{ row }">
              <el-tag size="small" :type="gateType(row.severity)">{{ row.severity }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="object" label="对象" min-width="130" />
          <el-table-column prop="hazard" label="隐患" min-width="260" show-overflow-tooltip />
        </el-table>
      </div>
    </section>

    <section class="two-col">
      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>组合合同</h2>
            <p>仓位和账户级风险约束。</p>
          </div>
        </div>
        <div class="kv-grid">
          <div v-for="item in portfolioRows" :key="item.label">
            <span>{{ item.label }}</span>
            <strong>{{ item.value }}</strong>
          </div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>退出合同</h2>
            <p>每张影子票据必须能追溯到明确退出规则。</p>
          </div>
        </div>
        <div class="kv-grid">
          <div v-for="item in exitRows" :key="item.label">
            <span>{{ item.label }}</span>
            <strong>{{ item.value }}</strong>
          </div>
        </div>
      </div>
      <div v-if="liveAdmissionSnapshotCard" class="decision-card" :class="`decision-card--${liveAdmissionTone}`">
        <div class="decision-main">
          <span>实盘准入快照</span>
          <strong>{{ liveAdmissionSnapshotCard.admission_label || '--' }}</strong>
          <p>{{ liveAdmissionSnapshotCard.next_step || liveAdmissionSnapshotCard.execution_posture || '--' }}</p>
        </div>
        <div class="decision-detail">
          <div>
            <span>主因</span>
            <p>{{ liveAdmissionSnapshotCard.primary_reason || '--' }}</p>
          </div>
          <div>
            <span>第一动作</span>
            <p>{{ liveAdmissionSnapshotCard.first_required_action || '--' }}</p>
          </div>
          <div>
            <span>复核规则</span>
            <p>{{ liveAdmissionSnapshotCard.verification_rule || '--' }}</p>
          </div>
        </div>
        <div class="decision-flags">
          <el-tag :type="truthy(liveAdmissionSnapshotCard.live_buy_allowed) ? 'success' : 'danger'">真实买入 {{ truthy(liveAdmissionSnapshotCard.live_buy_allowed) ? '允许' : '禁止' }}</el-tag>
          <el-tag :type="truthy(liveAdmissionSnapshotCard.paper_execution_allowed) ? 'warning' : 'info'">纸面执行 {{ truthy(liveAdmissionSnapshotCard.paper_execution_allowed) ? '可记录' : '不需要' }}</el-tag>
          <el-tag type="info">阻断 {{ liveAdmissionSnapshotCard.blocking_command_count ?? '--' }}</el-tag>
          <el-tag type="info">自动下单锁定</el-tag>
        </div>
      </div>
      <div v-if="firstLiveDecisionCard" class="decision-card" :class="`decision-card--${firstLiveDecisionTone}`">
        <div class="decision-main">
          <span>首日实战决策</span>
          <strong>{{ firstLiveDecisionCard.decision_label || '--' }}</strong>
          <p>{{ firstLiveDecisionCard.execution_posture || '--' }}</p>
        </div>
        <div class="decision-detail">
          <div>
            <span>主因</span>
            <p>{{ firstLiveDecisionCard.primary_reason || '--' }}</p>
          </div>
          <div>
            <span>第一动作</span>
            <p>{{ firstLiveDecisionCard.first_required_action || '--' }}</p>
          </div>
          <div>
            <span>锁定模式</span>
            <p>{{ firstLiveDecisionCard.locked_modes || '--' }}</p>
          </div>
        </div>
        <div class="decision-flags">
          <el-tag :type="truthy(firstLiveDecisionCard.live_buy_allowed) ? 'success' : 'info'">真实买入 {{ truthy(firstLiveDecisionCard.live_buy_allowed) ? '允许' : '禁止' }}</el-tag>
          <el-tag :type="truthy(firstLiveDecisionCard.paper_execution_allowed) ? 'warning' : 'info'">纸面执行 {{ truthy(firstLiveDecisionCard.paper_execution_allowed) ? '可记录' : '不需要' }}</el-tag>
          <el-tag type="info">自动下单锁定</el-tag>
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>正式交易策略</h2>
          <p>路由只作为来源通道，风控合同按正式交易策略保持一致。</p>
        </div>
      </div>
      <el-table :data="tradeStrategyPolicy" stripe size="small" empty-text="暂无正式策略合同">
        <el-table-column prop="label_cn" label="交易策略" width="150" />
        <el-table-column label="默认仓位" width="90" align="right">
          <template #default="{ row }">{{ pct(row.default_position_pct) }}</template>
        </el-table-column>
        <el-table-column label="来源路由" min-width="180">
          <template #default="{ row }">{{ listText(row.source_routes) }}</template>
        </el-table-column>
        <el-table-column label="合并来源" min-width="260">
          <template #default="{ row }">{{ listText(row.source_strategies) }}</template>
        </el-table-column>
        <el-table-column prop="risk_note" label="风控说明" min-width="260" show-overflow-tooltip />
      </el-table>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>今日票据风控</h2>
          <p>用于纸面执行前复核仓位、参考价、结构止损、硬止损和止盈。</p>
        </div>
      </div>
      <el-table v-loading="loading" :data="shadowTickets" stripe size="small" empty-text="暂无影子票据">
        <el-table-column prop="code" label="代码" width="110" fixed />
        <el-table-column prop="name" label="名称" min-width="110" fixed />
        <el-table-column label="交易策略" width="150">
          <template #default="{ row }">{{ row.trade_strategy_label || row.route_strategy_label || row.route_label || '--' }}</template>
        </el-table-column>
        <el-table-column prop="route_label" label="来源路线" width="130" />
        <el-table-column label="仓位" width="82" align="right">
          <template #default="{ row }">{{ pct(row.position_pct) }}</template>
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
        <el-table-column label="止盈一" width="90" align="right">
          <template #default="{ row }">{{ price(row.take_profit_1) }}</template>
        </el-table-column>
        <el-table-column label="合格" width="82">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.qualified_shadow_buy) ? 'success' : 'warning'">
              {{ truthy(row.qualified_shadow_buy) ? '是' : '否' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="exit_contract" label="退出合同" min-width="300" show-overflow-tooltip />
      </el-table>
    </section>

    <section class="two-col">
      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>正式交易硬锁</h2>
            <p>这些状态必须保持关闭，直到替代门槛全部通过。</p>
          </div>
        </div>
        <div class="gate-list">
          <div v-for="item in guardrailRows" :key="item.label" class="gate-row">
            <el-tag size="small" :type="item.ok ? 'success' : 'danger'">{{ item.ok ? '锁定' : '异常' }}</el-tag>
            <div>
              <strong>{{ item.label }}</strong>
              <span>{{ item.detail }}</span>
            </div>
          </div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>合同产物</h2>
            <p>确保风控规则能追溯到运行产物和蓝图。</p>
          </div>
        </div>
        <el-table :data="artifactRows" stripe size="small" empty-text="暂无合同产物">
          <el-table-column prop="name" label="产物" min-width="160" />
          <el-table-column label="状态" width="86">
            <template #default="{ row }">
              <el-tag size="small" :type="row.exists ? 'success' : 'danger'">{{ row.exists ? '存在' : '缺失' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="modified_at" label="更新时间" width="170" />
          <el-table-column prop="path" label="路径" min-width="260" show-overflow-tooltip />
        </el-table>
      </div>
    </section>

    <el-dialog v-model="liveLaunchSnapshotDetailVisible" title="G3 启动复盘快照详情" width="88%" destroy-on-close>
      <div v-if="liveLaunchSnapshotDetail" class="snapshot-detail">
        <div class="snapshot-detail-grid">
          <div>
            <span>快照</span>
            <strong>{{ liveLaunchSnapshotDetail.snapshot_id || liveLaunchSnapshotDetailId || '--' }}</strong>
          </div>
          <div>
            <span>阶段</span>
            <strong>{{ liveLaunchSnapshotDetail.phase || '--' }}</strong>
          </div>
          <div>
            <span>记录时间</span>
            <strong>{{ liveLaunchSnapshotDetail.generated_at || '--' }}</strong>
          </div>
          <div>
            <span>准入状态</span>
            <strong>{{ liveLaunchSnapshotDetail.live_admission_status || '--' }}</strong>
          </div>
          <div>
            <span>阻断</span>
            <strong>{{ liveLaunchSnapshotDetail.blocking_command_count ?? '--' }}</strong>
          </div>
          <div>
            <span>学习样本</span>
            <strong>{{ liveLaunchSnapshotDetail.launch_day_learning_queue_count ?? '--' }}</strong>
          </div>
          <div>
            <span>统一隐患</span>
            <strong>{{ liveLaunchSnapshotDetail.live_learning_ledger_count ?? liveLaunchSnapshotLearningLedgerRows.length ?? '--' }}</strong>
          </div>
        </div>
        <div class="snapshot-note">
          <strong>备注</strong>
          <p>{{ liveLaunchSnapshotDetail.note || '--' }}</p>
        </div>
        <el-table :data="liveLaunchSnapshotLearningLedgerRows" stripe size="small" class="review-checklist-table" empty-text="暂无统一学习隐患">
          <el-table-column prop="priority" label="#" width="64" />
          <el-table-column prop="origin" label="来源" width="170" show-overflow-tooltip />
          <el-table-column prop="learning_status" label="学习状态" width="120" show-overflow-tooltip />
          <el-table-column prop="risk_level" label="风险级别" width="110" show-overflow-tooltip />
          <el-table-column prop="issue_area" label="问题轴" width="140" show-overflow-tooltip />
          <el-table-column prop="object" label="对象" min-width="180" show-overflow-tooltip />
          <el-table-column prop="problem_signal" label="问题信号/隐患" min-width="300" show-overflow-tooltip />
          <el-table-column prop="evidence" label="证据/缺口" min-width="300" show-overflow-tooltip />
          <el-table-column prop="suggested_learning" label="学习/处理方向" min-width="320" show-overflow-tooltip />
          <el-table-column prop="natural_trade_boundary" label="自然交易边界" min-width="320" show-overflow-tooltip />
        </el-table>
        <el-table :data="liveLaunchSnapshotPremarketRows" stripe size="small" class="review-checklist-table" empty-text="暂无盘前命令">
          <el-table-column prop="action_label" label="动作" min-width="180" show-overflow-tooltip />
          <el-table-column prop="action_group" label="动作组" min-width="150" show-overflow-tooltip />
          <el-table-column prop="completion_check" label="完成检查" min-width="280" show-overflow-tooltip />
          <el-table-column prop="stop_if_fail" label="失败边界" min-width="260" show-overflow-tooltip />
          <el-table-column prop="natural_trade_boundary" label="自然交易边界" min-width="300" show-overflow-tooltip />
        </el-table>
        <el-table :data="liveLaunchSnapshotPlaybookRows" stripe size="small" class="review-checklist-table" empty-text="暂无启动日剧本">
          <el-table-column prop="priority" label="#" width="64" />
          <el-table-column prop="checkpoint_time" label="时间点" width="130" show-overflow-tooltip />
          <el-table-column prop="review_axis" label="复盘轴" width="130" show-overflow-tooltip />
          <el-table-column prop="object" label="对象" min-width="180" show-overflow-tooltip />
          <el-table-column prop="required_action" label="必须动作" min-width="300" show-overflow-tooltip />
          <el-table-column prop="review_status" label="复盘状态" width="120" show-overflow-tooltip />
          <el-table-column prop="review_note" label="备注" min-width="260" show-overflow-tooltip />
        </el-table>
        <el-table :data="liveLaunchSnapshotLearningRows" stripe size="small" class="review-checklist-table" empty-text="暂无学习样本">
          <el-table-column prop="priority" label="#" width="64" />
          <el-table-column prop="learning_status" label="学习状态" width="120" show-overflow-tooltip />
          <el-table-column prop="axis_label" label="复盘轴" width="130" show-overflow-tooltip />
          <el-table-column prop="object" label="对象" min-width="180" show-overflow-tooltip />
          <el-table-column prop="hidden_risk" label="隐患" min-width="300" show-overflow-tooltip />
          <el-table-column prop="optimization_direction" label="优化方向" min-width="320" show-overflow-tooltip />
          <el-table-column prop="natural_trade_boundary" label="自然交易边界" min-width="320" show-overflow-tooltip />
        </el-table>
      </div>
      <template #footer>
        <el-button @click="liveLaunchSnapshotDetailVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  getGen3StateAlphaContract,
  getGen3StateAlphaCurrent,
  getGen3StateAlphaRealtimeReadinessReview,
  getGen3StateAlphaPretradeTicketReviews,
  getGen3StateAlphaPaperWatchReviews,
  runGen3StateAlphaRealtimeReadinessReview,
  getGen3StateAlphaLiveLaunchPacket,
  runGen3StateAlphaLiveLaunchPacket,
  getGen3StateAlphaLiveLearningLedger,
  getGen3StateAlphaLiveLaunchDecision,
  getGen3StateAlphaLiveBlockerEvidenceBoard,
  getGen3StateAlphaLiveLaunchReadinessAudit,
  getGen3StateAlphaStrategyTuningAxisBoard,
  getGen3StateAlphaStrategyTuningReviewQueue,
  getGen3StateAlphaStrategyTuningCompletionAudit,
  getGen3StateAlphaStrategyTuningReplaySuggestions,
  getGen3StateAlphaStrategyTuningReplaySession,
  getGen3StateAlphaStrategyTuningCurrentStepCompletionPacket,
  getGen3StateAlphaLiveReplayCockpit,
  saveGen3StateAlphaStrategyTuningTaskReview,
  getGen3StateAlphaLiveLaunchReviewSnapshots,
  getGen3StateAlphaLiveLaunchReviewSnapshotDetail,
  recordGen3StateAlphaLiveLaunchReviewSnapshot,
  getGen3StateAlphaLaunchDayPlaybookReviews,
  saveGen3StateAlphaLaunchDayPlaybookReviewAndRun,
  getGen3StateAlphaHistoricalDecisionReplayTasks,
  getGen3StateAlphaHistoricalDecisionReplayAudit,
  getGen3StateAlphaPremarketControl,
  executeGen3StateAlphaPremarketNextAction,
  getGen3StateAlphaBrokerHoldingsSyncPreflight,
  getGen3StateAlphaBrokerHoldingsSyncConfirmationPacket,
  getGen3StateAlphaBrokerHoldingsSyncOutcome,
  getGen3StateAlphaBrokerPostSyncAcceptance,
  recordGen3StateAlphaBrokerPostSyncExecutionEvidence,
  getGen3StateAlphaLiveActionConsole,
  saveGen3StateAlphaLiveActionConsoleStepReview,
  syncGen3StateAlphaBrokerHoldingsFromThsAndReview,
  saveGen3StateAlphaPretradeTicketReview,
  startGen3StateAlphaPretradePaperWatchBatch,
  startGen3StateAlphaDay1PaperExecutionBatch,
  saveGen3StateAlphaPaperWatchReview,
  saveGen3StateAlphaCandidateOmissionReviewAndRun,
  saveGen3StateAlphaNoTradeDayReviewAndRun,
  saveGen3StateAlphaFormalActionReviewAndRun,
  getGen3StateAlphaDailyReviewChecklistReviews,
  saveGen3StateAlphaDailyReviewChecklistReviewAndRun
} from '@/api/trading'

const loading = ref(false)
const reviewLoading = ref(false)
const brokerSyncLoading = ref(false)
const brokerPostSyncEvidenceLoading = ref(false)
const liveActionConsoleLoading = ref(false)
const premarketControlLoading = ref(false)
const premarketNextActionLoading = ref(false)
const brokerSyncPreflightLoading = ref(false)
const liveLaunchPacketLoading = ref(false)
const liveLaunchSnapshotLoading = ref(false)
const liveLaunchSnapshotDetailLoading = ref(false)
const batchReviewLoading = ref(false)
const day1ExecutionLoading = ref(false)
const reviewSavingKey = ref('')
const formalActionSavingKey = ref('')
const dailyReviewChecklistSavingKey = ref('')
const launchDayPlaybookSavingKey = ref('')
const strategyTuningTaskSavingKey = ref('')
const liveActionConsoleReviewSavingKey = ref('')
const payload = ref({})
const currentPayload = ref({})
const readinessReview = ref({})
const premarketControl = ref({})
const liveLaunchPacketPayload = ref({})
const liveLaunchDecisionPayload = ref({})
const liveBlockerEvidenceBoardPayload = ref({})
const liveLaunchReadinessAuditPayload = ref({})
const strategyTuningAxisBoardPayload = ref({})
const strategyTuningReviewQueuePayload = ref({})
const strategyTuningCompletionAuditPayload = ref({})
const strategyTuningReplaySuggestionsPayload = ref({})
const strategyTuningReplaySessionPayload = ref({})
const strategyTuningCurrentStepCompletionPacketPayload = ref({})
const liveReplayCockpitPayload = ref({})
const historicalDecisionReplayPayload = ref({})
const historicalDecisionReplayAuditPayload = ref({})
const brokerSyncPreflightPayload = ref({})
const brokerSyncConfirmationPacketPayload = ref({})
const brokerSyncOutcomePayload = ref({})
const brokerPostSyncAcceptancePayload = ref({})
const liveActionConsolePayload = ref({})
const pretradeReviewLedger = ref([])
const paperWatchReviewLedger = ref([])
const dailyReviewChecklistReviewLedger = ref([])
const launchDayPlaybookReviewLedger = ref([])
const liveLaunchReviewSnapshotLedger = ref([])
const liveLearningLedgerPayload = ref({})
const liveLaunchSnapshotDetailVisible = ref(false)
const liveLaunchSnapshotDetailId = ref('')
const liveLaunchSnapshotDetailPayload = ref({})

const contract = computed(() => payload.value.contract || currentPayload.value.strategy_contract || {})
const summary = computed(() => currentPayload.value.summary || {})
const portfolio = computed(() => contract.value.portfolio_contract || contract.value.portfolio || {})
const exitContract = computed(() => contract.value.exit_contract || {})
const tradeStrategyPolicy = computed(() => Array.isArray(contract.value.trade_strategy_policy) ? contract.value.trade_strategy_policy : [])
const shadowTickets = computed(() => Array.isArray(currentPayload.value.shadow_tickets) ? currentPayload.value.shadow_tickets : [])
const qualifiedTickets = computed(() => shadowTickets.value.filter((row) => truthy(row.qualified_shadow_buy)))
const artifacts = computed(() => payload.value.artifacts || {})
const artifactRows = computed(() => Object.entries(artifacts.value).map(([name, item]) => ({ name, ...(item || {}) })))
const strategyPositionSummary = computed(() => '主升/强突/震荡/G2 50%；恐慌出清 25%-50%')
const readinessSummary = computed(() => readinessReview.value.summary || {})
const premarketCommand = computed(() => premarketControl.value.command || {})
const brokerSyncPreflight = computed(() => brokerSyncPreflightPayload.value || {})
const brokerSyncConfirmationPacket = computed(() => brokerSyncConfirmationPacketPayload.value || {})
const brokerSyncConfirmationFingerprint = computed(() => brokerSyncConfirmationPacket.value.action_fingerprint || {})
const brokerSyncOutcome = computed(() => brokerSyncOutcomePayload.value.outcome || {})
const brokerSyncLatestAttempt = computed(() => brokerSyncOutcomePayload.value.latest_sync_attempt || {})
const brokerSyncOutcomeBroker = computed(() => brokerSyncOutcomePayload.value.broker || {})
const brokerPostSyncAcceptance = computed(() => brokerPostSyncAcceptancePayload.value.acceptance || {})
const brokerPostSyncAcceptanceGaps = computed(() => Array.isArray(brokerPostSyncAcceptancePayload.value.gaps) ? brokerPostSyncAcceptancePayload.value.gaps : [])
const brokerPostSyncAcceptanceBroker = computed(() => brokerPostSyncAcceptancePayload.value.broker || {})
const liveActionConsoleSummary = computed(() => liveActionConsolePayload.value.summary || {})
const liveActionConsoleSteps = computed(() => Array.isArray(liveActionConsolePayload.value.steps) ? liveActionConsolePayload.value.steps : [])
const launchPacket = computed(() => liveLaunchPacketPayload.value.packet || {})
const launchPacketSummary = computed(() => launchPacket.value.summary || {})
const liveLaunchDecision = computed(() => liveLaunchDecisionPayload.value.decision || {})
const liveLaunchDecisionTone = computed(() => {
  const tone = String(liveLaunchDecision.value.risk_tone || '').trim()
  if (tone === 'block') return 'block'
  if (tone === 'ready') return 'ready'
  return 'observe'
})
const liveLaunchDecisionRiskRows = computed(() => Array.isArray(liveLaunchDecisionPayload.value.top_learning_risks) ? liveLaunchDecisionPayload.value.top_learning_risks : [])
const liveBlockerEvidenceBoardSummary = computed(() => liveBlockerEvidenceBoardPayload.value.summary || {})
const liveBlockerEvidenceBoardRows = computed(() => Array.isArray(liveBlockerEvidenceBoardPayload.value.rows) ? liveBlockerEvidenceBoardPayload.value.rows : [])
const liveLaunchReadinessAudit = computed(() => liveLaunchReadinessAuditPayload.value.audit || {})
const liveLaunchReadinessAuditSummary = computed(() => liveLaunchReadinessAuditPayload.value.summary || {})
const liveLaunchReadinessHardGaps = computed(() => Array.isArray(liveLaunchReadinessAuditPayload.value.hard_gaps) ? liveLaunchReadinessAuditPayload.value.hard_gaps : [])
const strategyTuningAxisSummary = computed(() => strategyTuningAxisBoardPayload.value.summary || {})
const strategyTuningAxisRows = computed(() => Array.isArray(strategyTuningAxisBoardPayload.value.axis_rows) ? strategyTuningAxisBoardPayload.value.axis_rows : [])
const strategyTuningRiskRows = computed(() => Array.isArray(strategyTuningAxisBoardPayload.value.risk_rows) ? strategyTuningAxisBoardPayload.value.risk_rows : [])
const strategyTuningReviewQueueSummary = computed(() => strategyTuningReviewQueuePayload.value.summary || {})
const strategyTuningReviewAxisSummaryRows = computed(() => Array.isArray(strategyTuningReviewQueuePayload.value.axis_task_summary) ? strategyTuningReviewQueuePayload.value.axis_task_summary : [])
const strategyTuningReviewTaskRows = computed(() => Array.isArray(strategyTuningReviewQueuePayload.value.task_rows) ? strategyTuningReviewQueuePayload.value.task_rows : [])
const strategyTuningReplaySuggestionSummary = computed(() => strategyTuningReplaySuggestionsPayload.value.summary || {})
const strategyTuningReplaySuggestionRows = computed(() => Array.isArray(strategyTuningReplaySuggestionsPayload.value.suggestions) ? strategyTuningReplaySuggestionsPayload.value.suggestions : [])
const strategyTuningReplaySession = computed(() => strategyTuningReplaySessionPayload.value.session || {})
const strategyTuningReplaySessionSummary = computed(() => strategyTuningReplaySessionPayload.value.summary || {})
const strategyTuningReplaySessionSteps = computed(() => Array.isArray(strategyTuningReplaySessionPayload.value.session_steps) ? strategyTuningReplaySessionPayload.value.session_steps : [])
const strategyTuningCurrentStepCompletionPacket = computed(() => strategyTuningCurrentStepCompletionPacketPayload.value.packet || {})
const strategyTuningCurrentStepCompletionGaps = computed(() => Array.isArray(strategyTuningCurrentStepCompletionPacket.value.gaps) ? strategyTuningCurrentStepCompletionPacket.value.gaps : [])
const liveReplayCockpitSummary = computed(() => liveReplayCockpitPayload.value.summary || {})
const liveReplayCockpitActionItems = computed(() => Array.isArray(liveReplayCockpitPayload.value.action_items) ? liveReplayCockpitPayload.value.action_items : [])
const liveReplayCockpitAxisProgress = computed(() => Array.isArray(liveReplayCockpitPayload.value.axis_progress) ? liveReplayCockpitPayload.value.axis_progress : [])
const liveReplayCockpitChecks = computed(() => Array.isArray(liveReplayCockpitPayload.value.natural_trade_checks) ? liveReplayCockpitPayload.value.natural_trade_checks : [])
const historicalDecisionReplaySummary = computed(() => historicalDecisionReplayPayload.value.summary || {})
const historicalDecisionReplayTasks = computed(() => Array.isArray(historicalDecisionReplayPayload.value.tasks) ? historicalDecisionReplayPayload.value.tasks : [])
const historicalDecisionReplayAudit = computed(() => historicalDecisionReplayAuditPayload.value.audit || {})
const historicalDecisionReplayAuditSummary = computed(() => historicalDecisionReplayAuditPayload.value.summary || {})
const historicalDecisionReplayAxisRows = computed(() => Array.isArray(historicalDecisionReplayAuditPayload.value.axis_audit_rows) ? historicalDecisionReplayAuditPayload.value.axis_audit_rows : [])
const historicalDecisionReplayHardGaps = computed(() => Array.isArray(historicalDecisionReplayAuditPayload.value.hard_gaps) ? historicalDecisionReplayAuditPayload.value.hard_gaps : [])
const strategyTuningCurrentStepBrokerEvidence = computed(() => {
  const evidence = strategyTuningCurrentStepCompletionPacket.value.current_evidence || {}
  return evidence.broker_sync || {}
})
const strategyTuningCompletionAudit = computed(() => strategyTuningCompletionAuditPayload.value.audit || {})
const strategyTuningCompletionSummary = computed(() => strategyTuningCompletionAuditPayload.value.summary || {})
const strategyTuningCompletionAxisRows = computed(() => Array.isArray(strategyTuningCompletionAuditPayload.value.axis_completion_rows) ? strategyTuningCompletionAuditPayload.value.axis_completion_rows : [])
const strategyTuningCompletionOpenTasks = computed(() => Array.isArray(strategyTuningCompletionAuditPayload.value.top_open_tasks) ? strategyTuningCompletionAuditPayload.value.top_open_tasks : [])
const liveLaunchStepRows = computed(() => {
  if (Array.isArray(liveLaunchPacketPayload.value.premarket_step_cards) && liveLaunchPacketPayload.value.premarket_step_cards.length) {
    return liveLaunchPacketPayload.value.premarket_step_cards
  }
  return Array.isArray(launchPacket.value.launch_steps) ? launchPacket.value.launch_steps : []
})
const liveLaunchReviewAxisRows = computed(() => {
  if (Array.isArray(liveLaunchPacketPayload.value.review_axis_matrix) && liveLaunchPacketPayload.value.review_axis_matrix.length) {
    return liveLaunchPacketPayload.value.review_axis_matrix
  }
  return Array.isArray(launchPacket.value.review_axis_matrix) ? launchPacket.value.review_axis_matrix : []
})
const liveLaunchLearningQueueRows = computed(() => {
  if (Array.isArray(liveLaunchPacketPayload.value.launch_day_learning_queue) && liveLaunchPacketPayload.value.launch_day_learning_queue.length) {
    return liveLaunchPacketPayload.value.launch_day_learning_queue
  }
  return Array.isArray(launchPacket.value.launch_day_learning_queue) ? launchPacket.value.launch_day_learning_queue : []
})
const liveLaunchDayPlaybookRows = computed(() => {
  if (Array.isArray(liveLaunchPacketPayload.value.launch_day_playbook) && liveLaunchPacketPayload.value.launch_day_playbook.length) {
    return liveLaunchPacketPayload.value.launch_day_playbook
  }
  return Array.isArray(launchPacket.value.launch_day_playbook) ? launchPacket.value.launch_day_playbook : []
})
const liveLaunchReviewSnapshotRows = computed(() => {
  return [...liveLaunchReviewSnapshotLedger.value].sort((a, b) => String(b?.generated_at || '').localeCompare(String(a?.generated_at || '')))
})
const liveLearningLedgerRows = computed(() => Array.isArray(liveLearningLedgerPayload.value.learning_ledger) ? liveLearningLedgerPayload.value.learning_ledger : [])
const liveLaunchSnapshotDetail = computed(() => liveLaunchSnapshotDetailPayload.value.detail || liveLaunchSnapshotDetailPayload.value.snapshot || null)
const liveLaunchSnapshotPremarketRows = computed(() => {
  const command = liveLaunchSnapshotDetail.value?.premarket_command || {}
  const action = command.current_action || {}
  return Object.keys(action).length ? [action] : []
})
const liveLaunchSnapshotPlaybookRows = computed(() => {
  const rows = liveLaunchSnapshotDetail.value?.launch_day_playbook
  return Array.isArray(rows) ? rows : []
})
const liveLaunchSnapshotLearningRows = computed(() => {
  const rows = liveLaunchSnapshotDetail.value?.launch_day_learning_queue
  return Array.isArray(rows) ? rows : []
})
const liveLaunchSnapshotLearningLedgerRows = computed(() => {
  const rows = liveLaunchSnapshotDetail.value?.live_learning_ledger
  return Array.isArray(rows) ? rows : []
})
const launchPacketStatusType = computed(() => {
  const status = String(launchPacket.value.status || '').trim()
  if (status === 'manual_review_ready') return 'success'
  if (status === 'blocked') return 'danger'
  return 'warning'
})
const readinessGates = computed(() => Array.isArray(readinessReview.value.readiness_gates) ? readinessReview.value.readiness_gates : [])
const pretradeActionRows = computed(() => Array.isArray(readinessReview.value.pretrade_action_checklist) ? readinessReview.value.pretrade_action_checklist : [])
const formalLaunchRows = computed(() => Array.isArray(readinessReview.value.formal_launch_checklist) ? readinessReview.value.formal_launch_checklist : [])
const executionModeRows = computed(() => Array.isArray(readinessReview.value.execution_mode_matrix) ? readinessReview.value.execution_mode_matrix : [])
const formalLaunchActionRows = computed(() => Array.isArray(readinessReview.value.formal_launch_action_queue) ? readinessReview.value.formal_launch_action_queue : [])
const premarketPlaybookRows = computed(() => Array.isArray(readinessReview.value.premarket_execution_playbook) ? readinessReview.value.premarket_execution_playbook : [])
const firstLiveDecisionCard = computed(() => {
  const rows = Array.isArray(readinessReview.value.first_live_decision_card) ? readinessReview.value.first_live_decision_card : []
  return rows[0] || null
})
const liveAdmissionSnapshotCard = computed(() => {
  const rows = Array.isArray(readinessReview.value.live_admission_snapshot) ? readinessReview.value.live_admission_snapshot : []
  return rows[0] || null
})
const firstLiveDecisionTone = computed(() => {
  const tone = String(firstLiveDecisionCard.value?.risk_tone || '').trim()
  if (['block', 'ready', 'watch', 'observe'].includes(tone)) return tone
  return 'watch'
})
const liveAdmissionTone = computed(() => {
  const tone = String(liveAdmissionSnapshotCard.value?.risk_tone || '').trim()
  if (['block', 'ready', 'watch', 'observe'].includes(tone)) return tone
  if (truthy(liveAdmissionSnapshotCard.value?.live_buy_allowed)) return 'ready'
  return 'watch'
})
const liveReviewTaskQueueRows = computed(() => Array.isArray(readinessReview.value.live_review_task_queue) ? readinessReview.value.live_review_task_queue : [])
const livePremarketCommandRows = computed(() => Array.isArray(readinessReview.value.live_premarket_command_sheet) ? readinessReview.value.live_premarket_command_sheet : [])
const livePremarketActionSequenceRows = computed(() => Array.isArray(readinessReview.value.live_premarket_action_sequence) ? readinessReview.value.live_premarket_action_sequence : [])
const livePremarketExecutionRecheckRows = computed(() => Array.isArray(readinessReview.value.live_premarket_execution_recheck) ? readinessReview.value.live_premarket_execution_recheck : [])
const livePremarketActionAttemptRows = computed(() => Array.isArray(readinessReview.value.live_premarket_action_attempts) ? readinessReview.value.live_premarket_action_attempts : [])
const liveManualLaunchAcceptanceRows = computed(() => Array.isArray(readinessReview.value.live_manual_launch_acceptance) ? readinessReview.value.live_manual_launch_acceptance : [])
const liveDay1ReviewJournalRows = computed(() => Array.isArray(readinessReview.value.live_day1_review_journal) ? readinessReview.value.live_day1_review_journal : [])
const liveBlockerResolutionRows = computed(() => Array.isArray(readinessReview.value.live_blocker_resolution_plan) ? readinessReview.value.live_blocker_resolution_plan : [])
const liveBlockerEvidenceRows = computed(() => Array.isArray(readinessReview.value.live_blocker_evidence_ledger) ? readinessReview.value.live_blocker_evidence_ledger : [])
const reviewCoverageDashboardRows = computed(() => Array.isArray(readinessReview.value.review_coverage_dashboard) ? readinessReview.value.review_coverage_dashboard : [])
const liveReviewEvidenceRubricRows = computed(() => Array.isArray(readinessReview.value.live_review_evidence_rubric) ? readinessReview.value.live_review_evidence_rubric : [])
const dailyLiveReviewBoardRows = computed(() => Array.isArray(readinessReview.value.daily_live_review_board) ? readinessReview.value.daily_live_review_board : [])
const strategyLearningBacklogRows = computed(() => Array.isArray(readinessReview.value.strategy_learning_backlog) ? readinessReview.value.strategy_learning_backlog : [])
const liveHiddenRiskWatchlistRows = computed(() => Array.isArray(readinessReview.value.live_hidden_risk_watchlist) ? readinessReview.value.live_hidden_risk_watchlist : [])
const liveDailyReviewActionLayerRows = computed(() => Array.isArray(readinessReview.value.live_daily_review_action_layers) ? readinessReview.value.live_daily_review_action_layers : [])
const liveDailyReviewExecutionChecklistRows = computed(() => Array.isArray(readinessReview.value.live_daily_review_execution_checklist) ? readinessReview.value.live_daily_review_execution_checklist : [])
const noTradeDayReviewRows = computed(() => Array.isArray(readinessReview.value.no_trade_day_review) ? readinessReview.value.no_trade_day_review : [])
const day1PaperReviewRows = computed(() => Array.isArray(readinessReview.value.day1_paper_review_pack) ? readinessReview.value.day1_paper_review_pack : [])
const day1AfterCloseReviewRows = computed(() => Array.isArray(readinessReview.value.day1_after_close_review_queue) ? readinessReview.value.day1_after_close_review_queue : [])
const pretradeEvidenceRows = computed(() => Array.isArray(readinessReview.value.pretrade_review_evidence) ? readinessReview.value.pretrade_review_evidence : [])
const naturalConsistencyRows = computed(() => Array.isArray(readinessReview.value.natural_trade_consistency_review) ? readinessReview.value.natural_trade_consistency_review : [])
const naturalExecutionRows = computed(() => Array.isArray(readinessReview.value.natural_execution_decision_matrix) ? readinessReview.value.natural_execution_decision_matrix : [])
const reviewTicketRows = computed(() => Array.isArray(readinessReview.value.next_trade_ticket_review) ? readinessReview.value.next_trade_ticket_review : [])
const unreviewedTicketRows = computed(() => reviewTicketRows.value.filter((row) => !String(row?.pretrade_review_action || '').trim()))
const pretradeReviewLedgerRows = computed(() => {
  return [...pretradeReviewLedger.value].sort((a, b) => String(b?.updated_at || '').localeCompare(String(a?.updated_at || '')))
})
const paperWatchFollowupRows = computed(() => {
  if (Array.isArray(readinessReview.value.paper_watch_followup) && readinessReview.value.paper_watch_followup.length) {
    return readinessReview.value.paper_watch_followup
  }
  const reviewMap = new Map(paperWatchReviewLedger.value.map((item) => [String(item?.key || item?.ticket_key || ''), item]))
  return pretradeReviewLedgerRows.value
    .filter((row) => row?.review_action === 'paper_watch')
    .map((row, index) => {
      const key = reviewRowKey(row)
      const review = reviewMap.get(key) || {}
      return {
        rank: index + 1,
        ...row,
        ...review,
        ticket_key: row.ticket_key || review.ticket_key || key,
        watch_result_label: review.watch_result_label || '待观察',
        followup_status: review.watch_result ? (review.watch_result === 'as_expected' ? 'validated' : review.watch_result === 'continue_watch' ? 'continue_watch' : 'issue_found') : 'pending_observation'
      }
    })
})
const ticketChecklistRows = computed(() => Array.isArray(readinessReview.value.ticket_review_checklist) ? readinessReview.value.ticket_review_checklist : [])
const holdingRefreshRows = computed(() => Array.isArray(readinessReview.value.holding_refresh_evidence) ? readinessReview.value.holding_refresh_evidence : [])
const holdingChecklistRows = computed(() => Array.isArray(readinessReview.value.holding_exit_checklist) ? readinessReview.value.holding_exit_checklist : [])
const candidateChecklistRows = computed(() => Array.isArray(readinessReview.value.candidate_omission_checklist) ? readinessReview.value.candidate_omission_checklist : [])
const hazardRows = computed(() => Array.isArray(readinessReview.value.hazard_register) ? readinessReview.value.hazard_register : [])
const readinessReviewTagType = computed(() => {
  const verdict = String(readinessSummary.value.verdict || '')
  if (verdict.includes('blocked')) return 'danger'
  if (verdict.includes('warning')) return 'warning'
  if (verdict.includes('ready')) return 'success'
  return 'info'
})

const portfolioRows = computed(() => [
  { label: '最大槽位', value: portfolio.value.max_slots ?? portfolio.value.slots ?? '--' },
  { label: '单槽仓位', value: pct(portfolio.value.per_slot_position_pct || portfolio.value.slot_pct) },
  { label: '最大总仓', value: pct(portfolio.value.max_total_position_pct) },
  { label: '正式策略数', value: portfolio.value.trade_strategy_count ?? (tradeStrategyPolicy.value.length || '--') },
  { label: '策略框架', value: portfolio.value.trade_strategy_framework || '--' },
  { label: '账户风险闸门', value: contract.value.guardrails?.account_risk_gate || contract.value.account_risk_gate || '--' },
  { label: '五策略仓位', value: strategyPositionSummary.value }
])

const exitRows = computed(() => [
  { label: '硬止损', value: pct(exitContract.value.hard_stop_pct) },
  { label: '第一止盈', value: pct(exitContract.value.first_take_profit_pct) },
  { label: '前低保护', value: exitContract.value.previous_low_protection || exitContract.value.low_protection || '--' },
  { label: '结构止损', value: exitContract.value.structure_stop || exitContract.value.structure_stop_policy || '--' },
  { label: '回撤减仓', value: pct(exitContract.value.risk_limits?.mtm_drawdown_reduce_risk_pct || exitContract.value.mtm_drawdown_reduce_risk_pct) },
  { label: '合同说明', value: exitContract.value.description || contract.value.default_exit_contract || '--' }
])

const guardrailRows = computed(() => [
  { label: '正式买点', ok: currentPayload.value.formal_buy_signal === false, detail: `formal_buy_signal=${currentPayload.value.formal_buy_signal}` },
  { label: '自动下单', ok: currentPayload.value.auto_order_allowed === false, detail: `auto_order_allowed=${currentPayload.value.auto_order_allowed}` },
  { label: '订单路径', ok: currentPayload.value.order_path_enabled === false, detail: `order_path_enabled=${currentPayload.value.order_path_enabled}` },
  { label: '影子模式', ok: currentPayload.value.shadow_only === true, detail: `shadow_only=${currentPayload.value.shadow_only}` },
  { label: '观察模式', ok: currentPayload.value.observe_only === true, detail: `observe_only=${currentPayload.value.observe_only}` }
])

function truthy(value) {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0
  if (typeof value === 'string') return ['1', 'true', 'yes', 'ok', 'pass'].includes(value.trim().toLowerCase())
  return false
}

function pct(value) {
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : '--'
}

function price(value) {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(2) : '--'
}

function listText(value) {
  return Array.isArray(value) ? value.join('、') : (value || '--')
}

function gateType(status) {
  if (status === 'pass' || status === 'ready') return 'success'
  if (status === 'warn' || status === 'watch' || status === 'manual_shadow_ready_with_warnings') return 'warning'
  if (status === 'block' || status === 'blocked') return 'danger'
  return 'info'
}

function pretradeReviewType(action) {
  if (action === 'manual_approved') return 'success'
  if (action === 'paper_watch') return 'primary'
  if (action === 'wait_refresh') return 'warning'
  if (action === 'skip' || action === 'reject') return 'danger'
  return 'info'
}

function paperWatchResultLabel(result) {
  const labels = {
    as_expected: '符合预期',
    buy_point_too_early: '买点偏早',
    buy_point_chasing: '买点追高',
    selection_issue: '选股隐患',
    model_switch_issue: '策略切换隐患',
    risk_exit_issue: '风控/卖点隐患',
    missed_opportunity: '可能误杀',
    invalid_signal: '信号失效',
    continue_watch: '继续观察'
  }
  return labels[result] || result || ''
}

function paperWatchStatusType(status) {
  if (status === 'validated') return 'success'
  if (status === 'issue_found') return 'danger'
  if (status === 'continue_watch') return 'warning'
  return 'info'
}

function day1PostureType(posture) {
  if (posture === 'formal_candidate_after_manual_review') return 'success'
  if (posture === 'contract_block_observation') return 'danger'
  if (posture === 'paper_watch' || posture === 'unreviewed_to_paper_watch') return 'primary'
  if (posture === 'wait_refresh') return 'warning'
  if (posture === 'skip') return 'info'
  return 'info'
}

function day1PostureLabel(posture) {
  const labels = {
    formal_candidate_after_manual_review: '人工候选',
    contract_block_observation: '合同阻断观察',
    paper_watch: '纸面观察',
    unreviewed_to_paper_watch: '先纸面',
    wait_refresh: '待刷新',
    skip: '跳过'
  }
  return labels[posture] || posture || '--'
}

function afterCloseStatusType(status) {
  if (status === 'validated') return 'success'
  if (status === 'issue_found') return 'danger'
  if (status === 'contract_block_observation') return 'danger'
  if (status === 'pending_after_close_review' || status === 'continue_watch') return 'warning'
  if (status === 'pending_paper_execution') return 'info'
  return 'info'
}

function afterCloseStatusLabel(status) {
  const labels = {
    pending_paper_execution: '待写执行',
    contract_block_observation: '合同阻断观察',
    pending_after_close_review: '待盘后复盘',
    validated: '已验证',
    continue_watch: '继续观察',
    issue_found: '发现隐患'
  }
  return labels[status] || status || '--'
}

function noTradeReviewResultLabel(result) {
  const labels = {
    natural_no_trade: '空仓合理',
    missed_opportunity: '可能误杀',
    data_gap: '数据缺口',
    process_gap: '流程缺口',
    continue_watch: '继续观察'
  }
  return labels[result] || result || ''
}

function noTradeReviewStatusType(status) {
  if (status === 'validated') return 'success'
  if (status === 'issue_found') return 'danger'
  if (status === 'continue_watch') return 'warning'
  return 'info'
}

function reviewBoardStatusType(status) {
  if (status === 'validated' || status === 'pass' || status === 'ready' || status === 'covered') return 'success'
  if (status === 'ready_for_manual_live_review') return 'success'
  if (status === 'issue_found' || status === 'block' || status === 'issue_found_hold_live') return 'danger'
  if (String(status || '').startsWith('pending') || status === 'warn' || status === 'watch' || status === 'continue_watch' || status === 'waiting_previous_step') return 'warning'
  if (status === 'next_action_required') return 'warning'
  return 'info'
}

function reviewBoardStatusLabel(status) {
  const labels = {
    no_buy_observe_pending: '无票观察待处理',
    no_buy_observe_ready: '无票观察',
    manual_live_ready: '人工实盘待确认',
    ticket_review_or_formal_action_pending: '有票未放行',
    blocked_review_required: '阻断复盘',
    paper_or_manual_review_pending: '复盘确认中',
    pending: '待处理',
    pending_review: '待复盘',
    pending_observation: '待观察',
    pending_after_close_review: '待盘后复盘',
    pending_paper_execution: '待纸面执行',
    pending_evidence: '待证据',
    next_action_required: '继续下一步',
    ready_for_manual_live_review: '人工实盘复核',
    issue_found_hold_live: '隐患禁止实盘',
    observe_no_blocker: '无阻断观察',
    needs_review: '需调优复盘',
    watch_more: '继续观察',
    issue_found: '发现隐患',
    block: '阻断',
    warn: '警告',
    watch: '观察',
    continue_watch: '继续观察',
    covered: '已覆盖',
    validated: '已验证',
    pass: '通过',
    ready: '就绪',
    waiting_previous_step: '等待前置步骤'
  }
  return labels[status] || status || '--'
}

function taskToReviewRow(row = {}) {
  return {
    ...row,
    review_key: row.review_key || row.blocking_key || row.primary_blocking_key || row.ledger_key,
    review_status: row.task_status || row.review_status,
    stage: row.window,
    required_action: row.required_action || row.action,
    review_type: row.review_type || row.object,
    posture: row.posture || row.task_status,
    evidence: row.evidence || row.acceptance,
    natural_decision: row.natural_decision || row.review_method,
    optimization_focus: row.optimization_focus || row.fallback,
    source: row.source || row.source_table,
    action_recommendation: row.action_recommendation || row.action,
    hidden_risks: row.hidden_risks || row.fallback
  }
}

function isDay1JournalNoTradeRow(row = {}) {
  const key = String(row.review_key || '')
  const type = String(row.journal_type || '')
  const action = String(row.action || row.next_action || '')
  return /^\d{4}-\d{2}-\d{2}\|/.test(key) || type.includes('无票') || action.includes('无票') || action.includes('归因')
}

function day1JournalRowLoading(row = {}) {
  const source = String(row.source_table || '')
  if (source === 'live_daily_review_execution_checklist') return dailyReviewChecklistRowLoading(row)
  if (source === 'live_blocker_evidence_ledger' && isDay1JournalNoTradeRow(row)) return reviewLoading.value
  return formalActionRowLoading(taskToReviewRow(row)) || reviewLoading.value
}

function dailyReviewChecklistRowKey(row = {}) {
  return String(row.review_key || row.ticket_key || [
    row.review_axis || '',
    row.review_scope || '',
    row.object || '',
    row.code || '',
    row.entry_date || ''
  ].join('|')).trim()
}

function dailyReviewChecklistRowLoading(row = {}) {
  return dailyReviewChecklistSavingKey.value === dailyReviewChecklistRowKey(row) || reviewLoading.value
}

function launchDayPlaybookRowKey(row = {}) {
  return String(row.review_key || [
    row.window || '',
    row.checkpoint_time || '',
    row.review_axis || '',
    row.object || '',
    row.action_type || ''
  ].join('|')).trim()
}

function launchDayPlaybookRowLoading(row = {}) {
  return launchDayPlaybookSavingKey.value === launchDayPlaybookRowKey(row) || liveLaunchPacketLoading.value
}

function strategyTuningTaskRowKey(row = {}) {
  return String(row.task_key || [
    row.axis || '',
    row.origin || '',
    row.object || '',
    String(row.problem_signal || '').slice(0, 80)
  ].join('|')).trim()
}

function isStrategyTuningTaskSaving(row = {}) {
  return strategyTuningTaskSavingKey.value === strategyTuningTaskRowKey(row)
}

function launchDayPlaybookStatusType(status) {
  if (status === 'validated') return 'success'
  if (status === 'issue_found') return 'danger'
  if (status === 'continue_watch') return 'warning'
  return 'info'
}

function launchDayPlaybookStatusLabel(status, label) {
  if (label) return label
  const labels = {
    validated: '符合预期',
    issue_found: '发现隐患',
    continue_watch: '继续观察',
    pending_review: '待复盘'
  }
  return labels[status] || status || '待复盘'
}

function upsertLiveLaunchSnapshot(snapshot = {}) {
  const key = String(snapshot?.snapshot_id || '')
  if (!key) return
  liveLaunchReviewSnapshotLedger.value = [
    snapshot,
    ...liveLaunchReviewSnapshotLedger.value.filter((item) => String(item?.snapshot_id || '') !== key)
  ]
}

async function openLiveLaunchSnapshotDetail(row = {}) {
  const snapshotId = String(row?.snapshot_id || '').trim()
  if (!snapshotId) {
    ElMessage.warning('缺少快照编号')
    return
  }
  liveLaunchSnapshotDetailId.value = snapshotId
  liveLaunchSnapshotDetailLoading.value = true
  try {
    const result = await getGen3StateAlphaLiveLaunchReviewSnapshotDetail(snapshotId)
    if (!result?.ok) {
      ElMessage.warning(result?.error || '读取启动复盘快照详情失败')
      return
    }
    liveLaunchSnapshotDetailPayload.value = result || {}
    liveLaunchSnapshotDetailVisible.value = true
  } catch (error) {
    ElMessage.warning(error?.message || '读取启动复盘快照详情失败')
  } finally {
    liveLaunchSnapshotDetailLoading.value = false
  }
}

function dailyReviewChecklistResultLabel(result) {
  const labels = {
    validated: '符合预期',
    issue_found: '发现隐患',
    continue_watch: '继续观察',
    blocked: '卡住/阻断',
    data_gap: '数据缺口',
    process_gap: '流程缺口'
  }
  return labels[result] || result || '--'
}

function dailyReviewChecklistAutoText(result, row = {}) {
  if (result === 'validated') {
    return {
      naturalDecision: '复盘证据符合当前合同，不因单日收益倒推改规则。',
      suggestion: row.pass_condition || '保留当前规则，继续积累同类样本。'
    }
  }
  if (result === 'continue_watch') {
    return {
      naturalDecision: '证据不足，继续观察，不放宽规则。',
      suggestion: row.target_action || '继续补齐走势、成交、阻断和切换证据后再判断。'
    }
  }
  if (result === 'data_gap') {
    return {
      naturalDecision: '先补数据证据，不把数据缺口当成交易模型结论。',
      suggestion: row.evidence_to_collect || row.target_action || '补齐行情、持仓、价格或审计证据。'
    }
  }
  return {
    naturalDecision: '发现隐患，进入对应买点、卖点、选股、策略切换或执行证据复盘。',
    suggestion: row.fail_condition || row.target_action || '按复盘轴定位规则缺口，不以单笔收益作为唯一优化目标。'
  }
}

function launchDayPlaybookAutoText(result, row = {}) {
  if (result === 'validated') {
    return {
      naturalDecision: '启动日动作符合当前合同与自然交易边界，保留规则并继续积累同类样本。',
      suggestion: row.pass_condition || '保留当前启动剧本，不因单日收益倒推放宽规则。'
    }
  }
  if (result === 'continue_watch') {
    return {
      naturalDecision: '证据仍不足，继续观察，不提前切换策略或放宽买点。',
      suggestion: row.required_action || row.evidence_to_collect || '继续补齐走势、执行、候选与切换证据。'
    }
  }
  if (result === 'data_gap') {
    return {
      naturalDecision: '先补齐数据与执行证据，不把数据缺口误判为策略结论。',
      suggestion: row.evidence_to_collect || '补齐真实持仓、价格、候选、买卖点或审计证据。'
    }
  }
  return {
    naturalDecision: '发现启动日隐患，进入买点、卖点、选股、模型切换或执行证据复盘，不直接修改合同。',
    suggestion: row.fail_condition || row.required_action || '定位隐患归因，形成可复验样本后再决定是否优化。'
  }
}

async function saveLaunchDayPlaybookReview(row, result) {
  const key = launchDayPlaybookRowKey(row)
  const label = launchDayPlaybookStatusLabel('', dailyReviewChecklistResultLabel(result))
  let note = ''
  try {
    const response = await ElMessageBox.prompt(
      `记录启动日剧本复盘：${row?.checkpoint_time || '--'} / ${row?.action_type || '--'}，结果为“${label}”。请写一句证据或判断。`,
      '启动日剧本落账',
      {
        confirmButtonText: '保存复盘',
        cancelButtonText: '取消',
        inputType: 'textarea',
        inputPlaceholder: '例如：同步真实持仓后 blocker 仍未消失；或：无票日归因符合自然空仓，不补票。',
        inputValidator: (value) => String(value || '').trim().length >= 4 || '请至少写一句复盘依据'
      }
    )
    note = String(response?.value || '').trim()
  } catch {
    return
  }
  const autoText = launchDayPlaybookAutoText(result, row)
  launchDayPlaybookSavingKey.value = key
  try {
    const saved = await saveGen3StateAlphaLaunchDayPlaybookReviewAndRun({
      review_key: row.review_key || key,
      priority: row.priority,
      window: row.window,
      checkpoint_time: row.checkpoint_time,
      action_type: row.action_type,
      review_axis: row.review_axis,
      object: row.object,
      required_action: row.required_action,
      evidence_to_collect: row.evidence_to_collect,
      pass_condition: row.pass_condition,
      fail_condition: row.fail_condition,
      review_result: result,
      issue_area: row.review_axis,
      evidence: row.evidence_to_collect,
      natural_decision: autoText.naturalDecision,
      optimization_suggestion: autoText.suggestion,
      review_note: note,
      source: 'launch_day_playbook'
    })
    if (!saved?.ok) {
      ElMessage.warning(saved?.error || '启动日剧本复盘保存失败')
      return
    }
    if (saved.packet) {
      liveLaunchPacketPayload.value = saved.packet
    }
    liveLearningLedgerPayload.value = await getGen3StateAlphaLiveLearningLedger()
    liveLaunchDecisionPayload.value = await getGen3StateAlphaLiveLaunchDecision()
    liveBlockerEvidenceBoardPayload.value = await getGen3StateAlphaLiveBlockerEvidenceBoard()
    liveLaunchReadinessAuditPayload.value = await getGen3StateAlphaLiveLaunchReadinessAudit()
    strategyTuningAxisBoardPayload.value = await getGen3StateAlphaStrategyTuningAxisBoard()
    strategyTuningReviewQueuePayload.value = await getGen3StateAlphaStrategyTuningReviewQueue()
    strategyTuningReplaySuggestionsPayload.value = await getGen3StateAlphaStrategyTuningReplaySuggestions()
    strategyTuningReplaySessionPayload.value = await getGen3StateAlphaStrategyTuningReplaySession()
    strategyTuningCurrentStepCompletionPacketPayload.value = await getGen3StateAlphaStrategyTuningCurrentStepCompletionPacket()
    strategyTuningCompletionAuditPayload.value = await getGen3StateAlphaStrategyTuningCompletionAudit()
    liveReplayCockpitPayload.value = await getGen3StateAlphaLiveReplayCockpit()
    if (saved.live_launch_review_snapshot) {
      upsertLiveLaunchSnapshot(saved.live_launch_review_snapshot)
    }
    if (saved.save?.review) {
      launchDayPlaybookReviewLedger.value = [
        saved.save.review,
        ...launchDayPlaybookReviewLedger.value.filter((item) => String(item?.key || item?.review_key || '') !== key)
      ]
    }
    ElMessage.success('已保存启动日剧本复盘并重建启动包')
  } catch (error) {
    ElMessage.warning(error?.message || '启动日剧本复盘保存失败')
  } finally {
    launchDayPlaybookSavingKey.value = ''
  }
}

async function saveDailyReviewChecklistReview(row, result) {
  const key = dailyReviewChecklistRowKey(row)
  const label = dailyReviewChecklistResultLabel(result)
  let note = ''
  try {
    const response = await ElMessageBox.prompt(
      `记录逐项复盘：${reviewAxisLabel(row?.review_axis)} / ${row?.object || row?.code || '--'}，结果为“${label}”。请写一句证据或判断。`,
      '逐项复盘落账',
      {
        confirmButtonText: '保存复盘',
        cancelButtonText: '取消',
        inputType: 'textarea',
        inputPlaceholder: '例如：盘后走势验证为自然空仓；或：候选被挡后走强，需复核选股阻断理由。',
        inputValidator: (value) => String(value || '').trim().length >= 4 || '请至少写一句复盘依据'
      }
    )
    note = String(response?.value || '').trim()
  } catch {
    return
  }
  const autoText = dailyReviewChecklistAutoText(result, row)
  dailyReviewChecklistSavingKey.value = key
  try {
    const saved = await saveGen3StateAlphaDailyReviewChecklistReviewAndRun({
      review_key: row.review_key || key,
      ticket_key: row.ticket_key,
      review_axis: row.review_axis,
      review_scope: row.review_scope,
      object: row.object,
      code: row.code,
      name: row.name,
      entry_date: row.entry_date,
      review_result: result,
      issue_area: row.review_axis,
      evidence: row.evidence_to_collect || row.evidence_to_record,
      natural_decision: autoText.naturalDecision,
      optimization_suggestion: autoText.suggestion,
      review_note: note,
      source: row.source || 'live_daily_review_execution_checklist'
    })
    if (!saved?.ok) {
      ElMessage.warning(saved?.error || '逐项复盘保存失败')
      return
    }
    if (saved.review) {
      readinessReview.value = saved.review
    }
    ElMessage.success('已保存逐项复盘并完成复审')
    await refreshCurrentAndLedgers('daily_review_checklist_saved')
  } catch (error) {
    ElMessage.warning(error?.message || '逐项复盘保存失败')
  } finally {
    dailyReviewChecklistSavingKey.value = ''
  }
}

function learningStatusType(status) {
  if (status === 'needs_review') return 'danger'
  if (status === 'watch_more') return 'warning'
  if (status === 'validated_rule') return 'success'
  return 'info'
}

function learningStatusLabel(status) {
  const labels = {
    needs_review: '需调优',
    watch_more: '继续观察',
    validated_rule: '规则验证'
  }
  return labels[status] || status || '--'
}

function hiddenRiskLevelType(level) {
  if (level === 'high') return 'danger'
  if (level === 'medium') return 'warning'
  if (level === 'low') return 'info'
  return 'info'
}

function hiddenRiskLevelLabel(level) {
  const labels = {
    high: '高',
    medium: '中',
    low: '低'
  }
  return labels[level] || level || '--'
}

function reviewAxisLabel(axis) {
  const labels = {
    execution_evidence: '执行证据',
    selection: '选股模式',
    model_switch: '策略切换',
    sell_point: '卖点/退出',
    buy_point: '买点',
    natural_trade_consistency: '自然一致性'
  }
  return labels[axis] || axis || '--'
}

function formalActionRowKey(row) {
  return row?.review_key || [row?.source, row?.object, row?.required_action].filter(Boolean).join('|')
}

function formalActionButtonLabel(row) {
  const source = String(row?.source || '')
  const object = String(row?.object || '')
  if (source.includes('holding') || object.includes('holding') || object.includes('持仓') || /\d{6}/.test(object)) {
    return '同步持仓并复审'
  }
  if (source.includes('formal_launch') || object.includes('审计')) return '重跑审计'
  if (source.includes('readiness_gates')) return '重跑审计'
  return '查看来源'
}

function formalActionRowLoading(row) {
  return formalActionSavingKey.value === formalActionRowKey(row) || brokerSyncLoading.value || reviewLoading.value
}

function noTradeIssueArea(result) {
  if (result === 'missed_opportunity') return 'selection_or_switch'
  if (result === 'data_gap') return 'data_confirmation'
  if (result === 'process_gap') return 'execution_process'
  return 'observation'
}

function paperWatchIssueArea(result) {
  if (result === 'buy_point_too_early' || result === 'buy_point_chasing') return 'buy_point'
  if (result === 'selection_issue') return 'selection'
  if (result === 'model_switch_issue') return 'model_switch'
  if (result === 'risk_exit_issue') return 'sell_exit'
  if (result === 'invalid_signal' || result === 'missed_opportunity') return 'signal_validation'
  return 'observation'
}

function isCandidateOmissionReview(row) {
  return Boolean(
    row?.review_scope === 'candidate_omission' ||
    row?.source === 'candidate_omission_checklist' ||
    row?.selection_state ||
    row?.strategy_switch_assessment ||
    row?.omission_watch_status
  )
}

function executionModeType(status, allowed) {
  if (truthy(allowed)) return 'success'
  if (status === 'locked') return 'info'
  if (status === 'blocked' || status === 'requires_reaudit') return 'danger'
  if (status === 'not_ready') return 'warning'
  return 'info'
}

function holdingRefreshType(status) {
  if (status === 'fresh_enough') return 'success'
  if (status === 'formal_refresh_required') return 'warning'
  if (status === 'observation_refresh') return 'info'
  return 'info'
}

function naturalConsistencyType(grade) {
  if (grade === 'smooth') return 'success'
  if (grade === 'watch') return 'warning'
  if (grade === 'strained') return 'danger'
  if (grade === 'incoherent') return 'danger'
  return 'info'
}

function naturalConsistencyLabel(grade) {
  const labels = {
    smooth: '顺畅',
    watch: '观察',
    strained: '偏拧',
    incoherent: '不顺'
  }
  return labels[grade] || grade || '--'
}

function reviewRowKey(row) {
  return String(row?.ticket_key || row?.candidate_key || row?.trade_key || row?.code || '')
}

function isReviewSaving(row) {
  return reviewSavingKey.value && reviewSavingKey.value === reviewRowKey(row)
}

function ticketApprovalChecklist(row) {
  const code = String(row?.code || '').trim()
  const matched = ticketChecklistRows.value.find((item) => String(item?.code || '').trim() === code) || {}
  return matched.manual_approval_checklist || row?.manual_approval_checklist || ''
}

function naturalExecutionDecision(row) {
  const code = String(row?.code || '').trim()
  return naturalExecutionRows.value.find((item) => String(item?.code || '').trim() === code) || {}
}

function naturalConsistencyReview(row) {
  const code = String(row?.code || '').trim()
  return naturalConsistencyRows.value.find((item) => String(item?.code || '').trim() === code) || {}
}

function splitReviewItems(text) {
  return String(text || '')
    .split(/[；;、\n]/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function pretradeConfirmationItems(row) {
  const ticketItems = splitReviewItems(ticketApprovalChecklist(row))
  const decision = naturalExecutionDecision(row)
  const items = [
    ...ticketItems,
    decision.required_confirmation,
    decision.downgrade_rule ? `降级规则：${decision.downgrade_rule}` : '',
    '确认本次记录不会触发自动下单，正式执行仍需盘前价格/持仓复核'
  ].filter(Boolean)
  return Array.from(new Set(items))
}

function reviewDecisionReason(action, row, naturalDecision, naturalConsistency) {
  const codeName = `${row?.code || ''} ${row?.name || ''}`.trim()
  const deductions = naturalConsistency?.deductions || naturalDecision?.source_deductions || ''
  if (action === 'manual_approved') {
    return `${codeName} 已完成人工放行复核；核心依据：${deductions || naturalDecision?.required_confirmation || '逐票确认通过'}`
  }
  if (action === 'paper_watch') {
    return `${codeName} 先纸面观察；原因：${deductions || naturalDecision?.required_confirmation || '实战前仍需观察买点、热度和执行约束'}`
  }
  if (action === 'wait_refresh') {
    return `${codeName} 等待刷新后再判断；原因：${naturalConsistency?.readiness_gaps || naturalDecision?.required_confirmation || '价格、持仓或信号证据未闭环'}`
  }
  if (action === 'skip') {
    return `${codeName} 本次跳过；原因：${deductions || naturalDecision?.downgrade_rule || '人工判断不进入本次执行'}`
  }
  return `${codeName} 记录逐票复盘结论`
}

function nextReviewTrigger(action, naturalDecision) {
  if (action === 'manual_approved') return '盘前同步真实持仓和盘口价格后重跑实战前审计'
  if (action === 'paper_watch') return '收盘后对照次日走势、买点新鲜度和策略解释复盘'
  if (action === 'wait_refresh') return '完成真实持仓/价格/信号刷新后重跑实战前审计'
  if (action === 'skip') return naturalDecision?.downgrade_rule || '本次不再进入执行，收盘后复盘是否误杀'
  return '重跑 G3 实战前审计'
}

function reviewNotes(action) {
  const notes = {
    paper_watch: '实战前逐票复盘：先按纸面观察记录，不开启自动实盘',
    manual_approved: '实战前逐票复盘：人工确认票据、风控与执行约束后放行',
    wait_refresh: '实战前逐票复盘：等待价格/持仓/信号刷新后再判断',
    skip: '实战前逐票复盘：人工跳过，不进入本次执行'
  }
  return notes[action] || '实战前逐票复盘：记录人工判断'
}

function buildPretradeReviewPayload(row, action) {
  const confirmationItems = pretradeConfirmationItems(row)
  const naturalDecision = naturalExecutionDecision(row)
  const naturalConsistency = naturalConsistencyReview(row)
  return {
    ticket_key: row?.ticket_key,
    code: row?.code,
    name: row?.name,
    entry_date: row?.entry_date,
    route: row?.route,
    natural_action: row?.natural_action,
    natural_reason: row?.notes,
    review_action: action,
    review_confidence: action === 'manual_approved' ? 'high' : action === 'paper_watch' ? 'medium' : 'low',
    risk_acknowledged: action === 'manual_approved',
    confirmation_items: action === 'manual_approved' ? confirmationItems : [],
    checked_items: action === 'manual_approved' ? confirmationItems : [],
    execution_posture: naturalDecision.execution_posture,
    decision_level: naturalDecision.decision_level,
    consistency_grade: naturalConsistency.consistency_grade,
    consistency_score: naturalConsistency.consistency_score,
    operational_readiness_score: naturalConsistency.operational_readiness_score,
    required_confirmation: naturalDecision.required_confirmation,
    downgrade_rule: naturalDecision.downgrade_rule,
    review_decision_reason: reviewDecisionReason(action, row, naturalDecision, naturalConsistency),
    next_review_trigger: nextReviewTrigger(action, naturalDecision),
    review_evidence: {
      buy_point_logic: naturalConsistency.buy_point_logic,
      sell_point_logic: naturalConsistency.sell_point_logic,
      selection_logic: naturalConsistency.selection_logic,
      model_switch_logic: naturalConsistency.model_switch_logic,
      logic_deductions: naturalConsistency.deductions,
      readiness_gaps: naturalConsistency.readiness_gaps,
      source_deductions: naturalDecision.source_deductions
    },
    note: action === 'manual_approved' && confirmationItems.length
      ? `${reviewNotes(action)}；确认项：${confirmationItems.join('；')}`
      : reviewNotes(action)
  }
}

async function refreshReadinessAndLedger(reason = '') {
  const review = await runGen3StateAlphaRealtimeReadinessReview(reason ? { reason } : {})
  const [control, brokerSyncPreflightResult, brokerSyncConfirmationPacketResult, brokerSyncOutcomeResult, brokerPostSyncAcceptanceResult, liveActionConsoleResult, launchPacketResult, liveLearningLedger, liveLaunchDecisionResult, liveBlockerEvidenceBoardResult, liveLaunchReadinessAuditResult, strategyTuningAxisBoardResult, strategyTuningReviewQueueResult, strategyTuningReplaySuggestionsResult, strategyTuningReplaySessionResult, strategyTuningCurrentStepCompletionPacketResult, strategyTuningCompletionAuditResult, liveReplayCockpitResult, historicalDecisionReplayAuditResult, historicalDecisionReplayResult, ledger, paperWatchLedger, dailyReviewChecklistLedger, launchDayPlaybookLedger, launchReviewSnapshots] = await Promise.all([
    getGen3StateAlphaPremarketControl({ refresh: false }),
    getGen3StateAlphaBrokerHoldingsSyncPreflight(),
    getGen3StateAlphaBrokerHoldingsSyncConfirmationPacket(),
    getGen3StateAlphaBrokerHoldingsSyncOutcome(),
    getGen3StateAlphaBrokerPostSyncAcceptance(),
    getGen3StateAlphaLiveActionConsole(),
    runGen3StateAlphaLiveLaunchPacket({ source: 'g3_risk_page_after_readiness' }),
    getGen3StateAlphaLiveLearningLedger(),
    getGen3StateAlphaLiveLaunchDecision(),
    getGen3StateAlphaLiveBlockerEvidenceBoard(),
    getGen3StateAlphaLiveLaunchReadinessAudit(),
    getGen3StateAlphaStrategyTuningAxisBoard(),
    getGen3StateAlphaStrategyTuningReviewQueue(),
    getGen3StateAlphaStrategyTuningReplaySuggestions(),
    getGen3StateAlphaStrategyTuningReplaySession(),
    getGen3StateAlphaStrategyTuningCurrentStepCompletionPacket(),
    getGen3StateAlphaStrategyTuningCompletionAudit(),
    getGen3StateAlphaLiveReplayCockpit(),
    getGen3StateAlphaHistoricalDecisionReplayAudit(),
    getGen3StateAlphaHistoricalDecisionReplayTasks({ limit: 80 }),
    getGen3StateAlphaPretradeTicketReviews(),
    getGen3StateAlphaPaperWatchReviews(),
    getGen3StateAlphaDailyReviewChecklistReviews(),
    getGen3StateAlphaLaunchDayPlaybookReviews(),
    getGen3StateAlphaLiveLaunchReviewSnapshots({ limit: 100 })
  ])
  readinessReview.value = review || {}
  premarketControl.value = control || {}
  brokerSyncPreflightPayload.value = brokerSyncPreflightResult || {}
  brokerSyncConfirmationPacketPayload.value = brokerSyncConfirmationPacketResult || {}
  brokerSyncOutcomePayload.value = brokerSyncOutcomeResult || {}
  brokerPostSyncAcceptancePayload.value = brokerPostSyncAcceptanceResult || {}
  liveActionConsolePayload.value = liveActionConsoleResult || {}
  liveLaunchPacketPayload.value = launchPacketResult || liveLaunchPacketPayload.value || {}
  liveLearningLedgerPayload.value = liveLearningLedger || {}
  liveLaunchDecisionPayload.value = liveLaunchDecisionResult || {}
  liveBlockerEvidenceBoardPayload.value = liveBlockerEvidenceBoardResult || {}
  liveLaunchReadinessAuditPayload.value = liveLaunchReadinessAuditResult || {}
  strategyTuningAxisBoardPayload.value = strategyTuningAxisBoardResult || {}
  strategyTuningReviewQueuePayload.value = strategyTuningReviewQueueResult || {}
  strategyTuningReplaySuggestionsPayload.value = strategyTuningReplaySuggestionsResult || {}
  strategyTuningReplaySessionPayload.value = strategyTuningReplaySessionResult || {}
  strategyTuningCurrentStepCompletionPacketPayload.value = strategyTuningCurrentStepCompletionPacketResult || {}
  strategyTuningCompletionAuditPayload.value = strategyTuningCompletionAuditResult || {}
  liveReplayCockpitPayload.value = liveReplayCockpitResult || {}
  historicalDecisionReplayAuditPayload.value = historicalDecisionReplayAuditResult || {}
  historicalDecisionReplayPayload.value = historicalDecisionReplayResult || {}
  pretradeReviewLedger.value = Array.isArray(ledger?.reviews) ? ledger.reviews : []
  paperWatchReviewLedger.value = Array.isArray(paperWatchLedger?.reviews) ? paperWatchLedger.reviews : []
  dailyReviewChecklistReviewLedger.value = Array.isArray(dailyReviewChecklistLedger?.reviews) ? dailyReviewChecklistLedger.reviews : []
  launchDayPlaybookReviewLedger.value = Array.isArray(launchDayPlaybookLedger?.reviews) ? launchDayPlaybookLedger.reviews : []
  liveLaunchReviewSnapshotLedger.value = Array.isArray(launchReviewSnapshots?.snapshots) ? launchReviewSnapshots.snapshots : []
  return review
}

async function refreshCurrentAndLedgers(reason = '') {
  const [current, review, control, brokerSyncPreflightResult, brokerSyncConfirmationPacketResult, brokerSyncOutcomeResult, brokerPostSyncAcceptanceResult, liveActionConsoleResult, launchPacketResult, liveLearningLedger, liveLaunchDecisionResult, liveBlockerEvidenceBoardResult, liveLaunchReadinessAuditResult, strategyTuningAxisBoardResult, strategyTuningReviewQueueResult, strategyTuningReplaySuggestionsResult, strategyTuningReplaySessionResult, strategyTuningCurrentStepCompletionPacketResult, strategyTuningCompletionAuditResult, liveReplayCockpitResult, historicalDecisionReplayAuditResult, historicalDecisionReplayResult, ledger, paperWatchLedger, dailyReviewChecklistLedger, launchDayPlaybookLedger, launchReviewSnapshots] = await Promise.all([
    getGen3StateAlphaCurrent({ limit: 100, reason }),
    getGen3StateAlphaRealtimeReadinessReview(),
    getGen3StateAlphaPremarketControl({ refresh: false }),
    getGen3StateAlphaBrokerHoldingsSyncPreflight(),
    getGen3StateAlphaBrokerHoldingsSyncConfirmationPacket(),
    getGen3StateAlphaBrokerHoldingsSyncOutcome(),
    getGen3StateAlphaBrokerPostSyncAcceptance(),
    getGen3StateAlphaLiveActionConsole(),
    getGen3StateAlphaLiveLaunchPacket(),
    getGen3StateAlphaLiveLearningLedger(),
    getGen3StateAlphaLiveLaunchDecision(),
    getGen3StateAlphaLiveBlockerEvidenceBoard(),
    getGen3StateAlphaLiveLaunchReadinessAudit(),
    getGen3StateAlphaStrategyTuningAxisBoard(),
    getGen3StateAlphaStrategyTuningReviewQueue(),
    getGen3StateAlphaStrategyTuningReplaySuggestions(),
    getGen3StateAlphaStrategyTuningReplaySession(),
    getGen3StateAlphaStrategyTuningCurrentStepCompletionPacket(),
    getGen3StateAlphaStrategyTuningCompletionAudit(),
    getGen3StateAlphaLiveReplayCockpit(),
    getGen3StateAlphaHistoricalDecisionReplayAudit(),
    getGen3StateAlphaHistoricalDecisionReplayTasks({ limit: 80 }),
    getGen3StateAlphaPretradeTicketReviews(),
    getGen3StateAlphaPaperWatchReviews(),
    getGen3StateAlphaDailyReviewChecklistReviews(),
    getGen3StateAlphaLaunchDayPlaybookReviews(),
    getGen3StateAlphaLiveLaunchReviewSnapshots({ limit: 100 })
  ])
  currentPayload.value = current || {}
  readinessReview.value = review || readinessReview.value || {}
  premarketControl.value = control || premarketControl.value || {}
  brokerSyncPreflightPayload.value = brokerSyncPreflightResult || brokerSyncPreflightPayload.value || {}
  brokerSyncConfirmationPacketPayload.value = brokerSyncConfirmationPacketResult || brokerSyncConfirmationPacketPayload.value || {}
  brokerSyncOutcomePayload.value = brokerSyncOutcomeResult || brokerSyncOutcomePayload.value || {}
  brokerPostSyncAcceptancePayload.value = brokerPostSyncAcceptanceResult || brokerPostSyncAcceptancePayload.value || {}
  liveActionConsolePayload.value = liveActionConsoleResult || liveActionConsolePayload.value || {}
  liveLaunchPacketPayload.value = launchPacketResult || liveLaunchPacketPayload.value || {}
  liveLearningLedgerPayload.value = liveLearningLedger || liveLearningLedgerPayload.value || {}
  liveLaunchDecisionPayload.value = liveLaunchDecisionResult || liveLaunchDecisionPayload.value || {}
  liveBlockerEvidenceBoardPayload.value = liveBlockerEvidenceBoardResult || liveBlockerEvidenceBoardPayload.value || {}
  liveLaunchReadinessAuditPayload.value = liveLaunchReadinessAuditResult || liveLaunchReadinessAuditPayload.value || {}
  strategyTuningAxisBoardPayload.value = strategyTuningAxisBoardResult || strategyTuningAxisBoardPayload.value || {}
  strategyTuningReviewQueuePayload.value = strategyTuningReviewQueueResult || strategyTuningReviewQueuePayload.value || {}
  strategyTuningReplaySuggestionsPayload.value = strategyTuningReplaySuggestionsResult || strategyTuningReplaySuggestionsPayload.value || {}
  strategyTuningReplaySessionPayload.value = strategyTuningReplaySessionResult || strategyTuningReplaySessionPayload.value || {}
  strategyTuningCurrentStepCompletionPacketPayload.value = strategyTuningCurrentStepCompletionPacketResult || strategyTuningCurrentStepCompletionPacketPayload.value || {}
  strategyTuningCompletionAuditPayload.value = strategyTuningCompletionAuditResult || strategyTuningCompletionAuditPayload.value || {}
  liveReplayCockpitPayload.value = liveReplayCockpitResult || liveReplayCockpitPayload.value || {}
  historicalDecisionReplayAuditPayload.value = historicalDecisionReplayAuditResult || historicalDecisionReplayAuditPayload.value || {}
  historicalDecisionReplayPayload.value = historicalDecisionReplayResult || historicalDecisionReplayPayload.value || {}
  pretradeReviewLedger.value = Array.isArray(ledger?.reviews) ? ledger.reviews : []
  paperWatchReviewLedger.value = Array.isArray(paperWatchLedger?.reviews) ? paperWatchLedger.reviews : []
  dailyReviewChecklistReviewLedger.value = Array.isArray(dailyReviewChecklistLedger?.reviews) ? dailyReviewChecklistLedger.reviews : []
  launchDayPlaybookReviewLedger.value = Array.isArray(launchDayPlaybookLedger?.reviews) ? launchDayPlaybookLedger.reviews : []
  liveLaunchReviewSnapshotLedger.value = Array.isArray(launchReviewSnapshots?.snapshots) ? launchReviewSnapshots.snapshots : []
  return review
}

async function load() {
  loading.value = true
  try {
    const [contractPayload, current, review, control, brokerSyncPreflightResult, brokerSyncConfirmationPacketResult, brokerSyncOutcomeResult, brokerPostSyncAcceptanceResult, liveActionConsoleResult, launchPacketResult, liveLearningLedger, liveLaunchDecisionResult, liveBlockerEvidenceBoardResult, liveLaunchReadinessAuditResult, strategyTuningAxisBoardResult, strategyTuningReviewQueueResult, strategyTuningReplaySuggestionsResult, strategyTuningReplaySessionResult, strategyTuningCurrentStepCompletionPacketResult, strategyTuningCompletionAuditResult, liveReplayCockpitResult, historicalDecisionReplayAuditResult, historicalDecisionReplayResult, ledger, paperWatchLedger, dailyReviewChecklistLedger, launchDayPlaybookLedger, launchReviewSnapshots] = await Promise.all([
      getGen3StateAlphaContract(),
      getGen3StateAlphaCurrent({ limit: 100 }),
      getGen3StateAlphaRealtimeReadinessReview(),
      getGen3StateAlphaPremarketControl({ refresh: false }),
      getGen3StateAlphaBrokerHoldingsSyncPreflight(),
      getGen3StateAlphaBrokerHoldingsSyncConfirmationPacket(),
      getGen3StateAlphaBrokerHoldingsSyncOutcome(),
      getGen3StateAlphaBrokerPostSyncAcceptance(),
      getGen3StateAlphaLiveActionConsole(),
      getGen3StateAlphaLiveLaunchPacket(),
      getGen3StateAlphaLiveLearningLedger(),
      getGen3StateAlphaLiveLaunchDecision(),
      getGen3StateAlphaLiveBlockerEvidenceBoard(),
      getGen3StateAlphaLiveLaunchReadinessAudit(),
      getGen3StateAlphaStrategyTuningAxisBoard(),
      getGen3StateAlphaStrategyTuningReviewQueue(),
      getGen3StateAlphaStrategyTuningReplaySuggestions(),
      getGen3StateAlphaStrategyTuningReplaySession(),
      getGen3StateAlphaStrategyTuningCurrentStepCompletionPacket(),
      getGen3StateAlphaStrategyTuningCompletionAudit(),
      getGen3StateAlphaLiveReplayCockpit(),
      getGen3StateAlphaHistoricalDecisionReplayAudit(),
      getGen3StateAlphaHistoricalDecisionReplayTasks({ limit: 80 }),
      getGen3StateAlphaPretradeTicketReviews(),
      getGen3StateAlphaPaperWatchReviews(),
      getGen3StateAlphaDailyReviewChecklistReviews(),
      getGen3StateAlphaLaunchDayPlaybookReviews(),
      getGen3StateAlphaLiveLaunchReviewSnapshots({ limit: 100 })
    ])
    payload.value = contractPayload
    currentPayload.value = current
    readinessReview.value = review || {}
    premarketControl.value = control || {}
    brokerSyncPreflightPayload.value = brokerSyncPreflightResult || {}
    brokerSyncConfirmationPacketPayload.value = brokerSyncConfirmationPacketResult || {}
    brokerSyncOutcomePayload.value = brokerSyncOutcomeResult || {}
    brokerPostSyncAcceptancePayload.value = brokerPostSyncAcceptanceResult || {}
    liveActionConsolePayload.value = liveActionConsoleResult || {}
    liveLaunchPacketPayload.value = launchPacketResult || {}
    liveLearningLedgerPayload.value = liveLearningLedger || {}
    liveLaunchDecisionPayload.value = liveLaunchDecisionResult || {}
    liveBlockerEvidenceBoardPayload.value = liveBlockerEvidenceBoardResult || {}
    liveLaunchReadinessAuditPayload.value = liveLaunchReadinessAuditResult || {}
    strategyTuningAxisBoardPayload.value = strategyTuningAxisBoardResult || {}
    strategyTuningReviewQueuePayload.value = strategyTuningReviewQueueResult || {}
    strategyTuningReplaySuggestionsPayload.value = strategyTuningReplaySuggestionsResult || {}
    strategyTuningReplaySessionPayload.value = strategyTuningReplaySessionResult || {}
    strategyTuningCurrentStepCompletionPacketPayload.value = strategyTuningCurrentStepCompletionPacketResult || {}
    strategyTuningCompletionAuditPayload.value = strategyTuningCompletionAuditResult || {}
    liveReplayCockpitPayload.value = liveReplayCockpitResult || {}
    historicalDecisionReplayAuditPayload.value = historicalDecisionReplayAuditResult || {}
    historicalDecisionReplayPayload.value = historicalDecisionReplayResult || {}
    pretradeReviewLedger.value = Array.isArray(ledger?.reviews) ? ledger.reviews : []
    paperWatchReviewLedger.value = Array.isArray(paperWatchLedger?.reviews) ? paperWatchLedger.reviews : []
    dailyReviewChecklistReviewLedger.value = Array.isArray(dailyReviewChecklistLedger?.reviews) ? dailyReviewChecklistLedger.reviews : []
    launchDayPlaybookReviewLedger.value = Array.isArray(launchDayPlaybookLedger?.reviews) ? launchDayPlaybookLedger.reviews : []
    liveLaunchReviewSnapshotLedger.value = Array.isArray(launchReviewSnapshots?.snapshots) ? launchReviewSnapshots.snapshots : []
  } catch (error) {
    ElMessage.warning(error?.message || '读取 G3 风控合同失败')
  } finally {
    loading.value = false
  }
}

async function refreshLiveLaunchPacket(rebuild = false) {
  liveLaunchPacketLoading.value = true
  try {
    const result = rebuild
      ? await runGen3StateAlphaLiveLaunchPacket({ source: 'g3_risk_page_manual_rebuild' })
      : await getGen3StateAlphaLiveLaunchPacket()
    liveLaunchPacketPayload.value = result || {}
    liveLearningLedgerPayload.value = await getGen3StateAlphaLiveLearningLedger()
    liveLaunchDecisionPayload.value = await getGen3StateAlphaLiveLaunchDecision()
    liveBlockerEvidenceBoardPayload.value = await getGen3StateAlphaLiveBlockerEvidenceBoard()
    liveLaunchReadinessAuditPayload.value = await getGen3StateAlphaLiveLaunchReadinessAudit()
    ElMessage.success(rebuild ? '已重建 G3 实战启动包' : '已读取 G3 实战启动包')
    return result
  } catch (error) {
    ElMessage.warning(error?.message || '读取 G3 实战启动包失败')
    return null
  } finally {
    liveLaunchPacketLoading.value = false
  }
}

async function recordLiveLaunchReviewSnapshot() {
  let note = ''
  try {
    const response = await ElMessageBox.prompt(
      '记录当前 G3 实战启动复盘快照。请写一句本次快照的判断或处理节点。',
      '记录启动快照',
      {
        confirmButtonText: '记录快照',
        cancelButtonText: '取消',
        inputType: 'textarea',
        inputPlaceholder: '例如：Day0 盘前，仍被真实持仓价格刷新阻断；不买入，等待同步后重跑审计。',
        inputValidator: (value) => String(value || '').trim().length >= 4 || '请至少写一句快照备注'
      }
    )
    note = String(response?.value || '').trim()
  } catch {
    return
  }
  liveLaunchSnapshotLoading.value = true
  try {
    const result = await recordGen3StateAlphaLiveLaunchReviewSnapshot({
      source: 'g3_risk_page_manual_snapshot',
      phase: 'manual_checkpoint',
      note,
      refresh: false
    })
    if (!result?.ok) {
      ElMessage.warning(result?.error || '启动复盘快照记录失败')
      return
    }
    if (result.snapshot) {
      upsertLiveLaunchSnapshot(result.snapshot)
    }
    ElMessage.success('已记录启动复盘快照')
  } catch (error) {
    ElMessage.warning(error?.message || '启动复盘快照记录失败')
  } finally {
    liveLaunchSnapshotLoading.value = false
  }
}

async function syncBrokerHoldingsAndReview() {
  try {
    await ElMessageBox.confirm(
      '确认手动同步同花顺真实持仓并重跑 G3 实战前审计？这会刷新真实账户持仓/价格证据，但不会触发下单。',
      '同步真实持仓',
      {
        confirmButtonText: '同步并复审',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )
  } catch {
    return
  }
  brokerSyncLoading.value = true
  try {
    const syncResult = await syncGen3StateAlphaBrokerHoldingsFromThsAndReview({ source: 'g3_pretrade_readiness_review' })
    if (syncResult?.live_launch_review_snapshot) {
      upsertLiveLaunchSnapshot(syncResult.live_launch_review_snapshot)
    }
    if (!syncResult?.ok) {
      ElMessage.warning(syncResult?.message || syncResult?.sync?.message || syncResult?.error || '同步同花顺真实持仓并复审失败')
      await refreshCurrentAndLedgers('broker_sync_attempted')
      return
    }
    const [current, review, control, brokerSyncPreflightResult, brokerSyncConfirmationPacketResult, brokerSyncOutcomeResult, brokerPostSyncAcceptanceResult, liveActionConsoleResult, launchPacketResult, liveLearningLedger, liveLaunchDecisionResult, liveBlockerEvidenceBoardResult, liveLaunchReadinessAuditResult, strategyTuningAxisBoardResult, strategyTuningReviewQueueResult, strategyTuningReplaySuggestionsResult, strategyTuningReplaySessionResult, strategyTuningCurrentStepCompletionPacketResult, strategyTuningCompletionAuditResult, liveReplayCockpitResult, historicalDecisionReplayAuditResult, historicalDecisionReplayResult, ledger, paperWatchLedger, dailyReviewChecklistLedger, launchDayPlaybookLedger, launchReviewSnapshots] = await Promise.all([
      getGen3StateAlphaCurrent({ limit: 100 }),
      getGen3StateAlphaRealtimeReadinessReview(),
      getGen3StateAlphaPremarketControl({ refresh: false }),
      getGen3StateAlphaBrokerHoldingsSyncPreflight(),
      getGen3StateAlphaBrokerHoldingsSyncConfirmationPacket(),
      getGen3StateAlphaBrokerHoldingsSyncOutcome(),
      getGen3StateAlphaBrokerPostSyncAcceptance(),
      getGen3StateAlphaLiveActionConsole(),
      runGen3StateAlphaLiveLaunchPacket({ source: 'g3_risk_page_after_broker_sync' }),
      getGen3StateAlphaLiveLearningLedger(),
      getGen3StateAlphaLiveLaunchDecision(),
      getGen3StateAlphaLiveBlockerEvidenceBoard(),
      getGen3StateAlphaLiveLaunchReadinessAudit(),
      getGen3StateAlphaStrategyTuningAxisBoard(),
      getGen3StateAlphaStrategyTuningReviewQueue(),
      getGen3StateAlphaStrategyTuningReplaySuggestions(),
      getGen3StateAlphaStrategyTuningReplaySession(),
      getGen3StateAlphaStrategyTuningCurrentStepCompletionPacket(),
      getGen3StateAlphaStrategyTuningCompletionAudit(),
      getGen3StateAlphaLiveReplayCockpit(),
      getGen3StateAlphaHistoricalDecisionReplayAudit(),
      getGen3StateAlphaHistoricalDecisionReplayTasks({ limit: 80 }),
      getGen3StateAlphaPretradeTicketReviews(),
      getGen3StateAlphaPaperWatchReviews(),
      getGen3StateAlphaDailyReviewChecklistReviews(),
      getGen3StateAlphaLaunchDayPlaybookReviews(),
      getGen3StateAlphaLiveLaunchReviewSnapshots({ limit: 100 })
    ])
    currentPayload.value = current || {}
    readinessReview.value = review || {}
    premarketControl.value = control || {}
    brokerSyncPreflightPayload.value = brokerSyncPreflightResult || brokerSyncPreflightPayload.value || {}
    brokerSyncConfirmationPacketPayload.value = brokerSyncConfirmationPacketResult || brokerSyncConfirmationPacketPayload.value || {}
    brokerSyncOutcomePayload.value = brokerSyncOutcomeResult || brokerSyncOutcomePayload.value || {}
    brokerPostSyncAcceptancePayload.value = brokerPostSyncAcceptanceResult || brokerPostSyncAcceptancePayload.value || {}
    liveActionConsolePayload.value = liveActionConsoleResult || liveActionConsolePayload.value || {}
    liveLaunchPacketPayload.value = launchPacketResult || liveLaunchPacketPayload.value || {}
    liveLearningLedgerPayload.value = liveLearningLedger || liveLearningLedgerPayload.value || {}
    liveLaunchDecisionPayload.value = liveLaunchDecisionResult || liveLaunchDecisionPayload.value || {}
    liveBlockerEvidenceBoardPayload.value = liveBlockerEvidenceBoardResult || liveBlockerEvidenceBoardPayload.value || {}
    liveLaunchReadinessAuditPayload.value = liveLaunchReadinessAuditResult || liveLaunchReadinessAuditPayload.value || {}
    strategyTuningAxisBoardPayload.value = strategyTuningAxisBoardResult || strategyTuningAxisBoardPayload.value || {}
    strategyTuningReviewQueuePayload.value = strategyTuningReviewQueueResult || strategyTuningReviewQueuePayload.value || {}
    strategyTuningReplaySuggestionsPayload.value = strategyTuningReplaySuggestionsResult || strategyTuningReplaySuggestionsPayload.value || {}
    strategyTuningReplaySessionPayload.value = strategyTuningReplaySessionResult || strategyTuningReplaySessionPayload.value || {}
    strategyTuningCurrentStepCompletionPacketPayload.value = strategyTuningCurrentStepCompletionPacketResult || strategyTuningCurrentStepCompletionPacketPayload.value || {}
    strategyTuningCompletionAuditPayload.value = strategyTuningCompletionAuditResult || strategyTuningCompletionAuditPayload.value || {}
    liveReplayCockpitPayload.value = liveReplayCockpitResult || liveReplayCockpitPayload.value || {}
    historicalDecisionReplayAuditPayload.value = historicalDecisionReplayAuditResult || historicalDecisionReplayAuditPayload.value || {}
    historicalDecisionReplayPayload.value = historicalDecisionReplayResult || historicalDecisionReplayPayload.value || {}
    pretradeReviewLedger.value = Array.isArray(ledger?.reviews) ? ledger.reviews : []
    paperWatchReviewLedger.value = Array.isArray(paperWatchLedger?.reviews) ? paperWatchLedger.reviews : []
    dailyReviewChecklistReviewLedger.value = Array.isArray(dailyReviewChecklistLedger?.reviews) ? dailyReviewChecklistLedger.reviews : []
    launchDayPlaybookReviewLedger.value = Array.isArray(launchDayPlaybookLedger?.reviews) ? launchDayPlaybookLedger.reviews : []
    liveLaunchReviewSnapshotLedger.value = Array.isArray(launchReviewSnapshots?.snapshots) ? launchReviewSnapshots.snapshots : []
    const admission = syncResult?.admission || {}
    ElMessage.success(`已同步真实持仓并复审：阻断数 ${admission.live_admission_blocking_command_count ?? '--'}，下一步 ${admission.live_premarket_next_action || '--'}`)
  } catch (error) {
    ElMessage.warning(error?.message || '同步真实持仓并复审失败')
  } finally {
    brokerSyncLoading.value = false
  }
}

async function refreshPremarketControl(refresh = false) {
  premarketControlLoading.value = true
  try {
    const control = await getGen3StateAlphaPremarketControl({ refresh })
    premarketControl.value = control || {}
    liveLaunchDecisionPayload.value = await getGen3StateAlphaLiveLaunchDecision()
    liveBlockerEvidenceBoardPayload.value = await getGen3StateAlphaLiveBlockerEvidenceBoard()
    liveLaunchReadinessAuditPayload.value = await getGen3StateAlphaLiveLaunchReadinessAudit()
    strategyTuningAxisBoardPayload.value = await getGen3StateAlphaStrategyTuningAxisBoard()
    strategyTuningReviewQueuePayload.value = await getGen3StateAlphaStrategyTuningReviewQueue()
    strategyTuningReplaySuggestionsPayload.value = await getGen3StateAlphaStrategyTuningReplaySuggestions()
    strategyTuningReplaySessionPayload.value = await getGen3StateAlphaStrategyTuningReplaySession()
    strategyTuningCurrentStepCompletionPacketPayload.value = await getGen3StateAlphaStrategyTuningCurrentStepCompletionPacket()
    strategyTuningCompletionAuditPayload.value = await getGen3StateAlphaStrategyTuningCompletionAudit()
    liveReplayCockpitPayload.value = await getGen3StateAlphaLiveReplayCockpit()
    brokerSyncPreflightPayload.value = await getGen3StateAlphaBrokerHoldingsSyncPreflight()
    brokerSyncConfirmationPacketPayload.value = await getGen3StateAlphaBrokerHoldingsSyncConfirmationPacket()
    brokerSyncOutcomePayload.value = await getGen3StateAlphaBrokerHoldingsSyncOutcome()
    brokerPostSyncAcceptancePayload.value = await getGen3StateAlphaBrokerPostSyncAcceptance()
    liveActionConsolePayload.value = await getGen3StateAlphaLiveActionConsole()
    if (refresh) {
      if (control?.summary) readinessReview.value = { ...(readinessReview.value || {}), summary: control.summary }
      ElMessage.success('已刷新盘前指挥卡')
    }
    return control
  } catch (error) {
    ElMessage.warning(error?.message || '读取盘前指挥卡失败')
    return null
  } finally {
    premarketControlLoading.value = false
  }
}

async function refreshBrokerSyncPreflight() {
  brokerSyncPreflightLoading.value = true
  try {
    const result = await getGen3StateAlphaBrokerHoldingsSyncPreflight()
    brokerSyncPreflightPayload.value = result || {}
    brokerSyncConfirmationPacketPayload.value = await getGen3StateAlphaBrokerHoldingsSyncConfirmationPacket()
    brokerSyncOutcomePayload.value = await getGen3StateAlphaBrokerHoldingsSyncOutcome()
    brokerPostSyncAcceptancePayload.value = await getGen3StateAlphaBrokerPostSyncAcceptance()
    liveActionConsolePayload.value = await getGen3StateAlphaLiveActionConsole()
    liveLaunchReadinessAuditPayload.value = await getGen3StateAlphaLiveLaunchReadinessAudit()
    strategyTuningAxisBoardPayload.value = await getGen3StateAlphaStrategyTuningAxisBoard()
    strategyTuningReviewQueuePayload.value = await getGen3StateAlphaStrategyTuningReviewQueue()
    strategyTuningReplaySuggestionsPayload.value = await getGen3StateAlphaStrategyTuningReplaySuggestions()
    strategyTuningReplaySessionPayload.value = await getGen3StateAlphaStrategyTuningReplaySession()
    strategyTuningCurrentStepCompletionPacketPayload.value = await getGen3StateAlphaStrategyTuningCurrentStepCompletionPacket()
    strategyTuningCompletionAuditPayload.value = await getGen3StateAlphaStrategyTuningCompletionAudit()
    liveReplayCockpitPayload.value = await getGen3StateAlphaLiveReplayCockpit()
    ElMessage.success('已刷新同步预检')
    return result
  } catch (error) {
    ElMessage.warning(error?.message || '同步预检失败')
    return null
  } finally {
    brokerSyncPreflightLoading.value = false
  }
}

async function executePremarketNextAction() {
  const command = premarketCommand.value || {}
  if (!command.action_group) {
    ElMessage.info('当前没有可执行的盘前动作')
    return
  }
  try {
    await ElMessageBox.confirm(
      `确认执行当前盘前动作：${command.action_label || command.next_action || command.action_group}？系统会再次校验当前动作，且不会开启自动下单。`,
      '执行盘前下一步',
      {
        confirmButtonText: command.requires_confirmation ? '确认执行' : '执行',
        cancelButtonText: '取消',
        type: command.requires_confirmation ? 'warning' : 'info'
      }
    )
  } catch {
    return
  }
  premarketNextActionLoading.value = true
  try {
    const result = await executeGen3StateAlphaPremarketNextAction({
      source: 'g3_state_alpha_risk_page',
      action_group: command.action_group,
      confirm: Boolean(command.requires_confirmation)
    })
    if (result?.live_launch_review_snapshot) {
      upsertLiveLaunchSnapshot(result.live_launch_review_snapshot)
    }
    if (!result?.ok) {
      ElMessage.warning(result?.message || result?.error || '盘前动作未完成')
      await refreshCurrentAndLedgers('premarket_next_action_attempted')
      return
    }
    if (result.review) {
      readinessReview.value = result.review
    }
    if (result.command) {
      premarketControl.value = { ...(premarketControl.value || {}), command: result.command }
    }
    await refreshCurrentAndLedgers('premarket_next_action_executed')
    const admission = result.admission || {}
    ElMessage.success(`盘前动作已复审：阻断数 ${admission.live_admission_blocking_command_count ?? '--'}，下一步 ${admission.live_premarket_next_action || result.command?.next_action || '--'}`)
  } catch (error) {
    ElMessage.warning(error?.message || '执行盘前下一步失败')
  } finally {
    premarketNextActionLoading.value = false
  }
}

async function handleFormalReviewBoardAction(row) {
  const key = formalActionRowKey(row)
  const label = formalActionButtonLabel(row)
  formalActionSavingKey.value = key
  try {
    if (label === '同步持仓并复审') {
      await syncBrokerHoldingsAndReview()
      return
    }
    if (label === '重跑审计') {
      await runReadinessReview()
      return
    }
    ElMessage.info('请在对应来源表格中处理该正式动作')
  } finally {
    formalActionSavingKey.value = ''
  }
}

async function handleBlockerResolutionAction(row) {
  const type = String(row?.resolution_type || '')
  if (type === 'broker_holding_price_refresh') {
    await syncBrokerHoldingsAndReview()
    return
  }
  if (type === 'rerun_readiness_audit') {
    await runReadinessReview()
    return
  }
  if (type === 'manual_formal_action_review') {
    await saveFormalActionReview(taskToReviewRow(row), 'manual_done')
    return
  }
  ElMessage.info('该阻断需要在对应复盘入口补充证据')
}

async function handlePremarketSequenceAction(row) {
  const group = String(row?.action_group || '')
  if (group === 'sync_broker_holding_price') {
    await syncBrokerHoldingsAndReview()
    return
  }
  if (group === 'rerun_readiness_audit') {
    await runReadinessReview()
    return
  }
  if (group === 'manual_formal_action_review') {
    await saveFormalActionReview(taskToReviewRow(row), 'manual_done')
    return
  }
  ElMessage.info('请按顺序完成前置事实后再记录该步骤')
}

async function handleDay1JournalPrimaryAction(row) {
  const actionText = `${row?.action || ''} ${row?.next_action || ''} ${row?.object || ''}`
  if (actionText.includes('同步') || actionText.includes('持仓') || actionText.includes('价格')) {
    await syncBrokerHoldingsAndReview()
    return
  }
  if (actionText.includes('重跑') || actionText.includes('审计')) {
    await runReadinessReview()
    return
  }
  if (isDay1JournalNoTradeRow(row)) {
    await saveNoTradeDayReview(taskToReviewRow(row), 'natural_no_trade')
    return
  }
  if (row?.source_table === 'live_manual_launch_acceptance') {
    ElMessage.info('验收项只显示准入状态；请先处理对应盘前阻断或逐项复盘证据。')
    return
  }
  ElMessage.info('请在同一行的证据处理按钮中记录复盘结论。')
}

function formalActionReviewLabel(result) {
  const labels = {
    manual_done: '人工已处理',
    system_triggered: '系统动作已触发',
    blocked: '处理卡住',
    continue_watch: '继续观察'
  }
  return labels[result] || result || '--'
}

function formalActionAutoText(result, row = {}) {
  if (result === 'blocked') {
    return {
      issueArea: 'execution_process',
      naturalDecision: '正式动作卡住，不能把流程缺口当成策略买卖点失败',
      suggestion: '先修复持仓刷新、价格确认、审计重跑或人工复盘入口，再讨论交易模型是否需要调参'
    }
  }
  if (result === 'continue_watch') {
    return {
      issueArea: 'operation_evidence',
      naturalDecision: '证据不足，暂不放大为策略问题',
      suggestion: '继续补齐动作证据，等底层审计事实变化后再判断是否清理完成'
    }
  }
  return {
    issueArea: 'operation_evidence',
    naturalDecision: row?.natural_decision || '正式动作已有处理证据，但是否放行仍以重跑审计结果为准',
    suggestion: '保存动作证据后重跑实战前审计，确认 gate/action 是否真实清理'
  }
}

async function saveFormalActionReview(row, result) {
  const key = formalActionRowKey(row)
  const label = formalActionReviewLabel(result)
  let note = ''
  try {
    const response = await ElMessageBox.prompt(
      `记录正式动作：${row?.object || '--'}，结果为“${label}”。请写一句处理证据或卡住原因。`,
      '正式动作复核',
      {
        confirmButtonText: '保存复核',
        cancelButtonText: '取消',
        inputType: 'textarea',
        inputPlaceholder: '例如：已手动刷新真实持仓并重跑审计，或：同花顺窗口不可用，暂不能确认退出价。',
        inputValidator: (value) => String(value || '').trim().length >= 4 || '请至少写一句复核依据'
      }
    )
    note = String(response?.value || '').trim()
  } catch {
    return
  }
  const autoText = formalActionAutoText(result, row)
  formalActionSavingKey.value = key
  try {
    const payload = {
      review_key: row?.review_key || key,
      object: row?.object,
      action: row?.required_action,
      source: row?.source,
      review_result: result,
      issue_area: autoText.issueArea,
      evidence: row?.evidence,
      natural_decision: autoText.naturalDecision,
      optimization_suggestion: autoText.suggestion,
      review_note: note
    }
    const saved = await saveGen3StateAlphaFormalActionReviewAndRun(payload)
    if (!saved?.ok) {
      ElMessage.warning(saved?.error || '正式动作复核保存失败')
      return
    }
    if (saved.review) {
      readinessReview.value = saved.review
    }
    ElMessage.success('已保存正式动作复核并完成复审')
    await refreshCurrentAndLedgers('formal_action_review_saved')
  } catch (error) {
    ElMessage.warning(error?.message || '正式动作复核保存失败')
  } finally {
    formalActionSavingKey.value = ''
  }
}

function strategyTuningReviewResultLabel(result) {
  const labels = {
    validated: 'validated',
    issue_found: 'issue found',
    continue_watch: 'continue watch',
    data_gap: 'data gap',
    evidence_pending: 'evidence pending',
    defer_contract_review: 'defer contract review'
  }
  return labels[result] || result || '--'
}

async function saveStrategyTuningTaskReview(row, result) {
  const key = strategyTuningTaskRowKey(row)
  const label = strategyTuningReviewResultLabel(result)
  let note = ''
  try {
    const response = await ElMessageBox.prompt(
      `Record tuning review: ${row?.axis_label || row?.axis || '--'} / ${row?.object || '--'} / ${label}. This only records evidence and never enables trading.`,
      'strategy tuning review',
      {
        confirmButtonText: 'save review',
        cancelButtonText: 'cancel',
        inputType: 'textarea',
        inputPlaceholder: 'Evidence, decision, or reason to keep watching',
        inputValidator: (value) => String(value || '').trim().length >= 4 || 'please write at least one sentence'
      }
    )
    note = String(response?.value || '').trim()
  } catch {
    return
  }
  strategyTuningTaskSavingKey.value = key
  try {
    const saved = await saveGen3StateAlphaStrategyTuningTaskReview({
      task_key: key,
      axis: row?.axis,
      axis_label: row?.axis_label,
      origin: row?.origin,
      object: row?.object,
      problem_signal: row?.problem_signal,
      source_status: row?.source_status,
      review_result: result,
      evidence: row?.evidence,
      completion_evidence: row?.completion_evidence,
      review_note: note,
      hidden_risk: row?.problem_signal,
      decision: label,
      optimization_suggestion: row?.suggested_learning,
      next_action: row?.next_action,
      optimization_boundary: row?.optimization_boundary,
      can_execute_trade: false,
      can_change_strategy_contract: false,
      profit_only_optimization_allowed: false
    })
    if (!saved?.ok) {
      ElMessage.warning(saved?.error || 'strategy tuning review save failed')
      return
    }
    const [axisBoard, queue, suggestions, session, completionPacket, completionAudit, cockpit, historicalReplayAudit, historicalReplay] = await Promise.all([
      getGen3StateAlphaStrategyTuningAxisBoard(),
      getGen3StateAlphaStrategyTuningReviewQueue(),
      getGen3StateAlphaStrategyTuningReplaySuggestions(),
      getGen3StateAlphaStrategyTuningReplaySession(),
      getGen3StateAlphaStrategyTuningCurrentStepCompletionPacket(),
      getGen3StateAlphaStrategyTuningCompletionAudit(),
      getGen3StateAlphaLiveReplayCockpit(),
      getGen3StateAlphaHistoricalDecisionReplayAudit(),
      getGen3StateAlphaHistoricalDecisionReplayTasks({ limit: 80 })
    ])
    strategyTuningAxisBoardPayload.value = axisBoard || strategyTuningAxisBoardPayload.value || {}
    strategyTuningReviewQueuePayload.value = queue || strategyTuningReviewQueuePayload.value || {}
    strategyTuningReplaySuggestionsPayload.value = suggestions || strategyTuningReplaySuggestionsPayload.value || {}
    strategyTuningReplaySessionPayload.value = session || strategyTuningReplaySessionPayload.value || {}
    strategyTuningCurrentStepCompletionPacketPayload.value = completionPacket || strategyTuningCurrentStepCompletionPacketPayload.value || {}
    strategyTuningCompletionAuditPayload.value = completionAudit || strategyTuningCompletionAuditPayload.value || {}
    liveReplayCockpitPayload.value = cockpit || liveReplayCockpitPayload.value || {}
    historicalDecisionReplayAuditPayload.value = historicalReplayAudit || historicalDecisionReplayAuditPayload.value || {}
    historicalDecisionReplayPayload.value = historicalReplay || historicalDecisionReplayPayload.value || {}
    ElMessage.success('strategy tuning review saved')
  } catch (error) {
    ElMessage.warning(error?.message || 'strategy tuning review save failed')
  } finally {
    strategyTuningTaskSavingKey.value = ''
  }
}

async function recordBrokerPostSyncExecutionEvidence() {
  if (!truthy(brokerPostSyncAcceptance.value.can_record_execution_evidence)) {
    ElMessage.warning('post-sync acceptance is not ready')
    return
  }
  brokerPostSyncEvidenceLoading.value = true
  try {
    const saved = await recordGen3StateAlphaBrokerPostSyncExecutionEvidence({
      source: 'g3_risk_page_post_sync_acceptance',
      refresh: false
    })
    if (!saved?.ok) {
      const gapCount = Array.isArray(saved?.gaps) ? saved.gaps.length : 0
      ElMessage.warning(saved?.error || `post-sync acceptance still blocked: ${gapCount} gaps`)
      brokerPostSyncAcceptancePayload.value = await getGen3StateAlphaBrokerPostSyncAcceptance()
      liveActionConsolePayload.value = await getGen3StateAlphaLiveActionConsole()
      liveReplayCockpitPayload.value = await getGen3StateAlphaLiveReplayCockpit()
      return
    }
    const [acceptance, liveActionConsoleResult, axisBoard, queue, suggestions, session, completionPacket, completionAudit, cockpit] = await Promise.all([
      getGen3StateAlphaBrokerPostSyncAcceptance(),
      getGen3StateAlphaLiveActionConsole(),
      getGen3StateAlphaStrategyTuningAxisBoard(),
      getGen3StateAlphaStrategyTuningReviewQueue(),
      getGen3StateAlphaStrategyTuningReplaySuggestions(),
      getGen3StateAlphaStrategyTuningReplaySession(),
      getGen3StateAlphaStrategyTuningCurrentStepCompletionPacket(),
      getGen3StateAlphaStrategyTuningCompletionAudit(),
      getGen3StateAlphaLiveReplayCockpit()
    ])
    brokerPostSyncAcceptancePayload.value = acceptance || brokerPostSyncAcceptancePayload.value || {}
    liveActionConsolePayload.value = liveActionConsoleResult || liveActionConsolePayload.value || {}
    strategyTuningAxisBoardPayload.value = axisBoard || strategyTuningAxisBoardPayload.value || {}
    strategyTuningReviewQueuePayload.value = queue || strategyTuningReviewQueuePayload.value || {}
    strategyTuningReplaySuggestionsPayload.value = suggestions || strategyTuningReplaySuggestionsPayload.value || {}
    strategyTuningReplaySessionPayload.value = session || strategyTuningReplaySessionPayload.value || {}
    strategyTuningCurrentStepCompletionPacketPayload.value = completionPacket || strategyTuningCurrentStepCompletionPacketPayload.value || {}
    strategyTuningCompletionAuditPayload.value = completionAudit || strategyTuningCompletionAuditPayload.value || {}
    liveReplayCockpitPayload.value = cockpit || liveReplayCockpitPayload.value || {}
    ElMessage.success('execution evidence recorded')
  } catch (error) {
    ElMessage.warning(error?.message || 'record execution evidence failed')
  } finally {
    brokerPostSyncEvidenceLoading.value = false
  }
}

async function handleLiveActionConsoleStep(row = {}) {
  const key = String(row?.action_key || '').trim()
  if (!key) {
    ElMessage.info('no action key')
    return
  }
  liveActionConsoleLoading.value = true
  try {
    if (key === 'sync_broker_holding_price') {
      liveActionConsoleLoading.value = false
      await syncBrokerHoldingsAndReview()
      liveActionConsolePayload.value = await getGen3StateAlphaLiveActionConsole()
      liveReplayCockpitPayload.value = await getGen3StateAlphaLiveReplayCockpit()
      return
    }
    if (key === 'post_sync_acceptance') {
      brokerPostSyncAcceptancePayload.value = await getGen3StateAlphaBrokerPostSyncAcceptance({ refresh: false })
      liveActionConsolePayload.value = await getGen3StateAlphaLiveActionConsole()
      liveReplayCockpitPayload.value = await getGen3StateAlphaLiveReplayCockpit()
      ElMessage.success('post-sync acceptance refreshed')
      return
    }
    if (key === 'record_execution_evidence') {
      liveActionConsoleLoading.value = false
      await recordBrokerPostSyncExecutionEvidence()
      liveActionConsolePayload.value = await getGen3StateAlphaLiveActionConsole()
      liveReplayCockpitPayload.value = await getGen3StateAlphaLiveReplayCockpit()
      return
    }
    if (key === 'rerun_tuning_completion') {
      strategyTuningCompletionAuditPayload.value = await getGen3StateAlphaStrategyTuningCompletionAudit({ refresh: true })
      strategyTuningReplaySessionPayload.value = await getGen3StateAlphaStrategyTuningReplaySession()
      strategyTuningCurrentStepCompletionPacketPayload.value = await getGen3StateAlphaStrategyTuningCurrentStepCompletionPacket()
      liveActionConsolePayload.value = await getGen3StateAlphaLiveActionConsole()
      liveReplayCockpitPayload.value = await getGen3StateAlphaLiveReplayCockpit()
      ElMessage.success('tuning completion audit refreshed')
      return
    }
    if (key === 'manual_live_review_gate') {
      liveLaunchReadinessAuditPayload.value = await getGen3StateAlphaLiveLaunchReadinessAudit({ refresh: true })
      liveActionConsolePayload.value = await getGen3StateAlphaLiveActionConsole()
      liveReplayCockpitPayload.value = await getGen3StateAlphaLiveReplayCockpit()
      ElMessage.success('live readiness audit refreshed')
      return
    }
    ElMessage.info(`no handler for ${key}`)
  } catch (error) {
    ElMessage.warning(error?.message || 'live action step failed')
  } finally {
    liveActionConsoleLoading.value = false
  }
}

async function saveLiveActionConsoleStepIssue(row = {}) {
  const key = String(row?.action_key || '').trim()
  if (!key) {
    ElMessage.info('no action key')
    return
  }
  let note = ''
  try {
    const response = await ElMessageBox.prompt(
      `Record blocker for ${row?.action_label || key}. This only writes replay evidence and will not unlock trading.`,
      'live action issue',
      {
        confirmButtonText: 'record',
        cancelButtonText: 'cancel',
        inputType: 'textarea',
        inputValue: row?.next_action || row?.current_evidence || '',
        inputPlaceholder: 'What evidence shows this step is not naturally ready?'
      }
    )
    note = response?.value || ''
  } catch {
    return
  }
  liveActionConsoleReviewSavingKey.value = key
  try {
    const saved = await saveGen3StateAlphaLiveActionConsoleStepReview({
      action_key: key,
      review_result: 'issue_found',
      review_note: note,
      problem_signal: `${key} status=${row?.status || '--'}`,
      hidden_risk: row?.trade_impact,
      next_action: row?.next_action,
      optimization_suggestion: 'Use this step as execution-evidence replay material before tuning buy/sell/selection/model switching.',
      can_execute_trade: false,
      can_change_strategy_contract: false,
      profit_only_optimization_allowed: false
    })
    if (!saved?.ok) {
      ElMessage.warning(saved?.error || 'live action issue record failed')
      return
    }
    const [consolePayload, axisBoard, queue, suggestions, session, completionPacket, completionAudit, cockpit] = await Promise.all([
      getGen3StateAlphaLiveActionConsole(),
      getGen3StateAlphaStrategyTuningAxisBoard(),
      getGen3StateAlphaStrategyTuningReviewQueue(),
      getGen3StateAlphaStrategyTuningReplaySuggestions(),
      getGen3StateAlphaStrategyTuningReplaySession(),
      getGen3StateAlphaStrategyTuningCurrentStepCompletionPacket(),
      getGen3StateAlphaStrategyTuningCompletionAudit(),
      getGen3StateAlphaLiveReplayCockpit()
    ])
    liveActionConsolePayload.value = consolePayload || liveActionConsolePayload.value || {}
    strategyTuningAxisBoardPayload.value = axisBoard || strategyTuningAxisBoardPayload.value || {}
    strategyTuningReviewQueuePayload.value = queue || strategyTuningReviewQueuePayload.value || {}
    strategyTuningReplaySuggestionsPayload.value = suggestions || strategyTuningReplaySuggestionsPayload.value || {}
    strategyTuningReplaySessionPayload.value = session || strategyTuningReplaySessionPayload.value || {}
    strategyTuningCurrentStepCompletionPacketPayload.value = completionPacket || strategyTuningCurrentStepCompletionPacketPayload.value || {}
    strategyTuningCompletionAuditPayload.value = completionAudit || strategyTuningCompletionAuditPayload.value || {}
    liveReplayCockpitPayload.value = cockpit || liveReplayCockpitPayload.value || {}
    ElMessage.success('live action issue recorded')
  } catch (error) {
    ElMessage.warning(error?.message || 'live action issue record failed')
  } finally {
    liveActionConsoleReviewSavingKey.value = ''
  }
}

async function applyStrategyTuningReplaySuggestion(row) {
  const key = strategyTuningTaskRowKey(row)
  const result = row?.suggested_review_result || 'continue_watch'
  strategyTuningTaskSavingKey.value = key
  try {
    const saved = await saveGen3StateAlphaStrategyTuningTaskReview({
      task_key: key,
      axis: row?.axis,
      axis_label: row?.axis_label,
      object: row?.object,
      problem_signal: row?.problem_signal,
      source_status: row?.source_status,
      review_result: result,
      evidence: row?.evidence_required,
      completion_evidence: row?.evidence_required,
      review_note: row?.suggested_review_note || row?.replay_focus,
      hidden_risk: row?.suggested_hidden_risk,
      decision: row?.suggested_decision,
      optimization_suggestion: row?.suggested_optimization,
      next_action: row?.next_action,
      optimization_boundary: row?.optimization_boundary,
      can_execute_trade: false,
      can_change_strategy_contract: false,
      profit_only_optimization_allowed: false
    })
    if (!saved?.ok) {
      ElMessage.warning(saved?.error || 'strategy tuning suggestion save failed')
      return
    }
    const [axisBoard, queue, suggestions, session, completionPacket, completionAudit, cockpit, historicalReplayAudit, historicalReplay] = await Promise.all([
      getGen3StateAlphaStrategyTuningAxisBoard(),
      getGen3StateAlphaStrategyTuningReviewQueue(),
      getGen3StateAlphaStrategyTuningReplaySuggestions(),
      getGen3StateAlphaStrategyTuningReplaySession(),
      getGen3StateAlphaStrategyTuningCurrentStepCompletionPacket(),
      getGen3StateAlphaStrategyTuningCompletionAudit(),
      getGen3StateAlphaLiveReplayCockpit(),
      getGen3StateAlphaHistoricalDecisionReplayAudit(),
      getGen3StateAlphaHistoricalDecisionReplayTasks({ limit: 80 })
    ])
    strategyTuningAxisBoardPayload.value = axisBoard || strategyTuningAxisBoardPayload.value || {}
    strategyTuningReviewQueuePayload.value = queue || strategyTuningReviewQueuePayload.value || {}
    strategyTuningReplaySuggestionsPayload.value = suggestions || strategyTuningReplaySuggestionsPayload.value || {}
    strategyTuningReplaySessionPayload.value = session || strategyTuningReplaySessionPayload.value || {}
    strategyTuningCurrentStepCompletionPacketPayload.value = completionPacket || strategyTuningCurrentStepCompletionPacketPayload.value || {}
    strategyTuningCompletionAuditPayload.value = completionAudit || strategyTuningCompletionAuditPayload.value || {}
    liveReplayCockpitPayload.value = cockpit || liveReplayCockpitPayload.value || {}
    historicalDecisionReplayAuditPayload.value = historicalReplayAudit || historicalDecisionReplayAuditPayload.value || {}
    historicalDecisionReplayPayload.value = historicalReplay || historicalDecisionReplayPayload.value || {}
    ElMessage.success('strategy tuning suggestion recorded')
  } catch (error) {
    ElMessage.warning(error?.message || 'strategy tuning suggestion save failed')
  } finally {
    strategyTuningTaskSavingKey.value = ''
  }
}

async function savePretradeReview(row, action) {
  const key = reviewRowKey(row)
  const confirmationItems = pretradeConfirmationItems(row)
  if (action === 'manual_approved') {
    try {
      await ElMessageBox.confirm(
        `确认将 ${row?.code || ''} ${row?.name || ''} 标记为“人工放行”？这只记录复盘结论，不会触发下单。\n\n必须确认：\n- ${confirmationItems.join('\n- ')}`,
        '逐票人工放行确认',
        {
          confirmButtonText: '确认放行',
          cancelButtonText: '取消',
          type: 'warning'
        }
      )
    } catch {
      return
    }
  }
  reviewSavingKey.value = key
  try {
    const result = await saveGen3StateAlphaPretradeTicketReview(buildPretradeReviewPayload(row, action))
    if (result?.ok) {
      ElMessage.success('已记录逐票复盘结论')
      await refreshReadinessAndLedger('pretrade_ticket_review_saved')
    } else {
      ElMessage.warning(result?.error || '逐票复盘记录失败')
    }
  } catch (error) {
    ElMessage.warning(error?.message || '逐票复盘记录失败')
  } finally {
    reviewSavingKey.value = ''
  }
}

async function markUnreviewedAsPaperWatch() {
  const rows = unreviewedTicketRows.value
  if (!rows.length) {
    ElMessage.info('当前没有未复盘票据')
    return
  }
  batchReviewLoading.value = true
  let preview = null
  try {
    preview = await startGen3StateAlphaPretradePaperWatchBatch({
      reason: '实战启动批次：未复盘票据先进入纸面观察',
      include_existing: false,
      dry_run: true,
      run_readiness_review: false
    })
  } catch (error) {
    batchReviewLoading.value = false
    ElMessage.warning(error?.message || '纸面观察批次预检失败')
    return
  }
  const previewRows = Array.isArray(preview?.preview_reviews) ? preview.preview_reviews : []
  const skippedRows = Array.isArray(preview?.skipped) ? preview.skipped : []
  if (!previewRows.length) {
    batchReviewLoading.value = false
    ElMessage.info(skippedRows.length ? `没有新增纸面观察票；已跳过 ${skippedRows.length} 张` : '当前没有可初始化的纸面观察票')
    return
  }
  const previewText = previewRows.map((row) => `${row.code || '--'} ${row.name || ''}`).join('、')
  try {
    await ElMessageBox.confirm(
      `确认把 ${previewRows.length} 张未复盘票据记录为“纸面观察”？\n\n预检票据：${previewText}\n\n这不会触发下单，也不会视为正式放行。`,
      '批量纸面观察',
      {
        confirmButtonText: '记录纸面观察',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )
  } catch {
    batchReviewLoading.value = false
    return
  }
  try {
    const batch = await startGen3StateAlphaPretradePaperWatchBatch({
      reason: '实战启动批次：未复盘票据先进入纸面观察',
      include_existing: false,
      run_readiness_review: true
    })
    await Promise.all([
      getGen3StateAlphaPretradeTicketReviews().then((ledger) => {
        pretradeReviewLedger.value = Array.isArray(ledger?.reviews) ? ledger.reviews : []
      }),
      getGen3StateAlphaPaperWatchReviews().then((ledger) => {
        paperWatchReviewLedger.value = Array.isArray(ledger?.reviews) ? ledger.reviews : []
      }),
      getGen3StateAlphaRealtimeReadinessReview().then((review) => {
        readinessReview.value = review || {}
      })
    ])
    if (!batch?.ok) {
      ElMessage.warning(batch?.saved_count ? `已记录 ${batch.saved_count} 张，仍有异常需复查` : (batch?.message || '批量纸面观察未完成'))
    } else if (batch?.skipped_count) {
      ElMessage.success(`已启动纸面观察批次 ${batch.batch_id}：新增 ${batch.saved_count} 张，跳过 ${batch.skipped_count} 张`)
    } else {
      ElMessage.success(`已启动纸面观察批次 ${batch.batch_id}：${batch.saved_count} 张`)
    }
  } catch (error) {
    ElMessage.warning(error?.message || '批量记录纸面观察失败')
  } finally {
    batchReviewLoading.value = false
  }
}

async function recordDay1PaperExecutions() {
  if (!day1PaperReviewRows.value.length) {
    ElMessage.info('当前没有Day1纸面复盘包')
    return
  }
  day1ExecutionLoading.value = true
  let preview = null
  try {
    preview = await startGen3StateAlphaDay1PaperExecutionBatch({
      dry_run: true,
      include_existing: false,
      run_readiness_review: false
    })
  } catch (error) {
    day1ExecutionLoading.value = false
    ElMessage.warning(error?.message || 'Day1纸面执行预检失败')
    return
  }
  const previewRows = Array.isArray(preview?.preview_executions) ? preview.preview_executions : []
  const skippedRows = Array.isArray(preview?.skipped) ? preview.skipped : []
  if (!previewRows.length) {
    day1ExecutionLoading.value = false
    ElMessage.info(skippedRows.length ? `没有新增纸面执行；已跳过 ${skippedRows.length} 张` : '当前没有可记录的Day1纸面执行')
    return
  }
  const previewText = previewRows
    .map((row) => `${row.code || '--'} ${row.name || ''} @${price(row.execution_price)} x ${row.quantity || 0}`)
    .join('\n')
  try {
    await ElMessageBox.confirm(
      `确认把 ${previewRows.length} 张票写入Day1纸面执行台账？\n\n${previewText}\n\n这只记录纸面执行，不会触发真实下单，也不等于正式放行。`,
      'Day1纸面执行',
      {
        confirmButtonText: '写入纸面台账',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )
  } catch {
    day1ExecutionLoading.value = false
    return
  }
  try {
    const batch = await startGen3StateAlphaDay1PaperExecutionBatch({
      include_existing: false,
      run_readiness_review: true
    })
    const [current, review] = await Promise.all([
      getGen3StateAlphaCurrent({ limit: 100 }),
      getGen3StateAlphaRealtimeReadinessReview()
    ])
    currentPayload.value = current || {}
    readinessReview.value = review || {}
    if (!batch?.ok) {
      ElMessage.warning(batch?.saved_count ? `已写入 ${batch.saved_count} 张，仍有异常需复查` : (batch?.message || 'Day1纸面执行未完成'))
    } else if (batch?.skipped_count) {
      ElMessage.success(`已写入Day1纸面执行批次 ${batch.batch_id}：新增 ${batch.saved_count} 张，跳过 ${batch.skipped_count} 张`)
    } else {
      ElMessage.success(`已写入Day1纸面执行批次 ${batch.batch_id}：${batch.saved_count} 张`)
    }
  } catch (error) {
    ElMessage.warning(error?.message || 'Day1纸面执行写入失败')
  } finally {
    day1ExecutionLoading.value = false
  }
}

function paperWatchAutoReviewText(result, row = {}) {
  const label = paperWatchResultLabel(result)
  const isOmission = isCandidateOmissionReview(row)
  if (isOmission && result === 'as_expected') {
    return {
      hiddenRisk: '候选被挡后没有暴露新增误杀证据',
      suggestion: '保持当前阻断/观察纪律，继续用同类候选验证排序、槽位和盘中确认规则'
    }
  }
  if (isOmission && result === 'missed_opportunity') {
    return {
      hiddenRisk: '可能误杀：候选被挡后仍按预期走强，需要复盘盘中确认、排序或补位规则是否过严',
      suggestion: '先把样本归入候选遗漏复盘，不直接放宽买点；重点检查确认窗口、槽位排序和策略切换时点'
    }
  }
  if (isOmission && result === 'invalid_signal') {
    return {
      hiddenRisk: '信号失效：候选被挡后未走强，当前阻断逻辑得到验证',
      suggestion: '保留当前阻断逻辑，并继续观察同类候选是否稳定失效'
    }
  }
  if (result === 'as_expected') {
    return {
      hiddenRisk: '暂未发现新增隐患',
      suggestion: '保留当前买点、选股和降仓解释，继续观察同类信号复现'
    }
  }
  if (result === 'continue_watch') {
    return {
      hiddenRisk: '证据不足，暂不归因为策略错误',
      suggestion: '延长观察窗口，等待价格、量能和退出信号进一步确认'
    }
  }
  return {
    hiddenRisk: `${label}：需要确认是否属于自然交易逻辑不顺，而不是单日收益噪声`,
    suggestion: `把本次问题纳入 ${paperWatchIssueArea(result)} 调优队列，先复盘行为解释，再决定是否改规则`
  }
}

async function savePaperWatchFollowup(row, result) {
  const label = paperWatchResultLabel(result)
  let note = ''
  try {
    const response = await ElMessageBox.prompt(
      `记录 ${row?.code || ''} ${row?.name || ''} 的纸面观察结果：${label}。请写一句观察依据，例如走势是否验证买点、是否追高、是否暴露选股或卖点隐患。`,
      '纸面观察后评估',
      {
        confirmButtonText: '保存评估',
        cancelButtonText: '取消',
        inputType: 'textarea',
        inputPlaceholder: '观察依据',
        inputValidator: (value) => String(value || '').trim().length >= 4 || '请至少写一句观察依据'
      }
    )
    note = String(response?.value || '').trim()
  } catch {
    return
  }
  const reviewContext = isCandidateOmissionReview(row) ? 'candidate_omission' : 'paper_watch'
  const autoText = paperWatchAutoReviewText(result, row)
  try {
    const payload = {
      ticket_key: row?.ticket_key || row?.key,
      code: row?.code,
      name: row?.name,
      entry_date: row?.entry_date,
      route: row?.route,
      source: row?.source || (reviewContext === 'candidate_omission' ? 'candidate_omission_checklist' : 'g3_state_alpha_risk_page'),
      review_context: reviewContext,
      watch_result: result,
      issue_area: paperWatchIssueArea(result),
      hidden_risk: autoText.hiddenRisk,
      optimization_suggestion: autoText.suggestion,
      selection_state: row?.selection_state || row?.evidence,
      strategy_switch_assessment: row?.strategy_switch_assessment || row?.natural_decision,
      candidate_block_reason: row?.hidden_risks || row?.optimization_focus,
      action_recommendation: row?.action_recommendation || row?.required_action,
      review_note: note
    }
    const saved = reviewContext === 'candidate_omission'
      ? await saveGen3StateAlphaCandidateOmissionReviewAndRun({ ...payload, review_result: result })
      : await saveGen3StateAlphaPaperWatchReview(payload)
    if (!saved?.ok) {
      ElMessage.warning(saved?.error || '纸面观察评估保存失败')
      return
    }
    if (saved.review) {
      readinessReview.value = saved.review
    }
    ElMessage.success(reviewContext === 'candidate_omission' ? '已保存候选遗漏复盘并完成复审' : '已保存纸面观察后评估')
    if (reviewContext === 'candidate_omission') {
      await refreshCurrentAndLedgers('candidate_omission_review_saved')
    } else {
      await refreshReadinessAndLedger('paper_watch_followup_saved')
    }
  } catch (error) {
    ElMessage.warning(error?.message || '纸面观察评估保存失败')
  }
}

function noTradeAutoReviewText(result, row = {}) {
  if (result === 'natural_no_trade') {
    return {
      hiddenRisk: '无票日未发现新增误杀证据',
      suggestion: '保持空仓纪律，不为了补满二槽降低买点、盘中确认或持仓刷新要求'
    }
  }
  if (result === 'missed_opportunity') {
    return {
      hiddenRisk: '可能误杀：无票日候选或路由被挡后仍出现可交易走势',
      suggestion: '先复盘候选排序、盘中确认、槽位约束和策略切换时点，不直接放宽仓位'
    }
  }
  if (result === 'data_gap') {
    return {
      hiddenRisk: '数据/确认缺口：无票可能来自分钟线、持仓刷新或审计输入不完整',
      suggestion: '优先修复数据链路和确认证据，再判断是否属于策略规则问题'
    }
  }
  if (result === 'process_gap') {
    return {
      hiddenRisk: '流程缺口：实战前动作未闭环，可能导致本可交易信号无法进入执行',
      suggestion: '把流程问题纳入盘前清单，先修复执行闭环，再评估交易模型'
    }
  }
  return {
    hiddenRisk: row?.hidden_risk || '证据不足，暂不归因为策略错误',
    suggestion: '继续观察无票日候选、持仓刷新和盘中确认结果，等待更完整证据'
  }
}

async function saveNoTradeDayReview(row, result) {
  const label = noTradeReviewResultLabel(result)
  let note = ''
  try {
    const response = await ElMessageBox.prompt(
      `记录 ${row?.entry_date || ''} 无票复盘：${label}。请写一句依据，例如是否自然空仓、是否误杀候选、是否来自数据或流程缺口。`,
      '无票日复盘',
      {
        confirmButtonText: '保存复盘',
        cancelButtonText: '取消',
        inputType: 'textarea',
        inputPlaceholder: '复盘依据',
        inputValidator: (value) => String(value || '').trim().length >= 4 || '请至少写一句复盘依据'
      }
    )
    note = String(response?.value || '').trim()
  } catch {
    return
  }
  const autoText = noTradeAutoReviewText(result, row)
  try {
    const payload = {
      review_key: row?.review_key,
      entry_date: row?.entry_date,
      review_type: row?.review_type,
      posture: row?.posture,
      source: row?.source,
      review_result: result,
      issue_area: noTradeIssueArea(result),
      evidence: row?.evidence,
      hidden_risk: autoText.hiddenRisk,
      natural_decision: row?.natural_decision,
      optimization_suggestion: autoText.suggestion,
      review_note: note
    }
    const saved = await saveGen3StateAlphaNoTradeDayReviewAndRun(payload)
    if (!saved?.ok) {
      ElMessage.warning(saved?.error || '无票日复盘保存失败')
      return
    }
    if (saved.review) {
      readinessReview.value = saved.review
    }
    ElMessage.success('已保存无票日复盘并完成复审')
    await refreshCurrentAndLedgers('no_trade_day_review_saved')
  } catch (error) {
    ElMessage.warning(error?.message || '无票日复盘保存失败')
  }
}

async function runReadinessReview() {
  reviewLoading.value = true
  try {
    const review = await refreshReadinessAndLedger()
    if (review?.ok) ElMessage.success('已重跑 G3 实战前逐票审计')
    else ElMessage.warning('审计已返回，但存在运行异常，请查看报告')
  } catch (error) {
    ElMessage.warning(error?.message || '重跑 G3 实战前审计失败')
  } finally {
    reviewLoading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.g3-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
  color: #1f2a44;
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
}

.eyebrow {
  color: #2f80ed;
  font-size: 12px;
  font-weight: 700;
  margin-bottom: 4px;
}

h1,
h2,
p {
  margin: 0;
}

h1 {
  font-size: 26px;
  line-height: 1.2;
}

h2 {
  font-size: 16px;
}

.topbar p,
.panel-head p,
.metric-card small,
.gate-row span {
  color: #667085;
  line-height: 1.5;
}

.actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 10px;
}

.metric-card {
  min-height: 92px;
  padding: 12px;
  border-left: 4px solid #2f80ed;
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.metric-card.accent {
  border-left-color: #12b76a;
}

.metric-card.danger {
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

.two-col {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 14px;
}

.review-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 12px;
}

.review-grid > div {
  min-height: 78px;
  border: 1px solid #eef2f6;
  border-radius: 8px;
  padding: 10px;
  background: #f8fafc;
}

.review-grid span,
.review-grid small {
  display: block;
  color: #667085;
  font-size: 12px;
  line-height: 1.4;
}

.review-grid strong {
  display: block;
  margin: 5px 0;
  color: #1f2a44;
  font-size: 18px;
}

.premarket-control-panel {
  border-left: 4px solid #7a5af8;
}

.live-launch-panel {
  border-left: 4px solid #12b76a;
}

.live-replay-cockpit {
  border-left: 4px solid #2f80ed;
}

.historical-replay-card {
  border-left: 4px solid #f79009;
}

.launch-summary-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 12px;
}

.launch-summary-grid > div {
  min-width: 0;
  min-height: 78px;
  border: 1px solid #e6f4ea;
  border-radius: 8px;
  padding: 10px;
  background: #f6fef9;
}

.launch-summary-grid span,
.launch-summary-grid small {
  display: block;
  color: #667085;
  font-size: 12px;
  line-height: 1.5;
}

.launch-summary-grid strong {
  display: block;
  margin: 4px 0;
  color: #101828;
  font-size: 16px;
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.snapshot-detail {
  display: grid;
  gap: 14px;
}

.snapshot-detail-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 10px;
}

.snapshot-detail-grid > div,
.snapshot-note {
  min-width: 0;
  border: 1px solid #eef2f6;
  border-radius: 8px;
  padding: 10px;
  background: #f8fafc;
}

.snapshot-detail-grid span {
  display: block;
  color: #667085;
  font-size: 12px;
  line-height: 1.5;
}

.snapshot-detail-grid strong {
  display: block;
  color: #101828;
  font-size: 14px;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.snapshot-note strong {
  display: block;
  color: #344054;
  font-size: 13px;
  margin-bottom: 6px;
}

.snapshot-note p {
  margin: 0;
  color: #475467;
  font-size: 13px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.premarket-command-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 10px;
}

.confirmation-card {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}

.premarket-command-grid > div,
.premarket-command-detail > div,
.confirmation-card > div {
  min-width: 0;
  border: 1px solid #eef2f6;
  border-radius: 8px;
  padding: 10px;
  background: #f8fafc;
}

.premarket-command-grid span,
.premarket-command-grid small,
.premarket-command-detail span,
.confirmation-card span,
.confirmation-card small {
  display: block;
  color: #667085;
  font-size: 12px;
  line-height: 1.5;
}

.premarket-command-grid strong,
.premarket-command-detail strong,
.confirmation-card strong {
  display: block;
  color: #101828;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.premarket-command-grid strong {
  font-size: 16px;
}

.premarket-command-detail {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.premarket-command-detail strong {
  font-size: 13px;
  font-weight: 600;
}

.decision-card {
  display: grid;
  grid-template-columns: minmax(180px, 0.9fr) minmax(0, 2.2fr) auto;
  gap: 12px;
  align-items: stretch;
  border: 1px solid #d9e2ec;
  border-left: 4px solid #2f80ed;
  border-radius: 8px;
  padding: 12px;
  background: #f8fafc;
}

.decision-card--block {
  border-left-color: #d92d20;
  background: #fff8f7;
}

.decision-card--ready {
  border-left-color: #12b76a;
  background: #f6fef9;
}

.decision-card--observe {
  border-left-color: #667085;
}

.decision-main span,
.decision-detail span {
  display: block;
  color: #667085;
  font-size: 12px;
  margin-bottom: 5px;
}

.decision-main strong {
  display: block;
  color: #1f2a44;
  font-size: 20px;
  line-height: 1.25;
  margin-bottom: 6px;
}

.decision-main p,
.decision-detail p {
  margin: 0;
  color: #344054;
  font-size: 13px;
  line-height: 1.45;
  word-break: break-word;
}

.decision-detail {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}

.decision-detail > div {
  border-left: 1px solid #e4e9f1;
  padding-left: 10px;
}

.decision-flags {
  display: flex;
  flex-direction: column;
  gap: 7px;
  align-items: flex-start;
  justify-content: center;
  min-width: 116px;
}

.audit-card {
  border: 1px solid #d0d5dd;
  border-radius: 8px;
  padding: 12px;
  background: #ffffff;
}

.audit-card-main span,
.audit-card-grid span {
  display: block;
  color: #667085;
  font-size: 12px;
  margin-bottom: 4px;
}

.audit-card-main strong {
  display: block;
  color: #101828;
  font-size: 18px;
  line-height: 1.3;
}

.audit-card-main p {
  margin: 6px 0 0;
  color: #344054;
  font-size: 13px;
  line-height: 1.45;
  word-break: break-word;
}

.audit-card-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-top: 10px;
}

.audit-card-grid > div {
  border: 1px solid #eef2f6;
  border-radius: 6px;
  padding: 8px;
  background: #f9fafb;
}

.audit-card-grid strong {
  color: #1d2939;
  font-size: 15px;
}

.review-tables {
  align-items: flex-start;
}

.review-ticket-table {
  margin-bottom: 12px;
}

.review-checklist-table {
  margin-bottom: 12px;
}

.inline-note {
  margin-left: 6px;
  color: #475467;
  font-size: 12px;
}

.muted-action {
  color: #98a2b3;
  font-size: 12px;
}

.kv-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}

.kv-grid > div {
  min-height: 74px;
  border: 1px solid #eef2f6;
  border-radius: 8px;
  padding: 10px;
  background: #f8fafc;
}

.kv-grid span {
  display: block;
  color: #667085;
  font-size: 12px;
  margin-bottom: 6px;
}

.kv-grid strong {
  color: #1f2a44;
  font-size: 16px;
  line-height: 1.35;
  word-break: break-word;
}

.gate-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.gate-row {
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr);
  gap: 10px;
  align-items: flex-start;
  border-bottom: 1px solid #eef2f6;
  padding-bottom: 10px;
}

.gate-row strong {
  display: block;
  margin-bottom: 3px;
}

@media (max-width: 1320px) {
  .metric-grid,
  .review-grid,
  .launch-summary-grid,
  .premarket-command-grid,
  .kv-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 980px) {
  .two-col {
    grid-template-columns: 1fr;
  }

  .premarket-command-grid,
  .launch-summary-grid,
  .snapshot-detail-grid,
  .premarket-command-detail {
    grid-template-columns: 1fr;
  }

  .decision-card {
    grid-template-columns: 1fr;
  }

  .decision-flags {
    flex-direction: row;
    flex-wrap: wrap;
  }
}

@media (max-width: 760px) {
  .topbar,
  .panel-head {
    flex-direction: column;
    align-items: flex-start;
  }

  .metric-grid,
  .review-grid,
  .launch-summary-grid,
  .snapshot-detail-grid,
  .audit-card-grid,
  .confirmation-card,
  .kv-grid {
    grid-template-columns: 1fr;
  }

  .decision-detail {
    grid-template-columns: 1fr;
  }

  .decision-detail > div {
    border-left: 0;
    border-top: 1px solid #e4e9f1;
    padding-left: 0;
    padding-top: 8px;
  }
}
</style>
