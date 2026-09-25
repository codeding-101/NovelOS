"""V0.7 本书设定与大纲：作者写进去的东西必须真的被模型读到。

之前 synopsis / worldview 存在库里，但没有任何提示词用过它们——
「重新选材、重编大纲」等于白写。这一组用例守着这条链路：字段能存能改，
并且真的出现在规划、写作、碎片成文的提示词里。
"""

from __future__ import annotations

from sqlalchemy import text

from app.ai.agents import ChapterWriter, FragmentRealizer, PlannerAgent
from app.ai.base import AIResponse
from app.schemas import FragmentCreate, FragmentRealizeRequest, WriteChapterRequest
from app.services import fragment_service, novel_service

SYNOPSIS = "简介：一个三品剑骨的少年从外门走到宗门之巅。"
WORLDVIEW = "世界观：剑修九品，炼气之上是筑基，御剑需筑基。"
OUTLINE = "全书大纲：三卷。第一卷在青云山立身，第二卷查父亲旧案，第三卷与血河教正面撞上。"


def _fill_setting(session, novel):
    novel.genre = "东方玄幻"
    novel.synopsis = SYNOPSIS
    novel.worldview = WORLDVIEW
    novel.outline = OUTLINE
    session.commit()
    return novel


class _CapturingProvider:
    """记录每次请求，并按任务返回可解析的假响应。"""

    name = "stub"
    model = "stub-v7"
    kind = "llm"
    supports_tools = False

    def __init__(self, reply: str, fragment_id: str | None = None):
        self.reply = reply
        self.fragment_id = fragment_id
        self.requests: list = []

    def generate(self, request):  # noqa: ANN001
        self.requests.append(request)
        parsed = None
        if request.task == "plan":
            parsed = {
                "chapters": [
                    {
                        "chapter_number": 21,
                        "title": "试剑",
                        "goals": "承接大纲第一卷收尾",
                        "must_include": [],
                        "forbidden": [],
                        "characters": ["林默"],
                        "advance_foreshadowing": [],
                        "rationale": "按大纲推进",
                    }
                ],
                "notes": "",
            }
        elif request.task == "realize" and self.fragment_id:
            parsed = {
                "title": "试剑",
                "passages": [
                    {
                        "fragment_id": self.fragment_id,
                        "prose": "他站在山门前，雾还没散。",
                        "uses_quote": "",
                        "treatment": "EXPANDED",
                    }
                ],
                "undeveloped": [],
                "notes": "",
            }
        return AIResponse(text=self.reply, parsed=parsed, provider=self.name, model=self.model)


# --------------------------------------------------------------------------- 字段本身
def test_outline_column_exists_and_is_empty_by_default(session, novel):
    columns = {item[1] for item in session.execute(text("PRAGMA table_info(novels)")).fetchall()}
    assert "outline" in columns, "novels 表应当有 outline 列（迁移负责补上）"
    assert novel.outline == "", "新书的大纲默认为空"


def _clear_setting(session, novel):
    """种子小说自带简介与世界观，断言「空」之前要先清掉。"""
    novel.genre = ""
    novel.synopsis = ""
    novel.worldview = ""
    novel.outline = ""
    session.commit()
    return novel


def test_novel_context_renders_only_filled_fields(session, novel):
    _clear_setting(session, novel)
    block = novel_service.novel_context(novel)
    assert "全书大纲" not in block and "世界观" not in block, "没填的字段不要出现"
    assert "本书设定" in block and novel.title in block, "书名与进度始终在"

    novel.outline = OUTLINE
    session.flush()
    block = novel_service.novel_context(novel)
    assert "全书大纲" in block and "三卷" in block
    assert "世界观" not in block, "没填的字段不要出现"

    _fill_setting(session, novel)
    block = novel_service.novel_context(novel)
    for expected in (SYNOPSIS, WORLDVIEW, OUTLINE, "东方玄幻"):
        assert expected in block


def test_seed_novel_setting_reaches_the_context(session, novel):
    """种子小说本来就有简介和世界观：这次改动之后它们才真的进得了提示词。"""
    block = novel_service.novel_context(novel)
    assert "简介：" in block or "简介" in block, block[:80]
    assert novel.synopsis[:12] in block
    assert novel.worldview[:12] in block


