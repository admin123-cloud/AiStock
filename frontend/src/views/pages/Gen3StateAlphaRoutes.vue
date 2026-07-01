<template>
  <div class="g3-page">
    <header class="topbar">
      <div>
        <div class="eyebrow">G3 正式五策略</div>
        <h1>有效性诊断</h1>
        <p>按融合后的正式 G3 五策略合同判断当前策略是否有效、今天走哪类策略、风险来自市场、候选质量还是执行链路。</p>
      </div>
      <div class="actions">
        <el-button type="primary" :loading="loading" @click="load">刷新</el-button>
      </div>
    </header>

    <section class="metric-grid">
      <div class="metric-card">
        <span>策略状态</span>
        <strong>{{ strategyStatus.label }}</strong>
        <small>{{ strategyStatus.reason }}</small>
      </div>
      <div class="metric-card accent">
        <span>当前主路由</span>
        <strong>{{ routeName(summary.selected_route) }}</strong>
        <small>{{ summary.route_reason || '--' }}</small>
      </div>
      <div class="metric-card">
        <span>合格候选</span>
        <strong>{{ eligibleCandidateCount }}</strong>
        <small>全量 {{ allCandidates.length }} / 阻断 {{ blockedCandidateCount }}</small>
      </div>
      <div class="metric-card">
        <span>主路由近样本</span>
        <strong>{{ fmt(selectedRouteDiagnostic?.route_health_count || selectedRouteDiagnostic?.sample_count, 0) }}</strong>
        <small>均笔 {{ pct(selectedRouteAvgReturn) }}</small>
      </div>
      <div class="metric-card" :class="worstTradeClass">
        <span>最差单笔</span>
        <strong>{{ pct(metrics.worst_trade) }}</strong>
        <small>账户连续亏损阈值优先看这里</small>
      </div>
      <div class="metric-card locked">
        <span>今日动作</span>
        <strong>{{ todayAction }}</strong>
        <small>route_health 观察，不作为硬 gate</small>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>信号链路</h2>
          <p>从市场状态到影子票据逐层确认，任何一层阻断都不能变成合格买入。</p>
        </div>
      </div>
      <div class="pipeline">
        <div v-for="item in pipeline" :key="item.key" class="stage" :class="`stage-${item.status}`">
          <div class="stage-title">
            <span>{{ item.title }}</span>
            <strong v-if="item.count !== null && item.count !== undefined">{{ item.count }}</strong>
          </div>
          <p>{{ item.detail || '--' }}</p>
        </div>
      </div>
    </section>

    <section class="two-col">
      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>失效预警</h2>
            <p>这里集中展示不该分散在工作台、候选池和历史成交页里的策略有效性判断。</p>
          </div>
        </div>
        <div class="warning-list">
          <div v-for="item in effectivenessWarnings" :key="item.key" class="warning-row" :class="item.level">
            <el-tag size="small" :type="warningType(item.level)">{{ warningLabel(item.level) }}</el-tag>
            <div>
              <strong>{{ item.title }}</strong>
              <span>{{ item.detail }}</span>
            </div>
          </div>
          <el-empty v-if="!effectivenessWarnings.length" description="暂无明显失效预警" />
        </div>
      </div>

      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>候选质量</h2>
            <p>候选池前排质量是比已成交收益更早的失效信号。</p>
          </div>
        </div>
        <div class="quality-grid">
          <div>
            <span>合格率</span>
            <strong>{{ pct(candidateEligibleRate) }}</strong>
            <small>通过 / 全量候选</small>
          </div>
          <div>
            <span>机构主升候选</span>
            <strong>{{ fmt(routeCandidateCount('institutional_mainwave'), 0) }}</strong>
            <small>主收益来源</small>
          </div>
          <div>
            <span>G2补位候选</span>
            <strong>{{ fmt(routeCandidateCount('g2_gap_supplement'), 0) }}</strong>
            <small>已退出实盘，仅观察</small>
          </div>
          <div>
            <span>阻断最多</span>
            <strong>{{ topBlockReason.reason }}</strong>
            <small>{{ topBlockReason.count }} 条</small>
          </div>
        </div>
      </div>
    </section>

    <section class="panel strategy-detail-panel">
      <div class="panel-head">
        <div>
          <h2>交易策略详情</h2>
          <p>把每个正式交易策略的职责、来源、入场合同、退出边界和诊断重点讲清楚；风控合同仍保留在独立页面，本页只做有效性解释和证据对照。</p>
        </div>
      </div>
      <div class="strategy-detail-grid">
        <article v-for="item in strategyDetails" :key="item.key" class="strategy-detail-card" :class="item.tone">
          <div class="strategy-detail-head">
            <div>
              <span class="strategy-role">{{ item.role }}</span>
              <h3>{{ item.label }}</h3>
            </div>
            <el-tag size="small" :type="item.tagType">{{ item.badge }}</el-tag>
          </div>
          <p class="strategy-summary">{{ item.summary }}</p>
          <div class="strategy-stats">
            <div>
              <span>候选</span>
              <strong>{{ fmt(item.candidateCount, 0) }}</strong>
            </div>
            <div>
              <span>交易</span>
              <strong>{{ fmt(item.tradeCount, 0) }}</strong>
            </div>
            <div>
              <span>胜率</span>
              <strong>{{ pct(item.winRate) }}</strong>
            </div>
            <div>
              <span>均笔</span>
              <strong>{{ pct(item.avgReturn) }}</strong>
            </div>
            <div>
              <span>最差</span>
              <strong>{{ pct(item.worstTrade) }}</strong>
            </div>
          </div>
          <dl class="strategy-contract-list">
            <div>
              <dt>来源路线</dt>
              <dd>{{ item.sources }}</dd>
            </div>
            <div>
              <dt>入场合同</dt>
              <dd>{{ item.entry }}</dd>
            </div>
            <div>
              <dt>仓位/退出</dt>
              <dd>{{ item.exit }}</dd>
            </div>
            <div>
              <dt>诊断重点</dt>
              <dd>{{ item.diagnosis }}</dd>
            </div>
          </dl>
        </article>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>当前路由健康</h2>
          <p>展示今日路由来源、合格数量、观察健康度和阻断原因。收益判断集中在本页，不再分散到工作台重复展示。</p>
        </div>
      </div>
      <el-table v-loading="loading" :data="routeDiagnostics" stripe size="small" empty-text="暂无路由诊断">
        <el-table-column label="路线" min-width="150">
          <template #default="{ row }">{{ row.route_label || routeName(row.route) }}</template>
        </el-table-column>
        <el-table-column label="来源" width="88" align="right">
          <template #default="{ row }">{{ fmt(row.rows || row.source_rows, 0) }}</template>
        </el-table-column>
        <el-table-column label="合格" width="88" align="right">
          <template #default="{ row }">{{ fmt(row.eligible_rows || row.final_candidates, 0) }}</template>
        </el-table-column>
        <el-table-column label="阻断" width="88" align="right">
          <template #default="{ row }">{{ fmt(row.blocked_rows, 0) }}</template>
        </el-table-column>
        <el-table-column label="健康" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.route_health_ok) || truthy(row.ok) ? 'success' : 'warning'">
              {{ truthy(row.route_health_ok) || truthy(row.ok) ? '通过' : '观察' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="近样本" width="90" align="right">
          <template #default="{ row }">{{ fmt(row.route_health_count || row.sample_count, 0) }}</template>
        </el-table-column>
        <el-table-column label="均笔" width="90" align="right">
          <template #default="{ row }">{{ pct(row.route_health_avg_ret || row.avg_trade_return) }}</template>
        </el-table-column>
        <el-table-column label="最差单笔" width="100" align="right">
          <template #default="{ row }">{{ pct(row.route_health_worst_ret || row.worst_trade) }}</template>
        </el-table-column>
        <el-table-column prop="top_block_reason" label="阻断/说明" min-width="260" show-overflow-tooltip />
      </el-table>
    </section>

    <section class="two-col">
      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>历史已平仓策略收益归因</h2>
            <p>按正式交易策略统计已平仓样本；最差单笔允许为负，代表历史止损或退出尾部，不代表当前候选票的预期收益。</p>
          </div>
        </div>
        <el-table :data="routeMetrics" stripe size="small" empty-text="暂无历史策略归因">
          <el-table-column label="交易策略" min-width="150">
            <template #default="{ row }">
              <div class="strategy-cell">
                <strong>{{ row.route_label || tradeStrategyName(row) }}</strong>
                <small v-if="row.strategy_summary">含：{{ row.strategy_summary }}</small>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="交易" width="82" align="right">
            <template #default="{ row }">{{ fmt(row.trade_count, 0) }}</template>
          </el-table-column>
          <el-table-column label="胜率" width="86" align="right">
            <template #default="{ row }">{{ pct(row.win_rate) }}</template>
          </el-table-column>
          <el-table-column label="均笔" width="86" align="right">
            <template #default="{ row }">{{ pct(row.avg_trade_return) }}</template>
          </el-table-column>
          <el-table-column label="最差单笔" width="100" align="right">
            <template #default="{ row }">{{ pct(row.worst_trade) }}</template>
          </el-table-column>
          <el-table-column label="盈亏" width="110" align="right">
            <template #default="{ row }">{{ money(row.sum_realized_pnl) }}</template>
          </el-table-column>
        </el-table>
      </div>

      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>市场风格归因</h2>
            <p>防止路线只在单一市场环境中有效。</p>
          </div>
        </div>
        <el-table :data="marketStyleMetrics" stripe size="small" empty-text="暂无市场风格归因">
          <el-table-column prop="market_style" label="风格" min-width="130" />
          <el-table-column label="交易" width="82" align="right">
            <template #default="{ row }">{{ fmt(row.trade_count, 0) }}</template>
          </el-table-column>
          <el-table-column label="胜率" width="86" align="right">
            <template #default="{ row }">{{ pct(row.win_rate) }}</template>
          </el-table-column>
          <el-table-column label="均笔" width="86" align="right">
            <template #default="{ row }">{{ pct(row.avg_trade_return) }}</template>
          </el-table-column>
          <el-table-column label="最差" width="86" align="right">
            <template #default="{ row }">{{ pct(row.worst_trade) }}</template>
          </el-table-column>
        </el-table>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>五策略正式合同验收</h2>
          <p>确认 5 个正式策略主体仍能还原 G3 最终版收益；正式收益固定看 G3最终版，桥接和简化场景只作为诊断证据。</p>
        </div>
        <el-tag size="small" :type="recovery.ok ? 'success' : 'warning'">
          {{ recovery.ok ? '已恢复召回' : '待修复' }}
        </el-tag>
      </div>
      <div class="quality-grid recovery-grid">
        <div>
          <span>合同验收</span>
          <strong>{{ fusionGuardrailSummary.ok ? '通过' : '失败' }}</strong>
          <small>硬失败 {{ fmt(fusionGuardrailSummary.hard_fail_count, 0) }} / 提示 {{ fmt(fusionGuardrailSummary.warn_count, 0) }}</small>
        </div>
        <div>
          <span>正式主收益</span>
          <strong>{{ pct(formalProfile.total_return) }}</strong>
          <small>G3 final 完整交易合同 / {{ fmt(formalProfile.trade_count, 0) }} 笔</small>
        </div>
        <div>
          <span>简化运行收益</span>
          <strong>{{ pct(practicalFusionSummary.recommended_total_return) }}</strong>
          <small>{{ practicalFusionSummary.recommended_scenario || 'score_range_g2' }} / {{ fmt(practicalFusionSummary.recommended_trade_count, 0) }} 笔</small>
        </div>
        <div>
          <span>收益保留率</span>
          <strong>{{ pct(practicalFusionSummary.recommended_retain_rate) }}</strong>
          <small>默认门槛 {{ pct(practicalFusionSummary.min_accept_retain_rate) }}</small>
        </div>
        <div>
          <span>桥接验收收益</span>
          <strong>{{ pct(recoveryHeadline.total_return) }}</strong>
          <small>只用于验证 5 策略标签，不作为正式目标</small>
        </div>
        <div>
          <span>收益缺口</span>
          <strong>{{ pct(returnGapSummary.return_gap) }}</strong>
          <small>{{ returnGapSummary.status || 'needs_rebuild' }}</small>
        </div>
        <div>
          <span>最大回撤</span>
          <strong>{{ pct(recoveryHeadline.max_drawdown) }}</strong>
          <small>研究排除源不进入默认合同</small>
        </div>
        <div>
          <span>桥接交易</span>
          <strong>{{ fmt(recoveryHeadline.closed_trades, 0) }}</strong>
          <small>候选 {{ fmt(recoveryHeadline.candidate_rows, 0) }} / 选票 {{ fmt(recoveryHeadline.selected_rows, 0) }}</small>
        </div>
        <div>
          <span>胜率 / 均笔</span>
          <strong>{{ pct(recoveryHeadline.win_rate) }}</strong>
          <small>均笔 {{ pct(recoveryHeadline.avg_ret) }}</small>
        </div>
        <div>
          <span>30m数据可用</span>
          <strong>{{ pct(recoveryM30Summary.m30_data_ok_rate) }}</strong>
          <small>入场前窗口 {{ pct(recoveryM30Summary.m30_pre_signal_ok_rate) }}</small>
        </div>
        <div>
          <span>30m代理确认</span>
          <strong>{{ pct(recoveryM30Summary.m30_confirmed_proxy_rate) }}</strong>
          <small>低通过率时保留原生确认语义</small>
        </div>
        <div>
          <span>原生30m确认</span>
          <strong>{{ pct(recoveryM30NativeSummary.native_m30_confirmed_rate) }}</strong>
          <small>代理误杀 {{ fmt(recoveryM30NativeSummary.proxy_kill_count, 0) }} 个候选</small>
        </div>
        <div>
          <span>代理筛选收益</span>
          <strong>{{ pct(recoveryM30NativeSummary.scenario_proxy_m30_total_return) }}</strong>
          <small>研究过滤，不作为默认硬 gate</small>
        </div>
      </div>
      <el-table :data="fusionGuardrailChecks" stripe size="small" empty-text="暂无合同验收项">
        <el-table-column label="验收项" min-width="180">
          <template #default="{ row }">{{ row.title || row.key }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="guardrailTagType(row.status)">
              {{ guardrailStatusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="evidence" label="证据" min-width="260" show-overflow-tooltip />
        <el-table-column prop="action" label="处理口径" min-width="260" show-overflow-tooltip />
      </el-table>
      <el-table :data="recoveryStrategies" stripe size="small" empty-text="暂无策略还原数据">
        <el-table-column label="交易策略" min-width="150">
          <template #default="{ row }">{{ row.trade_strategy_label || row.trade_strategy }}</template>
        </el-table-column>
        <el-table-column label="历史贡献" width="110" align="right">
          <template #default="{ row }">{{ money(row.sum_pnl_historical_g3_final) }}</template>
        </el-table-column>
        <el-table-column label="日线代理" width="110" align="right">
          <template #default="{ row }">{{ money(row.sum_pnl_daily_proxy_five_strategy) }}</template>
        </el-table-column>
        <el-table-column label="原生桥接" width="110" align="right">
          <template #default="{ row }">{{ money(row.sum_pnl_native_bridge_five_strategy) }}</template>
        </el-table-column>
        <el-table-column label="桥接胜率" width="100" align="right">
          <template #default="{ row }">{{ pct(row.win_rate_native_bridge_five_strategy) }}</template>
        </el-table-column>
        <el-table-column label="桥接均笔" width="100" align="right">
          <template #default="{ row }">{{ pct(row.avg_ret_native_bridge_five_strategy) }}</template>
        </el-table-column>
      </el-table>
      <el-table :data="recoveryScenarios" stripe size="small" class="recovery-scenario-table" empty-text="暂无场景矩阵">
        <el-table-column prop="scenario" label="场景" min-width="190" />
        <el-table-column label="收益" width="100" align="right">
          <template #default="{ row }">{{ pct(row.total_return) }}</template>
        </el-table-column>
        <el-table-column label="回撤" width="100" align="right">
          <template #default="{ row }">{{ pct(row.max_drawdown) }}</template>
        </el-table-column>
        <el-table-column label="交易" width="86" align="right">
          <template #default="{ row }">{{ fmt(row.closed, 0) }}</template>
        </el-table-column>
        <el-table-column label="胜率" width="90" align="right">
          <template #default="{ row }">{{ pct(row.win_rate) }}</template>
        </el-table-column>
      </el-table>
      <el-table :data="recoveryM30Strategies" stripe size="small" class="recovery-scenario-table" empty-text="暂无30m完整性数据">
        <el-table-column label="交易策略" min-width="150">
          <template #default="{ row }">{{ row.trade_strategy_label || row.trade_strategy }}</template>
        </el-table-column>
        <el-table-column label="候选" width="82" align="right">
          <template #default="{ row }">{{ fmt(row.candidate_count, 0) }}</template>
        </el-table-column>
        <el-table-column label="30m数据" width="100" align="right">
          <template #default="{ row }">{{ pct(row.m30_data_ok_rate) }}</template>
        </el-table-column>
        <el-table-column label="入场前窗口" width="110" align="right">
          <template #default="{ row }">{{ pct(row.m30_pre_signal_ok_rate) }}</template>
        </el-table-column>
        <el-table-column label="代理确认" width="100" align="right">
          <template #default="{ row }">{{ pct(row.m30_confirmed_proxy_rate) }}</template>
        </el-table-column>
        <el-table-column label="均30m位置" width="110" align="right">
          <template #default="{ row }">{{ pct(row.avg_m30_day_close_pos) }}</template>
        </el-table-column>
      </el-table>
      <el-table :data="recoveryM30NativeScenarios" stripe size="small" class="recovery-scenario-table" empty-text="暂无原生30m语义场景">
        <el-table-column prop="scenario" label="30m语义场景" min-width="190" />
        <el-table-column label="候选" width="82" align="right">
          <template #default="{ row }">{{ fmt(row.candidates, 0) }}</template>
        </el-table-column>
        <el-table-column label="交易" width="82" align="right">
          <template #default="{ row }">{{ fmt(row.closed, 0) }}</template>
        </el-table-column>
        <el-table-column label="收益" width="100" align="right">
          <template #default="{ row }">{{ pct(row.total_return) }}</template>
        </el-table-column>
        <el-table-column label="回撤" width="100" align="right">
          <template #default="{ row }">{{ pct(row.max_drawdown) }}</template>
        </el-table-column>
      </el-table>
    </section>

    <section class="panel">
      <div class="panel-head table-head">
        <div>
          <h2>路线候选明细</h2>
          <p>只保留路线级候选复核；完整逐日候选复盘进入候选池页。</p>
        </div>
        <el-select v-model="routeFilter" size="small" style="width: 190px">
          <el-option label="全部路线" value="all" />
          <el-option v-for="item in routeOptions" :key="item.value" :label="item.label" :value="item.value" />
        </el-select>
      </div>
      <el-table :data="visibleCandidates" stripe size="small" height="420" empty-text="暂无候选明细">
        <el-table-column prop="code" label="代码" width="110" fixed />
        <el-table-column prop="name" label="名称" min-width="110" fixed />
        <el-table-column label="交易策略" width="150">
          <template #default="{ row }">{{ tradeStrategyName(row) }}</template>
        </el-table-column>
        <el-table-column label="来源路线" width="140">
          <template #default="{ row }">{{ row.route_label || routeName(row.route || row.mode) }}</template>
        </el-table-column>
        <el-table-column label="准入" width="86">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.router_eligible || row.qualified_shadow_buy) ? 'success' : 'warning'">
              {{ truthy(row.router_eligible || row.qualified_shadow_buy) ? '通过' : '阻断' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="得分" width="90" align="right">
          <template #default="{ row }">{{ fmt(scoreOf(row)) }}</template>
        </el-table-column>
        <el-table-column prop="block_reason" label="阻断/说明" min-width="280" show-overflow-tooltip />
      </el-table>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getGen3StateAlphaCurrent, getGen3StateAlphaHistoricalTrades } from '@/api/trading'

const loading = ref(false)
const currentPayload = ref({})
const historyPayload = ref({})
const routeFilter = ref('all')

const summary = computed(() => currentPayload.value.summary || {})
const routeDiagnostics = computed(() => Array.isArray(currentPayload.value.route_diagnostics) ? currentPayload.value.route_diagnostics : [])
const allCandidates = computed(() => Array.isArray(currentPayload.value.all_source_candidates) ? currentPayload.value.all_source_candidates : [])
const pipeline = computed(() => Array.isArray(currentPayload.value.pipeline) ? currentPayload.value.pipeline : [])
const metrics = computed(() => historyPayload.value.metrics || {})
const routeMetrics = computed(() => Array.isArray(historyPayload.value.route_metrics) ? historyPayload.value.route_metrics : [])
const marketStyleMetrics = computed(() => Array.isArray(historyPayload.value.market_style_metrics) ? historyPayload.value.market_style_metrics : [])
const recovery = computed(() => historyPayload.value.strategy_reduction_recovery || {})
const recoveryHeadline = computed(() => recovery.value.headline || {})
const formalProfile = computed(() => recovery.value.formal_profile || {})
const practicalFusion = computed(() => recovery.value.practical_fusion_contract || {})
const practicalFusionSummary = computed(() => practicalFusion.value.summary || {})
const returnGapAudit = computed(() => recovery.value.return_gap_audit || {})
const returnGapSummary = computed(() => returnGapAudit.value.summary || {})
const fusionGuardrails = computed(() => recovery.value.fusion_guardrails || {})
const fusionGuardrailSummary = computed(() => fusionGuardrails.value.summary || {})
const fusionGuardrailChecks = computed(() => Array.isArray(fusionGuardrails.value.checks) ? fusionGuardrails.value.checks : [])
const recoveryStrategies = computed(() => Array.isArray(recovery.value.strategy_compare) ? recovery.value.strategy_compare : [])
const recoveryScenarios = computed(() => Array.isArray(recovery.value.scenario_matrix) ? recovery.value.scenario_matrix : [])
const recoveryM30 = computed(() => recovery.value.m30_integrity || {})
const recoveryM30Summary = computed(() => recoveryM30.value.summary || {})
const recoveryM30Strategies = computed(() => Array.isArray(recoveryM30.value.by_strategy) ? recoveryM30.value.by_strategy : [])
const recoveryM30Native = computed(() => recoveryM30.value.native_semantics || {})
const recoveryM30NativeSummary = computed(() => recoveryM30Native.value.summary || {})
const recoveryM30NativeScenarios = computed(() => Array.isArray(recoveryM30Native.value.scenario_matrix) ? recoveryM30Native.value.scenario_matrix : [])
const selectedRouteDiagnostic = computed(() => routeDiagnostics.value.find((row) => row.route === summary.value.selected_route) || null)
const selectedRouteAvgReturn = computed(() => (
  selectedRouteDiagnostic.value?.route_health_avg_ret
  ?? selectedRouteDiagnostic.value?.avg_trade_return
  ?? metrics.value.avg_trade_return
))
const eligibleCandidateCount = computed(() => allCandidates.value.filter((row) => truthy(row.router_eligible || row.qualified_shadow_buy)).length)
const blockedCandidateCount = computed(() => allCandidates.value.filter((row) => !truthy(row.router_eligible || row.qualified_shadow_buy) || row.block_reason).length)
const candidateEligibleRate = computed(() => {
  if (!allCandidates.value.length) return null
  return eligibleCandidateCount.value / allCandidates.value.length
})
const worstTradeClass = computed(() => {
  const worst = Number(metrics.value.worst_trade)
  if (Number.isFinite(worst) && worst <= -0.08) return 'danger'
  if (Number.isFinite(worst) && worst <= -0.05) return 'warning'
  return ''
})
const todayAction = computed(() => {
  if (!eligibleCandidateCount.value) return '观察'
  if ((effectivenessWarnings.value || []).some((item) => item.level === 'danger')) return '降权'
  return '可影子'
})
const strategyStatus = computed(() => {
  const danger = effectivenessWarnings.value.filter((item) => item.level === 'danger')
  const warn = effectivenessWarnings.value.filter((item) => item.level === 'warning')
  if (danger.length) return { label: '降权观察', reason: danger[0].title }
  if (warn.length) return { label: '有效待验', reason: warn[0].title }
  return { label: '有效', reason: '路由、候选和风险暂无明显异常' }
})
const effectivenessWarnings = computed(() => {
  const items = []
  if (!summary.value.selected_route) {
    items.push({ key: 'no_selected_route', level: 'warning', title: '今日没有选中主路由', detail: '没有主路由时，只能观察候选池和补位路线。' })
  }
  if (!eligibleCandidateCount.value) {
    items.push({ key: 'no_eligible_candidates', level: 'warning', title: '今日没有合格候选', detail: '如果连续多日无候选，要检查市场适配或数据链路。' })
  }
  const routeAvg = Number(selectedRouteAvgReturn.value)
  if (Number.isFinite(routeAvg) && routeAvg <= 0) {
    items.push({ key: 'route_avg_non_positive', level: 'warning', title: '主路由滚动均笔不为正', detail: `当前均笔 ${pct(routeAvg)}，route_health 仍只观察，不硬阻断。` })
  }
  const worst = Number(metrics.value.worst_trade)
  if (Number.isFinite(worst) && worst <= -0.08) {
    items.push({ key: 'tail_loss_large', level: 'danger', title: '历史尾部亏损偏大', detail: `最差单笔 ${pct(worst)}，需要继续盯单笔账户亏损。` })
  }
  const mainwaveCount = routeCandidateCount('institutional_mainwave')
  const g2Count = routeCandidateCount('g2_gap_supplement')
  if (g2Count > 0 && mainwaveCount === 0) {
    items.push({ key: 'g2_supplement_retired_observe', level: 'warning', title: 'G2补位仅观察', detail: '量能续强补位已退出实盘交易；候选只用于源质量和历史归因复盘。' })
  }
  return items
})
const topBlockReason = computed(() => {
  const counts = new Map()
  allCandidates.value.forEach((row) => {
    const reason = String(row.block_reason || row.skip_reason || '').trim()
    if (!reason) return
    counts.set(reason, (counts.get(reason) || 0) + 1)
  })
  const top = Array.from(counts.entries()).sort((a, b) => b[1] - a[1])[0]
  return top ? { reason: top[0], count: top[1] } : { reason: '无', count: 0 }
})
const routeOptions = computed(() => {
  const routes = new Map()
  ;[...routeDiagnostics.value, ...allCandidates.value, ...routeMetrics.value].forEach((row) => {
    const value = row.route || row.mode
    if (!value) return
    routes.set(value, row.route_label || routeName(value))
  })
  return Array.from(routes, ([value, label]) => ({ value, label }))
})
const visibleCandidates = computed(() => {
  if (routeFilter.value === 'all') return allCandidates.value
  return allCandidates.value.filter((row) => (row.route || row.mode) === routeFilter.value)
})
const strategyDetails = computed(() => {
  const definitions = [
    {
      key: 'institutional_score120_mainwave',
      label: '机构主升 Score120',
      role: '主收益引擎',
      badge: '进攻',
      tagType: 'success',
      tone: 'mainwave',
      routes: ['institutional_mainwave', 'score120_core'],
      aliases: ['institutional_score120_mainwave', 'institutional_score120_core', 'mainwave_breakout_offense'],
      summary: '只在机构主线扩散和分数确认同时成立时出手，承担 G3 的主要进攻收益。',
      sources: 'institutional_mainwave；旧 score120 只保留为证据来源。',
      entry: 'score>=120、sector_diffusion>=65、30m close>=MA20、index_mom60<=5%。index_mom60>5% 只观察，不进入影子盘或买入候选。',
      exit: '默认单槽 50%；12% 硬止损、12% 先减半、剩余仓以前低或 30m 转弱退出。',
      diagnosis: '重点看主升分数口径是否统一、指数 60 日动量是否过热、连续两笔机构主升亏损后的动态冷却是否触发。'
    },
    {
      key: 'range_weak_repair',
      label: '震荡弱势修复',
      role: '弱势修复',
      badge: '修复',
      tagType: 'warning',
      tone: 'repair',
      routes: ['old_g3_route_v3', 'panic_repair', 'range_gap'],
      aliases: ['range_weak_repair', 'repair_range_weak'],
      summary: '处理非主升、非下跌主杀环境里的震荡或弱反弹修复，不和机构主升混排评分。',
      sources: 'old_g3_route_v3、panic_repair range 来源；用 source_strategy_label 保留来源分层。',
      entry: 'market_style 以 standard_range / weak_rebound 为主；日线出现中短期回撤后的收盘修复、下影或反包，震荡恐慌来源优先要求 30m 修复确认。',
      exit: '基础首仓 20%，最高 25%；-6% 结构止损、-10% 硬止损、+8% 先减半，剩余仓以前低或 30m 转弱退出。',
      diagnosis: '重点看候选是否真是弱势修复而不是追高，压力释放过滤是否过松，30m 修复确认是否缺失。'
    },
    {
      key: 'panic_capitulation_repair',
      label: '恐慌出清修复',
      role: '压力释放',
      badge: '防守',
      tagType: 'warning',
      tone: 'panic',
      routes: ['panic_repair', 'old_g3_route_v3', 'down_panic'],
      aliases: ['panic_capitulation_repair', 'panic_repair_downtrend'],
      summary: '在恐慌扩散或下跌压力释放后做修复介入，保留独立风控，不再拆成多个下跌修复策略。',
      sources: 'old_g3 down_panic、panic_repair standard_downtrend；来源不同，交易策略合并。',
      entry: '日线先出现恐慌出清或压力释放；有盘中数据时优先要求 30m 修复确认，无确认时先作为观察票据。',
      exit: '基础首仓 20%，压力态可降到 12.5%；-5% 结构止损、-8% 硬止损、+8% 先减半，剩余仓以前低或 30m 转弱退出。',
      diagnosis: '重点看恐慌是否真的释放、下跌压力态是否降仓、修复票是否被误当成主升进攻票。'
    },
    {
      key: 'volume_runup_supplement',
      label: '量能续强补位',
      role: '退役观察',
      badge: '观察',
      tagType: 'info',
      tone: 'supplement',
      routes: ['g2_gap_supplement'],
      aliases: ['volume_runup_supplement', 'g2_gap_supplement'],
      summary: '历史回放显示负贡献且未改善组合回撤，已退出实盘买入路线，仅保留观察和归因。',
      sources: 'g2_gap_supplement；来自 G2 量能续强和板块加分信号，不触碰 G2 runtime。',
      entry: '不再进入影子票据、纸面交易或正式买入候选；源新鲜时也只进入观察池。',
      exit: '无实盘仓位合同；历史持仓仍按原退出合同复盘。',
      diagnosis: '重点看源质量、子来源归因和是否存在未来可重新验证的严格版本；默认不再为填空槽而交易。'
    },
    {
      key: 'old_g3_strong_breakout',
      label: '强势突破',
      role: '结构突破',
      badge: '证据',
      tagType: 'info',
      tone: 'breakout',
      routes: ['old_g3_route_v3', 'strong_main'],
      aliases: ['old_g3_strong_breakout', 'strong_breakout', 'mainwave_breakout_offense'],
      summary: '保留旧 G3 跨周期结构链路里的强势突破，不等同于机构主升 Score120。',
      sources: 'old_g3_route_v3 strong_main；作为正式五策略主体之一保留归因和诊断。',
      entry: '跨周期结构确认后才纳入，重点看突破是否具备趋势延续和市场适配；不和机构主升使用同一套分数直接比较。',
      exit: '默认单槽 50%；沿用 G3 统一退出合同，12% 硬止损、12% 先减半，剩余仓以前低或 30m 转弱退出。',
      diagnosis: '重点看强势突破是否被旧信号噪声放大、是否与机构主升重复计分、是否只在单一市场风格有效。'
    }
  ]

  return definitions.map((item) => {
    const metric = findStrategyMetric(item)
    const recoveryMetric = findRecoveryStrategyMetric(item)
    const m30Metric = findM30StrategyMetric(item)
    return {
      ...item,
      candidateCount: strategyCandidateCount(item),
      tradeCount: metric?.trade_count ?? recoveryMetric?.closed ?? recoveryMetric?.trade_count,
      winRate: metric?.win_rate ?? recoveryMetric?.win_rate_native_bridge_five_strategy ?? recoveryMetric?.win_rate,
      avgReturn: metric?.avg_trade_return ?? recoveryMetric?.avg_ret_native_bridge_five_strategy ?? recoveryMetric?.avg_ret,
      worstTrade: metric?.worst_trade ?? recoveryMetric?.worst_trade,
      diagnosis: m30Metric?.m30_data_ok_rate != null
        ? `${item.diagnosis} 30m 数据覆盖率 ${pct(m30Metric.m30_data_ok_rate)}，用于判断该策略是否具备实盘复现条件。`
        : item.diagnosis
    }
  })
})

function matchesStrategy(row, item) {
  if (!row) return false
  const values = [
    row.trade_strategy,
    row.route_strategy,
    row.strategy,
    row.route,
    row.mode,
    row.route_parent,
    row.source_route
  ].map((value) => String(value || '').trim()).filter(Boolean)
  return values.some((value) => item.aliases.includes(value) || item.routes.includes(value))
}

function strategyCandidateCount(item) {
  return allCandidates.value.filter((row) => matchesStrategy(row, item)).length
}

function findStrategyMetric(item) {
  return routeMetrics.value.find((row) => matchesStrategy(row, item)) || null
}

function findRecoveryStrategyMetric(item) {
  return recoveryStrategies.value.find((row) => matchesStrategy(row, item)) || null
}

function findM30StrategyMetric(item) {
  return recoveryM30Strategies.value.find((row) => matchesStrategy(row, item)) || null
}

function truthy(value) {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0
  if (typeof value === 'string') return ['1', 'true', 'yes', 'ok', 'pass'].includes(value.trim().toLowerCase())
  return false
}

function routeName(route) {
  const names = {
    institutional_mainwave: '机构主升',
    panic_repair: '恐慌修复',
    g2_gap_supplement: '量能续强补位',
    old_g3_route_v3: '旧G3原生来源',
    old_g3_route: '旧G3原生来源',
    range_gap: '震荡弱势修复来源',
    down_panic: '恐慌出清修复来源',
    strong_main: '强势突破来源'
  }
  return names[route] || route || '--'
}

function guardrailTagType(status) {
  if (status === 'pass') return 'success'
  if (status === 'fail') return 'danger'
  if (status === 'warn') return 'warning'
  return 'info'
}

function guardrailStatusLabel(status) {
  if (status === 'pass') return '通过'
  if (status === 'fail') return '失败'
  if (status === 'warn') return '提示'
  return status || '--'
}

function tradeStrategyName(row) {
  const names = {
    range_weak_repair: '震荡弱势修复',
    panic_capitulation_repair: '恐慌出清修复',
    institutional_score120_mainwave: '机构主升Score120',
    old_g3_strong_breakout: '强势突破',
    mainwave_breakout_offense: '主升/突破进攻',
    volume_runup_supplement: '量能续强补位'
  }
  return row?.trade_strategy_label || names[row?.trade_strategy] || row?.route_strategy_label || routeName(row?.route || row?.mode)
}

function routeCandidateCount(route) {
  return allCandidates.value.filter((row) => (row.route || row.mode) === route).length
}

function scoreOf(row) {
  if ((row.route || row.mode) === 'institutional_mainwave') {
    return row.wave_style_score ?? row.selected_score ?? row.score ?? row.candidate_score
  }
  return row.score ?? row.candidate_score ?? row.mode_pick_score ?? row.v4_score ?? row.selected_score
}

function warningType(level) {
  if (level === 'danger') return 'danger'
  if (level === 'warning') return 'warning'
  return 'info'
}

function warningLabel(level) {
  if (level === 'danger') return '风险'
  if (level === 'warning') return '观察'
  return '提示'
}

function fmt(value, digits = 2) {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(digits) : '--'
}

function pct(value) {
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : '--'
}

function money(value) {
  const n = Number(value)
  return Number.isFinite(n) ? n.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) : '--'
}

async function load() {
  loading.value = true
  try {
    const [current, history] = await Promise.all([
      getGen3StateAlphaCurrent({ limit: 200 }),
      getGen3StateAlphaHistoricalTrades({ limit: 500 })
    ])
    currentPayload.value = current
    historyPayload.value = history
  } catch (error) {
    ElMessage.warning(error?.message || '读取 G3 路由诊断失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.g3-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
  color: #1f2a44;
}

.topbar,
.panel,
.metric-card {
  background: #fff;
  border: 1px solid #e4e9f1;
  border-radius: 8px;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 16px;
}

.eyebrow {
  color: #2f80ed;
  font-size: 12px;
  font-weight: 700;
  margin-bottom: 4px;
}

h1,
h2,
p {
  margin: 0;
}

h1 {
  font-size: 26px;
  line-height: 1.2;
}

h2 {
  font-size: 16px;
}

.topbar p,
.panel-head p,
.metric-card small {
  color: #667085;
  line-height: 1.5;
}

.actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 10px;
}

.metric-card {
  min-height: 92px;
  padding: 12px;
  border-left: 4px solid #2f80ed;
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.metric-card.accent {
  border-left-color: #12b76a;
}

.metric-card.danger {
  border-left-color: #d92d20;
}

.metric-card.warning {
  border-left-color: #f79009;
}

.metric-card.locked {
  border-left-color: #b54708;
}

.metric-card span {
  color: #667085;
  font-size: 12px;
}

.metric-card strong {
  font-size: 21px;
  line-height: 1.2;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.panel {
  padding: 14px;
}

.panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.table-head {
  align-items: center;
}

.two-col {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 14px;
}

.warning-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.warning-row {
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr);
  gap: 10px;
  padding: 10px;
  border: 1px solid #eef2f6;
  border-left: 4px solid #98a2b3;
  border-radius: 8px;
  background: #f8fafc;
}

.warning-row.warning {
  border-left-color: #f79009;
}

.warning-row.danger {
  border-left-color: #d92d20;
}

.warning-row strong,
.quality-grid strong {
  display: block;
  color: #1f2a44;
  margin-bottom: 4px;
}

.warning-row span,
.quality-grid span,
.quality-grid small {
  color: #667085;
  line-height: 1.45;
}

.quality-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.quality-grid > div {
  min-height: 82px;
  padding: 10px;
  border: 1px solid #eef2f6;
  border-radius: 8px;
  background: #f8fafc;
}

.quality-grid strong {
  font-size: 20px;
  line-height: 1.2;
}

.recovery-grid {
  grid-template-columns: repeat(4, minmax(0, 1fr));
  margin-bottom: 12px;
}

.recovery-scenario-table {
  margin-top: 12px;
}

.strategy-cell {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.strategy-cell strong {
  color: #1f2a44;
  font-size: 13px;
}

.strategy-cell small {
  color: #667085;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.strategy-detail-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.strategy-detail-card {
  border: 1px solid #e4e9f1;
  border-left: 4px solid #98a2b3;
  border-radius: 8px;
  background: #fbfcff;
  padding: 12px;
  min-width: 0;
}

.strategy-detail-card.mainwave {
  border-left-color: #12b76a;
}

.strategy-detail-card.repair {
  border-left-color: #2f80ed;
}

.strategy-detail-card.panic {
  border-left-color: #f79009;
}

.strategy-detail-card.supplement {
  border-left-color: #667085;
}

.strategy-detail-card.breakout {
  border-left-color: #7a5af8;
}

.strategy-detail-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 8px;
}

.strategy-role {
  display: block;
  color: #667085;
  font-size: 12px;
  margin-bottom: 3px;
}

.strategy-detail-head h3 {
  margin: 0;
  color: #1f2a44;
  font-size: 16px;
  line-height: 1.25;
}

.strategy-summary {
  color: #42526e;
  line-height: 1.55;
  margin-bottom: 10px;
}

.strategy-stats {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 6px;
  margin-bottom: 10px;
}

.strategy-stats > div {
  min-height: 54px;
  border: 1px solid #eef2f6;
  border-radius: 8px;
  background: #fff;
  padding: 7px;
}

.strategy-stats span {
  display: block;
  color: #667085;
  font-size: 12px;
  margin-bottom: 4px;
}

.strategy-stats strong {
  display: block;
  color: #1f2a44;
  font-size: 15px;
  line-height: 1.2;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.strategy-contract-list {
  display: grid;
  grid-template-columns: 1fr;
  gap: 8px;
  margin: 0;
}

.strategy-contract-list div {
  display: grid;
  grid-template-columns: 78px minmax(0, 1fr);
  gap: 8px;
}

.strategy-contract-list dt {
  color: #344054;
  font-weight: 700;
}

.strategy-contract-list dd {
  margin: 0;
  color: #667085;
  line-height: 1.5;
  min-width: 0;
}

.pipeline {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.stage {
  min-height: 86px;
  border: 1px solid #e4e9f1;
  border-radius: 8px;
  padding: 10px;
  background: #f8fafc;
  border-left: 4px solid #98a2b3;
}

.stage-pass {
  border-left-color: #12b76a;
}

.stage-blocked {
  border-left-color: #d92d20;
}

.stage-locked {
  border-left-color: #b54708;
}

.stage-idle {
  border-left-color: #98a2b3;
}

.stage-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 8px;
}

.stage-title span {
  font-size: 14px;
  font-weight: 700;
}

.stage-title strong {
  font-size: 18px;
}

.stage p {
  color: #667085;
  line-height: 1.4;
}

@media (max-width: 1320px) {
  .metric-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .pipeline {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .strategy-detail-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 980px) {
  .two-col {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 760px) {
  .topbar,
  .panel-head {
    flex-direction: column;
    align-items: flex-start;
  }

  .metric-grid,
  .pipeline,
  .strategy-stats,
  .quality-grid {
    grid-template-columns: 1fr;
  }

  .strategy-contract-list div {
    grid-template-columns: 1fr;
  }
}
</style>
