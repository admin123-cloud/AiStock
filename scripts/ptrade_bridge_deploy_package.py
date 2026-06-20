"""Build a local deployment package for the AiStock -> PTrade bridge."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ptrade_bridge_preflight import run_preflight
from scripts.ptrade_bridge_readiness_audit import run_audit
from utils.paths import runtime_path


DEFAULT_OUT_DIR = ROOT / "reports" / "ptrade_bridge_deploy_package"
DEFAULT_BRIDGE_DIR = runtime_path("ptrade_bridge")
STRATEGY_SOURCE = ROOT / "scripts" / "ptrade_file_bridge_strategy.py"
EVIDENCE_REPORT_SOURCE = ROOT / "scripts" / "ptrade_bridge_evidence_report.py"
ORDER_PATH_PROBE_SOURCE = ROOT / "scripts" / "ptrade_order_path_probe.py"
HEARTBEAT_DIAGNOSE_SOURCE = ROOT / "scripts" / "ptrade_bridge_heartbeat_diagnose.py"
HEARTBEAT_SMOKE_SOURCE = ROOT / "scripts" / "ptrade_heartbeat_smoke_strategy.py"
GEN3_ACCEPTANCE_SOURCE = ROOT / "scripts" / "gen3_ptrade_bridge_acceptance.py"
GEN3_INTERNAL_ACCEPTANCE_SOURCE = ROOT / "scripts" / "gen3_ptrade_internal_strategy_acceptance.py"


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _clean_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _strategy_text_for_bridge(bridge_dir: Path) -> str:
    text = STRATEGY_SOURCE.read_text(encoding="utf-8-sig")
    bridge_literal = "BRIDGE_DIR = r" + json.dumps(str(bridge_dir), ensure_ascii=False)
    return re.sub(r"^BRIDGE_DIR\s*=.*$", bridge_literal, text, count=1, flags=re.MULTILINE)


def _readme(bridge_dir: Path, max_order_value: float, poll_seconds: float) -> str:
    return f"""# PTrade 云仿真桥接部署包

生成时间：{_now_text()}

## 文件

- `ptrade_file_bridge_strategy.py`：复制到湘财证券 PTrade 量化策略编辑器的策略源码。
- `ptrade_bridge_config.sample.json`：建议写入 `{bridge_dir}\\config.json` 的安全配置。
- `preflight.json`：本地部署前自检结果。
- `readiness_audit.json`：当前桥接状态的只读就绪审计结果。
- `manifest.json`：本部署包文件清单。

## PTrade 端部署步骤

1. 打开湘财证券 PTrade 交易端云仿真并完成登录。
2. 进入量化交易策略编辑页面，新建策略，粘贴 `ptrade_file_bridge_strategy.py`。
3. 确认策略顶部 `BRIDGE_DIR` 指向：`{bridge_dir}`。
4. 首次运行必须保持 `ENABLE_LIVE_ORDER = False`。
5. 启动策略任务后，回到 AiStock 页面或命令行执行验收门禁：

```powershell
python F:\\Stock\\AiStock\\scripts\\ptrade_bridge_acceptance.py --heartbeat-timeout-seconds 120 --dry-run-timeout-seconds 30
```

Bounded watcher command; start it before operating PTrade if you want AiStock to wait for heartbeat and then run dry-run acceptance automatically:
```powershell
python F:\\Stock\\AiStock\\scripts\\ptrade_bridge_watch_acceptance.py --watch-timeout-seconds 600 --dry-run-timeout-seconds 30
```

Evidence report command; it may refresh the isolated `ptrade_order_path_probe` directory, but it never writes real bridge `pending/*.json`, never calls PTrade, and never queries ClickHouse:
```powershell
python F:\\Stock\\AiStock\\scripts\\ptrade_bridge_evidence_report.py
```

Read-only evidence report command; use this when you only want to summarize existing evidence files:
```powershell
python F:\\Stock\\AiStock\\scripts\\ptrade_bridge_evidence_report.py --skip-order-path-probe-refresh
```

6. 如果只想手工分步验证，heartbeat 最近后执行 dry-run 探针：

```powershell
python F:\\Stock\\AiStock\\scripts\\ptrade_bridge_live_probe.py --submit-dry-run --require-heartbeat --timeout-seconds 30
```

启动 PTrade 内部策略任务后，执行 G3 内部策略买卖验收。该门禁要求心跳来源必须是
`ptrade_file_bridge_strategy`，否则不会写入 dry-run 订单：

```powershell
python F:\\Stock\\AiStock-core\\scripts\\gen3_ptrade_internal_strategy_acceptance.py --timeout-seconds 30
```

