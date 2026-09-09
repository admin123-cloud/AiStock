from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import sys
from pathlib import Path

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import SOURCE, _classify_market_style, _md_table


OUT_DIR = _report_path() / "gen3_strong_volume5_stop_recovery_v1"


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def _summary(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    rows = []
    for key, g in df.groupby(group_cols, dropna=False):
        if not isinstance(key, tuple):
            key = (key,)
        item = {col: value for col, value in zip(group_cols, key)}
        stop = g["stop5_touch_30m"].astype(bool)
        item.update(
            {
                "rows": int(len(g)),
                "stop_rows": int(stop.sum()),
                "stop_rate": float(stop.mean()) if len(g) else 0.0,
                "h5_mean": float(g["h5"].mean()),
                "h5_win": float((g["h5"] > 0).mean()),
                "h5_worst": float(g["h5"].min()),
                "stop_h5_mean": float(g.loc[stop, "h5"].mean()) if stop.any() else 0.0,
                "stop_h5_win": float((g.loc[stop, "h5"] > 0).mean()) if stop.any() else 0.0,
                "stop_h5_recover5": float((g.loc[stop, "h5"] >= 0.05).mean()) if stop.any() else 0.0,
                "stop_h5_bad10": float((g.loc[stop, "h5"] <= -0.10).mean()) if stop.any() else 0.0,
                "nostop_h5_mean": float(g.loc[~stop, "h5"].mean()) if (~stop).any() else 0.0,
                "nostop_h5_win": float((g.loc[~stop, "h5"] > 0).mean()) if (~stop).any() else 0.0,
            }
        )
        rows.append(item)
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.read_parquet(SOURCE)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.normalize()
    df = df[df["source_family"].eq("volume5")].copy()
    df["h5"] = pd.to_numeric(df["outcome_fwd_ret_5d"], errors="coerce").where(
        pd.to_numeric(df["outcome_fwd_ret_5d"], errors="coerce").notna(),
        pd.to_numeric(df["fwd_ret_5d"], errors="coerce"),
    )
    df["h10"] = pd.to_numeric(df["outcome_fwd_ret_10d"], errors="coerce").where(
        pd.to_numeric(df["outcome_fwd_ret_10d"], errors="coerce").notna(),
        pd.to_numeric(df["fwd_ret_10d"], errors="coerce"),
    )
    df["stop5_touch_30m"] = df.get("stop5_touch_30m", False).fillna(False).astype(bool)
    df["stop5_touch_datetime"] = pd.to_datetime(df.get("stop5_touch_datetime"), errors="coerce")
    df["g3_market_style"] = df.apply(_classify_market_style, axis=1)
    df = df[df["g3_market_style"].isin(["main_up", "weak_recovery"])].dropna(subset=["h5"]).copy()
    df["year"] = df["entry_date"].dt.year

    overall = _summary(df, ["g3_market_style"])
    yearly = _summary(df, ["year"])
    style_year = _summary(df, ["g3_market_style", "year"])
    stopped = df[df["stop5_touch_30m"]].copy()
    recovered = stopped.sort_values("h5", ascending=False).head(20)
    failed = stopped.sort_values("h5", ascending=True).head(20)

    overall.to_csv(OUT_DIR / "stop_recovery_by_style.csv", index=False, encoding="utf-8-sig")
    yearly.to_csv(OUT_DIR / "stop_recovery_by_year.csv", index=False, encoding="utf-8-sig")
    style_year.to_csv(OUT_DIR / "stop_recovery_by_style_year.csv", index=False, encoding="utf-8-sig")
    keep_cols = [c for c in ["entry_date", "code", "name", "g3_market_style", "v4_rank", "v4_score", "stop5_touch_datetime", "h5", "h10"] if c in df.columns]
    recovered[keep_cols].to_csv(OUT_DIR / "stopped_then_recovered_top20.csv", index=False, encoding="utf-8-sig")
    failed[keep_cols].to_csv(OUT_DIR / "stopped_then_failed_top20.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "stop_rate",
        "h5_mean",
        "h5_win",
        "h5_worst",
        "stop_h5_mean",
        "stop_h5_win",
        "stop_h5_recover5",
        "stop_h5_bad10",
        "nostop_h5_mean",
        "nostop_h5_win",
        "h5",
        "h10",
    }
    report = [
        "# G3 强势链路 Volume5 Stop 后修复审计 V1",
        "",
        "## 口径",
        "",
        "- 样本：`source_family=volume5` 且 `g3_market_style in [main_up, weak_recovery]`。",
        "- 问题：30m 触及 -5% 是否代表真实失败，还是强势股高波动里的临时下探。",
        "- 指标：`stop_h5_win` 表示触发 stop 后，固定 5 日结果仍为正的比例；`stop_h5_recover5` 表示触发 stop 后 5 日仍大于等于 +5%。",
        "",
        "## 按风格",
        "",
        _md_table(overall, pct_cols=pct_cols),
        "",
        "## 按年度",
        "",
        _md_table(yearly, pct_cols=pct_cols),
        "",
        "## Stop 后修复 Top20",
        "",
        _md_table(recovered[keep_cols].head(20), pct_cols=pct_cols),
        "",
        "## Stop 后失败 Top20",
        "",
        _md_table(failed[keep_cols].head(20), pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 如果 stop 后仍有较高 5 日转正率，强势链路不能使用裸 -5% 全退作为唯一退出。",
        "- 更合理的下一步是做“跌破后不修复/反包失败/板块强度同步转弱”的确认失败退出，而不是把 stop 触发本身当失败。",
        "",
    ]
    (OUT_DIR / "stop_recovery_report_cn.md").write_text("\n".join(report), encoding="utf-8")
    print(
        {
            "out_dir": str(OUT_DIR),
            "rows": len(df),
            "stop_rows": int(df["stop5_touch_30m"].sum()),
            "overall": overall.to_dict(orient="records"),
        }
    )


if __name__ == "__main__":
    main()
