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

from api.gen2_factor import _add_factor, _estimate_prewarm_days, _load_daily_ohlcv, build_gen2_factor_registry  # noqa: E402
from scripts.gen2_backtest_open_v1_portfolio import _json_default  # noqa: E402


def _factor_meta(factor_ids: list[str]) -> list[dict[str, Any]]:
    lookup = {str(item.get("id")): item for item in build_gen2_factor_registry().get("factors") or []}
    metas = []
    for fid in factor_ids:
        meta = lookup.get(fid)
        if not meta:
            raise RuntimeError(f"Unknown Alpha191 factor: {fid}")
        if meta.get("backend_status") != "implemented":
            raise RuntimeError(f"Alpha191 factor is not implemented: {fid}")
        metas.append(meta)
    return metas


def run(args: argparse.Namespace) -> dict[str, Any]:
    source = pd.read_parquet(args.source)
    source["date"] = pd.to_datetime(source["entry_date"], errors="coerce")
    source["code6"] = source["code"].astype(str).str[:6]
    factor_ids = [item.strip() for item in str(args.factors).split(",") if item.strip()]
    metas = _factor_meta(factor_ids)
    prewarm = max(_estimate_prewarm_days(meta) for meta in metas)
    start = pd.to_datetime(source["date"].min()).strftime("%Y-%m-%d")
    end = pd.to_datetime(source["date"].max()).strftime("%Y-%m-%d")
    daily = _load_daily_ohlcv(start, end, horizon=1, prewarm_days=prewarm)
    if daily.empty:
        raise RuntimeError("Daily OHLCV data is empty.")
    daily_dates = sorted(pd.to_datetime(daily["date"].dropna().unique()))
    prev_date: dict[pd.Timestamp, pd.Timestamp] = {}
    for dt in sorted(source["date"].dropna().unique()):
        ts = pd.Timestamp(dt)
        candidates = [item for item in daily_dates if item < ts]
        if candidates:
            prev_date[ts] = candidates[-1]
    needed = source[["date", "code6"]].drop_duplicates().copy()
    needed["factor_date"] = needed["date"].map(lambda x: prev_date.get(pd.Timestamp(x)))
    needed = needed.dropna(subset=["factor_date"]).copy()

    out = needed.copy()
    for meta in metas:
        fid = str(meta.get("id"))
        print(f"computing lagged {fid}", flush=True)
        factored = _add_factor(daily, meta)
        slim = factored[["date", "code", "factor_value"]].rename(
            columns={"date": "factor_date", "code": "code6", "factor_value": fid}
        )
        out = out.merge(slim, on=["factor_date", "code6"], how="left")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(output, index=False)
    out.to_csv(output.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    payload = {
        "schema_version": 1,
        "source": str(args.source),
        "output": str(output),
        "factors": factor_ids,
        "rows": int(len(out)),
        "entry_dates": int(out["date"].nunique()),
        "factor_dates": int(out["factor_date"].nunique()),
    }
    output.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Build T-1 Alpha191 values for G2 signal dates.")
    parser.add_argument("--source", default=str(ROOT / "reports" / "gen2_alpha191_overlay_candidate_train_dirs" / "sources" / "risk_cool_base.parquet"))
    parser.add_argument("--output", default=str(ROOT / "reports" / "gen2_alpha191_lag_values" / "alpha191_t1_signal_values.parquet"))
    parser.add_argument("--factors", default="Alpha150,Alpha095,Alpha144")
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
