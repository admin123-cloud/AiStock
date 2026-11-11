# COORDINATION：R55 市场看板 P0 三件套（2026-11-11，aistock-engineer）

任务书：`F:/Aistock/data/data/runtime/task_briefs/20261111_market_breadth_r55.md`（Owner 已批）。
Worktree：`F:/Aistock/worktrees/market-dashboard-r55`（分支 engineer/market-dashboard-r55，基点 main=e31dd78）。

## 变更面（全部零 formal 表写、零新外部接口）

1. **市场宽度** `services/market_breadth.py`（新）：kline_daily 一条聚合 SQL（raw+argMax、
   60 交易日窗 row_number 编号）；MA5/20/60 站上占比（1e-6 价格容差消除 FP 抖动）+
   60 日新高/新低 + 高低比；`GET /api/market/breadth?date=`。
2. **涨跌分布直方图** 同模块：13 分桶（<-10/-10~-8/.../0/.../8~10/>10，左闭右开）+
   平均/中位（quantileExact）；盘中（R28 快照 is_fresh）/盘后（日线）双模式
   fail-closed；`GET /api/market/advance-decline-distribution`。
3. **涨停生态聚合** `services/market_mood.py`（新）+ `scripts/market_mood_update.py`（新 CLI）：
   涨停价=昨收×R20b 板块规则（ST5%/BJ30%/创业科创20%/主板10%，Decimal ROUND_HALF_UP 到分）；
   曾涨停=当日最高触及（盘中=快照最大价 5min 粒度，盘后=日线 high 精确覆盖）；封板率/
   炸板/最高连板/梯队表；产物落 `runtime/market_mood/YYYYMMDD.json`（realtime/final 双键，
   原子写；**运营层 runtime JSON，非正式表**）；`GET /api/market/limit-up-stats`。
4. **调度** `scripts/pipeline_scheduler.py` v1.4.0→1.5.0：intraday 快照节拍成功后尾挂
   market_mood --intraday（独立异常面，mood 失败不影响快照状态）；新 job
   market_mood_finalize 工作日 16:40（窗口 16:30~23:50，交易日守卫，幂等）。
5. **regime 接入** `services/t_trade_signal.py` run_regime 增 breadth 键（水位确认因子：
   above_ma20 ≥60 strong/≤40 weak；up×weak 或 down×strong 标 divergence；**观察期只标注
   不改判定式**；breadth 不可用 → status=unavailable fail-safe，失败路径同样带键）。
6. **白名单** `api/operations_preview.py` 153→156 条 GET；安全矩阵 304→310 对。
7. **compose** `deploy/docker-compose.main.yml`：preview 容器只读嵌套挂载
   `runtime/market_mood:ro`（R40 t_tracker 先例；写方=pipeline-runner）。
8. **前端** `frontend-react/src/views/market/MarketDashboardBlocks.tsx`（新）+ Home.tsx 挂载：
   首页三区块（宽度条/直方图/涨停生态卡），随快照 5min 刷新。

## 测试与验证

- 新增测试 71 例全绿（分桶边界/封板率口径/连板计数/涨停价半进位/落盘原子性/
  模式选择/白名单矩阵/调度尾挂与守卫）。
- 全量 vs 干净基线 e31dd78：失败集全等 70=70（3 个既有收集错误文件按惯例 --ignore）。
- 真库验证（2026-11-11）：宽度 MA5 68.06% vs 独立 SQL 68.06% 对拍一致（连跑稳定）；
  分布 13 桶和=covered 5564；终算涨停 88/跌停 8/曾涨停 123/炸板 35/封板率 71.54%/
  最高 4 板（梯队 4/5/14/67 与 88 闭合）；pct 法交叉 89/7（口径差 1 只符合预期）；
  盘中注入 15:00 快照 limit_up 88=终算 88、ever 110<123（5min 粒度漏短暂触板，已声明）。
- regime 真库干跑：breadth 键 level=neutral、divergence={}（300054 up×neutral 无背离）。

## 运营层产物声明

- `F:/Aistock/data/data/runtime/market_mood/`（新目录，首件 20261111.json=final 键，真库终算）。
- regime_YYYYMMDD.json 自明日起 09:25 起 breadth 键生效。

## 遗留（明日盘中）

- 盘中形态浏览器实测待明日 09:35 后（快照 job 首拍 + mood realtime 键生成）；
  盘后形态今日已实测。盘中"曾涨停"5min 粒度漏极短触板为已声明口径（盘后终算精确覆盖）。
