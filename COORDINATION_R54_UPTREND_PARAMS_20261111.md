# COORDINATION_R54：上升周期 T 参数已切换 75/80 + 35/20（已部署）

致微信端 PM（跨会话通知，聊天互不可见）。执行：aistock-engineer，2026-11-11 16:2x。

1. **参数已按 Owner 裁决上线**：`e31dd78` 已 push origin/main 并 `DEPLOY_OK`（容器实证 v1.3.0）。**生效时点 11-12 09:25 起**（部署时已收盘，无盘中切换）。报告 `runtime/ttrade_r54/REPORT.md`。
2. **上升周期新语义（横盘/下降零改动）**：
   - 卖侧：RSI5m≥75 卖 1/2（草案 subtype=`trim`）→ ≥80 清余仓。你侧 RSI≥80 硬底线提醒与清仓线天然对齐（同一根线）。
   - 买侧：RSI5m≤35 常规买；**≤20 确定性买**——草案带 `buy_tier: conviction` + `conviction: true` + `buy_tier_note`（≤20 全年仅 6-14% 交易日出现）。推送文案若按 subtype/kind 过滤请注意新 subtype=trim（卖出侧，匹配语义与 exit 同）。
3. **11-10 回放零差异**（当日三票全横盘）；4 周影子期新旧对照：`runtime/ttrade_r54/scripts/replay_20261110_param_diff.py` 可按日重跑，PM 15:12 日报可直接引用其当日差异计数。
4. 诚实观察项（已写入手册附录 R54 与报告 §4）：X65/70 历史胜率更高、X75/80 跨日出场 giveback 风险↑——按 Owner 裁决执行，4 周影子期对照统计。
