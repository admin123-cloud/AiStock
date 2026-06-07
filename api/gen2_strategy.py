from __future__ import annotations

import json
import math
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from sqlalchemy import text

from utils.database import db
from utils.logger import get_logger
from utils.market_warehouse import clickhouse_available, clickhouse_query_df
from utils.paths import report_path, runtime_path

logger = get_logger("gen2_strategy")

REPO_ROOT = Path(__file__).resolve().parents[1]
GEN2_TRADE_CLASSIFICATION_PATH = runtime_path("gen2_trade_classifications.json")
GEN2_TRADE_CLASSIFICATION_LOCK = threading.Lock()

GEN2_TRADE_CLASSIFICATIONS = [
    {"value": "", "label": "未分类", "type": "info"},
    {"value": "favorite", "label": "最喜欢", "type": "success"},
    {"value": "watch", "label": "值得研究", "type": "primary"},
    {"value": "normal", "label": "普通样本", "type": "info"},
    {"value": "problem", "label": "问题交易", "type": "warning"},
    {"value": "reject", "label": "应过滤", "type": "danger"},
]

GEN2_META = {
    "strategy_code": "gen2_v4_reference_lab",
    "strategy_name": "第二代策略实验台",
    "strategy_label": "V4参考池 + 模式开发",
    "status": "research_only",
    "base_pool": "v4_reference",
    "execution_note": "当前只做研究和观察，不直接接入实盘交易闭环。",
}

REPORT_SOURCES = [
    {
        "key": "gen2_v2_complete_strategy",
        "label": "G2 v2 complete",
        "path": report_path("gen2_v2_complete_strategy", "summary.json"),
    },
    {
        "key": "gen2_alpha191_t1_keep80_stop_cd3_full",
        "label": "G2 Alpha191 keep80",
        "path": report_path("gen2_alpha191_t1_keep80_stop_cd3_full", "summary.json"),
    },
    {
        "key": "gen2_risk_cool_shadow_ledger",
        "label": "G2 risk cool shadow",
        "path": report_path("gen2_risk_cool_shadow_ledger", "summary.json"),
    },
]

GEN2_OPEN_SIGNAL_SOURCE = report_path("gen2_30m_fractal_restart_realistic_d1_w2", "fractal_triggers.parquet")
GEN2_OPEN_STATE_DAILY_SOURCE = report_path("gen2_open_state_research_full", "g2_open_state_daily.csv")
GEN2_OPEN_STATE_SWEEP_SUMMARY = report_path("gen2_open_v1_state_only_sweep_all", "summary.json")
GEN2_CURRENT_RISK_COOL_SOURCE = report_path("gen2_risk_cool_dynamic_circuit_user_v2_cap_v2", "sources", "risk_cool_base.csv")
GEN2_CURRENT_RISK_COOL_RUN_DIR = (
    report_path("gen2_risk_cool_dynamic_circuit_user_v2_cap_v2", "backtests", "two_stop_cd3_skip")
)
GEN2_CURRENT_RISK_COOL_REPORT = report_path("gen2_user_v2_optimization_notes.md")
GEN2_CURRENT_RISK_COOL_TIMING_REPORT = report_path("gen2_user_v2_timing_layer", "report.md")
GEN2_ALPHA191_T1_KEEP90_RUN_DIR = (
    report_path("gen2_alpha191_t1_keep90_stop_cd3_full", "backtests", "alpha191_gate_keep90_stop_cd3_skip", "stop_cd3_skip")
)
GEN2_ALPHA191_T1_KEEP90_REPORT = report_path("gen2_alpha191_t1_keep90_stop_cd3_full", "summary.json")
GEN2_ALPHA191_T1_KEEP80_RUN_DIR = (
    report_path("gen2_alpha191_t1_keep80_stop_cd3_full", "backtests", "alpha191_gate_keep80_stop_cd3_skip", "stop_cd3_skip")
)
GEN2_ALPHA191_T1_KEEP80_REPORT = report_path("gen2_alpha191_t1_keep80_stop_cd3_full", "summary.json")
GEN2_ALPHA191_SCORE050_DD8_RUN_DIR = (
    report_path("gen2_alpha191_dd_guard_sweep", "backtests", "score_ge_050", "trigger_time", "dd8_half_stop_cd3_skip")
)
GEN2_ALPHA191_SCORE050_DD8_ROLL_RUN_DIR = (
    report_path("gen2_alpha191_score050_next_guard_sweep_v2", "backtests", "score_ge_050", "trigger_time", "dd8_half_roll20_2stop_cd5_half")
)
GEN2_ALPHA191_OVERHEAD005_DD8_RUN_DIR = (
    report_path("gen2_alpha191_dd_guard_sweep", "backtests", "score050_overhead005", "trigger_time", "dd8_half_stop_cd3_skip")
)
GEN2_ALPHA191_DD_GUARD_REPORT = report_path("gen2_alpha191_dd_guard_sweep", "conclusion_zh.md")
GEN2_ALPHA191_NEXT_GUARD_REPORT = report_path("gen2_alpha191_score050_next_guard_sweep_v2", "conclusion_zh.md")
GEN2_ALPHA191_MAIN_VOLUME5_RUNUP_RUN_DIR = (
    report_path("gen2_alpha191_light_constraint_matrix", "backtests", "volume5_keep80_runup_le100")
)
GEN2_ALPHA191_MAIN_VOLUME5_RUNUP_REPORT = REPO_ROOT / "docs" / "strategy-g2-alpha191-light-constraint-contribution.md"
GEN2_CURRENT_SHADOW_LEDGER = report_path("gen2_risk_cool_shadow_ledger", "shadow_ledger.csv")
GEN2_CURRENT_SHADOW_SUMMARY = report_path("gen2_risk_cool_shadow_ledger", "summary.csv")
GEN2_CURRENT_TARGET_SOURCE = (
    report_path("gen2_intraday_normal_signal_filters_tday_context", "signals_intraday_normal_30m_before_confirm.parquet")
)
GEN2_CURRENT_V4_EVENT_DATASET = report_path("gen2_event_study_full", "v4_event_dataset.parquet")
GEN2_TIMING_INDEX_OPTIONS = {
    "999999.SH": {"code": "999999.SH", "name": "上证指数"},
    "399006.SZ": {"code": "399006.SZ", "name": "创业板指"},
    "399001.SZ": {"code": "399001.SZ", "name": "深证成指"},
}
GEN2_TIMING_DEFAULT_INDEX_CODE = "999999.SH"

GEN2_OPEN_STATE_DEFINITIONS = {
    "OFF": {
        "label": "OFF",
        "title": "不新开仓",
        "target_exposure": "0% - 20%",
        "can_open": False,
        "meaning": "活跃指数与市场广度同时偏弱时关闭新开仓，只管理已有仓位。",
        "trade_rule": "当前 G2 Open V1 不在 OFF 中交易。",
    },
    "PROBE": {
        "label": "PROBE",
        "title": "试探开仓",
        "target_exposure": "0% - 30%",
        "can_open": False,
        "meaning": "活跃指数刚转强或弱修复，但广度和斜率仍不足，只适合观察最高质量信号。",
        "trade_rule": "当前 30m pullback-restart 规则暂不在 PROBE 中交易。",
    },
    "NORMAL": {
        "label": "NORMAL",
        "title": "正常开仓",
        "target_exposure": "30% - 70%",
        "can_open": True,
        "meaning": "中证1000站上 MA20，市场广度达到基本交易区间，是当前 G2 Open V1 的主交易状态。",
        "trade_rule": "当前 G2 Open V1 只保留 NORMAL 作为有效开仓状态。",
    },
    "AGGRESSIVE": {
        "label": "AGGRESSIVE",
        "title": "积极开仓",
        "target_exposure": "70% - 90%",
        "can_open": False,
        "meaning": "顺风环境更强，但当前 pullback-restart 买点在该状态下验证不稳定。",
        "trade_rule": "暂不与 NORMAL 合并，后续如启用需匹配新的入场形态。",
    },
}

GEN2_OPEN_RULE_V1 = {
    "rule_id": "g2_open_v1_normal_rank100_30m_break_high_vol",
    "rule_name": "G2 Open V1",
    "status": "research_signal",
    "pattern": "pullback_restart_rank100",
    "g2_open_state": "NORMAL",
    "trigger_type": "bottom_fractal_break_high_vol",
    "hold_days": 3,
    "search_days": 2,
    "summary": "NORMAL market state + V4 rank<=100 pullback restart context + 30m bottom-fractal break-high with volume expansion.",
    "risk_note": "Research signal only. Use as candidate entry signal before portfolio sizing, stop loss and take profit rules are validated.",
    "stability": {
        "sample_count": 254,
        "unique_codes": 214,
        "mean_3d": 0.0293,
        "median_3d": 0.0175,
        "win_rate_3d": 0.6063,
        "excess_mean_vs_csi1000_3d": 0.0190,
        "leave_top10_mean_3d": 0.0176,
        "leave_top10_win_rate_3d": 0.5902,
    },
}

