"""文本规则工具：动作检测、状态判定、证据引用抽取。

ExtractorAgent 的离线提供者与 ContinuityChecker 的硬规则共用这里的实现，
保证「抽取」与「审校」对同一句话的判断是一致的。
"""

from __future__ import annotations

import re

from app.timeutil import split_sentences

TWO_CHAR_ACTIONS: tuple[str, ...] = (
    "按住", "沉声", "低声", "轻声", "冷冷", "缓缓", "开口", "说话", "说道", "说到", "沉着脸",
    "抬头", "低头", "点头", "摇头", "转身", "起身", "站起", "坐下", "挥手", "拔剑", "出手",
    "挡下", "拉住", "推开", "抓住", "握住", "冲出", "扑向", "后退", "苦笑", "冷笑", "沉默",
    "皱眉", "叹气", "递过", "接过", "扶起", "跪在", "拔起", "挥刀", "问道", "答道", "喊道",
    "喝道", "望着", "看向", "迈步", "走进", "走出", "踏入", "抬手", "抚摸", "拍在", "拦下",
    "挡在", "闪身", "侧身", "嗤笑", "冷哼", "俯身", "站定", "越过", "踏进", "握紧", "按下",
)

ONE_CHAR_ACTIONS = "说道笑走站坐看望问答喊喝叫握拉推抓扑退冲挡拦拔挥抬起跪拜叹"

_SKIP_PREFIX = re.compile(r"^(?:又|也|却|便|则|就|忽然|猛地|再次|终于|还是|只是|似乎|仿佛|仍旧|依旧|一时)")

_DEATH_TOKENS = ("死亡", "已死", "身亡", "战死", "气绝", "断气", "殒命", "死去", "死", "DEAD")

_STATUS_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"身亡|战死|气绝|断气|殒命|死了|死去|殉"), "死亡"),
    (re.compile(r"重伤昏迷|昏迷不醒|重伤"), "重伤昏迷"),
    (re.compile(r"苏醒|醒来|醒转|睁开眼|睁开眼睛|睁开了眼|眼睛睁|坐起来|坐起身|能坐"), "苏醒"),
)

_DAY_PATTERN = r"(?:初[零〇一二三四五六七八九十]|[零〇一二三四五六七八九十廿卅两\d]{1,3})"
_DATE_RE = re.compile(
    r"[\u4e00-\u9fa5]{0,2}?[零〇一二三四五六七八九十百两\d]+年(?:闰)?"
    r"[零〇一二三四五六七八九十廿卅两\d]{1,3}月"
    rf"(?:{_DAY_PATTERN}[日号]?)?"
)


def is_death_status(value: str | None) -> bool:
    text = (value or "").strip()
    if not text:
        return False
    if text.upper() in ("DEAD", "DECEASED"):
        return True
    return any(token in text for token in _DEATH_TOKENS)


def detect_action_in_sentence(sentence: str, name: str) -> str | None:
    """判断某个姓名在句子中是否紧接一个动作动词（返回该动词）。"""
    for match in re.finditer(re.escape(name), sentence):
        rest = sentence[match.end() : match.end() + 12]
        rest = _SKIP_PREFIX.sub("", rest)
        if not rest:
            continue
        for verb in TWO_CHAR_ACTIONS:
            if rest.startswith(verb):
                return verb
        if rest[0] in ONE_CHAR_ACTIONS:
            return rest[0]
    return None


def detect_action_in_text(content: str, name: str) -> tuple[str, str] | None:
    """在整章正文中找出该人物的动作，返回 (句子, 动词)。"""
    for sentence in split_sentences(content or ""):
        if name not in sentence:
            continue
        verb = detect_action_in_sentence(sentence, name)
        if verb:
            return sentence, verb
    return None


_WISH_MARKERS = "等要盼想望教别勿求"


def detect_status_change_in_sentence(sentence: str, name: str) -> str | None:
    for match in re.finditer(re.escape(name), sentence):
        # 「用来等王烈醒来」这类句子是愿望与等待，不是状态已发生
        prefix = sentence[max(0, match.start() - 6) : match.start()]
        if any(marker in prefix for marker in _WISH_MARKERS):
            continue
        window = sentence[match.end() : match.end() + 20]
        for pattern, value in _STATUS_RULES:
            if pattern.search(window):
                return value
    return None


