from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_wave_style_template_strategy_v1 import (  # noqa: E402
    _add_features,
    _load_daily,
    _load_index_features,
    _score_candidates,
    _simulate,
)
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("post_climax_continuation_hypothesis_v1")
START = "2020-01-01"
END = "2026-05-15"


def _metrics(df: pd.DataFrame, label: str) -> dict[str, object]:
    ret = pd.to_numeric(df.get("net_ret"), errors="coerce").dropna()
    return {
        "window": label,
        "trades": int(len(ret)),
        "avg_net_ret": float(ret.mean()) if len(ret) else None,
        "win_rate": float((ret > 0).mean()) if len(ret) else None,
        "worst_trade": float(ret.min()) if len(ret) else None,
        "sum_trade_ret": float(ret.sum()) if len(ret) else None,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    raw = _load_daily("2019-07-01", END)
    feat = _add_features(raw, max_hold=20)
    index = _load_index_features("2019-07-01", END)
    feat = feat.merge(index, on="trade_date", how="left")
    scored = _score_candidates(feat, use_learned_sector=False)

    # Hypothesis only: the five formal 2026 template-gap observations motivate this branch.
    # It is intentionally evaluated separately and never feeds the live mainwave contract.
    mask = (
        scored["trade_date"].between(pd.Timestamp(START), pd.Timestamp(END))
        & ~scored["code_raw"].astype(str).str.endswith(".BJ")
        & scored["next_entry_date"].notna()
        & scored["exit_close_20"].notna()
        & scored["next_open"].gt(0)
        & scored["amount_rank"].ge(0.70)
        & scored["above_ma20"].fillna(False)
        & scored["above_ma60"].fillna(False)
        & scored["close_to_high60"].ge(-0.10)
        & scored["amount5_20"].ge(1.10)
        & scored["ret5"].ge(0.15)
        & scored["ret20"].between(0.08, 0.60)
        & scored["limit_up_days20"].between(5, 8)
        & scored["index_mom60"].le(0.05)
    )
    candidates = scored[mask].copy()
    candidates["entry_date"] = pd.to_datetime(candidates["next_entry_date"]).dt.normalize()
    candidates["entry_price"] = pd.to_numeric(candidates["next_open"], errors="coerce")
    candidates["policy_exit_date"] = pd.to_datetime(candidates["exit_date_20"]).dt.normalize()
    candidates["exit_price"] = pd.to_numeric(candidates["exit_close_20"], errors="coerce")
    candidates["gross_ret"] = candidates["exit_price"] / candidates["entry_price"] - 1.0
    candidates["net_ret"] = candidates["gross_ret"] - 0.003
    candidates["hold_days"] = 20
    candidates["hypothesis_branch"] = "post_climax_continuation_research_only"
    candidates = candidates.sort_values(["entry_date", "wave_style_score", "amount_rank"], ascending=[True, False, False])

    curve, closed = _simulate(candidates, slots=2, slot_pct=0.50, daily_open_limit=1)
    windows = {
        "train_2020_2023": ("2020-01-01", "2023-12-31"),
        "validation_2024_2025": ("2024-01-01", "2025-12-31"),
        "hypothesis_origin_2026": ("2026-01-01", END),
        "full": (START, END),
    }
    summary = []
    for name, (start, end) in windows.items():
        view = closed[pd.to_datetime(closed["entry_date"]).between(start, end)] if not closed.empty else closed
        summary.append(_metrics(view, name))
    summary_df = pd.DataFrame(summary)
    candidate_year = candidates.assign(year=pd.to_datetime(candidates["entry_date"]).dt.year).groupby("year", as_index=False).agg(
        candidate_rows=("code_raw", "size"), candidate_days=("entry_date", "nunique")
    )
    result = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "candidate_rows": int(len(candidates)),
        "candidate_days": int(candidates["entry_date"].nunique()) if not candidates.empty else 0,
        "live_eligible": False,
        "missing_validation": "no historical 30m confirmation and parameters originate from 2026 template-gap observations",
    }
    candidates.to_csv(OUT_DIR / "candidates.csv", index=False, encoding="utf-8-sig")
    closed.to_csv(OUT_DIR / "closed_trades.csv", index=False, encoding="utf-8-sig")
    curve.to_csv(OUT_DIR / "equity_curve.csv", index=False, encoding="utf-8-sig")
    summary_df.to_csv(OUT_DIR / "window_summary.csv", index=False, encoding="utf-8-sig")
    candidate_year.to_csv(OUT_DIR / "candidate_year_summary.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 过热后二次主升研究假设 v1", "", "## 定位", "",
        "- 研究分支，不进入G3候选、影子或下单路径。",
        "- 规则参数来自2026正式样本缺口，仅用于生成待验证假设。",
        "- 使用日线T+1开盘、20日固定持有近似，未验证历史30分钟确认和正式退出合同。", "",
        "## 窗口结果", "", summary_df.to_markdown(index=False), "", "## 判断", "",
        "- 只有2020-2023和2024-2025均有足够交易数且收益行为一致，才值得进入下一轮30分钟与退出合同验证。",
        "- 2026属于规则起源样本，不能作为独立样本外证明。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
