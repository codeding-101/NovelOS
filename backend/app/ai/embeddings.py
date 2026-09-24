"""Embedding 提供者抽象。

两个实现：
- LocalHashingEmbedding：中文按 1/2/3 字符 n-gram 做带符号哈希投影，本地确定性生成，
  不需要网络与密钥，因此离线测试与降级都能用；它是词形层面的向量，不是训练出来的语义模型。
- OpenAICompatEmbedding：任何 OpenAI 兼容的 /embeddings 端点（OpenAI、本地 vLLM、其他厂商），
  配上以后混合检索就变成真正的语义召回，业务代码不需要改。
"""

from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod
from typing import Any

import httpx

from app.ai.base import AIError
from app.config import settings

_CJK_OR_WORD = re.compile(r"[\u4e00-\u9fa5]+|[A-Za-z0-9]+")


class EmbeddingProvider(ABC):
    name: str = "base"
    kind: str = "offline"

    @property
    @abstractmethod
    def dim(self) -> int:
        ...

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        ...

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "dim": self.dim, "kind": self.kind, "available": True}


class LocalHashingEmbedding(EmbeddingProvider):
    """本地确定性向量：n-gram 带符号哈希 + L2 归一化。"""

    name = "local-ngram"
    kind = "offline"

    def __init__(self, dim: int | None = None) -> None:
        self._dim = dim or settings.embedding_local_dim
        self._weights = {1: 0.5, 2: 1.0, 3: 0.8}

    @property
    def dim(self) -> int:
        return self._dim

    @staticmethod
    def _tokens(text: str) -> list[tuple[str, int]]:
        grams: list[tuple[str, int]] = []
        for chunk in _CJK_OR_WORD.findall(text or ""):
            for size, weight in ((1, 0.5), (2, 1.0), (3, 0.8)):
                if len(chunk) < size:
                    continue
                for start in range(len(chunk) - size + 1):
                    grams.append((chunk[start : start + size], size))
        return grams

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self._dim
            for gram, size in self._tokens(text):
                digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "big") % self._dim
                sign = 1.0 if digest[4] & 1 else -1.0
                vector[index] += sign * self._weights.get(size, 0.5)
            norm = math.sqrt(sum(value * value for value in vector))
            if norm > 0:
                vector = [value / norm for value in vector]
            vectors.append(vector)
        return vectors


class OpenAICompatEmbedding(EmbeddingProvider):
    name = "openai-embedding"
    kind = "remote"

    def __init__(
        self, base_url: str | None = None, model: str | None = None, api_key: str | None = None
    ) -> None:
        self.base_url = (base_url or settings.embedding_base_url).rstrip("/")
        self.model = model or settings.embedding_model
        self.api_key = api_key or settings.deepseek_api_key
        self._dim = 0

    @property
    def dim(self) -> int:
        return self._dim or 1536

    def describe(self) -> dict[str, Any]:
        info = super().describe()
        info["available"] = bool(self.base_url and self.api_key)
        if not info["available"]:
            info["detail"] = "未配置 NOVELOS_EMBEDDING_BASE_URL / API Key"
        return info

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not (self.base_url and self.api_key):
            raise AIError("远程 embedding 未配置 base_url 或 API Key")
        vectors: list[list[float]] = []
        batch = 32
        for start in range(0, len(texts), batch):
            chunk = texts[start : start + batch]
            try:
                response = httpx.post(
                    f"{self.base_url}/embeddings",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": self.model, "input": chunk},
                    timeout=settings.request_timeout_seconds,
                )
            except httpx.HTTPError as exc:
                raise AIError(f"embedding 请求失败：{exc}") from exc
            if response.status_code != 200:
                raise AIError(f"embedding 返回错误 {response.status_code}：{response.text[:200]}")
            payload = response.json()
            for item in payload.get("data", []):
                vector = [float(value) for value in item.get("embedding", [])]
                self._dim = len(vector) or self._dim
                vectors.append(vector)
        return vectors


def build_embedding_provider(name: str | None = None) -> tuple[EmbeddingProvider, list[str]]:
    """返回 (provider, warnings)。远程不可用时自动退回本地向量。"""
    requested = (name or settings.embedding_provider or "local").strip().lower()
    if requested in ("openai", "remote"):
        provider = OpenAICompatEmbedding()
        if not provider.describe()["available"]:
            return LocalHashingEmbedding(), [
                "远程 embedding 未配置完整，已退回本地 n-gram 向量（语义召回能力有限）"
            ]
        return provider, []
    return LocalHashingEmbedding(), []
