from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.market_warehouse import clickhouse_query_df  # noqa: E402


DEFAULT_CANDIDATES = REPO_ROOT / "reports" / "gen3_four_path_independent_candidates" / "g3_daily_candidates.parquet"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen3_four_path_independent_candidates" / "validation_v1"
HORIZONS = (1, 2, 3, 5, 10, 20)
WINDOWS = {
    "train": ("2020-01-01", "2023-12-31"),
    "valid": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-12-31"),
    "full": ("1900-01-01", "2999-12-31"),
}


def _json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if pd.isna(value):
        return None
    return str(value)


def _pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _load_candidates(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    d = pd.read_parquet(path)
    for col in ["trade_date", "entry_date"]:
        d[col] = pd.to_datetime(d[col], errors="coerce").dt.strftime("%Y-%m-%d")
    return d.dropna(subset=["entry_date", "code", "g3_chain"]).copy()


def _load_daily_for_candidates(candidates: pd.DataFrame, max_horizon: int) -> pd.DataFrame:
    codes = sorted(candidates["code"].dropna().astype(str).unique().tolist())
    if not codes:
        return pd.DataFrame()
    start_date = str(candidates["entry_date"].min())
    end_date = (pd.Timestamp(candidates["entry_date"].max()) + pd.Timedelta(days=max_horizon * 3 + 15)).strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    batch_size = 500
    for i in range(0, len(codes), batch_size):
        batch = codes[i : i + batch_size]
        quoted = ", ".join(f"'{code}'" for code in batch)
        part = clickhouse_query_df(
            f"""
            SELECT code, trade_date, open, high, low, close
            FROM kline_daily
            WHERE code IN ({quoted})
              AND trade_date BETWEEN toDate(%(start_date)s) AND toDate(%(end_date)s)
            ORDER BY code, trade_date
            """,
            {"start_date": start_date, "end_date": end_date},
        )
        parts.append(part)
    daily = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if daily.empty:
        return daily
    daily["trade_date"] = pd.to_datetime(daily["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close"]:
        daily[col] = pd.to_numeric(daily[col], errors="coerce")
    daily = daily.dropna(subset=["code", "trade_date", "open", "close"]).sort_values(["code", "trade_date"]).reset_index(drop=True)
    g = daily.groupby("code", sort=False)
    daily["entry_open"] = daily["open"]
    for h in HORIZONS:
        daily[f"exit_close_{h}d"] = g["close"].shift(-h + 1)
        daily[f"fwd_ret_open_to_close_{h}d"] = daily[f"exit_close_{h}d"] / daily["entry_open"] - 1.0
    return daily


def _attach_forward_returns(candidates: pd.DataFrame) -> pd.DataFrame:
    daily = _load_daily_for_candidates(candidates, max(HORIZONS))
    if daily.empty:
        return candidates.copy()
    cols = ["code", "trade_date", "entry_open"] + [f"fwd_ret_open_to_close_{h}d" for h in HORIZONS]
    labeled = candidates.merge(
        daily[cols].rename(columns={"trade_date": "entry_date"}),
        on=["code", "entry_date"],
        how="left",
    )
    return labeled


def _summarize_group(group: pd.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {
        "rows": int(len(group)),
        "days": int(group["entry_date"].nunique()) if "entry_date" in group else 0,
        "unique_codes": int(group["code"].nunique()) if "code" in group else 0,
    }
    for h in HORIZONS:
        col = f"fwd_ret_open_to_close_{h}d"
        s = pd.to_numeric(group[col], errors="coerce").dropna() if col in group else pd.Series(dtype=float)
        out[f"n_{h}d"] = int(len(s))
        out[f"mean_{h}d"] = float(s.mean()) if len(s) else None
        out[f"median_{h}d"] = float(s.median()) if len(s) else None
        out[f"win_{h}d"] = float((s > 0).mean()) if len(s) else None
        out[f"p25_{h}d"] = float(s.quantile(0.25)) if len(s) else None
        out[f"p75_{h}d"] = float(s.quantile(0.75)) if len(s) else None
    return out


def _build_summary(labeled: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for window, (start, end) in WINDOWS.items():
        w = labeled[(labeled["entry_date"] >= start) & (labeled["entry_date"] <= end)].copy()
        if w.empty:
            continue
        for chain, group in w.groupby("g3_chain", sort=True):
            item = {"window": window, "g3_chain": chain}
            item.update(_summarize_group(group))
            rows.append(item)
        item = {"window": window, "g3_chain": "ALL"}
        item.update(_summarize_group(w))
        rows.append(item)
    return pd.DataFrame(rows)


def _format_summary(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return raw
    d = raw.copy()
    for h in HORIZONS:
        for key in ["mean", "median", "win", "p25", "p75"]:
            col = f"{key}_{h}d"
            if col in d:
                d[col] = d[col].map(_pct)
    return d


def _verdict(summary: pd.DataFrame) -> list[str]:
    lines: list[str] = []
    full = summary[summary["window"] == "full"].copy()
    if full.empty:
        return ["样本为空，无法判断。"]
    for _, row in full[full["g3_chain"] != "ALL"].iterrows():
        chain = row["g3_chain"]
        rows = int(row["rows"])
        mean3 = row.get("mean_3d")
        mean5 = row.get("mean_5d")
        win5 = row.get("win_5d")
        if rows < 100:
            lines.append(f"- `{chain}` 样本 {rows} 条，暂时只能看方向，不能下正式结论。")
        elif pd.notna(mean3) and pd.notna(mean5) and float(mean3) > 0 and float(mean5) > 0 and pd.notna(win5) and float(win5) >= 0.50:
            lines.append(f"- `{chain}` 初步通过：3日/5日前瞻均值为正，5日胜率不低于 50%。")
        else:
            lines.append(f"- `{chain}` 暂未通过：3日/5日均值或胜率不满足独立正期望。")
    return lines


def _write_report(output_dir: Path, candidates_path: Path, labeled: pd.DataFrame, raw: pd.DataFrame, display: pd.DataFrame) -> None:
    lines = [
        "# G3 四链路候选源验证 V1",
        "",
        "## 口径",
        "",
        f"- 候选源：`{candidates_path}`",
        "- 入场价：`entry_date` 下一交易日开盘价。",
        "- 前瞻标签：持有 1/2/3/5/10/20 个交易日后以收盘价计算，不含滑点、手续费和盘中止损。",
        "- 这是候选源有效性验证，不是完整组合回测；通过后还要接 15m/30m 确认、风控和容量约束。",
        "",
        "## 初步结论",
        "",
        *_verdict(raw),
        "",
        "## 汇总表",
        "",
    ]
    if display.empty:
        lines.append("无可用样本。")
    else:
        cols = [
            "window",
            "g3_chain",
            "rows",
            "days",
            "unique_codes",
            "mean_3d",
            "median_3d",
            "win_3d",
            "mean_5d",
            "median_5d",
            "win_5d",
            "mean_10d",
            "median_10d",
            "win_10d",
            "mean_20d",
            "median_20d",
            "win_20d",
        ]
        lines.append(display[[c for c in cols if c in display.columns]].to_markdown(index=False))
    lines.extend(
        [
            "",
            "## 防未来函数说明",
            "",
            "- G3 候选生成使用信号日收盘后确认的日线状态，验证统一按下一交易日开盘价入场。",
            "- 这适合验证日线候选源方向；若要做盘中买法，必须进一步把确认条件改成 `confirm_datetime` 前可见的分钟级特征。",
            "",
        ]
    )
    output_dir.joinpath("validation_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def validate(args: argparse.Namespace) -> dict[str, Any]:
    candidates_path = Path(args.candidates)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates = _load_candidates(candidates_path)
    labeled = _attach_forward_returns(candidates)
    raw = _build_summary(labeled)
    display = _format_summary(raw)

    labeled.to_parquet(output_dir / "labeled_candidates.parquet", index=False)
    labeled.to_csv(output_dir / "labeled_candidates.csv", index=False, encoding="utf-8-sig")
    raw.to_csv(output_dir / "forward_summary_raw.csv", index=False, encoding="utf-8-sig")
    display.to_csv(output_dir / "forward_summary_display.csv", index=False, encoding="utf-8-sig")
    result = {
        "candidates_path": str(candidates_path),
        "output_dir": str(output_dir),
        "candidate_rows": int(len(candidates)),
        "labeled_rows": int(len(labeled)),
        "summary_rows": int(len(raw)),
    }
    (output_dir / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_report(output_dir, candidates_path, labeled, raw, display)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate G3 four-path candidates with forward returns.")
    parser.add_argument("--candidates", default=str(DEFAULT_CANDIDATES))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()
    print(json.dumps(validate(args), ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
