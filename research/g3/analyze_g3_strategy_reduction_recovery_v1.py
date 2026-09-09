from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import api.gen3_state_alpha as g3  # noqa: E402
from scripts.backtest_g3_five_strategies_from_scratch_v1 import _md_table, _money, _pct  # noqa: E402
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("g3_strategy_reduction_recovery_v1")
DAILY_PROXY_DIR = report_path("g3_five_strategies_from_scratch_v1")
NATIVE_BRIDGE_DIR = report_path("g3_five_strategies_native_bridge_v1")
RECALL_DIR = report_path("g3_profitable_signal_recall_v1")
BRIDGE_RECALL_DIR = report_path("g3_native_source_bridge_recall_v1")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _strategy_metrics_from_historical() -> pd.DataFrame:
    df = pd.read_csv(g3.HISTORICAL_TRADES_PATH, low_memory=False)
    df = g3._normalize_latest_g3_closed_trades(df)
    df = g3._with_route_strategy_fields(df)
    df["net_ret"] = pd.to_numeric(df.get("net_ret"), errors="coerce")
    df["realized_pnl"] = pd.to_numeric(df.get("realized_pnl"), errors="coerce").fillna(0.0)
    rows = []
    for (strategy, label), part in df.groupby(["trade_strategy", "trade_strategy_label"], dropna=False):
        ret = pd.to_numeric(part["net_ret"], errors="coerce")
        rows.append(
            {
                "version": "historical_g3_final",
                "trade_strategy": strategy,
                "trade_strategy_label": label,
                "trade_count": len(part),
                "win_rate": float((ret > 0).mean()) if len(ret) else None,
                "avg_ret": float(ret.mean()) if len(ret) else None,
                "sum_pnl": float(part["realized_pnl"].sum()),
            }
        )
    return pd.DataFrame(rows)


def _read_strategy_metrics(path: Path, version: str) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, low_memory=False)
    if df.empty:
        return df
    out = df[["trade_strategy", "trade_strategy_label", "trade_count", "win_rate", "avg_ret", "sum_pnl"]].copy()
    out.insert(0, "version", version)
    return out


def _overall_table() -> pd.DataFrame:
    historical = _strategy_metrics_from_historical()
    old_total = {
        "version": "historical_g3_final",
        "candidate_rows": None,
        "selected_rows": None,
        "closed_trades": int(historical["trade_count"].sum()),
        "win_rate": float((historical["win_rate"] * historical["trade_count"]).sum() / historical["trade_count"].sum()),
        "avg_ret": None,
        "total_return": None,
        "max_drawdown": None,
        "final_equity": None,
        "sum_pnl": float(historical["sum_pnl"].sum()),
    }
    daily = _read_json(DAILY_PROXY_DIR / "summary.json")
    bridge = _read_json(NATIVE_BRIDGE_DIR / "summary.json")
    rows = [old_total]
    for version, meta in [("daily_proxy_five_strategy", daily), ("native_bridge_five_strategy", bridge)]:
        rows.append(
            {
                "version": version,
                "candidate_rows": meta.get("candidate_rows"),
                "selected_rows": meta.get("selected_rows"),
                "closed_trades": meta.get("closed_trades"),
                "win_rate": meta.get("win_rate"),
                "avg_ret": meta.get("avg_ret"),
                "total_return": meta.get("total_return"),
                "max_drawdown": meta.get("max_drawdown"),
                "final_equity": meta.get("final_equity"),
                "sum_pnl": meta.get("sum_pnl"),
            }
        )
    return pd.DataFrame(rows)


def _strategy_compare() -> pd.DataFrame:
    frames = [
        _strategy_metrics_from_historical(),
        _read_strategy_metrics(DAILY_PROXY_DIR / "strategy_metrics.csv", "daily_proxy_five_strategy"),
        _read_strategy_metrics(NATIVE_BRIDGE_DIR / "strategy_metrics.csv", "native_bridge_five_strategy"),
    ]
    df = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    df.to_csv(OUT_DIR / "strategy_compare_long.csv", index=False, encoding="utf-8-sig")
    pivot = df.pivot_table(
        index=["trade_strategy", "trade_strategy_label"],
        columns="version",
        values=["trade_count", "win_rate", "avg_ret", "sum_pnl"],
        aggfunc="first",
    )
    pivot.columns = [f"{metric}_{version}" for metric, version in pivot.columns]
    return pivot.reset_index()


