from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path

SOURCE = report_path("gen3_market_state_router_strategy_v1", "g3_route_execution_mandate_candidate_closed_trades.csv")
OUT_DIR = report_path("gen3_shadow_live_daily_update_v1")


def _run(cmd: list[str]) -> dict:
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    return {
        "command": " ".join(cmd),
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "ok": proc.returncode == 0,
    }


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, low_memory=False)


def _md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_无数据_"
    return df.to_markdown(index=False)


def _diagnose(as_of_date: str, source_df: pd.DataFrame, payload_df: pd.DataFrame, summary_df: pd.DataFrame, blocked_df: pd.DataFrame) -> tuple[str, str]:
    if source_df.empty:
        return "SOURCE_EMPTY", "G3 研究源为空或不可读，不能生成影子候选。"
    if payload_df.empty:
        return "PAYLOAD_EMPTY", "live payload 为空，可能被 schema 全部阻断或生成失败。"
    latest = pd.to_datetime(payload_df.get("entry_date"), errors="coerce").max()
    today_rows = int((pd.to_datetime(payload_df.get("entry_date"), errors="coerce").dt.strftime("%Y-%m-%d") == as_of_date).sum())
    if today_rows == 0:
        latest_text = "" if pd.isna(latest) else latest.strftime("%Y-%m-%d")
        return "NO_G3_SIGNAL_FOR_DATE", f"live payload 中没有 {as_of_date} 的候选；当前最新 entry_date={latest_text}。"
    if not summary_df.empty:
        display = int(pd.to_numeric(summary_df.iloc[0].get("display_candidates", 0), errors="coerce") or 0)
        if display > 0:
            return "HAS_SHADOW_CANDIDATES", f"{as_of_date} 有 {display} 条 G3 影子观察候选。"
    if not blocked_df.empty:
        reasons = blocked_df.get("block_reason", pd.Series(dtype=str)).astype(str).value_counts().to_dict()
        return "BLOCKED_OR_PENDING", f"{as_of_date} 有记录但未展示，阻断/等待原因={reasons}。"
    return "NO_DISPLAY_AFTER_FILTER", f"{as_of_date} 有 payload 记录，但经过确认时间/状态过滤后没有展示候选。"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run G3 shadow live daily update chain.")
    parser.add_argument("--as-of", default=None, help="Decision datetime, e.g. 2026-06-01 15:00:00")
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    parser.add_argument("--allow-after-close", action="store_true")
    args = parser.parse_args()

    as_of = pd.Timestamp(args.as_of) if args.as_of else pd.Timestamp(datetime.now())
    as_of_date = as_of.strftime("%Y-%m-%d")
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    payload_out = out / "payload"
    entry_out = out / "entry"
    payload_out.mkdir(parents=True, exist_ok=True)
    entry_out.mkdir(parents=True, exist_ok=True)

    commands = []
    build_cmd = [
        sys.executable,
        "scripts/gen3_build_live_payload_v1.py",
        "--input",
        str(SOURCE),
        "--out-dir",
        str(payload_out),
    ]
    commands.append(_run(build_cmd))
    if not commands[-1]["ok"]:
        command_log = pd.DataFrame(commands)
        command_log.to_csv(out / "g3_shadow_daily_command_log.csv", index=False, encoding="utf-8-sig")
        diag = pd.DataFrame(
            [
                {
                    "as_of": as_of.strftime("%Y-%m-%d %H:%M:%S"),
                    "as_of_date": as_of_date,
                    "diagnosis_code": "PAYLOAD_BUILD_FAILED",
                    "diagnosis": "G3 live payload 生成失败，已停止 shadow entry，避免沿用旧 payload。",
                    "source_rows": 0,
                    "payload_rows": 0,
                    "display_candidates": 0,
                    "blocked_rows": 0,
                    "auto_order_allowed_rows": 0,
                }
            ]
        )
        diag.to_csv(out / "g3_shadow_daily_diagnosis.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame().to_csv(entry_out / "g3_shadow_live_candidates.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame().to_csv(entry_out / "g3_shadow_live_blocked.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame().to_csv(entry_out / "g3_shadow_live_recent_signal_dates.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame(
            [
                {
                    "as_of": as_of.strftime("%Y-%m-%d %H:%M:%S"),
                    "as_of_date": as_of_date,
                    "input_rows": 0,
                    "today_payload_rows": 0,
                    "display_candidates": 0,
                    "blocked_rows": 0,
                    "auto_order_allowed_rows": 0,
                    "latest_entry_date": "",
                    "after_close": False,
                    "allow_after_close": bool(args.allow_after_close),
                }
            ]
        ).to_csv(entry_out / "g3_shadow_live_summary.csv", index=False, encoding="utf-8-sig")
        print(
            {
                "out_dir": str(out),
                "diagnosis_code": "PAYLOAD_BUILD_FAILED",
                "display_candidates": 0,
                "blocked_rows": 0,
                "auto_order_allowed_rows": 0,
            }
        )
        return

    entry_cmd = [
        sys.executable,
        "scripts/gen3_shadow_live_entry_v1.py",
        "--input",
        str(payload_out / "g3_live_payload.csv"),
        "--out-dir",
        str(entry_out),
        "--as-of",
        as_of.strftime("%Y-%m-%d %H:%M:%S"),
    ]
    if args.allow_after_close:
        entry_cmd.append("--allow-after-close")
    commands.append(_run(entry_cmd))

    source_df = _read_csv(SOURCE)
    payload_df = _read_csv(payload_out / "g3_live_payload.csv")
    candidates_df = _read_csv(entry_out / "g3_shadow_live_candidates.csv")
    blocked_df = _read_csv(entry_out / "g3_shadow_live_blocked.csv")
    summary_df = _read_csv(entry_out / "g3_shadow_live_summary.csv")
    recent_df = _read_csv(entry_out / "g3_shadow_live_recent_signal_dates.csv")

    diagnosis_code, diagnosis = _diagnose(as_of_date, source_df, payload_df, summary_df, blocked_df)

    chain_rows = [
        {
            "stage": "source",
            "ok": SOURCE.exists() and not source_df.empty,
            "path": str(SOURCE),
            "rows": len(source_df),
            "detail": "research source readable" if SOURCE.exists() and not source_df.empty else "missing_or_empty",
        },
        {
            "stage": "payload_build",
            "ok": bool(commands[0]["ok"]) and not payload_df.empty,
            "path": str(payload_out / "g3_live_payload.csv"),
            "rows": len(payload_df),
            "detail": commands[0]["stdout"][-300:],
        },
        {
            "stage": "shadow_entry",
            "ok": bool(commands[1]["ok"]),
            "path": str(entry_out / "g3_shadow_live_candidates.csv"),
            "rows": len(candidates_df),
            "detail": commands[1]["stdout"][-300:],
        },
    ]
    chain = pd.DataFrame(chain_rows)
    command_log = pd.DataFrame(commands)
    diag = pd.DataFrame(
        [
            {
                "as_of": as_of.strftime("%Y-%m-%d %H:%M:%S"),
                "as_of_date": as_of_date,
                "diagnosis_code": diagnosis_code,
                "diagnosis": diagnosis,
                "source_rows": len(source_df),
                "payload_rows": len(payload_df),
                "display_candidates": len(candidates_df),
                "blocked_rows": len(blocked_df),
                "auto_order_allowed_rows": int(pd.to_numeric(candidates_df.get("auto_order_allowed", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()) if not candidates_df.empty else 0,
            }
        ]
    )

    chain.to_csv(out / "g3_shadow_daily_chain_status.csv", index=False, encoding="utf-8-sig")
    command_log.to_csv(out / "g3_shadow_daily_command_log.csv", index=False, encoding="utf-8-sig")
    diag.to_csv(out / "g3_shadow_daily_diagnosis.csv", index=False, encoding="utf-8-sig")

    report = f"""# G3 Shadow Live 每日更新链路 V1

生成时间：2026-06-01

## 决策时间

- as_of：`{as_of.strftime('%Y-%m-%d %H:%M:%S')}`
- as_of_date：`{as_of_date}`

## 链路

`research source -> live payload -> shadow entry -> diagnosis`

本脚本只读取 G3 独立产物，不触发 G2 重建，不启动服务，不下单。

## 诊断

{_md_table(diag)}

## 阶段状态

{_md_table(chain)}

## 今日观察候选

{_md_table(candidates_df)}

## 今日阻断/等待记录

{_md_table(blocked_df)}

## 最近有信号日期

{_md_table(recent_df)}

## 命令日志

{_md_table(command_log[['command', 'returncode', 'ok']])}

## 判断

- `HAS_SHADOW_CANDIDATES`：有可展示影子候选，但仍然 `auto_order_allowed = false`。
- `NO_G3_SIGNAL_FOR_DATE`：今日没有 G3 live payload 候选，不能展示历史旧信号。
- `BLOCKED_OR_PENDING`：今日有 payload，但被状态、确认时间或 schema 规则阻断。
- `PAYLOAD_EMPTY` / `SOURCE_EMPTY`：上游产物为空或不可读，应先修复上游。

## 下一步

第54步应把这个每日链路接入一个只读 API 或页面卡片，但仍保持 observe-only；页面上必须展示诊断码，不能只显示“无买点”。
"""
    (out / "g3_shadow_live_daily_update_report_cn.md").write_text(report, encoding="utf-8", newline="\n")
    print(
        {
            "out_dir": str(out),
            "diagnosis_code": diagnosis_code,
            "display_candidates": len(candidates_df),
            "blocked_rows": len(blocked_df),
            "auto_order_allowed_rows": int(diag.iloc[0]["auto_order_allowed_rows"]),
        }
    )


if __name__ == "__main__":
    main()
