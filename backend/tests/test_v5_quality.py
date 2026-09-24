"""V0.5 质量护栏：推进与节奏指标、六条新规则、跨章复读、声称核对、文风锁定。"""

from __future__ import annotations

from app.ai.agents.claim_verifier import ClaimVerifier, verify_claims
from app.ai.base import AIResponse
from app.services import claim_service, style_service
from tests.test_v3_style import AI_FLAVOR_TEXT, CLEAN_LONG_TEXT

PURE_DESCRIPTION = (
    "青云山很高。云在山的半腰上，一层一层地堆着。山石是青灰色的，被雨水洗得发亮，纹路里长着薄薄的苔。\n"
    "崖边的松树斜斜地长着，枝干弯向山谷。雾从谷底浮起来，慢慢漫过石阶，漫过石栏，漫过整座山门，把门柱上的字泡得发胀。\n"
    "阳光从云缝里漏下来，落在石阶上，拉出一道道细长的影子。风里带着湿润的气味，也带着松针的气味和一点铁锈味。\n"
    "远处的山脊像一条静伏着的线。天色由青转白，又由白转青，最后沉成一片灰蓝，把整片山谷都收进这个颜色里。\n"
    "石阶一共有三百六十级，每一级的边角都被踩得圆润。石栏上刻着云纹，云纹的深浅各处不同，边角也已经磨平。\n"
    "入夜之后，山谷里只剩下水声。月光照在湿石头上，反出一点冷冷的光，石缝里的水一滴一滴落下来。\n"
)
DEFINITION_DUMP = (
    "所谓剑骨，指的是人出生时骨骼中蕴含的剑意资质，也就是常说的天赋，与后天勤修并无太大关系。\n"
    "骨品一共分为九品，三品以上称为上品，六品以下称为下品。事实上，骨品只决定修行的起点，不能决定终点。\n"
    "剑诀分为三卷，上卷讲养气，中卷讲运剑，下卷讲杀伐。换言之，三卷分别是三个阶段，次序不能颠倒。\n"
    "炼气三层指的是气感稳定在三个刻度上。据说这一境界大致可分为前中后三期，每一期的感应范围都不同。\n"
    "青云剑宗又称青云剑派，据传来源于千年前的一支游侠。严格来说，它并不是门派，而是一处传剑的场所。\n"
    "内门弟子称为入门执剑者，外门弟子称为备选。具体而言，两者的差别在于能否进入剑冢观碑。\n"
)
UNIFORM_PUNCTUATION = (
    "他走进了山门。他看见了石阶。他抬起了脚步。他握紧了剑柄。他低下了头颅。他张开了嘴。\n"
    "风从东边吹来。风从西边吹来。风从南边吹来。风从北边吹来。风从中间吹来。风从上面吹来。\n"
    "石头很硬。木头很软。铁块很沉。布条很轻。水很凉。火很烫。夜很长。路很远。\n"
    "他往前一步。他往后一步。他往左一步。他往右一步。他往上一步。他往下一步。他往中间一步。\n"
    "天很黑。地很白。人很多。声很杂。夜很深。梦很长。云很低。风很大。\n"
    "他抬起了手。他放下了手。他抬起了脚。他放下了脚。他抬起了头。他低下了头。\n"
)
LOOPING_SENTENCES = (
    "林默抬起了手，向前伸了出去。他抬起了另一只手，向前伸了出去，指尖抖了一下。\n"
    "林默抬起了手，向那少年指了指。他抬起了另一只手，向那少年指了指，指尖也抖了一下。\n"
    "林默抬起了剑，向前刺了出去。他抬起了另一把剑，向前刺了出去，剑尖也抖了一下。\n"
    "林默抬起了脚，向前迈了出去。他抬起了另一只脚，向前迈了出去，鞋底也抖了一下。\n"
    "林默抬起了头，向上望了出去。他抬起了另一只头，向上望了出去，脖子也抖了一下。\n"
    "林默抬起了臂，向前挥了出去。他抬起了另一条臂，向前挥了出去，腕子也抖了一下。\n"
)

