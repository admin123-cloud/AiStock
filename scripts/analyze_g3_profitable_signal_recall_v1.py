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

import api.gen3_state_alpha as g3  # noqa: E402
from scripts.gen3_build_four_path_candidates import _load_trade_dates  # noqa: E402
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("g3_profitable_signal_recall_v1")
FRESH_BACKTEST_DIR = report_path("g3_five_strategies_from_scratch_v1")


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


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, money_cols: set[str] | None = None, max_rows: int = 30) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    money_cols = money_cols or set()
    d = df.head(max_rows).copy()
    for col in pct_cols:
        if col in d.columns:
            d[col] = d[col].map(_pct)
    for col in money_cols:
        if col in d.columns:
            d[col] = d[col].map(_money)
    return d.to_markdown(index=False)


def _norm_code(s: pd.Series) -> pd.Series:
    return s.fillna("").astype(str).str.strip().str.upper()


def _read_historical_profitable() -> pd.DataFrame:
    path = g3.HISTORICAL_TRADES_PATH
    if not path.exists():
        raise FileNotFoundError(path)
    df = pd.read_csv(path, low_memory=False)
    df = g3._normalize_latest_g3_closed_trades(df)
    df = g3._with_route_strategy_fields(df)
    df["code"] = _norm_code(df["code"])
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce")
    df["net_ret"] = pd.to_numeric(df.get("net_ret"), errors="coerce")
    df["realized_pnl"] = pd.to_numeric(df.get("realized_pnl"), errors="coerce")
    df = df[df["entry_date"].notna()].copy()
    profitable = df[df["net_ret"] > 0].copy()
    profitable["entry_date"] = profitable["entry_date"].dt.date.astype(str)
    profitable["native_source_label"] = profitable.get("route_strategy_label", "").fillna("").astype(str)
    profitable["native_route"] = profitable.get("route_strategy", "").fillna("").astype(str)
    profitable["trade_strategy_label"] = profitable.get("trade_strategy_label", "").fillna("").astype(str)
    profitable["trade_strategy"] = profitable.get("trade_strategy", "").fillna("").astype(str)
    return profitable


def _read_candidates(name: str) -> pd.DataFrame:
    path = FRESH_BACKTEST_DIR / name
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, low_memory=False)
    if df.empty:
        return df
    df["code"] = _norm_code(df["code"])
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce")
    df = df[df["entry_date"].notna()].copy()
    df["entry_date"] = df["entry_date"].dt.date.astype(str)
    for col in ["trade_strategy", "trade_strategy_label", "source_strategy_label"]:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str)
    return df


