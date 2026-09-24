"""AI 提供者工厂：业务代码只通过这里拿到 provider，便于替换模型。

注册表 = 内置提供者（deepseek / offline）+ 配置里的外部 OpenAI 兼容端点
（NOVELOS_PROVIDERS，可指向 Grok、OpenAI、本地 vLLM 等）。
"""

from __future__ import annotations

from app.ai.base import AIError, AIProvider
from app.ai.chat import ChatCompletionsProvider
from app.ai.deepseek import DeepSeekProvider
from app.ai.offline import OfflineProvider
from app.config import settings

_BUILTIN: dict[str, type[AIProvider]] = {
    "deepseek": DeepSeekProvider,
    "offline": OfflineProvider,
}


def provider_names() -> list[str]:
    names = list(_BUILTIN)
    names.extend(
        provider["name"]
        for provider in settings.external_providers()
        if provider["name"] not in names
    )
    return names


def build_provider(name: str) -> AIProvider:
    key = (name or "").strip().lower()
    factory = _BUILTIN.get(key)
    if factory is not None:
        return factory()
    external = settings.external_provider(key)
    if external is not None:
        return ChatCompletionsProvider(
            name=external["name"],
            base_url=external["base_url"],
            model=external["model"],
            api_key=external["api_key"],
            label=external["label"],
        )
    raise AIError(f"未知的 AI 提供者：{name}，可选 {', '.join(provider_names())}")


def resolve_provider(name: str | None = None) -> tuple[AIProvider, list[str]]:
    """返回 (provider, warnings)。

    未显式指定时用配置里的默认提供者；若默认提供者是 deepseek 但没有 API Key，
    自动降级到离线规则提供者，并把降级原因作为 warning 返回给调用方，绝不静默失败。
    """
    requested = (name or settings.ai_provider or "deepseek").strip().lower()
    if requested not in provider_names():
        raise AIError(f"未知的 AI 提供者：{name}，可选 {', '.join(provider_names())}")

    if requested == "deepseek" and not settings.deepseek_available():
        return build_provider("offline"), [
            "未配置 DEEPSEEK_API_KEY，已自动降级为离线规则提供者（结果精度较低）"
        ]
    provider = build_provider(requested)
    warnings: list[str] = []
    if not getattr(provider, "api_key", "x") and provider.kind == "llm":
        warnings.append(f"提供者 {provider.name} 未配置 API Key，调用会失败")
    return provider, warnings


def describe_providers() -> list[dict]:
    infos: list[dict] = []
    for name in provider_names():
        try:
            infos.append(build_provider(name).describe())
        except AIError as exc:  # pragma: no cover - 注册表内已知类型不会失败
            infos.append(
                {
                    "name": name,
                    "model": "",
                    "kind": "unknown",
                    "available": False,
                    "supports_tools": False,
                    "detail": str(exc),
                }
            )
    return infos
