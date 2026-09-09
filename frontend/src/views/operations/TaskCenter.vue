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
    <el-alert v-if="board.operations_publisher?.publisher_status!=='healthy'||!board.operations_publisher?.notifications_enabled" title="独立数据告警尚未接管或心跳已过期" description="此处记录异常不等于已发送通知；上线需同时部署通知发布器。原G3通知仅在自身运行窗口内兜底。" type="warning" :closable="false" show-icon />
    <el-alert v-if="board.host_inventory_status !== 'healthy'" title="Windows任务清单尚未发布或已过期" description="未把缺少心跳解释为任务正常。请检查 Operations Publisher 的运行状态。" type="warning" :closable="false" show-icon />
    <div class="ops-metrics"><div v-for="item in metrics" :key="item.label" class="ops-metric"><span>{{ item.label }}</span><strong>{{ item.value ?? '—' }}</strong></div></div>
    <section class="ops-panel"><div class="ops-toolbar"><h2>后台任务</h2><el-radio-group v-model="filter"><el-radio-button value="all">全部</el-radio-button><el-radio-button value="attention">需处理</el-radio-button><el-radio-button value="running">运行中</el-radio-button><el-radio-button value="disabled">已停用</el-radio-button></el-radio-group><el-input v-model="search" placeholder="搜索任务或产物" aria-label="搜索任务或产物" clearable style="max-width:260px" /></div>
      <div class="ops-scroll"><el-table :data="filtered" row-key="name" stripe empty-text="尚无任务清单">
        <el-table-column type="expand"><template #default="{row}"><div class="ops-detail" style="padding:16px"><div><strong>执行责任</strong><p>{{ row.description || row.note || row.owner || '详细用途待登记' }}</p><p>执行窗口：{{ row.window || '以宿主机计划为准' }}</p><p>最近成功：{{ dateTime(row.last_success) }}</p><p>最近完成：{{ dateTime(row.last_finished) }} · 耗时 {{ row.duration_seconds ?? '—' }} 秒</p><p>本进程执行 {{ row.runs ?? '—' }} 次 · 失败 {{ row.failures ?? '—' }} 次</p><p v-if="row.last_schedule_warning">{{ row.last_schedule_warning }} · {{ dateTime(row.last_warning_at) }}</p></div><div><strong>业务交付</strong><p>{{ row.artifact || '尚未绑定验收产物' }}</p><p>{{ row.business_reason || '执行完成不代表数据验收通过' }}</p><p v-if="row.error" role="alert">{{ row.error }}</p></div></div></template></el-table-column>
        <el-table-column prop="name" label="任务 / 用途" min-width="300" /><el-table-column prop="executor" label="执行器" width="95" />
        <el-table-column label="运行状态" width="165"><template #default="{row}"><el-tag :type="tone(error?'unknown':row.status)">{{ label(error?'unknown':row.status) }}</el-tag><small v-if="row.scheduler_paused"> 调度已暂停</small><small v-if="row.status==='running'&&row.running_seconds!=null"> {{ row.running_seconds }}秒</small></template></el-table-column>
        <el-table-column label="业务验收" width="150"><template #default="{row}">{{ label(row.business_status) }}</template></el-table-column>
        <el-table-column label="最近执行" width="180"><template #default="{row}">{{ dateTime(row.last_run) }}</template></el-table-column>
        <el-table-column label="下次执行" width="180"><template #default="{row}">{{ dateTime(row.next_run) }}</template></el-table-column>
        <el-table-column prop="last_result" label="退出码" width="105" />
      </el-table></div><p class="ops-meta">宿主机清单：{{ dateTime(board.host_inventory_at) }}。展开任务可查看失败原因、最近成功和耗时；退出码与业务结果分别展示。</p>
    </section>
    <section class="ops-panel"><h2>异常与恢复记录</h2><p>异常先进入恢复窗口，超时后合并提醒；SMTP接受不代表收件人已收到或阅读。</p><el-empty v-if="!events.length" description="尚无已记录事件；这不代表所有数据已通过验收" />
      <div v-for="event in events" :key="event.key" class="ops-candidate"><div class="ops-toolbar"><strong>{{ event.detail.message || event.detail.reason || event.key }}</strong><el-tag :type="tone(event.status)">{{ label(event.status) }}</el-tag><el-tag type="info">{{ label(event.notification) }}</el-tag></div><p class="ops-meta">首次发现 {{ dateTime(event.first_seen) }} · 恢复窗口 {{ dateTime(event.deadline) }} · 发送尝试 {{ event.attempts }}</p><p v-if="event.error">{{ event.error }}</p></div>
    </section>
  </div>
</template>
<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { getTasks, getIncidents, label, tone, dateTime } from '@/utils/operations'
import '@/styles/operations.css'
const board=ref({}), events=ref([]), loading=ref(false), error=ref(''), filter=ref('all'), search=ref('')
const metrics=computed(()=>[{label:'已登记任务',value:board.value.summary?.total},{label:'运行中',value:board.value.summary?.running},{label:'最近执行失败',value:board.value.summary?.failed},{label:'状态未知',value:board.value.summary?.unknown}])
const filtered=computed(()=>(board.value.tasks||[]).filter(x=>(filter.value==='all'||(filter.value==='attention'?(['failed','unknown','not_observed'].includes(x.status)||['blocked','stale','degraded'].includes(x.business_status)):x.status===filter.value))&&`${x.name} ${x.artifact||''}`.toLowerCase().includes(search.value.toLowerCase())))
async function load(){if(loading.value)return;loading.value=true;error.value='';try{const [tasks,incidents]=await Promise.all([getTasks(),getIncidents()]);board.value=tasks;events.value=incidents.incidents||[]}catch(e){error.value=e.response?.data?.detail||e.message}finally{loading.value=false}}
let timer
onMounted(()=>{load();timer=setInterval(()=>{if(!document.hidden)load()},10000)})
onUnmounted(()=>clearInterval(timer))
</script>
