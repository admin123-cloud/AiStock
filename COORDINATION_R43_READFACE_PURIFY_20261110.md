# COORDINATION — R43 封禁 GET 批量净化（35 条）已完成、已部署

写给：正在 F:/Aistock/app 共享树工作的并行会话
来自：aistock-engineer（R43，2026-11-10 23:55 收口；本文替代早前"进行中"版本）

## 终态

1. **origin/main = `0fc791e`**（R43 五连提交 rebase 于 R40/R42 之上：6b5eed9→d7f7fa8→02a88d3→e70d27e→0fc791e），**已部署 DEPLOY_OK**（官方 deploy-main.sh；共享树已 ff-only 前移到 0fc791e，你的 42 项未提交改动原样未动、未参与构建）。
2. 范围=R30 封禁 39 条中的 35 条（#14 归 R42 已合入并被本批 rebase 吸收；⑤类 sync-ths 3 条仍封 404 待批转 POST）。allowlist 116→**151 路径**，矩阵 232→**302 对**；35 条 GET 实测 35/35=200，POST 兄弟/OPTIONS 405，仍封 3 条 404。
3. 撞线文件：api/gen3_state_alpha.py、api/gen3_shadow.py、api/trading.py、api/system_config.py、api/operations_preview.py、tests/test_operations_preview_safety.py、tests/test_r43_readface_purify.py（新）。你后续若从旧 main 分出分支，rebase 到 0fc791e 即可。
4. 撞线要点：`_read_or_run_*` 两网关仅剩 POST 引用（snapshot/execute-next），GET 链全部走 `_read_*_cached` 只读孪生；调度器 status GET 全部 `_scheduler_state_readonly`/只读孪生；`_notification_config_status` 已无 SMTP 登录探测；daily-ticket 构建拆出 `_build_gen2_daily_trade_ticket_payload` 纯读核心；13 个 live-launch/strategy-tuning GET 有 `_live_chain_artifact_gate` 缺件门（缺件→not_ready，绝不自动跑）。
5. 实测期事件：sectors N+1 家族（R30 遗留缺陷）在 151 路径扫描时把 CH 打出 MEMORY_LIMIT_EXCEEDED 并拖垮 preview 后端（手动 restart 一次恢复）——与 R43 端点无关（全文件/CSV 读），但建议尽快另立任务修 sectors。

交付：`F:/Aistock/data/data/runtime/readface_purify_r43/`（REPORT.md 含 35 条逐条处置表/两轮全量差量对证/31 页走查/事件披露）。
