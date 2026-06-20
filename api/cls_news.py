from __future__ import annotations

from fastapi import APIRouter, Query

from services.cls_news_service import (
    fetch_store_and_score_cls_news,
    query_cls_event,
    query_cls_event_candidates,
    query_cls_latest,
    query_cls_radar_summary,
    query_cls_signals,
    query_cls_status,
)


router = APIRouter(prefix="/news/cls", tags=["cls-news"])


@router.post("/sync")
def sync_cls_news(
    pages: int = Query(1, ge=1, le=5),
    rn: int = Query(50, ge=1, le=100),
):
    return fetch_store_and_score_cls_news(pages=pages, rn=rn)


@router.get("/latest")
def get_cls_latest(limit: int = Query(50, ge=1, le=200)):
    return {"items": query_cls_latest(limit=limit)}


@router.get("/signals")
def get_cls_signals(
    limit: int = Query(50, ge=1, le=200),
    min_score: float = Query(0.0, ge=0.0, le=100.0),
    signal_class: str = "",
):
    return {"items": query_cls_signals(limit=limit, min_score=min_score, signal_class=signal_class)}


@router.get("/status")
def get_cls_status():
    return query_cls_status()


@router.get("/radar-summary")
def get_cls_radar_summary(
    limit: int = Query(50, ge=1, le=200),
    top_events: int = Query(10, ge=1, le=20),
    peer_limit: int = Query(12, ge=0, le=50),
):
    return query_cls_radar_summary(limit=limit, top_events=top_events, peer_limit=peer_limit)


@router.get("/event/{event_id}")
def get_cls_event(event_id: int):
    return query_cls_event(event_id)


@router.get("/event/{event_id}/candidates")
def get_cls_event_candidates(
    event_id: int,
    limit: int = Query(30, ge=1, le=100),
    peer_limit: int = Query(20, ge=0, le=80),
):
    return query_cls_event_candidates(event_id, limit=limit, peer_limit=peer_limit)
