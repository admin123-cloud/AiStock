<template>
  <div class="ops-page"><header class="ops-heading"><div><h1>主升每日跟踪</h1><p>先判断数据是否可信，再看候选推进与每日验收。</p></div><el-button :loading="loading" @click="load">刷新跟踪</el-button></header>
    <div v-if="error" class="ops-error" role="alert">{{ error }}；当前结果未更新。</div>
    <p class="ops-meta">批次校验：{{ data.batch?.ok?'完整批次已验证':'未通过 / 旧产物待重新发布' }} · {{ data.batch?.batch_id || '暂无批次号' }}</p>
    <el-alert :type="tone(data.state)==='danger'?'error':tone(data.state)" :title="headline" :closable="false" show-icon><p>{{ explanation }}</p><p>执行日 {{ data.entry_date || '待确认' }} · 决策日 {{ data.decision_date || '待确认' }} · 信号批次 {{ dateTime(data.source_generated_at) }}</p></el-alert>
    <div class="ops-metrics"><div class="ops-metric"><span>主升候选</span><strong>{{ data.summary?.candidate_count ?? '—' }}</strong></div><div class="ops-metric"><span>数据不可判断</span><strong>{{ data.summary?.data_blocked_count ?? '—' }}</strong></div><div class="ops-metric"><span>30m已确认</span><strong>{{ data.summary?.confirmed_count ?? '—' }}</strong></div><div class="ops-metric"><span>最近观察通过</span><strong>{{ data.summary?.accepted_days ?? '—' }} / {{ data.summary?.observed_days ?? '—' }}</strong></div></div>
    <section v-if="blockedChecks.length" class="ops-panel"><h2>今天先处理这些问题</h2><div v-for="check in blockedChecks" :key="check.name" class="ops-candidate"><strong>{{ check.message }}</strong><p class="ops-meta">{{ check.name }}</p></div></section>
    <section class="ops-panel"><div class="ops-toolbar"><h2>候选进展</h2><el-radio-group v-model="filter"><el-radio-button value="all">全部</el-radio-button><el-radio-button value="data_blocked">数据阻断</el-radio-button><el-radio-button value="waiting_30m">待确认</el-radio-button><el-radio-button value="confirmed">已确认</el-radio-button></el-radio-group></div>
      <div class="ops-scroll"><el-table :data="candidates" stripe empty-text="本批次无可展示候选；请先查看上方数据状态" @row-click="selected=$event">
        <el-table-column label="股票" min-width="155"><template #default="{row}"><el-button link type="primary" @click.stop="selected=row">{{ row.name || row.code }}</el-button><div class="ops-meta">{{ row.code }}</div></template></el-table-column>
        <el-table-column prop="industry" label="所属行业" min-width="130"/>
        <el-table-column label="当前阶段" min-width="160"><template #default="{row}"><el-tag :type="tone(row.stage)">{{ label(row.stage) }}</el-tag></template></el-table-column>
        <el-table-column label="主升分 / 门槛" width="140"><template #default="{row}">{{ score(row.score) }} / {{ data.contract?.entry?.minimum_score }}</template></el-table-column>
        <el-table-column prop="resonance" label="同业共振" width="100"/><el-table-column label="阻断原因" min-width="280" show-overflow-tooltip><template #default="{row}">{{ reasonText(row.reason) }}</template></el-table-column>
      </el-table></div>
    </section>
    <section class="ops-panel"><h2>较上个有效交易日的变化</h2><p v-if="data.comparison_status==='data_blocked'">当前数据未通过，不把异常造成的候选消失解释为策略退出。</p><p v-else-if="!data.comparison_date">尚无同合同的有效比较基线。发布器将开始记录每日状态。</p><template v-else><p>比较基线 {{ data.comparison_date }}</p><p v-if="!data.changes?.length">本批次未发现阶段变化。</p><p v-for="change in data.changes" :key="change.code">{{ change.name || change.code }}：{{ change.before ? label(change.before) : '新进入' }} → {{ change.after ? label(change.after) : '退出候选' }}</p></template></section>
    <section class="ops-panel"><h2>最近每日验收</h2><p>数据冲突日与正常空仓日分开；通过天数只计当前合同。</p><div class="ops-scroll"><table class="ops-calendar"><tbody><tr><td v-for="day in data.history" :key="day.date"><button class="ops-cell" :class="day.accepted?'complete':'missing'" :title="`${day.date} ${day.reason}`" @click="historyDay=day">{{ day.date?.slice(5) }}<small>{{ day.accepted?'通过':'阻断' }}</small></button></td></tr></tbody></table></div><p v-if="historyDay" aria-live="polite">{{ historyDay.date }}：{{ reasonText(historyDay.reason) }}；分钟修复{{ historyDay.repair_triggered?'已触发':'未触发' }}。</p></section>
    <section class="ops-panel"><h2>当前合同</h2><p>主升分 ≥ {{ data.contract?.entry?.minimum_score }} · 同业共振 ≥ {{ data.contract?.entry?.minimum_same_day_industry_mainwave_count }} · 指数60日涨幅 ≤ {{ ((data.contract?.entry?.index_mom60_max||0)*100).toFixed(0) }}% · 已完成30m放量突破前20根高点</p><p class="ops-meta">{{ data.contract_metadata?.strategy_id }} · {{ data.contract_metadata?.sha256?.slice(0,12) }} · 影子观察，正式买点和自动下单保持锁定。</p></section>
    <el-drawer v-model="drawer" title="候选证据" size="min(520px, 95vw)"><template v-if="selected"><h2>{{ selected.name }} {{ selected.code }}</h2><el-tag :type="tone(selected.stage)">{{ label(selected.stage) }}</el-tag><p>所属行业：{{ selected.industry }}（{{ selected.industry_source || '来源待确认' }}）</p><p>主升分：{{ score(selected.score) }}；同业共振：{{ selected.resonance }}</p><p>30m来源：{{ selected.m30_source || '未知' }}；冲突行数：{{ selected.conflict_rows || 0 }}</p><p>确认时间：{{ selected.confirmation_time || '尚未确认' }}</p><p>原因：{{ reasonText(selected.reason) }}</p><p>参考价格：{{ selected.reference_price || '未提供' }}</p><p>执行日 {{ selected.entry_date }}；决策日 {{ selected.decision_date }}</p></template></el-drawer>
  </div>
