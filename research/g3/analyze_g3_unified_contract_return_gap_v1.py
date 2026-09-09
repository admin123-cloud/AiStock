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


OUT_DIR = report_path("g3_unified_contract_return_gap_v1")
FORMAL_DIR = report_path("g2_g3_market_style_router_v1")
BRIDGE_DIR = report_path("g3_five_strategies_native_bridge_v1")
FORMAL_TRADES = FORMAL_DIR / "g3_final_with_g2_gap_supplement_closed_trades.csv"
BRIDGE_TRADES = BRIDGE_DIR / "closed_trades.csv"
FORMAL_SUMMARY = FORMAL_DIR / "summary.csv"
BRIDGE_SUMMARY = BRIDGE_DIR / "summary.json"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_formal_profile() -> dict[str, Any]:
    if not FORMAL_SUMMARY.exists():
        return {}
    df = pd.read_csv(FORMAL_SUMMARY, low_memory=False)
    row = df[(df.get("model") == g3.LATEST_G3_PROFILE) | (df.get("contract") == g3.LATEST_G3_PROFILE)]
    if row.empty:
        return {}
    item = row.iloc[0].to_dict()
    return {
        "model": g3.LATEST_G3_PROFILE,
        "total_return": float(item.get("return", item.get("total_return"))),
        "max_drawdown": float(item.get("max_drawdown")),
        "trade_count": int(float(item.get("trades", item.get("trade_count")))),
        "win_rate": float(item.get("win_rate")),
        "avg_trade_return": float(item.get("avg_trade_return")),
        "sum_pnl": float(item.get("sum_pnl")),
    }


