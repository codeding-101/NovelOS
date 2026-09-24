"""碎片成文编排：碎片 → 文学化正文 →（可选）评审改稿 →（可选）落成章节 → 回填碎片。

这一步是整个系统的转向所在：AI 不提供故事，只提供「把作者的想法写成文学」这项手艺。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.ai.agents.fragment_realizer import FragmentRealizer, merge_back
from app.models import Chapter, Novel
from app.schemas import (
    ChapterCreate,
    FragmentRealizeRequest,
    FragmentRealizeResponse,
    WriteChapterRequest,
)
from app.services import chapter_service, extraction_service, fragment_service, style_service


def realize_fragments(
    session: Session, novel: Novel, request: FragmentRealizeRequest
) -> FragmentRealizeResponse:
    agents = extraction_service.build_agents(request.provider)
    realizer: FragmentRealizer = agents["realizer"]
    realization, meta = realizer.realize(session, novel, request)
    warnings = list(agents["warnings"]) + list(meta["warnings"])

    style_report: dict[str, Any] = {}
    # 成文后按同一套评审走一遍：套话要压，作者的声音不能丢
    if request.revise:
        loop = agents["revision"]
        # 修订闭环里的写手请求要求至少 300 字；碎片很少时按 300 走，并如实说明
        target_words = max(300, request.target_words)
        if target_words != request.target_words:
            warnings.append("碎片过少，目标字数已按修订闭环的下限 300 字执行")
        write_request = WriteChapterRequest(
            goals=request.goals or "把作者的想法碎片写成正文",
            characters=request.characters,
            forbidden=request.forbidden,
            chapter_number=request.chapter_number,
            title=realization.title,
            target_words=target_words,
            save=request.save,
        )
        result = loop.run(
            session,
            novel,
            write_request,
            max_rounds=request.max_rounds,
            target_score=request.target_score,
            use_model_critic=request.use_model_critic,
            persist_reviews=True,
            initial_content=realization.content,
            initial_title=realization.title,
        )
        realization.content = result.draft.content
        realization.word_count = result.draft.word_count
        realization.saved_chapter_id = result.saved_chapter_id
        warnings.extend(result.warnings)
        voice_report = (result.rounds[-1].voice_score if result.rounds else None)
        style_report = {
            "final_score": result.final_score,
            "accepted": result.accepted,
            "rounds": [row.to_dict() for row in result.rounds],
            "metric_deltas": result.metric_deltas,
            "voice_score": voice_report,
        }
    elif request.save:
        chapter = chapter_service.create_chapter(
            session,
            novel,
            ChapterCreate(
                chapter_number=request.chapter_number,
                title=realization.title,
                content=realization.content,
                summary="由「碎片成文」生成（未经改稿）",
                status="DRAFT",
            ),
        )
        realization.saved_chapter_id = chapter.id

    if realization.saved_chapter_id:
        chapter = session.get(Chapter, realization.saved_chapter_id)
        realization.chapter_number = chapter.chapter_number if chapter else None

    updated = merge_back(session, novel, meta["context"]["fragments"], realization)

    # 最终稿的文风与声音体检（用于界面展示：套话有没有、像不像作者）
    voice_profile = style_service.default_voice_profile(session, novel.id)
    profile = style_service.default_profile(session, novel.id)
    final_review = style_service.review_text(
        realization.content, profile=profile, voice_profile=voice_profile
    )
    if not style_report:
        style_report = {
            "final_score": final_review["score"],
            "accepted": None,
            "issues": final_review["issues"],
            "metrics": final_review["metrics"],
        }
    session.flush()

    return FragmentRealizeResponse(
        realization=realization,
        provider=meta["provider"],
        model=meta["model"],
        warnings=warnings,
        style=style_report,
        voice=final_review.get("voice") or {},
        fragments_updated=updated,
    )


def emotion_prompts(
    session: Session, novel: Novel, *, goals: str, characters: list[str], chapter_number: int | None
) -> dict[str, Any]:
    """情感引导：只提问，不代写。作者的回答会变成新碎片。"""
    agents = extraction_service.build_agents()
    provider = agents["provider"]
    fragments = fragment_service.list_fragments(
        session, novel.id, target_chapter=chapter_number, limit=10
    ) if chapter_number else []
    from app.ai import prompts
    from app.ai.base import AIRequest

    character_block = "\n".join(characters) or "（未指定）"
    fragment_block = (
        "\n".join(f"- {fragment.text.strip()[:60]}" for fragment in fragments) or "（暂无）"
    )
    request = AIRequest(
        task="emotion_prompts",
        system=prompts.EMOTION_SYSTEM,
        prompt=prompts.EMOTION_USER.format(
            goals=goals or "（作者没有说明）",
            characters=character_block,
            fragments=fragment_block,
        ),
        context={"goals": goals, "characters": characters, "fragments": [f.text for f in fragments]},
        json_schema={"type": "object"},
        temperature=0.6,
        max_tokens=1024,
    )
    response = provider.generate(request)
    questions: list[str] = []
    if response.parsed and isinstance(response.parsed.get("questions"), list):
        questions = [str(item) for item in response.parsed["questions"] if str(item).strip()]
    warnings = list(agents["warnings"]) + list(response.warnings)
    if not questions:
        warnings.append("没有拿到引导问题，可稍后重试或自行写下当时的细节")
    return {
        "questions": questions[:6],
        "provider": response.provider,
        "model": response.model,
        "warnings": warnings,
    }
