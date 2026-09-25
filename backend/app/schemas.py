"""Pydantic 校验层：API 出入参与 AI 结构化输出的 Schema。

安全原则的落地位置：
- ContinuityIssue 强制要求证据（每条问题至少一条带来源章节的证据），否则模型输出直接被判非法。
- CanonFact 的写入接口不接受 status=CANON，AI 与 API 都只能产生 PROPOSED（见 routers/canon.py）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import (
    CanonStatus,
    ChapterStatus,
    ForeshadowStatus,
    Visibility,
)

CanonStatusLiteral = Literal["CANON", "PROPOSED", "REJECTED", "SUPERSEDED"]
ForeshadowStatusLiteral = Literal["OPEN", "DEVELOPING", "RESOLVED", "ABANDONED"]
VisibilityLiteral = Literal["PUBLIC", "SECRET"]
ChapterStatusLiteral = Literal["DRAFT", "COMPLETED"]
ReviewStatusLiteral = Literal["PENDING", "ACCEPTED", "REJECTED"]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------------------
# 小说
# --------------------------------------------------------------------------------------
class NovelCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    synopsis: str = ""
    genre: str = ""
    worldview: str = ""
    outline: str = ""
    author: str = ""
    target_word_count: int = Field(default=1_000_000, ge=0, le=20_000_000)
    slug: str | None = Field(default=None, max_length=120)


class NovelUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    synopsis: str | None = None
    genre: str | None = None
    worldview: str | None = None
    outline: str | None = None
    author: str | None = None
    target_word_count: int | None = Field(default=None, ge=0, le=20_000_000)


class NovelOut(ORMModel):
    id: str
    title: str
    slug: str
    synopsis: str
    genre: str
    worldview: str
    outline: str = ""
    author: str
    target_word_count: int
    word_count: int
    chapter_count: int
    created_at: datetime
    updated_at: datetime


class NovelStats(BaseModel):
    novel_id: str
    word_count: int
    chapter_count: int
    target_word_count: int
    progress: float
    character_count: int
    event_count: int
    canon_fact_count: int
    proposed_fact_count: int
    foreshadowing_open_count: int
    timeline_entry_count: int
    pending_review_items: int
    latest_continuity_errors: int
    latest_continuity_warnings: int


# --------------------------------------------------------------------------------------
# 章节
# --------------------------------------------------------------------------------------
class ChapterCreate(BaseModel):
    chapter_number: int | None = Field(default=None, ge=1, le=9999)
    title: str = Field(default="", max_length=200)
    content: str = ""
    summary: str = ""
    story_time: str | None = Field(default=None, max_length=64)
    location: str | None = Field(default=None, max_length=64)
    status: ChapterStatusLiteral = ChapterStatus.DRAFT


class ChapterUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    content: str | None = None
    summary: str | None = None
    story_time: str | None = Field(default=None, max_length=64)
    location: str | None = Field(default=None, max_length=64)
    status: ChapterStatusLiteral | None = None
    chapter_number: int | None = Field(default=None, ge=1, le=9999)


class ChapterSummaryOut(ORMModel):
    #: 数据库列名是 id，对外契约按需求叫 chapter_id
    chapter_id: str = Field(validation_alias=AliasChoices("chapter_id", "id"))
    novel_id: str
    chapter_number: int
    title: str
    summary: str
    word_count: int
    status: str
    story_time: str | None
    location: str | None
    created_at: datetime
    updated_at: datetime


class ChapterOut(ChapterSummaryOut):
    content: str
    content_path: str


class ChapterSearchHit(BaseModel):
    chapter_id: str
    chapter_number: int
    title: str
    snippet: str
    score: float
    match_source: str


class ChapterSearchResult(BaseModel):
    query: str
    engine: str
    hits: list[ChapterSearchHit]


# --------------------------------------------------------------------------------------
# 人物
# --------------------------------------------------------------------------------------
class CharacterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = ""
    personality: str = ""
    background: str = ""
    goals: list[str] = Field(default_factory=list)
    fears: list[str] = Field(default_factory=list)
    current_location: str = Field(default="", max_length=64)
    current_status: str = Field(default="ACTIVE", max_length=64)
    known_facts: list[str] = Field(default_factory=list)
    unknown_facts: list[str] = Field(default_factory=list)
    first_appearance: int | None = Field(default=None, ge=1)
    last_appearance: int | None = Field(default=None, ge=1)


class CharacterUpdate(BaseModel):
    description: str | None = None
    personality: str | None = None
    background: str | None = None
    goals: list[str] | None = None
    fears: list[str] | None = None
    current_location: str | None = Field(default=None, max_length=64)
    current_status: str | None = Field(default=None, max_length=64)
    known_facts: list[str] | None = None
    unknown_facts: list[str] | None = None
    first_appearance: int | None = Field(default=None, ge=1)
    last_appearance: int | None = Field(default=None, ge=1)


class CharacterStateOut(ORMModel):
    id: str
    character_id: str
    chapter_number: int | None
    status: str
    location: str
    note: str
    source: str
    created_at: datetime


class CharacterStateIn(BaseModel):
    """人物状态变更请求：写入历史并更新当前状态。"""

    status: str | None = Field(default=None, max_length=64)
    location: str | None = Field(default=None, max_length=64)
    note: str = ""
    chapter_number: int | None = Field(default=None, ge=1)
    source: str = "MANUAL"


class CharacterOut(ORMModel):
    id: str
    novel_id: str
    name: str
    description: str
    personality: str
    background: str
    goals: list[str]
    fears: list[str]
    current_location: str
    current_status: str
    known_facts: list[str]
    unknown_facts: list[str]
    first_appearance: int | None
    last_appearance: int | None
    created_at: datetime
    updated_at: datetime


class CharacterStateView(BaseModel):
    """get_character_state 工具的返回结构。"""

    character_id: str
    name: str
    first_appearance: int | None
    last_appearance: int | None
    current_status: str
    current_location: str
    state_at_chapter: CharacterStateOut | None
    history: list[CharacterStateOut]


# --------------------------------------------------------------------------------------
# 关系 / 事件 / Canon / 伏笔 / 时间线 / 世界观
# --------------------------------------------------------------------------------------
class RelationshipCreate(BaseModel):
    character_a: str = Field(min_length=1, max_length=64)
    character_b: str = Field(min_length=1, max_length=64)
    relation: str = Field(default="", max_length=64)
    description: str = ""
    status: str = Field(default="ACTIVE", max_length=32)
    source_chapter: int | None = Field(default=None, ge=1)


class RelationshipOut(ORMModel):
    id: str
    novel_id: str
    character_a_id: str
    character_b_id: str
    relation: str
    description: str
    status: str
    source_chapter: int | None


class RelationshipView(RelationshipOut):
    character_a_name: str
    character_b_name: str


class EventCreate(BaseModel):
    chapter_id: str | None = None
    chapter_number: int | None = Field(default=None, ge=1)
    time: str | None = Field(default=None, max_length=64)
    location: str = Field(default="", max_length=64)
    characters: list[str] = Field(default_factory=list)
    description: str = ""
    consequences: str = ""
    status: CanonStatusLiteral = CanonStatus.CANON


class EventOut(ORMModel):
    id: str
    novel_id: str
    chapter_id: str | None
    chapter_number: int | None
    time: str | None
    location: str
    characters: list[str]
    description: str
    consequences: str
    status: str
    created_at: datetime


class CanonFactCreate(BaseModel):
    """人工新增 Canon 事实。AI 通道禁止直接创建 CANON（见 CanonFactProposal）。"""

    subject: str = Field(min_length=1, max_length=64)
    predicate: str = Field(min_length=1, max_length=64)
    object: str = Field(min_length=1)
    source_chapter: int | None = Field(default=None, ge=1)
    status: CanonStatusLiteral = CanonStatus.PROPOSED
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    visibility: VisibilityLiteral = Visibility.PUBLIC
    known_by: list[str] = Field(default_factory=list)
    note: str = ""


class CanonFactProposal(BaseModel):
    """AI 提出的事实：status 被后端强制为 PROPOSED，模型无法指定 CANON。"""

    subject: str = Field(min_length=1, max_length=64)
    predicate: str = Field(min_length=1, max_length=64)
    object: str = Field(min_length=1)
    source_chapter: int | None = Field(default=None, ge=1)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    visibility: VisibilityLiteral = Visibility.PUBLIC
    known_by: list[str] = Field(default_factory=list)
    note: str = ""


class CanonFactUpdate(BaseModel):
    status: CanonStatusLiteral | None = None
    object: str | None = None
    predicate: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    visibility: VisibilityLiteral | None = None
    known_by: list[str] | None = None
    note: str | None = None


class CanonFactOut(ORMModel):
    id: str
    novel_id: str
    subject: str
    predicate: str
    object: str
    source_chapter: int | None
    valid_from_chapter: int | None
    valid_until_chapter: int | None
    status: str
    confidence: float
    visibility: str
    known_by: list[str]
    origin: str
    note: str
    superseded_by: str | None
    created_at: datetime
    updated_at: datetime


class FactDecision(BaseModel):
    """确认 / 驳回一条 PROPOSED 事实。"""

    note: str = ""
    supersede_previous: bool = True


class ForeshadowingCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    first_chapter: int | None = Field(default=None, ge=1)
    related_characters: list[str] = Field(default_factory=list)
    expected_payoff: str = Field(default="", max_length=200)
    status: ForeshadowStatusLiteral = ForeshadowStatus.OPEN
    last_reinforced_chapter: int | None = Field(default=None, ge=1)


class ForeshadowingUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    description: str | None = None
    expected_payoff: str | None = Field(default=None, max_length=200)
    status: ForeshadowStatusLiteral | None = None
    related_characters: list[str] | None = None
    last_reinforced_chapter: int | None = Field(default=None, ge=1)


class ForeshadowingOut(ORMModel):
    id: str
    novel_id: str
    name: str
    description: str
    first_chapter: int | None
    related_characters: list[str]
    expected_payoff: str
    status: str
    last_reinforced_chapter: int | None
    created_at: datetime
    updated_at: datetime


class TimelineEntryCreate(BaseModel):
    story_time: str = Field(min_length=1, max_length=64)
    chapter_id: str | None = None
    chapter_number: int | None = Field(default=None, ge=1)
    event: str = Field(default="", max_length=200)
    location: str = Field(default="", max_length=64)
    description: str = ""
    status: CanonStatusLiteral = CanonStatus.CANON


class TimelineEntryOut(ORMModel):
    id: str
    novel_id: str
    story_time: str
    story_time_sort: int
    chapter_id: str | None
    chapter_number: int | None
    event: str
    location: str
    description: str
    status: str


class WorldRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    rule_type: str = Field(default="freeform", max_length=48)
    subject: str = Field(default="", max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    enabled: bool = True
    source_chapter: int | None = Field(default=None, ge=1)


class WorldRuleOut(ORMModel):
    id: str
    novel_id: str
    name: str
    rule_type: str
    subject: str
    params: dict[str, Any]
    description: str
    enabled: bool
    source_chapter: int | None


# --------------------------------------------------------------------------------------
# AI：结构化抽取输出（ExtractorAgent）
# --------------------------------------------------------------------------------------
class ExtractedCharacterChange(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    action: str | None = None
    status_change: str | None = None
    location: str | None = None
    notes: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_change_alias(cls, data: Any) -> Any:
        """容忍模型用 change/description 代替 notes。"""
        if isinstance(data, dict) and not data.get("notes"):
            for key in ("change", "description", "summary"):
                if isinstance(data.get(key), str):
                    data = {**data, "notes": data[key]}
                    break
        return data


class ExtractedEvent(BaseModel):
    time: str | None = None
    location: str | None = None
    characters: list[str] = Field(default_factory=list)
    description: str = ""
    consequences: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_name_alias(cls, data: Any) -> Any:
        """容忍模型用 name/detail 描述事件。"""
        if isinstance(data, dict) and not data.get("description"):
            for key in ("name", "detail", "summary"):
                if isinstance(data.get(key), str):
                    data = {**data, "description": data[key]}
                    break
        return data


class ExtractedTimelineEntry(BaseModel):
    event: str = Field(default="", max_length=200)
    story_time: str = Field(min_length=1, max_length=64)
    location: str | None = None
    description: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_detail_alias(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get("description"):
                for key in ("detail", "summary", "note"):
                    if isinstance(data.get(key), str):
                        data = {**data, "description": data[key]}
                        break
            if not data.get("event"):
                for key in ("name", "title", "story_event"):
                    if isinstance(data.get(key), str):
                        data = {**data, "event": data[key]}
                        break
        return data


class ExtractedForeshadowing(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    expected_payoff: str | None = None
    related_characters: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _accept_content_alias(cls, data: Any) -> Any:
        """容忍模型用 {content, basis} 表达伏笔。"""
        if isinstance(data, dict) and not data.get("name"):
            content = data.get("content") or data.get("description") or data.get("summary")
            if isinstance(content, str) and content.strip():
                description = data.get("basis") or data.get("description") or ""
                data = {
                    **data,
                    "name": content.strip()[:40],
                    "description": f"{content}（依据：{description}）" if description else content,
                }
        return data


class ExtractedRelationshipChange(BaseModel):
    character_a: str = Field(min_length=1, max_length=64)
    character_b: str = Field(min_length=1, max_length=64)
    relation: str = Field(default="", max_length=64)
    change: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_subject_object(cls, data: Any) -> Any:
        """容忍模型用 {subject, object, change} 表达关系变化。"""
        if isinstance(data, dict) and not data.get("character_a"):
            left = data.get("subject") or data.get("character_1") or data.get("from")
            right = data.get("object") or data.get("character_2") or data.get("to")
            if isinstance(left, str) and isinstance(right, str):
                data = {**data, "character_a": left, "character_b": right}
        return data


class ExtractedFact(BaseModel):
    subject: str = Field(min_length=1, max_length=64)
    predicate: str = Field(min_length=1, max_length=64)
    object: str = Field(min_length=1)
    source_chapter: int | None = Field(default=None, ge=1)
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    visibility: VisibilityLiteral = Visibility.PUBLIC
    known_by: list[str] = Field(default_factory=list)

    @field_validator("object")
    @classmethod
    def _allow_unknown(cls, value: str) -> str:
        return value.strip() or "UNKNOWN"


class ExtractedCommitment(BaseModel):
    """抽取到的承诺/约定/威胁/悬置问题（用于承诺账本）。"""

    kind: Literal["APPOINTMENT", "DEADLINE", "THREAT", "PROMISE", "QUESTION"] = "APPOINTMENT"
    who: str = Field(default="", max_length=64)
    counterpart: str = Field(default="", max_length=64)
    what: str = Field(min_length=1, max_length=500)
    quote: str = Field(default="", max_length=500)
    deadline_text: str = Field(default="", max_length=64)


class ExtractionResult(BaseModel):
    """ExtractorAgent 的严格输出契约。

    校验仍以本 Schema 为准，但对各模型常见的等价写法（locations 写成
    [{name, detail}]、notes 写成数组等）做一次规范化，避免因为表示差异丢整章结果。
    """

    characters_changed: list[ExtractedCharacterChange] = Field(default_factory=list)
    events: list[ExtractedEvent] = Field(default_factory=list)
    timeline: list[ExtractedTimelineEntry] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    new_facts: list[ExtractedFact] = Field(default_factory=list)
    foreshadowing: list[ExtractedForeshadowing] = Field(default_factory=list)
    relationships_changed: list[ExtractedRelationshipChange] = Field(default_factory=list)
    commitments: list[ExtractedCommitment] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        locations = data.get("locations")
        if isinstance(locations, list):
            data["locations"] = [
                item
                if isinstance(item, str)
                else str(item.get("name") or item.get("location") or item.get("detail") or "").strip()
                for item in locations
                if isinstance(item, (str, dict))
            ]
            data["locations"] = [item for item in data["locations"] if item]
        for key in ("unknown", "notes"):
            value = data.get(key)
            if key == "notes" and isinstance(value, list):
                data["notes"] = "；".join(str(item) for item in value)
            elif key == "unknown" and isinstance(value, str):
                data["unknown"] = [value]
        return data


class ExtractionRunOut(ORMModel):
    id: str
    novel_id: str
    chapter_id: str
    chapter_number: int | None
    status: str
    provider: str
    model: str
    payload: dict[str, Any]
    warnings: list[str]
    created_at: datetime
    applied_at: datetime | None


class ExtractionItemOut(ORMModel):
    id: str
    run_id: str
    kind: str
    payload: dict[str, Any]
    review_status: str
    linked_fact_id: str | None
    applied_ref_id: str | None
    created_at: datetime


class ExtractionRunDetail(ExtractionRunOut):
    items: list[ExtractionItemOut]


class ItemReviewRequest(BaseModel):
    review_status: Literal["ACCEPTED", "REJECTED"] = "ACCEPTED"
    note: str = ""


class ApplyRunRequest(BaseModel):
    """按 kind 过滤要落库的抽取项；默认应用全部 ACCEPTED 项。"""

    kinds: list[str] | None = None
    accept_pending: bool = False


class ApplyRunResult(BaseModel):
    run_id: str
    status: str
    applied: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    message: str


# --------------------------------------------------------------------------------------
# AI：一致性检查（ContinuityChecker）
# --------------------------------------------------------------------------------------
class Evidence(BaseModel):
    """一致性问题必须携带的证据。source_chapter 不可为空。"""

    source_chapter: str = Field(min_length=1)
    ref_type: Literal[
        "CANON_FACT", "EVENT", "TIMELINE", "CHARACTER", "WORLD_RULE", "FORESHADOWING", "CHAPTER"
    ] = "CHAPTER"
    ref_id: str | None = None
    quote: str | None = None
    detail: str | None = None


class ContinuityIssue(BaseModel):
    level: Literal["error", "warning"]
    code: str = Field(min_length=1, max_length=48)
    message: str = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)
    suggested_fix: str | None = None

    @model_validator(mode="after")
    def _require_evidence_chapter(self) -> ContinuityIssue:
        if not self.evidence:
            raise ValueError("一致性问题必须提供证据")
        if any(not (item.source_chapter or "").strip() for item in self.evidence):
            raise ValueError("证据必须指明来源章节")
        return self


class ContinuityReportOut(ORMModel):
    id: str
    novel_id: str
    chapter_id: str
    chapter_number: int | None
    provider: str
    model: str
    errors: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    dropped_issues: list[dict[str, Any]]
    created_at: datetime


class ContinuityReportModel(BaseModel):
    chapter_id: str
    chapter_number: int | None
    errors: list[ContinuityIssue] = Field(default_factory=list)
    warnings: list[ContinuityIssue] = Field(default_factory=list)
    dropped_issues: list[dict[str, Any]] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    report_id: str | None = None
    created_at: datetime | None = None


class ContinuityRequest(BaseModel):
    chapter_id: str
    provider: str | None = None
    include_proposed_facts: bool = True
    narrative_pass: bool = True


# --------------------------------------------------------------------------------------
# AI：记忆检索问答（MemorySearch）
# --------------------------------------------------------------------------------------
class AskRequest(BaseModel):
    question: str = Field(min_length=1)
    provider: str | None = None
    max_evidence: int = Field(default=8, ge=1, le=30)
    persist: bool = True
    #: simple = 后端先检索再由模型组织语言；agent = 模型自己通过 AITool 取数
    mode: Literal["simple", "agent"] = "simple"


class EvidenceItem(BaseModel):
    ref_type: str
    ref_id: str | None = None
    title: str = ""
    chapter_number: int | None = None
    chapter_title: str | None = None
    excerpt: str = ""
    score: float = 0.0


class AskResponse(BaseModel):
    question: str
    answer: str
    confidence: Literal["HIGH", "MEDIUM", "LOW", "UNKNOWN"]
    evidence: list[EvidenceItem] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    warnings: list[str] = Field(default_factory=list)
    mode: str = "simple"
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    mode: str = "simple"
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class RetrievalHitOut(BaseModel):
    ref_type: str
    ref_id: str
    chapter_number: int | None = None
    title: str = ""
    excerpt: str = ""
    keyword_score: float = 0.0
    vector_score: float = 0.0
    term_weight: float = 0.0
    terms_matched: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    score: float = 0.0


class RetrievalResult(BaseModel):
    """混合检索的可视化结果（关键词通道 + 向量通道）。"""

    query: str
    engine: str
    channels: list[str] = Field(default_factory=list)
    hits: list[RetrievalHitOut] = Field(default_factory=list)


class EmbeddingStats(BaseModel):
    records: int = 0
    by_ref_type: dict[str, int] = Field(default_factory=dict)
    providers: list[str] = Field(default_factory=list)
    dim: int = 0


# --------------------------------------------------------------------------------------
# 时点视图
# --------------------------------------------------------------------------------------
class AsOfStateOut(BaseModel):
    """第 N 章时的世界状态快照。"""

    novel_id: str
    chapter_number: int
    canon_facts: list[dict[str, Any]] = Field(default_factory=list)
    characters: list[dict[str, Any]] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    foreshadowings: list[dict[str, Any]] = Field(default_factory=list)
    world_rules: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# 章节规划
# --------------------------------------------------------------------------------------
class PlannedChapter(BaseModel):
    chapter_number: int = Field(ge=1, le=9999)
    title: str = Field(default="", max_length=200)
    goals: str = Field(default="", max_length=600)
    must_include: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    characters: list[str] = Field(default_factory=list)
    advance_foreshadowing: list[str] = Field(default_factory=list)
    rationale: str = ""


class ChapterPlanResult(BaseModel):
    """PlannerAgent 的严格输出契约。"""

    chapters: list[PlannedChapter] = Field(default_factory=list)
    notes: str | None = None


class PlanGenerateRequest(BaseModel):
    from_chapter: int | None = Field(default=None, ge=1, le=9999)
    count: int = Field(default=3, ge=1, le=10)
    steer: str = Field(default="", max_length=500)
    provider: str | None = None
    overwrite: bool = False


class ChapterPlanCreate(BaseModel):
    chapter_number: int = Field(ge=1, le=9999)
    title: str = Field(default="", max_length=200)
    goals: str = ""
    must_include: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    characters: list[str] = Field(default_factory=list)
    advance_foreshadowing: list[str] = Field(default_factory=list)
    rationale: str = ""
    steer: str = ""
    status: str = Field(default="PLANNED", max_length=16)


class ChapterPlanOut(ORMModel):
    id: str
    novel_id: str
    chapter_number: int
    title: str
    goals: str
    must_include: list[str]
    forbidden: list[str]
    characters: list[str]
    advance_foreshadowing: list[str]
    rationale: str
    steer: str
    status: str
    source: str
    provider: str
    model: str
    created_at: datetime
    updated_at: datetime


class PlanGenerateResponse(BaseModel):
    plans: list[ChapterPlanOut] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    warnings: list[str] = Field(default_factory=list)
    context_summary: dict[str, Any] = Field(default_factory=dict)


class ForeshadowingDebtItem(BaseModel):
    id: str
    name: str
    status: str
    description: str = ""
    first_chapter: int | None = None
    last_reinforced_chapter: int | None = None
    related_characters: list[str] = Field(default_factory=list)
    expected_payoff: str = ""
    age: int = 0
    overdue: bool = False
    evidence: str = ""


class ForeshadowingSuggestion(BaseModel):
    foreshadowing_id: str
    name: str
    status: str
    age: int
    overdue: bool
    suggested_chapter: int
    urgency: Literal["HIGH", "MEDIUM", "LOW"]
    related_characters: list[str] = Field(default_factory=list)
    reason: str = ""
    expected_payoff: str = ""


class ForeshadowingPlanOut(BaseModel):
    frontier_chapter: int
    horizon: int
    overdue_count: int
    open_count: int
    suggestions: list[ForeshadowingSuggestion] = Field(default_factory=list)
    debt: list[ForeshadowingDebtItem] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# 全量扫描与总览
# --------------------------------------------------------------------------------------
class SweepRequest(BaseModel):
    #: rules = 只跑确定性规则（不花模型调用）；full = 先逐章抽取再审校
    mode: Literal["rules", "full"] = "rules"
    provider: str | None = None
    narrative_pass: bool = False
    chapter_numbers: list[int] | None = None
    force: bool = False


class ChapterHealth(BaseModel):
    chapter_number: int
    title: str = ""
    word_count: int = 0
    checked_at: datetime | None = None
    errors: int = 0
    warnings: int = 0
    top_codes: list[str] = Field(default_factory=list)


class SweepRunOut(ORMModel):
    id: str
    novel_id: str
    mode: str
    provider: str
    model: str
    chapters_total: int
    chapters_checked: int
    errors: int
    warnings: int
    detail: dict[str, Any]
    started_at: datetime
    finished_at: datetime | None


class DashboardOut(BaseModel):
    novel_id: str
    word_count: int
    chapter_count: int
    target_word_count: int
    chapters: list[ChapterHealth] = Field(default_factory=list)
    error_totals: dict[str, int] = Field(default_factory=dict)
    error_chapters: list[int] = Field(default_factory=list)
    unchecked_chapters: list[int] = Field(default_factory=list)
    foreshadowing_debt: list[ForeshadowingDebtItem] = Field(default_factory=list)
    overdue_foreshadowing: int = 0
    proposed_backlog: int = 0
    pending_review_items: int = 0
    vector_index: EmbeddingStats = Field(default_factory=EmbeddingStats)
    latest_sweep: SweepRunOut | None = None
    #: V0.3：全局不变量、承诺账本、文风
    invariant_errors: int = 0
    invariant_warnings: int = 0
    invariant_codes: dict[str, int] = Field(default_factory=dict)
    invariant_issues: list[dict[str, Any]] = Field(default_factory=list)
    commitments_open: int = 0
    commitments_overdue: int = 0
    commitments_overdue_items: list[dict[str, Any]] = Field(default_factory=list)
    style_baseline: str = ""
    style_review_average: float | None = None
    #: V0.4：作者声音画像与想法碎片
    voice_profile: str = ""
    voice_terms: list[str] = Field(default_factory=list)
    fragments_total: int = 0
    fragments_unplaced: int = 0
    fragments_realized: int = 0
    fragment_realization_rate: float = 0.0
    fragment_kinds: dict[str, int] = Field(default_factory=dict)
    style_locked: bool = False
    style_drift_chapters: int = 0
    style_baseline_stale: bool = False
    style_baseline_missing: list[str] = Field(default_factory=list)
    claim_reports: int = 0
    claim_conflicts: int = 0
    claim_unverified: int = 0
    latest_claim_conflicts: list[list[dict]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# AI：章节写作（ChapterWriter）
# --------------------------------------------------------------------------------------
class WriteChapterRequest(BaseModel):
    goals: str = Field(min_length=1, description="本章目标")
    must_include: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    characters: list[str] = Field(default_factory=list)
    chapter_number: int | None = Field(default=None, ge=1, le=9999)
    title: str | None = Field(default=None, max_length=200)
    story_time: str | None = Field(default=None, max_length=64)
    location: str | None = Field(default=None, max_length=64)
    target_words: int = Field(default=1200, ge=300, le=8000)
    provider: str | None = None
    save: bool = False


class RetrievalBundle(BaseModel):
    """写作前检索到的 Canon 上下文快照，用于审计“写作前是否读了前文”。"""

    canon_facts: list[dict[str, Any]] = Field(default_factory=list)
    characters: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    foreshadowing: list[dict[str, Any]] = Field(default_factory=list)
    world_rules: list[dict[str, Any]] = Field(default_factory=list)
    recent_chapter_summaries: list[dict[str, Any]] = Field(default_factory=list)
    #: 语义检索到的前文片段（用词不同但内容相关）
    semantic_hits: list[dict[str, Any]] = Field(default_factory=list)
    #: 作者自己的想法碎片（成文时优先体现）
    fragments: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ChapterDraft(BaseModel):
    title: str
    content: str
    word_count: int
    chapter_number: int | None = None
    story_time: str | None = None
    location: str | None = None


class WriteChapterResponse(BaseModel):
    draft: ChapterDraft
    retrieved: RetrievalBundle
    provider: str
    model: str
    warnings: list[str] = Field(default_factory=list)
    saved_chapter_id: str | None = None
    generation_id: str | None = None


# --------------------------------------------------------------------------------------
# 章节完成工作流
# --------------------------------------------------------------------------------------
class WorkflowStep(BaseModel):
    step: int
    name: str
    status: Literal["ok", "warning", "error", "skipped"]
    detail: str = ""


class ChapterCompletionReport(BaseModel):
    chapter_id: str
    chapter_number: int | None
    steps: list[WorkflowStep]
    extraction_run: ExtractionRunDetail | None = None
    continuity: ContinuityReportModel | None = None
    claims: "ClaimReportOut | None" = None
    pending_items: int = 0
    proposed_facts: int = 0
    errors: int = 0
    warnings: int = 0
    notes: list[str] = Field(default_factory=list)
    message: str = ""


# --------------------------------------------------------------------------------------
# 工具与提供者元信息
# --------------------------------------------------------------------------------------
class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class ProviderInfo(BaseModel):
    name: str
    model: str
    kind: str
    available: bool
    supports_tools: bool = False
    detail: str = ""


class HealthOut(BaseModel):
    status: str
    version: str
    database: str
    default_provider: str
    providers: list[ProviderInfo]


__all__ = [
    "AskRequest",
    "AskResponse",
    "ApplyRunRequest",
    "ApplyRunResult",
    "AsOfStateOut",
    "CanonFactCreate",
    "CanonFactOut",
    "CanonFactProposal",
    "CanonFactUpdate",
    "ChapterCreate",
    "ChapterCompletionReport",
    "ChapterDraft",
    "ChapterHealth",
    "ChapterOut",
    "ChapterPlanCreate",
    "ChapterPlanOut",
    "ChapterPlanResult",
    "ChapterSearchHit",
    "ChapterSearchResult",
    "ChapterSummaryOut",
    "ChapterUpdate",
    "CharacterCreate",
    "CharacterOut",
    "CharacterStateIn",
    "CharacterStateOut",
    "CharacterStateView",
    "CharacterUpdate",
    "ContinuityIssue",
    "ContinuityReportModel",
    "ContinuityReportOut",
    "ContinuityRequest",
    "DashboardOut",
    "EmbeddingStats",
    "EventCreate",
    "EventOut",
    "Evidence",
    "EvidenceItem",
    "ExtractedFact",
    "ExtractionItemOut",
    "ExtractionResult",
    "ExtractionRunDetail",
    "ExtractionRunOut",
    "FactDecision",
    "ForeshadowingCreate",
    "ForeshadowingDebtItem",
    "ForeshadowingOut",
    "ForeshadowingPlanOut",
    "ForeshadowingSuggestion",
    "ForeshadowingUpdate",
    "HealthOut",
    "ItemReviewRequest",
    "NovelCreate",
    "NovelOut",
    "NovelStats",
    "NovelUpdate",
    "PlanGenerateRequest",
    "PlanGenerateResponse",
    "PlannedChapter",
    "ProviderInfo",
    "RelationshipCreate",
    "RelationshipOut",
    "RelationshipView",
    "RetrievalBundle",
    "RetrievalHitOut",
    "RetrievalResult",
    "SweepRequest",
    "SweepRunOut",
    "TimelineEntryCreate",
    "TimelineEntryOut",
    "ToolSpec",
    "WorkflowStep",
    "WorldRuleCreate",
    "WorldRuleOut",
    "WriteChapterRequest",
    "WriteChapterResponse",
]


# --------------------------------------------------------------------------------------
# V0.3 质量层：文风 / 不变量 / 承诺 / 修订闭环
# --------------------------------------------------------------------------------------
class StyleIssueOut(BaseModel):
    code: str
    level: Literal["warning", "info"]
    metric: str = ""
    message: str = ""
    value: float = 0.0
    reference: float | None = None
    excerpt: str = ""
    suggestion: str = ""


class StyleReviewOut(BaseModel):
    chapter_id: str | None = None
    chapter_number: int | None = None
    score: float
    metrics: dict[str, Any] = Field(default_factory=dict)
    issues: list[StyleIssueOut] = Field(default_factory=list)
    baseline: str = ""
    baseline_metrics: dict[str, Any] = Field(default_factory=dict)
    baseline_metrics_version: str = ""
    baseline_missing_metrics: list[str] = Field(default_factory=list)
    baseline_stale: bool = False
    locked: bool = False
    locked_drift: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    model_summary: str = ""
    provider: str = ""
    model: str = ""
    review_id: str | None = None


class StyleProfileOut(ORMModel):
    id: str
    novel_id: str
    name: str
    source: str
    sample_count: int
    total_chars: int
    metrics: dict[str, Any]
    samples: list[Any]
    is_default: bool
    locked: bool = False
    created_at: datetime


class StyleProfileRequest(BaseModel):
    name: str = Field(default="本书基线", max_length=120)
    chapter_numbers: list[int] | None = None
    texts: list[str] = Field(default_factory=list)
    make_default: bool = True


class StyleTextRequest(BaseModel):
    text: str = Field(min_length=1)
    use_model: bool = True
    chapter_number: int | None = None


class InvariantEvidence(BaseModel):
    source_chapter: str = ""
    quote: str | None = None
    detail: str | None = None


class InvariantIssueOut(BaseModel):
    code: str
    level: Literal["error", "warning", "info"]
    subject: str = ""
    message: str
    evidence: list[InvariantEvidence] = Field(default_factory=list)
    suggestion: str = ""


class InvariantReportOut(BaseModel):
    novel_id: str
    report_id: str | None = None
    created_at: str | None = None
    errors: int = 0
    warnings: int = 0
    codes: dict[str, int] = Field(default_factory=dict)
    checked: dict[str, int] = Field(default_factory=dict)
    issues: list[InvariantIssueOut] = Field(default_factory=list)


class CommitmentCreate(BaseModel):
    source_chapter: int | None = Field(default=None, ge=1)
    kind: Literal["APPOINTMENT", "DEADLINE", "THREAT", "PROMISE", "QUESTION"] = "APPOINTMENT"
    who: str = Field(default="", max_length=64)
    counterpart: str = Field(default="", max_length=64)
    what: str = Field(min_length=1, max_length=500)
    quote: str = Field(default="", max_length=500)
    deadline_text: str = Field(default="", max_length=64)
    note: str = ""


class CommitmentDecision(BaseModel):
    note: str = ""
    chapter_number: int | None = Field(default=None, ge=1)


class CommitmentOut(BaseModel):
    id: str
    kind: str
    who: str = ""
    counterpart: str = ""
    what: str = ""
    quote: str = ""
    source_chapter: int | None = None
    deadline_text: str = ""
    due_story_time: str | None = None
    status: str
    stored_status: str = ""
    fulfilled_chapter: int | None = None
    days_remaining: int | None = None
    breach_chapter: int | None = None
    frontier_chapter: int = 0
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class RevisionRoundOut(BaseModel):
    round: int
    stage: str
    score: float
    word_count: int
    style_codes: list[str] = Field(default_factory=list)
    continuity_codes: list[str] = Field(default_factory=list)


class RevisionRequest(BaseModel):
    goals: str = Field(min_length=1)
    must_include: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    characters: list[str] = Field(default_factory=list)
    chapter_number: int | None = Field(default=None, ge=1, le=9999)
    title: str | None = Field(default=None, max_length=200)
    story_time: str | None = Field(default=None, max_length=64)
    location: str | None = Field(default=None, max_length=64)
    target_words: int = Field(default=1500, ge=300, le=8000)
    provider: str | None = None
    save: bool = False
    plan_id: str | None = None
    max_rounds: int = Field(default=2, ge=0, le=4)
    target_score: float = Field(default=85.0, ge=40.0, le=100.0)
    use_model_critic: bool = True


class RevisionResponse(BaseModel):
    draft: ChapterDraft
    rounds: list[RevisionRoundOut] = Field(default_factory=list)
    accepted: bool = False
    final_score: float = 0.0
    metric_deltas: dict[str, Any] = Field(default_factory=dict)
    retrieved: dict[str, Any] = Field(default_factory=dict)
    provider: str = ""
    model: str = ""
    warnings: list[str] = Field(default_factory=list)
    saved_chapter_id: str | None = None
    generation_id: str | None = None
    goal: str = ""
    plan_id: str | None = None


# --------------------------------------------------------------------------------------
# V0.4 想法碎片与「碎片成文」
# --------------------------------------------------------------------------------------
FragmentKindLiteral = Literal[
    "WHIM", "IMAGE", "LINE", "SCENE", "THEME", "CHARACTER", "MECHANIC", "OTHER"
]
FragmentStatusLiteral = Literal["INBOX", "PLACED", "REALIZED", "ARCHIVED"]


class FragmentCreate(BaseModel):
    kind: FragmentKindLiteral = "WHIM"
    title: str = Field(default="", max_length=200)
    text: str = Field(min_length=1)
    intent: str = ""
    tags: list[str] = Field(default_factory=list)
    related_characters: list[str] = Field(default_factory=list)
    target_chapter: int | None = Field(default=None, ge=1, le=9999)
    status: FragmentStatusLiteral = "INBOX"
    priority: int = Field(default=3, ge=1, le=5)
    origin: str = Field(default="USER", max_length=16)
    prompted_by: str = ""
    notes: str = ""


class FragmentUpdate(BaseModel):
    kind: FragmentKindLiteral | None = None
    title: str | None = Field(default=None, max_length=200)
    text: str | None = Field(default=None, min_length=1)
    intent: str | None = None
    tags: list[str] | None = None
    related_characters: list[str] | None = None
    target_chapter: int | None = Field(default=None, ge=1, le=9999)
    status: FragmentStatusLiteral | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    notes: str | None = None


class FragmentOut(BaseModel):
    id: str
    novel_id: str
    kind: str
    title: str
    text: str
    intent: str
    tags: list[str] = Field(default_factory=list)
    related_characters: list[str] = Field(default_factory=list)
    target_chapter: int | None = None
    status: str
    priority: int
    origin: str
    prompted_by: str = ""
    realized_chapter_id: str | None = None
    realized_excerpt: str = ""
    realized_treatment: str = ""
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""


class FragmentPlaceRequest(BaseModel):
    chapter_number: int = Field(ge=1, le=9999)


class FragmentPassage(BaseModel):
    """碎片 → 正文的对应关系（成文后由作者核对）。"""

    fragment_id: str
    prose: str = Field(min_length=1)
    uses_quote: str = ""
    treatment: Literal["QUOTED", "PARAPHRASED", "EXPANDED", "BACKGROUND"] = "PARAPHRASED"


class FragmentRealization(BaseModel):
    title: str = ""
    content: str = ""
    word_count: int = 0
    passages: list[FragmentPassage] = Field(default_factory=list)
    undeveloped: list[dict[str, Any]] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)
    invented_claims: list[dict[str, Any]] = Field(default_factory=list)
    notes: str = ""
    saved_chapter_id: str | None = None
    chapter_number: int | None = None


class FragmentRealizeRequest(BaseModel):
    fragment_ids: list[str] = Field(default_factory=list)
    raw_fragments: list[str] = Field(default_factory=list)
    goals: str = ""
    tone: str = Field(default="", max_length=200)
    must_keep: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    characters: list[str] = Field(default_factory=list)
    chapter_number: int | None = Field(default=None, ge=1, le=9999)
    target_words: int = Field(default=800, ge=100, le=8000)
    provider: str | None = None
    save: bool = False
    #: 成文后再过一遍「写作→评审→改稿」闭环
    revise: bool = False
    max_rounds: int = Field(default=1, ge=0, le=3)
    target_score: float = Field(default=85.0, ge=40.0, le=100.0)
    use_model_critic: bool = True


class FragmentRealizeResponse(BaseModel):
    realization: FragmentRealization
    provider: str = ""
    model: str = ""
    warnings: list[str] = Field(default_factory=list)
    style: dict[str, Any] = Field(default_factory=dict)
    voice: dict[str, Any] = Field(default_factory=dict)
    fragments_updated: int = 0


class FragmentStats(BaseModel):
    total: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    by_kind: dict[str, int] = Field(default_factory=dict)
    inbox: int = 0
    unplaced: int = 0
    realized: int = 0
    realization_rate: float = 0.0


class EmotionPromptRequest(BaseModel):
    goals: str = ""
    characters: list[str] = Field(default_factory=list)
    chapter_number: int | None = Field(default=None, ge=1, le=9999)
    provider: str | None = None


class EmotionPromptResponse(BaseModel):
    questions: list[str] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    warnings: list[str] = Field(default_factory=list)


class VoiceProfileOut(BaseModel):
    available: bool = False
    name: str = ""
    sample_count: int = 0
    total_chars: int = 0
    signature_terms: list[str] = Field(default_factory=list)
    punctuation: dict[str, float] = Field(default_factory=dict)
    created_at: datetime | None = None



# --------------------------------------------------------------------------- V0.5 声称核对
class ClaimVerifyRequest(BaseModel):
    """核对一段正文里的设定断言。text 与 chapter_number 至少给一个。"""

    text: str = ""
    chapter_number: int | None = Field(default=None, ge=1, le=9999)
    as_of_chapter: int | None = Field(default=None, ge=1, le=9999)
    use_model: bool = True
    persist: bool = True
    provider: str | None = None


class ClaimEvidenceOut(BaseModel):
    kind: str = ""
    text: str = ""
    source_chapter: int | None = None
    fact_id: str | None = None
    rule_type: str = ""


class ClaimVerdictOut(BaseModel):
    claim: dict = Field(default_factory=dict)
    verdict: str = ""
    reason: str = ""
    evidence: list[ClaimEvidenceOut] = Field(default_factory=list)


class ClaimReportOut(BaseModel):
    id: str | None = None
    novel_id: str = ""
    chapter_id: str | None = None
    chapter_number: int | None = None
    label: str = ""
    as_of_chapter: int | None = None
    claim_count: int = 0
    supported_count: int = 0
    unverified_count: int = 0
    conflict_count: int = 0
    claims: list[ClaimVerdictOut] = Field(default_factory=list)
    conflicts: list[ClaimVerdictOut] = Field(default_factory=list)
    unverified: list[ClaimVerdictOut] = Field(default_factory=list)
    supported: list[ClaimVerdictOut] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    warnings: list[str] = Field(default_factory=list)
    summary: str = ""
    created_at: datetime | None = None


class StyleLockRequest(BaseModel):
    locked: bool = True


class StyleDriftItem(BaseModel):
    chapter_number: int = 0
    title: str = ""
    score: float = 0.0
    drifted: list[dict] = Field(default_factory=list)


class StyleDriftReport(BaseModel):
    locked: bool = False
    profile_id: str | None = None
    profile_name: str = ""
    direction: str = ""
    metrics_version: str = ""
    missing_metrics: list[str] = Field(default_factory=list)
    stale: bool = False
    chapters: list[StyleDriftItem] = Field(default_factory=list)
    drifted_count: int = 0



# ClaimReportOut 定义在本文件末尾，这里显式解析前面的前向引用
ChapterCompletionReport.model_rebuild()
