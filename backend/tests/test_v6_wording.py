"""V0.6 用词与收束：可预测用词、生僻字飘、总结升华、平滑过渡、抽象替代具体、桥段同质化。

这一层的关键立场：**不追求困惑度**。「意外用词」是区间指标 ——
太平（用同一批词）和太飘（生造生僻字）都要拦，换词的方向是「更具体」，不是「更罕见」。
"""

from __future__ import annotations

from app.services import style_service
from tests.test_v3_style import CLEAN_LONG_TEXT

V6_CODES = (
    "PREDICTABLE_WORDING",
    "REPETITIVE_VOCABULARY",
    "VERB_MONOTONE",
    "WORD_DRIFT",
    "SUMMARY_ENDING",
    "ELEVATION_DENSE",
    "SMOOTH_TRANSITION",
    "ABSTRACT_SUBSTITUTION",
    "BRIDGE_REPEAT",
)

#: 用同一批词反复写（可预测的用词）
SAME_WORDS = (
    "他抬头看了看天。他低头看了看地。他抬手摸了摸剑。他低头摸了摸衣。\n"
    "他抬头看了看门。他抬头看了看窗。他低头看了看手。他低头看了看脚。\n"
    "他看了看天，又看了看地。他看了看门，又看了看窗。他看了看手，又看了看脚。\n"
    "他说了一句话，又说了一句话。他看了一眼，又看了一眼。他动了动，又动了动。\n"
)
#: 堆生僻辞藻：用词飘出本书语感
FLOWERY = (
    "氤氲的雾气缭绕着嶙峋的崖壁，缱绻的光影在苍茫间氤氲流转，氤氲出一种难以言喻的惆怅。\n"
    "他凝视着那片氤氲的苍茫，心绪在缱绻与惘然之间踯躅，仿佛冥冥之中有某种微妙的昭示。\n"
    "缱绻的思绪如氤氲的水汽般潆洄，嶙峋的记忆在怅惘里氤氲成一片苍茫的惘然。\n"
    "他惘然地凝视着氤氲的远方，潆洄的心绪在缱绻中踯躅，苍茫与嶙峋在冥冥里氤氲。\n"
)
#: 章末总结 + 升华
SUMMARY_TEXT = (
    "他收好剑，走出了客栈。这一切，终于结束了。\n"
    "从那以后，他再也没有回过青云镇。往后的路，他一个人走。\n"
    "他终于明白，有些事不是靠一把剑就能解决的。说到底，人总得学会低头。\n"
    "这一夜注定无眠。多年以后他才知道，那晚的选择改变了所有事情的一切。\n"
)
ELEVATION_TEXT = (
    "所谓江湖，不过是你来我往。人这一生，能信的又有几个？\n"
    "这世上本就没有真正的公平，所谓公道，无非是拳头硬的人说了算。\n"
    "有些东西失去了就是失去了，或许这就是命，谁又能如何。\n"
    "他终于懂了：真正的强大不是杀人，而是忍住不杀。这才是一个剑客的命。\n"
)
SMOOTH_TEXT = (
    "于是他就这样走了。随后，第二天一早，他紧接着出了城。\n"
    "接下来，他不知不觉走到了山脚。与此同时，天也渐渐黑了。片刻之后，他看到了灯。\n"
    "次日，他进了镇子。此后不多时，他便找到了那家铺子。转眼，事情就办成了。\n"
    "就这样，一切顺理成章。果不其然，他也就此得到了想要的东西，然后便离开了。\n"
)
ABSTRACT_TEXT = (
    "空气里弥漫着一种难以言喻的气息，杀意与压迫感交织成无形的氛围。\n"
    "他的心绪深处涌起某种微妙的存在感，气场在沉默中显得格外复杂而深邃。\n"
    "那股威压般的压迫感笼罩着整片空间，氛围变得幽深而难以捉摸。\n"
    "情绪在他内心深处翻涌，一种莫名的寒意与暖意同时浮现，无比微妙。\n"
)


