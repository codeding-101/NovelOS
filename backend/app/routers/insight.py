"""V0.2 洞察类接口：时点视图、总览看板、全量扫描、混合检索、向量索引。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_session
from app.models import Character, Chapter, Novel, SweepRun
from app.schemas import (
    AsOfStateOut,
    DashboardOut,
    EmbeddingStats,
    ForeshadowingPlanOut,
    RetrievalResult,
    SweepRequest,
    SweepRunOut,
)
from app.services import foreshadow_service, query_service, retrieval_service, sweep_service, vector_service

router = APIRouter(prefix="/api/novels", tags=["洞察"])


def _novel(session: Session, novel_id: str) -> Novel:
    novel = session.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(status_code=404, detail="小说不存在")
    return novel


@router.get("/{novel_id}/state", response_model=AsOfStateOut)
def state_as_of(
    novel_id: str,
    chapter: int = Query(ge=1, description="查看第 N 章时的世界状态"),
    session: Session = Depends(get_session),
) -> AsOfStateOut:
    """时点视图：第 N 章时成立的 Canon、人物状态、时间线、事件、伏笔与世界观规则。"""
    novel = _novel(session, novel_id)
    facts_at_chapter = query_service.canon_as_of(session, novel.id, chapter)
    #: 该时点生效的「状态」类 Canon 事实：用于在人物还没写状态历史时，给出当时的状态
    status_facts = {
        fact["subject"]: fact
        for fact in facts_at_chapter
        if fact["predicate"] == "状态" and fact.get("valid_from_chapter")
    }
    #: 任何时点的状态记录：用来判断「该角色其实有状态记录，只是不在这个时点」
    all_status_facts = {
        fact["subject"]: fact
        for fact in query_service.list_canon_facts(
            session, novel.id, status=None, limit=1000
        )
        if fact["predicate"] == "状态" and fact["status"] != "PROPOSED"
    }
    characters = []
    for character in session.scalars(
        select(Character).where(Character.novel_id == novel.id).order_by(Character.name)
    ):
        view = query_service.get_character_state(session, novel.id, character.name, chapter)
        recorded = view.get("state_at_chapter")
        fact = status_facts.get(character.name)
        too_early = (
            character.first_appearance is not None and chapter < character.first_appearance
        )
        if recorded:
            status = recorded["status"] or "未记录"
            location = recorded["location"] or character.current_location
            source = "状态历史"
        elif fact is not None:
            status = fact["object"]
            location = character.current_location
            source = f"Canon 事实（第{fact['source_chapter']}章起）"
        elif too_early:
            status = "尚未登场"
            location = "—"
            source = "第 %d 章时该人物还未出场" % chapter
        elif character.name in all_status_facts:
            earliest = all_status_facts[character.name]
            status = "未记录"
            location = "未记录"
            source = (
                f"该时点没有状态记录（最早的状态记录在第{earliest['source_chapter']}章："
                f"{earliest['object']}）"
            )
        else:
            status = character.current_status
            location = character.current_location
            source = "取当前值（该角色没有任何状态记录）"
        characters.append(
            {
                "name": character.name,
                "status_at_chapter": status,
                "location_at_chapter": location,
                "current_status": character.current_status,
                "first_appearance": character.first_appearance,
                "changed_since": recorded is not None,
                "status_source": source,
            }
        )
    notes: list[str] = []
    if not any(entry["changed_since"] for entry in characters):
        notes.append("第 %d 章之前没有记录过人物状态变更，状态按该时点的 Canon 事实或登场信息推导" % chapter)
    return AsOfStateOut(
        novel_id=novel.id,
        chapter_number=chapter,
        canon_facts=facts_at_chapter,
        characters=characters,
        timeline=query_service.list_timeline(
            session, novel.id, status=None, limit=500, as_of_chapter=chapter
        ),
        events=query_service.list_events(session, novel.id, limit=500, as_of_chapter=chapter),
        foreshadowings=[
            item
            for item in query_service.list_foreshadowing(session, novel.id)
            if (item["first_chapter"] or 0) <= chapter
        ],
        world_rules=query_service.list_world_rules(session, novel.id),
        notes=notes,
    )


@router.get("/{novel_id}/dashboard", response_model=DashboardOut)
def dashboard(novel_id: str, session: Session = Depends(get_session)) -> DashboardOut:
    novel = _novel(session, novel_id)
    from app.services import novel_service

    novel_service.recount(session, novel)
    return DashboardOut(**sweep_service.dashboard(session, novel))


@router.post("/{novel_id}/sweep", response_model=SweepRunOut)
def run_sweep(
    novel_id: str, payload: SweepRequest, session: Session = Depends(get_session)
) -> SweepRun:
    """全量一致性扫描。mode=rules 秒级完成；mode=full 会逐章调用模型抽取（较慢）。"""
    novel = _novel(session, novel_id)
    if session.scalar(select(func.count(Chapter.id)).where(Chapter.novel_id == novel.id)) == 0:
        raise HTTPException(status_code=409, detail="该小说还没有章节，无需扫描")
    return sweep_service.run_sweep(session, novel, payload)


@router.get("/{novel_id}/sweeps", response_model=list[SweepRunOut])
def list_sweeps(
    novel_id: str,
    limit: int = Query(default=10, ge=1, le=50),
    session: Session = Depends(get_session),
) -> list[SweepRun]:
    _novel(session, novel_id)
    return list(
        session.scalars(
            select(SweepRun)
            .where(SweepRun.novel_id == novel_id)
            .order_by(SweepRun.started_at.desc())
            .limit(limit)
        )
    )


@router.get("/{novel_id}/retrieval", response_model=RetrievalResult)
def hybrid_retrieval(
    novel_id: str,
    q: str = Query(min_length=1),
    limit: int = Query(default=10, ge=1, le=50),
    use_vector: bool = Query(default=True, description="是否启用向量通道"),
    ref_type: str | None = Query(default=None, description="只看某一类：CHAPTER/CANON_FACT/..."),
    session: Session = Depends(get_session),
) -> RetrievalResult:
    novel = _novel(session, novel_id)
    ref_types = (ref_type,) if ref_type else None
    result = retrieval_service.hybrid_search(
        session, novel, q, limit=limit, use_vector=use_vector, ref_types=ref_types
    )
    return RetrievalResult(
        query=result["query"],
        engine=result["engine"],
        channels=result["channels"],
        hits=result["hits"],
    )


@router.get("/{novel_id}/vectors", response_model=EmbeddingStats)
def vector_stats(novel_id: str, session: Session = Depends(get_session)) -> EmbeddingStats:
    _novel(session, novel_id)
    return EmbeddingStats(**vector_service.index_stats(session, novel_id))


@router.post("/{novel_id}/vectors/reindex")
def reindex_vectors(
    novel_id: str,
    provider: str | None = Query(default=None, description="向量提供者：local / openai"),
    session: Session = Depends(get_session),
) -> dict:
    novel = _novel(session, novel_id)
    result = vector_service.index_novel(session, novel.id, provider)
    return result


@router.get("/{novel_id}/foreshadowing-plan", response_model=ForeshadowingPlanOut)
def foreshadowing_plan(
    novel_id: str,
    horizon: int = Query(default=5, ge=1, le=30),
    gap: int = Query(default=foreshadow_service.DEBT_GAP, ge=1, le=100),
    session: Session = Depends(get_session),
) -> ForeshadowingPlanOut:
    """伏笔欠账与回收建议：哪些伏笔拖太久、建议在第几章回应。"""
    novel = _novel(session, novel_id)
    suggestions = foreshadow_service.suggest(session, novel, horizon=horizon, gap_threshold=gap)
    return ForeshadowingPlanOut(
        **suggestions, debt=foreshadow_service.debt(session, novel, gap_threshold=gap)
    )
