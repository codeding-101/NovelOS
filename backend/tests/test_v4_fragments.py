"""V0.4 想法碎片：从「AI 写文」到「把作者的想法碎片写成文章」。

覆盖三件事：碎片本身（录入/安排/回填/可检索）、碎片成文（对应表与不发明设定）、
以及声音保护（改稿不得抹平作者的语言个性）。
"""

from __future__ import annotations

from sqlalchemy import select

from app.ai.agents.fragment_realizer import FragmentRealizer
from app.ai.base import AIResponse
from app.models import Chapter, Fragment
from app.schemas import (
    FragmentCreate,
    FragmentRealizeRequest,
    FragmentUpdate,
)
from app.services import (
    extraction_service,
    fragment_service,
    realize_service,
    style_service,
    vector_service,
)

AUTHOR_TEXTS = [
    "雨点砸在瓦上。他翻过院墙，落地时左膝一软，疼得眼前发黑。他没有答。刀锋已经从左侧劈过来——他侧身让开半步，反手一剑捅进那人肋下。远处灯笼晃了晃。还有三个人。",
    "风从街口吹过来。他把衣领压紧了些，沿着石阶一步步往上走，心里忽然想，这条路他还会走很多次吧。坡上的雾散了一半，剩下的那一半，还在等着他。",
    "他握着剑柄，指节发白。院墙外的脚步声停了。他没有回头。刀锋切进肉里的手感还在他手上，粘的，热的。",
]
SMOOTH_TEXT = (
    "他缓缓地抬起头，眼中闪过一丝寒芒，看着对面的人，然后转身离开。除此之外什么也没有发生，"
    "天空依旧阴沉，远处的山峦笼罩在雾气之中，他的心里充满了复杂的情绪与难以言喻的感受。"
)


# --------------------------------------------------------------------------- 碎片
def test_fragment_crud_and_status_flow(session, novel):
    fragment = fragment_service.create_fragment(
        session,
        novel,
        FragmentCreate(
            kind="IMAGE",
            text="雨夜的灯笼被风吹得直晃，像有人提着它在屋檐下走。",
            intent="给小镇一个不安的开场",
            tags=["雨", "灯笼"],
            related_characters=["林默"],
            priority=2,
        ),
    )
    session.commit()
    assert fragment.id.startswith("frg")
    assert fragment.status == "INBOX"
    assert fragment.title.startswith("雨夜")

    fragment_service.place(session, fragment, 21)
    session.commit()
    assert fragment.status == "PLACED" and fragment.target_chapter == 21

    fragment_service.mark_realized(
        session, fragment, chapter_id=None, excerpt="雨夜的灯笼被风吹得直晃。", treatment="QUOTED"
    )
    session.commit()
    assert fragment.status == "REALIZED" and fragment.realized_treatment == "QUOTED"

    stats = fragment_service.stats(session, novel.id)
    assert stats["total"] == 1 and stats["realized"] == 1
    assert stats["by_kind"] == {"IMAGE": 1}


def test_fragments_are_searchable(session, novel):
    fragment_service.create_fragment(
        session,
        novel,
        FragmentCreate(
            kind="WHIM",
            text="他忽然想，如果当年那封信没有送到，师父是不是就不会死。",
            intent="让主角起疑",
        ),
    )
    session.commit()
    session.flush()

    stats = vector_service.index_stats(session, novel.id)
    assert stats["by_ref_type"].get("FRAGMENT") == 1

    hits = fragment_service.relevant_fragments(session, novel, "师父的死是不是和那封信有关", limit=3)
    assert hits, "作者的想法碎片必须能被检索到"
    assert hits[0]["ref_type"] == "FRAGMENT"
    assert "信" in hits[0]["excerpt"]

    by_keyword = fragment_service.relevant_fragments(session, novel, "信没有送到", limit=3)
    assert by_keyword


def test_fragment_update_and_delete(session, novel):
    fragment = fragment_service.create_fragment(
        session, novel, FragmentCreate(text="一条准备删掉的碎片，字数够长以便入库检索。")
    )
    session.commit()
    fragment_service.update_fragment(
        session, novel, fragment, FragmentUpdate(status="ARCHIVED", priority=5, tags=["弃用"])
    )
    session.commit()
    assert fragment.status == "ARCHIVED" and fragment.priority == 5
    fragment_id = fragment.id
    fragment_service.delete_fragment(session, novel, fragment)
    session.commit()
    assert session.get(Fragment, fragment_id) is None


