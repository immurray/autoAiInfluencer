"""FastAPI 应用入口，整合 AI 虚拟人自动发帖流水线。"""

from __future__ import annotations

from contextlib import asynccontextmanager
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

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


class AppContext:
    """应用运行所需的状态集合。"""

    def __init__(self, config_path: Path) -> None:
        self._config_path = config_path
        self._override_path: Optional[Path] = None
        self.reload()

    @property
    def config_path(self) -> Path:
        return self._config_path

    def reload(self) -> None:
        app_config, ai_config, raw_config, override_path = load_settings(self._config_path)
        setup_logging(app_config.log_path)

        self.app_config = app_config
        self.ai_config = ai_config
        self.raw_config = raw_config
        self._override_path = override_path
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
        return {
            "ai_pipeline": self._serialize_ai_config(),
            "caption": {
                "model": caption.get("model", "gpt-4o-mini"),
                "prompt": caption.get("prompt", ""),
                "templates": caption.get("templates", []),
            },
        }

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


class SettingsUpdate(BaseModel):
    """组合请求体，允许同时更新多个板块。"""

    ai_pipeline: Optional[AIPipelineUpdate] = None
    caption: Optional[CaptionUpdate] = None


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

    app = FastAPI(title="AI 虚拟人自动运营平台", version="0.2.0", lifespan=lifespan)
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

        return """
        <!DOCTYPE html>
        <html lang=\"zh-CN\">
        <head>
            <meta charset=\"utf-8\" />
            <title>AI 虚拟人自动运营控制台</title>
            <style>
                :root {
                    color-scheme: light dark;
                    font-family: \"PingFang SC\", \"Microsoft YaHei\", system-ui, sans-serif;
                    background: #f5f7fa;
                }
                body {
                    margin: 0;
                    padding: 2.5rem clamp(1rem, 3vw, 3rem);
                    background: #f5f7fa;
                    color: #1f2933;
                }
                h1 {
                    margin-top: 0;
                    font-size: clamp(1.8rem, 2.5vw, 2.6rem);
                }
                a.button, button {
                    display: inline-flex;
                    align-items: center;
                    gap: 0.25rem;
                    padding: 0.6rem 1.2rem;
                    margin: 0.25rem 0.5rem 0.25rem 0;
                    border-radius: 999px;
                    border: 1px solid #2563eb;
                    background: #2563eb;
                    color: #fff;
                    font-weight: 600;
                    cursor: pointer;
                    text-decoration: none;
                    transition: filter 0.2s ease;
                }
                a.button:hover, button:hover {
                    filter: brightness(1.05);
                }
                section {
                    background: rgba(255, 255, 255, 0.86);
                    border-radius: 18px;
                    padding: 1.5rem;
                    margin-top: 1.8rem;
                    box-shadow: 0 18px 38px rgba(15, 23, 42, 0.08);
                }
                pre {
                    background: rgba(15, 23, 42, 0.85);
                    color: #f8fafc;
                    padding: 1rem;
                    border-radius: 12px;
                    overflow-x: auto;
                    white-space: pre-wrap;
                    word-break: break-all;
                }
                ul {
                    padding-left: 1.2rem;
                    line-height: 1.65;
                }
                iframe {
                    width: 100%;
                    min-height: 520px;
                    border: none;
                    border-radius: 12px;
                    box-shadow: inset 0 0 0 1px rgba(99, 102, 241, 0.25);
                    background: #fff;
                }
                footer {
                    margin-top: 2rem;
                    font-size: 0.85rem;
                    color: #64748b;
                    text-align: center;
                }
            </style>
        </head>
        <body>
            <header>
                <h1>AI 虚拟人自动运营控制台</h1>
                <p>这里提供服务健康状态、最近任务与接口文档的快速入口，便于演示与调试。</p>
                <div>
                    <a class=\"button\" href=\"/docs\" target=\"_blank\" rel=\"noreferrer\">打开交互式文档</a>
                    <button id=\"refresh-health\">刷新服务状态</button>
                    <button id=\"trigger-run\">手动触发流水线</button>
                    <button id=\"load-history\">查看最近发文记录</button>
                </div>
            </header>

            <section>
                <h2>服务状态</h2>
                <pre id=\"health\">点击“刷新服务状态”以加载实时信息。</pre>
            </section>

            <section>
                <h2>文案提示词配置</h2>
                <p>点击下方按钮即可查看当前的提示词与模板，便于在更新配置前了解现状。</p>
                <button id=\"load-caption-config\">加载文案配置</button>
                <div id=\"caption-config\">
                    <p>尚未加载配置。</p>
                </div>
            </section>

            <section>
                <h2>最近发文记录</h2>
                <ul id=\"history-list\"><li>点击“查看最近发文记录”获取最新数据。</li></ul>
            </section>

            <section>
                <h2>接口文档预览</h2>
                <iframe src=\"/docs\" title=\"FastAPI 文档\"></iframe>
            </section>

            <footer>如需更多高级功能，请直接使用顶部的交互式文档。</footer>

            <script>
            const healthOutput = document.querySelector('#health');
            const historyList = document.querySelector('#history-list');
            const captionConfig = document.querySelector('#caption-config');

            function escapeHtml(value) {
                if (value === null || value === undefined) {
                    return '';
                }
                return String(value).replace(/[&<>"']/g, (ch) => ({
                    '&': '&amp;',
                    '<': '&lt;',
                    '>': '&gt;',
                    '"': '&quot;',
                    "'": '&#39;',
                })[ch] || ch);
            }

            function showToast(message, type = 'info') {
                const toast = document.createElement('div');
                toast.textContent = message;
                toast.style.position = 'fixed';
                toast.style.right = '1.5rem';
                toast.style.bottom = '1.5rem';
                toast.style.padding = '0.75rem 1.2rem';
                toast.style.borderRadius = '999px';
                toast.style.fontWeight = '600';
                toast.style.background = type === 'error' ? '#ef4444' : '#10b981';
                toast.style.color = '#fff';
                toast.style.boxShadow = '0 18px 38px rgba(15, 23, 42, 0.18)';
                toast.style.zIndex = '9999';
                document.body.appendChild(toast);
                setTimeout(() => toast.remove(), 2600);
            }

            document.querySelector('#refresh-health').addEventListener('click', async () => {
                healthOutput.textContent = '正在加载...';
                try {
                    const resp = await fetch('/health');
                    const data = await resp.json();
                    healthOutput.textContent = JSON.stringify(data, null, 2);
                } catch (error) {
                    healthOutput.textContent = '加载失败：' + error;
                }
            });

            document.querySelector('#load-caption-config').addEventListener('click', async () => {
                captionConfig.innerHTML = '<p>正在读取配置...</p>';
                try {
                    const resp = await fetch('/settings/ai');
                    const data = await resp.json();
                    const caption = data.caption || {};
                    const templates = Array.isArray(caption.templates) ? caption.templates : [];

                    const templateList = templates.length
                        ? `<ol>${templates.map(item => `<li>${escapeHtml(item)}</li>`).join('')}</ol>`
                        : '<p>当前未设置模板。</p>';

                    captionConfig.innerHTML = `
                        <p><strong>模型：</strong> ${escapeHtml(caption.model || '未配置')}</p>
                        <p><strong>提示词：</strong></p>
                        <pre>${escapeHtml(caption.prompt || '未配置提示词')}</pre>
                        <p><strong>模板列表：</strong></p>
                        ${templateList}
                    `;
                } catch (error) {
                    captionConfig.innerHTML = `<p>读取失败：${error}</p>`;
                }
            });

            document.querySelector('#trigger-run').addEventListener('click', async () => {
                try {
                    const resp = await fetch('/pipeline/run', { method: 'POST' });
                    const data = await resp.json();
                    showToast(data.message || '流水线任务已提交');
                    if (data.note) {
                        showToast(data.note, 'error');
                    }
                } catch (error) {
                    showToast('触发失败：' + error, 'error');
                }
            });

            document.querySelector('#load-history').addEventListener('click', async () => {
                historyList.innerHTML = '<li>正在读取...</li>';
                try {
                    const resp = await fetch('/posts/history?limit=10');
                    const data = await resp.json();
                    if (!data.items || data.items.length === 0) {
                        historyList.innerHTML = '<li>暂无记录。</li>';
                        return;
                    }
                    historyList.innerHTML = data.items.map(item => {
                        const createdAt = item.created_at || item.createdAt || item.created || '未知时间';
                        const caption = item.caption || '（无文案）';
                        return `<li><strong>${createdAt}</strong> - ${caption}</li>`;
                    }).join('');
                } catch (error) {
                    historyList.innerHTML = `<li>读取失败：${error}</li>`;
                }
            });
            </script>
        </body>
        </html>
        """

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
