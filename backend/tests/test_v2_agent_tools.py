"""V0.2 工具调用（agent 模式）、多模型注册表与迁移。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.ai.agents.tool_loop import ToolCallingAgent, tool_schemas
from app.ai.base import AIError, AIResponse, AIToolUnsupported
from app.ai.factory import build_provider, provider_names, resolve_provider
from app.ai.tools import AIToolKit
from app.migrations import ADDITIVE_COLUMNS, SCHEMA_VERSION, migrate
from app.models import CanonFact, Meta
from app.services import extraction_service


# --------------------------------------------------------------------------- 工具调用
def test_tool_schemas_are_openai_function_shape(session, novel):
    kit = AIToolKit(session, novel)
    schemas = tool_schemas(kit)
    assert len(schemas) == 10
    for item in schemas:
        assert item["type"] == "function"
        function = item["function"]
        assert function["name"] and function["description"]
        assert function["parameters"]["type"] == "object"
    assert any(item["function"]["name"] == "propose_canon_fact" for item in schemas)


def test_tool_loop_with_offline_provider(session, novel):
    memory = extraction_service.build_agents("offline")["memory"]
    response = memory.ask_agent(session, novel, "林默的佩剑是什么？")
    session.commit()
    assert response.mode == "agent"
    assert response.tool_calls, "agent 模式应实际调用工具"
    assert {call["name"] for call in response.tool_calls} <= {
        "search_chapters",
        "get_canon_facts",
        "get_character",
        "get_character_state",
        "get_events",
        "get_timeline",
        "get_foreshadowing",
        "get_world_rules",
        "get_relationship",
        "propose_canon_fact",
    }
    assert response.answer
    assert "第" in response.answer, "回答应带出处"
    assert response.confidence in ("HIGH", "MEDIUM", "LOW")


def test_ask_agent_endpoint_records_tool_calls(client):
    novel = client.post("/api/novels", json={"title": "agent 模式"}).json()
    client.post(f"/api/novels/{novel['id']}/seed")
    response = client.post(
        f"/api/novels/{novel['id']}/ai/ask",
        json={"question": "林默的师父是谁？", "mode": "agent"},
    ).json()
    assert response["mode"] == "agent"
    assert response["tool_calls"]
    assert response["answer"]

    simple = client.post(
        f"/api/novels/{novel['id']}/ai/ask",
        json={"question": "林默的师父是谁？", "mode": "simple"},
    ).json()
    assert simple["mode"] == "simple"
    assert "赵铁山" in simple["answer"] or "师承" in simple["answer"]


class _LyingToolProvider:
    """假装支持工具调用，但最终答案引用了不存在的章节。"""

    name = "stub"
    model = "stub-tools"
    kind = "llm"
    supports_tools = True

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.rounds = 0

    def generate(self, request):  # noqa: ANN001
        return AIResponse(text=self.answer, parsed={"answer": self.answer}, provider=self.name, model=self.model)

    def generate_with_tools(self, request, tools):  # noqa: ANN001
        self.rounds += 1
        if self.rounds == 1:
            return AIResponse(
                text="",
                provider=self.name,
                model=self.model,
                tool_calls=[
                    {
                        "id": "call_1",
                        "name": "get_canon_facts",
                        "arguments": {"subject": "林默", "limit": 5},
                    }
                ],
            )
        return AIResponse(text=self.answer, provider=self.name, model=self.model)


def test_agent_citation_guard_rejects_invented_chapters(session, novel):
    from app.ai.agents.memory_search import MemorySearch

    provider = _LyingToolProvider("林默的佩剑在第 88 章被换成了别的东西。")
    memory = MemorySearch(provider)  # type: ignore[arg-type]
    response = memory.ask_agent(session, novel, "林默的佩剑是什么？")
    assert "第88章" not in response.answer
    assert any("没有的章节" in warning for warning in response.warnings)


def test_tool_loop_degrades_when_provider_lacks_tools(session, novel):
    class _NoTools:
        name = "no-tools"
        model = "no-tools"
        kind = "llm"
        supports_tools = False

        def generate(self, request):  # noqa: ANN001
            raise AIToolUnsupported("no tools")

        def generate_with_tools(self, request, tools):  # noqa: ANN001
            raise AIToolUnsupported("no tools")

    kit = AIToolKit(session, novel)
    agent = ToolCallingAgent(_NoTools(), kit)  # type: ignore[arg-type]
    result = agent.run(system="test", question="随便问问")
    assert result.answer == ""
    assert result.warnings and "不支持工具调用" in result.warnings[0]
    assert result.tool_calls == []


def test_tool_loop_stops_at_max_rounds(session, novel):
    class _AlwaysTools:
        name = "loop"
        model = "loop"
        kind = "llm"
        supports_tools = True

        def __init__(self):
            self.calls = 0

        def generate(self, request):  # noqa: ANN001
            return AIResponse(text="", provider=self.name, model=self.model)

        def generate_with_tools(self, request, tools):  # noqa: ANN001
            self.calls += 1
            return AIResponse(
                text="",
                provider=self.name,
                model=self.model,
                tool_calls=[
                    {"id": f"c{self.calls}", "name": "get_world_rules", "arguments": {}}
                ],
            )

    provider = _AlwaysTools()
    agent = ToolCallingAgent(provider, AIToolKit(session, novel), max_rounds=3)
    result = agent.run(system="test", question="循环测试")
    assert provider.calls == 3
    assert any("超过 3 轮" in warning for warning in result.warnings)
    assert len(result.tool_calls) == 3


def test_agent_mode_never_writes_canon(session, novel):
    before = len(list(session.scalars(select(CanonFact).where(CanonFact.novel_id == novel.id))))
    memory = extraction_service.build_agents("offline")["memory"]
    memory.ask_agent(session, novel, "林默的父亲是谁？")
    session.commit()
    after = list(session.scalars(select(CanonFact).where(CanonFact.novel_id == novel.id)))
    assert len(after) == before, "问答（含 agent 模式）不得产生新的设定"


# --------------------------------------------------------------------------- 多模型
def test_provider_names_include_builtins():
    names = provider_names()
    assert "deepseek" in names and "offline" in names


def test_unknown_provider_raises():
    with pytest.raises(AIError):
        build_provider("no-such-provider")
    with pytest.raises(AIError):
        resolve_provider("no-such-provider")


def test_external_provider_registry(monkeypatch):
    class _StubSettings:
        ai_provider = "grok"
        deepseek_api_key = ""
        deepseek_base_url = "https://api.deepseek.com"
        deepseek_model = "deepseek-flash"
        request_timeout_seconds = 10
        max_retries = 1

        @staticmethod
        def external_providers():
            return [
                {
                    "name": "grok",
                    "base_url": "https://api.x.ai/v1",
                    "model": "grok-4",
                    "api_key": "test-key",
                    "label": "Grok",
                }
            ]

        @staticmethod
        def external_provider(name):
            return next(
                (item for item in _StubSettings.external_providers() if item["name"] == name), None
            )

        def deepseek_available(self):
            return False

    import app.ai.factory as factory

    monkeypatch.setattr(factory, "settings", _StubSettings())
    assert "grok" in provider_names()
    provider = build_provider("grok")
    assert provider.name == "grok" and provider.model == "grok-4"
    assert provider.supports_tools is True
    assert provider.describe()["available"] is True

    resolved, warnings = resolve_provider("grok")
    assert resolved.name == "grok" and warnings == []

    fallback, fallback_warnings = resolve_provider("deepseek")
    assert fallback.name == "offline" and fallback_warnings


# --------------------------------------------------------------------------- 迁移
def test_migration_records_schema_version(session):
    row = session.scalar(select(Meta).where(Meta.key == "schema_version"))
    assert row is not None and row.value == SCHEMA_VERSION


def test_migration_adds_missing_columns(tmp_path):
    from app.database import Base, build_engine

    engine = build_engine(tmp_path / "migrate.db")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX IF EXISTS ix_canon_facts_valid_from_chapter")
        connection.exec_driver_sql("ALTER TABLE canon_facts DROP COLUMN valid_from_chapter")
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(canon_facts)")}
    assert "valid_from_chapter" not in columns

    applied = migrate(engine)
    assert any("add column canon_facts.valid_from_chapter" in item for item in applied)
    assert any("add index ix_canon_facts_valid_from_chapter" in item for item in applied)
    with engine.begin() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(canon_facts)")}
        indexes = {
            row[1] for row in connection.exec_driver_sql("PRAGMA index_list(canon_facts)")
        }
    assert "valid_from_chapter" in columns
    assert "ix_canon_facts_valid_from_chapter" in indexes, "补列时应把索引一起补上"
    assert migrate(engine) == [], "重复迁移不应再产生动作"


def test_migration_backfills_legacy_rows(tmp_path):
    from app.database import Base, build_engine
    from sqlalchemy import text

    engine = build_engine(tmp_path / "backfill.db")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX IF EXISTS ix_canon_facts_valid_from_chapter")
        connection.exec_driver_sql("DROP INDEX IF EXISTS ix_canon_facts_valid_until_chapter")
        connection.exec_driver_sql("ALTER TABLE canon_facts DROP COLUMN valid_from_chapter")
        connection.exec_driver_sql("ALTER TABLE canon_facts DROP COLUMN valid_until_chapter")
        connection.execute(
            text(
                "INSERT INTO novels (id, title, slug, target_word_count, word_count, chapter_count, "
                "synopsis, genre, worldview, author, created_at, updated_at) VALUES "
                "('nov_x','t','t',1000,0,0,'','','','', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        for identifier, source, status, successor in (
            ("cf_old", 3, "SUPERSEDED", "cf_new"),
            ("cf_new", 14, "CANON", None),
        ):
            connection.execute(
                text(
                    "INSERT INTO canon_facts (id, novel_id, subject, predicate, object, source_chapter, "
                    "status, confidence, visibility, known_by, origin, note, created_at, updated_at) "
                    "VALUES (:id, 'nov_x', '林默', '佩剑', :obj, :source, :status, 1.0, 'PUBLIC', '[]', "
                    "'SEED', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "id": identifier,
                    "obj": "青霜剑" if identifier == "cf_old" else "赤霄剑",
                    "source": source,
                    "status": status,
                },
            )
        connection.exec_driver_sql(
            "UPDATE canon_facts SET superseded_by = 'cf_new' WHERE id = 'cf_old'"
        )

    applied = migrate(engine)
    assert any("backfill valid_from_chapter" in item for item in applied)
    assert any("backfill valid_until_chapter" in item for item in applied)
    with engine.begin() as connection:
        rows = {
            row[0]: (row[1], row[2])
            for row in connection.exec_driver_sql(
                "SELECT id, valid_from_chapter, valid_until_chapter FROM canon_facts"
            )
        }
    assert rows["cf_old"] == (3, 14), "旧事实应在被取代的那一章失效"
    assert rows["cf_new"] == (14, None)


def test_additive_columns_cover_new_fields():
    assert "canon_facts" in ADDITIVE_COLUMNS
    names = [name for name, _ in ADDITIVE_COLUMNS["canon_facts"]]
    assert "valid_from_chapter" in names and "valid_until_chapter" in names