def _codes(report: dict) -> set[str]:
    return {issue["code"] for issue in report["issues"] if issue["level"] == "warning"}


def _seed_texts(chapters) -> list[str]:
    return [chapters[number].content or "" for number in sorted(chapters)]


def _prior(novel_chapters: int = 6) -> list[str]:
    """造一个「前面几章反复用同一桥段」的既往库（种子小说本身很干净，不会触发）。"""
    return [
        f"第{index}章\n他把刀收进鞘里，深吸一口气，转身就走。\n"
        f"他没有再说话，消失在夜色里。\n"
        for index in range(1, novel_chapters + 1)
    ]


# --------------------------------------------------------------------------- 指标
def test_v6_metrics_are_deterministic_and_bounded():
    first = style_service.measure(CLEAN_LONG_TEXT).to_dict()
    second = style_service.measure(CLEAN_LONG_TEXT).to_dict()
    assert first == second
    for key in (
        "word_ttr",
        "word_concentration",
        "verb_variety",
        "novel_bigram_ratio",
        "novel_char_ratio",
        "summary_per_1k",
        "elevation_per_1k",
        "transition_per_1k",
        "abstract_unsupported_ratio",
    ):
        assert key in first, f"缺指标 {key}"
    assert 0.0 <= first["word_ttr"] <= 1.0
    assert 0.0 <= first["novel_char_ratio"] <= 1.0
    assert 0.0 <= first["abstract_unsupported_ratio"] <= 1.0


def test_novel_char_ratio_needs_history_and_is_ordered():
    first = (
        "他站在山门前的青石阶下，把肩上的旧布囊往上提了提。布囊里只有两身换洗衣裳、"
        "半块干饼，还有一本被翻得起了毛边的薄册子，册子是父亲留下的。\n"
    )
    second = (
        "她把手按在验灵石上，石中的灰雾缓缓浮起来，稳稳停在三道刻度上。执事抬眼看了"
        "她一圈，在名册上落了一笔，往后一指，让她去测骨碑那边排队等着。\n"
    )
    assert style_service.novel_char_ratio(first, None) == 0.0, "没有既往文本时不给结论"
    ratios = style_service.novel_char_ratios([first, second])
    assert ratios[0] == 0.0 and ratios[1] > 0.0, "第 2 章相对第 1 章应当有少量新字"
    reused = set(style_service._chars(first))
    assert style_service.novel_char_ratio(first, reused) == 0.0, "同一段文字里没有新字"
    assert style_service.novel_char_ratio(second, reused) > 0.3, "换一个完全不同的场景会引入大量新字"


def test_seed_chapters_do_not_trigger_v6_rules(session, novel, chapters):
    """规则必须对「本书自己写过的章节」保持沉默（含跨章桥段检查）。"""
    texts = _seed_texts(chapters)
    for index, text in enumerate(texts):
        report = style_service.review_text(
            text,
            min_chars=300,
            prior_texts=texts[:index],
            prior_labels=[f"第 {number} 章" for number in sorted(chapters)[:index]],
        )
        hit = _codes(report) & set(V6_CODES)
        assert not hit, f"第{index + 1}章被误报：{sorted(hit)}"


# --------------------------------------------------------------------------- 用词
def test_predictable_wording_and_verb_monotone_fire():
    report = style_service.review_text(SAME_WORDS, min_chars=200)
    codes = _codes(report)
    assert "PREDICTABLE_WORDING" in codes
    assert "REPETITIVE_VOCABULARY" in codes
    assert "VERB_MONOTONE" in codes
    issue = next(i for i in report["issues"] if i["code"] == "VERB_MONOTONE")
    assert issue["suggestion"], "每条问题都要给出可执行的改法"


def test_clean_text_has_healthy_vocabulary_metrics():
    metrics = style_service.measure(CLEAN_LONG_TEXT)
    assert metrics.word_ttr > 0.85, "正常行文的用词多样性不该偏低"
    assert metrics.word_concentration < 0.22
    assert metrics.verb_variety > 0.26


