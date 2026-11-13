# -*- coding: utf-8 -*-
"""做T信号质量日报（2026-11-13 Owner 要求，做T运行层 B 方案）。

每个交易日 15:20 由 pipeline_scheduler（job=signal_quality_report）调起：
  - 读当日 runtime/t_tracker/drafts/YYYYMMDD.jsonl（confirmed/expired 全量草案，
    含 fast 草案的收线结算终态）；
  - 收盘价主源=kline_daily 当日 argMax(close, created_at)（15:20 当日日线常未
    落库→回退东财实时价，15:00 后即终价；之后任何时点重跑自动以 kline_daily
    覆盖，close_source 如实标注）；
  - 产物 runtime/signal_quality/YYYYMMDD.json + .md（原子写、幂等重跑覆盖）。

口径：
  - triggered=该 trigger_kind 草案总数；confirmed=bar_status=confirmed_bar
    （慢车道无 bar_status 的最终草案视为已确认发出）；expired=expired_unconfirmed
    （瓦解）；open=fast 未结算（不计入确认/瓦解分母，单列）；
  - confirm_rate=confirmed/settled、breakdown_rate=expired/settled
    （settled=confirmed+expired；settled=0 → None）；
  - bps=方向调整后触发价→收盘价：(close-px)/px*1e4（买）/（px-close)/px*1e4
    （卖/reduce），按 kind 均值；px/close 缺失该样本不计入。

零交易零 formal 表写；纯读 drafts + kline_daily（raw 基表直查）。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.t_trade_signal import BEIJING_TZ  # noqa: E402

__version__ = "1.0.0"

SCHEMA_REPORT = "signal_quality_report/1"
REPORT_DIR_NAME = "signal_quality"

logger = logging.getLogger("signal_quality_report")

STATUS_CONFIRMED = "confirmed"
STATUS_EXPIRED = "expired"
STATUS_OPEN = "open"


# ── 纯口径函数 ──────────────────────────────────────────────

def classify_status(draft: Mapping[str, Any]) -> str:
    """草案终态：confirmed / expired（瓦解）/ open（fast 未结算）。

    慢车道最终草案（无 bar_status）发出即确认 → confirmed。
    """
    status = draft.get("bar_status")
    if status == "expired_unconfirmed":
        return STATUS_EXPIRED
    if status == "open":
        return STATUS_OPEN
    if status == "confirmed_bar" or draft.get("confirmed_at"):
        return STATUS_CONFIRMED
    return STATUS_CONFIRMED if draft.get("lane") != "fast" else STATUS_OPEN


def direction_bps(side: Any, px: Any, close: Any) -> Optional[float]:
    """方向调整后 触发价→收盘价 盈亏（bps）；方向/价格缺失 → None。"""
    side_text = str(side or "").strip().lower()
    if not side_text or px in (None, "") or close in (None, ""):
        return None
    try:
        px_f, close_f = float(px), float(close)
    except (TypeError, ValueError):
        return None
    if px_f <= 0:
        return None
    if side_text.startswith("buy"):
        return (close_f - px_f) / px_f * 1e4
    if side_text.startswith("sell") or side_text.startswith("reduce"):
        return (px_f - close_f) / px_f * 1e4
    return None


def _rate(num: int, den: int) -> Optional[float]:
    return round(num / den, 4) if den else None


def _avg(values: Sequence[float]) -> Optional[float]:
    return round(sum(values) / len(values), 1) if values else None


def build_report(
    *,
    day: date,
    drafts: Sequence[Mapping[str, Any]],
    closes: Mapping[str, Any],
    close_source: Optional[str],
    generated_at: str,
) -> dict:
    """纯聚合：drafts × closes → 按 trigger_kind/整体统计。零 IO。"""
    by_kind: dict[str, dict[str, Any]] = {}
    rows: list[dict] = []
    bps_all: list[float] = []

    for draft in drafts:
        code = str(draft.get("code") or "").split(".")[0]
        side = draft.get("side")
        px = draft.get("px")
        kind = str((draft.get("trigger") or {}).get("kind") or "unknown")
        status = classify_status(draft)
        close = closes.get(code)
        bps = direction_bps(side, px, close)
        bucket = by_kind.setdefault(kind, {
            "triggered": 0, "confirmed": 0, "expired": 0, "open": 0,
            "settled": 0, "confirm_rate": None, "breakdown_rate": None,
            "avg_bps": None, "bps_samples": 0, "_bps": []})
        bucket["triggered"] += 1
        bucket[status] += 1
        if bps is not None:
            bucket["_bps"].append(bps)
            bps_all.append(bps)
        rows.append({
            "id": draft.get("id"), "code": code, "name": draft.get("name"),
            "side": side, "trigger_kind": kind, "lane": draft.get("lane"),
            "px": px, "close": close, "status": status,
            "expire_reason": draft.get("expire_reason"), "bps": bps,
        })

    for bucket in by_kind.values():
        bucket["settled"] = bucket["confirmed"] + bucket["expired"]
        bucket["confirm_rate"] = _rate(bucket["confirmed"], bucket["settled"])
        bucket["breakdown_rate"] = _rate(bucket["expired"], bucket["settled"])
        bucket["bps_samples"] = len(bucket["_bps"])
        bucket["avg_bps"] = _avg(bucket["_bps"])
        del bucket["_bps"]

    totals = {
        "triggered": len(drafts),
        "confirmed": sum(b["confirmed"] for b in by_kind.values()),
        "expired": sum(b["expired"] for b in by_kind.values()),
        "open": sum(b["open"] for b in by_kind.values()),
        "confirm_rate": None, "breakdown_rate": None, "avg_bps": _avg(bps_all),
    }
    totals["settled"] = totals["confirmed"] + totals["expired"]
    totals["confirm_rate"] = _rate(totals["confirmed"], totals["settled"])
    totals["breakdown_rate"] = _rate(totals["expired"], totals["settled"])

    by_kind_sorted = {k: by_kind[k] for k in sorted(by_kind)}
    return {
        "schema": SCHEMA_REPORT, "version": __version__, "date": day.isoformat(),
        "generated_at": generated_at, "close_source": close_source,
        "closes": dict(sorted(closes.items())),
        "totals": totals, "by_trigger_kind": by_kind_sorted, "drafts": rows,
        "notes": [
            "confirmed=bar_status confirmed_bar（慢车道最终草案视为已确认）；"
            "expired=expired_unconfirmed（瓦解）；open=fast 未结算（不计入率分母）",
            "bps=方向调整后触发价→收盘价（买 (close-px)/px，卖 (px-close)/px，×1e4）",
            "收盘价主源 kline_daily 当日；15:20 当日日线未落库时回退东财实时价"
            "（15:00 后即终价），重跑自动以 kline_daily 覆盖",
        ],
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    day = report.get("date")
    totals = report.get("totals") or {}
    closes = report.get("closes") or {}
    close_line = ", ".join(f"{c}={v}" for c, v in closes.items()) or "-"
    lines = [
        f"# 做T信号质量日报 {day}",
        "",
        f"- 生成时间：{report.get('generated_at')}　收盘价源：`{report.get('close_source')}`",
        f"- 收盘价：{close_line}",
        "",
        "## 按 trigger_kind",
        "",
        "| trigger_kind | 触发 | 确认 | 瓦解 | 未结算 | 确认率 | 瓦解率 | 触发→收盘 bps |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    fmt_rate = lambda v: "-" if v is None else f"{v * 100:.1f}%"
    fmt_bps = lambda v: "-" if v is None else f"{v:+.1f}"
    for kind, b in (report.get("by_trigger_kind") or {}).items():
        lines.append(
            f"| {kind} | {b.get('triggered', 0)} | {b.get('confirmed', 0)} | "
            f"{b.get('expired', 0)} | {b.get('open', 0)} | "
            f"{fmt_rate(b.get('confirm_rate'))} | {fmt_rate(b.get('breakdown_rate'))} | "
            f"{fmt_bps(b.get('avg_bps'))} |")
    lines += [
        "",
        "## 合计",
        "",
        f"- 触发 {totals.get('triggered', 0)} · 确认 {totals.get('confirmed', 0)} · "
        f"瓦解 {totals.get('expired', 0)} · 未结算 {totals.get('open', 0)}"
        f"（settled={totals.get('settled', 0)}）",
        f"- 确认率 {fmt_rate(totals.get('confirm_rate'))} · "
        f"瓦解率 {fmt_rate(totals.get('breakdown_rate'))} · "
        f"平均 {fmt_bps(totals.get('avg_bps'))} bps",
        "",
        "## 草案明细",
        "",
        "| id | code | side | trigger_kind | 触发价 | 收盘价 | 终态 | bps |",
        "|---|---|---|---|---:|---:|---|---:|",
    ]
    for row in report.get("drafts") or []:
        lines.append(
            f"| {row.get('id')} | {row.get('code')} | {row.get('side')} | "
            f"{row.get('trigger_kind')} | {row.get('px')} | {row.get('close')} | "
            f"{row.get('status')} | {fmt_bps(row.get('bps'))} |")
    lines += ["", "## 口径", ""]
    lines += [f"- {n}" for n in report.get("notes") or []]
    return "\n".join(lines) + "\n"


# ── 数据面（可注入）─────────────────────────────────────────

def load_drafts(root: Path, day: date) -> list[dict]:
    path = Path(root) / "drafts" / f"{day.strftime('%Y%m%d')}.jsonl"
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _kline_codes(bare: str) -> list[str]:
    """kline_daily 用后缀码（002015.SZ）；造裸码+常见后缀候选。"""
    if bare.startswith(("6", "9", "5")):
        return [bare, f"{bare}.SH"]
    if bare.startswith(("4", "8")):
        return [bare, f"{bare}.BJ"]
    return [bare, f"{bare}.SZ"]


def load_closes_kline(codes: Sequence[str], day: date) -> dict:
    """kline_daily 当日收盘（argMax by created_at）；CH 不可用/无行 → {}。"""
    if not codes:
        return {}
    candidates: list[str] = []
    for code in codes:
        candidates.extend(_kline_codes(str(code).split(".")[0]))
    quoted = ",".join(f"'{c}'" for c in candidates)
    sql = (
        "SELECT code, argMax(close, created_at) AS close "
        f"FROM stock.kline_daily WHERE trade_date = '{day.isoformat()}' "
        f"AND code IN ({quoted}) GROUP BY code")
    try:
        from utils.market_warehouse import clickhouse_available, clickhouse_query_df
        if not clickhouse_available():
            return {}
        df = clickhouse_query_df(sql, raw=True)
    except Exception as exc:  # noqa: BLE001 - CH 故障走实时价回退
        logger.warning("[signal_quality] kline_daily 查询失败：%s", exc)
        return {}
    if df is None or df.empty:
        return {}
    out: dict[str, float] = {}
    for rec in df.to_dict("records"):
        bare = str(rec.get("code", "")).split(".")[0]
        try:
            close = float(rec.get("close"))
        except (TypeError, ValueError):
            continue
        if close > 0:
            out[bare] = close
    return out


def load_closes_realtime(codes: Sequence[str]) -> dict:
    """东财实时价（15:00 后即当日终价）；失败 → {}。watch_drafts 同款。"""
    if not codes:
        return {}
    secids = ",".join(
        ("1." if str(c).startswith(("6", "9", "5")) else "0.") + str(c)
        for c in codes)
    url = ("http://push2.eastmoney.com/api/qt/ulist.np/get?secids=" + secids +
           "&fields=f2,f12&fltt=2")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out: dict[str, float] = {}
        for item in (data.get("data") or {}).get("diff") or []:
            try:
                price = float(item.get("f2"))
            except (TypeError, ValueError):
                continue
            bare = str(item.get("f12", "")).split(".")[0]
            if bare and price > 0:
                out[bare] = price
        return out
    except Exception as exc:  # noqa: BLE001 - 回退源失败由上层标注
        logger.warning("[signal_quality] 实时价兜底失败：%s", exc)
        return {}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def run(
    *,
    day: Optional[date] = None,
    out_dir: Optional[Path] = None,
    tracker_root: Optional[Path] = None,
    drafts_loader: Optional[Callable[[Path, date], list]] = None,
    closes_loader: Optional[Callable[[Sequence[str], date], dict]] = None,
    now: Optional[datetime] = None,
    log: Optional[logging.Logger] = None,
) -> dict:
    """生成某交易日报告并落盘（幂等覆盖）。返回报告 dict。"""
    log = log or logger
    now = now or datetime.now(BEIJING_TZ).replace(tzinfo=None)
    day = day or now.date()
    if out_dir is None:
        from utils.paths import runtime_path
        out_dir = runtime_path(REPORT_DIR_NAME)
    if tracker_root is None:
        from services.t_trade_signal import tracker_dir
        tracker_root = tracker_dir(None)

    load_drafts_ = drafts_loader or load_drafts
    drafts = load_drafts_(tracker_root, day)
    codes = sorted({str(d.get("code") or "").split(".")[0] for d in drafts
                    if d.get("code")})

    close_source = None
    closes: dict = {}
    if closes_loader is not None:
        closes = closes_loader(codes, day) or {}
        close_source = "injected"
    else:
        if codes:
            closes = load_closes_kline(codes, day)
            if closes:
                close_source = "kline_daily"
            else:
                closes = load_closes_realtime(codes)
                if closes:
                    close_source = "eastmoney_realtime"
    missing = [c for c in codes if c not in closes]
    if missing:
        log.warning("[signal_quality] %s 收盘价缺失（bps 不计入）：%s", day, missing)

    report = build_report(
        day=day, drafts=drafts, closes=closes, close_source=close_source,
        generated_at=now.isoformat(timespec="seconds"))
    stamp = day.strftime("%Y%m%d")
    _atomic_write(Path(out_dir) / f"{stamp}.json",
                  json.dumps(report, ensure_ascii=False, indent=1, default=str) + "\n")
    _atomic_write(Path(out_dir) / f"{stamp}.md", render_markdown(report))
    log.info("[signal_quality] %s 报告落盘：%s（drafts=%d close_source=%s）",
             day, out_dir, len(drafts), close_source)
    return report


def _parse_date(text: Optional[str]) -> Optional[date]:
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise SystemExit(f"--date 无法解析: {text}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="做T信号质量日报：drafts×kline_daily 收盘 → runtime/signal_quality")
    parser.add_argument("--today", action="store_true",
                        help="报告今日（缺省行为；供调度器显式传参）")
    parser.add_argument("--date", help="YYYY-MM-DD（缺省=北京时间今日）")
    parser.add_argument("--out-dir", help="输出目录（缺省 runtime/signal_quality）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    try:
        report = run(
            day=_parse_date(args.date),
            out_dir=Path(args.out_dir) if args.out_dir else None)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 单次运行失败 rc=1 供调度器记 failure
        logger.error("[signal_quality] 日报失败：%s", exc, exc_info=True)
        return 1
    print(json.dumps({
        "status": "ok", "date": report.get("date"),
        "close_source": report.get("close_source"),
        "totals": report.get("totals")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
