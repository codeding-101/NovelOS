"""中文文本工具：字数统计、中文数字解析、故事时间排序键。"""

from __future__ import annotations

import re

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "廿": 20, "卅": 30,
}


def count_words(text: str) -> int:
    """中文小说字数：统计所有非空白字符（含汉字、数字、标点）。"""
    if not text:
        return 0
    return len(re.sub(r"\s+", "", text))


def cn_number_to_int(token: str) -> int | None:
    """把中文数字转成整数，支持 一/十/十五/二十/廿五/廿/三十/初七 这类写法。"""
    token = token.strip()
    if not token:
        return None
    if token.startswith("初"):
        return cn_number_to_int(token[1:])
    if token.isdigit():
        return int(token)
    if token.startswith("廿"):
        rest = token[1:]
        return 20 + (cn_number_to_int(rest) or 0)
    if token.startswith("卅"):
        rest = token[1:]
        return 30 + (cn_number_to_int(rest) or 0)
    if token == "十":
        return 10
    if "十" in token:
        head, _, tail = token.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        if head and head not in _CN_DIGITS:
            return None
        if tail and tail not in _CN_DIGITS:
            return None
        return tens * 10 + ones
    if len(token) == 1 and token in _CN_DIGITS:
        return _CN_DIGITS[token]
    return None


_DAY_PATTERN = r"(?:初[零〇一二三四五六七八九十]|[零〇一二三四五六七八九十廿卅两\d]{1,3})"

# 故事时间形如「天启三年四月初五」「天启三年三月」「天启三年闰四月」「天启三年五月廿五夜」
_STORY_TIME_RE = re.compile(
    r"(?P<era>[\u4e00-\u9fa5]{0,4}?)"
    r"(?P<year>[零〇一二三四五六七八九十百廿卅两\d]+)年"
    r"(?:闰)?(?P<month>[零〇一二三四五六七八九十廿卅两\d]{1,3})月"
    rf"(?:(?P<day>{_DAY_PATTERN})[日号]?)?"
)


def parse_story_time(value: str | None) -> tuple[int | None, int | None, int | None]:
    """解析故事时间字符串，返回 (年, 月, 日)。解析失败返回 (None, None, None)。"""
    if not value:
        return (None, None, None)
    match = _STORY_TIME_RE.search(value)
    if not match:
        return (None, None, None)
    year = cn_number_to_int(match.group("year"))
    month = cn_number_to_int(match.group("month"))
    day = cn_number_to_int(match.group("day")) if match.group("day") else None
    return (year, month, day)


def story_time_sort_key(value: str | None) -> int:
    """把故事时间压成可排序整数：年*10000 + 月*100 + 日。无法解析时返回一个大数（排在最后）。"""
    year, month, day = parse_story_time(value)
    if year is None:
        return 9_999_999
    return year * 10_000 + (month or 0) * 100 + (day or 0)


def format_chapter_ref(chapter_number: int | None) -> str:
    """章节引用文本，例如 3 -> 第3章。"""
    if chapter_number is None:
        return "未知章节"
    return f"第{chapter_number}章"

def chapter_number_from_ref(ref: str | None) -> int | None:
    """从「第3章」「ch03」「3」这类引用里取出章号。"""
    if not ref:
        return None
    text = str(ref).strip()
    match = re.search(r"(\d+)", text)
    if match:
        return int(match.group(1))
    cn_match = re.search(r"第([零〇一二三四五六七八九十百廿卅两]+)章", text)
    if cn_match:
        return cn_number_to_int(cn_match.group(1))
    return None


def split_sentences(text: str) -> list[str]:
    """按中文标点切句，保留标点。"""
    if not text:
        return []
    parts = re.split(r"(?<=[。！？!?；;…])", text)
    return [part.strip() for part in parts if part and part.strip()]


_WS_RE = re.compile(r"\s+")


def normalize_title(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip())


#: 期限推算用的近似历法：一个月按 30 天算（本书是虚构历法，只需要可比较、可显示）
DAYS_PER_MONTH = 30
MONTHS_PER_YEAR = 12
_CN_NUMERALS = "零一二三四五六七八九十"


