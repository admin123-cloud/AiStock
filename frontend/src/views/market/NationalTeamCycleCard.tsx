import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import type * as echarts from 'echarts'
import { EChart } from '@/components/EChart'
import {
  getG3AR4NationalTeamCycle,
  syncG3AR4MacroData,
  type G3AR4NationalTeamCycleResponse,
} from '@/api/g3'

interface NationalTeamCycleCardProps {
  dark: boolean
}

const YEAR_OPTIONS = [
  { label: '近1年', value: 1 },
  { label: '近2年', value: 2 },
  { label: '近3年', value: 3 },
  { label: '全周期', value: 5 },
]

const fmtSigned = (v?: number | null, digits = 1, suffix = ''): string => {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  const n = Number(v)
  return `${n > 0 ? '+' : ''}${n.toFixed(digits)}${suffix}`
}

export default function NationalTeamCycleCard({ dark }: NationalTeamCycleCardProps) {
  const [years, setYears] = useState<number>(3)
  const [report, setReport] = useState<G3AR4NationalTeamCycleResponse | null>(null)
  const [loading, setLoading] = useState<boolean>(false)
  const [syncing, setSyncing] = useState<boolean>(false)
  const [error, setError] = useState<string>('')
  const [notice, setNotice] = useState<string>('')
  const [showBreakdown, setShowBreakdown] = useState<boolean>(false)

  const loadData = useCallback(async (targetYears: number) => {
    setLoading(true)
    setError('')
    try {
      const res = await getG3AR4NationalTeamCycle(targetYears)
      setReport(res)
      if (!res.available && res.error) {
        setError(res.error)
      }
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '加载国家队持仓周期数据失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadData(years)
  }, [years, loadData])

  const handleSync = async () => {
    setSyncing(true)
    setError('')
    setNotice('')
    try {
      await syncG3AR4MacroData()
      setNotice('已增量同步上交所官方 ETF 份额与最新宏观 ERP 数据。')
      await loadData(years)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setError(err.response?.data?.detail || err.message || '同步国家队 ETF 份额数据失败')
    } finally {
      setSyncing(false)
    }
  }

  const latest = report?.latest
  const decision = latest?.decision

  const phaseBadgeClass = useMemo(() => {
    const tone = decision?.tone || 'blue'
    if (tone === 'red') {
      return 'border-red-500/40 bg-red-500/15 text-red-700 dark:text-red-300'
    }
    if (tone === 'emerald') {
      return 'border-emerald-500/40 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300'
    }
    if (tone === 'amber') {
      return 'border-amber-500/40 bg-amber-500/15 text-amber-700 dark:text-amber-300'
    }
    return 'border-sky-500/40 bg-sky-500/15 text-sky-700 dark:text-sky-300'
  }, [decision?.tone])

  const chartOption = useMemo<echarts.EChartsCoreOption | null>(() => {
    const s = report?.series
    if (!s || !s.dates || s.dates.length === 0) return null

    const axisLabelColor = dark ? '#94a3b8' : '#52525b'
    const splitLineColor = dark ? 'rgba(148,163,184,0.14)' : 'rgba(161,161,170,0.22)'
    const hs300Color = dark ? '#38bdf8' : '#0284c7'
    const pctileColor = dark ? '#fbbf24' : '#d97706'
    const erpColor = dark ? '#c084fc' : '#7c3aed'

    // 周期色带（重仓托底 / 低位派发 / ERP逃顶）
    const zoneMarkAreas = (report?.cycle_zones || []).map((z) => {
      const color =
        z.kind === 'overheat'
          ? dark
            ? 'rgba(239, 68, 68, 0.20)'
            : 'rgba(239, 68, 68, 0.14)'
          : z.kind === 'heavy'
            ? dark
              ? 'rgba(16, 185, 129, 0.14)'
              : 'rgba(16, 185, 129, 0.12)'
            : dark
              ? 'rgba(245, 158, 11, 0.13)'
              : 'rgba(245, 158, 11, 0.11)'
      return [
        { xAxis: z.start, itemStyle: { color } },
        { xAxis: z.end },
      ]
    })

    // 救市脉冲标注点（标注在沪深300曲线对应日期）
    const pulseMarkPoints = (report?.pulse_events || []).map((ev) => ({
      name: ev.label,
      coord: [ev.date, ev.hs300_close],
      value: ev.label,
      symbol: 'pin',
      symbolSize: 38,
      itemStyle: { color: '#e11d48' },
      label: {
        color: '#fff',
        fontSize: 9,
        fontWeight: 700,
        formatter: ev.label,
      },
    }))

    const scales = s.hs300_top3_scale_yi || []

    return {
      animationDuration: 350,
      legend: {
        top: 2,
        left: 'center',
        itemWidth: 12,
        itemHeight: 8,
        textStyle: { color: dark ? '#cbd5e1' : '#3f3f46', fontSize: 11 },
        data: ['沪深300', '国家队250日分位(%)', '6大宽基5日净申购(亿)', '沪深300 ERP(%)'],
      },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross', link: [{ xAxisIndex: 'all' }] },
        backgroundColor: dark ? 'rgba(15,23,42,0.94)' : 'rgba(255,255,255,0.96)',
        borderColor: dark ? 'rgba(148,163,184,0.3)' : 'rgba(161,161,170,0.4)',
        textStyle: { color: dark ? '#e2e8f0' : '#18181b', fontSize: 12 },
        formatter: (params: unknown) => {
          const arr = Array.isArray(params) ? params : []
          if (!arr.length) return ''
          const first = arr[0] as { dataIndex?: number; axisValue?: string }
          const idx = typeof first.dataIndex === 'number' ? first.dataIndex : 0
          const dt = first.axisValue || s.dates[idx] || ''
          const hs = s.hs300_close[idx]
          const pc = s.nt_pctile_250d[idx]
          const sc = scales[idx]
          const inf = s.broad6_inflow_5d_yi[idx]
          const erp = s.erp_pct[idx]
          const zoneLabel =
            erp > 0 && erp < 5.0
              ? '<span style="color:#ef4444;font-weight:700">ERP逃顶警戒(&lt;5%) · 进攻股仓0%</span>'
              : pc >= 70
                ? '<span style="color:#10b981;font-weight:700">重仓托底期(≥70%) · 进攻股仓65%</span>'
                : pc < 30
                  ? '<span style="color:#f59e0b;font-weight:700">低位派发期(&lt;30%) · 进攻股仓35%</span>'
                  : '<span style="color:#38bdf8;font-weight:600">中性平衡期 · 标准股仓50%</span>'
          return [
            `<div style="font-weight:700;margin-bottom:4px">${dt} &nbsp; ${zoneLabel}</div>`,
            `<div>沪深300：<b>${hs?.toFixed(2) ?? '—'}</b></div>`,
            `<div>国家队250日分位：<b>${pc?.toFixed(1) ?? '—'}%</b>（3大300ETF规模 <b>${sc?.toFixed(1) ?? '—'}亿</b>）</div>`,
            `<div>6大宽基5日纯份额净申购：<b style="color:${inf >= 0 ? '#ef4444' : '#10b981'}">${inf >= 0 ? '+' : ''}${inf?.toFixed(1) ?? '—'} 亿</b></div>`,
            `<div>沪深300 ERP：<b style="color:${erp < 5 ? '#ef4444' : '#a855f7'}">${erp?.toFixed(2) ?? '—'}%</b></div>`,
          ].join('')
        },
      },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: [
        { left: 54, right: 54, top: 38, height: '48%' },
        { left: 54, right: 54, top: '65%', height: '22%' },
      ],
      xAxis: [
        {
          type: 'category',
          gridIndex: 0,
          data: s.dates,
          boundaryGap: false,
          axisLabel: { show: false },
          axisLine: { lineStyle: { color: splitLineColor } },
        },
        {
          type: 'category',
          gridIndex: 1,
          data: s.dates,
          boundaryGap: false,
          axisLabel: { color: axisLabelColor, fontSize: 10 },
          axisLine: { lineStyle: { color: splitLineColor } },
        },
      ],
      yAxis: [
        {
          type: 'value',
          gridIndex: 0,
          name: '沪深300',
          scale: true,
          nameTextStyle: { color: hs300Color, fontSize: 10 },
          axisLabel: { color: axisLabelColor, fontSize: 10 },
          splitLine: { lineStyle: { color: splitLineColor } },
        },
        {
          type: 'value',
          gridIndex: 0,
          name: '持仓分位%',
          min: 0,
          max: 100,
          interval: 25,
          nameTextStyle: { color: pctileColor, fontSize: 10 },
          axisLabel: { color: axisLabelColor, fontSize: 10, formatter: '{value}%' },
          splitLine: { show: false },
        },
        {
          type: 'value',
          gridIndex: 1,
          name: '5日净申购(亿)',
          nameTextStyle: { color: axisLabelColor, fontSize: 10 },
          axisLabel: { color: axisLabelColor, fontSize: 10 },
          splitLine: { lineStyle: { color: splitLineColor } },
        },
        {
          type: 'value',
          gridIndex: 1,
          name: 'ERP%',
          scale: true,
          nameTextStyle: { color: erpColor, fontSize: 10 },
          axisLabel: { color: axisLabelColor, fontSize: 10, formatter: '{value}%' },
          splitLine: { show: false },
        },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 },
        {
          type: 'slider',
          xAxisIndex: [0, 1],
          bottom: 4,
          height: 18,
          borderColor: splitLineColor,
          textStyle: { color: axisLabelColor, fontSize: 10 },
        },
      ],
      series: [
        {
          name: '沪深300',
          type: 'line',
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: s.hs300_close,
          showSymbol: false,
          lineStyle: { width: 2, color: hs300Color },
          itemStyle: { color: hs300Color },
          markArea: { silent: true, data: zoneMarkAreas },
          markPoint: { data: pulseMarkPoints },
        },
        {
          name: '国家队250日分位(%)',
          type: 'line',
          xAxisIndex: 0,
          yAxisIndex: 1,
          data: s.nt_pctile_250d,
          showSymbol: false,
          smooth: 0.15,
          lineStyle: { width: 1.8, color: pctileColor },
          itemStyle: { color: pctileColor },
          areaStyle: {
            color: dark ? 'rgba(251, 191, 36, 0.08)' : 'rgba(217, 119, 6, 0.08)',
          },
          markLine: {
            silent: true,
            symbol: ['none', 'none'],
            label: { fontSize: 10, position: 'insideEndTop' },
            data: [
              {
                yAxis: 70,
                lineStyle: { color: '#10b981', type: 'dashed', width: 1.2 },
                label: { formatter: '70% 重仓托底', color: '#10b981' },
              },
              {
                yAxis: 30,
                lineStyle: { color: '#f59e0b', type: 'dashed', width: 1.2 },
                label: { formatter: '30% 低位派发', color: '#f59e0b' },
              },
            ],
          },
        },
        {
          name: '6大宽基5日净申购(亿)',
          type: 'bar',
          xAxisIndex: 1,
          yAxisIndex: 2,
          barMaxWidth: 5,
          data: s.broad6_inflow_5d_yi.map((v) => ({
            value: v,
            itemStyle: {
              color: v >= 0 ? 'rgba(239, 68, 68, 0.78)' : 'rgba(16, 185, 129, 0.78)',
            },
          })),
        },
        {
          name: '沪深300 ERP(%)',
          type: 'line',
          xAxisIndex: 1,
          yAxisIndex: 3,
          data: s.erp_pct,
          showSymbol: false,
          lineStyle: { width: 1.6, color: erpColor },
          itemStyle: { color: erpColor },
          markLine: {
            silent: true,
            symbol: ['none', 'none'],
            label: { fontSize: 9, position: 'insideEndTop' },
            data: [
              {
                yAxis: 5.0,
                lineStyle: { color: '#ef4444', type: 'dashed', width: 1.1 },
                label: { formatter: 'ERP 5% 逃顶线', color: '#ef4444' },
              },
            ],
          },
        },
      ],
    }
  }, [report, dark])

  return (
    <section className="mb-8 rounded-2xl border border-zinc-200 bg-gradient-to-b from-white to-zinc-50 p-4 shadow-sm dark:border-white/10 dark:bg-gradient-to-b dark:from-[#20252f] dark:to-[#171c26] dark:shadow-xl">
      {/* 顶栏 */}
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-lg font-semibold text-zinc-900 dark:text-slate-100">
              国家队持仓周期 × 沪深300 逃顶/抄底分析
            </h2>
            {decision && (
              <span className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold ${phaseBadgeClass}`}>
                {decision.phase_label}
              </span>
            )}
            {latest?.trade_date && (
              <span className="font-mono text-[11px] text-zinc-500 dark:text-slate-400">
                数据截止 {latest.trade_date}
              </span>
            )}
          </div>
          <p className="mt-1 text-xs text-zinc-500 dark:text-slate-300">
            以官方 3 大核心沪深300 ETF (510300/510310/510330) 场内份额规模 ÷ 沪深300点位的 250 日滚动分位刻画国家队仓位周期，叠加 6 大宽基 5 日纯份额净申购与股债性价比 ERP。
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-1.5 text-xs">
          {YEAR_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              onClick={() => setYears(opt.value)}
              className={`rounded-lg border px-2.5 py-1 font-medium transition ${
                years === opt.value
                  ? 'border-amber-500/70 bg-amber-500/15 text-amber-700 dark:text-amber-300'
                  : 'border-zinc-300 bg-white text-zinc-600 hover:border-zinc-400 dark:border-white/15 dark:bg-white/5 dark:text-slate-300 dark:hover:border-white/30'
              }`}
            >
              {opt.label}
            </button>
          ))}
          <button
            type="button"
            disabled={syncing}
            onClick={() => void handleSync()}
            className="rounded-lg border border-zinc-300 bg-white px-2.5 py-1 font-medium text-zinc-700 transition hover:border-sky-500 hover:text-sky-600 disabled:opacity-50 dark:border-white/15 dark:bg-white/5 dark:text-slate-200 dark:hover:border-sky-400"
          >
            {syncing ? '同步中…' : '同步份额'}
          </button>
          <Link
            to="/g3/desk"
            className="rounded-lg border border-sky-500/40 bg-sky-500/10 px-2.5 py-1 font-semibold text-sky-700 transition hover:bg-sky-500/20 dark:text-sky-300"
          >
            作战台联动 →
          </Link>
        </div>
      </div>

      {error && (
        <div className="mb-3 rounded-lg border border-red-500/40 bg-red-50 px-3 py-2 text-xs text-red-700 dark:bg-red-950/30 dark:text-red-300">
          {error}
        </div>
      )}
      {notice && (
        <div className="mb-3 rounded-lg border border-emerald-500/40 bg-emerald-50 px-3 py-2 text-xs text-emerald-700 dark:bg-emerald-950/30 dark:text-emerald-300">
          {notice}
        </div>
      )}

      {/* 4 枚核心实时周期 KPI */}
      {latest?.available && decision && (
        <div className="mb-3 grid grid-cols-2 gap-2.5 lg:grid-cols-4">
          <div className="rounded-xl border border-zinc-200/80 bg-white/80 p-3 dark:border-white/10 dark:bg-white/[0.04]">
            <div className="text-[11px] text-zinc-500 dark:text-slate-400">国家队 250 日持仓分位</div>
            <div className="mt-1 flex items-baseline gap-2">
              <span className="font-mono text-xl font-bold text-amber-600 dark:text-amber-300">
                {latest.nt_ratio_pctile_250d?.toFixed(1)}%
              </span>
              <span className="text-[11px] font-semibold text-zinc-600 dark:text-slate-300">
                AR3 进攻股仓上限 → <b className="text-emerald-600 dark:text-emerald-300">{decision.effective_stock_cap_pct}%</b>
              </span>
            </div>
            <div className="mt-1 text-[11px] text-zinc-500 dark:text-slate-400">
              ≥70% 重仓托底(65%) · &lt;30% 派发(35%)
            </div>
          </div>

          <div className="rounded-xl border border-zinc-200/80 bg-white/80 p-3 dark:border-white/10 dark:bg-white/[0.04]">
            <div className="text-[11px] text-zinc-500 dark:text-slate-400">3 大沪深300 ETF 实时规模</div>
            <div className="mt-1 flex items-baseline gap-2">
              <span className="font-mono text-xl font-bold text-zinc-900 dark:text-slate-100">
                ¥{latest.hs300_top3_scale_yi?.toLocaleString('zh-CN', { maximumFractionDigits: 1 })}亿
              </span>
            </div>
            <div className="mt-1 font-mono text-[11px] text-zinc-500 dark:text-slate-400">
              份额5日 {fmtSigned(latest.nt_share_chg_5d_pct, 2, '%')} · 20日 {fmtSigned(latest.nt_share_chg_20d_pct, 2, '%')}
            </div>
          </div>

          <div className="rounded-xl border border-zinc-200/80 bg-white/80 p-3 dark:border-white/10 dark:bg-white/[0.04]">
            <div className="text-[11px] text-zinc-500 dark:text-slate-400">6 大宽基 5 日纯份额净申购</div>
            <div className="mt-1 flex items-baseline gap-2">
              <span
                className={`font-mono text-xl font-bold ${
                  (latest.broad6_net_inflow_5d_yi ?? 0) >= 0
                    ? 'text-rose-600 dark:text-rose-400'
                    : 'text-emerald-600 dark:text-emerald-400'
                }`}
              >
                {fmtSigned(latest.broad6_net_inflow_5d_yi, 1, '亿')}
              </span>
              {(latest.broad6_net_inflow_5d_yi ?? 0) >= 300 && (
                <span className="rounded bg-rose-500/15 px-1.5 py-0.5 text-[10px] font-bold text-rose-600 dark:text-rose-300">
                  托底脉冲
                </span>
              )}
            </div>
            <div className="mt-1 text-[11px] text-zinc-500 dark:text-slate-400">
              6大宽基总规模 ¥{latest.broad6_scale_yi?.toLocaleString('zh-CN', { maximumFractionDigits: 1 })}亿
            </div>
          </div>

          <div className="rounded-xl border border-zinc-200/80 bg-white/80 p-3 dark:border-white/10 dark:bg-white/[0.04]">
            <div className="text-[11px] text-zinc-500 dark:text-slate-400">沪深300 ERP / 南向20日净买</div>
            <div className="mt-1 flex items-baseline gap-2">
              <span
                className={`font-mono text-xl font-bold ${
                  decision.erp_zone === 'overheated'
                    ? 'text-red-600 dark:text-red-400'
                    : 'text-violet-600 dark:text-violet-300'
                }`}
              >
                {latest.erp_pct?.toFixed(2)}%
              </span>
              <span className="font-mono text-[11px] font-semibold text-sky-700 dark:text-sky-300">
                南向 {fmtSigned(latest.sb_net_20d_yi, 1, '亿')}
              </span>
            </div>
            <div className="mt-1 font-mono text-[11px] text-zinc-500 dark:text-slate-400">
              PE {latest.hs300_pe_ttm?.toFixed(1)} · 10Y债 {latest.cn_bond_10y?.toFixed(2)}% · {latest.sb_hk_label || decision.erp_label}
            </div>
          </div>
        </div>
      )}

      {/* 周期色带图例 + 双联 ECharts 主图 */}
      <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-zinc-600 dark:text-slate-300">
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-4 rounded-sm bg-emerald-500/25 ring-1 ring-emerald-500/50" />
          绿带：重仓托底期（分位≥70%，右侧进攻放大至65%）
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-4 rounded-sm bg-amber-500/25 ring-1 ring-amber-500/50" />
          黄带：低位派发期（分位&lt;30%，进攻股仓收紧至35%）
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-4 rounded-sm bg-red-500/30 ring-1 ring-red-500/60" />
          红带：ERP&lt;5% 估值逃顶区（进攻股仓硬锁0%）
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-2.5 rounded-full bg-rose-600" />
          红标：千亿/四百亿级国家队 5 日救市脉冲
        </span>
      </div>

      {chartOption ? (
        <EChart key={`nt-cycle-${years}-${dark ? 'd' : 'l'}`} option={chartOption} height={430} loading={loading} />
      ) : (
        <div className="flex h-[320px] items-center justify-center text-xs text-zinc-500 dark:text-slate-400">
          {loading ? '正在加载国家队持仓周期图…' : '暂无国家队持仓周期数据，请点击右上角「同步份额」。'}
        </div>
      )}

      {/* 当前周期实操铁律 + 6大ETF明细折叠 */}
      {decision && (
        <div className="mt-3 rounded-xl border border-zinc-200/80 bg-zinc-50/90 p-3 text-xs dark:border-white/10 dark:bg-white/[0.03]">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="space-y-1 text-zinc-700 dark:text-slate-200">
              <div>
                <span className="font-semibold text-amber-700 dark:text-amber-300">🧭 当前周期研判：</span>
                {decision.advice}
              </div>
              {report?.methodology?.backtest_proof && (
                <div className="text-[11px] text-zinc-500 dark:text-slate-400">
                  📊 实证背书：{report.methodology.backtest_proof}
                </div>
              )}
            </div>
            {(report?.etf_breakdown?.length || 0) > 0 && (
              <button
                type="button"
                onClick={() => setShowBreakdown((v) => !v)}
                className="shrink-0 text-[11px] font-semibold text-sky-700 hover:underline dark:text-sky-300"
              >
                {showBreakdown ? '收起 6 大国家队 ETF 份额明细 ▲' : '展开 6 大国家队 ETF 份额明细 ▼'}
              </button>
            )}
          </div>

          {showBreakdown && report?.etf_breakdown && report.etf_breakdown.length > 0 && (
            <div className="mt-2.5 overflow-x-auto border-t border-zinc-200 pt-2.5 dark:border-white/10">
              <table className="w-full text-left text-[11px]">
                <thead>
                  <tr className="border-b border-zinc-200 text-zinc-500 dark:border-white/10 dark:text-slate-400">
                    <th className="py-1.5 pr-3">代码</th>
                    <th className="py-1.5 pr-3">名称</th>
                    <th className="py-1.5 pr-3 text-right">收盘价</th>
                    <th className="py-1.5 pr-3 text-right">场内份额(亿份)</th>
                    <th className="py-1.5 pr-3 text-right">最新规模(亿)</th>
                    <th className="py-1.5 pr-3 text-right">5日份额变动</th>
                    <th className="py-1.5 text-right">5日估算净申购</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-200/70 font-mono dark:divide-white/5">
                  {report.etf_breakdown.map((row) => (
                    <tr key={row.code} className="text-zinc-800 dark:text-slate-200">
                      <td className="py-1.5 pr-3 font-semibold">{row.code}</td>
                      <td className="py-1.5 pr-3 font-sans">{row.name}</td>
                      <td className="py-1.5 pr-3 text-right">{row.close.toFixed(3)}</td>
                      <td className="py-1.5 pr-3 text-right">{row.shares_yi.toFixed(2)}</td>
                      <td className="py-1.5 pr-3 text-right font-semibold">¥{row.scale_yi.toFixed(1)}</td>
                      <td
                        className={`py-1.5 pr-3 text-right ${
                          row.shares_chg_5d_yi >= 0
                            ? 'text-rose-600 dark:text-rose-400'
                            : 'text-emerald-600 dark:text-emerald-400'
                        }`}
                      >
                        {fmtSigned(row.shares_chg_5d_yi, 2, '亿')} ({fmtSigned(row.shares_chg_5d_pct, 2, '%')})
                      </td>
                      <td
                        className={`py-1.5 text-right font-semibold ${
                          row.est_inflow_5d_yi >= 0
                            ? 'text-rose-600 dark:text-rose-400'
                            : 'text-emerald-600 dark:text-emerald-400'
                        }`}
                      >
                        {fmtSigned(row.est_inflow_5d_yi, 1, '亿')}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </section>
  )
}