V5_CODES = (
    "LOW_ADVANCEMENT",
    "FILLER_HEAVY",
    "EXPOSITION_DUMP",
    "PUNCT_FLAT",
    "EMOTION_FLAT",
    "PATTERN_LOOP",
    "RESTATING_KNOWN",
)


def _codes(report: dict) -> set[str]:
    return {issue["code"] for issue in report["issues"]}


# --------------------------------------------------------------------------- 指标
def test_v5_metrics_are_deterministic_and_bounded():
    first = style_service.measure(CLEAN_LONG_TEXT).to_dict()
    second = style_service.measure(CLEAN_LONG_TEXT).to_dict()
    assert first == second
    for key in (
        "advancement_per_1k",
        "filler_paragraph_ratio",
        "exposition_per_1k",
        "punctuation_entropy",
        "emotion_flatness",
        "pattern_loop_ratio",
    ):
        assert isinstance(first[key], float) or isinstance(first[key], int)
    assert 0.0 <= first["punctuation_entropy"] <= 1.0
    assert 0.0 <= first["emotion_flatness"] <= 1.0
    assert 0.0 <= first["filler_paragraph_ratio"] <= 1.0


def test_pure_description_has_no_advancement_and_many_filler_paragraphs():
    metrics = style_service.measure(PURE_DESCRIPTION)
    assert metrics.advancement_per_1k == 0.0, "纯景物段里不该有剧情动作"
    assert metrics.filler_paragraph_ratio >= 0.8
    assert metrics.punctuation_entropy < 0.34


def test_seed_chapters_do_not_trigger_v5_rules(session, novel, chapters):
    """已认可的正文章节在默认阈值下不该被新规则误报。"""
    for chapter in chapters.values():
        report = style_service.review_text(chapter.content or "", min_chars=300)
        hit = _codes(report) & set(V5_CODES)
        assert not hit, f"第{chapter.chapter_number}章被误报：{sorted(hit)}"


# --------------------------------------------------------------------------- 六条规则
def test_filler_heavy_flags_pure_description_with_evidence():
    report = style_service.review_text(PURE_DESCRIPTION, min_chars=200)
    issue = next(i for i in report["issues"] if i["code"] == "FILLER_HEAVY")
    assert issue["level"] == "warning"
    assert issue["excerpt"], "注水必须给出具体段落作为证据"
    assert "LOW_ADVANCEMENT" in _codes(report), "整章没有剧情动作就该同时被点到"


def test_exposition_dump_flags_definition_sentences():
    report = style_service.review_text(DEFINITION_DUMP, min_chars=200)
    issue = next(i for i in report["issues"] if i["code"] == "EXPOSITION_DUMP")
    assert issue["value"] > 10
    assert any(marker in issue["excerpt"] for marker in ("所谓", "分为", "称为", "指的是"))


def test_uniform_punctuation_is_flagged():
    report = style_service.review_text(UNIFORM_PUNCTUATION, min_chars=200)
    codes = _codes(report)
    assert "PUNCT_FLAT" in codes
    assert "EMOTION_FLAT" in codes, "全篇一个情绪档位就是没有起伏"
    assert style_service.measure(UNIFORM_PUNCTUATION).punctuation_entropy < 0.34


def test_pattern_loop_is_flagged_with_skeletons():
    report = style_service.review_text(LOOPING_SENTENCES, min_chars=200)
    issue = next(i for i in report["issues"] if i["code"] == "PATTERN_LOOP")
    assert issue["value"] > 0.3
    assert issue["excerpt"], "要列出共用骨架，作者才知道改哪里"
    assert "×" in issue["excerpt"]


def test_v5_rules_do_not_fire_on_clean_varied_text():
    report = style_service.review_text(CLEAN_LONG_TEXT, min_chars=200)
    assert not (_codes(report) & set(V5_CODES)), sorted(_codes(report) & set(V5_CODES))


