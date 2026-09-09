<template>
  <div class="ops-page">
    <header class="ops-heading"><div><h1>后台任务与健康</h1><p>每10秒自动刷新 · 最近读取 {{ dateTime(board.generated_at) }} · {{ board.api_runtime?.source==='live'?'API实时状态':'已发布快照' }}</p></div><el-button :loading="loading" @click="load">刷新状态</el-button></header>
    <div v-if="error" role="alert" class="ops-error">{{ error }}；下方保留上次读取结果。</div>
    <section class="ops-panel" aria-label="服务启动与健康">
      <div class="ops-toolbar"><h2>服务启动与健康</h2><el-tag :type="!error&&board.api_runtime?.ready?'success':'danger'">{{ error?'连接中断':!board.api_runtime?.fresh?'状态未知 / 心跳过期':board.api_runtime?.ready?'调度服务已就绪':'服务未就绪' }}</el-tag></div>
      <p>服务启动 {{ dateTime(board.api_runtime?.started_at) }} · 心跳 {{ dateTime(board.api_runtime?.generated_at) }} · 进程 {{ board.api_runtime?.pid || '未知' }}</p>
      <p class="ops-meta">API任务心跳超过30秒视为未知；Windows任务状态按发布器采集时间判断。服务就绪不代表行情与策略数据已验收。</p>
      <p v-if="!board.api_runtime?.startups?.length">尚未收到正式服务的启动记录。</p>
      <p v-for="item in (board.api_runtime?.startups||[]).filter(x=>x.status==='failed')" :key="item.name" class="ops-error" role="alert">{{ item.name }}：{{ item.error }}</p>
      <details v-if="board.api_runtime?.startups?.length"><summary style="cursor:pointer;padding:10px 0">查看全部 {{ board.api_runtime.startups.length }} 项启动检查</summary><div v-for="item in board.api_runtime.startups" :key="item.name" class="ops-candidate"><div class="ops-toolbar"><strong>{{ item.name }}</strong><el-tag :type="tone(board.api_runtime?.fresh?item.status:'unknown')">{{ label(board.api_runtime?.fresh?item.status:'unknown') }}</el-tag><span class="ops-meta">{{ dateTime(item.finished_at) }}</span></div></div></details>
    </section>
    <el-alert v-if="board.operations_publisher?.publisher_status!=='healthy'||!board.operations_publisher?.notifications_enabled||!board.operations_publisher?.notification_transport_ok" title="独立数据告警尚未接管或心跳已过期" description="此处记录异常不等于已发送通知；上线需同时部署通知发布器。原G3通知仅在自身运行窗口内兜底。" type="warning" :closable="false" show-icon />
    <p class="ops-meta">通知通道：{{ label(board.operations_publisher?.notification_transport?.state) }} · 最近SMTP接受 {{ dateTime(board.operations_publisher?.notification_transport?.last_smtp_accepted_at) }}</p>
    <el-alert v-if="board.host_inventory_status !== 'healthy'" title="Windows任务清单尚未发布或已过期" description="未把缺少心跳解释为任务正常。请检查 Operations Publisher 的运行状态。" type="warning" :closable="false" show-icon />
    <section class="ops-panel" aria-label="备份与恢复验收">
      <div class="ops-toolbar"><h2>备份与恢复验收</h2><el-tag :type="tone(board.backups?.status)">{{ label(board.backups?.status) }}</el-tag></div>
      <p>最近完成备份 {{ dateTime(board.backups?.last_backup_at) }} · 距今 {{ board.backups?.backup_age_hours ?? '未知' }} 小时</p>
      <p>最近恢复验证 {{ dateTime(board.backups?.last_restore_verified_at) }} · 距今 {{ board.backups?.restore_age_days ?? '未知' }} 天</p>
      <p>宿主归档 {{ board.backups?.host_copy_verified === true ? '校验通过' : '尚未验收' }} · 执行状态 {{ label(board.backups?.execution_status) }} · 批次 {{ board.backups?.backup_id || '未知' }}</p>
      <p v-for="reason in board.backups?.reasons||[]" :key="reason" class="ops-error">{{ label(reason) }}</p>
      <p v-if="board.backups?.error" class="ops-error">{{ board.backups.error }}</p>
      <p class="ops-meta">超过25小时无新备份、恢复验证超过7天或备份失败进入统一异常记录。备份异常需排查，不自动重试未知结果的异步备份；不会因文件刚刷新就显示健康。</p>
    </section>
    <div class="ops-metrics"><div v-for="item in metrics" :key="item.label" class="ops-metric"><span>{{ item.label }}</span><strong>{{ item.value ?? '—' }}</strong></div></div>
    <section v-if="board.historical_recovery?.total" class="ops-panel" aria-label="历史数据恢复进度">
      <div class="ops-toolbar"><h2>历史数据恢复</h2><el-tag type="warning">{{ board.historical_recovery.candidate_complete?'候选已校验，待上线验收':board.historical_recovery.pending?.some(x=>x.state==='blocked')?'恢复已阻断':'候选构建记录' }}</el-tag></div>
      <p>已校验 {{ board.historical_recovery.verified }} / {{ board.historical_recovery.total }} 个月份周期 · {{ Number(board.historical_recovery.rows).toLocaleString() }} 行</p>
      <el-progress :percentage="Math.floor(board.historical_recovery.verified / board.historical_recovery.total * 100)" />
      <p>数据截止 {{ board.historical_recovery.cutoff }} · 不完整组合 {{ board.historical_recovery.incomplete_buckets }} 个（未生成完整K线）</p>
      <p v-for="item in board.historical_recovery.pending" :key="item.key" :class="{'ops-error':item.state==='blocked'}">{{ recoveryPeriod(item.key) }} · {{ recoveryStage(item.state) }}<span v-if="item.error">：{{ item.error }}</span></p>
      <p class="ops-meta">最近记录 {{ dateTime(board.historical_recovery.updated_at) }}{{ board.historical_recovery.record_stale?' · 记录已过期，需核对执行进程':'' }}。此处显示恢复检查点，不代表进程在线；候选构建不会自动替换正式表。</p>
    </section>
    <section class="ops-panel" aria-label="任务职责分组">
      <div class="ops-toolbar"><h2>按职责查看任务</h2><el-radio-group v-model="scope" @change="changeScope"><el-radio-button value="core">核心任务</el-radio-button><el-radio-button value="external">外部联动</el-radio-button><el-radio-button value="retired">已退役</el-radio-button></el-radio-group></div>
      <div class="ops-task-groups"><button v-for="item in visibleGroups" :key="item.id" class="ops-task-group" :class="{selected:group===item.id}" :aria-pressed="group===item.id" @click="group=item.id"><strong>{{ item.label }} <span>{{ item.count }}项</span></strong><p>{{ item.purpose }}</p><small>{{ item.attention ? `${item.attention}项需要核对` : '查看任务用途与执行详情' }}</small></button></div>
      <p class="ops-meta">按业务职责归类，保留真实子任务和执行窗口。已退役入口不计作当前生产任务。</p>
    </section>
    <section class="ops-panel"><div class="ops-toolbar"><h2>{{ groupTitle }} · {{ groupRows.length }}项</h2><el-radio-group v-model="filter"><el-radio-button value="all">全部</el-radio-button><el-radio-button value="attention">需处理</el-radio-button><el-radio-button value="running">运行中</el-radio-button><el-radio-button value="disabled">已停用</el-radio-button></el-radio-group><el-input v-model="search" placeholder="搜索本组任务或用途" aria-label="搜索任务或用途" clearable style="max-width:260px" /></div>
      <div class="ops-scroll"><el-table :data="filtered" row-key="name" stripe empty-text="尚无任务清单">
        <el-table-column type="expand"><template #default="{row}"><div class="ops-detail" style="padding:16px"><div><strong>执行责任</strong><p>{{ row.purpose }}</p><p>执行时机：{{ row.schedule_description }}</p><p>实际计划：{{ row.window || '以宿主机触发器为准' }}</p><p class="ops-meta">登记名称：{{ row.name }}</p><p>最近成功：{{ dateTime(row.last_success) }}</p><p>最近完成：{{ dateTime(row.last_finished) }} · 耗时 {{ row.duration_seconds ?? '—' }} 秒</p><p>本进程执行 {{ row.runs ?? '—' }} 次 · 失败 {{ row.failures ?? '—' }} 次</p><p v-if="row.last_schedule_warning">{{ row.last_schedule_warning }} · {{ dateTime(row.last_warning_at) }}</p></div><div><strong>产物与失败影响</strong><p>{{ row.produces }}</p><p>{{ row.failure_impact }}</p><p>{{ row.business_reason || '执行完成不代表数据验收通过' }}</p><p v-if="row.consolidation_note">整合说明：{{ row.consolidation_note }}</p><p v-if="row.replacement">接管入口：{{ row.replacement }}</p><div v-if="row.ingestion?.lanes?.length"><strong>盘中采集通道</strong><p>记录时间：{{ dateTime(row.ingestion.generated_at) }} · {{ row.ingestion.phase==='running'?'运行记录':'已停止' }} · 快照目标 {{ row.ingestion.snapshot_target_seconds }}秒</p><p v-for="lane in row.ingestion.lanes" :key="lane.name">{{ lane.name==='snapshot'?'全市场快照':'分钟K线' }}：{{ label(lane.status) }} · 最近耗时 {{ lane.duration_seconds ?? '—' }}秒<span v-if="lane.running_seconds!=null"> · 已运行 {{ lane.running_seconds }}秒</span><span v-if="lane.result?.universe_count"> · 源时间在5分钟内 {{ lane.result.source_fresh_300s }}/{{ lane.result.universe_count }}（含指数，豁免待核对）</span><span v-if="lane.error"> · {{ lane.error }}</span><span v-if="lane.result?.errors && Object.keys(lane.result.errors).length"> · {{ lane.result.errors }}</span></p><p class="ops-meta">记录时间与源时间需分别核对；历史运行记录不代表当前在线。</p></div><p v-if="row.error" role="alert">{{ row.error }}</p></div></div></template></el-table-column>
        <el-table-column label="任务 / 作用" min-width="350"><template #default="{row}"><strong>{{ row.title || row.name }}</strong><p class="ops-task-purpose">{{ row.purpose }}</p></template></el-table-column><el-table-column prop="executor" label="执行器" width="95" />
        <el-table-column label="运行状态" width="165"><template #default="{row}"><el-tag :type="tone(error?'unknown':row.status)">{{ label(error?'unknown':row.status) }}</el-tag><small v-if="row.scheduler_paused"> 调度已暂停</small><small v-if="row.status==='running'&&row.running_seconds!=null"> {{ row.running_seconds }}秒</small></template></el-table-column>
        <el-table-column label="业务验收" width="150"><template #default="{row}">{{ label(row.business_status) }}</template></el-table-column>
        <el-table-column label="最近执行" width="180"><template #default="{row}">{{ dateTime(row.last_run) }}</template></el-table-column>
        <el-table-column label="下次执行" width="180"><template #default="{row}">{{ dateTime(row.next_run) }}</template></el-table-column>
        <el-table-column prop="last_result" label="退出码" width="105" />
      </el-table></div><p class="ops-meta">宿主机清单：{{ dateTime(board.host_inventory_at) }}。展开任务可查看失败原因、最近成功和耗时；退出码与业务结果分别展示。</p>
    </section>
    <section class="ops-panel" aria-label="数据源稳定性"><h2>数据源稳定性 · 最近24小时</h2><p v-if="!board.source_metrics?.sources?.length">尚无真实请求样本，成功率与延迟未知。</p><div v-for="source in board.source_metrics?.sources||[]" :key="source.source+source.operation" class="ops-candidate"><strong>{{ source.source }} · {{ source.operation }}</strong><p>请求 {{ source.requests }} 次 · 失败 {{ source.failures }} · 超时 {{ source.timeouts }} · P95 {{ source.p95_seconds?.toFixed(2) }}秒</p><p>本次返回 {{ source.received ?? '未知' }} / {{ source.requested ?? '未知' }} · 源时间 {{ dateTime(source.source_at) }} · 落盘时间 {{ dateTime(source.persisted_at) }}</p><p class="ops-meta">最近采样 {{ dateTime(source.last_request_at) }}；请求返回覆盖不等于有效证券完整交付，历史记录不代表当前在线。</p></div><p>自动修复：{{ label(board.operations_publisher?.repair?.status) }}。明确缺口进入共享队列，执行完成后仍须独立复验。</p></section>
    <section class="ops-panel"><h2>异常与恢复记录</h2><p>异常先进入恢复窗口，超时后合并提醒；SMTP接受不代表收件人已收到或阅读。</p><el-empty v-if="!events.length" description="尚无已记录事件；这不代表所有数据已通过验收" />
      <div v-for="event in events" :key="event.key" class="ops-candidate"><div class="ops-toolbar"><strong>{{ event.detail.message || event.detail.reason || event.key }}</strong><el-tag :type="tone(event.status)">{{ label(event.status) }}</el-tag><el-tag type="info">{{ label(event.notification) }}</el-tag></div><p class="ops-meta">首次发现 {{ dateTime(event.first_seen) }} · 恢复窗口 {{ dateTime(event.deadline) }} · 发送尝试 {{ event.attempts }} · 恢复通知 {{ label(event.recovery_notification) }}</p><p v-if="event.error">{{ event.error }}</p></div>
    </section>
  </div>
