"""V0.7 发布前检查与导出：把平台的评审口径变成可执行清单。

平台原文（2026-09）：签约标准里明确不接受「AI粗制滥造、格式混乱、结构失常、空洞水文」，
以及「靠重复堆砌、机械扩写或大段无效铺陈拉长篇幅」「大段内容未能推动情节发展」；
优质内容要求「开篇能快速进入主线」；福利侧按每日 4000/6000 字与完读率评估。
"""

from __future__ import annotations

from app.services import publish_service

HEAD_OK = (
    "第21章 试剑\n\n"
    "刀锋从左侧劈过来，他侧身让开半步。\n\n"
    "“谁？”阴影里有人问。\n\n"
    "他没有答，反手一剑捅进那人肋下。血顺着砖缝流到脚边，热的。\n"
)
HEAD_MARKDOWN = "# 第21章 试剑\n\n**他**握紧了剑柄，看着对面的人。\n\n- 一句台词\n"
HEAD_RISK = (
    "第21章 试剑\n\n"
    "他掏出手机，想加个微信把地址发过去。群里有人说可以扫码进公众号看后续。\n\n"
    "他抬头看了看天，又低头看了看地。\n"
)
HEAD_INFO_DUMP = (
    "第21章 试剑\n\n"
    "所谓剑骨，指的是人出生时骨骼中蕴含的剑意资质，也就是常说的天赋。"
    "骨品一共分为九品，三品以上称为上品。事实上，骨品只决定修行的起点，不能决定终点。"
    "剑诀分为三卷，上卷讲养气，中卷讲运剑，下卷讲杀伐。换言之，三卷分别是三个阶段。\n"
)


def test_short_chapter_is_flagged_with_word_count():
    report = publish_service.check_text("第1章 开头\n\n" + "他走了很久。" * 40)
    codes = {item["code"] for item in report.checks}
    assert "WORDS_BELOW_TARGET" in codes
    assert report.word_count < 2000
    assert report.to_dict()["words_per_chapter"] == [2000, 3000], "默认按番茄的常见长度"


def test_chapter_within_range_passes_word_check():
    body = "他侧身让开半步，反手一剑捅进那人肋下。血顺着砖缝流到脚边。\n\n" * 90
    report = publish_service.check_text("第1章 试剑\n\n" + body)
    codes = {item["code"] for item in report.checks}
    assert "WORDS_OK" in codes or "WORDS_ABOVE_TARGET" in codes
    assert "WORDS_BELOW_TARGET" not in codes


def test_risk_words_are_reported_with_context_and_advice():
    report = publish_service.check_text(HEAD_RISK)
    categories = {risk["category"] for risk in report.risks}
    assert "站外引流" in categories
    words = {risk["word"] for risk in report.risks}
    assert {"微信", "扫码"} <= words
    risk_check = next(item for item in report.checks if item["code"] == "RISK_WORD")
    assert risk_check["excerpt"], "风险词要给上下文，作者才知道怎么改"
    assert risk_check["fix"]


def test_markdown_leftovers_are_flagged():
    report = publish_service.check_text(HEAD_MARKDOWN)
    codes = {item["code"] for item in report.format_issues}
    assert "FORMAT_MARKDOWN_LEFT" in codes
    assert "FORMAT_TITLE_MISSING" not in codes, "首行有 # 也算标题行"


def test_missing_title_line_is_flagged():
    report = publish_service.check_text("他握紧了剑柄，看着对面的人，没有说话。\n")
    codes = {item["code"] for item in report.format_issues}
    assert "FORMAT_TITLE_MISSING" in codes


def test_slow_opening_and_info_dump_are_flagged():
    slow = publish_service.opening_report(
        "青云山很高。云在山的半腰上，一层一层地堆着。石阶一共有三百六十级，边角都被踩得圆润。"
        "天色由青转白，最后沉成一片灰蓝。"
    )
    assert slow["available"] and slow["slow_start"] is True

    dumped = publish_service.opening_report(HEAD_INFO_DUMP)
    assert dumped["info_dump"] is True
    assert dumped["exposition_per_1k"] >= 8, "定义式说明要被抓到"
    assert "背景" in dumped["info_dump_verdict"] or "解释" in dumped["info_dump_verdict"]

    fast = publish_service.opening_report(HEAD_OK)
    assert fast["dialogue"] is True and fast["action"] is True
    assert fast["slow_start"] is False


