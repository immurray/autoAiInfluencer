"""FastAPI 应用入口，整合 AI 虚拟人自动发帖流水线。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
import asyncio
import logging
import os
import uuid

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from auto_ai_influencer.logging_config import setup_logging
from auto_ai_influencer.poster import TweetPoster

from .config import load_settings
from .database import Database
from .pipeline.caption_provider import CaptionProvider
from .pipeline.image_provider import ImageProvider
from .scheduler import PipelineScheduler

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


class AppContext:
    """应用运行所需的状态集合。"""

    def __init__(self, config_path: Path) -> None:
        self._config_path = config_path
        self.reload()

    @property
    def config_path(self) -> Path:
        return self._config_path

    def reload(self) -> None:
        app_config, ai_config, raw_config = load_settings(self._config_path)
        setup_logging(app_config.log_path)

        self.app_config = app_config
        self.ai_config = ai_config
        self.raw_config = raw_config
        self.database = Database(app_config.database_path)
        self.image_provider = ImageProvider(ai_config, self.database)
        self.caption_provider = CaptionProvider(ai_config, self.database, raw_config)
        self.poster = TweetPoster(app_config.twitter, app_config.dry_run)
        self.scheduler = PipelineScheduler(
            config=ai_config,
            image_provider=self.image_provider,
            caption_provider=self.caption_provider,
            poster=self.poster,
            database=self.database,
        )


def create_app(config_path: Path | None = None) -> FastAPI:
    config_file = config_path or Path(os.getenv("AI_PIPELINE_CONFIG", "config.json")).resolve()
    if not config_file.exists():
        raise FileNotFoundError(f"未找到配置文件：{config_file}")

    context = AppContext(config_file)

    app = FastAPI(title="AI 虚拟人自动运营平台", version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.context = context

    @app.on_event("startup")
    async def _startup() -> None:
        logging.getLogger(__name__).info("FastAPI 服务启动，配置文件：%s", context.config_path)
        await context.scheduler.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await context.scheduler.shutdown()

    def get_context() -> AppContext:
        return app.state.context  # type: ignore[attr-defined]

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        ctx = get_context()
        return {
            "status": "ok",
            "dry_run": ctx.poster.dry_run,
            "ai_pipeline": {
                "enable": ctx.ai_config.enable,
                "post_slots": ctx.ai_config.post_slots,
                "image_source": ctx.ai_config.image_source,
            },
        }

    @app.get("/posts/history")
    async def post_history(limit: int = 20, ctx: AppContext = Depends(get_context)) -> Dict[str, Any]:
        records = ctx.database.fetch_post_history(limit=limit)
        return {"items": records, "count": len(records)}

    @app.get("/captions/logs")
    async def caption_logs(limit: int = 20, ctx: AppContext = Depends(get_context)) -> Dict[str, Any]:
        records = ctx.database.fetch_caption_logs(limit=limit)
        return {"items": records, "count": len(records)}

    @app.post("/pipeline/run")
    async def pipeline_run(ctx: AppContext = Depends(get_context)) -> Dict[str, Any]:
        asyncio.create_task(ctx.scheduler.run_once())
        note = "流水线已禁用，仅手动触发执行" if not ctx.ai_config.enable else None
        payload = {"message": "流水线任务已加入队列"}
        if note:
            payload["note"] = note
        return payload

    @app.post("/config/reload")
    async def reload_config(ctx: AppContext = Depends(get_context)) -> Dict[str, Any]:
        await ctx.scheduler.shutdown()
        ctx.reload()
        await ctx.scheduler.start()
        return {"message": "配置已重新加载"}

    @app.post("/images/upload")
    async def upload_image(
        file: UploadFile = File(...),
        ctx: AppContext = Depends(get_context),
    ) -> Dict[str, Any]:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in _IMAGE_EXTENSIONS:
            raise HTTPException(status_code=400, detail="仅支持图片格式上传。")

        filename = f"upload_{uuid.uuid4().hex[:8]}{suffix}"
        target = ctx.ai_config.ready_directory / filename
        content = await file.read()
        target.write_bytes(content)
        return {"message": "上传成功", "filename": filename}

    return app


app = create_app()
