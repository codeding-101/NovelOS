"""真实 DeepSeek Flash 联调测试（默认跳过）。

运行方式：
    $env:RUN_LIVE=1; .venv\\Scripts\\python.exe -m pytest tests/test_live_deepseek.py -v
需要环境变量 DEEPSEEK_API_KEY。
"""

from __future__ import annotations

import os
import time

import pytest

from app.ai.base import AIError
from app.services import continuity_service, extraction_service, query_service

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE") != "1", reason="设置 RUN_LIVE=1 后才会调用真实模型"
    ),
]


def _extract(session, novel, chapter, *, provider: str = "deepseek"):
    """真实模型偶发限流/超时不应当被当成代码缺陷：重试一次，仍失败则跳过并说明原因。"""
    try:
        return extraction_service.run_extraction(
            session, novel, chapter, provider_name=provider
        )
    except AIError as exc:
        time.sleep(3)
        try:
            return extraction_service.run_extraction(
                session, novel, chapter, provider_name=provider
            )
        except AIError as retry_exc:
            pytest.skip(f"外部模型暂时不可用（非代码问题）：{retry_exc or exc}")


def test_live_extraction_and_continuity_on_chapter_15(session, novel, chapters):
    """真实模型应能抽出赵铁山本周的动作，并配合硬规则报出「死者复现」。"""
    chapter = chapters[15]
    result, run, warnings, provider, model = _extract(session, novel, chapter)
    session.commit()
    assert provider == "deepseek"
    assert model
    if run is not None and not run.items:
        # 模型这一次没读进去（偶发），重试一次；仍为空则如实跳过，不掩盖代码问题
        result, run, warnings, provider, model = _extract(session, novel, chapter)
        session.commit()
        if not run.items:
            pytest.skip(f"模型两次都没有产出可审阅条目（模型方差）：{warnings}")
    assert run is not None and run.items, "抽取结果应落为待审项"
    assert result.new_facts or result.characters_changed, "真实模型应抽出结构化信息"

    report, _ = continuity_service.run_check(
        session, novel, chapter, provider_name="deepseek", extraction=result
    )
    session.commit()
    codes = {issue.code for issue in report.errors}
    assert "DEAD_CHARACTER_ACTIVE" in codes, f"应报出死者复现，实际：{codes}"
    for issue in report.errors + report.warnings:
        assert all(evidence.source_chapter for evidence in issue.evidence)


def test_live_attribute_conflict_on_chapter_14(session, novel, chapters):
    chapter = chapters[14]
    result, _, _, _, _ = _extract(session, novel, chapter)
    report, _ = continuity_service.run_check(
        session, novel, chapter, provider_name="deepseek", extraction=result
    )
    session.commit()
    assert "FACT_CONFLICT" in {issue.code for issue in report.errors}


def test_live_memory_answer_cites_evidence(session, novel):
    memory = extraction_service.build_agents("deepseek")["memory"]
    response = memory.ask(session, novel, "林默什么时候第一次见到王烈？")
    session.commit()
    assert response.provider == "deepseek"
    assert response.answer and "三月十五" in response.answer
    allowed = {item.chapter_number for item in response.evidence}
    assert 2 in allowed


def test_live_writer_grounded_in_canon(session, novel):
    from app.schemas import WriteChapterRequest

    writer = extraction_service.build_agents("deepseek")["writer"]
    response = writer.write(
        session,
        novel,
        WriteChapterRequest(
            goals="林默在青云镇暗中跟踪一名血河教教徒",
            must_include=["青霜剑"],
            forbidden=["赤霄剑"],
            characters=["林默"],
            chapter_number=21,
            target_words=700,
        ),
    )
    session.commit()
    assert response.provider == "deepseek"
    assert response.draft.word_count > 300
    assert "青霜剑" in response.draft.content
    assert "赤霄剑" not in response.draft.content
    assert response.retrieved.canon_facts
    assert all(
        fact["status"] == "CANON" for fact in response.retrieved.canon_facts
    )