def story_time_to_day(value: str | None) -> int | None:
    """把故事时间换算成「绝对天数」，用于推算期限与比较先后（解析失败返回 None）。"""
    year, month, day = parse_story_time(value)
    if year is None:
        return None
    return (year - 1) * MONTHS_PER_YEAR * DAYS_PER_MONTH + ((month or 1) - 1) * DAYS_PER_MONTH + ((day or 1) - 1)


def day_to_story_time(total_days: int, *, era: str = "", leap: bool = False) -> str:
    """把绝对天数还原成可读的故事时间（与 story_time_to_day 互逆）。

    日期按中文习惯渲染：1~10 为「初N」，11~19 为「十N」，20 为「二十」，21~29 为「廿N」，30 为「三十」。
    """
    year = total_days // (MONTHS_PER_YEAR * DAYS_PER_MONTH) + 1
    remainder = total_days % (MONTHS_PER_YEAR * DAYS_PER_MONTH)
    month = remainder // DAYS_PER_MONTH + 1
    day = remainder % DAYS_PER_MONTH + 1
    return f"{era}{_int_to_cn(year)}年{'闰' if leap else ''}{_int_to_cn(month)}月{_cn_day(day)}"


def _cn_day(day: int) -> str:
    if day <= 10:
        return "初" + _CN_NUMERALS[day]
    if day < 20:
        return "十" + (_CN_NUMERALS[day - 10] if day > 10 else "")
    if day == 20:
        return "二十"
    if day < 30:
        return "廿" + _CN_NUMERALS[day - 20]
    return "三十" if day == 30 else _int_to_cn(day)


def _int_to_cn(value: int) -> str:
    """1..99 转中文数字，用于把推算结果写成「五月十七」这种样子。"""
    if value <= 10:
        return _CN_NUMERALS[value]
    if value < 20:
        return "十" + (_CN_NUMERALS[value - 10] if value > 10 else "")
    tens, ones = divmod(value, 10)
    return _CN_NUMERALS[tens] + "十" + (_CN_NUMERALS[ones] if ones else "")


def story_time_era(value: str | None) -> str:
    """取出年号前缀，例如「天启三年四月初五」-> 「天启」。"""
    match = re.match(r"^([\u4e00-\u9fa5]{1,4}?)[零〇一二三四五六七八九十百两\d]+年", (value or "").strip())
    return match.group(1) if match else ""


_DEADLINE_RULES: tuple[tuple[re.Pattern[str], int | str], ...] = (
    (re.compile(r"(\d+|[零〇一二三四五六七八九十两廿卅]+)\s*(?:天|日)\s*(?:之后|后|以后)"), "days"),
    (re.compile(r"(\d+|[零〇一二三四五六七八九十两廿卅]+)\s*(?:天|日)\s*(?:之内|内|以内)"), "days"),
    (re.compile(r"(?:明|次)日"), 1),
    (re.compile(r"(?:今|当)晚|今夜|今晚|子时"), 0),
    (re.compile(r"天亮(?:之)?(?:前|后)"), 0),
    (re.compile(r"天黑(?:之)?(?:前|后)"), 0),
    (re.compile(r"(\d+|[零〇一二三四五六七八九十两廿卅]+)\s*个时辰(?:之)?(?:后|内)"), "hours"),
    (re.compile(r"(\d+|[零〇一二三四五六七八九十两廿卅]+)\s*个月(?:之)?(?:后|内)"), "months"),
)


def parse_deadline(text: str) -> tuple[int | None, str]:
    """解析期限表达，返回 (相对天数, 原文片段)。只认文本里明确写出的说法。"""
    for pattern, kind in _DEADLINE_RULES:
        match = pattern.search(text or "")
        if not match:
            continue
        raw = match.group(0)
        if isinstance(kind, int):
            return kind, raw
        token = match.group(1) if match.groups() else ""
        value = cn_number_to_int(token) if token else None
        if value is None:
            continue
        if kind == "days":
            return value, raw
        if kind == "months":
            return value * DAYS_PER_MONTH, raw
        if kind == "hours":
            return max(1, value // 12), raw
    return None, ""
