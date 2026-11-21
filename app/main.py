"""FastAPI 入口：启动建表 + 可选演示数据，挂载全部路由。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import settings
from .database import SessionLocal, init_db
from .routers_batches import router as batches_router
from .routers_publish import router as publish_router
from .routers_taxonomy import router as taxonomy_router
from .seed import seed_if_empty


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if settings.seed_demo_on_startup:
        with SessionLocal() as db:
            seed_if_empty(db)
    yield


app = FastAPI(
    title="生物多样性调查数据 API",
    description=(
        "研究员上传观测记录与版本化分类表；发布批次时按分类表解析名称，"
        "遇到环路、未知名或同名歧义则整批待核；合法记录按样地+接受种汇总，"
        "保留原名与分类表版本。分类表修订只使受影响批次过期，"
        "重发布成功后才替换结果，失败时继续提供上一份完整版本。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(taxonomy_router)
app.include_router(batches_router)
app.include_router(publish_router)


@app.get("/health", tags=["系统"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}
