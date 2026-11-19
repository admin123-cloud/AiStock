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

> **⚠️ 2026-11-15 返修状态（独立复审 FAIL）**：上一任务未通过两位独立
> reviewer 复审，全自动读取链**未交付**。此前历史成交页面仅可见 22 行
> 数据；2026-10-21..11-12 只是行内可见日期区间，**不是**已核实的查询
> 范围。安全修复完成并通过 PM 独立复审前，**禁止直接重跑当前真实入口**
> （read-account --fills / export-fills 等一律先停）。下文既有资金链
> 与能力正文保留供追溯，未删除、未替换 stub。

## When to Use

- Owner 要求导出同花顺当日成交、读取持仓/资金、切换账号（曾⇄王）、检查客户端状态时。
- 15:05 成交导出 cron 失败排查、fills 缺口补跑时。
- 讨论双账号（曾/王）操作边界或身份门禁行为时。

项目代码：`F:/Aistock/app/scripts/ths_client.py`（R53 统一入口，八子命令；四旧脚本已降级为薄 wrapper）。运行环境：`F:/python/python3.10/python.exe`（有 pywinauto；F:/python3.10 没有——一字之差勿用错）。完整文档副本：`F:/Aistock/app/docs/ths_client.md`。

## 子命令

```bash
P=F:/python/python3.10/python.exe
$P F:/Aistock/app/scripts/ths_client.py login           # 启动链+登录态判定+凭证记住时自动点登录
$P F:/Aistock/app/scripts/ths_client.py export-fills    # 多账号成交导出（身份门禁+热切换+unavailable标记）
$P F:/Aistock/app/scripts/ths_client.py poll-holdings   # 盘中零交互持仓巡读单轮（schtask AiStock_THSHoldingsPoll）
$P F:/Aistock/app/scripts/ths_client.py survey          # 面板调查/账号绑定取证
$P F:/Aistock/app/scripts/ths_client.py status          # 一览：进程/面板/登录态/身份/最近产物
$P F:/Aistock/app/scripts/ths_client.py daily           # 15:05 全链：登录检查→导出→汇总（hermes cron 用）
$P F:/Aistock/app/scripts/ths_client.py read-account    # 远航版资金/持仓只读（详见下）
$P F:/Aistock/app/scripts/ths_client.py switch-account --to acct1  # 远航版切账号（详见下）
```

所有子命令输出 JSON envelope（schema ths_client/1，恒含 credentials_touched:false）。

## 远航版子命令（2026-11-14 Owner 裁决：能力代码进 ths_client.py 单文件）

目标客户端 `D:/同花顺远航/bin/happ.exe`（主程序，CEF）+ F12 拉起的 xiadan.exe
交易面板。自包含段（不经 services 经典面板链）；EasyTHS（MIT,
noimank/easyths 2.0.8）可用逻辑 vendored 并入并标注来源，运行时零外部包依赖。

### read-account [--account acct1|acct2] [--fills]（2026-11-15 起单账号链）

```bash
$P F:/Aistock/app/scripts/ths_client.py read-account                    # 读当前活动账号（资金+持仓）
$P F:/Aistock/app/scripts/ths_client.py read-account --account acct1    # 显式曾*；活动≠目标 → fail-closed rc4
$P F:/Aistock/app/scripts/ths_client.py read-account --account acct1 --fills  # 附加历史逐笔成交段
```

**单账号铁律（2026-11-15 任务书重构）**：新链**绝不切号**——活动账号≠目标即
`wrong_account_fail_closed` rc4（切号走显式 switch-account；旧"读取时自动切换"
能力原样保留在 `run_read_account_switchable` 供双账号阶段复用）。链路结构：
GUI 串行锁（`runtime/ths_client/yuanhang_window.lock`，msvcrt 跨进程互斥，
锁范围=身份核验+全部交互；锁忙 → `lock_busy` rc4 零交互）→ 采集器
`_yh_collect_single_snapshot` 取证 → 纯决策 `yh_single_snapshot` 裁决 →
（--fills 时）`yh_fills_verdict` 成交段裁决。