GEN2_BACKTEST_RUNS = {
    "g2_alpha191_volume5_keep80_runup": {
        "strategy_code": "g2_alpha191_volume5_keep80_runup",
        "strategy_name": "G2 第二代完整版：volume5 + 突破板块扩散",
        "strategy_display_name": "第二代完整版：volume5主线 + 突破主线 + 板块扩散",
        "status": "main_execution_candidate",
        "run_dir": GEN2_ALPHA191_MAIN_VOLUME5_RUNUP_RUN_DIR,
        "summary_report": report_path("gen2_v2_complete_strategy", "summary.md"),
        "state_report": REPO_ROOT / "docs" / "strategy-g2-alpha191-volume5-light-constraints.md",
        "explanation_report": report_path("gen2_v2_complete_strategy", "summary.md"),
        "rule_lines": [
            "主线一：volume5_keep80_runup 保留全部候选；当 l3_rt_strong3_ratio >= 0.05 时，给排序分数加 0.05。",
            "主线二：只接入 big_bull 二次突破，必须同时满足盘中个股强度和细分板块扩散 l3_rt_strong3_ratio >= 0.05。",
            "板块数据：使用确认时刻可见的细分板块盘中 30m 成员涨幅扩散，不使用收盘后的板块日线结果。",
            "排序：采用 g2_v2，同日先排 volume5 主线，再用突破主线补位；同主线内按调整后分数、排名和确认时间排序。",
            "组合引擎：沿用 stop_cd3_skip、30m 风控、最多 2 仓、每日最多 1 笔、单票最多 50%。",
            "当前定位：第二代正式完整回测版本；真实下单仍需实盘页资金、持仓纪律和当日数据质量确认。",
        ],
    },
    "g2_attack_v1_prev_low": {
        "strategy_code": "g2_attack_v1_prev_low",
        "strategy_name": "G2 Attack V1 PrevLow",
        "strategy_display_name": "第二代进攻策略 V1：昨日低点 30m 确认离场",
        "status": "frozen_research",
        "run_dir": report_path("gen2_prev_low_exit_fill_compare", "runs", "gap_confirm_30m_close__intraday_30m_close"),
        "summary_report": report_path("gen2_prev_low_exit_fill_compare", "fill_compare_report.md"),
        "state_report": report_path("gen2_open_v1_state_only_sweep_all", "state_only_conclusion.md"),
        "explanation_report": REPO_ROOT / "docs" / "strategy-g2-attack-v1-prev-low-30m-confirm.md",
        "rule_lines": [
            "固定名称：第二代进攻策略 V1（G2 Attack V1 PrevLow）。",
            "股票池来自 V4 参考池，要求 pullback_restart_rank100。",
            "市场状态固定为 NORMAL，不合并 OFF/PROBE/AGGRESSIVE。",
            "30m 底分型突破高点并放量确认后开仓。",
            "每日最多新买 1 只，最多同时持有 2 只，单只最多 50%。",
            "买入后 30m 盘中 -5% 硬止损。",
            "上涨 +10% 时卖出 50%，锁定一半利润。",
            "剩余半仓最多持有 10 个交易日。",
            "剩余半仓在 +10% 卖半后，若盘中跌破上一交易日最低点，则视为短线结构破坏并退出。",
            "当前不使用简单 MA20 开仓门控，不使用利润回撤移动止盈。",
        ],
    },
    "g2_open_v1_sl5": {
        "strategy_code": "g2_open_v1_sl5",
        "strategy_name": "G2 Open V1 SL5",
        "status": "frozen_research",
        "run_dir": report_path("gen2_open_v1_attack_drawdown", "runs", "attack_tp10_sl5_tr_none"),
        "summary_report": report_path("gen2_open_v1_attack_drawdown_conclusion.md"),
        "state_report": report_path("gen2_open_v1_state_only_sweep_all", "state_only_conclusion.md"),
        "rule_lines": [
            "市场状态固定为 NORMAL。",
            "股票池来自 V4 参考池，要求 pullback_restart_rank100。",
            "30m 底分型突破高点并放量确认后开仓。",
            "每日最多新买 1 只，最多同时持有 2 只，单只最多 50%。",
            "买入后 30m 盘中 -5% 硬止损。",
            "上涨 +10% 时卖出 50%，剩余仓位持有至 3 个交易日时间退出。",
            "当前不启用移动止盈，不合并 OFF/PROBE/AGGRESSIVE 状态。",
        ],
    },
    "g2_v3_risk_cool_stop_cd5_skip": {
        "strategy_code": "g2_v3_risk_cool_stop_cd5_skip",
        "strategy_name": "G2 V3 User V2 two_stop_cd3_skip",
        "strategy_display_name": "G2 V3：User V2 + 两次止损后3日熔断",
        "status": "shadow_live_candidate",
        "run_dir": GEN2_CURRENT_RISK_COOL_RUN_DIR,
        "summary_report": GEN2_CURRENT_RISK_COOL_REPORT,
        "state_report": GEN2_CURRENT_RISK_COOL_TIMING_REPORT,
        "explanation_report": GEN2_CURRENT_RISK_COOL_REPORT,
        "rule_lines": [
            "固定名称：第二代策略 V3（G2 V3 User V2 two_stop_cd3_skip）。",
            "信号池使用 User V2 过滤后的实盘可见候选，来源为 d1_rank200 候选 + 30m 回踩重启确认。",
            "核心过滤：mom20 <= 30%，mom5 <= 14%，vol_ratio <= 1.90。",
            "偏好过滤：按流通市值分层识别未解决上方筹码压力，过滤下跌反抽叠加重压力样本。",
            "确认时涨幅要求：0% <= 相对昨日收盘涨幅 <= 12%。",
            "确认时 30m 量比要求：1.5 <= rt_30m_amount_ratio <= 7.0。",
            "组合引擎沿用 G2 攻击型规则：最多 2 仓、每日最多买 1 只、单票最多 50%。",
            "买入后 30m 盘中 -5% 硬止损。",
            "上涨 +10% 时卖出 50%，锁定一半利润。",
            "剩余半仓在 +10% 卖半后，若盘中跌破上一交易日最低点，则视为短线结构破坏并退出；最多持有 10 个交易日。",
            "动态熔断：累计 2 次真实触发 stop_loss_30m 后，后续 3 个交易日暂停新增 G2 V3 买入，只管理已有持仓。",
            "当前定位为影子实盘候选，不作为自动买入规则。",
        ],
    },
    "g2_v3_user_v2_two_stop_cd3_skip": {
        "strategy_code": "g2_v3_user_v2_two_stop_cd3_skip",
        "strategy_name": "G2 V3 User V2 two_stop_cd3_skip",
        "strategy_display_name": "G2 V3：User V2 + 两次止损后3日熔断",
        "status": "shadow_live_candidate",
        "run_dir": GEN2_CURRENT_RISK_COOL_RUN_DIR,
        "summary_report": GEN2_CURRENT_RISK_COOL_REPORT,
        "state_report": GEN2_CURRENT_RISK_COOL_TIMING_REPORT,
        "explanation_report": GEN2_CURRENT_RISK_COOL_REPORT,
        "rule_lines": [
            "固定名称：第二代策略 V3（G2 V3 User V2 two_stop_cd3_skip）。",
            "信号池使用 User V2 过滤后的实盘可见候选，来源为 d1_rank200 候选 + 30m 回踩重启确认。",
            "核心过滤：mom20 <= 30%，mom5 <= 14%，vol_ratio <= 1.90。",
            "偏好过滤：按流通市值分层识别未解决上方筹码压力，过滤下跌反抽叠加重压力样本。",
            "确认时涨幅要求：0% <= 相对昨日收盘涨幅 <= 12%。",
            "确认时 30m 量比要求：1.5 <= rt_30m_amount_ratio <= 7.0。",
            "组合引擎沿用 G2 攻击型规则：最多 2 仓、每日最多买 1 只、单票最多 50%。",
            "买入后 30m 盘中 -5% 硬止损。",
            "上涨 +10% 时卖出 50%，锁定一半利润。",
            "剩余半仓在 +10% 卖半后，若盘中跌破上一交易日最低点，则视为短线结构破坏并退出；最多持有 10 个交易日。",
            "动态熔断：累计 2 次真实触发 stop_loss_30m 后，后续 3 个交易日暂停新增 G2 V3 买入，只管理已有持仓。",
            "当前定位为影子实盘候选，不作为自动买入规则。",
        ],
    },
    "g2_alpha191_t1_keep90_stop_cd3_skip": {
        "strategy_code": "g2_alpha191_t1_keep90_stop_cd3_skip",
        "strategy_name": "G2 Alpha191 T-1 keep90 stop_cd3",
        "strategy_display_name": "G2 Alpha191 T-1 keep90 + stop_cd3_skip",
        "status": "shadow_live_candidate",
        "run_dir": GEN2_ALPHA191_T1_KEEP90_RUN_DIR,
        "summary_report": GEN2_ALPHA191_T1_KEEP90_REPORT,
        "state_report": GEN2_CURRENT_RISK_COOL_TIMING_REPORT,
        "explanation_report": GEN2_ALPHA191_T1_KEEP90_REPORT,
        "rule_lines": [
            "Base pool: G2 V3 User V2 risk_cool candidates.",
            "Factor timing: use T-1 confirmed Alpha191 values only.",
            "Gate factors: Alpha150 high, Alpha095 low, Alpha144 high.",
            "Scoring: convert each factor to train-distribution percentile, then average.",
            "Threshold: keep90, score >= train 10% quantile, threshold 0.361979.",
            "Execution policy: stop_cd3_skip, one real stop loss triggers 3 trading-day skip.",
            "Validation note: added for historical review and shadow verification; live update still requires selecting Alpha191 gate.",
        ],
    },
    "g2_alpha191_t1_keep80_stop_cd3_skip": {
        "strategy_code": "g2_alpha191_t1_keep80_stop_cd3_skip",
        "strategy_name": "G2 Alpha191 T-1 keep80 stop_cd3",
        "strategy_display_name": "G2 Alpha191 T-1 keep80 + stop_cd3_skip",
        "status": "shadow_live_candidate",
        "run_dir": GEN2_ALPHA191_T1_KEEP80_RUN_DIR,
        "summary_report": GEN2_ALPHA191_T1_KEEP80_REPORT,
        "state_report": GEN2_CURRENT_RISK_COOL_TIMING_REPORT,
        "explanation_report": REPO_ROOT / "docs" / "strategy-g2-alpha191-t1-keep80-stop-cd3.md",
        "rule_lines": [
            "Base pool: G2 V3 User V2 risk_cool candidates.",
            "Factor timing: use T-1 confirmed Alpha191 values only.",
            "Gate factors: Alpha150 high, Alpha095 low, Alpha144 high.",
            "Scoring: convert each factor to train-distribution percentile, then average.",
            "Threshold: keep80, score >= train 20% quantile, threshold 0.391927.",
            "Execution policy: stop_cd3_skip, one real stop loss triggers 3 trading-day skip.",
            "Positioning: offensive shadow-live candidate for promotion into the main G2 strategy after live validation.",
        ],
    },
    "g2_alpha191_score050_dd8_half": {
        "strategy_code": "g2_alpha191_score050_dd8_half",
        "strategy_name": "G2 Alpha191 score>=0.50 dd8_half",
        "strategy_display_name": "G2 Alpha191 score>=0.50 + dd8_half",
        "status": "shadow_live_candidate",
        "run_dir": GEN2_ALPHA191_SCORE050_DD8_RUN_DIR,
        "summary_report": GEN2_ALPHA191_DD_GUARD_REPORT,
        "state_report": GEN2_CURRENT_RISK_COOL_TIMING_REPORT,
        "explanation_report": GEN2_ALPHA191_DD_GUARD_REPORT,
        "rule_lines": [
            "Base pool: G2 V3 User V2 risk_cool candidates.",
            "Factor timing: use T-1 confirmed Alpha191 values only.",
            "Gate factors: Alpha150 high, Alpha095 low, Alpha144 high.",
            "Scoring: convert each factor to train-distribution percentile, then average.",
            "Threshold: alpha191_gate_score >= 0.50.",
            "Execution policy: stop_cd3_skip plus portfolio drawdown guard.",
            "Drawdown guard: when strategy equity is down 8% from its historical peak, new buys use half position until equity recovers.",
            "Positioning: offensive candidate; stronger return and Sharpe, but still needs shadow validation before live use.",
        ],
    },
    "g2_alpha191_score050_dd8_roll_half": {
        "strategy_code": "g2_alpha191_score050_dd8_roll_half",
        "strategy_name": "G2 Alpha191 score>=0.50 dd8_half roll_stop_half",
        "strategy_display_name": "G2 Alpha191 score>=0.50 + dd8_half + rolling_stop_half",
        "status": "shadow_live_candidate",
        "run_dir": GEN2_ALPHA191_SCORE050_DD8_ROLL_RUN_DIR,
        "summary_report": GEN2_ALPHA191_NEXT_GUARD_REPORT,
        "state_report": GEN2_CURRENT_RISK_COOL_TIMING_REPORT,
        "explanation_report": GEN2_ALPHA191_NEXT_GUARD_REPORT,
        "rule_lines": [
            "Base pool: G2 V3 User V2 risk_cool candidates.",
            "Factor timing: use T-1 confirmed Alpha191 values only.",
            "Gate factors: Alpha150 high, Alpha095 low, Alpha144 high.",
            "Scoring: convert each factor to train-distribution percentile, then average.",
            "Threshold: alpha191_gate_score >= 0.50.",
            "Execution policy: stop_cd3_skip plus portfolio drawdown guard.",
            "Drawdown guard: when strategy equity is down 8% from its historical peak, new buys use half position until equity recovers.",
            "Rolling stop guard: if 2 stop_loss_30m events occur within the latest 20 trading days, new buys use half position for 5 trading days.",
            "Positioning: next offensive shadow candidate; improves return, Sharpe and profit concentration versus plain dd8_half in the current full-window backtest.",
        ],
    },
    "g2_alpha191_overhead005_dd8_half": {
        "strategy_code": "g2_alpha191_overhead005_dd8_half",
        "strategy_name": "G2 Alpha191 score>=0.50 overhead<=5% dd8_half",
        "strategy_display_name": "G2 Alpha191 score>=0.50 + overhead<=5% + dd8_half",
        "status": "shadow_live_candidate",
        "run_dir": GEN2_ALPHA191_OVERHEAD005_DD8_RUN_DIR,
        "summary_report": GEN2_ALPHA191_DD_GUARD_REPORT,
        "state_report": GEN2_CURRENT_RISK_COOL_TIMING_REPORT,
        "explanation_report": GEN2_ALPHA191_DD_GUARD_REPORT,
        "rule_lines": [
            "Base pool: G2 V3 User V2 risk_cool candidates.",
            "Factor timing: use T-1 confirmed Alpha191 values only.",
            "Gate factors: Alpha150 high, Alpha095 low, Alpha144 high.",
            "Scoring: convert each factor to train-distribution percentile, then average.",
            "Threshold: alpha191_gate_score >= 0.50.",
            "Quality guard: overhead_pressure_amount_share <= 5%.",
            "Execution policy: stop_cd3_skip plus portfolio drawdown guard.",
            "Drawdown guard: when strategy equity is down 8% from its historical peak, new buys use half position until equity recovers.",
            "Positioning: balanced candidate; lower drawdown than original keep80 in the current full-window backtest.",
        ],
    },
}