def sentence_with(content: str, needle: str) -> str | None:
    """取出包含关键片段的那一句，作为证据引用（quote）。"""
    if not needle:
        return None
    for sentence in split_sentences(content or ""):
        if needle in sentence:
            return sentence.strip()[:160]
    return None


def find_dates(text: str) -> list[str]:
    return [match.group(0) for match in _DATE_RE.finditer(text or "")]


def ngrams(text: str, size: int = 2) -> set[str]:
    cleaned = re.sub(r"[^\u4e00-\u9fa5]", "", text or "")
    if len(cleaned) < size:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + size] for i in range(len(cleaned) - size + 1)}


def similarity(left: str, right: str) -> float:
    """按二元组重合度衡量两个中文短语的相似度（0~1）。"""
    grams = ngrams(left)
    if not grams:
        return 0.0
    return len(grams & ngrams(right)) / len(grams)


# ---------------------------------------------------------------------------
# 确定性属性抽取：正文里显式写出的设定句（佩剑/身份/修为/师承/状态/死因…）
# ExtractorAgent 的离线提供者与 ContinuityChecker 的硬规则共用这里的实现，
# 因此「模型没抽到」不会导致这类机器可判的矛盾被漏掉。
# ---------------------------------------------------------------------------
_FACT_PATTERNS_CACHE: dict[tuple[str, ...], list[tuple[re.Pattern[str], str, str | None]]] = {}

_TRAILING_BOUNDARY = r"(?=[，。；！？、,.!?]|$)"


def attribute_patterns(names: list[str]) -> list[tuple[re.Pattern[str], str, str | None]]:
    """按已知人物名生成属性句模式（缓存，避免重复编译）。"""
    key = tuple(names)
    cached = _FACT_PATTERNS_CACHE.get(key)
    if cached is not None:
        return cached
    alternation = "|".join(sorted((re.escape(name) for name in names), key=len, reverse=True))
    if not alternation:
        return []
    weapon_object = r"(?P<obj>[\u4e00-\u9fa5]{1,10}?(?:剑|刀|枪|戟|鞭|扇|印|珠|镜))"
    patterns: list[tuple[re.Pattern[str], str, str | None]] = [
        (
            re.compile(
                rf"(?P<subj>{alternation})(?:腰间|身上|手中|背上)?的"
                rf"(?P<pred>佩剑|佩刀|兵器|武器)[，,]?\s*"
                rf"(?:换成了|改成了|变成了|是|为|叫|名为|称作)\s*{weapon_object}"
            ),
            "佩剑",
            None,
        ),
        (
            re.compile(
                rf"(?P<subj>{alternation})(?:手中|腰间|身上)(?:悬|挂|握|持)着"
                rf"(?P<obj>[\u4e00-\u9fa5]{{1,10}}?(?:剑|刀|枪))"
            ),
            "佩剑",
            None,
        ),
        (
            re.compile(
                rf"(?P<subj>{alternation})(?:的)?(?:真实)?(?P<pred>身份)"
                rf"(?:其实|原来|本)?(?:是|为|乃是|竟是)\s*(?P<obj>[\u4e00-\u9fa5]{{2,14}}?){_TRAILING_BOUNDARY}"
            ),
            "身份",
            None,
        ),
        (
            re.compile(
                rf"(?P<subj>{alternation})(?:的)?(?P<pred>修为|境界)(?:已|已经)?"
                rf"(?:是|为|达到|突破至|突破到)\s*(?P<obj>[\u4e00-\u9fa5]{{2,8}}?){_TRAILING_BOUNDARY}"
            ),
            "修为",
            None,
        ),
        (
            re.compile(
                rf"(?P<subj>{alternation})(?:已|已经|终于)(?:成功)?突破(?:至|到)\s*"
                rf"(?P<obj>[\u4e00-\u9fa5]{{2,8}}?){_TRAILING_BOUNDARY}"
            ),
            "修为",
            None,
        ),
        (
            re.compile(
                rf"(?P<subj>{alternation})(?:的)?(?P<pred>父亲|母亲|师父|师尊)"
                rf"(?:是|叫|名为|乃是)\s*(?P<obj>[\u4e00-\u9fa5]{{2,4}})"
            ),
            "父亲",
            None,
        ),
        (
            re.compile(rf"(?P<subj>{alternation})(?:拜|认)(?P<obj>[\u4e00-\u9fa5]{{2,4}})为师"),
            "师承",
            None,
        ),
        (
            re.compile(
                rf"(?P<subj>{alternation})(?:已经|早已|终于|也|就)?(?:身亡|战死|气绝|断气|殒命|死了|死去)"
            ),
            "状态",
            "死亡",
        ),
        (
            re.compile(
                rf"(?P<subj>{alternation})[^，。]{{0,12}}?被(?P<obj>[\u4e00-\u9fa5]{{2,4}})(?:所)?(?:杀|杀害|杀死)"
            ),
            "死因",
            None,
        ),
        (
            re.compile(rf"(?P<subj>{alternation})(?:已|已经)?(?:重伤昏迷|昏迷不醒)"),
            "状态",
            "重伤昏迷",
        ),
    ]
    _FACT_PATTERNS_CACHE[key] = patterns
    return patterns


