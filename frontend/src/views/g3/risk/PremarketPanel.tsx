// 盘前指挥卡（对应旧页 premarket-control-panel）
import { Btn, Panel, StatGrid, useRisk } from './shared'
import { truthy } from './labels'

export default function PremarketPanel() {
  const { v, a } = useRisk()
  const cmd = v.premarketCommand
  const readinessSummary = v.readinessSummary
  const preflight = v.brokerSyncPreflight

  return (
    <Panel
      className="mt-5"
      title="盘前指挥卡"
      desc="系统只给出当前下一步和复审边界；真实买入仍保持人工确认与下单锁定。"
      actions={
        <>
          <Btn tone="primary" loading={v.brokerSyncPreflightLoading} onClick={() => a.refreshBrokerSyncPreflight()}>
            同步预检
          </Btn>
          <Btn tone="primary" loading={v.premarketControlLoading} onClick={() => a.refreshPremarketControl(true)}>
            刷新指挥卡
          </Btn>
          <Btn
            tone="success"
            loading={v.premarketNextActionLoading || v.brokerSyncLoading || v.reviewLoading}
            disabled={!cmd.action_group || !truthy(cmd.can_execute_now)}
            onClick={() => a.executePremarketNextAction()}
          >
            执行当前下一步
          </Btn>
        </>
      }
    >
      <StatGrid
        items={[
          {
            span: '准入状态',
            strong: cmd.status || readinessSummary.live_admission_status || '--',
            small: cmd.status_label || readinessSummary.live_admission_label || '--'
          },
          {
            span: '当前动作',
            strong: cmd.action_label || cmd.next_action || '--',
            small: cmd.action_group || '--'
          },
          {
            span: '阻断/证据',
            strong: `${cmd.blocking_command_count ?? readinessSummary.live_admission_blocking_command_count ?? '--'} / ${
              cmd.pending_evidence_count ?? readinessSummary.live_blocker_pending_evidence_count ?? '--'
            }`,
            small: '阻断动作 / 待证据'
          },
          {
            span: '执行边界',
            strong: truthy(cmd.live_buy_allowed) ? '人工复核' : '禁止买入',
            small: cmd.requires_confirmation ? '需要确认；不触发自动下单' : '不需要确认；仍锁定下单通道'
          }
        ]}
      />
      <StatGrid
        items={[
          { span: '推荐动作', strong: cmd.current_action?.action || cmd.recommended_ui_action || '--' },
          { span: '完成检查', strong: cmd.current_action?.completion_check || '--' },
          { span: '失败边界', strong: cmd.current_action?.stop_if_fail || '--' },
          {
            span: '自然交易边界',
            strong: cmd.current_action?.natural_trade_boundary || cmd.current_recheck?.natural_trade_boundary || '--'
          }
        ]}
      />
      <StatGrid
        items={[
          {
            span: '同步预检',
            strong: preflight.preflight_status || '--',
            small: truthy(preflight.can_prompt_manual_sync) ? '可进入人工确认' : '暂不建议执行'
          },
          {
            span: '网关配置',
            strong: truthy(preflight.gateway_configured) ? '已配置' : '未配置',
            small: preflight.gateway_url_hint || '--'
          },
          {
            span: '持仓缓存',
            strong: `${preflight.holdings_count ?? '--'} / ${preflight.broker_staleness_minutes ?? '--'}m`,
            small: preflight.broker_updated_at || '--'
          },
          {
            span: '预检下一步',
            strong: preflight.current_action_label || cmd.action_label || '--',
            small: preflight.next_manual_action || '--'
          }
        ]}
      />
    </Panel>
  )
}