GEN2_BACKTEST_DEFAULT_CODE = "g2_alpha191_volume5_keep80_runup"
GEN2_BACKTEST_FUTURE_LEAK_CODES = {
    # Source summary reports lookahead_safe=false for gen2_30m_fractal_restart_expanded_w2.
    "g2_attack_v1_prev_low",
    "g2_open_v1_sl5",
}
for _code in GEN2_BACKTEST_FUTURE_LEAK_CODES:
    GEN2_BACKTEST_RUNS.pop(_code, None)
GEN2_BACKTEST_RUNS = {
    GEN2_BACKTEST_DEFAULT_CODE: GEN2_BACKTEST_RUNS[GEN2_BACKTEST_DEFAULT_CODE],
}


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except Exception:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def _normalize_date(value: Any) -> str:
    if value is None or value == "":
        return ""
    dt = pd.to_datetime(value, errors="coerce")
    if pd.isna(dt):
        return ""
    return dt.strftime("%Y-%m-%d")


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    if isinstance(value, tuple):
        return [_sanitize(v) for v in value]
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    return value


def _format_pct(value: Any, digits: int = 2) -> Optional[str]:
    number = _to_float(value)
    if number is None:
        return None
    return f"{number * 100:.{digits}f}%"


def _load_gen2_open_signal_frame() -> pd.DataFrame:
    path = GEN2_OPEN_SIGNAL_SOURCE
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_parquet(path)
    except Exception as exc:
        logger.warning(f"load gen2 open signals failed: {path}: {exc}")
        return pd.DataFrame()
    if df.empty:
        return df
    rule = GEN2_OPEN_RULE_V1
    d = df[
        (df.get("pattern") == rule["pattern"])
        & (df.get("g2_open_state") == rule["g2_open_state"])
        & (df.get("trigger_type") == rule["trigger_type"])
    ].copy()
    if d.empty:
        return d
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    d = d.dropna(subset=["entry_date", "code"]).copy()
    d = d.sort_values(["entry_date", "v4_rank", "v4_score", "code"], ascending=[False, True, False, True])
    return d.reset_index(drop=True)


def _signal_row(row: Dict[str, Any]) -> Dict[str, Any]:
    ret_3d = row.get("entry_fwd_ret_3d")
    excess_3d = row.get("entry_excess_vs_csi1000_3d")
    confirm_dt = row.get("confirm_datetime")
    if isinstance(confirm_dt, pd.Timestamp):
        confirm_dt = confirm_dt.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "rule_id": GEN2_OPEN_RULE_V1["rule_id"],
        "signal_date": row.get("entry_date") or "",
        "source_trade_date": row.get("trade_date") or "",
        "code": str(row.get("code") or ""),
        "name": str(row.get("name") or ""),
        "v4_rank": _to_int(row.get("v4_rank"), 0),
        "v4_score": _to_float(row.get("v4_score")),
        "entry_price": _to_float(row.get("entry_price")),
        "confirm_datetime": confirm_dt,
        "fractal_low": _to_float(row.get("fractal_low")),
        "fractal_close": _to_float(row.get("fractal_close")),
        "confirm_amount": _to_float(row.get("confirm_amount")),
        "confirm_amount_ma5_prev": _to_float(row.get("confirm_amount_ma5_prev")),
        "volume_expand": bool(row.get("volume_expand")),
        "expected_hold_days": GEN2_OPEN_RULE_V1["hold_days"],
        "historical_ret_3d": _to_float(ret_3d),
        "historical_ret_3d_text": _format_pct(ret_3d),
        "historical_excess_3d": _to_float(excess_3d),
        "historical_excess_3d_text": _format_pct(excess_3d),
        "signal_reason": "NORMAL + rank100 pullback restart + 30m bottom-fractal break-high volume expansion",
    }


