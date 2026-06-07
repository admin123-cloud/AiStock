from __future__ import annotations

import json
import importlib.util
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
GUARDED_MODULE = ROOT / "scripts" / "gen3_build_guarded_live_safe_payload_v1.py"
OUT_DIR = ROOT / "reports" / "gen3_struct_veto_combo_e_shadow_payload_v1"
RUNTIME_DIR = ROOT / "data" / "runtime" / "gen3_struct_veto_combo_e_shadow"
SELECTED_PATH = ROOT / "reports" / "gen3_struct_veto_combo_e_candidate_v1" / "struct_veto_combo_e_candidates.csv"

SCHEMA_VERSION = "g3_struct_veto_combo_e_shadow_payload_v1"
STRATEGY_ID = "g3_struct_veto_combo_e"


def load_guarded_module():
    spec = importlib.util.spec_from_file_location("gen3_build_guarded_live_safe_payload_v1", GUARDED_MODULE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {GUARDED_MODULE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_无数据_"
    return df.to_markdown(index=False)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    guarded = load_guarded_module()
    guarded.SELECTED_PATH = SELECTED_PATH
    guarded.OUT_DIR = OUT_DIR
    guarded.RUNTIME_DIR = RUNTIME_DIR

    selected = guarded._selected_keys()
    payload = guarded._finalize(
        [
            guarded._panic_payload(selected),
            guarded._range_payload(selected),
            guarded._strong_payload(selected),
        ]
    )
    if payload.empty:
        raise RuntimeError("empty combo_e shadow payload")

    payload["schema_version"] = SCHEMA_VERSION
    payload["strategy_id"] = STRATEGY_ID
    payload["mode"] = "shadow_only_observe"
    payload["shadow_action"] = "observe_only"
    payload["auto_order_allowed"] = False
    payload["formal_buy_signal"] = False
    payload["order_path_enabled"] = False
    payload["block_reason"] = payload["block_reason"].fillna("shadow_only_not_auto_ordered")
    payload.loc[payload["block_reason"].astype(str).str.len().eq(0), "block_reason"] = "shadow_only_not_auto_ordered"

    selected_raw = pd.read_csv(SELECTED_PATH, low_memory=False)
    selected_raw["entry_date"] = pd.to_datetime(selected_raw["entry_date"], errors="coerce").dt.normalize()
    selected_raw["code"] = selected_raw["code"].astype(str)
    selected_raw["_lineage"] = (
        selected_raw["code"]
        + "|"
        + selected_raw["entry_date"].dt.strftime("%Y-%m-%d")
        + "|"
        + selected_raw["route"].astype(str)
    )
    price_map = selected_raw.drop_duplicates("_lineage").set_index("_lineage")["entry_price"]
    missing_price = pd.to_numeric(payload["entry_price"], errors="coerce").isna()
    payload.loc[missing_price, "entry_price"] = payload.loc[missing_price, "source_lineage_key"].map(price_map)

    forbidden = guarded._forbidden_fields(list(payload.columns))
    if forbidden:
        raise RuntimeError(f"forbidden fields leaked into payload columns: {forbidden}")

    summary = guarded._summary(payload)
    selected_count = int(len(selected))
    payload_count = int(len(payload))
    route_summary = summary.to_dict(orient="records")
    missing_selected = selected_count - payload_count
    auto_rows = int(payload["auto_order_allowed"].fillna(False).sum())
    formal_rows = int(payload["formal_buy_signal"].fillna(False).sum())
    order_rows = int(payload["order_path_enabled"].fillna(False).sum())

    audit = pd.DataFrame(
        [
            {
                "payload_rows": payload_count,
                "selected_rows": selected_count,
                "missing_selected_rows": missing_selected,
                "forbidden_field_count": 0,
                "auto_order_allowed_rows": auto_rows,
                "formal_buy_signal_rows": formal_rows,
                "order_path_enabled_rows": order_rows,
                "verdict": "PASS" if missing_selected == 0 and auto_rows == 0 and formal_rows == 0 and order_rows == 0 else "FAIL",
            }
        ]
    )

    payload_path = OUT_DIR / "g3_struct_veto_combo_e_shadow_payload.csv"
    summary_path = OUT_DIR / "g3_struct_veto_combo_e_shadow_summary.csv"
    audit_path = OUT_DIR / "g3_struct_veto_combo_e_shadow_audit.csv"
    payload.to_csv(payload_path, index=False, encoding="utf-8-sig")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    audit.to_csv(audit_path, index=False, encoding="utf-8-sig")
    payload.to_csv(RUNTIME_DIR / "latest_payload.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(RUNTIME_DIR / "latest_summary.csv", index=False, encoding="utf-8-sig")

    meta = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "strategy_id": STRATEGY_ID,
        "schema_version": SCHEMA_VERSION,
        "payload_rows": payload_count,
        "selected_rows": selected_count,
        "missing_selected_rows": missing_selected,
        "routes": route_summary,
        "forbidden_field_count": 0,
        "auto_order_allowed_rows": auto_rows,
        "formal_buy_signal_rows": formal_rows,
        "order_path_enabled_rows": order_rows,
        "candidate_signal_window": "2020-01-01 to 2026-05-29",
        "actual_entry_window": "2020-01-03 to 2026-05-18",
        "mtm_settlement_window": "2020-01-03 to 2026-06-04",
        "live_status": "shadow_only_research_candidate_not_live",
        "verdict": "PASS_SHADOW_PAYLOAD_READY" if audit.loc[0, "verdict"] == "PASS" else "FAIL_PAYLOAD_AUDIT",
        "next_step": "wire_g3_v4_page_shadow_table_and_continue_new_range_or_30m_absorption_research",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (RUNTIME_DIR / "latest_summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report = "\n".join(
        [
            "# G3 struct_veto_combo_e 影子盘安全 payload",
            "",
            "## 范围",
            "- 策略版本：`struct_veto_combo_e`，中文含义是结构化否决版组合。",
            "- 回测候选信号/入场窗口：2020-01-01 至 2026-05-29。",
            "- 实际成交入场窗口：2020-01-03 至 2026-05-18。",
            "- MTM结算窗口：2020-01-03 至 2026-06-04。",
            "- 当前状态：只读影子盘，不是实盘自动交易。",
            "",
            "## 链路解释",
            "- `down_panic`：弱势恐慌买法，弱市/下跌后买恐慌出清和30m修复。",
            "- `range_gap`：横盘箱体买法，震荡/弱反弹里买箱体低位或跳空修复。",
            "- `strong_main`：强势主线买法，吸收G2 volume5，在强势周期里买二次确认。",
            "",
            "## 路由汇总",
            md_table(summary),
            "",
            "## 安全审计",
            md_table(audit),
            "",
            "## 结论",
            "- payload 已生成，且没有收益、退出、持仓金额等研究字段泄漏。",
            "- `auto_order_allowed`、`formal_buy_signal`、`order_path_enabled` 全部为 0。",
            "- 下一步可以接到 G3 V4 页面做只读展示，同时继续研究新的横盘/箱体底部独立候选源或30m真实放量承接源。",
        ]
    )
    (OUT_DIR / "report_cn.md").write_text(report + "\n", encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
