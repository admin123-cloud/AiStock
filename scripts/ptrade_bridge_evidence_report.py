"""Build a read-only PTrade bridge operator checklist and evidence summary."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.ptrade_bridge import PTradeFileBridge
from scripts.ptrade_bridge_readiness_audit import run_audit, write_json
from utils.paths import runtime_path


DEFAULT_BRIDGE_DIR = runtime_path("ptrade_bridge")
DEFAULT_OUT_DIR = ROOT / "reports" / "ptrade_bridge_operator_checklist"


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            data = json.loads(path.read_text(encoding=encoding))
            return data if isinstance(data, dict) else {"value": data}
        except Exception:
            continue
    return {"ok": False, "read_error": f"cannot read json: {path}"}


def _latest_report(name: str) -> Path:
    return ROOT / "reports" / name / "latest.json"


def _report_status(path: Path) -> Dict[str, Any]:
    data = _read_json(path)
    return {
        "path": str(path),
        "exists": path.exists(),
        "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S") if path.exists() else None,
        "ok": bool(data.get("ok")) if isinstance(data, dict) else False,
        "summary": data,
    }


def _check(name: str, ok: bool, evidence: str, next_action: str) -> Dict[str, Any]:
    return {
        "name": name,
        "ok": bool(ok),
        "evidence": evidence,
        "next_action": next_action,
    }


def _safe_get(data: Dict[str, Any], *keys: str) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _probe_check_ok(report: Dict[str, Any], name: str) -> bool:
    checks = _safe_get(report, "summary", "checks")
    if not isinstance(checks, list):
        return False
    return any(isinstance(item, dict) and item.get("name") == name and item.get("ok") for item in checks)


def _refresh_order_path_probe_report_subprocess() -> Dict[str, Any]:
    report_path = ROOT / "reports" / "ptrade_order_path_probe" / "latest.json"
    script_path = ROOT / "scripts" / "ptrade_order_path_probe.py"
    cmd = [sys.executable, str(script_path), "--report", str(report_path)]
    completed = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "ptrade_order_path_probe subprocess failed: "
            f"returncode={completed.returncode}; stderr={completed.stderr.strip() or completed.stdout.strip() or '--'}"
        )
    data = _read_json(report_path)
    if not isinstance(data, dict):
        raise RuntimeError(f"ptrade_order_path_probe report missing or invalid: {report_path}")
    return data


def _blocking_diagnosis(
    *,
    local_submit_ready: bool,
    nonblocking_order_path_ok: bool,
    readiness: Dict[str, Any],
    gates: Dict[str, Any],
    live_submit_evidence_ok: bool,
) -> Dict[str, Any]:
    if not local_submit_ready:
        return {
            "stage": "local_bridge_not_ready",
            "reason": "本地桥接目录不可写或配置异常，AiStock 还不能可靠写入 pending。",
            "next_action": "修复 F:\\Stock\\AiStockData\\data\\runtime\\ptrade_bridge 目录权限、config.json 或本地桥接初始化。",
            "external_wait": False,
        }
    if not nonblocking_order_path_ok:
        return {
            "stage": "order_path_not_proven_nonblocking",
            "reason": "G2 到 PTrade 的本地下单快路径尚未证明非阻塞。",
            "next_action": "运行 ptrade_order_path_probe.py，确认请求快照、缺快照快速拒绝、无重查询和提交耗时都通过。",
            "external_wait": False,
        }
    if not bool(readiness.get("ptrade_heartbeat_recent")):
        return {
            "stage": "ptrade_heartbeat_missing",
            "reason": "长时间阻塞原因：湘财证券 PTrade 云仿真交易端尚未写出真实策略心跳 status/latest.json，说明 PTrade 内部桥接策略未启动、未运行到轮询，或 BRIDGE_DIR 不一致。",
            "next_action": "在 PTrade 云仿真交易端启动/重启内部桥接策略，并确认策略里的 BRIDGE_DIR 指向 F:\\Stock\\AiStockData\\data\\runtime\\ptrade_bridge。",
            "external_wait": True,
        }
    if not bool(readiness.get("dry_run_probe_ack_recent")):
        return {
            "stage": "dry_run_ack_missing",
            "reason": "长时间阻塞原因：PTrade 心跳已有，但 dry-run 探针订单尚未被 PTrade 消费并写回 ack。",
            "next_action": "运行 ptrade_bridge_live_probe.py --submit-dry-run --require-heartbeat，随后检查 acks 与 PTrade 策略端 errors。",
            "external_wait": True,
        }
    if not bool(readiness.get("ptrade_live_order_enabled")):
        return {
            "stage": "ptrade_live_order_disabled",
            "reason": "长时间阻塞原因：dry-run 消费链路已证明，但 PTrade 策略心跳尚未报告 enable_live_order=true。",
            "next_action": "现场确认 dry-run ack 后，将 PTrade 策略 ENABLE_LIVE_ORDER 切到 True 并等待新心跳。",
            "external_wait": True,
        }
    if not bool(gates.get("live_submit_ready")):
        return {
            "stage": "live_submit_preconditions_missing",
            "reason": "live-submit 前置条件尚未同时满足，通常是队列未空、processing 陈旧或 readiness gate 尚未放行。",
            "next_action": "确认 pending/processing 为空、无陈旧 processing，并重新运行 readiness audit。",
            "external_wait": False,
        }
    if not live_submit_evidence_ok:
        return {
            "stage": "live_submit_evidence_missing",
            "reason": "小额 live-submit 还没有产生验收证据。",
            "next_action": "仅在账户、价格、数量人工确认后运行 ptrade_bridge_live_submit_test.py --approve-live-submit 小额测试。",
            "external_wait": True,
        }
    return {
        "stage": "complete",
        "reason": "PTrade 桥接验收证据已齐全。",
        "next_action": "",
        "external_wait": False,
    }


def build_evidence(
    bridge_dir: Path = DEFAULT_BRIDGE_DIR,
    out_dir: Path = DEFAULT_OUT_DIR,
    refresh_order_path_probe: bool = True,
) -> Dict[str, Any]:
    bridge = PTradeFileBridge(root=bridge_dir)
    status = bridge.status()
    audit = run_audit(bridge_dir=bridge.paths.root, require_empty_queue_for_live=True)
    if refresh_order_path_probe:
        order_path_probe = _refresh_order_path_probe_report_subprocess()
        write_json(_latest_report("ptrade_order_path_probe"), order_path_probe)
    reports = {
        "preflight": _report_status(_latest_report("ptrade_bridge_preflight")),
        "watch_acceptance": _report_status(_latest_report("ptrade_bridge_watch_acceptance")),
        "acceptance": _report_status(_latest_report("ptrade_bridge_acceptance")),
        "live_probe": _report_status(_latest_report("ptrade_bridge_live_probe")),
        "live_submit_test": _report_status(_latest_report("ptrade_bridge_live_submit_test")),
        "order_path_probe": _report_status(_latest_report("ptrade_order_path_probe")),
    }
    readiness = status.get("readiness") if isinstance(status.get("readiness"), dict) else {}
    gates = audit.get("gates") if isinstance(audit.get("gates"), dict) else {}
    pending_count = int(status.get("pending_count") or 0)
    processing_count = int(status.get("processing_count") or 0)
    heartbeat_file = status.get("ptrade_heartbeat_file")
    latest_probe_ack = status.get("latest_probe_ack") if isinstance(status.get("latest_probe_ack"), dict) else {}
    order_path_report = reports["order_path_probe"]
    order_path_ok = bool(order_path_report.get("ok"))
    nonblocking_order_path_ok = (
        order_path_ok
        and _probe_check_ok(order_path_report, "candidate_snapshot_used")
        and _probe_check_ok(order_path_report, "market_gate_snapshot_used")
        and _probe_check_ok(order_path_report, "missing_candidate_snapshot_fast_reject")
        and _probe_check_ok(order_path_report, "missing_market_gate_snapshot_fast_reject")
        and _probe_check_ok(order_path_report, "no_heavy_lookup_called")
        and _probe_check_ok(order_path_report, "submit_latency_within_budget")
        and _probe_check_ok(order_path_report, "pending_written")
    )
    submit_elapsed = _safe_get(order_path_report, "summary", "submit_elapsed_seconds")
    max_submit_seconds = _safe_get(order_path_report, "summary", "max_submit_seconds")

    checks = [
        _check(
            "本地下单写入路径可用",
            bool(readiness.get("local_submit_ready")),
            "execution.ptrade_bridge.PTradeFileBridge.status().readiness.local_submit_ready",
            "修复 F:\\Stock\\AiStockData\\data\\runtime\\ptrade_bridge 目录权限或配置后再验收。",
        ),
        _check(
            "正常下单路径非阻塞已验证",
            nonblocking_order_path_ok,
            f"{order_path_report['path']}; submit_elapsed_seconds={submit_elapsed}; max_submit_seconds={max_submit_seconds}",
            "运行 python F:\\Stock\\AiStock\\scripts\\ptrade_order_path_probe.py，确认请求快照路径、无重查询和提交耗时都通过。",
        ),
        _check(
            "真实 PTrade 策略心跳可见",
            bool(readiness.get("ptrade_heartbeat_recent")),
            str(heartbeat_file or "缺少 F:\\Stock\\AiStockData\\data\\runtime\\ptrade_bridge\\status\\latest.json"),
            "在湘财证券 PTrade 云仿真交易端启动内部桥接策略，等待 status/latest.json 更新。",
        ),
        _check(
            "dry-run ack 已被 PTrade 消费",
            bool(readiness.get("dry_run_probe_ack_recent")),
            str(latest_probe_ack.get("path") or latest_probe_ack.get("order_id") or "缺少近期 dry-run ack"),
            "心跳正常后运行 ptrade_bridge_live_probe.py --submit-dry-run --require-heartbeat。",
        ),
        _check(
            "PTrade 已显式允许 live order",
            bool(readiness.get("ptrade_live_order_enabled")),
            "status/latest.json heartbeat enable_live_order=true",
            "dry-run ack 证明消费链路后，现场再把 PTrade 策略 ENABLE_LIVE_ORDER 切到 True。",
        ),
        _check(
            "小额 live-submit 队列前置条件满足",
            bool(gates.get("live_submit_ready")),
            f"readiness_audit.gates.live_submit_ready={bool(gates.get('live_submit_ready'))}; pending={pending_count}; processing={processing_count}",
            "等待队列清空、心跳和 dry-run ack 同时满足后，再人工批准小额 live-submit。",
        ),
        _check(
            "小额 live-submit 证据已产生",
            bool(_safe_get(reports, "live_submit_test", "ok")),
            reports["live_submit_test"]["path"],
            "仅在 live_submit_ready=true 且人工确认账户后运行 --approve-live-submit 小额测试。",
        ),
    ]

    completed = all(item["ok"] for item in checks)
    live_submit_evidence_ok = bool(_safe_get(reports, "live_submit_test", "ok"))
    blocking = _blocking_diagnosis(
        local_submit_ready=bool(readiness.get("local_submit_ready")),
        nonblocking_order_path_ok=nonblocking_order_path_ok,
        readiness=readiness,
        gates=gates,
        live_submit_evidence_ok=live_submit_evidence_ok,
    )
    result = {
        "ok": completed,
        "generated_at": _now_text(),
        "bridge_dir": str(bridge.paths.root),
        "status": status,
        "readiness_audit": audit,
        "reports": reports,
        "checks": checks,
        "blocking": blocking,
        "blocking_stage": blocking.get("stage"),
        "blocking_reason": blocking.get("reason"),
        "next_actions": [item["next_action"] for item in checks if not item["ok"]],
        "note": "Evidence report. It never submits orders, writes real bridge pending files, calls PTrade, or queries ClickHouse. It may refresh the isolated ptrade_order_path_probe directory.",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "latest.json", result)
    (out_dir / "latest.md").write_text(render_markdown(result), encoding="utf-8")
    return result


def _yes(ok: bool) -> str:
    return "通过" if ok else "未通过"


def render_markdown(result: Dict[str, Any]) -> str:
    status = result.get("status") if isinstance(result.get("status"), dict) else {}
    readiness = status.get("readiness") if isinstance(status.get("readiness"), dict) else {}
    blocking = result.get("blocking") if isinstance(result.get("blocking"), dict) else {}
    reports = result.get("reports") if isinstance(result.get("reports"), dict) else {}
    checks = result.get("checks") if isinstance(result.get("checks"), list) else []
    next_actions = result.get("next_actions") if isinstance(result.get("next_actions"), list) else []
    lines = [
        "# PTrade 桥接现场验收证据清单",
        "",
        f"- 生成时间：{result.get('generated_at')}",
        f"- 桥接目录：`{result.get('bridge_dir')}`",
        f"- 总结论：`{_yes(bool(result.get('ok')))}`",
        f"- pending：`{status.get('pending_count', 0)}`；processing：`{status.get('processing_count', 0)}`；cancel_requests：`{status.get('cancel_request_count', 0)}`",
        f"- 心跳文件：`{status.get('ptrade_heartbeat_file') or '未发现'}`",
        f"- live readiness：`{bool(readiness.get('ready_for_live_order'))}`；PTrade live enabled：`{bool(readiness.get('ptrade_live_order_enabled'))}`",
        f"- 当前阻塞阶段：`{blocking.get('stage') or 'unknown'}`",
        f"- 当前阻塞原因：{blocking.get('reason') or '未生成诊断'}",
        "",
        "## 完成条件",
        "",
        "| 条件 | 状态 | 证据 | 下一步 |",
        "| --- | --- | --- | --- |",
    ]
    for item in checks:
        lines.append(
            f"| {item.get('name')} | {_yes(bool(item.get('ok')))} | `{item.get('evidence')}` | {item.get('next_action')} |"
        )
    lines.extend(
        [
            "",
            "## 现场操作顺序",
            "",
            "1. 只读检查本地状态：`python F:\\Stock\\AiStock\\scripts\\ptrade_bridge_readiness_audit.py`",
            "2. 在湘财证券 PTrade 云仿真交易端启动内部桥接策略，确认 `F:\\Stock\\AiStockData\\data\\runtime\\ptrade_bridge\\status\\latest.json` 持续更新。",
            "3. 跑等待式 dry-run 验收：`python F:\\Stock\\AiStock\\scripts\\ptrade_bridge_watch_acceptance.py --watch-timeout-seconds 600 --dry-run-timeout-seconds 30`",
            "4. dry-run ack 通过后，现场再把 PTrade 策略 `ENABLE_LIVE_ORDER` 切换为 `True`，等待心跳写出 `enable_live_order=true`。",
            "5. `live_submit_ready=true` 且账户/价格/数量人工确认后，才运行：`python F:\\Stock\\AiStock\\scripts\\ptrade_bridge_live_submit_test.py --approve-live-submit --code 600000 --price 10.5 --quantity 100`",
            "",
            "## 不阻塞下单边界",
            "",
            "- 正常 G2 下单路径只写真实桥接目录的本地 `pending/*.json` 并立即返回，不等待 PTrade ack。",
            "- 页面提交必须携带请求里已有的候选股快照和大盘闸门快照；快照缺失或不匹配时快速拒绝，不在下单路径重建选股池、拉全量行情或等待数据库入库。",
            "- 可以降低最新行情、持仓快照和 ClickHouse 入库频率；这些任务不得成为发现行情或写入 `pending` 的前置阻塞。",
            "- 只有 `dry_run=false` 且 `approved=true` 的真实 broker live-submit 会被 readiness gate 拦截；dry-run 和真实桥接目录的本地 pending 写入仍保持非阻塞。",
            "",
            "## 报告文件",
            "",
        ]
    )
    for name, info in reports.items():
        if isinstance(info, dict):
            lines.append(f"- `{name}`：exists=`{bool(info.get('exists'))}` ok=`{bool(info.get('ok'))}` path=`{info.get('path')}`")
    if next_actions:
        lines.extend(["", "## 当前下一步", ""])
        for action in next_actions:
            lines.append(f"- {action}")
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build read-only PTrade bridge evidence report.")
    parser.add_argument("--bridge-dir", default=str(DEFAULT_BRIDGE_DIR))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--skip-order-path-probe-refresh", action="store_true")
    args = parser.parse_args(argv)
    result = build_evidence(
        bridge_dir=Path(args.bridge_dir),
        out_dir=Path(args.out_dir),
        refresh_order_path_probe=not bool(args.skip_order_path_probe_refresh),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
