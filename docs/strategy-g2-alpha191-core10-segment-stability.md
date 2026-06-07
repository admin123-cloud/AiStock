# G2 Alpha191 核心 10 因子分段稳定性

生成日期：2026-05-25

本轮先复验最近两年最强的 10 个 Alpha191 因子：

`Alpha150, Alpha070, Alpha095, Alpha132, Alpha042, Alpha097, Alpha100, Alpha055, Alpha144, Alpha059`

输出目录：`reports/gen2_alpha191_segment_stability_core10`

## 分段

| 分段 | 区间 | 含义 |
| --- | --- | --- |
| `author_2010_2017` | 2010-01-04 到 2017-04-28 | 对齐作者因子检验主区间 |
| `post_style_2018_2021` | 2018-01-01 到 2021-12-31 | 2017 后风格变化和核心资产阶段 |
| `weak_market_2022_2023` | 2022-01-01 到 2023-12-31 | 弱市阶段 |
| `recent_g2_2024_2026` | 2024-01-01 到 2026-05-25 | 当前 G2 研究相关阶段 |

## 结论

第一批结果非常强：这 10 个因子在四个历史阶段全部为 `strong`，并且方向全部一致。

这说明我们最近两年筛出来的头部因子并不是只在近两年偶然有效，而是和作者 2010-2017 的长样本统计方向一致。它们可以进入 G2 的第一版 Alpha191 深化结合实验。

## 跨周期稳定性

| 因子 | 因子族 | 方向 | 平均 Edge | 最小 Edge | 平均 abs(IC) | 结论 |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `Alpha150` | price_volume | low | 1.13% | 0.70% | 0.094 | 4/4 strong，方向一致 |
| `Alpha070` | price_volume | low | 1.02% | 0.69% | 0.094 | 4/4 strong，方向一致 |
| `Alpha095` | price_volume | low | 0.98% | 0.63% | 0.087 | 4/4 strong，方向一致 |
| `Alpha132` | price_volume | low | 0.91% | 0.48% | 0.075 | 4/4 strong，方向一致 |
| `Alpha042` | price_volume_corr | high | 0.67% | 0.55% | 0.070 | 4/4 strong，方向一致 |
| `Alpha144` | price_volume | high | 0.64% | 0.40% | 0.050 | 4/4 strong，方向一致 |
| `Alpha097` | price_volume | low | 0.58% | 0.41% | 0.061 | 4/4 strong，方向一致 |
| `Alpha100` | price_volume | low | 0.55% | 0.37% | 0.056 | 4/4 strong，方向一致 |
| `Alpha059` | momentum_reversal | low | 0.44% | 0.34% | 0.050 | 4/4 strong，方向一致 |
| `Alpha055` | momentum_reversal | low | 0.37% | 0.24% | 0.050 | 4/4 strong，方向一致 |

## 分段观察

每个阶段的 10 个因子全部为 strong：

| 分段 | strong 数 | 最强因子 | 最强方向 | 最强 Edge |
| --- | ---: | --- | --- | ---: |
| `author_2010_2017` | 10/10 | `Alpha150` | low | 1.74% |
| `post_style_2018_2021` | 10/10 | `Alpha150` | low | 0.91% |
| `weak_market_2022_2023` | 10/10 | `Alpha150` | low | 1.16% |
| `recent_g2_2024_2026` | 10/10 | `Alpha150` | low | 0.70% |

`Alpha150` 是最强锚点，四个阶段都排在第一。`Alpha070` 和 `Alpha095` 是同族强确认，`Alpha132` 可作为备选，`Alpha042` 提供量价相关族确认，`Alpha144` 方向相反但同样稳定，适合增强组合维度。

## 对 G2 的使用方式

第一版深化结合建议：

1. 主门控：`Alpha150 + Alpha095 + Alpha144`
2. 强化门控实验：`Alpha150 + Alpha070 + Alpha095 + Alpha132 + Alpha144`
3. 因子族确认：量价族通过后，再用 `Alpha042` 做量价相关确认。
4. 反转修复辅助：`Alpha059/Alpha055` 只作为修复质量观察，不先放入主门控。

注意：这些结果仍是全市场横截面分层测试，不等同于 G2 候选池内收益。下一步必须把这 10 个稳定因子叠到 G2/V4 候选池上，验证它们是否能在候选池内部继续区分好坏。

## 产物

- 分段最佳因子表：`reports/gen2_alpha191_segment_stability_core10/alpha191_segment_best_by_factor.csv`
- 跨段稳定性表：`reports/gen2_alpha191_segment_stability_core10/alpha191_cross_segment_stability.csv`
- 分段评级统计：`reports/gen2_alpha191_segment_stability_core10/alpha191_segment_rating_summary.csv`
- 运行日志：`reports/gen2_alpha191_segment_stability_core10/run.log`
