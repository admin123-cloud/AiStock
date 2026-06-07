# G2/V4 Alpha191 轻约束候选版贡献审计
日期：2026-05-25

## 结论

这一步审计三个可落地候选版的交易贡献集中度，目标是确认 `runup<=100%` 的收益提升是不是靠少数极端交易撑起来。

结论：不是。

`volume5_keep80 + runup<=100%` 相比进攻基准 `volume5_rank_none`，总 PnL 提升约 12,690，top5 毛利占比为 30.65%，与基准的 30.26% 基本相同；top10 毛利占比为 50.90%，也只是略高于基准 49.38%。它的收益提升主要来自共同交易上的排序/仓位改善，而不是新增少数爆款交易。

## 数据口径

- 输入回测目录：`reports/gen2_alpha191_light_constraint_matrix/`
- 输出目录：`reports/gen2_alpha191_light_constraint_contribution_audit/`
- 汇总表：`reports/gen2_alpha191_light_constraint_contribution_audit/concentration_summary.csv`
- 差异表：`reports/gen2_alpha191_light_constraint_contribution_audit/delta_vs_baseline.csv`
- 分段贡献：`reports/gen2_alpha191_light_constraint_contribution_audit/segment_contribution.csv`
- 脚本：`scripts/gen2_alpha191_contribution_audit.py`
- 对照基准：`volume5_rank_none`

## 集中度对照

| 版本 | lot数 | 总PnL | 毛利 | 亏损 | 胜率 | top1/毛利 | top3/毛利 | top5/毛利 | top10/毛利 | 最大月份/净利 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `volume5_rank_none` | 60 | 237,284 | 376,265 | -138,981 | 58.33% | 9.16% | 20.49% | 30.26% | 49.38% | 27.71% |
| `volume5_rank_runup_le100` | 59 | 245,335 | 389,352 | -144,017 | 57.63% | 9.36% | 20.74% | 30.53% | 49.77% | 28.34% |
| `volume5_keep80_none` | 56 | 228,414 | 356,769 | -128,354 | 57.14% | 10.15% | 21.36% | 31.23% | 51.86% | 25.99% |
| `volume5_keep80_runup_le100` | 56 | 249,975 | 384,268 | -134,293 | 57.14% | 9.96% | 20.96% | 30.65% | 50.90% | 25.10% |
| `volume5_keep80_overhead_le5__runup_le100` | 53 | 240,332 | 364,730 | -124,397 | 58.49% | 10.24% | 21.55% | 31.51% | 52.33% | 25.48% |

## 与进攻基准的差异

| 版本 | 总PnL差异 | 共同交易数 | 新增交易 | 新增PnL | 删除交易 | 被删交易在基准PnL | 共同交易PnL差异 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `volume5_rank_runup_le100` | +8,050 | 56 | 3 | 4,795 | 4 | 7,255 | +10,510 |
| `volume5_keep80_none` | -8,870 | 53 | 3 | 28,970 | 7 | 40,111 | +2,270 |
| `volume5_keep80_runup_le100` | +12,690 | 51 | 5 | 40,386 | 9 | 39,848 | +12,152 |
| `volume5_keep80_overhead_le5__runup_le100` | +3,048 | 46 | 7 | 11,296 | 14 | 17,953 | +9,705 |

这里最关键的是 `volume5_keep80_runup_le100`：

- 新增交易 PnL 40,386，删除交易在基准里的 PnL 39,848，二者几乎抵消。
- 真正拉开差距的是共同交易 PnL 改善 12,152。
- 所以它不是“多抓了几笔大牛票”，而是同一批可成交机会里，排序与仓位分配更有效。

## 分段贡献

| 版本 | 训练段 | 验证段 | 2026YTD盲测 |
| --- | ---: | ---: | ---: |
| `volume5_rank_none` | 18,860 | 88,861 | 129,564 |
| `volume5_rank_runup_le100` | 28,481 | 93,987 | 122,867 |
| `volume5_keep80_none` | 18,860 | 101,955 | 107,599 |
| `volume5_keep80_runup_le100` | 28,481 | 107,764 | 113,729 |
| `volume5_keep80_overhead_le5__runup_le100` | 24,178 | 105,166 | 110,988 |

分段上要分清用途：

- `volume5_rank_none`：2026YTD 盲测 PnL 最高，适合作为进攻线和 shadow rank。
- `volume5_keep80_runup_le100`：训练、验证、盲测都不弱，全周期收益最高，且集中度没有明显恶化，适合作为主候选。
- `volume5_keep80_overhead_le5__runup_le100`：总收益低于主候选，但亏损更低、胜率更高，更像风险偏好低时的备选。

## 当前落地判断

不要把 Alpha191 做成单一硬门槛。更好的用法是三层：

1. 实盘先输出 `volume5_rank_none`，保留进攻排序视角。
2. 默认候选用 `volume5_keep80 + runup<=100%`，作为当前主线。
3. 当账户回撤、市场压力或个股上方筹码压力明显时，切到 `volume5_keep80 + overhead<=5% + runup<=100%`。

下一步应进入“实盘影子字段设计”，把 `alpha191_volume5_score`、`alpha191_volume5_rank_in_day`、`alpha191_original_v4_rank`、`runup_from_60d_low`、`overhead_pressure_amount_share` 一起输出到实盘页，先观察解释力，再决定是否接入真实下单权重。
