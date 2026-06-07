# G2/V4 Alpha191 组合叠加矩阵

日期：2026-05-25

## 结论

第二步把 Alpha191 从“候选池分层”推进到“真实组合回测”：同一套 G2/V4 risk_cool 候选池，同一套 30m 止损/止盈/冷却引擎，只改变 Alpha191 的使用方式。

当前最有价值的用法不是硬过滤，也不是仓位倾斜，而是把 `volume5` 或 `main3` 用作同日候选排序层。`volume5_rank` 全周期收益最高，`volume5_keep80` 的 Sharpe 略高且交易更少；`top50` 这类硬过滤明显伤害收益，不适合直接升级。

## 数据口径

- 输入分数：`reports/gen2_alpha191_candidate_core10_t1/candidate_alpha191_scored.parquet`
- 输出目录：`reports/gen2_alpha191_portfolio_overlay_matrix`
- 汇总表：`reports/gen2_alpha191_portfolio_overlay_matrix/portfolio_overlay_summary.csv`
- 分段表：`reports/gen2_alpha191_portfolio_overlay_matrix/portfolio_overlay_segment_summary.csv`
- 回测引擎：`scripts/gen2_backtest_risk_cool_dynamic_circuit.py`
- 新增矩阵脚本：`scripts/gen2_alpha191_portfolio_overlay_matrix.py`
- 基准策略：`baseline_trigger_time`，即原始 G2/V4 候选按触发时间买入
- 策略规则：`stop_cd3_skip`，最大 2 仓，每日最多 1 买，30m 风控，T-1 Alpha191 分数

## 组合矩阵

| variant | 用法 | 交易数 | 总收益 | 最大回撤 | Sharpe | 胜率 | 相对基准 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `volume5_rank` | 量价 5 因子只排序 | 94 | 158.19% | -14.78% | 2.17 | 71.28% | +28.51pct |
| `volume5_keep80` | 量价 5 因子弱过滤 | 88 | 152.28% | -14.78% | 2.20 | 70.45% | +22.60pct |
| `main3_rank` | 主 3 因子只排序 | 92 | 146.24% | -14.78% | 2.08 | 70.65% | +16.56pct |
| `main3_keep80` | 主 3 因子弱过滤 | 86 | 140.90% | -14.78% | 2.06 | 69.77% | +11.22pct |
| `baseline_trigger_time` | 原始触发时间排序 | 81 | 129.68% | -17.53% | 2.12 | 65.43% | 0 |
| `volume5_tilt` | 量价 5 因子仓位倾斜 | 94 | 117.01% | -12.59% | 2.16 | 71.28% | -12.67pct |
| `main3_tilt` | 主 3 因子仓位倾斜 | 92 | 105.70% | -12.53% | 2.06 | 70.65% | -23.98pct |
| `repair2_rank` | 修复 2 因子只排序 | 83 | 106.53% | -13.91% | 1.83 | 66.27% | -23.15pct |
| `core10_keep80` | 核心 10 因子弱过滤 | 84 | 80.51% | -15.12% | 1.51 | 61.90% | -49.16pct |

## 分段观察

| variant | 训练段 | 验证段 2025Q2-Q4 | 盲测 2026YTD |
| --- | ---: | ---: | ---: |
| `baseline_trigger_time` | 0.86% | 88.45% | 13.21% |
| `volume5_rank` | 12.57% | 51.32% | 52.54% |
| `volume5_keep80` | 12.57% | 59.01% | 41.84% |
| `main3_rank` | 7.46% | 51.32% | 52.40% |
| `main3_keep80` | 7.46% | 58.97% | 41.92% |
| `repair2_rank` | -2.25% | 51.50% | 33.23% |
| `repair2_top50` | -7.30% | 0.58% | 37.56% |
| `volume5_tilt` | 13.17% | 44.81% | 33.16% |
| `main3_tilt` | 8.63% | 43.89% | 32.35% |

这里要注意一个细节：验证段原始基准本身非常强，Alpha191 排序会牺牲一部分 2025Q2-Q4 的收益，但显著修复 2026YTD。也就是说，它更像“阶段均衡器”，不是单纯收益放大器。

## 使用判断

| 用法 | 结果 | 判断 |
| --- | --- | --- |
| 只排序 | `volume5_rank/main3_rank` 全周期提升，2026YTD 大幅强于基准 | 优先进入下一轮主线候选 |
| 弱过滤 keep80 | `volume5_keep80/main3_keep80` 收益略低于 rank，但 Sharpe/交易数更均衡 | 可作为稳健候选 |
| 硬过滤 top50 | 多数组合收益明显下降，`Alpha144_top50` 甚至为负 | 暂不采用 |
| 仓位倾斜 | 回撤下降，但收益也下降；适合风控版，不适合进攻主线 | 只保留为防守实验 |
| `repair2` | 候选池分层好，但组合收益不如 `volume5/main3` | 可做环境修复观察，不做主排序 |
| `core10` | 组合层显著弱于基准 | 不做大而全复合分 |

## 下一步

1. 固定两个候选进入第三步：`volume5_rank` 进攻版，`volume5_keep80` 稳健版。
2. 做交易贡献拆解：检查它们是否仍然被少数大牛股贡献主导，尤其看 Top3/Top5 贡献和月度集中度。
3. 做状态切分：按 2025 强势验证段、2026 盲测段、市场 MA20 状态、近端压力状态拆分，确认 `volume5/main3` 排序在什么环境下该启用。
4. 实盘侧先做 shadow rank 字段，不直接硬过滤；页面可以显示 `alpha191_volume5_rank`、`alpha191_volume5_score`、`alpha191_mode=rank/keep80`。

