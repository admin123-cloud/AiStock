# G2/V4 Alpha191 volume5 轻约束矩阵

日期：2026-05-25

## 结论

第五步把状态归因里最有希望的轻约束放回真实组合引擎里测试：

- `overhead_pressure_amount_share <= 5%`
- `runup_from_60d_low <= 100%`
- `market_breadth <= 80%`
- 以及它们的组合

结果不是“约束越多越好”。最有价值的是：

- 进攻版：`volume5_rank` 保持无约束或只加 `runup<=100%`。
- 稳健版：`volume5_keep80 + runup<=100%` 是当前最好组合。
- 回撤版：`volume5_keep80 + overhead<=5% + runup<=100%` 收益略低，但回撤更低、Sharpe 更高。
- `breadth<=80%` 单独不值得，会明显牺牲全周期收益。

## 数据口径

- 输入分数：`reports/gen2_alpha191_candidate_core10_t1/candidate_alpha191_scored.parquet`
- 输出目录：`reports/gen2_alpha191_light_constraint_matrix/`
- 汇总表：`reports/gen2_alpha191_light_constraint_matrix/light_constraint_summary.csv`
- 分段表：`reports/gen2_alpha191_light_constraint_matrix/light_constraint_segment_summary.csv`
- 脚本：`scripts/gen2_alpha191_light_constraint_matrix.py`
- 组合引擎：`stop_cd3_skip`，T-1 Alpha191，30m 风控

## 全周期结果

| variant | signals | trades | 总收益 | 最大回撤 | Sharpe | 胜率 | 平均交易收益 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `volume5_keep80_runup_le100` | 210 | 88 | 166.65% | -14.78% | 2.32 | 71.59% | 6.19% |
| `volume5_rank_runup_le100` | 289 | 92 | 163.56% | -14.78% | 2.24 | 71.74% | 5.87% |
| `volume5_keep80_overhead_le5__runup_le100` | 188 | 84 | 160.22% | -13.35% | 2.33 | 72.62% | 6.22% |
| `volume5_keep80_overhead_le5` | 198 | 87 | 158.84% | -13.35% | 2.25 | 71.26% | 6.05% |
| `volume5_rank_none` | 314 | 94 | 158.19% | -14.78% | 2.17 | 71.28% | 5.64% |
| `volume5_keep80_none` | 223 | 88 | 152.28% | -14.78% | 2.20 | 70.45% | 5.92% |
| `volume5_rank_overhead_le5` | 271 | 92 | 151.34% | -13.78% | 2.12 | 70.65% | 5.64% |
| `volume5_rank_breadth_le80` | 286 | 86 | 141.20% | -15.10% | 2.10 | 70.93% | 5.71% |

对比上一轮基准：

| 策略 | 总收益 | 最大回撤 | Sharpe |
| --- | ---: | ---: | ---: |
| `baseline_trigger_time` | 129.68% | -17.53% | 2.12 |
| `volume5_rank_none` | 158.19% | -14.78% | 2.17 |
| `volume5_keep80_runup_le100` | 166.65% | -14.78% | 2.32 |
| `volume5_keep80_overhead_le5__runup_le100` | 160.22% | -13.35% | 2.33 |

## 分段表现

### 2026YTD 盲测

| variant | 2026YTD 收益 | 最大回撤 | Sharpe |
| --- | ---: | ---: | ---: |
| `volume5_rank_none` | 52.54% | -7.60% | 4.07 |
| `volume5_rank_breadth_le80` | 52.54% | -7.60% | 4.07 |
| `volume5_rank_runup_le100` | 47.28% | -7.60% | 3.85 |
| `volume5_rank_overhead_le5` | 44.69% | -7.60% | 3.65 |
| `volume5_keep80_none` | 41.84% | -5.29% | 4.06 |
| `volume5_keep80_runup_le100` | 41.84% | -5.29% | 4.06 |
| `volume5_keep80_overhead_le5__runup_le100` | 41.84% | -5.29% | 4.06 |

盲测里 `rank_none` 仍最强。`keep80` 多数轻约束在 2026YTD 没有改变成交结果，说明被过滤掉的不是 2026 的主要成交票。

### 2025Q2-Q4 验证段

| variant | 验证段收益 | 最大回撤 | Sharpe |
| --- | ---: | ---: | ---: |
| `volume5_keep80_runup_le100__breadth_le80` | 62.36% | -13.35% | 2.58 |
| `volume5_keep80_overhead_le5__runup_le100__breadth_le80` | 62.36% | -13.35% | 2.58 |
| `volume5_keep80_breadth_le80` | 60.59% | -13.35% | 2.56 |
| `volume5_keep80_none` | 59.01% | -13.35% | 2.42 |
| `volume5_rank_none` | 51.32% | -13.78% | 2.19 |

验证段里 `keep80` 比 `rank` 更稳，轻约束能稍微改善验证段质量。

## 约束解释

### `runup<=100%`

这是本轮最值得保留的轻约束。

- `volume5_rank`：158.19% -> 163.56%，Sharpe 2.17 -> 2.24
- `volume5_keep80`：152.28% -> 166.65%，Sharpe 2.20 -> 2.32

它符合上一轮状态归因：60 日低点以来涨幅超过 100% 后，性价比明显下降。

### `overhead<=5%`

这是回撤约束，不是收益增强约束。

- `volume5_rank`：收益 158.19% -> 151.34%，回撤 -14.78% -> -13.78%
- `volume5_keep80`：收益 152.28% -> 158.84%，回撤 -14.78% -> -13.35%

对 `keep80` 更友好，对 `rank` 会牺牲 2026 盲测进攻。

### `breadth<=80%`

单独不建议保留。

- `volume5_rank`：158.19% -> 141.20%
- `volume5_keep80`：152.28% -> 135.68%

虽然状态归因显示市场宽度 50%-80% 最舒服，但直接 `breadth<=80%` 会删掉一些仍有价值的交易，不能机械硬切。

## 当前候选版本

| 版本 | 规则 | 用途 |
| --- | --- | --- |
| 进攻版 | `volume5_rank` | 保留 2026YTD 最强进攻性，适合作为 shadow rank 主观察线 |
| 均衡版 | `volume5_keep80 + runup<=100%` | 当前全周期收益最高，Sharpe 也高，适合作为下一轮主候选 |
| 稳健版 | `volume5_keep80 + overhead<=5% + runup<=100%` | 收益略低但回撤更低，适合风控偏好的候选 |

## 下一步

下一步不建议继续堆静态约束。更应该做两件事：

1. 对这三个候选版本做交易贡献审计，确认 `runup<=100%` 的收益提升不是新引入少数极端交易。
2. 做实盘影子字段设计：至少输出 `alpha191_volume5_score`、`alpha191_volume5_rank_in_day`、`alpha191_original_v4_rank`、`runup_from_60d_low`、`overhead_pressure_amount_share`，让实盘页面能解释为什么某票被提拔。

