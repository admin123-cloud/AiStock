import { Link, useSearchParams } from 'react-router-dom'
import { Button } from '@/components/catalyst/button'
import { PageHeader } from '@/components/ui'
import BrokerAccounts from '@/views/operations/BrokerAccounts'
import G3TTradeWorkbench from '@/views/g3/G3TTradeWorkbench'

type AccountsTab = 'accounts' | 'ttrade'

export default function G3Accounts() {
  const [searchParams, setSearchParams] = useSearchParams()
  const activeTab: AccountsTab = searchParams.get('tab') === 'ttrade' ? 'ttrade' : 'accounts'

  const switchTab = (next: AccountsTab) => {
    const params = new URLSearchParams(searchParams)
    if (next === 'accounts') {
      params.delete('tab')
    } else {
      params.set('tab', next)
    }
    setSearchParams(params, { replace: true })
  }

  return (
    <div className="flex w-full flex-col gap-4">
      <PageHeader
        title="资金账号与底仓做T中心"
        subtitle="双账号（ct1曾 / ct2）只读资金、持仓、成交明细 × 已有底仓实时 5m RSI 做T作战与执行台账"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Link to="/g3/desk">
              <Button outline>⚡ 返回实战交易台 →</Button>
            </Link>
            <Link to="/g3/review">
              <Button plain>📊 查看实盘复盘归因</Button>
            </Link>
          </div>
        }
      />

      {/* 顶部双 Tab 切换条 */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-zinc-800 pb-3">
        <div className="inline-flex rounded-xl border border-zinc-800 bg-zinc-900/80 p-1">
          <button
            type="button"
            onClick={() => switchTab('accounts')}
            className={`rounded-lg px-4 py-1.5 text-xs font-semibold transition ${
              activeTab === 'accounts'
                ? 'bg-blue-600 text-white shadow-sm'
                : 'text-zinc-400 hover:text-zinc-100'
            }`}
          >
            💼 资金、持仓与成交明细
          </button>
          <button
            type="button"
            onClick={() => switchTab('ttrade')}
            className={`rounded-lg px-4 py-1.5 text-xs font-semibold transition ${
              activeTab === 'ttrade'
                ? 'bg-blue-600 text-white shadow-sm'
                : 'text-zinc-400 hover:text-zinc-100'
            }`}
          >
            ⚡ 底仓做T 作战与台账
          </button>
        </div>

        <div className="text-xs text-zinc-400">
          AR4.5-L3b 每日精准买卖调仓单请前往{' '}
          <Link to="/g3/desk" className="font-medium text-blue-400 hover:underline">
            实战交易台
          </Link>{' '}
          一键预填
        </div>
      </div>

      {activeTab === 'accounts' ? <BrokerAccounts embedded /> : <G3TTradeWorkbench />}
    </div>
  )
}
