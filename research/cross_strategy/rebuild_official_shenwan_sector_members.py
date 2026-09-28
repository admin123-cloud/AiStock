from __future__ import annotations

import argparse
import io
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client, clickhouse_query_df  # noqa: E402


@dataclass(frozen=True)
class SectorInfo:
    code: str
    name: str
    level: int
    official_code: str
    parent_name: str | None
    official_count: int


def _clean_sw_name(name: Any) -> str:
    text = str(name or "").strip()
    for suffix in ("Ⅱ", "II"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    return text


def _parse_levels(text: str) -> list[int]:
    out: list[int] = []
    for item in str(text or "").split(","):
        item = item.strip()
        if not item:
            continue
        value = int(item)
        if value not in {1, 2, 3}:
            raise ValueError("levels only supports 1,2,3")
        out.append(value)
    return sorted(set(out)) or [1, 2, 3]


def _canonical_stock_code(raw: Any) -> str | None:
    text = str(raw or "").strip().upper()
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) != 6:
        return None
    if digits[0] in {"6", "9"}:
        return f"{digits}.SH"
    if digits[0] in {"0", "2", "3"}:
        return f"{digits}.SZ"
    if digits[0] in {"4", "8"}:
        return f"{digits}.BJ"
    return None


def load_official_sector_infos(levels: list[int]) -> list[SectorInfo]:
    import akshare as ak  # type: ignore

    loaders = {
        1: ak.sw_index_first_info,
        2: ak.sw_index_second_info,
        3: ak.sw_index_third_info,
    }
    infos: list[SectorInfo] = []
    for level in levels:
        df = loaders[level]()
        if df is None or df.empty:
            continue
        cols = list(df.columns)
        code_col = cols[0]
        name_col = cols[1]
        parent_col = cols[2] if level in {2, 3} else None
        count_col = cols[2] if level == 1 else cols[3]
        for _, row in df.iterrows():
            official_code = str(row.get(code_col) or "").strip().upper()
            name = _clean_sw_name(row.get(name_col))
            if not official_code or not name:
                continue
            official_count = int(pd.to_numeric(pd.Series([row.get(count_col)]), errors="coerce").fillna(0).iloc[0])
            infos.append(
                SectorInfo(
                    code=f"qmt:SW{level}{name}",
                    name=name,
                    level=level,
                    official_code=official_code,
                    parent_name=_clean_sw_name(row.get(parent_col)) if parent_col else None,
                    official_count=official_count,
                )
            )
    return infos


def fetch_component_rows(infos: list[SectorInfo], sleep_seconds: float = 0.05, retries: int = 3) -> tuple[pd.DataFrame, list[str]]:
    import akshare as ak  # type: ignore

    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for idx, info in enumerate(infos, start=1):
        symbol = info.official_code.replace(".SI", "")
        last_error = ""
        df = None
        for attempt in range(1, max(int(retries), 1) + 1):
            try:
                df = ak.index_component_sw(symbol=symbol)
                break
            except Exception as exc:
                last_error = str(exc)
                if attempt < max(int(retries), 1):
                    time.sleep(max(float(sleep_seconds), 0.1) * attempt)
        try:
            if df is None:
                raise RuntimeError(last_error or "empty_response")
        except Exception as exc:
            errors.append(f"{info.code}:{info.official_code}:{exc}")
            continue
        if df is None or df.empty:
            errors.append(f"{info.code}:{info.official_code}:empty_component")
            continue
        cols = list(df.columns)
        code_col = cols[1]
        weight_col = cols[3] if len(cols) > 3 else None
        for _, row in df.iterrows():
            stock_code = _canonical_stock_code(row.get(code_col))
            if not stock_code:
                continue
            weight = float(pd.to_numeric(pd.Series([row.get(weight_col)]), errors="coerce").fillna(0.0).iloc[0]) if weight_col else 0.0
            rows.append(
                {
                    "sector_code": info.code,
                    "stock_code": stock_code,
                    "weight": weight,
                }
            )
        if sleep_seconds > 0 and idx < len(infos):
            time.sleep(float(sleep_seconds))
    if not rows:
        return pd.DataFrame(columns=["sector_code", "stock_code", "weight"]), errors
    out = pd.DataFrame(rows).drop_duplicates(["sector_code", "stock_code"], keep="last").reset_index(drop=True)
    return out, errors


