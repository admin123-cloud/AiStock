from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


from pathlib import Path
import sys
from typing import Any

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path

from scripts.gen3_backtest_strong_volume5_confirm_d3_execution_stress_v1 import _load_daily_prices  # noqa: E402
from scripts.gen3_build_four_path_candidates import INDEX_CODE, _load_index_daily  # noqa: E402
from scripts.gen3_test_v4_strong_position_scale_v1 import md_table  # noqa: E402


SRC_DIR = report_path("gen3_v4_strong_second_093_execution_stress_v1")
OUT_DIR = report_path("gen3_v4_strong_failure_attribution_v1")
BASE_PROFILE = "close_30bps"
NEXTOPEN_PROFILE = "nextopen_30bps"


def load_trades(profile: str) -> pd.DataFrame:
    d = pd.read_csv(SRC_DIR / profile / "closed_trades.csv", low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in ["entry_price", "policy_net_ret", "realized_pnl", "stake", "score", "strong_day_rank"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d[d["route"].astype(str).eq("strong_main")].copy()
    return d.dropna(subset=["entry_date", "policy_exit_date", "code", "entry_price", "policy_net_ret"])


def build_price_maps(daily: pd.DataFrame) -> dict[str, pd.DataFrame]:
    d = daily.copy()
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.normalize()
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return {str(code): g.sort_values("trade_date").copy() for code, g in d.groupby("code", sort=False)}


def load_index(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    idx = _load_index_daily(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    idx["trade_date"] = pd.to_datetime(idx["trade_date"], errors="coerce").dt.normalize()
    idx["index_ret1"] = pd.to_numeric(idx["close"], errors="coerce").pct_change()
    return idx[["trade_date", "open", "close", "index_ret1"]].rename(columns={"open": "index_open", "close": "index_close"})


def enrich_path(trades: pd.DataFrame, daily: pd.DataFrame, index: pd.DataFrame) -> pd.DataFrame:
    price_maps = build_price_maps(daily)
    index_map = index.set_index("trade_date")
    rows: list[dict[str, Any]] = []
    for row in trades.itertuples(index=False):
        code = str(row.code)
        entry = pd.Timestamp(row.entry_date).normalize()
        exit_date = pd.Timestamp(row.policy_exit_date).normalize()
        entry_price = float(row.entry_price)
        path = price_maps.get(code, pd.DataFrame())
        path = path[(path["trade_date"] >= entry) & (path["trade_date"] <= exit_date)].copy() if not path.empty else path
        out = row._asdict()
        out["path_rows"] = int(len(path))
        if path.empty or entry_price <= 0:
            rows.append(out)
            continue
        path["open_ret"] = path["open"] / entry_price - 1.0
        path["high_ret"] = path["high"] / entry_price - 1.0
        path["low_ret"] = path["low"] / entry_price - 1.0
        path["close_ret"] = path["close"] / entry_price - 1.0
        path["stock_ret1"] = path["close"].pct_change()
        joined = path.merge(index[["trade_date", "index_ret1"]], on="trade_date", how="left")
        low_idx = path["low_ret"].idxmin()
        high_idx = path["high_ret"].idxmax()
        d1 = path.iloc[1] if len(path) > 1 else None
        d2 = path.iloc[2] if len(path) > 2 else None
        entry_index = index_map.loc[entry] if entry in index_map.index else None
        exit_index = index_map.loc[exit_date] if exit_date in index_map.index else None
        index_hold_ret = None
        if entry_index is not None and exit_index is not None and float(entry_index["index_close"]) > 0:
            index_hold_ret = float(exit_index["index_close"]) / float(entry_index["index_close"]) - 1.0
        out.update(
            {
                "min_low_ret": float(path.loc[low_idx, "low_ret"]),
                "min_low_date": path.loc[low_idx, "trade_date"],
                "min_low_day_index": int(path.index.get_loc(low_idx)),
                "max_high_ret": float(path.loc[high_idx, "high_ret"]),
                "max_high_date": path.loc[high_idx, "trade_date"],
                "max_high_day_index": int(path.index.get_loc(high_idx)),
                "giveback_from_high": float(path.loc[high_idx, "high_ret"] - float(row.policy_net_ret)),
                "entry_close_ret": float(path.iloc[0]["close_ret"]),
                "d1_close_ret": float(d1["close_ret"]) if d1 is not None else None,
                "d1_low_ret": float(d1["low_ret"]) if d1 is not None else None,
                "d2_close_ret": float(d2["close_ret"]) if d2 is not None else None,
                "d2_low_ret": float(d2["low_ret"]) if d2 is not None else None,
                "worst_stock_ret1": float(joined["stock_ret1"].min(skipna=True)),
                "worst_index_ret1": float(joined["index_ret1"].min(skipna=True)),
                "index_hold_ret": index_hold_ret,
                "index_down_days": int((pd.to_numeric(joined["index_ret1"], errors="coerce") < 0).sum()),
                "index_big_down_days": int((pd.to_numeric(joined["index_ret1"], errors="coerce") <= -0.02).sum()),
            }
        )
        rows.append(out)
    out_df = pd.DataFrame(rows)
    for col in ["min_low_date", "max_high_date"]:
        if col in out_df.columns:
            out_df[col] = pd.to_datetime(out_df[col], errors="coerce").dt.strftime("%Y-%m-%d")
    return out_df


def add_nextopen_delta(base: pd.DataFrame, nextopen: pd.DataFrame) -> pd.DataFrame:
    keep = ["entry_date", "code", "policy_net_ret", "realized_pnl"]
    n = nextopen[keep].rename(columns={"policy_net_ret": "nextopen_net_ret", "realized_pnl": "nextopen_pnl"}).copy()
    d = base.merge(n, on=["entry_date", "code"], how="left")
    d["nextopen_ret_delta"] = pd.to_numeric(d["nextopen_net_ret"], errors="coerce") - pd.to_numeric(d["policy_net_ret"], errors="coerce")
    d["nextopen_pnl_delta"] = pd.to_numeric(d["nextopen_pnl"], errors="coerce") - pd.to_numeric(d["realized_pnl"], errors="coerce")
    return d


def attribution(row: pd.Series) -> str:
    tags: list[str] = []
    ret = float(row.get("policy_net_ret", 0.0))
    if pd.to_numeric(row.get("nextopen_ret_delta"), errors="coerce") <= -0.05:
        tags.append("执行低开/隔夜冲击")
    if pd.to_numeric(row.get("index_hold_ret"), errors="coerce") <= -0.03 or int(row.get("index_big_down_days") or 0) >= 1:
        tags.append("指数冲击")
    if pd.to_numeric(row.get("max_high_ret"), errors="coerce") >= 0.10 and pd.to_numeric(row.get("giveback_from_high"), errors="coerce") >= 0.12:
        tags.append("强势后大回吐")
    if pd.to_numeric(row.get("d1_close_ret"), errors="coerce") <= -0.06 or pd.to_numeric(row.get("d2_close_ret"), errors="coerce") <= -0.08:
        tags.append("个股早期失败")
    if pd.to_numeric(row.get("min_low_ret"), errors="coerce") <= -0.12 and ret > -0.08:
        tags.append("盘中穿透但收回")
    if not tags:
        if ret <= -0.08:
            tags.append("个股持仓期失败")
        else:
            tags.append("小亏/噪音")
    return " / ".join(tags)


def bucket_summary(d: pd.DataFrame, col: str) -> pd.DataFrame:
    g = (
        d.groupby(col, dropna=False)
        .agg(
            trades=("code", "count"),
            mean_ret=("policy_net_ret", "mean"),
            win_rate=("policy_net_ret", lambda s: float((s > 0).mean())),
            bad8_rate=("policy_net_ret", lambda s: float((s <= -0.08).mean())),
            worst=("policy_net_ret", "min"),
            pnl=("realized_pnl", "sum"),
        )
        .reset_index()
    )
    return g.sort_values(["mean_ret", "trades"], ascending=[True, False])


def report_table(df: pd.DataFrame, pct_cols: set[str], money_cols: set[str]) -> str:
    if df.empty:
        return "_无数据_"
    d = df.copy()
    for col in money_cols:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce").map(lambda x: "" if pd.isna(x) else f"{x:,.2f}")
    return md_table(d, pct_cols=pct_cols)


def write_report(all_trades: pd.DataFrame, bad: pd.DataFrame, by_tag: pd.DataFrame, by_year: pd.DataFrame) -> None:
    pct_cols = {
        "policy_net_ret",
        "nextopen_net_ret",
        "nextopen_ret_delta",
        "min_low_ret",
        "max_high_ret",
        "giveback_from_high",
        "d1_close_ret",
        "d2_close_ret",
        "index_hold_ret",
        "mean_ret",
        "win_rate",
        "bad8_rate",
        "worst",
    }
    money_cols = {"realized_pnl", "nextopen_pnl_delta", "pnl"}
    summary = pd.DataFrame(
        [
            {
                "scope": "strong_main close_30bps",
                "trades": len(all_trades),
                "mean_ret": all_trades["policy_net_ret"].mean(),
                "win_rate": float((all_trades["policy_net_ret"] > 0).mean()),
                "bad8_trades": int((all_trades["policy_net_ret"] <= -0.08).sum()),
                "bad8_rate": float((all_trades["policy_net_ret"] <= -0.08).mean()),
                "pnl": all_trades["realized_pnl"].sum(),
            },
            {
                "scope": "bad8_or_big_giveback",
                "trades": len(bad),
                "mean_ret": bad["policy_net_ret"].mean(),
                "win_rate": float((bad["policy_net_ret"] > 0).mean()) if len(bad) else 0,
                "bad8_trades": int((bad["policy_net_ret"] <= -0.08).sum()) if len(bad) else 0,
                "bad8_rate": float((bad["policy_net_ret"] <= -0.08).mean()) if len(bad) else 0,
                "pnl": bad["realized_pnl"].sum() if len(bad) else 0,
            },
        ]
    )
    top_cols = [
        "entry_date",
        "policy_exit_date",
        "code",
        "name",
        "policy_net_ret",
        "realized_pnl",
        "nextopen_ret_delta",
        "min_low_ret",
        "max_high_ret",
        "giveback_from_high",
        "d1_close_ret",
        "d2_close_ret",
        "index_hold_ret",
        "failure_tag",
    ]
    lines = [
        "# G3 V4 strong_main 失败样本归因审计 v1",
        "",
        "## 边界",
        "",
        "- 样本固定为 `strong_second_score_ge_093` 的 `strong_main` 已成交交易。",
        "- 主口径使用 `close_30bps`，同时对比 `nextopen_30bps` 的执行差异。",
        "- 本报告只做失败归因，不调参、不生成新买卖规则。",
        "- 板块退潮目前只作为待验证方向；本版先用个股路径、指数路径和执行差异做可确认归因。",
        "",
        "## 总览",
        "",
        report_table(summary, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 失败标签汇总",
        "",
        report_table(by_tag, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 年度失败概览",
        "",
        report_table(by_year, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 重点失败样本",
        "",
        report_table(bad.sort_values(["policy_net_ret", "giveback_from_high"], ascending=[True, False]).head(35)[top_cols], pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 初步判断",
        "",
        "- 强势链路的退出问题不能继续用单一止盈止损阈值解决，需要先区分失败来源。",
        "- 如果主要标签集中在“强势后大回吐”，下一步才研究退潮信号或滞涨保护。",
        "- 如果主要标签集中在“个股早期失败”，应回到入场确认质量，而不是退出。",
        "- 如果“执行低开/隔夜冲击”占比高，则需要执行层降脆弱性，而不是选股层过滤。",
        "",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_trades(BASE_PROFILE)
    nextopen = load_trades(NEXTOPEN_PROFILE)
    daily = _load_daily_prices(base, extra_days=5)
    index = load_index(base["entry_date"].min(), base["policy_exit_date"].max() + pd.Timedelta(days=5))
    enriched = enrich_path(base, daily, index)
    enriched = add_nextopen_delta(enriched, nextopen)
    enriched["failure_tag"] = enriched.apply(attribution, axis=1)
    bad = enriched[(enriched["policy_net_ret"] <= -0.08) | (pd.to_numeric(enriched["giveback_from_high"], errors="coerce") >= 0.15)].copy()
    by_tag = bucket_summary(enriched, "failure_tag")
    enriched["entry_year"] = pd.to_datetime(enriched["entry_date"], errors="coerce").dt.year
    by_year = bucket_summary(enriched, "entry_year")
    enriched.to_csv(OUT_DIR / "strong_main_path_attribution.csv", index=False, encoding="utf-8-sig")
    bad.to_csv(OUT_DIR / "bad8_or_big_giveback_samples.csv", index=False, encoding="utf-8-sig")
    by_tag.to_csv(OUT_DIR / "failure_tag_summary.csv", index=False, encoding="utf-8-sig")
    by_year.to_csv(OUT_DIR / "year_summary.csv", index=False, encoding="utf-8-sig")
    write_report(enriched, bad, by_tag, by_year)
    print(f"wrote {OUT_DIR}")
    print(by_tag.to_string(index=False))
    print(by_year.to_string(index=False))


if __name__ == "__main__":
    main()
