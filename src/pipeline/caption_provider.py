"""文案生成模块，支持 OpenAI 与本地模板。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
import json
import logging
import random
import requests

from ..config import AIPipelineConfig
from ..database import Database

try:  # pragma: no cover
    from openai import OpenAI
    from openai import AuthenticationError as OpenAIAuthError
except Exception:  # pylint: disable=broad-except
    OpenAI = None  # type: ignore

    class OpenAIAuthError(Exception):
        """在缺少 openai 库时兜底的认证错误类型。"""

        pass


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
        self._http_fallback_enabled = bool(config.openai_api_key)
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
            except OpenAIAuthError as exc:  # pragma: no cover - 仅在真实调用时触发
                self._handle_openai_auth_error("OpenAI SDK", exc)
            except Exception as exc:  # pylint: disable=broad-except
                self._logger.exception("OpenAI 文案生成失败：%s", exc)

        if self._http_fallback_enabled and self._config.openai_api_key:
            try:
                caption = self._call_openai_http(image_path, style)
                self._log_caption(image_path, caption, "openai_http", metadata)
                return CaptionResult(text=caption, provider="openai_http", metadata=metadata)
            except requests.exceptions.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 401:
                    self._handle_openai_auth_error("OpenAI HTTP", exc)
                else:
                    self._logger.exception("OpenAI HTTP 文案生成失败：%s", exc)
            except Exception as exc:  # pylint: disable=broad-except
                self._logger.exception("OpenAI HTTP 文案生成失败：%s", exc)

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

        messages = [
            {"role": "system", "content": "你是一名资深新媒体编辑。"},
            {"role": "user", "content": prompt},
        ]

        if hasattr(self._client, "chat") and hasattr(self._client.chat, "completions"):
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=0.8,
                max_tokens=200,
            )
        elif hasattr(self._client, "responses"):
            response = self._client.responses.create(
                model=self._model,
                input=self._convert_messages_to_responses(messages),
                temperature=0.8,
                max_output_tokens=200,
            )
        else:  # pragma: no cover - 仅在未来 SDK 出现兼容性问题时触发
            raise RuntimeError("当前 OpenAI SDK 不支持 chat.completions 或 responses 接口")

        text = self._extract_text_from_openai_response(response)
        if not text:
            raise ValueError("OpenAI 返回内容为空")
        return text

    def _convert_messages_to_responses(self, messages: list[dict]) -> list[dict]:
        """将 Chat Completions 风格的消息转成 Responses 接口支持的格式。"""

        converted: list[dict] = []
        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")
            if isinstance(content, str):
                converted.append(
                    {
                        "role": role,
                        "content": [
                            {
                                "type": "text",
                                "text": content,
                            }
                        ],
                    }
                )
            else:
                converted.append(message)
        return converted

    def _extract_text_from_openai_response(self, response: Any) -> str:
        """兼容不同 OpenAI SDK 版本的返回结构，提取文本。"""

        # 优先处理 chat.completions 的结构
        choices = getattr(response, "choices", None)
        if choices:
            choice = choices[0]
            message = getattr(choice, "message", None)
            if message is not None:
                content = getattr(message, "content", None)
                text = self._normalize_openai_content(content)
                if text:
                    return text

        data = self._to_dict(response)
        if not data:
            return ""

        if "choices" in data:
            choices_data = data.get("choices", [])
            if choices_data:
                message_data = choices_data[0].get("message", {})
                text = self._normalize_openai_content(message_data.get("content"))
                if text:
                    return text

        if "output_text" in data and data["output_text"]:
            return str(data["output_text"]).strip()

        output_items = data.get("output")
        if isinstance(output_items, Iterable) and not isinstance(output_items, (str, bytes)):
            texts: list[str] = []
            for item in output_items:
                item_dict = item
                if not isinstance(item_dict, dict):
                    item_dict = getattr(item, "model_dump", lambda: {})()
                contents = item_dict.get("content")
                if isinstance(contents, Iterable) and not isinstance(contents, (str, bytes)):
                    for part in contents:
                        part_dict = part
                        if not isinstance(part_dict, dict):
                            part_dict = getattr(part, "model_dump", lambda: {})()
                        text = part_dict.get("text")
                        if text:
                            texts.append(str(text))
            joined = "".join(texts).strip()
            if joined:
                return joined

        return ""

    @staticmethod
    def _normalize_openai_content(content: Any) -> str:
        """将不同结构的 content 字段统一为纯文本。"""

        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            texts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if text:
                        texts.append(str(text))
                else:
                    text = getattr(item, "text", None)
                    if text:
                        texts.append(str(text))
            return "".join(texts).strip()
        return ""

    @staticmethod
    def _to_dict(response: Any) -> Dict[str, Any]:
        """尽可能地将 OpenAI 响应对象转换为字典。"""

        if isinstance(response, dict):
            return response
        for attr in ("model_dump", "dict", "to_dict"):
            method = getattr(response, attr, None)
            if callable(method):
                try:
                    data = method()
                    if isinstance(data, dict):
                        return data
                except Exception:  # pragma: no cover - 转换失败时忽略
                    continue
        return {}

    def _call_openai_http(self, image_path: Path, style: str) -> str:
        """使用 HTTP 调用 OpenAI 接口的兜底逻辑。"""

        if not self._config.openai_api_key:
            raise ValueError("未配置 OpenAI API Key")

        prompt = (
            f"请为文件名为 {image_path.name} 的图片编写一段适合 X 平台的中文文案，"
            f"整体风格为 {style}，需要包含 2-3 个 emoji 与至少 2 个话题标签，"
            "总长度控制在 100 字以内。"
        )
        if self._prompt:
            prompt = f"{self._prompt}\n\n{prompt}"

        headers = {
            "Authorization": f"Bearer {self._config.openai_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": "你是一名资深新媒体编辑。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.8,
            "max_tokens": 200,
        }

        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            raise ValueError("OpenAI HTTP 返回内容为空")

        # OpenAI 新版接口会返回 content 列表，因此需要做统一的文本提取
        content = choices[0].get("message", {}).get("content")
        text = self._normalize_openai_content(content)
        if not text:
            raise ValueError("OpenAI HTTP 返回文本为空")
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

    def _handle_openai_auth_error(self, provider: str, exc: Exception) -> None:
        """当检测到 OpenAI 返回 401 时，关闭后续云端调用。"""

        message = getattr(exc, "message", str(exc))
        self._logger.error("%s 返回未授权错误，已停用 OpenAI 文案：%s", provider, message)
        self._client = None
        self._http_fallback_enabled = False


__all__ = ["CaptionProvider", "CaptionResult"]
