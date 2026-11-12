# R46 做T 1 分钟快车道上线通知（aistock-engineer → 全会话，2026-11-11）

## 结论
R46 已上线：main=`6a9a399` push+DEPLOY_OK（11:27 容器切换，pipeline_scheduler 1.4.0）。
零交易、零 formal 表写、慢车道（5m 收线）判定语义不变。

## 变更面
- `services/t_trade_signal.py`（1.1.0）：新增 `run_fast_tick`（1 分钟拍：东财 ulist 实时价
  + 5m 形成中 bar 实时刷新 → intrabar RSI5；价带类触发=买带/卖带/在途T目标带/止损线
  → fast 草案 `fast_*`、bar_status=open、intrabar_rsi5 实值）；预检门/布防/30min 冷却
  与慢车道共用同一 SignalState（完全一致）；`run_beat` 开拍先做收线结算
  （confirmed_bar / expired_unconfirmed 留档）+ 同波 (code,side,band) 去重并入；
  reconcile 汇总 fast 命中率。票池=broker_state ∪ watchlist（`load_t_pool`）。
- `services/t_trade_shadow.py`（1.1.0）：`shadow_fast_tick` 影子同步（observe_only、
  shadow/ 物理隔离不变）；beat 收线结算（expired 不虚拟成交不进 4 周判定）。
- `scripts/pipeline_scheduler.py`（1.4.0）：新任务 `t_trade_signal_fast`，交易日
  09:31~14:59 每分钟（cron 秒错峰 23s，与快照/慢 beat 不撞线）。
- 东财限频：ulist 票池一次请求（≤10 码/块）；分时K TTL 缓存 ≤1 次/分钟/码；
  回放 1m 走 trends2（kline klt=1 实证只回当日）。

## 关键实证
- 11-10 全日快慢交织回放：002015 卖T fast **09:36** @13.70 intrabar RSI 73.4
  → 09:41 收线 confirmed_bar；现行慢车道 09:41、Owner 实际卖出 09:38:52——fast 领先。
  影子 4 条 fast 信号（1 条 09:32 触发 09:35 收线瓦解 → expired_unconfirmed 留档）。
- 测试：新增 40（fast lane 23 / shadow 10 / 调度 7），做T全家族 162 全过，
  预览安全矩阵 21/21；全量差量对证=另一会话 WIP 本底 20 条逐名全等零新增
  （唯一例外 R42 版本钉 1.3.0→1.4.0 已同步更新）。
- 今日盘内：11:27:24 首拍起每分钟落 beats/fast_20261111.jsonl；票池 3 码
  （002015/300054/603881，watchlist 10:46 实况）；ulist 实时价+intrabar RSI 实证；
  regime 已用并集票池重跑（603881 数据港 sideways 派生带 [21.8,22.31]）。

## 注意
- PM watcher：fast 草案落 pending_push 协议不变；收线确认/瓦解会原位重写
  pending_push 中**尚未消费**的同名文件（已消费则只改 drafts 台账）。
- 300037 新宙邦上午在 watchlist、10:46 被移除——池随实况，非缺陷。
- 快车道仅价带类；exit_rsi/fallback_eod 仍在慢车道（回测语义根基不动）。
