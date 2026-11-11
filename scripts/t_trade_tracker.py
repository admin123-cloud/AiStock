# -*- coding: utf-8 -*-
"""R37 L1 半自动做T：CLI 入口（周期判定 / 盘中节拍 / 对账 / 回放）。

用法：
  python scripts/t_trade_tracker.py --regime  [--date 2026-11-11]
  python scripts/t_trade_tracker.py --beat     [--now "2026-11-11 10:01:00"]
  python scripts/t_trade_tracker.py --fast-tick [--now "2026-11-11 10:01:23"]（R46 快车道）
  python scripts/t_trade_tracker.py --reconcile [--date 2026-11-11] [--fills-file path.json]
  python scripts/t_trade_tracker.py --replay --date 2026-11-10 [--cache cache.json]
  python scripts/t_trade_tracker.py --fast-replay --date 2026-11-10 [--cache ...]（R46）
  python scripts/t_trade_tracker.py --shadow-tick     [--now ...]（R40 影子单拍）
  python scripts/t_trade_tracker.py --shadow-finalize [--date 2026-11-11]（R40 影子日终）
  python scripts/t_trade_tracker.py --shadow-replay --date 2026-11-10 [--cache ...]（R40 影子回放）

零交易零 formal 写；产物全部在 runtime/t_tracker（回放写 replay/ 子目录，不动生产草案；
影子写 shadow/ 子目录，与 drafts/pending_push 物理隔离，绝不推送）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services import t_trade_shadow  # noqa: E402
from services import t_trade_signal as tts  # noqa: E402
from services.t_trade_signal import BEIJING_TZ, fmt_ts, parse_ts  # noqa: E402

# 回放节拍：与线上 beat cron 同节律（09:36~14:56，每 5min，错峰 +1min）
REPLAY_START = dt_time(9, 36)
REPLAY_END = dt_time(14, 56)
REPLAY_STEP_MIN = 5


def _parse_date(text: Optional[str]) -> date:
    if not text:
        return datetime.now(BEIJING_TZ).date()
    return date.fromisoformat(text)


def _parse_now(text: Optional[str]) -> datetime:
    if not text:
        return datetime.now(BEIJING_TZ).replace(tzinfo=None)
    ts = parse_ts(text)
    if ts is None:
        raise SystemExit(f"--now 无法解析: {text}")
    return ts


def cmd_regime(args: argparse.Namespace) -> int:
    for_date = _parse_date(args.date)
    now = datetime.now(BEIJING_TZ).replace(tzinfo=None)
    report = tts.run_regime(for_date=for_date, now=now, out_dir=args.out_dir)
    codes = {c: v["regime"] for c, v in (report.get("codes") or {}).items()}
    print(json.dumps({"status": report.get("status"), "date": report.get("date"),
                      "regimes": codes, "errors": report.get("errors", {})},
                     ensure_ascii=False))
    return 0 if report.get("status") in ("ok", "partial") else 1


def cmd_beat(args: argparse.Namespace) -> int:
    now = _parse_now(args.now)
    report = tts.run_beat(now=now, out_dir=args.out_dir)
    # R40 影子拍（observe_only）：同一进程随真节拍运行，但物理隔离 + 独立异常面
    # ——影子任何失败不影响真节拍结果与退出码，只如实打印 shadow 状态。
    shadow_summary: dict[str, Any] = {"status": "skipped"}
    if not args.no_shadow:
        try:
            shadow_summary = t_trade_shadow.shadow_beat(now=now, out_dir=args.out_dir)
        except Exception as exc:  # noqa: BLE001
            shadow_summary = {"status": f"error:{type(exc).__name__}", "detail": str(exc)}
    print(json.dumps({"status": report.get("status"), "ts": report.get("ts"),
                      "new_draft_ids": report.get("new_draft_ids", []),
                      "suppressed": {c: r.get("suppressed")
                                     for c, r in (report.get("codes") or {}).items()
                                     if isinstance(r, dict) and r.get("suppressed")},
                      "shadow": {k: shadow_summary.get(k)
                                 for k in ("status", "new_signal_ids", "signals_total")},
                      }, ensure_ascii=False))
    return 0 if report.get("status") == "ok" else 1


def cmd_fast_tick(args: argparse.Namespace) -> int:
    now = _parse_now(args.now)
    report = tts.run_fast_tick(now=now, out_dir=args.out_dir)
    # R46 影子快车道（observe_only）：独立异常面，失败不影响真快车道
    shadow_summary: dict[str, Any] = {"status": "skipped"}
    if not args.no_shadow:
        try:
            shadow_summary = t_trade_shadow.shadow_fast_tick(now=now, out_dir=args.out_dir)
        except Exception as exc:  # noqa: BLE001
            shadow_summary = {"status": f"error:{type(exc).__name__}", "detail": str(exc)}
    print(json.dumps({"status": report.get("status"), "ts": report.get("ts"),
                      "pool": report.get("pool"),
                      "new_draft_ids": report.get("new_draft_ids", []),
                      "suppressed": {c: r.get("suppressed")
                                     for c, r in (report.get("codes") or {}).items()
                                     if isinstance(r, dict) and r.get("suppressed")},
                      "shadow": {k: shadow_summary.get(k)
                                 for k in ("status", "new_signal_ids", "signals_total")},
                      }, ensure_ascii=False))
    return 0 if report.get("status") == "ok" else 1


def cmd_reconcile(args: argparse.Namespace) -> int:
    for_date = _parse_date(args.date)
    now = datetime.now(BEIJING_TZ).replace(tzinfo=None)
    loader: Optional[Callable[[date], tuple[list[dict], str]]] = None
    if args.fills_file:
        payload = json.loads(Path(args.fills_file).read_text(encoding="utf-8"))
        rows = payload.get("fills") if isinstance(payload, dict) else payload
        loader = lambda _d: (rows, f"file:{Path(args.fills_file).name}")  # noqa: E731
    report = tts.run_reconcile(for_date=for_date, now=now, out_dir=args.out_dir,
                               fills_loader=loader)
    print(json.dumps(report, ensure_ascii=False))
    return 0


def _replay_beat_times(day: date) -> list[datetime]:
    out, cur = [], datetime.combine(day, REPLAY_START)
    end = datetime.combine(day, REPLAY_END)
    while cur <= end:
        out.append(cur)
        cur += timedelta(minutes=REPLAY_STEP_MIN)
    return out


def _load_cache(cache_file: Path) -> dict:
    return json.loads(cache_file.read_text(encoding="utf-8"))


def _fetch_cache(pool_codes: list[str]) -> dict:
    cache: dict[str, Any] = {"fetched_at": fmt_ts(datetime.now(BEIJING_TZ).replace(tzinfo=None))}
    for code in pool_codes:
        cache[code] = {
            "bars5": tts.fetch_east_klines(code, 5),
            "bars15": tts.fetch_east_klines(code, 15),
        }
    return cache


def cmd_replay(args: argparse.Namespace) -> int:
    day = _parse_date(args.date)
    now = datetime.now(BEIJING_TZ).replace(tzinfo=None)
    pool = tts.load_broker_holdings()
    codes = [str(h["code"]).split(".")[0] for h in pool["holdings"]]

    cache_file = Path(args.cache) if args.cache else (
        tts.tracker_dir() / "replay" / f"cache_{day.strftime('%Y%m%d')}.json")
    if cache_file.is_file():
        cache = _load_cache(cache_file)
    else:
        cache = _fetch_cache(codes)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    out_dir = tts.tracker_dir() / "replay" / day.strftime("%Y%m%d")
    regime_loader = lambda d: tts._load_json(tts.regime_file_path(tts.tracker_dir(), d))  # noqa: E731
    bars5_loader = lambda code: (cache.get(code) or {}).get("bars5") or []  # noqa: E731
    bars15_loader = lambda code: (cache.get(code) or {}).get("bars15") or []  # noqa: E731
    snapshot_loader = lambda code: None  # 回放无快照表依赖，价=已收 5m 收价  # noqa: E731

    beats = []
    for beat_now in _replay_beat_times(day):
        report = tts.run_beat(
            now=beat_now, out_dir=out_dir, holdings_loader=lambda: pool,
            regime_loader=regime_loader, bars5_loader=bars5_loader,
            bars15_loader=bars15_loader, snapshot_loader=snapshot_loader,
            write_pending_push=False)
        beats.append({"ts": report.get("ts"), "status": report.get("status"),
                      "new_draft_ids": report.get("new_draft_ids"),
                      "suppressed": {c: r.get("suppressed")
                                     for c, r in (report.get("codes") or {}).items()
                                     if isinstance(r, dict) and r.get("suppressed")}})

    drafts = tts.read_jsonl(tts.drafts_path(out_dir, day))
    summary = {
        "schema": "t_tracker_replay/1", "date": day.isoformat(),
        "generated_at": fmt_ts(now), "beats": len(beats),
        "draft_count": len(drafts),
        "drafts": [{k: d.get(k) for k in ("id", "ts", "code", "side", "subtype",
                                          "px", "qty", "trigger", "regime")}
                   for d in drafts],
        "suppression_timeline": [b for b in beats if b["suppressed"] or b["new_draft_ids"]],
        "cache_file": str(cache_file),
    }
    out_file = tts.tracker_dir() / "replay" / f"{day.strftime('%Y%m%d')}_report.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"draft_count": len(drafts),
                      "draft_ids": [d["id"] for d in drafts],
                      "report": str(out_file)}, ensure_ascii=False))
    return 0


def _fast_replay_tick_times(day: date) -> list[datetime]:
    """R46 回放节律：09:31~14:59 每 1min（跳过午休无数据段）。"""
    out, cur = [], datetime.combine(day, dt_time(9, 31))
    end = datetime.combine(day, dt_time(14, 59))
    while cur <= end:
        t = cur.time()
        if not (dt_time(11, 30) < t < dt_time(13, 1)):
            out.append(cur)
        cur += timedelta(minutes=1)
    return out


def cmd_fast_replay(args: argparse.Namespace) -> int:
    """R46 快车道全日回放：1min fast ticks + 5min 慢 beats 交织 + 影子同步。

    实时价代理 = 已收最后一根 1m bar 收盘；形成中 5m bar 由区间内 1m 聚合
    （synthetic_fast_view）；写 replay/fast_<date>/，不动生产草案/影子台账。
    """
    day = _parse_date(args.date)
    now = datetime.now(BEIJING_TZ).replace(tzinfo=None)
    regime_doc = tts._load_json(tts.regime_file_path(tts.tracker_dir(), day)) or {}
    pool = tts.load_t_pool()
    codes = [c for c in (str(h["code"]).split(".")[0] for h in pool["holdings"])
             if c in (regime_doc.get("codes") or {})]

    cache_file = Path(args.cache) if args.cache else (
        tts.tracker_dir() / "replay" / f"fast_cache_{day.strftime('%Y%m%d')}.json")
    if cache_file.is_file():
        cache = _load_cache(cache_file)
    else:
        cache: dict[str, Any] = {"fetched_at": fmt_ts(now)}
        day_end = f"{day.isoformat()} 15:00"
        for code in codes:
            cache[code] = {"bars5": tts.fetch_east_klines(code, 5, lmt=480),
                           "bars1": [b for b in tts.fetch_east_trends_1m(code)
                                     if str(b["ts"]) <= day_end],
                           "bars15": tts.fetch_east_klines(code, 15)}
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    out_dir = tts.tracker_dir() / "replay" / f"fast_{day.strftime('%Y%m%d')}"
    regime_loader = lambda d: regime_doc if d == day else None  # noqa: E731
    bars15_loader = lambda code: (cache.get(code) or {}).get("bars15") or []  # noqa: E731

    # 事件流：同分钟 beat 先于 fast（生产 cron：beat 秒 0 / fast 秒 23 错峰）
    events: list[tuple[datetime, str]] = [(t, "beat") for t in _replay_beat_times(day)]
    events += [(t, "fast") for t in _fast_replay_tick_times(day)]
    events.sort(key=lambda e: (e[0], 0 if e[1] == "beat" else 1))

    timeline: list[dict] = []
    for ts, kind in events:
        try:
            if kind == "fast":
                views = {c: tts.synthetic_fast_view(cache[c]["bars5"], cache[c]["bars1"], ts)
                         for c in codes}
                b5 = lambda code, _v=views: _v.get(code, ([], None, None))[0]  # noqa: E731
                ql = lambda codes_, _v=views: {c: _v[c][2] for c in codes_  # noqa: E731
                                               if _v.get(c) and _v[c][2]}
                rep = tts.run_fast_tick(
                    now=ts, out_dir=out_dir, holdings_loader=lambda: pool,
                    regime_loader=regime_loader, bars5_loader=b5,
                    bars15_loader=bars15_loader, quotes_loader=ql,
                    snapshot_loader=lambda c: None, write_pending_push=False)
                shadow_rep = t_trade_shadow.shadow_fast_tick(
                    now=ts, out_dir=out_dir, holdings_loader=lambda: pool,
                    regime_loader=regime_loader, bars5_loader=b5, quotes_loader=ql)
            else:
                rep = tts.run_beat(
                    now=ts, out_dir=out_dir, holdings_loader=lambda: pool,
                    regime_loader=regime_loader,
                    bars5_loader=lambda code: (cache.get(code) or {}).get("bars5") or [],
                    bars15_loader=bars15_loader, snapshot_loader=lambda c: None,
                    write_pending_push=False)
                shadow_rep = t_trade_shadow.shadow_beat(
                    now=ts, out_dir=out_dir, holdings_loader=lambda: pool,
                    regime_loader=regime_loader,
                    bars5_loader=lambda code: (cache.get(code) or {}).get("bars5") or [])
            row = {"ts": fmt_ts(ts), "kind": kind,
                   "new_draft_ids": rep.get("new_draft_ids", []),
                   "fast_settled": rep.get("fast_settled", []),
                   "fast_merged": rep.get("fast_merged", []),
                   "shadow_new": (shadow_rep or {}).get("new_signal_ids", []),
                   "shadow_settled": (shadow_rep or {}).get("fast_settled", [])}
            if row["new_draft_ids"] or row["fast_settled"] or row["fast_merged"] \
                    or row["shadow_new"] or row["shadow_settled"]:
                timeline.append(row)
        except Exception as exc:  # noqa: BLE001 - 单事件失败如实记录继续
            timeline.append({"ts": fmt_ts(ts), "kind": kind, "error": f"{type(exc).__name__}: {exc}"})

    drafts = tts.read_jsonl(tts.drafts_path(out_dir, day))
    shadow_doc = tts._load_json(t_trade_shadow.shadow_day_path(out_dir, day)) or {}
    sells = [d for d in drafts if d.get("side") == tts.SIDE_SELL]
    summary = {
        "schema": "t_tracker_fast_replay/1", "date": day.isoformat(),
        "generated_at": fmt_ts(now), "codes": codes,
        "draft_count": len(drafts),
        "drafts": [{k: d.get(k) for k in ("id", "ts", "code", "side", "subtype", "lane",
                                          "bar_status", "px", "trigger", "confirm_source")}
                   for d in drafts],
        "first_sell_draft": ({k: sells[0].get(k) for k in ("id", "ts", "px", "lane",
                                                           "bar_status", "trigger")}
                             if sells else None),
        "shadow_signals": [{k: s.get(k) for k in ("id", "signal_ts", "signal_bar_ts", "code",
                                                  "lane", "bar_status", "features", "status",
                                                  "judged")}
                           for s in shadow_doc.get("signals", [])],
        "timeline": timeline,
        "cache_file": str(cache_file),
    }
    out_file = tts.tracker_dir() / "replay" / f"fast_{day.strftime('%Y%m%d')}_report.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"draft_count": len(drafts),
                      "first_sell_draft": summary["first_sell_draft"],
                      "shadow_fast_signals": [s for s in summary["shadow_signals"]
                                              if s.get("lane") == "fast"],
                      "report": str(out_file)}, ensure_ascii=False))
    return 0


def cmd_shadow_tick(args: argparse.Namespace) -> int:
    now = _parse_now(args.now)
    report = t_trade_shadow.shadow_beat(now=now, out_dir=args.out_dir)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("status") == "ok" else 1


def cmd_shadow_finalize(args: argparse.Namespace) -> int:
    for_date = _parse_date(args.date)
    now = datetime.now(BEIJING_TZ).replace(tzinfo=None)
    report = t_trade_shadow.finalize_shadow_day(for_date=for_date, now=now,
                                                out_dir=args.out_dir)
    print(json.dumps({k: report.get(k) for k in
                      ("date", "signals_total", "finalized", "judged_stats",
                       "rolling_4w")},
                     ensure_ascii=False))
    return 0 if report.get("beat_status") == "ok" else 1


def cmd_shadow_replay(args: argparse.Namespace) -> int:
    """影子回放：真节拍同律（09:36~14:56 每 5min）跑 shadow_beat + 15:05 日终固化。

    与 cmd_replay 的关键差异：写 shadow/（影子台账本体，供复盘页/滚动统计使用），
    绝不触碰 drafts/pending_push；行情缓存独立（需含 open 字段，480 根覆盖 RSI 收窗）。
    """
    day = _parse_date(args.date)
    now = datetime.now(BEIJING_TZ).replace(tzinfo=None)
    pool = tts.load_broker_holdings()
    codes = [str(h["code"]).split(".")[0] for h in pool["holdings"]]

    cache_file = Path(args.cache) if args.cache else (
        tts.tracker_dir() / "replay" / f"shadow_cache_{day.strftime('%Y%m%d')}.json")
    if cache_file.is_file():
        cache = _load_cache(cache_file)
    else:
        cache: dict[str, Any] = {"fetched_at": fmt_ts(now)}
        for code in codes:
            cache[code] = {"bars5": tts.fetch_east_klines(code, 5, lmt=480)}
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    regime_loader = lambda d: tts._load_json(tts.regime_file_path(tts.tracker_dir(), d))  # noqa: E731
    # 镜像线上 lmt=120：每拍只用已收 bar 的最后 120 根（RSI 收窗确定性）
    bars5_loader = lambda code: [b for b in ((cache.get(code) or {}).get("bars5") or [])
                                 if str(b["ts"]) <= "{} 15:00".format(day.isoformat())]  # noqa: E731

    beats = []
    for beat_now in _replay_beat_times(day):
        report = t_trade_shadow.shadow_beat(
            now=beat_now, out_dir=args.out_dir, holdings_loader=lambda: pool,
            regime_loader=regime_loader, bars5_loader=bars5_loader)
        beats.append({"ts": report.get("ts"), "status": report.get("status"),
                      "new_signal_ids": report.get("new_signal_ids", [])})

    summary = t_trade_shadow.finalize_shadow_day(
        for_date=day, now=now, out_dir=args.out_dir,
        holdings_loader=lambda: pool, regime_loader=regime_loader, bars5_loader=bars5_loader)
    out = {
        "schema": "t_tracker_shadow_replay/1", "date": day.isoformat(),
        "generated_at": fmt_ts(now), "beats": len(beats),
        "signal_count": summary.get("signals_total"),
        "judged_stats": summary.get("judged_stats"),
        "signals": [{k: s.get(k) for k in ("id", "signal_ts", "signal_bar_ts", "code",
                                           "features", "virtual_fill", "outcomes",
                                           "judged", "status")}
                    for s in (tts._load_json(t_trade_shadow.shadow_day_path(
                        tts.tracker_dir(args.out_dir), day)) or {}).get("signals", [])],
        "timeline": [b for b in beats if b.get("new_signal_ids")],
        "cache_file": str(cache_file),
    }
    out_file = tts.tracker_dir(args.out_dir) / "replay" / f"shadow_{day.strftime('%Y%m%d')}_report.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"signal_count": out["signal_count"],
                      "judged_stats": out["judged_stats"],
                      "report": str(out_file)}, ensure_ascii=False))
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="R37 L1 半自动做T引擎 CLI（R40 含影子做T）")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--regime", action="store_true", help="09:25 周期判定")
    mode.add_argument("--beat", action="store_true", help="盘中信号节拍（单次，含影子拍）")
    mode.add_argument("--fast-tick", action="store_true",
                      help="R46 快车道 1 分钟拍（实时价 intrabar 重估，含影子快车道）")
    mode.add_argument("--reconcile", action="store_true", help="15:10 当日成交对账")
    mode.add_argument("--replay", action="store_true", help="回放验证（写 replay/ 目录）")
    mode.add_argument("--fast-replay", action="store_true",
                      help="R46 快车道全日回放（1min fast + 5min beat 交织 + 影子）")
    mode.add_argument("--shadow-tick", action="store_true", help="R40 影子单拍（写 shadow/）")
    mode.add_argument("--shadow-finalize", action="store_true", help="R40 影子日终固化+滚动统计")
    mode.add_argument("--shadow-replay", action="store_true", help="R40 影子全日回放（写 shadow/）")
    parser.add_argument("--date", help="YYYY-MM-DD（默认今天）")
    parser.add_argument("--now", help="beat 用北京时间 YYYY-MM-DD HH:MM:SS（默认当前）")
    parser.add_argument("--fills-file", help="对账成交文件（{fills:[...]}）")
    parser.add_argument("--cache", help="回放行情缓存文件路径")
    parser.add_argument("--out-dir", help="覆盖 t_tracker 根目录（测试用）")
    parser.add_argument("--no-shadow", action="store_true",
                        help="beat 时跳过影子拍（真节拍单独调试用）")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.regime:
        return cmd_regime(args)
    if args.beat:
        return cmd_beat(args)
    if args.fast_tick:
        return cmd_fast_tick(args)
    if args.reconcile:
        return cmd_reconcile(args)
    if args.replay:
        return cmd_replay(args)
    if args.fast_replay:
        return cmd_fast_replay(args)
    if args.shadow_tick:
        return cmd_shadow_tick(args)
    if args.shadow_finalize:
        return cmd_shadow_finalize(args)
    return cmd_shadow_replay(args)


if __name__ == "__main__":
    raise SystemExit(main())