def test_live_proposed_never_becomes_canon(session, novel, chapters):
    """关键不变量：无论模型抽到多少，Canon 都不能被自动改写。"""
    _extract(session, novel, chapters[14])
    session.commit()
    canon = query_service.list_canon_facts(session, novel.id, subject="林默", predicate="佩剑")
    assert [fact["object"] for fact in canon] == ["青霜剑"], "抽取阶段绝不能改动 Canon"

    proposed = query_service.list_canon_facts(session, novel.id, status="PROPOSED", limit=200)
    assert all(fact["status"] == "PROPOSED" for fact in proposed)
    if not proposed:
        pytest.skip("本次模型输出没有产生新事实（模型方差），Canon 未被改写这一不变量已验证")


def test_live_planner_produces_usable_plans(session, novel):
    """真实模型规划：应引用既有伏笔与人物，且不把已死亡人物安排成出场。"""
    from app.schemas import PlanGenerateRequest
    from app.services import plan_service

    result = plan_service.generate_plans(
        session,
        novel,
        PlanGenerateRequest(
            from_chapter=21, count=3, steer="把师父的旧事往前推一步", provider="deepseek"
        ),
    )
    session.commit()
    assert result["provider"] == "deepseek"
    assert len(result["plans"]) == 3
    for plan in result["plans"]:
        assert plan.goals, "每章都要有目标"
        assert plan.title, "每章都要有标题"
        assert "赵铁山" not in plan.characters, "已死亡人物不应作为出场人物"
    joined = " ".join(
        " ".join(plan.advance_foreshadowing) + plan.goals + plan.title for plan in result["plans"]
    )
    assert any(
        keyword in joined
        for keyword in ("伏笔", "青霜", "断魂崖", "阁主", "残卷", "血河", "父亲")
    ), "计划应体现对既有伏笔/主线的推进"


def test_live_agent_mode_uses_tools(session, novel):
    """agent 模式：模型自己决定调用工具（若模型不支持工具调用，会退回 simple 并给出 warning）。"""
    from app.services import extraction_service as es

    memory = es.build_agents("deepseek")["memory"]
    response = memory.ask_agent(session, novel, "林默的师父是谁？他是什么修为？")
    session.commit()
    assert response.provider == "deepseek"
    assert response.answer
    if response.mode == "agent":
        assert response.tool_calls, "agent 模式应记录工具调用"
        assert any(call["name"] for call in response.tool_calls)
    else:  # 模型不支持工具时应显式回退，而不是静默失败
        assert any("回退" in warning or "工具" in warning for warning in response.warnings)


def test_live_sweep_reports_planted_conflicts(session, novel):
    from app.schemas import SweepRequest
    from app.services import sweep_service

    sweep = sweep_service.run_sweep(
        session,
        novel,
        SweepRequest(mode="full", provider="deepseek", chapter_numbers=[15, 18]),
    )
    session.commit()
    assert sweep.chapters_checked == 2
    by_chapter = {item["chapter_number"]: item for item in sweep.detail["chapters"]}
    assert "DEAD_CHARACTER_ACTIVE" in by_chapter[15]["codes"]
    assert "TIMELINE_INVERSION" in by_chapter[18]["codes"]
    # full 模式对每一章都尝试过抽取；模型偶尔抽不出东西时也要有明确交代，而不是静默跳过
    assert by_chapter[15]["extraction"] in ("model", "none")
    if by_chapter[15]["extraction"] == "none":
        assert by_chapter[15]["note"] and "抽取失败" in by_chapter[15]["note"]


def test_live_hybrid_retrieval_and_as_of(session, novel):
    from app.services import retrieval_service

    result = retrieval_service.hybrid_search(session, novel, "林默的佩剑", limit=8)
    assert result["hits"]
    assert result["hits"][0]["ref_type"] == "CANON_FACT"
    assert "vector" in result["channels"]

    facts = query_service.canon_as_of(session, novel.id, 6)
    assert all(fact["status"] != "PROPOSED" for fact in facts)
    assert not any(fact["subject"] == "赵铁山" and fact["predicate"] == "状态" for fact in facts)


