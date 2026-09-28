from __future__ import annotations

import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path, runtime_path  # noqa: E402

OUT_DIR = report_path("gen3_strategy_rebuild_blueprint_v1")
RUNTIME_DIR = runtime_path("gen3_state_alpha")

STRATEGY_ID = "g3_state_alpha_v1"
STRATEGY_NAME = "G3 State Alpha"
STRATEGY_NAME_CN = "G3 State Alpha（第三代市场状态 Alpha）"


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _classify_script(path: Path) -> tuple[str, str]:
    name = path.name.lower()
    text = path.stem.lower()
    if "state_router" in text or "market_state_router" in text:
        return "keep_current_core", "当前市场状态路由核心证据；保留并通过新合同重命名包装。"
    if "institutional_mainwave_current" in text:
        return "keep_current_core", "当前机构主升适配器；保留为 G3 State Alpha 的机构主升模式输入。"
    if "shadow" in text and ("state_router" in text or "v4_strong_offense" in text or "range_filtered" in text or "guarded" in text):
        return "keep_shadow_observation", "影子观察产物入口；保留但前端不再作为主命名展示。"
    if "score120" in text or "route_execution" in text or "v4_strong" in text or "v3_v4" in text:
        return "evidence_archive_hide", "旧研究命名或阶段性假设；保留为证据，主导航隐藏。"
    if text.startswith("gen3_audit_") or text.startswith("gen3_test_") or text.startswith("gen3_stress_"):
        return "evidence_archive", "审计/测试/压力验证脚本；保留证据，不作为当前策略入口。"
    if text.startswith("gen3_backtest_") or text.startswith("gen3_validate_") or text.startswith("gen3_research_"):
        return "evidence_archive", "历史回测/验证/研究脚本；保留证据，必要时迁移到研究归档。"
    if text.startswith("gen3_build_") or text.startswith("gen3_package_"):
        return "migration_review", "构建/打包脚本；逐个确认是否仍被当前合同引用，未引用者转证据归档。"
    if "update_panic_shadow" in text or "update_range_shadow" in text or "update_strong_shadow" in text:
        return "path_fix_then_archive", "旧 shadow 更新脚本含历史路径风险；先改 utils.paths，再归档或隐藏。"
    return "migration_review", "需要人工复核依赖关系后决定保留或归档。"


