from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_factor_analysis"


def _json_default(value: Any) -> Any:
    if pd.isna(value):
        return None
    return str(value)


def _alpha191_rows() -> list[dict[str, Any]]:
    rows = []
    for idx in range(1, 192):
        rows.append(
            {
                "factor_id": f"alpha{idx:03d}",
                "source": "gtja_alpha191",
                "formula_status": "missing_definition",
                "data_status": "pending_formula",
                "lookahead_risk": "unknown_until_formula_reviewed",
                "usable_for_backtest": False,
                "note": "Waiting for official formula definition and lag review.",
            }
        )
    return rows


def _proxy_rows() -> list[dict[str, Any]]:
    return [
        {
            "factor_id": "px_mom_5",
            "family": "momentum_trend",
            "formula": "close / close.shift(5) - 1",
            "required_fields": "kline_daily.close",
            "window": 5,
            "frequency": "daily",
            "lag_days": 1,
            "data_status": "directly_calculable",
            "usable_for_backtest": True,
        },
        {
            "factor_id": "px_mom_10",
            "family": "momentum_trend",
            "formula": "close / close.shift(10) - 1",
            "required_fields": "kline_daily.close",
            "window": 10,
            "frequency": "daily",
            "lag_days": 1,
            "data_status": "directly_calculable",
            "usable_for_backtest": True,
        },
        {
            "factor_id": "px_mom_20",
            "family": "momentum_trend",
            "formula": "close / close.shift(20) - 1",
            "required_fields": "kline_daily.close",
            "window": 20,
            "frequency": "daily",
            "lag_days": 1,
            "data_status": "directly_calculable",
            "usable_for_backtest": True,
        },
        {
            "factor_id": "px_ma20_gap",
            "family": "momentum_trend",
            "formula": "close / mean(close, 20) - 1",
            "required_fields": "kline_daily.close",
            "window": 20,
            "frequency": "daily",
            "lag_days": 1,
            "data_status": "directly_calculable",
            "usable_for_backtest": True,
        },
        {
            "factor_id": "px_volatility_20",
            "family": "volatility_crowding",
            "formula": "std(pct_change(close), 20)",
            "required_fields": "kline_daily.close",
            "window": 20,
            "frequency": "daily",
            "lag_days": 1,
            "data_status": "directly_calculable",
            "usable_for_backtest": True,
        },
        {
            "factor_id": "px_amplitude_20",
            "family": "volatility_crowding",
            "formula": "mean((high - low) / close, 20)",
            "required_fields": "kline_daily.high,kline_daily.low,kline_daily.close",
            "window": 20,
            "frequency": "daily",
            "lag_days": 1,
            "data_status": "directly_calculable",
            "usable_for_backtest": True,
        },
        {
            "factor_id": "px_turnover_20",
            "family": "liquidity_turnover",
            "formula": "mean(turnover_rate, 20)",
            "required_fields": "kline_daily.turnover_rate",
            "window": 20,
            "frequency": "daily",
            "lag_days": 1,
            "data_status": "directly_calculable",
            "usable_for_backtest": True,
        },
        {
            "factor_id": "px_turnover_z20",
            "family": "liquidity_turnover",
            "formula": "(turnover_rate - mean(turnover_rate, 20)) / std(turnover_rate, 20)",
            "required_fields": "kline_daily.turnover_rate",
            "window": 20,
            "frequency": "daily",
            "lag_days": 1,
            "data_status": "directly_calculable",
            "usable_for_backtest": True,
        },
        {
            "factor_id": "px_amount_ratio_5",
            "family": "volume_price",
            "formula": "amount / mean(amount.shift(1), 5)",
            "required_fields": "kline_daily.amount",
            "window": 5,
            "frequency": "daily",
            "lag_days": 1,
            "data_status": "directly_calculable",
            "usable_for_backtest": True,
        },
        {
            "factor_id": "px_volume_ratio_5",
            "family": "volume_price",
            "formula": "volume / mean(volume.shift(1), 5)",
            "required_fields": "kline_daily.volume",
            "window": 5,
            "frequency": "daily",
            "lag_days": 1,
            "data_status": "directly_calculable",
            "usable_for_backtest": True,
        },
        {
            "factor_id": "px_intraday_strength",
            "family": "volume_price",
            "formula": "(close - open) / open",
            "required_fields": "kline_daily.open,kline_daily.close",
            "window": 1,
            "frequency": "daily",
            "lag_days": 1,
            "data_status": "directly_calculable",
            "usable_for_backtest": True,
        },
        {
            "factor_id": "px_close_position",
            "family": "volume_price",
            "formula": "(close - low) / (high - low)",
            "required_fields": "kline_daily.high,kline_daily.low,kline_daily.close",
            "window": 1,
            "frequency": "daily",
            "lag_days": 1,
            "data_status": "directly_calculable",
            "usable_for_backtest": True,
        },
        {
            "factor_id": "px_reversal_3",
            "family": "reversal_repair",
            "formula": "-1 * (close / close.shift(3) - 1)",
            "required_fields": "kline_daily.close",
            "window": 3,
            "frequency": "daily",
            "lag_days": 1,
            "data_status": "directly_calculable",
            "usable_for_backtest": True,
        },
        {
            "factor_id": "px_market_cap_float",
            "family": "size_liquidity",
            "formula": "close * stocks.float_share",
            "required_fields": "kline_daily.close,stocks.float_share",
            "window": 1,
            "frequency": "daily",
            "lag_days": 1,
            "data_status": "directly_calculable_if_float_share_available",
            "usable_for_backtest": True,
        },
        {
            "factor_id": "px_industry",
            "family": "neutralization_group",
            "formula": "stocks.industry",
            "required_fields": "stocks.industry",
            "window": 1,
            "frequency": "static",
            "lag_days": 0,
            "data_status": "directly_available",
            "usable_for_backtest": True,
        },
    ]


