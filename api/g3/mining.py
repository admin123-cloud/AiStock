# -*- coding: utf-8 -*-
"""G3 AI 策略挖掘工坊 API（Phase B：模板一键挖掘 + 自定义假设验真 + 影子合约导出）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services.mining import MINING_TEMPLATES
from services.mining.ar3_dual_core_live import (
    build_ar3_dual_core_radar,
    invalidate_ar3_radar_cache,
    save_ar3_config,
)
from services.mining.backtest_benchmark import build_ar1_historical_backtest_report
from services.mining.backtest_engine import RuleSpec
from services.mining.contract_exporter import export_candidate_contract
from services.mining.feature_store import MINING_FEATURE_META
from services.mining.live_router import (
    load_regime_router_config,
    scan_active_strategy_signals,
    update_regime_route,
)
from services.mining.rule_miner import run_mining_experiment
from utils.common.paths import runtime_path

router = APIRouter(prefix="/mining", tags=["g3-mining"])


def _runs_dir() -> Path:
    d = runtime_path("mining_runs")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _contracts_dir() -> Path:
    d = runtime_path("mining_contracts")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _list_recent_runs(limit: int = 20) -> list[dict[str, Any]]:
    files = sorted(_runs_dir().glob("mine_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, Any]] = []
    for f in files[:limit]:
        doc = _read_json(f)
        if not doc:
            continue
        out.append(
            {
                "run_id": doc.get("run_id") or f.stem,
                "created_at": doc.get("created_at"),
                "template_id": doc.get("template_id"),
                "template_name": doc.get("template_name"),
                "universe": doc.get("universe"),
                "splits": doc.get("splits"),
                "summary": doc.get("summary"),
            }
        )
    return out


def _list_exported_contracts() -> list[dict[str, Any]]:
    files = sorted(_contracts_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, Any]] = []
    for f in files:
        doc = _read_json(f)
        if not doc:
            continue
        oos = ((doc.get("validation_evidence") or {}).get("oos")) or {}
        origin = doc.get("origin") or {}
        out.append(
            {
                "file": f.name,
                "strategy_id": doc.get("strategy_id") or f.stem,
                "display_name": doc.get("display_name") or f.stem,
                "status": doc.get("status") or "candidate_shadow",
                "template_id": origin.get("template_id"),
                "run_id": origin.get("run_id"),
                "exported_at": origin.get("exported_at"),
                "all_gates_passed": bool(origin.get("all_gates_passed")),
                "mining_score": origin.get("mining_score"),
                "thesis": doc.get("thesis") or "",
                "readable_conditions": (doc.get("entry") or {}).get("readable_conditions") or [],
                "target_regimes": (doc.get("entry") or {}).get("target_regimes") or [],
                "exit": doc.get("exit") or {},
                "oos_summary": {
                    "trades_count": oos.get("trades_count"),
                    "win_rate_pct": oos.get("win_rate_pct"),
                    "avg_ret_pct": oos.get("avg_ret_pct"),
                    "cum_ret_pct": oos.get("cum_ret_pct"),
                    "excess_ret_pct": oos.get("excess_ret_pct"),
                    "max_drawdown_pct": oos.get("max_drawdown_pct"),
                    "sharpe": oos.get("sharpe"),
                },
            }
        )
    return out


@router.get("/overview")
def get_mining_overview() -> dict[str, Any]:
    """返回挖掘模板、可用特征清单、历史实验列表与已导出的候选影子合约。"""
    runs = _list_recent_runs(limit=20)
    latest_run = None
    if runs:
        latest_run = _read_json(_runs_dir() / f"{runs[0]['run_id']}.json")
    return {
        "templates": list(MINING_TEMPLATES.values()),
        "features": [
            {"key": k, **v} for k, v in MINING_FEATURE_META.items()
        ],
        "recent_runs": runs,
        "latest_run": latest_run,
        "exported_contracts": _list_exported_contracts(),
    }


@router.get("/runs/{run_id}")
def get_mining_run_detail(run_id: str) -> dict[str, Any]:
    """读取单次策略挖掘实验的完整详情（含 IS/Val/OOS 三段与 5 道硬门禁）。"""
    safe_id = Path(run_id).name
    doc = _read_json(_runs_dir() / f"{safe_id}.json")
    if not doc:
        raise HTTPException(status_code=404, detail=f"未找到挖掘实验 {safe_id}")
    return doc


class CustomConditionItem(BaseModel):
    feature: str
    op: str = ">="
    value: float


class MiningRunRequest(BaseModel):
    template_id: str = "mainwave_offense"
    start_date: str = "2023-01-01"
    min_amt_wan: float = Field(default=5000.0, ge=500.0, le=50000.0)
    enable_tree_miner: bool = True
    tune_exit_on_val: bool = True
    # 自定义假设模式（当 custom_conditions 非空时生效）
    custom_name: str = ""
    custom_thesis: str = ""
    custom_regimes: list[str] = Field(default_factory=list)
    custom_conditions: list[CustomConditionItem] = Field(default_factory=list)
    custom_hold_days: int = Field(default=5, ge=2, le=10)
    custom_stop_loss_pct: float = Field(default=-0.07, le=-0.02, ge=-0.20)
    custom_take_profit_pct: float = Field(default=0.15, ge=0.03, le=0.50)


@router.post("/run")
def post_mining_run(req: MiningRunRequest) -> dict[str, Any]:
    """Scoped-write：运行一次全市场策略挖掘实验并落盘结果 JSON（只读 ClickHouse，绝不触发下单）。"""
    if req.template_id not in MINING_TEMPLATES:
        raise HTTPException(status_code=400, detail=f"未知挖掘模板: {req.template_id}")

    custom_rules: list[RuleSpec] | None = None
    if req.custom_conditions:
        for c in req.custom_conditions:
            if c.feature not in MINING_FEATURE_META:
                raise HTTPException(status_code=400, detail=f"未知特征字段: {c.feature}")
            if c.op not in (">=", ">", "<=", "<", "=="):
                raise HTTPException(status_code=400, detail=f"不支持的运算符: {c.op}")
        tpl = MINING_TEMPLATES[req.template_id]
        regimes = req.custom_regimes or list(tpl["target_regimes"])
        custom_rules = [
            RuleSpec(
                rule_id=f"custom_{req.template_id}_01",
                name=req.custom_name.strip() or "自定义假设策略",
                template_id=req.template_id,
                target_regimes=regimes,
                entry_conditions=[c.model_dump() for c in req.custom_conditions],
                exit_policy={
                    "hold_days": int(req.custom_hold_days),
                    "stop_loss_pct": float(req.custom_stop_loss_pct),
                    "take_profit_pct": float(req.custom_take_profit_pct),
                },
                rank_by=str(tpl["default_rank_by"]),
                source="custom_hypothesis",
                thesis=req.custom_thesis.strip() or "用户/AI 自定义明文假设",
            )
        ]

    try:
        return run_mining_experiment(
            template_id=req.template_id,
            start_date=req.start_date,
            min_amt_20d=float(req.min_amt_wan) * 1e4,
            custom_rules=custom_rules,
            enable_tree_miner=bool(req.enable_tree_miner) and not bool(custom_rules),
            tune_exit_on_val=bool(req.tune_exit_on_val),
            force_rebuild_cache=False,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"策略挖掘执行失败: {exc}") from exc


class ExportContractRequest(BaseModel):
    run_id: str
    rule_id: str


@router.post("/export-contract")
def post_export_mining_contract(req: ExportContractRequest) -> dict[str, Any]:
    """Scoped-write：将挖掘实验中的指定候选策略一键导出为 candidate_shadow 影子合约。"""
    safe_id = Path(req.run_id).name
    doc = _read_json(_runs_dir() / f"{safe_id}.json")
    if not doc:
        raise HTTPException(status_code=404, detail=f"未找到挖掘实验 {safe_id}")
    matched = None
    for item in doc.get("results") or []:
        if (item.get("rule") or {}).get("rule_id") == req.rule_id:
            matched = item
            break
    if not matched:
        raise HTTPException(status_code=404, detail=f"在实验 {safe_id} 中未找到规则 {req.rule_id}")
    exp = export_candidate_contract(matched, run_id=safe_id)
    return {
        "status": "ok",
        "strategy_id": exp["strategy_id"],
        "path": exp["path"],
        "contract": exp["contract"],
    }


@router.get("/router")
def get_mining_regime_router() -> dict[str, Any]:
    """返回五态大盘周期策略路由表与今日生效策略实时选股扫描结果。"""
    from api.g3.desk import get_g3_market_gate

    try:
        gate = get_g3_market_gate()
    except Exception:
        gate = {"regime": "balanced", "regime_label": "横盘平衡", "allow_new_mainwave": False, "target_position_cap_pct": 50.0}

    live = scan_active_strategy_signals(
        current_regime=str(gate.get("regime") or "balanced"),
        regime_label=str(gate.get("regime_label") or ""),
        allow_new_mainwave=bool(gate.get("allow_new_mainwave")),
        target_position_cap_pct=float(gate.get("target_position_cap_pct") or 0.0),
    )
    cfg = load_regime_router_config()
    return {
        "router_config": cfg,
        "live_signal": live,
    }


class BindRegimeRouteRequest(BaseModel):
    regime: str
    strategy_id: str = "cash_defense"


@router.post("/router/bind")
def post_bind_mining_regime_route(req: BindRegimeRouteRequest) -> dict[str, Any]:
    """Scoped-write：将指定大盘周期绑定为某个已导出的影子合约或 cash_defense（空仓防守）。"""
    try:
        cfg = update_regime_route(req.regime.strip(), req.strategy_id.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "router_config": cfg}


@router.get("/backtest-report")
def get_mining_backtest_report() -> dict[str, Any]:
    """返回 AR1 五阶段切换标准、基准 vs 阶段选股策略历史回测对比与 6 项防幻觉审计报告。"""
    try:
        return build_ar1_historical_backtest_report(force_rebuild=False)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"加载历史回测报告失败: {exc}") from exc


@router.post("/backtest-report/rebuild")
def post_rebuild_mining_backtest_report() -> dict[str, Any]:
    """Scoped-write：重新运行全市场单账户零杠杆历史回测并刷新落盘报告。"""
    try:
        return build_ar1_historical_backtest_report(force_rebuild=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"重跑历史回测失败: {exc}") from exc


def _resolve_live_gate_and_panic() -> tuple[dict[str, Any], dict[str, Any]]:
    from api.g3.desk import get_g3_market_gate
    from services.mining.panic_rebound_monitor import evaluate_panic_rebound_signal

    try:
        gate = get_g3_market_gate()
    except Exception:
        gate = {"regime": "balanced", "regime_label": "横盘平衡", "allow_new_mainwave": False, "target_position_cap_pct": 35.0}
    try:
        panic = evaluate_panic_rebound_signal(market_gate=gate)
    except Exception:
        panic = {"armed": False, "triggered": False}
    return gate, panic


@router.get("/ar3/radar")
def get_ar3_dual_core_radar(force_refresh: bool = False) -> dict[str, Any]:
    """返回 AR3 v3.0 终极双核实时作战雷达（#01/#02 跨资产 ETF + F2A/F3A 双选股雷达 + 20日赚钱效应）。"""
    gate, panic = _resolve_live_gate_and_panic()
    try:
        return build_ar3_dual_core_radar(
            market_gate=gate,
            panic_signal=panic,
            force_refresh=force_refresh,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"加载 AR3 v3.0 双核雷达失败: {exc}") from exc