def test_compare_metrics_knows_v5_directions():
    thin = style_service.measure(PURE_DESCRIPTION).to_dict()
    rich = style_service.measure(CLEAN_LONG_TEXT).to_dict()
    deltas = style_service.compare_metrics(thin, rich)["deltas"]
    assert deltas["advancement_per_1k"]["better"] is True, "推进信号变多算改善"
    assert deltas["filler_paragraph_ratio"]["better"] is True, "注水变少算改善"
    assert deltas["punctuation_entropy"]["better"] is True, "标点类型用开算改善"


# --------------------------------------------------------------------------- 跨章复读
def test_restating_known_flags_copied_paragraph(session, novel, chapters):
    source = next(
        paragraph
        for paragraph in (chapters[5].content or "").split("\n")
        if len(paragraph.strip()) >= 40
    )
    text = source.strip() + "\n他没说话，把刀收进鞘里，转身走了。\n"
    report = style_service.review_text(
        text,
        min_chars=60,
        prior_texts=[chapters[5].content or "", chapters[6].content or ""],
        prior_labels=["第 5 章", "第 6 章"],
    )
    issue = next(i for i in report["issues"] if i["code"] == "RESTATING_KNOWN")
    assert issue["value"] >= 0.55
    assert "第 5 章" in issue["message"]
    assert issue["excerpt"]

    fresh = style_service.restating_issues(CLEAN_LONG_TEXT, [chapters[5].content or ""])
    assert fresh == [], "原创内容不该被当成复读"


# --------------------------------------------------------------------------- 文风锁定
def test_locking_narrows_the_window(session, novel):
    profile = style_service.save_profile(
        session, novel, [CLEAN_LONG_TEXT], name="锁定用基线", make_default=True
    )
    session.commit()
    fallback = style_service.DEFAULT_RANGES["cliche_per_1k"]
    _low, high_open = style_service._threshold(profile.metrics, "cliche_per_1k", fallback)
    _low2, high_locked = style_service._threshold(
        profile.metrics,
        "cliche_per_1k",
        fallback,
        scale=style_service.LOCKED_TOLERANCE_SCALE,
    )
    assert high_locked < high_open, "锁定后窗口必须收紧"

    before = style_service.review_text(AI_FLAVOR_TEXT, profile=profile)
    assert before["locked"] is False
    assert "STYLE_DRIFT_LOCKED" not in _codes(before)

    style_service.lock_profile(session, profile, True)
    session.commit()
    assert profile.locked is True
    assert style_service.default_profile(session, novel.id).id == profile.id

    after = style_service.review_text(AI_FLAVOR_TEXT, profile=profile)
    assert after["locked"] is True
    assert "STYLE_DRIFT_LOCKED" in _codes(after)
    assert {item["metric"] for item in after["locked_drift"]} >= {"cliche_per_1k"}


def test_locking_keeps_one_profile_per_novel(session, novel):
    first = style_service.save_profile(session, novel, [CLEAN_LONG_TEXT], name="甲", make_default=True)
    second = style_service.save_profile(session, novel, [CLEAN_LONG_TEXT], name="乙", make_default=True)
    session.commit()
    style_service.lock_profile(session, first, True)
    style_service.lock_profile(session, second, True)
    session.commit()
    assert first.locked is False and second.locked is True, "同一本书同时只锁一个文风"
    assert style_service.default_profile(session, novel.id).id == second.id


def test_drift_report_lists_chapters(session, novel, chapters):
    profile = style_service.save_profile(session, novel, [CLEAN_LONG_TEXT], name="漂移基线", make_default=True)
    style_service.lock_profile(session, profile, True)
    session.commit()
    report = style_service.drift_report(session, novel)
    assert report["locked"] is True
    assert report["profile_id"] == profile.id
    assert report["chapters"], "应当逐章给出漂移情况"
    assert all("drifted" in item for item in report["chapters"])
    assert report["drifted_count"] <= len(report["chapters"])


# --------------------------------------------------------------------------- 声称核对（纯函数）
def _facts():
    return [
        {
            "id": "cf1",
            "subject": "林默",
            "predicate": "佩剑",
            "object": "青霜剑",
            "source_chapter": 3,
        },
        {
            "id": "cf2",
            "subject": "赵铁山",
            "predicate": "状态",
            "object": "死亡",
            "source_chapter": 8,
        },
    ]