#: 明显带「AI 味」的样段：套话密集、句长均齐、全篇旁白
AI_FLAVOR_SAMPLE = (
    "李青眼中闪过一丝寒芒，嘴角勾起一抹冷笑。他缓缓地抬起手，淡淡地说了一句什么。"
    "苏婉心中一惊，瞳孔微缩，不由得后退半步。空气中弥漫着难以言喻的气息，"
    "那股杀意让整个房间的气氛变得无比压抑。李青深吸一口气，若有所思地看着她，"
    "意味深长地点了点头。苏婉不禁心头一震，眼中闪过一丝复杂的情绪，"
    "她不明白他为什么这样看着她，她知道自己必须问个明白，"
    "她也知道现在不是问的时候，她更知道再等下去只会更难开口。"
    "李青不动声色地转过身，仿佛一切都在他的掌握之中。苏婉如释重负，"
    "却又有一种说不清道不明的感觉在心里盘旋，让她几乎无法呼吸，"
    "她想开口，却发现嗓子干得发不出声音，于是她只好把话都咽了回去。"
    "这一夜，注定无眠。"
)


def test_live_style_critique_quotes_exist_in_text(session, novel):
    """真实模型的读感意见必须能在原文里逐字找到出处，否则会被丢弃。"""
    from app.services import style_service

    critic = extraction_service.build_agents("deepseek")["style"]
    report = critic.review(
        session, novel, AI_FLAVOR_SAMPLE, use_model=True, persist=False, label="live-sample"
    )
    session.commit()
    assert report["provider"] == "deepseek"
    assert report["score"] < 95, "明显堆套话的段落不该接近满分"
    codes = {issue["code"] for issue in report["issues"]}
    assert codes & {"CLICHE_DENSE", "FLAT_RHYTHM", "TELLING_DENSE", "ABSTRACT_DENSE", "SELF_REPEAT"}, (
        f"规则层应至少报出一类问题，实际：{codes}"
    )
    for issue in report["issues"]:
        if issue["metric"] == "model":
            assert issue["excerpt"] in AI_FLAVOR_SAMPLE, "模型意见的摘录必须在正文里"
    assert style_service.measure(AI_FLAVOR_SAMPLE).cliche_per_1k > 5


def test_live_rewrite_reduces_ai_flavor(session, novel):
    """改稿是否真的降低了 AI 味：用指标对比说话，而不是靠感觉。

    判据只用「AI 味相关指标不得变差」+「节奏不得塌到底」：
    burstiness 单看会误导 —— 把长句拆短本身会降低 burstiness，但那正是我们要的改法。
    """
    from app.services import style_service

    critic = extraction_service.build_agents("deepseek")["style"]
    before = style_service.measure(AI_FLAVOR_SAMPLE)
    report = critic.review(session, novel, AI_FLAVOR_SAMPLE, use_model=True, persist=False)
    revised, warnings, provider, _model = critic.rewrite(
        session,
        novel,
        AI_FLAVOR_SAMPLE,
        report["issues"],
        goals="李青与苏婉的对峙",
        instructions="保留两人的对峙情节，只修改文风问题",
    )
    if revised.strip() == AI_FLAVOR_SAMPLE.strip():
        # 模型这一次把原稿原样退回（偶发）：再要一次，仍如此就如实跳过
        revised, warnings, provider, _model = critic.rewrite(
            session,
            novel,
            AI_FLAVOR_SAMPLE,
            report["issues"],
            goals="李青与苏婉的对峙",
            instructions="上一版原样退回，没有做任何修改；请务必按问题清单重写，套话与抽象词必须删掉",
        )
        if revised.strip() == AI_FLAVOR_SAMPLE.strip():
            pytest.skip("模型两次都把原稿原样退回（模型方差），改稿指标无法评估")
    session.commit()
    assert provider == "deepseek"
    assert revised.strip() and revised != AI_FLAVOR_SAMPLE
    after = style_service.measure(revised)
    comparison = style_service.compare_metrics(before.to_dict(), after.to_dict())

    # 1) AI 味指标一律不许变差
    assert after.cliche_per_1k <= before.cliche_per_1k, (
        f"套话密度不应上升：{before.cliche_per_1k} → {after.cliche_per_1k}"
    )
    assert after.abstract_per_1k <= before.abstract_per_1k + 0.5, (
        f"抽象词密度不应上升：{before.abstract_per_1k} → {after.abstract_per_1k}"
    )
    assert after.long_sentence_ratio <= before.long_sentence_ratio, (
        f"长句占比不应上升：{before.long_sentence_ratio} → {after.long_sentence_ratio}"
    )
    assert after.self_repeat_ratio <= before.self_repeat_ratio + 0.01, (
        f"章内复读不应上升：{before.self_repeat_ratio} → {after.self_repeat_ratio}"
    )
    # 2) 至少要有实打实的改善（套话清零或对白增加），而不是原地不动
    assert (
        after.cliche_per_1k < before.cliche_per_1k
        or after.dialogue_ratio > before.dialogue_ratio
        or comparison["improved"] >= 2
    ), f"改稿没有带来任何实质改善：{comparison}"
    # 3) 节奏不能塌成一种句式（短句堆砌同样是毛病）
    assert after.burstiness >= 0.35, f"改后句长变化过小：{after.burstiness}"
    assert abs(after.total_chars - before.total_chars) / max(before.total_chars, 1) < 0.6, (
        "改稿不应大改篇幅"
    )
    assert comparison["improved"] >= comparison["worsened"] - 1, comparison["deltas"]


