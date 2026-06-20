from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

import requests


CLS_ROLL_URL = "https://www.cls.cn/v1/roll/get_roll_list"
CLS_REFERER = "https://www.cls.cn/telegraph"
CLS_APP = "CailianpressWeb"
CLS_WEB_VERSION = "8.7.9"


def _sort_key(value: Any) -> str:
    return str(value).upper()


def _flatten_param(key: str, value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return f"{key}={value}"
    if isinstance(value, list):
        if not value:
            return f"{key}[]"
        return "&".join(_flatten_param(f"{key}[{idx}]", item) for idx, item in enumerate(value))
    if isinstance(value, dict):
        return "&".join(
            _flatten_param(f"{key}[{sub_key}]", value[sub_key])
            for sub_key in sorted(value.keys(), key=_sort_key)
        )
    return f"{key}={value}"


def build_cls_sign(params: Dict[str, Any]) -> str:
    query = "&".join(
        part
        for part in (
            _flatten_param(key, params[key])
            for key in sorted(params.keys(), key=_sort_key)
        )
        if part
    )
    sha1_hex = hashlib.sha1(query.encode("utf-8")).hexdigest()
    return hashlib.md5(sha1_hex.encode("utf-8")).hexdigest()


@dataclass
class ClsTelegraphClient:
    timeout: int = 10
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    )

    def fetch_roll_list(
        self,
        *,
        last_time: Optional[int] = None,
        rn: int = 50,
        refresh_type: int = 1,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {
            "app": CLS_APP,
            "last_time": int(last_time or time.time()),
            "os": "web",
            "refresh_type": int(refresh_type),
            "rn": max(1, min(int(rn), 100)),
            "sv": CLS_WEB_VERSION,
        }
        params["sign"] = build_cls_sign(params)
        headers = {
            "Accept": "application/json,text/plain,*/*",
            "Referer": CLS_REFERER,
            "User-Agent": self.user_agent,
        }
        response = requests.get(CLS_ROLL_URL, params=params, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        errno = payload.get("errno")
        if str(errno) not in {"0", ""} and errno != 0:
            raise RuntimeError(f"CLS telegraph request failed: errno={errno}, msg={payload.get('msg')}")
        data = payload.get("data") or {}
        rows = data.get("roll_data") or []
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]

    def fetch_pages(self, *, pages: int = 1, rn: int = 50) -> List[Dict[str, Any]]:
        all_rows: List[Dict[str, Any]] = []
        last_time: Optional[int] = None
        seen: set[int] = set()
        for _ in range(max(1, int(pages))):
            rows = self.fetch_roll_list(last_time=last_time, rn=rn)
            if not rows:
                break
            for row in rows:
                row_id = _safe_int(row.get("id"))
                if row_id and row_id in seen:
                    continue
                if row_id:
                    seen.add(row_id)
                all_rows.append(row)
            times = [_safe_int(row.get("ctime")) for row in rows]
            times = [item for item in times if item > 0]
            if not times:
                break
            next_time = min(times)
            if last_time is not None and next_time >= last_time:
                break
            last_time = next_time
        return all_rows


def _safe_int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(value)
    except Exception:
        return 0


def iter_cls_telegraph_rows(pages: int = 1, rn: int = 50) -> Iterable[Dict[str, Any]]:
    client = ClsTelegraphClient()
    yield from client.fetch_pages(pages=pages, rn=rn)
