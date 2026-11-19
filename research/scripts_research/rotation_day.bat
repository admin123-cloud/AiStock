@echo off
setlocal
chcp 65001 >nul
set "ROOT=E:\AStockHistory\wstock"
set "RUN_OUT=%ROOT%\输出\轮转暂存\tail%1"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "RUN_ID=%%I"
set "PLAN_DIR=F:\AiStock\data\data\runtime\quality_plan\wstock_rotation_tail%1_%RUN_ID%"
cd /d F:\AiStock\app
F:\python3.10\python.exe -X utf8 scripts\research\wstock_rotation_day.py --tail %1 --source-dir "%ROOT%\原始压缩包\WDZ" --output-dir "%RUN_OUT%" --export-timeout 1800 >> scripts\research\rotation_%1.log 2>&1
if errorlevel 4 exit /b 4
if errorlevel 3 exit /b 3
if errorlevel 1 exit /b 1
F:\python3.10\python.exe -X utf8 scripts\research\wstock_consolidate_yearly.py --source-root "%RUN_OUT%" --source-root "%ROOT%\输出\按年" --output-root "%ROOT%\输出\按年" --manifest "%PLAN_DIR%\manifest.json" --conflicts-csv "%PLAN_DIR%\conflicts.csv" --quality-exceptions "%PLAN_DIR%\quality_exceptions.csv" --write >> scripts\research\rotation_%1.log 2>&1
if errorlevel 1 exit /b 1
F:\python3.10\python.exe -X utf8 scripts\research\stage_wstock_tail.py --tail %1 --wstock-out "%RUN_OUT%" --queue scripts\research\queue_items_20260918.json --output F:\AiStock\data\data\warehouse\wstock_restore_20260919\staged_tail%1.json >> scripts\research\rotation_%1.log 2>&1
if errorlevel 1 exit /b 1
F:\python3.10\python.exe -X utf8 scripts\research\apply_wstock_5m_gated.py --staged F:\AiStock\data\data\warehouse\wstock_restore_20260919\staged_tail%1.json --output F:\AiStock\data\data\warehouse\wstock_restore_20260919\applied_tail%1.json --yes >> scripts\research\rotation_%1.log 2>&1
if errorlevel 1 exit /b 1

F:\python3.10\python.exe -X utf8 scripts\research\verify_tail_48.py --staged F:\AiStock\data\data\warehouse\wstock_restore_20260919\staged_tail%1.json >> scripts\research\rotation_%1.log 2>&1
if errorlevel 1 exit /b 1
F:\python3.10\python.exe -X utf8 scripts\research\ledger_wstock_tail.py --tail %1 --staged F:\AiStock\data\data\warehouse\wstock_restore_20260919\staged_tail%1.json --applied F:\AiStock\data\data\warehouse\wstock_restore_20260919\applied_tail%1.json --queue scripts\research\queue_items_20260918.json --out-dir "%RUN_OUT%" --yes >> scripts\research\rotation_%1.log 2>&1
if errorlevel 1 exit /b 1