def test_live_revision_loop_end_to_end(session, novel):
    """整条闭环跑通：写作 → 评审 → 改稿 → 复评，并记录每一轮指标。"""
    from app.schemas import WriteChapterRequest

    loop = extraction_service.build_agents("deepseek")["revision"]
    result = loop.run(
        session,
        novel,
        WriteChapterRequest(
            goals="林默在废祠外第一次正面遭遇血河教教徒，动手后发现自己下手比预想更狠",
            must_include=["废祠"],
            characters=["林默"],
            chapter_number=21,
            target_words=700,
        ),
        max_rounds=1,
        target_score=88.0,
        use_model_critic=True,
    )
    session.commit()
    assert result.rounds and result.rounds[0].stage == "draft"
    assert result.draft.content and "废祠" in result.draft.content
    assert result.provider == "deepseek"
    assert result.metric_deltas["deltas"], "闭环必须留下指标对比"
    assert all(round_row.word_count > 0 for round_row in result.rounds)
    if len(result.rounds) > 1:
        assert result.rounds[-1].stage in ("revised", "no-change", "reverted")


#: 风格很个人的碎片：口语、断句、意象都是作者的，不是模型的
AUTHOR_FRAGMENTS = [
    "他把刀口在袖子上擦了擦，没擦干净。血是热的，风是冷的，他站了一会儿，什么都没想。",
    "她想，如果那天她多说一句话，是不是就不用在这里等人了。",
    "屋檐下的冰凌化了半截，滴在他鞋面上，一滴，又一滴。",
    "——他后来才知道，那封信是假的。但那时候他已经把刀收起来了。",
]


def test_live_realize_keeps_author_fragments(session, novel):
    """碎片成文：作者的原话与意象必须留痕，且必须如实报告没用上的部分。"""
    from app.schemas import FragmentCreate, FragmentRealizeRequest
    from app.services import fragment_service, realize_service

    created = [
        fragment_service.create_fragment(session, novel, FragmentCreate(text=text, kind="SCENE"))
        for text in AUTHOR_FRAGMENTS
    ]
    session.commit()
    response = realize_service.realize_fragments(
        session,
        novel,
        FragmentRealizeRequest(
            fragment_ids=[fragment.id for fragment in created],
            goals="把这几段作者的想法写成一场戏，保留他的断句与语气",
            tone="冷、平静、克制",
            chapter_number=21,
            target_words=700,
            provider="deepseek",
        ),
    )
    session.commit()

    realization = response.realization
    assert realization.content.strip(), "应产出正文"
    assert response.provider == "deepseek"
    assert realization.coverage["fragments_used"] >= 2, (
        f"至少要展开两条碎片：{realization.coverage}"
    )
    # 逐条对应关系都能在正文里找到，且引用的原话确实来自碎片
    for passage in realization.passages:
        assert passage.prose.strip() in realization.content
        if passage.uses_quote:
            assert any(passage.uses_quote in text for text in AUTHOR_FRAGMENTS), (
                f"引用说明必须来自作者碎片：{passage.uses_quote}"
            )
    quoted_or_kept = [
        passage
        for passage in realization.passages
        if passage.treatment in ("QUOTED", "PARAPHRASED") and passage.fragment_id != "__bridge__"
    ]
    assert quoted_or_kept, "作者的碎片必须出现在对应表里"
    # 作者的语言痕迹要留一些：至少命中一个他惯用的说法
    traces = ["擦了擦", "什么都没想", "一滴，又一滴", "那时候他", "是不是就"]
    assert any(trace in realization.content for trace in traces), (
        "成文里应保留作者的原话或明显化用"
    )
    # 没用上的碎片要如实报告，而不是含糊过去
    assert realization.coverage["unused_ids"] is not None
    if realization.undeveloped:
        assert all("fragment_id" in item for item in realization.undeveloped)
    # 若模型引入了新设定，必须被规则抓出来
    assert isinstance(realization.invented_claims, list)
    assert response.voice == {} or "score" in response.voice


