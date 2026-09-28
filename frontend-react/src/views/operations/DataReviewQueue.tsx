import { useCallback, useEffect, useState } from 'react'
import {
  dateTime,
  getDataReviewQueue,
  postDataReviewAction,
  statusLabel,
  statusTone,
  type DataReviewQueue,
  type DataReviewQueueItem,
} from '@/api/operations'
import { Dialog, DialogActions, DialogBody, DialogDescription, DialogTitle } from '@/components/catalyst/dialog'
import { Textarea } from '@/components/catalyst/textarea'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Select } from '@/components/catalyst/select'
import { Alert, AlertDescription, AlertTitle } from '@/components/catalyst/alert'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/catalyst/table'
import {
  Card,
  EmptyState,
  PageHeader,
  PageLoading,
  SectionTitle,
} from '@/components/ui'

/** 状态值 → Catalyst Badge color（danger/success/warning → red/green/amber） */
const badgeColor = { danger: 'red', success: 'green', warning: 'amber' } as const

const ROW_KEYS = [
  'stock_amount_missing',
  'index_unit_candidates',
  'index_source_conflicts',
  'index_amount_volume_pending',
] as const

function joinDates(dates: unknown): string {
  return Array.isArray(dates) ? dates.join('、') : ''
}

/** 逐条明细表：按生产版 item.key 选择列结构 */
function SampleTable({ item }: { item: DataReviewQueueItem }) {
  const samples = item.samples ?? []
  const key = item.key

  if (key === 'market_status_candidates') {
    return (
      <Table dense className="mt-1">
        <TableHead>
          <TableRow>
            <TableHeader>证券</TableHeader>
            <TableHeader>命中缺失日</TableHeader>
            <TableHeader>停牌起始</TableHeader>
            <TableHeader>预计复牌</TableHeader>
            <TableHeader>原因</TableHeader>
            <TableHeader>审核状态</TableHeader>
            <TableHeader>公告证据</TableHeader>
          </TableRow>
        </TableHead>
        <TableBody>
          {samples.map((row, i) => {
            const r = row as Record<string, unknown>
            return (
              <TableRow key={i}>
                <TableCell>{String(r.code ?? '')}</TableCell>
                <TableCell>{String(r.missing_code_dates ?? '')}</TableCell>
                <TableCell>{String(r.start_date ?? '')}</TableCell>
                <TableCell>{String(r.expected_resume_date ?? '')}</TableCell>
                <TableCell>{String(r.reason ?? '')}</TableCell>
                <TableCell>{String(r.review_state ?? '')}</TableCell>
                <TableCell>
                  {typeof r.evidence_url === 'string' && r.evidence_url ? (
                    <a
                      href={r.evidence_url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-indigo-600 hover:underline dark:text-indigo-400"
                    >
                      打开
                    </a>
                  ) : (
                    '待核'
                  )}
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    )
  }

  if (key === 'qmt_source_absent') {
    return (
      <Table dense className="mt-1">
        <TableHead>
          <TableRow>
            <TableHeader>证券</TableHeader>
            <TableHeader>待补日</TableHeader>
            <TableHeader>具体日期</TableHeader>
            <TableHeader>首日</TableHeader>
            <TableHeader>末日</TableHeader>
          </TableRow>
        </TableHead>
        <TableBody>
          {samples.map((row, i) => {
            const r = row as Record<string, unknown>
            return (
              <TableRow key={i}>
                <TableCell>{String(r.code ?? '')}</TableCell>
                <TableCell>{String(r.missing_days ?? '')}</TableCell>
                <TableCell>{joinDates(r.missing_dates)}</TableCell>
                <TableCell>{String(r.first_missing ?? '')}</TableCell>
                <TableCell>{String(r.last_missing ?? '')}</TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    )
  }

  if ((ROW_KEYS as readonly string[]).includes(key)) {
    return (
      <Table dense className="mt-1">
        <TableHead>
          <TableRow>
            <TableHeader>证券/指数</TableHeader>
            <TableHeader>类型</TableHeader>
            <TableHeader>差异分类</TableHeader>
            <TableHeader>键数</TableHeader>
            <TableHeader>日期</TableHeader>
            <TableHeader>库金额样值</TableHeader>
            <TableHeader>TDX金额样值</TableHeader>
          </TableRow>
        </TableHead>
        <TableBody>
          {samples.map((row, i) => {
            const r = row as Record<string, unknown>
            return (
              <TableRow key={i}>
                <TableCell>{String(r.code ?? '')}</TableCell>
                <TableCell>{String(r.instrument_type ?? '')}</TableCell>
                <TableCell>{String(r.classification ?? '')}</TableCell>
                <TableCell>{String(r.rows ?? '')}</TableCell>
                <TableCell>{joinDates(r.trade_dates)}</TableCell>
                <TableCell>{String(r.db_amount ?? '')}</TableCell>
                <TableCell>{String(r.source_amount ?? '')}</TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    )
  }

  if (key === 'minute60_storage') {
    return (
      <Table dense className="mt-1">
        <TableHead>
          <TableRow>
            <TableHeader>发现时间</TableHeader>
            <TableHeader>物理分片</TableHeader>
            <TableHeader>校验错误</TableHeader>
          </TableRow>
        </TableHead>
        <TableBody>
          {samples.map((row, i) => {
            const r = row as Record<string, unknown>
            return (
              <TableRow key={i}>
                <TableCell>{String(r.occurred_at ?? '')}</TableCell>
                <TableCell>{String(r.part ?? '')}</TableCell>
                <TableCell>{String(r.message ?? '')}</TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    )
  }

  // 默认：缺失日明细
  return (
    <Table dense className="mt-1">
      <TableHead>
        <TableRow>
          <TableHeader>证券</TableHeader>
          <TableHeader>K线级别</TableHeader>
          <TableHeader>缺失日</TableHeader>
          <TableHeader>具体缺失日期</TableHeader>
          <TableHeader>起始日期</TableHeader>
          <TableHeader>结束日期</TableHeader>
        </TableRow>
      </TableHead>
      <TableBody>
        {samples.map((row, i) => {
          const r = row as Record<string, unknown>
          return (
            <TableRow key={i}>
              <TableCell>{String(r.code ?? '')}</TableCell>
              <TableCell>{String(r.period ?? r.kline_level ?? '—')}</TableCell>
              <TableCell>{String(r.missing_days ?? r.missing_count ?? '')}</TableCell>
              <TableCell>{joinDates(r.missing_dates) || String(r.date ?? r.trade_date ?? '')}</TableCell>
              <TableCell>{String(r.first_missing ?? r.date ?? r.trade_date ?? '')}</TableCell>
              <TableCell>{String(r.last_missing ?? r.date ?? r.trade_date ?? '')}</TableCell>
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
  )
}

async function sha256(value: string): Promise<string> {
  const bytes = new TextEncoder().encode(value)
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

const ACTION_LABELS: Record<string, string> = {
  confirm_suspension: '确认停牌',
  no_trade: '确认无交易',
  non_stock: '确认非股票',
  request_5m_repair: '申请5分钟修复',
  pull_qmt: '申请5分钟修复（QMT→TDX兜底）',
  pull_tdx: '申请5分钟修复（TDX→QMT回写）',
  keep_pending: '暂不处理',
  mark_reviewed: '已审核',
}

/** 单个审核项卡片：标题 + 状态徽章 + 证据/建议 + 逐条明细 */
function ReviewItem({ item, queueFingerprint, onAction }: { item: DataReviewQueueItem; queueFingerprint?: string; onAction: (action: string) => void }) {
  const samples = item.samples ?? []
  const isStringSamples = samples.length > 0 && typeof samples[0] === 'string'
  const reviewed = Boolean(item.reviewed || item.review_status === 'reviewed')
  return (
    <Card>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">{item.title}</h2>
          <p className="mt-0.5 text-sm text-zinc-500 dark:text-zinc-400">{item.finding}</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Badge color={badgeColor[statusTone(item.status)]}>{statusLabel(item.status)}</Badge>
          {reviewed && <Badge color="green">已审核</Badge>}
          <span className="text-lg font-bold tabular-nums text-zinc-900 dark:text-white">
            {item.count}
          </span>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-zinc-500 dark:text-zinc-400">
        {item.issue_type && <Badge color="zinc">{item.issue_type}</Badge>}
        <span>动作状态：{item.action_state || '待审核'}</span>
        {item.pull_result && <span>拉取复核：{item.pull_result}</span>}
        <span>版本：{item.item_version ?? '—'}</span>
        <span className="break-all">证据指纹：{item.evidence_fingerprint || '—'}</span>
      </div>

      {item.reason_detail && Object.keys(item.reason_detail).length > 0 && (
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-zinc-500 dark:text-zinc-400">
          {Object.entries(item.reason_detail)
            .filter(([key]) => key !== 'issue_type' && key !== 'source_reason')
            .map(([key, value]) => (
              <span key={key}>
                {key}: {value == null || value === '' ? '—' : String(value)}
              </span>
            ))}
        </div>
      )}

      <div className="mt-3 grid gap-4 md:grid-cols-2">
        <div>
          <h3 className="mb-1 text-sm font-medium text-zinc-700 dark:text-zinc-300">已确认的证据</h3>
          {item.evidence?.length ? (
            <ul className="list-disc space-y-1 pl-5 text-sm text-zinc-600 dark:text-zinc-400">
              {item.evidence.map((line, i) => (
                <li key={i}>{line}</li>
              ))}
            </ul>
          ) : (
            <EmptyState text="暂无证据" className="py-4" />
          )}
        </div>
        <div>
          <h3 className="mb-1 text-sm font-medium text-zinc-700 dark:text-zinc-300">建议审核</h3>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">{item.recommendation}</p>
        </div>
      </div>

      {samples.length > 0 && (
        <div className="mt-4">
          <h3 className="mb-2 text-sm font-medium text-zinc-700 dark:text-zinc-300">逐条明细</h3>
          {isStringSamples ? (
            <div className="flex flex-wrap gap-1.5">
              {(samples as string[]).map((s, i) => (
                <Badge key={i} color="zinc">
                  {s}
                </Badge>
              ))}
            </div>
          ) : (
            <SampleTable item={item} />
          )}
        </div>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-2">
        {!reviewed && (
          <Button outline onClick={() => onAction('mark_reviewed')}>
            已审核
          </Button>
        )}
        {Array.from(new Set((item.allowed_actions ?? []).map((action) => action === 'pull_qmt' || action === 'pull_tdx' ? 'request_5m_repair' : action))).map((action) => (
          action === 'mark_reviewed' ? null : (
          <Button key={action} outline onClick={() => onAction(action)}>
            {ACTION_LABELS[action] || action}
          </Button>
          )
        ))}
        {!item.allowed_actions?.length && <span className="text-xs text-zinc-400">当前无可用动作</span>}
      </div>
    </Card>
  )
}

/** 数据审核队列：按证券/日期逐条处置，并将业务豁免写入下一轮审计输入。 */
export default function DataReviewQueue() {
  const [queue, setQueue] = useState<DataReviewQueue>({ items: [] })
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [actionItem, setActionItem] = useState<DataReviewQueueItem | null>(null)
  const [action, setAction] = useState('')
  const [reason, setReason] = useState('')
  const [evidenceRefs, setEvidenceRefs] = useState('')
  const [actionLoading, setActionLoading] = useState(false)
  const [actionResult, setActionResult] = useState<Record<string, string>>({})
  const [page, setPage] = useState(0)
  const [pageSize, setPageSize] = useState(20)
  const [issueType, setIssueType] = useState('')
  const [reviewStatus, setReviewStatus] = useState<'all' | 'reviewed' | 'unreviewed'>('unreviewed')

  const ISSUE_TYPE_LABELS: Record<string, string> = {
    purchased_5m_daily_snapshot_reconciliation: '购买5m/日线快照对账差异',
    project_daily_missing_for_5m: '项目日线缺失（5m已聚合）',
    source_5m_incomplete_bars: '购买源 5m 结构不完整',
  }
  const submitAction = async (target = actionItem, selectedAction = action, selectedReason = reason, refs = evidenceRefs) => {
    const currentItem = target
    const currentAction = selectedAction
    const currentReason = selectedReason.trim()
    if (!currentItem || !currentAction || !currentReason || actionLoading) return
    setActionLoading(true)
    setError('')
    try {
      const evidence = await Promise.all(refs.split(/[\n,]/).map((value) => value.trim()).filter(Boolean).map(async (ref) => ({ ref, sha256: await sha256(ref) })))
      const evidenceHash = await sha256(JSON.stringify({ evidence_refs: evidence, reason: currentReason }))
      const response = await postDataReviewAction({
        item_key: currentItem.key,
        review_key: currentItem.key,
        action: currentAction,
        expected_queue_fingerprint: queue.queue_fingerprint,
        expected_item_version: currentItem.item_version,
        reason: currentReason.trim(),
        evidence_refs: evidence,
        evidence_hash: evidenceHash,
        idempotency_key: crypto.randomUUID(),
        confirm_token: queue.queue_fingerprint,
        fingerprint: queue.queue_fingerprint,
        contract_version: String(currentItem.item_version ?? 'unknown'),
        payload: { reason: currentReason.trim(), evidence_refs: evidence },
      })
      setActionResult((current) => ({ ...current, [currentItem.key]: response.detail || (response.action_state === 'recorded' ? '仅记录了请求，尚未执行拉取，缺失是否解决未验证' : response.action_state) || '尚未验证' }))
      setActionItem(null)
      await load()
    } catch (e) {
      const err = e as { response?: { status?: number; data?: { detail?: string } }; message?: string }
      const status = err.response?.status
      const prefix = status === 400 ? '请求参数无效' : status === 403 ? '没有执行该审核动作的权限' : status === 409 ? '版本已变化，请刷新后重试' : status === 423 ? '该项目当前被锁定' : status === 422 ? '理由或证据引用未通过校验' : '操作失败'
      setError(`${prefix}${err.response?.data?.detail ? `：${err.response.data.detail}` : ''}`)
    } finally {
      setActionLoading(false)
    }
  }

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
        setQueue(await getDataReviewQueue({ limit: pageSize, offset: page * pageSize, issue_type: issueType || null, review_status: reviewStatus }))
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }, [page, pageSize, issueType, reviewStatus])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    setPage(0)
  }, [issueType, pageSize, reviewStatus])

  const summary = queue.summary
  const items = queue.items ?? []
  const sources = queue.sources ?? {}

  return (
    <div>
      <PageHeader
        title="数据审核队列"
        subtitle="每行对应一个证券和一个具体缺失日期，直接查看缺失类型与原因；已审核只记录审核状态，原始缺失数据和队列记录继续保留。"
        actions={
          <>
            <Badge color="amber">需人工确认</Badge>
            <Button outline disabled={loading} onClick={() => void load()}>
              {loading ? '加载中…' : '刷新证据'}
            </Button>
          </>
        }
      />

      {error && (
        <Alert color="red" role="alert" className="mb-4">
          <div>
            <AlertTitle>加载失败</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </div>
        </Alert>
      )}

      {loading && !items.length ? (
        <PageLoading text="加载审核队列…" />
      ) : (
        <>
          <Card className="mb-4">
            <div className="mb-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
              <div>
                <div className="text-xs text-zinc-500 dark:text-zinc-400">待审核项目</div>
                <div className="text-2xl font-bold tabular-nums text-zinc-900 dark:text-white">
                  {summary?.pending_count ?? '—'}
                </div>
              </div>
              <div>
                <div className="text-xs text-zinc-500 dark:text-zinc-400">阻断项</div>
                <div className="text-2xl font-bold tabular-nums text-zinc-900 dark:text-white">
                  {summary?.blocked_count ?? '—'}
                </div>
              </div>
              <div>
                <div className="text-xs text-zinc-500 dark:text-zinc-400">审核状态</div>
                <Select
                  className="mt-1"
                  value={reviewStatus}
                  onChange={(event) => setReviewStatus(event.target.value as 'all' | 'reviewed' | 'unreviewed')}
                >
                  <option value="unreviewed">未审核（{summary?.unreviewed_count ?? '—'}）</option>
                  <option value="reviewed">已审核（{summary?.reviewed_count ?? '—'}）</option>
                  <option value="all">全部</option>
                </Select>
              </div>
              <div>
                <div className="text-xs text-zinc-500 dark:text-zinc-400">生成时间</div>
                <div className="text-sm font-medium text-zinc-900 dark:text-white">
                  {dateTime(queue.generated_at)}
                </div>
              </div>
              <div>
                <div className="text-xs text-zinc-500 dark:text-zinc-400">类型筛选</div>
                <Select
                  className="mt-1"
                  value={issueType}
                  onChange={(event) => setIssueType(event.target.value)}
                >
                  <option value="">全部类型</option>
                  {Object.entries(summary?.issue_type_counts ?? {}).map(([type, count]) => (
                    <option key={type} value={type}>
                      {ISSUE_TYPE_LABELS[type] || type}（{count}）
                    </option>
                  ))}
                </Select>
              </div>
              <div>
                <div className="text-xs text-zinc-500 dark:text-zinc-400">每页条数</div>
                <Select
                  className="mt-1"
                  value={String(pageSize)}
                  onChange={(event) => setPageSize(Number(event.target.value))}
                >
                  {[20, 50, 100, 200].map((size) => (
                    <option key={size} value={size}>{size}</option>
                  ))}
                </Select>
              </div>
            </div>
            <Alert color="amber">
              <div>
                <AlertTitle>审核结果会进入后续审计</AlertTitle>
                <AlertDescription>
                  “已审核”只记录本条审核结论，不补造、不删除缺失数据，也不会把记录移出队列；申请修复只进入受控修复队列，不直接改行情表。
                </AlertDescription>
              </div>
            </Alert>
          </Card>

          {items.length ? (
            <div className="mb-4 space-y-4">
              {items.map((item) => (
                <div key={item.key}>
                <ReviewItem
                  key={item.key}
                  item={item}
                  onAction={(nextAction) => {
                    if (nextAction === 'mark_reviewed') {
                      void submitAction(item, 'mark_reviewed', '用户确认：该日期数据保持缺失，不补造数据；本条记录已完成审核。', '')
                      return
                    }
                    if (nextAction === 'request_5m_repair' || nextAction === 'pull_qmt' || nextAction === 'pull_tdx') {
                      void submitAction(item, 'request_5m_repair', '申请5分钟修复：依次从QMT和TDX拉取，有数据即补库，无数据返回两源均无数据', '')
                      return
                    }
                    setActionItem(item)
                    setAction(nextAction)
                    setReason('')
                    setEvidenceRefs('')
                  }}
                />
                {actionResult[item.key] && <p className="mt-2 text-sm text-amber-600">重新拉取结果：{actionResult[item.key]}</p>}
                </div>
              ))}
            </div>
          ) : (
            <EmptyState text="当前没有待人工审核的项目" className="mb-4" />
          )}

          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <span className="text-sm text-zinc-500 dark:text-zinc-400">
              {(() => {
                const total = Number(summary?.pending_count ?? 0)
                if (!total) return '共 0 项'
                const from = (page * pageSize) + 1
                const to = Math.min(total, (page + 1) * pageSize)
                return `共 ${total.toLocaleString()} 项 · 当前 ${from.toLocaleString()}-${to.toLocaleString()}`
              })()}
            </span>
            <div className="flex items-center gap-2">
              <Button outline disabled={loading || page <= 0} onClick={() => setPage((p) => Math.max(0, p - 1))}>
                上一页
              </Button>
              <Button
                outline
                disabled={loading || (page + 1) * pageSize >= Number(summary?.pending_count ?? 0)}
                onClick={() => setPage((p) => p + 1)}
              >
                下一页
              </Button>
            </div>
          </div>

          <Card>
            <SectionTitle title="证据文件" />
            {Object.keys(sources).length ? (
              <div className="space-y-1">
                {Object.entries(sources).map(([key, path]) => (
                  <p key={key} className="text-xs break-all text-zinc-500 dark:text-zinc-400">
                    {key}：{typeof path === 'string' ? path : path?.path ? `${path.path}（SHA256: ${path.sha256 || '—'}）` : '当前没有可读证据'}
                  </p>
                ))}
              </div>
            ) : (
              <EmptyState text="当前没有可读证据" className="py-4" />
            )}
          </Card>
        </>
      )}

      <Dialog open={Boolean(actionItem)} onClose={() => !actionLoading && setActionItem(null)}>
        <DialogTitle>{actionItem ? ACTION_LABELS[action] || action : '审核操作'}</DialogTitle>
        <DialogDescription>
          {actionItem?.title || ''}。提交前请填写理由，并引用本次审核使用的证据。
        </DialogDescription>
        <DialogBody>
          <label className="block text-sm font-medium">理由（必填）</label>
          <Textarea value={reason} onChange={(event) => setReason(event.target.value)} className="mt-1" rows={4} />
          <label className="mt-4 block text-sm font-medium">证据引用（可选，每行一条）</label>
          <Textarea value={evidenceRefs} onChange={(event) => setEvidenceRefs(event.target.value)} className="mt-1" rows={3} />
        </DialogBody>
        <DialogActions>
          <Button outline disabled={actionLoading} onClick={() => setActionItem(null)}>取消</Button>
          <Button disabled={actionLoading || !reason.trim()} onClick={() => void submitAction()}>
            {actionLoading ? '提交中…' : '确认提交'}
          </Button>
        </DialogActions>
      </Dialog>
    </div>
  )
}
