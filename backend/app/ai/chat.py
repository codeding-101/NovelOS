"""通用 OpenAI 兼容提供者。

DeepSeek、Grok、OpenAI 以及本地 vLLM/Ollama 都走同一套 /chat/completions 协议，
因此这里实现一次，其余提供者只提供 base_url / model / api_key 三个参数。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.ai.base import AIError, AIProvider, AIRequest, AIResponse
from app.ai.json_utils import extract_json
from app.config import settings


class ChatCompletionsProvider(AIProvider):
    kind = "llm"
    supports_tools = True

    def __init__(
        self,
        name: str,
        base_url: str,
        model: str,
        api_key: str,
        *,
        label: str = "",
        timeout: float | None = None,
        max_retries: int | None = None,
        supports_tools: bool = True,
    ) -> None:
        self.name = name
        self.label = label or name
        self.base_url = base_url.rstrip("/")
        self._model = model
        self.api_key = api_key
        self.timeout = timeout or settings.request_timeout_seconds
        self.max_retries = max_retries or settings.max_retries
        self.supports_tools = supports_tools

    @property
    def model(self) -> str:
        return self._model

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model,
            "kind": self.kind,
            "available": bool(self.api_key),
            "supports_tools": self.supports_tools,
            "detail": "" if self.api_key else f"{self.label} 未配置 API Key",
        }

    # ------------------------------------------------------------------ 请求
    def _messages(self, request: AIRequest) -> list[dict[str, Any]]:
        if request.messages is not None:
            return request.messages
        return [
            {"role": "system", "content": request.system},
            {"role": "user", "content": request.prompt},
        ]

    def _payload(
        self, request: AIRequest, tools: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(request),
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        elif request.json_schema is not None:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise AIError(f"{self.label} 未配置 API Key，无法调用")
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = httpx.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.timeout,
                )
            except httpx.HTTPError as exc:
                last_error = AIError(f"网络错误：{exc}")
                time.sleep(min(2**attempt, 8))
                continue

            if response.status_code == 200:
                return response.json()

            if response.status_code in (401, 403):
                raise AIError(f"{self.label} 鉴权失败（HTTP {response.status_code}）")
            if response.status_code == 429 or response.status_code >= 500:
                last_error = AIError(
                    f"{self.label} 暂时不可用（HTTP {response.status_code}）：{response.text[:200]}"
                )
                time.sleep(min(2**attempt, 8))
                continue
            raise AIError(f"{self.label} 返回错误（HTTP {response.status_code}）：{response.text[:300]}")

        raise AIError(f"{self.label} 调用失败，已重试 {self.max_retries} 次：{last_error}")

    @staticmethod
    def _parse_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        for item in message.get("tool_calls") or []:
            if not isinstance(item, dict):
                continue
            function = item.get("function") or {}
            raw_arguments = function.get("arguments") or "{}"
            try:
                arguments = extract_json(raw_arguments) or {}
            except Exception:  # noqa: BLE001 - 模型可能给出非法参数，交给上层按 Schema 校验
                arguments = {}
            calls.append(
                {
                    "id": item.get("id") or f"call_{len(calls)}",
                    "name": function.get("name") or "",
                    "arguments": arguments,
                    "raw_arguments": raw_arguments,
                }
            )
        return calls

    def _build_response(self, data: dict[str, Any], request: AIRequest) -> AIResponse:
        choices = data.get("choices") or []
        message = (choices[0].get("message") or {}) if choices else {}
        text = message.get("content") or ""
        finish_reason = choices[0].get("finish_reason", "") if choices else ""
        warnings: list[str] = []
        if finish_reason == "length":
            # 截断是静默丢信息的主要来源（抽取少了几条、计划只写了一半），必须显式说出来
            warnings.append(
                "模型输出达到 max_tokens 被截断，这一轮结果可能不完整；可缩小范围或提高上限后重试"
            )
        parsed = None
        if request.json_schema is not None and not message.get("tool_calls"):
            parsed = extract_json(text)
            if parsed is None:
                warnings.append("模型未返回可解析的 JSON")
        return AIResponse(
            text=text,
            parsed=parsed,
            provider=self.name,
            model=self.model,
            usage=data.get("usage") or {},
            warnings=warnings,
            tool_calls=self._parse_tool_calls(message),
            finish_reason=finish_reason,
        )

    # ------------------------------------------------------------------ 调用
    def generate(self, request: AIRequest) -> AIResponse:
        return self._build_response(self._post(self._payload(request)), request)

    def generate_with_tools(
        self, request: AIRequest, tools: list[dict[str, Any]]
    ) -> AIResponse:
        if not self.supports_tools:
            return super().generate_with_tools(request, tools)
        return self._build_response(self._post(self._payload(request, tools)), request)
