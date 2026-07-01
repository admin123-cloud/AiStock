<template>
  <div class="system-config">
    <section class="ops-hero">
      <div class="hero-copy">
        <span class="eyebrow">System Command Center</span>
        <h1>系统配置</h1>
        <p>把日常维护收敛成一个主入口：自动调度负责常规更新，手动按钮只用于补链路、复核和救场。</p>
      </div>
      <div class="hero-status-card">
        <span class="status-pill large" :class="coreMaintenance.enabled ? 'enabled' : 'paused'">
          {{ coreMaintenance.enabled ? '自动维护运行中' : '自动维护已暂停' }}
        </span>
        <div class="hero-health">
          <strong :class="`state-${coreMaintenance.overall_status || 'idle'}`">{{ maintenanceStatusText }}</strong>
          <span>今日成功率 {{ formatRate(coreMaintenance.today_success_rate) }}</span>
        </div>
        <div class="hero-meta">
          <span>最近失败：{{ latestFailedText }}</span>
          <span>刷新于：{{ formatDateTime(lastStatusRefreshAt) }}</span>
        </div>
      </div>
    </section>

    <section class="command-grid">
      <article class="command-card primary-command">
        <span class="command-kicker">推荐操作</span>
        <h2>一键修复今日市场数据</h2>
        <p>覆盖交易日历、股票/指数/板块基础数据、当天日线、分钟快照、15/30m分钟K线、板块统计和盘后自检。</p>
        <button
          class="btn hero-btn"
          :disabled="runningTodayFullMarketRefreshNow || isTaskRunning('today_full_market_refresh')"
          @click="refreshTodayFullMarketNow"
        >
          {{ runningTodayFullMarketRefreshNow || isTaskRunning('today_full_market_refresh') ? '正在更新并自检...' : '立即执行今日全市场更新' }}
        </button>
      </article>

      <article class="command-card">
        <span class="command-kicker">自动化</span>
        <h3>{{ coreMaintenance.enabled ? '自动维护已接管' : '自动维护已暂停' }}</h3>
        <p>常规同步建议交给调度器。只有排障时才暂停或重启自动维护。</p>
        <button class="btn secondary" @click="toggleCoreMaintenance">
          {{ coreMaintenance.enabled ? '暂停自动维护' : '启动自动维护' }}
        </button>
      </article>

      <article class="command-card">
        <span class="command-kicker">排障</span>
        <h3>历史分钟K线修复</h3>
        <p>用于回补最近30个交易日股票/指数分钟K线，耗时较长，不作为日常主入口。</p>
        <button
          class="btn danger soft"
          :disabled="runningMarketMinuteHistoryRepairNow || getTaskRaw('market_minute_history_repair')?.is_running"
          @click="triggerMarketMinuteHistoryRepairNow"
        >
          {{ runningMarketMinuteHistoryRepairNow || getTaskRaw('market_minute_history_repair')?.is_running ? '修复中...' : '执行历史分钟修复' }}
        </button>
      </article>
    </section>

    <section class="section-card gateway-section">
      <div class="section-title-row">
        <div>
          <span class="eyebrow">TDX Gateway</span>
          <h2>TDX Gateway 排障入口</h2>
        </div>
        <span class="status-pill" :class="tdxGatewayStatusClass">
          {{ tdxGatewayStatusText }}
        </span>
      </div>
      <div class="maintenance-actions">
        <button class="btn" :disabled="tdxGatewayLoading" @click="refreshTdxGatewayDiagnostics">
          {{ tdxGatewayLoading ? '诊断中...' : '刷新诊断' }}
        </button>
        <button class="btn secondary" :disabled="tdxGatewayProbing || tdxGatewayLoading" @click="probeTdxGateway">
          {{ tdxGatewayProbing ? '探针中...' : '真实取数探针' }}
        </button>
        <button class="btn primary" :disabled="tdxGatewayInitializing || tdxGatewayRestarting" @click="initializeTdxGateway">
          {{ tdxGatewayInitializing ? '初始化中...' : '重新初始化' }}
        </button>
        <button class="btn primary" :disabled="tdxGatewayRecovering || tdxGatewayRestarting || tdxGatewayInitializing" @click="recoverTdxGateway">
          {{ tdxGatewayRecovering ? '恢复中...' : '一键恢复' }}
        </button>
        <button class="btn danger soft" :disabled="tdxGatewayRestarting" @click="restartTdxGateway">
          {{ tdxGatewayRestarting ? '重启请求已发送...' : '重启 Gateway' }}
        </button>
      </div>
      <div class="gateway-verdict" :class="`level-${tdxGatewayVerdict.level || 'unknown'}`">
        <strong>{{ tdxGatewayVerdict.summary || '等待诊断结果' }}</strong>
        <span>排障顺序：{{ tdxGatewayRecoveryOrderText }}</span>
      </div>
      <div class="gateway-grid">
        <article class="gateway-card">
          <span>后端访问地址</span>
          <strong>{{ tdxGatewayDiagnostics.gateway_url || '-' }}</strong>
          <small>Docker 后端通过这个地址访问宿主机 Gateway</small>
        </article>
        <article class="gateway-card">
          <span>健康检查</span>
          <strong>{{ tdxGatewayHealthText }}</strong>
          <small>{{ tdxGatewayHealthDetail }}</small>
        </article>
        <article class="gateway-card">
          <span>真实取数探针</span>
          <strong>{{ tdxGatewayProbeText }}</strong>
          <small>{{ tdxGatewayProbeDetail }}</small>
        </article>
        <article class="gateway-card">
          <span>宿主机进程</span>
          <strong>{{ tdxGatewayProcessText }}</strong>
          <small>{{ tdxGatewayTaskText }}</small>
        </article>
      </div>
      <div class="gateway-diagnostics">
        <div class="diagnostic-line">
          <span>最近检查</span>
          <strong>{{ formatDateTime(tdxGatewayDiagnostics.checked_at) }}</strong>
        </div>
        <div class="diagnostic-line">
          <span>后端配置</span>
          <strong>strict={{ tdxGatewayDiagnostics.backend?.strict_startup || '0' }}</strong>
        </div>
        <div v-if="tdxGatewayDiagnostics.health?.error" class="diagnostic-line danger-line">
          <span>健康错误</span>
          <strong>{{ tdxGatewayDiagnostics.health.error }}</strong>
        </div>
        <div v-if="tdxGatewayDiagnostics.market_data_probe?.error" class="diagnostic-line danger-line">
          <span>取数错误</span>
          <strong>{{ tdxGatewayDiagnostics.market_data_probe.error }}</strong>
        </div>
      </div>
      <div class="gateway-check-grid">
        <div
          v-for="check in tdxGatewayChecks"
          :key="check.key"
          class="gateway-check"
          :class="check.ok ? 'ok' : 'blocked'"
        >
          <span>{{ check.label }}</span>
          <strong>{{ check.status || '-' }}</strong>
        </div>
      </div>
      <div v-if="tdxGatewayBlockers.length" class="gateway-blockers">
        <strong>当前阻塞</strong>
        <span v-for="item in tdxGatewayBlockers" :key="item.key">{{ item.message }}</span>
      </div>
      <div v-if="tdxGatewayManualCommands.length" class="gateway-manual">
        <strong>Gateway 完全不可达时的宿主机兜底</strong>
        <div v-for="item in tdxGatewayManualCommands" :key="item.command" class="gateway-command">
          <span>{{ item.label }}</span>
          <code>{{ item.command }}</code>
        </div>
      </div>
    </section>

    <section class="section-card">
      <div class="section-title-row">
        <div>
          <span class="eyebrow">Data Pipeline</span>
          <h2>核心数据链路</h2>
        </div>
        <div class="section-actions">
          <button class="btn subtle" @click="refreshCoreMaintenanceStatus">刷新状态</button>
          <button class="btn subtle" @click="toggleTimelinePanel">
            {{ showTimelinePanel ? '收起时间线' : '查看时间线' }}
          </button>
        </div>
      </div>

      <div v-if="false" class="data-source-panel">
        <div class="data-source-toolbar">
          <div>
            <span class="eyebrow">Source Coverage</span>
            <h3>按日期统计数据源数量</h3>
          </div>
          <div class="data-source-date-actions">
            <input
              v-model="dataSourceCountDate"
              class="date-input"
              type="date"
              @change="refreshDataSourceCounts"
            />
            <button class="btn subtle" :disabled="dataSourceCountsLoading" @click="refreshDataSourceCounts">
              {{ dataSourceCountsLoading ? '统计中...' : '刷新统计' }}
            </button>
          </div>
        </div>
        <div class="data-source-meta">
          <span>统计日期：{{ dataSourceCounts.trade_date || dataSourceCountDate }}</span>
          <span>最新可用：{{ dataSourceCounts.latest_available_date || '-' }}</span>
          <span>缺口项：{{ dataSourceCounts.summary?.incomplete ?? dataSourceCounts.summary?.missing ?? '-' }}</span>
          <span>检查时间：{{ formatDateTime(dataSourceCounts.checked_at) }}</span>
        </div>
        <div class="data-source-table-wrap">
          <table class="data-source-table">
            <thead>
              <tr>
                <th>数据源</th>
                <th>覆盖状态</th>
                <th>标的数量</th>
                <th>数据行数</th>
                <th>完整度</th>
                <th>最新日期</th>
                <th>分钟明细</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!dataSourceCountsLoading && !dataSourceCountRows.length">
                <td colspan="7" class="table-empty">暂无统计数据</td>
              </tr>
              <tr
                v-for="row in dataSourceCountRows"
                :key="row.key"
                :class="getDataSourceRowClass(row)"
              >
                <td>
                  <strong>{{ row.label }}</strong>
                  <small>{{ row.key }}</small>
                </td>
                <td>
                  <span class="status-pill" :class="getDataSourceStatusClass(row)">
                    {{ getDataSourceStatusText(row) }}
                  </span>
                </td>
                <td>{{ formatInteger(row.code_count) }}</td>
                <td>{{ formatInteger(row.row_count) }}</td>
                <td>
                  <strong>{{ formatCoverage(row.coverage_rate) }}</strong>
                  <small v-if="row.expected_row_count">应有 {{ formatInteger(row.expected_row_count) }}</small>
                  <small v-if="row.missing_rows || row.extra_rows">
                    缺 {{ formatInteger(row.missing_rows) }} / 多 {{ formatInteger(row.extra_rows) }}
                  </small>
                </td>
                <td>{{ row.latest_date || '-' }}</td>
                <td>
                  <div v-if="row.periods?.length" class="period-stack">
                    <span
                      v-for="period in row.periods"
                      :key="`${row.key}-${period.period}`"
                      class="period-chip"
                      :class="period.complete ? 'complete' : 'incomplete'"
                    >
                      <strong>{{ period.period }}</strong>
                      <span>{{ formatInteger(period.row_count) }}/{{ formatInteger(period.expected_row_count) }}</span>
                      <em>{{ formatCoverage(period.coverage_rate) }}</em>
                      <small v-if="period.missing_rows || period.extra_rows">
                        缺{{ formatInteger(period.missing_rows) }} 多{{ formatInteger(period.extra_rows) }}
                      </small>
                    </span>
                  </div>
                  <span v-else>-</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="pipeline-grid">
        <article v-for="group in coreTaskGroups" :key="group.key" class="pipeline-card" :class="`accent-${group.accent}`">
          <div class="pipeline-head">
            <div>
              <span class="command-kicker">{{ group.kicker }}</span>
              <h3>{{ group.title }}</h3>
              <p>{{ group.description }}</p>
            </div>
            <span class="status-pill" :class="getGroupStatus(group)">
              {{ getGroupSummary(group) }}
            </span>
          </div>
          <div class="task-stack">
            <div v-for="task in group.tasks" :key="task.key" class="task-chip">
              <div class="task-chip-main">
                <span class="task-dot" :class="`state-bg-${getTaskStatus(task.key)}`"></span>
                <strong>{{ task.label }}</strong>
                <span :class="`state-${getTaskStatus(task.key)}`">{{ getTaskStatusText(task.key) }}</span>
              </div>
              <div class="task-chip-meta">
                <span>成功：{{ getTaskLastSuccess(task.key) }}</span>
                <span>下次：{{ getNextRunTime(task.key) }}</span>
              </div>
              <p>{{ getTaskMessage(task.key) }}</p>
              <button
                class="mini-btn"
                :disabled="isMaintenanceTaskActionDisabled(task.key)"
                @click="syncMaintenanceTask(task)"
              >
                {{ isMaintenanceTaskSyncing(task.key) ? '执行中' : '单项执行' }}
              </button>
            </div>
          </div>
        </article>
      </div>

      <div v-if="showTimelinePanel" class="timeline-panel">
        <div class="timeline-title">最近任务时间线</div>
        <div v-if="!coreMaintenance.timeline?.length" class="timeline-empty">暂无任务记录</div>
        <div v-for="(item, idx) in coreMaintenance.timeline" :key="`${item.task_name}-${item.created_at}-${idx}`" class="timeline-item">
          <span class="timeline-name">{{ getTaskAlias(item.task_name) }}</span>
          <span :class="`state-${item.status || 'idle'}`">{{ getStateText(item.status) }}</span>
          <span class="timeline-msg">{{ item.message || '-' }}</span>
          <span class="timeline-duration">{{ formatDuration(item.duration_sec) }}</span>
          <span class="timeline-time">{{ formatDateTime(item.created_at) }}</span>
        </div>
      </div>
    </section>

    <section class="section-card data-source-section">
      <div class="data-source-toolbar">
        <div>
          <span class="eyebrow">Source Coverage</span>
          <h2>按日期统计数据源数量</h2>
        </div>
        <div class="data-source-date-actions">
          <input
            v-model="dataSourceCountDate"
            class="date-input"
            type="date"
            @change="refreshDataSourceCounts"
          />
          <button class="btn subtle" :disabled="dataSourceCountsLoading" @click="refreshDataSourceCounts">
            {{ dataSourceCountsLoading ? '统计中...' : '刷新统计' }}
          </button>
          <button class="btn primary" :disabled="dataSourceRepairing || !dataSourceCountDate" @click="repairDataSourceDate">
            {{ dataSourceRepairing ? '修复中...' : '修复当前日期' }}
          </button>
        </div>
      </div>
      <div class="data-source-meta">
        <span>统计日期：{{ dataSourceCounts.trade_date || dataSourceCountDate }}</span>
        <span>最新可用：{{ dataSourceCounts.latest_available_date || '-' }}</span>
        <span>缺口项：{{ dataSourceCounts.summary?.incomplete ?? dataSourceCounts.summary?.missing ?? '-' }}</span>
        <span>检查时间：{{ formatDateTime(dataSourceCounts.checked_at) }}</span>
      </div>
      <div class="data-source-table-wrap">
        <table class="data-source-table">
          <thead>
            <tr>
              <th>数据源</th>
              <th>覆盖状态</th>
              <th>标的数量</th>
              <th>数据行数</th>
              <th>完整度</th>
              <th>最新日期</th>
              <th>分钟明细</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!dataSourceCountsLoading && !dataSourceCountRows.length">
              <td colspan="7" class="table-empty">暂无统计数据</td>
            </tr>
            <tr
              v-for="row in dataSourceCountRows"
              :key="row.key"
              :class="getDataSourceRowClass(row)"
            >
              <td>
                <strong>{{ row.label }}</strong>
                <small>{{ row.key }}</small>
              </td>
              <td>
                <span class="status-pill" :class="getDataSourceStatusClass(row)">
                  {{ getDataSourceStatusText(row) }}
                </span>
              </td>
              <td>{{ formatInteger(row.code_count) }}</td>
              <td>{{ formatInteger(row.row_count) }}</td>
              <td>
                <strong>{{ formatCoverage(row.coverage_rate) }}</strong>
                <small v-if="row.expected_row_count">应有 {{ formatInteger(row.expected_row_count) }}</small>
                <small v-if="row.missing_rows || row.extra_rows">
                  缺 {{ formatInteger(row.missing_rows) }} / 多 {{ formatInteger(row.extra_rows) }}
                </small>
              </td>
              <td>{{ row.latest_date || '-' }}</td>
              <td>
                <div v-if="row.periods?.length" class="period-stack">
                  <span
                    v-for="period in row.periods"
                    :key="`${row.key}-${period.period}`"
                    class="period-chip"
                    :class="period.complete ? 'complete' : 'incomplete'"
                  >
                    <strong>{{ period.period }}</strong>
                    <span>{{ formatInteger(period.row_count) }}/{{ formatInteger(period.expected_row_count) }}</span>
                    <em>{{ formatCoverage(period.coverage_rate) }}</em>
                    <small v-if="period.missing_rows || period.extra_rows">
                      缺 {{ formatInteger(period.missing_rows) }} 多 {{ formatInteger(period.extra_rows) }}
                    </small>
                  </span>
                </div>
                <span v-else>-</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="section-card">
      <div class="section-title-row">
        <div>
          <span class="eyebrow">Strategy Ops</span>
          <h2>策略维护</h2>
        </div>
        <span class="status-pill" :class="strategyMaintenanceRunning ? 'enabled' : 'paused'">
          {{ strategyMaintenanceRunning ? '执行中' : '待命' }}
        </span>
      </div>

      <div class="maintenance-actions">
        <button
          class="btn success"
          :disabled="strategyMaintenanceRunning"
          @click="runAllStrategyMaintenanceTasks('daily')"
        >
          {{ runningAllStrategyMaintenanceNow ? 'G3日常维护执行中...' : '一键执行G3日常维护' }}
        </button>
        <button
          class="btn danger soft"
          :disabled="strategyMaintenanceRunning"
          @click="runAllStrategyMaintenanceTasks('heavy')"
        >
          一键执行证据复核
        </button>
        <button class="btn" @click="refreshStrategyMaintenanceStatus">刷新G3状态</button>
      </div>

      <div class="strategy-grid">
        <article
          v-for="task in strategyMaintenanceTasks"
          :key="task.key"
          class="strategy-card"
          :class="{ 'strategy-card-heavy': task.mode === 'heavy' }"
        >
          <div class="strategy-title-row">
            <h3>{{ task.label }}</h3>
            <div class="strategy-title-badges">
              <span v-if="task.mode === 'heavy'" class="task-kind heavy">重任务</span>
              <span v-else class="task-kind daily">日常</span>
              <span :class="`state-${getStrategyTaskStatus(task.key)}`">{{ getStrategyTaskStatusText(task.key) }}</span>
            </div>
          </div>
          <p>{{ getStrategyTaskMessage(task) }}</p>
          <div class="strategy-meta">
            <span>上次成功</span>
            <strong>{{ formatDateTime(getStrategyTaskLastSuccess(task.key)) }}</strong>
          </div>
          <button
            class="mini-btn"
            :disabled="isStrategyTaskActionDisabled(task.key)"
            @click="runStrategyMaintenanceTask(task)"
          >
            {{ isStrategyTaskSyncing(task.key) ? '执行中' : (task.mode === 'heavy' ? '重型执行' : '执行一次') }}
          </button>
        </article>
      </div>
    </section>

    <section class="section-card">
      <div class="section-title-row">
        <div>
          <span class="eyebrow">Startup Policy</span>
          <h2>启动初始化</h2>
        </div>
        <span class="status-pill" :class="startupReferenceSyncEnabled ? 'enabled' : 'paused'">
          {{ startupReferenceSyncEnabled ? '启动时开启' : '启动时关闭' }}
        </span>
      </div>
      <p class="section-tip">
        控制项目启动时是否自动更新交易日历、股票列表、指数列表、板块列表和板块成分股。
        调试阶段建议关闭，线上运行建议开启。修改后需重启后端生效。
      </p>
      <div class="maintenance-actions">
        <button class="btn primary" :disabled="startupReferenceSyncSaving" @click="toggleStartupReferenceSync">
          {{ startupReferenceSyncSaving ? '保存中...' : (startupReferenceSyncEnabled ? '关闭启动初始化' : '开启启动初始化') }}
        </button>
        <button class="btn" :disabled="startupReferenceSyncSaving" @click="loadStartupReferenceSyncSetting">刷新状态</button>
      </div>
      <div class="config-note">
        当前默认：{{ startupReferenceSyncDefaultEnabled ? '线上模式默认开启' : '调试模式默认关闭' }}
      </div>
    </section>

    <section class="section-card">
      <div class="section-title-row">
        <div>
          <span class="eyebrow">Fallback Tools</span>
          <h2>高级维护</h2>
        </div>
        <button class="btn ghost" @click="advancedOpen = !advancedOpen">
          {{ advancedOpen ? '折叠' : '展开' }}
        </button>
      </div>
      <p class="section-tip">低频维护、重算、初始化任务放在这里。自动维护异常时再使用手工触发；“立即同步核心数据”已降级为参考数据入口，避免和今日全市场闭环重复。</p>

      <div v-show="advancedOpen" class="advanced-grid">
        <article class="manual-card auto-repair-card">
          <div class="auto-repair-head">
            <div>
              <span class="command-kicker">Auto Repair</span>
              <h3>自动修复控制台</h3>
              <p>按数据和策略两大类执行维护；数据修复可选择时间范围和 K 线周期，策略修复可选择日常或证据复核批次。</p>
            </div>
            <span class="status-pill" :class="autoRepairRunning ? 'enabled' : 'paused'">
              {{ autoRepairRunning ? '执行中' : '待命' }}
            </span>
          </div>

          <div class="auto-repair-layout">
            <div class="segmented-control">
              <button
                v-for="item in autoRepairCategories"
                :key="item.value"
                type="button"
                :class="{ active: autoRepairForm.category === item.value }"
                @click="autoRepairForm.category = item.value"
              >
                {{ item.label }}
              </button>
            </div>

            <div v-if="autoRepairForm.category === 'data'" class="auto-repair-fields">
              <label>
                <span>修复对象</span>
                <select v-model="autoRepairForm.dataScope">
                  <option v-for="item in autoRepairDataScopes" :key="item.value" :value="item.value">
                    {{ item.label }}
                  </option>
                </select>
              </label>
              <label>
                <span>时间范围</span>
                <select v-model.number="autoRepairForm.days">
                  <option v-for="item in autoRepairDayOptions" :key="item.value" :value="item.value">
                    {{ item.label }}
                  </option>
                </select>
              </label>
              <div class="period-box auto-period-box">
                <span>修复周期</span>
                <label v-for="period in autoRepairPeriodOptions" :key="`auto-${period.value}`">
                  <input type="checkbox" v-model="autoRepairForm.periods" :value="period.value" />
                  {{ period.label }}
                </label>
              </div>
            </div>

            <div v-else class="auto-repair-fields">
              <label>
                <span>策略批次</span>
                <select v-model="autoRepairForm.strategyMode">
                  <option value="daily">G3 日常维护</option>
                  <option value="heavy">G3 证据复核</option>
                  <option value="all">全部策略维护</option>
                </select>
              </label>
              <div class="auto-repair-preview">
                {{ autoRepairStrategyPreview }}
              </div>
            </div>

            <button class="btn primary auto-repair-submit" :disabled="autoRepairRunning || !canRunAutoRepair" @click="runAutoRepair">
              {{ autoRepairRunning ? '自动修复执行中...' : '执行自动修复' }}
            </button>
          </div>
        </article>

        <article class="manual-card spotlight">
          <h3>基础参考数据</h3>
          <p>只同步交易日历、股票/指数/板块基础列表和当日日线。日常请优先使用顶部“一键修复今日市场数据”。</p>
          <div class="tag-row">
            <span class="tag">参考数据</span>
            <span class="tag">轻量同步</span>
          </div>
          <button
            class="btn success"
            :disabled="runningCoreAssetsSyncNow"
            @click="syncCoreAssetsNow"
          >
            {{ runningCoreAssetsSyncNow ? '基础同步中...' : '只同步基础参考数据' }}
          </button>
        </article>

        <article class="manual-card">
          <h3>股票数据</h3>
          <p>股票列表、日线修复、全历史修复、盘中股票快照手动触发。</p>
          <div class="tag-row">
            <span class="tag">初始化</span>
            <span class="tag">补数</span>
            <span class="tag">重算</span>
          </div>
          <div class="button-row">
            <button class="btn" :disabled="loading" @click="updateStockList">{{ loading ? '执行中...' : '更新股票列表' }}</button>
            <button class="btn" :disabled="repairingDailyKlines" @click="repairDailyKlinesData">{{ repairingDailyKlines ? '执行中...' : '修复日线' }}</button>
            <button class="btn danger" :disabled="repairingAllHistoryKlines" @click="repairAllHistoryKlines">{{ repairingAllHistoryKlines ? '执行中...' : '修复全历史K线' }}</button>
          </div>
          <div class="period-box">
            <span>盘中股票快照周期：</span>
            <label v-for="period in klinePeriods" :key="`stock-${period.value}`">
              <input type="checkbox" v-model="selectedStockUpdatePeriods" :value="period.value" />
              {{ period.label }}
            </label>
          </div>
          <button class="btn success" :disabled="updatingStockTodayData || selectedStockUpdatePeriods.length === 0" @click="updateStockTodayData">
            {{ updatingStockTodayData ? '执行中...' : '手动更新当天股票数据' }}
          </button>
        </article>

        <article class="manual-card">
          <h3>指数与日历</h3>
          <p>指数列表、历史K线、交易日历和盘中指数快照。</p>
          <div class="tag-row">
            <span class="tag">初始化</span>
            <span class="tag">补数</span>
          </div>
          <div class="button-row">
            <button class="btn" :disabled="updatingIndices" @click="updateIndices">{{ updatingIndices ? '执行中...' : '更新指数列表' }}</button>
            <button class="btn" :disabled="updatingTradeCalendar" @click="updateTradeCalendar">{{ updatingTradeCalendar ? '执行中...' : '更新交易日历' }}</button>
          </div>
          <div class="period-box">
            <span>指数历史K线周期：</span>
            <label v-for="period in klinePeriods" :key="`history-${period.value}`">
              <input type="checkbox" v-model="selectedPeriods" :value="period.value" />
              {{ period.label }}
            </label>
          </div>
          <button class="btn" :disabled="repairingHistoryKlines || selectedPeriods.length === 0" @click="repairHistoryKlines">
            {{ repairingHistoryKlines ? '执行中...' : '同步指数全量历史K线' }}
          </button>
          <div class="period-box">
            <span>盘中指数快照周期：</span>
            <label v-for="period in klinePeriods" :key="`index-${period.value}`">
              <input type="checkbox" v-model="selectedUpdatePeriods" :value="period.value" />
              {{ period.label }}
            </label>
          </div>
          <button class="btn success" :disabled="updatingTodayData || selectedUpdatePeriods.length === 0" @click="updateTodayData">
            {{ updatingTodayData ? '执行中...' : '手动更新当天指数数据' }}
          </button>
        </article>

        <article class="manual-card">
          <h3>板块数据</h3>
          <p>一级、二级、三级板块列表与板块历史统计补齐。</p>
          <div class="tag-row">
            <span class="tag">参考数据</span>
            <span class="tag">补数</span>
          </div>
          <div class="button-row">
            <button class="btn" :disabled="syncingSectors" @click="syncSectors">{{ syncingSectors ? '执行中...' : '同步一级二级三级板块' }}</button>
            <button class="btn success" :disabled="updatingSectorIntradayStats" @click="updateSectorIntradayStats">{{ updatingSectorIntradayStats ? '执行中...' : '刷新板块当日统计' }}</button>
          </div>
          <div class="days-box">
            <label>板块历史同步天数</label>
            <input type="number" v-model.number="syncHistoryDays" min="1" max="365" />
          </div>
          <button class="btn" :disabled="syncingSectorHistory" @click="syncSectorHistory">{{ syncingSectorHistory ? '执行中...' : '同步板块历史涨跌' }}</button>
        </article>

        <article class="manual-card">
          <h3>情绪周期</h3>
          <p>情绪周期重算与盘中自动修复开关。</p>
          <div class="tag-row">
            <span class="tag">重算</span>
            <span class="tag">盘中修复</span>
          </div>
          <div class="button-row">
            <button class="btn" :disabled="repairingEmotion30d" @click="repairEmotion30d">{{ repairingEmotion30d ? '执行中...' : '修复近30天情绪' }}</button>
            <button class="btn" :disabled="repairingEmotionLatest" @click="repairEmotionLatest">{{ repairingEmotionLatest ? '执行中...' : '更新最新情绪' }}</button>
          </div>
          <button class="btn" :class="emotionAutoEnabled ? 'warning' : 'success'" @click="toggleEmotionAuto5m">
            {{ emotionAutoEnabled ? '关闭盘中每5分钟自动修复' : '开启盘中每5分钟自动修复' }}
          </button>
        </article>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import axios from 'axios'
