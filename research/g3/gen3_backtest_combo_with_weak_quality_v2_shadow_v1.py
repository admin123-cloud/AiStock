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

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _md_table


OUT_DIR = _report_path() / "gen3_combo_with_weak_quality_v2_shadow_v1"
MAIN_CURVE = _report_path() / "gen3_combo_panic_strong_mtm_v1" / "combo_50_50_mtm_curve.csv"
WEAK_CURVE = _report_path() / "gen3_range_weak_source_quality_v1" / "weak_quality_v2_cost50_mtm_curve.csv"
WEIGHTS = [0.0, 0.05, 0.10, 0.15]


def _load_curve(path: Path, label: str) -> pd.DataFrame:
    d = pd.read_csv(path)
    d["date"] = pd.to_datetime(d["date"], errors="coerce").dt.normalize()
    d["equity"] = pd.to_numeric(d["equity"], errors="coerce")
    d["worst_open_mtm_ret"] = pd.to_numeric(d.get("worst_open_mtm_ret", 0.0), errors="coerce").fillna(0.0)
    d["open_positions"] = pd.to_numeric(d.get("open_positions", 0), errors="coerce").fillna(0).astype(int)
    first = float(d["equity"].dropna().iloc[0])
    d[f"{label}_norm"] = d["equity"] / first
    return d[["date", f"{label}_norm", "worst_open_mtm_ret", "open_positions"]].dropna(subset=["date", f"{label}_norm"])


def _combine(main: pd.DataFrame, weak: pd.DataFrame, weak_weight: float) -> pd.DataFrame:
    dates = sorted(set(main["date"]).union(set(weak["date"])))
    m = main.set_index("date").reindex(dates).ffill()
    w = weak.set_index("date").reindex(dates).ffill()
    m["main_norm"] = m["main_norm"].fillna(1.0)
    w["weak_norm"] = w["weak_norm"].fillna(1.0)
    m["worst_open_mtm_ret"] = m["worst_open_mtm_ret"].fillna(0.0)
    w["worst_open_mtm_ret"] = w["worst_open_mtm_ret"].fillna(0.0)
    m["open_positions"] = m["open_positions"].fillna(0).astype(int)
    w["open_positions"] = w["open_positions"].fillna(0).astype(int)
    out = pd.DataFrame({"date": dates})
    out["equity"] = (1.0 - weak_weight) * m["main_norm"].values + weak_weight * w["weak_norm"].values
    out["main_norm"] = m["main_norm"].values
    out["weak_norm"] = w["weak_norm"].values
    out["open_positions"] = m["open_positions"].values + w["open_positions"].values
    out["worst_open_mtm_ret"] = pd.concat(
        [m["worst_open_mtm_ret"].reset_index(drop=True), w["worst_open_mtm_ret"].reset_index(drop=True)],
        axis=1,
    ).min(axis=1)
    out["peak"] = out["equity"].cummax()
    out["drawdown"] = out["equity"] / out["peak"] - 1.0
    out["ret_from_start"] = out["equity"] - 1.0
    out["weak_weight"] = weak_weight
    return out


def _metrics(book: str, curve: pd.DataFrame) -> dict:
    return {
        "book": book,
        "start": pd.Timestamp(curve["date"].min()).strftime("%Y-%m-%d"),
        "end": pd.Timestamp(curve["date"].max()).strftime("%Y-%m-%d"),
        "total_ret": float(curve["equity"].iloc[-1] - 1.0),
        "max_drawdown": float(curve["drawdown"].min()),
        "worst_open_mtm_ret": float(curve["worst_open_mtm_ret"].min()),
        "max_open_positions": int(curve["open_positions"].max()),
    }


def _annual(book: str, curve: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for year, part in curve.groupby(curve["date"].dt.year):
        part = part.sort_values("date")
        rows.append(
            {
                "book": book,
                "year": int(year),
                "return": float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0),
                "max_drawdown": float(part["equity"].div(part["equity"].cummax()).sub(1.0).min()),
                "worst_open_mtm_ret": float(part["worst_open_mtm_ret"].min()),
            }
        )
    return pd.DataFrame(rows)


