# 注册 AiStock G3 盈利闭环定时任务（Windows 任务计划程序，周一至周五交易日门禁保护）
$ErrorActionPreference = "Stop"

$PythonExe = "F:\python3.10\python.exe"
$ScriptPath = "F:\Aistock\code\scripts\g3_profit_loop_job.py"
$WorkDir = "F:\Aistock\code"

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

# 1. 每个工作日 09:15：生成盘前市场主线简报并推送微信（非交易日脚本内自动跳过）
$ActionPremarket = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$ScriptPath`" --mode premarket" `
    -WorkingDirectory $WorkDir

$TriggerPremarket = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "09:15"

Register-ScheduledTask `
    -TaskName "AiStock_G3_Premarket_Brief_0915" `
    -Description "AiStock 盈利闭环 Phase 1-3：每个交易日 09:15 生成盘前市场主线简报并推送微信（非交易日自动跳过）" `
    -Action $ActionPremarket `
    -Trigger $TriggerPremarket `
    -Settings $Settings `
    -Force | Out-Null

# 2. 每个工作日 17:45（17:30 双账号同步后）：刷新主升候选导出供 Agent 排雷 + 刷新四维归因与交易教练
$ActionPostclose = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$ScriptPath`" --mode postclose" `
    -WorkingDirectory $WorkDir

$TriggerPostclose = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "17:45"

Register-ScheduledTask `
    -TaskName "AiStock_G3_Postclose_Loop_1745" `
    -Description "AiStock 盈利闭环 Phase 1-3：每个交易日 17:45 刷新主升候选池、导出 AI 排雷上下文、预热四维归因与交易教练" `
    -Action $ActionPostclose `
    -Trigger $TriggerPostclose `
    -Settings $Settings `
    -Force | Out-Null

Get-ScheduledTask -TaskName "AiStock_G3_Premarket_Brief_0915", "AiStock_G3_Postclose_Loop_1745" |
    Select-Object TaskName, State, @{N="NextRunTime";E={(Get-ScheduledTaskInfo -TaskName $_.TaskName).NextRunTime}} |
    Format-Table -AutoSize