def test_live_rewrite_preserves_author_voice(session, novel):
    """改稿可以压套话，但不能把作者的味道抹平（这是 V0.4 的硬约束）。"""
    from app.services import style_service

    style_service.save_voice_profile(
        session, novel, AUTHOR_FRAGMENTS, name="作者声音（联调）"
    )
    session.commit()
    profile = style_service.default_voice_profile(session, novel.id)
    assert profile is not None

    text = (
        "他把刀口在袖子上擦了擦，没擦干净。血是热的，风是冷的，他站了一会儿，什么都没想。"
        "屋檐下的冰凌化了半截，滴在他鞋面上，一滴，又一滴。他忽然想，如果那天她多说一句话，"
        "是不是就不用在这里等人了。"
    )
    before = style_service.voice_score(text, profile)
    assert before["available"] and before["score"] is not None

    critic = extraction_service.build_agents("deepseek")["style"]
    report = critic.review(session, novel, text, use_model=True, persist=False)
    revised, warnings, provider, _model = critic.rewrite(
        session,
        novel,
        text,
        report["issues"],
        goals="他等人",
        instructions="只处理指出的文风问题；这是作者本人的文字，务必保留他的用词与断句习惯",
    )
    session.commit()
    if revised.strip() == text.strip():
        pytest.skip("模型这次原样退回（模型方差），声音保留无法评估")
    after = style_service.voice_score(revised, profile)
    assert after["score"] is not None
    assert after["score"] >= before["score"] - 8, (
        f"改稿把作者声音磨掉了：{before['score']} → {after['score']}"
    )
    assert len(after["signature_hits"]) >= max(1, len(before["signature_hits"]) - 2), (
        f"作者特征词被抹掉太多：{before['signature_hits']} → {after['signature_hits']}"
    )


# --------------------------------------------------------------------------- V0.5
def test_live_claim_verification_catches_planted_hallucination(session, novel, chapters):
    """真实模型把正文拆成断言后，植入的设定冲突要被逐条抓出来（带原文与 Canon 证据）。"""
    from app.services import claim_service

    planted = (
        "林默把剑横在膝上。这柄剑通体青云，是他从断魂崖下捡回来的。\n"
        "“师父教的第一式，是‘守’。”他低声说。\n"
        "赵铁山提剑迎上前去，白发在风里散开。\n"
        "林默御剑飞行，掠过了整条山脊。\n"
    )
    try:
        report = claim_service.verify_text(
            session, novel, text=planted, use_model=True, persist=False, provider_name="deepseek"
        )
    except AIError as exc:
        pytest.skip(f"外部模型暂时不可用（非代码问题）：{exc}")
    session.commit()

    assert report["provider"] == "deepseek"
    assert report["claim_count"] >= 2, f"应拆出多条断言，实际 {report['claim_count']}"
    assert report["conflict_count"] >= 2, f"植入的冲突未被抓到：{report['conflicts']}"
    reasons = " ".join(item["reason"] for item in report["conflicts"])
    assert "死亡" in reasons or "复生" in reasons, f"死者行动未被判出：{reasons}"
    assert "御剑" in reasons, f"能力门槛未被判出：{reasons}"
    for item in report["conflicts"]:
        assert item["reason"]
        assert item["evidence"], "每条冲突都必须带证据"
        assert item["claim"].get("quote"), "每条冲突都要指到原文片段"


