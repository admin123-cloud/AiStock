from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path  # noqa: E402


FORMAL_DIR = report_path("g3_formal_unified_five_strategy_contract_v1")
OUT_DIR = report_path("g3_natural_policy_shadow_v1")

SELECTED_PATH = FORMAL_DIR / "formal_unified_selected_candidates.csv"
CLOSED_PATH = FORMAL_DIR / "formal_unified_closed_trades.csv"

BASE_POSITION_PCT = 0.50
G2_SUPPLEMENT_DEFAULT_PCT = 0.25


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(str(path))
    return pd.read_csv(path, encoding="utf-8-sig", low_memory=False)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
    except Exception:
        return default
    return x if math.isfinite(x) else default


def _pct(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return "--"
    if not math.isfinite(x):
        return "--"
    return f"{x:.1%}"


def _money(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return "--"
    if not math.isfinite(x):
        return "--"
    return f"{x:,.0f}"


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, money_cols: set[str] | None = None, max_rows: int = 60) -> str:
    if df.empty:
        return "_无数据_"
    d = df.head(max_rows).copy()
    for col in pct_cols or set():
        if col in d.columns:
            d[col] = d[col].map(_pct)
    for col in money_cols or set():
        if col in d.columns:
            d[col] = d[col].map(_money)
    return d.to_markdown(index=False)


def _prepare_dates(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in ["entry_date", "exit_date", "policy_exit_date", "context_date", "decision_date"]:
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], errors="coerce").dt.normalize()
    for col in [
        "net_ret",
        "realized_pnl",
        "score",
        "raw_score",
        "wave_style_score",
        "sector_diffusion_score",
        "mom20",
        "mom60",
        "up_rate",
        "big_down_rate",
        "limit_down_proxy_rate",
        "position_pct",
    ]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def _context_tag(row: pd.Series) -> str:
    strategy = str(row.get("trade_strategy") or "")
    style = str(row.get("market_style") or "")
    route = str(row.get("route") or "")
    down_risk = bool(row.get("down_risk")) if "down_risk" in row else False
    if strategy == "institutional_score120_mainwave":
        if style == "standard_uptrend":
            return "market_aligned_mainwave"
        return "stock_leads_market_mainwave"
    if strategy == "old_g3_strong_breakout":
        if style == "standard_uptrend":
            return "market_aligned_breakout"
        return "stock_leads_market_breakout"
    if strategy in {"range_weak_repair", "panic_capitulation_repair"}:
        if down_risk or style == "standard_downtrend":
            return "repair_against_downrisk"
        if style == "standard_range":
            return "range_repair"
        return "weak_rebound_repair"
    if strategy == "volume_runup_supplement" or route == "g2_gap_supplement":
        return "g2_gap_supplement_rotation"
    return "unclassified"


def _g2_can_promote_to_full(row: pd.Series) -> bool:
    style = str(row.get("market_style") or "")
    mom60 = _safe_float(row.get("mom60"))
    up_rate = _safe_float(row.get("up_rate"))
    big_down = _safe_float(row.get("big_down_rate"))
    return bool(style == "standard_range" and mom60 <= 0.10 and 0.25 <= up_rate <= 0.70 and big_down <= 0.08)


