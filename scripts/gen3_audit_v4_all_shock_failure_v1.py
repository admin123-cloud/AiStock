from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "reports" / "gen3_v4_research_package_v1"
OUT_DIR = ROOT / "reports" / "gen3_v4_all_shock_failure_audit_v1"
INITIAL_CAPITAL = 150_000.0

VARIANTS = ["v4_h10_margin", "v4_h5_margin"]
BASE_PROFILE = "cost30"
SHOCK_PROFILE = "cost30_all_shock2"


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, low_memory=False)


def _max_drawdown(equity: pd.Series) -> float:
    values = pd.to_numeric(equity, errors="coerce").dropna()
    if values.empty:
        return 0.0
    return float((values / values.cummax() - 1.0).min())


def _drawdown_window(curve: pd.DataFrame) -> dict[str, Any]:
    if curve.empty:
        return {}
    d = curve.copy()
    d["equity"] = pd.to_numeric(d["equity"], errors="coerce")
    d = d.dropna(subset=["equity"]).reset_index(drop=True)
    if d.empty:
        return {}
    d["peak"] = d["equity"].cummax()
    d["dd"] = d["equity"] / d["peak"] - 1.0
    trough_idx = int(d["dd"].idxmin())
    peak_idx = int(d.loc[:trough_idx, "equity"].idxmax())
    return {
        "peak_date": str(d.loc[peak_idx, "date"]),
        "trough_date": str(d.loc[trough_idx, "date"]),
        "drawdown": float(d.loc[trough_idx, "dd"]),
        "peak_equity": float(d.loc[peak_idx, "equity"]),
        "trough_equity": float(d.loc[trough_idx, "equity"]),
        "days_to_trough": int(trough_idx - peak_idx),
    }


