# COORDINATION R49 acct2 绑定 + Alt 热切换双账号（2026-11-11，aistock-engineer）——代码已部署，基线落盘顺延明晚

## 终态：main=242243c（push，DEPLOY_OK health 200×3）
- 提交链：62c18a6（R49 主体）→ 242243c（R49b 最小化面板解析+恢复后读身份）
- 改动：services/{ths_hotswitch(新),ths_account_bridge,ths_holdings_poll,broker_state_autosync}、
  scripts/{ths_holdings_poll,ths_fills_export,ths_panels_bind(新)}、tests/test_ths_hotswitch_r49(新)+autosync 语义更新
- 他会话在 main 树的未提交改动零触碰（ff merge 只动上述文件）

## 运行态变化（明日自动生效）
1. ths_panels.json 已绑定 acct1(曾,Alt+1,常态位)/acct2(王,Alt+2)——热切换组共享单面板，锁统一 ths_window.lock
2. 巡读 09:35-15:00 双账号（曾→切王读王→切回曾；最小化/前台轮如实让位）
3. 15:05 fills 双账号导出；15:15 broker_state_sync acct2 在基线落盘前会 base_missing 拒绝（fail-closed 正常）
4. 基线一条命令（15:05 后人工一次）：见 runtime/URGENT_R49_ACCT2_BOOTSTRAP_TOMORROW_20261111.md 或 ths_holdings_r49/RUNBOOK_20261111.md

## 今晚实况
Owner 15:11 后关闭交易客户端 → 盘后双账号实跑/热切换首测顺延（fail-closed，绝不造数）。
acct2 首读数字已由 12:18 取证快照实证（603881 600股@33.993，资金面见 URGENT）。
契约数字已入 l2_risk_config（硬线=MA10 20.288 取近者；卖带 21.80-22.31）——PM 需向 Owner 过目。

## 会话痕迹
- worktree F:/Aistock/app-r47/r48 仍在（R47 惯例：留一日核验，明日可 remove，连同 app-r49）
- KB：changes/2026-11-11-acct2-hotswitch-r49.md
