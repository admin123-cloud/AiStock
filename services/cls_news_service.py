from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from data_fetcher.sources.cls_telegraph import ClsTelegraphClient
from utils.logger import get_logger
from utils.market_warehouse import clickhouse_client, clickhouse_query_df, clickhouse_table_exists
from utils.paths import cache_path, report_path


logger = get_logger("cls_news")

RAW_TABLE = "cls_news_raw"
SIGNAL_TABLE = "cls_news_signal"
SH_TZ = ZoneInfo("Asia/Shanghai")
STATE_PATH = cache_path("cls_news", "state.json")
G2_LIVE_SOURCE = report_path("gen2_v2_live_smoke_current", "source.parquet")
MAINLINE_THEME_POOL = report_path("mainline_theme_observation_pool_v1", "observation_pool.csv")
MAINLINE_THEME_ADDON = report_path("mainline_theme_strategy_overlay_v1", "theme_addon.csv")
MAINLINE_SECTOR_OVERLAY = report_path("mainline_intraday_candidate_overlay", "mainline_candidate_overlay.csv")
_STOCK_NAME_CACHE: Dict[str, Any] = {"loaded_at": None, "items": []}
GENERIC_STOCK_NAME_BLOCKLIST = {
    "太平洋",
    "中国",
    "中信",
    "国信",
    "东方",
    "西部",
    "南方",
    "北方",
    "机器人",
    "人民网",
}

HIGH_IMPACT_KEYWORDS = [
    "国务院",
    "中央",
    "政治局",
    "发改委",
    "工信部",
    "财政部",
    "央行",
    "证监会",
    "商务部",
    "国资委",
    "印发",
    "发布",
    "审议通过",
    "启动",
    "试点",
    "突破",
    "涨价",
    "中标",
    "订单",
    "并购",
    "重组",
    "回购",
    "增持",
]
THEME_KEYWORDS = {
    "人工智能": ["AI", "人工智能", "大模型", "算力", "数据中心", "机器人", "智能体"],
    "半导体": ["半导体", "芯片", "晶圆", "存储", "光刻", "封测"],
    "新能源": ["新能源", "光伏", "储能", "锂电", "风电", "氢能"],
    "低空经济": ["低空", "eVTOL", "无人机", "飞行汽车"],
    "军工": ["军工", "卫星", "商业航天", "导弹", "航空发动机"],
    "医药": ["创新药", "医药", "疫苗", "医疗器械", "生物"],
    "消费": ["消费", "零售", "旅游", "免税", "食品饮料"],
    "资源": ["有色", "铜", "铝", "黄金", "稀土", "煤炭", "石油"],
}
NOISE_KEYWORDS = ["体育", "足球", "天气", "地震", "海外央行", "欧洲央行", "美联储"]
STOCK_CODE_RE = re.compile(r"(?<!\d)([036]\d{5}|[48]\d{5})(?!\d)")


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else "", ensure_ascii=False, separators=(",", ":"))


def _read_state() -> Dict[str, Any]:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"Read CLS news state failed: {exc}")
    return {}


def _write_state(payload: Dict[str, Any]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception as exc:
        logger.warning(f"Write CLS news state failed: {exc}")


def _json_safe_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, pd.Timestamp):
        if value.tzinfo is None:
            value = value.tz_localize(SH_TZ)
        return value.isoformat()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=SH_TZ)
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return value


def _records_json_safe(df: Optional[pd.DataFrame]) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    records = df.to_dict(orient="records")
    return [{key: _json_safe_value(value) for key, value in row.items()} for row in records]


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return default
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _short_text(value: Any, length: int = 80) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= length else text[:length] + "..."


def _code6(code: Any) -> str:
    text = str(code or "").strip().upper()
    if "." in text:
        text = text.split(".", 1)[0]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6) if digits else ""


def _event_time_from_ctime(ctime: Any) -> datetime:
    ts = _safe_int(ctime)
    if ts <= 0:
        return datetime.now(SH_TZ).replace(tzinfo=None)
    return datetime.fromtimestamp(ts, tz=SH_TZ).replace(tzinfo=None)


def _normalize_code(raw: Any) -> Optional[str]:
    text = str(raw or "").strip().upper()
    if not text:
        return None
    if "." in text:
        code, market = text.split(".", 1)
        digits = "".join(ch for ch in code if ch.isdigit())
        market = market[:2]
        if len(digits) == 6 and market in {"SH", "SZ", "BJ"}:
            return f"{digits}.{market}"
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) != 6:
        return None
    if digits.startswith(("6", "9")):
        return f"{digits}.SH"
    if digits.startswith(("0", "3")):
        return f"{digits}.SZ"
    if digits.startswith(("4", "8")):
        return f"{digits}.BJ"
    return None


def _load_stock_name_items() -> List[Dict[str, str]]:
    loaded_at = _STOCK_NAME_CACHE.get("loaded_at")
    if loaded_at and (datetime.now(SH_TZ) - loaded_at).total_seconds() < 3600:
        return list(_STOCK_NAME_CACHE.get("items") or [])
    if not clickhouse_table_exists("stocks"):
        return []
    try:
        df = clickhouse_query_df(
            """
            SELECT code, name
            FROM stocks
            WHERE type = 'stock'
              AND code IS NOT NULL
              AND name IS NOT NULL
              AND length(name) >= 2
              AND (quit = 0 OR quit IS NULL)
            """
        )
    except Exception as exc:
        logger.warning(f"Load stock names for CLS matching failed: {exc}")
        return []
    items: List[Dict[str, str]] = []
    if df is not None and not df.empty:
        for row in df.itertuples(index=False):
            code = _normalize_code(getattr(row, "code", ""))
            name = str(getattr(row, "name", "") or "").strip()
            if (
                code
                and name
                and len(name) >= 3
                and name not in GENERIC_STOCK_NAME_BLOCKLIST
                and not name.upper().startswith(("ST", "*ST"))
            ):
                items.append({"code": code, "name": name})
    _STOCK_NAME_CACHE["loaded_at"] = datetime.now(SH_TZ)
    _STOCK_NAME_CACHE["items"] = items
    return items