def test_platform_rules_mapping_reports_the_four_categories():
    filler = "青云山很高。云在山的半腰上，一层一层地堆着。石阶一共有三百六十级。\n" * 6
    report = publish_service.check_text("第1章 山\n\n" + filler)
    rules = {item["rule"]: item for item in report.platform_rules}
    assert set(rules) >= {"空洞水文", "AI 粗制滥造 / 行文机械", "词藻堆砌 / 句式呆板", "结构失常 / 大段未推动情节"}
    assert rules["空洞水文"]["hit"] is True, "纯景物铺陈应当命中空洞水文"
    assert rules["空洞水文"]["advice"]


def test_weak_ending_is_flagged_for_completion_rate():
    body = "他把刀收回鞘里，转身走了。天亮了，街上有了人。\n\n" * 40
    report = publish_service.check_text("第1章 收尾\n\n" + body)
    codes = {item["code"] for item in report.checks}
    assert "ENDING_NO_HOOK" in codes


# --------------------------------------------------------------------------- 平台写作课提炼的规则
def test_first_page_tail_hook_detection():
    """平台原话：第一页页尾要留一句让人想翻页的话。"""
    strong = publish_service.page_tail_hook("我大概明天就要死了。")
    assert strong["strong"] is True
    assert strong["kind"] in ("危机", "揭示", "悬问")

    question = publish_service.page_tail_hook("他到底想干什么？")
    assert question["kind"] == "悬问" and question["strong"]

    cut = publish_service.page_tail_hook("他话说到一半就停住了——")
    assert cut["kind"] == "截断" and cut["strong"]

    flat = publish_service.page_tail_hook("天亮了，街上有了人。")
    assert flat["strong"] is False, "短陈述句不算钩子"


def test_first_page_check_uses_page_window():
    """口径是「第一页」（约 600 字）：窗口内最后一句决定读者会不会划下一页。"""
    hooked = "第1章 相亲\n\n他把外套的扣子扣好，又解开了一颗。\n“怎么才能避免被对方看上？”\n"
    report = publish_service.opening_report(hooked)
    assert report["tail_hook"] == "悬问" and report["tail_hook_strong"] is True

    flat = "第1章 相亲\n\n他把外套的扣子扣好。他往公交站走去。\n"
    assert publish_service.opening_report(flat)["tail_hook_strong"] is False

    long_chapter = (
        "第1章 相亲\n\n"
        + ("他把外套的扣子扣好，又解开了一颗。街上的早点摊刚支起来。\n" * 40)
        + "“最后一句在这里。”\n"
    )
    window = publish_service.opening_report(long_chapter)
    assert window["chars"] == publish_service.craft_rules.FIRST_PAGE_CHARS, "只取第一页窗口"
    assert "最后一句在这里" not in window["last_sentence"], "600 字之后的句子不算第一页"


def test_long_description_over_official_limit_is_flagged():
    """官方口径：超过一百字的风景和情绪描写要回头看看是不是水文。"""
    long_scenery = (
        "青云山很高，云在山腰一层一层堆着，山石青灰，被雨水洗得发亮，纹路里长着薄薄的苔。"
        "崖边的松树斜斜地长着，枝干弯向山谷。雾从谷底浮起来，慢慢漫过石阶，漫过石栏，"
        "漫过整座山门，把门柱上的字泡得发胀。阳光从云缝里漏下来，落在石阶上，"
        "拉出一道道细长的影子，风里带着湿润的气味，也带着松针的气味。"
    )
    assert len(long_scenery) > publish_service.craft_rules.DESCRIPTION_LIMIT
    report = publish_service.check_text("第1章 山\n\n" + long_scenery + "\n")
    codes = {item["code"] for item in report.checks}
    assert "DESCRIPTION_OVER_LIMIT" in codes

    short_scenery = "青云山很高，云在山腰一层一层堆着。"
    ok = publish_service.check_text("第1章 山\n\n" + short_scenery + "\n")
    assert "DESCRIPTION_OVER_LIMIT" not in {item["code"] for item in ok.checks}