def _candidate_labels(selected: pd.DataFrame) -> pd.DataFrame:
    selected = selected.sort_values(["entry_date", "rank_key", "score"], ascending=[True, False, False]).copy()
    open_positions: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for row in selected.to_dict("records"):
        day = pd.Timestamp(row.get("entry_date"))
        open_positions = [
            pos for pos in open_positions if pd.notna(pos.get("policy_exit_date")) and pd.Timestamp(pos["policy_exit_date"]) >= day
        ]
        code = str(row.get("code") or "")
        strategy = str(row.get("trade_strategy") or "")
        route = str(row.get("route") or "")
        already_open_same_stock = any(str(pos.get("code") or "") == code for pos in open_positions)
        action = "allow"
        action_label = "允许"
        natural_position_pct = BASE_POSITION_PCT
        rule_hits: list[str] = []
        reason_parts: list[str] = []

        if already_open_same_stock:
            action = "skip"
            action_label = "跳过"
            natural_position_pct = 0.0
            rule_hits.append("N1")
            reason_parts.append("同一只股票已有未退出仓位；当前合同没有显式加仓语义")
        elif strategy == "volume_runup_supplement" or route == "g2_gap_supplement":
            rule_hits.append("N3")
            if _g2_can_promote_to_full(pd.Series(row)):
                action = "allow_reduced"
                action_label = "允许但保守"
                natural_position_pct = G2_SUPPLEMENT_DEFAULT_PCT
                reason_parts.append("G2 空档补位处于较自然的轮动环境，但仍按补位角色半槽处理")
            else:
                action = "allow_reduced"
                action_label = "允许但降仓"
                natural_position_pct = G2_SUPPLEMENT_DEFAULT_PCT
                reason_parts.append("G2 空档补位不与 G3 主路由争同等风险预算")

        context_tag = _context_tag(pd.Series(row))
        if context_tag in {"stock_leads_market_mainwave", "stock_leads_market_breakout"}:
            rule_hits.append("N4")
            reason_parts.append("个股领先市场状态，需要页面解释为个股主升而非市场共振")
        elif context_tag in {"repair_against_downrisk", "range_repair", "weak_rebound_repair"}:
            rule_hits.append("N4")
            reason_parts.append("修复类交易需要解释市场压力与修复证据")

        out = row.copy()
        out["natural_action"] = action
        out["natural_action_label"] = action_label
        out["natural_position_pct"] = natural_position_pct
        out["natural_rule_hits"] = ",".join(dict.fromkeys(rule_hits))
        out["natural_context_tag"] = context_tag
        out["natural_reason"] = "；".join(reason_parts) if reason_parts else "符合当前自然交易合同"
        rows.append(out)

        if action != "skip" and pd.notna(row.get("policy_exit_date")):
            open_positions.append(row)
    return pd.DataFrame(rows)


def _closed_actions(closed: pd.DataFrame, candidate_labels: pd.DataFrame) -> pd.DataFrame:
    labels = candidate_labels[
        [
            c
            for c in [
                "trade_key",
                "natural_action",
                "natural_action_label",
                "natural_position_pct",
                "natural_rule_hits",
                "natural_context_tag",
                "natural_reason",
            ]
            if c in candidate_labels.columns
        ]
    ].drop_duplicates("trade_key", keep="first")
    out = closed.merge(labels, on="trade_key", how="left")
    out["hold_days"] = (pd.to_datetime(out["exit_date"], errors="coerce") - pd.to_datetime(out["entry_date"], errors="coerce")).dt.days
    out["review_tags"] = out.get("natural_rule_hits", "").fillna("").astype(str)
    out["exit_shadow_action"] = "none"
    out["exit_shadow_label"] = "无"
    out["exit_shadow_reason"] = ""

    exit_reason = out.get("exit_reason", pd.Series(dtype=str)).fillna("").astype(str)
    net_ret = pd.to_numeric(out.get("net_ret"), errors="coerce").fillna(0.0)
    hold_days = pd.to_numeric(out.get("hold_days"), errors="coerce").fillna(999)

    passive = exit_reason.eq("policy_exit_remaining")
    quick_fail = (hold_days <= 3) & (net_ret <= -0.03)
    out.loc[passive & (net_ret <= 0.02), "exit_shadow_action"] = "early_exit_review"
    out.loc[passive & (net_ret <= 0.02), "exit_shadow_label"] = "提前退出复核"
    out.loc[passive & (net_ret <= 0.02), "exit_shadow_reason"] = "未明显盈利且最终被动到期，剩余仓需要回答继续持有证据"
    out.loc[quick_fail, "exit_shadow_action"] = "buy_point_review"
    out.loc[quick_fail, "exit_shadow_label"] = "买点体检"
    out.loc[quick_fail, "exit_shadow_reason"] = "开仓后三日内快速失败，优先复核追高、30m确认、板块退潮和补位仓位"

    out["shadow_pnl_scale"] = 1.0
    out.loc[out["natural_action"].eq("skip"), "shadow_pnl_scale"] = 0.0
    g2_reduced = (out.get("trade_strategy", pd.Series(dtype=str)).fillna("").astype(str).eq("volume_runup_supplement")) & out[
        "natural_action"
    ].fillna("").astype(str).eq("allow_reduced")
    out.loc[g2_reduced, "shadow_pnl_scale"] = G2_SUPPLEMENT_DEFAULT_PCT / BASE_POSITION_PCT
    out["shadow_realized_pnl"] = pd.to_numeric(out.get("realized_pnl"), errors="coerce").fillna(0.0) * out["shadow_pnl_scale"]
    return out