def _extract_stock_codes(row: Dict[str, Any]) -> List[str]:
    codes: List[str] = []
    stocks = row.get("stocks")
    if isinstance(stocks, list):
        for item in stocks:
            if isinstance(item, dict):
                for key in ["code", "stock_code", "symbol", "secu_code"]:
                    code = _normalize_code(item.get(key))
                    if code:
                        codes.append(code)
            else:
                code = _normalize_code(item)
                if code:
                    codes.append(code)
    title_text = str(row.get("title") or "")
    text = str(row.get("content") or "") + " " + title_text
    for match in STOCK_CODE_RE.findall(text):
        code = _normalize_code(match)
        if code:
            codes.append(code)
    if len(codes) < 8:
        for item in _load_stock_name_items():
            if item["name"] in title_text:
                codes.append(item["code"])
                if len(set(codes)) >= 8:
                    break
    return sorted(set(codes))


def _extract_subjects(row: Dict[str, Any]) -> List[str]:
    subjects = row.get("subjects")
    names: List[str] = []
    if isinstance(subjects, list):
        for item in subjects:
            if isinstance(item, dict):
                name = str(item.get("subject_name") or "").strip()
                if name:
                    names.append(name)
    return sorted(set(names))


def _extract_themes(text: str, subjects: Iterable[str]) -> List[str]:
    joined = text + " " + " ".join(subjects)
    themes = []
    for theme, keywords in THEME_KEYWORDS.items():
        if any(keyword in joined for keyword in keywords):
            themes.append(theme)
    return themes


def ensure_cls_news_tables() -> None:
    client = clickhouse_client()
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {RAW_TABLE}
        (
            id UInt64,
            event_time DateTime('Asia/Shanghai'),
            event_date Date,
            title String,
            content String,
            brief String,
            level String,
            reading_num UInt64,
            subjects_json String,
            stocks_json String,
            share_url String,
            source String,
            raw_json String,
            created_at DateTime('Asia/Shanghai'),
            updated_at DateTime('Asia/Shanghai')
        )
        ENGINE = ReplacingMergeTree(updated_at)
        PARTITION BY event_date
        ORDER BY (event_date, id)
        """
    )
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {SIGNAL_TABLE}
        (
            id UInt64,
            event_time DateTime('Asia/Shanghai'),
            event_date Date,
            title String,
            content String,
            level String,
            subjects_text String,
            themes_text String,
            related_codes String,
            event_score Float64,
            pre_position_score Float64,
            post_confirm_score Float64,
            chase_risk_score Float64,
            mainline_theme_score Float64,
            strategy_overlap_score Float64 DEFAULT 0,
            strategy_overlap_codes String DEFAULT '',
            strategy_overlap_names String DEFAULT '',
            theme_overlap_score Float64 DEFAULT 0,
            theme_overlap_labels String DEFAULT '',
            opportunity_score Float64,
            signal_class String,
            reason String,
            updated_at DateTime('Asia/Shanghai')
        )
        ENGINE = ReplacingMergeTree(updated_at)
        PARTITION BY event_date
        ORDER BY (event_date, id)
        """
    )
    for table in [RAW_TABLE, SIGNAL_TABLE]:
        for column in ["event_time", "created_at", "updated_at"]:
            if table == SIGNAL_TABLE and column == "created_at":
                continue
            if not _column_needs_timezone_alter(table, column):
                continue
            try:
                client.command(f"ALTER TABLE {table} MODIFY COLUMN {column} DateTime('Asia/Shanghai')")
            except Exception as exc:
                logger.debug(f"Skip CLS table timezone alter: table={table}, column={column}, error={exc}")
    _ensure_signal_extra_columns(client)


def _ensure_signal_extra_columns(client: Any) -> None:
    extra_columns = {
        "strategy_overlap_score": "Float64 DEFAULT 0",
        "strategy_overlap_codes": "String DEFAULT ''",
        "strategy_overlap_names": "String DEFAULT ''",
        "theme_overlap_score": "Float64 DEFAULT 0",
        "theme_overlap_labels": "String DEFAULT ''",
    }
    for column, ddl in extra_columns.items():
        try:
            client.command(f"ALTER TABLE {SIGNAL_TABLE} ADD COLUMN IF NOT EXISTS {column} {ddl}")
        except Exception as exc:
            logger.debug(f"Skip CLS extra column add: column={column}, error={exc}")


def _column_needs_timezone_alter(table: str, column: str) -> bool:
    try:
        df = clickhouse_query_df(
            """
            SELECT type
            FROM system.columns
            WHERE database = currentDatabase()
              AND table = ?
              AND name = ?
            LIMIT 1
            """,
            [table, column],
        )
        if df is None or df.empty:
            return False
        col_type = str(df.iloc[0]["type"])
        return col_type == "DateTime"
    except Exception:
        return False


def normalize_cls_row(row: Dict[str, Any]) -> Dict[str, Any]:
    event_time = _event_time_from_ctime(row.get("ctime"))
    title = str(row.get("title") or "").strip()
    content = str(row.get("content") or row.get("brief") or "").strip()
    return {
        "id": _safe_int(row.get("id")),
        "event_time": event_time,
        "event_date": event_time.date(),
        "title": title,
        "content": content,
        "brief": str(row.get("brief") or content).strip(),
        "level": str(row.get("level") or "").strip(),
        "reading_num": _safe_int(row.get("reading_num")),
        "subjects_json": _json_dumps(row.get("subjects") or []),
        "stocks_json": _json_dumps(row.get("stocks") or []),
        "share_url": str(row.get("shareurl") or row.get("share_url") or ""),
        "source": "cls_web_roll",
        "raw_json": _json_dumps(row),
        "created_at": datetime.now(SH_TZ).replace(tzinfo=None),
        "updated_at": datetime.now(SH_TZ).replace(tzinfo=None),
        "_raw": row,
    }


