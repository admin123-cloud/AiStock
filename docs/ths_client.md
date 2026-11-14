---
name: ths-client
description: 同花顺客户端操作统一入口。导出成交、读持仓资金、切账号、查状态时使用。
version: 1.0.0
author: Hermes Agent (user-directed)
license: MIT
platforms: [windows]
metadata:
  hermes:
    tags: [AiStock, THS, client, fills]
    related_skills: [aistock-ths-ttrading, daily-tdx-postclose, pm-task-dispatch]
---

# THS Client（同花顺客户端统一入口）

## When to Use

- Owner 要求导出同花顺当日成交、读取持仓/资金、切换账号（曾⇄王）、检查客户端状态时。
- 15:05 成交导出 cron 失败排查、fills 缺口补跑时。
- 讨论双账号（曾/王）操作边界或身份门禁行为时。

项目代码：`F:/Aistock/app/scripts/ths_client.py`（R53 统一入口，六子命令；四旧脚本已降级为薄 wrapper）。运行环境：`F:/python/python3.10/python.exe`（有 pywinauto；F:/python3.10 没有——一字之差勿用错）。完整文档副本：`F:/Aistock/app/docs/ths_client.md`。

## 子命令

```bash
P=F:/python/python3.10/python.exe
$P F:/Aistock/app/scripts/ths_client.py login           # 启动链+登录态判定+凭证记住时自动点登录
$P F:/Aistock/app/scripts/ths_client.py export-fills    # 多账号成交导出（身份门禁+热切换+unavailable标记）
$P F:/Aistock/app/scripts/ths_client.py poll-holdings   # 盘中零交互持仓巡读单轮（schtask AiStock_THSHoldingsPoll）
$P F:/Aistock/app/scripts/ths_client.py survey          # 面板调查/账号绑定取证
$P F:/Aistock/app/scripts/ths_client.py status          # 一览：进程/面板/登录态/身份/最近产物
$P F:/Aistock/app/scripts/ths_client.py daily           # 15:05 全链：登录检查→导出→汇总（hermes cron 用）
```

所有子命令输出 JSON envelope（schema ths_client/1，恒含 credentials_touched:false）。

## 账号与安全边界（铁律）

- 账号映射：160100087340=广发证券-曾\*(acct1，主 T 账号)、160100159121=广发证券-王\*美(acct2，数据港单票)；面板脱敏显示 A5\*\*\*\*1851=曾、A6\*\*\*\*5646=王。
- **凭证零接触**：密码只读长度位判「是否记住」，绝不读内容/绝不输入；密码为空 → need_manual_login fail-closed 交 Owner。
- **绝不触碰委托下单界面**：只触查询/导出菜单；巡读零交互（PrintWindow 离屏 OCR）。
- 切换前检查 Owner 活跃（前台+GetLastInputInfo），Owner 在面板上绝不切。

## 触发链与排程

- 15:05 hermes cron「THS成交导出」→ `hermes/scripts/ths_fills_daily.py` → `ths_client.py daily`（PYTHONPATH 剥离防 win32api DLL 污染）。
- schtask `AiStock_THSHoldingsPoll`（盘中巡读，现 Disabled 待 Owner 批准启用）。
- 15:10 容器 `t_trade_reconcile` 消费 fills；15:15 `broker_state_sync` 更新 broker_state.json。

## 失败形态分诊（reason 字段）

- `need_manual_login`：登录框密码空 → 请 Owner 手输（唯一人工步骤）。
- `ProcessNotFoundError xiadan.exe`：客户端未启动 → 先跑 login。
- `NO_XIADAN_WINDOW`：窗口隐藏/最小化（pywinauto UIA 看不到隐藏窗）→ 还原面板重试。
- `PANEL_IDENTITY_MISMATCH`：活动账号与预期不符 → 热切换（R52 通道链）或人工核对。
- `PANEL_LOGIN_DIALOG`：远航版面板停登录框 → Owner 登录一次（凭证持久化后冷启动全自动）。
- `zero_trades`：当日无成交（非故障）。
- 三态分诊：panel_absent（未绑定，预期内）≠ unavailable（真失败）≠ ok。

## 双客户端现状（2026-11-14）

- **经典版**（D:/同花顺）：做T成交/持仓主链路；登录框自绘 UIA 不可达；账号切换=R52 通道链（ComboBox 多锚+动作升级，热键通道判死保留）。
- **远航版**（D:/同花顺远航，bin/happ.exe）：UIA 全暴露（登录页 37/已登录面板 134 控件，auto_id 精确：资金帐户 1001/交易密码 1012/登录 1006）；凭证持久化生效；账号切换=主程序「交易」菜单点账号（免登录，F12 开最近账号）。EasyTHS 试点待做（REST holding_query/funds_query）。
- 同一功能两版逻辑不同时，**先探测是哪版**（Path 含「远航」）再分支。

## 交付纪律

- 写库/写文件结果必须只读回验后再报完成；不造数；fills/holdings 缺口如实记 unavailable。
- 变更 ths_client.py 后：pytest THS 套件全绿 + 同步两份 wrapper 副本（hermes/scripts 与 runtime/t_tracker 如有）+ 更新本文档。
