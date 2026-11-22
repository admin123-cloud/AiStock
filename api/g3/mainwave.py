"""G3 AI 排雷与催化评分快照 API。"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from services.g3.ai_screening import (
    load_saved_screening,
    save_agent_screening,
)

router = APIRouter()


class AiScreeningItemIn(BaseModel):
    code: str
    name: str = ""
    industry: str = ""
    risk_light: str = Field(default="yellow", description="green | yellow | red")
    risk_flags: list[str] = Field(default_factory=list)
    catalyst_score: int = Field(default=3, ge=1, le=5)
    catalyst_type: str = Field(default="逻辑催化")
    one_line_thesis: str = ""
    source: str = "agent_enriched"


class AiScreeningIngestRequest(BaseModel):
    decision_date: str
    summary_note: Optional[str] = None
    agent_name: str = "antigravity_agent"
    items: list[AiScreeningItemIn] = Field(default_factory=list)


@router.get("/ai-screening")
def get_g3_ai_screening(decision_date: Optional[str] = Query(default=None)) -> dict[str, Any]:
    """读取已落盘的 AI 候选股排雷与催化评分快照。"""
    doc = load_saved_screening(decision_date=decision_date)
    return doc or {"schema": "g3_ai_screening/1", "decision_date": decision_date, "items_by_code": {}}


@router.post("/ai-screening/ingest")
def post_g3_ai_screening_ingest(req: AiScreeningIngestRequest) -> dict[str, Any]:
    """Scoped-write：供现有 Agent / 定时工作流提交结构化排雷与催化分析结果。"""
    if not req.items:
        raise HTTPException(status_code=400, detail="items 不能为空")
    doc = save_agent_screening(
        decision_date=req.decision_date,
        items=[it.model_dump() for it in req.items],
        summary_note=req.summary_note,
        agent_name=req.agent_name,
    )
    return {
        "status": "ok",
        "decision_date": doc["decision_date"],
        "count": len(doc["items_by_code"]),
        "generated_at": doc["generated_at"],
    }