def _formal_trades() -> pd.DataFrame:
    df = pd.read_csv(FORMAL_TRADES, low_memory=False)
    df = g3._normalize_latest_g3_closed_trades(df)
    df = g3._with_route_strategy_fields(df)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["net_ret", "account_ret", "stake", "realized_pnl", "score", "rank_key"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["match_key"] = df["code"].astype(str) + "|" + df["entry_date"].astype(str)
    df["formal_exit_date"] = df.get("policy_exit_date")
    df["formal_exit_reason"] = df.get("exit_reason")
    return df


def _bridge_trades() -> pd.DataFrame:
    df = pd.read_csv(BRIDGE_TRADES, low_memory=False)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["net_ret", "stake", "realized_pnl", "strategy_score"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["match_key"] = df["code"].astype(str) + "|" + df["entry_date"].astype(str)
    return df


def _metrics(df: pd.DataFrame, prefix: str) -> dict[str, Any]:
    ret = pd.to_numeric(df.get("net_ret"), errors="coerce")
    pnl = pd.to_numeric(df.get("realized_pnl"), errors="coerce").fillna(0)
    return {
        f"{prefix}_trades": int(len(df)),
        f"{prefix}_win_rate": float((ret > 0).mean()) if len(df) else None,
        f"{prefix}_avg_ret": float(ret.mean()) if len(df) else None,
        f"{prefix}_sum_pnl": float(pnl.sum()),
    }


def _strategy_gap(formal: pd.DataFrame, bridge: pd.DataFrame) -> pd.DataFrame:
    formal_rows = []
    for (strategy, label), part in formal.groupby(["trade_strategy", "trade_strategy_label"], dropna=False):
        formal_rows.append({"trade_strategy": strategy, "trade_strategy_label": label, **_metrics(part, "formal")})
    bridge_rows = []
    for (strategy, label), part in bridge.groupby(["trade_strategy", "trade_strategy_label"], dropna=False):
        bridge_rows.append({"trade_strategy": strategy, "trade_strategy_label": label, **_metrics(part, "bridge")})
    out = pd.merge(pd.DataFrame(formal_rows), pd.DataFrame(bridge_rows), on=["trade_strategy", "trade_strategy_label"], how="outer")
    out["pnl_gap_formal_minus_bridge"] = pd.to_numeric(out["formal_sum_pnl"], errors="coerce").fillna(0) - pd.to_numeric(out["bridge_sum_pnl"], errors="coerce").fillna(0)
    out["trade_gap_formal_minus_bridge"] = pd.to_numeric(out["formal_trades"], errors="coerce").fillna(0) - pd.to_numeric(out["bridge_trades"], errors="coerce").fillna(0)
    return out.sort_values("pnl_gap_formal_minus_bridge", ascending=False)


def _overlap_gap(formal: pd.DataFrame, bridge: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    formal_cols = [
        "match_key",
        "code",
        "name",
        "entry_date",
        "trade_strategy",
        "trade_strategy_label",
        "route",
        "mode",
        "formal_exit_date",
        "formal_exit_reason",
        "net_ret",
        "account_ret",
        "stake",
        "realized_pnl",
    ]
    bridge_cols = [
        "match_key",
        "trade_strategy",
        "trade_strategy_label",
        "source_strategy_label",
        "policy_exit_date",
        "exit_reason",
        "net_ret",
        "stake",
        "realized_pnl",
    ]
    overlap = pd.merge(
        formal[formal_cols],
        bridge[bridge_cols],
        on="match_key",
        how="inner",
        suffixes=("_formal", "_bridge"),
    )
    overlap["net_ret_gap_formal_minus_bridge"] = pd.to_numeric(overlap["net_ret_formal"], errors="coerce") - pd.to_numeric(overlap["net_ret_bridge"], errors="coerce")
    overlap["pnl_gap_formal_minus_bridge"] = pd.to_numeric(overlap["realized_pnl_formal"], errors="coerce") - pd.to_numeric(overlap["realized_pnl_bridge"], errors="coerce")
    formal_only = formal[~formal["match_key"].isin(set(bridge["match_key"]))].copy()
    bridge_only = bridge[~bridge["match_key"].isin(set(formal["match_key"]))].copy()
    return overlap.sort_values("pnl_gap_formal_minus_bridge", ascending=False), formal_only, bridge_only


def _summary(formal: pd.DataFrame, bridge: pd.DataFrame, overlap: pd.DataFrame, formal_only: pd.DataFrame, bridge_only: pd.DataFrame, strategy_gap: pd.DataFrame) -> dict[str, Any]:
    formal_profile = _read_formal_profile()
    bridge_profile = _read_json(BRIDGE_SUMMARY)
    formal_return = formal_profile.get("total_return")
    bridge_return = bridge_profile.get("total_return")
    return {
        "ok": False,
        "status": "needs_rebuild",
        "conclusion": "610% bridge validation is not the formal unified trading contract. The unified live contract must inherit the official G3 final entry, slot, sizing, and native exit semantics, then map trades into the five strategy bodies.",
        "formal_profile": formal_profile,
        "bridge_profile": bridge_profile,
        "return_gap": (float(formal_return) - float(bridge_return)) if formal_return is not None and bridge_return is not None else None,
        "formal_trade_count": int(len(formal)),
        "bridge_trade_count": int(len(bridge)),
        "overlap_trade_count": int(len(overlap)),
        "formal_only_trade_count": int(len(formal_only)),
        "bridge_only_trade_count": int(len(bridge_only)),
        "formal_only_positive_trade_count": int((pd.to_numeric(formal_only.get("net_ret"), errors="coerce") > 0).sum()),
        "formal_only_sum_pnl": float(pd.to_numeric(formal_only.get("realized_pnl"), errors="coerce").fillna(0).sum()),
        "bridge_only_sum_pnl": float(pd.to_numeric(bridge_only.get("realized_pnl"), errors="coerce").fillna(0).sum()),
        "overlap_pnl_gap_formal_minus_bridge": float(pd.to_numeric(overlap.get("pnl_gap_formal_minus_bridge"), errors="coerce").fillna(0).sum()),
        "largest_strategy_pnl_gaps": strategy_gap.head(5).astype(object).where(pd.notna(strategy_gap.head(5)), None).to_dict(orient="records"),
        "required_rebuild": [
            "Keep the five strategy bodies as naming and attribution families.",
            "Use the official G3 final selected candidates as the execution candidate stream.",
            "Preserve the official two-slot sizing and sector exposure contract.",
            "Preserve native 30m partial take-profit, previous-low protection, and policy exit semantics.",
            "Treat the daily bridge simulator as a regression guardrail only, not as the live trading return target.",
        ],
    }


def _write_report(summary: dict[str, Any], strategy_gap: pd.DataFrame, overlap: pd.DataFrame, formal_only: pd.DataFrame, bridge_only: pd.DataFrame) -> None:
    formal_profile = summary.get("formal_profile") or {}
    bridge_profile = summary.get("bridge_profile") or {}
    top_overlap = overlap.head(15)[
        [
            "code",
            "name",
            "entry_date",
            "trade_strategy_label_formal",
            "net_ret_formal",
            "net_ret_bridge",
            "net_ret_gap_formal_minus_bridge",
            "formal_exit_reason",
            "exit_reason",
        ]
    ]
    formal_only_top = formal_only.sort_values("realized_pnl", ascending=False).head(15)[
        ["code", "name", "entry_date", "trade_strategy_label", "route", "mode", "net_ret", "realized_pnl", "exit_reason"]
    ]
    lines = [
        "# G3 统一交易口径收益差距审计",
        "",
        "## 结论",
        "",
        "`+610%` 不能作为融合后的正式收益目标。它只是“5 个策略主体能承接原生信号”的桥接验收结果；真正面向未来运行的统一交易口径，必须沿用当前 G3 最终版的正式候选、二槽仓位、板块暴露、30m 分批止盈、前低保护和策略退出语义。",
        "",
        f"当前正式页全周期收益为 {_pct(formal_profile.get('total_return'))}，最大回撤 {_pct(formal_profile.get('max_drawdown'))}，成交 {formal_profile.get('trade_count')} 笔；桥接版收益为 {_pct(bridge_profile.get('total_return'))}，最大回撤 {_pct(bridge_profile.get('max_drawdown'))}，成交 {bridge_profile.get('closed_trades')} 笔。两者差距是 {_pct(summary.get('return_gap'))}。",
        "",
        "所以后续改造方向不是继续降低策略数量本身，而是：策略名称与归因统一为 5 个主体，交易执行合同回到正式 G3 final 的原生口径。",
        "",
        "## 策略损失位置",
        "",
        _md_table(
            strategy_gap,
            {"formal_win_rate", "formal_avg_ret", "bridge_win_rate", "bridge_avg_ret"},
            {"formal_sum_pnl", "bridge_sum_pnl", "pnl_gap_formal_minus_bridge"},
            max_rows=10,
        ),
        "",
        "## 同票同日收益差异",
        "",
        _md_table(
            top_overlap,
            {"net_ret_formal", "net_ret_bridge", "net_ret_gap_formal_minus_bridge"},
            set(),
            max_rows=15,
        ),
        "",
        "## 正式版有、桥接版漏掉的高贡献交易",
        "",
        _md_table(
            formal_only_top,
            {"net_ret"},
            {"realized_pnl"},
            max_rows=15,
        ),
        "",
        "## 交易口径判断",
        "",
        "1. 策略可以保持 5 个主体，但不能把正式原生策略改写成日线代理策略。",
        "2. 收益缺口主要来自重新模拟退出与原生 30m 退出语义不一致，以及部分正式高贡献交易没有被桥接候选流完整复现。",
        "3. 正式融合版本应以 `g3_final_with_g2_gap_supplement` 的交易合同为基准，只在展示、归因、未来执行配置上统一到 5 个策略主体。",
        "4. 桥接版、日线代理版只保留为风控回归和反例审计，不能进入实盘目标收益口径。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    formal = _formal_trades()
    bridge = _bridge_trades()
    strategy_gap = _strategy_gap(formal, bridge)
    overlap, formal_only, bridge_only = _overlap_gap(formal, bridge)
    summary = _summary(formal, bridge, overlap, formal_only, bridge_only, strategy_gap)

    strategy_gap.to_csv(OUT_DIR / "strategy_return_gap.csv", index=False, encoding="utf-8-sig")
    overlap.to_csv(OUT_DIR / "trade_overlap_return_gap.csv", index=False, encoding="utf-8-sig")
    formal_only.sort_values("realized_pnl", ascending=False).to_csv(OUT_DIR / "formal_only_trades.csv", index=False, encoding="utf-8-sig")
    bridge_only.sort_values("realized_pnl", ascending=False).to_csv(OUT_DIR / "bridge_only_trades.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _write_report(summary, strategy_gap, overlap, formal_only, bridge_only)
    print(json.dumps({"output_dir": str(OUT_DIR), **summary}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