class AR3ConfigUpdateRequest(BaseModel):
    active_stock_mode: str | None = None
    etf_core_mode: str | None = None
    stock_budget_cap_pct: int | None = Field(default=None, ge=20, le=60)
    max_slots: int | None = Field(default=None, ge=2, le=6)
    note: str | None = None


@router.post("/ar3/config")
def post_ar3_dual_core_config(req: AR3ConfigUpdateRequest) -> dict[str, Any]:
    """Scoped-write：随行情赚钱效应一键热切换 AR3 v3.0 个股主引擎（AB混合/F2A趋势/F3A回踩）与 ETF 核心。"""
    try:
        cfg = save_ar3_config(
            active_stock_mode=req.active_stock_mode,
            etf_core_mode=req.etf_core_mode,
            stock_budget_cap_pct=req.stock_budget_cap_pct,
            max_slots=req.max_slots,
            note=req.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    gate, panic = _resolve_live_gate_and_panic()
    radar = build_ar3_dual_core_radar(market_gate=gate, panic_signal=panic, force_refresh=True)
    return {"status": "ok", "config": cfg, "radar": radar}


@router.post("/ar3/sync")
def post_ar3_sync_data(sync_etf: bool = True, sync_financials: bool = False) -> dict[str, Any]:
    """Scoped-write：手动刷新 ClickHouse 独立表（跨资产 ETF 日线；可选全量刷新财报/预告/分红）并重算雷达。"""
    summary: dict[str, Any] = {}
    try:
        if sync_etf:
            from scripts.sync_cross_asset_etf_daily import sync_all_etfs
            summary["etf"] = sync_all_etfs()
        if sync_financials:
            from scripts.sync_financial_disclosures import ensure_tables, sync_dividends, sync_express_reports, sync_forecasts, sync_official_reports
            from datetime import datetime, timedelta, timezone
            ensure_tables()
            now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
            summary["official"] = sync_official_reports(now_str)
            summary["express"] = sync_express_reports(now_str)
            summary["forecast"] = sync_forecasts(now_str)
            summary["dividends"] = sync_dividends(now_str)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"同步 AR3 独立数据失败: {exc}") from exc

    invalidate_ar3_radar_cache()
    gate, panic = _resolve_live_gate_and_panic()
    radar = build_ar3_dual_core_radar(market_gate=gate, panic_signal=panic, force_refresh=True)
    return {"status": "ok", "sync": summary, "radar": radar}