def _build_gen2_open_signals(selected_date: str, limit: int) -> Dict[str, Any]:
    d = _load_gen2_open_signal_frame()
    if d.empty:
        return {
            "available": False,
            "rule": GEN2_OPEN_RULE_V1,
            "source": str(GEN2_OPEN_SIGNAL_SOURCE),
            "selected_date": selected_date,
            "latest_signal_date": "",
            "exact_rows": [],
            "recent_rows": [],
            "message": "G2 open signal source is not available. Re-run the 30m fractal restart research first.",
        }

    selected = selected_date or str(d["entry_date"].max())
    eligible = d[d["entry_date"] <= selected].copy()
    if eligible.empty:
        return {
            "available": True,
            "rule": GEN2_OPEN_RULE_V1,
            "source": str(GEN2_OPEN_SIGNAL_SOURCE),
            "selected_date": selected,
            "latest_signal_date": "",
            "exact_rows": [],
            "recent_rows": [],
            "message": f"No G2 Open V1 signal on or before {selected}.",
        }

    exact = d[d["entry_date"] == selected].copy()
    recent = eligible.head(max(1, int(limit or 30))).copy()
    latest_signal_date = str(eligible["entry_date"].max())
    exact_rows = [_signal_row(row) for row in exact.to_dict("records")]
    recent_rows = [_signal_row(row) for row in recent.to_dict("records")]
    message = (
        f"{len(exact_rows)} exact G2 Open V1 signals on {selected}; "
        f"latest available signal date is {latest_signal_date}."
    )
    return {
        "available": True,
        "rule": GEN2_OPEN_RULE_V1,
        "source": str(GEN2_OPEN_SIGNAL_SOURCE),
        "selected_date": selected,
        "latest_signal_date": latest_signal_date,
        "exact_count": len(exact_rows),
        "recent_count": len(recent_rows),
        "exact_rows": exact_rows,
        "recent_rows": recent_rows,
        "message": message,
    }


def _load_report_rows(limit: int = 12) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for source in REPORT_SOURCES:
        path = source["path"]
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"load gen2 report failed: {path}: {exc}")
            continue
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            if not isinstance(item, dict):
                continue
            total_return = _to_float(item.get("total_return"))
            max_drawdown = _to_float(item.get("max_drawdown"))
            rows.append(
                {
                    "source_key": source["key"],
                    "source_label": source["label"],
                    "name": str(item.get("name") or item.get("strategy_name") or source["key"]),
                    "description": str(item.get("description") or ""),
                    "start_date": item.get("start_date") or "",
                    "end_date": item.get("end_date") or "",
                    "total_return": total_return,
                    "total_return_pct": None if total_return is None else round(total_return * 100, 2),
                    "max_drawdown": max_drawdown,
                    "max_drawdown_pct": None if max_drawdown is None else round(max_drawdown * 100, 2),
                    "sharpe": _to_float(item.get("sharpe")),
                    "trade_count": _to_int(item.get("trade_count", item.get("buy_count"))),
                    "win_rate": _to_float(item.get("win_rate", item.get("win_rate_sell_event"))),
                    "final_equity": _to_float(item.get("final_equity")),
                }
            )
    rows.sort(key=lambda row: (_to_float(row.get("total_return"), -999.0) or -999.0), reverse=True)
    return rows[:limit]


