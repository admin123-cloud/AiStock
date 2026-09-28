from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _md_table


SRC_DIR = ROOT / "reports" / "gen3_combo_panic_strong_execution_stress_v1"
OUT_DIR = ROOT / "reports" / "gen3_combo_chain_contribution_v1"


def _fmt_pct(x: float) -> str:
    return f"{x:.2%}"


def _load_base_curve() -> pd.DataFrame:
    curve = pd.read_csv(SRC_DIR / "base_30bps_curve.csv")
    curve["date"] = pd.to_datetime(curve["date"], errors="coerce").dt.normalize()
    for col in ["equity", "panic_equity", "strong_equity"]:
        curve[col] = pd.to_numeric(curve[col], errors="coerce")
    return curve.dropna(subset=["date", "equity", "panic_equity", "strong_equity"]).sort_values("date")


def _load_base_trades() -> pd.DataFrame:
    trades = pd.read_csv(SRC_DIR / "base_30bps_closed_trades.csv", low_memory=False)
    trades["entry_date"] = pd.to_datetime(trades["entry_date"], errors="coerce").dt.normalize()
    trades["policy_exit_date"] = pd.to_datetime(trades.get("policy_exit_date", trades.get("exit_date")), errors="coerce").dt.normalize()
    trades["net_ret"] = pd.to_numeric(trades["net_ret"], errors="coerce")
    trades["realized_pnl"] = pd.to_numeric(trades.get("realized_pnl"), errors="coerce")
    if "chain" not in trades.columns:
        trades["chain"] = trades.get("g3_chain", "unknown")
    trades["style"] = trades.get("g3_market_style", pd.Series(index=trades.index, dtype=object)).fillna("")
    fallback_style = trades.get("market_style", pd.Series(index=trades.index, dtype=object)).fillna("")
    trades.loc[trades["style"].eq(""), "style"] = fallback_style[trades["style"].eq("")]
    trades.loc[trades["style"].eq(""), "style"] = "unknown"
    return trades.dropna(subset=["entry_date", "net_ret"])


def _annual_chain_contribution(curve: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for year, part in curve.groupby(curve["date"].dt.year):
        part = part.sort_values("date")
        combo_start = float(part["equity"].iloc[0])
        combo_end = float(part["equity"].iloc[-1])
        panic_start = float(part["panic_equity"].iloc[0])
        panic_end = float(part["panic_equity"].iloc[-1])
        strong_start = float(part["strong_equity"].iloc[0])
        strong_end = float(part["strong_equity"].iloc[-1])
        rows.append(
            {
                "year": int(year),
                "combo_return": combo_end / combo_start - 1.0,
                "panic_sleeve_return": panic_end / panic_start - 1.0,
                "strong_sleeve_return": strong_end / strong_start - 1.0,
                "panic_combo_contribution": 0.5 * (panic_end - panic_start) / combo_start,
                "strong_combo_contribution": 0.5 * (strong_end - strong_start) / combo_start,
                "contribution_gap": 0.5 * (panic_end - panic_start + strong_end - strong_start) / combo_start
                - (combo_end / combo_start - 1.0),
            }
        )
    return pd.DataFrame(rows)


def _trade_quality(trades: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    rows = []
    for vals, part in trades.groupby(keys, dropna=False):
        if not isinstance(vals, tuple):
            vals = (vals,)
        row = dict(zip(keys, vals))
        row.update(
            {
                "closed": int(len(part)),
                "win_rate": float((part["net_ret"] > 0).mean()),
                "mean_trade_ret": float(part["net_ret"].mean()),
                "median_trade_ret": float(part["net_ret"].median()),
                "sum_net_ret": float(part["net_ret"].sum()),
                "worst_trade": float(part["net_ret"].min()),
                "bad10_rate": float((part["net_ret"] <= -0.10).mean()),
            }
        )
        rows.append(row)
    return pd.DataFrame(rows).sort_values(keys).reset_index(drop=True)


def _write_report(annual: pd.DataFrame, yearly_quality: pd.DataFrame, style_quality: pd.DataFrame) -> None:
    pct_cols = {
        "combo_return",
        "panic_sleeve_return",
        "strong_sleeve_return",
        "panic_combo_contribution",
        "strong_combo_contribution",
        "contribution_gap",
        "win_rate",
        "mean_trade_ret",
        "median_trade_ret",
        "sum_net_ret",
        "worst_trade",
        "bad10_rate",
    }
    annual_view = annual.copy()
    annual_view["main_driver"] = annual_view.apply(
        lambda r: "panic" if abs(r["panic_combo_contribution"]) >= abs(r["strong_combo_contribution"]) else "strong",
        axis=1,
    )
    style_focus = style_quality.sort_values(["chain", "closed"], ascending=[True, False]).head(30)
    lines = [
        "# G3 Combo Chain Contribution V1",
        "",
        "## Annual Sleeve Attribution",
        "",
        _md_table(annual_view, pct_cols=pct_cols),
        "",
        "## Yearly Trade Quality",
        "",
        _md_table(yearly_quality, pct_cols=pct_cols),
        "",
        "## Style Trade Quality",
        "",
        _md_table(style_focus, pct_cols=pct_cols),
        "",
        "## Notes",
        "",
        "- Contributions are based on the base_30bps 50/50 sleeve equity curve from Step 35.",
        "- This is attribution, not a new optimized strategy. It should be used to decide which chain needs more research.",
        "",
    ]
    (OUT_DIR / "combo_chain_contribution_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    curve = _load_base_curve()
    trades = _load_base_trades()
    annual = _annual_chain_contribution(curve)
    yearly_quality = _trade_quality(trades.assign(year=trades["entry_date"].dt.year), ["year", "chain"])
    style_quality = _trade_quality(trades, ["chain", "style"])
    annual.to_csv(OUT_DIR / "annual_chain_contribution.csv", index=False, encoding="utf-8-sig")
    yearly_quality.to_csv(OUT_DIR / "yearly_trade_quality.csv", index=False, encoding="utf-8-sig")
    style_quality.to_csv(OUT_DIR / "style_trade_quality.csv", index=False, encoding="utf-8-sig")
    _write_report(annual, yearly_quality, style_quality)
    print(
        json.dumps(
            {
                "out_dir": str(OUT_DIR),
                "annual": annual.to_dict(orient="records"),
                "chain_quality": _trade_quality(trades, ["chain"]).to_dict(orient="records"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
