"""文风度量引擎：把「AI 味」拆成可计算的指标。

设计原则：
1. **全部指标都是确定性的**（正则与统计，不依赖模型），因此可以进测试、可以做回归对比；
2. **对比的是作者自己的基线**：一本书写了几十章之后，作者认可的章节就是这本书的「味儿」，
   新章偏离这本书的分布才是问题，而不是偏离某个通用标准；
3. 词表与阈值都是**启发式**，没有任何一项能单独判定「AI 味」——
   它们的价值在于可追踪：同一指标在修订前后是升还是降，是可以用数字说话。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from math import log2
from statistics import mean, pstdev
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Chapter, ChapterStatus, Novel, StyleProfile
from app.services.text_rules import ONE_CHAR_ACTIONS, TWO_CHAR_ACTIONS
from app.timeutil import count_words, split_sentences

METRICS_VERSION = "0.6"

#: AI 味与网文套话：密度高、种类少、反复出现在同一章里
CLICHE_PATTERNS: tuple[str, ...] = (
    "眼中闪过一丝", "眼中精光", "嘴角勾起", "嘴角微微", "不由", "不禁", "心中一惊", "心头一震",
    "瞳孔微缩", "瞳孔一缩", "深吸一口气", "深吸了一口气", "淡淡地", "冷冷地", "缓缓地", "深深地",
    "不易察觉", "几不可闻", "若有所思", "意味深长", "沉默了片刻", "沉默片刻", "陷入了沉思",
    "眉头微皱", "眉头一皱", "挑了挑眉", "微微颔首", "不置可否", "如释重负", "不动声色",
    "眼中寒意", "泛起一丝", "扯出一个", "扯了扯嘴角", "心念电转", "心中暗道", "眼中复杂",
    "说不清道不明", "难以言喻", "一股莫名", "没来由地", "下意识地", "本能地",
)

#: 抽象名词/形容堆砌：用得多就显得空
ABSTRACT_PATTERNS: tuple[str, ...] = (
    "气息", "杀意", "寒意", "暖意", "威压", "气场", "气势", "氛围", "存在感", "压迫感",
    "心境", "心绪", "情绪", "内心深处", "某种", "一股", "那份", "一股脑", "无形的",
    "深邃", "幽深", "难以捉摸", "复杂", "莫名", "微妙",
)

#: 解释性/总结性叙述（telling）：网文靠展示推进，这类句子一多就出戏
EXPLAINING_PATTERNS: tuple[str, ...] = (
    "他明白", "他知道", "她明白", "他知道", "他意识到", "他明白了", "原来", "这意味着",
    "也就是说", "他心里清楚", "他忽然想到", "他不禁想", "说明", "事实上", "这代表着",
    "由此可见", "在那一刻", "这一刻", "从某种意义上", "他懂了",
)

#: 张力/冲突信号（节奏指标用）
CONFLICT_PATTERNS: tuple[str, ...] = (
    "死", "血", "杀", "打", "喝", "吼", "刀", "剑", "枪", "轰", "爆", "怒", "惊", "急",
    "危", "逃", "追", "拦", "挡", "断", "裂", "痛", "嘶", "逃", "抓", "踹", "摔",
)

#: 章末钩子信号：疑问、转折、悬置
HOOK_PATTERNS: tuple[str, ...] = (
    "突然", "却", "可是", "然而", "竟然", "究竟", "是谁", "为什么", "不知", "下一刻",
    "就在这时", "一声", "来的人", "不该", "没有回答", "还没", "未曾", "直到", "而", "只",
)

DIALOGUE_RE = re.compile(r"[“「][^”」]{1,200}[”」]")
SUBJECT_OPENERS = ("他", "她", "它", "他们", "她们", "林默", "王烈", "苏月宁", "赵铁山", "血无痕")

#: 个性维度用的符号与口语标记
PUNCTUATION_SET = ("，", "。", "？", "！", "…", "—", "、", "：", "；", "“")
COLLOQUIAL_MARKERS = ("呢", "吧", "啊", "嘛", "哦", "呀", "诶", "嘿", "嗯", "罢了", "而已", "来着", "倒是")

#: 情节推进信号（V0.5）：决定、交易、得失、冲突、交代这些才是「剧情在走」
ADVANCEMENT_MARKERS = (
    "决定", "答应", "拒绝", "同意", "发誓", "立誓", "交代", "告诉", "承认", "否认",
    "答应过", "交出", "拿走", "夺", "抢", "毁", "烧", "杀", "死", "伤", "救", "追",
    "逃", "拦", "挡", "抓住", "放开", "放下", "收下", "带走", "逃出", "闯入", "闯进",
    "动手", "出手", "拔剑", "出鞘", "破", "断", "裂", "炸", "轰", "倒", "跪",
    "约定", "见面", "离开", "返回", "上山", "下山", "进城", "出城", "开门", "关门",
    "发现", "查到", "找到", "暴露", "揭穿", "供出", "招了", "认出", "识破",
)

#: 名词解释 / 设定说明的句式标记（V0.5）
EXPOSITION_MARKERS = (
    "所谓", "指的是", "也就是", "即是", "意为", "意思是", "叫作", "称为", "被称为",
    "来源于", "源于", "据说", "据传", "相传", "事实上", "严格来说", "从某种意义上",
    "分为", "共分", "一共有", "大致可分", "包括如下", "具体而言", "换言之",
)

#: 情绪强度信号（V0.5）：用于段落级的「情绪曲线」起伏判断
EMOTION_MARKERS = (
    "！", "？", "…", "—", "不", "别", "为什么", "怎么", "竟", "居然", "难道",
    "死", "血", "痛", "怕", "怒", "笑", "哭", "喊", "吼", "叫",
)

#: 总结/回扣句（V0.6）：演过的事再概括一遍。读者刚要自己体会，被作者抢先说了。
SUMMARY_PATTERNS = (
    "这一切", "这一切的", "从此以后", "从那以后", "从那天起", "往后的日子", "往后的路",
    "他明白了", "他终于明白", "他懂了", "他终于知道", "原来如此", "说到底", "总的来说",
    "就这样结束", "多年以后", "后来他才知道", "后来才知道", "日后的", "这一夜注定",
)
#: 升华/议论（V0.6）：把具体的事拔高成道理
ELEVATION_PATTERNS = (
    "所谓", "不过是", "人这一生", "这世上", "这世间", "有些人", "有些东西", "有些事",
    "真正的", "才是真正的", "终究是", "本就是", "无非是", "或许这就是", "大概这就是",
    "这就是江湖", "这就是命", "命中注定", "谁能想到", "值得吗", "又能如何",
)
#: 平滑过渡（V0.6）：把两件事顺滑地粘在一起，读者感觉不到跳跃
TRANSITION_PATTERNS = (
    "于是", "就这样", "接下来", "随后", "紧接着", "与此同时", "第二天", "次日",
    "此后", "从那之后", "不知不觉", "时间一晃", "转眼", "不多时", "片刻之后",
    "顺理成章", "果不其然", "便也", "也就此",
)
#: 「抽象替代具体」用的具体细节标记：数字量词或可见的物件
CONCRETE_MARKERS = (
    "剑", "刀", "枪", "门", "窗", "石", "血", "水", "火", "灯", "碗", "绳", "衣", "袖",
    "脚", "手", "眼", "肩", "崖", "树", "雪", "雨", "铜", "铁", "木", "布", "纸", "灰",
    "泥", "河", "山", "路", "车", "马", "桌", "椅", "墙", "屋", "箱", "袋", "酒", "饭", "茶",
)
_CONCRETE_NUMBER_RE = re.compile(r"[0-9零一二三四五六七八九十百千万两半几]")

#: 声音特征词用的停用词：只滤掉纯功能词，像「忽然/仿佛/终于」这类用词习惯要保留下来
_VOICE_STOPGRAMS = {
    "他的", "她的", "它第", "他们", "她们", "我们", "你们", "我的", "你的", "这个", "那个",
    "一个", "什么", "没有", "已经", "自己", "不是", "可以", "还是", "然后", "可是", "现在",
    "时候", "怎么", "因为", "所以", "如果", "只是", "一样", "知道", "的话", "起来", "出来",
    "过去", "下来", "进来", "出去", "之后", "之前", "里面", "外面", "上面", "下面", "旁边",
    "但是", "不过", "虽然", "于是", "而且", "还有", "就是", "只有", "其中", "这样", "那样",
    "的时", "了的", "是他", "是她", "不是他", "在了", "一点", "一次", "一天",
}


@dataclass
class StyleMetrics:
    total_chars: int = 0
    sentence_count: int = 0
    paragraph_count: int = 0
    mean_sentence_len: float = 0.0
    sentence_len_cv: float = 0.0
    burstiness: float = 0.0
    short_sentence_ratio: float = 0.0
    long_sentence_ratio: float = 0.0
    paragraph_len_cv: float = 0.0
    dialogue_ratio: float = 0.0
    dialogue_paragraph_ratio: float = 0.0
    cliche_per_1k: float = 0.0
    cliche_variety: float = 0.0
    abstract_per_1k: float = 0.0
    explaining_per_1k: float = 0.0
    conflict_per_1k: float = 0.0
    subject_repeat_ratio: float = 0.0
    opener_diversity: float = 0.0
    self_repeat_ratio: float = 0.0
    hook_score: float = 0.0
    #: 以下五项是「个性」维度：不是缺点，而是作者的指纹（V0.4）
    punctuation_variety: float = 0.0
    short_paragraph_ratio: float = 0.0
    dash_ellipsis_per_1k: float = 0.0
    colloquial_per_1k: float = 0.0
    question_ratio: float = 0.0
    #: 以下七项是「推进与节奏」维度（V0.5）：针对注水、解释腔、标点均匀、句式循环
    advancement_per_1k: float = 0.0
    filler_paragraph_ratio: float = 0.0
    exposition_per_1k: float = 0.0
    punctuation_entropy: float = 0.0
    emotion_flatness: float = 0.0
    pattern_loop_ratio: float = 0.0
    pattern_top: list[str] = field(default_factory=list)
    #: 以下七项是「用词与收束」维度（V0.6）：针对可预测的用词、总结升华、平滑过渡、抽象替代具体
    word_ttr: float = 0.0
    word_concentration: float = 0.0
    verb_variety: float = 0.0
    novel_bigram_ratio: float = 0.0
    novel_char_ratio: float = 0.0
    summary_per_1k: float = 0.0
    elevation_per_1k: float = 0.0
    transition_per_1k: float = 0.0
    abstract_unsupported_ratio: float = 0.0
    hook_signals: list[str] = field(default_factory=list)
    cliche_hits: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


#: 指标方向：higher_better / lower_better / range（区间内最好）
#: 注意 burstiness 与 conflict_per_1k 记作 range：把长句拆短、把「杀意」这类词删掉
#: 都会让它们下降，但那恰恰是正确的改法，不能算「变差」。
METRIC_DIRECTIONS: dict[str, str] = {
    "burstiness": "range",
    "sentence_len_cv": "range",
    "short_sentence_ratio": "range",
    "long_sentence_ratio": "lower_better",
    "paragraph_len_cv": "range",
    "dialogue_ratio": "range",
    "dialogue_paragraph_ratio": "range",
    "cliche_per_1k": "lower_better",
    "cliche_variety": "higher_better",
    "abstract_per_1k": "lower_better",
    "explaining_per_1k": "lower_better",
    "conflict_per_1k": "range",
    "subject_repeat_ratio": "lower_better",
    "opener_diversity": "higher_better",
    "self_repeat_ratio": "lower_better",
    "hook_score": "higher_better",
    "punctuation_variety": "range",
    "short_paragraph_ratio": "range",
    # V0.5 推进与节奏
    "advancement_per_1k": "higher_better",
    "filler_paragraph_ratio": "lower_better",
    "exposition_per_1k": "lower_better",
    "punctuation_entropy": "higher_better",
    "emotion_flatness": "lower_better",
    "pattern_loop_ratio": "lower_better",
    # V0.6 用词与收束
    "word_ttr": "higher_better",
    "word_concentration": "lower_better",
    "verb_variety": "higher_better",
    "novel_bigram_ratio": "range",
    "novel_char_ratio": "lower_better",
    "summary_per_1k": "lower_better",
    "elevation_per_1k": "lower_better",
    "transition_per_1k": "lower_better",
    "abstract_unsupported_ratio": "lower_better",
}

#: 在没有任何基线时使用的宽松经验阈值（越界只给提示，不当硬错误）
DEFAULT_RANGES: dict[str, tuple[float, float]] = {
    "burstiness": (0.55, 1.6),
    "long_sentence_ratio": (0.0, 0.22),
    "dialogue_ratio": (0.18, 0.65),
    "cliche_per_1k": (0.0, 2.5),
    "abstract_per_1k": (0.0, 12.0),
    "explaining_per_1k": (0.0, 14.0),
    "subject_repeat_ratio": (0.0, 0.55),
    "self_repeat_ratio": (0.0, 0.06),
    "hook_score": (0.25, 1.0),
    "advancement_per_1k": (2.0, float("inf")),
    "filler_paragraph_ratio": (0.0, 0.3),
    "exposition_per_1k": (0.0, 4.0),
    "punctuation_entropy": (0.34, 1.0),
    "emotion_flatness": (0.0, 0.72),
    "pattern_loop_ratio": (0.0, 0.3),
    # V0.6：阈值按 20 章种子实测标定（见 tests/test_v6_wording.py 的边界断言）
    "word_ttr": (0.80, 1.0),
    "word_concentration": (0.0, 0.22),
    "verb_variety": (0.26, 1.0),
    "summary_per_1k": (0.0, 2.5),
    "elevation_per_1k": (0.0, 3.0),
    "transition_per_1k": (0.0, 6.0),
    "abstract_unsupported_ratio": (0.0, 0.45),
}
#: 这两个「用词飘不飘」的指标随本书推进系统性下降（早期章节本来就会引入大量新字新搭配），
#: 相对窗口会误伤开头几章，所以只用绝对上限判断「满篇生僻字」，不进逐章漂移统计。
NOVEL_CHAR_CAP = 0.35

#: 容差下限：基线本身离散度大时，不能让窗口宽到「怎么都合格」
MIN_TOLERANCE: dict[str, float] = {
    "burstiness": 0.15,
    "sentence_len_cv": 0.1,
    "long_sentence_ratio": 0.08,
    "short_sentence_ratio": 0.08,
    "dialogue_ratio": 0.1,
    "dialogue_paragraph_ratio": 0.1,
    "cliche_per_1k": 0.8,
    "cliche_variety": 0.1,
    "abstract_per_1k": 3.0,
    "explaining_per_1k": 4.0,
    "conflict_per_1k": 2.0,
    "subject_repeat_ratio": 0.15,
    "opener_diversity": 0.1,
    "self_repeat_ratio": 0.02,
    "paragraph_len_cv": 0.15,
    "hook_score": 0.2,
    "advancement_per_1k": 1.5,
    "filler_paragraph_ratio": 0.08,
    "exposition_per_1k": 1.5,
    "punctuation_entropy": 0.1,
    "emotion_flatness": 0.1,
    "pattern_loop_ratio": 0.08,
    "word_ttr": 0.05,
    "word_concentration": 0.05,
    "verb_variety": 0.06,
    "summary_per_1k": 1.0,
    "elevation_per_1k": 1.0,
    "transition_per_1k": 1.5,
    "abstract_unsupported_ratio": 0.12,
}


def _hits(text: str, patterns: tuple[str, ...]) -> dict[str, int]:
    found: dict[str, int] = {}
    for pattern in patterns:
        count = text.count(pattern)
        if count:
            found[pattern] = count
    return found


def _per_1k(count: int, chars: int) -> float:
    return round(count * 1000 / chars, 2) if chars else 0.0


def _ngram_repeat_ratio(text: str, size: int = 4) -> float:
    cleaned = re.sub(r"[^\u4e00-\u9fa5]", "", text or "")
    if len(cleaned) < size * 4:
        return 0.0
    grams = [cleaned[i : i + size] for i in range(len(cleaned) - size + 1)]
    unique = len(set(grams))
    return round(1 - unique / len(grams), 4)


def _hook(text: str) -> tuple[float, list[str]]:
    """章末钩子：最后 200 字里有没有悬念、转折、未答的问题。"""
    tail = (text or "").strip()[-200:]
    if not tail:
        return 0.0, []
    signals = [pattern for pattern in HOOK_PATTERNS if pattern in tail]
    score = min(1.0, 0.25 * len(signals))
    if "？" in tail or "?" in tail:
        score = min(1.0, score + 0.3)
        signals.append("章末疑问")
    if DIALOGUE_RE.search(tail):
        score = min(1.0, score + 0.15)
        signals.append("章末对白")
    if tail.rstrip().endswith(("——", "…", "...")):
        score = min(1.0, score + 0.2)
        signals.append("句子被截断")
    return round(score, 3), signals


#: 判断一段话「有没有戏」用的动作字。比人物动作表更窄：
#: 去掉「起」这类过泛的字（雾气升起），也去掉「道/拉」这类在景物里常见的字（拉出一道影子），
#: 否则纯景物段永远判不出来。判不出「注水」比误判一两段更糟。
_FILLER_ACTION_CHARS = frozenset(ONE_CHAR_ACTIONS) - {"起", "道", "拉"}
#: 同上，二字动作里排除纯神态／方式词：「冷冷地立着」不是剧情动作
_FILLER_MANNER_WORDS = frozenset(
    {"冷冷", "缓缓", "沉声", "低声", "轻声", "沉着脸", "沉默", "苦笑", "冷笑", "皱眉", "叹气", "嗤笑", "冷哼"}
)

#: 收句标点：用于句式骨架的「怎么收尾」这一维
SENTENCE_END_MARKS = ("。", "？", "！", "…", "—")


def _has_story_action(paragraph: str) -> bool:
    """段落里有没有推进性动作：动作词、单字动词，或推进标记。"""
    if any(marker in paragraph for marker in ADVANCEMENT_MARKERS):
        return True
    if any(
        verb in paragraph
        for verb in TWO_CHAR_ACTIONS
        if verb not in _FILLER_MANNER_WORDS
    ):
        return True
    return any(ch in _FILLER_ACTION_CHARS for ch in paragraph)


def _sentence_skeleton(sentence: str) -> str:
    """句子的骨架：开头三字 + 收尾标点 + 长度档，用来找「固定句式循环」。"""
    body = sentence.strip()
    end = body[-1] if body and body[-1] in SENTENCE_END_MARKS else ""
    core = re.sub(r"[^\u4e00-\u9fa5]", "", body)
    return f"{core[:3]}|{end}|{len(core) // 12}"


def _pattern_loop(sentences: list[str]) -> tuple[float, list[str]]:
    """重复骨架占比与最集中的几个骨架（只统计真正重复过的）。"""
    if len(sentences) < 8:
        return 0.0, []
    counts = Counter(_sentence_skeleton(s) for s in sentences)
    total = len(sentences)
    ratio = round(1 - len(counts) / total, 3)
    top = [f"{skeleton} ×{count}" for skeleton, count in counts.most_common(3) if count >= 3]
    return ratio, top


def _punctuation_entropy(text: str) -> float:
    """标点类型分布的归一化香农熵。全靠「，。」维持的稿子熵很低，情绪标点用不上。"""
    marks = [ch for ch in text if ch in PUNCTUATION_SET]
    if len(marks) < 8:
        return 0.0
    total = len(marks)
    entropy = -sum((count / total) * log2(count / total) for count in Counter(marks).values())
    return round(entropy / log2(len(PUNCTUATION_SET)), 3)


def _emotion_flatness(paragraphs: list[str]) -> float:
    """段落情绪强度分「无 / 弱 / 强」三档后的集中度：全挤在一档 = 1，三档都铺开 = 0。

    用离散度（变异系数）会被少数爆发段拉爆而永远贴近 0，分档后才有区分度：
    整章都平、或整章都炸，都算「没有起伏」。
    """
    if not paragraphs:
        return 1.0
    buckets = [0, 0, 0]
    for paragraph in paragraphs:
        hits = sum(paragraph.count(marker) for marker in EMOTION_MARKERS)
        intensity = hits * 100 / max(count_words(paragraph), 1)
        buckets[0 if hits == 0 else (1 if intensity < 2.0 else 2)] += 1
    total = sum(buckets)
    if total == 0:
        return 1.0
    entropy = -sum((count / total) * log2(count / total) for count in buckets if count)
    return round(max(0.0, 1 - entropy / log2(3)), 3)


def _char_bigrams(text: str) -> list[str]:
    """汉字二元组序列：当作「词」的近似，用于多样性统计（不依赖分词器）。"""
    cleaned = re.sub(r"[^\u4e00-\u9fa5]", "", text or "")
    return [cleaned[i : i + 2] for i in range(len(cleaned) - 1)]


#: 多样性统计的滑动窗口（字）：按窗口平均，避免「文本越长类符比越低」的长度惩罚
WORD_WINDOW = 200


def _windowed_word_stats(text: str) -> tuple[float, float]:
    """滑动窗口内的类符/形符比与高频词集中度（各窗口均值）。

    ttr 低 = 翻来覆去就那几个词（可预测）；集中度高 = 少数几个词撑起全篇。
    """
    grams = _char_bigrams(text)
    if len(grams) < 20:
        return 0.0, 0.0
    ttrs: list[float] = []
    concentrations: list[float] = []
    for start in range(0, len(grams), WORD_WINDOW):
        chunk = grams[start : start + WORD_WINDOW]
        if len(chunk) < 20:
            continue
        counts = Counter(chunk)
        ttrs.append(len(counts) / len(chunk))
        concentrations.append(sum(count for _gram, count in counts.most_common(10)) / len(chunk))
    if not ttrs:
        return 0.0, 0.0
    return round(mean(ttrs), 3), round(mean(concentrations), 3)


def _verb_variety(text: str) -> float:
    """动作动词的类符/形符比：同一个动词被反复用（说、看、走）就会很低。"""
    hits: list[str] = []
    for word in TWO_CHAR_ACTIONS:
        hits.extend([word] * (text or "").count(word))
    for char in ONE_CHAR_ACTIONS:
        hits.extend([char] * (text or "").count(char))
    if len(hits) < 8:
        return 0.0
    return round(len(set(hits)) / len(hits), 3)


def _known_bigrams(texts: list[str]) -> set[str]:
    """既往文本里出现过的全部汉字二元组（用于判断本章用词是不是「全新的」）。"""
    known: set[str] = set()
    for content in texts or []:
        known.update(_char_bigrams(content))
    return known


def novel_bigram_ratio(text: str, known: set[str] | None) -> float:
    """本章二元组中，既往章节里从没出现过的占比。

    注意：这个值会随本书词表积累而系统性下降（第 2 章远高于第 20 章），
    所以只用作**参考值**，判定用 ``novel_char_ratio``。
    """
    if not known:
        return 0.0
    grams = _char_bigrams(text)
    if len(grams) < 20:
        return 0.0
    return round(sum(1 for gram in grams if gram not in known) / len(grams), 3)


def _chars(text: str) -> list[str]:
    return list(re.sub(r"[^\u4e00-\u9fa5]", "", text or ""))


def _known_chars(texts: list[str]) -> set[str]:
    """既往文本里出现过的全部汉字。"""
    known: set[str] = set()
    for content in texts or []:
        known.update(_chars(content))
    return known


def novel_char_ratio(text: str, known: set[str] | None) -> float:
    """本章里「本书从没用过的字」占全部汉字的比例（相对此前所有章节）。

    这是「用词是不是飘了」的护栏：字比词稳定得多，正常章节绝大多数用字都是本书已有
    的（新人名地名会带进来几个新字，比例很低）；一旦满篇生僻字、堆砌辞藻，这个值会
    明显抬起来。太低则说明在用同一批字打转 —— 所以它是**区间**指标，
    我们看的是「意外但没飘」，而不是一味追求罕见用词。
    """
    if not known:
        return 0.0
    chars = _chars(text)
    if len(chars) < 50:
        return 0.0
    fresh = sum(1 for char in chars if char not in known)
    return round(fresh / len(chars), 4)


def novel_char_ratios(texts: list[str]) -> list[float]:
    """按顺序逐章算「相对此前所有章节的新字占比」，一次遍历完成。"""
    known: set[str] = set()
    ratios: list[float] = []
    for content in texts:
        ratios.append(novel_char_ratio(content, known) if known else 0.0)
        known.update(_chars(content))
    return ratios


def _abstract_unsupported(text: str) -> float:
    """含抽象词、却没有任何具体细节（动作／对白／数字／可见物件）的句子占比。"""
    sentences = [s for s in split_sentences(text or "") if s.strip()]
    abstract_sentences = [
        sentence for sentence in sentences if any(marker in sentence for marker in ABSTRACT_PATTERNS)
    ]
    if len(abstract_sentences) < 3:
        return 0.0
    unsupported = sum(
        1
        for sentence in abstract_sentences
        if not _has_story_action(sentence)
        and not DIALOGUE_RE.search(sentence)
        and not _CONCRETE_NUMBER_RE.search(sentence)
        and not any(marker in sentence for marker in CONCRETE_MARKERS)
    )
    return round(unsupported / len(abstract_sentences), 3)


def _first_matching_sentence(text: str, patterns: tuple[str, ...]) -> str:
    for sentence in split_sentences(text or ""):
        if any(pattern in sentence for pattern in patterns):
            return sentence.strip()[:60]
    return ""


def _hits_in(text: str, patterns: tuple[str, ...]) -> list[str]:
    """按出现顺序列出命中的标记（去重后给证据用）。"""
    seen: list[str] = []
    for sentence in split_sentences(text or ""):
        for pattern in patterns:
            if pattern in sentence and pattern not in seen:
                seen.append(pattern)
    return seen


# --------------------------------------------------------------------------- 用词与收束规则（V0.6）
#: 桥段/收尾套话（V0.6）：这些短语本身不违规，但**同一本里反复用**就是桥段同质化
BRIDGE_PATTERNS: tuple[str, ...] = (
    "深吸一口气", "深吸了一口气", "转身就走", "转身离开", "转身离去", "拂袖而去",
    "一言不发", "不再多言", "沉默了片刻", "沉默片刻", "若有所思", "不置可否",
    "目光落在", "目光扫过", "视线落在", "缓缓开口", "淡淡开口", "微微一笑", "冷笑一声",
    "冷哼一声", "摇了摇头", "点了点头", "叹了口气", "皱了皱眉", "闭上了眼",
    "消失在夜色", "留下一个背影", "没人知道", "谁也没有注意到", "夜更深了", "天色渐晚",
    "不知不觉", "不知过了多久", "夜已深", "天光大亮",
)
#: 一个桥段短语要在多少「既往章节」里出现过，才算同质化（太少会误伤正常用词）
BRIDGE_REPEAT_CHAPTERS = 4


def bridge_repeat_issues(
    text: str, prior_texts: list[str] | None, *, labels: list[str] | None = None
) -> list[StyleIssue]:
    """跨章桥段重复：本章用的桥段短语，在本书此前已经反复用过。

    判据是「在多少个**既往章节**里出现过」而不是出现次数 —— 同一章里用两次是措辞问题
    （由章内规则管），跨章反复用才是桥段同质化。
    """
    if not prior_texts:
        return []
    chapter_hits: dict[str, list[str]] = {}
    for index, content in enumerate(prior_texts):
        for pattern in BRIDGE_PATTERNS:
            if pattern in (content or ""):
                chapter_hits.setdefault(pattern, []).append(_prior_label(labels, index))
    issues: list[StyleIssue] = []
    for pattern in BRIDGE_PATTERNS:
        if pattern not in (text or ""):
            continue
        chapters = chapter_hits.get(pattern, [])
        if len(chapters) < BRIDGE_REPEAT_CHAPTERS:
            continue
        issues.append(
            StyleIssue(
                code="BRIDGE_REPEAT",
                level="warning",
                metric="bridge_repeat",
                message=(
                    f"桥段同质化：「{pattern}」在本书已用过 {len(chapters)} 章"
                    f"（{('、'.join(chapters[:6]))}），本章又出现了一次"
                ),
                value=float(len(chapters)),
                reference=float(BRIDGE_REPEAT_CHAPTERS),
                excerpt=_first_matching_sentence(text, (pattern,)),
                suggestion="换一个只属于本章的收束方式：这次他是怎么做的？换个动作或直接切场景",
            )
        )
    return issues


def _wording_issues(
    text: str,
    metrics: StyleMetrics,
    profile_metrics: dict[str, Any] | None,
    *,
    scale: float = 1.0,
    known_chars: set[str] | None = None,
) -> list[StyleIssue]:
    """V0.6 八项：用词可预测、动词复用、生僻字飘、总结、升华、过渡太顺、抽象替代具体。"""
    issues: list[StyleIssue] = []

    def bounds(metric: str) -> tuple[float, float, float | None]:
        low, high = _threshold(
            profile_metrics, metric, DEFAULT_RANGES.get(metric, (0.0, float("inf"))), scale=scale
        )
        reference = None
        if isinstance(profile_metrics, dict) and isinstance(profile_metrics.get(metric), dict):
            reference = float(profile_metrics[metric]["mean"])
        return low, high, reference

    def detail(reference: float | None) -> str:
        return f"（本书基线 {reference}）" if reference is not None else "（经验阈值）"

    low, _high, reference = bounds("word_ttr")
    if metrics.word_ttr and metrics.word_ttr < low:
        issues.append(
            StyleIssue(
                code="PREDICTABLE_WORDING",
                level="warning",
                metric="word_ttr",
                message=(
                    f"用词可预测：滑动窗口内的用词多样性只有 {metrics.word_ttr}"
                    f"{detail(reference)}，下限 {round(low, 3)}"
                ),
                value=metrics.word_ttr,
                reference=reference,
                suggestion="同一件事换一种说法；把最常出现的那几个词换掉，别让它们撑起全篇",
            )
        )

    _low, high, reference = bounds("word_concentration")
    if metrics.word_concentration > high:
        issues.append(
            StyleIssue(
                code="REPETITIVE_VOCABULARY",
                level="warning",
                metric="word_concentration",
                message=(
                    f"少数词撑起全篇：最高频的十个搭配占了 {round(metrics.word_concentration * 100, 1)}%"
                    f"{detail(reference)}，上限 {round(high, 3)}"
                ),
                value=metrics.word_concentration,
                reference=reference,
                suggestion="把反复出现的那几个搭配分散开：换动词、换比喻的落点、换观察角度",
            )
        )

    low, _high, reference = bounds("verb_variety")
    if metrics.verb_variety and metrics.verb_variety < low:
        issues.append(
            StyleIssue(
                code="VERB_MONOTONE",
                level="warning",
                metric="verb_variety",
                message=(
                    f"动词翻来覆去就几个：动词多样性 {metrics.verb_variety}"
                    f"{detail(reference)}，下限 {round(low, 3)}"
                ),
                value=metrics.verb_variety,
                reference=reference,
                suggestion="把「看、说、走、拿」换成更具体的动作：怎么看的、怎么说的、往哪走的",
            )
        )

    if metrics.novel_char_ratio and metrics.novel_char_ratio > NOVEL_CHAR_CAP:
        fresh = [char for char in dict.fromkeys(_chars(text)) if known_chars and char not in known_chars]
        issues.append(
            StyleIssue(
                code="WORD_DRIFT",
                level="warning",
                metric="novel_char_ratio",
                message=(
                    f"用词飘出本书语感：本章有 {round(metrics.novel_char_ratio * 100, 1)}% 的字"
                    f"是本书此前从未用过的（上限 {round(NOVEL_CHAR_CAP * 100)}%）"
                ),
                value=metrics.novel_char_ratio,
                reference=NOVEL_CHAR_CAP,
                excerpt="、".join(fresh[:12]),
                suggestion="换回本书已经用惯的字眼：意外是指选择意外，不是堆生僻字",
            )
        )

    _low, high, reference = bounds("summary_per_1k")
    if metrics.summary_per_1k > high:
        issues.append(
            StyleIssue(
                code="SUMMARY_ENDING",
                level="warning",
                metric="summary_per_1k",
                message=(
                    f"总结回扣偏多：每千字 {metrics.summary_per_1k} 处"
                    f"{detail(reference)}，上限 {round(high, 3)}"
                ),
                value=metrics.summary_per_1k,
                reference=reference,
                excerpt=_first_matching_sentence(text, SUMMARY_PATTERNS),
                suggestion="把已经演过的事再概括一遍的句子删掉：读者自己会体会，不用替他总结",
            )
        )

    _low, high, reference = bounds("elevation_per_1k")
    if metrics.elevation_per_1k > high:
        issues.append(
            StyleIssue(
                code="ELEVATION_DENSE",
                level="warning",
                metric="elevation_per_1k",
                message=(
                    f"升华议论偏多：每千字 {metrics.elevation_per_1k} 处"
                    f"{detail(reference)}，上限 {round(high, 3)}"
                ),
                value=metrics.elevation_per_1k,
                reference=reference,
                excerpt=_first_matching_sentence(text, ELEVATION_PATTERNS),
                suggestion="把道理收回去，只留具体的人做了什么、看见了什么；想说的意思让读者自己得出来",
            )
        )

    _low, high, reference = bounds("transition_per_1k")
    if metrics.transition_per_1k > high:
        issues.append(
            StyleIssue(
                code="SMOOTH_TRANSITION",
                level="warning",
                metric="transition_per_1k",
                message=(
                    f"过渡太平滑：每千字 {metrics.transition_per_1k} 处「于是/随后/第二天」这类连接"
                    f"{detail(reference)}，上限 {round(high, 3)}"
                ),
                value=metrics.transition_per_1k,
                reference=reference,
                excerpt=_first_matching_sentence(text, TRANSITION_PATTERNS),
                suggestion="该跳就跳：直接切到下一个场景，让读者自己补那段时间；不必每件事都交代因果",
            )
        )

    _low, high, reference = bounds("abstract_unsupported_ratio")
    if metrics.abstract_unsupported_ratio > high:
        issues.append(
            StyleIssue(
                code="ABSTRACT_SUBSTITUTION",
                level="warning",
                metric="abstract_unsupported_ratio",
                message=(
                    f"抽象替代具体：{round(metrics.abstract_unsupported_ratio * 100)}% 的抽象词句里"
                    f"没有可见的东西（动作／对白／器物）{detail(reference)}，上限 {round(high, 3)}"
                ),
                value=metrics.abstract_unsupported_ratio,
                reference=reference,
                excerpt=_first_matching_sentence(text, ABSTRACT_PATTERNS),
                suggestion="把「气息、杀意、压迫感」换成一个能看见的细节：他手心的汗、刀鞘上的裂、谁先退了半步",
            )
        )
    return issues


def measure(text: str, *, known_texts: list[str] | None = None) -> StyleMetrics:
    """对一段正文计算全部文风指标（纯函数，可单测）。

    ``known_texts`` 给出此前章节的正文；给了才能算「本章新二元组占比」（用词是不是飘了）。
    """
    content = text or ""
    chars = count_words(content)
    sentences = [s for s in split_sentences(content) if s.strip()]
    paragraphs = [p.strip() for p in content.split("\n") if p.strip()]

    lengths = [count_words(s) for s in sentences] or [0]
    para_lengths = [count_words(p) for p in paragraphs] or [0]
    dialogue_chars = sum(count_words(match) for match in DIALOGUE_RE.findall(content))
    dialogue_paragraphs = sum(1 for p in paragraphs if DIALOGUE_RE.search(p))

    cliche_hits = _hits(content, CLICHE_PATTERNS)
    cliche_total = sum(cliche_hits.values())
    abstract_total = sum(_hits(content, ABSTRACT_PATTERNS).values())
    explaining_total = sum(_hits(content, EXPLAINING_PATTERNS).values())
    conflict_total = sum(_hits(content, CONFLICT_PATTERNS).values())
    hook_score, hook_signals = _hook(content)

    openers = [p[:2] for p in paragraphs]
    subject_openers = sum(1 for opener in openers if opener.startswith(SUBJECT_OPENERS))

    used_punctuation = {mark for mark in PUNCTUATION_SET if mark in content}
    short_paragraphs = sum(1 for p in paragraphs if count_words(p) <= 8)
    dash_ellipsis = content.count("——") + content.count("…")
    colloquial = sum(content.count(marker) for marker in COLLOQUIAL_MARKERS)
    question_sentences = sum(1 for s in sentences if s.strip().endswith(("？", "?")))

    # ---- V0.5 推进与节奏：注水、解释腔、标点均匀、情绪平、句式循环
    advancement_total = sum(_hits(content, ADVANCEMENT_MARKERS).values())
    filler_paragraphs = sum(
        1
        for p in paragraphs
        if count_words(p) >= 30 and not DIALOGUE_RE.search(p) and not _has_story_action(p)
    )
    exposition_total = 0
    for sentence in sentences:
        if any(marker in sentence for marker in EXPOSITION_MARKERS) and not any(
            marker in sentence for marker in ADVANCEMENT_MARKERS
        ):
            exposition_total += sum(sentence.count(marker) for marker in EXPOSITION_MARKERS)
    punctuation_entropy = _punctuation_entropy(content)
    emotion_flatness = _emotion_flatness(paragraphs)
    pattern_loop_ratio, pattern_top = _pattern_loop(sentences)

    # ---- V0.6 用词与收束：可预测的用词、总结升华、平滑过渡、抽象替代具体
    word_ttr, word_concentration = _windowed_word_stats(content)
    verb_variety = _verb_variety(content)
    novel_bigram_ratio_value = novel_bigram_ratio(content, _known_bigrams(known_texts or []))
    novel_char_ratio_value = novel_char_ratio(content, _known_chars(known_texts or []))
    summary_total = sum(_hits(content, SUMMARY_PATTERNS).values())
    elevation_total = sum(_hits(content, ELEVATION_PATTERNS).values())
    transition_total = sum(_hits(content, TRANSITION_PATTERNS).values())
    abstract_unsupported_ratio = _abstract_unsupported(content)

    metrics = StyleMetrics(
        total_chars=chars,
        sentence_count=len(sentences),
        paragraph_count=len(paragraphs),
        mean_sentence_len=round(mean(lengths), 2),
        sentence_len_cv=round(pstdev(lengths) / mean(lengths), 3) if mean(lengths) else 0.0,
        burstiness=round(pstdev(lengths) / mean(lengths), 3) if mean(lengths) else 0.0,
        short_sentence_ratio=round(sum(1 for x in lengths if x <= 8) / len(lengths), 3),
        long_sentence_ratio=round(sum(1 for x in lengths if x >= 40) / len(lengths), 3),
        paragraph_len_cv=round(pstdev(para_lengths) / mean(para_lengths), 3)
        if mean(para_lengths)
        else 0.0,
        dialogue_ratio=round(dialogue_chars / chars, 3) if chars else 0.0,
        dialogue_paragraph_ratio=round(dialogue_paragraphs / len(paragraphs), 3) if paragraphs else 0.0,
        cliche_per_1k=_per_1k(cliche_total, chars),
        cliche_variety=round(len(cliche_hits) / max(cliche_total, 1), 3),
        abstract_per_1k=_per_1k(abstract_total, chars),
        explaining_per_1k=_per_1k(explaining_total, chars),
        conflict_per_1k=_per_1k(conflict_total, chars),
        subject_repeat_ratio=round(subject_openers / len(paragraphs), 3) if paragraphs else 0.0,
        opener_diversity=round(len(set(openers)) / len(paragraphs), 3) if paragraphs else 0.0,
        self_repeat_ratio=_ngram_repeat_ratio(content),
        hook_score=hook_score,
        punctuation_variety=round(len(used_punctuation) / len(PUNCTUATION_SET), 3),
        short_paragraph_ratio=round(short_paragraphs / len(paragraphs), 3) if paragraphs else 0.0,
        dash_ellipsis_per_1k=_per_1k(dash_ellipsis, chars),
        colloquial_per_1k=_per_1k(colloquial, chars),
        question_ratio=round(question_sentences / len(sentences), 3) if sentences else 0.0,
        advancement_per_1k=_per_1k(advancement_total, chars),
        filler_paragraph_ratio=round(filler_paragraphs / len(paragraphs), 3) if paragraphs else 0.0,
        exposition_per_1k=_per_1k(exposition_total, chars),
        punctuation_entropy=punctuation_entropy,
        emotion_flatness=emotion_flatness,
        pattern_loop_ratio=pattern_loop_ratio,
        pattern_top=pattern_top,
        word_ttr=word_ttr,
        word_concentration=word_concentration,
        verb_variety=verb_variety,
        novel_bigram_ratio=novel_bigram_ratio_value,
        novel_char_ratio=novel_char_ratio_value,
        summary_per_1k=_per_1k(summary_total, chars),
        elevation_per_1k=_per_1k(elevation_total, chars),
        transition_per_1k=_per_1k(transition_total, chars),
        abstract_unsupported_ratio=abstract_unsupported_ratio,
        hook_signals=hook_signals,
        cliche_hits=dict(sorted(cliche_hits.items(), key=lambda item: -item[1])[:12]),
    )
    return metrics


# --------------------------------------------------------------------------- 基线
def build_profile(
    texts: list[str], *, name: str = "", source: str = "CHAPTERS", samples: list[str] | None = None
) -> dict[str, Any]:
    """对多份样章求每个指标的均值与标准差，形成这本书的文风基线。"""
    if not texts:
        return {}
    per_text = [measure(text).to_dict() for text in texts]
    numeric_keys = [
        key
        for key, value in per_text[0].items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    profile: dict[str, Any] = {"metrics_version": METRICS_VERSION, "sample_count": len(texts)}
    for key in numeric_keys:
        values = [float(item.get(key) or 0.0) for item in per_text]
        profile[key] = {
            "mean": round(mean(values), 4),
            "std": round(pstdev(values), 4) if len(values) > 1 else 0.0,
            "min": round(min(values), 4),
            "max": round(max(values), 4),
        }
    profile["total_chars"] = sum(count_words(text) for text in texts)
    profile["name"] = name
    profile["source"] = source
    profile["samples"] = samples or []
    return profile


def save_profile(
    session: Session,
    novel: Novel,
    texts: list[str],
    *,
    name: str,
    source: str = "CHAPTERS",
    samples: list[str] | None = None,
    make_default: bool = True,
) -> StyleProfile:
    profile = StyleProfile(
        novel_id=novel.id,
        name=name,
        source=source,
        sample_count=len(texts),
        total_chars=sum(count_words(text) for text in texts),
        metrics=build_profile(texts, name=name, source=source, samples=samples),
        samples=(samples or [])[:20],
        is_default=make_default,
    )
    if make_default:
        for existing in session.scalars(
            select(StyleProfile).where(StyleProfile.novel_id == novel.id)
        ):
            existing.is_default = False
    session.add(profile)
    session.flush()
    return profile


def default_profile(session: Session, novel_id: str) -> StyleProfile | None:
    return session.scalar(
        select(StyleProfile)
        .where(StyleProfile.novel_id == novel_id, StyleProfile.is_default.is_(True))
        .order_by(StyleProfile.locked.desc(), StyleProfile.created_at.desc())
    )


def lock_profile(session: Session, profile: StyleProfile, locked: bool = True) -> StyleProfile:
    """把某个基线「定下来」：定下来之后它就是这本书的文风，新章按更严的窗口比对。

    同一本书同时只保留一个锁定的基线 —— 换风格是作者的显式决定，不该是悄悄地漂。
    """
    if locked:
        for other in session.scalars(
            select(StyleProfile).where(
                StyleProfile.novel_id == profile.novel_id, StyleProfile.id != profile.id
            )
        ):
            other.locked = False
            other.is_default = False
        profile.is_default = True
    profile.locked = locked
    session.flush()
    return profile


def style_lock_directive(
    profile: StyleProfile | dict[str, Any] | None,
    *,
    voice_profile: StyleProfile | dict[str, Any] | None = None,
) -> str:
    """把锁定的文风基线翻译成能直接放进写作提示的硬性要求。

    「选定的文风不该随意变化」要落地，就得在生成之前把这本书的节奏、推进密度、
    标点密度、句式习惯写清楚 —— 事后挑错只能返工，事前对齐才叫保持。
    """
    metrics = (profile.metrics if isinstance(profile, StyleProfile) else profile) or {}
    if not metrics:
        return ""
    name = profile.name if isinstance(profile, StyleProfile) else ""

    def value(metric: str) -> float | None:
        entry = metrics.get(metric)
        if isinstance(entry, dict):
            return float(entry.get("mean") or 0.0)
        return None

    lines: list[str] = []
    burst = value("burstiness")
    if burst is not None:
        lines.append(f"节奏：句长变异系数保持 {round(burst, 2)} 左右（长短句要交错，别写成一顺的长句）")
    dialogue = value("dialogue_ratio")
    if dialogue is not None:
        lines.append(f"对白：对白占比保持 {round(dialogue * 100)}% 左右")
    cliche = value("cliche_per_1k")
    if cliche is not None:
        lines.append(f"用词：每千字套话不超过 {max(1.0, round(cliche, 1))} 处，同一句话的说法不许重复")
    advancement = value("advancement_per_1k")
    if advancement is not None:
        lines.append(
            f"推进：每千字至少 {max(4.0, round(advancement * 0.7, 1))} 处剧情动作"
            "（决定、交出、揭穿、动手这类），不能整章只有环境和心理"
        )
    filler = value("filler_paragraph_ratio")
    if filler is not None:
        lines.append(f"注水：无动作无对白的长段不超过 {round(max(0.05, filler) * 100)}%")
    exposition = value("exposition_per_1k")
    if exposition is not None:
        lines.append(f"解释：每千字名词解释不超过 {max(2.0, round(exposition, 1))} 处，设定要化进动作与对白")
    entropy = value("punctuation_entropy")
    if entropy is not None:
        lines.append(f"标点：标点类型要铺开（基线熵 {round(entropy, 2)}），该用「！…——」的地方不要都用「，。」")
    loop = value("pattern_loop_ratio")
    if loop is not None:
        lines.append(f"句式：共用同一骨架的句子不超过 {round(max(0.15, loop) * 100)}%")
    word_ttr = value("word_ttr")
    if word_ttr is not None:
        lines.append(
            f"用词：同一批词不要反复用（用词多样性基线 {round(word_ttr, 2)}）；"
            "但换词是换更具体的说法，不是换成生僻字"
        )
    verb_variety = value("verb_variety")
    if verb_variety is not None:
        lines.append(f"动词：同一个动词别反复用（动词多样性基线 {round(verb_variety, 2)}）")
    lines.append("收束：不要在章末把本章总结一遍或升华成道理；演完就停")
    lines.append("过渡：场景之间该跳就跳，不必每件事都用「于是/随后/第二天」粘起来")
    lines.append("桥段：不要重复本书前面用过的收尾方式（转身就走、消失在夜色里这类）")

    voice = voice_profile.metrics if isinstance(voice_profile, StyleProfile) else voice_profile
    terms = (voice or {}).get("signature_terms") or []
    if terms:
        lines.append("作者习惯用词（尽量保留，不要改写成更「标准」的说法）：" + "、".join(terms[:8]))

    if not lines:
        return ""
    header = f"【本书文风锁定{'：' + name if name else ''}】"
    return header + "\n" + "\n".join(f"- {line}" for line in lines)


def drift_report(    session: Session,
    novel: Novel,
    *,
    profile: StyleProfile | None = None,
    min_chars: int = 400,
    limit: int | None = None,
) -> dict[str, Any]:
    """逐章看：哪些章已经偏离（锁定时按收紧的窗口算），漂的是哪几项指标。"""
    baseline = profile or default_profile(session, novel.id)
    profile_metrics = (baseline.metrics if baseline else None) or None
    locked = bool(baseline is not None and baseline.locked)
    scale = LOCKED_TOLERANCE_SCALE if locked else 1.0
    stmt = (
        select(Chapter)
        .where(Chapter.novel_id == novel.id, Chapter.content.is_not(None))
        .order_by(Chapter.chapter_number)
    )
    chapters: list[dict[str, Any]] = []
    drifted_count = 0
    for chapter in session.scalars(stmt):
        content = chapter.content or ""
        if count_words(content) < min_chars:
            continue
        metrics = measure(content).to_dict()
        drifted = drifted_metrics(metrics, profile_metrics, scale=scale)
        if drifted:
            drifted_count += 1
        chapters.append(
            {
                "chapter_number": chapter.chapter_number,
                "title": chapter.title or "",
                "score": round(max(0.0, 100.0 - 8.0 * len(drifted)), 1),
                "drifted": drifted,
            }
        )
    if limit:
        chapters = chapters[-limit:]
    missing: list[str] = []
    if isinstance(profile_metrics, dict):
        missing = [key for key in TRACKED_METRICS if key not in profile_metrics]
    return {
        "locked": locked,
        "profile_id": baseline.id if baseline else None,
        "profile_name": baseline.name if baseline else "",
        "direction": "locked" if locked else ("baseline" if baseline else "none"),
        "metrics_version": str((profile_metrics or {}).get("metrics_version") or ""),
        "missing_metrics": missing,
        "stale": bool(missing),
        "chapters": chapters,
        "drifted_count": drifted_count,
    }


def profile_from_chapters(
    session: Session,
    novel: Novel,
    *,
    chapter_numbers: list[int] | None = None,
    min_chapters: int = 3,
    name: str = "本书基线",
    with_voice: bool = True,
) -> StyleProfile | None:
    """用本书已完成的章节建立基线；章节太少就不建（样本不足的基线没有意义）。

    with_voice=True 时同时建立「作者声音」画像（标点与用词习惯），供声音保护使用。
    """
    stmt = select(Chapter).where(
        Chapter.novel_id == novel.id, Chapter.status == ChapterStatus.COMPLETED
    )
    if chapter_numbers:
        stmt = stmt.where(Chapter.chapter_number.in_(chapter_numbers))
    chapters = list(session.scalars(stmt.order_by(Chapter.chapter_number)))
    usable = [chapter for chapter in chapters if count_words(chapter.content) >= 400]
    if len(usable) < min_chapters:
        return None
    if with_voice and default_voice_profile(session, novel.id) is None:
        save_voice_profile(
            session,
            novel,
            [chapter.content for chapter in usable],
            name="作者声音（本书章节）",
            samples=[f"第{chapter.chapter_number}章" for chapter in usable],
        )
    return save_profile(
        session,
        novel,
        [chapter.content for chapter in usable],
        name=name,
        source="CHAPTERS",
        samples=[f"第{chapter.chapter_number}章" for chapter in usable],
    )


# --------------------------------------------------------------------------- 评审
@dataclass
class StyleIssue:
    code: str
    level: str  # warning / info
    metric: str
    message: str
    value: float
    reference: float | None = None
    excerpt: str = ""
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _excerpt(text: str, pattern: str, width: int = 40) -> str:
    index = text.find(pattern)
    if index < 0:
        return ""
    start = max(0, index - width // 2)
    return text[start : index + len(pattern) + width].replace("\n", " ")


def _first_filler_paragraph(text: str) -> str:
    """第一段「既无动作也无对白」的长段，作为注水的证据。"""
    for paragraph in (p.strip() for p in text.split("\n")):
        if (
            count_words(paragraph) >= 30
            and not DIALOGUE_RE.search(paragraph)
            and not _has_story_action(paragraph)
        ):
            return paragraph[:60]
    return ""


def _first_exposition_sentence(text: str) -> str:
    """第一句只见解释、没有推进的名词说明，作为过度解读的证据。"""
    for sentence in split_sentences(text):
        if any(marker in sentence for marker in EXPOSITION_MARKERS) and not any(
            marker in sentence for marker in ADVANCEMENT_MARKERS
        ):
            return sentence.strip()[:60]
    return ""


#: 跨章复读判定的参数：段落最少字数、四元组最少个数、重合度阈值
RESTATE_MIN_CHARS = 25
RESTATE_MIN_GRAMS = 18
RESTATE_THRESHOLD = 0.55


def _prior_label(labels: list[str] | None, index: int) -> str:
    if labels and 0 <= index < len(labels):
        return f"{labels[index]}"
    return f"第 {index + 1} 篇前文"


def _paragraph_grams(paragraph: str, size: int = 4) -> set[str]:
    cleaned = re.sub(r"[^\u4e00-\u9fa5]", "", paragraph or "")
    if len(cleaned) < size:
        return set()
    return {cleaned[i : i + size] for i in range(len(cleaned) - size + 1)}


def restating_issues(
    text: str,
    prior_texts: list[str] | None,
    *,
    labels: list[str] | None = None,
    threshold: float = RESTATE_THRESHOLD,
) -> list[StyleIssue]:
    """跨章复读：本章段落与已发布章节的段落高度重合 = 又把读者已知的事讲了一遍。

    用四元组倒排索引比对，避免段落两两比较在长篇上爆炸。
    """
    if not prior_texts:
        return []
    paragraphs = [
        p.strip()
        for p in (text or "").split("\n")
        if count_words(p.strip()) >= RESTATE_MIN_CHARS
    ]
    if not paragraphs:
        return []

    index: dict[str, list[int]] = {}
    priors: list[tuple[str, int]] = []
    for chapter_index, content in enumerate(prior_texts):
        for paragraph in (p.strip() for p in (content or "").split("\n")):
            if count_words(paragraph) < RESTATE_MIN_CHARS:
                continue
            stored = len(priors)
            priors.append((paragraph, chapter_index))
            for gram in _paragraph_grams(paragraph):
                index.setdefault(gram, []).append(stored)
    if not priors:
        return []

    issues: list[StyleIssue] = []
    for paragraph in paragraphs:
        grams = _paragraph_grams(paragraph)
        if len(grams) < RESTATE_MIN_GRAMS:
            continue
        tally: Counter[int] = Counter()
        for gram in grams:
            tally.update(index.get(gram, ()))
        if not tally:
            continue
        best_index, best_overlap = tally.most_common(1)[0]
        source = priors[best_index][0]
        ratio = best_overlap / min(len(grams), len(_paragraph_grams(source)))
        if ratio < threshold:
            continue
        issues.append(
            StyleIssue(
                code="RESTATING_KNOWN",
                level="warning",
                metric="restating_ratio",
                message=(
                    f"重复交代已知信息：本段与前文重合度 {round(ratio, 2)}"
                    f"（{_prior_label(labels, priors[best_index][1])}已写过同样内容）"
                ),
                value=round(ratio, 3),
                reference=threshold,
                excerpt=f"本章：{paragraph[:48]} ↔ 前文：{source[:36]}",
                suggestion="读者已经知道的事不用再讲；这里换成新信息或直接删掉",
            )
        )
    return issues


def _advancement_issues(
    text: str,
    metrics: StyleMetrics,
    profile_metrics: dict[str, Any] | None,
    *,
    scale: float = 1.0,
) -> list[StyleIssue]:
    """V0.5 六项：推进不足、注水、名词解释、标点均匀、情绪平、句式循环。"""
    issues: list[StyleIssue] = []
    if metrics.total_chars < 200:
        return issues

    def bounds(metric: str) -> tuple[float, float, float | None]:
        low, high = _threshold(
            profile_metrics, metric, DEFAULT_RANGES.get(metric, (0.0, float("inf"))), scale=scale
        )
        reference = None
        if isinstance(profile_metrics, dict) and isinstance(profile_metrics.get(metric), dict):
            reference = float(profile_metrics[metric]["mean"])
        return low, high, reference

    def detail(reference: float | None) -> str:
        return f"（本书基线 {reference}）" if reference is not None else "（经验阈值）"

    low, _high, reference = bounds("advancement_per_1k")
    if metrics.advancement_per_1k < low:
        issues.append(
            StyleIssue(
                code="LOW_ADVANCEMENT",
                level="info" if metrics.advancement_per_1k >= low / 2 else "warning",
                metric="advancement_per_1k",
                message=(
                    f"推进信号偏少：每千字只有 {metrics.advancement_per_1k} 处剧情动作"
                    f"{detail(reference)}，下限 {round(low, 2)}"
                ),
                value=metrics.advancement_per_1k,
                reference=reference,
                suggestion="把可省的描写换成一个具体动作或结果：谁做了什么，局面因此变了什么",
            )
        )

    _low, high, reference = bounds("filler_paragraph_ratio")
    if metrics.filler_paragraph_ratio > high:
        issues.append(
            StyleIssue(
                code="FILLER_HEAVY",
                level="warning",
                metric="filler_paragraph_ratio",
                message=(
                    f"无推进段落偏多：{round(metrics.filler_paragraph_ratio * 100, 1)}% 的长段"
                    f"既无动作也无对白{detail(reference)}，上限 {round(high, 3)}"
                ),
                value=metrics.filler_paragraph_ratio,
                reference=reference,
                excerpt=_first_filler_paragraph(text),
                suggestion="压缩或删掉这些段落；每段至少给一个动作、一句对白或一条新信息",
            )
        )

    _low, high, reference = bounds("exposition_per_1k")
    if metrics.exposition_per_1k > high:
        issues.append(
            StyleIssue(
                code="EXPOSITION_DUMP",
                level="warning",
                metric="exposition_per_1k",
                message=(
                    f"名词解释/设定说明偏多：每千字 {metrics.exposition_per_1k} 处"
                    f"{detail(reference)}，上限 {round(high, 3)}"
                ),
                value=metrics.exposition_per_1k,
                reference=reference,
                excerpt=_first_exposition_sentence(text),
                suggestion="设定拆开融进动作与对白，读者需要时再给，不要成段解释",
            )
        )

    low, _high, reference = bounds("punctuation_entropy")
    if metrics.punctuation_entropy < low:
        strong_marks = sum(text.count(mark) for mark in ("！", "？", "…", "——"))
        issues.append(
            StyleIssue(
                code="PUNCT_FLAT",
                level="warning",
                metric="punctuation_entropy",
                message=(
                    f"标点分布过于均匀：类型熵 {metrics.punctuation_entropy}{detail(reference)}，"
                    f"下限 {round(low, 3)}；强标点（！？…——）只有 {strong_marks} 处"
                ),
                value=metrics.punctuation_entropy,
                reference=reference,
                suggestion="在爆发处用短句配「！」「…」「——」，让标点密度出现落差",
            )
        )

    _low, high, reference = bounds("emotion_flatness")
    if metrics.emotion_flatness > high:
        issues.append(
            StyleIssue(
                code="EMOTION_FLAT",
                level="warning",
                metric="emotion_flatness",
                message=(
                    f"情绪起伏不足：{metrics.emotion_flatness}{detail(reference)}，上限 {round(high, 3)}"
                    "；各段情绪强度挤在同一档"
                ),
                value=metrics.emotion_flatness,
                reference=reference,
                suggestion="给关键段落一次情绪爆发，同时让冷静处更彻底地冷下来，落差才有起伏",
            )
        )

    _low, high, reference = bounds("pattern_loop_ratio")
    if metrics.pattern_loop_ratio > high:
        issues.append(
            StyleIssue(
                code="PATTERN_LOOP",
                level="warning",
                metric="pattern_loop_ratio",
                message=(
                    f"句式循环：{round(metrics.pattern_loop_ratio * 100, 1)}% 的句子共用一个骨架"
                    f"{detail(reference)}，上限 {round(high, 3)}"
                ),
                value=metrics.pattern_loop_ratio,
                reference=reference,
                excerpt="、".join(metrics.pattern_top[:3]),
                suggestion="换掉开头与收尾：同一句式（开头三字 + 收尾标点）一章内别出现三次以上",
            )
        )
    return issues


def _threshold(
    profile: dict[str, Any] | None,
    metric: str,
    fallback: tuple[float, float],
    *,
    scale: float = 1.0,
) -> tuple[float, float]:
    """阈值 = 基线窗口（均值 ± 容差）∩ 绝对上下限。

    容差取 max(1.5σ, 30% 均值, 该指标的最小容差)，避免「基线离散度大 → 窗口宽到形同虚设」；
    绝对上下限来自 DEFAULT_RANGES，保证再怎么偏离也不会放过明显离谱的稿子。
    ``scale`` 用来收紧窗口：作者锁定文风后按 0.5 倍容差比对（V0.5）。
    """
    if not profile:
        return fallback
    entry = profile.get(metric)
    if not isinstance(entry, dict):
        return fallback
    mean_value = float(entry.get("mean") or 0.0)
    std = float(entry.get("std") or 0.0)
    tolerance = max(1.5 * std, 0.3 * abs(mean_value), MIN_TOLERANCE.get(metric, 0.05)) * scale
    direction = METRIC_DIRECTIONS.get(metric, "range")
    if direction == "lower_better":
        ceiling = min(fallback[1], mean_value + tolerance)
        return (fallback[0], ceiling)
    if direction == "higher_better":
        floor = max(fallback[0], mean_value - tolerance)
        return (floor, fallback[1])
    return (
        max(fallback[0], mean_value - tolerance),
        min(fallback[1], mean_value + tolerance),
    )


#: 已锁定文风时的容差缩放：窗口收紧一半，慢慢漂走的稿子会被抓出来
LOCKED_TOLERANCE_SCALE = 0.5
#: 逐章漂移报告与锁定检查覆盖的指标
TRACKED_METRICS: tuple[str, ...] = tuple(DEFAULT_RANGES)


def drifted_metrics(
    metrics: dict[str, Any],
    profile_metrics: dict[str, Any] | None,
    *,
    scale: float = 1.0,
    metrics_list: tuple[str, ...] = TRACKED_METRICS,
) -> list[dict[str, Any]]:
    """哪些指标越出了基线窗口（含方向与阈值），供锁定文风与漂移报告共用。"""
    drifted: list[dict[str, Any]] = []
    if not profile_metrics:
        return drifted
    for key in metrics_list:
        if key not in profile_metrics:
            continue
        entry = profile_metrics.get(key)
        if not isinstance(entry, dict):
            continue
        value = float(metrics.get(key) or 0.0)
        low, high = _threshold(profile_metrics, key, DEFAULT_RANGES.get(key, (0.0, float("inf"))), scale=scale)
        if low <= value <= high:
            continue
        drifted.append(
            {
                "metric": key,
                "value": round(value, 4),
                "mean": float(entry.get("mean") or 0.0),
                "low": round(low, 4),
                "high": round(high, 4),
                "direction": METRIC_DIRECTIONS.get(key, "range"),
                "side": "low" if value < low else "high",
            }
        )
    return drifted


def review_text(
    text: str,
    *,
    profile: StyleProfile | None = None,
    voice_profile: StyleProfile | None = None,
    min_chars: int = 300,
    prior_texts: list[str] | None = None,
    prior_labels: list[str] | None = None,
) -> dict[str, Any]:
    """对一段正文做文风评审：指标 + 问题清单 + 启发式评分 + 声音保留分。

    「AI 味」与「像不像作者」是两个方向：前者越低越好，后者不能丢。
    ``prior_texts`` 给出已发布章节的正文，用来判定「重复交代已知信息」。
    """
    metrics = measure(text, known_texts=prior_texts)
    profile_metrics = (profile.metrics if profile else None) or None
    locked = bool(profile is not None and getattr(profile, "locked", False))
    tolerance_scale = LOCKED_TOLERANCE_SCALE if locked else 1.0
    issues: list[StyleIssue] = []

    # 基线缺指标时必须说出来：旧版本的基线不会让新指标静默失效（只是没有相对窗口可依）
    baseline_version = ""
    missing_metrics: list[str] = []
    if isinstance(profile_metrics, dict):
        baseline_version = str(profile_metrics.get("metrics_version") or "")
        missing_metrics = [key for key in TRACKED_METRICS if key not in profile_metrics]
        if missing_metrics:
            issues.append(
                StyleIssue(
                    code="BASELINE_STALE",
                    level="info",
                    metric="baseline",
                    message=(
                        f"本书基线建立于指标版本 {baseline_version or '未知'}，"
                        f"缺少 {len(missing_metrics)} 项当前指标（{('、'.join(missing_metrics[:6]))}…）："
                        "这些指标只能按经验阈值判断，建议重建基线"
                    ),
                    value=float(len(missing_metrics)),
                    suggestion="在「质量」面板用「用本书已完成章节建立基线」重建，之后新指标才有相对窗口",
                )
            )

    if metrics.total_chars < min_chars:
        issues.append(
            StyleIssue(
                code="TOO_SHORT",
                level="info",
                metric="total_chars",
                message=f"正文只有 {metrics.total_chars} 字，指标统计意义有限",
                value=float(metrics.total_chars),
                suggestion="章节过短时先补内容，再看文风指标",
            )
        )

    checks = [
        ("cliche_per_1k", "CLICHE_DENSE", "套话密度偏高", "把重复的套话换成本章独有的动作或对白细节"),
        ("abstract_per_1k", "ABSTRACT_DENSE", "抽象名词/形容堆砌", "把「气息」「杀意」这类抽象词换成具体可见的细节"),
        ("explaining_per_1k", "TELLING_DENSE", "解释性叙述偏多", "把「他明白…」改成动作与对白，让读者自己看出来"),
        ("long_sentence_ratio", "LONG_SENTENCE_DENSE", "长句占比偏高", "把长句拆成短句，制造节奏差"),
        ("subject_repeat_ratio", "SUBJECT_REPEAT", "段落主语重复", "换用动作、环境、对白开头，别让每段都以「他」开始"),
        ("self_repeat_ratio", "SELF_REPEAT", "章内复读明显", "删掉重复表述；同一信息只交代一次"),
    ]
    for metric, code, label, suggestion in checks:
        value = float(getattr(metrics, metric))
        low, high = _threshold(
            profile_metrics, metric, DEFAULT_RANGES.get(metric, (0.0, float("inf"))), scale=tolerance_scale
        )
        if value > high:
            reference = None
            if isinstance(profile_metrics, dict) and isinstance(profile_metrics.get(metric), dict):
                reference = float(profile_metrics[metric]["mean"])
            detail = f"（本书基线 {reference}）" if reference is not None else "（经验阈值）"
            issues.append(
                StyleIssue(
                    code=code,
                    level="warning",
                    metric=metric,
                    message=f"{label}：{value}{detail}，上限 {round(high, 3)}",
                    value=value,
                    reference=reference,
                    excerpt=_excerpt(text, next(iter(metrics.cliche_hits), "")) if metric == "cliche_per_1k" else "",
                    suggestion=suggestion,
                )
            )

    for metric, code, label, suggestion in [
        ("burstiness", "FLAT_RHYTHM", "句长变化太小（节奏平）", "故意穿插极短句，制造停顿与压迫感"),
        ("hook_score", "NO_HOOK", "章末缺少钩子", "在结尾留一个未答的问题、转折或截断的句子"),
        ("paragraph_len_cv", "FLAT_PARAGRAPH", "段落长度过于均匀", "让段落长短交错，关键处单句成段"),
        ("dialogue_ratio", "DIALOGUE_LOW", "对白占比偏低", "把叙述改成角色对话，人物语气也会更鲜明"),
    ]:
        value = float(getattr(metrics, metric))
        low, high = _threshold(
            profile_metrics, metric, DEFAULT_RANGES.get(metric, (0.0, float("inf"))), scale=tolerance_scale
        )
        if value < low:
            reference = None
            if isinstance(profile_metrics, dict) and isinstance(profile_metrics.get(metric), dict):
                reference = float(profile_metrics[metric]["mean"])
            detail = f"（本书基线 {reference}）" if reference is not None else "（经验阈值）"
            level = "warning" if code in ("FLAT_RHYTHM", "NO_HOOK") else "info"
            issues.append(
                StyleIssue(
                    code=code,
                    level=level,
                    metric=metric,
                    message=f"{label}：{value}{detail}，下限 {round(low, 3)}",
                    value=value,
                    reference=reference,
                    excerpt=(text.strip()[-60:] if code == "NO_HOOK" else ""),
                    suggestion=suggestion,
                )
            )

    if metrics.cliche_variety and metrics.cliche_per_1k > 1.0 and metrics.cliche_variety < 0.5:
        issues.append(
            StyleIssue(
                code="CLICHE_MONOTONE",
                level="warning",
                metric="cliche_variety",
                message=f"套话种类太少（重复率 {metrics.cliche_variety}）：少数几个说法被反复用",
                value=metrics.cliche_variety,
                excerpt="、".join(list(metrics.cliche_hits)[:5]),
                suggestion="同一个意思换一种写法；一章内同一套话不要出现两次以上",
            )
        )

    issues.extend(_advancement_issues(text, metrics, profile_metrics, scale=tolerance_scale))
    issues.extend(
        _wording_issues(
            text,
            metrics,
            profile_metrics,
            scale=tolerance_scale,
            known_chars=_known_chars(prior_texts or []),
        )
    )
    issues.extend(restating_issues(text, prior_texts, labels=prior_labels))
    issues.extend(bridge_repeat_issues(text, prior_texts, labels=prior_labels))

    drift: list[dict[str, Any]] = []
    if locked:
        drift = drifted_metrics(metrics.to_dict(), profile_metrics, scale=tolerance_scale)
        if drift:
            names = "、".join(item["metric"] for item in drift[:6])
            issues.append(
                StyleIssue(
                    code="STYLE_DRIFT_LOCKED",
                    level="warning",
                    metric="locked_style",
                    message=(
                        f"文风已锁定，但本章有 {len(drift)} 项指标漂出基线窗口：{names}"
                    ),
                    value=float(len(drift)),
                    excerpt=names,
                    suggestion="按锁定的基线改回来：这本书的节奏、用词与标点密度是作者定下的",
                )
            )

    severity = sum(8 if issue.level == "warning" else 3 for issue in issues)
    score = max(0.0, 100.0 - severity)

    voice_issues, voice_report = voice_rules(
        text, voice_profile, metrics=metrics, min_chars=min(60, min_chars)
    )
    if voice_profile is not None:
        issues.extend(voice_issues)
        severity += sum(8 if issue.level == "warning" else 3 for issue in voice_issues)
        score = max(0.0, 100.0 - severity)

    return {
        "metrics_version": METRICS_VERSION,
        "score": round(score, 1),
        "metrics": metrics.to_dict(),
        "issues": [issue.to_dict() for issue in issues],
        "baseline": profile.name if profile else "",
        "baseline_metrics": profile_metrics or {},
        "baseline_metrics_version": baseline_version,
        "baseline_missing_metrics": missing_metrics,
        "baseline_stale": bool(missing_metrics),
        "locked": locked,
        "locked_drift": drift,
        "voice": voice_report,
        "voice_profile": voice_profile.name if voice_profile else "",
    }


def compare_metrics(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """比较两次指标的差异，用于验证「修订是否真的改善」。"""
    keys = [
        "burstiness",
        "short_sentence_ratio",
        "long_sentence_ratio",
        "dialogue_ratio",
        "cliche_per_1k",
        "abstract_per_1k",
        "explaining_per_1k",
        "subject_repeat_ratio",
        "self_repeat_ratio",
        "hook_score",
        "conflict_per_1k",
        "advancement_per_1k",
        "filler_paragraph_ratio",
        "exposition_per_1k",
        "punctuation_entropy",
        "emotion_flatness",
        "pattern_loop_ratio",
        "word_ttr",
        "word_concentration",
        "verb_variety",
        "summary_per_1k",
        "elevation_per_1k",
        "transition_per_1k",
        "abstract_unsupported_ratio",
    ]
    deltas: dict[str, Any] = {}
    improved = 0
    worsened = 0
    for key in keys:
        left = float(before.get(key) or 0.0)
        right = float(after.get(key) or 0.0)
        delta = round(right - left, 4)
        direction = METRIC_DIRECTIONS.get(key, "range")
        if abs(delta) < 1e-9:
            better: bool | None = None  # 无变化：既不是改善也不是变差
        elif direction == "higher_better":
            better = delta > 0
        elif direction == "lower_better":
            better = delta < 0
        else:
            better = None
        if better is True:
            improved += 1
        elif better is False:
            worsened += 1
        deltas[key] = {"before": left, "after": right, "delta": delta, "better": better}
    return {"deltas": deltas, "improved": improved, "worsened": worsened}


def review_chapter(
    session: Session, novel: Novel, chapter: Chapter, *, prior_chapters: int = 60
) -> dict[str, Any]:
    profile = default_profile(session, novel.id)
    voice_profile = default_voice_profile(session, novel.id)
    rows = session.execute(
        select(Chapter.chapter_number, Chapter.content)
        .where(
            Chapter.novel_id == novel.id,
            Chapter.chapter_number < chapter.chapter_number,
            Chapter.content.is_not(None),
        )
        .order_by(Chapter.chapter_number.desc())
        .limit(prior_chapters)
    ).all()
    prior = [content for _number, content in rows if content]
    labels = [f"第 {number} 章" for number, content in rows if content]
    report = review_text(
        chapter.content or "",
        profile=profile,
        voice_profile=voice_profile,
        prior_texts=prior,
        prior_labels=labels,
    )
    report["chapter_id"] = chapter.id
    report["chapter_number"] = chapter.chapter_number
    return report


def profile_from_fragments(session: Session, novel: Novel, *, name: str = "作者声音（碎片）") -> StyleProfile | None:
    """用作者写的碎片建声音画像：碎片就是他最原始的语言习惯。"""
    from app.models import Fragment

    fragments = list(session.scalars(select(Fragment).where(Fragment.novel_id == novel.id)))
    texts = [fragment.text for fragment in fragments if count_words(fragment.text) >= 20]
    if len(texts) < 2:
        return None
    return save_voice_profile(
        session,
        novel,
        texts,
        name=name,
        samples=[fragment.title or fragment.kind for fragment in fragments][:20],
    )


# ---------------------------------------------------------------------------
# 声音保护（V0.4）：作者的语言个性不是缺点，不能被改稿抹平
# ---------------------------------------------------------------------------
VOICE_PROFILE_SOURCE = "VOICE"
#: 作者特征词最多保留多少个
VOICE_TERM_LIMIT = 40


def _term_candidates(texts: list[str]) -> tuple[Counter, dict[str, int]]:
    """统计 2~3 字滑动片段，用于找作者的爱用词/意象词。

    两个要点：① 必须在**句子内部**做滑动窗口，跨句滑会切出「过来他」这种假词；
    ② 同时记录出现在几篇文本里，只有反复出现在多篇里的片段才算作者的用词习惯。
    """
    counter: Counter = Counter()
    documents: dict[str, int] = {}
    for index, text in enumerate(texts):
        for sentence in split_sentences(text or ""):
            cleaned = re.sub(r"[^\u4e00-\u9fa5A-Za-z]", "", sentence)
            if len(cleaned) < 2:
                continue
            local: set[str] = set()
            for size in (2, 3):
                for start in range(len(cleaned) - size + 1):
                    gram = cleaned[start : start + size]
                    if gram in _VOICE_STOPGRAMS or len(set(gram)) == 1:
                        continue
                    counter[gram] += 1
                    local.add(gram)
            for gram in local:
                documents[gram] = documents.get(gram, 0) + 1
        for word in re.findall(r"[A-Za-z]{3,}", text or ""):
            counter[word.lower()] += 1
            documents[word.lower()] = documents.get(word.lower(), 0) + 1
    return counter, documents


def _cjk_ratio(text: str) -> float:
    cleaned = re.sub(r"\s", "", text or "")
    if not cleaned:
        return 0.0
    return len(re.findall(r"[\u4e00-\u9fa5]", cleaned)) / len(cleaned)


def build_voice_profile(texts: list[str], *, name: str = "作者声音") -> dict[str, Any]:
    """从作者自己的文字里提取「指纹」：爱用词、标点习惯、句长与碎片句比例。

    只接受中文占多数的文本：编码错误或贴错内容时，宁可拒绝建画像，也不要存一份垃圾画像。
    """
    usable = [
        text for text in texts if count_words(text) >= 20 and _cjk_ratio(text) >= 0.5
    ]
    if not usable:
        return {}
    per_text = [measure(text) for text in usable]
    counter, documents = _term_candidates(usable)
    required_docs = min(2, len(usable))
    # 三级筛选：先要「多篇里反复出现」（真正的用词习惯），样本少或风格多样时逐级放宽，
    # 否则作者刚开始记碎片时画像会是空的 —— 那恰恰是他最需要能算出「像不像你」的时候。
    strict = [
        (gram, count)
        for gram, count in counter.items()
        if documents.get(gram, 0) >= required_docs and count >= required_docs
    ]
    if len(strict) >= 5:
        terms, term_rule = strict, "多篇重复"
    else:
        relaxed = [(gram, count) for gram, count in counter.items() if count >= 2]
        if len(relaxed) >= 5:
            terms, term_rule = relaxed, "篇内重复"
        else:
            terms = [(gram, count) for gram, count in counter.items() if count >= 1]
            term_rule = "出现即计入（样本较少）"
    terms.sort(key=lambda item: (-(item[1] * len(item[0])), item[0]))
    signature_terms = [gram for gram, _ in terms[:VOICE_TERM_LIMIT]]
    term_counts = {gram: count for gram, count in terms[:VOICE_TERM_LIMIT]}

    punctuation_total = sum(
        sum(text.count(mark) for mark in PUNCTUATION_SET) for text in usable
    ) or 1
    punctuation = {
        mark: round(sum(text.count(mark) for text in usable) / punctuation_total, 4)
        for mark in PUNCTUATION_SET
    }
    return {
        "metrics_version": f"{METRICS_VERSION}+voice",
        "sample_count": len(usable),
        "total_chars": sum(count_words(text) for text in usable),
        "signature_terms": signature_terms,
        "signature_term_counts": term_counts,
        "term_rule": term_rule,
        "punctuation": punctuation,
        "mean_sentence_len": round(mean([m.mean_sentence_len for m in per_text]), 3),
        "sentence_len_cv": round(mean([m.sentence_len_cv for m in per_text]), 3),
        "short_paragraph_ratio": round(mean([m.short_paragraph_ratio for m in per_text]), 3),
        "dash_ellipsis_per_1k": round(mean([m.dash_ellipsis_per_1k for m in per_text]), 3),
        "colloquial_per_1k": round(mean([m.colloquial_per_1k for m in per_text]), 3),
        "question_ratio": round(mean([m.question_ratio for m in per_text]), 3),
        "dialogue_ratio": round(mean([m.dialogue_ratio for m in per_text]), 3),
        "name": name,
        "source": VOICE_PROFILE_SOURCE,
    }


def save_voice_profile(
    session: Session,
    novel: Novel,
    texts: list[str],
    *,
    name: str = "作者声音",
    samples: list[str] | None = None,
) -> StyleProfile | None:
    profile = build_voice_profile(texts, name=name)
    if not profile:
        return None
    record = StyleProfile(
        novel_id=novel.id,
        name=name,
        source=VOICE_PROFILE_SOURCE,
        sample_count=profile["sample_count"],
        total_chars=profile["total_chars"],
        metrics=profile,
        samples=(samples or [])[:20],
        is_default=False,
    )
    session.add(record)
    session.flush()
    return record


def default_voice_profile(session: Session, novel_id: str) -> StyleProfile | None:
    return session.scalar(
        select(StyleProfile)
        .where(StyleProfile.novel_id == novel_id, StyleProfile.source == VOICE_PROFILE_SOURCE)
        .order_by(StyleProfile.created_at.desc())
    )


def voice_score(text: str, profile: StyleProfile | dict[str, Any] | None) -> dict[str, Any]:
    """声音保留分（0~100）：这段文字有多像作者本人。

    四个分项：作者特征词覆盖率、标点习惯相似度、句长习惯相似度、碎片句/停顿习惯相似度。
    它衡量的是「像不像你」，与「有没有 AI 味」是两个方向 —— 后者越低越好，前者不能丢。
    """
    metrics = profile.metrics if isinstance(profile, StyleProfile) else (profile or {})
    if not metrics or not metrics.get("signature_terms"):
        return {"score": None, "available": False, "signature_hits": [], "components": {}}
    measured = measure(text)
    chars = max(measured.total_chars, 1)
    terms = list(metrics.get("signature_terms") or [])
    hits = [term for term in terms if term in (text or "")]
    # 少量样本下不要把期望值定得太高：一篇短文本命中一两个特征词就够了
    expected = max(1.0, min(len(terms), chars / 400))
    signature = min(1.0, len(hits) / expected)

    punctuation = metrics.get("punctuation") or {}
    total_marks = sum(measured_punct_count(text, mark) for mark in PUNCTUATION_SET) or 1
    observed = {
        mark: measured_punct_count(text, mark) / total_marks for mark in PUNCTUATION_SET
    }
    punctuation_similarity = 1 - 0.5 * sum(
        abs(observed.get(mark, 0.0) - float(punctuation.get(mark, 0.0))) for mark in PUNCTUATION_SET
    )

    reference_cv = float(metrics.get("sentence_len_cv") or 0.0)
    sentence_similarity = 1 - min(
        1.0, abs(measured.sentence_len_cv - reference_cv) / max(reference_cv, 0.15)
    )

    reference_short = float(metrics.get("short_paragraph_ratio") or 0.0)
    short_similarity = 1 - min(
        1.0, abs(measured.short_paragraph_ratio - reference_short) / max(reference_short, 0.1)
    )

    score = 100 * (
        0.4 * signature
        + 0.2 * max(0.0, punctuation_similarity)
        + 0.2 * max(0.0, sentence_similarity)
        + 0.2 * max(0.0, short_similarity)
    )
    return {
        "score": round(score, 1),
        "available": True,
        "signature_hits": hits[:20],
        "signature_expected": round(expected, 2),
        "profile_terms": terms[:20],
        "components": {
            "signature_coverage": round(signature, 3),
            "punctuation_similarity": round(punctuation_similarity, 3),
            "sentence_similarity": round(sentence_similarity, 3),
            "short_paragraph_similarity": round(short_similarity, 3),
        },
    }


def measured_punct_count(text: str, mark: str) -> int:
    return (text or "").count(mark)


def voice_rules(
    text: str,
    profile: StyleProfile | dict[str, Any] | None,
    *,
    metrics: StyleMetrics | None = None,
    min_chars: int = 60,
) -> tuple[list[StyleIssue], dict[str, Any]]:
    """把「像不像作者」变成问题条目，并给出可执行建议。"""
    measured = metrics or measure(text)
    report = voice_score(text, profile)
    issues: list[StyleIssue] = []
    if not report.get("available") or measured.total_chars < min_chars:
        return issues, report

    if report["score"] < 55:
        issues.append(
            StyleIssue(
                code="VOICE_WEAK",
                level="warning",
                metric="voice_score",
                message=(
                    f"这段不太像你平时的写法（声音保留分 {report['score']}）："
                    f"作者特征词只命中 {len(report['signature_hits'])} 个"
                ),
                value=float(report["score"]),
                excerpt="、".join(report["signature_hits"][:6]) or "（一个都没命中）",
                suggestion=(
                    "把它改回你的说法：用你惯用的词与句式，允许不完整的句子与口语停顿；"
                    "如果这是有意换一种腔调，可以忽略这条"
                ),
            )
        )
    # 「过于规整」的参照系是作者自己：他用几种标点，他的文本就允许用几种
    profile_metrics_dict = (
        profile.metrics if isinstance(profile, StyleProfile) else (profile or {})
    ) or {}
    profile_punctuation = profile_metrics_dict.get("punctuation") if profile_metrics_dict else None
    if isinstance(profile_punctuation, dict) and profile_punctuation:
        profile_variety = len(
            [mark for mark, share in profile_punctuation.items() if float(share) > 0.001]
        ) / len(PUNCTUATION_SET)
        variety_floor = max(0.2, profile_variety * 0.6)
    else:
        variety_floor = 0.4
    if measured.punctuation_variety <= variety_floor and measured.short_paragraph_ratio == 0.0:
        issues.append(
            StyleIssue(
                code="OVER_REGULAR",
                level="warning",
                metric="punctuation_variety",
                message=(
                    f"标点只用了几种（{round(measured.punctuation_variety * len(PUNCTUATION_SET))}"
                    f"/{len(PUNCTUATION_SET)}），也没有短句成段：读起来像被磨平过"
                ),
                value=measured.punctuation_variety,
                excerpt=(text or "").strip()[:60],
                suggestion="允许破折号、省略号、单句成段出现；人写东西本来就不会句句工整",
            )
        )
    if (
        report["components"].get("punctuation_similarity", 1) < 0.6
        or report["components"].get("sentence_similarity", 1) < 0.5
    ):
        issues.append(
            StyleIssue(
                code="VOICE_DRIFT",
                level="info",
                metric="voice_score",
                message="标点或句长习惯与你的基线差别较大",
                value=float(report["score"]),
                excerpt="、".join(report["signature_hits"][:5]),
                suggestion="确认是有意为之即可；否则按你自己的断句习惯改一遍",
            )
        )
    return issues, report