def _pattern_tags(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    rank = _to_int(row.get("rank"), 999)
    score = _to_float(row.get("score_total"), 0.0) or 0.0
    rank_change = _to_int(row.get("rank_change"), 0)
    status = str(row.get("rank_change_status") or "")
    mom5 = _to_float(row.get("mom5"), 0.0) or 0.0
    mom10 = _to_float(row.get("mom10"), 0.0) or 0.0
    r_mom5 = _to_float(row.get("r_mom5"), 0.0) or 0.0
    r_vol_ratio = _to_float(row.get("r_vol_ratio"), 0.0) or 0.0

    tags: List[Dict[str, Any]] = []
    if bool(row.get("entry_pass")):
        tags.append({"key": "entry_pass", "label": "入场阈值通过", "level": "primary"})
    if rank <= 15 and (status == "new" or rank_change >= 5):
        tags.append({"key": "rank_acceleration", "label": "排名加速", "level": "hot"})
    if rank <= 10 and mom5 >= 8.0 and mom10 >= 12.0:
        tags.append({"key": "momentum_leader", "label": "短线动量龙头", "level": "hot"})
    if r_mom5 >= 0.8 and r_vol_ratio >= 0.8:
        tags.append({"key": "volume_momentum", "label": "量价共振", "level": "watch"})
    if score >= 0.85 and rank <= 20:
        tags.append({"key": "high_score_core", "label": "高分核心池", "level": "primary"})
    return tags


def _score_gen2(row: Dict[str, Any], tags: List[Dict[str, Any]]) -> float:
    score = (_to_float(row.get("score_total"), 0.0) or 0.0) * 100.0
    rank = _to_int(row.get("rank"), 999)
    rank_bonus = max(0.0, 31.0 - float(rank)) * 0.35 if rank < 999 else 0.0
    tag_bonus = 0.0
    for tag in tags:
        if tag.get("level") == "hot":
            tag_bonus += 8.0
        elif tag.get("level") == "primary":
            tag_bonus += 5.0
        else:
            tag_bonus += 2.0
    return round(score + rank_bonus + tag_bonus, 4)


def _build_pattern_rows(v4_reference: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    source_rows = v4_reference.get("top30") or []
    if not isinstance(source_rows, list):
        source_rows = []

    rows: List[Dict[str, Any]] = []
    for item in source_rows:
        if not isinstance(item, dict):
            continue
        tags = _pattern_tags(item)
        gen2_score = _score_gen2(item, tags)
        status = "primary" if any(tag.get("level") in {"hot", "primary"} for tag in tags) else "watch"
        rows.append(
            {
                **item,
                "gen2_score": gen2_score,
                "pattern_status": status,
                "pattern_tags": tags,
                "pattern_summary": " / ".join(str(tag.get("label")) for tag in tags) if tags else "仅在V4参考池内，等待模式确认",
            }
        )
    rows.sort(key=lambda row: (-float(row.get("gen2_score") or 0), _to_int(row.get("rank"), 999)))
    return rows[:limit]


def _load_gen2_v4_reference(selected_date: str, limit: int) -> Dict[str, Any]:
    if not selected_date or not GEN2_CURRENT_V4_EVENT_DATASET.exists():
        return {
            "entry_score": None,
            "score_pool_size": 0,
            "candidate_count": 0,
            "top30": [],
            "message": "G2 V4 reference dataset is not available.",
        }
    try:
        df = pd.read_parquet(GEN2_CURRENT_V4_EVENT_DATASET)
    except Exception as exc:
        logger.warning(f"load G2 V4 reference dataset failed: {exc}")
        return {
            "entry_score": None,
            "score_pool_size": 0,
            "candidate_count": 0,
            "top30": [],
            "message": "Failed to load G2 V4 reference dataset.",
        }
    if df.empty or "trade_date" not in df.columns:
        return {
            "entry_score": None,
            "score_pool_size": 0,
            "candidate_count": 0,
            "top30": [],
            "message": "G2 V4 reference dataset is empty.",
        }
    d = df.copy()
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    prior_dates = sorted([str(x) for x in d["trade_date"].dropna().unique() if str(x) <= selected_date])
    if not prior_dates:
        prior_dates = sorted([str(x) for x in d["trade_date"].dropna().unique()])
    if not prior_dates:
        return {
            "entry_score": None,
            "score_pool_size": 0,
            "candidate_count": 0,
            "top30": [],
            "message": "G2 V4 reference dataset has no valid date.",
        }
    selected = prior_dates[-1]
    day = d[d["trade_date"].eq(selected)].copy()
    rank_col = "v4_rank" if "v4_rank" in day.columns else "rank"
    score_col = "v4_score" if "v4_score" in day.columns else "score_total"
    if rank_col not in day.columns:
        day[rank_col] = range(1, len(day) + 1)
    if score_col not in day.columns:
        day[score_col] = 0.0
    day[rank_col] = pd.to_numeric(day[rank_col], errors="coerce")
    day[score_col] = pd.to_numeric(day[score_col], errors="coerce")
    day = day.sort_values([rank_col, "code"], na_position="last").head(max(30, int(limit or 30)))
    rows: List[Dict[str, Any]] = []
    for _, item in day.iterrows():
        rows.append(
            {
                "rank": _to_int(item.get(rank_col), 999),
                "code": str(item.get("code") or ""),
                "name": str(item.get("name") or ""),
                "score_total": _to_float(item.get(score_col), 0.0),
                "entry_pass": bool(item.get("entry_pass")) if "entry_pass" in day.columns else True,
                "rank_change": _to_int(item.get("rank_change"), 0),
                "rank_change_status": str(item.get("rank_change_status") or ""),
                "mom5": _format_pct(item.get("mom5")),
                "mom10": _format_pct(item.get("mom10")),
                "mom20": _format_pct(item.get("mom20")),
                "r_mom5": _to_float(item.get("r_mom5"), 0.0),
                "r_vol_ratio": _to_float(item.get("r_vol_ratio"), 0.0),
                "trade_date": selected,
            }
        )
    return {
        "entry_score": None,
        "score_pool_size": int(len(day)),
        "candidate_count": int(len(rows)),
        "top30": rows[:30],
        "message": f"G2 V4 reference rows loaded from {selected}.",
    }


def _resolve_gen2_selected_date(signal_date: Optional[str]) -> tuple[str, str, str]:
    requested = _normalize_date(signal_date) if signal_date else ""
    latest_candidates: List[str] = []
    for path, col in [
        (GEN2_CURRENT_SHADOW_LEDGER, "entry_date"),
        (GEN2_OPEN_SIGNAL_SOURCE, "entry_date"),
        (GEN2_CURRENT_V4_EVENT_DATASET, "trade_date"),
    ]:
        span = _source_date_span(path, col)
        latest = str(span.get("max_date") or "")
        if latest:
            latest_candidates.append(latest)
    latest = max(latest_candidates) if latest_candidates else ""
    selected = requested or latest
    if requested and latest and requested > latest:
        selected = latest
    return requested, selected, latest


def _load_current_shadow_ledger() -> pd.DataFrame:
    if not GEN2_CURRENT_SHADOW_LEDGER.exists():
        return pd.DataFrame()
    try:
        d = pd.read_csv(GEN2_CURRENT_SHADOW_LEDGER)
    except Exception as exc:
        logger.warning(f"load current G2 shadow ledger failed: {exc}")
        return pd.DataFrame()
    if "entry_date" in d.columns:
        d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    if "confirm_datetime" in d.columns:
        d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    for col in [
        "day_signal_rank",
        "entry_price",
        "v4_rank",
        "v4_score",
        "mom5",
        "mom10",
        "mom20",
        "rt_return_from_d1_close",
        "rt_30m_amount_ratio",
        "outcome_fwd_ret_5d",
        "outcome_fwd_ret_10d",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d


def _source_date_span(path: Path, date_col: str) -> Dict[str, Any]:
    if not path.exists():
        return {"exists": False, "rows": 0, "min_date": "", "max_date": ""}
    try:
        if path.suffix.lower() == ".csv":
            df = pd.read_csv(path, usecols=lambda col: col == date_col)
        else:
            df = pd.read_parquet(path, columns=[date_col])
    except Exception as exc:
        logger.warning(f"load source date span failed: {path}: {exc}")
        return {"exists": True, "rows": 0, "min_date": "", "max_date": "", "error": str(exc)}
    if date_col not in df.columns or df.empty:
        return {"exists": True, "rows": int(len(df)), "min_date": "", "max_date": ""}
    dates = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d").dropna()
    return {
        "exists": True,
        "rows": int(len(df)),
        "min_date": str(dates.min()) if not dates.empty else "",
        "max_date": str(dates.max()) if not dates.empty else "",
    }


def build_gen2_data_freshness(requested_date: Optional[str] = None, selected_date: Optional[str] = None) -> Dict[str, Any]:
    requested = _normalize_date(requested_date) if requested_date else ""
    selected = _normalize_date(selected_date) if selected_date else ""
    sources = {
        "v4_event_dataset": _source_date_span(GEN2_CURRENT_V4_EVENT_DATASET, "trade_date"),
        "g2_30m_triggers": _source_date_span(GEN2_OPEN_SIGNAL_SOURCE, "entry_date"),
        "valid_signal_target": _source_date_span(GEN2_CURRENT_TARGET_SOURCE, "entry_date"),
        "risk_cool_base": _source_date_span(GEN2_CURRENT_RISK_COOL_SOURCE, "entry_date"),
        "shadow_ledger": _source_date_span(GEN2_CURRENT_SHADOW_LEDGER, "entry_date"),
    }
    latest_daily = str(sources.get("v4_event_dataset", {}).get("max_date") or "")
    latest_trigger = str(sources.get("g2_30m_triggers", {}).get("max_date") or "")
    latest_valid = str(sources.get("valid_signal_target", {}).get("max_date") or "")
    latest_shadow = str(sources.get("shadow_ledger", {}).get("max_date") or "")
    notes: List[str] = []
    if latest_daily and latest_shadow and latest_daily > latest_shadow:
        notes.append(
            f"V4日线事件集已到 {latest_daily}，但G2 two_stop_cd3_skip影子台账最新有效信号仍是 {latest_shadow}。"
        )
    if latest_trigger and latest_valid and latest_trigger > latest_valid:
        notes.append(
            f"G2 30m原始触发已到 {latest_trigger}，但通过intraday NORMAL before-confirm过滤的有效信号仍停在 {latest_valid}。"
        )
    if requested and selected and selected < requested:
        notes.append(f"请求日期 {requested} 没有有效影子台账，页面已回退到最近有效日期 {selected}。")
    if not notes:
        notes.append("当前页面日期与G2有效信号源一致。")
    return {
        "requested_date": requested,
        "selected_date": selected,
        "latest_daily_date": latest_daily,
        "latest_trigger_date": latest_trigger,
        "latest_valid_signal_date": latest_valid,
        "latest_shadow_date": latest_shadow,
        "sources": sources,
        "notes": notes,
    }


def _current_shadow_tags(row: pd.Series) -> List[Dict[str, Any]]:
    tags: List[Dict[str, Any]] = []
    status = str(row.get("shadow_status") or "")
    if status == "executed":
        tags.append({"key": "executed", "label": "回测执行", "level": "hot"})
    elif status.startswith("suspended"):
        tags.append({"key": "suspended", "label": "熔断暂停", "level": "watch"})
    else:
        tags.append({"key": "observable", "label": "可观察", "level": "primary"})
    amount_ratio = _to_float(row.get("rt_30m_amount_ratio"), 0.0) or 0.0
    if amount_ratio >= 2.5:
        tags.append({"key": "amount_expand", "label": "30m放量", "level": "primary"})
    ret = _to_float(row.get("rt_return_from_d1_close"), 0.0) or 0.0
    if 0.0 <= ret <= 0.06:
        tags.append({"key": "clean_intraday_gain", "label": "涨幅适中", "level": "primary"})
    elif ret > 0.09:
        tags.append({"key": "stretch_watch", "label": "涨幅偏高", "level": "watch"})
    return tags


def _build_current_lab_rows(signal_date: str, limit: int) -> tuple[List[Dict[str, Any]], str, Dict[str, Any]]:
    d = _load_current_shadow_ledger()
    if d.empty or "entry_date" not in d.columns:
        return [], "", {"score_pool_size": 0, "candidate_count": 0, "message": "G2 User V2 影子台账尚未生成。"}
    all_dates = sorted([str(x) for x in d["entry_date"].dropna().unique() if str(x)])
    selected_date = all_dates[-1]
    requested = _normalize_date(signal_date) if signal_date else ""
    if requested:
        prior = [x for x in all_dates if x <= requested]
        selected_date = prior[-1] if prior else all_dates[-1]
    day = d[d["entry_date"].eq(selected_date)].copy()
    priority = {"executed": 0, "observable": 1, "suspended_by_two_stop_cd3": 2, "suspended_by_stop_cd5": 2}
    status_series = day["shadow_status"] if "shadow_status" in day.columns else pd.Series("", index=day.index)
    day["_priority"] = status_series.map(lambda x: priority.get(str(x), 9))
    day = day.sort_values(["_priority", "day_signal_rank", "confirm_datetime"], na_position="last").head(limit)
    rows: List[Dict[str, Any]] = []
    for _, item in day.iterrows():
        tags = _current_shadow_tags(item)
        status = "primary" if str(item.get("shadow_status") or "") in {"executed", "observable"} else "watch"
        score = (_to_float(item.get("v4_score"), 0.0) or 0.0) * 100.0
        rank = _to_int(item.get("v4_rank"), 999)
        rank_bonus = max(0.0, 101.0 - float(rank)) * 0.05 if rank < 999 else 0.0
        rows.append(
            {
                "rank": rank,
                "rank_change_status": str(item.get("rank_change_status") or ""),
                "rank_change_text": str(item.get("shadow_status") or "observable"),
                "code": str(item.get("code") or ""),
                "name": str(item.get("name") or ""),
                "gen2_score": round(score + rank_bonus, 4),
                "score_total": _to_float(item.get("v4_score")),
                "mom5": _format_pct(item.get("mom5")),
                "mom10": _format_pct(item.get("mom10")),
                "entry_date": str(item.get("entry_date") or ""),
                "confirm_datetime": str(item.get("confirm_datetime") or ""),
                "entry_price": _to_float(item.get("entry_price")),
                "pattern_status": status,
                "pattern_tags": tags,
                "pattern_summary": str(item.get("execution_note") or "G2 User V2 two_stop_cd3 影子候选"),
            }
        )
    meta = {
        "score_pool_size": int(len(day)),
        "candidate_count": int(len(day[~status_series.astype(str).str.startswith("suspended")])),
        "top30_count": int(len(day)),
        "message": "当前试验台读取 G2 V3 User V2 two_stop_cd3_skip 影子台账。",
    }
    return rows, selected_date, meta


def build_gen2_strategy_lab(signal_date: Optional[str] = None, limit: int = 30) -> Dict[str, Any]:
    selected_limit = max(10, min(int(limit or 30), 50))
    requested_date, selected_date, latest_trade_date = _resolve_gen2_selected_date(signal_date)
    v4_reference = _load_gen2_v4_reference(selected_date, selected_limit)
    current_rows, current_selected_date, current_meta = _build_current_lab_rows(str(signal_date or selected_date or ""), selected_limit)
    pattern_rows = current_rows or _build_pattern_rows(v4_reference, selected_limit)
    primary_rows = [row for row in pattern_rows if row.get("pattern_status") == "primary"]
    report_rows = _load_report_rows()
    open_signals = _build_gen2_open_signals(str(current_selected_date or selected_date or signal_date or ""), selected_limit)
    freshness = build_gen2_data_freshness(
        requested_date or signal_date or "",
        current_selected_date or selected_date or "",
    )
    available = bool(pattern_rows or open_signals.get("available") or latest_trade_date)

    return _sanitize(
        {
            "available": available,
            "message": current_meta.get("message") or v4_reference.get("message") or "",
            "meta": GEN2_META,
            "requested_date": requested_date or signal_date or "",
            "selected_date": current_selected_date or selected_date or "",
            "latest_trade_date": latest_trade_date or "",
            "snapshot_status": "gen2_sources",
            "legacy_regime": {},
            "v4_reference": {
                "entry_score": v4_reference.get("entry_score"),
                "score_pool_size": current_meta.get("score_pool_size", v4_reference.get("score_pool_size", 0)),
                "candidate_count": current_meta.get("candidate_count", v4_reference.get("candidate_count", 0)),
                "top30_count": current_meta.get("top30_count", len(v4_reference.get("top30") or [])),
                "message": current_meta.get("message") or v4_reference.get("message") or "",
            },
            "patterns": {
                "rows": pattern_rows,
                "primary_rows": primary_rows,
                "primary_count": len(primary_rows),
                "rules": [
                    "以 G2 V3 User V2 two_stop_cd3_skip 影子台账为当前观察池。",
                    "正常候选来自 User V2 压力过滤 + 30m 回踩重启确认；熔断暂停样本只观察，不新增买入。",
                    "当前阶段只输出研究/影子信号，不直接修改实盘持仓。",
                ],
            },
            "open_signals": open_signals,
            "data_freshness": freshness,
            "reports": report_rows,
            "next_steps": [
                "继续观察 two_stop_cd3_skip 的影子盘命中质量。",
                "把尝试信号到突破确认做成观察到转正的状态机。",
                "验证稳定后再接入实盘买入观察池，而不是直接接入自动交易。",
            ],
        }
    )


def build_gen2_open_signals(signal_date: Optional[str] = None, limit: int = 30) -> Dict[str, Any]:
    selected_limit = max(10, min(int(limit or 30), 100))
    selected_date = str(signal_date or "")
    if not selected_date:
        _, selected_date, _ = _resolve_gen2_selected_date(None)
    return _sanitize(_build_gen2_open_signals(selected_date, selected_limit))


def _read_csv_records(path: Path, limit: int = 0) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    df = pd.read_csv(path)
    if limit and limit > 0:
        df = df.head(limit)
    return df.to_dict("records")


def _read_json_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"load json failed: {path}: {exc}")
        return {}


def _datetime_key(value: Any) -> str:
    if value is None or value == "":
        return ""
    dt = pd.to_datetime(value, errors="coerce")
    if pd.isna(dt):
        return str(value or "").strip()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _buy_strategy_meta(signal: Dict[str, Any]) -> Dict[str, str]:
    source_family = str(signal.get("source_family") or "").strip()
    signal_family = str(signal.get("signal_family") or "").strip()
    buy_logic = str(signal.get("g2_v2_buy_logic") or "").strip()
    source = str(signal.get("source") or "").strip()

    if source_family == "big_bull" or "big_bull" in buy_logic or "breakout" in signal_family:
        return {
            "value": "breakout_big_bull_sector",
            "label": "突破主线",
            "detail": "大阳二次突破 + 盘中强度 + 板块扩散",
            "type": "danger",
        }
    if source_family == "volume5" and "sector_score_bonus" in buy_logic:
        return {
            "value": "volume5_sector_bonus",
            "label": "volume5主线+板块",
            "detail": "Alpha191 volume5 弱过滤 + runup<=100% + 板块扩散加分",
            "type": "success",
        }
    if source_family == "volume5" or "volume5" in buy_logic or "volume5" in signal_family:
        return {
            "value": "volume5_main",
            "label": "volume5主线",
            "detail": "Alpha191 volume5 弱过滤 + runup<=100%",
            "type": "primary",
        }
    return {
        "value": source_family or source or "unknown",
        "label": "未知来源",
        "detail": buy_logic or signal_family or source,
        "type": "info",
    }


def _enrich_trades_with_buy_strategy(rows: List[Dict[str, Any]], signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    signal_map: Dict[str, Dict[str, Any]] = {}
    for signal in signals:
        key = f"{str(signal.get('code') or '').strip()}|{_datetime_key(signal.get('confirm_datetime'))}"
        if key.strip("|"):
            signal_map[key] = signal

    for row in rows:
        key = f"{str(row.get('code') or '').strip()}|{_datetime_key(row.get('buy_datetime') or row.get('buy_date'))}"
        signal = signal_map.get(key) or {}
        meta = _buy_strategy_meta(signal)
        row["buy_strategy"] = meta.get("value")
        row["buy_strategy_label"] = meta.get("label")
        row["buy_strategy_detail"] = meta.get("detail")
        row["buy_strategy_type"] = meta.get("type")
        row["buy_source_family"] = str(signal.get("source_family") or "")
        row["buy_signal_family"] = str(signal.get("signal_family") or "")
        row["buy_logic"] = str(signal.get("g2_v2_buy_logic") or "")
        row["buy_sector_name"] = str(signal.get("l3_sector_name") or signal.get("l2_sector_name") or signal.get("l1_sector_name") or "")
        row["buy_sector_strong3_ratio"] = _to_float(signal.get("l3_rt_strong3_ratio"))
        row["buy_rt_return_from_d1_close"] = _to_float(signal.get("rt_return_from_d1_close"))
        row["buy_rt_breakout_vs_box_top"] = _to_float(signal.get("rt_breakout_vs_box_top"))
    return rows


def _trade_key(row: Dict[str, Any]) -> str:
    parts = [
        row.get("code"),
        row.get("buy_datetime") or row.get("buy_date"),
        row.get("sell_datetime") or row.get("sell_date"),
        row.get("sell_ratio"),
        row.get("exit_reason"),
    ]
    return "|".join(str(item or "").strip() for item in parts)


def _load_trade_classifications() -> Dict[str, Any]:
    raw = _read_json_file(GEN2_TRADE_CLASSIFICATION_PATH)
    if isinstance(raw.get("items"), dict):
        return raw
    return {"version": 1, "items": {}}


def _save_trade_classifications(data: Dict[str, Any]) -> None:
    GEN2_TRADE_CLASSIFICATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = GEN2_TRADE_CLASSIFICATION_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(GEN2_TRADE_CLASSIFICATION_PATH)


def _classification_meta(value: str) -> Dict[str, str]:
    selected = str(value or "")
    for item in GEN2_TRADE_CLASSIFICATIONS:
        if item.get("value") == selected:
            return dict(item)
    return {"value": selected, "label": selected or "未分类", "type": "info"}


def _apply_trade_classifications(strategy_code: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    data = _load_trade_classifications()
    items = data.get("items") if isinstance(data, dict) else {}
    if not isinstance(items, dict):
        items = {}
    for row in rows:
        key = _trade_key(row)
        full_key = f"{strategy_code}:{key}"
        saved = items.get(full_key) if isinstance(items.get(full_key), dict) else {}
        classification = str(saved.get("classification") or "")
        meta = _classification_meta(classification)
        row["trade_key"] = key
        row["classification"] = classification
        row["classification_label"] = meta.get("label")
        row["classification_type"] = meta.get("type")
        row["classification_note"] = str(saved.get("note") or "")
        row["classification_updated_at"] = str(saved.get("updated_at") or "")
    return rows


def update_gen2_trade_classification(payload: Dict[str, Any]) -> Dict[str, Any]:
    strategy_code = str(payload.get("strategy_code") or GEN2_BACKTEST_DEFAULT_CODE).strip()
    trade_key = str(payload.get("trade_key") or "").strip()
    if not trade_key:
        trade_key = _trade_key(payload)
    classification = str(payload.get("classification") or "").strip()
    note = str(payload.get("note") or "").strip()
    valid_values = {str(item.get("value") or "") for item in GEN2_TRADE_CLASSIFICATIONS}
    if not trade_key:
        return _sanitize({"ok": False, "message": "trade_key is required"})
    if classification not in valid_values:
        return _sanitize({"ok": False, "message": f"unsupported classification: {classification}"})

    full_key = f"{strategy_code}:{trade_key}"
    with GEN2_TRADE_CLASSIFICATION_LOCK:
        data = _load_trade_classifications()
        items = data.setdefault("items", {})
        if classification or note:
            items[full_key] = {
                "strategy_code": strategy_code,
                "trade_key": trade_key,
                "classification": classification,
                "note": note,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        else:
            items.pop(full_key, None)
        _save_trade_classifications(data)

    meta = _classification_meta(classification)
    return _sanitize(
        {
            "ok": True,
            "strategy_code": strategy_code,
            "trade_key": trade_key,
            "classification": classification,
            "classification_label": meta.get("label"),
            "classification_type": meta.get("type"),
            "note": note,
        }
    )


def _load_gen2_state_frame() -> pd.DataFrame:
    path = GEN2_OPEN_STATE_DAILY_SOURCE
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
    except Exception as exc:
        logger.warning(f"load gen2 open-state daily failed: {path}: {exc}")
        return pd.DataFrame()
    if df.empty or "trade_date" not in df.columns:
        return pd.DataFrame()
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["trade_date"]).sort_values("trade_date").reset_index(drop=True)
    return df


def _load_gen2_state_sweep_rows() -> List[Dict[str, Any]]:
    raw = _read_json_file(GEN2_OPEN_STATE_SWEEP_SUMMARY)
    rows = raw.get("rows") if isinstance(raw, dict) else []
    if not isinstance(rows, list):
        return []
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        result.append(
            {
                "state": str(row.get("state") or ""),
                "signal_count": _to_int(row.get("signal_count")),
                "trade_count": _to_int(row.get("trade_count")),
                "total_return": _to_float(row.get("total_return")),
                "benchmark_return": _to_float(row.get("benchmark_return")),
                "excess_return": _to_float(row.get("excess_return")),
                "max_drawdown": _to_float(row.get("max_drawdown")),
                "win_rate": _to_float(row.get("win_rate")),
                "avg_trade_return": _to_float(row.get("avg_trade_return")),
            }
        )
    return result


def _gen2_timing_index_options() -> List[Dict[str, str]]:
    return [dict(item) for item in GEN2_TIMING_INDEX_OPTIONS.values()]


def _resolve_gen2_timing_index(index_code: Optional[str]) -> Dict[str, str]:
    code = str(index_code or "").strip().upper()
    return GEN2_TIMING_INDEX_OPTIONS.get(code) or GEN2_TIMING_INDEX_OPTIONS[GEN2_TIMING_DEFAULT_INDEX_CODE]


def _load_index_kline(code: str, start_date: str, end_date: str, fallback_name: str) -> Dict[str, Any]:
    if clickhouse_available():
        df = clickhouse_query_df(
            """
            SELECT k.code, s.name, k.trade_date, k.open, k.high, k.low, k.close
            FROM kline_daily k
            JOIN stocks s ON s.code = k.code
            WHERE k.code = ?
              AND k.trade_date BETWEEN ?::DATE AND ?::DATE
            ORDER BY k.trade_date
            """,
            [code, start_date, end_date],
        )
    else:
        sql = text(
            """
            SELECT k.code, s.name, k.trade_date, k.open, k.high, k.low, k.close
            FROM kline_daily k
            JOIN stocks s ON s.code = k.code
            WHERE k.code = :code
              AND k.trade_date BETWEEN :start_date AND :end_date
            ORDER BY k.trade_date
            """
        )
        with db.engine.connect() as conn:
            df = pd.read_sql(sql, conn, params={"code": code, "start_date": start_date, "end_date": end_date})
    if df.empty:
        return {"code": code, "name": fallback_name, "daily": []}
    df["trade_date"] = df["trade_date"].astype(str).str[:10]
    daily = [
        {
            "date": str(row["trade_date"]),
            "open": _to_float(row.get("open")),
            "high": _to_float(row.get("high")),
            "low": _to_float(row.get("low")),
            "close": _to_float(row.get("close")),
        }
        for _, row in df.iterrows()
    ]
    return {"code": code, "name": str(df["name"].iloc[0]) if "name" in df.columns else fallback_name, "daily": daily}


def _build_gen2_state_segments(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not rows:
        return []
    segments: List[Dict[str, Any]] = []
    start_idx = 0
    current = str(rows[0].get("g2_open_state") or "")
    for idx, row in enumerate(rows[1:], start=1):
        state = str(row.get("g2_open_state") or "")
        if state == current:
            continue
        segments.append(_build_gen2_state_segment(rows, start_idx, idx - 1, current))
        start_idx = idx
        current = state
    segments.append(_build_gen2_state_segment(rows, start_idx, len(rows) - 1, current))
    return [item for item in segments if item]


def _build_gen2_state_segment(rows: List[Dict[str, Any]], start_idx: int, end_idx: int, state: str) -> Dict[str, Any]:
    part = rows[start_idx : end_idx + 1]
    if not part:
        return {}
    start_close = _to_float(part[0].get("close"))
    end_close = _to_float(part[-1].get("close"))
    segment_return = end_close / start_close - 1.0 if start_close and end_close else None
    return {
        "state": state,
        "state_label": GEN2_OPEN_STATE_DEFINITIONS.get(state, {}).get("label", state),
        "start_date": part[0].get("date"),
        "end_date": part[-1].get("date"),
        "mid_date": part[len(part) // 2].get("date"),
        "trade_days": len(part),
        "segment_return": segment_return,
    }


def _build_gen2_signal_markers(start_date: str, end_date: str, index_by_date: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    d = _load_gen2_open_signal_frame()
    if d.empty:
        return []
    window = d[(d["entry_date"] >= start_date) & (d["entry_date"] <= end_date)].copy()
    if window.empty:
        return []
    window = window.sort_values(["entry_date", "v4_rank", "v4_score", "code"], ascending=[True, True, False, True])
    markers = []
    for row in window.to_dict("records"):
        date_text = str(row.get("entry_date") or "")
        day = index_by_date.get(date_text)
        if not day:
            continue
        markers.append(
            {
                "date": date_text,
                "side": "OPEN",
                "count": 1,
                "price": _to_float(day.get("low"), row.get("entry_price")),
                "name": f"{row.get('code', '')} {row.get('name', '')}".strip(),
                "reason": "NORMAL + rank100 pullback restart + 30m bottom-fractal break-high volume expansion",
            }
        )
    return markers


def _state_condition_rows(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    state = str(row.get("g2_open_state") or "")
    return [
        {
            "label": "中证1000站上 MA20",
            "pass": bool(row.get("active_index_above_ma20")),
            "value": "是" if bool(row.get("active_index_above_ma20")) else "否",
            "reason": "G2 主开仓代理指数是否进入可交易区域。",
        },
        {
            "label": "中证1000 MA20 斜率向上",
            "pass": bool(row.get("active_index_slope_up")),
            "value": "是" if bool(row.get("active_index_slope_up")) else "否",
            "reason": "用于区分刚修复与正常顺风状态。",
        },
        {
            "label": "中证500确认",
            "pass": bool(row.get("confirm_index_above_ma20")),
            "value": "是" if bool(row.get("confirm_index_above_ma20")) else "否",
            "reason": "确认活跃中盘环境是否同步转强。",
        },
        {
            "label": "上证风险确认通过",
            "pass": bool(row.get("risk_index_ok")),
            "value": "是" if bool(row.get("risk_index_ok")) else "否",
            "reason": "避免系统性风险明显偏弱时新增仓位。",
        },
        {
            "label": "全市场 MA20 广度",
            "pass": (_to_float(row.get("breadth_ma20"), 0.0) or 0.0) >= (0.45 if state == "NORMAL" else 0.60 if state == "AGGRESSIVE" else 0.30),
            "value": None if _to_float(row.get("breadth_ma20")) is None else f"{(_to_float(row.get('breadth_ma20')) or 0) * 100:.2f}%",
            "reason": "广度决定仓位强弱，NORMAL 基线为 45%。",
        },
        {
            "label": "因子风险偏好",
            "pass": bool(row.get("factor_risk_on")),
            "value": "通过" if bool(row.get("factor_risk_on")) else "未通过",
            "reason": "用于确认是否允许升级到 AGGRESSIVE。",
        },
    ]


def build_gen2_timing(signal_date: Optional[str] = None, curve_days: int = 420, index_code: Optional[str] = None) -> Dict[str, Any]:
    selected_index = _resolve_gen2_timing_index(index_code)
    state_df = _load_gen2_state_frame()
    if state_df.empty:
        return _sanitize(
            {
                "available": False,
                "message": f"G2 open-state daily source is not available: {GEN2_OPEN_STATE_DAILY_SOURCE}",
                "state_definitions": GEN2_OPEN_STATE_DEFINITIONS,
                "selected_index_code": selected_index["code"],
                "available_indices": _gen2_timing_index_options(),
            }
        )

    selected = str(signal_date or state_df["trade_date"].max())
    eligible = state_df[state_df["trade_date"] <= selected].copy()
    if eligible.empty:
        return _sanitize({"available": False, "message": f"No G2 timing state on or before {selected}.", "requested_date": selected})
    selected_row = eligible.iloc[-1].to_dict()
    selected_date = str(selected_row.get("trade_date") or selected)
    start_idx = max(0, len(eligible) - max(80, min(int(curve_days or 360), 1500)))
    chart_state_df = eligible.iloc[start_idx:].copy()
    start_date = str(chart_state_df["trade_date"].iloc[0])
    end_date = str(chart_state_df["trade_date"].iloc[-1])
    index_payload = _load_index_kline(selected_index["code"], start_date, end_date, selected_index["name"])
    index_by_date = {item.get("date"): item for item in index_payload.get("daily") or []}

    daily_rows: List[Dict[str, Any]] = []
    for _, row in chart_state_df.iterrows():
        date_text = str(row["trade_date"])
        candle = index_by_date.get(date_text) or {}
        state = str(row.get("g2_open_state") or "")
        close = _to_float(candle.get("close"), _to_float(row.get("csi1000_close")))
        daily_rows.append(
            {
                "date": date_text,
                "open": _to_float(candle.get("open"), close),
                "high": _to_float(candle.get("high"), close),
                "low": _to_float(candle.get("low"), close),
                "close": close,
                "g2_open_state": state,
                "state_label": GEN2_OPEN_STATE_DEFINITIONS.get(state, {}).get("label", state),
                "target_exposure_min": _to_float(row.get("target_exposure_min")),
                "target_exposure_max": _to_float(row.get("target_exposure_max")),
                "breadth_ma20": _to_float(row.get("breadth_ma20")),
                "csi1000_ma20": _to_float(row.get("csi1000_ma20")),
                "csi1000_ma60": _to_float(row.get("csi1000_ma60")),
                "csi1000_ma120": _to_float(row.get("csi1000_ma120")),
                "csi1000_ma20_slope5": _to_float(row.get("csi1000_ma20_slope5")),
                "active_index_above_ma20": bool(row.get("active_index_above_ma20")),
                "active_index_slope_up": bool(row.get("active_index_slope_up")),
                "confirm_index_above_ma20": bool(row.get("confirm_index_above_ma20")),
                "growth_index_above_ma20": bool(row.get("growth_index_above_ma20")),
                "risk_index_ok": bool(row.get("risk_index_ok")),
                "breadth_improve3": bool(row.get("breadth_improve3")),
                "factor_risk_on": bool(row.get("factor_risk_on")),
            }
        )

    open_signals = _build_gen2_open_signals(selected_date, 80)
    exact_count = _to_int(open_signals.get("exact_count"), len(open_signals.get("exact_rows") or []))
    current_state = str(selected_row.get("g2_open_state") or "")
    state_def = GEN2_OPEN_STATE_DEFINITIONS.get(current_state, {})
    state_allows = bool(state_def.get("can_open"))
    is_open_day = bool(state_allows and exact_count > 0)
    not_open_reasons = []
    if not state_allows:
        not_open_reasons.append(f"当前状态为 {current_state}，当前 G2 Open V1 只允许 NORMAL 开仓。")
    if state_allows and exact_count <= 0:
        not_open_reasons.append("当日没有满足 NORMAL + rank100 pullback restart + 30m 底分型突破放量确认的开仓信号。")
    if current_state == "AGGRESSIVE":
        not_open_reasons.append("AGGRESSIVE 暂不并入该买点；历史验证显示该买点在 AGGRESSIVE 下收益和回撤不稳定。")

    selected_daily = next((item for item in daily_rows if item.get("date") == selected_date), daily_rows[-1] if daily_rows else {})
    signal_markers = _build_gen2_signal_markers(start_date, end_date, index_by_date)

    state_counts = {str(k): int(v) for k, v in chart_state_df["g2_open_state"].value_counts().to_dict().items()}
    return _sanitize(
        {
            "available": True,
            "message": "",
            "requested_date": signal_date or "",
            "selected_date": selected_date,
            "latest_state_date": str(state_df["trade_date"].max()),
            "selected_index_code": selected_index["code"],
            "available_indices": _gen2_timing_index_options(),
            "state_definitions": GEN2_OPEN_STATE_DEFINITIONS,
            "state_backtest_rows": _load_gen2_state_sweep_rows(),
            "current": {
                "state": current_state,
                "state_label": state_def.get("label", current_state),
                "state_title": state_def.get("title", ""),
                "state_meaning": state_def.get("meaning", ""),
                "target_exposure": state_def.get("target_exposure", ""),
                "is_open_day": is_open_day,
                "can_observe_open": state_allows,
                "exact_signal_count": exact_count,
                "decision_title": "今日是 G2 开仓日" if is_open_day else "今日不是 G2 开仓日",
                "decision_message": "当前状态为 NORMAL 且当日存在 G2 Open V1 开仓信号。" if is_open_day else "；".join(not_open_reasons),
                "not_open_reasons": not_open_reasons,
                "condition_rows": _state_condition_rows(selected_row),
                "daily": selected_daily,
            },
            "chart": {
                "index": index_payload,
                "daily": daily_rows,
                "segments": _build_gen2_state_segments(daily_rows),
                "state_counts": state_counts,
                "signal_markers": signal_markers,
            },
            "open_signals": open_signals,
            "rule": GEN2_OPEN_RULE_V1,
            "sources": {
                "state_daily": str(GEN2_OPEN_STATE_DAILY_SOURCE),
                "state_sweep_summary": str(GEN2_OPEN_STATE_SWEEP_SUMMARY),
                "open_signal_source": str(GEN2_OPEN_SIGNAL_SOURCE),
            },
        }
    )


def _metric_pct(summary: Dict[str, Any], key: str) -> Optional[str]:
    return _format_pct(summary.get(key))


def _metric_sharpe(curve_rows: List[Dict[str, Any]]) -> Optional[float]:
    if len(curve_rows) < 3:
        return None
    try:
        equity = pd.to_numeric(pd.DataFrame(curve_rows).get("strategy_equity"), errors="coerce")
        returns = equity.pct_change().dropna()
        if len(returns) < 2:
            return None
        std = float(returns.std(ddof=1))
        if std <= 0 or not math.isfinite(std):
            return None
        return float(returns.mean() / std * math.sqrt(252))
    except Exception:
        return None


def _profit_concentration_metrics(summary: Dict[str, Any], trade_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        final_equity = _to_float(summary.get("final_equity"))
        total_return = _to_float(summary.get("total_return"))
        if final_equity is None or total_return is None or total_return <= -0.999999:
            return {}
        initial_equity = final_equity / (1.0 + total_return)
        total_pnl = final_equity - initial_equity
        if not math.isfinite(total_pnl) or abs(total_pnl) <= 1e-9:
            return {}
        trades_df = pd.DataFrame(trade_rows)
        if trades_df.empty or "pnl" not in trades_df.columns:
            return {}
        pnl = pd.to_numeric(trades_df["pnl"], errors="coerce").dropna()
        winners = pnl[pnl > 0].sort_values(ascending=False)
        if winners.empty:
            return {
                "total_pnl": total_pnl,
                "top1_profit_share": None,
                "top3_profit_share": None,
                "top5_profit_share": None,
                "remove_top1_return": total_return,
                "remove_top3_return": total_return,
                "remove_top5_return": total_return,
                "profit_concentration_level": "none",
            }
        top1 = float(winners.head(1).sum())
        top3 = float(winners.head(3).sum())
        top5 = float(winners.head(5).sum())

        def _share(value: float) -> Optional[float]:
            if total_pnl <= 0:
                return None
            return float(value / total_pnl)

        def _remove_return(value: float) -> Optional[float]:
            if initial_equity <= 0:
                return None
            return float((final_equity - value) / initial_equity - 1.0)

        top3_share = _share(top3)
        top5_share = _share(top5)
        level = "low"
        if top3_share is not None and top3_share >= 0.40:
            level = "high"
        elif top5_share is not None and top5_share >= 0.50:
            level = "high"
        elif top3_share is not None and top3_share >= 0.25:
            level = "medium"
        elif top5_share is not None and top5_share >= 0.35:
            level = "medium"

        return {
            "total_pnl": total_pnl,
            "top1_profit_share": _share(top1),
            "top1_profit_share_text": _format_pct(_share(top1)),
            "top3_profit_share": top3_share,
            "top3_profit_share_text": _format_pct(top3_share),
            "top5_profit_share": top5_share,
            "top5_profit_share_text": _format_pct(top5_share),
            "remove_top1_return": _remove_return(top1),
            "remove_top1_return_text": _format_pct(_remove_return(top1)),
            "remove_top3_return": _remove_return(top3),
            "remove_top3_return_text": _format_pct(_remove_return(top3)),
            "remove_top5_return": _remove_return(top5),
            "remove_top5_return_text": _format_pct(_remove_return(top5)),
            "profit_concentration_level": level,
        }
    except Exception:
        return {}


def build_gen2_backtest_history(strategy_code: str = GEN2_BACKTEST_DEFAULT_CODE) -> Dict[str, Any]:
    code = str(strategy_code or GEN2_BACKTEST_DEFAULT_CODE)
    config = GEN2_BACKTEST_RUNS.get(code)
    if not config:
        return _sanitize({"available": False, "message": f"未知的策略版本: {code}", "strategy_code": code})

    run_dir = Path(config["run_dir"])
    summary_path = run_dir / "summary.json"
    curve_path = run_dir / "equity_curve.csv"
    trades_path = run_dir / "trades.csv"
    signals_path = run_dir / "signals.csv"
    segment_path = run_dir / "segment_summary.csv"
    decision_ledger_path = run_dir / "decision_ledger.csv"
    if not summary_path.exists() or not curve_path.exists() or not trades_path.exists():
        return _sanitize(
            {
                "available": False,
                "message": f"回测结果不存在或不完整: {run_dir}",
                "strategy_code": code,
                "run_dir": str(run_dir),
            }
        )

    summary = _read_json_file(summary_path)
    curve_rows = _read_csv_records(curve_path)
    signal_all_rows = _read_csv_records(signals_path)
    raw_trade_rows = _read_csv_records(trades_path)
    trade_rows = _apply_trade_classifications(code, _enrich_trades_with_buy_strategy(raw_trade_rows, signal_all_rows))
    signal_rows = signal_all_rows[:300]
    segment_rows = _read_csv_records(segment_path) if segment_path.exists() else []
    decision_rows = _read_csv_records(decision_ledger_path, limit=300) if decision_ledger_path.exists() else []
    latest_curve = curve_rows[-1] if curve_rows else {}
    daily_sharpe = _metric_sharpe(curve_rows)
    concentration = _profit_concentration_metrics(summary, trade_rows)

    metrics = {
        "total_return": summary.get("total_return"),
        "total_return_text": _metric_pct(summary, "total_return"),
        "benchmark_return": summary.get("benchmark_return"),
        "benchmark_return_text": _metric_pct(summary, "benchmark_return"),
        "excess_return": summary.get("excess_return"),
        "excess_return_text": _metric_pct(summary, "excess_return"),
        "max_drawdown": summary.get("max_drawdown"),
        "max_drawdown_text": _metric_pct(summary, "max_drawdown"),
        "win_rate": summary.get("win_rate"),
        "win_rate_text": _metric_pct(summary, "win_rate"),
        "avg_trade_return": summary.get("avg_trade_return"),
        "avg_trade_return_text": _metric_pct(summary, "avg_trade_return"),
        "trade_count": summary.get("trade_count"),
        "signal_count": summary.get("signal_count"),
        "final_equity": summary.get("final_equity"),
        "daily_sharpe": daily_sharpe,
        "daily_sharpe_text": f"{daily_sharpe:.2f}" if daily_sharpe is not None else None,
        **concentration,
    }

    return _sanitize(
        {
            "available": True,
            "message": "",
            "strategy_code": code,
            "strategy_name": config.get("strategy_name"),
            "strategy_display_name": config.get("strategy_display_name") or config.get("strategy_name"),
            "status": config.get("status"),
            "run_dir": str(run_dir),
            "summary_report": str(config.get("summary_report")),
            "state_report": str(config.get("state_report")),
            "explanation_report": str(config.get("explanation_report") or ""),
            "rule_lines": config.get("rule_lines") or [],
            "summary": summary,
            "metrics": metrics,
            "latest_curve": latest_curve,
            "equity_curve": curve_rows,
            "trades": trade_rows,
            "signals": signal_rows,
            "segment_summary": segment_rows,
            "decision_ledger": decision_rows,
            "trade_classifications": GEN2_TRADE_CLASSIFICATIONS,
            "outputs": {
                "summary": str(summary_path),
                "equity_curve": str(curve_path),
                "trades": str(trades_path),
                "signals": str(signals_path),
                "segment_summary": str(segment_path),
                "decision_ledger": str(decision_ledger_path),
            },
        }
    )
