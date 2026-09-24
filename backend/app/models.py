"""SQLite 数据模型（SQLAlchemy 2.0 声明式）。

设计要点：
- CanonFact 有明确状态机：PROPOSED → CANON / REJECTED，被新事实取代则置 SUPERSEDED。
  AI 只能创建 PROPOSED，只有后端在用户确认后才写入 CANON（见 services/extraction_service.py）。
- 人物动态状态既保存在 characters.current_status/current_location，也逐次记录到
  character_states 历史表，便于回溯「某章时人物处于什么状态」。
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CanonStatus:
    CANON = "CANON"
    PROPOSED = "PROPOSED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"

    ALL = (CANON, PROPOSED, REJECTED, SUPERSEDED)


class ForeshadowStatus:
    OPEN = "OPEN"
    DEVELOPING = "DEVELOPING"
    RESOLVED = "RESOLVED"
    ABANDONED = "ABANDONED"

    ALL = (OPEN, DEVELOPING, RESOLVED, ABANDONED)


class Visibility:
    PUBLIC = "PUBLIC"
    SECRET = "SECRET"

    ALL = (PUBLIC, SECRET)


class ChapterStatus:
    DRAFT = "DRAFT"
    COMPLETED = "COMPLETED"

    ALL = (DRAFT, COMPLETED)


class ExtractionItemKind:
    CHARACTER_STATE = "CHARACTER_STATE"
    EVENT = "EVENT"
    TIMELINE = "TIMELINE"
    FACT = "FACT"
    FORESHADOWING = "FORESHADOWING"
    RELATIONSHIP = "RELATIONSHIP"
    LOCATION = "LOCATION"

    ALL = (CHARACTER_STATE, EVENT, TIMELINE, FACT, FORESHADOWING, RELATIONSHIP, LOCATION)


class ItemReviewStatus:
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"

    ALL = (PENDING, ACCEPTED, REJECTED)


class RunStatus:
    PENDING_REVIEW = "PENDING_REVIEW"
    PARTIALLY_APPLIED = "PARTIALLY_APPLIED"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"

    ALL = (PENDING_REVIEW, PARTIALLY_APPLIED, APPLIED, REJECTED)


class Novel(Base):
    __tablename__ = "novels"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("nov"))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    synopsis: Mapped[str] = mapped_column(Text, default="")
    genre: Mapped[str] = mapped_column(String(64), default="")
    worldview: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(64), default="")
    target_word_count: Mapped[int] = mapped_column(Integer, default=1_000_000)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    chapter_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    chapters: Mapped[list[Chapter]] = relationship(
        back_populates="novel", cascade="all, delete-orphan", order_by="Chapter.chapter_number"
    )
    characters: Mapped[list[Character]] = relationship(
        back_populates="novel", cascade="all, delete-orphan"
    )


class Chapter(Base):
    __tablename__ = "chapters"
    __table_args__ = (UniqueConstraint("novel_id", "chapter_number", name="uq_chapter_number"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("chp"))
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id", ondelete="CASCADE"), index=True)
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default=ChapterStatus.DRAFT)
    content_path: Mapped[str] = mapped_column(String(400), default="")
    story_time: Mapped[str | None] = mapped_column(String(64), nullable=True)
    location: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    novel: Mapped[Novel] = relationship(back_populates="chapters")


class Character(Base):
    __tablename__ = "characters"
    __table_args__ = (UniqueConstraint("novel_id", "name", name="uq_character_name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("chr"))
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    personality: Mapped[str] = mapped_column(Text, default="")
    background: Mapped[str] = mapped_column(Text, default="")
    goals: Mapped[list] = mapped_column(JSON, default=list)
    fears: Mapped[list] = mapped_column(JSON, default=list)
    current_location: Mapped[str] = mapped_column(String(64), default="")
    current_status: Mapped[str] = mapped_column(String(64), default="ACTIVE")
    known_facts: Mapped[list] = mapped_column(JSON, default=list)
    unknown_facts: Mapped[list] = mapped_column(JSON, default=list)
    first_appearance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_appearance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    novel: Mapped[Novel] = relationship(back_populates="characters")
    states: Mapped[list[CharacterState]] = relationship(
        back_populates="character",
        cascade="all, delete-orphan",
        order_by="CharacterState.chapter_number",
    )


class CharacterState(Base):
    """人物状态历史：每次确认人物状态变更时追加一条。"""

    __tablename__ = "character_states"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("cst"))
    character_id: Mapped[str] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"), index=True
    )
    chapter_id: Mapped[str | None] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL"), nullable=True
    )
    chapter_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="")
    location: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(32), default="MANUAL")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    character: Mapped[Character] = relationship(back_populates="states")


class Relationship(Base):
    __tablename__ = "relationships"
    __table_args__ = (
        UniqueConstraint("novel_id", "character_a_id", "character_b_id", name="uq_relationship"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("rel"))
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id", ondelete="CASCADE"), index=True)
    character_a_id: Mapped[str] = mapped_column(ForeignKey("characters.id", ondelete="CASCADE"))
    character_b_id: Mapped[str] = mapped_column(ForeignKey("characters.id", ondelete="CASCADE"))
    relation: Mapped[str] = mapped_column(String(64), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    source_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("evt"))
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[str | None] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL"), nullable=True
    )
    chapter_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    time: Mapped[str | None] = mapped_column(String(64), nullable=True)
    location: Mapped[str] = mapped_column(String(64), default="")
    characters: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text, default="")
    consequences: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default=CanonStatus.CANON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CanonFact(Base):
    __tablename__ = "canon_facts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("cf"))
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    predicate: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    object: Mapped[str] = mapped_column(Text, nullable=False)
    source_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 时点视图：这条事实从哪一章开始成立、到哪一章被取代（含首不含尾）
    valid_from_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    valid_until_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=CanonStatus.PROPOSED, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    visibility: Mapped[str] = mapped_column(String(16), default=Visibility.PUBLIC)
    known_by: Mapped[list] = mapped_column(JSON, default=list)
    origin: Mapped[str] = mapped_column(String(32), default="USER")
    note: Mapped[str] = mapped_column(Text, default="")
    superseded_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Foreshadowing(Base):
    __tablename__ = "foreshadowings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("fs"))
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    first_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    related_characters: Mapped[list] = mapped_column(JSON, default=list)
    expected_payoff: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(16), default=ForeshadowStatus.OPEN)
    last_reinforced_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TimelineEntry(Base):
    __tablename__ = "timeline_entries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("tl"))
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id", ondelete="CASCADE"), index=True)
    story_time: Mapped[str] = mapped_column(String(64), nullable=False)
    story_time_sort: Mapped[int] = mapped_column(Integer, default=9_999_999, index=True)
    chapter_id: Mapped[str | None] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL"), nullable=True
    )
    chapter_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event: Mapped[str] = mapped_column(String(200), default="")
    location: Mapped[str] = mapped_column(String(64), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default=CanonStatus.CANON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorldRule(Base):
    __tablename__ = "world_rules"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("wr"))
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(48), default="freeform")
    subject: Mapped[str] = mapped_column(String(64), default="")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    source_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExtractionRun(Base):
    """一次章节抽取（ExtractorAgent）的完整结果，等待用户逐项确认。"""

    __tablename__ = "extraction_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("run"))
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), index=True)
    chapter_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default=RunStatus.PENDING_REVIEW)
    provider: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    raw_response: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items: Mapped[list[ExtractionItem]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="ExtractionItem.created_at"
    )


class ExtractionItem(Base):
    """抽取结果中的单条待确认项。FACT 类同时会在 canon_facts 里插入 PROPOSED 行。"""

    __tablename__ = "extraction_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("itm"))
    run_id: Mapped[str] = mapped_column(ForeignKey("extraction_runs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    review_status: Mapped[str] = mapped_column(String(16), default=ItemReviewStatus.PENDING)
    linked_fact_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    applied_ref_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[ExtractionRun] = relationship(back_populates="items")


class ContinuityReport(Base):
    __tablename__ = "continuity_reports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("cr"))
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), index=True)
    chapter_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    errors: Mapped[list] = mapped_column(JSON, default=list)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    dropped_issues: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MemoryQuery(Base):
    """MemorySearch 的问答审计记录：问题、答案、证据与置信度。"""

    __tablename__ = "memory_queries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("mq"))
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[str] = mapped_column(String(16), default="UNKNOWN")
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    provider: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GenerationRecord(Base):
    """ChapterWriter 的生成记录（草稿正文与检索到的 Canon 快照）。"""

    __tablename__ = "generation_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("gen"))
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[str | None] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL"), nullable=True
    )
    request: Mapped[dict] = mapped_column(JSON, default=dict)
    retrieved: Mapped[dict] = mapped_column(JSON, default=dict)
    content: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EmbeddingRecord(Base):
    """向量索引：一条记录对应一个文本片段（章节切块或某条设定）。"""

    __tablename__ = "embeddings"
    __table_args__ = (
        UniqueConstraint("novel_id", "ref_type", "ref_id", "chunk_index", name="uq_embedding_chunk"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("emb"))
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id", ondelete="CASCADE"), index=True)
    ref_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    ref_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    chapter_number: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    #: float32 序列化后的向量（array('f').tobytes()）
    vector: Mapped[bytes] = mapped_column(LargeBinary, default=b"")
    dim: Mapped[int] = mapped_column(Integer, default=0)
    provider: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChapterPlan(Base):
    """章节计划：可以由 PlannerAgent 生成，也可以由作者手写。"""

    __tablename__ = "chapter_plans"
    __table_args__ = (UniqueConstraint("novel_id", "chapter_number", name="uq_plan_chapter"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("plan"))
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id", ondelete="CASCADE"), index=True)
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    goals: Mapped[str] = mapped_column(Text, default="")
    must_include: Mapped[list] = mapped_column(JSON, default=list)
    forbidden: Mapped[list] = mapped_column(JSON, default=list)
    characters: Mapped[list] = mapped_column(JSON, default=list)
    advance_foreshadowing: Mapped[list] = mapped_column(JSON, default=list)
    rationale: Mapped[str] = mapped_column(Text, default="")
    steer: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="PLANNED")
    source: Mapped[str] = mapped_column(String(16), default="PLANNER")
    provider: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class SweepRun(Base):
    """全量一致性扫描的一次执行记录。"""

    __tablename__ = "sweep_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("sweep"))
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(16), default="rules")
    provider: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    chapters_total: Mapped[int] = mapped_column(Integer, default=0)
    chapters_checked: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    warnings: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Meta(Base):
    """键值元数据（当前用于 schema_version）。"""

    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Commitment(Base):
    """承诺账本：谁答应/威胁/约定了什么、期限是什么时候。

    长篇小说最常见的逻辑硬伤是「说了要做却一直没做」：约在听雨楼见、三日内回山、
    扬言三日后来取命……这些在几十章之后很容易被写忘。这里把它们记成可检查的对象。
    """

    __tablename__ = "commitments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("cmt"))
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id", ondelete="CASCADE"), index=True)
    source_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(24), default="APPOINTMENT")
    who: Mapped[str] = mapped_column(String(64), default="")
    counterpart: Mapped[str] = mapped_column(String(64), default="")
    what: Mapped[str] = mapped_column(Text, default="")
    quote: Mapped[str] = mapped_column(Text, default="")
    deadline_text: Mapped[str] = mapped_column(String(64), default="")
    #: 由「三日后」+ 来源章故事时间推算出的到期故事时间（可比较的整数键）
    due_story_time: Mapped[str | None] = mapped_column(String(64), nullable=True)
    due_sort: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    #: OPEN / FULFILLED / OVERDUE / ABANDONED
    status: Mapped[str] = mapped_column(String(16), default="OPEN", index=True)
    fulfilled_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(16), default="EXTRACTOR")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class StyleProfile(Base):
    """文风基线：作者认可的样章统计出来的指标分布，用来判定新章是否「不像这本书」。"""

    __tablename__ = "style_profiles"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("sty"))
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    source: Mapped[str] = mapped_column(String(24), default="CHAPTERS")
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    total_chars: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    samples: Mapped[list] = mapped_column(JSON, default=list)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    #: V0.5：作者把某个基线「定下来」之后，新章要按更严的窗口比对，避免文风慢慢漂走
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InvariantReport(Base):
    """全量不变量检查的一次结果（跨全书的硬约束）。"""

    __tablename__ = "invariant_reports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("inv"))
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id", ondelete="CASCADE"), index=True)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    warnings: Mapped[int] = mapped_column(Integer, default=0)
    codes: Mapped[dict] = mapped_column(JSON, default=dict)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Fragment(Base):
    """想法碎片：作者随时丢进来的奇思妙想、画面、一句想写的话、对世界的看法。

    这是 V0.4 的入口对象 —— 系统的定位不是「替作者写小说」，而是把碎片写成有文学气息的正文。
    碎片保留原文（可能很碎、语法不完整），成文后回填 realized_excerpt 与对应关系，可核对。
    """

    __tablename__ = "fragments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("frg"))
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24), default="WHIM", index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    #: 作者原话，允许不完整、口语化、跳跃
    text: Mapped[str] = mapped_column(Text, default="")
    #: 作者想让它起什么作用（意图显性化：评审只对照意图查执行）
    intent: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    related_characters: Mapped[list] = mapped_column(JSON, default=list)
    #: 作者希望它出现在第几章（null 表示还没安排）
    target_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    #: INBOX（刚丢进来）/ PLACED（已安排）/ REALIZED（已写成正文）/ ARCHIVED（暂不使用）
    status: Mapped[str] = mapped_column(String(16), default="INBOX", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=3)
    origin: Mapped[str] = mapped_column(String(16), default="USER")
    #: 若这条碎片是回答某个引导问题时产生的，记下那个问题（情感深度的入口）
    prompted_by: Mapped[str] = mapped_column(Text, default="")
    #: 成文回填
    realized_chapter_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    realized_excerpt: Mapped[str] = mapped_column(Text, default="")
    realized_treatment: Mapped[str] = mapped_column(String(16), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class StyleReview(Base):
    """一次文风评审记录（可回溯：某一稿的指标与问题）。"""

    __tablename__ = "style_reviews"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("rev"))
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[str | None] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL"), nullable=True
    )
    chapter_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    label: Mapped[str] = mapped_column(String(64), default="")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    provider: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)



class ClaimReport(Base):
    """一次「声称核对」报告：正文里每一条设定断言与 Canon／世界观规则的比对结论。

    这是一份**报告**：核对本身绝不写 Canon，确认与否由作者在界面上决定。
    """

    __tablename__ = "claim_reports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("clm"))
    novel_id: Mapped[str] = mapped_column(ForeignKey("novels.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[str | None] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL"), nullable=True
    )
    chapter_number: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    label: Mapped[str] = mapped_column(String(24), default="DRAFT")
    as_of_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    claim_count: Mapped[int] = mapped_column(Integer, default=0)
    supported_count: Mapped[int] = mapped_column(Integer, default=0)
    unverified_count: Mapped[int] = mapped_column(Integer, default=0)
    conflict_count: Mapped[int] = mapped_column(Integer, default=0)
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    provider: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