7. dry-run 探针确认 ack 最近后，将 PTrade 策略里的 `ENABLE_LIVE_ORDER` 切换为 `True`，等待心跳写出 `enable_live_order=true`，且 `live_submit_ready=true` 后，才允许人工批准一笔小额云仿真 live-submit 测试。
```powershell
python F:\\Stock\\AiStock\\scripts\\ptrade_bridge_live_submit_test.py --approve-live-submit --code 600000 --price 10.5 --quantity 100
```

该脚本在缺少 `--approve-live-submit`、`live_submit_ready` 未通过、队列不空或订单金额超过限额时会拒绝写入 live 订单。

## 安全默认值

- 策略轮询间隔：{poll_seconds} 秒。
- `ENABLE_LIVE_ORDER = False`。
- `dry_run_default = true`。
- `require_approval_default = true`。
- `max_order_value = {max_order_value}`。
- 持仓和订单快照默认关闭，避免 PTrade 账户查询阻塞订单消费。

## 心跳与队列观测

PTrade 策略每轮写入 `{bridge_dir}\\status\\latest.json`。除 `updated_at` 外，心跳会写入 `pending_count`、`processing_count`、`cancel_request_count`、`oldest_pending_age_seconds`、`oldest_processing_age_seconds`、`total_order_processed`、`total_cancel_processed`、`total_bridge_errors`、`last_order_poll`、`last_cancel_poll` 等字段。

这些字段用于证明策略正在消费本地文件队列；不要为了看持仓或订单快照而提高账户查询频率，订单消费和 ack 写入优先级最高。

## 不阻塞原则

AiStock 下单只写本地 `pending/*.json`，不等待行情快照、ClickHouse 入库、PTrade 账户查询或订单 ack。PTrade 策略独立消费 pending 队列并写 ack/status。
G2 页面下单必须携带页面已有的候选股快照和大盘门禁快照；快照缺失或不匹配时快速拒绝，不在下单路径重建选股池、拉全量行情或等待数据库入库。
"""


def _operator_checklist(bridge_dir: Path) -> str:
    return f"""# PTrade 云仿真桥接操作清单

生成时间：{_now_text()}

## 当前目标

先证明 PTrade 云仿真策略环境可以执行并写出心跳，再验证 AiStock 本地下单队列能被 PTrade 消费。不要直接跳到真实 live-submit。

## 登录后第一步：上传最小心跳烟测策略

1. 确认 PTrade 已登录到“云仿真（交易端）”。
2. 进入左侧“量化”。
3. 进入量化交易/策略上传入口。
4. 上传本部署包中的：

```text
ptrade_heartbeat_smoke_strategy.py
```

该文件约 731 字节，只写：

```text
{bridge_dir}\\status\\latest.json
```

它不导入 `os`，不导入 `pathlib`，不调用下单或撤单 API。

## 心跳验收

上传并启动烟测策略后，在本地运行：

```powershell
python F:\\Stock\\AiStock\\scripts\\ptrade_bridge_heartbeat_diagnose.py
```

通过标准：

- `stage` 不再是 `heartbeat_file_missing`
- `heartbeat_file_exists=true`
- `heartbeat_recent=true`

## 第二步：上传完整桥接策略

只有烟测心跳通过后，再上传：

```text
ptrade_file_bridge_strategy.py
```

首次运行仍保持策略内：

```python
ENABLE_LIVE_ORDER = False
```

## dry-run 消费验收

完整桥接策略启动并有心跳后，运行：

```powershell
python F:\\Stock\\AiStock\\scripts\\ptrade_bridge_live_probe.py --submit-dry-run --require-heartbeat --timeout-seconds 30
```

通过后再看证据报告：

```powershell
python F:\\Stock\\AiStock\\scripts\\ptrade_bridge_evidence_report.py --skip-order-path-probe-refresh
```

## 不阻塞原则

- AiStock 正常下单路径只写本地 `pending/*.json`，不能等待 PTrade、ClickHouse、行情快照入库或账户查询。
- 页面下单必须携带页面已有候选股快照和大盘门禁快照；缺快照时快速拒绝，不在下单路径重建候选池或拉全量行情。
- PTrade 策略只负责独立消费队列并写 `acks/status`。
- 持仓快照、订单快照、最新行情刷新频率可以降低；订单消费和 ack 写入优先级最高。

## 当前已知 PTrade 注意事项

