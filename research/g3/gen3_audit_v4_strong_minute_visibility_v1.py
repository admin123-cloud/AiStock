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

from scripts.gen3_backtest_strong_volume5_confirm_d3_execution_stress_v1 import _sql_literal  # noqa: E402
from scripts.gen3_test_v4_strong_position_scale_v1 import md_table  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


SRC_DIR = _report_path() / "gen3_v4_strong_visible_confirmation_audit_v1"
OUT_DIR = _report_path() / "gen3_v4_strong_minute_visibility_audit_v1"


def load_samples() -> pd.DataFrame:
    d = pd.read_csv(SRC_DIR / "visible_confirmation_enriched.csv", low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in [
        "sample_ret",
        "sample_pnl",
        "score",
        "strong_day_rank",
        "prev_close",
        "prev_high20",
        "prev_ret20",
        "pnl_delta_nextopen",
        "ret_delta_nextopen",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["entry_date", "code"]).reset_index(drop=True)


def load_minute30(samples: pd.DataFrame) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    date_codes = samples.groupby("entry_date")["code"].apply(lambda s: sorted(set(s.astype(str)))).to_dict()
    for day, codes in date_codes.items():
        day_text = pd.Timestamp(day).strftime("%Y-%m-%d")
        for i in range(0, len(codes), 200):
            quoted = ",".join(_sql_literal(c) for c in codes[i : i + 200])
            sql = f"""
            SELECT code, datetime, open, high, low, close, volume
            FROM kline_minute_30
            WHERE code IN ({quoted})
              AND toDate(datetime) = toDate({_sql_literal(day_text)})
            ORDER BY code, datetime
            """
            part = clickhouse_query_df(sql)
            if not part.empty:
                parts.append(part)
    d = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if d.empty:
        return d
    d["datetime"] = pd.to_datetime(d["datetime"], errors="coerce")
    d["entry_date"] = d["datetime"].dt.normalize()
    d["time_text"] = d["datetime"].dt.strftime("%H:%M:%S")
    for col in ["open", "high", "low", "close", "volume"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["code", "entry_date", "datetime", "open", "high", "low", "close"])


def pick_bar(g: pd.DataFrame, cutoff: str) -> pd.Series | None:
    part = g[g["time_text"] <= cutoff].sort_values("datetime")
    if part.empty:
        return None
    return part.iloc[-1]


def minute_features(samples: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    by_key = {(str(code), pd.Timestamp(day).normalize()): g.copy() for (code, day), g in bars.groupby(["code", "entry_date"])}
    rows: list[dict[str, Any]] = []
    for row in samples.itertuples(index=False):
        code = str(row.code)
        day = pd.Timestamp(row.entry_date).normalize()
        g = by_key.get((code, day), pd.DataFrame())
        prev_close = float(getattr(row, "prev_close", float("nan")) or float("nan"))
        prev_high20 = float(getattr(row, "prev_high20", float("nan")) or float("nan"))
        out = row._asdict()
        out["m30_bar_count"] = int(len(g))
        out["m30_first_time"] = "" if g.empty else str(g["time_text"].min())
        out["m30_last_time"] = "" if g.empty else str(g["time_text"].max())
        for cutoff in ["10:00:00", "10:30:00", "11:00:00"]:
            key = cutoff.replace(":", "")[:4]
            bar = pick_bar(g, cutoff)
            if bar is None or not pd.notna(prev_close) or prev_close <= 0:
                out[f"m30_ret_{key}"] = pd.NA
                out[f"m30_high_ret_{key}"] = pd.NA
                out[f"m30_break20_{key}"] = False
                continue
            upto = g[g["time_text"] <= cutoff]
            high = float(upto["high"].max()) if not upto.empty else float(bar["high"])
            close = float(bar["close"])
            out[f"m30_ret_{key}"] = close / prev_close - 1.0
            out[f"m30_high_ret_{key}"] = high / prev_close - 1.0
            out[f"m30_break20_{key}"] = bool(pd.notna(prev_high20) and high >= prev_high20)
        out["m30_visible_strength_1000"] = (
            pd.notna(out.get("m30_ret_1000"))
            and (float(out["m30_ret_1000"]) >= 0.03 or float(out["m30_high_ret_1000"]) >= 0.05)
            and bool(out.get("m30_break20_1000"))
        )
        out["m30_visible_strength_1030"] = (
            pd.notna(out.get("m30_ret_1030"))
            and (float(out["m30_ret_1030"]) >= 0.04 or float(out["m30_high_ret_1030"]) >= 0.07)
            and bool(out.get("m30_break20_1030"))
        )
        rows.append(out)
    return pd.DataFrame(rows)


def summarize(enriched: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rows: list[dict[str, Any]] = []
    for group, g in enriched.groupby("group"):
        for flag in ["m30_visible_strength_1000", "m30_visible_strength_1030"]:
            for value, b in g.groupby(g[flag].fillna(False)):
                rows.append(
                    {
                        "group": group,
                        "flag": flag,
                        "pass": bool(value),
                        "rows": int(len(b)),
                        "mean_ret": float(pd.to_numeric(b["sample_ret"], errors="coerce").mean()),
                        "win_rate": float((pd.to_numeric(b["sample_ret"], errors="coerce") > 0).mean()),
                        "sum_pnl_delta_or_na": float(pd.to_numeric(b.get("sample_pnl", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()),
                        "mean_score": float(pd.to_numeric(b.get("score", pd.Series(dtype=float)), errors="coerce").mean()),
                        "mean_m30_ret_1030": float(pd.to_numeric(b.get("m30_ret_1030", pd.Series(dtype=float)), errors="coerce").mean()),
                        "mean_m30_high_ret_1030": float(pd.to_numeric(b.get("m30_high_ret_1030", pd.Series(dtype=float)), errors="coerce").mean()),
                    }
                )
    flag_summary = pd.DataFrame(rows)

    coverage = (
        enriched.groupby("group")
        .agg(
            rows=("code", "count"),
            covered=("m30_bar_count", lambda s: int((pd.to_numeric(s, errors="coerce") > 0).sum())),
            coverage_rate=("m30_bar_count", lambda s: float((pd.to_numeric(s, errors="coerce") > 0).mean())),
            strength1000_rate=("m30_visible_strength_1000", "mean"),
            strength1030_rate=("m30_visible_strength_1030", "mean"),
        )
        .reset_index()
    )

    focus = pd.concat(
        [
            enriched[enriched["group"].eq("skipped_by_093")].sort_values("sample_ret", ascending=False).head(25),
            enriched[enriched["group"].eq("selected_strong_nextopen_damage")].sort_values("pnl_delta_nextopen").head(25),
        ],
        ignore_index=True,
    )
    cols = [
        "group",
        "entry_date",
        "code",
        "name",
        "score",
        "strong_day_rank",
        "sample_ret",
        "pnl_delta_nextopen",
        "m30_bar_count",
        "m30_ret_1000",
        "m30_high_ret_1000",
        "m30_break20_1000",
        "m30_visible_strength_1000",
        "m30_ret_1030",
        "m30_high_ret_1030",
        "m30_break20_1030",
        "m30_visible_strength_1030",
    ]
    focus = focus[[c for c in cols if c in focus.columns]]
    return coverage, flag_summary, focus


def write_report(coverage: pd.DataFrame, flag_summary: pd.DataFrame, focus: pd.DataFrame) -> None:
    pct_cols = {
        "coverage_rate",
        "strength1000_rate",
        "strength1030_rate",
        "mean_ret",
        "win_rate",
        "mean_m30_ret_1030",
        "mean_m30_high_ret_1030",
        "sample_ret",
        "m30_ret_1000",
        "m30_high_ret_1000",
        "m30_ret_1030",
        "m30_high_ret_1030",
    }
    lines = [
        "# G3 V4 strong_main 30m 可见强度诊断 v1",
        "",
        "## 边界",
        "",
        "- 使用 `kline_minute_30` 的历史 30m bar，只做研究诊断。",
        "- `10:00` 和 `10:30` 均为入场日盘中可见代理，后续若要正式化还需确认 bar 时间戳语义和真实成交价。",
        "- 当前不改变 `strong_second_score_ge_093`，只判断被过滤票与执行受损票是否存在可见强度差异。",
        "",
        "## 覆盖率",
        "",
        md_table(coverage, pct_cols=pct_cols),
        "",
        "## 强度分桶",
        "",
        md_table(flag_summary, pct_cols=pct_cols),
        "",
        "## 重点样本",
        "",
        md_table(focus, pct_cols=pct_cols),
        "",
        "## 初步结论",
        "",
        "- 如果 10:30 强度对 `skipped_by_093` 的均值/胜率有提升，下一步可以做 fixed 规则回放，而不是继续调 score 阈值。",
        "- 如果 10:30 强度同时出现在大量 `selected_strong_nextopen_damage` 中，说明它只能做进攻确认，不能解决退出回吐，还需要独立退出规则。",
        "",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    samples = load_samples()
    bars = load_minute30(samples)
    enriched = minute_features(samples, bars)
    coverage, flag_summary, focus = summarize(enriched)
    bars.to_csv(OUT_DIR / "minute30_bars_loaded.csv", index=False, encoding="utf-8-sig")
    enriched.to_csv(OUT_DIR / "minute_visibility_enriched.csv", index=False, encoding="utf-8-sig")
    coverage.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8-sig")
    flag_summary.to_csv(OUT_DIR / "flag_summary.csv", index=False, encoding="utf-8-sig")
    focus.to_csv(OUT_DIR / "focus_samples.csv", index=False, encoding="utf-8-sig")
    write_report(coverage, flag_summary, focus)
    print(f"wrote {OUT_DIR}")
    print(coverage.to_string(index=False))
    print(flag_summary.to_string(index=False))


if __name__ == "__main__":
    main()
