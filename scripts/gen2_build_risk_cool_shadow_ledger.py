from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_backtest_open_v1_portfolio import _json_default, _pct  # noqa: E402


DEFAULT_SOURCE = ROOT / "reports" / "gen2_risk_cool_dynamic_circuit_user_v2_cap_v2" / "sources" / "risk_cool_base.csv"
DEFAULT_RUN_DIR = ROOT / "reports" / "gen2_risk_cool_dynamic_circuit_user_v2_cap_v2" / "backtests" / "two_stop_cd3_skip"
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "gen2_risk_cool_shadow_ledger"


def _load_signals(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    d["entry_price"] = pd.to_numeric(d["entry_price"], errors="coerce")
    d["v4_rank"] = pd.to_numeric(d.get("v4_rank"), errors="coerce")
    d["v4_score"] = pd.to_numeric(d.get("v4_score"), errors="coerce")
    d = d.dropna(subset=["entry_date", "code", "confirm_datetime", "entry_price"]).copy()
    if "g2_v2_family_priority" in d.columns:
        d["g2_v2_family_priority"] = pd.to_numeric(d["g2_v2_family_priority"], errors="coerce").fillna(99).astype(int)
        d = d.sort_values(
            ["entry_date", "g2_v2_family_priority", "v4_score", "v4_rank", "confirm_datetime", "code"],
            ascending=[True, True, False, True, True, True],
        )
    else:
        d = d.sort_values(["entry_date", "confirm_datetime", "v4_rank", "v4_score", "code"], ascending=[True, True, True, False, True])
    d["day_signal_rank"] = d.groupby("entry_date").cumcount() + 1
    d["signal_key"] = d["code"].astype(str) + "|" + d["confirm_datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
    return d


def _load_decisions(run_dir: Path) -> pd.DataFrame:
    d = pd.read_csv(run_dir / "decision_ledger.csv")
    d["date"] = pd.to_datetime(d["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["cooldown_active", "buy_allowed"]:
        d[col] = d[col].astype(str).str.lower().isin(["true", "1"])
    d["capital_weight"] = pd.to_numeric(d.get("capital_weight"), errors="coerce")
    d["bought"] = pd.to_numeric(d.get("bought"), errors="coerce").fillna(0).astype(int)
    return d


def _load_executed(run_dir: Path) -> pd.DataFrame:
    trades = pd.read_csv(run_dir / "trades.csv")
    if trades.empty:
        return pd.DataFrame(columns=["signal_key", "executed_trade_rows", "executed_pnl", "executed_exit_reasons"])
    trades["buy_datetime"] = pd.to_datetime(trades["buy_datetime"], errors="coerce")
    trades["pnl"] = pd.to_numeric(trades["pnl"], errors="coerce")
    trades["signal_key"] = trades["code"].astype(str) + "|" + trades["buy_datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
    return (
        trades.groupby("signal_key", dropna=False)
        .agg(
            executed_trade_rows=("exit_reason", "count"),
            executed_pnl=("pnl", "sum"),
            executed_exit_reasons=("exit_reason", lambda x: "|".join(sorted(set(map(str, x))))),
        )
        .reset_index()
    )


def _build_ledger(source: Path, run_dir: Path) -> pd.DataFrame:
    signals = _load_signals(source)
    decisions = _load_decisions(run_dir).rename(columns={"date": "entry_date"})
    executed = _load_executed(run_dir)
    d = signals.merge(
        decisions[["entry_date", "cooldown_active", "buy_allowed", "capital_weight", "bought", "stop_events_today"]],
        on="entry_date",
        how="left",
    )
    d["cooldown_active"] = d["cooldown_active"].fillna(False).astype(bool)
    d["buy_allowed"] = d["buy_allowed"].fillna(True).astype(bool)
    d["capital_weight"] = pd.to_numeric(d["capital_weight"], errors="coerce").fillna(1.0)
    d["bought"] = pd.to_numeric(d["bought"], errors="coerce").fillna(0).astype(int)
    d = d.merge(executed, on="signal_key", how="left")
    d["shadow_status"] = "observable"
    d.loc[d["cooldown_active"], "shadow_status"] = "suspended_by_two_stop_cd3"
    d.loc[d["executed_trade_rows"].fillna(0).gt(0), "shadow_status"] = "executed"
    d["execution_note"] = ""
    d.loc[d["shadow_status"].eq("executed"), "execution_note"] = "executed by dynamic circuit backtest"
    d.loc[d["shadow_status"].eq("observable") & d["day_signal_rank"].gt(1), "execution_note"] = "observable alternate, not first daily candidate"
    d.loc[d["shadow_status"].eq("observable") & d["day_signal_rank"].eq(1) & d["bought"].eq(0), "execution_note"] = "observable but portfolio slot/cash unavailable"
    d.loc[d["shadow_status"].eq("suspended_by_two_stop_cd3"), "execution_note"] = "cooldown after two recent stop_loss_30m events"
    return d


def _summarize(ledger: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for status, g in ledger.groupby("shadow_status", sort=False):
        row: dict[str, Any] = {
            "shadow_status": status,
            "signals": int(len(g)),
            "days": int(g["entry_date"].nunique()),
            "executed_entries": int(g["executed_trade_rows"].fillna(0).gt(0).sum()),
            "avg_fwd5": float(pd.to_numeric(g.get("outcome_fwd_ret_5d"), errors="coerce").mean()),
            "avg_fwd10": float(pd.to_numeric(g.get("outcome_fwd_ret_10d"), errors="coerce").mean()),
            "avg_fwd20": float(pd.to_numeric(g.get("outcome_fwd_ret_20d"), errors="coerce").mean()),
            "stop5_rate": float(g["stop5_touch_30m"].astype(bool).mean()) if "stop5_touch_30m" in g.columns else None,
            "good_rate": float(g["outcome_good"].astype(bool).mean()) if "outcome_good" in g.columns else None,
            "bad_rate": float(g["outcome_bad"].astype(bool).mean()) if "outcome_bad" in g.columns else None,
        }
        rows.append(row)
    return pd.DataFrame(rows)


def _write_report(output_dir: Path, summary: pd.DataFrame, ledger: pd.DataFrame) -> None:
    lines = [
        "# G2 V3 User V2 two_stop_cd3 Shadow Ledger",
        "",
        "Historical shadow ledger for fixed `G2 V3 User V2 + two_stop_cd3_skip`.",
        "",
        "| status | signals | days | executed | avg5 | avg10 | avg20 | stop5 | good | bad |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summary.to_dict("records"):
        lines.append(
            f"| {row['shadow_status']} | {row['signals']} | {row['days']} | {row['executed_entries']} | "
            f"{_pct(row.get('avg_fwd5'))} | {_pct(row.get('avg_fwd10'))} | {_pct(row.get('avg_fwd20'))} | "
            f"{_pct(row.get('stop5_rate'))} | {_pct(row.get('good_rate'))} | {_pct(row.get('bad_rate'))} |"
        )
    suspended = ledger[ledger["shadow_status"].eq("suspended_by_two_stop_cd3")].copy()
    if not suspended.empty:
        fwd10 = "outcome_fwd_ret_10d"
        worst = suspended.sort_values(fwd10, ascending=True).head(10)
        best = suspended.sort_values(fwd10, ascending=False).head(10)
        lines.extend(["", "## Suspended Best 10 By 10D"])
        for row in best.to_dict("records"):
            lines.append(f"- {row['entry_date']} {row['code']} {row.get('name','')}: 10d={_pct(row.get('outcome_fwd_ret_10d'))}, stop5={row.get('stop5_touch_30m')}")
        lines.extend(["", "## Suspended Worst 10 By 10D"])
        for row in worst.to_dict("records"):
            lines.append(f"- {row['entry_date']} {row['code']} {row.get('name','')}: 10d={_pct(row.get('outcome_fwd_ret_10d'))}, stop5={row.get('stop5_touch_30m')}")
    (output_dir / "shadow_ledger_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    ledger = _build_ledger(Path(args.source), Path(args.run_dir))
    summary = _summarize(ledger)
    ledger.to_csv(output_dir / "shadow_ledger.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    _write_report(output_dir, summary, ledger)
    payload = {
        "schema_version": 1,
        "source": str(args.source),
        "run_dir": str(args.run_dir),
        "rows": summary.where(pd.notna(summary), None).to_dict("records"),
        "outputs": {"ledger": "shadow_ledger.csv", "summary": "summary.csv", "report": "shadow_ledger_report.md"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a historical shadow ledger for G2 V3 User V2 + two_stop_cd3_skip.")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