def load_stock_universe() -> set[str]:
    frames = [
        clickhouse_query_df("SELECT code FROM stocks WHERE type IN ('stock', 'index') AND quit = 0"),
        clickhouse_query_df("SELECT DISTINCT code FROM kline_daily"),
    ]
    codes: set[str] = set()
    for df in frames:
        if df.empty:
            continue
        codes.update(str(item).strip().upper() for item in df["code"].tolist() if str(item).strip())
    return codes


def load_official_stock_classification() -> pd.DataFrame:
    url = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"
    session = requests.Session()
    session.trust_env = False
    response = session.get(url, timeout=60, verify=False, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    raw = pd.read_excel(io.BytesIO(response.content))
    cols = list(raw.columns)
    if len(cols) < 3:
        raise RuntimeError(f"unexpected official stock classification columns: {cols}")
    out = pd.DataFrame(
        {
            "stock_code": raw[cols[0]].map(_canonical_stock_code),
            "in_date": pd.to_datetime(raw[cols[1]], errors="coerce"),
            "industry_code": raw[cols[2]].astype(str).str.zfill(6),
        }
    )
    out = out.dropna(subset=["stock_code", "in_date"]).copy()
    out = out.sort_values(["stock_code", "in_date"]).groupby("stock_code", as_index=False).tail(1)
    out["sw2_prefix"] = out["industry_code"].str[:4]
    return out.reset_index(drop=True)


def infer_sw2_prefix_map_from_current(classification: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    current = clickhouse_query_df(
        """
        SELECT s.code AS sector_code, s.name AS sector_name, ss.stock_code AS stock_code
        FROM sectors s
        INNER JOIN sector_stocks ss ON s.code = ss.sector_code
        WHERE s.type = 'industry'
          AND s.level = 2
        """
    )
    if current.empty:
        raise RuntimeError("current level-2 sector_stocks is empty; cannot infer SW2 prefix map")
    merged = current.merge(classification[["stock_code", "sw2_prefix"]], on="stock_code", how="left")
    merged = merged.dropna(subset=["sw2_prefix"]).copy()
    if merged.empty:
        raise RuntimeError("no current sector members matched official SW classification")
    counts = (
        merged.groupby(["sector_code", "sector_name", "sw2_prefix"], as_index=False)
        .size()
        .rename(columns={"size": "matched_count"})
        .sort_values(["sector_code", "matched_count"], ascending=[True, False])
    )
    top = counts.groupby(["sector_code", "sector_name"], as_index=False).head(1).copy()
    duplicate_prefixes = top[top["sw2_prefix"].duplicated(keep=False)].sort_values("sw2_prefix")
    resolved_duplicates: list[dict[str, Any]] = []
    if not duplicate_prefixes.empty:
        try:
            import akshare as ak  # type: ignore

            info = ak.sw_index_second_info()
            info_cols = list(info.columns)
            official_by_name = {
                _clean_sw_name(row.get(info_cols[1])): str(row.get(info_cols[0]) or "").replace(".SI", "")
                for _, row in info.iterrows()
            }
            for idx, item in duplicate_prefixes.iterrows():
                sector_name = str(item["sector_name"])
                symbol = official_by_name.get(_clean_sw_name(sector_name))
                if not symbol:
                    continue
                try:
                    comp = ak.index_component_sw(symbol=symbol)
                except Exception:
                    continue
                if comp is None or comp.empty:
                    continue
                comp_cols = list(comp.columns)
                comp_codes = pd.DataFrame({"stock_code": comp[comp_cols[1]].map(_canonical_stock_code)}).dropna()
                matched = comp_codes.merge(classification[["stock_code", "sw2_prefix"]], on="stock_code", how="left")
                matched = matched.dropna(subset=["sw2_prefix"])
                if matched.empty:
                    continue
                prefix = str(matched["sw2_prefix"].value_counts().idxmax())
                top.at[idx, "sw2_prefix"] = prefix
                top.at[idx, "matched_count"] = int(matched["sw2_prefix"].eq(prefix).sum())
                resolved_duplicates.append({"sector_code": str(item["sector_code"]), "sector_name": sector_name, "sw2_prefix": prefix})
            duplicate_prefixes = top[top["sw2_prefix"].duplicated(keep=False)].sort_values("sw2_prefix")
        except Exception:
            duplicate_prefixes = top[top["sw2_prefix"].duplicated(keep=False)].sort_values("sw2_prefix")
    summary = {
        "current_sector_count": int(current["sector_code"].nunique()),
        "inferred_sector_count": int(len(top)),
        "duplicate_prefix_count": int(duplicate_prefixes["sw2_prefix"].nunique()) if not duplicate_prefixes.empty else 0,
        "duplicates": duplicate_prefixes[["sector_code", "sector_name", "sw2_prefix", "matched_count"]].head(20).to_dict("records"),
        "resolved_duplicates": resolved_duplicates,
    }
    if summary["duplicate_prefix_count"] > 0:
        raise RuntimeError(f"ambiguous SW2 prefix inference: {summary}")
    return top[["sector_code", "sector_name", "sw2_prefix"]].reset_index(drop=True), summary


def build_sw2_from_official_classification(classification: pd.DataFrame, universe: set[str]) -> tuple[list[SectorInfo], pd.DataFrame, dict[str, Any]]:
    prefix_map, infer_summary = infer_sw2_prefix_map_from_current(classification)
    scoped = classification[classification["sw2_prefix"].isin(set(prefix_map["sw2_prefix"].astype(str)))].copy()
    before = len(scoped)
    if universe:
        scoped = scoped[scoped["stock_code"].astype(str).str.upper().isin(universe)].copy()
    by_prefix = {
        str(row["sw2_prefix"]): (str(row["sector_code"]), str(row["sector_name"]))
        for _, row in prefix_map.iterrows()
    }
    member_rows: list[dict[str, Any]] = []
    for _, row in scoped.iterrows():
        sector = by_prefix.get(str(row["sw2_prefix"]))
        if not sector:
            continue
        member_rows.append({"sector_code": sector[0], "stock_code": str(row["stock_code"]), "weight": 0.0})
    members = pd.DataFrame(member_rows).drop_duplicates(["sector_code", "stock_code"], keep="last")
    counts = members.groupby("sector_code")["stock_code"].nunique() if not members.empty else pd.Series(dtype=int)
    infos = [
        SectorInfo(
            code=str(row["sector_code"]),
            name=str(row["sector_name"]),
            level=2,
            official_code=str(row["sw2_prefix"]),
            parent_name=None,
            official_count=int(counts.get(str(row["sector_code"]), 0)),
        )
        for _, row in prefix_map.iterrows()
    ]
    summary = {
        **infer_summary,
        "classification_rows_before_universe_filter": int(before),
        "classification_rows": int(len(scoped)),
        "member_rows": int(len(members)),
    }
    return infos, members, summary


def overlay_component_successes(infos: list[SectorInfo], members: pd.DataFrame, sleep_seconds: float) -> tuple[pd.DataFrame, dict[str, Any]]:
    official_infos = load_official_sector_infos([2])
    official_by_code = {info.code: info for info in official_infos}
    valid_sector_codes = {info.code for info in infos}
    component_rows, errors = fetch_component_rows(
        [info for info in official_infos if info.code in valid_sector_codes],
        sleep_seconds=sleep_seconds,
        retries=2,
    )
    if component_rows.empty:
        return members, {"overlay_sector_count": 0, "overlay_rows": 0, "errors": errors[:20]}
    overlay_codes = set(component_rows["sector_code"].astype(str).unique())
    base = members[~members["sector_code"].astype(str).isin(overlay_codes)].copy()
    out = pd.concat([base, component_rows], ignore_index=True).drop_duplicates(["sector_code", "stock_code"], keep="last")
    overlay_counts = {code: int(component_rows[component_rows["sector_code"].eq(code)]["stock_code"].nunique()) for code in sorted(overlay_codes)}
    focus_counts = {
        code: overlay_counts.get(code)
        for code in ["qmt:SW2半导体", "qmt:SW2元件", "qmt:SW2自动化设备"]
        if code in valid_sector_codes
    }
    return out, {
        "overlay_sector_count": int(len(overlay_codes)),
        "overlay_rows": int(len(component_rows)),
        "focus_overlay_counts": focus_counts,
        "errors": errors[:20],
        "official_count_sample": {code: official_by_code[code].official_count for code in focus_counts if code in official_by_code},
    }


def build_sector_rows(infos: list[SectorInfo], mapped_counts: pd.Series) -> list[list[Any]]:
    now = datetime.now()
    by_level_name = {(info.level, info.name): info.code for info in infos}
    rows: list[list[Any]] = []
    for info in infos:
        parent_code = None
        if info.level == 2 and info.parent_name:
            parent_code = by_level_name.get((1, info.parent_name))
        elif info.level == 3 and info.parent_name:
            parent_code = by_level_name.get((2, info.parent_name))
        rows.append(
            [
                info.code,
                info.name,
                "industry",
                parent_code,
                int(info.level),
                int(mapped_counts.get(info.code, 0)),
                now,
            ]
        )
    return rows


def write_official_sw_tables(infos: list[SectorInfo], member_rows: pd.DataFrame, execute: bool) -> None:
    if not execute:
        return
    if not infos:
        raise RuntimeError("no official Shenwan sectors to write")
    if member_rows.empty:
        raise RuntimeError("no official Shenwan sector members to write")

    client = clickhouse_client()
    now = datetime.now()
    member_rows = member_rows.copy()
    member_rows["created_at"] = now
    mapped_counts = member_rows.groupby("sector_code")["stock_code"].nunique()
    sector_rows = build_sector_rows(infos, mapped_counts)

    client.command("DROP TABLE IF EXISTS sectors_sw_official_tmp")
    client.command("DROP TABLE IF EXISTS sector_stocks_sw_official_tmp")
    client.command("DROP TABLE IF EXISTS sectors_sw_official_backup")
    client.command("DROP TABLE IF EXISTS sector_stocks_sw_official_backup")
    client.command("CREATE TABLE sectors_sw_official_tmp AS sectors")
    client.command("CREATE TABLE sector_stocks_sw_official_tmp AS sector_stocks")

    client.insert(
        "sectors_sw_official_tmp",
        sector_rows,
        column_names=["code", "name", "type", "parent_code", "level", "stock_count", "created_at"],
    )
    client.insert_df(
        "sector_stocks_sw_official_tmp",
        member_rows[["sector_code", "stock_code", "weight", "created_at"]],
        column_names=["sector_code", "stock_code", "weight", "created_at"],
    )
    client.command(
        """
        RENAME TABLE
            sectors TO sectors_sw_official_backup,
            sectors_sw_official_tmp TO sectors,
            sector_stocks TO sector_stocks_sw_official_backup,
            sector_stocks_sw_official_tmp TO sector_stocks
        """
    )
    client.command("DROP TABLE IF EXISTS sectors_sw_official_backup")
    client.command("DROP TABLE IF EXISTS sector_stocks_sw_official_backup")


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild sectors/sector_stocks from official Shenwan index components.")
    parser.add_argument("--levels", default="1,2,3")
    parser.add_argument("--source", choices=["components", "classification"], default="classification")
    parser.add_argument("--sleep-seconds", type=float, default=0.03)
    parser.add_argument("--allow-missing-ratio", type=float, default=0.03)
    parser.add_argument("--filter-universe", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--overlay-components", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    levels = _parse_levels(args.levels)
    universe = load_stock_universe()
    errors: list[str] = []
    source_summary: dict[str, Any] = {}
    if args.source == "classification":
        if levels != [2]:
            raise RuntimeError("classification source currently supports --levels 2 only")
        classification = load_official_stock_classification()
        infos, components, source_summary = build_sw2_from_official_classification(
            classification,
            universe if args.filter_universe else set(),
        )
        if args.overlay_components:
            components, overlay_summary = overlay_component_successes(infos, components, sleep_seconds=float(args.sleep_seconds))
            source_summary["component_overlay"] = overlay_summary
        before_count = int(source_summary["classification_rows_before_universe_filter"])
    else:
        infos = load_official_sector_infos(levels)
        components, errors = fetch_component_rows(infos, sleep_seconds=float(args.sleep_seconds))
        before_count = len(components)
        if args.filter_universe and universe:
            components = components[components["stock_code"].astype(str).str.upper().isin(universe)].copy()
    missing_ratio = 0.0 if before_count == 0 else 1.0 - (len(components) / before_count)
    if args.filter_universe and missing_ratio > float(args.allow_missing_ratio):
        raise RuntimeError(
            f"official SW component universe mismatch too high: missing_ratio={missing_ratio:.4f}, "
            f"before={before_count}, after={len(components)}"
        )

    counts = components.groupby("sector_code")["stock_code"].nunique() if not components.empty else pd.Series(dtype=int)
    focus = {
        code: int(counts.get(code, 0))
        for code in ["qmt:SW2半导体", "qmt:SW2元件", "qmt:SW2自动化设备"]
        if code in {info.code for info in infos}
    }
    summary = {
        "execute": bool(args.execute),
        "levels": levels,
        "source": args.source,
        "official_sector_count": len(infos),
        "component_rows_before_universe_filter": int(before_count),
        "component_rows": int(len(components)),
        "unique_stock_count": int(components["stock_code"].nunique()) if not components.empty else 0,
        "universe_count": int(len(universe)),
        "missing_ratio": round(float(missing_ratio), 6),
        "focus_counts": focus,
        "error_count": len(errors),
        "errors_sample": errors[:10],
        "source_summary": source_summary,
    }
    print(summary)
    write_official_sw_tables(infos, components, execute=bool(args.execute))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