- PTrade 策略沙箱会拒绝直接 `import os`。
- `C:\\xczq\\ptrade_yfz\\Libs\\Python\\python3.exe` 不适合作为普通命令行 Python 使用。
- 若策略上传长时间停在“正在上传”，优先重进量化页；仍不释放时重启 PTrade 前端，再从最小心跳烟测策略开始。
"""


def build_package(
    out_dir: Path = DEFAULT_OUT_DIR,
    bridge_dir: Path = DEFAULT_BRIDGE_DIR,
    max_order_value: float = 20000.0,
    poll_seconds: float = 3.0,
) -> Dict[str, Any]:
    out_dir = Path(out_dir)
    bridge_dir = Path(bridge_dir)
    _clean_dir(out_dir)

    strategy_target = out_dir / "ptrade_file_bridge_strategy.py"
    strategy_target.write_text(_strategy_text_for_bridge(bridge_dir), encoding="utf-8")
    evidence_report_target = out_dir / "ptrade_bridge_evidence_report.py"
    order_path_probe_target = out_dir / "ptrade_order_path_probe.py"
    heartbeat_diagnose_target = out_dir / "ptrade_bridge_heartbeat_diagnose.py"
    heartbeat_smoke_target = out_dir / "ptrade_heartbeat_smoke_strategy.py"
    gen3_acceptance_target = out_dir / "gen3_ptrade_bridge_acceptance.py"
    gen3_internal_acceptance_target = out_dir / "gen3_ptrade_internal_strategy_acceptance.py"
    shutil.copy2(EVIDENCE_REPORT_SOURCE, evidence_report_target)
    shutil.copy2(ORDER_PATH_PROBE_SOURCE, order_path_probe_target)
    shutil.copy2(HEARTBEAT_DIAGNOSE_SOURCE, heartbeat_diagnose_target)
    shutil.copy2(HEARTBEAT_SMOKE_SOURCE, heartbeat_smoke_target)
    shutil.copy2(GEN3_ACCEPTANCE_SOURCE, gen3_acceptance_target)
    shutil.copy2(GEN3_INTERNAL_ACCEPTANCE_SOURCE, gen3_internal_acceptance_target)

    config = {
        "dry_run_default": True,
        "require_approval_default": True,
        "max_order_value": float(max_order_value),
        "processing_stale_seconds": 300,
        "heartbeat_recent_seconds": 30,
        "dry_run_ack_recent_seconds": 600,
        "updated_at": _now_text(),
    }
    config_path = out_dir / "ptrade_bridge_config.sample.json"
    write_json(config_path, config)

    preflight = run_preflight(bridge_dir=bridge_dir, strategy_file=strategy_target)
    preflight_path = out_dir / "preflight.json"
    write_json(preflight_path, preflight)

    audit = run_audit(bridge_dir=bridge_dir)
    audit_path = out_dir / "readiness_audit.json"
    write_json(audit_path, audit)

    readme_path = out_dir / "README.md"
    readme_path.write_text(_readme(bridge_dir, float(max_order_value), float(poll_seconds)), encoding="utf-8")
    operator_checklist_path = out_dir / "OPERATE_CN.md"
    operator_checklist_path.write_text(_operator_checklist(bridge_dir), encoding="utf-8")

    manifest = {
        "ok": True,
        "package_dir": str(out_dir),
        "bridge_dir": str(bridge_dir),
        "created_at": _now_text(),
        "files": {
            "strategy": str(strategy_target),
            "config_sample": str(config_path),
            "preflight": str(preflight_path),
            "readiness_audit": str(audit_path),
            "evidence_report_script": str(evidence_report_target),
            "order_path_probe_script": str(order_path_probe_target),
            "heartbeat_diagnose_script": str(heartbeat_diagnose_target),
            "heartbeat_smoke_strategy": str(heartbeat_smoke_target),
            "gen3_acceptance_script": str(gen3_acceptance_target),
            "gen3_internal_acceptance_script": str(gen3_internal_acceptance_target),
            "readme": str(readme_path),
            "operator_checklist": str(operator_checklist_path),
        },
        "preflight_ok": bool(preflight.get("ok")),
        "gates": audit.get("gates", {}),
        "next_actions": preflight.get("next_actions", audit.get("next_actions", [])),
        "note": "Package generation is local only; it never submits orders and never calls PTrade.",
    }
    manifest_path = out_dir / "manifest.json"
    write_json(manifest_path, manifest)
    return manifest


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build PTrade bridge deployment package.")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--bridge-dir", default=str(DEFAULT_BRIDGE_DIR))
    parser.add_argument("--max-order-value", type=float, default=20000.0)
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    args = parser.parse_args(argv)

    manifest = build_package(
        out_dir=Path(args.out_dir),
        bridge_dir=Path(args.bridge_dir),
        max_order_value=float(args.max_order_value),
        poll_seconds=float(args.poll_seconds),
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