流程：确保 happ 运行（未运行 Popen 带 cwd）→ F12 拉起/枚举面板（每次重枚举防句柄失效）→
PrintWindow 截图 `runtime/ths_client/read_account_时间戳.png` + 视觉读顶栏脱敏账号
（A5\*\*\*\*1851=曾/acct1、A6\*\*\*\*5646=王/acct2）→ UIA 资金区交叉验证 → 身份门禁 →
UIA TreeItem 切资金股票页 → 读资金汇总（UIA 直读，实测区，截图项补缺口）→ 持仓行=
截图+视觉模型读取 → 交互后身份复核 →（--fills）TreeItem 切历史成交页（候选：历史成交/
历史成交查询/成交查询/交割单）→ 截图读行 + 日期/计数/分页令牌证据 → 成交页身份复核。
输出 `{account, masked_id, identity{before,after}, funds, holdings, holdings_headers,
fills{status,rows,date_range,coverage_complete,...}, snapshot_decision, coverage, ...}`。

状态语义（分段如实降级，绝不造数）：

- 顶层 `status`：`ok`（决策 ok 且 fills 段（若请求）ok）/ `partial`（身份已核实但
  某段证据不足：OCR 缺席、表头不全、空仓未证实、fills 日期/分页证据不足等，rc 仍 0）/
  门禁态（rc4：`not_logged_in_fail_closed`、`identity_gate_unreadable`、
  `wrong_account_fail_closed`、`lock_busy`、`identity_changed_fail_closed`）/ `failure`（rc1）。
- `snapshot_decision.reason`：决策函数给出的一手原因（identity_unreadable/ocr_unavailable/
  headers_incomplete/funds_incomplete/holdings_coverage_unproven/holdings_empty_unconfirmed…）。
- `fills.status`：`ok`（行有效且日期对+共N条=行数+单页+无下一页，或"共0条"证实空）/
  `partial`（行有效但 date_range_unproven/coverage_unproven/pagination_incomplete）/
  `unavailable`（未请求/Owner 活跃/页面未定位/wrong_page（委托页冒充）/empty_not_proven/
  fills_identity_unverified/身份中途变化）。
- 成交段证据铁律：日期范围只取查询页面实际令牌（8 位数字/分隔符两形态归一 ISO），
  绝不按当前日期猜测；空记录无"共0条"类零计数证据不得输出成功空结果；真实重复成交
  原样保留绝不静默去重；`fills.rows[]` 字段：date/time/side(buy|sell)/code/name/
  quantity/price/amount/fee/stamp_tax/transfer_fee/contract_no。
- `holdings_source`：`vision_model` / `ocr_local` / `ocr_unavailable`（holdings=[]）/
  `identity_gate_blocked`（身份不可读，数据不归属）。
- rc：0 ok/partial · 1 失败 · 4 门禁 fail-closed。
- Owner 活跃守卫（**每次注入前复核**）：Owner 在 THS 窗前台**或全局输入空闲<90s**
  （GetLastInputInfo，机器级；run3 实机教训：仅查 THS 前台会在 Owner 用其他窗口时
  抢焦点）→ 零输入模式（不 F12 不翻页，仅截图+UIA 读；fills 段跳过标
  `owner_active_zero_input`；探测值入 `input_idle_probes` 留证）。
- 兼容：`allow_switch`/`--no-switch` 关键字保留（API 兼容），新链默认即不切号。

### switch-account --to acct1|acct2

```bash
$P F:/Aistock/app/scripts/ths_client.py switch-account --to acct2          # 真实切换
$P F:/Aistock/app/scripts/ths_client.py switch-account --to acct2 --dry-run  # 只跑定位链零点击
$P F:/Aistock/app/scripts/ths_client.py switch-account --to acct2 --menu-config menu.json
```

流程：确保主程序运行并前台化（SetForegroundWindow 被前台锁挡时 SwitchToThisWindow 兜底）→
**Owner 活跃守卫**（THS 窗前台+空闲<90s → rc3 绝不点击）→ 登录页探测（UIA
Edit1001/Edit1012/Button1006；未登录 fail-closed rc4）→ 脱敏账号栏读当前账号（=目标
幂等 rc0 不触 GUI）→ 点顶栏账号下拉（坐标配置优先，视觉辅助定位「广发证券-王\*美▼」
等；横线变体 －—– 归一）→ 账号项定位（候选=配置标签>脱敏账号>完整标签；**绝不用单字
Owner 名**——实机曾误中消息流「3分钟快速开户Q曾…」）→ 切换后验证：脱敏栏==目标（主锚
rc0）/总资产跳变（弱锚 rc2 unverified fail-closed）→ 验证过才刷账号状态缓存。
rc：0 switched/幂等/dry_run_located · 1 失败 · 2 unverified · 3 owner_active ·
4 fail-closed（未登录/定位不到绝不盲点）。

