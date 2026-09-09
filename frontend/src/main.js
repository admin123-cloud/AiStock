import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { ElAlert, ElAutocomplete, ElButton, ElCheckbox, ElCol, ElDatePicker, ElDialog, ElDrawer, ElEmpty, ElForm, ElFormItem, ElIcon, ElInput, ElInputNumber, ElOption, ElPagination, ElProgress, ElRadioButton, ElRadioGroup, ElRow, ElSegmented, ElSelect, ElSwitch, ElTabPane, ElTable, ElTableColumn, ElTabs, ElTag, ElLoading } from 'element-plus'
import 'element-plus/dist/index.css'
import { DocumentCopy } from '@element-plus/icons-vue'
import App from './App.vue'
import router from './router'

const app = createApp(App)
app.component('DocumentCopy', DocumentCopy)
for (const component of [ElAlert, ElAutocomplete, ElButton, ElCheckbox, ElCol, ElDatePicker, ElDialog, ElDrawer, ElEmpty, ElForm, ElFormItem, ElIcon, ElInput, ElInputNumber, ElOption, ElPagination, ElProgress, ElRadioButton, ElRadioGroup, ElRow, ElSegmented, ElSelect, ElSwitch, ElTabPane, ElTable, ElTableColumn, ElTabs, ElTag, ElLoading]) app.use(component)
app.use(createPinia())
app.use(router)
app.mount('#app')