def test_verify_claims_marks_conflict_unverified_and_supported():
    claims = [
        {"subject": "林默", "predicate": "佩剑", "object": "青霜剑", "quote": "林默的佩剑是青霜剑。"},
        {"subject": "林默", "predicate": "佩剑", "object": "青云剑", "quote": "林默的佩剑是青云剑。"},
        {"subject": "苏月宁", "predicate": "佩剑", "object": "碎星", "quote": "苏月宁的佩剑是碎星。"},
    ]
    verdicts = verify_claims(claims, facts=_facts(), rules=[], names=["林默", "苏月宁"])
    by_object = {item["claim"]["object"]: item for item in verdicts}
    assert by_object["青霜剑"]["verdict"] == "SUPPORTED"
    assert by_object["青云剑"]["verdict"] == "CONFLICT"
    assert "青霜剑" in by_object["青云剑"]["reason"]
    assert by_object["青云剑"]["evidence"][0]["fact_id"] == "cf1"
    assert by_object["碎星"]["verdict"] == "UNVERIFIED"
    assert "确认" in by_object["碎星"]["reason"]


def test_verify_claims_downgrades_non_comparable_object():
    """模型把长句当取值时，降级成人工确认，而不是报一条假冲突。"""
    claims = [
        {
            "subject": "林默",
            "predicate": "师承",
            "object": "师父所授第一式“守”",
            "quote": "师父教的第一式，是‘守’。",
        }
    ]
    facts = [
        {"id": "cf3", "subject": "林默", "predicate": "师承", "object": "赵铁山", "source_chapter": 3}
    ]
    verdicts = verify_claims(claims, facts=facts, rules=[], names=["林默"])
    assert verdicts[0]["verdict"] == "UNVERIFIED"
    assert "人工确认" in verdicts[0]["reason"]


def test_proposed_fact_is_not_treated_as_canon():
    claims = [{"subject": "林默", "predicate": "佩剑", "object": "玄铁重剑", "quote": "他的剑很沉。"}]
    verdicts = verify_claims(
        claims,
        facts=[],
        proposed=[
            {
                "id": "cf9",
                "subject": "林默",
                "predicate": "佩剑",
                "object": "玄铁重剑",
                "source_chapter": None,
            }
        ],
        rules=[],
        names=["林默"],
    )
    assert verdicts[0]["verdict"] == "UNVERIFIED", "待确认的条目不能当成已验证"
    assert "待确认" in verdicts[0]["reason"]


# --------------------------------------------------------------------------- 世界观规则判定
def _rules():
    return [
        {
            "id": "wr1",
            "name": "死者不可复生",
            "rule_type": "no_resurrection",
            "subject": "状态:死亡",
            "description": "死者不可复生。",
            "source_chapter": 8,
        },
        {
            "id": "wr2",
            "name": "御剑飞行门槛",
            "rule_type": "capability_gate",
            "subject": "御剑飞行",
            "description": "修为不足筑基者不可御剑飞行。",
            "source_chapter": 11,
        },
        {
            "id": "wr3",
            "name": "灵根天生",
            "rule_type": "immutable_trait",
            "subject": "灵根",
            "description": "灵根天生，无法后天改变。",
            "source_chapter": 1,
        },
        {
            "id": "wr4",
            "name": "本命灵剑唯一",
            "rule_type": "possession_unique",
            "subject": "本命灵剑",
            "description": "一人不可同时持有两把本命灵剑。",
            "source_chapter": 3,
        },
    ]


def test_no_resurrection_rule_catches_dead_character_acting():
    facts = _facts()
    verdicts = verify_claims(
        [],
        facts=facts,
        rules=[_rules()[0]],
        names=["赵铁山"],
        text="赵铁山提剑迎上前去，白发在风里散开。",
    )
    assert [item["verdict"] for item in verdicts] == ["CONFLICT"]
    assert verdicts[0]["evidence"][0]["rule_type"] == "no_resurrection"