# --------------------------------------------------------------------------- 碎片成文
def test_offline_realizer_keeps_author_words(session, novel):
    fragments = [
        fragment_service.create_fragment(
            session, novel, FragmentCreate(text="他数了数怀里的干粮，还剩三日。")
        ),
        fragment_service.create_fragment(
            session, novel, FragmentCreate(text="第三日的黄昏，他看见了废祠的檐角。")
        ),
    ]
    session.commit()
    realizer = FragmentRealizer(extraction_service.build_agents("offline")["provider"])
    realization, meta = realizer.realize(
        session,
        novel,
        FragmentRealizeRequest(
            fragment_ids=[fragment.id for fragment in fragments],
            goals="赶往废祠",
            target_words=300,
        ),
    )
    session.commit()
    assert realization.passages
    for fragment in fragments:
        assert fragment.text.strip("。") in realization.content, "作者原话必须留在成文里"
    quoted = [p for p in realization.passages if p.treatment == "QUOTED"]
    assert quoted, "离线提供者应把作者原话标为 QUOTED，而不是假装改写过"
    assert realization.coverage["fragments_used"] == 2
    assert realization.coverage["ratio"] == 1.0


def test_realizer_drops_unverifiable_correspondence(session, novel):
    fragment = fragment_service.create_fragment(
        session, novel, FragmentCreate(text="他把木牌收进怀里，没让人看见。")
    )
    session.commit()

    class _LyingRealizer:
        name = "stub"
        model = "stub-realizer"
        kind = "llm"
        supports_tools = False

        def generate(self, request):  # noqa: ANN001
            payload = {
                "title": "测试",
                "passages": [
                    {
                        "fragment_id": "frg_not_exist",
                        "prose": "他把一块不属于任何人的牌子收了起来。",
                        "uses_quote": "",
                        "treatment": "PARAPHRASED",
                    },
                    {
                        "fragment_id": fragment.id,
                        "prose": "他把木牌收进怀里，没让人看见。",
                        "uses_quote": "这句原话根本不在碎片里",
                        "treatment": "QUOTED",
                    },
                ],
                "undeveloped": [],
                "notes": "",
            }
            return AIResponse(
                text="{}", parsed=payload, provider=self.name, model=self.model
            )

    realizer = FragmentRealizer(_LyingRealizer())  # type: ignore[arg-type]
    realization, meta = realizer.realize(
        session, novel, FragmentRealizeRequest(fragment_ids=[fragment.id], goals="测试")
    )
    session.commit()
    assert len(realization.passages) == 1, "对不上的对应关系必须被丢弃"
    assert realization.passages[0].uses_quote == ""
    assert any("不在本次碎片里" in warning for warning in meta["warnings"])
    assert any("不在碎片原文里" in warning for warning in meta["warnings"])


def test_realizer_reports_invented_settings(session, novel):
    fragment = fragment_service.create_fragment(
        session, novel, FragmentCreate(text="他推开门，屋里没人。")
    )
    session.commit()

    class _InventingRealizer:
        name = "stub"
        model = "stub-realizer"
        kind = "llm"
        supports_tools = False

        def generate(self, request):  # noqa: ANN001
            payload = {
                "title": "测试",
                "passages": [
                    {
                        "fragment_id": fragment.id,
                        "prose": "他推开门，屋里没人。林默的佩剑是玄铁重剑。",
                        "uses_quote": "",
                        "treatment": "EXPANDED",
                    }
                ],
                "undeveloped": [],
                "notes": "",
            }
            return AIResponse(text="{}", parsed=payload, provider=self.name, model=self.model)

    realizer = FragmentRealizer(_InventingRealizer())  # type: ignore[arg-type]
    realization, meta = realizer.realize(
        session, novel, FragmentRealizeRequest(fragment_ids=[fragment.id], goals="测试")
    )
    session.commit()
    assert realization.invented_claims, "成文里新增的设定性陈述必须被报出来"
    assert any("Canon 未记录" in warning for warning in meta["warnings"])
    assert any("玄铁重剑" in item["object"] for item in realization.invented_claims)


