"""V0.4 想法碎片接口：录入、整理、安排、碎片成文、情感引导、声音画像。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_session
from app.models import Chapter, Fragment, Novel
from app.schemas import (
    EmotionPromptRequest,
    EmotionPromptResponse,
    FragmentCreate,
    FragmentOut,
    FragmentPlaceRequest,
    FragmentRealizeRequest,
    FragmentRealizeResponse,
    FragmentStats,
    FragmentUpdate,
    StyleProfileRequest,
    VoiceProfileOut,
)
from app.services import (
    fragment_service,
    realize_service,
    style_service,
)

novel_router = APIRouter(prefix="/api/novels", tags=["想法碎片"])
item_router = APIRouter(prefix="/api/fragments", tags=["想法碎片"])


def _novel(session: Session, novel_id: str) -> Novel:
    novel = session.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(status_code=404, detail="小说不存在")
    return novel


def _fragment(session: Session, fragment_id: str) -> Fragment:
    fragment = session.get(Fragment, fragment_id)
    if fragment is None:
        raise HTTPException(status_code=404, detail="碎片不存在")
    return fragment


@novel_router.get("/{novel_id}/fragments", response_model=list[FragmentOut])
def list_fragments(
    novel_id: str,
    status: str | None = Query(default=None, description="INBOX / PLACED / REALIZED / ARCHIVED"),
    kind: str | None = Query(default=None),
    character: str | None = Query(default=None),
    target_chapter: int | None = Query(default=None),
    unplaced_only: bool = Query(default=False, description="只看还没安排的"),
    limit: int = Query(default=200, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[dict]:
    _novel(session, novel_id)
    rows = fragment_service.list_fragments(
        session,
        novel_id,
        status=status,
        kind=kind,
        character=character,
        target_chapter=target_chapter,
        unplaced_only=unplaced_only,
        limit=limit,
    )
    return [fragment_service.fragment_to_dict(row) for row in rows]


@novel_router.post("/{novel_id}/fragments", response_model=FragmentOut, status_code=201)
def create_fragment(
    novel_id: str, payload: FragmentCreate, session: Session = Depends(get_session)
) -> dict:
    novel = _novel(session, novel_id)
    fragment = fragment_service.create_fragment(session, novel, payload)
    return fragment_service.fragment_to_dict(fragment)


@novel_router.post("/{novel_id}/fragments/bulk", response_model=list[FragmentOut], status_code=201)
def create_fragments_bulk(
    novel_id: str,
    payload: list[FragmentCreate],
    session: Session = Depends(get_session),
) -> list[dict]:
    """一次贴一堆碎片进来（作者经常是攒着一起记的）。"""
    novel = _novel(session, novel_id)
    created = [fragment_service.create_fragment(session, novel, item) for item in payload]
    return [fragment_service.fragment_to_dict(fragment) for fragment in created]


@novel_router.get("/{novel_id}/fragments/stats", response_model=FragmentStats)
def fragment_stats(novel_id: str, session: Session = Depends(get_session)) -> dict:
    _novel(session, novel_id)
    return fragment_service.stats(session, novel_id)


@novel_router.get("/{novel_id}/fragments/relevant")
def relevant_fragments(
    novel_id: str,
    q: str = Query(min_length=1),
    limit: int = Query(default=5, ge=1, le=20),
    session: Session = Depends(get_session),
) -> list[dict]:
    novel = _novel(session, novel_id)
    return fragment_service.relevant_fragments(session, novel, q, limit=limit)


@novel_router.get("/{novel_id}/fragments/intent/{chapter_number}")
def chapter_intent(
    novel_id: str, chapter_number: int, session: Session = Depends(get_session)
) -> dict:
    """一章的意图：安排给它的碎片 + 作者写的意图说明（评审只对照意图查执行）。"""
    _novel(session, novel_id)
    return fragment_service.chapter_intent(session, novel_id, chapter_number)


@item_router.get("/{fragment_id}", response_model=FragmentOut)
def get_fragment(fragment_id: str, session: Session = Depends(get_session)) -> dict:
    return fragment_service.fragment_to_dict(_fragment(session, fragment_id))


@item_router.patch("/{fragment_id}", response_model=FragmentOut)
def update_fragment(
    fragment_id: str, payload: FragmentUpdate, session: Session = Depends(get_session)
) -> dict:
    fragment = _fragment(session, fragment_id)
    novel = session.get(Novel, fragment.novel_id)
    fragment = fragment_service.update_fragment(session, novel, fragment, payload)
    return fragment_service.fragment_to_dict(fragment)


@item_router.delete("/{fragment_id}", status_code=204)
def delete_fragment(fragment_id: str, session: Session = Depends(get_session)) -> None:
    fragment = _fragment(session, fragment_id)
    novel = session.get(Novel, fragment.novel_id)
    fragment_service.delete_fragment(session, novel, fragment)


@item_router.post("/{fragment_id}/place", response_model=FragmentOut)
def place_fragment(
    fragment_id: str, payload: FragmentPlaceRequest, session: Session = Depends(get_session)
) -> dict:
    fragment = _fragment(session, fragment_id)
    fragment_service.place(session, fragment, payload.chapter_number)
    return fragment_service.fragment_to_dict(fragment)


@novel_router.post("/{novel_id}/fragments/realize", response_model=FragmentRealizeResponse)
def realize_fragments(
    novel_id: str,
    payload: FragmentRealizeRequest,
    provider: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> FragmentRealizeResponse:
    """把想法碎片写成正文：逐条给出「碎片 → 正文」的对应关系，可核对。"""
    novel = _novel(session, novel_id)
    if provider:
        payload.provider = provider
    if not payload.fragment_ids and not payload.raw_fragments:
        raise HTTPException(status_code=422, detail="至少要给一条碎片（fragment_ids 或 raw_fragments）")
    try:
        return realize_service.realize_fragments(session, novel, payload)
    except ValueError as exc:
        # 例如「第 N 章已存在」：这是作者的输入问题，应该是 409 而不是 500
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@novel_router.post("/{novel_id}/fragments/prompts", response_model=EmotionPromptResponse)
def emotion_prompts(
    novel_id: str,
    payload: EmotionPromptRequest,
    provider: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> EmotionPromptResponse:
    """情感引导：只向作者提问，不代写；作者的回答再存成碎片。"""
    novel = _novel(session, novel_id)
    result = realize_service.emotion_prompts(
        session,
        novel,
        goals=payload.goals,
        characters=payload.characters,
        chapter_number=payload.chapter_number,
    )
    return EmotionPromptResponse(**result)


@novel_router.get("/{novel_id}/style/voice", response_model=VoiceProfileOut)
def get_voice_profile(novel_id: str, session: Session = Depends(get_session)) -> VoiceProfileOut:
    """作者的声音画像：他惯用的词、标点习惯与断句方式。"""
    _novel(session, novel_id)
    profile = style_service.default_voice_profile(session, novel_id)
    if profile is None:
        return VoiceProfileOut(available=False)
    metrics = profile.metrics or {}
    return VoiceProfileOut(
        available=True,
        name=profile.name,
        sample_count=profile.sample_count,
        total_chars=profile.total_chars,
        signature_terms=list(metrics.get("signature_terms") or [])[:40],
        punctuation={
            mark: float(share) for mark, share in (metrics.get("punctuation") or {}).items()
        },
        created_at=profile.created_at,
    )


@novel_router.post("/{novel_id}/style/voice", response_model=VoiceProfileOut)
def build_voice_profile_endpoint(
    novel_id: str,
    payload: StyleProfileRequest,
    session: Session = Depends(get_session),
) -> VoiceProfileOut:
    """用作者自己的文字建立声音画像：可以指定章节，也可以直接贴他的文本/碎片。"""
    novel = _novel(session, novel_id)
    texts = [text for text in payload.texts if text.strip()]
    samples: list[str] = []
    if payload.chapter_numbers:
        from sqlalchemy import select

        chapters = list(
            session.scalars(
                select(Chapter).where(
                    Chapter.novel_id == novel.id,
                    Chapter.chapter_number.in_(payload.chapter_numbers),
                )
            )
        )
        texts.extend(chapter.content for chapter in chapters)
        samples.extend(f"第{chapter.chapter_number}章" for chapter in chapters)
    if not texts:
        profile = style_service.profile_from_chapters(
            session, novel, name=payload.name or "作者声音（本书章节）"
        )
        voice = style_service.default_voice_profile(session, novel.id)
        if profile is None or voice is None:
            raise HTTPException(status_code=409, detail="样本不足：至少需要 3 章已完成正文，或直接提供文本")
        profile = voice
    else:
        profile = style_service.save_voice_profile(
            session, novel, texts, name=payload.name or "作者声音", samples=samples
        )
        if profile is None:
            raise HTTPException(status_code=409, detail="样本不足：至少需要 2 段不少于 20 字的文本")
    metrics = profile.metrics or {}
    return VoiceProfileOut(
        available=True,
        name=profile.name,
        sample_count=profile.sample_count,
        total_chars=profile.total_chars,
        signature_terms=list(metrics.get("signature_terms") or [])[:40],
        punctuation={
            mark: float(share) for mark, share in (metrics.get("punctuation") or {}).items()
        },
        created_at=profile.created_at,
    )
