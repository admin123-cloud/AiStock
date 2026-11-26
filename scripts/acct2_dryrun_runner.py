# -*- coding: utf-8 -*-
"""acct2 1 周试运行（Dry-run）独立执行入口。

用法示例：
  python scripts/acct2_dryrun_runner.py init
  python scripts/acct2_dryrun_runner.py premarket
  python scripts/acct2_dryrun_runner.py intraday
  python scripts/acct2_dryrun_runner.py eod

所有读写严格隔离在 F:\Aistock\runtime\acct2_managed\ 下，零真实下单、零写 formal 库。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

# 确保以脚本方式运行时可导入 services.*
CODE_ROOT = Path(__file__).resolve().parents[1]
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from services.trading.acct2_managed_engine import (
    Acct2ManagedConfig,
    Acct2State,
    Position,
    append_jsonl,
    apply_eod_settlement,
    calc_etf_metrics,
    check_order_gate,
    evaluate_risk_tier,
    execute_paper_order,
    generate_strategy_intents,
    infer_asset_type,
    mark_to_market,
    normalize_code,
    save_json,
    target_position_cap,
)
from services.trading.acct2_market_feed import (
    fetch_daily_bars_tencent,
    fetch_realtime_quotes_tencent,
    load_market_breadth,
)

from utils.common.paths import runtime_path

DEFAULT_RUNTIME_DIR = runtime_path("acct2_managed")
DEFAULT_SNAPSHOT_PATH = runtime_path("ths_read_account") / "latest_acct2.json"
DEFAULT_T_TRACKER_DIR = runtime_path("t_tracker")
DEFAULT_WEIXIN_BRIDGE_URL = os.getenv("AISTOCK_WEIXIN_BRIDGE_URL", "http://127.0.0.1:8789")


def send_wechat_ilink(title: str, text: str, bridge_url: str = DEFAULT_WEIXIN_BRIDGE_URL, timeout: float = 6.0) -> dict[str, Any]:
    """通过本机微信 ClawBot (ilink_bridge) 推送单向交易通知，异常静默降级不阻塞主流程。"""
    base = (bridge_url or DEFAULT_WEIXIN_BRIDGE_URL).rstrip("/")
    try:
        payload = json.dumps(
            {"title": title, "text": text, "source": "acct2_managed"},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/send",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            res = json.loads(resp.read().decode("utf-8", errors="replace"))
        return {"ok": bool(res.get("ok")), "detail": str(res.get("detail") or res)}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "detail": f"HTTP {exc.code}: {body}"}
    except Exception as exc:
        return {"ok": False, "detail": f"notify_error: {exc}"}


def notify_paper_fill_wechat(fill: dict[str, Any], state: Acct2State, bridge_url: str = DEFAULT_WEIXIN_BRIDGE_URL) -> dict[str, Any]:
    """格式化并推送一笔 acct2 托管模拟成交到微信，便于用户手动同步执行。"""
    side_cn = "买入" if fill["side"] == "buy" else "卖出"
    icon = "🟢" if fill["side"] == "buy" else "🔴"
    title = f"【{icon} acct2托管·{side_cn}】{fill['name']}({fill['code']}) {fill['qty']}股 @ {fill['quote_price']}"
    lines = [
        f"- 账号：`acct2`（独立托管，区别于旧做T信号）",
        f"- 操作：**{side_cn} {fill['qty']} 股** `{fill['code']}` {fill['name']}",
        f"- 参考价：盘口 `{fill['quote_price']}`（模拟成交 `{fill['fill_price']}`，金额 `{fill['amount']:.2f}` 元）",
        f"- 时间：`{fill['ts']}`",
        f"- 理由：{fill['reason']}",
        f"- 执行后账户：现金 `{state.cash:.2f}` 元｜总资产 `{state.total_assets:.2f}` 元｜回撤 `{-state.drawdown_from_hwm*100:.2f}%`",
        f"- 提示：请在同花顺 `acct2` 核对盘口后手动同步执行。",
    ]
    return send_wechat_ilink(title, "\n".join(lines), bridge_url=bridge_url)


def _load_config(runtime_dir: Path) -> Acct2ManagedConfig:
    cfg_path = runtime_dir / "config.json"
    if not cfg_path.exists():
        return Acct2ManagedConfig()
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg = Acct2ManagedConfig()
    for k, v in raw.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
    return cfg


def _load_state(runtime_dir: Path) -> Acct2State:
    state_path = runtime_dir / "state.json"
    if not state_path.exists():
        raise FileNotFoundError(f"尚未初始化 acct2 托管状态，请先执行 init：{state_path}")
    return Acct2State.from_dict(json.loads(state_path.read_text(encoding="utf-8")))


def _save_state(runtime_dir: Path, state: Acct2State) -> None:
    save_json(runtime_dir / "state.json", state.to_dict())


def _collect_market_data(
    state: Acct2State, cfg: Acct2ManagedConfig, runtime_dir: Path, trade_date: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    codes = set(cfg.etf_universe.keys()) | set(state.positions.keys()) | set(state.benchmark_holdings.keys())
    codes.add("510300.SH")
    quotes = fetch_realtime_quotes_tencent(codes)
    cache_dir = runtime_dir / "etf_cache"
    kline_map = {}
    for code in sorted(codes):
        try:
            kline_map[normalize_code(code)] = fetch_daily_bars_tencent(
                code, count=45, cache_dir=cache_dir, trade_date=trade_date
            )
        except Exception:
            kline_map[normalize_code(code)] = []
    return quotes, kline_map


def cmd_init(args: argparse.Namespace) -> int:
    runtime_dir = Path(args.runtime_dir)
    state_path = runtime_dir / "state.json"
    if state_path.exists() and not args.force:
        print(f"[SKIP] 已存在建档状态：{state_path}（如需重置请加 --force）")
        return 0

    snap_path = Path(args.snapshot)
    if not snap_path.exists():
        print(f"[ERR] 未找到 acct2 快照文件：{snap_path}")
        return 2

    raw = json.loads(snap_path.read_text(encoding="utf-8"))
    ra = raw.get("read_account") or raw
    funds = ra.get("funds") or {}
    holdings = ra.get("holdings") or []

    cash = float(funds.get("available") or funds.get("balance") or 0.0)
    now = datetime.now()
    trade_date = args.date or now.strftime("%Y-%m-%d")

    positions: dict[str, Position] = {}
    bm_holdings: dict[str, int] = {}
    for h in holdings:
        qty = int(float(h.get("quantity") or 0))
        if qty <= 0:
            continue
        code = normalize_code(h["code"])
        avail = int(float(h.get("available_quantity") if h.get("available_quantity") is not None else qty))
        cur_px = float(h.get("current_price") or 0.0)
        legacy_cost = float(h.get("cost_price") or cur_px)
        positions[code] = Position(
            code=code,
            name=str(h.get("name") or code),
            asset_type=infer_asset_type(code),
            qty=qty,
            available_qty=avail,
            avg_cost=cur_px,  # 接管日现价作为接管口径基准成本
            last_price=cur_px,
            legacy_cost_price=legacy_cost,
            entry_date=trade_date,
        )
        bm_holdings[code] = qty

    state = Acct2State(
        initialized_at=now.isoformat(timespec="seconds"),
        base_date=trade_date,
        cash=round(cash, 2),
        positions=positions,
        last_trade_date=trade_date,
        benchmark_cash=round(cash, 2),
        benchmark_holdings=bm_holdings,
    )
    state.base_assets = state.total_assets
    state.hwm_assets = state.total_assets
    state.prev_eod_assets = state.total_assets

    # 同步抓取 510300.SH 作为沪深300基准起点
    try:
        hs300_q = fetch_realtime_quotes_tencent(["510300.SH"]).get("510300.SH")
        if hs300_q and hs300_q.price > 0:
            state.benchmark_hs300_base = hs300_q.price
    except Exception:
        pass

    cfg = Acct2ManagedConfig()
    save_json(runtime_dir / "config.json", asdict(cfg))
    _save_state(runtime_dir, state)

    baseline_row = {
        "event": "init_baseline",
        "trade_date": trade_date,
        "ts": now.isoformat(timespec="seconds"),
        "total_assets": state.total_assets,
        "cash": state.cash,
        "market_value": state.market_value,
        "nav": 1.0,
        "drawdown_pct": 0.0,
        "hard_red_line_assets": round(state.base_assets * (1.0 - cfg.dd_red_pct), 2),
        "positions": [asdict(p) for p in state.positions.values()],
    }
    append_jsonl(runtime_dir / "nav_ledger.jsonl", baseline_row)
    print(json.dumps(baseline_row, ensure_ascii=False, indent=2))
    return 0


def cmd_premarket(args: argparse.Namespace) -> int:
    runtime_dir = Path(args.runtime_dir)
    cfg = _load_config(runtime_dir)
    state = _load_state(runtime_dir)
    now = datetime.now()
    trade_date = args.date or now.strftime("%Y-%m-%d")

    breadth = load_market_breadth(Path(args.t_tracker_dir), trade_date)
    quotes, kline_map = _collect_market_data(state, cfg, runtime_dir, trade_date)
    mark_to_market(state, quotes)
    pos_cap, tier = target_position_cap(breadth, state, cfg)
    intents = generate_strategy_intents(state, cfg, breadth, quotes, kline_map)

    # 汇总 ETF 池扫描表
    etf_scan = []
    for code, meta in cfg.etf_universe.items():
        norm = normalize_code(code)
        q = quotes.get(norm)
        m = calc_etf_metrics(kline_map.get(norm) or [])
        etf_scan.append(
            {
                "code": norm,
                "name": meta["name"],
                "bucket": meta["bucket"],
                "price": q.price if q else None,
                "pct_chg": round(q.pct_change, 2) if q else None,
                "ma20": m["ma20"] if m else None,
                "mom20_pct": m["mom20_pct"] if m else None,
                "above_ma20": bool(q and m and q.price > m["ma20"]),
            }
        )
    etf_scan.sort(key=lambda x: (x["above_ma20"], x["mom20_pct"] or -999.0), reverse=True)

    lines = [
        f"# acct2 托管盘前计划（{trade_date}，Dry-run）",
        "",
        "## 1. 账户与风控水位",
        f"- 总资产：`{state.total_assets:.2f}` 元（基准 `{state.base_assets:.2f}`，净值 `{state.nav:.4f}`）",
        f"- 现金 / 持仓：`{state.cash:.2f}` 元 / `{state.market_value:.2f}` 元（当前仓位 `{state.market_value/max(state.total_assets,1)*100:.1f}%`）",
        f"- 动态回撤：`{-state.drawdown_from_hwm*100:.2f}%`（风控档位：**`{tier}`**，`-12%` 硬红线 = `{state.hwm_assets*(1-cfg.dd_red_pct):.2f}` 元）",
        f"- 市场宽度：`above_ma20_pct = {breadth.above_ma20_pct:.2f}%`（`{breadth.level}`，数据日 `{breadth.date}`）→ 目标仓位上限 **`{pos_cap*100:.0f}%`**",
        "",
        "## 2. 当前持仓",
    ]
    if state.positions:
        for p in state.positions.values():
            w = p.market_value / max(state.total_assets, 1.0) * 100.0
            lines.append(
                f"- `{p.code}` {p.name}：{p.qty} 股（可卖 {p.available_qty}），现价 `{p.last_price:.3f}`，市值 `{p.market_value:.2f}`（占比 `{w:.1f}%`）"
            )
    else:
        lines.append("- 空仓")

    lines.extend(["", "## 3. 拟执行候选指令（需盘中实时过风控门）"])
    if intents:
        for it in intents:
            lines.append(
                f"- [{it.side.upper()}] `{it.code}` {it.name} `{it.qty}` 股 @ `{it.limit_price}` — {it.reason}"
            )
    else:
        lines.append("- 暂无触发指令，保持观察与防御。")

    lines.extend(
        [
            "",
            "## 4. 核心 ETF 趋势扫描（按站上 MA20 + 20日动量排序）",
            "| 代码 | 名称 | 桶 | 现价 | 涨跌% | MA20 | 20日动量% | 趋势 |",
            "| :--- | :--- | :--- | :---: | :---: | :---: | :---: | :---: |",
        ]
    )
    for row in etf_scan:
        px = f"{row['price']:.3f}" if row["price"] is not None else "-"
        chg = f"{row['pct_chg']:+.2f}%" if row["pct_chg"] is not None else "-"
        ma20 = f"{row['ma20']:.3f}" if row["ma20"] is not None else "-"
        m20 = f"{row['mom20_pct']:+.2f}%" if row["mom20_pct"] is not None else "-"
        trend = "站上MA20" if row["above_ma20"] else "MA20下方"
        lines.append(
            f"| {row['code']} | {row['name']} | {row['bucket']} | {px} | {chg} | {ma20} | {m20} | {trend} |"
        )

    plan_dir = runtime_dir / "daily_plans"
    plan_dir.mkdir(parents=True, exist_ok=True)
    tag = trade_date.replace("-", "")
    md_path = plan_dir / f"{tag}.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    save_json(
        plan_dir / f"{tag}.json",
        {
            "trade_date": trade_date,
            "generated_at": now.isoformat(timespec="seconds"),
            "risk_tier": tier,
            "target_position_cap": pos_cap,
            "breadth": asdict(breadth),
            "intents": [asdict(i) for i in intents],
            "etf_scan": etf_scan,
        },
    )
    print(f"[OK] 盘前计划已生成：{md_path}")
    if not getattr(args, "no_notify", False):
        intent_brief = (
            "\n".join(f"  • [{i.side.upper()}] {i.code} {i.name} {i.qty}股@{i.limit_price}（{i.reason}）" for i in intents)
            if intents
            else "  • 暂无预触发指令，盘中每10分钟巡检监控"
        )
        send_wechat_ilink(
            f"【📋 acct2全权托管·盘前计划】{trade_date}",
            (
                f"- 总资产：`{state.total_assets:.2f}` 元｜现金 `{state.cash:.2f}`｜仓位 `{state.market_value/max(state.total_assets,1)*100:.1f}%`\n"
                f"- 回撤：`{-state.drawdown_from_hwm*100:.2f}%`（档位 `{tier}`，-12%红线 `{state.hwm_assets*(1-cfg.dd_red_pct):.2f}`）\n"
                f"- 市场宽度：`{breadth.above_ma20_pct:.1f}%`（`{breadth.level}`）→ 目标仓位上限 `{pos_cap*100:.0f}%`\n"
                f"- 今日预案：\n{intent_brief}"
            ),
        )
    return 0


def cmd_intraday(args: argparse.Namespace) -> int:
    runtime_dir = Path(args.runtime_dir)
    cfg = _load_config(runtime_dir)
    state = _load_state(runtime_dir)
    now = datetime.now()
    trade_date = args.date or now.strftime("%Y-%m-%d")

    breadth = load_market_breadth(Path(args.t_tracker_dir), trade_date)
    quotes, kline_map = _collect_market_data(state, cfg, runtime_dir, trade_date)
    mark_to_market(state, quotes)

    # 盘中实时触发 -12% 硬熔断保护
    tier, _ = evaluate_risk_tier(state, cfg)
    if tier == "red" and not state.kill_switch:
        state.kill_switch = True
        state.kill_switch_reason = (
            f"盘中触发 -{cfg.dd_red_pct*100:.0f}% 硬熔断（总资产 {state.total_assets:.2f}）"
        )
        _save_state(runtime_dir, state)
        if not getattr(args, "no_notify", False):
            send_wechat_ilink(
                "【🚨 acct2托管·硬熔断停机】触及 -12% 最大回撤红线",
                f"- 账号：`acct2`\n- 原因：{state.kill_switch_reason}\n- 状态：已自动锁死开仓权限，请人工复核。",
            )

    intents = generate_strategy_intents(state, cfg, breadth, quotes, kline_map)
    executed = []
    rejected = []

    for intent in intents:
        q = quotes.get(normalize_code(intent.code))
        gate = check_order_gate(
            intent,
            q,
            state,
            cfg,
            now_dt=now,
            bypass_session_check=bool(args.bypass_session),
        )
        if gate.approved and q is not None:
            fill = execute_paper_order(intent, q, state, cfg, trade_date, now)
            notify_res = (
                {"ok": False, "detail": "skipped_no_notify"}
                if getattr(args, "no_notify", False)
                else notify_paper_fill_wechat(fill, state)
            )
            fill["wechat_notify"] = notify_res
            executed.append(fill)
            append_jsonl(
                runtime_dir / "orders_journal.jsonl",
                {
                    "status": "paper_filled",
                    "gate": asdict(gate),
                    "fill": fill,
                    "wechat_notify": notify_res,
                },
            )
        else:
            rec = {
                "ts": now.isoformat(timespec="seconds"),
                "trade_date": trade_date,
                "status": "rejected",
                "intent": asdict(intent),
                "gate": asdict(gate),
            }
            rejected.append(rec)
            append_jsonl(runtime_dir / "orders_journal.jsonl", rec)

    _save_state(runtime_dir, state)
    summary = {
        "trade_date": trade_date,
        "ts": now.isoformat(timespec="seconds"),
        "risk_tier": tier,
        "total_assets": state.total_assets,
        "cash": state.cash,
        "intents_count": len(intents),
        "executed_count": len(executed),
        "rejected_count": len(rejected),
        "executed": executed,
        "rejected": rejected,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_eod(args: argparse.Namespace) -> int:
    runtime_dir = Path(args.runtime_dir)
    cfg = _load_config(runtime_dir)
    state = _load_state(runtime_dir)
    now = datetime.now()
    trade_date = args.date or now.strftime("%Y-%m-%d")

    quotes, _ = _collect_market_data(state, cfg, runtime_dir, trade_date)
    record = apply_eod_settlement(state, cfg, quotes, trade_date)
    _save_state(runtime_dir, state)
    append_jsonl(runtime_dir / "nav_ledger.jsonl", {"event": "eod", **record})

    review_dir = runtime_dir / "daily_reviews"
    review_dir.mkdir(parents=True, exist_ok=True)
    tag = trade_date.replace("-", "")
    md_path = review_dir / f"{tag}.md"
    hs300_str = f"{record['benchmark_hs300_nav']:.4f}" if record["benchmark_hs300_nav"] else "-"
    lines = [
        f"# acct2 托管盘后复盘（{trade_date}，Dry-run）",
        "",
        "## 1. 净值与回撤",
        f"- 总资产：`{record['total_assets']:.2f}` 元（现金 `{record['cash']:.2f}` / 持仓 `{record['market_value']:.2f}`，仓位 `{record['position_weight']*100:.1f}%`）",
        f"- 托管净值：**`{record['nav']:.4f}`**（当日 `{record['daily_pnl_pct']:+.2f}%`）",
        f"- 历史最高资产：`{record['hwm_assets']:.2f}` 元，当前动态回撤：**`{record['drawdown_pct']:.2f}%`**（风控档位：`{record['risk_tier']}`，kill_switch=`{record['kill_switch']}`）",
        f"- 累计仿真手续费：`{record['total_fees_paid']:.2f}` 元",
        "",
        "## 2. 对比基准",
        f"- 躺平不动净值（原样持有接管持仓）：`{record['benchmark_hold_nav']:.4f}`（资产 `{record['benchmark_hold_assets']:.2f}` 元）",
        f"- 托管相对躺平超额：**`{record['excess_vs_hold_pct']:+.2f}%`**",
        f"- 沪深300（510300.SH）基准净值：`{hs300_str}`",
        "",
        "## 3. 日终持仓（已执行 T+1 解冻）",
    ]
    if record["positions"]:
        for p in record["positions"]:
            lines.append(
                f"- `{p['code']}` {p['name']}：{p['qty']} 股 @ `{p['last_price']:.3f}`，市值 `{p['market_value']:.2f}` 元"
            )
    else:
        lines.append("- 空仓")

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[OK] 盘后复盘已生成：{md_path}")
    brief_path = _write_evening_brief(runtime_dir, trade_date, record, state)
    print(f"[OK] 晚间复盘简报已自动生成（零 token）：{brief_path}")
    if not getattr(args, "no_notify", False):
        pos_str = (
            "、".join(f"{p['name']}({p['code']}){p['qty']}股" for p in record["positions"])
            if record["positions"]
            else "空仓"
        )
        send_wechat_ilink(
            f"【📊 acct2全权托管·日终复盘】{trade_date} 净值 {record['nav']:.4f}",
            (
                f"- 总资产：`{record['total_assets']:.2f}` 元（当日 `{record['daily_pnl_pct']:+.2f}%`）\n"
                f"- 现金 / 持仓：`{record['cash']:.2f}` / `{record['market_value']:.2f}`（{pos_str}）\n"
                f"- 动态回撤：`{record['drawdown_pct']:.2f}%`（档位 `{record['risk_tier']}`，累计费用 `{record['total_fees_paid']:.2f}` 元）\n"
                f"- 对比躺平超额：`{record['excess_vs_hold_pct']:+.2f}%`｜沪深300净值：`{hs300_str}`"
            ),
        )
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def _write_evening_brief(
    runtime_dir: Path,
    trade_date: str,
    record: dict[str, Any],
    state: Acct2State,
) -> Path:
    """零 token 自动生成 acct2 晚间复盘简报（替代原 Antigravity 20:00 定时任务）。"""
    tag = trade_date.replace("-", "")
    brief_dir = runtime_dir / "evening_briefs"
    brief_dir.mkdir(parents=True, exist_ok=True)
    brief_path = brief_dir / f"{tag}.md"

    hwm = float(record.get("hwm_assets") or state.hwm_assets or 0.0)
    tot = float(record.get("total_assets") or state.total_assets or 0.0)
    dd_pct = float(record.get("drawdown_pct") or 0.0)
    red_line = hwm * 0.88
    orange_line = hwm * 0.90
    yellow_line = hwm * 0.94
    pad_red = tot - red_line
    pad_red_pct = (12.0 - abs(dd_pct)) if hwm > 0 else 0.0
    hs300_str = f"{record['benchmark_hs300_nav']:.4f}" if record.get("benchmark_hs300_nav") else "-"

    # 1. 读取当日 orders_journal.jsonl
    day_orders: list[dict[str, Any]] = []
    oj_path = runtime_dir / "orders_journal.jsonl"
    if oj_path.exists():
        for line in oj_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if str(obj.get("trade_date") or "") == trade_date or str(obj.get("ts") or "").startswith(trade_date):
                day_orders.append(obj)

    # 2. 审计当日 logs/YYYYMMDD.log 健康度
    log_path = runtime_dir / "logs" / f"{tag}.log"
    log_runs = 0
    log_errs: list[str] = []
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "rc=" in line:
                log_runs += 1
                if "rc=0" not in line:
                    log_errs.append(line.strip()[:160])
            elif "ERROR" in line or "Traceback" in line or "HTTP 400" in line:
                log_errs.append(line.strip()[:160])

    pos_lines = []
    for p in record.get("positions") or []:
        pos_lines.append(
            f"- `{p['code']}` {p['name']}：{p['qty']} 股 @ `{p['last_price']:.3f}`，市值 `{p['market_value']:.2f}` 元"
        )
    if not pos_lines:
        pos_lines = ["- 空仓（100% 现金防守观望）"]

    order_lines = []
    for o in day_orders:
        st = o.get("status") or o.get("event") or "event"
        if st == "paper_filled":
            f = o.get("fill") or o
            order_lines.append(
                f"| `{str(f.get('ts') or o.get('ts') or '')[11:19]}` | `{st}` | {f.get('side')} `{f.get('code')}` {f.get('name', '')} | {f.get('qty')}股 @ `{f.get('fill_price')}` | {f.get('reason', '')} |"
            )
        elif st == "rejected":
            it = o.get("intent") or {}
            gt = o.get("gate") or {}
            order_lines.append(
                f"| `{str(o.get('ts') or '')[11:19]}` | `rejected` | {it.get('side')} `{it.get('code')}` | {it.get('qty', '-')}股 | 拦截：{gt.get('reason', '')} |"
            )
        else:
            order_lines.append(
                f"| `{str(o.get('ts') or '')[11:19]}` | `{st}` | `{o.get('code', '-')}` | {o.get('qty', '-')} | {o.get('note') or o.get('reason') or '-'} |"
            )

    lines = [
        f"# acct2 半托管试运行晚间复盘 — {trade_date}（Python 零 Token 自动生成）",
        "",
        "## 一、核心指标与安全垫",
        f"- **日终总资产**：`{tot:.2f}` 元（现金 `{float(record.get('cash', 0)):.2f}` 元 / 持仓 `{float(record.get('market_value', 0)):.2f}` 元，仓位 `{float(record.get('position_weight', 0))*100:.1f}%`）",
        f"- **托管净值 / 当日盈亏**：`{float(record.get('nav', 1)):.4f}`（`{float(record.get('daily_pnl_pct', 0)):+.2f}%`）",
        f"- **历史最高资产（HWM）**：`{hwm:.2f}` 元｜**动态回撤**：`{dd_pct:.2f}%`（档位：`{record.get('risk_tier')}`，`kill_switch={record.get('kill_switch')}`）",
        f"- **距 `-12%` 硬红线（`{red_line:.2f}`）安全垫**：`+{pad_red_pct:.2f}%`（`+{pad_red:.2f}` 元）｜`-10%` 橙线 `{orange_line:.2f}`｜`-6%` 黄线 `{yellow_line:.2f}`",
        f"- **累计仿真费用**：`{float(record.get('total_fees_paid', 0)):.2f}` 元",
        "",
        "## 二、基准对比（托管 vs 躺平 vs 沪深 300）",
        f"- 托管净值：`{float(record.get('nav', 1)):.4f}`",
        f"- 躺平不动净值：`{float(record.get('benchmark_hold_nav', 1)):.4f}`（资产 `{float(record.get('benchmark_hold_assets', 0)):.2f}` 元）｜托管相对躺平超额：**`{float(record.get('excess_vs_hold_pct', 0)):+.2f}%`**",
        f"- 沪深300（510300.SH）基准净值：`{hs300_str}`",
        "",
        "## 三、日终持仓（已执行 T+1 解冻）",
        *pos_lines,
        "",
        "## 四、当日信号、成交与风控拦截归因",
    ]
    if order_lines:
        lines.extend([
            "| 时间 | 状态 | 方向/标的 | 数量与价格 | 触发/拦截归因 |",
            "| :--- | :--- | :--- | :--- | :--- |",
            *order_lines,
        ])
    else:
        lines.append("- 当日无成交或风控拦截记录（全程静默观察）。")

    lines.extend([
        "",
        "## 五、定时巡检健康度",
        f"- 日志文件：`{log_path.name}`｜记录巡检拍次：`{log_runs}`｜异常条目：`{len(log_errs)}`",
    ])
    for err in log_errs[:8]:
        lines.append(f"  - 异常：`{err}`")
    if not log_errs:
        lines.append("- 全日巡检零异常。")

    brief_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return brief_path


def cmd_evening_brief(args: argparse.Namespace) -> int:
    """独立补跑晚间复盘简报（基于当日 nav_ledger 最新 EOD 记录，零实盘改动）。"""
    runtime_dir = Path(args.runtime_dir)
    state = _load_state(runtime_dir)
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")
    rec: dict[str, Any] = {}
    nav_path = runtime_dir / "nav_ledger.jsonl"
    if nav_path.exists():
        for line in nav_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("event") == "eod" and str(obj.get("trade_date") or obj.get("date") or "") == trade_date:
                rec = obj
    if not rec:
        rec = {
            "trade_date": trade_date,
            "total_assets": state.total_assets,
            "cash": state.cash,
            "market_value": max(0.0, state.total_assets - state.cash),
            "position_weight": (max(0.0, state.total_assets - state.cash) / state.total_assets) if state.total_assets > 0 else 0.0,
            "nav": (state.total_assets / state.initial_assets) if getattr(state, "initial_assets", 0) else 1.0,
            "daily_pnl_pct": 0.0,
            "hwm_assets": state.hwm_assets,
            "drawdown_pct": -state.drawdown_from_hwm * 100.0,
            "risk_tier": state.risk_tier,
            "kill_switch": state.kill_switch,
            "total_fees_paid": state.total_fees_paid,
            "benchmark_hold_nav": 1.0,
            "benchmark_hold_assets": state.initial_assets,
            "excess_vs_hold_pct": 0.0,
            "benchmark_hs300_nav": None,
            "positions": [asdict(p) for p in state.positions.values()],
        }
    out = _write_evening_brief(runtime_dir, trade_date, rec, state)
    print(f"[OK] 晚间复盘简报已生成：{out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="acct2 独立半托管 1 周试运行工具（Dry-run）")
    ap.add_argument("--runtime-dir", default=str(DEFAULT_RUNTIME_DIR))
    ap.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT_PATH))
    ap.add_argument("--t-tracker-dir", default=str(DEFAULT_T_TRACKER_DIR))
    ap.add_argument("--date", default=None, help="覆盖交易日 YYYY-MM-DD")

    sub = ap.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="从 latest_acct2.json 初始化建档")
    p_init.add_argument("--force", action="store_true", help="强制覆盖已有建档")
    p_init.set_defaults(func=cmd_init)

    p_pre = sub.add_parser("premarket", help="生成当日盘前交易计划")
    p_pre.add_argument("--no-notify", action="store_true", help="关闭微信推送")
    p_pre.set_defaults(func=cmd_premarket)

    p_intra = sub.add_parser("intraday", help="执行盘中信号评估与纸面演练")
    p_intra.add_argument("--bypass-session", action="store_true", help="仅调试/离线回放时跳过时段闸")
    p_intra.add_argument("--no-notify", action="store_true", help="关闭微信 ClawBot 推送（仅离线测试用）")
    p_intra.set_defaults(func=cmd_intraday)

    p_eod = sub.add_parser("eod", help="执行盘后净值结算、T+1解冻与复盘")
    p_eod.add_argument("--no-notify", action="store_true", help="关闭微信推送")
    p_eod.set_defaults(func=cmd_eod)

    p_eb = sub.add_parser("evening-brief", help="零 token 独立生成晚间复盘简报")
    p_eb.set_defaults(func=cmd_evening_brief)
    return ap


def run_mode(mode: str, extra_args: Optional[list[str]] = None) -> int:
    """供内部调度器直接按 mode（premarket/intraday/eod/evening-brief）调用。"""
    ap = build_parser()
    argv = [mode] + list(extra_args or [])
    args = ap.parse_args(argv)
    return int(args.func(args))


def main() -> int:
    ap = build_parser()
    args = ap.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
