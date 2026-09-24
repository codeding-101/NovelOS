"""工具调用循环：让模型自己决定调用哪些 AITool，再把结果组织成回答。

与「Python 侧固定流程」的区别：工具由模型按问题选择，因此能回答「先查人物档案、
再按人物去查相关章节」这类多步问题。循环有轮数上限，且每一轮的工具参数都会
经过 Pydantic Schema 校验，模型无法绕过工具层直接碰数据库。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.ai.base import AIToolUnsupported, AIProvider, AIRequest
from app.ai.tools import AIToolKit

MAX_ROUNDS = 4


@dataclass
class ToolLoopResult:
    answer: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    warnings: list[str] = field(default_factory=list)
    citations: list[int] = field(default_factory=list)


def tool_schemas(kit: AIToolKit) -> list[dict[str, Any]]:
    """把 AITool 定义转成 OpenAI function calling 需要的 schema。"""
    return [
        {
            "type": "function",
            "function": {
                "name": definition.name,
                "description": definition.description,
                "parameters": definition.parameters,
            },
        }
        for definition in AIToolKit.definitions()
    ]


class ToolCallingAgent:
    """在给定工具箱上跑一个「模型选工具 → 执行 → 再问」的循环。"""

    def __init__(self, provider: AIProvider, kit: AIToolKit, *, max_rounds: int = MAX_ROUNDS) -> None:
        self.provider = provider
        self.kit = kit
        self.max_rounds = max_rounds

    def run(
        self,
        *,
        system: str,
        question: str,
        context: dict[str, Any] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> ToolLoopResult:
        tools = tool_schemas(self.kit)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ]
        executed: list[dict[str, Any]] = []
        warnings: list[str] = []
        citations: set[int] = set()
        provider_name = self.provider.name
        model = self.provider.model

        for _round in range(self.max_rounds):
            request = AIRequest(
                task="answer",
                system=system,
                prompt=question,
                messages=messages,
                context={"question": question, "entities": (context or {}).get("entities", [])},
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if not getattr(self.provider, "supports_tools", False):
                return ToolLoopResult(
                    answer="",
                    tool_calls=executed,
                    provider=provider_name,
                    model=model,
                    warnings=warnings + [f"提供者 {provider_name} 不支持工具调用，agent 模式不可用"],
                )
            try:
                response = self.provider.generate_with_tools(request, tools)
            except AIToolUnsupported as exc:
                return ToolLoopResult(
                    answer="",
                    tool_calls=executed,
                    provider=provider_name,
                    model=model,
                    warnings=warnings + [str(exc)],
                )
            provider_name, model = response.provider, response.model
            warnings.extend(response.warnings)

            if not response.tool_calls:
                answer = response.text.strip()
                if response.parsed and isinstance(response.parsed.get("answer"), str):
                    answer = response.parsed["answer"].strip()
                citations.update(_extract_citations(messages))
                return ToolLoopResult(
                    answer=answer,
                    tool_calls=executed,
                    provider=provider_name,
                    model=model,
                    warnings=warnings,
                    citations=sorted(citations),
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": response.text or "",
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                            },
                        }
                        for call in response.tool_calls
                    ],
                }
            )
            for call in response.tool_calls:
                result = self.kit.call(call["name"], call["arguments"])
                executed.append({"name": call["name"], "arguments": call["arguments"], "status": result.get("status")})
                citations.update(_citations_from_tool(result))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(result, ensure_ascii=False)[:6000],
                    }
                )

        return ToolLoopResult(
            answer="",
            tool_calls=executed,
            provider=provider_name,
            model=model,
            warnings=warnings + [f"工具调用超过 {self.max_rounds} 轮仍没有得到最终回答"],
            citations=sorted(citations),
        )


def _citations_from_tool(result: dict[str, Any]) -> set[int]:
    """从工具返回值里收集出现过的章号，作为允许被引用的范围。"""
    found: set[int] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("chapter_number", "source_chapter", "first_chapter") and isinstance(
                    value, int
                ):
                    found.add(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(result)
    return found


def _extract_citations(messages: list[dict[str, Any]]) -> set[int]:
    import re

    found: set[int] = set()
    for message in messages:
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        if isinstance(content, str):
            found.update(int(match) for match in re.findall(r"第(\d+)章", content))
        elif isinstance(content, dict):
            found.update(_citations_from_tool(content))
    return found