import { ElMessage } from 'element-plus'

const API_BASE = import.meta.env.VITE_API_BASE || '/api'

const loading = ref(false)
const updatingIndices = ref(false)
const syncingSectors = ref(false)
const syncingSectorHistory = ref(false)
const updatingSectorIntradayStats = ref(false)
const repairingHistoryKlines = ref(false)
const repairingDailyKlines = ref(false)
const repairingAllHistoryKlines = ref(false)
const updatingTodayData = ref(false)
const updatingStockTodayData = ref(false)
const updatingTradeCalendar = ref(false)
const repairingEmotion30d = ref(false)
const repairingEmotionLatest = ref(false)
const emotionAutoEnabled = ref(false)
const runningCoreAssetsSyncNow = ref(false)
const runningTodayFullMarketRefreshNow = ref(false)
const runningMarketMinuteHistoryRepairNow = ref(false)
const autoRepairRunning = ref(false)
const strategyTaskStates = ref({})
const strategyTaskSyncing = ref({})
const strategyTaskPollTimers = ref({})
const runningAllStrategyMaintenanceNow = ref(false)
const STRATEGY_SUCCESS_STORAGE_KEY = 'aistock.strategyMaintenance.lastSuccess.v1'

const advancedOpen = ref(false)
const showTimelinePanel = ref(false)
const lastStatusRefreshAt = ref(null)
const coreMaintenanceRefreshTimer = ref(null)
const dataSourceCountsLoading = ref(false)
const dataSourceRepairing = ref(false)
const dataSourceCountDate = ref('')
const dataSourceCounts = ref({
  trade_date: '',
  latest_available_date: null,
  checked_at: null,
  summary: {},
  rows: []
})
const startupReferenceSyncEnabled = ref(false)
const startupReferenceSyncDefaultEnabled = ref(false)
const startupReferenceSyncSaving = ref(false)
const tdxGatewayLoading = ref(false)
const tdxGatewayInitializing = ref(false)
const tdxGatewayRestarting = ref(false)
const tdxGatewayProbing = ref(false)
const tdxGatewayRecovering = ref(false)
const tdxGatewayDiagnostics = ref({})
const maintenanceTaskSyncing = ref({})
const syncHistoryDays = ref(30)
const klinePeriods = ref([
  { value: '1m', label: '1分钟' },
  { value: '5m', label: '5分钟' },
  { value: '15m', label: '15分钟' },
  { value: '30m', label: '30分钟' },
  { value: '60m', label: '60分钟' },
  { value: '1d', label: '日线' },
  { value: '1w', label: '周线' },
  { value: '1mon', label: '月线' },
  { value: '1q', label: '季线' },
  { value: '1y', label: '年线' }
])
const selectedPeriods = ref(['1d', '1w', '1mon'])
const selectedUpdatePeriods = ref(['5m', '15m', '30m', '60m', '1d'])
const selectedStockUpdatePeriods = ref(['5m', '15m', '30m', '60m', '1d'])
const autoRepairForm = ref({
  category: 'data',
  dataScope: 'minute_history',
  days: 30,
  periods: ['5m', '15m', '30m', '60m'],
  strategyMode: 'daily'
})
const autoRepairCategories = [
  { value: 'data', label: '数据修复' },
  { value: 'strategy', label: '策略修复' }
]
const autoRepairDataScopes = [
  { value: 'minute_history', label: '股票/指数历史分钟线' },
  { value: 'index_history', label: '指数历史K线' },
  { value: 'daily_stock', label: '股票日线修复' },
  { value: 'all_stock_history', label: '股票全历史K线' },
  { value: 'emotion_cycle', label: '情绪周期重算' }
]
const autoRepairDayOptions = [
  { value: 2, label: '近2个交易日' },
  { value: 5, label: '近5个交易日' },
  { value: 10, label: '近10个交易日' },
  { value: 30, label: '近30个交易日' },
  { value: 60, label: '近60个交易日' },
  { value: 120, label: '近120个交易日' }
]
const autoRepairPeriodOptions = [
  { value: '5m', label: '5分钟' },
  { value: '15m', label: '15分钟' },
  { value: '30m', label: '30分钟' },
  { value: '60m', label: '60分钟' },
  { value: '1d', label: '日线' },
  { value: '1w', label: '周线' },
  { value: '1mon', label: '月线' }
]