def test_word_drift_flags_flowery_wording_only_with_history(chapters):
    history = _seed_texts(chapters)
    with_history = style_service.review_text(FLOWERY, min_chars=200, prior_texts=history)
    issue = next(i for i in with_history["issues"] if i["code"] == "WORD_DRIFT")
    assert issue["value"] > style_service.NOVEL_CHAR_CAP
    assert issue["excerpt"], "要列出具体是哪些生僻字飘了"

    without_history = style_service.review_text(FLOWERY, min_chars=200)
    assert "WORD_DRIFT" not in _codes(without_history), "没有既往文本时不能判「飘」"


def test_flowery_text_is_also_warned_as_predictable_despite_new_chars():
    """生僻字多 ≠ 用词多样：堆砌辞藻的同时往往在用同一批词反复堆。"""
    report = style_service.review_text(FLOWERY, min_chars=200)
    assert "PREDICTABLE_WORDING" in _codes(report)


# --------------------------------------------------------------------------- 总结 / 升华 / 过渡 / 抽象
def test_summary_and_elevation_rules_fire_with_evidence():
    summary = style_service.review_text(SUMMARY_TEXT, min_chars=200)
    assert "SUMMARY_ENDING" in _codes(summary)
    assert next(i for i in summary["issues"] if i["code"] == "SUMMARY_ENDING")["excerpt"]

    elevation = style_service.review_text(ELEVATION_TEXT, min_chars=200)
    assert "ELEVATION_DENSE" in _codes(elevation)
    assert next(i for i in elevation["issues"] if i["code"] == "ELEVATION_DENSE")["excerpt"]


def test_smooth_transition_rule_fires():
    report = style_service.review_text(SMOOTH_TEXT, min_chars=200)
    issue = next(i for i in report["issues"] if i["code"] == "SMOOTH_TRANSITION")
    assert issue["value"] > 6.0
    assert "于是" in issue["excerpt"] or "随后" in issue["excerpt"]


def test_abstract_substitution_rule_fires():
    report = style_service.review_text(ABSTRACT_TEXT, min_chars=200)
    issue = next(i for i in report["issues"] if i["code"] == "ABSTRACT_SUBSTITUTION")
    assert issue["value"] > 0.45
    assert issue["excerpt"]
    assert "能看见的细节" in issue["suggestion"]


def test_concrete_writing_is_not_flagged():
    text = (
        "他把那块木牌从怀里掏出来，用拇指擦了擦上面的泥。牌面上刻着一个「林」字。\n"
        "“这个字是谁刻的？”他问。掌柜摇了摇头，指了指门外那条泥泞的路。\n"
        "他把木牌塞回怀里，推开门走了出去。雨还在下，路上的水已经没过了脚踝。\n"
        "他沿着墙根走了三十步，在一扇半掩的木门前停下，抬手敲了三下。\n"
    )
    report = style_service.review_text(text, min_chars=150)
    assert not (_codes(report) & {"ABSTRACT_SUBSTITUTION", "SUMMARY_ENDING", "ELEVATION_DENSE"})


# --------------------------------------------------------------------------- 桥段同质化
def test_bridge_repeat_across_chapters():
    bridge = "他把刀收进鞘里，深吸一口气，转身就走。\n他没有再说话，消失在夜色中。\n"
    report = style_service.review_text(
        bridge,
        min_chars=30,
        prior_texts=_prior(6),
        prior_labels=[f"第 {number} 章" for number in range(1, 7)],
    )
    issues = [i for i in report["issues"] if i["code"] == "BRIDGE_REPEAT"]
    assert issues, "反复出现的收尾桥段应当被点名"
    message = " ".join(issue["message"] for issue in issues)
    assert "深吸一口气" in message
    assert "第 1 章" in message, "要点出它在前文哪些章用过"

    thin = style_service.review_text(
        bridge,
        min_chars=30,
        prior_texts=_prior(2),
        prior_labels=["第 1 章", "第 2 章"],
    )
    assert "BRIDGE_REPEAT" not in _codes(thin), "只在两章里出现过不算桥段同质化"


