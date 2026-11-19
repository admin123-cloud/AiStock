// 实战前逐票复盘 · 中（任务队列 / 覆盖率 / 证据矩阵 / 放行清单 / 执行入口 / 放行队列 / 盘前剧本 / 每日看板 / 行动层 / 执行清单 / 隐患登记 / 学习队列）
import { Card } from '@/components/ui'
import { Btn, DataTable, Tag, useRisk } from './shared'
import type { Col } from './shared'
import {
  dailyReviewChecklistRowLoading,
  executionModeType,
  formalActionButtonLabel,
  formalActionRowLoading,
  gateType,
  hiddenRiskLevelLabel,
  hiddenRiskLevelType,
  learningStatusLabel,
  learningStatusType,
  pct,
  reviewAxisLabel,
  reviewBoardStatusLabel,
  reviewBoardStatusType,
  taskToReviewRow,
  truthy,
  type Row
} from './labels'
import ReadinessPanelC from './ReadinessPanelC'

export default function ReadinessPanelB() {
  const { v, a } = useRisk()

  const taskQueueCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'window', label: '窗口', width: 104 },
    { key: 'task_type', label: '任务', width: 132 },
    {
      key: 'task_status',
      label: '状态',
      width: 130,
      render: (row: Row) => (
        <Tag tone={reviewBoardStatusType(row.task_status)}>{reviewBoardStatusLabel(row.task_status)}</Tag>
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
    { key: 'code', label: '代码', width: 110 },
    { key: 'name', label: '名称', minWidth: 110 },
    { key: 'object', label: '对象', minWidth: 170 },
    { key: 'action', label: '动作', minWidth: 340 },
    { key: 'acceptance', label: '验收条件', minWidth: 320 },
    { key: 'review_method', label: '复盘入口', minWidth: 320 },
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
        if (row.review_scope === 'day1_after_close' || row.review_scope === 'day1_paper_pack') {
          return (
            <div className="flex gap-1.5">
              <Btn tone="success" onClick={() => a.savePaperWatchFollowup(reviewRow, 'as_expected')}>
                符合预期
              </Btn>
              <Btn tone="warning" onClick={() => a.savePaperWatchFollowup(reviewRow, 'buy_point_chasing')}>
                买点偏急
              </Btn>
              <Btn tone="danger" onClick={() => a.savePaperWatchFollowup(reviewRow, 'risk_exit_issue')}>
                卖点/风控
              </Btn>
            </div>
          )
        }
        return <span className="text-xs text-zinc-500 dark:text-zinc-400">按下方看板处理</span>
      }
    }
  ]

  const coverageCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'review_scope', label: '复盘范围', width: 170 },
    { key: 'task_type', label: '任务类型', width: 160 },
    {
      key: 'coverage_status',
      label: '覆盖状态',
      width: 126,
      render: (row: Row) => (
        <Tag tone={reviewBoardStatusType(row.coverage_status)}>{reviewBoardStatusLabel(row.coverage_status)}</Tag>
      )
    },
    { key: 'total_count', label: '任务数', width: 82, align: 'right' },
    { key: 'covered_count', label: '已覆盖', width: 82, align: 'right' },
    { key: 'pending_count', label: '待复盘', width: 82, align: 'right' },
    { key: 'issue_found_count', label: '隐患', width: 76, align: 'right' },
    { key: 'continue_watch_count', label: '观察', width: 76, align: 'right' },
    { key: 'formal_required_count', label: '正式必处理', width: 104, align: 'right' },
    {
      key: 'coverage_pct',
      label: '覆盖率',
      width: 92,
      align: 'right',
      render: (row: Row) => pct(row.coverage_pct)
    },
    { key: 'next_action', label: '下一步', minWidth: 360 }
  ]

  const rubricCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'review_scope', label: '复盘范围', width: 160 },
    { key: 'task_type', label: '任务类型', width: 150 },
    { key: 'review_axes', label: '复盘轴', minWidth: 190 },
    { key: 'task_count', label: '任务', width: 70, align: 'right' },
    { key: 'pending_count', label: '待复盘', width: 82, align: 'right' },
    { key: 'formal_required_count', label: '正式必处理', width: 104, align: 'right' },
    { key: 'evidence_required', label: '证据要求', minWidth: 360 },
    { key: 'decision_rule', label: '判断规则', minWidth: 340 },
    { key: 'optimization_boundary', label: '优化边界', minWidth: 360 },
    { key: 'promotion_rule', label: '升级条件', minWidth: 340 },
    { key: 'next_action', label: '下一步', minWidth: 320 }
  ]

  const formalLaunchCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'item', label: '检查项', minWidth: 180 },
    {
      key: 'status',
      label: '状态',
      width: 86,
      render: (row: Row) => (
        <Tag tone={gateType(row.status)}>{row.status || '--'}</Tag>
      )
    },
    { key: 'evidence', label: '证据', minWidth: 240 },
    { key: 'required_action', label: '缺口动作', minWidth: 320 }
  ]

  const executionModeCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'mode', label: '入口', minWidth: 150 },
    {
      key: 'status',
      label: '状态',
      width: 110,
      render: (row: Row) => (
        <Tag tone={executionModeType(row.status, row.allowed_now)}>{row.status || '--'}</Tag>
      )
    },
    {
      key: 'allowed_now',
      label: '当前允许',
      width: 96,
      render: (row: Row) => (
        <Tag tone={truthy(row.allowed_now) ? 'success' : 'info'}>
          {truthy(row.allowed_now) ? '允许' : '不允许'}
        </Tag>
      )
    },
    { key: 'evidence', label: '证据', minWidth: 320 },
    { key: 'required_before_use', label: '使用前要求', minWidth: 320 },
    { key: 'failure_mode', label: '误用风险', minWidth: 260 }
  ]

  const formalActionCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'stage', label: '阶段', width: 150 },
    {
      key: 'severity',
      label: '级别',
      width: 86,
      render: (row: Row) => (
        <Tag tone={gateType(row.severity)}>{row.severity || '--'}</Tag>
      )
    },
    { key: 'object', label: '对象', minWidth: 160 },
    { key: 'action', label: '动作', minWidth: 360 },
    { key: 'unlocks', label: '解锁', minWidth: 260 },
    { key: 'source', label: '来源', width: 180 }
  ]

  const playbookCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'window', label: '窗口', width: 120 },
    { key: 'step', label: '步骤', minWidth: 160 },
    { key: 'action', label: '动作', minWidth: 340 },
    { key: 'pass_condition', label: '通过条件', minWidth: 280 },
    { key: 'fallback_if_fail', label: '失败退路', minWidth: 260 },
    { key: 'source', label: '来源', width: 190 }
  ]

  const dailyBoardCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'stage', label: '阶段', width: 126 },
    { key: 'review_scope', label: '复盘范围', width: 150 },
    {
      key: 'review_status',
      label: '状态',
      width: 126,
      render: (row: Row) => (
        <Tag tone={reviewBoardStatusType(row.review_status)}>{reviewBoardStatusLabel(row.review_status)}</Tag>
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
    { key: 'code', label: '代码', width: 110 },
    { key: 'name', label: '名称', minWidth: 110 },
    { key: 'entry_date', label: '交易日', width: 104 },
    { key: 'object', label: '对象', minWidth: 180 },
    { key: 'required_action', label: '必须动作', minWidth: 340 },
    { key: 'evidence', label: '证据', minWidth: 320 },
    { key: 'natural_decision', label: '自然决策/观察', minWidth: 300 },
    { key: 'optimization_focus', label: '学习重点', minWidth: 340 },
    { key: 'source', label: '来源', width: 190 },
    {
      key: 'actions',
      label: '处理复盘项',
      width: 430,
      render: (row: Row) => {
        if (row.review_scope === 'no_trade_day') {
          return (
            <div className="flex flex-wrap gap-1.5">
              <Btn tone="success" onClick={() => a.saveNoTradeDayReview(row, 'natural_no_trade')}>空仓合理</Btn>
              <Btn tone="warning" onClick={() => a.saveNoTradeDayReview(row, 'missed_opportunity')}>可能误杀</Btn>
              <Btn tone="warning" onClick={() => a.saveNoTradeDayReview(row, 'data_gap')}>数据缺口</Btn>
              <Btn tone="danger" onClick={() => a.saveNoTradeDayReview(row, 'process_gap')}>流程缺口</Btn>
              <Btn tone="info" onClick={() => a.saveNoTradeDayReview(row, 'continue_watch')}>继续观察</Btn>
            </div>
          )
        }
        if (row.review_scope === 'candidate_omission') {
          return (
            <div className="flex flex-wrap gap-1.5">
              <Btn tone="success" onClick={() => a.savePaperWatchFollowup(row, 'as_expected')}>挡得合理</Btn>
              <Btn tone="warning" onClick={() => a.savePaperWatchFollowup(row, 'missed_opportunity')}>可能误杀</Btn>
              <Btn tone="danger" onClick={() => a.savePaperWatchFollowup(row, 'invalid_signal')}>信号失效</Btn>
              <Btn tone="info" onClick={() => a.savePaperWatchFollowup(row, 'continue_watch')}>继续观察</Btn>
            </div>
          )
        }
        if (row.review_scope === 'day1_after_close' || row.review_scope === 'day1_paper_pack') {
          return (
            <div className="flex flex-wrap gap-1.5">
              <Btn tone="success" onClick={() => a.savePaperWatchFollowup(row, 'as_expected')}>符合预期</Btn>
              <Btn tone="warning" onClick={() => a.savePaperWatchFollowup(row, 'buy_point_chasing')}>买点偏急</Btn>
              <Btn tone="warning" onClick={() => a.savePaperWatchFollowup(row, 'selection_issue')}>选股隐患</Btn>
              <Btn tone="danger" onClick={() => a.savePaperWatchFollowup(row, 'risk_exit_issue')}>卖点/风控</Btn>
              <Btn tone="info" onClick={() => a.savePaperWatchFollowup(row, 'continue_watch')}>继续观察</Btn>
            </div>
          )
        }
        if (row.review_scope === 'formal_action') {
          return (
            <div className="flex gap-1.5">
              <Btn tone="primary" loading={formalActionRowLoading(row, v)} onClick={() => a.handleFormalReviewBoardAction(row)}>
                {formalActionButtonLabel(row)}
              </Btn>
              <Btn tone="success" loading={formalActionRowLoading(row, v)} onClick={() => a.saveFormalActionReview(row, 'manual_done')}>
                已处理
              </Btn>
              <Btn tone="danger" loading={formalActionRowLoading(row, v)} onClick={() => a.saveFormalActionReview(row, 'blocked')}>
                卡住
              </Btn>
            </div>
          )
        }
        return <span className="text-xs text-zinc-500 dark:text-zinc-400">按来源处理</span>
      }
    }
  ]

  const actionLayerCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'layer_label', label: '行动层', width: 150 },
    { key: 'review_window', label: '窗口', width: 110 },
    { key: 'checkpoint_time', label: '检查时间', width: 160 },
    { key: 'item_count', label: '处理项', width: 82, align: 'right' },
    { key: 'pending_count', label: '待处理', width: 82, align: 'right' },
    { key: 'high_risk_count', label: '高风险', width: 82, align: 'right' },
    { key: 'review_axes', label: '复盘轴', minWidth: 190 },
    { key: 'first_action', label: '首要动作', minWidth: 320 },
    { key: 'buy_permission_effect', label: '买入影响', minWidth: 280 },
    { key: 'operator_instruction', label: '操作说明', minWidth: 360 }
  ]

  const executionChecklistCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'review_window', label: '窗口', width: 110 },
    { key: 'checkpoint_time', label: '检查时间', width: 170 },
    {
      key: 'review_axis',
      label: '复盘轴',
      width: 140,
      render: (row: Row) => reviewAxisLabel(row.review_axis)
    },
    {
      key: 'risk_level',
      label: '风险',
      width: 88,
      render: (row: Row) => (
        <Tag tone={hiddenRiskLevelType(row.risk_level)}>{hiddenRiskLevelLabel(row.risk_level)}</Tag>
      )
    },
    {
      key: 'watch_status',
      label: '状态',
      width: 120,
      render: (row: Row) => (
        <Tag tone={reviewBoardStatusType(row.watch_status)}>{reviewBoardStatusLabel(row.watch_status)}</Tag>
      )
    },
    { key: 'review_scope', label: '范围', width: 160 },
    {
      key: 'review_status',
      label: '复盘状态',
      width: 126,
      render: (row: Row) => (
        <Tag tone={reviewBoardStatusType(row.review_status)}>{reviewBoardStatusLabel(row.review_status)}</Tag>
      )
    },
    {
      key: 'actions',
      label: '复盘落账',
      width: 310,
      render: (row: Row) => (
        <div className="flex flex-wrap gap-1.5">
          <Btn tone="success" loading={dailyReviewChecklistRowLoading(row, v)} onClick={() => a.saveDailyReviewChecklistReview(row, 'validated')}>
            符合预期
          </Btn>
          <Btn tone="danger" loading={dailyReviewChecklistRowLoading(row, v)} onClick={() => a.saveDailyReviewChecklistReview(row, 'issue_found')}>
            发现隐患
          </Btn>
          <Btn tone="warning" loading={dailyReviewChecklistRowLoading(row, v)} onClick={() => a.saveDailyReviewChecklistReview(row, 'continue_watch')}>
            继续观察
          </Btn>
          <Btn tone="info" loading={dailyReviewChecklistRowLoading(row, v)} onClick={() => a.saveDailyReviewChecklistReview(row, 'data_gap')}>
            数据缺口
          </Btn>
        </div>
      )
    },
    { key: 'code', label: '代码', width: 110 },
    { key: 'name', label: '名称', minWidth: 110 },
    { key: 'entry_date', label: '交易日', width: 104 },
    { key: 'object', label: '对象', minWidth: 180 },
    { key: 'evidence_to_collect', label: '要收集的证据', minWidth: 320 },
    { key: 'pass_condition', label: '通过条件', minWidth: 340 },
    { key: 'fail_condition', label: '进入调优条件', minWidth: 340 },
    { key: 'target_action', label: '目标动作', minWidth: 360 },
    { key: 'natural_trade_boundary', label: '自然交易边界', minWidth: 360 }
  ]

  const hiddenRiskCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    {
      key: 'watch_status',
      label: '状态',
      width: 122,
      render: (row: Row) => (
        <Tag tone={reviewBoardStatusType(row.watch_status)}>{reviewBoardStatusLabel(row.watch_status)}</Tag>
      )
    },
    {
      key: 'risk_level',
      label: '风险级别',
      width: 96,
      render: (row: Row) => (
        <Tag tone={hiddenRiskLevelType(row.risk_level)}>{hiddenRiskLevelLabel(row.risk_level)}</Tag>
      )
    },
    { key: 'issue_area', label: '归因', width: 150 },
    { key: 'review_scope', label: '复盘范围', width: 170 },
    { key: 'code', label: '代码', width: 110 },
    { key: 'name', label: '名称', minWidth: 110 },
    { key: 'entry_date', label: '交易日', width: 104 },
    { key: 'object', label: '对象', minWidth: 180 },
    { key: 'risk_signal', label: '风险信号', minWidth: 260 },
    { key: 'evidence_gap', label: '证据缺口', minWidth: 320 },
    { key: 'next_review_action', label: '下一步复盘', minWidth: 340 },
    { key: 'natural_trade_boundary', label: '自然交易边界', minWidth: 360 },
    { key: 'source', label: '来源', width: 210 }
  ]

  const learningCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    {
      key: 'learning_status',
      label: '状态',
      width: 110,
      render: (row: Row) => (
        <Tag tone={learningStatusType(row.learning_status)}>{learningStatusLabel(row.learning_status)}</Tag>
      )
    },
    { key: 'issue_area', label: '归因', width: 150 },
    { key: 'review_scope', label: '来源范围', width: 160 },
    { key: 'code', label: '代码', width: 110 },
    { key: 'name', label: '名称', minWidth: 110 },
    { key: 'entry_date', label: '交易日', width: 104 },
    { key: 'object', label: '对象', minWidth: 180 },
    { key: 'problem_signal', label: '问题信号', minWidth: 180 },
    { key: 'evidence', label: '复盘证据', minWidth: 320 },
    { key: 'suggested_learning', label: '学习/调优重点', minWidth: 360 },
    { key: 'source', label: '来源', width: 190 }
  ]

  return (
    <Card className="mt-6 p-5">
      <h3 className="mb-3 text-left text-base font-semibold text-zinc-900 dark:text-zinc-100">实战复盘队列与看板</h3>
      <DataTable rows={v.liveReviewTaskQueueRows} cols={taskQueueCols} empty="暂无实战复盘任务队列" />
      <DataTable rows={v.reviewCoverageDashboardRows} cols={coverageCols} empty="暂无复盘覆盖率看板" />
      <DataTable rows={v.liveReviewEvidenceRubricRows} cols={rubricCols} empty="暂无实战复盘证据矩阵" />
      <DataTable rows={v.formalLaunchRows} cols={formalLaunchCols} empty="暂无正式放行清单" />
      <DataTable rows={v.executionModeRows} cols={executionModeCols} empty="暂无执行入口矩阵" />
      <DataTable rows={v.formalLaunchActionRows} cols={formalActionCols} empty="暂无正式放行优先队列" />
      <DataTable rows={v.premarketPlaybookRows} cols={playbookCols} empty="暂无盘前执行剧本" />
      <DataTable rows={v.dailyLiveReviewBoardRows} cols={dailyBoardCols} empty="暂无每日实战复盘看板" />
      <DataTable rows={v.liveDailyReviewActionLayerRows} cols={actionLayerCols} empty="暂无实战复盘行动层" />
      <DataTable
        rows={v.liveDailyReviewExecutionChecklistRows}
        cols={executionChecklistCols}
        empty="暂无实战逐项复盘执行清单"
      />
      <DataTable rows={v.liveHiddenRiskWatchlistRows} cols={hiddenRiskCols} empty="暂无实战隐患观察登记" />
      <DataTable rows={v.strategyLearningBacklogRows} cols={learningCols} empty="暂无策略学习隐患队列" />
    </Card>
  )
}