def _window_metrics(book: str, curve: pd.DataFrame, name: str, start: str, end: str) -> dict:
    d = curve[(curve["date"] >= pd.Timestamp(start)) & (curve["date"] <= pd.Timestamp(end))].copy()
    if d.empty:
        return {"book": book, "window": name}
    d = d.sort_values("date")
    local = d["equity"] / float(d["equity"].iloc[0])
    return {
        "book": book,
        "window": name,
        "start": pd.Timestamp(d["date"].min()).strftime("%Y-%m-%d"),
        "end": pd.Timestamp(d["date"].max()).strftime("%Y-%m-%d"),
        "return": float(local.iloc[-1] - 1.0),
        "max_drawdown": float(local.div(local.cummax()).sub(1.0).min()),
        "worst_open_mtm_ret": float(d["worst_open_mtm_ret"].min()),
    }


def _write_report(summary: pd.DataFrame, annual: pd.DataFrame, windows: pd.DataFrame) -> None:
    pct_cols = {"total_ret", "max_drawdown", "worst_open_mtm_ret", "return"}
    focus = annual[annual["year"].isin([2020, 2022, 2023, 2024, 2025, 2026])].copy()
    lines = [
        "# G3 主组合加入 Weak Quality V2 小权重审计 V1",
        "",
        "## 口径",
        "",
        "- 主组合：第33步 `Panic + Strong 50/50` 逐日 MTM。",
        "- 影子增强：第45步 `weak_quality_v2`，50bps 成本、固定 5 日持有、Top1。",
        "- 权重：只测 0%、5%、10%、15%，不搜索最优权重。",
        "- 目的：验证弱反弹质量源是否能补 2023/2024，同时不明显拖累 2025/2026 与 full 收益。",
        "- 这仍是组合层审计，不代表 `weak_quality_v2` 进入正式 G3。",
        "",
        "## Full 窗口",
        "",
        _md_table(summary, pct_cols=pct_cols),
        "",
        "## 关键年份",
        "",
        _md_table(focus.sort_values(["book", "year"]), pct_cols=pct_cols),
        "",
        "## 分段窗口",
        "",
        _md_table(windows.sort_values(["book", "window"]), pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 如果 5%-10% 权重不能改善 2023/2024，说明它不是有效的组合补丁。",
        "- 如果 15% 权重开始拖累 full 或 2025/2026，则上限应控制在影子观察权重。",
        "- 只有在提升组合收益/回撤比且不过度依赖单一年份时，才进入下一轮执行压力测试。",
        "",
    ]
    (OUT_DIR / "combo_with_weak_quality_v2_shadow_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    main_curve = _load_curve(MAIN_CURVE, "main")
    weak_curve = _load_curve(WEAK_CURVE, "weak")
    curves = {}
    for weight in WEIGHTS:
        if weight == 0:
            book = "main_only"
        else:
            book = f"main_{int((1-weight)*100)}_weak_{int(weight*100)}"
        curves[book] = _combine(main_curve, weak_curve, weight)

    summary_rows = []
    annual_parts = []
    window_rows = []
    windows = {
        "train_2020_2023": ("2020-01-01", "2023-12-31"),
        "valid_2024_2025": ("2024-01-01", "2025-12-31"),
        "blind_2026ytd": ("2026-01-01", "2026-12-31"),
        "weak_gap_2022_2023": ("2022-01-01", "2023-12-31"),
        "recent_2025_2026": ("2025-01-01", "2026-12-31"),
    }
    for book, curve in curves.items():
        curve.to_csv(OUT_DIR / f"{book}_curve.csv", index=False, encoding="utf-8-sig")
        summary_rows.append(_metrics(book, curve))
        annual_parts.append(_annual(book, curve))
        for window, (start, end) in windows.items():
            window_rows.append(_window_metrics(book, curve, window, start, end))
    summary = pd.DataFrame(summary_rows)
    annual = pd.concat(annual_parts, ignore_index=True)
    windows_df = pd.DataFrame(window_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual.csv", index=False, encoding="utf-8-sig")
    windows_df.to_csv(OUT_DIR / "window_summary.csv", index=False, encoding="utf-8-sig")
    _write_report(summary, annual, windows_df)
    print(json.dumps({"out_dir": str(OUT_DIR), "summary": summary.to_dict(orient="records")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
