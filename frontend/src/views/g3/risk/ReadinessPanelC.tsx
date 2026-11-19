// 实战前逐票复盘 · 下（无票日 / Day1 纸面 / Day1 盘后 / 逐票证据 / 自然一致性 / 自然执行 / 逐票复盘与台账 / 纸面后评估 / 动作清单 / 底稿 / 持仓刷新与退出 / 候选遗漏 / Gate+隐患）
import { Card } from '@/components/ui'
import { Btn, DataTable, Tag, useRisk } from './shared'
import type { Col } from './shared'
import {
  afterCloseStatusLabel,
  afterCloseStatusType,
  day1PostureLabel,
  day1PostureType,
  gateType,
  holdingRefreshType,
  isReviewSaving,
  naturalConsistencyLabel,
  naturalConsistencyType,
  noTradeReviewResultLabel,
  noTradeReviewStatusType,
  paperWatchResultLabel,
  paperWatchStatusType,
  pretradeReviewType,
  price,
  truthy,
  type Row
} from './labels'

export default function ReadinessPanelC() {
  const { v, a } = useRisk()

  const noTradeCols: Col[] = [
    { key: 'rank', label: '#', width: 56 },
    { key: 'entry_date', label: '交易日', width: 104 },
    { key: 'review_type', label: '复盘类型', minWidth: 180 },
    { key: 'posture', label: '姿态', width: 150 },
    {
      key: 'review_status',
      label: '复盘结论',
      width: 130,
      render: (row: Row) => (
        <Tag tone={noTradeReviewStatusType(row.review_status)}>
          {row.review_result_label || noTradeReviewResultLabel(row.review_result) || '待复盘'}
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
    { key: 'evidence', label: '证据', minWidth: 340 },
    { key: 'hidden_risk', label: '隐藏风险', minWidth: 340 },
    { key: 'natural_decision', label: '自然决策', minWidth: 320 },
    { key: 'next_action', label: '下一步', minWidth: 340 },
    { key: 'review_note', label: '人工说明', minWidth: 240 },
    { key: 'source', label: '来源', width: 190 },
    {
      key: 'actions',
      label: '记录无票复盘',
      width: 430,
      render: (row: Row) => (
        <div className="flex flex-wrap gap-1.5">
          <Btn tone="success" onClick={() => a.saveNoTradeDayReview(row, 'natural_no_trade')}>空仓合理</Btn>
          <Btn tone="warning" onClick={() => a.saveNoTradeDayReview(row, 'missed_opportunity')}>可能误杀</Btn>
          <Btn tone="warning" onClick={() => a.saveNoTradeDayReview(row, 'data_gap')}>数据缺口</Btn>
          <Btn tone="danger" onClick={() => a.saveNoTradeDayReview(row, 'process_gap')}>流程缺口</Btn>
          <Btn tone="info" onClick={() => a.saveNoTradeDayReview(row, 'continue_watch')}>继续观察</Btn>
        </div>
      )
    }
  ]

  const day1PaperCols: Col[] = [
    { key: 'rank', label: '#', width: 56 },
    { key: 'code', label: '代码', width: 110 },
    { key: 'name', label: '名称', minWidth: 110 },
    { key: 'entry_date', label: '入场日', width: 104 },
    {
      key: 'launch_posture',
      label: '启动姿态',
      width: 150,
      render: (row: Row) => (
        <Tag tone={day1PostureType(row.launch_posture)}>{day1PostureLabel(row.launch_posture)}</Tag>
      )
    },
    { key: 'buy_point_review', label: '买点复盘', minWidth: 300 },
    { key: 'selection_review', label: '选股复盘', minWidth: 300 },
    { key: 'model_switch_review', label: '策略切换', minWidth: 300 },
    { key: 'exit_contract_review', label: '卖点合同', minWidth: 300 },
    { key: 'hidden_risk_focus', label: '隐患重点', minWidth: 340 },
    { key: 'after_close_required_note', label: '盘后归因', minWidth: 360 },
    { key: 'next_action', label: '下一步', minWidth: 320 }
  ]

  const day1AfterCols: Col[] = [
    { key: 'rank', label: '#', width: 56 },
    { key: 'code', label: '代码', width: 110 },
    { key: 'name', label: '名称', minWidth: 110 },
    { key: 'entry_date', label: '入场日', width: 104 },
    {
      key: 'after_close_status',
      label: '盘后状态',
      width: 150,
      render: (row: Row) => (
        <Tag tone={afterCloseStatusType(row.after_close_status)}>
          {afterCloseStatusLabel(row.after_close_status)}
        </Tag>
      )
    },
    {
      key: 'execution_price',
      label: '纸面价',
      width: 92,
      align: 'right',
      render: (row: Row) => price(row.execution_price)
    },
    { key: 'quantity', label: '数量', width: 82, align: 'right' },
    { key: 'watch_result_label', label: '观察结果', minWidth: 130 },
    { key: 'issue_area', label: '归因', width: 120 },
    { key: 'hidden_risk_focus', label: '隐患重点', minWidth: 320 },
    { key: 'review_note', label: '复盘说明', minWidth: 260 },
    { key: 'next_action', label: '下一步', minWidth: 320 },
    {
      key: 'actions',
      label: '记录盘后结果',
      width: 430,
      render: (row: Row) => (
        <div className="flex flex-wrap gap-1.5">
          <Btn tone="success" onClick={() => a.savePaperWatchFollowup(row, 'as_expected')}>符合预期</Btn>
          <Btn tone="warning" onClick={() => a.savePaperWatchFollowup(row, 'buy_point_chasing')}>买点偏急</Btn>
          <Btn tone="warning" onClick={() => a.savePaperWatchFollowup(row, 'selection_issue')}>选股隐患</Btn>
          <Btn tone="danger" onClick={() => a.savePaperWatchFollowup(row, 'risk_exit_issue')}>卖点/风控</Btn>
          <Btn tone="info" onClick={() => a.savePaperWatchFollowup(row, 'continue_watch')}>继续观察</Btn>
        </div>
      )
    }
  ]

  const pretradeEvidenceCols: Col[] = [
    { key: 'rank', label: '#', width: 56 },
    { key: 'code', label: '代码', width: 110 },
    { key: 'name', label: '名称', minWidth: 110 },
    {
      key: 'review_action',
      label: '复盘',
      width: 120,
      render: (row: Row) => (
        <Tag tone={pretradeReviewType(row.review_action)}>
          {row.review_label || row.review_action || '未复盘'}
        </Tag>
      )
    },
    {
      key: 'risk_acknowledged',
      label: '风险确认',
      width: 96,
      render: (row: Row) => (
        <Tag tone={truthy(row.risk_acknowledged) ? 'success' : 'info'}>
          {truthy(row.risk_acknowledged) ? '已确认' : '未确认'}
        </Tag>
      )
    },
    {
      key: 'confirmation_complete',
      label: '确认项',
      width: 96,
      render: (row: Row) => (
        <Tag tone={truthy(row.confirmation_complete) ? 'success' : 'warning'}>
          {truthy(row.confirmation_complete) ? '完整' : '缺失'}
        </Tag>
      )
    },
    {
      key: 'formal_ready',
      label: '正式就绪',
      width: 96,
      render: (row: Row) => (
        <Tag tone={truthy(row.formal_ready) ? 'success' : 'warning'}>
          {truthy(row.formal_ready) ? '就绪' : '未就绪'}
        </Tag>
      )
    },
    { key: 'formal_ready_reason', label: '原因', minWidth: 220 },
    { key: 'missing_confirmation_items', label: '缺失确认项', minWidth: 260 },
    { key: 'review_decision_reason', label: '复盘原因', minWidth: 360 },
    { key: 'next_review_trigger', label: '下次触发', minWidth: 300 },
    { key: 'portfolio_decision', label: '组合决策', minWidth: 220 },
    { key: 'next_action', label: '下一步', minWidth: 320 }
  ]

  const naturalConsistencyCols: Col[] = [
    { key: 'rank', label: '#', width: 56 },
    { key: 'code', label: '代码', width: 110 },
    { key: 'name', label: '名称', minWidth: 110 },
    { key: 'consistency_score', label: '一致性分', width: 96 },
    { key: 'operational_readiness_score', label: '就绪分', width: 82 },
    {
      key: 'consistency_grade',
      label: '等级',
      width: 96,
      render: (row: Row) => (
        <Tag tone={naturalConsistencyType(row.consistency_grade)}>
          {naturalConsistencyLabel(row.consistency_grade)}
        </Tag>
      )
    },
    { key: 'buy_point_logic', label: '买点逻辑', minWidth: 280 },
    { key: 'sell_point_logic', label: '卖点合同', minWidth: 260 },
    { key: 'selection_logic', label: '选股逻辑', minWidth: 240 },
    { key: 'model_switch_logic', label: '策略切换', minWidth: 260 },
    { key: 'deductions', label: '逻辑扣分', minWidth: 320 },
    { key: 'readiness_gaps', label: '就绪缺口', minWidth: 260 },
    { key: 'recommended_action', label: '建议动作', minWidth: 340 }
  ]

  const naturalExecutionCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'code', label: '代码', width: 110 },
    { key: 'name', label: '名称', minWidth: 110 },
    {
      key: 'decision_level',
      label: '等级',
      width: 86,
      render: (row: Row) => <Tag tone={gateType(row.decision_level)}>{row.decision_level || '--'}</Tag>
    },
    { key: 'execution_posture', label: '执行姿态', minWidth: 170 },
    {
      key: 'manual_live_allowed_after_review',
      label: '人工实盘',
      width: 96,
      render: (row: Row) => (
        <Tag tone={truthy(row.manual_live_allowed_after_review) ? 'success' : 'info'}>
          {truthy(row.manual_live_allowed_after_review) ? '可候选' : '未允许'}
        </Tag>
      )
    },
    { key: 'required_confirmation', label: '必须确认', minWidth: 420 },
    { key: 'downgrade_rule', label: '降级规则', minWidth: 320 },
    { key: 'source_deductions', label: '来源扣分', minWidth: 300 }
  ]

  const reviewTicketCols: Col[] = [
    { key: 'code', label: '代码', width: 110 },
    { key: 'name', label: '名称', minWidth: 110 },
    { key: 'entry_date', label: '入场日', width: 104 },
    { key: 'strategy', label: '策略', minWidth: 130 },
    {
      key: 'natural_action',
      label: '自然纪律',
      width: 120,
      render: (row: Row) => row.natural_action_label || row.natural_action || '--'
    },
    {
      key: 'pretrade_review_action',
      label: '人工复盘',
      width: 128,
      render: (row: Row) => (
        <Tag tone={pretradeReviewType(row.pretrade_review_action)}>
          {row.pretrade_review_label || '未复盘'}
        </Tag>
      )
    },
    { key: 'warnings', label: '警告', minWidth: 220 },
    {
      key: 'actions',
      label: '记录',
      width: 330,
      render: (row: Row) => (
        <div className="flex flex-wrap gap-1.5">
          <Btn tone="primary" loading={isReviewSaving(row, v)} onClick={() => a.savePretradeReview(row, 'paper_watch')}>
            纸面观察
          </Btn>
          <Btn tone="success" loading={isReviewSaving(row, v)} onClick={() => a.savePretradeReview(row, 'manual_approved')}>
            人工放行
          </Btn>
          <Btn tone="warning" loading={isReviewSaving(row, v)} onClick={() => a.savePretradeReview(row, 'wait_refresh')}>
            待刷新
          </Btn>
          <Btn tone="danger" loading={isReviewSaving(row, v)} onClick={() => a.savePretradeReview(row, 'skip')}>
            跳过
          </Btn>
        </div>
      )
    }
  ]

  const reviewLedgerCols: Col[] = [
    { key: 'updated_at', label: '记录时间', width: 170 },
    { key: 'code', label: '代码', width: 110 },
    { key: 'name', label: '名称', minWidth: 110 },
    { key: 'entry_date', label: '入场日', width: 104 },
    {
      key: 'review_action',
      label: '结论',
      width: 120,
      render: (row: Row) => (
        <Tag tone={pretradeReviewType(row.review_action)}>{row.review_label || row.review_action || '--'}</Tag>
      )
    },
    {
      key: 'formal_ready',
      label: '正式就绪',
      width: 96,
      render: (row: Row) => {
        const ready =
          truthy(row.risk_acknowledged) && truthy(row.confirmation_complete) && row.review_action === 'manual_approved'
        return <Tag tone={ready ? 'success' : 'info'}>{ready ? '就绪' : '未就绪'}</Tag>
      }
    },
    { key: 'review_decision_reason', label: '复盘依据', minWidth: 360 },
    { key: 'next_review_trigger', label: '下次触发', minWidth: 300 },
    { key: 'note', label: '备注', minWidth: 260 }
  ]

  const paperFollowupCols: Col[] = [
    { key: 'updated_at', label: '评估时间', width: 170 },
    { key: 'code', label: '代码', width: 110 },
    { key: 'name', label: '名称', minWidth: 110 },
    { key: 'entry_date', label: '入场日', width: 104 },
    {
      key: 'followup_status',
      label: '评估',
      width: 128,
      render: (row: Row) => (
        <Tag tone={paperWatchStatusType(row.followup_status)}>
          {row.watch_result_label || paperWatchResultLabel(row.watch_result) || '待观察'}
        </Tag>
      )
    },
    { key: 'issue_area', label: '归因', width: 120 },
    { key: 'hidden_risk', label: '发现隐患', minWidth: 260 },
    { key: 'optimization_suggestion', label: '优化建议', minWidth: 300 },
    { key: 'review_note', label: '观察说明', minWidth: 280 },
    {
      key: 'actions',
      label: '记录观察结果',
      width: 430,
      render: (row: Row) => (
        <div className="flex flex-wrap gap-1.5">
          <Btn tone="success" onClick={() => a.savePaperWatchFollowup(row, 'as_expected')}>符合预期</Btn>
          <Btn tone="warning" onClick={() => a.savePaperWatchFollowup(row, 'buy_point_chasing')}>买点偏急</Btn>
          <Btn tone="warning" onClick={() => a.savePaperWatchFollowup(row, 'selection_issue')}>选股隐患</Btn>
          <Btn tone="danger" onClick={() => a.savePaperWatchFollowup(row, 'risk_exit_issue')}>卖点/风控</Btn>
          <Btn tone="info" onClick={() => a.savePaperWatchFollowup(row, 'continue_watch')}>继续观察</Btn>
        </div>
      )
    }
  ]

  const pretradeActionCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    { key: 'phase', label: '阶段', width: 120 },
    {
      key: 'severity',
      label: '级别',
      width: 86,
      render: (row: Row) => (
        <Tag tone={gateType(row.severity)}>{row.severity || '--'}</Tag>
      )
    },
    {
      key: 'formal_trade_required',
      label: '属性',
      width: 86,
      render: (row: Row) => (
        <Tag tone={truthy(row.formal_trade_required) ? 'danger' : 'info'}>
          {truthy(row.formal_trade_required) ? '必处理' : '观察'}
        </Tag>
      )
    },
    { key: 'object', label: '对象', minWidth: 150 },
    { key: 'action', label: '动作', minWidth: 280 },
    { key: 'done_condition', label: '完成条件', minWidth: 320 },
    { key: 'source', label: '来源', width: 180 }
  ]

  const ticketChecklistCols: Col[] = [
    { key: 'code', label: '代码', width: 110 },
    { key: 'name', label: '名称', minWidth: 110 },
    {
      key: 'checklist_level',
      label: '等级',
      width: 86,
      render: (row: Row) => (
        <Tag tone={gateType(row.checklist_level)}>{row.checklist_level || '--'}</Tag>
      )
    },
    { key: 'buy_point_assessment', label: '买点判断', minWidth: 260 },
    { key: 'exit_plan', label: '卖点合同', minWidth: 260 },
    { key: 'selection_pattern', label: '选股模式', minWidth: 230 },
    {
      key: 'portfolio_decision_level',
      label: '组合决策',
      minWidth: 260,
      render: (row: Row) => (
        <span className="flex items-center gap-2">
          <Tag tone={gateType(row.portfolio_decision_level)}>{row.portfolio_decision_level || '--'}</Tag>
          <span className="text-xs text-zinc-600 dark:text-zinc-300">{row.portfolio_decision || '--'}</span>
        </span>
      )
    },
    { key: 'portfolio_decision_reason', label: '组合原因', minWidth: 320 },
    { key: 'model_switch_assessment', label: '策略切换', minWidth: 230 },
    { key: 'manual_approval_checklist', label: '放行确认', minWidth: 360 },
    { key: 'hidden_risks', label: '隐藏风险', minWidth: 300 },
    { key: 'manual_questions', label: '人工问题', minWidth: 320 },
    { key: 'action_recommendation', label: '动作建议', minWidth: 220 }
  ]

  const holdingRefreshCols: Col[] = [
    { key: 'rank', label: '#', width: 56 },
    { key: 'source', label: '来源', width: 120 },
    { key: 'code', label: '代码', width: 110 },
    { key: 'name', label: '名称', minWidth: 110 },
    {
      key: 'formal_trade_required',
      label: '属性',
      width: 96,
      render: (row: Row) => (
        <Tag tone={truthy(row.formal_trade_required) ? 'danger' : 'info'}>
          {truthy(row.formal_trade_required) ? '必刷新' : '观察'}
        </Tag>
      )
    },
    {
      key: 'refresh_status',
      label: '状态',
      width: 150,
      render: (row: Row) => (
        <Tag tone={holdingRefreshType(row.refresh_status)}>{row.refresh_status || '--'}</Tag>
      )
    },
    { key: 'management_action', label: '管理动作', minWidth: 140 },
    { key: 'staleness_evidence', label: '新鲜度证据', minWidth: 300 },
    { key: 'pass_condition', label: '通过条件', minWidth: 320 },
    { key: 'fallback_if_fail', label: '失败退路', minWidth: 240 },
    { key: 'next_action', label: '下一步', minWidth: 280 }
  ]

  const holdingChecklistCols: Col[] = [
    { key: 'source', label: '来源', width: 110 },
    { key: 'code', label: '代码', width: 110 },
    { key: 'name', label: '名称', minWidth: 110 },
    {
      key: 'checklist_level',
      label: '等级',
      width: 86,
      render: (row: Row) => (
        <Tag tone={gateType(row.checklist_level)}>{row.checklist_level || '--'}</Tag>
      )
    },
    { key: 'exit_state', label: '退出状态', minWidth: 260 },
    { key: 'management_action', label: '管理动作', minWidth: 140 },
    { key: 'exit_reason', label: '退出原因', minWidth: 240 },
    { key: 'hidden_risks', label: '隐藏风险', minWidth: 300 },
    { key: 'manual_questions', label: '人工问题', minWidth: 320 },
    { key: 'action_recommendation', label: '动作建议', minWidth: 220 }
  ]

  const candidateCols: Col[] = [
    { key: 'code', label: '代码', width: 110 },
    { key: 'name', label: '名称', minWidth: 110 },
    {
      key: 'checklist_level',
      label: '等级',
      width: 86,
      render: (row: Row) => (
        <Tag tone={gateType(row.checklist_level)}>{row.checklist_level || '--'}</Tag>
      )
    },
    {
      key: 'selected_next_trade',
      label: '入选',
      width: 86,
      render: (row: Row) => (
        <Tag tone={truthy(row.selected_next_trade) ? 'success' : 'info'}>
          {truthy(row.selected_next_trade) ? '已入选' : '未入选'}
        </Tag>
      )
    },
    { key: 'strategy', label: '策略', minWidth: 130 },
    {
      key: 'omission_watch_status',
      label: '观察结论',
      width: 130,
      render: (row: Row) => (
        <Tag tone={paperWatchStatusType(row.omission_watch_status)}>
          {row.watch_result_label || paperWatchResultLabel(row.watch_result) || '待观察'}
        </Tag>
      )
    },
    { key: 'selection_state', label: '选股状态', minWidth: 280 },
    { key: 'strategy_switch_assessment', label: '策略切换', minWidth: 260 },
    { key: 'hidden_risks', label: '隐藏风险', minWidth: 300 },
    { key: 'manual_questions', label: '人工问题', minWidth: 320 },
    { key: 'action_recommendation', label: '动作建议', minWidth: 220 },
    {
      key: 'actions',
      label: '记录遗漏复盘',
      width: 420,
      render: (row: Row) => (
        <div className="flex flex-wrap gap-1.5">
          <Btn tone="success" onClick={() => a.savePaperWatchFollowup(row, 'as_expected')}>挡得合理</Btn>
          <Btn tone="warning" onClick={() => a.savePaperWatchFollowup(row, 'missed_opportunity')}>可能误杀</Btn>
          <Btn tone="danger" onClick={() => a.savePaperWatchFollowup(row, 'invalid_signal')}>信号失效</Btn>
          <Btn tone="info" onClick={() => a.savePaperWatchFollowup(row, 'continue_watch')}>继续观察</Btn>
        </div>
      )
    }
  ]

  const gateCols: Col[] = [
    { key: 'gate', label: 'Gate', minWidth: 170 },
    {
      key: 'status',
      label: '状态',
      width: 86,
      render: (row: Row) => <Tag tone={gateType(row.status)}>{row.status}</Tag>
    },
    { key: 'message', label: '说明', minWidth: 260 }
  ]

  const hazardCols: Col[] = [
    {
      key: 'severity',
      label: '级别',
      width: 86,
      render: (row: Row) => <Tag tone={gateType(row.severity)}>{row.severity}</Tag>
    },
    { key: 'object', label: '对象', minWidth: 130 },
    { key: 'hazard', label: '隐患', minWidth: 260 }
  ]

  return (
    <Card className="mt-6 p-5">
      <h3 className="mb-3 text-left text-base font-semibold text-zinc-900 dark:text-zinc-100">逐票复盘底稿与台账</h3>
      <DataTable rows={v.noTradeDayReviewRows} cols={noTradeCols} empty="暂无无票日复盘" />
      <DataTable rows={v.day1PaperReviewRows} cols={day1PaperCols} empty="暂无第1天纸面复盘包" />
      <DataTable rows={v.day1AfterCloseReviewRows} cols={day1AfterCols} empty="暂无Day1盘后复盘队列" />
      <DataTable rows={v.pretradeEvidenceRows} cols={pretradeEvidenceCols} empty="暂无逐票复盘证据" />
      <DataTable rows={v.naturalConsistencyRows} cols={naturalConsistencyCols} empty="暂无自然交易一致性审计" />
      <DataTable rows={v.naturalExecutionRows} cols={naturalExecutionCols} empty="暂无自然执行决策" />
      <DataTable rows={v.reviewTicketRows} cols={reviewTicketCols} empty="暂无下一交易日逐票复盘" />
      <DataTable rows={v.pretradeReviewLedgerRows} cols={reviewLedgerCols} empty="暂无逐票复盘台账" />
      <DataTable rows={v.paperWatchFollowupRows} cols={paperFollowupCols} empty="暂无纸面观察后评估" />
      <DataTable rows={v.pretradeActionRows} cols={pretradeActionCols} empty="暂无实战前动作清单" />
      <DataTable rows={v.ticketChecklistRows} cols={ticketChecklistCols} empty="暂无逐票复盘底稿" />
      <DataTable rows={v.holdingRefreshRows} cols={holdingRefreshCols} empty="暂无持仓刷新证明" />
      <DataTable rows={v.holdingChecklistRows} cols={holdingChecklistCols} empty="暂无持仓退出底稿" />
      <DataTable rows={v.candidateChecklistRows} cols={candidateCols} empty="暂无候选遗漏底稿" />

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <div>
          <h4 className="mb-1 text-xs font-semibold text-zinc-500 dark:text-zinc-400">实战 Gate</h4>
          <DataTable rows={v.readinessGates} cols={gateCols} empty="暂无实战 Gate" />
        </div>
        <div>
          <h4 className="mb-1 text-xs font-semibold text-zinc-500 dark:text-zinc-400">隐患登记</h4>
          <DataTable rows={v.hazardRows} cols={hazardCols} empty="暂无隐患登记" />
        </div>
      </div>
    </Card>
  )
}