def _read_optional_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, low_memory=False) if path.exists() else pd.DataFrame()


def _write_report(overall: pd.DataFrame, strategy_compare: pd.DataFrame, scenario: pd.DataFrame, recall: pd.DataFrame, bridge_recall: pd.DataFrame) -> None:
    report = [
        "# G3 策略减少后的收益还原审计",
        "",
        "## 结论",
        "",
        "策略主体可以减少到 5 个，但不能把原生信号源替换成日线代理。真正造成收益跑丢的原因，是 Score120、G2量能续强、强势突破、Panic/旧G3修复等原生买点没有进入统一候选；同时 Range V3 弱势低吸在统一卖出合同下形成主要拖累。",
        "",
        "默认恢复方案：保留 5 个统一交易策略主体，挂回原生信号源，默认排除 `Range V3弱势低吸原生源`，让旧G3弱势/震荡修复继续承担 `震荡弱势修复` 策略主体。",
        "",
        "## 三版收益对比",
        "",
        _md_table(
            overall,
            {"win_rate", "avg_ret", "total_return", "max_drawdown"},
            {"sum_pnl", "final_equity"},
        ),
        "",
        "## 策略维度对比",
        "",
        _md_table(
            strategy_compare,
            {
                "win_rate_historical_g3_final",
                "win_rate_daily_proxy_five_strategy",
                "win_rate_native_bridge_five_strategy",
                "avg_ret_historical_g3_final",
                "avg_ret_daily_proxy_five_strategy",
                "avg_ret_native_bridge_five_strategy",
            },
            {
                "sum_pnl_historical_g3_final",
                "sum_pnl_daily_proxy_five_strategy",
                "sum_pnl_native_bridge_five_strategy",
            },
            max_rows=20,
        ),
        "",
        "## 召回证据",
        "",
        "日线代理五策略同策略盈利样本召回：",
        "",
        _md_table(
            recall,
            {"candidate_within_3_trade_days_rate", "selected_within_3_trade_days_rate", "avg_net_ret"},
            {"realized_pnl"},
            max_rows=10,
        ),
        "",
        "原生源桥接同策略盈利样本召回：",
        "",
        _md_table(
            bridge_recall,
            {"candidate_within_3_trade_days_rate", "avg_net_ret"},
            {"realized_pnl"},
            max_rows=10,
        ),
        "",
        "## 场景矩阵",
        "",
        _md_table(
            scenario,
            {"win_rate", "avg_ret", "total_return", "max_drawdown"},
            {"sum_pnl"},
            max_rows=10,
        ),
        "",
        "## 交易结论",
        "",
        "1. `机构主升Score120`、`量能续强补位`、`强势突破`必须保留原生买点生成器，不能用普通日线动量代理替代。",
        "2. `恐慌出清修复`可以统一，但要继承旧G3/Panic的30m修复确认与压力分层。",
        "3. `震荡弱势修复`可以统一，但默认只接旧G3弱势/震荡修复原生源；Range V3 暂列研究源，不进正式默认合同。",
        "4. 后续最终验收还需要恢复 30m 盘中执行顺序；当前桥接版已经证明收益可以被还原，但还不是最终实盘级别回测。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(report), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    overall = _overall_table()
    strategy_compare = _strategy_compare()
    scenario = _read_optional_csv(NATIVE_BRIDGE_DIR / "scenario_matrix.csv")
    recall = _read_optional_csv(RECALL_DIR / "profitable_recall_by_strategy.csv")
    bridge_recall = _read_optional_csv(BRIDGE_RECALL_DIR / "native_bridge_recall_by_strategy.csv")
    overall.to_csv(OUT_DIR / "overall_compare.csv", index=False, encoding="utf-8-sig")
    strategy_compare.to_csv(OUT_DIR / "strategy_compare_wide.csv", index=False, encoding="utf-8-sig")
    scenario.to_csv(OUT_DIR / "scenario_matrix.csv", index=False, encoding="utf-8-sig")
    _write_report(overall, strategy_compare, scenario, recall, bridge_recall)
    print(json.dumps({"output_dir": str(OUT_DIR), "versions": overall["version"].tolist()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
