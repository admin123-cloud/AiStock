from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _md_table


OUT_DIR = ROOT / "reports" / "gen3_combo_with_range_weak_shadow_v1"
MAIN_CURVE = ROOT / "reports" / "gen3_combo_panic_strong_mtm_v1" / "combo_50_50_mtm_curve.csv"
SHADOW_CURVE = ROOT / "reports" / "gen3_range_weak_shadow_mtm_v1" / "range_weak_50_50_cost30_mtm_curve.csv"


def _load_curve(path: Path, label: str) -> pd.DataFrame:
    d = pd.read_csv(path)
    d["date"] = pd.to_datetime(d["date"], errors="coerce").dt.normalize()
    d["equity"] = pd.to_numeric(d["equity"], errors="coerce")
    d["worst_open_mtm_ret"] = pd.to_numeric(d.get("worst_open_mtm_ret", 0.0), errors="coerce").fillna(0.0)
    d["open_positions"] = pd.to_numeric(d.get("open_positions", 0), errors="coerce").fillna(0).astype(int)
    first = float(d["equity"].dropna().iloc[0])
    d[f"{label}_norm"] = d["equity"] / first
    return d[["date", f"{label}_norm", "worst_open_mtm_ret", "open_positions"]].dropna(subset=["date", f"{label}_norm"])


def _combine(main: pd.DataFrame, shadow: pd.DataFrame, shadow_weight: float) -> pd.DataFrame:
    dates = sorted(set(main["date"]).union(set(shadow["date"])))
    m = main.set_index("date").reindex(dates).ffill()
    s = shadow.set_index("date").reindex(dates).ffill()
    m["main_norm"] = m["main_norm"].fillna(1.0)
    s["shadow_norm"] = s["shadow_norm"].fillna(1.0)
    m["worst_open_mtm_ret"] = m["worst_open_mtm_ret"].fillna(0.0)
    s["worst_open_mtm_ret"] = s["worst_open_mtm_ret"].fillna(0.0)
    m["open_positions"] = m["open_positions"].fillna(0).astype(int)
    s["open_positions"] = s["open_positions"].fillna(0).astype(int)
    out = pd.DataFrame({"date": dates})
    out["equity"] = (1.0 - shadow_weight) * m["main_norm"].values + shadow_weight * s["shadow_norm"].values
    out["main_norm"] = m["main_norm"].values
    out["shadow_norm"] = s["shadow_norm"].values
    out["open_positions"] = m["open_positions"].values + s["open_positions"].values
    out["worst_open_mtm_ret"] = pd.concat(
        [m["worst_open_mtm_ret"].reset_index(drop=True), s["worst_open_mtm_ret"].reset_index(drop=True)],
        axis=1,
    ).min(axis=1)
    out["peak"] = out["equity"].cummax()
    out["drawdown"] = out["equity"] / out["peak"] - 1.0
    out["ret_from_start"] = out["equity"] - 1.0
    out["shadow_weight"] = shadow_weight
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


def _write_report(summary: pd.DataFrame, annual: pd.DataFrame) -> None:
    pct_cols = {"total_ret", "max_drawdown", "worst_open_mtm_ret", "return"}
    focus = annual[annual["year"].isin([2020, 2023, 2024, 2025, 2026])].copy()
    lines = [
        "# G3 主组合加入横盘/弱反弹 Shadow 审计 V1",
        "",
        "## 口径",
        "",
        "- 主组合：第33步 `Panic + Strong 50/50` 逐日 MTM。",
        "- Shadow：第39步 `range_weak_50_50`，30bps 成本，固定 5 日持有。",
        "- 只测试固定 10%/20% shadow 权重，不搜索最优权重。",
        "- 这是组合层审计，不代表横盘/弱反弹已进入正式 G3。",
        "",
        "## Full 窗口",
        "",
        _md_table(summary, pct_cols=pct_cols),
        "",
        "## 关键年度",
        "",
        _md_table(focus.sort_values(["book", "year"]), pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 如果 10% shadow 能明显改善 2023/2024 且不显著恶化 2025/2026，可保留为 shadow 组合观察。",
        "- 如果改善很小或拖累后段收益，继续重写横盘/弱反弹候选源，不做权重优化。",
        "",
    ]
    (OUT_DIR / "combo_with_range_weak_shadow_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    main_curve = _load_curve(MAIN_CURVE, "main")
    shadow_curve = _load_curve(SHADOW_CURVE, "shadow")
    curves = {"main_only": _combine(main_curve, shadow_curve, 0.0)}
    for w in [0.10, 0.20]:
        curves[f"main_{int((1-w)*100)}_shadow_{int(w*100)}"] = _combine(main_curve, shadow_curve, w)

    summary_rows = []
    annual_parts = []
    for book, curve in curves.items():
        curve.to_csv(OUT_DIR / f"{book}_curve.csv", index=False, encoding="utf-8-sig")
        summary_rows.append(_metrics(book, curve))
        annual_parts.append(_annual(book, curve))
    summary = pd.DataFrame(summary_rows)
    annual = pd.concat(annual_parts, ignore_index=True)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual.csv", index=False, encoding="utf-8-sig")
    _write_report(summary, annual)
    print(json.dumps({"out_dir": str(OUT_DIR), "summary": summary.to_dict(orient="records")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