</template>
<script setup>
import {ref,computed,onMounted} from 'vue'
import {getMainwaveDaily,label,tone,dateTime,reasonText} from '@/utils/operations'
import '@/styles/operations.css'
const data=ref({}),loading=ref(false),error=ref(''),filter=ref('all'),selected=ref(null),historyDay=ref(null)
const drawer=computed({get:()=>!!selected.value,set:v=>{if(!v)selected.value=null}})
const headline=computed(()=>loading.value&&!data.value.state?'正在读取当日证据':label(data.value.state))
const explanation=computed(()=>data.value.state==='data_blocked'?'当前数据不足以形成可信买入结论，候选为0不等于今天没有机会。':data.value.stale_batch?'当前显示历史批次，不作为今天的新买点。':'按当前合同跟踪候选；确认结果仍需通过账户与执行安全检查。')
const blockedChecks=computed(()=>(data.value.checks||[]).filter(x=>!x.ok))
const candidates=computed(()=>(data.value.candidates||[]).filter(x=>filter.value==='all'||x.stage===filter.value))
const score=v=>v!=null&&v!==''&&Number.isFinite(Number(v))?Number(v).toFixed(1):'待评估'
async function load(){loading.value=true;error.value='';try{data.value=await getMainwaveDaily()}catch(e){error.value=e.response?.data?.detail||e.message}finally{loading.value=false}}
onMounted(load)
</script>