def _insert_raw(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    clickhouse_client().insert(
        RAW_TABLE,
        [
            (
                row["id"],
                row["event_time"],
                row["event_date"],
                row["title"],
                row["content"],
                row["brief"],
                row["level"],
                row["reading_num"],
                row["subjects_json"],
                row["stocks_json"],
                row["share_url"],
                row["source"],
                row["raw_json"],
                row["created_at"],
                row["updated_at"],
            )
            for row in rows
        ],
        column_names=[
            "id",
            "event_time",
            "event_date",
            "title",
            "content",
            "brief",
            "level",
            "reading_num",
            "subjects_json",
            "stocks_json",
            "share_url",
            "source",
            "raw_json",
            "created_at",
            "updated_at",
        ],
    )


def _quote(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _window_quote_metrics(codes: List[str], event_time: datetime) -> Tuple[float, float, float]:
    if not codes or not clickhouse_table_exists("intraday_quote_snapshot"):
        return 0.0, 0.0, 0.0
    code_sql = ",".join(_quote(code) for code in codes)
    event_text = event_time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        df = clickhouse_query_df(
            f"""
            SELECT code, snapshot_time, price, amount
            FROM intraday_quote_snapshot
            WHERE code IN ({code_sql})
              AND snapshot_time >= toDateTime({_quote(event_text)}) - INTERVAL 60 MINUTE
              AND snapshot_time <= toDateTime({_quote(event_text)}) + INTERVAL 30 MINUTE
            ORDER BY code, snapshot_time
            """
        )
    except Exception as exc:
        logger.warning(f"CLS quote window query failed: {exc}")
        return 0.0, 0.0, 0.0
    if df is None or df.empty:
        return 0.0, 0.0, 0.0
    df["snapshot_time"] = pd.to_datetime(df["snapshot_time"], errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df = df.dropna(subset=["snapshot_time", "price"])
    if df.empty:
        return 0.0, 0.0, 0.0

    pre_scores: List[float] = []
    post_scores: List[float] = []
    chase_scores: List[float] = []
    event_ts = pd.Timestamp(event_time)
    for _, group in df.groupby("code"):
        g = group.sort_values("snapshot_time")
        pre = g[(g["snapshot_time"] >= event_ts - pd.Timedelta(minutes=60)) & (g["snapshot_time"] <= event_ts)]
        post = g[(g["snapshot_time"] >= event_ts) & (g["snapshot_time"] <= event_ts + pd.Timedelta(minutes=30))]
        if len(pre) >= 2:
            first = _safe_float(pre.iloc[0]["price"])
            last = _safe_float(pre.iloc[-1]["price"])
            if first > 0:
                pre_ret = (last / first - 1.0) * 100.0
                pre_amount_delta = max(0.0, _safe_float(pre.iloc[-1]["amount"]) - _safe_float(pre.iloc[0]["amount"]))
                pre_scores.append(min(100.0, max(0.0, pre_ret * 12.0 + math.log1p(pre_amount_delta / 1e8) * 8.0)))
                if pre_ret >= 5:
                    chase_scores.append(min(100.0, pre_ret * 10.0))
        if len(post) >= 2:
            first = _safe_float(post.iloc[0]["price"])
            last = _safe_float(post.iloc[-1]["price"])
            if first > 0:
                post_ret = (last / first - 1.0) * 100.0
                post_amount_delta = max(0.0, _safe_float(post.iloc[-1]["amount"]) - _safe_float(post.iloc[0]["amount"]))
                post_scores.append(min(100.0, max(0.0, post_ret * 14.0 + math.log1p(post_amount_delta / 1e8) * 8.0)))
                if post_ret <= -2:
                    chase_scores.append(min(100.0, abs(post_ret) * 15.0))

    return (
        round(float(sum(pre_scores) / len(pre_scores)), 2) if pre_scores else 0.0,
        round(float(sum(post_scores) / len(post_scores)), 2) if post_scores else 0.0,
        round(float(max(chase_scores)), 2) if chase_scores else 0.0,
    )


def _read_table_file(path: Path) -> pd.DataFrame:
    try:
        if not path.exists():
            return pd.DataFrame()
        if path.suffix.lower() == ".parquet":
            return pd.read_parquet(path)
        return pd.read_csv(path, encoding="utf-8-sig")
    except Exception as exc:
        logger.warning(f"Read CLS context artifact failed: path={path}, error={exc}")
        return pd.DataFrame()


def _safe_col(df: pd.DataFrame, names: List[str]) -> Optional[str]:
    for name in names:
        if name in df.columns:
            return name
    return None


def _load_strategy_context() -> Dict[str, Dict[str, Any]]:
    frames: List[pd.DataFrame] = []
    sources = [
        ("g2_live", G2_LIVE_SOURCE),
        ("mainline_sector", MAINLINE_SECTOR_OVERLAY),
        ("mainline_theme_addon", MAINLINE_THEME_ADDON),
    ]
    for source, path in sources:
        df = _read_table_file(path)
        if df.empty:
            continue
        code_col = _safe_col(df, ["code", "code6", "stock_code", "stock_code6", "symbol"])
        if not code_col:
            continue
        name_col = _safe_col(df, ["name", "display_name", "stock_name"])
        score_col = _safe_col(df, ["score", "overlay_score", "candidate_score", "theme_candidate_score", "mainline_intraday_score", "v4_score"])
        label_col = _safe_col(df, ["strategy_source", "candidate_source", "source_family", "signal_family", "theme_label", "sector_name"])
        out = pd.DataFrame()
        out["code6"] = df[code_col].map(_code6)
        out["name"] = df[name_col].astype(str) if name_col else ""
        out["score"] = pd.to_numeric(df[score_col], errors="coerce") if score_col else 0.0
        out["label"] = df[label_col].astype(str) if label_col else source
        out["source"] = source
        frames.append(out[out["code6"].astype(bool)])
    if not frames:
        return {}
    merged = pd.concat(frames, ignore_index=True)
    context: Dict[str, Dict[str, Any]] = {}
    for code6, group in merged.groupby("code6"):
        score = pd.to_numeric(group["score"], errors="coerce").fillna(0)
        labels = [str(item) for item in group["label"].dropna().astype(str).tolist() if item and item.lower() != "nan"]
        names = [str(item) for item in group["name"].dropna().astype(str).tolist() if item and item.lower() != "nan"]
        sources_text = [str(item) for item in group["source"].dropna().astype(str).tolist()]
        context[str(code6)] = {
            "score": float(min(100.0, max(20.0, score.max() if not score.empty else 20.0))),
            "name": names[0] if names else "",
            "labels": sorted(set(labels))[:5],
            "sources": sorted(set(sources_text))[:5],
        }
    return context


def _load_theme_context() -> Dict[str, float]:
    df = _read_table_file(MAINLINE_THEME_POOL)
    if df.empty:
        return {}
    label_col = _safe_col(df, ["theme_label", "mainline_theme_label"])
    score_col = _safe_col(df, ["theme_candidate_score", "mainline_theme_score", "theme_score", "candidate_score"])
    if not label_col:
        return {}
    labels = df[label_col].dropna().astype(str)
    scores = pd.to_numeric(df[score_col], errors="coerce").fillna(30.0) if score_col else pd.Series([30.0] * len(df))
    context: Dict[str, float] = {}
    for label, score in zip(labels, scores):
        label = str(label).strip()
        if not label:
            continue
        context[label] = max(context.get(label, 0.0), float(min(100.0, max(20.0, score))))
    return context


def _context_overlap(codes: List[str], themes: List[str], subjects: List[str]) -> Dict[str, Any]:
    strategy_context = _load_strategy_context()
    matched_codes: List[str] = []
    matched_names: List[str] = []
    strategy_scores: List[float] = []
    for code in codes:
        code6 = _code6(code)
        item = strategy_context.get(code6)
        if not item:
            continue
        matched_codes.append(code)
        matched_names.append(item.get("name") or ",".join(item.get("labels") or []) or code)
        strategy_scores.append(_safe_float(item.get("score"), 20.0))

    theme_context = _load_theme_context()
    matched_theme_labels: List[str] = []
    theme_scores: List[float] = []
    haystack = " ".join(list(themes) + list(subjects))
    for label, score in theme_context.items():
        if label and (label in haystack or any(part and part in label for part in themes)):
            matched_theme_labels.append(label)
            theme_scores.append(score)

    return {
        "strategy_overlap_score": round(float(max(strategy_scores)), 2) if strategy_scores else 0.0,
        "strategy_overlap_codes": ",".join(sorted(set(matched_codes))),
        "strategy_overlap_names": ",".join(sorted(set(matched_names))[:6]),
        "theme_overlap_score": round(float(max(theme_scores)), 2) if theme_scores else 0.0,
        "theme_overlap_labels": ",".join(sorted(set(matched_theme_labels))[:6]),
    }


def _score_event(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = row["_raw"]
    text = f"{row['title']} {row['content']}"
    subjects = _extract_subjects(raw)
    codes = _extract_stock_codes(raw)
    themes = _extract_themes(text, subjects)

    score = 20.0
    reasons: List[str] = []
    if row["level"].upper() in {"A", "B"}:
        score += 20
        reasons.append(f"level={row['level']}")
    keyword_hits = [kw for kw in HIGH_IMPACT_KEYWORDS if kw in text]
    if keyword_hits:
        score += min(30.0, len(keyword_hits) * 8.0)
        reasons.append("高冲击关键词:" + ",".join(keyword_hits[:4]))
    if subjects:
        score += min(15.0, len(subjects) * 4.0)
        reasons.append("关联话题:" + ",".join(subjects[:3]))
    if codes:
        score += min(12.0, len(codes) * 4.0)
        reasons.append("关联个股:" + ",".join(codes[:5]))
    if themes:
        score += min(18.0, len(themes) * 8.0)
        reasons.append("题材:" + ",".join(themes[:4]))
    if row["reading_num"] >= 50000:
        score += 8
        reasons.append("阅读量较高")
    if any(keyword in text for keyword in NOISE_KEYWORDS) and not codes and not themes:
        score -= 18
        reasons.append("偏泛新闻/低A股映射")
    event_score = round(max(0.0, min(100.0, score)), 2)

    pre_score, post_score, quote_chase = _window_quote_metrics(codes, row["event_time"])
    context = _context_overlap(codes, themes, subjects)
    mainline_theme_score = min(
        100.0,
        len(themes) * 18.0
        + len(subjects) * 2.0
        + context["theme_overlap_score"] * 0.45
        + context["strategy_overlap_score"] * 0.25,
    )
    if context["strategy_overlap_codes"]:
        reasons.append("策略候选重合:" + context["strategy_overlap_names"])
    if context["theme_overlap_labels"]:
        reasons.append("主线题材重合:" + context["theme_overlap_labels"])
    chase_risk = max(quote_chase, 20.0 if pre_score >= 65 and post_score < 20 else 0.0)
    opportunity = round(
        max(
            0.0,
            min(
                100.0,
                0.30 * event_score
                + 0.25 * pre_score
                + 0.25 * post_score
                + 0.15 * mainline_theme_score
                + 0.10 * context["strategy_overlap_score"]
                + 0.08 * context["theme_overlap_score"]
                - 0.20 * chase_risk,
            ),
        ),
        2,
    )
    if opportunity >= 70 and post_score >= 40:
        signal_class = "A1 主升确认"
    elif opportunity >= 55:
        signal_class = "A2 可短线跟随"
    elif pre_score >= 60 and post_score < 20:
        signal_class = "C1 疑似兑现"
    elif event_score >= 45 or themes:
        signal_class = "B1 只观察"
    else:
        signal_class = "D1 噪音过滤"
    return {
        "id": row["id"],
        "event_time": row["event_time"],
        "event_date": row["event_date"],
        "title": row["title"],
        "content": row["content"],
        "level": row["level"],
        "subjects_text": ",".join(subjects),
        "themes_text": ",".join(themes),
        "related_codes": ",".join(codes),
        "event_score": event_score,
        "pre_position_score": pre_score,
        "post_confirm_score": post_score,
        "chase_risk_score": round(chase_risk, 2),
        "mainline_theme_score": round(mainline_theme_score, 2),
        "strategy_overlap_score": context["strategy_overlap_score"],
        "strategy_overlap_codes": context["strategy_overlap_codes"],
        "strategy_overlap_names": context["strategy_overlap_names"],
        "theme_overlap_score": context["theme_overlap_score"],
        "theme_overlap_labels": context["theme_overlap_labels"],
        "opportunity_score": opportunity,
        "signal_class": signal_class,
        "reason": "；".join(reasons[:8]),
        "updated_at": datetime.now(SH_TZ).replace(tzinfo=None),
    }


def _insert_signals(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    clickhouse_client().insert(
        SIGNAL_TABLE,
        [
            (
                row["id"],
                row["event_time"],
                row["event_date"],
                row["title"],
                row["content"],
                row["level"],
                row["subjects_text"],
                row["themes_text"],
                row["related_codes"],
                row["event_score"],
                row["pre_position_score"],
                row["post_confirm_score"],
                row["chase_risk_score"],
                row["mainline_theme_score"],
                row["strategy_overlap_score"],
                row["strategy_overlap_codes"],
                row["strategy_overlap_names"],
                row["theme_overlap_score"],
                row["theme_overlap_labels"],
                row["opportunity_score"],
                row["signal_class"],
                row["reason"],
                row["updated_at"],
            )
            for row in rows
        ],
        column_names=[
            "id",
            "event_time",
            "event_date",
            "title",
            "content",
            "level",
            "subjects_text",
            "themes_text",
            "related_codes",
            "event_score",
            "pre_position_score",
            "post_confirm_score",
            "chase_risk_score",
            "mainline_theme_score",
            "strategy_overlap_score",
            "strategy_overlap_codes",
            "strategy_overlap_names",
            "theme_overlap_score",
            "theme_overlap_labels",
            "opportunity_score",
            "signal_class",
            "reason",
            "updated_at",
        ],
    )


def fetch_store_and_score_cls_news(*, pages: int = 1, rn: int = 50) -> Dict[str, Any]:
    ensure_cls_news_tables()
    state = _read_state()
    previous_latest_id = _safe_int(state.get("latest_id"))
    client = ClsTelegraphClient()
    raw_rows = client.fetch_pages(pages=pages, rn=rn)
    rows = [normalize_cls_row(row) for row in raw_rows]
    rows = [row for row in rows if row["id"] > 0]
    new_rows = [row for row in rows if row["id"] > previous_latest_id]
    _insert_raw(rows)
    signals = [_score_event(row) for row in rows]
    _insert_signals(signals)
    latest_id = max([row["id"] for row in rows], default=previous_latest_id)
    latest_event_time = max([row["event_time"] for row in rows], default=None)
    result = {
        "ok": True,
        "source": "cls_web_roll",
        "fetched": len(raw_rows),
        "stored": len(rows),
        "new_rows": len(new_rows),
        "scored": len(signals),
        "latest_id": latest_id,
        "latest_event_time": latest_event_time,
        "state_path": str(STATE_PATH),
    }
    _write_state(
        {
            "latest_id": latest_id,
            "latest_event_time": latest_event_time.isoformat() if latest_event_time else state.get("latest_event_time"),
            "last_sync_at": datetime.now(SH_TZ).isoformat(),
            "last_result": {key: _json_safe_value(value) for key, value in result.items()},
        }
    )
    return result


def query_cls_latest(limit: int = 50) -> List[Dict[str, Any]]:
    ensure_cls_news_tables()
    n = max(1, min(int(limit), 200))
    df = clickhouse_query_df(
        f"""
        SELECT *
        FROM {RAW_TABLE} FINAL
        ORDER BY event_time DESC, id DESC
        LIMIT {n}
        """
    )
    return _records_json_safe(df)


def query_cls_signals(limit: int = 50, min_score: float = 0.0, signal_class: str = "") -> List[Dict[str, Any]]:
    ensure_cls_news_tables()
    n = max(1, min(int(limit), 200))
    filters = [f"opportunity_score >= {float(min_score)}"]
    if signal_class:
        filters.append(f"signal_class = {_quote(signal_class)}")
    where = " AND ".join(filters)
    df = clickhouse_query_df(
        f"""
        SELECT *
        FROM {SIGNAL_TABLE} FINAL
        WHERE {where}
        ORDER BY event_time DESC, opportunity_score DESC, id DESC
        LIMIT {n}
        """
    )
    return _records_json_safe(df)


def query_cls_radar_summary(limit: int = 50, top_events: int = 10, peer_limit: int = 12) -> Dict[str, Any]:
    signals = query_cls_signals(limit=limit, min_score=0.0)
    class_counts: Dict[str, int] = {}
    theme_counts: Dict[str, int] = {}
    hot_events: List[Dict[str, Any]] = []

    for signal in signals:
        signal_class = str(signal.get("signal_class") or "未分层")
        class_counts[signal_class] = class_counts.get(signal_class, 0) + 1
        for theme in str(signal.get("themes_text") or "").replace("，", ",").split(","):
            label = theme.strip()
            if label:
                theme_counts[label] = theme_counts.get(label, 0) + 1

    candidate_source = [
        row
        for row in signals
        if str(row.get("related_codes") or "").strip() or _safe_float(row.get("opportunity_score")) >= 20
    ][: max(1, min(int(top_events), 20))]

    setup_counts: Dict[str, int] = {}
    capital_counts: Dict[str, int] = {}
    for signal in candidate_source:
        event_id = int(signal.get("id") or 0)
        if event_id <= 0:
            continue
        try:
            candidate_payload = query_cls_event_candidates(event_id, limit=8, peer_limit=peer_limit)
        except Exception as exc:
            logger.warning(f"CLS radar candidate summary failed for {event_id}: {exc}")
            continue
        items = candidate_payload.get("items") or []
        if not items:
            continue
        for item in items:
            setup = str(item.get("setup_tag") or "未分类")
            capital = str(item.get("capital_tag") or "资金未知")
            setup_counts[setup] = setup_counts.get(setup, 0) + 1
            capital_counts[capital] = capital_counts.get(capital, 0) + 1
        best = items[0] if items else {}
        hot_events.append(
            {
                "event_id": event_id,
                "event_time": signal.get("event_time"),
                "title": signal.get("title") or _short_text(signal.get("content"), 80),
                "signal_class": signal.get("signal_class"),
                "opportunity_score": signal.get("opportunity_score"),
                "themes_text": signal.get("themes_text"),
                "related_codes": signal.get("related_codes"),
                "candidate_total": candidate_payload.get("summary", {}).get("total", 0),
                "best_code": best.get("code", ""),
                "best_name": best.get("name", ""),
                "best_score": best.get("candidate_score"),
                "best_setup_tag": best.get("setup_tag", ""),
                "best_capital_score": best.get("capital_proxy_score"),
                "best_capital_tag": best.get("capital_tag", ""),
                "best_action_tag": best.get("action_tag", ""),
                "best_risk_tag": best.get("risk_tag", ""),
            }
        )

    hot_events = sorted(
        hot_events,
        key=lambda item: (_safe_float(item.get("opportunity_score")), _safe_float(item.get("best_score"))),
        reverse=True,
    )
    return {
        "limit": int(limit),
        "signal_count": len(signals),
        "class_counts": dict(sorted(class_counts.items(), key=lambda kv: kv[1], reverse=True)),
        "theme_counts": dict(sorted(theme_counts.items(), key=lambda kv: kv[1], reverse=True)[:20]),
        "setup_counts": dict(sorted(setup_counts.items(), key=lambda kv: kv[1], reverse=True)),
        "capital_counts": dict(sorted(capital_counts.items(), key=lambda kv: kv[1], reverse=True)),
        "hot_events": hot_events,
    }


def query_cls_event(event_id: int) -> Dict[str, Any]:
    ensure_cls_news_tables()
    eid = int(event_id)
    raw_df = clickhouse_query_df(f"SELECT * FROM {RAW_TABLE} FINAL WHERE id = {eid} LIMIT 1")
    signal_df = clickhouse_query_df(f"SELECT * FROM {SIGNAL_TABLE} FINAL WHERE id = {eid} LIMIT 1")
    return {
        "raw": _records_json_safe(raw_df)[0] if raw_df is not None and not raw_df.empty else {},
        "signal": _records_json_safe(signal_df)[0] if signal_df is not None and not signal_df.empty else {},
    }


def _split_codes(text: Any) -> List[str]:
    codes: List[str] = []
    for item in str(text or "").replace("，", ",").split(","):
        code = _normalize_code(item.strip())
        if code:
            codes.append(code)
    return sorted(set(codes))


def _latest_daily_date() -> str:
    try:
        df = clickhouse_query_df("SELECT max(trade_date) AS trade_date FROM kline_daily")
        if df is not None and not df.empty:
            value = df.iloc[0]["trade_date"]
            return str(value)[:10]
    except Exception:
        pass
    return ""


def _sector_candidates_for_codes(codes: List[str], peer_limit: int) -> Tuple[Dict[str, List[str]], List[str]]:
    if not codes or not clickhouse_table_exists("sector_stocks") or not clickhouse_table_exists("sectors"):
        return {}, []
    code_sql = ",".join(_quote(code) for code in codes)
    try:
        sector_df = clickhouse_query_df(
            f"""
            SELECT ss.stock_code, ss.sector_code, s.name AS sector_name, s.level AS sector_level
            FROM sector_stocks ss
            ANY LEFT JOIN sectors s ON s.code = ss.sector_code
            WHERE ss.stock_code IN ({code_sql})
            ORDER BY ss.stock_code, s.level ASC, ss.sector_code
            """
        )
    except Exception as exc:
        logger.warning(f"CLS sector query failed: {exc}")
        return {}, []
    if sector_df is None or sector_df.empty:
        return {}, []

    sector_names_by_code: Dict[str, List[str]] = {}
    sector_codes = []
    for row in sector_df.itertuples(index=False):
        stock_code = str(getattr(row, "stock_code", "") or "")
        sector_code = str(getattr(row, "sector_code", "") or "")
        sector_name = str(getattr(row, "sector_name", "") or "")
        if stock_code and sector_name:
            sector_names_by_code.setdefault(stock_code, [])
            if sector_name not in sector_names_by_code[stock_code]:
                sector_names_by_code[stock_code].append(sector_name)
        if sector_code:
            sector_codes.append(sector_code)

    peer_codes: List[str] = []
    if sector_codes and peer_limit > 0:
        sector_sql = ",".join(_quote(code) for code in sorted(set(sector_codes)))
        direct_sql = ",".join(_quote(code) for code in codes)
        latest_date = _latest_daily_date()
        date_join = f"AND k.trade_date = toDate({_quote(latest_date)})" if latest_date else ""
        try:
            peer_df = clickhouse_query_df(
                f"""
                SELECT
                    ss.stock_code AS code,
                    any(s.name) AS sector_name,
                    max(k.change_pct) AS change_pct,
                    max(k.amount) AS amount
                FROM sector_stocks ss
                LEFT JOIN sectors s ON s.code = ss.sector_code
                LEFT JOIN kline_daily k
                    ON k.code = ss.stock_code
                   {date_join}
                WHERE ss.sector_code IN ({sector_sql})
                  AND ss.stock_code NOT IN ({direct_sql})
                GROUP BY ss.stock_code
                ORDER BY change_pct DESC NULLS LAST, amount DESC NULLS LAST
                LIMIT {max(1, int(peer_limit))}
                """
            )
            if peer_df is not None and not peer_df.empty:
                peer_codes = [_normalize_code(row.code) or "" for row in peer_df.itertuples(index=False)]
                peer_codes = [code for code in peer_codes if code]
                for row in peer_df.itertuples(index=False):
                    code = _normalize_code(row.code)
                    sector_name = str(getattr(row, "sector_name", "") or "")
                    if code and sector_name:
                        sector_names_by_code.setdefault(code, [])
                        if sector_name not in sector_names_by_code[code]:
                            sector_names_by_code[code].append(sector_name)
        except Exception as exc:
            logger.warning(f"CLS sector peer query failed: {exc}")
    return sector_names_by_code, sorted(set(peer_codes))


def _stock_snapshot_map(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    if not codes:
        return {}
    code_sql = ",".join(_quote(code) for code in codes)
    result: Dict[str, Dict[str, Any]] = {}
    latest_date = _latest_daily_date()
    date_filter = f"AND trade_date = toDate({_quote(latest_date)})" if latest_date else ""
    try:
        daily = clickhouse_query_df(
            f"""
            SELECT code, trade_date, close, change_pct, amount, turnover_rate
            FROM kline_daily
            WHERE code IN ({code_sql})
              {date_filter}
            """
        )
        if daily is not None and not daily.empty:
            for row in daily.itertuples(index=False):
                code = str(row.code)
                result.setdefault(code, {}).update(
                    {
                        "trade_date": str(row.trade_date)[:10],
                        "close": _safe_float(row.close),
                        "change_pct": _safe_float(row.change_pct),
                        "amount": _safe_float(row.amount),
                        "turnover_rate": _safe_float(row.turnover_rate),
                    }
                )
    except Exception as exc:
        logger.warning(f"CLS candidate daily query failed: {exc}")

    if clickhouse_table_exists("intraday_quote_snapshot"):
        try:
            snap_date_filter = f"AND snapshot_date = toDate({_quote(latest_date)})" if latest_date else ""
            snap = clickhouse_query_df(
                f"""
                SELECT
                    code,
                    argMax(name, snapshot_time) AS name,
                    argMax(price, snapshot_time) AS price,
                    argMax(open, snapshot_time) AS open,
                    argMax(pre_close, snapshot_time) AS pre_close,
                    argMax(amount, snapshot_time) AS snapshot_amount,
                    max(snapshot_time) AS latest_snapshot_time
                FROM intraday_quote_snapshot
                WHERE code IN ({code_sql})
                  {snap_date_filter}
                GROUP BY code
                """
            )
            if snap is not None and not snap.empty:
                for row in snap.itertuples(index=False):
                    code = str(row.code)
                    price = _safe_float(row.price)
                    pre_close = _safe_float(row.pre_close)
                    intraday_change = (price / pre_close - 1.0) * 100.0 if price > 0 and pre_close > 0 else None
                    result.setdefault(code, {}).update(
                        {
                            "name": str(row.name or ""),
                            "snapshot_price": price,
                            "snapshot_change_pct": round(intraday_change, 2) if intraday_change is not None else None,
                            "snapshot_amount": _safe_float(row.snapshot_amount),
                            "snapshot_time": _json_safe_value(row.latest_snapshot_time),
                        }
                    )
        except Exception as exc:
            logger.warning(f"CLS candidate snapshot query failed: {exc}")

    try:
        stock_df = clickhouse_query_df(
            f"""
            SELECT code, name, industry
            FROM stocks
            WHERE code IN ({code_sql})
            """
        )
        if stock_df is not None and not stock_df.empty:
            for row in stock_df.itertuples(index=False):
                code = str(row.code)
                result.setdefault(code, {}).update(
                    {
                        "name": str(row.name or result.get(code, {}).get("name") or ""),
                        "industry": str(row.industry or ""),
                    }
                )
    except Exception as exc:
        logger.warning(f"CLS candidate stock query failed: {exc}")
    return result


def _stock_capital_context_map(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    if not codes:
        return {}
    code_sql = ",".join(_quote(code) for code in codes)
    latest_date = _latest_daily_date()
    date_filter = f"AND trade_date <= toDate({_quote(latest_date)})" if latest_date else ""
    result: Dict[str, Dict[str, Any]] = {}
    try:
        df = clickhouse_query_df(
            f"""
            SELECT code, trade_date, close, change_pct, amount, turnover_rate
            FROM kline_daily
            WHERE code IN ({code_sql})
              {date_filter}
            ORDER BY code, trade_date DESC
            LIMIT 40 BY code
            """
        )
    except Exception as exc:
        logger.warning(f"CLS candidate capital context query failed: {exc}")
        return result
    if df is None or df.empty:
        return result

    for col in ["change_pct", "amount", "turnover_rate"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    for code, group in df.groupby("code"):
        g = group.sort_values("trade_date", ascending=False).reset_index(drop=True)
        if g.empty:
            continue
        latest = g.iloc[0]
        prev5 = g.iloc[1:6]
        prev20 = g.iloc[1:21]
        latest_amount = _safe_float(latest.get("amount"))
        avg5 = _safe_float(prev5["amount"].mean()) if not prev5.empty else 0.0
        avg20 = _safe_float(prev20["amount"].mean()) if not prev20.empty else 0.0
        amount_ratio5 = latest_amount / avg5 if avg5 > 0 else None
        amount_ratio20 = latest_amount / avg20 if avg20 > 0 else None
        pct = _safe_float(latest.get("change_pct"))
        turnover = _safe_float(latest.get("turnover_rate"))
        amount_yi = latest_amount / 1e4 if latest_amount else 0.0

        volume_score = 0.0
        if amount_ratio5 is not None:
            volume_score += max(0.0, min(38.0, (amount_ratio5 - 1.0) * 22.0))
        if amount_ratio20 is not None:
            volume_score += max(0.0, min(32.0, (amount_ratio20 - 1.0) * 16.0))
        liquidity_score = max(0.0, min(15.0, amount_yi / 2.0))
        turnover_score = max(0.0, min(15.0, turnover * 2.0))
        capital_proxy_score = round(max(0.0, min(100.0, volume_score + liquidity_score + turnover_score)), 2)

        if capital_proxy_score >= 65 and pct < 7:
            capital_tag = "资金提前活跃"
        elif capital_proxy_score >= 50 and 3 <= pct <= 9:
            capital_tag = "量价确认"
        elif pct >= 9 and capital_proxy_score >= 45:
            capital_tag = "放量追高"
        elif capital_proxy_score >= 35:
            capital_tag = "温和放量"
        else:
            capital_tag = "资金平淡"

        result[str(code)] = {
            "amount_ratio5": round(float(amount_ratio5), 3) if amount_ratio5 is not None else None,
            "amount_ratio20": round(float(amount_ratio20), 3) if amount_ratio20 is not None else None,
            "capital_proxy_score": capital_proxy_score,
            "capital_tag": capital_tag,
            "amount_yi": round(amount_yi, 3),
        }
    return result


def _classify_event_candidate(
    relation: str,
    candidate_score: float,
    change_pct: Any,
    amount: float,
    pre_score: float,
    post_score: float,
    chase_score: float,
    base_score: float,
    capital_score: float = 0.0,
    capital_tag: str = "",
) -> Dict[str, str]:
    pct = _safe_float(change_pct)
    amount_yi = amount / 1e4 if amount else 0.0

    if candidate_score >= 70 and post_score >= 28 and chase_score < 35:
        setup_tag = "主升浪候选"
        action_tag = "可重点跟踪"
    elif (pre_score >= 35 or capital_score >= 65) and pct < 7:
        setup_tag = "资金提前埋伏"
        action_tag = "等消息确认"
    elif post_score >= 24 and 3 <= pct <= 8:
        setup_tag = "跟风确认"
        action_tag = "只做强中选强"
    elif pct >= 9 or chase_score >= 45:
        setup_tag = "追高风险"
        action_tag = "谨慎追买"
    elif relation == "direct" and base_score >= 35:
        setup_tag = "消息直连"
        action_tag = "观察承接"
    else:
        setup_tag = "观察扩散"
        action_tag = "等待放量"

    risk_notes: List[str] = []
    if pct >= 9:
        risk_notes.append("涨幅过高")
    if chase_score >= 45:
        risk_notes.append("追高风险高")
    if amount_yi < 0.5:
        risk_notes.append("成交不足")
    if relation != "direct":
        risk_notes.append("非直接标的")
    risk_tag = " / ".join(risk_notes) if risk_notes else "风险可控"

    reason_parts = [
        "直接点名" if relation == "direct" else "板块扩散",
        f"候选分{candidate_score:.1f}",
    ]
    if pct:
        reason_parts.append(f"涨跌{pct:.2f}%")
    if amount_yi:
        reason_parts.append(f"成交{amount_yi:.2f}亿")
    if pre_score:
        reason_parts.append(f"埋伏{pre_score:.1f}")
    if post_score:
        reason_parts.append(f"确认{post_score:.1f}")
    if capital_score:
        reason_parts.append(f"资金{capital_score:.1f}")
    if capital_tag:
        reason_parts.append(capital_tag)

    return {
        "setup_tag": setup_tag,
        "action_tag": action_tag,
        "risk_tag": risk_tag,
        "decision_reason": "；".join(reason_parts),
    }


def query_cls_event_candidates(event_id: int, limit: int = 30, peer_limit: int = 20) -> Dict[str, Any]:
    event = query_cls_event(event_id)
    signal = event.get("signal") or {}
    if not signal:
        return {"event_id": int(event_id), "items": [], "summary": {"direct": 0, "sector_peer": 0}}

    direct_codes = _split_codes(signal.get("related_codes"))
    sector_names_by_code, peer_codes = _sector_candidates_for_codes(direct_codes, peer_limit=peer_limit)
    all_codes = sorted(set(direct_codes + peer_codes))
    snapshot = _stock_snapshot_map(all_codes)
    capital_context = _stock_capital_context_map(all_codes)

    rows: List[Dict[str, Any]] = []
    base_score = _safe_float(signal.get("opportunity_score"))
    pre_score = _safe_float(signal.get("pre_position_score"))
    post_score = _safe_float(signal.get("post_confirm_score"))
    for code in all_codes:
        info = snapshot.get(code, {})
        relation = "direct" if code in direct_codes else "sector_peer"
        change_pct = info.get("snapshot_change_pct")
        if change_pct is None:
            change_pct = info.get("change_pct")
        amount = _safe_float(info.get("snapshot_amount") or info.get("amount"))
        capital = capital_context.get(code, {})
        capital_score = _safe_float(capital.get("capital_proxy_score"))
        relation_bonus = 25.0 if relation == "direct" else 10.0
        change_bonus = max(0.0, min(18.0, _safe_float(change_pct) * 2.2))
        amount_bonus = min(12.0, math.log1p(max(0.0, amount) / 1e4) * 4.0)
        candidate_score = round(
            max(
                0.0,
                min(
                    100.0,
                    base_score * 0.34
                    + pre_score * 0.12
                    + post_score * 0.12
                    + capital_score * 0.18
                    + relation_bonus
                    + change_bonus
                    + amount_bonus,
                ),
            ),
            2,
        )
        classification = _classify_event_candidate(
            relation=relation,
            candidate_score=candidate_score,
            change_pct=change_pct,
            amount=amount,
            pre_score=pre_score,
            post_score=post_score,
            chase_score=_safe_float(signal.get("chase_risk_score")),
            base_score=base_score,
            capital_score=capital_score,
            capital_tag=str(capital.get("capital_tag") or ""),
        )
        rows.append(
            {
                "event_id": int(event_id),
                "code": code,
                "name": info.get("name") or code,
                "industry": info.get("industry") or "",
                "relation": relation,
                "sector_names": ",".join(sector_names_by_code.get(code, [])[:5]),
                "candidate_score": candidate_score,
                "change_pct": change_pct,
                "amount": amount,
                "turnover_rate": info.get("turnover_rate"),
                "amount_ratio5": capital.get("amount_ratio5"),
                "amount_ratio20": capital.get("amount_ratio20"),
                "capital_proxy_score": capital_score,
                "capital_tag": capital.get("capital_tag") or "",
                "snapshot_price": info.get("snapshot_price"),
                "snapshot_time": info.get("snapshot_time"),
                "trade_date": info.get("trade_date"),
                **classification,
            }
        )
    rows = sorted(rows, key=lambda item: (item["candidate_score"], _safe_float(item.get("change_pct")), _safe_float(item.get("amount"))), reverse=True)
    n = max(1, min(int(limit), 100))
    return {
        "event_id": int(event_id),
        "signal": signal,
        "items": rows[:n],
        "summary": {
            "direct": len(direct_codes),
            "sector_peer": len(peer_codes),
            "total": len(rows),
        },
    }


def query_cls_status() -> Dict[str, Any]:
    ensure_cls_news_tables()
    state = _read_state()
    payload: Dict[str, Any] = {
        "ok": True,
        "state": state,
        "state_path": str(STATE_PATH),
        "tables": {
            RAW_TABLE: clickhouse_table_exists(RAW_TABLE),
            SIGNAL_TABLE: clickhouse_table_exists(SIGNAL_TABLE),
        },
    }
    try:
        raw_count = clickhouse_query_df(f"SELECT count() AS cnt, max(event_time) AS latest_time FROM {RAW_TABLE} FINAL")
        signal_count = clickhouse_query_df(f"SELECT count() AS cnt, max(event_time) AS latest_time FROM {SIGNAL_TABLE} FINAL")
        payload["raw"] = _records_json_safe(raw_count)[0] if raw_count is not None and not raw_count.empty else {}
        payload["signals"] = _records_json_safe(signal_count)[0] if signal_count is not None and not signal_count.empty else {}
        payload["context"] = _context_artifact_status()
    except Exception as exc:
        payload["ok"] = False
        payload["error"] = str(exc)
    return payload


def _context_artifact_status() -> List[Dict[str, Any]]:
    items = [
        ("g2_live", G2_LIVE_SOURCE),
        ("mainline_theme_pool", MAINLINE_THEME_POOL),
        ("mainline_theme_addon", MAINLINE_THEME_ADDON),
        ("mainline_sector_overlay", MAINLINE_SECTOR_OVERLAY),
    ]
    result: List[Dict[str, Any]] = []
    for name, path in items:
        exists = path.exists()
        row_count = 0
        columns: List[str] = []
        if exists:
            df = _read_table_file(path)
            row_count = int(len(df)) if df is not None else 0
            columns = list(df.columns[:20]) if df is not None and not df.empty else list(df.columns[:20]) if df is not None else []
        result.append(
            {
                "name": name,
                "path": str(path),
                "exists": exists,
                "row_count": row_count,
                "columns": columns,
                "usable": bool(exists and row_count > 0),
            }
        )
    return result