</template>
<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { getTasks, getIncidents, label, tone, dateTime } from '@/utils/operations'
import '@/styles/operations.css'
const board=ref({}), events=ref([]), loading=ref(false), error=ref(''), filter=ref('all'), search=ref(''), scope=ref('core'), group=ref('delivery')
const metrics=computed(()=>[{label:'Windows已登记',value:board.value.summary?.registered_windows},{label:'运行中',value:board.value.summary?.running},{label:'最近执行失败',value:board.value.summary?.failed},{label:'历史 / 退役入口',value:board.value.summary?.retired}])
const recoveryStage=state=>({checking_source:'核验5分钟源数据',writing:'写入候选',validating:'校验候选与源数据',blocked:'已阻断，需排查'}[state]||state)
const recoveryPeriod=key=>{const match=/^(15|30|60):(\d{4})(\d{2})$/.exec(key);return match?`${match[2]}年${match[3]}月 · ${match[1]}分钟`:key}
const visibleGroups=computed(()=>(board.value.groups||[]).filter(x=>scope.value==='core'?!['external','retired'].includes(x.id):x.id===scope.value))
const groupRows=computed(()=>(board.value.tasks||[]).filter(x=>x.group===group.value))
const groupTitle=computed(()=>(board.value.groups||[]).find(x=>x.id===group.value)?.label||'后台任务')
function changeScope(){group.value=scope.value==='core'?'delivery':scope.value;filter.value='all';search.value=''}
const filtered=computed(()=>groupRows.value.filter(x=>(filter.value==='all'||(filter.value==='attention'?(['failed','unknown','not_observed'].includes(x.status)||['blocked','stale','degraded'].includes(x.business_status)):x.status===filter.value))&&`${x.name} ${x.title||''} ${x.purpose||''} ${x.produces||''}`.toLowerCase().includes(search.value.toLowerCase())))
async function load(){if(loading.value)return;loading.value=true;error.value='';try{const [tasks,incidents]=await Promise.all([getTasks(),getIncidents()]);board.value=tasks;events.value=incidents.incidents||[]}catch(e){error.value=e.response?.data?.detail||e.message}finally{loading.value=false}}
let timer
onMounted(()=>{load();timer=setInterval(()=>{if(!document.hidden)load()},10000)})
onUnmounted(()=>clearInterval(timer))
</script>
