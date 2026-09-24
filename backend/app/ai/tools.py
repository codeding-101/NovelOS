"""AITool 集合：Agent 访问数据库的唯一通道。

- 每个工具都有结构化参数 Schema（Pydantic），并通过 GET /api/ai/tools 对外暴露，
  便于未来切换成模型原生 function calling。
- 工具只读为主；唯一的写工具 propose_canon_fact 被强制为 PROPOSED，
  且写库动作由后端完成 —— 模型永远拿不到 SQL。
- 查询不到结果时返回 status="UNKNOWN"，而不是编造（安全原则 3）。
"""

from __future__ import annotations

from typing import Any, Literal
from collections.abc import Callable

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.models import Novel
from app.services import query_service, search_service


class SearchChaptersParams(BaseModel):
    query: str = Field(description="检索词，支持中文子串")
    limit: int = Field(default=5, ge=1, le=20)


class GetCharacterParams(BaseModel):
    name: str = Field(description="人物姓名")


class GetCharacterStateParams(BaseModel):
    name: str = Field(description="人物姓名")
    chapter_number: int | None = Field(default=None, description="查看该章时的人物状态")


class GetRelationshipParams(BaseModel):
    character_a: str = Field(description="人物甲")
    character_b: str | None = Field(default=None, description="人物乙，留空则返回甲的全部关系")


class GetEventsParams(BaseModel):
    chapter_number: int | None = Field(default=None, description="限定章节")
    character: str | None = Field(default=None, description="限定参与人物")
    limit: int = Field(default=20, ge=1, le=100)


class GetTimelineParams(BaseModel):
    limit: int = Field(default=50, ge=1, le=200)


class GetCanonFactsParams(BaseModel):
    subject: str | None = Field(default=None, description="事实主语")
    predicate: str | None = Field(default=None, description="事实谓语")
    status: Literal["CANON", "PROPOSED"] = Field(
        default="CANON", description="默认只取已确认的 CANON"
    )
    limit: int = Field(default=50, ge=1, le=200)


class GetForeshadowingParams(BaseModel):
    status: str | None = Field(default=None, description="OPEN/DEVELOPING/RESOLVED/ABANDONED")
    character: str | None = Field(default=None, description="相关人物")


class GetWorldRulesParams(BaseModel):
    pass


class ProposeCanonFactParams(BaseModel):
    subject: str = Field(description="事实主语")
    predicate: str = Field(description="事实谓语，如 佩剑/修为/身份/状态")
    object: str = Field(description="事实宾语，无法确认时填 UNKNOWN")
    source_chapter: int | None = Field(default=None, description="来源章号")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    visibility: Literal["PUBLIC", "SECRET"] = "PUBLIC"
    known_by: list[str] = Field(default_factory=list, description="已知晓该事实的人物")


