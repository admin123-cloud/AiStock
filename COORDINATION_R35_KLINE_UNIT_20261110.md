# COORDINATION_R35_KLINE_UNIT_UNIFY_20261110（aistock-engineer → 微信端 PM / aistock-dev）

R35 K线单位全局普查与统一已完成（Owner 已裁，任务书 task_briefs/20261110_kline_unit_unify_r35.md）。

## 结论速览
1. 全库 K 线单位已统一为 volume=手、amount=元：kline_minute_5/15/30/60 的 2020-01..2026-09 分区 + 周/月/季/年线共 345 个表×月批 ALTER UPDATE，345/345 批等比对账通过，终审 residual A/B/C=0。
2. kline_daily、pre2010、purchased、live_derived、盘中快照本就一致，未动。
3. Phase C 终验全绿：3码3日3代 5m 合计 vs 日线 off=0；81 个月交叉 rv=ra=1；系数=1.019（close 代理 VWAP 预期）。
4. 读侧零生产代码改动（生产读路径本就假设手/元）；R34 研究工件 a3_normalize.py 重跑会二次换算（已列示）。

## 需 PM/Owner 知悉的决策与遗留
- 5m harry 代 19.3% 非整百股：按任务书选项一「取整+残差留档」执行（volume=intDiv(vol,100) + volume_exact 留档原股数，零信息损失）；未动 schema。
- 分钟表内非股票资产（880/881 板块、转债、ETF、回购、指数）按类排除未动，口径矩阵见产物。
- X 坏数据 185 万行（0.3%）未动（包络检验无法归因单位，属既有数据质量问题）。
- 2026-08/09 双覆盖重复版本 ~920 万行：FINAL 读侧正确；物理 DEDUPLICATE 属生产级变更待 Owner 批。

产物：runtime/kline_unit_unify_r35/（REPORT.md 汇总；MATRIX/PLAN/READS/证据与 journal 全量落盘）。
锁 kline_unit_unify_r35 已释放；16:15 盘后管道先行完成（16:23 全绿）后才启动写批，无冲突。
KB 回写随后提交（projects/aistock）。
