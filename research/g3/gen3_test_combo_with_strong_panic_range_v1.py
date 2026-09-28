from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
from datetime import datetime
from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_build_dynamic_router_combo_v1 import (  # noqa: E402
    ROUTE_PRIORITY,
    md_table,
    pct,
    simulate,
    standardize_panic,
    summarize,
    summarize_routes,
    summarize_windows,
)
from scripts.gen3_build_dynamic_router_guarded_v1 import standardize_strong_guarded  # noqa: E402
from scripts.gen3_rebuild_combo_with_range_filter_v1 import (  # noqa: E402
    RANGE_FILTERS,
    STRONG_GUARD,
    _standardize_range_filtered,
)


OUT_DIR = _report_path() / "gen3_combo_with_strong_panic_range_v1"
NEW_RANGE_PATH = (
    _report_path()
    / "gen3_strong_panic_30m_d1d2_exit_v1"
    / "strong_panic_daily_d2_exit__cost30"
    / "closed_trades.csv"
)


COMBO_SPECS = [
    {
        "variant": "panic_strong_only",
        "desc": "只保留 down_panic + strong_main，不接任何横盘/箱体源",
        "old_range": False,
        "new_range": False,
    },
    {
        "variant": "old_range_conservative",
        "desc": "旧组合最优保守 range_gap：过滤 downtrend_strength 和小幅高开",
        "old_range": True,
        "new_range": False,
    },
    {
        "variant": "new_strong_panic_range",
        "desc": "新增 strong_panic_daily_d2_exit 作为横盘/箱体源",
        "old_range": False,
        "new_range": True,
    },
    {
        "variant": "old_plus_new_range",
        "desc": "旧保守 range_gap 与新增 strong_panic 源共同竞争 range 路由名额",
        "old_range": True,
        "new_range": True,
    },
]


def standardize_new_range() -> pd.DataFrame:
    d = pd.read_csv(NEW_RANGE_PATH, low_memory=False)
    out = pd.DataFrame()
    out["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    out["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    out["code"] = d["code"].astype(str)
    out["name"] = d.get("name", "")
    out["route"] = "range_gap"
    out["route_source"] = "strong_panic_daily_d2_exit"
    out["route_priority"] = ROUTE_PRIORITY["range_gap"]
    out["score"] = pd.to_numeric(d.get("candidate_score", 0.0), errors="coerce").fillna(0.0)
    out["entry_price"] = pd.to_numeric(d.get("entry_open"), errors="coerce")
    out["policy_net_ret"] = pd.to_numeric(d.get("policy_net_ret"), errors="coerce")
    return out.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])


def old_conservative_range() -> pd.DataFrame:
    spec = next(s for s in RANGE_FILTERS if s["name"] == "range_conservative_combo_b")
    return _standardize_range_filtered(spec)


def candidate_set(spec: dict[str, Any]) -> pd.DataFrame:
    parts = [standardize_panic(), standardize_strong_guarded(STRONG_GUARD)]
    if spec["old_range"]:
        parts.append(old_conservative_range())
    if spec["new_range"]:
        parts.append(standardize_new_range())
    d = pd.concat(parts, ignore_index=True)
    d = d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"]).copy()
    d["route_priority"] = pd.to_numeric(d["route_priority"], errors="coerce").fillna(0)
    d["score"] = pd.to_numeric(d["score"], errors="coerce").fillna(0)
    return d.sort_values(["entry_date", "route_priority", "score"], ascending=[True, False, False])


def source_counts(candidates: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for source, part in candidates.groupby("route_source", dropna=False):
        rows.append(
            {
                "route_source": source,
                "route": part["route"].iloc[0] if "route" in part else "",
                "candidates": int(len(part)),
                "days": int(part["entry_date"].nunique()) if "entry_date" in part else 0,
            }
        )
    return pd.DataFrame(rows).sort_values(["route", "route_source"])


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_rows = []
    window_rows = []
    route_rows = []
    coverage_rows = []

    for spec in COMBO_SPECS:
        candidates = candidate_set(spec)
        candidates.to_csv(OUT_DIR / f"{spec['variant']}_candidates.csv", index=False, encoding="utf-8-sig")
        counts = source_counts(candidates)
        counts.insert(0, "variant", spec["variant"])
        coverage_rows.append(counts)

        for cost in [30.0, 50.0, 100.0]:
            curve, closed = simulate(candidates, cost)
            tag = f"{spec['variant']}_cost{int(cost)}"
            curve.to_csv(OUT_DIR / f"{tag}_mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(OUT_DIR / f"{tag}_closed_trades.csv", index=False, encoding="utf-8-sig")

            s = summarize(curve, closed, cost)
            s["variant"] = spec["variant"]
            s["desc"] = spec["desc"]
            s["candidate_count"] = int(len(candidates))
            summary_rows.append(s)

            w = summarize_windows(curve, closed)
            w.insert(0, "cost_bps", cost)
            w.insert(0, "variant", spec["variant"])
            window_rows.append(w)

            r = summarize_routes(closed)
            r.insert(0, "cost_bps", cost)
            r.insert(0, "variant", spec["variant"])
            route_rows.append(r)

    summary = pd.DataFrame(summary_rows)
    windows = pd.concat(window_rows, ignore_index=True)
    routes = pd.concat(route_rows, ignore_index=True)
    coverage = pd.concat(coverage_rows, ignore_index=True)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    routes.to_csv(OUT_DIR / "route_attribution.csv", index=False, encoding="utf-8-sig")
    coverage.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8-sig")

    best100 = summary[summary["cost_bps"].eq(100.0)].sort_values(["total_return", "max_drawdown"], ascending=[False, False]).head(1)
    meta = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "best_100bps_variant": str(best100.iloc[0]["variant"]) if not best100.empty else "",
        "best_100bps_total_return": float(best100.iloc[0]["total_return"]) if not best100.empty else None,
        "best_100bps_max_drawdown": float(best100.iloc[0]["max_drawdown"]) if not best100.empty else None,
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "worst_open_mtm_ret", "return"}
    report = [
        "# G3 新 strong_panic 横盘源组合层复验 v1",
        "",
        "## 英文名解释",
        "",
        "- `panic_strong_only`：只使用弱势 panic 链路和强势 strong_main 链路，不接横盘源。",
        "- `old_range_conservative`：旧的保守横盘源，即 `range_conservative_combo_b`。",
        "- `new_strong_panic_range`：把 `strong_panic_daily_d2_exit` 当作新的横盘/箱体源。",
        "- `old_plus_new_range`：旧横盘源和新 strong_panic 横盘源一起竞争每日 range 路由名额。",
        "- `down_panic`：弱势/下跌市场里的恐慌出清反弹买法。",
        "- `strong_main`：强势市场里的主升/突破类买法。",
        "- `range_gap`：横盘/箱体或弱修复市场里的低位反抽买法。",
        "",
        "## 覆盖率",
        "",
        md_table(coverage),
        "",
        "## 组合结果",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 分窗口结果",
        "",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 路由贡献",
        "",
        md_table(routes, pct_cols={"win_rate", "avg_trade_return", "worst_trade"}),
        "",
        "## 当前判断",
        "",
        f"- 100bps 最优组合：`{meta['best_100bps_variant']}`，收益 `{pct(meta['best_100bps_total_return'])}`，回撤 `{pct(meta['best_100bps_max_drawdown'])}`。",
        "- 如果新源不优于旧保守横盘源，后续不应把它纳入 G3 组合，只保留为研究档案。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