def _trade_date_index(dates: pd.Series) -> dict[str, int]:
    start = pd.to_datetime(dates.min()) - pd.Timedelta(days=10)
    end = pd.to_datetime(dates.max()) + pd.Timedelta(days=10)
    try:
        trade_dates = _load_trade_dates(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    except Exception:
        trade_dates = pd.date_range(start, end, freq="B").strftime("%Y-%m-%d").tolist()
    return {str(d): i for i, d in enumerate(trade_dates)}


def _date_distance(a: str, b: str, date_to_idx: dict[str, int]) -> float:
    if a in date_to_idx and b in date_to_idx:
        return abs(date_to_idx[a] - date_to_idx[b])
    da = pd.to_datetime(a, errors="coerce")
    db = pd.to_datetime(b, errors="coerce")
    if pd.isna(da) or pd.isna(db):
        return math.inf
    return abs((da - db).days)


def _nearest_match(row: pd.Series, by_code: dict[str, pd.DataFrame], date_to_idx: dict[str, int], require_same_strategy: bool = True) -> dict[str, Any]:
    code = str(row["code"])
    entry_date = str(row["entry_date"])
    pool = by_code.get(code)
    if pool is None or pool.empty:
        return {
            "hit_exact": False,
            "hit_1d": False,
            "hit_3d": False,
            "nearest_distance": math.inf,
            "nearest_entry_date": "",
            "nearest_trade_strategy_label": "",
            "nearest_source_strategy_label": "",
        }
    if require_same_strategy and "trade_strategy" in pool.columns and "trade_strategy" in row.index:
        wanted = str(row.get("trade_strategy") or "")
        same = pool[pool["trade_strategy"].astype(str).eq(wanted)].copy()
        if not same.empty:
            pool = same
        else:
            return {
                "hit_exact": False,
                "hit_1d": False,
                "hit_3d": False,
                "nearest_distance": math.inf,
                "nearest_entry_date": "",
                "nearest_trade_strategy_label": "",
                "nearest_source_strategy_label": "",
            }
    distances = pool["entry_date"].map(lambda d: _date_distance(entry_date, str(d), date_to_idx))
    pos = distances.idxmin()
    nearest = pool.loc[pos]
    dist = float(distances.loc[pos])
    return {
        "hit_exact": bool(dist == 0),
        "hit_1d": bool(dist <= 1),
        "hit_3d": bool(dist <= 3),
        "nearest_distance": dist,
        "nearest_entry_date": str(nearest.get("entry_date", "")),
        "nearest_trade_strategy_label": str(nearest.get("trade_strategy_label", "")),
        "nearest_source_strategy_label": str(nearest.get("source_strategy_label", "")),
    }


def _attach_recall(
    profitable: pd.DataFrame,
    candidates: pd.DataFrame,
    prefix: str,
    date_to_idx: dict[str, int],
    require_same_strategy: bool = True,
) -> pd.DataFrame:
    if candidates.empty:
        out = profitable.copy()
        for col in ["hit_exact", "hit_1d", "hit_3d"]:
            out[f"{prefix}_{col}"] = False
        out[f"{prefix}_nearest_distance"] = math.inf
        out[f"{prefix}_nearest_entry_date"] = ""
        out[f"{prefix}_nearest_trade_strategy_label"] = ""
        out[f"{prefix}_nearest_source_strategy_label"] = ""
        return out
    by_code = {code: group.reset_index(drop=True) for code, group in candidates.groupby("code", dropna=False)}
    records = []
    for _, row in profitable.iterrows():
        records.append(_nearest_match(row, by_code, date_to_idx, require_same_strategy=require_same_strategy))
    recall = pd.DataFrame(records, index=profitable.index).add_prefix(f"{prefix}_")
    return pd.concat([profitable, recall], axis=1)


def _summary_row(df: pd.DataFrame, label: str) -> dict[str, Any]:
    n = len(df)
    realized = pd.to_numeric(df.get("realized_pnl"), errors="coerce").fillna(0.0)
    net_ret = pd.to_numeric(df.get("net_ret"), errors="coerce")
    row = {
        "group": label,
        "profitable_trades": n,
        "realized_pnl": realized.sum(),
        "avg_net_ret": net_ret.mean() if n else 0.0,
    }
    for prefix in ["candidate", "selected"]:
        for win, col in [("exact", "hit_exact"), ("within_1_trade_day", "hit_1d"), ("within_3_trade_days", "hit_3d")]:
            key = f"{prefix}_{col}"
            row[f"{prefix}_{win}_count"] = int(df[key].sum()) if key in df.columns else 0
            row[f"{prefix}_{win}_rate"] = (float(df[key].mean()) if n and key in df.columns else 0.0)
    return row


def _group_summary(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    rows = []
    for keys, gdf in df.groupby(group_cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        row = {col: key for col, key in zip(group_cols, keys)}
        row.update(_summary_row(gdf, "group"))
        rows.append(row)
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values(["realized_pnl", "profitable_trades"], ascending=[False, False]).reset_index(drop=True)


def _miss_bucket(row: pd.Series) -> str:
    if bool(row.get("selected_hit_3d", False)):
        return "已进入最终选票窗口"
    if bool(row.get("candidate_hit_3d", False)):
        return "候选有召回但路由未选中"
    return "候选源未召回"


def _write_report(
    profitable: pd.DataFrame,
    summary: pd.DataFrame,
    by_strategy: pd.DataFrame,
    by_native: pd.DataFrame,
    missed: pd.DataFrame,
) -> None:
    top_missed = missed.sort_values(["realized_pnl", "net_ret"], ascending=[False, False]).head(20)
    text = f"""# G3 五策略收益还原召回审计

## 结论

这次审计把历史 G3 最终版里的盈利成交作为基准，只检查新的“从零日线五策略候选”是否在相同股票、相近交易日重新找到机会。它不把历史成交当作回测输入，因此结论可以用来判断：收益跑丢主要是信号源丢失，还是候选已出现但路由没有选中。

核心判断：减少策略本身不能自动还原收益。只有在五个统一策略主体下面保留原生信号源、30m确认、Score120/主线扩散、G2补位等原始买点生成逻辑，收益才有可能还原。当前日线代理版本更像“重写了一套弱化策略”，不是“把原策略统一归类”。

## 总体召回

{_md_table(summary, pct_cols={"avg_net_ret", "candidate_exact_rate", "candidate_within_1_trade_day_rate", "candidate_within_3_trade_days_rate", "selected_exact_rate", "selected_within_1_trade_day_rate", "selected_within_3_trade_days_rate"}, money_cols={"realized_pnl"})}

## 按统一交易策略

{_md_table(by_strategy, pct_cols={"avg_net_ret", "candidate_exact_rate", "candidate_within_1_trade_day_rate", "candidate_within_3_trade_days_rate", "selected_exact_rate", "selected_within_1_trade_day_rate", "selected_within_3_trade_days_rate"}, money_cols={"realized_pnl"})}

## 按原生来源

{_md_table(by_native, pct_cols={"avg_net_ret", "candidate_exact_rate", "candidate_within_1_trade_day_rate", "candidate_within_3_trade_days_rate", "selected_exact_rate", "selected_within_1_trade_day_rate", "selected_within_3_trade_days_rate"}, money_cols={"realized_pnl"})}

## 最大漏召回盈利样本

{_md_table(top_missed[["code", "name", "entry_date", "trade_strategy_label", "native_source_label", "net_ret", "realized_pnl", "miss_bucket", "candidate_nearest_distance", "candidate_nearest_entry_date", "selected_nearest_distance", "selected_nearest_entry_date"]], pct_cols={"net_ret"}, money_cols={"realized_pnl"})}

## 原因拆解

1. 如果“候选源未召回”占主导，说明五策略从零重跑时没有复现原生买点生成器，不能靠改名或简单阈值恢复。
2. 如果“候选有召回但路由未选中”占主导，说明买点还在，但策略优先级、每日两槽、板块暴露或打分排序把赚钱票挤掉了。
3. 如果修复类策略召回少且新回测亏损大，说明合并后的修复策略边界过宽，缺少旧 G3/30m 的结构确认与退出顺序。
4. 如果进攻类策略召回少，说明 `机构主升Score120` 与 `强势突破` 不能用普通日线动量代理替代，必须恢复 Score120、主线扩散、30m接受度、强势突破各自的原生过滤器。

## 下一步

保留 5 个正式交易策略主体，但把原生信号源作为子来源挂回去：

- `机构主升Score120`：恢复 Score120 核心、主线扩散、30m overlay。
- `强势突破`：恢复旧 G3 强势突破的突破/接受度逻辑，不并入 Score120。
- `恐慌出清修复`：合并旧 G3 恐慌修复与下跌恐慌修复，但保留下跌压力分层和 30m 修复确认。
- `震荡弱势修复`：合并旧 G3 弱势/震荡修复与震荡恐慌修复，但必须收紧结构止损和弱势过滤。
- `量能续强补位`：恢复 G2 v2 / Alpha191 volume5 keep80 的原生补位信号，只在 G3 未占满槽位时补位。
"""
    (OUT_DIR / "REPORT_CN.md").write_text(text, encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    profitable = _read_historical_profitable()
    candidates = _read_candidates("all_strategy_candidates.csv")
    selected = _read_candidates("selected_daily_candidates.csv")
    if profitable.empty:
        raise RuntimeError("No profitable historical trades found.")
    date_to_idx = _trade_date_index(profitable["entry_date"])
    recalled = _attach_recall(profitable, candidates, "candidate", date_to_idx)
    recalled = _attach_recall(recalled, selected, "selected", date_to_idx)
    recalled["miss_bucket"] = recalled.apply(_miss_bucket, axis=1)

    summary = pd.DataFrame([_summary_row(recalled, "overall")])
    by_strategy = _group_summary(recalled, ["trade_strategy", "trade_strategy_label"])
    by_native = _group_summary(recalled, ["trade_strategy_label", "native_route", "native_source_label"])
    by_bucket = _group_summary(recalled, ["miss_bucket"])
    missed = recalled[~recalled["selected_hit_3d"].astype(bool)].copy()

    recalled.to_csv(OUT_DIR / "profitable_trade_recall_detail.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "profitable_recall_summary.csv", index=False, encoding="utf-8-sig")
    by_strategy.to_csv(OUT_DIR / "profitable_recall_by_strategy.csv", index=False, encoding="utf-8-sig")
    by_native.to_csv(OUT_DIR / "profitable_recall_by_native_source.csv", index=False, encoding="utf-8-sig")
    by_bucket.to_csv(OUT_DIR / "profitable_recall_by_miss_bucket.csv", index=False, encoding="utf-8-sig")
    missed.to_csv(OUT_DIR / "missed_profitable_trades.csv", index=False, encoding="utf-8-sig")

    summary_json = {
        "historical_path": str(g3.HISTORICAL_TRADES_PATH),
        "fresh_candidates_path": str(FRESH_BACKTEST_DIR / "all_strategy_candidates.csv"),
        "fresh_selected_path": str(FRESH_BACKTEST_DIR / "selected_daily_candidates.csv"),
        "profitable_trades": int(len(profitable)),
        "candidate_rows": int(len(candidates)),
        "selected_rows": int(len(selected)),
        "overall": summary.iloc[0].to_dict(),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary_json, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(recalled, summary, by_strategy, by_native, missed)
    print(json.dumps(summary_json, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