def test_checks_point_at_the_craft_rule_they_come_from():
    """每条检查要能追到出处：作者可以自己回去核对平台原文。"""
    report = publish_service.check_text("第1章 开头\n\n他出生在江城，家里有三口人。\n")
    tagged = [item for item in report.checks if item.get("rule")]
    assert tagged, "检查项应当标注来源规则"
    known = {rule.code for rule in publish_service.craft_rules.CRAFT_RULES}
    for item in tagged:
        assert item["rule"] in known, f"未知规则编号 {item['rule']}"
    assert publish_service.craft_rules.rule("FIRST_PAGE") is not None
    assert len(publish_service.craft_rules.catalog()) == len(publish_service.craft_rules.CRAFT_RULES)


def test_strip_markdown_removes_markers_keeps_text():
    raw = "# 第21章 试剑\n\n**他**握紧了`剑柄`。\n\n- 一句台词\n- 又一句\n\n> 引用一行"
    cleaned = publish_service.strip_markdown_text(raw)
    assert cleaned.startswith("第21章 试剑")
    assert "**" not in cleaned and "`" not in cleaned
    assert "他握紧了剑柄。" in cleaned
    assert "- 一句台词" not in cleaned and "一句台词" in cleaned
    assert cleaned.count("\n") < raw.count("\n") + 2


def test_pace_window_flags_always_tense_and_always_loose():
    """平台课《拒绝流水账》：节奏要张弛有度，一直紧读者累，一直松读者走。"""
    from app.services import structure_service as st

    base = {
        "chapter_id": "",
        "title": "",
        "word_count": 2000,
        "hook_score": 0.6,
        "advancement_per_1k": 10.0,
        "filler_paragraph_ratio": 0.0,
        "dialogue_ratio": 0.2,
        "continuity_errors": 0,
        "continuity_warnings": 0,
        "claim_conflicts": 0,
        "foreshadowing_opened": [],
        "foreshadowing_advanced": [],
        "commitments_due": [],
        "verdicts": [],
    }
    tense = [{**base, "chapter_number": i, "conflict_per_1k": 45.0} for i in range(1, 6)]
    assert "一直紧" in st._pace_windows(tense)[0]["verdict"]

    loose = [{**base, "chapter_number": i, "conflict_per_1k": 2.0} for i in range(1, 6)]
    assert "一直松" in st._pace_windows(loose)[0]["verdict"]

    mixed = [
        {**base, "chapter_number": i, "conflict_per_1k": value}
        for i, value in enumerate([5.0, 40.0, 6.0, 38.0, 7.0], start=1)
    ]
    assert "有起伏" in st._pace_windows(mixed)[0]["verdict"]


def test_craft_rules_are_exposed_with_sources(client):
    """规则与出处要能查：作者可以回去核对平台原文，而不是只听我们一句经验。"""
    response = client.get("/api/craft-rules")
    assert response.status_code == 200
    rules = response.json()
    codes = {item["code"] for item in rules}
    assert {"FIRST_PAGE", "PAGE_END_HOOK", "CHAPTER_END_HOOK", "DESCRIPTION_OVER_100"} <= codes
    for item in rules:
        assert item["source"] and item["advice"], f"{item['code']} 缺出处或做法"
        assert "番茄作家课堂" in item["source"]


# --------------------------------------------------------------------------- 接口
def test_publish_check_api_on_chapter(client, novel, chapters):
    chapter = chapters[5]
    response = client.post(f"/api/chapters/{chapter.id}/publish-check")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["chapter_number"] == chapter.chapter_number
    assert payload["word_count"] > 0
    assert payload["platform"] == publish_service.PLATFORM_NAME
    assert isinstance(payload["checks"], list) and payload["checks"]
    assert payload["daily_words_targets"] == [4000, 6000]


