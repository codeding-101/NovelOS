"""V0.3 质量层接口：文风基线 / 文风评审 / 全局不变量 / 承诺账本 / 修订闭环。V0.5 增加声称核对与文风锁定。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_session
from app.models import Chapter, Commitment, Novel, StyleProfile, StyleReview
from app.schemas import (
    ChapterDraft,
    ClaimReportOut,
    ClaimVerifyRequest,
    CommitmentCreate,
    CommitmentDecision,
    CommitmentOut,
    InvariantReportOut,
    RevisionRequest,
    RevisionResponse,
    RevisionRoundOut,
    StyleDriftReport,
    StyleLockRequest,
    StyleProfileOut,
    StyleProfileRequest,
    StyleReviewOut,
    StyleTextRequest,
)
from app.services import (
    claim_service,
    commitment_service,
    extraction_service,
    invariant_service,
    plan_service,
    style_service,
)

novel_router = APIRouter(prefix="/api/novels", tags=["质量"])
chapter_router = APIRouter(prefix="/api/chapters", tags=["质量"])
item_router = APIRouter(prefix="/api", tags=["质量"])


def _novel(session: Session, novel_id: str) -> Novel:
    novel = session.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(status_code=404, detail="小说不存在")
    return novel


def _chapter(session: Session, chapter_id: str) -> Chapter:
    chapter = session.get(Chapter, chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="章节不存在")
    return chapter


# --------------------------------------------------------------------------- 文风基线
@novel_router.get("/{novel_id}/style/baseline", response_model=StyleProfileOut | None)
def get_baseline(novel_id: str, session: Session = Depends(get_session)):
    _novel(session, novel_id)
    return style_service.default_profile(session, novel_id)


@novel_router.post("/{novel_id}/style/baseline", response_model=StyleProfileOut)
def build_baseline(
    novel_id: str, payload: StyleProfileRequest, session: Session = Depends(get_session)
) -> StyleProfile:
    """用作者认可的样章建立文风基线：可以指定章节号，也可以直接贴文本。"""
    novel = _novel(session, novel_id)
    texts = [text for text in payload.texts if text.strip()]
    samples: list[str] = []
    if payload.chapter_numbers:
        chapters = list(
            session.scalars(
                select(Chapter).where(
                    Chapter.novel_id == novel.id,
                    Chapter.chapter_number.in_(payload.chapter_numbers),
                )
            )
        )
        if not chapters:
            raise HTTPException(status_code=404, detail="指定的章节不存在")
        texts.extend(chapter.content for chapter in chapters)
        samples.extend(f"第{chapter.chapter_number}章" for chapter in chapters)
    if not texts:
        profile = style_service.profile_from_chapters(session, novel, name=payload.name)
        if profile is None:
            raise HTTPException(
                status_code=409, detail="样本不足：至少需要 3 章已完成且不少于 400 字的正文"
            )
        return profile
    return style_service.save_profile(
        session,
        novel,
        texts,
        name=payload.name,
        source="CHAPTERS" if samples else "TEXTS",
        samples=samples,
        make_default=payload.make_default,
    )


@novel_router.get("/{novel_id}/style/reviews")
def list_style_reviews(
    novel_id: str,
    chapter_number: int | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> list[dict]:
    _novel(session, novel_id)
    stmt = select(StyleReview).where(StyleReview.novel_id == novel_id)
    if chapter_number is not None:
        stmt = stmt.where(StyleReview.chapter_number == chapter_number)
    rows = list(session.scalars(stmt.order_by(StyleReview.created_at.desc()).limit(limit)))
    return [
        {
            "id": row.id,
            "chapter_number": row.chapter_number,
            "label": row.label,
            "score": row.score,
            "codes": [issue.get("code") for issue in (row.issues or [])],
            "metrics": row.metrics,
            "provider": row.provider,
            "model": row.model,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


@chapter_router.post("/{chapter_id}/style-review", response_model=StyleReviewOut)
def review_chapter_style(
    chapter_id: str,
    use_model: bool = Query(default=True, description="是否启用模型读感评审"),
    persist: bool = Query(default=True),
    session: Session = Depends(get_session),
) -> dict:
    chapter = _chapter(session, chapter_id)
    novel = session.get(Novel, chapter.novel_id)
    critic = extraction_service.build_agents()["style"]
    report = critic.review(
        session,
        novel,
        chapter.content or "",
        use_model=use_model,
        persist=persist,
        chapter=chapter,
        label="chapter",
    )
    report["chapter_id"] = chapter.id
    report["chapter_number"] = chapter.chapter_number
    return report


@novel_router.post("/{novel_id}/style/review-text", response_model=StyleReviewOut)
def review_text_style(
    novel_id: str, payload: StyleTextRequest, session: Session = Depends(get_session)
) -> dict:
    """对一段草稿文本做文风评审（不落章节，用于「先看看这稿怎么样」）。"""
    novel = _novel(session, novel_id)
    critic = extraction_service.build_agents()["style"]
    return critic.review(
        session,
        novel,
        payload.text,
        use_model=payload.use_model,
        persist=False,
        label="draft",
    )


# --------------------------------------------------------------------------- 全局不变量
@novel_router.get("/{novel_id}/invariants", response_model=InvariantReportOut)
def get_invariants(
    novel_id: str,
    run: bool = Query(default=False, description="true 则重新检查并留存报告"),
    session: Session = Depends(get_session),
) -> dict:
    novel = _novel(session, novel_id)
    if run:
        report = invariant_service.run_invariants(session, novel)
        return InvariantReportOut(
            novel_id=novel.id,
            report_id=report.get("report_id"),
            created_at=report.get("created_at"),
            errors=len(report["errors"]),
            warnings=len(report["warnings"]),
            codes=report["codes"],
            checked=report["checked"],
            issues=report["errors"] + report["warnings"],
        )
    record = invariant_service.latest_report(session, novel_id)
    if record is None:
        report = invariant_service.check_invariants(session, novel)
        return InvariantReportOut(
            novel_id=novel.id,
            errors=len(report["errors"]),
            warnings=len(report["warnings"]),
            codes=report["codes"],
            checked=report["checked"],
            issues=report["errors"] + report["warnings"],
        )
    return InvariantReportOut(
        novel_id=novel.id,
        report_id=record.id,
        created_at=record.created_at.isoformat(),
        errors=record.errors,
        warnings=record.warnings,
        codes=record.codes or {},
        issues=record.issues or [],
    )


@novel_router.post("/{novel_id}/invariants/run", response_model=InvariantReportOut)
def run_invariants(novel_id: str, session: Session = Depends(get_session)) -> dict:
    novel = _novel(session, novel_id)
    report = invariant_service.run_invariants(session, novel)
    return InvariantReportOut(
        novel_id=novel.id,
        report_id=report.get("report_id"),
        created_at=report.get("created_at"),
        errors=len(report["errors"]),
        warnings=len(report["warnings"]),
        codes=report["codes"],
        checked=report["checked"],
        issues=report["errors"] + report["warnings"],
    )


# --------------------------------------------------------------------------- 承诺账本
@novel_router.get("/{novel_id}/commitments", response_model=list[CommitmentOut])
def list_commitments(
    novel_id: str,
    status: str | None = Query(default=None, description="OPEN / OVERDUE / FULFILLED / ABANDONED"),
    session: Session = Depends(get_session),
) -> list[dict]:
    novel = _novel(session, novel_id)
    items = commitment_service.evaluate(session, novel)
    if status:
        items = [item for item in items if item["status"] == status]
    return items


@novel_router.post("/{novel_id}/commitments", response_model=CommitmentOut, status_code=201)
def create_commitment(
    novel_id: str, payload: CommitmentCreate, session: Session = Depends(get_session)
) -> dict:
    novel = _novel(session, novel_id)
    record = commitment_service.add_commitment(
        session,
        novel,
        source_chapter=payload.source_chapter,
        kind=payload.kind,
        who=payload.who,
        counterpart=payload.counterpart,
        what=payload.what,
        quote=payload.quote,
        deadline_text=payload.deadline_text,
        origin="USER",
        note=payload.note,
    )
    session.flush()
    for item in commitment_service.evaluate(session, novel):
        if item["id"] == record.id:
            return item
    raise HTTPException(status_code=500, detail="承诺写入后未能读取")


@item_router.post("/commitments/{commitment_id}/fulfill", response_model=CommitmentOut)
def fulfill_commitment(
    commitment_id: str, payload: CommitmentDecision, session: Session = Depends(get_session)
) -> dict:
    record = session.get(Commitment, commitment_id)
    if record is None:
        raise HTTPException(status_code=404, detail="承诺不存在")
    commitment_service.fulfil(
        session, record, chapter_number=payload.chapter_number, note=payload.note
    )
    novel = session.get(Novel, record.novel_id)
    for item in commitment_service.evaluate(session, novel):
        if item["id"] == record.id:
            return item
    raise HTTPException(status_code=500, detail="承诺更新后未能读取")


@item_router.post("/commitments/{commitment_id}/abandon", response_model=CommitmentOut)
def abandon_commitment(
    commitment_id: str, payload: CommitmentDecision, session: Session = Depends(get_session)
) -> dict:
    record = session.get(Commitment, commitment_id)
    if record is None:
        raise HTTPException(status_code=404, detail="承诺不存在")
    commitment_service.abandon(session, record, note=payload.note)
    novel = session.get(Novel, record.novel_id)
    for item in commitment_service.evaluate(session, novel):
        if item["id"] == record.id:
            return item
    raise HTTPException(status_code=500, detail="承诺更新后未能读取")


# --------------------------------------------------------------------------- 修订闭环
@novel_router.post("/{novel_id}/ai/revise-chapter", response_model=RevisionResponse)
def revise_chapter(
    novel_id: str,
    payload: RevisionRequest,
    provider: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> RevisionResponse:
    """写作 + 文风/一致性评审 + 自动改稿的闭环（可审计每一轮的指标与问题）。"""
    novel = _novel(session, novel_id)
    agents = extraction_service.build_agents(provider or payload.provider)
    write_request = plan_service.write_request_from_dict(payload.model_dump())
    loop = agents["revision"]
    result = loop.run(
        session,
        novel,
        write_request,
        max_rounds=payload.max_rounds,
        target_score=payload.target_score,
        use_model_critic=payload.use_model_critic,
        persist_reviews=True,
        plan_id=payload.plan_id,
    )
    result.warnings.extend(agents["warnings"])
    if result.plan_id and result.saved_chapter_id:
        plan = plan_service.get_plan(session, result.plan_id)
        if plan is not None:
            plan.status = "WRITTEN"
            session.flush()
    return RevisionResponse(
        draft=ChapterDraft(**result.draft.model_dump()),
        rounds=[RevisionRoundOut(**row.to_dict()) for row in result.rounds],
        accepted=result.accepted,
        final_score=result.final_score,
        metric_deltas=result.metric_deltas,
        retrieved=result.retrieved,
        provider=result.provider,
        model=result.model,
        warnings=result.warnings,
        saved_chapter_id=result.saved_chapter_id,
        generation_id=result.generation_id,
        goal=result.goal,
        plan_id=result.plan_id,
    )


# --------------------------------------------------------------------------- V0.5 声称核对
@novel_router.post("/{novel_id}/claims/verify", response_model=ClaimReportOut)
def verify_claims(
    novel_id: str,
    payload: ClaimVerifyRequest,
    session: Session = Depends(get_session),
) -> dict:
    """核对一段正文（草稿或某一章）里的设定断言：与 Canon／世界观规则逐条比对。

    只出报告，不写 Canon —— 查无此设定的一律是 UNVERIFIED，等作者确认。
    """
    novel = _novel(session, novel_id)
    chapter = None
    text = payload.text
    if payload.chapter_number:
        chapter = session.scalar(
            select(Chapter).where(
                Chapter.novel_id == novel.id,
                Chapter.chapter_number == payload.chapter_number,
            )
        )
        if chapter is None:
            raise HTTPException(status_code=404, detail="章节不存在")
        if not text:
            text = chapter.content or ""
    if not (text or "").strip():
        raise HTTPException(status_code=400, detail="请提供正文（text 或 chapter_number）")
    return claim_service.verify_text(
        session,
        novel,
        text=text,
        chapter=chapter,
        as_of_chapter=payload.as_of_chapter,
        use_model=payload.use_model,
        persist=payload.persist,
        label="CHAPTER" if chapter else "DRAFT",
        provider_name=payload.provider,
    )


@novel_router.get("/{novel_id}/claims", response_model=list[ClaimReportOut])
def list_claim_reports(
    novel_id: str,
    chapter_number: int | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> list[dict]:
    _novel(session, novel_id)
    return claim_service.list_reports(
        session, novel_id, chapter_number=chapter_number, limit=limit
    )


@item_router.get("/claims/{report_id}", response_model=ClaimReportOut)
def get_claim_report(report_id: str, session: Session = Depends(get_session)) -> dict:
    row = claim_service.get_report(session, report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="核对报告不存在")
    return claim_service.report_to_dict(row)


# --------------------------------------------------------------------------- V0.5 文风锁定
@novel_router.post("/{novel_id}/style/baseline/{profile_id}/lock", response_model=StyleProfileOut)
def lock_style_baseline(
    novel_id: str,
    profile_id: str,
    payload: StyleLockRequest,
    session: Session = Depends(get_session),
) -> StyleProfile:
    """锁定／解锁文风基线：锁定后新章按收紧一半的窗口比对，漂移会被点名。"""
    _novel(session, novel_id)
    profile = session.get(StyleProfile, profile_id)
    if profile is None or profile.novel_id != novel_id:
        raise HTTPException(status_code=404, detail="文风基线不存在")
    return style_service.lock_profile(session, profile, payload.locked)


@novel_router.get("/{novel_id}/style/drift", response_model=StyleDriftReport)
def style_drift(
    novel_id: str,
    limit: int | None = Query(default=None, ge=1, le=500),
    session: Session = Depends(get_session),
) -> dict:
    """逐章漂移报告：锁定的文风在哪几章、哪几项指标上漂了。"""
    novel = _novel(session, novel_id)
    return style_service.drift_report(session, novel, limit=limit)

