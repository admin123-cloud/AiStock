import request from '@/utils/request'

export interface BrokerFunds {
  balance?: number | null
  frozen?: number | null
  available?: number | null
  withdrawable?: number | null
  market_value?: number | null
  total_assets?: number | null
  holding_profit?: number | null
  daily_profit?: number | null
  daily_profit_ratio?: number | null
}

export interface BrokerHolding {
  code: string
  name: string
  quantity?: number | null
  available_quantity?: number | null
  frozen_quantity?: number | null
  cost_price?: number | null
  current_price?: number | null
  floating_profit?: number | null
  profit_ratio?: number | null
  daily_profit?: number | null
  daily_profit_ratio?: number | null
}

export interface BrokerFill {
  code?: string | null
  name?: string | null
  date?: string | null
  time?: string | null
  side?: string | null
  quantity?: number | null
  price?: number | null
  amount?: number | null
  net_amount?: number | null
  contract_no?: string | null
  fill_no?: string | null
}

export interface BrokerAccountDetail {
  account_id: string
  label: string
  masked_id: string
  owner: string
  available: boolean
  status: string
  reason?: string | null
  rc: number
  generated_at?: string | null
  source_file?: string | null
  fallback_from_legacy?: boolean
  last_sync_status?: string | null
  last_sync_reason?: string | null
  credentials_touched: boolean
  funds_read_ok: boolean
  funds: BrokerFunds | null
  holdings: BrokerHolding[]
  holdings_count: number
  fills: BrokerFill[]
  fills_count: number
  fills_status?: string | null
  coverage?: {
    holdings_complete?: boolean
    fills_complete?: boolean
  }
  identity?: {
    before?: string | null
    after?: string | null
  }
}

export interface DesktopBridgeStatus {
  online: boolean
  heartbeat_at?: string | null
  age_seconds?: number | null
  state: string
  active_request_id?: string | null
  pending_count: number
  pending_ids: string[]
  latest_completed?: Record<string, unknown> | null
}

export interface AccountsOverviewResponse {
  schema: string
  credentials_touched: boolean
  accounts_total: number
  accounts_ready: number
  merged: {
    total_assets: number
    market_value: number
    balance: number
    available: number
    withdrawable: number
    frozen: number
    holding_profit: number
    daily_profit: number
    daily_profit_ratio?: number | null
    holdings_count: number
  }
  accounts: BrokerAccountDetail[]
  bridge: DesktopBridgeStatus
}

export interface SyncEnqueueResponse {
  status: string
  request_id: string
  accounts: string[]
  with_fills: boolean
  credentials_touched: boolean
  bridge: DesktopBridgeStatus
}

export function getAccountsOverview(): Promise<AccountsOverviewResponse> {
  return request.get('/accounts/overview') as unknown as Promise<AccountsOverviewResponse>
}

export function triggerAccountsSync(accounts?: string[], withFills = true): Promise<SyncEnqueueResponse> {
  return request.post('/accounts/sync', {
    accounts: accounts && accounts.length > 0 ? accounts : undefined,
    with_fills: withFills,
  }) as unknown as Promise<SyncEnqueueResponse>
}