def test_live_claim_verification_keeps_invention_as_unverified(session, novel, chapters):
    """查无此设定的新信息只能是「待确认」，不能升级成 Canon，也不能算冲突。"""
    from app.services import claim_service

    invented = "苏月宁的佩剑是碎星，剑鞘上刻着天机的星图。她把它挂在了腰间。\n"
    try:
        report = claim_service.verify_text(
            session, novel, text=invented, use_model=True, persist=False, provider_name="deepseek"
        )
    except AIError as exc:
        pytest.skip(f"外部模型暂时不可用（非代码问题）：{exc}")
    session.commit()

    if report["claim_count"] == 0:
        pytest.skip("模型这次没有拆出断言（模型方差）")
    assert report["conflict_count"] == 0, f"新设定不该被判成冲突：{report['conflicts']}"
    assert report["unverified_count"] >= 1
    assert any(
        "碎星" in item["claim"].get("object", "") or "佩剑" in item["claim"].get("predicate", "")
        for item in report["unverified"]
    ), f"新设定应进入待确认：{report['unverified']}"


def test_live_write_follows_locked_style_and_world_rules(session, novel, chapters):
    """锁定文风 + 世界观规则要真的进入写作提示，生成结果再过一遍断言核对。"""
    from app.schemas import WriteChapterRequest
    from app.services import claim_service, style_service

    style_service.save_profile(
        session,
        novel,
        [chapter.content or "" for chapter in list(chapters.values())[:4]],
        name="联调锁定基线",
        make_default=True,
    )
    baseline = style_service.default_profile(session, novel.id)
    assert baseline is not None
    style_service.lock_profile(session, baseline, True)
    session.commit()
    assert baseline.locked is True

    writer = extraction_service.build_agents("deepseek")["writer"]
    request = WriteChapterRequest(
        goals="林默与王烈在青云镇外埋伏血河教的人，遭遇一次小而具体的交手",
        characters=["林默", "王烈"],
        target_words=600,
        chapter_number=21,
    )
    try:
        response = writer.write(session, novel, request)
    except AIError as exc:
        pytest.skip(f"外部模型暂时不可用（非代码问题）：{exc}")
    session.commit()

    assert any("已锁定的文风" in warning for warning in response.warnings), response.warnings
    assert any("世界观规则" in warning for warning in response.warnings), response.warnings
    body = response.draft.content or ""
    assert len(body) >= 300

    try:
        report = claim_service.verify_text(
            session, novel, text=body, use_model=True, persist=False, provider_name="deepseek"
        )
    except AIError as exc:
        pytest.skip(f"外部模型暂时不可用（非代码问题）：{exc}")
    session.commit()
    for item in report["conflicts"]:
        assert item["evidence"], "冲突必须带证据（Canon 事实或世界观规则）"
        assert item["claim"].get("quote") in body, "证据片段必须能在正文里逐字找到"


# --------------------------------------------------------------------------- V0.6
SUMMARY_HEAVY_TEXT = (
    "他收好剑，走出了客栈。这一切，终于结束了。\n"
    "从那以后，他再也没有回过青云镇。往后的路，他一个人走。\n"
    "掌柜站在门口看了他很久，最后什么也没说，把手里的抹布搭在肩上进了屋。\n"
    "他终于明白，有些事不是靠一把剑就能解决的。说到底，人总得学会低头。\n"
    "街上有个卖糖的摊子，铜锅里冒着白气，甜味混着雨腥味往鼻子里钻。他站在摊前看了两眼，"
    "摸出两枚铜钱，买了一包，揣进怀里。\n"
    "这一夜注定无眠。多年以后他才知道，那晚的选择改变了所有事情的一切。\n"
)


