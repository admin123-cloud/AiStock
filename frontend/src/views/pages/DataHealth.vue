<template>
  <div class="ops-page"><header class="ops-heading"><div><h1>数据健康</h1><p>按交易日检查应有数据，区分尚未到期、覆盖缺失与来源冲突。</p></div><div class="ops-toolbar"><el-select v-model="days" style="width:140px" aria-label="交易日范围" @change="load"><el-option :value="10" label="近10个交易日"/><el-option :value="30" label="近30个交易日"/><el-option :value="60" label="近60个交易日"/></el-select><el-button :loading="loading" @click="load">刷新验收</el-button></div></header>
    <div v-if="error" class="ops-error" role="alert">{{ error }}；没有将查询错误显示成零缺口。</div>
    <el-alert v-if="loading" title="正在按交易日核对唯一时间键" description="首次读取可能需要等待。保留上次结果及其验收时间。" type="info" :closable="false" />
    <section class="ops-panel"><h2>交易日交付矩阵</h2><p class="ops-meta">{{ calendar.scope || '等待独立覆盖验收，未用实际记录数充当应有数量。' }}</p><div class="ops-toolbar"><el-tag type="success">覆盖齐全</el-tag><el-tag type="warning">部分缺失 / 待验收</el-tag><el-tag type="danger">缺失 / 覆盖不足90%</el-tag><el-tag type="info">未知 / 未到期</el-tag></div>
      <div class="ops-scroll"><table class="ops-calendar"><thead><tr><th>数据集</th><th v-for="day in calendar.dates" :key="day">{{ day.slice(5) }}</th></tr></thead><tbody><tr v-for="dataset in calendar.datasets" :key="dataset.id"><th>{{ dataset.label }}</th><td v-for="cell in dataset.cells" :key="cell.date"><button class="ops-cell" :class="[cell.status,{severe:cell.coverage!=null&&cell.coverage<0.9}]" :aria-label="`${dataset.label} ${cell.date} ${label(cell.status)}`" @click="selected={...cell,dataset:dataset.label,error:dataset.error}">{{ label(cell.status) }}<small v-if="cell.coverage!=null">{{ (cell.coverage*100).toFixed(1) }}%</small></button></td></tr></tbody></table></div><p class="ops-meta">验收时间 {{ dateTime(calendar.generated_at) }} · 发布快照有效期15分钟，即时查询缓存5分钟。点击格子查看缺口与样例。</p>
    </section>
    <section v-if="selected" class="ops-panel" aria-live="polite"><h2>{{ selected.dataset }} · {{ selected.date }}</h2><el-tag :type="tone(selected.status)">{{ label(selected.status) }}</el-tag><div class="ops-metrics"><div class="ops-metric"><span>应有唯一键</span><strong>{{ selected.expected ?? '待定义' }}</strong></div><div class="ops-metric"><span>实际覆盖</span><strong>{{ selected.actual ?? '未知' }}</strong></div><div class="ops-metric"><span>缺失键</span><strong>{{ selected.missing ?? '未验收' }}</strong></div><div class="ops-metric"><span>有依据的业务豁免标的</span><strong>{{ selected.business_exceptions ?? '—' }}</strong></div></div><p v-if="selected.missing_codes_sample?.length">缺口标的（最多30只）：{{ selected.missing_codes_sample.join('、') }}</p><p v-if="selected.error">{{ selected.error }}</p></section>
  </div>
</template>
<script setup>
import {ref,onMounted} from 'vue'
import {getDataCalendar,label,tone,dateTime} from '@/utils/operations'
import '@/styles/operations.css'
const calendar=ref({}),selected=ref(null),days=ref(10),loading=ref(false),error=ref('')
async function load(){loading.value=true;error.value='';try{calendar.value=await getDataCalendar(days.value);selected.value=null}catch(e){error.value=e.response?.data?.detail||e.message}finally{loading.value=false}}
onMounted(load)
</script>