def test_chapter_check_does_not_ask_for_a_title_line(client, novel, chapters):
    """库里章节的标题在单独字段里（导出时补），正文首行不该被要求是标题。"""
    response = client.post(f"/api/chapters/{chapters[5].id}/publish-check")
    assert response.status_code == 200
    codes = {item["code"] for item in response.json()["format_issues"]}
    assert "FORMAT_TITLE_MISSING" not in codes


def test_publish_check_api_on_draft_text(client, novel):
    response = client.post(
        f"/api/novels/{novel.id}/publish-check",
        json={"text": "第1章 试\n\n他掏出手机，想加个微信。", "title": "第1章 试"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert any(item["code"] == "RISK_WORD" for item in payload["checks"])
    assert payload["ready"] is False, "有风险词时不该判为可直接发布"

    empty = client.post(f"/api/novels/{novel.id}/publish-check", json={"text": "   "})
    assert empty.status_code == 400


def test_structure_view_lines_up_chapters_and_flags_weak_runs(client, novel, chapters):
    """结构视图：逐章信号对齐、连续弱区、节奏窗口——用来找读者会掉队的地方。"""
    response = client.get(f"/api/novels/{novel.id}/structure")
    assert response.status_code == 200, response.text
    view = response.json()

    assert view["chapter_count"] == len(chapters)
    numbers = [row["chapter_number"] for row in view["chapters"]]
    assert numbers == sorted(numbers), "按章号排序"
    first = view["chapters"][0]
    for key in (
        "hook_score",
        "advancement_per_1k",
        "filler_paragraph_ratio",
        "continuity_errors",
        "claim_conflicts",
        "verdicts",
    ):
        assert key in first, f"缺字段 {key}"
    assert view["floors"]["pace_window"] >= 3

    for run in view["weak_runs"]:
        assert run["end_chapter"] >= run["start_chapter"]
        assert run["length"] == run["end_chapter"] - run["start_chapter"] + 1
        assert run["reasons"], "弱区必须说明原因"

    for window in view["pace_windows"]:
        assert window["end_chapter"] - window["start_chapter"] + 1 <= view["floors"]["pace_window"]
        assert window["verdict"]


def test_export_filename_survives_a_chinese_slug(client):
    """中文书名会变成中文目录名，响应头必须能用（HTTP 头只能是 latin-1）。"""
    created = client.post("/api/novels", json={"title": "剑起青云"}).json()
    assert created["slug"], "中文标题应当生成目录名"
    client.post(
        f"/api/novels/{created['id']}/chapters",
        json={"chapter_number": 1, "title": "入山", "content": "第1章 入山\n\n他站在山门前。"},
    )

    response = client.get(f"/api/novels/{created['id']}/export?fmt=txt")
    assert response.status_code == 200, response.text
    disposition = response.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition, "中文文件名要走 RFC 5987"
    assert all(ord(char) < 128 for char in disposition), "响应头里不能出现非 ASCII 字符"
    assert "第1章 入山" in response.text


def test_export_returns_plain_text_without_markdown(client, novel, chapters):
    response = client.get(f"/api/novels/{novel.id}/export?fmt=txt")
    assert response.status_code == 200
    assert 'filename="' in response.headers["content-disposition"]
    assert ".txt" in response.headers["content-disposition"]
    text = response.text
    assert "第1章" in text
    assert "**" not in text and "```" not in text
    assert "\n\n\n" in text, "章节之间留空行，粘贴到后台更清楚"

    limited = client.get(f"/api/novels/{novel.id}/export?fmt=txt&from_chapter=1&to_chapter=2")
    assert "第2章" in limited.text
    assert "第3章" not in limited.text

    markdown = client.get(f"/api/novels/{novel.id}/export?fmt=md")
    assert markdown.status_code == 200

    missing = client.get(f"/api/novels/{novel.id}/export?from_chapter=999")
    assert missing.status_code == 404
