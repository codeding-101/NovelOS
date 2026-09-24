"""DeepSeek Flash 提供者（默认 AI）。

走通用 OpenAI 兼容通道，只是把 base_url / model / 密钥换成 DeepSeek 的默认值。
"""

from __future__ import annotations

from typing import Any

from app.ai.chat import ChatCompletionsProvider
from app.config import settings


class DeepSeekProvider(ChatCompletionsProvider):
    name = "deepseek"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        super().__init__(
            name="deepseek",
            base_url=base_url or settings.deepseek_base_url,
            model=model or settings.deepseek_model,
            api_key=api_key if api_key is not None else settings.deepseek_api_key,
            label="DeepSeek",
            timeout=timeout,
            max_retries=max_retries,
        )

    def describe(self) -> dict[str, Any]:
        info = super().describe()
        info["detail"] = "" if self.api_key else "未配置 DEEPSEEK_API_KEY"
        return info
