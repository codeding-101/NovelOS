"""Obsidian 对接接口：状态、配置、导入笔记、导出设定库。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_session
from app.models import Novel
from app.schemas import (
    ObsidianConfigRequest,
    ObsidianStatusOut,
    VaultImportRequest,
    VaultImportResult,
    VaultExportRequest,
    VaultExportResult,
)
from app.services import obsidian_service

novel_router = APIRouter(prefix="/api/novels", tags=["Obsidian"])
root_router = APIRouter(prefix="/api", tags=["Obsidian"])


def _novel(session: Session, novel_id: str) -> Novel:
    novel = session.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(status_code=404, detail="小说不存在")
    return novel


@root_router.get("/obsidian", response_model=ObsidianStatusOut)
def obsidian_status(vault: str | None = None, inbox: str | None = None) -> dict:
    """看当前连接的库：路径从哪来的、收件箱在哪、之前导出过多少篇。

    库路径优先取显式参数，其次环境变量 NOVELOS_OBSIDIAN_VAULT，
    再其次配置里存的，最后从 Obsidian 自己的配置里探测（通常是当前打开的那个库）。
    """
    return obsidian_service.vault_status(vault, inbox)


@root_router.post("/obsidian", response_model=ObsidianStatusOut)
def obsidian_config(payload: ObsidianConfigRequest) -> dict:
    """改库路径／收件箱／导出目录／导入标签（存进数据目录的 integrations.json）。"""
    patch: dict = {}
    if payload.vault is not None:
        patch["vault"] = payload.vault
    if payload.inbox is not None:
        patch["inbox"] = payload.inbox
    if payload.export_dir is not None:
        patch["export_dir"] = payload.export_dir
    if payload.tags is not None:
        patch["tags"] = payload.tags
    obsidian_service.save_integrations(patch)
    return obsidian_service.vault_status(payload.vault, payload.inbox)


@novel_router.post("/{novel_id}/obsidian/import", response_model=VaultImportResult)
def import_from_vault(
    novel_id: str, payload: VaultImportRequest, session: Session = Depends(get_session)
) -> dict:
    """把库里收件箱的笔记导入成想法碎片。幂等：同一篇笔记重复导入只更新正文。"""
    novel = _novel(session, novel_id)
    result = obsidian_service.import_notes(
        session,
        novel,
        vault_path=payload.vault,
        inbox=payload.inbox,
        tags=payload.tags,
        limit=payload.limit,
    )
    session.commit()
    return result


@novel_router.post("/{novel_id}/obsidian/export", response_model=VaultExportResult)
def export_to_vault(
    novel_id: str, payload: VaultExportRequest, session: Session = Depends(get_session)
) -> dict:
    """把设定库导出成带双链的笔记。在库里改过的那篇不覆盖，另存 .conflict.md。"""
    novel = _novel(session, novel_id)
    result = obsidian_service.export_bible(
        session, novel, vault_path=payload.vault, subdir=payload.subdir
    )
    session.commit()
    return result
