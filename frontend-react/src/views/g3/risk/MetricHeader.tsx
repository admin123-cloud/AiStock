// 顶部操作栏 + 指标卡 + confirmation 卡（对应旧页 topbar / metric-grid）
// 视觉对齐全局 UI 原语：PageHeader / Card / StatCard（@/components/ui）
import { Button } from '@/components/catalyst/button'
import { Card, PageHeader, StatCard } from '@/components/ui'
import { Btn, useRisk } from './shared'
import { pct, truthy } from './labels'

export default function MetricHeader() {
  const { v, a } = useRisk()
  const contract = v.contract
  const portfolio = v.portfolio
  const exitContract = v.exitContract
  const summary = v.summary

  const accountRiskAction = summary.account_risk?.action || 'normal'
  const accountRiskTone = accountRiskAction !== 'normal' ? ('warning' as const) : ('default' as const)

  const confirmationCards = [
    {
      items: [
        {
          span: 'confirmation packet',
          strong: v.brokerSyncConfirmationPacket.packet_status || '--',
          small: truthy(v.brokerSyncConfirmationPacket.safe_to_prompt) ? 'safe to prompt' : 'not ready'
        },
        {
          span: 'action fingerprint',
          strong: v.brokerSyncConfirmationFingerprint.action_group || '--',
          small:
            v.brokerSyncConfirmationFingerprint.primary_blocking_key ||
            v.brokerSyncConfirmationFingerprint.object ||
            '--'
        },
        {
          span: 'completion check',
          strong: v.brokerSyncConfirmationPacket.completion_check || '--',
          small: v.brokerSyncConfirmationPacket.stop_if_fail || '--'
        }
      ]
    },
    {
      items: [
        {
          span: 'sync outcome',
          strong: v.brokerSyncOutcome.outcome_status || '--',
          small: v.brokerSyncOutcome.next_action || '--'
        },
        {
          span: 'latest attempt',
          strong: v.brokerSyncOutcomePayload.sync_attempt_count ?? '--',
          small: v.brokerSyncLatestAttempt.attempted_at || 'no attempt yet'
        },
        {
          span: 'broker freshness',
          strong: truthy(v.brokerSyncOutcomeBroker.is_fresh) ? 'fresh' : 'stale',
          small: `${v.brokerSyncOutcomeBroker.staleness_minutes ?? '--'}m / ${v.brokerSyncOutcomeBroker.holdings_count ?? '--'} holdings`
        }
      ]
    }
  ]

  return (
    <header className="mb-5">
      <PageHeader
        title={
          <>
            <span className="mb-0.5 block text-xs font-medium uppercase tracking-wider text-indigo-600 dark:text-indigo-400">
              G3 正式五策略
            </span>
            风控合同
          </>
        }
        subtitle="集中查看五策略仓位、止损、止盈、退出合同和正式交易硬锁，保证页面、回测和未来调度使用同一套交易口径。"
        actions={
          <>
            <Button color="indigo" disabled={v.loading} onClick={() => a.load()}>
              刷新
            </Button>
            <Btn
              tone="primary"
              loading={v.batchReviewLoading}
              disabled={!v.unreviewedTicketRows.length}
              onClick={() => a.markUnreviewedAsPaperWatch()}
            >
              未复盘转纸面观察
            </Btn>
            <Btn
              tone="success"
              loading={v.day1ExecutionLoading}
              disabled={!v.day1PaperReviewRows.length}
              onClick={() => a.recordDay1PaperExecutions()}
            >
              记录Day1纸面执行
            </Btn>
            <Btn tone="success" loading={v.brokerSyncLoading} onClick={() => a.syncBrokerHoldingsAndReview()}>
              同步真实持仓并复审
            </Btn>
            <Btn tone="warning" loading={v.reviewLoading} onClick={() => a.runReadinessReview()}>
              重跑实战前审计
            </Btn>
          </>
        }
      />

      <section className="mt-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        <StatCard label="策略阶段" value={contract.stage || v.payload.stage || '--'} hint="shadow / observe 优先" />
        <StatCard
          tone="info"
          label="组合槽位"
          value={portfolio.max_slots ?? portfolio.slots ?? '--'}
          hint={`单槽 ${pct(portfolio.per_slot_position_pct || portfolio.slot_pct)}`}
        />
        <StatCard
          label="硬止损"
          value={pct(exitContract.hard_stop_pct)}
          hint={`先减半 ${pct(exitContract.first_take_profit_pct)}`}
        />
        <StatCard label="今日票据" value={v.shadowTickets.length} hint={`合格 ${v.qualifiedTickets.length}`} />
        <StatCard
          tone={accountRiskTone}
          label="账户风控"
          value={accountRiskAction}
          hint={summary.account_risk?.reason || '按影子台账判断'}
        />
        <StatCard
          tone="warning"
          label="下单通道"
          value="关闭"
          hint="正式买点、自动下单、订单路径均锁定"
        />
        {confirmationCards.map((card, idx) => (
          <Card key={idx} className="flex flex-col gap-3">
            {card.items.map((item, i) => (
              <div key={i} className={i === 0 ? '' : 'border-t border-zinc-100 pt-3 dark:border-white/10'}>
                <div className="truncate text-xs text-zinc-500 dark:text-zinc-400">{item.span}</div>
                <div className="mt-0.5 truncate text-sm font-semibold text-zinc-900 dark:text-white">
                  {item.strong}
                </div>
                <div className="truncate text-xs text-zinc-500 dark:text-zinc-400">{item.small}</div>
              </div>
            ))}
          </Card>
        ))}
      </section>
    </header>
  )
}
