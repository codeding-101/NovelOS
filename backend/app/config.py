"""运行期配置。环境变量优先，其次读取 backend/.env，最后使用默认值。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = BACKEND_DIR.parent / "data"


def _load_dotenv() -> None:
    """把 backend/.env 里的键值对注入 os.environ（已存在的环境变量优先）。"""
    env_file = BACKEND_DIR / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_path(name: str, default: Path) -> Path:
    return Path(_env_str(name, str(default)))


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: _env_path("NOVELOS_DATA_DIR", DEFAULT_DATA_DIR))
    db_path: Path = field(
        default_factory=lambda: _env_path("NOVELOS_DB_PATH", DEFAULT_DATA_DIR / "novelos.db")
    )
    ai_provider: str = field(default_factory=lambda: _env_str("NOVELOS_AI_PROVIDER", "deepseek"))
    #: Obsidian 笔记库路径（不填就从 Obsidian 自己的配置里探测）
    obsidian_vault: str = field(default_factory=lambda: _env_str("NOVELOS_OBSIDIAN_VAULT", ""))
    deepseek_api_key: str = field(default_factory=lambda: _env_str("DEEPSEEK_API_KEY", ""))
    deepseek_base_url: str = field(
        default_factory=lambda: _env_str("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    )
    deepseek_model: str = field(default_factory=lambda: _env_str("DEEPSEEK_MODEL", "deepseek-flash"))
    #: 外部模型（任何 OpenAI 兼容端点），JSON 数组：
    #: [{"name":"grok","base_url":"https://api.x.ai/v1","model":"grok-4","api_key_env":"XAI_API_KEY"}]
    providers_json: str = field(default_factory=lambda: _env_str("NOVELOS_PROVIDERS", "[]"))
    #: 语义检索用的向量提供者：local（本地字符 n-gram，无需网络）| openai（远程 embedding）
    embedding_provider: str = field(
        default_factory=lambda: _env_str("NOVELOS_EMBEDDING_PROVIDER", "local")
    )
    embedding_base_url: str = field(
        default_factory=lambda: _env_str("NOVELOS_EMBEDDING_BASE_URL", "")
    )
    embedding_model: str = field(
        default_factory=lambda: _env_str("NOVELOS_EMBEDDING_MODEL", "text-embedding-3-small")
    )
    embedding_local_dim: int = 384
    request_timeout_seconds: float = 120.0
    max_retries: int = 3
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            origin.strip()
            for origin in _env_str(
                "NOVELOS_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
            ).split(",")
            if origin.strip()
        )
    )

    @property
    def database_url(self) -> str:
        return f"sqlite+pysqlite:///{self.db_path.as_posix()}"

    @property
    def chapters_dir(self) -> Path:
        return self.data_dir / "novels"

    def chapter_file(self, novel_slug: str, chapter_number: int) -> Path:
        return self.chapters_dir / novel_slug / f"ch{chapter_number:03d}.md"

    def deepseek_available(self) -> bool:
        return bool(self.deepseek_api_key)

    def external_providers(self) -> list[dict]:
        """解析 NOVELOS_PROVIDERS，返回可用的外部 OpenAI 兼容提供者配置。"""
        try:
            raw = json.loads(self.providers_json or "[]")
        except json.JSONDecodeError:
            return []
        providers: list[dict] = []
        if not isinstance(raw, list):
            return providers
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            base_url = str(item.get("base_url") or "").strip()
            model = str(item.get("model") or "").strip()
            if not (name and base_url and model):
                continue
            api_key = str(item.get("api_key") or "")
            if not api_key and item.get("api_key_env"):
                api_key = os.environ.get(str(item["api_key_env"]), "")
            providers.append(
                {
                    "name": name,
                    "base_url": base_url,
                    "model": model,
                    "api_key": api_key,
                    "label": str(item.get("label") or name),
                }
            )
        return providers

    def external_provider(self, name: str) -> dict | None:
        for provider in self.external_providers():
            if provider["name"] == name:
                return provider
        return None


settings = Settings()
