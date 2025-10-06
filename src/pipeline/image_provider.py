"""图片提供模块，兼顾本地轮询与云端生成。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import base64
import json
import logging
import time
import uuid

from ..config import AIPipelineConfig
from ..database import Database

try:  # pragma: no cover - 依赖在运行环境中按需安装
    import requests
except Exception:  # pylint: disable=broad-except
    requests = None  # type: ignore


_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_DEFAULT_PLACEHOLDER = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAACXBIWXMAAAsSAAALEgHS3X78AAABfElEQVR4nO2aMU7DMBBF39YhzAGSACSwAElwAEmABJLAASXAATsg3NEEraPFc+UqBnob+Lbs93nE4AAAAAAAAAADwHcN2gVeXuVusYObQtdgEoGf4dAHMWVvUhYAC4ijKkztaAP6gdzppQzcE6huqZwll9gDpc5zsxFrqxbPjzvY4xXHzkwmo7aX6ixkmfsVNH+pEwzTp/DMAYA9YgAXgCBFAANEMAE0QwATRAABRDAFNEAAEUQwDTBABNEEAFEMAU0QAARQwDTBABNEEAURS0x3KgwxhpTAMsoZ07dAhZEBtcHTAr3e8DqgHwhjaxAgAnjcMqxsDCYXgEWh05rq6ArlTcidH8333AdxAi8uKXu95ACnHCqB6gINVi6zkRgLpmjDoE7YOhDdyCjTiMQuILzcuEGoVYBoNvAQmJsteVEhDWUpAJZciV06P88wJEqn3Ejj6+UeJ8V+RaHcRUW2KIiMzFxDBI0X58F3RrgPf63HgFUsVTNAgwAAAABJRU5ErkJggg=="
)


@dataclass
class ImageResult:
    """封装图片选择或生成的结果。"""

    path: Path
    source: str
    metadata: Optional[dict] = None


class ImageProvider:
    """提供图片素材，优先使用本地素材，其次调用云端服务。"""

    def __init__(self, config: AIPipelineConfig, database: Database) -> None:
        self._config = config
        self._database = database
        self._logger = logging.getLogger(__name__)
        self._ready_dir = config.ready_directory
        self._ready_dir.mkdir(parents=True, exist_ok=True)

    def get_image(self) -> ImageResult:
        """获取一张待发布图片，必要时从云端生成。"""

        posted = {name for name in self._database.get_posted_images()}
        local = self._pick_local(posted)
        if local:
            return local

        generated = self._generate_cloud_image(force=False)
        if generated:
            return generated

        if self._config.enable and self._config.is_cloud_enabled:
            self._logger.warning("云端生成失败，回退默认测试图。")
        else:
            self._logger.info("云端生成未启用，回退默认测试图。")
        return self._default_image()

    def generate_image(self) -> ImageResult:
        """主动触发一次云端图片生成，便于前端控制台调用。"""

        generated = self._generate_cloud_image(force=True)
        if generated:
            return generated

        self._logger.warning("云端生成不可用，返回默认测试图以便调试。")
        return self._default_image()

    def _pick_local(self, posted: set[str]) -> Optional[ImageResult]:
        candidates = [
            path
            for path in sorted(self._ready_dir.iterdir())
            if path.is_file() and path.suffix.lower() in _IMAGE_EXTENSIONS
        ]
        for path in candidates:
            if path.name not in posted:
                self._logger.info("选取本地图片：%s", path.name)
                return ImageResult(path=path, source="local", metadata={"reason": "ready_to_post"})
        return None

    def _default_image(self) -> ImageResult:
        target = self._config.default_image
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(base64.b64decode(_DEFAULT_PLACEHOLDER))
        return ImageResult(path=target, source="default", metadata={"reason": "fallback"})

    def _generate_cloud_image(self, *, force: bool) -> Optional[ImageResult]:
        """根据配置调用云端服务生成图片。"""

        if not self._config.is_cloud_enabled:
            if force:
                self._logger.error("当前 image_source=%s，未启用云端图片生成。", self._config.image_source)
            return None

        if not force and not self._config.enable:
            self._logger.info("AI 流水线未启用，跳过云端图片生成。")
            return None

        generator = {
            "replicate": self._generate_with_replicate,
            "leonardo": self._generate_with_leonardo,
        }.get(self._config.image_source, self._generate_with_replicate)

        generated = generator()
        if not generated and force:
            self._logger.error("云端生成失败，请检查凭证或模型配置。")
        return generated

    def _generate_with_replicate(self) -> Optional[ImageResult]:
        if requests is None:
            self._logger.error("未安装 requests，无法调用 Replicate。")
            return None
        if not self._config.replicate_token or not self._config.replicate_model:
            self._logger.error("缺少 Replicate 凭证或模型配置。")
            return None

        headers = {
            "Authorization": f"Token {self._config.replicate_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "version": self._config.replicate_model,
            "input": {"prompt": self._config.prompt_template},
        }
        try:
            response = requests.post(
                "https://api.replicate.com/v1/predictions",
                headers=headers,
                data=json.dumps(payload),
                timeout=30,
            )
            if response.status_code == 401:
                self._logger.error("Replicate 返回 401 未授权，请确认 API Token 是否有效。")
                return None
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.exception("调用 Replicate 失败：%s", exc)
            return None

        prediction_url = data.get("urls", {}).get("get")
        if not prediction_url:
            self._logger.error("Replicate 响应缺少状态轮询地址。")
            return None

        status = data.get("status")
        try:
            while status in {"starting", "processing"}:
                time.sleep(2)
                poll = requests.get(prediction_url, headers=headers, timeout=30)
                poll.raise_for_status()
                data = poll.json()
                status = data.get("status")
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.exception("轮询 Replicate 失败：%s", exc)
            return None

        if status != "succeeded":
            self._logger.error("Replicate 生成失败，状态：%s", status)
            return None

        output = data.get("output")
        if not output:
            self._logger.error("Replicate 输出为空。")
            return None

        image_url = output[0] if isinstance(output, list) else output
        return self._download_remote_image(str(image_url), provider="replicate", extra={"status": status})

    def _generate_with_leonardo(self) -> Optional[ImageResult]:
        if requests is None:
            self._logger.error("未安装 requests，无法调用 Leonardo.ai。")
            return None
        if not self._config.leonardo_token or not self._config.leonardo_model:
            self._logger.error("缺少 Leonardo.ai 凭证或模型配置。")
            return None

        headers = {
            "Authorization": f"Bearer {self._config.leonardo_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "modelId": self._config.leonardo_model,
            "prompt": self._config.prompt_template,
            "num_images": 1,
        }
        try:
            response = requests.post(
                "https://cloud.leonardo.ai/api/rest/v1/generations",
                headers=headers,
                data=json.dumps(payload),
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.exception("调用 Leonardo.ai 失败：%s", exc)
            return None

        generations = data.get("generations") or data.get("data")
        if not generations:
            self._logger.error("Leonardo.ai 返回为空：%s", data)
            return None

        first = generations[0]
        images = first.get("generated_images") or first.get("images") or []
        if not images:
            self._logger.error("Leonardo.ai 未返回图片链接。")
            return None

        image_url = images[0].get("url") or images[0].get("image")
        return self._download_remote_image(str(image_url), provider="leonardo", extra={"generation_id": first.get("id")})

    def _download_remote_image(self, url: str, provider: str, extra: Optional[dict] = None) -> Optional[ImageResult]:
        if requests is None:
            return None
        filename = f"{provider}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.png"
        target = self._ready_dir / filename
        try:
            response = requests.get(url, timeout=60)
            response.raise_for_status()
            target.write_bytes(response.content)
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.exception("下载 %s 图片失败：%s", provider, exc)
            return None

        self._logger.info("成功生成图片：%s", target.name)
        return ImageResult(path=target, source=provider, metadata=extra or {})


__all__ = ["ImageProvider", "ImageResult"]