def _summary_by_action(closed_actions: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for keys, part in closed_actions.groupby(["natural_action", "natural_action_label"], dropna=False):
        action, label = keys
        ret = pd.to_numeric(part.get("net_ret"), errors="coerce").dropna()
        rows.append(
            {
                "natural_action": action,
                "natural_action_label": label,
                "trade_count": int(len(part)),
                "win_rate": float((ret > 0).mean()) if len(ret) else None,
                "avg_ret": float(ret.mean()) if len(ret) else None,
                "original_pnl": float(pd.to_numeric(part.get("realized_pnl"), errors="coerce").sum()),
                "shadow_pnl": float(pd.to_numeric(part.get("shadow_realized_pnl"), errors="coerce").sum()),
                "pnl_delta": float(
                    pd.to_numeric(part.get("shadow_realized_pnl"), errors="coerce").sum()
                    - pd.to_numeric(part.get("realized_pnl"), errors="coerce").sum()
                ),
            }
        )
    return pd.DataFrame(rows).sort_values(["natural_action", "trade_count"], ascending=[True, False])


def _summary_by_exit_action(closed_actions: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for keys, part in closed_actions.groupby(["exit_shadow_action", "exit_shadow_label"], dropna=False):
        action, label = keys
        ret = pd.to_numeric(part.get("net_ret"), errors="coerce").dropna()
        rows.append(
            {
                "exit_shadow_action": action,
                "exit_shadow_label": label,
                "trade_count": int(len(part)),
                "win_rate": float((ret > 0).mean()) if len(ret) else None,
                "avg_ret": float(ret.mean()) if len(ret) else None,
                "original_pnl": float(pd.to_numeric(part.get("realized_pnl"), errors="coerce").sum()),
            }
        )
    return pd.DataFrame(rows).sort_values(["trade_count"], ascending=False)


def _contract() -> dict[str, Any]:
    return {
        "version": "g3_natural_policy_shadow_v1",
        "status": "shadow_only",
        "base_contract": "g3_final_with_g2_gap_supplement",
        "principle": "不为了历史收益硬调参，优先让交易行为自然、可解释、可执行。",
        "rules": [
            {
                "id": "N1",
                "name": "same_stock_open_position_guard",
                "stage": "entry",
                "shadow_action": "skip",
                "future_promotion": "若 30 个交易日纸面盘验证通过，可升级为硬约束；显式加仓合同另行设计。",
            },
            {
                "id": "N2",
                "name": "stateful_remaining_position_exit",
                "stage": "exit",
                "shadow_action": "early_exit_review",
                "future_promotion": "先作为卖出建议，不直接替代原生 30m 退出；验证后接入工作台退出管理。",
            },
            {
                "id": "N3",
                "name": "g2_gap_supplement_half_slot",
                "stage": "position",
                "shadow_action": "allow_reduced",
                "position_pct": G2_SUPPLEMENT_DEFAULT_PCT,
                "future_promotion": "若纸面盘显示补位半槽更符合执行体验，可作为默认仓位缩放。",
            },
            {
                "id": "N4",
                "name": "market_context_explanation_tag",
                "stage": "explain",
                "shadow_action": "tag_only",
                "future_promotion": "页面和复盘优先展示，不作为硬 gate。",
            },
            {
                "id": "N5",
                "name": "quick_failure_buy_point_review",
                "stage": "review",
                "shadow_action": "review_only",
                "future_promotion": "累积足够失败样本后再反推买点质量规则。",
            },
        ],
    }


def run() -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    selected = _prepare_dates(_read_csv(SELECTED_PATH))
    closed = _prepare_dates(_read_csv(CLOSED_PATH))

    candidate_labels = _candidate_labels(selected)
    closed_actions = _closed_actions(closed, candidate_labels)
    action_summary = _summary_by_action(closed_actions)
    exit_action_summary = _summary_by_exit_action(closed_actions)
    contract = _contract()

    candidate_labels.to_csv(OUT_DIR / "candidate_shadow_labels.csv", index=False, encoding="utf-8-sig")
    closed_actions.to_csv(OUT_DIR / "closed_trade_shadow_actions.csv", index=False, encoding="utf-8-sig")
    action_summary.to_csv(OUT_DIR / "entry_position_shadow_summary.csv", index=False, encoding="utf-8-sig")
    exit_action_summary.to_csv(OUT_DIR / "exit_shadow_summary.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "natural_policy_contract.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")

    original_pnl = float(pd.to_numeric(closed_actions.get("realized_pnl"), errors="coerce").sum())
    shadow_pnl = float(pd.to_numeric(closed_actions.get("shadow_realized_pnl"), errors="coerce").sum())
    skip_count = int(candidate_labels.get("natural_action", pd.Series(dtype=str)).fillna("").astype(str).eq("skip").sum())
    reduced_count = int(candidate_labels.get("natural_action", pd.Series(dtype=str)).fillna("").astype(str).eq("allow_reduced").sum())
    early_exit_review_count = int(
        closed_actions.get("exit_shadow_action", pd.Series(dtype=str)).fillna("").astype(str).eq("early_exit_review").sum()
    )
    buy_point_review_count = int(
        closed_actions.get("exit_shadow_action", pd.Series(dtype=str)).fillna("").astype(str).eq("buy_point_review").sum()
    )
    meta = {
        "version": "g3_natural_policy_shadow_v1",
        "status": "shadow_only",
        "candidate_count": int(len(candidate_labels)),
        "closed_trade_count": int(len(closed_actions)),
        "skip_count": skip_count,
        "reduced_position_count": reduced_count,
        "early_exit_review_count": early_exit_review_count,
        "buy_point_review_count": buy_point_review_count,
        "original_pnl": original_pnl,
        "shadow_scaled_pnl": shadow_pnl,
        "shadow_pnl_delta": shadow_pnl - original_pnl,
        "output_dir": str(OUT_DIR),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report = [
        "# G3 自然交易影子合同 v1",
        "",
        "## 结论",
        "",
        "这不是正式收益优化版本，而是一层影子交易纪律：它保留 G3 最终版作为主合同，只在候选和成交上标记更自然的交易动作。",
        "",
        "本版本建议先进入 30 个交易日纸面盘观察，不直接改正式下单。原因很简单：规则 N1/N2/N3 都更符合真实交易，但仍需要验证它们不会误伤 G3 的进攻能力。",
        "",
        "## 影子结果",
        "",
        f"- 候选数：{meta['candidate_count']}",
        f"- 闭合交易数：{meta['closed_trade_count']}",
        f"- N1 同票持仓跳过：{skip_count}",
        f"- N3 补位降仓：{reduced_count}",
        f"- N2 提前退出复核：{early_exit_review_count}",
        f"- N5 买点体检：{buy_point_review_count}",
        f"- 原始历史 PnL：{_money(original_pnl)}",
        f"- 仅按 N1/N3 缩放后的影子 PnL：{_money(shadow_pnl)}",
        f"- 影子 PnL 差额：{_money(shadow_pnl - original_pnl)}",
        "",
        "注意：影子 PnL 只是风险预算缩放估算，不代表正式回测收益。这里的目标是观察行为是否更自然，而不是挑一个更好看的数字。",
        "",
        "## 入场和仓位动作",
        "",
        _md_table(action_summary, {"win_rate", "avg_ret"}, {"original_pnl", "shadow_pnl", "pnl_delta"}),
        "",
        "## 卖出复核动作",
        "",
        _md_table(exit_action_summary, {"win_rate", "avg_ret"}, {"original_pnl"}),
        "",
        "## 合同草案",
        "",
        "```json",
        json.dumps(contract, ensure_ascii=False, indent=2),
        "```",
        "",
        "## 落地建议",
        "",
        "1. 先把 `candidate_shadow_labels.csv` 接入 G3 逐日复盘页，作为影子标签展示。",
        "2. 把 `closed_trade_shadow_actions.csv` 接入历史成交页，标记同票重叠、补位降仓、提前退出复核和买点体检。",
        "3. 工作台今日买入只展示影子建议，不自动拦单；等 30 个交易日纸面盘验证后，再决定 N1 是否硬约束、N3 是否默认仓位缩放。",
        "4. N2 先进入退出管理，不直接替代原生 30m 卖出合同。",
        "",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(report), encoding="utf-8")
    return meta


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
