"""Verify real Big QMT market responses before activating its transport."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from data_fetcher.sources.qmt_builtin import QmtBuiltinMarketApi, _write_json
from utils.paths import runtime_path

TZ = ZoneInfo("Asia/Shanghai")
FIELDS = ["open", "high", "low", "close", "volume", "amount"]
CODES = ["600000.SH", "000001.SZ", "000001.SH", "399001.SZ"]
INDEX_CODES = {"000001.SH", "399001.SZ"}


def verify(api, output: Path) -> dict:
    now = datetime.now(TZ)
    result = {"source": "qmt_builtin_contextinfo", "tested_at": now.isoformat(),
              "market_only": True, "database_written": False, "checks": {}, "errors": []}
    output.mkdir(parents=True, exist_ok=True)
    def check(name, callback):
        try:
            detail = callback()
            result["checks"][name] = {"ok": True, "detail": detail}
            return detail
        except Exception as exc:
            result["checks"][name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            result["errors"].append(name)
            return None
    connection = check("connection", api.connect)
    if connection is None:
        result["verification_passed"] = False
        return result
    result["connection"] = connection
    yesterday = (now.date() - timedelta(days=1)).strftime("%Y%m%d")
    start = (now.date() - timedelta(days=14)).strftime("%Y%m%d")
    calendar = check("calendar", lambda: api.get_trading_dates("SH", start, yesterday, 10))
    dates = sorted({str(item)[:8] for item in calendar or []})
    if not dates or any(len(day) != 8 or not day.isdigit() for day in dates):
        result["errors"].append("calendar_empty_or_invalid")
        result["verification_passed"] = False
        return result
    expected = dates[-1]
    result["expected_last_completed_trade_date"] = expected
    def ticks():
        data = api.get_full_tick(CODES)
        if any(float((data.get(code) or {}).get("lastPrice", 0)) <= 0 for code in CODES):
            raise ValueError("empty/invalid full_tick prices")
        for code in CODES:
            tick = data[code]
            if "time" in tick:
                timestamp = datetime.fromtimestamp(float(tick["time"]) / 1000, TZ)
                if timestamp.strftime("%Y%m%d") < expected or timestamp.date() > now.date():
                    raise ValueError("stale/future tick time: " + code)
            else:
                raise ValueError("native tick lacks epoch time: " + code)
        _write_json(output / "ticks.json", data)
        return {code: {key: data[code].get(key) for key in ["time", "lastPrice", "lastClose"]} for code in CODES}
    check("full_tick", ticks)
    def universe():
        codes = api.get_stock_list_in_sector("沪深京A股")
        if len(codes) < 5000 or len(codes) != len(set(codes)):
            raise ValueError("empty/incomplete/duplicate native A-share universe")
        quotes = api.get_full_tick(codes)
        priced = sum(float((quotes.get(code) or {}).get("lastPrice", 0)) > 0 for code in codes)
        if priced / len(codes) < 0.99:
            raise ValueError("full-market snapshot has less than 99% priced coverage")
        _write_json(output / "universe.json", {"codes": codes})
        return {"codes": len(codes), "priced_ticks": priced, "coverage": priced / len(codes)}
    check("full_market_snapshot", universe)
    def instrument_details():
        details = {code: api.get_instrument_detail(code) for code in CODES[:2]}
        if any(not detail.get("InstrumentName") for detail in details.values()):
            raise ValueError("native stock detail missing name")
        _write_json(output / "sample_details.json", details)
        return {code: detail.get("InstrumentName") for code, detail in details.items()}
    check("instrument_details", instrument_details)
    frames = {}
    for period in ["1d", "5m"]:
        for code in CODES:
            def bars(code=code, period=period):
                api.download_history_data(code, period, start, yesterday)
                frame = api.get_market_data_ex(FIELDS, [code], period=period,
                                               start_time=start, end_time=yesterday)[code]
                if frame.empty:
                    raise ValueError("native history is empty")
                numeric = frame[FIELDS].apply(pd.to_numeric, errors="coerce")
                if numeric.isna().any().any() or (numeric[["open", "high", "low", "close"]] <= 0).any().any():
                    raise ValueError("invalid/null prices")
                if (numeric[["volume", "amount"]] < 0).any().any():
                    raise ValueError("negative volume/amount")
                if ((numeric.high < numeric[["open", "close", "low"]].max(axis=1)) |
                        (numeric.low > numeric[["open", "close", "high"]].min(axis=1))).any():
                    raise ValueError("invalid OHLC envelope")
                if max(str(index)[:8] for index in frame.index) != expected:
                    raise ValueError("history not at latest completed trade date")
                frames[(code, period)] = frame
                filename = f"{code}_{period}.csv"
                frame.to_csv(output / filename, index_label="qmt_time_raw", encoding="utf-8")
                return {"rows": len(frame), "first": str(frame.index[0]), "last": str(frame.index[-1]),
                        "csv": filename, "sha256": hashlib.sha256((output / filename).read_bytes()).hexdigest()}
            check(f"{code}_{period}", bars)
    for code in CODES:
        def units_and_slots(code=code):
            daily, minute = frames[(code, "1d")], frames[(code, "5m")]
            d = daily[[str(i)[:8] == expected for i in daily.index]].iloc[-1]
            m = minute[[str(i)[:8] == expected for i in minute.index]]
            from scripts.qmt_xtquant_minute_backfill_validate import REGULAR_ASHARE_5M_TIMES
            actual = {datetime.strptime(str(i)[:14], "%Y%m%d%H%M%S").time() for i in m.index}
            if len(m) != 48 or actual != set(REGULAR_ASHARE_5M_TIMES):
                raise ValueError("latest trading day does not contain exactly 48 regular 5m bars")
            if float(m.low.min()) < float(d.low) - 0.011 or float(m.high.max()) > float(d.high) + 0.011:
                raise ValueError("5m prices outside unadjusted daily range")
            # Existing stock daily contract is lots/yuan. No inferred scaling is applied.
            vwap = float(d.amount) / (float(d.volume) * 100) if float(d.volume) > 0 else 0
            if code not in INDEX_CODES and not float(d.low) - 0.011 <= vwap <= float(d.high) + 0.011:
                raise ValueError("native stock daily volume/amount do not fit existing lots/yuan contract")
            if code in INDEX_CODES:
                tick = json.loads((output / "ticks.json").read_text(encoding="utf-8"))[code]
                if not float(tick["volume"]) == float(d.volume):
                    raise ValueError("native index daily volume differs from last completed tick lots")
                if not float(tick["pvolume"]) == float(tick["volume"]) * 100:
                    raise ValueError("native SH/SZ index tick share/lot fields disagree")
                from utils.kline_units import normalize_qmt_daily_units
                normalized = normalize_qmt_daily_units(daily, instrument_type="index")
                if not normalized.volume.equals(daily.volume):
                    raise ValueError("native index volume unexpectedly rescaled")
            if float(m.volume.sum()) > float(d.volume) * 1.005 or float(m.amount.sum()) > float(d.amount) * 1.005:
                raise ValueError("native minute quantity/amount exceeds daily contract; unit review required")
            return {"bars": len(m), "minute_to_daily_volume": float(m.volume.sum()) / float(d.volume),
                    "minute_to_daily_amount": float(m.amount.sum()) / float(d.amount), "daily_vwap": vwap}
        check(f"{code}_compatibility", units_and_slots)
    def subscription():
        batches = []
        identifier = api.subscribe_whole_quote(CODES, batches.append)
        try:
            time.sleep(2)
            api.check_stream_health()
            return {"accepted": True, "callback_batches": len(batches),
                    "live_callback_verified": bool(batches)}
        finally:
            api.unsubscribe_quote(identifier)
    check("subscription", subscription)
    result["unconfirmed_reference_capabilities"] = [name for name in
        ["get_sector_list", "download_sector_data", "download_history_contracts"]
        if name not in connection.get("capabilities", [])]
    result["verification_passed"] = not result["errors"]
    result["activation_scope"] = "stock_index_history_full_market_ticks_calendar_subscription_api"
    result["limitations"] = ["No formal database writes; sampled history does not prove full history coverage",
                             "Sector directory and expired-contract refresh remain explicitly unsupported",
                             "No callback batches on a closed market does not verify live delivery"]
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--activate", action="store_true")
    args = parser.parse_args()
    output = runtime_path("qmt_builtin_market", "verification", datetime.now(TZ).strftime("%Y%m%d_%H%M%S"))
    report = verify(QmtBuiltinMarketApi(), output)
    report["activation_requested"] = args.activate
    activated = False
    if args.activate and report["verification_passed"]:
        _write_json(runtime_path("qmt_builtin_market", "active_transport.json"), {
            "active_transport": "builtin", "verification_passed": True,
            "activation_scope": report["activation_scope"], "report": str(output / "report.json"),
            "activated_at": datetime.now(TZ).isoformat(),
            "unconfirmed_reference_capabilities": report["unconfirmed_reference_capabilities"]})
        activated = True
    report["activated"] = activated
    _write_json(output / "report.json", report)
    print(json.dumps({"verification_passed": report["verification_passed"], "errors": report["errors"],
                      "report": str(output / "report.json"), "activated": activated}, ensure_ascii=False))
    return 0 if report["verification_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