def test_realize_service_saves_chapter_and_marks_fragments(session, novel):
    fragments = [
        fragment_service.create_fragment(
            session, novel, FragmentCreate(text="他把断成两截的枪头捡起来，塞进包袱。")
        ),
        fragment_service.create_fragment(
            session, novel, FragmentCreate(text="山道上有人在唱一支他小时候听过的调子。")
        ),
    ]
    session.commit()
    response = realize_service.realize_fragments(
        session,
        novel,
        FragmentRealizeRequest(
            fragment_ids=[fragment.id for fragment in fragments],
            goals="下山路上",
            chapter_number=21,
            target_words=400,
            save=True,
            provider="offline",
        ),
    )
    session.commit()
    assert response.realization.saved_chapter_id
    chapter = session.get(Chapter, response.realization.saved_chapter_id)
    assert chapter is not None and chapter.status == "DRAFT"
    assert chapter.content == response.realization.content
    assert response.fragments_updated == 2

    for fragment in fragments:
        session.refresh(fragment)
        assert fragment.status == "REALIZED"
        assert fragment.realized_excerpt
        assert fragment.realized_chapter_id == chapter.id
    assert response.voice == {} or "score" in response.voice


def test_realize_with_revision_loop_reports_style(session, novel):
    fragment = fragment_service.create_fragment(
        session,
        novel,
        FragmentCreate(text="他盯着那盏灯看了很久，直到灯芯烧短了一截，才把手从刀柄上松开。"),
    )
    session.commit()
    style_service.save_voice_profile(session, novel, AUTHOR_TEXTS, name="测试声音")
    session.commit()
    response = realize_service.realize_fragments(
        session,
        novel,
        FragmentRealizeRequest(
            fragment_ids=[fragment.id],
            goals="松手",
            chapter_number=22,
            target_words=300,
            revise=True,
            max_rounds=1,
            use_model_critic=False,
            provider="offline",
        ),
    )
    session.commit()
    assert response.realization.content
    assert response.style.get("rounds"), "闭环应给出轮次轨迹"
    assert response.style.get("final_score", 0) >= 0
    assert fragment.text.strip("。") in response.realization.content


# --------------------------------------------------------------------------- 声音保护
def test_voice_profile_and_scoring(session, novel):
    profile = style_service.save_voice_profile(session, novel, AUTHOR_TEXTS, name="作者声音测试")
    session.commit()
    assert profile is not None
    terms = profile.metrics["signature_terms"]
    assert len(terms) >= 3, f"应能从作者的文字里提出特征词：{terms}"

    same = style_service.voice_score(AUTHOR_TEXTS[0], profile)
    smooth = style_service.voice_score(SMOOTH_TEXT, profile)
    assert same["score"] is not None and same["score"] > 70, same
    assert smooth["score"] < same["score"] - 15, (same["score"], smooth["score"])

    issues, _report = style_service.voice_rules(SMOOTH_TEXT, profile)
    codes = {issue.code for issue in issues}
    assert "VOICE_WEAK" in codes or "OVER_REGULAR" in codes

    # 作者自己的文字不该被判成「过于规整」
    own_issues, _ = style_service.voice_rules(AUTHOR_TEXTS[0], profile)
    assert "OVER_REGULAR" not in {issue.code for issue in own_issues}


def test_voice_profile_from_fragments(session, novel):
    for text in AUTHOR_TEXTS:
        fragment_service.create_fragment(session, novel, FragmentCreate(text=text))
    session.commit()
    profile = style_service.profile_from_fragments(session, novel)
    session.commit()
    assert profile is not None and profile.source == "VOICE"
    assert len(profile.metrics["signature_terms"]) >= 3


