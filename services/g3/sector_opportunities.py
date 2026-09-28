"""Domain logic extracted without changing existing API behavior."""
from typing import Any
import pandas as pd


def _to_float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
        if pd.isna(number):
            return None
        return number
    except Exception:
        return None


def _mainwave_sector_state(avg_diffusion: float, max_score: float, candidate_count: int, recommended_count: int) -> tuple[str, str]:
    if avg_diffusion >= 85 and max_score >= 120 and (candidate_count >= 2 or recommended_count > 0):
        return "strong_mainwave", "强主升机会"
    if avg_diffusion >= 75 and max_score >= 118:
        return "important_industry", "重点行业机会"
    return "watch", "观察机会"


def _build_mainwave_sector_opportunities(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in candidates:
        groups.setdefault(str(item.get("sector_name") or "未识别行业"), []).append(item)
    out: list[dict[str, Any]] = []
    for sector, rows in groups.items():
        scores = [_to_float_or_none(item.get("wave_style_score")) for item in rows]
        scores = [value for value in scores if value is not None]
        diffusions = [_to_float_or_none(item.get("sector_diffusion_score")) for item in rows]
        diffusions = [value for value in diffusions if value is not None]
        max_score = max(scores) if scores else 0.0
        avg_score = sum(scores) / len(scores) if scores else 0.0
        avg_diffusion = sum(diffusions) / len(diffusions) if diffusions else 0.0
        recommended = [item for item in rows if item.get("is_recommended")]
        m30_ok = [item for item in rows if item.get("m30_confirmed") or str(item.get("m30_status") or "") == "ok"]
        state, state_label = _mainwave_sector_state(avg_diffusion, max_score, len(rows), len(recommended))
        top_rows = sorted(rows, key=lambda item: _to_float_or_none(item.get("wave_style_score")) or 0, reverse=True)[:5]
        templates = sorted({str(item.get("template_label") or "") for item in rows if item.get("template_label")})
        sector_code = next(
            (
                str(item.get("sector_code") or item.get("l2_sector_code") or "").strip()
                for item in rows
                if str(item.get("sector_code") or item.get("l2_sector_code") or "").strip()
            ),
            "",
        )
        out.append(
            {
                "sector_name": sector,
                "sector_code": sector_code,
                "state": state,
                "state_label": state_label,
                "candidate_count": len(rows),
                "recommended_count": len(recommended),
                "m30_ok_count": len(m30_ok),
                "max_wave_style_score": max_score,
                "avg_wave_style_score": avg_score,
                "avg_sector_diffusion_score": avg_diffusion,
                "top_candidates": top_rows,
                "top_candidate_names": " / ".join([str(item.get("name") or item.get("code") or "") for item in top_rows[:3]]),
                "template_labels": templates,
                "rank_score": avg_diffusion * 0.55 + max_score * 0.35 + min(len(rows), 8) * 2.0 + len(recommended) * 5.0,
            }
        )
    out.sort(key=lambda item: _to_float_or_none(item.get("rank_score")) or 0, reverse=True)
    return out


def _build_mainwave_sector_opportunities_v2(candidates: list[dict[str, Any]], watch_candidates: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in candidates:
        groups.setdefault(str(item.get("sector_name") or ""), []).append(item)
    watch_groups: dict[str, list[dict[str, Any]]] = {}
    for item in watch_candidates or []:
        watch_groups.setdefault(str(item.get("sector_name") or ""), []).append(item)
    for sector in watch_groups:
        groups.setdefault(sector, [])

    out: list[dict[str, Any]] = []
    for sector, rows in groups.items():
        watch_rows = watch_groups.get(sector, [])
        scoring_rows = rows or watch_rows
        scores = [_to_float_or_none(item.get("wave_style_score")) for item in scoring_rows]
        scores = [value for value in scores if value is not None]
        diffusions = [_to_float_or_none(item.get("sector_diffusion_score")) for item in scoring_rows]
        diffusions = [value for value in diffusions if value is not None]
        max_score = max(scores) if scores else 0.0
        avg_score = sum(scores) / len(scores) if scores else 0.0
        avg_diffusion = sum(diffusions) / len(diffusions) if diffusions else 0.0
        recommended = [item for item in rows if item.get("is_recommended")]
        m30_ok = [item for item in rows if item.get("m30_confirmed") or str(item.get("m30_status") or "") == "ok"]
        state, state_label = _mainwave_sector_state(avg_diffusion, max_score, len(rows), len(recommended))
        if not rows and watch_rows:
            state, state_label = "sector_watch", "板块观察"
        top_rows = sorted(scoring_rows, key=lambda item: _to_float_or_none(item.get("wave_style_score")) or 0, reverse=True)[:5]
        templates = sorted({str(item.get("template_label") or "") for item in scoring_rows if item.get("template_label")})
        sector_code = next(
            (
                str(item.get("sector_code") or item.get("l2_sector_code") or "").strip()
                for item in scoring_rows
                if str(item.get("sector_code") or item.get("l2_sector_code") or "").strip()
            ),
            "",
        )
        out.append(
            {
                "sector_name": sector,
                "sector_code": sector_code,
                "state": state,
                "state_label": state_label,
                "candidate_count": len(rows),
                "watch_candidate_count": len(watch_rows),
                "recommended_count": len(recommended),
                "m30_ok_count": len(m30_ok),
                "max_wave_style_score": max_score,
                "avg_wave_style_score": avg_score,
                "avg_sector_diffusion_score": avg_diffusion,
                "top_candidates": top_rows,
                "top_candidate_names": " / ".join([str(item.get("name") or item.get("code") or "") for item in top_rows[:3]]),
                "template_labels": templates,
                "rank_score": avg_diffusion * 0.55 + max_score * 0.35 + min(len(rows), 8) * 2.0 + min(len(watch_rows), 8) * 1.2 + len(recommended) * 5.0,
            }
        )
    out.sort(key=lambda item: _to_float_or_none(item.get("rank_score")) or 0, reverse=True)
    return out