def test_no_resurrection_rule_ignores_retrospection():
    verdicts = verify_claims(
        [],
        facts=_facts(),
        rules=[_rules()[0]],
        names=["赵铁山"],
        text="当年赵铁山提剑迎上去的样子，他一直记得。",
    )
    assert verdicts == [], "回忆里出现死人不是冲突"


def test_capability_gate_uses_realm_from_canon():
    facts = [
        {
            "id": "cf5",
            "subject": "林默",
            "predicate": "修为",
            "object": "炼气九层",
            "source_chapter": 12,
        }
    ]
    verdicts = verify_claims(
        [],
        facts=facts,
        rules=[_rules()[1]],
        names=["林默"],
        text="林默御剑飞行，掠过了整条山脊。",
    )
    assert [item["verdict"] for item in verdicts] == ["CONFLICT"]
    assert "炼气" in verdicts[0]["reason"]

    allowed = verify_claims(
        [],
        facts=[{**facts[0], "object": "金丹初期"}],
        rules=[_rules()[1]],
        names=["林默"],
        text="林默御剑飞行，掠过了整条山脊。",
    )
    assert allowed == [], "修为够高就不该被判冲突"

    denied = verify_claims(
        [],
        facts=facts,
        rules=[_rules()[1]],
        names=["林默"],
        text="林默修为不足，不能御剑飞行。",
    )
    assert denied == [], "否定句不是冲突"


def test_immutable_trait_rule_catches_rewritten_trait():
    verdicts = verify_claims(
        [],
        facts=[],
        rules=[_rules()[2]],
        names=[],
        text="他的灵根变成了火灵根，掌心一片滚烫。",
    )
    assert [item["verdict"] for item in verdicts] == ["CONFLICT"]


def test_possession_unique_rule_catches_two_swords():
    claims = [
        {"subject": "林默", "predicate": "本命灵剑", "object": "赤霄", "quote": "他的本命灵剑是赤霄。"},
        {"subject": "林默", "predicate": "本命灵剑", "object": "青霜", "quote": "他的本命灵剑是青霜。"},
    ]
    verdicts = verify_claims(
        claims,
        facts=[],
        rules=[_rules()[3]],
        names=["林默"],
        text="他的本命灵剑是赤霄。他的本命灵剑是青霜。",
    )
    conflicts = [item for item in verdicts if item["verdict"] == "CONFLICT"]
    assert len(conflicts) == 1
    assert "不可同时" in conflicts[0]["reason"]
    assert "赤霄" in conflicts[0]["claim"]["object"] and "青霜" in conflicts[0]["claim"]["object"]


# --------------------------------------------------------------------------- 声称核对（服务层）
def test_claim_verification_catches_planted_setting_conflict(session, novel, chapters):
    fact = claim_service.verify_text(
        session,
        novel,
        text="林默的佩剑是青云剑，剑身泛着淡青。",
        use_model=False,
        persist=False,
    )
    assert fact["conflict_count"] == 1
    conflict = fact["conflicts"][0]
    assert conflict["claim"]["object"] == "青云剑"
    assert conflict["evidence"][0]["kind"] == "CANON_FACT"
    assert conflict["evidence"][0]["text"]

    consistent = claim_service.verify_text(
        session,
        novel,
        text="林默的佩剑是青霜剑，剑身泛着淡青。",
        use_model=False,
        persist=False,
    )
    assert consistent["conflict_count"] == 0
    assert consistent["supported_count"] == 1


def test_claim_report_is_persisted_and_listed(session, novel, chapters):
    report = claim_service.verify_chapter(session, novel, chapters[3], use_model=False)
    session.commit()
    assert report["id"]
    assert report["chapter_number"] == 3
    listed = claim_service.list_reports(session, novel.id, chapter_number=3)
    assert listed and listed[0]["id"] == report["id"]
    assert listed[0]["claims"] == report["claims"]


