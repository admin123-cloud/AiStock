# -*- coding: utf-8 -*-
"""G3 Phase 2 现有 Agent / 定时工作流 CLI 闭环工具。

用法示例：
  1. 导出当日主升浪候选 + 大盘闸门上下文（供 AI Agent 联网做公告排雷与催化分析）：
     python scripts/g3_phase2_workflow.py --task export-candidates

  2. 将 AI Agent 生成的结构化排雷与催化 JSON 校验落盘到 runtime/g3_ai_screening/：
     python scripts/g3_phase2_workflow.py --task ingest-screening --file screening_input.json

  3. 生成当日 09:15 盘前市场主线简报（可选合并 Agent 隔夜研判 JSON、可选推送微信）：
     python scripts/g3_phase2_workflow.py --task build-brief [--overnight-file overnight.json] [--push]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.g3.desk import get_g3_market_gate
from services.g3.ai_screening import merge_screening_into_mainwave, save_agent_screening
from services.g3.mainwave_scanner import scan_mainwave_daily
from services.g3.premarket_brief import generate_premarket_brief, push_premarket_brief_wechat


def cmd_export_candidates(out_file: str | None) -> int:
    gate = get_g3_market_gate()
    mw = scan_mainwave_daily(force_refresh=True)
    merged = merge_screening_into_mainwave(mw, market_gate=gate)
    payload = {
        "decision_date": merged.get("decision_date"),
        "entry_date": merged.get("entry_date"),
        "market_gate": {
            "regime": gate.get("regime"),
            "regime_label": gate.get("regime_label"),
            "allow_new_mainwave": gate.get("allow_new_mainwave"),
            "target_position_cap_pct": gate.get("target_position_cap_pct"),
            "reasons": gate.get("reasons"),
        },
        "candidates": [
            {
                "code": c.get("code"),
                "name": c.get("name"),
                "industry": c.get("industry"),
                "score": c.get("score"),
                "resonance": c.get("resonance"),
                "stage": c.get("stage"),
                "baseline_light": (c.get("ai_screening") or {}).get("risk_light"),
                "baseline_flags": (c.get("ai_screening") or {}).get("risk_flags"),
            }
            for c in (merged.get("candidates") or [])
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if out_file:
        Path(out_file).write_text(text, encoding="utf-8")
        print(f"已导出 {len(payload['candidates'])} 只主升候选至 {out_file}")
    else:
        print(text)
    return 0


def cmd_ingest_screening(input_file: str, agent_name: str) -> int:
    data = json.loads(Path(input_file).read_text(encoding="utf-8"))
    decision_date = data.get("decision_date") or ""
    items = data.get("items") or []
    summary_note = data.get("summary_note")
    doc = save_agent_screening(
        decision_date=decision_date,
        items=items,
        summary_note=summary_note,
        agent_name=agent_name,
    )
    print(f"已落盘 AI 排雷与催化结果：date={doc['decision_date']}，覆盖 {len(doc['items_by_code'])} 只候选股")
    return 0


def cmd_build_brief(overnight_file: str | None, push: bool) -> int:
    gate = get_g3_market_gate()
    mw = scan_mainwave_daily(force_refresh=False)
    merged = merge_screening_into_mainwave(mw, market_gate=gate)

    overnight_notes = None
    agent_thesis = None
    source = "auto_baseline"
    if overnight_file:
        ov = json.loads(Path(overnight_file).read_text(encoding="utf-8"))
        overnight_notes = ov.get("overnight_notes")
        agent_thesis = ov.get("agent_thesis")
        source = str(ov.get("source") or "agent_enriched")

    brief = generate_premarket_brief(
        market_gate=gate,
        mainwave_payload=merged,
        overnight_notes=overnight_notes,
        agent_thesis=agent_thesis,
        source=source,
    )
    print(brief["markdown_text"])
    if push:
        res = push_premarket_brief_wechat(brief)
        print(f"\n微信推送结果：ok={res['ok']} channel={res['channel']} detail={res['detail']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="G3 Phase 2 Agent 工作流工具")
    parser.add_argument("--task", required=True, choices=["export-candidates", "ingest-screening", "build-brief"])
    parser.add_argument("--file", help="export 输出路径或 ingest 输入 JSON 路径")
    parser.add_argument("--overnight-file", help="build-brief 可选隔夜 Agent 研判 JSON")
    parser.add_argument("--agent-name", default="antigravity_agent")
    parser.add_argument("--push", action="store_true", help="生成简报后推送微信")
    args = parser.parse_args()

    if args.task == "export-candidates":
        return cmd_export_candidates(args.file)
    if args.task == "ingest-screening":
        if not args.file:
            raise SystemExit("ingest-screening 必须指定 --file")
        return cmd_ingest_screening(args.file, args.agent_name)
    return cmd_build_brief(args.overnight_file, args.push)


if __name__ == "__main__":
    raise SystemExit(main())
