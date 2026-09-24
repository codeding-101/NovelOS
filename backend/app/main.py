"""FastAPI 应用入口。

启动：
    cd backend
    pip install -r requirements.txt
    uvicorn app.main:app --reload --port 8000
文档：
    http://127.0.0.1:8000/docs
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.config import settings
from app.database import init_db
from app.routers import ai, chapters, entities, fragments, insight, novels, plans, quality


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.chapters_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    yield


app = FastAPI(
    title="NovelOS V0.6",
    description=(
        "中文长篇小说 AI 辅助创作系统：长期记忆 + Canon 一致性守卫。"
        "AI 只能提出 PROPOSED 事实，只有作者确认后才升级为 CANON。"
    ),
    version=__version__,
    lifespan=lifespan,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins) or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(novels.router)
app.include_router(chapters.novel_router)
app.include_router(chapters.chapter_router)
app.include_router(entities.novel_router)
app.include_router(entities.item_router)
app.include_router(insight.router)
app.include_router(plans.router)
app.include_router(plans.item_router)
app.include_router(quality.novel_router)
app.include_router(quality.chapter_router)
app.include_router(quality.item_router)
app.include_router(fragments.novel_router)
app.include_router(fragments.item_router)
app.include_router(ai.router)


@app.get("/", include_in_schema=False)
def index() -> dict:
    return {
        "name": "NovelOS V0.6",
        "version": __version__,
        "docs": "/docs",
        "health": "/api/health",
    }
