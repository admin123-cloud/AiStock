// G3 实战启动包（下半部分：阻断消证看板 / 启动摘要 / 快照与详情 / 启动步骤 / 启动日剧本 / 学习样本 / 复盘轴矩阵）
import { Dialog, DialogActions, DialogBody, DialogTitle } from '@/components/catalyst/dialog'
import { Button } from '@/components/catalyst/button'
import { AuditGrid, AuditMain, Btn, DataTable, StatGrid, Tag, useRisk } from './shared'
import type { Col } from './shared'
import {
  launchDayPlaybookRowLoading,
  launchDayPlaybookStatusLabel,
  launchDayPlaybookStatusType,
  reviewBoardStatusLabel,
  reviewBoardStatusType,
  truthy,
  type Row
} from './labels'

export default function LiveLaunchTail() {
  const { v, a } = useRisk()

  const blockerCols: Col[] = [
    { key: 'priority', label: '#', width: 56 },
    {
      key: 'evidence_status',
      label: '证据状态',
      width: 118,
      render: (row: Row) => (
        <Tag tone={reviewBoardStatusType(row.evidence_status)}>{reviewBoardStatusLabel(row.evidence_status)}</Tag>
      )
    },
    {
      key: 'can_execute_now',
      label: '当前可做',
      width: 92,
      render: (row: Row) => (
        <Tag tone={truthy(row.can_execute_now) ? 'success' : 'info'}>
          {truthy(row.can_execute_now) ? '是' : '等待'}
        </Tag>
      )
    },
    { key: 'resolution_type', label: '处理路径', width: 190 },
    { key: 'object', label: '对象', minWidth: 170 },
    { key: 'action', label: '阻断动作', minWidth: 280 },
    { key: 'learning_axis', label: '影响轴', width: 160 },
    { key: 'trade_impact', label: '交易影响', minWidth: 280 },
    { key: 'completion_check', label: '完成标准', minWidth: 320 }
  ]

  const snapshotCols: Col[] = [
    { key: 'generated_at', label: '记录时间', width: 170 },
    { key: 'phase', label: '阶段', width: 130 },
    { key: 'entry_date', label: '交易日', width: 110 },
    { key: 'live_admission_status', label: '准入状态', minWidth: 180 },
    { key: 'blocking_command_count', label: '阻断', width: 80, align: 'right' },
    { key: 'next_action', label: '下一动作', minWidth: 220 },
    { key: 'launch_day_learning_queue_count', label: '学习样本', width: 100, align: 'right' },
    { key: 'current_action_label', label: '当前动作', minWidth: 220 },
    { key: 'note', label: '备注', minWidth: 260 },
    {
      key: 'detail',
      label: '证据',
      width: 92,
      render: (row: Row) => (
        <Btn
          loading={v.liveLaunchSnapshotDetailLoading && v.liveLaunchSnapshotDetailId === row.snapshot_id}
          onClick={() => a.openLiveLaunchSnapshotDetail(row)}
        >
          查看
        </Btn>
      )
    }
  ]

  const stepCols: Col[] = [
    { key: 'step', label: '#', width: 56 },
    { key: 'action_label', label: '启动动作', minWidth: 180 },
    { key: 'action_group', label: '动作组', minWidth: 170 },
    {
      key: 'can_execute_now',
      label: '当前可做',
      width: 92,
      render: (row: Row) => (
        <Tag tone={truthy(row.can_execute_now) ? 'success' : 'info'}>
          {truthy(row.can_execute_now) ? '可执行' : '等前置'}
        </Tag>
      )
    },
    {
      key: 'requires_manual_confirmation',
      label: '需确认',
      width: 86,
      render: (row: Row) => (
        <Tag tone={truthy(row.requires_manual_confirmation) ? 'warning' : 'info'}>
          {truthy(row.requires_manual_confirmation) ? '需要' : '无需'}
        </Tag>
      )
    },
    { key: 'expected_effect', label: '预期效果', minWidth: 280 },
    { key: 'completion_check', label: '完成检查', minWidth: 300 },
    { key: 'stop_if_fail', label: '失败边界', minWidth: 280 },
    { key: 'natural_trade_boundary', label: '自然交易边界', minWidth: 320 }
  ]

  const playbookCols: Col[] = [
    { key: 'priority', label: '#', width: 64 },
    { key: 'window', label: '窗口', width: 110 },
    { key: 'checkpoint_time', label: '时间点', width: 140 },
    { key: 'action_type', label: '动作类型', minWidth: 150 },
    { key: 'review_axis', label: '复盘轴', width: 150 },
    { key: 'object', label: '对象', minWidth: 180 },
    { key: 'required_action', label: '必须动作', minWidth: 320 },
    { key: 'evidence_to_collect', label: '应收集证据', minWidth: 340 },
    { key: 'pass_condition', label: '通过条件', minWidth: 300 },
    { key: 'fail_condition', label: '失败边界', minWidth: 300 },
    { key: 'launch_permission_effect', label: '准入影响', minWidth: 220 },
    {
      key: 'review_status',
      label: '复盘状态',
      width: 116,
      render: (row: Row) => (
        <Tag tone={launchDayPlaybookStatusType(row.review_status)}>
          {launchDayPlaybookStatusLabel(row.review_status, row.review_result_label)}
        </Tag>
      )
    },
    {
      key: 'actions',
      label: '落账',
      width: 330,
      render: (row: Row) => (
        <div className="flex flex-wrap gap-1.5">
          <Btn tone="success" loading={launchDayPlaybookRowLoading(row, v)} onClick={() => a.saveLaunchDayPlaybookReview(row, 'validated')}>
            符合预期
          </Btn>
          <Btn tone="danger" loading={launchDayPlaybookRowLoading(row, v)} onClick={() => a.saveLaunchDayPlaybookReview(row, 'issue_found')}>
            发现隐患
          </Btn>
          <Btn tone="warning" loading={launchDayPlaybookRowLoading(row, v)} onClick={() => a.saveLaunchDayPlaybookReview(row, 'continue_watch')}>
            继续观察
          </Btn>
          <Btn tone="info" loading={launchDayPlaybookRowLoading(row, v)} onClick={() => a.saveLaunchDayPlaybookReview(row, 'data_gap')}>
            数据缺口
          </Btn>
        </div>
      )
    }
  ]

  const learningCols: Col[] = [
    { key: 'priority', label: '#', width: 64 },
    { key: 'learning_status', label: '学习状态', width: 120 },
    { key: 'axis_label', label: '复盘轴', width: 130 },
    { key: 'object', label: '对象', minWidth: 200 },
    { key: 'evidence', label: '证据', minWidth: 300 },
    { key: 'hidden_risk', label: '隐患', minWidth: 320 },
    { key: 'optimization_direction', label: '优化方向', minWidth: 320 },
    { key: 'natural_trade_boundary', label: '自然交易边界', minWidth: 320 }
  ]

  const reviewAxisCols: Col[] = [
    { key: 'axis_label', label: '复盘轴', width: 130 },
    { key: 'pending_count', label: '待复盘', width: 86, align: 'right' },
    { key: 'high_risk_count', label: '高风险', width: 86, align: 'right' },
    { key: 'formal_required_count', label: '正式必处理', width: 104, align: 'right' },
    { key: 'first_object', label: '首个对象', minWidth: 180 },
    { key: 'next_action', label: '下一步证据', minWidth: 320 },
    { key: 'pass_condition', label: '通过条件', minWidth: 320 },
    { key: 'fail_condition', label: '失败边界', minWidth: 320 },
    { key: 'launch_decision', label: '启动决策', minWidth: 260 }
  ]

  // 快照详情弹窗内表格
  const detailLedgerCols: Col[] = [
    { key: 'priority', label: '#', width: 64 },
    { key: 'origin', label: '来源', width: 170 },
    { key: 'learning_status', label: '学习状态', width: 120 },
    { key: 'risk_level', label: '风险级别', width: 110 },
    { key: 'issue_area', label: '问题轴', width: 140 },
    { key: 'object', label: '对象', minWidth: 180 },
    { key: 'problem_signal', label: '问题信号/隐患', minWidth: 300 },
    { key: 'evidence', label: '证据/缺口', minWidth: 300 },
    { key: 'suggested_learning', label: '学习/处理方向', minWidth: 320 },
    { key: 'natural_trade_boundary', label: '自然交易边界', minWidth: 320 }
  ]
  const detailPremarketCols: Col[] = [
    { key: 'action_label', label: '动作', minWidth: 180 },
    { key: 'action_group', label: '动作组', minWidth: 150 },
    { key: 'completion_check', label: '完成检查', minWidth: 280 },
    { key: 'stop_if_fail', label: '失败边界', minWidth: 260 },
    { key: 'natural_trade_boundary', label: '自然交易边界', minWidth: 300 }
  ]
  const detailPlaybookCols: Col[] = [
    { key: 'priority', label: '#', width: 64 },
    { key: 'checkpoint_time', label: '时间点', width: 130 },
    { key: 'review_axis', label: '复盘轴', width: 130 },
    { key: 'object', label: '对象', minWidth: 180 },
    { key: 'required_action', label: '必须动作', minWidth: 300 },
    { key: 'review_status', label: '复盘状态', width: 120 },
    { key: 'review_note', label: '备注', minWidth: 260 }
  ]
  const detailLearningCols: Col[] = [
    { key: 'priority', label: '#', width: 64 },
    { key: 'learning_status', label: '学习状态', width: 120 },
    { key: 'axis_label', label: '复盘轴', width: 130 },
    { key: 'object', label: '对象', minWidth: 180 },
    { key: 'hidden_risk', label: '隐患', minWidth: 300 },
    { key: 'optimization_direction', label: '优化方向', minWidth: 320 },
    { key: 'natural_trade_boundary', label: '自然交易边界', minWidth: 320 }
  ]

  const detail = v.liveLaunchSnapshotDetail

  return (
    <>
      <DataTable rows={v.liveBlockerEvidenceBoardRows} cols={blockerCols} empty="暂无阻断消证项" />

      <StatGrid
        items={[
          {
            span: '启动状态',
            strong: <Tag tone={v.launchPacketStatusType as any}>{v.launchPacket.status || '--'}</Tag>,
            small: v.launchPacket.generated_at || '--'
          },
          {
            span: '烟测状态',
            strong: v.launchPacketSummary.smoke_status || '--',
            small: v.launchPacketSummary.smoke_generated_at || '--'
          },
          {
            span: '实盘准入',
            strong: v.launchPacketSummary.live_admission_status || '--',
            small: truthy(v.launchPacketSummary.live_admission_buy_allowed) ? '可进入人工复核' : '禁止真实买入'
          },
          {
            span: '下一动作',
            strong: v.launchPacketSummary.live_premarket_next_action || v.premarketCommand.next_action || '--',
            small: `阻断 ${v.launchPacketSummary.live_admission_blocking_command_count ?? '--'} 项`
          },
          {
            span: '下一交易日',
            strong: v.launchPacketSummary.next_trade_entry_date || '--',
            small: `票据 ${v.launchPacketSummary.next_trade_ticket_count ?? '--'} 张`
          },
          {
            span: '遗漏复盘',
            strong: v.launchPacketSummary.candidate_omission_pending_count ?? '--',
            small: `无票日 ${v.launchPacketSummary.no_trade_day_pending_review_count ?? '--'} / 每日待办 ${
              v.launchPacketSummary.daily_live_review_board_pending_count ?? '--'
            }`
          },
          {
            span: '启动日学习',
            strong: v.launchPacketSummary.launch_day_learning_queue_count ?? '--',
            small: `需调优 ${v.launchPacketSummary.launch_day_learning_needs_review_count ?? '--'} / 继续观察 ${
              v.launchPacketSummary.launch_day_learning_watch_more_count ?? '--'
            }`
          }
        ]}
      />

      <DataTable rows={v.liveLaunchReviewSnapshotRows} cols={snapshotCols} empty="暂无启动复盘快照" />
      <DataTable rows={v.liveLaunchStepRows} cols={stepCols} empty="暂无启动步骤" />
      <DataTable rows={v.liveLaunchDayPlaybookRows} cols={playbookCols} empty="暂无启动日剧本" />
      <DataTable rows={v.liveLaunchLearningQueueRows} cols={learningCols} empty="暂无启动日学习样本" />
      <DataTable rows={v.liveLaunchReviewAxisRows} cols={reviewAxisCols} empty="暂无复盘轴矩阵" />

      <Dialog open={v.liveLaunchSnapshotDetailVisible} onClose={() => a.closeLiveLaunchSnapshotDetail()} size="5xl">
        <DialogTitle>G3 启动复盘快照详情</DialogTitle>
        <DialogBody>
          {detail ? (
            <div>
              <AuditGrid
                items={[
                  { label: '快照', value: detail.snapshot_id || v.liveLaunchSnapshotDetailId || '--' },
                  { label: '阶段', value: detail.phase || '--' },
                  { label: '记录时间', value: detail.generated_at || '--' },
                  { label: '准入状态', value: detail.live_admission_status || '--' },
                  { label: '阻断', value: detail.blocking_command_count ?? '--' },
                  { label: '学习样本', value: detail.launch_day_learning_queue_count ?? '--' },
                  {
                    label: '统一隐患',
                    value:
                      detail.live_learning_ledger_count ?? v.liveLaunchSnapshotLearningLedgerRows.length ?? '--'
                  }
                ]}
              />
              <AuditMain label="备注" strong={detail.note || '--'} />
              <DataTable
                rows={v.liveLaunchSnapshotLearningLedgerRows}
                cols={detailLedgerCols}
                empty="暂无统一学习隐患"
              />
              <DataTable rows={v.liveLaunchSnapshotPremarketRows} cols={detailPremarketCols} empty="暂无盘前命令" />
              <DataTable rows={v.liveLaunchSnapshotPlaybookRows} cols={detailPlaybookCols} empty="暂无启动日剧本" />
              <DataTable rows={v.liveLaunchSnapshotLearningRows} cols={detailLearningCols} empty="暂无学习样本" />
            </div>
          ) : (
            <p className="text-sm text-zinc-500 dark:text-zinc-400">暂无快照详情</p>
          )}
        </DialogBody>
        <DialogActions>
          <Button plain onClick={() => a.closeLiveLaunchSnapshotDetail()}>
            关闭
          </Button>
        </DialogActions>
      </Dialog>
    </>
  )
}
