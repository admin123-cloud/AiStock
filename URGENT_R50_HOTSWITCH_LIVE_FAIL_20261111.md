# URGENT_R50_HOTSWITCH_LIVE_FAIL_20261111（aistock-engineer → PM/Owner，2026-11-11 23:2x）

## 一句话

R50 Phase1 实机测绘受阻：Alt+1 账号热切换实机首测无效（12 轮 OCR 自证恒"广发证券-王*美" → switch_failed rc=4 零触碰）。测绘未发生，面板原状未动（王*美位、非最小化）。

## 事实链

1. 首跑 23:05 崩溃已修并提交 8c394a6：survey 曾给 hotswitch 传仅 handle 的假窗、缺生产窗口焦点调用面；现改用 bridge._find_window_for_panel（fills_export/poll 同源契约）+ 回归测试，l2_panel_survey 10 例 + ths_hotswitch_r49 18 例全过。
2. 修复后 23:10 实跑：前台无人校验通过，Alt+1 已发出，12 轮×0.6s OCR 顶栏恒"广发证券-王*美" → 按铁律 switch_failed 零采集零触碰。
3. 只读取证：无模态弹窗（0x8200b2 为底部悬浮迷你条）；顶栏账号切换器 = ComboBox aid=2322 (646,247)，value="广发证券-王*美"（旁：DropDown"打开"/添加(1691)/退出(1708)/登录(1709)）；关闭态列表项不暴露 → 曾*是否在登无法只读判定。
4. R49 Alt+N 绑定从未实机验证（ths_panels.json bound_at 15:56/16:00，客户端 15:11 已关；R49 记录自留"热切换首测顺延"）。

## 影响

- R50：panel_map.json 测绘等待账号就位（一条命令随时可跑）。
- R48/R49：明晨 09:35 巡读 acct2（王*美）走同一热切换 → 将 fail-closed 零采集（安全侧缺数据，非错读）；请 PM 知悉晨报可能缺 acct2。

## 待 Owner 裁决（三选一）

A.（推荐）手工点顶栏 ▼（"广发证券-王*美"右侧下拉）选曾*，回一句，我免参重跑一条命令完成测绘；
B. 授权以王*美位测绘：--account acct2（下单控件结构与账号无关）；
C. 授权 UIA 展开 ComboBox 探测在登账号清单（交互动作，超出 Phase1 零触碰边界，需明示批准）。

证据：runtime/l2_r50/journal_r50_phase1.md（23:0x-23:2x 段）、identity_check.png、panel_state_after_switch_failed.png、mini_floatbar_8200b2.png。