const maintenanceTasks = [
  { key: 'trade_calendar', label: '交易日历同步' },
  { key: 'stock_list_sync', label: '股票列表同步' },
  { key: 'index_list_sync', label: '指数列表同步' },
  { key: 'sector_list_sync', label: '板块列表同步' },
  { key: 'stock_intraday', label: '盘中股票/指数日线快照' },
  { key: 'market_intraday_minutes', label: '盘中股票/指数分钟级快照' },
  { key: 'market_intraday_kline_refresh', label: '当天15/30m分钟K线落库' },
  { key: 'minute_kline_daily_repair_validate', label: '最近2日分钟K线补全验证' },
  { key: 'sector_intraday_stats_refresh', label: '板块当日统计刷新' },
  { key: 'official_daily', label: '盘后正式日线写库' },
  { key: 'repair_daily', label: '次日自动巡检修复' }
]

const taskByKey = Object.fromEntries(maintenanceTasks.map((task) => [task.key, task]))

const coreTaskGroups = [
  {
    key: 'reference',
    kicker: 'Reference',
    title: '基础参考数据',
    description: '交易日历、股票/指数列表、板块列表与成分，是所有同步链路的底座。',
    accent: 'blue',
    tasks: ['trade_calendar', 'stock_list_sync', 'index_list_sync', 'sector_list_sync'].map((key) => taskByKey[key])
  },
  {
    key: 'intraday',
    kicker: 'Today Market',
    title: '当天市场数据',
    description: '日线快照、分钟快照、15/30m分钟K线和板块当日统计，支撑盘中页面与策略。',
    accent: 'green',
    tasks: ['stock_intraday', 'market_intraday_minutes', 'market_intraday_kline_refresh', 'sector_intraday_stats_refresh'].map((key) => taskByKey[key])
  },
  {
    key: 'after_close',
    kicker: 'After Close',
    title: '盘后复核',
    description: '盘后正式日线写库、分钟K线完整性验证和次日巡检修复，负责把数据闭环收紧。',
    accent: 'amber',
    tasks: ['official_daily', 'minute_kline_daily_repair_validate', 'repair_daily'].map((key) => taskByKey[key])
  }
]

const maintenanceTaskNameMap = {
  trade_calendar: 'update_trade_calendar',
  stock_list_sync: 'update_stock_list',
  index_list_sync: 'update_index_list',
  sector_list_sync: 'sync_sectors',
  stock_intraday: 'update_stock_today_data',
  market_intraday_minutes: 'update_market_today_minute_data',
  market_intraday_kline_refresh: 'sync_today_intraday_kline',
  minute_kline_daily_repair_validate: 'minute_kline_daily_repair_validate',
  sector_intraday_stats_refresh: 'update_sector_intraday_stats',
  official_daily: 'official_daily_close_sync',
  repair_daily: 'repair_previous_daily_kline'
}

const strategyMaintenanceTasks = [
  {
    key: 'g3_state_alpha_refresh',
    label: 'G3 State Alpha刷新',
    mode: 'daily',
    description: '刷新第三代市场状态Alpha影子工作流，生成当日候选、影子票据和台账。'
  },
  {
    key: 'g3_shadow_monitor',
    label: 'G3影子信号监控',
    mode: 'daily',
    description: '手动运行G3影子监控与通知链路，检查阻断、心跳和候选变化。'
  },
  {
    key: 'g3_workflow_status',
    label: 'G3信号链路自检',
    mode: 'daily',
    description: '检查G3运行链路、候选池、30m确认、风控合同和正式交易闸门。'
  },
  {
    key: 'g3_current_shadow',
    label: 'G3今日影子票据',
    mode: 'daily',
    description: '读取G3今日影子买入票据、候选路由和影子台账可用性。'
  },
  {
    key: 'g3_historical_trades',
    label: 'G3历史成交复核',
    mode: 'heavy',
    description: '复核G3 State Alpha历史成交、权益曲线、路线收益和未来函数审计。'
  },
  {
    key: 'g3_evidence_inventory',
    label: 'G3证据归档复核',
    mode: 'heavy',
    description: '检查G3研究证据归档、蓝图报告和清理清单是否仍可读取。'
  },
  {
    key: 'g3_monitor_status',
    label: 'G3调度监控',
    mode: 'daily',
    description: '检查G3 shadow monitor调度器、邮件、心跳和下一次运行时间。'
  },
  {
    key: 'g3_current_readiness',
    label: 'G3当前准入检查',
    mode: 'daily',
    description: '读取G3当前工作台数据，确认shadow-only、observe-only和正式交易锁。'
  }
]

const coreMaintenance = ref({
  enabled: false,
  overall_status: 'idle',
  today_success_rate: 100,
  latest_failed_task: null,
  running_task: null,
  jobs: {},
  task_status: {},
  task_status_normalized: {},
  timeline: []
})

const dataSourceCountRows = computed(() => dataSourceCounts.value?.rows || [])

const strategyMaintenanceRunning = computed(() => {
  const hasLocalTaskRunning = Object.values(strategyTaskSyncing.value || {}).some(Boolean)
  const hasBackendTaskRunning = Object.values(strategyTaskStates.value || {}).some((state) => {
    return normalizeStrategyStatus(state?.status) === 'running'
  })
  return runningAllStrategyMaintenanceNow.value || hasLocalTaskRunning || hasBackendTaskRunning
})

const autoRepairSelectedStrategyTasks = computed(() => {
  const mode = autoRepairForm.value.strategyMode
  if (mode === 'all') return strategyMaintenanceTasks
  return strategyMaintenanceTasks.filter((task) => (task.mode || 'daily') === mode)
})

const autoRepairStrategyPreview = computed(() => {
  const tasks = autoRepairSelectedStrategyTasks.value
  if (!tasks.length) return '暂无可执行策略维护任务'
  return tasks.map((task) => task.label).join(' / ')
})

const canRunAutoRepair = computed(() => {
  if (autoRepairForm.value.category === 'strategy') return autoRepairSelectedStrategyTasks.value.length > 0
  const scope = autoRepairForm.value.dataScope
  if (scope === 'daily_stock' || scope === 'all_stock_history') return true
  return (autoRepairForm.value.periods || []).length > 0
})

const normalizeCoreMaintenance = (payload = {}) => {
  return {
    enabled: !!payload.enabled || !!payload.is_enabled,
    overall_status: payload.overall_status || 'idle',
    today_success_rate: payload.today_success_rate ?? 100,
    latest_failed_task: payload.latest_failed_task || null,
    latest_failed_at: payload.latest_failed_at || null,
    running_task: payload.running_task || null,
    jobs: payload.jobs || {},
    task_status: payload.task_status || {},
    task_status_normalized: payload.task_status_normalized || {},
    timeline: payload.timeline || []
  }
}

const formatDateTime = (value) => {
  if (!value) return '-'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return String(value)
  return d.toLocaleString('zh-CN', { hour12: false })
}

const formatRate = (value) => `${Number(value || 0).toFixed(2)}%`
const formatInteger = (value) => {
  const n = Number(value)
  if (!Number.isFinite(n)) return '-'
  return n.toLocaleString('zh-CN')
}

const formatCoverage = (value) => {
  const n = Number(value)
  if (!Number.isFinite(n)) return '-'
  return `${(n * 100).toFixed(2)}%`
}

const getDataSourceStatusText = (row) => {
  if (!row?.ok) return '缺失'
  if (row.complete) return '完整'
  return '有缺口'
}

const getDataSourceStatusClass = (row) => {
  if (!row?.ok) return 'failed'
  if (row.complete) return 'enabled'
  return 'paused'
}

const getDataSourceRowClass = (row) => ({
  'data-source-row-missing': !row?.ok,
  'data-source-row-incomplete': row?.ok && !row?.complete
})

const formatDuration = (valueSec) => {
  const sec = Number(valueSec)
  if (!Number.isFinite(sec) || sec < 0) return '-'
  if (sec < 1) return `${Math.round(sec * 1000)}ms`
  if (sec < 60) return `${sec.toFixed(2)}s`
  const min = Math.floor(sec / 60)
  const remain = sec - min * 60
  return `${min}m ${remain.toFixed(1)}s`
}

const unwrapGatewayBody = (section) => {
  const body = section?.body
  return body?.data || body || {}
}

const tdxGatewayHealthPayload = computed(() => unwrapGatewayBody(tdxGatewayDiagnostics.value.health))
const tdxGatewayHostPayload = computed(() => unwrapGatewayBody(tdxGatewayDiagnostics.value.host_diagnostics))
const tdxGatewayProcessPayload = computed(() => tdxGatewayHostPayload.value?.process?.data || tdxGatewayHostPayload.value?.process || {})
const tdxGatewayProbePayload = computed(() => tdxGatewayDiagnostics.value.market_data_probe || tdxGatewayHostPayload.value?.market_data_probe || {})
const tdxGatewayVerdict = computed(() => tdxGatewayDiagnostics.value?.verdict || {})
const tdxGatewayChecks = computed(() => tdxGatewayVerdict.value?.checks || [])
const tdxGatewayBlockers = computed(() => tdxGatewayVerdict.value?.blockers || [])
const tdxGatewayManualCommands = computed(() => tdxGatewayVerdict.value?.manual_commands || [])
const tdxGatewayRecoveryOrderText = computed(() => {
  const order = tdxGatewayVerdict.value?.recovery_order || []
  return order.length ? order.join(' / ') : '刷新诊断 / 真实取数探针 / 重新初始化 / 必要时重启 Gateway'
})

const tdxGatewayStatusClass = computed(() => {
  if (tdxGatewayLoading.value || tdxGatewayInitializing.value || tdxGatewayRestarting.value || tdxGatewayProbing.value || tdxGatewayRecovering.value) return 'running'
  if (tdxGatewayVerdict.value?.ready) return 'enabled'
  if (tdxGatewayVerdict.value?.level === 'error') return 'failed'
  if (tdxGatewayProbePayload.value?.probe_ok || tdxGatewayProbePayload.value?.ok) return 'enabled'
  return 'paused'
})

const tdxGatewayStatusText = computed(() => {
  if (tdxGatewayRecovering.value) return '恢复中'
  if (tdxGatewayProbing.value) return '探针中'
  if (tdxGatewayRestarting.value) return '重启中'
  if (tdxGatewayInitializing.value) return '初始化中'
  if (tdxGatewayLoading.value) return '诊断中'
  if (tdxGatewayStatusClass.value === 'enabled') return '可用'
  if (tdxGatewayStatusClass.value === 'failed') return '不可达'
  return '待排查'
})

const tdxGatewayHealthText = computed(() => {
  const health = tdxGatewayHealthPayload.value
  if (health.ready || health.status === 'available') return 'available'
  return health.status || 'unknown'
})

const tdxGatewayHealthDetail = computed(() => {
  const health = tdxGatewayHealthPayload.value
  return health.last_error || `last=${formatDateTime(health.last_activity)}`
})

const tdxGatewayProbeText = computed(() => {
  const probe = tdxGatewayProbePayload.value
  if (probe.probe_ok || probe.ok) return '取数通过'
  return '未通过'
})

const tdxGatewayProbeDetail = computed(() => {
  const probe = tdxGatewayProbePayload.value
  const fields = probe.fields || []
  if (fields.length) return fields.join(', ')
  return probe.error || probe.body?.detail || '-'
})

const tdxGatewayProcessText = computed(() => {
  const proc = tdxGatewayProcessPayload.value || {}
  const pid = proc.pid || proc.data?.pid
  const memory = proc.privateMemoryMb || proc.data?.privateMemoryMb
  return pid ? `PID ${pid}${memory ? ` / ${memory}MB` : ''}` : '-'
})

const tdxGatewayTaskText = computed(() => {
  const proc = tdxGatewayProcessPayload.value || {}
  return `task=${proc.taskState || '-'} result=${proc.lastTaskResult ?? '-'}`
})

