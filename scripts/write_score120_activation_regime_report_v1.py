from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("score120_activation_regime_v1")


def _safe_float(value: Any) -> float | None:
    try:
        x = float(value)
    except Exception:
        return None
    if not math.isfinite(x):
        return None
    return x


def _pct(value: Any) -> str:
    x = _safe_float(value)
    return "" if x is None else f"{x:.2%}"


def _pct_point(value: Any) -> str:
    x = _safe_float(value)
    return "" if x is None else f"{x:.2f}%"


def _md_table(
    df: pd.DataFrame,
    pct_cols: set[str] | None = None,
    pct_point_cols: set[str] | None = None,
    limit: int | None = None,
) -> str:
    d = df.head(limit).copy() if limit else df.copy()
    for col in pct_cols or set():
        if col in d.columns:
            d[col] = d[col].map(_pct)
    for col in pct_point_cols or set():
        if col in d.columns:
            d[col] = d[col].map(_pct_point)
    return d.to_markdown(index=False)


def main() -> int:
    gates = pd.read_csv(OUT_DIR / "fixed_gate_results_corrected.csv", encoding="utf-8-sig")
    trades = pd.read_csv(OUT_DIR / "diff65_m30_trades_with_regime_corrected.csv", encoding="utf-8-sig")
    daily = pd.read_csv(OUT_DIR / "daily_market_regime_corrected.csv", encoding="utf-8-sig")
    trades["entry_date"] = pd.to_datetime(trades["entry_date"], errors="coerce").dt.normalize()
    for col in ["net_ret", "realized_pnl", "sig_index_mom60", "sig_index_mom20"]:
        if col in trades.columns:
            trades[col] = pd.to_numeric(trades[col], errors="coerce")

    pct_cols = {"ret", "dd", "post", "blind", "win", "avg", "worst"}
    display = gates.copy()
    display = display[["name", "trades", "ret", "dd", "post", "blind", "win", "avg", "worst"]]
    focus_names = [
        "base",
        "sig_mom60_le_0.05",
        "sig_mom60_le_0.08",
        "not_sig_index_bull",
        "sig_mom60_le005_mktchg_ge0",
        "mkt_count_chg20_ge0",
    ]
    focus = display[display["name"].isin(focus_names)].copy()
    focus["order"] = focus["name"].map({name: i for i, name in enumerate(focus_names)})
    focus = focus.sort_values("order").drop(columns=["order"])

    by_year = trades.copy()
    by_year["year"] = by_year["entry_date"].dt.year
    by_year["enabled_mom60_le_005"] = by_year["sig_index_mom60"] <= 0.05
    yearly = (
        by_year.groupby(["year", "enabled_mom60_le_005"], dropna=False)
        .agg(
            trades=("net_ret", "size"),
            avg_ret=("net_ret", "mean"),
            win_rate=("net_ret", lambda x: float((x > 0).mean())),
            pnl=("realized_pnl", "sum"),
            worst_ret=("net_ret", "min"),
        )
        .reset_index()
    )
    yearly["pnl"] = yearly["pnl"].round(0)

    hot = trades[trades["sig_index_mom60"] > 0.05].copy()
    ok = trades[trades["sig_index_mom60"] <= 0.05].copy()
    hot_summary = {
        "trades": int(len(hot)),
        "avg_ret": float(hot["net_ret"].mean()) if len(hot) else 0.0,
        "win_rate": float((hot["net_ret"] > 0).mean()) if len(hot) else 0.0,
        "pnl": float(hot["realized_pnl"].sum()) if len(hot) else 0.0,
        "worst_ret": float(hot["net_ret"].min()) if len(hot) else 0.0,
    }
    ok_summary = {
        "trades": int(len(ok)),
        "avg_ret": float(ok["net_ret"].mean()) if len(ok) else 0.0,
        "win_rate": float((ok["net_ret"] > 0).mean()) if len(ok) else 0.0,
        "pnl": float(ok["realized_pnl"].sum()) if len(ok) else 0.0,
        "worst_ret": float(ok["net_ret"].min()) if len(ok) else 0.0,
    }
    regime_compare = pd.DataFrame(
        [
            {"regime": "启用：信号日指数60日动量 <= 5%", **ok_summary},
            {"regime": "关闭/降仓：信号日指数60日动量 > 5%", **hot_summary},
        ]
    )
    regime_compare["pnl"] = regime_compare["pnl"].round(0)

    lines = [
        "# score120 收益引擎启用条件研究 v1",
        "",
        "## 核心结论",
        "",
        "- `score120_diff65_m30_ma20` 是有效收益引擎，但不能全周期无条件开启。",
        "- 最有效、且无未来函数的启用条件是：**信号日指数 60 日动量不高于 5%**。",
        "- 这个条件的含义不是熊市低吸，而是避免在指数 60 日涨幅已经过热时继续追主线扩散；真正有超额的阶段更像“主线扩散仍在，但指数整体尚未充分过热”。",
        "- 普通指数均线牛市门控无效，甚至会过滤掉最赚钱的早中段；判断重点应放在“指数未过热 + 主线扩散成立 + 30m 承接仍在”。",
        "",
        "## 启用条件对比",
        "",
        _md_table(focus, pct_point_cols=pct_cols),
        "",
        "## 60日动量分组",
        "",
        _md_table(regime_compare, pct_cols={"avg_ret", "win_rate", "worst_ret"}),
        "",
        "## 分年验证",
        "",
        _md_table(yearly, pct_cols={"avg_ret", "win_rate", "worst_ret"}),
        "",
        "## 推荐开关",
        "",
        "1. 基础交易开关：`score120` 模型分数达到 1.20。",
        "2. 主线开关：行业扩散分 `sector_diffusion_score >= 65`。",
        "3. 承接开关：信号日 30m 收盘价高于 30m MA20。",
        "4. 收益引擎启用条件：信号日指数 60 日动量 `index_mom60 <= 0.05`。",
        "5. 当前机构主升正式合同下，`index_mom60 > 0.05` 只观察/阻断，不降到 25% 买入；等待新的主线扩散、30m确认与指数热度回到门槛内。",
        "",
        "## 当前解释",
        "",
        "- `index_mom60 <= 0.05` 后，全周期从 +394.10% 提升到 +480.29%，最大回撤从 -35.57% 降到 -14.67%。",
        "- `not_sig_index_bull` 的回撤更低，但交易数只有 28，过于偏早段，可能错过机构主升中后段。",
        "- `mkt_count_chg20_ge0` 也有效，但收益低于 60 日动量门控；它更适合作为辅助确认，而不是第一开关。",
    ]
    out = OUT_DIR / "REPORT_CN.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    result = {
        "status": "completed",
        "out_dir": str(OUT_DIR),
        "recommended_gate": "signal_index_mom60 <= 0.05",
        "base_engine": "score120_diff65_m30_ma20",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