def _path_rule_status(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return "unreadable"
    patterns = [
        r'REPO_ROOT\s*/\s*"reports"',
        r'ROOT\s*/\s*"reports"',
        r'REPO_ROOT\s*/\s*"data"',
        r'ROOT\s*/\s*"data"',
    ]
    return "needs_utils_paths_fix" if any(re.search(p, text) for p in patterns) else "ok"


def _report_dir_action(name: str) -> tuple[str, str]:
    key = name.lower()
    if key in {
        "gen3_market_state_router_v1",
        "gen3_market_state_router_strategy_v1",
        "gen3_state_router_shadow_daily_v1",
        "gen3_institutional_mainwave_current_v1",
    }:
        return "keep_current_core", "当前 G3 State Alpha 的历史/当前核心证据。"
    if "v4_strong" in key or "score120" in key or "route_execution" in key:
        return "evidence_archive_hide", "旧强势/score120/执行约束命名，保留证据但从主体验隐藏。"
    if "shadow" in key or "live_safe" in key:
        return "keep_shadow_observation", "影子/安全输出，保留用于观察一致性和运行诊断。"
    if "audit" in key or "stress" in key or "validation" in key or "research" in key or "backtest" in key:
        return "evidence_archive", "研究、压力和审计证据，迁入证据归档索引。"
    return "migration_review", "需要依据引用关系复核。"


def _build_inventory() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((ROOT / "scripts").glob("gen3*.py")):
        action, reason = _classify_script(path)
        rows.append(
            {
                "kind": "script",
                "name": path.name,
                "path": str(path),
                "action": action,
                "reason": reason,
                "path_rule_status": _path_rule_status(path),
            }
        )

    for path in [
        ROOT / "api" / "gen3_shadow.py",
        ROOT / "frontend" / "src" / "views" / "pages" / "Gen3Research.vue",
        ROOT / "frontend" / "src" / "views" / "pages" / "Gen3V3Backtest.vue",
        ROOT / "frontend" / "src" / "views" / "pages" / "Gen3V4ResearchBacktest.vue",
    ]:
        if path.exists():
            rows.append(
                {
                    "kind": "frontend_or_api",
                    "name": path.name,
                    "path": str(path),
                    "action": "hide_or_wrap",
                    "reason": "旧 G3 展示/接口入口；新主入口应切到 G3 State Alpha，旧页改为证据直达或隐藏。",
                    "path_rule_status": _path_rule_status(path) if path.suffix == ".py" else "n/a",
                }
            )

    reports_root = report_path()
    if reports_root.exists():
        for path in sorted(reports_root.glob("*gen3*")):
            if not path.is_dir():
                continue
            action, reason = _report_dir_action(path.name)
            rows.append(
                {
                    "kind": "report_dir",
                    "name": path.name,
                    "path": str(path),
                    "action": action,
                    "reason": reason,
                    "path_rule_status": "external_report_path",
                }
            )
    return rows


def _strategy_contract() -> dict[str, Any]:
    return {
        "strategy_id": STRATEGY_ID,
        "name": STRATEGY_NAME,
        "name_cn": STRATEGY_NAME_CN,
        "stage": "shadow_current",
        "generated_at": _now_text(),
        "replaces_display_names": [
            "G3 V3",
            "G3 V4",
            "score120",
            "route_execution_mandate",
            "g3_market_state_router_strategy_v1",
        ],
        "hard_guardrails": {
            "shadow_only": True,
            "observe_only": True,
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
            "real_order_integration": "disabled",
        },
        "contract_modes": [
            "research_backtest",
            "shadow_current",
            "formal_disabled",
            "risk_audit",
            "evidence_archive",
        ],
        "route_policy": [
            {
                "mode": "panic_repair",
                "label_cn": "恐慌修复",
                "role": "市场出现恐慌扩散时优先进入修复观察，不追强。",
                "status": "shadow_current",
            },
            {
                "mode": "institutional_mainwave",
                "label_cn": "机构主升",
                "role": "保留 2024-10 以后主升行情的赚钱模式，但必须通过近期已退出样本健康门控。",
                "regime_gate": "最近 240 天已退出 institutional_mainwave 样本 count>=2 且 avg_ret>0；big_loss_rate 只诊断。",
                "status": "shadow_current",
            },
            {
                "mode": "pre_2024_compatible",
                "label_cn": "2024-10 前兼容模式",
                "role": "等待另一个研究线程的最终结论接入；当前先以 old_g3_route_v3 作为兼容占位，不作为最终命名。",
                "status": "pending_research_merge",
            },
            {
                "mode": "risk_blocked_observation",
                "label_cn": "风险阻断观察",
                "role": "30m 未确认、数据缺口、低吸链路、失败退出未证明时只进入观察池。",
                "status": "blocked_observe_only",
            },
        ],
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _render_report(inventory: list[dict[str, Any]], contract: dict[str, Any]) -> str:
    counts: dict[str, int] = {}
    for row in inventory:
        counts[row["action"]] = counts.get(row["action"], 0) + 1

    def count(action: str) -> int:
        return counts.get(action, 0)

    return f"""# G3 第三代策略重建蓝图 v1

生成时间：{_now_text()}

## 一、定型结论

新 G3 推荐定名为 **{STRATEGY_NAME_CN}**，代码标识固定为 `{STRATEGY_ID}`。

命名理由：

- `State` 明确第一层是市场状态识别，不再把单一路线收益误当全周期策略。
- `Alpha` 表示这是赚钱模式选择器，而不是某个旧回测包、旧分数或旧执行压力测试。
- 保留 G3 品牌，但旧的 `G3 V3 / G3 V4 / score120 / route_execution` 只作为证据标签，不再作为产品入口名。

当前阶段结论：**shadow_current / observe_only**。即使当前候选存在，也只能进入影子观察，不允许真实自动下单。

## 二、产品形态

G3 State Alpha 应改成一个交易策略工作台，而不是研究页堆叠。主页面建议只有一个入口：

1. **状态总览**：最新交易日、决策日、市场状态、当前启用赚钱模式、阻断原因、数据新鲜度。
2. **当前候选**：只显示 shadow / observe 候选，明确 `formal_buy_signal=false`、`auto_order_allowed=false`。
3. **模式证据**：机构主升、恐慌修复、2024-10 前兼容模式分别展示近期样本、历史窗口、失败样本。
4. **风险退出**：失败退出、30m 未确认、数据缺口、低吸链路、成交可得性统一进入风险审计区。
5. **证据归档**：旧 V3/V4/score120/route_execution 页面从主导航隐藏，改为证据档案直达。

前端风格保持交易工作台：高信息密度、少装饰、状态标签清楚、表格和曲线优先，不做营销页。

## 三、策略形态

策略链路固定为：

`市场状态识别 -> 赚钱模式选择 -> 当前候选生成 -> 风险/失败退出审计 -> shadow 一致性观察 -> 证据归档`

首版路线：

- **panic_repair**：恐慌扩散或下跌修复期，优先防守修复，不追机构主升。
- **institutional_mainwave**：机构主升模式，使用当前实时适配器；最近 240 天已退出样本需 `count>=2 && avg_ret>0`，大亏率只诊断。
- **pre_2024_compatible**：2024-10 前兼容模式，等待独立研究线程结论；当前只允许以 `old_g3_route_v3` 占位，不能继续作为最终命名。
- **risk_blocked_observation**：30m 未确认、低吸链路、数据缺口、失败退出未证明时，只观察不交易。

## 四、接口合同

新接口主语义建议：

- `GET /api/gen3-state-alpha/contract`：策略合同、命名、阶段、硬风控开关。
- `GET /api/gen3-state-alpha/current`：读取现有 `gen3_state_router_shadow` runtime，包装为 G3 State Alpha 当前影子候选。
- `GET /api/gen3-state-alpha/evidence-inventory`：返回本报告生成的旧 G3 清理清单。
- 后续再补：`/backtest-summary`、`/risk-audit`、`/archive-index`。

所有响应必须固定返回：

- `strategy_id={STRATEGY_ID}`
- `stage=shadow_current` 或 `research_backtest`
- `formal_buy_signal=false`
- `auto_order_allowed=false`
- `order_path_enabled=false`

## 五、runtime 与报告路径

新路径约定：

- runtime：`{runtime_path("gen3_state_alpha")}`
- 蓝图报告：`{OUT_DIR}`
- 旧核心 current runtime 仍读取：`{runtime_path("gen3_state_router_shadow")}`
- 旧核心 report 仍读取：`{report_path("gen3_state_router_shadow_daily_v1")}`

禁止在仓库根目录新增 `data/`、`reports/`、`logs/`、`artifacts/`。

## 六、旧 G3 清理清单摘要

本次盘点条目数：{len(inventory)}

- 保留为当前核心：{count("keep_current_core")}
- 保留为影子观察：{count("keep_shadow_observation")}
- 证据归档并从主入口隐藏：{count("evidence_archive_hide")}
- 证据归档：{count("evidence_archive")}
- 迁移复核：{count("migration_review")}
- 路径修复后归档：{count("path_fix_then_archive")}
- 旧 API/前端包装或隐藏：{count("hide_or_wrap")}

完整清单见：`g3_cleanup_inventory.csv`。

## 七、保留、隐藏、归档、删除原则

**保留为核心**

- `gen3_state_router_shadow_daily_v1.py`
- `gen3_market_state_router_v1.py`
- `gen3_package_market_state_router_strategy_v1.py`
- `gen3_institutional_mainwave_current_v1.py`
- runtime `gen3_state_router_shadow`

**主导航隐藏但保留证据**

- 旧 `Gen3Research.vue`
- 旧 `Gen3V3Backtest.vue`
- 旧 `Gen3V4ResearchBacktest.vue`
- `route_execution_v3`
- `v4_strong_offense`
- `score120` 相关研究报告

**迁移到证据归档**

- `gen3_audit_*`
- `gen3_test_*`
- `gen3_stress_*`
- `gen3_backtest_*`
- `gen3_validate_*`
- 旧 V3/V4/强势/震荡/恐慌分支研究目录

**暂不建议删除**

- 任何 closed_trades、equity_curve、window_metrics、failure_attribution、REPORT_CN.md。
- 任何能解释“为什么旧 G3 不能全周期成立”的失败证据。

**可在二次确认后删除或迁移**

- 前端 public 下两个旧 fallback JSON，前提是新 API 已稳定返回同等证据数据。
- 仓库内仍使用 `ROOT / "reports"` 或 `ROOT / "data"` 的旧 G3 脚本，需先改为 `utils.paths` 或迁入研究归档，不能继续作为核心脚本。

## 八、实施计划

1. **骨架阶段**：新增 G3 State Alpha 合同接口和蓝图报告，不改旧产物。
2. **前端入口阶段**：新增 `/gen3/state-alpha` 页面；旧 V3/V4 导航隐藏，保留直达链接。
3. **runtime 阶段**：将 state-router shadow 输出复制/包装到 `runtime_path("gen3_state_alpha")`，统一字段名。
4. **研究合并阶段**：等待 2024-10 前盈利策略线程结论，把兼容模式接入 `pre_2024_compatible`，替换 `old_g3_route_v3` 占位名。
5. **风险审计阶段**：把失败退出、30m 确认、数据新鲜度、成交可得性做成固定 risk_audit 输出。
6. **归档阶段**：给旧目录生成索引，迁移到研究归档或从主导航隐藏；删除必须另列清单并确认。

## 九、当前最小代码骨架

本次只建议落地：

- `scripts/gen3_state_alpha_rebuild_blueprint_v1.py`
- `api/gen3_state_alpha.py`
- `GET /api/gen3-state-alpha/contract`
- `GET /api/gen3-state-alpha/current`
- `GET /api/gen3-state-alpha/evidence-inventory`

这一步的价值是先把产品名、策略合同和只读入口定住，避免继续在旧 G3 V3/V4/score120 命名里打转。
"""


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    inventory = _build_inventory()
    contract = _strategy_contract()

    _write_csv(OUT_DIR / "g3_cleanup_inventory.csv", inventory)
    (OUT_DIR / "g3_state_alpha_contract.json").write_text(
        json.dumps(contract, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    (RUNTIME_DIR / "latest_strategy_contract.json").write_text(
        json.dumps(contract, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    (OUT_DIR / "REPORT_CN.md").write_text(_render_report(inventory, contract), encoding="utf-8", newline="\n")

    summary = {
        "ok": True,
        "strategy_id": STRATEGY_ID,
        "name": STRATEGY_NAME_CN,
        "generated_at": _now_text(),
        "out_dir": str(OUT_DIR),
        "runtime_dir": str(RUNTIME_DIR),
        "inventory_rows": len(inventory),
        "contract_path": str(OUT_DIR / "g3_state_alpha_contract.json"),
        "report_path": str(OUT_DIR / "REPORT_CN.md"),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
