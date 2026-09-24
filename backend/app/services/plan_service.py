"""章节计划服务：生成、维护计划，并把计划转成写作请求。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.ai.agents.planner import PlannerAgent
from app.ai.factory import resolve_provider
from app.models import Chapter, ChapterPlan, Novel
from app.schemas import ChapterPlanCreate, PlanGenerateRequest, WriteChapterRequest

PLAN_STATUSES = ("PLANNED", "WRITTEN", "DISCARDED")
#: 这些状态的人物不能被安排成出场人物（只能出现在 forbidden 或追述语境里）
UNAVAILABLE_TOKENS = ("死亡", "已死", "重伤昏迷", "昏迷", "失踪", "被囚", "封印")


def unavailable_characters(context: dict[str, Any]) -> dict[str, str]:
    """从规划上下文里挑出无法行动的人物：名字 -> 状态。"""
    unavailable: dict[str, str] = {}
    for character in context.get("characters") or []:
        status = str(character.get("current_status") or "")
        if any(token in status for token in UNAVAILABLE_TOKENS):
            unavailable[str(character.get("name"))] = status
    return unavailable


def enforce_cast_constraints(
    chapters, unavailable: dict[str, str]
) -> list[str]:
    """代码兜底：模型若把无法行动的人物写成出场人物，自动移出并写入 forbidden。

    只靠提示词约束模型是不可靠的，这条不变式必须在服务层强制执行并可复现。
    """
    warnings: list[str] = []
    for item in chapters:
        blocked = [name for name in item.characters if name in unavailable]
        if not blocked:
            continue
        item.characters = [name for name in item.characters if name not in blocked]
        item.forbidden = list(dict.fromkeys([*item.forbidden, *blocked]))
        detail = "、".join(f"{name}（{unavailable[name]}）" for name in blocked)
        warnings.append(
            f"第{item.chapter_number}章：{detail}无法行动，已移出出场人物并写入禁止出现"
        )
    return warnings


def list_plans(
    session: Session, novel_id: str, *, status: str | None = None, from_chapter: int | None = None
) -> list[ChapterPlan]:
    stmt = select(ChapterPlan).where(ChapterPlan.novel_id == novel_id)
    if status:
        stmt = stmt.where(ChapterPlan.status == status)
    if from_chapter:
        stmt = stmt.where(ChapterPlan.chapter_number >= from_chapter)
    return list(session.scalars(stmt.order_by(ChapterPlan.chapter_number)))


def sync_status(session: Session, novel: Novel) -> int:
    """把计划状态与正文对齐：写出正文的标记 WRITTEN，正文被删掉的退回 PLANNED。"""
    written = {
        number
        for number in session.scalars(
            select(Chapter.chapter_number).where(Chapter.novel_id == novel.id)
        )
    }
    changed = 0
    for plan in list_plans(session, novel.id):
        if plan.chapter_number in written and plan.status == "PLANNED":
            plan.status = "WRITTEN"
            changed += 1
        elif plan.chapter_number not in written and plan.status == "WRITTEN":
            plan.status = "PLANNED"
            changed += 1
    if changed:
        session.flush()
    return changed


def generate_plans(
    session: Session,
    novel: Novel,
    payload: PlanGenerateRequest,
) -> dict[str, Any]:
    provider, warnings = resolve_provider(payload.provider)
    agent = PlannerAgent(provider)
    result, meta = agent.plan(
        session,
        novel,
        from_chapter=payload.from_chapter,
        count=payload.count,
        steer=payload.steer,
    )
    warnings.extend(meta["warnings"])
    warnings.extend(enforce_cast_constraints(result.chapters, unavailable_characters(meta["context"])))

    existing = {plan.chapter_number: plan for plan in list_plans(session, novel.id)}
    written = {
        number
        for number in session.scalars(
            select(Chapter.chapter_number).where(Chapter.novel_id == novel.id)
        )
    }
    saved: list[ChapterPlan] = []
    skipped: list[dict[str, Any]] = []
    for item in result.chapters:
        if item.chapter_number in written:
            skipped.append(
                {"chapter_number": item.chapter_number, "reason": "该章已有正文，计划未保存"}
            )
            continue
        current = existing.get(item.chapter_number)
        if current is not None and not payload.overwrite:
            skipped.append(
                {"chapter_number": item.chapter_number, "reason": "已有计划，未覆盖（可传 overwrite=true）"}
            )
            continue
        plan = current or ChapterPlan(novel_id=novel.id, chapter_number=item.chapter_number)
        plan.title = item.title or f"第{item.chapter_number}章"
        plan.goals = item.goals
        plan.must_include = item.must_include
        plan.forbidden = item.forbidden
        plan.characters = item.characters
        plan.advance_foreshadowing = item.advance_foreshadowing
        plan.rationale = item.rationale
        plan.steer = payload.steer
        plan.status = "PLANNED"
        plan.source = "PLANNER"
        plan.provider = meta["provider"]
        plan.model = meta["model"]
        session.add(plan)
        saved.append(plan)
    session.flush()

    context = meta["context"]
    return {
        "plans": saved,
        "provider": meta["provider"],
        "model": meta["model"],
        "warnings": warnings,
        "context_summary": {
            "frontier_chapter": context["frontier"],
            "canon_facts": len(context["canon_facts"]),
            "characters": len(context["characters"]),
            "foreshadowing_debt": len(context["foreshadowing_debt"]),
            "recent_summaries": len(context["recent_summaries"]),
            "skipped": skipped,
        },
    }


def upsert_plan(session: Session, novel: Novel, payload: ChapterPlanCreate) -> ChapterPlan:
    plan = session.scalar(
        select(ChapterPlan).where(
            ChapterPlan.novel_id == novel.id, ChapterPlan.chapter_number == payload.chapter_number
        )
    )
    if plan is None:
        plan = ChapterPlan(novel_id=novel.id, chapter_number=payload.chapter_number)
        session.add(plan)
        plan.source = "USER"
    status = payload.status if payload.status in PLAN_STATUSES else "PLANNED"
    plan.title = payload.title or plan.title or f"第{payload.chapter_number}章"
    plan.goals = payload.goals
    plan.must_include = payload.must_include
    plan.forbidden = payload.forbidden
    plan.characters = payload.characters
    plan.advance_foreshadowing = payload.advance_foreshadowing
    plan.rationale = payload.rationale or plan.rationale
    plan.steer = payload.steer
    plan.status = status
    session.flush()
    return plan


def delete_plan(session: Session, plan: ChapterPlan) -> None:
    session.execute(delete(ChapterPlan).where(ChapterPlan.id == plan.id))


def get_plan(session: Session, plan_id: str) -> ChapterPlan | None:
    return session.get(ChapterPlan, plan_id)


def write_request_from_dict(data: dict[str, Any]) -> WriteChapterRequest:
    """把接口层的请求体（可能带 max_rounds 等额外字段）转成写作请求。"""
    allowed = {
        "goals",
        "must_include",
        "forbidden",
        "characters",
        "chapter_number",
        "title",
        "story_time",
        "location",
        "target_words",
        "provider",
        "save",
    }
    return WriteChapterRequest(**{key: value for key, value in data.items() if key in allowed})


def plan_to_write_request(plan: ChapterPlan, *, target_words: int = 1500) -> WriteChapterRequest:
    """把计划直接变成写作请求：计划里的必须/禁止/人物直接成为写作约束。"""
    return WriteChapterRequest(
        goals=plan.goals or f"按计划写第{plan.chapter_number}章",
        must_include=list(plan.must_include or []),
        forbidden=list(plan.forbidden or []),
        characters=list(plan.characters or []),
        chapter_number=plan.chapter_number,
        title=plan.title,
        target_words=target_words,
    )