def test_rule_conflict_shadows_the_unverified_twin(session, novel):
    """同一句话既被规则判冲突、又只是「查无此设定」时，只留冲突那条（少让作者看一遍）。"""
    text = "林默御剑飞行，掠过了整条山脊。"

    class _Stub:
        name = "stub"
        model = "stub-claims"
        kind = "llm"
        supports_tools = False

        def generate(self, request):  # noqa: ANN001
            payload = {
                "claims": [
                    {
                        "subject": "林默",
                        "predicate": "能力",
                        "object": "御剑飞行",
                        "kind": "ABILITY",
                        "quote": "林默御剑飞行，掠过了整条山脊",
                    }
                ],
                "unknown_entities": [],
                "summary": "",
            }
            return AIResponse(text="{}", parsed=payload, provider=self.name, model=self.model)

    verifier = ClaimVerifier(_Stub())  # type: ignore[arg-type]
    report = verifier.verify(session, novel, text, use_model=True)
    assert report["conflict_count"] == 1, report["conflicts"]
    assert report["unverified_count"] == 0, report["unverified"]
    conflict = report["conflicts"][0]
    assert "御剑" in conflict["reason"]
    assert conflict["claim"]["quote"] in text


def test_model_pass_drops_fabricated_quotes(session, novel):
    text = "林默的佩剑是青霜剑，剑身泛着淡青。"

    class _Stub:
        name = "stub"
        model = "stub-claims"
        kind = "llm"
        supports_tools = False

        def generate(self, request):  # noqa: ANN001
            payload = {
                "claims": [
                    {
                        "subject": "林默",
                        "predicate": "佩剑",
                        "object": "青云剑",
                        "kind": "SETTING",
                        "quote": "这句原文根本不存在于正文里",
                    },
                    {
                        "subject": "林默",
                        "predicate": "佩剑",
                        "object": "青霜剑",
                        "kind": "SETTING",
                        "quote": "林默的佩剑是青霜剑",
                    },
                ],
                "unknown_entities": ["青霜剑", "不存在的名字"],
                "summary": "",
            }
            return AIResponse(text="{}", parsed=payload, provider=self.name, model=self.model)

    verifier = ClaimVerifier(_Stub())  # type: ignore[arg-type]
    report = verifier.verify(session, novel, text, use_model=True)
    assert report["conflict_count"] == 0
    assert report["supported_count"] >= 1
    assert any("无法在正文里逐字核对" in warning for warning in report["warnings"])
    assert report["claim_count"] == 1, "编造的声称必须被丢弃"


# --------------------------------------------------------------------------- 接口
def test_claim_and_style_lock_api(client, novel, chapters):
    response = client.post(
        f"/api/novels/{novel.id}/claims/verify",
        json={"text": "林默的佩剑是青云剑。", "use_model": False},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["conflict_count"] == 1
    assert payload["conflicts"][0]["evidence"][0]["kind"] == "CANON_FACT"

    chapter_response = client.post(
        f"/api/novels/{novel.id}/claims/verify",
        json={"chapter_number": chapters[1].chapter_number, "use_model": False},
    )
    assert chapter_response.status_code == 200
    assert chapter_response.json()["chapter_number"] == chapters[1].chapter_number

    listed = client.get(f"/api/novels/{novel.id}/claims")
    assert listed.status_code == 200
    assert len(listed.json()) >= 2

    empty = client.post(f"/api/novels/{novel.id}/claims/verify", json={"text": "  "})
    assert empty.status_code == 400

    baseline = client.post(
        f"/api/novels/{novel.id}/style/baseline",
        json={"texts": [CLEAN_LONG_TEXT], "name": "接口基线", "make_default": True},
    )
    assert baseline.status_code == 200, baseline.text
    profile_id = baseline.json()["id"]

    locked = client.post(
        f"/api/novels/{novel.id}/style/baseline/{profile_id}/lock", json={"locked": True}
    )
    assert locked.status_code == 200
    assert locked.json()["locked"] is True

    drift = client.get(f"/api/novels/{novel.id}/style/drift")
    assert drift.status_code == 200
    assert drift.json()["locked"] is True
    assert drift.json()["chapters"]

    unlocked = client.post(
        f"/api/novels/{novel.id}/style/baseline/{profile_id}/lock", json={"locked": False}
    )
    assert unlocked.status_code == 200
    assert unlocked.json()["locked"] is False