def build_registry(output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    alpha_df = pd.DataFrame(_alpha191_rows())
    proxy_df = pd.DataFrame(_proxy_rows())
    alpha_df.to_csv(output_dir / "alpha191_registry.csv", index=False, encoding="utf-8-sig")
    proxy_df.to_csv(output_dir / "factor_proxy_registry.csv", index=False, encoding="utf-8-sig")

    coverage = {
        "schema_version": 1,
        "status": "registry_scaffold_created",
        "alpha191": {
            "count": 191,
            "formula_status": "missing official formula file in repo",
            "next_action": "import formula definitions, dependencies, and lag rules before IC testing",
        },
        "available_data": {
            "kline_daily": [
                "code",
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "amplitude",
                "change_pct",
                "change_amount",
                "turnover_rate",
            ],
            "kline_minute_30": ["code", "datetime", "open", "high", "low", "close", "volume", "amount"],
            "stocks": ["code", "name", "market", "industry", "list_date", "quit", "st", "float_share", "total_share"],
        },
        "proxy_factors": {
            "count": int(len(proxy_df)),
            "directly_calculable": int(proxy_df["usable_for_backtest"].sum()),
            "families": sorted(proxy_df["family"].unique().tolist()),
        },
        "outputs": {
            "alpha191_registry": "alpha191_registry.csv",
            "factor_proxy_registry": "factor_proxy_registry.csv",
            "coverage": "factor_coverage.json",
            "notes": "factor_analysis_notes.md",
        },
    }
    (output_dir / "factor_coverage.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_notes(output_dir, coverage)
    return coverage


def _write_notes(output_dir: Path, coverage: dict[str, Any]) -> None:
    lines = [
        "# G2 Factor Analysis Registry Scaffold",
        "",
        "This is the first audit layer for the GTJA Alpha191 workstream.",
        "",
        "## Current Status",
        "",
        "- Alpha191 formula definitions are not present in the repository yet.",
        "- A 191-row placeholder registry has been created to track formula import, dependency review and lookahead risk.",
        "- A smaller proxy factor registry has been created using fields that are already available in the current warehouse.",
        "",
        "## Directly Available Factor Families",
        "",
        "- momentum_trend",
        "- volume_price",
        "- liquidity_turnover",
        "- volatility_crowding",
        "- reversal_repair",
        "- size_liquidity",
        "- neutralization_group",
        "",
        "## Next Step",
        "",
        "Import the official Alpha191 formula definitions, map every formula to warehouse fields, then mark each factor as directly calculable, needs extra fields, or not usable for daily backtests.",
    ]
    (output_dir / "factor_analysis_notes.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build G2 factor registry scaffold for Alpha191 and proxy factors.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()
    payload = build_registry(Path(args.output_dir))
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
