from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_confirm_fail_exit_v1 import _apply_policy, _prepare_base
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _md_table


OUT_DIR = ROOT / "reports" / "gen3_strong_volume5_confirm_exit_visibility_v1"


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def _audit_trades(d: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = []
    samples = []
    for reason, g in d.groupby("exit_reason_proxy", dropna=False):
        is_early = str(reason) != "fixed_h5"
        rows.append(
            {
                "exit_reason_proxy": reason,
                "rows": int(len(g)),
                "is_early_exit": bool(is_early),
                "mean_net_ret": float(g["net_ret"].mean()),
                "win_rate": float((g["net_ret"] > 0).mean()),
                "worst_net_ret": float(g["net_ret"].min()),
                "stop_touch_rate": float(g["stop5_touch_30m"].astype(bool).mean()) if "stop5_touch_30m" in g.columns else 0.0,
                "min_hold_days": int((g["policy_exit_date"] - g["entry_date"]).dt.days.min()),
                "median_hold_days": float((g["policy_exit_date"] - g["entry_date"]).dt.days.median()),
                "max_hold_days": int((g["policy_exit_date"] - g["entry_date"]).dt.days.max()),
            }
        )
        cols = [
            "entry_date",
            "policy_exit_date",
            "code",
            "name",
            "g3_market_style",
            "exit_reason_proxy",
            "stop5_touch_datetime",
            "fwd_ret_3d",
            "h5",
            "net_ret",
        ]
        samples.append(g.sort_values("net_ret").head(12)[[c for c in cols if c in g.columns]])
    return pd.DataFrame(rows), pd.concat(samples, ignore_index=True) if samples else pd.DataFrame()


def _year_reason(d: pd.DataFrame) -> pd.DataFrame:
    x = d.copy()
    x["year"] = x["entry_date"].dt.year
    rows = []
    for (year, reason), g in x.groupby(["year", "exit_reason_proxy"], dropna=False):
        rows.append(
            {
                "year": int(year),
                "exit_reason_proxy": reason,
                "rows": int(len(g)),
                "mean_net_ret": float(g["net_ret"].mean()),
                "win_rate": float((g["net_ret"] > 0).mean()),
                "worst_net_ret": float(g["net_ret"].min()),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cost_bps = 30.0
    base = _prepare_base(cost_bps)
    fixed = _apply_policy(base, "fixed_h5", cost_bps)
    confirm = _apply_policy(base, "confirm_d3_le0", cost_bps)
    fixed["policy"] = "fixed_h5"
    confirm["policy"] = "confirm_d3_le0"

    reason_summary, samples = _audit_trades(confirm)
    year_reason = _year_reason(confirm)
    compare_rows = []
    for label, d in [("fixed_h5", fixed), ("confirm_d3_le0", confirm)]:
        compare_rows.append(
            {
                "policy": label,
                "rows": int(len(d)),
                "mean_net_ret": float(d["net_ret"].mean()),
                "win_rate": float((d["net_ret"] > 0).mean()),
                "worst_net_ret": float(d["net_ret"].min()),
                "stop_touch_rate": float(d["stop5_touch_30m"].astype(bool).mean()),
                "bad10_rate": float((d["net_ret"] <= -0.10).mean()),
            }
        )
    compare = pd.DataFrame(compare_rows)

    reason_summary.to_csv(OUT_DIR / "confirm_d3_exit_reason_summary.csv", index=False, encoding="utf-8-sig")
    year_reason.to_csv(OUT_DIR / "confirm_d3_year_reason_summary.csv", index=False, encoding="utf-8-sig")
    compare.to_csv(OUT_DIR / "confirm_d3_vs_fixed_trade_compare.csv", index=False, encoding="utf-8-sig")
    samples.to_csv(OUT_DIR / "confirm_d3_reason_worst_samples.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"mean_net_ret", "win_rate", "worst_net_ret", "stop_touch_rate", "bad10_rate"}
    lines = [
        "# G3 强势链路 Confirm D3 退出可见性审计 V1",
        "",
        "## 口径",
        "",
        "- 审计对象：`core_recovery_volume5` 的 `confirm_d3_le0` 退出候选。",
        "- 规则解释：入场后若 30m 曾触及 -5%，但第 3 个交易日收盘相对入场仍未转正，则第 3 日收盘退出；否则按固定 5 日持有。",
        "- 可见性判断：该规则不能在入场当天提前知道结果，但在第 3 日收盘时可见；因此它不是入场过滤规则，只能是延迟确认退出规则。",
        "- 当前仍是代理版本，因为 `stop5_touch_30m` 来自既有源字段；正式化前必须用 30m 明细重放触发时刻、成交价和不可成交情况。",
        "",
        "## 固定持有 vs Confirm D3",
        "",
        _md_table(compare, pct_cols=pct_cols),
        "",
        "## Confirm D3 退出原因",
        "",
        _md_table(reason_summary, pct_cols=pct_cols),
        "",
        "## 年度退出原因",
        "",
        _md_table(year_reason, pct_cols=pct_cols),
        "",
        "## 最差样本",
        "",
        _md_table(samples.head(30), pct_cols={"fwd_ret_3d", "h5", "net_ret"}),
        "",
        "## 判断",
        "",
        "- `confirm_d3_le0` 不应被写成买入前过滤；它只能在持仓第 3 日收盘触发，属于延迟退出。",
        "- 从可见性上看，它可以被实盘化，但必须接受 3 日等待期间的盘中浮亏，因此下一步需要做逐日/30m MTM 风险审计。",
        "- 若 MTM 审计显示等待期间回撤不可承受，就需要改成更早的 30m 修复失败确认，而不是继续用 D3 收盘。",
        "",
    ]
    (OUT_DIR / "confirm_exit_visibility_report_cn.md").write_text("\n".join(lines), encoding="utf-8")
    print(
        {
            "out_dir": str(OUT_DIR),
            "compare": compare.to_dict(orient="records"),
            "reason_summary": reason_summary.to_dict(orient="records"),
        }
    )


if __name__ == "__main__":
    main()