const getTaskState = (taskKey) => {
  return coreMaintenance.value.task_status_normalized?.[taskKey] || {}
}

const getTaskRaw = (taskKey) => {
  return coreMaintenance.value.task_status?.[taskKey] || {}
}

const getTaskStatus = (taskKey) => {
  const status = getTaskState(taskKey).status
  if (status) return status
  const raw = getTaskRaw(taskKey)
  if (raw?.is_running) return 'running'
  if (raw?.error) return 'failed'
  if (raw?.results) return 'success'
  const scheduled = coreMaintenance.value.jobs?.[taskKey]?.enabled
  return scheduled ? 'idle' : 'paused'
}

const getTaskStatusText = (taskKey) => getStateText(getTaskStatus(taskKey))

const getTaskMessage = (taskKey) => {
  const state = getTaskState(taskKey)
  if (state?.display_message) return state.display_message
  const task = getTaskRaw(taskKey)
  if (task?.progress?.message) return task.progress.message
  if (task?.error) return task.error
  if (task?.results?.message) return task.results.message
  return coreMaintenance.value.jobs?.[taskKey]?.enabled ? '已调度，等待执行' : '未调度'
}

const getTaskLastSuccess = (taskKey) => {
  const val = getTaskState(taskKey).last_success_at || getTaskRaw(taskKey).last_success_at
  return formatDateTime(val)
}

const getTaskLastDuration = (taskKey) => {
  const value = getTaskState(taskKey).last_duration_sec
  return formatDuration(value)
}

const getTaskAvgDuration = (taskKey) => {
  const state = getTaskState(taskKey)
  const avg = state.recent_avg_duration_sec
  const count = Number(state.recent_run_count || 0)
  if (avg === null || avg === undefined || count <= 0) return '-'
  return `${formatDuration(avg)} (${count}次)`
}

const getTriggerSourceText = (source) => {
  const map = {
    auto: '自动定时',
    manual: '手动触发',
    startup: '启动快刷'
  }
  return map[source] || '-'
}

const getTaskTriggerSource = (taskKey) => {
  const source = getTaskState(taskKey).trigger_source || getTaskRaw(taskKey).trigger_source
  return getTriggerSourceText(source)
}

const getNextRunTime = (taskKey) => formatDateTime(coreMaintenance.value.jobs?.[taskKey]?.next_run_time)

const isTaskRunning = (taskKey) => !!getTaskRaw(taskKey)?.is_running
const isMaintenanceTaskSyncing = (taskKey) => !!maintenanceTaskSyncing.value[taskKey]
const isMaintenanceTaskActionDisabled = (taskKey) => isTaskRunning(taskKey) || isMaintenanceTaskSyncing(taskKey)

const getGroupStatus = (group) => {
  const statuses = (group.tasks || []).map((task) => getTaskStatus(task.key))
  if (statuses.includes('running')) return 'enabled'
  if (statuses.includes('failed')) return 'failed'
  if (statuses.every((status) => status === 'success')) return 'enabled'
  return 'paused'
}

const getGroupSummary = (group) => {
  const tasks = group.tasks || []
  const running = tasks.filter((task) => getTaskStatus(task.key) === 'running').length
  if (running > 0) return `${running} 项运行中`
  const failed = tasks.filter((task) => getTaskStatus(task.key) === 'failed').length
  if (failed > 0) return `${failed} 项失败`
  const success = tasks.filter((task) => getTaskStatus(task.key) === 'success').length
  return `${success}/${tasks.length} 成功`
}

const loadStrategySuccessMemory = () => {
  try {
    const raw = localStorage.getItem(STRATEGY_SUCCESS_STORAGE_KEY)
    const data = raw ? JSON.parse(raw) : {}
    Object.entries(data || {}).forEach(([taskKey, lastSuccessAt]) => {
      if (lastSuccessAt) {
        setStrategyTaskState(taskKey, { last_success_at: lastSuccessAt })
      }
    })
  } catch (error) {
    // Ignore corrupted local UI cache; fresh status refresh will repopulate it.
  }
}

const saveStrategySuccessMemory = (taskKey, lastSuccessAt) => {
  if (!taskKey || !lastSuccessAt) return
  try {
    const raw = localStorage.getItem(STRATEGY_SUCCESS_STORAGE_KEY)
    const data = raw ? JSON.parse(raw) : {}
    data[taskKey] = lastSuccessAt
    localStorage.setItem(STRATEGY_SUCCESS_STORAGE_KEY, JSON.stringify(data))
  } catch (error) {
    // Local persistence is only a UI convenience; do not block task execution.
  }
}

const setStrategyTaskState = (taskKey, patch) => {
  const previousState = strategyTaskStates.value[taskKey] || {}
  const nextState = {
    ...previousState,
    ...patch
  }
  const rememberedSuccessAt =
    nextState.last_success_at ||
    previousState.last_success_at ||
    (normalizeStrategyStatus(nextState.status) === 'success' ? (nextState.updated_at || new Date().toISOString()) : null)
  if (rememberedSuccessAt) {
    nextState.last_success_at = rememberedSuccessAt
  }
  strategyTaskStates.value = {
    ...strategyTaskStates.value,
    [taskKey]: nextState
  }
  if (rememberedSuccessAt) {
    saveStrategySuccessMemory(taskKey, rememberedSuccessAt)
  }
}

const setStrategyTaskSyncing = (taskKey, syncing) => {
  strategyTaskSyncing.value = {
    ...strategyTaskSyncing.value,
    [taskKey]: syncing
  }
}

const normalizeStrategyStatus = (status, fallback = 'idle') => {
  const value = String(status || '').toLowerCase()
  if (['running', 'queued', 'pending'].includes(value)) return 'running'
  if (['completed', 'success', 'ok', 'passed'].includes(value)) return 'success'
  if (['failed', 'error', 'missing'].includes(value)) return 'failed'
  if (value === 'paused' || value === 'disabled') return 'paused'
  return fallback
}

const strategyTaskMessageFromResult = (payload) => {
  if (!payload || typeof payload !== 'object') return ''
  if (payload.message) return String(payload.message)
  if (payload.error) return String(payload.error)
  if (payload.reason) return String(payload.reason)
  if (payload.last_error) return String(payload.last_error)
  if (payload.last_result?.reason) return String(payload.last_result.reason)
  if (payload.last_result?.message) return String(payload.last_result.message)
  return ''
}

const getStrategyTaskStatus = (taskKey) => {
  if (isStrategyTaskSyncing(taskKey)) return 'running'
  return normalizeStrategyStatus(strategyTaskStates.value[taskKey]?.status, 'idle')
}

const getStrategyTaskStatusText = (taskKey) => getStateText(getStrategyTaskStatus(taskKey))

const getStrategyTaskMessage = (task) => {
  const state = strategyTaskStates.value[task.key] || {}
  return state.message || task.description || '-'
}

const getStrategyTaskLastSuccess = (taskKey) => {
  const state = strategyTaskStates.value[taskKey] || {}
  return state.last_success_at
}

const isStrategyTaskSyncing = (taskKey) => !!strategyTaskSyncing.value[taskKey]
const isStrategyTaskActionDisabled = (taskKey) => strategyMaintenanceRunning.value || isStrategyTaskSyncing(taskKey)

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

const clearStrategyTaskPollTimer = (taskKey) => {
  const timer = strategyTaskPollTimers.value[taskKey]
  if (timer) clearTimeout(timer)
  strategyTaskPollTimers.value = {
    ...strategyTaskPollTimers.value,
    [taskKey]: null
  }
}

const pollStrategyAsyncTask = async (task, taskId, statusUrl, options = {}) => {
  const { scheduleNext = true, showToast = true } = options
  clearStrategyTaskPollTimer(task.key)
  try {
    const response = await axios.get(`${API_BASE}${statusUrl}`)
    const payload = response.data || {}
    const rawStatus = payload.status || payload.task?.status
    const status = normalizeStrategyStatus(rawStatus)
    const statePatch = {
      status,
      task_id: taskId,
      progress: payload.progress ?? payload.task?.progress,
      message: strategyTaskMessageFromResult(payload) || `${task.label} ${status === 'running' ? '执行中' : '已更新'}`,
      updated_at: payload.updated_at || payload.finished_at || payload.task?.updated_at || new Date().toISOString(),
      raw: payload
    }
    if (status === 'success') {
      statePatch.last_success_at = payload.finished_at || payload.updated_at || payload.task?.updated_at || new Date().toISOString()
    }
    setStrategyTaskState(task.key, statePatch)
    if (status === 'running') {
      if (scheduleNext) {
        strategyTaskPollTimers.value = {
          ...strategyTaskPollTimers.value,
          [task.key]: setTimeout(() => pollStrategyAsyncTask(task, taskId, statusUrl), 3000)
        }
      }
      return status
    }
    setStrategyTaskSyncing(task.key, false)
    if (status === 'failed') {
      if (showToast) ElMessage.error(`${task.label}失败: ${strategyTaskMessageFromResult(payload) || rawStatus || '未知错误'}`)
    } else if (showToast) {
      ElMessage.success(`${task.label}完成`)
    }
    return status
  } catch (error) {
    setStrategyTaskSyncing(task.key, false)
    setStrategyTaskState(task.key, {
      status: 'failed',
      message: error.response?.data?.detail || error.message || '查询任务状态失败',
      updated_at: new Date().toISOString()
    })
    if (showToast) ElMessage.error(`${task.label}状态查询失败`)
    return 'failed'
  }
}

const waitStrategyAsyncTask = async (task, taskId, statusUrl) => {
  let status = 'running'
  while (status === 'running') {
    status = await pollStrategyAsyncTask(task, taskId, statusUrl, { scheduleNext: false, showToast: false })
    if (status === 'running') await sleep(3000)
  }
  return status
}

const getStateText = (status) => {
  const map = {
    running: '运行中',
    success: '成功',
    failed: '失败',
    paused: '已暂停',
    idle: '待执行'
  }
  return map[status] || '待执行'
}

const taskAliasMap = {
  update_trade_calendar: '交易日历同步',
  update_stock_list: '股票列表同步',
  update_index_list: '指数列表同步',
  sync_sectors: '板块列表同步',
  update_stock_today_data: '盘中股票/指数日线快照',
  update_today_data: '盘中指数分钟级快照',
  update_market_today_minute_data: '盘中股票/指数分钟级快照',
  sync_today_intraday_kline: '当天15/30m分钟K线落库',
  minute_kline_daily_repair_validate: '分钟K线补全验证',
  market_minute_history_repair: '股票/指数历史分钟级修复',
  today_full_market_refresh: '当天全市场更新自检',
  update_sector_intraday_stats: '板块当日统计刷新',
  official_daily_close_sync: '盘后正式日线写库',
  repair_previous_daily_kline: '次日自动巡检修复',
  core_data_maintenance: '核心数据自动维护'
}

const getTaskAlias = (taskName) => taskAliasMap[taskName] || taskName

const maintenanceStatusText = computed(() => getStateText(coreMaintenance.value.overall_status))
const runningTaskText = computed(() => {
  if (!coreMaintenance.value.running_task) return '-'
  const taskKey = coreMaintenance.value.running_task
  return maintenanceTasks.find((item) => item.key === taskKey)?.label || getTaskAlias(taskKey)
})
const latestFailedText = computed(() => {
  if (!coreMaintenance.value.latest_failed_task) return '-'
  const taskKey = coreMaintenance.value.latest_failed_task
  return maintenanceTasks.find((item) => item.key === taskKey)?.label || getTaskAlias(taskKey)
})

const refreshCoreMaintenanceStatus = async () => {
  try {
    const response = await axios.get(`${API_BASE}/system/core-data-maintenance/status`)
    coreMaintenance.value = normalizeCoreMaintenance(response.data?.data || {})
    lastStatusRefreshAt.value = new Date().toISOString()
  } catch (error) {
    ElMessage.error('获取核心自动维护状态失败')
  }
}

const refreshDataSourceCounts = async () => {
  dataSourceCountsLoading.value = true
  try {
    const date = dataSourceCountDate.value || ''
    const query = date ? `?trade_date=${encodeURIComponent(date)}` : ''
    const response = await axios.get(`${API_BASE}/system/data-source-counts${query}`)
    dataSourceCounts.value = response.data?.data || {
      trade_date: date,
      latest_available_date: null,
      checked_at: null,
      summary: {},
      rows: []
    }
    if (dataSourceCounts.value.trade_date) {
      dataSourceCountDate.value = dataSourceCounts.value.trade_date
    }
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || error.message || '数据源统计读取失败')
  } finally {
    dataSourceCountsLoading.value = false
  }
}

const repairDataSourceDate = async () => {
  const date = dataSourceCountDate.value || dataSourceCounts.value?.trade_date || ''
  if (!date) {
    ElMessage.warning('请先选择统计日期')
    return
  }
  if (!confirm(`确认修复 ${date} 的日线和 5m/15m/30m/60m 分钟数据吗？`)) return
  dataSourceRepairing.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/data-source-counts/repair-date?trade_date=${encodeURIComponent(date)}`)
    if (response.data?.success) {
      ElMessage.success(response.data?.message || '按日期修复任务已启动')
      pollTaskStatus(response.data?.task_name || 'data_source_date_repair', async () => {
        dataSourceRepairing.value = false
        await refreshDataSourceCounts()
      })
      return
    }
    dataSourceRepairing.value = false
    ElMessage.error(response.data?.message || '启动修复失败')
  } catch (error) {
    dataSourceRepairing.value = false
    ElMessage.error(error.response?.data?.detail || error.message || '按日期修复失败')
  }
}

const loadStartupReferenceSyncSetting = async () => {
  try {
    const response = await axios.get(`${API_BASE}/system/startup-reference-sync-setting`)
    const data = response.data?.data || {}
    startupReferenceSyncEnabled.value = !!data.enabled
    startupReferenceSyncDefaultEnabled.value = !!data.default_enabled
  } catch (error) {
    ElMessage.error('获取启动初始化配置失败')
  }
}

const refreshTdxGatewayDiagnostics = async () => {
  tdxGatewayLoading.value = true
  try {
    const response = await axios.get(`${API_BASE}/system/tdx-gateway/diagnostics?run_probe=true`)
    const data = response.data?.data || {}
    tdxGatewayDiagnostics.value = data.diagnostics || data || {}
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || error.message || 'TDX Gateway诊断失败')
  } finally {
    tdxGatewayLoading.value = false
  }
}

const probeTdxGateway = async () => {
  tdxGatewayProbing.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/tdx-gateway/probe`)
    const data = response.data?.data || {}
    tdxGatewayDiagnostics.value = data.diagnostics || tdxGatewayDiagnostics.value
    if (response.data?.success) {
      ElMessage.success('TDX Gateway真实取数探针通过')
    } else {
      ElMessage.warning('TDX Gateway真实取数探针未通过，请查看当前阻塞')
    }
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || error.message || 'TDX Gateway真实取数探针失败')
  } finally {
    tdxGatewayProbing.value = false
  }
}

