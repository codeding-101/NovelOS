"""AI Provider 抽象层。

所有 AI 调用都必须经过 AIProvider 接口，业务代码不感知具体模型，
因此接入 Grok / OpenAI / 本地模型只需在配置里加一条 OpenAI 兼容端点。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

#: 工具调用在内部统一成这个结构
ToolCall = dict[str, Any]  # {"id": str, "name": str, "arguments": dict}


class AIError(RuntimeError):
    """AI 调用失败（网络、鉴权、限流、超时等）。"""


class AIResponseFormatError(AIError):
    """模型返回内容无法解析为约定结构。"""


class AIToolUnsupported(AIError):
    """当前提供者不支持工具调用。"""


@dataclass
class AIRequest:
    """一次 AI 调用的完整输入。

    task 用于让提供者识别业务场景（extract / continuity_narrative / answer / plan / write），
    真实 LLM 提供者主要使用 system + prompt；离线规则提供者则依赖 task + context。
    messages 供工具调用循环传入完整的多轮消息（设置后忽略 system/prompt）。
    """

    task: str
    system: str = ""
    prompt: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    json_schema: dict[str, Any] | None = None
    temperature: float = 0.3
    max_tokens: int = 4096
    messages: list[dict[str, Any]] | None = None


@dataclass
class AIResponse:
    text: str
    provider: str
    model: str
    parsed: dict[str, Any] | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""


class AIProvider(ABC):
    """AI 提供者接口。"""

    name: str = "base"
    kind: str = "llm"
    supports_tools: bool = False

    @property
    @abstractmethod
    def model(self) -> str:
        ...

    @abstractmethod
    def generate(self, request: AIRequest) -> AIResponse:
        """执行一次生成，返回文本与（可选的）结构化结果。"""

    def generate_with_tools(self, request: AIRequest, tools: list[dict[str, Any]]) -> AIResponse:
        raise AIToolUnsupported(f"提供者 {self.name} 不支持工具调用")

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model,
            "kind": self.kind,
            "available": True,
            "supports_tools": self.supports_tools,
            "detail": "",
        }
