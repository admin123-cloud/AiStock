import request from '@/utils/request'

export const getTasks = () => request.get('/operations/tasks')
export const getIncidents = () => request.get('/operations/incidents')
export const getDataCalendar = (days = 30) => request.get('/operations/data-calendar', { params: { days }, timeout: 120000 })
export const getMainwaveDaily = () => request.get('/operations/mainwave-daily')
export const labels = {
  backup_status_unavailable_or_stale: '备份状态缺失或已过期', backup_execution_failed: '最近备份执行失败',
  backup_success_not_verified: '尚无完成备份的证据', backup_older_than_25_hours: '超过25小时没有完成新备份',
  restore_drill_not_verified: '尚无隔离恢复验证证据', restore_drill_older_than_7_days: '隔离恢复验证已超过7天',
  host_archive_not_verified: '宿主归档尚未验收', backup_execution_unknown: '备份执行状态未知',
  backup_reports_degraded: '备份生产者报告验收未通过', isolated_restore_drill_required: '需要隔离恢复验证',

  configured_unverified: '已配置，发送尚未验收', configuration_incomplete: '通知配置不完整', failed_or_uncertain: '发送失败或结果不确定', queued: '已排队', awaiting_verification: '等待独立复验', not_requested: '尚未启用', not_required: '无需通知', retired: '已退役', external: '外部联动',
  ready: '启动成功', not_ready: '尚未就绪', starting: '启动中', stopped: '已停止',
  healthy: '正常', blocked: '阻断', degraded: '降级', stale: '已过期', deferred: '休市待刷新',
  unknown: '状态未知', not_observed: '未发现实例', disabled: '已停用', configured: '已配置 · 待核在线',
  running: '运行中', failed: '失败', idle: '待下次运行', never_run: '尚未运行',
  unverified: '待验收', not_due: '尚未到期', complete: '覆盖齐全', partial: '部分缺失', missing: '缺失',
  observing: '恢复窗口内', overdue: '恢复已超时', resolved: '已恢复', waiting: '等待恢复',
  smtp_accepted: 'SMTP已接受', sending: '发送中', data_blocked: '数据不可判断', confirmed: '已完成30m确认',
  waiting_30m: '等待30m确认', candidates: '候选跟踪中', no_candidates: '本批次无候选', stale_batch: '历史批次',
  candidate_observing: '门槛未达 · 继续观察',
}
export const label = value => labels[value] || value || '未知'
export const tone = value => ['blocked','failed','missing','overdue','data_blocked'].includes(value) ? 'danger' : ['healthy','complete','resolved','confirmed'].includes(value) ? 'success' : 'warning'
export const dateTime = value => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '暂无记录'
const reasons = {
  institutional_30m_source_conflict: '30分钟主表与阶段表存在来源冲突',
  data_gap_30m: '30分钟数据缺口，验收未通过',
  data_repair_failed: '自动数据修复未通过',
  accepted: '当日验收通过',
}
export const reasonText = value => reasons[value] ? `${value}（${reasons[value]}）` : value || '未提供原因'
