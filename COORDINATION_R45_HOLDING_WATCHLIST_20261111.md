# COORDINATION — R45 G3 做T持仓股四项修复已上线（2026-11-11，aistock-engineer）

给并行会话（微信端/桌面端）的状态通知：R45 任务书（task_briefs/20261110_ttrade_watchlist_r45.md）四项已全部完成并部署，勿重复执行。

## 已完成（勿重复）
- 提交 `6e850d0`（main 已 push），`deploy-main.sh` DEPLOY_OK（backend/frontend 镜像同 SHA）。改动 10 文件 +1279/-59。
- **唯一放行写端点**（scoped-write，预览栈白名单 151→153 路径、矩阵 302→304 对）：
  `POST /api/gen3-state-alpha/holding-t/watchlist-upsert`、`POST /api/gen3-state-alpha/holding-t/watchlist-sync-ths`。
  其余一切 POST/PUT/DELETE 仍封禁；旧 PUT /holding-tick-watchlist 保持封禁（405）未动。
- **watchlist 文件迁移**：新 store `runtime/gen3_state_alpha/holding_t_watchlist/holding_tick_watchlist.json`
  （v2：items[{code,name,qty,source:manual|ths,added_at,updated_at}] + codes 兼容）；旧根下文件只读保留（sha256 未变）。
  消费方 `g3_holding_t_paper_monitor._watchlist_codes` 已改 store 优先读——盯盘覆盖面与页面同源。
- **compose 挂载**：`gen3_state_alpha` 仍整体 :ro，仅嵌套 `holding_t_watchlist` 子目录 rw（docker inspect 父 RW=false/子 RW=true）。
  broker_state.json 等其余文件容器不可写（线上实证未被触碰）。
- 合并语义（Owner 1.3）：THS 有→更新、THS 无→保留绝不删除（ths 降标 manual）、THS 新→追加、幂等零写；
  broker_state 无 updated_at → fail-closed。响应带 synced_at 与 diff。

## 注意
- 主工作区仍有另一会话（R41 收尾）未提交改动；本批提交只含上述 10 文件（git add 精确点名），
  全量 pytest 差量定责：干净基线 vs HEAD+本批 失败集 70=70 逐名全等，主工作区 r43-cat1 zstandard 失败系
  另一会话未提交改动所致，非本批引入。
- 验证全量证据：`F:/Aistock/data/data/runtime/ttrade_r45/VERIFICATION.md`（四项报障逐条浏览器+API 实测输出）。
- 环境遗留（非本批）：tests 5 个收集错误源于工作区已删除的 R41 脚本（test_daily_kline_unit_audit 等），待该会话收口。
