# -*- coding: utf-8 -*-
"""AiStock 证券主数据（stock.stocks）安全增量同步脚本。

职责：
1. 从东方财富/沪深北全市场名录（ak.stock_info_a_code_name）拉取最新在市 A 股清单；
2. 严格遵守安全门禁：
   - 仅处理合法沪深北 A 股（00/30 -> .SZ、60/68 -> .SH、92 -> .BJ），天然满足 NEEQ 约束；
   - 官方有效在市股数 < 5000 时直接 fail-closed 拒绝写入，绝不误标退市；
   - 绝不修改或删除任何非 stock（index / bond / market_statistic）记录；
   - 对存量股票 100% 继承原有行业、地区、股本、自选/持仓标记与创建时间；
3. 增量闭环四类变更：
   - 新增新上市股票（自动从 kline_daily 补齐首日 list_date）；
   - 将已在市且有行情的 pending_listing 升格为 active；
   - 同步真实名称变更与 ST 戴帽/摘帽状态（自动忽略纯空白字符抖动）；
   - 退市/吸收合并双证据判定：从官方在市名录消失 且 最近 5 个交易日在 kline_daily 无成交时，
     标记 quit=1 与 delist_date=末次交易日（绝不误杀临时停牌股）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

os.environ["NO_PROXY"] = "*"

BJ_TZ = timezone(timedelta(hours=8))
MIN_VALID_LIVE_STOCKS = 5000
ST_NAME_RE = re.compile(r"(^\*?ST|^S\*?ST|退市|退$)", re.IGNORECASE)


def normalize_a_share_code(raw_code: Any) -> Optional[str]:
    """将 6 位原始代码规范化为合法沪深北 A 股代码；拒绝 NEEQ/旧三板/非法代码。"""
    s = str(raw_code or "").strip().upper()
    if "." in s:
        s = s.split(".", 1)[0]
    if len(s) != 6 or not s.isdigit():
        return None
    if s.startswith(("60", "68")):
        return f"{s}.SH"
    if s.startswith(("00", "30")):
        return f"{s}.SZ"
    if s.startswith("92"):
        return f"{s}.BJ"
    return None


def is_st_name(name: str) -> int:
    clean = re.sub(r"\s+", "", str(name or "").strip())
    return 1 if ST_NAME_RE.search(clean) else 0


def _compact_name(name: str) -> str:
    return re.sub(r"\s+", "", str(name or "").strip())


def _market_of(full_code: str) -> str:
    return full_code.split(".", 1)[1] if "." in full_code else ""


def _ch_http(sql: str, body: bytes | None = None, timeout: int = 30) -> str:
    host = (os.environ.get("AISTOCK_CLICKHOUSE_HOST") or "127.0.0.1").strip()
    port = (os.environ.get("AISTOCK_CLICKHOUSE_PORT") or "8123").strip()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if body is None:
        req = urllib.request.Request(f"http://{host}:{port}/", data=sql.encode("utf-8"), method="POST")
    else:
        q = urllib.parse.urlencode({"query": sql})
        req = urllib.request.Request(f"http://{host}:{port}/?{q}", data=body, method="POST")
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8").strip()


def _ch_json_rows(sql: str) -> list[dict[str, Any]]:
    raw = _ch_http(f"{sql.rstrip(';')} FORMAT JSONEachRow")
    if not raw:
        return []
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def fetch_live_a_share_catalog() -> dict[str, str]:
    """从 ak.stock_info_a_code_name 获取全市场沪深北在市 A 股名录 {code: name}。"""
    import akshare as ak

    df = ak.stock_info_a_code_name()
    if df is None or df.empty:
        return {}
    catalog: dict[str, str] = {}
    for _, row in df.iterrows():
        fc = normalize_a_share_code(row.get("code"))
        if not fc:
            continue
        name = str(row.get("name") or "").strip()
        if name:
            catalog[fc] = name
    return catalog


def plan_stock_reference_updates(
    *,
    live_catalog: Mapping[str, str],
    existing_stocks: Mapping[str, Mapping[str, Any]],
    kline_date_bounds: Mapping[str, tuple[Optional[str], Optional[str]]],
    delist_cutoff_date: Optional[str],
    now_str: str,
    min_live_stocks: int = MIN_VALID_LIVE_STOCKS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """纯函数：计算 stock.stocks 增量变更行与审计摘要（零副作用，便于单测）。"""
    if len(live_catalog) < min_live_stocks:
        raise RuntimeError(
            f"官方在市名录样本不足（got={len(live_catalog)} < {min_live_stocks}），"
            "触发 fail-closed 拒绝写入 stock.stocks"
        )

    upserts: list[dict[str, Any]] = []
    added_codes: list[str] = []
    promoted_codes: list[str] = []
    renamed_or_st_codes: list[str] = []
    delisted_codes: list[str] = []
    skipped_suspended_codes: list[str] = []

    def _base_record(old: Mapping[str, Any], **overrides: Any) -> dict[str, Any]:
        rec = {
            "code": str(old["code"]),
            "name": str(old["name"]),
            "market": str(old.get("market") or _market_of(str(old["code"]))),
            "type": "stock",
            "listing_status": str(old.get("listing_status") or "active"),
            "industry": old.get("industry"),
            "region": old.get("region"),
            "list_date": old.get("list_date"),
            "delist_date": old.get("delist_date"),
            "quit": int(old.get("quit") or 0),
            "st": int(old.get("st") or 0),
            "industry_code": old.get("industry_code"),
            "self_selected": int(old.get("self_selected") or 0),
            "holding": int(old.get("holding") or 0),
            "float_share": old.get("float_share"),
            "total_share": old.get("total_share"),
            "created_at": str(old.get("created_at") or now_str),
            "updated_at": now_str,
            "id": int(old.get("id") or 0),
        }
        rec.update(overrides)
        return rec

    # 1. 新增新股 / pending_listing 转正 / 名称与 ST 变更
    for code in sorted(live_catalog.keys()):
        live_name = str(live_catalog[code]).strip()
        live_st = is_st_name(live_name)
        first_td, _last_td = kline_date_bounds.get(code, (None, None))
        old = existing_stocks.get(code)

        if old is None:
            upserts.append(
                {
                    "code": code,
                    "name": live_name,
                    "market": _market_of(code),
                    "type": "stock",
                    "listing_status": "active",
                    "industry": "",
                    "region": None,
                    "list_date": first_td,
                    "delist_date": None,
                    "quit": 0,
                    "st": live_st,
                    "industry_code": None,
                    "self_selected": 0,
                    "holding": 0,
                    "float_share": None,
                    "total_share": None,
                    "created_at": now_str,
                    "updated_at": now_str,
                    "id": 0,
                }
            )
            added_codes.append(f"{code} {live_name}")
            continue

        old_status = str(old.get("listing_status") or "active")
        old_quit = int(old.get("quit") or 0)
        old_name = str(old.get("name") or "")
        old_st = int(old.get("st") or 0)
        old_list_date = old.get("list_date")

        if old_status == "pending_listing":
            upserts.append(
                _base_record(
                    old,
                    name=live_name,
                    listing_status="active",
                    quit=0,
                    st=live_st,
                    list_date=old_list_date or first_td,
                )
            )
            promoted_codes.append(f"{code} {live_name}")
            continue

        if old_quit == 1:
            # 仍在官方名录但库中误标 quit=1 → 恢复在市
            upserts.append(
                _base_record(
                    old,
                    name=live_name,
                    listing_status="active",
                    quit=0,
                    delist_date=None,
                    st=live_st,
                    list_date=old_list_date or first_td,
                )
            )
            promoted_codes.append(f"{code} {live_name} (恢复在市)")
            continue

        name_diff = _compact_name(old_name) != _compact_name(live_name)
        st_diff = old_st != live_st
        need_list_date = (old_list_date is None or str(old_list_date) == "") and first_td is not None
        if name_diff or st_diff or need_list_date:
            upserts.append(
                _base_record(
                    old,
                    name=live_name if name_diff else old_name,
                    st=live_st,
                    list_date=old_list_date or first_td,
                )
            )
            if name_diff or st_diff:
                renamed_or_st_codes.append(f"{code} {old_name}->{live_name}")

    # 2. 双证据退市/摘牌检测（不在官方在市名录 + 最近 5 个交易日无日线成交）
    for code in sorted(existing_stocks.keys()):
        if code in live_catalog:
            continue
        old = existing_stocks[code]
        if str(old.get("listing_status") or "active") != "active" or int(old.get("quit") or 0) != 0:
            continue
        _first_td, last_td = kline_date_bounds.get(code, (None, None))
        if delist_cutoff_date and last_td and last_td >= delist_cutoff_date:
            # 近 5 个交易日仍有成交，保守按停牌/源抖动跳过，绝不误标退市
            skipped_suspended_codes.append(code)
            continue
        upserts.append(
            _base_record(
                old,
                quit=1,
                delist_date=old.get("delist_date") or last_td,
            )
        )
        delisted_codes.append(f"{code} {old.get('name')} (last={last_td})")

    summary = {
        "live_catalog_count": len(live_catalog),
        "existing_stock_count": len(existing_stocks),
        "total_upserts": len(upserts),
        "added_count": len(added_codes),
        "promoted_count": len(promoted_codes),
        "renamed_or_st_count": len(renamed_or_st_codes),
        "delisted_count": len(delisted_codes),
        "skipped_suspended_count": len(skipped_suspended_codes),
        "added": added_codes[:30],
        "promoted": promoted_codes[:20],
        "renamed_or_st": renamed_or_st_codes[:30],
        "delisted": delisted_codes[:20],
    }
    return upserts, summary


def sync_stocks_reference(
    *,
    dry_run: bool = False,
    catalog_fn: Callable[[], dict[str, str]] = fetch_live_a_share_catalog,
) -> dict[str, Any]:
    """执行 stock.stocks 增量同步并刷新数据中心缓存。"""
    now_bj = datetime.now(BJ_TZ)
    now_str = now_bj.strftime("%Y-%m-%d %H:%M:%S")

    live_catalog = catalog_fn()
    existing_rows = _ch_json_rows(
        """
        SELECT
            code, name, market, type, listing_status,
            industry, region,
            if(isNull(list_date), NULL, toString(list_date)) AS list_date,
            if(isNull(delist_date), NULL, toString(delist_date)) AS delist_date,
            quit, st, industry_code, self_selected, holding,
            float_share, total_share,
            toString(created_at) AS created_at,
            id
        FROM stock.stocks FINAL
        WHERE type = 'stock'
        """
    )
    existing_stocks = {str(r["code"]): r for r in existing_rows}

    # 仅查询需要补首日或核验末日的股票集合，避免扫无关索引
    candidate_codes = sorted(
        (set(live_catalog.keys()) - set(existing_stocks.keys()))
        | {k for k, v in existing_stocks.items() if v.get("listing_status") == "pending_listing" or v.get("list_date") in (None, "")}
        | {k for k, v in existing_stocks.items() if k not in live_catalog and v.get("listing_status") == "active" and int(v.get("quit") or 0) == 0}
    )

    kline_date_bounds: dict[str, tuple[Optional[str], Optional[str]]] = {}
    if candidate_codes:
        in_sql = ", ".join(f"'{c}'" for c in candidate_codes)
        bound_rows = _ch_json_rows(
            f"""
            SELECT code, toString(min(trade_date)) AS min_d, toString(max(trade_date)) AS max_d
            FROM stock.kline_daily
            WHERE code IN ({in_sql})
            GROUP BY code
            """
        )
        for r in bound_rows:
            kline_date_bounds[str(r["code"])] = (r.get("min_d"), r.get("max_d"))

    # 取最近第 5 个交易日作为退市双证据截止线
    cal_rows = _ch_json_rows(
        """
        SELECT toString(trade_date) AS d
        FROM stock.trade_calendar
        WHERE market = 'SH' AND is_trading = 1 AND trade_date <= today()
        ORDER BY trade_date DESC
        LIMIT 5
        """
    )
    delist_cutoff = str(cal_rows[-1]["d"]) if cal_rows else None

    upserts, summary = plan_stock_reference_updates(
        live_catalog=live_catalog,
        existing_stocks=existing_stocks,
        kline_date_bounds=kline_date_bounds,
        delist_cutoff_date=delist_cutoff,
        now_str=now_str,
    )

    if dry_run:
        return {"status": "dry_run", "synced_at": now_str, **summary}

    if upserts:
        payload = ("\n".join(json.dumps(row, ensure_ascii=False) for row in upserts) + "\n").encode("utf-8")
        _ch_http("INSERT INTO stock.stocks FORMAT JSONEachRow", body=payload, timeout=60)
        _ch_http("OPTIMIZE TABLE stock.stocks FINAL", timeout=60)

    try:
        from services.ops.data_center_service import invalidate_data_center_cache
        invalidate_data_center_cache()
    except Exception:
        pass

    return {"status": "ok", "synced_at": now_str, **summary}


def main() -> int:
    parser = argparse.ArgumentParser(description="AiStock 证券主数据 stock.stocks 增量同步")
    parser.add_argument("--dry-run", action="store_true", help="仅比对变更不写入 ClickHouse")
    args = parser.parse_args()
    res = sync_stocks_reference(dry_run=args.dry_run)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
