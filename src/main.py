"""FastAPI 应用入口，整合 AI 虚拟人自动发帖流水线。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import asyncio
import json
import logging
import os
import uuid

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from auto_ai_influencer.logging_config import setup_logging
from auto_ai_influencer.poster import TweetPoster

from .config import load_settings
from .database import Database
from .pipeline.caption_provider import CaptionProvider
from .pipeline.image_provider import ImageProvider
from .scheduler import PipelineScheduler
from . import __version__

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_INDEX_HTML_PATH = Path(__file__).with_name("index.html")


def _load_index_html() -> str:
    """读取首页 HTML 内容，失败时返回降级提示。"""

    try:
        return _INDEX_HTML_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logging.getLogger(__name__).warning("未找到 index.html，返回降级页面。")
    except OSError as exc:
        logging.getLogger(__name__).warning("读取 index.html 失败：%s", exc)
    return "<h1>控制台页面暂时不可用，请检查服务端日志。</h1>"


class AppContext:
    """应用运行所需的状态集合。"""

    def __init__(self, config_path: Path) -> None:
        self._config_path = config_path
        self._override_path: Optional[Path] = None
        self._logger = logging.getLogger(__name__)
        self.reload()

    @property
    def config_path(self) -> Path:
        return self._config_path

    def _resolve_config_path(self, value: str) -> Path:
        raw = Path(value)
        return raw if raw.is_absolute() else (self._config_path.parent / raw).resolve()

    @staticmethod
    def _clean_template_items(values: List[Any]) -> List[str]:
        items: List[str] = []
        for item in values:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                items.append(text)
        return items

    def _resolve_prompt_content(self, caption: Dict[str, Any]) -> str:
        inline_prompt = str(caption.get("prompt", "") or "").strip()
        prompt_file = caption.get("prompt_file")
        if not prompt_file:
            return inline_prompt

        path = self._resolve_config_path(prompt_file)
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._logger.warning("未找到提示词文件 %s，已回退至内联配置。", path)
            return inline_prompt
        except OSError as exc:
            self._logger.warning("读取提示词文件 %s 失败：%s，已回退至内联配置。", path, exc)
            return inline_prompt

        stripped = content.strip()
        if not stripped:
            self._logger.warning("提示词文件 %s 内容为空，已回退至内联配置。", path)
            return inline_prompt

        return stripped

    def _resolve_templates(self, caption: Dict[str, Any]) -> List[str]:
        inline_templates = self._clean_template_items(caption.get("templates", []))
        templates_file = caption.get("templates_file")
        if not templates_file:
            return inline_templates

        path = self._resolve_config_path(templates_file)
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._logger.warning("未找到模板文件 %s，已回退至内联配置。", path)
            return inline_templates
        except OSError as exc:
            self._logger.warning("读取模板文件 %s 失败：%s，已回退至内联配置。", path, exc)
            return inline_templates

        stripped = content.strip()
        if not stripped:
            self._logger.warning("模板文件 %s 内容为空，已回退至内联配置。", path)
            return inline_templates

        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None

        if isinstance(parsed, list):
            cleaned = self._clean_template_items(parsed)
            if cleaned:
                return cleaned
            self._logger.warning("模板文件 %s 中的 JSON 列表为空，已回退至内联配置。", path)
            return inline_templates

        lines = [line.strip() for line in stripped.splitlines() if line.strip()]
        if lines:
            return lines

        self._logger.warning("模板文件 %s 内容无法解析，已回退至内联配置。", path)
        return inline_templates

    def reload(self) -> None:
        app_config, ai_config, raw_config, override_path = load_settings(self._config_path)
        setup_logging(app_config.log_path)

        self.app_config = app_config
        self.ai_config = ai_config
        self.raw_config = raw_config
        self._override_path = override_path
        self.database = Database(app_config.database_path)
        self.image_provider = ImageProvider(ai_config, self.database)
        self.caption_provider = CaptionProvider(
            ai_config,
            self.database,
            raw_config,
            self._config_path.parent,
        )
        self.poster = TweetPoster(app_config.twitter, app_config.dry_run)
        self.scheduler = PipelineScheduler(
            config=ai_config,
            image_provider=self.image_provider,
            caption_provider=self.caption_provider,
            poster=self.poster,
            database=self.database,
        )

    def _serialize_ai_config(self) -> Dict[str, Any]:
        """将 AI 流水线配置转换为可返回的字典。"""

        return {
            "enable": self.ai_config.enable,
            "post_slots": self.ai_config.post_slots,
            "image_source": self.ai_config.image_source,
            "prompt_template": self.ai_config.prompt_template,
            "caption_style": self.ai_config.caption_style,
            "openai_api_key": self.ai_config.openai_api_key,
            "replicate_model": self.ai_config.replicate_model,
            "replicate_model_version": self.ai_config.replicate_model_version,
            "replicate_token": self.ai_config.replicate_token,
            "leonardo_model": self.ai_config.leonardo_model,
            "leonardo_token": self.ai_config.leonardo_token,
            "ready_directory": str(self.ai_config.ready_directory),
            "caption_log_directory": str(self.ai_config.caption_log_directory),
            "timezone": self.ai_config.timezone,
            "default_image": str(self.ai_config.default_image),
        }

    def get_settings_snapshot(self) -> Dict[str, Any]:
        """返回当前生效的配置，便于接口响应。"""

        caption = self.raw_config.get("caption", {})
        prompt_content = self._resolve_prompt_content(caption)
        templates_content = self._resolve_templates(caption)
        return {
            "ai_pipeline": self._serialize_ai_config(),
            "caption": {
                "model": caption.get("model", "gpt-4o-mini"),
                "prompt": prompt_content,
                "templates": templates_content,
                "prompt_file": caption.get("prompt_file"),
                "templates_file": caption.get("templates_file"),
            },
        }

    def list_ready_images(self) -> List[Dict[str, Any]]:
        """枚举待发布目录下的素材，标记是否已用过。"""

        directory = self.ai_config.ready_directory
        directory.mkdir(parents=True, exist_ok=True)
        posted = set(self.database.get_posted_images())
        items: List[Dict[str, Any]] = []
        for path in sorted(directory.iterdir()):
            if not path.is_file():
                continue
            if path.suffix.lower() not in _IMAGE_EXTENSIONS:
                continue
            stat = path.stat()
            items.append(
                {
                    "filename": path.name,
                    "path": str(path),
                    "used": path.name in posted,
                    "size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
                }
            )
        return items

    def get_scheduler_overview(self) -> Dict[str, Any]:
        """返回调度器当前状态，用于辅助运营判断。"""

        return self.scheduler.get_overview()

    async def apply_settings_update(
        self,
        *,
        ai_payload: Optional[Dict[str, Any]] = None,
        caption_payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """写入配置覆盖文件并重新加载，确保 UI 修改持久化。"""

        override_path = self._override_path or self._config_path
        existing: Dict[str, Any] = {}
        if override_path.exists():
            try:
                with override_path.open("r", encoding="utf-8") as fp:
                    existing = json.load(fp)
            except json.JSONDecodeError:
                existing = {}

        if ai_payload:
            stored_ai = existing.get("ai_pipeline", {}).copy()
            for key, value in ai_payload.items():
                if isinstance(value, str):
                    stored_ai[key] = value.strip()
                else:
                    stored_ai[key] = value
            existing["ai_pipeline"] = stored_ai

        if caption_payload:
            stored_caption = existing.get("caption", {}).copy()
            if "templates" in caption_payload:
                templates = [
                    item.strip()
                    for item in caption_payload["templates"] or []
                    if item and item.strip()
                ]
                stored_caption["templates"] = templates
            if "prompt" in caption_payload and caption_payload["prompt"] is not None:
                stored_caption["prompt"] = caption_payload["prompt"].strip()
            if "model" in caption_payload and caption_payload["model"] is not None:
                stored_caption["model"] = caption_payload["model"].strip()
            if "prompt_file" in caption_payload:
                prompt_file = caption_payload["prompt_file"]
                if prompt_file is None:
                    stored_caption.pop("prompt_file", None)
                else:
                    value = prompt_file.strip()
                    if value:
                        stored_caption["prompt_file"] = value
                    else:
                        stored_caption.pop("prompt_file", None)
            if "templates_file" in caption_payload:
                templates_file = caption_payload["templates_file"]
                if templates_file is None:
                    stored_caption.pop("templates_file", None)
                else:
                    value = templates_file.strip()
                    if value:
                        stored_caption["templates_file"] = value
                    else:
                        stored_caption.pop("templates_file", None)
            existing["caption"] = stored_caption

        override_path.parent.mkdir(parents=True, exist_ok=True)
        with override_path.open("w", encoding="utf-8") as fp:
            json.dump(existing, fp, ensure_ascii=False, indent=2, sort_keys=True)
            fp.write("\n")

        await self.scheduler.shutdown()
        self.reload()
        await self.scheduler.start()
        return self.get_settings_snapshot()


class AIPipelineUpdate(BaseModel):
    """AI 流水线可编辑字段。"""

    enable: Optional[bool] = Field(None, description="是否启用流水线")
    post_slots: Optional[List[str]] = Field(
        None,
        description="每日自动发布的时间点，例如 11:00",
    )
    image_source: Optional[str] = Field(None, description="图片来源配置")
    prompt_template: Optional[str] = Field(None, description="图像生成提示词模板")
    caption_style: Optional[str] = Field(None, description="文案风格标识")
    openai_api_key: Optional[str] = Field(None, description="OpenAI 密钥覆盖")
    replicate_model: Optional[str] = None
    replicate_model_version: Optional[str] = None
    replicate_token: Optional[str] = None
    leonardo_model: Optional[str] = None
    leonardo_token: Optional[str] = None
    timezone: Optional[str] = Field(None, description="调度使用的时区")


class CaptionUpdate(BaseModel):
    """文案相关可编辑字段。"""

    model: Optional[str] = None
    prompt: Optional[str] = None
    templates: Optional[List[str]] = None
    prompt_file: Optional[str] = None
    templates_file: Optional[str] = None


class SettingsUpdate(BaseModel):
    """组合请求体，允许同时更新多个板块。"""

    ai_pipeline: Optional[AIPipelineUpdate] = None
    caption: Optional[CaptionUpdate] = None


class CaptionPreviewRequest(BaseModel):
    """临时生成文案的请求体。"""

    image_name: Optional[str] = Field(
        None,
        description="待预览的图片文件名，留空则使用默认测试图",
    )
    style: Optional[str] = Field(None, description="临时覆盖的文案风格")
    prompt_override: Optional[str] = Field(
        None,
        description="额外的提示词前缀，不会写回配置",
    )


def create_app(config_path: Path | None = None) -> FastAPI:
    config_file = config_path or Path(os.getenv("AI_PIPELINE_CONFIG", "config.json")).resolve()
    if not config_file.exists():
        raise FileNotFoundError(f"未找到配置文件：{config_file}")

    context = AppContext(config_file)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        """统一处理应用的启动与停止逻辑，避免重复代码。"""

        logging.getLogger(__name__).info("FastAPI 服务启动，配置文件：%s", context.config_path)
        await context.scheduler.start()
        try:
            yield
        finally:
            await context.scheduler.shutdown()

    app = FastAPI(title="AI 虚拟人自动运营平台", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.context = context

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        """提供一个轻量的可视化控制台，方便查看文档与常用操作。"""

        return _load_index_html()

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

    @app.get("/settings/ai")
    async def read_ai_settings(ctx: AppContext = Depends(get_context)) -> Dict[str, Any]:
        """读取当前 AI 流水线与文案配置。"""

        return ctx.get_settings_snapshot()

    @app.put("/settings/ai")
    async def update_ai_settings(
        payload: SettingsUpdate,
        ctx: AppContext = Depends(get_context),
    ) -> Dict[str, Any]:
        """写入配置覆盖文件，避免升级时被覆盖。"""

        if payload.ai_pipeline is None and payload.caption is None:
            raise HTTPException(status_code=400, detail="请至少提供一项配置内容。")

        ai_payload = payload.ai_pipeline.dict(exclude_none=True) if payload.ai_pipeline else None
        caption_payload = (
            payload.caption.dict(exclude_none=True)
            if payload.caption
            else None
        )
        return await ctx.apply_settings_update(
            ai_payload=ai_payload,
            caption_payload=caption_payload,
        )

    @app.get("/posts/history")
    async def post_history(limit: int = 20, ctx: AppContext = Depends(get_context)) -> Dict[str, Any]:
        records = ctx.database.fetch_post_history(limit=limit)
        return {"items": records, "count": len(records)}

    @app.get("/captions/logs")
    async def caption_logs(limit: int = 20, ctx: AppContext = Depends(get_context)) -> Dict[str, Any]:
        records = ctx.database.fetch_caption_logs(limit=limit)
        return {"items": records, "count": len(records)}

    @app.get("/assistant/ready-images")
    async def ready_images(ctx: AppContext = Depends(get_context)) -> Dict[str, Any]:
        """列出待发布目录，便于盘点素材储备。"""

        items = ctx.list_ready_images()
        return {"items": items, "count": len(items)}

    @app.get("/assistant/schedule")
    async def schedule_overview(ctx: AppContext = Depends(get_context)) -> Dict[str, Any]:
        """返回调度器状态，提示下一次运行时间。"""

        return ctx.get_scheduler_overview()

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

    @app.post("/images/generate")
    async def generate_image(ctx: AppContext = Depends(get_context)) -> Dict[str, Any]:
        """触发云端生成一张图片并保存到待发布目录。"""

        image = ctx.image_provider.generate_image()
        success = image.source != "default"
        message = "生成成功" if success else "已返回默认测试图"
        payload: Dict[str, Any] = {
            "message": message,
            "filename": image.path.name,
            "source": image.source,
        }
        if image.metadata:
            payload["metadata"] = image.metadata
        if not success:
            payload["note"] = "请检查配置中的云端服务是否可用。"
        return payload

    @app.post("/assistant/preview-caption")
    async def preview_caption(
        payload: CaptionPreviewRequest,
        ctx: AppContext = Depends(get_context),
    ) -> Dict[str, Any]:
        """实时生成一条文案草稿，帮助运营提前审稿。"""

        if payload.image_name:
            target = ctx.ai_config.ready_directory / payload.image_name
        else:
            target = ctx.ai_config.default_image

        if not target.exists():
            raise HTTPException(status_code=404, detail="未找到指定图片，请确认文件是否存在。")

        style = payload.style or ctx.ai_config.caption_style
        result = await asyncio.to_thread(
            ctx.caption_provider.get_caption,
            target,
            style,
            log_result=False,
            prompt_override=payload.prompt_override,
        )

        response: Dict[str, Any] = {
            "caption": result.text,
            "provider": result.provider,
            "style": style,
            "image": target.name,
        }
        if result.metadata:
            response["metadata"] = result.metadata
        return response

    return app


app = create_app()


def _get_bool_env(key: str, default: bool) -> bool:
    """解析布尔环境变量，默认值与返回值均为布尔型。"""

    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def run() -> None:
    """以 5500 端口启动 FastAPI 服务，可通过环境变量覆盖。"""

    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PORT", "5500"))
    reload = _get_bool_env("APP_RELOAD", True)

    import uvicorn  # 延迟导入，避免在未运行服务时增加依赖

    uvicorn.run("src.main:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    run()
