// 实战前逐票复盘 · 上（review-grid / 学习台账 / 盘前复查 / 尝试记录 / 验收单 / Day1 日志 / 盘前指挥单 / 执行顺序 / 阻断路线图 / 证据台账）
import { Badge } from '@/components/catalyst/badge'
import { Btn, DataTable, Panel, StatGrid, Tag, useRisk } from './shared'
import type { Col } from './shared'
import {
  day1JournalRowLoading,
  formalActionButtonLabel,
  formalActionRowLoading,
  gateType,
  isDay1JournalNoTradeRow,
  reviewBoardStatusLabel,
  reviewBoardStatusType,
  taskToReviewRow,
  truthy,
  type Row
} from './labels'
import ReadinessPanelB from './ReadinessPanelB'

export default function ReadinessPanelA() {
  const { v, a } = useRisk()
  const s = v.readinessSummary

  const verdictTone =
    String(s.verdict || '').includes('blocked')
      ? 'red'
      : String(s.verdict || '').includes('warning')
        ? 'amber'
        : String(s.verdict || '').includes('ready')
          ? 'emerald'
          : 'zinc'

  const gridItems = [
    { span: '下一交易日', strong: s.next_trade_entry_date || '--', small: `票据 ${s.next_trade_ticket_count ?? '--'} 张` },
    {
      span: 'Gate 阻断/警告',
      strong: `${s.gate_block_count ?? '--'} / ${s.gate_warn_count ?? '--'}`,
      small: s.generated_at || '--'
    },
    {
      span: '票据阻断/警告',
      strong: `${s.ticket_block_count ?? '--'} / ${s.ticket_warning_count ?? '--'}`,
      small: '逐票执行前确认'
    },
    {
      span: '逐票复盘状态',
      strong: `就绪 ${s.pretrade_review_formal_ready_count ?? '--'} / 未复盘 ${s.pretrade_review_unreviewed_count ?? '--'}`,
      small: `纸面 ${s.pretrade_review_paper_watch_count ?? '--'} / 待刷新 ${s.pretrade_review_wait_refresh_count ?? '--'} / 跳过 ${s.pretrade_review_skip_reject_count ?? '--'}`
    },
    {
      span: '持仓阻断/警告',
      strong: `${s.holding_block_count ?? '--'} / ${s.holding_warning_count ?? '--'}`,
      small: `正式刷新 ${s.holding_formal_refresh_pending_count ?? '--'} / 观察 ${s.holding_observation_refresh_count ?? '--'}`
    },
    {
      span: '正式动作',
      strong: s.pretrade_formal_action_pending_count ?? '--',
      small: `观察 ${s.pretrade_observation_action_pending_count ?? '--'} / 总待处理 ${s.pretrade_action_pending_count ?? '--'}`
    },
    {
      span: '组合决策',
      strong: `${s.portfolio_decision_block_count ?? '--'} / ${s.portfolio_decision_warn_watch_count ?? '--'}`,
      small: '阻断 / 关注'
    },
    {
      span: '正式放行',
      strong: s.formal_launch_status || '--',
      small: `缺口 ${s.formal_launch_missing_count ?? '--'} / ${s.formal_launch_next_step || '--'}`
    },
    {
      span: '执行入口',
      strong: `${s.execution_mode_ready_count ?? '--'} / ${s.execution_mode_locked_count ?? '--'}`,
      small: '可用 / 锁定，dry-run 不等于真实下单'
    },
    { span: '放行队列', strong: s.formal_launch_action_queue_count ?? '--', small: '按优先级处理' },
    {
      span: '盘前剧本',
      strong: s.premarket_playbook_step_count ?? '--',
      small: '按窗口顺序执行，不把观察项当硬阻断'
    },
    {
      span: '盘前指挥单',
      strong: `${s.live_premarket_blocking_command_count ?? '--'} / ${s.live_premarket_command_count ?? '--'}`,
      small: `阻止真实买入 / 指挥动作；正式必处理 ${s.live_premarket_formal_command_count ?? '--'}`
    },
    {
      span: '无票复盘',
      strong: s.no_trade_day_review_count ?? '--',
      small: `待复盘 ${s.no_trade_day_pending_review_count ?? '--'} / 已验证 ${s.no_trade_day_validated_count ?? '--'} / 隐患 ${s.no_trade_day_issue_found_count ?? '--'}`
    },
    {
      span: '每日复盘看板',
      strong: s.daily_live_review_board_count ?? '--',
      small: `待处理 ${s.daily_live_review_board_pending_count ?? '--'} / 正式必处理 ${s.daily_live_review_board_formal_required_count ?? '--'}`
    },
    {
      span: '实战任务队列',
      strong: s.live_review_task_count ?? '--',
      small: `盘前 ${s.live_review_premarket_task_count ?? '--'} / 盘中盘后 ${s.live_review_intraday_after_task_count ?? '--'} / 盘后 ${s.live_review_after_close_task_count ?? '--'}`
    },
    {
      span: '复盘覆盖率',
      strong: `${s.review_coverage_pending_scope_count ?? '--'} / ${s.review_coverage_scope_count ?? '--'}`,
      small: `待复盘 / 总范围；隐患 ${s.review_coverage_issue_scope_count ?? '--'} / 继续观察 ${s.review_coverage_continue_watch_scope_count ?? '--'}`
    },
    {
      span: '复盘证据矩阵',
      strong: `${s.live_review_rubric_pending_scope_count ?? '--'} / ${s.live_review_rubric_scope_count ?? '--'}`,
      small: `待复盘范围 / 规则范围；正式必处理 ${s.live_review_rubric_formal_scope_count ?? '--'}`
    },
    {
      span: '学习隐患队列',
      strong: s.strategy_learning_backlog_count ?? '--',
      small: `需调优 ${s.strategy_learning_needs_review_count ?? '--'} / 继续观察 ${s.strategy_learning_watch_more_count ?? '--'}`
    },
    {
      span: '候选遗漏复盘',
      strong: s.candidate_omission_pending_count ?? '--',
      small: `待观察 / 已验证 ${s.candidate_omission_validated_count ?? '--'} / 继续 ${s.candidate_omission_continue_watch_count ?? '--'} / 隐患 ${s.candidate_omission_issue_found_count ?? '--'}`
    },
    {
      span: 'Day1 纸面复盘包',
      strong: s.day1_paper_review_ticket_count ?? '--',
      small: `待纸面/待刷新 ${s.day1_paper_review_pending_count ?? '--'}，不等于正式买入`
    },
    {
      span: 'Day1 盘后复盘',
      strong: s.day1_after_close_review_count ?? '--',
      small: `待执行 ${s.day1_after_close_pending_execution_count ?? '--'} / 待复盘 ${s.day1_after_close_pending_review_count ?? '--'} / 隐患 ${s.day1_after_close_issue_found_count ?? '--'}`
    },
    {
      span: '复盘证据',
      strong: s.pretrade_review_evidence_count ?? '--',
      small: '人工放行必须同时有风险确认'
    },
    {
      span: '自然交易一致性',
      strong: s.natural_consistency_min_score ?? '--',
      small: `偏拧/不顺 ${s.natural_consistency_strained_count ?? '--'} 张；执行阻断 ${s.natural_execution_block_count ?? '--'}`
    }
  ]

  const ledgerCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'origin', label: '来源', width: 170 },
    { key: 'learning_status', label: '学习状态', width: 120 },
    { key: 'risk_level', label: '风险级别', width: 110 },
    { key: 'issue_area', label: '问题轴', width: 150 },
    { key: 'object', label: '对象', minWidth: 180 },
    { key: 'problem_signal', label: '问题信号/隐患', minWidth: 320 },
    { key: 'evidence', label: '证据/缺口', minWidth: 320 },
    { key: 'suggested_learning', label: '学习/处理方向', minWidth: 340 },
    { key: 'natural_trade_boundary', label: '自然交易边界', minWidth: 340 }
  ]

  const recheckCols: Col[] = [
    {
      key: 'recheck_status',
      label: '复查结论',
      width: 160,
      render: (row: Row) => (
        <Tag tone={reviewBoardStatusType(row.recheck_status)}>{reviewBoardStatusLabel(row.recheck_status)}</Tag>
      )
    },
    {
      key: 'live_buy_allowed',
      label: '真实买入',
      width: 96,
      render: (row: Row) => (
        <Tag tone={truthy(row.live_buy_allowed) ? 'success' : 'danger'}>
          {truthy(row.live_buy_allowed) ? '允许' : '禁止'}
        </Tag>
      )
    },
    { key: 'blocking_command_count', label: '阻断动作', width: 92, align: 'right' },
    { key: 'pending_evidence_count', label: '待证据', width: 88, align: 'right' },
    { key: 'current_action_label', label: '当前步骤', minWidth: 210 },
    { key: 'next_operator_action', label: '下一动作', minWidth: 340 },
    { key: 'recheck_rule', label: '复查规则', minWidth: 360 },
    { key: 'natural_trade_boundary', label: '自然交易边界', minWidth: 360 }
  ]

  const attemptCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'attempted_at', label: '尝试时间', width: 170 },
    { key: 'action_label', label: '动作', minWidth: 190 },
    {
      key: 'ok',
      label: '结果',
      width: 92,
      render: (row: Row) => (
        <Tag tone={truthy(row.ok) ? 'success' : 'danger'}>{truthy(row.ok) ? '成功' : '未完成'}</Tag>
      )
    },
    {
      key: 'sync_review',
      label: '同步/复审',
      width: 120,
      render: (row: Row) =>
        `${
          row.sync_mode === 'not_applicable' ? '无需同步' : truthy(row.sync_ok) ? '同步' : '同步失败'
        } / ${truthy(row.review_ok) ? '复审' : '复审失败'}`
    },
    { key: 'review_key', label: '复盘键', minWidth: 180 },
    { key: 'review_status', label: '复盘状态', width: 110 },
    { key: 'live_admission_blocking_command_count', label: '复审阻断', width: 96, align: 'right' },
    {
      key: 'live_admission_buy_allowed',
      label: '真实买入',
      width: 96,
      render: (row: Row) => (
        <Tag tone={truthy(row.live_admission_buy_allowed) ? 'success' : 'danger'}>
          {truthy(row.live_admission_buy_allowed) ? '允许' : '禁止'}
        </Tag>
      )
    },
    { key: 'live_premarket_next_action', label: '复审后下一步', minWidth: 260 },
    { key: 'evidence_boundary', label: '证据边界', minWidth: 360 }
  ]

  const acceptanceCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'acceptance_label', label: '验收项', minWidth: 180 },
    {
      key: 'status',
      label: '状态',
      width: 96,
      render: (row: Row) => (
        <Tag tone={row.status === 'pass' ? 'success' : row.status === 'block' ? 'danger' : 'warning'}>
          {row.status_label || reviewBoardStatusLabel(row.status)}
        </Tag>
      )
    },
    {
      key: 'is_hard_blocker',
      label: '硬阻断',
      width: 92,
      render: (row: Row) => (
        <Tag tone={truthy(row.is_hard_blocker) ? 'danger' : 'info'}>
          {truthy(row.is_hard_blocker) ? '是' : '否'}
        </Tag>
      )
    },
    { key: 'current_value', label: '当前值', minWidth: 150 },
    { key: 'required_value', label: '要求', minWidth: 180 },
    { key: 'evidence', label: '证据', minWidth: 260 },
    { key: 'next_action', label: '下一步', minWidth: 320 },
    { key: 'natural_trade_boundary', label: '自然交易边界', minWidth: 340 }
  ]

  const journalCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'journal_window', label: '窗口', width: 110 },
    { key: 'checkpoint_time', label: '时间点', width: 130 },
    { key: 'journal_type', label: '日志类型', width: 150 },
    {
      key: 'status',
      label: '状态',
      width: 130,
      render: (row: Row) => (
        <Tag tone={reviewBoardStatusType(row.status)}>{reviewBoardStatusLabel(row.status)}</Tag>
      )
    },
    {
      key: 'is_blocking',
      label: '阻断',
      width: 82,
      render: (row: Row) => (
        <Tag tone={truthy(row.is_blocking) ? 'danger' : 'info'}>
          {truthy(row.is_blocking) ? '是' : '否'}
        </Tag>
      )
    },
    { key: 'review_axis', label: '复盘轴', width: 160 },
    { key: 'object', label: '对象', minWidth: 180 },
    { key: 'action', label: '动作', minWidth: 280 },
    { key: 'evidence_to_record', label: '应记录证据', minWidth: 320 },
    { key: 'pass_condition', label: '通过条件', minWidth: 280 },
    { key: 'fail_condition', label: '失败边界', minWidth: 280 },
    { key: 'next_action', label: '下一步', minWidth: 300 },
    { key: 'natural_trade_boundary', label: '自然交易边界', minWidth: 320 },
    { key: 'review_key', label: 'Review Key', minWidth: 260 },
    {
      key: 'actions',
      label: '处理',
      width: 390,
      render: (row: Row) => {
        if (row.source_table === 'live_premarket_action_sequence' || row.source_table === 'live_manual_launch_acceptance') {
          return (
            <Btn tone="primary" loading={day1JournalRowLoading(row, v)} onClick={() => a.handleDay1JournalPrimaryAction(row)}>
              执行下一步
            </Btn>
          )
        }
        if (row.source_table === 'live_blocker_evidence_ledger') {
          if (isDay1JournalNoTradeRow(row)) {
            return (
              <div className="flex gap-1.5">
                <Btn tone="success" loading={day1JournalRowLoading(row, v)} onClick={() => a.saveNoTradeDayReview(taskToReviewRow(row), 'natural_no_trade')}>
                  空仓合理
                </Btn>
                <Btn tone="danger" loading={day1JournalRowLoading(row, v)} onClick={() => a.saveNoTradeDayReview(taskToReviewRow(row), 'process_gap')}>
                  流程缺口
                </Btn>
              </div>
            )
          }
          return (
            <div className="flex gap-1.5">
              <Btn tone="success" loading={day1JournalRowLoading(row, v)} onClick={() => a.saveFormalActionReview(taskToReviewRow(row), 'manual_done')}>
                已验证
              </Btn>
              <Btn tone="danger" loading={day1JournalRowLoading(row, v)} onClick={() => a.saveFormalActionReview(taskToReviewRow(row), 'blocked')}>
                发现隐患
              </Btn>
              <Btn tone="warning" loading={day1JournalRowLoading(row, v)} onClick={() => a.saveFormalActionReview(taskToReviewRow(row), 'continue_watch')}>
                继续观察
              </Btn>
            </div>
          )
        }
        if (row.source_table === 'live_daily_review_execution_checklist') {
          return (
            <div className="flex gap-1.5">
              <Btn tone="success" loading={day1JournalRowLoading(row, v)} onClick={() => a.saveDailyReviewChecklistReview(row, 'validated')}>
                符合预期
              </Btn>
              <Btn tone="danger" loading={day1JournalRowLoading(row, v)} onClick={() => a.saveDailyReviewChecklistReview(row, 'issue_found')}>
                发现隐患
              </Btn>
              <Btn tone="warning" loading={day1JournalRowLoading(row, v)} onClick={() => a.saveDailyReviewChecklistReview(row, 'continue_watch')}>
                继续观察
              </Btn>
            </div>
          )
        }
        return <span className="text-xs text-zinc-500 dark:text-zinc-400">按盘后队列复盘</span>
      }
    }
  ]

  const commandCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'command_window', label: '窗口', width: 104 },
    { key: 'command_type', label: '动作类型', width: 140 },
    { key: 'decision_gate', label: '放行判断', minWidth: 180 },
    {
      key: 'current_status',
      label: '当前状态',
      width: 126,
      render: (row: Row) => (
        <Tag tone={reviewBoardStatusType(row.current_status)}>{reviewBoardStatusLabel(row.current_status)}</Tag>
      )
    },
    { key: 'unlock_status', label: '解锁状态', width: 150 },
    {
      key: 'blocks_live_buy',
      label: '阻止买入',
      width: 96,
      render: (row: Row) => (
        <Tag tone={truthy(row.blocks_live_buy) ? 'danger' : 'info'}>
          {truthy(row.blocks_live_buy) ? '阻止' : '不阻止'}
        </Tag>
      )
    },
    {
      key: 'formal_trade_required',
      label: '正式必处理',
      width: 104,
      render: (row: Row) => (
        <Tag tone={truthy(row.formal_trade_required) ? 'warning' : 'info'}>
          {truthy(row.formal_trade_required) ? '是' : '观察'}
        </Tag>
      )
    },
    { key: 'review_scope', label: '复盘范围', width: 150 },
    { key: 'code', label: '代码', width: 100 },
    { key: 'name', label: '名称', minWidth: 100 },
    { key: 'object', label: '对象', minWidth: 170 },
    { key: 'action', label: '动作', minWidth: 340 },
    { key: 'acceptance', label: '验收标准', minWidth: 320 },
    { key: 'evidence_required', label: '证据要求', minWidth: 340 },
    { key: 'optimization_boundary', label: '优化边界', minWidth: 340 },
    { key: 'post_action_check', label: '处理后复核', minWidth: 360 },
    { key: 'ledger_key', label: 'Ledger Key', minWidth: 280 },
    { key: 'fallback', label: '失败退路', minWidth: 280 },
    {
      key: 'actions',
      label: '处理',
      width: 430,
      render: (row: Row) => {
        const reviewRow = taskToReviewRow(row)
        if (row.review_scope === 'first_live_decision') {
          return (
            <Btn tone="warning" loading={v.reviewLoading} onClick={() => a.runReadinessReview()}>
              重跑审计
            </Btn>
          )
        }
        if (row.review_scope === 'formal_action') {
          return (
            <div className="flex gap-1.5">
              <Btn tone="primary" loading={formalActionRowLoading(reviewRow, v)} onClick={() => a.handleFormalReviewBoardAction(reviewRow)}>
                {formalActionButtonLabel(reviewRow)}
              </Btn>
              <Btn tone="success" loading={formalActionRowLoading(reviewRow, v)} onClick={() => a.saveFormalActionReview(reviewRow, 'manual_done')}>
                已处理
              </Btn>
              <Btn tone="danger" loading={formalActionRowLoading(reviewRow, v)} onClick={() => a.saveFormalActionReview(reviewRow, 'blocked')}>
                卡住
              </Btn>
            </div>
          )
        }
        if (row.review_scope === 'no_trade_day') {
          return (
            <div className="flex gap-1.5">
              <Btn tone="success" onClick={() => a.saveNoTradeDayReview(reviewRow, 'natural_no_trade')}>
                空仓合理
              </Btn>
              <Btn tone="warning" onClick={() => a.saveNoTradeDayReview(reviewRow, 'missed_opportunity')}>
                可能误杀
              </Btn>
              <Btn tone="danger" onClick={() => a.saveNoTradeDayReview(reviewRow, 'process_gap')}>
                流程缺口
              </Btn>
            </div>
          )
        }
        if (row.review_scope === 'candidate_omission') {
          return (
            <div className="flex gap-1.5">
              <Btn tone="success" onClick={() => a.savePaperWatchFollowup(reviewRow, 'as_expected')}>
                挡得合理
              </Btn>
              <Btn tone="warning" onClick={() => a.savePaperWatchFollowup(reviewRow, 'missed_opportunity')}>
                可能误杀
              </Btn>
              <Btn tone="info" onClick={() => a.savePaperWatchFollowup(reviewRow, 'continue_watch')}>
                继续观察
              </Btn>
            </div>
          )
        }
        return <span className="text-xs text-zinc-500 dark:text-zinc-400">按任务队列处理</span>
      }
    }
  ]

  const sequenceCols: Col[] = [
    { key: 'step', label: '#', width: 56 },
    { key: 'action_label', label: '执行步骤', minWidth: 190 },
    { key: 'blocker_count', label: '合并阻断', width: 92, align: 'right' },
    {
      key: 'can_execute_now',
      label: '当前可做',
      width: 96,
      render: (row: Row) => (
        <Tag tone={truthy(row.can_execute_now) ? 'success' : 'info'}>
          {truthy(row.can_execute_now) ? '可执行' : '等前置'}
        </Tag>
      )
    },
    { key: 'object', label: '覆盖对象', minWidth: 220 },
    { key: 'evidence_statuses', label: '证据状态', width: 150 },
    { key: 'expected_effect', label: '预期效果', minWidth: 300 },
    { key: 'stop_if_fail', label: '失败边界', minWidth: 300 },
    { key: 'completion_check', label: '完成检查', minWidth: 320 },
    { key: 'natural_trade_boundary', label: '自然交易边界', minWidth: 320 },
    {
      key: 'actions',
      label: '执行',
      width: 260,
      render: (row: Row) => {
        if (row.action_group === 'sync_broker_holding_price') {
          return (
            <Btn tone="primary" disabled={!truthy(row.can_execute_now)} loading={v.brokerSyncLoading} onClick={() => a.handlePremarketSequenceAction(row)}>
              同步并复审
            </Btn>
          )
        }
        if (row.action_group === 'rerun_readiness_audit') {
          return (
            <Btn tone="warning" disabled={!truthy(row.can_execute_now)} loading={v.reviewLoading} onClick={() => a.handlePremarketSequenceAction(row)}>
              重跑审计
            </Btn>
          )
        }
        if (row.action_group === 'review_no_trade_context') {
          return (
            <div className="flex gap-1.5">
              <Btn tone="success" disabled={!truthy(row.can_execute_now)} onClick={() => a.saveNoTradeDayReview(taskToReviewRow(row), 'natural_no_trade')}>
                空仓合理
              </Btn>
              <Btn tone="danger" disabled={!truthy(row.can_execute_now)} onClick={() => a.saveNoTradeDayReview(taskToReviewRow(row), 'process_gap')}>
                流程缺口
              </Btn>
            </div>
          )
        }
        return (
          <Btn
            tone="success"
            disabled={!truthy(row.can_execute_now)}
            loading={formalActionRowLoading(taskToReviewRow(row), v)}
            onClick={() => a.saveFormalActionReview(taskToReviewRow(row), 'manual_done')}
          >
            已处理
          </Btn>
        )
      }
    }
  ]

  const resolutionCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'resolution_type', label: '处理路径', width: 190 },
    { key: 'execution_owner', label: '执行归属', width: 190 },
    {
      key: 'can_auto_trigger',
      label: '可触发',
      width: 90,
      render: (row: Row) => (
        <Tag tone={truthy(row.can_auto_trigger) ? 'success' : 'info'}>
          {truthy(row.can_auto_trigger) ? '可' : '人工'}
        </Tag>
      )
    },
    {
      key: 'requires_manual_confirmation',
      label: '人工确认',
      width: 96,
      render: (row: Row) => (
        <Tag tone={truthy(row.requires_manual_confirmation) ? 'warning' : 'info'}>
          {truthy(row.requires_manual_confirmation) ? '需要' : '不需要'}
        </Tag>
      )
    },
    { key: 'object', label: '对象', minWidth: 170 },
    { key: 'action', label: '阻断动作', minWidth: 300 },
    { key: 'recommended_ui_action', label: '页面入口', minWidth: 240 },
    { key: 'recommended_api_action', label: '接口入口', minWidth: 300 },
    { key: 'evidence_required', label: '证据要求', minWidth: 300 },
    { key: 'completion_check', label: '完成检查', minWidth: 320 },
    { key: 'fallback', label: '失败退路', minWidth: 300 },
    { key: 'natural_trade_boundary', label: '自然交易边界', minWidth: 320 },
    {
      key: 'actions',
      label: '处理',
      width: 300,
      render: (row: Row) => {
        if (row.resolution_type === 'broker_holding_price_refresh') {
          return (
            <Btn tone="primary" loading={v.brokerSyncLoading} onClick={() => a.handleBlockerResolutionAction(row)}>
              同步并复审
            </Btn>
          )
        }
        if (row.resolution_type === 'rerun_readiness_audit') {
          return (
            <Btn tone="warning" loading={v.reviewLoading} onClick={() => a.handleBlockerResolutionAction(row)}>
              重跑审计
            </Btn>
          )
        }
        if (row.resolution_type === 'no_trade_context_review') {
          return (
            <div className="flex gap-1.5">
              <Btn tone="success" onClick={() => a.saveNoTradeDayReview(taskToReviewRow(row), 'natural_no_trade')}>
                空仓合理
              </Btn>
              <Btn tone="danger" onClick={() => a.saveNoTradeDayReview(taskToReviewRow(row), 'process_gap')}>
                流程缺口
              </Btn>
            </div>
          )
        }
        return (
          <div className="flex gap-1.5">
            <Btn
              tone="success"
              loading={formalActionRowLoading(taskToReviewRow(row), v)}
              onClick={() => a.saveFormalActionReview(taskToReviewRow(row), 'manual_done')}
            >
              已处理
            </Btn>
            <Btn
              tone="danger"
              loading={formalActionRowLoading(taskToReviewRow(row), v)}
              onClick={() => a.saveFormalActionReview(taskToReviewRow(row), 'blocked')}
            >
              卡住
            </Btn>
          </div>
        )
      }
    }
  ]

  const evidenceCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    {
      key: 'evidence_status',
      label: '证据状态',
      width: 118,
      render: (row: Row) => (
        <Tag tone={reviewBoardStatusType(row.evidence_status)}>{reviewBoardStatusLabel(row.evidence_status)}</Tag>
      )
    },
    { key: 'resolution_type', label: '处理路径', width: 190 },
    { key: 'object', label: '对象', minWidth: 170 },
    { key: 'action', label: '阻断动作', minWidth: 260 },
    { key: 'review_result_label', label: '复盘结果', width: 140 },
    { key: 'issue_area', label: '问题域', width: 150 },
    { key: 'evidence_required', label: '证据要求', minWidth: 280 },
    { key: 'review_note', label: '人工证据', minWidth: 280 },
    { key: 'recommended_ui_action', label: '推荐入口', minWidth: 220 },
    { key: 'next_action', label: '下一步', minWidth: 320 },
    { key: 'completion_check', label: '完成检查', minWidth: 320 },
    { key: 'natural_trade_boundary', label: '自然交易边界', minWidth: 320 },
    {
      key: 'actions',
      label: '证据处理',
      width: 390,
      render: (row: Row) => {
        if (row.review_scope === 'no_trade_day') {
          return (
            <div className="flex gap-1.5">
              <Btn tone="success" onClick={() => a.saveNoTradeDayReview(taskToReviewRow(row), 'natural_no_trade')}>
                空仓合理
              </Btn>
              <Btn tone="danger" onClick={() => a.saveNoTradeDayReview(taskToReviewRow(row), 'process_gap')}>
                流程缺口
              </Btn>
            </div>
          )
        }
        return (
          <div className="flex gap-1.5">
            <Btn
              tone="success"
              loading={formalActionRowLoading(taskToReviewRow(row), v)}
              onClick={() => a.saveFormalActionReview(taskToReviewRow(row), 'manual_done')}
            >
              已验证
            </Btn>
            <Btn
              tone="danger"
              loading={formalActionRowLoading(taskToReviewRow(row), v)}
              onClick={() => a.saveFormalActionReview(taskToReviewRow(row), 'blocked')}
            >
              发现隐患
            </Btn>
            <Btn
              tone="warning"
              loading={formalActionRowLoading(taskToReviewRow(row), v)}
              onClick={() => a.saveFormalActionReview(taskToReviewRow(row), 'continue_watch')}
            >
              继续观察
            </Btn>
          </div>
        )
      }
    }
  ]

  return (
    <Panel
      className="mt-5"
      title="实战前逐票复盘"
      desc="结合下一交易日票据、准入阻断与已平仓归因，逐票确认是否具备可执行性。"
      actions={<Badge color={verdictTone as any}>{s.verdict || '未生成'}</Badge>}
    >
      <StatGrid items={gridItems} />

      <DataTable rows={v.liveLearningLedgerRows} cols={ledgerCols} empty="暂无实战学习隐患" />
      <DataTable rows={v.livePremarketExecutionRecheckRows} cols={recheckCols} empty="暂无盘前执行后复查" />
      <DataTable rows={v.livePremarketActionAttemptRows} cols={attemptCols} empty="暂无盘前动作尝试" />
      <DataTable rows={v.liveManualLaunchAcceptanceRows} cols={acceptanceCols} empty="暂无人工实战验收单" />
      <DataTable rows={v.liveDay1ReviewJournalRows} cols={journalCols} empty="暂无Day1实战复盘日志" />
      <DataTable rows={v.livePremarketCommandRows} cols={commandCols} empty="暂无实战盘前指挥单" />
      <DataTable rows={v.livePremarketActionSequenceRows} cols={sequenceCols} empty="暂无盘前执行顺序" />
      <DataTable rows={v.liveBlockerResolutionRows} cols={resolutionCols} empty="暂无实盘阻断处理路线图" />
      <DataTable rows={v.liveBlockerEvidenceRows} cols={evidenceCols} empty="暂无实盘阻断证据闭环" />

      <ReadinessPanelB />
    </Panel>
  )
}
