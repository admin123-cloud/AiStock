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
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>实战前逐票复盘</h2>
          <p>只读审计当前票据、持仓退出、候选遗漏和 PTrade 准备状态；不刷新信号、不触发下单。</p>
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
        <el-table-column prop="selection_state" label="选股状态" min-width="280" show-overflow-tooltip />
        <el-table-column prop="strategy_switch_assessment" label="策略切换" min-width="260" show-overflow-tooltip />
        <el-table-column prop="hidden_risks" label="隐藏风险" min-width="300" show-overflow-tooltip />
        <el-table-column prop="manual_questions" label="人工问题" min-width="320" show-overflow-tooltip />
        <el-table-column prop="action_recommendation" label="动作建议" min-width="220" show-overflow-tooltip />
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
  syncGen3StateAlphaBrokerHoldingsFromThs,
  saveGen3StateAlphaPretradeTicketReview,
  saveGen3StateAlphaPaperWatchReview
} from '@/api/trading'

const loading = ref(false)
const reviewLoading = ref(false)
const brokerSyncLoading = ref(false)
const batchReviewLoading = ref(false)
const reviewSavingKey = ref('')
const payload = ref({})
const currentPayload = ref({})
const readinessReview = ref({})
const pretradeReviewLedger = ref([])
const paperWatchReviewLedger = ref([])

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
const readinessGates = computed(() => Array.isArray(readinessReview.value.readiness_gates) ? readinessReview.value.readiness_gates : [])
const pretradeActionRows = computed(() => Array.isArray(readinessReview.value.pretrade_action_checklist) ? readinessReview.value.pretrade_action_checklist : [])
const formalLaunchRows = computed(() => Array.isArray(readinessReview.value.formal_launch_checklist) ? readinessReview.value.formal_launch_checklist : [])
const executionModeRows = computed(() => Array.isArray(readinessReview.value.execution_mode_matrix) ? readinessReview.value.execution_mode_matrix : [])
const formalLaunchActionRows = computed(() => Array.isArray(readinessReview.value.formal_launch_action_queue) ? readinessReview.value.formal_launch_action_queue : [])
const premarketPlaybookRows = computed(() => Array.isArray(readinessReview.value.premarket_execution_playbook) ? readinessReview.value.premarket_execution_playbook : [])
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

function paperWatchIssueArea(result) {
  if (result === 'buy_point_too_early' || result === 'buy_point_chasing') return 'buy_point'
  if (result === 'selection_issue') return 'selection'
  if (result === 'model_switch_issue') return 'model_switch'
  if (result === 'risk_exit_issue') return 'sell_exit'
  if (result === 'invalid_signal' || result === 'missed_opportunity') return 'signal_validation'
  return 'observation'
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
  const [review, ledger, paperWatchLedger] = await Promise.all([
    runGen3StateAlphaRealtimeReadinessReview(reason ? { reason } : {}),
    getGen3StateAlphaPretradeTicketReviews(),
    getGen3StateAlphaPaperWatchReviews()
  ])
  readinessReview.value = review || {}
  pretradeReviewLedger.value = Array.isArray(ledger?.reviews) ? ledger.reviews : []
  paperWatchReviewLedger.value = Array.isArray(paperWatchLedger?.reviews) ? paperWatchLedger.reviews : []
  return review
}

async function load() {
  loading.value = true
  try {
    const [contractPayload, current, review, ledger, paperWatchLedger] = await Promise.all([
      getGen3StateAlphaContract(),
      getGen3StateAlphaCurrent({ limit: 100 }),
      getGen3StateAlphaRealtimeReadinessReview(),
      getGen3StateAlphaPretradeTicketReviews(),
      getGen3StateAlphaPaperWatchReviews()
    ])
    payload.value = contractPayload
    currentPayload.value = current
    readinessReview.value = review || {}
    pretradeReviewLedger.value = Array.isArray(ledger?.reviews) ? ledger.reviews : []
    paperWatchReviewLedger.value = Array.isArray(paperWatchLedger?.reviews) ? paperWatchLedger.reviews : []
  } catch (error) {
    ElMessage.warning(error?.message || '读取 G3 风控合同失败')
  } finally {
    loading.value = false
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
    const syncResult = await syncGen3StateAlphaBrokerHoldingsFromThs({ source: 'g3_pretrade_readiness_review' })
    if (!syncResult?.ok) {
      ElMessage.warning(syncResult?.message || syncResult?.error || '同步同花顺真实持仓失败')
      return
    }
    const [current, review, ledger, paperWatchLedger] = await Promise.all([
      getGen3StateAlphaCurrent({ limit: 100 }),
      runGen3StateAlphaRealtimeReadinessReview({ reason: 'broker_holdings_synced' }),
      getGen3StateAlphaPretradeTicketReviews(),
      getGen3StateAlphaPaperWatchReviews()
    ])
    currentPayload.value = current || {}
    readinessReview.value = review || {}
    pretradeReviewLedger.value = Array.isArray(ledger?.reviews) ? ledger.reviews : []
    paperWatchReviewLedger.value = Array.isArray(paperWatchLedger?.reviews) ? paperWatchLedger.reviews : []
    ElMessage.success(`已同步真实持仓并复审：${syncResult.holdings_count ?? syncResult.holdings?.length ?? '--'} 只`)
  } catch (error) {
    ElMessage.warning(error?.message || '同步真实持仓并复审失败')
  } finally {
    brokerSyncLoading.value = false
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
  try {
    await ElMessageBox.confirm(
      `确认把 ${rows.length} 张未复盘票据记录为“纸面观察”？这不会触发下单，也不会视为正式放行。`,
      '批量纸面观察',
      {
        confirmButtonText: '记录纸面观察',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )
  } catch {
    return
  }
  batchReviewLoading.value = true
  try {
    const results = await Promise.all(rows.map((row) => saveGen3StateAlphaPretradeTicketReview(buildPretradeReviewPayload(row, 'paper_watch'))))
    const failed = results.filter((item) => !item?.ok)
    await refreshReadinessAndLedger('pretrade_unreviewed_marked_paper_watch')
    if (failed.length) {
      ElMessage.warning(`已记录 ${rows.length - failed.length} 张，失败 ${failed.length} 张`)
    } else {
      ElMessage.success(`已将 ${rows.length} 张未复盘票据记录为纸面观察`)
    }
  } catch (error) {
    ElMessage.warning(error?.message || '批量记录纸面观察失败')
  } finally {
    batchReviewLoading.value = false
  }
}

function paperWatchAutoReviewText(result) {
  const label = paperWatchResultLabel(result)
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
  const autoText = paperWatchAutoReviewText(result)
  try {
    const payload = {
      ticket_key: row?.ticket_key || row?.key,
      code: row?.code,
      name: row?.name,
      entry_date: row?.entry_date,
      route: row?.route,
      watch_result: result,
      issue_area: paperWatchIssueArea(result),
      hidden_risk: autoText.hiddenRisk,
      optimization_suggestion: autoText.suggestion,
      review_note: note
    }
    const saved = await saveGen3StateAlphaPaperWatchReview(payload)
    if (!saved?.ok) {
      ElMessage.warning(saved?.error || '纸面观察评估保存失败')
      return
    }
    ElMessage.success('已保存纸面观察后评估')
    await refreshReadinessAndLedger('paper_watch_followup_saved')
  } catch (error) {
    ElMessage.warning(error?.message || '纸面观察评估保存失败')
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
  .kv-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 980px) {
  .two-col {
    grid-template-columns: 1fr;
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
  .kv-grid {
    grid-template-columns: 1fr;
  }
}
</style>