def test_live_rewrite_cuts_summary_and_elevation(session, novel, chapters):
    """真实改稿：把总结与升华句删掉，同时不能把用词改成生僻字（不追求困惑度）。"""
    from app.services import style_service

    style_service.save_profile(
        session,
        novel,
        [chapter.content or "" for chapter in list(chapters.values())[:4]],
        name="V0.6 基线",
        make_default=True,
    )
    session.commit()
    before_metrics = style_service.measure(SUMMARY_HEAVY_TEXT)
    assert before_metrics.summary_per_1k > 2.5, "构造的样稿应当确实有总结句"

    critic = extraction_service.build_agents("deepseek")["style"]
    try:
        report = critic.review(session, novel, SUMMARY_HEAVY_TEXT, use_model=True, persist=False)
        codes = {issue["code"] for issue in report["issues"]}
        assert {"SUMMARY_ENDING", "ELEVATION_DENSE"} <= codes, (
            f"规则层必须抓到总结与升华句，实际：{sorted(codes)}"
        )
        revised, warnings, _provider, _model = critic.rewrite(
            session,
            novel,
            SUMMARY_HEAVY_TEXT,
            report["issues"],
            goals="他在客栈遇袭后离开青云镇",
            instructions="删掉总结与升华的句子，抽象处换成看得见的东西；不要换成生僻字",
        )
    except AIError as exc:
        pytest.skip(f"外部模型暂时不可用（非代码问题）：{exc}")
    session.commit()
    if revised.strip() == SUMMARY_HEAVY_TEXT.strip():
        # 声音护栏（改稿把作者特征磨掉就回退）与模型原样退回都会走到这里
        pytest.skip(f"改稿未落地（护栏拦截或模型原样退回）：{warnings}")

    after_metrics = style_service.measure(revised)
    assert after_metrics.summary_per_1k <= before_metrics.summary_per_1k, (
        f"总结句没有减少：{before_metrics.summary_per_1k} → {after_metrics.summary_per_1k}"
    )
    assert after_metrics.elevation_per_1k <= before_metrics.elevation_per_1k, (
        f"升华句没有减少：{before_metrics.elevation_per_1k} → {after_metrics.elevation_per_1k}"
    )
    assert after_metrics.novel_char_ratio <= style_service.NOVEL_CHAR_CAP, (
        "改稿不该靠堆生僻字来制造「人味」"
    )
    assert after_metrics.word_ttr >= before_metrics.word_ttr - 0.05, "用词多样性不该被改稿压塌"


def test_live_critic_flags_summary_and_safe_wording(session, novel, chapters):
    """模型的读感评审应当自己点名「总结式收尾」这类问题（引用必须逐字来自正文）。"""
    from app.ai import prompts
    from app.ai.agents.style_critic import StyleModelResult
    from app.ai.base import AIRequest
    from app.ai.json_utils import extract_json

    agents = extraction_service.build_agents("deepseek")
    provider = agents["provider"]
    request = AIRequest(
        task="style_review",
        system=prompts.STYLE_SYSTEM,
        prompt=prompts.STYLE_USER.format(
            baseline="（联调用例：只关注用词与收束）",
            rule_issues="（规则层结果已省略，请独立判断）",
            content=SUMMARY_HEAVY_TEXT,
        ),
        context={"novel_id": novel.id, "content": SUMMARY_HEAVY_TEXT},
        json_schema=StyleModelResult.model_json_schema(),
        temperature=0.2,
        max_tokens=4096,
    )
    try:
        response = provider.generate(request)
    except AIError as exc:
        pytest.skip(f"外部模型暂时不可用（非代码问题）：{exc}")
    parsed = extract_json(response.text or "")
    if not isinstance(parsed, dict) or not parsed.get("issues"):
        pytest.skip(
            f"模型这次没有给出可解析的意见（模型方差／输出被截断）：{response.warnings}"
            f"｜{(response.text or '')[:200]}"
        )

    quotes = [str(item.get("quote") or "") for item in parsed["issues"]]
    assert any(quote and quote in SUMMARY_HEAVY_TEXT for quote in quotes), (
        f"引用必须逐字来自正文：{quotes[:3]}"
    )
    pointed = [
        quote
        for quote in quotes
        if any(marker in quote for marker in ("这一切", "多年以后", "他终于明白", "说到底", "从那以后"))
    ]
    assert pointed, f"模型没有点名总结／升华句：{quotes[:5]}"

