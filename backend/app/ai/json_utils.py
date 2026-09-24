"""模型输出的 JSON 容错解析。

模型经常把 JSON 包在 ```json 代码块里，或在末尾多带一句话。这里做的是
「尽力提取第一个完整的 JSON 对象」，解析失败返回 None，由调用方决定重试或报错。
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def strip_code_fence(text: str) -> str:
    if not text:
        return ""
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def _find_balanced_object(text: str) -> str | None:
    """扫描出第一个花括号配平的片段（跳过字符串内的括号）。"""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        start = text.find("{", start + 1)
    return None


def _loosen(raw: str) -> str:
    """去掉尾随逗号这类常见小毛病。"""
    return re.sub(r",\s*([}\]])", r"\1", raw)


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出中提取 JSON 对象，失败返回 None。"""
    if not text:
        return None
    candidates: list[str] = []
    stripped = strip_code_fence(text)
    candidates.append(stripped)
    balanced = _find_balanced_object(stripped)
    if balanced:
        candidates.append(balanced)
    balanced_all = _find_balanced_object(text)
    if balanced_all:
        candidates.append(balanced_all)
    for candidate in candidates:
        for attempt in (candidate, _loosen(candidate)):
            try:
                parsed = json.loads(attempt)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def parse_issues_payload(text: str) -> list[dict[str, Any]]:
    """解析叙事通道返回的问题列表：既支持 {"issues": [...]}，也支持直接是数组。"""
    parsed = extract_json(text)
    if isinstance(parsed, dict):
        for key in ("issues", "problems", "results"):
            value = parsed.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []
    if not text:
        return []
    array_match = re.search(r"\[.*\]", strip_code_fence(text), re.DOTALL)
    if array_match:
        try:
            value = json.loads(_loosen(array_match.group(0)))
        except json.JSONDecodeError:
            return []
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []
