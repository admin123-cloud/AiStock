from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_panic_final_candidate_v1 import WINDOWS, _display, _metrics, _simulate_slot_mtm


def _load_candidates(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path)
    for col in ["entry_date", "exit_date", "policy_exit_date", "confirm_datetime", "exit_datetime"]:
        if col in d.columns:
            d[col] = pd.to_datetime(d[col], errors="coerce")
    for col in [
        "policy_net_ret",
        "candidate_score",
        "chain_rank",
        "day_breadth_ma20",
        "day_index_mom20",
        "day_market_amount_ratio20",
        "day_up_rate",
        "big_down_rate",
        "range_pos60",
        "amount_ratio20",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["entry_date"] = d["entry_date"].dt.normalize()
    d["policy_exit_date"] = d["policy_exit_date"].dt.normalize()
    return d.dropna(subset=["entry_date", "code", "policy_exit_date", "policy_net_ret"]).copy()


def _pause_masks(d: pd.DataFrame) -> dict[str, pd.Series]:
    breadth = pd.to_numeric(d["day_breadth_ma20"], errors="coerce")
    mom = pd.to_numeric(d["day_index_mom20"], errors="coerce")
    big_down = pd.to_numeric(d["big_down_rate"], errors="coerce")
    amount = pd.to_numeric(d["day_market_amount_ratio20"], errors="coerce")
    up_rate = pd.to_numeric(d["day_up_rate"], errors="coerce")
    return {
        "base_no_extra_pause": pd.Series(False, index=d.index),
        # Fixed hypotheses from the 2023 audit. They are candidates, not final rules.
        "pause_broad_positive_nonpanic": (breadth >= 0.45) & (mom >= 0.0) & (big_down < 0.25),
        "pause_positive_mom_low_capitulation": (mom >= 0.0) & (big_down < 0.20),
        "pause_broad_liquid_rebound_trap": (breadth >= 0.45) & (amount >= 1.00) & (up_rate >= 0.30) & (big_down < 0.25),
    }


def _skip_summary(candidates: pd.DataFrame, mask: pd.Series, policy: str) -> dict:
    s = candidates[mask].copy()
    if s.empty:
        return {"policy": policy, "skipped_candidates": 0}
    return {
        "policy": policy,
        "skipped_candidates": int(len(s)),
        "skipped_days": int(s["entry_date"].nunique()),
        "skipped_2023": int(s["entry_date"].dt.year.eq(2023).sum()),
        "skipped_mean_ret": float(s["policy_net_ret"].mean()),
        "skipped_worst_ret": float(s["policy_net_ret"].min()),
        "skipped_win_rate": float((s["policy_net_ret"] > 0).mean()),
    }


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def _display_skip(raw: pd.DataFrame) -> pd.DataFrame:
    out = raw.copy()
    for col in ["skipped_mean_ret", "skipped_worst_ret", "skipped_win_rate"]:
        if col in out.columns:
            out[col] = out[col].map(_pct)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit false-panic pause candidates for G3 panic final candidate.")
    parser.add_argument(
        "--input",
        default="reports/gen3_panic_v2_research/final_candidate_v1/m30_close5_full_nextopen_cost30_policy_candidates.csv",
    )
    parser.add_argument("--output-dir", default="reports/gen3_panic_v2_research/false_panic_pause_audit_v1")
    parser.add_argument("--initial-capital", type=float, default=150000.0)
    parser.add_argument("--cost-bps", type=float, default=30.0)
    args = parser.parse_args()

    source = Path(args.input)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates = _load_candidates(source)
    masks = _pause_masks(candidates)

    rows: list[dict] = []
    skip_rows: list[dict] = []
    for policy, pause_mask in masks.items():
        selected = candidates[~pause_mask].copy()
        selected.to_csv(out_dir / f"{policy}_policy_candidates.csv", index=False, encoding="utf-8-sig")
        closed, curve = _simulate_slot_mtm(
            candidates=selected,
            initial_capital=args.initial_capital,
            slots=5,
            slot_pct=0.20,
            mtm_cost_bps=args.cost_bps,
        )
        closed.to_csv(out_dir / f"{policy}_closed_trades.csv", index=False, encoding="utf-8-sig")
        curve.to_csv(out_dir / f"{policy}_mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
        for window in ["train_2020_2023", "valid_2024_2025", "blind_2026ytd", "full"]:
            m = _metrics(closed, curve, window, policy, args.cost_bps)
            m["pause_policy"] = policy
            m["skipped_candidates"] = int(pause_mask.sum())
            rows.append(m)
        skip_rows.append(_skip_summary(candidates, pause_mask, policy))

    raw = pd.DataFrame(rows)
    raw.to_csv(out_dir / "false_panic_pause_summary_raw.csv", index=False, encoding="utf-8-sig")
    display = _display(raw)
    display.to_csv(out_dir / "false_panic_pause_summary_display.csv", index=False, encoding="utf-8-sig")
    skip_raw = pd.DataFrame(skip_rows)
    skip_raw.to_csv(out_dir / "false_panic_pause_skipped_raw.csv", index=False, encoding="utf-8-sig")
    skip_display = _display_skip(skip_raw)
    skip_display.to_csv(out_dir / "false_panic_pause_skipped_display.csv", index=False, encoding="utf-8-sig")

    full = display[display["window"].eq("full")]
    train = display[display["window"].eq("train_2020_2023")]
    valid = display[display["window"].eq("valid_2024_2025")]
    lines = [
        "# G3 Panic 假恐慌暂停候选审计 V1",
        "",
        "## 口径",
        "",
        f"- 输入：`{source}`",
        "- 基线：最终候选 V1，30bps、slot5、20% 单槽、30m -5% 失败退出。",
        "- 本审计只验证暂停候选是否有研究价值，不把规则写入正式策略。",
        "",
        "## 暂停候选",
        "",
        "- `pause_broad_positive_nonpanic`：宽度 >=45%，指数20日动量 >=0，大跌率 <25%。",
        "- `pause_positive_mom_low_capitulation`：指数20日动量 >=0，大跌率 <20%。",
        "- `pause_broad_liquid_rebound_trap`：宽度 >=45%，成交不低于20日均量，上涨率 >=30%，大跌率 <25%。",
        "",
        "## full 对比",
        "",
        full[["policy", "closed", "skipped", "skipped_candidates", "total_ret", "max_drawdown", "worst_open_mtm_ret", "mean_trade_ret", "worst_trade"]].to_markdown(index=False),
        "",
        "## train 对比",
        "",
        train[["policy", "closed", "skipped", "skipped_candidates", "total_ret", "max_drawdown", "worst_open_mtm_ret", "mean_trade_ret", "worst_trade"]].to_markdown(index=False),
        "",
        "## valid 对比",
        "",
        valid[["policy", "closed", "skipped", "skipped_candidates", "total_ret", "max_drawdown", "worst_open_mtm_ret", "mean_trade_ret", "worst_trade"]].to_markdown(index=False),
        "",
        "## 被跳过候选质量",
        "",
        skip_display.to_markdown(index=False),
        "",
        "## 判断",
        "",
        "1. 如果暂停候选只改善 2023 但伤害 full/valid，则是过拟合，不应采用。",
        "2. 如果 full、train、valid 同时改善，才进入下一轮逐年和逐笔复盘。",
        "3. 这一步的目标是识别是否值得继续研究，不是新增正式过滤器。",
    ]
    (out_dir / "false_panic_pause_audit_report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "input": str(source),
        "output_dir": str(out_dir),
        "policies": list(masks.keys()),
        "rows": int(len(raw)),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
