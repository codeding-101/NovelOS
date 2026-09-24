"""小说项目路由。"""

from __future__ import annotations

import shutil

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_session
from app.models import Novel
from app.schemas import NovelCreate, NovelOut, NovelStats, NovelUpdate
from app.services import novel_service, search_service

router = APIRouter(prefix="/api/novels", tags=["小说"])


@router.get("", response_model=list[NovelOut])
def list_novels(session: Session = Depends(get_session)) -> list[Novel]:
    return list(session.scalars(select(Novel).order_by(Novel.created_at.desc())))


@router.post("", response_model=NovelOut, status_code=status.HTTP_201_CREATED)
def create_novel(payload: NovelCreate, session: Session = Depends(get_session)) -> Novel:
    novel = Novel(
        title=payload.title,
        slug="",
        synopsis=payload.synopsis,
        genre=payload.genre,
        worldview=payload.worldview,
        author=payload.author,
        target_word_count=payload.target_word_count,
    )
    session.add(novel)
    session.flush()
    base = payload.slug or novel_service.make_slug(payload.title, novel.id)
    novel.slug = novel_service.unique_slug(session, base)
    session.flush()
    return novel


@router.get("/{novel_id}", response_model=NovelOut)
def get_novel(novel_id: str, session: Session = Depends(get_session)) -> Novel:
    novel = session.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(status_code=404, detail="小说不存在")
    novel_service.recount(session, novel)
    return novel


@router.patch("/{novel_id}", response_model=NovelOut)
def update_novel(
    novel_id: str, payload: NovelUpdate, session: Session = Depends(get_session)
) -> Novel:
    novel = session.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(status_code=404, detail="小说不存在")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(novel, key, value)
    novel_service.recount(session, novel)
    return novel


@router.delete("/{novel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_novel(novel_id: str, session: Session = Depends(get_session)) -> None:
    novel = session.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(status_code=404, detail="小说不存在")
    directory = settings.chapters_dir / novel.slug
    session.delete(novel)
    session.flush()
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)


@router.get("/{novel_id}/stats", response_model=NovelStats)
def novel_stats(novel_id: str, session: Session = Depends(get_session)) -> dict:
    novel = session.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(status_code=404, detail="小说不存在")
    novel_service.recount(session, novel)
    return novel_service.build_stats(session, novel)


@router.post("/{novel_id}/reindex")
def reindex(novel_id: str, session: Session = Depends(get_session)) -> dict:
    novel = session.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(status_code=404, detail="小说不存在")
    count = search_service.rebuild_index(session, novel.id)
    return {
        "indexed": count,
        "engine": "fts5-trigram" if search_service.fts_available(session) else "like",
    }


@router.post("/{novel_id}/recount", response_model=NovelOut)
def recount(novel_id: str, session: Session = Depends(get_session)) -> Novel:
    novel = session.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(status_code=404, detail="小说不存在")
    return novel_service.recount(session, novel)
