from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path  # noqa: E402


FORMAL_DISPATCH = report_path(
    "g3_formal_five_strategy_dispatch_contract_v1",
    "formal_trades_with_dispatch_strategy.csv",
)
OUT_DIR = report_path("gen3_score120_formal_institutional_source_v1")


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        if isinstance(value, float) and not math.isfinite(value):
            return None
    except Exception:
        pass
    return value


def _pct(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return ""
    if not math.isfinite(x):
        return ""
    return f"{x:.2%}"


def main() -> None:
    if not FORMAL_DISPATCH.exists():
        raise FileNotFoundError(f"missing formal dispatch source: {FORMAL_DISPATCH}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(FORMAL_DISPATCH, encoding="utf-8-sig", low_memory=False)
    inst = df[df.get("trade_strategy", pd.Series("", index=df.index)).fillna("").astype(str).eq("institutional_score120_mainwave")].copy()
    if inst.empty:
        raise RuntimeError("formal dispatch has no institutional_score120_mainwave rows")

    for col in ["entry_date", "policy_exit_date", "exit_date", "trade_date", "decision_date"]:
        if col in inst.columns:
            inst[col] = pd.to_datetime(inst[col], errors="coerce").dt.strftime("%Y-%m-%d")

    if "wave_style_score" not in inst.columns:
        inst["wave_style_score"] = pd.to_numeric(inst.get("score"), errors="coerce")
    inst["score"] = pd.to_numeric(inst["wave_style_score"], errors="coerce")
    inst["raw_score"] = pd.to_numeric(inst.get("raw_score"), errors="coerce")
    inst["selected_score"] = pd.NA
    inst["score_source"] = "wave_style_score"
    inst["score_scale"] = "historical_wave_style_score_0_140_saved_below_120"
    inst["route"] = "score120_core"
    inst["mode"] = "institutional_mainwave"
    inst["route_source"] = "formal_dispatch_institutional_score120"
    inst["source_family"] = "g3_formal_score120_mainwave"
    inst["source_type"] = "formal_dispatch_closed_trade"
    inst["strategy_id"] = "gen3_score120_formal_institutional_source_v1"
    inst["profile"] = "g3_final_with_g2_gap_supplement"
    inst["chain"] = "score120_formal_contract_family+sector_diff65+30m+index_mom60_le005"
    inst["g3_chain"] = inst["chain"]
    inst["confirm_rule"] = (
        "historical Score120 contract family; saved wave_style_score is not a standalone score>=120 replay gate; "
        "sector_diffusion>=65 + 30m confirmation + index_mom60<=5%"
    )
    if "stock_name" not in inst.columns and "name" in inst.columns:
        inst["stock_name"] = inst["name"]
    if "name" not in inst.columns and "stock_name" in inst.columns:
        inst["name"] = inst["stock_name"]

    ret = pd.to_numeric(inst.get("net_ret"), errors="coerce")
    pnl = pd.to_numeric(inst.get("realized_pnl"), errors="coerce")
    summary = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": str(FORMAL_DISPATCH),
        "out_dir": str(OUT_DIR),
        "rows": int(len(inst)),
        "entry_start": str(pd.to_datetime(inst["entry_date"], errors="coerce").min().date()),
        "entry_end": str(pd.to_datetime(inst["entry_date"], errors="coerce").max().date()),
        "avg_net_ret": float(ret.mean()),
        "sum_net_ret": float(ret.sum()),
        "win_rate": float((ret > 0).mean()),
        "sum_realized_pnl": float(pnl.sum()) if pnl.notna().any() else None,
        "score_min": float(pd.to_numeric(inst["score"], errors="coerce").min()),
        "score_mean": float(pd.to_numeric(inst["score"], errors="coerce").mean()),
        "score_max": float(pd.to_numeric(inst["score"], errors="coerce").max()),
        "score_source": "wave_style_score",
        "score_scale": "historical_wave_style_score_0_140_saved_below_120",
        "verdict": "formal_score120_source_restored_without_overwriting_current_research_source",
    }

    inst.to_csv(OUT_DIR / "closed_trades.csv", index=False, encoding="utf-8-sig")
    inst.to_csv(OUT_DIR / "formal_institutional_mainwave_closed_trades.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    lines = [
        "# Score120 正式机构主升源 v1",
        "",
        "## 结论",
        "",
        "- 本产物从 `g3_formal_five_strategy_dispatch_contract_v1/formal_trades_with_dispatch_strategy.csv` 抽取正式机构主升样本。",
        "- 目的：把历史正式 Score120 合同族与当前 `selected_score` 调度研究口径隔离，避免不同量纲混用。",
        f"- 样本：{summary['rows']} 笔，区间 {summary['entry_start']} 至 {summary['entry_end']}。",
        f"- 平均单笔收益：{_pct(summary['avg_net_ret'])}；胜率：{_pct(summary['win_rate'])}。",
        f"- 实现利润合计：{summary['sum_realized_pnl']:,.2f}。",
        f"- 保存分数：wave_style_score 均值 {summary['score_mean']:.2f}，区间 {summary['score_min']:.2f} 到 {summary['score_max']:.2f}；不能单独按 `score>=120` 复算历史正式样本。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