def test_revision_loop_reverts_when_author_voice_is_worn_away(session, novel):
    """改稿把作者特征磨平了就回退 —— 这是「语言个性」的护栏。"""
    from app.schemas import WriteChapterRequest

    style_service.save_voice_profile(session, novel, AUTHOR_TEXTS, name="护栏测试声音")
    session.commit()

    class _VoiceEatingProvider:
        name = "voice-eater"
        model = "voice-eater-1"
        kind = "llm"
        supports_tools = False

        def generate(self, request):  # noqa: ANN001
            if request.task == "write":
                return AIResponse(
                    text="# 第21章 测试\n\n" + AUTHOR_TEXTS[0],
                    provider=self.name,
                    model=self.model,
                )
            if request.task == "style_revise":
                # 把作者的味道全洗掉：规整、书面、无特征词
                return AIResponse(
                    text=(
                        "他抬起头观察对面的人，随后转身离开。院子里十分安静，天空阴沉，"
                        "远处的山峦笼罩在雾气中，他内心充满复杂的情绪。"
                    ),
                    provider=self.name,
                    model=self.model,
                )
            return AIResponse(text="{}", parsed={}, provider=self.name, model=self.model)

    from app.ai.agents.revision_loop import RevisionLoop

    loop = RevisionLoop(_VoiceEatingProvider())  # type: ignore[arg-type]
    result = loop.run(
        session,
        novel,
        WriteChapterRequest(goals="测试声音护栏", characters=["林默"], chapter_number=21, target_words=400),
        max_rounds=2,
        target_score=99.0,
        use_model_critic=False,
    )
    session.commit()
    # 两层护栏任一触发都算通过：内层（单次改稿的声音护栏）或外层（闭环回退）
    assert any("作者的声音磨掉" in warning or "作者特征" in warning for warning in result.warnings), (
        result.warnings
    )
    assert any(row.stage in ("no-change", "reverted") for row in result.rounds), (
        [row.stage for row in result.rounds]
    )
    assert "刀锋" in result.draft.content or "院墙" in result.draft.content, (
        "回退后应保留作者原稿里的用词"
    )


def test_emotion_prompts_ask_questions(session, novel):
    result = realize_service.emotion_prompts(
        session, novel, goals="他在墓前站了一夜", characters=["林默"], chapter_number=20
    )
    session.commit()
    assert result["questions"], "应给出引导问题"
    assert all(question.strip().endswith(("？", "?")) for question in result["questions"])
    assert any("失去" in question or "说" in question for question in result["questions"])


def test_planner_context_includes_unplaced_fragments(session, novel):
    fragment_service.create_fragment(
        session,
        novel,
        FragmentCreate(text="他想在结尾让人看见一只停在刀柄上的麻雀。", intent="收尾意象"),
    )
    session.commit()
    from app.ai.agents.planner import PlannerAgent

    agent = PlannerAgent(extraction_service.build_agents("offline")["provider"])
    context = agent.build_context(session, novel, from_chapter=21, count=3)
    assert context["unplaced_fragments"], "规划必须看到作者还没安排的碎片"
    assert "麻雀" in context["unplaced_fragments"][0]["text"]


# --------------------------------------------------------------------------- 接口
def test_degenerate_rewrite_is_rejected(session, novel):
    """模型偶尔会返回一两句就交差：这种退化改稿不能收，必须保留原稿并说明。"""
    from app.ai.agents.style_critic import StyleCritic

    class _DegenerateProvider:
        name = "degenerate"
        model = "degenerate-1"
        kind = "llm"
        supports_tools = False

        def generate(self, request):  # noqa: ANN001
            return AIResponse(
                text="他走了。",  # 原文几百字，这里只剩一句
                provider=self.name,
                model=self.model,
            )

    critic = StyleCritic(_DegenerateProvider())  # type: ignore[arg-type]
    original = AUTHOR_TEXTS[0] + AUTHOR_TEXTS[1]
    revised, warnings, _provider, _model = critic.rewrite(
        session, novel, original, [{"code": "CLICHE_DENSE", "metric": "cliche_per_1k", "message": "x"}]
    )
    assert revised == original, "退化改稿必须被拒绝"
    assert any("偏离过大" in warning for warning in warnings)


def test_realize_rejects_existing_chapter_number(client):
    """章号已存在时应给 409 并说明，而不是 500。"""
    novel = client.post("/api/novels", json={"title": "章号冲突"}).json()
    client.post(f"/api/novels/{novel['id']}/seed")
    response = client.post(
        f"/api/novels/{novel['id']}/fragments/realize",
        json={
            "raw_fragments": ["他站在门口，没有进去。"],
            "chapter_number": 5,
            "target_words": 300,
            "save": True,
        },
    )
    assert response.status_code == 409
    assert "已存在" in response.json()["detail"]