def extract_attribute_facts(content: str, names: list[str]) -> list[dict[str, str]]:
    """从正文里抽出显式属性句，返回 [{subject, predicate, object, sentence}]。"""
    facts: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    patterns = attribute_patterns(names)
    if not patterns:
        return facts
    for sentence in split_sentences(content or ""):
        for pattern, default_predicate, forced_object in patterns:
            for match in pattern.finditer(sentence):
                groups = match.groupdict()
                subject = (groups.get("subj") or "").strip()
                predicate = (groups.get("pred") or default_predicate).strip()
                obj = (forced_object or groups.get("obj") or "").strip()
                obj = re.sub(r"(的|了|吗|吧)$", "", obj)
                if not subject or not predicate or not obj:
                    continue
                key = (subject, predicate, obj)
                if key in seen:
                    continue
                seen.add(key)
                facts.append(
                    {
                        "subject": subject,
                        "predicate": predicate,
                        "object": obj,
                        "sentence": sentence.strip()[:160],
                    }
                )
    return facts


# ---------------------------------------------------------------------------
# 日期与事件的确定性关联
# ---------------------------------------------------------------------------
def event_name_overlap(name: str, text: str) -> int:
    """事件名与文本的最长连续重合片段长度（忽略非汉字字符）。"""
    cleaned = re.sub(r"[^\u4e00-\u9fa5]", "", name or "")
    if len(cleaned) < 2:
        return 0
    best = 0
    for size in range(len(cleaned), 1, -1):
        if size <= best:
            break
        for start in range(len(cleaned) - size + 1):
            if cleaned[start : start + size] in text:
                best = size
                break
    return best


def required_event_overlap(name: str, minimum: int = 5) -> int:
    cleaned = re.sub(r"[^\u4e00-\u9fa5]", "", name or "")
    return max(3, min(minimum, len(cleaned) - 1))


def match_event_name(paragraph: str, window: str, known_events: list[str], minimum: int = 5) -> str:
    """把一段带日期的正文归到某个既有事件名上；无法可靠归属时返回空串。

    要求事件名与文本存在足够长的**连续**重合片段：章节开篇常写「天启三年四月初六，青云山」，
    其中「青云」「青云剑宗」这类泛用词会造成误归属，进而产生假的时间线冲突。
    """
    haystack = f"{paragraph}\n{window}"
    best_name, best_score, best_required = "", 0, 0
    for name in known_events:
        score = event_name_overlap(name, haystack)
        if score > best_score:
            best_name, best_score, best_required = name, score, required_event_overlap(name, minimum)
    if best_name and best_score >= best_required:
        return best_name
    return ""


def extract_dated_mentions(content: str, known_events: list[str]) -> list[dict[str, str]]:
    """抽出「正文里出现的日期 → 归属事件名」的候选，供离线抽取与审校硬规则共用。"""
    paragraphs = [paragraph.strip() for paragraph in (content or "").split("\n") if paragraph.strip()]
    mentions: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, paragraph in enumerate(paragraphs):
        for match in _DATE_RE.finditer(paragraph):
            story_time = match.group(0)
            window = "\n".join(paragraphs[max(0, index - 2) : index + 3])
            event = match_event_name(paragraph, window, known_events)
            key = (story_time, event)
            if key in seen:
                continue
            seen.add(key)
            mentions.append(
                {"story_time": story_time, "event": event, "paragraph": paragraph[:120]}
            )
    return mentions
