// 组合合同 / 退出合同 / 实盘准入与首日决策卡 / 正式交易策略 / 今日票据风控 / 正式交易硬锁 / 合同产物
import { Btn, DataTable, DecisionCard, KVGrid, Panel, Tag, useRisk } from './shared'
import type { Col } from './shared'
import { listText, pct, price, truthy, type Row } from './labels'

export default function ContractsPanel() {
  const { v, a } = useRisk()

  const strategyCols: Col[] = [
    { key: 'label_cn', label: '交易策略', width: 150 },
    {
      key: 'default_position_pct',
      label: '默认仓位',
      width: 90,
      align: 'right',
      render: (row: Row) => pct(row.default_position_pct)
    },
    {
      key: 'source_routes',
      label: '来源路由',
      minWidth: 180,
      render: (row: Row) => listText(row.source_routes)
    },
    {
      key: 'source_strategies',
      label: '合并来源',
      minWidth: 260,
      render: (row: Row) => listText(row.source_strategies)
    },
    { key: 'risk_note', label: '风控说明', minWidth: 260 }
  ]

  const ticketCols: Col[] = [
    { key: 'code', label: '代码', width: 110 },
    { key: 'name', label: '名称', minWidth: 110 },
    {
      key: 'trade_strategy_label',
      label: '交易策略',
      width: 150,
      render: (row: Row) => row.trade_strategy_label || row.route_strategy_label || row.route_label || '--'
    },
    { key: 'route_label', label: '来源路线', width: 130 },
    {
      key: 'position_pct',
      label: '仓位',
      width: 82,
      align: 'right',
      render: (row: Row) => pct(row.position_pct)
    },
    {
      key: 'reference_close',
      label: '参考价',
      width: 90,
      align: 'right',
      render: (row: Row) => price(row.reference_close)
    },
    {
      key: 'structure_stop',
      label: '结构止损',
      width: 96,
      align: 'right',
      render: (row: Row) => price(row.structure_stop)
    },
    {
      key: 'hard_stop',
      label: '硬止损',
      width: 90,
      align: 'right',
      render: (row: Row) => price(row.hard_stop)
    },
    {
      key: 'take_profit_1',
      label: '止盈一',
      width: 90,
      align: 'right',
      render: (row: Row) => price(row.take_profit_1)
    },
    {
      key: 'qualified_shadow_buy',
      label: '合格',
      width: 82,
      render: (row: Row) => (
        <Tag tone={truthy(row.qualified_shadow_buy) ? 'success' : 'warning'}>
          {truthy(row.qualified_shadow_buy) ? '是' : '否'}
        </Tag>
      )
    },
    { key: 'exit_contract', label: '退出合同', minWidth: 300 }
  ]

  const artifactCols: Col[] = [
    { key: 'name', label: '产物', minWidth: 160 },
    {
      key: 'exists',
      label: '状态',
      width: 86,
      render: (row: Row) => (
        <Tag tone={row.exists ? 'success' : 'danger'}>{row.exists ? '存在' : '缺失'}</Tag>
      )
    },
    { key: 'modified_at', label: '更新时间', width: 170 },
    { key: 'path', label: '路径', minWidth: 260 }
  ]

  return (
    <>
      <section className="mt-5 grid gap-4 lg:grid-cols-2">
        <Panel title="组合合同" desc="仓位和账户级风险约束。">
          <KVGrid items={v.portfolioRows.map((item: any) => ({ label: item.label, value: item.value }))} />
        </Panel>
        <Panel title="退出合同" desc="每张影子票据必须能追溯到明确退出规则。">
          <KVGrid items={v.exitRows.map((item: any) => ({ label: item.label, value: item.value }))} />
        </Panel>
        {v.liveAdmissionSnapshotCard && (
          <DecisionCard
            tone={v.liveAdmissionTone}
            label="实盘准入快照"
            strong={v.liveAdmissionSnapshotCard.admission_label || '--'}
            text={v.liveAdmissionSnapshotCard.next_step || v.liveAdmissionSnapshotCard.execution_posture || '--'}
            detail={[
              { label: '主因', value: v.liveAdmissionSnapshotCard.primary_reason || '--' },
              { label: '第一动作', value: v.liveAdmissionSnapshotCard.first_required_action || '--' },
              { label: '复核规则', value: v.liveAdmissionSnapshotCard.verification_rule || '--' }
            ]}
            flags={[
              {
                tone: truthy(v.liveAdmissionSnapshotCard.live_buy_allowed) ? 'success' : 'danger',
                text: `真实买入 ${truthy(v.liveAdmissionSnapshotCard.live_buy_allowed) ? '允许' : '禁止'}`
              },
              {
                tone: truthy(v.liveAdmissionSnapshotCard.paper_execution_allowed) ? 'warning' : 'info',
                text: `纸面执行 ${
                  truthy(v.liveAdmissionSnapshotCard.paper_execution_allowed) ? '可记录' : '不需要'
                }`
              },
              { tone: 'info', text: `阻断 ${v.liveAdmissionSnapshotCard.blocking_command_count ?? '--'}` },
              { tone: 'info', text: '自动下单锁定' }
            ]}
          />
        )}
        {v.firstLiveDecisionCard && (
          <DecisionCard
            tone={v.firstLiveDecisionTone}
            label="首日实战决策"
            strong={v.firstLiveDecisionCard.decision_label || '--'}
            text={v.firstLiveDecisionCard.execution_posture || '--'}
            detail={[
              { label: '主因', value: v.firstLiveDecisionCard.primary_reason || '--' },
              { label: '第一动作', value: v.firstLiveDecisionCard.first_required_action || '--' },
              { label: '锁定模式', value: v.firstLiveDecisionCard.locked_modes || '--' }
            ]}
            flags={[
              {
                tone: truthy(v.firstLiveDecisionCard.live_buy_allowed) ? 'success' : 'info',
                text: `真实买入 ${truthy(v.firstLiveDecisionCard.live_buy_allowed) ? '允许' : '禁止'}`
              },
              {
                tone: truthy(v.firstLiveDecisionCard.paper_execution_allowed) ? 'warning' : 'info',
                text: `纸面执行 ${
                  truthy(v.firstLiveDecisionCard.paper_execution_allowed) ? '可记录' : '不需要'
                }`
              },
              { tone: 'info', text: '自动下单锁定' }
            ]}
          />
        )}
      </section>

      <Panel className="mt-5" title="正式交易策略" desc="路由只作为来源通道，风控合同按正式交易策略保持一致。">
        <DataTable rows={v.tradeStrategyPolicy} cols={strategyCols} empty="暂无正式策略合同" />
      </Panel>

      <Panel className="mt-5" title="今日票据风控" desc="用于纸面执行前复核仓位、参考价、结构止损、硬止损和止盈。">
        <div className="flex justify-end">
          <Btn tone="primary" loading={v.loading} onClick={() => a.load()}>
            刷新
          </Btn>
        </div>
        <DataTable rows={v.shadowTickets} cols={ticketCols} empty="暂无影子票据" />
      </Panel>

      <section className="mt-5 grid gap-4 lg:grid-cols-2">
        <Panel title="正式交易硬锁" desc="这些状态必须保持关闭，直到替代门槛全部通过。">
          <div className="mt-4 flex flex-col gap-2">
            {v.guardrailRows.map((item: any, i: number) => (
              <div
                key={i}
                className="flex items-start gap-3 rounded-lg p-3 ring-1 ring-zinc-950/10 dark:ring-white/10"
              >
                <Tag tone={item.ok ? 'success' : 'danger'}>{item.ok ? '锁定' : '异常'}</Tag>
                <div>
                  <div className="text-sm font-semibold text-zinc-950 dark:text-white">{item.label}</div>
                  <div className="text-xs text-zinc-500 dark:text-zinc-400">{item.detail}</div>
                </div>
              </div>
            ))}
          </div>
        </Panel>
        <Panel title="合同产物" desc="确保风控规则能追溯到运行产物和蓝图。">
          <DataTable rows={v.artifactRows} cols={artifactCols} empty="暂无合同产物" />
        </Panel>
      </section>
    </>
  )
}
