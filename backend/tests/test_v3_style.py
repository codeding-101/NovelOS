"""V0.3 文风层：指标计算、基线对比、规则审查、确定性改稿。"""

from __future__ import annotations

from app.ai.agents.style_critic import StyleCritic
from app.services import extraction_service, style_service

UNIFORM_TEXT = "他缓缓地抬起头。\n\n她淡淡地说了一句话。\n\n他深深地看了她一眼。\n\n她冷冷地转过身去。\n\n他不由得握住剑柄。\n\n她微微颔首表示同意。\n\n他深吸一口气。\n\n她沉默了片刻。\n"
VARIED_TEXT = """雨点砸在瓦上。
他翻过院墙，落地时左膝一软，疼得眼前发黑。
“谁？”阴影里有人问。
他没有答。刀锋已经从左侧劈过来，他侧身让开半步，反手一剑捅进那人肋下。
那人闷哼一声，倒下了。血顺着砖缝流到他脚边，热的。
远处灯笼晃了晃。还有三个人。"""
AI_FLAVOR_TEXT = (
    "他眼中闪过一丝寒芒，嘴角勾起一抹冷笑。他缓缓地抬起手，淡淡地说了一句。"
    "她心中一惊，瞳孔微缩，不由后退半步。空气中弥漫着难以言喻的气息，"
    "那股杀意让气氛变得无比压抑。他深吸一口气，若有所思地看着她，"
    "意味深长地点了点头。她不禁心头一震，眼中闪过一丝复杂的情绪。"
    "他不动声色地转过身，仿佛一切都在他的掌握之中。她如释重负，"
    "却又有一种说不清道不明的感觉。这一夜，注定无眠。"
)
#: 干净的范文：句式、标点、情绪都有落差，用来验证规则不会对正常稿子乱报警。
#: 注意不能用同一段文字重复拼接——那本身就构成复读与句式循环。
CLEAN_LONG_TEXT = """雨点砸在瓦上，密得像有人在外头撒豆子。

他翻过院墙，落地时左膝一软，疼得眼前发黑。阴影里有人问了一句什么。

刀锋已经从左侧劈过来！他侧身让开半步，反手一剑捅进那人肋下。那人闷哼一声，倒了。血顺着砖缝流到他脚边，热的。

远处灯笼晃了晃。还有三个人——三个都握着刀。

“往东门走，”妇人压低声音，“西边那条巷子昨夜就封了。”

他弯腰下去，从砖缝里把那半截断剑抠了出来。断口很新，边缘还有毛刺。

父亲说过半句话：剑不可轻出，出则——

后面被涂死了。他盯着那断口看了很久，直到血从指缝里滴下来。

脚步声到了墙外。三个人，一个比一个重。他数着，数到第三下就动了手。

门被撞开的时候，他已经站在门后。第一刀落空，第二刀撞在断剑上，火星溅了一脸。"""


def test_measure_is_pure_and_deterministic():
    first = style_service.measure(AI_FLAVOR_TEXT).to_dict()
    second = style_service.measure(AI_FLAVOR_TEXT).to_dict()
    assert first == second, "文风指标必须是可复现的纯函数"


def test_uniform_text_has_low_burstiness_and_high_cliche():
    uniform = style_service.measure(UNIFORM_TEXT)
    ai_flavor = style_service.measure(AI_FLAVOR_TEXT)
    varied = style_service.measure(VARIED_TEXT)
    monotone = style_service.measure("他眼中闪过一丝寒芒，嘴角勾起一抹冷笑。" * 6)

    assert uniform.burstiness < 0.35, "句长几乎一致的段落不应该有高的句长变异"
    assert varied.burstiness > uniform.burstiness, "长短交错的文字节奏更明显"
    assert ai_flavor.cliche_per_1k > 20, "密集套话应被计入"
    assert monotone.cliche_variety < 0.4, "同一句话反复用，套话种类必然很少"
    assert len(ai_flavor.cliche_hits) >= 5, "不同套话应被分别统计"
    assert uniform.hook_score < 0.5
    assert varied.dialogue_ratio > 0
    assert uniform.self_repeat_ratio >= 0


def test_hook_detection():
    with_question, signals = style_service._hook("他站在门口，没有说话。\n\n那封信，是谁送来的？")
    assert with_question >= 0.5 and "章末疑问" in signals
    flat, _ = style_service._hook("他把剑收回鞘里，然后回屋睡下了。")
    assert flat < with_question
    truncated, signals_cut = style_service._hook("他抬头看去——")
    assert truncated > 0 and "句子被截断" in signals_cut


def test_self_repeat_detection():
    text = "他握紧了剑柄，看着对面的敌人。" * 6
    metrics = style_service.measure(text)
    assert metrics.self_repeat_ratio > 0.2, "整段复读必须被识别"
    fresh = style_service.measure(VARIED_TEXT)
    assert fresh.self_repeat_ratio < 0.05


def test_review_flags_ai_flavor_text():
    report = style_service.review_text(AI_FLAVOR_TEXT)
    codes = {issue["code"] for issue in report["issues"]}
    assert report["score"] < 70, "堆套话的文字不该拿到高分"
    assert "CLICHE_DENSE" in codes
    assert any(code in codes for code in ("ABSTRACT_DENSE", "TELLING_DENSE"))
    cliche_issue = next(issue for issue in report["issues"] if issue["code"] == "CLICHE_DENSE")
    assert cliche_issue["suggestion"], "每条问题都要给出可执行的改法"
    assert report["metrics"]["cliche_hits"], "要能列出生频最高的套话"


