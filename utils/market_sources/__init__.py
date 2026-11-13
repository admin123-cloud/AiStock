# -*- coding: utf-8 -*-
"""AiStock 行情数据源适配层。

- tdx_official_package: 通达信官网每日盘后包（日线主源，PoC-B 2026-11-01 验证）
- tencent_daily: 腾讯 K 线日线（交叉源，PoC-A 2026-11-01 验证）
- tencent_kline: 腾讯分钟线 m1/m5/m15/m30/m60（vendored a-stock-data V3.10.3
  §1.2 tencent_kline；proxy.finance.qq.com 通道优先，2026-11-13 盘中链路主源，
  对 TDX 盘后 m5 逐 bar 对账见 runtime/intraday_kline_upsert/）
"""