const waitForTdxGatewayReady = async (timeoutMs = 45000) => {
  const started = Date.now()
  let lastPayload = null
  while (Date.now() - started < timeoutMs) {
    await sleep(3000)
    try {
      const response = await axios.get(`${API_BASE}/system/tdx-gateway/diagnostics?run_probe=true`)
      const data = response.data?.data || {}
      lastPayload = data.diagnostics || data || {}
      tdxGatewayDiagnostics.value = lastPayload
      const probe = lastPayload.market_data_probe || {}
      if (probe.probe_ok || lastPayload.verdict?.ready) {
        return { ok: true, payload: lastPayload }
      }
    } catch (error) {
      lastPayload = {
        ...(lastPayload || {}),
        health: { error: error.response?.data?.detail || error.message || 'TDX Gateway刷新失败' }
      }
      tdxGatewayDiagnostics.value = lastPayload
    }
  }
  return { ok: false, payload: lastPayload }
}

const recoverTdxGateway = async () => {
  tdxGatewayRecovering.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/tdx-gateway/recover?allow_restart=true`)
    const data = response.data?.data || {}
    tdxGatewayDiagnostics.value = data.diagnostics || tdxGatewayDiagnostics.value
    if (data.recovered || data.diagnostics?.verdict?.ready) {
      ElMessage.success('TDX Gateway已恢复并通过真实取数')
    } else if (data.restart_requested) {
      ElMessage.success('已请求重启Gateway，正在等待真实取数恢复')
      const ready = await waitForTdxGatewayReady()
      if (ready.ok) {
        ElMessage.success('TDX Gateway已恢复')
      } else {
        ElMessage.warning('Gateway重启后仍未通过真实取数，请查看阻塞清单')
      }
    } else {
      ElMessage.warning(response.data?.message || '一键恢复结束，请查看诊断结果')
    }
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || error.message || 'TDX Gateway一键恢复失败')
  } finally {
    tdxGatewayRecovering.value = false
  }
}

const initializeTdxGateway = async () => {
  tdxGatewayInitializing.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/tdx-gateway/initialize`)
    tdxGatewayDiagnostics.value = response.data?.data?.diagnostics || {}
    if (tdxGatewayDiagnostics.value?.verdict?.ready) {
      ElMessage.success('TDX Gateway重新初始化完成并通过真实取数')
    } else {
      const ready = await waitForTdxGatewayReady(18000)
      if (ready.ok) {
        ElMessage.success('TDX Gateway已恢复并通过真实取数')
      } else {
        ElMessage.warning('TDX Gateway初始化后仍未通过真实取数，请查看阻塞清单')
      }
    }
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || error.message || 'TDX Gateway初始化失败')
  } finally {
    tdxGatewayInitializing.value = false
  }
}

const restartTdxGateway = async () => {
  tdxGatewayRestarting.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/tdx-gateway/restart`)
    tdxGatewayDiagnostics.value = response.data?.data?.diagnostics || tdxGatewayDiagnostics.value
    if (response.data?.success) {
      ElMessage.success('TDX Gateway重启请求已发送，正在等待恢复')
      const ready = await waitForTdxGatewayReady()
      if (ready.ok) {
        ElMessage.success('TDX Gateway已恢复并通过真实取数')
      } else {
        ElMessage.warning('TDX Gateway重启后仍未通过真实取数，请查看阻塞清单')
      }
    } else {
      ElMessage.warning(response.data?.message || '重启请求未成功，请查看诊断')
    }
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || error.message || 'TDX Gateway重启请求失败')
  } finally {
    tdxGatewayRestarting.value = false
  }
}

const refreshTimeline = async () => {
  try {
    const response = await axios.get(`${API_BASE}/system/core-data-maintenance/timeline?limit=30`)
    if (response.data?.success) {
      coreMaintenance.value.timeline = response.data.data?.timeline || []
    }
  } catch (error) {
    ElMessage.error('获取任务时间线失败')
  }
}

const refreshStrategyMaintenanceStatus = async () => {
  const statusRequests = [
    {
      key: 'g3_state_alpha_refresh',
      url: '/gen3-state-alpha/shadow-monitor/status',
      map: (data) => ({
        status: data.workflow?.ok === false || data.monitor?.last_error ? 'failed' : 'success',
        message: data.workflow?.message || strategyTaskMessageFromResult(data.monitor?.last_result) || 'G3 State Alpha运行链路可用',
        last_success_at: data.monitor?.last_success_at || data.workflow?.checked_at || null,
        updated_at: data.workflow?.checked_at || data.monitor?.last_run_at || data.scheduler?.next_run_time,
        task_id: data.monitor?.last_refresh_task_id || '-',
        raw: data
      })
    },
    {
      key: 'g3_shadow_monitor',
      url: '/gen3-state-alpha/shadow-monitor/status',
      map: (data) => ({
        status: data.monitor?.last_error ? 'failed' : (data.monitor?.enabled ? 'idle' : 'paused'),
        message: data.scheduler?.message || (data.monitor?.enabled ? 'G3影子监控已启用，等待触发' : 'G3影子监控已暂停'),
        last_success_at: data.monitor?.last_error ? null : data.monitor?.last_success_at,
        updated_at: data.monitor?.last_run_at || data.monitor?.last_heartbeat_sent_at || data.scheduler?.next_run_time,
        task_id: data.monitor?.last_refresh_task_id || '-',
        raw: data
      })
    },
    {
      key: 'g3_workflow_status',
      url: '/gen3-state-alpha/workflow/status',
      map: (data) => ({
        status: data.ok === false ? 'failed' : 'success',
        message: data.message || `路线=${data.selected_route || '-'}，阻断=${Array.isArray(data.blockers) ? data.blockers.length : 0}`,
        last_success_at: data.ok === false ? null : data.checked_at,
        updated_at: data.checked_at,
        task_id: '-',
        raw: data
      })
    },
    {
      key: 'g3_current_shadow',
      url: '/gen3-state-alpha/current?refresh=false&limit=80',
      map: (data) => ({
        status: data.ok === false ? 'failed' : 'success',
        message: strategyTaskMessageFromResult(data) || data.diagnosis || 'G3当前影子票据可读取',
        last_success_at: data.ok === false ? null : (data.updated_at || data.generated_at || new Date().toISOString()),
        updated_at: data.updated_at || data.generated_at || new Date().toISOString(),
        task_id: '-',
        raw: data
      })
    },
    {
      key: 'g3_monitor_status',
      url: '/gen3-state-alpha/shadow-monitor/status',
      map: (data) => ({
        status: data.scheduler?.enabled ? 'success' : (data.monitor?.enabled ? 'success' : 'paused'),
        message: data.scheduler?.enabled ? `下次运行 ${formatDateTime(data.scheduler.next_run_time)}` : 'G3监控调度未启用',
        last_success_at: data.monitor?.last_success_at || null,
        updated_at: data.monitor?.last_run_at || data.scheduler?.next_run_time,
        task_id: data.monitor?.job_id || '-',
        raw: data
      })
    },
    {
      key: 'g3_current_readiness',
      url: '/gen3-state-alpha/current?refresh=false&limit=80',
      map: (data) => ({
        status: data.ok === false ? 'failed' : (data.formal_buy_signal || data.auto_order_allowed ? 'failed' : 'success'),
        message: data.formal_buy_signal || data.auto_order_allowed
          ? '正式交易闸门异常开启，请立即复核'
          : 'shadow-only / observe-only，正式交易闸门锁定',
        last_success_at: data.ok === false ? null : (data.updated_at || data.generated_at || new Date().toISOString()),
        updated_at: data.updated_at || data.generated_at || new Date().toISOString(),
        task_id: '-',
        raw: data
      })
    },
    {
      key: 'g3_historical_trades',
      url: '/gen3-state-alpha/historical-trades?limit=80',
      map: (data) => ({
        status: data.ok === false ? 'failed' : 'success',
        message: data.metrics?.trade_count != null
          ? `历史成交 ${data.metrics.trade_count} 笔，未来函数审计 ${data.future_leak_audit?.status || '已读取'}`
          : (strategyTaskMessageFromResult(data) || 'G3历史成交复核可读取'),
        last_success_at: data.ok === false ? null : new Date().toISOString(),
        updated_at: new Date().toISOString(),
        task_id: '-',
        raw: data
      })
    },
    {
      key: 'g3_evidence_inventory',
      url: '/gen3-state-alpha/evidence-inventory?limit=200',
      map: (data) => ({
        status: data.ok === false ? 'failed' : 'success',
        message: `证据归档 ${Array.isArray(data.inventory) ? data.inventory.length : 0} 条，蓝图报告${data.artifacts?.blueprint_report?.ok ? '可读' : '待复核'}`,
        last_success_at: data.ok === false ? null : new Date().toISOString(),
        updated_at: new Date().toISOString(),
        task_id: '-',
        raw: data
      })
    }
  ]

  await Promise.all(statusRequests.map(async (item) => {
    try {
      const response = await axios.get(`${API_BASE}${item.url}`)
      const data = response.data || {}
      setStrategyTaskState(item.key, item.map(data))
    } catch (error) {
      setStrategyTaskState(item.key, {
        status: 'failed',
        message: error.response?.data?.detail || error.message || '获取状态失败',
        updated_at: new Date().toISOString()
      })
    }
  }))
}

const runStrategyMaintenanceTask = async (task, options = {}) => {
  const { waitForCompletion = false, showToast = true } = options
  setStrategyTaskSyncing(task.key, true)
  setStrategyTaskState(task.key, {
    status: 'running',
    message: `${task.label} 已触发...`,
    updated_at: new Date().toISOString()
  })

  try {
    let response
    if (task.key === 'g3_state_alpha_refresh') {
      response = await axios.post(`${API_BASE}/gen3-state-alpha/workflow/refresh/run-once`, { force_send: false })
      const payload = response.data || {}
      if (payload.task_id) {
        const statusUrl = `/gen3-state-alpha/workflow/refresh-task/${payload.task_id}`
        return waitForCompletion
          ? await waitStrategyAsyncTask(task, payload.task_id, statusUrl)
          : await pollStrategyAsyncTask(task, payload.task_id, statusUrl, { showToast })
      }
      setStrategyTaskState(task.key, {
        status: payload.ok === false ? 'failed' : 'success',
        message: strategyTaskMessageFromResult(payload) || 'G3 State Alpha刷新完成',
        last_success_at: payload.ok === false ? null : new Date().toISOString(),
        updated_at: payload.checked_at || new Date().toISOString(),
        task_id: '-',
        raw: payload
      })
      setStrategyTaskSyncing(task.key, false)
      await refreshStrategyMaintenanceStatus()
      return getStrategyTaskStatus(task.key)
    }

    if (task.key === 'g3_shadow_monitor') {
      response = await axios.post(`${API_BASE}/gen3-state-alpha/shadow-monitor/run-once`, { force_send: true })
      const payload = response.data || {}
      if (payload.task_id) {
        const statusUrl = `/gen3-state-alpha/workflow/refresh-task/${payload.task_id}`
        return waitForCompletion
          ? await waitStrategyAsyncTask(task, payload.task_id, statusUrl)
          : await pollStrategyAsyncTask(task, payload.task_id, statusUrl, { showToast })
      }
      setStrategyTaskState(task.key, {
        status: payload.ok === false ? 'failed' : 'success',
        message: strategyTaskMessageFromResult(payload) || 'G3影子监控完成',
        last_success_at: payload.ok === false ? null : new Date().toISOString(),
        updated_at: payload.checked_at || new Date().toISOString(),
        task_id: '-',
        raw: payload
      })
      setStrategyTaskSyncing(task.key, false)
      await refreshStrategyMaintenanceStatus()
      return getStrategyTaskStatus(task.key)
    }

    if (task.key === 'g3_workflow_status') {
      response = await axios.get(`${API_BASE}/gen3-state-alpha/workflow/status`)
      const payload = response.data || {}
      setStrategyTaskState(task.key, {
        status: payload.ok === false ? 'failed' : 'success',
        message: payload.message || `路线=${payload.selected_route || '-'}，阻断=${Array.isArray(payload.blockers) ? payload.blockers.length : 0}`,
        last_success_at: payload.ok === false ? null : (payload.checked_at || new Date().toISOString()),
        updated_at: payload.checked_at || new Date().toISOString(),
        task_id: '-',
        raw: payload
      })
      setStrategyTaskSyncing(task.key, false)
      return getStrategyTaskStatus(task.key)
    }

    if (['g3_current_shadow', 'g3_current_readiness'].includes(task.key)) {
      response = await axios.get(`${API_BASE}/gen3-state-alpha/current?refresh=false&limit=80`)
      const payload = response.data || {}
      const guardrailOpen = !!payload.formal_buy_signal || !!payload.auto_order_allowed
      setStrategyTaskState(task.key, {
        status: payload.ok === false || guardrailOpen ? 'failed' : 'success',
        message: guardrailOpen
          ? '正式交易闸门异常开启，请立即复核'
          : (strategyTaskMessageFromResult(payload) || 'G3当前影子票据与准入检查可读取'),
        last_success_at: payload.ok === false || guardrailOpen ? null : new Date().toISOString(),
        updated_at: payload.updated_at || payload.generated_at || new Date().toISOString(),
        task_id: '-',
        raw: payload
      })
      setStrategyTaskSyncing(task.key, false)
      return getStrategyTaskStatus(task.key)
    }

    if (task.key === 'g3_monitor_status') {
      response = await axios.get(`${API_BASE}/gen3-state-alpha/shadow-monitor/status`)
      const payload = response.data || {}
      setStrategyTaskState(task.key, {
        status: payload.scheduler?.enabled ? 'success' : (payload.monitor?.enabled ? 'success' : 'paused'),
        message: payload.scheduler?.enabled ? `下次运行 ${formatDateTime(payload.scheduler.next_run_time)}` : 'G3监控调度未启用',
        last_success_at: payload.monitor?.last_success_at || null,
        updated_at: payload.monitor?.last_run_at || payload.scheduler?.next_run_time || new Date().toISOString(),
        task_id: payload.monitor?.job_id || '-',
        raw: payload
      })
      setStrategyTaskSyncing(task.key, false)
      return getStrategyTaskStatus(task.key)
    }

    if (task.key === 'g3_historical_trades') {
      response = await axios.get(`${API_BASE}/gen3-state-alpha/historical-trades?limit=80`)
      const payload = response.data || {}
      setStrategyTaskState(task.key, {
        status: payload.ok === false ? 'failed' : 'success',
        message: payload.metrics?.trade_count != null
          ? `历史成交 ${payload.metrics.trade_count} 笔，未来函数审计 ${payload.future_leak_audit?.status || '已读取'}`
          : (strategyTaskMessageFromResult(payload) || 'G3历史成交复核完成'),
        last_success_at: payload.ok === false ? null : new Date().toISOString(),
        updated_at: new Date().toISOString(),
        task_id: '-',
        raw: payload
      })
      setStrategyTaskSyncing(task.key, false)
      return getStrategyTaskStatus(task.key)
    }

    if (task.key === 'g3_evidence_inventory') {
      response = await axios.get(`${API_BASE}/gen3-state-alpha/evidence-inventory?limit=200`)
      const payload = response.data || {}
      setStrategyTaskState(task.key, {
        status: payload.ok === false ? 'failed' : 'success',
        message: `证据归档 ${Array.isArray(payload.inventory) ? payload.inventory.length : 0} 条，蓝图报告${payload.artifacts?.blueprint_report?.ok ? '可读' : '待复核'}`,
        last_success_at: payload.ok === false ? null : new Date().toISOString(),
        updated_at: new Date().toISOString(),
        task_id: '-',
        raw: payload
      })
      setStrategyTaskSyncing(task.key, false)
      return getStrategyTaskStatus(task.key)
    }

    if (task.key === 'gen2_strategy_refresh') {
      response = await axios.post(`${API_BASE}/trading/gen2/strategy-refresh/run-once`, { force: true })
      const payload = response.data || {}
      setStrategyTaskState(task.key, {
        status: payload.ok === false && !payload.skipped ? 'failed' : 'success',
        message: strategyTaskMessageFromResult(payload) || 'G2 30m策略刷新完成',
        last_success_at: payload.finished_at || new Date().toISOString(),
        updated_at: payload.finished_at || payload.last_run_at || new Date().toISOString(),
        task_id: payload.steps?.find?.((item) => item.task_id)?.task_id || payload.last_mainline_task_id || '-',
        raw: payload
      })
      setStrategyTaskSyncing(task.key, false)
      await refreshStrategyMaintenanceStatus()
      return getStrategyTaskStatus(task.key)
    }

    if (task.key === 'gen2_shadow_monitor') {
      response = await axios.post(`${API_BASE}/trading/gen2/shadow-monitor/run-once`, { force_send: false })
      const payload = response.data || {}
      setStrategyTaskState(task.key, {
        status: payload.ok === false ? 'failed' : 'success',
        message: strategyTaskMessageFromResult(payload) || 'G2实盘信号监控完成',
        last_success_at: payload.ok === false ? null : (payload.finished_at || payload.checked_at || new Date().toISOString()),
        updated_at: payload.finished_at || payload.checked_at || new Date().toISOString(),
        task_id: payload.official_rebuild?.task_id || '-',
        raw: payload
      })
      setStrategyTaskSyncing(task.key, false)
      await refreshStrategyMaintenanceStatus()
      return getStrategyTaskStatus(task.key)
    }

    if (task.key === 'gen2_selection_shadow') {
      response = await axios.post(`${API_BASE}/trading/gen2/risk-cool-shadow/update?pool_rank=200&alpha191_gate=g2_v2_complete`)
      const payload = response.data || {}
      if (payload.task_id) {
        const statusUrl = `/trading/gen2/risk-cool-shadow/update-task/${payload.task_id}`
        return waitForCompletion
          ? await waitStrategyAsyncTask(task, payload.task_id, statusUrl)
          : await pollStrategyAsyncTask(task, payload.task_id, statusUrl, { showToast })
      } else {
        throw new Error('未返回任务ID')
      }
    }

    if (task.key === 'gen2_mainline_hotspots') {
      response = await axios.post(`${API_BASE}/trading/gen2/mainline-hotspots/update?mode=all&limit=30`)
      const payload = response.data || {}
      if (payload.task_id) {
        const statusUrl = `/trading/gen2/mainline-hotspots/update-task/${payload.task_id}`
        return waitForCompletion
          ? await waitStrategyAsyncTask(task, payload.task_id, statusUrl)
          : await pollStrategyAsyncTask(task, payload.task_id, statusUrl, { showToast })
      } else {
        throw new Error('未返回任务ID')
      }
    }

    if (task.key === 'gen2_backtest_latest') {
      response = await axios.post(`${API_BASE}/trading/gen2/backtest/update-latest?legacy_330=true`)
      const payload = response.data || {}
      if (payload.task_id) {
        const statusUrl = `/trading/gen2/backtest/update-task/${payload.task_id}`
        return waitForCompletion
          ? await waitStrategyAsyncTask(task, payload.task_id, statusUrl)
          : await pollStrategyAsyncTask(task, payload.task_id, statusUrl, { showToast })
      } else {
        throw new Error('未返回任务ID')
      }
    }

    if (task.key === 'gen2_official_rebuild') {
      response = await axios.post(`${API_BASE}/trading/gen2/official-rebuild/update-latest?mode=breakout_only&legacy_330=true`)
      const payload = response.data || {}
      if (payload.task_id) {
        const statusUrl = `/trading/gen2/official-rebuild/update-task/${payload.task_id}`
        return waitForCompletion
          ? await waitStrategyAsyncTask(task, payload.task_id, statusUrl)
          : await pollStrategyAsyncTask(task, payload.task_id, statusUrl, { showToast })
      } else {
        throw new Error('未返回任务ID')
      }
    }

    if (task.key === 'v4_manual_holding_monitor') {
      response = await axios.post(`${API_BASE}/trading/v4/manual-holdings/monitor/run-once`, { force_send: false })
      const payload = response.data || {}
      setStrategyTaskState(task.key, {
        status: payload.ok === false ? 'failed' : 'success',
        message: strategyTaskMessageFromResult(payload) || '实盘持仓监控完成',
        last_success_at: payload.ok === false ? null : (payload.checked_at || payload.updated_at || new Date().toISOString()),
        updated_at: payload.checked_at || payload.updated_at || new Date().toISOString(),
        task_id: '-',
        raw: payload
      })
      setStrategyTaskSyncing(task.key, false)
      await refreshStrategyMaintenanceStatus()
      return getStrategyTaskStatus(task.key)
    }

    if (task.key === 'gen3_live_refresh') {
      response = await axios.get(`${API_BASE}/gen3-shadow/live?refresh=true&allow_after_close=true&limit=80`)
      const payload = response.data || {}
      setStrategyTaskState(task.key, {
        status: payload.ok === false || payload.refresh?.ok === false ? 'failed' : 'success',
        message: strategyTaskMessageFromResult(payload) || 'G3研究数据已刷新读取',
        last_success_at: payload.ok === false || payload.refresh?.ok === false ? null : new Date().toISOString(),
        updated_at: payload.updated_at || payload.generated_at || new Date().toISOString(),
        task_id: '-',
        raw: payload
      })
      setStrategyTaskSyncing(task.key, false)
      return getStrategyTaskStatus(task.key)
    }

    throw new Error('未配置策略维护任务')
  } catch (error) {
    setStrategyTaskSyncing(task.key, false)
    setStrategyTaskState(task.key, {
      status: 'failed',
      message: error.response?.data?.detail || error.message || '执行失败',
      updated_at: new Date().toISOString()
    })
    if (showToast) ElMessage.error(`${task.label}执行失败`)
    return 'failed'
  }
}

const runAllStrategyMaintenanceTasks = async (mode = 'daily') => {
  const selectedTasks = strategyMaintenanceTasks.filter((task) => (task.mode || 'daily') === mode)
  const modeText = mode === 'heavy' ? '证据复核' : 'G3日常维护'
  if (!selectedTasks.length) {
    ElMessage.info(`暂无可执行的${modeText}任务`)
    return
  }
  const confirmText = mode === 'heavy'
    ? '确认一键执行G3历史成交和证据归档复核吗？该操作只读取研究证据，不会打开正式交易闸门。'
    : '确认按顺序一键执行G3日常维护吗？将刷新影子工作流并检查监控、票据、台账和正式交易锁。'
  if (!confirm(confirmText)) return
  runningAllStrategyMaintenanceNow.value = true
  try {
    let failedCount = 0
    for (const task of selectedTasks) {
      const status = await runStrategyMaintenanceTask(task, { waitForCompletion: true, showToast: false })
      if (status === 'failed') failedCount += 1
    }
    await refreshStrategyMaintenanceStatus()
    if (failedCount > 0) {
      ElMessage.warning(`策略${modeText}一键执行完成，失败 ${failedCount} 项`)
    } else {
      ElMessage.success(`策略${modeText}一键执行完成`)
    }
  } finally {
    runningAllStrategyMaintenanceNow.value = false
  }
}

const toggleTimelinePanel = async () => {
  showTimelinePanel.value = !showTimelinePanel.value
  if (showTimelinePanel.value) {
    await refreshTimeline()
  }
}

const toggleCoreMaintenance = async () => {
  try {
    if (coreMaintenance.value.enabled) {
      await axios.post(`${API_BASE}/system/core-data-maintenance/stop`)
      ElMessage.success('核心数据自动维护已暂停')
    } else {
      await axios.post(`${API_BASE}/system/core-data-maintenance/start`)
      ElMessage.success('核心数据自动维护已启动')
    }
    await refreshCoreMaintenanceStatus()
    if (showTimelinePanel.value) await refreshTimeline()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || error.message || '操作失败')
  }
}

const toggleStartupReferenceSync = async () => {
  startupReferenceSyncSaving.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/startup-reference-sync-setting`, {
      enabled: !startupReferenceSyncEnabled.value
    })
    if (response.data?.success) {
      startupReferenceSyncEnabled.value = !!response.data.data?.enabled
      startupReferenceSyncDefaultEnabled.value = !!response.data.data?.default_enabled
      ElMessage.success(response.data?.message || '启动初始化配置已更新')
    } else {
      ElMessage.error('保存失败')
    }
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || error.message || '保存失败')
  } finally {
    startupReferenceSyncSaving.value = false
  }
}