坐标配置（可选）：`runtime/ths_client/yuanhang_menu.json` 或 env
`AISTOCK_THS_YUANHANG_MENU_JSON`，模板见 `runtime/ths_client/yuanhang_menu_template.json`。
点击安全：点前校验光标点下顶层窗确属主程序（被遮挡→临时 TOPMOST 抬升复验再点，点毕
还原；仍遮挡 fail-closed 拒点）。

## 账号与安全边界（铁律）

- 账号映射：160100087340=广发证券-曾\*(acct1，主 T 账号)、160100159121=广发证券-王\*美(acct2，数据港单票)；面板脱敏显示 A5\*\*\*\*1851=曾、A6\*\*\*\*5646=王。
- **凭证零接触**：密码只读长度位判「是否记住」，绝不读内容/绝不输入；远航版登录页探测只读 ControlType/AutomationId。
- **绝不触碰委托下单界面**：只触查询/导出菜单与交易菜单账号项；巡读零交互（PrintWindow 离屏 OCR）。
- 切换前检查 Owner 活跃（前台+GetLastInputInfo，自有注入剔除），Owner 在面板上绝不切。

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
- `not_logged_in_fail_closed`（远航版 read/switch）：F12 后无面板或停登录页 → 未登录 fail-closed。
- `identity_gate_unreadable/unknown_account`（远航版）：顶栏脱敏账号读不到/非注册账号 → 数据不归属（capture_path 留视觉复核）。
- `menu_not_located` / `menu_item_not_located`（远航版 switch）：定位链未命中 → 绝不盲点；填坐标配置或等视觉环境。
- `switch_unverified`（远航版）：脱敏栏读不到仅总资产跳变 → 弱锚 fail-closed 交人工复核。
- `owner_active`（远航版 switch）：Owner 在 THS 窗上 → 让位（rc3）。
- `click_target_covered`（远航版）：点击点被其他窗口遮挡且 TOPMOST 抬升无效 → 拒点。
- `zero_trades`：当日无成交（非故障）。
- 三态分诊：panel_absent（未绑定，预期内）≠ unavailable（真失败）≠ ok。

## 双客户端现状（2026-11-14）

- **经典版**（D:/同花顺）：做T成交/持仓主链路；登录框自绘 UIA 不可达；账号切换=R52 通道链（ComboBox 多锚+动作升级，热键通道判死保留）。
- **远航版**（D:/同花顺远航，bin/happ.exe）：UIA 全暴露（登录页 37/已登录面板 134 控件）；凭证持久化生效；F12 面板=MFC 壳（网上股票交易系统5.0，UIA Text 资金区可读+TreeItem 导航可点）；持仓表自绘 UIA 无行数据→PrintWindow+视觉/OCR；**账号切换=主程序顶栏账号下拉**（CEF，实拍「广发证券－王\*美▼」@(784,20)，非独立「交易」菜单），read-account/switch-account 已入本文件（EasyTHS vendored：funds 字段 auto_id 映射、holding 列名适配、账号切换幂等+验证后刷缓存、AccountState 模式）。2026-11-14 实机自测：read-account 全链 ok（acct2：资金 9 项+持仓 603881 数据港 600股 与资金区交叉一致）；switch 定位链+幂等+owner 守卫+fail-closed 各分支实测通过，完整点击链因 RDP 会话锁（SetCursorPos err5）未能闭环，待会话恢复后补验。
- 同一功能两版逻辑不同时，**先探测是哪版**（Path 含「远航」或主进程 happ.exe）再分支。

## 交付纪律

- 写库/写文件结果必须只读回验后再报完成；不造数；fills/holdings 缺口如实记 unavailable。
- 变更 ths_client.py 后：pytest THS 套件全绿（2026-11-14 起含 test_ths_client_yuanhang.py 35 例）+ 同步两份 wrapper 副本（hermes/scripts 与 runtime/t_tracker 如有）+ 更新本文档。
