"""文案生成组件。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import logging
import random

from openai import OpenAI

from .config import CaptionConfig, mask_sensitive_value


@dataclass
class CaptionResult:
    """文案生成结果数据。"""

    text: str
    used_template: Optional[str]
    model: Optional[str]


class CaptionGenerator:
    """负责基于图片信息生成推文文案。"""

    def __init__(self, config: CaptionConfig, api_key: Optional[str]) -> None:
        self._config = config
        self._api_key = api_key
        self._client: Optional[OpenAI] = None
        if api_key:
            self._client = OpenAI(api_key=api_key)
        self._logger = logging.getLogger(__name__)

        masked_key = mask_sensitive_value(api_key)
        if api_key:
            self._logger.info("OpenAI API Key 已注入，掩码后为：%s", masked_key)
        else:
            self._logger.warning("OpenAI API Key 未配置，文案将使用本地模板。")

    def _build_prompt(self, image_path: Path) -> str:
        """根据图片文件生成提示语。"""

        return f"{self._config.prompt}\n文件名：{image_path.name}"

    def generate(self, image_path: Path) -> CaptionResult:
        """根据配置生成一段文案。"""

        if self._client is None:
            template = random.choice(self._config.templates)
            text = template.format(filename=image_path.stem)
            self._logger.debug("使用模板文案：%s", text)
            return CaptionResult(text=text, used_template=template, model=None)

        prompt = self._build_prompt(image_path)
        try:
            response = self._client.chat.completions.create(
                model=self._config.model,
                messages=[
                    {
                        "role": "system",
                        "content": "你是一位专门撰写社交媒体推文的中文创意助理。",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.8,
                max_tokens=200,
            )
        except Exception as exc:
            self._logger.exception("调用 OpenAI 文案服务失败，将改用模板：%s", exc)
            template = random.choice(self._config.templates)
            text = template.format(filename=image_path.stem)
            return CaptionResult(text=text, used_template=template, model=None)

        message = response.choices[0].message.content.strip()
        return CaptionResult(text=message, used_template=None, model=self._config.model)


__all__ = ["CaptionGenerator", "CaptionResult"]
