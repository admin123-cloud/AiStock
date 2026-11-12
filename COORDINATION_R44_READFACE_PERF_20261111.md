# COORDINATION — R44 放行面性能修复已完成、已部署（main=3d65a4b）

写给：正在 F:/Aistock/app 共享树工作的并行会话
来自：aistock-engineer（R44，2026-11-11 01:35 收口）

## 终态

1. **origin/main = `3d65a4b`**（四连提交：857bf37 raw+argMax/谓词裁剪/sync handler → 82ba912 两步法+缺件 memo 修复+有界窗 → 3969c10 console 链 builder memo → 3d65a4b 日历 TTL 缓存），全部经官方 deploy-main.sh **DEPLOY_OK**；共享树已 ff-only 前移，你的未提交改动零触碰、未参与构建。
2. R43 放行 35 条 GET 性能验收全过：**冷 P95 0.487s（max 1.697s）<2s、热 P95 0.384s<500ms**；PM 点名三条 current 42s→0.17s、gen2 workflow 31s→0.27s、daily-trade-ticket 11.8s→0.004s（后两条修前实为 504/被阻塞连带）。零功能语义变化（argMax/两步法/谓词逐项独立对拍全等）、零 formal 写。
3. **撞线文件**（你后续 rebase 到 3d65a4b 即可）：api/gen3_state_alpha.py、api/trading.py、api/gen2_strategy.py、scheduler/trading_calendar.py、tests/test_r43_readface_purify.py（调用助手 sync/async 双态）、tests/test_r44_readface_perf.py（新，26 例守卫）。若你手上有 trading.py / gen3_state_alpha.py 未提交改动，合并时注意：我只改了查询形状与缓存，函数签名零变化（6 个构建器新增 `*_uncached` 内部变体，外部名不变）。
4. 行为面唯一新增语义（任务书明示的 TTL 缓存）：`_load_current_runtime` 15s、6 个中层构建器 20s、TradingCalendar 300s——锚点文件 mtime 变更即时失效；`refresh=True`（POST 写链）一律绕过缓存直通重算；POST record-execution-evidence 写 task_reviews 后相关缓存即刻失效。
5. 测量期实证：R41 的 GB 级扫描会把 0.1s 查询拖成 2-4s——全站计时/验收请先确认 CH 静默（system.query_log 近 3 分钟 read_bytes>1e9 计数=0）再开测。
6. 后端日志中 QMT 桥 503 告警为既有问题（host 桥未开），与 R44 无关。

交付：`F:/Aistock/data/data/runtime/readface_perf_r44/`（REPORT.md：35 条冷热前后对照表、9 处病灶归因、四轮 pytest 差量对证 70=70、等价对拍脚本与输出）。
