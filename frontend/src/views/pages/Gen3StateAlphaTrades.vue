<template>
  <div class="trades-page">
    <header class="topbar">
      <div>
        <div class="eyebrow">G3 正式五策略</div>
        <h1>历史成交复盘</h1>
        <p>上半部分看收益曲线、窗口表现和成交明细；下半部分按日期复盘候选池、选中票、跳过原因和当日决策。</p>
      </div>
      <div class="actions">
        <el-button type="primary" :loading="loading" @click="load">刷新</el-button>
      </div>
    </header>

    <el-alert
      :closable="false"
      show-icon
      type="warning"
      :title="fusionText"
    />

    <section class="panel current-shadow-panel">
      <div class="panel-head">
        <div>
          <h2>今日影子买入</h2>
          <p>这里读取运行态影子台账；下方成交明细是历史回测闭合成交，不包含今天尚未退出的影子票。</p>
        </div>
        <el-tag :type="currentShadowTickets.length ? 'warning' : 'info'" effect="dark">
          {{ currentSummary.entry_date || '--' }} / {{ currentShadowTickets.length ? '有影子票' : '暂无影子票' }}
        </el-tag>
      </div>
      <el-table :data="currentShadowTickets" stripe size="small" empty-text="暂无今日影子买入">
        <el-table-column prop="entry_date" label="入场日" width="104" />
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column prop="route_label" label="路由" width="120">
          <template #default="{ row }">{{ row.route_label || routeName(row.route) }}</template>
        </el-table-column>
        <el-table-column prop="planned_entry_ts" label="计划时间" width="160" show-overflow-tooltip />
        <el-table-column label="仓位" width="82" align="right">
          <template #default="{ row }">{{ pct(row.position_pct) }}</template>
        </el-table-column>
        <el-table-column label="参考价" width="90" align="right">
          <template #default="{ row }">{{ price(row.reference_close) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="190" show-overflow-tooltip>
          <template #default="{ row }">{{ compactStatusWithZh(row.shadow_status) }}</template>
        </el-table-column>
        <el-table-column prop="block_reason" label="阻断/说明" min-width="220" show-overflow-tooltip />
      </el-table>
    </section>

    <section class="filters panel">
      <el-form :inline="true" label-position="top">
        <el-form-item label="路由">
          <el-select v-model="query.route" class="filter-control" @change="load">
            <el-option label="全部路由" value="all" />
            <el-option label="机构主升浪" value="institutional_mainwave" />
            <el-option label="恐慌修复" value="panic_repair" />
            <el-option label="旧G3原生来源" value="old_g3_route_v3" />
          </el-select>
        </el-form-item>
        <el-form-item label="窗口">
          <el-select v-model="query.window" class="filter-control" @change="load">
            <el-option label="全周期" value="all" />
            <el-option label="2024年9月前" value="pre_2024_09" />
            <el-option label="2024年9月后" value="post_2024_09" />
            <el-option label="2022弱市" value="weak_2022" />
            <el-option label="2024验证段" value="valid_2024" />
            <el-option label="2026年内盲测" value="blind_2026ytd" />
          </el-select>
        </el-form-item>
        <el-form-item label="排序">
          <el-select v-model="query.sort_by" class="filter-control" @change="load">
            <el-option label="入场日" value="entry_date" />
            <el-option label="收益率" value="net_ret" />
            <el-option label="实现盈亏" value="realized_pnl" />
            <el-option label="得分" value="score" />
          </el-select>
        </el-form-item>
        <el-form-item label="方向">
          <el-segmented v-model="query.sort_order" :options="sortOptions" @change="load" />
        </el-form-item>
        <el-form-item label="行数">
          <el-input-number v-model="query.limit" :min="50" :max="2000" :step="50" @change="load" />
        </el-form-item>
      </el-form>
    </section>

    <section class="metric-grid">
      <div class="metric-card">
        <span>交易数</span>
        <strong>{{ fmt(metrics.trade_count, 0) }}</strong>
        <small>盈利 {{ fmt(metrics.winning_trades, 0) }} / 亏损 {{ fmt(metrics.losing_trades, 0) }}</small>
      </div>
      <div class="metric-card">
        <span>胜率</span>
        <strong>{{ pct(metrics.win_rate) }}</strong>
        <small>均笔 {{ pct(metrics.avg_trade_return) }}</small>
      </div>
      <div class="metric-card">
        <span>中位收益</span>
        <strong>{{ pct(metrics.median_trade_return) }}</strong>
        <small>最好 {{ pct(metrics.best_trade) }}</small>
      </div>
      <div class="metric-card danger">
        <span>最差单笔</span>
        <strong>{{ pct(metrics.worst_trade) }}</strong>
        <small>单笔大亏仍需继续压缩</small>
      </div>
      <div class="metric-card accent">
        <span>实现盈亏</span>
        <strong>{{ money(metrics.sum_realized_pnl) }}</strong>
        <small>按回测资金口径</small>
      </div>
      <div class="metric-card locked">
        <span>G2退场</span>
        <strong>{{ assessment.g2_can_exit_now ? '可以' : '暂不' }}</strong>
        <small>先降权观察，不直接删除</small>
      </div>
    </section>

    <section class="panel natural-panel">
      <div class="panel-head">
        <div>
          <h2>自然交易纪律</h2>
          <p>这是一层影子合同，只用于观察交易行为是否自然、可解释、可执行，不改变正式买卖决策。</p>
        </div>
        <el-tag type="warning" effect="plain">
          {{ naturalPolicyStatusText }}
        </el-tag>
      </div>

      <div class="natural-summary-grid">
        <div>
          <span>N1 同票跳过</span>
          <strong>{{ fmt(naturalSummary.skip_count, 0) }}</strong>
          <small>避免隐式加仓伪装成独立二槽</small>
        </div>
        <div>
          <span>N3 补位降仓</span>
          <strong>{{ fmt(naturalSummary.reduced_position_count, 0) }}</strong>
          <small>G2 空档补位回到补位身份</small>
        </div>
        <div>
          <span>N2 退出复核</span>
          <strong>{{ fmt(naturalSummary.early_exit_review_count, 0) }}</strong>
          <small>被动到期仓位需要继续持有证据</small>
        </div>
        <div>
          <span>N5 买点体检</span>
          <strong>{{ fmt(naturalSummary.buy_point_review_count, 0) }}</strong>
          <small>开仓后三日快速失败进入复盘池</small>
        </div>
      </div>

      <div class="two-col natural-tables">
        <el-table :data="naturalEntrySummary" stripe size="small" empty-text="暂无入场纪律汇总">
          <el-table-column prop="natural_action_label" label="入场动作" min-width="120" />
          <el-table-column label="交易数" width="86" align="right">
            <template #default="{ row }">{{ fmt(row.trade_count, 0) }}</template>
          </el-table-column>
          <el-table-column label="胜率" width="86" align="right">
            <template #default="{ row }">{{ pct(row.win_rate) }}</template>
          </el-table-column>
          <el-table-column label="均笔" width="86" align="right">
            <template #default="{ row }">{{ pct(row.avg_ret) }}</template>
          </el-table-column>
          <el-table-column label="原始盈亏" width="112" align="right">
            <template #default="{ row }">{{ money(row.original_pnl) }}</template>
          </el-table-column>
        </el-table>

        <el-table :data="naturalExitSummary" stripe size="small" empty-text="暂无卖出复核汇总">
          <el-table-column prop="exit_shadow_label" label="卖出复核" min-width="130" />
          <el-table-column label="交易数" width="86" align="right">
            <template #default="{ row }">{{ fmt(row.trade_count, 0) }}</template>
          </el-table-column>
          <el-table-column label="胜率" width="86" align="right">
            <template #default="{ row }">{{ pct(row.win_rate) }}</template>
          </el-table-column>
          <el-table-column label="均笔" width="86" align="right">
            <template #default="{ row }">{{ pct(row.avg_ret) }}</template>
          </el-table-column>
          <el-table-column label="原始盈亏" width="112" align="right">
            <template #default="{ row }">{{ money(row.original_pnl) }}</template>
          </el-table-column>
        </el-table>
      </div>
    </section>

    <section class="two-col">
      <div class="panel chart-panel">
        <div class="panel-head">
          <div>
            <h2>收益曲线</h2>
            <p>按历史回测权益、累计收益和回撤展示，方便观察收益来源是否集中在单一行情窗口。</p>
          </div>
        </div>
        <div ref="curveEl" class="equity-chart"></div>
      </div>

      <div class="panel audit-panel">
        <div class="panel-head">
          <div>
            <h2>未来函数与仓位审计</h2>
            <p>检查确认时间、上下文日期、候选排序和交易时段实际仓位。</p>
          </div>
          <el-tag :type="futureLeakAudit.has_high_risk_issue ? 'danger' : 'success'" size="small">
            {{ futureLeakAudit.has_high_risk_issue ? '需修复' : '通过' }}
          </el-tag>
        </div>

        <el-alert
          v-if="futureLeakAudit.has_high_risk_issue"
          class="audit-alert"
          type="error"
          :closable="false"
          show-icon
          title="发现高风险未来函数：历史候选排序疑似使用最终收益 net_ret。"
        />

        <div class="exposure-grid">
          <div>
            <span>交易日平均仓位</span>
            <strong>{{ pct(exposure.avg_exposure_active_days) }}</strong>
          </div>
          <div>
            <span>全周期平均仓位</span>
            <strong>{{ pct(exposure.avg_exposure_all_days) }}</strong>
          </div>
          <div>
            <span>持仓日占比</span>
            <strong>{{ pct(exposure.active_day_ratio) }}</strong>
          </div>
          <div>
            <span>平均持仓数</span>
            <strong>{{ fmt(exposure.avg_open_positions_active_days, 2) }}</strong>
          </div>
          <div>
            <span>最大持仓数</span>
            <strong>{{ fmt(exposure.max_open_positions, 0) }}</strong>
          </div>
          <div>
            <span>单槽仓位</span>
            <strong>{{ pct(exposure.median_slot_pct) }}</strong>
          </div>
        </div>

        <p class="audit-comment">{{ exposure.comment || '--' }}</p>
        <div class="audit-list">
          <div v-for="item in futureLeakAudit.issues || []" :key="item.item" class="audit-item danger">
            <strong>{{ item.item }}</strong>
            <span>{{ item.impact }}</span>
          </div>
          <div v-for="item in futureLeakAudit.passes || []" :key="item.item" class="audit-item pass">
            <strong>{{ item.item }}</strong>
            <span>{{ item.evidence }}</span>
          </div>
        </div>
      </div>
    </section>

    <section class="two-col">
      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>路由成交归因</h2>
            <p>按正式交易策略归并相似成交，来源路由保留追溯。</p>
          </div>
        </div>
        <el-table :data="routeMetrics" stripe size="small" empty-text="暂无路由归因">
          <el-table-column prop="trade_strategy_label" label="交易策略" min-width="170">
            <template #default="{ row }">
              <div class="route-strategy-cell">
                <strong>{{ row.route_label || routeStrategyName(row) }}</strong>
                <small v-if="row.strategy_summary">
                  含：{{ row.strategy_summary }}
                </small>
                <small v-else-if="row.parent_route_label || row.route_parent_label">
                  来源：{{ row.parent_route_label || row.route_parent_label }}
                </small>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="交易数" width="86" align="right">
            <template #default="{ row }">{{ fmt(row.trade_count, 0) }}</template>
          </el-table-column>
          <el-table-column label="胜率" width="86" align="right">
            <template #default="{ row }">{{ pct(row.win_rate) }}</template>
          </el-table-column>
          <el-table-column label="均笔" width="88" align="right">
            <template #default="{ row }">{{ pct(row.avg_trade_return) }}</template>
          </el-table-column>
          <el-table-column label="最差" width="88" align="right">
            <template #default="{ row }">{{ pct(row.worst_trade) }}</template>
          </el-table-column>
          <el-table-column label="盈亏" width="120" align="right">
            <template #default="{ row }">{{ money(row.sum_realized_pnl) }}</template>
          </el-table-column>
        </el-table>
      </div>

      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>窗口表现</h2>
            <p>重点看 2024年9月前后差异，防止单一行情拟合。</p>
          </div>
        </div>
        <el-table :data="windowMetrics" stripe size="small" empty-text="暂无窗口表现">
          <el-table-column prop="window" label="窗口" min-width="126" />
          <el-table-column label="交易数" width="86" align="right">
            <template #default="{ row }">{{ fmt(row.trade_count, 0) }}</template>
          </el-table-column>
          <el-table-column label="收益" width="90" align="right">
            <template #default="{ row }">{{ pct(row.return) }}</template>
          </el-table-column>
          <el-table-column label="回撤" width="90" align="right">
            <template #default="{ row }">{{ pct(row.max_drawdown) }}</template>
          </el-table-column>
          <el-table-column label="胜率" width="86" align="right">
            <template #default="{ row }">{{ pct(row.win_rate) }}</template>
          </el-table-column>
        </el-table>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>成交明细</h2>
          <p>每一笔都保留入场、退出、路由、市场状态、收益和正式交易闸门。</p>
        </div>
      </div>
        <el-table :data="trades" stripe size="small" height="560" empty-text="暂无历史成交">
        <el-table-column prop="entry_date" label="入场日" width="104" sortable />
        <el-table-column label="退出日" width="104">
          <template #default="{ row }">{{ row.trade_status === 'open_shadow' ? '持有中' : row.policy_exit_date }}</template>
        </el-table-column>
        <el-table-column label="买入时间" width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ entryTimeText(row) }}</template>
        </el-table-column>
        <el-table-column label="卖出时间" width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ exitTimeText(row) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="row.trade_status === 'open_shadow' ? 'warning' : 'success'">
              {{ row.trade_status === 'open_shadow' ? '影子持有' : '已退出' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column label="路由" width="116">
          <template #default="{ row }">{{ row.route_label || routeName(row.route) }}</template>
        </el-table-column>
        <el-table-column label="策略" width="146">
          <template #default="{ row }">{{ row.route_strategy_label || routeStrategyName(row) }}</template>
        </el-table-column>
        <el-table-column label="收益" width="90" align="right">
          <template #default="{ row }">
            <span :class="Number(row.net_ret) >= 0 ? 'pos' : 'neg'">{{ pct(row.net_ret) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="盈亏" width="118" align="right">
          <template #default="{ row }">
            <span :class="Number(row.realized_pnl) >= 0 ? 'pos' : 'neg'">{{ money(row.realized_pnl) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="入场价" width="90" align="right">
          <template #default="{ row }">{{ price(row.entry_price) }}</template>
        </el-table-column>
        <el-table-column label="仓位" width="76" align="right">
          <template #default="{ row }">{{ pct(row.slot_pct) }}</template>
        </el-table-column>
        <el-table-column label="得分" width="92" align="right">
          <template #default="{ row }">
            <div class="score-cell">
              <strong>{{ fmt(row.score, 2) }}</strong>
              <small>{{ scoreScale(row) }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="market_style" label="市场风格" width="138" />
        <el-table-column label="指数20日" width="92" align="right">
          <template #default="{ row }">{{ pct(row.index_mom20) }}</template>
        </el-table-column>
        <el-table-column label="上涨率" width="86" align="right">
          <template #default="{ row }">{{ pct(row.up_rate) }}</template>
        </el-table-column>
        <el-table-column prop="confirm_datetime" label="确认时间" width="160" show-overflow-tooltip />
        <el-table-column label="Live" width="72">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.live_ready) ? 'success' : 'info'">
              {{ truthy(row.live_ready) ? '是' : '否' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="正式" width="72">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.formal_buy_signal) ? 'danger' : 'info'">
              {{ truthy(row.formal_buy_signal) ? '开' : '关' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="自然纪律" width="150" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="natural-cell">
              <el-tag size="small" :type="naturalActionTagType(row)">
                {{ row.natural_action_label || '未标记' }}
              </el-tag>
              <small v-if="row.exit_shadow_label && row.exit_shadow_action !== 'none'">
                {{ row.exit_shadow_label }}
              </small>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="block_reason" label="阻断/说明" min-width="220" show-overflow-tooltip />
      </el-table>
    </section>

    <section class="panel replay-panel">
      <div class="panel-head">
        <div>
          <h2>逐日买入与候选复盘</h2>
          <p>核心校验每天应该买入哪些票、候选池是否完整、买入票是否来自合格候选，以及阻断票为什么没有进入买入。</p>
        </div>
        <div class="actions">
          <el-select v-model="replayDate" class="filter-control" placeholder="选择复盘日期">
            <el-option v-for="item in replayDateOptions" :key="item" :label="item" :value="item" />
          </el-select>
        </div>
      </div>

      <el-alert
        v-if="!currentReplayRows.length"
        type="info"
        :closable="false"
        show-icon
        title="当前接口尚未返回该日全量候选归档；已先展示历史成交切片。后续补后端候选归档后，这里会承接全周期逐日候选复盘。"
      />

      <div class="replay-summary-grid">
        <div>
          <span>复盘日期</span>
          <strong>{{ activeReplayDate || '--' }}</strong>
          <small>历史成交 {{ replayTrades.length }} 笔；未发生交易日允许为空</small>
        </div>
        <div>
          <span>推荐买入</span>
          <strong>{{ currentReplayTickets.length }}</strong>
          <small>{{ replayRecommendedNames || '--' }}</small>
        </div>
        <div>
          <span>候选池</span>
          <strong>{{ currentReplayRows.length }}</strong>
          <small>合格 {{ replayEligibleCount }} / 阻断 {{ replayBlockedCount }}</small>
        </div>
        <div>
          <span>校验结论</span>
          <strong>{{ replayDecisionStatus }}</strong>
          <small>{{ replayCandidateAuditText }}</small>
        </div>
      </div>

      <el-alert
        v-if="currentReplayRows.length"
        class="audit-alert"
        :type="replayDecisionStatusType"
        :closable="false"
        show-icon
        :title="replayCandidateAuditText"
      />

      <el-table :data="replayTrades" stripe size="small" class="replay-table" empty-text="该日暂无历史成交">
        <el-table-column prop="entry_date" label="入场日" width="104" />
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column label="路由" width="124">
          <template #default="{ row }">{{ row.route_label || routeName(row.route) }}</template>
        </el-table-column>
        <el-table-column label="收益" width="90" align="right">
          <template #default="{ row }">
            <span :class="Number(row.net_ret) >= 0 ? 'pos' : 'neg'">{{ pct(row.net_ret) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="仓位" width="76" align="right">
          <template #default="{ row }">{{ pct(row.slot_pct) }}</template>
        </el-table-column>
        <el-table-column label="自然纪律" width="150" show-overflow-tooltip>
          <template #default="{ row }">
            <el-tag size="small" :type="naturalActionTagType(row)">
              {{ row.natural_action_label || row.exit_shadow_label || '未标记' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="block_reason" label="说明" min-width="220" show-overflow-tooltip />
      </el-table>

      <el-table :data="currentReplayRows" stripe size="small" class="replay-table" height="360" empty-text="暂无该日全量候选">
        <el-table-column label="类型" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="replayRowKindTagType(row)">{{ replayRowKind(row) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column label="路由" width="124">
          <template #default="{ row }">{{ row.route_label || routeName(row.route) }}</template>
        </el-table-column>
        <el-table-column label="准入" width="78">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.router_eligible || row.qualified_shadow_buy) ? 'success' : 'warning'">
              {{ truthy(row.router_eligible || row.qualified_shadow_buy) ? '通过' : '阻断' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="30m" width="78">
          <template #default="{ row }">
            <el-tag size="small" :type="truthy(row.m30_confirmed) ? 'success' : 'info'">{{ compactStatusWithZh(row.m30_status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="得分" width="90" align="right">
          <template #default="{ row }">{{ fmt(displayScore(row), 2) }}</template>
        </el-table-column>
        <el-table-column prop="confirm_datetime" label="确认时间" width="160" show-overflow-tooltip />
        <el-table-column label="影子状态" width="190" show-overflow-tooltip>
          <template #default="{ row }">{{ compactStatusWithZh(row.shadow_status) }}</template>
        </el-table-column>
        <el-table-column prop="block_reason" label="阻断/说明" min-width="240" show-overflow-tooltip />
      </el-table>
    </section>

    <section class="two-col">
      <div class="panel">
        <div class="panel-head">
          <div>
            <h2>市场风格归因</h2>
            <p>检查哪些市场环境贡献收益，哪些环境拖累。</p>
          </div>
        </div>
        <el-table :data="marketStyleMetrics" stripe size="small" empty-text="暂无风格归因">
          <el-table-column prop="market_style" label="市场风格" min-width="130" />
          <el-table-column label="交易数" width="86" align="right">
            <template #default="{ row }">{{ fmt(row.trade_count, 0) }}</template>
          </el-table-column>
          <el-table-column label="胜率" width="86" align="right">
            <template #default="{ row }">{{ pct(row.win_rate) }}</template>
          </el-table-column>
          <el-table-column label="均笔" width="90" align="right">
            <template #default="{ row }">{{ pct(row.avg_trade_return) }}</template>
          </el-table-column>
          <el-table-column label="最差" width="90" align="right">
            <template #default="{ row }">{{ pct(row.worst_trade) }}</template>
          </el-table-column>
        </el-table>
      </div>

      <div class="panel">
        <div class="panel-head">
          <div>
              <h2>正式合同验收</h2>
            <p>不是把 G2 当成独立主策略保留，而是确认补位、提醒、台账和稳定运行能力已经归入 G3。</p>
          </div>
        </div>
        <div class="gate-list">
          <div v-for="item in replacementGates" :key="item.name" class="gate-row">
            <el-tag :type="gateTagType(item)" size="small">{{ gateText(item) }}</el-tag>
            <div>
              <strong>{{ item.name }}</strong>
              <span>{{ item.detail }}</span>
            </div>
          </div>
          <el-empty v-if="!replacementGates.length" description="暂无替代审计门槛" />
          <div v-for="item in assessment.minimum_exit_gates || []" :key="item" class="gate-row">
            <el-tag type="warning" size="small">待确认</el-tag>
            <span>{{ item }}</span>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import * as echarts from 'echarts'
import { compactStatusWithZh } from '@/utils/g3StatusText'
import { getGen3StateAlphaCurrent, getGen3StateAlphaHistoricalTrades } from '@/api/trading'

const loading = ref(false)
const payload = ref({})
const currentPayload = ref({})
const replayDate = ref('')
const curveEl = ref(null)
let curveChart = null
const query = reactive({
  route: 'all',
  window: 'all',
  sort_by: 'entry_date',
  sort_order: 'desc',
  limit: 300
})

const sortOptions = [
  { label: '降序', value: 'desc' },
  { label: '升序', value: 'asc' }
]

const trades = computed(() => Array.isArray(payload.value.historical_trades) ? payload.value.historical_trades : [])
const metrics = computed(() => payload.value.metrics || {})
const routeMetrics = computed(() => Array.isArray(payload.value.route_metrics) ? payload.value.route_metrics : [])
const windowMetrics = computed(() => Array.isArray(payload.value.window_metrics) ? payload.value.window_metrics : [])
const marketStyleMetrics = computed(() => Array.isArray(payload.value.market_style_metrics) ? payload.value.market_style_metrics : [])
const assessment = computed(() => payload.value.replacement_assessment || {})
const replacementGates = computed(() => Array.isArray(assessment.value.gates) ? assessment.value.gates : [])
const equityCurve = computed(() => Array.isArray(payload.value.equity_curve) ? payload.value.equity_curve : [])
const exposure = computed(() => payload.value.exposure_metrics || {})
const futureLeakAudit = computed(() => payload.value.future_leak_audit || {})
const naturalPolicyShadow = computed(() => payload.value.natural_policy_shadow || {})
const naturalSummary = computed(() => naturalPolicyShadow.value.summary || {})
const naturalContract = computed(() => naturalPolicyShadow.value.contract || {})
const naturalEntrySummary = computed(() => (
  Array.isArray(naturalPolicyShadow.value.entry_position_summary) ? naturalPolicyShadow.value.entry_position_summary : []
))
const naturalExitSummary = computed(() => (
  Array.isArray(naturalPolicyShadow.value.exit_summary) ? naturalPolicyShadow.value.exit_summary : []
))
const naturalPolicyStatusText = computed(() => {
  const status = naturalContract.value.status || naturalSummary.value.status || 'shadow_only'
  if (status === 'shadow_only') return '影子观察'
  return compactStatusWithZh(status)
})
const currentSummary = computed(() => currentPayload.value.summary || {})
const currentShadowTickets = computed(() => (
  Array.isArray(currentPayload.value.shadow_tickets) ? currentPayload.value.shadow_tickets : []
))
const currentAfterhoursTickets = computed(() => (
  Array.isArray(currentPayload.value.afterhours_shadow_tickets) ? currentPayload.value.afterhours_shadow_tickets : []
))
const currentNextTradeTickets = computed(() => (
  Array.isArray(currentPayload.value.next_trade_buy_tickets) ? currentPayload.value.next_trade_buy_tickets : []
))
const currentAllCandidates = computed(() => (
  Array.isArray(currentPayload.value.all_source_candidates) ? currentPayload.value.all_source_candidates : []
))
const fusionText = computed(() => assessment.value.verdict || 'G3 正在按最终版合同融合 G2 补位能力；观察期内保留深链验收，不再作为独立主策略展示。')
const replayDateOptions = computed(() => {
  const dates = new Set()
  trades.value.forEach((row) => {
    if (row.entry_date) dates.add(String(row.entry_date))
  })
  currentAllCandidates.value.forEach((row) => {
    const date = replayRowDate(row)
    if (date) dates.add(String(date).slice(0, 10))
  })
  ;[currentNextTradeTickets.value, currentAfterhoursTickets.value, currentShadowTickets.value].forEach((rows) => {
    rows.forEach((row) => {
      const date = replayRowDate(row)
      if (date) dates.add(String(date).slice(0, 10))
    })
  })
  return Array.from(dates).sort().reverse()
})
const activeReplayDate = computed(() => replayDate.value || replayDateOptions.value[0] || '')
const replayTrades = computed(() => {
  if (!activeReplayDate.value) return []
  return trades.value.filter((row) => String(row.entry_date || '').slice(0, 10) === activeReplayDate.value)
})
const currentReplayCandidates = computed(() => {
  if (!activeReplayDate.value) return []
  return currentAllCandidates.value.filter((row) => {
    const date = replayRowDate(row)
    return String(date || '').slice(0, 10) === activeReplayDate.value
  })
})
const currentReplayTickets = computed(() => {
  if (!activeReplayDate.value) return []
  return dedupeReplayRows([
    ...currentNextTradeTickets.value,
    ...currentAfterhoursTickets.value,
    ...currentShadowTickets.value
  ].filter((row) => String(replayRowDate(row) || '').slice(0, 10) === activeReplayDate.value))
})
const currentReplayRows = computed(() => dedupeReplayRows([
  ...currentReplayTickets.value,
  ...currentReplayCandidates.value
]))
const replayEligibleCount = computed(() => currentReplayRows.value.filter((row) => truthy(row.router_eligible || row.qualified_shadow_buy)).length)
const replayBlockedCount = computed(() => currentReplayRows.value.filter((row) => !truthy(row.router_eligible || row.qualified_shadow_buy) || row.block_reason).length)
const replayTopBlockReason = computed(() => {
  const counts = new Map()
  currentReplayRows.value.forEach((row) => {
    const reason = String(row.block_reason || row.block_detail || '').trim()
    if (!reason) return
    counts.set(reason, (counts.get(reason) || 0) + 1)
  })
  return Array.from(counts.entries()).sort((a, b) => b[1] - a[1])[0]?.[0] || ''
})
const replayRecommendedNames = computed(() => (
  currentReplayTickets.value.map((item) => item.name || item.code).filter(Boolean).join(' / ')
))
const replayDecisionStatus = computed(() => {
  if (!currentReplayRows.value.length) return '无归档'
  if (!currentReplayTickets.value.length) return '无买入'
  const invalidTickets = currentReplayTickets.value.filter((row) => !truthy(row.router_eligible || row.qualified_shadow_buy))
  return invalidTickets.length ? '需复核' : '正确'
})
const replayDecisionStatusType = computed(() => {
  if (!currentReplayRows.value.length) return 'info'
  if (!currentReplayTickets.value.length) return 'warning'
  return replayDecisionStatus.value === '正确' ? 'success' : 'warning'
})
const replayCandidateAuditText = computed(() => {
  if (!activeReplayDate.value) return '请选择复盘日期。'
  if (!currentReplayRows.value.length) return `${activeReplayDate.value} 暂无买入票据或候选归档。`
  if (!currentReplayTickets.value.length) {
    const reason = replayTopBlockReason.value ? `主阻断：${replayTopBlockReason.value}` : '未发现合格买入票。'
    return `${activeReplayDate.value} 没有推荐买入；候选 ${currentReplayRows.value.length} 条，${reason}`
  }
  const blockedText = replayBlockedCount.value ? `另有 ${replayBlockedCount.value} 条候选被阻断。` : '无阻断候选。'
  return `${activeReplayDate.value} 推荐买入：${replayRecommendedNames.value}；买入票来自合格票据，${blockedText}`
})

function replayRowDate(row) {
  return row?.entry_date || row?.signal_date || row?.decision_date || currentSummary.value.entry_date
}

function isReplayTicket(row) {
  return Boolean(row?.ticket_key || row?.qualified_shadow_buy || row?.paper_trade_ready)
}

function replayRowKind(row) {
  if (isReplayTicket(row)) return '推荐买入'
  if (truthy(row?.router_eligible)) return '合格候选'
  return '阻断候选'
}

function replayRowKindTagType(row) {
  if (isReplayTicket(row)) return 'success'
  if (truthy(row?.router_eligible)) return 'primary'
  return 'warning'
}

function replayRowKey(row) {
  return [
    row?.ticket_key || '',
    row?.candidate_key || '',
    row?.entry_date || '',
    row?.route || '',
    row?.code || ''
  ].filter(Boolean).join('|')
}

function dedupeReplayRows(rows) {
  const seen = new Set()
  const out = []
  rows.forEach((row) => {
    const key = replayRowKey(row)
    if (!key || seen.has(key)) return
    seen.add(key)
    out.push(row)
  })
  return out
}

function gateTagType(item) {
  if (item?.ok) return 'success'
  if (item?.blocking) return 'danger'
  return 'warning'
}

function gateText(item) {
  if (item?.ok) return '通过'
  if (item?.blocking) return '阻断'
  return '观察'
}

function naturalActionTagType(row) {
  const action = String(row?.natural_action || '')
  const exitAction = String(row?.exit_shadow_action || '')
  if (action === 'skip') return 'danger'
  if (exitAction === 'buy_point_review') return 'danger'
  if (exitAction === 'early_exit_review') return 'warning'
  if (action === 'allow_reduced') return 'warning'
  if (action === 'allow') return 'success'
  return 'info'
}

function truthy(value) {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0
  if (typeof value === 'string') return ['1', 'true', 'yes', 'ok'].includes(value.trim().toLowerCase())
  return false
}

function entryTimeText(row) {
  return row?.entry_ts || row?.entry_datetime || row?.buy_datetime || row?.planned_entry_ts || row?.entry_date || '--'
}

function exitTimeText(row) {
  if (row?.trade_status === 'open_shadow') return '持有中'
  return row?.exit_ts || row?.exit_datetime || row?.sell_datetime || row?.policy_exit_datetime || row?.exit_date || row?.policy_exit_date || '--'
}

function routeName(route) {
  const names = {
    institutional_mainwave: '机构主升浪',
    score120_core: '机构主升浪',
    strong_main: '强势突破来源',
    panic_repair: '恐慌修复',
    g2_gap_supplement: '量能续强补位',
    old_g3_route_v3: '旧G3原生来源',
    range_gap: '震荡弱势修复来源',
    down_panic: '恐慌出清修复来源'
  }
  return names[route] || route || '--'
}

function routeStrategyName(row) {
  const key = row?.trade_strategy || row?.route_strategy || row?.route
  const names = {
    range_weak_repair: '震荡弱势修复',
    panic_capitulation_repair: '恐慌出清修复',
    institutional_score120_mainwave: '机构主升Score120',
    old_g3_strong_breakout: '强势突破',
    mainwave_breakout_offense: '主升/突破进攻',
    volume_runup_supplement: '量能续强补位',
    institutional_mainwave: '机构主升浪',
    institutional_score120_core: '机构主升浪Score120核心',
    institutional_institutional_mainwave: '机构主升浪',
    score120_core: '机构主升浪Score120核心',
    strong_main: row?.mode === 'old_g3_route_v3' ? '强势突破' : '机构主升浪',
    panic_repair: '恐慌修复',
    panic_repair_range: '震荡恐慌修复',
    panic_repair_downtrend: '下跌恐慌修复',
    g2_gap_supplement: '量能续强补位',
    g2_volume5_keep80_runup_sector_bonus: 'G2量能续强+板块加分',
    g2_volume5_keep80_runup: 'G2量能续强',
    g2_g2_gap_supplement: 'G2补位未细分',
    old_g3_old_g3_route_v3: '旧G3原生来源未细分',
    old_g3_strong_main: '强势突破',
    old_g3_strong_trend_breakout: '强势突破',
    old_g3_range_gap: '震荡弱势修复',
    old_g3_weak_rebound_repair: '震荡弱势修复',
    old_g3_range_box_bottom: '震荡弱势修复观察',
    old_g3_down_panic: '恐慌出清修复',
    old_g3_downtrend_panic_capitulation: '恐慌出清修复'
  }
  if (row?.trade_strategy_label) return row.trade_strategy_label
  if (row?.route_strategy_label) return row.route_strategy_label
  if (row?.mode === 'old_g3_route_v3' && row?.route) return names[`old_g3_${row.route}`] || '旧G3原生来源未细分'
  return names[key] || routeName(row?.route)
}

function displayScore(row) {
  if (row?.route === 'institutional_mainwave') {
    return row.wave_style_score ?? row.selected_score ?? row.score ?? row.candidate_score
  }
  return row.score ?? row.candidate_score ?? row.selected_score ?? row.mode_score
}

function fmt(value, digits = 2) {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  return n.toFixed(digits)
}

function scoreScale(row) {
  const score = Number(row?.score)
  const scale = String(row?.score_scale || '')
  const route = String(row?.route || row?.mode || '')
  const mode = String(row?.mode || '')
  const status = String(row?.trade_status || '')
  if (!Number.isFinite(score)) return '--'
  if (scale === 'mainwave_0_140') return '主升0-140'
  if (scale === 'legacy_scheduler') return '历史调度'
  if (scale === 'route_internal') return route === 'panic_repair' ? '修复0-1' : '路线内分'
  if (status === 'open_shadow' && score >= 20) return '主升0-140'
  if (route === 'institutional_mainwave' && score >= 20) return '主升0-140'
  if (route === 'score120_core' || mode === 'institutional_mainwave') return '历史调度'
  if (route === 'panic_repair') return '修复0-1'
  if (route === 'range_gap' || route === 'down_panic' || route === 'old_g3_route_v3') return '结构0-1'
  return score >= 20 ? '主升0-140' : '路线内分'
}

function pct(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  return `${(n * 100).toFixed(1)}%`
}

function price(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  return n.toFixed(2)
}

function money(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  return n.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}

function renderCurve() {
  if (!curveEl.value) return
  if (!curveChart) {
    curveChart = echarts.init(curveEl.value)
  }
  const rows = equityCurve.value
  if (!rows.length) {
    curveChart.clear()
    return
  }
  const dates = rows.map((row) => row.date)
  const returns = rows.map((row) => {
    const n = Number(row.ret_from_start)
    return Number.isFinite(n) ? Number((n * 100).toFixed(2)) : null
  })
  const mtmReturns = rows.map((row) => {
    const n = Number(row.ret_from_start_mtm)
    return Number.isFinite(n) ? Number((n * 100).toFixed(2)) : null
  })
  const drawdowns = rows.map((row) => {
    const n = Number.isFinite(Number(row.drawdown_mtm)) ? Number(row.drawdown_mtm) : Number(row.drawdown)
    return Number.isFinite(n) ? Number((n * 100).toFixed(2)) : null
  })
  curveChart.setOption({
    animation: false,
    grid: { left: 48, right: 22, top: 36, bottom: 42 },
    tooltip: {
      trigger: 'axis',
      valueFormatter: (value) => `${Number(value).toFixed(2)}%`
    },
    legend: {
      top: 4,
      data: ['账面累计收益', 'MTM累计收益', 'MTM回撤']
    },
    xAxis: {
      type: 'category',
      data: dates,
      boundaryGap: false,
      axisLabel: { color: '#667085' }
    },
    yAxis: {
      type: 'value',
      axisLabel: { formatter: '{value}%', color: '#667085' },
      splitLine: { lineStyle: { color: '#eef2f6' } }
    },
    series: [
      {
        name: '账面累计收益',
        type: 'line',
        data: returns,
        showSymbol: false,
        smooth: false,
        lineStyle: { width: 2, color: '#b42318' },
        itemStyle: { color: '#b42318' }
      },
      {
        name: 'MTM累计收益',
        type: 'line',
        data: mtmReturns,
        showSymbol: false,
        smooth: false,
        lineStyle: { width: 1.5, color: '#f79009' },
        itemStyle: { color: '#f79009' }
      },
      {
        name: 'MTM回撤',
        type: 'line',
        data: drawdowns,
        showSymbol: false,
        smooth: false,
        lineStyle: { width: 1.5, color: '#067647' },
        itemStyle: { color: '#067647' }
      }
    ]
  })
}

function resizeCurve() {
  curveChart?.resize()
}

async function load() {
  loading.value = true
  try {
    const [history, current] = await Promise.all([
      getGen3StateAlphaHistoricalTrades({ ...query }),
      getGen3StateAlphaCurrent({ limit: 30 })
    ])
    payload.value = history
    currentPayload.value = current
    if (!replayDate.value && replayDateOptions.value.length) {
      replayDate.value = replayDateOptions.value[0]
    }
    await nextTick()
    renderCurve()
  } catch (error) {
    ElMessage.warning(error?.message || '读取 G3 历史成交失败')
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  load()
  window.addEventListener('resize', resizeCurve)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resizeCurve)
  curveChart?.dispose()
  curveChart = null
})
</script>

<style scoped>
.trades-page {
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

.topbar h1 {
  margin: 0 0 6px;
  font-size: 26px;
}

.topbar p,
.panel-head p,
.metric-card small,
.gate-row span {
  margin: 0;
  color: #667085;
  line-height: 1.5;
}

.gate-row strong {
  display: block;
  margin-bottom: 3px;
  color: #1f2a44;
  font-size: 13px;
}

.actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.panel {
  padding: 14px;
}

.chart-panel,
.audit-panel {
  min-height: 390px;
}

.equity-chart {
  width: 100%;
  height: 320px;
  min-height: 320px;
}

.replay-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.replay-summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.replay-summary-grid > div {
  min-width: 0;
  border: 1px solid #eef2f6;
  border-radius: 8px;
  padding: 10px;
  background: #fbfcff;
}

.replay-summary-grid span,
.replay-summary-grid small {
  display: block;
  color: #667085;
  font-size: 12px;
  line-height: 1.45;
}

.replay-summary-grid strong {
  display: block;
  margin: 4px 0 2px;
  color: #1f2a44;
  font-size: 18px;
  line-height: 1.25;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.replay-table {
  width: 100%;
}

.audit-alert {
  margin-bottom: 12px;
}

.exposure-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 12px;
}

.exposure-grid > div {
  border: 1px solid #eef2f6;
  border-radius: 8px;
  padding: 10px;
  min-width: 0;
}

.exposure-grid span,
.audit-item span {
  display: block;
  color: #667085;
  font-size: 12px;
  line-height: 1.45;
}

.exposure-grid strong {
  display: block;
  margin-top: 5px;
  color: #1f2a44;
  font-size: 18px;
}

.audit-comment {
  margin: 0 0 12px;
  padding: 10px 12px;
  border-radius: 8px;
  background: #f8fafc;
  color: #344054;
  line-height: 1.5;
}

.audit-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 150px;
  overflow: auto;
}

.audit-item {
  border-left: 3px solid #12b76a;
  background: #f8fafc;
  padding: 8px 10px;
}

.audit-item.danger {
  border-left-color: #d92d20;
}

.audit-item.pass {
  border-left-color: #12b76a;
}

.audit-item strong {
  display: block;
  margin-bottom: 3px;
  color: #1f2a44;
  font-size: 12px;
}

.filters :deep(.el-form-item) {
  margin-bottom: 0;
}

.filter-control {
  width: 180px;
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

.natural-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.natural-summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.natural-summary-grid > div {
  min-width: 0;
  border: 1px solid #eef2f6;
  border-radius: 8px;
  padding: 10px;
  background: #fbfcff;
}

.natural-summary-grid span,
.natural-summary-grid small {
  display: block;
  color: #667085;
  font-size: 12px;
  line-height: 1.45;
}

.natural-summary-grid strong {
  display: block;
  margin: 4px 0 2px;
  color: #1f2a44;
  font-size: 18px;
  line-height: 1.25;
}

.natural-tables {
  gap: 10px;
}

.natural-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
  line-height: 1.2;
}

.natural-cell small {
  color: #667085;
  font-size: 11px;
  white-space: nowrap;
}

.two-col {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 14px;
}

.panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.panel-head h2 {
  margin: 0 0 6px;
  font-size: 16px;
}

.pos {
  color: #b42318;
  font-weight: 700;
}

.score-cell {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
  line-height: 1.15;
}

.score-cell strong {
  color: #1f2a44;
  font-weight: 700;
}

.score-cell small {
  color: #667085;
  font-size: 11px;
  white-space: nowrap;
}

.route-strategy-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  line-height: 1.2;
}

.route-strategy-cell strong {
  color: #1f2a44;
  font-weight: 700;
}

.route-strategy-cell small {
  color: #667085;
  font-size: 11px;
  white-space: nowrap;
}

.neg {
  color: #067647;
  font-weight: 700;
}

.gate-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.gate-row {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr);
  gap: 10px;
  align-items: flex-start;
  padding-bottom: 10px;
  border-bottom: 1px solid #eef2f6;
}

.gate-row:last-child {
  border-bottom: 0;
  padding-bottom: 0;
}

@media (max-width: 1320px) {
  .metric-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 980px) {
  .natural-summary-grid,
  .replay-summary-grid,
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

  .metric-grid {
    grid-template-columns: 1fr;
  }

  .exposure-grid {
    grid-template-columns: 1fr;
  }
}
</style>