def test_bridge_repeat_needs_prior_chapters():
    bridge = "他深吸一口气，转身就走。"
    report = style_service.review_text(bridge, min_chars=10)
    assert "BRIDGE_REPEAT" not in _codes(report)


# --------------------------------------------------------------------------- 与既有机制协同
def test_compare_metrics_knows_v6_directions():
    thin = style_service.measure(SAME_WORDS).to_dict()
    rich = style_service.measure(CLEAN_LONG_TEXT).to_dict()
    deltas = style_service.compare_metrics(thin, rich)["deltas"]
    assert deltas["word_ttr"]["better"] is True, "用词多样性变好算改善"
    assert deltas["word_concentration"]["better"] is True, "集中度下降算改善"
    assert deltas["summary_per_1k"]["better"] in (True, None)


def test_style_lock_directive_states_v6_constraints(session, novel):
    profile = style_service.save_profile(session, novel, [CLEAN_LONG_TEXT], name="V0.6 基线")
    session.commit()
    voice = style_service.save_voice_profile(session, novel, [CLEAN_LONG_TEXT], name="V0.6 声音")
    session.commit()
    directive = style_service.style_lock_directive(profile, voice_profile=voice)
    assert "用词" in directive
    assert "总结" in directive
    assert "过渡" in directive
    assert "桥段" in directive


def test_drift_report_tracks_v6_metrics(session, novel, chapters):
    profile = style_service.save_profile(
        session, novel, [_seed_texts(chapters)[0]], name="漂移 V0.6", make_default=True
    )
    style_service.lock_profile(session, profile, True)
    session.commit()
    report = style_service.drift_report(session, novel)
    metrics = {item["metric"] for chapter in report["chapters"] for item in chapter["drifted"]}
    assert metrics, "至少应报出漂移指标"
    assert "novel_char_ratio" not in metrics, "随书推进系统性下降的指标不参与逐章漂移统计"


def test_stale_baseline_is_surfaced_not_silently_ignored(session, novel):
    """旧版本建出来的基线缺少新指标：必须显式说出来，而不是让新规则静默失效。"""
    profile = style_service.save_profile(
        session, novel, [CLEAN_LONG_TEXT], name="旧版基线", make_default=True
    )
    stale_metrics = {
        key: value
        for key, value in (profile.metrics or {}).items()
        if key not in ("word_ttr", "summary_per_1k", "transition_per_1k")
    }
    stale_metrics["metrics_version"] = "0.3"
    profile.metrics = stale_metrics
    session.commit()

    report = style_service.review_text(CLEAN_LONG_TEXT, profile=profile)
    assert report["baseline_stale"] is True
    assert report["baseline_metrics_version"] == "0.3"
    assert {"word_ttr", "summary_per_1k", "transition_per_1k"} <= set(
        report["baseline_missing_metrics"]
    )
    issue = next(i for i in report["issues"] if i["code"] == "BASELINE_STALE")
    assert issue["level"] == "info"
    assert "重建" in issue["suggestion"]

    drift = style_service.drift_report(session, novel)
    assert drift["stale"] is True
    assert drift["metrics_version"] == "0.3"
    assert set(drift["missing_metrics"]) >= {"word_ttr", "summary_per_1k"}


def test_fresh_baseline_is_not_marked_stale(session, novel):
    profile = style_service.save_profile(
        session, novel, [CLEAN_LONG_TEXT], name="新基线", make_default=True
    )
    session.commit()
    report = style_service.review_text(CLEAN_LONG_TEXT, profile=profile)
    assert report["baseline_stale"] is False
    assert report["baseline_missing_metrics"] == []
    assert "BASELINE_STALE" not in _codes(report)
    assert style_service.drift_report(session, novel)["stale"] is False