def _curve_summary(curve: pd.DataFrame, profile: str) -> dict[str, Any]:
    if curve.empty:
        return {
            "profile": profile,
            "trading_days": 0,
            "total_return": None,
            "max_drawdown": None,
            "worst_open_mtm_ret": None,
        }
    d = curve.copy()
    d["equity"] = pd.to_numeric(d["equity"], errors="coerce")
    return {
        "profile": profile,
        "trading_days": int(len(d)),
        "total_return": float(d["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0),
        "max_drawdown": _max_drawdown(d["equity"]),
        "worst_open_mtm_ret": float(pd.to_numeric(d.get("worst_open_mtm_ret"), errors="coerce").min()),
        "drawdown_window": _drawdown_window(d),
    }


def _prep_trades(df: pd.DataFrame, profile: str) -> pd.DataFrame:
    if df.empty:
        return df
    d = df.copy()
    d["profile"] = profile
    d["entry_date"] = d["entry_date"].astype(str)
    d["code"] = d["code"].astype(str)
    d["route"] = d["route"].astype(str)
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    d["realized_pnl"] = pd.to_numeric(d["realized_pnl"], errors="coerce")
    d["stake"] = pd.to_numeric(d.get("stake"), errors="coerce")
    d["entry_year"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.year
    d["trade_key"] = d["entry_date"] + "|" + d["code"] + "|" + d["route"]
    return d


def _compare_trades(base: pd.DataFrame, shock: pd.DataFrame) -> pd.DataFrame:
    b = _prep_trades(base, BASE_PROFILE)
    s = _prep_trades(shock, SHOCK_PROFILE)
    keep = ["trade_key", "entry_date", "code", "name", "route", "route_source", "entry_year", "policy_net_ret", "realized_pnl", "stake"]
    merged = b[keep].merge(
        s[keep],
        on="trade_key",
        how="outer",
        suffixes=("_base", "_shock"),
        indicator=True,
    )
    for col in ["entry_date", "code", "name", "route", "route_source", "entry_year"]:
        merged[col] = merged[f"{col}_base"].combine_first(merged[f"{col}_shock"])
    merged["ret_drop"] = merged["policy_net_ret_shock"] - merged["policy_net_ret_base"]
    merged["pnl_drop"] = merged["realized_pnl_shock"] - merged["realized_pnl_base"]
    merged["abs_pnl_drop"] = merged["pnl_drop"].abs()
    merged["stake_drop"] = merged["stake_shock"] - merged["stake_base"]
    merged["direct_cost_effect"] = merged["stake_base"] * merged["ret_drop"]
    merged["path_stake_effect"] = merged["stake_drop"] * merged["policy_net_ret_shock"]
    merged["effect_residual"] = merged["pnl_drop"] - merged["direct_cost_effect"] - merged["path_stake_effect"]
    merged["became_loss"] = (merged["policy_net_ret_base"] > 0) & (merged["policy_net_ret_shock"] <= 0)
    merged["base_loss_deeper"] = (merged["policy_net_ret_base"] <= 0) & (merged["policy_net_ret_shock"] < merged["policy_net_ret_base"])
    return merged


def _route_delta(comp: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for route, g in comp.groupby("route", dropna=False):
        rows.append(
            {
                "route": route,
                "trade_count": int(len(g)),
                "matched_trades": int(g["_merge"].eq("both").sum()),
                "base_pnl": float(g["realized_pnl_base"].sum(skipna=True)),
                "shock_pnl": float(g["realized_pnl_shock"].sum(skipna=True)),
                "pnl_drop": float(g["pnl_drop"].sum(skipna=True)),
                "direct_cost_effect": float(g["direct_cost_effect"].sum(skipna=True)),
                "path_stake_effect": float(g["path_stake_effect"].sum(skipna=True)),
                "avg_stake_base": float(g["stake_base"].mean(skipna=True)),
                "avg_stake_shock": float(g["stake_shock"].mean(skipna=True)),
                "avg_base_ret": float(g["policy_net_ret_base"].mean(skipna=True)),
                "avg_shock_ret": float(g["policy_net_ret_shock"].mean(skipna=True)),
                "win_rate_base": float((g["policy_net_ret_base"] > 0).mean()),
                "win_rate_shock": float((g["policy_net_ret_shock"] > 0).mean()),
                "became_loss_count": int(g["became_loss"].sum()),
                "deeper_loss_count": int(g["base_loss_deeper"].sum()),
            }
        )
    return pd.DataFrame(rows).sort_values("pnl_drop")


def _year_route_delta(comp: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (year, route), g in comp.groupby(["entry_year", "route"], dropna=False):
        rows.append(
            {
                "entry_year": int(year) if pd.notna(year) else None,
                "route": route,
                "trade_count": int(len(g)),
                "base_pnl": float(g["realized_pnl_base"].sum(skipna=True)),
                "shock_pnl": float(g["realized_pnl_shock"].sum(skipna=True)),
                "pnl_drop": float(g["pnl_drop"].sum(skipna=True)),
                "direct_cost_effect": float(g["direct_cost_effect"].sum(skipna=True)),
                "path_stake_effect": float(g["path_stake_effect"].sum(skipna=True)),
                "avg_stake_base": float(g["stake_base"].mean(skipna=True)),
                "avg_stake_shock": float(g["stake_shock"].mean(skipna=True)),
                "became_loss_count": int(g["became_loss"].sum()),
            }
        )
    return pd.DataFrame(rows).sort_values(["entry_year", "pnl_drop"])


def _top_drop(comp: pd.DataFrame, n: int = 30) -> pd.DataFrame:
    cols = [
        "entry_date",
        "code",
        "name",
        "route",
        "route_source",
        "policy_net_ret_base",
        "policy_net_ret_shock",
        "ret_drop",
        "realized_pnl_base",
        "realized_pnl_shock",
        "pnl_drop",
        "stake_base",
        "stake_shock",
        "direct_cost_effect",
        "path_stake_effect",
        "became_loss",
        "base_loss_deeper",
    ]
    return comp.sort_values("pnl_drop").head(n)[cols]


def _pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _num(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value):,.2f}"


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, money_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    money_cols = money_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = _pct(value)
            elif col in money_cols:
                item[col] = _num(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def _write_report(all_summary: list[dict[str, Any]], route_tables: dict[str, pd.DataFrame], top_tables: dict[str, pd.DataFrame]) -> None:
    lines = [
        "# G3 V4 全链路 -2% 冲击失败归因 v1",
        "",
        "## 目的",
        "",
        "这一步不做调参，只回答一个问题：V4 在全链路额外 -2% 成交冲击下，收益和回撤被谁打坏。",
        "",
        "## 总览",
        "",
        _md_table(
            pd.DataFrame(all_summary),
            pct_cols={"base_total_return", "shock_total_return", "base_max_drawdown", "shock_max_drawdown", "return_drop"},
            money_cols={"base_final_equity", "shock_final_equity"},
        ),
        "",
    ]
    for variant in VARIANTS:
        route_df = route_tables.get(variant, pd.DataFrame())
        top_df = top_tables.get(variant, pd.DataFrame())
        lines.extend(
            [
                f"## {variant} 链路损伤",
                "",
                _md_table(
                    route_df[
                        [
                            "route",
                            "trade_count",
                            "base_pnl",
                            "shock_pnl",
                            "pnl_drop",
                            "direct_cost_effect",
                            "path_stake_effect",
                            "avg_stake_base",
                            "avg_stake_shock",
                            "avg_base_ret",
                            "avg_shock_ret",
                            "win_rate_base",
                            "win_rate_shock",
                            "became_loss_count",
                        ]
                    ],
                    pct_cols={"avg_base_ret", "avg_shock_ret", "win_rate_base", "win_rate_shock"},
                    money_cols={"base_pnl", "shock_pnl", "pnl_drop", "direct_cost_effect", "path_stake_effect", "avg_stake_base", "avg_stake_shock"},
                ),
                "",
                f"## {variant} 最大损伤样本",
                "",
                _md_table(
                    top_df.head(15),
                    pct_cols={"policy_net_ret_base", "policy_net_ret_shock", "ret_drop"},
                    money_cols={"realized_pnl_base", "realized_pnl_shock", "pnl_drop", "stake_base", "stake_shock", "direct_cost_effect", "path_stake_effect"},
                ),
                "",
            ]
        )
    lines.extend(
        [
            "## 初步判断",
            "",
            "- 全链路冲击不是单纯的 range 问题，strong_main 的交易数量和仓位会放大冲击。",
            "- 损伤需要区分直接成交成本和权益路径损伤：如果 path_stake_effect 占比高，问题不是某一笔多亏 2%，而是前期冲击让后续大赢家吃不到足够仓位。",
            "- 如果主要损伤来自强势链路，下一步应该先做 strong_main 的成交保护和退出可见性，而不是继续加仓或扩大强势源。",
            "- 如果主要损伤来自 range_gap，当前 range margin 只能作为低频研究样本，不能升级成完整横盘体系。",
            "- 这份审计只定位失败来源，不产生新的买点规则。",
        ]
    )
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_summary: list[dict[str, Any]] = []
    route_tables: dict[str, pd.DataFrame] = {}
    top_tables: dict[str, pd.DataFrame] = {}

    for variant in VARIANTS:
        base_dir = PACKAGE_DIR / f"{variant}__{BASE_PROFILE}"
        shock_dir = PACKAGE_DIR / f"{variant}__{SHOCK_PROFILE}"
        base_curve = _read_csv(base_dir / "mtm_equity_curve.csv")
        shock_curve = _read_csv(shock_dir / "mtm_equity_curve.csv")
        base_trades = _read_csv(base_dir / "closed_trades.csv")
        shock_trades = _read_csv(shock_dir / "closed_trades.csv")

        comp = _compare_trades(base_trades, shock_trades)
        route_df = _route_delta(comp)
        year_route_df = _year_route_delta(comp)
        top_df = _top_drop(comp)

        base_summary = _curve_summary(base_curve, BASE_PROFILE)
        shock_summary = _curve_summary(shock_curve, SHOCK_PROFILE)
        base_final = float(pd.to_numeric(base_curve["equity"], errors="coerce").iloc[-1]) if not base_curve.empty else None
        shock_final = float(pd.to_numeric(shock_curve["equity"], errors="coerce").iloc[-1]) if not shock_curve.empty else None
        all_summary.append(
            {
                "variant": variant,
                "base_trade_count": int(len(base_trades)),
                "shock_trade_count": int(len(shock_trades)),
                "base_total_return": base_summary["total_return"],
                "shock_total_return": shock_summary["total_return"],
                "return_drop": shock_summary["total_return"] - base_summary["total_return"],
                "base_max_drawdown": base_summary["max_drawdown"],
                "shock_max_drawdown": shock_summary["max_drawdown"],
                "base_final_equity": base_final,
                "shock_final_equity": shock_final,
                "shock_drawdown_peak": shock_summary["drawdown_window"].get("peak_date"),
                "shock_drawdown_trough": shock_summary["drawdown_window"].get("trough_date"),
            }
        )

        variant_dir = OUT_DIR / variant
        variant_dir.mkdir(parents=True, exist_ok=True)
        comp.to_csv(variant_dir / "trade_delta.csv", index=False, encoding="utf-8-sig")
        route_df.to_csv(variant_dir / "route_delta.csv", index=False, encoding="utf-8-sig")
        year_route_df.to_csv(variant_dir / "year_route_delta.csv", index=False, encoding="utf-8-sig")
        top_df.to_csv(variant_dir / "top_loss_contributors.csv", index=False, encoding="utf-8-sig")
        route_tables[variant] = route_df
        top_tables[variant] = top_df

    pd.DataFrame(all_summary).to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps({"status": "completed", "rows": all_summary}, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(all_summary, route_tables, top_tables)
    print(pd.DataFrame(all_summary).to_string(index=False))
    for variant, table in route_tables.items():
        print()
        print(variant)
        print(table.to_string(index=False))


if __name__ == "__main__":
    main()