const pollTaskStatus = async (taskName, onDone) => {
  try {
    const response = await axios.get(`${API_BASE}/system/task-status/${taskName}`)
    const task = response.data?.task || {}
    if (task.is_running) {
      setTimeout(() => pollTaskStatus(taskName, onDone), 3000)
      return
    }
    if (onDone) onDone(task)
    if (task.results) {
      ElMessage.success(task.results.message || '任务完成')
    } else if (task.error) {
      ElMessage.error(`任务失败: ${task.error}`)
    } else {
      ElMessage.info('任务已完成')
    }
    await refreshCoreMaintenanceStatus()
    if (showTimelinePanel.value) await refreshTimeline()
  } catch (error) {
    if (onDone) onDone()
  }
}

const refreshTodayFullMarketNow = async () => {
  if (!confirm('确认一键更新当天所有股票、指数、板块数据，并在盘后执行完整性自检吗？')) return
  runningTodayFullMarketRefreshNow.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/core-data-maintenance/refresh-today-full-market`)
    if (response.data?.success) {
      ElMessage.success(response.data?.message || '当天全市场更新自检任务已启动')
      pollTaskStatus(response.data?.task_name || 'today_full_market_refresh', () => {
        runningTodayFullMarketRefreshNow.value = false
      })
      return
    }
    runningTodayFullMarketRefreshNow.value = false
    ElMessage.error(response.data?.message || '启动失败')
  } catch (error) {
    runningTodayFullMarketRefreshNow.value = false
    ElMessage.error(error.response?.data?.detail || error.message || '启动失败')
  }
}

const triggerMarketMinuteHistoryRepairNow = async () => {
  if (!confirm('确认修复最近30个交易日的股票/指数5m/15m/30m/60m历史分钟K线吗？')) return
  runningMarketMinuteHistoryRepairNow.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/core-data-maintenance/run-task/market_minute_history_repair`)
    if (response.data?.success) {
      ElMessage.success(response.data?.message || '股票/指数历史分钟修复任务已启动')
      pollTaskStatus(response.data?.task_name || 'market_minute_history_repair', () => {
        runningMarketMinuteHistoryRepairNow.value = false
      })
      return
    }
    runningMarketMinuteHistoryRepairNow.value = false
    ElMessage.error(response.data?.message || '启动失败')
  } catch (error) {
    runningMarketMinuteHistoryRepairNow.value = false
    ElMessage.error(error.response?.data?.detail || error.message || '启动失败')
  }
}

const buildAutoRepairQuery = () => {
  const params = new URLSearchParams()
  params.set('days', String(autoRepairForm.value.days || 30))
  for (const period of autoRepairForm.value.periods || []) {
    params.append('periods', period)
  }
  return params.toString()
}

const runAutoRepair = async () => {
  if (!canRunAutoRepair.value) {
    ElMessage.warning('请先选择修复范围')
    return
  }
  const categoryText = autoRepairForm.value.category === 'strategy' ? '策略' : '数据'
  if (!confirm(`确认执行${categoryText}自动修复吗？`)) return
  autoRepairRunning.value = true
  try {
    if (autoRepairForm.value.category === 'strategy') {
      let failedCount = 0
      for (const task of autoRepairSelectedStrategyTasks.value) {
        const status = await runStrategyMaintenanceTask(task, { waitForCompletion: true, showToast: false })
        if (status === 'failed') failedCount += 1
      }
      await refreshStrategyMaintenanceStatus()
      if (failedCount > 0) {
        ElMessage.warning(`策略自动修复完成，失败 ${failedCount} 项`)
      } else {
        ElMessage.success('策略自动修复完成')
      }
      autoRepairRunning.value = false
      return
    }

    const scope = autoRepairForm.value.dataScope
    let taskName = ''
    let response = null
    if (scope === 'minute_history') {
      response = await axios.post(`${API_BASE}/system/core-data-maintenance/run-task/market_minute_history_repair?${buildAutoRepairQuery()}`)
      taskName = response.data?.task_name || 'market_minute_history_repair'
    } else if (scope === 'index_history') {
      response = await axios.post(`${API_BASE}/system/repair-history-klines`, {
        periods: autoRepairForm.value.periods,
        type: 'index'
      })
      taskName = 'repair_history_klines'
    } else if (scope === 'daily_stock') {
      response = await axios.post(`${API_BASE}/system/repair-daily-klines`)
      taskName = 'repair_daily_klines'
    } else if (scope === 'all_stock_history') {
      response = await axios.post(`${API_BASE}/system/repair-all-history-klines`)
      taskName = 'repair_all_history_klines'
    } else if (scope === 'emotion_cycle') {
      response = await axios.post(`${API_BASE}/system/repair-emotion-cycle-30d?days=${autoRepairForm.value.days || 30}`)
      taskName = 'repair_emotion_cycle_30d'
    }

    if (response?.data?.success && taskName) {
      ElMessage.success(response.data?.message || '自动修复任务已启动')
      pollTaskStatus(taskName, () => {
        autoRepairRunning.value = false
      })
      return
    }
    autoRepairRunning.value = false
    ElMessage.error(response?.data?.message || '启动失败')
  } catch (error) {
    autoRepairRunning.value = false
    ElMessage.error(error.response?.data?.detail || error.message || '自动修复失败')
  }
}

