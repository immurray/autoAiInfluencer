"""与多平台 API 交互的发布组件。"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol
from urllib.parse import urljoin

import requests
import tweepy
from PIL import Image

from .config import TwitterCredentials, XiaohongshuMcpConfig


@dataclass
class PostResult:
    """单个平台发布后的结果。"""

    platform: str
    post_id: Optional[str]
    text: str
    dry_run: bool

    @property
    def tweet_id(self) -> Optional[str]:
        """兼容旧字段，便于历史逻辑复用。"""

        if self.platform == "twitter":
            return self.post_id
        return None


class PosterProtocol(Protocol):
    """用于描述具备发布能力的组件。"""

    platform: str

    @property
    def dry_run(self) -> bool:  # pragma: no cover - 类型提示方法
        ...

    def post(self, image_path: Path, text: str) -> PostResult:
        ...


class TweetPostError(RuntimeError):
    """封装推文发布阶段产生的错误，便于向上层传递人类可读的信息。"""


class TweetPoster:
    """负责将生成好的内容发布至 X(Twitter)。"""

    def __init__(self, credentials: TwitterCredentials, dry_run: bool) -> None:
        self._credentials = credentials
        self._dry_run = dry_run or not credentials.is_configured
        self._logger = logging.getLogger(__name__)
        self._client_v2: Optional[tweepy.Client] = None
        self._client_v1: Optional[tweepy.API] = None
        self.platform = "twitter"

        if not self._dry_run:
            auth = tweepy.OAuth1UserHandler(
                credentials.api_key,
                credentials.api_key_secret,
                credentials.access_token,
                credentials.access_token_secret,
            )
            self._client_v1 = tweepy.API(auth)
            self._client_v2 = tweepy.Client(
                consumer_key=credentials.api_key,
                consumer_secret=credentials.api_key_secret,
                access_token=credentials.access_token,
                access_token_secret=credentials.access_token_secret,
                bearer_token=credentials.bearer_token,
            )
        else:
            self._logger.info("未提供完整的推特凭证，系统将以 dry-run 模式运行。")

    @property
    def dry_run(self) -> bool:
        """是否处于 dry-run 模式。"""

        return self._dry_run

    def post(self, image_path: Path, text: str) -> PostResult:
        """发布推文或模拟发布。"""

        if self._dry_run:
            self._logger.info("[Dry-Run] 模拟发布推文：%s", text)
            return PostResult(platform=self.platform, post_id=None, text=text, dry_run=True)

        assert self._client_v1 is not None
        assert self._client_v2 is not None

        upload_path = self._ensure_supported_media(image_path)

        media_type = self._detect_media_type(upload_path)

        try:
            with upload_path.open("rb") as file_obj:
                media = self._client_v1.media_upload(
                    filename=str(upload_path),
                    file=file_obj,
                )
            self._logger.debug("媒体类型检测结果：%s，将按照 Tweepy 默认推断上传。", media_type)
            response = self._client_v2.create_tweet(text=text, media_ids=[media.media_id])
        except tweepy.errors.Forbidden as exc:
            detail = self._extract_twitter_error_detail(exc)
            hint = (
                "X API 返回 403 Forbidden，通常表示当前应用或访问令牌缺少写权限或媒体上传权限。"
                "请在 X Developer Portal 中确认应用权限已启用 Read 与 Write，"
                "并重新生成 Access Token / Secret。"
            )
            raise TweetPostError(f"{hint} 详细信息：{detail}") from exc
        except tweepy.TweepyException as exc:
            detail = self._extract_twitter_error_detail(exc)
            raise TweetPostError(f"调用 X API 失败：{detail}") from exc
        except Exception as exc:  # pylint: disable=broad-except
            raise TweetPostError(f"发布推文时发生未知错误：{exc}") from exc

        tweet_id = str(response.data.get("id")) if response.data else None
        self._logger.info("推文已发布，ID：%s", tweet_id)
        return PostResult(platform=self.platform, post_id=tweet_id, text=text, dry_run=False)

    @staticmethod
    def _extract_twitter_error_detail(exc: Exception) -> str:
        """提取 Tweepy 异常中的详细错误信息，帮助排查权限或配置问题。"""

        response = getattr(exc, "response", None)
        if response is None:
            return str(exc)

        try:
            payload = response.json()
        except Exception:  # pragma: no cover - 回退到字符串表示
            return str(exc)

        if isinstance(payload, dict):
            errors = payload.get("errors")
            details = []
            if isinstance(errors, list):
                for item in errors:
                    if not isinstance(item, dict):
                        continue
                    code = item.get("code") or item.get("title")
                    message = item.get("message") or item.get("detail")
                    if code and message:
                        details.append(f"{code}: {message}")
                    elif message:
                        details.append(str(message))
            detail_text = payload.get("detail")
            if detail_text:
                details.append(str(detail_text))
            if details:
                return "；".join(details)
            try:
                return json.dumps(payload, ensure_ascii=False)
            except Exception:  # pragma: no cover
                return str(payload)

        return str(exc)

    def _ensure_supported_media(self, image_path: Path) -> Path:
        """确保上传的图片为 X 平台支持的格式，必要时自动转码。"""

        try:
            with Image.open(image_path) as image:
                fmt = (image.format or "").upper()
                if fmt in {"PNG", "JPEG", "JPG", "GIF"}:
                    return image_path

                target = image_path.with_name(f"{image_path.stem}_twitter.png")
                self._logger.info("检测到图片格式 %s，自动转码为 PNG：%s", fmt or "unknown", target.name)

                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGBA")
                image.save(target, format="PNG")
                return target
        except Exception as exc:  # pylint: disable=broad-except
            raise TweetPostError(f"无法处理待上传图片：{exc}") from exc

    def _detect_media_type(self, image_path: Path) -> str:
        """推断图片的 MIME 类型，确保上传时显式声明，避免服务端识别失败。"""

        mime, _ = mimetypes.guess_type(str(image_path))
        mime = self._normalize_mime_type(mime)
        if mime:
            return mime

        try:
            with Image.open(image_path) as image:
                fmt = (image.format or "").upper()
        except Exception as exc:  # pylint: disable=broad-except
            raise TweetPostError(f"无法识别图片 MIME 类型：{exc}") from exc

        if fmt:
            mime_from_pil = Image.MIME.get(fmt)
            mime_from_pil = self._normalize_mime_type(mime_from_pil)
            if mime_from_pil:
                return mime_from_pil

        raise TweetPostError("无法识别图片的具体 MIME 类型，请检查文件格式是否受支持。")

    @staticmethod
    def _normalize_mime_type(mime: Optional[str]) -> Optional[str]:
        """对猜测出的 MIME 类型进行归一化，确保符合 X API 的预期。"""

        if not mime:
            return None

        normalized = mime.lower()
        # X 平台允许的媒体 MIME 列表（常见图片类型）。
        allowed = {"image/png", "image/jpeg", "image/gif"}

        if normalized in allowed:
            return normalized

        # 针对部分系统可能返回的别名进行归一化。
        alias_map = {
            "image/x-png": "image/png",
            "image/pjpeg": "image/jpeg",
            "image/jpg": "image/jpeg",
        }

        mapped = alias_map.get(normalized)
        if mapped in allowed:
            return mapped

        return None


class XiaohongshuPostError(RuntimeError):
    """封装小红书 MCP 相关错误。"""


class XiaohongshuPoster:
    """负责与小红书 MCP 服务对接。"""

    platform = "xiaohongshu"

    def __init__(self, config: XiaohongshuMcpConfig, dry_run: bool) -> None:
        self._config = config
        self._dry_run = dry_run or not config.is_configured or not config.enable
        self._logger = logging.getLogger(__name__)
        self._session = requests.Session()
        self._access_token: Optional[str] = None
        self._expire_at: float = 0.0

        if not config.enable:
            self._logger.info("小红书渠道在配置中被禁用，将跳过实际推送。")
        elif self._dry_run:
            self._logger.info("小红书 MCP 凭证不完整，系统将对该渠道执行 dry-run。")

    @property
    def dry_run(self) -> bool:
        """是否处于 dry-run 模式。"""

        return self._dry_run

    def post(self, image_path: Path, text: str) -> PostResult:
        """发布小红书笔记或模拟发布。"""

        title = self._build_title(text)
        if self._dry_run:
            self._logger.info("[Dry-Run] 模拟发布小红书笔记《%s》", title)
            return PostResult(platform=self.platform, post_id=None, text=text, dry_run=True)

        token = self._ensure_access_token()
        payload = {
            "channel_id": self._config.channel_id,
            "title": title,
            "content": text,
            "note_type": self._config.note_type,
            "visibility": self._config.visibility,
            "image_base64": self._encode_image(image_path),
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        url = self._build_url(self._config.publish_endpoint)
        try:
            response = self._session.post(url, json=payload, headers=headers, timeout=self._config.timeout)
        except requests.RequestException as exc:
            raise XiaohongshuPostError(f"调用小红书 MCP 接口失败：{exc}") from exc

        if response.status_code >= 400:
            detail = self._safe_extract_error(response)
            raise XiaohongshuPostError(f"小红书 MCP 返回错误：HTTP {response.status_code} - {detail}")

        data = self._safe_json(response)
        note_id = self._extract_note_id(data)
        self._logger.info("小红书笔记已发布，ID：%s", note_id or "未知")
        return PostResult(platform=self.platform, post_id=note_id, text=text, dry_run=False)

    def _ensure_access_token(self) -> str:
        """按需刷新访问令牌，避免频繁请求。"""

        now = time.time()
        if self._access_token and now < self._expire_at - 30:
            return self._access_token

        url = self._build_url(self._config.token_endpoint)
        payload = {
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "grant_type": "client_credentials",
        }

        try:
            response = self._session.post(url, json=payload, timeout=self._config.timeout)
        except requests.RequestException as exc:
            raise XiaohongshuPostError(f"获取小红书访问令牌失败：{exc}") from exc

        if response.status_code >= 400:
            detail = self._safe_extract_error(response)
            raise XiaohongshuPostError(f"获取小红书访问令牌失败：HTTP {response.status_code} - {detail}")

        data = self._safe_json(response)
        token = data.get("access_token")
        expires_in = int(data.get("expires_in", 3600))
        if not token:
            raise XiaohongshuPostError("令牌响应中缺少 access_token 字段。")

        self._access_token = token
        self._expire_at = now + max(expires_in - 30, 60)
        return token

    def _build_url(self, endpoint: str) -> str:
        """拼接完整的接口地址。"""

        base = self._config.base_url or ""
        return urljoin(base.rstrip("/") + "/", endpoint.lstrip("/"))

    def _encode_image(self, image_path: Path) -> str:
        """将图片转为 Base64，便于通过 JSON 上传。"""

        try:
            content = image_path.read_bytes()
        except OSError as exc:
            raise XiaohongshuPostError(f"读取图片失败：{exc}") from exc

        encoded = base64.b64encode(content).decode("utf-8")
        return encoded

    def _build_title(self, text: str) -> str:
        """根据模板生成笔记标题。"""

        summary = text.strip().splitlines()[0] if text.strip() else "AI 灵感"
        summary = summary[: self._config.title_max_length].rstrip()
        if not summary:
            summary = "AI 灵感"
        try:
            title = self._config.title_template.format(summary=summary, text=text)
        except Exception:  # pylint: disable=broad-except
            title = summary
        return title[: self._config.title_max_length] if self._config.title_max_length > 0 else title

    @staticmethod
    def _safe_extract_error(response: requests.Response) -> str:
        """提取响应中的可读错误信息。"""

        try:
            payload = response.json()
        except ValueError:
            return response.text

        if isinstance(payload, dict):
            message = payload.get("error_description") or payload.get("message")
            if message:
                return str(message)
        return response.text

    @staticmethod
    def _safe_json(response: requests.Response) -> dict:
        """安全解析 JSON 响应。"""

        try:
            data = response.json()
        except ValueError as exc:
            raise XiaohongshuPostError(f"解析小红书响应失败：{exc}") from exc

        if not isinstance(data, dict):
            raise XiaohongshuPostError("小红书响应格式异常，期望为对象。")
        return data

    @staticmethod
    def _extract_note_id(data: dict) -> Optional[str]:
        """尝试从响应中提取笔记 ID。"""

        for key in ("note_id", "id", "data"):
            if key not in data:
                continue
            value = data[key]
            if isinstance(value, dict):
                note_id = value.get("note_id") or value.get("id")
                if note_id:
                    return str(note_id)
            elif value:
                return str(value)
        return None


__all__ = [
    "TweetPoster",
    "XiaohongshuPoster",
    "PostResult",
    "PosterProtocol",
    "TweetPostError",
    "XiaohongshuPostError",
]
