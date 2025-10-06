"""文案生成模块，支持 OpenAI 与本地模板。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import json
import logging
import random

from ..config import AIPipelineConfig
from ..database import Database

try:  # pragma: no cover
    from openai import OpenAI
except Exception:  # pylint: disable=broad-except
    OpenAI = None  # type: ignore


@dataclass
class CaptionResult:
    """封装文案生成的结果。"""

    text: str
    provider: str
    metadata: Optional[dict] = None


class CaptionProvider:
    """根据配置生成 X 平台文案。"""

    def __init__(
        self,
        config: AIPipelineConfig,
        database: Database,
        raw_config: dict,
    ) -> None:
        self._config = config
        self._database = database
        self._logger = logging.getLogger(__name__)
        self._templates = raw_config.get("caption", {}).get("templates", [])
        self._prompt = raw_config.get("caption", {}).get("prompt", "")
        self._model = raw_config.get("caption", {}).get("model", "gpt-4o-mini")
        self._log_file = config.caption_log_directory / "captions.log"
        self._log_file.parent.mkdir(parents=True, exist_ok=True)

        self._client = None
        if config.openai_api_key and OpenAI is not None:
            try:
                self._client = OpenAI(api_key=config.openai_api_key)
            except Exception as exc:  # pylint: disable=broad-except
                self._logger.exception("初始化 OpenAI 客户端失败：%s", exc)
                self._client = None
        elif config.openai_api_key:
            self._logger.warning("未安装 openai 库，无法使用云端文案。")

    def get_caption(self, image_path: Path, style: Optional[str] = None) -> CaptionResult:
        """获取文案，优先调用 OpenAI。"""

        style = style or self._config.caption_style
        metadata: dict = {"style": style, "image": image_path.name}

        if self._client is not None:
            try:
                caption = self._call_openai(image_path, style)
                self._log_caption(image_path, caption, "openai", metadata)
                return CaptionResult(text=caption, provider="openai", metadata=metadata)
            except Exception as exc:  # pylint: disable=broad-except
                self._logger.exception("OpenAI 文案生成失败：%s", exc)

        caption = self._generate_from_template(image_path, style)
        self._log_caption(image_path, caption, "template", metadata)
        return CaptionResult(text=caption, provider="template", metadata=metadata)

    def _call_openai(self, image_path: Path, style: str) -> str:
        assert self._client is not None
        prompt = (
            f"请为文件名为 {image_path.name} 的图片编写一段适合 X 平台的中文文案，"
            f"整体风格为 {style}，需要包含 2-3 个 emoji 与至少 2 个话题标签，"
            "总长度控制在 100 字以内。"
        )
        if self._prompt:
            prompt = f"{self._prompt}\n\n{prompt}"

        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": "你是一名资深新媒体编辑。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.8,
            max_tokens=200,
        )
        choice = response.choices[0]
        text = (choice.message.content or "").strip()
        if not text:
            raise ValueError("OpenAI 返回内容为空")
        return text

    def _generate_from_template(self, image_path: Path, style: str) -> str:
        if self._templates:
            template = random.choice(self._templates)
            caption = template.format(filename=image_path.name, style=style)
        else:
            caption = f"今天的主角是 {image_path.stem}，欢迎在评论区分享你的看法！ #AI #虚拟人"
        return caption

    def _log_caption(self, image_path: Path, caption: str, provider: str, metadata: dict) -> None:
        record = {
            "image": image_path.name,
            "caption": caption,
            "provider": provider,
            "metadata": metadata,
        }
        try:
            self._database.log_caption(
                image_path=image_path,
                caption=caption,
                style=metadata.get("style", ""),
                provider=provider,
                extra=metadata,
            )
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.exception("写入数据库 caption_log 失败：%s", exc)

        try:
            with self._log_file.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.exception("写入 caption 日志文件失败：%s", exc)


__all__ = ["CaptionProvider", "CaptionResult"]