const syncMaintenanceTask = async (task) => {
  const backendTaskName = maintenanceTaskNameMap[task.key]
  if (!backendTaskName) {
    ElMessage.error('未配置对应的同步任务')
    return
  }

  maintenanceTaskSyncing.value = {
    ...maintenanceTaskSyncing.value,
    [task.key]: true
  }
  const forceTasks = new Set([
    'stock_intraday',
    'market_intraday_minutes',
    'market_intraday_kline_refresh',
    'sector_intraday_stats_refresh'
  ])
  const forceQuery = forceTasks.has(task.key) ? '?force=true' : ''

  try {
    const response = await axios.post(`${API_BASE}/system/core-data-maintenance/run-task/${task.key}${forceQuery}`)
    if (response.data?.success) {
      ElMessage.success(response.data?.message || '任务已启动')
      pollTaskStatus(response.data?.task_name || backendTaskName, () => {
        maintenanceTaskSyncing.value = {
          ...maintenanceTaskSyncing.value,
          [task.key]: false
        }
      })
      return
    }

    maintenanceTaskSyncing.value = {
      ...maintenanceTaskSyncing.value,
      [task.key]: false
    }
    ElMessage.error(response.data?.message || '启动失败')
  } catch (error) {
    maintenanceTaskSyncing.value = {
      ...maintenanceTaskSyncing.value,
      [task.key]: false
    }
    ElMessage.error(error.response?.data?.detail || error.message || '启动失败')
  }
}

const syncCoreAssetsNow = async () => {
  runningCoreAssetsSyncNow.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/core-data-maintenance/sync-core-assets`)
    if (response.data?.success) {
      ElMessage.success(response.data?.message || '核心数据同步任务已启动')
      pollTaskStatus(response.data?.task_name || 'core_data_manual_sync', () => {
        runningCoreAssetsSyncNow.value = false
      })
      return
    }
    runningCoreAssetsSyncNow.value = false
    ElMessage.error(response.data?.message || '启动失败')
  } catch (error) {
    runningCoreAssetsSyncNow.value = false
    ElMessage.error(error.response?.data?.detail || error.message || '启动失败')
  }
}

const updateStockList = async () => {
  loading.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/update-stock-list`)
    if (response.data?.success) {
      ElMessage.success('股票列表更新任务已启动')
      pollTaskStatus('update_stock_list', () => { loading.value = false })
      return
    }
    loading.value = false
    ElMessage.error('启动失败')
  } catch (error) {
    loading.value = false
    ElMessage.error('股票列表更新失败')
  }
}

const updateIndices = async () => {
  updatingIndices.value = true
  try {
    const response = await axios.post(`${API_BASE}/stocks/update-indices`)
    if (response.data?.success > 0) {
      ElMessage.success(`成功更新 ${response.data.success} 个指数`)
    } else {
      ElMessage.error(`更新指数列表失败: ${response.data?.error || '无数据'}`)
    }
  } catch (error) {
    ElMessage.error(`更新指数列表失败: ${error.response?.data?.detail || error.message}`)
  } finally {
    updatingIndices.value = false
  }
}

const syncSectors = async () => {
  if (!confirm('确认同步一二三级行业板块数据吗？')) return
  syncingSectors.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/sync-sectors`)
    if (response.data?.success) {
      ElMessage.success('行业板块同步任务已启动')
      pollTaskStatus('sync_sectors', () => { syncingSectors.value = false })
    } else {
      syncingSectors.value = false
      ElMessage.error('启动失败')
    }
  } catch (error) {
    syncingSectors.value = false
    ElMessage.error(`启动失败: ${error.response?.data?.detail || error.message}`)
  }
}

const syncSectorHistory = async () => {
  if (!confirm(`确认同步近 ${syncHistoryDays.value} 天板块历史涨跌数据吗？`)) return
  syncingSectorHistory.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/sync-sector-history?days=${syncHistoryDays.value}`)
    if (response.data?.success) {
      ElMessage.success('板块历史同步任务已启动')
      pollTaskStatus('sync_sector_history', () => { syncingSectorHistory.value = false })
    } else {
      syncingSectorHistory.value = false
      ElMessage.error('启动失败')
    }
  } catch (error) {
    syncingSectorHistory.value = false
    ElMessage.error(`启动失败: ${error.response?.data?.detail || error.message}`)
  }
}

const updateSectorIntradayStats = async () => {
  if (!confirm('确认刷新板块当日成分涨跌统计吗？')) return
  updatingSectorIntradayStats.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/update-sector-intraday-stats?force=true`)
    if (response.data?.success) {
      ElMessage.success('板块当日统计刷新任务已启动')
      pollTaskStatus('update_sector_intraday_stats', () => { updatingSectorIntradayStats.value = false })
    } else {
      updatingSectorIntradayStats.value = false
      ElMessage.error('启动失败')
    }
  } catch (error) {
    updatingSectorIntradayStats.value = false
    ElMessage.error(`启动失败: ${error.response?.data?.detail || error.message}`)
  }
}

const repairHistoryKlines = async () => {
  if (!selectedPeriods.value.length) {
    ElMessage.warning('请至少选择一个K线周期')
    return
  }
  if (!confirm('确认同步指数全量历史K线吗？')) return
  repairingHistoryKlines.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/repair-history-klines`, { periods: selectedPeriods.value, type: 'index' })
    if (response.data?.success) {
      ElMessage.success('历史K线同步任务已启动')
      pollTaskStatus('repair_history_klines', () => { repairingHistoryKlines.value = false })
    } else {
      repairingHistoryKlines.value = false
      ElMessage.error('启动失败')
    }
  } catch (error) {
    repairingHistoryKlines.value = false
    ElMessage.error(`启动失败: ${error.response?.data?.detail || error.message}`)
  }
}

const repairDailyKlinesData = async () => {
  if (!confirm('确认修复日K线数据吗？')) return
  repairingDailyKlines.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/repair-daily-klines`)
    if (response.data?.success) {
      ElMessage.success('日K线修复任务已启动')
      pollTaskStatus('repair_daily_klines', () => { repairingDailyKlines.value = false })
    } else {
      repairingDailyKlines.value = false
      ElMessage.error('启动失败')
    }
  } catch (error) {
    repairingDailyKlines.value = false
    ElMessage.error(`启动失败: ${error.response?.data?.detail || error.message}`)
  }
}

const repairAllHistoryKlines = async () => {
  if (!confirm('确认修复全量历史K线吗？该任务耗时较长。')) return
  repairingAllHistoryKlines.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/repair-all-history-klines`)
    if (response.data?.success) {
      ElMessage.success('全量历史K线修复任务已启动')
      pollTaskStatus('repair_all_history_klines', () => { repairingAllHistoryKlines.value = false })
    } else {
      repairingAllHistoryKlines.value = false
      ElMessage.error('启动失败')
    }
  } catch (error) {
    repairingAllHistoryKlines.value = false
    ElMessage.error(`启动失败: ${error.response?.data?.detail || error.message}`)
  }
}

const updateTradeCalendar = async () => {
  if (!confirm('确认更新交易日历吗？')) return
  updatingTradeCalendar.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/update-trade-calendar`)
    if (response.data?.success) {
      ElMessage.success('交易日历更新任务已启动')
      pollTaskStatus('update_trade_calendar', () => { updatingTradeCalendar.value = false })
    } else {
      updatingTradeCalendar.value = false
      ElMessage.error('启动失败')
    }
  } catch (error) {
    updatingTradeCalendar.value = false
    ElMessage.error(`启动失败: ${error.response?.data?.detail || error.message}`)
  }
}

const updateTodayData = async () => {
  if (!selectedUpdatePeriods.value.length) {
    ElMessage.warning('请至少选择一个K线周期')
    return
  }
  if (!confirm('确认更新当天指数数据吗？')) return
  updatingTodayData.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/update-today-data`, {
      periods: selectedUpdatePeriods.value,
      force: true,
    })
    if (response.data?.success) {
      ElMessage.success('当天指数数据更新任务已启动')
      pollTaskStatus('update_today_data', () => { updatingTodayData.value = false })
    } else {
      updatingTodayData.value = false
      ElMessage.error('启动失败')
    }
  } catch (error) {
    updatingTodayData.value = false
    ElMessage.error(`启动失败: ${error.response?.data?.detail || error.message}`)
  }
}

const updateStockTodayData = async () => {
  if (!selectedStockUpdatePeriods.value.length) {
    ElMessage.warning('请至少选择一个K线周期')
    return
  }
  if (!confirm('确认更新当天股票数据吗？')) return
  updatingStockTodayData.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/update-stock-today-data`, {
      periods: selectedStockUpdatePeriods.value,
      force: true,
    })
    if (response.data?.success) {
      ElMessage.success('当天股票数据更新任务已启动')
      pollTaskStatus('update_stock_today_data', () => { updatingStockTodayData.value = false })
    } else {
      updatingStockTodayData.value = false
      ElMessage.error('启动失败')
    }
  } catch (error) {
    updatingStockTodayData.value = false
    ElMessage.error(`启动失败: ${error.response?.data?.detail || error.message}`)
  }
}

const repairEmotion30d = async () => {
  if (!confirm('确认修复近30天情绪周期数据吗？')) return
  repairingEmotion30d.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/repair-emotion-cycle-30d?days=30`)
    if (response.data?.success) {
      ElMessage.success('情绪周期30天修复任务已启动')
      pollTaskStatus('repair_emotion_cycle_30d', () => { repairingEmotion30d.value = false })
    } else {
      repairingEmotion30d.value = false
      ElMessage.error('启动失败')
    }
  } catch (error) {
    repairingEmotion30d.value = false
    ElMessage.error(`启动失败: ${error.response?.data?.detail || error.message}`)
  }
}

const repairEmotionLatest = async () => {
  if (!confirm('确认更新最新交易日情绪周期吗？')) return
  repairingEmotionLatest.value = true
  try {
    const response = await axios.post(`${API_BASE}/system/repair-emotion-cycle-latest`)
    if (response.data?.success) {
      ElMessage.success('最新情绪周期更新任务已启动')
      pollTaskStatus('repair_emotion_cycle_latest', () => { repairingEmotionLatest.value = false })
    } else {
      repairingEmotionLatest.value = false
      ElMessage.error('启动失败')
    }
  } catch (error) {
    repairingEmotionLatest.value = false
    ElMessage.error(`启动失败: ${error.response?.data?.detail || error.message}`)
  }
}

const toggleEmotionAuto5m = async () => {
  const enable = !emotionAutoEnabled.value
  try {
    const endpoint = enable ? 'start' : 'stop'
    const response = await axios.post(`${API_BASE}/system/emotion-cycle-auto-5m/${endpoint}`)
    if (response.data?.success) {
      emotionAutoEnabled.value = enable
      ElMessage.success(enable ? '已开启盘中每5分钟自动修复' : '已关闭盘中自动修复')
    }
  } catch (error) {
    ElMessage.error(`${enable ? '开启' : '关闭'}失败: ${error.response?.data?.detail || error.message}`)
  }
}

onMounted(() => {
  loadStrategySuccessMemory()
  refreshCoreMaintenanceStatus()
  refreshDataSourceCounts()
  refreshStrategyMaintenanceStatus()
  loadStartupReferenceSyncSetting()
  refreshTdxGatewayDiagnostics()
  coreMaintenanceRefreshTimer.value = setInterval(async () => {
    await refreshCoreMaintenanceStatus()
    await refreshStrategyMaintenanceStatus()
    if (showTimelinePanel.value) await refreshTimeline()
  }, 10000)
})

onUnmounted(() => {
  if (coreMaintenanceRefreshTimer.value) {
    clearInterval(coreMaintenanceRefreshTimer.value)
    coreMaintenanceRefreshTimer.value = null
  }
  Object.values(strategyTaskPollTimers.value || {}).forEach((timer) => {
    if (timer) clearTimeout(timer)
  })
})
</script>

<style scoped>
.system-config {
  --ink: #17223b;
  --muted: #61708d;
  --line: rgba(90, 119, 164, 0.18);
  --panel: rgba(255, 255, 255, 0.92);
  --blue: #315fbd;
  --green: #0b8f63;
  --amber: #c56b08;
  --red: #c43d32;
  max-width: 1280px;
  margin: 0 auto;
  padding: 24px;
  color: var(--ink);
}

.ops-hero {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1.5fr) minmax(320px, 0.7fr);
  gap: 20px;
  overflow: hidden;
  margin-bottom: 18px;
  padding: 28px;
  border: 1px solid rgba(96, 126, 187, 0.2);
  border-radius: 28px;
  background:
    radial-gradient(circle at 8% 12%, rgba(74, 144, 226, 0.22), transparent 30%),
    radial-gradient(circle at 82% 6%, rgba(12, 143, 99, 0.18), transparent 28%),
    linear-gradient(135deg, #f8fbff 0%, #eef5ff 52%, #f7fbf4 100%);
  box-shadow: 0 24px 60px rgba(43, 68, 112, 0.12);
}

.ops-hero::after {
  content: "";
  position: absolute;
  inset: auto -80px -130px auto;
  width: 300px;
  height: 300px;
  border-radius: 999px;
  background: rgba(49, 95, 189, 0.08);
}

.hero-copy,
.hero-status-card {
  position: relative;
  z-index: 1;
}

.eyebrow,
.command-kicker {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: #53709f;
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.hero-copy h1 {
  margin: 0;
  font-size: clamp(36px, 5vw, 58px);
  letter-spacing: -0.05em;
  color: #13213c;
}

.hero-copy p {
  max-width: 720px;
  margin: 12px 0 0;
  color: var(--muted);
  font-size: 16px;
  line-height: 1.8;
}

.hero-status-card {
  display: grid;
  gap: 18px;
  align-content: center;
  padding: 20px;
  border: 1px solid rgba(255, 255, 255, 0.7);
  border-radius: 22px;
  background: rgba(255, 255, 255, 0.72);
  backdrop-filter: blur(14px);
}

.hero-health strong {
  display: block;
  font-size: 32px;
  letter-spacing: -0.03em;
}

.hero-health span,
.hero-meta span {
  color: var(--muted);
}

.hero-meta {
  display: grid;
  gap: 8px;
  font-size: 13px;
}

.command-grid {
  display: grid;
  grid-template-columns: 1.5fr 0.9fr 0.9fr;
  gap: 14px;
  margin-bottom: 18px;
}

.command-card {
  min-height: 210px;
  padding: 20px;
  border: 1px solid var(--line);
  border-radius: 22px;
  background: var(--panel);
  box-shadow: 0 16px 42px rgba(28, 49, 89, 0.08);
}

.command-card h2,
.command-card h3 {
  margin: 8px 0;
  color: #172746;
  letter-spacing: -0.03em;
}

.command-card h2 {
  font-size: 30px;
}

.command-card p {
  color: var(--muted);
  line-height: 1.7;
}

.primary-command {
  color: #fff;
  background:
    linear-gradient(135deg, rgba(28, 80, 168, 0.94), rgba(9, 123, 92, 0.9)),
    radial-gradient(circle at top right, rgba(255, 255, 255, 0.25), transparent 35%);
}

.primary-command h2,
.primary-command p,
.primary-command .command-kicker {
  color: #fff;
}

.section-card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 24px;
  padding: 18px;
  margin-bottom: 18px;
  box-shadow: 0 16px 42px rgba(31, 51, 86, 0.07);
}

