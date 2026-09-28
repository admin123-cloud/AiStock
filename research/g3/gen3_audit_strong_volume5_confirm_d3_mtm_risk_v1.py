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

from scripts.gen3_backtest_strong_volume5_confirm_fail_exit_v1 import _apply_policy, _prepare_base
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _md_table


OUT_DIR = _report_path() / "gen3_strong_volume5_confirm_d3_mtm_risk_v1"


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def _bucket(d: pd.DataFrame, label_col: str) -> pd.DataFrame:
    rows = []
    for label, g in d.groupby(label_col, dropna=False):
        rows.append(
            {
                label_col: label,
                "rows": int(len(g)),
                "mean_net_ret": float(g["net_ret"].mean()),
                "win_rate": float((g["net_ret"] > 0).mean()),
                "worst_net_ret": float(g["net_ret"].min()),
                "mean_mae3": float(g["mae_close_3d"].mean()),
                "worst_mae3": float(g["mae_close_3d"].min()),
                "mae3_le_m8": float((g["mae_close_3d"] <= -0.08).mean()),
                "mae3_le_m12": float((g["mae_close_3d"] <= -0.12).mean()),
                "mae5_le_m12": float((g["mae_close_5d"] <= -0.12).mean()),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = _prepare_base(30.0)
    for col in ["mae_close_1d", "mae_close_2d", "mae_close_3d", "mae_close_5d", "mfe_close_3d", "mfe_close_5d"]:
        base[col] = pd.to_numeric(base.get(col), errors="coerce")
    confirm = _apply_policy(base, "confirm_d3_le0", 30.0)
    fixed = _apply_policy(base, "fixed_h5", 30.0)

    compare = []
    for label, d in [("fixed_h5", fixed), ("confirm_d3_le0", confirm)]:
        compare.append(
            {
                "policy": label,
                "rows": int(len(d)),
                "mean_net_ret": float(d["net_ret"].mean()),
                "win_rate": float((d["net_ret"] > 0).mean()),
                "worst_net_ret": float(d["net_ret"].min()),
                "mean_mae3": float(d["mae_close_3d"].mean()),
                "worst_mae3": float(d["mae_close_3d"].min()),
                "mae3_le_m8": float((d["mae_close_3d"] <= -0.08).mean()),
                "mae3_le_m12": float((d["mae_close_3d"] <= -0.12).mean()),
                "mae5_le_m12": float((d["mae_close_5d"] <= -0.12).mean()),
            }
        )
    compare_df = pd.DataFrame(compare)
    by_reason = _bucket(confirm, "exit_reason_proxy")
    confirm["year"] = confirm["entry_date"].dt.year
    by_year = _bucket(confirm, "year")
    worst_wait = confirm.sort_values("mae_close_3d").head(30)
    cols = [
        "entry_date",
        "policy_exit_date",
        "code",
        "name",
        "g3_market_style",
        "exit_reason_proxy",
        "net_ret",
        "mae_close_1d",
        "mae_close_2d",
        "mae_close_3d",
        "mae_close_5d",
        "mfe_close_3d",
    ]
    cols = [c for c in cols if c in worst_wait.columns]

    compare_df.to_csv(OUT_DIR / "confirm_d3_mtm_compare.csv", index=False, encoding="utf-8-sig")
    by_reason.to_csv(OUT_DIR / "confirm_d3_mtm_by_reason.csv", index=False, encoding="utf-8-sig")
    by_year.to_csv(OUT_DIR / "confirm_d3_mtm_by_year.csv", index=False, encoding="utf-8-sig")
    worst_wait[cols].to_csv(OUT_DIR / "confirm_d3_worst_wait_mtm_samples.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "mean_net_ret",
        "win_rate",
        "worst_net_ret",
        "mean_mae3",
        "worst_mae3",
        "mae3_le_m8",
        "mae3_le_m12",
        "mae5_le_m12",
        "net_ret",
        "mae_close_1d",
        "mae_close_2d",
        "mae_close_3d",
        "mae_close_5d",
        "mfe_close_3d",
    }
    lines = [
        "# G3 强势链路 Confirm D3 等待期 MTM 风险审计 V1",
        "",
        "## 口径",
        "",
        "- 审计对象：`confirm_d3_le0` 退出候选。",
        "- 使用源字段 `mae_close_1d/2d/3d/5d` 作为收盘级等待期浮亏代理；这不是 30m 最差价，因此仍低估盘中痛感。",
        "- 目标：确认等待第3日收盘是否会引入不可承受的持仓浮亏。",
        "",
        "## Fixed vs Confirm D3",
        "",
        _md_table(compare_df, pct_cols=pct_cols),
        "",
        "## Confirm D3 按退出原因",
        "",
        _md_table(by_reason, pct_cols=pct_cols),
        "",
        "## Confirm D3 按年度",
        "",
        _md_table(by_year, pct_cols=pct_cols),
        "",
        "## 等待期最差样本",
        "",
        _md_table(worst_wait[cols].head(30), pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 如果 `mae3_le_m12` 比例较高，则 D3 确认虽然改善最终收益，但等待体验可能不适合实盘。",
        "- 若等待期风险集中在少数个股，下一步应从 30m 明细里找更早的修复失败信号，而不是提前用日线硬止损。",
        "",
    ]
    (OUT_DIR / "confirm_d3_mtm_risk_report_cn.md").write_text("\n".join(lines), encoding="utf-8")
    print(
        {
            "out_dir": str(OUT_DIR),
            "compare": compare_df.to_dict(orient="records"),
            "by_reason": by_reason.to_dict(orient="records"),
        }
    )


if __name__ == "__main__":
    main()
