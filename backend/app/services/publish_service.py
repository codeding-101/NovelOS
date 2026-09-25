"""发布前检查：把平台的评审标准变成可执行的清单（以番茄免费小说为样本）。

依据（2026-09 平台原文）：
- 签约标准「内容合规原则」明确不接受：AI粗制滥造、格式混乱、结构失常、空洞水文，
  以及「靠重复堆砌、机械扩写或大段无效铺陈拉长篇幅」「大段内容未能推动情节发展」。
- 低质治理公告（8 月）处置的四类：ai粗制滥造 / 格式混乱 / 结构失常 / 空洞水文；
  典型案例描述里有「行文机械、文笔空洞」「词藻堆砌、句式呆板」。
- 优质内容评估里要求「开篇能快速进入主线」「人物出场时机对主线情节有推动作用」。
- 福利侧：每日有效更新 4000 或 6000 字，未过审章节不计入；连续 15 天断更失去全部福利；
  星火奖按 30/50/80/100 万字的完读率评估（章节阅读人数少于 1000 时数据不置信）。

所以这个模块做四件事：字数是否够一章、审核风险词、格式是否规范、
开篇与章末是否达标（这两处直接关系到完读率）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Chapter, Novel
from app.services import craft_rules, style_service
from app.timeutil import count_words, split_sentences

#: 番茄免费小说按章发布的常见长度区间与每日更新目标（平台福利口径：每日 4000 或 6000 字）
PLATFORM_NAME = "番茄免费小说"
WORDS_PER_CHAPTER = (2000, 3000)
DAILY_WORDS_TARGETS = (4000, 6000)

#: 审核风险词：分类 + 词 + 处置建议。
#: 这里只收**通用、可判定**的高风险表达（站外引流、露骨描写、迷信赌博等），
#: 政治与民族宗教类不在代码里列词表 —— 那类判断交给作者与平台规则，别用一张词表去猜。
DEFAULT_RISK_WORDS: dict[str, tuple[tuple[str, str], ...]] = {
    "站外引流": (
        ("微信", "不要出现站外联系方式或引流暗示，平台明令禁止"),
        ("微信号", "不要出现站外联系方式或引流暗示，平台明令禁止"),
        ("公众号", "不要出现站外联系方式或引流暗示，平台明令禁止"),
        ("QQ群", "不要出现站外联系方式或引流暗示，平台明令禁止"),
        ("扫码", "不要出现站外联系方式或引流暗示，平台明令禁止"),
        ("二维码", "不要出现站外联系方式或引流暗示，平台明令禁止"),
        ("加群", "不要出现站外联系方式或引流暗示，平台明令禁止"),
        ("私聊", "不要出现站外联系方式或引流暗示，平台明令禁止"),
    ),
    "露骨描写": (
        ("赤裸", "情色与身体描写注意尺度，露骨段落容易被判违规"),
        ("胴体", "情色与身体描写注意尺度，露骨段落容易被判违规"),
        ("呻吟", "情色与身体描写注意尺度，露骨段落容易被判违规"),
        ("情欲", "情色与身体描写注意尺度，露骨段落容易被判违规"),
    ),
    "血腥过度": (
        ("血肉模糊", "暴力血腥描写注意分寸，过细的伤情描写容易被判违规"),
        ("脑浆", "暴力血腥描写注意分寸，过细的伤情描写容易被判违规"),
        ("开膛", "暴力血腥描写注意分寸，过细的伤情描写容易被判违规"),
        ("肢解", "暴力血腥描写注意分寸，过细的伤情描写容易被判违规"),
    ),
    "封建迷信": (
        ("算命", "迷信与算命类内容容易被判违规，改成更中性的说法"),
        ("跳大神", "迷信与算命类内容容易被判违规，改成更中性的说法"),
        ("驱邪符水", "迷信与算命类内容容易被判违规，改成更中性的说法"),
    ),
    "赌博毒品": (
        ("赌场", "赌博类描写容易被判违规"),
        ("下注", "赌博类描写容易被判违规"),
        ("吸毒", "毒品类描写容易被判违规"),
        ("冰毒", "毒品类描写容易被判违规"),
    ),
}

#: 中文句子里混进英文标点：不算错字，但排版会很乱
_LATIN_PUNCT_RE = re.compile(r"[\u4e00-\u9fa5][,;:!?]")
#: Markdown 残留（正文里不该有，粘贴到平台后台会原样显示）
_MARKDOWN_RE = re.compile(r"(^|\n)\s*(#{1,6}\s|\*\*|[-*+]\s|>\s)|`")
#: 章节标题行：番茄用「第N章 标题」
_TITLE_RE = re.compile(r"^\s*#{0,6}\s*第\s*[0-9零一二三四五六七八九十百千]+\s*章[^\n]*")


@dataclass
class PublishCheck:
    """一张发布前的清单。"""

    novel_id: str = ""
    chapter_number: int | None = None
    title: str = ""
    word_count: int = 0
    platform: str = PLATFORM_NAME
    words_per_chapter: tuple[int, int] = WORDS_PER_CHAPTER
    checks: list[dict[str, Any]] = field(default_factory=list)
    risks: list[dict[str, Any]] = field(default_factory=list)
    format_issues: list[dict[str, Any]] = field(default_factory=list)
    platform_rules: list[dict[str, Any]] = field(default_factory=list)

    @property
    def blocking(self) -> int:
        return sum(1 for item in self.checks if item.get("level") == "error")

    @property
    def warnings(self) -> int:
        return sum(1 for item in self.checks if item.get("level") == "warning")

    def to_dict(self) -> dict[str, Any]:
        return {
            "novel_id": self.novel_id,
            "chapter_number": self.chapter_number,
            "title": self.title,
            "word_count": self.word_count,
            "platform": self.platform,
            "words_per_chapter": list(self.words_per_chapter),
            "daily_words_targets": list(DAILY_WORDS_TARGETS),
            "checks": self.checks,
            "risks": self.risks,
            "format_issues": self.format_issues,
            "platform_rules": self.platform_rules,
            "blocking": self.blocking,
            "warnings": self.warnings,
            "ready": self.blocking == 0,
        }


def _issue(code: str, level: str, message: str, fix: str = "", **extra: Any) -> dict[str, Any]:
    return {"code": code, "level": level, "message": message, "fix": fix, **extra}


def _last_sentence_of(text: str) -> str:
    """正文最后一句（有实际内容的那句）。

    分句器会把收尾的引号单独切成一段（「…？」+「”」），直接取末段会拿到一个引号，
    于是凡是以对白收尾的章都会被误判成「没有钩子」。这里丢掉末尾没有汉字的碎片。
    """
    sentences = [item.strip() for item in split_sentences(text or "") if item.strip()]
    while sentences and not re.search(r"[\u4e00-\u9fa5]", sentences[-1]):
        sentences.pop()
    return sentences[-1].strip("“”\"' \n") if sentences else ""


def _ending_beat(text: str, *, window: int = 200) -> str:
    """章末「这一拍」：最后约 200 字。

    只看最后一句太窄了：像「……『没回来。』他把册子一夹……他跟上去了。」这种收尾，
    钩子在中段，末句是动作收束 —— 读者感受到的是整个结尾的落点，不是一个句子。
    """
    body = (text or "").strip()
    return body[-window:]


def _last_complete_sentence(text: str) -> str:
    """窗口内最后一句**完整**的话。

    「第一页」是按字数切出来的，切点很可能落在一句话中间（「讲那人留下过东西—」）。
    拿半句去判「页尾有没有钩子」是误报，所以这里往前退到最近一句以句末标点收尾的。
    """
    sentences = [
        item.strip().lstrip("“”「」\"' \n").strip()
        for item in split_sentences(text or "")
        if item.strip()
    ]
    sentences = [item for item in sentences if re.search(r"[\u4e00-\u9fa5]", item)]
    for sentence in reversed(sentences):
        # 破折号不算「完整收尾」：它可能是作者故意截断，也可能是字数窗口正好切在这里，
        # 两者分不清，而误报「页尾没钩子」比漏掉一次截断更糟
        if sentence.endswith(("。", "！", "？", "…", "”", "」")):
            return sentence.strip("“”\"' \n")
    return sentences[-1].strip("“”\"' \n") if sentences else ""


def load_extra_risk_words() -> dict[str, tuple[tuple[str, str], ...]]:
    """读作者自己的风险词表：<数据目录>/risk_words.json。

    形如 ``{"站外引流": ["加微", "私信领取"], "自定分类": [["词", "建议"]]}``。
    平台口径会变、题材各有各的雷，所以留了这个口子 —— 不用改代码。
    """
    path = settings.data_dir / "risk_words.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    extra: dict[str, tuple[tuple[str, str], ...]] = {}
    for category, entries in raw.items():
        if not isinstance(entries, list):
            continue
        collected: list[tuple[str, str]] = []
        for entry in entries:
            if isinstance(entry, str) and entry.strip():
                collected.append((entry.strip(), "作者自己加的词：确认上下文是否会被判违规"))
            elif isinstance(entry, list) and entry and isinstance(entry[0], str):
                advice = entry[1] if len(entry) > 1 and isinstance(entry[1], str) else "作者自己加的词"
                collected.append((entry[0].strip(), advice))
        if collected:
            extra[str(category)] = tuple(collected)
    return extra


def find_risk_words(text: str) -> list[dict[str, Any]]:
    """扫审核风险词，给出分类、上下文与建议（内置词表 + 作者扩展表）。"""
    hits: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    tables = [DEFAULT_RISK_WORDS, load_extra_risk_words()]
    for table in tables:
        for category, entries in table.items():
            for word, advice in entries:
                if word not in text or (category, word) in seen:
                    continue
                seen.add((category, word))
                index = text.find(word)
                start = max(0, index - 18)
                hits.append(
                    {
                        "category": category,
                        "word": word,
                        "quote": text[start : index + len(word) + 18].replace("\n", " "),
                        "advice": advice,
                    }
                )
    return hits


def find_format_issues(
    text: str, *, expected_title: str = "", expect_title_line: bool = True
) -> list[dict[str, Any]]:
    """格式问题：粘贴到平台后台会原样显示的东西。

    ``expect_title_line=False`` 用于检查已经入库的章节：标题存在单独字段里，
    导出时会自动补上，正文首行本来就不该是标题。
    """
    issues: list[dict[str, Any]] = []
    if _MARKDOWN_RE.search(text or ""):
        sample = next(
            (
                line.strip()
                for line in (text or "").splitlines()
                if line.strip().startswith(("#", "**", "- ", "* ", "> "))
            ),
            "",
        )
        issues.append(
            _issue(
                "FORMAT_MARKDOWN_LEFT",
                "warning",
                "正文里还有 Markdown 标记（#/**/- 等），粘贴到平台后台会原样显示",
                "导出或复制时去掉标记，只留正文",
                excerpt=sample[:40],
            )
        )
    latin = _LATIN_PUNCT_RE.findall(text or "")
    if len(latin) >= 5:
        issues.append(
            _issue(
                "FORMAT_PUNCT_MIXED",
                "info",
                f"中文句子里混入了 {len(latin)} 处英文标点",
                "统一用中文标点，排版更整齐",
            )
        )
    if re.search(r"\n{4,}", text or ""):
        issues.append(
            _issue(
                "FORMAT_BLANK_LINES",
                "info",
                "存在连续多个空行",
                "相邻段落之间保留一个空行即可",
            )
        )
    if expect_title_line and (text or "").strip() and not _TITLE_RE.match(text.strip()):
        issues.append(
            _issue(
                "FORMAT_TITLE_MISSING",
                "info",
                f"首行不像章节标题（平台惯用「{expected_title or '第N章 标题'}」）",
                "首行写成「第N章 标题」，粘贴时平台会识别为章节名",
            )
        )
    return issues


def opening_report(text: str, *, head_chars: int = craft_rules.FIRST_PAGE_CHARS) -> dict[str, Any]:
    """开篇检查：按「第一页」的口径，而不是泛泛的前几百字。

    平台的原话是「前三章很重要，但第一页更重要」（番茄按页阅读）。
    所以这里看三件事：第一页有没有对白／动作／冲突信号；有没有把设定大段倒出来；
    以及**第一页的最后一句**有没有留下让人翻页的钩子。
    """
    head = (text or "")[:head_chars]
    if not head.strip():
        return {"available": False}
    metrics = style_service.measure(head)
    sentences = [item for item in split_sentences(head) if item.strip()]
    dialogue = style_service.DIALOGUE_RE.search(head) is not None
    action = style_service._has_story_action(head)
    conflict = any(marker in head for marker in style_service.CONFLICT_PATTERNS)
    slow = not (dialogue or action or conflict)
    dump = (
        metrics.exposition_per_1k >= 8
        or metrics.explaining_per_1k >= 12
        or metrics.abstract_per_1k >= 14
    )
    last_sentence = _last_complete_sentence(head)
    tail = page_tail_hook(last_sentence)
    return {
        "available": True,
        "chars": len(head),
        "sentences": len(sentences),
        "dialogue": dialogue,
        "action": action,
        "conflict": conflict,
        "slow_start": slow,
        "info_dump": dump,
        "last_sentence": last_sentence[:60],
        "tail_hook": tail["kind"],
        "tail_hook_strong": tail["strong"],
        "explaining_per_1k": metrics.explaining_per_1k,
        "exposition_per_1k": metrics.exposition_per_1k,
        "abstract_per_1k": metrics.abstract_per_1k,
        "verdict": (
            "第一页偏慢：没有动作、对白或冲突信号，平台看重「开篇快速进入主线」"
            if slow
            else "第一页有动作／对白／冲突信号"
        ),
        "info_dump_verdict": (
            "第一页定义式说明、解释性叙述或抽象词偏多，像在交代背景而不是进入故事"
            if dump
            else ""
        ),
    }


#: 章末／页尾钩子的类型（依据平台课「章末留钩」的几种常见做法）
HOOK_KINDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "悬问": ("悬着的问题", ("？", "?", "吗", "呢", "什么", "为什么", "谁", "怎么办")),
    "危机": ("危险或威胁压过来", ("死", "杀", "血", "伤", "痛", "追", "逃", "危险", "完了", "来不及")),
    "揭示": ("亮出新信息或身份", ("原来", "竟然", "居然", "没想到", "竟是", "真正", "身份", "秘密")),
    "反转": ("局面掉头", ("却", "可", "但", "然而", "忽然", "突然", "偏偏")),
    "截断": ("话没说完／动作停住", ("…", "……", "——", "没说完", "戛然")),
    "情绪": ("一句短促的心理或感叹", ("！", "!", "心里", "只觉得", "后悔", "害怕", "愤怒")),
}


def page_tail_hook(sentence: str) -> dict[str, Any]:
    """判断一句收尾有没有钩子，以及属于哪一类。

    只做「像不像留了钩子」的近似判断：句末是问句、危险信号、新信息、掉头词、截断，
    或者带情绪标记的短句。判断不出就返回「平淡」。
    注意：单句成段的短陈述句（「他转身走了。」）不算钩子 —— 那是节奏手法，读者不会因此翻页。
    """
    text = (sentence or "").strip()
    if not text:
        return {"kind": "平淡", "strong": False}
    hits: list[str] = []
    for kind, (_label, markers) in HOOK_KINDS.items():
        if any(marker in text for marker in markers):
            hits.append(kind)
    if hits:
        return {"kind": hits[0], "strong": True, "kinds": hits}
    return {"kind": "平淡", "strong": False, "kinds": []}


def long_description_paragraphs(text: str, *, limit: int = craft_rules.DESCRIPTION_LIMIT) -> list[str]:
    """找出一整段都是描写（无动作、无对白）且超过官方口径长度的段落。

    官方原话：「超过一百字的风景和情绪描写，都要好好琢磨一下，是不是水文了」——
    针对的是风景与情绪描写。像「三十斤粟米两块灵石，一尺粗棉布半块」这种算账，
    数字密集、信息在推进，不该被当成水文，所以这里对有具体数目/量词的段落放行。
    """
    found: list[str] = []
    for paragraph in (text or "").split("\n"):
        body = paragraph.strip()
        if count_words(body) <= limit:
            continue
        if style_service.DIALOGUE_RE.search(body):
            continue
        if style_service._has_story_action(body):
            continue
        if len(style_service._CONCRETE_NUMBER_RE.findall(body)) >= 3 and any(
            marker in body for marker in ("灵石", "文钱", "铜钱", "斤", "两", "尺", "斗", "枚")
        ):
            continue  # 算账、计量、价格这类叙事，不是风景或情绪描写
        found.append(body)
    return found


def _platform_rule_mapping(report: dict[str, Any]) -> list[dict[str, Any]]:
    """把已有的文风指标映射到平台点名的四条低质规则上。

    只统计 warning 级的发现，与「检查项清单」保持同一口径 ——
    否则会出现「规则说命中，清单里却找不到对应那条」的困惑。
    """
    codes = {
        issue["code"] for issue in report.get("issues", []) if issue.get("level") == "warning"
    }

    def hit(*candidates: str) -> str:
        matched = [code for code in candidates if code in codes]
        return "、".join(matched)

    return [
        {
            "rule": "空洞水文",
            "hit": bool(hit("FILLER_HEAVY", "LOW_ADVANCEMENT", "SELF_REPEAT", "RESTATING_KNOWN")),
            "evidence": hit("FILLER_HEAVY", "LOW_ADVANCEMENT", "SELF_REPEAT", "RESTATING_KNOWN"),
            "advice": "删掉不推进情节的段落；每一段至少要有动作、对白或新信息",
        },
        {
            "rule": "AI 粗制滥造 / 行文机械",
            "hit": bool(hit("CLICHE_DENSE", "TELLING_DENSE", "PREDICTABLE_WORDING", "VERB_MONOTONE")),
            "evidence": hit("CLICHE_DENSE", "TELLING_DENSE", "PREDICTABLE_WORDING", "VERB_MONOTONE"),
            "advice": "把套话和解释性叙述改成具体动作；同一批词不要反复用",
        },
        {
            "rule": "词藻堆砌 / 句式呆板",
            "hit": bool(hit("WORD_DRIFT", "ABSTRACT_SUBSTITUTION", "PATTERN_LOOP", "ELEVATION_DENSE")),
            "evidence": hit("WORD_DRIFT", "ABSTRACT_SUBSTITUTION", "PATTERN_LOOP", "ELEVATION_DENSE"),
            "advice": "抽象词要有具体落点；句式要错开；堆生僻字不算文字质感",
        },
        {
            "rule": "结构失常 / 大段未推动情节",
            "hit": bool(hit("NO_HOOK", "FLAT_RHYTHM", "EXPOSITION_DUMP", "SUMMARY_ENDING")),
            "evidence": hit("NO_HOOK", "FLAT_RHYTHM", "EXPOSITION_DUMP", "SUMMARY_ENDING"),
            "advice": "章末留悬念（关系到完读率）；设定化进情节，别成段解释",
        },
    ]


def check_text(
    text: str,
    *,
    novel_id: str = "",
    chapter_number: int | None = None,
    title: str = "",
    target_words: tuple[int, int] = WORDS_PER_CHAPTER,
    style_profile: Any = None,
    voice_profile: Any = None,
    expect_title_line: bool = True,
) -> PublishCheck:
    """对一章正文做发布前检查（不落库、不改正文）。"""
    content = text or ""
    words = count_words(content)
    result = PublishCheck(
        novel_id=novel_id,
        chapter_number=chapter_number,
        title=title,
        word_count=words,
        words_per_chapter=target_words,
    )

    low, high = target_words
    if words < low:
        result.checks.append(
            _issue(
                "WORDS_BELOW_TARGET",
                "warning",
                f"正文 {words} 字，低于按章发布的常见长度（{low}–{high} 字）",
                f"补到 {low} 字以上再发；平台的有效更新与完读率都按字数算",
                value=words,
            )
        )
    elif words > high * 1.4:
        result.checks.append(
            _issue(
                "WORDS_ABOVE_TARGET",
                "info",
                f"正文 {words} 字，明显超过按章发布的常见长度（{low}–{high} 字）",
                "考虑拆成两章，长章在手机上的完读率通常更低",
                value=words,
            )
        )
    else:
        result.checks.append(
            _issue("WORDS_OK", "ok", f"字数 {words}，在常见区间内", "", value=words)
        )

    result.risks = find_risk_words(content)
    for risk in result.risks:
        result.checks.append(
            _issue(
                "RISK_WORD",
                # 风险词比文风警告严重：后果是审核不过或下架，所以算阻断项
                "error",
                f"命中「{risk['category']}」类风险词：{risk['word']}",
                risk["advice"],
                category=risk["category"],
                word=risk["word"],
                excerpt=risk["quote"],
            )
        )

    result.format_issues = find_format_issues(
        content, expected_title=title or "第N章 标题", expect_title_line=expect_title_line
    )
    result.checks.extend(result.format_issues)

    opening = opening_report(content)
    if opening.get("available"):
        label = "第一页" if (chapter_number or 1) == 1 else "开篇"
        if opening["slow_start"]:
            result.checks.append(
                _issue(
                    "OPENING_SLOW",
                    "warning",
                    opening["verdict"],
                    "第一页就给动作、对白或麻烦；背景留到后面边写边给",
                    rule="FIRST_PAGE",
                )
            )
        if opening["info_dump"]:
            result.checks.append(
                _issue(
                    "OPENING_INFO_DUMP",
                    "warning",
                    opening["info_dump_verdict"],
                    "把设定拆成三五句，或者改成人物对话里带出来",
                    explaining_per_1k=opening["explaining_per_1k"],
                    abstract_per_1k=opening["abstract_per_1k"],
                    rule="FIRST_PAGE",
                )
            )
        # 正文比一页还短时，「第一页」就是整章，页尾检查会与章末检查重复一遍
        if not opening["tail_hook_strong"] and len(content) > craft_rules.FIRST_PAGE_CHARS:
            result.checks.append(
                _issue(
                    "FIRST_PAGE_TAIL_FLAT",
                    "warning",
                    f"{label}最后一句没有留钩子（当前收尾：「{opening['last_sentence']}」）",
                    "页尾放一个悬着的问题、一个反常的举动，或一句戛然而止的台词",
                    excerpt=opening["last_sentence"],
                    rule="PAGE_END_HOOK",
                )
            )

    tail_text = _ending_beat(content)
    tail = page_tail_hook(tail_text)
    if not tail["strong"]:
        result.checks.append(
            _issue(
                "ENDING_NO_HOOK",
                "warning",
                f"章末缺少钩子（结尾这一拍：「{tail_text[-40:]}」）",
                "结尾留一个没答的问题、一次转折或一句截断的话——完读率主要靠这里",
                excerpt=tail_text[-60:],
                rule="CHAPTER_END_HOOK",
            )
        )
    elif tail["kind"] == "情绪" and style_service.measure(content).hook_score < 0.4:
        result.checks.append(
            _issue(
                "ENDING_HOOK_WEAK_KIND",
                "info",
                f"章末钩子类型偏软（{tail['kind']}）",
                "换成悬念、危机、揭示或反转这类更强的收尾，钩子强度会更好",
                rule="CHAPTER_END_HOOK",
            )
        )

    long_paragraphs = long_description_paragraphs(content)
    if long_paragraphs:
        result.checks.append(
            _issue(
                "DESCRIPTION_OVER_LIMIT",
                "warning",
                f"有 {len(long_paragraphs)} 段纯描写超过 {craft_rules.DESCRIPTION_LIMIT} 字"
                "（官方口径：超过一百字的风景和情绪描写要回头看看是不是水文）",
                "拆开：留一两句最有画面的，其余换成人物动作或对白",
                excerpt=long_paragraphs[0][:60],
                rule="DESCRIPTION_OVER_100",
            )
        )

    style_report = style_service.review_text(
        content, profile=style_profile, voice_profile=voice_profile
    )
    already = {item["code"] for item in result.checks}
    for issue in style_report.get("issues", []):
        if issue["code"] in ("TOO_SHORT", "BASELINE_STALE"):
            continue
        # 章末钩子这里已经按「最后一句的类型」判过了，不再重复报一遍文风层的同一条
        if issue["code"] == "NO_HOOK" and "ENDING_NO_HOOK" in already:
            continue
        if issue["level"] == "warning":
            result.checks.append(
                _issue(
                    f"STYLE_{issue['code']}",
                    "warning",
                    issue["message"],
                    issue.get("suggestion", ""),
                    excerpt=issue.get("excerpt", ""),
                )
            )

    result.platform_rules = _platform_rule_mapping(style_report)
    return result


def check_chapter(session: Session, chapter: Chapter, *, novel: Novel | None = None) -> dict[str, Any]:
    """对已入库的章节做发布前检查（口径取这本书自己的设置）。"""
    owner = novel or session.get(Novel, chapter.novel_id)
    target = WORDS_PER_CHAPTER
    if owner is not None:
        target = (owner.chapter_words_min, owner.chapter_words_max)
    report = check_text(
        chapter.content or "",
        novel_id=chapter.novel_id,
        chapter_number=chapter.chapter_number,
        title=f"第{chapter.chapter_number}章 {chapter.title or ''}".strip(),
        target_words=target,
        style_profile=style_service.default_profile(session, chapter.novel_id),
        voice_profile=style_service.default_voice_profile(session, chapter.novel_id),
        # 入库章节的标题在单独字段里（导出时会补），正文首行本来就不是标题行
        expect_title_line=False,
    )
    payload = report.to_dict()
    payload["chapter_id"] = chapter.id
    if owner is not None:
        payload["novel_title"] = owner.title
    return payload


# --------------------------------------------------------------------------- 导出
def export_text(
    session: Session,
    novel: Novel,
    *,
    from_chapter: int | None = None,
    to_chapter: int | None = None,
    strip_markdown: bool = True,
) -> str:
    """导出成可以直接粘贴到平台后台的纯文本：一章一段，标题单独一行。"""
    from sqlalchemy import select

    stmt = select(Chapter).where(Chapter.novel_id == novel.id)
    if from_chapter is not None:
        stmt = stmt.where(Chapter.chapter_number >= from_chapter)
    if to_chapter is not None:
        stmt = stmt.where(Chapter.chapter_number <= to_chapter)
    chapters = list(session.scalars(stmt.order_by(Chapter.chapter_number)))

    blocks: list[str] = []
    for chapter in chapters:
        heading = f"第{chapter.chapter_number}章 {chapter.title or ''}".strip()
        body = chapter.content or ""
        if strip_markdown:
            body = strip_markdown_text(body)
        blocks.append(f"{heading}\n\n{body.strip()}")
    return "\n\n\n".join(blocks)


def strip_markdown_text(text: str) -> str:
    """去掉 Markdown 标记，保留正文与空行结构。"""
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("#"):
            line = stripped.lstrip("#").strip()
        elif stripped.startswith(("- ", "* ", "+ ")):
            line = stripped[2:].strip()
        elif stripped.startswith(">"):
            line = stripped.lstrip(">").strip()
        line = line.replace("**", "").replace("`", "")
        lines.append(line)
    cleaned = "\n".join(lines)
    return re.sub(r"\n{4,}", "\n\n\n", cleaned).strip()
