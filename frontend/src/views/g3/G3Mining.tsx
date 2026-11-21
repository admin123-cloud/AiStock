import { useCallback, useEffect, useState } from 'react'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/catalyst/table'
import { Card, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import {
  exportG3MiningContract,
  getG3MiningOverview,
  getG3MiningRunDetail,
  runG3MiningExperiment,
  type G3MiningCandidateResult,
  type G3MiningOverviewResponse,
  type G3MiningRunDetail,
} from '@/api/g3'

const REGIME_LABELS: Record<string, string> = {
  offensive: '主升进攻',
  extreme_offensive: '极端高潮',
  balanced: '横盘平衡',
  defensive: '防守退潮',
  extreme_panic: '极端恐慌',
}

const fmtSigned = (v?: number | null, digits = 2, suffix = '%'): string => {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  const n = Number(v)
  return `${n > 0 ? '+' : ''}${n.toFixed(digits)}${suffix}`
}

const fmtNum = (v?: number | null, digits = 2): string =>
  v != null && Number.isFinite(Number(v)) ? Number(v).toFixed(digits) : '—'

interface CustomCondRow {
  feature: string
  op: string
  value: string
}

export default function G3Mining() {
  const [overview, setOverview] = useState<G3MiningOverviewResponse | null>(null)
  const [activeRun, setActiveRun] = useState<G3MiningRunDetail | null>(null)
  const [selectedRuleId, setSelectedRuleId] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [running, setRunning] = useState(false)
  const [exportingRuleId, setExportingRuleId] = useState<string | null>(null)
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')

  // 挖掘任务参数
  const [templateId, setTemplateId] = useState('sector_rotation')
  const [startDate, setStartDate] = useState('2023-01-01')
  const [minAmtWan, setMinAmtWan] = useState('5000')
  const [enableTreeMiner, setEnableTreeMiner] = useState(true)
  const [tuneExitOnVal, setTuneExitOnVal] = useState(true)

  // 自定义假设编辑器状态
  const [customName, setCustomName] = useState('自定义主线低吸策略')
  const [customThesis, setCustomThesis] = useState('主线强势行业中低吸未大涨且未破位标的')
  const [customHoldDays, setCustomHoldDays] = useState('5')
  const [customStopLoss, setCustomStopLoss] = useState('-0.07')
  const [customTakeProfit, setCustomTakeProfit] = useState('0.12')
  const [customConds, setCustomConds] = useState<CustomCondRow[]>([
    { feature: 'industry_above_ma20_pct', op: '>=', value: '70' },
    { feature: 'rs_5d_vs_hs300', op: '<=', value: '1.0' },
    { feature: 'pos_20d', op: '>=', value: '0.25' },
  ])

  const loadOverview = useCallback(async (keepRunId?: string) => {
    setLoading(true)
    setError('')
    try {
      const res = await getG3MiningOverview()
      setOverview(res)
      if (keepRunId) {
        const detail = await getG3MiningRunDetail(keepRunId)
        setActiveRun(detail)
        setSelectedRuleId(detail.results[0]?.rule.rule_id || null)
      } else if (res.latest_run) {
        setActiveRun(res.latest_run)
        setSelectedRuleId(res.latest_run.results[0]?.rule.rule_id || null)
      }
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '加载策略挖掘工坊失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadOverview()
  }, [loadOverview])

  const handleSelectRun = async (runId: string) => {
    setLoading(true)
    setError('')
    try {
      const detail = await getG3MiningRunDetail(runId)
      setActiveRun(detail)
      setSelectedRuleId(detail.results[0]?.rule.rule_id || null)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '加载历史实验失败')
    } finally {
      setLoading(false)
    }
  }

  const handleRunMining = async () => {
    setRunning(true)
    setError('')
    setMsg('')
    try {
      const isCustom = templateId === 'custom_hypothesis'
      const parsedConds = isCustom
        ? customConds
            .map((c) => ({ feature: c.feature, op: c.op, value: Number(c.value) }))
            .filter((c) => c.feature && Number.isFinite(c.value))
        : undefined
      if (isCustom && (!parsedConds || parsedConds.length === 0)) {
        setError('自定义假设至少需要 1 条有效明文条件')
        setRunning(false)
        return
      }
      const res = await runG3MiningExperiment({
        template_id: templateId,
        start_date: startDate,
        min_amt_wan: Number(minAmtWan) || 5000,
        enable_tree_miner: enableTreeMiner,
        tune_exit_on_val: tuneExitOnVal,
        ...(isCustom
          ? {
              custom_name: customName,
              custom_thesis: customThesis,
              custom_conditions: parsedConds,
              custom_hold_days: Number(customHoldDays) || 5,
              custom_stop_loss_pct: Number(customStopLoss) || -0.07,
              custom_take_profit_pct: Number(customTakeProfit) || 0.12,
            }
          : {}),
      })
      setActiveRun(res)
      setSelectedRuleId(res.results[0]?.rule.rule_id || null)
      setMsg(
        `挖掘完成（${res.template_name}）：评估 ${res.summary.candidates_evaluated} 条候选，通过全部 5 道防过拟合硬门禁 ${res.summary.passed_all_gates} 条！`
      )
      await loadOverview(res.run_id)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '启动策略挖掘失败')
    } finally {
      setRunning(false)
    }
  }

  const handleExportContract = async (runId: string, ruleId: string) => {
    setExportingRuleId(ruleId)
    setError('')
    setMsg('')
    try {
      const res = await exportG3MiningContract({ run_id: runId, rule_id: ruleId })
      setMsg(`已导出影子观察合约 ${res.strategy_id}（status: candidate_shadow，零自动下单）`)
      await loadOverview(runId)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '导出影子合约失败')
    } finally {
      setExportingRuleId(null)
    }
  }

  const featureLabelMap: Record<string, string> = {}
  for (const f of overview?.features || []) {
    featureLabelMap[f.key] = f.label
  }

  const selectedItem: G3MiningCandidateResult | null =
    activeRun?.results.find((x) => x.rule.rule_id === selectedRuleId) || activeRun?.results[0] || null

  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 p-4 sm:p-6">
      <PageHeader
        title="G3 AI 策略挖掘工坊（Alpha Mining Studio）"
        subtitle="ClickHouse 只读全市场特征宽表 × 浅层决策树/AI假设双引擎 × 训练/验证/盲测样本外五道防过拟合硬门禁"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Badge color="emerald">ClickHouse 只读</Badge>
            <Badge color="blue">严格 T+1 零未来函数</Badge>
            <Badge color="amber">最高输出 candidate_shadow</Badge>
            <Button outline onClick={() => void loadOverview(activeRun?.run_id)} disabled={loading}>
              {loading ? '刷新中…' : '刷新记录'}
            </Button>
          </div>
        }
      />

      {error && (
        <div className="rounded-lg border border-red-500/40 bg-red-950/30 px-4 py-2.5 text-sm text-red-200">
          {error}
        </div>
      )}
      {msg && (
        <div className="rounded-lg border border-emerald-500/40 bg-emerald-950/30 px-4 py-2.5 text-sm text-emerald-200">
          {msg}
        </div>
      )}

      {/* 1. 一键挖掘控制台 */}
      <Card>
        <SectionTitle title="1. 选择挖掘模板 & 一键运行全市场闭环" />
        <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
          {(overview?.templates || []).map((tpl) => {
            const active = templateId === tpl.template_id
            return (
              <button
                key={tpl.template_id}
                type="button"
                onClick={() => setTemplateId(tpl.template_id)}
                className={`rounded-xl border p-3 text-left transition ${
                  active
                    ? 'border-blue-400 bg-blue-950/35 ring-1 ring-blue-400/60'
                    : 'border-zinc-800 bg-zinc-900/60 hover:border-zinc-700'
                }`}
              >
                <div className="flex items-center justify-between gap-1">
                  <span className="text-sm font-semibold text-white">{tpl.name}</span>
                  {active && <Badge color="blue">已选</Badge>}
                </div>
                <p className="mt-1.5 text-xs leading-relaxed text-zinc-400">{tpl.description}</p>
                <div className="mt-2 flex flex-wrap gap-1">
                  {tpl.target_regimes.map((rg) => (
                    <span
                      key={rg}
                      className="rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-300"
                    >
                      {REGIME_LABELS[rg] || rg}
                    </span>
                  ))}
                </div>
              </button>
            )
          })}
        </div>

        {/* 参数条 */}
        <div className="mt-4 flex flex-wrap items-end gap-4 border-t border-zinc-800 pt-3 text-xs">
          <div>
            <label className="block text-zinc-400">起始日期</label>
            <input
              type="text"
              value={startDate}
              onChange={(e) => setStartDate(e.target.value)}
              className="mt-1 w-32 rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 font-mono text-sm text-white"
            />
          </div>
          <div>
            <label className="block text-zinc-400">20日均成交额门槛（万元）</label>
            <input
              type="number"
              step="500"
              min="500"
              value={minAmtWan}
              onChange={(e) => setMinAmtWan(e.target.value)}
              className="mt-1 w-36 rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 font-mono text-sm text-white"
            />
          </div>
          <label className="flex items-center gap-2 pb-1.5 text-zinc-200">
            <input
              type="checkbox"
              checked={enableTreeMiner}
              onChange={(e) => setEnableTreeMiner(e.target.checked)}
              disabled={templateId === 'custom_hypothesis'}
              className="rounded border-zinc-600 bg-zinc-900"
            />
            启用 Pure-NumPy 决策树自动提纯
          </label>
          <label className="flex items-center gap-2 pb-1.5 text-zinc-200">
            <input
              type="checkbox"
              checked={tuneExitOnVal}
              onChange={(e) => setTuneExitOnVal(e.target.checked)}
              className="rounded border-zinc-600 bg-zinc-900"
            />
            在 Val 验证集自动择优出场参数
          </label>
          <div className="ml-auto">
            <Button onClick={() => void handleRunMining()} disabled={running}>
              {running ? '全市场挖掘与盲测中（约 10~25s）…' : '🚀 启动全市场策略挖掘'}
            </Button>
          </div>
        </div>

        {/* 自定义假设 DSL 构造器 */}
        {templateId === 'custom_hypothesis' && (
          <div className="mt-4 rounded-xl border border-blue-500/40 bg-blue-950/15 p-3.5 text-xs">
            <div className="mb-2 font-semibold text-blue-200">自定义交易假设（明文 DSL 条件）</div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-5">
              <div className="sm:col-span-2">
                <label className="block text-zinc-400">策略名称</label>
                <input
                  type="text"
                  value={customName}
                  onChange={(e) => setCustomName(e.target.value)}
                  className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-sm text-white"
                />
              </div>
              <div>
                <label className="block text-zinc-400">持仓天数(2~10)</label>
                <input
                  type="number"
                  min="2"
                  max="10"
                  value={customHoldDays}
                  onChange={(e) => setCustomHoldDays(e.target.value)}
                  className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 font-mono text-sm text-white"
                />
              </div>
              <div>
                <label className="block text-zinc-400">硬止损(如 -0.07)</label>
                <input
                  type="number"
                  step="0.01"
                  value={customStopLoss}
                  onChange={(e) => setCustomStopLoss(e.target.value)}
                  className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 font-mono text-sm text-white"
                />
              </div>
              <div>
                <label className="block text-zinc-400">止盈(如 0.12)</label>
                <input
                  type="number"
                  step="0.01"
                  value={customTakeProfit}
                  onChange={(e) => setCustomTakeProfit(e.target.value)}
                  className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 font-mono text-sm text-white"
                />
              </div>
            </div>
            <div className="mt-2">
              <label className="block text-zinc-400">交易逻辑说明</label>
              <input
                type="text"
                value={customThesis}
                onChange={(e) => setCustomThesis(e.target.value)}
                className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-sm text-white"
              />
            </div>

            <div className="mt-3 space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-zinc-300">入场条件（最多 5 条，满足 Gate 5 简洁度门禁）</span>
                {customConds.length < 5 && (
                  <Button
                    outline
                    onClick={() =>
                      setCustomConds((prev) => [
                        ...prev,
                        { feature: 'vol_ratio_5d', op: '>=', value: '1.2' },
                      ])
                    }
                  >
                    + 添加条件
                  </Button>
                )}
              </div>
              {customConds.map((row, idx) => (
                <div key={idx} className="flex flex-wrap items-center gap-2">
                  <select
                    value={row.feature}
                    onChange={(e) =>
                      setCustomConds((prev) =>
                        prev.map((x, i) => (i === idx ? { ...x, feature: e.target.value } : x))
                      )
                    }
                    className="rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-sm text-white"
                  >
                    {(overview?.features || []).map((f) => (
                      <option key={f.key} value={f.key}>
                        {f.label} ({f.key})
                      </option>
                    ))}
                  </select>
                  <select
                    value={row.op}
                    onChange={(e) =>
                      setCustomConds((prev) =>
                        prev.map((x, i) => (i === idx ? { ...x, op: e.target.value } : x))
                      )
                    }
                    className="w-20 rounded-lg border border-zinc-700 bg-zinc-900 px-2 py-1.5 font-mono text-sm text-white"
                  >
                    <option value=">=">&gt;=</option>
                    <option value="<=">&lt;=</option>
                    <option value=">">&gt;</option>
                    <option value="<">&lt;</option>
                    <option value="==">==</option>
                  </select>
                  <input
                    type="number"
                    step="0.1"
                    value={row.value}
                    onChange={(e) =>
                      setCustomConds((prev) =>
                        prev.map((x, i) => (i === idx ? { ...x, value: e.target.value } : x))
                      )
                    }
                    className="w-28 rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 font-mono text-sm text-white"
                  />
                  {customConds.length > 1 && (
                    <Button
                      plain
                      onClick={() => setCustomConds((prev) => prev.filter((_, i) => i !== idx))}
                    >
                      删除
                    </Button>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
      </Card>

      {/* 2. 已导出候选影子合约池 */}
      <Card>
        <SectionTitle
          title={`2. 已导出影子观察合约池（${overview?.exported_contracts.length || 0} 份 · candidate_shadow）`}
        />
        {!overview?.exported_contracts.length ? (
          <div className="py-4 text-sm text-zinc-400">
            暂无已导出合约；在下方的实验结果中，点击任意通过 5 道防过拟合门禁的策略「导出影子合约」即可沉淀到此池。
          </div>
        ) : (
          <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-2">
            {overview.exported_contracts.map((c) => (
              <div
                key={c.strategy_id}
                className="rounded-xl border border-emerald-500/35 bg-emerald-950/15 p-3.5 text-xs"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-semibold text-white">{c.display_name}</span>
                    <Badge color={c.all_gates_passed ? 'emerald' : 'amber'}>
                      {c.all_gates_passed ? 'PASS 5/5' : '研究候选'}
                    </Badge>
                    <Badge color="blue">{c.status}</Badge>
                  </div>
                  <span className="font-mono text-zinc-300">评分 {fmtNum(c.mining_score, 1)}</span>
                </div>
                <p className="mt-1.5 text-zinc-300">{c.thesis}</p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {c.readable_conditions.map((rc, i) => (
                    <span key={i} className="rounded bg-zinc-900/90 px-2 py-0.5 font-mono text-[11px] text-emerald-300">
                      {rc}
                    </span>
                  ))}
                </div>
                <div className="mt-2.5 flex flex-wrap items-center gap-4 border-t border-zinc-800/80 pt-2 font-mono text-[11px] text-zinc-300">
                  <span>OOS {c.oos_summary.trades_count ?? '—'} 笔</span>
                  <span>胜率 {fmtNum(c.oos_summary.win_rate_pct, 1)}%</span>
                  <span className="text-emerald-300">单笔均 {fmtSigned(c.oos_summary.avg_ret_pct)}</span>
                  <span className="text-blue-300">超额 {fmtSigned(c.oos_summary.excess_ret_pct, 1)}</span>
                  <span>回撤 {fmtNum(c.oos_summary.max_drawdown_pct, 1)}%</span>
                  <span>夏普 {fmtNum(c.oos_summary.sharpe, 2)}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* 3. 实验详情与候选排行榜 */}
      {activeRun && (
        <>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <SectionTitle title={`3. 挖掘实验报告 · ${activeRun.template_name}`} />
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-zinc-400">切换历史实验：</span>
              <select
                value={activeRun.run_id}
                onChange={(e) => void handleSelectRun(e.target.value)}
                className="rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1 text-xs font-mono text-white"
              >
                {(overview?.recent_runs || []).map((r) => (
                  <option key={r.run_id} value={r.run_id}>
                    {r.created_at?.slice(5, 16)} · {r.template_name} (PASS {r.summary?.passed_all_gates || 0}/{r.summary?.candidates_evaluated || 0})
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <StatCard
              label="流动性达标样本池"
              value={`${activeRun.universe.unique_stocks.toLocaleString()} 只`}
              sub={`≥${activeRun.universe.min_amt_20d_wan}万 · ${activeRun.universe.trade_days} 交易日`}
            />
            <StatCard
              label="OOS 盲测样本外区间"
              value={`${activeRun.splits.oos.from.slice(2)}`}
              sub={`至 ${activeRun.splits.oos.to}（绝不参与调参）`}
            />
            <StatCard
              label="通过 5 道硬门禁"
              value={`${activeRun.summary.passed_all_gates} / ${activeRun.summary.candidates_evaluated}`}
              sub="严格淘汰过拟合候选"
              tone={activeRun.summary.passed_all_gates > 0 ? 'emerald' : 'amber'}
            />
            <StatCard
              label="最高挖掘综合分"
              value={`${activeRun.summary.best_mining_score.toFixed(1)}`}
              sub={activeRun.summary.best_rule_id || '—'}
              tone={activeRun.summary.best_mining_score >= 65 ? 'emerald' : 'blue'}
            />
          </div>

          <Card>
            <SectionTitle title="候选策略排行榜（点击任意行查看 5 道门禁明细与 OOS 成交样本）" />
            <div className="mt-3 overflow-x-auto">
              <Table dense>
                <TableHead>
                  <TableRow>
                    <TableHeader>门禁</TableHeader>
                    <TableHeader>评分</TableHeader>
                    <TableHeader>策略 ID / 来源</TableHeader>
                    <TableHeader>IS 训练(均/笔)</TableHeader>
                    <TableHeader>Val 验证(均/笔)</TableHeader>
                    <TableHeader>OOS 盲测(均/胜率)</TableHeader>
                    <TableHeader>OOS 累计 / 超额</TableHeader>
                    <TableHeader>OOS 回撤 / 夏普</TableHeader>
                    <TableHeader>操作</TableHeader>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {activeRun.results.map((item) => {
                    const g = item.quality_gates
                    const isSelected = selectedItem?.rule.rule_id === item.rule.rule_id
                    return (
                      <TableRow
                        key={item.rule.rule_id}
                        className={isSelected ? 'bg-blue-950/35' : 'cursor-pointer hover:bg-zinc-900/70'}
                        onClick={() => setSelectedRuleId(item.rule.rule_id)}
                      >
                        <TableCell>
                          <Badge color={g.all_passed ? 'emerald' : g.passed_count >= 3 ? 'amber' : 'red'}>
                            {g.all_passed ? 'PASS 5/5' : `${g.passed_count}/5`}
                          </Badge>
                        </TableCell>
                        <TableCell className="font-mono text-xs font-semibold text-white">
                          {g.mining_score.toFixed(1)}
                        </TableCell>
                        <TableCell className="text-xs">
                          <div className="font-medium text-zinc-100">{item.rule.name}</div>
                          <div className="font-mono text-[11px] text-zinc-400">
                            {item.rule.rule_id} · {item.rule.source}
                          </div>
                        </TableCell>
                        <TableCell className="font-mono text-xs text-zinc-300">
                          {fmtSigned(item.segments.is.avg_ret_pct)} / {item.segments.is.trades_count}
                        </TableCell>
                        <TableCell className="font-mono text-xs text-zinc-300">
                          {fmtSigned(item.segments.val.avg_ret_pct)} / {item.segments.val.trades_count}
                        </TableCell>
                        <TableCell className="font-mono text-xs">
                          <span className={item.segments.oos.avg_ret_pct >= 0 ? 'text-emerald-300 font-semibold' : 'text-red-300'}>
                            {fmtSigned(item.segments.oos.avg_ret_pct)}
                          </span>{' '}
                          <span className="text-zinc-400">({item.segments.oos.win_rate_pct.toFixed(1)}%)</span>
                        </TableCell>
                        <TableCell className="font-mono text-xs">
                          <span className={item.segments.oos.cum_ret_pct >= 0 ? 'text-emerald-300' : 'text-red-300'}>
                            {fmtSigned(item.segments.oos.cum_ret_pct, 1)}
                          </span>{' '}
                          <span className="text-blue-300">({fmtSigned(item.segments.oos.excess_ret_pct, 1)})</span>
                        </TableCell>
                        <TableCell className="font-mono text-xs text-zinc-300">
                          {item.segments.oos.max_drawdown_pct.toFixed(1)}% / {item.segments.oos.sharpe.toFixed(2)}
                        </TableCell>
                        <TableCell>
                          <Button
                            outline
                            disabled={exportingRuleId === item.rule.rule_id}
                            onClick={(e: React.MouseEvent) => {
                              e.stopPropagation()
                              void handleExportContract(activeRun.run_id, item.rule.rule_id)
                            }}
                          >
                            {exportingRuleId === item.rule.rule_id ? '导出中…' : '导出影子合约'}
                          </Button>
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            </div>
          </Card>

          {/* 选中策略深度拆解 */}
          {selectedItem && (
            <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
              <Card>
                <SectionTitle title={`明文规则与五道防过拟合硬门禁 · ${selectedItem.rule.rule_id}`} />
                <p className="mt-2 text-xs text-zinc-300">{selectedItem.rule.thesis}</p>
                <div className="mt-2.5 flex flex-wrap gap-1.5">
                  {selectedItem.rule.entry_conditions.map((c, idx) => (
                    <span
                      key={idx}
                      className="rounded-md border border-blue-500/30 bg-blue-950/30 px-2 py-1 font-mono text-xs text-blue-200"
                    >
                      {featureLabelMap[c.feature] || c.feature} {c.op} {c.value}
                    </span>
                  ))}
                  <span className="rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-xs text-zinc-300">
                    持{selectedItem.rule.exit_policy.hold_days}d · 止损
                    {(selectedItem.rule.exit_policy.stop_loss_pct * 100).toFixed(0)}% · 止盈+
                    {(selectedItem.rule.exit_policy.take_profit_pct * 100).toFixed(0)}%
                  </span>
                </div>

                <div className="mt-3 space-y-1.5">
                  {selectedItem.quality_gates.gates.map((g) => (
                    <div
                      key={g.gate}
                      className={`flex items-start justify-between gap-2 rounded-lg border px-3 py-2 text-xs ${
                        g.passed
                          ? 'border-emerald-500/30 bg-emerald-950/20 text-emerald-100'
                          : 'border-red-500/30 bg-red-950/20 text-red-100'
                      }`}
                    >
                      <div>
                        <span className="font-semibold">
                          {g.passed ? '✅' : '⛔'} {g.label}
                        </span>
                        <div className="mt-0.5 text-[11px] opacity-90">{g.detail}</div>
                      </div>
                    </div>
                  ))}
                </div>

                {selectedItem.quality_gates.failure_reasons.length > 0 && (
                  <div className="mt-3 rounded-lg border border-amber-500/30 bg-amber-950/20 p-2.5 text-xs text-amber-200">
                    <div className="font-semibold">AI 迭代淘汰归因：</div>
                    <ul className="mt-1 list-disc pl-4 space-y-0.5">
                      {selectedItem.quality_gates.failure_reasons.map((r, i) => (
                        <li key={i}>{r}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </Card>

              <Card>
                <SectionTitle title="OOS 盲测样本外最近 10 笔代表成交" />
                {!selectedItem.oos_sample_trades.length ? (
                  <div className="py-6 text-center text-sm text-zinc-400">OOS 区间无触发交易</div>
                ) : (
                  <div className="mt-3 overflow-x-auto">
                    <Table dense>
                      <TableHead>
                        <TableRow>
                          <TableHeader>信号日</TableHeader>
                          <TableHeader>标的</TableHeader>
                          <TableHeader>行业</TableHeader>
                          <TableHeader>T+1开盘买</TableHeader>
                          <TableHeader>持有</TableHeader>
                          <TableHeader>扣费净收益</TableHeader>
                        </TableRow>
                      </TableHead>
                      <TableBody>
                        {selectedItem.oos_sample_trades.map((t, i) => (
                          <TableRow key={`${t.trade_date}-${t.code}-${i}`}>
                            <TableCell className="font-mono text-xs text-zinc-300">{t.trade_date}</TableCell>
                            <TableCell className="text-xs text-zinc-100">
                              {t.name} <span className="font-mono text-zinc-400">{t.code}</span>
                            </TableCell>
                            <TableCell className="text-xs text-zinc-400">{t.industry || '—'}</TableCell>
                            <TableCell className="font-mono text-xs">{t.entry_open.toFixed(2)}</TableCell>
                            <TableCell className="font-mono text-xs">{t.exit_day}d</TableCell>
                            <TableCell
                              className={`font-mono text-xs font-semibold ${
                                t.net_ret_pct >= 0 ? 'text-rose-400' : 'text-emerald-400'
                              }`}
                            >
                              {fmtSigned(t.net_ret_pct)}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                )}
              </Card>
            </div>
          )}
        </>
      )}
    </div>
  )
}