.section-title-row {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: center;
  margin-bottom: 14px;
}

.section-title-row h2 {
  margin: 2px 0 0;
  color: #172746;
  font-size: 24px;
  letter-spacing: -0.03em;
}

.section-actions {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}

.gateway-section {
  border-color: rgba(197, 107, 8, 0.24);
}

.gateway-verdict {
  display: grid;
  gap: 6px;
  margin-top: 12px;
  padding: 12px 14px;
  border-radius: 14px;
  border: 1px solid var(--line);
  background: #f7f9fc;
}

.gateway-verdict strong {
  color: #172746;
  font-size: 15px;
}

.gateway-verdict span {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.6;
}

.gateway-verdict.level-ok {
  border-color: rgba(11, 143, 99, 0.24);
  background: #f3fbf7;
}

.gateway-verdict.level-warning {
  border-color: rgba(197, 107, 8, 0.28);
  background: #fff9ed;
}

.gateway-verdict.level-error {
  border-color: rgba(196, 61, 50, 0.28);
  background: #fff5f3;
}

.gateway-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-top: 14px;
}

.gateway-card {
  display: grid;
  gap: 6px;
  min-width: 0;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 16px;
  background: linear-gradient(180deg, #fff, #fbfcff);
}

.gateway-card span,
.gateway-card small,
.diagnostic-line span {
  color: var(--muted);
  font-size: 12px;
}

.gateway-card strong {
  min-width: 0;
  overflow-wrap: anywhere;
  color: #203153;
  font-size: 16px;
}

.gateway-diagnostics {
  display: grid;
  gap: 8px;
  margin-top: 14px;
}

.diagnostic-line {
  display: grid;
  grid-template-columns: 120px minmax(0, 1fr);
  gap: 10px;
  padding: 10px 12px;
  border-radius: 12px;
  background: #f7f9fc;
}

.diagnostic-line strong {
  min-width: 0;
  overflow-wrap: anywhere;
  color: #26395f;
  font-size: 13px;
}

.danger-line {
  background: #fff5f3;
}

.danger-line strong {
  color: var(--red);
}

.gateway-check-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin-top: 14px;
}

.gateway-check {
  display: grid;
  gap: 5px;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: #fff;
}

.gateway-check span,
.gateway-blockers span {
  color: var(--muted);
  font-size: 12px;
}

.gateway-check strong {
  min-width: 0;
  overflow-wrap: anywhere;
  color: #26395f;
  font-size: 13px;
}

.gateway-check.ok {
  border-color: rgba(11, 143, 99, 0.18);
}

.gateway-check.blocked {
  border-color: rgba(196, 61, 50, 0.22);
  background: #fffafa;
}

.gateway-blockers {
  display: grid;
  gap: 6px;
  margin-top: 12px;
  padding: 12px 14px;
  border-radius: 14px;
  border: 1px solid rgba(196, 61, 50, 0.22);
  background: #fff5f3;
}

.gateway-blockers strong {
  color: var(--red);
  font-size: 14px;
}

.gateway-manual {
  display: grid;
  gap: 8px;
  margin-top: 12px;
  padding: 12px 14px;
  border-radius: 14px;
  border: 1px solid rgba(49, 95, 189, 0.18);
  background: #f7faff;
}

.gateway-manual > strong {
  color: #203153;
  font-size: 14px;
}

.gateway-command {
  display: grid;
  grid-template-columns: 160px minmax(0, 1fr);
  gap: 10px;
  align-items: center;
}

.gateway-command span {
  color: var(--muted);
  font-size: 12px;
}

.gateway-command code {
  min-width: 0;
  overflow-wrap: anywhere;
  padding: 6px 8px;
  border-radius: 8px;
  color: #172746;
  background: rgba(49, 95, 189, 0.08);
  font-size: 12px;
}

.data-source-panel {
  margin-top: 16px;
  margin-bottom: 16px;
  border: 1px solid #e5ebf5;
  border-radius: 10px;
  padding: 14px;
  background: #fbfdff;
}

.data-source-toolbar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.data-source-toolbar h3 {
  margin: 2px 0 0;
  color: #203153;
  font-size: 16px;
}

.data-source-date-actions {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.date-input {
  min-width: 150px;
  height: 38px;
  border: 1px solid #d9e1ee;
  border-radius: 10px;
  padding: 0 10px;
  color: #203153;
  background: #fff;
  font-size: 13px;
}

.data-source-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 16px;
  margin-top: 10px;
  color: #6d7d96;
  font-size: 12px;
}

.data-source-table-wrap {
  margin-top: 12px;
  overflow-x: auto;
}

.data-source-table {
  width: 100%;
  min-width: 980px;
  border-collapse: collapse;
  font-size: 13px;
}

.data-source-table th {
  padding: 10px 12px;
  color: #6a7890;
  background: #f1f5fb;
  text-align: left;
  font-weight: 700;
  white-space: nowrap;
}

.data-source-table td {
  padding: 11px 12px;
  border-bottom: 1px solid #e8eef7;
  color: #243956;
  vertical-align: top;
}

.data-source-table tr:last-child td {
  border-bottom: none;
}

.data-source-table td strong,
.data-source-table td small {
  display: block;
}

.data-source-table td small {
  margin-top: 3px;
  color: #8a97aa;
  font-size: 11px;
}

.data-source-row-missing td {
  background: #fff8f7;
}

.data-source-row-incomplete td {
  background: #fffaf0;
}

.period-stack {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  max-width: 520px;
}

.period-chip {
  display: inline-grid;
  grid-template-columns: auto auto auto;
  align-items: center;
  gap: 4px 6px;
  min-height: 28px;
  border: 1px solid #dbe5f1;
  border-radius: 8px;
  padding: 5px 7px;
  color: #40536d;
  background: #fff;
  font-size: 12px;
  white-space: nowrap;
}

.period-chip strong {
  color: #203153;
}

.period-chip em {
  color: #60718a;
  font-style: normal;
}

.period-chip small {
  grid-column: 1 / -1;
  margin-top: 0;
  color: #a35a08;
}

.period-chip.complete {
  border-color: rgba(11, 143, 99, 0.18);
  background: #f4fbf7;
}

.period-chip.incomplete {
  border-color: rgba(197, 107, 8, 0.24);
  background: #fff8eb;
}

.period-summary {
  color: #556983;
  font-size: 12px;
  line-height: 1.5;
}

.table-empty {
  color: #7b8aa1;
  text-align: center;
}

.pipeline-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
}

.pipeline-card {
  border: 1px solid var(--line);
  border-radius: 20px;
  overflow: hidden;
  background: #fff;
}

.pipeline-head {
  padding: 16px;
  border-bottom: 1px solid var(--line);
  background: linear-gradient(135deg, rgba(49, 95, 189, 0.08), transparent);
}

.pipeline-card.accent-green .pipeline-head {
  background: linear-gradient(135deg, rgba(11, 143, 99, 0.09), transparent);
}

.pipeline-card.accent-amber .pipeline-head {
  background: linear-gradient(135deg, rgba(197, 107, 8, 0.1), transparent);
}

.pipeline-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: flex-start;
}

.pipeline-head h3 {
  margin: 6px 0;
  color: #1c2b47;
}

.pipeline-head p {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.6;
}

.task-stack {
  display: grid;
}

.task-chip {
  position: relative;
  padding: 14px 14px 44px;
  border-bottom: 1px solid #edf2f7;
}

.task-chip:last-child {
  border-bottom: none;
}

.task-chip-main {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.task-chip-main strong {
  flex: 1;
  min-width: 0;
  color: #203153;
}

.task-dot {
  width: 9px;
  height: 9px;
  border-radius: 999px;
  background: #8793a8;
}

.task-chip-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
  color: #7a879d;
  font-size: 12px;
}

.task-chip p {
  margin: 8px 0 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.5;
}

.strategy-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-top: 14px;
}

.strategy-card {
  position: relative;
  padding: 16px 16px 48px;
  border: 1px solid var(--line);
  border-radius: 18px;
  background: linear-gradient(180deg, #fff, #f9fbff);
}

.strategy-card-heavy {
  background: linear-gradient(180deg, #fffaf4, #fff);
  border-color: rgba(197, 107, 8, 0.24);
}

.strategy-title-row {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  align-items: center;
}

.strategy-title-row h3 {
  margin: 0;
  color: #203153;
  font-size: 15px;
}

.strategy-title-badges {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  white-space: nowrap;
}

.task-kind {
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  padding: 3px 8px;
  font-size: 11px;
  font-weight: 700;
}

.task-kind.daily {
  color: #0c7652;
  background: #e9f8f0;
}

.task-kind.heavy {
  color: #a35a08;
  background: #fff1d8;
}

.strategy-card p {
  min-height: 58px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.6;
}

.strategy-meta {
  display: grid;
  gap: 4px;
  color: #7c8aa0;
  font-size: 12px;
}

.strategy-meta strong {
  color: #203153;
}

.mini-btn {
  position: absolute;
  right: 12px;
  bottom: 12px;
  border: none;
  border-radius: 999px;
  padding: 7px 12px;
  color: #fff;
  background: #4d65d9;
  cursor: pointer;
  font-size: 12px;
}

.mini-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.maintenance-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 14px;
}

.btn {
  border: none;
  border-radius: 12px;
  padding: 10px 14px;
  color: #fff;
  background: #4d65d9;
  cursor: pointer;
  box-shadow: 0 10px 22px rgba(42, 72, 143, 0.18);
}

.hero-btn {
  margin-top: 10px;
  padding: 13px 18px;
  color: #17315a;
  background: #fff;
  font-weight: 800;
}

.btn.secondary,
.btn.subtle {
  color: #26405f;
  background: #edf4ff;
  box-shadow: none;
}

.btn.soft {
  box-shadow: none;
}

.btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.btn.primary {
  background: #3f62e5;
}

.btn.warning {
  background: #ca6a00;
}

.btn.success {
  background: #0e8b57;
}

.btn.danger {
  background: #ba3b2d;
}

.btn.ghost {
  background: #6b7387;
}

.status-pill {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 5px 11px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  white-space: nowrap;
}

.status-pill.large {
  width: fit-content;
  padding: 7px 12px;
}

.status-pill.enabled {
  color: #117447;
  background: #e8f8ee;
}

.status-pill.paused {
  color: #805500;
  background: #fff4d8;
}

.status-pill.failed {
  color: #b42318;
  background: #fff0ef;
}

.timeline-panel {
  margin-top: 14px;
  border: 1px solid #ebeff7;
  border-radius: 10px;
  padding: 10px;
  background: #fbfdff;
}

.timeline-title {
  font-weight: 600;
  margin-bottom: 8px;
  color: #2b4269;
}

.timeline-empty {
  color: #70839f;
  font-size: 13px;
}

.timeline-item {
  display: grid;
  grid-template-columns: 1.2fr 0.8fr 1.8fr 0.8fr 1.2fr;
  gap: 8px;
  font-size: 12px;
  border-bottom: 1px solid #eef3fb;
  padding: 8px 0;
}

.timeline-item:last-child {
  border-bottom: none;
}

.timeline-msg {
  color: #5e6f89;
}

.timeline-duration {
  color: #38506f;
  font-weight: 600;
}

.section-tip {
  margin-top: 0;
  margin-bottom: 12px;
  color: #667997;
}

.config-note {
  margin-top: 10px;
  color: #667997;
  font-size: 13px;
}

.advanced-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.auto-repair-card {
  grid-column: 1 / -1;
  border-color: rgba(49, 95, 189, 0.26);
  background: linear-gradient(180deg, #ffffff, #f7fbff);
}

.auto-repair-head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
}

.auto-repair-layout {
  display: grid;
  gap: 14px;
  margin-top: 12px;
}

.segmented-control {
  display: inline-grid;
  grid-template-columns: repeat(2, minmax(120px, 1fr));
  max-width: 360px;
  border: 1px solid #dbe5f3;
  border-radius: 10px;
  overflow: hidden;
  background: #eef4fb;
}

.segmented-control button {
  min-height: 38px;
  border: none;
  color: #425775;
  background: transparent;
  cursor: pointer;
  font-weight: 700;
}

.segmented-control button.active {
  color: #fff;
  background: #315fbd;
}

.auto-repair-fields {
  display: grid;
  grid-template-columns: repeat(2, minmax(180px, 1fr));
  gap: 12px;
}

.auto-repair-fields label {
  display: grid;
  gap: 6px;
  color: #52657f;
  font-size: 13px;
  font-weight: 700;
}

.auto-repair-fields select {
  min-height: 38px;
  border: 1px solid #d9e2f0;
  border-radius: 8px;
  padding: 0 10px;
  color: var(--ink);
  background: #fff;
}

.auto-period-box {
  grid-column: 1 / -1;
  align-items: center;
  margin-bottom: 0;
}

.auto-repair-preview {
  grid-column: 1 / -1;
  min-height: 38px;
  border: 1px solid #e1e8f3;
  border-radius: 8px;
  padding: 10px;
  color: #52657f;
  background: #fbfdff;
  font-size: 13px;
  line-height: 1.5;
}

.auto-repair-submit {
  width: fit-content;
}

.manual-card {
  border: 1px solid #ebeff7;
  border-radius: 18px;
  padding: 14px;
  background: #fff;
}

.manual-card.spotlight {
  background: linear-gradient(135deg, #f6fbff, #eef8f3);
}

.manual-card h3 {
  margin: 0;
  color: #243b63;
}

.manual-card p {
  font-size: 13px;
  color: #667a97;
}

.tag-row {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}

.tag {
  font-size: 12px;
  color: #3766ae;
  background: #edf4ff;
  border-radius: 999px;
  padding: 2px 8px;
}

.button-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.period-box {
  font-size: 12px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 10px;
}

.days-box {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 10px;
}

.days-box input {
  width: 90px;
  padding: 6px;
  border: 1px solid #d4deef;
  border-radius: 6px;
}

.state-running {
  color: #1f66d5;
}

.state-success {
  color: #1a8f58;
}

.state-failed {
  color: #cf352e;
}

.state-paused {
  color: #7a6a3d;
}

.state-idle {
  color: #5f6f8a;
}

.state-bg-running {
  background: #1f66d5;
}

.state-bg-success {
  background: #1a8f58;
}

.state-bg-failed {
  background: #cf352e;
}

.state-bg-paused {
  background: #b5811f;
}

.state-bg-idle {
  background: #7b8798;
}

@media (max-width: 980px) {
  .ops-hero,
  .command-grid,
  .gateway-grid,
  .pipeline-grid,
  .strategy-grid {
    grid-template-columns: 1fr;
  }

  .advanced-grid {
    grid-template-columns: 1fr;
  }

  .auto-repair-head,
  .auto-repair-fields {
    grid-template-columns: 1fr;
  }

  .auto-repair-head {
    flex-direction: column;
  }

  .section-title-row,
  .pipeline-head {
    align-items: flex-start;
    flex-direction: column;
  }

  .timeline-item {
    grid-template-columns: 1fr;
  }
}
</style>