class ToolDefinition(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


def _wrap(name: str, status: str, data: Any) -> dict[str, Any]:
    return {"tool": name, "status": status, "data": data}


class AIToolKit:
    """绑定到具体小说与数据库会话的工具箱。"""

    def __init__(self, session: Session, novel: Novel) -> None:
        self.session = session
        self.novel = novel

    # ------------------------------------------------------------------ 工具
    def search_chapters(self, params: SearchChaptersParams) -> dict[str, Any]:
        result = search_service.search_chapters(
            self.session, self.novel.id, params.query, limit=params.limit
        )
        status = "OK" if result["hits"] else "UNKNOWN"
        return _wrap("search_chapters", status, result)

    def get_character(self, params: GetCharacterParams) -> dict[str, Any]:
        character = query_service.get_character(self.session, self.novel.id, params.name)
        if character is None:
            return _wrap("get_character", "UNKNOWN", {"name": params.name, "note": "人物库中没有该人物"})
        return _wrap("get_character", "OK", query_service.character_to_dict(character))

    def get_character_state(self, params: GetCharacterStateParams) -> dict[str, Any]:
        state = query_service.get_character_state(
            self.session, self.novel.id, params.name, params.chapter_number
        )
        return _wrap("get_character_state", "OK" if state.get("found") else "UNKNOWN", state)

    def get_relationship(self, params: GetRelationshipParams) -> dict[str, Any]:
        data = query_service.list_relationships(
            self.session, self.novel.id, params.character_a, params.character_b
        )
        return _wrap("get_relationship", "OK" if data else "UNKNOWN", data)

    def get_events(self, params: GetEventsParams) -> dict[str, Any]:
        data = query_service.list_events(
            self.session,
            self.novel.id,
            chapter_number=params.chapter_number,
            character=params.character,
            limit=params.limit,
        )
        return _wrap("get_events", "OK" if data else "UNKNOWN", data)

    def get_timeline(self, params: GetTimelineParams) -> dict[str, Any]:
        data = query_service.list_timeline(self.session, self.novel.id, limit=params.limit)
        return _wrap("get_timeline", "OK" if data else "UNKNOWN", data)

    def get_canon_facts(self, params: GetCanonFactsParams) -> dict[str, Any]:
        data = query_service.list_canon_facts(
            self.session,
            self.novel.id,
            subject=params.subject,
            predicate=params.predicate,
            status=params.status,
            limit=params.limit,
        )
        return _wrap("get_canon_facts", "OK" if data else "UNKNOWN", data)

    def get_foreshadowing(self, params: GetForeshadowingParams) -> dict[str, Any]:
        data = query_service.list_foreshadowing(
            self.session, self.novel.id, status=params.status, character=params.character
        )
        return _wrap("get_foreshadowing", "OK" if data else "UNKNOWN", data)

    def get_world_rules(self, params: GetWorldRulesParams) -> dict[str, Any]:
        data = query_service.list_world_rules(self.session, self.novel.id)
        return _wrap("get_world_rules", "OK" if data else "UNKNOWN", data)

    def propose_canon_fact(self, params: ProposeCanonFactParams) -> dict[str, Any]:
        fact = query_service.propose_canon_fact(
            self.session,
            self.novel.id,
            subject=params.subject,
            predicate=params.predicate,
            object_value=params.object,
            source_chapter=params.source_chapter,
            confidence=params.confidence,
            visibility=params.visibility,
            known_by=params.known_by,
            origin="AI_TOOL",
        )
        return _wrap(
            "propose_canon_fact",
            "OK",
            {
                **query_service.canon_fact_to_dict(fact),
                "note": "已按安全原则写入 PROPOSED，需作者确认后才会成为 CANON",
            },
        )

    # ------------------------------------------------------------------ 注册表
    @staticmethod
    def definitions() -> list[ToolDefinition]:
        raw: list[tuple[str, str, type[BaseModel], Callable[..., dict[str, Any]]]] = [
            ("search_chapters", "按关键词全文检索章节，返回命中片段与章号", SearchChaptersParams, AIToolKit.search_chapters),
            ("get_character", "读取人物档案", GetCharacterParams, AIToolKit.get_character),
            ("get_character_state", "读取人物当前状态（可指定章号查看当时状态）", GetCharacterStateParams, AIToolKit.get_character_state),
            ("get_relationship", "读取人物关系", GetRelationshipParams, AIToolKit.get_relationship),
            ("get_events", "读取事件记录", GetEventsParams, AIToolKit.get_events),
            ("get_timeline", "按故事时间读取时间线", GetTimelineParams, AIToolKit.get_timeline),
            ("get_canon_facts", "读取 Canon 事实（默认仅 CANON）", GetCanonFactsParams, AIToolKit.get_canon_facts),
            ("get_foreshadowing", "读取伏笔清单", GetForeshadowingParams, AIToolKit.get_foreshadowing),
            ("get_world_rules", "读取世界观规则", GetWorldRulesParams, AIToolKit.get_world_rules),
            ("propose_canon_fact", "提出一条新事实（只能落为 PROPOSED）", ProposeCanonFactParams, AIToolKit.propose_canon_fact),
        ]
        return [
            ToolDefinition(
                name=name,
                description=description,
                parameters=params_model.model_json_schema(),
            )
            for name, description, params_model, _ in raw
        ]

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """按名字调用工具，参数先过 Schema 校验（模型输出不能绕过校验）。"""
        table: dict[str, tuple[type[BaseModel], Callable[..., dict[str, Any]]]] = {
            "search_chapters": (SearchChaptersParams, self.search_chapters),
            "get_character": (GetCharacterParams, self.get_character),
            "get_character_state": (GetCharacterStateParams, self.get_character_state),
            "get_relationship": (GetRelationshipParams, self.get_relationship),
            "get_events": (GetEventsParams, self.get_events),
            "get_timeline": (GetTimelineParams, self.get_timeline),
            "get_canon_facts": (GetCanonFactsParams, self.get_canon_facts),
            "get_foreshadowing": (GetForeshadowingParams, self.get_foreshadowing),
            "get_world_rules": (GetWorldRulesParams, self.get_world_rules),
            "propose_canon_fact": (ProposeCanonFactParams, self.propose_canon_fact),
        }
        entry = table.get(name)
        if entry is None:
            return _wrap(name, "ERROR", {"message": f"未知工具 {name}"})
        params_model, func = entry
        try:
            params = params_model.model_validate(arguments or {})
        except ValidationError as exc:
            return _wrap(name, "ERROR", {"message": "参数未通过 Schema 校验", "errors": exc.errors(include_url=False)})
        return func(params)