def test_creating_a_novel_keeps_every_field(client):
    """建书时给的字段必须全都落库：之前逐字段手写，outline 被静默丢过。"""
    created = client.post(
        "/api/novels",
        json={
            "title": "建书字段测试",
            "synopsis": "一句简介",
            "genre": "仙侠",
            "worldview": "境界与势力",
            "outline": "三卷：起、承、合",
            "author": "作者",
            "target_word_count": 800000,
        },
    ).json()
    assert created["outline"] == "三卷：起、承、合"
    assert created["genre"] == "仙侠" and created["worldview"] == "境界与势力"
    assert created["synopsis"] == "一句简介" and created["author"] == "作者"
    assert created["target_word_count"] == 800000
    assert created["chapter_words_min"] == 2000 and created["daily_words_target"] == 4000

    fetched = client.get(f"/api/novels/{created['id']}").json()
    assert fetched["outline"] == "三卷：起、承、合", "重新读一遍也要在"


def test_outline_is_editable_through_the_api(client):
    created = client.post("/api/novels", json={"title": "大纲接口测试"}).json()
    assert created["outline"] == ""

    patched = client.patch(f"/api/novels/{created['id']}", json={"outline": OUTLINE}).json()
    assert patched["outline"] == OUTLINE
    assert client.get(f"/api/novels/{created['id']}").json()["outline"] == OUTLINE

    again = client.patch(
        f"/api/novels/{created['id']}", json={"synopsis": SYNOPSIS, "worldview": WORLDVIEW}
    ).json()
    assert again["outline"] == OUTLINE, "只改其它字段不该清空大纲"


# --------------------------------------------------------------------------- 真的进提示词
def test_writer_prompt_carries_book_setting(session, novel):
    _fill_setting(session, novel)
    provider = _CapturingProvider("# 第21章 试剑\n他握紧了剑柄，看着对面的人。")
    writer = ChapterWriter(provider)  # type: ignore[arg-type]
    response = writer.write(
        session,
        novel,
        WriteChapterRequest(goals="按大纲推进第一卷收尾", characters=["林默"], target_words=400),
    )
    session.commit()

    prompt = provider.requests[0].prompt
    assert OUTLINE in prompt and WORLDVIEW in prompt and SYNOPSIS in prompt
    assert "本书设定" in prompt
    assert provider.requests[0].context["novel_context"], "生成记录里要留下当时读到的本书设定"
    assert any("大纲" in warning for warning in response.warnings)


def test_writer_warns_when_book_setting_is_empty(session, novel):
    _clear_setting(session, novel)
    provider = _CapturingProvider("# 第21章 试剑\n他握紧了剑柄。")
    writer = ChapterWriter(provider)  # type: ignore[arg-type]
    response = writer.write(
        session, novel, WriteChapterRequest(goals="随便写点", characters=["林默"], target_words=400)
    )
    session.commit()
    assert any("还没有简介、世界观或大纲" in warning for warning in response.warnings)
    assert "还没有填简介" in provider.requests[0].prompt


def test_planner_prompt_carries_book_setting(session, novel):
    _fill_setting(session, novel)
    provider = _CapturingProvider("{}")
    planner = PlannerAgent(provider)  # type: ignore[arg-type]
    plan, _meta = planner.plan(session, novel, from_chapter=21, count=1, steer="推进第一卷收尾")
    session.commit()

    assert plan.chapters, "假响应应当通过校验，确保这次调用真的走到了提示词"
    prompt = provider.requests[0].prompt
    assert OUTLINE in prompt and "本书设定" in prompt


def test_realizer_prompt_carries_book_setting(session, novel):
    _fill_setting(session, novel)
    fragment = fragment_service.create_fragment(
        session, novel, FragmentCreate(text="他站在山门前，雾还没散。", intent="开篇的画面")
    )
    session.commit()

    provider = _CapturingProvider("{}", fragment_id=fragment.id)
    realizer = FragmentRealizer(provider)  # type: ignore[arg-type]
    realization, _meta = realizer.realize(
        session, novel, FragmentRealizeRequest(fragment_ids=[fragment.id], goals="开篇")
    )
    session.commit()

    assert realization.passages, "假响应应当产出对应关系"
    prompt = provider.requests[0].prompt
    assert OUTLINE in prompt and WORLDVIEW in prompt and "本书设定" in prompt
