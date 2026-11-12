# COORDINATION R47 同花顺多账号接入（2026-11-11，aistock-engineer）——已完成已部署

## 状态：已合并已部署
- 提交链：30d858b（多账号地基）→ 40c3155（union_holdings+watchlist 并集）→ c89227e（引擎票池分账号展开）
  （rebase 吸收 R46=6a9a399 后三连，main=c89227e，DEPLOY_OK 11:44 容器切换，health 200×3）
- 最终改动足迹：services/ths_account_bridge.py、services/broker_state_autosync.py、
  services/t_trade_signal.py（票池+草案 account，rebase 后落地无冲突）、api/gen3_state_alpha.py、
  scripts/{ths_fills_export,broker_state_sync,t_trade_tracker}.py、tests/×5 新增；pipeline_scheduler.py 零改动
- 测试：全套件 218 passed（含 R46 fast-lane/R45 AST 守卫）；preview 安全矩阵 21（隔离进程）；新增 35 例
- KB：changes/2026-11-11-ths-multiaccount-r47.md + CURRENT + LEARNINGS（push b5e8922）

## 交接给今晚值守（PM/后续会话）
1. 15:05 host cron（新代码）：预期 fills/20261111.json + 20261111_acct1.json 全等镜像；acct2=panel_absent
2. 15:10 reconcile：legacy 台账照旧（无 acct2 fills 不产生 _acct2/merged 文件）
3. 15:15 broker_state_sync：acct1 正常、acct2=not_configured（rc=0）
- 若 15:05 出现 unavailable/异常：查 runtime/t_tracker/fills/ 标记与 PM cron 日志，勿手工造数

## 账号2 面板到场后的接入手顺（已备好，等 Owner）
1. Owner 打开账号2交易面板（券商待告知）→ 2. 只读 OCR 取证（券商/尾号）→ 3. Owner/PM 确认 →
4. ths_panels.json bind（acct2 规则）→ 5. 首次 broker_state_sync --account acct2 --init-base <锚点> →
6. 此后 15:05/15:10/15:15 全自动双账号；引擎票池自动纳入（无需改码）
- 铁律不变：只触查询/导出菜单，绝不打开任何下单界面

## 取证结论（11-11 上午，只读，证据 runtime/ths_holdings_r47/forensic/）
- xiadan-plus.exe 14864 = xiadan.exe 37292 的子进程（ParentProcessId 实证），仅 NVOpenGLPbuffer 隐藏窗，无交易 UI —— PM 侦察"账号2疑似"不成立
- 全系统唯一交易面板 = "网上股票交易系统5.0"（广发-曾*，acct1，8890729932）
- hexin.exe 主端登录同为广发-曾*；账号2（数据港 603881）当日无任何已登录面板 → 首读阻塞，待 Owner 开面板
- URGENT：runtime/URGENT_R47_THS_ACCOUNT2_PANEL_ABSENT_20261111.md（需 Owner 答：账号2券商/客户端）

## 会话痕迹
- worktree F:/Aistock/app-r47（分支已合并，暂留今晚核验备用，明日可 git worktree remove）
- 报告：runtime/ths_holdings_r47/REPORT_MIDDAY.md + FINAL.md + forensic/