def test_review_of_clean_text_has_few_issues():
    report = style_service.review_text(CLEAN_LONG_TEXT)
    warnings = [issue for issue in report["issues"] if issue["level"] == "warning"]
    assert len(warnings) <= 1, f"干净文本不应被大量告警：{[i['code'] for i in warnings]}"
    assert report["score"] >= 85


def test_baseline_and_relative_threshold(session, novel, chapters):
    profile = style_service.profile_from_chapters(session, novel)
    session.commit()
    assert profile is not None
    assert profile.sample_count >= 3
    assert profile.metrics["burstiness"]["mean"] > 0
    assert style_service.default_profile(session, novel.id).id == profile.id

    drifting = "他缓缓地抬起头，眼中闪过一丝复杂的神色。" * 12
    with_baseline = style_service.review_text(drifting, profile=profile)
    without = style_service.review_text(drifting)
    assert with_baseline["baseline"] == profile.name
    with_codes = {issue["code"] for issue in with_baseline["issues"]}
    without_codes = {issue["code"] for issue in without["issues"]}
    assert "CLICHE_DENSE" in with_codes, "明显偏离本书基线的套话密度应被抓到"
    assert "CLICHE_DENSE" in without_codes


def test_baseline_needs_enough_samples(session, novel, chapters):
    from app.schemas import ChapterCreate
    from app.services import chapter_service

    for number in range(1, 21):
        chapter = session.get(type(chapters[1]), chapters[number].id)
        chapter_service.delete_chapter(session, novel, chapter)
    session.commit()
    chapter_service.create_chapter(
        session, novel, ChapterCreate(chapter_number=1, title="唯一一章", content="短。" * 50)
    )
    session.commit()
    assert style_service.profile_from_chapters(session, novel) is None, "样本不足时不建基线"


def test_compare_metrics_reports_direction():
    before = style_service.measure(AI_FLAVOR_TEXT).to_dict()
    after = style_service.measure(VARIED_TEXT * 3).to_dict()
    comparison = style_service.compare_metrics(before, after)
    assert comparison["deltas"]["cliche_per_1k"]["better"] is True, "套话下降应判为改善"
    assert comparison["deltas"]["abstract_per_1k"]["better"] is True
    # burstiness 是区间指标：改稿把长句拆短会让它下降，那不算「变差」
    assert style_service.METRIC_DIRECTIONS["burstiness"] == "range"
    assert comparison["deltas"]["burstiness"]["better"] is None
    assert comparison["improved"] > 0


def test_compare_metrics_treats_no_change_as_neutral():
    """没有变化的指标既不算改善也不算变差（否则「持平」会被显示成退步）。"""
    metrics = style_service.measure(AI_FLAVOR_TEXT).to_dict()
    comparison = style_service.compare_metrics(metrics, metrics)
    assert comparison["improved"] == 0 and comparison["worsened"] == 0
    assert all(item["delta"] == 0 for item in comparison["deltas"].values())
    assert all(item["better"] is None for item in comparison["deltas"].values())


def test_offline_rewriter_removes_filler_and_splits_sentences(session, novel):
    critic = extraction_service.build_agents("offline")["style"]
    report = style_service.review_text(AI_FLAVOR_TEXT)
    revised, warnings, provider, _model = critic.rewrite(
        session, novel, AI_FLAVOR_TEXT, report["issues"], goals="测试改稿"
    )
    assert provider == "offline"
    after = style_service.measure(revised)
    before = style_service.measure(AI_FLAVOR_TEXT)
    assert after.cliche_per_1k <= before.cliche_per_1k
    assert "缓缓地" not in revised and "淡淡地" not in revised
    assert "深吸一口气" not in revised
    assert warnings, "离线改稿要说明它做不到的部分"


def test_offline_rewriter_splits_long_sentences(session, novel):
    long_text = "他沿着山路一直往前赶，风从耳边刮过，脚下的碎石不断往下滚落，直到天色发白才停下脚步。"
    before = style_service.measure(long_text)
    assert before.long_sentence_ratio == 1.0, "这段确实只有一个超长句"
    critic = extraction_service.build_agents("offline")["style"]
    issues = [
        {
            "code": "LONG_SENTENCE_DENSE",
            "metric": "long_sentence_ratio",
            "message": "长句占比偏高",
            "level": "warning",
            "excerpt": "",
            "suggestion": "拆句",
        }
    ]
    revised, _, _, _ = critic.rewrite(session, novel, long_text, issues)
    after = style_service.measure(revised)
    assert after.sentence_count > before.sentence_count, "超长句应被断开"
    assert after.long_sentence_ratio < before.long_sentence_ratio
    assert "。" in revised


def test_style_review_persists_history(session, novel, chapters):
    critic = extraction_service.build_agents("offline")["style"]
    critic.review(
        session,
        novel,
        chapters[6].content,
        use_model=True,
        persist=True,
        chapter=chapters[6],
        label="chapter",
    )
    session.commit()
    from sqlalchemy import select

    from app.models import StyleReview

    rows = list(session.scalars(select(StyleReview).where(StyleReview.novel_id == novel.id)))
    assert rows and rows[-1].chapter_number == 6
    assert isinstance(rows[-1].metrics, dict) and "burstiness" in rows[-1].metrics


def test_offline_style_critic_does_not_invent_findings(session, novel):
    """离线提供者没有语义判断力，必须如实返回「没有模型意见」，而不是编几条读感问题。"""
    critic = StyleCritic(extraction_service.build_agents("offline")["provider"])
    report = critic.review(session, novel, VARIED_TEXT * 4, use_model=True, persist=False)
    assert all(issue["metric"] != "model" for issue in report["issues"])