def test_fragment_api_flow(client):
    novel = client.post("/api/novels", json={"title": "碎片接口"}).json()
    client.post(f"/api/novels/{novel['id']}/seed")

    created = client.post(
        f"/api/novels/{novel['id']}/fragments",
        json={"kind": "LINE", "text": "「我等你很久了。」他说。", "intent": "让反派先开口"},
    )
    assert created.status_code == 201, created.text
    fragment = created.json()
    assert fragment["status"] == "INBOX"

    bulk = client.post(
        f"/api/novels/{novel['id']}/fragments/bulk",
        json=[
            {"text": "碎片一：屋檐下的冰凌在响。"},
            {"text": "碎片二：他把纸烧了，灰落在鞋面上。"},
        ],
    )
    assert bulk.status_code == 201 and len(bulk.json()) == 2

    stats = client.get(f"/api/novels/{novel['id']}/fragments/stats").json()
    assert stats["total"] == 3 and stats["unplaced"] == 3

    placed = client.post(f"/api/fragments/{fragment['id']}/place", json={"chapter_number": 21}).json()
    assert placed["status"] == "PLACED" and placed["target_chapter"] == 21

    intent = client.get(f"/api/novels/{novel['id']}/fragments/intent/21").json()
    assert intent["count"] == 1 and intent["intents"] == ["让反派先开口"]

    realized = client.post(
        f"/api/novels/{novel['id']}/fragments/realize",
        json={
            "fragment_ids": [fragment["id"]],
            "goals": "客栈相遇",
            "chapter_number": 21,
            "target_words": 300,
            "save": True,
        },
    )
    assert realized.status_code == 200, realized.text
    body = realized.json()
    assert body["realization"]["passages"][0]["fragment_id"] == fragment["id"]
    assert body["realization"]["passages"][0]["treatment"] == "QUOTED"
    assert "我等你很久了" in body["realization"]["content"]
    assert body["realization"]["saved_chapter_id"]
    assert body["provider"] == "offline"
    assert body["fragments_updated"] == 1

    after = client.get(f"/api/fragments/{fragment['id']}").json()
    assert after["status"] == "REALIZED" and after["realized_excerpt"]

    prompts = client.post(
        f"/api/novels/{novel['id']}/fragments/prompts",
        json={"goals": "客栈相遇", "characters": ["林默"], "chapter_number": 21},
    ).json()
    assert prompts["questions"]

    voice = client.post(
        f"/api/novels/{novel['id']}/style/voice",
        json={"name": "接口声音", "texts": AUTHOR_TEXTS},
    ).json()
    assert voice["available"] is True
    assert voice["signature_terms"]
    assert client.get(f"/api/novels/{novel['id']}/style/voice").json()["name"] == "接口声音"

    assert client.post(f"/api/novels/{novel['id']}/fragments/realize", json={}).status_code == 422
    assert client.get("/api/fragments/frg_missing").status_code == 404


def test_dashboard_includes_fragment_stats(client):
    novel = client.post("/api/novels", json={"title": "碎片看板"}).json()
    client.post(f"/api/novels/{novel['id']}/seed")
    client.post(
        f"/api/novels/{novel['id']}/fragments",
        json={"text": "他想在最后一页写一场雪。"},
    )
    board = client.get(f"/api/novels/{novel['id']}/dashboard").json()
    assert board["fragments_total"] == 1
    assert board["fragments_unplaced"] == 1
    assert board["fragment_realization_rate"] == 0.0


def test_style_baseline_also_builds_voice_profile(session, novel):
    profile = style_service.profile_from_chapters(session, novel)
    session.commit()
    assert profile is not None
    voice = style_service.default_voice_profile(session, novel.id)
    assert voice is not None, "建立本书基线时应同时建立作者声音画像"
    assert voice.metrics.get("signature_terms")

    chapter = session.scalar(
        select(Chapter).where(Chapter.novel_id == novel.id, Chapter.chapter_number == 3)
    )
    review = style_service.review_text(chapter.content, profile=profile, voice_profile=voice)
    assert review["voice"].get("available") is True
    assert review["voice"]["score"] is not None
    assert review["baseline"] == profile.name
