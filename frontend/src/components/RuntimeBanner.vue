<template><div class="runtime-banner" :class="blocked?'attention':'healthy'" role="status"><strong>{{ health?.preview?'只读快照预览 · ':'' }}{{ title }}</strong><span>{{ detail }}</span></div></template>
<script setup>
import {ref,computed,onMounted,onBeforeUnmount} from 'vue'
import request from '@/utils/request'
const health=ref(null)
let timer
const blocked=computed(()=>!health.value?.strategy_actionable)
const title=computed(()=>health.value==null?'数据状态待确认':blocked.value?'数据交付未通过':'数据交付检查通过')
const detail=computed(()=>health.value?.publisher_status==='stale'?'健康发布已过期，不能沿用上次结论':(health.value?.components||[]).filter(x=>!['healthy','deferred'].includes(x.status)).map(x=>({daily_kline_coverage:'日线覆盖',qmt_after_close_validation:'分钟盘后验收',g3_strategy_summary:'G3摘要',broker_snapshot:'账户快照'}[x.name]||x.name)).join('、')||'详细验收见左侧数据健康；策略准入另行检查')
async function load(){try{health.value=await request.get('/health/runtime',{skipErrorHandler:true})}catch{health.value=null}}
onMounted(()=>{load();timer=setInterval(load,60000)})
onBeforeUnmount(()=>clearInterval(timer))
</script>
<style scoped>
.runtime-banner{padding:10px 20px;display:flex;gap:12px;flex-wrap:wrap;font-size:13px;line-height:1.5;border-bottom:1px solid #e2e8f0}.runtime-banner strong{flex-shrink:0}.attention{background:#fff7ed;color:#9a3412}.healthy{background:#f0fdf4;color:#166534}
</style>
